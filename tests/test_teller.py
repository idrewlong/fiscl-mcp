"""Teller connector: parsing from recorded-shape fixtures, pagination, sign convention, forget, and the
"no secret anywhere" sweep. Fixtures are synthetic but follow the shapes in Teller's API reference."""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.parse
from datetime import date

import pytest
from mcp import Client

from fiscl.guard import GuardError
from fiscl.server import build_server
from fiscl.sync import base, connections, registry, teller
from fiscl.sync.base import SyncError

from .conftest import TODAY

TOKEN = "tok_TELLERSECRET9999"
# Dummy PEM bodies, assembled so no real-looking key block sits in the source.
_BEGIN, _END = "-----BEGIN ", "-----END "
CERT = f"{_BEGIN}CERTIFICATE-----\nCERTSECRETBODY\n{_END}CERTIFICATE-----"
KEY = f"{_BEGIN}PRIVATE KEY-----\nKEYSECRETBODY\n{_END}PRIVATE KEY-----"
APP_ID = "app_APPIDSECRET"

ACCOUNTS = [
    {
        "id": "acc_chk1",
        "enrollment_id": "enr_1",
        "institution": {"id": "gulf_bank", "name": "Gulf Bank"},
        "type": "depository",
        "subtype": "checking",
        "name": "Everyday Checking",
        "currency": "USD",
        "last_four": "1234",
        "status": "open",
    },
    {
        "id": "acc_cc1",
        "enrollment_id": "enr_1",
        "institution": {"id": "gulf_bank", "name": "Gulf Bank"},
        "type": "credit",
        "subtype": "credit_card",
        "name": "Rewards Visa",
        "currency": "USD",
        "last_four": "9876",
        "status": "open",
    },
    {
        "id": "acc_old",
        "enrollment_id": "enr_1",
        "institution": {"id": "gulf_bank", "name": "Gulf Bank"},
        "type": "depository",
        "subtype": "savings",
        "name": "Closed Savings",
        "currency": "USD",
        "last_four": "5555",
        "status": "closed",
    },
]


def _txn(i, day, amount, desc, status="posted", acct="acc_chk1"):
    return {
        "id": f"txn_{i}",
        "account_id": acct,
        "amount": amount,
        "date": day,
        "description": desc,
        "status": status,
        "type": "card_payment",
        "running_balance": None,
        "details": {
            "processing_status": "complete",
            "category": "general",
            "counterparty": {"name": desc, "type": "organization"},
        },
    }


# Newest first, like Teller. Signs follow the convention pinned in teller.TELLER_AMOUNT_SIGN.
CHECKING_TXNS = [
    _txn(5, "2026-10-11", "-9.99", "SPOTIFY USA", status="pending"),
    _txn(4, "2026-10-10", "-42.10", "SHELL OIL 123"),
    _txn(3, "2026-10-08", "2500.00", "ACME PAYROLL"),
    _txn(2, "2026-10-05", "-61.25", "KROGER #123"),
    _txn(1, "2026-09-02", "-5.00", "OLD COFFEE"),  # before the sync window
]
CARD_TXNS = [
    _txn(11, "2026-10-09", "-19.00", "NETFLIX.COM", acct="acc_cc1"),
    _txn(10, "2026-10-04", "250.00", "PAYMENT THANK YOU", acct="acc_cc1"),
]
BALANCES = {
    "acc_chk1": {"account_id": "acc_chk1", "ledger": "1520.11", "available": "1500.00"},
    "acc_cc1": {"account_id": "acc_cc1", "ledger": "300.00", "available": None},
}


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTeller:
    """Routes requests the way api.teller.io would, and records them."""

    def __init__(self, accounts=None, txns=None, fail=None):
        self.accounts = ACCOUNTS if accounts is None else accounts
        self.txns = {"acc_chk1": CHECKING_TXNS, "acc_cc1": CARD_TXNS} if txns is None else txns
        self.fail = fail or {}  # (method, path) -> http status
        self.calls: list[tuple[str, str, dict, str | None]] = []

    def __call__(self, req, timeout):
        parts = urllib.parse.urlsplit(req.full_url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append((req.get_method(), parts.path, query, req.get_header("Authorization")))
        key = (req.get_method(), parts.path)
        if key in self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail[key], "x", {}, None)
        segments = parts.path.strip("/").split("/")
        if key == ("GET", "/accounts"):
            return FakeResp(json.dumps(self.accounts).encode())
        if req.get_method() == "DELETE":
            return FakeResp(b"")
        if segments[-1] == "balances":
            return FakeResp(json.dumps(BALANCES[segments[1]]).encode())
        if segments[-1] == "transactions":
            rows = self.txns[segments[1]]
            if "from_id" in query:
                ids = [r["id"] for r in rows]
                rows = rows[ids.index(query["from_id"]) + 1 :]
            lo, hi = query.get("start_date", "0000"), query.get("end_date", "9999")
            rows = [r for r in rows if not r["date"].startswith("20") or lo <= r["date"] <= hi]
            return FakeResp(json.dumps(rows[: int(query["count"])]).encode())
        raise AssertionError(f"unexpected request {key}")

    def paths(self, method="GET"):
        return [(p, q) for m, p, q, _ in self.calls if m == method]


