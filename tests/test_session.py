"""Cloud session, migration, and the legacy-file block. No network and no real finance file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import cli
from finance import default_data_path
from finance.crypto import SnapshotMetadata, decrypt, encrypt
from finance.drive_adapter import LOCK_FILE_NAME, GoogleDriveAdapter, generation_of_name, snapshot_name
from finance.lock import (
    CHOICE_CREATE_LOCK,
    MODE_EDITABLE as LOCK_EDITABLE,
    LockDocument,
    ProcessLock,
    blank_lock,
    decode_lock,
    encode_lock,
)
from finance.profile import encrypted_snapshots_dir, load_local_config, process_lock_path
from finance.session import (
    CHOICE_CREATE_EMPTY,
    CHOICE_DISCARD,
    CHOICE_IMPORT,
    CHOICE_RESTORE_LOCAL,
    MODE_ACCESS_LOST,
    MODE_CLOSED,
    MODE_EDITABLE,
    MODE_OFFLINE,
    MODE_PAUSED,
    MODE_READ_ONLY,
    MODE_SETUP,
    FinanceSession,
    SessionError,
    SessionStatus,
)
from finance.snapshots import (
    SOURCE_DRIVE,
    SOURCE_LOCAL,
    LocalEncryptedCopies,
    summary_text,
)
from finance.store import FinanceStore, LocalSaveRefused
from tests.fake_drive import FakeDrive, http_error
from tests.support import SYNTHETIC_SECRET

FOLDER_ID = "folder-id-0123456789abc"
LOCK_ID = "lock-id-0123456789abcd"
PASSPHRASE = "synthetic-passphrase-alpha"
NOW = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _http_date(when: datetime) -> str:
    when = when.astimezone(timezone.utc)
    return (
        f"{_WEEKDAYS[when.weekday()]}, {when.day:02d} {_MONTHS[when.month - 1]} "
        f"{when.year:04d} {when:%H:%M:%S} GMT"
    )


def _finance_stamp() -> tuple[bool, int | None]:
    path = default_data_path()
    if not path.exists():
        return False, None
    return True, path.stat().st_mtime_ns


def _same(left: bytes, right: bytes) -> None:
    if left != right:
        raise AssertionError(f"byte strings differ ({len(left)} vs {len(right)})")


def _quiet(status: SessionStatus) -> None:
    text = repr(status) + status.message + status.legacy_notice
    assert SYNTHETIC_SECRET not in text
    assert PASSPHRASE not in text


@dataclass
class World:
    fake: FakeDrive
    adapter: GoogleDriveAdapter
    session: FinanceSession
    root: Path
    memory: Path
    legacy: Path
    stamp: tuple[bool, int | None]


def _adapter(fake: FakeDrive) -> GoogleDriveAdapter:
    return GoogleDriveAdapter(
        FOLDER_ID,
        service=fake.service(),
        sleep=lambda _delay: None,
        retry_delays=(),
        list_delays=(),
        list_attempts=1,
    )


def _session(adapter: GoogleDriveAdapter, root: Path) -> FinanceSession:
    return FinanceSession(
        adapter,
        folder_id=FOLDER_ID,
        owner_label="Boris",
        legacy_path=root / "legacy.json",
        memory_path=root / "not-written.json",
        sleep=lambda _delay: None,
    )


def _records(root: Path, amount: str) -> FinanceStore:
    store = FinanceStore(root / "builder-not-saved.json")
    store.add_income(
        amount=amount,
        on_date=date(2026, 10, 4),
        user="Milena",
        note=SYNTHETIC_SECRET,
        recorded_on=date(2026, 10, 5),
    )
    return store


def _envelope(store: FinanceStore, generation: int) -> bytes:
    metadata = SnapshotMetadata(
        generation=generation,
        created_at_utc=NOW,
        spending_count=len(store.spending),
        income_count=len(store.income),
        next_id=store._next_id,
    )
    return encrypt(store.to_json_bytes(), PASSPHRASE, metadata)


def _plant(fake: FakeDrive, store: FinanceStore, generation: int, file_id: str) -> None:
    fake.add(
        file_id,
        snapshot_name(generation, NOW, f"{generation:012x}"),
        content=_envelope(store, generation),
        created_time=NOW + timedelta(seconds=generation),
    )


def _free_lock(fake: FakeDrive) -> None:
    fake.add(LOCK_ID, LOCK_FILE_NAME, content=encode_lock(blank_lock()), created_time=NOW)


def _held_lock(fake: FakeDrive) -> None:
    document = LockDocument(
        format_version=1,
        state="held",
        session_token="ab" * 32,
        owner_label="Milena",
        machine_id="cd" * 32,
        machine_label="macOS",
        acquired_at_utc=NOW,
        heartbeat_at_utc=NOW,
    )
    fake.add(LOCK_ID, LOCK_FILE_NAME, content=encode_lock(document), created_time=NOW)


def _snapshots(fake: FakeDrive) -> list:
    return [
        item
        for item in fake.files.values()
        if generation_of_name(item.name) is not None and not item.trashed
    ]


def _locks(fake: FakeDrive) -> list:
    return [item for item in fake.files.values() if item.name == LOCK_FILE_NAME and not item.trashed]


def _assert_secret_hidden(fake: FakeDrive) -> None:
    raw = SYNTHETIC_SECRET.encode("utf-8")
    for item in fake.files.values():
        assert SYNTHETIC_SECRET not in item.name
        assert PASSPHRASE not in item.name
        if item.name.endswith(".enc"):
            assert raw not in item.content


@pytest.fixture
def world(tmp_path: Path):
    fake = FakeDrive(FOLDER_ID)
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    bundle = World(
        fake,
        adapter,
        _session(adapter, tmp_path),
        tmp_path,
        tmp_path / "not-written.json",
        tmp_path / "legacy.json",
        _finance_stamp(),
    )
    try:
        yield bundle
    finally:
        bundle.session.close(discard=True)
        bundle.adapter.close()
        assert not bundle.memory.exists()
        assert _finance_stamp() == bundle.stamp
        _assert_secret_hidden(fake)


def test_open_edits_and_does_not_write_the_legacy_file(world: World, monkeypatch) -> None:
    source = _records(world.root, "20.00")
    original = source.to_json_bytes()
    world.legacy.write_bytes(original)
    _plant(world.fake, source, 1, "snap-0001-abcdefghij")
    _free_lock(world.fake)
    read_bytes = Path.read_bytes

    def guard(self: Path, *args, **kwargs):
        if self.resolve() == world.legacy.resolve():
            raise AssertionError("legacy file was read")
        return read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guard)
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.mode == MODE_EDITABLE
    assert status.can_edit is True
    assert status.generation == 1
    assert "was not imported" in status.legacy_notice
    assert load_local_config().cloud_mode is True
    assert world.session.store is not None
    assert world.session.store.income[0].amount == Decimal("20")

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    world.session.store.add_income(
        amount="3.00",
        on_date=date(2026, 10, 6),
        user="Boris",
        note="second",
    )
    saved = world.session.save()
    _quiet(saved)
    assert saved.mode == MODE_EDITABLE
    assert saved.can_edit is True
    assert saved.dirty is False
    assert saved.generation == 2
    assert len(_snapshots(world.fake)) == 2
    _same(world.legacy.read_bytes(), original)

    other = FinanceStore(world.root / "blocked.json")
    with pytest.raises(LocalSaveRefused, match="was not written"):
        other.save()
    assert not (world.root / "blocked.json").exists()


def test_wrong_passphrase_does_not_take_the_lock(world: World) -> None:
    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(world.fake)
    before = _locks(world.fake)[0].content

    refused = world.session.open_with_passphrase("not-the-passphrase")
    _quiet(refused)
    assert refused.mode == MODE_SETUP
    assert refused.can_edit is False
    assert _locks(world.fake)[0].content == before
    assert not any(call["method"] == "update" for call in world.fake.calls)
    probe = ProcessLock(process_lock_path())
    assert probe.try_acquire() is True
    probe.release()
    assert load_local_config().cloud_mode is False

    opened = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(opened)
    assert opened.mode == MODE_EDITABLE
    assert opened.can_edit is True
    assert load_local_config().cloud_mode is True


def test_no_access_to_the_folder(world: World) -> None:
    world.fake.get_error = http_error(403, "forbidden", "no")
    world.fake.fail_get_ids.add(FOLDER_ID)
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.mode == MODE_ACCESS_LOST
    assert status.can_edit is False
    assert "cannot open the saved Drive folder" in status.message
    assert load_local_config().cloud_mode is False
    assert not any(call["method"] in {"create", "update"} for call in world.fake.calls)


def test_corrupt_newest_snapshot_falls_back(world: World) -> None:
    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    world.fake.add(
        "snap-0002-abcdefghij",
        snapshot_name(2, NOW, "000000000002"),
        content=b"not-an-envelope",
        created_time=NOW + timedelta(seconds=5),
    )
    _free_lock(world.fake)
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.mode == MODE_EDITABLE
    assert status.generation == 1
    assert "Skipped snapshots" in status.message
    assert world.session.store is not None
    assert world.session.store.income[0].amount == Decimal("20")
    assert world.fake.files["snap-0002-abcdefghij"].trashed is False
    assert world.fake.delete_calls == []


def test_no_valid_snapshot_does_not_create_an_empty_dataset(world: World) -> None:
    world.fake.add(
        "snap-0001-abcdefghij",
        snapshot_name(1, NOW, "000000000001"),
        content=b"not-an-envelope",
        created_time=NOW,
    )
    _free_lock(world.fake)
    before = _locks(world.fake)[0].content
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.mode == MODE_SETUP
    assert status.can_edit is False
    assert "none of them could be opened" in status.message
    assert "empty dataset was not created" in status.message
    assert _locks(world.fake)[0].content == before
    assert len(_snapshots(world.fake)) == 1
    assert load_local_config().cloud_mode is False


def test_lock_already_held_is_read_only(world: World) -> None:
    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    _held_lock(world.fake)
    before = _locks(world.fake)[0].content
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.mode == MODE_READ_ONLY
    assert status.can_edit is False
    assert "Milena" in status.message
    assert world.session.store is not None
    with pytest.raises(SessionError, match="Editing is disabled"):
        world.session.store.add_income(
            amount="1.00",
            on_date=date(2026, 10, 8),
            user="Boris",
        )
    refused = world.session.save()
    assert refused.can_edit is False
    assert "Nothing was saved" in refused.message
    assert len(_snapshots(world.fake)) == 1
    assert _locks(world.fake)[0].content == before


def test_missing_lock_is_created_on_confirmation(world: World) -> None:
    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.can_edit is False
    assert CHOICE_CREATE_LOCK in status.choices
    assert load_local_config().cloud_mode is False

    created = world.session.create_missing_lock()
    _quiet(created)
    assert created.mode == MODE_EDITABLE
    assert created.can_edit is True
    assert len(_locks(world.fake)) == 1
    assert load_local_config().cloud_mode is True
    assert load_local_config().lock_file_id == _locks(world.fake)[0].file_id


def test_generation_changed_between_load_and_lock(world: World) -> None:
    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(world.fake)
    added = {"done": False}

    def before(method: str) -> None:
        if method == "update" and not added["done"]:
            added["done"] = True
            _plant(world.fake, _records(world.root, "10.00"), 2, "snap-0002-abcdefghij")

    world.fake.before_execute = before
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert added["done"] is True
    assert status.reloaded is True
    assert status.generation == 2
    assert status.mode == MODE_EDITABLE
    assert "updated from the other computer" in status.message
    assert world.session.store is not None
    assert world.session.store.income[0].amount == Decimal("10")


def test_newer_copy_survives_older_files_with_higher_generations(world: World) -> None:
    directory = encrypted_snapshots_dir()
    directory.mkdir(parents=True, exist_ok=True)
    other = _records(world.root, "1.00")
    other_metadata = SnapshotMetadata(
        generation=1,
        created_at_utc=NOW - timedelta(days=30),
        spending_count=0,
        income_count=1,
        next_id=other._next_id,
    )
    for generation in range(13, 23):
        metadata = SnapshotMetadata(
            generation=generation,
            created_at_utc=other_metadata.created_at_utc,
            spending_count=0,
            income_count=1,
            next_id=other._next_id,
        )
        name = snapshot_name(generation, NOW - timedelta(days=30), f"{generation:012x}")
        path = directory / name
        path.write_bytes(encrypt(other.to_json_bytes(), "other-passphrase", metadata))
        old_stamp = (NOW - timedelta(days=30)).timestamp()
        os.utime(path, (old_stamp, old_stamp))

    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(world.fake)
    opened = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(opened)
    assert opened.mode == MODE_EDITABLE
    names = [path.name for path in directory.iterdir() if path.is_file()]
    assert any(generation_of_name(name) == 1 for name in names)
    assert len(names) <= 10

    def down(_method: str) -> None:
        raise TimeoutError("down")

    world.fake.before_execute = down
    second = _session(world.adapter, world.root)
    try:
        offline = second.open_with_passphrase(PASSPHRASE)
        _quiet(offline)
        assert offline.mode == MODE_OFFLINE
        assert offline.generation == 1
        assert second.store is not None
        assert second.store.income[0].amount == Decimal("20")
        assert PASSPHRASE not in offline.message
    finally:
        second.close()


def test_offline_plaintext_copy_is_not_called_a_wrong_passphrase(world: World) -> None:
    directory = encrypted_snapshots_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = snapshot_name(4, NOW - timedelta(days=1), "abcdefabcdef")
    (directory / name).write_bytes(b"{" + b"not-a-snapshot" * 8)

    def down(_method: str) -> None:
        raise TimeoutError("down")

    world.fake.before_execute = down
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.mode == MODE_ACCESS_LOST
    assert "not encrypted snapshots" in status.message
    assert "passphrase" not in status.message.lower()
    assert world.session.store is None


def test_drive_unreachable_uses_the_local_copy_read_only(world: World) -> None:
    def down(_method: str) -> None:
        raise TimeoutError("down")

    world.fake.before_execute = down
    missing = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(missing)
    assert missing.mode == MODE_ACCESS_LOST
    assert missing.can_edit is False
    assert "no local encrypted copy" in missing.message

    world.fake.before_execute = None
    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(world.fake)
    opened = world.session.open_with_passphrase(PASSPHRASE)
    assert opened.mode == MODE_EDITABLE

    world.fake.before_execute = down
    second = _session(world.adapter, world.root)
    try:
        offline = second.open_with_passphrase(PASSPHRASE)
        _quiet(offline)
        assert offline.mode == MODE_OFFLINE
        assert offline.can_edit is False
        assert offline.generation == 1
        assert "Offline" in offline.message
        assert "generation 1" in offline.message
        assert offline.data_time
        assert second.store is not None
        assert second.store.income[0].amount == Decimal("20")
        with pytest.raises(SessionError, match="Editing is disabled"):
            second.store.add_income(
                amount="1.00",
                on_date=date(2026, 10, 9),
                user="Boris",
            )
        refused = second.save()
        assert refused.can_edit is False
        assert len(_snapshots(world.fake)) == 1
    finally:
        second.close()


def test_idle_pause_and_resume_reloads_a_newer_generation(world: World) -> None:
    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(world.fake)
    opened = world.session.open_with_passphrase(PASSPHRASE)
    assert opened.mode == MODE_EDITABLE

    world.fake.date_header = _http_date(NOW + timedelta(minutes=11))
    paused = world.session.poll()
    _quiet(paused)
    assert paused.mode == MODE_PAUSED
    assert paused.can_edit is False
    assert decode_lock(_locks(world.fake)[0].content).state == "free"
    with pytest.raises(SessionError, match="Editing is disabled"):
        assert world.session.store is not None
        world.session.store.add_income(
            amount="1.00",
            on_date=date(2026, 10, 9),
            user="Boris",
        )

    _plant(world.fake, _records(world.root, "10.00"), 2, "snap-0002-abcdefghij")
    world.fake.date_header = _http_date(NOW + timedelta(minutes=12))
    resumed = world.session.resume()
    _quiet(resumed)
    assert resumed.mode == MODE_EDITABLE
    assert resumed.can_edit is True
    assert resumed.reloaded is True
    assert resumed.generation == 2
    assert "updated from the other computer" in resumed.message
    assert world.session.store is not None
    assert world.session.store.income[0].amount == Decimal("10")
    assert decode_lock(_locks(world.fake)[0].content).state == "held"


def test_shutdown_keeps_a_dirty_session_and_releases_a_clean_one(world: World) -> None:
    _plant(world.fake, _records(world.root, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(world.fake)
    assert world.session.open_with_passphrase(PASSPHRASE).mode == MODE_EDITABLE
    assert world.session.store is not None
    world.session.store.add_income(
        amount="4.00",
        on_date=date(2026, 10, 7),
        user="Boris",
    )
    reloaded = world.session.reload_discard()
    _quiet(reloaded)
    assert reloaded.mode == MODE_EDITABLE
    assert reloaded.reloaded is True
    assert world.session.store is not None
    assert len(world.session.store.income) == 1
    assert decode_lock(_locks(world.fake)[0].content).state == "held"
    assert len(_snapshots(world.fake)) == 1

    world.session.store.add_income(
        amount="4.00",
        on_date=date(2026, 10, 7),
        user="Boris",
    )
    dirty = world.session.close()
    _quiet(dirty)
    assert dirty.mode == MODE_EDITABLE
    assert CHOICE_DISCARD in dirty.choices
    assert "was not released" in dirty.message
    assert decode_lock(_locks(world.fake)[0].content).state == "held"
    assert world.session.dirty is True

    closed = world.session.close(discard=True)
    _quiet(closed)
    assert closed.mode == MODE_CLOSED
    assert closed.can_edit is False
    assert decode_lock(_locks(world.fake)[0].content).state == "free"
    assert len(_snapshots(world.fake)) == 1
    assert world.session.store is not None
    assert len(world.session.store.income) == 1


def test_explicit_empty_dataset_and_a_mismatched_passphrase(world: World) -> None:
    preview = world.session.open_with_passphrase("not-checked-yet")
    _quiet(preview)
    assert preview.mode == MODE_SETUP
    assert preview.can_edit is False
    assert CHOICE_CREATE_EMPTY in preview.choices
    assert CHOICE_IMPORT not in preview.choices
    assert "probably wrong" in preview.message
    assert "password manager" in preview.message
    assert _snapshots(world.fake) == []
    assert _locks(world.fake) == []

    mismatched = world.session.create_empty("alpha", "beta")
    _quiet(mismatched)
    assert "did not match" in mismatched.message
    assert mismatched.can_edit is False
    assert _snapshots(world.fake) == []
    assert load_local_config().cloud_mode is False

    created = world.session.create_empty(PASSPHRASE, PASSPHRASE)
    _quiet(created)
    assert created.mode == MODE_EDITABLE
    assert created.generation == 1
    assert load_local_config().cloud_mode is True
    assert len(_locks(world.fake)) == 1
    snapshots = _snapshots(world.fake)
    assert len(snapshots) == 1
    opened = decrypt(snapshots[0].content, PASSPHRASE)
    parsed = FinanceStore.from_json_bytes(opened.plaintext, world.root / "parsed-not-saved.json")
    assert parsed.spending == []
    assert parsed.income == []


def test_migration_failure_retries_the_same_lock_file(world: World, capsys) -> None:
    source = _records(world.root, "14.20")
    original = source.to_json_bytes()
    world.legacy.write_bytes(original)
    backup_dir = world.root / "backups"

    mismatched = world.session.import_legacy("alpha", "beta", backup_dir)
    _quiet(mismatched)
    assert "did not match" in mismatched.message
    assert not backup_dir.exists()
    _same(world.legacy.read_bytes(), original)
    assert _snapshots(world.fake) == []
    assert load_local_config().cloud_mode is False

    state = {"creates": 0}

    def fail_second_create(method: str) -> None:
        if method != "create":
            return
        state["creates"] += 1
        if state["creates"] == 2:
            world.fake.lose_create_response = True
            world.fake.apply_lost_create = False

    world.fake.before_execute = fail_second_create
    failed = world.session.import_legacy(PASSPHRASE, PASSPHRASE, backup_dir)
    _quiet(failed)
    assert failed.can_edit is True
    assert failed.dirty is True
    assert load_local_config().cloud_mode is False
    assert _snapshots(world.fake) == []
    assert len(_locks(world.fake)) == 1
    lock_id = _locks(world.fake)[0].file_id
    _same(world.legacy.read_bytes(), original)
    backups = list(backup_dir.iterdir())
    assert len(backups) == 1
    _same(backups[0].read_bytes(), original)
    assert SYNTHETIC_SECRET not in failed.message

    world.fake.before_execute = None
    saved = world.session.import_legacy(PASSPHRASE, PASSPHRASE, backup_dir)
    _quiet(saved)
    assert saved.mode == MODE_EDITABLE
    assert saved.generation == 1
    assert saved.dirty is False
    assert "not changed" in saved.message
    assert load_local_config().cloud_mode is True
    assert len(_locks(world.fake)) == 1
    assert _locks(world.fake)[0].file_id == lock_id
    snapshots = _snapshots(world.fake)
    assert len(snapshots) == 1
    opened = decrypt(snapshots[0].content, PASSPHRASE)
    _same(opened.plaintext, original)
    assert opened.metadata.generation == 1
    _same(world.legacy.read_bytes(), original)
    assert len(list(backup_dir.iterdir())) == 2
    assert cli.main(["summary", "2026", "10"]) == 1
    captured = capsys.readouterr()
    assert "does not support synced data yet" in captured.err
    assert SYNTHETIC_SECRET not in captured.err
    assert _finance_stamp() == world.stamp


def test_setup_shows_record_counts_and_not_notes(world: World) -> None:
    source = _records(world.root, "14.20")
    original = source.to_json_bytes()
    world.legacy.write_bytes(original)
    status = world.session.open_with_passphrase("anything")
    _quiet(status)
    assert status.mode == MODE_SETUP
    assert "0 spending records and 1 income records" in status.message
    assert CHOICE_IMPORT in status.choices
    assert CHOICE_CREATE_EMPTY not in status.choices
    refused = world.session.create_empty(PASSPHRASE, PASSPHRASE)
    _quiet(refused)
    assert "was not replaced" in refused.message
    _same(world.legacy.read_bytes(), original)
    assert _snapshots(world.fake) == []
    assert load_local_config().cloud_mode is False

    world.legacy.write_bytes(b"not-json " + SYNTHETIC_SECRET.encode("utf-8"))
    again = world.session.open_with_passphrase("anything")
    _quiet(again)
    assert "could not be read" in again.message
    assert CHOICE_IMPORT not in again.choices
    assert load_local_config().cloud_mode is False


def test_lock_mode_constant_matches_the_lock_module() -> None:
    assert MODE_EDITABLE == LOCK_EDITABLE


def _sealed(store: FinanceStore, generation: int, when: datetime) -> bytes:
    metadata = SnapshotMetadata(
        generation=generation,
        created_at_utc=when,
        spending_count=len(store.spending),
        income_count=len(store.income),
        next_id=store._next_id,
    )
    return encrypt(store.to_json_bytes(), PASSPHRASE, metadata)


def _spending(root: Path, note: str, amount: str = "3.00") -> FinanceStore:
    store = FinanceStore(root / "builder-not-saved.json")
    store.add_spending(
        amount=amount,
        category="Misc",
        on_date=date(2026, 10, 2),
        user="Boris",
        note=note,
        recorded_on=date(2026, 10, 3),
    )
    return store


def test_backup_list_is_newest_first_and_does_not_decrypt(world: World, monkeypatch) -> None:
    early = _records(world.root, "11.00")
    late = _spending(world.root, "late-conflict-note")
    local_store = _spending(world.root, "local-row-note", "4.00")
    local_store.add_spending(
        amount="5.00",
        category="Misc",
        on_date=date(2026, 10, 3),
        user="Milena",
        note="local-row-two",
        recorded_on=date(2026, 10, 3),
    )
    early_when = NOW
    late_when = NOW + timedelta(seconds=30)
    local_when = NOW + timedelta(hours=1)
    old_when = NOW - timedelta(days=1)
    early_id = "snap-gen2-earlyaaaa"
    late_id = "snap-gen2-lateaaaaa"
    world.fake.add(
        "snap-gen1-olderaaaa",
        snapshot_name(1, old_when, "444444444444"),
        content=_sealed(early, 1, old_when),
        created_time=old_when,
    )
    world.fake.add(
        early_id,
        snapshot_name(2, early_when, "111111111111"),
        content=_sealed(early, 2, early_when),
        created_time=early_when,
    )
    world.fake.add(
        late_id,
        snapshot_name(2, late_when, "222222222222"),
        content=_sealed(late, 2, late_when),
        created_time=late_when,
    )
    world.fake.add(
        "snap-gen3-badaaaaaa",
        snapshot_name(3, NOW, "333333333333"),
        content=b"not-a-snapshot",
        created_time=NOW + timedelta(minutes=5),
    )
    local_name = snapshot_name(4, local_when, "555555555555")
    LocalEncryptedCopies(encrypted_snapshots_dir()).write(
        local_name, _sealed(local_store, 4, local_when)
    )
    _free_lock(world.fake)
    opened = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(opened)
    assert opened.generation == 2
    import finance.snapshots as snapshot_module

    calls = {"decrypt": 0}
    real_decrypt = snapshot_module.decrypt

    def counting(envelope: bytes, passphrase: str):
        calls["decrypt"] += 1
        return real_decrypt(envelope, passphrase)

    monkeypatch.setattr(snapshot_module, "decrypt", counting)
    world.fake.calls.clear()
    catalog = world.session.list_backups()
    text = summary_text(catalog)
    _quiet(opened)
    assert SYNTHETIC_SECRET not in text
    assert "late-conflict-note" not in text
    assert "local-row-note" not in text
    assert PASSPHRASE not in text
    assert calls["decrypt"] == 0
    assert [item.generation for item in catalog.valid] == [4, 2, 2, 1]
    assert [item.generation for item in catalog.unusable] == [3]
    assert catalog.valid[0].source == SOURCE_LOCAL
    assert catalog.valid[0].saved_by == "This computer"
    assert catalog.valid[0].spending_count == 2
    late_row = next(item for item in catalog.valid if item.key == f"{SOURCE_DRIVE}:{late_id}")
    early_row = next(item for item in catalog.valid if item.key == f"{SOURCE_DRIVE}:{early_id}")
    assert late_row.spending_count == 1
    assert late_row.known_valid is False
    assert late_row.conflict is True
    assert early_row.conflict is True
    assert early_row.current is True
    assert early_row.saved_by == "Account A"
    assert catalog.valid[-1].conflict is False
    assert "envelope" in catalog.unusable[0].reason
    media = [call["fileId"] for call in world.fake.calls if call["method"] == "get_media"]
    assert early_id not in media
    assert late_id in media
    refused = world.session.restore_backup(catalog.unusable[0].key)
    _quiet(refused)
    assert "cannot be restored" in refused.message
    assert calls["decrypt"] == 0
    assert len(_snapshots(world.fake)) == 4


def test_restore_refuses_while_dirty_and_without_the_lock(world: World) -> None:
    first = _records(world.root, "11.00")
    second = _records(world.root, "22.00")
    _plant(world.fake, first, 1, "snap-0001-abcdefghij")
    _plant(world.fake, second, 2, "snap-0002-abcdefghij")
    _free_lock(world.fake)
    assert world.session.open_with_passphrase(PASSPHRASE).generation == 2
    catalog = world.session.list_backups()
    older = next(item for item in catalog.valid if item.generation == 1)
    world.session.store.add_income(
        amount="3.00",
        on_date=date(2026, 10, 6),
        user="Boris",
        note="still-here",
    )
    before = {item.file_id: bytes(item.content) for item in _snapshots(world.fake)}
    refused = world.session.restore_backup(older.key)
    _quiet(refused)
    assert "Unsaved" in refused.message
    assert world.session.dirty is True
    assert len(world.session.store.income) == 2
    assert {item.file_id: bytes(item.content) for item in _snapshots(world.fake)} == before


def test_restore_needs_the_editing_lock(world: World) -> None:
    first = _records(world.root, "11.00")
    _plant(world.fake, first, 1, "snap-0001-abcdefghij")
    _held_lock(world.fake)
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.mode == MODE_READ_ONLY
    catalog = world.session.list_backups()
    blocked = world.session.restore_backup(catalog.valid[0].key)
    _quiet(blocked)
    assert "editing lock" in blocked.message
    assert blocked.can_edit is False
    assert len(_snapshots(world.fake)) == 1


def test_restore_older_version_and_a_local_copy(world: World) -> None:
    first = _records(world.root, "11.00")
    second = _records(world.root, "22.00")
    _plant(world.fake, first, 1, "snap-0001-abcdefghij")
    _plant(world.fake, second, 2, "snap-0002-abcdefghij")
    _free_lock(world.fake)
    assert world.session.open_with_passphrase(PASSPHRASE).generation == 2
    older = next(
        item for item in world.session.list_backups().valid if item.generation == 1
    )
    previous = {
        item.file_id: bytes(item.content) for item in _snapshots(world.fake)
    }
    restored = world.session.restore_backup(older.key)
    _quiet(restored)
    assert restored.mode == MODE_EDITABLE
    assert restored.generation == 3
    assert restored.dirty is False
    assert world.session.store is not None
    assert world.session.store.income[0].amount == Decimal("11")
    for file_id, content in previous.items():
        assert world.fake.files[file_id].content == content
        assert world.fake.files[file_id].trashed is False
    assert len(_snapshots(world.fake)) == 3
    newest = max(_snapshots(world.fake), key=lambda item: generation_of_name(item.name) or 0)
    opened = decrypt(newest.content, PASSPHRASE)
    _same(opened.plaintext, first.to_json_bytes())
    assert opened.metadata.generation == 3

    local_name = snapshot_name(8, NOW + timedelta(minutes=2), "999999999999")
    LocalEncryptedCopies(encrypted_snapshots_dir()).write(
        local_name, _sealed(second, 8, NOW + timedelta(minutes=2))
    )
    local = next(
        item
        for item in world.session.list_backups().valid
        if item.source == SOURCE_LOCAL and item.generation == 8
    )
    again = world.session.restore_backup(local.key)
    _quiet(again)
    assert again.generation == 4
    assert again.dirty is False
    assert world.session.store.income[0].amount == Decimal("22")
    assert world.fake.files["snap-0001-abcdefghij"].trashed is False
    assert world.fake.files["snap-0002-abcdefghij"].trashed is False
    assert len(_snapshots(world.fake)) == 4
    _same(
        decrypt(world.fake.files["snap-0002-abcdefghij"].content, PASSPHRASE).plaintext,
        second.to_json_bytes(),
    )


def test_failed_restore_upload_leaves_the_current_snapshot(world: World) -> None:
    first = _records(world.root, "11.00")
    second = _records(world.root, "22.00")
    _plant(world.fake, first, 1, "snap-0001-abcdefghij")
    _plant(world.fake, second, 2, "snap-0002-abcdefghij")
    _free_lock(world.fake)
    assert world.session.open_with_passphrase(PASSPHRASE).generation == 2
    older = next(
        item for item in world.session.list_backups().valid if item.generation == 1
    )
    before = {
        item.file_id: (bytes(item.content), item.trashed, item.name)
        for item in world.fake.files.values()
        if generation_of_name(item.name) is not None
    }
    world.fake.lose_create_response = True
    world.fake.apply_lost_create = False
    failed = world.session.restore_backup(older.key)
    _quiet(failed)
    assert "not confirmed" in failed.message or "left in place" in failed.message
    assert world.session.generation == 2
    assert world.session.dirty is False
    assert world.session.store is not None
    assert world.session.store.income[0].amount == Decimal("22")
    assert len(_snapshots(world.fake)) == 2
    for file_id, (content, trashed, name) in before.items():
        item = world.fake.files[file_id]
        assert item.content == content
        assert item.trashed is trashed
        assert item.name == name


def test_retention_after_restore_keeps_the_previous_latest(world: World, monkeypatch) -> None:
    source = _records(world.root, "11.00")
    _plant(world.fake, source, 1, "snap-0001-abcdefghij")
    _free_lock(world.fake)
    assert world.session.open_with_passphrase(PASSPHRASE).generation == 1
    for amount in ("12.00", "13.00", "14.00"):
        world.session.store.add_income(
            amount=amount,
            on_date=date(2026, 10, 8),
            user="Boris",
            note="extra",
        )
        saved = world.session.save()
        _quiet(saved)
        assert saved.dirty is False
    assert world.session.generation == 4
    previous = next(
        item for item in _snapshots(world.fake) if generation_of_name(item.name) == 4
    )
    older = next(
        item for item in world.session.list_backups().valid if item.generation == 1
    )
    monkeypatch.setattr("finance.snapshots.KEEP_RECENT_SNAPSHOTS", 2)
    monkeypatch.setattr("finance.snapshots.MIN_KNOWN_VALID", 2)
    restored = world.session.restore_backup(older.key)
    _quiet(restored)
    assert restored.generation == 5
    assert restored.dirty is False
    assert len(world.session.store.income) == 1
    assert world.session.store.income[0].amount == Decimal("11")
    assert previous.trashed is False
    kept = {
        generation_of_name(item.name)
        for item in _snapshots(world.fake)
    }
    trashed = {
        generation_of_name(item.name)
        for item in world.fake.files.values()
        if generation_of_name(item.name) is not None and item.trashed
    }
    assert kept == {4, 5}
    assert trashed == {1, 2, 3}
    newest = next(item for item in _snapshots(world.fake) if generation_of_name(item.name) == 5)
    _same(decrypt(newest.content, PASSPHRASE).plaintext, source.to_json_bytes())


def test_corrupt_newest_is_listed_and_not_replaced_on_open(world: World) -> None:
    good = _records(world.root, "5.00")
    _plant(world.fake, good, 1, "snap-0001-abcdefghij")
    world.fake.add(
        "snap-0002-badaaaaaaa",
        snapshot_name(2, NOW, "666666666666"),
        content=b"not-a-snapshot",
        created_time=NOW + timedelta(minutes=1),
    )
    _free_lock(world.fake)
    status = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(status)
    assert status.mode == MODE_EDITABLE
    assert status.generation == 1
    assert "Skipped" in status.message
    catalog = world.session.list_backups()
    assert any(item.generation == 2 for item in catalog.unusable)
    assert all(item.generation != 2 for item in catalog.valid)
    assert len(_snapshots(world.fake)) == 2
    assert SYNTHETIC_SECRET not in summary_text(catalog)


def test_restore_local_copy_into_an_empty_folder(world: World) -> None:
    source = _records(world.root, "14.20")
    name = snapshot_name(7, NOW, "abc123abc123")
    LocalEncryptedCopies(encrypted_snapshots_dir()).write(name, _sealed(source, 7, NOW))
    preview = world.session.open_with_passphrase(PASSPHRASE)
    _quiet(preview)
    assert preview.mode == MODE_SETUP
    assert CHOICE_RESTORE_LOCAL in preview.choices
    assert CHOICE_CREATE_EMPTY in preview.choices
    assert "later step" not in preview.message
    assert "uploads that copy" in preview.message
    status = world.session.restore_local_copy()
    _quiet(status)
    assert status.mode == MODE_EDITABLE
    assert status.generation == 1
    assert status.dirty is False
    assert "generation 7" in status.message
    assert load_local_config().cloud_mode is True
    assert len(_locks(world.fake)) == 1
    snapshots = _snapshots(world.fake)
    assert len(snapshots) == 1
    opened = decrypt(snapshots[0].content, PASSPHRASE)
    _same(opened.plaintext, source.to_json_bytes())
    assert opened.metadata.generation == 1
    assert (encrypted_snapshots_dir() / name).is_file()
    assert not world.legacy.exists()


def test_wrong_passphrase_does_not_upload_a_local_copy(world: World) -> None:
    source = _records(world.root, "14.20")
    name = snapshot_name(7, NOW, "abc123abc123")
    LocalEncryptedCopies(encrypted_snapshots_dir()).write(name, _sealed(source, 7, NOW))
    preview = world.session.open_with_passphrase("wrong-passphrase-value")
    _quiet(preview)
    assert CHOICE_RESTORE_LOCAL in preview.choices
    failed = world.session.restore_local_copy()
    _quiet(failed)
    assert "did not open" in failed.message
    assert failed.can_edit is False
    assert _snapshots(world.fake) == []
    assert _locks(world.fake) == []
    assert load_local_config().cloud_mode is False
    assert "wrong-passphrase-value" not in failed.message
    assert (encrypted_snapshots_dir() / name).is_file()
