"""export_schedule_c: a preparer-ready Schedule C package (summary CSV, Markdown, optional detail CSV).

Files are only ever created inside `<data dir>/exports/`. No path, directory, or filename comes from the caller:
names are built from fixed tokens, the year, a business id, and a timestamp. Everything bank-derived is escaped for
CSV formula injection and Markdown before it is written.
"""

from __future__ import annotations

import contextlib
import csv
import errno
import io
import os
import re
import stat
from datetime import date, datetime
from pathlib import Path

from ..common import meta, year_bounds
from ..config import DIR_MODE, FILE_MODE, Config
from ..db import BUSINESS_SQL, SCOPE_SQL, TXN_FROM
from ..guard import GuardError, choice, clean_text, redact_text
from ..money import amount, exponent
from .businesses import businesses_for_period, get_business
from .lines import DISCLAIMER, SCHEDULE_C_LINES, line_for
from .schedule_c import schedule_c_draft

FORMATS = ("csv", "markdown")

SUMMARY_COLUMNS = ("section", "line", "line_name", "category", "amount", "full_amount", "count", "note")
SUMMARY_TEXT = {"line_name", "category", "note"}
DETAIL_COLUMNS = (
    "date",
    "account",
    "merchant",
    "amount",
    "category",
    "line",
    "scope",
    "business",
    "note",
    "transaction_id",
    "split_id",
)
DETAIL_TEXT = {"account", "merchant", "category", "line", "scope", "business", "note"}

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")
FOOTER = "Prepared from your own records by fiscl; not tax advice."
KEEP_NOTE = (
    "These files hold the same sensitive data as your database. Delete exports you've sent to your preparer; "
    "fiscl doesn't remove them."
)
NOT_PRIVATE = "the exports folder isn't a private directory inside your data folder; run fiscl doctor"


# --- escaping ------------------------------------------------------------------------------


def csv_cell(value: object) -> str:
    """Bank-derived text for a CSV cell: cleaned, scrubbed of card/SSN/long account-like numbers, and defanged if a
    spreadsheet would read it as a formula."""
    text = redact_text(clean_text(None if value is None else str(value)))
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


_MD_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_MD_PUNCT = re.compile(r"[&<>\\`*_\[\](){}#|!~]")
_MD_SCHEME = re.compile(r"(?<=[A-Za-z])://")
_MD_WWW = re.compile(r"\b(www)\.", re.IGNORECASE)


def md_escape(text: str) -> str:
    """Markdown-escape already-clean text: no raw HTML, no links (including bare URLs), no emphasis."""
    text = _MD_PUNCT.sub(lambda m: _MD_ESCAPES.get(m.group(), "\\" + m.group()), text)
    # GFM auto-links bare `scheme://` and `www.` text; a backslash escape keeps it readable but not clickable.
    text = _MD_SCHEME.sub(r"\\://", text)
    return _MD_WWW.sub(r"\1\\.", text)


def md_text(value: object) -> str:
    """Bank-derived text for Markdown: cleaned, scrubbed of card/SSN/account-like numbers, one line, not clickable."""
    return md_escape(redact_text(clean_text(None if value is None else str(value))))


def plain(minor: int, currency: str) -> str:
    """Plain decimal string for a spreadsheet: no symbol, no grouping."""
    places = exponent(currency)
    if places == 0:
        return str(int(minor))
    whole, frac = divmod(abs(int(minor)), 10**places)
    return f"{'-' if minor < 0 else ''}{whole}.{frac:0{places}d}"


# --- content -------------------------------------------------------------------------------


def _freshness(draft: dict) -> str:
    parts = [f"{f['account']}: {f['latest_transaction'] or 'no transactions'}" for f in draft["meta"]["data_freshness"]]
    return "; ".join(parts) or "no accounts"


