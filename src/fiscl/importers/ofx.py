"""Tolerant OFX/QFX parser (OFX 1.x SGML and 2.x XML).

Real-world bank OFX is frequently malformed (unclosed leaf tags, odd headers), so this
reads values by tag rather than requiring a valid document.
"""

from __future__ import annotations

import re
from datetime import date

from ..guard import clean_text
from ..money import parse_amount
from .types import ParsedStatement, ParsedTxn


class OFXError(ValueError):
    pass


def looks_like_ofx(head: str) -> bool:
    h = head.upper()
    return "OFXHEADER" in h or "<OFX>" in h or "<?OFX" in h


def _value(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\s*([^<\r\n]*)", block, re.IGNORECASE)
    if not m:
        return None
    v = m.group(1).strip()
    return v or None


def _blocks(text: str, tag: str) -> list[str]:
    return re.findall(rf"<{tag}>(.*?)</{tag}>", text, re.IGNORECASE | re.DOTALL)


def _ofx_date(value: str | None) -> str:
    if not value or len(value) < 8 or not value[:8].isdigit():
        raise OFXError(f"bad OFX date {value!r}")
    return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()


def parse_ofx(text: str) -> list[ParsedStatement]:
    start = text.upper().find("<OFX>")
    if start < 0:
        raise OFXError("no <OFX> element found")
    body = text[start:]
    institution = _value(body, "ORG")
    statements: list[ParsedStatement] = []

    for tag, acct_tag, kind in (("STMTRS", "BANKACCTFROM", "bank"), ("CCSTMTRS", "CCACCTFROM", "credit")):
        for stmt in _blocks(body, tag):
            acct_block = (_blocks(stmt, acct_tag) or [stmt])[0]
            acct_id = _value(acct_block, "ACCTID")
            if not acct_id:
                raise OFXError("statement has no ACCTID")
            acct_type = (_value(acct_block, "ACCTTYPE") or "").upper()
            if kind == "credit":
                type_hint = "credit"
            elif acct_type in ("SAVINGS", "MONEYMRKT", "CD"):
                type_hint = "savings"
            elif acct_type == "CREDITLINE":
                type_hint = "credit"
            else:
                type_hint = "checking"
            currency = (_value(stmt, "CURDEF") or "USD").upper()

            txns: list[ParsedTxn] = []
            for trn in _blocks(stmt, "STMTTRN"):
                name = _value(trn, "NAME") or _value(trn, "PAYEE") or ""
                memo = _value(trn, "MEMO")
                txns.append(
                    ParsedTxn(
                        posted_date=_ofx_date(_value(trn, "DTPOSTED")),
                        amount=parse_amount(_value(trn, "TRNAMT") or "", currency),
                        description=clean_text(name or memo or "(no description)"),
                        memo=clean_text(memo) if memo and name else None,
                        fitid=clean_text(_value(trn, "FITID"), 64) or None,
                    )
                )

            balances = []
            for bal in _blocks(stmt, "LEDGERBAL"):
                amt, as_of = _value(bal, "BALAMT"), _value(bal, "DTASOF")
                if amt and as_of:
                    balances.append((_ofx_date(as_of), parse_amount(amt, currency)))

            statements.append(
                ParsedStatement(
                    format="ofx",
                    account_key=f"{_value(acct_block, 'BANKID') or institution or ''}:{acct_id}",
                    last4=acct_id[-4:],
                    type_hint=type_hint,
                    institution=institution,
                    currency=currency,
                    txns=txns,
                    balances=balances,
                )
            )
    if not statements:
        raise OFXError("no bank or credit card statements found in OFX file")
    return statements
