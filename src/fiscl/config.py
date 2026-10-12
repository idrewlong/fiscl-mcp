"""Config, data-directory layout, rules and budgets files."""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .money import AmountError, to_minor

DIR_MODE = 0o700
FILE_MODE = 0o600


class ConfigError(ValueError):
    pass


@dataclass
class GuardConfig:
    max_rows_default: int = 50
    max_rows_hard: int = 500
    redact_descriptions: bool = False
    audit_log: bool = True


# IRS standard business mileage rate in cents per mile, by tax year (IRS notices; verify each year).
DEFAULT_MILEAGE_CENTS = {2024: "67", 2025: "70", 2026: "72.5"}
# Social Security wage base in dollars, by year (SSA; verify each year).
DEFAULT_SS_WAGE_BASE = {2024: 168600, 2025: 176100, 2026: 184500}


@dataclass(frozen=True)
class HomeOffice:
    """Simplified-method home office for one tax year, entered under [tax.home_office.YYYY]."""

    square_feet: int  # 1-300, regular and exclusive business use
    months_used: int = 12  # 1-12; months with business use, averaged into the allowable area


@dataclass
class TaxConfig:
    mileage_cents: dict[int, str] = field(default_factory=lambda: dict(DEFAULT_MILEAGE_CENTS))
    ss_wage_base: dict[int, int] = field(default_factory=lambda: dict(DEFAULT_SS_WAGE_BASE))
    # Optional: your own planning rate for income tax on business profit, e.g. 22.
    income_tax_rate_pct: str | None = None
    # Optional: two-letter state code (or DC) and your own planning rate for state income tax, 0-20.
    state: str | None = None
    state_income_tax_rate_pct: str | None = None
    home_office: dict[int, HomeOffice] = field(default_factory=dict)


@dataclass
class DebtTerms:
    """Terms the bank export doesn't carry, entered once under [debts."Account name"] in config.toml."""

    apr_pct: str  # annual percentage rate, e.g. "24.99"
    min_payment: int  # minor units


TELLER_ENVIRONMENTS = ("development", "sandbox", "production")
PLAID_ENVIRONMENTS = ("sandbox", "production")  # Plaid has no "development" host any more


@dataclass
class Config:
    home: Path
    currency: str = "USD"
    timezone: str = "America/Chicago"
    transfer_window_days: int = 3
    recurring_min_occurrences: int = 3
    recurring_amount_tolerance_pct: int = 15
    guard: GuardConfig = field(default_factory=GuardConfig)
    tax: TaxConfig = field(default_factory=TaxConfig)
    debts: dict[str, DebtTerms] = field(default_factory=dict)
    teller_environment: str = "development"  # [connectors.teller] environment
    plaid_environment: str = "sandbox"  # [connectors.plaid] environment

    @property
    def db_path(self) -> Path:
        return self.home / "fiscl.db"

    @property
    def inbox(self) -> Path:
        return self.home / "inbox"

    @property
    def processed(self) -> Path:
        return self.inbox / "processed"

    @property
    def failed(self) -> Path:
        return self.inbox / "failed"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    @property
    def exports(self) -> Path:
        return self.home / "exports"

    @property
    def profiles_dir(self) -> Path:
        return self.home / "profiles"

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def rules_file(self) -> Path:
        return self.home / "rules.toml"

    @property
    def budgets_file(self) -> Path:
        return self.home / "budgets.toml"


def default_home() -> Path:
    return Path(os.environ.get("FISCL_HOME", "~/fiscl")).expanduser()


DEFAULT_CONFIG = """\
# fiscl configuration
currency = "USD"
timezone = "America/Chicago"

[guard]
max_rows_default = 50
max_rows_hard = 500
# redact_descriptions = true withholds raw descriptions and memos. Normalized merchant names (which can include
# P2P counterparty names) are still returned.
redact_descriptions = false
audit_log = true

[transfers]
window_days = 3

[recurring]
min_occurrences = 3
amount_tolerance_pct = 15

[tax]
# Your own planning rate for income tax on business profit (used by estimated_tax). Leave unset to
# get self-employment tax only.
# income_tax_rate_pct = 22

# Your state (two-letter code, or DC) adds state due dates and a planning-rate estimate to estimated_tax. The rate
# is your own number (0-20), applied to the same base as the federal estimate; it is not a state tax calculation.
# state = "MS"
# state_income_tax_rate_pct = 4.4

# Override or add years as the IRS/SSA publish them:
# [tax.mileage_cents]
# 2027 = "75"
# [tax.ss_wage_base]
# 2027 = 190000

# Home office, simplified method ($5 per sq ft, at most 300 sq ft), per tax year. Shows up on Schedule C line 30.
# [tax.home_office.2026]
# square_feet = 200          # 1-300, used regularly and exclusively for business
# months_used = 12           # 1-12 (default 12); months of business use, averaged into the area

# Optional: interest rates and minimums for debt_payoff (bank exports don't carry them).
# Key = the account name in fiscl. apr is a percent, min_payment is dollars.
# [debts."Chase Sapphire"]
# apr = 24.99
# min_payment = 35
"""

