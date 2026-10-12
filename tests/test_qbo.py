"""QuickBooks Online connector (sandbox only): allowlists, query-text pinning, token rotation, the OAuth callback
server, sync into qbo_* tables, receivables / books_vs_bank, forget, and the "no secret anywhere" sweep. Everything
runs against a fake opener; no socket is ever opened to Intuit. Fixtures under tests/fixtures/qbo are synthetic."""

from __future__ import annotations

import base64
import errno
import http.client
import io
import json
import re
import socket
import sqlite3
import threading
import urllib.error
import urllib.parse
from datetime import date
from pathlib import Path

import pytest
from mcp import Client

from fiscl import analysis, cli
from fiscl.config import ConfigError, load_config
from fiscl.guard import GuardError
from fiscl.server import build_server
from fiscl.sync import base, connect, connections, qbo, registry
from fiscl.sync.base import SyncError

from .conftest import TODAY, FakeKeyring, add_txn, write
from .test_teller import FakeResp

FIXTURES = Path(__file__).parent / "fixtures" / "qbo"
SRC = Path(__file__).resolve().parent.parent / "src"
INVOICES = json.loads((FIXTURES / "invoices.json").read_text())
PNL = {y: json.loads((FIXTURES / f"profit_and_loss_{y}.json").read_text()) for y in (2025, 2026)}

REALM = "9341450000000001"
CLIENT_ID = "ABQBOclientIDsecret0123456789"
CLIENT_SECRET = "qboClientSecretVALUE0123456789"
CODE = "AB11code-0123456789xyz"
REFRESH_1 = "RT1-0001-refresh-secret-value"
ACCESS_1 = "eyJ.access.0000.secret.value"
COMPANY = "Sandbox Company_US_1"
API = "sandbox-quickbooks.api.intuit.com"
SECRETS = [CLIENT_ID, CLIENT_SECRET, CODE, REFRESH_1, ACCESS_1, REALM]


def herr(status, body=b"", headers=None):
    return urllib.error.HTTPError("https://x", status, "err", headers or {}, io.BytesIO(body))


class FakeQbo:
    """Answers like the sandbox: token endpoint (with refresh-token rotation), revoke, companyinfo, query, reports."""

    def __init__(self, invoices=None, rotate=True, fail=None, events=None, company=COMPANY):
        self.invoices = [dict(i) for i in (INVOICES if invoices is None else invoices)]
        self.rotate, self.company = rotate, company
        self.fail = fail or {}  # (method, path) -> exception, or a list of exceptions consumed in order
        self.events = events if events is not None else []
        self.refresh, self.access, self.n = REFRESH_1, ACCESS_1, 0
        self.calls: list[tuple] = []
        self.revoked: list[str] = []

    def __call__(self, req, timeout):
        parts = urllib.parse.urlsplit(req.full_url)
        method, host, path = req.get_method(), parts.netloc, parts.path
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append((method, host, path, query, req.get_header("Authorization"), req.data))
        self.events.append((method, host, path))
        failing = self.fail.get((method, path))
        if isinstance(failing, list) and failing:
            raise failing.pop(0)
        if isinstance(failing, Exception):
            raise failing
        if host == qbo.TOKEN_HOST:
            return self._token(req)
        if host == qbo.REVOKE_HOST:
            self.revoked.append(json.loads(req.data)["token"])
            return FakeResp(b"{}")
        assert host == API, host
        if req.get_header("Authorization") != f"Bearer {self.access}":
            raise herr(401)
        assert query.pop("minorversion") == "75"
        if path.endswith(f"/companyinfo/{REALM}"):
            return FakeResp(json.dumps({"CompanyInfo": {"CompanyName": self.company, "Id": REALM}}).encode())
        if path.endswith("/query"):
            return FakeResp(json.dumps(self._query(query["query"])).encode())
        if path.endswith("/reports/ProfitAndLoss"):
            return FakeResp(json.dumps(PNL[int(query["start_date"][:4])]).encode())
        raise AssertionError(f"unexpected request {method} {path}")

    def _token(self, req):
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        assert req.get_header("Authorization") == expected
        fields = dict(urllib.parse.parse_qsl(req.data.decode()))
        if fields["grant_type"] == "authorization_code":
            if fields["code"] != CODE:
                raise herr(400, b'{"error":"invalid_grant"}')
        else:
            if fields["refresh_token"] != self.refresh:
                raise herr(400, b'{"error":"invalid_grant"}')
            if self.rotate:
                self.n += 1
                self.refresh = f"RT{self.n + 1}-{self.n + 1:04d}-refresh-secret-value"
            self.access = f"eyJ.access.{self.n + 1:04d}.secret.value"
        body = {"access_token": self.access, "refresh_token": self.refresh, "token_type": "bearer", "expires_in": 3600}
        return FakeResp(json.dumps(body).encode())

    def _query(self, text):
        m = re.fullmatch(
            r"SELECT \* FROM (\w+)(?: WHERE MetaData\.LastUpdatedTime >= '([^']+)')? ORDERBY Id "
            r"STARTPOSITION (\d+) MAXRESULTS (\d+)",
            text,
        )
        assert m, text
        entity, since, start, size = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        if entity != "Invoice":
            return {"QueryResponse": {}}
        rows = [
            i
            for i in self.invoices
            if since is None
            or date.fromisoformat(i["MetaData"]["LastUpdatedTime"][:10]) >= date.fromisoformat(since[:10])
        ]
        return {"QueryResponse": {"Invoice": rows[start - 1 : start - 1 + size], "startPosition": start}}

    def queries(self):
        return [q["query"] for _, host, path, q, *_ in self.calls if host == API and path.endswith("/query")]


@pytest.fixture
def no_sockets(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def connected(cfg, conn, keychain):
    """A QuickBooks connection (sandbox): app keys, refresh token and company id in the fake keychain."""
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    ref = connections.item_ref_hash("qbo", REALM)
    keychain.set_password("fiscl", base.secret_name("qbo", ref), REFRESH_1)
    keychain.set_password("fiscl", qbo.realm_secret_name(ref), REALM)
    connections.add_connection(conn, "qbo", COMPANY, ref)
    conn.commit()
    return ref


def sync(conn, cfg, api, **kw):
    return registry.sync_accounts(conn, cfg, provider="qbo", today=TODAY, opener=api, **kw)


@pytest.fixture
def synced(cfg, conn, connected):
    api = FakeQbo()
    out = sync(conn, cfg, api)
    conn.commit()
    return api, out


def connector(cfg, conn, api, sleep=None):
    rows = connections.list_rows(conn, "qbo")
    companies = [qbo.QboCompany(r["item_ref_hash"], r["institution"], r["cursor"]) for r in rows]
    return qbo.QboConnector(cfg, companies, opener=api, sleep=sleep or (lambda s: None))


def usd(minor):
    return {"minor": minor, "display": f"${minor // 100:,}.{minor % 100:02d}"}


# --- allowlist: refused before any socket opens ------------------------------------------------------------------

Q = urllib.parse.quote
GOOD_Q = "SELECT * FROM Invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 1000"
BASE = f"https://{API}/v3/company/1"


def query_url(text, extra="minorversion=75"):
    return f"{BASE}/query?query={Q(text)}&{extra}"


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", f"{BASE}/invoice"),
        ("POST", f"{BASE}/invoice?operation=delete"),
        ("POST", f"{BASE}/invoice?operation=void&minorversion=75"),
        ("GET", f"{BASE}/invoice/5?minorversion=75"),
        ("GET", f"{BASE}/customer/5?minorversion=75"),
        ("POST", f"{BASE}/query?minorversion=75"),
        ("PUT", f"{BASE}/query?minorversion=75"),
        ("DELETE", f"{BASE}/invoice/5"),
        ("POST", f"{BASE}/batch"),
        ("GET", f"{BASE}/batch?minorversion=75"),
        ("POST", f"{BASE}/invoice/5/send"),
        ("POST", f"{BASE}/upload"),
        ("GET", f"{BASE}/reports/BalanceSheet?minorversion=75"),
        ("GET", f"{BASE}/companyinfo/2?minorversion=75"),  # a different company than the path's
        ("GET", f"{BASE}/query/../invoice?minorversion=75"),
        ("GET", f"https://{API}/v3/company/abc/query?minorversion=75"),
        ("GET", f"https://{API}/v3/company/{'1' * 21}/query?minorversion=75"),
        ("GET", query_url(GOOD_Q).replace("https://", "http://")),
        ("GET", query_url(GOOD_Q).replace(API, "quickbooks.api.intuit.com")),  # the production host
        ("GET", query_url(GOOD_Q).replace(API, f"{API}.evil.example")),
        ("GET", query_url(GOOD_Q).replace(API, f"{API}:8443")),
        ("GET", query_url(GOOD_Q).replace("https://", "https://user:pw@")),
        ("GET", query_url(GOOD_Q) + "#frag"),
    ],
)
def test_api_requests_outside_the_allowlist_raise_before_any_socket(no_sockets, method, url):
    with pytest.raises(SyncError):
        base.request(qbo.policy_for("api"), method, url)  # default opener: a socket would trip no_sockets


