"""Local JSON load/save, including atomic replace and rejected files."""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal

import pytest

from finance.store import DATA_VERSION, FinanceStore
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

# Logical document produced by today's saver for `_sample_store`: schema
# version 2, indent 2, UTF-8, integral amounts as JSON integers, and
# fractional amounts as JSON numbers (14.20 is written 14.2). `save()` writes
# that text in text mode, so the file uses the platform newline.
_SCHEMA_V2_DOCUMENT = "\n".join(
    [
        "{",
        '  "version": 2,',
        '  "next_id": 3,',
        '  "spending": [',
        "    {",
        '      "id": 1,',
        '      "amount": 14.2,',
        '      "category": "Groceries",',
        '      "date": "2026-10-13",',
        '      "date_recorded": "2026-10-14",',
        '      "user": "Boris",',
        '      "note": "weekly shop"',
        "    }",
        "  ],",
        '  "income": [',
        "    {",
        '      "id": 2,',
        '      "amount": 250,',
        '      "date": "2026-10-04",',
        '      "date_recorded": "2026-10-05",',
        '      "user": "Milena",',
        '      "note": ""',
        "    }",
        "  ]",
        "}",
    ]
)


def _sample_store(path) -> FinanceStore:
    store = FinanceStore(path)
    store.add_spending(
        amount="14.20",
        category="Groceries",
        on_date=date(2026, 10, 13),
        user="Boris",
        note="weekly shop",
        recorded_on=date(2026, 10, 14),
    )
    store.add_income(
        amount=250,
        on_date=date(2026, 10, 4),
        user="Milena",
        note="",
        recorded_on=date(2026, 10, 5),
    )
    return store


def _valid_spending(**overrides: object) -> dict:
    data = {
        "id": 4,
        "amount": 12.5,
        "category": "Groceries",
        "date": "2026-10-01",
        "date_recorded": "2026-10-02",
        "user": "Boris",
        "note": SYNTHETIC_SECRET,
    }
    data.update(overrides)
    return data


def test_schema_version_constant_is_two() -> None:
    assert DATA_VERSION == 2


def test_missing_file_loads_as_an_empty_store_without_creating_it(data_path) -> None:
    store = FinanceStore.load(data_path)

    assert store.spending == []
    assert store.income == []
    assert data_path.exists() is False


def test_save_writes_the_current_schema_document(data_path) -> None:
    store = _sample_store(data_path)
    store.save()

    assert data_path.read_text(encoding="utf-8") == _SCHEMA_V2_DOCUMENT
    assert data_path.read_bytes() == _SCHEMA_V2_DOCUMENT.replace(
        "\n", os.linesep
    ).encode("utf-8")
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert isinstance(raw["spending"][0]["amount"], float)
    assert isinstance(raw["income"][0]["amount"], int)


def test_save_and_load_preserves_every_field(data_path) -> None:
    _sample_store(data_path).save()
    loaded = FinanceStore.load(data_path)

    spending = loaded.spending[0]
    income = loaded.income[0]
    assert spending.id == 1
    assert spending.amount == Decimal("14.20")
    assert type(spending.amount) is Decimal
    assert spending.category == "Groceries"
    assert spending.date == date(2026, 10, 13)
    assert spending.date_recorded == date(2026, 10, 14)
    assert spending.user == "Boris"
    assert spending.note == "weekly shop"
    assert income.id == 2
    assert income.amount == Decimal("250")
    assert income.date == date(2026, 10, 4)
    assert income.date_recorded == date(2026, 10, 5)
    assert income.user == "Milena"
    assert income.note == ""
    assert loaded.add_spending(
        amount="1.00",
        category="Misc",
        on_date=date(2026, 10, 20),
        user="Boris",
        recorded_on=date(2026, 10, 20),
    ).id == 3


def test_resave_without_changes_is_byte_for_byte(data_path) -> None:
    store = _sample_store(data_path)
    store.save()
    first = data_path.read_bytes()
    store.save()

    assert data_path.read_bytes() == first


@pytest.mark.parametrize(
    "amount",
    ["0.01", "0.10", "0.20", "0.30", "1.10", "1.23", "10.50", "14.20", "19.99", "100.05"],
)
def test_two_decimal_amounts_round_trip_as_decimals(data_path, amount: str) -> None:
    store = FinanceStore(data_path)
    store.add_spending(
        amount=amount,
        category="Misc",
        on_date=date(2026, 5, 2),
        user="Boris",
        recorded_on=date(2026, 5, 2),
    )
    store.save()

    assert FinanceStore.load(data_path).spending[0].amount == Decimal(amount)


def test_unicode_note_is_written_as_utf8(data_path) -> None:
    note = 'café "ticket" — билет'
    store = FinanceStore(data_path)
    store.add_income(
        amount="15.00",
        on_date=date(2026, 6, 1),
        user="Milena",
        note=note,
        recorded_on=date(2026, 6, 2),
    )
    store.save()
    raw = data_path.read_bytes()

    assert "café".encode("utf-8") in raw
    assert "билет".encode("utf-8") in raw
    assert "—".encode("utf-8") in raw
    assert b"\\u00e9" not in raw
    assert b'\\"' in raw
    assert FinanceStore.load(data_path).income[0].note == note


