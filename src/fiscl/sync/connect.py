"""Human-only connection flow: a one-shot localhost server that hosts the provider's Connect/Link widget.

This is only ever started from the CLI (`fiscl connect ...`), never from an MCP tool, so a model can't open a
bank connection. The server binds to 127.0.0.1 on a random port, serves one page under a random 32-byte URL
token, accepts exactly one POST with the enrollment result, stores the access token in the keychain, records
the connection, and shuts down. It gives up after 10 minutes.
"""

from __future__ import annotations

import errno
import hmac
import json
import re
import secrets
import socket
import sqlite3
import sys
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..config import Config
from ..guard import clean_text
from . import base, connections, plaid, qbo, teller
from .base import SyncError

TIMEOUT_SECONDS = 600
MAX_BODY = 16 * 1024
BIND_HOST = "127.0.0.1"

TELLER_CONNECT_SCRIPT = "https://cdn.teller.io/connect/connect.js"
# NEEDS THE HUMAN SANDBOX CHECK: Teller documents only cdn.teller.io. Connect probably loads more Teller hosts
# (frames, API calls). Run `fiscl connect teller` with environment = "sandbox" in a browser, read the CSP
# violations in the console, and narrow this list to exactly the hosts Connect needs.
TELLER_CONNECT_HOSTS = ("https://cdn.teller.io", "https://teller.io", "https://*.teller.io")

PLAID_LINK_SCRIPT = "https://cdn.plaid.com/link/v2/stable/link-initialize.js"
# Plaid's documented Link CSP needs cdn.plaid.com (script, frame) and the Plaid API hosts (connect).
PLAID_LINK_HOSTS = ("https://cdn.plaid.com", "https://*.plaid.com")

_APP_ID = re.compile(r"[A-Za-z0-9_\-]{1,100}")
_TOKEN = re.compile(r"[A-Za-z0-9_\-.=]{1,512}")
_ENROLLMENT = re.compile(r"[A-Za-z0-9_\-]{1,100}")
_PUBLIC_TOKEN = re.compile(r"public-[A-Za-z0-9_\-]{1,200}")


class OneShotServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, token: str, render: Callable[[str], tuple[str, str]], on_post: Callable[[dict], None]):
        super().__init__((BIND_HOST, 0), _Handler)
        self.token = token
        self.render = render  # nonce -> (html, content-security-policy)
        self.on_post = on_post
        self.consumed = False
        self.result: object = None  # what on_post returned (the saved institution), for the human to confirm
        self.error: str | None = None
        self.deadline: float | None = None  # monotonic; set by serve_once so no connection outlives the flow
        self.timeout = 0.5

    @property
    def url(self) -> str:
        return f"http://{BIND_HOST}:{self.server_port}/{self.token}"


def make_server(token: str, render: Callable[[str], tuple[str, str]], on_post: Callable[[dict], None]) -> OneShotServer:
    """The server factory. It can only ever bind to 127.0.0.1."""
    return OneShotServer(token, render, on_post)


