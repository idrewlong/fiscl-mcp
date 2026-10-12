"""QA edge-case tests for v2 Phase 1 (edits, planning, monitoring, guardrails across the MCP surface).

BUG-n references point at the QA report; all the former xfail regressions now pass.
"""

from __future__ import annotations

import json
import socket
import time

import pytest
from mcp import Client

from fiscl import analysis, tax
from fiscl.config import load_budgets
from fiscl.guard import GuardError
from fiscl.importers.core import import_statements
from fiscl.importers.types import ParsedStatement, ParsedTxn
from fiscl.server import build_server

from .conftest import TODAY, add_txn

TOP = {"amount": -10000}


def _cats(conn, cfg, **kw):
    r = analysis.spending_summary(conn, cfg, from_="2026-10-01", to="2026-10-31", compare_to="none", today=TODAY, **kw)
    return {g["name"]: g["spent"]["minor"] for g in r["groups"]}


# --- split lifecycle ----------------------------------------------------------------------


def test_split_unsplit_resplit_math_is_exact(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", -10000, "AMAZON", "Shopping")
    first = [{"amount": -33.33, "category": "Groceries"}, {"amount": -33.33, "category": "Dining"},
             {"amount": -33.34, "category": "Auto: Fuel"}]  # fmt: skip
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=first, dry_run=False)
    assert _cats(conn, cfg) == {"Groceries": 3333, "Dining": 3333, "Auto: Fuel": 3334}

    analysis.split_transaction(conn, cfg, transaction_id=t, parts=[], dry_run=False)
    assert _cats(conn, cfg) == {"Shopping": 10000}
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0

    second = [{"amount": -99.99, "category": "Groceries"}, {"amount": -0.01, "category": "Dining"}]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=second, dry_run=False)
    assert _cats(conn, cfg) == {"Groceries": 9999, "Dining": 1}
    # Cash flow and the listing are unaffected by any of this.
    cf = analysis.cash_flow(conn, cfg, from_="2026-10-01", to="2026-10-31", today=TODAY)
    assert cf["totals"]["spending"]["minor"] == 10000


