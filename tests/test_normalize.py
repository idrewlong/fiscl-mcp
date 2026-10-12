import pytest

from fiscl.normalize import normalize_merchant


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # existing behavior
        ("SQ *BLUE BOTTLE COFFEE", "Blue Bottle Coffee"),
        ("TST* THE CHIMNEY", "The Chimney"),
        ("KROGER #0412 GULFPORT MS", "Kroger Gulfport"),
        ("NETFLIX.COM", "Netflix.com"),
        ("PURCHASE AUTHORIZED ON 09/14 SHELL OIL 57444", "Shell Oil"),
        ("B&H PHOTO 800-606-6969 NY", "B&H Photo"),
        # trailing store numbers without '#'
        ("CHIPOTLE 2291", "Chipotle"),
        ("WAFFLE HOUSE 1123", "Waffle House"),
        # short trailing numbers are part of the name
        ("FOREVER 21", "Forever 21"),
        ("MOTEL 6", "Motel 6"),
        # ACH credits/deposits lose the prefix; legal suffixes keep their case
        ("ACH CREDIT MAGNOLIA MEDIA LLC", "Magnolia Media LLC"),
        ("ACH DEPOSIT GUSTO PAYROLL", "Gusto Payroll"),
    ],
)
def test_normalize_merchant(raw, expected):
    assert normalize_merchant(raw) == expected
