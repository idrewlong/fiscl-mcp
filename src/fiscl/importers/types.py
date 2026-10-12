from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedTxn:
    posted_date: str  # YYYY-MM-DD
    amount: int  # minor units, negative = money out
    description: str
    memo: str | None = None
    fitid: str | None = None
    pending: bool = False
    # Bank-sync extras (None/False for files): a posted row that supersedes the pending row with this fitid, and
    # a row the provider says changed since it was imported (update in place, keep the user's edits).
    replaces_fitid: str | None = None
    modified: bool = False


@dataclass
class ParsedStatement:
    format: str  # csv | ofx
    txns: list[ParsedTxn]
    account_key: str | None = None  # institution account identifier (OFX); hashed before storage
    last4: str | None = None
    type_hint: str | None = None
    institution: str | None = None
    currency: str = "USD"
    balances: list[tuple[str, int]] = field(default_factory=list)
    profile: str | None = None
    account_name: str | None = None  # from a CSV profile's `account` field, or a QIF `!Account` block
    # Counts of things the parser skipped on purpose (QIF sections, option lines, split lines). File-level counts ride
    # on the first statement only, so summing across statements never double counts.
    warnings: dict[str, int] = field(default_factory=dict)
