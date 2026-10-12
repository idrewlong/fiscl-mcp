"""Phase 1 monitoring and history: anomalies, subscription watchdog, refund tracker, merchant/category history."""

from __future__ import annotations

import pytest

from fiscl import analysis
from fiscl.guard import GuardError
from fiscl.recurring import refresh_recurring

from .conftest import TODAY, add_txn

MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]


@pytest.fixture
def world(conn, cfg, accounts):
    chk = accounts["checking"]
    for ym in MONTHS:
        add_txn(conn, chk, f"{ym}-01", -150000, "RENT PAYMENT", "Housing")
    for d in ("2026-08-14", "2026-08-28", "2026-09-11", "2026-09-25", "2026-10-09"):
        add_txn(conn, chk, d, 300000, "ACME PAYROLL", "Income: Salary")
    # A one-off spike at a merchant with an irregular history.
    for d, a in (("2026-03-03", 4000), ("2026-04-20", 5000), ("2026-06-11", 6000), ("2026-08-02", 5000)):
        add_txn(conn, chk, d, -a, "B AND H PHOTO", "Shopping")
    add_txn(conn, chk, "2026-10-05", -60000, "B AND H PHOTO", "Shopping")
    # A smaller bump that only some sensitivities report.
    for d in ("2026-03-09", "2026-04-27", "2026-06-17", "2026-08-08"):
        add_txn(conn, chk, d, -5000, "GEAR HUT", "Shopping")
    add_txn(conn, chk, "2026-10-10", -13000, "GEAR HUT", "Shopping")
    # A double charge, and a fee.
    add_txn(conn, chk, "2026-10-09", -4500, "CORNER BISTRO", "Dining")
    add_txn(conn, chk, "2026-10-09", -4500, "CORNER BISTRO", "Dining")
    add_txn(conn, chk, "2026-10-03", -3500, "OVERDRAFT FEE", "Fees & Interest")
    add_txn(conn, chk, "2026-10-04", -450, "BLUE BOTTLE COFFEE", "Dining")
    # Subscriptions: a price change, a music overlap, and a trial that turned paid.
    for ym in MONTHS[1:-2]:
        add_txn(conn, chk, f"{ym}-15", -1549, "NETFLIX", "Subscriptions")
        add_txn(conn, chk, f"{ym}-12", -1099, "SPOTIFY", "Subscriptions")
        add_txn(conn, chk, f"{ym}-20", -1099, "APPLE MUSIC", "Subscriptions")
    add_txn(conn, chk, "2026-09-15", -1799, "NETFLIX", "Subscriptions")
    add_txn(conn, chk, "2026-09-12", -1099, "SPOTIFY", "Subscriptions")
    add_txn(conn, chk, "2026-09-20", -1099, "APPLE MUSIC", "Subscriptions")
    add_txn(conn, chk, "2026-08-01", -99, "STREAMLY TRIAL", "Subscriptions")
    add_txn(conn, chk, "2026-08-15", -1599, "STREAMLY TRIAL", "Subscriptions")
    add_txn(conn, chk, "2026-09-14", -1599, "STREAMLY TRIAL", "Subscriptions")
    # Refunds.
    add_txn(conn, chk, "2026-09-10", -12000, "SHOES R US", "Shopping")
    add_txn(conn, chk, "2026-10-05", 12000, "SHOES R US", "Shopping")
    waiting = add_txn(conn, chk, "2026-09-01", -8000, "GADGET SHOP", "Shopping")
    conn.execute("INSERT INTO tags (transaction_id, tag) VALUES (?, 'refund-pending')", (waiting,))
    # Category series: dining climbs, groceries are level.
    for i, ym in enumerate(MONTHS[2:8]):
        add_txn(conn, chk, f"{ym}-18", -(10000 + 5000 * i), "LOCAL GRILL", "Dining")
        add_txn(conn, chk, f"{ym}-22", -20000, "FOOD MART", "Groceries")
    refresh_recurring(conn, cfg, TODAY)
    conn.commit()
    return {"waiting": waiting}


def _by(result, kind):
    return [f for f in result["findings"] if f["type"] == kind]


def _merchants(result):
    return {f["merchant"] for f in result["findings"] if f["merchant"]}


def test_anomalies_report_duplicate_and_spike_but_not_rent_or_payroll(conn, cfg, world):
    r = analysis.find_anomalies(conn, cfg, today=TODAY)
    assert {f["merchant"] for f in _by(r, "duplicate")} == {"Corner Bistro"}
    dup = _by(r, "duplicate")[0]
    assert len(dup["transaction_ids"]) == 2 and dup["question"].endswith("?")
    spikes = _by(r, "spike")
    assert any("Photo" in s["merchant"] for s in spikes)
    assert all(f["question"] for f in r["findings"])
    assert not any(m and ("rent" in m.lower() or "payroll" in m.lower()) for m in _merchants(r))
    assert not any(f["category"] == "Housing" for f in r["findings"])
    assert [f["merchant"] for f in _by(r, "fee")] == ["Overdraft Fee"]  # "coffee" must not match
    assert "data_freshness" in r["meta"]
    assert sum(r["totals"]["by_type"].values()) == r["totals"]["findings"]


