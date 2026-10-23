"""The manual Drive check, with a stand-in adapter. No network and no finance file."""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from finance import default_data_path
from finance.drive_adapter import (
    LOCK_FILE_NAME,
    NOT_OWNER_SKIPPED,
    DriveFile,
    DriveOwner,
    GenerationListing,
    TrashResult,
    UploadedFile,
)
from finance.drive_check import (
    LOCK_V1,
    LOCK_V2,
    SNAPSHOT_V1,
    SNAPSHOT_V2,
    run_drive_check,
)
from finance.drive_folder import DriveFolder
from finance.profile import (
    CONFIG_DIR_ENV,
    config_file,
    ensure_machine_identity,
    save_local_config,
)
from tests.support import SYNTHETIC_SECRET

FOLDER_ID = "folder-id-0123456789abc"
SNAPSHOT_ID = "snapshot-id-0123456789"
LOCK_ID = "lock-file-id-012345678"
WHEN = datetime(2026, 10, 22, 9, 41, tzinfo=timezone.utc)
FOLDER = DriveFolder(
    folder_id=FOLDER_ID,
    name="FinanceTracker test",
    owner_display_name="Test Owner",
    owner_email="owner@example.com",
    followed_shortcut=False,
)


def _file(file_id: str, name: str) -> DriveFile:
    return DriveFile(
        file_id=file_id,
        name=name,
        parents=(FOLDER_ID,),
        owners=(DriveOwner("Account A", "a@example.com"),),
        can_trash=True,
        created_time=WHEN,
        md5_checksum="",
        trashed=False,
    )


class StubAdapter:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.closed = False
        self.locks: list[DriveFile] = []
        self.lock_bytes = LOCK_V1
        self.snapshot_bytes = SNAPSHOT_V1
        self.show_snapshot = False
        self.can_trash = False
        self.snapshot = _file(
            SNAPSHOT_ID,
            "finance_snapshot_g000001_20261022T094100Z_abc123abc123.enc",
        )
        self.lock = _file(LOCK_ID, LOCK_FILE_NAME)

    def describe_folder(self) -> DriveFolder:
        self.calls.append("describe")
        return FOLDER

    def list_for_generation(self, known_ids: tuple[str, ...] = ()) -> GenerationListing:
        self.calls.append(("list", tuple(known_ids)))
        files = list(self.locks)
        if self.show_snapshot:
            files.append(self.snapshot)
        return GenerationListing(tuple(files), ())

    def upload_snapshot(self, generation: int, content: bytes, **kwargs: object) -> UploadedFile:
        self.calls.append(("upload", generation, content))
        return UploadedFile(
            self.snapshot,
            GenerationListing((self.snapshot,), (self.snapshot.file_id,)),
        )

    def create_lock(self, content: bytes) -> UploadedFile:
        self.calls.append(("create_lock", content))
        self.locks = [self.lock]
        return UploadedFile(self.lock, GenerationListing((self.lock,), ()))

    def download(self, file_id: str) -> bytes:
        self.calls.append(("download", file_id))
        return self.snapshot_bytes

    def read_lock(self, file_id: str) -> bytes:
        self.calls.append(("read_lock", file_id))
        return self.lock_bytes

    def update_content(self, file_id: str, content: bytes) -> DriveFile:
        self.calls.append(("update", file_id, content))
        self.snapshot_bytes = content
        return self.snapshot

    def update_lock(self, file_id: str, content: bytes) -> DriveFile:
        self.calls.append(("update_lock", file_id, content))
        self.lock_bytes = content
        return self.lock

    def trash(self, file_id: str) -> TrashResult:
        self.calls.append(("trash", file_id))
        if file_id == SNAPSHOT_ID and not self.can_trash:
            return TrashResult(file_id, trashed=False, skipped_not_owner=True)
        return TrashResult(file_id, trashed=True, skipped_not_owner=False)

    def server_time(self) -> datetime:
        self.calls.append("time")
        return WHEN

    def close(self) -> None:
        self.closed = True


