# fiscl

A local-first personal **and** business finance server for the Model Context Protocol. Import your bank exports (or sync read-only through SimpleFIN), then ask Claude or any MCP client questions like these:

- "Where did my money go last month compared to September?"
- "What's safe to spend before payday?"
- "Which subscriptions went up in price?"
- "Draft my Schedule C and tell me what I still need to sort out for my accountant."

Your data stays in a SQLite file on your machine. fiscl can't move money, and every number comes from code, not from the model doing arithmetic.

## Quick start

```bash
git clone … && cd fiscl
uv sync
uv run fiscl --home /tmp/fiscl-demo demo           # six months of synthetic data, in a throwaway folder
uv run fiscl --home /tmp/fiscl-demo serve          # MCP server over stdio, pointed at the demo data
```

Use `--home` for the demo so synthetic rows never mix into your real database (`fiscl demo` refuses to load into a database that already holds real data unless you pass `--force`). Drop `--home` when you are ready to use your own data in `~/fiscl`.

Installing with `pip` or `uvx` resolves dependency version ranges at install time; for exactly the dependency versions this repo tests, install from a clone with `uv sync`, which uses `uv.lock`.

Add fiscl to Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "fiscl": { "command": "uv", "args": ["--directory", "/path/to/fiscl", "run", "fiscl", "serve"] }
  }
}
```

Or add it to Claude Code: `claude mcp add fiscl -- uv --directory /path/to/fiscl run fiscl serve`

## Your own data

```bash
fiscl accounts add "Chase Checking" --type checking
fiscl accounts add "Studio Checking" --type checking --scope business
fiscl import ~/Downloads/Chase1234_Activity.CSV --account "Chase Checking"
fiscl import ~/Downloads/statement.qfx          # OFX/QFX create the account automatically
```

If a CSV layout isn't recognized, run `fiscl profile init file.csv`, review the generated profile in `~/fiscl/profiles/`, then import again. Set `account = "…"` in a profile and you can just drop files into `~/fiscl/inbox/`; the server picks them up.

### Read-only bank sync (optional)

```bash
uv sync --extra sync
fiscl sync setup   # paste a SimpleFIN Bridge setup token; it's stored in your OS keychain
fiscl sync run
```

Teller (recommended): create a free developer account at teller.io, then connect in your terminal. The data flows bank -> Teller -> your machine; Teller holds the bank connection and sees the data in transit.

```bash
uv sync --extra sync
# optional, in ~/fiscl/config.toml:  [connectors.teller]  environment = "development"   # or "sandbox"
fiscl connect teller        # asks for your application id (hidden) and your Teller certificate/key files once
fiscl connect list
fiscl connect forget teller # revokes access at Teller and deletes the stored credentials
```

Plaid (alternative): create your own developer account at plaid.com. Sandbox works immediately with test banks; real banks need an approved production app (the Trial plan covers 10 Items, and removing an Item does not free its slot). The data flows bank -> Plaid -> your machine; Plaid holds the bank connection and sees the data in transit. fiscl only ever asks Plaid for the `transactions` product (balances and transactions), never account numbers, identity, or transfers.

```bash
uv sync --extra sync
# optional, in ~/fiscl/config.toml:  [connectors.plaid]  environment = "sandbox"   # default; or "production"
fiscl connect plaid         # asks for your client_id and secret (hidden) once, then opens Plaid Link in your browser
fiscl connect forget plaid  # calls /item/remove at Plaid and deletes the stored credentials
```

Sync is incremental (Plaid's cursor): the first pull imports what Plaid has, later pulls add new, changed, and removed transactions, and pending charges are replaced by their posted versions without losing your notes, tags, or categories.

Then ask your assistant to run `sync_accounts` (all connected providers, or `provider: teller | plaid | simplefin`) and `list_connections`.

## What it can do

| Area | Tools |
|---|---|
| Spending & cash flow | `query_transactions`, `spending_summary`, `cash_flow` |
| Subscriptions & bills | `detect_recurring` |
| Budgets | `budget_status`, `suggest_budgets` |
| Forecast | `forecast_cash` (low point, card payments, safe-to-spend) |
| Charts | `show_chart` (renders in-chat on MCP Apps hosts) |
| Categorizing | `list_uncategorized`, `apply_categories` |
| Business & tax | `set_scope`, `schedule_c_draft`, `review_writeoffs`, `estimated_tax`, `log_trip`, `mileage_log` |
| Bank sync | `sync_accounts` |

There are also prompts for common jobs: `monthly_review`, `subscription_audit`, `categorize_backlog`, `cash_forecast`, and `tax_checkup`.

Tax features organize your records for you and your tax preparer. They are not tax advice.

## Safety

Tools that change local data default to a dry run (`dry_run=true`) that shows the plan first. That is a safety default, not a gate: your MCP client's tool approval is the human gate, and tools that can remove or overwrite data are annotated as destructive so clients can ask accordingly. `sync_accounts` writes without a dry run and may make billed provider calls. Card numbers and account numbers are redacted from every response, and an audit log records what the model asked for (`fiscl audit`). See [SECURITY.md](SECURITY.md) and §14 of [docs/PROJECT.md](docs/PROJECT.md).

### Moving machines or restoring a backup

Account identity uses a per-install secret key (OS keychain, or a `0600` `.install_key` file in the data folder). Backups contain the database only, so back the key up separately from your database backups (a password manager is a good place):

```bash
fiscl key export ~/fiscl-key.txt   # new owner-only file; asks for a hidden confirmation; never printed
fiscl key import ~/Desktop/fiscl-key.txt   # on the new machine, before importing anything
```

Without the original key, re-importing the same bank files creates duplicate accounts. `fiscl doctor` warns when the key on this machine doesn't match the database. Delete the exported file once it is stored safely.

Disconnecting a bank (`fiscl connect forget`, `fiscl sync forget`) removes stored credentials and revokes access at Teller and Plaid, but it does not delete the transactions already synced into your database or any backups. SimpleFIN forget is local only: also revoke the access at the SimpleFIN Bridge. If you downloaded Teller certificate/key files, delete them after fiscl copies them into the keychain.

## Development

```bash
uv run pytest
uv run ruff check src tests
```

The full design and roadmap are in [docs/PROJECT.md](docs/PROJECT.md).
