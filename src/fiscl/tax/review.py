"""Write-off review: questions to confirm with the user and their preparer, never conclusions."""

from __future__ import annotations

import re

from ..common import meta, year_bounds
from ..config import Config
from ..db import SCOPE_SQL, SPENDING_FILTER, TXN_FROM
from ..money import amount
from .lines import DISCLAIMER, line_for

# Merchants that are often business expenses for freelancers and creative/technical work.
# Used only to raise questions in review_writeoffs, never to classify.
_BUSINESSY = re.compile(
    r"adobe|google (workspace|cloud|ads)|gsuite|github|gitlab|aws|amazon web services|digitalocean|"
    r"vercel|netlify|heroku|cloudflare|figma|notion|slack|zoom|dropbox|canva|squarespace|wix|godaddy|"
    r"namecheap|openai|anthropic|jetbrains|microsoft 365|office 365|quickbooks|freshbooks|wave|"
    r"mailchimp|hubspot|calendly|docusign|linkedin|upwork|fiverr|b&h|bh photo|adorama|dji|"
    r"staples|office depot|officemax|fedex|ups store|usps|wework|regus|coworking|apple\.com|best buy|"
    r"semrush|ahrefs|sanity|github sponsors",
    re.IGNORECASE,
)
DE_MINIMIS_MINOR = 250_000  # $2,500 de minimis safe harbor per item/invoice (no applicable financial statement)


def review_writeoffs(conn, cfg: Config, year=None, limit=25, today=None) -> dict:
    """Questions to confirm, not conclusions: things that might be business expenses or need attention."""
    year, start, end = year_bounds(year, today)
    cur = cfg.currency
    span = (start.isoformat(), end.isoformat())
    limit = max(1, min(int(limit), 100))
    sections: dict[str, list] = {}

    personal = conn.execute(
        f"""SELECT t.merchant, COUNT(DISTINCT t.id) AS n, -SUM(t.amount) AS spent, MAX(t.posted_date) AS last,
                   GROUP_CONCAT(DISTINCT c.name) AS cats
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'personal' AND {SPENDING_FILTER}
            GROUP BY t.merchant ORDER BY spent DESC""",
        span,
    ).fetchall()
    sections["possibly_business_in_personal"] = [
        {
            "merchant": r["merchant"],
            "count": r["n"],
            "spent": amount(r["spent"], cur),
            "last_date": r["last"],
            "categories": (r["cats"] or "Uncategorized").split(","),
            "question": "Was any of this for your business? If so, mark it business with set_scope.",
        }
        for r in personal
        if _BUSINESSY.search(r["merchant"] or "")
    ][:limit]

    biz = conn.execute(
        f"""SELECT t.id, t.posted_date, t.merchant, t.amount, c.name AS category, c.schedule_c_line AS line
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' AND {SPENDING_FILTER}
            ORDER BY t.amount""",
        span,
    ).fetchall()
    sections["business_needs_category"] = [
        {
            "id": r["id"],
            "date": r["posted_date"],
            "merchant": r["merchant"],
            "amount": amount(r["amount"], cur),
            "category": r["category"] or "Uncategorized",
            "question": "Which Schedule C category does this belong in?",
        }
        for r in biz
        if line_for(r["category"], r["line"]) in (None, "1")
    ][:limit]
    sections["large_business_purchases"] = [
        {
            "id": r["id"],
            "date": r["posted_date"],
            "merchant": r["merchant"],
            "amount": amount(r["amount"], cur),
            "question": "Over the $2,500 de minimis safe harbor. Is this equipment that should be depreciated or "
            "expensed under Section 179? Ask your preparer.",
        }
        for r in biz
        if -r["amount"] > DE_MINIMIS_MINOR
    ][:limit]
    meals = [r for r in biz if line_for(r["category"], r["line"]) == "24b"]
    if meals:
        sections["business_meals"] = [
            {
                "count": len(meals),
                "total": amount(-sum(r["amount"] for r in meals), cur),
                "question": "Business meals are usually 50% deductible and need a record of who attended and the "
                "business purpose. Do you have those notes?",
            }
        ]

    mixed = conn.execute(
        """SELECT a.name, COUNT(DISTINCT t.id) AS n FROM txn_lines t JOIN accounts a ON a.id = t.account_id
           WHERE a.scope = 'mixed' AND t.scope IS NULL AND t.transfer_group IS NULL
             AND t.posted_date BETWEEN ? AND ? GROUP BY a.id""",
        span,
    ).fetchall()
    sections["mixed_account_unassigned"] = [
        {
            "account": r["name"],
            "transactions": r["n"],
            "question": "This account mixes business and personal. Mark the business ones with set_scope.",
        }
        for r in mixed
    ]

    income = conn.execute(
        f"""SELECT t.merchant, COUNT(DISTINCT t.id) AS n, SUM(t.amount) AS total, GROUP_CONCAT(DISTINCT c.name) AS cats
            FROM {TXN_FROM}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'personal' AND t.transfer_group IS NULL
              AND t.amount > 0 AND COALESCE(c.name, '') NOT IN ('Income: Salary', 'Income: Interest')
              AND COALESCE(c.kind, 'income') = 'income'
            GROUP BY t.merchant ORDER BY total DESC""",
        span,
    ).fetchall()
    sections["income_to_confirm"] = [
        {
            "merchant": r["merchant"],
            "count": r["n"],
            "total": amount(r["total"], cur),
            "categories": (r["cats"] or "Uncategorized").split(","),
            "question": "Is this business income (client payments, sales) or personal "
            "(refunds, gifts, reimbursements)?",
        }
        for r in income
    ][:limit]

    vehicle = [r for r in biz if line_for(r["category"], r["line"]) == "9"]
    trips = conn.execute("SELECT COUNT(*) FROM trips WHERE trip_date BETWEEN ? AND ?", span).fetchone()[0]
    if vehicle and trips:
        sections["vehicle_method"] = [
            {
                "question": "You have both business vehicle costs and a mileage log. "
                "Generally one method applies per vehicle. Which will you use?"
            }
        ]
    elif vehicle and not trips:
        sections["vehicle_method"] = [
            {
                "question": "You have business vehicle costs but no mileage log. A contemporaneous log is "
                "needed either way; log trips with log_trip."
            }
        ]

    open_questions = sum(len(v) for v in sections.values())
    return {
        "tax_year": year,
        "open_questions": open_questions,
        "sections": {k: v for k, v in sections.items() if v},
        "notes": [DISCLAIMER],
        "meta": meta(conn, cfg, start, end, row_count=open_questions),
    }
