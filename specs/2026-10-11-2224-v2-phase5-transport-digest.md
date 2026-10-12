# fiscl v2 Phase 5: localhost HTTP transport, change notifications, weekly digest
Status: draft
Created: 2026-10-11 22:24 CDT   ·   Updated: 2026-10-11 22:24 CDT
Author: Claude Code / my-skills:planner / planner subagent on feat/v2
Owner: Richard Long
Reasoning: The umbrella plan (`specs/2026-10-11-1948-v2-roadmap.md`, Phase 5 row) closes v2 with transport and habit features: some MCP clients and scheduled agents can only reach a server over HTTP, imports should tell connected clients their resources changed, and a weekly digest turns fiscl's monitors into a routine. HTTP is the first listening socket in the MCP server itself, so this spec makes its security model explicit and testable before anything else.
Research: `specs/2026-10-11-1948-v2-roadmap.md` (Phase 5 row); `docs/PROJECT.md` §11 (quarterly set-aside reminders via digest), §12.3–12.4, §14, §15 (MCP + Monitoring boxes); `docs/decisions/DECISIONS.md` (2026-10-11 connector layer entry: loopback one-shot server pattern with Host/Origin checks; security hardening entry: stdout discipline, SDK logger at WARNING); installed SDK source `mcp` 2.3.0 (`mcp/server/mcpserver/server.py` `run(transport="streamable-http", host, port, transport_security=…)`, `streamable_http_app(...)`, `mcp/server/transport_security.py` `TransportSecuritySettings`, `mcp/server/subscriptions.py` `SubscriptionBus` / `subscriptions/listen`, `mcp/server/mcpserver/context.py` `notify_resource_updated`, `mcp/server/session.py` `send_resource_updated`). Recommended: a short researcher check of which target clients (Claude Desktop, Claude Code) honor resource notifications and connect to local HTTP with a bearer header (R4).
Supersedes: none (child of the umbrella roadmap)

## Goal
`fiscl serve --http` exposes the same MCP surface over streamable HTTP on loopback only, protected by a keychain-held bearer token and strict Host/Origin rules, so local clients and scheduled agents can use fiscl without stdio. Clients learn when imports or syncs change data. `weekly_digest` gives a one-page weekly summary (tool + prompt) and can write it as a private Markdown file in the data dir; `spending_deep_dive` and `year_in_review` prompts round out the prompt set.

## Non-goals
- Any non-loopback bind, LAN or remote access, TLS termination, reverse-proxy support, or OAuth authorization server. If a user wants remote access, that's a different product decision.
- CORS of any kind. Browser access to the HTTP endpoint.
- Multiple tokens, per-client scopes, or read-only tokens (one token, full surface, same as stdio).
- Sending the digest anywhere (email, push, Slack). A file in the data dir and the tool result only.
- A scheduler inside fiscl. Scheduling is the user's (cron/launchd/Task Scheduler, or a Claude scheduled task calling the tool).
- Changing `goal_check_in` (already shipped in Phase 1).

