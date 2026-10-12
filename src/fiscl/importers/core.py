"""Import pipeline: detect, parse, resolve account, dedupe, insert, post-process."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import Config, load_rules_raw
from ..db import add_account, find_account
from ..normalize import load_aliases, normalize_merchant
from .csv_import import CSVImportError, parse_csv
from .ofx import OFXError, looks_like_ofx, parse_ofx
from .types import ParsedStatement

SUPPORTED = {".csv", ".ofx", ".qfx", ".txt"}
MAX_FILE_BYTES = 50 * 1024 * 1024


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
    error: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _account_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _dedupe_key(posted: str, amount: int, merchant: str, occurrence: int) -> str:
    raw = f"{posted}|{amount}|{merchant.lower()}|{occurrence}"
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def parse_file(path: Path, cfg: Config, profile: str | None = None) -> list[ParsedStatement]:
    if path.suffix.lower() not in SUPPORTED:
        raise ImportFailed(f"unsupported file type {path.suffix!r} (expected CSV, OFX, or QFX)")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ImportFailed("file is larger than 50 MB")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        if looks_like_ofx(text[:2000]):
            return parse_ofx(text)
        return [parse_csv(text, cfg, profile)]
    except (OFXError, CSVImportError) as exc:
        raise ImportFailed(str(exc)) from exc


def _resolve_account(conn: sqlite3.Connection, stmt: ParsedStatement, account_name: str | None) -> sqlite3.Row:
    name = account_name or stmt.account_name
    if stmt.account_key:
        acct_hash = _account_hash(stmt.account_key)
        row = conn.execute("SELECT * FROM accounts WHERE acct_hash = ?", (acct_hash,)).fetchone()
        if row:
            return row
        if name and (row := find_account(conn, name)):
            if row["acct_hash"] is None:
                conn.execute(
                    "UPDATE accounts SET acct_hash = ?, last4 = COALESCE(last4, ?) WHERE id = ?",
                    (acct_hash, stmt.last4, row["id"]),
                )
            return find_account(conn, name)
        new_name = name or f"{stmt.institution or 'Account'} ••{stmt.last4}"
        add_account(
            conn, new_name, stmt.type_hint or "checking", stmt.institution, stmt.currency, acct_hash, stmt.last4
        )
        return find_account(conn, new_name)
    if not name:
        raise ImportFailed("CSV files need an account: pass --account NAME or set `account` in the profile")
    row = find_account(conn, name)
    if row is None:
        raise ImportFailed(f"no account named {name!r}. Create it with `fiscl accounts add`.")
    return row


def _insert_statement(
    conn: sqlite3.Connection, stmt: ParsedStatement, account: sqlite3.Row, import_id: int, aliases
) -> tuple[int, int, int]:
    added = skipped = replaced = 0
    occurrences: dict[tuple, int] = {}
    for t in stmt.txns:
        merchant = normalize_merchant(t.description, aliases)
        if t.fitid and not t.pending:
            key = "f:" + t.fitid
        else:
            # Keyed on the raw description (not the normalized merchant) so editing aliases later
            # can't make an overlapping export look like new transactions.
            ident = (t.posted_date, t.amount, t.description.lower(), t.pending)
            occurrences[ident] = occurrences.get(ident, 0) + 1
            key = ("p:" if t.pending else "") + _dedupe_key(t.posted_date, t.amount, t.description, occurrences[ident])
        if not t.pending:
            # A posted row replaces a matching pending row from up to 7 days earlier.
            start = (date.fromisoformat(t.posted_date) - timedelta(days=7)).isoformat()
            pend = conn.execute(
                """SELECT id FROM transactions WHERE account_id = ? AND is_pending = 1 AND amount = ?
                   AND lower(merchant) = lower(?) AND posted_date BETWEEN ? AND ? ORDER BY posted_date LIMIT 1""",
                (account["id"], t.amount, merchant, start, t.posted_date),
            ).fetchone()
            if pend:
                conn.execute("DELETE FROM transactions WHERE id = ?", (pend["id"],))
                replaced += 1
        cur = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (account_id, posted_date, amount, currency, raw_description, merchant, memo,
                is_pending, fitid, dedupe_key, import_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    source = "ofx" if stmt.format == "ofx" else "csv"
    conn.executemany(
        "INSERT OR REPLACE INTO balances (account_id, as_of, amount, source) VALUES (?, ?, ?, ?)",
        [(account["id"], d, b, source) for d, b in stmt.balances],
    )
    return added, skipped, replaced


def import_file(
    conn: sqlite3.Connection,
    cfg: Config,
    path: Path,
    account_name: str | None = None,
    profile: str | None = None,
    dry_run: bool = False,
    post_process: bool = True,
) -> ImportResult:
    path = Path(path)
    result = ImportResult(filename=path.name, status="ok")
    digest = sha256_file(path)
    prior = conn.execute("SELECT status FROM imports WHERE file_sha256 = ?", (digest,)).fetchone()
    if prior and prior["status"] != "failed":
        result.status = "duplicate_file"
        return result
    try:
        statements = parse_file(path, cfg, profile)
    except ImportFailed as exc:
        return _record_failure(conn, path.name, path.suffix.lstrip(".").lower(), digest, result, str(exc), dry_run)
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
            account = _resolve_account(conn, stmt, account_name)
            account_id = account["id"]
            result.accounts.append(account["name"])
            added, skipped, replaced = _insert_statement(conn, stmt, account, import_id, aliases)
            result.rows_seen += len(stmt.txns)
            result.rows_added += added
            result.rows_skipped += skipped
            result.pending_replaced += replaced
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
