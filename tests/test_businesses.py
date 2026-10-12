"""Phase 3 slice D: several businesses (Option A). A `businesses` table plus a nullable `business_id` on accounts,
transactions, splits and 1099 forms; NULL means the default business (id 1), so single-business output is unchanged."""

from __future__ import annotations

import csv
import json
import sqlite3

import pytest
from mcp import Client

from fiscl import analysis, tax
from fiscl.config import ConfigError, load_config
from fiscl.db import MIGRATIONS, add_account, connect
from fiscl.guard import GuardError
from fiscl.server import build_server

from .conftest import TODAY, add_txn

YEAR = 2025  # a past year: estimated_tax then uses actual profit instead of annualizing
DEFAULT_NAME = "Studio"


def with_config(cfg, text: str):
    cfg.config_file.write_text(text, encoding="utf-8")
    return load_config(cfg.home)


def draft(conn, cfg, **kw):
    return tax.schedule_c_draft(conn, cfg, year=kw.pop("year", YEAR), today=TODAY, **kw)


def line_amounts(d: dict) -> dict[str, int]:
    return {e["line"]: e["amount"]["minor"] for e in d["expenses_by_line"]}


@pytest.fixture
def two(conn, cfg):
    """Studio (the default business, id 1) and Consulting (id 2), a shared card, and one split across both.

    Studio:     income 10,000.00; Adobe 200.00 (27b); Zoom 150.00 (27b, shared card, no explicit business);
                split part 200.00 supplies (22)                                   -> expenses 550.00, net 9,450.00
    Consulting: income  6,000.00; Office Depot 1,000.00 (18); Staples 300.00 (22, shared card, per transaction);
                split part 300.00 office expense (18)                             -> expenses 1,600.00, net 4,400.00
    """
    tax.manage_business(conn, cfg, action="rename", name="My business", new_name=DEFAULT_NAME, dry_run=False)
    tax.manage_business(conn, cfg, action="add", name="Consulting", dry_run=False)
    studio = add_account(conn, "Studio acct", "checking", scope="business")
    consult = add_account(conn, "Consulting acct", "checking", scope="business")
    shared = add_account(conn, "Shared card", "credit", scope="mixed")
    tax.set_scope(conn, cfg, [{"account": "Consulting acct", "scope": "business", "business": "Consulting"}], False)
    add_txn(conn, studio, f"{YEAR}-03-01", 1_000_000, "Client A", category="Income: Business")
    add_txn(conn, studio, f"{YEAR}-03-02", -20_000, "Adobe", category="Business: Software & Subscriptions")
    add_txn(conn, consult, f"{YEAR}-04-01", 600_000, "Client B", category="Income: Business")
    add_txn(conn, consult, f"{YEAR}-04-02", -100_000, "Office Depot", category="Business: Office Expense")
    zoom = add_txn(conn, shared, f"{YEAR}-05-01", -15_000, "Zoom", category="Business: Software & Subscriptions")
    staples = add_txn(conn, shared, f"{YEAR}-05-02", -30_000, "Staples", category="Business: Supplies")
    amazon = add_txn(conn, shared, f"{YEAR}-05-03", -50_000, "Amazon", category="Shopping")
    conn.commit()
    tax.set_scope(conn, cfg, [{"transaction_ids": [zoom], "scope": "business"}], False)
    tax.set_scope(conn, cfg, [{"transaction_ids": [staples], "scope": "business", "business": "Consulting"}], False)
    analysis.split_transaction(
        conn,
        cfg,
        transaction_id=amazon,
        parts=[
            {"amount": "-200.00", "category": "Business: Supplies", "scope": "business", "business": DEFAULT_NAME},
            {
                "amount": "-300.00",
                "category": "Business: Office Expense",
                "scope": "business",
                "business": "consulting",
            },
        ],
        dry_run=False,
    )
    conn.commit()
    return {"studio": studio, "consult": consult, "shared": shared, "zoom": zoom, "staples": staples, "amazon": amazon}


def by_name(d: dict) -> dict[str, dict]:
    return {b["business"]["name"]: b for b in d["businesses"]}


# --- migration 8 -----------------------------------------------------------------------------


