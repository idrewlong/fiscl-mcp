"""Phase 1 edits: migration 3, notes, tags, splits (and split-aware aggregates), dismissed recurring, set_budget."""

from __future__ import annotations

import sqlite3
import stat

import pytest

from fiscl import analysis, tax
from fiscl.config import ConfigError, load_budgets
from fiscl.db import MIGRATIONS, connect
from fiscl.guard import GuardError
from fiscl.recurring import refresh_recurring

from .conftest import TODAY, add_txn


def _groups(conn, cfg, **kw):
    r = analysis.spending_summary(conn, cfg, from_="2026-10-01", to="2026-10-12", compare_to="none", today=TODAY, **kw)
    return {g["name"]: g["spent"]["minor"] for g in r["groups"]}


# --- migration 3 -----------------------------------------------------------------------------


def test_v1_database_migrates_to_latest_with_backup(cfg):
    raw = sqlite3.connect(cfg.db_path)
    for sql in MIGRATIONS[:2]:
        raw.executescript(sql)
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    raw.execute("INSERT INTO schema_version VALUES (2)")
    raw.execute("INSERT INTO categories (name, kind) VALUES ('Groceries', 'expense')")
    raw.execute("INSERT INTO accounts (name, type, is_asset, created_at) VALUES ('Checking', 'checking', 1, 'x')")
    raw.execute(
        """INSERT INTO transactions (account_id, posted_date, amount, currency, raw_description, merchant,
               category_id, dedupe_key) VALUES (1, '2026-10-01', -5000, 'USD', 'KROGER', 'Kroger', 1, 'k1')"""
    )
    raw.commit()
    raw.close()

    conn = connect(cfg)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == len(MIGRATIONS)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        assert {"tags", "splits", "manual_assets", "goals", "txn_lines"} <= tables
        kept = conn.execute("SELECT merchant, amount FROM transactions").fetchall()
        assert [(r["merchant"], r["amount"]) for r in kept] == [("Kroger", -5000)]  # the v1 row survived
        assert conn.execute("SELECT amount FROM txn_lines").fetchone()[0] == -5000
        backups = list(cfg.backups.glob("fiscl-*-pre-migration.db"))
        assert len(backups) == 1 and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        old = sqlite3.connect(backups[0])
        assert old.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        old.close()
    finally:
        conn.close()


