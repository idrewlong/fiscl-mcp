"""Query and analysis services. Every number a tool reports is computed here, in SQL or integer math.

Each service is `fn(conn, cfg, **args) -> dict` and is wrapped as an MCP tool in server.py. New tools go in
the module that matches their area (or a new one), and are re-exported here.
"""

from ..common import MAX_BULK_ROWS, data_freshness, meta, resolve_target
from .budgets import budget_status, set_budget, suggest_budgets
from .edits import add_note, apply_categories, dismiss_recurring, split_transaction, tag_transactions
from .forecast import LIQUID_TYPES, forecast_cash
from .planning import afford, debt_payoff, goal_progress, manage_asset, manage_goal, net_worth
from .recurring_report import list_recurring
from .spending import cash_flow, spending_summary
from .transactions import import_status, list_accounts, list_uncategorized, query_transactions

__all__ = [
    "LIQUID_TYPES",
    "MAX_BULK_ROWS",
    "add_note",
    "afford",
    "apply_categories",
    "budget_status",
    "cash_flow",
    "data_freshness",
    "debt_payoff",
    "dismiss_recurring",
    "forecast_cash",
    "goal_progress",
    "import_status",
    "list_accounts",
    "list_recurring",
    "list_uncategorized",
    "manage_asset",
    "manage_goal",
    "meta",
    "net_worth",
    "query_transactions",
    "resolve_target",
    "set_budget",
    "spending_summary",
    "split_transaction",
    "suggest_budgets",
    "tag_transactions",
]
