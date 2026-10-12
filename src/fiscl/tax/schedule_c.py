"""Schedule C draft: gross receipts, expenses mapped to lines, and the standard-mileage deduction.

With one business the response is the plain single draft. With several, `schedule_c_draft` returns one draft per
business plus combined totals, and `business=` selects one. Mileage is not tracked per business: the trip log counts
on the default business only, so it is never counted twice.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from ..common import meta, year_bounds
from ..config import Config
from ..db import BUSINESS_SQL, DEFAULT_BUSINESS_ID, INCOME_FILTER, SCOPE_SQL, SPENDING_FILTER, TXN_FROM
from ..money import amount
from .businesses import business_view, businesses_for_period, get_business
from .home_office import (
    LIMIT_NOTE,
    METHOD_NOTE,
    UNASSIGNED_NOTE,
    UNMATCHED_NOTE,
    home_office_deduction,
    unmatched_allocations,
)
from .lines import DISCLAIMER, LINE_ORDER, SCHEDULE_C_LINES, line_for

SE_RULE_NOTE = (
    "Each business has its own Schedule C, but self-employment tax is figured per person on the combined net profit "
    "of all of them (a loss in one reduces the profit of another), so estimated_tax adds the businesses together."
)
MILEAGE_NOTE = "The mileage log isn't split by business; its deduction is counted once, on the default business."


def mileage_rate(cfg: Config, year: int) -> Decimal | None:
    rate = cfg.tax.mileage_cents.get(year)
    return Decimal(rate) if rate is not None else None


def mileage_summary(conn, cfg: Config, start: date, end: date) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(miles_tenths), 0) AS t, COUNT(*) AS n FROM trips WHERE trip_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    rate = mileage_rate(cfg, start.year)
    miles = Decimal(row["t"]) / 10
    deduction = int((miles * rate).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)) if rate is not None else None
    return {
        "trips": row["n"],
        "miles": f"{miles:.1f}",
        "rate_cents_per_mile": str(rate) if rate else None,
        "deduction_minor": deduction,
    }


def _no_mileage(cfg: Config, year: int) -> dict:
    rate = mileage_rate(cfg, year)
    return {
        "trips": 0,
        "miles": "0.0",
        "rate_cents_per_mile": str(rate) if rate else None,
        "deduction_minor": 0 if rate is not None else None,
    }


def _build(conn, cfg: Config, year: int, start: date, end: date, biz: sqlite3.Row, *, filtered: bool, shown: bool):
    """One business's draft. `filtered` restricts rows to the business; `shown` adds its identity to the response."""
    cur = cfg.currency
    multi = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] > 1
    span = (start.isoformat(), end.isoformat())
    biz_sql, biz_params = (f"AND {BUSINESS_SQL} = ?", (biz["id"],)) if filtered else ("", ())
    gross = conn.execute(
        f"""SELECT COALESCE(SUM(t.amount), 0) FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' {biz_sql}
              AND {INCOME_FILTER}""",
        (*span, *biz_params),
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT c.name AS category, c.schedule_c_line AS line, -SUM(t.amount) AS spent, COUNT(DISTINCT t.id) AS n
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' {biz_sql} AND {SPENDING_FILTER}
            GROUP BY c.id""",
        (*span, *biz_params),
    ).fetchall()

    lines: dict[str, dict] = {}
    unmapped = []
    for r in rows:
        line = line_for(r["category"], r["line"])
        if line is None or line not in SCHEDULE_C_LINES or line == "1":
            unmapped.append(
                {"category": r["category"] or "Uncategorized", "spent": amount(r["spent"], cur), "count": r["n"]}
            )
            continue
        entry = lines.setdefault(line, {"line": line, "name": SCHEDULE_C_LINES[line], "amount": 0, "categories": []})
        entry["amount"] += r["spent"]
        entry["categories"].append({"category": r["category"], "spent": amount(r["spent"], cur), "count": r["n"]})

    notes = [DISCLAIMER]
    if "24b" in lines:
        full = lines["24b"]["amount"]
        lines["24b"]["full_amount"] = amount(full, cur)
        lines["24b"]["amount"] = full // 2
        notes.append(
            "Line 24b shows 50% of business meals, the usual limit. Some meals are fully "
            "deductible or not deductible at all; confirm with your preparer."
        )

    miles = mileage_summary(conn, cfg, start, end) if biz["id"] == DEFAULT_BUSINESS_ID else _no_mileage(cfg, year)
    vehicle_actual = lines.get("9", {}).get("amount", 0)
    if miles["trips"]:
        if multi:
            notes.append(MILEAGE_NOTE)
        if miles["deduction_minor"] is None:
            notes.append(f"No IRS mileage rate configured for {year}; add it under [tax.mileage_cents].")
        else:
            entry = lines.setdefault("9", {"line": "9", "name": SCHEDULE_C_LINES["9"], "amount": 0, "categories": []})
            entry["standard_mileage"] = {
                "miles": miles["miles"],
                "rate_cents": miles["rate_cents_per_mile"],
                "deduction": amount(miles["deduction_minor"], cur),
            }
            entry["amount"] += miles["deduction_minor"]
            if vehicle_actual:
                notes.append(
                    "Line 9 includes both actual vehicle costs and standard mileage. Generally you "
                    "use one method per vehicle (parking and tolls can be added to standard mileage). "
                    "Decide which method applies before filing."
                )

    ordered = [lines[k] for k in LINE_ORDER if k in lines]
    for e in ordered:
        e["amount"] = amount(e["amount"], cur)
    total_expenses = sum(e["amount"]["minor"] for e in ordered)
    if unmapped:
        notes.append(
            "Some business spending isn't mapped to a Schedule C line yet (see unmapped). Recategorize "
            "it into a 'Business: …' category so it lands on the right line."
        )
    profit_before = gross - total_expenses
    home_office = home_office_deduction(cfg, year, profit_before, business_view(biz))
    line_30 = home_office["line_30"]["minor"] if home_office else 0
    if home_office:
        notes.append(METHOD_NOTE)
        if home_office["limited_to_profit"]:
            notes.append(LIMIT_NOTE)
    elif year in cfg.tax.home_office:
        notes.append(UNASSIGNED_NOTE.format(year=year))
    else:
        notes.append(
            f"No home office configured for {year}; add [tax.home_office.{year}] if you use part of your home "
            "regularly and exclusively for business."
        )
    notes.append("Depreciation details, inventory/COGS (Part III), and health insurance are not computed here.")
    net_profit = amount(profit_before - line_30, cur)
    draft = {
        "tax_year": year,
        "gross_receipts": amount(gross, cur),
        "expenses_by_line": ordered,
        "total_expenses": amount(total_expenses, cur),
        "tentative_profit_before_home_office": amount(profit_before, cur),
        "home_office": home_office,
        "net_profit": net_profit,
        "tentative_net_profit": net_profit,  # alias kept for existing callers
        "unmapped_business_spending": unmapped,
        "mileage": {
            **miles,
            "deduction": amount(miles["deduction_minor"], cur) if miles["deduction_minor"] is not None else None,
        },
        "notes": notes,
        "meta": meta(conn, cfg, start, end, row_count=len(ordered)),
    }
    if shown:
        draft = {"business": business_view(biz), **draft}
    return draft


def draft_parts(conn, cfg: Config, year: int, today=None) -> tuple[list[sqlite3.Row], list[dict]]:
    """The businesses a report covers for `year` and one draft each (unfiltered when there is only one)."""
    year, start, end = year_bounds(year, today)
    rows = businesses_for_period(conn, start, end)
    if len(rows) == 1:
        return rows, [_build(conn, cfg, year, start, end, rows[0], filtered=False, shown=False)]
    return rows, [_build(conn, cfg, year, start, end, r, filtered=True, shown=True) for r in rows]


def _allocation_notes(conn, cfg: Config, year: int) -> list[str]:
    names = [r["name"] for r in conn.execute("SELECT name FROM businesses")]
    return [UNMATCHED_NOTE.format(year=year)] if unmatched_allocations(cfg, year, names) else []


def schedule_c_draft(conn, cfg: Config, year=None, today=None, business=None) -> dict:
    year, start, end = year_bounds(year, today)
    cur = cfg.currency
    if business is not None:
        biz = get_business(conn, business)
        draft = _build(conn, cfg, year, start, end, biz, filtered=True, shown=True)
        draft["notes"] += _allocation_notes(conn, cfg, year)
        return draft
    rows, drafts = draft_parts(conn, cfg, year, today)
    if len(rows) == 1:
        drafts[0]["notes"] += _allocation_notes(conn, cfg, year)
        return drafts[0]
    for d in drafts:
        d.pop("meta")
    net = sum(d["net_profit"]["minor"] for d in drafts)
    return {
        "tax_year": year,
        "businesses": drafts,
        "combined": {
            "gross_receipts": amount(sum(d["gross_receipts"]["minor"] for d in drafts), cur),
            "total_expenses": amount(sum(d["total_expenses"]["minor"] for d in drafts), cur),
            "net_profit": amount(net, cur),
        },
        "combined_net_profit": amount(net, cur),
        "notes": [DISCLAIMER, SE_RULE_NOTE, *_allocation_notes(conn, cfg, year)],
        "meta": meta(conn, cfg, start, end, row_count=len(drafts)),
    }
