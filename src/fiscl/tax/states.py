"""State estimated-tax data for tax year 2026: who has an income tax, who requires installments, and when.

Data comes from docs/research/2026-10-11-state-estimates-1099.md. This is planning data, not a tax engine: no
brackets, credits, local taxes, or residency rules. Each row carries a `verified` flag (False where the research
found only older or secondary evidence), and `VERIFIED_FOR` is the tax year the table was checked against.

Installment dates are stored as "MM-DD" (plus "+1" for the following calendar year) and rolled to the next business
day when emitted, so Hawaii's Jun 20 2026 (a Saturday) becomes Jun 22 and Iowa's Jan 31 2027 becomes Feb 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

VERIFIED_FOR = 2026

Installments = tuple[tuple[str, str], ...]  # (date, percent of the annual estimate)

FEDERAL: Installments = (("04-15", "25"), ("06-15", "25"), ("09-15", "25"), ("01-15+1", "25"))
CALIFORNIA: Installments = (("04-15", "30"), ("06-15", "40"), ("09-15", "0"), ("01-15+1", "30"))
HAWAII: Installments = (("04-20", "25"), ("06-20", "25"), ("09-20", "25"), ("01-20+1", "25"))
IOWA: Installments = (("04-30", "25"), ("06-30", "25"), ("09-30", "25"), ("01-31+1", "25"))
DELAWARE: Installments = (("04-30", "25"), ("06-15", "25"), ("09-15", "25"), ("01-15+1", "25"))
VIRGINIA: Installments = (("05-01", "25"), ("06-15", "25"), ("09-15", "25"), ("01-15+1", "25"))


@dataclass(frozen=True)
class StateInfo:
    name: str
    income_tax: bool  # a broad personal income tax on wages and self-employment income
    estimated_required: bool  # individuals must pay quarterly installments (False for UT, ID and no-tax states)
    installments: Installments
    source: str  # the state revenue page or form the row was taken from
    verified: bool  # confirmed against a 2026 official source (see the research doc)
    caveat: str | None = None


def _taxed(
    name: str,
    source: str,
    installments: Installments = FEDERAL,
    verified: bool = True,
    caveat: str | None = None,
) -> StateInfo:
    return StateInfo(name, True, True, installments, source, verified, caveat)


def _optional(name: str, source: str, caveat: str) -> StateInfo:
    """Taxes income but does not require quarterly estimates (Utah, Idaho)."""
    return StateInfo(name, True, False, (), source, True, caveat)


def _untaxed(name: str, source: str, verified: bool = True, caveat: str | None = None) -> StateInfo:
    return StateInfo(name, False, False, (), source, verified, caveat)


STATES: dict[str, StateInfo] = {
    "AL": _taxed(
        "Alabama",
        "https://www.revenue.alabama.gov/wp-content/uploads/2026/01/26f40esinstr.pdf",
    ),
    "AK": _untaxed(
        "Alaska",
        "https://tax.alaska.gov/",
        verified=False,
    ),
    "AZ": _taxed(
        "Arizona",
        "https://azdor.gov/sites/default/files/document/FORMS_INDIVIDUAL_2026_140ES_f.pdf",
    ),
    "AR": _taxed(
        "Arkansas",
        "https://www.dfa.arkansas.gov/office/taxes/income-tax-administration/individual-income-tax/forms/",
        verified=False,
    ),
    "CA": _taxed(
        "California",
        "https://www.ftb.ca.gov/pay/estimated-tax-payments.html",
        installments=CALIFORNIA,
        caveat="California splits the year 30% / 40% / 0% / 30%; there is no September payment.",
    ),
    "CO": _taxed(
        "Colorado",
        "https://tax.colorado.gov/sites/default/files/documents/DR_0104EP_2026.pdf",
    ),
    "CT": _taxed(
        "Connecticut",
        # Reference data (a printed-form link shown to the user), not a request target. The forbidden-endpoint source
        # scans in tests/ exempt this exact string via conftest.ALLOWED_REFERENCE_URLS; change it there too.
        "https://portal.ct.gov/-/media/drs/forms/2025/income/ct1040es-flat0126.pdf",
    ),
    "DE": _taxed(
        "Delaware",
        "https://revenuefiles.delaware.gov/2025/PITForms_Instructions/Instructions/PIT-EST_Instructions_2026-01.pdf",
        installments=DELAWARE,
        caveat="Delaware's first installment is due April 30, not April 15.",
    ),
    "DC": _taxed(
        "District of Columbia",
        "https://otr.cfo.dc.gov/sites/default/files/dc/sites/otr/publication/attachments/"
        "2026_D40ES_Book_wLinks04012026.pdf",
    ),
    "FL": _untaxed(
        "Florida",
        "https://floridarevenue.com/",
        verified=False,
    ),
    "GA": _taxed(
        "Georgia",
        "https://dor.georgia.gov/500-es-individual-and-fiduciary-estimated-tax-payment-voucher",
        verified=False,
    ),
    "HI": _taxed(
        "Hawaii",
        "https://files.hawaii.gov/tax/legal/taxfacts/tf2019-3.pdf",
        installments=HAWAII,
        caveat="Hawaii's installments are due on the 20th of April, June, September and January.",
    ),
    "ID": _optional(
        "Idaho",
        "https://tax.idaho.gov/taxes/income-tax/individual-income/filing-and-paying/",
        "Idaho does not require individual quarterly installments; you may prepay any time (Form 51) until the "
        "return due date. Business Form 41ES has its own dates.",
    ),
    "IL": _taxed(
        "Illinois",
        "https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/"
        "il-1040-es.pdf",
        verified=False,
    ),
    "IN": _taxed(
        "Indiana",
        "https://www.in.gov/dor/i-need-to/make-a-payment/estimated-payments/",
    ),
    "IA": _taxed(
        "Iowa",
        "https://tax.iowa.gov/estimated-income-tax-payments",
        installments=IOWA,
        caveat="Iowa's installments are due April 30, June 30, September 30 and January 31.",
    ),
    "KS": _taxed(
        "Kansas",
        "https://www.ksrevenue.gov/pdf/k-40es26.pdf",
    ),
    "KY": _taxed(
        "Kentucky",
        "https://revenue.ky.gov/Forms/740es%202026.pdf",
    ),
    "LA": _taxed(
        "Louisiana",
        "https://revenue.louisiana.gov/individuals/general-resources/declaration-of-estimated-income-taxes/",
        verified=False,
    ),
    "ME": _taxed(
        "Maine",
        "https://www.maine.gov/revenue/sites/maine.gov.revenue/files/inline-files/26_1040es_fillable.pdf",
    ),
    "MD": _taxed(
        "Maryland",
        "https://www.marylandtaxes.gov/forms/Personal_Tax_Tips/tip54.pdf",
        verified=False,
    ),
    "MA": _taxed(
        "Massachusetts",
        "https://www.mass.gov/doc/2026-form-1-es-estimated-tax-payment-vouchers-instructions-and-worksheets/download",
        caveat=(
            "Two Massachusetts DOR pages disagree on the second 2026 installment (June 15 on Form 1-ES, June 16 on "
            "the due-dates page). Confirm the date with MassTaxConnect or your state."
        ),
    ),
    "MI": _taxed(
        "Michigan",
        "https://www.michigan.gov/taxes/-/media/Project/Websites/taxes/Forms/IIT/TY2025/MI-1040ES-(2026).pdf",
    ),
    "MN": _taxed(
        "Minnesota",
        "https://www.revenue.state.mn.us/estimated-tax-payments",
    ),
    "MS": _taxed(
        "Mississippi",
        "https://www.dor.ms.gov/individual/individual-income-tax-frequently-asked-questions",
        verified=False,
        caveat=(
            "Mississippi's rule is that estimated payments must total at least 80% of the annual liability rather "
            "than a set percent per installment; equal installments are shown here."
        ),
    ),
    "MO": _taxed(
        "Missouri",
        "https://dor.mo.gov/forms/MO-1040ES_2026.pdf",
    ),
    "MT": _taxed(
        "Montana",
        "https://revenue.mt.gov/taxes/estimated-tax-payments",
    ),
    "NE": _taxed(
        "Nebraska",
        "https://revenue.nebraska.gov/individuals/instructions-paying-your-estimated-individual-income-tax",
    ),
    "NV": _untaxed(
        "Nevada",
        "https://tax.nv.gov/about-nevada-department-of-taxation/income-tax-in-nevada/",
    ),
    "NH": _untaxed(
        "New Hampshire",
        "https://www.revenue.nh.gov/news-and-media/repeal-nh-interest-and-dividends-tax-now-effect",
        caveat=(
            "New Hampshire's interest and dividends tax was repealed for taxable periods beginning after "
            "December 31, 2024, so there is no 2026 personal liability. Business taxes still exist."
        ),
    ),
    "NJ": _taxed(
        "New Jersey",
        "https://www.nj.gov/treasury/taxation/pdf/current/1040esi.pdf",
    ),
    "NM": _taxed(
        "New Mexico",
        "https://www.tax.newmexico.gov/individuals/file-your-taxes-overview/estimated-payments/",
        verified=False,
    ),
    "NY": _taxed(
        "New York",
        "https://www.tax.ny.gov/pit/estimated_tax/estimated_tax_payment_due_dates.htm",
    ),
    "NC": _taxed(
        "North Carolina",
        "https://www.ncdor.gov/taxes-forms/individual-income-tax/estimated-income-tax",
        verified=False,
    ),
    "ND": _taxed(
        "North Dakota",
        "https://www.tax.nd.gov/sites/www/files/documents/forms/software-developer/individual-income-forms/"
        "28709-form-nd-1es-2026%20final.pdf",
    ),
    "OH": _taxed(
        "Ohio",
        "https://tax.ohio.gov/individual/resources/estimated-payments",
        verified=False,
    ),
    "OK": _taxed(
        "Oklahoma",
        "https://oklahoma.gov/content/dam/ok/en/tax/documents/forms/software-developers/DevOW-8-ES-26.pdf",
    ),
    "OR": _taxed(
        "Oregon",
        "https://www.oregon.gov/dor/forms/FormsPubs/publication-or-estimate_101-026_2026.pdf",
    ),
    "PA": _taxed(
        "Pennsylvania",
        "https://www.pa.gov/content/dam/copapwp-pagov/en/revenue/documents/formsandpublications/formsforindividuals/"
        "pit/documents/2026/2026_rev-413i.pdf",
    ),
    "RI": _taxed(
        "Rhode Island",
        "https://tax.ri.gov/sites/g/files/xkgbur541/files/2026-01/2026%20RI-1040ES_w.pdf",
    ),
    "SC": _taxed(
        "South Carolina",
        "https://dor.sc.gov/sites/dor/files/forms/SC1040ES_2026.pdf",
    ),
    "SD": _untaxed(
        "South Dakota",
        "https://dor.sd.gov/individuals/taxes/",
    ),
    "TN": _untaxed(
        "Tennessee",
        "https://www.tn.gov/revenue/taxes/hall-income-tax.html",
        caveat="Tennessee's Hall income tax was repealed for years beginning on or after January 1, 2021.",
    ),
    "TX": _untaxed(
        "Texas",
        "https://comptroller.texas.gov/taxes/",
        caveat="Sole proprietors are exempt from the Texas franchise tax.",
    ),
    "UT": _optional(
        "Utah",
        "https://tax.utah.gov/forms/current/tc-546.pdf",
        "Utah does not require individual quarterly installments; you may prepay any time (Form TC-546), and "
        "prepayment requirements must be met by the April return due date.",
    ),
    "VT": _taxed(
        "Vermont",
        "https://tax.vermont.gov/individuals/estimated-income-tax",
    ),
    "VA": _taxed(
        "Virginia",
        "https://www.tax.virginia.gov/sites/default/files/taxforms/individual-income-tax/2026/760es-2026.pdf",
        installments=VIRGINIA,
        caveat="Virginia's first installment is due May 1, not April 15.",
    ),
    "WA": _untaxed(
        "Washington",
        "https://dor.wa.gov/taxes-rates/other-taxes/capital-gains-tax",
        verified=False,
        caveat=(
            "Washington has no broad personal income tax, but its capital gains excise tax (7% up to $1 million of "
            "gains, 9.9% above) applies to long-term gains, not to wages or self-employment income."
        ),
    ),
    "WV": _taxed(
        "West Virginia",
        "https://tax.wv.gov/Documents/TaxForms/it140esi.pdf",
        verified=False,
    ),
    "WI": _taxed(
        "Wisconsin",
        "https://www.revenue.wi.gov/TaxForms2026/2026-Form1-ES-Inst.pdf",
    ),
    "WY": _untaxed(
        "Wyoming",
        "https://revenue.wyo.gov/",
        verified=False,
    ),
}


# --- business-day rolling -----------------------------------------------------------------


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def federal_holidays(year: int) -> set[date]:
    """US federal legal holidays in `year`. A Sunday holiday also closes the Monday after it."""
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    days = {d + timedelta(days=1) if d.weekday() == 6 else d for d in fixed}
    days |= {d for d in fixed if d.weekday() != 5}  # a Saturday holiday needs no extra closure; weekends roll anyway
    days |= {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    }
    return days


def dc_emancipation_day(year: int) -> date:
    """April 16, observed Friday when it falls on a Saturday and Monday when it falls on a Sunday."""
    day = date(year, 4, 16)
    return day - timedelta(days=1) if day.weekday() == 5 else day + timedelta(days=1) if day.weekday() == 6 else day


def next_business_day(day: date, *, dc: bool = False) -> date:
    """`day`, or the next weekday that is not a US federal holiday.

    `dc=True` also treats DC Emancipation Day as a holiday. IRC 7503 moves a deadline that falls on a legal holiday
    in the District, so federal April dates (and DC's own dates) roll past it; other states' dates must not.
    Other state-only holidays (for example Patriot's Day in Massachusetts) are not known here.
    """
    while day.weekday() >= 5 or day in federal_holidays(day.year) or (dc and day == dc_emancipation_day(day.year)):
        day += timedelta(days=1)
    return day


def installment_date(token: str, year: int, *, dc: bool = False) -> date:
    """Turn a table date like "06-15" or "01-15+1" into a real, business-day-adjusted date for tax year `year`."""
    plus = token.endswith("+1")
    month, dom = (int(x) for x in token.removesuffix("+1").split("-"))
    return next_business_day(date(year + plus, month, dom), dc=dc)


def state_installments(code: str, year: int) -> list[tuple[date, str]]:
    """The state's (date, percent) installments for tax year `year`, rolled to business days."""
    return [(installment_date(token, year, dc=code == "DC"), pct) for token, pct in STATES[code].installments]
