"""MCP contract and guardrail tests through a real client."""

import json
import sys

import pytest
from mcp import Client, StdioServerParameters

from fiscl.demo import load_demo
from fiscl.server import build_server

from .conftest import TODAY, add_txn


@pytest.fixture
def demo(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    return cfg


async def test_tools_listed_with_annotations(demo):
    async with Client(build_server(demo)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    expected = {
        "query_transactions",
        "spending_summary",
        "cash_flow",
        "detect_recurring",
        "budget_status",
        "forecast_cash",
        "apply_categories",
        "show_chart",
        "schedule_c_draft",
        "review_writeoffs",
        "estimated_tax",
        "set_scope",
        "log_trip",
        "sync_accounts",
        "add_note",
        "tag_transactions",
        "split_transaction",
        "dismiss_recurring",
        "set_budget",
        "manage_asset",
        "net_worth",
        "afford",
        "debt_payoff",
        "manage_goal",
        "goal_progress",
        "find_anomalies",
        "subscription_watchdog",
        "refund_tracker",
        "merchant_history",
        "category_trend",
        "record_1099",
        "reconcile_1099",
    }
    assert expected <= set(tools)
    assert tools["spending_summary"].annotations.read_only_hint is True
    assert tools["apply_categories"].annotations.read_only_hint is False
    for name in (
        "net_worth",
        "afford",
        "debt_payoff",
        "goal_progress",
        "find_anomalies",
        "subscription_watchdog",
        "refund_tracker",
        "merchant_history",
        "category_trend",
        "reconcile_1099",
    ):
        assert tools[name].annotations.read_only_hint is True, name
    destructive_tools = (
        "apply_categories",
        "add_note",
        "tag_transactions",
        "split_transaction",
        "dismiss_recurring",
        "set_budget",
        "manage_asset",
        "manage_goal",
        "set_scope",
        "record_1099",
    )
    for name in destructive_tools:
        a = tools[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is True and a.open_world_hint is False, name
        assert a.idempotent_hint is False, name
        assert "dry_run" in tools[name].description, name
    # log_trip only appends a row, so it stays a plain (non-destructive) local write.
    a = tools["log_trip"].annotations
    assert a.read_only_hint is False and a.destructive_hint is False and a.open_world_hint is False
    assert tools["sync_accounts"].annotations.open_world_hint is True
    assert tools["sync_accounts"].annotations.destructive_hint is True
    assert tools["show_chart"].meta["ui"]["resourceUri"] == "ui://fiscl/chart.html"


async def test_every_read_tool_returns_meta_and_display_amounts(demo):
    async with Client(build_server(demo)) as c:
        for name, args in [
            ("spending_summary", {}),
            ("cash_flow", {}),
            ("detect_recurring", {}),
            ("list_accounts", {}),
            ("schedule_c_draft", {"year": 2026}),
            ("net_worth", {}),
            ("afford", {"amount": 2000}),
            ("debt_payoff", {"debts": [{"name": "Card", "balance": 1000, "apr": 12, "min_payment": 100}]}),
            ("goal_progress", {}),
            ("find_anomalies", {}),
            ("find_anomalies", {"sensitivity": "high", "limit": 5}),
            ("subscription_watchdog", {}),
            ("refund_tracker", {"days": 30}),
            ("merchant_history", {"merchant": "Kroger Gulfport"}),
            ("category_trend", {"category": "Groceries", "months": 6}),
        ]:
            r = await c.call_tool(name, args)
            assert not r.is_error, (name, r.content)
            assert "data_freshness" in r.structured_content["meta"]


async def test_bad_input_is_a_tool_error_not_a_crash(demo):
    async with Client(build_server(demo)) as c:
        r = await c.call_tool("spending_summary", {"start_date": "not-a-date"})
        assert r.is_error and "ISO date" in r.content[0].text
        r = await c.call_tool("show_chart", {"kind": "pie"})
        assert r.is_error


async def test_write_tools_default_to_dry_run(demo, conn):
    before = conn.execute("SELECT COUNT(*) FROM transactions WHERE category_source = 'manual'").fetchone()[0]
    async with Client(build_server(demo)) as c:
        r = await c.call_tool("apply_categories", {"items": [{"merchant": "Kroger Gulfport", "category": "Groceries"}]})
        assert r.structured_content["dry_run"] is True
        assert r.structured_content["transactions_affected"] > 0
    after = conn.execute("SELECT COUNT(*) FROM transactions WHERE category_source = 'manual'").fetchone()[0]
    assert before == after


async def test_new_write_tools_default_to_dry_run_and_change_nothing(demo, conn):
    def snapshot():
        return (
            conn.execute("SELECT COUNT(note) FROM transactions").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM recurring").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM recurring_dismissed").fetchone()[0],
            demo.budgets_file.read_text(),
        )

    parent = conn.execute(
        "SELECT id, amount, merchant FROM transactions WHERE amount < -1000 AND transfer_group IS NULL LIMIT 1"
    ).fetchone()
    rec = conn.execute("SELECT merchant FROM recurring LIMIT 1").fetchone()[0]
    half = parent["amount"] // 2
    calls = {
        "add_note": {"transaction_id": parent["id"], "note": "hello"},
        "tag_transactions": {"items": [{"merchant": parent["merchant"], "add": ["x"]}]},
        "split_transaction": {
            "transaction_id": parent["id"],
            "parts": [
                {"amount": half / 100, "category": "Groceries"},
                {"amount": (parent["amount"] - half) / 100, "category": "Dining"},
            ],
        },
        "dismiss_recurring": {"merchant": rec},
        "set_budget": {"items": [{"category": "Dining", "monthly": 123}]},
    }
    before = snapshot()
    async with Client(build_server(demo)) as c:
        for name, args in calls.items():
            r = await c.call_tool(name, args)
            assert not r.is_error, (name, r.content)
            assert r.structured_content["dry_run"] is True, name
            assert r.structured_content["changes"], name
            assert "data_freshness" in r.structured_content["meta"], name
        assert snapshot() == before
        # Bad input is a tool error, not a crash.
        bad = [
            ("split_transaction", {"transaction_id": parent["id"], "parts": [{"amount": 1, "category": "A"}] * 2}),
            ("tag_transactions", {"items": [{"merchant": parent["merchant"], "add": ["x" * 50]}]}),
            ("set_budget", {"items": [{"category": "Dining", "monthly": -1}]}),
            ("add_note", {"transaction_id": 10**9, "note": "x"}),
        ]
        for name, args in bad:
            r = await c.call_tool(name, args)
            assert r.is_error, name
        # dry_run=false applies through the same path.
        r = await c.call_tool("set_budget", {"items": [{"category": "Dining", "monthly": 123}], "dry_run": False})
        assert r.structured_content["dry_run"] is False
        assert "123.00" in demo.budgets_file.read_text()


async def test_planning_write_tools_default_to_dry_run_and_change_nothing(demo, conn):
    def snapshot():
        return (
            conn.execute("SELECT COUNT(*) FROM manual_assets").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0],
        )

    base = snapshot()  # the demo seeds one asset and one goal
    calls = {
        "manage_asset": {"action": "add", "name": "Truck", "kind": "vehicle", "value": 18000},
        "manage_goal": {"action": "add", "name": "Trip", "target": 3000, "saved": 100},
    }
    async with Client(build_server(demo)) as c:
        for name, args in calls.items():
            r = await c.call_tool(name, args)
            assert not r.is_error, (name, r.content)
            assert r.structured_content["dry_run"] is True and r.structured_content["changes"], name
            assert "data_freshness" in r.structured_content["meta"], name
        assert snapshot() == base
        for name, args in [
            ("manage_asset", {"action": "add", "name": "X", "kind": "boat", "value": 1}),
            ("manage_goal", {"action": "add", "name": "X", "target": -1}),
            ("afford", {"amount": -5}),
            ("debt_payoff", {"debts": [{"name": "Card", "balance": 1000}]}),
            ("goal_progress", {"name": "missing"}),
            ("find_anomalies", {"sensitivity": "paranoid"}),
            ("refund_tracker", {"days": 0}),
            ("merchant_history", {"merchant": "Nobody Inc"}),
            ("category_trend", {"category": "Nope"}),
        ]:
            assert (await c.call_tool(name, args)).is_error, name
        for name, args in calls.items():
            r = await c.call_tool(name, {**args, "dry_run": False})
            assert r.structured_content["dry_run"] is False
        assert snapshot() == (base[0] + 1, base[1] + 1)
        nw = (await c.call_tool("net_worth", {})).structured_content
        assert any(a["name"] == "Truck" for a in nw["assets"])
        af = (await c.call_tool("afford", {"amount": 2000})).structured_content
        assert af["affordable"] == (af["with_purchase"]["lowest_balance"]["amount"]["minor"] >= af["buffer"]["minor"])


async def test_output_is_redacted_and_audit_log_has_no_merchants(cfg, conn, accounts):
    add_txn(conn, accounts["checking"], "2026-10-01", -1000, "PAYMENT REF 4111111111111111")
    conn.commit()
    async with Client(build_server(cfg)) as c:
        r = await c.call_tool(
            "query_transactions", {"text": "PAYMENT", "start_date": "2026-10-01", "end_date": "2026-10-12"}
        )
    blob = json.dumps(r.structured_content, ensure_ascii=False)
    assert "4111111111111111" not in blob and "••••1111" in blob
    log = (cfg.logs / "audit.log").read_text()
    assert "PAYMENT" not in log and '"text": "<set>"' in log


async def test_chart_tool_returns_renderable_spec(demo):
    async with Client(build_server(demo)) as c:
        r = await c.call_tool("show_chart", {"kind": "spending_by_category"})
        chart = r.structured_content["chart"]
        assert chart["type"] == "hbar" and len(chart["labels"]) == len(chart["series"][0]["values"])
        res = await c.read_resource("ui://fiscl/chart.html")
        assert "ui/initialize" in res.contents[0].text


async def test_new_chart_kinds_and_category_arg(demo):
    async with Client(build_server(demo)) as c:
        nw = (await c.call_tool("show_chart", {"kind": "net_worth"})).structured_content["chart"]
        assert nw["labels"] == ["Assets", "Liabilities", "Net worth"]
        ct = await c.call_tool("show_chart", {"kind": "category_trend", "category": "Groceries"})
        chart = ct.structured_content["chart"]
        assert chart["reference"]["name"] == "3-month median" and len(chart["labels"]) == 12
        assert (await c.call_tool("show_chart", {"kind": "category_trend"})).is_error  # category is required
        assert (await c.call_tool("show_chart", {"kind": "category_trend", "category": "Nope"})).is_error


async def test_goal_and_recurring_resources_and_prompt(demo):
    async with Client(build_server(demo)) as c:
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        assert {"fiscl://goals", "fiscl://recurring"} <= uris
        goals = json.loads((await c.read_resource("fiscl://goals")).contents[0].text)
        assert goals["goals"][0]["name"] == "Emergency fund"
        recurring = json.loads((await c.read_resource("fiscl://recurring")).contents[0].text)
        assert "meta" in recurring
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert "goal_check_in" in prompts
        text = (await c.get_prompt("goal_check_in")).messages[0].content.text
        assert "goal_progress" in text and "net_worth" in text and "afford" in text and "not advice" in text


async def test_stdio_server_keeps_stdout_clean(demo):
    """Anything printed to stdout would corrupt the protocol; a real stdio session must work."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "fiscl", "--home", str(demo.home), "serve", "--no-watch"]
    )
    async with Client(params) as c:
        tools = await c.list_tools()
        assert any(t.name == "forecast_cash" for t in tools.tools)
        r = await c.call_tool("list_accounts", {})
        assert not r.is_error


async def test_export_schedule_c_annotations_and_dry_run_default(demo):
    async with Client(build_server(demo)) as c:
        tool = {t.name: t for t in (await c.list_tools()).tools}["export_schedule_c"]
        a = tool.annotations
        assert a.read_only_hint is False and a.destructive_hint is False and a.open_world_hint is False
        assert a.idempotent_hint is False
        props = {"year", "formats", "include_transactions", "dry_run", "business"}
        assert set(tool.input_schema["properties"]) == props
        r = await c.call_tool("export_schedule_c", {"year": 2026})
        assert not r.is_error
        assert r.structured_content["dry_run"] is True
        assert [f["kind"] for f in r.structured_content["files"]] == ["summary_csv", "markdown"]
        assert list(demo.exports.iterdir()) == []
        for bad in ({"year": 1999}, {"formats": ["pdf"]}, {"year": "../../x"}):
            r = await c.call_tool("export_schedule_c", bad)
            assert r.is_error
        assert list(demo.exports.iterdir()) == []


async def test_export_schedule_c_applies_and_returns_relative_paths(demo):
    async with Client(build_server(demo)) as c:
        r = await c.call_tool("export_schedule_c", {"year": 2026, "dry_run": False})
        assert not r.is_error and r.structured_content["written"] is True
        for f in r.structured_content["files"]:
            assert f["path"].startswith("exports/") and not f["path"].startswith("/")
            assert (demo.home / f["path"]).is_file()
        assert str(demo.home) not in json.dumps(r.structured_content)