DEFAULT_RULES = """\
# fiscl rules — first match wins, top to bottom. Manual categorizations always beat rules.
# Matchers: merchant, description_regex, account, amount_gt, amount_lt, amount_eq, day_of_month
# Amounts are in normal units (dollars), negative = money out.

[[alias]]
match = "AMZN MKTP|AMAZON\\\\.COM|AMZN DIGITAL|AMAZON MKTPL"
merchant = "Amazon"

[[rule]]
description_regex = "(?i)payroll|direct dep"
amount_gt = 0
category = "Income: Salary"

[[rule]]
description_regex = "(?i)\\\\brent\\\\b|apartments|\\\\bapts\\\\b|mortgage"
amount_lt = 0
category = "Housing"

[[rule]]
description_regex = "(?i)power|electric|energy|water (dept|works|utility)|utilit|comcast|xfinity|spectrum|at&t|verizon|t-mobile"
amount_lt = 0
category = "Utilities"

[[rule]]
merchant = "Amazon"
category = "Shopping"

[[rule]]
description_regex = "(?i)netflix|spotify|hulu|disney\\\\+|youtube premium|apple\\\\.com/bill"
category = "Subscriptions"

[[rule]]
description_regex = "(?i)shell|exxon|chevron|circle k|murphy usa"
category = "Auto: Fuel"
"""

DEFAULT_BUDGETS = """\
# Monthly budgets in normal units (dollars).
# [[budget]]
# category = "Dining"
# monthly = 400.00
"""


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, FILE_MODE)


def ensure_layout(cfg: Config) -> None:
    """Create the data directory with private permissions and starter files."""
    home = cfg.home.resolve()
    if home == Path.home().resolve() or home == Path(home.anchor):
        # We chmod the data dir to 0700 and fill it with files; never do that to $HOME or a filesystem root.
        raise ConfigError(f"{cfg.home} can't be the fiscl data directory; use a dedicated folder like ~/fiscl")
    for d in (cfg.home, cfg.inbox, cfg.processed, cfg.failed, cfg.logs, cfg.backups, cfg.profiles_dir, cfg.exports):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, DIR_MODE)
    for path, text in (
        (cfg.config_file, DEFAULT_CONFIG),
        (cfg.rules_file, DEFAULT_RULES),
        (cfg.budgets_file, DEFAULT_BUDGETS),
    ):
        if not path.exists():
            _write_private(path, text)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc


def _int(section: dict, key: str, default: int, lo: int, hi: int, where: str) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ConfigError(f"{where}.{key} must be an integer between {lo} and {hi}")
    return value


def _load_home_office(raw: Any) -> dict[int, HomeOffice]:
    """Parse [tax.home_office.YYYY] tables. Errors say which year, never anything else."""
    if not isinstance(raw, dict):
        raise ConfigError("[tax.home_office] must be a table of [tax.home_office.YYYY] entries")
    out: dict[int, HomeOffice] = {}
    for key, entry in raw.items():
        if not (isinstance(key, str) and key.isascii() and key.isdigit() and 2000 <= int(key) <= 2100):
            raise ConfigError("[tax.home_office] years must be 4-digit years between 2000 and 2100")
        where = f"tax.home_office.{int(key)}"
        if not isinstance(entry, dict) or set(entry) - {"square_feet", "months_used"}:
            raise ConfigError(f"[{where}] takes only square_feet and months_used")
        if "square_feet" not in entry:
            raise ConfigError(f"[{where}] needs square_feet (1 to 300)")
        out[int(key)] = HomeOffice(
            square_feet=_int(entry, "square_feet", 0, 1, 300, where),
            months_used=_int(entry, "months_used", 12, 1, 12, where),
        )
    return out


def _load_state(raw: Any) -> str | None:
    """tax.state: a two-letter code from the states table, case-insensitive."""
    if raw is None:
        return None
    from .tax.states import STATES  # deferred: the tax package imports this module

    code = raw.strip().upper() if isinstance(raw, str) else None
    if code not in STATES:
        raise ConfigError("tax.state must be a two-letter US state code (or DC), like MS")
    return code


