"""Holdings: what the user's latest positions imports say they hold.

Every price and value comes from an import; nothing here fetches a market price. Lots (several rows for one symbol)
are summed per account and symbol. Cost basis is reported only where every lot has one, and unrealized gain only
where both a value and a basis exist, so a missing number is never counted as zero.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext

from ..common import account_id, meta
from ..config import Config
from ..guard import GuardError, choice, clamp_limit, parse_date
from ..money import amount, pct

STALE_DAYS = 7
GROUP_BY = ("position", "asset_class", "account")


def snapshot_value(conn: sqlite3.Connection, acct: int, on: date) -> tuple[str, int, int] | None:
    """(as_of, total market value in minor units, positions without a value) of one account's latest snapshot at or
    before `on`, or None when it has none. Used by net_worth so a brokerage is valued from its positions."""
    snap = conn.execute(
        "SELECT id, as_of FROM holdings_snapshots WHERE account_id = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (acct, on.isoformat()),
    ).fetchone()
    if snap is None:
        return None
    total, unvalued = conn.execute(
        "SELECT COALESCE(SUM(market_value), 0), COALESCE(SUM(market_value IS NULL), 0) FROM holdings "
        "WHERE snapshot_id = ?",
        (snap["id"],),
    ).fetchone()
    return snap["as_of"], int(total), int(unvalued)


@dataclass
class _Position:
    account: str
    symbol: str
    name: str | None
    asset_class: str
    quantity: Decimal = Decimal(0)
    prices: set[str] = field(default_factory=set)
    value: int | None = None
    basis: int | None = None
    basis_missing: bool = False

    @property
    def gain(self) -> int | None:
        return None if self.value is None or self.basis is None else self.value - self.basis


def _add(total: int | None, part: int | None) -> int | None:
    return total if part is None else (part if total is None else total + part)


def _positions(conn: sqlite3.Connection, snaps: list[sqlite3.Row]) -> list[_Position]:
    if not snaps:
        return []
    names = {s["id"]: s["account"] for s in snaps}
    marks = ",".join("?" * len(names))
    rows = conn.execute(f"SELECT * FROM holdings WHERE snapshot_id IN ({marks}) ORDER BY id", list(names))
    merged: dict[tuple[str, str], _Position] = {}
    with localcontext() as ctx:
        ctx.prec = 80
        for r in rows:
            account = names[r["snapshot_id"]]
            pos = merged.setdefault(
                (account, r["symbol"]), _Position(account, r["symbol"], r["name"], r["asset_class"])
            )
            if pos.name is None:
                pos.name = r["name"]
            pos.quantity += Decimal(r["quantity"])
            if r["price"] is not None:
                pos.prices.add(r["price"])
            pos.value = _add(pos.value, r["market_value"])
            if r["cost_basis"] is None:
                pos.basis_missing = True
            else:
                pos.basis = _add(pos.basis, r["cost_basis"])
    for pos in merged.values():
        if pos.basis_missing:
            pos.basis = None  # a partial basis would understate cost and overstate gain
    return list(merged.values())


def _totals(positions: list[_Position], cur: str) -> dict:
    value = sum(p.value for p in positions if p.value is not None)
    return {
        "market_value": amount(value, cur),
        "cost_basis_known": amount(sum(p.basis for p in positions if p.basis is not None), cur),
        "unrealized_gain_known": amount(sum(g for p in positions if (g := p.gain) is not None), cur),
        "positions_without_basis": sum(1 for p in positions if p.basis is None),
        "positions_without_value": sum(1 for p in positions if p.value is None),
    }


def _canonical(d: Decimal) -> str:
    return "0" if d == 0 else format(d.normalize(), "f")


def _share(value: int | None, total: int) -> str | None:
    return pct(value, total) if value is not None and total > 0 else None


def _position_dict(p: _Position, total_value: int, cur: str) -> dict:
    gain = p.gain
    return {
        "account": p.account,
        "symbol": p.symbol,
        "name": p.name,
        "asset_class": p.asset_class,
        "quantity": _canonical(p.quantity),
        "price": next(iter(p.prices)) if len(p.prices) == 1 else None,
        "market_value": amount(p.value, cur) if p.value is not None else None,
        "cost_basis": amount(p.basis, cur) if p.basis is not None else None,
        "unrealized_gain": amount(gain, cur) if gain is not None else None,
        "unrealized_pct": pct(gain, p.basis) if gain is not None and p.basis and p.basis > 0 else None,
        "share_pct": _share(p.value, total_value),
    }


def _group_dict(label: dict, members: list[_Position], total_value: int, cur: str) -> dict:
    t = _totals(members, cur)
    value = t["market_value"]["minor"]
    return {
        **label,
        "positions": len(members),
        "market_value": t["market_value"],
        "cost_basis_known": t["cost_basis_known"],
        "unrealized_gain_known": t["unrealized_gain_known"],
        "share_pct": _share(value, total_value),
    }


def holdings(
    conn: sqlite3.Connection,
    cfg: Config,
    account: str | None = None,
    as_of: str | None = None,
    group_by: str = "position",
    limit: int | None = None,
    today: date | None = None,
) -> dict:
    """Positions from each account's latest snapshot at or before `as_of` (default today)."""
    today = today or date.today()
    on = parse_date(as_of, "as_of") or today
    if on > today:
        raise GuardError("as_of can't be in the future")
    group_by = choice(group_by, GROUP_BY, "group_by")
    cap = clamp_limit(limit, cfg)
    only = account_id(conn, account)

    sql = """SELECT s.id, s.account_id, s.as_of, a.name AS account FROM holdings_snapshots s
             JOIN accounts a ON a.id = s.account_id
             WHERE a.archived = 0 AND s.as_of <= :on
               AND s.as_of = (SELECT MAX(s2.as_of) FROM holdings_snapshots s2
                              WHERE s2.account_id = s.account_id AND s2.as_of <= :on)"""
    if only is not None:
        sql += " AND s.account_id = :only"
    snaps = conn.execute(sql + " ORDER BY a.name", {"on": on.isoformat(), "only": only}).fetchall()
    snapshots = [
        {"account": s["account"], "as_of": s["as_of"], "stale": (on - date.fromisoformat(s["as_of"])).days > STALE_DAYS}
        for s in snaps
    ]

    cur = cfg.currency
    positions = _positions(conn, snaps)
    positions.sort(key=lambda p: (p.value is None, -(p.value or 0), p.account, p.symbol))
    totals = _totals(positions, cur)
    total_value = totals["market_value"]["minor"]

    out: dict = {"snapshots": snapshots}
    if group_by == "position":
        items = [_position_dict(p, total_value, cur) for p in positions]
        out["positions"] = items[:cap]
        shown, total_count = len(out["positions"]), len(items)
    else:
        buckets: dict[tuple, list[_Position]] = {}
        as_of_by_account = {s["account"]: s["as_of"] for s in snaps}
        for p in positions:
            key = ("asset_class", p.asset_class) if group_by == "asset_class" else ("account", p.account)
            buckets.setdefault(key, []).append(p)
        groups = []
        for (kind, label), members in buckets.items():
            tag = {kind: label}
            if kind == "account":
                tag["as_of"] = as_of_by_account[label]
            groups.append(_group_dict(tag, members, total_value, cur))
        groups.sort(key=lambda g: (-g["market_value"]["minor"], str(g.get("asset_class") or g.get("account"))))
        out["groups"] = groups[:cap]
        shown, total_count = len(out["groups"]), len(groups)
    out["totals"] = totals

    notes = ["Prices and values come from your holdings imports; fiscl never fetches market prices."]
    if not snaps:
        notes.append("No holdings snapshot found. Import a positions file with `fiscl import FILE --account NAME`.")
    if any(s["stale"] for s in snapshots):
        notes.append(f"Some snapshots are more than {STALE_DAYS} days old; import a fresh positions file.")
    if totals["positions_without_basis"]:
        notes.append(
            "Cost basis and unrealized gain cover only positions that have them "
            "(totals say 'known'); positions_without_basis counts the rest."
        )
    if totals["positions_without_value"]:
        notes.append("Some positions have no price or value in the import and are left out of the totals.")
    notes.append("This reports what your imports say; it is not investment advice.")
    out["notes"] = notes
    out["meta"] = meta(
        conn,
        cfg,
        row_count=shown,
        total_count=total_count,
        truncated=total_count > shown,
        as_of=on.isoformat(),
        holdings_freshness=snapshots,
    )
    return out
