"""Spending and cash flow: spending_summary, cash_flow, merchant_history, category_trend."""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

from ..common import account_id, meta, txn_filter
from ..config import Config, load_budgets
from ..db import INCOME_FILTER, SPENDING_FILTER, TXN_FROM
from ..guard import GuardError, choice, clean_text, date_range
from ..money import amount, pct
from ..recurring import add_months

_GROUP_SQL = {
    "category": "COALESCE(c.name, 'Uncategorized')",
    "merchant": "COALESCE(t.merchant, '(unknown)')",
    "account": "a.name",
    "month": "substr(t.posted_date, 1, 7)",
}


def _shift_period(start: date, end: date, mode: str) -> tuple[date, date]:
    if mode == "same_period_last_year":
        return add_months(start, -12), add_months(end, -12)
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def _spending_by(
    conn, group_by: str, start: date, end: date, account: int | None, scope: str = "all"
) -> dict[str, tuple[int, int]]:
    f_sql, f_params = txn_filter(account, scope)
    rows = conn.execute(
        f"""SELECT {_GROUP_SQL[group_by]} AS k, -SUM(t.amount) AS spent, COUNT(*) AS n
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SPENDING_FILTER} {f_sql}
            GROUP BY k""",
        [start.isoformat(), end.isoformat(), *f_params],
    ).fetchall()
    return {r["k"]: (r["spent"], r["n"]) for r in rows}


def spending_summary(
    conn,
    cfg: Config,
    from_=None,
    to=None,
    group_by="category",
    compare_to="previous_period",
    top_n=10,
    account=None,
    scope="all",
    today=None,
) -> dict:
    start, end = date_range(from_, to, default_days=30, today=today)
    choice(group_by, tuple(_GROUP_SQL), "group_by")
    choice(compare_to, ("previous_period", "same_period_last_year", "none"), "compare_to")
    if not isinstance(top_n, int) or not 1 <= top_n <= 50:
        raise GuardError("top_n must be between 1 and 50")
    acct = account_id(conn, account)
    current = _spending_by(conn, group_by, start, end, acct, scope)
    prior: dict[str, tuple[int, int]] = {}
    prior_range = None
    if compare_to != "none" and group_by != "month":
        p_start, p_end = _shift_period(start, end, compare_to)
        prior = _spending_by(conn, group_by, p_start, p_end, acct, scope)
        prior_range = {"from": p_start.isoformat(), "to": p_end.isoformat()}

    total = sum(v[0] for v in current.values())
    prior_total = sum(v[0] for v in prior.values())
    ordered = sorted(current.items(), key=lambda kv: kv[0] if group_by == "month" else -kv[1][0])
    if group_by != "month" and len(ordered) > top_n:
        head, tail = ordered[:top_n], ordered[top_n:]
        other = (sum(v[0] for _, v in tail), sum(v[1] for _, v in tail))
        ordered = [*head, (f"Other ({len(tail)} groups)", other)]
    groups = []
    for key, (spent, n) in ordered:
        g = {"name": key, "spent": amount(spent, cfg.currency), "count": n, "share_pct": pct(spent, total)}
        if prior_range is not None and not key.startswith("Other ("):
            before = prior.get(key, (0, 0))[0]
            g["prior_spent"] = amount(before, cfg.currency)
            g["change"] = amount(spent - before, cfg.currency)
            g["change_pct"] = pct(spent - before, before)
        groups.append(g)
    result = {
        "group_by": group_by,
        "scope": scope,
        "total_spent": amount(total, cfg.currency),
        "groups": groups,
        "meta": meta(
            conn,
            cfg,
            start,
            end,
            row_count=len(groups),
            note="Spending excludes transfers between your accounts; refunds reduce spending.",
        ),
    }
    if prior_range is not None:
        result["comparison"] = {
            "period": prior_range,
            "total_spent": amount(prior_total, cfg.currency),
            "change": amount(total - prior_total, cfg.currency),
            "change_pct": pct(total - prior_total, prior_total),
        }
    return result


