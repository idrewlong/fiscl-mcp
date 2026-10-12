"""Read-only bank sync through Plaid (https://plaid.com/docs/api/).

Authentication is the user's own Plaid `client_id` and `secret`, sent as `PLAID-CLIENT-ID` / `PLAID-SECRET`
headers (unredirected), plus a per-Item `access_token` in the JSON body as Plaid requires. All three live only in
the OS keychain. The sync cursor is an opaque bookmark and is kept in the `connections` row.

Read-only is enforced in layers: Link is only asked for `PLAID_PRODUCTS` (transactions), the route allowlist below is
the only thing `request` will send, only the host of the configured environment is reachable, and nothing in this
package can reach account numbers, identity, or money movement.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from ..config import Config
from ..guard import clean_text
from ..importers.core import account_hash
from ..importers.types import ParsedStatement, ParsedTxn
from ..money import AmountError, parse_amount
from . import base
from .base import Policy, SyncError

PROVIDER = "plaid"
ENVIRONMENTS = ("sandbox", "production")  # Plaid has no "development" host
HOSTS = {"sandbox": "sandbox.plaid.com", "production": "production.plaid.com"}
PLAID_PRODUCTS = ("transactions",)  # the only products Link is ever asked for
CLIENT_NAME = "fiscl"
PAGE_SIZE = 500  # /transactions/sync allows 1-500
MAX_PAGES = 200
MAX_RESTARTS = 3
MUTATION_ERROR = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
_REAUTH_CODES = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT", "INVALID_ACCESS_TOKEN"}

# The complete list of calls fiscl may make to Plaid. All are POST.
ROUTES = (
    ("POST", "/link/token/create"),
    ("POST", "/item/public_token/exchange"),
    ("POST", "/transactions/sync"),
    ("POST", "/accounts/get"),
    ("POST", "/item/remove"),
)
SANDBOX_ROUTES = (("POST", "/sandbox/public_token/create"),)  # test-only, sandbox environment only
SANDBOX_INSTITUTION = "ins_109508"  # Plaid's "First Platypus Bank"
SANDBOX_USER = "user_good"

_ID = re.compile(r"[A-Za-z0-9_\-]{1,100}")
_TXN_ID = re.compile(r"[A-Za-z0-9_\-+=/.]{1,128}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CODE = re.compile(r"[A-Z_]{1,80}")
_SAVINGS = {"savings", "cd", "money market", "cash management"}


def policy_for(environment: str) -> Policy:
    """The allowlist for one environment. The sandbox-only route exists only when environment is sandbox."""
    if environment not in ENVIRONMENTS:
        raise SyncError("Plaid environment must be sandbox or production")
    routes = ROUTES + (SANDBOX_ROUTES if environment == "sandbox" else ())
    return Policy("Plaid", (HOSTS[environment],), routes)


def base_url(environment: str) -> str:
    return "https://" + HOSTS[environment]


# --- sign convention ---------------------------------------------------------------------------
# Plaid documents `amount` as positive for money leaving the account and negative for money entering it, which
# is the opposite of fiscl. Balances: for credit and loan accounts a positive `current` is the amount owed, which
# fiscl stores as negative (like Teller and SimpleFIN).
PLAID_OWED_TYPES = ("credit", "loan")


def _minor(raw, currency: str) -> int:
    try:
        text = f"{Decimal(str(raw)):f}"  # JSON numbers arrive as floats; avoid exponent notation
    except (InvalidOperation, ValueError) as exc:
        raise AmountError("unreadable amount") from exc
    return parse_amount(text, currency)


def _plaid_amount(raw, currency: str = "USD") -> int:
    """Plaid transaction amount -> fiscl minor units (negative = money out). The only place that flips the sign."""
    return -_minor(raw, currency)


def _plaid_balance(raw, account: dict, currency: str = "USD") -> int:
    """Plaid `balances.current` -> fiscl minor units; amounts owed are negative."""
    minor = _minor(raw, currency)
    return -minor if account.get("type") in PLAID_OWED_TYPES else minor


def _type_hint(account: dict) -> str:
    kind, sub = account.get("type"), str(account.get("subtype") or "")
    if kind == "credit":
        return "credit"
    if kind == "loan":
        return "loan"
    if kind in ("investment", "brokerage"):
        return "brokerage"
    if kind == "depository":
        return "savings" if sub in _SAVINGS else "checking"
    return "other"


# --- keychain -----------------------------------------------------------------------------------


def _name(ref: str) -> str:
    return base.secret_name(PROVIDER, ref)


def store_app_credentials(client_id: str, secret: str) -> None:
    """Save the user's Plaid keys (collected with hidden input by the CLI)."""
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", client_id or "") or not re.fullmatch(r"[A-Za-z0-9]{8,64}", secret or ""):
        raise SyncError("those don't look like a Plaid client_id and secret")
    base.secret_set(_name("client_id"), client_id)
    base.secret_set(_name("secret"), secret)