def test_migration_8_adds_businesses_and_columns(conn):
    assert len(MIGRATIONS) >= 8
    assert conn.execute("SELECT id, name, archived FROM businesses").fetchall()[0][:] == (1, "My business", 0)
    for table in ("accounts", "transactions", "splits", "forms_1099"):
        assert "business_id" in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    assert "business_id" in {r["name"] for r in conn.execute("PRAGMA table_info(txn_lines)")}


def test_schema_6_database_migrates_to_8_with_a_backup_and_identical_output(cfg, tmp_path):
    def populate(c):
        c.execute(
            "INSERT INTO accounts (name, type, is_asset, created_at, scope) VALUES ('Biz','checking',1,'x','business')"
        )
        cat = c.execute("SELECT id FROM categories WHERE name = 'Business: Supplies'").fetchone()[0]
        inc = c.execute("SELECT id FROM categories WHERE name = 'Income: Business'").fetchone()[0]
        for i, (amt, cid) in enumerate(((500_000, inc), (-12_345, cat))):
            c.execute(
                "INSERT INTO transactions (account_id, posted_date, amount, currency, raw_description, merchant, "
                "category_id, category_source, dedupe_key) VALUES (1, ?, ?, 'USD', 'x', 'X', ?, 'manual', ?)",
                (f"2025-02-0{i + 1}", amt, cid, f"k{i}"),
            )

    # An old database, frozen at schema 6.
    old = sqlite3.connect(cfg.db_path)
    for sql in MIGRATIONS[:6]:
        old.executescript(sql)
    old.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    old.execute("INSERT INTO schema_version VALUES (6)")
    populate(old)
    old.commit()
    old.close()
    migrated = connect(cfg)
    try:
        assert migrated.execute("SELECT version FROM schema_version").fetchone()[0] == len(MIGRATIONS)
        assert list(cfg.backups.glob("fiscl-*-pre-migration.db"))
        got = tax.schedule_c_draft(migrated, cfg, year=2025, today=TODAY)
    finally:
        migrated.close()
    # The same data in a database that was created at the latest schema gives the same draft, JSON minus meta.
    fresh_cfg = load_config(tmp_path / "fresh")
    from fiscl.config import ensure_layout

    ensure_layout(fresh_cfg)
    fresh = connect(fresh_cfg)
    try:
        populate(fresh)
        fresh.commit()
        want = tax.schedule_c_draft(fresh, fresh_cfg, year=2025, today=TODAY)
    finally:
        fresh.close()
    got.pop("meta"), want.pop("meta")
    assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)
    assert "business" not in got and got["gross_receipts"]["minor"] == 500_000


def test_single_business_output_has_no_business_keys(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], f"{YEAR}-03-01", 100_000, "Client", category="Income: Business")
    conn.commit()
    d = draft(conn, cfg)
    assert "business" not in d and "businesses" not in d
    e = tax.estimated_tax(conn, cfg, year=YEAR, today=TODAY)
    assert "by_business" not in e
    assert "business_unassigned" not in tax.review_writeoffs(conn, cfg, year=YEAR, today=TODAY)["sections"]
    assert "My business" not in json.dumps(tax.export_schedule_c(conn, cfg, year=YEAR, today=TODAY))


# --- manage_business -------------------------------------------------------------------------


def test_manage_business_dry_run_changes_nothing(conn, cfg):
    r = tax.manage_business(conn, cfg, action="add", name="Consulting")
    assert r["dry_run"] is True and r["changes"][0]["action"] == "add"
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1


def test_add_rename_archive_round_trip(conn, cfg):
    r = tax.manage_business(conn, cfg, action="add", name="Consulting", dry_run=False)
    assert r["dry_run"] is False
    row = conn.execute("SELECT * FROM businesses WHERE name = 'Consulting'").fetchone()
    assert row["id"] == 2 and row["archived"] == 0
    tax.manage_business(conn, cfg, action="rename", name="consulting", new_name="Advisory", dry_run=False)
    assert conn.execute("SELECT name FROM businesses WHERE id = 2").fetchone()[0] == "Advisory"
    tax.manage_business(conn, cfg, action="archive", name="Advisory", dry_run=False)
    assert conn.execute("SELECT archived FROM businesses WHERE id = 2").fetchone()[0] == 1


