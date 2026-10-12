"""Synthetic demo data so anyone can try fiscl in two minutes without real bank files."""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from .config import Config, append_toml_block, load_budgets
from .db import add_account, find_account
from .importers.core import import_file, post_import
from .recurring import add_months

CHECKING, CARD, SAVINGS, BUSINESS = "Demo Checking", "Demo Credit Card", "Demo Savings", "Demo Business Checking"

# Rules for the demo merchants, so the demo shows categorized spending and a realistic Schedule C.
# Some merchants (GitHub and Planet Fitness on the personal card) stay uncategorized on purpose,
# so list_uncategorized and review_writeoffs have something to show. First match wins.
DEMO_RULES = f"""\
[[rule]]
description_regex = "(?i)stripe payout|zelle from gulf coast realty|magnolia media"
account = "{BUSINESS}"
category = "Income: Business"

[[rule]]
description_regex = "(?i)adobe|digitalocean"
account = "{BUSINESS}"
category = "Business: Software & Subscriptions"

[[rule]]
description_regex = "(?i)b&h photo"
account = "{BUSINESS}"
category = "Business: Equipment"

[[rule]]
description_regex = "(?i)business lunch"
account = "{BUSINESS}"
category = "Business: Meals"

[[rule]]
description_regex = "(?i)kroger"
category = "Groceries"

[[rule]]
description_regex = "(?i)chimney|waffle house|chipotle|blue bottle"
category = "Dining"
"""


def _fmt(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100}.{abs(minor) % 100:02d}"


def generate_business(today: date, seed: int = 11) -> list:
    """A small freelance business: client payments, software, gear, meals."""
    rng = random.Random(seed)
    start = add_months(today.replace(day=1), -6)
    rows = []
    month = start
    while month <= today:

        def add(day, amt, desc, m=month):
            dt = m.replace(day=day)
            if dt <= today:
                rows.append((dt, amt, desc))

        for _ in range(rng.randint(2, 3)):
            add(
                rng.randint(3, 27),
                rng.randint(80000, 350000),
                rng.choice(["STRIPE PAYOUT", "ZELLE FROM GULF COAST REALTY", "ACH CREDIT MAGNOLIA MEDIA LLC"]),
            )
        add(6, -5999, "ADOBE *CREATIVE CLOUD")
        add(9, -2400, "DIGITALOCEAN.COM")
        add(rng.randint(10, 25), -rng.randint(2500, 7500), "TST* THE CHIMNEY BUSINESS LUNCH")
        month = add_months(month, 1)
    rows.append((today - timedelta(days=40), -324900, "B&H PHOTO 800-606-6969"))
    return sorted(rows)


def generate(today: date, seed: int = 7) -> tuple[list, list, list]:
    """Return (checking, card, savings) lists of (date, amount_minor, description)."""
    rng = random.Random(seed)
    start = add_months(today.replace(day=1), -6)
    checking, card, savings = [], [], []

    d = start
    while d.weekday() != 4:  # first Friday
        d += timedelta(days=1)
    while d <= today:
        checking.append((d, 186000, "ACME CORP PAYROLL PPD ID 9876543210"))
        d += timedelta(days=14)

    month = start
    card_by_month: dict[tuple[int, int], int] = {}
    while month <= today:

        def on(day: int, m=month) -> date:
            return m.replace(day=day)

        def add(lst, day, amt, desc, m=month):
            dt = on(day, m)
            if dt <= today:
                lst.append((dt, amt, desc))

        add(checking, 1, -145000, "OAKWOOD APTS RENT")
        add(checking, 15, -rng.randint(9500, 16500), "MISSISSIPPI POWER BILL PAY")
        add(checking, 12, -1199, "SPOTIFY USA")
        add(checking, 5, -20000, "ONLINE TRANSFER TO SAVINGS XXXXXX4421")
        add(savings, 5, 20000, "ONLINE TRANSFER FROM CHECKING XXXXXX1180")
        months_back = (today.year - month.year) * 12 + today.month - month.month
        add(card, 3, -1799 if months_back <= 1 else -1549, "NETFLIX.COM")
        add(card, 8, -2499, "PLANET FITNESS CLUB FEES")
        add(card, 14, -400, "GITHUB INC")
        for wk in range(4):
            add(card, 2 + wk * 7, -rng.randint(6000, 14500), "KROGER #0412 GULFPORT MS")
        for _ in range(rng.randint(5, 9)):
            add(
                card,
                rng.randint(1, 28),
                -rng.randint(450, 4800),
                rng.choice(["SQ *BLUE BOTTLE COFFEE", "CHIPOTLE 2291", "TST* THE CHIMNEY", "WAFFLE HOUSE 1123"]),
            )
        for _ in range(rng.randint(2, 3)):
            add(card, rng.randint(1, 28), -rng.randint(3500, 6200), "SHELL OIL 57444012")
        for _ in range(rng.randint(1, 4)):
            add(card, rng.randint(1, 28), -rng.randint(1200, 9000), "AMZN MKTP US*2K4LP09")
        month = add_months(month, 1)

    for dt, amt, _ in card:
        card_by_month[(dt.year, dt.month)] = card_by_month.get((dt.year, dt.month), 0) + amt
    month = add_months(start, 1)
    while month <= today:
        prev = add_months(month, -1)
        due = month.replace(day=20)
        total = -card_by_month.get((prev.year, prev.month), 0)
        if due <= today and total > 0:
            checking.append((due, -total, "DEMO CARD ONLINE PMT"))
            card.append((due + timedelta(days=1), total, "PAYMENT THANK YOU-MOBILE"))
        month = add_months(month, 1)
    return sorted(checking), sorted(card), sorted(savings)


