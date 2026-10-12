"""Weekly digest: one page composed from the existing services (no new SQL of its own, apart from one existence check).

`weekly_digest` is read-only. `write_digest` saves the same Markdown under `<data dir>/digests/` with the exports
writer's rules: a fixed folder, a name built only from the ISO week, `O_CREAT|O_EXCL|O_NOFOLLOW`, 0600, and bank text
escaped and scrubbed before it reaches the file.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from ..common import meta
from ..config import Config
from ..db import SCOPE_SQL, TXN_FROM
from ..guard import GuardError, choice, clean_text, parse_date, redact_text
from ..money import amount, pct
from ..tax import due_dates, estimated_tax
from ..tax.export import md_escape, private_dir, write_all
from .budgets import budget_status
from .forecast import forecast_cash
from .monitoring import find_anomalies, subscription_watchdog
from .planning import goal_progress
from .spending import cash_flow, spending_summary

SCOPES = ("personal", "business", "all")
MAX_ALERTS = 10
TAX_WINDOW_DAYS = 21
UPCOMING_DAYS = 7
BANNER = "fiscl weekly digest — generated locally from your data; not financial advice."
NOT_PRIVATE = "the digests folder isn't a private directory inside your data folder; run fiscl doctor"
SUNDAY_MESSAGE = "week_ending must be a Sunday (YYYY-MM-DD)"
MAX_SUFFIX = 9  # digest-YYYY-Www.md, then -2 ... -9

# Anomaly types as the digest names them; anything else keeps its own name.
_ALERT_TYPES = {"duplicate": "duplicate_charge", "category_spike": "spike"}


def default_week_ending(today: date) -> date:
    """The most recent Sunday on or before `today`."""
    return today - timedelta(days=(today.weekday() + 1) % 7)


def _week(week_ending, today: date) -> tuple[date, date]:
    end = parse_date(week_ending, "week_ending") if week_ending else default_week_ending(today)
    if end.weekday() != 6:
        raise GuardError(SUNDAY_MESSAGE, code="invalid")
    if end > today:
        raise GuardError("week_ending can't be in the future")
    return end - timedelta(days=6), end


def _avg(total: int, parts: int) -> int:
    return int((Decimal(total) / parts).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _spending(conn, cfg: Config, start: date, end: date, scope: str, today: date) -> tuple[dict, int]:
    cur = cfg.currency
    now = spending_summary(
        conn,
        cfg,
        from_=start.isoformat(),
        to=end.isoformat(),
        group_by="category",
        compare_to="previous_period",
        top_n=5,
        scope=scope,
        today=today,
    )
    base_start = start - timedelta(days=28)
    base = spending_summary(
        conn,
        cfg,
        from_=base_start.isoformat(),
        to=(start - timedelta(days=1)).isoformat(),
        group_by="category",
        compare_to="none",
        top_n=50,
        scope=scope,
        today=today,
    )
    total = now["total_spent"]["minor"]
    prior = now["comparison"]["total_spent"]["minor"]
    avg = _avg(base["total_spent"]["minor"], 4)
    cat_avg = {g["name"]: _avg(g["spent"]["minor"], 4) for g in base["groups"]}
    top = []
    for g in now["groups"]:
        if g["name"].startswith("Other ("):
            continue
        spent = g["spent"]["minor"]
        usual = cat_avg.get(g["name"], 0)
        top.append(
            {
                "category": g["name"],
                "spent": g["spent"],
                "change_vs_4wk_avg_pct": pct(spent - usual, usual),
            }
        )
    out = {
        "total": amount(total, cur),
        "vs_prior_week": {
            "spent": amount(prior, cur),
            "change": amount(total - prior, cur),
            "change_pct": pct(total - prior, prior),
        },
        "vs_4wk_avg": {
            "average": amount(avg, cur),
            "change": amount(total - avg, cur),
            "change_pct": pct(total - avg, avg),
        },
        "top_categories": top,
    }
    return out, now["meta"]


def _income(conn, cfg: Config, start: date, end: date, scope: str, today: date) -> dict:
    flow = cash_flow(conn, cfg, from_=start.isoformat(), to=end.isoformat(), interval="week", scope=scope, today=today)
    return {"total": flow["totals"]["income"]}


def _budgets(conn, cfg: Config, end: date, today: date, notes: list[str]) -> list[dict]:
    status = budget_status(conn, cfg, month=end.strftime("%Y-%m"), today=today)
    if not status["budgets"]:
        notes.append("No budgets are set, so there is no budget section.")
    return [
        {"category": b["category"], "status": b["status"], "spent": b["spent"], "budget": b["budget"]}
        for b in status["budgets"]
        if b["status"] in ("over", "at_risk")
    ]


def _forecast(conn, cfg: Config, scope: str, today: date, notes: list[str]) -> tuple[list[dict], dict | None]:
    try:
        fc = forecast_cash(conn, cfg, days=30, scope=scope, today=today)
    except GuardError:
        notes.append("No cash forecast: it needs a checking or savings account with a balance.")
        return [], None
    horizon = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
    upcoming = [
        {"merchant": u["merchant"], "expected": u["date"], "amount": u["amount"], "kind": u["kind"]}
        for u in fc["upcoming"]
        if u["kind"] != "income" and today.isoformat() <= u["date"] <= horizon
    ]
    return upcoming, fc["safe_to_spend"]


def _alerts(conn, cfg: Config, start: date, end: date, today: date) -> list[dict]:
    alerts: list[dict] = []
    found = find_anomalies(conn, cfg, from_=start.isoformat(), to=end.isoformat(), today=today)
    for f in found["findings"]:
        alerts.append(
            {
                "type": _ALERT_TYPES.get(f["type"], f["type"]),
                "merchant": f.get("merchant"),
                "category": f.get("category"),
                "date": f.get("date"),
                "amount": f.get("amount"),
                "question": f["question"],
                "evidence": f.get("evidence", {}),
            }
        )
    watch = subscription_watchdog(conn, cfg, today=today)
    window = (start.isoformat(), end.isoformat())
    for t in watch["trials_turned_paid"]:
        paid = t["first_paid_charge"]
        if window[0] <= paid["date"] <= window[1]:
            alerts.append(
                {
                    "type": "trial_converted",
                    "merchant": t["merchant"],
                    "date": paid["date"],
                    "amount": paid["amount"],
                    "question": t["question"],
                    "evidence": {"trial_charge": t["trial_charge"], "days_to_convert": t["days_to_convert"]},
                }
            )
    for p in watch["price_changes"]:
        if window[0] <= p["last_date"] <= window[1] and p["change"]["minor"] > 0:
            alerts.append(
                {
                    "type": "price_increase",
                    "merchant": p["merchant"],
                    "date": p["last_date"],
                    "amount": p["new_amount"],
                    "question": p["question"],
                    "evidence": {"old_amount": p["old_amount"], "annualized_change": p["annualized_change"]},
                }
            )
    return alerts[:MAX_ALERTS]


def _has_business_rows(conn, year: int) -> bool:
    row = conn.execute(
        f"""SELECT 1 FROM {TXN_FROM} WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business'
            AND t.amount IS NOT NULL LIMIT 1""",
        (f"{year}-01-01", f"{year}-12-31"),
    ).fetchone()
    return row is not None


def _tax_reminder(conn, cfg: Config, today: date) -> dict | None:
    """The next federal 1040-ES date within 21 days, when there is business data for that tax year."""
    for tax_year in (today.year - 1, today.year):
        for raw in due_dates(tax_year):
            due = date.fromisoformat(raw)
            days = (due - today).days
            if 0 <= days <= TAX_WINDOW_DAYS and _has_business_rows(conn, tax_year):
                est = estimated_tax(conn, cfg, year=tax_year, today=today)
                return {
                    "due_date": raw,
                    "days_until": days,
                    "tax_year": tax_year,
                    "suggested_per_quarter": est["suggested_per_quarter"],
                    "note": "Federal 1040-ES date. Rough planning figure; confirm with your preparer.",
                }
    return None


def weekly_digest(conn, cfg: Config, week_ending=None, scope="personal", today=None) -> dict:
    today = today or date.today()
    scope = choice(scope, SCOPES, "scope")
    start, end = _week(week_ending, today)
    notes: list[str] = []
    spending, spend_meta = _spending(conn, cfg, start, end, scope, today)
    income = _income(conn, cfg, start, end, scope, today)
    budgets = _budgets(conn, cfg, end, today, notes)
    upcoming, safe = _forecast(conn, cfg, scope, today, notes)
    alerts = _alerts(conn, cfg, start, end, today)
    goals_raw = goal_progress(conn, cfg, today=today)["goals"]
    goals = [{"name": g["name"], "pct": g["pct"], "on_track": g["on_track"]} for g in goals_raw if not g["archived"]]
    if not goals:
        notes.append("No savings goals are set.")
    reminder = _tax_reminder(conn, cfg, today)
    result = {
        "week": {"start": start.isoformat(), "end": end.isoformat()},
        "scope": scope,
        "spending": spending,
        "income": income,
        "budgets": budgets,
        "upcoming_7d": upcoming,
        "safe_to_spend": safe,
        "alerts": alerts,
        "goals": goals,
        "tax_reminder": reminder,
        "freshness": spend_meta["data_freshness"],
        "notes": notes,
    }
    result["meta"] = meta(conn, cfg, start, end, row_count=len(alerts))
    # The totals above silently exclude rows with no exchange rate; the file and the CLI print only the Markdown,
    # so the warning must travel in it too. Both strings are fixed prose from fx.py.
    notes.extend(w for w in _fx_warnings(result["meta"]))
    result["markdown"] = render_markdown(result)
    return result


def _fx_warnings(m: dict) -> list[str]:
    out = []
    if m.get("unconverted"):
        out.append(m["unconverted"]["note"])
    if m.get("fx_notice"):
        out.append(m["fx_notice"])
    return out


# --- Markdown --------------------------------------------------------------------------------------------------------


def _t(value: object, limit: int = 80) -> str:
    """Bank-derived text for the file: cleaned, scrubbed, capped (80 characters for names), escaped, never clickable."""
    return md_escape(redact_text(clean_text(None if value is None else str(value), limit)))


def _m(value: dict | None) -> str:
    return md_escape(value["display"]) if value else "n/a"


def _pct(value: str | None) -> str:
    return "n/a" if value is None else md_escape(f"{value}%")


def render_markdown(d: dict) -> str:
    wk = d["week"]
    sp = d["spending"]
    fresh = "; ".join(f"{_t(f['account'])}: {_t(f['latest_transaction'] or 'no transactions')}" for f in d["freshness"])
    lines = [
        f"# Week of {wk['start']} to {wk['end']}",
        "",
        md_escape(BANNER),
        "",
        f"Data through: {fresh or 'no accounts'}",
        "",
    ]
    warnings = _fx_warnings(d.get("meta", {}))
    if warnings:
        lines += [f"**Warning:** {md_escape(w)}" for w in warnings]
        lines.append("")
    lines += [
        "## Spending",
        "",
        f"- Total: {_m(sp['total'])}",
        f"- Prior week: {_m(sp['vs_prior_week']['spent'])} (change {_m(sp['vs_prior_week']['change'])}, "
        f"{_pct(sp['vs_prior_week']['change_pct'])})",
        f"- Usual week (4-week average): {_m(sp['vs_4wk_avg']['average'])} (change "
        f"{_m(sp['vs_4wk_avg']['change'])}, {_pct(sp['vs_4wk_avg']['change_pct'])})",
        f"- Income: {_m(d['income']['total'])}",
        "",
    ]
    if sp["top_categories"]:
        lines += ["### Top categories", ""]
        lines += [
            f"- {_t(c['category'])}: {_m(c['spent'])} ({_pct(c['change_vs_4wk_avg_pct'])} vs 4-week average)"
            for c in sp["top_categories"]
        ]
        lines.append("")
    if d["budgets"]:
        lines += ["## Budgets to watch", ""]
        lines += [
            f"- {_t(b['category'])}: {_t(b['status'].replace('_', ' '))}, {_m(b['spent'])} of {_m(b['budget'])}"
            for b in d["budgets"]
        ]
        lines.append("")
    if d["upcoming_7d"]:
        lines += ["## Coming up in the next 7 days", ""]
        lines += [f"- {u['expected']}: {_t(u['merchant'])}, {_m(u['amount'])}" for u in d["upcoming_7d"]]
        lines.append("")
    if d["safe_to_spend"]:
        lines += [f"Safe to spend before the next income: {_m(d['safe_to_spend'])}", ""]
    if d["alerts"]:
        lines += ["## Worth a look", ""]
        for a in d["alerts"]:
            label = a.get("merchant") or a.get("category")
            who = f"{_t(label)}: " if label else ""
            lines.append(f"- {who}{_t(a['question'], 300)}")
        lines.append("")
    if d["goals"]:
        lines += ["## Goals", ""]
        lines += [
            f"- {_t(g['name'])}: {_pct(g['pct'])}, {'on track' if g['on_track'] else 'behind pace'}" for g in d["goals"]
        ]
        lines.append("")
    if d["tax_reminder"]:
        t = d["tax_reminder"]
        lines += [
            "## Estimated tax",
            "",
            f"- Federal estimated payment due {t['due_date']} ({t['days_until']} days). Rough quarterly set-aside: "
            f"{_m(t['suggested_per_quarter'])}. Confirm with your preparer.",
            "",
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


# --- write_digest ----------------------------------------------------------------------------------------------------


def digest_stem(week_end: date) -> str:
    iso = week_end.isocalendar()
    return f"digest-{iso.year}-W{iso.week:02d}"


def write_digest(conn, cfg: Config, week_ending=None, scope="personal", dry_run=True, today=None) -> dict:
    """Plan (default) or write the digest for one week. Never overwrites: a second file gets -2, -3, ... -9."""
    digest = weekly_digest(conn, cfg, week_ending=week_ending, scope=scope, today=today)
    data = digest["markdown"].encode("utf-8")
    stem = digest_stem(date.fromisoformat(digest["week"]["end"]))
    directory = private_dir(cfg, cfg.digests, create=not dry_run, message=NOT_PRIVATE)
    names = [f"{stem}.md", *(f"{stem}-{n}.md" for n in range(2, MAX_SUFFIX + 1))]
    free = [n for n in names if not os.path.lexists(directory / n)]
    if not free:
        raise GuardError("digests for that week already exist (up to nine); delete old ones first", code="exists")
    name = free[0]
    if not dry_run:
        exists_msg = "a digest with that name already exists; try again"
        for name in free:
            try:
                write_all(directory, [(name, data)], exists_msg)
                break
            except GuardError as exc:
                if exc.code != "exists":
                    raise
        else:
            raise GuardError("digests for that week already exist (up to nine); delete old ones first", code="exists")
    result = {
        "dry_run": bool(dry_run),
        "path": f"digests/{name}",
        "bytes": len(data),
        "next_step": "Call again with dry_run=false to write this file." if dry_run else "Applied.",
        "notes": ["The file holds merchant names and amounts, like your database. Delete old digests you don't need."],
    }
    if not dry_run:
        result["written"] = True
    result["meta"] = meta(conn, cfg, row_count=1)
    return result