def test_default_business_can_be_renamed_but_never_archived(conn, cfg):
    tax.manage_business(conn, cfg, action="rename", name="My business", new_name="Studio", dry_run=False)
    assert conn.execute("SELECT name FROM businesses WHERE id = 1").fetchone()[0] == "Studio"
    with pytest.raises(GuardError) as e:
        tax.manage_business(conn, cfg, action="archive", name="Studio", dry_run=False)
    assert e.value.code == "invalid"


def test_names_are_unique_case_insensitively_and_validated(conn, cfg):
    tax.manage_business(conn, cfg, action="add", name="Consulting", dry_run=False)
    for name in ("consulting", "CONSULTING", "my BUSINESS"):
        with pytest.raises(GuardError) as e:
            tax.manage_business(conn, cfg, action="add", name=name, dry_run=False)
        assert e.value.code == "exists"
    for bad in ("", "   ", "x" * 61, None, 5):
        with pytest.raises(GuardError):
            tax.manage_business(conn, cfg, action="add", name=bad)
    with pytest.raises(GuardError):
        tax.manage_business(conn, cfg, action="explode", name="Consulting")
    with pytest.raises(GuardError) as e:
        tax.manage_business(conn, cfg, action="rename", name="Consulting")
    assert e.value.code in ("invalid", "needs_terms")
    with pytest.raises(GuardError) as e:
        tax.manage_business(conn, cfg, action="rename", name="ZZNOSUCHZZ", new_name="Other")
    assert e.value.code == "not_found" and "ZZNOSUCHZZ" not in str(e.value)


def test_cannot_archive_a_business_with_activity_this_year(conn, cfg, two):
    this_year = TODAY.year
    add_txn(conn, two["consult"], f"{this_year}-02-01", 5_000, "Client B", category="Income: Business")
    conn.commit()
    with pytest.raises(GuardError) as e:
        tax.manage_business(conn, cfg, action="archive", name="Consulting", dry_run=False, today=TODAY)
    assert e.value.code == "invalid" and "Consulting" not in str(e.value)


def test_archived_business_is_hidden_when_idle_and_cannot_receive_new_assignments(conn, cfg):
    tax.manage_business(conn, cfg, action="add", name="Old", dry_run=False)
    tax.manage_business(conn, cfg, action="archive", name="Old", dry_run=False, today=TODAY)
    add_account(conn, "A", "checking", scope="business")
    conn.commit()
    with pytest.raises(GuardError) as e:
        tax.set_scope(conn, cfg, [{"account": "A", "scope": "business", "business": "Old"}], False)
    assert e.value.code == "invalid"
    assert "business" not in draft(conn, cfg)  # the idle archived business doesn't turn on the multi-business shape


def test_list_businesses_reports_activity(conn, cfg, two):
    r = tax.list_businesses(conn, cfg, year=YEAR, today=TODAY)
    rows = {b["name"]: b for b in r["businesses"]}
    assert rows[DEFAULT_NAME]["gross_receipts"]["minor"] == 1_000_000
    assert rows[DEFAULT_NAME]["expenses"]["minor"] == 55_000
    assert rows["Consulting"]["gross_receipts"]["minor"] == 600_000
    assert rows["Consulting"]["expenses"]["minor"] == 160_000
    assert rows["Consulting"]["accounts"] == ["Consulting acct"]
    assert rows[DEFAULT_NAME]["is_default"] is True


# --- assignment: set_scope and split_transaction ----------------------------------------------


def test_business_only_with_business_scope_and_known_name(conn, cfg, two):
    with pytest.raises(GuardError):
        tax.set_scope(conn, cfg, [{"transaction_ids": [two["zoom"]], "scope": "personal", "business": "Consulting"}])
    with pytest.raises(GuardError):
        tax.set_scope(conn, cfg, [{"account": "Shared card", "scope": "personal", "business": "Consulting"}])
    with pytest.raises(GuardError) as e:
        tax.set_scope(conn, cfg, [{"transaction_ids": [two["zoom"]], "scope": "business", "business": "ZZNOPEZZ"}])
    assert e.value.code == "not_found" and "ZZNOPEZZ" not in str(e.value)
    with pytest.raises(GuardError):
        tax.set_scope(conn, cfg, [{"transaction_ids": [two["zoom"]], "scope": "business", "business": 5}])


