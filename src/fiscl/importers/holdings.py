"""Holdings snapshot import: a positions file becomes one snapshot per account and date.

Quantities and prices are exact Decimal strings (crypto needs 18 places); values and cost basis are minor units.
Prices come only from the file: nothing here ever fetches a market price. Brokers put the full account number in a
column; this module never stores it. With the default policy the column is not read at all, and with `last4` it is
cut down to its last four characters on the spot (used only to pick the fiscl account), so the full number never
reaches the database, the import result, or an error message.
Error text is fixed prose plus a row number; file values are never echoed.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from ..config import Config
from ..db import add_account, find_account
from ..guard import clean_text, safe_regex
from ..money import AmountError, exponent, parse_amount, to_minor
from .csv_import import (
    CSVImportError,
    Profile,
    _get,
    _norm,
    _parse_date,
    load_profiles,
    match_profile,
    read_profile_rows,
)
from .ofx import looks_like_ofx
from .qif import looks_like_qif

MAX_ROWS = 20_000
MAX_PLACES = 18  # quantity and price decimal places
MAX_DIGITS = 36
MAX_QUANTITY = Decimal(10**15)
MIN_AS_OF = date(2000, 1, 1)

ASSET_CLASSES = ("equity", "fund", "bond", "cash", "crypto", "option", "other")
# Words brokers use in an asset-type column, lower-cased. Anything else is "other".
_CLASS_ALIASES = {
    "equity": "equity",
    "equities": "equity",
    "stock": "equity",
    "stocks": "equity",
    "common stock": "equity",
    "etf": "fund",
    "etfs": "fund",
    "etfs & closed end funds": "fund",
    "fund": "fund",
    "funds": "fund",
    "mutual fund": "fund",
    "mutual funds": "fund",
    "bond": "bond",
    "bonds": "bond",
    "fixed income": "bond",
    "cash": "cash",
    "cash and money market": "cash",
    "cash & cash investments": "cash",
    "money market": "cash",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "option": "option",
    "options": "option",
}
_NULLS = {"", "-", "--", "n/a", "na", "not available"}
_DECIMAL = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


@dataclass
class Position:
    symbol: str
    name: str | None
    asset_class: str
    quantity: str  # canonical Decimal string
    price: str | None
    market_value: int | None
    cost_basis: int | None
    account_key: str | None = None  # last 4 characters of the file's account column, when the profile asks for it


@dataclass
class ParsedHoldings:
    as_of: str
    positions: list[Position]
    skipped: int  # summary rows, zero-quantity rows, and rows with no account value


def find_profile(text: str, cfg: Config, profile_name: str | None) -> Profile | None:
    """The holdings profile for this file, or None when it isn't a holdings file (OFX/QIF never are)."""
    if looks_like_ofx(text[:2000]) or looks_like_qif(text):
        return None
    profiles = load_profiles(cfg)
    if profile_name:
        found = next((p for p in profiles if p.name == profile_name), None)
    else:
        found = match_profile(text, profiles)
    return found if found is not None and found.kind == "holdings" else None


# --- parsing -----------------------------------------------------------------------------


def _decimal(text: str, field: str, row: int) -> Decimal | None:
    cleaned = text.strip().replace("$", "").replace(",", "").replace(" ", "")
    if cleaned.lower() in _NULLS:
        return None
    if not _DECIMAL.fullmatch(cleaned):
        raise CSVImportError(f"bad {field} in row {row}")
    d = Decimal(cleaned)
    exponent = d.as_tuple().exponent
    if (isinstance(exponent, int) and -exponent > MAX_PLACES) or len(d.as_tuple().digits) > MAX_DIGITS:
        raise CSVImportError(f"{field} in row {row} has too many digits")
    if abs(d) >= MAX_QUANTITY:
        raise CSVImportError(f"{field} in row {row} is too large")
    return d


def _canonical(d: Decimal) -> str:
    return "0" if d == 0 else format(d.normalize(), "f")


def _money(text: str, currency: str, field: str, row: int) -> int | None:
    if text.strip().lower() in _NULLS:
        return None
    try:
        return parse_amount(text, currency)
    except AmountError as exc:
        raise CSVImportError(f"bad {field} in row {row}") from exc


def _check_as_of(value: date) -> str:
    if not MIN_AS_OF <= value <= date.today() + timedelta(days=1):
        raise CSVImportError("the snapshot date is out of range")
    return value.isoformat()


def _as_of_from(value: str, fmt: str | None) -> str:
    return _check_as_of(date.fromisoformat(_parse_date(value, fmt)))


