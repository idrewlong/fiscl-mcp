"""Local edits to categorization: apply_categories (dry run by default)."""

from __future__ import annotations

from ..categorize import append_merchant_rule
from ..common import check_bulk, check_items, count_by, plan_result, resolve_target, target_of
from ..config import Config
from ..db import get_category_id, get_or_create_category
from ..guard import GuardError, clean_text


def apply_categories(conn, cfg: Config, items: list[dict], create_rule=False, dry_run=True) -> dict:
    """Set categories by merchant or transaction id. Defaults to a dry run that changes nothing."""
    check_items(items)
    plan, total_rows = [], 0
    for i, item in enumerate(items, start=1):
        category = clean_text(str(item.get("category", "")), 60)
        if not category:
            raise GuardError(f"item {i}: category is required")
        if category.lower() == "transfers":
            raise GuardError(f"item {i}: transfers are matched automatically, not assigned")
        merchant, ids, rows = resolve_target(conn, item, i)
        total_rows += len(rows)
        plan.append(
            {
                "target": target_of(merchant, ids),
                "category": category,
                "creates_category": get_category_id(conn, category) is None,
                "matched_transactions": len(rows),
                "current_categories": count_by(rows, lambda r: r["current"]),
                "creates_rule": bool(create_rule and merchant),
                "_ids": [r["id"] for r in rows],
            }
        )
    check_bulk(total_rows)

    if not dry_run:
        for p in plan:
            cat_id = get_or_create_category(conn, p["category"])
            conn.executemany(
                "UPDATE transactions SET category_id = ?, category_source = 'manual' WHERE id = ?",
                [(cat_id, tid) for tid in p["_ids"]],
            )
            if p["creates_rule"]:
                append_merchant_rule(cfg, p["target"]["merchant"], p["category"])
    return plan_result(plan, total_rows, dry_run, "Call again with dry_run=false to apply these changes.")
