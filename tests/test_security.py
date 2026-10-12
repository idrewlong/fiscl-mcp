"""Security hardening: redirects never carry credentials, the chart view can't run injected markup,
private file modes, and local-only checks."""

import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from fiscl import cli
from fiscl.charts import CHART_HTML
from fiscl.config import ConfigError, ensure_layout, load_config
from fiscl.sync import simplefin


class _Redirector(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        _Redirector.hits.append(self.path)
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def test_sync_opener_never_follows_redirects():
    """A redirect from the bank-sync host must not be followed (it would resend the credentials)."""
    server = HTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Redirector.hits = []
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        with pytest.raises(urllib.error.HTTPError) as exc:
            simplefin.open_url(urllib.request.Request(url), timeout=5)
        assert exc.value.code == 302
        assert _Redirector.hits == ["/start"]
    finally:
        server.shutdown()


def test_sync_redirect_is_a_readable_error():
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, None)

    with pytest.raises(simplefin.SyncError, match="redirect"):
        simplefin.fetch(
            "https://user:pw@bridge.simplefin.org/simplefin",
            simplefin.date(2026, 1, 1),
            simplefin.date(2026, 1, 31),
            simplefin.DEFAULT_ALLOWED_HOSTS,
            opener,
        )


def test_sync_credentials_are_never_forwarded():
    seen = {}

    def opener(req, timeout):
        seen["req"] = req
        raise urllib.error.HTTPError(req.full_url, 500, "x", {}, None)

    with pytest.raises(simplefin.SyncError):
        simplefin.fetch(
            "https://user:pw@bridge.simplefin.org/simplefin",
            simplefin.date(2026, 1, 1),
            simplefin.date(2026, 1, 31),
            simplefin.DEFAULT_ALLOWED_HOSTS,
            opener,
        )
    req = seen["req"]
    assert "Authorization" in req.unredirected_hdrs
    assert "Authorization" not in req.headers
    assert "pw" not in req.full_url


def test_chart_view_cannot_render_injected_markup():
    # Tooltips carry bank-derived labels; they must be set as text, never parsed as HTML.
    assert "tip.innerHTML" not in CHART_HTML
    assert "tip.textContent" in CHART_HTML
    # Only the host frame may drive the view.
    assert "ev.source !== window.parent" in CHART_HTML
    # Even if markup slipped through, the page can't load or send anything.
    assert "Content-Security-Policy" in CHART_HTML
    assert "default-src 'none'" in CHART_HTML
    assert "connect-src" not in CHART_HTML or "connect-src 'none'" in CHART_HTML


def test_cli_creates_private_logs(tmp_path):
    home = tmp_path / "fh"
    old = os.umask(0o022)
    try:
        assert cli.main(["--home", str(home), "doctor"]) == 0
    finally:
        os.umask(old)
    for p in (home / "logs" / "fiscl.log", home / "fiscl.db"):
        assert stat.S_IMODE(p.stat().st_mode) & 0o077 == 0, p


def test_refuses_to_use_the_home_directory_itself(tmp_path, monkeypatch):
    # Point HOME at a temp dir so a regression can never chmod or litter the real home directory.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with pytest.raises(ConfigError):
        ensure_layout(load_config(fake_home))
    assert list(fake_home.iterdir()) == []


@pytest.mark.parametrize(
    ("path", "synced"),
    [
        ("~/Library/Mobile Documents/com~apple~CloudDocs/fiscl", True),
        ("~/Library/CloudStorage/OneDrive-Personal/fiscl", True),
        ("~/Dropbox/fiscl", True),
        ("~/Google Drive/fiscl", True),
        ("~/fiscl", False),
    ],
)
def test_cloud_sync_folder_warning(path, synced):
    assert (cli.cloud_sync_warning(Path(path).expanduser()) is not None) == synced


# --- audit log carries no error text (SEC-1) --------------------------------------------------


async def test_failed_calls_leave_no_names_or_amounts_in_audit_log(cfg):
    from mcp import Client

    from fiscl.db import connect
    from fiscl.server import build_server

    from .conftest import add_txn

    c0 = connect(cfg)
    from fiscl.db import add_account

    acct = add_account(c0, "Checking", "checking")
    t = add_txn(c0, acct, "2026-10-05", -12345, "ZEBRA CAFE", "Dining")
    c0.commit()
    c0.close()
    calls = [
        ("merchant_history", {"merchant": "ZEBRAQQ UNKNOWN"}),
        (
            "split_transaction",
            {
                "transaction_id": t,
                "parts": [{"amount": -50, "category": "Dining"}, {"amount": -50, "category": "Groceries"}],
            },
        ),
        ("manage_asset", {"action": "update", "name": "Secret Boat Fund", "value": 5}),
        ("manage_goal", {"action": "update", "name": "Hidden Goal Name"}),
    ]
    async with Client(build_server(cfg)) as c:
        for name, args in calls:
            try:
                await c.call_tool(name, args)
            except Exception:  # noqa: BLE001, S110 - tool errors surface as exceptions
                pass
    log = (cfg.logs / "audit.log").read_text()
    assert log.strip(), "audit log should have entries"
    for leak in ("ZEBRA", "Secret Boat", "Hidden Goal", "$", "123.45", "100.00"):
        assert leak not in log, leak
    assert "not_found" in log and "sum_mismatch" in log


async def test_tool_error_text_is_redacted_for_the_model(cfg):
    from mcp import Client

    from fiscl.server import build_server

    async with Client(build_server(cfg)) as c:
        r = await c.call_tool("merchant_history", {"merchant": "4111111111111111"})
    assert r.is_error
    text = json.dumps([getattr(x, "text", "") for x in r.content], ensure_ascii=False)
    assert "4111111111111111" not in text and "••••1111" in text


def test_backup_file_is_private_even_if_original_is_loose(tmp_path):
    from fiscl.config import backup_file

    src = tmp_path / "budgets.toml"
    src.write_text("x = 1\n")
    os.chmod(src, 0o644)
    backup_file(src)
    bak = tmp_path / "budgets.toml.bak"
    assert bak.read_text() == "x = 1\n"
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    os.chmod(bak, 0o666)  # an existing loose .bak is tightened on the next backup
    backup_file(src)
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
