"""Data models for finance records.

Records are plain dataclasses. Amounts are always `Decimal` (never float) so
totals stay exact, and dates are always `datetime.date`. Conversion to/from the
JSON-friendly representation (numbers + ISO date strings) happens here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .config import ISO_DATE_FORMAT, USERS


def _to_decimal(value: Any) -> Decimal:
    # str() first so floats coming from JSON don't introduce binary noise.
    return Decimal(str(value))


def _amount_to_json(value: Decimal):
    # Keep the JSON tidy for hand-editing: whole numbers stay ints (3560,
    # not 3560.0); others become plain floats (14.2).
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _date_from_iso(value: str) -> date:
    return datetime.strptime(value, ISO_DATE_FORMAT).date()


def _date_to_iso(value: date) -> str:
    return value.strftime(ISO_DATE_FORMAT)


def _parse_user(value: Any) -> str:
    user = str(value)
    if user not in USERS:
        raise ValueError(f"Unknown user {user!r}; expected one of {USERS}")
    return user


@dataclass
class SpendingRecord:
    id: int
    amount: Decimal
    category: str
    date: date          # date of purchase
    date_recorded: date
    user: str           # Boris or Milena
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "amount": _amount_to_json(self.amount),
            "category": self.category,
            "date": _date_to_iso(self.date),
            "date_recorded": _date_to_iso(self.date_recorded),
            "user": self.user,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SpendingRecord":
        return cls(
            id=int(data["id"]),
            amount=_to_decimal(data["amount"]),
            category=str(data.get("category", "")),
            date=_date_from_iso(data["date"]),
            date_recorded=_date_from_iso(data["date_recorded"]),
            user=_parse_user(data["user"]),
            note=str(data.get("note", "")),
        )


@dataclass
class IncomeRecord:
    id: int
    amount: Decimal
    date: date          # date of income
    date_recorded: date
    user: str           # Boris or Milena
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "amount": _amount_to_json(self.amount),
            "date": _date_to_iso(self.date),
            "date_recorded": _date_to_iso(self.date_recorded),
            "user": self.user,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IncomeRecord":
        return cls(
            id=int(data["id"]),
            amount=_to_decimal(data["amount"]),
            date=_date_from_iso(data["date"]),
            date_recorded=_date_from_iso(data["date_recorded"]),
            user=_parse_user(data["user"]),
            note=str(data.get("note", "")),
        )
