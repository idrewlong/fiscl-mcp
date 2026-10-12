"""Phase 1 planning: manual assets, net worth, afford, debt payoff, goals."""

from __future__ import annotations

import pytest

from fiscl import analysis
from fiscl.config import ConfigError, DebtTerms, load_config
from fiscl.db import add_account
from fiscl.demo import load_demo
from fiscl.guard import GuardError

from .conftest import TODAY, add_txn


def _balance(conn, acct, day, minor):
    conn.execute(
        "INSERT OR REPLACE INTO balances (account_id, as_of, amount, source) VALUES (?, ?, ?, 'manual')",
        (acct, day, minor),
    )


def _m(r):
    return r["minor"]


# --- manage_asset ----------------------------------------------------------------------------


def test_manage_asset_dry_run_changes_nothing_then_applies(conn, cfg):
    r = analysis.manage_asset(conn, cfg, action="add", name="Truck", kind="vehicle", value=18000, today=TODAY)
    assert r["dry_run"] is True and r["changes"][0]["after"]["value"]["minor"] == 1800000
    assert "data_freshness" in r["meta"]
    assert conn.execute("SELECT COUNT(*) FROM manual_assets").fetchone()[0] == 0

    analysis.manage_asset(
        conn, cfg, action="add", name="Truck", kind="vehicle", value=18000, dry_run=False, today=TODAY
    )
    row = conn.execute("SELECT * FROM manual_assets").fetchone()
    assert (row["value"], row["as_of"]) == (1800000, "2026-10-12")

    r = analysis.manage_asset(conn, cfg, action="update", name="truck", value=17000.5, dry_run=False, today=TODAY)
    assert r["changes"][0]["before"]["value"]["minor"] == 1800000
    assert conn.execute("SELECT value FROM manual_assets").fetchone()[0] == 1700050

    analysis.manage_asset(conn, cfg, action="remove", name="Truck", dry_run=False, today=TODAY)
    assert conn.execute("SELECT COUNT(*) FROM manual_assets").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "add", "name": "X", "kind": "boat", "value": 1},  # unknown kind
        {"action": "add", "name": "X", "kind": "cash"},  # no value
        {"action": "add", "name": "X", "kind": "cash", "value": -5},  # negative
        {"action": "add", "name": "X", "kind": "cash", "value": 5, "as_of": "2999-01-01"},  # future
        {"action": "update", "name": "Nope", "value": 5},  # unknown
        {"action": "remove", "name": "Nope"},
        {"action": "sell", "name": "X"},
        {"action": "add", "name": "", "kind": "cash", "value": 1},
    ],
)
def test_manage_asset_rejects_bad_input(conn, cfg, kwargs):
    with pytest.raises(GuardError):
        analysis.manage_asset(conn, cfg, today=TODAY, **kwargs)


def test_manage_asset_duplicate_and_empty_update(conn, cfg):
    analysis.manage_asset(conn, cfg, action="add", name="Gear", kind="equipment", value=500, dry_run=False, today=TODAY)
    with pytest.raises(GuardError, match="already exists"):
        analysis.manage_asset(conn, cfg, action="add", name="GEAR", kind="equipment", value=1, today=TODAY)
    with pytest.raises(GuardError, match="at least one"):
        analysis.manage_asset(conn, cfg, action="update", name="Gear", today=TODAY)


# --- net_worth -------------------------------------------------------------------------------


