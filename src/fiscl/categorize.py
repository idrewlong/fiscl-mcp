"""Deterministic rules engine. First match wins; manual categorizations are never overwritten."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .config import Config, append_toml_block, load_rules_raw, toml_string
from .db import get_or_create_category
from .guard import GuardError, safe_regex
from .money import AmountError, to_minor

MATCHERS = {"merchant", "description_regex", "account", "amount_gt", "amount_lt", "amount_eq", "day_of_month"}


@dataclass
class Rule:
    index: int
    category: str
    merchant: str | None = None
    description_regex: re.Pattern | None = None
    account: str | None = None
    amount_gt: int | None = None
    amount_lt: int | None = None
    amount_eq: int | None = None
    day_of_month: int | None = None

    def matches(self, txn: dict) -> bool:
        if self.merchant is not None and (txn.get("merchant") or "").lower() != self.merchant.lower():
            return False
        if self.description_regex is not None and not self.description_regex.search(txn.get("raw_description") or ""):
            return False
        if self.account is not None and (txn.get("account_name") or "").lower() != self.account.lower():
            return False
        amt = txn.get("amount", 0)
        if self.amount_gt is not None and not amt > self.amount_gt:
            return False
        if self.amount_lt is not None and not amt < self.amount_lt:
            return False
        if self.amount_eq is not None and amt != self.amount_eq:
            return False
        return not (self.day_of_month is not None and int(txn["posted_date"][8:10]) != self.day_of_month)


def load_rules(cfg: Config) -> list[Rule]:
    raw = load_rules_raw(cfg)
    rules: list[Rule] = []
    for i, r in enumerate(raw.get("rule", []), start=1):
        if not isinstance(r, dict) or "category" not in r:
            raise GuardError(f"rules.toml rule {i} needs a category")
        unknown = set(r) - MATCHERS - {"category"}
        if unknown:
            raise GuardError(f"rules.toml rule {i}: unknown keys {sorted(unknown)}")
        if not set(r) & MATCHERS:
            raise GuardError(f"rules.toml rule {i}: needs at least one matcher")
        try:
            rule = Rule(
                index=i,
                category=str(r["category"]),
                merchant=r.get("merchant"),
                description_regex=safe_regex(r["description_regex"]) if "description_regex" in r else None,
                account=r.get("account"),
                amount_gt=to_minor(r["amount_gt"], cfg.currency) if "amount_gt" in r else None,
                amount_lt=to_minor(r["amount_lt"], cfg.currency) if "amount_lt" in r else None,
                amount_eq=to_minor(r["amount_eq"], cfg.currency) if "amount_eq" in r else None,
                day_of_month=int(r["day_of_month"]) if "day_of_month" in r else None,
            )
        except (AmountError, ValueError) as exc:
            raise GuardError(f"rules.toml rule {i}: {exc}") from exc
        rules.append(rule)
    return rules


def first_match(rules: list[Rule], txn: dict) -> Rule | None:
    return next((r for r in rules if r.matches(txn)), None)


def apply_rules(conn: sqlite3.Connection, cfg: Config, recategorize: bool = False, dry_run: bool = False) -> dict:
    """Categorize by rules. Default touches only uncategorized rows; `recategorize` also redoes
    rule-assigned rows. Manual and transfer categorizations are never changed."""
    rules = load_rules(cfg)
    sources = ("none", "rule") if recategorize else ("none",)
    rows = conn.execute(
        f"""SELECT t.id, t.merchant, t.raw_description, t.amount, t.posted_date, t.category_id,
                   a.name AS account_name
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE t.category_source IN ({",".join("?" * len(sources))}) AND t.transfer_group IS NULL""",
        sources,
    ).fetchall()
    changes: dict[str, int] = {}
    updates: list[tuple[int, int]] = []
    for row in rows:
        rule = first_match(rules, dict(row))
        if rule is None:
            continue
        cat_id = get_or_create_category(conn, rule.category) if not dry_run else None
        if not dry_run and cat_id == row["category_id"]:
            continue
        changes[rule.category] = changes.get(rule.category, 0) + 1
        if not dry_run:
            updates.append((cat_id, row["id"]))
    conn.executemany("UPDATE transactions SET category_id = ?, category_source = 'rule' WHERE id = ?", updates)
    return {"examined": len(rows), "categorized": sum(changes.values()), "by_category": changes}


def explain(cfg: Config, description: str, merchant: str, amount: int = -100, account: str = "") -> dict:
    rules = load_rules(cfg)
    txn = {
        "merchant": merchant,
        "raw_description": description,
        "amount": amount,
        "account_name": account,
        "posted_date": "2000-01-01",
    }
    rule = first_match(rules, txn)
    return {"merchant": merchant, "rule": rule.index if rule else None, "category": rule.category if rule else None}


def append_merchant_rule(cfg: Config, merchant: str, category: str) -> None:
    block = f"[[rule]]\nmerchant = {toml_string(merchant)}\ncategory = {toml_string(category)}\n"
    append_toml_block(cfg.rules_file, block, tool="apply_categories")
