"""Mapping-profile driven CSV import."""

from __future__ import annotations

import csv
import io
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import FILE_MODE, Config
from ..guard import clean_text
from ..money import AmountError, parse_amount
from .types import ParsedStatement, ParsedTxn

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y")


class CSVImportError(ValueError):
    pass


@dataclass
class Profile:
    name: str
    match_headers: list[str]
    date_column: str
    description_column: str
    amount_column: str | None = None
    debit_column: str | None = None
    credit_column: str | None = None
    memo_column: str | None = None
    balance_column: str | None = None
    status_column: str | None = None  # rows whose status is "pending" are flagged pending
    date_format: str | None = None
    amount_sign: str = "as_is"  # as_is | invert
    skip_rows: int = 0
    account: str | None = None

    @classmethod
    def from_dict(cls, data: dict, source: str) -> Profile:
        try:
            p = cls(**data)
        except TypeError as exc:
            raise CSVImportError(f"profile {source}: {exc}") from exc
        if not p.amount_column and not (p.debit_column or p.credit_column):
            raise CSVImportError(f"profile {source}: needs amount_column or debit_column/credit_column")
        if p.amount_sign not in ("as_is", "invert"):
            raise CSVImportError(f"profile {source}: amount_sign must be as_is or invert")
        return p


# Built-in profiles. Bank export formats change; verify against your own file and add a user
# profile in ~/fiscl/profiles/ if one of these is wrong. User profiles are checked first.
BUILTIN_PROFILES = [
    Profile(
        name="chase-checking",
        match_headers=["Details", "Posting Date", "Description", "Amount", "Type", "Balance"],
        date_column="Posting Date",
        description_column="Description",
        amount_column="Amount",
        balance_column="Balance",
        date_format="%m/%d/%Y",
    ),
    Profile(
        name="chase-credit",
        match_headers=["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount"],
        date_column="Post Date",
        description_column="Description",
        amount_column="Amount",
        memo_column="Memo",
        date_format="%m/%d/%Y",
    ),
    Profile(
        name="generic-debit-credit",
        match_headers=["Date", "Description", "Debit", "Credit"],
        date_column="Date",
        description_column="Description",
        debit_column="Debit",
        credit_column="Credit",
    ),
    Profile(
        name="generic",
        match_headers=["Date", "Description", "Amount"],
        date_column="Date",
        description_column="Description",
        amount_column="Amount",
    ),
]


def load_profiles(cfg: Config) -> list[Profile]:
    profiles: list[Profile] = []
    if cfg.profiles_dir.exists():
        for path in sorted(cfg.profiles_dir.glob("*.toml")):
            with path.open("rb") as fh:
                profiles.append(Profile.from_dict(tomllib.load(fh), path.name))
    return profiles + BUILTIN_PROFILES


def _norm(h: str) -> str:
    return h.strip().strip("﻿").lower()


def _read_rows(text: str, skip_rows: int) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()[skip_rows:]
    reader = csv.reader(io.StringIO("\n".join(lines)))
    try:
        header = [h.strip().strip("﻿") for h in next(reader)]
    except StopIteration as exc:
        raise CSVImportError("file is empty") from exc
    rows = []
    for raw in reader:
        if not any(cell.strip() for cell in raw):
            continue
        rows.append({header[i]: raw[i] if i < len(raw) else "" for i in range(len(header))})
    return header, rows


def match_profile(text: str, profiles: list[Profile]) -> Profile | None:
    for p in profiles:
        try:
            header, _ = _read_rows(text, p.skip_rows)
        except CSVImportError:
            continue
        have = {_norm(h) for h in header}
        if all(_norm(h) in have for h in p.match_headers):
            return p
    return None


def _parse_date(value: str, fmt: str | None) -> str:
    value = value.strip()
    for f in (fmt,) if fmt else DATE_FORMATS:
        try:
            return datetime.strptime(value, f).date().isoformat()
        except ValueError:
            continue
    raise CSVImportError(f"unrecognized date {value!r}")


def _get(row: dict[str, str], column: str | None) -> str:
    if not column:
        return ""
    for k, v in row.items():
        if _norm(k) == _norm(column):
            return (v or "").strip()
    return ""


