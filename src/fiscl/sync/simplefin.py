"""Read-only bank sync through SimpleFIN Bridge (https://www.simplefin.org/protocol.html).

The SimpleFIN protocol is read-only by design: it shares balances and transactions, never moves money.
The access URL embeds credentials, so it lives in the OS keychain only (never config, env, logs, or
the database), and requests may only go to an allowlisted host.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..guard import clean_text
from ..importers.core import ImportResult, import_statements
from ..importers.types import ParsedStatement, ParsedTxn
from ..money import AmountError, parse_amount
from . import base
from .base import TIMEOUT, Policy, SyncError, open_url

__all__ = ["SyncError", "open_url"]

# Legacy keychain entry, kept so existing connections keep working (other providers use fiscl:<provider>:<ref>).
KEYRING_SERVICE = base.KEYRING_SERVICE
KEYRING_USER = "simplefin_access_url"
DEFAULT_ALLOWED_HOSTS = ("bridge.simplefin.org", "beta-bridge.simplefin.org")
MAX_WINDOW_DAYS = 90
_PATH = r"(?:/[\w.\-~%=]+)*"


def _policy(allowed: tuple[str, ...]) -> Policy:
    return Policy(
        "SimpleFIN",
        tuple(allowed),
        routes=(("GET", _PATH + "/accounts"), ("POST", _PATH + "/claim/[\\w.\\-~%=]+")),
    )


def _keyring():
    return base.keyring_backend()


def _check_access_url(access_url: str, allowed: tuple[str, ...]) -> urllib.parse.SplitResult:
    """Validate scheme/host of an access URL (which embeds credentials) via the shared policy."""
    parts = urllib.parse.urlsplit(access_url)
    netloc = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    stripped = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/") + "/accounts", "", ""))
    _policy(allowed).check("GET", stripped)
    return parts


def claim(setup_token: str, allowed: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS, opener=open_url) -> str:
    """Exchange a one-time setup token for an access URL and store it in the keychain."""
    try:
        claim_url = base64.b64decode(setup_token.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise SyncError("that doesn't look like a SimpleFIN setup token") from exc
    try:
        _, body = base.request(
            _policy(allowed), "POST", claim_url, headers={"Content-Length": "0"}, data=b"", opener=opener
        )
    except SyncError as exc:
        if exc.status is not None and not 300 <= exc.status < 400:
            raise SyncError(f"claim failed with HTTP {exc.status}; setup tokens can only be used once") from exc
        raise
    access_url = body.decode().strip()
    _check_access_url(access_url, allowed)
    _keyring().set_password(KEYRING_SERVICE, KEYRING_USER, access_url)
    return urllib.parse.urlsplit(access_url).hostname or ""


def forget() -> None:
    kr = _keyring()
    try:
        kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass


def is_connected() -> bool:
    try:
        return bool(_keyring().get_password(KEYRING_SERVICE, KEYRING_USER))
    except SyncError:
        return False


def _guess_type(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("credit", "card", "visa", "mastercard", "amex", "discover")):
        return "credit"
    if any(k in n for k in ("saving", "money market", "mmkt")):
        return "savings"
    if any(k in n for k in ("loan", "mortgage", "auto")):
        return "loan"
    if any(k in n for k in ("brokerage", "ira", "401k", "invest")):
        return "brokerage"
    return "checking"


def parse_accounts(payload: dict, tz: ZoneInfo) -> tuple[list[ParsedStatement], list[str]]:
    statements: list[ParsedStatement] = []
    warnings = [clean_text(str(e), 200) for e in payload.get("errors", [])]

    def to_date(epoch) -> str:
        return datetime.fromtimestamp(int(epoch), tz=UTC).astimezone(tz).date().isoformat()

    for acct in payload.get("accounts", []):
        currency = str(acct.get("currency", "USD")).upper()
        if len(currency) != 3:
            warnings.append(f"skipped account with custom currency {currency[:20]!r}")
            continue
        txns = []
        for t in acct.get("transactions", []):
            try:
                amt = parse_amount(str(t["amount"]), currency)
            except (KeyError, AmountError):
                warnings.append("skipped a transaction with an unreadable amount")
                continue
            posted = t.get("posted") or 0
            pending = bool(t.get("pending")) or not posted
            when = posted or t.get("transacted_at")
            if not when:
                warnings.append("skipped a pending transaction with no date")
                continue
            desc = t.get("payee") or t.get("description") or "(no description)"
            txns.append(
                ParsedTxn(
                    posted_date=to_date(when),
                    amount=amt,
                    description=clean_text(desc),
                    memo=clean_text(t.get("memo")) or None,
                    fitid=clean_text(str(t.get("id", "")), 64) or None,
                    pending=pending,
                )
            )
        name = clean_text(acct.get("name") or "Account", 80)
        org = acct.get("org") or {}
        balances = []
        if acct.get("balance") is not None and acct.get("balance-date"):
            try:
                balances.append((to_date(acct["balance-date"]), parse_amount(str(acct["balance"]), currency)))
            except AmountError:
                pass
        statements.append(
            ParsedStatement(
                format="simplefin",
                txns=txns,
                account_key=f"simplefin:{acct.get('id')}",
                last4=None,
                type_hint=_guess_type(name),
                institution=clean_text(org.get("name"), 80) or None,
                currency=currency,
                balances=balances,
                account_name=f"{clean_text(org.get('name'), 40) + ' ' if org.get('name') else ''}{name}",
            )
        )
    return statements, warnings


def fetch(access_url: str, start: date, end: date, allowed: tuple[str, ...], opener=open_url) -> dict:
    parts = _check_access_url(access_url, allowed)
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    base_url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    query = urllib.parse.urlencode(
        {
            "start-date": int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp()),
            "end-date": int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp()) + 86400,
            "pending": 1,
        }
    )
    token = base64.b64encode(
        f"{urllib.parse.unquote(parts.username or '')}:{urllib.parse.unquote(parts.password or '')}".encode()
    ).decode()
    try:
        _, body = base.request(
            _policy(allowed),
            "GET",
            f"{base_url}/accounts?{query}",
            secret_headers={"Authorization": f"Basic {token}"},
            opener=opener,
            timeout=TIMEOUT,
        )
    except SyncError as exc:
        if exc.status == 403:
            raise SyncError("SimpleFIN rejected the stored access (403). Run `fiscl sync setup` again.", 403) from exc
        raise
    return json.loads(body.decode())


class SimpleFinConnector:
    """`Connector` over the SimpleFIN access URL in the keychain (the CLI/MCP path uses `sync()` below, which
    also de-duplicates identical payloads)."""

    provider = "simplefin"

    def __init__(self, cfg: Config, access_url: str | None = None, opener=open_url) -> None:
        self.cfg, self._access_url, self._opener = cfg, access_url, opener
        self.warnings: list[str] = []

    def fetch(self, start: date, end: date) -> list[ParsedStatement]:
        url = self._access_url or _keyring().get_password(KEYRING_SERVICE, KEYRING_USER)
        if not url:
            raise SyncError("no SimpleFIN connection yet. Run `fiscl connect simplefin`.")
        payload = fetch(url, start, end, DEFAULT_ALLOWED_HOSTS, self._opener)
        statements, warns = parse_accounts(payload, ZoneInfo(self.cfg.timezone))
        self.warnings += warns
        return statements

    def forget(self) -> None:
        forget()


def sync(
    conn,
    cfg: Config,
    days: int | None = None,
    today: date | None = None,
    access_url: str | None = None,
    opener=open_url,
) -> dict:
    """Pull recent transactions and balances for every SimpleFIN account and import them."""
    today = today or date.today()
    allowed = DEFAULT_ALLOWED_HOSTS
    if access_url is None:
        access_url = _keyring().get_password(KEYRING_SERVICE, KEYRING_USER)
        if not access_url:
            raise SyncError("no SimpleFIN connection yet. Run `fiscl sync setup` with a setup token.")
    if days is None:
        last = conn.execute(
            "SELECT MAX(imported_at) FROM imports WHERE format = 'simplefin' AND status = 'ok'"
        ).fetchone()[0]
        days = 14 if last else MAX_WINDOW_DAYS
    days = max(1, min(int(days), 365))
    tz = ZoneInfo(cfg.timezone)
    results: list[ImportResult] = []
    warnings: list[str] = []
    window_end = today
    remaining = days
    while remaining > 0:
        span = min(remaining, MAX_WINDOW_DAYS)
        window_start = window_end - timedelta(days=span - 1)
        payload = fetch(access_url, window_start, window_end, allowed, opener)
        statements, warns = parse_accounts(payload, tz)
        warnings += warns
        digest = "simplefin:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        already = conn.execute("SELECT 1 FROM imports WHERE file_sha256 = ? AND status = 'ok'", (digest,)).fetchone()
        if statements and not already:
            label = f"SimpleFIN sync {window_start}..{window_end}"
            results.append(import_statements(conn, cfg, statements, label, digest, post_process=False))
        window_end = window_start - timedelta(days=1)
        remaining -= span
    from ..importers.core import post_import

    post = post_import(conn, cfg, today=today)
    return {
        "windows": len(results),
        "accounts": sorted({a for r in results for a in r.accounts}),
        "rows_added": sum(r.rows_added for r in results),
        "rows_skipped": sum(r.rows_skipped for r in results),
        "warnings": warnings,
        "post_import": post,
    }
