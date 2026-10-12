"""Mapping-profile driven CSV import."""

from __future__ import annotations

import csv
import io
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import FILE_MODE, Config, toml_string
from ..guard import GuardError, clean_text, safe_regex
from ..money import AmountError, is_currency_code, parse_amount
from .types import ParsedStatement, ParsedTxn

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y")


class CSVImportError(ValueError):
    pass


KINDS = ("transactions", "holdings")
ACCOUNT_POLICIES = ("ignore", "last4")
HOLDINGS_ACCOUNT_TYPES = ("brokerage", "crypto")


@dataclass
class Profile:
    name: str
    match_headers: list[str]
    date_column: str = ""
    description_column: str = ""
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
    currency: str | None = None  # the statement's currency (3-letter code); default is the home currency
    # File layout, for exports with a preamble or footer: find the header row by `match_headers` instead of counting
    # `skip_rows`, and stop at the first blank line (a footer or a second section follows it).
    find_header: bool = False
    stop_at_blank: bool = False
    # Holdings profiles (kind = "holdings"): one row per position, a snapshot per file.
    kind: str = "transactions"
    symbol_column: str | None = None
    name_column: str | None = None
    quantity_column: str | None = None
    price_column: str | None = None
    value_column: str | None = None
    cost_basis_column: str | None = None
    asset_class_column: str | None = None
    as_of_column: str | None = None
    as_of_format: str | None = None  # strptime format for as_of_column and as_of_regex
    as_of_regex: str | None = None  # one capture group, searched line by line over the whole file
    skip_symbols: list[str] | None = None  # summary rows to skip, such as "Positions Total"
    # Brokers put the full account number in a column. "ignore" never reads it; "last4" keeps only the last four
    # characters to route rows to an account that has the same last4. The rest is dropped before anything is stored.
    account_column: str | None = None
    account_column_policy: str = "ignore"
    account_type: str = "brokerage"  # type of an account created by the import

    @classmethod
    def from_dict(cls, data: dict, source: str) -> Profile:
        try:
            p = cls(**data)
        except TypeError as exc:
            raise CSVImportError(f"profile {source}: has an unknown field or is missing a required one") from exc
        if p.kind not in KINDS:
            raise CSVImportError(f"profile {source}: kind must be transactions or holdings")
        if p.currency is not None:
            p.currency = p.currency.strip().upper() if isinstance(p.currency, str) else ""
            if not is_currency_code(p.currency):
                raise CSVImportError(f"profile {source}: currency must be a 3-letter code like EUR")
        if p.kind == "holdings":
            p._validate_holdings(source)
        else:
            if not p.date_column or not p.description_column:
                raise CSVImportError(f"profile {source}: has an unknown field or is missing a required one")
            if not p.amount_column and not (p.debit_column or p.credit_column):
                raise CSVImportError(f"profile {source}: needs amount_column or debit_column/credit_column")
            if p.amount_sign not in ("as_is", "invert"):
                raise CSVImportError(f"profile {source}: amount_sign must be as_is or invert")
        return p

    def _validate_holdings(self, source: str) -> None:
        if not self.symbol_column or not self.quantity_column:
            raise CSVImportError(f"profile {source}: holdings profiles need symbol_column and quantity_column")
        if self.account_column_policy not in ACCOUNT_POLICIES:
            raise CSVImportError(f"profile {source}: account_column_policy must be ignore or last4")
        if self.account_type not in HOLDINGS_ACCOUNT_TYPES:
            raise CSVImportError(f"profile {source}: account_type must be brokerage or crypto")
        if self.skip_symbols is not None and not (
            isinstance(self.skip_symbols, list) and all(isinstance(s, str) for s in self.skip_symbols)
        ):
            raise CSVImportError(f"profile {source}: skip_symbols must be a list of strings")
        if self.as_of_regex:
            try:
                rx = safe_regex(self.as_of_regex)
            except GuardError as exc:
                raise CSVImportError(f"profile {source}: as_of_regex is not usable") from exc
            if rx.groups != 1 or not self.as_of_format:
                raise CSVImportError(f"profile {source}: as_of_regex needs one capture group and an as_of_format")


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
    # Positions export in the simplest layout. Vendor-specific profiles (broker and crypto exports) are deliberately
    # absent until real, scrubbed header samples exist: run `fiscl profile init FILE --kind holdings` for yours.
    Profile(
        name="generic-holdings",
        match_headers=["Symbol", "Quantity"],
        kind="holdings",
        find_header=True,
        symbol_column="Symbol",
        name_column="Description",
        quantity_column="Quantity",
        price_column="Price",
        value_column="Market Value",
        cost_basis_column="Cost Basis",
        asset_class_column="Asset Class",
    ),
]