def _save_folder() -> None:
    config = ensure_machine_identity()
    config.drive_folder_id = FOLDER_ID
    save_local_config(config)


def _run(argv: list[str], stub: StubAdapter | None = None, answers: list[str] | None = None):
    output = StringIO()
    prompts: list[str] = []

    def prompt(question: str) -> str:
        prompts.append(question)
        if not answers:
            raise AssertionError(question)
        return answers.pop(0)

    opener_calls: list[str] = []

    def opener(folder_id: str) -> StubAdapter:
        opener_calls.append(folder_id)
        assert stub is not None
        return stub

    code = run_drive_check(
        argv,
        prompt=prompt,
        output=output,
        opener=opener if stub is not None else opener,
    )
    return code, output.getvalue(), stub, opener_calls, prompts


def test_refuses_without_a_dry_run_profile(monkeypatch) -> None:
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    code, text, _stub, opened, _prompts = _run(["ping"], StubAdapter())
    assert code == 1
    assert "FINANCETRACKER_CONFIG_DIR" in text
    assert opened == []


def test_refuses_a_profile_inside_the_source_tree(monkeypatch) -> None:
    monkeypatch.setenv(CONFIG_DIR_ENV, str(default_data_path().parent))
    code, text, _stub, opened, _prompts = _run(["ping"], StubAdapter())
    assert code == 1
    assert "source directory" in text
    assert opened == []
    assert not (default_data_path().parent / "config.json").exists()


def test_refuses_when_no_folder_is_saved() -> None:
    ensure_machine_identity()
    code, text, _stub, opened, _prompts = _run(["ping"], StubAdapter())
    assert code == 1
    assert "google_setup" in text
    assert opened == []


def test_refuses_when_cloud_mode_is_on() -> None:
    _save_folder()
    text_file = config_file().read_text(encoding="utf-8").replace(
        '"cloud_mode": false',
        '"cloud_mode": true',
    )
    config_file().write_text(text_file, encoding="utf-8")
    code, text, _stub, opened, _prompts = _run(["create"], StubAdapter(), ["FinanceTracker test"])
    assert code == 1
    assert "Cloud mode is already on" in text
    assert opened == []
    assert '"cloud_mode": true' in config_file().read_text(encoding="utf-8")


def test_create_uploads_only_the_synthetic_markers() -> None:
    _save_folder()
    legacy = default_data_path()
    before = legacy.stat().st_mtime_ns if legacy.exists() else None
    existed = legacy.exists()
    stub = StubAdapter()
    code, text, _stub, opened, prompts = _run(
        ["create"],
        stub,
        ["FinanceTracker test"],
    )
    assert code == 0
    assert opened == [FOLDER_ID]
    assert prompts == ["Folder name: "]
    assert ("upload", 1, SNAPSHOT_V1) in stub.calls
    assert ("create_lock", LOCK_V1) in stub.calls
    assert SNAPSHOT_ID in text
    assert LOCK_ID in text
    assert "not finance records" in text
    assert "files.get of the new snapshot id succeeded." in text
    assert "had not caught up" in text
    assert stub.closed is True
    assert legacy.exists() is existed
    after = legacy.stat().st_mtime_ns if legacy.exists() else None
    assert before == after
    profile = Path(config_file().parent)
    assert list(profile.rglob("token.json")) == []


def test_a_wrong_folder_name_uploads_nothing() -> None:
    _save_folder()
    stub = StubAdapter()
    code, text, _stub, _opened, _prompts = _run(["create"], stub, ["nope"])
    assert code == 2
    assert "cancelled" in text
    assert not any(call[0] == "upload" for call in stub.calls if isinstance(call, tuple))
    assert stub.closed is True


