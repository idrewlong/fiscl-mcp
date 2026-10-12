"""Import pipeline: detect, parse, resolve account, dedupe, insert, post-process."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import fx
from ..config import Config, load_rules_raw
from ..db import add_account, find_account
from ..guard import clean_text
from ..installkey import (
    KeyMissing,
    account_hash,
    has_hashed_accounts,
    legacy_account_hash,
    record_fingerprint,
    require_usable_key,
)
from ..normalize import load_aliases, normalize_merchant
from .csv_import import CSVImportError, parse_csv
from .ofx import OFXError, looks_like_ofx, parse_ofx
from .qif import QIFError, looks_like_qif, parse_qif
from .types import ParsedStatement

SUPPORTED = {".csv", ".ofx", ".qfx", ".qif", ".txt"}
MAX_FILE_BYTES = 50 * 1024 * 1024
# balances.source is CHECK-constrained; bank connectors all record as 'sync'.
BALANCE_SOURCE = {"ofx": "ofx", "simplefin": "sync", "teller": "sync", "plaid": "sync"}


class ImportFailed(ValueError):
    pass


@dataclass
class ImportResult:
    filename: str
    status: str  # ok | duplicate_file | failed
    format: str | None = None
    accounts: list[str] = field(default_factory=list)
    rows_seen: int = 0
    rows_added: int = 0
    rows_skipped: int = 0
    pending_replaced: int = 0
    edits_carried: int = 0  # pending rows whose splits/tags/note/category/scope moved to the posted row
    edits_dropped: int = 0  # pending rows whose split no longer matched the posted amount and was dropped
    error: str | None = None
    # Counts of things skipped on purpose (QIF sections, split lines, holdings rows without a value).
    warnings: dict[str, int] = field(default_factory=dict)
    as_of: str | None = None  # holdings imports: the snapshot date
    snapshots_replaced: int = 0  # holdings imports: snapshots this file replaced

    def as_dict(self) -> dict:
        out = self.__dict__.copy()
        for key in ("warnings", "as_of", "snapshots_replaced"):  # keep older formats' output unchanged
            if not out[key]:
                del out[key]
        return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dedupe_key(posted: str, amount: int, merchant: str, occurrence: int) -> str:
    raw = f"{posted}|{amount}|{merchant.lower()}|{occurrence}"
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def read_import_text(path: Path) -> str:
    if path.suffix.lower() not in SUPPORTED:
        raise ImportFailed("unsupported file type (expected CSV, OFX, QFX, or QIF)")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ImportFailed("file is larger than 50 MB")
    return path.read_text(encoding="utf-8-sig", errors="replace")


def parse_text(
    text: str, path: Path, cfg: Config, profile: str | None = None, qif_date: str | None = None
) -> list[ParsedStatement]:
    try:
        if looks_like_ofx(text[:2000]):
            return parse_ofx(text)
        if path.suffix.lower() == ".qif" or looks_like_qif(text):
            return parse_qif(text, cfg, qif_date)
        return [parse_csv(text, cfg, profile)]
    except (OFXError, CSVImportError, QIFError) as exc:
        raise ImportFailed(str(exc)) from exc


def parse_file(
    path: Path, cfg: Config, profile: str | None = None, qif_date: str | None = None
) -> list[ParsedStatement]:
    return parse_text(read_import_text(path), path, cfg, profile, qif_date)


def _free_name(conn: sqlite3.Connection, name: str) -> str:
    candidate, n = name, 1
    while find_account(conn, candidate):
        n += 1
        candidate = f"{name} {n}"
    return candidate


def _resolve_account(
    conn: sqlite3.Connection, cfg: Config, stmt: ParsedStatement, account_name: str | None
) -> sqlite3.Row:
    name = account_name or stmt.account_name
    if stmt.account_key:
        acct_hash = account_hash(cfg, stmt.account_key)
        # Record which key made this database's hashes (first use wins) so `fiscl doctor` can notice a database that
        # moved without its key. Only record when the key is known good: no hashed accounts yet, or one just matched.
        if not has_hashed_accounts(conn):
            record_fingerprint(conn, cfg)
        row = conn.execute("SELECT * FROM accounts WHERE acct_hash = ?", (acct_hash,)).fetchone()
        if row:
            record_fingerprint(conn, cfg)
            return row
        # Accounts from before the keyed hash carry sha256(key): find them and upgrade in place, once.
        legacy = conn.execute(
            "SELECT * FROM accounts WHERE acct_hash = ?", (legacy_account_hash(stmt.account_key),)
        ).fetchone()
        if legacy:
            conn.execute("UPDATE accounts SET acct_hash = ? WHERE id = ?", (acct_hash, legacy["id"]))
            record_fingerprint(conn, cfg)
            return conn.execute("SELECT * FROM accounts WHERE id = ?", (legacy["id"],)).fetchone()
        new_name = name or f"{stmt.institution or 'Account'} ••{stmt.last4}"
        if name and (row := find_account(conn, name)):
            if row["acct_hash"] is None:
                conn.execute(
                    "UPDATE accounts SET acct_hash = ?, last4 = COALESCE(last4, ?) WHERE id = ?",
                    (acct_hash, stmt.last4, row["id"]),
                )
                return find_account(conn, name)
            if account_name is not None:
                # The user named this account on purpose (--account): that is their call, so keep merging.
                return row
            # The name belongs to an account already bound to a different identity. A display name is bank-supplied
            # text, never proof of identity, so don't merge: make a separate, clearly labelled account.
            new_name = _free_name(conn, f"{name} ({stmt.format})")
        elif find_account(conn, new_name):
            new_name = _free_name(conn, f"{new_name} ({stmt.format})")
        add_account(
            conn, new_name, stmt.type_hint or "checking", stmt.institution, stmt.currency, acct_hash, stmt.last4
        )
        return find_account(conn, new_name)
    if not name:
        raise ImportFailed(
            "this file needs an account: pass --account NAME, set `account` in the CSV profile, "
            "or (QIF) name it in an !Account block"
        )
    row = find_account(conn, name)
    if row is None:
        raise ImportFailed("the account name given doesn't exist. Create it with `fiscl accounts add`.")
    return row


def _carry_edits(conn: sqlite3.Connection, pend: sqlite3.Row, new_id: int, amount: int) -> tuple[int, int]:
    """Move the user's edits from a pending row to the posted row that replaces it.

    Returns (carried, dropped): 1 if anything moved, 1 if a split was dropped because its parts no longer sum to
    the posted amount (never keep an invalid split silently).
    """
    carried = dropped = 0
    split_total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM splits WHERE transaction_id = ?", (pend["id"],)
    ).fetchone()
    if split_total[0]:
        if split_total[1] == amount:
            conn.execute("UPDATE splits SET transaction_id = ? WHERE transaction_id = ?", (new_id, pend["id"]))
            carried = 1
        else:
            dropped = 1  # the pending row's cascade removes the stale split
    tags = conn.execute(
        "INSERT OR IGNORE INTO tags (transaction_id, tag) SELECT ?, tag FROM tags WHERE transaction_id = ?",
        (new_id, pend["id"]),
    )
    if tags.rowcount > 0:
        carried = 1
    manual = pend["category_id"] is not None and pend["category_source"] == "manual"
    if manual or pend["note"] is not None or pend["scope"] is not None:
        conn.execute(
            """UPDATE transactions SET note = COALESCE(note, ?), scope = COALESCE(scope, ?),
                 category_id = CASE WHEN ? THEN ? ELSE category_id END,
                 category_source = CASE WHEN ? THEN 'manual' ELSE category_source END
               WHERE id = ?""",
            (pend["note"], pend["scope"], manual, pend["category_id"], manual, new_id),
        )
        carried = 1
    return carried, dropped


def _update_in_place(conn: sqlite3.Connection, cfg: Config, account_id: int, t, merchant: str) -> int | None:
    """Apply a provider-reported change to a row we already hold (matched by fitid). The user's category, note,
    tags, and scope stay; a split that no longer sums to the new amount is dropped rather than kept invalid.
    Returns None when the row isn't there yet (the caller inserts it), else 1 if a split was dropped, else 0."""
    row = conn.execute(
        "SELECT id, amount FROM transactions WHERE account_id = ? AND fitid = ?", (account_id, t.fitid)
    ).fetchone()
    if row is None:
        return None
    dropped = 0
    if row["amount"] != t.amount:
        dropped = 1 if conn.execute("DELETE FROM splits WHERE transaction_id = ?", (row["id"],)).rowcount else 0
    key = "f:" + t.fitid if not t.pending else None
    conn.execute(
        """UPDATE OR IGNORE transactions SET posted_date = ?, amount = ?, raw_description = ?, merchant = ?,
             memo = ?, is_pending = ?, dedupe_key = COALESCE(?, dedupe_key) WHERE id = ?""",
        (t.posted_date, t.amount, t.description, merchant, t.memo, int(t.pending), key, row["id"]),
    )
    fx.refresh_home_amounts(conn, cfg, ids=[row["id"]])  # new date or amount can change the rate in force
    return dropped


