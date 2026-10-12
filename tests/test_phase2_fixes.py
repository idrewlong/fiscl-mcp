"""Phase 2 QA + security review fixes (SEC-1 .. SEC-13). One section per finding; the id is in each test's name
or docstring so a regression points straight at the finding it reopens."""

from __future__ import annotations

import ast
import base64
import io
import json
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from fiscl import cli, guard
from fiscl.guard import GuardError
from fiscl.importers.core import import_statements
from fiscl.importers.types import ParsedStatement, ParsedTxn
from fiscl.sync import base, connect, connections, plaid, registry, simplefin, teller
from fiscl.sync.base import SyncError

from .conftest import ALLOWED_REFERENCE_URLS, TODAY, FakeKeyring
from .test_phase2_qa import _post, _raw, _srv
from .test_plaid import (
    ACCESS,
    ACCOUNTS,
    CLIENT_ID,
    PUBLIC,
    SECRET,
    FakePlaid,
    _browser,
    connected,  # noqa: F401  (fixture, re-exported for plaid_connected below)
    page,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "fiscl"


@pytest.fixture
def plaid_connected(connected):  # noqa: F811
    return connected


# ---------------------------------------------------------------------------------------------------
# SEC-1: only a real OS keychain is accepted


def _backend(path: str, **attrs):
    mod, _, name = path.rpartition(".")
    return type(name, (), {"__module__": mod, **attrs})()


def _install_keyring(monkeypatch, backend):
    kr = types.ModuleType("keyring")
    kr.get_keyring = lambda: backend
    monkeypatch.setitem(sys.modules, "keyring", kr)
    return kr


@pytest.mark.parametrize("path", sorted(base.OS_KEYCHAIN_BACKENDS))
def test_sec1_every_allowlisted_os_backend_is_accepted(monkeypatch, path):
    kr = _install_keyring(monkeypatch, _backend(path))
    assert base.keyring_backend() is kr


@pytest.mark.parametrize(
    "path",
    [
        "keyrings.alt.file.PlaintextKeyring",
        "keyrings.alt.file.EncryptedKeyring",
        "keyring.backends.null.Keyring",
        "keyring.backends.fail.Keyring",
        "somewhere.else.Keyring",
        "keyring.backends.macOS.Imposter",
    ],
)
def test_sec1_plaintext_null_fail_and_unknown_backends_are_refused(monkeypatch, path):
    _install_keyring(monkeypatch, _backend(path))
    with pytest.raises(SyncError, match="no OS keychain is available.*plain text"):
        base.keyring_backend()


def test_sec1_env_selected_plaintext_backend_is_refused(monkeypatch):
    # PYTHON_KEYRING_BACKEND / keyringrc.cfg make keyring.get_keyring() return the chosen class; we judge the result.
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyrings.alt.file.PlaintextKeyring")
    _install_keyring(monkeypatch, _backend("keyrings.alt.file.PlaintextKeyring"))
    with pytest.raises(SyncError, match="no OS keychain"):
        base.keyring_backend()


def test_sec1_chainer_is_checked_member_by_member(monkeypatch):
    mac, plain = _backend("keyring.backends.macOS.Keyring"), _backend("keyrings.alt.file.PlaintextKeyring")
    _install_keyring(monkeypatch, _backend("keyring.backends.chainer.ChainerBackend", backends=[mac, plain]))
    with pytest.raises(SyncError, match="no OS keychain"):
        base.keyring_backend()
    kr = _install_keyring(monkeypatch, _backend("keyring.backends.chainer.ChainerBackend", backends=[mac]))
    assert base.keyring_backend() is kr
    _install_keyring(monkeypatch, _backend("keyring.backends.chainer.ChainerBackend", backends=[]))
    with pytest.raises(SyncError):
        base.keyring_backend()


def test_sec1_simplefin_uses_the_same_check(monkeypatch):
    _install_keyring(monkeypatch, _backend("keyrings.alt.file.PlaintextKeyring"))
    token = base64.b64encode(b"https://bridge.simplefin.org/simplefin/claim/abc").decode()

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    opener = lambda req, timeout: Resp(b"https://user:pass@bridge.simplefin.org/simplefin")  # noqa: E731
    with pytest.raises(SyncError, match="no OS keychain"):
        simplefin.claim(token, opener=opener)
    with pytest.raises(SyncError, match="no OS keychain"):
        simplefin.forget()
    assert simplefin.is_connected() is False


def test_sec1_security_md_documents_the_allowlist():
    text = (SRC.parent.parent / "SECURITY.md").read_text(encoding="utf-8")
    for path in base.OS_KEYCHAIN_BACKENDS:
        assert path in text


# ---------------------------------------------------------------------------------------------------
# SEC-2 / BUG-1: the connect server can't be held by a stalled client (the 3 pinned cases are in test_phase2_qa)


def test_sec2_a_stalled_body_does_not_burn_the_single_use_slot():
    s, posts = _srv()
    s.deadline = time.monotonic() + 0.6  # what serve_once sets; bounds the handler's socket reads
    stalled = socket.create_connection(("127.0.0.1", s.server_port))
    head = f"POST /{s.token} HTTP/1.1\r\nHost: 127.0.0.1:{s.server_port}\r\nContent-Type: application/json\r\n"
    stalled.sendall((head + "Content-Length: 50\r\n\r\n").encode())
    t = threading.Thread(target=s.handle_request)
    t.start()
    t.join(5)
    stalled.close()
    assert not t.is_alive()
    assert s.consumed is False
    s.deadline = None
    out = _post(s, b'{"ok": 1}')
    s.server_close()
    assert b" 200 " in out.split(b"\r\n")[0] and posts == [{"ok": 1}] and s.consumed


def test_sec2_a_complete_but_invalid_post_still_consumes_the_slot():
    s, posts = _srv()
    out = _post(s, b"not json")
    again = _post(s, b'{"ok": 1}')
    s.server_close()
    assert b" 400 " in out.split(b"\r\n")[0] and b" 409 " in again.split(b"\r\n")[0] and posts == []


def test_sec2_a_client_that_hangs_up_mid_body_does_not_crash_or_consume():
    s, posts = _srv()
    head = f"POST /{s.token} HTTP/1.1\r\nHost: 127.0.0.1:{s.server_port}\r\nContent-Type: application/json\r\n"
    _raw(s, (head + "Content-Length: 50\r\n\r\n{").encode())  # sends 1 byte then closes
    assert s.consumed is False and posts == []
    s.server_close()


# ---------------------------------------------------------------------------------------------------
# SEC-3: provider data never binds to an account by bank-supplied name


def _stmt(fmt, key, name, balances=(), fitid="f1"):
    return ParsedStatement(
        format=fmt,
        txns=[ParsedTxn("2026-10-01", -500, "Coffee", fitid=fitid)],
        account_key=key,
        type_hint="checking",
        institution="Gulf Bank",
        balances=list(balances),
        account_name=name,
    )


def test_sec3_a_name_collision_with_a_bound_account_creates_a_separate_account(cfg, conn):
    import_statements(conn, cfg, [_stmt("teller", "teller:acc_real", "Gulf Checking")], "a", "d1")
    real = conn.execute("SELECT id, acct_hash FROM accounts WHERE name = 'Gulf Checking'").fetchone()
    res = import_statements(conn, cfg, [_stmt("plaid", "plaid:acc_other", "Gulf Checking", fitid="p1")], "b", "d2")
    names = [r["name"] for r in conn.execute("SELECT name FROM accounts ORDER BY id")]
    assert names == ["Gulf Checking", "Gulf Checking (plaid)"] and res.accounts == ["Gulf Checking (plaid)"]
    assert conn.execute("SELECT acct_hash FROM accounts WHERE id = ?", (real["id"],)).fetchone()[0] == real["acct_hash"]
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (real["id"],)).fetchone()[0] == 1
    # a third collision keeps counting rather than failing
    import_statements(conn, cfg, [_stmt("plaid", "plaid:acc_third", "Gulf Checking", fitid="p2")], "c", "d3")
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE name LIKE 'Gulf Checking (plaid)%'").fetchone()[0] == 2


