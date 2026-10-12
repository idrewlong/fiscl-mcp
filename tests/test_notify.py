"""Resource change notifications: debounce, URIs only, applied edits, and the watcher thread reaching the loop."""

from __future__ import annotations

import time
from datetime import date

import anyio
import pytest
from mcp import Client

from fiscl.demo import load_demo
from fiscl.notify import ChangeNotifier, uris_for
from fiscl.server import build_server
from fiscl.watch import start_watcher

from .conftest import TODAY, write

ALL_URIS = [
    "fiscl://accounts",
    "fiscl://summary/current-month",
    "fiscl://recurring",
    "fiscl://budgets",
    "fiscl://goals",
    "fiscl://rules",
    f"fiscl://tax/{date.today().year}",
]

OFX = (
    "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Test Bank</FI></SONRS>"
    "</SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD"
    "<BANKACCTFROM><BANKID>1<ACCTID>5550001234<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST>"
    "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260903<TRNAMT>-10.00<FITID>A1<NAME>Cafe</STMTTRN>"
    "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
)


@pytest.fixture
def demo(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    return cfg


async def collect(sub, quiet: float = 0.6, limit: float = 6.0) -> list[str]:
    """URIs delivered until the stream has been quiet for `quiet` seconds (or `limit` seconds in all)."""
    uris: list[str] = []
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        got = None
        with anyio.move_on_after(quiet):
            got = await sub.__anext__()
        if got is None:
            break
        uris.append(got.uri)
    return uris


def test_uri_sets():
    imp = uris_for("import", year=2026)
    assert "fiscl://accounts" in imp and "fiscl://tax/2026" in imp and "fiscl://summary/current-month" in imp
    edit = uris_for("edit", year=2026)
    assert "fiscl://accounts" not in edit and "fiscl://budgets" in edit
    assert "fiscl://rules" in uris_for("edit", ("fiscl://rules",), year=2026)
    with pytest.raises(ValueError):
        uris_for("nope")


async def test_applied_set_budget_notifies_once_and_a_dry_run_does_not(demo):
    server = build_server(demo)
    server.notifier._debounce = 0.1
    async with Client(server) as c, c.listen(resource_subscriptions=ALL_URIS) as sub:
        item = {"items": [{"category": "Dining", "monthly": 250}]}
        await c.call_tool("set_budget", item)  # dry run by default
        assert await collect(sub) == []
        await c.call_tool("set_budget", {**item, "dry_run": False})
        got = await collect(sub)
    assert got.count("fiscl://budgets") == 1  # debounced to one event per URI
    assert len(got) == len(set(got))  # nothing published twice
    assert "fiscl://accounts" not in got and "fiscl://rules" not in got


async def test_saved_rules_also_notify_rules(demo):
    server = build_server(demo)
    server.notifier._debounce = 0.1
    async with Client(server) as c, c.listen(resource_subscriptions=ALL_URIS) as sub:
        args = {"items": [{"merchant": "Planet Fitness", "category": "Health"}], "create_rule": True}
        await c.call_tool("apply_categories", {**args, "dry_run": False})
        got = await collect(sub)
    assert "fiscl://rules" in got


async def test_file_writing_tools_send_nothing(demo):
    server = build_server(demo)
    server.notifier._debounce = 0.1
    async with Client(server) as c, c.listen(resource_subscriptions=ALL_URIS) as sub:
        await c.call_tool("write_digest", {"dry_run": False})
        assert await collect(sub) == []


async def test_notification_payload_is_only_a_uri(demo):
    server = build_server(demo)
    server.notifier._debounce = 0.1
    async with Client(server) as c, c.listen(resource_subscriptions=ALL_URIS) as sub:
        await c.call_tool("set_budget", {"items": [{"category": "Dining", "monthly": 99}], "dry_run": False})
        with anyio.fail_after(5):
            event = await sub.__anext__()
    assert set(vars(event)) == {"uri"} and event.uri.startswith("fiscl://")


async def test_inbox_import_reaches_a_listener_within_five_seconds(demo):
    server = build_server(demo)
    server.notifier._debounce = 0.2
    observer = None
    try:
        async with Client(server) as c, c.listen(resource_subscriptions=ALL_URIS) as sub:
            observer = start_watcher(demo, on_imported=lambda: server.notifier.changed("import"))
            write(demo.inbox / "new.ofx", OFX)
            got: list[str] = []
            with anyio.fail_after(5):
                while not {"fiscl://accounts", "fiscl://summary/current-month"} <= set(got):
                    got.append((await sub.__anext__()).uri)
    finally:
        if observer is not None:
            observer.stop()
    assert {"fiscl://accounts", "fiscl://summary/current-month"} <= set(got)


@pytest.mark.parametrize(("added", "expect"), [(3, True), (0, False)])
async def test_sync_notifies_only_when_rows_arrived(demo, monkeypatch, added, expect):
    from fiscl.sync import registry

    def fake_sync(conn, cfg, **kw):
        return {"results": {}, "skipped": [], "errors": [], "totals": {"rows_added": added}, "meta": {}}

    monkeypatch.setattr(registry, "sync_accounts", fake_sync)
    server = build_server(demo)
    server.notifier._debounce = 0.1
    async with Client(server) as c, c.listen(resource_subscriptions=ALL_URIS) as sub:
        assert not (await c.call_tool("sync_accounts", {})).is_error
        got = await collect(sub)
    assert ("fiscl://accounts" in got) is expect


def test_no_listener_yet_is_harmless():
    class Bus:
        async def publish(self, event):  # pragma: no cover - must not be called
            raise AssertionError

    n = ChangeNotifier(Bus(), debounce=0.01)
    n.changed("edit")
    time.sleep(0.1)  # no loop token: the flush drops the event instead of raising
    n.close()
