# Teller and Plaid read-only connector facts for fiscl Phase 2
Date: 2026-10-11 · Author: my-skills:researcher · Requested by: orchestrator · Status: final (with flagged unverified items)

## Answer (TL;DR)
- Both providers can be driven with stdlib `urllib` + `ssl`. Plaid is plain JSON over HTTPS. Teller needs an mTLS client cert plus HTTP Basic (`base64("<access_token>:")`).
- **Python cannot load a client cert from memory.** `ssl.SSLContext.load_cert_chain` only takes file paths. I tested the `/dev/fd` pipe trick on macOS (Python 3.14.7, OpenSSL 3.6.4) and it fails with `Illegal seek`. Safest stdlib option: write cert and key to a fresh 0700 `mkdtemp` dir with 0600 files, call `load_cert_chain`, and unlink immediately. The spec's "never written to disk" claim must be relaxed (see Contradictions). Confidence: high.
- Teller individuals can use the free **development** environment (real banks, 100 enrollments, mTLS required). **Production requires KYB (business verification)**, so individuals cannot self-serve it. Confidence: high (official Environments guide).
- Plaid: sandbox works with no browser via `/sandbox/public_token/create`. Production for individuals is the Trial plan (10 Items, free) or Pay-as-you-go. Confidence: medium-high; Plaid's own docs conflict on Trial vs "Limited Production".
- **Unverified:** Teller's transaction `amount` sign convention is not documented (only "signed"). It must be confirmed against a real or sandbox account before the parser is written.

## Findings: Teller

