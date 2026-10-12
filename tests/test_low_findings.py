"""Regression tests for the low findings of the final security audit (SEC-9 .. SEC-12 and leftovers)."""

from __future__ import annotations

import getpass
import json
import os
import stat

import pytest

from fiscl import cli, installkey
from fiscl.config import ensure_layout, load_config
from fiscl.db import add_account
from fiscl.demo import CHECKING, has_real_data, load_demo
from fiscl.importers.core import ImportFailed, _resolve_account, import_statements
from fiscl.importers.ofx import OFXError, parse_ofx
from fiscl.importers.types import ParsedStatement, ParsedTxn

from .conftest import TODAY

# --- SEC-10: cloud-sync detection ------------------------------------------------------------------------------------


def test_apple_desktop_and_documents_count_as_synced_only_when_enabled(tmp_path):
    home = tmp_path / "h"
    (home / "Desktop").mkdir(parents=True)
    (home / "Documents").mkdir()
    assert cli.cloud_sync_warning(home / "Desktop" / "fiscl", home=home) is None
    (home / "Library/Mobile Documents/com~apple~CloudDocs/Desktop").mkdir(parents=True)
    assert "iCloud" in cli.cloud_sync_warning(home / "Desktop" / "fiscl", home=home)
    assert cli.cloud_sync_warning(home / "Documents" / "fiscl", home=home) is None  # only Desktop is synced so far
    (home / "Library/Mobile Documents/com~apple~CloudDocs/Documents").mkdir()
    assert "iCloud" in cli.cloud_sync_warning(home / "Documents" / "fiscl", home=home)
    assert cli.cloud_sync_warning(home / "fiscl", home=home) is None  # ~/fiscl itself is local


def test_every_command_warns_on_stderr_only(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "cloud_sync_warning", lambda path, home=None: "synced folder!")
    assert cli.main(["--home", str(cfg.home), "version"]) == 0
    out, err = capsys.readouterr()
    assert "synced folder!" in err
    assert "synced folder!" not in out  # stdout carries MCP for `serve`


# --- SEC-11: demo into a real database -------------------------------------------------------------------------------


def test_demo_refuses_real_data_without_force(cfg, conn, capsys):
    add_account(conn, "My Real Checking", "checking")
    conn.commit()
    assert has_real_data(conn)
    assert cli.main(["--home", str(cfg.home), "demo"]) == 1
    err = capsys.readouterr().err
    assert "fiscl --home /tmp/fiscl-demo demo" in err
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_demo_allowed_on_empty_or_demo_only_and_with_force(cfg, conn, capsys):
    assert not has_real_data(conn)
    assert cli.main(["--home", str(cfg.home), "demo"]) == 0
    assert cli.main(["--home", str(cfg.home), "demo"]) == 0  # demo-only data: reloading is fine
    add_account(conn, "My Real Checking", "checking")
    conn.commit()
    assert cli.main(["--home", str(cfg.home), "demo"]) == 1
    assert cli.main(["--home", str(cfg.home), "demo", "--force"]) == 0


# --- SEC-12: doctor permissions --------------------------------------------------------------------------------------


