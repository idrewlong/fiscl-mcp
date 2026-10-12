"""Multi-currency: exponents, rate rules, amount_home materialization, set_fx_rate, list_fx_rates, conversions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal

import pytest

from fiscl import analysis, fx
from fiscl.db import MIGRATIONS, add_account, migrate
from fiscl.demo import load_demo
from fiscl.guard import GuardError
from fiscl.importers.core import import_statements
from fiscl.importers.types import ParsedStatement, ParsedTxn
from fiscl.money import CURRENCY_EXPONENT, amount, exponent, fmt, parse_amount, to_minor

from .conftest import TODAY, add_txn


def eur_account(conn, name="Euro"):
    return add_account(conn, name, "checking", currency="EUR")


def import_eur(conn, cfg, rows, name="Euro", currency="EUR", digest="d1"):
    """rows: (date, minor, description). Imports into account `name` (created if missing)."""
    if conn.execute("SELECT 1 FROM accounts WHERE name = ?", (name,)).fetchone() is None:
        add_account(conn, name, "checking", currency=currency)
    stmt = ParsedStatement(
        format="csv", txns=[ParsedTxn(d, a, desc) for d, a, desc in rows], currency=currency, balances=[]
    )
    r = import_statements(conn, cfg, [stmt], "f.csv", digest, account_name=name, post_process=False)
    conn.commit()
    assert r.status == "ok", r.error
    return r


def put_rate(conn, day, cur, rate, source="manual"):
    conn.execute(
        "INSERT OR REPLACE INTO fx_rates (rate_date, currency, rate, source) VALUES (?, ?, ?, ?)",
        (day, cur, rate, source),
    )
    conn.commit()


def homes(conn):
    rows = conn.execute("SELECT raw_description, amount_home FROM transactions")
    return {r["raw_description"]: r["amount_home"] for r in rows}


def rate_item(day="2026-09-01", cur="EUR", rate="1.1"):
    return {"date": day, "currency": cur, "rate": rate}


def set_balance(conn, account_id, minor):
    conn.execute(
        "INSERT INTO balances (account_id, as_of, amount, source) VALUES (?, '2026-10-10', ?, 'manual')",
        (account_id, minor),
    )
    conn.commit()


def set_rate(conn, cfg, item, apply=False):
    return analysis.set_fx_rate(conn, cfg, items=[item], dry_run=not apply, today=TODAY)


def split_in_two(conn, cfg, tid, first, second):
    parts = [{"amount": first, "category": "Groceries"}, {"amount": second, "category": "Dining"}]
    analysis.split_transaction(conn, cfg, transaction_id=tid, parts=parts, dry_run=False)


# --- exponents and money ---------------------------------------------------------------------------------------------


def test_currency_exponents():
    assert exponent("USD") == 2 and exponent("eur") == 2
    assert exponent("JPY") == 0 and exponent("KRW") == 0 and exponent("VND") == 0 and exponent("CLP") == 0
    assert exponent("KWD") == 3 and exponent("BHD") == 3 and exponent("OMR") == 3 and exponent("JOD") == 3
    assert CURRENCY_EXPONENT["TND"] == 3 and CURRENCY_EXPONENT["IQD"] == 3 and CURRENCY_EXPONENT["LYD"] == 3


def test_three_decimal_parse_and_format():
    assert parse_amount("1.234", "KWD") == 1234
    assert to_minor("2.5", "KWD") == 2500
    assert fmt(1234, "KWD") == "KWD 1.234"
    assert fmt(-5, "KWD") == "-KWD 0.005"
    assert fmt(1500, "JPY") == "¥1,500"
    assert amount(1234, "KWD") == {"minor": 1234, "display": "KWD 1.234"}


# --- rate validation -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("good", ["1.085", 0.5, 2, "0.0067", "1000000", "123456.789012"])
def test_parse_rate_accepts(good):
    assert fx.parse_rate(good) > 0


BAD_RATES = ["0", "-1", "abc", "NaN", "Infinity", "1000000.01", "1.2345678901234", True, None, "", "1e400"]


@pytest.mark.parametrize("bad", BAD_RATES)
def test_parse_rate_rejects(bad):
    with pytest.raises(GuardError) as exc:
        fx.parse_rate(bad)
    assert exc.value.code == "invalid"


# --- conversion math -------------------------------------------------------------------------------------------------


def test_convert_minor_half_even_per_row():
    d = Decimal
    assert fx.convert_minor(10000, "EUR", "USD", d("1.0850")) == 10850
    assert fx.convert_minor(1, "EUR", "USD", d("0.5")) == 0  # 0.5 -> even 0
    assert fx.convert_minor(3, "EUR", "USD", d("0.5")) == 2  # 1.5 -> even 2
    assert fx.convert_minor(5, "EUR", "USD", d("0.5")) == 2  # 2.5 -> even 2
    assert fx.convert_minor(-3, "EUR", "USD", d("0.5")) == -2


def test_convert_minor_zero_and_three_decimal_currencies():
    d = Decimal
    # 1000 JPY at 0.0067 USD = 6.70 USD
    assert fx.convert_minor(1000, "JPY", "USD", d("0.0067")) == 670
    # 1.500 KWD at 3.25 USD = 4.875 -> 487.5 cents -> 488
    assert fx.convert_minor(1500, "KWD", "USD", d("3.25")) == 488
    # home currency with other exponents: 25.00 USD at 0.3 KWD/USD = 7.500 KWD; 10.00 USD at 150 JPY = 1500 JPY
    assert fx.convert_minor(2500, "USD", "KWD", d("0.3")) == 7500
    assert fx.convert_minor(1000, "USD", "JPY", d("150")) == 1500


def test_split_homes_sum_exactly_and_remainder_to_largest():
    parts = fx.split_homes([3333, 3333, 3334], 10000, 9999)
    assert sum(parts) == 9999
    parts = fx.split_homes([-500, -250, -250], -1000, -1083)
    assert sum(parts) == -1083
    # 541.5 -> 542 (half-even), 270.75 -> 271, 270.75 -> 271: rounded sum 1084, so the largest child absorbs -1
    assert parts == [-541, -271, -271]
    assert fx.split_homes([-500, -500], -1000, -1000) == [-500, -500]


def test_split_homes_unconverted_parent():
    assert fx.split_homes([-500, -500], -1000, None) == [None, None]


# --- rate lookup window ----------------------------------------------------------------------------------------------


def test_rate_within_seven_days_converts_and_eight_does_not(conn, cfg):
    put_rate(conn, "2026-09-10", "EUR", "1.1")
    import_eur(
        conn,
        cfg,
        [("2026-09-10", -1000, "same day"), ("2026-09-17", -1000, "seven days"), ("2026-09-18", -1000, "eight days")],
    )
    h = homes(conn)
    assert h["same day"] == -1100 and h["seven days"] == -1100 and h["eight days"] is None


def test_latest_rate_on_or_before_wins_and_future_rates_ignored(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.0")
    put_rate(conn, "2026-09-05", "EUR", "2.0")
    put_rate(conn, "2026-09-09", "EUR", "3.0")
    import_eur(conn, cfg, [("2026-09-08", -100, "a"), ("2026-09-09", -100, "b"), ("2026-08-31", -100, "c")])
    assert homes(conn) == {"a": -200, "b": -300, "c": None}


def test_max_age_is_configurable(conn, cfg):
    cfg.fx_max_rate_age_days = 31
    put_rate(conn, "2026-09-01", "EUR", "1.0")
    import_eur(conn, cfg, [("2026-09-30", -100, "late")])
    assert homes(conn)["late"] == -100


# --- home rows and the view ------------------------------------------------------------------------------------------


def test_home_rows_get_amount_home_even_from_raw_inserts(conn, accounts):
    tid = add_txn(conn, accounts["checking"], "2026-09-01", -1234, "Raw insert")
    conn.execute("INSERT INTO splits (transaction_id, amount) VALUES (?, ?)", (tid, -1000))
    conn.execute("INSERT INTO splits (transaction_id, amount) VALUES (?, ?)", (tid, -234))
    assert conn.execute("SELECT amount_home FROM transactions WHERE id = ?", (tid,)).fetchone()[0] == -1234
    assert [r[0] for r in conn.execute("SELECT amount_home FROM splits ORDER BY id")] == [-1000, -234]


def test_txn_lines_amount_is_home_and_exposes_original(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.1")
    import_eur(conn, cfg, [("2026-09-01", -1000, "x")])
    row = conn.execute("SELECT amount, orig_amount, orig_currency FROM txn_lines WHERE merchant IS NOT NULL").fetchone()
    assert (row["amount"], row["orig_amount"], row["orig_currency"]) == (-1100, -1000, "EUR")


def test_demo_database_home_amounts_equal_native(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE amount_home IS NOT amount").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM splits WHERE amount_home IS NOT amount").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM txn_lines WHERE amount IS NOT orig_amount").fetchone()[0] == 0


# Hashes of canonical JSON, computed on the demo data before the multi-currency change (home-only DBs must not move).
PRE_FX_HASHES = {
    "spending_summary": "c64cf67afd2b77c745387cdc732f55a807454259f40db12ba9a0992254b56855",
    "spending_by_merchant": "d6c736e8bdd8ad664c5ad7bf9281c29937e664cdf982567286bd45c00235f224",
    "cash_flow": "ef22db0189fe48d4b9f37318d6aca1c083241357cbf11a5619b3f64364b306a1",
    "budget_status": "827bf5cc5fd3033393b7d600d533e91cf5de2e3319612ae0211b549b82a6a902",
    "forecast_cash": "53e1a6f659711d5001da1361626f915f436a6a646be88c2e7b39243b10647b3d",
    "query_transactions": "3f69eae92d3365b87fa044a725e336ae6fe225b2001ec8b66bd452c7cb24be11",
    "net_worth": "105eba6b53d4aaf91b8b6eaab638910fd0ea759f9998bc3e96dddcea82be9c0f",
    "list_uncategorized": "78ee335dab74c75ab8b8d4ba51162a0bc62ebd7a27940033cc650bdf6bf1805e",
    "find_anomalies": "b6443640c066c3d31fa94ee253dc8a556e3a470f02fee5e55830c6e90e7b9096",
    "refund_tracker": "95238697a386167ea976bac7b21a408479d96dab4259511ecdd83ed0933aff61",
    "subscription_watchdog": "c44edea4c19e01c5f7abaf6be642ce55c457fd8cdc6fc9a0467fdf22d29e65a3",
    "list_recurring": "38279ead5566ff6393bbc28cf49d5690451588e820886c548d39fdbc2a3843f9",
}


def test_single_currency_outputs_are_byte_identical_to_pre_fx(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    outs = {
        "spending_summary": analysis.spending_summary(conn, cfg, today=TODAY),
        "spending_by_merchant": analysis.spending_summary(conn, cfg, group_by="merchant", today=TODAY),
        "cash_flow": analysis.cash_flow(conn, cfg, today=TODAY),
        "budget_status": analysis.budget_status(conn, cfg, today=TODAY),
        "forecast_cash": analysis.forecast_cash(conn, cfg, today=TODAY),
        "query_transactions": analysis.query_transactions(conn, cfg, today=TODAY),
        "net_worth": analysis.net_worth(conn, cfg, today=TODAY),
        "list_uncategorized": analysis.list_uncategorized(conn, cfg, today=TODAY),
        "find_anomalies": analysis.find_anomalies(conn, cfg, today=TODAY),
        "refund_tracker": analysis.refund_tracker(conn, cfg, today=TODAY),
        "subscription_watchdog": analysis.subscription_watchdog(conn, cfg, today=TODAY),
        "list_recurring": analysis.list_recurring(conn, cfg, today=TODAY),
    }
    got = {k: hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest() for k, v in outs.items()}
    assert got == PRE_FX_HASHES


# --- migration 10 ----------------------------------------------------------------------------------------------------


def test_migration_backfills_home_rows_and_leaves_foreign_null(cfg):
    path = cfg.db_path
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for script in MIGRATIONS[:9]:
        raw.executescript(script)
    raw.execute("INSERT INTO schema_version VALUES (9)")
    acct_sql = "INSERT INTO accounts (id, name, type, currency, is_asset, created_at) VALUES (?, ?, 'cash', ?, 1, 'x')"
    raw.execute(acct_sql, (1, "U", "USD"))
    raw.execute(acct_sql, (2, "E", "EUR"))
    for i, (acct, cur) in enumerate([(1, "USD"), (1, "USD"), (2, "EUR")], start=1):
        raw.execute(
            "INSERT INTO transactions (id, account_id, posted_date, amount, currency, raw_description, dedupe_key) "
            "VALUES (?, ?, '2026-09-01', ?, ?, 'x', ?)",
            (i, acct, -100 * i, cur, f"k{i}"),
        )
    raw.execute("INSERT INTO splits (transaction_id, amount) VALUES (2, -100)")
    raw.execute("INSERT INTO splits (transaction_id, amount) VALUES (2, -100)")
    raw.commit()
    migrate(raw, cfg)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == len(MIGRATIONS)
    got = {r["id"]: r["amount_home"] for r in raw.execute("SELECT id, amount_home FROM transactions")}
    assert got == {1: -100, 2: -200, 3: None}
    assert [r[0] for r in raw.execute("SELECT amount_home FROM splits")] == [-100, -100]
    assert raw.execute("SELECT value FROM meta WHERE key = 'home_currency'").fetchone()[0] == "USD"
    raw.close()


# --- imports, splits, meta.unconverted -------------------------------------------------------------------------------


def test_unconverted_rows_are_excluded_and_counted_in_meta(conn, cfg):
    acct = add_account(conn, "Cash", "checking")
    add_txn(conn, acct, "2026-09-02", -5000, "Home Shop", "Dining")
    import_eur(conn, cfg, [("2026-09-03", -9000, "Euro Cafe")])
    r = analysis.spending_summary(conn, cfg, from_="2026-09-01", to="2026-09-30", today=TODAY)
    assert r["total_spent"]["minor"] == 5000
    un = r["meta"]["unconverted"]
    assert un["count"] == 1 and un["currencies"] == ["EUR"]
    assert "set_fx_rate" in un["note"] and "7 days" in un["note"]
    cf = analysis.cash_flow(conn, cfg, from_="2026-09-01", to="2026-09-30", today=TODAY)
    assert cf["meta"]["unconverted"]["count"] == 1
    # out of period -> no key
    other = analysis.spending_summary(conn, cfg, from_="2026-01-01", to="2026-01-31", today=TODAY)
    assert "unconverted" not in other["meta"]


def test_count_in_summary_ignores_unconverted_expense_kind_rows(conn, cfg):
    import_eur(conn, cfg, [("2026-09-03", 900, "Refund from Cafe")])
    conn.execute("UPDATE transactions SET category_id = (SELECT id FROM categories WHERE name = 'Dining')")
    conn.commit()
    r = analysis.spending_summary(conn, cfg, from_="2026-09-01", to="2026-09-30", today=TODAY)
    assert r["meta"]["row_count"] == 0


def test_foreign_transactions_convert_and_sum_in_aggregates(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.1")
    import_eur(conn, cfg, [("2026-09-03", -1000, "Euro Cafe"), ("2026-09-04", -333, "Euro Bus")])
    conn.execute("UPDATE transactions SET category_id = (SELECT id FROM categories WHERE name = 'Dining')")
    conn.commit()
    r = analysis.spending_summary(conn, cfg, from_="2026-09-01", to="2026-09-30", today=TODAY)
    # per-row rounding: 1100 + round(366.3)=366
    assert r["total_spent"]["minor"] == 1466
    assert "unconverted" not in r["meta"]
    q = analysis.query_transactions(conn, cfg, from_="2026-09-01", to="2026-09-30", today=TODAY)
    row = next(t for t in q["transactions"] if t["merchant"] and "Cafe" in t["merchant"])
    assert row["amount"] == {"minor": -1000, "display": "-€10.00"}
    assert row["amount_home"] == {"minor": -1100, "display": "-$11.00"}
    assert q["totals"]["net"]["minor"] == -1466


def test_query_transactions_unconverted_row_has_null_home_and_stays_out_of_totals(conn, cfg):
    import_eur(conn, cfg, [("2026-09-03", -1000, "Euro Cafe")])
    q = analysis.query_transactions(conn, cfg, from_="2026-09-01", to="2026-09-30", today=TODAY)
    assert q["transactions"][0]["amount_home"] is None
    assert q["totals"]["net"]["minor"] == 0
    assert q["meta"]["unconverted"]["count"] == 1


def test_split_children_sum_exactly_to_parent_home(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.0833")
    import_eur(conn, cfg, [("2026-09-02", -1000, "Euro Mart")])
    tid = conn.execute("SELECT id FROM transactions").fetchone()[0]
    parts = [{"amount": -3.33, "category": c} for c in ("Groceries", "Dining")]
    parts.append({"amount": -3.34, "category": "Shopping"})
    analysis.split_transaction(conn, cfg, transaction_id=tid, parts=parts, dry_run=False)
    parent = conn.execute("SELECT amount_home FROM transactions WHERE id = ?", (tid,)).fetchone()[0]
    kids = [r[0] for r in conn.execute("SELECT amount_home FROM splits ORDER BY id")]
    assert parent == -1083 and sum(kids) == parent and all(k is not None for k in kids)
    lines = conn.execute("SELECT SUM(amount) FROM txn_lines").fetchone()[0]
    assert lines == parent


def test_splitting_an_unconverted_parent_leaves_children_unconverted_until_rate_arrives(conn, cfg):
    import_eur(conn, cfg, [("2026-09-02", -1000, "Euro Mart")])
    tid = conn.execute("SELECT id FROM transactions").fetchone()[0]
    split_in_two(conn, cfg, tid, -6, -4)
    assert [r[0] for r in conn.execute("SELECT amount_home FROM splits")] == [None, None]
    analysis.set_fx_rate(conn, cfg, items=[rate_item(rate="2")], dry_run=False, today=TODAY)
    assert [r[0] for r in conn.execute("SELECT amount_home FROM splits ORDER BY id")] == [-1200, -800]


def test_split_parts_use_the_transactions_currency_exponent(conn, cfg):
    put_rate(conn, "2026-09-01", "JPY", "0.0067")
    import_eur(conn, cfg, [("2026-09-02", -1000, "Tokyo Shop")], name="Yen", currency="JPY")
    tid = conn.execute("SELECT id FROM transactions").fetchone()[0]
    split_in_two(conn, cfg, tid, -600, -400)
    kids = [r[0] for r in conn.execute("SELECT amount FROM splits ORDER BY id")]
    assert kids == [-600, -400]
    assert sum(r[0] for r in conn.execute("SELECT amount_home FROM splits")) == -670


def test_pending_replacement_recomputes_carried_splits(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.1")
    add_account(conn, "Euro", "checking", currency="EUR")
    pending = ParsedTxn("2026-09-02", -1000, "Euro Mart", pending=True)
    pend = ParsedStatement(format="csv", txns=[pending], currency="EUR")
    import_statements(conn, cfg, [pend], "p.csv", "p1", account_name="Euro", post_process=False)
    tid = conn.execute("SELECT id FROM transactions").fetchone()[0]
    split_in_two(conn, cfg, tid, -6, -4)
    post = ParsedStatement(format="csv", txns=[ParsedTxn("2026-09-03", -1000, "Euro Mart")], currency="EUR")
    import_statements(conn, cfg, [post], "q.csv", "p2", account_name="Euro", post_process=False)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert sum(r[0] for r in conn.execute("SELECT amount_home FROM splits")) == -1100


# --- set_fx_rate -----------------------------------------------------------------------------------------------------


def test_set_fx_rate_dry_run_changes_nothing(conn, cfg):
    import_eur(conn, cfg, [("2026-09-03", -1000, "Euro Cafe")])
    r = set_rate(conn, cfg, rate_item(cur="eur"))
    assert r["dry_run"] is True
    assert r["changes"][0]["before"] is None and r["changes"][0]["after"] == "1.1"
    assert r["changes"][0]["affected_transactions"] == 1 and r["transactions_affected"] == 1
    assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0
    assert conn.execute("SELECT amount_home FROM transactions").fetchone()[0] is None


def test_set_fx_rate_apply_recomputes_and_reports(conn, cfg):
    rows = [("2026-09-03", -1000, "Euro Cafe"), ("2026-09-20", -1000, "Late Cafe"), ("2026-08-30", -1000, "Early Cafe")]
    import_eur(conn, cfg, rows)
    r = set_rate(conn, cfg, rate_item(), apply=True)
    assert r["dry_run"] is False and r["transactions_affected"] == 1
    assert homes(conn) == {"Euro Cafe": -1100, "Late Cafe": None, "Early Cafe": None}
    row = conn.execute("SELECT * FROM fx_rates").fetchone()
    assert (row["rate_date"], row["currency"], row["rate"], row["source"]) == ("2026-09-01", "EUR", "1.1", "manual")


def test_set_fx_rate_overwrite_and_remove(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.1")
    import_eur(conn, cfg, [("2026-09-03", -1000, "Euro Cafe")])
    r = set_rate(conn, cfg, rate_item(rate="1.2"), apply=True)
    assert r["changes"][0]["before"] == "1.1" and r["changes"][0]["after"] == "1.2"
    assert homes(conn)["Euro Cafe"] == -1200
    r = set_rate(conn, cfg, rate_item(rate=None), apply=True)
    assert r["changes"][0]["action"] == "remove" and r["transactions_affected"] == 1
    assert homes(conn)["Euro Cafe"] is None
    assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0


def test_set_fx_rate_unchanged_rate_affects_nothing(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.1")
    import_eur(conn, cfg, [("2026-09-03", -1000, "Euro Cafe")])
    r = set_rate(conn, cfg, rate_item(rate="1.10"))
    assert r["transactions_affected"] == 0 and r["changes"][0]["action"] == "unchanged"


@pytest.mark.parametrize(
    "item,code",
    [
        ({"date": "2026-09-01", "currency": "EUR", "rate": "0"}, "invalid"),
        ({"date": "2026-09-01", "currency": "EUR", "rate": "abc"}, "invalid"),
        ({"date": "2026-09-01", "currency": "USD", "rate": "1"}, "invalid"),  # home currency
        ({"date": "2026-09-01", "currency": "EURO", "rate": "1"}, "invalid"),
        ({"date": "2026-09-01", "currency": "E1R", "rate": "1"}, "invalid"),
        ({"date": "nope", "currency": "EUR", "rate": "1"}, "invalid"),
        ({"date": "2026-09-01", "currency": "EUR"}, "invalid"),  # rate key missing: not a removal
        ({"date": "2026-09-01", "currency": "EUR", "rate": None}, "not_found"),  # nothing to remove
    ],
)
def test_set_fx_rate_validation(conn, cfg, item, code):
    with pytest.raises(GuardError) as exc:
        analysis.set_fx_rate(conn, cfg, items=[item], today=TODAY)
    assert exc.value.code == code
    assert "EUR" not in str(exc.value) or code == "invalid"


def test_set_fx_rate_item_limits_and_duplicates(conn, cfg):
    with pytest.raises(GuardError):
        analysis.set_fx_rate(conn, cfg, items=[], today=TODAY)
    with pytest.raises(GuardError):
        analysis.set_fx_rate(conn, cfg, items=[{"date": "2026-09-01", "currency": "EUR", "rate": "1"}] * 2, today=TODAY)
    many = [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "currency": "EUR", "rate": "1"} for i in range(501)]
    with pytest.raises(GuardError):
        analysis.set_fx_rate(conn, cfg, items=many, today=TODAY)


def test_set_fx_rate_failure_applies_nothing(conn, cfg):
    with pytest.raises(GuardError):
        items = [rate_item(rate="1"), rate_item(day="2026-09-02", rate="x")]
        analysis.set_fx_rate(conn, cfg, items=items, dry_run=False, today=TODAY)
    assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0


# --- CSV import / refresh --------------------------------------------------------------------------------------------


def test_import_rates_csv_dry_run_then_apply(conn, cfg, tmp_path):
    p = tmp_path / "rates.csv"
    p.write_text("date,currency,rate\n2026-09-01,EUR,1.1\n2026-09-02,GBP,1.3\n", encoding="utf-8")
    import_eur(conn, cfg, [("2026-09-03", -1000, "Euro Cafe")])
    dry = fx.import_rates_csv(conn, cfg, p, apply=False)
    assert dry["dry_run"] is True and dry["rates"] == 2 and dry["transactions_affected"] == 1
    assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0
    done = fx.import_rates_csv(conn, cfg, p, apply=True)
    assert done["dry_run"] is False
    assert {r["source"] for r in conn.execute("SELECT source FROM fx_rates")} == {"csv"}
    assert homes(conn)["Euro Cafe"] == -1100


@pytest.mark.parametrize(
    "text",
    [
        "date,currency\n2026-09-01,EUR\n",  # header missing a column
        "date,currency,rate\n2026-09-01,EUR,zero\n",
        "date,currency,rate\n2026-09-01,USD,1\n",
        "date,currency,rate\nbad,EUR,1\n",
        "date,currency,rate\n2026-09-01,EUR,1\n2026-09-01,EUR,2\n",
    ],
)
def test_import_rates_csv_rejects_bad_files_without_echoing(conn, cfg, tmp_path, text):
    p = tmp_path / "rates.csv"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(GuardError) as exc:
        fx.import_rates_csv(conn, cfg, p, apply=True)
    assert "zero" not in str(exc.value)
    assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0


def test_refresh_recomputes_after_home_currency_change(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    cfg.currency = "EUR"
    n = analysis.spending_summary(conn, cfg, today=TODAY)
    assert n["meta"]["fx_notice"]  # stored home currency differs from config
    fx.refresh_home_amounts(conn, cfg)
    conn.commit()
    assert conn.execute("SELECT value FROM meta WHERE key = 'home_currency'").fetchone()[0] == "EUR"
    # every row is now in a currency other than EUR with no rates -> all unconverted
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE amount_home IS NOT NULL").fetchone()[0] == 0


# --- list_fx_rates ---------------------------------------------------------------------------------------------------


def test_list_fx_rates_filters_and_gaps(conn, cfg):
    put_rate(conn, "2026-09-01", "EUR", "1.1")
    put_rate(conn, "2026-09-05", "GBP", "1.3", "csv")
    rows = [("2026-09-03", -1000, "ok"), ("2026-10-01", -1000, "gap a"), ("2026-10-02", -1000, "gap b")]
    import_eur(conn, cfg, rows)
    r = analysis.list_fx_rates(conn, cfg, today=TODAY)
    assert [(x["date"], x["currency"], x["rate"], x["source"]) for x in r["rates"]] == [
        ("2026-09-05", "GBP", "1.3", "csv"),
        ("2026-09-01", "EUR", "1.1", "manual"),
    ]
    gap_range = {"from": "2026-10-01", "to": "2026-10-02", "transactions": 2}
    assert r["gaps"] == [{"currency": "EUR", "transactions": 2, "ranges": [gap_range]}]
    only = analysis.list_fx_rates(conn, cfg, currency="gbp", start_date="2026-09-01", end_date="2026-09-30")
    assert [x["currency"] for x in only["rates"]] == ["GBP"]
    assert analysis.list_fx_rates(conn, cfg, limit=1, today=TODAY)["meta"]["truncated"] is True


def test_list_fx_rates_empty_and_bad_currency(conn, cfg):
    r = analysis.list_fx_rates(conn, cfg, today=TODAY)
    assert r["rates"] == [] and r["gaps"] == []
    with pytest.raises(GuardError):
        analysis.list_fx_rates(conn, cfg, currency="dollars", today=TODAY)


# --- net worth and holdings ------------------------------------------------------------------------------------------


def test_net_worth_converts_foreign_balances_and_flags_missing(conn, cfg):
    usd = add_account(conn, "Cash", "checking")
    eur = eur_account(conn)
    set_balance(conn, usd, 100000)
    set_balance(conn, eur, 50000)
    r = analysis.net_worth(conn, cfg, today=TODAY)
    assert r["total_assets"]["minor"] == 100000
    assert r["unconverted_balances"] == [{"account": "Euro", "currency": "EUR", "reason": "no_fx_rate"}]
    assert "Euro" in r["missing_balances"]
    put_rate(conn, "2026-10-09", "EUR", "1.1")
    r = analysis.net_worth(conn, cfg, today=TODAY)
    assert r["total_assets"]["minor"] == 155000
    euro = next(a for a in r["assets"] if a["name"] == "Euro")
    assert euro["original_amount"] == {"minor": 50000, "display": "€500.00"}
    assert "unconverted_balances" not in r


def test_forecast_uses_home_currency_accounts_only(conn, cfg):
    usd = add_account(conn, "Cash", "checking")
    eur = eur_account(conn)
    set_balance(conn, usd, 100000)
    set_balance(conn, eur, 777700)
    r = analysis.forecast_cash(conn, cfg, today=TODAY, include_discretionary=False)
    assert r["starting_balance"]["minor"] == 100000
    assert any("Euro" in a and "other currenc" in a for a in r["assumptions"])


def test_holdings_in_foreign_currency_are_converted(conn, cfg):
    acct = add_account(conn, "Euro Broker", "brokerage", currency="EUR")
    conn.execute(
        "INSERT INTO holdings_snapshots (account_id, as_of, created_at) VALUES (?, '2026-10-09', 'x')", (acct,)
    )
    conn.execute(
        "INSERT INTO holdings (snapshot_id, symbol, asset_class, quantity, market_value, cost_basis, currency) "
        "VALUES (1, 'ABC', 'equity', '10', 10000, 8000, 'EUR')"
    )
    conn.commit()
    r = analysis.holdings(conn, cfg, today=TODAY)
    assert r["totals"]["unconverted_positions"] == 1
    assert r["positions"][0]["market_value"] is None
    put_rate(conn, "2026-10-08", "EUR", "1.1")
    r = analysis.holdings(conn, cfg, today=TODAY)
    p = r["positions"][0]
    assert (p["market_value"]["minor"], p["cost_basis"]["minor"], p["unrealized_gain"]["minor"]) == (11000, 8800, 2200)
    assert p["original_currency"] == "EUR"
    nw = analysis.net_worth(conn, cfg, today=TODAY)
    assert nw["total_assets"]["minor"] == 11000


# --- recurring and anomalies read home amounts -----------------------------------------------------------------------


def test_recurring_detection_skips_unconverted_rows(conn, cfg):
    from fiscl.recurring import refresh_recurring

    put_rate(conn, "2026-06-01", "EUR", "1.0")
    rows = [(f"2026-{m:02d}-05", -999, "Euro Streaming") for m in (6, 7, 8, 9)]
    import_eur(conn, cfg, rows)
    refresh_recurring(conn, cfg, today=TODAY)
    # rows in July-Sept have no rate within 7 days -> only June converted -> not enough occurrences
    assert conn.execute("SELECT COUNT(*) FROM recurring").fetchone()[0] == 0
    for m in (7, 8, 9):  # control: with a rate for every month the same rows are a recurring charge
        put_rate(conn, f"2026-{m:02d}-01", "EUR", "1.0")
    fx.refresh_home_amounts(conn, cfg)
    refresh_recurring(conn, cfg, today=TODAY)
    assert conn.execute("SELECT COUNT(*) FROM recurring").fetchone()[0] == 1


# --- profile currency, CLI, doctor, migration safety -----------------------------------------------------------------


def test_csv_profile_currency_sets_statement_currency_and_exponent(conn, cfg, tmp_path):
    from fiscl.importers.core import import_file

    cfg.profiles_dir.mkdir(exist_ok=True)
    (cfg.profiles_dir / "kw.toml").write_text(
        'name = "kw"\nmatch_headers = ["Day", "Memo", "Amt"]\ndate_column = "Day"\ndescription_column = "Memo"\n'
        'amount_column = "Amt"\ndate_format = "%Y-%m-%d"\ncurrency = "kwd"\n',
        encoding="utf-8",
    )
    add_account(conn, "Kuwait", "checking", currency="KWD")
    f = tmp_path / "kw.csv"
    f.write_text("Day,Memo,Amt\n2026-09-02,Souq,-1.250\n", encoding="utf-8")
    put_rate(conn, "2026-09-01", "KWD", "3.25")
    r = import_file(conn, cfg, f, account_name="Kuwait")
    assert r.status == "ok", r.error
    row = conn.execute("SELECT amount, currency, amount_home FROM transactions").fetchone()
    assert (row["amount"], row["currency"], row["amount_home"]) == (-1250, "KWD", -406)  # 4.0625 USD -> 406 cents


def test_csv_profile_rejects_bad_currency(cfg):
    from fiscl.importers.csv_import import CSVImportError, Profile

    base = {"name": "x", "match_headers": ["a"], "date_column": "a", "description_column": "a", "amount_column": "a"}
    with pytest.raises(CSVImportError):
        Profile.from_dict({**base, "currency": "euros"}, "x")
    assert Profile.from_dict({**base, "currency": " eur "}, "x").currency == "EUR"


def test_cli_fx_import_is_a_dry_run_until_apply(cfg, tmp_path, capsys):
    from fiscl import cli
    from fiscl.db import open_db

    rates = tmp_path / "rates.csv"
    rates.write_text("date,currency,rate\n2026-09-01,EUR,1.1\n", encoding="utf-8")
    home = str(cfg.home)
    assert cli.main(["--home", home, "fx", "import", str(rates)]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    with open_db(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0
    assert cli.main(["--home", home, "fx", "import", str(rates), "--apply"]) == 0
    with open_db(cfg) as conn:
        assert conn.execute("SELECT rate, source FROM fx_rates").fetchone()[:] == ("1.1", "csv")
    rates.write_text("date,currency,rate\n2026-09-01,EUR,-3\n", encoding="utf-8")
    assert cli.main(["--home", home, "fx", "import", str(rates), "--apply"]) == 1
    assert cli.main(["--home", home, "fx", "refresh"]) == 0


def test_doctor_warns_when_home_currency_changed(cfg, conn, capsys):
    from fiscl import cli

    conn.commit()
    (cfg.home / "config.toml").write_text('currency = "EUR"\n', encoding="utf-8")
    assert cli.main(["--home", str(cfg.home), "doctor"]) == 0
    assert "fx refresh" in capsys.readouterr().out


def test_failed_post_migration_step_rolls_the_whole_migration_back(cfg, monkeypatch):
    from fiscl import db

    raw = sqlite3.connect(cfg.db_path)
    raw.row_factory = sqlite3.Row
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for script in MIGRATIONS[:9]:
        raw.executescript(script)
    raw.execute("INSERT INTO schema_version VALUES (9)")
    raw.commit()

    def boom(conn, cfg):
        raise RuntimeError("backfill failed")

    monkeypatch.setitem(db.POST_MIGRATION, 10, boom)
    with pytest.raises(RuntimeError):
        db.migrate(raw, cfg)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 9
    assert raw.execute("SELECT name FROM sqlite_master WHERE name = 'fx_rates'").fetchone() is None
    assert "amount_home" not in [r["name"] for r in raw.execute("PRAGMA table_info(transactions)")]
    raw.close()


def test_fx_module_imports_no_network_libraries():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parent.parent / "src" / "fiscl" / "fx.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    assert not modules & {"socket", "urllib", "http", "requests", "ssl", "httpx"}


# --- performance (DoD: aggregates stay fast with materialized home amounts) ---------------------------------------


def test_aggregates_stay_fast_on_50k_rows_with_ten_percent_foreign(conn, cfg):
    import time

    usd = add_account(conn, "Checking", "checking")
    eur = eur_account(conn)
    conn.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Dining')")
    cat = conn.execute("SELECT id FROM categories WHERE name = 'Dining'").fetchone()[0]
    rows = []
    for i in range(50_000):
        foreign = i % 10 == 0
        minor = -(100 + i % 5000)
        rows.append(
            (
                eur if foreign else usd,
                f"2026-{1 + i % 9:02d}-{1 + i % 28:02d}",
                minor,
                "EUR" if foreign else "USD",
                minor * 11 // 10 if foreign else minor,
                f"M{i % 300}",
                cat,
                f"fx{i}",
            )
        )
    conn.executemany(
        """INSERT INTO transactions (account_id, posted_date, amount, currency, amount_home, raw_description, merchant,
           category_id, category_source, dedupe_key) VALUES (?, ?, ?, ?, ?, 'x', ?, ?, 'manual', ?)""",
        rows,
    )
    conn.commit()
    t0 = time.monotonic()
    summary = analysis.spending_summary(conn, cfg, from_="2026-01-01", to="2026-12-31", today=TODAY)
    analysis.cash_flow(conn, cfg, from_="2026-01-01", to="2026-12-31", today=TODAY)
    elapsed_ms = (time.monotonic() - t0) * 1000
    print(f"fx benchmark: spending_summary + cash_flow on 50k rows (10% foreign) took {elapsed_ms:.0f} ms")
    assert summary["meta"]["row_count"] > 0
    # Spec budget is 200 ms each; the bar is loose for slow CI machines (same as the other benchmarks).
    assert elapsed_ms < 1500, f"two aggregates took {elapsed_ms:.0f} ms"