def _insert_statement(
    conn: sqlite3.Connection, cfg: Config, stmt: ParsedStatement, account: sqlite3.Row, import_id: int, aliases
) -> tuple[int, int, int, int, int]:
    added = skipped = replaced = carried = dropped = 0
    rates = fx.RateBook(conn, cfg)
    occurrences: dict[tuple, int] = {}
    for t in stmt.txns:
        merchant = normalize_merchant(t.description, aliases)
        if t.fitid and not t.pending:
            key = "f:" + t.fitid
        elif t.fitid and stmt.format == "plaid":
            # Cursor connectors give every pending row a stable provider id; two identical pending charges that
            # arrive in different syncs are different charges, so key on the id, not the description.
            key = "p:f:" + t.fitid
        else:
            # Keyed on the raw description (not the normalized merchant) so editing aliases later
            # can't make an overlapping export look like new transactions.
            ident = (t.posted_date, t.amount, t.description.lower(), t.pending)
            occurrences[ident] = occurrences.get(ident, 0) + 1
            key = ("p:" if t.pending else "") + _dedupe_key(t.posted_date, t.amount, t.description, occurrences[ident])
        if t.modified and t.fitid and (updated := _update_in_place(conn, cfg, account["id"], t, merchant)) is not None:
            skipped += 1
            dropped += updated
            continue
        pend = None
        if not t.pending and t.replaces_fitid:
            # The provider named the pending row this one supersedes: exact, so tips and edits still carry over.
            pend = conn.execute(
                """SELECT id, note, category_id, category_source, scope FROM transactions
                   WHERE account_id = ? AND is_pending = 1 AND fitid = ?""",
                (account["id"], t.replaces_fitid),
            ).fetchone()
        if not t.pending and pend is None:
            # A posted row replaces a matching pending row from up to 7 days earlier.
            start = (date.fromisoformat(t.posted_date) - timedelta(days=7)).isoformat()
            pend = conn.execute(
                """SELECT id, note, category_id, category_source, scope FROM transactions
                   WHERE account_id = ? AND is_pending = 1 AND amount = ?
                   AND lower(merchant) = lower(?) AND posted_date BETWEEN ? AND ? ORDER BY posted_date LIMIT 1""",
                (account["id"], t.amount, merchant, start, t.posted_date),
            ).fetchone()
        cur = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (account_id, posted_date, amount, currency, raw_description, merchant, memo,
                is_pending, fitid, dedupe_key, import_id, amount_home)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account["id"],
                t.posted_date,
                t.amount,
                stmt.currency,
                t.description,
                merchant,
                t.memo,
                int(t.pending),
                t.fitid,
                key,
                import_id,
                rates.convert(t.amount, stmt.currency, t.posted_date),
            ),
        )
        if cur.rowcount:
            added += 1
            if pend:
                c, d = _carry_edits(conn, pend, int(cur.lastrowid), t.amount)
                fx.refresh_home_amounts(conn, cfg, ids=[int(cur.lastrowid)])  # carried split parts follow the new row
                carried += c
                dropped += d
                conn.execute("DELETE FROM transactions WHERE id = ?", (pend["id"],))
                replaced += 1
        else:
            skipped += 1
    source = BALANCE_SOURCE.get(stmt.format, "csv")
    conn.executemany(
        "INSERT OR REPLACE INTO balances (account_id, as_of, amount, source) VALUES (?, ?, ?, ?)",
        [(account["id"], d, b, source) for d, b in stmt.balances],
    )
    return added, skipped, replaced, carried, dropped


