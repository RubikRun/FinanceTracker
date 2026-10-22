"""FinanceTracker core package.

This package is the UI-independent API for the app. It knows how to load,
save and query finance records, but knows nothing about Qt. The Qt UI and the
small CLI (cli.py) are both just thin clients on top of `FinanceStore`.

`finance.crypto` encrypts snapshot bytes. The GUI and CLI do not call it yet.

`finance.profile`, `finance.google_auth`, `finance.drive_folder`, and
`finance.google_setup` are the per-user config and Google sign-in pieces.
The GUI and CLI do not call them yet. Cloud mode is not on, and running
``python -m finance.google_setup`` does not upload finance data.
"""
from .config import (
    CATEGORIES,
    CURRENCY_SYMBOL,
    USER_BORIS,
    USER_MILENA,
    USERS,
    VIEW_BOTH_LABEL,
    default_data_path,
    format_amount,
)
from .crypto import (
    CryptoError,
    DecryptedSnapshot,
    SnapshotMetadata,
    decrypt,
    encrypt,
    read_unverified_metadata,
)
from .models import IncomeRecord, SpendingRecord
from .store import FinanceStore, month_range

__all__ = [
    "CATEGORIES",
    "CURRENCY_SYMBOL",
    "USER_BORIS",
    "USER_MILENA",
    "USERS",
    "VIEW_BOTH_LABEL",
    "default_data_path",
    "format_amount",
    "IncomeRecord",
    "SpendingRecord",
    "FinanceStore",
    "month_range",
    "CryptoError",
    "DecryptedSnapshot",
    "SnapshotMetadata",
    "decrypt",
    "encrypt",
    "read_unverified_metadata",
]
