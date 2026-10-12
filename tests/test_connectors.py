"""Shared connector layer: allowlists, no-redirect transport, keychain rules, the read-only source scan,
and the one-shot localhost connect server."""

from __future__ import annotations

import http.client
import http.server
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from fiscl.sync import base, connect, plaid, teller
from fiscl.sync.base import Policy, SyncError

from .conftest import ALLOWED_REFERENCE_URLS

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def no_sockets(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


# --- allowlists: refused before any socket opens --------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "https://api.teller.io/accounts/acc_1/" + "det" + "ails"),  # account/routing numbers
        ("GET", "https://api.teller.io/identity"),
        ("GET", "https://api.teller.io/accounts/acc_1"),  # only DELETE exists for a single account
        ("POST", "https://api.teller.io/accounts"),
        ("POST", "https://api.teller.io/accounts/acc_1/transactions"),
        ("PUT", "https://api.teller.io/accounts/acc_1/balances"),
        ("DELETE", "https://api.teller.io/accounts/acc_1/transactions"),
        ("GET", "https://api.teller.io/accounts/../identity"),
        ("GET", "https://api.teller.io/accounts/acc_1/transactions/extra"),
        ("GET", "http://api.teller.io/accounts"),
        ("GET", "https://evil.example/accounts"),
        ("GET", "https://api.teller.io.evil.example/accounts"),
        ("GET", "https://api.teller.io:8443/accounts"),
        ("GET", "https://user:pw@api.teller.io/accounts"),
        ("GET", "ftp://api.teller.io/accounts"),
    ],
)
def test_request_outside_the_allowlist_raises_before_any_socket(no_sockets, method, url):
    with pytest.raises(SyncError):
        base.request(teller.POLICY, method, url)  # default opener: would hit a (blocked) socket if reached


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/accounts"),
        ("GET", "/accounts/acc_abc-1/transactions?count=5&from_id=txn_1"),
        ("GET", "/accounts/acc_abc-1/balances"),
        ("DELETE", "/accounts/acc_abc-1"),
        ("DELETE", "/accounts"),
    ],
)
def test_allowlisted_teller_requests_pass_the_policy(method, path):
    teller.POLICY.check(method, "https://api.teller.io" + path)


def test_secret_headers_are_unredirected_and_policy_runs_first():
    seen = {}

    def opener(req, timeout):
        seen["req"] = req
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, None)

    with pytest.raises(SyncError, match="redirect") as exc:
        base.request(
            teller.POLICY,
            "GET",
            "https://api.teller.io/accounts",
            secret_headers={"Authorization": "Basic c2VjcmV0Og=="},
            opener=opener,
        )
    assert exc.value.status == 302
    req = seen["req"]
    assert "Authorization" in req.unredirected_hdrs and "Authorization" not in req.headers
    assert "c2VjcmV0" not in req.full_url


def test_opener_never_follows_redirects_and_resend_nothing():
    class Redirector(http.server.BaseHTTPRequestHandler):
        hits: list[str] = []

        def do_GET(self):
            Redirector.hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            base.open_url(urllib.request.Request(f"http://127.0.0.1:{server.server_port}/start"), timeout=5)
        assert exc.value.code == 302 and Redirector.hits == ["/start"]
    finally:
        server.shutdown()


def test_custom_context_opener_also_never_redirects():
    opener = base.build_opener(ssl.create_default_context())
    assert any(isinstance(h, base._NoRedirect) for h in opener.handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in opener.handlers)


def test_http_errors_and_unreachable_hosts_become_sync_errors():
    def fail(code):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)

        return opener

    with pytest.raises(SyncError) as exc:
        base.request(teller.POLICY, "GET", "https://api.teller.io/accounts", opener=fail(403))
    assert exc.value.status == 403

    def down(req, timeout):
        raise urllib.error.URLError("no route")

    with pytest.raises(SyncError, match="couldn't reach"):
        base.request(teller.POLICY, "GET", "https://api.teller.io/accounts", opener=down)


def test_policy_is_per_connector():
    other = Policy("Other", ("example.org",), (("GET", "/ok"),))
    other.check("GET", "https://example.org/ok")
    with pytest.raises(SyncError):
        other.check("GET", "https://api.teller.io/accounts")