def test_sec3_an_unbound_account_with_the_same_name_is_still_adopted(cfg, conn):
    from fiscl.db import add_account

    mine = add_account(conn, "Gulf Checking", "checking")
    conn.commit()
    res = import_statements(conn, cfg, [_stmt("teller", "teller:acc_1", "Gulf Checking")], "a", "d1")
    row = conn.execute("SELECT id, acct_hash FROM accounts").fetchall()
    assert len(row) == 1 and row[0]["id"] == mine and row[0]["acct_hash"] and res.accounts == ["Gulf Checking"]


@pytest.mark.parametrize("fmt", ["teller", "plaid", "simplefin"])
def test_sec3_connector_balances_are_recorded_with_the_sync_source(cfg, conn, fmt):
    import_statements(conn, cfg, [_stmt(fmt, f"{fmt}:a", "Acct", balances=[("2026-10-01", 12345)])], "a", "d1")
    assert [tuple(r) for r in conn.execute("SELECT source, amount FROM balances")] == [("sync", 12345)]


# ---------------------------------------------------------------------------------------------------
# SEC-4: Plaid `removed` only deletes inside the syncing Item's accounts


def test_sec4_removed_transactions_are_scoped_to_the_items_own_accounts(cfg, conn, plaid_connected, keychain):
    mine = _stmt("plaid", "plaid:acc_chk", "Mine", fitid="t_mine")
    other = _stmt("plaid", "plaid:acc_other_item", "Other Item", fitid="t_other")
    import_statements(conn, cfg, [mine, other], "seed", "d0")
    conn.commit()
    api = FakePlaid(pages={"": page(removed=["t_mine", "t_other"], cursor="c1")}, accounts=[ACCOUNTS[0]])
    registry.sync_accounts(conn, cfg, provider="plaid", today=TODAY, opener=api)
    left = {r[0] for r in conn.execute("SELECT fitid FROM transactions")}
    assert left == {"t_other"}


