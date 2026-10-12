"""Guardrails from PROJECT.md §14 that aren't covered elsewhere."""

import os
import socket
import stat
import sys

import pytest

from fiscl import analysis, tax
from fiscl.charts import build_chart
from fiscl.demo import load_demo
from fiscl.watch import process_inbox_file

from .conftest import TODAY, write


def test_core_makes_no_network_calls(conn, cfg, monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    load_demo(conn, cfg, today=TODAY)
    analysis.forecast_cash(conn, cfg, today=TODAY)
    analysis.spending_summary(conn, cfg, today=TODAY)
    tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    tax.review_writeoffs(conn, cfg, year=2026, today=TODAY)
    analysis.query_transactions(conn, cfg, tag="x", today=TODAY)
    analysis.add_note(conn, cfg, transaction_id=1, note="n")
    analysis.tag_transactions(conn, cfg, [{"transaction_ids": [1], "add": ["x"]}])
    analysis.set_budget(conn, cfg, [{"category": "Dining", "monthly": 5}])
    analysis.net_worth(conn, cfg, today=TODAY)
    analysis.afford(conn, cfg, amount=2000, today=TODAY)
    analysis.debt_payoff(
        conn, cfg, debts=[{"name": "Card", "balance": 1000, "apr": 12, "min_payment": 100}], today=TODAY
    )
    analysis.goal_progress(conn, cfg, today=TODAY)
    analysis.find_anomalies(conn, cfg, today=TODAY)
    analysis.subscription_watchdog(conn, cfg, today=TODAY)
    analysis.refund_tracker(conn, cfg, today=TODAY)
    analysis.merchant_history(conn, cfg, merchant="Kroger Gulfport", today=TODAY)
    analysis.category_trend(conn, cfg, category="Groceries", today=TODAY)
    analysis.manage_asset(conn, cfg, action="add", name="Truck", kind="vehicle", value=1, today=TODAY)
    analysis.manage_goal(conn, cfg, action="add", name="Trip", target=10, today=TODAY)
    for kind in ("spending_by_category", "spending_by_month", "cash_flow", "forecast", "budget", "net_worth"):
        build_chart(conn, cfg, kind=kind, today=TODAY)
    build_chart(conn, cfg, kind="category_trend", category="Groceries", today=TODAY)


# Arguments for every MCP tool except sync_accounts (the only one allowed to touch the network). The test below
# fails when a tool is registered that is missing here, so new tools are covered automatically.
NO_NETWORK_TOOL_ARGS: dict[str, dict] = {
    "show_chart": {"kind": "spending_by_category"},
    "query_transactions": {},
    "spending_summary": {},
    "cash_flow": {},
    "detect_recurring": {},
    "budget_status": {},
    "suggest_budgets": {},
    "forecast_cash": {},
    "net_worth": {},
    "afford": {"amount": 2000},
    "debt_payoff": {"debts": [{"name": "Card", "balance": 1000, "apr": 12, "min_payment": 100}]},
    "goal_progress": {},
    "find_anomalies": {},
    "subscription_watchdog": {},
    "refund_tracker": {},
    "merchant_history": {"merchant": "Kroger Gulfport"},
    "category_trend": {"category": "Groceries"},
    "list_accounts": {},
    "import_status": {},
    "list_uncategorized": {},
    "apply_categories": {"items": [{"merchant": "Kroger Gulfport", "category": "Groceries"}]},
    "add_note": {"transaction_id": 1, "note": "n"},
    "tag_transactions": {"items": [{"transaction_ids": [1], "add": ["x"]}]},
    "split_transaction": {"transaction_id": 1, "parts": []},
    "dismiss_recurring": {"merchant": "Spotify"},
    "set_budget": {"items": [{"category": "Dining", "monthly": 5}]},
    "manage_asset": {"action": "add", "name": "Truck", "kind": "vehicle", "value": 1},
    "manage_goal": {"action": "add", "name": "Trip", "target": 10},
    "schedule_c_draft": {"year": 2026},
    "estimated_tax": {"year": 2026},
    "review_writeoffs": {"year": 2026},
    "set_scope": {"items": [{"merchant": "Kroger Gulfport", "scope": "personal"}]},
    "log_trip": {"trip_date": "2026-10-01", "miles": 10, "purpose": "Client visit"},
    "mileage_log": {"year": 2026},
    "export_schedule_c": {"year": 2026, "include_transactions": True},
    "record_1099": {"items": [{"action": "add", "tax_year": 2026, "form": "nec", "payer": "Acme", "amount": 100}]},
    "reconcile_1099": {"tax_year": 2026},
    "list_connections": {},
}


async def test_every_mcp_tool_except_sync_makes_no_network_calls(conn, cfg, monkeypatch):
    from mcp import Client

    from fiscl.server import build_server

    load_demo(conn, cfg, today=TODAY)
    conn.commit()

    attempts: list[str] = []

    def blocked(*a, **k):
        attempts.append("network access attempted")  # tool wrappers swallow exceptions, so record instead
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    async with Client(build_server(cfg)) as c:
        registered = {t.name for t in (await c.list_tools()).tools}
        assert registered - {"sync_accounts"} == set(NO_NETWORK_TOOL_ARGS), "tool registry changed: update the table"
        for name, args in NO_NETWORK_TOOL_ARGS.items():
            await c.call_tool(name, args)  # bad-input tool errors are fine; only network use matters here
            assert not attempts, name


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_private_permissions(cfg, conn):
    assert stat.S_IMODE(os.stat(cfg.home).st_mode) == 0o700
    for f in (cfg.db_path, cfg.config_file, cfg.rules_file, cfg.budgets_file):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, f


def test_watcher_never_guesses_an_account(cfg, conn, accounts):
    f = write(cfg.inbox / "export.csv", "Date,Description,Amount\n10/01/2026,SHOP,-1.00\n")
    assert process_inbox_file(cfg, f) == "failed"
    assert (cfg.failed / "export.csv").exists() and (cfg.failed / "export.csv.error.txt").exists()
    # With a profile that names the account, the same layout imports and is filed as processed.
    write(
        cfg.profiles_dir / "mine.toml",
        'name = "mine"\nmatch_headers = ["Date", "Description", "Amount"]\n'
        'date_column = "Date"\ndescription_column = "Description"\namount_column = "Amount"\n'
        'account = "Checking"\n',
    )
    f2 = write(cfg.inbox / "export2.csv", "Date,Description,Amount\n10/01/2026,SHOP,-1.00\n")
    assert process_inbox_file(cfg, f2) == "ok"
    assert (cfg.processed / "export2.csv").exists()


def test_bulk_write_cap(conn, cfg, accounts):
    from fiscl.guard import GuardError

    with pytest.raises(GuardError):
        analysis.apply_categories(conn, cfg, [{"transaction_ids": list(range(1, 600)), "category": "Dining"}])