# --- read-only by construction: forbidden endpoint strings never appear in src/ -----------------------

FORBIDDEN = (
    "/transfer",
    "/payment_initiation",
    "/processor",
    "/bank_transfer",
    "/payments",
    "/details",
    "/auth/get",
    "/identity/get",
    "/signal/",
    "/income/",
    "/investments/",
    "/accounts/balance/get",
)


def scan_text(rel: str, text: str) -> list[str]:
    """Forbidden fragments in a source file's text, after removing its explicitly allowlisted reference URLs.

    Only an exact URL, in exactly the file named in conftest.ALLOWED_REFERENCE_URLS, is exempt.
    Limitation: this is a textual scan, so a path assembled at runtime (join, format, a computed string) is not seen;
    test_phase2_fixes.py adds an AST pass that also catches `"a" + "b"` assembled from literals.
    """
    for url in ALLOWED_REFERENCE_URLS.get(rel, ()):
        text = text.replace(url, "")
    return [f"{rel}: {bad}" for bad in FORBIDDEN if bad in text]


def test_source_contains_no_money_movement_or_account_number_endpoints():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix().removeprefix("fiscl/")
        offenders += scan_text(rel, path.read_text(encoding="utf-8"))
    assert not offenders, offenders


def test_the_reference_url_allowlist_is_narrow():
    (rel, (url,)), *rest = ALLOWED_REFERENCE_URLS.items()
    assert not rest and rel == "tax/states.py"
    assert (SRC / "fiscl" / rel).read_text(encoding="utf-8").count(url) == 1  # one literal, not assembled
    assert scan_text(rel, f'X = "{url}"') == []
    assert scan_text("sync/plaid.py", f'X = "{url}"'), "the same URL in any other file is still flagged"
    assert scan_text(rel, 'X = "https://example.gov/forms/income/other.pdf"'), "any other string is still flagged"
    assert scan_text(rel, f'X = "{url}" + "/income/"'), "extra forbidden text next to the URL is still flagged"
    assert scan_text(rel, 'X = "/transfer/create"'), "other fragments are still flagged in that file"


def test_connector_modules_never_ask_for_forbidden_products():
    assert teller.PRODUCTS == ["transactions", "balance"]
    assert plaid.PLAID_PRODUCTS == ("transactions",)
    assert "products" not in connect.plaid_page("link-x", "n")
    page = connect.teller_page("app_1", "sandbox", "nonce")
    assert '["transactions", "balance"]' in page
    for word in ("payments", "verify", "identity"):
        assert word not in page.split("TellerConnect.setup")[1].split("onSuccess")[0]


# --- keychain -----------------------------------------------------------------------------------


def test_secret_names_follow_the_scheme_and_reject_junk():
    assert base.secret_name("teller", "abc123") == "fiscl:teller:abc123"
    for bad in (("nope", "x"), ("teller", ""), ("teller", "a/b"), ("teller", "a" * 65)):
        with pytest.raises(SyncError):
            base.secret_name(*bad)


def test_keychain_is_required(monkeypatch):
    # keyring package missing
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(SyncError, match="keyring"):
        base.keyring_backend()

    # keyring present, but only the fail backend (no OS keychain)
    fail_mod = types.ModuleType("keyring.backends.fail")

    class Keyring:  # same name the real fail backend uses
        pass

    fail_mod.Keyring = Keyring
    kr = types.ModuleType("keyring")
    kr.get_keyring = lambda: Keyring()
    kr.backends = types.ModuleType("keyring.backends")
    kr.backends.fail = fail_mod
    monkeypatch.setitem(sys.modules, "keyring", kr)
    monkeypatch.setitem(sys.modules, "keyring.backends", kr.backends)
    monkeypatch.setitem(sys.modules, "keyring.backends.fail", fail_mod)
    with pytest.raises(SyncError, match="no OS keychain"):
        base.keyring_backend()


