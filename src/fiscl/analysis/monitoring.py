"""Monitoring: find_anomalies, subscription_watchdog, refund_tracker.

Everything here reports things worth a second look, phrased as questions. Nothing is concluded for the user:
a flagged charge may be perfectly fine. Row-level checks (duplicates, spikes, refunds) read base transactions,
where a split parent is the real charge; category totals read the split-aware lines.
"""

from __future__ import annotations

import calendar
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta
from difflib import SequenceMatcher

from ..common import meta
from ..config import Config
from ..db import SPENDING_FILTER, TXN_BASE_FROM, TXN_FROM
from ..guard import GuardError, choice, clamp_limit, date_range
from ..money import amount, fmt
from ..recurring import add_months
from .recurring_report import PER_YEAR

REFUND_TAG = "refund-pending"
DUP_WINDOW_DAYS = 3
DUP_SAME_DAY_MIN = 2000  # $20: same-day pairs are often legitimately two items
DUP_MIN = 1000  # $10
HISTORY_DAYS = 365
NEW_MERCHANT_MIN_HISTORY_DAYS = 60
CATEGORY_SPIKE_AFTER_DAY = 7
TRIAL_MAX = 100  # $1
TRIAL_CONVERT_DAYS = 45
RENEWAL_DAYS = 30

# spike multiple (tenths), spike floor over median, new-merchant min, category multiple (tenths), category floor
SENSITIVITY = {
    "low": (40, 10000, 50000, 20, 20000),
    "normal": (25, 5000, 20000, 15, 10000),
    "high": (20, 2500, 10000, 12, 5000),
}
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_FEE_RE = re.compile(
    r"\bfees?\b|overdraft|\bnsf\b|late (fee|charge|payment)|interest charge|finance charge|foreign transaction",
    re.IGNORECASE,
)

# Static service families for the duplicate-services check. Whole-word matches on the normalized merchant.
FAMILIES: dict[str, tuple[str, ...]] = {
    "video streaming": (
        "netflix",
        "hulu",
        "disney",
        "hbo",
        "max",
        "peacock",
        "paramount",
        "prime video",
        "apple tv",
        "youtube tv",
    ),
    "music streaming": ("spotify", "apple music", "tidal", "pandora", "youtube music", "amazon music", "deezer"),
    "cloud storage": ("dropbox", "icloud", "google one", "onedrive", "box"),
    "news": ("new york times", "nytimes", "wall street journal", "wsj", "washington post", "economist", "athletic"),
    "fitness": ("peloton", "planet fitness", "strava", "classpass", "equinox", "anytime fitness"),
}


def _active_recurring_out(conn) -> set[str]:
    rows = conn.execute("SELECT merchant FROM recurring WHERE direction = 'out' AND status != 'cancelled'").fetchall()
    return {r["merchant"].lower() for r in rows}


def _finding(kind, severity, impact, *, merchant=None, category=None, when=None, amt=None, ids=(), evidence, question):
    return {
        "type": kind,
        "severity": severity,
        "merchant": merchant,
        "category": category,
        "date": when,
        "amount": amt,
        "evidence": evidence,
        "transaction_ids": list(ids),
        "question": question,
        "_impact": impact,
    }


# --- find_anomalies ----------------------------------------------------------------------


