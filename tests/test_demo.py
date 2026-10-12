"""The demo data exercises every v2 tool, and loading it twice changes nothing."""

from __future__ import annotations

import pytest

from fiscl import analysis, tax
from fiscl.charts import build_chart
from fiscl.demo import load_demo

from .conftest import TODAY


@pytest.fixture
def demo_conn(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    return conn


def _findings(conn, cfg, kind):
    r = analysis.find_anomalies(conn, cfg, from_="2026-09-12", to="2026-10-12", today=TODAY)
    return [f for f in r["findings"] if f["type"] == kind], r


def test_demo_duplicate_charge_found(demo_conn, cfg):
    dupes, _ = _findings(demo_conn, cfg, "duplicate")
    assert [(f["merchant"], f["amount"]["minor"]) for f in dupes] == [("Wholefds Mkt", 8417)]


def test_demo_bh_spike_found_and_rent_payroll_clean(demo_conn, cfg):
    spikes, r = _findings(demo_conn, cfg, "spike")
    assert any(f["merchant"] == "B&H Photo" and f["amount"]["minor"] == 124900 for f in spikes)
    flagged = {f["merchant"] for f in r["findings"] if f["merchant"]}
    assert not {m for m in flagged if "oakwood" in m.lower() or "payroll" in m.lower() or "acme" in m.lower()}


def test_demo_fee_and_new_merchant_found(demo_conn, cfg):
    fees, _ = _findings(demo_conn, cfg, "fee")
    assert any(f["amount"]["minor"] == 3400 for f in fees)
    new, _ = _findings(demo_conn, cfg, "new_merchant")
    assert any(f["merchant"] == "Luxe Estate Sales" and f["amount"]["minor"] == 38500 for f in new)


def test_demo_trial_turned_paid_is_one_merchant(demo_conn, cfg):
    w = analysis.subscription_watchdog(demo_conn, cfg, today=TODAY)
    trial = next(t for t in w["trials_turned_paid"] if t["merchant"] == "Streamly.com")
    assert trial["trial_charge"]["amount"]["minor"] == 100
    assert trial["first_paid_charge"]["amount"]["minor"] == 1599
    assert {p["merchant"] for p in w["price_changes"]} >= {"Netflix.com"}


def test_demo_refund_received_and_expected(demo_conn, cfg):
    r = analysis.refund_tracker(demo_conn, cfg, days=90, today=TODAY)
    target = next(x for x in r["received_refunds"] if x["merchant"] == "Target")
    assert target["amount"]["minor"] == 4250 and target["matched_purchase"]["amount"]["minor"] == 4250
    assert [(e["merchant"], e["amount"]["minor"]) for e in r["expected_refunds"]] == [("Nordstrom", 12000)]


def test_demo_net_worth_goal_and_split(demo_conn, cfg):
    nw = analysis.net_worth(demo_conn, cfg, today=TODAY)
    assert "Pickup truck" in {a["name"] for a in nw["assets"]}
    goals = analysis.goal_progress(demo_conn, cfg, today=TODAY)["goals"]
    assert [g["name"] for g in goals] == ["Emergency fund"] and goals[0]["tracks_account"] == "Demo Savings"
    assert goals[0]["on_track"] is False  # the prompt has something to say

    lines = demo_conn.execute(
        "SELECT amount, scope FROM txn_lines "
        "WHERE id = (SELECT id FROM transactions WHERE raw_description = 'AMZN MKTP US*7Q2XY81')"
    ).fetchall()
    assert sorted((r["amount"], r["scope"]) for r in lines if r["scope"]) == [(-4000, "business")]
    c = tax.schedule_c_draft(demo_conn, cfg, year=2026, today=TODAY)
    assert c["tentative_net_profit"] is not None


def test_demo_charts_for_new_kinds(demo_conn, cfg):
    nw = build_chart(demo_conn, cfg, "net_worth", today=TODAY)
    assert nw["chart"]["labels"] == ["Assets", "Liabilities", "Net worth"]
    ct = build_chart(demo_conn, cfg, "category_trend", category="Groceries", today=TODAY)
    assert ct["chart"]["reference"]["value"] > 0 and len(ct["chart"]["labels"]) == 12


def test_demo_is_idempotent(conn, cfg):
    def counts():
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("transactions", "tags", "splits", "manual_assets", "goals", "accounts")
        } | {
            "rules": cfg.rules_file.read_text().count("[[rule]]"),
            "budgets": cfg.budgets_file.read_text().count("[[budget]]"),
        }

    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    first = counts()
    assert first["tags"] == 1 and first["splits"] == 2 and first["manual_assets"] == 1 and first["goals"] == 1
    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    assert counts() == first
