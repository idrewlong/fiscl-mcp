"""NEW-1: a missing or swapped install key must refuse imports instead of silently creating duplicate accounts."""

from __future__ import annotations

import getpass
import os

import pytest

from fiscl import cli, installkey
from fiscl.guard import GuardError
from fiscl.importers.core import import_file
from fiscl.sync import registry
from fiscl.watch import process_inbox_file

from .conftest import TODAY, write
from .test_importers import OFX
from .test_phase2_qa import tconn, tsync  # noqa: F401  (fixtures/helpers for the sync path)
from .test_teller import FakeTeller

MESSAGE = "install key isn't available on this machine"


def _counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
    )


def _swap_key(cfg):
    """Simulate a database that moved to a machine with a different key."""
    (cfg.home / installkey.KEY_FILE).unlink()
    installkey.reset_cache()
    (cfg.home / installkey.KEY_FILE).write_text(os.urandom(32).hex(), encoding="ascii")


def _first_import(conn, cfg, tmp_path):
    r = import_file(conn, cfg, write(tmp_path / "s.ofx", OFX))
    conn.commit()
    assert r.status == "ok"
    return installkey.install_key(cfg)


def test_moved_db_with_different_key_refuses_import_and_writes_nothing(conn, cfg, tmp_path):
    _first_import(conn, cfg, tmp_path)
    before = _counts(conn)
    _swap_key(cfg)
    r = import_file(conn, cfg, write(tmp_path / "s2.ofx", OFX.replace("A2", "A3")))
    assert r.status == "failed" and MESSAGE in r.error and "fiscl key import" in r.error
    assert _counts(conn) == before


def test_lost_key_with_hashed_accounts_does_not_mint_a_new_key(conn, cfg, tmp_path):
    _first_import(conn, cfg, tmp_path)
    (cfg.home / installkey.KEY_FILE).unlink()
    installkey.reset_cache()
    before = _counts(conn)
    r = import_file(conn, cfg, write(tmp_path / "s2.ofx", OFX.replace("A2", "A3")))
    assert r.status == "failed" and MESSAGE in r.error
    assert not (cfg.home / installkey.KEY_FILE).exists()
    assert _counts(conn) == before


def test_lost_key_without_recorded_fingerprint_but_hashed_accounts_is_refused(conn, cfg):
    from fiscl.db import add_account

    add_account(conn, "A", "checking", acct_hash="h")
    conn.commit()
    with pytest.raises(installkey.KeyMissing):
        installkey.require_usable_key(conn, cfg, ["bank:acct"])
    assert not (cfg.home / installkey.KEY_FILE).exists()


def test_fresh_database_still_creates_a_key(conn, cfg, tmp_path):
    assert not (cfg.home / installkey.KEY_FILE).exists()
    r = import_file(conn, cfg, write(tmp_path / "s.ofx", OFX))
    assert r.status == "ok"
    assert (cfg.home / installkey.KEY_FILE).exists()


def test_restoring_the_right_key_lets_the_import_succeed_without_duplicates(conn, cfg, tmp_path, monkeypatch):
    key = _first_import(conn, cfg, tmp_path)
    export = tmp_path / "exported.txt"
    export.write_text(key.hex(), encoding="ascii")
    (cfg.home / installkey.KEY_FILE).unlink()
    installkey.reset_cache()
    bad = write(tmp_path / "s2.ofx", OFX.replace("A2", "A3"))
    assert import_file(conn, cfg, bad).status == "failed"

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "import")
    assert cli.main(["--home", str(cfg.home), "key", "import", str(export)]) == 0
    r = import_file(conn, cfg, bad)  # the failed attempt is retried, not remembered as a duplicate
    assert r.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_inbox_watcher_files_it_as_failed_with_the_capped_message(conn, cfg, tmp_path):
    _first_import(conn, cfg, tmp_path)
    _swap_key(cfg)
    before = _counts(conn)
    f = write(cfg.inbox / "again.ofx", OFX.replace("A2", "A3"))
    assert process_inbox_file(cfg, f) == "failed"
    note = (cfg.failed / "again.ofx.error.txt").read_text()
    assert MESSAGE in note and len(note) <= 250
    assert _counts(conn) == before


def test_sync_accounts_raises_key_missing_before_fetching_anything(cfg, conn, tconn, tmp_path):  # noqa: F811
    _first_import(conn, cfg, tmp_path)
    _swap_key(cfg)
    before = _counts(conn)
    api = FakeTeller()
    with pytest.raises(GuardError) as err:
        registry.sync_accounts(conn, cfg, provider="teller", today=TODAY, opener=api)
    assert err.value.code == "key_missing" and MESSAGE in str(err.value)
    assert api.paths() == []  # no network traffic either
    assert _counts(conn) == before
