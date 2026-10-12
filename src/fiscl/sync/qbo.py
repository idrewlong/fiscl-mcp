"""Read-only QuickBooks Online connector (SANDBOX ONLY in this release).

QuickBooks has no read-only OAuth scope (`com.intuit.quickbooks.accounting` can write), so read-only is enforced in
fiscl's code, in layers, all before any socket opens:

* `policy_for("api")` allows `GET` and nothing else, on four fixed routes (`query`, `companyinfo`, and the
  `ProfitAndLoss` / `AgedReceivables` reports) under the sandbox API host. There is no POST/PUT/DELETE route on the API
  host, so create, update, delete, void, send and batch calls cannot be expressed.
* The policy's `query_check` pins the query string: `query` must fully match a `SELECT * FROM
  Invoice|Customer|Payment ...` shape that only `build_query` produces, `minorversion` must equal `MINOR_VERSION`, and
  reports accept only their documented date/basis parameters.
* The sign-in hosts accept exactly one POST route each (token, revoke) and no query string.
* Production is refused (`require_sandbox`), and no production host exists in this file.
* Tokens, the client secret and the realm id live only in the OS keychain. The refresh token is stored before the
  access token it came with is used; the access token is never persisted.

The stored token could still write to the user's books if it were stolen; fiscl's code cannot.

Unverified against a live sandbox (see docs/research/2026-10-11-quickbooks-online-readonly.md): report parameters,
the AgedReceivables report, and the exact error bodies. Reports are parsed by group name, never by row position.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..config import Config
from ..guard import clean_text
from ..money import AmountError, to_minor
from . import base, connections
from .base import Policy, SyncError

PROVIDER = "qbo"
SANDBOX_API_HOST = "sandbox-quickbooks.api.intuit.com"
TOKEN_HOST = "oauth.platform.intuit.com"
REVOKE_HOST = "developer.api.intuit.com"
AUTHORIZE_HOST = "appcenter.intuit.com"  # opened only in the user's browser; fiscl never requests it
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "75"
PAGE_SIZE = 1000
MAX_PAGES = 50  # per entity per run
MAX_RETRIES = 3
RATE_LIMIT_WAIT = 60.0  # seconds to wait on a 429 without a usable Retry-After (Intuit: wait 60 s)
MAX_WAIT = 120.0
ENTITIES = ("Invoice", "Customer", "Payment")
REAUTH_MESSAGE = "QuickBooks needs to be reconnected: run fiscl connect qbo --reconnect"
PRODUCTION_MESSAGE = (
    "QuickBooks production access is disabled in this release: fiscl supports the QuickBooks sandbox only, until a "
    'human has verified Intuit\'s production requirements. Set [connectors.qbo] environment = "sandbox".'
)

_REALM = re.compile(r"[0-9]{1,20}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_STAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}"
_STAMP_RX = re.compile(_STAMP)
_ACCESS = re.compile(r"[A-Za-z0-9\-_.~+/=]{10,4096}")
_REFRESH = re.compile(r"[A-Za-z0-9\-_.]{10,512}")
_CLIENT = re.compile(r"[A-Za-z0-9]{8,100}")
_CODE = re.compile(r"[A-Za-z0-9\-_.~]{1,512}")
_QUERY_RX = re.compile(
    rf"SELECT \* FROM (?:{'|'.join(ENTITIES)})"
    rf"(?: WHERE MetaData\.LastUpdatedTime >= '{_STAMP}')?"
    r" ORDERBY Id STARTPOSITION [0-9]{1,7} MAXRESULTS (?:1000|[0-9]{1,3})"
)

# The complete list of calls fiscl may make to QuickBooks. All are GET.
API_ROUTES = (
    ("GET", r"/v3/company/([0-9]{1,20})/query"),
    ("GET", r"/v3/company/([0-9]{1,20})/companyinfo/\1"),
    ("GET", r"/v3/company/([0-9]{1,20})/reports/ProfitAndLoss"),
    ("GET", r"/v3/company/([0-9]{1,20})/reports/AgedReceivables"),
)
TOKEN_ROUTES = (("POST", "/oauth2/v1/tokens/bearer"),)
REVOKE_ROUTES = (("POST", "/v2/oauth2/tokens/revoke"),)


# --- environment ----------------------------------------------------------------------------------


def require_sandbox(cfg: Config) -> None:
    """Production is disabled until a human verifies Intuit's production requirements (DECISIONS 2026-10-11)."""
    if cfg.qbo_environment != "sandbox":
        raise SyncError(PRODUCTION_MESSAGE)


