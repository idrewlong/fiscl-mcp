"""Home office, simplified method: $5 per square foot, at most 300 sq ft, limited to the business's profit.

Only the simplified method is computed (no Form 8829, no depreciation of the home, no carryover of a limited
amount). The user enters square footage once per year in config.toml under [tax.home_office.YYYY].
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from ..config import Config
from ..money import amount

RATE_PER_SQFT_MINOR = 500  # $5.00
MAX_SQFT = 300

LIMIT_NOTE = (
    "The simplified home office deduction can't exceed the business's gross income minus other expenses; "
    "the unused part does not carry over under this method."
)
METHOD_NOTE = (
    "Home office uses the simplified method only ($5 per square foot, up to 300 square feet). The regular "
    "method (Form 8829), a second home, and daycare rules are not computed. Part-year use is averaged by months."
)


def _exact_sqft(square_feet: int, months_used: int) -> Decimal:
    """Square feet averaged over the year, never above the 300 sq ft cap. Not rounded: money is computed from this."""
    return min(Decimal(square_feet) * months_used / 12, Decimal(MAX_SQFT))


def allowable_sqft(square_feet: int, months_used: int) -> Decimal:
    """The averaged area rounded to one decimal place, for display only."""
    return _exact_sqft(square_feet, months_used).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)


def exact_deduction_minor(square_feet: int, months_used: int) -> int:
    """$5 per averaged square foot in minor units, rounded half-even once at the end."""
    exact = _exact_sqft(square_feet, months_used) * RATE_PER_SQFT_MINOR
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def home_office_deduction(cfg: Config, year: int, profit_before_minor: int) -> dict | None:
    """The line-30 block for `year`, or None when the user hasn't configured a home office for it."""
    ho = cfg.tax.home_office.get(year)
    if ho is None:
        return None
    cur = cfg.currency
    area = allowable_sqft(ho.square_feet, ho.months_used)  # shown rounded; the deduction below uses the exact area
    computed = exact_deduction_minor(ho.square_feet, ho.months_used)
    line_30 = min(computed, max(profit_before_minor, 0))
    return {
        "method": "simplified",
        "square_feet": ho.square_feet,
        "months_used": ho.months_used,
        "allowable_square_feet": f"{area:.1f}",
        "rate_per_sqft": amount(RATE_PER_SQFT_MINOR, cur),
        "computed": amount(computed, cur),
        "limited_to_profit": line_30 < computed,
        "line_30": amount(line_30, cur),
    }