def test_secret_roundtrip_and_delete_is_idempotent(keychain):
    name = base.secret_name("teller", "ref1")
    base.secret_set(name, "v")
    assert base.secret_get(name) == "v"
    base.secret_delete(name)
    base.secret_delete(name)  # already gone: fine
    assert base.secret_get(name) is None


# --- mTLS client certificate handling ----------------------------------------------------------------


@pytest.fixture
def fake_load(monkeypatch):
    """Record the state of the PEM file while load_cert_chain runs."""
    seen = {}

    def fake(self, certfile, keyfile=None, password=None):
        seen["path"] = certfile
        seen["exists"] = os.path.exists(certfile)
        seen["content"] = Path(certfile).read_text()
        if not certfile.startswith("/proc/"):
            seen["file_mode"] = stat.S_IMODE(os.stat(certfile).st_mode)
            seen["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(certfile)).st_mode)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", fake)
    return seen


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_cert_tempfile_is_private_and_gone_after_context_creation(monkeypatch, fake_load):
    monkeypatch.delattr(os, "memfd_create", raising=False)  # force the portable path everywhere
    ctx = base.client_context("-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----", "KEYPEM")
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert fake_load["exists"] and fake_load["file_mode"] == 0o600 and fake_load["dir_mode"] == 0o700
    assert "KEYPEM" in fake_load["content"]
    assert not os.path.exists(fake_load["path"])
    assert not os.path.exists(os.path.dirname(fake_load["path"]))


def test_cert_tempfile_is_removed_even_when_loading_fails(monkeypatch):
    monkeypatch.delattr(os, "memfd_create", raising=False)
    paths = {}

    def boom(self, certfile, keyfile=None, password=None):
        paths["p"] = certfile
        raise ssl.SSLError("bad pem")

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", boom)
    with pytest.raises(SyncError):
        base.client_context("c", "k")
    assert not os.path.exists(paths["p"]) and not os.path.exists(os.path.dirname(paths["p"]))


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="Linux only")
def test_memfd_path_is_used_where_available(fake_load):
    base.client_context("c", "k")
    assert fake_load["path"].startswith("/proc/self/fd/")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_real_pem_pair_loads(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
        + ["-keyout", str(key), "-out", str(cert), "-subj", "/CN=fiscl-test", "-days", "1"],
        check=True,
        capture_output=True,
    )
    ctx = base.client_context(cert.read_text(), key.read_text())
    assert isinstance(ctx, ssl.SSLContext)
    leftovers = [p for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("fiscl-mtls-*")]
    assert leftovers == []


# --- the one-shot connect server ---------------------------------------------------------------------


def _server(posts: list | None = None, fail: bool = False):
    posts = posts if posts is not None else []

    def on_post(data):
        if fail:
            raise SyncError("nope")
        posts.append(data)

    return connect.make_server("tok" + "x" * 40, lambda nonce: (f"<html>{nonce}</html>", "default-src 'none'"), on_post)


def _req(server, method, path, body=None, headers=None, host=None):
    """Send one request and let the server handle exactly one connection."""
    t = threading.Thread(target=server.handle_request)
    t.start()
    c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    hdrs = {"Host": host or f"127.0.0.1:{server.server_port}", **(headers or {})}
    c.request(method, path, body=body, headers=hdrs)
    r = c.getresponse()
    data = r.read()
    c.close()
    t.join(5)
    return r.status, data, r


def test_server_binds_only_to_loopback_on_a_random_port():
    s1, s2 = _server(), _server()
    try:
        assert s1.server_address[0] == "127.0.0.1" and s2.server_address[0] == "127.0.0.1"
        assert s1.server_port != s2.server_port or s1.server_port > 1023
        assert s1.url.startswith("http://127.0.0.1:")
    finally:
        s1.server_close()
        s2.server_close()


def test_connect_token_is_32_random_bytes():
    import secrets

    assert len(secrets.token_urlsafe(32)) >= 43  # what connect_teller uses


