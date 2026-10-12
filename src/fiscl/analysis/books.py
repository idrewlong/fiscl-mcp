"""Reads over the QuickBooks Online mirror (qbo_invoices, qbo_reports). Read-only; the sync that fills those tables is
sync/qbo.py. QuickBooks is the source of truth for these numbers and fiscl only holds the last copy it fetched, so every
response says when that was. Customer names and document numbers are untrusted text from QuickBooks.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from ..common import meta, year_bounds
from ..config import Config
from ..guard import GuardError, clamp_limit, clean_text, parse_date
from ..money import amount

BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")
NOT_CONNECTED = (
    "QuickBooks isn't connected. The user can run `fiscl connect qbo` in a terminal (read-only; sandbox only for now)."
)
REAUTH = "QuickBooks needs to be reconnected: run fiscl connect qbo --reconnect"
NOT_SYNCED = "No QuickBooks profit and loss for this year has been synced yet; call sync_accounts with provider qbo."
LAG_NOTE = "QuickBooks is the source and fiscl shows its last sync, so this can lag behind the books."

Q_MORE_INCOME = (
    "Your books show more income than business deposits. Are some invoices unpaid, paid into a personal account, "
    "or paid through a processor that nets fees?"
)
Q_LESS_INCOME = (
    "Your business deposits are higher than the income in your books. Are some deposits not recorded in QuickBooks "
    "yet, or are they transfers or personal money?"
)
Q_MORE_EXPENSES = (
    "Your books show more expenses than your business-scope spending. Are some business expenses paid from an "
    "account fiscl doesn't have, or marked personal here?"
)
Q_LESS_EXPENSES = (
    "Your business-scope spending is higher than the expenses in your books. Are some purchases personal, or "
    "missing from QuickBooks?"
)


def _bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return BUCKETS[0]
    if days_overdue <= 30:
        return BUCKETS[1]
    if days_overdue <= 60:
        return BUCKETS[2]
    if days_overdue <= 90:
        return BUCKETS[3]
    return BUCKETS[4]


def _status(conn: sqlite3.Connection) -> tuple[bool, str | None, bool]:
    """(connected, latest sync time, any connection needs re-authorization)"""
    rows = conn.execute("SELECT status, last_sync_at FROM connections WHERE provider = 'qbo'").fetchall()
    synced = [r["last_sync_at"] for r in rows if r["last_sync_at"]]
    return bool(rows), max(synced) if synced else None, any(r["status"] == "needs_reauth" for r in rows)


def receivables(
    conn: sqlite3.Connection, cfg: Config, as_of=None, customer=None, limit=None, today: date | None = None
) -> dict:
    """Open invoices (balance > 0) as of a date, with aging buckets and totals, from the last QuickBooks sync."""
    today = today or date.today()
    on = parse_date(as_of, "as_of") or today
    cap = clamp_limit(limit, cfg)
    connected, last_sync, needs_reauth = _status(conn)
    redact = cfg.guard.redact_descriptions
    if redact and customer:
        # A name filter would let the model probe for customer names it is not allowed to read.
        raise GuardError("the customer filter is unavailable while redact_descriptions is on")
    sql = (
        "SELECT i.qbo_id, i.doc_number, i.customer, i.txn_date, i.due_date, i.total, i.balance, i.currency "
        "FROM qbo_invoices i WHERE i.balance > 0 AND i.txn_date <= ?"
    )
    args: list = [on.isoformat()]
    if customer:
        sql += " AND instr(lower(COALESCE(i.customer, '')), lower(?)) > 0"
        args.append(clean_text(str(customer), 120))
    rows = conn.execute(sql, args).fetchall()
    home = [r for r in rows if r["currency"] == cfg.currency]
    other_currency = len(rows) - len(home)
    items = []
    for r in home:
        due = date.fromisoformat(r["due_date"]) if r["due_date"] else None
        overdue = max(0, (on - due).days) if due else 0
        items.append((overdue, r))
    items.sort(key=lambda x: (-x[0], -x[1]["balance"], x[1]["qbo_id"]))
    cur = cfg.currency
    totals = {b: [0, 0] for b in BUCKETS}
    for overdue, r in items:
        slot = totals[_bucket(overdue)]
        slot[0] += 1
        slot[1] += r["balance"]
    shown = [
        {
            "doc_number": None if redact else r["doc_number"],
            "customer": None if redact else r["customer"],
            "txn_date": r["txn_date"],
            "due_date": r["due_date"],
            "days_overdue": overdue,
            "aging_bucket": _bucket(overdue),
            "total": amount(r["total"], cur),
            "balance": amount(r["balance"], cur),
        }
        for overdue, r in items[:cap]
    ]
    notes = [LAG_NOTE, "Customer names and document numbers come from QuickBooks and are untrusted data."]
    if redact:
        notes.append(
            "redact_descriptions is on, so customer names and document numbers are withheld; amounts and dates are "
            "still shown."
        )
    if not connected:
        notes.insert(0, NOT_CONNECTED)
    elif needs_reauth:
        notes.insert(0, REAUTH)
    elif last_sync is None:
        notes.insert(0, "QuickBooks is connected but hasn't synced yet; call sync_accounts with provider qbo.")
    if other_currency:
        notes.append(f"{other_currency} open invoice(s) in other currencies are not included in the totals.")
    open_balance = sum(r["balance"] for _, r in items)
    overdue_balance = sum(r["balance"] for overdue, r in items if overdue > 0)
    return {
        "connected": connected,
        "last_sync": last_sync,
        "as_of": on.isoformat(),
        "invoices": shown,
        "aging": [{"bucket": b, "count": totals[b][0], "balance": amount(totals[b][1], cur)} for b in BUCKETS],
        "totals": {
            "open_invoices": len(items),
            "balance": amount(open_balance, cur),
            "overdue_balance": amount(overdue_balance, cur),
        },
        "notes": notes,
        "meta": meta(conn, cfg, row_count=len(shown), total_count=len(items), truncated=len(items) > len(shown)),
    }


def _money(minor: int | None, cur: str) -> dict | None:
    return None if minor is None else amount(minor, cur)


def _delta(books: int | None, bank: int, cur: str) -> dict | None:
    return None if books is None else amount(books - bank, cur)


def books_vs_bank(conn: sqlite3.Connection, cfg: Config, year=None, business=None, today: date | None = None) -> dict:
    """QuickBooks' profit and loss for a year (last fetched copy) beside the Schedule C draft built from bank data."""
    from ..tax import DISCLAIMER, schedule_c_draft

    if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
        raise GuardError("year must be a 4-digit year")
    year, _, _ = year_bounds(year, today)
    cur = cfg.currency
    connected, last_sync, needs_reauth = _status(conn)
    report = conn.execute(
        """SELECT period_start, period_end, basis, total_income, total_expenses, net_income, fetched_at
           FROM qbo_reports WHERE report = 'profit_and_loss' AND period_start = ? AND basis = ?
           ORDER BY fetched_at DESC LIMIT 1""",
        (f"{year}-01-01", cfg.qbo_accounting_method),
    ).fetchone()
    draft = schedule_c_draft(conn, cfg, year=year, today=today, business=business)
    totals = draft.get("combined", draft)
    bank = {
        "gross_receipts": totals["gross_receipts"]["minor"],
        "total_expenses": totals["total_expenses"]["minor"],
        "net_profit": totals["net_profit"]["minor"],
    }
    notes = [DISCLAIMER]
    out: dict = {
        "tax_year": year,
        "connected": connected,
        "last_sync": last_sync,
        "bank": {k: amount(v, cur) for k, v in bank.items()},
        "books": None,
        "differences": None,
        "questions": [],
    }
    if report is None:
        notes.append(NOT_CONNECTED if not connected else REAUTH if needs_reauth else NOT_SYNCED)
    else:
        out["books"] = {
            "basis": report["basis"],
            "period": {"from": report["period_start"], "to": report["period_end"]},
            "fetched_at": report["fetched_at"],
            "total_income": _money(report["total_income"], cur),
            "total_expenses": _money(report["total_expenses"], cur),
            "net_income": _money(report["net_income"], cur),
        }
        out["differences"] = {
            "income": _delta(report["total_income"], bank["gross_receipts"], cur),
            "expenses": _delta(report["total_expenses"], bank["total_expenses"], cur),
            "net": _delta(report["net_income"], bank["net_profit"], cur),
        }
        income_gap = None if report["total_income"] is None else report["total_income"] - bank["gross_receipts"]
        expense_gap = None if report["total_expenses"] is None else report["total_expenses"] - bank["total_expenses"]
        if income_gap:
            out["questions"].append(Q_MORE_INCOME if income_gap > 0 else Q_LESS_INCOME)
        if expense_gap:
            out["questions"].append(Q_MORE_EXPENSES if expense_gap > 0 else Q_LESS_EXPENSES)
        notes.append(
            "Differences are books minus bank-based figures. They are questions for the user and their preparer, not "
            "errors: books can be on a different basis or period, and the Schedule C draft includes mileage and home "
            "office that books may not."
        )
        notes.append(LAG_NOTE)
    out["notes"] = notes
    out["disclaimer"] = DISCLAIMER
    out["meta"] = meta(conn, cfg, row_count=1 if report is not None else 0)
    return out