## Context
- Stack (from `uv.lock`): Python ≥3.12, `mcp` 2.3.0, whose own dependencies already include `starlette` 1.7.0, `uvicorn` 0.54.0, `sse-starlette` 3.5.0, `anyio` 4.15.1. **No new runtime dependency.** Optional `keyring>=25`.
- `src/fiscl/cli.py::cmd_serve` builds the server and calls `build_server(cfg).run("stdio")`, starting the inbox watcher (`watch.start_watcher`) alongside; `main()` sets `umask 077` and `OTEL_SDK_DISABLED=true` before importing the SDK; logs go to files only.
- `src/fiscl/server.py::build_server(cfg) -> MCPServer`: tools are **sync** functions, so the SDK runs them in worker threads (`anyio.to_thread.run_sync` in `mcp/server/mcpserver/utilities/func_metadata.py`); resources `fiscl://accounts|categories|rules|budgets|goals|recurring|summary/current-month`; prompts `monthly_review`, `subscription_audit`, `categorize_backlog`, `cash_forecast`, `goal_check_in`, `tax_checkup`; `INSTRUCTIONS`; `run()` guard (open DB, redact, audit).
- SDK facts read from installed source: `MCPServer.run("streamable-http", host=…, port=…, transport_security=…)`; when `host` is loopback and no settings are given, the SDK enables DNS-rebinding protection with wildcard ports (`127.0.0.1:*`, `localhost:*`). `MCPServer(…, subscriptions=<SubscriptionBus>)` takes a bus; `Context.notify_resource_updated(uri)` publishes to `subscriptions/listen` streams (2026-07-28 protocol); older clients that used `resources/subscribe` need `session.send_resource_updated(uri)`. The SDK's own auth (`AuthSettings` + `TokenVerifier`) is an OAuth resource-server model (issuer and resource-metadata URLs) that doesn't fit a static local token.
- Keychain: `src/fiscl/sync/base.py` (`secret_set/get/delete`, `secret_name`, `is_os_keychain`, `keyring_backend`) — keychain, not network, so core code may import these helpers.
- Analysis services the digest composes: `analysis.spending_summary`, `cash_flow`, `budget_status`, `forecast_cash` (safe-to-spend), `list_recurring` / `recurring` upcoming, `find_anomalies`, `subscription_watchdog`, `goal_progress`, `tax.estimated_tax` (due dates), `common.data_freshness`.
- Guardrail tests: `tests/test_guardrails.py` (sockets blocked during every tool; AST rule that only `sync/base.py` imports network libraries with `sync/connect.py` as the named loopback exception), `tests/test_security.py` (file modes), `tests/test_mcp.py`, `tests/test_smoke.py`.

