"""Transaction lists and context: query_transactions, list_accounts, import_status, list_uncategorized."""

from __future__ import annotations

from typing import Any

from ..common import MAX_TAG_LEN, account_id, latest_balance, meta, money_arg, scope_sql
from ..config import Config
from ..db import TXN_BASE_FROM, TXN_FROM
from ..guard import choice, clamp_limit, clean_text, date_range, like_escape
from ..money import amount

_SORTS = {
    "date_desc": "t.posted_date DESC, t.id DESC",
    "date_asc": "t.posted_date ASC, t.id ASC",
    "amount_asc": "t.amount_home ASC",
    "amount_desc": "t.amount_home DESC",
}


def query_transactions(
    conn,
    cfg: Config,
    from_=None,
    to=None,
    account=None,
    category=None,
    merchant=None,
    min_amount=None,
    max_amount=None,
    text=None,
    limit=None,
    sort="date_desc",
    scope="all",
    tag=None,
    today=None,
) -> dict:
    """Transaction rows (a split parent is one row with a `splits` array). Totals are over those parent rows.

    The category and scope filters also match a parent when any of its split children match.
    """
    start, end = date_range(from_, to, default_days=30, today=today)
    limit = clamp_limit(limit, cfg)
    sort_sql = _SORTS[choice(sort, tuple(_SORTS), "sort")]
    where = ["t.posted_date BETWEEN ? AND ?"]
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if (acct := account_id(conn, account)) is not None:
        where.append("t.account_id = ?")
        params.append(acct)
    if category:
        if category.lower() == "uncategorized":
            where.append("t.category_id IS NULL AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)")
        else:
            where.append(f"t.id IN (SELECT t.id FROM {TXN_FROM} WHERE c.name = ? COLLATE NOCASE)")
            params.append(category)
    if tag:
        where.append("t.id IN (SELECT transaction_id FROM tags WHERE tag = ?)")
        params.append(clean_text(tag, MAX_TAG_LEN).lower())
    if merchant:
        where.append("t.merchant = ? COLLATE NOCASE")
        params.append(clean_text(merchant))
    if (lo := money_arg(min_amount, cfg, "min_amount")) is not None:
        where.append("t.amount_home >= ?")  # min/max are in the home currency
        params.append(lo)
    if (hi := money_arg(max_amount, cfg, "max_amount")) is not None:
        where.append("t.amount_home <= ?")
        params.append(hi)
    if text:
        needle = f"%{like_escape(clean_text(text, 100))}%"
        where.append("(t.merchant LIKE ? ESCAPE '\\' OR t.raw_description LIKE ? ESCAPE '\\')")
        params += [needle, needle]
    s_sql, s_params = scope_sql(scope)
    if s_sql:  # evaluated per money line so a split parent matches when any child is in scope
        where.append(f"t.id IN (SELECT t.id FROM {TXN_FROM} WHERE 1 = 1 {s_sql})")
        params += s_params
    base = f"FROM {TXN_BASE_FROM} WHERE {' AND '.join(where)}"
    totals = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(t.amount_home), 0) AS net,
                   COALESCE(SUM(CASE WHEN t.amount_home < 0 THEN t.amount_home END), 0) AS outflow,
                   COALESCE(SUM(CASE WHEN t.amount_home > 0 THEN t.amount_home END), 0) AS inflow {base}""",
        params,
    ).fetchone()
    rows = conn.execute(
        f"""SELECT t.id, t.posted_date, a.name AS account, t.merchant, t.raw_description, t.memo,
                   c.name AS category, t.amount, t.amount_home, t.currency, t.is_pending, t.transfer_group, t.note
            {base} ORDER BY {sort_sql} LIMIT ?""",
        [*params, limit],
    ).fetchall()
    ids = [r["id"] for r in rows]
    currency_of = {r["id"]: r["currency"] for r in rows}
    tags_by: dict[int, list[str]] = {}
    splits_by: dict[int, list[dict]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for tr in conn.execute(
            f"SELECT transaction_id, tag FROM tags WHERE transaction_id IN ({marks}) ORDER BY tag", ids
        ):
            tags_by.setdefault(tr["transaction_id"], []).append(tr["tag"])
        for sr in conn.execute(
            f"""SELECT s.transaction_id, s.id, s.amount, s.scope, s.note, c.name AS category
                FROM splits s LEFT JOIN categories c ON c.id = s.category_id
                WHERE s.transaction_id IN ({marks}) ORDER BY s.id""",
            ids,
        ):
            part = {
                "amount": amount(sr["amount"], currency_of[sr["transaction_id"]]),
                "category": sr["category"] or "Uncategorized",
                "scope": sr["scope"],
            }
            if sr["note"]:
                part["note"] = sr["note"]
            splits_by.setdefault(sr["transaction_id"], []).append(part)
    items = []
    for r in rows:
        item = {
            "id": r["id"],
            "date": r["posted_date"],
            "account": r["account"],
            "merchant": r["merchant"],
            "category": r["category"] or "Uncategorized",
            "amount": amount(r["amount"], r["currency"]),
            "pending": bool(r["is_pending"]),
            "is_transfer": r["transfer_group"] is not None,
        }
        if r["currency"] != cfg.currency:  # home-currency rows stay exactly as before
            item["amount_home"] = amount(r["amount_home"], cfg.currency) if r["amount_home"] is not None else None
        if not cfg.guard.redact_descriptions:
            item["description"] = r["raw_description"]
            if r["memo"]:
                item["memo"] = r["memo"]
        if r["note"]:
            item["note"] = r["note"]
        if r["id"] in tags_by:
            item["tags"] = tags_by[r["id"]]
        if r["id"] in splits_by:
            item["splits"] = splits_by[r["id"]]
        items.append(item)
    return {
        "transactions": items,
        "totals": {
            "count": totals["n"],
            "net": amount(totals["net"], cfg.currency),
            "money_out": amount(totals["outflow"], cfg.currency),
            "money_in": amount(totals["inflow"], cfg.currency),
        },
        "meta": meta(
            conn, cfg, start, end, row_count=len(items), total_count=totals["n"], truncated=totals["n"] > len(items)
        ),
    }


def list_accounts(conn, cfg: Config, include_archived=False) -> dict:
    rows = conn.execute(
        """SELECT a.*, (SELECT MAX(posted_date) FROM transactions t WHERE t.account_id = a.id) AS latest,
                  (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS n
           FROM accounts a WHERE (? OR a.archived = 0) ORDER BY a.name""",
        (int(bool(include_archived)),),
    ).fetchall()
    items = []
    for r in rows:
        bal = latest_balance(conn, r["id"])
        items.append(
            {
                "name": r["name"],
                "type": r["type"],
                "institution": r["institution"],
                "last4": r["last4"],
                "currency": r["currency"],
                "is_asset": bool(r["is_asset"]),
                "archived": bool(r["archived"]),
                "latest_balance": {"amount": amount(bal["amount"], r["currency"]), "as_of": bal["as_of"]}
                if bal
                else None,
                "latest_transaction": r["latest"],
                "transaction_count": r["n"],
            }
        )
    return {"accounts": items, "meta": meta(conn, cfg, row_count=len(items))}


def import_status(conn, cfg: Config, limit=10) -> dict:
    limit = clamp_limit(limit, cfg)
    rows = conn.execute(
        """SELECT i.*, a.name AS account FROM imports i LEFT JOIN accounts a ON a.id = i.account_id
           ORDER BY i.imported_at DESC, i.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return {
        "imports": [
            {
                "file": r["filename"],
                "format": r["format"],
                "account": r["account"],
                "status": r["status"],
                "rows_seen": r["rows_seen"],
                "rows_added": r["rows_added"],
                "rows_skipped": r["rows_skipped"],
                "error": clean_text(r["error"], 200) or None,
                "imported_at": r["imported_at"],
            }
            for r in rows
        ],
        "meta": meta(conn, cfg, row_count=len(rows)),
    }


def list_uncategorized(conn, cfg: Config, from_=None, to=None, limit=None, today=None) -> dict:
    start, end = date_range(from_, to, default_days=365, today=today)
    limit = clamp_limit(limit, cfg)
    rows = conn.execute(
        """SELECT t.merchant, COUNT(*) AS n, SUM(t.amount_home) AS total, MIN(t.posted_date) AS first,
                  MAX(t.posted_date) AS last, GROUP_CONCAT(DISTINCT a.name) AS accounts
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.category_id IS NULL AND t.transfer_group IS NULL AND t.posted_date BETWEEN ? AND ?
             AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)
           GROUP BY t.merchant ORDER BY ABS(SUM(t.amount_home)) DESC""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    cats = [r[0] for r in conn.execute("SELECT name FROM categories WHERE kind != 'transfer' ORDER BY name")]
    return {
        "merchants": [
            {
                "merchant": r["merchant"],
                "count": r["n"],
                "total": amount(r["total"], cfg.currency),
                "first_date": r["first"],
                "last_date": r["last"],
                "accounts": (r["accounts"] or "").split(","),
            }
            for r in rows[:limit]
        ],
        "uncategorized_transactions": sum(r["n"] for r in rows),
        "existing_categories": cats,
        "meta": meta(
            conn, cfg, start, end, row_count=min(len(rows), limit), total_count=len(rows), truncated=len(rows) > limit
        ),
    }
