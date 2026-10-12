"""Amazon order-history import: the CSV from Amazon's "Request your data" > "Your Orders" export.

Rows are items, not transactions: they go to `amazon_items` and only feed `amazon_matches`, which proposes splits
for card charges. fiscl never logs in to Amazon or scrapes it; the user brings the file.

UNVERIFIED: the column names come from community write-ups of the export (docs/research/2026-10-11-holdings-amazon-
formats.md), not from Amazon or a real file. Every column name lives in `COLUMNS` so a format change is a one-line
fix, and two spellings seen in the wild are accepted. Whether `Total Owed` includes tax, shipping and discounts per
item row is also unverified; the matcher only relies on the rows of a shipment adding up to the card charge.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from ..config import Config
from ..guard import clean_text
from ..money import AmountError, is_currency_code, parse_amount
from .csv_import import CSVImportError

# field -> accepted header spellings (compared case-insensitively, after stripping quotes and spaces)
COLUMNS: dict[str, tuple[str, ...]] = {
    "order_id": ("Order ID",),
    "order_date": ("Order Date",),
    "ship_date": ("Ship Date",),
    "name": ("Product Name",),
    "quantity": ("Quantity", "Original Quantity"),
    "total": ("Total Owed", "Total Amount"),
    "currency": ("Currency",),
    "status": ("Order Status",),
}
REQUIRED = ("order_id", "order_date", "name", "total")
NOT_AVAILABLE = "not available"
MAX_ITEM_NAME = 200
_ORDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]{0,63}")
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[T ]|$)")
_US_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _norm(header: str) -> str:
    return re.sub(r"\s+", " ", header.replace('"', "").replace("﻿", "")).strip().lower()


def _column_map(header: list[str]) -> dict[str, int]:
    """field -> column index for the fields this header carries (the first spelling that matches wins)."""
    names = {_norm(h): i for i, h in enumerate(header)}
    found: dict[str, int] = {}
    for key, spellings in COLUMNS.items():
        for spelling in spellings:
            if _norm(spelling) in names:
                found[key] = names[_norm(spelling)]
                break
    return found


def _first_row(text: str) -> list[str]:
    for row in csv.reader(io.StringIO(text.lstrip("﻿")[:8192])):
        if any(cell.strip() for cell in row):
            return row
    return []


def looks_like_amazon(text: str) -> bool:
    """True when the first row has the columns every known variant of the export carries."""
    try:
        header = _first_row(text)
    except csv.Error:
        return False
    return bool(header) and all(key in _column_map(header) for key in REQUIRED)


@dataclass
class AmazonItem:
    order_id: str
    order_date: str
    ship_date: str | None
    name: str
    quantity: int
    total: int
    currency: str


@dataclass
class ParsedItems:
    items: list[AmazonItem] = field(default_factory=list)
    rows_seen: int = 0
    warnings: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.warnings[reason] = self.warnings.get(reason, 0) + 1


def parse_date(value: str | None) -> str | None:
    """An ISO date from `2026-09-02T08:00:00Z`, `2026-09-02`, or `9/2/2026`. Several values joined with " and "
    (split shipments) use the first one that reads. `Not Available` and junk are None. Times are UTC in the export, so
    a late-evening local order can land a day later; the matcher's window absorbs that."""
    for piece in re.split(r"\s+and\s+", (value or "").strip()):
        piece = piece.strip()
        if not piece or piece.lower() == NOT_AVAILABLE:
            continue
        if m := _ISO_DATE.match(piece):
            try:
                return date.fromisoformat(m.group(1)).isoformat()
            except ValueError:
                continue
        if m := _US_DATE.fullmatch(piece):
            try:
                return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
            except ValueError:
                continue
    return None


def _cell(row: list[str], cols: dict[str, int], key: str) -> str | None:
    i = cols.get(key)
    if i is None or i >= len(row):
        return None
    value = row[i].strip()
    return None if not value or value.lower() == NOT_AVAILABLE else value


