"""Guardrails: input validation, redaction, row caps, untrusted-text hygiene, audit log."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from .config import Config

MAX_TEXT = 256
MAX_REGEX = 200
MAX_RANGE_DAYS = 3660  # ~10 years


class GuardError(ValueError):
    """Bad input from a tool call. The message is safe to show the model (it is redacted on the way out).

    `code` is a stable, data-free label; it is the only part of an error that reaches the audit log.
    """

    def __init__(self, message: str = "", code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


# --- untrusted text -----------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def clean_text(value: str | None, limit: int = MAX_TEXT) -> str:
    """Normalize bank-supplied text: NFC, no control/bidi characters, collapsed whitespace, capped."""
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = _CONTROL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# --- redaction ----------------------------------------------------------------------

_CARDISH = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d.])")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_LONG_DIGITS = re.compile(r"(?<![\d.])\d{9,}(?![\d.])")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask_card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return f"••••{digits[-4:]}"
    return m.group(0)


def redact_text(text: str) -> str:
    text = _SSN.sub("•••-••-••••", text)
    text = _CARDISH.sub(_mask_card, text)
    text = _LONG_DIGITS.sub(lambda m: f"••••{m.group(0)[-4:]}", text)
    return text


def redact(obj: Any) -> Any:
    """Recursively mask card, SSN and account-like numbers in every string of a tool response."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {(redact_text(k) if isinstance(k, str) else k): redact(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact(v) for v in obj]
    return obj


# --- validation ---------------------------------------------------------------------


def parse_date(value: str | None, field: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise GuardError(f"{field} must be an ISO date like 2026-01-31") from exc


def date_range(
    from_: str | None, to: str | None, default_days: int = 30, today: date | None = None
) -> tuple[date, date]:
    today = today or date.today()
    end = parse_date(to, "end_date") or today
    start = parse_date(from_, "start_date") or (end - timedelta(days=default_days - 1))
    if start > end:
        raise GuardError("start_date must be on or before end_date")
    if (end - start).days > MAX_RANGE_DAYS:
        raise GuardError("date range cannot exceed 10 years")
    return start, end


def parse_month(value: str | None, today: date | None = None) -> date:
    if not value:
        t = today or date.today()
        return t.replace(day=1)
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise GuardError("month must look like 2026-10") from exc


def choice(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise GuardError(f"{field} must be one of: {', '.join(allowed)}")
    return value


def clamp_limit(limit: int | None, cfg: Config) -> int:
    if limit is None:
        return cfg.guard.max_rows_default
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise GuardError("limit must be a positive integer")
    return min(limit, cfg.guard.max_rows_hard)


def safe_regex(pattern: str) -> re.Pattern:
    if len(pattern) > MAX_REGEX:
        raise GuardError(f"regex longer than {MAX_REGEX} characters")
    # Reject nested quantifiers, the usual catastrophic-backtracking shape.
    if re.search(r"\([^)]*[+*][^)]*\)[+*{]", pattern):
        raise GuardError("regex has nested quantifiers; simplify it")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise GuardError(f"invalid regex: {exc}") from exc


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- audit log ----------------------------------------------------------------------

# Only these argument values are logged verbatim. Everything else is logged as present/absent,
# so the audit log never contains merchant names, descriptions, or amounts.
_AUDIT_SAFE_KEYS = {
    "from_",
    "from",
    "to",
    "month",
    "limit",
    "group_by",
    "compare_to",
    "interval",
    "top_n",
    "sort",
    "dry_run",
    "create_rule",
    "days",
    "provider",
    "status",
    "kind",
    "min_confidence",
    "include_archived",
}


# Enum-like keys are logged verbatim only when the value is one of the known choices; free text that was
# (mis)passed in one of these slots is logged as "<invalid>", never as written.
_AUDIT_ENUMS: dict[str, frozenset[str]] = {
    "provider": frozenset({"all", "simplefin", "teller", "plaid", "qbo"}),
    "group_by": frozenset({"category", "merchant", "account", "month", "position", "asset_class"}),
    "compare_to": frozenset({"previous_period", "same_period_last_year", "none"}),
    "interval": frozenset({"month", "week"}),
    "sort": frozenset({"date_desc", "date_asc", "amount_asc", "amount_desc"}),
    "status": frozenset({"active", "price_changed", "missed", "cancelled"}),
    "kind": frozenset(
        {"subscription", "bill", "income"}  # list_recurring
        | {"spending_by_category", "spending_by_month", "cash_flow", "forecast", "budget", "net_worth"}
        | {"category_trend"}  # show_chart
        | {"property", "vehicle", "equipment", "investment", "cash", "liability", "other"}  # manage_asset
    ),
}


_AUDIT_DATE_KEYS = {"from_", "from", "to", "month"}
_AUDIT_NUM_KEYS = {"limit", "days", "top_n", "months", "min_confidence"}
_AUDIT_BOOL_KEYS = {"dry_run", "create_rule", "include_archived"}
_AUDIT_DATE = re.compile(r"\d{4}-\d{2}(-\d{2})?")


def _audit_value(key: str, value: Any) -> Any:
    """Log a value verbatim only if it has the shape its key promises; anything else is "<invalid>"."""
    if key in _AUDIT_ENUMS:
        ok = isinstance(value, str) and value in _AUDIT_ENUMS[key]
    elif key in _AUDIT_DATE_KEYS:
        ok = isinstance(value, str) and _AUDIT_DATE.fullmatch(value) is not None
    elif key in _AUDIT_NUM_KEYS:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif key in _AUDIT_BOOL_KEYS:
        ok = isinstance(value, bool)
    else:
        return "<set>"
    return value if ok else "<invalid>"


def audit(
    cfg: Config, tool: str, args: dict[str, Any], rows: int | None, started: float, error: str | None = None
) -> None:
    if not cfg.guard.audit_log:
        return
    safe_args = {k: _audit_value(k, v) for k, v in args.items() if v is not None}
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": tool,
        "args": safe_args,
        "rows": rows,
        "ms": round((time.monotonic() - started) * 1000),
        "error": error,
    }
    cfg.logs.mkdir(parents=True, exist_ok=True)
    with (cfg.logs / "audit.log").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
