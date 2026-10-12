import pytest

from fiscl.guard import GuardError, clean_text, date_range, redact, redact_text, safe_regex
from fiscl.money import AmountError, amount, fmt, parse_amount, pct, to_minor


@pytest.mark.parametrize(
    "text,expected",
    [
        ("-45.23", -4523),
        ("(45.23)", -4523),
        ("45.23 CR", 4523),
        ("45.23 DR", -4523),
        ("$1,234.56", 123456),
        ("1.234,56", 123456),
        ("12,34", 1234),
        ("1,234", 123400),
        ("+12", 1200),
        ("45.23-", -4523),
        ("0.005", 0),  # banker's rounding
        ("0.015", 2),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3", "--", "."])
def test_parse_amount_rejects(bad):
    with pytest.raises(AmountError):
        parse_amount(bad)


def test_format_and_helpers():
    assert fmt(-123456) == "-$1,234.56"
    assert amount(500) == {"minor": 500, "display": "$5.00"}
    assert to_minor(400) == 40000 and to_minor("19.99") == 1999
    assert pct(1, 3) == "33.3" and pct(5, 0) is None


def test_redaction_masks_cards_ssn_and_account_numbers():
    text = "Card 4111 1111 1111 1111 ref 123-45-6789 acct 000123456789"
    out = redact_text(text)
    assert "4111 1111 1111 1111" not in out and "••••1111" in out
    assert "123-45-6789" not in out
    assert "000123456789" not in out and "••••6789" in out


def test_redaction_keeps_non_luhn_dates_and_amounts():
    assert redact_text("2026-10-12 $1,234.56") == "2026-10-12 $1,234.56"
    nested = redact({"a": ["acct 9876543210123"], "n": 5})
    assert nested["n"] == 5 and "9876543210123" not in nested["a"][0]


def test_clean_text_strips_controls_and_bidi():
    assert clean_text("A\x00B‮C\n  D") == "A B C D"
    assert len(clean_text("x" * 1000)) == 256


def test_date_range_validation():
    with pytest.raises(GuardError):
        date_range("2026-02-01", "2026-01-01")
    with pytest.raises(GuardError):
        date_range("2000-01-01", "2026-01-01")
    with pytest.raises(GuardError):
        date_range("01/02/2026", None)


def test_safe_regex_rejects_catastrophic_patterns():
    with pytest.raises(GuardError):
        safe_regex("(a+)+$")
    with pytest.raises(GuardError):
        safe_regex("x" * 300)
    assert safe_regex("netflix|hulu").search("NETFLIX.COM")
