"""The manual snapshot check, with an in-memory Drive. No network and no finance file."""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO

from finance import default_data_path
from finance.drive_adapter import GoogleDriveAdapter, generation_of_name
from finance.profile import (
    CONFIG_DIR_ENV,
    config_file,
    encrypted_snapshots_dir,
    ensure_machine_identity,
    load_local_config,
    save_local_config,
)
from finance.snapshot_check import MARKER, run_snapshot_check
from tests.fake_drive import FakeDrive

FOLDER_ID = "folder-id-0123456789abc"
WHEN = datetime(2026, 10, 22, 9, 41, tzinfo=timezone.utc)


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


def _run(argv: list[str], fake: FakeDrive | None, answers: list[str], save_count: int | None = 1):
    output = StringIO()
    before = _stamp()
    adapter = _adapter(fake) if fake is not None else None

    def prompt(question: str) -> str:
        if not answers:
            raise AssertionError(question)
        return answers.pop(0)

    def opener(folder_id: str):
        assert folder_id == FOLDER_ID
        assert adapter is not None
        return adapter

    code = run_snapshot_check(
        argv,
        prompt=prompt,
        output=output,
        opener=opener if fake is not None else None,
        save_count=save_count,
    )
    if adapter is not None and not adapter._closed:
        adapter.close()
    assert _stamp() == before
    return code, output.getvalue(), fake


def _stamp() -> tuple[bool, int | None]:
    path = default_data_path()
    if not path.exists():
        return False, None
    return True, path.stat().st_mtime_ns


def test_refuses_without_a_dry_run_profile(monkeypatch) -> None:
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    code, text, _fake = _run(["fill"], FakeDrive(FOLDER_ID), ["FinanceTracker test"])
    assert code == 1
    assert "FINANCETRACKER_CONFIG_DIR" in text
    assert MARKER not in text


def test_refuses_a_profile_inside_the_source_tree(monkeypatch) -> None:
    monkeypatch.setenv(CONFIG_DIR_ENV, str(default_data_path().parent))
    code, text, _fake = _run(["open"], FakeDrive(FOLDER_ID), ["FinanceTracker test"])
    assert code == 1
    assert "source directory" in text
    assert not (default_data_path().parent / "config.json").exists()


def test_fill_cancel_uploads_nothing() -> None:
    _save_folder()
    fake = FakeDrive(FOLDER_ID)
    code, text, _fake = _run(["fill"], fake, [""])
    assert code == 2
    assert fake.files == {}
    assert "cancelled" in text
    assert load_local_config().cloud_mode is False


def test_fill_one_synthetic_snapshot_stays_encrypted() -> None:
    _save_folder()
    fake = FakeDrive(FOLDER_ID)
    code, text, _fake = _run(["fill"], fake, ["FinanceTracker test"], save_count=1)
    assert code == 0, text
    assert MARKER not in text
    assert "snapshot-check-passphrase" in text
    assert "Cloud mode was not turned on." in text
    snapshots = [item for item in fake.files.values() if generation_of_name(item.name)]
    assert len(snapshots) == 1
    assert snapshots[0].trashed is False
    assert snapshots[0].content.startswith(b"FTEN")
    assert MARKER.encode("utf-8") not in snapshots[0].content
    local = list(encrypted_snapshots_dir().iterdir())
    assert local
    assert all(path.read_bytes().startswith(b"FTEN") for path in local)
    assert all(MARKER.encode("utf-8") not in path.read_bytes() for path in local)
    assert fake.delete_calls == []
    assert '"cloud_mode": false' in config_file().read_text(encoding="utf-8")
    assert not (encrypted_snapshots_dir().parent / "snapshot-memory.json").exists()


def test_extend_does_not_trash_the_other_accounts_snapshot() -> None:
    _save_folder()
    fake = FakeDrive(FOLDER_ID)
    assert _run(["fill"], fake, ["FinanceTracker test"], save_count=1)[0] == 0
    original = next(iter(fake.files.values()))
    original.can_trash = False
    code, text, _fake = _run(["extend"], fake, ["FinanceTracker test"], save_count=1)
    assert code == 0, text
    assert original.trashed is False
    assert "cannot trash" in text
    assert MARKER not in text
    assert fake.delete_calls == []


def test_clean_moves_only_an_owned_snapshot_to_the_trash() -> None:
    _save_folder()
    fake = FakeDrive(FOLDER_ID)
    assert _run(["fill"], fake, ["FinanceTracker test"], save_count=1)[0] == 0
    code, text, _fake = _run(["clean"], fake, ["FinanceTracker test"], save_count=None)
    assert code == 0, text
    assert all(item.trashed for item in fake.files.values())
    assert "not permanently deleted" in text
    assert fake.delete_calls == []
