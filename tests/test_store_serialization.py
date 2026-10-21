"""In-memory schema-v2 documents. These tests must not create a data file."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance.store import FinanceStore

# Same text today's saver emits for `_sample_store`, with `\n` newlines.
_SAMPLE_DOCUMENT = "\n".join(
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
).encode("utf-8")


def _sample_store(path: Path) -> FinanceStore:
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


def _rich_store(path: Path) -> FinanceStore:
    store = _sample_store(path)
    removed = store.add_spending(
        amount="0.10",
        category="Food",
        on_date=date(2024, 2, 29),
        user="Milena",
        note="temporary row",
        recorded_on=date(2024, 3, 1),
    )
    store.delete_record(removed.id)
    store.spending[0].note = 'café "ticket" — билет'
    return store


def test_sample_document_is_canonical_and_stable(tmp_path: Path) -> None:
    store = _sample_store(tmp_path / "unused.json")

    assert store.to_json_bytes() == _SAMPLE_DOCUMENT
    assert store.to_json_bytes() == store.to_json_bytes()
    assert b"\r\n" not in store.to_json_bytes()


def test_round_trip_keeps_records_ids_and_next_id(tmp_path: Path) -> None:
    path = tmp_path / "unused.json"
    original = _rich_store(path)
    payload = original.to_json_bytes()
    restored = FinanceStore.from_json_bytes(payload, path)

    assert restored.to_json_bytes() == payload
    assert len(restored.spending) == 1
    assert restored.income[0].id == 2
    spending = restored.spending[0]
    income = restored.income[0]
    assert spending.id == 1
    assert spending.amount == Decimal("14.20")
    assert type(spending.amount) is Decimal
    assert spending.category == "Groceries"
    assert spending.date == date(2026, 10, 13)
    assert spending.date_recorded == date(2026, 10, 14)
    assert spending.user == "Boris"
    assert spending.note == 'café "ticket" — билет'
    assert income.amount == Decimal("250")
    assert type(income.amount) is Decimal
    assert income.date == date(2026, 10, 4)
    assert income.date_recorded == date(2026, 10, 5)
    assert income.user == "Milena"
    assert income.note == ""
    assert b"temporary row" not in payload
    assert 'café'.encode("utf-8") in payload
    assert b"\\u00e9" not in payload

    added = restored.add_spending(
        amount="1.00",
        category="Misc",
        on_date=date(2026, 11, 1),
        user="Boris",
        recorded_on=date(2026, 11, 1),
    )
    assert added.id == 4


def test_in_memory_path_creates_no_plaintext_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbid_temp_file(*args: object, **kwargs: object) -> None:
        raise AssertionError("in-memory serialization created a temp file")

    monkeypatch.setattr("finance.store.tempfile.mkstemp", forbid_temp_file)
    path = tmp_path / "nested" / "finance_data.json"
    payload = _rich_store(tmp_path / "also-unused.json").to_json_bytes()

    restored = FinanceStore.from_json_bytes(payload, path)

    assert restored.to_json_bytes() == payload
    assert not path.exists()
    assert not path.parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_load_accepts_lf_and_crlf_copies_of_the_same_document(tmp_path: Path) -> None:
    canonical = _sample_store(tmp_path / "unused.json").to_json_bytes()
    lf_path = tmp_path / "lf.json"
    crlf_path = tmp_path / "crlf.json"
    lf_path.write_bytes(canonical)
    crlf_path.write_bytes(canonical.replace(b"\n", b"\r\n"))

    assert FinanceStore.load(lf_path).to_json_bytes() == canonical
    assert FinanceStore.load(crlf_path).to_json_bytes() == canonical


def test_save_writes_canonical_text_with_platform_newlines(tmp_path: Path) -> None:
    path = tmp_path / "finance_data.json"
    store = _sample_store(path)
    canonical = store.to_json_bytes()
    store.save()

    assert path.read_bytes() == canonical.replace(b"\n", os.linesep.encode("ascii"))
    assert FinanceStore.load(path).to_json_bytes() == canonical


def test_empty_document_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "unused.json"
    store = FinanceStore(path)
    payload = store.to_json_bytes()

    assert payload == store.to_json_bytes()
    assert payload.decode("utf-8") == "\n".join(
        [
            "{",
            '  "version": 2,',
            '  "next_id": 1,',
            '  "spending": [],',
            '  "income": []',
            "}",
        ]
    )
    restored = FinanceStore.from_json_bytes(payload, path)
    assert restored.to_json_bytes() == payload
    assert restored.spending == []
    assert restored.income == []
    assert not path.exists()
