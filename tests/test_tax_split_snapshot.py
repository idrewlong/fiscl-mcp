"""Byte-for-byte guard for the tax.py -> tax/ package split (Phase 3, slice 0).

The fixture was computed from the single-module implementation before the split. It pins the JSON of the four
read services on demo data, so a refactor that changes any number, key, or note fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

from fiscl import tax
from fiscl.demo import load_demo

from .conftest import TODAY

FIXTURE = Path(__file__).parent / "fixtures" / "tax_snapshot.json"


def build_snapshot(conn, cfg) -> dict:
    load_demo(conn, cfg, today=TODAY)
    trips = (("2026-03-04", 42.5, "Client shoot, Biloxi"), ("2026-05-20", 12, "Bank and supplies run"))
    for d, miles, purpose in trips:
        tax.log_trip(conn, cfg, d, miles, purpose, vehicle="Truck", dry_run=False)
    conn.commit()
    return {
        "schedule_c_draft": tax.schedule_c_draft(conn, cfg, year=2026, today=TODAY),
        "estimated_tax": tax.estimated_tax(conn, cfg, year=2026, income_tax_rate_pct=22, today=TODAY),
        "estimated_tax_no_rate": tax.estimated_tax(conn, cfg, year=2026, today=TODAY),
        "review_writeoffs": tax.review_writeoffs(conn, cfg, year=2026, today=TODAY),
        "mileage_log": tax.mileage_log(conn, cfg, year=2026, today=TODAY),
    }


def test_tax_outputs_match_pre_split_snapshot(conn, cfg):
    got = json.dumps(build_snapshot(conn, cfg), indent=2, sort_keys=True)
    assert got == FIXTURE.read_text().rstrip("\n")


def test_tax_package_reexports():
    for name in (
        "schedule_c_draft",
        "estimated_tax",
        "review_writeoffs",
        "set_scope",
        "log_trip",
        "mileage_log",
        "mileage_rate",
        "due_dates",
        "DISCLAIMER",
        "SCHEDULE_C_LINES",
        "LINE_ORDER",
        "CATEGORY_DEFAULT_LINES",
        "DE_MINIMIS_MINOR",
    ):
        assert hasattr(tax, name), name
