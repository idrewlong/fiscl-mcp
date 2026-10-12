"""Several businesses under one taxpayer: the `businesses` table, manage_business, list_businesses, lookups.

A row is assigned to a business at the split, transaction, or account level; with none of those it belongs to the
default business (id 1, renamable, never archived). Which business owns a row only matters where the row's scope is
business, so every query pairs `BUSINESS_SQL` with `SCOPE_SQL = 'business'`.

Error text never contains a business name; names travel only in structured response fields.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from ..categorize import rules_naming_business
from ..common import meta, plan_result, with_meta, year_bounds
from ..config import Config
from ..db import BUSINESS_SQL, DEFAULT_BUSINESS_ID, INCOME_FILTER, SCOPE_SQL, SPENDING_FILTER, TXN_FROM
from ..guard import GuardError, choice, clean_text
from ..money import amount
from .lines import DISCLAIMER

MAX_NAME = 60
ACTIONS = ("add", "rename", "archive")


def _name(value, what: str) -> str:
    if not isinstance(value, str):
        raise GuardError(f"{what} must be text of 1 to {MAX_NAME} characters", code="needs_terms")
    name = clean_text(value, 1000)
    if not 1 <= len(name) <= MAX_NAME:
        raise GuardError(f"{what} must be 1 to {MAX_NAME} characters", code="needs_terms")
    return name


def get_business(conn: sqlite3.Connection, name, *, allow_archived: bool = True) -> sqlite3.Row:
    """A business by name (case-insensitive). Unknown names raise `not_found` without echoing the name."""
    if not isinstance(name, str):
        raise GuardError("business must be a business name; list_businesses shows them")
    row = conn.execute("SELECT * FROM businesses WHERE name = ?", (clean_text(name, 1000),)).fetchone()
    if row is None:
        raise GuardError("unknown business; list_businesses shows them", code="not_found")
    if row["archived"] and not allow_archived:
        raise GuardError("that business is archived; choose an active business", code="invalid")
    return row


def default_business(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM businesses WHERE id = ?", (DEFAULT_BUSINESS_ID,)).fetchone()


def businesses_for_period(conn: sqlite3.Connection, start: date, end: date) -> list[sqlite3.Row]:
    """Businesses a report covers: every active one, plus any archived one with business rows in the period."""
    if conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1:
        return [default_business(conn)]
    return conn.execute(
        f"""SELECT b.* FROM businesses b
            WHERE b.archived = 0 OR b.id IN (
                SELECT {BUSINESS_SQL} FROM {TXN_FROM}
                WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business')
            ORDER BY b.id""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


def business_view(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"]}


def _has_open_activity(conn: sqlite3.Connection, business_id: int, today: date) -> bool:
    """Business rows in the current tax year, or 1099 forms for it, mean the business is still in use."""
    start = date(today.year, 1, 1).isoformat()
    row = conn.execute(
        f"""SELECT 1 FROM {TXN_FROM}
            WHERE t.posted_date >= ? AND {SCOPE_SQL} = 'business' AND {BUSINESS_SQL} = ? LIMIT 1""",
        (start, business_id),
    ).fetchone()
    if row:
        return True
    forms = conn.execute(
        "SELECT 1 FROM forms_1099 WHERE COALESCE(business_id, ?) = ? AND tax_year >= ? LIMIT 1",
        (DEFAULT_BUSINESS_ID, business_id, today.year),
    ).fetchone()
    return forms is not None


def manage_business(conn, cfg: Config, action=None, name=None, new_name=None, dry_run=True, today=None) -> dict:
    """Add, rename, or archive a business. Dry run by default; archiving needs no activity in the current year."""
    today = today or date.today()
    action = choice(action, ACTIONS, "action")
    name = _name(name, "name")
    if action == "add":
        if conn.execute("SELECT 1 FROM businesses WHERE name = ?", (name,)).fetchone():
            raise GuardError("a business with that name already exists", code="exists")
        change = {"action": "add", "name": name}
        if not dry_run:
            conn.execute(
                "INSERT INTO businesses (name, created_at) VALUES (?, ?)",
                (name, datetime.now().isoformat(timespec="seconds")),
            )
            change["id"] = conn.execute("SELECT id FROM businesses WHERE name = ?", (name,)).fetchone()[0]
    else:
        row = get_business(conn, name)
        # Every import and sync resolves rule businesses by name, so a rename or archive would break them all.
        named_by = rules_naming_business(cfg, row["name"])
        if named_by:
            raise GuardError(
                f"rules.toml rule {named_by[0]} names this business; change the rule first", code="invalid"
            )
        if action == "rename":
            if new_name is None:
                raise GuardError("new_name is required to rename a business", code="needs_terms")
            new = _name(new_name, "new_name")
            clash = conn.execute("SELECT id FROM businesses WHERE name = ?", (new,)).fetchone()
            if clash and clash["id"] != row["id"]:
                raise GuardError("a business with that name already exists", code="exists")
            change = {"action": "rename", "id": row["id"], "from": row["name"], "to": new}
            if not dry_run:
                conn.execute("UPDATE businesses SET name = ? WHERE id = ?", (new, row["id"]))
        else:
            if row["id"] == DEFAULT_BUSINESS_ID:
                raise GuardError("the default business can be renamed but not archived", code="invalid")
            if row["archived"]:
                raise GuardError("that business is already archived", code="invalid")
            if _has_open_activity(conn, row["id"], today):
                raise GuardError(
                    "that business has transactions or 1099 forms in the current tax year; "
                    "reassign them with set_scope before archiving",
                    code="invalid",
                )
            change = {"action": "archive", "id": row["id"], "name": row["name"]}
            if not dry_run:
                conn.execute("UPDATE businesses SET archived = 1 WHERE id = ?", (row["id"],))
    result = plan_result([change], 0, dry_run, "Call again with dry_run=false to apply.")
    return with_meta(conn, cfg, result)


def list_businesses(conn, cfg: Config, year=None, today=None) -> dict:
    """Each business with its accounts and the year's gross receipts and business spending."""
    year, start, end = year_bounds(year, today)
    cur = cfg.currency
    span = (start.isoformat(), end.isoformat())
    gross = {
        r["b"]: r["g"]
        for r in conn.execute(
            f"""SELECT {BUSINESS_SQL} AS b, SUM(t.amount) AS g FROM {TXN_FROM}
                WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' AND {INCOME_FILTER}
                GROUP BY b""",
            span,
        )
    }
    spent = {
        r["b"]: r["s"]
        for r in conn.execute(
            f"""SELECT {BUSINESS_SQL} AS b, -SUM(t.amount) AS s FROM {TXN_FROM}
                WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' AND {SPENDING_FILTER}
                GROUP BY b""",
            span,
        )
    }
    accounts: dict[int, list[str]] = {}
    for r in conn.execute("SELECT name, business_id, scope FROM accounts WHERE archived = 0 ORDER BY name"):
        if r["business_id"] is not None:
            accounts.setdefault(r["business_id"], []).append(r["name"])
        elif r["scope"] in ("business", "mixed"):
            accounts.setdefault(DEFAULT_BUSINESS_ID, []).append(r["name"])
    rows = conn.execute("SELECT * FROM businesses ORDER BY id").fetchall()
    return {
        "tax_year": year,
        "businesses": [
            {
                "id": b["id"],
                "name": b["name"],
                "archived": bool(b["archived"]),
                "is_default": b["id"] == DEFAULT_BUSINESS_ID,
                "accounts": accounts.get(b["id"], []),
                "gross_receipts": amount(gross.get(b["id"], 0), cur),
                "expenses": amount(spent.get(b["id"], 0), cur),
            }
            for b in rows
        ],
        "notes": [
            DISCLAIMER,
            "A transaction belongs to its split's business, else its own, else its account's, else the default "
            "business. Set them with set_scope (business) or split_transaction.",
        ],
        "meta": meta(conn, cfg, start, end, row_count=len(rows)),
    }
