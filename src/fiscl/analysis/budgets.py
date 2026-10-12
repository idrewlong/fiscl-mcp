"""Budgets: budget_status (pace + rollover) and suggest_budgets."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from ..common import meta
from ..config import Config, load_budgets
from ..db import SPENDING_FILTER, TXN_FROM
from ..guard import GuardError, parse_month
from ..money import amount, pct
from ..recurring import add_months


def _spent_by_category(conn, start: date, end: date) -> list[tuple[str | None, int]]:
    """(category name or None for uncategorized, net spending) for each category in the range."""
    rows = conn.execute(
        f"""SELECT c.name AS name, -SUM(t.amount) AS spent FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SPENDING_FILTER} GROUP BY c.id""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [(r["name"], r["spent"]) for r in rows]


def budget_status(conn, cfg: Config, month=None, today=None) -> dict:
    today = today or date.today()
    first = parse_month(month, today)
    days_in = calendar.monthrange(first.year, first.month)[1]
    last = first.replace(day=days_in)
    if first > today:
        raise GuardError("month is in the future")
    elapsed = days_in if last < today else today.day
    budgets = load_budgets(cfg)
    spent_rows = [(name or "Uncategorized", s) for name, s in _spent_by_category(conn, first, last)]
    spent = {name.lower(): s for name, s in spent_rows}

    # Rollover: carry last month's unspent (or overspent) amount forward.
    prev = {}
    if any(b.rollover for b in budgets):
        prev_rows = _spent_by_category(conn, add_months(first, -1), first - timedelta(days=1))
        prev = {(name or "Uncategorized").lower(): s for name, s in prev_rows}

    lines, total_budget, total_spent = [], 0, 0
    for b in budgets:
        available = b.monthly + ((b.monthly - prev.get(b.category.lower(), 0)) if b.rollover else 0)
        s = spent.get(b.category.lower(), 0)
        projected = s * days_in // elapsed if elapsed else s
        if s > available:
            status = "over"
        elif projected > available:
            status = "at_risk"
        else:
            status = "on_track"
        lines.append(
            {
                "category": b.category,
                "budget": amount(available, cfg.currency),
                "spent": amount(s, cfg.currency),
                "remaining": amount(available - s, cfg.currency),
                "used_pct": pct(s, available),
                "projected_month_end": amount(projected, cfg.currency),
                "status": status,
                "rollover": b.rollover,
            }
        )
        total_budget += available
        total_spent += s
    budgeted = {b.category.lower() for b in budgets}
    unbudgeted = sorted(
        (
            {"category": name, "spent": amount(v, cfg.currency)}
            for name, v in spent_rows
            if name.lower() not in budgeted and v > 0
        ),
        key=lambda x: -x["spent"]["minor"],
    )
    return {
        "month": first.strftime("%Y-%m"),
        "days_elapsed": elapsed,
        "days_in_month": days_in,
        "budgets": lines,
        "totals": {
            "budget": amount(total_budget, cfg.currency),
            "spent": amount(total_spent, cfg.currency),
            "remaining": amount(total_budget - total_spent, cfg.currency),
        },
        "unbudgeted_spending": unbudgeted[:15],
        "meta": meta(
            conn,
            cfg,
            first,
            last,
            row_count=len(lines),
            note="projected_month_end assumes spending continues at this month's daily pace.",
        ),
    }


def suggest_budgets(conn, cfg: Config, months=3, today=None) -> dict:
    """Suggested monthly budgets from the median of the last N full months, rounded up to $10."""
    today = today or date.today()
    if not isinstance(months, int) or not 1 <= months <= 12:
        raise GuardError("months must be between 1 and 12")
    end_month = today.replace(day=1)
    starts = [add_months(end_month, -i) for i in range(months, 0, -1)]
    per_cat: dict[str, list[int]] = {}
    for idx, s in enumerate(starts):
        for name, spent in _spent_by_category(conn, s, add_months(s, 1) - timedelta(days=1)):
            if name is None:
                continue  # uncategorized spending can't be budgeted
            per_cat.setdefault(name, [0] * months)[idx] = max(spent, 0)
    unit = 1000  # $10 in cents
    suggestions = []
    for name, vals in per_cat.items():
        med = sorted(vals)[len(vals) // 2]
        rounded = -(-med // unit) * unit
        if rounded > 0:
            suggestions.append(
                {
                    "category": name,
                    "suggested_monthly": amount(rounded, cfg.currency),
                    "monthly_history": [amount(v, cfg.currency) for v in vals],
                }
            )
    suggestions.sort(key=lambda x: -x["suggested_monthly"]["minor"])
    return {
        "months_used": [s.strftime("%Y-%m") for s in starts],
        "suggestions": suggestions,
        "meta": meta(conn, cfg, note="Suggestions only. Add the ones you want to budgets.toml."),
    }
