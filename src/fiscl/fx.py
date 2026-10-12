"""Exchange rates and home-currency amounts.

Rates come only from the user (`set_fx_rate`, `fiscl fx import`); nothing here talks to a network. A rate means
"home units per 1 unit of the foreign currency". Every transaction keeps its original `amount` and `currency`;
`amount_home` (minor units of the home currency) is materialized per row with banker's rounding, so aggregates can
sum it and the result is reproducible. A foreign row uses the latest rate dated on or before its posting date, as long
as that rate is at most `fx.max_rate_age_days` older; otherwise `amount_home` is NULL ("unconverted") and the row stays
out of totals.
"""

from __future__ import annotations

import csv
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from pathlib import Path

from .config import Config
from .guard import GuardError, parse_date
from .money import CURRENCY_CODE_MSG, exponent, is_currency_code

MAX_RATE = Decimal(1_000_000)
MAX_SIGNIFICANT_DIGITS = 12
MIN_ADJUSTED_EXPONENT = -15  # rejects absurdly tiny rates whose conversions would all round to zero
MAX_SQLITE_INT = 2**63 - 1
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 20000
_IN_CHUNK = 500


# --- validation ----------------------------------------------------------------------------------------------------


def parse_rate(value: object) -> Decimal:
    """A rate argument as a validated Decimal: finite, 0 < rate <= 1,000,000, at most 12 significant digits."""
    if isinstance(value, bool) or value is None or not isinstance(value, int | float | str | Decimal):
        raise GuardError("rate must be a positive number")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise GuardError("rate must be a positive number") from exc
    if not d.is_finite() or d <= 0:
        raise GuardError("rate must be a positive number")
    if d > MAX_RATE or d.adjusted() < MIN_ADJUSTED_EXPONENT:
        raise GuardError("rate is out of range (more than 0 and at most 1,000,000)")
    if len(d.normalize().as_tuple().digits) > MAX_SIGNIFICANT_DIGITS:
        raise GuardError(f"rate may have at most {MAX_SIGNIFICANT_DIGITS} significant digits")
    return d


def rate_text(rate: Decimal) -> str:
    """The canonical stored form: no exponent, no trailing zeros."""
    return format(rate.normalize(), "f")


def check_foreign_currency(value: object, cfg: Config, field: str = "currency") -> str:
    """An upper-cased ISO-style code that isn't the home currency (a home rate would be meaningless)."""
    code = value.strip().upper() if isinstance(value, str) else ""
    if not is_currency_code(code):
        raise GuardError(f"{field} {CURRENCY_CODE_MSG}")
    if code == cfg.currency:
        raise GuardError(f"{field} can't be your home currency; rates convert other currencies into it")
    return code


# --- conversion ----------------------------------------------------------------------------------------------------


def convert_minor(minor: int, currency: str, home: str, rate: Decimal) -> int:
    """round_half_even(minor / 10^exp(currency) * rate * 10^exp(home)). One call = one row's rounding."""
    with localcontext() as ctx:
        ctx.prec = 60
        major = Decimal(minor).scaleb(-exponent(currency))
        return int((major * rate).scaleb(exponent(home)).to_integral_value(rounding=ROUND_HALF_EVEN))


def split_homes(children: list[int], parent_amount: int, parent_home: int | None) -> list[int | None]:
    """Home amounts of a split's parts. Each is round_half_even(child * parent_home / parent_amount); the rounding
    remainder goes to the part with the largest absolute amount (first on ties), so the parts sum exactly to the
    parent's home amount. An unconverted parent leaves every part unconverted."""
    if parent_home is None:
        return [None] * len(children)
    if parent_amount == 0:
        return [0] * len(children)
    with localcontext() as ctx:
        ctx.prec = 60
        homes = [
            int((Decimal(c) * parent_home / Decimal(parent_amount)).to_integral_value(rounding=ROUND_HALF_EVEN))
            for c in children
        ]
    diff = parent_home - sum(homes)
    if diff and homes:
        biggest = max(range(len(children)), key=lambda i: (abs(children[i]), -i))
        homes[biggest] += diff
    return homes


