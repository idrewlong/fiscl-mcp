"""Phase 3 slice C: record the 1099s you received and reconcile them against business income fiscl sees."""

from __future__ import annotations

import json
import sqlite3

import pytest
from mcp import Client

from fiscl import tax
from fiscl.db import MIGRATIONS, connect
from fiscl.demo import load_demo
from fiscl.guard import GuardError
from fiscl.server import build_server
from fiscl.tax import forms1099

from .conftest import TODAY, add_txn


@pytest.fixture
def demo(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    return cfg


def add_form(conn, cfg, **kw):
    item = {"action": "add", "tax_year": 2026, "form": "nec", "payer": "Acme Studio", "amount": 5000}
    item.update(kw)
    return tax.record_1099(conn, cfg, [item], dry_run=False, today=TODAY)


def rows(conn):
    return conn.execute("SELECT * FROM forms_1099 ORDER BY id").fetchall()


# --- migration ----------------------------------------------------------------------------


def test_migration_7_creates_the_table_and_is_reversible(cfg):
    assert len(MIGRATIONS) >= 7
    raw = sqlite3.connect(cfg.db_path)
    for sql in MIGRATIONS[:6]:
        raw.executescript(sql)
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    raw.execute("INSERT INTO schema_version VALUES (6)")
    raw.commit()
    raw.close()
    conn = connect(cfg)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == len(MIGRATIONS)
        assert list(cfg.backups.glob("fiscl-*-pre-migration.db"))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(forms_1099)")}
        assert {"tax_year", "form", "payer", "amount", "match_merchants", "note", "created_at", "updated_at"} <= cols
        conn.execute("DROP TABLE forms_1099")  # the documented Down
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'forms_1099'").fetchone()
    finally:
        conn.close()


def test_table_constraints(conn):
    ins = "INSERT INTO forms_1099 (tax_year, form, payer, amount, created_at, updated_at) VALUES (?, ?, ?, ?, 'x', 'x')"
    conn.execute(ins, (2026, "nec", "Acme", 100))
    for bad in ((2026, "w2", "B", 100), (2026, "nec", "C", 0), (1999, "nec", "D", 100), (2026, "NEC", "acme", 100)):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins, bad)


# --- thresholds ---------------------------------------------------------------------------


def test_thresholds_are_looked_up_by_payment_year():
    assert forms1099.nec_misc_threshold(2024) == 60_000
    assert forms1099.nec_misc_threshold(2025) == 60_000
    assert forms1099.nec_misc_threshold(2026) == 200_000
    assert forms1099.nec_misc_threshold(2027) is None  # inflation-indexed, not published
    assert forms1099.K_TPSO_AMOUNT_MINOR == 2_000_000 and forms1099.K_TPSO_TRANSACTIONS == 200


# --- record_1099 --------------------------------------------------------------------------


def test_dry_run_is_the_default_and_changes_nothing(conn, cfg):
    item = {"action": "add", "tax_year": 2026, "form": "nec", "payer": "Acme Studio", "amount": 5000}
    r = tax.record_1099(conn, cfg, [item], today=TODAY)
    assert r["dry_run"] is True and rows(conn) == []
    assert r["changes"][0]["action"] == "add" and r["changes"][0]["after"]["amount"]["minor"] == 500_000
    assert r["changes"][0]["id"] is None and "dry_run=false" in r["next_step"]


def test_add_update_remove_round_trip(conn, cfg):
    r = add_form(conn, cfg, match_merchants=["Acme Studio", "acme studio", "Acme Pay"], note="  hello  ")
    assert r["dry_run"] is False
    fid = r["changes"][0]["id"]
    (row,) = rows(conn)
    assert row["id"] == fid and row["amount"] == 500_000 and row["payer"] == "Acme Studio" and row["note"] == "hello"
    assert json.loads(row["match_merchants"]) == ["Acme Studio", "Acme Pay"]  # case-insensitive dedupe

    plan = tax.record_1099(conn, cfg, [{"action": "update", "id": fid, "amount": 4800.5}], today=TODAY)
    assert plan["changes"][0]["before"]["amount"]["minor"] == 500_000
    assert plan["changes"][0]["after"]["amount"]["minor"] == 480_050
    assert rows(conn)[0]["amount"] == 500_000  # dry run
    tax.record_1099(conn, cfg, [{"action": "update", "id": fid, "amount": 4800.5, "form": "misc"}], False, TODAY)
    (row,) = rows(conn)
    assert row["amount"] == 480_050 and row["form"] == "misc" and row["payer"] == "Acme Studio"
    assert row["updated_at"] >= row["created_at"]

    tax.record_1099(conn, cfg, [{"action": "remove", "id": fid}], dry_run=True, today=TODAY)
    assert len(rows(conn)) == 1
    tax.record_1099(conn, cfg, [{"action": "remove", "id": fid}], dry_run=False, today=TODAY)
    assert rows(conn) == []