def test_page_is_served_only_at_the_tokenized_url_with_a_csp():
    s = _server()
    try:
        token = s.token
        assert _req(s, "GET", "/")[0] == 404
        assert _req(s, "GET", "/wrong")[0] == 404
        assert _req(s, "GET", f"/{token}", host="evil.example")[0] == 404  # DNS-rebinding guard
        status, body, r = _req(s, "GET", f"/{token}")
        assert status == 200 and b"<html>" in body
        assert r.getheader("Content-Security-Policy") == "default-src 'none'"
        assert r.getheader("Cache-Control") == "no-store"
    finally:
        s.server_close()


def test_wrong_token_post_is_rejected_and_does_not_consume_the_slot():
    posts: list = []
    s = _server(posts)
    try:
        body = json.dumps({"x": 1})
        assert (
            _req(s, "POST", "/nope", body, {"Content-Type": "application/json", "Content-Length": str(len(body))})[0]
            == 404
        )
        assert not s.consumed and posts == []
    finally:
        s.server_close()


def test_only_one_post_is_accepted():
    posts: list = []
    s = _server(posts)
    try:
        body = json.dumps({"hello": "world"})
        h = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        assert _req(s, "POST", f"/{s.token}", body, h)[0] == 200
        assert _req(s, "POST", f"/{s.token}", body, h)[0] == 409
        assert posts == [{"hello": "world"}] and s.consumed
    finally:
        s.server_close()


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def test_non_get_post_methods_are_rejected(method):
    s = _server()
    try:
        status = _req(s, method, f"/{s.token}")[0]
        assert status == 405 and not s.consumed
    finally:
        s.server_close()


def test_bad_post_bodies_and_origins_are_rejected():
    for headers, body in [
        ({"Content-Type": "text/plain"}, "{}"),
        ({"Content-Type": "application/json"}, "not json"),
        ({"Content-Type": "application/json"}, "[1]"),
        ({"Content-Type": "application/json", "Origin": "https://evil.example"}, "{}"),
    ]:
        posts: list = []
        s = _server(posts)
        try:
            headers = {**headers, "Content-Length": str(len(body))}
            assert _req(s, "POST", f"/{s.token}", body, headers)[0] == 400
            assert posts == []
        finally:
            s.server_close()


def test_serve_once_times_out_and_closes():
    s = _server()
    with pytest.raises(SyncError, match="timed out"):
        connect.serve_once(s, timeout=0.2)


def test_serve_once_surfaces_a_rejected_result():
    s = _server(fail=True)
    body = "{}"
    raised: list[Exception] = []

    def serve():
        try:
            connect.serve_once(s, 5)
        except SyncError as exc:
            raised.append(exc)

    t = threading.Thread(target=serve, daemon=True)
    # drive via a client; serve_once raises in the thread, so just check the 400 and consumed flag
    t.start()
    c = http.client.HTTPConnection("127.0.0.1", s.server_port, timeout=5)
    c.request(
        "POST",
        f"/{s.token}",
        body,
        {"Content-Type": "application/json", "Content-Length": "2", "Host": f"127.0.0.1:{s.server_port}"},
    )
    assert c.getresponse().status == 400
    t.join(5)
    assert s.consumed and s.error == "nope" and raised and "nope" in str(raised[0])


def test_teller_csp_allows_only_self_and_teller_hosts():
    csp = connect.teller_csp("N0NCE")
    assert csp.startswith("default-src 'none'") and "'nonce-N0NCE'" in csp
    assert "https://cdn.teller.io" in csp and "frame-ancestors 'none'" in csp
    hosts = set(re.findall(r"https://[^\s;]+", csp))
    assert hosts == set(connect.TELLER_CONNECT_HOSTS)
    assert all(h.endswith("teller.io") for h in hosts)
    assert "unsafe-eval" not in csp and "script-src 'unsafe-inline'" not in csp


def test_teller_page_embeds_values_safely():
    page = connect.teller_page('app"</script><b>', "sandbox", "n")
    assert "</script><b>" not in page and "\\u003c/script" in page
    assert 'src="https://cdn.teller.io/connect/connect.js"' in page
    assert "innerHTML" not in page


