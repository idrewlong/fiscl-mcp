# Decisions

Newest first. Each entry: date, decision, why, alternatives rejected, where it lives.

## 2026-10-11 — Split analysis.py into a package; shared helpers in common.py
- **Decision:** `analysis.py` (894 lines) became `analysis/` with one module per area (transactions, spending, budgets, forecast, recurring_report, edits). Shared service helpers (meta, money_arg, account_id, scope/txn filters, year_bounds, resolve_target, dry-run `plan_result`, bulk caps) moved to `common.py`. `db.TXN_FROM` is the standard transaction join. `fiscl.analysis.<fn>` stays a stable import path via re-exports.
- **Why:** v2 adds ~15 tools (net worth, goals, debt payoff, anomalies, splits/notes/tags…). Each area now has an obvious home, and `tax.py` no longer imports analysis internals. Duplicated filter, balance, and spent-by-category SQL was consolidated.
- **Rejected:** auto-generating MCP tool wrappers from service signatures. That would put MCP schema descriptions in the service layer and rely on SDK signature introspection. Explicit thin wrappers in `server.py` stay.
- **Where:** `src/fiscl/analysis/`, `src/fiscl/common.py`, `src/fiscl/db.py`.

## 2026-10-11 — Demo ships its own categorization rules
- **Decision:** `fiscl demo` appends demo-only rules to `rules.toml` once (idempotent), so business spending maps to Schedule C lines. GitHub and Planet Fitness on the personal card stay uncategorized on purpose.
- **Why:** without them, all demo business spending was uncategorized: Schedule C showed $90 of expenses and overstated profit and estimated tax, which undercut the day-one experience.
- **Where:** `src/fiscl/demo.py` (`DEMO_RULES`).

## 2026-10-11 — Security hardening pass
- **Decision:** SimpleFIN (and all future connectors) use a no-redirect opener with unredirected auth headers. The chart view gets a strict CSP, text-only tooltips, and a parent-frame check. The CLI runs with umask 077. `doctor` warns about cloud-synced data dirs and FileVault being off. fiscl refuses `$HOME` or a filesystem root as its data dir. The MCP SDK logger is set to WARNING. SECURITY.md now states plainly that tool results reach the user's AI model provider.
- **Why:** an audit found credential forwarding on redirect, an XSS path from bank-derived labels in tooltips, world-readable logs, and that the SDK echoed tool error text into logs.
- **Where:** `src/fiscl/sync/simplefin.py`, `src/fiscl/charts.py`, `src/fiscl/cli.py`, `src/fiscl/config.py`, `tests/test_security.py`, `SECURITY.md`.

## 2026-10-11 — Plaid/Teller connectors are Phase 2, read-only in four layers
- **Decision:** bank connections via Plaid and Teller move up to v2 Phase 2. Read-only is enforced by the products requested (`transactions` only), a per-connector endpoint allowlist, a host allowlist with no redirects, and a source scan for money-movement endpoints. Connecting a bank is CLI-only, never an MCP tool. Users bring their own keys, stored in the keychain.
- **Why:** the user asked for aggregator bank connections that stay read-only and keep data local.
- **Where:** `specs/2026-10-11-1948-v2-roadmap.md` (Phase 2).