def parse_items(text: str, cfg: Config) -> ParsedItems:
    """Parse the export. Rows that can't be used are counted in `warnings` (never named), not fatal:
    unreadable_rows (no order id/date or an unreadable amount), rows_without_charge (a zero or negative total),
    cancelled_rows (never charged)."""
    try:
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    except csv.Error as exc:
        raise CSVImportError("the file isn't a readable CSV") from exc
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows or not looks_like_amazon(text):
        raise CSVImportError("this doesn't look like an Amazon order-history export")
    cols = _column_map(rows[0])
    out = ParsedItems(rows_seen=len(rows) - 1)
    for row in rows[1:]:
        status = (_cell(row, cols, "status") or "").lower()
        if "cancel" in status:
            out.skip("cancelled_rows")
            continue
        order_id = _cell(row, cols, "order_id")
        order_date = parse_date(_cell(row, cols, "order_date"))
        currency = (_cell(row, cols, "currency") or cfg.currency).upper()
        raw_total = _cell(row, cols, "total")
        if not (order_id and _ORDER_ID.fullmatch(order_id) and order_date and raw_total):
            out.skip("unreadable_rows")
            continue
        if not is_currency_code(currency):
            currency = cfg.currency
        try:
            total = parse_amount(raw_total, currency)
        except AmountError:
            out.skip("unreadable_rows")
            continue
        if total <= 0:
            out.skip("rows_without_charge")
            continue
        try:
            quantity = max(1, min(int(_cell(row, cols, "quantity") or 1), 9999))
        except ValueError:
            quantity = 1
        out.items.append(
            AmazonItem(
                order_id=order_id,
                order_date=order_date,
                ship_date=parse_date(_cell(row, cols, "ship_date")),
                name=clean_text(_cell(row, cols, "name"), MAX_ITEM_NAME) or "Amazon item",
                quantity=quantity,
                total=total,
                currency=currency,
            )
        )
    return out


def _dedupe_key(item: AmazonItem, occurrence: int) -> str:
    raw = f"{item.order_id}|{item.name.lower()}|{item.ship_date}|{item.total}|{occurrence}"
    return "a:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def import_items(
    conn: sqlite3.Connection, cfg: Config, text: str, label: str, digest: str, result, dry_run: bool = False
):
    """Import one export atomically. `result` is the caller's ImportResult; it is returned filled in.
    Rows already stored (an overlapping export) are skipped by their content key."""
    from .core import _record_failure

    result.format = "amazon"
    savepoint = "fiscl_amazon"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        parsed = parse_items(text, cfg)
        conn.execute("DELETE FROM imports WHERE file_sha256 = ? AND status = 'failed'", (digest,))
        cur = conn.execute(
            "INSERT INTO imports (filename, file_sha256, format, status, imported_at) VALUES (?, ?, 'amazon', 'ok', ?)",
            (label, digest, datetime.now().isoformat(timespec="seconds")),
        )
        import_id = int(cur.lastrowid)
        seen: dict[tuple, int] = {}
        added = 0
        for item in parsed.items:
            ident = (item.order_id, item.name.lower(), item.ship_date, item.total)
            seen[ident] = seen.get(ident, 0) + 1
            added += conn.execute(
                """INSERT OR IGNORE INTO amazon_items
                   (order_id, order_date, ship_date, name, quantity, total, currency, import_id, dedupe_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.order_id,
                    item.order_date,
                    item.ship_date,
                    item.name,
                    item.quantity,
                    item.total,
                    item.currency,
                    import_id,
                    _dedupe_key(item, seen[ident]),
                ),
            ).rowcount
        result.rows_seen = parsed.rows_seen
        result.rows_added = added
        result.rows_skipped = parsed.rows_seen - added
        result.warnings = dict(parsed.warnings)
        conn.execute(
            "UPDATE imports SET rows_seen = ?, rows_added = ?, rows_skipped = ? WHERE id = ?",
            (result.rows_seen, added, result.rows_skipped, import_id),
        )
        if dry_run:
            conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
    except CSVImportError as exc:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        result.rows_seen = result.rows_added = result.rows_skipped = 0
        result.warnings = {}
        return _record_failure(conn, label, "amazon", digest, result, str(exc), dry_run)
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    return result
