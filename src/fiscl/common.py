"""Helpers shared by the service modules (analysis, tax): response meta, filters, edit targets, dry-run plans."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from typing import Any

from .config import Config
from .db import SCOPE_SQL
from .guard import GuardError, choice, clean_text
from .money import AmountError, to_minor

MAX_BULK_ROWS = 500
MAX_EDIT_ITEMS = 100
SCOPES = ("all", "personal", "business")


# --- response meta -----------------------------------------------------------------------


def data_freshness(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT a.name, MAX(t.posted_date) AS latest FROM accounts a
           LEFT JOIN transactions t ON t.account_id = a.id
           WHERE a.archived = 0 GROUP BY a.id ORDER BY a.name"""
    ).fetchall()
    return [{"account": r["name"], "latest_transaction": r["latest"]} for r in rows]


def meta(conn, cfg: Config, start: date | None = None, end: date | None = None, **extra) -> dict:
    out: dict[str, Any] = {"currency": cfg.currency, "data_freshness": data_freshness(conn)}
    if start and end:
        out["period"] = {"from": start.isoformat(), "to": end.isoformat()}
    out.update(extra)
    return out


# --- arguments ---------------------------------------------------------------------------


def money_arg(value, cfg: Config, field: str) -> int | None:
    """Dollars from a tool argument → minor units, or None."""
    if value is None:
        return None
    try:
        return to_minor(value, cfg.currency)
    except AmountError as exc:
        raise GuardError(f"{field}: {exc}") from exc


def account_id(conn, name: str | None) -> int | None:
    if not name:
        return None
    row = conn.execute("SELECT id FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row is None:
        names = [r[0] for r in conn.execute("SELECT name FROM accounts WHERE archived = 0")]
        raise GuardError(f"unknown account {name!r}. Accounts: {', '.join(names) or 'none yet'}")
    return row[0]


def year_bounds(year: int | None, today: date | None = None) -> tuple[int, date, date]:
    today = today or date.today()
    year = year or today.year
    if not isinstance(year, int) or not 2000 <= year <= today.year + 1:
        raise GuardError("year must be a 4-digit year")
    return year, date(year, 1, 1), date(year, 12, 31)


# --- SQL filters -------------------------------------------------------------------------


def scope_sql(scope: str | None) -> tuple[str, list]:
    """SQL fragment (needs `accounts a` joined) restricting rows to personal or business money."""
    scope = choice(scope or "all", SCOPES, "scope")
    return ("", []) if scope == "all" else (f"AND {SCOPE_SQL} = ?", [scope])


def account_scope_sql(scope: str | None) -> str:
    """Restrict accounts (alias-free) for balance-based tools."""
    scope = choice(scope or "all", SCOPES, "scope")
    return {"all": "", "personal": "AND scope != 'business'", "business": "AND scope != 'personal'"}[scope]


def txn_filter(account: int | None, scope: str | None) -> tuple[str, list]:
    """Optional account and scope restrictions as one `AND …` fragment (needs `t` and `a` aliases)."""
    sql, params = "", []
    if account is not None:
        sql, params = "AND t.account_id = ?", [account]
    s_sql, s_params = scope_sql(scope)
    return f"{sql} {s_sql}", params + s_params


def latest_balance(conn, account: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT amount, as_of FROM balances WHERE account_id = ? ORDER BY as_of DESC LIMIT 1", (account,)
    ).fetchone()


# --- edits -------------------------------------------------------------------------------


def check_items(items) -> None:
    if not isinstance(items, list) or not items:
        raise GuardError("items must be a non-empty list")
    if len(items) > MAX_EDIT_ITEMS:
        raise GuardError(f"at most {MAX_EDIT_ITEMS} items per call")


def resolve_target(conn, item: dict, i: int) -> tuple[str | None, list[int] | None, list]:
    """Find the transactions an edit item points at: {merchant} or {transaction_ids}. Transfers are excluded."""
    merchant = item.get("merchant")
    ids = item.get("transaction_ids")
    if bool(merchant) == bool(ids):
        raise GuardError(f"item {i}: give exactly one of merchant or transaction_ids")
    select = """SELECT t.id, COALESCE(c.name, 'Uncategorized') AS current, t.scope AS scope
                FROM transactions t LEFT JOIN categories c ON c.id = t.category_id"""
    if merchant:
        merchant = clean_text(str(merchant), 120)
        rows = conn.execute(
            f"{select} WHERE t.merchant = ? COLLATE NOCASE AND t.transfer_group IS NULL", (merchant,)
        ).fetchall()
    else:
        if not isinstance(ids, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in ids):
            raise GuardError(f"item {i}: transaction_ids must be a list of integers")
        if len(ids) > MAX_BULK_ROWS:
            raise GuardError(f"item {i}: at most {MAX_BULK_ROWS} transaction_ids")
        rows = conn.execute(
            f"{select} WHERE t.id IN ({','.join('?' * len(ids))}) AND t.transfer_group IS NULL", ids
        ).fetchall()
    return merchant, ids, rows


def target_of(merchant: str | None, ids: list[int] | None) -> dict:
    return {"merchant": merchant} if merchant else {"transaction_ids": ids}


def count_by(rows, key) -> dict[str, int]:
    return dict(Counter(key(r) for r in rows))


def check_bulk(total: int) -> None:
    if total > MAX_BULK_ROWS:
        raise GuardError(f"this would change {total} transactions; the limit is {MAX_BULK_ROWS} per call")


def plan_result(plan: list[dict], total: int, dry_run: bool, next_step: str) -> dict:
    """The dry-run envelope every local write returns. Keys starting with `_` are internal and dropped."""
    for p in plan:
        for k in [k for k in p if k.startswith("_")]:
            del p[k]
    return {
        "dry_run": bool(dry_run),
        "changes": plan,
        "transactions_affected": total,
        "next_step": next_step if dry_run else "Applied.",
    }