def redirect_uri(port: int) -> str:
    """Intuit allows plain-http localhost redirects for sandbox only, and rejects IP addresses; the URI must match
    the one registered in the user's Intuit app exactly."""
    return f"http://localhost:{int(port)}/qbo/callback"


def authorize_url(client_id: str, redirect: str, state: str) -> str:
    """Where the user's browser goes to approve access. Built here, opened by `fiscl connect qbo`, never fetched."""
    query = urllib.parse.urlencode(
        {"client_id": client_id, "response_type": "code", "scope": SCOPE, "redirect_uri": redirect, "state": state},
        quote_via=urllib.parse.quote,
    )
    return f"https://{AUTHORIZE_HOST}/connect/oauth2?{query}"


# --- the allowlist --------------------------------------------------------------------------------


def _valid_date(value: str) -> bool:
    if not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_query(raw: str) -> dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    except ValueError as exc:
        raise SyncError("that QuickBooks query string isn't allowed") from exc
    params: dict[str, str] = {}
    for key, value in pairs:
        if key in params:
            raise SyncError("that QuickBooks query string isn't allowed")
        params[key] = value
    return params


def _check_api_query(method: str, path: str, raw: str) -> None:
    refuse = SyncError("that QuickBooks query isn't allowed (read-only connector)")
    if method != "GET":
        raise refuse
    params = _parse_query(raw)
    if params.pop("minorversion", None) != MINOR_VERSION:
        raise refuse
    leaf = path.rsplit("/", 1)[-1]
    if leaf == "query":
        if set(params) != {"query"} or not _QUERY_RX.fullmatch(params["query"]):
            raise refuse
    elif leaf == "ProfitAndLoss":
        method_ok = params.get("accounting_method") in ("Cash", "Accrual")
        dates_ok = all(_valid_date(params.get(k, "")) for k in ("start_date", "end_date"))
        if set(params) != {"start_date", "end_date", "accounting_method"} or not (method_ok and dates_ok):
            raise refuse
    elif leaf == "AgedReceivables":
        if not (set(params) <= {"report_date"} and all(_valid_date(v) for v in params.values())):
            raise refuse
    elif params:  # companyinfo/<realm> takes no parameters
        raise refuse


def _check_no_query(method: str, path: str, raw: str) -> None:
    if raw:
        raise SyncError("sign-in requests carry no query string")


def policy_for(kind: str) -> Policy:
    """The allowlist for one QuickBooks host family: api (GET only), token (POST) or revoke (POST)."""
    if kind == "api":
        return Policy("QuickBooks", (SANDBOX_API_HOST,), API_ROUTES, query_check=_check_api_query)
    if kind == "token":
        return Policy("QuickBooks sign-in", (TOKEN_HOST,), TOKEN_ROUTES, query_check=_check_no_query)
    if kind == "revoke":
        return Policy("QuickBooks sign-in", (REVOKE_HOST,), REVOKE_ROUTES, query_check=_check_no_query)
    raise SyncError("unknown QuickBooks endpoint")


def build_query(entity: str, since: str | None, start: int, size: int = PAGE_SIZE) -> str:
    """The only place query text is built. `since` is a QuickBooks LastUpdatedTime stamp, or None for everything."""
    if entity not in ENTITIES or not (1 <= start <= 9_999_999) or not (1 <= size <= PAGE_SIZE):
        raise SyncError("that QuickBooks query isn't allowed (read-only connector)")
    where = ""
    if since is not None:
        if not _STAMP_RX.fullmatch(since):
            raise SyncError("that QuickBooks query isn't allowed (read-only connector)")
        where = f" WHERE MetaData.LastUpdatedTime >= '{since}'"
    return f"SELECT * FROM {entity}{where} ORDERBY Id STARTPOSITION {start} MAXRESULTS {size}"


class ReauthNeeded(SyncError):
    """The stored grant no longer works (revoked, expired, or the app was disconnected)."""

    def __init__(self) -> None:
        super().__init__(REAUTH_MESSAGE)


# --- transport ------------------------------------------------------------------------------------


