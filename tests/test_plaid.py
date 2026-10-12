"""Plaid connector: link-token body, allowlist, cursor sync, sign conventions, pending->posted, forget, the connect
page, and the "no secret anywhere" sweep. Fixtures are synthetic but follow the shapes in Plaid's API reference.
A live sandbox test runs only when FISCL_PLAID_CLIENT_ID and FISCL_PLAID_SECRET are set."""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
from datetime import date

import pytest
from mcp import Client

from fiscl.config import ConfigError, load_config
from fiscl.db import get_or_create_category
from fiscl.money import AmountError
from fiscl.server import build_server
from fiscl.sync import base, connect, connections, plaid, registry
from fiscl.sync.base import SyncError

from .conftest import TODAY, add_txn
from .test_teller import FakeResp

CLIENT_ID = "CLIENTIDSECRET0123456789ab"
SECRET = "PLAIDSECRETVALUE0123456789cd"
ACCESS = "access-sandbox-ACCESSSECRET-1111"
PUBLIC = "public-sandbox-PUBLICSECRET-2222"
LINK = "link-sandbox-0123456789abcdef"

ACCOUNTS = [
    {
        "account_id": "acc_chk",
        "name": "Plaid Checking",
        "mask": "0000",
        "type": "depository",
        "subtype": "checking",
        "balances": {"current": 110.0, "available": 100.0, "iso_currency_code": "USD"},
    },
    {
        "account_id": "acc_cc",
        "name": "Plaid Credit Card",
        "mask": "3333",
        "type": "credit",
        "subtype": "credit card",
        "balances": {"current": 410.5, "available": None, "iso_currency_code": "USD"},
    },
]


def ptxn(tid, day, amount, name, acct="acc_chk", pending=False, merchant=None, pending_id=None, authorized=None):
    return {
        "transaction_id": tid,
        "account_id": acct,
        "amount": amount,
        "date": day,
        "authorized_date": authorized,
        "name": name,
        "merchant_name": merchant,
        "pending": pending,
        "pending_transaction_id": pending_id,
        "iso_currency_code": "USD",
    }


def page(added=(), modified=(), removed=(), cursor="", more=False):
    return {
        "added": list(added),
        "modified": list(modified),
        "removed": [{"transaction_id": r} for r in removed],
        "next_cursor": cursor,
        "has_more": more,
    }


def perr(status, code):
    body = json.dumps({"error_type": "X", "error_code": code, "error_message": "msg with MERCHANT name"}).encode()
    return urllib.error.HTTPError("https://x", status, "err", {}, __import__("io").BytesIO(body))


class FakePlaid:
    """Routes requests the way sandbox.plaid.com would. `pages` maps the incoming cursor to the response page; a
    value that is an Exception instance is raised once, then the next entry for that cursor (a list) is used."""

    def __init__(self, pages=None, accounts=None, fail=None):
        self.pages = pages or {"": page(cursor="")}
        self.accounts = ACCOUNTS if accounts is None else accounts
        self.fail = fail or {}  # path -> exception or list of exceptions (consumed in order)
        self.calls: list[tuple[str, dict, object, object]] = []

    def __call__(self, req, timeout):
        parts = urllib.parse.urlsplit(req.full_url)
        body = json.loads(req.data.decode()) if req.data else {}
        self.calls.append((parts.path, body, req.get_header("Plaid-client-id"), req.get_header("Plaid-secret")))
        assert req.get_method() == "POST" and parts.netloc == "sandbox.plaid.com"
        failing = self.fail.get(parts.path)
        if isinstance(failing, list) and failing:
            raise failing.pop(0)
        if isinstance(failing, Exception):
            raise failing
        if parts.path == "/accounts/get":
            return FakeResp(json.dumps({"accounts": self.accounts}).encode())
        if parts.path == "/transactions/sync":
            entry = self.pages[body.get("cursor", "")]
            if isinstance(entry, list):
                entry = entry.pop(0)
            if isinstance(entry, Exception):
                raise entry
            return FakeResp(json.dumps(entry).encode())
        if parts.path == "/item/remove":
            return FakeResp(b'{"request_id": "r"}')
        if parts.path == "/link/token/create":
            return FakeResp(json.dumps({"link_token": LINK}).encode())
        if parts.path == "/item/public_token/exchange":
            return FakeResp(json.dumps({"access_token": ACCESS, "item_id": "item_1"}).encode())
        raise AssertionError(f"unexpected request {parts.path}")

    def paths(self, path):
        return [b for p, b, *_ in self.calls if p == path]


