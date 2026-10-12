"""Phase 3 slice A: export_schedule_c (dry run, safe writer, CSV/Markdown injection escaping)."""

from __future__ import annotations

import csv
import io
import os
import re
import stat
import sys
from datetime import datetime

import pytest

from fiscl import tax
from fiscl.config import load_config
from fiscl.demo import load_demo
from fiscl.guard import GuardError

from .conftest import TODAY, add_txn

NOW = datetime(2026, 10, 12, 9, 30, 0)
NAME_RE = re.compile(r"^schedule-c-\d{4}-b\d+-\d{8}T\d{6}(-detail)?\.(csv|md)$")
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and symlinks")


def export(conn, cfg, **kw):
    kw.setdefault("year", 2026)
    return tax.export_schedule_c(conn, cfg, today=TODAY, now=NOW, **kw)


def files_on_disk(cfg):
    return sorted(p.name for p in cfg.exports.iterdir()) if cfg.exports.exists() else []


def read_csv(path):
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))


@pytest.fixture
def seeded(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 500_000, "Client payment")
    add_txn(conn, accounts["biz"], "2026-03-02", -12_345, "Staples", category="Shopping")
    conn.commit()
    return accounts


def test_dry_run_is_default_and_writes_nothing(conn, cfg, seeded):
    r = export(conn, cfg)
    assert r["dry_run"] is True and "written" not in r
    paths = [f["path"] for f in r["files"]]
    assert paths == ["exports/schedule-c-2026-b1-20261012T093000.csv", "exports/schedule-c-2026-b1-20261012T093000.md"]
    assert r["files"][0]["kind"] == "summary_csv" and r["files"][0]["rows"] > 5
    assert r["files"][1]["kind"] == "markdown"
    assert files_on_disk(cfg) == []
    assert r["summary"]["net_profit"]["minor"] == 500_000 - 12_345
    assert tax.DISCLAIMER in r["notes"]
    assert "meta" in r
    assert not any(str(cfg.home) in p for p in paths)


@posix
def test_apply_writes_private_files(conn, cfg, seeded):
    r = export(conn, cfg, include_transactions=True, dry_run=False)
    assert r["dry_run"] is False and r["written"] is True and r["data_dir_relative"] is True
    assert len(r["files"]) == 3 and r["files"][2]["kind"] == "detail_csv"
    assert stat.S_IMODE(os.stat(cfg.exports).st_mode) == 0o700
    for f in r["files"]:
        assert NAME_RE.match(os.path.basename(f["path"])), f
        assert stat.S_IMODE(os.stat(cfg.home / f["path"]).st_mode) == 0o600
    assert not any(n.endswith(".tmp") for n in files_on_disk(cfg))


