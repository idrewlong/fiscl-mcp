"""fiscl command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import DIR_MODE, FILE_MODE, ConfigError, ensure_layout, load_budgets, load_config
from .db import add_account, backup, find_account, open_db
from .guard import GuardError


def setup_logging(cfg, verbose: bool = False) -> None:
    """Logs go to a file, never stdout: stdout carries the MCP protocol."""
    cfg.logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(cfg.logs / "fiscl.log", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # The MCP SDK logs tool error text at INFO, which can echo argument values. fiscl's own audit log
    # records sanitized calls instead.
    logging.getLogger("mcp").setLevel(logging.WARNING)


# Folders that sync to someone else's servers. Financial data should stay on this machine.
_CLOUD_SYNC_MARKERS = (
    "library/mobile documents",  # iCloud Drive
    "library/cloudstorage",  # macOS File Provider: OneDrive, Google Drive, Dropbox, Box
    "dropbox",
    "onedrive",
    "google drive",
    "googledrive",
    "icloud",
    "box sync",
)


def cloud_sync_warning(path: Path) -> str | None:
    lowered = str(path.expanduser().resolve()).lower().replace("\\", "/")
    for marker in _CLOUD_SYNC_MARKERS:
        if marker in lowered:
            return (
                f"{path} looks like a cloud-synced folder ({marker}); your financial data would leave this machine. "
                "Move it with --home or FISCL_HOME to a local folder such as ~/fiscl."
            )
    return None


def disk_encryption_warning() -> str | None:
    """Best-effort local check (no network) that full-disk encryption is on."""
    if sys.platform != "darwin":
        return None
    import subprocess

    try:
        out = subprocess.run(["fdesetup", "status"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "FileVault is Off" in out:
        return "FileVault is off. Turn it on so the fiscl database is encrypted at rest."
    return None


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_serve(cfg, args) -> int:
    from .server import build_server

    observer = None
    if not args.no_watch:
        from .watch import start_watcher

        observer = start_watcher(cfg)
    try:
        build_server(cfg).run("stdio")
    finally:
        if observer is not None:
            observer.stop()
    return 0


def cmd_import(cfg, args) -> int:
    from .importers.core import import_file

    code = 0
    with open_db(cfg) as conn:
        for f in args.files:
            result = import_file(
                conn, cfg, Path(f), account_name=args.account, profile=args.profile, dry_run=args.dry_run
            )
            r = result.as_dict()
            if args.dry_run:
                r["dry_run"] = True
            _print(r)
            if result.status == "failed":
                code = 1
    return code


def cmd_watch(cfg, args) -> int:
    import time

    from .watch import start_watcher

    observer = start_watcher(cfg)
    print(f"Watching {cfg.inbox} (Ctrl+C to stop)", file=sys.stderr)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    return 0


def cmd_accounts(cfg, args) -> int:
    from .analysis import list_accounts

    with open_db(cfg) as conn:
        if args.action == "list":
            _print(list_accounts(conn, cfg, include_archived=args.all)["accounts"])
        elif args.action == "add":
            if find_account(conn, args.name):
                print(f"account {args.name!r} already exists", file=sys.stderr)
                return 1
            add_account(conn, args.name, args.type, args.institution, args.currency or cfg.currency, scope=args.scope)
            print(f"added {args.name} ({args.scope})")
        elif args.action == "scope":
            cur = conn.execute("UPDATE accounts SET scope = ? WHERE name = ? COLLATE NOCASE", (args.scope, args.name))
            print(f"{args.name}: {args.scope}" if cur.rowcount else f"no account named {args.name!r}")
        elif args.action == "rename":
            conn.execute("UPDATE accounts SET name = ? WHERE name = ? COLLATE NOCASE", (args.new_name, args.name))
            print(f"renamed {args.name} -> {args.new_name}")
        elif args.action == "archive":
            conn.execute("UPDATE accounts SET archived = 1 WHERE name = ? COLLATE NOCASE", (args.name,))
            print(f"archived {args.name}")
    return 0


def cmd_profile(cfg, args) -> int:
    from .importers.csv_import import init_profile

    out = init_profile(Path(args.file), cfg, args.name)
    print(f"wrote {out}\nReview it, then: fiscl import {args.file} --account NAME")
    return 0


def cmd_rules(cfg, args) -> int:
    from .categorize import apply_rules, explain
    from .config import load_rules_raw
    from .normalize import load_aliases, normalize_merchant

    if args.action == "test":
        merchant = normalize_merchant(args.description, load_aliases(load_rules_raw(cfg)))
        _print(explain(cfg, args.description, merchant))
        return 0
    with open_db(cfg) as conn:
        _print(apply_rules(conn, cfg, recategorize=args.recategorize, dry_run=args.dry_run))
    return 0


def cmd_recurring(cfg, args) -> int:
    from .recurring import refresh_recurring
    from .transfers import match_transfers

    with open_db(cfg) as conn:
        _print({"transfers_matched": match_transfers(conn, cfg), "recurring_found": refresh_recurring(conn, cfg)})
    return 0


def cmd_sync(cfg, args) -> int:
    import getpass

    from .sync import simplefin

    try:
        if args.action == "setup":
            print(
                "Paste your SimpleFIN setup token (from https://beta-bridge.simplefin.org). Input is hidden.",
                file=sys.stderr,
            )
            token = getpass.getpass("Setup token: ")
            host = simplefin.claim(token)
            print(f"Connected to {host}. The access credential is stored in your OS keychain.")
        elif args.action == "run":
            with open_db(cfg) as conn:
                _print(simplefin.sync(conn, cfg, days=args.days))
        elif args.action == "forget":
            simplefin.forget()
            print("Removed the SimpleFIN credential from the keychain.")
    except simplefin.SyncError as exc:
        print(f"sync error: {exc}", file=sys.stderr)
        return 1
    return 0


def _ensure_teller_credentials(cfg) -> None:
    """Collect the user's Teller application id (hidden input) and, outside sandbox, their client certificate and
    key (read once from the files Teller issued, then kept only in the keychain; the paths are not retained)."""
    import getpass

    from .sync import base, teller

    need_app = not base.secret_get(base.secret_name("teller", "app_id"))
    need_cert = cfg.teller_environment != "sandbox" and not (
        base.secret_get(base.secret_name("teller", "cert")) and base.secret_get(base.secret_name("teller", "key"))
    )
    if not (need_app or need_cert):
        return
    app_id = (
        getpass.getpass("Teller application ID (hidden): ").strip()
        if need_app
        else base.secret_get(base.secret_name("teller", "app_id"))
    )
    cert = key = None
    if need_cert:
        print(
            "Teller needs the client certificate and private key it issued to your developer account\n"
            "(teller.io/settings/certificates). Give the file paths once; they are copied into your OS keychain.",
            file=sys.stderr,
        )
        cert, key = teller.read_pem_files(input("Certificate file: ").strip(), input("Private key file: ").strip())
    teller.store_app_credentials(app_id or "", cert, key)


def _ensure_plaid_credentials() -> None:
    """Collect the user's Plaid client_id and secret with hidden input; they go only to the keychain."""
    import getpass

    from .sync import plaid

    if plaid.has_app_credentials():
        return
    print(
        "Plaid keys are in your dashboard under Developers > Keys (use the secret for your environment).",
        file=sys.stderr,
    )
    plaid.store_app_credentials(
        getpass.getpass("Plaid client_id (hidden): ").strip(), getpass.getpass("Plaid secret (hidden): ").strip()
    )