@pytest.mark.parametrize(
    "text",
    [
        "SELECT * FROM Invoice; DELETE FROM Invoice",
        "SELECT * FROM Invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 1000; DELETE",
        "SELECT * FROM Invoice WHERE Id = '1' ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
        "SELECT Id FROM Invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
        "SELECT count(*) FROM Invoice",
        "SELECT * FROM Account ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
        "SELECT * FROM Invoice, Customer ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
        "select * from invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
        "SELECT * FROM Invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 1001",
        "SELECT * FROM Invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 10 ",
        "SELECT * FROM Invoice ORDERBY Id STARTPOSITION 1 MAXRESULTS 10\n",
        "SELECT * FROM Invoice ORDERBY Id STARTPOSITION ١ MAXRESULTS 10",
        "SELECT * FROM Invoice ORDERBY Id STARTPOSITION 12345678 MAXRESULTS 10",
        "SELECT * FROM Invoice WHERE MetaData.LastUpdatedTime >= '2026-01-01T00:00:00-05:00' OR Id > '0' "
        "ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
        "SELECT * FROM Invoice WHERE MetaData.LastUpdatedTime >= '2026-01-01' ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
        "SELECT * FROM Invoice WHERE MetaData.LastUpdatedTime >= '2026-01-01T00:00:00-05:00' --' "
        "ORDERBY Id STARTPOSITION 1 MAXRESULTS 10",
        "",
    ],
)
def test_query_text_that_is_not_the_pinned_select_shape_is_refused(no_sockets, text):
    with pytest.raises(SyncError, match="isn't allowed"):
        base.request(qbo.policy_for("api"), "GET", query_url(text))


@pytest.mark.parametrize(
    "extra",
    [
        "",  # minorversion missing
        "minorversion=74",
        "minorversion=76",
        "minorversion=75&minorversion=75",
        "minorversion=75&operation=update",
        "minorversion=75&sparse=true",
        "minorversion=75&requestid=abc",
        "minorversion=75&query=SELECT",
    ],
)
def test_the_pinned_minorversion_is_required_and_no_other_parameter_is_allowed(no_sockets, extra):
    with pytest.raises(SyncError):
        base.request(qbo.policy_for("api"), "GET", f"{BASE}/query?query={Q(GOOD_Q)}&{extra}")


@pytest.mark.parametrize(
    "tail",
    [
        "reports/ProfitAndLoss?minorversion=75",
        "reports/ProfitAndLoss?start_date=2026-01-01&end_date=2026-12-31&minorversion=75",
        "reports/ProfitAndLoss?start_date=2026-01-01&end_date=2026-12-31&accounting_method=Both&minorversion=75",
        "reports/ProfitAndLoss?start_date=2026-1-1&end_date=2026-12-31&accounting_method=Cash&minorversion=75",
        "reports/ProfitAndLoss?start_date=2026-01-01&end_date=2026-12-31&accounting_method=Cash&minorversion=75&x=1",
        "reports/AgedReceivables?report_date=tomorrow&minorversion=75",
        "reports/AgedReceivables?customer=1&minorversion=75",
        f"companyinfo/{1}?x=1&minorversion=75",
    ],
)
def test_report_and_companyinfo_parameters_are_pinned(no_sockets, tail):
    with pytest.raises(SyncError):
        base.request(qbo.policy_for("api"), "GET", f"{BASE}/{tail}")


def test_the_requests_fiscl_really_builds_pass_the_policy():
    policy = qbo.policy_for("api")
    for entity in qbo.ENTITIES:
        for since in (None, "2026-10-06T10:00:00-05:00", "2026-10-06T10:00:00+05:30"):
            for start in (1, 1001, 49_001):
                policy.check("GET", query_url(qbo.build_query(entity, since, start)))
    policy.check("GET", f"{BASE}/companyinfo/1?minorversion=75")
    policy.check(
        "GET",
        f"{BASE}/reports/ProfitAndLoss?start_date=2026-01-01&end_date=2026-10-12&accounting_method=Cash&minorversion=75",
    )
    policy.check("GET", f"{BASE}/reports/AgedReceivables?report_date=2026-10-12&minorversion=75")
    policy.check("GET", f"{BASE}/reports/AgedReceivables?minorversion=75")


@pytest.mark.parametrize(
    ("entity", "since", "start", "size"),
    [
        ("Account", None, 1, 10),
        ("Invoice; DROP", None, 1, 10),
        ("Invoice", "2026-01-01", 1, 10),
        ("Invoice", "2026-01-01T00:00:00-05:00' OR Id > '0", 1, 10),
        ("Invoice", None, 0, 10),
        ("Invoice", None, 1, 1001),
        ("Invoice", None, 10**8, 10),
    ],
)
def test_build_query_refuses_anything_off_shape(entity, since, start, size):
    with pytest.raises(SyncError):
        qbo.build_query(entity, since, start, size)


def test_signin_hosts_accept_one_post_route_each_and_no_query_string(no_sockets):
    token, revoke = qbo.policy_for("token"), qbo.policy_for("revoke")
    token.check("POST", "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer")
    revoke.check("POST", "https://developer.api.intuit.com/v2/oauth2/tokens/revoke")
    for policy, method, url in [
        (token, "GET", "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"),
        (token, "POST", "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer?x=1"),
        (token, "POST", "https://oauth.platform.intuit.com/oauth2/v1/tokens/revoke"),
        (token, "POST", "https://developer.api.intuit.com/oauth2/v1/tokens/bearer"),
        (revoke, "GET", "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"),
        (revoke, "POST", "https://oauth.platform.intuit.com/v2/oauth2/tokens/revoke"),
        (revoke, "POST", "https://developer.api.intuit.com/v2/oauth2/tokens/bearer"),
        (token, "POST", "http://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"),
    ]:
        with pytest.raises(SyncError):
            base.request(policy, method, url)
    with pytest.raises(SyncError):
        qbo.policy_for("anything-else")


def test_the_browser_only_authorize_host_is_in_no_policy(no_sockets):
    assert qbo.AUTHORIZE_HOST == "appcenter.intuit.com"
    for kind in ("api", "token", "revoke"):
        policy = qbo.policy_for(kind)
        assert qbo.AUTHORIZE_HOST not in policy.hosts
        with pytest.raises(SyncError):
            base.request(policy, "GET", f"https://{qbo.AUTHORIZE_HOST}/connect/oauth2")


