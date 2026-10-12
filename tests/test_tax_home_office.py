"""Phase 3 slice A: simplified home office method (Schedule C line 30) and its effect on estimated tax."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from fiscl import tax
from fiscl.config import ConfigError, load_config

from .conftest import TODAY, add_txn


def with_config(cfg, text: str):
    cfg.config_file.write_text(text, encoding="utf-8")
    return load_config(cfg.home)


def seed(conn, accounts, income: int, spend: int = 0, year: int = 2026):
    add_txn(conn, accounts["biz"], f"{year}-03-01", income, "Client payment")
    if spend:
        add_txn(conn, accounts["biz"], f"{year}-03-02", -spend, "Staples", category="Shopping")
    conn.commit()


def test_full_year_200_sqft_is_1000_dollars(conn, cfg, accounts):
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 200\n")
    seed(conn, accounts, 400_000)
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    ho = d["home_office"]
    assert ho["method"] == "simplified"
    assert ho["square_feet"] == 200 and ho["months_used"] == 12
    assert ho["allowable_square_feet"] == "200.0"
    assert ho["rate_per_sqft"]["minor"] == 500
    assert ho["computed"]["minor"] == 100_000
    assert ho["line_30"]["display"] == "$1,000.00"
    assert ho["limited_to_profit"] is False
    assert d["tentative_profit_before_home_office"]["minor"] == 400_000
    assert d["net_profit"]["minor"] == 300_000
    assert d["tentative_net_profit"] == d["net_profit"]
    assert any("simplified method only" in n for n in d["notes"])


def test_six_months_halves_the_deduction(conn, cfg, accounts):
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 200\nmonths_used = 6\n")
    seed(conn, accounts, 400_000)
    ho = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["home_office"]
    assert ho["allowable_square_feet"] == "100.0"
    assert ho["line_30"]["display"] == "$500.00"


def test_limited_to_profit_with_note(conn, cfg, accounts):
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 200\n")
    seed(conn, accounts, 140_000, spend=100_000)  # profit before home office: $400
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    ho = d["home_office"]
    assert ho["computed"]["minor"] == 100_000
    assert ho["line_30"]["minor"] == 40_000
    assert ho["limited_to_profit"] is True
    assert d["net_profit"]["minor"] == 0
    assert any("can't exceed" in n and "does not carry over" in n for n in d["notes"])


def test_loss_year_deducts_nothing(conn, cfg, accounts):
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 100\n")
    seed(conn, accounts, 10_000, spend=50_000)
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    assert d["home_office"]["line_30"]["minor"] == 0
    assert d["net_profit"]["minor"] == -40_000


def test_not_configured_adds_hint_and_no_block(conn, cfg, accounts):
    seed(conn, accounts, 400_000)
    d = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)
    assert d["home_office"] is None
    assert d["net_profit"] == d["tentative_profit_before_home_office"]
    assert any("[tax.home_office.2026]" in n for n in d["notes"])


def test_other_year_config_does_not_apply(conn, cfg, accounts):
    cfg = with_config(cfg, "[tax.home_office.2025]\nsquare_feet = 200\n")
    seed(conn, accounts, 400_000)
    assert tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["home_office"] is None


@pytest.mark.parametrize(
    "body",
    [
        "square_feet = 301",
        "square_feet = 0",
        "square_feet = 200\nmonths_used = 13",
        "square_feet = 200\nmonths_used = 0",
        'square_feet = "200"',
        "square_feet = true",
        "square_feet = 200\nbogus = 1",
        "",
    ],
)
def test_bad_home_office_config_is_rejected(cfg, body):
    with pytest.raises(ConfigError):
        with_config(cfg, f"[tax.home_office.2026]\n{body}\n")


def test_bad_home_office_year_is_rejected(cfg):
    with pytest.raises(ConfigError):
        with_config(cfg, "[tax.home_office.1850]\nsquare_feet = 100\n")
    with pytest.raises(ConfigError):
        with_config(cfg, "[tax.home_office.abcd]\nsquare_feet = 100\n")


def test_partial_month_rounding_is_one_decimal_half_even(conn, cfg, accounts):
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 155\nmonths_used = 7\n")
    seed(conn, accounts, 400_000)
    ho = tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY)["home_office"]
    assert ho["allowable_square_feet"] == "90.4"  # 155 * 7 / 12 = 90.41666...
    assert ho["computed"]["minor"] == 45_200


def test_estimated_tax_uses_profit_after_line_30(conn, cfg, accounts):
    cfg = with_config(cfg, "[tax.home_office.2025]\nsquare_feet = 200\n")
    seed(conn, accounts, 1_000_000, year=2025)  # $10,000 gross, $1,000 home office -> $9,000 profit
    est = tax.estimated_tax(conn, cfg, year=2025, today=TODAY)
    assert est["ytd_net_profit"]["minor"] == 900_000
    net_earnings = int(Decimal(900_000) * Decimal("0.9235"))  # 831_150
    expected = int((Decimal(net_earnings) * Decimal("0.153")).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    assert expected == 127_166  # 831,150 x 15.3% = 127,165.95
    assert est["self_employment_tax"]["minor"] == expected


def test_estimated_tax_current_year_deducts_home_office_once_not_annualized(conn, cfg, accounts):
    cfg = with_config(cfg, "[tax.home_office.2026]\nsquare_feet = 200\n")
    seed(conn, accounts, 1_000_000, year=2026)
    est = tax.estimated_tax(conn, cfg, year=2026, today=TODAY)
    days = 365
    elapsed = (TODAY - TODAY.replace(month=1, day=1)).days + 1
    projected_before = 1_000_000 * days // elapsed
    assert est["projected_annual_net_profit"]["minor"] == projected_before - 100_000