def test_summary_net_profit_matches_draft_on_demo(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    draft = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    r = export(conn, cfg, dry_run=False)
    rows = read_csv(cfg.home / r["files"][0]["path"])
    by_section = {row["section"]: row for row in rows}
    cents = draft["net_profit"]["minor"]
    assert by_section["net_profit"]["amount"] == f"{cents // 100}.{cents % 100:02d}"
    assert by_section["disclaimer"]["note"] == tax.DISCLAIMER
    assert by_section["gross_receipts"]["line"] == "1"
    assert len(rows) == r["files"][0]["rows"]


def test_home_office_row_in_summary(conn, cfg, seeded):
    cfg.config_file.write_text("[tax.home_office.2026]\nsquare_feet = 200\n", encoding="utf-8")
    cfg = load_config(cfg.home)
    r = export(conn, cfg, formats=["csv"], dry_run=False)
    rows = {row["section"]: row for row in read_csv(cfg.home / r["files"][0]["path"])}
    assert rows["home_office"]["line"] == "30" and rows["home_office"]["amount"] == "1000.00"
    assert rows["net_profit"]["amount"] == "3876.55"
    assert len(r["files"]) == 1


def test_same_second_second_export_refuses_to_overwrite(conn, cfg, seeded):
    export(conn, cfg, dry_run=False)
    before = {n: (cfg.exports / n).read_bytes() for n in files_on_disk(cfg)}
    with pytest.raises(GuardError) as e:
        export(conn, cfg, dry_run=False)
    assert e.value.code == "exists"
    assert {n: (cfg.exports / n).read_bytes() for n in files_on_disk(cfg)} == before


@posix
def test_symlinked_exports_dir_is_refused(conn, cfg, seeded, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.rmdir(cfg.exports)
    os.symlink(target, cfg.exports)
    for dry in (True, False):
        with pytest.raises(GuardError) as e:
            export(conn, cfg, dry_run=dry)
        assert e.value.code == "invalid" and "exports folder" in str(e.value)
    assert list(target.iterdir()) == []


@posix
def test_pre_existing_symlink_file_is_not_followed(conn, cfg, seeded, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    os.symlink(victim, cfg.exports / "schedule-c-2026-b1-20261012T093000.csv.tmp")
    with pytest.raises(GuardError):
        export(conn, cfg, dry_run=False)
    assert victim.read_text(encoding="utf-8") == "keep"


@posix
def test_failed_write_leaves_no_partial_files(conn, cfg, seeded, monkeypatch):
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        export(conn, cfg, dry_run=False)
    assert files_on_disk(cfg) == []


def test_missing_exports_dir_is_created_private(conn, cfg, seeded):
    os.rmdir(cfg.exports)
    export(conn, cfg, dry_run=False)
    assert cfg.exports.is_dir()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(cfg.exports).st_mode) == 0o700


@pytest.mark.parametrize("year", ["../../x", 1999, 3000, "2026", True])
def test_bad_year_is_invalid_and_writes_nothing(conn, cfg, seeded, year):
    with pytest.raises(GuardError) as e:
        export(conn, cfg, year=year, dry_run=False)
    assert e.value.code == "invalid"
    assert files_on_disk(cfg) == []


@pytest.mark.parametrize("formats", [["pdf"], [], ["csv", "../x"], "csv"])
def test_bad_formats_are_invalid(conn, cfg, seeded, formats):
    with pytest.raises(GuardError) as e:
        export(conn, cfg, formats=formats)
    assert e.value.code == "invalid"


def test_csv_formula_injection_is_neutralised(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-02", -5_000, "x", category='=HYPERLINK("x")')
    add_txn(conn, accounts["biz"], "2026-03-03", -2_000, "y", category="Shopping")
    conn.execute("UPDATE transactions SET merchant = '+cmd', note = '@SUM(A1)' WHERE raw_description = 'y'")
    conn.execute("UPDATE accounts SET name = '-evil' WHERE id = ?", (accounts["biz"],))
    conn.commit()
    r = export(conn, cfg, include_transactions=True, dry_run=False)
    summary = (cfg.home / r["files"][0]["path"]).read_text(encoding="utf-8")
    assert "'=HYPERLINK" in summary
    assert not any(row["category"].startswith("=") for row in read_csv(cfg.home / r["files"][0]["path"]))
    detail = read_csv(cfg.home / r["files"][2]["path"])
    merchants = {row["merchant"] for row in detail}
    assert "'+cmd" in merchants
    assert {row["note"] for row in detail} >= {"'@SUM(A1)"}
    assert all(row["account"] == "'-evil" for row in detail)
    for path in (cfg.home / r["files"][0]["path"], cfg.home / r["files"][2]["path"]):
        for row in read_csv(path):
            for k, v in row.items():
                if k in ("amount", "full_amount", "count", "line", "transaction_id", "split_id", "date"):
                    continue
                assert not v or v[0] not in "=+-@\t\r＝＋－＠", (k, v)


def test_detail_csv_negative_amounts_stay_numeric(conn, cfg, seeded):
    r = export(conn, cfg, include_transactions=True, dry_run=False)
    detail = read_csv(cfg.home / r["files"][2]["path"])
    assert {row["amount"] for row in detail} == {"5000.00", "-123.45"}
    assert list(detail[0]) == [
        "date",
        "account",
        "merchant",
        "amount",
        "category",
        "line",
        "scope",
        "business",
        "note",
        "transaction_id",
        "split_id",
    ]


def test_markdown_escapes_bank_text(conn, cfg, accounts):
    add_txn(conn, accounts["biz"], "2026-03-02", -5_000, "x", category="[click](http://x)")
    add_txn(conn, accounts["biz"], "2026-03-03", -5_000, "y", category="<script>alert(1)</script> | *b* _i_ #h")
    conn.commit()
    r = export(conn, cfg, formats=["markdown"], dry_run=False)
    text = (cfg.home / r["files"][0]["path"]).read_text(encoding="utf-8")
    assert r"\[click\]\(http://x\)" in text
    body = "\n".join(line for line in text.splitlines() if not line.startswith("> "))  # blockquote markers are ours
    assert "<" not in text and ">" not in body
    assert "&lt;script&gt;" in text
    assert r"\|" in text and r"\*b\*" in text and r"\_i\_" in text and r"\#h" in text
    assert "not tax advice" in text.lower()
    assert tax.DISCLAIMER in text


def test_redact_descriptions_blanks_merchant_and_note(conn, cfg, seeded):
    cfg.config_file.write_text("[guard]\nredact_descriptions = true\n", encoding="utf-8")
    cfg = load_config(cfg.home)
    conn.execute("UPDATE transactions SET note = 'private' WHERE raw_description = 'Staples'")
    conn.commit()
    r = export(conn, cfg, include_transactions=True, dry_run=False)
    detail = read_csv(cfg.home / r["files"][2]["path"])
    assert all(row["merchant"] == "" and row["note"] == "" for row in detail)
    assert any("redact" in n.lower() for n in r["notes"])


def test_notes_tell_user_to_delete_sent_exports(conn, cfg, seeded):
    assert any("delete" in n.lower() for n in export(conn, cfg)["notes"])


def test_personal_scope_is_not_exported(conn, cfg, seeded):
    add_txn(conn, seeded["checking"], "2026-04-01", -99_999, "Secret hobby", category="Shopping")
    conn.commit()
    r = export(conn, cfg, include_transactions=True, dry_run=False)
    detail = (cfg.home / r["files"][2]["path"]).read_text(encoding="utf-8")
    assert "99999" not in detail and "999.99" not in detail
