"""Merchant name normalization: strip processor noise, apply user aliases."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .guard import GuardError, clean_text, safe_regex

_PREFIXES = [
    r"^purchase authorized on \d{1,2}/\d{1,2}\s*",
    r"^(pos|debit card|checkcard|visa|recurring)\s+(debit|purchase|payment)?\s*(\d{4}\s+)?",
    r"^(ach|electronic)\s+(debit|credit|deposit|withdrawal|payment)\s*",
    r"^(sq|tst|sp|py|pp|in|bt)\s*\*\s*",
    r"^paypal\s*\*\s*",
]
_NOISE = [
    r"\b(x{2,}|\*{2,})\d{2,}\b",  # masked card numbers
    r"#\s?\d+",  # store numbers
    r"\b\d{3}[-.]\d{3}[-.]\d{4}\b",  # phone numbers
    r"\b(ref|id|conf|trace)[:#]?\s*[a-z0-9]{5,}\b",
    r"\b\d{5,}\b",  # long reference numbers
    r"\b(ppd|ccd|web)( id)?\b:?",  # ACH entry codes
    r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b",  # inline dates
]
_STATE_SUFFIX = re.compile(r"\s+[A-Z]{2}$")
# A bare 3-4 digit number at the end is a store number ("CHIPOTLE 2291"); shorter ones are names ("MOTEL 6").
_STORE_SUFFIX = re.compile(r"(?<=[a-z])\s+\d{3,4}$", re.IGNORECASE)
_UPPER_WORDS = {"llc", "llp", "pllc", "usa"}


@dataclass
class Alias:
    pattern: re.Pattern
    merchant: str


def load_aliases(raw_rules: dict) -> list[Alias]:
    aliases: list[Alias] = []
    for i, a in enumerate(raw_rules.get("alias", [])):
        if not isinstance(a, dict) or "match" not in a or "merchant" not in a:
            raise GuardError(f"rules.toml alias {i + 1} needs match and merchant")
        aliases.append(Alias(safe_regex(str(a["match"])), clean_text(str(a["merchant"]), 80)))
    return aliases


def _title(text: str) -> str:
    # Capitalize word starts, but not after a dot (netflix.com) or inside alphanumeric codes.
    titled = re.sub(r"(?<![\w.'])[a-z]", lambda m: m[0].upper(), text.lower())
    return re.sub(r"\b[a-z]+\b", lambda m: m[0].upper() if m[0].lower() in _UPPER_WORDS else m[0], titled, flags=re.I)


def normalize_merchant(description: str, aliases: list[Alias] | None = None) -> str:
    desc = clean_text(description)
    for alias in aliases or []:
        if alias.pattern.search(desc):
            return alias.merchant
    s = desc
    for pat in _PREFIXES:
        s = re.sub(pat, "", s, flags=re.IGNORECASE)
    for pat in _NOISE:
        s = re.sub(pat, " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\*\s*", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -*.,")
    s = _STORE_SUFFIX.sub("", s)
    if desc.isupper() or s.isupper():
        s = _STATE_SUFFIX.sub("", s)
    s = s.strip(" -*.,")
    return _title(s) if s else _title(desc)[:80]