def cash_flow(conn, cfg: Config, from_=None, to=None, interval="month", account=None, scope="all", today=None) -> dict:
    today = today or date.today()
    start, end = date_range(from_, to, default_days=180, today=today)
    choice(interval, ("month", "week"), "interval")
    bucket = "substr(t.posted_date, 1, 7)" if interval == "month" else "strftime('%Y-W%W', t.posted_date)"
    f_sql, f_params = txn_filter(account_id(conn, account), scope)
    params: list[Any] = [start.isoformat(), end.isoformat(), *f_params]

    def series(filter_sql: str, sign: int) -> dict[str, int]:
        rows = conn.execute(
            f"""SELECT {bucket} AS b, SUM(t.amount) AS s FROM {TXN_FROM}
                WHERE t.posted_date BETWEEN ? AND ? AND {filter_sql} {f_sql} GROUP BY b""",
            params,
        ).fetchall()
        return {r["b"]: sign * r["s"] for r in rows}

    income, spending = series(INCOME_FILTER, 1), series(SPENDING_FILTER, -1)
    periods = []
    for b in sorted(set(income) | set(spending)):
        inc, out = income.get(b, 0), spending.get(b, 0)
        periods.append(
            {
                "period": b,
                "income": amount(inc, cfg.currency),
                "spending": amount(out, cfg.currency),
                "net": amount(inc - out, cfg.currency),
                "savings_rate_pct": pct(inc - out, inc),
            }
        )
    ti, ts = sum(income.values()), sum(spending.values())
    partial = end >= today and interval == "month"
    return {
        "interval": interval,
        "scope": scope,
        "periods": periods,
        "totals": {
            "income": amount(ti, cfg.currency),
            "spending": amount(ts, cfg.currency),
            "net": amount(ti - ts, cfg.currency),
            "savings_rate_pct": pct(ti - ts, ti),
        },
        "meta": meta(
            conn, cfg, start, end, row_count=len(periods), note="The last period is partial." if partial else None
        ),
    }


# --- history and trends ------------------------------------------------------------------


def _month_window(months, today: date) -> tuple[list[str], date]:
    if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= 60:
        raise GuardError("months must be an integer between 1 and 60")
    first = add_months(today.replace(day=1), -(months - 1))
    keys = []
    for i in range(months):
        d = add_months(first, i)
        keys.append(f"{d.year:04d}-{d.month:02d}")
    return keys, first


