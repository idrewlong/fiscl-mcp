"""Phase 3 QA/security fixes: export scrubbing, no-overwrite publish, transactional migrations, rule businesses."""

from __future__ import annotations

import errno
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from fiscl import categorize, db, tax
from fiscl.config import ConfigError
from fiscl.guard import GuardError

from .conftest import TODAY, add_txn

NOW = datetime(2026, 10, 12, 9, 30, 0)
CARD = "4111 1111 1111 1111"
SSN = "123-45-6789"


def export(conn, cfg, **kw):
    kw.setdefault("year", 2026)
    return tax.export_schedule_c(conn, cfg, today=TODAY, now=NOW, **kw)


def all_text(cfg) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in cfg.exports.iterdir())


# --- SEC-1: scrubber ---------------------------------------------------------------------------


def test_exports_never_contain_card_or_ssn_or_long_numbers(conn, cfg, accounts):
    biz = accounts["biz"]
    add_txn(conn, biz, "2026-03-01", -1_000, f"Shop {CARD}", category="Business: Supplies")
    add_txn(conn, biz, "2026-03-02", -1_000, f"Person {SSN}", category="Business: Supplies")
    conn.execute("UPDATE transactions SET note = 'invoice 1234567890 for you' WHERE raw_description LIKE 'Shop%'")
    conn.execute("UPDATE transactions SET merchant = ? WHERE raw_description LIKE 'Shop%'", (f"Shop {CARD}",))
    conn.execute("UPDATE transactions SET merchant = ? WHERE raw_description LIKE 'Person%'", (f"Person {SSN}",))
    add_txn(conn, biz, "2026-03-03", -500, "Other", category=f"Business: Card {CARD} {SSN}")
    add_txn(conn, biz, "2026-03-04", 5_000_000_00, "Big client", category="Income")
    conn.commit()
    export(conn, cfg, dry_run=False, include_transactions=True)
    text = all_text(cfg)
    for secret in (CARD, "4111111111111111", "1111 1111 1111", SSN, "1234567890"):
        assert secret not in text
    assert "••••1111" in text
    assert "•••-••-••••" in text
    assert "••••7890" in text
    # amounts are untouched, even 9+ digit ones
    assert "5000000.00" in text and "$5,000,000.00" in text


def test_redact_descriptions_still_blanks_merchant_and_note(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", -1_000, "Secret Shop", category="Business: Supplies")
    conn.commit()
    cfg.guard.redact_descriptions = True
    export(conn, cfg, dry_run=False, include_transactions=True)
    assert "Secret Shop" not in all_text(cfg)


# --- SEC-2: no overwrite -----------------------------------------------------------------------


def _seed(conn, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", -1_000, "Zoom", category="Business: Supplies")
    conn.commit()


def test_file_appearing_between_check_and_publish_is_never_overwritten(conn, cfg, accounts, monkeypatch):
    _seed(conn, accounts)
    real_link = os.link
    victim = {}

    def racing_link(src, dst, *a, **kw):
        if not victim and str(dst).endswith(".md"):  # the .csv was already published; the .md name now appears
            victim["path"] = dst
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write("PRECIOUS")
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(GuardError) as ei:
        export(conn, cfg, dry_run=False)
    assert ei.value.code == "exists"
    assert Path(victim["path"]).read_text(encoding="utf-8") == "PRECIOUS"
    # all-or-nothing: our own files are gone, no temp files linger; only the file that was not ours remains
    assert [p.name for p in cfg.exports.iterdir()] == [os.path.basename(victim["path"])]


def test_hardlink_unsupported_falls_back_to_exclusive_create(conn, cfg, accounts, monkeypatch):
    _seed(conn, accounts)

    def no_link(*a, **kw):
        raise OSError(errno.EPERM, "hard links unsupported")

    monkeypatch.setattr(os, "link", no_link)
    export(conn, cfg, dry_run=False)
    names = sorted(p.name for p in cfg.exports.iterdir())
    assert len(names) == 2 and not any(n.endswith(".tmp") for n in names)
    with pytest.raises(GuardError) as ei:  # same second again: the pre-check refuses
        export(conn, cfg, dry_run=False)
    assert ei.value.code == "exists"
    assert sorted(p.name for p in cfg.exports.iterdir()) == names


def test_fallback_does_not_overwrite_a_late_file(conn, cfg, accounts, monkeypatch):
    _seed(conn, accounts)
    real_open = os.open
    state = {"armed": False}

    def no_link(*a, **kw):
        state["armed"] = True
        raise OSError(errno.EPERM, "hard links unsupported")

    def late_open(path, flags, *a, **kw):
        p = str(path)
        if state["armed"] and p.endswith(".csv") and not p.endswith(".tmp"):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("PRECIOUS")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(os, "open", late_open)
    with pytest.raises(GuardError) as ei:
        export(conn, cfg, dry_run=False)
    assert ei.value.code == "exists"
    survivors = list(cfg.exports.iterdir())
    assert len(survivors) == 1 and survivors[0].read_text(encoding="utf-8") == "PRECIOUS"


# --- QA BUG-1 / BUG-3 extras ---------------------------------------------------------------------


def test_www_and_other_schemes_are_neutralized_but_readable():
    from fiscl.tax.export import md_text

    out = md_text("see www.evil.example and ftp://x.example")
    assert "www\\.evil" in out and "ftp\\://" in out
    assert not re.search(r"(?<!\\)(www\.|[a-z]+://)", out)


def test_nan_and_infinite_rates_are_rejected(conn, cfg, accounts):
    _seed(conn, accounts)
    for bad in ("nan", "inf"):
        cfg.config_file.write_text(f"[tax]\nincome_tax_rate_pct = {bad}\n", encoding="utf-8")
        from fiscl.config import load_config

        with pytest.raises(ConfigError, match="between 0 and 60"):
            load_config(cfg.home)


def test_negative_w2_wages_rejected_with_fixed_message(conn, cfg, accounts):
    _seed(conn, accounts)
    with pytest.raises(GuardError) as ei:
        tax.estimated_tax(conn, cfg, year=2026, w2_wages=-1, today=TODAY)
    assert ei.value.code == "invalid" and "-1" not in str(ei.value)


# --- migrations in a transaction -----------------------------------------------------------------


def test_failing_migration_leaves_schema_and_version_unchanged(conn, cfg, monkeypatch):
    before_version = db.schema_version(conn)
    tables_before = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master"))
    bad = "CREATE TABLE half_applied (a INTEGER); INSERT INTO no_such_table VALUES (1);"
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, bad])
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn, cfg)
    assert not conn.in_transaction
    assert db.schema_version(conn) == before_version
    assert sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master")) == tables_before
    assert list(cfg.backups.glob("*pre-migration*.db"))  # the backup still happens first