def load_app_credentials() -> tuple[str, str]:
    client_id, secret = base.secret_get(_name("client_id")), base.secret_get(_name("secret"))
    if not (client_id and secret):
        raise SyncError("no Plaid keys are stored. Run `fiscl connect plaid`.")
    return client_id, secret


def has_app_credentials() -> bool:
    return bool(base.secret_get(_name("client_id")) and base.secret_get(_name("secret")))


def store_access_token(ref_hash: str, token: str) -> None:
    base.secret_set(_name(ref_hash), token)


def forget_app_credentials() -> None:
    for ref in ("client_id", "secret"):
        base.secret_delete(_name(ref))


# --- client -------------------------------------------------------------------------------------


def error_code(exc: SyncError) -> str | None:
    """Plaid's machine-readable error_code from a failed response (e.g. ITEM_LOGIN_REQUIRED), if any."""
    try:
        code = json.loads((exc.body or b"").decode()).get("error_code")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


class PlaidClient:
    """One allowlisted JSON client for one environment."""

    def __init__(self, environment: str, client_id: str, secret: str, opener=None) -> None:
        self.environment = environment
        self.policy = policy_for(environment)
        self._client_id, self._secret = client_id, secret
        self._opener = opener

    def call(self, path: str, body: dict) -> dict:
        try:
            _, raw = base.request(
                self.policy,
                "POST",
                base_url(self.environment) + path,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                secret_headers={"PLAID-CLIENT-ID": self._client_id, "PLAID-SECRET": self._secret},
                data=json.dumps(body).encode(),
                opener=self._opener,
            )
        except SyncError as exc:
            code = error_code(exc)
            if code:
                raise SyncError(f"Plaid rejected the request ({code})", exc.status, exc.body) from exc
            raise
        try:
            data = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise SyncError("Plaid sent an unreadable response") from exc
        if not isinstance(data, dict):
            raise SyncError("Plaid sent an unexpected response")
        return data


def link_token_body() -> dict:
    """The exact body of /link/token/create. `products` is PLAID_PRODUCTS and nothing else; no redirect_uri
    (desktop web Link doesn't need one, and http localhost is sandbox-only anyway)."""
    return {
        "client_name": CLIENT_NAME,
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": secrets.token_hex(16)},  # random, no PII
        "products": list(PLAID_PRODUCTS),
    }


def create_link_token(client: PlaidClient) -> str:
    token = client.call("/link/token/create", link_token_body()).get("link_token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9\-_]{10,300}", token):
        raise SyncError("Plaid didn't return a link token")
    return token


def exchange_public_token(client: PlaidClient, public_token: str) -> tuple[str, str]:
    """Trade the one-time public token for (access_token, item_id)."""
    data = client.call("/item/public_token/exchange", {"public_token": public_token})
    access, item = data.get("access_token"), data.get("item_id")
    if not (isinstance(access, str) and re.fullmatch(r"[A-Za-z0-9\-_]{10,300}", access)):
        raise SyncError("Plaid's result had no usable access token")
    if not (isinstance(item, str) and _ID.fullmatch(item)):
        raise SyncError("Plaid's result had an invalid item id")
    return access, item


def sandbox_public_token(client: PlaidClient) -> str:
    """Test only: mint a sandbox public token without a browser. Refused outside the sandbox environment."""
    if client.environment != "sandbox":
        raise SyncError("sandbox helpers only run against the Plaid sandbox")
    data = client.call(
        "/sandbox/public_token/create",
        {
            "institution_id": SANDBOX_INSTITUTION,
            "initial_products": list(PLAID_PRODUCTS),
            "options": {"override_username": SANDBOX_USER},
        },
    )
    token = data.get("public_token")
    if not isinstance(token, str):
        raise SyncError("Plaid didn't return a sandbox public token")
    return token


# --- connector ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaidItem:
    ref: str  # item_ref_hash; also the keychain ref of the access token
    institution: str | None = None
    cursor: str | None = None


