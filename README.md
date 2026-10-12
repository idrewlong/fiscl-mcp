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
uv run fiscl demo        # loads six months of synthetic personal + business data
uv run fiscl serve       # MCP server over stdio
```

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

Any tool that changes data does a dry run first. Card numbers and account numbers are redacted from every response, and an audit log records what the model asked for (`fiscl audit`). See [SECURITY.md](SECURITY.md) and §14 of [PROJECT.md](PROJECT.md).

## Development

```bash
uv run pytest
uv run ruff check src tests
```

The full design and roadmap are in [PROJECT.md](PROJECT.md).