class RateBook:
    """The user's rates, loaded lazily per currency, for many lookups against one snapshot of the table."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config) -> None:
        self.conn = conn
        self.home = cfg.currency
        self.max_age = cfg.fx_max_rate_age_days
        self._by_currency: dict[str, tuple[list[str], list[Decimal]]] = {}

    def _load(self, currency: str) -> tuple[list[str], list[Decimal]]:
        if currency not in self._by_currency:
            dates: list[str] = []
            rates: list[Decimal] = []
            for row in self.conn.execute(
                "SELECT rate_date, rate FROM fx_rates WHERE currency = ? ORDER BY rate_date", (currency,)
            ):
                try:
                    rate = Decimal(row["rate"])
                except InvalidOperation:
                    continue  # a hand-edited bad value is skipped rather than trusted
                if rate.is_finite() and rate > 0:
                    dates.append(row["rate_date"])
                    rates.append(rate)
            self._by_currency[currency] = (dates, rates)
        return self._by_currency[currency]

    def rate_on(self, currency: str, day: str) -> tuple[Decimal, str] | None:
        """(rate, its date) in force on `day`, or None when there is none within the allowed age."""
        dates, rates = self._load(currency)
        i = bisect_right(dates, day) - 1
        if i < 0:
            return None
        try:
            age = (date.fromisoformat(day) - date.fromisoformat(dates[i])).days
        except ValueError:
            return None
        return (rates[i], dates[i]) if age <= self.max_age else None

    def convert(self, minor: int, currency: str, day: str) -> int | None:
        """Home-currency minor units for `minor` of `currency` on `day`; None when unconvertible."""
        if currency == self.home:
            return minor
        found = self.rate_on(currency, day)
        if found is None:
            return None
        out = convert_minor(minor, currency, self.home, found[0])
        return out if abs(out) <= MAX_SQLITE_INT else None


# --- materializing amount_home --------------------------------------------------------------------------------------


def refresh_home_amounts(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    currency: str | None = None,
    start: str | None = None,
    end: str | None = None,
    foreign_only: bool = False,
    ids: list[int] | None = None,
) -> list[dict]:
    """Recompute amount_home for transactions and their split parts; returns the transactions whose value changed.

    With no filter at all this is the full refresh `fiscl fx refresh` runs (it also records the home currency).
    """
    if ids is not None:
        out: list[dict] = []
        for i in range(0, len(ids), _IN_CHUNK):
            out += _refresh(conn, cfg, currency, start, end, foreign_only, ids[i : i + _IN_CHUNK])
        return out
    changed = _refresh(conn, cfg, currency, start, end, foreign_only, None)
    if currency is None and start is None and end is None and not foreign_only:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('home_currency', ?)", (cfg.currency,))
    return changed


def _refresh(conn, cfg, currency, start, end, foreign_only, ids) -> list[dict]:
    home = cfg.currency
    book = RateBook(conn, cfg)
    where, params = ["1 = 1"], []
    if currency is not None:
        where.append("t.currency = ?")
        params.append(currency)
    elif foreign_only:
        where.append("t.currency != ?")
        params.append(home)
    if start is not None:
        where.append("t.posted_date >= ?")
        params.append(start)
    if end is not None:
        where.append("t.posted_date <= ?")
        params.append(end)
    if ids is not None:
        where.append(f"t.id IN ({','.join('?' * len(ids))})")
        params += ids
    clause = " AND ".join(where)

    updates, changed = [], {}
    for r in conn.execute(
        f"SELECT t.id, t.posted_date, t.amount, t.currency, t.amount_home FROM transactions t WHERE {clause}", params
    ).fetchall():
        new = book.convert(r["amount"], r["currency"], r["posted_date"])
        if new != r["amount_home"]:
            updates.append((new, r["id"]))
            changed[r["id"]] = {"id": r["id"], "currency": r["currency"], "posted_date": r["posted_date"]}
    conn.executemany("UPDATE transactions SET amount_home = ? WHERE id = ?", updates)

    # Parts follow their parent's (new) home amount.
    parts: dict[int, list[sqlite3.Row]] = {}
    for s in conn.execute(
        f"""SELECT s.id, s.transaction_id, s.amount, s.amount_home, t.amount AS p_amount, t.amount_home AS p_home,
                   t.currency, t.posted_date
            FROM splits s JOIN transactions t ON t.id = s.transaction_id WHERE {clause} ORDER BY s.id""",
        params,
    ):
        parts.setdefault(s["transaction_id"], []).append(s)
    split_updates = []
    for tid, group in parts.items():
        homes = split_homes([g["amount"] for g in group], group[0]["p_amount"], group[0]["p_home"])
        for g, new in zip(group, homes, strict=True):
            if new != g["amount_home"]:
                split_updates.append((new, g["id"]))
                changed.setdefault(tid, {"id": tid, "currency": g["currency"], "posted_date": g["posted_date"]})
    conn.executemany("UPDATE splits SET amount_home = ? WHERE id = ?", split_updates)
    return list(changed.values())


# --- changing rates -------------------------------------------------------------------------------------------------


@dataclass
class RateItem:
    day: str
    currency: str
    rate: Decimal | None  # None = remove


def current_rates(conn: sqlite3.Connection, items: list[RateItem]) -> list[str | None]:
    """The stored rate text for each item's (currency, date), or None."""
    out = []
    for it in items:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE currency = ? AND rate_date = ?", (it.currency, it.day)
        ).fetchone()
        out.append(row["rate"] if row else None)
    return out


def _apply_items(conn: sqlite3.Connection, items: list[RateItem], source: str) -> None:
    for it in items:
        if it.rate is None:
            conn.execute("DELETE FROM fx_rates WHERE currency = ? AND rate_date = ?", (it.currency, it.day))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates (rate_date, currency, rate, source) VALUES (?, ?, ?, ?)",
                (it.day, it.currency, rate_text(it.rate), source),
            )