def parse_csv(text: str, cfg: Config, profile_name: str | None = None) -> ParsedStatement:
    profiles = load_profiles(cfg)
    if profile_name:
        profile = next((p for p in profiles if p.name == profile_name), None)
        if profile is None:
            raise CSVImportError(f"no profile named {profile_name!r}")
    else:
        profile = match_profile(text, profiles)
        if profile is None:
            raise CSVImportError(
                "no CSV profile matches this file's headers. Run `fiscl profile init <file>` to create one."
            )
    _, rows = _read_rows(text, profile.skip_rows)
    txns: list[ParsedTxn] = []
    balances: list[tuple[str, int]] = []
    for n, row in enumerate(rows, start=profile.skip_rows + 2):
        try:
            posted = _parse_date(_get(row, profile.date_column), profile.date_format)
            if profile.amount_column:
                amt = parse_amount(_get(row, profile.amount_column), cfg.currency)
            else:
                debit, credit = _get(row, profile.debit_column), _get(row, profile.credit_column)
                amt = (parse_amount(credit, cfg.currency) if credit else 0) - (
                    abs(parse_amount(debit, cfg.currency)) if debit else 0
                )
            if profile.amount_sign == "invert":
                amt = -amt
        except (AmountError, CSVImportError) as exc:
            raise CSVImportError(f"line {n}: {exc}") from exc
        pending = _get(row, profile.status_column).lower() == "pending"
        txns.append(
            ParsedTxn(
                posted_date=posted,
                amount=amt,
                description=clean_text(_get(row, profile.description_column)) or "(no description)",
                memo=clean_text(_get(row, profile.memo_column)) or None,
                pending=pending,
            )
        )
        bal = _get(row, profile.balance_column)
        if bal:
            try:
                balances.append((posted, parse_amount(bal, cfg.currency)))
            except AmountError:
                pass
    # Keep only the latest balance per date (last row seen for that date).
    latest: dict[str, int] = {}
    for d, b in balances:
        latest.setdefault(d, b)  # most banks list newest first
    return ParsedStatement(
        format="csv",
        txns=txns,
        currency=cfg.currency,
        balances=sorted(latest.items()),
        profile=profile.name,
        account_name=profile.account,
    )


def init_profile(path: Path, cfg: Config, name: str | None = None) -> Path:
    """Guess a mapping profile from a CSV's header and write it for the user to review."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    header, rows = _read_rows(text, 0)
    lower = {h.lower(): h for h in header}

    def pick(*cands: str) -> str | None:
        for c in cands:
            for low, orig in lower.items():
                if c in low:
                    return orig
        return None

    date_col = pick("posting date", "post date", "posted", "date")
    desc_col = pick("description", "payee", "merchant", "name", "details")
    amount_col = pick("amount")
    debit_col = pick("debit", "withdrawal")
    credit_col = pick("credit", "deposit")
    balance_col = pick("balance")
    if not date_col or not desc_col or not (amount_col or debit_col or credit_col):
        raise CSVImportError("could not guess date, description, and amount columns; write the profile by hand")
    name = name or path.stem.lower().replace(" ", "-")
    sample_date = rows[0].get(date_col, "") if rows else ""
    fmt = next((f for f in DATE_FORMATS if _try(sample_date, f)), None)
    lines = [
        f'name = "{name}"',
        f"match_headers = [{', '.join(repr(h).replace(chr(39), chr(34)) for h in header)}]",
        f'date_column = "{date_col}"',
        f'description_column = "{desc_col}"',
    ]
    if amount_col:
        lines.append(f'amount_column = "{amount_col}"')
    else:
        lines += [f'debit_column = "{debit_col}"', f'credit_column = "{credit_col}"']
    if balance_col:
        lines.append(f'balance_column = "{balance_col}"')
    if fmt:
        lines.append(f'date_format = "{fmt}"')
    lines += [
        'amount_sign = "as_is"   # set to "invert" if purchases show as positive numbers',
        '# account = "Chase Checking"   # optional: import straight into this account',
    ]
    cfg.profiles_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.profiles_dir / f"{name}.toml"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.chmod(FILE_MODE)
    return out


def _try(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value.strip(), fmt)
        return True
    except ValueError:
        return False
