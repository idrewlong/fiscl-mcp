"""One-page tax-year summary for the fiscl://tax/{year} resource: drafts per business, the estimate, open questions,
and 1099 status. Read-only; everything here comes from the tools it calls, trimmed to headline numbers."""

from __future__ import annotations

from datetime import date

from ..common import meta, year_bounds
from ..config import Config
from .businesses import business_view, default_business
from .estimates import estimated_tax
from .export import existing_exports
from .forms1099 import reconcile_1099
from .lines import DISCLAIMER
from .review import review_writeoffs
from .schedule_c import schedule_c_draft


def tax_year_summary(conn, cfg: Config, year=None, today=None) -> dict:
    today = today or date.today()
    year, start, end = year_bounds(year, today)
    sc = schedule_c_draft(conn, cfg, year, today)
    drafts = sc["businesses"] if "businesses" in sc else [{"business": business_view(default_business(conn)), **sc}]
    businesses = [
        {
            "business": d["business"],
            "gross_receipts": d["gross_receipts"],
            "total_expenses": d["total_expenses"],
            "home_office_line_30": d["home_office"]["line_30"] if d["home_office"] else None,
            "net_profit": d["net_profit"],
            "unmapped_categories": len(d["unmapped_business_spending"]),
        }
        for d in drafts
    ]
    combined = (
        sc["combined"] if "combined" in sc else {k: sc[k] for k in ("gross_receipts", "total_expenses", "net_profit")}
    )
    estimate = estimated_tax(conn, cfg, year=year, today=today)
    estimate.pop("meta")
    review = review_writeoffs(conn, cfg, year=year, today=today)
    forms = reconcile_1099(conn, cfg, tax_year=year, today=today)
    return {
        "tax_year": year,
        "disclaimer": DISCLAIMER,
        "schedule_c": {"businesses": businesses, "combined": combined},
        "estimated_tax": estimate,
        "review": {
            "open_questions": review["open_questions"],
            "sections": {name: len(rows) for name, rows in review["sections"].items()},
        },
        "forms_1099": {
            "count": len(forms["forms"]),
            "mismatches": sum(1 for f in forms["forms"] if f["status"] != "matches"),
            "unreported_payers": forms["unreported_payers"],
        },
        "exports": existing_exports(cfg, year),
        "meta": meta(conn, cfg, start, end),
    }
