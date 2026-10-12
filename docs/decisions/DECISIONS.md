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

## 2026-10-11 — QuickBooks: sandbox-only first, production behind a human check
- **Decision:** Build the QBO read-only connector as the last Phase 4 slice, sandbox-only. Production stays disabled until a human completes Intuit's production questionnaire and confirms that OAuth Playground tokens work for a personal app. If not, QBO remains a sandbox test harness.
- **Why:** Research shows it's free (Builder tier: 500k reads/month, blocked rather than billed over the cap), which meets the "free or cheap" rule. But production OAuth needs an https redirect, which a local-only tool can't provide without a workaround.
- **Where:** `docs/research/2026-10-11-quickbooks-online-readonly.md`, `specs/2026-10-11-2224-v2-phase4-data.md`.

## 2026-10-11 — Federal and state due dates roll to the next business day
- **Decision:** `estimated_tax` rolls every due date (federal 1040-ES and state) to the next business day (weekends plus US federal holidays). State-only holidays aren't modeled and the notes say so. Shapes: `state` keys are omitted when no state is configured, and `suggested_per_quarter_with_state` is a per-quarter list because state percentages can differ.

## 2026-10-11 — Multiple-business rules (Phase 3 slice D)
- SE tax is computed once on the sum of all businesses' net profit (it's owed per person), so one business's loss offsets another's profit. Each Schedule C is drafted per business.
- Mileage stays on the default business: the trip log has no business column yet.
- Archiving a business is refused while it has current-year activity or 1099 forms.
- The `tax_checkup` prompt falls back to the current year on an invalid `year` (fixed sentence, no echo) rather than erroring.
- The forbidden-endpoint source scan has one exemption mechanism, an exact-string allowlist per file with a reason (`tests/conftest.py::ALLOWED_REFERENCE_URLS`). The AST scan also folds literal concatenation.

## 2026-10-12 — QIF and holdings import rules (Phase 4)
- QIF imports never create accounts from file text; `!Account` names must match an existing account. Dotted `DD.MM.YYYY` dates are always day-first; slash dates default to month-first (`--qif-date` or `[import] qif_date_order` overrides). A file that looks like the other order fails without echoing values.
- Holdings: quantities and prices are Decimal strings; values are minor units. Negative quantities (shorts) are allowed. Cost basis is summed only when every lot has one. Holdings accounts stay home-currency until FX lands (superseded 2026-10-12: foreign holdings convert at the as-of rate; only different-decimal currencies are refused). Broker account-number columns are ignored or cut to last 4 for routing, never stored.
- Built-in Fidelity/Schwab/Vanguard/crypto profiles wait for the owner's scrubbed header samples.

## 2026-10-12 — Multi-currency semantics (Phase 4)
- Home amounts are materialized (`amount_home`) using the latest rate on or before the transaction date, within `[fx] max_rate_age_days` (default 7, range 1–31). Banker's rounding per row. Split parts get the remainder on the largest part so they sum exactly. With no rate the row stays unconverted: excluded from totals and counted in `meta.unconverted`.
- Rates come only from `set_fx_rate` or `fiscl fx import`; there is never a network lookup. SQL triggers keep home-currency rows at `amount_home = amount` for every writer.
- Balances, transfer matching, dedupe and rule amount conditions stay in native currency. Transfers between accounts in different currencies won't auto-match (known limitation).
- Changing `config.currency` needs `fiscl fx refresh`; tools and `doctor` warn until then.

