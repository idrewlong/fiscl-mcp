"""Money handling. Every amount in fiscl is an integer number of minor units (cents).

Floats never touch money. Input from files and from tool calls goes through Decimal.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

SYMBOLS = {"USD": "$", "CAD": "CA$", "EUR": "€", "GBP": "£", "AUD": "A$", "JPY": "¥"}
ZERO_DECIMAL = {"JPY", "KRW"}


class AmountError(ValueError):
    pass


def _scale(currency: str) -> int:
    return 1 if currency.upper() in ZERO_DECIMAL else 100


def parse_amount(text: str, currency: str = "USD") -> int:
    """Parse a bank-export amount string into minor units.

    Handles: "-45.23", "(45.23)", "45.23 CR", "45.23 DR", "$1,234.56", "1.234,56", "+12".
    """
    if text is None:
        raise AmountError("missing amount")
    s = str(text).strip()
    if not s:
        raise AmountError("empty amount")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    upper = s.upper()
    if upper.endswith("CR"):
        s = s[:-2].strip()
    elif upper.endswith("DR"):
        neg, s = not neg, s[:-2].strip()
    for sym in ("$", "€", "£", "¥", "USD", "EUR", "GBP", " "):
        s = s.replace(sym, "")
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if s.endswith("-"):  # some banks put the sign last
        neg, s = not neg, s[:-1]
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    if not re.fullmatch(r"\d*\.?\d*", s) or s in ("", "."):
        raise AmountError(f"unparseable amount: {text!r}")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise AmountError(f"unparseable amount: {text!r}") from exc
    minor = int((value * _scale(currency)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    return -minor if neg else minor


def to_minor(value: int | float | str | Decimal, currency: str = "USD") -> int:
    """Convert a major-unit value from config or a tool argument (e.g. 400.00) to minor units."""
    if isinstance(value, bool):
        raise AmountError("boolean is not an amount")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise AmountError(f"not a number: {value!r}") from exc
    return int((d * _scale(currency)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def fmt(minor: int, currency: str = "USD") -> str:
    scale = _scale(currency)
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), scale)
    sym = SYMBOLS.get(currency.upper(), currency.upper() + " ")
    body = f"{whole:,}" if scale == 1 else f"{whole:,}.{frac:02d}"
    return f"{sign}{sym}{body}"


def amount(minor: int, currency: str = "USD") -> dict:
    """The shape every tool uses for money: exact integer plus a display string."""
    return {"minor": int(minor), "display": fmt(int(minor), currency)}


def pct(numerator: int, denominator: int) -> str | None:
    """Percentage as a string with one decimal, computed with integers. None if undefined."""
    if denominator == 0:
        return None
    tenths = (Decimal(numerator) * 1000 / Decimal(denominator)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return f"{tenths / 10:.1f}"