class Http:
    """Sends allowlisted requests, waiting out QuickBooks' 429 (at most MAX_RETRIES times)."""

    def __init__(self, opener=None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._opener = opener
        self._sleep = sleep

    def send(self, kind: str, method: str, url: str, *, headers=None, secret_headers=None, data=None) -> bytes:
        for attempt in range(MAX_RETRIES + 1):
            try:
                _, body = base.request(
                    policy_for(kind),
                    method,
                    url,
                    headers=headers,
                    secret_headers=secret_headers,
                    data=data,
                    opener=self._opener,
                )
                return body
            except SyncError as exc:
                if exc.status != 429:
                    raise
                if attempt == MAX_RETRIES:
                    raise SyncError("QuickBooks is rate limiting fiscl; try again in a few minutes", 429) from exc
                self._sleep(min(max(exc.retry_after or RATE_LIMIT_WAIT, 1.0), MAX_WAIT))
        raise AssertionError("unreachable")


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode(), parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SyncError("QuickBooks sent an unreadable response") from exc
    if not isinstance(data, dict):
        raise SyncError("QuickBooks sent an unexpected response")
    return data


def _basic(client_id: str, secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()}


def _error_code(exc: SyncError) -> str | None:
    try:
        code = json.loads((exc.body or b"").decode()).get("error")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,60}", code) else None


def _token_call(http: Http, client_id: str, secret: str, fields: dict[str, str]) -> tuple[str, str]:
    try:
        raw = http.send(
            "token",
            "POST",
            f"https://{TOKEN_HOST}{TOKEN_ROUTES[0][1]}",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            secret_headers=_basic(client_id, secret),
            data=urllib.parse.urlencode(fields).encode(),
        )
    except SyncError as exc:
        if exc.status in (400, 401) and _error_code(exc) == "invalid_grant":
            raise ReauthNeeded() from exc
        raise
    data = _json(raw)
    access, refresh = data.get("access_token"), data.get("refresh_token")
    if not (isinstance(access, str) and _ACCESS.fullmatch(access) and isinstance(refresh, str)):
        raise SyncError("QuickBooks didn't return usable tokens")
    if not _REFRESH.fullmatch(refresh):
        raise SyncError("QuickBooks didn't return usable tokens")
    return access, refresh


def exchange_code(http: Http, client_id: str, secret: str, code: str, redirect: str) -> tuple[str, str]:
    """Trade the one-time authorization code for (access_token, refresh_token). No PKCE: Intuit doesn't document it."""
    if not _CODE.fullmatch(code or ""):
        raise SyncError("QuickBooks' result had an invalid authorization code")
    return _token_call(
        http, client_id, secret, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect}
    )


def revoke_token(http: Http, client_id: str, secret: str, refresh_token: str) -> None:
    http.send(
        "revoke",
        "POST",
        f"https://{REVOKE_HOST}{REVOKE_ROUTES[0][1]}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        secret_headers=_basic(client_id, secret),
        data=json.dumps({"token": refresh_token}).encode(),
    )


# --- keychain -------------------------------------------------------------------------------------


def _name(ref: str) -> str:
    return base.secret_name(PROVIDER, ref)


def store_app_credentials(client_id: str, secret: str) -> None:
    """Save the user's Intuit app keys (collected with hidden input by the CLI)."""
    if not _CLIENT.fullmatch(client_id or "") or not _CLIENT.fullmatch(secret or ""):
        raise SyncError("those don't look like an Intuit app client ID and secret")
    base.secret_set(_name("client_id"), client_id)
    base.secret_set(_name("client_secret"), secret)


def load_app_credentials() -> tuple[str, str]:
    client_id, secret = base.secret_get(_name("client_id")), base.secret_get(_name("client_secret"))
    if not (client_id and secret):
        raise SyncError("no QuickBooks app keys are stored. Run `fiscl connect qbo`.")
    return client_id, secret


def has_app_credentials() -> bool:
    return bool(base.secret_get(_name("client_id")) and base.secret_get(_name("client_secret")))


def forget_app_credentials() -> None:
    for ref in ("client_id", "client_secret"):
        base.secret_delete(_name(ref))


def realm_secret_name(ref: str) -> str:
    return _name(ref + ".realm")