def _load_debts(raw: Any, currency: str) -> dict[str, DebtTerms]:
    """Parse the optional [debts."Account name"] tables: apr (percent) and min_payment (dollars), both required."""
    if not isinstance(raw, dict):
        raise ConfigError('[debts] must be a table of [debts."Account name"] entries')
    out: dict[str, DebtTerms] = {}
    for n, (name, entry) in enumerate(raw.items(), start=1):
        where = f"[debts] entry {n}"  # by position: account names stay out of error text
        if not isinstance(entry, dict) or set(entry) - {"apr", "min_payment"}:
            raise ConfigError(f"{where} takes only apr and min_payment")
        if "apr" not in entry or "min_payment" not in entry:
            raise ConfigError(f"{where} needs both apr (percent) and min_payment (dollars)")
        try:
            apr = Decimal(str(entry["apr"]))
            minimum = to_minor(entry["min_payment"], currency)
        except (InvalidOperation, AmountError) as exc:
            raise ConfigError(f"{where}: {exc}") from exc
        if isinstance(entry["apr"], bool) or not apr.is_finite() or not 0 <= apr <= 100:
            raise ConfigError(f"{where}.apr must be a percentage between 0 and 100")
        if minimum <= 0:
            raise ConfigError(f"{where}.min_payment must be greater than 0")
        out[name.lower()] = DebtTerms(apr_pct=str(apr), min_payment=minimum)
    return out


def load_config(home: Path | None = None) -> Config:
    home = (home or default_home()).expanduser()
    raw = _read_toml(home / "config.toml")
    guard_raw = raw.get("guard", {})
    guard = GuardConfig(
        max_rows_default=_int(guard_raw, "max_rows_default", 50, 1, 500, "guard"),
        max_rows_hard=_int(guard_raw, "max_rows_hard", 500, 1, 5000, "guard"),
        redact_descriptions=bool(guard_raw.get("redact_descriptions", False)),
        audit_log=bool(guard_raw.get("audit_log", True)),
    )
    if guard.max_rows_default > guard.max_rows_hard:
        raise ConfigError("guard.max_rows_default cannot exceed guard.max_rows_hard")
    currency = raw.get("currency", "USD")
    if not isinstance(currency, str) or len(currency) != 3:
        raise ConfigError("currency must be a 3-letter ISO code like USD")
    tax_raw = raw.get("tax", {})
    tax = TaxConfig()
    try:
        for k, v in tax_raw.get("mileage_cents", {}).items():
            tax.mileage_cents[int(k)] = str(Decimal(str(v)))
        for k, v in tax_raw.get("ss_wage_base", {}).items():
            tax.ss_wage_base[int(k)] = int(v)
        if tax_raw.get("income_tax_rate_pct") is not None:
            rate = Decimal(str(tax_raw["income_tax_rate_pct"]))
            if not 0 <= rate <= 60:
                raise ConfigError("tax.income_tax_rate_pct must be between 0 and 60")
            tax.income_tax_rate_pct = str(rate)
        tax.state = _load_state(tax_raw.get("state"))
        if tax_raw.get("state_income_tax_rate_pct") is not None:
            state_rate = tax_raw["state_income_tax_rate_pct"]
            if isinstance(state_rate, bool) or not isinstance(state_rate, int | float):
                raise ConfigError("tax.state_income_tax_rate_pct must be a number between 0 and 20")
            if not 0 <= Decimal(str(state_rate)) <= 20:
                raise ConfigError("tax.state_income_tax_rate_pct must be a number between 0 and 20")
            tax.state_income_tax_rate_pct = str(Decimal(str(state_rate)))
        tax.home_office = _load_home_office(tax_raw.get("home_office", {}))
    except (ValueError, InvalidOperation) as exc:
        raise ConfigError(f"[tax] section: {exc}") from exc
    teller_env = (raw.get("connectors", {}).get("teller", {}) or {}).get("environment", "development")
    if teller_env not in TELLER_ENVIRONMENTS:
        raise ConfigError(f"connectors.teller.environment must be one of {', '.join(TELLER_ENVIRONMENTS)}")
    plaid_env = (raw.get("connectors", {}).get("plaid", {}) or {}).get("environment", "sandbox")
    if plaid_env not in PLAID_ENVIRONMENTS:
        raise ConfigError(f"connectors.plaid.environment must be one of {', '.join(PLAID_ENVIRONMENTS)}")
    return Config(
        home=home,
        tax=tax,
        teller_environment=teller_env,
        plaid_environment=plaid_env,
        debts=_load_debts(raw.get("debts", {}), currency.upper()),
        currency=currency.upper(),
        timezone=str(raw.get("timezone", "America/Chicago")),
        transfer_window_days=_int(raw.get("transfers", {}), "window_days", 3, 0, 14, "transfers"),
        recurring_min_occurrences=_int(raw.get("recurring", {}), "min_occurrences", 3, 2, 24, "recurring"),
        recurring_amount_tolerance_pct=_int(raw.get("recurring", {}), "amount_tolerance_pct", 15, 0, 100, "recurring"),
        guard=guard,
    )


