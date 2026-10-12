"""Fix pass: rule-named businesses are protected, DC Emancipation Day moves federal dates, QIF two-digit year pivot."""

from __future__ import annotations

import json
from datetime import date

import pytest

from fiscl import cli, tax
from fiscl.guard import GuardError
from fiscl.importers import qif
from fiscl.tax.states import next_business_day, state_installments

RULE = '[[rule]]\nmerchant = "Zoom"\ncategory = "Business: Software"\nbusiness = "{}"\n'
FIXED = "rules.toml rule 2 names this business; change the rule first"


def _rules(cfg, name: str) -> None:
    cfg.rules_file.write_text(
        '[[rule]]\nmerchant = "Other"\ncategory = "X"\n' + RULE.format(name),
        encoding="utf-8",
    )


@pytest.fixture
def consulting(conn):
    conn.execute("INSERT INTO businesses (name, created_at) VALUES ('Consulting', '2026-01-01')")
    conn.commit()


# --- rules.toml protects the businesses it names ------------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_rename_refused_when_a_rule_names_the_business(conn, cfg, consulting, dry_run):
    _rules(cfg, "CONSULTING")
    with pytest.raises(GuardError) as e:
        tax.manage_business(conn, cfg, action="rename", name="consulting", new_name="Advisory", dry_run=dry_run)
    assert e.value.code == "invalid" and str(e.value) == FIXED
    assert "onsulting" not in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM businesses WHERE name = 'Consulting'").fetchone()[0] == 1


@pytest.mark.parametrize("dry_run", [True, False])
def test_archive_refused_when_a_rule_names_the_business(conn, cfg, consulting, dry_run):
    _rules(cfg, "consulting")
    with pytest.raises(GuardError) as e:
        tax.manage_business(conn, cfg, action="archive", name="Consulting", dry_run=dry_run)
    assert e.value.code == "invalid" and str(e.value) == FIXED
    assert conn.execute("SELECT archived FROM businesses WHERE name = 'Consulting'").fetchone()[0] == 0


def test_rename_and_archive_allowed_when_no_rule_names_the_business(conn, cfg, consulting):
    _rules(cfg, "Studio")
    tax.manage_business(conn, cfg, action="rename", name="Consulting", new_name="Advisory", dry_run=False)
    tax.manage_business(conn, cfg, action="archive", name="Advisory", dry_run=False)
    assert conn.execute("SELECT archived FROM businesses WHERE name = 'Advisory'").fetchone()[0] == 1


def test_doctor_reports_rule_businesses_that_are_unknown_or_archived(conn, cfg, consulting, capsys):
    _rules(cfg, "Nonexistent Corp")
    capsys.readouterr()
    assert cli.main(["--home", str(cfg.home), "doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert "rules.toml rule 2: business isn't an active business" in report["problems"]
    assert "Nonexistent" not in json.dumps(report["problems"])

    _rules(cfg, "Consulting")
    conn.execute("UPDATE businesses SET archived = 1 WHERE name = 'Consulting'")
    conn.commit()
    capsys.readouterr()
    assert cli.main(["--home", str(cfg.home), "doctor"]) == 1
    assert "rules.toml rule 2: business isn't an active business" in json.loads(capsys.readouterr().out)["problems"]


def test_doctor_is_clean_when_rule_businesses_are_active(conn, cfg, consulting, capsys):
    _rules(cfg, "consulting")
    capsys.readouterr()
    assert cli.main(["--home", str(cfg.home), "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []


# --- DC Emancipation Day (IRC 7503) -------------------------------------------------------------


def test_dc_emancipation_day_moves_the_2028_federal_april_deadline():
    # Apr 15 2028 is a Saturday; Apr 16 is a Sunday, observed Monday Apr 17 -> Tuesday Apr 18.
    assert tax.due_dates(2028)[0] == "2028-04-18"


def test_2026_federal_dates_are_unchanged():
    assert tax.due_dates(2026) == ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2028, 4, 17), date(2028, 4, 18)),  # Sunday Apr 16 observed Monday
        (date(2029, 4, 16), date(2029, 4, 17)),  # Monday, the day itself
        (date(2027, 4, 16), date(2027, 4, 19)),  # Friday, the day itself
        (date(2033, 4, 15), date(2033, 4, 18)),  # Apr 16 2033 is a Saturday, observed Friday Apr 15
    ],
)
def test_dc_holiday_rolls_federal_dates(day, expected):
    assert next_business_day(day, dc=True) == expected


def test_dc_holiday_is_not_applied_to_other_states():
    assert next_business_day(date(2028, 4, 17)) == date(2028, 4, 17)
    assert next_business_day(date(2028, 4, 17), dc=False) == date(2028, 4, 17)
    assert state_installments("DC", 2028)[0][0] == date(2028, 4, 18)
    assert state_installments("NY", 2028)[0][0] == date(2028, 4, 17)


# --- QIF two-digit year pivot ---------------------------------------------------------------------

NOW = date.today().year


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("99", 1999),
        ("50", 1950),
        (f"{NOW % 100:02d}", NOW),
        (f"{(NOW + 1) % 100:02d}", NOW + 1),  # one year ahead still reads as the future
        (f"{(NOW + 2) % 100:02d}", NOW + 2 - 100),  # two ahead reads as the past century
        ("2026", 2026),
    ],
)
def test_two_digit_year_pivot(raw, expected):
    assert qif._year(raw) == expected


def test_pivot_with_a_pinned_year():
    assert qif._year("26", now_year=2026) == 2026
    assert qif._year("27", now_year=2026) == 2027
    assert qif._year("28", now_year=2026) == 1928
    assert qif._year("99", now_year=2026) == 1999
    assert qif._year("50", now_year=2026) == 1950
    assert qif._year("00", now_year=2026) == 2000
