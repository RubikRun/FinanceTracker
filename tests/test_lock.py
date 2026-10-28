"""Drive lock behavior against an in-memory Drive. No network and no finance file."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from finance import default_data_path
from finance.drive_adapter import LOCK_FILE_NAME, GoogleDriveAdapter, snapshot_name
from finance.lock import (
    ACQUIRE_CONFIRMATION_DELAY,
    MODE_CHANGED,
    MODE_DUPLICATE,
    MODE_EDITABLE,
    MODE_LOST,
    MODE_MISSING,
    MODE_PAUSED,
    MODE_READ_ONLY,
    MODE_STALE,
    MODE_TAKEOVER,
    MODE_UNREADABLE,
    MODE_UNVERIFIED,
    LockError,
    LockSession,
    ProcessLock,
    blank_lock,
    decode_lock,
    encode_lock,
)
from finance.profile import (
    default_machine_label,
    ensure_machine_identity,
    load_local_config,
    lock_session_path,
    process_lock_path,
    save_local_config,
)
from tests.fake_drive import FakeDrive
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

FOLDER_ID = "folder-id-0123456789abc"
LOCK_ID = "lock-id-0123456789abcd"
SNAPSHOT_ID = "snap-id-0123456789abcd"
SNAPSHOT_BYTES = b"synthetic-encrypted-snapshot"
OTHER_MACHINE = "ab" * 32
NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
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


class _Generations:
    def __init__(self) -> None:
        self.base = 1
        self.newest = 1
        self.reloads = 0

    def base_generation(self) -> int:
        return self.base

    def newest_generation(self) -> int:
        return self.newest

    def reload(self) -> int:
        self.reloads += 1
        self.base = self.newest
        return self.base


@dataclass
class World:
    fake: FakeDrive
    adapter: GoogleDriveAdapter
    session: LockSession
    sleeps: list[float]
    generations: _Generations
    stamp: tuple[bool, int | None]


def _bundle(
    *,
    locks: int = 1,
    snapshot: bool = True,
    sleep=None,
    generations: _Generations | None = None,
) -> World:
    fake = FakeDrive(FOLDER_ID)
    fake.date_header = _http_date(NOW)
    if snapshot:
        fake.add(
            SNAPSHOT_ID,
            snapshot_name(1, NOW, "abcabcabcabc"),
            content=SNAPSHOT_BYTES,
        )
    for index in range(locks):
        file_id = LOCK_ID if index == 0 else f"lock-extra-{index:04d}-abcdef"
        fake.add(file_id, LOCK_FILE_NAME, content=encode_lock(blank_lock()))
    sleeps: list[float] = []

    def record(delay: float) -> None:
        sleeps.append(delay)
        if sleep is not None:
            sleep(delay)

    adapter = GoogleDriveAdapter(
        FOLDER_ID,
        service=fake.service(),
        sleep=lambda _delay: None,
        retry_delays=(),
        list_delays=(),
        list_attempts=1,
    )
    view = generations if generations is not None else _Generations()
    session = LockSession(
        adapter,
        owner_label="Boris",
        generations=view,
        sleep=record,
    )
    return World(fake, adapter, session, sleeps, view, _finance_stamp())


def _finish(world: World) -> None:
    world.session.close()
    world.adapter.close()
    item = world.fake.files.get(SNAPSHOT_ID)
    if item is not None:
        assert item.content == SNAPSHOT_BYTES
        assert item.trashed is False
    assert world.fake.delete_calls == []
    assert all("appProperties" not in call for call in world.fake.calls)
    assert world.stamp == _finance_stamp()
    assert load_local_config().cloud_mode is False


def _at(world: World, when: datetime) -> None:
    world.fake.date_header = _http_date(when)


def _lock(world: World):
    locks = [item for item in world.fake.files.values() if item.name == LOCK_FILE_NAME]
    assert len(locks) == 1
    return locks[0]


@pytest.fixture
def world() -> World:
    bundle = _bundle()
    yield bundle
    _finish(bundle)


def test_round_trip_and_a_bad_file_hides_its_bytes() -> None:
    restored = decode_lock(encode_lock(blank_lock()))
    assert restored.state == "free"
    assert restored.session_token == ""
    held = decode_lock(
        encode_lock(
            replace(
                blank_lock(),
                state="held",
                session_token="ab" * 32,
                owner_label="Milena",
                machine_id=OTHER_MACHINE,
                machine_label="Other computer",
                acquired_at_utc=NOW,
                heartbeat_at_utc=NOW,
            )
        )
    )
    assert held.owner_label == "Milena"
    assert held.heartbeat_at_utc == NOW
    payload = json.dumps({"format_version": 1, "note": SYNTHETIC_SECRET}).encode()
    with pytest.raises(LockError) as caught:
        decode_lock(payload)
    assert_failure_omits_payload(caught.value)


def test_process_lock_is_exclusive_and_a_closed_handle_can_be_taken() -> None:
    first = ProcessLock(process_lock_path())
    second = ProcessLock(process_lock_path())
    assert first.try_acquire() is True
    assert second.try_acquire() is False
    first.release()
    assert second.try_acquire() is True
    second.release()


def test_acquire_and_release_then_the_next_session_can_take_it(world: World) -> None:
    status = world.session.start()
    assert status.can_edit is True
    assert status.mode == MODE_EDITABLE
    assert world.sleeps == [ACQUIRE_CONFIRMATION_DELAY.total_seconds()]
    document = decode_lock(_lock(world).content)
    assert document.state == "held"
    assert document.owner_label == "Boris"
    assert document.machine_label == default_machine_label()
    assert len(document.session_token) == 64
    assert document.session_token not in status.message
    assert document.heartbeat_at_utc == NOW
    saved = json.loads(lock_session_path().read_text(encoding="utf-8"))
    assert saved["machine_id"] == document.machine_id
    assert saved["session_token"] == document.session_token
    assert load_local_config().lock_file_id == LOCK_ID

    released = world.session.close()
    assert released.mode == MODE_PAUSED
    assert decode_lock(_lock(world).content).state == "free"
    assert decode_lock(_lock(world).content).session_token == ""
    assert not lock_session_path().exists()

    follower = LockSession(world.adapter, owner_label="Milena", sleep=lambda _delay: None)
    try:
        assert follower.start().can_edit is True
        assert decode_lock(_lock(world).content).owner_label == "Milena"
    finally:
        follower.close()


def test_a_confirmation_race_leaves_the_other_token_in_place() -> None:
    stolen = encode_lock(
        replace(
            blank_lock(),
            state="held",
            session_token="11" * 32,
            owner_label="Milena",
            machine_id=OTHER_MACHINE,
            machine_label="Other computer",
            acquired_at_utc=NOW,
            heartbeat_at_utc=NOW,
        )
    )

    def steal(delay: float) -> None:
        assert delay == 5
        _lock(bundle).content = stolen

    bundle = _bundle(sleep=steal)
    try:
        status = bundle.session.start()
        assert status.mode == MODE_READ_ONLY
        assert status.can_edit is False
        assert "read-only" in status.message
        assert _lock(bundle).content == stolen
        assert bundle.fake.delete_calls == []
    finally:
        _finish(bundle)


def test_a_fresh_lock_is_read_only_and_a_stale_one_waits_for_confirmation(world: World) -> None:
    held = _held("Milena", NOW)
    _lock(world).content = encode_lock(held)
    before = _lock(world).content
    status = world.session.start()
    assert status.mode == MODE_READ_ONLY
    assert status.can_edit is False
    assert "Milena" in status.message
    assert "read-only" in status.message
    assert _lock(world).content == before

    _lock(world).content = encode_lock(_held("Milena", NOW - timedelta(minutes=4)))
    before = _lock(world).content
    world.session.close()
    again = LockSession(world.adapter, owner_label="Boris", sleep=lambda _delay: None)
    try:
        status = again.start()
        assert status.mode == MODE_STALE
        assert status.can_edit is False
        assert "not taken" in status.message
        assert _lock(world).content == before
        changed = encode_lock(_held("Milena", NOW - timedelta(minutes=5)))
        _lock(world).content = changed
        refused = again.recover(status)
        assert refused.mode == MODE_CHANGED
        assert _lock(world).content == changed
    finally:
        again.close()


def test_recover_takes_a_stale_lock_only_when_it_is_unchanged(world: World) -> None:
    _lock(world).content = encode_lock(_held("Milena", NOW - timedelta(minutes=4)))
    status = world.session.start()
    assert status.mode == MODE_STALE
    taken = world.session.recover(status)
    assert taken.can_edit is True
    document = decode_lock(_lock(world).content)
    assert document.owner_label == "Boris"
    assert document.machine_id != OTHER_MACHINE
    assert document.session_token != "11" * 32


def test_duplicate_and_missing_lock_files_are_not_guessed() -> None:
    duplicate = _bundle(locks=2)
    try:
        left = duplicate.fake.files[LOCK_ID].content
        right_id = next(item.file_id for item in duplicate.fake.files.values() if item.file_id != LOCK_ID and item.name == LOCK_FILE_NAME)
        right = duplicate.fake.files[right_id].content
        status = duplicate.session.start()
        assert status.mode == MODE_DUPLICATE
        assert duplicate.fake.files[LOCK_ID].content == left
        assert duplicate.fake.files[right_id].content == right
    finally:
        _finish(duplicate)

    missing = _bundle(locks=0)
    try:
        status = missing.session.start()
        assert status.mode == MODE_MISSING
        assert "snapshots" in status.message
        assert not any(item.name == LOCK_FILE_NAME for item in missing.fake.files.values())
        created = missing.session.create_missing_lock()
        assert created.can_edit is True
        assert sum(item.name == LOCK_FILE_NAME for item in missing.fake.files.values()) == 1
    finally:
        _finish(missing)

    empty = _bundle(locks=0, snapshot=False)
    try:
        status = empty.session.start()
        assert status.mode == MODE_MISSING
        assert "Nothing was created." in status.message
        assert empty.fake.files == {}
    finally:
        _finish(empty)


def test_a_saved_lock_id_that_does_not_match_the_folder_is_left_alone() -> None:
    config = ensure_machine_identity()
    config.lock_file_id = "other-lock-0123456789ab"
    save_local_config(config)
    bundle = _bundle()
    try:
        before = _lock(bundle).content
        status = bundle.session.start()
        assert status.mode == MODE_DUPLICATE
        assert "not the one saved" in status.message
        assert _lock(bundle).content == before
        assert load_local_config().lock_file_id == "other-lock-0123456789ab"
    finally:
        _finish(bundle)


def test_heartbeat_updates_and_a_different_token_is_not_overwritten(world: World) -> None:
    assert world.session.start().can_edit is True
    _at(world, NOW + timedelta(seconds=30))
    assert world.session.heartbeat().can_edit is True
    assert decode_lock(_lock(world).content).heartbeat_at_utc == NOW + timedelta(seconds=30)

    stolen = encode_lock(_held("Milena", NOW))
    _lock(world).content = stolen
    lost = world.session.heartbeat()
    assert lost.mode == MODE_LOST
    assert lost.can_edit is False
    assert _lock(world).content == stolen


def test_save_accepts_a_matching_token_and_refuses_a_different_one(world: World) -> None:
    assert world.session.start().can_edit is True
    assert world.session.confirm_for_save().can_edit is True
    stolen = encode_lock(_held("Milena", NOW))
    _lock(world).content = stolen
    refused = world.session.confirm_for_save()
    assert refused.can_edit is False
    assert refused.mode == MODE_LOST
    assert _lock(world).content == stolen


def test_a_stale_heartbeat_refuses_the_save_without_writing(world: World) -> None:
    assert world.session.start().can_edit is True
    current = decode_lock(_lock(world).content)
    rewritten = encode_lock(replace(current, heartbeat_at_utc=NOW - timedelta(minutes=4)))
    _lock(world).content = rewritten
    refused = world.session.confirm_for_save()
    assert refused.mode == MODE_UNVERIFIED
    assert refused.can_edit is False
    assert _lock(world).content == rewritten


def test_a_long_gap_asks_for_a_fresh_read_before_a_save(world: World) -> None:
    assert world.session.start().can_edit is True
    original = _lock(world).content
    _at(world, NOW + timedelta(minutes=4))
    refused = world.session.confirm_for_save()
    assert refused.can_edit is False
    assert _lock(world).content == original
    confirmed = world.session.reverify()
    assert confirmed.can_edit is True
    assert decode_lock(_lock(world).content).heartbeat_at_utc == NOW + timedelta(minutes=4)
    assert world.session.confirm_for_save().can_edit is True


def test_drive_time_is_used_when_the_local_clock_disagrees(world: World, monkeypatch) -> None:
    class _Skewed(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(minutes=7)

    monkeypatch.setattr("finance.lock.datetime", _Skewed)
    assert world.session.start().can_edit is True
    assert decode_lock(_lock(world).content).heartbeat_at_utc == NOW
    _at(world, NOW + timedelta(minutes=2))
    assert world.session.confirm_for_save().can_edit is True
    _at(world, NOW + timedelta(minutes=4))
    assert world.session.confirm_for_save().can_edit is False


def test_release_does_not_change_another_sessions_lock(world: World) -> None:
    assert world.session.start().can_edit is True
    stolen = encode_lock(_held("Milena", NOW))
    _lock(world).content = stolen
    released = world.session.release()
    assert released.mode == MODE_LOST
    assert _lock(world).content == stolen


def test_a_dirty_session_keeps_the_lock_on_release_and_on_close(world: World) -> None:
    assert world.session.start().can_edit is True
    world.session.set_dirty(True)
    kept = world.session.release()
    assert kept.can_edit is True
    assert decode_lock(_lock(world).content).state == "held"
    closed = world.session.close()
    assert "not released" in closed.message
    assert decode_lock(_lock(world).content).state == "held"


def test_same_computer_crash_can_be_taken_and_a_live_window_cannot() -> None:
    first = _bundle()
    second_session: LockSession | None = None
    try:
        assert first.session.start().can_edit is True
        token = decode_lock(_lock(first).content).session_token
        before = _lock(first).content
        first.session.process_lock.release()
        second_session = LockSession(
            first.adapter,
            owner_label="Boris",
            sleep=lambda _delay: None,
        )
        offered = second_session.start()
        assert offered.mode == MODE_TAKEOVER
        assert "not taken" in offered.message
        assert _lock(first).content == before
        taken = second_session.recover(offered)
        assert taken.can_edit is True
        assert decode_lock(_lock(first).content).session_token != token
        late = first.session.release()
        assert late.mode == MODE_LOST
        assert decode_lock(_lock(first).content).session_token != token
    finally:
        if second_session is not None:
            second_session.close()
        _finish(first)

    busy = _bundle()
    other: LockSession | None = None
    try:
        assert busy.session.start().can_edit is True
        before = _lock(busy).content
        other = LockSession(busy.adapter, owner_label="Milena", sleep=lambda _delay: None)
        status = other.start()
        assert status.mode == MODE_READ_ONLY
        assert status.can_edit is False
        assert "window" in status.message
        assert _lock(busy).content == before
        refused = other.recover(status)
        assert refused.can_edit is False
        assert _lock(busy).content == before
    finally:
        if other is not None:
            other.close()
        _finish(busy)


def test_activity_keeps_a_clean_session_from_releasing(world: World) -> None:
    assert world.session.start().can_edit is True
    world.session.note_activity()
    _at(world, NOW + timedelta(minutes=10))
    status = world.session.poll()
    assert status.can_edit is True
    assert decode_lock(_lock(world).content).state == "held"


def test_idle_release_waits_for_a_clean_session_and_resume_reloads(world: World) -> None:
    assert world.session.start().can_edit is True
    world.session.set_dirty(True)
    _at(world, NOW + timedelta(minutes=10))
    still = world.session.poll()
    assert still.can_edit is True
    assert decode_lock(_lock(world).content).state == "held"
    world.session.set_dirty(False)
    paused = world.session.poll()
    assert paused.mode == MODE_PAUSED
    assert decode_lock(_lock(world).content).state == "free"

    world.generations.newest = 4
    resumed = world.session.resume()
    assert world.generations.reloads == 1
    assert world.generations.base == 4
    assert resumed.can_edit is True
    assert resumed.reloaded is True
    assert "other computer" in resumed.message


def test_resume_reloads_and_stays_read_only_when_the_race_is_lost() -> None:
    stolen = encode_lock(_held("Milena", NOW + timedelta(minutes=10)))
    calls = {"n": 0}

    def steal(delay: float) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            _lock(bundle).content = stolen

    bundle = _bundle(sleep=steal)
    try:
        assert bundle.session.start().can_edit is True
        _at(bundle, NOW + timedelta(minutes=10))
        assert bundle.session.poll().mode == MODE_PAUSED
        bundle.generations.newest = 5
        status = bundle.session.resume()
        assert bundle.generations.reloads == 1
        assert status.can_edit is False
        assert status.reloaded is True
        assert "other computer" in status.message
        assert _lock(bundle).content == stolen
    finally:
        _finish(bundle)


def test_an_unreadable_lock_is_not_overwritten_and_hides_its_bytes(world: World) -> None:
    payload = f"not-json {SYNTHETIC_SECRET}".encode()
    _lock(world).content = payload
    status = world.session.start()
    assert status.mode == MODE_UNREADABLE
    assert status.can_edit is False
    if SYNTHETIC_SECRET in status.message or SYNTHETIC_SECRET in repr(status):
        raise AssertionError("status included a synthetic payload marker")
    assert _lock(world).content == payload


def test_a_machine_label_with_a_marker_is_not_copied_into_the_message(world: World) -> None:
    document = _held("Milena", NOW)
    labeled = replace(document, machine_label=SYNTHETIC_SECRET)
    _lock(world).content = encode_lock(labeled)
    status = world.session.start()
    assert status.mode == MODE_READ_ONLY
    if SYNTHETIC_SECRET in status.message or SYNTHETIC_SECRET in status.machine_label:
        raise AssertionError("status included a synthetic payload marker")
    assert _lock(world).content == encode_lock(labeled)


def test_the_timer_polls_without_waiting_for_a_heartbeat_interval(world: World) -> None:
    seen = threading.Event()
    original = world.session.poll

    def wrapped() -> object:
        seen.set()
        return original()

    world.session.poll = wrapped  # type: ignore[method-assign]
    world.session.start_timer(interval=0.01)
    assert seen.wait(2)
    world.session.stop_timer()


def _held(owner: str, when: datetime):
    return replace(
        blank_lock(),
        state="held",
        session_token="11" * 32,
        owner_label=owner,
        machine_id=OTHER_MACHINE,
        machine_label="Other computer",
        acquired_at_utc=when,
        heartbeat_at_utc=when,
    )
