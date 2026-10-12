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
    }
    assert expected <= set(tools)
    assert tools["spending_summary"].annotations.read_only_hint is True
    assert tools["apply_categories"].annotations.read_only_hint is False
    assert tools["show_chart"].meta["ui"]["resourceUri"] == "ui://fiscl/chart.html"


async def test_every_read_tool_returns_meta_and_display_amounts(demo):
    async with Client(build_server(demo)) as c:
        for name, args in [
            ("spending_summary", {}),
            ("cash_flow", {}),
            ("detect_recurring", {}),
            ("list_accounts", {}),
            ("schedule_c_draft", {"year": 2026}),
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