def _resolve_as_of(text: str, profile: Profile, rows: list[dict[str, str]], given: str | None) -> str:
    """The snapshot date: --as-of wins, then the profile's date column, then a regex over the file's lines."""
    if given:
        try:
            return _check_as_of(date.fromisoformat(given))
        except ValueError as exc:
            raise CSVImportError("--as-of must be a date like 2026-10-01") from exc
    if profile.as_of_column:
        values = {_get(r, profile.as_of_column) for r in rows} - {""}
        if len(values) > 1:
            raise CSVImportError("the as-of column holds more than one date; import one snapshot per file")
        if values:
            return _as_of_from(values.pop(), profile.as_of_format or profile.date_format)
    if profile.as_of_regex:
        rx = safe_regex(profile.as_of_regex)
        for line in text.splitlines():
            if m := rx.search(line[:500]):
                return _as_of_from(m.group(1), profile.as_of_format)
    raise CSVImportError("this holdings file has no snapshot date: pass --as-of YYYY-MM-DD")


def parse_holdings(text: str, cfg: Config, profile: Profile, as_of: str | None = None) -> ParsedHoldings:
    header, rows = read_profile_rows(text, profile)
    have = {_norm(h) for h in header}
    if _norm(profile.symbol_column or "") not in have or _norm(profile.quantity_column or "") not in have:
        raise CSVImportError("the file has no column for the symbol or the quantity")
    if len(rows) > MAX_ROWS:
        raise CSVImportError("the file has too many rows for a holdings snapshot")
    snapshot_date = _resolve_as_of(text, profile, rows, as_of)
    scale = to_minor(1, cfg.currency)
    skip = {s.strip().lower() for s in profile.skip_symbols or []}
    default_class = "crypto" if profile.account_type == "crypto" else "other"
    use_last4 = bool(profile.account_column) and profile.account_column_policy == "last4"
    positions: list[Position] = []
    skipped = 0
    for n, row in enumerate(rows, start=1):
        symbol = clean_text(_get(row, profile.symbol_column), 32)
        if not symbol or symbol.lower() in skip:
            skipped += 1
            continue
        key = None
        if use_last4:
            # Cut to the last four characters immediately; the full value is never kept anywhere.
            key = re.sub(r"[^A-Za-z0-9]", "", _get(row, profile.account_column))[-4:].upper() or None
            if key is None:
                skipped += 1
                continue
        raw_class = _get(row, profile.asset_class_column).lower()
        asset_class = _CLASS_ALIASES.get(raw_class, "other") if raw_class else default_class
        quantity = _decimal(_get(row, profile.quantity_column), "quantity", n)
        price = _decimal(_get(row, profile.price_column), "price", n)
        if price is not None and price < 0:
            raise CSVImportError(f"bad price in row {n}")
        value = _money(_get(row, profile.value_column), cfg.currency, "market value", n)
        cost = _money(_get(row, profile.cost_basis_column), cfg.currency, "cost basis", n)
        if quantity is None:
            # A money-market line with only a value (no quantity or price) is cash, counted in currency units.
            if value is None or asset_class not in ("cash", "other"):
                raise CSVImportError(f"bad or missing quantity in row {n}")
            quantity, price = Decimal(value) / scale, Decimal(1)
            asset_class = "cash"
        if quantity == 0:
            skipped += 1
            continue
        if value is None and price is not None:
            with localcontext() as ctx:
                ctx.prec = 80
                value = int((quantity * price * scale).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
            if abs(value) > scale * 10**12:
                raise CSVImportError(f"market value in row {n} is too large")
        positions.append(
            Position(
                symbol=symbol,
                name=clean_text(_get(row, profile.name_column), 100) or None,
                asset_class=asset_class,
                quantity=_canonical(quantity),
                price=_canonical(price) if price is not None else None,
                market_value=value,
                cost_basis=cost,
                account_key=key,
            )
        )
    if not positions:
        raise CSVImportError("no positions found in this file")
    return ParsedHoldings(as_of=snapshot_date, positions=positions, skipped=skipped)


# --- storing -----------------------------------------------------------------------------


def _route(
    conn: sqlite3.Connection, cfg: Config, profile: Profile, positions: list[Position], account_name: str | None
) -> list[tuple[sqlite3.Row, list[Position]]]:
    """Pick the account for each position. Runs inside the import's savepoint, so a created account rolls back."""
    target = clean_text(account_name or profile.account or "", 100)
    keys = {p.account_key for p in positions if p.account_key}
    if target:
        if len(keys) > 1:
            raise CSVImportError("this file holds several accounts; import it without --account")
        row = find_account(conn, target)
        if row is None:
            add_account(conn, target, profile.account_type, None, cfg.currency)
            row = find_account(conn, target)
        return [(row, positions)]
    if not keys:
        raise CSVImportError("holdings files need an account: pass --account NAME or set `account` in the profile")
    routed = []
    for key in sorted(keys):
        found = conn.execute(
            "SELECT * FROM accounts WHERE last4 = ? COLLATE NOCASE AND archived = 0", (key,)
        ).fetchall()
        if len(found) != 1:
            raise CSVImportError(
                "an account in this file doesn't match exactly one fiscl account by its last 4 characters; "
                "add them with `fiscl accounts add NAME --type brokerage --last4 1234` or pass --account"
            )
        routed.append((found[0], [p for p in positions if p.account_key == key]))
    return routed


def store_snapshot(
    conn: sqlite3.Connection, account_id: int, as_of: str, import_id: int, currency: str, positions: list[Position]
) -> bool:
    """Write one snapshot, replacing any earlier one for the same account and date. Returns True if it replaced."""
    old = conn.execute(
        "SELECT id FROM holdings_snapshots WHERE account_id = ? AND as_of = ?", (account_id, as_of)
    ).fetchone()
    if old:
        conn.execute("DELETE FROM holdings WHERE snapshot_id = ?", (old["id"],))
        conn.execute("DELETE FROM holdings_snapshots WHERE id = ?", (old["id"],))
    cur = conn.execute(
        "INSERT INTO holdings_snapshots (account_id, as_of, import_id, created_at) VALUES (?, ?, ?, ?)",
        (account_id, as_of, import_id, datetime.now().isoformat(timespec="seconds")),
    )
    snapshot_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO holdings (snapshot_id, symbol, name, asset_class, quantity, price, market_value, cost_basis,
               currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (snapshot_id, p.symbol, p.name, p.asset_class, p.quantity, p.price, p.market_value, p.cost_basis, currency)
            for p in positions
        ],
    )
    return old is not None