def import_file(
    conn: sqlite3.Connection,
    cfg: Config,
    path: Path,
    account_name: str | None = None,
    profile: str | None = None,
    dry_run: bool = False,
    post_process: bool = True,
    qif_date: str | None = None,
    as_of: str | None = None,
) -> ImportResult:
    from . import holdings

    path = Path(path)
    result = ImportResult(filename=path.name, status="ok")
    digest = sha256_file(path)
    prior = conn.execute("SELECT status FROM imports WHERE file_sha256 = ?", (digest,)).fetchone()
    if prior and prior["status"] != "failed":
        result.status = "duplicate_file"
        return result
    fmt = path.suffix.lstrip(".").lower()
    try:
        text = read_import_text(path)
        holdings_profile = holdings.find_profile(text, cfg, profile)
        if holdings_profile is None:
            statements = parse_text(text, path, cfg, profile, qif_date)
    except (ImportFailed, CSVImportError) as exc:
        return _record_failure(conn, path.name, fmt, digest, result, str(exc), dry_run)
    if holdings_profile is not None:
        return holdings.import_holdings(
            conn, cfg, text, holdings_profile, path.name, digest, result, account_name, as_of, dry_run
        )
    return import_statements(conn, cfg, statements, path.name, digest, result, account_name, dry_run, post_process)


