"""Reporting on detected recurring items (detection itself lives in fiscl.recurring)."""

from __future__ import annotations

from ..common import meta
from ..config import Config
from ..guard import choice
from ..money import amount

PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "annual": 1}


def list_recurring(conn, cfg: Config, status=None, kind=None, min_confidence=0.6, today=None) -> dict:
    where, params = ["r.confidence >= ?"], [float(min_confidence)]
    if status:
        choice(status, ("active", "price_changed", "missed", "cancelled"), "status")
        where.append("r.status = ?")
        params.append(status)
    if kind:
        choice(kind, ("subscription", "bill", "income"), "kind")
        where.append("r.kind = ?")
        params.append(kind)
    rows = conn.execute(
        f"""SELECT r.*, a.name AS account FROM recurring r LEFT JOIN accounts a ON a.id = r.account_id
            WHERE {" AND ".join(where)} ORDER BY r.direction, r.typical_amount DESC""",
        params,
    ).fetchall()
    items, annual_out, annual_in = [], 0, 0
    for r in rows:
        yearly = r["last_amount"] * PER_YEAR[r["cadence"]]
        if r["status"] != "cancelled":
            if r["direction"] == "out":
                annual_out += yearly
            else:
                annual_in += yearly
        items.append(
            {
                "merchant": r["merchant"],
                "account": r["account"],
                "kind": r["kind"],
                "cadence": r["cadence"],
                "typical_amount": amount(r["typical_amount"], cfg.currency),
                "last_amount": amount(r["last_amount"], cfg.currency),
                "last_date": r["last_date"],
                "next_expected": r["next_expected"],
                "status": r["status"],
                "price_change": amount(r["last_amount"] - r["typical_amount"], cfg.currency)
                if r["status"] == "price_changed"
                else None,
                "annualized": amount(yearly, cfg.currency),
                "occurrences": r["occurrences"],
                "confidence": r["confidence"],
            }
        )
    return {
        "recurring": items,
        "totals": {
            "annual_recurring_spending": amount(annual_out, cfg.currency),
            "annual_recurring_income": amount(annual_in, cfg.currency),
            "price_changes": sum(1 for i in items if i["status"] == "price_changed"),
            "missed": sum(1 for i in items if i["status"] == "missed"),
        },
        "meta": meta(
            conn,
            cfg,
            row_count=len(items),
            note="Annualized totals use the most recent amount and exclude cancelled items.",
        ),
    }
