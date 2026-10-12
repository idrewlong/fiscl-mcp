"""Rough quarterly estimated tax from year-to-date business profit."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from ..common import meta, year_bounds
from ..config import Config
from ..guard import GuardError
from ..money import amount, pct
from .home_office import home_office_deduction
from .lines import DISCLAIMER
from .schedule_c import schedule_c_draft
from .states import STATES, VERIFIED_FOR, next_business_day, state_installments

MAX_STATE_RATE = 20
STATE_BASE_NOTE = (
    "States define taxable income differently; this applies your planning rate to the same base as the federal "
    "estimate. It is not a state return and ignores state credits, deductions, and local taxes."
)


def due_dates(year: int) -> list[str]:
    """The four 1040-ES dates for tax year `year`, moved to the next business day when they land on a weekend."""
    raw = [date(year, 4, 15), date(year, 6, 15), date(year, 9, 15), date(year + 1, 1, 15)]
    return [next_business_day(d).isoformat() for d in raw]


def _state_code(raw) -> str:
    code = raw.strip().upper() if isinstance(raw, str) else None
    if code not in STATES:
        raise GuardError("state must be a two-letter US state code (or DC)")
    return code


def _state_rate(raw) -> Decimal | None:
    if raw is None:
        return None
    try:
        rate = Decimal(str(raw)) if not isinstance(raw, bool) else None
    except InvalidOperation:
        rate = None
    if rate is None or not rate.is_finite() or not 0 <= rate <= MAX_STATE_RATE:
        raise GuardError(f"state_income_tax_rate_pct must be a number between 0 and {MAX_STATE_RATE}")
    return rate


def _split(total: int, percents: list[Decimal]) -> list[int]:
    """Divide `total` by percentages in minor units; the rounding remainder lands on the last non-zero share."""
    parts = [int((Decimal(total) * p / 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)) for p in percents]
    paying = [i for i, p in enumerate(percents) if p > 0]
    if paying:
        parts[paying[-1]] += total - sum(parts)
    return parts


def _state_block(code: str, rate_raw, year: int, taxable: int, today: date, cur: str) -> dict:
    """Planning-rate state estimate and installment dates. No brackets: rate x the federal taxable base."""
    info = STATES[code]
    rate = _state_rate(rate_raw)
    block: dict = {
        "code": code,
        "name": info.name,
        "no_income_tax": not info.income_tax,
        "estimated_required": info.estimated_required,
        "rate_pct": None,
        "income_tax_estimate": None,
        "installments": [],
        "verified_for": VERIFIED_FOR,
        "verified": info.verified,
    }
    notes: list[str] = []
    if not info.income_tax:
        notes.append(f"{info.name} has no broad personal income tax on wages or self-employment income.")
        if rate is not None:
            notes.append("The state planning rate you supplied is ignored for this state.")
    else:
        notes.append(STATE_BASE_NOTE)
        estimate = None
        if rate is None:
            notes.append("No state estimate: pass state_income_tax_rate_pct or set tax.state_income_tax_rate_pct.")
        else:
            estimate = int((Decimal(taxable) * rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
            block["rate_pct"] = str(rate)
            block["income_tax_estimate"] = amount(estimate, cur)
        if info.estimated_required:
            dated = state_installments(code, year)
            amounts = _split(estimate, [Decimal(p) for _, p in dated]) if estimate is not None else None
            block["installments"] = [
                {
                    "date": d.isoformat(),
                    "pct": p,
                    "amount": amount(amounts[i], cur) if amounts is not None else None,
                    "passed": d.isoformat() < today.isoformat(),
                }
                for i, (d, p) in enumerate(dated)
            ]
            notes.append(
                "Dates on a weekend or federal holiday are shown on the next business day; state-only holidays "
                "aren't known here, so confirm dates with your state."
            )
        else:
            notes.append(
                "No required quarterly installments for this state; you may prepay any time and settle by the "
                "April return due date."
            )
    if info.caveat:
        notes.append(info.caveat)
    if not info.verified:
        notes.append(
            f"This state's row came from a source that wasn't confirmed for {VERIFIED_FOR}; confirm with your "
            "state's revenue department."
        )
    if year != VERIFIED_FOR:
        notes.append(f"The state table was checked for {VERIFIED_FOR}; verify this year's dates with your state.")
    block["notes"] = notes
    return block


def estimated_tax(
    conn,
    cfg: Config,
    year=None,
    income_tax_rate_pct=None,
    w2_wages=0,
    today=None,
    state=None,
    state_income_tax_rate_pct=None,
) -> dict:
    """Rough quarterly estimate from year-to-date business profit, annualized."""
    today = today or date.today()
    year, start, end = year_bounds(year, today)
    cur = cfg.currency
    state_code = _state_code(state) if state is not None else cfg.tax.state
    state_rate_raw = (
        state_income_tax_rate_pct if state_income_tax_rate_pct is not None else cfg.tax.state_income_tax_rate_pct
    )
    draft = schedule_c_draft(conn, cfg, year, today)
    ytd_profit = draft["net_profit"]["minor"]
    before = draft["tentative_profit_before_home_office"]["minor"]
    if year == today.year:
        elapsed = (today - start).days + 1
        days_in_year = (end - start).days + 1
        projected = before * days_in_year // elapsed if elapsed else before
        # The home office deduction is an annual figure: subtract it once from the annualized profit rather
        # than annualizing a year-to-date profit that already had the full-year amount taken out.
        ho = home_office_deduction(cfg, year, projected)
        projected -= ho["line_30"]["minor"] if ho else 0
    else:
        projected = ytd_profit

    rate_raw = income_tax_rate_pct if income_tax_rate_pct is not None else cfg.tax.income_tax_rate_pct
    try:
        w2 = int(Decimal(str(w2_wages or 0)) * 100)
        rate = Decimal(str(rate_raw)) if rate_raw is not None else None
    except Exception as exc:
        raise GuardError("income_tax_rate_pct and w2_wages must be numbers") from exc
    if rate is not None and not 0 <= rate <= 60:
        raise GuardError("income_tax_rate_pct must be between 0 and 60")

    # Self-employment tax (Schedule SE): 92.35% of net profit; 12.4% Social Security up to the wage base
    # (reduced by W-2 wages), 2.9% Medicare on all of it. Nothing owed below $400 of net earnings.
    net_earnings = int(Decimal(max(projected, 0)) * Decimal("0.9235"))
    base = cfg.tax.ss_wage_base.get(year)
    notes = [DISCLAIMER]
    if base is None:
        notes.append(f"No Social Security wage base configured for {year}; Social Security part assumes no cap.")
        ss_taxable = net_earnings
    else:
        ss_taxable = max(0, min(net_earnings, base * 100 - w2))
    se_tax = 0
    if net_earnings >= 40_000:
        se_tax = int(
            (Decimal(ss_taxable) * Decimal("0.124") + Decimal(net_earnings) * Decimal("0.029")).quantize(
                Decimal("1"), rounding=ROUND_HALF_EVEN
            )
        )
    income_tax = None
    taxable = max(projected - se_tax // 2, 0)  # half of SE tax is deductible
    if rate is not None:
        income_tax = int((Decimal(taxable) * rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    else:
        notes.append(
            "Income tax isn't included. Pass income_tax_rate_pct (your own planning rate) or set "
            "tax.income_tax_rate_pct in config.toml."
        )
    total = se_tax + (income_tax or 0)
    per_quarter = total // 4
    notes += [
        "Annualizes year-to-date business profit; uneven income makes this rough.",
        "Ignores Additional Medicare Tax, the QBI deduction, credits, other income, and withholding.",
        "Safe-harbor rules based on last year's total tax (100%, or 110% at higher incomes) can lower what "
        "you must pay quarterly; ask your preparer.",
    ]
    result = {
        "tax_year": year,
        "ytd_net_profit": amount(ytd_profit, cur),
        "projected_annual_net_profit": amount(projected, cur),
        "self_employment_tax": amount(se_tax, cur),
        "income_tax_estimate": amount(income_tax, cur) if income_tax is not None else None,
        "income_tax_rate_pct": str(rate) if rate is not None else None,
        "projected_total": amount(total, cur),
        "suggested_per_quarter": amount(per_quarter, cur),
        "due_dates": [{"date": d, "passed": d < today.isoformat()} for d in due_dates(year)],
        "set_aside_pct_of_profit": pct(total, projected) if projected > 0 else None,
        "notes": notes,
        "meta": meta(conn, cfg, start, min(end, today)),
    }
    if state_code is not None:
        block = _state_block(state_code, state_rate_raw, year, taxable, today, cur)
        state_total = 0 if block["no_income_tax"] else (block["income_tax_estimate"] or {}).get("minor")
        result["state"] = block
        result["projected_total_with_state"] = amount(total + state_total, cur) if state_total is not None else None
        # Federal installments are equal quarters; the state's follow its own percentages (CA is 30/40/0/30).
        state_parts = [i["amount"] for i in block["installments"]]
        have_parts = len(state_parts) == 4 and all(p is not None for p in state_parts)
        result["suggested_per_quarter_with_state"] = [
            {
                "quarter": q + 1,
                "federal_date": fed_date,
                "federal": amount(per_quarter, cur),
                "state_date": block["installments"][q]["date"] if have_parts else None,
                "state": state_parts[q] if have_parts else None,
                "total": amount(per_quarter + (state_parts[q]["minor"] if have_parts else 0), cur),
            }
            for q, fed_date in enumerate(due_dates(year))
        ]
    return result
