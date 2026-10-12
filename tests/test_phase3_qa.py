"""Phase 3 independent QA: hand-computed tax arithmetic and edge cases, written without reading the builders' tests.

Every expected number below is derived in the comment above it from the rule, not from running the code.
Setup: TODAY is 2026-10-12, which is day 285 of 2026 (Jan 1..Sep 30 = 273 days, + 12). A current-year estimate
annualizes year-to-date profit by 365 / 285, so profits that are multiples of 285 annualize exactly.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
import sys
from datetime import date, datetime
from urllib.parse import quote

import pytest
from mcp import Client

from fiscl import tax
from fiscl.config import load_config
from fiscl.db import add_account
from fiscl.guard import GuardError
from fiscl.server import build_server
from fiscl.tax.forms1099 import nec_misc_threshold

from .conftest import TODAY, add_txn

NOW = datetime(2026, 10, 12, 9, 30, 0)
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and symlinks")

INCOME = "Income: Business"
SOFTWARE = "Business: Software & Subscriptions"


def with_config(cfg, text: str):
    cfg.config_file.write_text(text, encoding="utf-8")
    return load_config(cfg.home)


def two_biz(conn, cfg, year_str="2026"):
    """Studio (default, renamed) and Consulting, each with its own business account."""
    tax.manage_business(conn, cfg, action="rename", name="My business", new_name="Studio", dry_run=False)
    tax.manage_business(conn, cfg, action="add", name="Consulting", dry_run=False)
    studio = add_account(conn, "Studio acct", "checking", scope="business")
    consult = add_account(conn, "Consulting acct", "checking", scope="business")
    tax.set_scope(conn, cfg, [{"account": "Consulting acct", "scope": "business", "business": "Consulting"}], False)
    conn.commit()
    return studio, consult


# --- home office --------------------------------------------------------------------------------


def test_home_office_part_year_rounds_half_even_once(conn, cfg, accounts):
    # 200 sq ft x 7 months / 12 = 116.666... sq ft; x $5 = $583.333... -> 58,333 minor. Display area 116.7.
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 200\nmonths_used = 7\n")
    add_txn(conn, accounts["biz"], "2026-03-01", 1_000_000, "Client")
    conn.commit()
    ho = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["home_office"]
    assert ho["allowable_square_feet"] == "116.7"
    assert ho["computed"]["minor"] == 58_333 and ho["line_30"]["minor"] == 58_333


def test_home_office_300_sqft_cap_is_1500_dollars(conn, cfg, accounts):
    # 300 sq ft x 12/12 x $5 = $1,500.00; 300 x 5/12 = 125 sq ft -> $625.00
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 300\n")
    add_txn(conn, accounts["biz"], "2026-03-01", 1_000_000, "Client")
    conn.commit()
    assert tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["home_office"]["line_30"]["minor"] == 150_000
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 300\nmonths_used = 5\n")
    assert tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["home_office"]["line_30"]["minor"] == 62_500


@pytest.mark.parametrize("sqft", [0, 301, -5])
def test_home_office_square_feet_bounds_fail_config_load(cfg, sqft):
    from fiscl.config import ConfigError

    cfg.config_file.write_text(f"[tax.home_office.2026]\nsquare_feet = {sqft}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cfg.home)


def test_home_office_gross_income_limit_boundary(conn, cfg, accounts):
    # 200 sq ft -> $1,000. Profit before home office exactly $1,000.00: not limited. $999.99: limited to $999.99.
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 200\n")
    add_txn(conn, accounts["biz"], "2026-03-01", 100_000, "Client")
    conn.commit()
    ho = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["home_office"]
    assert ho["line_30"]["minor"] == 100_000 and ho["limited_to_profit"] is False
    add_txn(conn, accounts["biz"], "2026-03-02", -1, "Pen", category=SOFTWARE)
    conn.commit()
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    assert d["home_office"]["line_30"]["minor"] == 99_999 and d["home_office"]["limited_to_profit"] is True
    assert d["net_profit"]["minor"] == 0


def test_home_office_allocation_with_part_year_and_per_business_limit(conn, cfg):
    # Studio 150 sq ft, Consulting 50 sq ft, 6 months: 150 x 6/12 = 75 sq ft = $375.00; 50 x 6/12 = 25 = $125.00.
    # Consulting profit is only $100.00, so its line 30 is limited to $100.00 while Studio's is untouched.
    cfg = with_config(
        cfg,
        "[tax.home_office.2026]\nsquare_feet = 200\nmonths_used = 6\n[tax.home_office.2026.allocation]\n"
        '"Studio" = 150\n"consulting" = 50\n',  # lower-case name must still match the business
    )
    studio, consult = two_biz(conn, cfg)
    add_txn(conn, studio, "2026-03-01", 500_000, "Client A", category=INCOME)
    add_txn(conn, consult, "2026-03-01", 10_000, "Client B", category=INCOME)
    conn.commit()
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    by = {b["business"]["name"]: b for b in d["businesses"]}
    assert by["Studio"]["home_office"]["line_30"]["minor"] == 37_500
    assert by["Studio"]["home_office"]["limited_to_profit"] is False
    assert by["Consulting"]["home_office"]["computed"]["minor"] == 12_500
    assert by["Consulting"]["home_office"]["line_30"]["minor"] == 10_000
    assert by["Consulting"]["home_office"]["limited_to_profit"] is True
    # net = (5,000 - 375) + (100 - 100) = 4,625.00
    assert d["combined_net_profit"]["minor"] == 462_500


def test_home_office_loss_business_gets_nothing_and_does_not_borrow(conn, cfg):
    # Consulting has a $200 loss; its $250 share is limited to 0. Studio's profit is not used for Consulting.
    cfg = with_config(
        cfg,
        "[tax.home_office.2026]\nsquare_feet = 100\n[tax.home_office.2026.allocation]\n"
        '"Studio" = 50\n"Consulting" = 50\n',
    )
    studio, consult = two_biz(conn, cfg)
    add_txn(conn, studio, "2026-03-01", 500_000, "Client A", category=INCOME)
    add_txn(conn, consult, "2026-03-01", -20_000, "Laptop", category=SOFTWARE)
    conn.commit()
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    by = {b["business"]["name"]: b for b in d["businesses"]}
    assert by["Consulting"]["home_office"]["line_30"]["minor"] == 0
    assert by["Studio"]["home_office"]["line_30"]["minor"] == 25_000  # 50 x $5
    assert by["Consulting"]["net_profit"]["minor"] == -20_000
    assert d["combined_net_profit"]["minor"] == 500_000 - 25_000 - 20_000


# --- estimated tax: SE tax near the Social Security wage base -----------------------------------


def _se_fixture(conn, accounts, before_minor):
    add_txn(conn, accounts["biz"], "2026-03-01", before_minor, "Client", category=INCOME)
    conn.commit()


def test_se_tax_with_w2_wages_just_under_the_2026_wage_base(conn, cfg, accounts):
    # YTD profit $57,000 -> annualized x 365/285 = $73,000 = 7,300,000 minor.
    # Net earnings = 7,300,000 x 0.9235 = 6,741,550.
    # W-2 $170,000 leaves 184,500 - 170,000 = $14,500 of wage base = 1,450,000.
    # SS = 12.4% x 1,450,000 = 179,800 ; Medicare = 2.9% x 6,741,550 = 195,504.95 ; total 375,304.95 -> 375,305.
    _se_fixture(conn, accounts, 5_700_000)
    e = tax.estimated_tax(conn, cfg, year=2026, w2_wages=170_000, today=TODAY)
    assert e["projected_annual_net_profit"]["minor"] == 7_300_000
    assert e["self_employment_tax"]["minor"] == 375_305


@pytest.mark.xfail(
    strict=True,
    reason="BUG-3: negative w2_wages is accepted and pushes Social Security tax above the wage-base cap",
)
def test_negative_w2_wages_cannot_raise_social_security_above_the_cap(conn, cfg, accounts):
    # 2025 profit $250,000 -> net earnings 2,308,750 x ... well above the $176,100 base. W-2 wages below zero make no
    # sense; they must be rejected (or treated as 0), never lift the SS-taxable amount above the base.
    add_txn(conn, accounts["biz"], "2025-03-01", 25_000_000, "Client", category=INCOME)
    conn.commit()
    base_case = tax.estimated_tax(conn, cfg, year=2025, w2_wages=0, today=TODAY)["self_employment_tax"]["minor"]
    try:
        got = tax.estimated_tax(conn, cfg, year=2025, w2_wages=-50_000, today=TODAY)["self_employment_tax"]["minor"]
    except GuardError:
        return
    assert got == base_case


def test_se_tax_with_w2_wages_over_or_at_the_wage_base_pays_only_medicare(conn, cfg, accounts):
    # SS room is zero at W-2 = 184,500 and below zero above it (never negative). Medicare only: 195,504.95 -> 195,505.
    _se_fixture(conn, accounts, 5_700_000)
    for w2 in (184_500, 190_000, 250_000):
        e = tax.estimated_tax(conn, cfg, year=2026, w2_wages=w2, today=TODAY)
        assert e["self_employment_tax"]["minor"] == 195_505, w2


def test_se_tax_without_wages_and_below_the_floor(conn, cfg, accounts):
    # No W-2: SE = 15.3% of 6,741,550 = 835,952.2 + 195,504.95 = 1,031,457.15 -> 1,031,457.
    _se_fixture(conn, accounts, 5_700_000)
    assert tax.estimated_tax(conn, cfg, year=2026, today=TODAY)["self_employment_tax"]["minor"] == 1_031_457


def test_se_tax_floor_is_net_earnings_of_400_dollars(conn, cfg, accounts):
    # Past year (no annualization). Net earnings = profit x 0.9235. Profit $433.00 -> 39,990.5 -> 39,990 (< $400.00):
    # no SE tax. Profit $433.10 -> 40,000 (43,310 x 0.9235 = 39,999.785 -> 39,999)... so use $433.20:
    # 43,320 x 0.9235 = 40,006.02 -> 40,006 >= 40,000: SE tax = 15.3% of 40,006 = 6,120.918 -> 6,121.
    add_txn(conn, accounts["biz"], "2025-03-01", 43_300, "Client", category=INCOME)
    conn.commit()
    assert tax.estimated_tax(conn, cfg, year=2025, today=TODAY)["self_employment_tax"]["minor"] == 0
    add_txn(conn, accounts["biz"], "2025-03-02", 20, "Tip", category=INCOME)
    conn.commit()
    assert tax.estimated_tax(conn, cfg, year=2025, today=TODAY)["self_employment_tax"]["minor"] == 6_121


def test_combined_se_tax_across_two_businesses_near_the_wage_base(conn, cfg):
    # Studio before = 285 x 11,000 = 3,135,000 -> annualized 4,015,000 ; Consulting 2,565,000 -> 3,285,000.
    # Sum 7,300,000: identical to the single-business case, so SE tax must be the same 375,305 with W-2 $170,000
    # (the wage base is applied once, not once per business).
    studio, consult = two_biz(conn, cfg)
    add_txn(conn, studio, "2026-03-01", 3_135_000, "A", category=INCOME)
    add_txn(conn, consult, "2026-03-01", 2_565_000, "B", category=INCOME)
    conn.commit()
    e = tax.estimated_tax(conn, cfg, year=2026, w2_wages=170_000, today=TODAY)
    assert e["projected_annual_net_profit"]["minor"] == 7_300_000
    assert e["self_employment_tax"]["minor"] == 375_305
    assert {b["business"]["name"]: b["projected_annual_net_profit"]["minor"] for b in e["by_business"]} == {
        "Studio": 4_015_000,
        "Consulting": 3_285_000,
    }


def test_combined_se_tax_with_a_loss_in_one_business(conn, cfg):
    # Studio before 285 x 150 = 4,275,000 -> 5,475,000. Consulting loss -570,000 -> -730,000. Combined 4,745,000.
    # Net earnings 4,745,000 x 0.9235 = 4,382,007.5 -> 4,382,007 (int). SS 12.4% = 543,368.868 ; Medicare 2.9% =
    # 127,078.203 ; sum 670,447.07 -> 670,447. The loss must offset, not be ignored (ignoring gives a larger tax).
    studio, consult = two_biz(conn, cfg)
    add_txn(conn, studio, "2026-03-01", 4_275_000, "A", category=INCOME)
    add_txn(conn, consult, "2026-03-01", -570_000, "Gear", category=SOFTWARE)
    conn.commit()
    e = tax.estimated_tax(conn, cfg, year=2026, today=TODAY)
    assert e["projected_annual_net_profit"]["minor"] == 4_745_000
    assert e["self_employment_tax"]["minor"] == 670_447


def test_current_year_home_office_is_subtracted_once_after_annualizing(conn, cfg, accounts):
    # 200 sq ft = $1,000 (100,000). Annualized 7,300,000 - 100,000 = 7,200,000 (not 365/285 x (5,700,000 - 100,000)).
    # Net earnings 7,200,000 x 0.9235 = 6,649,200. SE = 12.4% x 6,649,200 = 824,500.8 + 2.9% = 192,826.8 -> 1,017,327.6
    # -> 1,017,328. Half = 508,664 ; taxable 7,200,000 - 508,664 = 6,691,336 ; at 22% = 1,472,093.92 -> 1,472,094.
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 200\n")
    _se_fixture(conn, accounts, 5_700_000)
    e = tax.estimated_tax(conn, cfg, year=2026, income_tax_rate_pct=22, today=TODAY)
    assert e["projected_annual_net_profit"]["minor"] == 7_200_000
    assert e["self_employment_tax"]["minor"] == 1_017_328
    assert e["income_tax_estimate"]["minor"] == 1_472_094


# --- state estimates ----------------------------------------------------------------------------


def test_california_estimate_and_installments_by_hand(conn, cfg, accounts):
    # Projected 7,300,000. SE 1,031,457 (see above). Taxable = 7,300,000 - 1,031,457 // 2 (515,728) = 6,784,272.
    # 6% = 407,056.32 -> 407,056. Installments 30/40/0/30: 122,116.8 -> 122,117 ; 162,822.4 -> 162,822 ; 0 ;
    # last gets the remainder 407,056 - 122,117 - 162,822 = 122,117.
    _se_fixture(conn, accounts, 5_700_000)
    s = tax.estimated_tax(conn, cfg, year=2026, state="CA", state_income_tax_rate_pct=6, today=TODAY)["state"]
    assert s["income_tax_estimate"]["minor"] == 407_056
    assert [i["date"] for i in s["installments"]] == ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"]
    assert [i["pct"] for i in s["installments"]] == ["30", "40", "0", "30"]
    assert [i["amount"]["minor"] for i in s["installments"]] == [122_117, 162_822, 0, 122_117]
    assert [i["passed"] for i in s["installments"]] == [True, True, True, False]


@pytest.mark.parametrize(
    ("code", "dates"),
    [
        # Hawaii: Jun 20 2026 is a Saturday -> Mon Jun 22 ; Sep 20 2026 is a Sunday -> Mon Sep 21 ; Apr 20 is a Monday ;
        # Jan 20 2027 is a Wednesday.
        ("HI", ["2026-04-20", "2026-06-22", "2026-09-21", "2027-01-20"]),
        # Iowa: Jan 31 2027 is a Sunday -> Mon Feb 1 2027. Apr 30 Thu, Jun 30 Tue, Sep 30 Wed.
        ("IA", ["2026-04-30", "2026-06-30", "2026-09-30", "2027-02-01"]),
        ("DE", ["2026-04-30", "2026-06-15", "2026-09-15", "2027-01-15"]),
        ("VA", ["2026-05-01", "2026-06-15", "2026-09-15", "2027-01-15"]),
        ("NY", ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"]),
    ],
)
def test_state_installment_dates_roll_to_business_days(conn, cfg, accounts, code, dates):
    _se_fixture(conn, accounts, 5_700_000)
    s = tax.estimated_tax(conn, cfg, year=2026, state=code, state_income_tax_rate_pct=5, today=TODAY)["state"]
    assert [i["date"] for i in s["installments"]] == dates
    assert sum(i["amount"]["minor"] for i in s["installments"]) == s["income_tax_estimate"]["minor"]


@pytest.mark.parametrize("code", ["UT", "ID"])
def test_utah_and_idaho_have_no_installments_but_still_estimate(conn, cfg, accounts, code):
    _se_fixture(conn, accounts, 5_700_000)
    e = tax.estimated_tax(conn, cfg, year=2026, state=code, state_income_tax_rate_pct=5, today=TODAY)
    assert e["state"]["installments"] == [] and e["state"]["estimated_required"] is False
    assert e["state"]["no_income_tax"] is False
    # 5% of the taxable base 6,784,272 = 339,213.6 -> 339,214
    assert e["state"]["income_tax_estimate"]["minor"] == 339_214
    assert e["suggested_per_quarter_with_state"][0]["state"] is None


@pytest.mark.parametrize("code", ["AK", "FL", "NV", "NH", "SD", "TN", "TX", "WA", "WY"])
def test_no_income_tax_states_ignore_a_supplied_rate(conn, cfg, accounts, code):
    _se_fixture(conn, accounts, 5_700_000)
    e = tax.estimated_tax(conn, cfg, year=2026, state=code, state_income_tax_rate_pct=9, today=TODAY)
    s = e["state"]
    assert s["no_income_tax"] is True and s["income_tax_estimate"] is None and s["installments"] == []
    assert e["projected_total_with_state"] == e["projected_total"]


def test_federal_due_dates_for_2027_use_the_mlk_holiday():
    # Jan 15 2028 is a Saturday, Jan 17 2028 is Martin Luther King Jr. Day -> Tue Jan 18 2028.
    # Apr 15 2027 Thu, Jun 15 2027 Tue, Sep 15 2027 Wed.
    assert tax.due_dates(2027) == ["2027-04-15", "2027-06-15", "2027-09-15", "2028-01-18"]


@pytest.mark.parametrize("bad", [-1, 20.01, "abc", "NaN", "Infinity", True, "1e2"])
def test_state_rate_bounds(conn, cfg, accounts, bad):
    with pytest.raises(GuardError):
        tax.estimated_tax(conn, cfg, year=2026, state="CA", state_income_tax_rate_pct=bad, today=TODAY)


def test_state_rate_boundaries_accepted(conn, cfg, accounts):
    _se_fixture(conn, accounts, 5_700_000)
    for rate in (0, 20):
        tax.estimated_tax(conn, cfg, year=2026, state="CA", state_income_tax_rate_pct=rate, today=TODAY)


@pytest.mark.xfail(
    strict=True,
    reason="BUG-2: state_income_tax_rate_pct = nan raises ConfigError with the text "
    "'[<class decimal.InvalidOperation>]' instead of the range message (Decimal('nan') <= 20 raises first)",
)
def test_nan_state_rate_in_config_gets_the_range_message(cfg):
    from fiscl.config import ConfigError

    cfg.config_file.write_text("[tax]\nstate_income_tax_rate_pct = nan\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="between 0 and 20"):
        load_config(cfg.home)


# --- 1099 thresholds by payment year ------------------------------------------------------------


def test_nec_misc_threshold_table_by_year():
    assert nec_misc_threshold(2024) == 60_000 and nec_misc_threshold(2025) == 60_000
    assert nec_misc_threshold(2026) == 200_000
    assert nec_misc_threshold(2027) is None


@pytest.mark.parametrize(
    ("year", "below", "at"),
    [(2025, 59_999, 60_000), (2026, 199_999, 200_000)],
)
def test_unreported_payer_threshold_is_at_least(conn, cfg, accounts, year, below, at):
    add_txn(conn, accounts["biz"], f"{year}-02-01", below, "Under Co", category=INCOME)
    add_txn(conn, accounts["biz"], f"{year}-02-02", at, "Exact Co", category=INCOME)
    conn.commit()
    r = tax.reconcile_1099(conn, cfg, tax_year=year, today=TODAY)
    assert [p["merchant"] for p in r["unreported_payers"]] == ["Exact Co"]
    assert r["thresholds"]["nec_misc"]["comparison"] == "at least"


def test_k_threshold_is_strictly_over_20000_in_the_question(conn, cfg):
    def asked(amount):
        tax.record_1099(
            conn,
            cfg,
            [{"action": "add", "tax_year": 2026, "form": "k", "payer": "PayPal", "amount": amount}],
            dry_run=False,
            today=TODAY,
        )
        r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
        conn.execute("DELETE FROM forms_1099")
        return any("20,000 or less" in q for q in r["forms"][0]["questions"])

    assert asked("20000.00") is True  # not "over" 20,000: the small-form question applies
    assert asked("20000.01") is False
    r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    assert r["thresholds"]["k_third_party_network"]["comparison"] == "over"
    assert r["thresholds"]["k_third_party_network"]["transactions"] == 200


def test_reconcile_never_counts_personal_scope_or_transfers(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-02-01", 250_000, "Real Client", category=INCOME)
    add_txn(conn, accounts["checking"], "2026-02-02", 900_000, "Personal Gig")  # personal account
    t = add_txn(conn, accounts["biz"], "2026-02-03", 700_000, "Move Money")
    conn.execute("UPDATE transactions SET transfer_group = 'g1' WHERE id = ?", (t,))
    conn.commit()
    r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    assert [p["merchant"] for p in r["unreported_payers"]] == ["Real Client"]
    assert r["totals"]["business_gross_receipts"]["minor"] == 250_000


def test_reconcile_with_payer_alias_through_match_merchants(conn, cfg, accounts):
    # The bank shows the processor name; the form lists the client. $5,000 reported vs 2,000 + 2,990 = 4,990 -> a
    # $10.00 gap: reported_higher. A payer name that matches nothing is "no_matching_income" without the alias.
    add_txn(conn, accounts["biz"], "2026-02-01", 200_000, "STRIPE TRANSFER ST-AAA", category=INCOME)
    add_txn(conn, accounts["biz"], "2026-03-01", 299_000, "Stripe Transfer ST-BBB", category=INCOME)
    conn.commit()
    plan = lambda mm: tax.record_1099(  # noqa: E731
        conn,
        cfg,
        [
            {
                "action": "add",
                "tax_year": 2026,
                "form": "nec",
                "payer": "Acme LLC",
                "amount": 5000,
                "match_merchants": mm,
            }
        ],
        dry_run=False,
        today=TODAY,
    )
    plan([])
    r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    assert r["forms"][0]["status"] == "no_matching_income"
    conn.execute("DELETE FROM forms_1099")
    plan(["STRIPE TRANSFER ST-AAA", "stripe transfer st-bbb"])
    r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    f = r["forms"][0]
    assert f["matched_income"]["minor"] == 499_000 and f["difference"]["minor"] == 1_000
    assert f["status"] == "reported_higher" and f["questions"]
    assert r["unreported_payers"] == []  # the matched merchants are accounted for, not "unreported"


def test_record_1099_batch_is_atomic(conn, cfg):
    good = {"action": "add", "tax_year": 2026, "form": "nec", "payer": "A", "amount": 100}
    cases = [
        [good, {**good, "payer": "B", "amount": 0}],  # bad amount second
        [good, {"action": "update", "id": 999, "amount": 5}],  # unknown id second
        [good, {**good, "payer": "a"}],  # duplicate key (case) second
        [good, {**good, "payer": "C", "form": "w2"}],  # bad enum
        [good, {**good, "payer": "D", "tax_year": 1999}],
        [good, {**good, "payer": "E", "bogus": 1}],
    ]
    for items in cases:
        with pytest.raises(GuardError):
            tax.record_1099(conn, cfg, items, dry_run=False, today=TODAY)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM forms_1099").fetchone()[0] == 0, items


def test_record_1099_remove_then_add_same_key_in_one_batch(conn, cfg):
    tax.record_1099(
        conn,
        cfg,
        [{"action": "add", "tax_year": 2026, "form": "nec", "payer": "A", "amount": 100}],
        dry_run=False,
        today=TODAY,
    )
    fid = conn.execute("SELECT id FROM forms_1099").fetchone()[0]
    tax.record_1099(
        conn,
        cfg,
        [
            {"action": "remove", "id": fid},
            {"action": "add", "tax_year": 2026, "form": "nec", "payer": "A", "amount": 200},
        ],
        dry_run=False,
        today=TODAY,
    )
    assert [r[0] for r in conn.execute("SELECT amount FROM forms_1099")] == [20_000]


# --- businesses edge cases ----------------------------------------------------------------------


def test_archived_business_with_prior_year_activity_appears_in_that_years_draft(conn, cfg):
    studio, consult = two_biz(conn, cfg)
    add_txn(conn, studio, "2025-03-01", 100_000, "A", category=INCOME)
    add_txn(conn, consult, "2025-03-01", 50_000, "B", category=INCOME)
    conn.commit()
    tax.manage_business(conn, cfg, action="archive", name="Consulting", dry_run=False, today=TODAY)
    conn.commit()
    d25 = tax.schedule_c_draft(conn, cfg, year=2025, today=TODAY)
    assert [b["business"]["name"] for b in d25["businesses"]] == ["Studio", "Consulting"]
    assert d25["combined"]["gross_receipts"]["minor"] == 150_000
    e = tax.estimated_tax(conn, cfg, year=2025, today=TODAY)
    assert e["ytd_net_profit"]["minor"] == 150_000
    d26 = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    assert "businesses" not in d26 and d26["business"]["name"] == "Studio" if "business" in d26 else True
    r = tax.export_schedule_c(conn, cfg, year=2025, today=TODAY, now=NOW)
    assert len(r["files"]) == 4  # csv + md for each of two businesses


def test_transfers_between_two_businesses_accounts_are_excluded(conn, cfg):
    studio, consult = two_biz(conn, cfg)
    add_txn(conn, studio, "2026-03-01", 100_000, "Client", category=INCOME)
    out = add_txn(conn, studio, "2026-03-05", -40_000, "Transfer to Consulting")
    inn = add_txn(conn, consult, "2026-03-05", 40_000, "Transfer from Studio")
    conn.execute("UPDATE transactions SET transfer_group = 'tg1' WHERE id IN (?, ?)", (out, inn))
    conn.commit()
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    by = {b["business"]["name"]: b for b in d["businesses"]}
    assert by["Studio"]["gross_receipts"]["minor"] == 100_000 and by["Studio"]["total_expenses"]["minor"] == 0
    assert by["Consulting"]["gross_receipts"]["minor"] == 0 and by["Consulting"]["net_profit"]["minor"] == 0
    assert d["combined_net_profit"]["minor"] == 100_000


def test_shared_card_mixed_scope_only_business_rows_count(conn, cfg):
    studio, consult = two_biz(conn, cfg)
    card = add_account(conn, "Shared", "credit", scope="mixed")
    a = add_txn(conn, card, "2026-04-01", -10_000, "Zoom", category=SOFTWARE)
    b = add_txn(conn, card, "2026-04-02", -20_000, "Notion", category=SOFTWARE)
    add_txn(conn, card, "2026-04-03", -99_000, "Groceries", category="Groceries")
    conn.commit()
    tax.set_scope(conn, cfg, [{"transaction_ids": [a], "scope": "business", "business": "Consulting"}], False)
    tax.set_scope(conn, cfg, [{"transaction_ids": [b], "scope": "business"}], False)
    conn.commit()
    by = {x["business"]["name"]: x for x in tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["businesses"]}
    assert by["Consulting"]["total_expenses"]["minor"] == 10_000
    assert by["Studio"]["total_expenses"]["minor"] == 20_000  # no explicit business -> account/default (Studio)


# --- export edge cases --------------------------------------------------------------------------


def export(conn, cfg, **kw):
    kw.setdefault("year", 2026)
    return tax.export_schedule_c(conn, cfg, today=TODAY, now=NOW, **kw)


def rows_of(path):
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))


def test_export_with_no_activity_still_produces_valid_files(conn, cfg, accounts):
    r = export(conn, cfg, year=2024, dry_run=False, include_transactions=True)
    assert r["summary"]["net_profit"]["minor"] == 0
    names = sorted(p.name for p in cfg.exports.iterdir())
    assert len(names) == 3
    summary = next(
        p for p in cfg.exports.iterdir() if p.name.endswith("0.csv") or p.suffix == ".csv" and "detail" not in p.name
    )
    body = {row[0]: row for row in rows_of(summary)[1:]}
    assert body["net_profit"][4] == "0.00" and body["gross_receipts"][4] == "0.00"
    detail = next(p for p in cfg.exports.iterdir() if "detail" in p.name)
    assert len(rows_of(detail)) == 1  # header only


def test_export_second_business_collision_leaves_nothing_behind(conn, cfg):
    studio, consult = two_biz(conn, cfg)
    add_txn(conn, studio, "2026-03-01", 100_000, "A", category=INCOME)
    add_txn(conn, consult, "2026-03-01", 100_000, "B", category=INCOME)
    conn.commit()
    cfg.exports.mkdir(mode=0o700, exist_ok=True)
    blocker = cfg.exports / "schedule-c-2026-b2-20261012T093000.md"
    blocker.write_text("mine")
    with pytest.raises(GuardError) as ei:
        export(conn, cfg, dry_run=False)
    assert ei.value.code == "exists"
    assert [p.name for p in cfg.exports.iterdir()] == [blocker.name] and blocker.read_text() == "mine"


@posix
def test_export_refuses_when_exports_is_a_regular_file(conn, cfg, accounts):
    cfg.exports.rmdir()
    cfg.exports.write_text("not a dir")
    with pytest.raises(GuardError) as ei:
        export(conn, cfg, dry_run=False)
    assert ei.value.code == "invalid"


@posix
def test_export_repairs_loose_directory_permissions_and_files_are_private(conn, cfg, accounts):
    os.chmod(cfg.exports, 0o777)
    export(conn, cfg, dry_run=False)
    assert stat.S_IMODE(os.lstat(cfg.exports).st_mode) == 0o700
    assert {stat.S_IMODE(p.stat().st_mode) for p in cfg.exports.iterdir()} == {0o600}


@posix
def test_export_symlink_to_directory_inside_the_data_dir_is_still_refused(conn, cfg, accounts):
    other = cfg.home / "elsewhere"
    other.mkdir()
    cfg.exports.rmdir()
    cfg.exports.symlink_to(other)
    with pytest.raises(GuardError) as ei:
        export(conn, cfg, dry_run=False)
    assert ei.value.code == "invalid" and list(other.iterdir()) == []


@posix
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes")
async def test_export_into_a_read_only_directory_fails_cleanly_over_mcp(conn, cfg, accounts):
    os.chmod(cfg.exports, 0o500)
    try:
        # The directory check repairs the mode to 0700 first, so the write succeeds or fails with a fixed message;
        # either way no absolute path may leak through the tool error.
        async with Client(build_server(cfg)) as c:
            try:
                res = await c.call_tool("export_schedule_c", {"year": 2026, "dry_run": False})
                text = json.dumps(res.structured_content or "") + str(res.content)
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
        assert str(cfg.home) not in text
    finally:
        os.chmod(cfg.exports, 0o700)


HOSTILE = [
    '=HYPERLINK("http://evil","x")',
    "+cmd|' /C calc'!A0",
    "-2+3",
    "@SUM(A1)",
    "=cmd\nsecond line",
    "\t=1+1",
    "  =1+1",
    "＝1+1",
    "a|b | c",
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[click](http://x)",
    "![i](http://x/y.png)",
    "line1\r\nline2\n# Heading",
    "`code` *b* _i_ ~~s~~",
]


def test_hostile_text_in_merchants_categories_and_business_names(conn, cfg):
    studio, consult = two_biz(conn, cfg)
    for i, text in enumerate(HOSTILE):
        add_txn(conn, studio, f"2026-03-{i + 1:02d}", -1_000 - i, text, category=f"Business: {text}")
        add_txn(conn, studio, f"2026-04-{i + 1:02d}", 1_000, text, category=INCOME)
    conn.commit()
    tax.manage_business(conn, cfg, action="rename", name="Consulting", new_name='=SUM(1,1)<b>"x"|', dry_run=False)
    add_txn(conn, consult, "2026-05-01", 5_000, "Client", category=INCOME)
    conn.commit()
    export(conn, cfg, dry_run=False, include_transactions=True)
    cell_start = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")
    for p in cfg.exports.iterdir():
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".csv":
            for row in csv.reader(io.StringIO(text, newline="")):
                for cell in row:
                    if cell and cell[0] in cell_start:
                        # only genuine numbers (amounts like -12.34) may start with a formula character
                        assert re.fullmatch(r"-?\d+(\.\d+)?", cell), (p.name, cell)
        else:
            assert "<script" not in text and "<img" not in text and "<b>" not in text
            assert "](http" not in text.replace("\\](", "")  # no live link syntax
            assert "![" not in text.replace("\\!\\[", "")
            for line in text.splitlines():
                if line.startswith("|"):  # a data cell may not break the table: unescaped pipes only separate cells
                    assert re.sub(r"\\\|", "", line).count("|") == line.count("|") - line.count("\\|")
            # injected text must never start a heading of its own
            assert not re.search(r"^# Heading", text, re.M)


@pytest.mark.xfail(
    strict=True,
    reason="BUG-1: md_text() escapes brackets/parens/pipes but not bare URLs; GFM renderers auto-link http(s):// and "
    "www. text in category and business names, which the spec says must not become links",
)
def test_markdown_bare_urls_are_not_left_clickable(conn, cfg, accounts):
    # Merchants never reach the Markdown (only category and business names do), so use a category name.
    add_txn(conn, accounts["biz"], "2026-03-01", -1_000, "Zoom", category="Business: http://evil.example/x")
    conn.commit()
    export(conn, cfg, dry_run=False)
    md = next(p for p in cfg.exports.iterdir() if p.suffix == ".md").read_text(encoding="utf-8")
    assert not re.search(r"(?<!\\)https?://", md)


# --- resource year validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "year",
    ["2026; DROP", "20266", "٢٠٢٦", "-2026", "+2026", " 2026", "2026 ", "0x7ea", "2026.0", "１９９９", "2101", "1999"],
)
async def test_resource_rejects_malformed_years_without_echo(cfg, conn, year):
    conn.commit()
    async with Client(build_server(cfg)) as c:
        with pytest.raises(Exception) as ei:  # noqa: B017, PT011
            await c.read_resource(f"fiscl://tax/{quote(year, safe='')}")
    # The SDK's own wrapper names the URI the client sent; the server must not add its own detail or any data.
    assert "Error creating resource" in str(ei.value) or "year must be" in str(ei.value)


async def test_resource_accepts_boundary_years(cfg, conn):
    async with Client(build_server(cfg)) as c:
        for y in ("2000", "2026", str(date.today().year + 1)):
            data = json.loads((await c.read_resource(f"fiscl://tax/{y}")).contents[0].text)
            assert data["tax_year"] == int(y)