def test_two_lock_files_block_the_upload() -> None:
    _save_folder()
    stub = StubAdapter()
    stub.locks = [_file(LOCK_ID, LOCK_FILE_NAME), _file("lock-file-id-abcdef123", LOCK_FILE_NAME)]
    code, text, _stub, _opened, _prompts = _run(["create"], stub, ["FinanceTracker test"])
    assert code == 1
    assert "More than one lock file" in text
    assert not any(isinstance(call, tuple) and call[0] == "upload" for call in stub.calls)


def test_partner_updates_and_skips_trash() -> None:
    _save_folder()
    stub = StubAdapter()
    stub.locks = [stub.lock]
    stub.show_snapshot = True
    code, text, _stub, _opened, _prompts = _run(
        ["partner", SNAPSHOT_ID, LOCK_ID],
        stub,
        ["FinanceTracker test"],
    )
    assert code == 0
    assert ("download", SNAPSHOT_ID) in stub.calls
    assert ("update", SNAPSHOT_ID, SNAPSHOT_V2) in stub.calls
    assert ("update_lock", LOCK_ID, LOCK_V2) in stub.calls
    assert NOT_OWNER_SKIPPED in text
    assert "still in the folder" in text
    assert stub.closed is True


def test_partner_does_not_print_unexpected_bytes() -> None:
    _save_folder()
    stub = StubAdapter()
    stub.locks = [stub.lock]
    stub.show_snapshot = True
    stub.snapshot_bytes = SYNTHETIC_SECRET.encode("utf-8")
    code, text, _stub, _opened, _prompts = _run(
        ["partner", SNAPSHOT_ID, LOCK_ID],
        stub,
        ["FinanceTracker test"],
    )
    assert code == 1
    assert SYNTHETIC_SECRET not in text
    assert "Length:" in text
    assert not any(isinstance(call, tuple) and call[0] == "update" for call in stub.calls)
    assert not any(isinstance(call, tuple) and call[0] == "trash" for call in stub.calls)


def test_finish_trashes_only_the_updated_markers() -> None:
    _save_folder()
    stub = StubAdapter()
    stub.can_trash = True
    stub.snapshot_bytes = SNAPSHOT_V2
    stub.lock_bytes = LOCK_V2
    code, text, _stub, _opened, _prompts = _run(
        ["finish", SNAPSHOT_ID, LOCK_ID],
        stub,
        ["FinanceTracker test"],
    )
    assert code == 0
    assert "read back by file id" in text
    assert "not permanently deleted" in text
    assert ("trash", SNAPSHOT_ID) in stub.calls
    assert ("trash", LOCK_ID) in stub.calls

    stub = StubAdapter()
    stub.can_trash = True
    stub.snapshot_bytes = SNAPSHOT_V1
    stub.lock_bytes = LOCK_V2
    code, text, _stub, _opened, _prompts = _run(
        ["finish", SNAPSHOT_ID, LOCK_ID],
        stub,
        ["FinanceTracker test"],
    )
    assert code == 1
    assert "not the drive-check marker" in text
    assert not any(isinstance(call, tuple) and call[0] == "trash" for call in stub.calls)


def test_ping_prints_drive_time_without_asking_for_a_name() -> None:
    _save_folder()
    stub = StubAdapter()

    def prompt(question: str) -> str:
        raise AssertionError(question)

    output = StringIO()
    code = run_drive_check(["ping"], prompt=prompt, output=output, opener=lambda folder_id: stub)
    assert code == 0
    assert "2026-10-22 09:41:00 UTC" in output.getvalue()
    assert "30 seconds" in output.getvalue()
    assert stub.closed is True


def test_a_bad_file_id_is_refused() -> None:
    _save_folder()
    stub = StubAdapter()
    code, text, _stub, opened, _prompts = _run(["partner", "short", LOCK_ID], stub)
    assert code == 1
    assert "cannot be used" in text
    assert opened == [FOLDER_ID]
    assert stub.closed is True
