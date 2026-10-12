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
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..guard import clean_text
from ..importers.core import ImportResult, import_statements
from ..importers.types import ParsedStatement, ParsedTxn
from ..money import AmountError, parse_amount

KEYRING_SERVICE = "fiscl"
KEYRING_USER = "simplefin_access_url"
DEFAULT_ALLOWED_HOSTS = ("bridge.simplefin.org", "beta-bridge.simplefin.org")
MAX_WINDOW_DAYS = 90
TIMEOUT = 30


class SyncError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the allowlist is checked on the URL we build, and a redirect could
    point anywhere (including plain http)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# TLS certificates and hostnames are verified by urllib's default HTTPS context.
_OPENER = urllib.request.build_opener(_NoRedirect)


def open_url(req: urllib.request.Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


def _http_error(exc: urllib.error.HTTPError) -> SyncError:
    if 300 <= exc.code < 400:
        return SyncError(f"SimpleFIN answered with a redirect (HTTP {exc.code}); fiscl doesn't follow redirects")
    return SyncError(f"SimpleFIN returned HTTP {exc.code}")


def _keyring():
    try:
        import keyring  # optional dependency: pip install "fiscl[sync]"
        from keyring.backends.fail import Keyring as FailKeyring
    except ImportError as exc:
        raise SyncError('bank sync needs the keyring package: pip install "fiscl[sync]"') from exc
    if isinstance(keyring.get_keyring(), FailKeyring):
        raise SyncError("no OS keychain is available on this machine; fiscl won't store bank credentials in plain text")
    return keyring


def _check_host(url: str, allowed: tuple[str, ...]) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise SyncError("SimpleFIN URLs must use https")
    if (parts.hostname or "").lower() not in allowed:
        raise SyncError(f"host {parts.hostname!r} is not in the SimpleFIN allowlist {list(allowed)}")
    return parts


def claim(setup_token: str, allowed: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS, opener=open_url) -> str:
    """Exchange a one-time setup token for an access URL and store it in the keychain."""
    try:
        claim_url = base64.b64decode(setup_token.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise SyncError("that doesn't look like a SimpleFIN setup token") from exc
    _check_host(claim_url, allowed)
    req = urllib.request.Request(claim_url, method="POST", data=b"", headers={"Content-Length": "0"})
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            access_url = resp.read().decode().strip()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise _http_error(exc) from exc
        raise SyncError(f"claim failed with HTTP {exc.code}; setup tokens can only be used once") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"couldn't reach SimpleFIN: {exc.reason}") from exc
    _check_host(access_url, allowed)
    _keyring().set_password(KEYRING_SERVICE, KEYRING_USER, access_url)
    return urllib.parse.urlsplit(access_url).hostname or ""


def forget() -> None:
    kr = _keyring()
    try:
        kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass


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
    parts = _check_host(access_url, allowed)
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    base = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    query = urllib.parse.urlencode(
        {
            "start-date": int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp()),
            "end-date": int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp()) + 86400,
            "pending": 1,
        }
    )
    req = urllib.request.Request(f"{base}/accounts?{query}")
    token = base64.b64encode(
        f"{urllib.parse.unquote(parts.username or '')}:{urllib.parse.unquote(parts.password or '')}".encode()
    ).decode()
    req.add_unredirected_header("Authorization", f"Basic {token}")
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise SyncError("SimpleFIN rejected the stored access (403). Run `fiscl sync setup` again.") from exc
        raise _http_error(exc) from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"couldn't reach SimpleFIN: {exc.reason}") from exc


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