## Security model for the HTTP transport (normative)
**Assets:** the whole tool surface (all financial data; local write tools; `sync_accounts` network calls).
**Threats and controls:**
| Threat | Control (all enforced in `src/fiscl/http_transport.py`, each with a test) |
|---|---|
| Remote/LAN attacker | Bind only to the literal `127.0.0.1` (default) or `::1`. `--host` accepts only those two strings; anything else (`0.0.0.0`, `::`, `localhost`, a LAN IP, a hostname) exits with status 2 before a socket is created. After bind, the server asserts the socket's address is loopback. Requests whose ASGI `scope["client"]` host is not loopback get 403 (defense in depth). |
| Other local users / other local processes reaching the port | Every request needs `Authorization: Bearer <token>`. Token: 32 random bytes (`secrets.token_urlsafe(32)`), stored only in the OS keychain (`fiscl:http:token`); no keychain → `fiscl serve --http` refuses to start. Compare `hmac.compare_digest(sha256(presented), sha256(expected))` so length and content leak nothing. Missing/invalid → 401 with `WWW-Authenticate: Bearer` and an empty body. The token is never accepted in a query string or cookie. |
| Browser pages (drive-by `fetch` to `http://127.0.0.1:port`) | Any request carrying an `Origin` header is rejected with 403, whatever its value (native MCP clients don't send `Origin`; browsers always do on cross-origin and on POST). No CORS headers are ever sent; `OPTIONS` → 405 with no `Access-Control-*` headers. Only `POST`, `GET`, `DELETE` on the MCP path (per streamable HTTP) are routed; everything else 404. Requests with `Content-Type` other than `application/json` on POST → 415. |
| DNS rebinding (attacker domain resolving to 127.0.0.1) | `Host` must equal exactly `127.0.0.1:<port>` (or `[::1]:<port>` when bound to ::1; `localhost:<port>` also accepted only for the 127.0.0.1 bind). Wrong host or port → 421/403. Port-specific, not the SDK's `:*` wildcard. Also pass `TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[exact], allowed_origins=[])` to the SDK as a second layer. |
| Token brute force / log leakage | 256-bit token makes guessing infeasible; still, after 20 failed auths in 60 s the middleware returns 429 for 60 s (counter in memory). Failed auths are logged by count only. uvicorn `access_log=False`, `server_header=False`, `log_config` routed to the fiscl log file at WARNING; the token, `Authorization` header, and request bodies are never logged or audited. |
| Port squatting (another process binds the port first and impersonates fiscl to harvest the token) | If the bind fails, fiscl exits with a clear message and never retries another port silently. Residual risk documented: a same-user process that can bind first can receive the token from a misconfigured client. Rotate with `fiscl http token --rotate`. |
| Same-user malware | Out of scope (same as SECURITY.md "Keychain scope"): it can read the keychain and the DB directly. |
| Resource exhaustion | `max_request_body_size` 1 MiB; SDK `max_sessions` 8; session idle timeout 30 min; uvicorn `limit_concurrency` 16. |
| Plain HTTP on loopback | Accepted: traffic never leaves the host. Documented. |

**Order of checks** in the outermost ASGI middleware (before the SDK app sees the request): client address → Host → Origin → method/path → bearer token → rate limit state → pass to SDK app. Rejections never echo request data.

## Contract

### CLI
- `fiscl serve --http [--port N] [--host 127.0.0.1|::1]` — default port from `[http] port` in `config.toml` (default 8787; 1024–65535). Starts the inbox watcher like stdio mode. Prints `fiscl listening on http://127.0.0.1:N/mcp` to **stderr** (never the token).
- `fiscl http token` — creates the token if missing and prints nothing but "token stored in the OS keychain". `fiscl http token --show` prints it to the terminal (stderr, only when stderr is a TTY; otherwise refuses) for pasting into a client config. `--rotate` replaces it (old token stops working on the next request; the running server re-reads the keychain at most every 30 s, or immediately on 401). `fiscl http token --forget` deletes it.
- `fiscl digest [--week-ending YYYY-MM-DD] [--write]` — prints the digest Markdown to stdout (CLI, not the server, so stdout is fine); `--write` saves it like `write_digest`. Lets cron/launchd run it without a model.
- Install: `fiscl[http]` extra = `keyring>=25` (same pin as `sync`), documented in README.

### Config
```toml
[http]
port = 8787            # serve --http port, loopback only
```

### HTTP endpoint
- Path `/mcp` (SDK default), streamable HTTP, **stateful** sessions (needed for notifications), JSON-RPC per the SDK. No other routes (no `/health`, no static files).
- Status codes from the fiscl middleware: 401 (no/invalid token), 403 (non-loopback client, any `Origin`), 404 (other paths), 405 (other methods incl. OPTIONS), 415 (non-JSON POST), 421 (bad Host), 429 (auth rate limit). Bodies are empty or a fixed short string.

### Resource change notifications
- New module `src/fiscl/notify.py`: `ChangeNotifier(bus)` with `changed(kind: "import"|"sync"|"edit")`. It maps the kind to resource URIs:
  - import/sync → `fiscl://accounts`, `fiscl://summary/current-month`, `fiscl://recurring`, `fiscl://budgets`, `fiscl://goals`, and `fiscl://tax/{current_year}` if Phase 3 shipped it
  - edit (any `LOCAL_*` tool applied with `dry_run=false`) → the same set minus `fiscl://accounts`, plus `fiscl://rules` for `apply_categories` with saved rules and `fiscl://budgets` for `set_budget`
- Delivery: `build_server` creates one `InMemorySubscriptionBus` and passes it as `MCPServer(subscriptions=bus)`. A lifespan hook captures the event loop token (`anyio.lowlevel.current_token()`); `changed()` publishes `ResourceUpdated(uri)` for each URI via `anyio.from_thread.run(bus.publish, event, token=…)` from worker threads (tools) and from the watchdog thread (inbox imports). Calls coalesce: identical URIs within 2 s publish once (a small debounce in the notifier).
- Triggers: `run()` in `server.py` calls `notifier.changed("edit")` after a successful write tool call with `dry_run=False`; the `sync_accounts` wrapper calls `changed("sync")` when rows were added or updated; `watch.process_inbox_file` gets an optional `on_imported` callback that `cmd_serve` wires to `changed("import")`.
- Legacy clients (`resources/subscribe` on older protocol versions): best effort — when the trigger runs inside a request (tools), also call `ctx.session.send_resource_updated(uri)` for subscribed URIs if the SDK exposes the subscription set; watcher-driven imports reach only `subscriptions/listen` clients. Works the same over stdio and HTTP.
- Notification payloads carry only URIs, never data.

### Tool: `weekly_digest` (READ_ONLY)
Module `src/fiscl/analysis/digest.py`.
```
args: { week_ending?: date (default: the most recent Sunday ≤ today), scope?: "personal"|"business"|"all" = "personal" }
200: { "week": {"start": "2026-10-05", "end": "2026-10-11"},
       "spending": { "total": {...}, "vs_prior_week": {...}, "vs_4wk_avg": {...},
                     "top_categories": [ {"category": "...", "spent": {...}, "change_vs_4wk_avg_pct": "12.0"} ] (≤5) },
       "income": { "total": {...} },
       "budgets": [ {"category": "...", "status": "over"|"on_pace"|"under", "spent": {...}, "budget": {...}} ] (only over or at risk),
       "upcoming_7d": [ {"merchant": "...", "expected": "2026-10-14", "amount": {...}, "kind": "bill"} ],
       "safe_to_spend": {...} | null,
       "alerts": [ {"type": "duplicate_charge"|"spike"|"new_merchant"|"trial_converted"|"price_increase", "question": "...", "evidence": {...}} ] (≤10),
       "goals": [ {"name": "...", "pct": "41.0", "on_track": true} ],
       "tax_reminder": {"due_date": "2027-01-15", "days_until": 12, "suggested_per_quarter": {...}} | null,   // only when a federal (or state, if Phase 3) due date is ≤ 21 days away and business data exists
       "freshness": [...],
       "markdown": "# Week of …",               // the same content rendered (see Digest file)
       "notes": [...], "meta": {...} }
```
- Pure composition of existing services with a pinned `today`; no new SQL beyond what those services do. Each section degrades to empty with a note when its source has no data (no budgets, no goals, no business data).
- Merchant and category names are labeled fields; the `markdown` field escapes them (rules below).

### Tool: `write_digest` (W — `LOCAL_WRITE`, additive, `dry_run=True` default)
`{ week_ending?: date, scope?: …, dry_run: bool = true }` → `{dry_run, path: "digests/digest-2026-W41.md", bytes, next_step}`; applied adds `written: true`.
- Directory `digests/` (0700) created by `ensure_layout`; same safe-writer rules as Phase 3 exports (lstat not-a-symlink, inside `cfg.home`, `O_CREAT|O_EXCL|O_NOFOLLOW`, 0600). If `digest-YYYY-Www.md` exists, use `-2`, `-3`, … up to `-9`, then `GuardError(code="exists")`. Filenames come only from the ISO week; no user input reaches the path.
- **Markdown escaping** for every data value: backslash-escape ``\ ` * _ [ ] ( ) < > | # ! ~``, collapse newlines and control characters, cap at 80 chars; no links, images, or HTML are ever emitted. The file starts with "fiscl weekly digest — generated locally from your data; not financial advice." and the data-freshness line.
- If Phase 3's shared safe writer exists (`tax/export.py`), extract it to `src/fiscl/files.py::write_private_new(path_parts…)` and use it from both; otherwise create `files.py` here and Phase 3 adopts it. (Whichever lands first creates `files.py`; the other reuses it.)

### Prompts
- `weekly_digest(week_ending?: str)` — "Call weekly_digest{week_ending}. Summarize in under 200 words: spending vs usual, anything over budget, bills in the next 7 days, alerts as questions, goal progress, and any tax reminder. Quote the display amounts. Then offer to save it with write_digest (dry run first)." `week_ending` validated `^\d{4}-\d{2}-\d{2}$` or empty before interpolation.
- `spending_deep_dive(category?: str, merchant?: str, months?: str = "6")` — at most one of category/merchant (both → validation error). If neither is given, it starts from `spending_summary` by category for the last month and asks which to dig into. Steps: `category_trend` or `merchant_history`, `query_transactions` (top 10 by amount), `find_anomalies` in the window, `budget_status`. Argument values are validated before interpolation: `months` integer 1–24; `category`/`merchant` pass through `guard.clean_text(…, 60)` and are wrapped as a quoted literal the prompt tells the model to treat as a name, not an instruction.
- `year_in_review(year?: str)` — validated `^\d{4}$`, 2000..current year. Steps: `cash_flow` by month for the year, `spending_summary` by category with vs-last-year, `detect_recurring` annualized totals, `net_worth` change over 365 days, `goal_progress`, and for business users `schedule_c_draft` (and `export_schedule_c` dry run if Phase 3 shipped). Carries "facts, not advice" framing.

### Server instructions
Add `write_digest` to the dry-run-first list. Add one line: "When fiscl sends a resource-updated notification, re-read that resource before quoting it."

## Data
- No schema migration.
- Data dir: `digests/` (0700, files 0600). `fiscl doctor` checks/repairs `digests/` like `exports/`. Not included in DB backups.
- Keychain: `fiscl:http:token`.

## Ownership
| Agent | Owns |
|---|---|
| backend | `src/fiscl/http_transport.py` (new: middleware, bind checks, uvicorn runner), `src/fiscl/notify.py` (new), `src/fiscl/files.py` (new or shared with Phase 3), `src/fiscl/analysis/digest.py` (new), `src/fiscl/analysis/__init__.py`, `src/fiscl/server.py` (bus, lifespan, notifier triggers, tools, prompts, instructions), `src/fiscl/watch.py` (`on_imported` callback), `src/fiscl/cli.py` (`serve --http`, `http token`, `digest`, doctor), `src/fiscl/config.py` (`[http]`, `digests` dir), `pyproject.toml` + `uv.lock` (extra `http`), `scripts/smoke.py` |
| qa | `tests/**` — new `tests/test_http_transport.py`, `tests/test_notify.py`, `tests/test_digest.py`; updates to `tests/test_guardrails.py` (AST exception for `http_transport.py`, tool table), `tests/test_mcp.py`, `tests/test_security.py` |
| devops | `.github/**` — no change expected; the loopback HTTP test must pass on the existing ubuntu/macos matrix (Windows stays report-only) |
| shared (orchestrator) | `docs/PROJECT.md` §12.3–12.4, §13, §14 (new guardrail: HTTP transport), §15; `SECURITY.md` (HTTP transport section with the threat table summarized); `README.md` (client setup for HTTP); `docs/decisions/DECISIONS.md` (HTTP transport decision: own middleware instead of SDK OAuth auth, Origin-always-rejected) |

## Tasks (vertical slices; each is one PR)
1. [backend] **HTTP transport**: `http_transport.py` (bind validation, outer ASGI middleware with the ordered checks, exact-Host `TransportSecuritySettings`, uvicorn config: `access_log=False`, `server_header=False`, `limit_concurrency=16`, logs to file), `fiscl serve --http`, `fiscl http token [--show|--rotate|--forget]`, `[http] port`, `fiscl[http]` extra. — independent; start now
2. [backend] **Notifications**: `notify.py`, bus + lifespan token in `build_server`, triggers in `run()`, `sync_accounts`, watcher callback; works over stdio and HTTP. — after 1 only for the HTTP end-to-end test; the stdio part can start in parallel
3. [backend] **Digest + prompts**: `analysis/digest.py`, `weekly_digest`, `write_digest`, `files.py` safe writer, `digests/` dir + doctor, `fiscl digest`, prompts `weekly_digest`, `spending_deep_dive`, `year_in_review`, instructions. — independent; parallel with 1 and 2
4. [qa] Tests per slice (DoD), including an in-process ASGI test suite for every middleware rejection and one real-socket loopback test file allowed to open sockets on 127.0.0.1 only. — alongside each slice
5. [security] Review slice 1 against the threat table (mandatory before merge) and slice 3's file writer. — after 1, after 3
6. [reviewer] Per slice; final pass. — last
7. [shared] Docs: SECURITY.md HTTP section, README client config example (header `Authorization: Bearer …`), PROJECT §12–§15, DECISIONS entry. — after each merge
8. [human] Manual check: connect one real client over HTTP with the bearer header and confirm a resource-updated notification arrives after dropping a file in the inbox (R4). — after 2

Parallelism: 1 and 3 in separate worktrees immediately (conflict only in `server.py`/`cli.py` registration blocks). 2 starts with the stdio half, finishes after 1.

## Definition of Done
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` pass; `uv run python scripts/smoke.py` calls `weekly_digest`, `write_digest` (dry run), and renders all three new prompts
- [ ] `fiscl serve --http --host 0.0.0.0`, `--host ::`, `--host localhost`, and `--host 192.168.1.5` each exit with status 2 and open no socket (test patches `socket.socket` to fail if constructed)
- [ ] `fiscl serve --http` with no OS keychain exits non-zero with a keychain message and opens no socket (test)
- [ ] In-process ASGI tests against the wrapped app: no `Authorization` → 401; wrong token → 401; correct token → reaches the SDK (initialize succeeds); `Origin: http://evil.example` with a valid token → 403; `Origin: http://127.0.0.1:8787` with a valid token → 403; `Host: evil.example:8787` → rejected; `Host: 127.0.0.1:9999` (wrong port) → rejected; `OPTIONS /mcp` → 405 with no `access-control-*` headers; `GET /other` → 404; POST with `text/plain` → 415; client `("10.0.0.2", 5555)` in scope → 403; token in `?token=` query with no header → 401
- [ ] No response from the middleware, for any request, contains an `access-control-allow-*` header (test iterates all cases above)
- [ ] 21 bad-token requests within 60 s → the 21st returns 429, and a correct token is also refused until the window passes (test with a fake clock)
- [ ] Token comparison uses `hmac.compare_digest` over SHA-256 digests (unit test on the helper + a source assertion that `==` isn't used on the token)
- [ ] The fiscl log and audit log after an HTTP session contain neither the token nor the string `Authorization` (grep test)
- [ ] One real-socket test: server on `127.0.0.1:<ephemeral>`, a client with the bearer header lists tools and calls `list_accounts`; the same client without the header gets 401 (marked so it runs on macOS/Linux; it is the only test that opens a socket outside `sync/` fixtures)
- [ ] The AST guardrail lists `src/fiscl/http_transport.py` as the second named loopback exception, and still fails for any other new network import (test)
- [ ] `fiscl http token --show` refuses when stderr isn't a TTY (test); `--rotate` changes the keychain value and the old token gets 401 within one request after rotation (test with fake keychain)
- [ ] **Notifications:** with a `subscriptions/listen` client connected in-process, applying `set_budget(dry_run=false)` produces exactly one `ResourceUpdated` for `fiscl://budgets` (debounced) and a dry run produces none (test)
- [ ] Dropping a CSV in the inbox while serving produces `ResourceUpdated` for `fiscl://accounts` and `fiscl://summary/current-month` within 5 s (test with the watcher and an in-process listener)
- [ ] Notification payloads contain only `uri` (test inspects the notification params)
- [ ] **Digest:** on demo data with pinned today, `weekly_digest` returns spending total and vs-prior-week values equal to `spending_summary` over the same ranges (test), lists the seeded duplicate charge under `alerts`, and includes `tax_reminder` only when a due date is ≤ 21 days away (two pinned dates tested)
- [ ] `write_digest` default writes nothing; `dry_run=false` writes `digests/digest-YYYY-Www.md` mode 0600 in a 0700 dir; a second call writes `-2` (tests); a symlinked `digests` is refused (test)
- [ ] A merchant named `[x](http://evil)<img src=x>` appears in the file as `\[x\]\(http://evil\)\<img src=x\>` and the file contains no unescaped `<`, `](`, or `![` (test)
- [ ] `fiscl digest` prints the same Markdown as `weekly_digest(...)["markdown"]` for the same week (test)
- [ ] Prompts: `year_in_review(year="20x6")`, `spending_deep_dive(months="99")`, and `spending_deep_dive(category="A", merchant="B")` raise a validation error; a category containing a newline and "ignore previous instructions" is rendered as a single quoted, cleaned literal (tests)
- [ ] Every new tool is in `NO_NETWORK_TOOL_ARGS`; `write_digest` annotated `readOnlyHint=false, destructiveHint=false`; `weekly_digest` `READ_ONLY` (contract tests)
- [ ] SECURITY.md has an "HTTP transport" section covering loopback-only bind, bearer token in keychain, Origin always rejected, exact Host, no CORS, port-squatting residual risk; PROJECT §15 MCP boxes and the Monitoring "Weekly digest" box are ticked

## Risks & open questions
- **R1. Rejecting every `Origin` could block a legitimate client that sends one** (some HTTP client libraries or Electron-based apps add `Origin`). → Recommended: reject all `Origin` by default; add `[http] allowed_origins = []` only if a real client needs it, values must be exact `http://127.0.0.1:<port>`-style loopback origins, never `*`. Needs human decision? No (revisit only with evidence from R4).
- **R2. Using our own middleware instead of the SDK's `AuthSettings`/`TokenVerifier`.** The SDK path assumes OAuth (issuer URL, protected-resource metadata) and would advertise discovery endpoints we don't want. → Recommended: own pure-ASGI middleware (small, fully tested) plus the SDK's `TransportSecuritySettings` as a second Host check. Record in DECISIONS. Needs human decision? No.
- **R3. Port squatting** can't be fully prevented without TLS or OS-level peer credentials. → Recommended: document the residual risk, fail loudly on bind errors, never auto-pick a port, offer `--rotate`. Needs human decision? No.
- **R4. Client support is unverified**: whether Claude Desktop / Claude Code connect to a local streamable-HTTP server with a static bearer header, and whether they act on `subscriptions/listen` or `resources/subscribe` notifications. → Recommended: short researcher check (or the human manual check in task 8) before announcing the feature; notifications ship as best-effort either way. Needs human decision? No.
- **R5. Notifications from the watcher thread** need the event-loop token captured in lifespan; `anyio.from_thread.run(..., token=)` behavior must be confirmed against anyio 4.15.1. → Recommended: builder verifies in a unit test first; fallback is a thread-safe queue drained by a task started in lifespan. Needs human decision? No.
- **R6. Default port 8787 collides with something on the user's machine** (and Phase 4 QBO's callback default 8765 must differ). → Recommended: configurable `[http] port`, clear bind-failure message. Needs human decision? No.
- **R7. Does HTTP mode change the human-gate story?** A scheduled agent over HTTP may run write tools without per-call approval. → Recommended: document that HTTP clients must keep per-call approval for write tools; dry-run defaults stay; consider `[http] read_only = true` (reject `LOCAL_*` tools over HTTP) as a follow-up if the owner wants scheduled agents read-only. Needs human decision? Yes — whether HTTP mode should default to read-only tools (recommended answer: no for v2, add the opt-in `read_only` flag later if scheduled agents are used).
- **R8. Digest content leaving the machine.** The digest file stays local, but the tool result goes to the user's model like every other tool. → Recommended: same SECURITY.md framing; respect `redact_descriptions` in the digest (merchant names kept, descriptions never included). Needs human decision? No.
