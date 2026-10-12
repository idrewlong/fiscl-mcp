"""Local edits to the user's own data: categories, notes, tags, splits, dismissed recurring items.

Every writer here defaults to a dry run and returns `common.plan_result`; nothing leaves the machine.
"""

from __future__ import annotations

from ..categorize import append_merchant_rule
from ..common import (
    MAX_TAGS_PER_ITEM,
    check_bulk,
    check_items,
    clean_tag,
    count_by,
    meta,
    money_arg,
    plan_result,
    resolve_target,
    target_of,
    valid_id,
    without_split_parents,
)
from ..config import Config
from ..db import get_category_id, get_or_create_category
from ..guard import GuardError, choice, clean_text
from ..money import amount

MAX_NOTE = 500
MAX_SPLIT_PARTS = 20


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
        rows, skipped = without_split_parents(rows)
        total_rows += len(rows)
        plan.append(
            {
                "target": target_of(merchant, ids),
                **skipped,
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
    result = plan_result(plan, total_rows, dry_run, "Call again with dry_run=false to apply these changes.")
    return _with_meta(conn, cfg, result)


def _with_meta(conn, cfg: Config, result: dict) -> dict:
    result["meta"] = meta(conn, cfg)
    return result


def _transaction(conn, transaction_id) -> dict:
    valid_id(transaction_id)
    row = conn.execute(
        "SELECT id, posted_date, merchant, amount, currency, transfer_group, note FROM transactions WHERE id = ?",
        (transaction_id,),
    ).fetchone()
    if row is None:
        raise GuardError(f"no transaction with id {transaction_id}", code="not_found")
    return dict(row)


def add_note(conn, cfg: Config, transaction_id=None, note=None, dry_run=True) -> dict:
    """Set or clear (note=null) the free-text note on one transaction."""
    txn = _transaction(conn, transaction_id)
    new = None
    if note is not None:
        new = clean_text(str(note), 10_000) or None  # a blank note clears
        if new and len(new) > MAX_NOTE:
            raise GuardError(f"note is longer than {MAX_NOTE} characters")
    plan = [
        {
            "transaction_id": txn["id"],
            "date": txn["posted_date"],
            "merchant": txn["merchant"],
            "old_note": txn["note"],
            "new_note": new,
        }
    ]
    if not dry_run:
        conn.execute("UPDATE transactions SET note = ? WHERE id = ?", (new, txn["id"]))
    return _with_meta(conn, cfg, plan_result(plan, 1, dry_run, "Call again with dry_run=false to save this note."))


def tag_transactions(conn, cfg: Config, items: list[dict], dry_run=True) -> dict:
    """Add and remove tags by merchant or transaction id. Tags are lowercased, at most 40 characters."""
    check_items(items)
    plan, total_rows = [], 0
    for i, item in enumerate(items, start=1):
        where = f"item {i}"
        lists: dict[str, list[str]] = {}
        for key in ("add", "remove"):
            raw = item.get(key) or []
            if not isinstance(raw, list):
                raise GuardError(f"{where}: {key} must be a list of tags")
            if len(raw) > MAX_TAGS_PER_ITEM:
                raise GuardError(f"{where}: at most {MAX_TAGS_PER_ITEM} tags in {key}")
            lists[key] = list(dict.fromkeys(clean_tag(t, where) for t in raw))
        if not lists["add"] and not lists["remove"]:
            raise GuardError(f"{where}: give at least one tag in add or remove")
        if set(lists["add"]) & set(lists["remove"]):
            raise GuardError(f"{where}: a tag can't be in both add and remove")
        merchant, ids, rows = resolve_target(conn, item, i)
        found = [r["id"] for r in rows]
        total_rows += len(found)
        counts: dict[str, int] = {}
        if found:
            marks = ",".join("?" * len(found))
            counts = {
                r["tag"]: r["n"]
                for r in conn.execute(
                    f"SELECT tag, COUNT(*) AS n FROM tags WHERE transaction_id IN ({marks}) GROUP BY tag", found
                )
            }
        after = dict(counts)
        if found:
            for tag in lists["add"]:
                after[tag] = len(found)
        for tag in lists["remove"]:
            after.pop(tag, None)
        plan.append(
            {
                "target": target_of(merchant, ids),
                "add": lists["add"],
                "remove": lists["remove"],
                "matched_transactions": len(found),
                "tags_after": dict(sorted(after.items())),  # tag -> how many matched transactions carry it
                "_ids": found,
            }
        )
    check_bulk(total_rows)

    if not dry_run:
        for p in plan:
            conn.executemany(
                "INSERT OR IGNORE INTO tags (transaction_id, tag) VALUES (?, ?)",
                [(tid, tag) for tid in p["_ids"] for tag in p["add"]],
            )
            conn.executemany(
                "DELETE FROM tags WHERE transaction_id = ? AND tag = ?",
                [(tid, tag) for tid in p["_ids"] for tag in p["remove"]],
            )
    return _with_meta(conn, cfg, plan_result(plan, total_rows, dry_run, "Call again with dry_run=false to apply."))


def dismiss_recurring(conn, cfg: Config, merchant=None, account=None, direction=None, undo=False, dry_run=True) -> dict:
    """Hide a detected recurring item so refreshes keep it hidden. undo=true restores it at the next refresh."""
    merchant = clean_text(str(merchant or ""), 120)
    if not merchant:
        raise GuardError("merchant is required")
    where, params = ["merchant = ? COLLATE NOCASE"], [merchant]
    if account:
        acct = conn.execute(
            "SELECT id FROM accounts WHERE name = ? COLLATE NOCASE", (clean_text(account, 120),)
        ).fetchone()
        if acct is None:
            raise GuardError(f"unknown account {account!r}")
        where.append("account_id = ?")
        params.append(acct["id"])
    if direction is not None:
        where.append("direction = ?")
        params.append(choice(direction, ("out", "in"), "direction"))
    clause = " AND ".join(where)

    if undo:
        rows = conn.execute(
            f"SELECT merchant, account_id, direction FROM recurring_dismissed WHERE {clause}", params
        ).fetchall()
        if not rows:
            raise GuardError(f"{merchant!r} isn't dismissed (check merchant, account, direction)")
    else:
        rows = conn.execute(
            f"SELECT id, merchant, account_id, direction FROM recurring WHERE {clause}", params
        ).fetchall()
        if not rows:
            raise GuardError(f"no detected recurring item for {merchant!r}; check detect_recurring for the merchant")
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM accounts")}
    plan = [
        {
            "merchant": r["merchant"],
            "account": names.get(r["account_id"]),
            "direction": r["direction"],
            "action": "restore" if undo else "dismiss",
        }
        for r in rows
    ]
    if not dry_run:
        keys = [(r["merchant"], r["account_id"], r["direction"]) for r in rows]
        if undo:
            conn.executemany(
                "DELETE FROM recurring_dismissed WHERE merchant = ? AND account_id IS ? AND direction = ?", keys
            )
        else:
            conn.executemany(
                "INSERT OR IGNORE INTO recurring_dismissed (merchant, account_id, direction) VALUES (?, ?, ?)", keys
            )
            conn.executemany("DELETE FROM recurring WHERE id = ?", [(r["id"],) for r in rows])
    step = "Call again with dry_run=false to apply."
    if undo:
        step += " The item reappears at the next import or recurring refresh."
    return _with_meta(conn, cfg, plan_result(plan, 0, dry_run, step))


def _split_parts(parts, parent: dict, cfg: Config) -> list[dict]:
    if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_SPLIT_PARTS:
        raise GuardError(f"parts needs 2 to {MAX_SPLIT_PARTS} entries (or an empty list to remove the split)")
    out = []
    negative = parent["amount"] < 0
    for i, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise GuardError(f"part {i}: must be an object with amount and category")
        minor = money_arg(part.get("amount"), cfg, f"part {i} amount")
        if not minor:
            raise GuardError(f"part {i}: amount is required and can't be zero")
        if (minor < 0) != negative:
            example = amount(-abs(minor) if negative else abs(minor), cfg.currency)["display"]
            raise GuardError(f"part {i}: amount must have the same sign as the transaction (e.g. {example})")
        category = clean_text(str(part.get("category") or ""), 60)
        if not category:
            raise GuardError(f"part {i}: category is required")
        if category.lower() == "transfers":
            raise GuardError(f"part {i}: transfers are matched automatically, not assigned")
        scope = part.get("scope")
        if scope is not None:
            choice(scope, ("personal", "business"), f"part {i} scope")
        note = clean_text(str(part["note"]), 200) if part.get("note") else None
        out.append({"minor": minor, "category": category, "scope": scope, "note": note})
    total = sum(p["minor"] for p in out)
    if total != parent["amount"]:
        raise GuardError(
            f"parts sum to {amount(total, cfg.currency)['display']} but the transaction is "
            f"{amount(parent['amount'], cfg.currency)['display']}; they must match exactly",
            code="sum_mismatch",
        )
    return out


def split_transaction(conn, cfg: Config, transaction_id=None, parts=None, dry_run=True) -> dict:
    """Split one transaction into categorized parts that add up exactly (parts=[] removes the split)."""
    parent = _transaction(conn, transaction_id)
    if parent["transfer_group"] is not None:
        raise GuardError("transfers between your own accounts can't be split")
    existing = conn.execute("SELECT COUNT(*) FROM splits WHERE transaction_id = ?", (parent["id"],)).fetchone()[0]
    if parts == []:
        if not existing:
            raise GuardError("this transaction isn't split")
        new_parts: list[dict] = []
    else:
        new_parts = _split_parts(parts, parent, cfg)
    plan = [
        {
            "transaction_id": parent["id"],
            "date": parent["posted_date"],
            "merchant": parent["merchant"],
            "amount": amount(parent["amount"], cfg.currency),
            "action": "split" if new_parts else "remove_split",
            "replaces_existing_split": bool(existing),
            "parts": [
                {
                    "amount": amount(p["minor"], cfg.currency),
                    "category": p["category"],
                    "creates_category": get_category_id(conn, p["category"]) is None,
                    "scope": p["scope"],
                    "note": p["note"],
                }
                for p in new_parts
            ],
        }
    ]
    if not dry_run:
        conn.execute("DELETE FROM splits WHERE transaction_id = ?", (parent["id"],))
        for p in new_parts:
            conn.execute(
                "INSERT INTO splits (transaction_id, amount, category_id, scope, note) VALUES (?, ?, ?, ?, ?)",
                (parent["id"], p["minor"], get_or_create_category(conn, p["category"]), p["scope"], p["note"]),
            )
    return _with_meta(conn, cfg, plan_result(plan, 1, dry_run, "Call again with dry_run=false to apply."))