def cmd_connect(cfg, args) -> int:
    import getpass

    from .sync import base, connect, connections, registry, simplefin
    from .sync.base import SyncError

    def say(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        if args.target == "list":
            with open_db(cfg) as conn:
                _print(registry.list_connections(conn, cfg))
        elif args.target == "forget":
            with open_db(cfg) as conn:
                revoked = registry.forget(conn, cfg, args.provider, args.institution)
            if args.institution:
                print(f"Disconnected {revoked} {args.provider} connection(s) matching that institution.")
            else:
                print(f"Disconnected {args.provider}.")
        else:
            base.keyring_backend()  # refuse before asking for anything
            if args.target == "plaid":
                if cfg.plaid_environment == "production":
                    say("Plaid environment is 'production': this needs your own approved Plaid app (Trial = 10 Items).")
                _ensure_plaid_credentials()
                with open_db(cfg) as conn:
                    connect.connect_plaid(cfg, conn, announce=say)
                print("Connected. Ask your assistant to sync_accounts, or run `fiscl sync run`.")
            elif args.target == "teller":
                if cfg.teller_environment == "production":
                    say("Teller environment is 'production' (needs Teller business verification).")
                _ensure_teller_credentials(cfg)
                with open_db(cfg) as conn:
                    connect.connect_teller(cfg, conn, announce=say)
                print("Connected. Ask your assistant to sync_accounts, or run `fiscl sync run`.")
            elif args.target == "simplefin":
                say("Paste your SimpleFIN setup token (from https://beta-bridge.simplefin.org). Input is hidden.")
                host = simplefin.claim(getpass.getpass("Setup token: "))
                with open_db(cfg) as conn:
                    ref = connections.item_ref_hash("simplefin", "access_url")
                    connections.add_connection(conn, "simplefin", "SimpleFIN Bridge", ref)
                print(f"Connected to {host}. The access credential is stored in your OS keychain.")
    except (SyncError, GuardError) as exc:
        print(f"connect error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_tax(cfg, args) -> int:
    from . import tax
    from .db import get_or_create_category

    with open_db(cfg) as conn:
        if args.action == "draft":
            _print(tax.schedule_c_draft(conn, cfg, year=args.year))
        elif args.action == "estimate":
            _print(tax.estimated_tax(conn, cfg, year=args.year, income_tax_rate_pct=args.rate))
        elif args.action == "review":
            _print(tax.review_writeoffs(conn, cfg, year=args.year))
        elif args.action == "map":
            if args.line not in tax.SCHEDULE_C_LINES:
                print(f"unknown line {args.line!r}; valid: {', '.join(tax.SCHEDULE_C_LINES)}", file=sys.stderr)
                return 1
            cat_id = get_or_create_category(conn, args.category)
            conn.execute("UPDATE categories SET schedule_c_line = ? WHERE id = ?", (args.line, cat_id))
            print(f"{args.category} -> line {args.line} ({tax.SCHEDULE_C_LINES[args.line]})")
        elif args.action == "trip":
            _print(tax.log_trip(conn, cfg, args.date, args.miles, args.purpose, args.vehicle, dry_run=False))
    return 0


def cmd_backup(cfg, args) -> int:
    with open_db(cfg) as conn:
        print(backup(conn, cfg, reason="manual"))
    return 0


def cmd_doctor(cfg, args) -> int:
    from .analysis import data_freshness
    from .categorize import load_rules

    problems, fixed = [], []
    warnings = [w for w in (cloud_sync_warning(cfg.home), disk_encryption_warning()) if w]
    for path in [cfg.home, cfg.inbox, cfg.logs, cfg.backups, cfg.profiles_dir]:
        if path.exists() and stat.S_IMODE(path.stat().st_mode) != DIR_MODE:
            os.chmod(path, DIR_MODE)
            fixed.append(f"permissions on {path}")
    private_files = [cfg.db_path, cfg.config_file, cfg.rules_file, cfg.budgets_file]
    private_files += [p for d in (cfg.logs, cfg.backups, cfg.profiles_dir) if d.exists() for p in d.iterdir()]
    private_files += [cfg.db_path.with_name(cfg.db_path.name + s) for s in ("-wal", "-shm")]
    for path in private_files:
        if path.is_file() and stat.S_IMODE(path.stat().st_mode) != FILE_MODE:
            os.chmod(path, FILE_MODE)
            fixed.append(f"permissions on {path}")
    try:
        load_rules(cfg)
        load_budgets(cfg)
    except Exception as exc:
        problems.append(str(exc))
    with open_db(cfg) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"database integrity: {integrity}")
        freshness = data_freshness(conn)
        failed = conn.execute("SELECT COUNT(*) FROM imports WHERE status = 'failed'").fetchone()[0]
    _print(
        {
            "home": str(cfg.home),
            "problems": problems,
            "warnings": warnings,
            "fixed": fixed,
            "failed_imports": failed,
            "data_freshness": freshness,
        }
    )
    return 1 if problems else 0


def cmd_demo(cfg, args) -> int:
    from .demo import load_demo

    with open_db(cfg) as conn:
        _print(load_demo(conn, cfg))
    print("Demo data loaded. Try: fiscl serve, then ask your MCP client about spending.", file=sys.stderr)
    return 0


def cmd_audit(cfg, args) -> int:
    path = cfg.logs / "audit.log"
    if not path.exists():
        print("no tool calls logged yet")
        return 0
    for line in path.read_text(encoding="utf-8").splitlines()[-args.n :]:
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fiscl", description="Local-first personal finance MCP server")
    p.add_argument("--home", help="data directory (default $FISCL_HOME or ~/fiscl)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the MCP server over stdio")
    s.add_argument("--no-watch", action="store_true", help="don't watch the inbox folder")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("import", help="import CSV/OFX/QFX files")
    s.add_argument("files", nargs="+")
    s.add_argument("--account", help="account name (required for CSV unless the profile sets one)")
    s.add_argument("--profile", help="force a CSV profile by name")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_import)

    sub.add_parser("watch", help="watch the inbox folder").set_defaults(fn=cmd_watch)

    s = sub.add_parser("accounts", help="manage accounts")
    a = s.add_subparsers(dest="action", required=True)
    x = a.add_parser("list")
    x.add_argument("--all", action="store_true")
    x = a.add_parser("add")
    x.add_argument("name")
    x.add_argument(
        "--type",
        required=True,
        choices=["checking", "savings", "credit", "brokerage", "loan", "crypto", "cash", "other"],
    )
    x.add_argument("--institution")
    x.add_argument("--currency")
    x.add_argument("--scope", default="personal", choices=["personal", "business", "mixed"])
    x = a.add_parser("scope", help="set an account to personal, business, or mixed")
    x.add_argument("name")
    x.add_argument("scope", choices=["personal", "business", "mixed"])
    x = a.add_parser("rename")
    x.add_argument("name")
    x.add_argument("new_name")
    x = a.add_parser("archive")
    x.add_argument("name")
    s.set_defaults(fn=cmd_accounts)

    s = sub.add_parser("profile", help="CSV mapping profiles")
    a = s.add_subparsers(dest="action", required=True)
    x = a.add_parser("init", help="generate a profile from a CSV header")
    x.add_argument("file")
    x.add_argument("--name")
    s.set_defaults(fn=cmd_profile)

    s = sub.add_parser("rules", help="categorization rules")
    a = s.add_subparsers(dest="action", required=True)
    x = a.add_parser("apply")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--recategorize", action="store_true", help="also redo rule-assigned rows")
    x = a.add_parser("test")
    x.add_argument("description")
    s.set_defaults(fn=cmd_rules)

    s = sub.add_parser("recurring", help="recurring detection")
    s.add_subparsers(dest="action", required=True).add_parser("refresh")
    s.set_defaults(fn=cmd_recurring)

    s = sub.add_parser("sync", help="read-only bank sync via SimpleFIN Bridge")
    a = s.add_subparsers(dest="action", required=True)
    a.add_parser("setup", help="connect with a SimpleFIN setup token (stored in the OS keychain)")
    x = a.add_parser("run", help="pull recent transactions")
    x.add_argument("--days", type=int)
    a.add_parser("forget", help="remove the stored credential")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("connect", help="connect a bank for read-only sync (human-only; never an MCP tool)")
    a = s.add_subparsers(dest="target", required=True)
    a.add_parser("teller", help="connect through Teller (recommended; needs your own Teller developer account)")
    a.add_parser("plaid", help="connect through Plaid (needs your own Plaid developer account)")
    a.add_parser("simplefin", help="connect with a SimpleFIN setup token")
    a.add_parser("list", help="show connected banks (no secrets)")
    x = a.add_parser("forget", help="revoke access and delete the stored credentials")
    x.add_argument("provider", choices=("simplefin", "teller", "plaid"))
    x.add_argument("--institution", help="only this institution")
    s.set_defaults(fn=cmd_connect)

    s = sub.add_parser("tax", help="business tax helpers (for you and your preparer; not advice)")
    a = s.add_subparsers(dest="action", required=True)
    for name in ("draft", "estimate", "review"):
        x = a.add_parser(name)
        x.add_argument("--year", type=int)
        if name == "estimate":
            x.add_argument("--rate", type=float, help="your income-tax planning rate, e.g. 22")
    x = a.add_parser("map", help="map a category to a Schedule C line")
    x.add_argument("category")
    x.add_argument("line")
    x = a.add_parser("trip", help="log a business trip")
    x.add_argument("date")
    x.add_argument("miles", type=float)
    x.add_argument("purpose")
    x.add_argument("--vehicle")
    s.set_defaults(fn=cmd_tax)

    sub.add_parser("backup", help="snapshot the database").set_defaults(fn=cmd_backup)
    sub.add_parser("doctor", help="check config, permissions, and data").set_defaults(fn=cmd_doctor)
    sub.add_parser("demo", help="load synthetic demo data").set_defaults(fn=cmd_demo)
    s = sub.add_parser("audit", help="show recent tool calls")
    s.add_argument("-n", type=int, default=30)
    s.set_defaults(fn=cmd_audit)
    sub.add_parser("version").set_defaults(fn=lambda cfg, args: print(__version__) or 0)
    return p


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # everything fiscl creates (db, WAL, logs, backups, exports) is private to this user
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(Path(args.home).expanduser() if args.home else None)
        ensure_layout(cfg)
        setup_logging(cfg, args.verbose)
        return args.fn(cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except GuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
