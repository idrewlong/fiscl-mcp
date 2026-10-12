"""Read-only bank sync through Teller (https://teller.io/docs/api).

Teller authenticates with mTLS (a client certificate issued to the user's own developer account) plus HTTP
Basic where the username is the enrollment's access token and the password is empty. Everything secret
(access tokens, certificate, key, application id) lives only in the OS keychain.

Read-only is enforced three ways: Connect is only asked for `transactions` and `balance`
(see `connect.py`), the endpoint allowlist below is the only thing `request` will send, and nothing in this
package can reach account numbers, identity, verification, or money movement.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..config import Config
from ..guard import clean_text
from ..importers.types import ParsedStatement, ParsedTxn
from ..money import AmountError, parse_amount
from . import base
from .base import Policy, SyncError

PROVIDER = "teller"
HOSTS = ("api.teller.io",)
BASE_URL = "https://api.teller.io"
PRODUCTS = ["transactions", "balance"]  # Connect never asks for anything else
PAGE_SIZE = 100
MAX_PAGES = 200

_ID = r"[A-Za-z0-9_\-]+"
# The complete list of calls fiscl may make to Teller. Revoking (DELETE) is how `forget` cuts off access.
ROUTES = (
    ("GET", "/accounts"),
    ("GET", rf"/accounts/{_ID}/transactions"),
    ("GET", rf"/accounts/{_ID}/balances"),
    ("DELETE", rf"/accounts/{_ID}"),
    ("DELETE", "/accounts"),
)
POLICY = Policy("Teller", HOSTS, ROUTES)

# --- sign convention ------------------------------------------------------------------------
# Teller documents `amount` only as "signed" and every example is positive, so the real convention is
# UNVERIFIED. This is our best reading: amounts are from the account holder's side (negative = money out),
# which is also fiscl's convention, for both depository and credit accounts. For credit cards the balance
# `ledger` is read as the amount owed (positive), which fiscl stores as negative.
# MUST be verified against a live sandbox enrollment (known debit, deposit, card purchase, refund) before
# release; tests/test_teller.py pins whatever is set here, so a change is a one-line edit in these tables.
TELLER_AMOUNT_SIGN = {"depository": 1, "credit": 1}
TELLER_BALANCE_SIGN = {"depository": 1, "credit": -1}

_SAVINGS = {"savings", "money_market", "certificate_of_deposit", "treasury", "sweep"}


def _teller_amount(raw, account: dict) -> int:
    """Teller transaction amount string -> fiscl minor units (negative = money out). The only place that
    interprets Teller's sign."""
    minor = parse_amount(str(raw), str(account.get("currency") or "USD").upper())
    return minor * TELLER_AMOUNT_SIGN.get(account.get("type"), 1)


def _teller_balance(raw, account: dict) -> int:
    minor = parse_amount(str(raw), str(account.get("currency") or "USD").upper())
    return minor * TELLER_BALANCE_SIGN.get(account.get("type"), 1)


def _type_hint(account: dict) -> str:
    if account.get("type") == "credit":
        return "credit"
    return "savings" if account.get("subtype") in _SAVINGS else "checking"


# --- keychain -----------------------------------------------------------------------------------


def _name(ref: str) -> str:
    return base.secret_name(PROVIDER, ref)