def test_net_worth_subtracts_cards_and_manual_liabilities(conn, cfg):
    chk = add_account(conn, "Checking", "checking")
    card = add_account(conn, "Card", "credit")
    stale = add_account(conn, "Old Savings", "savings")
    add_account(conn, "No Data", "checking")
    _balance(conn, chk, "2026-09-12", 1_000_000)
    add_txn(conn, chk, "2026-09-20", 50_000, "PAYCHECK")  # rolled forward
    _balance(conn, card, "2026-10-10", -120_000)  # negative = owed
    _balance(conn, stale, "2026-07-01", 300_000)
    conn.commit()
    analysis.manage_asset(
        conn, cfg, action="add", name="House", kind="property", value=250000, dry_run=False, today=TODAY
    )
    analysis.manage_asset(
        conn, cfg, action="add", name="Car loan", kind="liability", value=9000, dry_run=False, today=TODAY
    )

    r = analysis.net_worth(conn, cfg, today=TODAY)
    assert _m(r["total_assets"]) == 1_050_000 + 300_000 + 25_000_000
    assert _m(r["total_liabilities"]) == 120_000 + 900_000
    assert _m(r["net_worth"]) == _m(r["total_assets"]) - _m(r["total_liabilities"])
    assert {x["name"] for x in r["liabilities"]} == {"Card", "Car loan"}
    assert all(x["amount"]["minor"] >= 0 for x in r["liabilities"])
    text = " ".join(r["assumptions"])
    assert "No Data" in text and "Old Savings" in text and "negative" in text
    assert "data_freshness" in r["meta"]


def test_net_worth_change_vs_uses_history_only_where_it_exists(conn, cfg):
    chk = add_account(conn, "Checking", "checking")
    _balance(conn, chk, "2026-10-12", 500_000)
    add_txn(conn, chk, "2026-09-01", 100_000, "PAYCHECK")  # earliest data: 2026-09-01
    add_txn(conn, chk, "2026-10-05", 40_000, "PAYCHECK")
    conn.commit()
    r = analysis.net_worth(conn, cfg, today=TODAY)
    by_days = {c["days"]: c for c in r["change_vs"]}
    assert set(by_days) == {30}  # 90 and 365 days ago predate the data
    assert _m(by_days[30]["accounts_net_then"]) == 500_000 - 40_000  # 2026-09-12: before the 10-05 paycheck
    assert _m(by_days[30]["change"]) == 40_000


def test_net_worth_as_of_rolls_back_and_rejects_future(conn, cfg):
    chk = add_account(conn, "Checking", "checking")
    _balance(conn, chk, "2026-10-12", 500_000)
    add_txn(conn, chk, "2026-10-05", 40_000, "PAYCHECK")
    conn.commit()
    r = analysis.net_worth(conn, cfg, as_of="2026-10-01", today=TODAY)
    assert _m(r["net_worth"]) == 460_000
    with pytest.raises(GuardError):
        analysis.net_worth(conn, cfg, as_of="2026-12-01", today=TODAY)


# --- afford ----------------------------------------------------------------------------------


