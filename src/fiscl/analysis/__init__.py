"""Query and analysis services. Every number a tool reports is computed here, in SQL or integer math.

Each service is `fn(conn, cfg, **args) -> dict` and is wrapped as an MCP tool in server.py. New tools go in
the module that matches their area (or a new one), and are re-exported here.
"""

from ..common import MAX_BULK_ROWS, data_freshness, meta, resolve_target
from .budgets import budget_status, suggest_budgets
from .edits import apply_categories
from .forecast import LIQUID_TYPES, forecast_cash
from .recurring_report import list_recurring
from .spending import cash_flow, spending_summary
from .transactions import import_status, list_accounts, list_uncategorized, query_transactions

__all__ = [
    "LIQUID_TYPES",
    "MAX_BULK_ROWS",
    "apply_categories",
    "budget_status",
    "cash_flow",
    "data_freshness",
    "forecast_cash",
    "import_status",
    "list_accounts",
    "list_recurring",
    "list_uncategorized",
    "meta",
    "query_transactions",
    "resolve_target",
    "spending_summary",
    "suggest_budgets",
]
