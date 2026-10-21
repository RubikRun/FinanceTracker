"""Record serialization: schema version 2 shape, users, dates, and amounts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance import USERS
from finance.models import IncomeRecord, SpendingRecord
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload


def _spending(**overrides: object) -> dict:
    data = {
        "id": 1,
        "amount": 14.2,
        "category": "Groceries",
        "date": "2026-10-13",
        "date_recorded": "2026-10-14",
        "user": "Boris",
        "note": "weekly shop",
    }
    data.update(overrides)
    return data


def _income(**overrides: object) -> dict:
    data = {
        "id": 2,
        "amount": 250,
        "date": "2026-10-04",
        "date_recorded": "2026-10-05",
        "user": "Milena",
        "note": "",
    }
    data.update(overrides)
    return data


def test_configured_users_are_boris_and_milena() -> None:
    assert USERS == ("Boris", "Milena")


def test_spending_round_trip_keeps_ownership_dates_and_decimal_amount() -> None:
    record = SpendingRecord.from_dict(_spending())

    assert record.id == 1
    assert record.amount == Decimal("14.20")
    assert type(record.amount) is Decimal
    assert record.category == "Groceries"
    assert record.date == date(2026, 10, 13)
    assert record.date_recorded == date(2026, 10, 14)
    assert record.user == "Boris"
    assert record.note == "weekly shop"
    assert record.to_dict() == _spending()


def test_income_round_trip_keeps_ownership_dates_and_decimal_amount() -> None:
    record = IncomeRecord.from_dict(_income())

    assert record.amount == Decimal("250")
    assert type(record.amount) is Decimal
    assert record.date == date(2026, 10, 4)
    assert record.date_recorded == date(2026, 10, 5)
    assert record.user == "Milena"
    assert record.note == ""
    assert list(record.to_dict()) == [
        "id",
        "amount",
        "date",
        "date_recorded",
        "user",
        "note",
    ]
    assert "category" not in record.to_dict()


def test_spending_dict_key_order_matches_the_schema() -> None:
    assert list(SpendingRecord.from_dict(_spending()).to_dict()) == [
        "id",
        "amount",
        "category",
        "date",
        "date_recorded",
        "user",
        "note",
    ]


def test_dates_are_stored_as_iso_strings() -> None:
    spending = SpendingRecord.from_dict(_spending()).to_dict()
    income = IncomeRecord.from_dict(_income()).to_dict()

    assert spending["date"] == "2026-10-13"
    assert spending["date_recorded"] == "2026-10-14"
    assert income["date"] == "2026-10-04"
    assert "/" not in spending["date"]


@pytest.mark.parametrize(
    ("amount", "json_value", "json_type"),
    [
        ("14.00", 14, int),
        ("20", 20, int),
        (250, 250, int),
        ("14.20", 14.2, float),
        ("0.10", 0.1, float),
        ("19.99", 19.99, float),
    ],
)
def test_amount_json_form_distinguishes_whole_numbers_from_fractions(
    amount: object, json_value: object, json_type: type
) -> None:
    record = SpendingRecord.from_dict(_spending(amount=json_value))

    assert record.amount == Decimal(str(amount))
    stored = record.to_dict()["amount"]
    assert type(stored) is json_type
    assert stored == json_value


def test_empty_and_missing_notes_become_empty_strings() -> None:
    empty = SpendingRecord.from_dict(_spending(note=""))
    missing = SpendingRecord.from_dict(
        {key: value for key, value in _spending().items() if key != "note"}
    )

    assert empty.note == ""
    assert missing.note == ""
    assert empty.to_dict()["note"] == ""


def test_unicode_and_quoted_notes_round_trip() -> None:
    note = 'café "ticket" — билет'
    record = IncomeRecord.from_dict(_income(note=note))

    assert record.note == note
    assert IncomeRecord.from_dict(record.to_dict()).note == note


def test_unknown_category_is_preserved() -> None:
    record = SpendingRecord.from_dict(_spending(category="Food"))

    assert record.category == "Food"
    assert record.to_dict()["category"] == "Food"


def test_missing_category_becomes_an_empty_string() -> None:
    data = {key: value for key, value in _spending().items() if key != "category"}

    assert SpendingRecord.from_dict(data).category == ""


@pytest.mark.parametrize("user", ["Boris", "Milena"])
def test_both_configured_users_are_accepted(user: str) -> None:
    assert SpendingRecord.from_dict(_spending(user=user)).user == user
    assert IncomeRecord.from_dict(_income(user=user)).user == user


@pytest.mark.parametrize("factory", [SpendingRecord.from_dict, IncomeRecord.from_dict])
def test_unknown_user_fails_without_leaking_the_note(factory) -> None:
    if factory is SpendingRecord.from_dict:
        payload = _spending(user="Pat", note=SYNTHETIC_SECRET)
    else:
        payload = _income(user="Pat", note=SYNTHETIC_SECRET)

    with pytest.raises(ValueError, match="Unknown user 'Pat'") as exc_info:
        factory(payload)

    assert_failure_omits_payload(exc_info.value)
