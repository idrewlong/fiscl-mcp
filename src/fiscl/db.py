"""SQLite storage: connection, migrations, backups, category helpers."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from .config import FILE_MODE, Config

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE accounts (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        institution TEXT,
        type        TEXT NOT NULL CHECK (type IN
                      ('checking','savings','credit','brokerage','loan','crypto','cash','other')),
        currency    TEXT NOT NULL DEFAULT 'USD',
        acct_hash   TEXT UNIQUE,
        last4       TEXT,
        is_asset    INTEGER NOT NULL,
        archived    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE categories (
        id        INTEGER PRIMARY KEY,
        name      TEXT NOT NULL UNIQUE,
        parent_id INTEGER REFERENCES categories(id),
        kind      TEXT NOT NULL CHECK (kind IN ('expense','income','transfer','ignore')),
        tax_tag   TEXT
    );

    CREATE TABLE imports (
        id           INTEGER PRIMARY KEY,
        filename     TEXT NOT NULL,
        file_sha256  TEXT NOT NULL UNIQUE,
        format       TEXT NOT NULL,
        account_id   INTEGER REFERENCES accounts(id),
        rows_seen    INTEGER NOT NULL DEFAULT 0,
        rows_added   INTEGER NOT NULL DEFAULT 0,
        rows_skipped INTEGER NOT NULL DEFAULT 0,
        status       TEXT NOT NULL CHECK (status IN ('ok','partial','failed')),
        error        TEXT,
        imported_at  TEXT NOT NULL
    );

    CREATE TABLE transactions (
        id              INTEGER PRIMARY KEY,
        account_id      INTEGER NOT NULL REFERENCES accounts(id),
        posted_date     TEXT NOT NULL,
        amount          INTEGER NOT NULL,
        currency        TEXT NOT NULL,
        raw_description TEXT NOT NULL,
        merchant        TEXT,
        memo            TEXT,
        category_id     INTEGER REFERENCES categories(id),
        category_source TEXT NOT NULL DEFAULT 'none'
                          CHECK (category_source IN ('rule','manual','transfer','none')),
        transfer_group  TEXT,
        is_pending      INTEGER NOT NULL DEFAULT 0,
        fitid           TEXT,
        dedupe_key      TEXT NOT NULL,
        import_id       INTEGER REFERENCES imports(id),
        note            TEXT,
        UNIQUE (account_id, dedupe_key)
    );
    CREATE INDEX ix_txn_date ON transactions(posted_date);
    CREATE INDEX ix_txn_account_date ON transactions(account_id, posted_date);
    CREATE INDEX ix_txn_merchant ON transactions(merchant);
    CREATE INDEX ix_txn_category ON transactions(category_id);

    CREATE TABLE balances (
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        as_of      TEXT NOT NULL,
        amount     INTEGER NOT NULL,
        source     TEXT NOT NULL CHECK (source IN ('ofx','csv','sync','manual','derived')),
        PRIMARY KEY (account_id, as_of, source)
    );

    CREATE TABLE recurring (
        id             INTEGER PRIMARY KEY,
        merchant       TEXT NOT NULL,
        account_id     INTEGER REFERENCES accounts(id),
        direction      TEXT NOT NULL CHECK (direction IN ('out','in')),
        cadence        TEXT NOT NULL,
        typical_amount INTEGER NOT NULL,
        last_amount    INTEGER NOT NULL,
        last_date      TEXT NOT NULL,
        next_expected  TEXT,
        occurrences    INTEGER NOT NULL,
        confidence     REAL NOT NULL,
        status         TEXT NOT NULL,
        kind           TEXT,
        UNIQUE (merchant, account_id, direction)
    );

    CREATE TABLE recurring_dismissed (
        merchant   TEXT NOT NULL,
        account_id INTEGER,
        direction  TEXT NOT NULL,
        PRIMARY KEY (merchant, account_id, direction)
    );
    """,
    # 2 — business/personal scope, Schedule C mapping, mileage log
    """
    ALTER TABLE accounts ADD COLUMN scope TEXT NOT NULL DEFAULT 'personal'
        CHECK (scope IN ('personal', 'business', 'mixed'));
    ALTER TABLE transactions ADD COLUMN scope TEXT CHECK (scope IN ('personal', 'business'));
    ALTER TABLE categories ADD COLUMN schedule_c_line TEXT;

    CREATE TABLE trips (
        id           INTEGER PRIMARY KEY,
        trip_date    TEXT NOT NULL,
        miles_tenths INTEGER NOT NULL CHECK (miles_tenths > 0),
        purpose      TEXT NOT NULL,
        vehicle      TEXT,
        created_at   TEXT NOT NULL
    );
    CREATE INDEX ix_trips_date ON trips(trip_date);

    INSERT OR IGNORE INTO categories (name, kind, schedule_c_line) VALUES
        ('Income: Business', 'income', '1'),
        ('Business: Advertising', 'expense', '8'),
        ('Business: Car & Truck', 'expense', '9'),
        ('Business: Commissions & Fees', 'expense', '10'),
        ('Business: Contract Labor', 'expense', '11'),
        ('Business: Equipment', 'expense', '13'),
        ('Business: Insurance', 'expense', '15'),
        ('Business: Interest', 'expense', '16b'),
        ('Business: Legal & Professional', 'expense', '17'),
        ('Business: Office Expense', 'expense', '18'),
        ('Business: Rent (Equipment)', 'expense', '20a'),
        ('Business: Rent (Property)', 'expense', '20b'),
        ('Business: Repairs & Maintenance', 'expense', '21'),
        ('Business: Supplies', 'expense', '22'),
        ('Business: Taxes & Licenses', 'expense', '23'),
        ('Business: Travel', 'expense', '24a'),
        ('Business: Meals', 'expense', '24b'),
        ('Business: Utilities', 'expense', '25'),
        ('Business: Wages', 'expense', '26'),
        ('Business: Software & Subscriptions', 'expense', '27b'),
        ('Business: Education', 'expense', '27b'),
        ('Business: Bank Fees', 'expense', '27b');
    UPDATE categories SET schedule_c_line = '1' WHERE name = 'Income: Freelance';
    """,
]

