"""Home office, simplified method: $5 per square foot, at most 300 sq ft, limited to the business's profit.

Only the simplified method is computed (no Form 8829, no depreciation of the home, no carryover of a limited
amount). The user enters square footage once per year in config.toml under [tax.home_office.YYYY]. With several
businesses in the home, an [allocation] table splits the area by business name; each business is limited by its own
profit. Without an allocation the whole area belongs to the default business.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from ..config import Config
from ..db import DEFAULT_BUSINESS_ID
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
UNASSIGNED_NOTE = (
    "No part of the home office area is assigned to this business. Without an allocation the whole area goes to the "
    "default business; add [tax.home_office.{year}.allocation] to split it by business name."
)
UNMATCHED_NOTE = (
    "The home office allocation for {year} lists a name that matches no business; check the spelling against "
    "list_businesses."
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


def business_square_feet(cfg: Config, year: int, business: dict | None) -> int | None:
    """The area assigned to `business` for `year`: None when no home office is set up or none is assigned to it.

    `business` is {"id", "name"}; None means the one-business case (everything goes to it).
    """
    ho = cfg.tax.home_office.get(year)
    if ho is None:
        return None
    if business is None:
        return ho.square_feet
    if ho.allocation:
        return {k.casefold(): v for k, v in ho.allocation.items()}.get(business["name"].casefold())
    return ho.square_feet if business["id"] == DEFAULT_BUSINESS_ID else None


def unmatched_allocations(cfg: Config, year: int, business_names: list[str]) -> bool:
    """True when the year's allocation names something that isn't a business."""
    ho = cfg.tax.home_office.get(year)
    if ho is None or not ho.allocation:
        return False
    known = {n.casefold() for n in business_names}
    return any(k.casefold() not in known for k in ho.allocation)


def home_office_deduction(
    cfg: Config, year: int, profit_before_minor: int, business: dict | None = None
) -> dict | None:
    """The line-30 block for `year`, or None when no home office is configured for it (or none is assigned)."""
    ho = cfg.tax.home_office.get(year)
    sqft = business_square_feet(cfg, year, business)
    if ho is None or sqft is None:
        return None
    cur = cfg.currency
    area = allowable_sqft(sqft, ho.months_used)  # shown rounded; the deduction below uses the exact area
    computed = exact_deduction_minor(sqft, ho.months_used)
    line_30 = min(computed, max(profit_before_minor, 0))
    block = {
        "method": "simplified",
        "square_feet": sqft,
        "months_used": ho.months_used,
        "allowable_square_feet": f"{area:.1f}",
        "rate_per_sqft": amount(RATE_PER_SQFT_MINOR, cur),
        "computed": amount(computed, cur),
        "limited_to_profit": line_30 < computed,
        "line_30": amount(line_30, cur),
    }
    if sqft != ho.square_feet:
        block["home_square_feet"] = ho.square_feet  # this business's share of a larger home office area
    return block
