"""Rough quarterly estimated tax from year-to-date business profit."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from ..common import meta, year_bounds
from ..config import Config
from ..guard import GuardError
from ..money import amount, pct
from .lines import DISCLAIMER
from .schedule_c import schedule_c_draft


def due_dates(year: int) -> list[str]:
    known = {2026: ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"]}
    return known.get(year, [f"{year}-04-15", f"{year}-06-15", f"{year}-09-15", f"{year + 1}-01-15"])


def estimated_tax(conn, cfg: Config, year=None, income_tax_rate_pct=None, w2_wages=0, today=None) -> dict:
    """Rough quarterly estimate from year-to-date business profit, annualized."""
    today = today or date.today()
    year, start, end = year_bounds(year, today)
    cur = cfg.currency
    draft = schedule_c_draft(conn, cfg, year, today)
    ytd_profit = draft["tentative_net_profit"]["minor"]
    if year == today.year:
        elapsed = (today - start).days + 1
        days_in_year = (end - start).days + 1
        projected = ytd_profit * days_in_year // elapsed if elapsed else ytd_profit
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
    if rate is not None:
        taxable = max(projected - se_tax // 2, 0)  # half of SE tax is deductible
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
    return {
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