@pytest.fixture
def connected(cfg, conn, keychain):
    """A Teller connection with its token in the fake keychain."""
    cfg.teller_environment = "sandbox"
    ref = connections.item_ref_hash("teller", "enr_1")
    keychain.set_password("fiscl", base.secret_name("teller", ref), TOKEN)
    connections.add_connection(conn, "teller", "Gulf Bank", ref)
    conn.commit()
    return ref


# --- sign convention (pinned; verify live before release) -----------------------------------------


def test_sign_convention_is_isolated_and_pinned():
    # If a live sandbox enrollment shows a different convention, change TELLER_AMOUNT_SIGN / TELLER_BALANCE_SIGN
    # and update this test deliberately. Nothing else interprets Teller's sign.
    assert teller.TELLER_AMOUNT_SIGN == {"depository": 1, "credit": 1}
    assert teller.TELLER_BALANCE_SIGN == {"depository": 1, "credit": -1}
    dep, card = {"type": "depository", "currency": "USD"}, {"type": "credit", "currency": "USD"}
    assert teller._teller_amount("-42.10", dep) == -4210
    assert teller._teller_amount("2500.00", dep) == 250000
    assert teller._teller_amount("-19.00", card) == -1900
    assert teller._teller_amount("250.00", card) == 25000
    assert teller._teller_balance("300.00", card) == -30000  # owed -> negative
    assert teller._teller_balance("1520.11", dep) == 152011


# --- parsing and pagination -----------------------------------------------------------------------


def test_fetch_parses_accounts_transactions_pending_and_balances(cfg, connected, monkeypatch):
    monkeypatch.setattr(teller, "PAGE_SIZE", 2)  # force from_id pagination
    api = FakeTeller()
    c = teller.TellerConnector(cfg, [teller.TellerItem(connected, "Gulf Bank")], opener=api)
    stmts = c.fetch(date(2026, 10, 1), date(2026, 10, 12))

    assert [s.account_key for s in stmts] == ["teller:acc_chk1", "teller:acc_cc1"]  # closed account skipped
    chk, card = stmts
    assert chk.format == "teller" and chk.institution == "Gulf Bank" and chk.last4 == "1234"
    assert chk.type_hint == "checking" and card.type_hint == "credit"
    assert chk.account_name == "Gulf Bank Everyday Checking ••1234"
    by_id = {t.fitid: t for t in chk.txns}
    assert set(by_id) == {"txn_5", "txn_4", "txn_3", "txn_2"}  # txn_1 is before the window
    assert by_id["txn_5"].pending is True and by_id["txn_4"].pending is False
    assert [by_id[k].amount for k in ("txn_5", "txn_4", "txn_3", "txn_2")] == [-999, -4210, 250000, -6125]
    assert by_id["txn_4"].description == "SHELL OIL 123" and by_id["txn_4"].posted_date == "2026-10-10"
    assert chk.balances == [("2026-10-12", 152011)] and card.balances == [("2026-10-12", -30000)]
    assert {t.fitid: t.amount for t in card.txns} == {"txn_11": -1900, "txn_10": 25000}
    assert c.warnings == [] and c.item_status == {connected: "ok"}

    # pagination walked backward with from_id; balances once per open account; only reads
    chk_pages = [q for p, q in api.paths() if p == "/accounts/acc_chk1/transactions"]
    assert [q.get("from_id") for q in chk_pages] == [None, "txn_4", "txn_2"]
    assert all(q["count"] == "2" for q in chk_pages)
    assert [p for p, _ in api.paths() if p.endswith("/balances")] == [
        "/accounts/acc_chk1/balances",
        "/accounts/acc_cc1/balances",
    ]
    assert all(m == "GET" for m, *_ in api.calls)


def test_basic_auth_carries_the_token_with_an_empty_password(cfg, connected):
    api = FakeTeller(accounts=[], txns={})
    teller.TellerConnector(cfg, [teller.TellerItem(connected)], opener=api).fetch(date(2026, 10, 1), TODAY)
    assert api.calls[0][3] == "Basic " + base64.b64encode(f"{TOKEN}:".encode()).decode()


