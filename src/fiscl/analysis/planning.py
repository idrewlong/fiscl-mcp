"""Planning: manual assets, net worth, "can I afford it?", debt payoff, and savings goals.

Reads are deterministic integer math over local data. Writers default to a dry run and return `plan_result`.
Nothing here moves money; the output is arithmetic on what the user's own files say, always with its assumptions.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from ..common import account_id, check_items, find_account, meta, money_arg, plan_result, with_meta
from ..config import Config
from ..guard import GuardError, choice, clean_text, parse_date
from ..money import amount, pct
from ..recurring import add_months
from .forecast import forecast_cash, rolled_balance

ASSET_KINDS = ("property", "vehicle", "equipment", "investment", "cash", "liability", "other")
DEBT_TYPES = ("credit", "loan")
STALE_DAYS = 30
CHANGE_WINDOWS = (30, 90, 365)
MAX_DEBTS = 20
MAX_PAYOFF_MONTHS = 600
SCHEDULE_MONTHS = 6
DAYS_PER_MONTH = Decimal("30.4375")
MAX_NAME = 80
MAX_NOTE = 200

_money = amount  # afford() has an `amount` argument that shadows the formatter
_date = date


def _name(value, field="name") -> str:
    name = clean_text(str(value or ""), 500)
    if not name:
        raise GuardError(f"{field} is required")
    if len(name) > MAX_NAME:
        raise GuardError(f"{field} is longer than {MAX_NAME} characters")
    return name


def _note(value) -> str | None:
    text = clean_text(str(value), 1000)
    if len(text) > MAX_NOTE:
        raise GuardError(f"note is longer than {MAX_NOTE} characters")
    return text or None  # blank clears


def _past_date(value, field: str, today: date) -> date:
    d = parse_date(value, field)
    if d is None:
        return today
    if d > today:
        raise GuardError(f"{field} can't be in the future")
    return d


# --- manual assets -------------------------------------------------------------------------


def _asset_view(row, cfg: Config) -> dict:
    return {
        "name": row["name"],
        "kind": row["kind"],
        "value": amount(row["value"], cfg.currency),
        "as_of": row["as_of"],
        "note": row["note"],
    }


def manage_asset(
    conn,
    cfg: Config,
    action=None,
    name=None,
    kind=None,
    value=None,
    as_of=None,
    note=None,
    dry_run=True,
    today=None,
) -> dict:
    """Add, update, or remove a manual asset or liability (house, vehicle, a loan held elsewhere)."""
    today = today or date.today()
    action = choice(action, ("add", "update", "remove"), "action")
    name = _name(name)
    row = conn.execute("SELECT * FROM manual_assets WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    value_minor = money_arg(value, cfg, "value")
    if value_minor is not None and value_minor < 0:
        raise GuardError(
            "value can't be negative; liabilities are entered as the positive amount owed (kind=liability)"
        )
    if kind is not None:
        choice(kind, ASSET_KINDS, "kind")

    if action == "add":
        if row is not None:
            raise GuardError("an asset with that name already exists; use action=update", code="exists")
        if kind is None or value_minor is None:
            raise GuardError("kind and value are required to add an asset")
        after = {
            "name": name,
            "kind": kind,
            "value": value_minor,
            "as_of": _past_date(as_of, "as_of", today).isoformat(),
            "note": _note(note) if note is not None else None,
        }
        before = None
    else:
        if row is None:
            raise GuardError("no manual asset with that name; list them with net_worth", code="not_found")
        before = dict(row)
        after = None
        if action == "update":
            if kind is None and value_minor is None and as_of is None and note is None:
                raise GuardError("give at least one of kind, value, as_of, note to change")
            after = dict(row)
            if kind is not None:
                after["kind"] = kind
            if value_minor is not None:
                after["value"] = value_minor
                after["as_of"] = _past_date(as_of, "as_of", today).isoformat()  # a new value is as of today
            elif as_of is not None:
                after["as_of"] = _past_date(as_of, "as_of", today).isoformat()
            if note is not None:
                after["note"] = _note(note)

    plan = [
        {
            "action": action,
            "name": (before or after)["name"],
            "before": _asset_view(before, cfg) if before else None,
            "after": _asset_view(after, cfg) if after else None,
        }
    ]
    if not dry_run:
        if action == "add":
            conn.execute(
                "INSERT INTO manual_assets (name, kind, value, as_of, note) VALUES (?, ?, ?, ?, ?)",
                (after["name"], after["kind"], after["value"], after["as_of"], after["note"]),
            )
        elif action == "update":
            conn.execute(
                "UPDATE manual_assets SET kind = ?, value = ?, as_of = ?, note = ? WHERE id = ?",
                (after["kind"], after["value"], after["as_of"], after["note"], row["id"]),
            )
        else:
            conn.execute("DELETE FROM manual_assets WHERE id = ?", (row["id"],))
    return with_meta(conn, cfg, plan_result(plan, 0, dry_run, "Call again with dry_run=false to apply."))


# --- net worth -----------------------------------------------------------------------------


def _first_data_day(conn, acct: int) -> str | None:
    return conn.execute(
        """SELECT MIN(d) FROM (SELECT MIN(posted_date) AS d FROM transactions WHERE account_id = ?
                               UNION ALL SELECT MIN(as_of) FROM balances WHERE account_id = ?)""",
        (acct, acct),
    ).fetchone()[0]


def _last_data_day(conn, acct: int, snapshot: str, on: date) -> str:
    last_txn = conn.execute(
        "SELECT MAX(posted_date) FROM transactions WHERE account_id = ? AND posted_date <= ?", (acct, on.isoformat())
    ).fetchone()[0]
    return max(snapshot, last_txn or snapshot)


def net_worth(conn, cfg: Config, as_of=None, today=None) -> dict:
    """Assets minus liabilities: account balances rolled to the date, plus manual assets and liabilities."""
    today = today or date.today()
    on = _past_date(as_of, "as_of", today)
    accounts = conn.execute("SELECT id, name, type, scope FROM accounts WHERE archived = 0 ORDER BY name").fetchall()
    assets: list[dict] = []
    liabilities: list[dict] = []
    stale, missing = [], []
    balances: dict[int, int] = {}
    for a in accounts:
        rolled = rolled_balance(conn, a["id"], on)
        if rolled is None:
            missing.append(a["name"])
            continue
        minor, snapshot = rolled
        balances[a["id"]] = minor
        last = _last_data_day(conn, a["id"], snapshot, on)
        if (on - date.fromisoformat(last)).days > STALE_DAYS:
            stale.append({"account": a["name"], "data_through": last})
        row = {"name": a["name"], "type": a["type"], "source": "account", "as_of": last}
        # Signed as the bank reports it: a negative card or loan balance is money owed.
        if minor >= 0:
            assets.append({**row, "amount": amount(minor, cfg.currency)})
        else:
            liabilities.append({**row, "amount": amount(-minor, cfg.currency)})
    for m in conn.execute("SELECT * FROM manual_assets ORDER BY name"):
        row = {"name": m["name"], "type": m["kind"], "source": "manual", "as_of": m["as_of"], "note": m["note"]}
        (liabilities if m["kind"] == "liability" else assets).append(
            {**row, "amount": amount(m["value"], cfg.currency)}
        )

    total_assets = sum(r["amount"]["minor"] for r in assets)
    total_liabilities = sum(r["amount"]["minor"] for r in liabilities)
    assets.sort(key=lambda r: -r["amount"]["minor"])
    liabilities.sort(key=lambda r: -r["amount"]["minor"])

    change_vs = []
    for days in CHANGE_WINDOWS:
        then_day = on - timedelta(days=days)
        now_sum = then_sum = 0
        used, skipped = 0, 0
        for a in accounts:
            if a["id"] not in balances:
                continue
            first = _first_data_day(conn, a["id"])
            then = rolled_balance(conn, a["id"], then_day) if first and first <= then_day.isoformat() else None
            if then is None:
                skipped += 1
                continue
            now_sum += balances[a["id"]]
            then_sum += then[0]
            used += 1
        if used:
            change_vs.append(
                {
                    "days": days,
                    "date": then_day.isoformat(),
                    "accounts_net_then": amount(then_sum, cfg.currency),
                    "accounts_net_now": amount(now_sum, cfg.currency),
                    "change": amount(now_sum - then_sum, cfg.currency),
                    "accounts_compared": used,
                    "accounts_without_history": skipped,
                }
            )
    assumptions = [
        "Account balances are signed as the bank reports them: negative on a credit card or loan means money owed "
        "(the OFX and SimpleFIN convention), so those balances are subtracted. A positive card balance (overpaid) "
        "counts as an asset.",
        "Each balance is the latest snapshot at or before the date, rolled forward by transactions posted since.",
        "Manual assets and liabilities are the values you entered, as of the date shown; they aren't revalued.",
        "change_vs compares accounts only (manual assets have no history), and only accounts with data on both dates.",
    ]
    if on != today:
        assumptions.append("Manual assets use their current values, not their value on the chosen date.")
    if missing:
        assumptions.append(
            "Some accounts have no balance on file and are left out of the totals (see missing_balances)."
        )
    if stale:
        assumptions.append(
            f"Some balances are more than {STALE_DAYS} days old (see stale_balances). Import fresh data."
        )
    return {
        "as_of": on.isoformat(),
        "assets": assets,
        "liabilities": liabilities,
        "total_assets": amount(total_assets, cfg.currency),
        "total_liabilities": amount(total_liabilities, cfg.currency),
        "net_worth": amount(total_assets - total_liabilities, cfg.currency),
        "change_vs": change_vs,
        "assumptions": assumptions,
        "missing_balances": missing,
        "stale_balances": stale,
        "meta": meta(conn, cfg, row_count=len(assets) + len(liabilities)),
    }


# --- afford --------------------------------------------------------------------------------


def afford(
    conn,
    cfg: Config,
    amount=None,
    date=None,
    buffer=0,
    days=30,
    recurring_monthly=False,
    today=None,
) -> dict:
    """Rerun the cash forecast with a purchase added (one-off, or monthly from its date) and compare."""
    today = today or _date.today()
    cost = money_arg(amount, cfg, "amount")
    if cost is None or cost <= 0:
        raise GuardError("amount must be a positive number of dollars (the cost)")
    if not isinstance(days, int) or isinstance(days, bool) or not 7 <= days <= 120:
        raise GuardError("days must be between 7 and 120")
    when = parse_date(date, "date") or today
    horizon = today + timedelta(days=days)
    if when < today:
        raise GuardError("date must be today or later")
    if when > horizon:
        raise GuardError(f"date is past the {days}-day forecast window ({horizon.isoformat()}); raise days (max 120)")
    buffer_minor = money_arg(buffer, cfg, "buffer") or 0
    if buffer_minor < 0:
        raise GuardError("buffer can't be negative")

    events, d, n = [], when, 0
    while d <= horizon:
        events.append({"date": d.isoformat(), "merchant": "Planned purchase", "kind": "what_if", "amount": -cost})
        n += 1
        if not recurring_monthly:
            break
        d = add_months(when, n)

    base = forecast_cash(conn, cfg, days=days, buffer=buffer, today=today)
    new = forecast_cash(conn, cfg, days=days, buffer=buffer, today=today, extra_events=events)
    low_base = base["lowest_balance"]["amount"]["minor"]
    low_new = new["lowest_balance"]["amount"]["minor"]
    safe_base = base["safe_to_spend"]["minor"]
    safe_new = new["safe_to_spend"]["minor"]
    affordable = low_new >= buffer_minor
    cur = cfg.currency
    assumptions = [
        f"Adds {_money(cost, cur)['display']} "
        + (
            f"monthly from {when.isoformat()} ({len(events)} charge{'s' if len(events) != 1 else ''} in the window)"
            if recurring_monthly
            else f"on {when.isoformat()}"
        )
        + " to the cash forecast, which uses personal checking, savings and cash accounts.",
        "affordable means the lowest projected balance stays at or above the buffer for the whole window.",
        *base["assumptions"],
    ]
    if not base["starting_balance_known"]:
        assumptions.append(
            "Some starting balances are missing, so treat the answer as unreliable until they're imported."
        )
    return {
        "amount": _money(cost, cur),
        "date": when.isoformat(),
        "recurring_monthly": bool(recurring_monthly),
        "charges_in_window": len(events),
        "horizon_days": days,
        "buffer": _money(buffer_minor, cur),
        "affordable": affordable,
        "baseline": {
            "lowest_balance": base["lowest_balance"],
            "safe_to_spend": base["safe_to_spend"],
            "goes_negative": base["goes_negative"],
        },
        "with_purchase": {
            "lowest_balance": new["lowest_balance"],
            "safe_to_spend": new["safe_to_spend"],
            "goes_negative": new["goes_negative"],
        },
        "delta": {
            "lowest_balance": _money(low_new - low_base, cur),
            "safe_to_spend": _money(safe_new - safe_base, cur),
        },
        "shortfall": _money(buffer_minor - low_new, cur) if not affordable else None,
        "starting_balance_known": base["starting_balance_known"],
        "assumptions": assumptions,
        "meta": meta(conn, cfg, today, horizon, buffer=_money(buffer_minor, cur)),
    }


# --- debt payoff ---------------------------------------------------------------------------


def _percent(value, where: str) -> Decimal:
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        apr = Decimal(str(value))
    except InvalidOperation as exc:
        raise GuardError(f"{where}: apr must be a number (percent, e.g. 24.99)") from exc
    if not apr.is_finite() or not 0 <= apr <= 100:
        raise GuardError(f"{where}: apr must be between 0 and 100 (percent)")
    return apr


def _debts_from_args(conn, cfg: Config, debts) -> tuple[list[dict], list[str], list[dict]]:
    check_items(debts)
    if len(debts) > MAX_DEBTS:
        raise GuardError(f"at most {MAX_DEBTS} debts")
    out, notes, seen = [], [], set()
    for i, item in enumerate(debts, start=1):
        where = f"debt {i}"
        if not isinstance(item, dict) or set(item) - {"name", "balance", "apr", "min_payment"}:
            raise GuardError(f"{where}: use {{name, balance, apr, min_payment}}")
        name = _name(item.get("name"), f"{where} name")
        if name.lower() in seen:
            raise GuardError(f"{where}: this name is already listed")
        seen.add(name.lower())
        terms = cfg.debts.get(name.lower())
        balance = money_arg(item.get("balance"), cfg, f"{where} balance")
        if balance is None:
            acct = find_account(conn, name)
            rolled = rolled_balance(conn, acct["id"]) if acct else None
            if rolled is not None and rolled[0] < 0:
                balance = -rolled[0]
                notes.append(f"debt {i}: balance taken from the account of the same name.")
        apr = _percent(item["apr"], where) if item.get("apr") is not None else None
        minimum = money_arg(item.get("min_payment"), cfg, f"{where} min_payment")
        if apr is None and terms:
            apr = Decimal(terms.apr_pct)
            notes.append(f"debt {i}: APR from config.toml [debts].")
        if minimum is None and terms:
            minimum = terms.min_payment
            notes.append(f"debt {i}: minimum payment from config.toml [debts].")
        missing = [k for k, v in (("balance", balance), ("apr", apr), ("min_payment", minimum)) if v is None]
        if missing:
            raise GuardError(f"{where}: missing {', '.join(missing)}. fiscl never guesses these.")
        if balance <= 0:
            raise GuardError(f"{where}: balance must be greater than 0")
        if minimum <= 0:
            raise GuardError(f"{where}: min_payment must be greater than 0")
        out.append({"name": name, "balance": balance, "apr": apr, "min": minimum})
    return out, notes, []


def _debts_from_accounts(conn, cfg: Config) -> tuple[list[dict], list[str], list[dict]]:
    rows = conn.execute(
        "SELECT id, name FROM accounts WHERE archived = 0 AND type IN (?, ?) ORDER BY name",
        DEBT_TYPES,
    ).fetchall()
    out, notes, lacking, left_out = [], [], [], []
    for a in rows:
        rolled = rolled_balance(conn, a["id"])
        if rolled is None:
            left_out.append({"account": a["name"], "reason": "no balance on file"})
            continue
        if rolled[0] >= 0:
            left_out.append({"account": a["name"], "reason": "nothing owed"})
            continue
        terms = cfg.debts.get(a["name"].lower())
        if terms is None:
            lacking.append(a["name"])
            continue
        out.append({"name": a["name"], "balance": -rolled[0], "apr": Decimal(terms.apr_pct), "min": terms.min_payment})
    if lacking:
        raise GuardError(
            f"APR and minimum payment are needed for {len(lacking)} credit or loan account(s) (list_accounts shows "
            "them). Bank exports don't carry these and fiscl never guesses. Pass "
            'debts=[{name, balance, apr, min_payment}] or add a [debts."<account name>"] table with apr and '
            "min_payment to config.toml.",
            code="needs_terms",
        )
    if not out:
        raise GuardError(
            "no credit or loan account with a balance owed. Pass debts=[{name, balance, apr, min_payment}] to model "
            "debts that aren't imported."
        )
    if left_out:
        notes.append(f"{len(left_out)} account(s) were left out (see accounts_left_out).")
    return out, notes, left_out


def _simulate(debts: list[dict], order_key, extra: int, cfg: Config, today: date) -> dict:
    state = [{**d, "bal": d["balance"], "interest": 0, "paid": 0, "done": None} for d in debts]
    order = sorted(state, key=order_key)
    pool = sum(d["min"] for d in state) + extra
    schedule, months = [], 0
    for month in range(1, MAX_PAYOFF_MONTHS + 1):
        active = [d for d in state if d["bal"] > 0]
        if not active:
            break
        months = month
        month_interest = 0
        for d in active:
            # Interest accrues first, to the cent, banker's rounding.
            interest = int((Decimal(d["bal"]) * d["apr"] / 1200).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
            d["bal"] += interest
            d["interest"] += interest
            month_interest += interest
        left, pays = pool, {}
        for d in active:  # minimums first
            p = min(d["min"], d["bal"], left)
            d["bal"] -= p
            left -= p
            pays[d["name"]] = p
        for d in order:  # then everything freed up goes to the target
            if left <= 0:
                break
            if d["bal"] > 0:
                p = min(left, d["bal"])
                d["bal"] -= p
                left -= p
                pays[d["name"]] += p
        for d in active:
            d["paid"] += pays[d["name"]]
            if d["bal"] == 0:
                d["done"] = month
        if month <= SCHEDULE_MONTHS:
            schedule.append(
                {
                    "month": month,
                    "date": add_months(today, month).isoformat(),
                    "interest": amount(month_interest, cfg.currency),
                    "payments": {n: amount(p, cfg.currency) for n, p in pays.items()},
                    "total_paid": amount(sum(pays.values()), cfg.currency),
                    "remaining_balance": amount(sum(d["bal"] for d in state), cfg.currency),
                }
            )
    still = [d for d in state if d["bal"] > 0]
    result = {
        "months": months,
        "paid_off": not still,
        "payoff_date": add_months(today, months).isoformat() if not still else None,
        "total_interest": amount(sum(d["interest"] for d in state), cfg.currency),
        "total_paid": amount(sum(d["paid"] for d in state), cfg.currency),
        "payoff_order": [
            {
                "name": d["name"],
                "starting_balance": amount(d["balance"], cfg.currency),
                "apr_pct": str(d["apr"]),
                "paid_off_month": d["done"],
                "payoff_date": add_months(today, d["done"]).isoformat() if d["done"] else None,
                "interest_paid": amount(d["interest"], cfg.currency),
                "total_paid": amount(d["paid"], cfg.currency),
                "remaining_balance": amount(d["bal"], cfg.currency),
            }
            for d in sorted(state, key=lambda d: (d["done"] is None, d["done"] or 0, d["name"]))
        ],
        "schedule": schedule,
    }
    if still:
        result["note"] = (
            f"Not paid off within {MAX_PAYOFF_MONTHS} months (the cap): payments don't outrun interest on "
            f"{', '.join(d['name'] for d in still)}. Raise extra_monthly or check the minimums."
        )
    return result


def debt_payoff(conn, cfg: Config, extra_monthly=0, strategy="both", debts=None, today=None) -> dict:
    """Month-by-month payoff plan for avalanche (highest APR first), snowball (smallest balance first), or both."""
    today = today or date.today()
    strategy = choice(strategy, ("avalanche", "snowball", "both"), "strategy")
    extra = money_arg(extra_monthly, cfg, "extra_monthly") or 0
    if extra < 0:
        raise GuardError("extra_monthly can't be negative")
    if debts is None:
        items, notes, left_out = _debts_from_accounts(conn, cfg)
    else:
        items, notes, left_out = _debts_from_args(conn, cfg, debts)
    orders = {
        "avalanche": lambda d: (-d["apr"], d["balance"], d["name"].lower()),
        "snowball": lambda d: (d["balance"], -d["apr"], d["name"].lower()),
    }
    names = ("avalanche", "snowball") if strategy == "both" else (strategy,)
    results = {n: _simulate(items, orders[n], extra, cfg, today) for n in names}
    cur = cfg.currency
    minimums = sum(d["min"] for d in items)
    out = {
        "extra_monthly": amount(extra, cur),
        "total_minimums": amount(minimums, cur),
        "monthly_payment_pool": amount(minimums + extra, cur),
        "debts": [
            {
                "name": d["name"],
                "balance": amount(d["balance"], cur),
                "apr_pct": str(d["apr"]),
                "min_payment": amount(d["min"], cur),
            }
            for d in items
        ],
        "strategies": results,
        "accounts_left_out": left_out,
    }
    if strategy == "both":
        a, s = results["avalanche"], results["snowball"]
        out["comparison"] = {
            "interest_saved_by_avalanche": amount(s["total_interest"]["minor"] - a["total_interest"]["minor"], cur),
            "months_saved_by_avalanche": s["months"] - a["months"],
        }
    out["assumptions"] = [
        "Interest each month is balance x APR / 12, rounded to the cent (banker's rounding), added before payments.",
        "Every month you pay the same total: all minimums plus extra_monthly. When a debt is paid off, its freed "
        "minimum goes to the next target.",
        "Avalanche targets the highest APR first; snowball targets the smallest starting balance first.",
        f"Month 1 is the payment one month from today ({add_months(today, 1).isoformat()}). No new charges, fees, or "
        "promotional rate changes are modeled.",
        f"Simulation stops at {MAX_PAYOFF_MONTHS} months.",
        *notes,
    ]
    return with_meta(conn, cfg, out, row_count=len(items))


# --- goals ---------------------------------------------------------------------------------


def _goal_view(row, account_name: str | None, cfg: Config) -> dict:
    return {
        "name": row["name"],
        "target": amount(row["target"], cfg.currency),
        "target_date": row["target_date"],
        "tracks_account": account_name,
        "saved": amount(row["saved"], cfg.currency) if account_name is None else None,
        "archived": bool(row["archived"]),
    }


def manage_goal(
    conn,
    cfg: Config,
    action=None,
    name=None,
    target=None,
    target_date=None,
    account=None,
    saved=None,
    dry_run=True,
    today=None,
) -> dict:
    """Add, update, or archive a savings goal that tracks an account balance or a manual saved amount."""
    today = today or date.today()
    action = choice(action, ("add", "update", "archive"), "action")
    name = _name(name)
    row = conn.execute("SELECT * FROM goals WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    target_minor = money_arg(target, cfg, "target")
    if target_minor is not None and target_minor <= 0:
        raise GuardError("target must be greater than 0")
    saved_minor = money_arg(saved, cfg, "saved")
    if saved_minor is not None and saved_minor < 0:
        raise GuardError("saved can't be negative")
    if account and saved_minor is not None:
        raise GuardError("give either account (track its balance) or saved (a manual amount), not both")
    when = parse_date(target_date, "target_date") if target_date else None
    if when is not None and when <= today:
        raise GuardError("target_date must be in the future")
    acct_id = account_id(conn, str(account)) if account else None

    def tracked(goal_account_id) -> str | None:
        if goal_account_id is None:
            return None
        return conn.execute("SELECT name FROM accounts WHERE id = ?", (goal_account_id,)).fetchone()[0]

    if action == "add":
        if row is not None:
            raise GuardError(
                "a goal with that name already exists" + (" (archived)" if row["archived"] else ""), code="exists"
            )
        if target_minor is None:
            raise GuardError("target is required to add a goal")
        after = {
            "name": name,
            "target": target_minor,
            "target_date": when.isoformat() if when else None,
            "account_id": acct_id,
            "saved": saved_minor or 0,
            "archived": 0,
        }
        before = None
    else:
        if row is None:
            raise GuardError("no goal with that name; list goals with goal_progress", code="not_found")
        before = dict(row)
        after = dict(row)
        if action == "archive":
            after["archived"] = 1
        else:
            if target_minor is None and target_date is None and account is None and saved_minor is None:
                raise GuardError("give at least one of target, target_date, account, saved to change")
            if target_minor is not None:
                after["target"] = target_minor
            if target_date is not None:
                after["target_date"] = when.isoformat() if when else None  # "" clears
            if account is not None:
                after["account_id"] = acct_id  # "" switches back to a manual amount
            if saved_minor is not None:
                after["saved"] = saved_minor
                after["account_id"] = None
            after["archived"] = 0  # updating an archived goal restores it
    plan = [
        {
            "action": action,
            "name": (before or after)["name"],
            "before": _goal_view(before, tracked(before["account_id"]), cfg) if before else None,
            "after": _goal_view(after, tracked(after["account_id"]), cfg),
        }
    ]
    if not dry_run:
        if action == "add":
            conn.execute(
                "INSERT INTO goals (name, target, target_date, account_id, saved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    after["name"],
                    after["target"],
                    after["target_date"],
                    after["account_id"],
                    after["saved"],
                    today.isoformat(),
                ),
            )
        else:
            conn.execute(
                "UPDATE goals SET target = ?, target_date = ?, account_id = ?, saved = ?, archived = ? WHERE id = ?",
                (
                    after["target"],
                    after["target_date"],
                    after["account_id"],
                    after["saved"],
                    after["archived"],
                    row["id"],
                ),
            )
    return with_meta(conn, cfg, plan_result(plan, 0, dry_run, "Call again with dry_run=false to apply."))


def _goal_progress_row(conn, cfg: Config, g: sqlite3.Row, today: date) -> dict:
    cur = cfg.currency
    current: int | None
    pace: int | None = None  # minor units per month over the last 90 days
    notes: list[str] = []
    account_name = None
    if g["account_id"] is None:
        current = g["saved"]
        notes.append("Manual goal: no balance history, so there's no pace. Update saved as you go.")
    else:
        account_name = conn.execute("SELECT name FROM accounts WHERE id = ?", (g["account_id"],)).fetchone()[0]
        now = rolled_balance(conn, g["account_id"])
        current = now[0] if now else None
        if now is None:
            notes.append("The tracked account has no balance on file.")
        else:
            then_day = today - timedelta(days=90)
            first = _first_data_day(conn, g["account_id"])
            then = rolled_balance(conn, g["account_id"], then_day) if first and first <= then_day.isoformat() else None
            if then is None:
                notes.append("Less than 90 days of history, so there's no pace yet.")
            else:
                pace = int((Decimal(now[0] - then[0]) * DAYS_PER_MONTH / 90).quantize(Decimal(1), ROUND_HALF_EVEN))
    row = {
        "name": g["name"],
        "archived": bool(g["archived"]),
        "tracks_account": account_name,
        "target": amount(g["target"], cur),
        "target_date": g["target_date"],
        "current": amount(current, cur) if current is not None else None,
        "pct": pct(current, g["target"]) if current is not None else None,
        "remaining": amount(max(g["target"] - current, 0), cur) if current is not None else None,
        "required_monthly": None,
        "pace_last_90_days_monthly": amount(pace, cur) if pace is not None else None,
        "on_track": None,
        "projected_completion": None,
        "notes": notes,
    }
    if current is None:
        return row
    remaining = max(g["target"] - current, 0)
    if remaining == 0:
        row["required_monthly"] = amount(0, cur)
        row["on_track"] = True
        notes.append("Target reached.")
        return row
    required = None
    if g["target_date"]:
        days_left = (date.fromisoformat(g["target_date"]) - today).days
        months_left = max(Decimal(1), Decimal(days_left) / DAYS_PER_MONTH)
        required = int((Decimal(remaining) / months_left).quantize(Decimal(1), ROUND_HALF_EVEN))
        row["required_monthly"] = amount(required, cur)
        if days_left <= 0:
            notes.append("The target date has passed.")
            row["on_track"] = False
    else:
        notes.append("No target date, so no required monthly amount.")
    if pace is not None:
        if required is not None and row["on_track"] is None:
            row["on_track"] = pace >= required
        if pace > 0:
            months_to_go = (Decimal(remaining) / pace).to_integral_value(rounding="ROUND_CEILING")
            if months_to_go <= 1200:
                row["projected_completion"] = (today + timedelta(days=int(months_to_go * DAYS_PER_MONTH))).isoformat()
        elif required is not None or g["target_date"] is None:
            notes.append("Balance has not grown over the last 90 days.")
    return row


def goal_progress(conn, cfg: Config, name=None, today=None) -> dict:
    """Progress for active goals (or one named goal): current, percent, required monthly, on-track by pace."""
    today = today or date.today()
    if name:
        goals = conn.execute(
            "SELECT * FROM goals WHERE name = ? COLLATE NOCASE", (clean_text(str(name), 200),)
        ).fetchall()
        if not goals:
            raise GuardError("no goal with that name; list goals with goal_progress", code="not_found")
    else:
        goals = conn.execute("SELECT * FROM goals WHERE archived = 0 ORDER BY name").fetchall()
    rows = [_goal_progress_row(conn, cfg, g, today) for g in goals]
    return {
        "goals": rows,
        "assumptions": [
            "on_track compares the last-90-days pace (balance change per month, including every deposit and "
            "withdrawal) with the monthly amount needed to reach the target by its date. A projection, not a promise.",
            "required_monthly spreads what's left over the months until target_date (at least one month).",
        ],
        "meta": meta(conn, cfg, row_count=len(rows)),
    }