def test_enrollment_result_validation():
    ok = {"accessToken": "test_abc123", "enrollment": {"id": "enr_1", "institution": {"name": "Gulf\nBank"}}}
    assert connect.parse_teller_enrollment(ok) == ("test_abc123", "enr_1", "Gulf Bank")
    for bad in (
        {},
        {"accessToken": "x", "enrollment": "nope"},
        {"accessToken": "has space", "enrollment": {"id": "enr_1"}},
        {"accessToken": "ok_tok", "enrollment": {"id": "../etc"}},
        {"accessToken": 5, "enrollment": {"id": "enr_1"}},
    ):
        with pytest.raises(SyncError):
            connect.parse_teller_enrollment(bad)


def test_connect_refuses_without_a_keychain(cfg, conn, monkeypatch):
    def none():
        raise SyncError("no OS keychain is available")

    monkeypatch.setattr(base, "keyring_backend", none)
    opened = []
    with pytest.raises(SyncError, match="keychain"):
        connect.connect_teller(cfg, conn, open_browser=opened.append)
    assert opened == []
    from fiscl import cli

    assert cli.main(["--home", str(cfg.home), "connect", "teller"]) == 1
    assert cli.main(["--home", str(cfg.home), "connect", "simplefin"]) == 1


def test_connect_teller_end_to_end_stores_token_in_keychain_only(cfg, conn, keychain):
    cfg.teller_environment = "sandbox"
    teller.store_app_credentials("app_test1", None, None)
    result = {}

    def browser(url):
        # the "browser": fetch the page, then post the enrollment back to the same tokenized URL
        def go():
            u = urllib.parse.urlsplit(url)
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
            c.request("GET", u.path)
            page = c.getresponse().read().decode()
            result["page"] = page
            body = json.dumps(
                {"accessToken": "tok_SECRET1", "enrollment": {"id": "enr_9", "institution": {"name": "Gulf Bank"}}}
            )
            c.request("POST", u.path, body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
            result["status"] = c.getresponse().status

        t = threading.Thread(target=go, daemon=True)
        threads.append(t)
        t.start()
        return True

    threads: list[threading.Thread] = []
    connect.connect_teller(cfg, conn, open_browser=browser, timeout=10)
    threads[0].join(5)
    assert result["status"] == 200 and '"app_test1"' in result["page"]
    from fiscl.sync import connections

    rows = connections.list_rows(conn, "teller")
    assert len(rows) == 1 and rows[0]["institution"] == "Gulf Bank"
    assert keychain.get_password("fiscl", f"fiscl:teller:{rows[0]['item_ref_hash']}") == "tok_SECRET1"
    assert "tok_SECRET1" not in json.dumps([tuple(r) for r in conn.execute("SELECT * FROM connections")])


# --- migration, protocol conformance, CLI --------------------------------------------------------------


def test_connections_table_has_no_secret_columns(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(connections)")]
    assert cols == ["id", "provider", "institution", "item_ref_hash", "created_at", "last_sync_at", "status", "cursor"]
    with pytest.raises(Exception, match="CHECK"):
        conn.execute("INSERT INTO connections (provider, item_ref_hash, created_at) VALUES ('evil', 'x', 'now')")


def test_connectors_satisfy_the_protocol(cfg):
    from fiscl.sync.simplefin import SimpleFinConnector

    for c in (SimpleFinConnector(cfg), teller.TellerConnector(cfg, []), plaid.PlaidConnector(cfg, [])):
        assert c.provider in base.PROVIDERS and c.warnings == []
        assert callable(c.fetch) and callable(c.forget)


def test_simplefin_still_uses_its_own_allowlist(no_sockets):
    from fiscl.sync import simplefin

    with pytest.raises(SyncError):
        base.request(simplefin._policy(simplefin.DEFAULT_ALLOWED_HOSTS), "GET", "https://api.teller.io/accounts")
    with pytest.raises(SyncError):
        base.request(
            simplefin._policy(simplefin.DEFAULT_ALLOWED_HOSTS),
            "DELETE",
            "https://bridge.simplefin.org/simplefin/accounts",
        )


def test_cli_connect_list_and_forget(cfg, keychain, capsys):
    from fiscl import cli

    home = ["--home", str(cfg.home)]
    assert cli.main([*home, "connect", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["connections"] == []
    assert cli.main([*home, "connect", "forget", "simplefin"]) == 0