def store_app_credentials(app_id: str, cert_pem: str | None, key_pem: str | None) -> None:
    """Save the user's Teller application id and (outside sandbox) client certificate and key."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,100}", app_id or ""):
        raise SyncError("that doesn't look like a Teller application id")
    base.secret_set(_name("app_id"), app_id)
    if cert_pem and key_pem:
        base.secret_set(_name("cert"), cert_pem)
        base.secret_set(_name("key"), key_pem)


def has_app_credentials(environment: str) -> bool:
    if not base.secret_get(_name("app_id")):
        return False
    return environment == "sandbox" or bool(base.secret_get(_name("cert")) and base.secret_get(_name("key")))


def store_access_token(ref_hash: str, token: str) -> None:
    base.secret_set(_name(ref_hash), token)


def forget_app_credentials() -> None:
    for ref in ("app_id", "cert", "key"):
        base.secret_delete(_name(ref))


def read_pem_files(cert_path: str, key_path: str) -> tuple[str, str]:
    """Read the certificate and key the user downloaded from Teller. The paths are used once and not kept."""
    try:
        cert = Path(cert_path).expanduser().read_text(encoding="utf-8")
        key = Path(key_path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SyncError("couldn't read the certificate or key file") from exc
    if "-----BEGIN CERTIFICATE-----" not in cert or not re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", key):
        raise SyncError("those don't look like a PEM certificate and private key (check which file is which)")
    return cert, key


# --- connector ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TellerItem:
    ref: str  # item_ref_hash; also the keychain ref of the access token
    institution: str | None = None


class TellerConnector:
    provider = PROVIDER

    def __init__(self, cfg: Config, items: list[TellerItem], opener=None, environment: str | None = None) -> None:
        self.cfg = cfg
        self.items = items
        self.environment = environment or cfg.teller_environment
        self._opener = opener
        self.warnings: list[str] = []
        self.item_status: dict[str, str] = {}  # ref -> ok | needs_reauth | error
        self.item_institution: dict[str, str] = {}

    # -- transport --
    def _get_opener(self):
        if self._opener is None:
            ctx: base.SSLContext | None = None
            if self.environment != "sandbox":
                cert, key = base.secret_get(_name("cert")), base.secret_get(_name("key"))
                if not (cert and key):
                    raise SyncError("Teller needs your client certificate. Run `fiscl connect teller`.")
                ctx = base.client_context(cert, key)
            self._opener = base.opener_for(ctx)
        return self._opener

    def _call(self, method: str, token: str, path: str, query: dict | None = None):
        url = BASE_URL + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        auth = _basic(token)
        _, body = base.request(
            POLICY, method, url, secret_headers={"Authorization": f"Basic {auth}"}, opener=self._get_opener()
        )
        if method == "DELETE" or not body:
            return None
        try:
            return json.loads(body.decode())
        except ValueError as exc:
            raise SyncError("Teller sent an unreadable response") from exc

    # -- fetch --
    def fetch(self, start: date, end: date) -> list[ParsedStatement]:
        statements: list[ParsedStatement] = []
        failures: list[SyncError] = []
        for item in self.items:
            token = base.secret_get(_name(item.ref))
            if not token:
                self._fail_item(item, 403)
                failures.append(SyncError("a Teller connection has no stored access token"))
                continue
            try:
                accounts = self._call("GET", token, "/accounts")
                if not isinstance(accounts, list):
                    raise SyncError("Teller sent an unexpected response")
            except SyncError as exc:
                self._fail_item(item, exc.status)
                failures.append(exc)
                continue
            self.item_status[item.ref] = "ok"
            for acct in accounts:
                if isinstance(acct, dict):
                    stmt = self._account(item, token, acct, start, end)
                    if stmt:
                        statements.append(stmt)
        if self.items and len(failures) == len(self.items):
            raise failures[0]
        return statements

    def _fail_item(self, item: TellerItem, status: int | None) -> None:
        if status in (401, 403, 404):
            self.item_status[item.ref] = "needs_reauth"
            self.warnings.append("a Teller connection needs to be re-authorized: run `fiscl connect teller` again")
        else:
            self.item_status[item.ref] = "error"
            self.warnings.append("Teller couldn't be reached for one connection; try again later")

    def _account(self, item: TellerItem, token: str, acct: dict, start: date, end: date) -> ParsedStatement | None:
        acct_id = str(acct.get("id") or "")
        if not re.fullmatch(_ID, acct_id) or acct.get("status") == "closed":
            return None
        currency = str(acct.get("currency") or "USD").upper()
        if len(currency) != 3:
            self.warnings.append("skipped an account with an unsupported currency")
            return None
        try:
            raw_txns = self._transactions(token, acct_id, start, end)
        except SyncError as exc:
            if exc.status == 410:
                return None  # closed since we listed it
            self.warnings.append("skipped an account whose transactions couldn't be read")
            return None
        txns: list[ParsedTxn] = []
        for t in raw_txns:
            parsed = self._txn(t, acct, start, end)
            if parsed:
                txns.append(parsed)
        balances: list[tuple[str, int]] = []
        try:
            bal = self._call("GET", token, f"/accounts/{acct_id}/balances")  # once per account per sync (billed)
            raw = (bal or {}).get("ledger") or (bal or {}).get("available")
            if raw is not None:
                balances.append((end.isoformat(), _teller_balance(raw, acct)))
        except (SyncError, AmountError, AttributeError):
            self.warnings.append("skipped a balance that couldn't be read")
        inst = clean_text((acct.get("institution") or {}).get("name"), 80) or item.institution or None
        if inst:
            self.item_institution[item.ref] = inst
        last4 = str(acct.get("last_four") or "")
        last4 = last4 if re.fullmatch(r"\d{2,4}", last4) else None
        name = clean_text(acct.get("name") or "Account", 80)
        label = f"{clean_text(inst, 40) + ' ' if inst else ''}{name}" + (f" ••{last4}" if last4 else "")
        return ParsedStatement(
            format=PROVIDER,
            txns=txns,
            account_key=f"teller:{acct_id}",
            last4=last4,
            type_hint=_type_hint(acct),
            institution=inst,
            currency=currency,
            balances=balances,
            account_name=label,
        )

    def _transactions(self, token: str, acct_id: str, start: date, end: date) -> list[dict]:
        """Pages run newest to oldest: `from_id` asks for transactions older than that id."""
        out: list[dict] = []
        from_id: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            query: dict = {"count": PAGE_SIZE, "start_date": start.isoformat(), "end_date": end.isoformat()}
            if from_id:
                query["from_id"] = from_id
            page = self._call("GET", token, f"/accounts/{acct_id}/transactions", query)
            if not isinstance(page, list):
                raise SyncError("Teller sent an unexpected response")
            out += [t for t in page if isinstance(t, dict)]
            last = page[-1].get("id") if page and isinstance(page[-1], dict) else None
            if len(page) < PAGE_SIZE or not last or last in seen:
                return out
            if str(page[-1].get("date") or "9999") < start.isoformat():
                return out  # the oldest transaction on this page is already before the window
            seen.add(last)
            from_id = last
        self.warnings.append("transaction history was cut short after many pages")
        return out

    def _txn(self, t: dict, acct: dict, start: date, end: date) -> ParsedTxn | None:
        when = str(t.get("date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
            self.warnings.append("skipped a transaction with no usable date")
            return None
        if not start.isoformat() <= when <= end.isoformat():
            return None
        try:
            amount = _teller_amount(t["amount"], acct)
        except (KeyError, AmountError):
            self.warnings.append("skipped a transaction with an unreadable amount")
            return None
        counterparty = ((t.get("details") or {}).get("counterparty") or {}).get("name")
        desc = clean_text(t.get("description") or counterparty) or "(no description)"
        fitid = clean_text(str(t.get("id") or ""), 64) or None
        return ParsedTxn(
            posted_date=when, amount=amount, description=desc, fitid=fitid, pending=t.get("status") == "pending"
        )

    # -- forget --
    def revoke_token(self, token: str) -> None:
        """Revoke one enrollment's access at Teller (`DELETE /accounts`); used to clean up a failed connect."""
        self._call("DELETE", token, "/accounts")

    def forget(self) -> None:
        """Revoke each enrollment at Teller (`DELETE /accounts`), then remove its token from the keychain.

        If revocation fails for a reason other than "already revoked", the token is kept so the user can retry.
        """
        for item in self.items:
            token = base.secret_get(_name(item.ref))
            if token:
                try:
                    self._call("DELETE", token, "/accounts")
                except SyncError as exc:
                    if exc.status not in (401, 403, 404):
                        raise SyncError(
                            "couldn't revoke the connection at Teller, so it was kept; try again, or revoke it "
                            "from your Teller dashboard"
                        ) from exc
            base.secret_delete(_name(item.ref))


def _basic(token: str) -> str:
    return base64.b64encode(f"{token}:".encode()).decode()
