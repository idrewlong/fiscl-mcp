"""Config, data-directory layout, rules and budgets files."""

from __future__ import annotations

import os
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


@dataclass
class TaxConfig:
    mileage_cents: dict[int, str] = field(default_factory=lambda: dict(DEFAULT_MILEAGE_CENTS))
    ss_wage_base: dict[int, int] = field(default_factory=lambda: dict(DEFAULT_SS_WAGE_BASE))
    # Optional: your own planning rate for income tax on business profit, e.g. 22.
    income_tax_rate_pct: str | None = None


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
redact_descriptions = false   # true = raw bank descriptions never leave the server
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

# Override or add years as the IRS/SSA publish them:
# [tax.mileage_cents]
# 2027 = "75"
# [tax.ss_wage_base]
# 2027 = 190000
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
    for d in (cfg.home, cfg.inbox, cfg.processed, cfg.failed, cfg.logs, cfg.backups, cfg.profiles_dir):
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
    except (ValueError, InvalidOperation) as exc:
        raise ConfigError(f"[tax] section: {exc}") from exc
    return Config(
        home=home,
        tax=tax,
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
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def append_toml_block(path: Path, block: str, tool: str) -> None:
    """Append to a user-edited TOML file, keeping a .bak and a provenance comment."""
    backup_file(path)
    stamp = datetime.now().isoformat(timespec="seconds")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "" if existing.endswith("\n") or not existing else "\n"
    _write_private(path, f"{existing}{sep}\n# added by {tool} at {stamp}\n{block.rstrip()}\n")
    tomllib.loads(path.read_text(encoding="utf-8"))  # fail loudly if we produced invalid TOML
