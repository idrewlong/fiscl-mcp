# State estimated-tax dates (TY2026), federal 1099 thresholds, 1040-ES dates, simplified home office
Date: 2026-10-11 · Author: my-skills:researcher · Requested by: orchestrator (Phase 3 slices B and C; spec risks R3, R4, R6) · Status: final (state table has PARTIAL/UNVERIFIED rows, listed below)

## Answer (TL;DR)
- **Federal (high confidence, IRS primary sources):** 1040-ES dates for TY2026 are Apr 15, Jun 15, Sep 15 2026 and Jan 15 2027 (none fall on a weekend or holiday). 1099-NEC and 1099-MISC thresholds are $600 for payments before 2026 and **$2,000 for payments made in 2026**, indexed for inflation from 2027. 1099-K (third-party settlement organizations) is **>$20,000 AND >200 transactions**, retroactively reinstated by OBBBA. Simplified home office is unchanged: $5/sq ft, 300 sq ft cap, gross-income limit, no carryover.
- **States (medium-high confidence):** 9 states have no broad personal income tax on wages or self-employment income: AK, FL, NV, NH, SD, TN, TX, WA, WY. The spec's list is right. Of the 42 jurisdictions (41 states + DC) that tax income, most follow the federal 4/15, 6/15, 9/15, 1/15 dates at 25% each. **Confirmed exceptions:** CA is 30/40/0/30. VA starts May 1. IA is 4/30, 6/30, 9/30, 1/31. DE starts Apr 30. HI uses the 20th (4/20, 6/20, 9/20, 1/20). **UT and ID do not require individual quarterly estimates at all.**
- **Contradictions with the spec:** see "Spec contradictions and required changes" below. The two that matter most are (1) UT and ID need a "no required installments" representation, and (2) "weekend/holiday shifts are not computed" is wrong for HI (Jun 22 and Sep 21, 2026) and IA (Feb 1, 2027).

## How to read the status column
- **VERIFIED**: an official state revenue/tax-department page or 2026 form was identified and the 2026 dates were taken from it. Most reads were done through search-tool excerpts of the official page or PDF, not by opening every page in full. Only HI (TF 2019-3) and the federal 1040-ES and Pub 1099 PDFs were read end to end by me.
- **PARTIAL**: an official source was found, but it is not 2026-specific (older form, FAQ, or "tax year 2025" text) or I could not see the Jan 2027 date. Dates are the standard federal pattern; confirm before shipping `VERIFIED_FOR`.
- **UNVERIFIED**: no official statement was retrieved.
No state page was inspected for a Jan 2027 holiday shift beyond what is noted. No 51-entry aggregate from a reliable single source exists (searched; only per-state pages and tax-prep blogs). No secondary aggregate was used for any table value.

## Findings

