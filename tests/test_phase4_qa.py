"""Phase 4 QA pass: independent checks of FX arithmetic, ISO 4217 exponents, QIF dates, Amazon matching, holdings
privacy and the QuickBooks allowlist. The oracles here are written separately from the code under test (Fractions and
Python's round-half-even, a hard-coded ISO table), so they do not share its arithmetic.

Tests marked ``xfail(strict=True)`` pin a bug found in this pass: each fails today and must be flipped to a normal test
when the owning code is fixed (strict means an unexpected pass fails the suite, forcing the marker's removal).
"""

from __future__ import annotations

import json
import random
import re
import socket
import urllib.parse
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from fiscl import analysis, fx
from fiscl.db import add_account
from fiscl.importers.core import import_file
from fiscl.importers.qif import parse_qif
from fiscl.money import CURRENCY_EXPONENT, exponent
from fiscl.sync import base, connections, qbo, registry
from fiscl.sync.base import SyncError
from fiscl.transfers import match_transfers

from .conftest import TODAY, add_txn, write
from .test_amazon import charge, matches, put
from .test_fx import homes, import_eur, put_rate
from .test_qbo import API, CLIENT_ID, CLIENT_SECRET, COMPANY, REALM, REFRESH_1, FakeQbo, herr

# --- ISO 4217 --------------------------------------------------------------------------------------------------------

# Every currency whose ISO 4217 minor unit is not 2, from SIX Group's "list one" (the ISO 4217 maintenance agency's
# published table), file https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/
# lists/list-one.xml, `Pblshd` 2026-09-17, read by QA on 2026-10-12. Funds and metals ("N.A.") carry no exponent.
ISO_ZERO = [
    "BIF",
    "CLP",
    "DJF",
    "GNF",
    "ISK",
    "JPY",
    "KMF",
    "KRW",
    "PYG",
    "RWF",
    "UGX",
    "UYI",
    "VND",
    "VUV",
    "XAF",
    "XOF",
    "XPF",
]
ISO_THREE = ["BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"]
ISO_FOUR = ["CLF", "UYW"]
ISO_EXPONENTS = {**dict.fromkeys(ISO_ZERO, 0), **dict.fromkeys(ISO_THREE, 3), **dict.fromkeys(ISO_FOUR, 4)}


def test_every_exponent_fiscl_declares_matches_iso_4217():
    for code, exp in CURRENCY_EXPONENT.items():
        assert ISO_EXPONENTS.get(code) == exp, code


def test_every_non_two_decimal_iso_currency_is_declared():
    assert {c: exponent(c) for c in ISO_EXPONENTS} == ISO_EXPONENTS


@pytest.mark.parametrize("code", ["XAU", "XAG", "XPT", "XPD", "XDR", "XTS", "XXX", "XBA", "XSU", "XUA"])
def test_currencies_without_a_minor_unit_are_refused_with_a_fixed_message(conn, cfg, code):
    from fiscl.guard import GuardError
    from fiscl.money import CURRENCY_CODE_MSG, is_currency_code

    assert not is_currency_code(code) and is_currency_code("CLF") and is_currency_code("EUR")
    with pytest.raises(GuardError, match="metals, fund units and test codes") as exc:
        fx.check_foreign_currency(code, cfg)
    assert code not in str(exc.value) and CURRENCY_CODE_MSG in str(exc.value)


# --- FX arithmetic against an independent oracle ----------------------------------------------------------------------


def oracle_home(minor: int, cur: str, home: str, rate: str) -> int:
    """Exact rational arithmetic; Python's round() on a Fraction is round-half-to-even."""
    major = Fraction(minor, 10 ** ISO_EXPONENTS.get(cur, 2))
    return round(major * Fraction(rate) * 10 ** ISO_EXPONENTS.get(home, 2))


@pytest.mark.parametrize("home", ["USD", "JPY", "KWD"])
@pytest.mark.parametrize("cur", ["EUR", "JPY", "KWD", "BHD", "JOD", "TND", "OMR", "IQD", "LYD", "KRW", "VND"])
def test_convert_minor_matches_the_oracle_on_random_inputs(cur, home):
    if cur == home:
        pytest.skip("same currency")
    rng = random.Random(f"{cur}{home}")
    for _ in range(150):
        minor = rng.randint(-(10**9), 10**9)
        places = rng.randint(0, 8)
        rate = (
            f"{rng.randint(0, 999)}.{rng.randint(0, 10**places - 1):0{places}d}" if places else str(rng.randint(1, 999))
        )
        if Fraction(rate) == 0:
            rate = "0.5"
        assert fx.convert_minor(minor, cur, home, fx.parse_rate(rate)) == oracle_home(minor, cur, home, rate), (
            minor,
            cur,
            home,
            rate,
        )


@pytest.mark.parametrize(
    ("minor", "rate", "want"),
    [(1, "0.5", 0), (3, "0.5", 2), (5, "0.5", 2), (7, "0.5", 4), (-1, "0.5", 0), (-3, "0.5", -2), (-5, "0.5", -2)],
)
def test_ties_round_half_even_in_both_signs(minor, rate, want):
    assert fx.convert_minor(minor, "EUR", "USD", fx.parse_rate(rate)) == want


