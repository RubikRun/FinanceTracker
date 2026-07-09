"""FinanceTracker core package.

This package is the UI-independent API for the app. It knows how to load,
save and query finance records, but knows nothing about Qt. The Qt UI and the
small CLI (cli.py) are both just thin clients on top of `FinanceStore`.
"""
from .config import (
    CATEGORIES,
    CURRENCY_SYMBOL,
    default_data_path,
    format_amount,
)
from .models import IncomeRecord, SpendingRecord
from .store import FinanceStore, month_range

__all__ = [
    "CATEGORIES",
    "CURRENCY_SYMBOL",
    "default_data_path",
    "format_amount",
    "IncomeRecord",
    "SpendingRecord",
    "FinanceStore",
    "month_range",
]
