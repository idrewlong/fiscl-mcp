"""Business tax helpers for sole proprietors: Schedule C draft, mileage, estimated tax, write-off review.

Everything here organizes the user's own data for them and their tax preparer. Nothing here decides
what is deductible. Every response carries that framing.

`fiscl.tax.<name>` stays the stable import path; each area lives in its own module.
"""

from __future__ import annotations

from .edits import log_trip, mileage_log, set_scope
from .estimates import due_dates, estimated_tax
from .export import export_schedule_c
from .home_office import home_office_deduction
from .lines import CATEGORY_DEFAULT_LINES, DISCLAIMER, LINE_ORDER, SCHEDULE_C_LINES, line_for
from .review import DE_MINIMIS_MINOR, review_writeoffs
from .schedule_c import mileage_rate, mileage_summary, schedule_c_draft

__all__ = [
    "CATEGORY_DEFAULT_LINES",
    "DE_MINIMIS_MINOR",
    "DISCLAIMER",
    "LINE_ORDER",
    "SCHEDULE_C_LINES",
    "due_dates",
    "estimated_tax",
    "export_schedule_c",
    "home_office_deduction",
    "line_for",
    "log_trip",
    "mileage_log",
    "mileage_rate",
    "mileage_summary",
    "review_writeoffs",
    "schedule_c_draft",
    "set_scope",
]