## 2026-10-12 — Amazon matching and QBO connector details (Phase 4)
- Amazon: imported from the user's order-history CSV (detected by header); `amazon_matches` proposes `split_transaction` args by exact shipment, then exact order total, with a ±1¢/item fallback reported as `rounding_adjustment`. Only `description_regex` rules categorize items (merchant rules would put everything in one category). Never auto-applied. Amount semantics are unverified until a real export is checked.
- QBO: `http://localhost:<redirect_port>/qbo/callback`. No PKCE (Intuit doesn't document it), so state is compared in constant time. The access token is never stored. A rotated refresh token is written to the keychain before use. 429 → wait Retry-After (≤120 s), at most 3 retries. `Policy.query_check(method, path, raw_query)` validates the query text per route. Stdlib only, no `fiscl[qbo]` extra. Production stays refused.

## 2026-10-12 — Phase 4 QA and security fixes
- Transfers pair only within one currency (equal native minor units across currencies are a coincidence). Currencies with no ISO minor unit (metals, XDR, XTS, XXX, fund units) are refused as transaction/rate currencies with a fixed message; CLF and UYW carry 4 decimals.
- Amazon matching claims items, not groups: if a shipment proposal and an order proposal (or any two charges) would use the same item, none of the colliding charges is proposed and all become `ambiguous`. Re-exports key items on order id + name + unit total + occurrence (not ship date), and fill the ship date in place.
- QIF apostrophe years (`'YY`) are always 20YY; the pivot applies only to slash/dotted two-digit years.
- `connect forget qbo` revokes at Intuit only when the app keys are stored; it always deletes the refresh token, realm and connection rows and reports the skipped revoke on stderr.
- The QBO callback binds `[::1]:<redirect_port>` without listening (no reuse options) so `localhost` can't be squatted on IPv6; busy means stop, no IPv6 loopback means skip. Only a request with the right `state` consumes the one-shot. `receivables` withholds customer names, document numbers and the customer filter under `redact_descriptions`.

## 2026-10-12 — HTTP transport design (Phase 5)
- fiscl uses its own ASGI gate in front of the SDK's streamable-HTTP app, not the SDK's OAuth `AuthSettings`. Order: loopback peer → exact Host (with port) → any Origin rejected → path/method → rate limit (20 failures/60 s → 60 s lock) → bearer digest compare → JSON content type. No CORS headers ever.
- The token lives only in the OS keychain (`fiscl:http:token`) and is read per request, so rotation is immediate. A keychain failure fails closed with 503. It is never accepted from a query string or cookie.
- Read-only by default: every tool not annotated `readOnlyHint=true` (including `sync_accounts`) is removed from the HTTP server unless `[http] allow_writes = true`. New tools must opt in as read-only.
- `http_transport.py` is the second named exception (with `sync/connect.py`) in the AST network-import guard.

## 2026-10-12 — Phase 5 QA and security fixes
- SEC-1: the HTTP server reserves the other loopback family on its port (`loopback.reserve_other_family`, shared with the QBO callback; bind only, no reuse options; busy refuses to start, exit 2; no IPv6 loopback skips). `localhost:<port>` is no longer an allowed Host: clients use the literal URL printed in the banner. `bind_loopback` calls `listen()` immediately. `loopback.py` is the third named socket exception in the AST guard; `connect.py` no longer imports `socket`.
- SEC-2: any `Sec-Fetch-Site/Mode/Dest` is treated like `Origin` (403). A request with no `Authorization` header gets 401 before the limiter and the keychain read; only presented-but-wrong credentials count toward lockout.
- SEC-3: the AST guard also covers `httpx2`, `mcp.client`, `starlette.testclient` and rejects anyio's `connect_tcp`/`create_tcp_listener`-style calls anywhere in `src/`.
- Markdown escaping also escapes `@` (`refunds\@example.com`), so GFM won't autolink e-mail addresses. The digest Markdown, file and CLI now carry the unconverted-rows / home-currency warnings from `meta` near the top.

## 2026-10-12 — Phase 5 review fixes (correction to SEC-2)
- SEC-2 corrected: `Sec-Fetch-Mode` is no longer a rejection trigger. Node's built-in fetch (undici) sends `sec-fetch-mode: cors` on every request (checked on Node v24: host, authorization, accept-language `*`, sec-fetch-mode, user-agent `node`; no Origin, Sec-Fetch-Site or Sec-Fetch-Dest), so the MCP TypeScript SDK (Claude Code, Claude Desktop, MCP Inspector, mcp-remote) got 403. The gate now rejects on `Origin`, `Sec-Fetch-Site` or `Sec-Fetch-Dest`; browsers send all three Sec-Fetch headers to `http://127.0.0.1` (a potentially trustworthy origin), so `<img>`/no-cors requests stay blocked. The no-Authorization-is-401-and-uncounted rule is unchanged.
- Windows: the listening socket and the other-family reservation set `SO_EXCLUSIVEADDRUSE` (guarded by `hasattr`), since `SO_REUSEADDR` is not set there and exclusivity is the Windows equivalent of the POSIX behavior.
- `week_ending` for `weekly_digest`, `write_digest`, `fiscl digest` and the `weekly_digest` prompt must be a Sunday (GuardError, code `invalid`). Rolling non-Sunday windows were accepted before and produced misleading ISO-week file names. The default (most recent Sunday) is unchanged.
- The `cash_forecast(days)` prompt validates `days` as ASCII digits within the forecast tool's 7-120 range (default 30), like `months`, before interpolating it.

## 2026-10-12 — Phase 4 review fixes and Phase 5 notifications/digest
- Statements without a stated currency (QIF; CSV profiles without `currency`) adopt the account's currency. A stated currency that differs from the account's is refused. OFX `CURDEF` is validated against ISO 4217 at parse time.
- A capped QBO read (50 pages) never advances the cursor or replaces the mirror. Invoices deleted in QuickBooks stay in the mirror until forget + reconnect (known limitation, noted in `receivables`).
- Resource-updated notifications are debounced (~2 s) and sent only after applied edits, syncs that added rows, and imports. URIs only. They're delivered via `subscriptions/listen`; legacy `resources/subscribe` isn't served. The watcher thread reaches the event loop through an anyio token captured at startup.
- The digest reuses the export safe-writer (`tax.export.private_dir` + `write_all`). Its tax reminder covers federal 1040-ES dates within 21 days when business data exists; state dates aren't included yet.
- (INFO-1, 2026-10-12) Only a single Bearer credential is treated as a token guess. A missing, duplicated, or non-Bearer `Authorization` header (e.g. Basic from URL userinfo) gets 401 before the limiter and without a keychain read; wrong Bearer tokens still count toward the lockout.