### 1. Teller Connect (browser)
Source: https://teller.io/docs/guides/connect
- Script: `https://cdn.teller.io/connect/connect.js`, placed near the end of `<body>`, no `async`/`defer`.
- `TellerConnect.setup({...})` options: `applicationId` (required), `products` (required array; allowed `verify`, `verify.instant`, `balance`, `transactions`, `identity`, `payments` (BETA)), `environment` (`sandbox` | `development` | `production`), `institution` (skip picker), `selectAccount` (`disabled` default, `single`, `multiple`), `enrollmentId` (update/repair mode), `connectToken`, `nonce`, `onSuccess` (required), `onInit`, `onExit`, `onFailure`.
- `onSuccess(enrollment)` payload: `accessToken`, `user.id`, `enrollment.id`, `enrollment.institution.name`, and `signatures` only when a `nonce` was passed. Signature = ED25519 over SHA-256 of `nonce.accessToken.userId.enrollmentId.environment` (dot-joined), verified with the "Token Signing Key" from the dashboard; multiple signatures during key rotation. Verifying needs ED25519, which is not in the stdlib, so skip it. The token only works with the app's client cert anyway (https://teller.io/docs/api/authentication).
- **Origin/redirect:** the Connect and Quickstart docs state no allowed-origin, redirect-URI, or localhost registration. The Quickstart examples run on `https://localhost:8000`, with `ENV=development CERT=... CERT_KEY=...` (https://teller.io/docs/guides/quickstart). The only app setting named is the Application ID (teller.io/settings/application). **Not verified in a browser: whether Connect works from `http://127.0.0.1:<port>`.** Test early; a plain-http page may be a problem for the Teller iframe or for Teller's own origin checks.
- Teller Connect probably loads additional Teller hosts in an iframe (not documented). The spec's CSP ("provider's CDN only") needs empirical browser testing for Teller. Plaid's CSP is documented (below).

### 2. Auth, mTLS, in-memory loading
Source: https://teller.io/docs/api/authentication
- Access token goes in HTTP Basic as username, empty password: `Authorization: Basic base64(token + ":")` (curl `-u TOKEN:`).
- mTLS: **sandbox** does not require a client cert (validated if sent); **development and production require it** for all requests involving end-user data. Missing cert gives 400. Cert and key are issued by Teller at sign-up (`teller.zip` containing the cert and private key PEMs) and managed at teller.io/settings/certificates (revoke and reissue there).
- Tokens are useless without the matching client cert.
- No `Teller-Version` header is documented.

In-memory loading, tested locally (scratchpad, Python 3.14.7, macOS, no `os.memfd_create`):
| Approach | Result |
|---|---|
| `load_cert_chain("/dev/fd/N")` from a pipe | **Fails on macOS** (`OSError 29 Illegal seek`); OpenSSL also opens cert and key separately |
| temp file (0600) then immediate `unlink` after load | Works |
| encrypted key + `password=` callable | Works, but encrypting the key needs openssl CLI or `cryptography` (not stdlib) |
| `os.memfd_create` + `/proc/self/fd/N` | Linux only; not available on macOS/Windows; untested here |

Recommendation: `tempfile.mkdtemp()` (0700) then `os.open(path, O_CREAT|O_EXCL|O_WRONLY, 0o600)` for one combined PEM (cert chain + key; `load_cert_chain(certfile)` with no keyfile reads the key from the same file), build the `SSLContext`, `os.unlink` and `rmdir` in `finally`. Build the context once per process and reuse it. Prefer `memfd_create` when `hasattr(os, "memfd_create")`. The window on disk is milliseconds, owner-only, in a private dir. Document this honestly in SECURITY.md. Use a custom `HTTPSHandler(context=ctx)` with the existing `_NoRedirect` opener. Keep `ssl.create_default_context()` defaults (hostname check and CA verify stay on).

### 3. Teller endpoints and shapes
Base URL `https://api.teller.io` (one host for all environments; sandbox vs live is determined by the token).
- `GET /accounts` list. Account: `id`, `enrollment_id`, `institution{id,name}`, `type` (`depository`|`credit`), `subtype` (`checking`, `savings`, `money_market`, `certificate_of_deposit`, `treasury`, `sweep`, `credit_card`), `name`, `currency`, `last_four`, `status` (`open`|`closed`), `links{self,balances,transactions,details?}`. Closed accounts return 410 `account.closed`. (https://teller.io/docs/api/accounts)
- `GET /accounts/{id}/balances`: `{account_id, ledger, available, links}`; strings, both nullable, at least one present. Billed $0.10 per call in production (https://teller.io/ pricing section; free in development).
- `GET /accounts/{id}/transactions` query: `count`, `from_id` (returns transactions older than that id, i.e. pages backward from newest), `start_date`, `end_date` (inclusive ISO dates). Fields: `id`, `account_id`, `amount` (string, signed), `date` (ISO date), `description` (raw statement text), `status` (`posted`|`pending`), `type` (free string such as `card_payment`, `transfer`, `atm`; no complete list), `running_balance` (string or null; only on posted, often null), `details{processing_status (pending|complete), category (nullable; ~27 values), counterparty{name,type}}`, `links`. (https://teller.io/docs/api/account/transactions) Pagination loop: request `count=N` (page-size cap not documented), then `from_id=<last id>` until an empty or short page; also bound by `start_date`.
- **Sign convention: NOT documented** ("signed amount as a string"; every doc example is positive, including a pending transfer). Do not assume. Verify against a sandbox enrollment (known debit, credit, and card purchase/refund) before coding; depository and credit accounts may differ.
- Revoke: `DELETE /accounts/{id}` removes access to one account; **`DELETE /accounts` removes access to every account in the enrollment** (what `forget` needs). Both return 204 with no body; the accounts are not deleted. Teller Connect tokens cannot be revoked any other documented way.
- Do NOT allowlist `GET /accounts/{id}/details` (full account and routing numbers), `/identity`, `/payments`, `/institutions`.
- Errors (https://teller.io/docs/api/errors): 400 missing cert, 401 no token, 403 bad/revoked token, 404 incl. disconnected enrollment with `error.code` `enrollment.disconnected[.reason]` (repair via Connect with `enrollmentId`), 410 closed account, 422, 429 rate limit (thresholds not public; free plan lower), 502 institution down. Body: `{"error":{"code","message"}}`.
- Environments (https://teller.io/docs/guides/environments): **sandbox** free, unlimited, fake data, no cert needed; **development** free, real banks, **max 100 enrollments ever created** (deletes don't refund), same rate limits as prod, mTLS required, does not carry to production; **production** paid, unlimited, KYB required.
- Sandbox credentials (https://teller.io/docs/guides/sandbox): username `username` + password `password` (success); `otp` (code `0000`), `challenge` (answer `blue`), `disconnected`, plus enrollment-status usernames. Sandbox tokens are created only through Connect; no browserless token mint is documented, so a Teller end-to-end test needs a browser step (or a recorded fixture).

### 4. Products, read-only, pricing, signup
- Products: `verify`, `verify.instant`, `balance`, `transactions`, `identity`, `payments` (BETA). Request `["transactions", "balance"]` only. Never `payments`, `verify*` (exposes account numbers), `identity`. Payments need a separate payee/payment flow and are a separate BETA product, so omitting it from `products` is the grant control; Teller also enforces on its side.
- Production pricing (https://teller.io/): Balance $0.10 per call, Transactions $0.30 per enrollment per month, Identity $1.75 per call, Verify $1.50 per account. "Free for independent developers and teams prototyping ideas": 100 live connections.
- Signup: sign-in/dashboard flow; cert is issued at sign-up. The pricing page does not state personal-use terms; the development tier is the path for individuals. Whether sign-up needs a business is not stated for development.
- Webhooks (`enrollment.disconnected`, `transactions.processed`, ...) need an app-registered URL and are optional; skip. Poll and detect `enrollment.disconnected` 404s instead.

## Findings: Plaid

### 5. Link token and Link web flow
- `POST /link/token/create` (https://plaid.com/docs/api/link/): required `client_name` (max 30 chars), `language` (`en`), `country_codes` (e.g. `["US"]`), `user.client_user_id` (no PII), `products`; optional `redirect_uri`, `webhook`. Response `link_token`, `expiration` (4h new Item; 30m update mode), `request_id`.
- Body: `{"client_id","secret","client_name","language":"en","country_codes":["US"],"user":{"client_user_id":"<random uuid>"},"products":["transactions"]}`. Never include `auth`, `identity`, `transfer`, `payment_initiation`, `signal`, `income_verification`, `investments`, `liabilities`, `assets`. Consider `transactions.days_requested` (1-730, default 90) set once at creation via `transactions: {days_requested: N}` on the token request; it cannot be raised later.
- Link web (https://plaid.com/docs/link/web/): script `https://cdn.plaid.com/link/v2/stable/link-initialize.js` (must load from Plaid's CDN; unversioned). Documented CSP: `default-src https://cdn.plaid.com/`; `script-src 'unsafe-inline'` + that script (or nonce); `style-src` (+ `-elem`, `-attr`) `'unsafe-inline'` or nonce; `frame-src https://cdn.plaid.com/`; `connect-src https://production.plaid.com/` (or `https://sandbox.plaid.com`). `Plaid.create({token, onSuccess(public_token, metadata), onExit(err, metadata), onEvent})`; `metadata.institution{name,institution_id}`, `metadata.accounts[{id,name,mask,type,subtype}]`, `metadata.link_session_id`. The link token is one-time use and short-lived.
- OAuth/localhost (https://plaid.com/docs/link/oauth/): `redirect_uri` is required for mobile; desktop web does not strictly need one, and OAuth banks open in a pop-up without it. Redirect URIs must be HTTPS and on the dashboard allowlist; **HTTP localhost is allowed only in Sandbox**. So a desktop-web localhost page with no `redirect_uri` is the correct design (do not send one). OAuth institutions in production additionally require Plaid production access and the compliance-center registration (hours to ~5 business days; Schwab/Fidelity slower). **Not verified:** whether Link in production works from a plain-http 127.0.0.1 origin; Plaid docs are silent. Treat as a risk to test with a real production key.

### 6. API
- Base URLs: `https://sandbox.plaid.com`, `https://production.plaid.com` (https://plaid.com/docs/api/). No separate "development" environment is documented (Items can't move between environments).
- Auth: `client_id` and `secret` in JSON body, or headers `PLAID-CLIENT-ID` / `PLAID-SECRET`. Prefer the headers (keeps secrets out of any logged body; headers are the "unredirected" place for the existing pattern). Content-Type `application/json`, POST for everything.
- `POST /item/public_token/exchange` `{public_token}` -> `{access_token, item_id, request_id}`. Public token expires in 30 min; access token never expires until `/item/remove`.
- `POST /transactions/sync` `{access_token, cursor?, count?(1-500, default 100), options?{days_requested (1-730, only for uninitialized Items), include_original_description, account_id}}` -> `{added[], modified[], removed[{transaction_id,account_id}], next_cursor, has_more, accounts[], transactions_update_status}`. Loop while `has_more`; persist the final `next_cursor`. On `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, restart from the cursor you began the loop with. Empty arrays and empty `next_cursor` mean data isn't ready yet (first pull can lag after link). Cursors from a completed pull are valid at least a year. Sync does not return `PRODUCT_NOT_READY`. (https://plaid.com/docs/api/products/transactions/)
- Transaction fields: `transaction_id`, `account_id`, `amount`, `iso_currency_code`/`unofficial_currency_code` (one is null), `date` (posted, or occurred date if pending), `authorized_date`, `name` (deprecated), `merchant_name`, `pending`, `pending_transaction_id` (on the posted twin; use it to drop the old pending row). **Sign: positive = money OUT of the account, negative = money IN.** Invert for fiscl.
- `POST /accounts/get` `{access_token, options?{account_ids}}` -> accounts with `account_id`, `balances{available,current,limit,iso_currency_code}`, `mask`, `name`, `official_name`, `type`, `subtype`, plus `item`. Free and cached (~daily). For credit/loan, positive `current` = amount owed. `/accounts/balance/get` is real-time but paid and not in the allowlist. (https://plaid.com/docs/api/accounts/)
- `POST /item/remove` `{access_token}` -> `{request_id}`. Required to stop Transactions subscription billing; does not free Trial slots. (https://plaid.com/docs/api/items/)
- Sandbox: creds `user_good` / `pass_good` (https://plaid.com/docs/sandbox/). **Browserless test:** `POST https://sandbox.plaid.com/sandbox/public_token/create` `{client_id, secret, institution_id:"ins_109508", initial_products:["transactions"], options:{override_username:"user_good", transactions:{days_requested:90}}}` -> `{public_token}` -> exchange -> sync. `ins_109508` is Plaid's well-known First Platypus Bank id from their examples; it is not on the reference page itself. This path is test-only; keep it out of the production allowlist (put it in a test-only constant).
- Webhooks: optional; skip. Plaid says to listen for `SYNC_UPDATES_AVAILABLE`, but polling `/transactions/sync` works (Plaid refreshes 1-4x/day per institution).

### 7. Terms for individuals (https://plaid.com/docs/account/billing/, https://plaid.com/pricing/)
- Plans: **Trial** (free; 10 production Items; new US/CA teams created on or after 2026-04-15; "most appropriate for hobbyist use"), **Pay as you go** (no minimum, "early-stage small businesses"), Growth, Custom. Removing Items does not return Trial slots. Transactions is a monthly per-Item subscription while a token exists; dollar amount not published in docs (shown on the request form). Request production via dashboard.plaid.com/overview/request-products (pay-as-you-go/trial through dashboard sign-up, email verification).
- Conflict: the pricing page still mentions "Limited Production: up to 200 API calls per available product using live data"; the billing page (more specific, dated 2026) says Trial = 10 Items. Treat the billing page as current and re-check on signup.
- Plaid documents no formal personal-use rule; expect a company/use-case questionnaire. OAuth banks need extra registration.

## Hosts to allowlist
| Provider | Server-side (urllib) | Browser-side (Link page CSP) |
|---|---|---|
| Teller | `api.teller.io` | `cdn.teller.io` script; other Teller frame/connect hosts unknown, must be discovered in a browser |
| Plaid | `production.plaid.com`; `sandbox.plaid.com` (dev/test only) | `cdn.plaid.com` (script, frame), `production.plaid.com`/`sandbox.plaid.com` (connect-src); plus bank OAuth pop-ups open in a separate window, not under our CSP |

## Contradictions with the spec (specs/2026-10-11-1948-v2-roadmap.md, Phase 2)
1. **"Teller's mTLS client certificate is loaded from the keychain into memory, never written to disk"**: not achievable with stdlib `ssl`. Use a momentary 0600 temp file in a 0700 dir (or Linux memfd) and reword the guarantee and the SECURITY.md text.
2. **Teller allowlist is incomplete**: forget must call `DELETE /accounts` (revokes the whole enrollment) rather than only `DELETE /accounts/{id}`. Allow both; keep `/details` out. The `GET /accounts` list is also required (already listed).
3. **Teller "primary" for individuals**: production needs KYB. The documented individual path is the **development** environment (100 lifetime enrollments, real banks, mTLS). Add an `environment` setting (`development` default for personal users); the page must pass it to `TellerConnect.setup`. Document the 100-enrollment lifetime cap (reconnects in update mode with `enrollmentId` do not create new enrollments, per docs wording; verify).
4. **"Plaid sandbox full flow in DoD"** is fine and can be browserless via `/sandbox/public_token/create`, but that endpoint is not in the spec's allowlist; add it as test-only. Teller has no browserless sandbox token path; its e2e must be manual or fixture-based.
5. **Teller `balance` product** is billed per call in production; fiscl should call balances sparingly (the `transactions` response has `running_balance` only sometimes).
6. **CSP "allows just the provider's CDN and our localhost endpoint"**: Plaid needs `cdn.plaid.com` for `script-src`, `frame-src`, `default-src` plus `connect-src` for the Plaid API; Teller hosts beyond `cdn.teller.io` are undocumented.
7. Spec says Plaid production hosts only `production.plaid.com`/`sandbox.plaid.com`: correct. No "development" host exists.
8. Sign handling: Plaid amounts are inverted relative to fiscl's convention (negative = out, presumably); Teller's convention is unverified and must be pinned by a test fixture from a real sandbox response.

## Recommendation
Build both connectors on stdlib. Plaid first is lower-risk to test (browserless sandbox, documented CSP and sign), but keep Teller as the documented primary per the decision. Before coding Teller's parser: (a) sign up for a free Teller account, (b) run Connect in sandbox from `http://127.0.0.1:<port>` in a browser and record the actual network hosts and a `GET /accounts/{id}/transactions` payload for depository and credit to fix the sign convention and the CSP. If Connect refuses plain-http localhost, the fallback is a self-signed local https cert (the Quickstart itself uses `https://localhost:8000`), which would add a dependency to the one-shot server. What would change this: Teller requiring https origins, Teller dropping the development tier, or Plaid restricting production Link to https origins.

## Sources
- [Teller Connect](https://teller.io/docs/guides/connect) — accessed 2026-10-11
- [Teller Authentication](https://teller.io/docs/api/authentication) — accessed 2026-10-11
- [Teller API intro](https://teller.io/docs/api) — accessed 2026-10-11
- [Teller Accounts](https://teller.io/docs/api/accounts) — accessed 2026-10-11
- [Teller Balances](https://teller.io/docs/api/account/balances) — accessed 2026-10-11
- [Teller Transactions](https://teller.io/docs/api/account/transactions) — accessed 2026-10-11
- [Teller Account details](https://teller.io/docs/api/account/details) — accessed 2026-10-11
- [Teller Errors](https://teller.io/docs/api/errors) — accessed 2026-10-11
- [Teller Webhooks](https://teller.io/docs/api/webhooks) — accessed 2026-10-11
- [Teller Environments](https://teller.io/docs/guides/environments) — accessed 2026-10-11
- [Teller Sandbox](https://teller.io/docs/guides/sandbox) — accessed 2026-10-11
- [Teller Quickstart](https://teller.io/docs/guides/quickstart) — accessed 2026-10-11
- [Teller home/pricing](https://teller.io/) — accessed 2026-10-11
- [Plaid API intro](https://plaid.com/docs/api/) — accessed 2026-10-11
- [Plaid Link API](https://plaid.com/docs/api/link/) — accessed 2026-10-11
- [Plaid Link web](https://plaid.com/docs/link/web/) — accessed 2026-10-11
- [Plaid OAuth](https://plaid.com/docs/link/oauth/) — accessed 2026-10-11
- [Plaid Transactions](https://plaid.com/docs/api/products/transactions/) — accessed 2026-10-11
- [Plaid Items](https://plaid.com/docs/api/items/) — accessed 2026-10-11
- [Plaid Accounts](https://plaid.com/docs/api/accounts/) — accessed 2026-10-11
- [Plaid Sandbox](https://plaid.com/docs/sandbox/) and [Sandbox API](https://plaid.com/docs/api/sandbox/) — accessed 2026-10-11
- [Plaid Billing](https://plaid.com/docs/account/billing/) and [Pricing](https://plaid.com/pricing/) — accessed 2026-10-11
- Local test: Python 3.14.7 / OpenSSL 3.6.4 on macOS, `/dev/fd` pipe vs temp file vs encrypted key for `load_cert_chain` — 2026-10-11
