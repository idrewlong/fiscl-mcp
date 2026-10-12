"""QIF (Quicken Interchange Format) parser.

Imports the bank-like sections (`!Type:Bank`, `CCard`, `Cash`, `Oth A`, `Oth L`). Everything else (investment
transactions, category/class/memorized lists, unknown sections, `!Option:`/`!Clear:` lines) is skipped and counted.
Splits import as one row for the transaction's total; the split lines are counted, not stored.

QIF dates carry no declared order, so the order is a setting (US month-first by default). A file that cannot be
month-first (for example `13/01/2026`) fails with fixed prose instead of guessing. Messages never echo file values.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from ..config import Config
from ..guard import clean_text
from ..money import AmountError, parse_amount
from .types import ParsedStatement, ParsedTxn

# Section name (after `!Type:`, lower-cased) -> account type hint.
SECTION_HINTS = {"bank": "checking", "ccard": "credit", "cash": "cash", "oth a": "other", "oth l": "loan"}
DATE_ORDERS = ("mdy", "dmy")
DAY_FIRST_MSG = "QIF dates look day-first; re-import with --qif-date dmy"
MONTH_FIRST_MSG = "QIF dates look month-first; re-import with --qif-date mdy"

_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DOTTED = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})")
# `M/D'YY`, `M/ D/YY`, `MM/DD/YYYY`: Quicken pads with spaces and marks post-1999 two-digit years with an apostrophe.
_SLASHED = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*['/]\s*(\d{2}|\d{4})")
_TRANSFER = re.compile(r"\[.+\]")


class QIFError(ValueError):
    pass


def looks_like_qif(text: str) -> bool:
    """True when the first non-blank line is a QIF header."""
    for line in text.lstrip("﻿").splitlines():
        stripped = line.strip().lower()
        if stripped:
            return stripped.startswith(("!type:", "!account", "!option:", "!clear:"))
    return False


def _bad_date(index: int) -> QIFError:
    return QIFError(f"bad or missing date in QIF record {index}")


def _year(raw: str) -> int:
    return int(raw) if len(raw) == 4 else 2000 + int(raw)


def _parse_date(value: str, order: str, index: int) -> str:
    text = value.strip()
    try:
        if m := _ISO.fullmatch(text):
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif m := _DOTTED.fullmatch(text):
            d, mo, y = int(m[1]), int(m[2]), _year(m[3])  # dotted dates are day-first in every export seen
        elif m := _SLASHED.fullmatch(text):
            first, second, y = int(m[1]), int(m[2]), _year(m[3])
            if order == "mdy":
                if first > 12 and second <= 12:
                    raise QIFError(DAY_FIRST_MSG)
                mo, d = first, second
            else:
                if second > 12 and first <= 12:
                    raise QIFError(MONTH_FIRST_MSG)
                d, mo = first, second
        else:
            raise _bad_date(index)
        return date(y, mo, d).isoformat()
    except ValueError as exc:
        if isinstance(exc, QIFError):
            raise
        raise _bad_date(index) from exc


def _build_txn(rec: dict[str, str], order: str, currency: str, index: int) -> ParsedTxn:
    posted = _parse_date(rec.get("D", ""), order, index)
    raw_amount = rec.get("T") or rec.get("U")  # T wins; U is a duplicate some exports write instead
    if not raw_amount:
        raise QIFError(f"no amount in QIF record {index}")
    try:
        amount = parse_amount(raw_amount, currency)
    except AmountError as exc:
        raise QIFError(f"bad amount in QIF record {index}") from exc
    payee, memo = clean_text(rec.get("P")), clean_text(rec.get("M"))
    description = payee or memo or "(no description)"
    memo_out = memo if payee and memo else None
    check = clean_text(rec.get("N"), 20)
    if check:
        memo_out = f"{memo_out}; check {check}" if memo_out else f"check {check}"
    if _TRANSFER.match(rec.get("L", "")):
        description += " TRANSFER"  # transfers.py already treats this word as a transfer hint
    return ParsedTxn(posted_date=posted, amount=amount, description=description, memo=memo_out)


def parse_qif(text: str, cfg: Config, date_format: str | None = None) -> list[ParsedStatement]:
    order = date_format or cfg.qif_date_order
    if order not in DATE_ORDERS:
        raise QIFError("QIF date order must be mdy or dmy")
    statements: dict[tuple[str | None, str], ParsedStatement] = {}
    warnings: Counter[str] = Counter()
    mode: str | None = None  # account | txn | skip
    in_account_list = False  # between !Option:AutoSwitch and !Clear:AutoSwitch the !Account blocks are a list
    pending_account: str | None = None  # applies to the next !Type: section only, never to later ones
    section_account: str | None = None
    hint = "other"
    rec: dict[str, str] = {}
    index = 0

    def finish_record() -> None:
        nonlocal pending_account, index
        if not rec:
            return
        if mode == "account":
            if not in_account_list and rec.get("N"):
                pending_account = clean_text(rec["N"], 100) or None
        elif mode == "txn":
            index += 1
            txn = _build_txn(rec, order, cfg.currency, index)
            stmt = statements.setdefault(
                (section_account, hint),
                ParsedStatement(
                    format="qif", txns=[], currency=cfg.currency, type_hint=hint, account_name=section_account
                ),
            )
            stmt.txns.append(txn)
        rec.clear()

    for line in text.lstrip("﻿").splitlines():
        if not line.strip():
            continue
        if line.startswith("!"):
            finish_record()
            header = line[1:].strip().lower()
            if header.startswith("option:") or header.startswith("clear:"):
                warnings["ignored_option_lines"] += 1
                if header.startswith("option:autoswitch"):
                    in_account_list = True
                elif header.startswith("clear:autoswitch"):
                    in_account_list = False
            elif header.startswith("account"):
                mode = "account"
            elif header.startswith("type:") and header[5:].strip() in SECTION_HINTS:
                mode = "txn"
                hint = SECTION_HINTS[header[5:].strip()]
                section_account, pending_account = pending_account, None
            else:
                mode = "skip"
                warnings["skipped_sections"] += 1
            continue
        if mode not in ("account", "txn"):
            continue
        code, value = line[0], line[1:].strip()
        if code == "^":
            finish_record()
            continue
        if code == "S":
            warnings["split_lines"] += 1
        if code in "DTUPMNL" and code not in rec:  # first occurrence wins; other codes are ignored
            rec[code] = value
    finish_record()

    if not statements:
        raise QIFError("no bank, card, cash, or asset/liability transactions found (other QIF sections are skipped)")
    first = next(iter(statements.values()))
    first.warnings = {k: v for k, v in warnings.items() if v}
    return list(statements.values())
