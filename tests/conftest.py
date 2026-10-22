"""Fixtures for finance-core tests.

Every store path is under pytest's temporary directory, never the private
`finance_data.json` next to the application.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finance import default_data_path
from finance.store import FinanceStore


@pytest.fixture(autouse=True)
def isolated_app_profile(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Keep every test off the production config directory and keyring slot.

    The directory is a sibling of the test's ``tmp_path``, so a test that
    asserts its own temporary directory stays empty is not affected.
    ``FINANCETRACKER_CONFIG_DIR`` selects the profile and a non-production
    keyring service name. Tests that need the default platform directory
    clear the variable themselves.
    """
    directory = tmp_path_factory.mktemp("app-profile")
    monkeypatch.setenv("FINANCETRACKER_CONFIG_DIR", str(directory))
    return directory


@pytest.fixture
def data_path(tmp_path: Path) -> Path:
    path = tmp_path / "finance_data.json"
    assert path.resolve() != default_data_path().resolve()
    return path


@pytest.fixture
def store(data_path: Path) -> FinanceStore:
    return FinanceStore(data_path)
