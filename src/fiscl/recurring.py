"""Detect subscriptions, bills, and recurring income from transaction history."""

from __future__ import annotations

import calendar
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta

from .config import Config

# name, nominal days, tolerance days
CADENCES = [
    ("weekly", 7, 2),
    ("biweekly", 14, 3),
    ("monthly", 30.44, 4),
    ("quarterly", 91.3, 9),
    ("annual", 365.25, 20),
]
BILL_CATEGORIES = {"housing", "utilities", "insurance", "taxes", "fees & interest"}
MIN_CONFIDENCE = 0.6


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def next_date(last: date, cadence: str) -> date:
    if cadence == "monthly":
        return add_months(last, 1)
    if cadence == "quarterly":
        return add_months(last, 3)
    if cadence == "annual":
        return add_months(last, 12)
    return last + timedelta(days=7 if cadence == "weekly" else 14)


def _classify(intervals: list[int]) -> tuple[str, float] | None:
    med = statistics.median(intervals)
    for name, nominal, tol in CADENCES:
        if abs(med - nominal) <= tol:
            regular = sum(1 for i in intervals if abs(i - nominal) <= tol) / len(intervals)
            return name, regular
    return None


def detect(series: list[tuple[date, int]], min_occurrences: int, tolerance_pct: int, today: date) -> dict | None:
    """series: (date, abs amount) for one merchant/account/direction, any order."""
    by_day: dict[date, int] = defaultdict(int)
    for d, a in series:
        by_day[d] += a
    points = sorted(by_day.items())
    if len(points) < min_occurrences:
        return None
    intervals = [(b[0] - a[0]).days for a, b in zip(points, points[1:], strict=False)]
    classified = _classify(intervals)
    if classified is None:
        return None
    cadence, regularity = classified

    amounts = [a for _, a in points]
    history = amounts[:-1]
    typical = int(statistics.median(history))
    tol = max(typical * tolerance_pct // 100, 100)  # at least $1 of slack
    consistency = sum(1 for a in history if abs(a - typical) <= tol) / len(history)
    confidence = round(0.6 * regularity + 0.4 * consistency, 2)
    # Both gates matter: weekly grocery runs are regular but their amounts aren't.
    if confidence < MIN_CONFIDENCE or consistency < 0.5:
        return None

    last_day, last_amount = points[-1]
    nxt = next_date(last_day, cadence)
    nominal = next(n for c, n, _ in CADENCES if c == cadence)
    grace = next(t for c, _, t in CADENCES if c == cadence)
    status = "active"
    if abs(last_amount - typical) > max(typical // 100, 50) and abs(last_amount - typical) <= typical:
        status = "price_changed"
    if today > nxt + timedelta(days=grace):
        status = "missed"
    if today > nxt + timedelta(days=int(nominal * 2)):
        status = "cancelled"
    return {
        "cadence": cadence,
        "typical_amount": typical,
        "last_amount": last_amount,
        "last_date": last_day.isoformat(),
        "next_expected": nxt.isoformat(),
        "occurrences": len(points),
        "confidence": confidence,
        "status": status,
    }


def refresh_recurring(conn: sqlite3.Connection, cfg: Config, today: date | None = None) -> int:
    today = today or date.today()
    since = add_months(today, -18).isoformat()
    rows = conn.execute(
        """SELECT t.merchant, t.account_id, t.posted_date, t.amount, c.name AS category
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.transfer_group IS NULL AND t.is_pending = 0 AND t.posted_date >= ?
             AND COALESCE(c.kind, '') != 'ignore' AND t.merchant IS NOT NULL
           ORDER BY t.posted_date""",
        (since,),
    ).fetchall()
    dismissed = {
        (r["merchant"], r["account_id"], r["direction"])
        for r in conn.execute("SELECT * FROM recurring_dismissed").fetchall()
    }
    groups: dict[tuple, list] = defaultdict(list)
    last_category: dict[tuple, str | None] = {}
    for r in rows:
        key = (r["merchant"], r["account_id"], "in" if r["amount"] > 0 else "out")
        groups[key].append((date.fromisoformat(r["posted_date"]), abs(r["amount"])))
        last_category[key] = r["category"]

    found = []
    for key, series in groups.items():
        if key in dismissed:
            continue
        result = detect(series, cfg.recurring_min_occurrences, cfg.recurring_amount_tolerance_pct, today)
        if result is None:
            continue
        merchant, account_id, direction = key
        if direction == "in":
            kind = "income"
        elif (last_category.get(key) or "").lower() in BILL_CATEGORIES:
            kind = "bill"
        else:
            kind = "subscription"
        found.append((merchant, account_id, direction, kind, result))

    conn.execute("DELETE FROM recurring")
    conn.executemany(
        """INSERT INTO recurring (merchant, account_id, direction, cadence, typical_amount, last_amount,
               last_date, next_expected, occurrences, confidence, status, kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                m,
                a,
                d,
                r["cadence"],
                r["typical_amount"],
                r["last_amount"],
                r["last_date"],
                r["next_expected"],
                r["occurrences"],
                r["confidence"],
                r["status"],
                k,
            )
            for m, a, d, k, r in found
        ],
    )
    return len(found)