def test_txn_lines_replaces_parent_with_children(conn, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-01", -10000, "AMAZON", "Shopping")
    add_txn(conn, accounts["checking"], "2026-10-02", -500, "COFFEE", "Dining")
    cat = conn.execute("SELECT id FROM categories WHERE name = 'Groceries'").fetchone()[0]
    conn.executemany(
        "INSERT INTO splits (transaction_id, amount, category_id) VALUES (?, ?, ?)", [(t, -6000, cat), (t, -4000, cat)]
    )
    rows = conn.execute("SELECT id, split_id, amount FROM txn_lines ORDER BY amount").fetchall()
    assert [(r["id"], r["amount"]) for r in rows] == [(t, -6000), (t, -4000), (2, -500)]
    assert rows[0]["split_id"] is not None and rows[2]["split_id"] is None


# --- notes and tags --------------------------------------------------------------------------


def test_add_note_dry_run_apply_and_clear(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-01", -2500, "DINER")
    plan = analysis.add_note(conn, cfg, transaction_id=t, note="  client\n lunch ")
    assert plan["dry_run"] and plan["changes"][0]["new_note"] == "client lunch"
    assert conn.execute("SELECT note FROM transactions WHERE id = ?", (t,)).fetchone()[0] is None
    analysis.add_note(conn, cfg, transaction_id=t, note="client lunch", dry_run=False)
    row = analysis.query_transactions(conn, cfg, from_="2026-10-01", today=TODAY)["transactions"][0]
    assert row["note"] == "client lunch"
    out = analysis.add_note(conn, cfg, transaction_id=t, note=None, dry_run=False)
    assert out["changes"][0]["old_note"] == "client lunch"
    assert conn.execute("SELECT note FROM transactions WHERE id = ?", (t,)).fetchone()[0] is None


def test_add_note_validation(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-01", -2500, "DINER")
    with pytest.raises(GuardError):
        analysis.add_note(conn, cfg, transaction_id=t, note="x" * 501)
    with pytest.raises(GuardError, match="no transaction"):
        analysis.add_note(conn, cfg, transaction_id=9999, note="hi")
    with pytest.raises(GuardError):
        analysis.add_note(conn, cfg, transaction_id="1", note="hi")


def test_tags_add_remove_filter_and_limits(conn, cfg, accounts):
    a = add_txn(conn, accounts["checking"], "2026-10-01", -2500, "DINER")
    b = add_txn(conn, accounts["checking"], "2026-10-02", -900, "DINER")
    add_txn(conn, accounts["checking"], "2026-10-03", -900, "OTHER")
    items = [{"merchant": "Diner", "add": ["  Refund-Pending ", "trip"]}]
    plan = analysis.tag_transactions(conn, cfg, items)
    assert plan["dry_run"] and plan["changes"][0]["matched_transactions"] == 2
    assert plan["changes"][0]["add"] == ["refund-pending", "trip"]
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
    analysis.tag_transactions(conn, cfg, items, dry_run=False)
    analysis.tag_transactions(conn, cfg, items, dry_run=False)  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 4

    q = analysis.query_transactions(conn, cfg, from_="2026-10-01", tag="REFUND-PENDING", today=TODAY)
    assert {t["id"] for t in q["transactions"]} == {a, b}
    assert q["transactions"][0]["tags"] == ["refund-pending", "trip"]

    analysis.tag_transactions(conn, cfg, [{"transaction_ids": [a], "remove": ["trip"]}], dry_run=False)
    q = analysis.query_transactions(conn, cfg, from_="2026-10-01", tag="trip", today=TODAY)
    assert [t["id"] for t in q["transactions"]] == [b]


@pytest.mark.parametrize(
    "item",
    [
        {"merchant": "Diner", "add": ["x" * 41]},
        {"merchant": "Diner", "add": [f"t{i}" for i in range(21)]},
        {"merchant": "Diner"},
        {"merchant": "Diner", "add": ["a"], "remove": ["a"]},
        {"merchant": "Diner", "add": [""]},
        {"add": ["a"]},
    ],
)
def test_tag_validation(conn, cfg, accounts, item):
    add_txn(conn, accounts["checking"], "2026-10-01", -2500, "DINER")
    with pytest.raises(GuardError):
        analysis.tag_transactions(conn, cfg, [item])


def test_tag_bulk_cap(conn, cfg, accounts):
    with pytest.raises(GuardError):
        analysis.tag_transactions(conn, cfg, [{"transaction_ids": list(range(1, 600)), "add": ["x"]}])


# --- splits ----------------------------------------------------------------------------------


def _amazon(conn, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", -10000, "AMAZON", "Shopping")
    add_txn(conn, accounts["checking"], "2026-10-06", -3000, "COFFEE", "Dining")
    return t


def test_split_dry_run_changes_nothing(conn, cfg, accounts):
    t = _amazon(conn, accounts)
    parts = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Business: Supplies"}]
    plan = analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts)
    assert plan["dry_run"] and plan["changes"][0]["action"] == "split"
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0
    assert _groups(conn, cfg)["Shopping"] == 10000


def test_split_changes_spending_and_schedule_c_line_22(conn, cfg, accounts):
    t = _amazon(conn, accounts)
    assert "Groceries" not in _groups(conn, cfg)
    before_c = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    assert not [e for e in before_c["expenses_by_line"] if e["line"] == "22"]

    parts = [
        {"amount": -60, "category": "Groceries"},
        {"amount": -40, "category": "Business: Supplies", "scope": "business", "note": "printer paper"},
    ]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)

    groups = _groups(conn, cfg)
    assert groups["Groceries"] == 6000 and groups["Business: Supplies"] == 4000
    assert "Shopping" not in groups and groups["Dining"] == 3000
    assert _groups(conn, cfg, scope="business") == {"Business: Supplies": 4000}
    assert _groups(conn, cfg, scope="personal")["Groceries"] == 6000

    draft = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    line22 = next(e for e in draft["expenses_by_line"] if e["line"] == "22")
    assert line22["amount"]["minor"] == 4000
    assert "not tax advice" in " ".join(draft["notes"])

    # Cash flow totals are unchanged by a split; budgets follow the children.
    cf = analysis.cash_flow(conn, cfg, from_="2026-10-01", to="2026-10-12", today=TODAY)
    assert cf["totals"]["spending"]["minor"] == 13000


def test_split_counts_the_purchase_once_outside_category_grouping(conn, cfg, accounts):
    t = _amazon(conn, accounts)
    parts = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Business: Supplies"}]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    kw = {"from_": "2026-10-01", "to": "2026-10-12", "today": TODAY}
    by_merchant = analysis.spending_summary(conn, cfg, group_by="merchant", **kw)
    assert {g["name"]: g["count"] for g in by_merchant["groups"]}["Amazon"] == 1
    by_cat = analysis.spending_summary(conn, cfg, group_by="category", **kw)
    assert {g["name"]: g["count"] for g in by_cat["groups"]}["Groceries"] == 1
    figma = add_txn(conn, accounts["checking"], "2026-10-07", -5000, "FIGMA", "Software")
    analysis.split_transaction(
        conn,
        cfg,
        transaction_id=figma,
        parts=[{"amount": -30, "category": "Software"}, {"amount": -20, "category": "Shopping"}],
        dry_run=False,
    )
    r = tax.review_writeoffs(conn, cfg, year=2026, today=TODAY)
    row = next(x for x in r["sections"]["possibly_business_in_personal"] if x["merchant"] == "Figma")
    assert row["count"] == 1


def test_split_listing_shows_parent_with_splits_array(conn, cfg, accounts):
    t = _amazon(conn, accounts)
    parts = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Business: Supplies"}]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    q = analysis.query_transactions(conn, cfg, from_="2026-10-01", today=TODAY)
    assert q["totals"]["count"] == 2 and q["totals"]["money_out"]["minor"] == -13000
    row = next(r for r in q["transactions"] if r["id"] == t)
    assert [(p["category"], p["amount"]["minor"]) for p in row["splits"]] == [
        ("Groceries", -6000),
        ("Business: Supplies", -4000),
    ]
    # Category filter finds the parent through its children.
    q = analysis.query_transactions(conn, cfg, from_="2026-10-01", category="groceries", today=TODAY)
    assert [r["id"] for r in q["transactions"]] == [t]
    assert (
        analysis.query_transactions(conn, cfg, from_="2026-10-01", category="Shopping", today=TODAY)["transactions"]
        == []
    )


def test_split_not_in_uncategorized_backlog(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", -10000, "AMAZON")
    parts = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Dining"}]
    assert analysis.list_uncategorized(conn, cfg, today=TODAY)["uncategorized_transactions"] == 1
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    assert analysis.list_uncategorized(conn, cfg, today=TODAY)["uncategorized_transactions"] == 0


def test_split_that_does_not_sum_is_an_error(conn, cfg, accounts):
    t = _amazon(conn, accounts)
    parts = [{"amount": -60, "category": "Groceries"}, {"amount": -39, "category": "Business: Supplies"}]
    with pytest.raises(GuardError, match="must match exactly"):
        analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0


def test_split_validation(conn, cfg, accounts):
    t = _amazon(conn, accounts)
    ok = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Dining"}]
    bad = {
        "one part": [{"amount": -100, "category": "Groceries"}],
        "wrong sign": [{"amount": 60, "category": "Groceries"}, {"amount": -160, "category": "Dining"}],
        "no category": [{"amount": -60}, {"amount": -40, "category": "Dining"}],
        "zero": [{"amount": 0, "category": "Groceries"}, {"amount": -100, "category": "Dining"}],
        "bad scope": [{"amount": -60, "category": "Groceries", "scope": "x"}, ok[1]],
        "transfers": [{"amount": -60, "category": "Transfers"}, ok[1]],
        "too many": [{"amount": -1, "category": "Dining"}] * 21,
        "not a list": "nope",
    }
    for name, parts in bad.items():
        with pytest.raises(GuardError):
            analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts)
        assert name
    with pytest.raises(GuardError):
        analysis.split_transaction(conn, cfg, transaction_id=9999, parts=ok)
    with pytest.raises(GuardError, match="isn't split"):
        analysis.split_transaction(conn, cfg, transaction_id=t, parts=[])


def test_transfers_cannot_be_split(conn, cfg, accounts):
    a = add_txn(conn, accounts["checking"], "2026-10-05", -50000, "TO SAVINGS")
    conn.execute("UPDATE transactions SET transfer_group = 'g1' WHERE id = ?", (a,))
    parts = [{"amount": -300, "category": "Dining"}, {"amount": -200, "category": "Groceries"}]
    with pytest.raises(GuardError, match="transfers"):
        analysis.split_transaction(conn, cfg, transaction_id=a, parts=parts)


def test_resplit_replaces_and_empty_parts_removes(conn, cfg, accounts):
    t = _amazon(conn, accounts)
    first = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Dining"}]
    second = [{"amount": -10, "category": "Dining"}, {"amount": -90, "category": "Groceries"}]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=first, dry_run=False)
    plan = analysis.split_transaction(conn, cfg, transaction_id=t, parts=second, dry_run=False)
    assert plan["changes"][0]["replaces_existing_split"]
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 2
    assert _groups(conn, cfg)["Groceries"] == 9000
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=[], dry_run=False)
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0
    assert _groups(conn, cfg)["Shopping"] == 10000