def test_duplicate_is_refused_even_with_different_case(conn, cfg):
    add_form(conn, cfg)
    with pytest.raises(GuardError) as exc:
        add_form(conn, cfg, payer="ACME studio")
    assert exc.value.code == "exists"
    add_form(conn, cfg, form="k")  # same payer, different form: allowed
    add_form(conn, cfg, tax_year=2025)  # different year: allowed


def test_duplicate_inside_one_batch_is_refused_and_nothing_is_written(conn, cfg):
    two = [
        {"action": "add", "tax_year": 2026, "form": "nec", "payer": "Beta LLC", "amount": 10},
        {"action": "add", "tax_year": 2026, "form": "nec", "payer": "BETA llc", "amount": 20},
    ]
    with pytest.raises(GuardError) as exc:
        tax.record_1099(conn, cfg, two, dry_run=False, today=TODAY)
    assert exc.value.code == "exists" and rows(conn) == []


def test_update_to_an_existing_key_is_refused(conn, cfg):
    add_form(conn, cfg)
    r = add_form(conn, cfg, payer="Beta LLC")
    with pytest.raises(GuardError) as exc:
        tax.record_1099(conn, cfg, [{"action": "update", "id": r["changes"][0]["id"], "payer": "acme STUDIO"}], False)
    assert exc.value.code == "exists"


def test_not_found(conn, cfg):
    for action in ("update", "remove"):
        with pytest.raises(GuardError) as exc:
            tax.record_1099(conn, cfg, [{"action": action, "id": 999, "amount": 5}], dry_run=False, today=TODAY)
        assert exc.value.code == "not_found"


@pytest.mark.parametrize(
    "patch",
    [
        {"action": "rename"},
        {"form": "w2"},
        {"form": None},
        {"amount": 0},
        {"amount": -5},
        {"amount": None},
        {"amount": True},
        {"amount": "abc"},
        {"tax_year": 1999},
        {"tax_year": 2999},
        {"tax_year": "2026"},
        {"payer": ""},
        {"payer": "   "},
        {"payer": "x" * 81},
        {"match_merchants": "Acme"},
        {"match_merchants": [str(i) for i in range(11)]},
        {"match_merchants": [""]},
        {"match_merchants": [5]},
        {"note": "n" * 201},
        {"bogus": 1},
    ],
)
def test_invalid_items_are_rejected(conn, cfg, patch):
    item = {"action": "add", "tax_year": 2026, "form": "nec", "payer": "Acme Studio", "amount": 5000}
    item.update(patch)
    with pytest.raises(GuardError) as exc:
        tax.record_1099(conn, cfg, [item], dry_run=False, today=TODAY)
    assert exc.value.code == "invalid" and "Acme" not in str(exc.value)
    assert rows(conn) == []


def test_update_and_remove_need_an_id_and_batches_are_bounded(conn, cfg):
    for item in ({"action": "update", "amount": 5}, {"action": "remove"}, {"action": "update", "id": 1}):
        with pytest.raises(GuardError):
            tax.record_1099(conn, cfg, [item], today=TODAY)
    for items in ([], "x", [{"action": "add"}] * 51):
        with pytest.raises(GuardError):
            tax.record_1099(conn, cfg, items, today=TODAY)
    add_form(conn, cfg)
    with pytest.raises(GuardError):  # the same id twice in one call
        tax.record_1099(conn, cfg, [{"action": "remove", "id": 1}, {"action": "update", "id": 1, "amount": 5}])


def test_error_text_never_contains_payer_merchant_or_amount(conn, cfg):
    name = "ZZPAYERZZ"
    add_form(conn, cfg, payer=name, amount=1234.56)
    bad_calls = [
        [{"action": "add", "tax_year": 2026, "form": "nec", "payer": name, "amount": 99}],  # exists
        [{"action": "add", "tax_year": 2026, "form": "bogus", "payer": name, "amount": 99}],
        [{"action": "add", "tax_year": 2026, "form": "nec", "payer": name, "amount": -77.77}],
        [{"action": "add", "tax_year": 1990, "form": "nec", "payer": name, "amount": 99}],
        [
            {
                "action": "add",
                "tax_year": 2026,
                "form": "nec",
                "payer": name,
                "amount": 9,
                "match_merchants": [name] * 11,
            }
        ],
        [{"action": "update", "id": 4242, "payer": name, "amount": 88.88}],
        [{"action": "add", "tax_year": 2026, "form": "nec", "payer": name, "amount": 9, name: 1}],
    ]
    for items in bad_calls:
        with pytest.raises(GuardError) as exc:
            tax.record_1099(conn, cfg, items, dry_run=False, today=TODAY)
        text = str(exc.value)
        assert name not in text and "1234" not in text and "77.77" not in text and "88.88" not in text, text


