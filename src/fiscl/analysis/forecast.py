"""Cash forecast and safe-to-spend."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from ..common import account_scope_sql, latest_balance, meta, money_arg
from ..config import Config
from ..db import SPENDING_FILTER
from ..guard import GuardError
from ..money import amount
from ..recurring import add_months, next_date

LIQUID_TYPES = ("checking", "savings", "cash")


def _marks(values) -> str:
    return ",".join("?" * len(values))


def _card_payment_events(
    conn,
    liquid_ids: list[int],
    today: date,
    horizon: date,
    assumptions: list[str],
    currency: str = "USD",
    account_filter: str = "",
) -> list:
    """Card payments are transfers, so recurring detection skips them. Project each card's next
    payment from its charges since the last payment, then later months from the median payment."""
    if not liquid_ids:
        return []
    events = []
    for card in conn.execute(
        f"SELECT id, name FROM accounts WHERE archived = 0 AND type = 'credit' AND currency = ? {account_filter}",
        (currency,),
    ).fetchall():
        pays = conn.execute(
            f"""SELECT l.posted_date AS paid, c.posted_date AS card_date, -l.amount AS amt
                FROM transactions l JOIN transactions c
                  ON c.transfer_group = l.transfer_group AND c.account_id = ?
                WHERE l.account_id IN ({_marks(liquid_ids)}) AND l.amount < 0 ORDER BY l.posted_date""",
            [card["id"], *liquid_ids],
        ).fetchall()
        if not pays:
            continue
        days_of_month = sorted(int(p["paid"][8:10]) for p in pays)
        pay_day = days_of_month[len(days_of_month) // 2]
        typical = sorted(p["amt"] for p in pays)[len(pays) // 2]
        owed = conn.execute(
            """SELECT COALESCE(-SUM(amount), 0) FROM transactions
               WHERE account_id = ? AND transfer_group IS NULL AND posted_date > ?""",
            (card["id"], pays[-1]["card_date"]),
        ).fetchone()[0]
        last_paid = date.fromisoformat(pays[-1]["paid"])
        month = add_months(last_paid.replace(day=1), 1)
        first = True
        while True:
            due = month.replace(day=min(pay_day, calendar.monthrange(month.year, month.month)[1]))
            if due > horizon:
                break
            if due > today:
                amt = owed if first else typical
                if amt > 0:
                    events.append(
                        {
                            "date": due.isoformat(),
                            "merchant": f"{card['name']} payment",
                            "kind": "card_payment",
                            "amount": -amt,
                        }
                    )
                first = False
            month = add_months(month, 1)
        assumptions.append(
            f"{card['name']}: next payment estimated as charges since the last payment "
            f"({amount(owed, currency)['display']}), later months at the typical payment "
            f"({amount(typical, currency)['display']}), around day {pay_day}."
        )
    return events


def rolled_balance(conn, account_id: int, on: date | None = None) -> tuple[int, str] | None:
    """One account's balance, rolled by transactions from its balance snapshot to `on` (default: all of them).

    Returns (minor units, as_of of the snapshot used) or None when the account has no balance on file. With
    `on`, uses the latest snapshot at or before that day, else the earliest later one rolled backward. Shared by
    the forecast and net_worth so both agree on what an account holds.
    """
    if on is None:
        row = latest_balance(conn, account_id)
        if row is None:
            return None
        after = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_id = ? AND posted_date > ?",
            (account_id, row["as_of"]),
        ).fetchone()[0]
        return row["amount"] + after, row["as_of"]
    day = on.isoformat()
    row = conn.execute(
        "SELECT amount, as_of FROM balances WHERE account_id = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (account_id, day),
    ).fetchone()
    if row is not None:
        after = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_id = ? AND posted_date > ? "
            "AND posted_date <= ?",
            (account_id, row["as_of"], day),
        ).fetchone()[0]
        return row["amount"] + after, row["as_of"]
    row = conn.execute(
        "SELECT amount, as_of FROM balances WHERE account_id = ? AND as_of > ? ORDER BY as_of LIMIT 1",
        (account_id, day),
    ).fetchone()
    if row is None:
        return None
    between = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_id = ? AND posted_date > ? "
        "AND posted_date <= ?",
        (account_id, day, row["as_of"]),
    ).fetchone()[0]
    return row["amount"] - between, row["as_of"]


def _starting_balance(conn, liquid, assumptions: list[str]) -> tuple[int, bool]:
    """Sum of each liquid account's latest balance, rolled forward by transactions posted since."""
    total, missing = 0, []
    for acct in liquid:
        rolled = rolled_balance(conn, acct["id"])
        if rolled is None:
            missing.append(acct["name"])
            continue
        total += rolled[0]
    if missing:
        assumptions.append(
            f"No balance on file for: {', '.join(missing)} (treated as $0). "
            "Import an OFX file or pass starting_balance."
        )
    if not liquid:
        assumptions.append("No checking, savings, or cash accounts found.")
    return total, bool(liquid) and not missing


def _recurring_events(conn, liquid_ids: list[int], today: date, horizon: date) -> list[dict]:
    if not liquid_ids:
        return []
    events = []
    recs = conn.execute(
        f"""SELECT * FROM recurring WHERE status IN ('active', 'price_changed', 'missed')
            AND confidence >= 0.6 AND account_id IN ({_marks(liquid_ids)})""",
        liquid_ids,
    ).fetchall()
    for r in recs:
        d = date.fromisoformat(r["next_expected"])
        if r["status"] == "missed":
            if r["direction"] == "in":
                continue  # never count on income that has stopped arriving
            d = today + timedelta(days=1)  # assume an overdue bill lands tomorrow
        while d <= today:
            d = next_date(d, r["cadence"])
        while d <= horizon:
            signed = r["last_amount"] if r["direction"] == "in" else -r["last_amount"]
            events.append({"date": d.isoformat(), "merchant": r["merchant"], "kind": r["kind"], "amount": signed})
            d = next_date(d, r["cadence"])
    return events