def test_afford_demo_returns_baseline_and_new_low(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    before = analysis.forecast_cash(conn, cfg, today=TODAY)
    r = analysis.afford(conn, cfg, amount=2000, today=TODAY)
    base, new = r["baseline"]["lowest_balance"], r["with_purchase"]["lowest_balance"]
    assert base["amount"]["minor"] > 0 and new["date"]
    assert _m(new["amount"]) == _m(base["amount"]) - 200_000
    assert r["affordable"] is (_m(new["amount"]) >= _m(r["buffer"]))
    assert _m(r["delta"]["lowest_balance"]) == -200_000
    assert r["assumptions"] and "data_freshness" in r["meta"]
    assert analysis.forecast_cash(conn, cfg, today=TODAY) == before  # the forecast itself is untouched


def test_afford_matches_new_low_against_buffer(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    low = _m(analysis.afford(conn, cfg, amount=2000, today=TODAY)["with_purchase"]["lowest_balance"]["amount"])
    yes = analysis.afford(conn, cfg, amount=2000, buffer=(low - 100) / 100, today=TODAY)
    no = analysis.afford(conn, cfg, amount=2000, buffer=(low + 100) / 100, today=TODAY)
    assert yes["affordable"] is True and yes["shortfall"] is None
    assert no["affordable"] is False and _m(no["shortfall"]) == 100
    huge = analysis.afford(conn, cfg, amount=1_000_000, today=TODAY)
    assert huge["affordable"] is False and huge["with_purchase"]["goes_negative"] is True


def test_afford_future_date_and_recurring(conn, cfg):
    load_demo(conn, cfg, today=TODAY)
    one = analysis.afford(conn, cfg, amount=300, date="2026-10-20", days=70, today=TODAY)
    monthly = analysis.afford(conn, cfg, amount=300, date="2026-10-20", days=70, recurring_monthly=True, today=TODAY)
    assert one["charges_in_window"] == 1 and monthly["charges_in_window"] == 3  # 10-20, 11-20, 12-20
    assert _m(monthly["delta"]["lowest_balance"]) <= _m(one["delta"]["lowest_balance"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount": 0},
        {"amount": -5},
        {"amount": None},
        {"amount": 10, "days": 500},
        {"amount": 10, "date": "2026-10-01"},
        {"amount": 10, "date": "2027-06-01"},
        {"amount": 10, "date": "soon"},
        {"amount": 10, "buffer": -1},
    ],
)
def test_afford_rejects_bad_input(conn, cfg, kwargs):
    load_demo(conn, cfg, today=TODAY)
    with pytest.raises(GuardError):
        analysis.afford(conn, cfg, today=TODAY, **kwargs)


# --- debt_payoff (hand-computed cases) ---------------------------------------------------------


def _debt(name, balance, apr, minimum):
    return {"name": name, "balance": balance, "apr": apr, "min_payment": minimum}


def test_debt_payoff_single_debt_hand_computed(conn, cfg):
    r = analysis.debt_payoff(conn, cfg, strategy="avalanche", debts=[_debt("Card", 1000, 12, 100)], today=TODAY)
    s = r["strategies"]["avalanche"]
    assert s["months"] == 11 and s["paid_off"] is True
    assert _m(s["total_interest"]) == 5898
    assert _m(s["total_paid"]) == 105_898
    assert s["payoff_order"][0]["paid_off_month"] == 11
    assert s["payoff_order"][0]["payoff_date"] == "2027-09-12"
    assert len(s["schedule"]) == 6
    assert _m(s["schedule"][0]["interest"]) == 1000  # $1,000 x 12% / 12
    assert "data_freshness" in r["meta"]


def test_debt_payoff_two_cards_both_strategies(conn, cfg):
    debts = [_debt("Card A", 1000, 24, 100), _debt("Card B", 500, 12, 50)]
    r = analysis.debt_payoff(conn, cfg, extra_monthly=100, debts=debts, today=TODAY)
    assert _m(r["monthly_payment_pool"]) == 25_000
    av, sn = r["strategies"]["avalanche"], r["strategies"]["snowball"]

    by = {d["name"]: d for d in av["payoff_order"]}
    assert av["months"] == 7
    assert (by["Card A"]["paid_off_month"], by["Card B"]["paid_off_month"]) == (6, 7)
    assert _m(by["Card A"]["interest_paid"]) == 6454 and _m(by["Card B"]["interest_paid"]) == 2404
    assert _m(av["total_interest"]) == 8858 and _m(av["total_paid"]) == 158_858

    by = {d["name"]: d for d in sn["payoff_order"]}
    assert sn["months"] == 7
    assert (by["Card B"]["paid_off_month"], by["Card A"]["paid_off_month"]) == (4, 7)
    assert _m(by["Card A"]["interest_paid"]) == 9076 and _m(by["Card B"]["interest_paid"]) == 1125
    assert _m(sn["total_interest"]) == 10_201 and _m(sn["total_paid"]) == 160_201

    assert _m(r["comparison"]["interest_saved_by_avalanche"]) == 10_201 - 8858
    assert r["comparison"]["months_saved_by_avalanche"] == 0


def test_debt_payoff_caps_when_minimum_cannot_outrun_interest(conn, cfg):
    r = analysis.debt_payoff(conn, cfg, strategy="snowball", debts=[_debt("Card", 10000, 30, 100)], today=TODAY)
    s = r["strategies"]["snowball"]
    assert s["paid_off"] is False and s["months"] == 600 and "cap" in s["note"] and s["payoff_date"] is None


def test_debt_payoff_defaults_to_accounts_with_config_terms(conn, cfg):
    card = add_account(conn, "Chase Card", "credit")
    paid = add_account(conn, "Paid Card", "credit")
    _balance(conn, card, "2026-10-10", -100_000)
    _balance(conn, paid, "2026-10-10", 0)
    conn.commit()
    with pytest.raises(GuardError, match="Chase Card") as exc:
        analysis.debt_payoff(conn, cfg, today=TODAY)
    assert "Paid Card" not in str(exc.value) and "never guesses" in str(exc.value)

    cfg.debts["chase card"] = DebtTerms(apr_pct="12", min_payment=10_000)
    r = analysis.debt_payoff(conn, cfg, strategy="avalanche", today=TODAY)
    assert r["strategies"]["avalanche"]["months"] == 11
    assert _m(r["debts"][0]["balance"]) == 100_000


def test_debt_payoff_args_fall_back_to_config_and_validate(conn, cfg):
    cfg.debts["card a"] = DebtTerms(apr_pct="24", min_payment=10_000)
    r = analysis.debt_payoff(conn, cfg, debts=[{"name": "Card A", "balance": 1000}], today=TODAY)
    assert r["strategies"]["avalanche"]["months"] > 0
    bad = [
        [{"name": "X", "balance": 100, "apr": 10}],  # no minimum
        [{"name": "X", "balance": 100, "min_payment": 10}],  # no apr
        [{"name": "X", "apr": 10, "min_payment": 10}],  # no balance
        [{"name": "X", "balance": 100, "apr": 120, "min_payment": 10}],
        [{"name": "X", "balance": 100, "apr": "abc", "min_payment": 10}],
        [{"name": "X", "balance": 0, "apr": 10, "min_payment": 10}],
        [{"name": "X", "balance": 100, "apr": 10, "min_payment": 0}],
        [_debt("X", 100, 10, 10), _debt("x", 100, 10, 10)],
        [{"name": "X", "balance": 100, "apr": 10, "min_payment": 10, "extra": 1}],
        [],
    ]
    for debts in bad:
        with pytest.raises(GuardError):
            analysis.debt_payoff(conn, cfg, debts=debts, today=TODAY)
    with pytest.raises(GuardError):
        analysis.debt_payoff(conn, cfg, strategy="yolo", debts=[_debt("X", 100, 10, 10)], today=TODAY)
    with pytest.raises(GuardError):
        analysis.debt_payoff(conn, cfg, extra_monthly=-1, debts=[_debt("X", 100, 10, 10)], today=TODAY)
    with pytest.raises(GuardError, match="no credit or loan"):
        analysis.debt_payoff(conn, cfg, today=TODAY)


def test_debts_config_table_parses_and_rejects_bad_entries(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text('[debts."Chase Sapphire"]\napr = 24.99\nmin_payment = 35\n', encoding="utf-8")
    terms = load_config(home).debts["chase sapphire"]
    assert (terms.apr_pct, terms.min_payment) == ("24.99", 3500)
    for body in (
        "apr = 24.99",
        "apr = 120\nmin_payment = 5",
        "apr = 5\nmin_payment = 0",
        "apr = 5\nmin_payment = 5\nx = 1",
    ):
        (home / "config.toml").write_text(f'[debts."Card"]\n{body}\n', encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(home)


# --- goals -----------------------------------------------------------------------------------


def test_manage_goal_lifecycle(conn, cfg):
    sav = add_account(conn, "Savings", "savings")
    conn.commit()
    r = analysis.manage_goal(conn, cfg, action="add", name="Trip", target=3000, target_date="2027-03-01", today=TODAY)
    assert r["dry_run"] is True and conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0
    analysis.manage_goal(
        conn,
        cfg,
        action="add",
        name="Trip",
        target=3000,
        target_date="2027-03-01",
        saved=500,
        dry_run=False,
        today=TODAY,
    )
    g = conn.execute("SELECT * FROM goals").fetchone()
    assert (g["target"], g["saved"], g["created_at"]) == (300_000, 50_000, "2026-10-12")

    analysis.manage_goal(conn, cfg, action="update", name="trip", account="Savings", dry_run=False, today=TODAY)
    assert conn.execute("SELECT account_id FROM goals").fetchone()[0] == sav
    analysis.manage_goal(conn, cfg, action="update", name="Trip", target_date="", dry_run=False, today=TODAY)
    assert conn.execute("SELECT target_date FROM goals").fetchone()[0] is None

    r = analysis.manage_goal(conn, cfg, action="archive", name="Trip", dry_run=False, today=TODAY)
    assert r["changes"][0]["after"]["archived"] is True
    assert analysis.goal_progress(conn, cfg, today=TODAY)["goals"] == []
    with pytest.raises(GuardError, match="archived"):
        analysis.manage_goal(conn, cfg, action="add", name="Trip", target=1, today=TODAY)
    analysis.manage_goal(conn, cfg, action="update", name="Trip", target=2000, dry_run=False, today=TODAY)  # restores
    assert len(analysis.goal_progress(conn, cfg, today=TODAY)["goals"]) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "add", "name": "G"},  # no target
        {"action": "add", "name": "G", "target": 0},
        {"action": "add", "name": "G", "target": 10, "target_date": "2020-01-01"},
        {"action": "add", "name": "G", "target": 10, "account": "Nope"},
        {"action": "add", "name": "G", "target": 10, "account": "Savings", "saved": 5},
        {"action": "add", "name": "G", "target": 10, "saved": -1},
        {"action": "update", "name": "Missing", "target": 10},
        {"action": "archive", "name": "Missing"},
        {"action": "delete", "name": "G"},
    ],
)
def test_manage_goal_rejects_bad_input(conn, cfg, kwargs):
    add_account(conn, "Savings", "savings")
    with pytest.raises(GuardError):
        analysis.manage_goal(conn, cfg, today=TODAY, **kwargs)


def test_goal_progress_account_pace_and_on_track(conn, cfg):
    sav = add_account(conn, "Savings", "savings")
    _balance(conn, sav, "2026-07-01", 100_000)
    _balance(conn, sav, "2026-10-12", 280_000)  # +$1,800 in the last 90 days is about $600/month
    add_txn(conn, sav, "2026-07-02", 5_000, "DEPOSIT")  # history reaches back past 90 days
    conn.commit()
    analysis.manage_goal(
        conn,
        cfg,
        action="add",
        name="Fund",
        target=500,
        account="Savings",
        target_date="2027-04-12",
        dry_run=False,
        today=TODAY,
    )
    analysis.manage_goal(
        conn,
        cfg,
        action="add",
        name="Big",
        target=100000,
        account="Savings",
        target_date="2027-04-12",
        dry_run=False,
        today=TODAY,
    )
    goals = {g["name"]: g for g in analysis.goal_progress(conn, cfg, today=TODAY)["goals"]}

    fund = goals["Fund"]
    assert _m(fund["current"]) == 280_000 and fund["pct"] == "560.0"
    assert fund["on_track"] is True and _m(fund["remaining"]) == 0

    big = goals["Big"]
    assert _m(big["remaining"]) == 10_000_000 - 280_000
    assert _m(big["required_monthly"]) > _m(big["pace_last_90_days_monthly"]) > 0
    assert big["on_track"] is False
    one = analysis.goal_progress(conn, cfg, name="big", today=TODAY)
    assert [g["name"] for g in one["goals"]] == ["Big"] and "data_freshness" in one["meta"]
    with pytest.raises(GuardError):
        analysis.goal_progress(conn, cfg, name="nope", today=TODAY)


def test_goal_progress_manual_goal_has_no_pace(conn, cfg):
    analysis.manage_goal(
        conn,
        cfg,
        action="add",
        name="Gift",
        target=1200,
        saved=300,
        target_date="2027-10-12",
        dry_run=False,
        today=TODAY,
    )
    g = analysis.goal_progress(conn, cfg, today=TODAY)["goals"][0]
    assert _m(g["current"]) == 30_000 and g["pct"] == "25.0"
    assert g["on_track"] is None and g["pace_last_90_days_monthly"] is None
    assert _m(g["required_monthly"]) == 7505  # $900 left over 365 / 30.4375 months
    assert any("Manual" in n for n in g["notes"])
