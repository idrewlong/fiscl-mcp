from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fiscl.config import ensure_layout, load_config
from fiscl.db import add_account, connect

TODAY = date(2026, 10, 12)


@pytest.fixture
def cfg(tmp_path: Path):
    c = load_config(tmp_path / "home")
    ensure_layout(c)
    return c


@pytest.fixture
def conn(cfg):
    c = connect(cfg)
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    ids = {
        "checking": add_account(conn, "Checking", "checking"),
        "card": add_account(conn, "Card", "credit"),
        "biz": add_account(conn, "Biz", "checking", scope="business"),
    }
    conn.commit()
    return ids


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def add_txn(
    conn, account_id: int, day: str, amount: int, desc: str, category: str | None = None, scope: str | None = None
) -> int:
    from fiscl.db import get_or_create_category
    from fiscl.normalize import normalize_merchant

    cat = get_or_create_category(conn, category) if category else None
    cur = conn.execute(
        """INSERT INTO transactions (account_id, posted_date, amount, currency, raw_description, merchant,
               category_id, category_source, dedupe_key, scope)
           VALUES (?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?)""",
        (
            account_id,
            day,
            amount,
            desc,
            normalize_merchant(desc),
            cat,
            "manual" if cat else "none",
            f"t:{account_id}:{day}:{amount}:{desc}:{conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]}",
            scope,
        ),
    )
    return int(cur.lastrowid)


class PasswordDeleteError(Exception):
    """Same class name as keyring.errors.PasswordDeleteError, which is what a real backend raises for "not found"."""


class FakeKeyring:
    """An in-memory stand-in for the OS keychain."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}

    def set_password(self, service, user, value):
        self.items[(service, user)] = value

    def get_password(self, service, user):
        return self.items.get((service, user))

    def delete_password(self, service, user):
        if (service, user) not in self.items:
            raise PasswordDeleteError(user)
        del self.items[(service, user)]


@pytest.fixture
def keychain(monkeypatch):
    """Pretend a real OS keychain exists (the keyring package isn't installed in the test environment)."""
    from fiscl.sync import base

    kr = FakeKeyring()
    monkeypatch.setattr(base, "keyring_backend", lambda: kr)
    return kr
