"""Totals, categories, months, and inclusive date ranges."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

import pytest

from finance import month_range
from finance.store import FinanceStore


def _spend(
    store: FinanceStore,
    amount: str,
    category: str,
    on: date,
    user: str,
    note: str = "",
    recorded_on: date | None = None,
):
    return store.add_spending(
        amount=amount,
        category=category,
        on_date=on,
        user=user,
        note=note,
        recorded_on=recorded_on or date(2026, 1, 15),
    )


def _earn(
    store: FinanceStore,
    amount: str,
    on: date,
    user: str,
    recorded_on: date | None = None,
):
    return store.add_income(
        amount=amount,
        on_date=on,
        user=user,
        recorded_on=recorded_on or date(2026, 1, 15),
    )


def test_empty_store_totals_are_zero_decimals(store: FinanceStore) -> None:
    start, end = month_range(2026, 10)

    assert store.total_spent(start, end) == Decimal("0")
    assert isinstance(store.total_spent(start, end), Decimal)
    assert store.total_earned(start, end) == Decimal("0")
    assert store.gap(start, end) == Decimal("0")
    assert store.spending_between(start, end) == []
    assert store.income_between(start, end) == []
    assert store.spending_by_category(start, end) == []
    assert store.spending_split_by_category(start, end) == {}
    assert store.available_months() == []


def test_date_range_is_inclusive(store: FinanceStore) -> None:
    start, end = month_range(2026, 10)
    _spend(store, "1.00", "Misc", date(2026, 9, 30), "Boris")
    opening = _spend(store, "2.00", "Misc", start, "Boris")
    closing = _spend(store, "3.00", "Misc", end, "Milena")
    _spend(store, "4.00", "Misc", date(2026, 11, 1), "Milena")

    matched = {record.id for record in store.spending_between(start, end)}

    assert matched == {opening.id, closing.id}
    assert store.total_spent(start, end) == Decimal("5.00")


def test_period_uses_transaction_date_not_recorded_date(store: FinanceStore) -> None:
    _spend(
        store,
        "6.00",
        "Groceries",
        date(2026, 9, 15),
        "Boris",
        recorded_on=date(2026, 10, 1),
    )
    september = month_range(2026, 9)
    october = month_range(2026, 10)

    assert len(store.spending_between(*september)) == 1
    assert store.spending_between(*october) == []
    assert store.available_months() == [(2026, 9)]


def test_totals_and_gap_respect_person_scope(store: FinanceStore) -> None:
    start, end = month_range(2026, 10)
    _spend(store, "10.00", "Groceries", date(2026, 10, 1), "Boris")
    _earn(store, "100.00", date(2026, 10, 2), "Boris")
    _spend(store, "3.50", "Eating out", date(2026, 10, 31), "Milena")
    _earn(store, "40.00", date(2026, 10, 15), "Milena")
    _spend(store, "99.00", "Misc", date(2026, 9, 30), "Boris")

    assert store.total_spent(start, end, "Boris") == Decimal("10.00")
    assert store.total_earned(start, end, "Boris") == Decimal("100.00")
    assert store.gap(start, end, "Boris") == Decimal("90.00")
    assert store.total_spent(start, end, "Milena") == Decimal("3.50")
    assert store.total_earned(start, end, "Milena") == Decimal("40.00")
    assert store.gap(start, end, "Milena") == Decimal("36.50")
    assert store.total_spent(start, end) == Decimal("13.50")
    assert store.total_earned(start, end) == Decimal("140.00")
    assert store.gap(start, end) == Decimal("126.50")


def test_negative_gap_is_earned_minus_spent(store: FinanceStore) -> None:
    start, end = month_range(2026, 8)
    _earn(store, "5.00", date(2026, 8, 2), "Milena")
    _spend(store, "20.00", "Rent", date(2026, 8, 3), "Milena")

    assert store.gap(start, end, "Milena") == Decimal("-15.00")


def test_decimal_totals_stay_exact(store: FinanceStore) -> None:
    start, end = month_range(2026, 5)
    for day in (1, 2, 3):
        _spend(store, "0.10", "Groceries", date(2026, 5, day), "Boris")
    _earn(store, "1.00", date(2026, 5, 4), "Boris")

    assert store.total_spent(start, end, "Boris") == Decimal("0.30")
    assert store.gap(start, end, "Boris") == Decimal("0.70")


def test_spending_by_category_sorts_by_amount_then_name(store: FinanceStore) -> None:
    start, end = month_range(2026, 10)
    _spend(store, "10.00", "Groceries", date(2026, 10, 1), "Boris")
    _spend(store, "0.50", "Groceries", date(2026, 10, 2), "Boris")
    _spend(store, "10.00", "Holiday", date(2026, 10, 3), "Milena")
    _spend(store, "10.00", "Bills / Utilities", date(2026, 10, 4), "Milena")
    _spend(store, "4.00", "Eating out", date(2026, 9, 1), "Boris")

    assert store.spending_by_category(start, end) == [
        ("Groceries", Decimal("10.50")),
        ("Bills / Utilities", Decimal("10.00")),
        ("Holiday", Decimal("10.00")),
    ]
    assert store.spending_by_category(start, end, "Boris") == [
        ("Groceries", Decimal("10.50"))
    ]


def test_empty_category_is_reported_as_uncategorized(store: FinanceStore) -> None:
    start, end = month_range(2026, 10)
    _spend(store, "2.00", "", date(2026, 10, 1), "Boris")
    _spend(store, "1.25", "", date(2026, 10, 2), "Milena")

    assert store.spending_by_category(start, end) == [
        ("Uncategorized", Decimal("3.25"))
    ]


def test_spending_split_by_category_omits_people_who_spent_nothing(
    store: FinanceStore,
) -> None:
    start, end = month_range(2026, 10)
    _spend(store, "10.00", "Groceries", date(2026, 10, 1), "Boris")
    _spend(store, "4.00", "Groceries", date(2026, 10, 2), "Milena")
    _spend(store, "2.00", "Transport", date(2026, 10, 3), "Boris")

    split = store.spending_split_by_category(start, end)

    assert split == {
        "Groceries": {"Boris": Decimal("10.00"), "Milena": Decimal("4.00")},
        "Transport": {"Boris": Decimal("2.00")},
    }
    assert "Milena" not in split["Transport"]


def test_available_months_are_unique_and_newest_first(store: FinanceStore) -> None:
    _spend(store, "1.00", "Misc", date(2026, 1, 10), "Boris")
    _earn(store, "2.00", date(2026, 1, 11), "Boris")
    _earn(store, "3.00", date(2026, 3, 1), "Milena")
    _spend(store, "4.00", "Misc", date(2025, 12, 31), "Boris")
    _spend(store, "5.00", "Misc", date(2026, 11, 2), "Milena")

    assert store.available_months() == [
        (2026, 11),
        (2026, 3),
        (2026, 1),
        (2025, 12),
    ]
    assert store.available_months("Boris") == [(2026, 1), (2025, 12)]
    assert store.available_months("Milena") == [(2026, 11), (2026, 3)]


def test_month_range_covers_leap_and_non_leap_february(store: FinanceStore) -> None:
    assert month_range(2024, 2) == (date(2024, 2, 1), date(2024, 2, 29))
    assert month_range(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_range(2026, 10) == (date(2026, 10, 1), date(2026, 10, 31))

    leap_day = _spend(store, "7.00", "Holiday", date(2024, 2, 29), "Milena")

    assert store.spending_between(*month_range(2024, 2)) == [leap_day]
    assert store.spending_between(*month_range(2023, 2)) == []
    with pytest.raises(calendar.IllegalMonthError):
        month_range(2026, 13)
