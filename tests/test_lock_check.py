"""The manual lock check, with an in-memory Drive. No network and no finance file."""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO

from finance import default_data_path
from finance.drive_adapter import LOCK_FILE_NAME, GoogleDriveAdapter, snapshot_name
from finance.lock import blank_lock, decode_lock, encode_lock
from finance.lock_check import run_lock_check
from finance.profile import (
    CONFIG_DIR_ENV,
    config_file,
    ensure_machine_identity,
    load_local_config,
    save_local_config,
)
from tests.fake_drive import FakeDrive

FOLDER_ID = "folder-id-0123456789abc"
SNAPSHOT_ID = "snap-id-0123456789abcd"
SNAPSHOT_BYTES = b"synthetic-encrypted-snapshot"
NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)


def _adapter(fake: FakeDrive) -> GoogleDriveAdapter:
    return GoogleDriveAdapter(
        FOLDER_ID,
        service=fake.service(),
        sleep=lambda _delay: None,
        retry_delays=(),
        list_delays=(),
        list_attempts=1,
    )


def _save_folder() -> None:
    config = ensure_machine_identity()
    config.drive_folder_id = FOLDER_ID
    save_local_config(config)


def _run(argv: list[str], fake: FakeDrive, answers: list[str]):
    output = StringIO()
    before = _stamp()

    def prompt(question: str) -> str:
        if not answers:
            raise AssertionError(question)
        return answers.pop(0)

    def opener(folder_id: str):
        assert folder_id == FOLDER_ID
        return _adapter(fake)

    code = run_lock_check(
        argv,
        prompt=prompt,
        output=output,
        opener=opener,
        confirmation_sleep=lambda _delay: None,
    )
    assert _stamp() == before
    return code, output.getvalue()


def _stamp() -> tuple[bool, int | None]:
    path = default_data_path()
    if not path.exists():
        return False, None
    return True, path.stat().st_mtime_ns


def _add_snapshot(fake: FakeDrive) -> None:
    fake.add(SNAPSHOT_ID, snapshot_name(1, NOW, "abcabcabcabc"), content=SNAPSHOT_BYTES)


def test_refuses_without_a_dry_run_profile(monkeypatch) -> None:
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    code, text = _run(["hold"], FakeDrive(FOLDER_ID), ["FinanceTracker test"])
    assert code == 1
    assert "FINANCETRACKER_CONFIG_DIR" in text


def test_refuses_a_profile_inside_the_source_tree(monkeypatch) -> None:
    monkeypatch.setenv(CONFIG_DIR_ENV, str(default_data_path().parent))
    code, text = _run(["open"], FakeDrive(FOLDER_ID), ["FinanceTracker test"])
    assert code == 1
    assert "source directory" in text
    assert not (default_data_path().parent / "config.json").exists()


def test_hold_releases_and_leaves_a_snapshot_in_place() -> None:
    _save_folder()
    fake = FakeDrive(FOLDER_ID)
    _add_snapshot(fake)
    fake.add("lock-id-0123456789abcd", LOCK_FILE_NAME, content=encode_lock(blank_lock()))
    code, text = _run(["hold"], fake, ["FinanceTracker test", ""])
    assert code == 0, text
    assert "Mode: editable" in text
    assert "Mode: paused" in text
    assert "Cloud mode was not turned on." in text
    assert decode_lock(_only_lock(fake).content).state == "free"
    assert fake.files[SNAPSHOT_ID].content == SNAPSHOT_BYTES
    assert fake.files[SNAPSHOT_ID].trashed is False
    assert fake.delete_calls == []
    assert '"cloud_mode": false' in config_file().read_text(encoding="utf-8")


def test_open_reports_a_stale_plant_and_recover_takes_it() -> None:
    _save_folder()
    fake = FakeDrive(FOLDER_ID)
    _add_snapshot(fake)
    code, text = _run(["plant", "--owner", "Milena"], fake, ["FinanceTracker test"])
    assert code == 0, text
    assert "stale" in text
    assert "Milena" in text
    planted = _only_lock(fake).content
    assert fake.files[SNAPSHOT_ID].content == SNAPSHOT_BYTES

    code, text = _run(["open", "--owner", "Boris"], fake, ["FinanceTracker test", ""])
    assert code == 0, text
    assert "Mode: stale" in text
    assert "Nothing was changed." in text
    assert _only_lock(fake).content == planted
    assert fake.files[SNAPSHOT_ID].content == SNAPSHOT_BYTES
    assert fake.delete_calls == []
    assert load_local_config().cloud_mode is False

    code, text = _run(
        ["open", "--owner", "Boris"],
        fake,
        ["FinanceTracker test", "RECOVER", ""],
    )
    assert code == 0, text
    assert "Mode: editable" in text
    assert "Mode: paused" in text
    assert decode_lock(_only_lock(fake).content).state == "free"
    assert fake.files[SNAPSHOT_ID].content == SNAPSHOT_BYTES
    assert fake.files[SNAPSHOT_ID].trashed is False
    assert fake.delete_calls == []


def test_a_fresh_plant_stays_read_only_when_the_phrase_is_not_typed() -> None:
    _save_folder()
    fake = FakeDrive(FOLDER_ID)
    _add_snapshot(fake)
    assert _run(["plant", "--fresh", "--owner", "Milena"], fake, ["FinanceTracker test"])[0] == 0
    planted = _only_lock(fake).content
    code, text = _run(["open"], fake, ["FinanceTracker test", "no"])
    assert code == 0, text
    assert "Mode: read_only" in text
    assert "Nothing was changed." in text or "read-only" in text
    assert _only_lock(fake).content == planted
    assert fake.files[SNAPSHOT_ID].trashed is False
    assert fake.delete_calls == []


def _only_lock(fake: FakeDrive):
    locks = [item for item in fake.files.values() if item.name == LOCK_FILE_NAME and not item.trashed]
    assert len(locks) == 1
    return locks[0]