def test_set_scope_dry_run_names_the_business_and_writes_nothing(conn, cfg, two):
    r = tax.set_scope(conn, cfg, [{"transaction_ids": [two["zoom"]], "scope": "business", "business": "Consulting"}])
    assert r["dry_run"] is True and r["changes"][0]["business"] == "Consulting"
    assert conn.execute("SELECT business_id FROM transactions WHERE id = ?", (two["zoom"],)).fetchone()[0] is None


def test_assigning_a_transaction_moves_it_between_drafts(conn, cfg, two):
    before = by_name(draft(conn, cfg))
    assert line_amounts(before[DEFAULT_NAME])["27b"] == 35_000
    tax.set_scope(conn, cfg, [{"transaction_ids": [two["zoom"]], "scope": "business", "business": "Consulting"}], False)
    after = by_name(draft(conn, cfg))
    assert line_amounts(after[DEFAULT_NAME])["27b"] == 20_000
    assert line_amounts(after["Consulting"])["27b"] == 15_000


def test_assigning_a_merchant_and_an_account(conn, cfg, two):
    tax.set_scope(conn, cfg, [{"merchant": "Zoom", "scope": "business", "business": "Consulting"}], False)
    assert line_amounts(by_name(draft(conn, cfg))["Consulting"])["27b"] == 15_000
    tax.set_scope(conn, cfg, [{"account": "Studio acct", "scope": "business", "business": "Consulting"}], False)
    d = by_name(draft(conn, cfg))
    assert d["Consulting"]["gross_receipts"]["minor"] == 1_600_000


def test_split_part_business_lands_on_that_business(conn, cfg, two):
    d = by_name(draft(conn, cfg))
    assert line_amounts(d[DEFAULT_NAME])["22"] == 20_000  # the split's Studio part
    assert line_amounts(d["Consulting"])["22"] == 30_000  # Staples, assigned by transaction
    assert line_amounts(d["Consulting"])["18"] == 130_000  # Office Depot + the split's Consulting part


def test_split_part_business_needs_business_scope(conn, cfg, two):
    parts = [
        {"amount": "-100.00", "category": "Shopping", "business": "Consulting"},
        {"amount": "-400.00", "category": "Shopping"},
    ]
    with pytest.raises(GuardError):
        analysis.split_transaction(conn, cfg, transaction_id=two["amazon"], parts=parts)
    parts[0]["scope"] = "business"
    parts[0]["business"] = "ZZNOPEZZ"
    with pytest.raises(GuardError) as e:
        analysis.split_transaction(conn, cfg, transaction_id=two["amazon"], parts=parts)
    assert e.value.code == "not_found" and "ZZNOPEZZ" not in str(e.value)


def test_split_dry_run_shows_the_business(conn, cfg, two):
    r = analysis.split_transaction(
        conn,
        cfg,
        transaction_id=two["amazon"],
        parts=[
            {"amount": "-100.00", "category": "Shopping", "scope": "business", "business": "Consulting"},
            {"amount": "-400.00", "category": "Shopping"},
        ],
    )
    assert r["changes"][0]["parts"][0]["business"] == "Consulting"


# --- schedule_c_draft ------------------------------------------------------------------------


