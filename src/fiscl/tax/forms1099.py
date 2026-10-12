"""1099 forms the user received: record them by hand, then reconcile against business income fiscl can see.

Nothing is pulled from anywhere. The user types in what each form says; fiscl compares it with business-scope
deposits from the merchants they name and asks questions about any gap. Payer names are user-typed and merchant
names are bank-derived, so both are returned only as labeled data fields, never in error text or question text.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from ..common import check_items, meta, money_arg, plan_result, valid_id, with_meta, year_bounds
from ..config import Config
from ..db import SCOPE_SQL, TXN_FROM
from ..guard import GuardError, choice, clean_text
from ..money import amount
from ..normalize import normalize_merchant
from .lines import DISCLAIMER

FORMS = ("nec", "k", "misc")
FORM_LABELS = {"nec": "1099-NEC", "k": "1099-K", "misc": "1099-MISC"}
ITEM_FIELDS = {"action", "id", "tax_year", "form", "payer", "amount", "match_merchants", "note"}
MAX_ITEMS = 50
MAX_PAYER = 80
MAX_MERCHANT = 120
MAX_MERCHANTS = 10
MAX_NOTE = 200
MATCH_TOLERANCE_MINOR = 100  # within $1.00 counts as matching
MAX_UNREPORTED = 25

# Federal information-return thresholds by payment year (IRS Pub 1099 (2026); see
# docs/research/2026-10-11-state-estimates-1099.md). 1099-NEC and 1099-MISC: $600 for payments before 2026 and $2,000
# for payments made in 2026 (a form is due at or above the threshold), inflation-indexed from 2027, not yet published.
NEC_MISC_THRESHOLD_MINOR = {2026: 200_000}
NEC_MISC_BEFORE_2026_MINOR = 60_000
# 1099-K from third-party settlement organizations (PayPal, Venmo, Etsy ...): over $20,000 AND over 200 transactions.
# Payment-card processors have no threshold.
K_TPSO_AMOUNT_MINOR = 2_000_000
K_TPSO_TRANSACTIONS = 200


def nec_misc_threshold(payment_year: int) -> int | None:
    """Minor units at or above which a payer must file a 1099-NEC/MISC, or None when the year isn't published."""
    if payment_year < 2026:
        return NEC_MISC_BEFORE_2026_MINOR
    return NEC_MISC_THRESHOLD_MINOR.get(payment_year)


# --- record_1099 --------------------------------------------------------------------------


def _key(tax_year: int, form: str, payer: str) -> tuple[int, str, str]:
    return (tax_year, form, payer.casefold())


