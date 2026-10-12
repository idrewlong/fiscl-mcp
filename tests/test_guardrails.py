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
    analysis.manage_asset(conn, cfg, action="add", name="Truck", kind="vehicle", value=1, today=TODAY)
    analysis.manage_goal(conn, cfg, action="add", name="Trip", target=10, today=TODAY)
    for kind in ("spending_by_category", "spending_by_month", "cash_flow", "forecast", "budget"):
        build_chart(conn, cfg, kind=kind, today=TODAY)


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
