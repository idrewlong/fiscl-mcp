from datetime import date

import pytest

from fiscl import analysis
from fiscl.guard import GuardError
from fiscl.recurring import detect, refresh_recurring

from .conftest import TODAY, add_txn


def test_spending_excludes_transfers_and_nets_refunds(conn, cfg, accounts):
    a = accounts["checking"]
    add_txn(conn, a, "2026-10-01", -10000, "KROGER", "Groceries")
    add_txn(conn, a, "2026-10-02", 2500, "KROGER REFUND", "Groceries")
    tid = add_txn(conn, a, "2026-10-03", -50000, "TRANSFER TO SAVINGS")
    conn.execute("UPDATE transactions SET transfer_group = 'x' WHERE id = ?", (tid,))
    add_txn(conn, a, "2026-10-04", 300000, "PAYROLL", "Income: Salary")
    s = analysis.spending_summary(conn, cfg, from_="2026-10-01", to="2026-10-31", compare_to="none", today=TODAY)
    assert s["total_spent"]["minor"] == 7500
    cf = analysis.cash_flow(conn, cfg, from_="2026-10-01", to="2026-10-31", today=TODAY)
    assert cf["totals"]["income"]["minor"] == 300000 and cf["totals"]["spending"]["minor"] == 7500


def test_query_totals_cover_all_matches_even_when_truncated(conn, cfg, accounts):
    for i in range(1, 8):
        add_txn(conn, accounts["checking"], f"2026-10-0{i}", -100 * i, f"SHOP {i}")
    r = analysis.query_transactions(conn, cfg, from_="2026-10-01", to="2026-10-31", limit=3, today=TODAY)
    assert len(r["transactions"]) == 3 and r["meta"]["truncated"]
    assert r["totals"]["count"] == 7 and r["totals"]["money_out"]["minor"] == -2800


def test_limit_is_capped_by_hard_max(conn, cfg, accounts):
    r = analysis.query_transactions(conn, cfg, limit=10_000, today=TODAY)
    assert r["meta"]["row_count"] == 0  # no data, but no error


def test_unknown_account_lists_real_ones(conn, cfg, accounts):
    with pytest.raises(GuardError, match="Checking"):
        analysis.query_transactions(conn, cfg, account="Nope", today=TODAY)


def test_budget_pace_projection(conn, cfg, accounts):
    cfg.budgets_file.write_text('[[budget]]\ncategory = "Dining"\nmonthly = 300\n')
    add_txn(conn, accounts["checking"], "2026-10-05", -15000, "CHIPOTLE", "Dining")
    b = analysis.budget_status(conn, cfg, month="2026-10", today=TODAY)
    line = b["budgets"][0]
    assert line["spent"]["minor"] == 15000
    assert line["projected_month_end"]["minor"] == 15000 * 31 // 12
    assert line["status"] == "at_risk"


def test_recurring_monthly_with_price_change_and_weekly_groceries_excluded():
    series = [(date(2026, m, 3), 1549) for m in range(4, 10)] + [(date(2026, 10, 3), 1799)]
    r = detect(series, 3, 15, TODAY)
    assert r["cadence"] == "monthly" and r["status"] == "price_changed" and r["typical_amount"] == 1549
    import random

    rng = random.Random(1)
    groceries = [
        (date(2026, 4, 1).fromordinal(date(2026, 4, 1).toordinal() + 7 * i), rng.randint(6000, 14500))
        for i in range(20)
    ]
    assert detect(groceries, 3, 15, TODAY) is None


def test_recurring_missed_and_cancelled():
    series = [(date(2026, m, 1), 2000) for m in range(3, 8)]  # last charge July 1
    assert detect(series, 3, 15, date(2026, 8, 10))["status"] == "missed"
    assert detect(series, 3, 15, TODAY)["status"] == "cancelled"


def test_forecast_includes_bills_income_and_card_payment(conn, cfg, accounts):
    chk, card = accounts["checking"], accounts["card"]
    conn.execute("INSERT INTO balances VALUES (?, '2026-10-12', 100000, 'manual')", (chk,))
    for m in range(5, 11):
        add_txn(conn, chk, f"2026-{m:02d}-01", -80000, "RENT", "Housing")
    for m in range(5, 10):
        p = add_txn(conn, chk, f"2026-{m:02d}-20", -30000, "CARD PMT")
        q = add_txn(conn, card, f"2026-{m:02d}-21", 30000, "PAYMENT THANK YOU")
        conn.execute("UPDATE transactions SET transfer_group = ? WHERE id IN (?, ?)", (f"g{m}", p, q))
    add_txn(conn, card, "2026-10-01", -25000, "KROGER")
    refresh_recurring(conn, cfg, today=TODAY)
    f = analysis.forecast_cash(conn, cfg, days=30, include_discretionary=False, today=TODAY)
    kinds = {(e["merchant"], e["amount"]["minor"]) for e in f["upcoming"]}
    assert ("Card payment", -25000) in kinds and ("Rent", -80000) in kinds
    assert f["lowest_balance"]["amount"]["minor"] == 100000 - 25000 - 80000
    assert f["goes_negative"] is True and f["safe_to_spend"]["minor"] == 0


def test_apply_categories_dry_run_then_apply_with_rule(conn, cfg, accounts):
    add_txn(conn, accounts["card"], "2026-10-01", -9000, "KROGER #12")
    plan = analysis.apply_categories(conn, cfg, [{"merchant": "Kroger", "category": "Groceries"}], create_rule=True)
    assert plan["dry_run"] and plan["transactions_affected"] == 1
    assert conn.execute("SELECT category_id FROM transactions").fetchone()[0] is None
    assert "Kroger" not in cfg.rules_file.read_text()
    analysis.apply_categories(
        conn, cfg, [{"merchant": "Kroger", "category": "Groceries"}], create_rule=True, dry_run=False
    )
    row = conn.execute("SELECT category_source FROM transactions").fetchone()
    assert row[0] == "manual" and 'merchant = "Kroger"' in cfg.rules_file.read_text()
    assert cfg.rules_file.with_suffix(".toml.bak").exists()


def test_apply_categories_refuses_transfers_and_ambiguous_targets(conn, cfg, accounts):
    with pytest.raises(GuardError):
        analysis.apply_categories(conn, cfg, [{"merchant": "X", "category": "Transfers"}])
    with pytest.raises(GuardError):
        analysis.apply_categories(conn, cfg, [{"merchant": "X", "transaction_ids": [1], "category": "Dining"}])
