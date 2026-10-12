"""Phase 5 HTTP transport: every row of the spec's threat table, plus read-only-by-default tools.

In-process ASGI tests cover each gate rejection. One real-socket test file section (marked `loopback`) opens
sockets on 127.0.0.1 only; it is the only test here that does.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import httpx2
import pytest

from fiscl import http_transport as ht
from fiscl.cli import main
from fiscl.config import load_config
from fiscl.demo import load_demo
from fiscl.server import build_server
from fiscl.sync import base

from .conftest import TODAY

PORT = 8787
JSON = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def token(keychain):
    return ht.TokenStore().ensure()[0]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(cfg, token, clock):
    return ht.build_app(
        build_server(cfg, read_only=True), ht.TokenStore().get, "127.0.0.1", PORT, limiter=ht.AuthLimiter(clock)
    )


def asgi_client(app, *, client=("127.0.0.1", 5555), port=PORT) -> httpx2.AsyncClient:
    transport = httpx2.ASGITransport(app=app, client=client)
    return httpx2.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{port}")


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# --- binding -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "localhost", "192.168.1.5", "", "127.0.0.2", "::ffff:127.0.0.1", "0:0:0:0:0:0:0:1", "*"]
)
def test_only_the_two_loopback_literals_are_accepted(host, monkeypatch):
    def no_socket(*a, **k):
        raise AssertionError("a socket was created")

    monkeypatch.setattr(socket, "socket", no_socket)
    with pytest.raises(ht.HttpConfigError):
        ht.bind_loopback(host, 8787)


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, -1, True, "8787"])
def test_port_must_be_unprivileged(port):
    with pytest.raises(ht.HttpConfigError):
        ht.validate_bind("127.0.0.1", port)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "localhost", "192.168.1.5"])
def test_cli_refuses_a_non_loopback_host_with_status_2_and_opens_no_socket(host, cfg, keychain, monkeypatch, capsys):
    def no_socket(*a, **k):
        raise AssertionError("a socket was created")

    monkeypatch.setattr(socket, "socket", no_socket)
    assert main(["--home", str(cfg.home), "serve", "--http", "--host", host, "--no-watch"]) == 2
    assert "127.0.0.1" in capsys.readouterr().err


def test_cli_refuses_without_an_os_keychain_and_opens_no_socket(cfg, monkeypatch, capsys):
    def no_keychain():
        raise base.SyncError(base.NO_KEYCHAIN)

    monkeypatch.setattr(base, "keyring_backend", no_keychain)

    def no_socket(*a, **k):
        raise AssertionError("a socket was created")

    monkeypatch.setattr(socket, "socket", no_socket)
    assert main(["--home", str(cfg.home), "serve", "--http", "--no-watch"]) == 2
    assert "keychain" in capsys.readouterr().err.lower()


def test_bound_socket_is_loopback_and_a_busy_port_is_a_clear_error_not_another_port():
    sock = ht.bind_loopback("127.0.0.1", 0, allow_ephemeral=True)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
        busy = sock.getsockname()[1]
        with pytest.raises(ht.HttpConfigError, match="never tries a different port"):
            ht.bind_loopback("127.0.0.1", busy)
    finally:
        sock.close()


def test_ipv6_loopback_bind_when_available():
    try:
        sock = ht.bind_loopback("::1", 0, allow_ephemeral=True)
    except ht.HttpConfigError:
        pytest.skip("no IPv6 loopback")
    try:
        assert sock.getsockname()[0] == "::1"
        assert ht.allowed_hosts("::1", 9) == ("[::1]:9",)
    finally:
        sock.close()


def test_loopback_address_check_handles_mapped_and_garbage():
    assert ht.is_loopback_address("127.0.0.1") and ht.is_loopback_address("::1")
    assert ht.is_loopback_address("::ffff:127.0.0.1")
    for bad in ("10.0.0.2", "0.0.0.0", "evil.example", "", None, "::ffff:10.0.0.2"):
        assert not ht.is_loopback_address(bad)


def test_allowed_hosts_are_port_specific():
    assert ht.allowed_hosts("127.0.0.1", 9001) == ("127.0.0.1:9001",)
    assert ht.allowed_hosts("::1", 9001) == ("[::1]:9001",)
    assert ht.client_url("127.0.0.1", 9001) == "http://127.0.0.1:9001/mcp"
    assert ht.client_url("::1", 9001) == "http://[::1]:9001/mcp"


# --- gate: auth, host, origin, methods ---------------------------------------------------------


async def test_missing_or_wrong_token_is_401_with_challenge_and_empty_body(app, token):
    async with asgi_client(app) as c:
        for headers in (
            {},
            bearer("wrong"),
            {"authorization": "Basic abc"},
            {"authorization": f"bearer{token}"},
            {"authorization": "Bearer "},
            {"authorization": f"Bearer {token} extra"},
        ):
            r = await c.post("/mcp", json=INIT, headers={**JSON, **headers})
            assert r.status_code == 401, headers
            assert r.headers["www-authenticate"] == "Bearer"
            assert r.content == b""


async def test_token_in_the_query_string_is_not_accepted(app, token):
    async with asgi_client(app) as c:
        r = await c.post(f"/mcp?token={token}&access_token={token}", json=INIT, headers=JSON)
        assert r.status_code == 401
        r = await c.post("/mcp", json=INIT, headers={**JSON, "cookie": f"token={token}"})
        assert r.status_code == 401


async def test_correct_token_reaches_the_sdk(app, token):
    async with app.inner.router.lifespan_context(app.inner), asgi_client(app) as c:
        r = await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token)})
        assert r.status_code == 200, r.text
        assert "fiscl" in r.text


@pytest.mark.parametrize("origin", ["http://evil.example", f"http://127.0.0.1:{PORT}", "null", ""])
async def test_any_origin_header_is_refused_even_with_a_valid_token(app, token, origin):
    async with asgi_client(app) as c:
        r = await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token), "origin": origin})
        assert r.status_code == 403


@pytest.mark.parametrize(
    "host", [f"evil.com:{PORT}", "127.0.0.1:9999", "127.0.0.1", "evil.example", f"127.0.0.1.evil.com:{PORT}", ""]
)
async def test_host_must_match_exactly_including_port(app, token, host):
    async with asgi_client(app) as c:
        r = await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token), "host": host})
        assert r.status_code == 421


async def test_localhost_is_refused_even_with_the_right_port_and_token(app, token):
    # SEC-1: `localhost` may resolve to the other loopback family; clients must use the literal address.
    async with asgi_client(app) as c:
        r = await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token), "host": f"localhost:{PORT}"})
        assert r.status_code == 421


async def test_non_loopback_peer_is_403_even_with_everything_else_right(app, token):
    async with asgi_client(app, client=("10.0.0.2", 5555)) as c:
        r = await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token)})
        assert r.status_code == 403
    scope_seen = []

    async def inner(scope, receive, send):
        scope_seen.append(scope)

    gate = ht.GateMiddleware(inner, token_source=lambda: token, hosts=("h",))
    sent = []

    async def send(m):
        sent.append(m)

    await gate({"type": "http", "client": None, "headers": [], "path": "/mcp", "method": "POST"}, None, send)
    assert sent[0]["status"] == 403 and not scope_seen


async def test_options_is_405_without_cors_and_other_paths_are_404(app, token):
    async with asgi_client(app) as c:
        r = await c.options("/mcp", headers=bearer(token))
        assert r.status_code == 405
        for method in ("PUT", "PATCH", "HEAD"):
            assert (await c.request(method, "/mcp", headers=bearer(token))).status_code == 405
        for path in ("/other", "/", "/mcp/", "/health", "/.well-known/oauth-protected-resource"):
            assert (await c.get(path, headers=bearer(token))).status_code == 404


async def test_non_json_post_is_415(app, token):
    async with asgi_client(app) as c:
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "text/json"):
            r = await c.post("/mcp", content=b"{}", headers={"content-type": ctype, **bearer(token)})
            assert r.status_code == 415, ctype
        r = await c.post("/mcp", content=b"{}", headers=bearer(token))
        assert r.status_code == 415


async def test_no_response_ever_carries_cors_or_server_headers(app, token):
    cases = [
        ("POST", "/mcp", {**JSON, **bearer(token), "origin": "http://evil.example"}),
        ("POST", "/mcp", {**JSON, "host": "evil.com:1"}),
        ("POST", "/mcp", JSON),
        ("POST", "/mcp", {"content-type": "text/plain", **bearer(token)}),
        ("OPTIONS", "/mcp", {"access-control-request-method": "POST", **bearer(token)}),
        ("GET", "/nope", bearer(token)),
        ("PUT", "/mcp", bearer(token)),
    ]
    async with asgi_client(app) as c:
        for method, path, headers in cases:
            r = await c.request(method, path, headers=headers)
            assert not [h for h in r.headers if h.lower().startswith("access-control-")], (method, path)
    async with asgi_client(app, client=("10.0.0.2", 1)) as c:
        r = await c.get("/mcp")
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]
    async with app.inner.router.lifespan_context(app.inner), asgi_client(app) as c:
        r = await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token)})
        assert r.status_code == 200
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]


# --- rate limit --------------------------------------------------------------------------------


async def test_21st_bad_token_in_a_minute_is_429_and_even_the_right_token_waits(app, token, clock):
    async with asgi_client(app) as c:
        for _ in range(20):
            assert (await c.post("/mcp", json=INIT, headers={**JSON, **bearer("bad")})).status_code == 401
            clock.t += 1
        r = await c.post("/mcp", json=INIT, headers={**JSON, **bearer("bad")})
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert (await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token)})).status_code == 429
        clock.t += 61
        assert (await c.post("/mcp", json=INIT, headers={**JSON, **bearer("bad")})).status_code == 401


async def test_slow_failures_outside_the_window_never_lock(app, clock):
    async with asgi_client(app) as c:
        for _ in range(40):
            assert (await c.post("/mcp", json=INIT, headers={**JSON, **bearer("bad")})).status_code == 401
            clock.t += 5  # 12 per minute


# --- token handling ----------------------------------------------------------------------------


def test_compare_is_constant_time_over_digests():
    assert ht.digests_match("abc", "abc")
    assert not ht.digests_match("abc", "abd")
    assert not ht.digests_match("abc", "abcd")
    assert not ht.digests_match("", "x")
    src = inspect.getsource(ht.digests_match)
    assert "hmac.compare_digest" in src and "sha256" in src


def test_no_equality_operator_is_ever_applied_to_a_token():
    tree = ast.parse(Path(ht.__file__).read_text(encoding="utf-8"))
    names = {"token", "presented", "expected", "bearer"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.Eq | ast.NotEq) for op in node.ops):
            for part in [node.left, *node.comparators]:
                ident = part.id if isinstance(part, ast.Name) else part.attr if isinstance(part, ast.Attribute) else ""
                assert ident not in names, f"line {node.lineno}"


def test_token_is_32_random_bytes_stored_only_in_the_keychain(keychain, cfg):
    tokens = ht.TokenStore()
    t1, created = tokens.ensure()
    t2, created2 = tokens.ensure()
    assert created and not created2 and t1 == t2
    assert len(t1) >= 43  # token_urlsafe(32)
    assert keychain.items == {(base.KEYRING_SERVICE, "fiscl:http:token"): t1}
    assert not any(t1 in p.read_text(errors="ignore") for p in cfg.home.rglob("*") if p.is_file())


def test_http_token_show_refuses_when_stderr_is_not_a_terminal(cfg, token, capsys):
    assert main(["--home", str(cfg.home), "http", "token", "show"]) == 1
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_http_token_show_prints_to_stderr_only_on_a_terminal(cfg, token, capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    assert main(["--home", str(cfg.home), "http", "token", "show"]) == 0
    captured = capsys.readouterr()
    assert token in captured.err and token not in captured.out


def test_http_token_create_prints_nothing_secret(cfg, keychain, capsys):
    assert main(["--home", str(cfg.home), "http", "token"]) == 0
    token = keychain.items[(base.KEYRING_SERVICE, "fiscl:http:token")]
    captured = capsys.readouterr()
    assert "keychain" in captured.err and token not in captured.out + captured.err


def test_http_token_without_a_keychain_is_a_clean_error(cfg, monkeypatch, capsys):
    def no_keychain():
        raise base.SyncError(base.NO_KEYCHAIN)

    monkeypatch.setattr(base, "keyring_backend", no_keychain)
    assert main(["--home", str(cfg.home), "http", "token"]) == 2
    assert "keychain" in capsys.readouterr().err.lower()


async def test_rotate_changes_the_keychain_value_and_the_old_token_dies_on_the_next_request(cfg, app, token, keychain):
    async with app.inner.router.lifespan_context(app.inner), asgi_client(app) as c:
        assert (await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token)})).status_code == 200
        assert main(["--home", str(cfg.home), "http", "token", "rotate"]) == 0
        new = keychain.items[(base.KEYRING_SERVICE, "fiscl:http:token")]
        assert new != token
        assert (await c.post("/mcp", json=INIT, headers={**JSON, **bearer(token)})).status_code == 401
        assert (await c.post("/mcp", json=INIT, headers={**JSON, **bearer(new)})).status_code == 200


def test_forget_removes_the_token(cfg, token, keychain):
    assert main(["--home", str(cfg.home), "http", "token", "forget"]) == 0
    assert keychain.items == {}


async def test_missing_token_in_the_keychain_fails_closed(cfg, keychain, clock):
    app = ht.build_app(build_server(cfg, read_only=True), ht.TokenStore().get, "127.0.0.1", PORT)
    async with asgi_client(app) as c:
        r = await c.post("/mcp", json=INIT, headers={**JSON, **bearer("anything")})
        assert r.status_code == 503


async def test_token_never_reaches_the_logs_or_audit_log(cfg, token, app):
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage() + " " + str(record.args))

    ht.make_server(app)  # applies the server-side logger levels (the test's own client logs the URL it requests)
    logging.getLogger("httpx2").setLevel(logging.CRITICAL)
    h = Grab(level=logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        async with app.inner.router.lifespan_context(app.inner), asgi_client(app) as c:
            for headers in (bearer(token), bearer("bad-secret-guess"), {}):
                await c.post("/mcp", json=INIT, headers={**JSON, **headers})
            await c.post("/mcp?token=" + token, json=INIT, headers=JSON)
    finally:
        root.removeHandler(h)
        root.setLevel(old)
    blob = "\n".join(records)
    assert token not in blob and "bad-secret-guess" not in blob and "authorization" not in blob.lower()
    for log_file in cfg.logs.glob("*"):
        text = log_file.read_text(errors="ignore")
        assert token not in text and "authorization" not in text.lower()


# --- read-only by default ----------------------------------------------------------------------


async def tool_names(server) -> set[str]:
    from mcp import Client

    async with Client(server) as c:
        return {t.name for t in (await c.list_tools()).tools}


async def test_read_only_server_lists_only_read_only_tools(cfg):
    from mcp import Client

    full = build_server(cfg)
    async with Client(full) as c:
        tools = (await c.list_tools()).tools
    read = {t.name for t in tools if t.annotations and t.annotations.read_only_hint is True}
    write = {t.name for t in tools} - read
    assert {"sync_accounts", "set_budget", "apply_categories", "set_fx_rate", "log_trip", "export_schedule_c"} <= write
    assert {"query_transactions", "show_chart", "list_accounts"} <= read

    assert await tool_names(build_server(cfg, read_only=True)) == read
    assert await tool_names(build_server(cfg, read_only=False)) == read | write


async def test_write_tools_are_refused_when_called_over_a_read_only_server(cfg, conn):
    from mcp import Client

    async with Client(build_server(cfg, read_only=True)) as c:
        for name, args in (
            ("set_budget", {"items": [{"category": "Dining", "monthly": 5}], "dry_run": False}),
            ("sync_accounts", {}),
            ("apply_categories", {"items": [], "dry_run": False}),
        ):
            result = await c.call_tool(name, args)
            assert result.is_error, name
    assert "[[budget]]\ncategory" not in (cfg.home / "budgets.toml").read_text()


async def test_read_only_server_says_so_in_its_instructions(cfg):
    from mcp import Client

    async with Client(build_server(cfg, read_only=True)) as c:
        assert "read-only" in (c.instructions or "")
    async with Client(build_server(cfg)) as c:
        assert "read-only" not in (c.instructions or "")


def test_allow_writes_config_is_a_strict_boolean(tmp_path):
    from fiscl.config import ConfigError

    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text('[http]\nallow_writes = "yes"\n')
    with pytest.raises(ConfigError):
        load_config(home)
    (home / "config.toml").write_text("[http]\nallow_writes = true\nport = 9100\n")
    c = load_config(home)
    assert c.http_allow_writes is True and c.http_port == 9100
    (home / "config.toml").write_text("[http]\nport = 80\n")
    with pytest.raises(ConfigError):
        load_config(home)
    (home / "config.toml").write_text("")
    c = load_config(home)
    assert c.http_allow_writes is False and c.http_port == 8787


@pytest.mark.parametrize("allow", [False, True])
async def test_cli_serve_http_wires_read_only_from_config(cfg, keychain, monkeypatch, allow):
    seen = {}

    def fake_serve(mcp, sock, token_source):
        seen["sock"] = sock.getsockname()
        seen["names"] = {t.name for t in mcp._tool_manager.list_tools()}
        sock.close()

    monkeypatch.setattr(ht, "serve", fake_serve)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    if allow:
        cfg.config_file.write_text("[http]\nallow_writes = true\n")
    rc = await asyncio.to_thread(main, ["--home", str(cfg.home), "serve", "--http", "--port", str(port), "--no-watch"])
    assert rc == 0 and seen["sock"] == ("127.0.0.1", port)
    assert ("set_budget" in seen["names"]) is allow
    assert ("sync_accounts" in seen["names"]) is allow
    assert "list_accounts" in seen["names"]


# --- real loopback socket ----------------------------------------------------------------------


class LiveServer:
    def __init__(self, cfg, tokens):
        self.sock = ht.bind_loopback("127.0.0.1", 0, allow_ephemeral=True)
        self.port = self.sock.getsockname()[1]
        mcp = build_server(cfg, read_only=True)
        self.server = ht.make_server(ht.build_app(mcp, tokens.get, "127.0.0.1", self.port))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve(sockets=[self.sock])), daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.sock.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


@pytest.fixture
def live(cfg, conn, token):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    with LiveServer(cfg, ht.TokenStore()) as s:
        yield s


async def test_end_to_end_mcp_session_over_real_loopback_http(live, token):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(headers=bearer(token))
    async with http, Client(streamable_http_client(live.url, http_client=http)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "list_accounts" in names and "set_budget" not in names and "sync_accounts" not in names
        result = await c.call_tool("list_accounts", {})
        assert not result.is_error
        assert "Checking" in str(result.structured_content or result.content)
        refused = await c.call_tool("set_budget", {"items": [], "dry_run": False})
        assert refused.is_error


async def test_real_socket_rejections(live, token):
    async with httpx2.AsyncClient() as c:
        assert (await c.post(live.url, json=INIT, headers=JSON)).status_code == 401
        assert (await c.post(live.url, json=INIT, headers={**JSON, **bearer("nope")})).status_code == 401
        r = await c.post(live.url, json=INIT, headers={**JSON, **bearer(token), "origin": "http://evil.example"})
        assert r.status_code == 403
        r = await c.post(live.url, json=INIT, headers={**JSON, **bearer(token), "host": f"evil.com:{live.port}"})
        assert r.status_code == 421
        r = await c.options(
            live.url, headers={"origin": "http://evil.example", "access-control-request-method": "POST"}
        )
        assert r.status_code in (403, 405)
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]
        assert "server" not in r.headers


def test_uvicorn_is_configured_without_access_logs_or_server_header(app):
    cfgu = ht.make_server(app).config
    assert cfgu.access_log is False and cfgu.server_header is False and cfgu.proxy_headers is False
    assert cfgu.limit_concurrency == 16 and cfgu.log_config is None