AMAZON_SPLIT_DESC = "AMZN MKTP US*7Q2XY81"
NORDSTROM_AMOUNT = -12000


def generate_extras(today: date) -> tuple[list, list]:
    """Seeded cases for the v2 tools, as (checking, card) rows. Dates are relative to today.

    Descriptions are chosen so normalize_merchant gives one merchant per story (STREAMLY.COM for the trial and
    the paid charges, the same B&H string for the small and the big purchase).
    """
    d = timedelta
    bh = "B&H PHOTO 800-606-6969"
    checking = [(today - d(days=12), -3400, "OVERDRAFT FEE")]
    card = [
        (today - d(days=5), -8417, "WHOLEFDS MKT"),  # same charge twice
        (today - d(days=4), -8417, "WHOLEFDS MKT"),
        (today - d(days=150), -4500, bh),  # small purchases, then a spike
        (today - d(days=100), -3200, bh),
        (today - d(days=60), -5800, bh),
        (today - d(days=8), -124900, bh),
        (today - d(days=40), -100, "STREAMLY.COM"),  # trial, then paid
        (today - d(days=26), -1599, "STREAMLY.COM"),
        (today - d(days=2), -1599, "STREAMLY.COM"),
        (today - d(days=6), -38500, "LUXE ESTATE SALES"),  # new merchant
        (today - d(days=25), -4250, "TARGET"),  # refund received
        (today - d(days=10), 4250, "TARGET"),
        (today - d(days=35), NORDSTROM_AMOUNT, "NORDSTROM"),  # refund expected (tagged below)
        (today - d(days=9), -10000, AMAZON_SPLIT_DESC),  # split below
    ]
    return checking, card


def _seed_extras(conn, today: date) -> None:
    """Rows that live outside the bank files (tags, splits, a manual asset, a goal). Safe to run repeatedly."""
    from .db import get_or_create_category

    conn.execute(
        """INSERT OR IGNORE INTO tags (transaction_id, tag)
           SELECT id, 'refund-pending' FROM transactions WHERE merchant = 'Nordstrom' AND amount = ?""",
        (NORDSTROM_AMOUNT,),
    )
    parent = conn.execute("SELECT id FROM transactions WHERE raw_description = ?", (AMAZON_SPLIT_DESC,)).fetchone()
    if parent and not conn.execute("SELECT 1 FROM splits WHERE transaction_id = ?", (parent["id"],)).fetchone():
        for minor, category, scope, note in (
            (-6000, "Shopping", None, None),
            (-4000, "Business: Supplies", "business", "printer paper and ink"),
        ):
            conn.execute(
                "INSERT INTO splits (transaction_id, amount, category_id, scope, note) VALUES (?, ?, ?, ?, ?)",
                (parent["id"], minor, get_or_create_category(conn, category), scope, note),
            )
    conn.execute(
        "INSERT OR IGNORE INTO manual_assets (name, kind, value, as_of, note) VALUES (?, 'vehicle', ?, ?, ?)",
        ("Pickup truck", 1850000, today.isoformat(), "Estimated resale value"),
    )
    savings = find_account(conn, SAVINGS)
    conn.execute(
        """INSERT OR IGNORE INTO goals (name, target, target_date, account_id, saved, created_at)
           VALUES ('Emergency fund', 1000000, ?, ?, 0, ?)""",
        ((today + timedelta(days=180)).isoformat(), savings["id"], today.isoformat()),
    )