def test_jpy_and_kwd_land_on_the_right_scale():
    assert fx.convert_minor(100000, "JPY", "USD", fx.parse_rate("0.0067")) == 670_00  # 1000 yen -> $6.70
    assert fx.convert_minor(1234, "KWD", "USD", fx.parse_rate("3.25")) == 401  # 1.234 KWD * 3.25 = $4.0105 -> 401 cents
    assert fx.convert_minor(-1500, "USD", "KWD", fx.parse_rate("0.307")) == -4605  # $15.00*0.307 = 4.605 KWD


@pytest.mark.parametrize("cur", ["EUR", "JPY", "KWD"])
def test_split_children_sum_exactly_and_stay_within_one_minor_unit(cur, conn, cfg):
    rng = random.Random(cur)
    put_rate(conn, "2026-09-01", cur, "1.0833")
    total_cases = 0
    for i in range(25):
        parent = -rng.randint(100, 9_999_999)
        n = rng.randint(2, 5)
        cuts = sorted(rng.sample(range(1, abs(parent)), n - 1))
        parts = [b - a for a, b in zip([0, *cuts], [*cuts, abs(parent)], strict=True)]
        name = f"Acct{i}"
        import_eur(conn, cfg, [("2026-09-02", parent, f"row{i}")], name=name, currency=cur, digest=f"d{i}")
        tid = conn.execute("SELECT id FROM transactions WHERE raw_description = ?", (f"row{i}",)).fetchone()[0]
        scale = 10 ** ISO_EXPONENTS.get(cur, 2)
        args = [{"amount": float(-Fraction(p, scale)), "category": f"Cat{k}"} for k, p in enumerate(parts)]
        analysis.split_transaction(conn, cfg, transaction_id=tid, parts=args, dry_run=False)
        parent_home = conn.execute("SELECT amount_home FROM transactions WHERE id = ?", (tid,)).fetchone()[0]
        assert parent_home == oracle_home(parent, cur, "USD", "1.0833")
        kids = [r[0] for r in conn.execute("SELECT amount_home FROM splits WHERE transaction_id = ?", (tid,))]
        assert sum(kids) == parent_home
        for p, k in zip(parts, kids, strict=True):
            ideal = Fraction(-p) * parent_home / parent  # this part's exact share of the parent's home amount
            slack = len(parts) if p == max(parts) else 1  # the largest part absorbs the rounding remainder
            assert abs(Fraction(k) - ideal) <= slack, (p, k)
        total_cases += 1
    assert total_cases == 25


@pytest.mark.parametrize("age", [1, 7, 31])
def test_rate_age_boundary_is_inclusive_on_the_last_day_and_exclusive_after(conn, cfg, age):
    cfg.fx_max_rate_age_days = age
    put_rate(conn, "2026-01-01", "EUR", "2")
    last_ok = date(2026, 1, 1).toordinal() + age
    rows = [
        (date.fromordinal(last_ok - 1).isoformat(), -100, "before"),
        (date.fromordinal(last_ok).isoformat(), -100, "edge"),
        (date.fromordinal(last_ok + 1).isoformat(), -100, "after"),
    ]
    import_eur(conn, cfg, rows)
    assert homes(conn) == {"before": -200, "edge": -200, "after": None}


def test_net_worth_applies_the_same_age_boundary_to_balances(conn, cfg):
    acct = add_account(conn, "Euro", "checking", currency="EUR")
    conn.execute(
        "INSERT INTO balances (account_id, as_of, amount, source) VALUES (?, '2026-10-12', 10000, 'manual')", (acct,)
    )
    put_rate(conn, "2026-10-05", "EUR", "1.5")  # exactly 7 days before TODAY
    assert analysis.net_worth(conn, cfg, today=TODAY)["total_assets"]["minor"] == 15000
    conn.execute("DELETE FROM fx_rates")
    put_rate(conn, "2026-10-04", "EUR", "1.5")  # 8 days
    r = analysis.net_worth(conn, cfg, today=TODAY)
    assert r["total_assets"]["minor"] == 0 and r["unconverted_balances"][0]["reason"] == "no_fx_rate"


# --- unconverted rows never leak into any aggregate ------------------------------------------------------------------


def test_unconverted_rows_reach_no_aggregate_and_are_counted(conn, cfg):
    home = add_account(conn, "Cash", "checking")
    sentinel = 77_777_777  # EUR 777,777.77, no rate anywhere
    add_txn(conn, home, "2026-09-02", -2500, "Corner Grocer", "Groceries")
    add_txn(conn, home, "2026-09-05", 300000, "Payroll Inc", "Income")
    import_eur(conn, cfg, [("2026-09-03", -sentinel, "Euro Mart"), ("2026-09-04", sentinel, "Euro Salary")])
    conn.execute(
        "UPDATE transactions SET category_id = (SELECT id FROM categories WHERE name = 'Groceries') "
        "WHERE currency = 'EUR' AND amount < 0"
    )
    conn.commit()
    kw = {"today": TODAY}
    calls = {
        "spending_summary": analysis.spending_summary(conn, cfg, from_="2026-09-01", to="2026-09-30", **kw),
        "by_merchant": analysis.spending_summary(
            conn, cfg, from_="2026-09-01", to="2026-09-30", group_by="merchant", **kw
        ),
        "cash_flow": analysis.cash_flow(conn, cfg, from_="2026-09-01", to="2026-09-30", **kw),
        "category_trend": analysis.category_trend(conn, cfg, category="Groceries", **kw),
        "budget_status": analysis.budget_status(conn, cfg, **kw),
        "suggest_budgets": analysis.suggest_budgets(conn, cfg, **kw),
        "anomalies": analysis.find_anomalies(conn, cfg, **kw),
        "watchdog": analysis.subscription_watchdog(conn, cfg, **kw),
        "recurring": analysis.list_recurring(conn, cfg, **kw),
        "forecast": analysis.forecast_cash(conn, cfg, **kw),
        "merchant_history": analysis.merchant_history(conn, cfg, merchant="Corner Grocer", **kw),
        "refunds": analysis.refund_tracker(conn, cfg, **kw),
    }
    for name, out in calls.items():
        text = json.dumps(out, default=str)
        assert "77777777" not in text and "777,777" not in text, name
    ss = calls["spending_summary"]
    assert ss["total_spent"]["minor"] == 2500
    assert ss["meta"]["unconverted"]["count"] == 2 and ss["meta"]["unconverted"]["currencies"] == ["EUR"]
    cf = calls["cash_flow"]
    assert cf["meta"]["unconverted"]["count"] == 2
    q = analysis.query_transactions(conn, cfg, from_="2026-09-01", to="2026-09-30", **kw)
    assert q["totals"]["net"]["minor"] == 300000 - 2500


