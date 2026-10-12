"""Schedule C draft: gross receipts, expenses mapped to lines, and the standard-mileage deduction."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from ..common import meta, year_bounds
from ..config import Config
from ..db import SCOPE_SQL, SPENDING_FILTER, TXN_FROM
from ..money import amount
from .lines import DISCLAIMER, LINE_ORDER, SCHEDULE_C_LINES, line_for


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


def schedule_c_draft(conn, cfg: Config, year=None, today=None) -> dict:
    year, start, end = year_bounds(year, today)
    cur = cfg.currency
    span = (start.isoformat(), end.isoformat())
    gross = conn.execute(
        f"""SELECT COALESCE(SUM(t.amount), 0) FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business'
              AND t.transfer_group IS NULL AND t.amount > 0
              AND COALESCE(c.kind, 'income') NOT IN ('transfer', 'ignore', 'expense')""",
        span,
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT c.name AS category, c.schedule_c_line AS line, -SUM(t.amount) AS spent, COUNT(DISTINCT t.id) AS n
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' AND {SPENDING_FILTER}
            GROUP BY c.id""",
        span,
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

    miles = mileage_summary(conn, cfg, start, end)
    vehicle_actual = lines.get("9", {}).get("amount", 0)
    if miles["trips"]:
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
    notes.append(
        "Home office (line 30), depreciation details, inventory/COGS (Part III), and health "
        "insurance are not computed here."
    )
    return {
        "tax_year": year,
        "gross_receipts": amount(gross, cur),
        "expenses_by_line": ordered,
        "total_expenses": amount(total_expenses, cur),
        "tentative_net_profit": amount(gross - total_expenses, cur),
        "unmapped_business_spending": unmapped,
        "mileage": {
            **miles,
            "deduction": amount(miles["deduction_minor"], cur) if miles["deduction_minor"] is not None else None,
        },
        "notes": notes,
        "meta": meta(conn, cfg, start, end, row_count=len(ordered)),
    }