def summary_rows(draft: dict, cur: str) -> list[dict]:
    rows: list[dict] = []

    def add(section, line="", line_name="", category="", amt="", full="", count="", note=""):
        rows.append(
            dict(
                section=section,
                line=line,
                line_name=line_name,
                category=category,
                amount=amt,
                full_amount=full,
                count=count,
                note=note,
            )
        )

    def money(a: dict) -> str:
        return plain(a["minor"], cur)

    if "business" in draft:
        add("business", note=draft["business"]["name"])
    add("gross_receipts", "1", SCHEDULE_C_LINES["1"], amt=money(draft["gross_receipts"]))
    for entry in draft["expenses_by_line"]:
        line, name = entry["line"], entry["name"]
        for cat in entry["categories"]:
            add("category", line, name, cat["category"], money(cat["spent"]), count=cat["count"])
        mileage = entry.get("standard_mileage")
        if mileage:
            note = f"{mileage['miles']} miles at {mileage['rate_cents']} cents per mile"
            add("mileage", line, name, "Standard mileage", money(mileage["deduction"]), note=note)
        full = money(entry["full_amount"]) if "full_amount" in entry else ""
        note = "50% of business meals" if line == "24b" else ""
        count = sum(c["count"] for c in entry["categories"])
        add("line", line, name, amt=money(entry["amount"]), full=full, count=count, note=note)
    add("total_expenses", "28", "Total expenses", amt=money(draft["total_expenses"]))
    add("tentative_profit", "29", "Tentative profit", amt=money(draft["tentative_profit_before_home_office"]))
    ho = draft["home_office"]
    if ho:
        note = f"Simplified method, {ho['allowable_square_feet']} sq ft at {ho['rate_per_sqft']['display']}"
        if "home_square_feet" in ho:
            note += f"; this business's share of a {ho['home_square_feet']} sq ft home office"
        if ho["limited_to_profit"]:
            note += "; limited to business profit"
        add("home_office", "30", "Expenses for business use of your home", amt=money(ho["line_30"]), note=note)
    else:
        add("home_office", "30", "Expenses for business use of your home", amt=plain(0, cur), note="not configured")
    add("net_profit", "31", "Net profit", amt=money(draft["net_profit"]))
    for u in draft["unmapped_business_spending"]:
        add("unmapped", category=u["category"], amt=money(u["spent"]), count=u["count"], note="no Schedule C line")
    add("disclaimer", note=DISCLAIMER)
    add("data_through", note=_freshness(draft))
    return rows


def detail_rows(conn, cfg: Config, start: date, end: date, business_id: int | None = None) -> list[dict]:
    """Business-scope lines in the year; `business_id` restricts to one business and fills the business column."""
    redact = cfg.guard.redact_descriptions
    biz_sql, biz_params = (f"AND {BUSINESS_SQL} = ?", (business_id,)) if business_id is not None else ("", ())
    found = conn.execute(
        f"""SELECT t.posted_date, a.name AS account, t.merchant, t.amount, c.name AS category,
                   c.schedule_c_line AS line, {SCOPE_SQL} AS scope, t.note, t.id AS txn_id, t.split_id,
                   b.name AS business
            FROM {TXN_FROM} LEFT JOIN businesses b ON b.id = {BUSINESS_SQL}
            WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business' {biz_sql}
              AND t.amount IS NOT NULL  -- unconverted foreign rows have no home amount yet
            ORDER BY t.posted_date, t.id, t.split_id""",
        (start.isoformat(), end.isoformat(), *biz_params),
    ).fetchall()
    return [
        {
            "date": r["posted_date"],
            "account": r["account"],
            "merchant": "" if redact else r["merchant"],
            "amount": plain(r["amount"], cfg.currency),
            "category": r["category"] or "Uncategorized",
            "line": line_for(r["category"], r["line"]) or "",
            "scope": r["scope"],
            "business": r["business"] if business_id is not None else "",  # blank with a single business
            "note": "" if redact else r["note"],
            "transaction_id": r["txn_id"],
            "split_id": r["split_id"] if r["split_id"] is not None else "",
        }
        for r in found
    ]


