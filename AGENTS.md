# fiscl

Local-first personal + business finance MCP server. It imports bank exports (CSV/OFX/QFX) or imports bank exports (free), or syncs read-only through SimpleFIN ($1.50/month or $15/year), Plaid Trial, or Teller into a local SQLite DB, and exposes MCP tools for spending, cash flow, budgets, recurring charges, a cash forecast with safe-to-spend, in-chat charts, and sole-proprietor tax prep (Schedule C draft, write-off review, mileage, estimated tax). What matters most: **data stays local, numbers are deterministic, and nothing can move money.** The full product spec and roadmap are in `docs/PROJECT.md`.

## Stack
- Python 3.12+ · official MCP Python SDK `mcp>=2.3,<3` (`MCPServer`, `mcp.server.apps` for MCP Apps) · stdlib `sqlite3`, `tomllib`, `argparse`, `urllib` · `watchdog` · optional `keyring` (`fiscl[sync]`)
- Package manager: **uv** (`uv.lock`). Always use it; never add a requirements.txt or another lockfile.
- Runtime dependencies are deliberately minimal. Don't add one without a decision entry.

## Commands
| Task | Command |
|---|---|
| Install | `uv sync` |
| Run server (stdio) | `uv run fiscl serve` (add `--home DIR` to use a throwaway data dir) |
| Demo data | `uv run fiscl --home /tmp/fiscl-demo demo` |
| Lint + format check | `uv run ruff check . && uv run ruff format --check .` |
| Format | `uv run ruff format .` |
| Unit tests | `uv run pytest -q` (~6s; `-m 'not smoke'` skips the stdio smoke test) |
| Smoke (every tool over stdio) | `uv run python scripts/smoke.py` |
| Build | `uv build` |

## Layout
```
src/fiscl/
  server.py        MCP wiring: one thin wrapper per tool → run() guard (open DB, call service, redact, audit)
  analysis/        read services + categorization edits, one module per area:
    transactions.py  query_transactions, list_accounts, import_status, list_uncategorized
    spending.py      spending_summary, cash_flow, merchant_history, category_trend
    budgets.py       budget_status, suggest_budgets, set_budget
    forecast.py      forecast_cash (+ card payment projection)
    recurring_report.py  list_recurring (detection itself is fiscl/recurring.py)
    edits.py         apply_categories, add_note, tag_transactions, split_transaction, dismiss_recurring
    planning.py      manage_asset, net_worth, afford, debt_payoff, manage_goal, goal_progress
    monitoring.py    find_anomalies, subscription_watchdog, refund_tracker
  tax/             lines (constants, DISCLAIMER, line_for) · schedule_c · estimates · review (write-off questions) · edits (set_scope, log_trip, mileage_log) · home_office (simplified method, line 30) · export (`export_schedule_c`, files only under `<data dir>/exports/`)
  common.py        shared service helpers: meta, with_meta, money_arg, account_id/find_account, scope/txn filters, year_bounds,
                   resolve_target, dry-run plan_result, bulk limits
  db.py            schema migrations, TXN_FROM join, SCOPE_SQL / SPENDING_FILTER / INCOME_FILTER, backups
  guard.py         validation, redaction, untrusted-text cleaning, safe regex, audit log
  config.py        data dir layout, config/rules/budgets TOML, tax settings
  charts.py        chart specs + MCP Apps HTML view (no external requests)
  importers/       core pipeline, csv_import (profiles), ofx, types
  sync/            read-only bank connectors (only package that may use the network):
    base.py          allowlisted no-redirect request helper, keychain helpers, mTLS, Connector protocol
    simplefin.py · plaid.py · teller.py   one connector per provider (fixed route allowlists)
    connect.py       human-only localhost connect flow (`fiscl connect plaid|teller [--reconnect]`)
    connections.py   the `connections` table (provider, institution, hashed ref, status, Plaid cursor; never a token)
    registry.py      build connectors, sync_accounts, list_connections, forget
  installkey.py    per-install random key for the keyed account hash (OS keychain, else 0600 `.install_key`)
  normalize.py · categorize.py · transfers.py · recurring.py · money.py · watch.py · demo.py · cli.py
scripts/smoke.py   dev-only: demo data + every tool/resource/prompt over real stdio (`uv run python scripts/smoke.py`)
tests/             pytest; conftest has `cfg`, `conn`, `demo` fixtures, `add_txn` helper, fixed TODAY
docs/PROJECT.md    product spec + roadmap (v1 done, v2 planned)
```

