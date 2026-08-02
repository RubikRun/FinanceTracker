"""The finance data store: load/save JSON plus all queries.

`FinanceStore` is the single entry point for any client (Qt UI, CLI, scripts).
Clients never touch the JSON file directly; they call methods here. This keeps
the data format and query logic in one place and makes the UI replaceable.
"""
from __future__ import annotations

import calendar
import json
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional, Union

from .config import USERS
from .models import IncomeRecord, SpendingRecord

# Bumped if the on-disk JSON layout ever changes (helps future migrations).
DATA_VERSION = 2

PathLike = Union[str, Path]


def month_range(year: int, month: int) -> tuple[date, date]:
    """First and last calendar day of a given month, inclusive."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _matches_user(record_user: str, user: Optional[str]) -> bool:
    """`user=None` means both people (no filter)."""
    return user is None or record_user == user


class FinanceStore:
    """In-memory collection of records backed by a single JSON file."""

    def __init__(
        self,
        path: PathLike,
        spending: Optional[Iterable[SpendingRecord]] = None,
        income: Optional[Iterable[IncomeRecord]] = None,
        next_id: int = 1,
    ) -> None:
        self.path = Path(path)
        self.spending: list[SpendingRecord] = list(spending or [])
        self.income: list[IncomeRecord] = list(income or [])
        self._next_id = next_id

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: PathLike) -> "FinanceStore":
        """Load a store from `path`. A missing file yields an empty store."""
        path = Path(path)
        if not path.exists():
            return cls(path)

        with path.open("r", encoding="utf-8") as f:
            # parse_float=Decimal keeps amounts exact instead of going via float.
            data = json.load(f, parse_float=Decimal)

        spending = [SpendingRecord.from_dict(d) for d in data.get("spending", [])]
        income = [IncomeRecord.from_dict(d) for d in data.get("income", [])]
        next_id = int(data.get("next_id", cls._compute_next_id(spending, income)))
        return cls(path, spending, income, next_id)

    def save(self) -> None:
        """Write the store to disk atomically (temp file + replace)."""
        payload = {
            "version": DATA_VERSION,
            "next_id": self._next_id,
            "spending": [r.to_dict() for r in self.spending],
            "income": [r.to_dict() for r in self.income],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)  # atomic on Windows and macOS
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    @staticmethod
    def _compute_next_id(
        spending: Iterable[SpendingRecord], income: Iterable[IncomeRecord]
    ) -> int:
        ids = [r.id for r in spending] + [r.id for r in income]
        return (max(ids) + 1) if ids else 1

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def add_spending(
        self,
        amount: Union[Decimal, str, float, int],
        category: str,
        on_date: date,
        user: str,
        note: str = "",
        recorded_on: Optional[date] = None,
    ) -> SpendingRecord:
        if user not in USERS:
            raise ValueError(f"Unknown user {user!r}; expected one of {USERS}")
        record = SpendingRecord(
            id=self._take_id(),
            amount=Decimal(str(amount)),
            category=category,
            date=on_date,
            date_recorded=recorded_on or date.today(),
            user=user,
            note=note,
        )
        self.spending.append(record)
        return record

    def add_income(
        self,
        amount: Union[Decimal, str, float, int],
        on_date: date,
        user: str,
        note: str = "",
        recorded_on: Optional[date] = None,
    ) -> IncomeRecord:
        if user not in USERS:
            raise ValueError(f"Unknown user {user!r}; expected one of {USERS}")
        record = IncomeRecord(
            id=self._take_id(),
            amount=Decimal(str(amount)),
            date=on_date,
            date_recorded=recorded_on or date.today(),
            user=user,
            note=note,
        )
        self.income.append(record)
        return record

    def _take_id(self) -> int:
        # A single global counter shared by both record types, matching the
        # example data where IDs run continuously across spending and income.
        new_id = self._next_id
        self._next_id += 1
        return new_id

    # ------------------------------------------------------------------ #
    # Queries (period = inclusive [start, end] date range)
    # `user=None` means both people; otherwise filter to that person.
    # ------------------------------------------------------------------ #
    def spending_between(
        self, start: date, end: date, user: Optional[str] = None
    ) -> list[SpendingRecord]:
        return [
            r
            for r in self.spending
            if start <= r.date <= end and _matches_user(r.user, user)
        ]

    def income_between(
        self, start: date, end: date, user: Optional[str] = None
    ) -> list[IncomeRecord]:
        return [
            r
            for r in self.income
            if start <= r.date <= end and _matches_user(r.user, user)
        ]

    def total_spent(
        self, start: date, end: date, user: Optional[str] = None
    ) -> Decimal:
        return sum(
            (r.amount for r in self.spending_between(start, end, user)), Decimal("0")
        )

    def total_earned(
        self, start: date, end: date, user: Optional[str] = None
    ) -> Decimal:
        return sum(
            (r.amount for r in self.income_between(start, end, user)), Decimal("0")
        )

    def gap(self, start: date, end: date, user: Optional[str] = None) -> Decimal:
        """Income minus spending for the period (positive = saved money)."""
        return self.total_earned(start, end, user) - self.total_spent(start, end, user)

    def available_months(self, user: Optional[str] = None) -> list[tuple[int, int]]:
        """`(year, month)` pairs that have at least one matching record, newest first.

        Used to populate the period dropdown so it only offers months that
        actually contain data for the active view.
        """
        months: set[tuple[int, int]] = set()
        for record in self.spending:
            if _matches_user(record.user, user):
                months.add((record.date.year, record.date.month))
        for record in self.income:
            if _matches_user(record.user, user):
                months.add((record.date.year, record.date.month))
        return sorted(months, reverse=True)
