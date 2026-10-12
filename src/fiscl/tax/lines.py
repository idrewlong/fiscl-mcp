"""Schedule C line tables and the disclaimer every tax response carries."""

from __future__ import annotations

DISCLAIMER = (
    "This organizes your own records for you and your tax preparer. It is not tax advice, and "
    "whether something is deductible depends on facts only you and a professional can confirm."
)

# Official Schedule C (Form 1040) Part II line names (2025 form; recheck when the 2026 form is released).
SCHEDULE_C_LINES = {
    "1": "Gross receipts or sales",
    "8": "Advertising",
    "9": "Car and truck expenses",
    "10": "Commissions and fees",
    "11": "Contract labor",
    "12": "Depletion",
    "13": "Depreciation and section 179 expense deduction",
    "14": "Employee benefit programs",
    "15": "Insurance (other than health)",
    "16a": "Interest: Mortgage (paid to banks, etc.)",
    "16b": "Interest: Other",
    "17": "Legal and professional services",
    "18": "Office expense",
    "19": "Pension and profit-sharing plans",
    "20a": "Rent or lease: Vehicles, machinery, and equipment",
    "20b": "Rent or lease: Other business property",
    "21": "Repairs and maintenance",
    "22": "Supplies",
    "23": "Taxes and licenses",
    "24a": "Travel",
    "24b": "Deductible meals",
    "25": "Utilities",
    "26": "Wages (less employment credits)",
    "27b": "Other expenses (from line 48)",
}
LINE_ORDER = list(SCHEDULE_C_LINES)

# When a business-scope transaction sits in an everyday category, this is the line it most likely
# belongs on. Categories with an explicit schedule_c_line always win.
CATEGORY_DEFAULT_LINES = {
    "utilities": "25",
    "insurance": "15",
    "travel": "24a",
    "dining": "24b",
    "auto: fuel": "9",
    "auto: maintenance": "9",
    "transport": "24a",
    "subscriptions": "27b",
    "education": "27b",
    "fees & interest": "27b",
    "taxes": "23",
    "shopping": "22",
}


def line_for(category: str | None, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return CATEGORY_DEFAULT_LINES.get((category or "").lower())