# The standard join for transaction queries. SCOPE_SQL, SPENDING_FILTER and INCOME_FILTER rely on these aliases.
TXN_FROM = """transactions t JOIN accounts a ON a.id = t.account_id
              LEFT JOIN categories c ON c.id = t.category_id"""

# Effective scope of a transaction: its own override, else its account's (mixed counts as personal
# until a transaction is explicitly marked business).
SCOPE_SQL = "COALESCE(t.scope, CASE a.scope WHEN 'business' THEN 'business' ELSE 'personal' END)"

DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("Income", "income"),
    ("Income: Salary", "income"),
    ("Income: Freelance", "income"),
    ("Income: Interest", "income"),
    ("Housing", "expense"),
    ("Utilities", "expense"),
    ("Groceries", "expense"),
    ("Dining", "expense"),
    ("Transport", "expense"),
    ("Auto: Fuel", "expense"),
    ("Auto: Maintenance", "expense"),
    ("Health", "expense"),
    ("Insurance", "expense"),
    ("Shopping", "expense"),
    ("Subscriptions", "expense"),
    ("Entertainment", "expense"),
    ("Travel", "expense"),
    ("Education", "expense"),
    ("Gifts & Donations", "expense"),
    ("Fees & Interest", "expense"),
    ("Taxes", "expense"),
    ("Business", "expense"),
    ("Transfers", "transfer"),
    ("Ignore", "ignore"),
]

# Rows that count as spending: money out, or refunds into an expense category.
SPENDING_FILTER = """
    t.transfer_group IS NULL
    AND COALESCE(c.kind, 'expense') NOT IN ('transfer', 'ignore', 'income')
    AND (t.amount < 0 OR c.kind = 'expense')
"""
# Rows that count as income: money in that isn't a transfer, ignored, or an expense refund.
INCOME_FILTER = """
    t.transfer_group IS NULL
    AND t.amount > 0
    AND COALESCE(c.kind, 'income') NOT IN ('transfer', 'ignore', 'expense')
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(cfg: Config) -> sqlite3.Connection:
    existed = cfg.db_path.exists()
    conn = sqlite3.connect(cfg.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if not existed:
        os.chmod(cfg.db_path, FILE_MODE)
    migrate(conn, cfg)
    return conn


@contextmanager
def open_db(cfg: Config) -> Iterator[sqlite3.Connection]:
    conn = connect(cfg)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else 0


def migrate(conn: sqlite3.Connection, cfg: Config) -> None:
    current = schema_version(conn)
    target = len(MIGRATIONS)
    if current >= target:
        return
    if current > 0:
        backup(conn, cfg, reason="pre-migration")
    for version in range(current, target):
        conn.executescript(MIGRATIONS[version])
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version + 1,))
    if current == 0:
        conn.executemany("INSERT OR IGNORE INTO categories (name, kind) VALUES (?, ?)", DEFAULT_CATEGORIES)
    conn.commit()


def backup(conn: sqlite3.Connection, cfg: Config, reason: str = "manual", keep: int = 14) -> str:
    cfg.backups.mkdir(parents=True, exist_ok=True)
    dest = cfg.backups / f"fiscl-{datetime.now():%Y%m%d-%H%M%S}-{reason}.db"
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    os.chmod(dest, FILE_MODE)
    snapshots = sorted(cfg.backups.glob("fiscl-*.db"))
    for old in snapshots[:-keep]:
        old.unlink(missing_ok=True)
    return str(dest)


def backup_if_stale(conn: sqlite3.Connection, cfg: Config, max_age: timedelta = timedelta(hours=24)) -> None:
    snapshots = sorted(cfg.backups.glob("fiscl-*.db"))
    if snapshots:
        age = datetime.now() - datetime.fromtimestamp(snapshots[-1].stat().st_mtime)
        if age < max_age:
            return
    if conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]:
        backup(conn, cfg, reason="daily")


def category_kind_for_new(name: str) -> str:
    return "income" if name.lower().startswith("income") else "expense"


def get_category_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM categories WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return row[0] if row else None


def get_or_create_category(conn: sqlite3.Connection, name: str) -> int:
    existing = get_category_id(conn, name)
    if existing is not None:
        return existing
    cur = conn.execute("INSERT INTO categories (name, kind) VALUES (?, ?)", (name, category_kind_for_new(name)))
    return int(cur.lastrowid)


def add_account(
    conn: sqlite3.Connection,
    name: str,
    type_: str,
    institution: str | None = None,
    currency: str = "USD",
    acct_hash: str | None = None,
    last4: str | None = None,
    scope: str = "personal",
) -> int:
    is_asset = 0 if type_ in ("credit", "loan") else 1
    cur = conn.execute(
        """INSERT INTO accounts (name, institution, type, currency, acct_hash, last4, is_asset, created_at, scope)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, institution, type_, currency.upper(), acct_hash, last4, is_asset, _now(), scope),
    )
    return int(cur.lastrowid)


def find_account(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