def test_two_businesses_give_two_drafts_that_sum_to_the_combined_spending(conn, cfg, two):
    d = draft(conn, cfg)
    assert [b["business"]["name"] for b in d["businesses"]] == [DEFAULT_NAME, "Consulting"]
    studio, cons = by_name(d)[DEFAULT_NAME], by_name(d)["Consulting"]
    assert studio["gross_receipts"]["minor"] == 1_000_000
    assert studio["total_expenses"]["minor"] == 55_000
    assert studio["net_profit"]["minor"] == 945_000
    assert cons["gross_receipts"]["minor"] == 600_000
    assert cons["total_expenses"]["minor"] == 160_000
    assert cons["net_profit"]["minor"] == 440_000
    # The drafts add up to every business-scope line in the database.
    all_gross, all_spend = conn.execute(
        """SELECT SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END),
                  -SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END)
           FROM txn_lines t JOIN accounts a ON a.id = t.account_id
           WHERE COALESCE(t.scope, CASE a.scope WHEN 'business' THEN 'business' ELSE 'personal' END) = 'business'"""
    ).fetchone()
    assert studio["gross_receipts"]["minor"] + cons["gross_receipts"]["minor"] == all_gross == 1_600_000
    assert studio["total_expenses"]["minor"] + cons["total_expenses"]["minor"] == all_spend == 215_000
    assert d["combined"]["gross_receipts"]["minor"] == all_gross
    assert d["combined"]["total_expenses"]["minor"] == all_spend
    assert d["combined_net_profit"]["minor"] == 1_385_000
    assert "meta" in d and all("meta" not in b for b in d["businesses"])


def test_draft_for_one_business_by_name_is_case_insensitive_and_carries_its_identity(conn, cfg, two):
    d = draft(conn, cfg, business="CONSULTING")
    assert d["business"] == {"id": 2, "name": "Consulting"}
    assert d["gross_receipts"]["minor"] == 600_000
    assert "businesses" not in d


def test_unknown_business_is_not_found_without_echoing_the_name(conn, cfg, two):
    for fn in (
        lambda: draft(conn, cfg, business="ZZNOPEZZ"),
        lambda: tax.review_writeoffs(conn, cfg, year=YEAR, business="ZZNOPEZZ", today=TODAY),
        lambda: tax.export_schedule_c(conn, cfg, year=YEAR, business="ZZNOPEZZ", today=TODAY),
        lambda: tax.reconcile_1099(conn, cfg, tax_year=YEAR, business="ZZNOPEZZ", today=TODAY),
    ):
        with pytest.raises(GuardError) as e:
            fn()
        assert e.value.code == "not_found" and "ZZNOPEZZ" not in str(e.value)


def test_mileage_is_counted_once_on_the_default_business(conn, cfg, two):
    tax.log_trip(conn, cfg, f"{YEAR}-06-01", 100, "Client visit across town", dry_run=False)
    conn.commit()
    d = by_name(draft(conn, cfg))
    assert d[DEFAULT_NAME]["mileage"]["trips"] == 1
    assert d["Consulting"]["mileage"]["trips"] == 0
    assert any("default business" in n for n in d[DEFAULT_NAME]["notes"])


# --- estimated_tax: self-employment tax is per person -----------------------------------------


def test_se_tax_is_computed_once_on_the_combined_profit(conn, cfg, two):
    # Combined net 13,850.00 -> net earnings 1,279,047 (x 0.9235, truncated). W-2 wages of 170,000 leave 6,100.00 of
    # the 2025 wage base ($176,100), so Social Security applies to 610,000 only once, not once per business.
    #   12.4% x 610,000 = 75,640.0 ; 2.9% x 1,279,047 = 37,092.363 ; total 112,732 (half-even)
    e = tax.estimated_tax(conn, cfg, year=YEAR, w2_wages=170_000, today=TODAY)
    assert e["self_employment_tax"]["minor"] == 112_732
    assert e["ytd_net_profit"]["minor"] == 1_385_000
    nets = {b["business"]["name"]: b["ytd_net_profit"]["minor"] for b in e["by_business"]}
    assert nets == {DEFAULT_NAME: 945_000, "Consulting": 440_000}
    assert any("per person" in n for n in e["notes"])


def test_a_loss_in_one_business_reduces_the_combined_profit(conn, cfg, two):
    add_txn(conn, two["consult"], f"{YEAR}-07-01", -1_000_000, "Big gear", category="Business: Equipment")
    conn.commit()
    e = tax.estimated_tax(conn, cfg, year=YEAR, today=TODAY)
    assert e["ytd_net_profit"]["minor"] == 385_000
    assert {b["business"]["name"]: b["ytd_net_profit"]["minor"] for b in e["by_business"]}["Consulting"] == -560_000


# --- review_writeoffs ------------------------------------------------------------------------