def merchant_history(conn, cfg: Config, merchant: str, months=12, today=None) -> dict:
    today = today or date.today()
    name = clean_text(str(merchant or ""), 120)
    if not name:
        raise GuardError("merchant is required")
    keys, first = _month_window(months, today)
    seen = conn.execute(
        f"""SELECT MIN(t.posted_date) AS first, MAX(t.posted_date) AS last, COUNT(DISTINCT t.id) AS n
            FROM {TXN_FROM} WHERE t.merchant = ? COLLATE NOCASE AND {SPENDING_FILTER}""",
        (name,),
    ).fetchone()
    if not seen["n"]:
        raise GuardError(f"no spending found for merchant {name!r}; check the spelling with query_transactions")
    rows = conn.execute(
        f"""SELECT substr(t.posted_date, 1, 7) AS ym, -SUM(t.amount) AS spent, COUNT(DISTINCT t.id) AS n
            FROM {TXN_FROM}
            WHERE t.merchant = ? COLLATE NOCASE AND t.posted_date BETWEEN ? AND ? AND {SPENDING_FILTER}
            GROUP BY ym""",
        (name, first.isoformat(), today.isoformat()),
    ).fetchall()
    by_month = {r["ym"]: (r["spent"], r["n"]) for r in rows}
    series = [
        {"month": k, "spent": amount(by_month.get(k, (0, 0))[0], cfg.currency), "count": by_month.get(k, (0, 0))[1]}
        for k in keys
    ]
    total = sum(v[0] for v in by_month.values())
    count = sum(v[1] for v in by_month.values())
    recurring = conn.execute(
        """SELECT r.*, a.name AS account FROM recurring r LEFT JOIN accounts a ON a.id = r.account_id
           WHERE r.merchant = ? COLLATE NOCASE""",
        (name,),
    ).fetchall()
    return {
        "merchant": name,
        "months": series,
        "totals": {
            "spent": amount(total, cfg.currency),
            "count": count,
            "average_ticket": amount((2 * total + count) // (2 * count), cfg.currency) if count else None,
            "monthly_average": amount((2 * total + len(keys)) // (2 * len(keys)), cfg.currency),
        },
        "first_seen": seen["first"],
        "last_seen": seen["last"],
        "recurring": [
            {
                "account": r["account"],
                "direction": r["direction"],
                "kind": r["kind"],
                "cadence": r["cadence"],
                "status": r["status"],
                "typical_amount": amount(r["typical_amount"], cfg.currency),
                "last_amount": amount(r["last_amount"], cfg.currency),
                "next_expected": r["next_expected"],
            }
            for r in recurring
        ],
        "meta": meta(
            conn,
            cfg,
            first,
            today,
            row_count=len(series),
            note="Months with no charges are shown as zero; the current month is partial. Refunds reduce a month.",
        ),
    }


def _trend_direction(ys: list[int]) -> tuple[str, int]:
    """Least-squares slope over equally spaced months, in exact integer math → (direction, slope per month)."""
    n = len(ys)
    if n < 3:
        return "insufficient_data", 0
    sx, sxx = n * (n - 1) // 2, (n - 1) * n * (2 * n - 1) // 6
    sy, sxy = sum(ys), sum(i * y for i, y in enumerate(ys))
    num, den = n * sxy - sx * sy, n * sxx - sx * sx
    slope = num // den
    # Change over the span is slope*(n-1); "flat" when that is under 5% of the mean (sy/n).
    if sy == 0 or abs(num) * (n - 1) * 100 * n < 5 * sy * den:
        return "flat", slope
    return ("up" if num > 0 else "down"), slope


def category_trend(conn, cfg: Config, category: str, months=12, scope="all", today=None) -> dict:
    today = today or date.today()
    name = clean_text(str(category or ""), 120)
    if not name:
        raise GuardError("category is required")
    keys, first = _month_window(months, today)
    row = conn.execute("SELECT name FROM categories WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row is None and name.lower() != "uncategorized":
        names = [r[0] for r in conn.execute("SELECT name FROM categories WHERE kind = 'expense' ORDER BY name")]
        raise GuardError(f"unknown category {name!r}. Expense categories: {', '.join(names)}")
    name = row["name"] if row else "Uncategorized"
    f_sql, f_params = txn_filter(None, scope)
    rows = conn.execute(
        f"""SELECT substr(t.posted_date, 1, 7) AS ym, -SUM(t.amount) AS spent
            FROM {TXN_FROM}
            WHERE COALESCE(c.name, 'Uncategorized') = ? COLLATE NOCASE AND t.posted_date BETWEEN ? AND ?
              AND {SPENDING_FILTER} {f_sql}
            GROUP BY ym""",
        [name, first.isoformat(), today.isoformat(), *f_params],
    ).fetchall()
    by_month = {r["ym"]: r["spent"] for r in rows}
    values = [by_month.get(k, 0) for k in keys]
    complete = values[:-1]  # the current month is partial and would fake a downward trend
    direction, slope = _trend_direction(complete)
    median3 = int(statistics.median(complete[-3:])) if complete else None
    budget = next((b for b in load_budgets(cfg) if b.category.lower() == name.lower()), None)
    return {
        "category": name,
        "scope": scope,
        "months": [{"month": k, "spent": amount(v, cfg.currency)} for k, v in zip(keys, values, strict=True)],
        "trend": {
            "direction": direction,
            "slope_per_month": amount(slope, cfg.currency),
            "median_last_3_complete_months": amount(median3, cfg.currency) if median3 is not None else None,
            "basis": "least-squares fit over complete months; flat when the fitted change is under 5% of the mean",
        },
        "budget": None
        if budget is None
        else {
            "monthly": amount(budget.monthly, cfg.currency),
            "months_over_budget": sum(1 for v in complete if v > budget.monthly),
            "median_vs_budget": amount(median3 - budget.monthly, cfg.currency) if median3 is not None else None,
        },
        "totals": {"spent": amount(sum(values), cfg.currency)},
        "meta": meta(
            conn,
            cfg,
            first,
            today,
            row_count=len(keys),
            note="The current month is partial and excluded from the trend and median.",
        ),
    }