def test_routes_are_pinned_get_only_for_the_api_and_one_post_each_for_signin():
    assert qbo.API_ROUTES == (
        ("GET", r"/v3/company/([0-9]{1,20})/query"),
        ("GET", r"/v3/company/([0-9]{1,20})/companyinfo/\1"),
        ("GET", r"/v3/company/([0-9]{1,20})/reports/ProfitAndLoss"),
        ("GET", r"/v3/company/([0-9]{1,20})/reports/AgedReceivables"),
    )
    assert qbo.TOKEN_ROUTES == (("POST", "/oauth2/v1/tokens/bearer"),)
    assert qbo.REVOKE_ROUTES == (("POST", "/v2/oauth2/tokens/revoke"),)
    assert qbo.policy_for("api").hosts == ("sandbox-quickbooks.api.intuit.com",)
    assert qbo.policy_for("token").hosts == ("oauth.platform.intuit.com",)
    assert qbo.policy_for("revoke").hosts == ("developer.api.intuit.com",)


def test_source_has_no_quickbooks_write_markers_and_no_production_host():
    write_markers = ("operation=delete", "operation=void", "/batch", "/send", '"sparse"', "/upload")
    text = {p.relative_to(SRC).as_posix(): p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")}
    # Connector code only: tax/states.py legitimately links to a state's upload page for the user to read.
    hits = [(rel, m) for rel, body in text.items() if "/sync/" in rel for m in write_markers if m in body]
    assert hits == []
    prod = re.compile(r"(?<![\w-])quickbooks\.api\.intuit\.com")  # "sandbox-quickbooks.api..." is the allowed host
    assert [rel for rel, body in text.items() if prod.search(body)] == []
    assert "POST" not in "".join(m for m, _ in qbo.API_ROUTES)


# --- transport: redirects, unredirected credentials, rate limits ----------------------------------------------------


def test_a_redirect_from_quickbooks_raises_and_resends_nothing():
    seen = []

    def redirecting(req, timeout):
        seen.append(req)
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://evil.example/"}, None)

    http_ = qbo.Http(redirecting)
    with pytest.raises(SyncError, match="redirect"):
        http_.send("api", "GET", query_url(GOOD_Q), secret_headers={"Authorization": "Bearer abc"})
    assert len(seen) == 1
    req = seen[0]
    assert "Authorization" in req.unredirected_hdrs and "Authorization" not in req.headers
    assert "abc" not in req.full_url


def test_the_default_opener_is_the_no_redirect_one():
    assert any(isinstance(h, base._NoRedirect) for h in base._OPENER.handlers)


def test_token_requests_carry_basic_auth_only_in_an_unredirected_header(connected, cfg, conn):
    api = FakeQbo()
    qbo.Session(connected, CLIENT_ID, CLIENT_SECRET, qbo.Http(api)).access_token()
    method, host, path, query, auth, data = api.calls[0]
    assert (method, host, path, query) == ("POST", qbo.TOKEN_HOST, "/oauth2/v1/tokens/bearer", {})
    assert auth.startswith("Basic ") and CLIENT_SECRET not in auth  # base64, not the raw secret
    assert CLIENT_ID.encode() not in data and CLIENT_SECRET.encode() not in data and REFRESH_1.encode() in data


def test_429_waits_retry_after_then_succeeds_and_defaults_to_sixty_seconds(cfg, conn, connected):
    sleeps: list[float] = []
    api = FakeQbo(fail={("GET", f"/v3/company/{REALM}/query"): [herr(429, headers={"Retry-After": "3"}), herr(429)]})
    out = connector(cfg, conn, api, sleeps.append).sync_records(conn, TODAY)
    assert sleeps == [3.0, 60.0] and out["invoices"] == 7


def test_429_gives_up_after_three_waits_with_a_fixed_message(cfg, conn, connected):
    sleeps: list[float] = []
    api = FakeQbo(fail={("GET", f"/v3/company/{REALM}/query"): [herr(429)] * 10})
    with pytest.raises(SyncError, match="rate limiting") as exc:
        connector(cfg, conn, api, sleeps.append).sync_records(conn, TODAY)
    assert sleeps == [60.0, 60.0, 60.0] and exc.value.status == 429
    assert conn.execute("SELECT status FROM connections").fetchone()[0] == "error"


def test_an_absurd_retry_after_is_capped(cfg, conn, connected):
    sleeps: list[float] = []
    api = FakeQbo(fail={("GET", f"/v3/company/{REALM}/query"): [herr(429, headers={"Retry-After": "3000"})]})
    connector(cfg, conn, api, sleeps.append).sync_records(conn, TODAY)
    assert sleeps == [qbo.MAX_WAIT]


# --- token rotation -------------------------------------------------------------------------------------------------


class RecordingKeyring(FakeKeyring):
    def __init__(self, events, fail_on=()):
        super().__init__()
        self.events, self.fail_on = events, set(fail_on)

    def set_password(self, service, user, value):
        if user in self.fail_on:
            raise RuntimeError("keychain is locked")
        self.events.append(("keychain-set", user))
        super().set_password(service, user, value)


def test_the_rotated_refresh_token_is_stored_before_the_new_access_token_is_used(cfg, conn, monkeypatch):
    events: list = []
    kr = RecordingKeyring(events)
    monkeypatch.setattr(base, "keyring_backend", lambda: kr)
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    ref = connections.item_ref_hash("qbo", REALM)
    kr.items[("fiscl", base.secret_name("qbo", ref))] = REFRESH_1
    kr.items[("fiscl", qbo.realm_secret_name(ref))] = REALM
    connections.add_connection(conn, "qbo", COMPANY, ref)
    events.clear()
    api = FakeQbo(events=events)
    connector(cfg, conn, api).sync_records(conn, TODAY)
    kinds = [e[0] if e[0] == "keychain-set" else e[2] for e in events]
    store = kinds.index("keychain-set")
    assert kinds[:store] == ["/oauth2/v1/tokens/bearer"]
    assert all(k != "keychain-set" for k in kinds[store + 1 :])
    first_api_call = next(i for i, k in enumerate(kinds) if k.startswith("/v3/"))
    assert store < first_api_call
    assert kr.get_password("fiscl", base.secret_name("qbo", ref)) == api.refresh != REFRESH_1


def test_a_keychain_write_failure_aborts_the_sync_and_keeps_the_old_token(cfg, conn, monkeypatch):
    events: list = []
    ref = connections.item_ref_hash("qbo", REALM)
    name = base.secret_name("qbo", ref)
    kr = RecordingKeyring(events, fail_on=[name])
    monkeypatch.setattr(base, "keyring_backend", lambda: kr)
    for k, v in (("client_id", CLIENT_ID), ("client_secret", CLIENT_SECRET)):
        kr.items[("fiscl", base.secret_name("qbo", k))] = v
    kr.items[("fiscl", name)] = REFRESH_1
    kr.items[("fiscl", qbo.realm_secret_name(ref))] = REALM
    connections.add_connection(conn, "qbo", COMPANY, ref)
    api = FakeQbo(events=events)
    with pytest.raises(SyncError, match="previous token was kept") as exc:
        connector(cfg, conn, api).sync_records(conn, TODAY)
    assert kr.get_password("fiscl", name) == REFRESH_1
    assert not [c for c in api.calls if c[1] == API]  # the new access token was never used
    for secret in SECRETS:
        assert secret not in str(exc.value)


def test_an_unchanged_refresh_token_is_not_rewritten(cfg, conn, connected, keychain):
    writes: list = []
    original = keychain.set_password
    keychain.set_password = lambda s, u, v: (writes.append(u), original(s, u, v))[1]
    connector(cfg, conn, FakeQbo(rotate=False)).sync_records(conn, TODAY)
    assert writes == []


def test_the_access_token_is_never_persisted(cfg, conn, connected, keychain):
    api = FakeQbo()
    connector(cfg, conn, api).sync_records(conn, TODAY)
    assert api.access not in "".join(keychain.items.values())
    assert sorted(u for (_, u) in keychain.items) == sorted(
        f"fiscl:qbo:{x}" for x in ("client_id", "client_secret", connected, connected + ".realm")
    )


def test_invalid_grant_marks_the_connection_needs_reauth_with_a_message_that_leaks_nothing(cfg, conn, connected):
    api = FakeQbo()
    api.refresh = "someone-elses-token-value"  # the stored token is no longer the live one
    with pytest.raises(GuardError, match="fiscl connect qbo --reconnect") as exc:
        sync(conn, cfg, api)
    assert exc.value.code == "sync_failed"
    for leak in (*SECRETS, COMPANY, "invalid_grant"):
        assert leak not in str(exc.value)
    row = conn.execute("SELECT status FROM connections").fetchone()
    assert row["status"] == "needs_reauth"
    listed = registry.list_connections(conn, cfg)
    assert listed["connections"][0]["status"] == "needs_reauth"


def test_a_missing_keychain_entry_is_a_reauth_not_a_crash(cfg, conn, connected, keychain):
    del keychain.items[("fiscl", base.secret_name("qbo", connected))]
    with pytest.raises(GuardError, match="reconnected"):
        sync(conn, cfg, FakeQbo())
    assert conn.execute("SELECT status FROM connections").fetchone()[0] == "needs_reauth"


def test_a_401_on_a_fresh_token_is_a_reauth(cfg, conn, connected):
    api = FakeQbo(fail={("GET", f"/v3/company/{REALM}/query"): [herr(401)]})
    with pytest.raises(GuardError, match="reconnected"):
        sync(conn, cfg, api)


# --- sandbox only --------------------------------------------------------------------------------------------------


def test_environment_config(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    c = load_config(home)
    assert (c.qbo_environment, c.qbo_redirect_port, c.qbo_accounting_method) == ("sandbox", 8765, "cash")
    (home / "config.toml").write_text(
        '[connectors.qbo]\nenvironment = "production"\nredirect_port = 9001\naccounting_method = "accrual"\n'
    )
    c = load_config(home)
    assert (c.qbo_environment, c.qbo_redirect_port, c.qbo_accounting_method) == ("production", 9001, "accrual")
    for bad in ('environment = "staging"', "redirect_port = 80", 'redirect_port = "x"', 'accounting_method = "both"'):
        (home / "config.toml").write_text(f"[connectors.qbo]\n{bad}\n")
        with pytest.raises(ConfigError):
            load_config(home)


def test_production_is_refused_by_connect_sync_and_the_cli_before_any_network(
    cfg, conn, connected, keychain, no_sockets
):
    cfg.qbo_environment = "production"
    with pytest.raises(SyncError, match="sandbox only") as exc:
        connect.connect_qbo(cfg, conn, open_browser=lambda url: pytest.fail("opened a browser"), timeout=1)
    assert 'environment = "sandbox"' in str(exc.value)
    with pytest.raises(GuardError, match="sandbox only"):
        registry.sync_accounts(conn, cfg, provider="qbo", today=TODAY)
    write(cfg.config_file, '[connectors.qbo]\nenvironment = "production"\n')
    assert cli.main(["--home", str(cfg.home), "connect", "qbo"]) == 1


# --- sync and the tools ----------------------------------------------------------------------------------------------


def test_fixture_sync_lands_invoices_reports_and_a_cursor(cfg, conn, synced):
    api, out = synced
    result = out["results"]["qbo"]
    assert (result["invoices"], result["reports"], result["rows_added"]) == (7, 2, 0)
    assert any("couldn't be read" in w for w in result["warnings"])  # invoice 199 has no TxnDate
    rows = {r["qbo_id"]: r for r in conn.execute("SELECT * FROM qbo_invoices")}
    assert sorted(rows) == ["101", "102", "103", "104", "105", "106", "107"]
    assert (rows["102"]["total"], rows["102"]["balance"], rows["102"]["customer"]) == (80050, 30025, "Beta LLC")
    assert rows["107"]["currency"] == "EUR" and rows["101"]["due_date"] == "2026-10-01"
    reports = {
        r["period_start"]: r
        for r in conn.execute("SELECT * FROM qbo_reports WHERE report = 'profit_and_loss' AND basis = 'cash'")
    }
    assert reports["2026-01-01"]["period_end"] == "2026-10-12"
    assert (reports["2026-01-01"]["total_income"], reports["2026-01-01"]["total_expenses"]) == (6_000_000, 1_734_567)
    assert reports["2026-01-01"]["net_income"] == 4_265_433
    assert (reports["2025-01-01"]["total_income"], reports["2025-01-01"]["total_expenses"]) == (5_000_000, 2_000_000)
    assert reports["2025-01-01"]["period_end"] == "2025-12-31"
    row = conn.execute("SELECT status, last_sync_at, cursor FROM connections").fetchone()
    assert row["status"] == "ok" and row["last_sync_at"] and row["cursor"] == "2026-10-06T10:00:00-05:00"
    assert api.queries() == [qbo.build_query("Invoice", None, 1)]
    basis = {c[3].get("accounting_method") for c in api.calls if c[2].endswith("ProfitAndLoss")}
    assert basis == {"Cash"}


def test_a_second_sync_asks_only_for_rows_updated_since_the_cursor(cfg, conn, synced):
    api, _ = synced
    api.invoices = [i for i in api.invoices if i["Id"] in ("101", "103", "199")]
    api.invoices[0] = {**api.invoices[0], "Balance": 0, "MetaData": {"LastUpdatedTime": "2026-10-10T08:00:00-05:00"}}
    api.invoices.append(
        {**INVOICES[2], "Id": "108", "DocNumber": "1008", "MetaData": {"LastUpdatedTime": "2026-10-11T08:00:00-05:00"}}
    )
    api.calls.clear()
    out = sync(conn, cfg, api)
    assert api.queries() == [qbo.build_query("Invoice", "2026-10-06T10:00:00-05:00", 1)]
    assert out["results"]["qbo"]["invoices"] == 3  # 101 (now paid), 103 (boundary, re-read), 108 (new)
    assert conn.execute("SELECT COUNT(*) FROM qbo_invoices").fetchone()[0] == 8
    assert conn.execute("SELECT balance FROM qbo_invoices WHERE qbo_id = '101'").fetchone()[0] == 0
    assert conn.execute("SELECT cursor FROM connections").fetchone()[0] == "2026-10-11T08:00:00-05:00"
    assert analysis.receivables(conn, cfg, today=TODAY)["totals"]["open_invoices"] == 5  # 101 paid, 108 open


def test_a_full_read_replaces_the_mirror_so_deleted_invoices_disappear(cfg, conn, synced):
    api, _ = synced
    conn.execute("UPDATE connections SET cursor = NULL")
    api.invoices = [i for i in api.invoices if i["Id"] != "105"]
    sync(conn, cfg, api)
    assert conn.execute("SELECT COUNT(*) FROM qbo_invoices WHERE qbo_id = '105'").fetchone()[0] == 0


def _many_invoices(n: int, stamp: str = "2026-10-10T08:00:00-05:00") -> list[dict]:
    return [
        {**INVOICES[0], "Id": str(10_000 + k), "DocNumber": str(k), "MetaData": {"LastUpdatedTime": stamp}}
        for k in range(n)
    ]


def test_a_capped_incremental_read_does_not_advance_the_cursor(cfg, conn, synced):
    api, _ = synced
    before = conn.execute("SELECT cursor FROM connections").fetchone()[0]
    api.invoices = _many_invoices(qbo.MAX_PAGES * qbo.PAGE_SIZE)
    api.calls.clear()
    out = sync(conn, cfg, api)
    assert len(api.queries()) == qbo.MAX_PAGES  # the cap stopped the read at 50 full pages
    assert conn.execute("SELECT cursor FROM connections").fetchone()[0] == before
    assert any("cursor was not advanced" in w for w in out["results"]["qbo"]["warnings"]), out


def test_a_capped_first_read_keeps_the_existing_mirror_and_sets_no_cursor(cfg, conn, synced):
    api, _ = synced
    conn.execute("UPDATE connections SET cursor = NULL")
    api.invoices = _many_invoices(qbo.MAX_PAGES * qbo.PAGE_SIZE)
    sync(conn, cfg, api)
    assert conn.execute("SELECT cursor FROM connections").fetchone()[0] is None
    # The fixture invoices (not in this partial read) were not wiped by the replace-the-mirror step.
    assert conn.execute("SELECT COUNT(*) FROM qbo_invoices WHERE qbo_id = '105'").fetchone()[0] == 1


def test_receivables_and_books_notes_say_deleted_invoices_linger(cfg, conn, synced):
    r = analysis.receivables(conn, cfg, today=TODAY)
    assert any("deleted in QuickBooks" in n for n in r["notes"])


def test_receivables_aging_buckets_match_the_hand_computed_fixture(cfg, conn, synced):
    r = analysis.receivables(conn, cfg, today=TODAY)
    assert [(i["doc_number"], i["days_overdue"], i["aging_bucket"]) for i in r["invoices"]] == [
        ("1005", 134, "90+"),
        ("1004", 89, "61-90"),
        ("1002", 42, "31-60"),
        ("1001", 11, "1-30"),
        ("1003", 0, "current"),
    ]
    assert {b["bucket"]: (b["count"], b["balance"]["minor"]) for b in r["aging"]} == {
        "current": (1, 25000),
        "1-30": (1, 150000),
        "31-60": (1, 30025),
        "61-90": (1, 100000),
        "90+": (1, 40000),
    }
    assert r["totals"] == {"open_invoices": 5, "balance": usd(345025), "overdue_balance": usd(320025)}
    assert r["invoices"][2]["total"] == usd(80050) and r["invoices"][2]["balance"] == usd(30025)
    assert r["connected"] is True and r["last_sync"] and r["as_of"] == "2026-10-12"
    assert any("other currencies" in n for n in r["notes"])  # the EUR invoice is left out, and said so
    assert r["meta"]["row_count"] == 5


def test_receivables_filters_by_customer_and_date_and_caps_the_list(cfg, conn, synced):
    acme = analysis.receivables(conn, cfg, customer="ACME", today=TODAY)
    assert [i["doc_number"] for i in acme["invoices"]] == ["1001", "1003"]
    assert acme["totals"]["balance"] == usd(175000)
    early = analysis.receivables(conn, cfg, as_of="2026-09-02", today=TODAY)  # 1003 doesn't exist yet
    assert [i["doc_number"] for i in early["invoices"]] == ["1005", "1004", "1002", "1001"]
    assert early["invoices"][-1]["days_overdue"] == 0 and early["invoices"][0]["days_overdue"] == 94
    capped = analysis.receivables(conn, cfg, limit=2, today=TODAY)
    assert len(capped["invoices"]) == 2 and capped["totals"]["open_invoices"] == 5
    assert capped["meta"]["truncated"] is True and capped["meta"]["total_count"] == 5
    with pytest.raises(GuardError):
        analysis.receivables(conn, cfg, as_of="soon")


def test_redact_descriptions_withholds_customers_and_document_numbers_but_keeps_amounts(cfg, conn, synced):
    plain = analysis.receivables(conn, cfg, today=TODAY)
    cfg.guard.redact_descriptions = True
    r = analysis.receivables(conn, cfg, today=TODAY)
    assert r["invoices"] and all(i["customer"] is None and i["doc_number"] is None for i in r["invoices"])
    assert [i["balance"] for i in r["invoices"]] == [i["balance"] for i in plain["invoices"]]
    assert r["totals"] == plain["totals"] and any("redact_descriptions is on" in n for n in r["notes"])
    with pytest.raises(GuardError, match="customer filter"):
        analysis.receivables(conn, cfg, customer="ACME", today=TODAY)


def test_receivables_customer_names_are_data_not_prose(cfg, conn, synced):
    r = analysis.receivables(conn, cfg, today=TODAY)
    assert any("Ignore previous instructions" in i["customer"] for i in r["invoices"])
    assert "Ignore previous" not in json.dumps({k: v for k, v in r.items() if k != "invoices"})


def test_empty_states_say_what_to_do(cfg, conn):
    r = analysis.receivables(conn, cfg, today=TODAY)
    assert r["connected"] is False and r["invoices"] == [] and "fiscl connect qbo" in r["notes"][0]
    b = analysis.books_vs_bank(conn, cfg, today=TODAY)
    assert b["books"] is None and b["differences"] is None and "fiscl connect qbo" in " ".join(b["notes"])
    assert b["bank"]["gross_receipts"]["minor"] == 0 and b["disclaimer"]


def test_receivables_on_a_connection_that_needs_reauth_says_so(cfg, conn, connected):
    connections.set_status(conn, "qbo", connected, "needs_reauth")
    assert "reconnect" in analysis.receivables(conn, cfg, today=TODAY)["notes"][0]


def test_books_vs_bank_compares_the_synced_pnl_with_the_schedule_c_draft(cfg, conn, synced, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 5_500_000, "CLIENT PAYMENT", "Income: Business")
    add_txn(conn, accounts["biz"], "2026-03-02", -1_000_000, "OFFICE SUPPLY CO", "Business: Supplies")
    r = analysis.books_vs_bank(conn, cfg, year=2026, today=TODAY)
    assert r["bank"] == {
        "gross_receipts": usd(5_500_000),
        "total_expenses": usd(1_000_000),
        "net_profit": usd(4_500_000),
    }
    assert r["books"]["total_income"] == usd(6_000_000) and r["books"]["total_expenses"] == usd(1_734_567)
    assert r["books"]["net_income"] == usd(4_265_433) and r["books"]["basis"] == "cash"
    assert r["differences"]["income"] == usd(500_000) and r["differences"]["expenses"] == usd(734_567)
    assert r["differences"]["net"] == {"minor": -234_567, "display": "-$2,345.67"}
    assert r["questions"][0].startswith("Your books show more income than business deposits.")
    assert "paid through a processor that nets fees" in r["questions"][0]
    assert len(r["questions"]) == 2 and r["disclaimer"] in r["notes"]
    last_year = analysis.books_vs_bank(conn, cfg, year=2025, today=TODAY)
    assert last_year["books"]["total_income"] == usd(5_000_000) and last_year["books"]["period"]["to"] == "2025-12-31"
    assert analysis.books_vs_bank(conn, cfg, year=2024, today=TODAY)["books"] is None


def test_books_vs_bank_asks_the_other_way_round_too(cfg, conn, synced, accounts):
    add_txn(conn, accounts["biz"], "2026-03-01", 9_000_000, "BIG CLIENT", "Income: Business")
    add_txn(conn, accounts["biz"], "2026-03-02", -5_000_000, "BIG SPEND", "Business: Supplies")
    r = analysis.books_vs_bank(conn, cfg, year=2026, today=TODAY)
    assert r["questions"][0].startswith("Your business deposits are higher")
    assert r["questions"][1].startswith("Your business-scope spending is higher")


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"Rows": {"Row": []}},
        {"Rows": {"Row": [{"group": "Other", "Summary": {"ColData": [{"value": "x"}, {"value": "1.00"}]}}]}},
        {"Header": {"Currency": "EUR"}, "Rows": PNL[2026]["Rows"]},
        {"Rows": []},
        {"Rows": {"Row": "oops"}},
    ],
)
def test_unrecognised_reports_parse_to_none(report):
    assert qbo.parse_profit_and_loss(report, "USD") is None