def load_rules_raw(cfg: Config) -> dict[str, Any]:
    return _read_toml(cfg.rules_file)


@dataclass
class Budget:
    category: str
    monthly: int  # minor units
    rollover: bool = False


def load_budgets(cfg: Config) -> list[Budget]:
    raw = _read_toml(cfg.budgets_file)
    out: list[Budget] = []
    for i, b in enumerate(raw.get("budget", [])):
        if not isinstance(b, dict) or "category" not in b or "monthly" not in b:
            raise ConfigError(f"budgets.toml entry {i + 1} needs category and monthly")
        try:
            monthly = to_minor(b["monthly"], cfg.currency)
        except AmountError as exc:
            raise ConfigError(f"budgets.toml entry {i + 1}: {exc}") from exc
        if monthly <= 0:
            raise ConfigError(f"budgets.toml entry {i + 1}: monthly must be positive")
        out.append(Budget(category=str(b["category"]), monthly=monthly, rollover=bool(b.get("rollover", False))))
    return out


def backup_file(path: Path) -> None:
    """Copy `path` to `path.bak` with private permissions, whatever the original's mode is."""
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copyfile(path, bak)
        os.chmod(bak, FILE_MODE)


def append_toml_block(path: Path, block: str, tool: str) -> None:
    """Append to a user-edited TOML file, keeping a .bak and a provenance comment."""
    stamp = datetime.now().isoformat(timespec="seconds")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if existing.endswith("\n") or not existing else "\n"
    new = f"{existing}{sep}\n# added by {tool} at {stamp}\n{block.rstrip()}\n"
    tomllib.loads(new)  # validate before anything touches disk: a bad block must never leave a broken file
    backup_file(path)
    _write_private(path, new)


def toml_string(value: str) -> str:
    # ensure_ascii=False keeps non-BMP text literal (JSON would emit surrogate-pair escapes, which TOML rejects).
    # DEL is the one control character JSON leaves raw that TOML forbids.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


_BUDGET_HEADER = re.compile(r"^\s*\[\[\s*budget\s*\]\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")


def write_budgets(cfg: Config, budgets: list[Budget], tool: str = "set_budget") -> None:
    """Rewrite only the [[budget]] blocks of budgets.toml.

    Everything outside those blocks (header comments, other tables) is kept. A .bak is written first, a
    provenance comment is added, and the result is re-validated with load_budgets; on failure the original
    file is restored.
    """
    path = cfg.budgets_file
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    kept: list[str] = []
    in_budget = False
    for line in original.splitlines():
        stripped = line.strip()
        if _BUDGET_HEADER.match(line):
            in_budget = True
            continue  # the header is rewritten along with the block
        if _ANY_HEADER.match(line):
            in_budget = False
        if stripped.startswith("# updated by "):
            continue
        if in_budget and stripped and not stripped.startswith("#"):
            continue  # one of the block's own key lines; comments and blank lines stay where they are
        if not stripped and kept and not kept[-1].strip():
            continue  # collapse blank runs left behind by removed blocks
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    scale = 1 if cfg.currency in ("JPY", "KRW") else 100
    out = "\n".join(kept)
    out += "\n\n" if out else ""
    out += f"# updated by {tool} at {datetime.now().isoformat(timespec='seconds')}\n"
    for b in sorted(budgets, key=lambda b: b.category.lower()):
        monthly = str(b.monthly) if scale == 1 else f"{Decimal(b.monthly) / scale:.2f}"
        out += f"\n[[budget]]\ncategory = {toml_string(b.category)}\nmonthly = {monthly}\n"
        if b.rollover:
            out += "rollover = true\n"
    backup_file(path)
    _write_private(path, out)
    try:
        written = load_budgets(cfg)
        if [(b.category, b.monthly, b.rollover) for b in written] != [
            (b.category, b.monthly, b.rollover) for b in sorted(budgets, key=lambda b: b.category.lower())
        ]:
            raise ConfigError("budgets.toml did not round-trip")
    except ConfigError as exc:
        _write_private(path, original)
        raise ConfigError(
            "budgets.toml couldn't be rewritten safely, so it was left unchanged (a backup is budgets.toml.bak). "
            "Check it for hand-edits inside the [[budget]] entries, such as multi-line values."
        ) from exc