def load_profiles(cfg: Config) -> list[Profile]:
    profiles: list[Profile] = []
    if cfg.profiles_dir.exists():
        for path in sorted(cfg.profiles_dir.glob("*.toml")):
            try:
                with path.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise CSVImportError(f"profile {path.name} is not valid TOML") from exc
            profiles.append(Profile.from_dict(data, path.name))
    return profiles + BUILTIN_PROFILES


def _norm(h: str) -> str:
    return h.strip().strip("﻿").lower()


HEADER_SEARCH_LINES = 100


def _find_header_line(lines: list[str], match_headers: list[str]) -> int | None:
    """Index of the first line (within the first HEADER_SEARCH_LINES) whose cells include every wanted header."""
    want = {_norm(h) for h in match_headers}
    for i, line in enumerate(lines[:HEADER_SEARCH_LINES]):
        try:
            cells = next(csv.reader([line]), [])
        except csv.Error:
            continue
        if want <= {_norm(c) for c in cells}:
            return i
    return None


def _read_rows(
    text: str,
    skip_rows: int,
    *,
    match_headers: list[str] | None = None,
    find_header: bool = False,
    stop_at_blank: bool = False,
) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.splitlines()
    start = skip_rows
    if find_header and match_headers:
        found = _find_header_line(lines, match_headers)
        if found is None:
            raise CSVImportError("could not find the header row in this file")
        start = found
    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    try:
        header = [h.strip().strip("﻿") for h in next(reader)]
    except StopIteration as exc:
        raise CSVImportError("file is empty") from exc
    rows = []
    for raw in reader:
        if not any(cell.strip() for cell in raw):
            if stop_at_blank:
                break
            continue
        # Exports often end every line with a comma: the unnamed trailing column is dropped.
        rows.append({header[i]: raw[i] if i < len(raw) else "" for i in range(len(header)) if header[i]})
    return header, rows


def read_profile_rows(text: str, profile: Profile) -> tuple[list[str], list[dict[str, str]]]:
    return _read_rows(
        text,
        profile.skip_rows,
        match_headers=profile.match_headers,
        find_header=profile.find_header,
        stop_at_blank=profile.stop_at_blank,
    )


def match_profile(text: str, profiles: list[Profile]) -> Profile | None:
    for p in profiles:
        try:
            header, _ = read_profile_rows(text, p)
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
    raise CSVImportError("unrecognized date")


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
            raise CSVImportError("no CSV profile with that name exists in the profiles folder")
    else:
        profile = match_profile(text, profiles)
        if profile is None:
            raise CSVImportError(
                "no CSV profile matches this file's headers. Run `fiscl profile init <file>` to create one."
            )
    if profile.kind == "holdings":
        raise CSVImportError("that profile is for holdings files, which are imported as a snapshot, not transactions")
    _, rows = read_profile_rows(text, profile)
    currency = profile.currency or cfg.currency
    txns: list[ParsedTxn] = []
    balances: list[tuple[str, int]] = []
    for n, row in enumerate(rows, start=profile.skip_rows + 2):
        try:
            posted = _parse_date(_get(row, profile.date_column), profile.date_format)
            if profile.amount_column:
                amt = parse_amount(_get(row, profile.amount_column), currency)
            else:
                debit, credit = _get(row, profile.debit_column), _get(row, profile.credit_column)
                amt = (parse_amount(credit, currency) if credit else 0) - (
                    abs(parse_amount(debit, currency)) if debit else 0
                )
            if profile.amount_sign == "invert":
                amt = -amt
        except (AmountError, CSVImportError) as exc:
            raise CSVImportError(f"{exc} on line {n}") from exc
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
                balances.append((posted, parse_amount(bal, currency)))
            except AmountError:
                pass
    # Keep only the latest balance per date (last row seen for that date).
    latest: dict[str, int] = {}
    for d, b in balances:
        latest.setdefault(d, b)  # most banks list newest first
    return ParsedStatement(
        format="csv",
        txns=txns,
        currency=currency,
        balances=sorted(latest.items()),
        profile=profile.name,
        account_name=profile.account,
    )