class _Handler(BaseHTTPRequestHandler):
    server: OneShotServer  # type: ignore[assignment]
    server_version = "fiscl-connect"
    sys_version = ""
    timeout = 10  # per socket read; setup() shrinks it so a stalled client can't outlive the overall deadline

    def setup(self):
        deadline = self.server.deadline
        if deadline is not None:
            self.timeout = max(0.05, min(self.timeout, deadline - time.monotonic()))
        super().setup()

    def handle(self):
        # A stalled, half-open, or dropped client must never hang or crash the flow; it just loses its turn.
        try:
            super().handle()
        except (TimeoutError, OSError):
            self.close_connection = True

    def log_message(self, *args):  # nothing may print: the URL contains the token
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        # DNS-rebinding defence: the Host header must be exactly our loopback address and port.
        if self.headers.get("Host") != f"{BIND_HOST}:{self.server.server_port}":
            return False
        path = self.path.split("?", 1)[0]
        return hmac.compare_digest(path.encode(), f"/{self.server.token}".encode())

    def do_GET(self):
        if not self._authorized():
            return self._send(404, b"not found")
        nonce = secrets.token_urlsafe(16)
        html, csp = self.server.render(nonce)
        self._send(200, html.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})

    def do_POST(self):
        if not self._authorized():
            return self._send(404, b"not found")
        if self.server.consumed:
            return self._send(409, b'{"ok": false, "error": "already used"}', "application/json")
        try:
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://{BIND_HOST}:{self.server.server_port}":
                raise ValueError("unexpected origin")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise ValueError("expected JSON")
            length = int(self.headers.get("Content-Length") or "-1")
            if not 0 < length <= MAX_BODY:
                raise ValueError("bad length")
        except ValueError:
            self.server.consumed = True  # a malformed authorised POST still ends the flow
            return self._fail(400, "the result wasn't valid")
        raw = self.rfile.read(length)  # a stall raises TimeoutError: the slot stays free for the real browser
        if len(raw) != length:
            raise ConnectionError("client closed before sending the whole body")
        self.server.consumed = True  # exactly one complete POST, whatever it contains
        try:
            data = json.loads(raw.decode())
            if not isinstance(data, dict):
                raise ValueError("expected an object")
            self.server.result = self.server.on_post(data)
        except (ValueError, SyncError) as exc:
            return self._fail(400, str(exc) if isinstance(exc, SyncError) else "the result wasn't valid")
        except Exception:  # a bug or a full disk must not look like success
            return self._fail(500, "fiscl couldn't save the connection; see the terminal")
        self._send(200, b'{"ok": true}', "application/json")

    def _fail(self, code: int, message: str):
        self.server.error = message
        self._send(code, json.dumps({"ok": False, "error": message}).encode(), "application/json")

    def _nope(self):
        self._send(405, b"method not allowed", extra={"Allow": "GET, POST"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _nope


def serve_once(server: OneShotServer, timeout: float = TIMEOUT_SECONDS) -> None:
    """Handle requests until the single POST has arrived or the deadline passes. Connection handling is bounded by
    the same deadline, so a stalled client can't hold the CLI past it."""
    server.deadline = time.monotonic() + timeout
    try:
        while not server.consumed and time.monotonic() < server.deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not server.consumed:
        raise SyncError("timed out waiting for the bank connection (10 minutes)")
    if server.error:
        raise SyncError(server.error)


def run_flow(
    server: OneShotServer,
    label: str,
    open_browser: Callable[[str], object],
    timeout: float,
    announce: Callable[[str], None],
) -> None:
    """Announce, open the browser (or print the link), and wait for the single POST."""
    announce(f"Opening your browser to connect a bank ({label}). Waiting up to 10 minutes...")
    if not open_browser(server.url):
        announce(f"Open this link in your browser: {server.url}")
    serve_once(server, timeout)
    if isinstance(server.result, str) and server.result:
        announce(f"Saved a connection to: {server.result}. If that isn't the bank you just linked, forget it with")
        announce("`fiscl connect forget <provider> --institution NAME` and connect again.")


def _js(value) -> str:
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


# --- Teller ---------------------------------------------------------------------------------


def teller_csp(nonce: str) -> str:
    hosts = " ".join(TELLER_CONNECT_HOSTS)
    return "; ".join(
        [
            "default-src 'none'",
            f"script-src 'nonce-{nonce}' {hosts}",
            f"style-src 'unsafe-inline' {hosts}",  # Connect injects its own styles
            f"frame-src {hosts}",
            f"connect-src 'self' {hosts}",
            f"img-src data: {hosts}",
            f"font-src {hosts}",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]
    )


def teller_page(app_id: str, environment: str, nonce: str, enrollment_id: str | None = None) -> str:
    reauth = f"enrollmentId: {_js(enrollment_id)},\n      " if enrollment_id else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>fiscl: connect a bank</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem">
<h1>Connect a bank to fiscl</h1>
<p>Read-only: fiscl asks Teller for transactions and balances only. The access token is stored in your
OS keychain on this machine.</p>
<button id="go" type="button">Connect your bank</button>
<p id="status" role="status"></p>
<script src="{TELLER_CONNECT_SCRIPT}"></script>
<script nonce="{nonce}">
(function () {{
  var status = document.getElementById("status");
  function say(t) {{ status.textContent = t; }}
  var teller;
  try {{
    teller = TellerConnect.setup({{
      applicationId: {_js(app_id)},
      {reauth}environment: {_js(environment)},
      products: {_js(teller.PRODUCTS)},
      onSuccess: function (e) {{
        var en = (e && e.enrollment) || {{}};
        var body = {{ accessToken: e && e.accessToken,
                     enrollment: {{ id: en.id, institution: {{ name: en.institution && en.institution.name }} }} }};
        fetch(location.pathname, {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
                                   body: JSON.stringify(body) }})
          .then(function (r) {{ return r.json().then(function (j) {{ return {{ status: r.status, j: j }}; }}); }})
          .then(function (x) {{
            say(x.status === 409
              ? "WARNING: fiscl had already received a result for this link. Do not rely on it: in the terminal run " +
                "`fiscl connect forget <provider>` and connect again."
              : x.j.ok ? "Connected. You can close this tab and go back to the terminal."
                       : "Not saved: " + x.j.error);
          }})
          .catch(function () {{ say("Could not reach fiscl on this machine."); }});
      }},
      onExit: function () {{ say("Closed without connecting."); }},
      onFailure: function () {{ say("Teller reported a problem. Nothing was saved."); }}
    }});
  }} catch (err) {{
    say("Teller Connect didn't load. Check your connection and the terminal.");
  }}
  document.getElementById("go").addEventListener("click", function () {{ if (teller) teller.open(); }});
}})();
</script></body></html>
"""


def parse_teller_enrollment(data: dict) -> tuple[str, str, str]:
    """Validate what the page posted. Returns (access_token, enrollment_id, institution)."""
    token = data.get("accessToken")
    enrollment = data.get("enrollment")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token) or not isinstance(enrollment, dict):
        raise SyncError("Teller's result was missing an access token or enrollment")
    eid = enrollment.get("id")
    if not isinstance(eid, str) or not _ENROLLMENT.fullmatch(eid):
        raise SyncError("Teller's result had an invalid enrollment id")
    inst = enrollment.get("institution")
    name = inst.get("name") if isinstance(inst, dict) else None
    return token, eid, clean_text(name if isinstance(name, str) else "", 80)


def _store_then_record(
    conn: sqlite3.Connection,
    provider: str,
    ref: str,
    token: str,
    institution: str,
    revoke: Callable[[], None] | None,
) -> None:
    """Token to the keychain first, then the connection row. If either fails, put the keychain back exactly as it
    was (restore a token we overwrote rather than deleting a working one) and, when we created the grant just now,
    best-effort revoke it at the provider so a failed save can't leave a live, untracked grant."""
    name = base.secret_name(provider, ref)
    previous = base.secret_get(name)
    try:
        base.secret_set(name, token)
        connections.add_connection(conn, provider, institution, ref)
        conn.commit()
    except Exception:
        conn.rollback()
        try:
            if previous is not None:
                base.secret_set(name, previous)
            else:
                base.secret_delete(name)
        except Exception:  # keep the original failure as the story
            pass
        if previous is None and revoke is not None:
            try:
                revoke()
            except Exception:
                pass
        raise


def save_teller_enrollment(conn: sqlite3.Connection, data: dict, revoke: Callable[[str], None] | None = None) -> str:
    """Store the enrollment's token and row atomically (see `_store_then_record`). `revoke(token)` is called only
    when saving failed and nothing was stored before."""
    token, eid, institution = parse_teller_enrollment(data)
    ref = connections.item_ref_hash("teller", eid)
    _store_then_record(conn, "teller", ref, token, institution, (lambda: revoke(token)) if revoke else None)
    try:  # the raw enrollment id lets `--reconnect` re-authorize this same enrollment later; not a credential
        base.secret_set(base.secret_name("teller", ref + ".eid"), eid)
    except SyncError:
        pass
    return institution


def _pick_reauth(conn: sqlite3.Connection, provider: str, institution: str | None):
    """The one connection that needs re-authorization (optionally narrowed by --institution)."""
    rows = [r for r in connections.list_rows(conn, provider, institution) if r["status"] == "needs_reauth"]
    label = "QuickBooks" if provider == "qbo" else provider.title()
    if not rows:
        raise SyncError(f"no {label} connection needs re-authorization; run `fiscl connect list` to check")
    if len(rows) > 1:
        raise SyncError(f"more than one {label} connection needs re-authorization; pass --institution NAME")
    return rows[0]


def save_teller_reconnect(
    conn: sqlite3.Connection, row: sqlite3.Row, data: dict, revoke: Callable[[str], None] | None = None
) -> str:
    """Replace the stored token for the *same* enrollment. If Teller handed back a different enrollment (which
    would create duplicate accounts), nothing is replaced and the stray grant is revoked."""
    token, eid, _ = parse_teller_enrollment(data)
    ref = row["item_ref_hash"]
    if connections.item_ref_hash("teller", eid) != ref:
        if revoke:
            try:
                revoke(token)
            except Exception:
                pass
        raise SyncError("Teller returned a different enrollment than the one being re-authorized; nothing was changed")
    _store_then_record(conn, "teller", ref, token, row["institution"] or "", None)
    return row["institution"] or ""


def connect_teller(
    cfg: Config,
    conn: sqlite3.Connection,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = TIMEOUT_SECONDS,
    announce: Callable[[str], None] = lambda msg: None,
    opener=None,
    reconnect: bool = False,
    institution: str | None = None,
) -> str:
    """Run the whole flow. Credentials (application id, and the certificate outside sandbox) must already be
    in the keychain; the CLI collects them first. With `reconnect`, re-authorize the one connection that needs it
    through the same enrollment (TellerConnect `enrollmentId`) instead of creating a new one."""
    base.keyring_backend()  # no keychain, no connection
    env = cfg.teller_environment
    app_id = base.secret_get(base.secret_name("teller", "app_id")) or ""
    if not _APP_ID.fullmatch(app_id):
        raise SyncError("no Teller application id is stored; run `fiscl connect teller` again")

    def revoke(tok: str) -> None:
        teller.TellerConnector(cfg, [], opener=opener).revoke_token(tok)

    enrollment_id = None
    if reconnect:
        row = _pick_reauth(conn, "teller", institution)
        enrollment_id = base.secret_get(base.secret_name("teller", row["item_ref_hash"] + ".eid"))
        if not (enrollment_id and _ENROLLMENT.fullmatch(enrollment_id)):
            raise SyncError(
                "fiscl doesn't know this connection's Teller enrollment id yet (it is learned on a successful sync "
                "or when connecting). Forget it with `fiscl connect forget teller --institution NAME` and connect "
                "again; your imported data is kept."
            )
        save = lambda data: save_teller_reconnect(conn, row, data, revoke)  # noqa: E731
    else:
        save = lambda data: save_teller_enrollment(conn, data, revoke)  # noqa: E731
    server = make_server(
        secrets.token_urlsafe(32),
        lambda nonce: (teller_page(app_id, env, nonce, enrollment_id), teller_csp(nonce)),
        save,
    )
    run_flow(server, f"Teller {env}", open_browser, timeout, announce)
    return "teller"


# --- Plaid ----------------------------------------------------------------------------------


def plaid_csp(nonce: str) -> str:
    hosts = " ".join(PLAID_LINK_HOSTS)
    return "; ".join(
        [
            "default-src 'none'",
            f"script-src 'nonce-{nonce}' {hosts}",
            f"style-src 'unsafe-inline' {hosts}",  # Link injects its own styles
            f"frame-src {hosts}",
            f"connect-src 'self' {hosts}",
            f"img-src data: {hosts}",
            f"font-src {hosts}",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]
    )


def plaid_page(link_token: str, nonce: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>fiscl: connect a bank</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem">
<h1>Connect a bank to fiscl</h1>
<p>Read-only: fiscl asks Plaid for transactions and balances only. The access token is stored in your
OS keychain on this machine.</p>
<button id="go" type="button">Connect your bank</button>
<p id="status" role="status"></p>
<script src="{PLAID_LINK_SCRIPT}"></script>
<script nonce="{nonce}">
(function () {{
  var status = document.getElementById("status");
  function say(t) {{ status.textContent = t; }}
  var handler;
  try {{
    handler = Plaid.create({{
      token: {_js(link_token)},
      onSuccess: function (publicToken, metadata) {{
        var inst = (metadata && metadata.institution) || {{}};
        var body = {{ public_token: publicToken, institution: {{ name: inst.name }} }};
        fetch(location.pathname, {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
                                   body: JSON.stringify(body) }})
          .then(function (r) {{ return r.json().then(function (j) {{ return {{ status: r.status, j: j }}; }}); }})
          .then(function (x) {{
            say(x.status === 409
              ? "WARNING: fiscl had already received a result for this link. Do not rely on it: in the terminal run " +
                "`fiscl connect forget <provider>` and connect again."
              : x.j.ok ? "Connected. You can close this tab and go back to the terminal."
                       : "Not saved: " + x.j.error);
          }})
          .catch(function () {{ say("Could not reach fiscl on this machine."); }});
      }},
      onExit: function () {{ say("Closed without connecting."); }}
    }});
  }} catch (err) {{
    say("Plaid Link didn't load. Check your connection and the terminal.");
  }}
  document.getElementById("go").addEventListener("click", function () {{ if (handler) handler.open(); }});
}})();
</script></body></html>
"""


def parse_plaid_result(data: dict) -> tuple[str, str]:
    """Validate what the page posted. Returns (public_token, institution)."""
    token = data.get("public_token")
    if not isinstance(token, str) or not _PUBLIC_TOKEN.fullmatch(token):
        raise SyncError("Plaid's result was missing a public token")
    inst = data.get("institution")
    name = inst.get("name") if isinstance(inst, dict) else None
    return token, clean_text(name if isinstance(name, str) else "", 80)


def save_plaid_item(conn: sqlite3.Connection, client: plaid.PlaidClient, data: dict) -> str:
    """Exchange the public token, store the access token in the keychain, then record the connection (rolling the
    keychain entry back if the row fails). The access token never touches the DB."""
    public_token, institution = parse_plaid_result(data)
    access, item_id = plaid.exchange_public_token(client, public_token)
    ref = connections.item_ref_hash("plaid", item_id)
    _store_then_record(
        conn, "plaid", ref, access, institution, lambda: client.call("/item/remove", {"access_token": access})
    )
    return institution


def save_plaid_reconnect(conn: sqlite3.Connection, client: plaid.PlaidClient, row: sqlite3.Row, data: dict) -> str:
    """Link update mode finished. The Item and its access token are unchanged (Plaid just refreshed the login), so
    there is nothing to exchange or store: confirm the Item answers again, then mark the connection ok."""
    parse_plaid_result(data)  # same shape check as a new link; the public token itself is not used
    ref = row["item_ref_hash"]
    token = base.secret_get(base.secret_name("plaid", ref))
    if not token:
        raise SyncError("this connection has no stored access token; forget it and connect again")
    try:
        client.call("/accounts/get", {"access_token": token})
    except SyncError as exc:
        raise SyncError("Plaid still reports this connection needs re-authorization; try `--reconnect` again") from exc
    connections.set_status(conn, "plaid", ref, "ok")
    conn.commit()
    return row["institution"] or ""


def connect_plaid(
    cfg: Config,
    conn: sqlite3.Connection,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = TIMEOUT_SECONDS,
    announce: Callable[[str], None] = lambda msg: None,
    opener=None,
    reconnect: bool = False,
    institution: str | None = None,
) -> str:
    """Run the whole flow. The Plaid keys must already be in the keychain; the CLI collects them first.
    The link token is created here (server side) and is the only thing the page receives. With `reconnect`, Link
    runs in update mode for the one connection that needs re-authorization, so no new Item is created."""
    base.keyring_backend()  # no keychain, no connection
    client_id, secret = plaid.load_app_credentials()
    client = plaid.PlaidClient(cfg.plaid_environment, client_id, secret, opener)
    if reconnect:
        row = _pick_reauth(conn, "plaid", institution)
        access = base.secret_get(base.secret_name("plaid", row["item_ref_hash"]))
        if not access:
            raise SyncError("this connection has no stored access token; forget it and connect again")
        link_token = plaid.create_link_token(client, access_token=access)
        save = lambda data: save_plaid_reconnect(conn, client, row, data)  # noqa: E731
    else:
        link_token = plaid.create_link_token(client)
        save = lambda data: save_plaid_item(conn, client, data)  # noqa: E731
    server = make_server(
        secrets.token_urlsafe(32),
        lambda nonce: (plaid_page(link_token, nonce), plaid_csp(nonce)),
        save,
    )
    run_flow(server, f"Plaid {cfg.plaid_environment}", open_browser, timeout, announce)
    return "plaid"


# --- QuickBooks Online (OAuth redirect) ----------------------------------------------------------------
# Intuit redirects the browser to a registered URI, so this flow cannot use a random port or a secret URL path:
# a one-shot server on the fixed `redirect_port` answers GET /qbo/callback exactly once. What authenticates the
# answer is the random OAuth `state` (compared in constant time) plus the client secret used in the code exchange.

CALLBACK_PATH = "/qbo/callback"
_CALLBACK_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
_PAGE_OK = b"<!doctype html><meta charset=utf-8><title>fiscl</title><p>QuickBooks is connected. You can close this tab."
_PAGE_FAILED = b"<!doctype html><meta charset=utf-8><title>fiscl</title><p>Nothing was saved. See the terminal."
_CODE = re.compile(r"[A-Za-z0-9\-_.~]{1,512}")


def _reserve_ipv6_loopback(port: int) -> socket.socket | None:
    """The redirect URI says `localhost`, which a browser may resolve to ::1 as well as 127.0.0.1, but the server only
    binds 127.0.0.1. Bind (never listen on) ::1 on the same port so no other local program can sit there and receive
    the authorization code; connections to a bound, non-listening socket are refused and the browser falls back to
    127.0.0.1. No SO_REUSEADDR/SO_REUSEPORT: the reservation must fail if anything else holds the port. Hosts without
    an IPv6 loopback skip it."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)  # only ::1; 127.0.0.1 stays ours
        sock.bind(("::1", port))
    except OSError as exc:
        sock.close()
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            raise SyncError(
                f"another program is listening on localhost port {port}; close it or change redirect_port "
                "under [connectors.qbo]"
            ) from exc
        return None  # EADDRNOTAVAIL and friends: no IPv6 loopback on this machine
    return sock


class OAuthCallbackServer(HTTPServer):
    # The port is fixed, so a second `fiscl connect qbo` a minute after the first would hit TIME_WAIT. SO_REUSEADDR is
    # on for the 127.0.0.1 listener on POSIX, where it only skips TIME_WAIT and never lets a second socket share a
    # port that has a live listener; on Windows it would allow sharing, so it stays off there. The ::1 reservation
    # never sets it.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int, state: str, on_callback: Callable[[str, str], object]):
        self._reserved: socket.socket | None = None
        try:
            super().__init__((BIND_HOST, port), _CallbackHandler)
        except OSError as exc:
            raise SyncError(
                f"port {port} is busy: free it, or change redirect_port under [connectors.qbo] "
                "(and the redirect URI in your Intuit app to match)"
            ) from exc
        try:
            self._reserved = _reserve_ipv6_loopback(self.server_port)
        except SyncError:
            super().server_close()
            raise
        self.state = state
        self.on_callback = on_callback  # (code, realm id) -> the saved company name
        self.consumed = False
        self.result: object = None
        self.error: str | None = None
        self.deadline: float | None = None
        self.timeout = 0.5

    def server_close(self) -> None:
        super().server_close()
        if self._reserved is not None:
            self._reserved.close()
            self._reserved = None


def make_oauth_server(port: int, state: str, on_callback: Callable[[str, str], object]) -> OAuthCallbackServer:
    """The server factory for the OAuth redirect. It can only ever bind to 127.0.0.1."""
    return OAuthCallbackServer(port, state, on_callback)


class _CallbackHandler(_Handler):
    server: OAuthCallbackServer  # type: ignore[assignment]

    def _page(self, code: int, body: bytes):
        self._send(code, body, "text/html; charset=utf-8", {"Content-Security-Policy": _CALLBACK_CSP})

    def do_GET(self):
        port = self.server.server_port
        # DNS-rebinding defence: the Host header must be exactly the registered loopback name and our port, and a
        # request that names some other Origin (a page elsewhere poking at localhost) is not ours to answer.
        if self.headers.get("Host") not in (f"localhost:{port}", f"{BIND_HOST}:{port}"):
            return self._send(404, b"not found")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://localhost:{port}", f"http://{BIND_HOST}:{port}"):
            return self._send(404, b"not found")
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            return self._send(404, b"not found")
        if self.server.consumed:
            return self._send(409, b"already used")
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)

        def one(key: str) -> str | None:
            values = query.get(key)
            return values[0] if values and len(values) == 1 else None

        # Only a request carrying our state may consume the one shot; anything else (a stray local request, a
        # squatter probing the port) gets a 400 and cannot end or fail the real flow.
        state = one("state")
        if state is None or not hmac.compare_digest(state.encode(), self.server.state.encode()):
            return self._page(400, _PAGE_FAILED)
        self.server.consumed = True  # exactly one request with the right state reaches the callback logic
        if one("error") is not None:
            self.server.error = "QuickBooks reported that access wasn't approved; nothing was saved"
            return self._page(400, _PAGE_FAILED)
        code, realm = one("code"), one("realmId")
        if not (code and _CODE.fullmatch(code) and realm):
            self.server.error = "QuickBooks' answer was missing its code or company; nothing was saved"
            return self._page(400, _PAGE_FAILED)
        try:
            self.server.result = self.server.on_callback(code, realm)
        except SyncError as exc:
            self.server.error = str(exc)
            return self._page(400, _PAGE_FAILED)
        except Exception:  # a bug or a full disk must not look like success
            self.server.error = "fiscl couldn't save the connection; see the terminal"
            return self._page(500, _PAGE_FAILED)
        self._page(200, _PAGE_OK)

    do_POST = _Handler._nope