@pytest.fixture
def no_sockets(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def connected(cfg, conn, keychain):
    """A Plaid connection (sandbox) with keys and the access token in the fake keychain."""
    assert cfg.plaid_environment == "sandbox"
    keychain.set_password("fiscl", base.secret_name("plaid", "client_id"), CLIENT_ID)
    keychain.set_password("fiscl", base.secret_name("plaid", "secret"), SECRET)
    ref = connections.item_ref_hash("plaid", "item_1")
    keychain.set_password("fiscl", base.secret_name("plaid", ref), ACCESS)
    connections.add_connection(conn, "plaid", "First Platypus Bank", ref)
    conn.commit()
    return ref


def sync(conn, cfg, api):
    return registry.sync_accounts(conn, cfg, provider="plaid", today=TODAY, opener=api)


def cursor_of(conn, ref):
    return conn.execute("SELECT cursor FROM connections WHERE item_ref_hash = ?", (ref,)).fetchone()[0]


# --- what we ask for ----------------------------------------------------------------------------------


def test_link_token_body_asks_for_transactions_only():
    api = FakePlaid()
    client = plaid.PlaidClient("sandbox", CLIENT_ID, SECRET, api)
    assert plaid.create_link_token(client) == LINK
    (body,) = api.paths("/link/token/create")
    user = body.pop("user")
    assert list(user) == ["client_user_id"] and len(user["client_user_id"]) == 32
    assert body == {"client_name": "fiscl", "language": "en", "country_codes": ["US"], "products": ["transactions"]}
    assert plaid.PLAID_PRODUCTS == ("transactions",)
    assert "redirect_uri" not in body


def test_credentials_travel_in_unredirected_headers_not_the_body_or_url():
    api = FakePlaid()
    plaid.create_link_token(plaid.PlaidClient("sandbox", CLIENT_ID, SECRET, api))
    path, body, cid, sec = api.calls[0]
    assert (cid, sec) == (CLIENT_ID, SECRET)
    assert CLIENT_ID not in json.dumps(body) and SECRET not in json.dumps(body)


# --- allowlist: refused before any socket opens ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/auth/get",
        "/transfer/create",
        "/transfer/authorization/create",
        "/payment_initiation/payment/create",
        "/processor/token/create",
        "/identity/get",
        "/accounts/balance/get",
        "/investments/holdings/get",
        "/transactions/get",
        "/item/get",
        "/accounts/get/extra",
        "/sandbox/public_token/create/../x",
    ],
)
def test_non_allowlisted_endpoints_raise_before_any_socket(no_sockets, path):
    for env in plaid.ENVIRONMENTS:
        client = plaid.PlaidClient(env, CLIENT_ID, SECRET)  # default opener: a socket would trip no_sockets
        with pytest.raises(SyncError, match="not allowed"):
            client.call(path, {})


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "https://sandbox.plaid.com/accounts/get"),
        ("PUT", "https://sandbox.plaid.com/accounts/get"),
        ("POST", "http://sandbox.plaid.com/accounts/get"),
        ("POST", "https://development.plaid.com/accounts/get"),
        ("POST", "https://production.plaid.com/accounts/get"),  # wrong host for the sandbox policy
        ("POST", "https://sandbox.plaid.com.evil.example/accounts/get"),
        ("POST", "https://sandbox.plaid.com:8443/accounts/get"),
        ("POST", "https://user:pw@sandbox.plaid.com/accounts/get"),
    ],
)
def test_wrong_method_scheme_or_host_raises_before_any_socket(no_sockets, method, url):
    with pytest.raises(SyncError):
        base.request(plaid.policy_for("sandbox"), method, url)


