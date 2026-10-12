import base64
import io
import json
from zoneinfo import ZoneInfo

import pytest

from fiscl.sync import simplefin

PAYLOAD = {
    "errors": [],
    "accounts": [
        {
            "org": {"name": "Gulf Bank", "domain": "gulfbank.example"},
            "id": "ACT-1",
            "name": "Everyday Checking",
            "currency": "USD",
            "balance": "1520.11",
            "balance-date": 1791763200,
            "transactions": [
                {
                    "id": "T1",
                    "posted": 1791676800,
                    "amount": "-42.10",
                    "description": "SHELL OIL 123",
                    "pending": False,
                },
                {
                    "id": "T2",
                    "posted": 0,
                    "transacted_at": 1791720000,
                    "amount": "-9.99",
                    "description": "SPOTIFY",
                    "pending": True,
                },
            ],
        },
        {
            "id": "ACT-2",
            "name": "Rewards Visa",
            "currency": "USD",
            "balance": "-300.00",
            "balance-date": 1791763200,
            "transactions": [],
        },
    ],
}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_parse_accounts_maps_types_pending_and_balances():
    stmts, warnings = simplefin.parse_accounts(PAYLOAD, ZoneInfo("America/Chicago"))
    assert warnings == []
    chk, card = stmts
    assert chk.type_hint == "checking" and card.type_hint == "credit"
    assert chk.account_name == "Gulf Bank Everyday Checking"
    assert [t.amount for t in chk.txns] == [-4210, -999]
    assert chk.txns[1].pending is True and chk.balances[0][1] == 152011


def test_host_allowlist_blocks_other_hosts():
    with pytest.raises(simplefin.SyncError):
        simplefin.fetch("https://u:p@evil.example/simplefin", None, None, simplefin.DEFAULT_ALLOWED_HOSTS)
    with pytest.raises(simplefin.SyncError):
        simplefin.fetch("http://u:p@bridge.simplefin.org/simplefin", None, None, simplefin.DEFAULT_ALLOWED_HOSTS)


def test_claim_rejects_garbage_token():
    with pytest.raises(simplefin.SyncError):
        simplefin.claim("not base64!!")
    bad_host = base64.b64encode(b"https://evil.example/claim/abc").decode()
    with pytest.raises(simplefin.SyncError):
        simplefin.claim(bad_host)


def test_sync_imports_through_pipeline_with_basic_auth(conn, cfg, monkeypatch):
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return FakeResp(json.dumps(PAYLOAD).encode())

    url = "https://user:secret@beta-bridge.simplefin.org/simplefin"
    result = simplefin.sync(conn, cfg, days=30, access_url=url, opener=opener)
    assert "secret" not in seen["url"] and seen["auth"] == "Basic " + base64.b64encode(b"user:secret").decode()
    assert result["rows_added"] == 2 and len(result["accounts"]) == 2
    # Running again adds nothing new.
    assert simplefin.sync(conn, cfg, days=30, access_url=url, opener=opener)["rows_added"] == 0
    types = {r["name"]: r["type"] for r in conn.execute("SELECT name, type FROM accounts")}
    assert types["Rewards Visa"] == "credit"
