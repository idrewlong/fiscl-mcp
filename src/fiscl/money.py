"""Money handling. Every amount in fiscl is an integer number of minor units (cents).

Floats never touch money. Input from files and from tool calls goes through Decimal.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

SYMBOLS = {"USD": "$", "CAD": "CA$", "EUR": "€", "GBP": "£", "AUD": "A$", "JPY": "¥"}

# Minor-unit exponents (ISO 4217 "minor unit" column): the number of decimal places a currency's minor unit carries.
# Zero-exponent: BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF. Three-exponent: BHD IQD JOD KWD
# LYD OMR TND. Every other currency uses two. (Source: ISO 4217 list one, published by SIX Group.)
_ZERO_EXPONENT = ("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG")
_ZERO_EXPONENT += ("RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF")
_THREE_EXPONENT = ("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND")
_FOUR_EXPONENT = ("CLF", "UYW")
CURRENCY_EXPONENT: dict[str, int] = {
    **dict.fromkeys(_ZERO_EXPONENT, 0),
    **dict.fromkeys(_THREE_EXPONENT, 3),
    **dict.fromkeys(_FOUR_EXPONENT, 4),
}
ZERO_DECIMAL = {code for code, exp in CURRENCY_EXPONENT.items() if exp == 0}
# ISO 4217 codes with no minor unit ("N.A."): precious metals, IMF units, the test code and "no currency". They have no
# decimal convention to price in, so they are refused as transaction currencies rather than guessed at two places.
NO_MINOR_UNIT = frozenset({"XAU", "XAG", "XPT", "XPD", "XDR", "XTS", "XXX", "XBA", "XBB", "XBC", "XBD", "XSU", "XUA"})
CURRENCY_CODE_MSG = "must be a 3-letter currency code like EUR (metals, fund units and test codes are not supported)"
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def exponent(currency: str) -> int:
    return CURRENCY_EXPONENT.get(currency.upper(), 2)


def is_currency_code(value: object) -> bool:
    return isinstance(value, str) and _CURRENCY_CODE.fullmatch(value) is not None and value not in NO_MINOR_UNIT


class AmountError(ValueError):
    pass


MAX_MAJOR = Decimal(10**12)  # $1 trillion: anything larger is a typo or an attack, not money


def _check_range(d: Decimal, original: object) -> Decimal:
    if not d.is_finite():
        raise AmountError("not a finite number")
    if abs(d) > MAX_MAJOR:
        raise AmountError("amount is too large")
    return d


def _scale(currency: str) -> int:
    return 10 ** exponent(currency)


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
        raise AmountError("unparseable amount")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise AmountError("unparseable amount") from exc
    _check_range(value, text)
    minor = int((value * _scale(currency)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    return -minor if neg else minor


def to_minor(value: int | float | str | Decimal, currency: str = "USD") -> int:
    """Convert a major-unit value from config or a tool argument (e.g. 400.00) to minor units."""
    if isinstance(value, bool):
        raise AmountError("boolean is not an amount")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise AmountError("not a number") from exc
    _check_range(d, value)
    return int((d * _scale(currency)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def fmt(minor: int, currency: str = "USD") -> str:
    scale = _scale(currency)
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), scale)
    sym = SYMBOLS.get(currency.upper(), currency.upper() + " ")
    places = exponent(currency)
    body = f"{whole:,}" if places == 0 else f"{whole:,}.{frac:0{places}d}"
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
