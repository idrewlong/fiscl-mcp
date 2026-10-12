"""The `connections` table: which providers/institutions are connected. Metadata only, never a token.

A connection's secret lives in the OS keychain under `fiscl:<provider>:<item_ref_hash>`.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

from ..guard import clean_text
from .base import PROVIDERS, SyncError

STATUSES = ("ok", "needs_reauth", "error")


def item_ref_hash(provider: str, raw_id: str) -> str:
    """A stable, non-reversible handle for a provider's enrollment/item id (used as the keychain ref)."""
    return hashlib.sha256(f"{provider}:{raw_id}".encode()).hexdigest()[:32]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def add_connection(conn: sqlite3.Connection, provider: str, institution: str | None, ref_hash: str) -> int:
    if provider not in PROVIDERS:
        raise SyncError("unknown provider")
    inst = clean_text(institution, 80) or None
    conn.execute(
        """INSERT INTO connections (provider, institution, item_ref_hash, created_at, status)
           VALUES (?, ?, ?, ?, 'ok')
           ON CONFLICT (provider, item_ref_hash)
           DO UPDATE SET institution = COALESCE(excluded.institution, institution), status = 'ok'""",
        (provider, inst, ref_hash, _now()),
    )
    row = conn.execute(
        "SELECT id FROM connections WHERE provider = ? AND item_ref_hash = ?", (provider, ref_hash)
    ).fetchone()
    return int(row[0])


def list_rows(
    conn: sqlite3.Connection, provider: str | None = None, institution: str | None = None
) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM connections WHERE 1=1", []
    if provider:
        sql += " AND provider = ?"
        args.append(provider)
    if institution:
        sql += " AND lower(institution) = lower(?)"
        args.append(institution)
    return conn.execute(sql + " ORDER BY provider, id", args).fetchall()


def set_status(conn: sqlite3.Connection, provider: str, ref_hash: str, status: str) -> None:
    if status not in STATUSES:
        raise SyncError("invalid connection status")
    conn.execute(
        "UPDATE connections SET status = ? WHERE provider = ? AND item_ref_hash = ?", (status, provider, ref_hash)
    )


def mark_synced(conn: sqlite3.Connection, provider: str, ref_hash: str, institution: str | None = None) -> None:
    conn.execute(
        """UPDATE connections SET last_sync_at = ?, status = 'ok', institution = COALESCE(?, institution)
           WHERE provider = ? AND item_ref_hash = ?""",
        (_now(), clean_text(institution, 80) or None, provider, ref_hash),
    )


def delete_rows(conn: sqlite3.Connection, provider: str, ref_hashes: list[str]) -> None:
    conn.executemany(
        "DELETE FROM connections WHERE provider = ? AND item_ref_hash = ?", [(provider, r) for r in ref_hashes]
    )