### 1. Federal 1040-ES, TY2026
- 2026 Form 1040-ES (rev. Feb 12, 2026) lists installments due Apr 15, Jun 15, Sep 15 2026 and Jan 15 2027. The Jan 15 payment can be skipped if the 2026 return is filed by Feb 1, 2027 with full payment. (Read from the downloaded PDF: https://www.irs.gov/pub/irs-pdf/f1040es.pdf)
- Weekday check: Apr 15 2026 is Wed, Jun 15 Mon, Sep 15 Tue, Jan 15 2027 Fri. No shifts needed. The IRS rule (Estimated Taxes page) is that a date on a weekend or legal holiday moves to the next business day.
- 2027 dates: not found. Form 1040-ES for TY2027 is not published as of today. Statutory pattern would be Apr 15, Jun 15, Sep 15 2027 and Jan 18 2028 (Jan 15 2028 is a Saturday, Jan 17 is Martin Luther King Jr. Day). That last date is my calculation, not an IRS statement. Mark unverified.

### 2. Federal information-return thresholds (IRS Pub 1099 (2026), Mar 19 2026, and IRS web pages)
| Form | TY2024 payments | TY2025 payments | TY2026 payments | Later |
|---|---|---|---|---|
| 1099-NEC (box 1) | $600 | $600 | **$2,000** | Inflation-adjusted beginning calendar year 2027 |
| 1099-MISC (rents, other income, prizes, medical, etc.) | $600 | $600 | **$2,000** | Same indexing from 2027 |
| 1099-MISC royalties | $10 | $10 | $10 (unchanged) | |
| 1099-MISC direct sales of consumer goods for resale | $5,000 | $5,000 | $5,000 | |
| 1099-K, third-party settlement organizations (PayPal, Venmo business, Etsy, etc.) | >$20,000 and >200 txns | >$20,000 and >200 txns | >$20,000 and >200 txns | |
| 1099-K, payment cards (merchant acquirers) | no threshold | no threshold | no threshold | |

- Quote, Pub 1099 (2026) p.1: "For tax years beginning after 2025, the minimum threshold amount for reporting certain payments ... increased to $2,000 and will be adjusted for inflation beginning in calendar year 2027. Previously, the threshold amount was $600." The Guide to Information Returns table shows "$2,000 or more" for 1099-NEC and rents/other income in MISC.
- IRS "Am I required to file a Form 1099" page: "For payments made before 2026, the reporting threshold is $600." "For payments made in 2026, the reporting threshold is $2,000." "For payments made after 2026, see Pub. 1099 for the inflation-adjusted reporting threshold." https://www.irs.gov/businesses/small-businesses-self-employed/am-i-required-to-file-a-form-1099-or-other-information-return
- 1099-K: Pub 1099 (2026) says TPSOs report "only if, in a calendar year, both of the following are met. 1. Over $20,000 in total amount of payments. 2. Over 200 in total number of transactions." IRS FAQ and newsroom: OBBBA "retroactively reinstated the reporting threshold in effect prior to the passage of ARPA". So the $5,000 (2024) and $2,500 (2025) phase-in steps never took effect as law. (The $5,000 and $2,500 figures are from my background knowledge of Notice 2024-85, not re-fetched; the IRS FAQ page I read did not mention them. Treat as context only.) Payment-card transactions: "There is no threshold amount that must be met." https://www.irs.gov/newsroom/form-1099-k-faqs-general-information
- Issuing below threshold is allowed but not required. IRS: a TPSO "may still send a Form 1099-K for payments for goods or services for amounts lower than the thresholds"; states may set a lower TPSO threshold; income is taxable regardless of whether a form arrives. Nothing in the IRS pages requires a payer to issue a 1099-NEC/MISC below the threshold, and the threshold is the minimum that triggers the filing duty. A payer who paid a contractor under $2,000 in 2026 is not required to file, though the contractor must still report the income.
- Tax-year versus payment-year wording: the NEC/MISC change is keyed to "payments made in 2026" (Pub 1099 phrases it "tax years beginning after 2025"). Form for 2026 payments is furnished by Feb 1, 2027 (not researched; due dates not asked).
- Implication for `tax/forms1099.py`: the threshold table needs `{year: {nec: 600|2000, misc: 600|2000, k_amount: 20000, k_txns: 200}}` with 2027+ marked "inflation-indexed, not yet published". `unreported_payers` should use the threshold for the transaction year, not the current year.

### 3. Simplified home office method
- IRS Pub 587 (for use in preparing 2025 returns; only the first 100,000 of 207,540 characters were read): the deduction is $5 times the area used, "The area you use to figure your deduction is limited to 300 square feet"; the deduction is limited to the gross income from the business use of the home, minus business expenses unrelated to the home; if those exceed gross income, no deduction; carryovers of disallowed amounts from prior actual-expense years cannot be deducted in a simplified-method year. https://www.irs.gov/publications/p587
- IRS "Simplified option for home office deduction" page (last reviewed 02-Mar-2026): $5 per sq ft, max 300 sq ft (max $1,500), excess over gross-income limit not carried over; no mention of any 2025 or 2026 change. https://www.irs.gov/businesses/small-businesses-self-employed/simplified-option-for-home-office-deduction
- Rev. Proc. 2013-13 is still the cited authority in the IRS FAQ. I did not locate a Pub 587 for 2026 (not yet published). Confidence high that nothing changed for 2026; the Schedule C instructions for 2026 will confirm in Dec 2026 to Jan 2027.
- Spec Risk R6 and config rules (`square_feet` 1 to 300, months_used) are consistent with this. Note: the IRS method works from "area used", not from months; the spec's `months_used` is a fiscl simplification (Rev. Proc. 2013-13 allows computing the average monthly square footage for part-year use, so the arithmetic is consistent, but the final line must still be capped at 300 average sq ft). No contradiction.

### 4. States: income tax on wages and self-employment income, TY2026
No broad personal income tax on wages or self-employment income:

| State | Status | Caveat | Official source |
|---|---|---|---|
| AK | UNVERIFIED (official site search did not state it; Dept of Commerce tax-facts page and the Tax Division site list no personal income tax) | | https://tax.alaska.gov/ |
| FL | PARTIAL (FL DOR page says Florida levies no individual income tax) | | https://floridarevenue.com/ |
| NV | VERIFIED ("Nevada residents do not pay state tax on income earned from salaries, wages, or similar compensation") | | https://tax.nv.gov/about-nevada-department-of-taxation/income-tax-in-nevada/ |
| NH | VERIFIED | Interest and Dividends tax (3% over $2,400/$4,800) **repealed for taxable periods beginning after Dec 31, 2024**; no TY2025 or TY2026 I&D filing or estimates. Spec's "history" caveat should read "repealed 2025", not "interest/dividends tax applies". Business profits tax and BET still exist for businesses (not researched; not personal wage tax). | https://www.revenue.nh.gov/news-and-media/repeal-nh-interest-and-dividends-tax-now-effect |
| SD | VERIFIED | | https://dor.sd.gov/individuals/taxes/ |
| TN | VERIFIED | Hall income tax repealed for years beginning on or after Jan 1, 2021; do not file. | https://www.tn.gov/revenue/taxes/hall-income-tax.html |
| TX | VERIFIED (Comptroller) | Franchise tax has a $2,650,000 no-tax-due threshold from Jan 1, 2026; sole proprietors are exempt from the franchise tax. | https://comptroller.texas.gov/taxes/ |
| WA | VERIFIED for the capital gains facts; "no broad personal income tax" statement not found on a DOR page (relies on general knowledge) | Capital gains excise tax: 7% on gains up to $1M, 9.9% above (TY2025 on; ESSB 5813); standard deduction $278,000 for 2025; TY2025 return due May 1, 2026; prepay allowed from TY2026. Applies to long-term gains, not wages or self-employment income. | https://dor.wa.gov/taxes-rates/other-taxes/capital-gains-tax |
| WY | UNVERIFIED (searched revenue.wyo.gov; no statement retrieved) | | https://revenue.wyo.gov/ |

Spec check: the spec's list (AK, FL, NV, NH, SD, TN, TX, WA, WY) is correct. The Texas Comptroller text (older fiscal note) says sole proprietors in TX, AK, FL, SD and WY face no business tax or personal income tax; that is an extra data point, not a list change.

### 5. States that tax income: estimated-payment schedule, TY2026
Default = Apr 15, Jun 15, Sep 15 2026 and Jan 15 2027, 25% each ("federal"). All sources state the next-business-day rule. 2026/27 weekday check for default dates: no shifts.

| State | Due dates (TY2026) | Installments | Weekend/holiday shift in 2026-27 | Status | Source |
|---|---|---|---|---|---|
| AL | federal | 25x4 | none | VERIFIED. (An older AL FAQ lists payment 4 as Dec 15; the 2026 form says Jan 15, 2027. Use the form.) | https://www.revenue.alabama.gov/wp-content/uploads/2026/01/26f40esinstr.pdf |
| AZ | federal | 25x4 | none | VERIFIED | https://azdor.gov/sites/default/files/document/FORMS_INDIVIDUAL_2026_140ES_f.pdf |
| AR | federal (Apr 15, Jun 15, Sep 15, Jan 15 2027 from the 2026 composite voucher; individual AR1000ES 2026 posted 06/02/2026, contents not read) | assume 25x4 | none | PARTIAL | https://www.dfa.arkansas.gov/office/taxes/income-tax-administration/individual-income-tax/forms/ |
| CA | Apr 15, Jun 15, Sep 15 (no payment), Jan 15 2027 | **30 / 40 / 0 / 30** | none | VERIFIED | https://www.ftb.ca.gov/pay/estimated-tax-payments.html |
| CO | federal | 25x4 | none | VERIFIED | https://tax.colorado.gov/sites/default/files/documents/DR_0104EP_2026.pdf |
| CT | federal | 25x4 | none | VERIFIED | https://portal.ct.gov/-/media/drs/forms/2025/income/ct1040es-flat0126.pdf |
| DE | **Apr 30**, Jun 15, Sep 15, Jan 15 2027 | 25x4 | none (Apr 30 is a Thursday) | VERIFIED (PIT-EST 2026 instructions; older 200-ES versions show Jun 16/17 and Sep 16/17, ignore) | https://revenuefiles.delaware.gov/2025/PITForms_Instructions/Instructions/PIT-EST_Instructions_2026-01.pdf |
| DC | federal | 25x4 | none | VERIFIED | https://otr.cfo.dc.gov/sites/default/files/dc/sites/otr/publication/attachments/2026_D40ES_Book_wLinks04012026.pdf |
| GA | federal | 25x4 | none | PARTIAL (instructions read were the 2024 revision; 2026 voucher exists on the DOR site) | https://dor.georgia.gov/500-es-individual-and-fiduciary-estimated-tax-payment-voucher |
| HI | **Apr 20, Jun 20, Sep 20, Jan 20 2027** (statutory) | 25x4 (N-1 says 1/4 each) | **Jun 20 2026 is Sat -> Mon Jun 22; Sep 20 2026 is Sun -> Mon Sep 21.** Apr 20 (Mon) and Jan 20 2027 (Wed) do not move. | VERIFIED (TF 2019-3, read in full; revision Feb 2026 per the DoTax list). The Jun 22/Sep 21 shift is derived from the TF's own weekend rule plus the calendar; a tax-prep blog reports the same. | https://files.hawaii.gov/tax/legal/taxfacts/tf2019-3.pdf |
| ID | **No required individual estimates.** Form 51 is voluntary, payments may be made any time until the return due date. (Business Form 41ES uses Apr/Jun/Sep/**Dec** 15.) | n/a | n/a | VERIFIED | https://tax.idaho.gov/taxes/income-tax/individual-income/filing-and-paying/ |
| IL | federal (IL-1040-ES) | 25x4 | none | PARTIAL (form surfaced by search; dates reported from it) | https://tax.illinois.gov/content/dam/soi/en/web/tax/forms/incometax/documents/currentyear/individual/il-1040-es.pdf |
| IN | federal (ES-40/IT-40ES) | 25x4 | none | VERIFIED (DOR Tax Chapter) | https://www.in.gov/dor/i-need-to/make-a-payment/estimated-payments/ |
| IA | **Apr 30, Jun 30, Sep 30, Jan 31 2027** | 25x4 | Jan 31 2027 is a **Sunday -> Mon Feb 1, 2027**. Apr 30/Jun 30/Sep 30 are weekdays. | VERIFIED. Also, from TY2026 the requirement threshold rose from $200 to $1,000. | https://tax.iowa.gov/estimated-income-tax-payments |
| KS | federal | 25x4 | none | VERIFIED | https://www.ksrevenue.gov/pdf/k-40es26.pdf |
| KY | federal | 25x4 | none | VERIFIED | https://revenue.ky.gov/Forms/740es%202026.pdf |
| LA | federal (dates for the first three installments are on the LDR calendar; Jan 15 from general guidance) | equal installments | none | VERIFIED for Apr/Jun/Sep, PARTIAL for Jan. Voucher IT-540ES. Threshold: estimated tax over $1,000 single, $2,000 joint. | https://revenue.louisiana.gov/individuals/general-resources/declaration-of-estimated-income-taxes/ |
| ME | federal | 25x4 | none | VERIFIED. (2026 form revised Jul 2026 for a new 2% surcharge; amounts, not dates.) | https://www.maine.gov/revenue/sites/maine.gov.revenue/files/inline-files/26_1040es_fillable.pdf |
| MD | federal | 25x4 | none | PARTIAL (Comptroller FAQ and tip sheet, not 2026-specific) | https://www.marylandtaxes.gov/forms/Personal_Tax_Tips/tip54.pdf |
| MA | federal per Form 1-ES (Jun 15). **DOR "Tax Due Dates" page lists the 2nd installment as Jun 16, 2026.** | 25x4 | Jun 15 2026 is a Monday, so no legitimate shift. Discrepancy unresolved. | VERIFIED for Apr/Sep/Jan; **Jun date conflicts between two official pages. Store Jun 15 (the form) and recommend a user-facing "confirm with MassTaxConnect" note.** | https://www.mass.gov/doc/2026-form-1-es-estimated-tax-payment-vouchers-instructions-and-worksheets/download |
| MI | federal | 25x4 | none | VERIFIED | https://www.michigan.gov/taxes/-/media/Project/Websites/taxes/Forms/IIT/TY2025/MI-1040ES-(2026).pdf |
| MN | federal | 25x4 | none | VERIFIED | https://www.revenue.state.mn.us/estimated-tax-payments |
| MS | federal | **80% of annual liability must be prepaid** (not a per-installment percent; MS FAQ says estimated payments must total at least 80%). Installments assumed equal. | none | PARTIAL (FAQ plus 2025 instructions; no 2026 form text) | https://www.dor.ms.gov/individual/individual-income-tax-frequently-asked-questions |
| MO | federal | 25x4 | none | VERIFIED | https://dor.mo.gov/forms/MO-1040ES_2026.pdf |
| MT | federal | 25x4 | none | VERIFIED | https://revenue.mt.gov/taxes/estimated-tax-payments |
| NE | federal | 25x4 | none | VERIFIED | https://revenue.nebraska.gov/individuals/instructions-paying-your-estimated-individual-income-tax |
| NJ | federal | 25x4 | none (Jan skip if filed by **Feb 15**, 2027) | VERIFIED | https://www.nj.gov/treasury/taxation/pdf/current/1040esi.pdf |
| NM | federal | 25x4 | none | PARTIAL (page about 500 days old; vouchers are not year-specific) | https://www.tax.newmexico.gov/individuals/file-your-taxes-overview/estimated-payments/ |
| NY | federal | 25x4 | none | VERIFIED | https://www.tax.ny.gov/pit/estimated_tax/estimated_tax_payment_due_dates.htm |
| NC | federal | 25x4 | none | PARTIAL (page undated and about 590 days old; 2026 NC-40 form exists) | https://www.ncdor.gov/taxes-forms/individual-income-tax/estimated-income-tax |
| ND | federal (Form ND-1ES 2026) | 25x4 | none | VERIFIED | https://www.tax.nd.gov/sites/www/files/documents/forms/software-developer/individual-income-forms/28709-form-nd-1es-2026%20final.pdf |
| OH | federal | 25x4 | none | PARTIAL (official page, flagged by the search tool as old; dates stated as Apr 15, Jun 15, Sep 15, Jan 15 2027) | https://tax.ohio.gov/individual/resources/estimated-payments |
| OK | federal (OW-8-ES 2026) | 25x4 | none | VERIFIED | https://oklahoma.gov/content/dam/ok/en/tax/documents/forms/software-developers/DevOW-8-ES-26.pdf |
| OR | federal | 25x4 | none | VERIFIED | https://www.oregon.gov/dor/forms/FormsPubs/publication-or-estimate_101-026_2026.pdf |
| PA | federal | 25x4 | none | VERIFIED | https://www.pa.gov/content/dam/copapwp-pagov/en/revenue/documents/formsandpublications/formsforindividuals/pit/documents/2026/2026_rev-413i.pdf |
| RI | federal | 25x4 | none (Jan skip if filed by **Feb 15**, 2027) | VERIFIED | https://tax.ri.gov/sites/g/files/xkgbur541/files/2026-01/2026%20RI-1040ES_w.pdf |
| SC | federal | 25x4 | none | VERIFIED. Act 110 changes SC brackets from TY2026 (amounts, not dates). | https://dor.sc.gov/sites/dor/files/forms/SC1040ES_2026.pdf |
| UT | **No required quarterly individual estimates.** Prepay any time with Form TC-546 (not TC-547); prepayment requirements must be met by the return due date (Apr 15, 2027). | n/a | n/a | VERIFIED | https://tax.utah.gov/forms/current/tc-546.pdf |
| VT | federal | 25x4 | none | VERIFIED | https://tax.vermont.gov/individuals/estimated-income-tax |
| VA | **May 1**, Jun 15, Sep 15, Jan 15 2027 | 25x4 (for starters on or before Apr 15; later starters get fewer, larger installments) | none (May 1, 2026 is Fri) | VERIFIED (760ES 2026). Jan skip if filed and paid by **Mar 1**, 2027. | https://www.tax.virginia.gov/sites/default/files/taxforms/individual-income-tax/2026/760es-2026.pdf |
| WV | federal expected | 25x4 | none | PARTIAL (IT-140ES instructions say "typically"; April 2026 calendar supports Apr 15; Jan 2027 not confirmed) | https://tax.wv.gov/Documents/TaxForms/it140esi.pdf |
| WI | federal | 25x4 | none | VERIFIED | https://www.revenue.wi.gov/TaxForms2026/2026-Form1-ES-Inst.pdf |

Count: 42 income-tax jurisdictions listed plus 9 no-tax = 51. Verified-vs-not: of the 42 income-tax rows, PARTIAL = AR, GA, IL, LA (Jan only), MD, MS, NM, NC, OH, WV; the rest VERIFIED. Of the 9 no-tax rows, UNVERIFIED = AK, WY; PARTIAL = FL.

Notes on percentages:
- Only CA departs from 25/25/25/25 among full-year filers. No state in this set uses a 3-installment schedule for full-year individuals. UT and ID have no required installments. Late-start taxpayers get compressed schedules in most states (for example GA, NC, PA, VA, WI, DE). That is out of scope for a planning-rate model; a note is enough.
- Federal Form 1040-ES also describes optional paths (pay everything in the first installment, skip Jan if filed by Feb 1). States differ on the "file by" date for skipping the January payment: CA Jan 31, KS Jan 31, HI Jan 31, NJ and RI Feb 15, AZ Feb 1, VA Mar 1, farmers Mar 1. Not needed for fiscl; a generic note suffices.
- Safe harbor and thresholds differ by state (MS 80%, CO 70%, others 90% of current or 100%/110% of prior). Not captured by the spec's planning-rate model; do not claim safe-harbor amounts.

## Spec contradictions and required changes
1. **UT and ID**: individual quarterly estimates are not required. `StateInfo` needs a flag (for example `estimated_required: bool = False`, installments empty, note "no required quarterly installments; you may prepay any time; settle by the April return due date"). The current spec only models "no income tax" versus "installments". This is a real gap in the spec's data model.
2. **Weekend shifts**: spec line "Weekend/holiday shifts are **not** computed; a note says due dates move to the next business day." Federal and 38 of 42 state dates need no shift in this cycle, but HI (Jun 22 and Sep 21, 2026) and IA (Feb 1, 2027) do. Recommend either storing the already-adjusted 2026 dates with a `statutory_date` field, or a small `next_business_day(date)` helper (weekends plus federal holidays; state-only holidays still need a note) applied when emitting dates. Without this, Hawaii's June and September dates are two days early.
3. **Exceptions list** (spec R3 mentions "CA 30/40/0/30" from memory): CA confirmed (30/40/0/30, Sep 15 payment is zero). Add VA (May 1 start), IA (4/30, 6/30, 9/30, 1/31), DE (Apr 30), HI (20th), UT and ID (none). No state with 3-installment individual schedules was found.
4. **NH caveat wording**: the spec says the NH caveat covers "interest/dividends history". Correct text: the I&D tax was repealed for taxable periods beginning after Dec 31, 2024; there is no TY2026 liability.
5. **WA caveat**: capital gains excise tax at 7% to $1M and 9.9% above (from TY2025); it applies to long-term gains, not to wages or self-employment income. Fine as a caveat; do not apply the state planning rate.
6. **1099 thresholds (R4)**: confirmed. Table for `tax/forms1099.py`: NEC 600 (2024), 600 (2025), 2000 (2026), indexed from 2027; K: 20000 and 200 transactions for all three years. The spec's "believed" claim is correct. The threshold applies to the payment year; the spec's `unreported_payers` rule "total ≥ tax.reporting_threshold_nec for that year" matches. Note the threshold is "$2,000 **or more**" for NEC/MISC, and 1099-K requires strictly **over** $20,000 **and** over 200 transactions. Use `>=` for NEC and `>` for K.
7. **Payment-card 1099-Ks**: no threshold, so `unreported_payers` messaging that says "may be below the reporting threshold" does not apply to payers that are payment-card processors; it is safe for NEC.
8. **Massachusetts**: two official MA pages disagree on the June 2026 date (Jun 15 versus Jun 16). Recommend the table store Jun 15 and fiscl surface "verify with your state" (spec already has such a note for unknown years).
9. **`VERIFIED_FOR = 2026`**: acceptable for the 32 VERIFIED income-tax rows, but the ten PARTIAL rows (AR, GA, IL, LA, MD, MS, NM, NC, OH, WV) and AK/WY/FL should carry a `confidence` or `verified: bool` field per row, or the test "each with a `source` URL" will pass while the claim is overstated. Recommended: add `verified: bool` to `StateInfo`; the spec's test only asserts 51 entries and non-empty `source`, so that stays compatible.
10. **Simplified home office**: no contradiction. The spec's line-30 formula ($5 x sq ft x 100 in minor units, `limited_to_profit`) matches IRS rules; carryover note is correct.

## Options compared
Not applicable (fact-finding). One implementation choice worth noting for slice B:
| Option | Fit | Cost | Risk | Notes |
|---|---|---|---|---|
| Store statutory dates, shift at read time with a helper | High | Small helper plus tests | Federal/state holidays need a list | Handles HI/IA and future years automatically |
| Store pre-shifted 2026 dates only | Medium | None | Breaks for the "unknown year, shift dates" fallback in the spec | Simpler, but wrong when the year changes |

## Recommendation
- Proceed with slice B using the table above as `tax/states.py`. Add `estimated_required` (UT, ID false) and `verified` per row; use `VERIFIED_FOR = 2026`.
- Implement a weekend roll in the date emitter (Hawaii and Iowa are the 2026 cases) rather than a note.
- Proceed with slice C using the threshold table in section 2.
- Before shipping, a human or a follow-up pass should confirm the ten PARTIAL income-tax states and AK/WY/FL. They are almost certainly the federal pattern, but the evidence is older than TY2026. MA's June date needs a one-line confirmation.
- What would change this: a state revenue department publishing a revised TY2026 schedule (disaster relief postponements, such as HI Kona Low and Hurricane relief, are one-off penalty waivers and not schedule changes), or the IRS publishing the indexed 2027 NEC/MISC threshold.

## Sources
- [Form 1040-ES (2026)](https://www.irs.gov/pub/irs-pdf/f1040es.pdf) — accessed 2026-10-11
- [IRS Estimated Taxes](https://www.irs.gov/businesses/small-businesses-self-employed/estimated-taxes) — accessed 2026-10-11
- [IRS: Am I required to file a Form 1099](https://www.irs.gov/businesses/small-businesses-self-employed/am-i-required-to-file-a-form-1099-or-other-information-return) — accessed 2026-10-11
- [IRS Publication 1099 (2026)](https://www.irs.gov/pub/irs-pdf/p1099.pdf) — accessed 2026-10-11
- [IRS Form 1099-K FAQs: General information](https://www.irs.gov/newsroom/form-1099-k-faqs-general-information) — accessed 2026-10-11
- [IRS: 1099-K threshold under the One, Big, Beautiful Bill](https://www.irs.gov/newsroom/irs-issues-faqs-on-form-1099-k-threshold-under-the-one-big-beautiful-bill-dollar-limit-reverts-to-20000) — accessed 2026-10-11
- [IRS Publication 587](https://www.irs.gov/publications/p587) — accessed 2026-10-11 (first half only)
- [IRS Simplified option for home office deduction](https://www.irs.gov/businesses/small-businesses-self-employed/simplified-option-for-home-office-deduction) — accessed 2026-10-11
- State sources are the per-state URLs in the table in section 5 and the no-tax table in section 4 — accessed 2026-10-11
- [Hawaii TF 2019-3](https://files.hawaii.gov/tax/legal/taxfacts/tf2019-3.pdf) — read in full 2026-10-11
