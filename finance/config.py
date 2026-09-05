"""Shared configuration and small formatting helpers.

Everything UI-agnostic that needs a single source of truth lives here so the
core API, the CLI and the Qt UI all stay consistent. Edit the constants below
to tweak categories, currency, date formatting or where the data file lives.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

# Predefined spending categories shown in the (fixed) Add-dialog dropdown.
# Add or remove entries here to change the available categories.
CATEGORIES: list[str] = [
    "Groceries",
    "Eating out",
    "Transport",
    "Rent",
    "Bills / Utilities",
    "Home Stuff",
    "Health",
    "Personal care",
    "Entertainment",
    "Sports",
    "Shopping",
    "Maintenance",
    "Car Stuff",
    "Work Expenses",
    "Hobbies",
    "Gifts",
    "Holiday",
    "Misc",
]

# The two people who use the app. Stored on every record as `user`.
USERS: tuple[str, ...] = ("Boris", "Milena")
USER_BORIS = USERS[0]
USER_MILENA = USERS[1]

# Combined view label (UI only — never stored on a record).
VIEW_BOTH_LABEL = "Boris & Milena"

# Currency symbol shown next to amounts (storage stays a plain number).
CURRENCY_SYMBOL = "\u20ac"  # euro sign

# How dates look in the UI vs how they are stored in the JSON file.
DISPLAY_DATE_FORMAT = "%d/%m/%Y"  # e.g. 11/06/2026 (shown to the user)
ISO_DATE_FORMAT = "%Y-%m-%d"      # e.g. 2026-06-11 (stored in JSON, sortable)

# Name of the JSON data file kept next to the application.
DATA_FILENAME = "finance_data.json"


def project_root() -> Path:
    """Directory that contains the application (one level above this package)."""
    return Path(__file__).resolve().parent.parent


def default_data_path() -> Path:
    """Full path to the JSON data file, stored next to the program."""
    return project_root() / DATA_FILENAME


def format_amount(value: Decimal) -> str:
    """Format a monetary amount with two decimals and the currency symbol."""
    return f"{value:.2f}\u00a0{CURRENCY_SYMBOL}"