def best_effort(action: Callable[[], object]) -> None:
    try:
        action()
    except Exception:  # noqa: S110 - the original failure is the story
        pass


def save_qbo_connection(
    conn: sqlite3.Connection,
    http: qbo.Http,
    client_id: str,
    secret: str,
    code: str,
    realm_id: str,
    redirect: str,
    reconnect_row: sqlite3.Row | None = None,
) -> str:
    """Exchange the code, prove the grant works by reading the company name, then store the refresh token and the
    company id in the keychain and record the connection. On any failure after tokens were issued the grant is
    revoked (a new connection) or the keychain put back as it was, so nothing live is left untracked."""
    qbo.check_realm(realm_id)
    access, refresh = qbo.exchange_code(http, client_id, secret, code, redirect)

    def revoke() -> None:
        qbo.revoke_token(http, client_id, secret, refresh)

    ref = connections.item_ref_hash("qbo", realm_id)
    try:
        if reconnect_row is not None and reconnect_row["item_ref_hash"] != ref:
            raise SyncError("QuickBooks returned a different company than the one being reconnected; nothing changed")
        company = qbo.Client(realm_id, qbo.Session(ref, client_id, secret, http, access), http).company_name()
    except Exception:
        best_effort(revoke)  # a grant fiscl won't keep must not be left live
        raise
    realm_name = qbo.realm_secret_name(ref)
    previous_realm = base.secret_get(realm_name)
    try:
        base.secret_set(realm_name, realm_id)
        _store_then_record(conn, "qbo", ref, refresh, company, revoke)
    except Exception:
        best_effort(
            (lambda: base.secret_set(realm_name, previous_realm))
            if previous_realm is not None
            else (lambda: base.secret_delete(realm_name))
        )
        raise
    return company


