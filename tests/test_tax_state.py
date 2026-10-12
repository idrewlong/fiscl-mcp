"""Phase 3 slice B: state estimated-tax dates and a planning-rate estimate next to the federal one."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from fiscl import tax
from fiscl.config import ConfigError, load_config
from fiscl.guard import GuardError
from fiscl.tax import states
from fiscl.tax.states import STATES, VERIFIED_FOR, next_business_day, state_installments

from .conftest import TODAY, add_txn


def with_config(cfg, text: str):
    cfg.config_file.write_text(text, encoding="utf-8")
    return load_config(cfg.home)


def seed(conn, accounts, income: int = 10_000_000, year: int = 2026):
    add_txn(conn, accounts["biz"], f"{year}-03-01", income, "Client payment")
    conn.commit()


# --- the table ----------------------------------------------------------------------------


def test_table_has_50_states_plus_dc_with_sources():
    assert len(STATES) == 51 and "DC" in STATES
    assert VERIFIED_FOR == 2026
    for code, info in STATES.items():
        assert info.source.startswith("https://"), code
        assert info.name, code


def test_no_income_tax_states_are_exactly_the_researched_nine():
    assert {c for c, s in STATES.items() if not s.income_tax} == {"AK", "FL", "NV", "NH", "SD", "TN", "TX", "WA", "WY"}
    for code in ("AK", "FL", "NV", "NH", "SD", "TN", "TX", "WA", "WY"):
        assert not STATES[code].estimated_required and STATES[code].installments == ()
    assert "repealed" in (STATES["NH"].caveat or "").lower()
    assert "capital gains" in (STATES["WA"].caveat or "").lower()


def test_utah_and_idaho_tax_income_but_require_no_estimates():
    for code in ("UT", "ID"):
        assert STATES[code].income_tax and not STATES[code].estimated_required
        assert STATES[code].installments == ()


def test_installment_percentages_sum_to_100():
    for code, info in STATES.items():
        if info.estimated_required:
            assert sum(Decimal(p) for _, p in info.installments) == 100, code
            assert len(info.installments) == 4, code


def test_researched_exceptions():
    ca = [(d, p) for d, p in STATES["CA"].installments]
    assert [p for _, p in ca] == ["30", "40", "0", "30"]
    assert STATES["VA"].installments[0][0] == "05-01"
    assert STATES["DE"].installments[0][0] == "04-30"
    assert [d for d, _ in STATES["IA"].installments] == ["04-30", "06-30", "09-30", "01-31+1"]
    assert [d for d, _ in STATES["HI"].installments] == ["04-20", "06-20", "09-20", "01-20+1"]
    assert "confirm" in (STATES["MA"].caveat or "").lower()
    assert not STATES["GA"].verified and not STATES["AK"].verified and STATES["NY"].verified


# --- next_business_day --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 6, 20), date(2026, 6, 22)),  # Saturday
        (date(2026, 9, 20), date(2026, 9, 21)),  # Sunday
        (date(2027, 1, 31), date(2027, 2, 1)),  # Sunday
        (date(2026, 4, 15), date(2026, 4, 15)),  # Wednesday
        (date(2027, 1, 15), date(2027, 1, 15)),  # Friday
        (date(2028, 1, 15), date(2028, 1, 18)),  # Saturday, then Martin Luther King Jr. Day
        (date(2026, 6, 19), date(2026, 6, 22)),  # Juneteenth (Friday)
        (date(2026, 7, 4), date(2026, 7, 6)),  # Saturday
        (date(2027, 7, 4), date(2027, 7, 6)),  # Sunday, observed Monday
        (date(2026, 9, 7), date(2026, 9, 8)),  # Labor Day
    ],
)
def test_next_business_day(day, expected):
    assert next_business_day(day) == expected


def test_state_dates_roll_forward():
    hi = [d.isoformat() for d, _ in state_installments("HI", 2026)]
    assert hi == ["2026-04-20", "2026-06-22", "2026-09-21", "2027-01-20"]
    ia = [d.isoformat() for d, _ in state_installments("IA", 2026)]
    assert ia == ["2026-04-30", "2026-06-30", "2026-09-30", "2027-02-01"]
    assert [d.isoformat() for d, _ in state_installments("NY", 2026)] == [
        "2026-04-15",
        "2026-06-15",
        "2026-09-15",
        "2027-01-15",
    ]


def test_federal_due_dates_roll_forward_for_unlisted_years():
    # Jun 15 2025 was a Sunday: the IRS date was Monday Jun 16.
    assert tax.due_dates(2025) == ["2025-04-15", "2025-06-16", "2025-09-15", "2026-01-15"]
    assert tax.due_dates(2026) == ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"]


# --- config -------------------------------------------------------------------------------


def test_config_state_and_rate(cfg):
    c = with_config(cfg, '[tax]\nstate = "ms"\nstate_income_tax_rate_pct = 4.7\n')
    assert c.tax.state == "MS" and c.tax.state_income_tax_rate_pct == "4.7"
    assert load_config(cfg.home.parent / "other").tax.state is None


@pytest.mark.parametrize(
    "body",
    [
        'state = "ZZ"',
        "state = 12",
        'state = "Mississippi"',
        "state_income_tax_rate_pct = 21",
        "state_income_tax_rate_pct = -1",
    ],
)
def test_bad_state_config_is_rejected(cfg, body):
    with pytest.raises(ConfigError):
        with_config(cfg, f"[tax]\n{body}\n")


# --- estimated_tax state block ------------------------------------------------------------


def test_no_state_leaves_the_response_unchanged(conn, cfg, accounts):
    seed(conn, accounts)
    est = tax.estimated_tax(conn, cfg, year=2026, income_tax_rate_pct=22, today=TODAY)
    assert "state" not in est and "projected_total_with_state" not in est


def test_california_installments_use_the_state_percentages(conn, cfg, accounts):
    seed(conn, accounts)
    est = tax.estimated_tax(
        conn, cfg, year=2026, income_tax_rate_pct=22, state="CA", state_income_tax_rate_pct=6, today=TODAY
    )
    st = est["state"]
    assert st["code"] == "CA" and st["name"] == "California" and st["no_income_tax"] is False
    assert st["estimated_required"] is True and st["verified_for"] == 2026 and st["verified"] is True
    # Same base as the federal estimate: projected profit less half the SE tax.
    projected = est["projected_annual_net_profit"]["minor"]
    taxable = max(projected - est["self_employment_tax"]["minor"] // 2, 0)
    expected = int((Decimal(taxable) * Decimal(6) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    assert st["rate_pct"] == "6" and st["income_tax_estimate"]["minor"] == expected
    assert [(i["date"], i["pct"]) for i in st["installments"]] == [
        ("2026-04-15", "30"),
        ("2026-06-15", "40"),
        ("2026-09-15", "0"),
        ("2027-01-15", "30"),
    ]
    assert sum(i["amount"]["minor"] for i in st["installments"]) == expected
    assert st["installments"][2]["amount"]["minor"] == 0
    assert [i["passed"] for i in st["installments"]] == [True, True, True, False]  # TODAY is 2026-10-12
    assert est["projected_total_with_state"]["minor"] == est["projected_total"]["minor"] + expected
    quarters = est["suggested_per_quarter_with_state"]
    assert [q["state"]["minor"] for q in quarters] == [i["amount"]["minor"] for i in st["installments"]]
    assert all(q["total"]["minor"] == q["federal"]["minor"] + q["state"]["minor"] for q in quarters)


def test_state_rate_from_config_and_argument_override(conn, cfg, accounts):
    seed(conn, accounts)
    cfg = with_config(cfg, '[tax]\nstate = "NY"\nstate_income_tax_rate_pct = 5\n')
    a = tax.estimated_tax(conn, cfg, year=2026, today=TODAY)["state"]
    b = tax.estimated_tax(conn, cfg, year=2026, state_income_tax_rate_pct=10, state="ny", today=TODAY)["state"]
    assert a["code"] == "NY" and a["rate_pct"] == "5" and b["rate_pct"] == "10"
    assert a["income_tax_estimate"]["minor"] > 0
    assert abs(b["income_tax_estimate"]["minor"] - a["income_tax_estimate"]["minor"] * 2) <= 1


def test_state_without_a_rate_gives_dates_and_a_hint(conn, cfg, accounts):
    seed(conn, accounts)
    st = tax.estimated_tax(conn, cfg, year=2026, state="NY", today=TODAY)["state"]
    assert st["rate_pct"] is None and st["income_tax_estimate"] is None
    assert len(st["installments"]) == 4 and all(i["amount"] is None for i in st["installments"])
    assert any("state_income_tax_rate_pct" in n for n in st["notes"])


def test_texas_has_no_income_tax(conn, cfg, accounts):
    seed(conn, accounts)
    est = tax.estimated_tax(conn, cfg, year=2026, state="TX", state_income_tax_rate_pct=6, today=TODAY)
    st = est["state"]
    assert st["no_income_tax"] is True and st["income_tax_estimate"] is None and st["installments"] == []
    assert any("no broad personal income tax" in n for n in st["notes"])
    assert any("ignored" in n for n in st["notes"])  # the supplied rate
    assert est["projected_total_with_state"]["minor"] == est["projected_total"]["minor"]


def test_washington_and_new_hampshire_carry_their_caveats(conn, cfg, accounts):
    seed(conn, accounts)
    wa = tax.estimated_tax(conn, cfg, year=2026, state="WA", today=TODAY)["state"]
    nh = tax.estimated_tax(conn, cfg, year=2026, state="NH", today=TODAY)["state"]
    assert any("capital gains" in n.lower() for n in wa["notes"])
    assert any("repealed" in n.lower() for n in nh["notes"])


def test_utah_and_idaho_need_no_installments(conn, cfg, accounts):
    seed(conn, accounts)
    for code in ("UT", "ID"):
        st = tax.estimated_tax(conn, cfg, year=2026, state=code, state_income_tax_rate_pct=5, today=TODAY)["state"]
        assert st["no_income_tax"] is False and st["estimated_required"] is False
        assert st["installments"] == [] and st["income_tax_estimate"]["minor"] > 0
        assert any("no required quarterly installments" in n.lower() for n in st["notes"])


def test_hawaii_dates_roll_to_next_business_day(conn, cfg, accounts):
    seed(conn, accounts)
    st = tax.estimated_tax(conn, cfg, year=2026, state="HI", state_income_tax_rate_pct=5, today=TODAY)["state"]
    assert [i["date"] for i in st["installments"]] == ["2026-04-20", "2026-06-22", "2026-09-21", "2027-01-20"]
    assert any("next business day" in n for n in st["notes"])


def test_massachusetts_asks_to_confirm_and_unverified_states_say_so(conn, cfg, accounts):
    seed(conn, accounts)
    ma = tax.estimated_tax(conn, cfg, year=2026, state="MA", state_income_tax_rate_pct=5, today=TODAY)["state"]
    assert any("confirm" in n.lower() for n in ma["notes"])
    ga = tax.estimated_tax(conn, cfg, year=2026, state="GA", state_income_tax_rate_pct=5, today=TODAY)["state"]
    assert ga["verified"] is False and any("wasn't confirmed" in n for n in ga["notes"])
    ny = tax.estimated_tax(conn, cfg, year=2026, state="NY", state_income_tax_rate_pct=5, today=TODAY)["state"]
    assert ny["verified"] is True and not any("wasn't confirmed" in n for n in ny["notes"])


def test_year_other_than_verified_year_says_to_verify(conn, cfg, accounts):
    seed(conn, accounts)
    st = tax.estimated_tax(conn, cfg, year=2025, state="NY", state_income_tax_rate_pct=5, today=TODAY)["state"]
    assert st["installments"][0]["date"] == "2025-04-15" and st["installments"][1]["date"] == "2025-06-16"
    assert any("verify this year's dates" in n for n in st["notes"])


def test_state_notes_are_framed_as_planning_not_advice(conn, cfg, accounts):
    seed(conn, accounts)
    est = tax.estimated_tax(conn, cfg, year=2026, state="NY", state_income_tax_rate_pct=5, today=TODAY)
    assert tax.DISCLAIMER in est["notes"]
    assert any("define taxable income differently" in n for n in est["state"]["notes"])


@pytest.mark.parametrize("bad", ["ZZ", "", "USA", "N", "1", "New York"])
def test_unknown_state_is_invalid(conn, cfg, accounts, bad):
    with pytest.raises(GuardError) as exc:
        tax.estimated_tax(conn, cfg, year=2026, state=bad, today=TODAY)
    assert exc.value.code == "invalid"


@pytest.mark.parametrize("bad", [-1, 21, "abc", True])
def test_bad_state_rate_is_invalid(conn, cfg, accounts, bad):
    with pytest.raises(GuardError):
        tax.estimated_tax(conn, cfg, year=2026, state="NY", state_income_tax_rate_pct=bad, today=TODAY)


async def test_mcp_estimated_tax_takes_state_arguments(conn, cfg):
    from mcp import Client

    from fiscl.demo import load_demo
    from fiscl.server import build_server

    load_demo(conn, cfg, today=TODAY)
    conn.commit()
    async with Client(build_server(cfg)) as c:
        tool = {t.name: t for t in (await c.list_tools()).tools}["estimated_tax"]
        assert {"state", "state_income_tax_rate_pct"} <= set(tool.input_schema["properties"])
        r = await c.call_tool("estimated_tax", {"year": 2026, "state": "CA", "state_income_tax_rate_pct": 6})
        assert not r.is_error and r.structured_content["state"]["code"] == "CA"
        r = await c.call_tool("estimated_tax", {"year": 2026, "state": "ZZ"})
        assert r.is_error and "ZZ" not in r.content[0].text


def test_states_module_exposes_no_bracket_engine():
    assert not hasattr(states, "brackets")
