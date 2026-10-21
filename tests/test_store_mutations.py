"""Add, edit, convert, and delete. IDs are shared and are not reused."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance.models import IncomeRecord, SpendingRecord
from finance.store import FinanceStore


def _spend(store: FinanceStore, **overrides: object) -> SpendingRecord:
    fields = {
        "amount": "5.00",
        "category": "Transport",
        "on_date": date(2026, 3, 1),
        "user": "Boris",
        "note": "bus",
        "recorded_on": date(2026, 3, 2),
    }
    fields.update(overrides)
    return store.add_spending(**fields)


def _income(store: FinanceStore, **overrides: object) -> IncomeRecord:
    fields = {
        "amount": "80.00",
        "on_date": date(2026, 3, 3),
        "user": "Milena",
        "note": "refund",
        "recorded_on": date(2026, 3, 4),
    }
    fields.update(overrides)
    return store.add_income(**fields)


def test_spending_and_income_share_one_id_sequence(store: FinanceStore) -> None:
    spending = _spend(store)
    income = _income(store)
    another = _spend(store, on_date=date(2026, 3, 5), note="tram")

    assert spending.id == 1
    assert income.id == 2
    assert another.id == 3


def test_deleted_ids_are_not_reused(store: FinanceStore) -> None:
    first = _spend(store)
    _income(store)
    removed = store.delete_record(first.id)
    added = _spend(store, note="later")

    assert removed is first
    assert store.get_spending(first.id) is None
    assert added.id == 3
    with pytest.raises(KeyError, match="No record with id 99"):
        store.delete_record(99)


def test_unknown_user_is_rejected_and_not_stored(store: FinanceStore) -> None:
    with pytest.raises(ValueError, match="Unknown user 'Pat'"):
        store.add_spending(
            amount="1.00",
            category="Misc",
            on_date=date(2026, 1, 1),
            user="Pat",
        )
    with pytest.raises(ValueError, match="Unknown user 'Pat'"):
        store.add_income(amount="1.00", on_date=date(2026, 1, 1), user="Pat")

    assert store.spending == []
    assert store.income == []


def test_recorded_on_defaults_to_today(store: FinanceStore) -> None:
    before = date.today()
    record = store.add_income(
        amount="1.00", on_date=date(2026, 1, 1), user="Boris"
    )
    after = date.today()

    assert record.note == ""
    assert record.date_recorded in (before, after)


def test_update_in_place_preserves_identity_and_order(store: FinanceStore) -> None:
    first = _spend(store)
    second = _spend(
        store,
        on_date=date(2026, 3, 8),
        user="Milena",
        note="taxi",
        recorded_on=date(2026, 3, 9),
    )

    updated = store.update_record(
        first.id,
        "spending",
        amount="6.75",
        on_date=date(2026, 5, 5),
        note="tram",
        category="Holiday",
    )

    assert updated is first
    assert first.amount == Decimal("6.75")
    assert first.category == "Holiday"
    assert first.date == date(2026, 5, 5)
    assert first.note == "tram"
    assert first.user == "Boris"
    assert first.date_recorded == date(2026, 3, 2)
    assert [record.id for record in store.spending] == [first.id, second.id]
    assert second.user == "Milena"


def test_income_converts_to_spending_and_keeps_identity(store: FinanceStore) -> None:
    spending = _spend(store)
    income = _income(store)

    converted = store.update_record(
        income.id,
        "spending",
        amount="81.25",
        on_date=date(2026, 4, 1),
        note="reclassified",
        category="Misc",
    )

    assert isinstance(converted, SpendingRecord)
    assert converted.id == income.id
    assert converted.user == "Milena"
    assert converted.date_recorded == date(2026, 3, 4)
    assert converted.amount == Decimal("81.25")
    assert converted.category == "Misc"
    assert converted.date == date(2026, 4, 1)
    assert converted.note == "reclassified"
    assert store.get_income(income.id) is None
    assert store.get_spending(income.id) is converted
    assert [record.id for record in store.spending] == [spending.id, income.id]
    assert store.income == []


def test_spending_converts_to_income_and_drops_the_category(store: FinanceStore) -> None:
    spending = _spend(store)
    income = _income(store)

    converted = store.update_record(
        spending.id,
        "income",
        amount="5.50",
        on_date=date(2026, 3, 9),
        note="actually income",
    )

    assert isinstance(converted, IncomeRecord)
    assert converted.id == spending.id
    assert converted.user == "Boris"
    assert converted.date_recorded == date(2026, 3, 2)
    assert converted.amount == Decimal("5.50")
    assert converted.date == date(2026, 3, 9)
    assert converted.note == "actually income"
    assert store.get_spending(spending.id) is None
    assert [record.id for record in store.income] == [income.id, spending.id]
    assert store.spending == []


def test_unknown_record_type_and_unknown_id_are_rejected(store: FinanceStore) -> None:
    _spend(store)

    with pytest.raises(ValueError, match="Unknown record type 'transfer'"):
        store.update_record(
            1,
            "transfer",
            amount="1.00",
            on_date=date(2026, 3, 1),
        )
    with pytest.raises(KeyError, match="No record with id 99"):
        store.update_record(
            99,
            "spending",
            amount="1.00",
            on_date=date(2026, 3, 1),
            category="Misc",
        )


def test_lookup_returns_none_when_the_id_is_absent(store: FinanceStore) -> None:
    assert store.get_spending(1) is None
    assert store.get_income(1) is None