def find_anomalies(conn, cfg: Config, from_=None, to=None, sensitivity="normal", limit=None, today=None) -> dict:
    today = today or date.today()
    start, end = date_range(from_, to, default_days=30, today=today)
    choice(sensitivity, tuple(SENSITIVITY), "sensitivity")
    limit = clamp_limit(limit, cfg)
    spike_x10, spike_floor, new_min, cat_x10, cat_floor = SENSITIVITY[sensitivity]
    cur = cfg.currency
    skip = _active_recurring_out(conn)

    rows = conn.execute(
        f"""SELECT t.id, t.account_id, t.posted_date, -t.amount AS spent, t.merchant, t.raw_description,
                   c.name AS category
            FROM {TXN_BASE_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SPENDING_FILTER} AND t.amount < 0
            ORDER BY t.posted_date, t.id""",
        [(start - timedelta(days=HISTORY_DAYS)).isoformat(), end.isoformat()],
    ).fetchall()
    first_row = conn.execute("SELECT MIN(posted_date) FROM transactions").fetchone()[0]
    earliest = date.fromisoformat(first_row) if first_row else None

    findings: list[dict] = []
    by_merchant: dict[str, list] = defaultdict(list)
    by_key: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r["merchant"]:
            by_merchant[r["merchant"].lower()].append(r)
            by_key[(r["account_id"], r["merchant"].lower(), r["spent"])].append(r)

    for key, history in by_merchant.items():
        for i, r in enumerate(history):
            day = date.fromisoformat(r["posted_date"])
            if not start <= day <= end:
                continue
            prior = [p for p in history[:i] if (day - date.fromisoformat(p["posted_date"])).days <= HISTORY_DAYS]
            if key in skip:
                continue
            spent = r["spent"]
            if prior:
                if len(prior) >= 3:
                    median = int(statistics.median(p["spent"] for p in prior))
                    if spent * 10 >= spike_x10 * median and spent - median >= spike_floor and median > 0:
                        times = spent * 10 // median
                        findings.append(
                            _finding(
                                "spike",
                                "high" if spent * 10 >= 2 * spike_x10 * median else "medium",
                                spent - median,
                                merchant=r["merchant"],
                                category=r["category"],
                                when=r["posted_date"],
                                amt=amount(spent, cur),
                                ids=[r["id"]],
                                evidence={
                                    "median_prior_charge": amount(median, cur),
                                    "prior_charges": len(prior),
                                    "times_median": f"{times // 10}.{times % 10}",
                                },
                                question=(
                                    f"This charge is {fmt(spent, cur)} against a usual {fmt(median, cur)} at this "
                                    "merchant. Was it expected?"
                                ),
                            )
                        )
            elif spent >= new_min and earliest is not None and (day - earliest).days >= NEW_MERCHANT_MIN_HISTORY_DAYS:
                findings.append(
                    _finding(
                        "new_merchant",
                        "medium" if spent >= 2 * new_min else "low",
                        spent,
                        merchant=r["merchant"],
                        category=r["category"],
                        when=r["posted_date"],
                        amt=amount(spent, cur),
                        ids=[r["id"]],
                        evidence={"first_charge_in_days": HISTORY_DAYS, "threshold": amount(new_min, cur)},
                        question=(
                            f"First charge from this merchant in over a year, for {fmt(spent, cur)}. "
                            "Do you recognize it?"
                        ),
                    )
                )

    # Duplicates: same account + merchant + amount, close together.
    for (_acct, _m, spent), group in by_key.items():
        if len(group) < 2:
            continue
        for a, b in zip(group, group[1:], strict=False):
            da, db_ = date.fromisoformat(a["posted_date"]), date.fromisoformat(b["posted_date"])
            gap = (db_ - da).days
            if not start <= db_ <= end or gap > DUP_WINDOW_DAYS:
                continue
            if spent < (DUP_SAME_DAY_MIN if gap == 0 else DUP_MIN):
                continue
            findings.append(
                _finding(
                    "duplicate",
                    "high",
                    spent,
                    merchant=b["merchant"],
                    category=b["category"],
                    when=b["posted_date"],
                    amt=amount(spent, cur),
                    ids=[a["id"], b["id"]],
                    evidence={"days_apart": gap, "first_date": a["posted_date"], "second_date": b["posted_date"]},
                    question=(
                        f"Two charges of {fmt(spent, cur)} from this merchant on the same account within "
                        f"{gap or 'zero'} day(s). Was it charged twice?"
                    ),
                )
            )

    for r in rows:
        day = date.fromisoformat(r["posted_date"])
        if start <= day <= end and (
            (r["category"] or "").lower() == "fees & interest" or _FEE_RE.search(r["raw_description"] or "")
        ):
            findings.append(
                _finding(
                    "fee",
                    "medium" if r["spent"] >= 2500 else "low",
                    r["spent"],
                    merchant=r["merchant"],
                    category=r["category"],
                    when=r["posted_date"],
                    amt=amount(r["spent"], cur),
                    ids=[r["id"]],
                    evidence={"category": r["category"]},
                    question=f"A {fmt(r['spent'], cur)} fee or interest charge. Did you expect it?",
                )
            )

    findings += _category_spikes(conn, cfg, end, skip, cat_x10, cat_floor)

    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], -f["_impact"], f["date"] or ""))
    by_type: dict[str, int] = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
    total = len(findings)
    shown = findings[:limit]
    for f in shown:
        f["dollar_impact"] = amount(f.pop("_impact"), cur)
    extra = {"total_count": total, "truncated": True} if total > limit else {}
    return {
        "sensitivity": sensitivity,
        "findings": shown,
        "totals": {"findings": total, "by_type": dict(by_type)},
        "meta": meta(
            conn,
            cfg,
            start,
            end,
            row_count=len(shown),
            note="Findings are prompts to check, not conclusions. Active recurring bills are skipped.",
            **extra,
        ),
    }


