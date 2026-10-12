"""Tax edits and the mileage log: set_scope, log_trip, mileage_log. Writes are dry-run by default."""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from ..common import (
    check_bulk,
    check_items,
    count_by,
    find_account,
    meta,
    plan_result,
    resolve_target,
    target_of,
    with_meta,
    without_split_parents,
    year_bounds,
)
from ..config import Config
from ..guard import GuardError, clean_text, parse_date
from ..money import amount
from .lines import DISCLAIMER
from .schedule_c import mileage_rate, mileage_summary


def set_scope(conn, cfg: Config, items: list[dict], dry_run=True) -> dict:
    """Mark accounts, merchants, or transactions as business or personal. Dry run by default."""
    check_items(items)
    plan, total = [], 0
    for i, item in enumerate(items, start=1):
        scope = item.get("scope")
        if "account" in item:
            if scope not in ("personal", "business", "mixed"):
                raise GuardError(f"item {i}: account scope must be personal, business, or mixed")
            acct = find_account(conn, clean_text(str(item["account"]), 120))
            if acct is None:
                raise GuardError(f"item {i}: unknown account; list_accounts shows them", code="not_found")
            plan.append({"target": {"account": acct["name"]}, "from": acct["scope"], "to": scope, "_acct": acct["id"]})
            continue
        if scope not in ("personal", "business"):
            raise GuardError(f"item {i}: transaction scope must be personal or business")
        merchant, ids, rows = resolve_target(conn, item, i)
        rows, skipped = without_split_parents(rows)
        total += len(rows)
        plan.append(
            {
                "target": target_of(merchant, ids),
                **skipped,
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
    result = plan_result(plan, total, dry_run, "Call again with dry_run=false to apply.")
    return with_meta(conn, cfg, result)


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
        "meta": meta(conn, cfg),
    }


def mileage_log(conn, cfg: Config, year=None, limit=50, today=None) -> dict:
    year, start, end = year_bounds(year, today)
    rows = conn.execute(
        "SELECT * FROM trips WHERE trip_date BETWEEN ? AND ? ORDER BY trip_date DESC LIMIT ?",
        (start.isoformat(), end.isoformat(), max(1, min(int(limit), 500))),
    ).fetchall()
    summary = mileage_summary(conn, cfg, start, end)
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