async def test_mcp_errors_and_audit_log_never_contain_names(demo):
    name = "ZZPAYERZZ"
    async with Client(build_server(demo)) as c:
        tool = {t.name: t for t in (await c.list_tools()).tools}["record_1099"]
        a = tool.annotations
        assert a.read_only_hint is False and a.destructive_hint is True and a.open_world_hint is False
        assert a.idempotent_hint is False and "dry_run" in tool.description
        item = {"action": "add", "tax_year": 2026, "form": "nec", "payer": name, "amount": 100}
        dry = await c.call_tool("record_1099", {"items": [item]})
        assert not dry.is_error and dry.structured_content["dry_run"] is True
        assert (await c.call_tool("record_1099", {"items": [item], "dry_run": False})).is_error is False
        for bad in (item, {**item, "form": "zz"}, {**item, "amount": -4321}):
            r = await c.call_tool("record_1099", {"items": [bad], "dry_run": False})
            assert r.is_error and name not in r.content[0].text
        r = await c.call_tool("reconcile_1099", {"tax_year": 1800})
        assert r.is_error
    log = (demo.logs / "audit.log").read_text()
    assert name not in log and "4321" not in log and "record_1099" in log


# --- reconcile_1099 -----------------------------------------------------------------------


def test_reported_higher_than_matched_deposits(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 300_000, "ACME STUDIO")
    add_txn(conn, accounts["biz"], "2026-06-01", 180_000, "ACME STUDIO")  # $4,800 total
    add_txn(conn, accounts["checking"], "2026-07-01", 90_000, "ACME STUDIO")  # personal scope: never counted
    conn.commit()
    add_form(conn, cfg)  # $5,000, no match_merchants: the exact payer name matches
    r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    (f,) = r["forms"]
    assert f["status"] == "reported_higher" and f["form"] == "nec" and f["payer"] == "Acme Studio"
    assert f["reported"]["minor"] == 500_000 and f["matched_income"]["minor"] == 480_000
    assert f["difference"]["display"] == "$200.00" and f["matched_count"] == 2
    assert f["questions"] and all(q.endswith("?") for q in f["questions"])
    assert "Acme" not in " ".join(f["questions"])
    assert r["totals"]["reported"]["minor"] == 500_000 and r["totals"]["matched_income"]["minor"] == 480_000
    assert r["totals"]["business_gross_receipts"]["minor"] == 480_000
    assert tax.DISCLAIMER in r["notes"] and r["meta"]["row_count"] == 1


def test_match_within_a_dollar_matches_and_has_no_questions(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 499_950, "ACME STUDIO")
    conn.commit()
    add_form(conn, cfg)
    (f,) = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)["forms"]
    assert f["status"] == "matches" and f["difference"]["minor"] == 50 and f["questions"] == []


def test_reported_lower_and_no_matching_income(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 700_000, "ACME STUDIO")
    conn.commit()
    add_form(conn, cfg)
    add_form(conn, cfg, payer="Ghost Co", amount=1000)
    by = {f["payer"]: f for f in tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)["forms"]}
    assert by["Acme Studio"]["status"] == "reported_lower" and by["Acme Studio"]["difference"]["minor"] == -200_000
    ghost = by["Ghost Co"]
    assert ghost["status"] == "no_matching_income" and ghost["matched_count"] == 0
    assert any("Which deposits" in q for q in ghost["questions"])
    assert any("match_merchants" in q for q in ghost["questions"])


def test_match_merchants_cover_processor_names(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 200_000, "STRIPE TRANSFER")
    add_txn(conn, accounts["biz"], "2026-04-01", 300_000, "stripe transfer")
    conn.commit()
    add_form(conn, cfg, match_merchants=["Stripe Transfer"], amount=5000)
    (f,) = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)["forms"]
    assert f["status"] == "matches" and f["matched_count"] == 2


def test_only_the_tax_year_counts(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2025-12-31", 100_000, "ACME STUDIO")
    add_txn(conn, accounts["biz"], "2026-01-01", 200_000, "ACME STUDIO")
    add_txn(conn, accounts["biz"], "2027-01-01", 400_000, "ACME STUDIO")
    conn.commit()
    add_form(conn, cfg, amount=2000)
    (f,) = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)["forms"]
    assert f["matched_income"]["minor"] == 200_000 and f["status"] == "matches"


def test_k_form_asks_about_gross_versus_net(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 900_000, "ETSY PAYOUT")
    conn.commit()
    add_form(conn, cfg, form="k", payer="Etsy", amount=10_000, match_merchants=["Etsy Payout"])
    (f,) = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)["forms"]
    assert f["status"] == "reported_higher"
    assert any("fees, refunds" in q for q in f["questions"])