def check_realm(realm_id: str) -> str:
    """The QuickBooks company id, or SyncError. It goes into request paths, so it must be digits only."""
    if not isinstance(realm_id, str) or not _REALM.fullmatch(realm_id):
        raise SyncError("QuickBooks' company id was invalid")
    return realm_id


# --- session and client ---------------------------------------------------------------------------


class Session:
    """Holds one connection's access token in memory and rotates its refresh token safely."""

    def __init__(self, ref: str, client_id: str, secret: str, http: Http, access: str | None = None) -> None:
        self.ref, self._client_id, self._secret, self._http = ref, client_id, secret, http
        self._access = access

    def access_token(self) -> str:
        if self._access is None:
            self._refresh()
        assert self._access is not None
        return self._access

    def _refresh(self) -> None:
        name = _name(self.ref)
        old = base.secret_get(name)
        if not old:
            raise ReauthNeeded()
        access, new = _token_call(
            self._http, self._client_id, self._secret, {"grant_type": "refresh_token", "refresh_token": old}
        )
        if new != old:
            # Intuit may rotate the refresh token on any refresh and retires the old one. Store the new one before the
            # new access token is used anywhere; if the keychain refuses, stop and keep what was stored.
            try:
                base.secret_set(name, new)
            except Exception as exc:
                raise SyncError(
                    "couldn't save QuickBooks' rotated token to the keychain; the sync was stopped and the previous "
                    "token was kept"
                ) from exc
        self._access = access

    def forget_access(self) -> None:
        self._access = None


class Client:
    """GET-only QuickBooks client for one company (realm)."""

    def __init__(self, realm_id: str, session: Session, http: Http) -> None:
        self.realm_id, self._session, self._http = check_realm(realm_id), session, http

    def get(self, resource: str, params: dict[str, str]) -> dict:
        query = urllib.parse.urlencode({**params, "minorversion": MINOR_VERSION}, quote_via=urllib.parse.quote)
        url = f"https://{SANDBOX_API_HOST}/v3/company/{self.realm_id}/{resource}?{query}"
        try:
            raw = self._http.send(
                "api",
                "GET",
                url,
                headers={"Accept": "application/json"},
                secret_headers={"Authorization": f"Bearer {self._session.access_token()}"},
            )
        except SyncError as exc:
            if exc.status == 401:  # a token we just refreshed was refused: the app was disconnected
                raise ReauthNeeded() from exc
            raise
        return _json(raw)

    def company_name(self) -> str:
        info = self.get(f"companyinfo/{self.realm_id}", {}).get("CompanyInfo")
        name = info.get("CompanyName") if isinstance(info, dict) else None
        return clean_text(name if isinstance(name, str) else "", 80) or "QuickBooks company"

    def query(self, entity: str, since: str | None) -> tuple[list[dict], bool]:
        """Every row of `entity` updated at or after `since`, newest page last. The bool says the page cap was hit."""
        rows: list[dict] = []
        for page in range(MAX_PAGES):
            data = self.get("query", {"query": build_query(entity, since, 1 + page * PAGE_SIZE)})
            response = data.get("QueryResponse")
            got = response.get(entity) if isinstance(response, dict) else None
            got = got if isinstance(got, list) else []
            rows += [r for r in got if isinstance(r, dict)]
            if len(got) < PAGE_SIZE:
                return rows, False
        return rows, True

    def profit_and_loss(self, start: date, end: date, method: str) -> dict:
        label = "Cash" if method == "cash" else "Accrual"
        return self.get(
            "reports/ProfitAndLoss",
            {"start_date": start.isoformat(), "end_date": end.isoformat(), "accounting_method": label},
        )

    def aged_receivables(self, report_date: date) -> dict:
        """QuickBooks' own A/R aging report. Allowlisted for a later cross-check; sync doesn't store it yet."""
        return self.get("reports/AgedReceivables", {"report_date": report_date.isoformat()})


# --- parsing --------------------------------------------------------------------------------------


def _minor(value, currency: str) -> int | None:
    try:
        return to_minor(Decimal(str(value).replace(",", "")), currency)
    except (AmountError, InvalidOperation, ValueError):
        return None


def _stamp(value) -> str | None:
    return value if isinstance(value, str) and _STAMP_RX.fullmatch(value) else None


