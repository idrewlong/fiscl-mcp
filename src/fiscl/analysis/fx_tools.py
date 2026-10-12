"""Exchange-rate tools: set_fx_rate (the user's own rates, with a dry run) and list_fx_rates (rates plus gaps).

Rates are typed in or imported from a file; nothing here looks one up. See `fiscl.fx` for the conversion rules.
"""

from __future__ import annotations

from datetime import date, timedelta

from .. import fx
from ..common import meta, plan_result, with_meta
from ..config import Config
from ..guard import GuardError, clamp_limit, parse_date
from ..money import CURRENCY_CODE_MSG, is_currency_code

MAX_RATE_ITEMS = 500
MAX_GAP_RANGES = 50


def _items(conn, cfg: Config, raw) -> list[fx.RateItem]:
    if not isinstance(raw, list) or not raw:
        raise GuardError("items must be a non-empty list")
    if len(raw) > MAX_RATE_ITEMS:
        raise GuardError(f"at most {MAX_RATE_ITEMS} items per call")
    out: list[fx.RateItem] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise GuardError(f"item {i}: must be an object with date, currency and rate")
        if "rate" not in item:
            raise GuardError(f"item {i}: rate is required (use null to remove a rate)")
        try:
            day = parse_date(item.get("date"), "date")
            if day is None:
                raise GuardError("date is required")
            currency = fx.check_foreign_currency(item.get("currency"), cfg)
            rate = None if item["rate"] is None else fx.parse_rate(item["rate"])
        except GuardError as exc:
            raise GuardError(f"item {i}: {exc}", code=exc.code) from exc
        key = (currency, day.isoformat())
        if key in seen:
            raise GuardError(f"item {i}: the same currency and date appears twice")
        seen.add(key)
        out.append(fx.RateItem(day.isoformat(), currency, rate))
    return out


def set_fx_rate(conn, cfg: Config, items=None, dry_run=True, today=None) -> dict:
    """Add, replace or remove exchange rates (home units per 1 unit of the currency) and recompute the transactions
    they reach. Defaults to a dry run that reports exactly what applying would change."""
    parsed = _items(conn, cfg, items)
    before = fx.current_rates(conn, parsed)
    for i, (it, old) in enumerate(zip(parsed, before, strict=True), start=1):
        if it.rate is None and old is None:
            raise GuardError(f"item {i}: there is no rate on that date to remove", code="not_found")
    unchanged = [it.rate is not None and fx.rate_text(it.rate) == old for it, old in zip(parsed, before, strict=True)]
    live = [it for it, same in zip(parsed, unchanged, strict=True) if not same]
    changed = fx.change_rates(conn, cfg, live, "manual", apply=not dry_run) if live else []
    plan = []
    for it, old, same in zip(parsed, before, unchanged, strict=True):
        action = "unchanged" if same else "remove" if it.rate is None else "replace" if old is not None else "add"
        plan.append(
            {
                "date": it.day,
                "currency": it.currency,
                "action": action,
                "before": old,
                "after": None if it.rate is None else fx.rate_text(it.rate),
                "affected_transactions": 0
                if same
                else sum(1 for row in changed if fx.reaches(it, row, cfg.fx_max_rate_age_days)),
            }
        )
    result = plan_result(plan, len(changed), dry_run, "Call again with dry_run=false to apply.")
    return with_meta(conn, cfg, result)


def _gaps(conn, cfg: Config, currency: str | None, start: date | None, end: date | None) -> list[dict]:
    where, params = ["amount_home IS NULL"], []
    if currency:
        where.append("currency = ?")
        params.append(currency)
    if start:
        where.append("posted_date >= ?")
        params.append(start.isoformat())
    if end:
        where.append("posted_date <= ?")
        params.append(end.isoformat())
    by_currency: dict[str, list[str]] = {}
    for r in conn.execute(
        f"SELECT currency, posted_date FROM transactions WHERE {' AND '.join(where)} ORDER BY currency, posted_date",
        params,
    ):
        by_currency.setdefault(r["currency"], []).append(r["posted_date"])
    gap = timedelta(days=cfg.fx_max_rate_age_days)
    out = []
    for cur, days in by_currency.items():
        ranges: list[dict] = []
        for day in days:
            if ranges and date.fromisoformat(day) - date.fromisoformat(ranges[-1]["to"]) <= gap:
                ranges[-1]["to"] = day
                ranges[-1]["transactions"] += 1
            else:
                ranges.append({"from": day, "to": day, "transactions": 1})
        entry = {"currency": cur, "transactions": len(days), "ranges": ranges[:MAX_GAP_RANGES]}
        if len(ranges) > MAX_GAP_RANGES:
            entry["ranges_truncated"] = True
        out.append(entry)
    return out


def list_fx_rates(conn, cfg: Config, currency=None, start_date=None, end_date=None, limit=None, today=None) -> dict:
    """The user's stored exchange rates (newest first) plus, per currency in use, the date ranges of transactions
    that have no usable rate."""
    code = None
    if currency:
        code = str(currency).strip().upper()
        if not is_currency_code(code):
            raise GuardError(f"currency {CURRENCY_CODE_MSG}")
    start, end = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if start and end and start > end:
        raise GuardError("start_date must be on or before end_date")
    cap = clamp_limit(limit, cfg)
    where, params = ["1 = 1"], []
    if code:
        where.append("currency = ?")
        params.append(code)
    if start:
        where.append("rate_date >= ?")
        params.append(start.isoformat())
    if end:
        where.append("rate_date <= ?")
        params.append(end.isoformat())
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM fx_rates WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT rate_date, currency, rate, source FROM fx_rates WHERE {clause} "
        "ORDER BY rate_date DESC, currency LIMIT ?",
        [*params, cap],
    ).fetchall()
    rates = [
        {"date": r["rate_date"], "currency": r["currency"], "rate": r["rate"], "source": r["source"]} for r in rows
    ]
    return {
        "rates": rates,
        "gaps": _gaps(conn, cfg, code, start, end),
        "notes": [
            "A rate is home-currency units per 1 unit of the currency. A transaction uses the latest rate dated on or "
            f"before it, if that rate is at most {cfg.fx_max_rate_age_days} days older. fiscl never looks up rates; "
            "add them with set_fx_rate or `fiscl fx import`."
        ],
        "meta": meta(conn, cfg, row_count=len(rates), total_count=total, truncated=total > len(rates)),
    }