def change_rates(conn: sqlite3.Connection, cfg: Config, items: list[RateItem], source: str, apply: bool) -> list[dict]:
    """Apply `items`, recompute the transactions they can reach, and return those that changed. When `apply` is
    False everything is rolled back, so the result is an exact preview of what applying would do."""
    windows: dict[str, tuple[str, str]] = {}
    for it in items:
        end = (date.fromisoformat(it.day) + timedelta(days=cfg.fx_max_rate_age_days)).isoformat()
        lo, hi = windows.get(it.currency, (it.day, end))
        windows[it.currency] = (min(lo, it.day), max(hi, end))
    conn.execute("SAVEPOINT fx_rate_change")
    try:
        _apply_items(conn, items, source)
        changed: list[dict] = []
        for cur, (lo, hi) in windows.items():
            changed += refresh_home_amounts(conn, cfg, currency=cur, start=lo, end=hi)
        if not apply:
            conn.execute("ROLLBACK TO fx_rate_change")
    except BaseException:
        conn.execute("ROLLBACK TO fx_rate_change")
        conn.execute("RELEASE fx_rate_change")
        raise
    conn.execute("RELEASE fx_rate_change")
    return changed


def reaches(item: RateItem, row: dict, max_age: int) -> bool:
    """Could this item's rate have decided this changed row? (Same currency, within the age window after it.)"""
    if row["currency"] != item.currency:
        return False
    return item.day <= row["posted_date"] <= (date.fromisoformat(item.day) + timedelta(days=max_age)).isoformat()


def import_rates_csv(conn: sqlite3.Connection, cfg: Config, path: Path, apply: bool = False) -> dict:
    """Load `date,currency,rate` rows (header required) as source 'csv'. Errors name a row number, never a value."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_CSV_BYTES:
            raise GuardError("rates file is larger than 5 MB")
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise GuardError("couldn't read the rates file", code="not_found") from exc
    except UnicodeDecodeError as exc:
        raise GuardError("rates file must be UTF-8 text") from exc
    reader = csv.reader(text.splitlines())
    header = next(reader, None)
    names = [h.strip().lower() for h in header or []]
    if not {"date", "currency", "rate"} <= set(names):
        raise GuardError("rates file needs a header row with the columns date, currency, rate")
    col = {n: names.index(n) for n in ("date", "currency", "rate")}
    items: list[RateItem] = []
    seen: set[tuple[str, str]] = set()
    for n, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(items) >= MAX_CSV_ROWS:
            raise GuardError(f"at most {MAX_CSV_ROWS} rates per file")
        try:
            raw_date, raw_cur, raw_rate = (row[col[k]] for k in ("date", "currency", "rate"))
        except IndexError as exc:
            raise GuardError(f"row {n}: missing a column") from exc
        try:
            day = parse_date(raw_date.strip(), "date")
            if day is None:
                raise GuardError("date is required")
            cur = check_foreign_currency(raw_cur, cfg)
            rate = parse_rate(raw_rate)
        except GuardError as exc:
            raise GuardError(f"row {n}: {exc}") from exc
        key = (cur, day.isoformat())
        if key in seen:
            raise GuardError(f"row {n}: a rate for the same currency and date appears twice")
        seen.add(key)
        items.append(RateItem(day.isoformat(), cur, rate))
    if not items:
        raise GuardError("the rates file has no rows")
    before = current_rates(conn, items)
    changed = change_rates(conn, cfg, items, "csv", apply)
    return {
        "dry_run": not apply,
        "rates": len(items),
        "new": sum(1 for b in before if b is None),
        "replaced": sum(1 for b in before if b is not None),
        "transactions_affected": len(changed),
    }


# --- what's missing ---------------------------------------------------------------------------------------------------


def unconverted_in_period(conn: sqlite3.Connection, cfg: Config, start: date, end: date) -> dict | None:
    """meta.unconverted for a period, or None when every row in it has a home amount."""
    rows = conn.execute(
        """SELECT currency, COUNT(*) AS n FROM transactions
           WHERE amount_home IS NULL AND transfer_group IS NULL AND posted_date BETWEEN ? AND ?
           GROUP BY currency ORDER BY currency""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if not rows:
        return None
    return {
        "count": sum(r["n"] for r in rows),
        "currencies": [r["currency"] for r in rows],
        "note": (
            f"Some transactions in other currencies have no exchange rate within {cfg.fx_max_rate_age_days} days "
            "and are left out of totals. Add rates with set_fx_rate."
        ),
    }


def home_currency_notice(conn: sqlite3.Connection, cfg: Config) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'home_currency'").fetchone()
    if row is None or row["value"] == cfg.currency:
        return None
    return (
        "The home currency in config.toml differs from the one this data was last converted to. "
        "Run `fiscl fx refresh` to recompute home-currency amounts; until then totals can be wrong."
    )