def to_csv(columns: tuple[str, ...], text_cols: set[str], rows: list[dict]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for row in rows:
        w.writerow([csv_cell(row[c]) if c in text_cols else row[c] for c in columns])
    return buf.getvalue().encode("utf-8")


def to_markdown(draft: dict, cur: str) -> bytes:
    def money(a: dict) -> str:
        return md_escape(a["display"])  # amounts are not scrubbed

    def table(head: tuple[str, ...], body: list[tuple]) -> list[str]:
        out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        out += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
        return out + [""]

    ho = draft["home_office"]
    summary = [("1", md_text(SCHEDULE_C_LINES["1"]), money(draft["gross_receipts"]))]
    summary += [(e["line"], md_text(e["name"]), money(e["amount"])) for e in draft["expenses_by_line"]]
    summary += [
        ("28", "Total expenses", money(draft["total_expenses"])),
        ("29", "Tentative profit", money(draft["tentative_profit_before_home_office"])),
        ("30", "Home office (simplified method)", money(ho["line_30"]) if ho else "not configured"),
        ("31", "Net profit", money(draft["net_profit"])),
    ]
    lines = [
        f"# Schedule C draft, {draft['tax_year']}",
        "",
        f"> {DISCLAIMER}",
        "",
        *([f"Business: {md_text(draft['business']['name'])}", ""] if "business" in draft else []),
        f"Data through: {md_text(_freshness(draft))}",
        "",
        "## Summary",
        "",
        *table(("Line", "Name", "Amount"), summary),
        "## Expenses by category",
        "",
    ]
    cats = []
    for e in draft["expenses_by_line"]:
        cats += [(e["line"], md_text(c["category"]), money(c["spent"]), c["count"]) for c in e["categories"]]
        m = e.get("standard_mileage")
        if m:
            cats.append((e["line"], "Standard mileage", money(m["deduction"]), md_text(f"{m['miles']} miles")))
    lines += table(("Line", "Category", "Amount", "Count"), cats)
    if draft["unmapped_business_spending"]:
        lines += ["## Unmapped business spending", ""]
        lines += table(
            ("Category", "Amount", "Count"),
            [(md_text(u["category"]), money(u["spent"]), u["count"]) for u in draft["unmapped_business_spending"]],
        )
    lines += ["## Notes", ""]
    lines += [f"- {md_text(n)}" for n in draft["notes"][1:]] + ["", FOOTER, ""]
    return "\n".join(lines).encode("utf-8")


EXPORT_NAME = re.compile(r"^schedule-c-(\d{4})-b\d+-(\d{8}T\d{6})(?:-detail)?\.(?:csv|md)$")


def existing_exports(cfg: Config, year: int, limit: int = 10) -> list[str]:
    """Relative paths of export files for `year`, newest first. Names only: nothing is opened or read."""
    try:
        directory = exports_dir(cfg, create=False)
        names = os.listdir(directory)
    except (GuardError, OSError):
        return []
    found = []
    for name in names:
        m = EXPORT_NAME.match(name)
        if m and int(m.group(1)) == year:
            found.append((m.group(2), name))
    return [f"exports/{name}" for _, name in sorted(found, reverse=True)[:limit]]


# --- safe writing --------------------------------------------------------------------------


def exports_dir(cfg: Config, create: bool) -> Path:
    """The exports folder, verified to be a real private directory directly inside the data folder."""
    return private_dir(cfg, cfg.exports, create)


def private_dir(cfg: Config, d: Path, create: bool, message: str = NOT_PRIVATE) -> Path:
    """`d` (a fixed folder directly inside the data folder), verified to be a real private directory. Shared with the
    weekly digest writer, which reuses `write_all` below."""
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        if not create:
            return d
        d.mkdir(mode=DIR_MODE)
        st = os.lstat(d)
    uid_ok = not hasattr(os, "getuid") or st.st_uid == os.getuid()
    real_dir = stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
    if not (real_dir and uid_ok and d.resolve().parent == cfg.home.resolve()):
        raise GuardError(message, code="invalid")
    if create and stat.S_IMODE(st.st_mode) != DIR_MODE:
        os.chmod(d, DIR_MODE)
    return d


_NO_HARDLINK_ERRNOS = {
    getattr(errno, n) for n in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EMLINK", "EXDEV") if hasattr(errno, n)
}


def write_all(
    directory: Path,
    files: list[tuple[str, bytes]],
    exists_message: str = "an export with that name already exists; try again in a second",
) -> None:
    """Write every file or none: temp files opened O_EXCL|O_NOFOLLOW (0600), then published with a hard link, which
    fails instead of replacing a file that appeared meanwhile."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    made: list[Path] = []
    exists = GuardError(exists_message, code="exists")
    try:
        for name, _ in files:
            if os.path.lexists(directory / name):
                raise exists
        for name, data in files:
            tmp = directory / (name + ".tmp")
            try:
                fd = os.open(tmp, flags, FILE_MODE)
            except FileExistsError as exc:
                raise exists from exc
            made.append(tmp)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        for name, data in files:
            tmp, final = directory / (name + ".tmp"), directory / name
            try:
                os.link(tmp, final, follow_symlinks=False)
            except FileExistsError as exc:
                raise exists from exc
            except (OSError, NotImplementedError) as exc:
                if isinstance(exc, OSError) and exc.errno not in _NO_HARDLINK_ERRNOS:
                    raise
                # no hard links on this filesystem: create the final name directly, still exclusively
                try:
                    fd = os.open(final, flags, FILE_MODE)
                except FileExistsError as exc:
                    raise exists from exc
                made.append(final)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            else:
                made.append(final)
            tmp.unlink()
            made.remove(tmp)
    except BaseException:
        for p in made:
            with contextlib.suppress(OSError):
                p.unlink()
        raise


# --- the tool ------------------------------------------------------------------------------


def export_schedule_c(
    conn,
    cfg: Config,
    year=None,
    formats=None,
    include_transactions=False,
    dry_run=True,
    today=None,
    now=None,
    business=None,
) -> dict:
    """Plan (default) or write the Schedule C export files for `year`. Never overwrites, never leaves the data dir.

    With several businesses, one file set per business that has activity (or just `business`)."""
    today = today or date.today()
    year, start, end = year_bounds(year, today)
    if formats is None:
        formats = list(FORMATS)
    if not isinstance(formats, (list, tuple)) or not formats:
        raise GuardError("formats must be a non-empty list: csv, markdown")
    formats = [f for f in FORMATS if f in {choice(x, FORMATS, "formats") for x in formats}]
    cur = cfg.currency
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")

    # (business id, draft, shown): `shown` is False only for the plain single-business case, whose files and
    # response keep their original shape. Otherwise each targeted business gets its own file set.
    targets: list[tuple[int, dict, bool]]
    if business is not None:
        biz = get_business(conn, business)
        targets = [(biz["id"], schedule_c_draft(conn, cfg, year, today, business=business), True)]
    else:
        covered = businesses_for_period(conn, start, end)
        if len(covered) == 1:
            targets = [(covered[0]["id"], schedule_c_draft(conn, cfg, year, today), False)]
        else:
            active = {
                r[0]
                for r in conn.execute(
                    f"""SELECT DISTINCT {BUSINESS_SQL} FROM {TXN_FROM}
                        WHERE t.posted_date BETWEEN ? AND ? AND {SCOPE_SQL} = 'business'""",
                    (start.isoformat(), end.isoformat()),
                )
            }
            chosen = [r for r in covered if r["id"] in active] or covered[:1]
            targets = [(r["id"], schedule_c_draft(conn, cfg, year, today, business=r["name"]), True) for r in chosen]

    planned: list[tuple[str, str, bytes, int | None]] = []  # name, kind, content, rows
    for biz_id, draft, shown in targets:
        stem = f"schedule-c-{year}-b{biz_id}-{stamp}"
        if "csv" in formats:
            rows = summary_rows(draft, cur)
            planned.append((f"{stem}.csv", "summary_csv", to_csv(SUMMARY_COLUMNS, SUMMARY_TEXT, rows), len(rows)))
        if "markdown" in formats:
            planned.append((f"{stem}.md", "markdown", to_markdown(draft, cur), None))
        if include_transactions:
            detail = detail_rows(conn, cfg, start, end, biz_id if shown else None)
            planned.append(
                (f"{stem}-detail.csv", "detail_csv", to_csv(DETAIL_COLUMNS, DETAIL_TEXT, detail), len(detail))
            )

    directory = exports_dir(cfg, create=not dry_run)
    if not dry_run:
        write_all(directory, [(name, data) for name, _, data, _ in planned])

    files = []
    for name, kind, _, rows in planned:
        entry = {"path": f"exports/{name}", "kind": kind}
        if rows is not None:
            entry["rows"] = rows
        files.append(entry)
    notes = [DISCLAIMER, KEEP_NOTE]
    if include_transactions and cfg.guard.redact_descriptions:
        notes.append("redact_descriptions is on, so the merchant and note columns in the detail CSV are empty.")
    drafts = [d for _, d, _ in targets]
    if len(drafts) == 1:
        summary = {k: drafts[0][k] for k in ("gross_receipts", "total_expenses", "net_profit")}
    else:  # several businesses: the combined figures; by_business has each one
        summary = {
            k: amount(sum(d[k]["minor"] for d in drafts), cur)
            for k in ("gross_receipts", "total_expenses", "net_profit")
        }
    result = {
        "dry_run": bool(dry_run),
        "files": files,
        "summary": summary,
        "next_step": "Call again with dry_run=false to write these files." if dry_run else "Applied.",
        "notes": notes,
    }
    if any(shown for _, _, shown in targets):
        result["by_business"] = [
            {
                "business": d["business"],
                "summary": {k: d[k] for k in ("gross_receipts", "total_expenses", "net_profit")},
            }
            for _, d, _ in targets
        ]
        notes.append(
            "One file set per business (the -bN- part of each name is the business id). Self-employment tax is "
            "figured on the combined profit of all your businesses."
        )
    if not dry_run:
        result["written"] = True
        result["data_dir_relative"] = True
    result["meta"] = meta(conn, cfg, start, end, row_count=len(files))
    return result