def test_unconverted_business_rows_reach_no_tax_output_and_do_not_crash(conn, cfg):
    """Per-row readers of txn_lines (export detail, write-off review) must skip rows whose home amount is NULL."""
    from fiscl import tax

    tax.manage_business(conn, cfg, action="add", name="Consulting", dry_run=False)
    add_account(conn, "Euro biz", "checking", currency="EUR", scope="business")
    add_account(conn, "Euro consulting", "checking", currency="EUR", scope="business")
    tax.set_scope(conn, cfg, [{"account": "Euro consulting", "scope": "business", "business": "Consulting"}], False)
    home = add_account(conn, "Biz", "checking", scope="business")
    add_txn(conn, home, "2026-03-01", 500000, "Client A", category="Income: Business")
    add_txn(conn, home, "2026-03-02", -2500, "Adobe", category="Business: Software & Subscriptions")
    sentinel = 77_777_777
    rows = [("2026-04-03", -sentinel, "Euro Gear"), ("2026-04-04", sentinel, "Euro Client")]
    import_eur(conn, cfg, rows, name="Euro biz", digest="b1")
    import_eur(conn, cfg, rows, name="Euro consulting", digest="b2")
    conn.execute(
        "UPDATE transactions SET category_id = (SELECT id FROM categories WHERE name = 'Business: Supplies') "
        "WHERE currency = 'EUR' AND amount < 0"
    )
    conn.commit()
    kw = {"year": 2026, "today": TODAY}
    calls = {
        "schedule_c_draft": tax.schedule_c_draft(conn, cfg, **kw),
        "schedule_c_draft_biz": tax.schedule_c_draft(conn, cfg, business="Consulting", **kw),
        "review_writeoffs": tax.review_writeoffs(conn, cfg, **kw),
        "review_writeoffs_biz": tax.review_writeoffs(conn, cfg, business="Consulting", **kw),
        "export": tax.export_schedule_c(conn, cfg, include_transactions=True, **kw),
        "list_businesses": tax.list_businesses(conn, cfg, **kw),
        "reconcile_1099": tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY),
    }
    for name, out in calls.items():
        text = json.dumps(out, default=str)
        assert "77777777" not in text and "777,777" not in text, name
        if "meta" in out:
            assert out["meta"]["unconverted"]["count"] == 4, name
    assert calls["export"]["summary"]["gross_receipts"]["minor"] == 500000
    # The detail CSV is written (not just planned) without touching the unconverted rows.
    written = tax.export_schedule_c(conn, cfg, include_transactions=True, dry_run=False, **kw)
    detail = [f for f in written["files"] if f["kind"] == "detail_csv"]
    assert sorted(f["rows"] for f in detail) == [0, 2]  # the two USD rows; no EUR row appears


def test_adding_the_missing_rate_brings_the_rows_in_and_clears_the_counter(conn, cfg):
    import_eur(conn, cfg, [("2026-09-03", -10000, "Euro Mart")])
    conn.execute("UPDATE transactions SET category_id = (SELECT id FROM categories WHERE name = 'Groceries')")
    conn.commit()
    kw = {"from_": "2026-09-01", "to": "2026-09-30", "today": TODAY}
    assert analysis.spending_summary(conn, cfg, **kw)["total_spent"]["minor"] == 0
    analysis.set_fx_rate(conn, cfg, items=[{"date": "2026-09-03", "currency": "EUR", "rate": "1.085"}], dry_run=False)
    r = analysis.spending_summary(conn, cfg, **kw)
    assert r["total_spent"]["minor"] == 10850 and "unconverted" not in r["meta"]


# --- set_fx_rate dry run is an exact preview -------------------------------------------------------------------------