def import_statements(
    conn: sqlite3.Connection,
    cfg: Config,
    statements: list[ParsedStatement],
    label: str,
    digest: str,
    result: ImportResult | None = None,
    account_name: str | None = None,
    dry_run: bool = False,
    post_process: bool = True,
) -> ImportResult:
    """Insert parsed statements from any source (file or bank sync) as one atomic import."""
    result = result or ImportResult(filename=label, status="ok")
    result.format = statements[0].format if statements else "none"
    if account_name and len(statements) > 1 and result.format == "qif":
        # --account would send every section to one account; the file already says which is which.
        error = "this QIF file holds several accounts; import it without --account"
        return _record_failure(conn, label, result.format, digest, result, error, dry_run)
    try:
        require_usable_key(
            conn, cfg, [s.account_key for s in statements if s.account_key]
        )  # before any account is resolved: a missing key must never create duplicates
    except KeyMissing as exc:
        return _record_failure(conn, label, result.format, digest, result, str(exc), dry_run)
    aliases = load_aliases(load_rules_raw(cfg))
    savepoint = "fiscl_import"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute("DELETE FROM imports WHERE file_sha256 = ? AND status = 'failed'", (digest,))
        cur = conn.execute(
            "INSERT INTO imports (filename, file_sha256, format, status, imported_at) VALUES (?, ?, ?, 'ok', ?)",
            (label, digest, result.format, datetime.now().isoformat(timespec="seconds")),
        )
        import_id = int(cur.lastrowid)
        account_id = None
        for stmt in statements:
            account = _resolve_account(conn, cfg, stmt, account_name)
            account_id = account["id"]
            result.accounts.append(account["name"])
            added, skipped, replaced, carried, dropped = _insert_statement(conn, cfg, stmt, account, import_id, aliases)
            result.rows_seen += len(stmt.txns)
            result.rows_added += added
            result.rows_skipped += skipped
            result.pending_replaced += replaced
            result.edits_carried += carried
            result.edits_dropped += dropped
            for key, count in stmt.warnings.items():
                result.warnings[key] = result.warnings.get(key, 0) + count
        conn.execute(
            "UPDATE imports SET account_id = ?, rows_seen = ?, rows_added = ?, rows_skipped = ? WHERE id = ?",
            (account_id, result.rows_seen, result.rows_added, result.rows_skipped, import_id),
        )
        if dry_run:
            conn.execute(f"ROLLBACK TO {savepoint}")
        elif post_process:
            post_import(conn, cfg)
        conn.execute(f"RELEASE {savepoint}")
    except ImportFailed as exc:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        return _record_failure(conn, label, result.format, digest, result, str(exc), dry_run)
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    return result


def _record_failure(
    conn, label: str, fmt: str | None, digest: str, result: ImportResult, error: str, dry_run: bool
) -> ImportResult:
    error = clean_text(error, 200)
    result.status, result.error = "failed", error
    if not dry_run:
        conn.execute(
            """INSERT INTO imports (filename, file_sha256, format, status, error, imported_at)
               VALUES (?, ?, ?, 'failed', ?, ?)
               ON CONFLICT(file_sha256) DO UPDATE SET status = 'failed', error = excluded.error,
                 imported_at = excluded.imported_at""",
            (label, digest, result.format or fmt or "unknown", error, datetime.now().isoformat(timespec="seconds")),
        )
    return result


def post_import(conn: sqlite3.Connection, cfg: Config, today: date | None = None) -> dict:
    """Jobs that run after every import: transfers first (so they aren't categorized as spending),
    then rules, then recurring detection."""
    from ..categorize import apply_rules
    from ..recurring import refresh_recurring
    from ..transfers import match_transfers

    return {
        "transfers_matched": match_transfers(conn, cfg),
        "rules": apply_rules(conn, cfg),
        "recurring": refresh_recurring(conn, cfg, today=today),
    }