def connect_qbo(
    cfg: Config,
    conn: sqlite3.Connection,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = TIMEOUT_SECONDS,
    announce: Callable[[str], None] = lambda msg: None,
    opener=None,
    reconnect: bool = False,
    institution: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Run the whole flow. The Intuit app keys must already be in the keychain; the CLI collects them first.
    Sandbox only: production is refused before anything is opened. With `reconnect`, re-authorize the one
    connection that needs it and refuse a different company."""
    base.keyring_backend()  # no keychain, no connection
    qbo.require_sandbox(cfg)
    client_id, secret = qbo.load_app_credentials()
    row = _pick_reauth(conn, "qbo", institution) if reconnect else None
    http = qbo.Http(opener, sleep)
    redirect = qbo.redirect_uri(cfg.qbo_redirect_port)
    state = secrets.token_urlsafe(32)
    server = make_oauth_server(
        cfg.qbo_redirect_port,
        state,
        lambda code, realm: save_qbo_connection(conn, http, client_id, secret, code, realm, redirect, row),
    )
    url = qbo.authorize_url(client_id, redirect, state)
    try:
        announce("Opening your browser to connect QuickBooks (sandbox). Waiting up to 10 minutes...")
        if not open_browser(url):
            announce(f"Open this link in your browser: {url}")
    except BaseException:
        server.server_close()
        raise
    serve_once(server, timeout)
    if isinstance(server.result, str) and server.result:
        announce(f"Saved a connection to the QuickBooks company: {server.result}.")
    return "qbo"