def test_split_part_limits_and_zero_parts(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", -10000, "AMAZON", "Shopping")
    twenty_one = [{"amount": -1, "category": "Dining"}] * 20 + [{"amount": -80, "category": "Dining"}]
    with pytest.raises(GuardError):
        analysis.split_transaction(conn, cfg, transaction_id=t, parts=twenty_one, dry_run=False)
    with pytest.raises(GuardError):  # one part is not a split
        analysis.split_transaction(
            conn, cfg, transaction_id=t, parts=[{"amount": -100, "category": "Dining"}], dry_run=False
        )
    # A zero or wrong-sign part that still sums is not a sensible split.
    for parts in (
        [{"amount": 0, "category": "Dining"}, {"amount": -100, "category": "Dining"}],
        [{"amount": 50, "category": "Dining"}, {"amount": -150, "category": "Dining"}],
    ):
        with pytest.raises(GuardError):
            analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0


def test_split_of_refund_keeps_sign_and_nets_spending(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", 5000, "AMAZON REFUND", "Shopping")
    parts = [{"amount": 30, "category": "Groceries"}, {"amount": 20, "category": "Dining"}]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    assert _cats(conn, cfg) == {"Groceries": -3000, "Dining": -2000}


def test_split_parent_deleted_cascades_children(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", -10000, "AMAZON", "Shopping")
    parts = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Dining"}]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    conn.execute("DELETE FROM transactions WHERE id = ?", (t,))
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0
    assert _cats(conn, cfg) == {}


def _pend_then_post(conn, cfg):
    pend = ParsedStatement(format="csv", txns=[ParsedTxn("2026-10-01", -12000, "CHIPOTLE 22", pending=True)])
    import_statements(conn, cfg, [pend], "p", "d1", account_name="Checking")
    return conn.execute("SELECT id FROM transactions").fetchone()[0]


def _post(conn, cfg):
    posted = ParsedStatement(format="csv", txns=[ParsedTxn("2026-10-03", -12000, "CHIPOTLE 22")])
    return import_statements(conn, cfg, [posted], "q", "d2", account_name="Checking")


def test_pending_to_posted_keeps_splits_tags_and_notes(conn, cfg, accounts):
    tid = _pend_then_post(conn, cfg)
    analysis.add_note(conn, cfg, transaction_id=tid, note="team lunch", dry_run=False)
    analysis.tag_transactions(conn, cfg, [{"transaction_ids": [tid], "add": ["refund-pending"]}], dry_run=False)
    parts = [{"amount": -70, "category": "Dining"}, {"amount": -50, "category": "Business: Meals", "scope": "business"}]
    analysis.split_transaction(conn, cfg, transaction_id=tid, parts=parts, dry_run=False)
    assert _post(conn, cfg).pending_replaced == 1
    row = conn.execute("SELECT id, note FROM transactions").fetchone()
    assert row["note"] == "team lunch"
    assert conn.execute("SELECT COUNT(*) FROM splits WHERE transaction_id = ?", (row["id"],)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE transaction_id = ?", (row["id"],)).fetchone()[0] == 1


def test_pending_to_posted_reports_carried_edits(conn, cfg, accounts):
    tid = _pend_then_post(conn, cfg)
    analysis.tag_transactions(conn, cfg, [{"transaction_ids": [tid], "add": ["x"]}], dry_run=False)
    r = _post(conn, cfg)
    assert (r.pending_replaced, r.edits_carried, r.edits_dropped) == (1, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_pending_to_posted_carries_manual_category_and_scope(conn, cfg, accounts):
    tid = _pend_then_post(conn, cfg)
    analysis.apply_categories(conn, cfg, [{"transaction_ids": [tid], "category": "Dining"}], dry_run=False)
    conn.execute("UPDATE transactions SET scope = 'business' WHERE id = ?", (tid,))
    assert _post(conn, cfg).edits_carried == 1
    row = conn.execute(
        "SELECT c.name, t.category_source, t.scope FROM transactions t JOIN categories c ON c.id = t.category_id"
    ).fetchone()
    assert tuple(row) == ("Dining", "manual", "business")


def test_pending_to_posted_drops_split_that_no_longer_sums(conn, cfg, accounts):
    from fiscl.importers.core import _carry_edits

    tid = _pend_then_post(conn, cfg)
    parts = [{"amount": -70, "category": "Dining"}, {"amount": -50, "category": "Groceries"}]
    analysis.split_transaction(conn, cfg, transaction_id=tid, parts=parts, dry_run=False)
    pend = conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    new_id = conn.execute(
        "INSERT INTO transactions (account_id, posted_date, amount, currency, raw_description, merchant, "
        "is_pending, dedupe_key) VALUES (?, '2026-10-06', -13000, 'USD', 'X', 'X', 0, 'k')",
        (pend["account_id"],),
    ).lastrowid
    assert _carry_edits(conn, pend, new_id, -13000) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM splits WHERE transaction_id = ?", (new_id,)).fetchone()[0] == 0


# --- set_budget on hand-edited TOML --------------------------------------------------------

HAND = """# my budgets
# keep this header

[[budget]]   # food
category = "Dining"
monthly = 400

# --- transport section ---
[[budget]]
category = "Auto: Fuel"   # gas
monthly = 120.50
rollover = true
# trailing footer comment
"""


def test_set_budget_preserves_comments_between_blocks_and_handles_commented_headers(conn, cfg):
    cfg.budgets_file.write_text(HAND, encoding="utf-8")
    analysis.set_budget(conn, cfg, [{"category": "Groceries", "monthly": 600}], dry_run=False)
    text = cfg.budgets_file.read_text()
    assert "# keep this header" in text
    assert "# --- transport section ---" in text and "# trailing footer comment" in text
    assert {b.category for b in load_budgets(cfg)} == {"Dining", "Auto: Fuel", "Groceries"}


def test_set_budget_commented_header_does_not_corrupt_or_duplicate(conn, cfg):
    cfg.budgets_file.write_text(HAND, encoding="utf-8")
    try:
        analysis.set_budget(conn, cfg, [{"category": "Groceries", "monthly": 600}], dry_run=False)
    except Exception:  # a refusal is acceptable; silent corruption is not
        pass
    cats = [b.category for b in load_budgets(cfg)]
    assert len(cats) == len(set(cats)), cats
    assert {"Dining", "Auto: Fuel"} <= set(cats)


def test_set_budget_odd_spacing_and_other_tables_survive(conn, cfg):
    cfg.budgets_file.write_text(
        '# h\n\n[settings]\nnote = "keep me"\n\n[[budget]]\ncategory="Dining"\nmonthly=5\n', encoding="utf-8"
    )
    analysis.set_budget(conn, cfg, [{"category": "Dining", "monthly": 7}], dry_run=False)
    text = cfg.budgets_file.read_text()
    assert 'note = "keep me"' in text and "# h" in text
    assert [(b.category, b.monthly) for b in load_budgets(cfg)] == [("Dining", 700)]


# --- hostile / odd input on the real MCP surface ------------------------------------------


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e400", "sNaN"])
def test_non_finite_money_args_are_guard_errors(conn, cfg, bad):
    from fiscl.common import money_arg

    with pytest.raises(GuardError):
        money_arg(bad, cfg, "amount")


def test_huge_money_arg_is_guard_error(conn, cfg):
    from fiscl.common import money_arg

    with pytest.raises(GuardError):
        money_arg(10**30, cfg, "amount")


def test_huge_transaction_id_is_guard_error(conn, cfg, accounts):
    with pytest.raises(GuardError):
        analysis.add_note(conn, cfg, transaction_id=10**30, note="x")
    with pytest.raises(GuardError):
        analysis.tag_transactions(conn, cfg, [{"transaction_ids": [10**30], "add": ["x"]}])


def test_minus_zero_and_tiny_amounts_rejected(conn, cfg, accounts):
    for bad in ("-0", 0, "0.001", -5):
        with pytest.raises(GuardError):
            analysis.afford(conn, cfg, amount=bad, today=TODAY)


def test_bool_money_and_ids_rejected_at_service_layer(conn, cfg, accounts):
    with pytest.raises(GuardError):
        analysis.afford(conn, cfg, amount=True, today=TODAY)
    with pytest.raises(GuardError):
        analysis.tag_transactions(conn, cfg, [{"transaction_ids": [True], "add": ["x"]}])
    with pytest.raises(GuardError):
        analysis.manage_asset(conn, cfg, action="add", name="T", kind="cash", value=True)


def test_tags_strip_control_and_bidi_and_stay_single_line(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", -1000, "SHOP", "Shopping")
    analysis.tag_transactions(conn, cfg, [{"transaction_ids": [t], "add": ["a‮b\x00c\nd​e", "ÜNI"]}], dry_run=False)
    tags = [r[0] for r in conn.execute("SELECT tag FROM tags ORDER BY tag")]
    for tag in tags:
        assert tag == tag.lower()
        assert not any(ch in tag for ch in "‮\x00\n​")
    assert "üni" in tags


@pytest.mark.parametrize("bad_date", ["2026-02-30", "2026-13-01", "2026-1-1", "tomorrow"])
def test_impossible_dates_are_tool_errors(conn, cfg, accounts, bad_date):
    for call in (
        lambda: analysis.find_anomalies(conn, cfg, from_=bad_date, today=TODAY),
        lambda: analysis.net_worth(conn, cfg, as_of=bad_date, today=TODAY),
        lambda: analysis.manage_goal(conn, cfg, action="add", name="G", target=10, target_date=bad_date, today=TODAY),
    ):
        try:
            call()
        except GuardError:
            continue
        pytest.fail(f"{bad_date!r} accepted")


@pytest.mark.parametrize("limit", [0, -1, "5", 1.5])
def test_limit_edge_values(conn, cfg, accounts, limit):
    for fn in (analysis.find_anomalies, analysis.query_transactions):
        with pytest.raises(GuardError):
            fn(conn, cfg, limit=limit, today=TODAY)


def test_huge_limit_is_clamped_not_honoured(conn, cfg, accounts):
    r = analysis.find_anomalies(conn, cfg, limit=10**9, today=TODAY)
    assert r["meta"]["row_count"] <= 500


# --- empty database ------------------------------------------------------------------------


def test_new_read_tools_on_empty_database_do_not_crash(conn, cfg):
    nw = analysis.net_worth(conn, cfg, today=TODAY)
    assert nw["meta"] and nw["net_worth"]["minor"] == 0
    for r in (
        analysis.find_anomalies(conn, cfg, today=TODAY),
        analysis.subscription_watchdog(conn, cfg, today=TODAY),
        analysis.refund_tracker(conn, cfg, today=TODAY),
        analysis.goal_progress(conn, cfg, today=TODAY),
    ):
        assert "meta" in r
    assert analysis.find_anomalies(conn, cfg, today=TODAY)["findings"] == []
    with pytest.raises(GuardError):
        analysis.debt_payoff(conn, cfg, today=TODAY)
    with pytest.raises(GuardError):
        analysis.merchant_history(conn, cfg, merchant="Nobody", today=TODAY)


def test_net_worth_with_accounts_but_no_balances(conn, cfg, accounts):
    r = analysis.net_worth(conn, cfg, today=TODAY)
    assert r["net_worth"]["minor"] == 0
    assert r["assumptions"], "missing balances should be called out"


def test_afford_and_forecast_on_account_without_balance(conn, cfg, accounts):
    r = analysis.afford(conn, cfg, amount=50, today=TODAY)
    assert "meta" in r and isinstance(r["affordable"], bool)


# --- debt payoff ---------------------------------------------------------------------------


def test_debt_payoff_zero_apr_is_exact(conn, cfg):
    r = analysis.debt_payoff(
        conn, cfg, debts=[{"name": "Loan", "balance": 1000, "apr": 0, "min_payment": 100}], today=TODAY
    )
    av = r["strategies"]["avalanche"]
    assert av["months"] == 10 and av["total_interest"]["minor"] == 0 and av["total_paid"]["minor"] == 100000
    assert av["paid_off"] is True


def test_debt_payoff_minimum_below_interest_terminates_and_says_so(conn, cfg):
    t0 = time.monotonic()
    r = analysis.debt_payoff(
        conn, cfg, debts=[{"name": "Card", "balance": 1000, "apr": 20, "min_payment": 10}], today=TODAY
    )
    assert time.monotonic() - t0 < 2
    av = r["strategies"]["avalanche"]
    assert av["paid_off"] is False and av["payoff_date"] is None
    assert "outrun" in json.dumps(r) or "Not paid off" in json.dumps(r)


def test_debt_payoff_minimum_equal_to_interest_never_pays_down(conn, cfg):
    r = analysis.debt_payoff(
        conn, cfg, debts=[{"name": "Card", "balance": 1000, "apr": 20, "min_payment": 16.67}], today=TODAY
    )
    assert r["strategies"]["avalanche"]["paid_off"] is False


def test_debt_payoff_rejects_nan_and_out_of_range_rates(conn, cfg):
    for apr in ("NaN", -1, 101):
        with pytest.raises(GuardError):
            analysis.debt_payoff(
                conn, cfg, debts=[{"name": "C", "balance": 100, "apr": apr, "min_payment": 10}], today=TODAY
            )


# --- afford beyond horizon -----------------------------------------------------------------


def test_afford_date_beyond_horizon_is_an_explicit_error(conn, cfg, accounts):
    with pytest.raises(GuardError, match="window"):
        analysis.afford(conn, cfg, amount=10, date="2027-06-01", days=30, today=TODAY)
    ok = analysis.afford(conn, cfg, amount=10, date="2027-01-01", days=120, today=TODAY)
    assert "meta" in ok


# --- redaction & guardrails across new MCP tools -------------------------------------------

CARD = "4111111111111111"
SSN = "123-45-6789"


async def test_new_tools_redact_card_and_ssn_in_responses(cfg, conn, accounts):
    a = accounts["checking"]
    add_txn(conn, a, "2026-10-01", -2500, f"CORNER SHOP {CARD}", "Shopping")
    t = add_txn(conn, a, "2026-10-02", -9000, f"WIDGETS INC {SSN}", "Shopping")
    add_txn(conn, a, "2026-10-04", 9000, f"WIDGETS INC {SSN}", "Shopping")  # refund
    conn.execute("INSERT INTO tags (transaction_id, tag) VALUES (?, 'refund-pending')", (t,))
    conn.commit()

    async with Client(build_server(cfg)) as c:
        calls = [
            ("merchant_history", {"merchant": "CORNER SHOP"}),
            ("merchant_history", {"merchant": "WIDGETS INC"}),
            ("refund_tracker", {"days": 365}),
            ("find_anomalies", {"start_date": "2026-09-01", "end_date": "2026-10-12", "sensitivity": "high"}),
            ("add_note", {"transaction_id": t, "note": f"paid with {CARD} ssn {SSN}"}),
            ("tag_transactions", {"items": [{"transaction_ids": [t], "add": ["cardholder"]}]}),
            ("split_transaction", {"transaction_id": t, "parts": [
                {"amount": "-50", "category": "Dining", "note": f"{CARD}"},
                {"amount": "-40", "category": "Shopping", "note": SSN}]}),
            ("manage_asset", {"action": "add", "name": f"Acct {CARD}", "kind": "cash", "value": 5, "note": SSN}),
            ("manage_goal", {"action": "add", "name": f"Goal {SSN}", "target": 100}),
            ("query_transactions", {"start_date": "2026-10-01", "end_date": "2026-10-12"}),
            ("subscription_watchdog", {}),
            ("category_trend", {"category": "Shopping"}),
        ]  # fmt: skip
        for name, args in calls:
            r = await c.call_tool(name, args)
            blob = json.dumps(r.structured_content, ensure_ascii=False) if not r.is_error else r.content[0].text
            assert CARD not in blob and SSN not in blob, (name, blob[:300])
        # Apply the writes, then read the stored values back through other tools.
        await c.call_tool("add_note", {"transaction_id": t, "note": f"paid {CARD} {SSN}", "dry_run": False})
        await c.call_tool("manage_goal", {"action": "add", "name": f"Goal {SSN}", "target": 100, "dry_run": False})
        await c.call_tool(
            "manage_asset", {"action": "add", "name": f"Acct {CARD}", "kind": "cash", "value": 5, "dry_run": False}
        )
        for name, args in [
            ("query_transactions", {"start_date": "2026-10-01", "end_date": "2026-10-12"}),
            ("goal_progress", {}),
            ("net_worth", {}),
        ]:
            r = await c.call_tool(name, args)
            blob = json.dumps(r.structured_content, ensure_ascii=False)
            assert CARD not in blob and SSN not in blob, (name, blob[:300])
        res = await c.read_resource("fiscl://goals")
        assert SSN not in res.contents[0].text


def test_redact_masks_dict_keys_too():
    from fiscl.guard import redact

    assert CARD not in json.dumps(redact({f"card{CARD}": 1, "n": {SSN: 2}}))


@pytest.fixture
def demo(conn, cfg):
    from fiscl.demo import load_demo

    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    return cfg


async def test_every_new_write_tool_is_local_write_and_errors_not_crashes(demo):
    async with Client(build_server(demo)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        for name in (
            "add_note", "tag_transactions", "split_transaction", "dismiss_recurring", "set_budget",
            "manage_asset", "manage_goal",
        ):  # fmt: skip
            ann = tools[name].annotations
            assert ann.read_only_hint is False and ann.destructive_hint is False, name
        for name in ("net_worth", "afford", "debt_payoff", "goal_progress", "find_anomalies",
                     "subscription_watchdog", "refund_tracker", "merchant_history", "category_trend"):  # fmt: skip
            assert tools[name].annotations.read_only_hint is True, name
        bad = [
            ("split_transaction", {"transaction_id": 1, "parts": [{"amount": "x", "category": "Dining"}] * 2}),
            ("split_transaction", {"transaction_id": 1, "parts": "nope"}),
            ("set_budget", {"items": [{"category": "Dining", "monthly": "NaN"}]}),
            ("set_budget", {"items": [{"category": "Dining", "monthly": -5}]}),
            ("set_budget", {"items": [{"category": "Dining", "monthly": 5}] * 101}),
            ("tag_transactions", {"items": [{"transaction_ids": list(range(501)), "add": ["x"]}]}),
            ("dismiss_recurring", {"merchant": "No Such Thing"}),
            ("manage_asset", {"action": "explode", "name": "x"}),
            ("manage_goal", {"action": "archive", "name": "No such goal"}),
            ("afford", {"amount": "NaN"}),
        ]
        for name, args in bad:
            r = await c.call_tool(name, args)
            assert r.is_error, (name, args)


async def test_bulk_caps_enforced_on_apply_not_just_dry_run(demo, conn):
    async with Client(build_server(demo)) as c:
        r = await c.call_tool(
            "tag_transactions", {"items": [{"transaction_ids": list(range(1, 502)), "add": ["x"]}], "dry_run": False}
        )
        assert r.is_error
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE tag = 'x'").fetchone()[0] == 0


def test_additional_new_paths_make_no_network_calls(conn, cfg, accounts, monkeypatch):
    from fiscl.charts import build_chart

    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    t = add_txn(conn, accounts["checking"], "2026-10-05", -10000, "AMAZON", "Shopping")
    for m in ("07", "08", "09", "10"):
        add_txn(conn, accounts["card"], f"2026-{m}-01", -1599, "NETFLIX.COM", "Subscriptions")
    conn.commit()
    from fiscl.recurring import refresh_recurring

    refresh_recurring(conn, cfg, today=TODAY)
    merchant = conn.execute("SELECT merchant FROM recurring").fetchone()[0]
    parts = [{"amount": -60, "category": "Groceries"}, {"amount": -40, "category": "Dining"}]
    analysis.split_transaction(conn, cfg, transaction_id=t, parts=parts, dry_run=False)
    analysis.dismiss_recurring(conn, cfg, merchant=merchant, dry_run=False)
    analysis.manage_goal(conn, cfg, action="add", name="Trip", target=10, today=TODAY, dry_run=False)
    analysis.goal_progress(conn, cfg, today=TODAY)
    analysis.net_worth(conn, cfg, today=TODAY)
    for kind in ("net_worth", "category_trend"):
        build_chart(conn, cfg, kind=kind, category="Groceries", today=TODAY)
    tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)


# --- performance (spec risk: aggregates under 200 ms on a 50k-row fixture) ------------------


def test_aggregates_stay_fast_on_50k_rows_with_splits(conn, cfg, accounts):
    a = accounts["checking"]
    conn.execute("INSERT OR IGNORE INTO categories (name) VALUES ('Dining')")
    cat = conn.execute("SELECT id FROM categories WHERE name='Dining'").fetchone()[0]
    rows = [
        (a, f"2026-{1 + i % 9:02d}-{1 + i % 28:02d}", -(100 + i % 5000), "USD", f"M{i % 300}", f"M{i % 300}", cat,
         "manual", f"k{i}")
        for i in range(50_000)
    ]  # fmt: skip
    conn.executemany(
        """INSERT INTO transactions (account_id, posted_date, amount, currency, raw_description, merchant,
           category_id, category_source, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    ids = [r[0] for r in conn.execute("SELECT id FROM transactions LIMIT 5000")]
    conn.executemany(
        "INSERT INTO splits (transaction_id, amount, category_id) VALUES (?, ?, ?)",
        [(i, -50, cat) for i in ids] + [(i, -50, cat) for i in ids],
    )
    conn.commit()
    t0 = time.monotonic()
    analysis.spending_summary(conn, cfg, from_="2026-01-01", to="2026-12-31", today=TODAY)
    analysis.cash_flow(conn, cfg, from_="2026-01-01", to="2026-12-31", today=TODAY)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"two aggregates took {elapsed:.2f}s"  # spec budget is 200 ms each; allow CI noise


# --- independent oracle for debt_payoff -----------------------------------------------------


def _oracle(debts, extra, key):
    """Plain-Python amortisation, written independently of the service (integer cents, half-even)."""
    from decimal import ROUND_HALF_EVEN, Decimal

    bal = {n: b for n, b, _, _ in debts}
    apr = {n: Decimal(a) for n, _, a, _ in debts}
    mn = {n: m for n, _, _, m in debts}
    interest = dict.fromkeys(bal, 0)
    done: dict[str, int] = {}
    pool = sum(mn.values()) + extra
    month = 0
    while any(v > 0 for v in bal.values()) and month < 600:
        month += 1
        for n in bal:
            if bal[n] > 0:
                i = int((Decimal(bal[n]) * apr[n] / 1200).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
                bal[n] += i
                interest[n] += i
        left = pool
        for n in bal:
            if bal[n] > 0:
                p = min(mn[n], bal[n], left)
                bal[n] -= p
                left -= p
        for n in sorted((n for n in bal if bal[n] > 0), key=key(bal, apr)):
            p = min(left, bal[n])
            bal[n] -= p
            left -= p
        for n in bal:
            if bal[n] == 0 and n not in done:
                done[n] = month
    return month, interest, done


@pytest.mark.parametrize("extra", [0, 50, 250])
def test_debt_payoff_matches_independent_oracle(conn, cfg, extra):
    cards = [("A", 123_456, "24.99", 4_500), ("B", 50_000, "9.5", 2_000), ("C", 800_000, "17.25", 20_000)]
    debts = [{"name": n, "balance": b / 100, "apr": a, "min_payment": m / 100} for n, b, a, m in cards]
    r = analysis.debt_payoff(conn, cfg, extra_monthly=extra, debts=debts, today=TODAY)
    for strat, key in (
        ("avalanche", lambda bal, apr: lambda n: -apr[n]),
        ("snowball", lambda bal, apr: lambda n: bal[n]),
    ):
        months, interest, done = _oracle(cards, extra * 100, key)
        got = r["strategies"][strat]
        assert got["months"] == months, strat
        assert got["total_interest"]["minor"] == sum(interest.values()), strat
        assert {d["name"]: d["paid_off_month"] for d in got["payoff_order"]} == done, strat


def test_tag_filter_is_case_insensitive_and_unicode_safe(conn, cfg, accounts):
    t = add_txn(conn, accounts["checking"], "2026-10-05", -1000, "SHOP", "Shopping")
    analysis.tag_transactions(conn, cfg, [{"transaction_ids": [t], "add": ["ÜNI Test"]}], dry_run=False)
    for q in ("ÜNI Test", "üni test", " üni test "):
        r = analysis.query_transactions(conn, cfg, from_="2026-10-01", tag=q, today=TODAY)
        assert [x["id"] for x in r["transactions"]] == [t], q
    assert analysis.query_transactions(conn, cfg, from_="2026-10-01", tag="nope", today=TODAY)["transactions"] == []


def test_set_budget_header_variants_and_round_trip_failure_is_clear(conn, cfg):
    cfg.budgets_file.write_text(
        '[[ budget ]]\ncategory = "Dining"\nmonthly = 5\n\n# between\n'
        '[[budget]] # c\ncategory = "Auto: Fuel"\nmonthly = 6\n',
        encoding="utf-8",
    )
    analysis.set_budget(conn, cfg, [{"category": "Dining", "monthly": 9}], dry_run=False)
    text = cfg.budgets_file.read_text()
    assert "# between" in text
    assert sorted((b.category, b.monthly) for b in load_budgets(cfg)) == [("Auto: Fuel", 600), ("Dining", 900)]
    # repeated writes don't pile up blank lines
    analysis.set_budget(conn, cfg, [{"category": "Dining", "monthly": 9}], dry_run=False)
    assert "\n\n\n" not in cfg.budgets_file.read_text()


def test_set_budget_unrewritable_file_is_restored_with_clear_error(conn, cfg):
    from fiscl.config import ConfigError

    original = '[[budget]]\ncategory = "Dining"\nmonthly = [\n  5,\n]\n'
    cfg.budgets_file.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError):
        analysis.set_budget(conn, cfg, [{"category": "Groceries", "monthly": 6}], dry_run=False)
    assert cfg.budgets_file.read_text() == original