def test_review_asks_which_business_for_unassigned_rows(conn, cfg, two):
    r = tax.review_writeoffs(conn, cfg, year=YEAR, today=TODAY)
    rows = r["sections"]["business_unassigned"]
    merchants = {x["merchant"] for x in rows}
    assert {"Client A", "Adobe", "Zoom"} <= merchants  # they fall through to the default business
    assert "Staples" not in merchants and "Client B" not in merchants
    assert all("Which business" in x["question"] for x in rows)


def test_review_can_be_limited_to_one_business(conn, cfg, two):
    add_txn(conn, two["consult"], f"{YEAR}-08-01", -400_000, "Gear", category="Business: Equipment")
    add_txn(conn, two["studio"], f"{YEAR}-08-02", -300_000, "Camera", category="Business: Equipment")
    conn.commit()
    r = tax.review_writeoffs(conn, cfg, year=YEAR, business="Consulting", today=TODAY)
    assert [x["merchant"] for x in r["sections"]["large_business_purchases"]] == ["Gear"]
    assert "business_unassigned" not in r["sections"]


# --- export_schedule_c -----------------------------------------------------------------------


def test_export_writes_one_file_set_per_business_named_by_id(conn, cfg, two):
    plan = tax.export_schedule_c(conn, cfg, year=YEAR, include_transactions=True, today=TODAY)
    paths = [f["path"] for f in plan["files"]]
    assert len(paths) == 6
    assert sum("-b1-" in p for p in paths) == 3 and sum("-b2-" in p for p in paths) == 3
    assert "Consulting" not in json.dumps(paths) and DEFAULT_NAME not in json.dumps(paths)
    assert not list(cfg.exports.glob("*"))
    assert {b["business"]["name"] for b in plan["by_business"]} == {DEFAULT_NAME, "Consulting"}
    assert plan["summary"]["net_profit"]["minor"] == 1_385_000