def test_reports_are_parsed_by_group_name_not_position():
    assert qbo.parse_profit_and_loss(PNL[2025], "USD") == {
        "total_income": 5_000_000,
        "total_expenses": 2_000_000,
        "net_income": 3_000_000,
    }
    assert qbo.parse_profit_and_loss(PNL[2026], "USD") == {
        "total_income": 6_000_000,
        "total_expenses": 1_734_567,
        "net_income": 4_265_433,
    }
    nested = {
        "Rows": {
            "Row": [
                {
                    "group": "Section",
                    "Rows": {"Row": [{"group": "Income", "Summary": {"ColData": [{"value": "T"}, {"value": "7.00"}]}}]},
                },
                {"group": "Income", "Summary": {"ColData": [{"value": "T"}, {"value": "9.00"}]}},
            ]
        }
    }
    assert qbo.parse_profit_and_loss(nested, "USD")["total_income"] == 900  # the shallowest section of a name wins


def test_a_report_that_cannot_be_read_is_skipped_with_a_warning_not_a_failure(cfg, conn, connected):
    api = FakeQbo()
    PNL_BAD = {"Header": {"Currency": "USD"}, "Rows": {"Row": []}}
    original = dict(PNL)
    try:
        PNL[2025] = PNL_BAD
        out = connector(cfg, conn, api).sync_records(conn, TODAY)
    finally:
        PNL.update(original)
    assert out["reports"] == 1 and any("profit-and-loss" in w for w in out["warnings"])