def init_profile(path: Path, cfg: Config, name: str | None = None, kind: str = "transactions") -> Path:
    """Guess a mapping profile from a CSV's header and write it for the user to review."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if kind == "holdings":
        return _init_holdings_profile(text, cfg, name or path.stem.lower().replace(" ", "-"))
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
    q = toml_string
    lines = [
        f"name = {q(name)}",
        f"match_headers = [{', '.join(q(h) for h in header)}]",
        f"date_column = {q(date_col)}",
        f"description_column = {q(desc_col)}",
    ]
    if amount_col:
        lines.append(f"amount_column = {q(amount_col)}")
    else:
        lines += [f"debit_column = {q(debit_col)}", f"credit_column = {q(credit_col)}"]
    if balance_col:
        lines.append(f"balance_column = {q(balance_col)}")
    if fmt:
        lines.append(f"date_format = {q(fmt)}")
    lines += [
        'amount_sign = "as_is"   # set to "invert" if purchases show as positive numbers',
        '# account = "Chase Checking"   # optional: import straight into this account',
        '# currency = "EUR"             # optional: the statement\'s currency (default: your home currency)',
    ]
    return _write_profile(cfg, name, lines)


def _write_profile(cfg: Config, name: str, lines: list[str]) -> Path:
    cfg.profiles_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.profiles_dir / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('.-') or 'profile'}.toml"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.chmod(FILE_MODE)
    return out


# Substrings tried in order against the header, case-insensitively, to guess holdings columns.
_HOLDINGS_GUESSES = {
    "symbol_column": ("symbol", "ticker"),
    "name_column": ("description", "investment name", "security name"),
    "quantity_column": ("quantity", "qty", "shares"),
    "price_column": ("last price", "share price", "price"),
    "value_column": ("market value", "mkt val", "current value", "total value", "value"),
    "cost_basis_column": ("cost basis total", "cost basis", "cost"),
    "asset_class_column": ("asset class", "asset type", "security type", "investment type"),
}
_ACCOUNT_COLUMN_GUESSES = ("account number", "account #", "account no", "acct")


def _init_holdings_profile(text: str, cfg: Config, name: str) -> Path:
    lines = text.splitlines()
    header: list[str] = []
    for line in lines[:HEADER_SEARCH_LINES]:
        try:
            cells = [c.strip() for c in next(csv.reader([line]), [])]
        except csv.Error:
            continue
        low = [c.lower() for c in cells]
        if any("symbol" in c or "ticker" in c for c in low) and any(
            "quantity" in c or "qty" in c or "shares" in c for c in low
        ):
            header = [c for c in cells if c]
            break
    if not header:
        raise CSVImportError("could not find a header row with symbol and quantity columns; write the profile by hand")
    lower = {h.lower(): h for h in header}

    def pick(*cands: str) -> str | None:
        for c in cands:
            for low, orig in lower.items():
                if c in low:
                    return orig
        return None

    q = toml_string
    out = [
        f"name = {q(name)}",
        'kind = "holdings"',
        f"match_headers = [{', '.join(q(h) for h in header)}]",
        "find_header = true   # the header row is found by match_headers, so a preamble above it is fine",
    ]
    guessed = {key: pick(*cands) for key, cands in _HOLDINGS_GUESSES.items()}
    out += [f"{key} = {q(col)}" for key, col in guessed.items() if col]
    account_col = pick(*_ACCOUNT_COLUMN_GUESSES)
    out += [
        'account_column_policy = "ignore"   # the account column is never read; see the note below',
        '# account = "Brokerage"            # import straight into this account (created if missing)',
        '# account_type = "crypto"          # type for a created account: brokerage (default) or crypto',
        "# stop_at_blank = true             # stop at the first blank line (a footer or a second section follows)",
        '# skip_symbols = ["Positions Total", "Account Total"]   # summary rows to skip',
        "# The snapshot date: pass --as-of on import, or read it from a line in the file (one capture group):",
        '# as_of_regex = "Date downloaded (\\\\w{3}-\\\\d{2}-\\\\d{4})"',
        '# as_of_format = "%b-%d-%Y"',
    ]
    if account_col:
        out += [
            f"# This file has an account column ({q(account_col)}); fiscl never stores account numbers. To route rows",
            "# to accounts, give each account its last 4 (`fiscl accounts add NAME --type brokerage --last4 1234`):",
            f"# account_column = {q(account_col)}",
            '# account_column_policy = "last4"   # keeps only the last 4 characters of each value',
        ]
    return _write_profile(cfg, name, out)


def _try(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value.strip(), fmt)
        return True
    except ValueError:
        return False