def test_export_for_one_business_and_its_detail_rows(conn, cfg, two):
    r = tax.export_schedule_c(conn, cfg, year=YEAR, business="Consulting", include_transactions=True, dry_run=False)
    paths = [f["path"] for f in r["files"]]
    assert len(paths) == 3 and all("-b2-" in p for p in paths)
    detail = next(p for p in paths if p.endswith("-detail.csv"))
    with open(cfg.home / detail, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert {row["business"] for row in rows} == {"Consulting"}
    assert sum(1 for row in rows if row["split_id"]) == 1  # the one split part assigned to Consulting
    summary = next(p for p in paths if p.endswith(".csv") and "detail" not in p)
    with open(cfg.home / summary, newline="") as fh:
        net = next(row for row in csv.DictReader(fh) if row["section"] == "net_profit")
    assert net["amount"] == "4400.00"


# --- 1099 ------------------------------------------------------------------------------------


def add_form(conn, cfg, **kw):
    item = {"action": "add", "tax_year": YEAR, "form": "nec", "payer": "Acme", "amount": 5000}
    item.update(kw)
    return tax.record_1099(conn, cfg, [item], dry_run=False, today=TODAY)


def test_forms_unique_key_includes_the_business(conn, cfg, two):
    add_form(conn, cfg, business="Consulting")
    add_form(conn, cfg, business=DEFAULT_NAME)
    with pytest.raises(GuardError) as e:
        add_form(conn, cfg)  # no business means the default, which already has this payer and form
    assert e.value.code == "exists"
    with pytest.raises(GuardError) as e:
        add_form(conn, cfg, business="consulting")
    assert e.value.code == "exists"
    rows = conn.execute("SELECT business_id FROM forms_1099 ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [2, 1]


def test_record_1099_unknown_business_and_view(conn, cfg, two):
    with pytest.raises(GuardError) as e:
        add_form(conn, cfg, business="ZZNOPEZZ")
    assert e.value.code == "not_found" and "ZZNOPEZZ" not in str(e.value)
    r = tax.record_1099(
        conn,
        cfg,
        [{"action": "add", "tax_year": YEAR, "form": "nec", "payer": "Acme", "amount": 10, "business": "Consulting"}],
        today=TODAY,
    )
    assert r["changes"][0]["after"]["business"] == "Consulting"


def test_reconcile_by_business(conn, cfg, two):
    add_form(conn, cfg, business="Consulting", payer="Client B", amount=6_000)
    add_form(conn, cfg, business=DEFAULT_NAME, payer="Client A", amount=10_000)
    c = tax.reconcile_1099(conn, cfg, tax_year=YEAR, business="Consulting", today=TODAY)
    assert c["business"] == {"id": 2, "name": "Consulting"}
    assert [f["payer"] for f in c["forms"]] == ["Client B"]
    assert c["forms"][0]["status"] == "matches"
    assert c["totals"]["business_gross_receipts"]["minor"] == 600_000
    s = tax.reconcile_1099(conn, cfg, tax_year=YEAR, business=DEFAULT_NAME, today=TODAY)
    assert [f["payer"] for f in s["forms"]] == ["Client A"] and s["forms"][0]["status"] == "matches"
    both = tax.reconcile_1099(conn, cfg, tax_year=YEAR, today=TODAY)
    assert len(both["forms"]) == 2 and "business" not in both


def test_reconcile_does_not_match_income_of_another_business(conn, cfg, two):
    add_form(conn, cfg, business=DEFAULT_NAME, payer="Client B", amount=6_000)
    r = tax.reconcile_1099(conn, cfg, tax_year=YEAR, business=DEFAULT_NAME, today=TODAY)
    assert r["forms"][0]["status"] == "no_matching_income"


# --- home office allocation -------------------------------------------------------------------

ALLOC = (
    '[tax.home_office.2025]\nsquare_feet = 200\n[tax.home_office.2025.allocation]\n"Studio" = 150\n"Consulting" = 50\n'
)


def test_allocation_splits_the_home_office_between_businesses(conn, cfg, two):
    cfg = with_config(cfg, ALLOC)
    assert cfg.tax.home_office[2025].allocation == {"Studio": 150, "Consulting": 50}
    d = by_name(draft(conn, cfg))
    assert d[DEFAULT_NAME]["home_office"]["line_30"]["minor"] == 75_000
    assert d[DEFAULT_NAME]["home_office"]["square_feet"] == 150
    assert d["Consulting"]["home_office"]["line_30"]["minor"] == 25_000
    assert d[DEFAULT_NAME]["net_profit"]["minor"] == 945_000 - 75_000
    assert d["Consulting"]["net_profit"]["minor"] == 440_000 - 25_000


def test_each_business_limit_uses_its_own_profit(conn, cfg, two):
    add_txn(conn, two["consult"], f"{YEAR}-09-01", -430_000, "Gear", category="Business: Equipment")  # profit $100
    conn.commit()
    cfg = with_config(cfg, ALLOC)
    c = by_name(draft(conn, cfg))["Consulting"]["home_office"]
    assert c["computed"]["minor"] == 25_000 and c["line_30"]["minor"] == 10_000 and c["limited_to_profit"] is True


def test_without_allocation_the_home_office_goes_to_the_default_business(conn, cfg, two):
    cfg = with_config(cfg, "[tax.home_office.2025]\nsquare_feet = 200\n")
    d = by_name(draft(conn, cfg))
    assert d[DEFAULT_NAME]["home_office"]["line_30"]["minor"] == 100_000
    assert d["Consulting"]["home_office"] is None
    assert any("allocation" in n for n in d["Consulting"]["notes"])


def test_unlisted_business_gets_no_home_office_with_an_allocation(conn, cfg, two):
    cfg = with_config(
        cfg, '[tax.home_office.2025]\nsquare_feet = 200\n[tax.home_office.2025.allocation]\n"Studio" = 200\n'
    )
    d = by_name(draft(conn, cfg))
    assert d["Consulting"]["home_office"] is None and d[DEFAULT_NAME]["home_office"]["line_30"]["minor"] == 100_000


def test_allocation_names_a_business_that_does_not_exist_is_flagged(conn, cfg, two):
    cfg = with_config(
        cfg, '[tax.home_office.2025]\nsquare_feet = 200\n[tax.home_office.2025.allocation]\n"Nope Co" = 50\n'
    )
    d = draft(conn, cfg)
    assert any("allocation" in n and "no business" in n for n in d["notes"])
    assert "Nope Co" not in json.dumps(d["notes"])


def test_estimated_tax_uses_each_business_line_30(conn, cfg, two):
    cfg = with_config(cfg, ALLOC)
    e = tax.estimated_tax(conn, cfg, year=YEAR, today=TODAY)
    assert e["ytd_net_profit"]["minor"] == 1_385_000 - 100_000


@pytest.mark.parametrize(
    "body",
    [
        'square_feet = 200\n[tax.home_office.2025.allocation]\n"Studio" = 150\n"Consulting" = 51\n',  # 201 > 200
        'square_feet = 200\n[tax.home_office.2025.allocation]\n"Studio" = 0\n',
        'square_feet = 200\n[tax.home_office.2025.allocation]\n"Studio" = "150"\n',
        'square_feet = 200\n[tax.home_office.2025.allocation]\n"Studio" = true\n',
        'square_feet = 200\n[tax.home_office.2025.allocation]\n"Studio" = 100\n"studio" = 50\n',
        'square_feet = 200\n[tax.home_office.2025.allocation]\n"" = 50\n',
        "square_feet = 200\nallocation = 5\n",
    ],
)
def test_bad_allocation_config_is_rejected(cfg, body):
    with pytest.raises(ConfigError):
        with_config(cfg, f"[tax.home_office.2025]\n{body}")


def test_allocation_total_of_301_fails_config_load(cfg):
    with pytest.raises(ConfigError):
        with_config(
            cfg, '[tax.home_office.2025]\nsquare_feet = 300\n[tax.home_office.2025.allocation]\n"A" = 300\n"B" = 1\n'
        )


# --- performance -----------------------------------------------------------------------------


def test_per_business_drafts_stay_fast_on_50k_business_rows(conn, cfg, two):
    import time

    cat = conn.execute("SELECT id FROM categories WHERE name = 'Business: Supplies'").fetchone()[0]
    rows = [
        (
            two["studio"] if i % 2 else two["consult"],
            f"{YEAR}-{1 + i % 9:02d}-{1 + i % 28:02d}",
            -(100 + i % 5000),
            f"M{i % 300}",
            cat,
            f"perf{i}",
        )
        for i in range(50_000)
    ]
    conn.executemany(
        """INSERT INTO transactions (account_id, posted_date, amount, currency, raw_description, merchant,
           category_id, category_source, dedupe_key) VALUES (?, ?, ?, 'USD', 'x', ?, ?, 'manual', ?)""",
        rows,
    )
    conn.commit()
    t0 = time.monotonic()
    d = draft(conn, cfg)
    tax.estimated_tax(conn, cfg, year=YEAR, today=TODAY)
    elapsed = time.monotonic() - t0
    assert len(d["businesses"]) == 2
    # Spec budget is 200 ms per draft; the bar is loose for slow CI machines.
    assert elapsed < 2.0, f"two drafts and an estimate took {elapsed:.2f}s"


# --- MCP contract ----------------------------------------------------------------------------


async def test_business_tools_over_mcp(cfg, conn):
    conn.commit()
    async with Client(build_server(cfg)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["manage_business"].annotations.destructive_hint is True
        assert tools["list_businesses"].annotations.read_only_hint is True
        r = await c.call_tool("manage_business", {"action": "add", "name": "Consulting"})
        assert r.structured_content["dry_run"] is True
        r = await c.call_tool("manage_business", {"action": "add", "name": "Consulting", "dry_run": False})
        assert r.structured_content["dry_run"] is False
        listed = (await c.call_tool("list_businesses", {})).structured_content
        assert [b["name"] for b in listed["businesses"]] == ["My business", "Consulting"]
        d = (await c.call_tool("schedule_c_draft", {"year": YEAR})).structured_content
        assert len(d["businesses"]) == 2
        d = (await c.call_tool("schedule_c_draft", {"year": YEAR, "business": "Consulting"})).structured_content
        assert d["business"]["name"] == "Consulting"
        bad = await c.call_tool("schedule_c_draft", {"year": YEAR, "business": "ZZNOPEZZ"})
        assert bad.is_error and "ZZNOPEZZ" not in bad.content[0].text