class PlaidConnector:
    provider = PROVIDER

    def __init__(self, cfg: Config, items: list[PlaidItem], opener=None, environment: str | None = None) -> None:
        self.cfg = cfg
        self.items = items
        self.environment = environment or cfg.plaid_environment
        self._opener = opener
        self._client: PlaidClient | None = None
        self.warnings: list[str] = []
        self.item_status: dict[str, str] = {}  # ref -> ok | needs_reauth | error
        self.item_institution: dict[str, str] = {}
        self._cursors: dict[str, str] = {}  # saved by finalize() once the import succeeded
        self._removed: list[tuple[str, list[str]]] = []  # (transaction id, this Item's account hashes) to delete

    def _get_client(self) -> PlaidClient:
        if self._client is None:
            client_id, secret = load_app_credentials()
            self._client = PlaidClient(self.environment, client_id, secret, self._opener)
        return self._client

    # -- fetch --
    def fetch(self, start: date, end: date) -> list[ParsedStatement]:
        """Pull the changes since each Item's cursor. `start` is unused: the cursor, not a date window, decides
        what is new. Nothing is written here; `finalize` stores cursors and applies removals after the import."""
        client = self._get_client()
        statements: list[ParsedStatement] = []
        failures: list[SyncError] = []
        for item in self.items:
            token = base.secret_get(_name(item.ref))
            if not token:
                self._fail_item(item, None, "ITEM_NOT_FOUND")
                failures.append(SyncError("a Plaid connection has no stored access token"))
                continue
            try:
                statements += self._fetch_item(client, item, token, end)
            except SyncError as exc:
                self._fail_item(item, exc.status, error_code(exc))
                failures.append(exc)
                continue
            self.item_status[item.ref] = "ok"
        if self.items and len(failures) == len(self.items):
            raise failures[0]
        return statements

    def _fail_item(self, item: PlaidItem, status: int | None, code: str | None) -> None:
        if code in _REAUTH_CODES or code == "ITEM_NOT_FOUND":
            self.item_status[item.ref] = "needs_reauth"
            self.warnings.append("a Plaid connection needs to be re-authorized: run `fiscl connect plaid` again")
        else:
            self.item_status[item.ref] = "error"
            self.warnings.append("Plaid couldn't be reached for one connection; try again later")

    def _fetch_item(self, client: PlaidClient, item: PlaidItem, token: str, end: date) -> list[ParsedStatement]:
        got = client.call("/accounts/get", {"access_token": token}).get("accounts")
        if not isinstance(got, list):
            raise SyncError("Plaid sent an unexpected response")
        accounts = {
            str(a.get("account_id")): a for a in got if isinstance(a, dict) and _ID.fullmatch(str(a.get("account_id")))
        }
        added, modified, removed, cursor = self._sync(client, token, item.cursor or "")
        if cursor and cursor != (item.cursor or ""):
            self._cursors[item.ref] = cursor
        elif not cursor and not (added or modified):
            self.warnings.append("Plaid is still preparing this connection's transactions; sync again in a few minutes")
        # A removal may only touch accounts that belong to this Item, whatever ids Plaid sends back.
        mine = [account_hash(f"{PROVIDER}:{aid}") for aid in accounts]
        self._removed += [
            (str(r["transaction_id"]), mine) for r in removed if _TXN_ID.fullmatch(str(r.get("transaction_id")))
        ]

        by_account: dict[str, list[ParsedTxn]] = {k: [] for k in accounts}
        for flag, rows in ((False, added), (True, modified)):
            for t in rows:
                acct = accounts.get(str(t.get("account_id")))
                if acct is None:
                    self.warnings.append("skipped a transaction for an account Plaid didn't list")
                    continue
                parsed = self._txn(t, acct, flag)
                if parsed:
                    by_account[str(t["account_id"])].append(parsed)
        inst = item.institution
        return [s for s in (self._statement(a, by_account[aid], inst, end) for aid, a in accounts.items()) if s]

    def _sync(self, client: PlaidClient, token: str, start_cursor: str):
        """Walk /transactions/sync to the end. A mutation error means the data changed under us: throw away this
        pass and restart from the cursor we started with, as Plaid specifies."""
        for _ in range(MAX_RESTARTS + 1):
            added: list[dict] = []
            modified: list[dict] = []
            removed: list[dict] = []
            cursor = start_cursor
            try:
                for _page in range(MAX_PAGES):
                    body: dict = {"access_token": token, "count": PAGE_SIZE}
                    if cursor:
                        body["cursor"] = cursor
                    data = client.call("/transactions/sync", body)
                    for bucket, key in ((added, "added"), (modified, "modified"), (removed, "removed")):
                        rows = data.get(key) or []
                        if not isinstance(rows, list):
                            raise SyncError("Plaid sent an unexpected response")
                        bucket += [r for r in rows if isinstance(r, dict)]
                    nxt = data.get("next_cursor")
                    cursor = nxt if isinstance(nxt, str) else cursor
                    if not data.get("has_more"):
                        return added, modified, removed, cursor
                self.warnings.append("transaction history was cut short after many pages; the next sync continues")
                return added, modified, removed, cursor
            except SyncError as exc:
                if error_code(exc) != MUTATION_ERROR:
                    raise
        raise SyncError("Plaid's data kept changing during the sync; try again in a minute")

    def _statement(self, acct: dict, txns: list[ParsedTxn], institution: str | None, end: date):
        bal = acct.get("balances") if isinstance(acct.get("balances"), dict) else {}
        if bal.get("unofficial_currency_code") and not bal.get("iso_currency_code"):
            self.warnings.append("skipped an account with an unsupported currency")
            return None
        currency = str(bal.get("iso_currency_code") or "USD").upper()
        if len(currency) != 3:
            self.warnings.append("skipped an account with an unsupported currency")
            return None
        txns = [t for t in txns if t is not None]
        balances: list[tuple[str, int]] = []
        if bal.get("current") is not None:
            try:
                balances.append((end.isoformat(), _plaid_balance(bal["current"], acct, currency)))
            except AmountError:
                self.warnings.append("skipped a balance that couldn't be read")
        mask = str(acct.get("mask") or "")
        last4 = mask if re.fullmatch(r"\d{2,4}", mask) else None
        name = clean_text(acct.get("name") or "Account", 80)
        label = f"{clean_text(institution, 40) + ' ' if institution else ''}{name}" + (f" ••{last4}" if last4 else "")
        return ParsedStatement(
            format=PROVIDER,
            txns=txns,
            account_key=f"plaid:{acct['account_id']}",
            last4=last4,
            type_hint=_type_hint(acct),
            institution=institution or None,
            currency=currency,
            balances=balances,
            account_name=label,
        )

    def _txn(self, t: dict, acct: dict, modified: bool) -> ParsedTxn | None:
        pending = bool(t.get("pending"))
        when = str((t.get("authorized_date") or t.get("date")) if pending else t.get("date") or "")
        fitid = str(t.get("transaction_id") or "")
        if not _DATE.fullmatch(when) or not _TXN_ID.fullmatch(fitid):
            self.warnings.append("skipped a transaction with no usable date or id")
            return None
        currency = str((acct.get("balances") or {}).get("iso_currency_code") or "USD").upper()
        code = t.get("iso_currency_code")
        if code and str(code).upper() != currency:
            self.warnings.append("skipped a transaction in a different currency than its account")
            return None
        try:
            amount = _plaid_amount(t["amount"], currency)
        except (KeyError, AmountError):
            self.warnings.append("skipped a transaction with an unreadable amount")
            return None
        desc = clean_text(t.get("merchant_name") or t.get("name")) or "(no description)"
        prior = t.get("pending_transaction_id")
        replaces = prior if not pending and isinstance(prior, str) and _TXN_ID.fullmatch(prior) else None
        return ParsedTxn(
            posted_date=when,
            amount=amount,
            description=desc,
            fitid=fitid,
            pending=pending,
            replaces_fitid=replaces,
            modified=modified,
        )

    # -- after the import --
    def finalize(self, conn: sqlite3.Connection) -> None:
        """Called only after the import succeeded: delete what Plaid removed (only rows this connector imported,
        never anything the user created) and advance each Item's cursor. Safe to repeat."""
        for fitid, hashes in self._removed:
            if not hashes:
                continue
            marks = ",".join("?" * len(hashes))
            conn.execute(
                f"""DELETE FROM transactions WHERE fitid = ?
                    AND account_id IN (SELECT id FROM accounts WHERE acct_hash IN ({marks}))
                    AND import_id IN (SELECT id FROM imports WHERE format = ?)""",
                (fitid, *hashes, PROVIDER),
            )
        for ref, cursor in self._cursors.items():
            conn.execute(
                "UPDATE connections SET cursor = ? WHERE provider = ? AND item_ref_hash = ?", (cursor, PROVIDER, ref)
            )
        self._removed, self._cursors = [], {}

    # -- forget --
    def forget(self) -> None:
        """Remove each Item at Plaid (`/item/remove`), then delete its token from the keychain.

        ITEM_NOT_FOUND (or an invalid token) means Plaid already dropped it. Any other failure keeps the token so
        the user can retry or remove the Item from the Plaid dashboard.
        """
        client = self._get_client() if any(base.secret_get(_name(i.ref)) for i in self.items) else None
        for item in self.items:
            token = base.secret_get(_name(item.ref))
            if token and client:
                try:
                    client.call("/item/remove", {"access_token": token})
                except SyncError as exc:
                    if error_code(exc) not in ("ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"):
                        raise SyncError(
                            "couldn't remove the connection at Plaid, so it was kept; try again, or remove it "
                            "from your Plaid dashboard"
                        ) from exc
            base.secret_delete(_name(item.ref))
