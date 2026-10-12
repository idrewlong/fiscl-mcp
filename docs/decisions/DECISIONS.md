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