def _category_spikes(conn, cfg, end: date, skip: set[str], x10: int, floor: int) -> list[dict]:
    if end.day <= CATEGORY_SPIKE_AFTER_DAY:
        return []
    month_start = end.replace(day=1)
    prior_starts = [add_months(month_start, -k) for k in (3, 2, 1)]
    skip_sql, skip_params = "", []
    if skip:
        skip_sql = f"AND LOWER(COALESCE(t.merchant, '')) NOT IN ({','.join('?' * len(skip))})"
        skip_params = sorted(skip)
    rows = conn.execute(
        f"""SELECT COALESCE(c.name, 'Uncategorized') AS cat, substr(t.posted_date, 1, 7) AS ym, -SUM(t.amount) AS spent
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SPENDING_FILTER} {skip_sql}
            GROUP BY cat, ym""",
        [prior_starts[0].isoformat(), end.isoformat(), *skip_params],
    ).fetchall()
    months = {f"{d.year:04d}-{d.month:02d}": i for i, d in enumerate(prior_starts)}
    this_month = f"{month_start.year:04d}-{month_start.month:02d}"
    mtd: dict[str, int] = {}
    prior: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        if r["ym"] == this_month:
            mtd[r["cat"]] = r["spent"]
        elif r["ym"] in months:
            prior[r["cat"]][months[r["ym"]]] = r["spent"]
    days_in_month = calendar.monthrange(end.year, end.month)[1]
    out = []
    for cat, spent in mtd.items():
        median = int(statistics.median(prior[cat])) if cat in prior else 0
        projected = spent * days_in_month // end.day
        if median <= 0 or projected * 10 < x10 * median or projected - median < floor:
            continue
        top = conn.execute(
            f"""SELECT DISTINCT t.id FROM {TXN_FROM}
                WHERE COALESCE(c.name, 'Uncategorized') = ? AND t.posted_date BETWEEN ? AND ? AND {SPENDING_FILTER}
                ORDER BY t.amount LIMIT 5""",
            [cat, month_start.isoformat(), end.isoformat()],
        ).fetchall()
        cur = cfg.currency
        out.append(
            _finding(
                "category_spike",
                "medium" if projected * 10 >= 2 * x10 * median else "low",
                projected - median,
                category=cat,
                when=end.isoformat(),
                amt=amount(spent, cur),
                ids=[r["id"] for r in top],
                evidence={
                    "month_to_date": amount(spent, cur),
                    "projected_month_end": amount(projected, cur),
                    "median_prior_3_months": amount(median, cur),
                },
                question=(
                    f"At this pace the month ends near {fmt(projected, cur)} against a usual {fmt(median, cur)}. "
                    "Is something unusual going on in this category?"
                ),
            )
        )
    return out


# --- subscription_watchdog ---------------------------------------------------------------


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", name.lower()).strip()


def _family_of(name: str) -> str | None:
    norm = f" {_norm_name(name)} "
    for family, keys in FAMILIES.items():
        if any(f" {k} " in norm for k in keys):
            return family
    return None


