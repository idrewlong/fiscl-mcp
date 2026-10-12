"""Orchestration over connectors: which providers are connected, run a sync, list and forget connections.

A new provider plugs in by adding a branch to `build_connector`; the import pipeline, dedupe,
transfer matching, rules, and recurring detection are shared.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from datetime import date, timedelta

from ..common import meta
from ..config import Config
from ..guard import GuardError, choice
from ..importers.core import ImportResult, import_statements, post_import
from . import connections, plaid, simplefin
from .base import PROVIDERS, Connector, SyncError
from .teller import TellerConnector, TellerItem

MAX_FIRST_SYNC_DAYS = 90
FOLLOW_UP_DAYS = 14
SIMPLEFIN_REF = connections.item_ref_hash("simplefin", "access_url")


def connected_providers(conn: sqlite3.Connection) -> list[str]:
    found = {r["provider"] for r in connections.list_rows(conn)}
    if "simplefin" not in found and simplefin.is_connected():  # connected before the connections table existed
        found.add("simplefin")
    return [p for p in PROVIDERS if p in found]


def build_connector(provider: str, conn: sqlite3.Connection, cfg: Config, opener=None, institution: str | None = None):
    rows = connections.list_rows(conn, provider, institution)
    if provider == "teller":
        items = [TellerItem(r["item_ref_hash"], r["institution"]) for r in rows]
        return TellerConnector(cfg, items, opener=opener)
    if provider == "plaid":
        return plaid.PlaidConnector(
            cfg, [plaid.PlaidItem(r["item_ref_hash"], r["institution"], r["cursor"]) for r in rows], opener
        )
    if provider == "simplefin":
        return simplefin.SimpleFinConnector(cfg)
    raise SyncError("unknown provider")


def _digest(provider: str, start: date, end: date, statements) -> str:
    payload = json.dumps([dataclasses.asdict(s) for s in statements], sort_keys=True, default=str)
    return f"{provider}:{start}:{end}:" + hashlib.sha256(payload.encode()).hexdigest()


def _since_days(conn: sqlite3.Connection, provider: str, days: int | None) -> int:
    if days is None:
        last = conn.execute(
            "SELECT MAX(imported_at) FROM imports WHERE format = ? AND status = 'ok'", (provider,)
        ).fetchone()[0]
        days = FOLLOW_UP_DAYS if last else MAX_FIRST_SYNC_DAYS
    return max(1, min(int(days), 365))


def run_connector(
    conn: sqlite3.Connection, cfg: Config, connector: Connector, days: int | None = None, today: date | None = None
) -> dict:
    """Fetch through a connector, import through the shared pipeline, and record per-connection status."""
    today = today or date.today()
    start = today - timedelta(days=_since_days(conn, connector.provider, days) - 1)
    try:
        statements = connector.fetch(start, today)
    finally:
        _record_status(conn, connector)
    result: ImportResult | None = None
    digest = _digest(connector.provider, start, today, statements)
    already = conn.execute("SELECT 1 FROM imports WHERE file_sha256 = ? AND status = 'ok'", (digest,)).fetchone()
    if statements and not already:
        result = import_statements(
            conn, cfg, statements, f"{connector.provider.title()} sync {start}..{today}", digest, post_process=False
        )
    finalize = getattr(connector, "finalize", None)  # cursor connectors: advance bookmarks only after a good import
    if finalize and (result is None or result.status == "ok"):
        finalize(conn)
    post = post_import(conn, cfg, today=today)
    return {
        "accounts": sorted(set(result.accounts)) if result else [],
        "rows_added": result.rows_added if result else 0,
        "rows_skipped": result.rows_skipped if result else 0,
        "warnings": list(connector.warnings),
        "post_import": post,
    }


def _record_status(conn: sqlite3.Connection, connector) -> None:
    statuses = getattr(connector, "item_status", {})
    inst = getattr(connector, "item_institution", {})
    for ref, status in statuses.items():
        if status == "ok":
            connections.mark_synced(conn, connector.provider, ref, inst.get(ref))
        else:
            connections.set_status(conn, connector.provider, ref, status)


def sync_accounts(
    conn: sqlite3.Connection,
    cfg: Config,
    provider: str = "all",
    days: int | None = None,
    today: date | None = None,
    opener=None,
) -> dict:
    """Sync one provider, or every connected one. Unconnected providers are skipped with a note."""
    choice(provider, ("all", *PROVIDERS), "provider")
    connected = connected_providers(conn)
    if provider != "all" and provider not in connected:
        raise GuardError(f"{provider} isn't connected. The user can run `fiscl connect {provider}`.", code="not_found")
    targets = connected if provider == "all" else [provider]
    results: dict[str, dict] = {}
    errors: list[dict] = []
    for p in targets:
        try:
            if p == "simplefin":
                out = simplefin.sync(conn, cfg, days=days, today=today)
                connections.add_connection(conn, "simplefin", "SimpleFIN Bridge", SIMPLEFIN_REF)
                connections.mark_synced(conn, "simplefin", SIMPLEFIN_REF)
            else:
                out = run_connector(conn, cfg, build_connector(p, conn, cfg, opener), days, today)
            results[p] = out
        except SyncError as exc:
            if p == "simplefin" and exc.status in (401, 403):
                connections.set_status(conn, "simplefin", SIMPLEFIN_REF, "needs_reauth")
            errors.append({"provider": p, "error": str(exc)})
    if provider != "all" and errors and not results:
        conn.commit()  # keep connection-status updates (e.g. needs_reauth) even though the call fails
        raise GuardError(errors[0]["error"], code="sync_failed")
    skipped = [{"provider": p, "reason": "not connected"} for p in PROVIDERS if p not in targets and provider == "all"]
    note = None
    if not targets:
        note = "No bank is connected yet. The user can connect one with `fiscl connect teller`."
    elif errors:
        note = "Some providers failed; see errors. Data from the others was imported."
    return {
        "results": results,
        "skipped": skipped,
        "errors": errors,
        "totals": {"rows_added": sum(r["rows_added"] for r in results.values())},
        "meta": meta(conn, cfg, row_count=len(results), **({"note": note} if note else {})),
    }


def list_connections(conn: sqlite3.Connection, cfg: Config, today: date | None = None) -> dict:
    """What's connected: provider, institution, account names, last sync, status. Never a token."""
    rows = connections.list_rows(conn)
    out = []
    for r in rows:
        accounts = [
            a["name"]
            for a in conn.execute(
                """SELECT DISTINCT a.name FROM accounts a
                      JOIN transactions t ON t.account_id = a.id JOIN imports i ON i.id = t.import_id
                    WHERE i.format = ? AND (? = 'simplefin' OR lower(COALESCE(a.institution, '')) = lower(?))
                    ORDER BY a.name""",
                (r["provider"], r["provider"], r["institution"] or ""),
            )
        ]
        out.append(
            {
                "provider": r["provider"],
                "institution": r["institution"],
                "accounts": accounts,
                "last_sync": r["last_sync_at"],
                "status": r["status"],
            }
        )
    present = {r["provider"] for r in rows}
    note = "Connections are made by the user in a terminal: `fiscl connect teller|plaid|simplefin`."
    return {
        "connections": out,
        "not_connected": [p for p in PROVIDERS if p not in present],
        "meta": meta(conn, cfg, row_count=len(out), note=note),
    }


def forget(conn: sqlite3.Connection, cfg: Config, provider: str, institution: str | None = None, opener=None) -> None:
    """Revoke at the provider, delete the keychain entries, drop the connection rows."""
    choice(provider, PROVIDERS, "provider")
    rows = connections.list_rows(conn, provider, institution)
    if provider == "simplefin":
        simplefin.forget()
    elif provider == "teller":
        connector = build_connector(provider, conn, cfg, opener, institution)
        connector.forget()  # raises (keeping the token) if Teller couldn't be told
        if institution is None:
            from .teller import forget_app_credentials

            forget_app_credentials()
    elif provider == "plaid":
        connector = build_connector(provider, conn, cfg, opener, institution)
        connector.forget()  # raises (keeping the token) if Plaid couldn't be told
        if institution is None:
            plaid.forget_app_credentials()
    connections.delete_rows(conn, provider, [r["item_ref_hash"] for r in rows])
    conn.commit()
