from fiscl.importers.core import import_file
from fiscl.importers.csv_import import init_profile

from .conftest import write

CSV = """Date,Description,Amount
10/02/2026,SQ *BLUE BOTTLE COFFEE,-5.00
10/02/2026,SQ *BLUE BOTTLE COFFEE,-5.00
10/03/2026,NETFLIX.COM,-15.49
10/04/2026,REFUND THANK YOU,20.00
"""

OFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Test Bank</FI></SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD
<BANKACCTFROM><BANKID>123456789<ACCTID>5550001234<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261001120000[-5:CDT]<TRNAMT>-1450.00<FITID>A1<NAME>OAKWOOD APTS RENT</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261002<TRNAMT>1860.00<FITID>A2<NAME>ACME PAYROLL<MEMO>PPD</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>3210.55<DTASOF>20261003</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def rows(conn):
    return conn.execute("SELECT * FROM transactions ORDER BY posted_date, id").fetchall()


def test_csv_import_keeps_same_day_duplicates(conn, cfg, accounts, tmp_path):
    r = import_file(conn, cfg, write(tmp_path / "a.csv", CSV), account_name="Checking")
    assert r.status == "ok" and r.rows_added == 4
    coffee = [x for x in rows(conn) if x["merchant"] == "Blue Bottle Coffee"]
    assert len(coffee) == 2 and all(x["amount"] == -500 for x in coffee)


def test_reimporting_same_file_is_noop(conn, cfg, accounts, tmp_path):
    f = write(tmp_path / "a.csv", CSV)
    import_file(conn, cfg, f, account_name="Checking")
    again = import_file(conn, cfg, f, account_name="Checking")
    assert again.status == "duplicate_file" and len(rows(conn)) == 4


def test_overlapping_exports_dedupe(conn, cfg, accounts, tmp_path):
    import_file(conn, cfg, write(tmp_path / "a.csv", CSV), account_name="Checking")
    overlap = CSV + "10/05/2026,KROGER #12,-88.10\n"
    r = import_file(conn, cfg, write(tmp_path / "b.csv", overlap), account_name="Checking")
    assert r.rows_added == 1 and r.rows_skipped == 4


def test_csv_without_account_fails_cleanly(conn, cfg, accounts, tmp_path):
    r = import_file(conn, cfg, write(tmp_path / "a.csv", CSV))
    assert r.status == "failed" and "account" in r.error
    status = conn.execute("SELECT status FROM imports").fetchone()["status"]
    assert status == "failed"
    # A failed file can be retried once the problem is fixed.
    assert import_file(conn, cfg, tmp_path / "a.csv", account_name="Checking").status == "ok"


def test_unknown_csv_layout_points_to_profile_init(conn, cfg, accounts, tmp_path):
    f = write(tmp_path / "odd.csv", "When,What,How Much\n2026-10-01,Thing,-1\n")
    r = import_file(conn, cfg, f, account_name="Checking")
    assert r.status == "failed" and "profile init" in r.error


def test_profile_init_then_import(conn, cfg, accounts, tmp_path):
    f = write(
        tmp_path / "mybank.csv", "Post Date,Payee,Withdrawal,Deposit\n10/01/2026,SHELL,40.00,\n10/02/2026,PAY,,100\n"
    )
    out = init_profile(f, cfg)
    assert out.exists()
    r = import_file(conn, cfg, f, account_name="Checking")
    assert r.status == "ok", r.error
    amounts = sorted(x["amount"] for x in rows(conn))
    assert amounts == [-4000, 10000]


def test_ofx_import_creates_account_without_storing_number(conn, cfg, tmp_path):
    r = import_file(conn, cfg, write(tmp_path / "s.ofx", OFX))
    assert r.status == "ok" and r.rows_added == 2
    acct = conn.execute("SELECT * FROM accounts").fetchone()
    assert acct["last4"] == "1234" and "5550001234" not in (acct["acct_hash"] or "")
    assert "5550001234" not in acct["name"]
    bal = conn.execute("SELECT amount, as_of FROM balances").fetchone()
    assert bal["amount"] == 321055 and bal["as_of"] == "2026-10-03"
    # Same account on a second file (different content) maps to the same account by hash.
    r2 = import_file(conn, cfg, write(tmp_path / "s2.ofx", OFX.replace("A2", "A3")))
    assert r2.rows_added == 1 and conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_pending_replaced_by_posted(conn, cfg, accounts, tmp_path):
    from fiscl.importers.core import import_statements
    from fiscl.importers.types import ParsedStatement, ParsedTxn

    pend = ParsedStatement(format="csv", txns=[ParsedTxn("2026-10-01", -1200, "CHIPOTLE 22", pending=True)])
    import_statements(conn, cfg, [pend], "p", "d1", account_name="Checking")
    posted = ParsedStatement(format="csv", txns=[ParsedTxn("2026-10-03", -1200, "CHIPOTLE 22")])
    r = import_statements(conn, cfg, [posted], "q", "d2", account_name="Checking")
    assert r.pending_replaced == 1
    left = rows(conn)
    assert len(left) == 1 and left[0]["is_pending"] == 0


def test_dry_run_writes_nothing(conn, cfg, accounts, tmp_path):
    r = import_file(conn, cfg, write(tmp_path / "a.csv", CSV), account_name="Checking", dry_run=True)
    assert r.rows_added == 4
    assert rows(conn) == [] and conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_rules_and_transfers_after_import(conn, cfg, accounts, tmp_path):
    write(
        tmp_path / "chk.csv",
        "Date,Description,Amount\n10/20/2026,CARD ONLINE PMT,-300.00\n10/03/2026,NETFLIX.COM,-15.49\n",
    )
    write(tmp_path / "card.csv", "Date,Description,Amount\n10/21/2026,PAYMENT THANK YOU,300.00\n")
    import_file(conn, cfg, tmp_path / "chk.csv", account_name="Checking")
    import_file(conn, cfg, tmp_path / "card.csv", account_name="Card")
    transfers = conn.execute("SELECT COUNT(*) FROM transactions WHERE transfer_group IS NOT NULL").fetchone()[0]
    assert transfers == 2
    cat = conn.execute(
        "SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.merchant = 'Netflix.com'"
    ).fetchone()[0]
    assert cat == "Subscriptions"
