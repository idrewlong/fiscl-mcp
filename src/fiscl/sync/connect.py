"""Human-only connection flow: a one-shot localhost server that hosts the provider's Connect/Link widget.

This is only ever started from the CLI (`fiscl connect ...`), never from an MCP tool, so a model can't open a
bank connection. The server binds to 127.0.0.1 on a random port, serves one page under a random 32-byte URL
token, accepts exactly one POST with the enrollment result, stores the access token in the keychain, records
the connection, and shuts down. It gives up after 10 minutes.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..config import Config
from ..guard import clean_text
from . import base, connections, plaid, teller
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
        self.error: str | None = None
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
        self.server.consumed = True  # exactly one POST, whatever it contains
        origin = self.headers.get("Origin")
        try:
            if origin is not None and origin != f"http://{BIND_HOST}:{self.server.server_port}":
                raise ValueError("unexpected origin")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise ValueError("expected JSON")
            length = int(self.headers.get("Content-Length") or "-1")
            if not 0 < length <= MAX_BODY:
                raise ValueError("bad length")
            data = json.loads(self.rfile.read(length).decode())
            if not isinstance(data, dict):
                raise ValueError("expected an object")
            self.server.on_post(data)
        except (ValueError, SyncError) as exc:
            self.server.error = str(exc) if isinstance(exc, SyncError) else "the result wasn't valid"
            return self._send(400, json.dumps({"ok": False, "error": self.server.error}).encode(), "application/json")
        except Exception:  # a bug or a full disk must not look like success
            self.server.error = "fiscl couldn't save the connection; see the terminal"
            return self._send(500, json.dumps({"ok": False, "error": self.server.error}).encode(), "application/json")
        self._send(200, b'{"ok": true}', "application/json")

    def _nope(self):
        self._send(405, b"method not allowed", extra={"Allow": "GET, POST"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _nope


def serve_once(server: OneShotServer, timeout: float = TIMEOUT_SECONDS) -> None:
    """Handle requests until the single POST has arrived or the deadline passes."""
    deadline = time.monotonic() + timeout
    try:
        while not server.consumed and time.monotonic() < deadline:
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


def teller_page(app_id: str, environment: str, nonce: str) -> str:
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
      environment: {_js(environment)},
      products: {_js(teller.PRODUCTS)},
      onSuccess: function (e) {{
        var en = (e && e.enrollment) || {{}};
        var body = {{ accessToken: e && e.accessToken,
                     enrollment: {{ id: en.id, institution: {{ name: en.institution && en.institution.name }} }} }};
        fetch(location.pathname, {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
                                   body: JSON.stringify(body) }})
          .then(function (r) {{ return r.json(); }})
          .then(function (j) {{ say(j.ok ? "Connected. You can close this tab and go back to the terminal."
                                          : "Not saved: " + j.error); }})
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


def save_teller_enrollment(conn: sqlite3.Connection, data: dict) -> str:
    """Token to keychain first, then the connection row; roll the keychain entry back if the row fails."""
    token, eid, institution = parse_teller_enrollment(data)
    ref = connections.item_ref_hash("teller", eid)
    teller.store_access_token(ref, token)
    try:
        connections.add_connection(conn, "teller", institution, ref)
        conn.commit()
    except Exception:
        base.secret_delete(base.secret_name("teller", ref))
        raise
    return institution


def connect_teller(
    cfg: Config,
    conn: sqlite3.Connection,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = TIMEOUT_SECONDS,
    announce: Callable[[str], None] = lambda msg: None,
) -> str:
    """Run the whole flow. Credentials (application id, and the certificate outside sandbox) must already be
    in the keychain; the CLI collects them first."""
    base.keyring_backend()  # no keychain, no connection
    env = cfg.teller_environment
    app_id = base.secret_get(base.secret_name("teller", "app_id")) or ""
    if not _APP_ID.fullmatch(app_id):
        raise SyncError("no Teller application id is stored; run `fiscl connect teller` again")
    server = make_server(
        secrets.token_urlsafe(32),
        lambda nonce: (teller_page(app_id, env, nonce), teller_csp(nonce)),
        lambda data: save_teller_enrollment(conn, data),
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
          .then(function (r) {{ return r.json(); }})
          .then(function (j) {{ say(j.ok ? "Connected. You can close this tab and go back to the terminal."
                                          : "Not saved: " + j.error); }})
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
    plaid.store_access_token(ref, access)
    try:
        connections.add_connection(conn, "plaid", institution, ref)
        conn.commit()
    except Exception:
        base.secret_delete(base.secret_name("plaid", ref))
        raise
    return institution


def connect_plaid(
    cfg: Config,
    conn: sqlite3.Connection,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout: float = TIMEOUT_SECONDS,
    announce: Callable[[str], None] = lambda msg: None,
    opener=None,
) -> str:
    """Run the whole flow. The Plaid keys must already be in the keychain; the CLI collects them first.
    The link token is created here (server side) and is the only thing the page receives."""
    base.keyring_backend()  # no keychain, no connection
    client_id, secret = plaid.load_app_credentials()
    client = plaid.PlaidClient(cfg.plaid_environment, client_id, secret, opener)
    link_token = plaid.create_link_token(client)
    server = make_server(
        secrets.token_urlsafe(32),
        lambda nonce: (plaid_page(link_token, nonce), plaid_csp(nonce)),
        lambda data: save_plaid_item(conn, client, data),
    )
    run_flow(server, f"Plaid {cfg.plaid_environment}", open_browser, timeout, announce)
    return "plaid"