def test_sandbox_endpoint_exists_only_in_the_sandbox_environment(no_sockets):
    plaid.policy_for("sandbox").check("POST", "https://sandbox.plaid.com/sandbox/public_token/create")
    with pytest.raises(SyncError):
        plaid.policy_for("production").check("POST", "https://production.plaid.com/sandbox/public_token/create")
    with pytest.raises(SyncError, match="sandbox"):
        plaid.sandbox_public_token(plaid.PlaidClient("production", CLIENT_ID, SECRET))
    for path in (
        "/link/token/create",
        "/item/public_token/exchange",
        "/transactions/sync",
        "/accounts/get",
        "/item/remove",
    ):
        plaid.policy_for("production").check("POST", "https://production.plaid.com" + path)
    with pytest.raises(SyncError):
        plaid.policy_for("development")


def test_a_redirect_from_plaid_raises_and_resends_nothing():
    def redirecting(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://evil.example/"}, None)

    with pytest.raises(SyncError, match="redirect"):
        plaid.PlaidClient("sandbox", CLIENT_ID, SECRET, redirecting).call("/accounts/get", {})


def test_environment_config(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    assert load_config(home).plaid_environment == "sandbox"
    (home / "config.toml").write_text('[connectors.plaid]\nenvironment = "production"\n')
    assert load_config(home).plaid_environment == "production"
    for bad in ("development", "prod"):
        (home / "config.toml").write_text(f'[connectors.plaid]\nenvironment = "{bad}"\n')
        with pytest.raises(ConfigError):
            load_config(home)


# --- sign conventions (pinned) -------------------------------------------------------------------------


def test_plaid_amount_inverts_the_sign():
    assert plaid._plaid_amount(89.4) == -8940  # positive = money out in Plaid
    assert plaid._plaid_amount(-2500) == 250000  # negative = money in
    assert plaid._plaid_amount(0.1 + 0.2) == -30
    assert plaid._plaid_amount("12.35") == -1235
    for bad in (None, "x", float("nan")):
        with pytest.raises(AmountError):
            plaid._plaid_amount(bad)


def test_plaid_balance_stores_owed_amounts_as_negative():
    dep, card, loan = {"type": "depository"}, {"type": "credit"}, {"type": "loan"}
    assert plaid._plaid_balance(1520.11, dep) == 152011
    assert plaid._plaid_balance(410.5, card) == -41050  # owed -> negative
    assert plaid._plaid_balance(20000, loan) == -2000000
    assert plaid._plaid_balance(-12.0, card) == 1200  # a credit balance (overpaid)


# --- cursor sync ----------------------------------------------------------------------------------------


def test_sync_paginates_applies_modified_and_removed_and_stores_the_cursor(cfg, conn, connected, accounts):
    pages = {
        "": page(
            added=[
                ptxn("t1", "2026-10-01", 89.4, "SHELL OIL 55", merchant="Shell"),
                ptxn("t2", "2026-10-02", -2500, "ACME PAYROLL"),
                ptxn("t3", "2026-10-03", 12.0, "DOOMED STORE"),
            ],
            cursor="c1",
            more=True,
        ),
        "c1": page(
            added=[ptxn("t4", "2026-10-04", 19.0, "NETFLIX", acct="acc_cc")],
            modified=[],
            cursor="c2",
            more=True,
        ),
        "c2": page(cursor="c3"),
    }
    api = FakePlaid(pages)
    out = sync(conn, cfg, api)
    assert out["errors"] == [] and out["results"]["plaid"]["rows_added"] == 4

    sync_calls = api.paths("/transactions/sync")
    assert [c.get("cursor") for c in sync_calls] == [None, "c1", "c2"]
    assert all(c["count"] == 500 and c["access_token"] == ACCESS for c in sync_calls)
    assert cursor_of(conn, connected) == "c3"

    rows = {
        r["fitid"]: r
        for r in conn.execute(
            "SELECT t.*, a.name AS acct, a.type AS atype FROM transactions t JOIN accounts a ON a.id = t.account_id"
        )
    }
    assert rows["t1"]["amount"] == -8940 and rows["t1"]["raw_description"] == "Shell"  # merchant_name preferred
    assert rows["t2"]["amount"] == 250000 and rows["t2"]["raw_description"] == "ACME PAYROLL"
    assert rows["t4"]["amount"] == -1900 and rows["t4"]["atype"] == "credit"
    assert rows["t1"]["acct"] == "First Platypus Bank Plaid Checking ••0000"
    bal = {
        r["name"]: r["amount"]
        for r in conn.execute("SELECT a.name, b.amount FROM balances b JOIN accounts a ON a.id = b.account_id")
    }
    assert sorted(bal.values()) == [-41050, 11000]

    # second sync: starts from the stored cursor; modified + removed arrive
    user_row = add_txn(conn, accounts["checking"], "2026-10-03", -1200, "DOOMED STORE")  # user's own row, no fitid
    conn.execute("UPDATE transactions SET fitid = 't3' WHERE id = ?", (user_row,))
    conn.execute("UPDATE transactions SET import_id = NULL WHERE id = ?", (user_row,))
    # the user categorized and noted t1 before Plaid modifies it
    cat = get_or_create_category(conn, "Fuel")
    conn.execute(
        "UPDATE transactions SET category_id = ?, category_source = 'manual', note = 'mine' WHERE fitid = 't1'", (cat,)
    )
    conn.commit()
    api2 = FakePlaid(
        {
            "c3": page(
                modified=[ptxn("t1", "2026-10-02", 90.0, "SHELL OIL 55", merchant="Shell Oil")],
                removed=["t3"],
                cursor="c4",
            )
        }
    )
    out2 = sync(conn, cfg, api2)
    assert out2["results"]["plaid"]["rows_added"] == 0
    assert [c.get("cursor") for c in api2.paths("/transactions/sync")] == ["c3"]
    t1 = conn.execute("SELECT * FROM transactions WHERE fitid = 't1'").fetchone()
    assert (t1["amount"], t1["posted_date"], t1["raw_description"]) == (-9000, "2026-10-02", "Shell Oil")
    assert t1["category_id"] == cat and t1["note"] == "mine" and t1["category_source"] == "manual"
    # t3 imported from Plaid is deleted; the user's own row that merely shares the id survives
    left = conn.execute("SELECT id, import_id FROM transactions WHERE fitid = 't3'").fetchall()
    assert [(r["id"], r["import_id"]) for r in left] == [(user_row, None)]
    assert cursor_of(conn, connected) == "c4"

    # nothing new: still safe, adds 0
    api3 = FakePlaid({"c4": page(cursor="c4")})
    assert sync(conn, cfg, api3)["results"]["plaid"]["rows_added"] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE fitid IS NOT NULL").fetchone()[0] == 4


def test_mutation_during_pagination_restarts_from_the_original_cursor(cfg, conn, connected):
    pages = {
        "": [
            page(added=[ptxn("t1", "2026-10-01", 5.0, "ONE")], cursor="c1", more=True),
            page(added=[ptxn("t1", "2026-10-01", 5.0, "ONE")], cursor="c1b", more=True),  # restarted pass
        ],
        "c1": [perr(400, plaid.MUTATION_ERROR)],
        "c1b": page(added=[ptxn("t2", "2026-10-02", 6.0, "TWO")], cursor="c9"),
    }
    api = FakePlaid(pages)
    out = sync(conn, cfg, api)
    assert [c.get("cursor") for c in api.paths("/transactions/sync")] == [None, "c1", None, "c1b"]
    assert out["results"]["plaid"]["rows_added"] == 2
    assert cursor_of(conn, connected) == "c9"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_persistent_mutation_errors_give_up_without_moving_the_cursor(cfg, conn, connected):
    err = lambda: perr(400, plaid.MUTATION_ERROR)  # noqa: E731
    api = FakePlaid({"": [page(cursor="c1", more=True)] * 4, "c1": [err() for _ in range(4)]})
    with pytest.raises(Exception, match="kept changing"):
        sync(conn, cfg, api)
    assert cursor_of(conn, connected) is None


def test_cursor_is_not_advanced_when_the_import_fails(cfg, conn, connected, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(registry, "import_statements", boom)
    api = FakePlaid({"": page(added=[ptxn("t1", "2026-10-01", 5.0, "ONE")], cursor="c1")})
    with pytest.raises(RuntimeError):
        sync(conn, cfg, api)
    assert cursor_of(conn, connected) is None


def test_first_pull_not_ready_warns_and_keeps_no_cursor(cfg, conn, connected):
    out = sync(conn, cfg, FakePlaid({"": page(cursor="")}))
    assert any("still preparing" in w for w in out["results"]["plaid"]["warnings"])
    assert cursor_of(conn, connected) is None


def test_pending_to_posted_via_pending_transaction_id_keeps_user_edits(cfg, conn, connected):
    first = FakePlaid(
        {"": page(added=[ptxn("p1", "2026-10-12", 20.0, "BISTRO", pending=True, authorized="2026-10-11")], cursor="c1")}
    )
    sync(conn, cfg, first)
    row = conn.execute("SELECT * FROM transactions WHERE fitid = 'p1'").fetchone()
    assert row["is_pending"] == 1 and row["posted_date"] == "2026-10-11" and row["amount"] == -2000
    cat = get_or_create_category(conn, "Dining")
    conn.execute(
        "UPDATE transactions SET category_id = ?, category_source = 'manual', note = 'client lunch' WHERE id = ?",
        (cat, row["id"]),
    )
    conn.execute("INSERT INTO tags (transaction_id, tag) VALUES (?, 'work')", (row["id"],))
    conn.commit()

    # the posted version has a tip added, so the amount differs: only pending_transaction_id can link them
    second = FakePlaid(
        {
            "c1": page(
                added=[ptxn("q1", "2026-10-13", 24.0, "BISTRO", pending_id="p1")],
                removed=["p1"],
                cursor="c2",
            )
        }
    )
    sync(conn, cfg, second)
    rows = conn.execute("SELECT * FROM transactions").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert (r["fitid"], r["is_pending"], r["amount"], r["posted_date"]) == ("q1", 0, -2400, "2026-10-13")
    assert r["note"] == "client lunch" and r["category_id"] == cat and r["category_source"] == "manual"
    assert [t[0] for t in conn.execute("SELECT tag FROM tags WHERE transaction_id = ?", (r["id"],))] == ["work"]


def test_login_required_marks_the_connection_needs_reauth(cfg, conn, connected):
    api = FakePlaid(fail={"/accounts/get": perr(400, "ITEM_LOGIN_REQUIRED")})
    with pytest.raises(Exception, match="Plaid"):
        sync(conn, cfg, api)
    assert connections.list_rows(conn, "plaid")[0]["status"] == "needs_reauth"


def test_bad_rows_are_skipped_with_fixed_warnings(cfg, connected):
    rows = [
        ptxn("a", "2026-10-01", "not money", "LEAKY MERCHANT NAME"),
        ptxn("b", "garbage", 1.0, "LEAKY MERCHANT NAME"),
        ptxn("c", "2026-10-02", 2.0, "  GOOD\x00  ROW  "),
        ptxn("d", "2026-10-02", 2.0, "X", acct="acc_unknown"),
    ]
    api = FakePlaid({"": page(added=rows, cursor="c1")})
    c = plaid.PlaidConnector(cfg, [plaid.PlaidItem(connected, "Bank")], opener=api)
    stmts = c.fetch(date(2026, 10, 1), TODAY)
    chk = next(s for s in stmts if s.account_key == "plaid:acc_chk")
    assert [t.description for t in chk.txns] == ["GOOD ROW"]
    assert len(c.warnings) == 3 and not any("LEAKY" in w for w in c.warnings)


def test_account_type_hints_last4_and_balance_date(cfg, connected):
    api = FakePlaid()
    c = plaid.PlaidConnector(cfg, [plaid.PlaidItem(connected, "Bank")], opener=api)
    chk, card = c.fetch(date(2026, 10, 1), TODAY)
    assert (chk.type_hint, card.type_hint) == ("checking", "credit")
    assert (chk.last4, card.last4) == ("0000", "3333")
    assert chk.balances == [("2026-10-12", 11000)] and card.balances == [("2026-10-12", -41050)]


# --- forget ---------------------------------------------------------------------------------------------


def test_forget_calls_item_remove_and_clears_the_keychain(cfg, conn, connected, keychain):
    api = FakePlaid()
    registry.forget(conn, cfg, "plaid", opener=api)
    assert api.paths("/item/remove") == [{"access_token": ACCESS}]
    assert keychain.items == {}
    assert connections.list_rows(conn, "plaid") == []


def test_forget_one_institution_keeps_the_keys_and_other_items(cfg, conn, connected, keychain):
    other = connections.item_ref_hash("plaid", "item_2")
    keychain.set_password("fiscl", base.secret_name("plaid", other), "access-sandbox-other-3333")
    connections.add_connection(conn, "plaid", "Other Bank", other)
    registry.forget(conn, cfg, "plaid", institution="First Platypus Bank", opener=FakePlaid())
    assert [r["institution"] for r in connections.list_rows(conn, "plaid")] == ["Other Bank"]
    assert keychain.get_password("fiscl", base.secret_name("plaid", other))
    assert keychain.get_password("fiscl", base.secret_name("plaid", "secret")) == SECRET


def test_forget_treats_item_not_found_as_already_removed(cfg, conn, connected, keychain):
    registry.forget(conn, cfg, "plaid", opener=FakePlaid(fail={"/item/remove": perr(400, "ITEM_NOT_FOUND")}))
    assert keychain.items == {}


def test_forget_keeps_the_token_if_plaid_could_not_be_told(cfg, conn, connected, keychain):
    with pytest.raises(SyncError, match="kept"):
        registry.forget(conn, cfg, "plaid", opener=FakePlaid(fail={"/item/remove": perr(500, "INTERNAL_SERVER_ERROR")}))
    assert keychain.get_password("fiscl", base.secret_name("plaid", connected)) == ACCESS
    assert len(connections.list_rows(conn, "plaid")) == 1


# --- keys ---------------------------------------------------------------------------------------------------


def test_keys_are_validated_and_required(keychain):
    with pytest.raises(SyncError):
        plaid.load_app_credentials()
    with pytest.raises(SyncError):
        plaid.store_app_credentials("short", SECRET)
    with pytest.raises(SyncError):
        plaid.store_app_credentials(CLIENT_ID, "has space and !!")
    assert not plaid.has_app_credentials()
    plaid.store_app_credentials(CLIENT_ID, SECRET)
    assert plaid.load_app_credentials() == (CLIENT_ID, SECRET)
    plaid.forget_app_credentials()
    assert keychain.items == {}


# --- the connect page and one-shot server -----------------------------------------------------------------------


def test_plaid_csp_allows_only_self_and_plaid_hosts():
    csp = connect.plaid_csp("N0NCE")
    assert csp.startswith("default-src 'none'") and "'nonce-N0NCE'" in csp
    assert set(connect.PLAID_LINK_HOSTS) == {"https://cdn.plaid.com", "https://*.plaid.com"}
    assert set(__import__("re").findall(r"https://[^\s;]+", csp)) == set(connect.PLAID_LINK_HOSTS)
    for directive in ("script-src", "frame-src", "connect-src"):
        part = next(p for p in csp.split("; ") if p.startswith(directive))
        assert "https://cdn.plaid.com" in part and "https://*.plaid.com" in part
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp and "script-src 'unsafe-inline'" not in csp


def test_plaid_page_loads_the_official_script_and_embeds_the_token_safely():
    page_ = connect.plaid_page('link-"</script><b>', "n")
    assert 'src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"' in page_
    assert "</script><b>" not in page_ and "\\u003c/script" in page_
    assert "innerHTML" not in page_ and "Plaid.create" in page_
    assert "products" not in page_  # products are fixed server side in the link token


def test_public_token_result_validation():
    ok = {"public_token": PUBLIC, "institution": {"name": "First\nPlatypus"}}
    assert connect.parse_plaid_result(ok) == (PUBLIC, "First Platypus")
    for bad in ({}, {"public_token": 5}, {"public_token": "access-sandbox-x"}, {"public_token": "public-../x"}):
        with pytest.raises(SyncError):
            connect.parse_plaid_result(bad)


def _browser(posts, result, threads):
    def browser(url):
        def go():
            u = urllib.parse.urlsplit(url)
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
            c.request("GET", u.path)
            r = c.getresponse()
            result["csp"], result["page"] = r.getheader("Content-Security-Policy"), r.read().decode()
            for body in posts:
                text = json.dumps(body)
                c.request("POST", u.path, text, {"Content-Type": "application/json", "Content-Length": str(len(text))})
                resp = c.getresponse()
                result.setdefault("statuses", []).append(resp.status)
                resp.read()

        t = threading.Thread(target=go, daemon=True)
        threads.append(t)
        t.start()
        return True

    return browser


def test_connect_plaid_end_to_end_stores_the_token_in_the_keychain_only(cfg, conn, keychain):
    plaid.store_app_credentials(CLIENT_ID, SECRET)
    api, result, threads = FakePlaid(), {}, []
    good = {"public_token": PUBLIC, "institution": {"name": "First Platypus Bank"}}
    connect.connect_plaid(cfg, conn, open_browser=_browser([good], result, threads), timeout=10, opener=api)
    threads[0].join(5)
    assert result["statuses"] == [200]
    assert LINK in result["page"] and "cdn.plaid.com" in result["csp"] and "'nonce-" in result["csp"]
    assert api.paths("/link/token/create")[0]["products"] == ["transactions"]
    assert api.paths("/item/public_token/exchange") == [{"public_token": PUBLIC}]
    (row,) = connections.list_rows(conn, "plaid")
    assert row["institution"] == "First Platypus Bank" and row["cursor"] is None
    assert keychain.get_password("fiscl", f"fiscl:plaid:{row['item_ref_hash']}") == ACCESS
    dump = "\n".join(conn.iterdump())
    assert ACCESS not in dump and PUBLIC not in dump and SECRET not in dump


def test_connect_plaid_reports_a_failed_exchange_and_stores_nothing(cfg, conn, keychain):
    plaid.store_app_credentials(CLIENT_ID, SECRET)
    api = FakePlaid(fail={"/item/public_token/exchange": [perr(400, "INVALID_PUBLIC_TOKEN")]})
    result, threads = {}, []
    with pytest.raises(SyncError, match="INVALID_PUBLIC_TOKEN"):
        connect.connect_plaid(
            cfg, conn, open_browser=_browser([{"public_token": PUBLIC}], result, threads), timeout=10, opener=api
        )
    threads[0].join(5)
    assert result["statuses"] == [400] and connections.list_rows(conn, "plaid") == []
    assert not [
        k
        for k in keychain.items
        if k[1].startswith("fiscl:plaid:") and k[1] not in ("fiscl:plaid:client_id", "fiscl:plaid:secret")
    ]


def test_connect_plaid_refuses_without_keys_or_keychain(cfg, conn, keychain, monkeypatch):
    with pytest.raises(SyncError, match="Plaid keys"):
        connect.connect_plaid(cfg, conn, open_browser=lambda url: True, timeout=1)

    def none():
        raise SyncError("no OS keychain is available")

    monkeypatch.setattr(base, "keyring_backend", none)
    with pytest.raises(SyncError, match="keychain"):
        connect.connect_plaid(cfg, conn, open_browser=lambda url: True, timeout=1)


def test_connect_plaid_in_production_never_leaves_the_production_host(cfg, conn, keychain, no_sockets):
    cfg.plaid_environment = "production"
    plaid.store_app_credentials(CLIENT_ID, SECRET)

    def prod_only(req, timeout):
        assert urllib.parse.urlsplit(req.full_url).netloc == "production.plaid.com"
        raise urllib.error.URLError("offline")

    with pytest.raises(SyncError, match="reach"):
        connect.connect_plaid(cfg, conn, open_browser=lambda url: True, timeout=1, opener=prod_only)


# --- MCP surface and the "no secret anywhere" sweep ---------------------------------------------------------


async def test_mcp_sync_and_list_never_expose_secrets(cfg, conn, connected, keychain, monkeypatch):
    api = FakePlaid(
        {
            "": page(
                added=[ptxn("t1", "2026-10-01", 89.4, "SHELL OIL 55"), ptxn("t2", "2026-10-02", 3.0, "COFFEE")],
                cursor="cur1",
            ),
            "cur1": page(cursor="cur1"),
        }
    )

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(registry, "date", FrozenDate)
    monkeypatch.setattr(base, "open_url", api)  # the connector's default opener
    async with Client(build_server(cfg)) as c:
        synced = await c.call_tool("sync_accounts", {"provider": "plaid"})
        everything = await c.call_tool("sync_accounts", {})
        listed = await c.call_tool("list_connections", {})
    assert not synced.is_error and not listed.is_error and not everything.is_error
    assert synced.structured_content["results"]["plaid"]["rows_added"] == 2
    assert "plaid" in everything.structured_content["results"]
    (entry,) = listed.structured_content["connections"]
    assert entry["provider"] == "plaid" and entry["status"] == "ok" and entry["last_sync"]
    assert entry["accounts"] == ["First Platypus Bank Plaid Checking ••0000"]  # accounts with imported rows

    conn.commit()
    secrets_ = [CLIENT_ID, SECRET, ACCESS, PUBLIC]
    haystacks = {
        "tool responses": json.dumps(
            [synced.structured_content, everything.structured_content, listed.structured_content]
        )
        + "".join(getattr(x, "text", "") for x in synced.content + listed.content + everything.content),
        "audit log": (cfg.logs / "audit.log").read_text(),
        "database": "\n".join(conn.iterdump()),
        "db file": cfg.db_path.read_bytes().decode("latin-1"),
        "config files": "".join(p.read_text() for p in cfg.home.glob("*.toml")),
        "all logs": "".join(p.read_text(errors="ignore") for p in cfg.logs.glob("*")),
        "backups": "".join(p.read_bytes().decode("latin-1") for p in cfg.backups.glob("*")),
    }
    for where, text in haystacks.items():
        for secret in secrets_:
            assert secret not in text, (where, secret[:8])
    assert "cur1" in "\n".join(conn.iterdump())  # the (non-secret) cursor is what the DB holds
    assert all(ACCESS not in p for p, *_ in api.calls)


def test_provider_listed_in_registry_and_unconnected_plaid_is_a_guard_error(cfg, conn, keychain):
    from fiscl.guard import GuardError

    assert "plaid" in registry.PROVIDERS
    with pytest.raises(GuardError) as exc:
        registry.sync_accounts(conn, cfg, provider="plaid", today=TODAY)
    assert exc.value.code == "not_found"


def test_errors_never_carry_provider_message_text(cfg, conn, connected):
    api = FakePlaid(fail={"/accounts/get": perr(400, "INVALID_REQUEST")})
    with pytest.raises(Exception) as exc:
        sync(conn, cfg, api)
    assert "MERCHANT" not in str(exc.value) and "msg" not in str(exc.value)


# --- live sandbox (skipped unless keys are provided) ---------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("FISCL_PLAID_CLIENT_ID") and os.environ.get("FISCL_PLAID_SECRET")),
    reason="set FISCL_PLAID_CLIENT_ID and FISCL_PLAID_SECRET (Plaid sandbox keys) to run",
)
def test_live_sandbox_roundtrip(cfg, conn, keychain):
    """sandbox public token -> exchange -> sync -> re-sync adds 0 -> forget. Hits sandbox.plaid.com for real."""
    cfg.plaid_environment = "sandbox"
    plaid.store_app_credentials(os.environ["FISCL_PLAID_CLIENT_ID"], os.environ["FISCL_PLAID_SECRET"])
    client = plaid.PlaidClient("sandbox", *plaid.load_app_credentials())
    access, item_id = plaid.exchange_public_token(client, plaid.sandbox_public_token(client))
    ref = connections.item_ref_hash("plaid", item_id)
    plaid.store_access_token(ref, access)
    connections.add_connection(conn, "plaid", "First Platypus Bank", ref)
    conn.commit()

    added = 0
    for _ in range(15):  # the first pull can lag right after linking
        added += registry.sync_accounts(conn, cfg, provider="plaid")["results"]["plaid"]["rows_added"]
        if added:
            break
        time.sleep(2)
    assert added > 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE fitid IS NOT NULL").fetchone()[0] == added
    assert cursor_of(conn, ref)
    assert registry.sync_accounts(conn, cfg, provider="plaid")["results"]["plaid"]["rows_added"] == 0
    assert access not in "\n".join(conn.iterdump())

    registry.forget(conn, cfg, "plaid")
    assert connections.list_rows(conn, "plaid") == []
    assert base.secret_get(base.secret_name("plaid", ref)) is None
    assert plaid.has_app_credentials() is False
