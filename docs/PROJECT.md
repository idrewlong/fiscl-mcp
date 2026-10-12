# fiscl — Personal + Business Finance MCP Server

> A local-first finance server that speaks the Model Context Protocol (MCP), so any MCP client (Claude Desktop, Claude Code, Cursor, etc.) can answer questions about your money, for your household and your business, from your own data on your own machine.

Status: **v1 working** (190 tests passing; v2 Phase 1 done). Checked boxes in §15 are implemented.

---

## 1. Overview

fiscl ingests bank, card, and business exports (or syncs read-only through SimpleFIN Bridge) into a local SQLite database. It exposes opinionated MCP tools for:

- spending, cash flow, budgets, and subscriptions
- a forward-looking cash forecast with a safe-to-spend number
- charts that render inside the chat
- sole-proprietor tax prep: Schedule C draft, write-off review, mileage log, and quarterly estimated tax

It follows the same shape as stock-analysis MCP servers like Maverick:

| Maverick (stocks)                     | fiscl (personal + business finance)                       |
|---------------------------------------|-----------------------------------------------------------|
| Runs locally, any MCP client          | Runs locally, any MCP client                              |
| Free market data, no API key          | File imports (CSV/OFX/QFX), no API key                    |
| Technical analysis, screening         | Spending, cash flow, recurring charges, budgets, forecast |
| Portfolio tracking                    | Accounts, balances, business vs personal                  |
| Optional: backtesting, LLM research   | Optional: read-only bank sync, tax prep, QuickBooks (v2)  |

### Goals

- **Zero-config core:** drop exports in a folder and ask questions.
- **One store for personal and business money**, with a clear line between them.
- **All data stays local.** There's no telemetry, and the core makes no network calls.
- **Deterministic numbers:** SQL and integer math compute everything; the LLM only interprets.
- **Easy install:** `uvx fiscl` / `pipx install fiscl`. Useful on day one with the demo data.

### Non-goals

- Moving money, paying bills, trading, or any write to a financial institution. Ever.
- Being a double-entry accounting system, payroll, or invoicing tool.
- Giving financial, tax, or investment advice. fiscl organizes records for the user and their preparer.
- A web UI in v1/v2. The MCP client is the UI, charts render in-chat, and a CLI covers admin tasks.

---

## 2. Landscape and positioning (researched Oct 2026)

**Name.** `ledgr` (the original working name) is taken on PyPI and npm. It's also used by at least four finance products, including an iOS "Ledgr: Expense & Tax Tracker" with a near-identical pitch. `fiscl` was free on PyPI and npm at the time of checking. **To do before publishing:** a USPTO trademark search, plus checks of the GitHub org and Homebrew formula names.