def test_split_income_and_cents_exactness(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", 10001, "CLIENT PAY")
    parts = [{"amount": "33.34", "category": "Income: Freelance"}, {"amount": "66.67", "category": "Income"}]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    assert sum(r["amount"] for r in conn.execute("SELECT amount FROM txn_lines")) == 10001


# --- dismiss_recurring -----------------------------------------------------------------------


def _netflix(conn, cfg, accounts):
    for m in ("07", "08", "09", "10"):
        add_txn(conn, accounts["card"], f"2026-{m}-01", -1599, "NETFLIX.COM", "Subscriptions")
    conn.commit()
    assert refresh_recurring(conn, cfg, today=TODAY) == 1
    return conn.execute("SELECT merchant FROM recurring").fetchone()[0]


def test_dismiss_recurring_dry_run_apply_undo(conn, cfg, accounts):
    merchant = _netflix(conn, cfg, accounts)
    plan = analysis.dismiss_recurring(conn, cfg, merchant=merchant.upper())
    assert plan["dry_run"] and plan["changes"][0]["action"] == "dismiss"
    assert conn.execute("SELECT COUNT(*) FROM recurring").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM recurring_dismissed").fetchone()[0] == 0

    analysis.dismiss_recurring(conn, cfg, merchant=merchant, dry_run=False)
    assert conn.execute("SELECT COUNT(*) FROM recurring").fetchone()[0] == 0
    assert refresh_recurring(conn, cfg, today=TODAY) == 0  # stays dismissed

    plan = analysis.dismiss_recurring(conn, cfg, merchant=merchant, undo=True)
    assert plan["changes"][0]["action"] == "restore" and plan["dry_run"]
    analysis.dismiss_recurring(conn, cfg, merchant=merchant, undo=True, dry_run=False)
    assert conn.execute("SELECT COUNT(*) FROM recurring_dismissed").fetchone()[0] == 0
    assert refresh_recurring(conn, cfg, today=TODAY) == 1  # restored by the next refresh


def test_dismiss_recurring_errors(conn, cfg, accounts):
    merchant = _netflix(conn, cfg, accounts)
    with pytest.raises(GuardError, match="no detected recurring"):
        analysis.dismiss_recurring(conn, cfg, merchant="Nothing")
    with pytest.raises(GuardError, match="nothing matching is dismissed"):
        analysis.dismiss_recurring(conn, cfg, merchant=merchant, undo=True)
    with pytest.raises(GuardError):
        analysis.dismiss_recurring(conn, cfg, merchant=merchant, direction="sideways")
    with pytest.raises(GuardError, match="unknown account"):
        analysis.dismiss_recurring(conn, cfg, merchant=merchant, account="Nope")
    with pytest.raises(GuardError):
        analysis.dismiss_recurring(conn, cfg, merchant="")


# --- set_budget ------------------------------------------------------------------------------


HEADER = "# my hand-written header\n# Monthly budgets in dollars.\n"


def test_set_budget_writes_toml_with_backup_and_budget_status_follows(conn, cfg, accounts):
    cfg.budgets_file.write_text(HEADER + '\n[[budget]]\ncategory = "Dining"\nmonthly = 400.00\n', encoding="utf-8")
    add_txn(conn, accounts["checking"], "2026-10-02", -12000, "RESTAURANT", "Dining")
    items = [{"category": "Dining", "monthly": 350.5, "rollover": True}, {"category": "Groceries", "monthly": 600}]

    plan = analysis.set_budget(conn, cfg, items)
    assert plan["dry_run"] and {c["action"] for c in plan["changes"]} == {"update", "add"}
    assert plan["changes"][0]["before"]["monthly"]["minor"] == 40000
    assert [b.monthly for b in load_budgets(cfg)] == [40000]  # untouched
    assert not cfg.budgets_file.with_suffix(".toml.bak").exists()

    analysis.set_budget(conn, cfg, items, dry_run=False)
    text = cfg.budgets_file.read_text()
    assert text.startswith(HEADER) and "# updated by set_budget at" in text
    assert cfg.budgets_file.with_suffix(".toml.bak").read_text().count("400.00") == 1
    assert stat.S_IMODE(cfg.budgets_file.stat().st_mode) == 0o600
    got = {b.category: (b.monthly, b.rollover) for b in load_budgets(cfg)}
    assert got == {"Dining": (35050, True), "Groceries": (60000, False)}

    status = analysis.budget_status(conn, cfg, month="2026-10", today=TODAY)
    dining = next(b for b in status["budgets"] if b["category"] == "Dining")
    assert (
        dining["spent"]["minor"] == 12000 and dining["budget"]["minor"] == 70100
    )  # rollover carries last month unspent
    assert {b["category"] for b in status["budgets"]} == {"Dining", "Groceries"}


def test_set_budget_remove_and_idempotent_header(conn, cfg):
    analysis.set_budget(conn, cfg, [{"category": "Dining", "monthly": 400}], dry_run=False)
    analysis.set_budget(conn, cfg, [{"category": "dining", "monthly": 410}], dry_run=False)  # case-insensitive
    assert cfg.budgets_file.read_text().count("# updated by set_budget") == 1
    assert [(b.category, b.monthly) for b in load_budgets(cfg)] == [("Dining", 41000)]
    analysis.set_budget(conn, cfg, [{"category": "Dining", "monthly": None}], dry_run=False)
    assert load_budgets(cfg) == []
    assert "[[budget]]" not in cfg.budgets_file.read_text().splitlines()


def test_set_budget_validation_and_special_characters(conn, cfg):
    for bad in (
        [{"category": "", "monthly": 5}],
        [{"category": "A"}],
        [{"category": "A", "monthly": 0}],
        [{"category": "A", "monthly": -5}],
        [{"category": "A", "monthly": "abc"}],
        [{"category": "A", "monthly": 5, "rollover": "yes"}],
        [{"category": "A", "monthly": 5}, {"category": "a", "monthly": 6}],
        [{"category": "Never", "monthly": None}],
    ):
        with pytest.raises(GuardError):
            analysis.set_budget(conn, cfg, bad)
    tricky = 'Dining "out" \\ & é'
    analysis.set_budget(conn, cfg, [{"category": tricky, "monthly": 10}], dry_run=False)
    assert load_budgets(cfg)[0].category == tricky


def test_set_budget_refuses_to_clobber_a_broken_file(conn, cfg):
    cfg.budgets_file.write_text("this is = not [valid toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        analysis.set_budget(conn, cfg, [{"category": "Dining", "monthly": 5}], dry_run=False)
    assert cfg.budgets_file.read_text() == "this is = not [valid toml"


# --- edits on split parents ----------------------------------------------------------------------


def _split_parent(conn, cfg, accounts):
    parent = add_txn(conn, accounts["card"], "2026-10-05", -10000, "AMZN MKTP", "Shopping")
    plain = add_txn(conn, accounts["card"], "2026-10-06", -2000, "AMZN MKTP", "Shopping")
    parts = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Business: Supplies"}]
    analysis.split_transaction(conn, cfg, transaction_id=parent, parts=parts, dry_run=False)
    return parent, plain


def test_apply_categories_skips_split_parents(conn, cfg, accounts):
    parent, plain = _split_parent(conn, cfg, accounts)
    plan = analysis.apply_categories(conn, cfg, [{"merchant": "Amzn Mktp", "category": "Gifts"}], dry_run=False)
    item = plan["changes"][0]
    assert item["matched_transactions"] == 1 and item["skipped_split_parents"] == 1
    assert "split_transaction" in item["note"]
    cats = {r["id"]: r["category_id"] for r in conn.execute("SELECT id, category_id FROM transactions")}
    assert cats[plain] != cats[parent]
    assert plan["transactions_affected"] == 1


def test_set_scope_skips_split_parents(conn, cfg, accounts):
    parent, plain = _split_parent(conn, cfg, accounts)
    plan = tax.set_scope(conn, cfg, [{"transaction_ids": [parent, plain], "scope": "business"}], dry_run=False)
    item = plan["changes"][0]
    assert item["matched_transactions"] == 1 and item["skipped_split_parents"] == 1
    assert "split_transaction" in item["note"]
    scopes = {r["id"]: r["scope"] for r in conn.execute("SELECT id, scope FROM transactions")}
    assert scopes[plain] == "business" and scopes[parent] is None


def test_edits_without_split_parents_report_zero_skipped(conn, cfg, accounts):
    t = add_txn(conn, accounts["card"], "2026-10-05", -1000, "KROGER")
    item = analysis.apply_categories(conn, cfg, [{"transaction_ids": [t], "category": "Groceries"}])["changes"][0]
    assert item["skipped_split_parents"] == 0 and "note" not in item
