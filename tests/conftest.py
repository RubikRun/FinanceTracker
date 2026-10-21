"""Fixtures for finance-core tests.

Every store path is under pytest's temporary directory, never the private
`finance_data.json` next to the application.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finance import default_data_path
from finance.store import FinanceStore


@pytest.fixture
def data_path(tmp_path: Path) -> Path:
    path = tmp_path / "finance_data.json"
    assert path.resolve() != default_data_path().resolve()
    return path


@pytest.fixture
def store(data_path: Path) -> FinanceStore:
    return FinanceStore(data_path)