def test_findings_sorted_by_severity_then_impact(conn, cfg, world):
    r = analysis.find_anomalies(conn, cfg, today=TODAY)
    rank = {"high": 0, "medium": 1, "low": 2}
    keys = [(rank[f["severity"]], -f["dollar_impact"]["minor"]) for f in r["findings"]]
    assert keys == sorted(keys)


def test_sensitivity_scales_the_spike_threshold(conn, cfg, world):
    def spiked(level):
        r = analysis.find_anomalies(conn, cfg, sensitivity=level, today=TODAY)
        return {s["merchant"] for s in _by(r, "spike")}

    assert any("Gear" in m for m in spiked("normal")) and any("Gear" in m for m in spiked("high"))
    assert not any("Gear" in m for m in spiked("low"))
    assert any("Photo" in m for m in spiked("low"))


def test_limit_truncates_with_meta(conn, cfg, world):
    r = analysis.find_anomalies(conn, cfg, limit=1, today=TODAY)
    assert len(r["findings"]) == 1 and r["meta"]["truncated"] is True
    assert r["meta"]["total_count"] == r["totals"]["findings"] > 1


def test_anomalies_validate_input(conn, cfg, world):
    with pytest.raises(GuardError):
        analysis.find_anomalies(conn, cfg, sensitivity="paranoid", today=TODAY)
    with pytest.raises(GuardError):
        analysis.find_anomalies(conn, cfg, from_="2026-10-10", to="2026-10-01", today=TODAY)


def test_same_day_duplicates_below_twenty_dollars_are_ignored(conn, cfg, accounts):
    chk = accounts["checking"]
    add_txn(conn, chk, "2026-07-01", -500, "OLD STUFF", "Shopping")
    add_txn(conn, chk, "2026-10-09", -1500, "VENDING", "Shopping")
    add_txn(conn, chk, "2026-10-09", -1500, "VENDING", "Shopping")
    add_txn(conn, chk, "2026-10-07", -1500, "KIOSK", "Shopping")
    add_txn(conn, chk, "2026-10-09", -1500, "KIOSK", "Shopping")
    r = analysis.find_anomalies(conn, cfg, today=TODAY)
    assert {f["merchant"] for f in _by(r, "duplicate")} == {"Kiosk"}


def test_watchdog_reports_trial_price_change_and_overlap(conn, cfg, world):
    r = analysis.subscription_watchdog(conn, cfg, today=TODAY)
    trial = r["trials_turned_paid"]
    assert [t["merchant"] for t in trial] == ["Streamly Trial"]
    assert trial[0]["days_to_convert"] == 14
    changes = {p["merchant"]: p for p in r["price_changes"]}
    assert changes["Netflix"]["old_amount"]["minor"] == 1549 and changes["Netflix"]["new_amount"]["minor"] == 1799
    assert changes["Netflix"]["annualized_change"]["minor"] == 250 * 12
    music = [d for d in r["duplicate_services"] if d["group"] == "music streaming"]
    assert music and set(music[0]["merchants"]) == {"Spotify", "Apple Music"}
    assert r["totals"]["price_changes"] == len(r["price_changes"]) and "data_freshness" in r["meta"]


def test_watchdog_lists_annual_renewals_due_soon(conn, cfg, accounts):
    # Detection only looks back 18 months, so seed the recurring rows the way a long history would leave them.
    for name, nxt in (("Prime Club", "2026-10-25"), ("Far Away", "2026-12-25")):
        conn.execute(
            """INSERT INTO recurring (merchant, account_id, direction, cadence, typical_amount, last_amount,
                   last_date, next_expected, occurrences, confidence, status, kind)
               VALUES (?, ?, 'out', 'annual', 9900, 9900, '2025-10-25', ?, 3, 0.9, 'active', 'subscription')""",
            (name, accounts["checking"], nxt),
        )
    r = analysis.subscription_watchdog(conn, cfg, today=TODAY)
    assert [x["merchant"] for x in r["annual_renewals"]] == ["Prime Club"]
    assert r["annual_renewals"][0]["due"] == "2026-10-25"


def test_watchdog_ignores_a_one_off_purchase_after_a_tiny_first_charge(conn, cfg, world, accounts):
    chk = accounts["checking"]
    add_txn(conn, chk, "2026-09-01", -50, "CORNER KIOSK", "Shopping")
    add_txn(conn, chk, "2026-09-10", -4000, "CORNER KIOSK", "Shopping")
    conn.commit()
    r = analysis.subscription_watchdog(conn, cfg, today=TODAY)
    assert [t["merchant"] for t in r["trials_turned_paid"]] == ["Streamly Trial"]


