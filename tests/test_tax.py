from decimal import Decimal

import pytest

from fiscl import tax
from fiscl.guard import GuardError

from .conftest import TODAY, add_txn


def setup_business(conn, accounts):
    b = accounts["biz"]
    add_txn(conn, b, "2026-03-01", 2_000_000, "CLIENT PAYMENT")
    add_txn(conn, b, "2026-03-02", -60000, "ADOBE", "Business: Software & Subscriptions")
    add_txn(conn, b, "2026-03-03", -10000, "LUNCH", "Business: Meals")
    add_txn(conn, b, "2026-03-04", -325000, "B&H PHOTO")  # uncategorized, large
    add_txn(conn, b, "2026-03-05", -4000, "COMCAST", "Utilities")  # everyday category -> line 25


def test_schedule_c_lines_meals_and_mileage(conn, cfg, accounts):
    setup_business(conn, accounts)
    conn.execute(
        "INSERT INTO trips (trip_date, miles_tenths, purpose, created_at) VALUES ('2026-03-10', 1000, "
        "'Client shoot', 'x')"
    )
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    lines = {e["line"]: e for e in d["expenses_by_line"]}
    assert d["gross_receipts"]["minor"] == 2_000_000
    assert lines["27b"]["amount"]["minor"] == 60000
    assert lines["24b"]["amount"]["minor"] == 5000 and lines["24b"]["full_amount"]["minor"] == 10000
    assert lines["25"]["amount"]["minor"] == 4000
    assert lines["9"]["amount"]["minor"] == 7250  # 100 miles x 72.5 cents
    assert d["unmapped_business_spending"][0]["spent"]["minor"] == 325000
    assert d["tentative_net_profit"]["minor"] == 2_000_000 - (60000 + 5000 + 4000 + 7250)
    assert any("not tax advice" in n for n in d["notes"])


def test_personal_spending_never_lands_on_schedule_c(conn, cfg, accounts):
    add_txn(conn, accounts["checking"], "2026-03-02", -60000, "ADOBE", "Business: Software & Subscriptions")
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    assert d["expenses_by_line"] == [] and d["total_expenses"]["minor"] == 0
    # ...until the user marks it business.
    tid = conn.execute("SELECT id FROM transactions").fetchone()[0]
    tax.set_scope(conn, cfg, [{"transaction_ids": [tid], "scope": "business"}], dry_run=False)
    assert tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["total_expenses"]["minor"] == 60000


def test_self_employment_tax_math(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-01-15", 10_000_000, "CLIENT")  # $100,000 profit
    e = tax.estimated_tax(conn, cfg, year=2026, income_tax_rate_pct=20, today=TODAY.replace(year=2027))
    net = Decimal(10_000_000) * Decimal("0.9235")
    expected_se = int((net * Decimal("0.153")).quantize(Decimal("1")))
    assert e["self_employment_tax"]["minor"] == expected_se
    taxable = 10_000_000 - expected_se // 2
    assert e["income_tax_estimate"]["minor"] == int((Decimal(taxable) * Decimal("0.20")).quantize(Decimal("1")))
    assert len(e["due_dates"]) == 4 and e["due_dates"][3]["date"] == "2027-01-15"


def test_ss_wage_base_caps_social_security(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-01-15", 50_000_000, "CLIENT")  # $500,000
    e = tax.estimated_tax(conn, cfg, year=2026, today=TODAY.replace(year=2027))
    net = Decimal(50_000_000) * Decimal("0.9235")
    expected = int((Decimal(18_450_000) * Decimal("0.124") + net * Decimal("0.029")).quantize(Decimal("1")))
    assert e["self_employment_tax"]["minor"] == expected and e["income_tax_estimate"] is None


def test_review_writeoffs_raises_questions_not_conclusions(conn, cfg, accounts):
    setup_business(conn, accounts)
    add_txn(conn, accounts["checking"], "2026-03-08", -400, "GITHUB INC")
    add_txn(conn, accounts["checking"], "2026-03-09", 150000, "ZELLE FROM MAGNOLIA MEDIA")
    r = tax.review_writeoffs(conn, cfg, year=2026, today=TODAY)
    s = r["sections"]
    assert s["possibly_business_in_personal"][0]["merchant"] == "Github Inc"
    assert s["large_business_purchases"][0]["amount"]["minor"] == -325000
    assert "business_meals" in s and s["income_to_confirm"][0]["total"]["minor"] == 150000
    assert all("question" in item for items in s.values() for item in items)


def test_log_trip_dry_run_and_validation(conn, cfg, accounts):
    r = tax.log_trip(conn, cfg, "2026-10-01", 42.5, "Client shoot, Biloxi")
    assert r["dry_run"] and r["trip"]["estimated_deduction"]["minor"] == 3081
    assert conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 0
    tax.log_trip(conn, cfg, "2026-10-01", 42.5, "Client shoot, Biloxi", dry_run=False)
    assert tax.mileage_log(conn, cfg, year=2026, today=TODAY)["total_miles"] == "42.5"
    with pytest.raises(GuardError):
        tax.log_trip(conn, cfg, "2026-10-01", 10, "")
    with pytest.raises(GuardError):
        tax.log_trip(conn, cfg, "2999-01-01", 10, "Future trip")


def test_account_scope_change(conn, cfg, accounts):
    plan = tax.set_scope(conn, cfg, [{"account": "Checking", "scope": "mixed"}])
    assert plan["changes"][0]["from"] == "personal"
    tax.set_scope(conn, cfg, [{"account": "Checking", "scope": "mixed"}], dry_run=False)
    assert conn.execute("SELECT scope FROM accounts WHERE name = 'Checking'").fetchone()[0] == "mixed"