def test_bad_rows_are_skipped_with_fixed_warnings(cfg, connected):
    rows = [
        _txn(1, "2026-10-05", "not money", "LEAKY MERCHANT NAME"),
        _txn(2, "garbage", "-1.00", "LEAKY MERCHANT NAME"),
        _txn(3, "2026-10-06", "-2.00", "  GOOD\x00  ROW  "),
    ]
    api = FakeTeller(accounts=ACCOUNTS[:1], txns={"acc_chk1": rows})
    c = teller.TellerConnector(cfg, [teller.TellerItem(connected)], opener=api)
    (stmt,) = c.fetch(date(2026, 10, 1), TODAY)
    assert [t.description for t in stmt.txns] == ["GOOD ROW"]
    assert len(c.warnings) == 2 and not any("LEAKY" in w for w in c.warnings)


def test_disconnected_enrollment_marks_needs_reauth(cfg, connected):
    api = FakeTeller(fail={("GET", "/accounts"): 404})
    c = teller.TellerConnector(cfg, [teller.TellerItem(connected)], opener=api)
    with pytest.raises(SyncError):
        c.fetch(date(2026, 10, 1), TODAY)
    assert c.item_status == {connected: "needs_reauth"}


# --- end to end through the import pipeline ---------------------------------------------------------


def test_sync_imports_resync_is_idempotent_and_updates_the_connection(cfg, conn, connected):
    out = registry.sync_accounts(conn, cfg, provider="teller", today=TODAY, opener=FakeTeller())
    res = out["results"]["teller"]
    assert res["rows_added"] == 7  # the 90-day first window includes the September row
    assert len(res["accounts"]) == 2
    assert out["errors"] == [] and out["skipped"] == []
    row = connections.list_rows(conn, "teller")[0]
    assert row["last_sync_at"] and row["status"] == "ok"

    again = registry.sync_accounts(conn, cfg, provider="teller", today=TODAY, opener=FakeTeller())
    assert again["results"]["teller"]["rows_added"] == 0
    pending = conn.execute("SELECT is_pending FROM transactions WHERE raw_description = 'SPOTIFY USA'").fetchone()[0]
    assert pending == 1
    assert conn.execute("SELECT type FROM accounts WHERE name LIKE '%Rewards%'").fetchone()[0] == "credit"


def test_sync_all_skips_unconnected_and_reports_failures(cfg, conn, connected):
    api = FakeTeller(fail={("GET", "/accounts"): 403})
    out = registry.sync_accounts(conn, cfg, provider="all", today=TODAY, opener=api)
    assert out["results"] == {} and [e["provider"] for e in out["errors"]] == ["teller"]
    assert {s["provider"] for s in out["skipped"]} == {"simplefin", "plaid"}
    assert connections.list_rows(conn, "teller")[0]["status"] == "needs_reauth"


def test_explicit_provider_not_connected_is_a_guard_error(cfg, conn, keychain):
    with pytest.raises(GuardError) as exc:
        registry.sync_accounts(conn, cfg, provider="teller", today=TODAY)
    assert exc.value.code == "not_found"
    with pytest.raises(GuardError):
        registry.sync_accounts(conn, cfg, provider="bogus")
    empty = registry.sync_accounts(conn, cfg, today=TODAY)
    assert empty["results"] == {} and "No bank is connected" in empty["meta"]["note"]


def test_list_connections_has_no_tokens(cfg, conn, connected):
    registry.sync_accounts(conn, cfg, provider="teller", today=TODAY, opener=FakeTeller())
    out = registry.list_connections(conn, cfg)
    (c,) = out["connections"]
    assert c["provider"] == "teller" and c["institution"] == "Gulf Bank" and c["status"] == "ok"
    assert sorted(c["accounts"]) == ["Gulf Bank Everyday Checking ••1234", "Gulf Bank Rewards Visa ••9876"]
    assert out["not_connected"] == ["simplefin", "plaid"]
    assert TOKEN not in json.dumps(out)


# --- forget -----------------------------------------------------------------------------------------


def _seed_app(keychain):
    for ref, val in (("app_id", APP_ID), ("cert", CERT), ("key", KEY)):
        keychain.set_password("fiscl", base.secret_name("teller", ref), val)


def test_forget_revokes_the_enrollment_and_deletes_the_keychain_entry(cfg, conn, connected, keychain):
    _seed_app(keychain)
    api = FakeTeller()
    registry.forget(conn, cfg, "teller", opener=api)
    assert [(m, p) for m, p, *_ in api.calls] == [("DELETE", "/accounts")]
    assert api.calls[0][3] == "Basic " + base64.b64encode(f"{TOKEN}:".encode()).decode()
    assert keychain.items == {}  # token and app credentials are all gone
    assert connections.list_rows(conn, "teller") == []