## Conventions
- **Service signature:** `fn(conn, cfg, **args) -> dict`, pure functions over a sqlite3 connection. Accept `today=None` for anything date-relative so tests can pin it. Register the tool in `server.py` with a thin wrapper that calls `run("tool_name", service, **kwargs)`, and re-export from `analysis/__init__.py`.
- **Money:** always integer minor units in the DB and in math. Respond with `amount(minor, currency)` → `{"minor", "display"}`. Parse user dollars with `common.money_arg`. Use `Decimal` for rates and banker's rounding. Never floats for money.
- **Every response** includes `meta` from `common.meta(...)` (currency, data_freshness, period, row_count, total_count/truncated when capped, note). Return the totals you describe, so the model never adds.
- **SQL:** parameterized only. Use `FROM {TXN_FROM}` (the split-aware `txn_lines` view) for aggregates and `TXN_BASE_FROM` for row listings, balances, and edits so the `t`/`a`/`c` aliases that `SCOPE_SQL`, `SPENDING_FILTER`, and `INCOME_FILTER` need are present. Use `common.txn_filter(account_id, scope)` for optional account/scope restrictions.
- **Bad input:** raise `GuardError("message safe to show the model", code="not_found|sum_mismatch|needs_terms|exists|invalid|sync_failed|key_missing")`; `run()` returns the redacted message to the model and logs only the code. Never put merchant names, amounts, or asset/goal/account names in error text; put names in structured response fields with fixed prose. Validate enums with `guard.choice`, dates with `guard.date_range`/`parse_month`, row caps with `guard.clamp_limit`.
- **Local writes:** `dry_run=True` by default, return `common.plan_result(...)`, cap bulk edits with `check_items`/`check_bulk`, and annotate the tool `LOCAL_DESTRUCTIVE` if it can remove or overwrite user data, `LOCAL_WRITE` only if purely additive. `dry_run` is a safety default, not a gate; the human gate is the MCP client's tool approval. TOML edits go through `config.append_toml_block` (keeps `.bak` + provenance).
- **Files fiscl writes for the user** (exports, digests) go only under fixed folders in the data dir, opened with `O_CREAT|O_EXCL|O_NOFOLLOW` at 0600; never accept a caller-supplied path. Escape CSV formula prefixes and Markdown in bank-derived text.
- **Untrusted text:** bank descriptions go through `guard.clean_text` and are returned only as labeled data fields, never interpolated into prompts or instructions.
- **Tax output** carries `DISCLAIMER` and frames findings as questions for the user and their preparer.
- **Style:** ruff (line length 120, rules E/F/I/B/UP/SIM), `from __future__ import annotations`, module docstrings, sparse comments that explain why.
- **Tests:** every guardrail has a test. New tools get service tests (pinned `today`) plus an MCP contract check in `tests/test_mcp.py`. Use TDD for bug fixes.

## Environments
- Local only. Runtime data lives in `~/fiscl` or `$FISCL_HOME` (or `--home`): `fiscl.db` (0600), TOML config, inbox, logs, backups.
- Distribution target: PyPI as `fiscl` (`uvx fiscl serve`), not published yet (pending trademark check). No hosting or deploy.
- No env vars or secrets in the repo. SimpleFIN access URLs live only in the OS keychain.

## Project memory
- **Decisions:** `docs/decisions/DECISIONS.md`. Read recent entries before changing architecture or conventions, and don't contradict them silently.
- **Plans/specs:** `specs/` (active) and `specs/archive/` (done or superseded). Only follow plans with status draft, approved, or in-progress.
- **Product spec + roadmap:** `docs/PROJECT.md`. Tick roadmap boxes in §15 when a feature lands.
- **Research:** `docs/research/`.

## Never
- Add any code path that moves money, pays, transfers, or writes to a financial institution.
- Make network calls from the core (only `sync/` may, https + allowlisted hosts, through the no-redirect `open_url` with credentials in unredirected headers). `tests/test_guardrails.py` blocks sockets.
- Recommend providers out of order: file import (free) → SimpleFIN → Plaid Trial → Teller (existing Teller access only; no public signup). Plaid and Teller stay marked beta until live verification passes (DECISIONS 2026-10-11).
- Request or call any bank/aggregator product or endpoint that can move money or reveal full account numbers (Plaid `auth`/`transfer`/`payment_initiation`, Teller `payments`). Bank connections are set up by the human through the CLI, never through an MCP tool.
- Render bank-derived text as HTML in the chart view (`innerHTML`); use `textContent`. Keep its CSP strict.
- Create files without private permissions, or use `$HOME` itself as the data dir.
- Print to stdout from server code paths: stdout carries the MCP protocol. Log to files.
- Store full account numbers, secrets in files (the one exception is the random HMAC key `.install_key`, 0600, used only when no OS keychain exists), or credentials in URLs.
- Use floats for money, or f-string user values into SQL.
- Commit real bank exports or `.db` files. Test fixtures must be synthetic.
- Commit secrets or real `.env` files; push to `main` directly; publish to PyPI without the human.