# --- connect: the OAuth redirect -------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(port, path, *, host=None, origin=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"localhost:{port}")
    if origin:
        c.putheader("Origin", origin)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


def callback(port, state, code=CODE, realm=REALM, **kw):
    q = urllib.parse.urlencode({"code": code, "state": state, "realmId": realm})
    return fetch(port, f"/qbo/callback?{q}", **kw)


@pytest.fixture
def port(cfg):
    cfg.qbo_redirect_port = free_port()
    return cfg.qbo_redirect_port


def run_connect(cfg, conn, api, steps, result=None, timeout=10, **kw):
    """Run connect_qbo with a fake browser that performs `steps` (each: (port, state) -> response)."""
    result = result if result is not None else {}
    result["responses"] = []

    def browser(url):
        result["url"] = url
        result["query"] = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

        def go():
            for step in steps:
                result["responses"].append(step(cfg.qbo_redirect_port, result["query"]["state"]))

        result["thread"] = threading.Thread(target=go, daemon=True)
        result["thread"].start()
        return True

    try:
        return connect.connect_qbo(cfg, conn, open_browser=browser, timeout=timeout, opener=api, **kw)
    finally:
        if "thread" in result:
            result["thread"].join(5)


def test_connect_qbo_end_to_end_stores_secrets_in_the_keychain_only(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    api, result = FakeQbo(), {}
    assert run_connect(cfg, conn, api, [callback], result) == "qbo"
    (status, headers, body) = result["responses"][0]
    assert status == 200 and b"QuickBooks is connected" in body
    assert (
        headers["content-security-policy"]
        == "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    assert CODE.encode() not in body and REALM.encode() not in body and COMPANY.encode() not in body
    # the browser was sent to Intuit's authorize page, with no PKCE and only the accounting scope
    url = urllib.parse.urlsplit(result["url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "appcenter.intuit.com", "/connect/oauth2")
    q = result["query"]
    assert q["client_id"] == CLIENT_ID and q["response_type"] == "code"
    assert (
        q["scope"] == "com.intuit.quickbooks.accounting"
        and q["redirect_uri"] == f"http://localhost:{port}/qbo/callback"
    )
    assert len(q["state"]) >= 43 and not [k for k in q if "challenge" in k or "verifier" in k]
    assert CLIENT_SECRET not in result["url"]
    # code exchange: POST with the registered redirect_uri; the companyinfo read named the company
    (token_call, info_call) = [c for c in api.calls if c[1] in (qbo.TOKEN_HOST, API)]
    assert token_call[0] == "POST" and token_call[1] == qbo.TOKEN_HOST
    form = dict(urllib.parse.parse_qsl(token_call[5].decode()))
    assert form == {"grant_type": "authorization_code", "code": CODE, "redirect_uri": q["redirect_uri"]}
    assert info_call[2] == f"/v3/company/{REALM}/companyinfo/{REALM}"
    assert not [c for c in api.calls if "appcenter" in c[1]]  # the authorize host is never requested by fiscl
    (row,) = connections.list_rows(conn, "qbo")
    assert row["institution"] == COMPANY and row["status"] == "ok" and row["item_ref_hash"] != REALM
    ref = row["item_ref_hash"]
    assert keychain.get_password("fiscl", f"fiscl:qbo:{ref}") == REFRESH_1
    assert keychain.get_password("fiscl", f"fiscl:qbo:{ref}.realm") == REALM
    dump = "\n".join(conn.iterdump())
    for secret in SECRETS:
        assert secret not in dump


def test_a_wrong_or_missing_state_is_refused_without_consuming_the_one_shot(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    api, result = FakeQbo(), {}
    steps = [
        lambda p, s: callback(p, "not-the-state"),
        lambda p, s: fetch(p, "/qbo/callback"),
        lambda p, s: fetch(p, f"/qbo/callback?code={CODE}&realmId={REALM}"),
        callback,  # the real answer still goes through
    ]
    assert run_connect(cfg, conn, api, steps, result) == "qbo"
    statuses = [r[0] for r in result["responses"]]
    assert statuses == [400, 400, 400, 200]
    for _status, _, body in result["responses"][:3]:
        assert b"not-the-state" not in body and CODE.encode() not in body
    assert [c for c in api.calls if c[1] == qbo.TOKEN_HOST] != [] and len(connections.list_rows(conn, "qbo")) == 1


def test_a_wrong_state_alone_never_ends_the_flow_it_times_out(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    api = FakeQbo()
    with pytest.raises(SyncError, match="timed out"):
        run_connect(cfg, conn, api, [lambda p, s: callback(p, "not-the-state")], timeout=1.5)
    assert api.calls == [] and connections.list_rows(conn, "qbo") == []


def _squat_ipv6(port):
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        s.bind(("::1", port))
    except OSError:
        s.close()
        pytest.skip("no IPv6 loopback")
    return s


def test_the_flow_reserves_ipv6_loopback_so_a_squatter_cannot_bind_it(cfg, port):
    probe = _squat_ipv6(port)
    probe.close()  # IPv6 loopback works here; free it for the server
    server = connect.make_oauth_server(port, "s", lambda c, r: "x")
    try:
        squatter = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        with pytest.raises(OSError) as exc:
            squatter.bind(("::1", port))
        assert exc.value.errno == errno.EADDRINUSE
        squatter.close()
        # requests still reach fiscl through 127.0.0.1
        results = []
        t = threading.Thread(target=lambda: results.append(callback(port, "s")), daemon=True)
        t.start()
        server.timeout = 5
        server.handle_request()
        t.join(5)
        assert results[0][0] == 200
    finally:
        server.server_close()
    again = _squat_ipv6(port)  # closing the server released the reservation
    again.close()


def test_an_occupied_ipv6_port_stops_the_flow_with_a_fixed_message(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    squatter = _squat_ipv6(port)
    try:
        with pytest.raises(SyncError, match=rf"another program is listening on localhost port {port}") as exc:
            connect.connect_qbo(cfg, conn, open_browser=lambda url: pytest.fail("opened"), timeout=1, opener=FakeQbo())
        assert "redirect_port" in str(exc.value)
    finally:
        squatter.close()
    # the 127.0.0.1 listener was released too
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_without_an_ipv6_loopback_the_reservation_is_skipped(cfg, port, monkeypatch):
    real = socket.socket

    class Fake:
        def __new__(cls, family=socket.AF_INET, *a, **k):
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "no ipv6")
            return real(family, *a, **k)

    monkeypatch.setattr(connect.socket, "socket", Fake)
    server = connect.make_oauth_server(port, "s", lambda c, r: "x")
    try:
        assert server._reserved is None
    finally:
        server.server_close()


@pytest.mark.parametrize(
    "query",
    [
        "state={state}",
        "state={state}&code=" + CODE,
        "state={state}&code=" + CODE + "&realmId=abc",
        "state={state}&error=access_denied",
    ],
)
def test_connect_rejects_incomplete_or_denied_answers(cfg, conn, keychain, port, query):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    api = FakeQbo()
    with pytest.raises(SyncError):
        run_connect(cfg, conn, api, [lambda p, s: fetch(p, "/qbo/callback?" + query.format(state=s))])
    assert connections.list_rows(conn, "qbo") == [] and [c for c in api.calls if c[1] == qbo.TOKEN_HOST] == []


def test_connect_ignores_requests_with_a_foreign_host_origin_or_path_and_keeps_waiting(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    result = {}
    steps = [
        lambda p, s: callback(p, s, host=f"evil.example:{p}"),
        lambda p, s: callback(p, s, host="localhost"),
        lambda p, s: callback(p, s, origin="https://evil.example"),
        lambda p, s: fetch(p, "/other?state=" + s),
        lambda p, s: fetch(p, "/qbo/callback/extra?state=" + s),
        lambda p, s: callback(p, s, method="POST"),
        callback,
    ]
    run_connect(cfg, conn, FakeQbo(), steps, result)
    assert [r[0] for r in result["responses"]] == [404, 404, 404, 404, 404, 405, 200]
    assert len(connections.list_rows(conn, "qbo")) == 1


def test_connect_accepts_both_loopback_names_for_the_host_header(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    result = {}
    run_connect(cfg, conn, FakeQbo(), [lambda p, s: callback(p, s, host=f"127.0.0.1:{p}")], result)
    assert result["responses"][0][0] == 200


def test_connect_server_answers_exactly_one_request(cfg, port):
    saved = []
    server = connect.make_oauth_server(port, "state-1", lambda code, realm: saved.append((code, realm)) or "Co")
    server.timeout = 5
    responses = []

    def browser():
        responses.append(callback(port, "state-1"))
        responses.append(callback(port, "state-1"))

    t = threading.Thread(target=browser, daemon=True)
    t.start()
    server.handle_request()
    server.handle_request()
    t.join(5)
    server.server_close()
    assert [r[0] for r in responses] == [200, 409] and saved == [(CODE, REALM)]


def test_connect_gives_up_after_the_timeout_without_a_request(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    with pytest.raises(SyncError, match="timed out"):
        connect.connect_qbo(cfg, conn, open_browser=lambda url: True, timeout=0.3, opener=FakeQbo())
    assert connections.list_rows(conn, "qbo") == []


def test_connect_binds_loopback_only_and_reports_a_busy_port(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    server = connect.make_oauth_server(port, "s", lambda c, r: "x")
    try:
        assert server.server_address[0] == "127.0.0.1"
        with pytest.raises(SyncError, match="redirect_port"):
            connect.connect_qbo(cfg, conn, open_browser=lambda url: True, timeout=1, opener=FakeQbo())
    finally:
        server.server_close()


def test_connect_refuses_without_keys_or_keychain(cfg, conn, keychain, monkeypatch, port):
    with pytest.raises(SyncError, match="app keys"):
        connect.connect_qbo(cfg, conn, open_browser=lambda url: pytest.fail("opened"), timeout=1)

    def none():
        raise SyncError(base.NO_KEYCHAIN)

    monkeypatch.setattr(base, "keyring_backend", none)
    with pytest.raises(SyncError, match="keychain"):
        connect.connect_qbo(cfg, conn, open_browser=lambda url: pytest.fail("opened"), timeout=1)


def test_the_cli_refuses_without_an_os_keychain_before_asking_for_anything(cfg, monkeypatch, capsys):
    def none():
        raise SyncError(base.NO_KEYCHAIN)

    def no_prompt(*a, **k):
        raise AssertionError("asked for a secret without a keychain")

    monkeypatch.setattr(base, "keyring_backend", none)
    monkeypatch.setattr("getpass.getpass", no_prompt)
    assert cli.main(["--home", str(cfg.home), "connect", "qbo"]) == 1
    assert "keychain" in capsys.readouterr().err


def test_a_failed_company_read_revokes_the_new_grant_and_stores_nothing(cfg, conn, keychain, port):
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    api = FakeQbo(fail={("GET", f"/v3/company/{REALM}/companyinfo/{REALM}"): herr(500)})
    with pytest.raises(SyncError):
        run_connect(cfg, conn, api, [callback])
    assert api.revoked == [REFRESH_1] and connections.list_rows(conn, "qbo") == []
    assert not [k for k in keychain.items if k[1] not in ("fiscl:qbo:client_id", "fiscl:qbo:client_secret")]


def test_a_keychain_failure_while_saving_revokes_and_leaves_no_row(cfg, conn, monkeypatch, port):
    events: list = []
    ref = connections.item_ref_hash("qbo", REALM)
    kr = RecordingKeyring(events, fail_on=[base.secret_name("qbo", ref)])
    monkeypatch.setattr(base, "keyring_backend", lambda: kr)
    qbo.store_app_credentials(CLIENT_ID, CLIENT_SECRET)
    api = FakeQbo()
    with pytest.raises(SyncError):
        run_connect(cfg, conn, api, [callback])
    assert api.revoked == [REFRESH_1] and connections.list_rows(conn, "qbo") == []
    assert qbo.realm_secret_name(ref) not in [k[1] for k in kr.items]


def test_reconnect_replaces_the_token_for_the_same_company_and_refuses_another(cfg, conn, connected, keychain, port):
    connections.set_status(conn, "qbo", connected, "needs_reauth")
    conn.commit()
    api = FakeQbo()
    run_connect(cfg, conn, api, [callback], reconnect=True)
    assert conn.execute("SELECT status FROM connections").fetchone()[0] == "ok"
    assert len(connections.list_rows(conn, "qbo")) == 1
    other = FakeQbo()
    connections.set_status(conn, "qbo", connected, "needs_reauth")
    conn.commit()
    other_realm = "9341459999999999"
    with pytest.raises(SyncError, match="different company"):
        run_connect(cfg, conn, other, [lambda p, s: callback(p, s, realm=other_realm)], reconnect=True)
    assert other.revoked == [REFRESH_1]  # the stray grant is revoked
    assert keychain.get_password("fiscl", base.secret_name("qbo", connected)) == REFRESH_1
    with pytest.raises(SyncError, match="no QuickBooks connection needs re-authorization"):
        connections.set_status(conn, "qbo", connected, "ok")
        connect.connect_qbo(cfg, conn, open_browser=lambda url: True, timeout=1, reconnect=True)


# --- forget -----------------------------------------------------------------------------------------------------------


def test_forget_revokes_at_intuit_and_deletes_every_keychain_entry_and_the_mirror(cfg, conn, synced, keychain):
    api, _ = synced
    ref = connections.item_ref_hash("qbo", REALM)
    stored = keychain.get_password("fiscl", base.secret_name("qbo", ref))
    assert registry.forget(conn, cfg, "qbo", opener=api) == 1
    assert api.revoked == [stored]
    revoke_call = [c for c in api.calls if c[1] == qbo.REVOKE_HOST][0]
    assert revoke_call[0] == "POST" and revoke_call[2] == "/v2/oauth2/tokens/revoke"
    assert keychain.items == {}
    assert conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM qbo_invoices").fetchone()[0] == 0  # cascade
    assert conn.execute("SELECT COUNT(*) FROM qbo_reports").fetchone()[0] == 0


def test_forget_keeps_everything_when_intuit_cannot_be_told(cfg, conn, connected, keychain):
    api = FakeQbo(fail={("POST", "/v2/oauth2/tokens/revoke"): herr(500)})
    with pytest.raises(SyncError):
        registry.forget(conn, cfg, "qbo", opener=api)
    assert keychain.get_password("fiscl", base.secret_name("qbo", connected)) == REFRESH_1
    assert len(connections.list_rows(conn, "qbo")) == 1


def test_forget_treats_an_already_dead_grant_as_revoked(cfg, conn, connected, keychain):
    api = FakeQbo(fail={("POST", "/v2/oauth2/tokens/revoke"): herr(400, b'{"error":"invalid_grant"}')})
    assert registry.forget(conn, cfg, "qbo", opener=api) == 1
    assert keychain.items == {} and connections.list_rows(conn, "qbo") == []


# --- surfaces -------------------------------------------------------------------------------------------------------


def test_sync_all_includes_qbo_when_connected_and_skips_it_otherwise(cfg, conn, connected):
    out = registry.sync_accounts(conn, cfg, provider="all", today=TODAY, opener=FakeQbo())
    assert list(out["results"]) == ["qbo"] and out["errors"] == []
    assert {s["provider"] for s in out["skipped"]} == {"simplefin", "teller", "plaid"}
    assert out["totals"]["rows_added"] == 0


def test_unconnected_qbo_is_a_clean_not_found(cfg, conn, keychain):
    with pytest.raises(GuardError) as exc:
        registry.sync_accounts(conn, cfg, provider="qbo", today=TODAY)
    assert exc.value.code == "not_found"


def test_list_connections_shows_the_company_and_status_but_no_token_or_realm(cfg, conn, synced):
    out = registry.list_connections(conn, cfg)
    (c,) = out["connections"]
    assert (c["provider"], c["institution"], c["status"]) == ("qbo", COMPANY, "ok") and c["last_sync"]
    text = json.dumps(out)
    for secret in SECRETS:
        assert secret not in text
    assert "qbo" not in out["not_connected"]


async def test_mcp_tools_and_the_secret_sweep_after_a_fixture_sync(cfg, conn, connected, keychain, monkeypatch):
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    api = FakeQbo()
    monkeypatch.setattr(registry, "date", FrozenDate)
    monkeypatch.setattr(base, "open_url", api)  # the connector's default opener
    conn.commit()
    async with Client(build_server(cfg)) as c:
        synced = await c.call_tool("sync_accounts", {"provider": "qbo"})
        recv = await c.call_tool("receivables", {"as_of": "2026-10-12"})
        books = await c.call_tool("books_vs_bank", {"year": 2026})
        listed = await c.call_tool("list_connections", {})
        bad = await c.call_tool("receivables", {"as_of": "whenever"})
    assert not synced.is_error and not recv.is_error and not books.is_error and not listed.is_error and bad.is_error
    assert synced.structured_content["results"]["qbo"]["invoices"] == 7
    assert recv.structured_content["totals"]["open_invoices"] >= 0
    assert books.structured_content["books"]["total_income"]["minor"] == 6_000_000
    conn.commit()
    haystacks = {
        "tool responses": json.dumps([x.structured_content for x in (synced, recv, books, listed)])
        + "".join(getattr(x, "text", "") for r in (synced, recv, books, listed, bad) for x in r.content),
        "audit log": (cfg.logs / "audit.log").read_text(),
        "database": "\n".join(conn.iterdump()),
        "db file": cfg.db_path.read_bytes().decode("latin-1"),
        "config files": "".join(p.read_text() for p in cfg.home.glob("*.toml")),
        "all logs": "".join(p.read_text(errors="ignore") for p in cfg.logs.glob("*")),
        "backups": "".join(p.read_bytes().decode("latin-1") for p in cfg.backups.glob("*")),
    }
    secrets_ = [CLIENT_ID, CLIENT_SECRET, REFRESH_1, ACCESS_1, REALM, api.refresh, api.access]
    for where, text in haystacks.items():
        for secret in secrets_:
            assert secret not in text, (where, secret[:8])
    assert "Ignore previous instructions" not in haystacks["audit log"]
    assert all(REFRESH_1 not in str(c[2]) for c in api.calls if c[1] == API)  # never sent to the API host


# --- migration -----------------------------------------------------------------------------------------------------


def test_migration_rebuilds_connections_for_qbo_keeping_row_ids_and_cascades(cfg):
    from fiscl.db import MIGRATIONS, migrate

    raw = sqlite3.connect(cfg.db_path)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    for sql in MIGRATIONS[:11]:
        raw.executescript(sql)
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    raw.execute("INSERT INTO schema_version VALUES (11)")
    raw.execute(
        "INSERT INTO connections (id, provider, institution, item_ref_hash, created_at, last_sync_at, status, cursor) "
        "VALUES (7, 'plaid', 'Bank', 'h1', '2026-01-01', '2026-02-01', 'needs_reauth', 'cur'), "
        "(9, 'teller', NULL, 'h2', '2026-01-02', NULL, 'ok', NULL)"
    )
    raw.commit()
    with pytest.raises(sqlite3.IntegrityError):  # before: 'qbo' isn't allowed
        raw.execute("INSERT INTO connections (provider, item_ref_hash, created_at) VALUES ('qbo', 'x', 'now')")
    raw.rollback()
    migrate(raw, cfg)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == len(MIGRATIONS)
    rows = [
        tuple(r) for r in raw.execute("SELECT id, provider, institution, status, cursor FROM connections ORDER BY id")
    ]
    assert rows == [(7, "plaid", "Bank", "needs_reauth", "cur"), (9, "teller", None, "ok", None)]
    raw.execute("INSERT INTO connections (id, provider, item_ref_hash, created_at) VALUES (20, 'qbo', 'x', 'now')")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("INSERT INTO connections (provider, item_ref_hash, created_at) VALUES ('bogus', 'y', 'now')")
    raw.execute(
        "INSERT INTO qbo_invoices VALUES (20, '1', '1', 'C', '2026-01-01', NULL, 100, 100, 'USD', '2026-01-01')"
    )
    raw.execute(
        "INSERT INTO qbo_reports VALUES (20, 'profit_and_loss', '2026-01-01', '2026-12-31', 'cash', 1, 2, 3, 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO qbo_reports VALUES (20, 'balance_sheet', '2026-01-01', '2026-12-31', 'cash', 1, 2, 3, 'now')"
        )
    raw.execute("DELETE FROM connections WHERE id = 20")
    assert raw.execute("SELECT COUNT(*) FROM qbo_invoices").fetchone()[0] == 0
    assert raw.execute("SELECT COUNT(*) FROM qbo_reports").fetchone()[0] == 0
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    raw.close()
