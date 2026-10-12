"""Spending and cash flow: spending_summary, cash_flow."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..common import account_id, meta, txn_filter
from ..config import Config
from ..db import INCOME_FILTER, SPENDING_FILTER, TXN_FROM
from ..guard import GuardError, choice, date_range
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