**Closest competitor: [ledgerline](https://pypi.org/project/ledgerline/)** (MIT, Python, v0.3.3, June 2026). It's a local-first pipeline for CSV/OFX/QFX imports and SimpleFIN sync, with agent-driven categorization, recurring detection, personal/business/mixed account tags, and a read-only SQL tool. Its listing doesn't mention forecasting, budgets, charts, or Schedule C. **Worth reading its code before v2.**

**Other servers:**
- **Official, mostly business and hosted:** QuickBooks Online, Xero, Stripe, Mercury, Ramp, Beancount.io. Several of them can move money.
- **Community wrappers that need their app running:** YNAB, Monarch (via unofficial login), Lunch Money, Actual, Firefly III, Copilot.

**fiscl's differentiators** (nothing local found that does these):
1. A deterministic cash-flow forecast and safe-to-spend number, including projected credit-card payments.
2. Budgets with pace projection, rollover, and suggested budgets.
3. Interactive charts in the chat, via MCP Apps.
4. Sole-proprietor tax prep without QuickBooks: Schedule C line mapping, a write-off review that asks questions, a mileage log, and a quarterly estimate.
5. Guardrails as a feature: dry-run writes, redaction, an audit log, a host allowlist, and keychain-only secrets.

---

## 3. Design Principles

1. **Local-first.** The database, config, and logs live in one private data directory.
2. **Read-only to the outside world.** Writes touch only the local DB and local config files.
3. **SQL computes, the model narrates.** Every tool returns the totals it describes.
4. **Summaries over dumps.** Aggregates by default; raw rows are capped.
5. **Untrusted text is data.** Bank descriptions are never treated as instructions.
6. **Questions, not conclusions, on tax.** fiscl flags and organizes; the user and their preparer decide.
7. **Boring, durable formats:** SQLite, TOML, CSV.

---

## 4. Architecture

```
 ┌──────────────┐      stdio (v1)       ┌────────────────────────────────────────────┐
 │  MCP client  │ ◄──────────────────►  │                fiscl server                │
 │ (Claude etc) │  streamable HTTP (v2) │  tools · resources · prompts · chart app   │
 └──────────────┘   localhost + token   │        │                                   │
                                        │        ▼                                   │
                                        │  guard: validate → run → redact → audit    │
                                        │        │                                   │
                                        │  analysis · tax · charts · recurring       │
                                        │        │                                   │
                                        │     SQLite (fiscl.db, 0600)                │
                                        │        ▲                                   │
                                        │  importers: CSV · OFX/QFX · SimpleFIN      │
                                        └────────┼───────────────────────────────────┘
                                                 │
                       ~/fiscl/inbox/ (watched)  │  SimpleFIN Bridge (read-only, allowlisted host)
```

### Data flow

1. Data comes in one of three ways:
   - you drop exports in `~/fiscl/inbox/`
   - you run `fiscl import <file>`
   - you run `fiscl sync run`, or the client calls `sync_accounts`
2. The importer detects the format, resolves the account (never guessing in the watcher), normalizes merchants, dedupes, and writes everything inside one savepoint.
3. Post-import jobs run in order: transfer matching first, then rules, then recurring detection.
4. Processed files move to `inbox/processed/`. Failed ones go to `inbox/failed/` with a `.error.txt` note.
5. Each tool call goes through `run()`: open the DB, validate, compute, redact, audit. Bad input becomes an MCP tool error the model can read.

---

## 5. Tech Stack

| Concern        | Choice                                                     | Why |
|----------------|------------------------------------------------------------|-----|
| Language       | Python 3.12+                                               | Best data tooling; most MCP examples |
| MCP            | Official SDK `mcp>=2.3` (`MCPServer`, `mcp.server.apps`)   | Spec-tracking; MCP Apps built in |
| Database       | SQLite via stdlib `sqlite3` (WAL, foreign keys)            | Zero dependencies |
| Migrations     | Ordered SQL list + `schema_version`; backup before migrating | No external tooling |
| Config         | TOML via stdlib `tomllib`                                  | Human-editable rules and budgets |
| CLI            | stdlib `argparse`                                          | No dependencies |
| File watching  | `watchdog`                                                 | Cross-platform inbox |
| Bank sync      | SimpleFIN over stdlib `urllib`; `keyring` extra for secrets | Read-only protocol; OS keychain |
| Money          | `int` minor units + `Decimal` for parsing and rates        | Never floats |
| Logging        | `logging` to a rotating file; audit log as JSONL           | stdout is reserved for MCP |
| Tooling        | `uv`, `ruff`, `pytest` + `pytest-asyncio`                  | Fast, standard |

Runtime dependencies: `mcp`, `watchdog` (plus `keyring` with `fiscl[sync]`).

---

## 6. Repository Layout

```
fiscl/
├── src/fiscl/
│   ├── cli.py            # serve, import, watch, accounts, profile, rules, recurring, sync, tax, backup, doctor, demo, audit
│   ├── server.py         # MCPServer wiring: tools, resources, prompts, chart app, run() guard wrapper
│   ├── config.py         # data dir layout, config/rules/budgets TOML, tax settings
│   ├── db.py             # schema + migrations, backups, category/account helpers, shared SQL filters
│   ├── money.py          # parse/format minor units, pct
│   ├── guard.py          # validation, redaction, untrusted-text cleaning, regex safety, audit log
│   ├── normalize.py      # merchant cleanup + aliases
│   ├── categorize.py     # rules engine
│   ├── transfers.py      # inter-account transfer matching
│   ├── recurring.py      # subscription/bill/income detection
│   ├── analysis/         # transactions, spending, budgets, forecast, recurring_report, edits (one module per area)
│   ├── common.py         # shared service helpers: meta, filters, edit targets, dry-run plans
│   ├── tax.py            # Schedule C draft, estimated tax, write-off review, scope, mileage
│   ├── charts.py         # chart specs + MCP Apps HTML view (no external requests)
│   ├── watch.py          # inbox watcher
│   ├── demo.py           # synthetic personal + business demo data
│   ├── importers/        # core pipeline, csv_import (profiles), ofx, types
│   └── sync/simplefin.py # read-only bank sync
├── tests/                # 190 tests: importers, analysis, tax, MCP contract, stdio, guardrails, SimpleFIN
├── examples/claude_desktop_config.json
├── PROJECT.md · README.md · SECURITY.md · LICENSE
└── pyproject.toml
```

### Data directory (runtime, `~/fiscl` or `$FISCL_HOME`)

```
fiscl.db (0600) · config.toml · rules.toml · budgets.toml (0600)
inbox/{processed,failed}/ · profiles/*.toml · logs/{fiscl.log,audit.log} · backups/ (last 14) · demo/
```

---

## 7. Data Model

All amounts are integer minor units. Negative means money out, from the account holder's view.

| Table | Purpose |
|---|---|
| `accounts` | name, institution, type (checking/savings/credit/brokerage/loan/crypto/cash/other), currency, `acct_hash` (SHA-256 of the institution account id; the raw number is never stored), `last4`, `is_asset`, `archived`, **`scope` (personal/business/mixed)** |
| `transactions` | account, posted date, amount, raw description, normalized merchant, memo, category + `category_source` (rule/manual/transfer/none), `transfer_group`, pending flag, `fitid`, `dedupe_key` (unique per account), import id, note, **`scope` override** |
| `categories` | name, kind (expense/income/transfer/ignore), tax_tag, **`schedule_c_line`** |
| `balances` | account, as-of date, amount, source (ofx/csv/sync/manual/derived) |
| `recurring` | merchant, account, direction, cadence, typical/last amount, next expected, confidence, status (active/price_changed/missed/cancelled), kind (subscription/bill/income) |
| `recurring_dismissed` | user-dismissed recurring items, preserved across refreshes |
| `imports` | filename/label, sha256 (unique), format, row counts, status, error |
| `trips` | mileage log: date, miles in tenths, purpose, vehicle |

A transaction's effective scope is its own `scope`, else its account's (mixed accounts count as personal until marked).

Planned (v2): `holdings`, `goals`, `tags`, `splits`, `receipts`.

---

## 8. Ingestion

### 8.1 Sources

| Source | Status | Notes |
|--------|--------|-------|
| CSV with mapping profiles | v1 ✅ | Built-in: chase-checking, chase-credit, generic, generic-debit-credit. User profiles in `~/fiscl/profiles/`; `fiscl profile init <csv>` writes a starter profile |
| OFX 1.x (SGML), 2.x (XML), QFX | v1 ✅ | Tolerant tag parser; balances from LEDGERBAL; multiple statements per file |
| **SimpleFIN Bridge (read-only bank sync)** | v1 ✅ | About $15/year per user, no developer approval. Setup token → access URL stored in the OS keychain; https + host allowlist; 90-day windows |
| Teller / Plaid (bring your own keys) | v2 | Read-only scopes only. Plaid's free trial covers 10 live connections; Teller's free tier covers 100 |
| QuickBooks Online (read-only) | v2 | Invoices, A/R, and P&L, so "income" can include invoiced-but-unpaid. Intuit developer app + OAuth |
| Amazon order history | v2 | Splits "AMZN $214" into real items and categories |
| Brokerage holdings CSV, crypto (Solana tax engine export) | v2 | Positions and cost basis |
| QIF | v2 | Legacy |
| Receipts (PDF/image) | later | Attach to transactions for tax records |

### 8.2 Dedupe

- **File level:** the file's SHA-256. A failed file can be retried.
- **Row level:** OFX/SimpleFIN `FITID`, or `sha256(date | amount | raw description | occurrence index)`. The index keeps two identical same-day coffees as separate rows. The key uses the raw description, so editing aliases never creates duplicates.
- **Overlapping exports** dedupe cleanly.
- **Pending → posted:** a posted row replaces a matching pending row from up to 7 days earlier.
- **Repeated sync payloads** are skipped.

### 8.3 Merchant normalization

Strips processor prefixes (`SQ *`, `TST*`, `POS DEBIT`, `PURCHASE AUTHORIZED ON`), ACH codes (`PPD ID`), store numbers, phone numbers, reference ids, inline dates, masked cards, and trailing state codes. Then it applies smart title-casing (`B&H Photo`, `Netflix.com`) and user aliases from `rules.toml`.

### 8.4 Transfer matching

Pairs opposite, equal amounts in different accounts within ±3 days (configurable). A pair matches only when one side has a transfer hint (`TRANSFER`, `PAYMENT THANK YOU`, `ONLINE PMT`, …) or one account is a credit card or loan. Matched pairs are excluded from spending and income.

---

## 9. Categorization

- **Rules (`rules.toml`):** first match wins. Matchers are `merchant`, `description_regex` (length-limited, nested quantifiers rejected), `account`, `amount_gt/lt/eq`, and `day_of_month`. Defaults ship for Amazon, salary, rent/mortgage, utilities, subscriptions, and fuel. Manual and transfer categorizations are never overwritten.
- **LLM-assisted, no API key:** `list_uncategorized` → the client proposes categories → `apply_categories` (dry run by default) → apply. Setting `create_rule` appends the rule to `rules.toml`, with a `.bak` and a provenance comment.
- **Default categories:**
  - Personal: Income, Housing, Utilities, Groceries, Dining, Transport, Auto, Health, Insurance, Shopping, Subscriptions, Entertainment, Travel, Education, Gifts & Donations, Fees & Interest, Taxes, Transfers, Ignore.
  - Business: a "Business: …" set, each mapped to a Schedule C line (see §11).

---

## 10. Budgets, Forecast, Charts

- **Budgets** (`budgets.toml`):
  - monthly per category, with optional rollover
  - `budget_status` gives spent, remaining, % used, projected month-end at the current pace, status (on_track/at_risk/over), and unbudgeted spending
  - `suggest_budgets` uses the median of the last N full months, rounded up to $10
  - *v2:* zero-based/envelope mode, `set_budget` via MCP, and pace alerts in the weekly digest
- **Cash forecast** (`forecast_cash`, 7–120 days):
  - starts from the latest balance of checking, savings, and cash accounts, rolled forward
  - adds detected recurring bills and income on those accounts
  - adds **projected credit-card payments**: the next one is charges since the last payment, later ones are the typical payment
  - optionally adds typical everyday spending (90-day average of non-recurring spending)
  - returns the lowest balance and its date, whether it goes negative, upcoming items, the daily balance series, and **safe-to-spend**: the most you can spend today without the projection ever dropping below your buffer
  - missed income is never counted; overdue bills are assumed to land tomorrow
  - every response lists its assumptions
- **Charts** (`show_chart`):
  - kinds: spending by category, spending by month, cash flow, forecast, budget
  - the MCP Apps view is plain HTML/SVG/JS with no external requests, so it works under the host's default CSP
  - it follows the host's theme variables and supports light and dark
  - clients without MCP Apps get the same numbers as structured data

---

## 11. Business & Tax (sole proprietors)

Everything here is framed as **organizing records for you and your tax preparer**, not advice. Every response carries that note.

- **Scope filters:** `query_transactions`, `spending_summary`, `cash_flow`, and `show_chart` accept `scope` (all/personal/business). `forecast_cash` defaults to personal, so business cash never inflates your personal safe-to-spend.
- **Scope:** accounts are personal, business, or mixed. Transactions can override their account's scope. `set_scope` (dry run by default) changes accounts, merchants, or transactions; the CLI has `fiscl accounts add --scope business` and `fiscl accounts scope NAME business`.
- **Schedule C mapping:** official 2025 line names (8 Advertising … 27b Other expenses; recheck against the 2026 form when it's out).
  - "Business: …" categories carry an explicit line.
  - Everyday categories on business-scope rows fall back to a likely line: Utilities→25, Dining→24b, Travel→24a, Subscriptions→27b, Auto→9, Shopping→22.
  - Override with `fiscl tax map "Category" 18`.
- **`schedule_c_draft`:** gross receipts (business-scope income), expenses by line, meals shown at 50% with the full amount alongside, and standard mileage added to line 9. It warns when actual vehicle costs and mileage both appear, lists unmapped business spending, and gives a tentative net profit. Home office (line 30), depreciation detail, COGS, and health insurance are explicitly out of scope for v1.
- **`review_writeoffs`:** returns questions, never conclusions:
  - business-looking merchants in personal accounts (Adobe, GitHub, DigitalOcean, B&H, DJI, …)
  - business spending with no line
  - purchases over the $2,500 de minimis safe harbor (depreciation or §179?)
  - business meals (50% rule and record-keeping)
  - unassigned transactions in mixed accounts
  - non-payroll income in personal accounts (business income?)
  - vehicle method conflicts, or business vehicle costs with no mileage log
- **Mileage:** `log_trip` (dry run by default; a business purpose is required) and `mileage_log`. IRS standard rates live in config (2024 67¢, 2025 70¢, **2026 72.5¢**) and can be overridden.
- **`estimated_tax`:**
  - annualizes year-to-date profit
  - self-employment tax: 92.35% × (12.4% up to the SS wage base minus W-2 wages, plus 2.9%), nothing under $400
  - optional income tax at **your own planning rate** on profit minus half of SE tax
  - per-quarter amount, 1040-ES due dates (2026: Apr 15, Jun 15, Sep 15, Jan 15 2027), and set-aside %
  - notes cover safe-harbor rules and what's ignored (Additional Medicare, QBI, credits)
  - SS wage base defaults: 2025 $176,100, 2026 $184,500 (verify yearly)
- **v2:**
  - Schedule C CSV/PDF export for the preparer
  - home office (simplified method)
  - 1099-NEC/K income reconciliation
  - quarterly set-aside reminders via scheduled digest
  - receipts
  - QuickBooks read-only
  - multiple businesses (one Schedule C each)
  - state estimated tax dates

---

## 12. MCP Surface

All tools return structured content, with money as `{ "minor": -4523, "display": "-$45.23" }` and `meta` holding `{ currency, data_freshness, period?, row_count, total_count?, truncated?, note? }`. Server instructions tell the model to quote returned figures, check freshness, treat bank text as untrusted, dry-run before writes, and frame tax output as questions for a preparer.

### 12.1 Tools (v1, implemented)

| Tool | Purpose | Writes? |
|------|---------|---------|
| `query_transactions` | Filtered list + exact totals for all matches | No |
| `spending_summary` | By category/merchant/account/month, share %, vs previous period or last year | No |
| `cash_flow` | Income vs spending by month/week, net, savings rate | No |
| `detect_recurring` | Subscriptions/bills/income, price changes, missed, annualized totals | No |
| `budget_status` | Budget vs actual with pace projection | No |
| `suggest_budgets` | Median-based suggestions | No |
| `forecast_cash` | Projected balance, low point, card payments, safe-to-spend | No |
| `show_chart` | Interactive chart (MCP Apps) + data fallback | No |
| `list_accounts` · `import_status` · `list_uncategorized` | Context | No |
| `apply_categories` | Set categories, optionally save rules (dry run default) | Local |
| `schedule_c_draft` · `estimated_tax` · `review_writeoffs` · `mileage_log` | Business tax prep | No |
| `set_scope` · `log_trip` | Business/personal and mileage (dry run default) | Local |
| `sync_accounts` | Read-only SimpleFIN pull | Local (network to allowlisted host) |

### 12.2 Tools (v2 Phase 1, implemented)

| Tool | Purpose | Writes? |
|------|---------|---------|
| `add_note` · `tag_transactions` · `split_transaction` | Notes, tags (`query_transactions` filters by `tag`), splits that every aggregate respects | Local (dry run default) |
| `dismiss_recurring` · `set_budget` | Hide a recurring item; edit `budgets.toml` | Local (dry run default) |
| `manage_asset` · `manage_goal` | Manual assets/liabilities; savings goals | Local (dry run default) |
| `net_worth` | Accounts + manual assets, change vs 30/90/365 days | No |
| `afford` | "Can I afford X?" against the cash forecast | No |
| `debt_payoff` | Avalanche vs snowball, to the cent | No |
| `goal_progress` | Progress, required monthly, on-track by pace | No |
| `find_anomalies` | Duplicates, spikes, new merchants, fees, category spikes (as questions) | No |
| `subscription_watchdog` | Trials turned paid, duplicate services, price changes, annual renewals | No |
| `refund_tracker` | Refunds received and refunds still expected (`refund-pending` tag) | No |
| `merchant_history` · `category_trend` | Monthly history and trend direction | No |

Still planned: `holdings`, `export_report`, `export_schedule_c`, bank connectors (see `specs/2026-10-11-1948-v2-roadmap.md`).

### 12.3 Resources

v1: `fiscl://accounts`, `fiscl://categories`, `fiscl://rules`, `fiscl://budgets`, `fiscl://summary/current-month`, `ui://fiscl/chart.html`.
v2 (done): `fiscl://goals`, `fiscl://recurring`. Planned: `fiscl://tax/{year}`.

### 12.4 Prompts

v1: `monthly_review`, `subscription_audit`, `categorize_backlog`, `cash_forecast`, `tax_checkup`.
v2 (done): `goal_check_in`. Planned: `spending_deep_dive`, `year_in_review`, `weekly_digest`.

---

## 13. CLI

```
fiscl serve [--no-watch]                      # MCP over stdio (+ inbox watcher)
fiscl import FILE... [--account NAME] [--profile P] [--dry-run]
fiscl watch
fiscl accounts list|add|rename|archive|scope
fiscl profile init CSV [--name N]
fiscl rules apply [--dry-run] [--recategorize] | rules test "DESCRIPTION"
fiscl recurring refresh
fiscl sync setup|run [--days N]|forget         # SimpleFIN; token read with hidden input
fiscl tax draft|review|estimate [--year Y] [--rate R] | tax map CATEGORY LINE | tax trip DATE MILES PURPOSE
fiscl backup · fiscl doctor · fiscl demo · fiscl audit [-n N] · fiscl version
```

---

## 14. Guardrails

Each one has a test (see `tests/test_guardrails.py`, `test_mcp.py`, `test_simplefin.py`).

1. **Read-only to the outside world.** No code path moves money. The schema has no payee or destination concept. SimpleFIN is read-only by protocol.
2. **Local writes are deliberate.** `apply_categories`, `set_scope`, and `log_trip` default to `dry_run=true` and return a plan. Bulk edits are capped at 500 rows. Imports are atomic (savepoint). The DB is backed up before migrations and daily while serving (last 14 kept). TOML edits write a `.bak` plus a provenance comment and are re-validated.
3. **Secrets.** The SimpleFIN access URL lives only in the OS keychain. With no keychain, sync refuses rather than storing plain text. The setup token is read with hidden input, never from argv. Credentials go in an Authorization header, never the URL.
4. **Network allowlist.** Sync requires https and an allowlisted host (`bridge.simplefin.org`, `beta-bridge.simplefin.org`). The core makes no network calls (a test blocks sockets while running every analysis path).
5. **Redaction.** Full account numbers are never stored. Every tool response is scrubbed for Luhn-valid card numbers, SSNs, and 9+ digit runs. `redact_descriptions` hides raw descriptions and memos (normalized merchant names, which can include P2P counterparty names, are still returned).
6. **Determinism.** Integer cents, Decimal rates, banker's rounding. Every summary includes its totals and counts, and freshness travels with every response.
7. **Untrusted input.** Descriptions are NFC-normalized, stripped of control and bidi characters, and capped at 256 characters. They're returned only in labeled data fields, never interpolated into prompts.
8. **Output size.** Default 50 rows, hard cap 500, with `truncated` and true totals reported; aggregates use top-N plus "Other".
9. **Input validation.** ISO dates, ranges ≤ 10 years, enums, safe regex, and parameterized SQL everywhere.
10. **Privacy and storage.** The data dir is 0700 and files are 0600; `doctor` checks and fixes them. Encryption at rest relies on OS disk encryption (FileVault, BitLocker, LUKS).
11. **Audit log.** JSONL of tool, allowlisted args only (merchant and text values show as `<set>`), row count, duration, and error. View it with `fiscl audit`.
12. **Not advice.** Tax tools carry the preparer framing. The forecast states its assumptions. Estimated tax uses the user's own planning rate.
13. **stdio safety.** Logs go to files only. A test runs a real stdio session through the CLI.

---

## 15. Roadmap

### v1 — Local core + business basics

**Ingestion**
- [x] CSV profiles + `profile init`; OFX/QFX with balances
- [x] File and row dedupe, overlapping exports, pending → posted
- [x] Inbox watcher (processed/failed, never guesses accounts)
- [x] Merchant normalization + aliases; transfer matching
- [x] **SimpleFIN read-only bank sync** (keychain, allowlist, windows)

**Categorization**
- [x] Default personal + business categories, rules engine, `rules test`
- [x] `list_uncategorized` → `apply_categories` (dry run, rule creation)

**Analysis**
- [x] `query_transactions`, `spending_summary` (comparisons), `cash_flow`
- [x] `detect_recurring` (cadence, price change, missed/cancelled, annualized)
- [x] Budgets with pace projection, rollover, `suggest_budgets`
- [x] **Cash forecast + safe-to-spend** (incl. card payments)
- [x] **In-chat charts** (MCP Apps) with data fallback

**Business & tax**
- [x] Personal/business/mixed scope, `set_scope`
- [x] Schedule C line mapping + `schedule_c_draft`
- [x] `review_writeoffs`, mileage log, `estimated_tax`

**MCP / ops**
- [x] stdio server, resources, 5 prompts
- [x] CLI, backups, doctor, audit, demo data
- [x] Guardrails §14 with tests
- [ ] Publish to PyPI as `fiscl` (after trademark check); `uvx fiscl` docs
- [ ] GitHub Actions: ruff + pytest on macOS/Linux/Windows
- [ ] Verify the chart view in Claude Desktop and other MCP Apps hosts

### v2 — Connected, forward-looking, preparer-ready

**Data**
- [x] Teller and Plaid connectors (bring your own keys, read-only; human live checks pending, see DECISIONS)
- [ ] **QuickBooks Online read-only** (invoices, A/R, P&L)
- [ ] Amazon order-history splitting
- [ ] Holdings and crypto import
- [ ] QIF
- [ ] Multi-currency with FX snapshots

**Planning**
- [x] `net_worth` with manual assets
- [x] Goals
- [x] `debt_payoff`
- [x] `afford` what-if
- [ ] Zero-based/envelope budgets
- [x] `set_budget`

**Monitoring**
- [x] `find_anomalies`
- [x] Subscription watchdog: trials converting to paid, duplicate services
- [x] Refund tracker
- [ ] Weekly digest (Markdown file or Claude scheduled task)

**Tax**
- [ ] Schedule C export (CSV/PDF)
- [ ] Home office (simplified)
- [ ] 1099 reconciliation
- [ ] Multiple businesses
- [ ] State estimates
- [ ] Receipts

**Editing**
- [x] Splits
- [x] Notes and tags
- [x] Dismiss recurring

**MCP**
- [ ] Streamable HTTP on localhost with a keychain bearer token
- [ ] Resource change notifications after imports

### Later / maybe

- Optional local dashboard
- Household mode (multiple people)
- Pluggable importers
- Encrypted DB (SQLCipher) as an extra
- Contribute or align with ledgerline where it makes sense

---

## 16. Testing

- **190 pytest tests**, run with `uv run pytest`. They cover:
  - money parsing edge cases (parentheses, CR/DR, locales, banker's rounding)
  - redaction
  - merchant normalization (processor prefixes, store numbers, ACH codes, casing)
  - date and regex validation
  - CSV/OFX import, dedupe (same-day duplicates, overlaps, re-import, pending → posted), dry runs, profile init
  - transfers and rules after import
  - spending/refund/transfer math, query totals under truncation, budget pace
  - recurring detection (price change, groceries excluded, missed/cancelled)
  - forecast with card payments
  - categorization dry run and rule writing
  - Schedule C lines (meals 50%, mileage, fallback mapping, personal never on Schedule C), SE tax and the wage-base cap
  - write-off review questions, trips, scope changes
  - MCP contract (annotations, meta, errors, dry-run defaults, redaction, audit log, chart resource), a real stdio session
  - no-network check, file permissions, watcher behavior, SimpleFIN parsing, allowlist, auth header, idempotent sync
- **Planned:** golden files for real (sanitized) bank exports, fuzzing the CSV/OFX parsers (Hypothesis), and CI across operating systems.

---

## 17. Distribution and Client Setup

```bash
uvx fiscl demo                 # try it with synthetic data
uvx fiscl import ~/Downloads/chase.csv --account "Chase Checking"
pip install "fiscl[sync]" && fiscl sync setup   # optional read-only bank sync
```

Claude Desktop (`claude_desktop_config.json`):

```json
{ "mcpServers": { "fiscl": { "command": "uvx", "args": ["fiscl", "serve"] } } }
```

Claude Code: `claude mcp add fiscl -- uvx fiscl serve`

---

## 18. Open Questions

- Trademark search for "fiscl"; GitHub org and domain availability.
- License: MIT (current) vs Apache-2.0.
- Fork or contribute to ledgerline for the shared core, or stay independent?
- Should recurring detection run on a timer while serving, as well as after imports?
- How US-specific should tax features be? Schedule C only, or plug-in tax regimes?
- MCP sampling for server-initiated categorization suggestions, or keep the client-driven flow?