def subscription_watchdog(conn, cfg: Config, today=None) -> dict:
    today = today or date.today()
    cur = cfg.currency
    recurring = conn.execute("SELECT * FROM recurring WHERE direction = 'out' AND status != 'cancelled'").fetchall()
    active_names = {r["merchant"].lower() for r in recurring}

    # Trials that turned paid: first-ever charge of $1 or less, then a real charge within 45 days.
    since = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    rows = conn.execute(
        f"""SELECT t.id, t.posted_date, -t.amount AS spent, t.merchant
            FROM {TXN_BASE_FROM}
            WHERE t.merchant IS NOT NULL AND t.transfer_group IS NULL AND t.amount <= 0
              AND COALESCE(c.kind, 'expense') NOT IN ('transfer', 'ignore', 'income')
            ORDER BY t.posted_date, t.id"""
    ).fetchall()
    per_merchant: dict[str, list] = defaultdict(list)
    for r in rows:
        per_merchant[r["merchant"].lower()].append(r)
    trials = []
    for key, charges in per_merchant.items():
        first = charges[0]
        if first["spent"] > TRIAL_MAX:
            continue
        d0 = date.fromisoformat(first["posted_date"])
        paid = next(
            (
                c
                for c in charges[1:]
                if c["spent"] > TRIAL_MAX
                and 0 <= (date.fromisoformat(c["posted_date"]) - d0).days <= TRIAL_CONVERT_DAYS
            ),
            None,
        )
        if paid is None or paid["posted_date"] < since:
            continue
        trials.append(
            {
                "merchant": first["merchant"],
                "trial_charge": {"date": first["posted_date"], "amount": amount(first["spent"], cur)},
                "first_paid_charge": {"date": paid["posted_date"], "amount": amount(paid["spent"], cur)},
                "days_to_convert": (date.fromisoformat(paid["posted_date"]) - d0).days,
                "still_recurring": key in active_names,
                "transaction_ids": [first["id"], paid["id"]],
                "question": "A trial-sized first charge was followed by a full-price one. Did you mean to keep this?",
            }
        )
    trials.sort(key=lambda t: t["first_paid_charge"]["date"], reverse=True)

    # Overlapping services among active subscriptions.
    subs = [r for r in recurring if r["kind"] == "subscription" and r["status"] in ("active", "price_changed")]
    cat_of = {
        r["merchant"].lower(): r["cat"]
        for r in conn.execute(
            """SELECT t.merchant, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
               WHERE t.id IN (SELECT MAX(id) FROM transactions WHERE merchant IS NOT NULL GROUP BY LOWER(merchant))"""
        )
    }
    dupes = []
    families: dict[str, list] = defaultdict(list)
    loose = []
    for r in subs:
        fam = _family_of(r["merchant"])
        (families[fam] if fam else loose).append(r)
    seen_pairs: set[tuple[str, str]] = set()

    def cost(group) -> int:
        return sum(g["last_amount"] * PER_YEAR[g["cadence"]] for g in group)

    for fam, group in families.items():
        names = {g["merchant"].lower() for g in group}
        if len(names) < 2:
            continue
        dupes.append(_dupe(fam, "service_family", group, cost(group), cur))
    for i, a in enumerate(loose):
        for b in loose[i + 1 :]:
            ca, cb = cat_of.get(a["merchant"].lower()), cat_of.get(b["merchant"].lower())
            key = tuple(sorted((a["merchant"].lower(), b["merchant"].lower())))
            if ca and ca == cb and key not in seen_pairs:
                ratio = SequenceMatcher(None, _norm_name(a["merchant"]), _norm_name(b["merchant"])).ratio()
                if ratio >= 0.8:
                    seen_pairs.add(key)
                    dupes.append(_dupe(ca, "similar_names", [a, b], cost([a, b]), cur))

    price_changes = []
    for r in recurring:
        if r["status"] != "price_changed":
            continue
        per_year = PER_YEAR[r["cadence"]]
        delta = r["last_amount"] - r["typical_amount"]
        price_changes.append(
            {
                "merchant": r["merchant"],
                "cadence": r["cadence"],
                "old_amount": amount(r["typical_amount"], cur),
                "new_amount": amount(r["last_amount"], cur),
                "change": amount(delta, cur),
                "annualized_change": amount(delta * per_year, cur),
                "last_date": r["last_date"],
                "question": "The latest charge differs from the usual amount. Were you told about the change?",
            }
        )
    price_changes.sort(key=lambda p: -abs(p["annualized_change"]["minor"]))

    horizon = (today + timedelta(days=RENEWAL_DAYS)).isoformat()
    renewals = [
        {
            "merchant": r["merchant"],
            "due": r["next_expected"],
            "expected_amount": amount(r["last_amount"], cur),
            "question": "An annual charge is coming up. Do you still want it?",
        }
        for r in sorted(recurring, key=lambda r: r["next_expected"] or "")
        if r["cadence"] == "annual"
        and r["next_expected"]
        and today.isoformat() <= r["next_expected"] <= horizon
        and r["status"] in ("active", "price_changed")
    ]
    return {
        "trials_turned_paid": trials,
        "duplicate_services": dupes,
        "price_changes": price_changes,
        "annual_renewals": renewals,
        "totals": {
            "trials_turned_paid": len(trials),
            "duplicate_services": len(dupes),
            "price_changes": len(price_changes),
            "annual_renewals": len(renewals),
            "annualized_price_change": amount(sum(p["annualized_change"]["minor"] for p in price_changes), cur),
        },
        "meta": meta(
            conn,
            cfg,
            row_count=len(trials) + len(dupes) + len(price_changes) + len(renewals),
            note=(
                "Trials look back one year. Overlaps use a built-in list of service families (video, music, cloud "
                "storage, news, fitness) plus near-identical names within a category; they are prompts, not verdicts."
            ),
        ),
    }


def _dupe(group_name, basis, items, annual_cost, cur) -> dict:
    return {
        "group": group_name,
        "basis": basis,
        "merchants": [i["merchant"] for i in items],
        "annualized_cost": amount(annual_cost, cur),
        "question": "These subscriptions look like they serve the same purpose. Do you use all of them?",
    }


# --- refund_tracker ----------------------------------------------------------------------