def _text(value, limit: int, i: int, what: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise GuardError(f"item {i}: {what} must be text")
    out = clean_text(value, 1000)
    if len(out) > limit or (not out and not allow_empty):
        raise GuardError(f"item {i}: {what} must be {'at most' if allow_empty else '1 to'} {limit} characters")
    return out


def _merchants(value, i: int) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_MERCHANTS:
        raise GuardError(f"item {i}: match_merchants must be a list of at most {MAX_MERCHANTS} names")
    out: list[str] = []
    seen: set[str] = set()
    for v in value:
        name = _text(v, MAX_MERCHANT, i, "a match_merchants name")
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def _amount(value, cfg: Config, i: int) -> int:
    try:
        minor = money_arg(value, cfg, "amount")
    except GuardError:
        minor = None
    if minor is None or minor <= 0:
        raise GuardError(f"item {i}: amount must be a dollar amount greater than 0")
    return minor


def _tax_year(value, today: date, i: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 2000 <= value <= today.year + 1:
        raise GuardError(f"item {i}: tax_year must be a 4-digit year")
    return value


def _view(d: dict, cfg: Config) -> dict:
    return {
        "tax_year": d["tax_year"],
        "form": d["form"],
        "payer": d["payer"],
        "amount": amount(d["amount"], cfg.currency),
        "match_merchants": list(d["match_merchants"]),
        "note": d["note"],
    }


def _row_dict(row) -> dict:
    return {
        "tax_year": row["tax_year"],
        "form": row["form"],
        "payer": row["payer"],
        "amount": row["amount"],
        "match_merchants": json.loads(row["match_merchants"]),
        "note": row["note"],
    }


def record_1099(conn, cfg: Config, items=None, dry_run=True, today=None) -> dict:
    """Add, update, or remove 1099 forms the user received. Dry run by default; all-or-nothing."""
    today = today or date.today()
    check_items(items)
    if len(items) > MAX_ITEMS:
        raise GuardError(f"at most {MAX_ITEMS} items per call")

    stored = {r["id"]: _row_dict(r) for r in conn.execute("SELECT * FROM forms_1099")}
    index = {_key(d["tax_year"], d["form"], d["payer"]): fid for fid, d in stored.items()}
    touched: set[int] = set()
    plan: list[dict] = []
    ops: list[tuple[str, int | None, dict | None]] = []

    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict) or set(item) - ITEM_FIELDS:
            raise GuardError(f"item {i}: unexpected field; allowed fields are {', '.join(sorted(ITEM_FIELDS))}")
        action = choice(item.get("action"), ("add", "update", "remove"), f"item {i}: action")
        before: dict | None = None
        fid: int | None = None
        if action in ("update", "remove"):
            if item.get("id") is None:
                raise GuardError(f"item {i}: id is required to {action} a form")
            fid = valid_id(item["id"], f"item {i}: id")
            if fid in touched:
                raise GuardError(f"item {i}: the same id appears more than once in this call")
            if fid not in stored:
                raise GuardError(f"item {i}: no 1099 form with that id", code="not_found")
            touched.add(fid)
            before = stored[fid]
        if action == "remove":
            index.pop(_key(before["tax_year"], before["form"], before["payer"]), None)
            plan.append({"action": "remove", "id": fid, "before": _view(before, cfg), "after": None})
            ops.append(("remove", fid, None))
            continue

        after = dict(before) if before else {"match_merchants": [], "note": None}
        if action == "add":
            missing = [f for f in ("tax_year", "form", "payer", "amount") if item.get(f) is None]
            if missing:
                raise GuardError(f"item {i}: tax_year, form, payer and amount are required to add a form")
        elif not set(item) - {"action", "id"}:
            raise GuardError(f"item {i}: give at least one field to change")
        if "tax_year" in item:
            after["tax_year"] = _tax_year(item["tax_year"], today, i)
        if "form" in item:
            if not isinstance(item["form"], str):
                raise GuardError(f"item {i}: form must be one of: {', '.join(FORMS)}")
            after["form"] = choice(item["form"], FORMS, f"item {i}: form")
        if "payer" in item:
            after["payer"] = _text(item["payer"], MAX_PAYER, i, "payer")
        if "amount" in item:
            after["amount"] = _amount(item["amount"], cfg, i)
        if "match_merchants" in item:
            after["match_merchants"] = _merchants(item["match_merchants"], i)
        if "note" in item:
            note = _text(item["note"], MAX_NOTE, i, "note", allow_empty=True) if item["note"] is not None else ""
            after["note"] = note or None  # empty clears

        new_key = _key(after["tax_year"], after["form"], after["payer"])
        old_key = _key(before["tax_year"], before["form"], before["payer"]) if before else None
        if new_key != old_key and new_key in index:
            raise GuardError(
                f"item {i}: a form with the same year, type and payer already exists; use update", code="exists"
            )
        if old_key is not None:
            index.pop(old_key, None)
        index[new_key] = fid if fid is not None else -i
        plan.append(
            {"action": action, "id": fid, "before": _view(before, cfg) if before else None, "after": _view(after, cfg)}
        )
        ops.append((action, fid, after))

    if not dry_run:
        now = datetime.now().isoformat(timespec="seconds")
        for n, (action, fid, data) in enumerate(ops):
            if action == "remove":
                conn.execute("DELETE FROM forms_1099 WHERE id = ?", (fid,))
                continue
            values = (
                data["tax_year"],
                data["form"],
                data["payer"],
                data["amount"],
                json.dumps(data["match_merchants"]),
                data["note"],
            )
            if action == "add":
                cur = conn.execute(
                    "INSERT INTO forms_1099 (tax_year, form, payer, amount, match_merchants, note, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, now, now),
                )
                plan[n]["id"] = int(cur.lastrowid)
            else:
                conn.execute(
                    "UPDATE forms_1099 SET tax_year = ?, form = ?, payer = ?, amount = ?, match_merchants = ?, "
                    "note = ?, updated_at = ? WHERE id = ?",
                    (*values, now, fid),
                )
    result = plan_result(plan, 0, dry_run, "Call again with dry_run=false to apply.")
    result["forms_affected"] = len(plan)
    return with_meta(conn, cfg, result)


# --- reconcile_1099 -----------------------------------------------------------------------

Q_TIMING = (
    "Were any invoices paid in a different calendar year than the one the payer reported (for example December work "
    "paid in January)?"
)
Q_NAME = (
    "Do the deposits from this payer appear under a different name, such as a payment processor (if so, should that "
    "name be added to match_merchants with record_1099)?"
)
Q_SCOPE = (
    "Could deposits from this payer have landed in an account marked personal (set_scope can mark them as business)?"
)
Q_WHICH = "Which deposits are from this payer, and should the names they appear under be added to match_merchants?"
Q_LOWER = (
    "Do the matched deposits include money from other payers, refunds, or reimbursements that share a merchant name "
    "with this payer?"
)
Q_DOUBLE = (
    "This payer is listed on more than one form for the year. Could the same payment appear on both, which would "
    "double count it?"
)
Q_K_GROSS = (
    "The 1099-K gross can include fees, refunds, and sales tax that never reached your bank. Do you have the "
    "processor's annual summary?"
)
Q_K_SMALL = (
    "This 1099-K is $20,000 or less. Third-party payment networks report only above $20,000 and over 200 "
    "transactions, while payment-card processors have no minimum. Do you know which kind of processor issued it?"
)
Q_UNREPORTED = (
    "You received business income from this payer at or above the 1099 reporting threshold but entered no form. "
    "That can be normal (paid by card or a payment app, the payer isn't required to file, or part of it wasn't for "
    "services); is it in your gross receipts, and are you expecting a form?"
)


def _questions(form: str, status: str, reported: int, doubled: bool) -> list[str]:
    qs: list[str] = []
    if status == "reported_higher":
        qs += [Q_K_GROSS if form == "k" else Q_TIMING, Q_NAME, Q_SCOPE]
    elif status == "reported_lower":
        qs += [Q_LOWER, Q_TIMING]
    elif status == "no_matching_income":
        qs += [Q_WHICH, Q_SCOPE]
        if form == "k":
            qs.append(Q_K_GROSS)
    if form == "k" and reported <= K_TPSO_AMOUNT_MINOR:
        qs.append(Q_K_SMALL)
    if doubled:
        qs.append(Q_DOUBLE)
    return qs


def _match_keys(names: list[str]) -> set[str]:
    keys: set[str] = set()
    for n in names:
        keys.add(n.casefold())
        keys.add(normalize_merchant(n).casefold())
    return keys


def reconcile_1099(conn, cfg: Config, tax_year=None, today=None) -> dict:
    """Compare typed-in 1099 amounts with business-scope income by merchant, as questions for the user."""
    year, start, end = year_bounds(tax_year, today)
    cur = cfg.currency
    forms = conn.execute(
        "SELECT * FROM forms_1099 WHERE tax_year = ? ORDER BY form, payer COLLATE NOCASE", (year,)
    ).fetchall()
    income = conn.execute(
        f"""SELECT t.merchant AS merchant, SUM(t.amount) AS total, COUNT(*) AS n FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business'
              AND t.transfer_group IS NULL AND t.amount > 0
              AND COALESCE(c.kind, 'income') NOT IN ('transfer', 'ignore', 'expense')
            GROUP BY t.merchant""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    # Merge merchants that differ only by case into one bucket.
    buckets: dict[str, dict] = {}
    for r in income:
        b = buckets.setdefault((r["merchant"] or "").casefold(), {"merchant": r["merchant"] or "", "total": 0, "n": 0})
        b["total"] += r["total"]
        b["n"] += r["n"]

    payer_forms: dict[str, int] = {}
    for f in forms:
        payer_forms[f["payer"].casefold()] = payer_forms.get(f["payer"].casefold(), 0) + 1

    out_forms = []
    used_keys: set[str] = set()
    for f in forms:
        names = json.loads(f["match_merchants"]) or [f["payer"]]
        keys = _match_keys(names)
        used_keys |= keys
        hits = [buckets[k] for k in keys if k in buckets]
        matched = sum(b["total"] for b in hits)
        count = sum(b["n"] for b in hits)
        diff = f["amount"] - matched
        if count == 0:
            status = "no_matching_income"
        elif abs(diff) <= MATCH_TOLERANCE_MINOR:
            status = "matches"
        else:
            status = "reported_higher" if diff > 0 else "reported_lower"
        out_forms.append(
            {
                "id": f["id"],
                "form": f["form"],
                "payer": f["payer"],
                "reported": amount(f["amount"], cur),
                "matched_income": amount(matched, cur),
                "matched_count": count,
                "difference": amount(diff, cur),
                "status": status,
                "questions": _questions(f["form"], status, f["amount"], payer_forms[f["payer"].casefold()] > 1),
                "match_merchants": json.loads(f["match_merchants"]),
                "note": f["note"],
            }
        )

    threshold = nec_misc_threshold(year)
    floor = threshold if threshold is not None else NEC_MISC_THRESHOLD_MINOR[2026]
    unreported = sorted(
        (b for k, b in buckets.items() if k and k not in used_keys and b["total"] >= floor),
        key=lambda b: (-b["total"], b["merchant"]),
    )[:MAX_UNREPORTED]

    matched_total = sum(b["total"] for k, b in buckets.items() if k in used_keys)
    notes = [
        DISCLAIMER,
        "Matching uses the merchants you listed for each form (or the payer's name when you listed none); it never "
        "guesses. Differences are questions to settle with your records and your preparer, not conclusions.",
        "Income counted is business-scope deposits posted in the calendar year; transfers are excluded.",
    ]
    if not out_forms:
        notes.append("No 1099 forms are recorded for this year. Enter the ones you received with record_1099.")
    if threshold is None:
        notes.append(
            f"The inflation-indexed 1099-NEC/MISC threshold for {year} isn't published yet; the 2026 amount is used "
            "for the unreported-payer list."
        )
    return {
        "tax_year": year,
        "forms": out_forms,
        "unreported_payers": [
            {
                "merchant": b["merchant"],
                "income": amount(b["total"], cur),
                "count": b["n"],
                "question": Q_UNREPORTED,
            }
            for b in unreported
        ],
        "thresholds": {
            "payment_year": year,
            "nec_misc": {
                "amount": amount(floor, cur),
                "comparison": "at least",
                "published": threshold is not None,
            },
            "k_third_party_network": {
                "amount": amount(K_TPSO_AMOUNT_MINOR, cur),
                "comparison": "over",
                "transactions": K_TPSO_TRANSACTIONS,
            },
            "k_payment_card": "no threshold",
        },
        "totals": {
            "reported": amount(sum(f["amount"] for f in forms), cur),
            "matched_income": amount(matched_total, cur),
            "business_gross_receipts": amount(sum(b["total"] for b in buckets.values()), cur),
        },
        "notes": notes,
        "meta": meta(conn, cfg, start, min(end, today or date.today()), row_count=len(out_forms)),
    }
