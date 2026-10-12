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
    # 3 — v2 phase 1: tags, split transactions, manual assets, goals
    """
    CREATE TABLE tags (
        transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        tag            TEXT NOT NULL,
        PRIMARY KEY (transaction_id, tag)
    );
    CREATE INDEX ix_tags_tag ON tags(tag);

    CREATE TABLE splits (
        id             INTEGER PRIMARY KEY,
        transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        amount         INTEGER NOT NULL,
        category_id    INTEGER REFERENCES categories(id),
        scope          TEXT CHECK (scope IN ('personal','business')),
        note           TEXT
    );
    CREATE INDEX ix_splits_txn ON splits(transaction_id);

    CREATE TABLE manual_assets (
        id    INTEGER PRIMARY KEY,
        name  TEXT NOT NULL UNIQUE,
        kind  TEXT NOT NULL CHECK (kind IN
                ('property','vehicle','equipment','investment','cash','liability','other')),
        value INTEGER NOT NULL,
        as_of TEXT NOT NULL,
        note  TEXT
    );

    CREATE TABLE goals (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        target      INTEGER NOT NULL CHECK (target > 0),
        target_date TEXT,
        account_id  INTEGER REFERENCES accounts(id),
        saved       INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        archived    INTEGER NOT NULL DEFAULT 0
    );

    -- One row per money line: an unsplit transaction, or each child of a split one. A split parent is
    -- replaced by its children (their amount, category and scope; the parent's date, account, merchant,
    -- transfer group and pending flag). `id` is always the parent transaction id; `split_id` is set on children.
    CREATE VIEW txn_lines AS
    SELECT t.id AS id, NULL AS split_id, t.account_id, t.posted_date, t.amount, t.currency,
           t.raw_description, t.merchant, t.memo, t.category_id, t.category_source, t.scope,
           t.transfer_group, t.is_pending, t.note, t.import_id
      FROM transactions t
     WHERE NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)
    UNION ALL
    SELECT t.id, s.id, t.account_id, t.posted_date, s.amount, t.currency,
           t.raw_description, t.merchant, t.memo, s.category_id, 'manual', COALESCE(s.scope, t.scope),
           t.transfer_group, t.is_pending, COALESCE(s.note, t.note), t.import_id
      FROM splits s JOIN transactions t ON t.id = s.transaction_id;
    """,
    # 4 — v2 phase 2: bank connections. Metadata only; tokens live in the OS keychain, never here.
    # Down: DROP TABLE connections;
    """
    CREATE TABLE connections (
        id            INTEGER PRIMARY KEY,
        provider      TEXT NOT NULL CHECK (provider IN ('simplefin','teller','plaid')),
        institution   TEXT,
        item_ref_hash TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        last_sync_at  TEXT,
        status        TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','needs_reauth','error')),
        UNIQUE (provider, item_ref_hash)
    );
    """,
    # 5 — v2 phase 2B: Plaid /transactions/sync cursor per Item. A cursor is an opaque bookmark, not a secret.
    # Down: ALTER TABLE connections DROP COLUMN cursor;  (SQLite 3.35+)
    """
    ALTER TABLE connections ADD COLUMN cursor TEXT;
    """,
    # 6 — install-key fingerprint (non-secret sha256(key)[:16]) so doctor can tell when a DB moved without its key.
    # Down: DROP TABLE meta;
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 7 — v2 phase 3: 1099 forms the user received (typed in by hand). Amounts are minor units. A named unique
    # index, not an inline UNIQUE, so a later migration can replace it without rebuilding the table.
    # Down: DROP TABLE forms_1099;
    """
    CREATE TABLE forms_1099 (
        id              INTEGER PRIMARY KEY,
        tax_year        INTEGER NOT NULL CHECK (tax_year BETWEEN 2000 AND 2100),
        form            TEXT NOT NULL CHECK (form IN ('nec','k','misc')),
        payer           TEXT NOT NULL,
        amount          INTEGER NOT NULL CHECK (amount > 0),
        match_merchants TEXT NOT NULL DEFAULT '[]',
        note            TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE UNIQUE INDEX ux_forms_1099 ON forms_1099 (tax_year, form, payer COLLATE NOCASE);
    """,
    # 8 — v2 phase 3: several businesses. NULL business_id means "the account's business, else the default (id 1)",
    # so existing rows need no backfill and single-business output stays identical. txn_lines gains the effective
    # transaction/split business. forms_1099 uniqueness now includes the business.
    # Down: restore the pre-migration backup. (SQLite 3.35+ can DROP COLUMN business_id, but first recreate txn_lines
    # from migration 3's definition, DROP INDEX ux_forms_1099_biz ix_txn_business, and recreate ux_forms_1099.)
    """
    CREATE TABLE businesses (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        archived   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    INSERT INTO businesses (id, name, created_at) VALUES (1, 'My business', datetime('now'));
    ALTER TABLE accounts     ADD COLUMN business_id INTEGER REFERENCES businesses(id);
    ALTER TABLE transactions ADD COLUMN business_id INTEGER REFERENCES businesses(id);
    ALTER TABLE splits       ADD COLUMN business_id INTEGER REFERENCES businesses(id);
    ALTER TABLE forms_1099   ADD COLUMN business_id INTEGER REFERENCES businesses(id);
    DROP INDEX ux_forms_1099;
    CREATE UNIQUE INDEX ux_forms_1099_biz
        ON forms_1099 (tax_year, form, payer COLLATE NOCASE, COALESCE(business_id, 1));
    CREATE INDEX ix_txn_business ON transactions(business_id);
    DROP VIEW txn_lines;
    CREATE VIEW txn_lines AS
    SELECT t.id AS id, NULL AS split_id, t.account_id, t.posted_date, t.amount, t.currency,
           t.raw_description, t.merchant, t.memo, t.category_id, t.category_source, t.scope,
           t.transfer_group, t.is_pending, t.note, t.import_id, t.business_id
      FROM transactions t
     WHERE NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)
    UNION ALL
    SELECT t.id, s.id, t.account_id, t.posted_date, s.amount, t.currency,
           t.raw_description, t.merchant, t.memo, s.category_id, 'manual', COALESCE(s.scope, t.scope),
           t.transfer_group, t.is_pending, COALESCE(s.note, t.note), t.import_id,
           COALESCE(s.business_id, t.business_id)
      FROM splits s JOIN transactions t ON t.id = s.transaction_id;
    """,
    # 9 — v2 phase 4: holdings snapshots from imported positions files. Quantity and price are Decimal strings (crypto
    # needs up to 18 places); value and cost basis are minor units. One snapshot per account and date; re-importing
    # replaces it. No account numbers are stored: the file's account column is dropped or reduced to its last 4.
    # Down: DROP TABLE holdings; DROP TABLE holdings_snapshots;
    """
    CREATE TABLE holdings_snapshots (
        id         INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        as_of      TEXT NOT NULL,
        import_id  INTEGER REFERENCES imports(id),
        created_at TEXT NOT NULL,
        UNIQUE (account_id, as_of)
    );
    CREATE TABLE holdings (
        id           INTEGER PRIMARY KEY,
        snapshot_id  INTEGER NOT NULL REFERENCES holdings_snapshots(id) ON DELETE CASCADE,
        symbol       TEXT NOT NULL,
        name         TEXT,
        asset_class  TEXT NOT NULL DEFAULT 'other'
                       CHECK (asset_class IN ('equity','fund','bond','cash','crypto','option','other')),
        quantity     TEXT NOT NULL,
        price        TEXT,
        market_value INTEGER,
        cost_basis   INTEGER,
        currency     TEXT NOT NULL
    );
    CREATE INDEX ix_holdings_snapshot ON holdings(snapshot_id);
    """,
]

