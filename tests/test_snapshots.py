"""Encrypted snapshot saves against an in-memory Drive. No network and no finance file."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from finance import default_data_path
from finance.crypto import decrypt, encrypt, read_unverified_metadata
from finance.crypto import SnapshotMetadata
from finance.drive_adapter import (
    NOT_OWNER_SKIPPED,
    GoogleDriveAdapter,
    generation_of_name,
    snapshot_name,
)
from finance.snapshots import (
    CHOICE_CANCEL,
    CHOICE_INSPECT_RECOVER,
    CHOICE_KEEP_AND_RETRY,
    CHOICE_RELOAD_DISCARD,
    CHOICE_RETRY_PASSPHRASE,
    SnapshotError,
    KEEP_DAILY_DAYS,
    KEEP_RECENT_SNAPSHOTS,
    LOCAL_ENCRYPTED_COPIES,
    MIN_KNOWN_VALID,
    OUTCOME_CONFLICT,
    OUTCOME_EMPTY,
    OUTCOME_FAILED,
    OUTCOME_LOADED,
    OUTCOME_REFUSED,
    OUTCOME_SAVED,
    OUTCOME_UNCHANGED,
    OUTCOME_UNREADABLE,
    KnownSnapshot,
    SnapshotRepository,
    retention_keep_ids,
)
from finance.store import FinanceStore
from tests.fake_drive import FakeDrive, http_error
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

FOLDER_ID = "folder-id-0123456789abc"
OTHER_FOLDER = "other-folder-0123456789a"
PASSPHRASE = "synthetic-passphrase-alpha"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@dataclass
class Session:
    fake: FakeDrive
    adapter: GoogleDriveAdapter
    repo: SnapshotRepository
    root: Path
    clock: "_Clock"


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


def _finance_stamp() -> tuple[bool, int | None]:
    path = default_data_path()
    if not path.exists():
        return False, None
    return True, path.stat().st_mtime_ns


def _adapter(fake: FakeDrive) -> GoogleDriveAdapter:
    return GoogleDriveAdapter(
        FOLDER_ID,
        service=fake.service(),
        sleep=lambda _delay: None,
        retry_delays=(),
        list_delays=(),
        list_attempts=1,
    )


def _repo(adapter: GoogleDriveAdapter, root: Path, clock: _Clock) -> SnapshotRepository:
    return SnapshotRepository(
        adapter,
        PASSPHRASE,
        cache_path=root / "known_valid.json",
        local_dir=root / "encrypted",
        memory_path=root / "not-written.json",
        clock=clock,
    )


@pytest.fixture
def session(tmp_path: Path) -> Session:
    before = _finance_stamp()
    fake = FakeDrive(FOLDER_ID)
    adapter = _adapter(fake)
    clock = _Clock()
    bundle = Session(fake, adapter, _repo(adapter, tmp_path, clock), tmp_path, clock)
    yield bundle
    adapter.close()
    assert _finance_stamp() == before
    assert not (tmp_path / "not-written.json").exists()
    _assert_secret_absent(fake, tmp_path)


def _assert_secret_absent(fake: FakeDrive, root: Path) -> None:
    raw = SYNTHETIC_SECRET.encode("utf-8")
    for item in fake.files.values():
        assert raw not in item.content
        assert SYNTHETIC_SECRET not in item.name
    cache = root / "known_valid.json"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
        assert SYNTHETIC_SECRET not in text
        assert PASSPHRASE not in text
    local = root / "encrypted"
    if not local.exists():
        return
    for path in local.iterdir():
        assert path.suffix != ".tmp"
        if not path.is_file():
            continue
        assert raw not in path.read_bytes()


def _store(path: Path) -> FinanceStore:
    store = FinanceStore(path)
    store.add_spending(
        amount="14.20",
        category="Groceries",
        on_date=date(2026, 10, 13),
        user="Boris",
        note=SYNTHETIC_SECRET,
        recorded_on=date(2026, 10, 14),
    )
    store.add_income(
        amount="250.50",
        on_date=date(2026, 10, 4),
        user="Milena",
        note="café invoice",
        recorded_on=date(2026, 10, 5),
    )
    return store


def _grow(store: FinanceStore, number: int) -> None:
    store.add_spending(
        amount=Decimal("1.00") + (Decimal(number) / Decimal("100")),
        category="Misc",
        on_date=date(2026, 10, 1),
        user="Boris" if number % 2 == 0 else "Milena",
        note=f"row-{number}",
        recorded_on=date(2026, 10, 2),
    )


def _quiet(result: object) -> None:
    text = getattr(result, "message", "")
    assert SYNTHETIC_SECRET not in text
    assert PASSPHRASE not in text


def _generations(fake: FakeDrive, *, trashed: bool) -> list[int]:
    found = []
    for item in fake.files.values():
        generation = generation_of_name(item.name)
        if generation is None or item.trashed is not trashed:
            continue
        found.append(generation)
    return sorted(found)


def _by_generation(fake: FakeDrive) -> dict[int, object]:
    found = {}
    for item in fake.files.values():
        generation = generation_of_name(item.name)
        if generation is not None and not item.trashed:
            found[generation] = item
    return found


def _media_ids(fake: FakeDrive) -> list[str]:
    return [call["fileId"] for call in fake.calls if call["method"] == "get_media"]


def _creates(fake: FakeDrive) -> int:
    return sum(1 for call in fake.calls if call["method"] == "create")


def _known(file_id: str, generation: int, created: datetime) -> KnownSnapshot:
    return KnownSnapshot(
        file_id=file_id,
        generation=generation,
        created_at_utc=created,
        drive_created_at=created,
        random_id=f"{generation:012d}",
    )


def test_retention_rule_keeps_recent_daily_and_the_minimum() -> None:
    today = NOW
    same_day = [
        _known(f"id-{generation:02d}", generation, today)
        for generation in range(1, 26)
    ]
    kept = retention_keep_ids(same_day, now=today)
    assert kept == {f"id-{generation:02d}" for generation in range(6, 26)}

    few = [_known(f"few-{index}", index, today) for index in range(1, MIN_KNOWN_VALID)]
    assert retention_keep_ids(few, now=today) == {item.file_id for item in few}

    start = today - timedelta(days=KEEP_DAILY_DAYS - 1)
    outside = today - timedelta(days=KEEP_DAILY_DAYS)
    older = _known("outside", 1, outside)
    edge = _known("edge", 2, start)
    recent = [
        _known(f"recent-{index}", index + 2, today)
        for index in range(KEEP_RECENT_SNAPSHOTS)
    ]
    kept = retention_keep_ids([older, edge, *recent], now=today)
    assert "edge" in kept
    assert "outside" not in kept
    assert all(item.file_id in kept for item in recent)


def test_first_save_reloads_and_skips_an_unchanged_document(session: Session) -> None:
    session.fake.add(
        "notes-file-0123456789ab",
        "notes.txt",
        content=b"leave-this-file",
    )
    session.fake.add(
        "outside-file-0123456789",
        snapshot_name(1, NOW, "cccccccccccc"),
        parents=(OTHER_FOLDER,),
        content=b"outside",
    )
    loaded = session.repo.load_latest()
    assert loaded.outcome == OUTCOME_EMPTY
    assert loaded.store is None

    store = _store(session.root / "memory.json")
    saved = session.repo.save(store)
    _quiet(saved)
    assert saved.outcome == OUTCOME_SAVED
    assert saved.generation == 1
    assert saved.adopted is True
    item = _by_generation(session.fake)[1]
    opened = decrypt(item.content, PASSPHRASE)
    assert opened.plaintext == store.to_json_bytes()
    assert opened.metadata.generation == 1
    assert opened.metadata.spending_count == 1
    assert opened.metadata.income_count == 1
    assert read_unverified_metadata(item.content).generation == generation_of_name(item.name)
    assert SYNTHETIC_SECRET.encode("utf-8") not in item.content

    session.fake.calls.clear()
    again = _repo(session.adapter, session.root, session.clock)
    reloaded = again.load_latest()
    assert reloaded.outcome == OUTCOME_LOADED
    assert reloaded.store is not None
    assert reloaded.store.to_json_bytes() == store.to_json_bytes()
    assert _media_ids(session.fake) == []

    same = again.save(reloaded.store)
    assert same.outcome == OUTCOME_UNCHANGED
    assert _creates(session.fake) == 0
    assert session.fake.files["notes-file-0123456789ab"].content == b"leave-this-file"
    assert session.fake.files["outside-file-0123456789"].content == b"outside"
    assert session.fake.delete_calls == []


def test_twenty_five_same_day_snapshots_keep_the_newest_twenty(session: Session) -> None:
    loaded = session.repo.load_latest()
    assert loaded.outcome == OUTCOME_EMPTY
    store = _store(session.root / "memory.json")
    for number in range(1, 26):
        session.clock.advance(seconds=1)
        _grow(store, number)
        saved = session.repo.save(store)
        assert saved.outcome == OUTCOME_SAVED, saved.message
        assert saved.generation == number
    assert _generations(session.fake, trashed=False) == list(range(6, 26))
    assert _generations(session.fake, trashed=True) == list(range(1, 6))
    local = sorted(path.name for path in (session.root / "encrypted").iterdir())
    assert len(local) == LOCAL_ENCRYPTED_COPIES
    assert [generation_of_name(name) for name in local] == list(range(16, 26))
    for path in (session.root / "encrypted").iterdir():
        assert path.read_bytes().startswith(b"FTEN")
    assert session.fake.delete_calls == []
    unchanged = session.repo.save(store)
    assert unchanged.outcome == OUTCOME_UNCHANGED
    assert _generations(session.fake, trashed=False) == list(range(6, 26))


def test_daily_retention_keeps_the_newest_snapshot_of_each_day(session: Session) -> None:
    today = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
    session.clock.now = today - timedelta(days=KEEP_DAILY_DAYS + 5)
    loaded = session.repo.load_latest()
    assert loaded.outcome == OUTCOME_EMPTY
    store = _store(session.root / "memory.json")
    _grow(store, 1)
    assert session.repo.save(store).generation == 1

    session.clock.now = today - timedelta(days=10)
    _grow(store, 2)
    assert session.repo.save(store).generation == 2
    session.clock.advance(hours=1)
    _grow(store, 3)
    assert session.repo.save(store).generation == 3

    session.clock.now = today
    for number in range(4, 24):
        session.clock.advance(seconds=1)
        _grow(store, number)
        saved = session.repo.save(store)
        assert saved.outcome == OUTCOME_SAVED, saved.message
    assert _generations(session.fake, trashed=False) == [3, *range(4, 24)]
    assert _generations(session.fake, trashed=True) == [1, 2]


def test_other_account_snapshots_are_not_trashed(session: Session, monkeypatch) -> None:
    monkeypatch.setattr("finance.snapshots.KEEP_RECENT_SNAPSHOTS", 3)
    monkeypatch.setattr("finance.snapshots.MIN_KNOWN_VALID", 1)
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    for number in range(1, 4):
        _grow(store, number)
        assert session.repo.save(store).outcome == OUTCOME_SAVED
    oldest = _by_generation(session.fake)[1]
    oldest.can_trash = False
    _grow(store, 4)
    saved = session.repo.save(store)
    assert saved.outcome == OUTCOME_SAVED
    assert any(action.status == NOT_OWNER_SKIPPED for action in saved.prune)
    assert 1 in _generations(session.fake, trashed=False)
    _grow(store, 5)
    session.repo.save(store)
    assert _generations(session.fake, trashed=True) == [2]
    assert 1 in _generations(session.fake, trashed=False)
    oldest.can_trash = True
    _grow(store, 6)
    session.repo.save(store)
    assert 1 in _generations(session.fake, trashed=True)


def test_failed_upload_leaves_the_previous_snapshot(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    _grow(store, 1)
    session.fake.lose_create_response = True
    session.fake.apply_lost_create = False
    failed = session.repo.save(store)
    _quiet(failed)
    assert failed.outcome == OUTCOME_FAILED
    assert _generations(session.fake, trashed=False) == [1]
    retried = session.repo.save(store)
    assert retried.outcome == OUTCOME_SAVED
    assert retried.generation == 2
    opened = session.repo.reload()
    assert opened.store is not None
    assert opened.store.to_json_bytes() == store.to_json_bytes()


def test_corrupt_readback_and_bad_document_leave_previous_snapshots(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    _grow(store, 1)
    _tamper_next_download(session, lambda plaintext, metadata: b"not-an-envelope")
    corrupt = session.repo.save(store)
    _quiet(corrupt)
    assert corrupt.outcome == OUTCOME_FAILED
    assert "could not be decrypted" in corrupt.message
    assert _generations(session.fake, trashed=False) == [1, 2]
    assert _generations(session.fake, trashed=True) == []

    when = NOW + timedelta(minutes=10)
    metadata = SnapshotMetadata(
        generation=3,
        created_at_utc=when,
        spending_count=0,
        income_count=0,
        next_id=1,
    )
    session.fake.add(
        "bad-json-file-012345678",
        snapshot_name(3, when, "abc123abc123"),
        content=encrypt(b"{", PASSPHRASE, metadata),
        created_time=when,
    )
    loaded = session.repo.load_latest()
    _quiet(loaded)
    assert loaded.outcome == OUTCOME_LOADED
    assert loaded.generation == 1
    assert any("not a valid finance document" in item.reason for item in loaded.skipped)
    assert _generations(session.fake, trashed=True) == []
    assert session.fake.delete_calls == []


def test_byte_mismatch_fails_when_the_json_would_still_parse(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    _grow(store, 1)

    def tweak(plaintext: bytes, metadata: SnapshotMetadata) -> bytes:
        changed = plaintext.replace(b'"version": 2', b'"version":  2', 1)
        assert changed != plaintext
        FinanceStore.from_json_bytes(changed, session.root / "parsed.json")
        return encrypt(changed, PASSPHRASE, metadata)

    _tamper_next_download(session, tweak)
    failed = session.repo.save(store)
    _quiet(failed)
    assert failed.outcome == OUTCOME_FAILED
    assert "did not match" in failed.message
    assert _generations(session.fake, trashed=True) == []
    assert not (session.root / "parsed.json").exists()
    session.fake.before_execute = None
    retried = session.repo.save(store)
    assert retried.outcome == OUTCOME_SAVED
    assert retried.generation == 2


def test_unconfirmed_readback_does_not_trash(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    _grow(store, 1)
    session.fake.next_id = "new-file-id-0123456789"
    session.fake.fail_get_ids.add(session.fake.next_id)
    session.fake.get_error = http_error(503, "backendError", "unavailable")
    failed = session.repo.save(store)
    _quiet(failed)
    assert failed.outcome == OUTCOME_FAILED
    assert _generations(session.fake, trashed=False) == [1, 2]
    assert session.fake.delete_calls == []
    opened = session.repo.reload()
    assert opened.outcome == OUTCOME_LOADED
    assert opened.generation == 2
    assert opened.store is not None
    assert opened.store.to_json_bytes() == store.to_json_bytes()


def test_trash_failure_leaves_old_and_new_snapshots(session: Session, monkeypatch) -> None:
    monkeypatch.setattr("finance.snapshots.KEEP_RECENT_SNAPSHOTS", 2)
    monkeypatch.setattr("finance.snapshots.MIN_KNOWN_VALID", 1)
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    for number in range(1, 3):
        _grow(store, number)
        assert session.repo.save(store).outcome == OUTCOME_SAVED

    def before(method: str) -> None:
        if method == "update":
            raise TimeoutError("trash timed out")

    session.fake.before_execute = before
    _grow(store, 3)
    saved = session.repo.save(store)
    _quiet(saved)
    assert saved.outcome == OUTCOME_SAVED
    assert saved.generation == 3
    assert any(action.status == "failed" for action in saved.prune)
    assert "left in place" in saved.message
    assert _generations(session.fake, trashed=False) == [1, 2, 3]
    assert session.fake.delete_calls == []


def test_generation_mismatch_keeps_the_change_and_offers_both_choices(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    remote = FinanceStore(session.root / "remote.json")
    remote.add_income(
        amount="80",
        on_date=date(2026, 10, 2),
        user="Milena",
        note="other-session",
        recorded_on=date(2026, 10, 2),
    )
    when = NOW + timedelta(hours=2)
    metadata = SnapshotMetadata(
        generation=2,
        created_at_utc=when,
        spending_count=0,
        income_count=1,
        next_id=2,
    )
    session.fake.add(
        "remote-file-0123456789ab",
        snapshot_name(2, when, "dddddddddddd"),
        content=encrypt(remote.to_json_bytes(), PASSPHRASE, metadata),
        created_time=when,
    )
    _grow(store, 7)
    created_before = _creates(session.fake)
    refused = session.repo.save(store)
    _quiet(refused)
    assert refused.outcome == OUTCOME_REFUSED
    assert refused.choices == (CHOICE_RELOAD_DISCARD, CHOICE_KEEP_AND_RETRY)
    assert refused.remote_generation == 2
    assert _creates(session.fake) == created_before
    assert any(record.note == "row-7" for record in store.spending)

    again = session.repo.save(store)
    assert again.outcome == OUTCOME_REFUSED
    assert any(record.note == "row-7" for record in store.spending)

    reloaded = session.repo.reload()
    assert reloaded.outcome == OUTCOME_LOADED
    assert reloaded.generation == 2
    assert reloaded.store is not None
    assert reloaded.store.to_json_bytes() == remote.to_json_bytes()
    assert any(record.note == "row-7" for record in store.spending)


def test_same_generation_conflict_keeps_both(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    other = FinanceStore(session.root / "other.json")
    other.add_income(
        amount="15",
        on_date=date(2026, 10, 3),
        user="Boris",
        note="earlier-copy",
        recorded_on=date(2026, 10, 3),
    )
    earlier = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
    metadata = SnapshotMetadata(
        generation=2,
        created_at_utc=earlier,
        spending_count=0,
        income_count=1,
        next_id=2,
    )
    envelope = encrypt(other.to_json_bytes(), PASSPHRASE, metadata)
    creates = {"count": 0}

    def before(method: str) -> None:
        if method != "create":
            return
        creates["count"] += 1
        if creates["count"] != 1:
            return
        session.fake.add(
            "earlier-file-0123456789",
            snapshot_name(2, earlier, "eeeeeeeeeeee"),
            content=envelope,
            created_time=earlier,
        )

    session.fake.before_execute = before
    _grow(store, 4)
    saved = session.repo.save(store)
    _quiet(saved)
    assert saved.outcome == OUTCOME_CONFLICT
    assert saved.adopted is False
    assert saved.current_file_id == "earlier-file-0123456789"
    assert set(saved.conflict_file_ids) == {saved.file_id, "earlier-file-0123456789"}
    assert _generations(session.fake, trashed=False) == [1, 2, 2]
    assert _generations(session.fake, trashed=True) == []
    held = session.repo.save(store)
    assert held.outcome == OUTCOME_CONFLICT
    assert _creates(session.fake) == 2
    reloaded = session.repo.reload()
    assert reloaded.store is not None
    assert reloaded.store.to_json_bytes() == other.to_json_bytes()
    assert len(reloaded.conflict_file_ids) == 2


def test_delayed_listing_still_treats_the_new_id_as_saved(session: Session) -> None:
    session.fake.hide_created = True
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    saved = session.repo.save(store)
    assert saved.outcome == OUTCOME_SAVED
    assert saved.file_id in saved.included_by_get
    assert saved.file_id in session.fake.files


def test_corrupt_newest_falls_back_without_trashing(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    session.fake.add(
        "corrupt-file-012345678",
        snapshot_name(2, NOW + timedelta(minutes=5), "ffffffffffff"),
        content=b"this-is-not-an-envelope",
        created_time=NOW + timedelta(minutes=5),
    )
    loaded = session.repo.load_latest()
    _quiet(loaded)
    assert loaded.outcome == OUTCOME_LOADED
    assert loaded.generation == 1
    assert loaded.store is not None
    assert loaded.store.to_json_bytes() == store.to_json_bytes()
    assert any(item.generation == 2 for item in loaded.skipped)
    assert _generations(session.fake, trashed=False) == [1, 2]
    assert session.fake.delete_calls == []


def test_filename_generation_mismatch_is_invalid(session: Session, monkeypatch) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    when = NOW + timedelta(minutes=5)
    metadata = SnapshotMetadata(
        generation=5,
        created_at_utc=when,
        spending_count=1,
        income_count=1,
        next_id=3,
    )
    session.fake.add(
        "mismatch-file-01234567",
        snapshot_name(2, when, "aabbccddeeff"),
        content=encrypt(store.to_json_bytes(), PASSPHRASE, metadata),
        created_time=when,
    )
    real = decrypt

    def guarded(envelope: bytes, passphrase: str):
        assert read_unverified_metadata(envelope).generation != 5
        return real(envelope, passphrase)

    monkeypatch.setattr("finance.snapshots.decrypt", guarded)
    loaded = session.repo.load_latest()
    assert loaded.outcome == OUTCOME_LOADED
    assert loaded.generation == 1
    assert any("does not match" in item.reason for item in loaded.skipped)
    assert _generations(session.fake, trashed=False) == [1, 2]


def test_unreadable_folder_does_not_create_a_blank_dataset(session: Session) -> None:
    session.fake.add(
        "bad-file-id-0123456789",
        snapshot_name(1, NOW, "1234567890ab"),
        content=b"not-a-snapshot",
    )
    loaded = session.repo.load_latest()
    _quiet(loaded)
    assert loaded.outcome == OUTCOME_UNREADABLE
    assert loaded.store is None
    assert loaded.choices == (
        CHOICE_RETRY_PASSPHRASE,
        CHOICE_INSPECT_RECOVER,
        CHOICE_CANCEL,
    )
    assert _creates(session.fake) == 0
    assert _generations(session.fake, trashed=False) == [1]


def test_wrong_passphrase_does_not_create_a_blank_dataset(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    wrong = SnapshotRepository(
        session.adapter,
        "synthetic-passphrase-beta",
        cache_path=session.root / "known_valid.json",
        local_dir=session.root / "encrypted",
        memory_path=session.root / "not-written.json",
        clock=session.clock,
    )
    loaded = wrong.load_latest()
    _quiet(loaded)
    assert loaded.outcome == OUTCOME_UNREADABLE
    assert loaded.store is None
    assert CHOICE_RETRY_PASSPHRASE in loaded.choices
    assert _generations(session.fake, trashed=False) == [1]
    opened = session.repo.reload()
    assert opened.outcome == OUTCOME_LOADED
    assert opened.store is not None
    assert opened.store.to_json_bytes() == store.to_json_bytes()


def test_cache_skips_download_until_the_checksum_changes(session: Session) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    _grow(store, 1)
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    _grow(store, 2)
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    session.fake.calls.clear()
    described = session.repo.describe()
    assert [item.generation for item in described] == [2, 1]
    assert all(item.known_valid for item in described)
    assert _media_ids(session.fake) == []

    oldest = _by_generation(session.fake)[1]
    untouched = _by_generation(session.fake)[2].file_id
    oldest.content = b"replaced-snapshot-bytes"
    session.fake.calls.clear()
    _grow(store, 3)
    saved = session.repo.save(store)
    assert saved.outcome == OUTCOME_SAVED
    media = _media_ids(session.fake)
    assert oldest.file_id in media
    assert untouched not in media
    assert oldest.trashed is False


def test_prune_does_not_decrypt_uncached_snapshots_below_the_minimum(
    session: Session, monkeypatch
) -> None:
    when = NOW - timedelta(days=1)
    for generation in (1, 2):
        metadata = SnapshotMetadata(
            generation=generation,
            created_at_utc=when,
            spending_count=0,
            income_count=0,
            next_id=1,
        )
        document = FinanceStore(session.root / "empty.json").to_json_bytes()
        session.fake.add(
            f"old-file-{generation}-abcdefghij",
            snapshot_name(generation, when, f"{generation:012x}"),
            content=encrypt(document, PASSPHRASE, metadata),
            created_time=when,
        )
    calls = {"decrypt": 0}
    real = decrypt

    def counting(envelope: bytes, passphrase: str):
        calls["decrypt"] += 1
        return real(envelope, passphrase)

    monkeypatch.setattr("finance.snapshots.decrypt", counting)
    loaded = session.repo.load_latest()
    assert loaded.outcome == OUTCOME_LOADED
    assert loaded.generation == 2
    assert calls["decrypt"] == 1
    session.fake.calls.clear()
    calls["decrypt"] = 0
    store = loaded.store
    assert store is not None
    _grow(store, 9)
    saved = session.repo.save(store)
    assert saved.outcome == OUTCOME_SAVED
    assert saved.prune == ()
    assert calls["decrypt"] == 1
    media = set(_media_ids(session.fake))
    assert "old-file-1-abcdefghij" not in media
    assert _generations(session.fake, trashed=False) == [1, 2, 3]


def test_catalog_reads_headers_without_decrypting(session: Session, monkeypatch) -> None:
    session.repo.load_latest()
    store = _store(session.root / "memory.json")
    assert session.repo.save(store).outcome == OUTCOME_SAVED
    when = NOW + timedelta(minutes=3)
    metadata = SnapshotMetadata(
        generation=4,
        created_at_utc=when,
        spending_count=0,
        income_count=0,
        next_id=1,
    )
    session.fake.add(
        "header-only-0123456789",
        snapshot_name(4, when, "001122334455"),
        content=encrypt(FinanceStore(session.root / "empty.json").to_json_bytes(), PASSPHRASE, metadata),
        created_time=when,
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("decrypt")

    monkeypatch.setattr("finance.snapshots.decrypt", forbidden)
    described = session.repo.describe()
    by_generation = {item.generation: item for item in described}
    assert by_generation[1].known_valid is True
    assert by_generation[4].known_valid is False
    assert by_generation[4].invalid is False
    assert by_generation[4].spending_count == 0
    assert by_generation[4].next_id == 1


def test_empty_passphrase_is_refused(session: Session) -> None:
    with pytest.raises(SnapshotError) as caught:
        SnapshotRepository(session.adapter, "")
    assert_failure_omits_payload(caught.value, SYNTHETIC_SECRET)
    assert PASSPHRASE not in str(caught.value)


def _tamper_next_download(session: Session, replace) -> None:
    state = {"done": False}

    def before(method: str) -> None:
        if method != "get_media" or state["done"]:
            return
        state["done"] = True
        current = max(
            (
                item
                for item in session.fake.files.values()
                if generation_of_name(item.name) is not None and not item.trashed
            ),
            key=lambda item: item.created_time,
        )
        if current.content.startswith(b"FTEN"):
            opened = decrypt(current.content, PASSPHRASE)
            current.content = replace(opened.plaintext, opened.metadata)
        else:
            current.content = replace(b"", SnapshotMetadata(1, NOW, 0, 0, 1))

    session.fake.before_execute = before