def refund_tracker(conn, cfg: Config, days=90, today=None) -> dict:
    today = today or date.today()
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise GuardError("days must be an integer between 1 and 365")
    cur = cfg.currency
    start = today - timedelta(days=days - 1)
    base = f"""SELECT t.id, t.posted_date, t.amount, t.merchant, c.name AS category, c.kind AS kind
            FROM {TXN_BASE_FROM} WHERE t.transfer_group IS NULL AND t.merchant IS NOT NULL
              AND COALESCE(c.kind, 'expense') NOT IN ('transfer', 'ignore') """
    purchases_by: dict[str, list] = defaultdict(list)
    credits_by: dict[str, list] = defaultdict(list)
    for r in conn.execute(
        base + "AND t.posted_date >= ? ORDER BY t.posted_date, t.id",
        [(start - timedelta(days=90)).isoformat()],
    ):
        (credits_by if r["amount"] > 0 else purchases_by)[r["merchant"].lower()].append(r)

    received = []
    used: set[int] = set()
    window_credits = [
        (k, c) for k, cs in credits_by.items() for c in cs if start.isoformat() <= c["posted_date"] <= today.isoformat()
    ]
    window_credits.sort(key=lambda kc: (kc[1]["posted_date"], kc[1]["id"]))
    for key, credit in window_credits:
        cdate = date.fromisoformat(credit["posted_date"])
        candidates = [
            p
            for p in purchases_by.get(key, [])
            if 0 <= (cdate - date.fromisoformat(p["posted_date"])).days <= 90 and p["id"] not in used
        ]
        if credit["kind"] != "expense" and not candidates:
            continue  # an unrelated deposit, not a refund
        if credit["kind"] == "income":
            continue
        exact = [p for p in candidates if -p["amount"] == credit["amount"]]
        larger = [p for p in candidates if -p["amount"] > credit["amount"]]
        match = (exact or larger or [None])[-1]
        if match is not None:
            used.add(match["id"])
        received.append(
            {
                "merchant": credit["merchant"],
                "date": credit["posted_date"],
                "amount": amount(credit["amount"], cur),
                "category": credit["category"],
                "transaction_id": credit["id"],
                "matched_purchase": None
                if match is None
                else {
                    "transaction_id": match["id"],
                    "date": match["posted_date"],
                    "amount": amount(-match["amount"], cur),
                    "match": "exact_amount" if exact else "larger_purchase",
                },
            }
        )

    tagged = conn.execute(
        f"""SELECT t.id, t.posted_date, -t.amount AS spent, t.merchant, c.name AS category
            FROM {TXN_BASE_FROM} JOIN tags g ON g.transaction_id = t.id
            WHERE g.tag = ? AND t.amount < 0 ORDER BY t.posted_date, t.id""",
        (REFUND_TAG,),
    ).fetchall()
    merchants = sorted({r["merchant"].lower() for r in tagged if r["merchant"]})
    credits: dict[str, list] = defaultdict(list)
    if merchants:
        for r in conn.execute(
            f"""SELECT t.posted_date, t.amount, LOWER(t.merchant) AS m FROM transactions t
                WHERE t.amount > 0 AND t.transfer_group IS NULL
                  AND LOWER(t.merchant) IN ({",".join("?" * len(merchants))})""",
            merchants,
        ):
            credits[r["m"]].append(r)
    expected = []
    for p in tagged:
        got = sum(
            c["amount"] for c in credits.get((p["merchant"] or "").lower(), []) if c["posted_date"] >= p["posted_date"]
        )
        if got >= p["spent"]:
            continue
        expected.append(
            {
                "merchant": p["merchant"],
                "purchase_date": p["posted_date"],
                "amount": amount(p["spent"], cur),
                "received_so_far": amount(got, cur),
                "days_waiting": (today - date.fromisoformat(p["posted_date"])).days,
                "transaction_id": p["id"],
                "question": "Tagged refund-pending, and no credit has arrived. Is the refund still pending?",
            }
        )
    expected.sort(key=lambda e: -e["days_waiting"])
    return {
        "received_refunds": received,
        "expected_refunds": expected,
        "totals": {
            "received": amount(sum(r["amount"]["minor"] for r in received), cur),
            "received_count": len(received),
            "expected": amount(sum(e["amount"]["minor"] - e["received_so_far"]["minor"] for e in expected), cur),
            "expected_count": len(expected),
        },
        "meta": meta(
            conn,
            cfg,
            start,
            today,
            row_count=len(received) + len(expected),
            note=f"Tag a purchase '{REFUND_TAG}' (tag_transactions) to track a refund you are waiting for.",
        ),
    }