def test_successful_migration_bumps_version_atomically(conn, cfg, monkeypatch):
    start = db.schema_version(conn)
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, "CREATE TABLE extra_ok (a INTEGER);"])
    db.migrate(conn, cfg)
    assert db.schema_version(conn) == start + 1
    assert conn.execute("SELECT COUNT(*) FROM extra_ok").fetchone()[0] == 0


# --- rules.toml business ---------------------------------------------------------------------------


def _rules(cfg, body: str):
    cfg.rules_file.write_text(body, encoding="utf-8")


def _biz(conn, name):
    conn.execute("INSERT INTO businesses (name, created_at) VALUES (?, '2026-01-01')", (name,))
    conn.commit()
    return conn.execute("SELECT id FROM businesses WHERE name = ?", (name,)).fetchone()[0]


def test_rule_business_fills_empty_business_on_categorized_rows(conn, cfg, accounts):
    bid = _biz(conn, "Studio")
    t1 = add_txn(conn, accounts["biz"], "2026-03-01", -1_000, "Zoom")
    t2 = add_txn(conn, accounts["biz"], "2026-03-02", -2_000, "Other")
    conn.commit()
    _rules(cfg, '[[rule]]\nmerchant = "Zoom"\ncategory = "Business: Software"\nbusiness = "studio"\n')
    categorize.apply_rules(conn, cfg)
    rows = {r["id"]: r["business_id"] for r in conn.execute("SELECT id, business_id FROM transactions")}
    assert rows[t1] == bid and rows[t2] is None


def test_rule_business_never_overrides_manual_business(conn, cfg, accounts):
    _biz(conn, "Studio")
    other = _biz(conn, "Other Co")
    t1 = add_txn(conn, accounts["biz"], "2026-03-01", -1_000, "Zoom")
    conn.execute("UPDATE transactions SET business_id = ? WHERE id = ?", (other, t1))
    conn.commit()
    _rules(cfg, '[[rule]]\nmerchant = "Zoom"\ncategory = "Business: Software"\nbusiness = "Studio"\n')
    categorize.apply_rules(conn, cfg)
    assert conn.execute("SELECT business_id FROM transactions WHERE id = ?", (t1,)).fetchone()[0] == other


def test_rule_business_dry_run_changes_nothing(conn, cfg, accounts):
    _biz(conn, "Studio")
    t1 = add_txn(conn, accounts["biz"], "2026-03-01", -1_000, "Zoom")
    conn.commit()
    _rules(cfg, '[[rule]]\nmerchant = "Zoom"\ncategory = "Business: Software"\nbusiness = "Studio"\n')
    categorize.apply_rules(conn, cfg, dry_run=True)
    assert conn.execute("SELECT business_id FROM transactions WHERE id = ?", (t1,)).fetchone()[0] is None


def test_rule_with_unknown_business_fails_without_echoing_the_name(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", -1_000, "Zoom")
    conn.commit()
    _rules(cfg, '[[rule]]\nmerchant = "Zoom"\ncategory = "X"\nbusiness = "Nonexistent Corp"\n')
    with pytest.raises(ConfigError) as ei:
        categorize.apply_rules(conn, cfg)
    assert "rule 1" in str(ei.value) and "Nonexistent" not in str(ei.value)


def test_rule_with_archived_or_non_string_business_fails(conn, cfg, accounts):
    _biz(conn, "Old Co")
    conn.execute("UPDATE businesses SET archived = 1 WHERE name = 'Old Co'")
    conn.commit()
    _rules(cfg, '[[rule]]\nmerchant = "Zoom"\ncategory = "X"\nbusiness = "Old Co"\n')
    with pytest.raises(ConfigError):
        categorize.apply_rules(conn, cfg)
    _rules(cfg, '[[rule]]\nmerchant = "Zoom"\ncategory = "X"\nbusiness = 5\n')
    with pytest.raises(GuardError):
        categorize.apply_rules(conn, cfg)