# ---------------------------------------------------------------------------------------------------
# SEC-5: only "not found" is ignored when deleting a keychain entry


class _BrokenDelete(FakeKeyring):
    def delete_password(self, service, user):
        raise RuntimeError("keychain locked")


def test_sec5_delete_ignores_not_found_but_surfaces_real_errors(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(base, "keyring_backend", lambda: kr)
    base.secret_delete("fiscl:teller:missing")  # PasswordDeleteError: fine
    broken = _BrokenDelete()
    monkeypatch.setattr(base, "keyring_backend", lambda: broken)
    with pytest.raises(SyncError, match="couldn't delete a stored credential.*nothing was disconnected"):
        base.secret_delete("fiscl:teller:x")
    with pytest.raises(SyncError, match="nothing was disconnected"):
        simplefin.forget()


def test_sec5_a_failed_keychain_delete_keeps_the_connection_row(cfg, conn, plaid_connected, keychain, monkeypatch):
    class Locked(FakeKeyring):
        def delete_password(self, service, user):
            raise RuntimeError("locked")

    locked = Locked()
    locked.items = keychain.items
    monkeypatch.setattr(base, "keyring_backend", lambda: locked)
    with pytest.raises(SyncError, match="nothing was disconnected"):
        registry.forget(conn, cfg, "plaid", opener=FakePlaid())
    assert len(connections.list_rows(conn, "plaid")) == 1


# ---------------------------------------------------------------------------------------------------
# SEC-6: forgetting an institution that isn't connected is an error, and success reports a count


def test_sec6_forget_unknown_institution_is_not_found(cfg, conn, plaid_connected, keychain):
    with pytest.raises(GuardError) as ei:
        registry.forget(conn, cfg, "plaid", "No Such Bank", opener=FakePlaid())
    assert ei.value.code == "not_found" and "No Such Bank" not in str(ei.value)
    assert len(connections.list_rows(conn, "plaid")) == 1


def test_sec6_forget_returns_how_many_were_revoked(cfg, conn, plaid_connected, keychain):
    assert registry.forget(conn, cfg, "plaid", "First Platypus Bank", opener=FakePlaid()) == 1


def test_sec6_cli_does_not_claim_a_disconnect_for_a_miss(cfg, conn, plaid_connected, keychain, capsys):
    rc = cli.main(["--home", str(cfg.home), "connect", "forget", "plaid", "--institution", "Nope"])
    out = capsys.readouterr()
    assert rc == 1 and "Disconnected" not in out.out and "connect error" in out.err


# ---------------------------------------------------------------------------------------------------
# SEC-7: a failed save never leaves a live grant, and never destroys a working token


def _boom(*a, **k):
    raise RuntimeError("disk full")


def _enrollment(eid="enr_7", token="tok_NEW"):
    return {"accessToken": token, "enrollment": {"id": eid, "institution": {"name": "Gulf Bank"}}}


def test_sec7_failed_teller_save_removes_the_token_and_revokes_the_new_grant(conn, keychain, monkeypatch):
    monkeypatch.setattr(connections, "add_connection", _boom)
    revoked = []
    with pytest.raises(RuntimeError):
        connect.save_teller_enrollment(conn, _enrollment(), revoked.append)
    assert revoked == ["tok_NEW"] and keychain.items == {}


def test_sec7_failed_teller_save_restores_the_previous_token_and_does_not_revoke(conn, keychain, monkeypatch):
    ref = connections.item_ref_hash("teller", "enr_7")
    keychain.set_password("fiscl", base.secret_name("teller", ref), "tok_WORKING")
    monkeypatch.setattr(connections, "add_connection", _boom)
    revoked = []
    with pytest.raises(RuntimeError):
        connect.save_teller_enrollment(conn, _enrollment(), revoked.append)
    assert keychain.get_password("fiscl", base.secret_name("teller", ref)) == "tok_WORKING" and revoked == []


def test_sec7_a_revoke_failure_does_not_mask_the_original_error(conn, keychain, monkeypatch):
    monkeypatch.setattr(connections, "add_connection", _boom)

    def bad_revoke(token):
        raise SyncError("offline")

    with pytest.raises(RuntimeError, match="disk full"):
        connect.save_teller_enrollment(conn, _enrollment(), bad_revoke)
    assert keychain.items == {}


def test_sec7_failed_plaid_save_removes_the_item_at_plaid(conn, keychain, monkeypatch):
    api = FakePlaid()
    client = plaid.PlaidClient("sandbox", CLIENT_ID, SECRET, api)
    monkeypatch.setattr(connections, "add_connection", _boom)
    with pytest.raises(RuntimeError):
        connect.save_plaid_item(conn, client, {"public_token": PUBLIC, "institution": {"name": "X Bank"}})
    assert api.paths("/item/remove") == [{"access_token": ACCESS}] and keychain.items == {}


def test_sec7_failed_plaid_save_restores_a_working_token(conn, keychain, monkeypatch):
    ref = connections.item_ref_hash("plaid", "item_1")
    keychain.set_password("fiscl", base.secret_name("plaid", ref), "access-old-working-token")
    api = FakePlaid()
    client = plaid.PlaidClient("sandbox", CLIENT_ID, SECRET, api)
    monkeypatch.setattr(connections, "add_connection", _boom)
    with pytest.raises(RuntimeError):
        connect.save_plaid_item(conn, client, {"public_token": PUBLIC})
    assert keychain.get_password("fiscl", base.secret_name("plaid", ref)) == "access-old-working-token"
    assert api.paths("/item/remove") == []


def test_sec7_teller_revoke_uses_the_allowlisted_delete(cfg):
    cfg.teller_environment = "sandbox"
    seen = []

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        seen.append((req.get_method(), urllib.parse.urlsplit(req.full_url).path))
        return Resp(b"")

    teller.TellerConnector(cfg, [], opener=opener).revoke_token("tok_x")
    assert seen == [("DELETE", "/accounts")]


# ---------------------------------------------------------------------------------------------------
# SEC-8: enum-like audit keys are logged only when valid


def _audit_lines(cfg):
    return [json.loads(line) for line in (cfg.logs / "audit.log").read_text().splitlines()]


def test_sec8_free_text_in_enum_slots_never_reaches_the_audit_log(cfg):
    secret = "WALMART #4417 $52.10"
    guard.audit(cfg, "sync_accounts", {"provider": secret, "kind": secret, "status": secret, "days": 3}, 1, 0.0)
    log_text = (cfg.logs / "audit.log").read_text()
    assert "WALMART" not in log_text and "4417" not in log_text
    (entry,) = _audit_lines(cfg)
    assert entry["args"] == {"provider": "<invalid>", "kind": "<invalid>", "status": "<invalid>", "days": 3}


def test_sec8_valid_enum_values_are_still_logged(cfg):
    guard.audit(cfg, "sync_accounts", {"provider": "teller", "kind": "subscription", "status": "active"}, 1, 0.0)
    (entry,) = _audit_lines(cfg)
    assert entry["args"] == {"provider": "teller", "kind": "subscription", "status": "active"}


def test_sec8_non_string_values_are_invalid_and_provider_set_matches_the_connectors():
    assert guard._AUDIT_ENUMS["provider"] == {"all", *base.PROVIDERS}
    assert guard._audit_value("provider", ["teller"]) == "<invalid>"


# ---------------------------------------------------------------------------------------------------
# SEC-9: read-only is pinned in code and checked structurally


def test_sec9_route_allowlists_are_pinned_exactly():
    assert teller.ROUTES == (
        ("GET", "/accounts"),
        ("GET", r"/accounts/[A-Za-z0-9_\-]+/transactions"),
        ("GET", r"/accounts/[A-Za-z0-9_\-]+/balances"),
        ("DELETE", r"/accounts/[A-Za-z0-9_\-]+"),
        ("DELETE", "/accounts"),
    )
    assert plaid.ROUTES == (
        ("POST", "/link/token/create"),
        ("POST", "/item/public_token/exchange"),
        ("POST", "/transactions/sync"),
        ("POST", "/accounts/get"),
        ("POST", "/item/remove"),
    )
    assert plaid.SANDBOX_ROUTES == (("POST", "/sandbox/public_token/create"),)
    assert simplefin._policy(("h.example",)).routes == (
        ("GET", r"(?:/[\w.\-~%=]+)*/accounts"),
        ("POST", r"(?:/[\w.\-~%=]+)*/claim/[\w.\-~%=]+"),
    )


def _src_trees():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


FORBIDDEN_FRAGMENTS = (
    "/transfer",
    "/payment",
    "/processor",
    "/bank_transfer",
    "/details",
    "/auth/get",
    "/identity",
    "/verify",
    "/payees",
    "/signal/",
    "/income/",
    "/investments/",
    "/accounts/balance/get",
)


def _literal_text(node) -> str | None:
    """The text of a string literal, or of `"a" + "b"` built only from literals (None for anything computed)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_text(node.left), _literal_text(node.right)
        return left + right if left is not None and right is not None else None
    return None


def _forbidden_hits(rel: str, tree: ast.AST) -> list[tuple[str, str]]:
    """Forbidden fragments in string literals, including ones split across `+`.

    Not caught (document, don't pretend): paths assembled at runtime with join/format/f-strings or from data.
    The only exemptions are exact URLs in conftest.ALLOWED_REFERENCE_URLS for the one file listed there.
    """
    skip = _docstring_nodes(tree)
    allowed = ALLOWED_REFERENCE_URLS.get(rel, ())
    hits = []
    for node in ast.walk(tree):
        if id(node) in skip or not isinstance(node, ast.Constant | ast.BinOp):
            continue
        text = _literal_text(node)
        if text is None or text in allowed:
            continue
        hits += [(rel, f) for f in FORBIDDEN_FRAGMENTS if f in text]
    return hits


def test_sec9_no_string_constant_in_any_module_names_a_money_movement_or_identity_endpoint():
    hits = []
    for rel, tree in _src_trees():
        hits += _forbidden_hits(rel, tree)
    assert hits == []


def test_sec9_scan_catches_a_forbidden_path_assembled_from_literals():
    split = ast.parse('URL = "https://x.example/inc" + "ome/get"')
    assert _forbidden_hits("sync/plaid.py", split) == [("sync/plaid.py", "/income/")]
    three = ast.parse('URL = "/pay" + "ment" + "s/send"')
    assert ("a.py", "/payment") in _forbidden_hits("a.py", three)
    assert _forbidden_hits("a.py", ast.parse('URL = "/ok" + "/path"')) == []


def test_sec9_reference_url_exemption_is_exact_and_single_file():
    url = next(iter(ALLOWED_REFERENCE_URLS["tax/states.py"]))
    tree = ast.parse(f'U = "{url}"')
    assert _forbidden_hits("tax/states.py", tree) == []
    assert _forbidden_hits("sync/plaid.py", tree) == [("sync/plaid.py", "/income/")]
    assert _forbidden_hits("tax/states.py", ast.parse(f'U = "{url}x"')) == [("tax/states.py", "/income/")]
    assert _forbidden_hits("tax/states.py", ast.parse(f'U = "{url}" + "/income/"')) != []


def test_sec9_policy_is_only_constructed_by_the_connector_modules():
    allowed = {"sync/teller.py", "sync/plaid.py", "sync/simplefin.py"}
    built = set()
    for rel, tree in _src_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Name) and f.id == "Policy") or (
                    isinstance(f, ast.Attribute) and f.attr == "Policy"
                ):
                    built.add(rel)
    assert built == allowed


NETWORK_MODULES = {
    "urllib.request",
    "http.client",
    "http.server",
    "socket",
    "socketserver",
    "ssl",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc.client",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "websockets",
}
# connect.py's loopback-only one-shot server (127.0.0.1, random port, token URL) is the single allowed exception.
ALLOWED_NETWORK_IMPORTS = {"sync/base.py": {"urllib.request", "ssl"}, "sync/connect.py": {"http.server"}}


def _imported_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module
            for alias in node.names:
                yield f"{node.module}.{alias.name}"


def test_sec9_only_base_opens_network_connections_and_connect_serves_loopback():
    found: dict[str, set[str]] = {}
    for rel, tree in _src_trees():
        mods = {m for m in _imported_modules(tree) if m in NETWORK_MODULES}
        if mods:
            found[rel] = mods
    assert found == ALLOWED_NETWORK_IMPORTS


def test_sec9_no_dynamic_imports_in_src():
    for rel, tree in _src_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                assert name not in ("__import__", "import_module"), rel


def test_sec9_every_base_request_call_passes_a_module_level_policy():
    calls = 0
    for rel, tree in _src_trees():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "request"
            ):
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "base"):
                continue
            calls += 1
            first = node.args[0]
            ok = (
                (isinstance(first, ast.Name) and first.id == "POLICY")  # teller's module-level policy
                or (isinstance(first, ast.Attribute) and first.attr == "policy")  # plaid's per-environment policy
                or (isinstance(first, ast.Call) and getattr(first.func, "id", "") in ("_policy", "policy_for"))
            )
            assert ok, f"{rel}:{node.lineno} passes something other than a connector policy"
    assert calls == 4  # teller, plaid, simplefin claim + fetch: a new call site must be reviewed here


# ---------------------------------------------------------------------------------------------------
# SEC-10 / SEC-11


def test_sec10_memfd_is_close_on_exec(monkeypatch):
    if not hasattr(os_mod := __import__("os"), "memfd_create"):
        pytest.skip("memfd_create is Linux only")
    flags = []
    real = os_mod.memfd_create
    monkeypatch.setattr(os_mod, "memfd_create", lambda name, f=0: flags.append(f) or real(name, f))
    with pytest.raises(SyncError):
        base.client_context("not a cert", "not a key")
    assert flags == [os_mod.MFD_CLOEXEC]


def test_sec11_pages_warn_loudly_on_a_409():
    for html in (connect.teller_page("app_1", "sandbox", "n"), connect.plaid_page("link-xxxxxxxxxx", "n")):
        assert "x.status === 409" in html and "WARNING" in html and "fiscl connect forget" in html


def test_sec11_the_saved_institution_is_announced_for_the_human_to_confirm(cfg, conn, keychain):
    plaid.store_app_credentials(CLIENT_ID, SECRET)
    api, result, threads, said = FakePlaid(), {}, [], []
    good = {"public_token": PUBLIC, "institution": {"name": "First Platypus Bank"}}
    connect.connect_plaid(
        cfg, conn, open_browser=_browser([good], result, threads), timeout=10, announce=said.append, opener=api
    )
    threads[0].join(5)
    assert any("First Platypus Bank" in line for line in said)


# ---------------------------------------------------------------------------------------------------
# SEC-12 / SEC-13


def test_sec12_simplefin_bridge_errors_become_fixed_prose():
    from zoneinfo import ZoneInfo

    payload = {
        "errors": ["Auth failed for WALMART card 4417 $52.10", "x"],
        "accounts": [{"id": "a", "currency": "http://evil"}],
    }
    _, warnings = simplefin.parse_accounts(payload, ZoneInfo("UTC"))
    assert warnings == [
        "SimpleFIN reported 2 connection error(s); check your bridge",
        "skipped an account with an unsupported currency",
    ]
    assert "WALMART" not in json.dumps(warnings) and "evil" not in json.dumps(warnings)


def test_sec12_non_list_errors_are_ignored():
    from zoneinfo import ZoneInfo

    assert simplefin.parse_accounts({"errors": "boom", "accounts": []}, ZoneInfo("UTC"))[1] == []


def test_sec13_schedule_c_counts_a_split_transaction_once(cfg, conn, accounts):
    from fiscl import tax

    from .conftest import add_txn

    tid = add_txn(conn, accounts["biz"], "2026-03-02", -10000, "Office depot", "Business: Office", scope="business")
    cat = conn.execute("SELECT id FROM categories WHERE name = 'Business: Office'").fetchone()[0]
    for amt in (-6000, -4000):  # both parts in the same category: COUNT(*) would say 2
        conn.execute("INSERT INTO splits (transaction_id, category_id, amount) VALUES (?, ?, ?)", (tid, cat, amt))
    conn.commit()
    out = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    counts = [c["count"] for e in out["expenses_by_line"] for c in e["categories"]]
    counts += [u["count"] for u in out["unmapped_business_spending"]]
    assert counts == [1]