DEFAULT_BUSINESS_ID = 1

# The standard join for aggregate queries, over split-aware money lines (see the txn_lines view).
# SCOPE_SQL, SPENDING_FILTER and INCOME_FILTER rely on the t/a/c aliases.
TXN_FROM = """txn_lines t JOIN accounts a ON a.id = t.account_id
              LEFT JOIN categories c ON c.id = t.category_id"""

# The same join over the raw transactions table: row listings and edits, where a split parent is one row.
TXN_BASE_FROM = """transactions t JOIN accounts a ON a.id = t.account_id
                   LEFT JOIN categories c ON c.id = t.category_id"""

# Effective scope of a transaction: its own override, else its account's (mixed counts as personal
# until a transaction is explicitly marked business).
SCOPE_SQL = "COALESCE(t.scope, CASE a.scope WHEN 'business' THEN 'business' ELSE 'personal' END)"

# Effective business of a business-scope row: the split/transaction override (txn_lines already folds the split in),
# else the account's, else the default business (id 1). Only meaningful where SCOPE_SQL = 'business'.
BUSINESS_SQL = f"COALESCE(t.business_id, a.business_id, {DEFAULT_BUSINESS_ID})"

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
    conn.commit()
    for version in range(current, target):
        # One transaction per migration, including the version bump: a failure part-way rolls everything back
        # instead of leaving half the script applied. (SQLite DDL is transactional.)
        script = (
            f"BEGIN;\n{MIGRATIONS[version]}\nDELETE FROM schema_version;\n"
            f"INSERT INTO schema_version (version) VALUES ({int(version) + 1});\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
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