def test_missing_next_id_is_one_past_the_highest_id(data_path) -> None:
    data_path.write_text(
        json.dumps(
            {
                "version": 2,
                "spending": [_valid_spending(id=2, note="kept")],
                "income": [
                    {
                        "id": 5,
                        "amount": 40,
                        "date": "2026-10-03",
                        "date_recorded": "2026-10-03",
                        "user": "Milena",
                        "note": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = FinanceStore.load(data_path)
    added = loaded.add_spending(
        amount="1",
        category="Misc",
        on_date=date(2026, 10, 6),
        user="Boris",
        recorded_on=date(2026, 10, 6),
    )

    assert added.id == 6


def test_missing_record_lists_load_as_empty(data_path) -> None:
    data_path.write_text('{"version": 2}', encoding="utf-8")
    loaded = FinanceStore.load(data_path)

    assert loaded.spending == []
    assert loaded.income == []
    assert loaded.add_income(
        amount="8",
        on_date=date(2026, 1, 1),
        user="Milena",
        recorded_on=date(2026, 1, 1),
    ).id == 1


def test_deleted_ids_stay_unused_after_reload(store: FinanceStore) -> None:
    first = store.add_spending(
        amount="4.00",
        category="Transport",
        on_date=date(2026, 2, 2),
        user="Boris",
        recorded_on=date(2026, 2, 2),
    )
    store.add_income(
        amount="9.00",
        on_date=date(2026, 2, 3),
        user="Milena",
        recorded_on=date(2026, 2, 3),
    )
    store.delete_record(first.id)
    store.save()

    loaded = FinanceStore.load(store.path)
    added = loaded.add_income(
        amount="1.00",
        on_date=date(2026, 2, 4),
        user="Boris",
        recorded_on=date(2026, 2, 4),
    )

    assert added.id == 3
    assert loaded.get_spending(first.id) is None


def test_atomic_save_leaves_no_temp_file(store: FinanceStore) -> None:
    store.add_income(
        amount="3.00",
        on_date=date(2026, 7, 1),
        user="Boris",
        recorded_on=date(2026, 7, 1),
    )
    store.save()

    assert store.path.is_file()
    assert list(store.path.parent.glob("*.tmp")) == []


def test_failed_replace_keeps_the_previous_file_and_removes_the_temp(
    store: FinanceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_income(
        amount="3.00",
        on_date=date(2026, 7, 1),
        user="Boris",
        recorded_on=date(2026, 7, 1),
    )
    store.save()
    original = store.path.read_bytes()
    store.add_income(
        amount="4.00",
        on_date=date(2026, 7, 2),
        user="Milena",
        recorded_on=date(2026, 7, 2),
    )

    def fail_replace(src: str, dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("finance.store.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.save()

    assert store.path.read_bytes() == original
    assert list(store.path.parent.glob("*.tmp")) == []
    assert len(store.income) == 2
    assert len(FinanceStore.load(store.path).income) == 1


def test_save_creates_a_missing_parent_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "finance_data.json"
    store = FinanceStore(path)
    store.add_income(
        amount="15",
        on_date=date(2026, 4, 1),
        user="Milena",
        recorded_on=date(2026, 4, 1),
    )
    store.save()

    assert path.is_file()
    assert FinanceStore.load(path).income[0].amount == Decimal("15")


def test_malformed_json_fails_without_leaking_payload(data_path) -> None:
    data_path.write_text(
        '{"version": 2, "spending": [{"note": "%s", ' % SYNTHETIC_SECRET,
        encoding="utf-8",
    )

    with pytest.raises(json.JSONDecodeError) as exc_info:
        FinanceStore.load(data_path)

    assert str(exc_info.value)
    assert_failure_omits_payload(exc_info.value)
    assert list(data_path.parent.glob("*.tmp")) == []


def test_missing_user_fails_without_leaking_payload(data_path) -> None:
    payload = _valid_spending()
    del payload["user"]
    data_path.write_text(
        json.dumps({"version": 2, "next_id": 5, "spending": [payload], "income": []}),
        encoding="utf-8",
    )

    with pytest.raises(KeyError) as exc_info:
        FinanceStore.load(data_path)

    assert_failure_omits_payload(exc_info.value)


def test_unknown_user_in_file_fails_with_a_clear_message(data_path) -> None:
    data_path.write_text(
        json.dumps(
            {
                "version": 2,
                "next_id": 5,
                "spending": [_valid_spending(user="Pat")],
                "income": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown user 'Pat'") as exc_info:
        FinanceStore.load(data_path)

    assert SYNTHETIC_SECRET not in str(exc_info.value)
    assert_failure_omits_payload(exc_info.value)


def test_invalid_date_fails_without_leaking_payload(data_path) -> None:
    data_path.write_text(
        json.dumps(
            {
                "version": 2,
                "next_id": 5,
                "spending": [_valid_spending(date="2026-13-40")],
                "income": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        FinanceStore.load(data_path)

    assert "2026-13-40" in str(exc_info.value)
    assert_failure_omits_payload(exc_info.value)