def test_refund_tracker_matches_received_and_lists_expected(conn, cfg, world):
    r = analysis.refund_tracker(conn, cfg, today=TODAY)
    got = r["received_refunds"]
    assert len(got) == 1 and got[0]["amount"]["minor"] == 12000
    assert got[0]["matched_purchase"]["match"] == "exact_amount"
    assert got[0]["matched_purchase"]["date"] == "2026-09-10"
    exp = r["expected_refunds"]
    assert [e["merchant"] for e in exp] == ["Gadget Shop"]
    assert exp[0]["days_waiting"] == 41 and exp[0]["transaction_id"] == world["waiting"]
    assert r["totals"]["expected"]["minor"] == 8000


def test_refund_arrival_clears_expected_and_partial_stays(conn, cfg, world, accounts):
    chk = accounts["checking"]
    add_txn(conn, chk, "2026-10-08", 3000, "GADGET SHOP", "Shopping")
    r = analysis.refund_tracker(conn, cfg, today=TODAY)
    assert r["expected_refunds"][0]["received_so_far"]["minor"] == 3000
    add_txn(conn, chk, "2026-10-09", 5000, "GADGET SHOP", "Shopping")
    assert analysis.refund_tracker(conn, cfg, today=TODAY)["expected_refunds"] == []


def test_one_credit_clears_only_one_of_two_tagged_purchases(conn, cfg, accounts):
    chk = accounts["checking"]
    for d in ("2026-09-01", "2026-09-05"):
        tid = add_txn(conn, chk, d, -5000, "TWIN STORE", "Shopping")
        conn.execute("INSERT INTO tags (transaction_id, tag) VALUES (?, 'refund-pending')", (tid,))
    add_txn(conn, chk, "2026-10-01", 5000, "TWIN STORE", "Shopping")
    conn.commit()
    r = analysis.refund_tracker(conn, cfg, today=TODAY)
    assert len(r["expected_refunds"]) == 1
    assert r["expected_refunds"][0]["purchase_date"] == "2026-09-05"  # oldest purchase consumed the credit
    assert r["expected_refunds"][0]["received_so_far"]["minor"] == 0
    assert r["totals"]["expected"]["minor"] == 5000


def test_partial_credit_spills_across_tagged_purchases(conn, cfg, accounts):
    chk = accounts["checking"]
    for d in ("2026-09-01", "2026-09-05"):
        tid = add_txn(conn, chk, d, -5000, "TWIN STORE", "Shopping")
        conn.execute("INSERT INTO tags (transaction_id, tag) VALUES (?, 'refund-pending')", (tid,))
    add_txn(conn, chk, "2026-10-01", 7000, "TWIN STORE", "Shopping")
    conn.commit()
    r = analysis.refund_tracker(conn, cfg, today=TODAY)
    assert len(r["expected_refunds"]) == 1
    assert r["expected_refunds"][0]["received_so_far"]["minor"] == 2000
    assert r["totals"]["expected"]["minor"] == 3000


def test_refund_tracker_validates_days(conn, cfg, world):
    for bad in (0, 366, "x"):
        with pytest.raises(GuardError):
            analysis.refund_tracker(conn, cfg, days=bad, today=TODAY)


def test_category_trend_direction_and_zero_months(conn, cfg, world):
    up = analysis.category_trend(conn, cfg, category="Dining", months=7, today=TODAY)
    assert up["trend"]["direction"] == "up" and up["trend"]["slope_per_month"]["minor"] == 5000
    assert len(up["months"]) == 7 and up["months"][-1]["spent"]["minor"] == 9450  # partial October
    assert up["trend"]["median_last_3_complete_months"]["minor"] == 30000
    flat = analysis.category_trend(conn, cfg, category="groceries", months=6, today=TODAY)
    assert flat["trend"]["direction"] == "flat" and flat["budget"] is None
    with pytest.raises(GuardError):
        analysis.category_trend(conn, cfg, category="Nope", today=TODAY)
    with pytest.raises(GuardError):
        analysis.category_trend(conn, cfg, category="Dining", months=0, today=TODAY)


def test_category_trend_reports_budget(conn, cfg, world):
    cfg.budgets_file.write_text('[[budget]]\ncategory = "Groceries"\nmonthly = 150\n', encoding="utf-8")
    r = analysis.category_trend(conn, cfg, category="Groceries", months=6, today=TODAY)
    assert r["budget"]["monthly"]["minor"] == 15000 and r["budget"]["months_over_budget"] == 5


def test_merchant_history_series(conn, cfg, world):
    r = analysis.merchant_history(conn, cfg, merchant="b and h photo", months=12, today=TODAY)
    assert len(r["months"]) == 12 and r["months"][0]["month"] == "2025-11"
    assert sum(m["spent"]["minor"] for m in r["months"]) == r["totals"]["spent"]["minor"] == 80000
    assert r["totals"]["count"] == 5 and r["totals"]["average_ticket"]["minor"] == 16000
    assert (r["first_seen"], r["last_seen"]) == ("2026-03-03", "2026-10-05")
    rent = analysis.merchant_history(conn, cfg, merchant="Rent Payment", months=3, today=TODAY)
    assert rent["recurring"] and rent["recurring"][0]["cadence"] == "monthly"
    with pytest.raises(GuardError):
        analysis.merchant_history(conn, cfg, merchant="Nobody Inc", today=TODAY)