def invoice_row(inv: dict, home: str) -> tuple | None:
    """(qbo_id, doc_number, customer, txn_date, due_date, total, balance, currency, updated_at) or None if unusable."""
    qid = str(inv.get("Id", ""))
    txn_date = inv.get("TxnDate")
    if not (_REALM.fullmatch(qid) and isinstance(txn_date, str) and _valid_date(txn_date)):
        return None
    ref = inv.get("CurrencyRef")
    currency = ref.get("value") if isinstance(ref, dict) else None
    currency = currency if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) else home
    total, balance = _minor(inv.get("TotalAmt"), currency), _minor(inv.get("Balance"), currency)
    if total is None or balance is None:
        return None
    due = inv.get("DueDate")
    cust = inv.get("CustomerRef")
    name = cust.get("name") if isinstance(cust, dict) else None
    meta = inv.get("MetaData")
    updated = _stamp(meta.get("LastUpdatedTime")) if isinstance(meta, dict) else None
    return (
        qid,
        clean_text(str(inv.get("DocNumber") or ""), 40) or None,
        clean_text(name if isinstance(name, str) else "", 120) or None,
        txn_date,
        due if isinstance(due, str) and _valid_date(due) else None,
        total,
        balance,
        currency,
        updated or datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def parse_profit_and_loss(report: dict, home: str) -> dict | None:
    """Total income, expenses (cost of goods sold plus expenses) and net income, in minor units, found by each
    section's `group` name rather than its position (the report service changes after 2026-06-30). None when the
    report is in another currency or none of the groups are present."""
    header = report.get("Header")
    currency = header.get("Currency") if isinstance(header, dict) else None
    if isinstance(currency, str) and currency.upper() != home:
        return None
    rows = report.get("Rows")
    queue: deque = deque([rows.get("Row", [])] if isinstance(rows, dict) else [])
    found: dict[str, int | None] = {}
    while queue:  # level by level, so the shallowest section of a name wins
        level = queue.popleft()
        children = []
        for row in level if isinstance(level, list) else []:
            if not isinstance(row, dict):
                continue
            group, summary = row.get("group"), row.get("Summary")
            cols = summary.get("ColData") if isinstance(summary, dict) else None
            if isinstance(group, str) and group not in found and isinstance(cols, list) and cols:
                last = cols[-1]
                found[group] = _minor(last.get("value"), home) if isinstance(last, dict) else None
            sub = row.get("Rows")
            if isinstance(sub, dict):
                children.append(sub.get("Row", []))
        queue.extend(children)
    income, net = found.get("Income"), found.get("NetIncome")
    parts = [found.get(g) for g in ("COGS", "Expenses") if found.get(g) is not None]
    expenses = sum(parts) if parts else None
    if income is None and expenses is None and net is None:
        return None
    return {"total_income": income, "total_expenses": expenses, "net_income": net}


def newest_stamp(stamps: list[str]) -> str | None:
    """The latest QuickBooks timestamp (they carry different UTC offsets, so compare as instants)."""
    best: tuple[datetime, str] | None = None
    for s in stamps:
        t = datetime.fromisoformat(s)
        if best is None or t > best[0]:
            best = (t, s)
    return best[1] if best else None


# --- connector ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QboCompany:
    ref: str  # item_ref_hash; also the keychain ref of the refresh token
    institution: str | None = None
    cursor: str | None = None


class QboConnector:
    provider = PROVIDER

    def __init__(
        self, cfg: Config, companies: list[QboCompany], opener=None, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.cfg = cfg
        self.companies = companies
        self.http = Http(opener, sleep)
        self.warnings: list[str] = []

    def sync_records(self, conn: sqlite3.Connection, today: date) -> dict:
        require_sandbox(self.cfg)
        if not self.companies:
            raise SyncError("QuickBooks isn't connected. Run `fiscl connect qbo`.")
        client_id, secret = load_app_credentials()
        summary = {"rows_added": 0, "invoices": 0, "reports": 0}
        failures: list[SyncError] = []
        for company in self.companies:
            try:
                self._sync_one(conn, company, client_id, secret, today, summary)
            except ReauthNeeded as exc:
                connections.set_status(conn, PROVIDER, company.ref, "needs_reauth")
                self.warnings.append(REAUTH_MESSAGE)
                failures.append(exc)
            except SyncError as exc:
                connections.set_status(conn, PROVIDER, company.ref, "error")
                self.warnings.append("QuickBooks couldn't be read for one connection; try again later")
                failures.append(exc)
        conn.commit()  # keep status updates even when the call is about to fail
        if len(failures) == len(self.companies):
            raise failures[0]
        return {**summary, "warnings": list(self.warnings)}

    def _sync_one(
        self, conn: sqlite3.Connection, company: QboCompany, client_id: str, secret: str, today: date, summary: dict
    ) -> None:
        realm = base.secret_get(realm_secret_name(company.ref))
        if not realm:
            raise ReauthNeeded()
        session = Session(company.ref, client_id, secret, self.http)
        client = Client(realm, session, self.http)
        since = company.cursor if company.cursor and _STAMP_RX.fullmatch(company.cursor) else None
        invoices, capped = client.query("Invoice", since)
        if capped:
            self.warnings.append(
                f"QuickBooks has more invoices than one run reads ({MAX_PAGES * PAGE_SIZE}); sync again"
            )
        reports = self._reports(client, today)
        row = conn.execute(
            "SELECT id FROM connections WHERE provider = ? AND item_ref_hash = ?", (PROVIDER, company.ref)
        ).fetchone()
        cid = int(row["id"])
        mapped = [r for r in (invoice_row(i, self.cfg.currency) for i in invoices) if r is not None]
        if len(mapped) < len(invoices):
            self.warnings.append("Some QuickBooks invoices couldn't be read and were skipped")
        conn.execute("SAVEPOINT fiscl_qbo")
        try:
            if since is None:  # a full read replaces the mirror, so invoices deleted in QuickBooks disappear
                conn.execute("DELETE FROM qbo_invoices WHERE connection_id = ?", (cid,))
            conn.executemany(
                """INSERT OR REPLACE INTO qbo_invoices
                   (connection_id, qbo_id, doc_number, customer, txn_date, due_date, total, balance, currency,
                    updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(cid, *r) for r in mapped],
            )
            fetched = datetime.now().isoformat(timespec="seconds")
            conn.executemany(
                """INSERT OR REPLACE INTO qbo_reports
                   (connection_id, report, period_start, period_end, basis, total_income, total_expenses, net_income,
                    fetched_at) VALUES (?, 'profit_and_loss', ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        cid,
                        start,
                        end,
                        self.cfg.qbo_accounting_method,
                        v["total_income"],
                        v["total_expenses"],
                        v["net_income"],
                        fetched,
                    )
                    for start, end, v in reports
                ],
            )
            stamps = [r[8] for r in mapped if _STAMP_RX.fullmatch(r[8])]
            newest = newest_stamp(([since] if since else []) + stamps)
            if newest:
                conn.execute("UPDATE connections SET cursor = ? WHERE id = ?", (newest, cid))
            conn.execute("RELEASE fiscl_qbo")
        except Exception:
            conn.execute("ROLLBACK TO fiscl_qbo")
            conn.execute("RELEASE fiscl_qbo")
            raise
        connections.mark_synced(conn, PROVIDER, company.ref)
        summary["invoices"] += len(mapped)
        summary["reports"] += len(reports)

    def _reports(self, client: Client, today: date) -> list[tuple[str, str, dict]]:
        out = []
        for year in (today.year - 1, today.year):
            start = date(year, 1, 1)
            end = date(year, 12, 31) if year < today.year else today
            parsed = parse_profit_and_loss(
                client.profit_and_loss(start, end, self.cfg.qbo_accounting_method), self.cfg.currency
            )
            if parsed is None:
                self.warnings.append("A QuickBooks profit-and-loss report wasn't in a format fiscl recognises; skipped")
                continue
            out.append((start.isoformat(), end.isoformat(), parsed))
        return out

    def forget(self) -> None:
        """Revoke each stored grant at Intuit, then delete the keychain entries. If Intuit can't be told (network,
        server error) nothing is deleted. A grant Intuit already considers dead (400/401) counts as revoked."""
        client_id, secret = load_app_credentials()
        for company in self.companies:
            token = base.secret_get(_name(company.ref))
            if token:
                try:
                    revoke_token(self.http, client_id, secret, token)
                except SyncError as exc:
                    if exc.status not in (400, 401):
                        raise
            for name in (_name(company.ref), realm_secret_name(company.ref)):
                base.secret_delete(name)


def is_connected(conn: sqlite3.Connection) -> bool:
    return bool(connections.list_rows(conn, PROVIDER))
