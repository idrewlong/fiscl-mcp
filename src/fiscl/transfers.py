"""Match money moving between your own accounts so it doesn't count as spending or income."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import date

from .config import Config
from .db import get_or_create_category

HINT = re.compile(
    r"transfer|xfer|payment thank you|autopay|online pmt|online payment|card payment|"
    r"epay|ach pmt|to savings|from savings|to checking|from checking",
    re.IGNORECASE,
)


def match_transfers(conn: sqlite3.Connection, cfg: Config) -> int:
    """Pair opposite, equal amounts in different accounts within the window.

    A pair only matches when at least one side looks like a transfer (description hint) or one
    account is a credit card or loan, to avoid pairing unrelated $50 charges and deposits.
    Returns the number of pairs matched.
    """
    rows = conn.execute(
        """SELECT t.id, t.account_id, t.posted_date, t.amount, t.raw_description, t.category_source,
                  a.type AS account_type
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.transfer_group IS NULL AND t.is_pending = 0 AND t.category_source != 'manual'
           ORDER BY t.posted_date, t.id"""
    ).fetchall()
    incoming: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["amount"] > 0:
            incoming[r["amount"]].append(r)

    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    window = cfg.transfer_window_days
    for out in rows:
        if out["amount"] >= 0 or out["id"] in used:
            continue
        out_day = date.fromisoformat(out["posted_date"])
        best, best_score = None, None
        for cand in incoming.get(-out["amount"], []):
            if cand["id"] in used or cand["account_id"] == out["account_id"]:
                continue
            gap = abs((date.fromisoformat(cand["posted_date"]) - out_day).days)
            if gap > window:
                continue
            hinted = bool(HINT.search(out["raw_description"]) or HINT.search(cand["raw_description"]))
            debt_side = "credit" in (out["account_type"], cand["account_type"]) or "loan" in (
                out["account_type"],
                cand["account_type"],
            )
            if not (hinted or debt_side):
                continue
            score = (0 if hinted else 1, gap)
            if best_score is None or score < best_score:
                best, best_score = cand, score
        if best is not None:
            used.update((out["id"], best["id"]))
            pairs.append((out["id"], best["id"]))

    if pairs:
        cat_id = get_or_create_category(conn, "Transfers")
        for a, b in pairs:
            group = uuid.uuid4().hex[:12]
            conn.execute(
                """UPDATE transactions SET transfer_group = ?, category_id = ?, category_source = 'transfer'
                   WHERE id IN (?, ?)""",
                (group, cat_id, a, b),
            )
    return len(pairs)
