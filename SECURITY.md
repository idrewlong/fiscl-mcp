# Security

fiscl handles financial data, so its defaults are conservative and its guarantees are enforced in code and covered by tests (`tests/test_guardrails.py`, `tests/test_security.py`, `tests/test_mcp.py`, `tests/test_simplefin.py`, `tests/test_connectors.py`, `tests/test_teller.py`, `tests/test_plaid.py`).

## Where your data goes

| Data path | Where it goes | Notes |
|---|---|---|
| Bank exports you import (CSV/OFX/QFX) | Your machine only | Parsed locally into `~/fiscl/fiscl.db`. No third party involved. |
| Optional bank sync (SimpleFIN, Teller, Plaid) | Bank → aggregator → your machine | The aggregator holds the bank connection and sees the data in transit, under its own privacy policy. fiscl stores it only locally. Sync is read-only and opt-in. |
| **Tool results sent to your AI model** | **Your MCP client sends them to whatever model it uses** | fiscl runs locally, but when you ask Claude (or another assistant) a question, the numbers and merchant names fiscl returns go to that model's provider, just like anything else you type into the chat. To limit what leaves your machine: keep aggregates by default (fiscl does), set `redact_descriptions = true`, or use an MCP client backed by a local model. |

fiscl itself has no telemetry, no analytics, no accounts, and no servers.

## Guarantees

- **It never moves money.** There is no code path for payments, transfers, or trades. Every bank integration is read-only by protocol, and the schema has no payee or destination concept.
- **Local only.** Data lives in `~/fiscl` (or `$FISCL_HOME`/`--home`). The core makes no network calls; a test blocks sockets while running every analysis path. Only the sync code may connect, over https to allowlisted hosts.
- **Private files.** The data directory is 0700, and fiscl runs with umask 077, so everything it creates (database, WAL, logs, backups, `.bak` files) is 0600. `fiscl doctor` checks and repairs permissions, warns when the data directory is inside a cloud-synced folder (iCloud Drive, Dropbox, OneDrive, Google Drive, Box), and warns when FileVault is off on macOS. fiscl refuses to use your home directory itself as its data directory.
- **Encryption at rest** relies on OS full-disk encryption (FileVault, BitLocker, LUKS). Turn it on. An encrypted database (SQLCipher) is on the roadmap.
- **Secrets.** Bank-sync credentials are stored only in your OS keychain. fiscl refuses to sync if no keychain is available rather than fall back to plain text. Setup tokens are read with hidden input, never from command-line arguments. Credentials travel in an `Authorization` header, never a URL, and that header is never forwarded on redirects.
- **Network hardening for sync.** https only, an allowlist of hosts, TLS verification on, and redirects never followed, so credentials can't be bounced to another host. Each connector also has a hard-coded allowlist of (method, path) pairs checked before any socket opens: Teller may only read accounts, transactions, and balances, and revoke access (`DELETE`); Plaid may only call `/link/token/create`, `/item/public_token/exchange`, `/transactions/sync`, `/accounts/get`, and `/item/remove` on the one host of your configured environment (`sandbox.plaid.com` or `production.plaid.com`), and its Link token requests only the `transactions` product. Nothing can reach account/routing numbers, identity, verification, or money movement, and a test scans `src/` for those endpoint strings.
- **Bank connections are human-only.** `fiscl connect teller|plaid` runs a one-shot server on 127.0.0.1 (random port, random 32-byte URL token, one POST, 10-minute timeout, strict CSP) that hosts the provider's own Connect/Link widget in your browser (the only third-party script involved, allowed by a per-provider CSP: Plaid's needs `cdn.plaid.com` and `*.plaid.com`); it is never an MCP tool, so a model can't start one. Access tokens, your Plaid client_id/secret, your Teller certificate/key, and application id live only in the OS keychain (`fiscl:<provider>:<ref>`); the database stores only provider, institution, a hashed reference, sync status, and Plaid's opaque sync cursor (a bookmark, not a credential). `fiscl connect forget teller|plaid` revokes access at the provider (`DELETE /accounts`, `/item/remove`) and deletes the keychain entries. Plaid Link on plain `http://127.0.0.1` is only verified for the sandbox; production Link from that origin must be confirmed by the user.
- **Client certificate on disk, briefly.** Python's `ssl` can only load a client certificate from a file path. On Linux fiscl uses an in-memory file; elsewhere it writes the PEM (0600) into a fresh private (0700) temp directory, loads it, and deletes both immediately. It exists for milliseconds and is never in the data directory.
- **Redaction.** Full account numbers are never stored, only a hash and the last 4 digits. Every tool response is scrubbed for card numbers (Luhn-checked), SSNs, and long account-like numbers.
- **Untrusted text.** Bank descriptions are Unicode-normalized, stripped of control and bidi characters, capped, and returned only as labeled data. The server tells the model never to follow instructions found in them.
- **Chart view.** The in-chat chart has a strict Content-Security-Policy (no network, no external resources), renders bank-derived labels as text only, and accepts messages only from its host frame.
- **Deliberate writes.** Category, scope, and mileage edits are dry runs by default, bulk edits are capped, and imports are atomic. The database is backed up before migrations and daily (last 14 kept).
- **Audit trail.** `fiscl audit` shows every tool call with sanitized arguments; merchant names, descriptions, and amounts are never logged, and failed calls log only a stable error code (such as `not_found`), never the error text.

## Recommendations for users

1. Turn on full-disk encryption.
2. Keep `~/fiscl` out of cloud-synced folders (`fiscl doctor` will tell you).
3. Don't enable fiscl in the same MCP client session as tools that can send data to the internet (web fetch, email, etc.) unless you trust every data source. Bank descriptions are untrusted text, and the safest assistant setup can't be tricked into sending your data anywhere.
4. Set `redact_descriptions = true` in `config.toml` if you don't want raw bank text sent to your AI model.

## Reporting a vulnerability

Open a private security advisory on the GitHub repository rather than a public issue.
