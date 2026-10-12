"""Business tax helpers for sole proprietors: Schedule C draft, mileage, estimated tax, write-off review.

Everything here organizes the user's own data for them and their tax preparer. Nothing here decides
what is deductible. Every response carries that framing.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from .common import check_bulk, check_items, count_by, meta, plan_result, resolve_target, target_of, year_bounds
from .config import Config
from .db import SCOPE_SQL, SPENDING_FILTER, TXN_FROM
from .guard import GuardError, clean_text, parse_date
from .money import amount, pct

DISCLAIMER = (
    "This organizes your own records for you and your tax preparer. It is not tax advice, and "
    "whether something is deductible depends on facts only you and a professional can confirm."
)

# Official Schedule C (Form 1040) Part II line names (2025 form; recheck when the 2026 form is released).
SCHEDULE_C_LINES = {
    "1": "Gross receipts or sales",
    "8": "Advertising",
    "9": "Car and truck expenses",
    "10": "Commissions and fees",
    "11": "Contract labor",
    "12": "Depletion",
    "13": "Depreciation and section 179 expense deduction",
    "14": "Employee benefit programs",
    "15": "Insurance (other than health)",
    "16a": "Interest: Mortgage (paid to banks, etc.)",
    "16b": "Interest: Other",
    "17": "Legal and professional services",
    "18": "Office expense",
    "19": "Pension and profit-sharing plans",
    "20a": "Rent or lease: Vehicles, machinery, and equipment",
    "20b": "Rent or lease: Other business property",
    "21": "Repairs and maintenance",
    "22": "Supplies",
    "23": "Taxes and licenses",
    "24a": "Travel",
    "24b": "Deductible meals",
    "25": "Utilities",
    "26": "Wages (less employment credits)",
    "27b": "Other expenses (from line 48)",
}
LINE_ORDER = list(SCHEDULE_C_LINES)

# When a business-scope transaction sits in an everyday category, this is the line it most likely
# belongs on. Categories with an explicit schedule_c_line always win.
CATEGORY_DEFAULT_LINES = {
    "utilities": "25",
    "insurance": "15",
    "travel": "24a",
    "dining": "24b",
    "auto: fuel": "9",
    "auto: maintenance": "9",
    "transport": "24a",
    "subscriptions": "27b",
    "education": "27b",
    "fees & interest": "27b",
    "taxes": "23",
    "shopping": "22",
}

# Merchants that are often business expenses for freelancers and creative/technical work.
# Used only to raise questions in review_writeoffs, never to classify.
_BUSINESSY = re.compile(
    r"adobe|google (workspace|cloud|ads)|gsuite|github|gitlab|aws|amazon web services|digitalocean|"
    r"vercel|netlify|heroku|cloudflare|figma|notion|slack|zoom|dropbox|canva|squarespace|wix|godaddy|"
    r"namecheap|openai|anthropic|jetbrains|microsoft 365|office 365|quickbooks|freshbooks|wave|"
    r"mailchimp|hubspot|calendly|docusign|linkedin|upwork|fiverr|b&h|bh photo|adorama|dji|"
    r"staples|office depot|officemax|fedex|ups store|usps|wework|regus|coworking|apple\.com|best buy|"
    r"semrush|ahrefs|sanity|github sponsors",
    re.IGNORECASE,
)
DE_MINIMIS_MINOR = 250_000  # $2,500 de minimis safe harbor per item/invoice (no applicable financial statement)


def _line_for(category: str | None, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return CATEGORY_DEFAULT_LINES.get((category or "").lower())


def mileage_rate(cfg: Config, year: int) -> Decimal | None:
    rate = cfg.tax.mileage_cents.get(year)
    return Decimal(rate) if rate is not None else None


def _mileage(conn, cfg: Config, start: date, end: date) -> dict:
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


# --- Schedule C draft ----------------------------------------------------------------------


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
        f"""SELECT c.name AS category, c.schedule_c_line AS line, -SUM(t.amount) AS spent, COUNT(*) AS n
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' AND {SPENDING_FILTER}
            GROUP BY c.id""",
        span,
    ).fetchall()

    lines: dict[str, dict] = {}
    unmapped = []
    for r in rows:
        line = _line_for(r["category"], r["line"])
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

    miles = _mileage(conn, cfg, start, end)
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


# --- estimated tax ----------------------------------------------------------------------------


def due_dates(year: int) -> list[str]:
    known = {2026: ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"]}
    return known.get(year, [f"{year}-04-15", f"{year}-06-15", f"{year}-09-15", f"{year + 1}-01-15"])


def estimated_tax(conn, cfg: Config, year=None, income_tax_rate_pct=None, w2_wages=0, today=None) -> dict:
    """Rough quarterly estimate from year-to-date business profit, annualized."""
    today = today or date.today()
    year, start, end = year_bounds(year, today)
    cur = cfg.currency
    draft = schedule_c_draft(conn, cfg, year, today)
    ytd_profit = draft["tentative_net_profit"]["minor"]
    if year == today.year:
        elapsed = (today - start).days + 1
        days_in_year = (end - start).days + 1
        projected = ytd_profit * days_in_year // elapsed if elapsed else ytd_profit
    else:
        projected = ytd_profit

    rate_raw = income_tax_rate_pct if income_tax_rate_pct is not None else cfg.tax.income_tax_rate_pct
    try:
        w2 = int(Decimal(str(w2_wages or 0)) * 100)
        rate = Decimal(str(rate_raw)) if rate_raw is not None else None
    except Exception as exc:
        raise GuardError("income_tax_rate_pct and w2_wages must be numbers") from exc
    if rate is not None and not 0 <= rate <= 60:
        raise GuardError("income_tax_rate_pct must be between 0 and 60")

    # Self-employment tax (Schedule SE): 92.35% of net profit; 12.4% Social Security up to the wage base
    # (reduced by W-2 wages), 2.9% Medicare on all of it. Nothing owed below $400 of net earnings.
    net_earnings = int(Decimal(max(projected, 0)) * Decimal("0.9235"))
    base = cfg.tax.ss_wage_base.get(year)
    notes = [DISCLAIMER]
    if base is None:
        notes.append(f"No Social Security wage base configured for {year}; Social Security part assumes no cap.")
        ss_taxable = net_earnings
    else:
        ss_taxable = max(0, min(net_earnings, base * 100 - w2))
    se_tax = 0
    if net_earnings >= 40_000:
        se_tax = int(
            (Decimal(ss_taxable) * Decimal("0.124") + Decimal(net_earnings) * Decimal("0.029")).quantize(
                Decimal("1"), rounding=ROUND_HALF_EVEN
            )
        )
    income_tax = None
    if rate is not None:
        taxable = max(projected - se_tax // 2, 0)  # half of SE tax is deductible
        income_tax = int((Decimal(taxable) * rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    else:
        notes.append(
            "Income tax isn't included. Pass income_tax_rate_pct (your own planning rate) or set "
            "tax.income_tax_rate_pct in config.toml."
        )
    total = se_tax + (income_tax or 0)
    per_quarter = total // 4
    notes += [
        "Annualizes year-to-date business profit; uneven income makes this rough.",
        "Ignores Additional Medicare Tax, the QBI deduction, credits, other income, and withholding.",
        "Safe-harbor rules based on last year's total tax (100%, or 110% at higher incomes) can lower what "
        "you must pay quarterly; ask your preparer.",
    ]
    return {
        "tax_year": year,
        "ytd_net_profit": amount(ytd_profit, cur),
        "projected_annual_net_profit": amount(projected, cur),
        "self_employment_tax": amount(se_tax, cur),
        "income_tax_estimate": amount(income_tax, cur) if income_tax is not None else None,
        "income_tax_rate_pct": str(rate) if rate is not None else None,
        "projected_total": amount(total, cur),
        "suggested_per_quarter": amount(per_quarter, cur),
        "due_dates": [{"date": d, "passed": d < today.isoformat()} for d in due_dates(year)],
        "set_aside_pct_of_profit": pct(total, projected) if projected > 0 else None,
        "notes": notes,
        "meta": meta(conn, cfg, start, min(end, today)),
    }


# --- write-off review --------------------------------------------------------------------------


def review_writeoffs(conn, cfg: Config, year=None, limit=25, today=None) -> dict:
    """Questions to confirm, not conclusions: things that might be business expenses or need attention."""
    year, start, end = year_bounds(year, today)
    cur = cfg.currency
    span = (start.isoformat(), end.isoformat())
    limit = max(1, min(int(limit), 100))
    sections: dict[str, list] = {}

    personal = conn.execute(
        f"""SELECT t.merchant, COUNT(*) AS n, -SUM(t.amount) AS spent, MAX(t.posted_date) AS last,
                   GROUP_CONCAT(DISTINCT c.name) AS cats
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'personal' AND {SPENDING_FILTER}
            GROUP BY t.merchant ORDER BY spent DESC""",
        span,
    ).fetchall()
    sections["possibly_business_in_personal"] = [
        {
            "merchant": r["merchant"],
            "count": r["n"],
            "spent": amount(r["spent"], cur),
            "last_date": r["last"],
            "categories": (r["cats"] or "Uncategorized").split(","),
            "question": "Was any of this for your business? If so, mark it business with set_scope.",
        }
        for r in personal
        if _BUSINESSY.search(r["merchant"] or "")
    ][:limit]

    biz = conn.execute(
        f"""SELECT t.id, t.posted_date, t.merchant, t.amount, c.name AS category, c.schedule_c_line AS line
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' AND {SPENDING_FILTER}
            ORDER BY t.amount""",
        span,
    ).fetchall()
    sections["business_needs_category"] = [
        {
            "id": r["id"],
            "date": r["posted_date"],
            "merchant": r["merchant"],
            "amount": amount(r["amount"], cur),
            "category": r["category"] or "Uncategorized",
            "question": "Which Schedule C category does this belong in?",
        }
        for r in biz
        if _line_for(r["category"], r["line"]) in (None, "1")
    ][:limit]
    sections["large_business_purchases"] = [
        {
            "id": r["id"],
            "date": r["posted_date"],
            "merchant": r["merchant"],
            "amount": amount(r["amount"], cur),
            "question": "Over the $2,500 de minimis safe harbor. Is this equipment that should be depreciated or "
            "expensed under Section 179? Ask your preparer.",
        }
        for r in biz
        if -r["amount"] > DE_MINIMIS_MINOR
    ][:limit]
    meals = [r for r in biz if _line_for(r["category"], r["line"]) == "24b"]
    if meals:
        sections["business_meals"] = [
            {
                "count": len(meals),
                "total": amount(-sum(r["amount"] for r in meals), cur),
                "question": "Business meals are usually 50% deductible and need a record of who attended and the "
                "business purpose. Do you have those notes?",
            }
        ]

    mixed = conn.execute(
        """SELECT a.name, COUNT(*) AS n FROM txn_lines t JOIN accounts a ON a.id = t.account_id
           WHERE a.scope = 'mixed' AND t.scope IS NULL AND t.transfer_group IS NULL
             AND t.posted_date BETWEEN ? AND ? GROUP BY a.id""",
        span,
    ).fetchall()
    sections["mixed_account_unassigned"] = [
        {
            "account": r["name"],
            "transactions": r["n"],
            "question": "This account mixes business and personal. Mark the business ones with set_scope.",
        }
        for r in mixed
    ]

    income = conn.execute(
        f"""SELECT t.merchant, COUNT(*) AS n, SUM(t.amount) AS total, GROUP_CONCAT(DISTINCT c.name) AS cats
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'personal' AND t.transfer_group IS NULL
              AND t.amount > 0 AND COALESCE(c.name, '') NOT IN ('Income: Salary', 'Income: Interest')
              AND COALESCE(c.kind, 'income') = 'income'
            GROUP BY t.merchant ORDER BY total DESC""",
        span,
    ).fetchall()
    sections["income_to_confirm"] = [
        {
            "merchant": r["merchant"],
            "count": r["n"],
            "total": amount(r["total"], cur),
            "categories": (r["cats"] or "Uncategorized").split(","),
            "question": "Is this business income (client payments, sales) or personal "
            "(refunds, gifts, reimbursements)?",
        }
        for r in income
    ][:limit]

    vehicle = [r for r in biz if _line_for(r["category"], r["line"]) == "9"]
    trips = conn.execute("SELECT COUNT(*) FROM trips WHERE trip_date BETWEEN ? AND ?", span).fetchone()[0]
    if vehicle and trips:
        sections["vehicle_method"] = [
            {
                "question": "You have both business vehicle costs and a mileage log. "
                "Generally one method applies per vehicle. Which will you use?"
            }
        ]
    elif vehicle and not trips:
        sections["vehicle_method"] = [
            {
                "question": "You have business vehicle costs but no mileage log. A contemporaneous log is "
                "needed either way; log trips with log_trip."
            }
        ]

    open_questions = sum(len(v) for v in sections.values())
    return {
        "tax_year": year,
        "open_questions": open_questions,
        "sections": {k: v for k, v in sections.items() if v},
        "notes": [DISCLAIMER],
        "meta": meta(conn, cfg, start, end, row_count=open_questions),
    }


# --- edits: scope and mileage ------------------------------------------------------------------


def set_scope(conn, cfg: Config, items: list[dict], dry_run=True) -> dict:
    """Mark accounts, merchants, or transactions as business or personal. Dry run by default."""
    check_items(items)
    plan, total = [], 0
    for i, item in enumerate(items, start=1):
        scope = item.get("scope")
        if "account" in item:
            if scope not in ("personal", "business", "mixed"):
                raise GuardError(f"item {i}: account scope must be personal, business, or mixed")
            acct = conn.execute(
                "SELECT id, name, scope FROM accounts WHERE name = ? COLLATE NOCASE",
                (clean_text(str(item["account"]), 120),),
            ).fetchone()
            if acct is None:
                raise GuardError(f"item {i}: unknown account {item['account']!r}")
            plan.append({"target": {"account": acct["name"]}, "from": acct["scope"], "to": scope, "_acct": acct["id"]})
            continue
        if scope not in ("personal", "business"):
            raise GuardError(f"item {i}: transaction scope must be personal or business")
        merchant, ids, rows = resolve_target(conn, item, i)
        total += len(rows)
        plan.append(
            {
                "target": target_of(merchant, ids),
                "to": scope,
                "matched_transactions": len(rows),
                "current": count_by(rows, lambda r: r["scope"] or "account default"),
                "_ids": [r["id"] for r in rows],
            }
        )
    check_bulk(total)
    if not dry_run:
        for p in plan:
            if "_acct" in p:
                conn.execute("UPDATE accounts SET scope = ? WHERE id = ?", (p["to"], p["_acct"]))
            else:
                conn.executemany("UPDATE transactions SET scope = ? WHERE id = ?", [(p["to"], t) for t in p["_ids"]])
    return plan_result(plan, total, dry_run, "Call again with dry_run=false to apply.")


def log_trip(conn, cfg: Config, trip_date: str, miles, purpose: str, vehicle=None, dry_run=True) -> dict:
    d = parse_date(trip_date, "trip_date")
    if d is None or d > date.today():
        raise GuardError("trip_date is required and can't be in the future")
    try:
        tenths = int((Decimal(str(miles)) * 10).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    except Exception as exc:
        raise GuardError("miles must be a number") from exc
    if not 0 < tenths <= 20_000:
        raise GuardError("miles must be between 0.1 and 2000")
    purpose = clean_text(purpose, 200)
    if len(purpose) < 3:
        raise GuardError("purpose is required (who/where/why); the IRS expects a business purpose per trip")
    rate = mileage_rate(cfg, d.year)
    est = (Decimal(tenths) / 10 * rate).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) if rate else None
    trip = {
        "date": d.isoformat(),
        "miles": f"{Decimal(tenths) / 10:.1f}",
        "purpose": purpose,
        "vehicle": clean_text(vehicle, 60) if vehicle else None,
        "estimated_deduction": amount(int(est), cfg.currency) if est is not None else None,
    }
    if not dry_run:
        conn.execute(
            "INSERT INTO trips (trip_date, miles_tenths, purpose, vehicle, created_at) VALUES (?, ?, ?, ?, ?)",
            (trip["date"], tenths, purpose, trip["vehicle"], datetime.now().isoformat(timespec="seconds")),
        )
    return {
        "dry_run": bool(dry_run),
        "trip": trip,
        "next_step": "Call again with dry_run=false to save it." if dry_run else "Saved.",
    }


def mileage_log(conn, cfg: Config, year=None, limit=50, today=None) -> dict:
    year, start, end = year_bounds(year, today)
    rows = conn.execute(
        "SELECT * FROM trips WHERE trip_date BETWEEN ? AND ? ORDER BY trip_date DESC LIMIT ?",
        (start.isoformat(), end.isoformat(), max(1, min(int(limit), 500))),
    ).fetchall()
    summary = _mileage(conn, cfg, start, end)
    return {
        "tax_year": year,
        "trips": [
            {
                "date": r["trip_date"],
                "miles": f"{Decimal(r['miles_tenths']) / 10:.1f}",
                "purpose": r["purpose"],
                "vehicle": r["vehicle"],
            }
            for r in rows
        ],
        "total_miles": summary["miles"],
        "rate_cents_per_mile": summary["rate_cents_per_mile"],
        "standard_mileage_deduction": amount(summary["deduction_minor"], cfg.currency)
        if summary["deduction_minor"] is not None
        else None,
        "notes": [DISCLAIMER],
        "meta": meta(conn, cfg, start, end, row_count=len(rows), total_count=summary["trips"]),
    }