def test_doctor_repairs_baks_inbox_archives_demo_and_profiles(cfg, conn, capsys):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    files = [
        cfg.home / "config.toml.bak",
        cfg.processed / "old.csv",
        cfg.failed / "bad.csv",
        cfg.failed / "bad.csv.error.txt",
        cfg.profiles_dir / "mine.toml",
        cfg.home / "demo" / "card.csv",
        cfg.home / ".install_key",
    ]
    for f in files:
        f.write_text("x", encoding="utf-8")
        os.chmod(f, 0o644)
    for d in (cfg.processed, cfg.failed, cfg.home / "demo"):
        os.chmod(d, 0o755)
    cli.main(["--home", str(cfg.home), "doctor"])
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    for d in (cfg.processed, cfg.failed, cfg.home / "demo"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


# --- SEC-5 leftovers: no file values in error text -------------------------------------------------------------------


OFX_BAD_DATE = (
    "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>1<ACCTID>99001180<ACCTTYPE>CHECKING"
    "</BANKACCTFROM><BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>SECRET99<TRNAMT>-1.00<FITID>A1<NAME>X</STMTTRN>"
    "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
)


def test_ofx_date_error_names_the_field_not_the_value():
    with pytest.raises(OFXError) as exc:
        parse_ofx(OFX_BAD_DATE)
    assert "SECRET99" not in str(exc.value)
    assert "DTPOSTED" in str(exc.value) and "entry 1" in str(exc.value)


def test_unknown_account_error_does_not_echo_the_name(cfg, conn):
    stmt = ParsedStatement(format="csv", txns=[])
    with pytest.raises(ImportFailed) as exc:
        _resolve_account(conn, cfg, stmt, "Secret Acct Name")
    assert "Secret Acct Name" not in str(exc.value)


# --- OTel hardening --------------------------------------------------------------------------------------------------


def test_otel_disabled_by_default_and_user_value_respected(cfg, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "x")  # registers cleanup for this key
    monkeypatch.delenv("OTEL_SDK_DISABLED")
    assert cli.main(["--home", str(cfg.home), "version"]) == 0
    assert os.environ["OTEL_SDK_DISABLED"] == "true"
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    assert cli.main(["--home", str(cfg.home), "version"]) == 0
    assert os.environ["OTEL_SDK_DISABLED"] == "false"


# --- install key portability -----------------------------------------------------------------------------------------


def _hashed_account(conn):
    add_account(conn, "A", "checking", acct_hash="h")
    conn.commit()


def _answer(monkeypatch, text):
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": text)


def test_key_export_import_roundtrip_keeps_account_hashes(cfg, tmp_path, monkeypatch, capsys):
    key = installkey.install_key(cfg)
    dest = tmp_path / "out" / "key.txt"
    dest.parent.mkdir()
    _answer(monkeypatch, "export")
    assert cli.main(["--home", str(cfg.home), "key", "export", str(dest)]) == 0
    captured = capsys.readouterr()
    assert key.hex() not in captured.out + captured.err  # never printed
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert dest.read_text().strip() == key.hex()
    assert cli.main(["--home", str(cfg.home), "key", "export", str(dest)]) == 1  # refuses to overwrite

    other = load_config(tmp_path / "home2")
    ensure_layout(other)
    installkey.reset_cache()
    assert installkey.install_key(other) != key  # a fresh machine mints its own key
    (other.home / installkey.KEY_FILE).unlink()
    installkey.reset_cache()
    _answer(monkeypatch, "import")
    assert cli.main(["--home", str(other.home), "key", "import", str(dest)]) == 0
    assert installkey.install_key(other) == key
    assert installkey.account_hash(other, "1:2") == installkey.account_hash(cfg, "1:2")


def test_key_export_and_import_need_the_hidden_confirmation(cfg, tmp_path, monkeypatch):
    dest = tmp_path / "k.txt"
    _answer(monkeypatch, "no")
    assert cli.main(["--home", str(cfg.home), "key", "export", str(dest)]) == 1
    assert not dest.exists()
    dest.write_text(os.urandom(32).hex(), encoding="ascii")
    before = installkey.install_key(cfg)
    assert cli.main(["--home", str(cfg.home), "key", "import", str(dest)]) == 1
    assert installkey.install_key(cfg) == before


def test_key_import_refuses_to_orphan_existing_accounts_without_force(cfg, conn, tmp_path, monkeypatch):
    _hashed_account(conn)
    src = tmp_path / "k.txt"
    src.write_text(os.urandom(32).hex(), encoding="ascii")
    before = installkey.install_key(cfg)
    _answer(monkeypatch, "import")
    assert cli.main(["--home", str(cfg.home), "key", "import", str(src)]) == 1
    assert installkey.install_key(cfg) == before
    assert cli.main(["--home", str(cfg.home), "key", "import", str(src), "--force"]) == 0
    assert installkey.install_key(cfg) != before


def test_key_import_rejects_garbage(cfg, tmp_path, monkeypatch):
    src = tmp_path / "k.txt"
    src.write_text("not a key", encoding="ascii")
    _answer(monkeypatch, "import")
    assert cli.main(["--home", str(cfg.home), "key", "import", str(src)]) == 1


def test_fingerprint_recorded_on_first_use_and_doctor_flags_a_missing_key(cfg, conn, capsys):
    assert installkey.key_status(conn, cfg) == "no_accounts"
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    assert installkey.stored_fingerprint(conn) == installkey.fingerprint(installkey.install_key(cfg))
    assert installkey.key_status(conn, cfg) == "ok"

    # The database moves to a machine whose key is new: the stored fingerprint no longer matches.
    (cfg.home / installkey.KEY_FILE).unlink()
    installkey.reset_cache()
    assert installkey.key_status(conn, cfg) == "mismatch"
    capsys.readouterr()
    assert cli.main(["--home", str(cfg.home), "doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert any("install key" in w for w in report["warnings"])


def test_doctor_reports_unverified_when_no_fingerprint(cfg, conn, capsys):
    _hashed_account(conn)
    assert installkey.key_status(conn, cfg) == "unverified"


# --- Plaid modified rows that drop a split are counted ---------------------------------------------------------------


def _stmt(amount: int, modified: bool) -> ParsedStatement:
    t = ParsedTxn("2026-10-01", amount, "SHELL OIL 55", fitid="f1", modified=modified)
    return ParsedStatement(format="plaid", txns=[t], account_key="plaid:A1", account_name="Plaid Acct")


def test_modified_row_with_changed_amount_counts_the_dropped_split(cfg, conn):
    import_statements(conn, cfg, [_stmt(-1000, False)], "p1", "d1", post_process=False)
    tid = conn.execute("SELECT id FROM transactions").fetchone()[0]
    conn.execute("INSERT INTO splits (transaction_id, amount) VALUES (?, -400), (?, -600)", (tid, tid))
    r = import_statements(conn, cfg, [_stmt(-1200, True)], "p2", "d2", post_process=False)
    assert r.edits_dropped == 1
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0
    # Same amount: nothing to drop, nothing counted.
    conn.execute("INSERT INTO splits (transaction_id, amount) VALUES (?, -500), (?, -700)", (tid, tid))
    r = import_statements(conn, cfg, [_stmt(-1200, True)], "p3", "d3", post_process=False)
    assert r.edits_dropped == 0
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 2


def test_demo_account_names_are_what_has_real_data_ignores(cfg, conn):
    add_account(conn, CHECKING, "checking")
    conn.commit()
    assert not has_real_data(conn)
