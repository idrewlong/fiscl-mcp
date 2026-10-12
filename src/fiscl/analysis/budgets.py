"""Budgets: budget_status (pace + rollover) and suggest_budgets."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from ..common import check_items, meta, money_arg, plan_result, with_meta
from ..config import Budget, Config, load_budgets, write_budgets
from ..db import SPENDING_FILTER, TXN_FROM
from ..guard import GuardError, clean_text, parse_month
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


def _line(b: Budget, cfg: Config) -> dict:
    return {"category": b.category, "monthly": amount(b.monthly, cfg.currency), "rollover": b.rollover}


def set_budget(conn, cfg: Config, items: list[dict], dry_run=True) -> dict:
    """Add, change, or remove monthly budgets in budgets.toml (monthly=null removes). Dry run by default."""
    check_items(items)
    current = {b.category.lower(): b for b in load_budgets(cfg)}
    after = dict(current)
    plan, seen = [], set()
    for i, item in enumerate(items, start=1):
        category = clean_text(str(item.get("category") or ""), 60)
        if not category:
            raise GuardError(f"item {i}: category is required")
        key = category.lower()
        if key in seen:
            raise GuardError(f"item {i}: that category appears twice")
        seen.add(key)
        if "monthly" not in item:
            raise GuardError(f"item {i}: monthly is required (dollars, or null to remove the budget)")
        old = current.get(key)
        if item["monthly"] is None:
            if old is None:
                raise GuardError(f"item {i}: no budget exists for that category", code="not_found")
            del after[key]
            plan.append({"category": old.category, "action": "remove", "before": _line(old, cfg), "after": None})
            continue
        monthly = money_arg(item["monthly"], cfg, f"item {i} monthly")
        if monthly is None or monthly <= 0:
            raise GuardError(f"item {i}: monthly must be a positive amount")
        rollover = item.get("rollover", old.rollover if old else False)
        if not isinstance(rollover, bool):
            raise GuardError(f"item {i}: rollover must be true or false")
        new = Budget(category=old.category if old else category, monthly=monthly, rollover=rollover)
        after[key] = new
        if old is None:
            action = "add"
        else:
            action = "unchanged" if (old.monthly, old.rollover) == (monthly, rollover) else "update"
        exists = conn.execute("SELECT 1 FROM categories WHERE name = ? COLLATE NOCASE", (new.category,)).fetchone()
        plan.append(
            {
                "category": new.category,
                "action": action,
                "before": _line(old, cfg) if old else None,
                "after": _line(new, cfg),
                "category_exists": exists is not None,
            }
        )
    if not dry_run:
        write_budgets(cfg, list(after.values()))
    result = plan_result(plan, 0, dry_run, "Call again with dry_run=false to write budgets.toml (a .bak is kept).")
    result["budgets_after"] = [_line(b, cfg) for b in sorted(after.values(), key=lambda b: b.category.lower())]
    return with_meta(conn, cfg, result)