def test_k_threshold_question_depends_on_the_amount(conn, cfg, accounts):
    add_form(conn, cfg, form="k", payer="PayPal", amount=15_000)
    add_form(conn, cfg, form="k", payer="Square", amount=25_000)
    by = {f["payer"]: f for f in tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)["forms"]}
    assert any("$20,000" in q for q in by["PayPal"]["questions"])
    assert not any("$20,000" in q for q in by["Square"]["questions"])


def test_same_payer_on_two_forms_raises_a_double_count_question(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 500_000, "ACME STUDIO")
    conn.commit()
    add_form(conn, cfg)
    add_form(conn, cfg, form="k", amount=5000)
    r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    assert all(any("double" in q.lower() for q in f["questions"]) for f in r["forms"])
    # The same deposits are not counted twice in the totals.
    assert r["totals"]["matched_income"]["minor"] == 500_000 and r["totals"]["reported"]["minor"] == 1_000_000


def test_unreported_payers_use_the_threshold_for_the_payment_year(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 200_000, "BETA LLC")  # exactly $2,000: listed (>=)
    add_txn(conn, accounts["biz"], "2026-03-02", 199_999, "GAMMA INC")  # $1,999.99: below the 2026 threshold
    add_txn(conn, accounts["checking"], "2026-03-03", 900_000, "PERSONAL PERSON")  # personal scope
    add_txn(conn, accounts["biz"], "2025-03-01", 60_000, "GAMMA INC")  # $600 in 2025: listed for 2025
    add_txn(conn, accounts["biz"], "2025-03-02", 59_999, "DELTA INC")
    conn.commit()
    r26 = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    assert [p["merchant"] for p in r26["unreported_payers"]] == ["Beta LLC"]
    p = r26["unreported_payers"][0]
    assert p["income"]["minor"] == 200_000 and p["count"] == 1 and p["question"].endswith("?")
    assert "Beta" not in p["question"] and "reporting threshold" in p["question"]
    assert r26["thresholds"]["nec_misc"]["amount"]["minor"] == 200_000
    assert r26["thresholds"]["nec_misc"]["comparison"] == "at least"
    r25 = tax.reconcile_1099(conn, cfg, tax_year=2025, today=TODAY)
    assert [x["merchant"] for x in r25["unreported_payers"]] == ["Gamma Inc"]
    assert r25["thresholds"]["nec_misc"]["amount"]["minor"] == 60_000


def test_a_payer_with_a_form_is_not_reported_as_unreported(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 500_000, "ACME STUDIO")
    add_txn(conn, accounts["biz"], "2026-03-02", 500_000, "STRIPE")
    conn.commit()
    add_form(conn, cfg, match_merchants=["Stripe"])
    r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    assert [p["merchant"] for p in r["unreported_payers"]] == ["Acme Studio"]  # Stripe is matched to a form, Acme isn't


def test_unpublished_year_threshold_falls_back_with_a_note(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2027-03-01", 250_000, "BETA LLC")
    conn.commit()
    r = tax.reconcile_1099(conn, cfg, tax_year=2027, today=TODAY)
    assert [p["merchant"] for p in r["unreported_payers"]] == ["Beta LLC"]
    assert any("inflation" in n for n in r["notes"])
    assert r["thresholds"]["nec_misc"]["published"] is False


def test_no_forms_entered_still_works_and_says_how_to_add_them(conn, cfg, accounts):
    r = tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    assert r["forms"] == [] and r["unreported_payers"] == []
    assert any("record_1099" in n for n in r["notes"])


def test_reconcile_is_read_only_and_validates_year(conn, cfg, accounts):
    add_form(conn, cfg)
    before = conn.execute("SELECT * FROM forms_1099").fetchall()
    tax.reconcile_1099(conn, cfg, tax_year=2026, today=TODAY)
    assert [tuple(r) for r in conn.execute("SELECT * FROM forms_1099")] == [tuple(r) for r in before]
    for bad in (1999, 3000, "2026"):
        with pytest.raises(GuardError):
            tax.reconcile_1099(conn, cfg, tax_year=bad, today=TODAY)
    assert tax.reconcile_1099(conn, cfg, today=TODAY)["tax_year"] == TODAY.year


async def test_reconcile_annotations(demo):
    async with Client(build_server(demo)) as c:
        tool = {t.name: t for t in (await c.list_tools()).tools}["reconcile_1099"]
        a = tool.annotations
        assert a.read_only_hint is True and a.destructive_hint is False and a.open_world_hint is False
        r = await c.call_tool("reconcile_1099", {"tax_year": 2026})
        assert not r.is_error and tax.DISCLAIMER in r.structured_content["notes"]