## 2026-10-11 — Read-only connector layer; Teller first; setup is CLI-only
- **Decision:** All bank connectors go through `sync/base.py`. A `Policy` enforces https, a host allowlist, port 443, no userinfo, and a (method, path) allowlist before any socket opens. Redirects are never followed and auth headers are unredirected. Secrets are keychain-only, keyed `fiscl:<provider>:<ref>`. Teller is the primary provider (environment `development` by default, since individuals can't get production without KYB); Plaid and SimpleFIN are alternatives. Connecting a bank is CLI-only: a one-shot 127.0.0.1 server with a random path token, Host/Origin checks, a single POST, and a 10-minute timeout. It is never an MCP tool. Teller's mTLS cert is loaded through a 0600 temp file in a 0700 mkdtemp folder that's deleted immediately (memfd on Linux), because Python's `ssl` can't load certs from memory.
- **Why:** the user wants aggregator connections that are provably read-only and keep data local. Teller is self-serve and free for individuals in development.
- **Rejected:** vendor SDKs (extra dependencies and less control over the endpoint allowlist); exposing connect as an MCP tool (it would let the model start a bank connection).
- **Open, needs the human before release:** Teller's amount and balance sign convention, the Connect CSP hosts, and whether Connect accepts plain `http://127.0.0.1` (fallback: a self-signed local https cert). See `src/fiscl/sync/teller.py` (`TELLER_AMOUNT_SIGN`) and `connect.py` (`TELLER_CONNECT_HOSTS`).
- **Where:** `src/fiscl/sync/`, `specs/2026-10-11-1948-v2-roadmap.md` Phase 2, `docs/research/2026-10-11-teller-plaid-readonly.md`.

## 2026-10-11 — Plaid connector: cursor sync, finalize hook, importer learns modified/replaces
- **Decision:** `sync/plaid.py` uses `/transactions/sync` with a per-Item cursor stored in `connections.cursor` (migration 5; opaque bookmark, not a secret). `fetch` stays side-effect free; connectors may expose `finalize(conn)`, which `run_connector` calls only after a successful import to delete Plaid-removed rows (only rows from `format = 'plaid'` imports, never user-created data) and advance cursors, so a failed import re-fetches instead of losing data. `ParsedTxn` gained `replaces_fitid` (Plaid `pending_transaction_id`, so a posted row inherits the pending row's edits even if the tip changed the amount) and `modified` (update an existing row in place, keeping category/note/tags/scope, dropping a split that no longer sums). A mutation error restarts the pass from the original cursor. Plaid amounts are inverted in `_plaid_amount`; credit/loan balances are stored negative in `_plaid_balance`. Environment is `sandbox` (default) or `production`; `/sandbox/public_token/create` is routable only in sandbox. `SyncError` now carries the provider error body so connectors can read `error_code` (never shown).
- **Why:** correctness under retries and partial failure, and read-only enforced in code.
- **Rejected:** a separate sync-state table (one cursor per connection fits the existing row); deleting removed rows inside `fetch` (would lose data if the import then failed).
- **Open, needs the human:** run the live sandbox test (`FISCL_PLAID_CLIENT_ID`/`FISCL_PLAID_SECRET`) and a sandbox `fiscl connect plaid` in a browser to confirm Link loads under the CSP on `http://127.0.0.1`; production Link from plain-http localhost is unverified; `ins_109508` and the first-pull lag behavior come from docs/research, not from a recorded run.
- **Where:** `src/fiscl/sync/plaid.py`, `connect.py`, `registry.py`, `importers/core.py`, `tests/test_plaid.py`.

## 2026-10-11 — Default bank connection: free file import, then SimpleFIN; Teller demoted
- **Decision:** Recommended order is (1) free file imports (CSV/OFX/QFX + inbox watcher, no third party), (2) SimpleFIN Bridge for cheap automatic sync ($1.50/month or $15/year), (3) Plaid Trial (free, 10 connections, needs the user's own Plaid developer account), (4) Teller, supported for users who already have Teller access. Supersedes "Teller first" from earlier today. No connector code is removed.
- **Why:** Teller has no public signup (only a sign-in page; `/signup` returns 404, checked 2026-10-11), so most users couldn't follow a Teller-first setup. The user's requirement: "it needs to be a free or cheap option".
- **Rejected:** Plaid as the default (free, but a developer account per user is too much friction for non-technical users); Teller as the default (not obtainable).
- **Release gates:** live Teller and Plaid checks are deferred by the user's choice; connectors remain release-gated until verified.

## 2026-10-11 — Phase 3–5 product decisions
- **Multiple businesses (Phase 3 slice D):** Option A, per-transaction. Add a `businesses` table plus a nullable `business_id` on accounts, transactions, splits and 1099 forms. NULL means the default business (id 1), so there's no backfill and single-business output stays byte-identical. Rejected: account-level only (can't share a card or split a purchase across businesses).
- **State tax:** planning depth only, signed off. Due dates, installment percentages, a user-supplied rate, no-income-tax states, and `estimated_required: false` for UT and ID. Weekend dates roll forward in code. A per-state `verified` flag with `VERIFIED_FOR = 2026`. No bracket engine. Source: `docs/research/2026-10-11-state-estimates-1099.md`.
- **1099 thresholds:** NEC/MISC are $600 before 2026 and $2,000 for 2026 (`>=`), indexed from 2027. 1099-K applies above $20,000 AND above 200 transactions (`>`); card processors have no threshold. Thresholds are looked up by payment year.
- **QuickBooks Online (Phase 4):** research first. Build only if personal use is free and needs no paid partner tier or review we can't do. Otherwise drop it.
- **Holdings import profiles (Phase 4):** built-in profiles for Fidelity, Schwab and Vanguard position exports, plus crypto tax reports (start with the most common export formats; generic profile for the rest).
- **HTTP transport (Phase 5):** read-only tools by default. Write tools over HTTP require `[http] allow_writes = true` in config. Why: HTTP is reachable by any local process holding the token, whereas stdio is bound to one client.
- **tax.py → tax/ package** (Phase 3 slice 0), same pattern as analysis/, with a byte-identical snapshot test.
