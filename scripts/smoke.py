"""Dev-only smoke test: every MCP tool, resource, and prompt over a real stdio session against demo data.

Not part of the package. Run with `uv run python scripts/smoke.py`; exits non-zero if a call that should work
fails, or a call that should be rejected is not. Writes are only called as dry runs (the default), and the
script checks nothing changed. This process opens no sockets (it blocks them to prove it); the only channel
is the stdio pipe to the server subprocess.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from mcp import Client, StdioServerParameters


# Prove the harness itself makes no network calls. (AF_UNIX socketpairs for the event loop are unaffected.)
def _blocked(*_a, **_k):
    raise RuntimeError("smoke test must not use the network")


socket.socket.connect = _blocked  # type: ignore[method-assign]
socket.getaddrinfo = _blocked  # type: ignore[assignment]

OK, ERROR, DRY = "ok", "error", "dry_run"
START = (date.today() - timedelta(days=400)).isoformat()  # tools cap the range at 10 years; demo is 6 months


def dollars(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100}.{abs(minor) % 100:02d}"


class Smoke:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0
        self.called: set[str] = set()

    def check(self, label: str, condition: bool, detail: object = "") -> None:
        if condition:
            self.passed += 1
        else:
            self.failures.append(f"{label}: {str(detail)[:300]}")

    async def call(self, c: Client, tool: str, args: dict, expect: str = OK):
        self.called.add(tool)
        label = f"{tool}({', '.join(sorted(args))}) expect {expect}"
        try:
            r = await c.call_tool(tool, args)
        except Exception as exc:  # a protocol-level failure is a failure either way
            self.check(label, expect == ERROR, f"raised {type(exc).__name__}: {exc}")
            return None
        if expect == ERROR:
            self.check(label, r.is_error, "expected a tool error but the call succeeded")
            return None
        if r.is_error:
            self.check(label, False, r.content)
            return None
        data = r.structured_content or {}
        self.check(label + " has meta", "meta" in data, list(data))
        if expect == DRY:
            self.check(label + " is a dry run", data.get("dry_run") is True, data.get("dry_run"))
        return data


def counts(db: Path) -> tuple:
    with sqlite3.connect(db) as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "transactions",
                "tags",
                "splits",
                "manual_assets",
                "goals",
                "trips",
                "categories",
                "forms_1099",
                "businesses",
            )
        )


async def run(home: Path) -> Smoke:
    s = Smoke()
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "fiscl", "--home", str(home), "serve", "--no-watch"]
    )
    before = counts(home / "fiscl.db")
    async with Client(params) as c:
        tools = {t.name for t in (await c.list_tools()).tools}

        # A real transaction to point the id-based tools at.
        found = await s.call(c, "query_transactions", {"text": "Kroger", "start_date": START, "limit": 5})
        txn = found["transactions"][0]
        tid, minor = txn["id"], txn["amount"]["minor"]
        half = minor // 2
        parts = [
            {"amount": dollars(half), "category": "Groceries"},
            {"amount": dollars(minor - half), "category": "Shopping"},
        ]
        debts = [{"name": "Card", "balance": 1500, "apr": 21.9, "min_payment": 45}]

        reads: list[tuple[str, dict]] = [
            ("query_transactions", {"start_date": START, "limit": 3, "tag": "refund-pending"}),
            ("spending_summary", {}),
            ("spending_summary", {"group_by": "merchant", "scope": "business"}),
            ("cash_flow", {}),
            ("detect_recurring", {}),
            ("budget_status", {}),
            ("suggest_budgets", {}),
            ("forecast_cash", {"days": 30}),
            ("net_worth", {}),
            # no holdings in the demo: empty states, never a network call
            ("holdings", {}),
            ("holdings", {"group_by": "asset_class", "limit": 5}),
            # no rates in the demo: empty state, never a rate lookup
            ("list_fx_rates", {}),
            ("list_fx_rates", {"currency": "EUR", "start_date": START, "limit": 5}),
            # no Amazon history or QuickBooks in the demo: empty states, never a network call
            ("amazon_matches", {}),
            ("amazon_matches", {"start_date": START, "include_ambiguous": False, "limit": 5}),
            ("receivables", {}),
            ("receivables", {"customer": "Acme", "limit": 5}),
            ("books_vs_bank", {}),
            ("books_vs_bank", {"year": date.today().year - 1}),
            ("afford", {"amount": 500}),
            ("afford", {"amount": 25, "recurring_monthly": True, "buffer": 100}),
            ("debt_payoff", {"extra_monthly": 50, "debts": debts}),
            ("goal_progress", {}),
            ("goal_progress", {"name": "Emergency fund"}),
            ("find_anomalies", {"start_date": START, "sensitivity": "high"}),
            ("find_anomalies", {}),
            ("subscription_watchdog", {}),
            ("refund_tracker", {}),
            ("merchant_history", {"merchant": txn["merchant"]}),
            ("category_trend", {"category": "Groceries"}),
            ("list_accounts", {}),
            ("list_connections", {}),
            # nothing is connected in a fresh demo home: skipped with a note, never a network call
            ("sync_accounts", {}),
            ("import_status", {}),
            ("list_uncategorized", {}),
            ("schedule_c_draft", {}),
            ("estimated_tax", {"income_tax_rate_pct": 22}),
            ("estimated_tax", {"income_tax_rate_pct": 22, "state": "CA", "state_income_tax_rate_pct": 6}),
            ("estimated_tax", {"state": "TX"}),
            ("reconcile_1099", {}),
            ("reconcile_1099", {"business": "My business"}),
            ("list_businesses", {}),
            ("schedule_c_draft", {"business": "My business"}),
            ("review_writeoffs", {}),
            ("mileage_log", {}),
        ]
        for kind in ("spending_by_category", "spending_by_month", "cash_flow", "forecast", "budget", "net_worth"):
            reads.append(("show_chart", {"kind": kind}))
        reads.append(("show_chart", {"kind": "category_trend", "category": "Groceries"}))
        for tool, args in reads:
            await s.call(c, tool, args)

        writes: list[tuple[str, dict]] = [
            ("apply_categories", {"items": [{"merchant": "Kroger", "category": "Groceries"}], "create_rule": True}),
            ("add_note", {"transaction_id": tid, "note": "smoke test"}),
            ("tag_transactions", {"items": [{"transaction_ids": [tid], "add": ["smoke"]}]}),
            ("split_transaction", {"transaction_id": tid, "parts": parts}),
            ("dismiss_recurring", {"merchant": "Netflix.com"}),
            ("set_budget", {"items": [{"category": "Groceries", "monthly": 500}]}),
            ("manage_asset", {"action": "add", "name": "Smoke boat", "kind": "other", "value": 1000}),
            ("manage_goal", {"action": "add", "name": "Smoke goal", "target": 500}),
            ("set_fx_rate", {"items": [{"date": "2026-01-02", "currency": "EUR", "rate": "1.0850"}]}),
            ("set_scope", {"items": [{"transaction_ids": [tid], "scope": "business"}]}),
            ("log_trip", {"trip_date": "2026-01-02", "miles": 10, "purpose": "Smoke test client visit"}),
            ("export_schedule_c", {"include_transactions": True}),
            ("manage_business", {"action": "add", "name": "Smoke Consulting"}),
            ("set_scope", {"items": [{"transaction_ids": [tid], "scope": "business", "business": "My business"}]}),
            (
                "record_1099",
                {"items": [{"action": "add", "tax_year": 2026, "form": "nec", "payer": "Smoke Payer", "amount": 1000}]},
            ),
        ]
        for tool, args in writes:
            await s.call(c, tool, args, DRY)

        # Inputs the server must reject with a tool error rather than crash or accept.
        bad: list[tuple[str, dict]] = [
            ("spending_summary", {"group_by": "zodiac"}),
            ("query_transactions", {"start_date": "not-a-date"}),
            ("manage_asset", {"action": "add", "name": "X", "kind": "boat", "value": 1}),
            ("manage_goal", {"action": "add", "name": "X", "target": -1}),
            ("afford", {"amount": -5}),
            ("debt_payoff", {"debts": [{"name": "Card", "balance": 1000}]}),
            ("goal_progress", {"name": "no such goal"}),
            ("find_anomalies", {"sensitivity": "paranoid"}),
            ("refund_tracker", {"days": 0}),
            ("merchant_history", {"merchant": "Nobody Inc"}),
            ("category_trend", {"category": "No such category"}),
            ("holdings", {"group_by": "zodiac"}),
            ("holdings", {"account": "No such account"}),
            ("list_fx_rates", {"currency": "dollars"}),
            ("set_fx_rate", {"items": [{"date": "2026-01-02", "currency": "EUR", "rate": "0"}]}),
            ("set_fx_rate", {"items": [{"date": "2026-01-02", "currency": "USD", "rate": "1"}]}),
            ("set_fx_rate", {"items": [{"date": "2026-01-02", "currency": "EUR", "rate": None}]}),
            ("show_chart", {"kind": "pie"}),
            ("show_chart", {"kind": "category_trend"}),
            ("split_transaction", {"transaction_id": tid, "parts": [{"amount": "-1.00", "category": "A"}] * 2}),
            ("add_note", {"transaction_id": 10**9, "note": "x"}),
            ("apply_categories", {"items": []}),
            ("tag_transactions", {"items": [{"transaction_ids": [tid]}]}),
            ("set_budget", {"items": [{"category": "Groceries", "monthly": -5}]}),
            ("log_trip", {"trip_date": "2999-01-01", "miles": 5, "purpose": "future trip"}),
            ("estimated_tax", {"state": "ZZ"}),
            ("estimated_tax", {"state": "CA", "state_income_tax_rate_pct": 99}),
            ("record_1099", {"items": [{"action": "remove", "id": 10**6}]}),
            ("record_1099", {"items": [{"action": "add", "tax_year": 2026, "form": "w2", "payer": "X", "amount": 1}]}),
            ("reconcile_1099", {"tax_year": 1800}),
            ("schedule_c_draft", {"business": "No such business"}),
            ("manage_business", {"action": "archive", "name": "My business"}),
            ("manage_business", {"action": "add", "name": "my BUSINESS"}),
            ("set_scope", {"items": [{"transaction_ids": [tid], "scope": "personal", "business": "My business"}]}),
            # not connected in a fresh demo home; must fail cleanly, not reach the network
            ("sync_accounts", {"provider": "teller"}),
            ("sync_accounts", {"provider": "plaid"}),
            ("sync_accounts", {"provider": "qbo"}),
            ("amazon_matches", {"start_date": "yesterday"}),
            ("receivables", {"as_of": "soon"}),
            ("books_vs_bank", {"year": 1800}),
            ("sync_accounts", {"provider": "bogus"}),
        ]
        for tool, args in bad:
            await s.call(c, tool, args, ERROR)

        leftovers = list((home / "exports").iterdir())
        s.check("a dry-run export wrote no files", not leftovers, leftovers)
        # The one real write: an applied export lands only under exports/, as relative paths, and can be read back.
        exported = await s.call(c, "export_schedule_c", {"dry_run": False, "include_transactions": True})
        for f in exported["files"]:
            ok = f["path"].startswith("exports/") and (home / f["path"]).is_file()
            s.check(f"exported {f['path']} is relative and exists", ok, f)

        # Spot-check that the seeded demo cases surface.
        anomalies = await s.call(c, "find_anomalies", {"start_date": START, "sensitivity": "high"})
        types = {f["type"] for f in anomalies["findings"]}
        s.check("demo shows duplicate, spike, fee", {"duplicate", "spike", "fee"} <= types, types)
        watch = await s.call(c, "subscription_watchdog", {})
        s.check("demo shows a trial turned paid", bool(watch["trials_turned_paid"]), watch["totals"])

        resources = (await c.list_resources()).resources
        for res in resources:
            text = (await c.read_resource(str(res.uri))).contents[0].text
            if str(res.uri).startswith("fiscl://"):
                try:
                    json.loads(text)
                    s.check(f"resource {res.uri} is JSON", True)
                except ValueError as exc:
                    s.check(f"resource {res.uri} is JSON", False, exc)
            else:
                s.check(f"resource {res.uri} readable", bool(text))
        uris = {str(r.uri) for r in resources}
        s.check("v2 resources listed", {"fiscl://goals", "fiscl://recurring"} <= uris, uris)

        # Resource templates aren't in list_resources: read the tax-year summary directly, and a bad year must fail.
        templates = {str(t.uri_template) for t in (await c.list_resource_templates()).resource_templates}
        s.check("tax resource template listed", "fiscl://tax/{year}" in templates, templates)
        tax_year = json.loads((await c.read_resource(f"fiscl://tax/{date.today().year}")).contents[0].text)
        wanted = {"schedule_c", "estimated_tax", "review", "forms_1099", "exports", "disclaimer"}
        s.check("fiscl://tax/{year} has its sections", wanted <= set(tax_year), sorted(tax_year))
        for bad_year in ("abcd", "1999"):
            try:
                await c.read_resource(f"fiscl://tax/{bad_year}")
                s.check(f"fiscl://tax/{bad_year} is rejected", False, "read succeeded")
            except Exception:  # noqa: BLE001 - any protocol error is the expected outcome
                s.check(f"fiscl://tax/{bad_year} is rejected", True)

        prompt_args = {"monthly_review": {}, "cash_forecast": {"days": "45"}, "tax_checkup": {"year": "2026"}}
        prompts = (await c.list_prompts()).prompts
        for p in prompts:
            r = await c.get_prompt(p.name, prompt_args.get(p.name, {}))
            s.check(f"prompt {p.name}", bool(r.messages), r)
        s.check("goal_check_in prompt listed", "goal_check_in" in {p.name for p in prompts})

        # Anything a new tool adds must be added to this script too.
        missing = tools - s.called
        s.check("every tool was called", not missing, sorted(missing))

    after = counts(home / "fiscl.db")
    s.check("dry runs and bad inputs changed nothing", before == after, (before, after))
    return s


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fiscl-smoke-") as tmp:
        home = Path(tmp) / "home"
        demo = subprocess.run(
            [sys.executable, "-m", "fiscl", "--home", str(home), "demo"], capture_output=True, text=True, check=False
        )
        if demo.returncode != 0:
            print("demo load failed:\n" + demo.stderr, file=sys.stderr)
            return 1
        smoke = asyncio.run(run(home))
    print(f"smoke: {smoke.passed} checks passed, {len(smoke.failures)} failed, {len(smoke.called)} tools called")
    for f in smoke.failures:
        print("FAIL", f)
    return 1 if smoke.failures else 0


if __name__ == "__main__":
    sys.exit(main())