def import_holdings(
    conn: sqlite3.Connection,
    cfg: Config,
    text: str,
    profile: Profile,
    label: str,
    digest: str,
    result,
    account_name: str | None = None,
    as_of: str | None = None,
    dry_run: bool = False,
):
    """Import one positions file atomically. `result` is the caller's ImportResult; it is returned filled in."""
    from .core import ImportFailed, _record_failure

    result.format = "holdings"
    savepoint = "fiscl_holdings"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        parsed = parse_holdings(text, cfg, profile, as_of)
        conn.execute("DELETE FROM imports WHERE file_sha256 = ? AND status = 'failed'", (digest,))
        cur = conn.execute(
            "INSERT INTO imports (filename, file_sha256, format, status, imported_at) "
            "VALUES (?, ?, 'holdings', 'ok', ?)",
            (label, digest, datetime.now().isoformat(timespec="seconds")),
        )
        import_id = int(cur.lastrowid)
        targets = _route(conn, cfg, profile, parsed.positions, account_name)
        for account, positions in targets:
            acct_currency = account["currency"].upper()
            if exponent(acct_currency) != exponent(cfg.currency):
                # Values were parsed with the home currency's decimal places; another exponent would misread them.
                raise CSVImportError(
                    "holdings can't be imported into an account whose currency has a different number of "
                    "decimal places than your home currency"
                )
            if store_snapshot(conn, account["id"], parsed.as_of, import_id, acct_currency, positions):
                result.snapshots_replaced += 1
            result.accounts.append(account["name"])
        unvalued = sum(1 for p in parsed.positions if p.market_value is None)
        result.rows_seen = len(parsed.positions) + parsed.skipped
        result.rows_added = len(parsed.positions)
        result.rows_skipped = parsed.skipped
        result.as_of = parsed.as_of
        if unvalued:
            result.warnings["positions_without_value"] = unvalued
        only_account = targets[0][0]["id"] if len(targets) == 1 else None
        conn.execute(
            "UPDATE imports SET account_id = ?, rows_seen = ?, rows_added = ?, rows_skipped = ? WHERE id = ?",
            (only_account, result.rows_seen, result.rows_added, parsed.skipped, import_id),
        )
        if dry_run:
            conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
    except (CSVImportError, ImportFailed) as exc:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        result.accounts, result.rows_seen, result.rows_added, result.rows_skipped = [], 0, 0, 0
        result.as_of, result.snapshots_replaced, result.warnings = None, 0, {}
        return _record_failure(conn, label, "holdings", digest, result, str(exc), dry_run)
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    return result