def _daily_discretionary(conn, liquid_ids: list[int], today: date) -> int:
    """90-day average of spending from these accounts at merchants that aren't recurring."""
    recurring_merchants = {r[0] for r in conn.execute("SELECT merchant FROM recurring")}
    since = (today - timedelta(days=90)).isoformat()
    rows = conn.execute(
        f"""SELECT t.merchant, -SUM(t.amount) AS s FROM txn_lines t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.posted_date > ? AND t.posted_date <= ? AND {SPENDING_FILTER}
              AND t.account_id IN ({_marks(liquid_ids)}) GROUP BY t.merchant""",
        [since, today.isoformat(), *liquid_ids],
    ).fetchall()
    return sum(r["s"] for r in rows if r["merchant"] not in recurring_merchants) // 90


def forecast_cash(
    conn,
    cfg: Config,
    days=30,
    starting_balance=None,
    buffer=0,
    include_discretionary=True,
    scope="personal",
    today=None,
    extra_events=None,
) -> dict:
    """Project liquid-account balances forward from known recurring items.

    `extra_events` is internal (afford uses it, the MCP tool doesn't expose it): hypothetical
    {date, merchant, kind, amount (signed minor units)} items. Those dated today or earlier come off the
    starting balance; later ones join the projection.
    """
    today = today or date.today()
    if not isinstance(days, int) or not 7 <= days <= 120:
        raise GuardError("days must be between 7 and 120")
    buffer_minor = money_arg(buffer, cfg, "buffer") or 0
    horizon = today + timedelta(days=days)
    acct_filter = account_scope_sql(scope)
    liquid = conn.execute(
        f"""SELECT id, name FROM accounts WHERE archived = 0 AND type IN ({_marks(LIQUID_TYPES)})
            AND currency = ? {acct_filter}""",
        [*LIQUID_TYPES, cfg.currency],
    ).fetchall()
    liquid_ids = [r["id"] for r in liquid]
    assumptions: list[str] = []
    foreign = conn.execute(
        f"""SELECT name FROM accounts WHERE archived = 0 AND type IN ({_marks(LIQUID_TYPES)})
            AND currency != ? {acct_filter} ORDER BY name""",
        [*LIQUID_TYPES, cfg.currency],
    ).fetchall()
    if foreign:
        assumptions.append(
            f"Accounts in other currencies are left out of the forecast: {', '.join(r['name'] for r in foreign)}."
        )

    if starting_balance is not None:
        start_bal = money_arg(starting_balance, cfg, "starting_balance")
        balance_known = True
        assumptions.append("Starting balance supplied by you.")
    else:
        start_bal, balance_known = _starting_balance(conn, liquid, assumptions)

    events = _recurring_events(conn, liquid_ids, today, horizon)
    events += _card_payment_events(conn, liquid_ids, today, horizon, assumptions, cfg.currency, acct_filter)
    if scope != "all":
        assumptions.append(f"Includes {scope} accounts only (scope={scope}); mixed accounts count as personal.")
    for e in extra_events or []:
        if e["date"] <= today.isoformat():
            start_bal += e["amount"]
        elif e["date"] <= horizon.isoformat():
            events.append(e)
    events.sort(key=lambda e: (e["date"], e["amount"]))
    assumptions.append("Uses recurring items detected on checking/savings/cash accounts and their most recent amounts.")

    daily_disc = 0
    if include_discretionary and liquid_ids:
        daily_disc = _daily_discretionary(conn, liquid_ids, today)
        assumptions.append(
            f"Everyday spending of {amount(daily_disc, cfg.currency)['display']}/day, the "
            "90-day average of non-recurring spending from these accounts."
        )

    by_day: dict[str, int] = {}
    for e in events:
        by_day[e["date"]] = by_day.get(e["date"], 0) + e["amount"]
    balance = start_bal
    daily, low, low_day = [], start_bal, today.isoformat()
    next_income = next((e["date"] for e in events if e["amount"] > 0), None)
    for i in range(1, days + 1):
        d = (today + timedelta(days=i)).isoformat()
        balance += by_day.get(d, 0) - daily_disc
        daily.append({"date": d, "balance": balance})
        if balance < low:
            low, low_day = balance, d
    # Spending X today lowers every later day by X, so the most you can spend without the projection
    # ever dropping below the buffer is the lowest projected balance minus the buffer.
    safe = low - buffer_minor
    return {
        "scope": scope,
        "starting_balance": amount(start_bal, cfg.currency),
        "starting_balance_known": balance_known,
        "horizon_days": days,
        "projected_end_balance": amount(balance, cfg.currency),
        "lowest_balance": {"amount": amount(low, cfg.currency), "date": low_day},
        "goes_negative": low < 0,
        "next_income_date": next_income,
        "safe_to_spend": amount(max(safe, 0), cfg.currency),
        "safe_to_spend_note": f"Extra you could spend today without the projected balance dropping below the "
        f"buffer at any point in the next {days} days, after bills, card payments"
        + (" and typical everyday spending." if include_discretionary else "."),
        "upcoming": [{**e, "amount": amount(e["amount"], cfg.currency)} for e in events[:40]],
        "daily_balance": daily,
        "assumptions": assumptions,
        "meta": meta(conn, cfg, today, horizon, buffer=amount(buffer_minor, cfg.currency)),
    }