def _write_csv(path: Path, rows: list) -> None:
    lines = ["Date,Description,Amount"]
    for dt, amt, desc in sorted(rows, reverse=True):
        lines.append(f"{dt:%m/%d/%Y},{desc},{_fmt(amt)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_ofx(path: Path, rows: list, opening: int, today: date) -> None:
    balance = opening + sum(a for _, a, _ in rows)
    trns = "".join(
        f"<STMTTRN><TRNTYPE>{'CREDIT' if a > 0 else 'DEBIT'}<DTPOSTED>{dt:%Y%m%d}120000"
        f"<TRNAMT>{_fmt(a)}<FITID>DEMO{dt:%Y%m%d}{i:04d}<NAME>{desc}</STMTTRN>\n"
        for i, (dt, a, desc) in enumerate(rows)
    )
    path.write_text(
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Demo Bank</FI>"
        "</SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD"
        "<BANKACCTFROM><BANKID>000000000<ACCTID>99001180<ACCTTYPE>CHECKING</BANKACCTFROM>"
        f"<BANKTRANLIST>\n{trns}</BANKTRANLIST>"
        f"<LEDGERBAL><BALAMT>{_fmt(balance)}<DTASOF>{today:%Y%m%d}</LEDGERBAL>"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n",
        encoding="utf-8",
    )


def has_real_data(conn) -> bool:
    """True when the database holds any account or transaction that the demo didn't create, so loading the demo
    would mix synthetic rows into real financial data."""
    names = (CHECKING, CARD, SAVINGS, BUSINESS)
    marks = ",".join("?" * len(names))
    if conn.execute(f"SELECT 1 FROM accounts WHERE name NOT IN ({marks}) LIMIT 1", names).fetchone():
        return True
    return bool(
        conn.execute(
            "SELECT 1 FROM transactions t JOIN accounts a ON a.id = t.account_id "
            f"WHERE a.name NOT IN ({marks}) LIMIT 1",
            names,
        ).fetchone()
    )


def load_demo(conn, cfg: Config, today: date | None = None) -> dict:
    today = today or date.today()
    checking, card, savings = generate(today)
    extra_checking, extra_card = generate_extras(today)
    checking, card = sorted(checking + extra_checking), sorted(card + extra_card)
    demo_dir = cfg.home / "demo"
    demo_dir.mkdir(exist_ok=True)
    for name, type_, scope in (
        (CHECKING, "checking", "personal"),
        (CARD, "credit", "personal"),
        (SAVINGS, "savings", "personal"),
        (BUSINESS, "checking", "business"),
    ):
        if find_account(conn, name) is None:
            add_account(conn, name, type_, "Demo Bank", cfg.currency, scope=scope)
    _write_ofx(demo_dir / "checking.ofx", checking, opening=180000, today=today)
    _write_csv(demo_dir / "card.csv", card)
    _write_csv(demo_dir / "savings.csv", savings)
    business = generate_business(today)
    _write_csv(demo_dir / "business.csv", business)
    results = [
        import_file(conn, cfg, demo_dir / "checking.ofx", account_name=CHECKING, post_process=False),
        import_file(conn, cfg, demo_dir / "card.csv", account_name=CARD, post_process=False),
        import_file(conn, cfg, demo_dir / "savings.csv", account_name=SAVINGS, post_process=False),
        import_file(conn, cfg, demo_dir / "business.csv", account_name=BUSINESS, post_process=False),
    ]
    biz_id = find_account(conn, BUSINESS)["id"]
    total = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_id = ?", (biz_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO balances (account_id, as_of, amount, source) VALUES (?, ?, ?, 'manual')",
        (biz_id, today.isoformat(), 300000 + total),
    )
    if not conn.execute("SELECT 1 FROM trips LIMIT 1").fetchone():
        for days_ago, miles, purpose in (
            (50, 42.5, "Client shoot, Biloxi waterfront"),
            (31, 18.2, "Gear pickup and client meeting, Gulfport"),
            (12, 64.0, "Real estate shoot, Ocean Springs"),
        ):
            d = today - timedelta(days=days_ago)
            if d.year == today.year:
                conn.execute(
                    "INSERT INTO trips (trip_date, miles_tenths, purpose, vehicle, created_at) "
                    "VALUES (?, ?, ?, 'Truck', ?)",
                    (d.isoformat(), int(miles * 10), purpose, today.isoformat()),
                )
    savings_id = find_account(conn, SAVINGS)["id"]
    total = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_id = ?", (savings_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO balances (account_id, as_of, amount, source) VALUES (?, ?, ?, 'manual')",
        (savings_id, today.isoformat(), 250000 + total),
    )
    if "Business: Software & Subscriptions" not in cfg.rules_file.read_text(encoding="utf-8"):
        append_toml_block(cfg.rules_file, DEMO_RULES, tool="fiscl demo")
    _seed_extras(conn, today)
    post = post_import(conn, cfg, today=today)
    if not load_budgets(cfg):
        append_toml_block(
            cfg.budgets_file,
            "\n".join(
                f'[[budget]]\ncategory = "{c}"\nmonthly = {v}\n'
                for c, v in (("Groceries", 450), ("Dining", 200), ("Shopping", 150), ("Subscriptions", 60))
            ),
            tool="fiscl demo",
        )
    return {"imports": [r.as_dict() for r in results], "post_import": post}