def test_forget_one_institution_keeps_others_and_app_credentials(cfg, conn, connected, keychain):
    _seed_app(keychain)
    other = connections.item_ref_hash("teller", "enr_2")
    keychain.set_password("fiscl", base.secret_name("teller", other), "tok_other")
    connections.add_connection(conn, "teller", "Other Bank", other)
    registry.forget(conn, cfg, "teller", institution="Gulf Bank", opener=FakeTeller())
    assert [r["institution"] for r in connections.list_rows(conn, "teller")] == ["Other Bank"]
    assert keychain.get_password("fiscl", base.secret_name("teller", other)) == "tok_other"
    assert keychain.get_password("fiscl", base.secret_name("teller", "cert")) == CERT


def test_forget_keeps_the_token_if_teller_could_not_be_told(cfg, conn, connected, keychain):
    with pytest.raises(SyncError, match="kept"):
        registry.forget(conn, cfg, "teller", opener=FakeTeller(fail={("DELETE", "/accounts"): 500}))
    assert keychain.get_password("fiscl", base.secret_name("teller", connected)) == TOKEN
    assert len(connections.list_rows(conn, "teller")) == 1


def test_forget_treats_an_already_revoked_token_as_done(cfg, conn, connected, keychain):
    registry.forget(conn, cfg, "teller", opener=FakeTeller(fail={("DELETE", "/accounts"): 403}))
    assert keychain.get_password("fiscl", base.secret_name("teller", connected)) is None


# --- credentials and config --------------------------------------------------------------------------


def test_pem_files_are_validated_and_paths_not_kept(tmp_path, keychain):
    c, k = tmp_path / "certificate.pem", tmp_path / "private_key.pem"
    c.write_text(CERT)
    k.write_text(KEY)
    assert teller.read_pem_files(str(c), str(k)) == (CERT, KEY)
    with pytest.raises(SyncError):
        teller.read_pem_files(str(k), str(c))  # swapped
    with pytest.raises(SyncError):
        teller.read_pem_files(str(tmp_path / "missing.pem"), str(k))
    teller.store_app_credentials(APP_ID, CERT, KEY)
    assert all(str(tmp_path) not in v for v in keychain.items.values())
    with pytest.raises(SyncError):
        teller.store_app_credentials("bad id!", None, None)


def test_environment_config(tmp_path):
    from fiscl.config import ConfigError, load_config

    home = tmp_path / "h"
    home.mkdir()
    assert load_config(home).teller_environment == "development"
    (home / "config.toml").write_text('[connectors.teller]\nenvironment = "sandbox"\n')
    assert load_config(home).teller_environment == "sandbox"
    (home / "config.toml").write_text('[connectors.teller]\nenvironment = "prod"\n')
    with pytest.raises(ConfigError):
        load_config(home)


def test_non_sandbox_needs_the_client_certificate(cfg, connected, keychain):
    cfg.teller_environment = "development"
    c = teller.TellerConnector(cfg, [teller.TellerItem(connected)])  # no opener injected
    with pytest.raises(SyncError, match="certificate"):
        c.fetch(date(2026, 10, 1), TODAY)


# --- MCP surface and the "no secret anywhere" sweep ----------------------------------------------------


async def test_mcp_sync_and_list_never_expose_secrets(cfg, conn, connected, keychain, monkeypatch):
    _seed_app(keychain)
    api = FakeTeller()

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(registry, "date", FrozenDate)
    monkeypatch.setattr(base, "open_url", api)  # the sandbox connector uses the default opener
    async with Client(build_server(cfg)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["sync_accounts"].annotations.open_world_hint is True
        assert tools["list_connections"].annotations.read_only_hint is True
        synced = await c.call_tool("sync_accounts", {"provider": "teller"})
        listed = await c.call_tool("list_connections", {})
        not_connected = await c.call_tool("sync_accounts", {"provider": "simplefin"})
    assert not synced.is_error and not listed.is_error
    assert synced.structured_content["results"]["teller"]["rows_added"] == 7
    assert not_connected.is_error

    conn.commit()
    secrets_ = [TOKEN, "CERTSECRETBODY", "KEYSECRETBODY", APP_ID, base64.b64encode(f"{TOKEN}:".encode()).decode()]
    haystacks = {
        "tool responses": json.dumps([synced.structured_content, listed.structured_content])
        + "".join(getattr(x, "text", "") for x in synced.content + listed.content + not_connected.content),
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
    # and the token never travelled in a URL
    assert all(TOKEN not in p for _, p, _, _ in api.calls)