def test_dry_run_is_byte_exact_preview_of_apply(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.1")
    rows = [(f"2026-09-{d:02d}", -1000 - d, f"r{d}") for d in range(1, 29)]
    import_eur(conn, cfg, rows)
    items = [
        {"date": "2026-09-10", "currency": "EUR", "rate": "1.2"},
        {"date": "2026-09-01", "currency": "EUR", "rate": None},
        {"date": "2026-09-20", "currency": "EUR", "rate": "0.9"},
    ]
    before = "\n".join(conn.iterdump())
    dry = analysis.set_fx_rate(conn, cfg, items=items, dry_run=True, today=TODAY)
    assert "\n".join(conn.iterdump()) == before  # nothing at all changed
    applied = analysis.set_fx_rate(conn, cfg, items=items, dry_run=False, today=TODAY)
    conn.commit()
    strip = lambda r: {k: v for k, v in r.items() if k not in ("dry_run", "meta", "note", "message")}  # noqa: E731
    assert [c["affected_transactions"] for c in dry["changes"]] == [
        c["affected_transactions"] for c in applied["changes"]
    ]
    assert (
        dry["transactions_affected"] == applied["transactions_affected"] and strip(dry).keys() == strip(applied).keys()
    )
    # and the applied state equals an independent recomputation from the stored rates
    stored = {r["rate_date"]: r["rate"] for r in conn.execute("SELECT rate_date, rate FROM fx_rates")}
    assert set(stored) == {"2026-09-10", "2026-09-20"}
    for r in conn.execute("SELECT posted_date, amount, amount_home FROM transactions"):
        usable = [d for d in stored if d <= r["posted_date"]]
        if not usable:
            want = None
        else:
            d = max(usable)
            age = (date.fromisoformat(r["posted_date"]) - date.fromisoformat(d)).days
            want = oracle_home(r["amount"], "EUR", "USD", stored[d]) if age <= 7 else None
        assert r["amount_home"] == want, r["posted_date"]


def test_fx_refresh_after_manual_recompute_changes_nothing(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.0833")
    import_eur(conn, cfg, [("2026-09-02", -1000, "a"), ("2026-09-03", -3333, "b"), ("2026-09-04", 5, "c")])
    assert fx.refresh_home_amounts(conn, cfg) == []  # idempotent: the importer already materialized the same values


# --- transfers between accounts in different currencies --------------------------------------------------------------


def test_equal_native_amounts_in_different_currencies_are_not_paired_as_a_transfer(conn, cfg):
    add_account(conn, "US", "checking")
    import_eur(conn, cfg, [("2026-09-01", -10000, "Transfer")], name="US", currency="USD", digest="u")
    import_eur(conn, cfg, [("2026-09-01", 10000, "Transfer")], name="Euro", currency="EUR", digest="e")
    assert match_transfers(conn, cfg) == 0


def test_same_currency_foreign_transfer_still_pairs(conn, cfg):
    import_eur(conn, cfg, [("2026-09-01", -10000, "Transfer")], name="E1", digest="1")
    import_eur(conn, cfg, [("2026-09-01", 10000, "Transfer")], name="E2", digest="2")
    assert match_transfers(conn, cfg) == 1


def test_the_limitation_is_documented_in_decisions():
    text = (Path(__file__).resolve().parent.parent / "docs" / "decisions" / "DECISIONS.md").read_text()
    assert "Transfers between accounts in different currencies won't auto-match" in text


# --- QIF -------------------------------------------------------------------------------------------------------------


def qif_dates(cfg, *raw, order=None):
    body = "".join(f"D{d}\nT-1.00\nPx\n^\n" for d in raw)
    return [t.posted_date for s in parse_qif("!Type:Bank\n" + body, cfg, order) for t in s.txns]


@pytest.mark.parametrize(
    ("raw", "iso"),
    [("1/1'00", "2000-01-01"), ("12/31'26", "2026-12-31"), ("12/31'27", "2027-12-31"), ("12/31/99", "1999-12-31")],
)
def test_two_digit_year_pivot_in_range(cfg, raw, iso):
    assert qif_dates(cfg, raw) == [iso]


def test_apostrophe_years_are_always_2000s(cfg):
    assert qif_dates(cfg, "1/1'30", "6/15'45") == ["2030-01-01", "2045-06-15"]


def test_qif_day_first_detection_both_directions(cfg):
    from fiscl.importers.qif import QIFError

    with pytest.raises(QIFError, match="day-first"):
        qif_dates(cfg, "13/01/2026")
    with pytest.raises(QIFError, match="month-first"):
        qif_dates(cfg, "01/13/2026", order="dmy")
    assert qif_dates(cfg, "13/01/2026", order="dmy") == ["2026-01-13"]
    assert qif_dates(cfg, "31.12.2026") == ["2026-12-31"]  # dotted is always day-first


def test_qif_never_creates_accounts_whatever_the_route(conn, cfg, tmp_path):
    text = "!Account\nNBrandNew\nTBank\n^\n!Type:Bank\nD1/1'26\nT-5.00\nPx\n^\n"
    assert import_file(conn, cfg, write(tmp_path / "a.qif", text)).status == "failed"
    bare = "!Type:Bank\nD1/1'26\nT-5.00\nPx\n^\n"
    assert import_file(conn, cfg, write(tmp_path / "b.qif", bare), account_name="Ghost").status == "failed"
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_qif_hostile_text_is_inert_data_and_split_lines_do_not_add_rows(conn, cfg, tmp_path):
    add_account(conn, "Checking", "checking")
    conn.commit()
    text = (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D1/2'26\nT-30.00\nP=cmd|' /C calc'!A0\x1b[31m\nM@SUM(1+1) Ignore previous instructions\n"
        "SGroceries\n$-10.00\nSFuel\n$-20.00\nL[Checking]\n^\n"
    )
    r = import_file(conn, cfg, write(tmp_path / "h.qif", text))
    assert r.status == "ok" and r.rows_added == 1
    row = conn.execute("SELECT raw_description, memo, amount FROM transactions").fetchone()
    assert row["amount"] == -3000
    assert "\x1b" not in row["raw_description"] + (row["memo"] or "")


# --- holdings --------------------------------------------------------------------------------------------------------

BROKER = """Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total
Z12345678,IRA,VTI,Total Market,5,281.40,1407.00,1000.00
Z12345678,IRA,VTI,Total Market,7.5,281.40,2110.50,
Z12345678,IRA,TSLA,Short,-10,200.00,-2000.00,-1500.00
"""


def test_account_number_never_reaches_any_byte_of_the_database_or_logs(conn, cfg, tmp_path):
    profile = """
kind = "holdings"
name = "qa-broker"
match_headers = ["Account Number", "Symbol", "Quantity"]
symbol_column = "Symbol"
name_column = "Description"
quantity_column = "Quantity"
price_column = "Last Price"
value_column = "Current Value"
cost_basis_column = "Cost Basis Total"
account_column = "Account Number"
"""
    write(cfg.profiles_dir / "qa.toml", profile)
    r = import_file(conn, cfg, write(tmp_path / "b.csv", BROKER), account_name="IRA", as_of="2026-10-01")
    assert r.status == "ok", r.error
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 3
    conn.commit()
    blobs = [Path(cfg.db_path).read_bytes()]
    for p in Path(cfg.home).rglob("*"):
        if p.is_file() and p.name != Path(cfg.db_path).name:
            blobs.append(p.read_bytes())
    blobs.append(("\n".join(conn.iterdump()) + json.dumps(r.as_dict(), default=str)).encode())
    for blob in blobs:
        assert b"12345678" not in blob and b"Z1234" not in blob


def test_lots_sum_but_basis_is_only_known_when_every_lot_has_one(conn, cfg, tmp_path):
    r = import_file(
        conn,
        cfg,
        write(
            tmp_path / "h.csv",
            "Symbol,Description,Quantity,Price,Market Value,Cost Basis\n"
            "VTI,ETF,5,281.40,1407.00,1000.00\nVTI,ETF,7.5,281.40,2110.50,\n"
            "TSLA,Short,-10,200.00,-2000.00,-1500.00\n"
            "SOL,Sol,0.123456789012345678,0.00001234,,\n",
        ),
        account_name="Brokerage",
        as_of="2026-10-01",
    )
    assert r.status == "ok", r.error
    out = analysis.holdings(conn, cfg, today=TODAY)
    by = {p["symbol"]: p for p in out["positions"]}
    assert by["VTI"]["quantity"] == "12.5" and by["VTI"]["market_value"]["minor"] == 351750
    assert by["VTI"]["cost_basis"] is None and by["VTI"]["unrealized_gain"] is None
    assert by["TSLA"]["market_value"]["minor"] == -200000 and by["TSLA"]["unrealized_gain"]["minor"] == -50000
    assert by["SOL"]["quantity"] == "0.123456789012345678" and by["SOL"]["market_value"]["minor"] == 0
    assert out["totals"]["market_value"]["minor"] == 351750 - 200000 + 0
    assert out["totals"]["positions_without_basis"] == 2


# --- Amazon ----------------------------------------------------------------------------------------------------------


def test_one_item_is_never_proposed_for_two_charges(conn, cfg, accounts):
    """A -20.00 charge matches shipment S1 and a -30.00 charge matches the whole order that contains S1."""
    put(conn, "QA-1", "2026-09-08", "2026-09-10", "A", 1200)
    put(conn, "QA-1", "2026-09-08", "2026-09-10", "B", 800)
    put(conn, "QA-1", "2026-09-08", "2026-09-11", "C", 1000)
    charge(conn, accounts, "2026-09-11", -2000)
    charge(conn, accounts, "2026-09-12", -3000)
    conn.commit()
    r = matches(conn, cfg)
    claimed = [i["name"] for p in r["proposals"] for i in p["items"]]
    assert len(claimed) == len(set(claimed)), claimed
    # Colliding charges are all ambiguous (none wins), so the user decides which charge paid for which items.
    assert r["proposals"] == [] and len(r["ambiguous"]) == 2


def test_reexport_after_shipping_does_not_duplicate_items(conn, cfg, tmp_path):
    head = "Order ID,Order Date,Ship Date,Product Name,Quantity,Total Owed,Currency\n"
    first = write(tmp_path / "a.csv", head + "111-1,2026-09-01,Not Available,Widget,1,10.00,USD\n")
    later = write(tmp_path / "b.csv", head + "111-1,2026-09-01,2026-09-02,Widget,1,10.00,USD\n")
    import_file(conn, cfg, first)
    r = import_file(conn, cfg, later)
    assert r.rows_added == 0
    rows = conn.execute("SELECT ship_date FROM amazon_items").fetchall()
    assert [x["ship_date"] for x in rows] == ["2026-09-02"]  # shipment recorded in place


def test_amazon_refund_rows_and_positive_card_credits_are_never_proposed(conn, cfg, accounts, tmp_path):
    head = "Order ID,Order Date,Ship Date,Product Name,Quantity,Total Owed,Currency\n"
    f = write(tmp_path / "r.csv", head + "111-9,2026-09-01,2026-09-02,Widget,1,-10.00,USD\n")
    r = import_file(conn, cfg, f)
    assert r.rows_added == 0 and r.warnings == {"rows_without_charge": 1}
    charge(conn, accounts, "2026-09-03", 1000)  # a refund posts as a credit
    assert matches(conn, cfg)["proposals"] == []


def test_amazon_same_shipment_twice_in_two_orders_is_ambiguous_not_double_proposed(conn, cfg, accounts):
    for oid in ("QA-A", "QA-B"):
        put(conn, oid, "2026-09-01", "2026-09-02", f"x{oid}", 1500)
    charge(conn, accounts, "2026-09-03", -1500)
    r = matches(conn, cfg)
    assert r["proposals"] == [] and len(r["ambiguous"]) == 1


# --- QuickBooks ------------------------------------------------------------------------------------------------------

GOOD_Q = "SELECT * FROM Invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 1000"
STAMPED = qbo.build_query("Payment", "2026-01-01T00:00:00-08:00", 1, 50)


@pytest.fixture
def no_sockets(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def api_url(text, extra="&minorversion=75", path="query", key="query", raw=False):
    q = text if raw else urllib.parse.quote(text)
    return f"https://{API}/v3/company/1/{path}?{key}={q}{extra}"


HOSTILE_QUERIES = [
    "SELECT * FROM Invoice; DELETE FROM Invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
    GOOD_Q + "; DELETE",
    GOOD_Q + " --",
    GOOD_Q + " /* x */",
    GOOD_Q + "\n",
    GOOD_Q + "\r\nDELETE",
    GOOD_Q + "\x00",
    " " + GOOD_Q,
    GOOD_Q.lower(),
    GOOD_Q.replace("SELECT", "select"),
    GOOD_Q.replace("SELECT", "SeLeCt"),
    GOOD_Q.replace("SELECT", "ＳＥＬＥＣＴ"),  # fullwidth
    GOOD_Q.replace("Invoice", "Invoicе"),  # Cyrillic e
    GOOD_Q.replace("Invoice", "Invoice WHERE Id = '1'"),
    GOOD_Q.replace("Invoice", "Bill"),
    GOOD_Q.replace("Invoice", "Invoice, Customer"),
    GOOD_Q.replace("*", "Id"),
    GOOD_Q.replace("*", "COUNT(*)"),
    GOOD_Q.replace("STARTPOSITION 1", "STARTPOSITION ١"),  # Arabic-Indic digit
    GOOD_Q.replace("STARTPOSITION 1", "STARTPOSITION 1 STARTPOSITION 2"),
    GOOD_Q.replace("STARTPOSITION 1", "STARTPOSITION -1"),
    GOOD_Q.replace("STARTPOSITION 1", "STARTPOSITION 12345678"),
    GOOD_Q.replace("MAXRESULTS 1000", "MAXRESULTS 10000"),
    GOOD_Q.replace(" ", "\t"),
    GOOD_Q.replace(" ", "  "),
    GOOD_Q.replace(" ", "\u00a0"),
    GOOD_Q.replace("ORDERBY Id", "ORDER BY Id"),
    GOOD_Q.replace("ORDERBY Id", "ORDERBY Id DESC"),
    STAMPED.replace("2026-01-01", "2026-01-01' OR '1'='1"),
    STAMPED.replace("T00:00:00-08:00", "T00:00:00-08:00' ORDERBY Id --"),
    STAMPED.replace(">=", "<="),
    STAMPED.replace("MetaData.LastUpdatedTime", "MetaData.CreateTime"),
    "",
    "A" * 100_000,
]


@pytest.mark.parametrize("text", HOSTILE_QUERIES)
def test_hostile_query_text_is_refused_before_any_socket(no_sockets, text):
    with pytest.raises(SyncError):
        base.request(qbo.policy_for("api"), "GET", api_url(text))


@pytest.mark.parametrize(
    "url",
    [
        # encoded payloads that decode to a refused statement
        api_url(urllib.parse.quote(GOOD_Q + "; DELETE", safe=""), raw=True),
        api_url(urllib.parse.quote(urllib.parse.quote(GOOD_Q), safe=""), raw=True),  # double-encoded
        api_url("%53ELECT%20*%20FROM%20Invoice%3B%20DELETE", raw=True),
        api_url(urllib.parse.quote(GOOD_Q) + "%00", raw=True),
        api_url(urllib.parse.quote(GOOD_Q) + "%FF", raw=True),  # invalid UTF-8 byte
        # parameter smuggling
        api_url(GOOD_Q, "&minorversion=75&operation=delete"),
        api_url(GOOD_Q, "&minorversion=75&query=" + urllib.parse.quote("SELECT * FROM Bill")),
        api_url(GOOD_Q, "&minorversion=75;operation=void"),
        api_url(GOOD_Q, "&minorversion=76"),
        api_url(GOOD_Q, ""),
        api_url(GOOD_Q, "&minorversion=75&minorversion=75"),
        api_url(GOOD_Q, "&minorversion=%37%35%0a"),
        api_url(GOOD_Q, "&minorversion=75&"),
        api_url(GOOD_Q, "&minorversion=75#x"),
        # wrong key, wrong path shapes
        api_url(GOOD_Q, key="q"),
        api_url(GOOD_Q, path="query/"),
        api_url(GOOD_Q, path="query;x=1"),
        api_url(GOOD_Q, path="Query"),
        api_url(GOOD_Q, path="query%2f..%2finvoice"),
        api_url(GOOD_Q, path="invoice"),
        api_url(GOOD_Q, path="../../v3/company/2/query"),
        # host / scheme / port / userinfo tricks
        api_url(GOOD_Q).replace(API, "quickbooks.api.intuit.com"),
        api_url(GOOD_Q).replace(API, API + "."),
        api_url(GOOD_Q).replace(API, "evil.example\\@" + API),
        api_url(GOOD_Q).replace(API, API + "@evil.example"),
        api_url(GOOD_Q).replace(API, "sandbox-quickbooks.api.intuit.com.evil.example"),
        api_url(GOOD_Q).replace("https:", "http:"),
        api_url(GOOD_Q).replace("https:", "HTTP:"),
        api_url(GOOD_Q).replace("https:", "ftp:"),
        api_url(GOOD_Q).replace(API, API + ":80"),
    ],
)
def test_encoded_and_smuggled_requests_are_refused_before_any_socket(no_sockets, url):
    with pytest.raises(SyncError):
        base.request(qbo.policy_for("api"), "GET", url)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT", ""])
def test_every_non_get_method_is_refused_on_every_api_route(no_sockets, method):
    for url in (
        api_url(GOOD_Q),
        f"https://{API}/v3/company/1/companyinfo/1?minorversion=75",
        f"https://{API}/v3/company/1/reports/ProfitAndLoss?minorversion=75&start_date=2026-01-01"
        "&end_date=2026-12-31&accounting_method=Cash",
    ):
        with pytest.raises(SyncError):
            base.request(qbo.policy_for("api"), method, url)


def test_fuzzed_single_character_edits_never_widen_what_is_accepted(no_sockets):
    """Mutate good queries one character at a time; anything the policy still accepts must be a plain ASCII
    SELECT of the pinned shape (no statement separator, comment, or write verb can survive)."""
    rng = random.Random(20261012)
    shape = re.compile(
        r"SELECT \* FROM (Invoice|Customer|Payment)"
        r"( WHERE MetaData\.LastUpdatedTime >= '\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d')?"
        r" ORDERBY Id STARTPOSITION \d{1,7} MAXRESULTS \d{1,4}",
        re.ASCII,
    )
    alphabet = list(";'\"-/*\n\t\x00 %&=#+\\()") + ["ｓ", "е", "١", "İ", "ſ", "K"]
    accepted = 0
    for base_text in (GOOD_Q, STAMPED):
        for _ in range(1500):
            chars = list(base_text)
            op = rng.choice(["sub", "ins", "del"])
            i = rng.randrange(len(chars))
            if op == "sub":
                chars[i] = rng.choice(alphabet)
            elif op == "ins":
                chars.insert(i, rng.choice(alphabet))
            else:
                del chars[i]
            text = "".join(chars)
            try:
                qbo.policy_for("api").check("GET", api_url(text))
            except SyncError:
                continue
            accepted += 1
            assert shape.fullmatch(text), text
            assert not re.search(r"[;\n\r\x00]|--|/\*|\b(DELETE|UPDATE|INSERT|DROP)\b", text, re.I)
    assert accepted > 0


def test_production_is_refused_by_every_entry_point(cfg, conn, keychain, no_sockets):
    from fiscl.sync import connect

    cfg.qbo_environment = "production"
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    with pytest.raises(SyncError, match="sandbox"):
        connect.connect_qbo(cfg, conn, open_browser=lambda url: pytest.fail("browser opened"))
    with pytest.raises(SyncError, match="sandbox"):
        qbo.QboConnector(cfg, [qbo.QboCompany("x")]).sync_records(conn, TODAY)
    assert qbo.policy_for("api").hosts == ("sandbox-quickbooks.api.intuit.com",)  # no production API host exists


def _connected(cfg, conn, keychain):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    ref = connections.item_ref_hash("qbo", REALM)
    keychain.set_password("fiscl", base.secret_name("qbo", ref), REFRESH_1)
    keychain.set_password("fiscl", qbo.realm_secret_name(ref), REALM)
    connections.add_connection(conn, "qbo", COMPANY, ref)
    conn.commit()
    return ref


def test_forget_works_even_if_the_stored_app_keys_are_gone(cfg, conn, keychain):
    """Forgetting must not depend on the client id/secret still being present: the user may have wiped part of the
    keychain, and the refresh token must still be removable (revoke is then best effort)."""
    ref = _connected(cfg, conn, keychain)
    qbo.forget_app_credentials()
    try:
        registry.forget(conn, cfg, "qbo", opener=FakeQbo())
    except SyncError:
        pass
    leftovers = [k for k in keychain.items if ref in k[1]]
    assert not leftovers and connections.list_rows(conn, "qbo") == []


def test_forget_without_app_keys_reports_the_skipped_revoke_and_calls_nothing(cfg, conn, keychain, capsys):
    _connected(cfg, conn, keychain)
    qbo.forget_app_credentials()
    api = FakeQbo()
    notices: list[str] = []
    assert registry.forget(conn, cfg, "qbo", opener=api, notices=notices) == 1
    assert notices == [registry.REVOKE_SKIPPED] and api.calls == []
    # the CLI prints the fixed notice on stderr
    _connected(cfg, conn, keychain)
    qbo.forget_app_credentials()
    from fiscl import cli

    assert cli.main(["--home", str(cfg.home), "connect", "forget", "qbo"]) == 0
    captured = capsys.readouterr()
    assert registry.REVOKE_SKIPPED in captured.err and registry.REVOKE_SKIPPED not in captured.out


def test_a_5xx_or_network_failure_on_revoke_deletes_nothing(cfg, conn, keychain):
    ref = _connected(cfg, conn, keychain)
    for status in (500, 502, 503):  # (429 is left out: forget() waits out Retry-After with the real clock)
        api = FakeQbo(fail={("POST", "/v2/oauth2/tokens/revoke"): herr(status)})
        with pytest.raises(SyncError):
            registry.forget(conn, cfg, "qbo", opener=api)
        assert keychain.get_password("fiscl", base.secret_name("qbo", ref)) == REFRESH_1
        assert len(connections.list_rows(conn, "qbo")) == 1


# --- imports into a foreign-currency account use the account's currency ----------------------------------------------


def _ofx(curdef: str, acct: str = "5550001234") -> str:
    return (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Test Bank</FI></SONRS>"
        f"</SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>{curdef}"
        f"<BANKACCTFROM><BANKID>1<ACCTID>{acct}<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST>"
        "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260903<TRNAMT>-10.00<FITID>A1<NAME>Cafe</STMTTRN>"
        "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    )


def _profile(cfg, extra: str) -> None:
    cfg.profiles_dir.mkdir(exist_ok=True)
    (cfg.profiles_dir / "p.toml").write_text(
        'name = "p"\nmatch_headers = ["Date", "Description", "Amount"]\ndate_column = "Date"\n'
        'description_column = "Description"\namount_column = "Amount"\ndate_format = "%Y-%m-%d"\n' + extra,
        encoding="utf-8",
    )


def test_qif_into_a_foreign_account_is_that_currency_and_converts(conn, cfg, tmp_path):
    add_account(conn, "Euro", "checking", currency="EUR")
    put_rate(conn, "2026-09-01", "EUR", "1.1")
    text = "!Type:Bank\nD9/3'26\nT-10.00\nPCafe\n^\n"
    r = import_file(conn, cfg, write(tmp_path / "e.qif", text), account_name="Euro")
    assert r.status == "ok" and r.rows_added == 1
    row = conn.execute("SELECT currency, amount, amount_home FROM transactions").fetchone()
    assert (row["currency"], row["amount"], row["amount_home"]) == ("EUR", -1000, -1100)


def test_qif_into_a_foreign_account_without_a_rate_is_unconverted_not_home(conn, cfg, tmp_path):
    add_account(conn, "Euro", "checking", currency="EUR")
    text = "!Type:Bank\nD9/3'26\nT-10.00\nPCafe\n^\n"
    import_file(conn, cfg, write(tmp_path / "e.qif", text), account_name="Euro")
    row = conn.execute("SELECT currency, amount_home FROM transactions").fetchone()
    assert row["currency"] == "EUR" and row["amount_home"] is None


def test_qif_into_an_account_with_other_decimals_is_refused(conn, cfg, tmp_path):
    add_account(conn, "Yen", "checking", currency="JPY")
    text = "!Type:Bank\nD9/3'26\nT-1000\nPCafe\n^\n"
    r = import_file(conn, cfg, write(tmp_path / "y.qif", text), account_name="Yen")
    assert r.status == "failed" and "decimal places" in r.error
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_csv_without_profile_currency_takes_the_accounts_currency(conn, cfg, tmp_path):
    add_account(conn, "Euro", "checking", currency="EUR")
    put_rate(conn, "2026-09-01", "EUR", "2")
    _profile(cfg, "")
    csv_text = "Date,Description,Amount\n2026-09-03,Cafe,-10.00\n"
    r = import_file(conn, cfg, write(tmp_path / "e.csv", csv_text), account_name="Euro")
    assert r.status == "ok", r.error
    row = conn.execute("SELECT currency, amount_home FROM transactions").fetchone()
    assert (row["currency"], row["amount_home"]) == ("EUR", -2000)


def test_ofx_curdef_must_match_the_account_currency(conn, cfg, tmp_path):
    add_account(conn, "Euro", "checking", currency="EUR")
    r = import_file(conn, cfg, write(tmp_path / "u.ofx", _ofx("USD")), account_name="Euro")
    assert r.status == "failed" and "currency" in r.error
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    ok = import_file(conn, cfg, write(tmp_path / "e.ofx", _ofx("EUR")), account_name="Euro")
    assert ok.status == "ok" and conn.execute("SELECT currency FROM transactions").fetchone()[0] == "EUR"


def test_a_csv_profile_currency_that_differs_from_the_account_is_refused(conn, cfg, tmp_path):
    add_account(conn, "Checking", "checking")
    _profile(cfg, 'currency = "EUR"\n')
    csv_text = "Date,Description,Amount\n2026-09-03,Cafe,-10.00\n"
    r = import_file(conn, cfg, write(tmp_path / "e.csv", csv_text), account_name="Checking")
    assert r.status == "failed" and "currency" in r.error


@pytest.mark.parametrize("code", ["XAU", "XTS", "QQ", "usd1"])
def test_ofx_curdef_that_is_not_a_supported_currency_code_fails_at_parse(code):
    from fiscl.importers.ofx import OFXError, parse_ofx
    from fiscl.money import CURRENCY_CODE_MSG

    with pytest.raises(OFXError) as exc:
        parse_ofx(_ofx(code))
    assert CURRENCY_CODE_MSG in str(exc.value) and code not in str(exc.value)
