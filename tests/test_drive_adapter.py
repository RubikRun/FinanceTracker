"""Drive adapter against an in-memory Drive service. No network and no finance file."""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from finance import default_data_path
from finance.drive_adapter import (
    FILE_FIELDS,
    LOCK_FILE_NAME,
    NOT_OWNER_SKIPPED,
    DriveAuthError,
    DriveError,
    DriveFile,
    DriveOwner,
    DrivePermissionError,
    DriveUnavailable,
    DriveUploadUncertain,
    GoogleDriveAdapter,
    classify_generation,
    generation_of_name,
    is_app_file_name,
    snapshot_name,
)
from finance.drive_folder import FOLDER_MIME, SHORTCUT_MIME
from finance.google_setup import FOLDER_LOOKUP_FIELDS
from tests.fake_drive import FakeDrive, http_error
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

FOLDER_ID = "folder-id-0123456789abc"
OTHER_FOLDER = "other-folder-0123456789a"
SHORTCUT_ID = "shortcut-id-0123456789ab"
WHEN = datetime(2026, 10, 22, 9, 41, tzinfo=timezone.utc)
RANDOM_ID = "abc123abc123"
OUTSIDE_ID = "outside-file-0123456789"
NOTES_ID = "notes-file-0123456789ab"
SNAPSHOT_BYTES = b"synthetic-snapshot-bytes"
LOCK_BYTES = b'{"format_version":1,"state":"free"}\n'
EXPECTED_QUERY = (
    f"'{FOLDER_ID}' in parents and trashed = false and "
    "(name = 'finance_lock.json' or name contains 'finance_snapshot_g')"
)
FILE_FIELD_NAMES = (
    "id",
    "name",
    "parents",
    "owners",
    "capabilities",
    "createdTime",
    "md5Checksum",
    "trashed",
)


def _name(generation: int = 1, when: datetime = WHEN, random_id: str = RANDOM_ID) -> str:
    return snapshot_name(generation, when, random_id)


def _open(fake: FakeDrive, **kwargs: object) -> GoogleDriveAdapter:
    settings: dict = {
        "sleep": lambda delay: None,
        "retry_delays": (),
        "list_delays": (),
        "list_attempts": 1,
    }
    settings.update(kwargs)
    return GoogleDriveAdapter(FOLDER_ID, service=fake.service(), **settings)


def _methods(fake: FakeDrive) -> list[str]:
    return [call["method"] for call in fake.calls]


def test_snapshot_names_carry_no_finance_details() -> None:
    name = _name()
    assert name == "finance_snapshot_g000001_20261022T094100Z_abc123abc123.enc"
    assert generation_of_name(name) == 1
    assert is_app_file_name(name) is True
    assert is_app_file_name(LOCK_FILE_NAME) is True
    assert is_app_file_name("notes.txt") is False
    assert is_app_file_name("finance_snapshot_g.enc") is False


@pytest.mark.parametrize(
    ("generation", "when", "random_id"),
    [
        (0, WHEN, RANDOM_ID),
        (1, datetime(2026, 10, 22, 9, 41), RANDOM_ID),
        (1, WHEN, "short"),
        (True, WHEN, RANDOM_ID),
    ],
)
def test_snapshot_name_rejects_unusable_parts(generation, when, random_id) -> None:
    with pytest.raises(DriveError):
        snapshot_name(generation, when, random_id)


def test_classify_keeps_both_files_and_picks_the_earlier_one() -> None:
    earlier = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
    later = earlier + timedelta(minutes=5)

    def item(file_id: str, when: datetime, random_id: str) -> DriveFile:
        return DriveFile(
            file_id=file_id,
            name=_name(when=when, random_id=random_id),
            parents=(FOLDER_ID,),
            owners=(DriveOwner("A", "a@example.com"),),
            can_trash=True,
            created_time=when,
            md5_checksum="",
            trashed=False,
        )

    older = item("older-file-0123456789ab", earlier, "aaaaaaaaaaaa")
    newer = item("newer-file-0123456789ab", later, "bbbbbbbbbbbb")
    found = classify_generation([newer, older], 1)
    assert found.conflict is True
    assert found.current == older
    assert found.others == (newer,)
    tied_a = item("aaaa-file-id-012345678", earlier, "cccccccccccc")
    tied_b = item("bbbb-file-id-012345678", earlier, "dddddddddddd")
    tied = classify_generation([tied_b, tied_a], 1)
    assert tied.current == tied_a
    assert tied.others == (tied_b,)
    with pytest.raises(DriveError, match="not in the folder"):
        classify_generation([older], 2)


def test_adapter_source_does_not_delete_or_touch_finance_data() -> None:
    import finance.drive_adapter as drive_adapter

    text = Path(drive_adapter.__file__).read_text(encoding="utf-8")
    assert "PySide6" not in text
    assert "finance.store" not in text
    assert "finance_data" not in text
    assert ".delete(" not in text
    assert "print(" not in text


def test_refuses_a_bad_folder_id_or_two_services() -> None:
    fake = FakeDrive(FOLDER_ID)
    with pytest.raises(DriveError, match="folder id"):
        GoogleDriveAdapter("short", service=fake.service())
    with pytest.raises(DriveError, match="credentials or a stand-in"):
        GoogleDriveAdapter(FOLDER_ID)
    with pytest.raises(DriveError, match="credentials or a stand-in"):
        GoogleDriveAdapter(FOLDER_ID, credentials=object(), service=fake.service())
    assert fake.calls == []


def test_list_asks_only_for_children_of_the_configured_folder() -> None:
    fake = FakeDrive(FOLDER_ID)
    fake.add(NOTES_ID, "notes.txt", content=b"notes")
    fake.add(
        OUTSIDE_ID,
        _name(random_id="eeeeeeeeeeee"),
        parents=(OTHER_FOLDER,),
        content=b"outside",
    )
    kept = fake.add("kept-file-0123456789abc", _name(), content=SNAPSHOT_BYTES)
    trashed = fake.add(
        "trashed-file-0123456789a",
        _name(random_id="ffffffffffff"),
        content=b"old",
        trashed=True,
    )
    with _open(fake) as adapter:
        found = adapter.list_children()
    assert [item.file_id for item in found] == [kept.file_id]
    listed = fake.calls[-1]
    assert listed["method"] == "list"
    assert listed["q"] == EXPECTED_QUERY
    assert listed["pageSize"] == 100
    assert listed["supportsAllDrives"] is True
    assert listed["includeItemsFromAllDrives"] is True
    assert listed["num_retries"] == 0
    assert "corpora" not in listed
    for name in FILE_FIELD_NAMES:
        assert name in listed["fields"]
    assert "nextPageToken" in listed["fields"]
    assert "appProperties" not in listed["fields"]
    touched = {call.get("fileId") for call in fake.calls}
    assert OUTSIDE_ID not in touched
    assert NOTES_ID not in touched
    assert trashed.file_id not in touched
    assert fake.delete_calls == []


def test_a_response_that_ignores_the_query_is_still_filtered() -> None:
    fake = FakeDrive(FOLDER_ID)
    fake.honor_query = False
    fake.add(NOTES_ID, "notes.txt")
    fake.add(OUTSIDE_ID, _name(random_id="eeeeeeeeeeee"), parents=(OTHER_FOLDER,))
    fake.add("trashed-file-0123456789a", _name(random_id="ffffffffffff"), trashed=True)
    kept = fake.add("kept-file-0123456789abc", _name(), content=SNAPSHOT_BYTES)
    with _open(fake) as adapter:
        found = adapter.list_children()
    assert [item.file_id for item in found] == [kept.file_id]
    assert {call.get("fileId") for call in fake.calls if call["method"] != "list"} == {FOLDER_ID}


def test_list_follows_pages_and_stops_on_a_broken_cursor() -> None:
    fake = FakeDrive(FOLDER_ID)
    fake.page_size = 2
    ids = []
    for index, random_id in enumerate(("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc")):
        item = fake.add(
            f"page-file-{index}-0123456789",
            _name(random_id=random_id),
            created_time=WHEN + timedelta(minutes=index),
        )
        ids.append(item.file_id)
    with _open(fake) as adapter:
        found = adapter.list_children()
    assert {item.file_id for item in found} == set(ids)
    assert _methods(fake).count("list") == 2

    fake.calls.clear()
    fake.repeat_token = True
    with _open(fake) as adapter:
        with pytest.raises(DriveError, match="cannot read"):
            adapter.list_children()

    fake.repeat_token = False
    fake.endless_pages = True
    with _open(fake) as adapter:
        with pytest.raises(DriveError, match="too many"):
            adapter.list_children()
    assert fake.delete_calls == []


def test_incomplete_listing_is_retried() -> None:
    slept: list[float] = []
    fake = FakeDrive(FOLDER_ID)
    fake.incomplete_lists = 1
    fake.add("kept-file-0123456789abc", _name(), content=SNAPSHOT_BYTES)
    with _open(fake, retry_delays=(0,), sleep=slept.append) as adapter:
        found = adapter.list_children()
    assert len(found) == 1
    assert _methods(fake).count("list") == 2
    assert slept == [0]


def test_upload_download_update_and_trash_round_trip() -> None:
    legacy = default_data_path()
    before = legacy.stat().st_mtime_ns if legacy.exists() else None
    fake = FakeDrive(FOLDER_ID)
    with _open(fake) as adapter:
        uploaded = adapter.upload_snapshot(1, SNAPSHOT_BYTES, random_id=RANDOM_ID, when=WHEN)
        downloaded = adapter.download(uploaded.file.file_id)
        updated = adapter.update_content(uploaded.file.file_id, b"synthetic-snapshot-v2")
        replaced = adapter.download(uploaded.file.file_id)
        result = adapter.trash(uploaded.file.file_id)
        remaining = adapter.list_children()
    assert downloaded == SNAPSHOT_BYTES
    assert replaced == b"synthetic-snapshot-v2"
    assert updated.md5_checksum == hashlib.md5(b"synthetic-snapshot-v2").hexdigest()
    assert uploaded.file.name == _name()
    assert uploaded.file.generation == 1
    assert uploaded.file.parents == (FOLDER_ID,)
    assert uploaded.file.owners[0].email == "a@example.com"
    assert uploaded.file.can_trash is True
    assert uploaded.file.md5_checksum == hashlib.md5(SNAPSHOT_BYTES).hexdigest()
    assert uploaded.file.file_id in {item.file_id for item in uploaded.listing.files}
    assert result.trashed is True
    assert result.status_text == "trashed"
    assert remaining == ()
    created = next(call for call in fake.calls if call["method"] == "create")
    assert created["body"] == {"name": _name(), "parents": [FOLDER_ID]}
    assert created["content"] == SNAPSHOT_BYTES
    assert created["fields"] == FILE_FIELDS
    assert "appProperties" not in created["body"]
    trashed = next(
        call for call in fake.calls if call["method"] == "update" and call.get("body")
    )
    assert trashed["body"] == {"trashed": True}
    assert "content" not in trashed
    assert "appProperties" not in trashed
    content_update = next(
        call for call in fake.calls if call["method"] == "update" and "content" in call
    )
    assert "body" not in content_update
    assert "appProperties" not in content_update
    assert fake.delete_calls == []
    assert all(call["num_retries"] == 0 for call in fake.calls)
    after = legacy.stat().st_mtime_ns if legacy.exists() else None
    assert before == after


def test_the_other_account_can_update_but_not_trash() -> None:
    fake = FakeDrive(FOLDER_ID)
    item = fake.add(
        "theirs-file-0123456789ab",
        _name(),
        content=SNAPSHOT_BYTES,
        can_trash=False,
        email="b@example.com",
        display_name="Account B",
    )
    with _open(fake) as adapter:
        changed = adapter.update_content(item.file_id, b"updated-by-editor")
        skipped = adapter.trash(item.file_id)
        downloaded = adapter.download(item.file_id)
    assert changed.can_trash is False
    assert skipped.trashed is False
    assert skipped.skipped_not_owner is True
    assert skipped.status_text == NOT_OWNER_SKIPPED
    assert downloaded == b"updated-by-editor"
    assert fake.files[item.file_id].trashed is False
    assert not any(
        call["method"] == "update" and call.get("body") == {"trashed": True}
        for call in fake.calls
    )
    assert fake.delete_calls == []


def test_trash_refused_by_drive_is_a_skip() -> None:
    fake = FakeDrive(FOLDER_ID)
    item = fake.add("owned-file-0123456789abc", _name(), content=SNAPSHOT_BYTES)
    fake.refuse_trash_ids.add(item.file_id)
    with _open(fake) as adapter:
        skipped = adapter.trash(item.file_id)
    assert skipped.status_text == NOT_OWNER_SKIPPED
    assert fake.files[item.file_id].trashed is False
    assert any(call["method"] == "update" for call in fake.calls)
    assert fake.delete_calls == []


def test_files_outside_the_pattern_are_not_changed() -> None:
    fake = FakeDrive(FOLDER_ID)
    notes = fake.add(NOTES_ID, "notes.txt", content=b"leave-me")
    outside = fake.add(
        OUTSIDE_ID,
        _name(),
        parents=(OTHER_FOLDER,),
        content=b"outside",
    )
    with _open(fake) as adapter:
        with pytest.raises(DriveError, match="not a FinanceTracker"):
            adapter.trash(notes.file_id)
        with pytest.raises(DriveError, match="not in the configured"):
            adapter.download(outside.file_id)
    assert fake.files[notes.file_id].content == b"leave-me"
    assert fake.files[outside.file_id].content == b"outside"
    assert not any(call["method"] in {"update", "get_media", "delete"} for call in fake.calls)


def test_lock_is_read_and_updated_by_id_and_not_duplicated() -> None:
    fake = FakeDrive(FOLDER_ID)
    with _open(fake) as adapter:
        created = adapter.create_lock(LOCK_BYTES)
        assert adapter.read_lock(created.file.file_id) == LOCK_BYTES
        adapter.update_lock(created.file.file_id, LOCK_BYTES + b"\n")
        assert adapter.read_lock(created.file.file_id) == LOCK_BYTES + b"\n"
        with pytest.raises(DriveError, match="already"):
            adapter.create_lock(LOCK_BYTES)
    creates = [call for call in fake.calls if call["method"] == "create"]
    assert len(creates) == 1
    assert creates[0]["body"]["name"] == LOCK_FILE_NAME
    assert all("appProperties" not in call.get("body", {}) for call in fake.calls)
    assert fake.delete_calls == []


def test_update_lock_refuses_a_snapshot() -> None:
    fake = FakeDrive(FOLDER_ID)
    item = fake.add("snap-file-0123456789abc", _name(), content=SNAPSHOT_BYTES)
    with _open(fake) as adapter:
        with pytest.raises(DriveError, match="not the lock"):
            adapter.update_lock(item.file_id, LOCK_BYTES)
        with pytest.raises(DriveError, match="not the lock"):
            adapter.read_lock(item.file_id)
    assert fake.files[item.file_id].content == SNAPSHOT_BYTES
    assert not any(call["method"] == "get_media" for call in fake.calls)


def test_empty_and_huge_uploads_are_refused_before_create() -> None:
    fake = FakeDrive(FOLDER_ID)
    with _open(fake) as adapter:
        with pytest.raises(DriveError, match="empty"):
            adapter.upload_snapshot(1, b"")
        with pytest.raises(DriveError, match="8 MiB"):
            adapter.upload_snapshot(1, b"x" * (8 * 1024 * 1024 + 1))
    assert fake.calls == []


def test_api_errors_timeouts_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_body = SYNTHETIC_SECRET

    def listed(error: BaseException, **kwargs: object) -> FakeDrive:
        fake = FakeDrive(FOLDER_ID)
        fake.fail_list_remaining = 1
        fake.list_error = error
        return fake

    fake = listed(http_error(401, "authError", secret_body))
    with _open(fake, retry_delays=(0, 0)) as adapter:
        with pytest.raises(DriveAuthError, match="Sign in again") as caught:
            adapter.list_children()
    assert_failure_omits_payload(caught.value)
    assert _methods(fake).count("list") == 1

    fake = listed(http_error(404, "notFound", secret_body))
    with _open(fake, retry_delays=(0, 0)) as adapter:
        with pytest.raises(DrivePermissionError, match="not found") as caught:
            adapter.list_children()
    assert_failure_omits_payload(caught.value)
    assert _methods(fake).count("list") == 1

    fake = listed(http_error(403, "insufficientFilePermissions", secret_body))
    with _open(fake, retry_delays=(0, 0)) as adapter:
        with pytest.raises(DrivePermissionError, match="Editor") as caught:
            adapter.list_children()
    assert_failure_omits_payload(caught.value)

    fake = listed(http_error(429, "userRateLimitExceeded", secret_body))
    fake.add("kept-file-0123456789abc", _name())
    slept: list[float] = []
    with _open(fake, retry_delays=(0,), sleep=slept.append) as adapter:
        assert len(adapter.list_children()) == 1
    assert slept == [0]
    assert _methods(fake).count("list") == 2

    fake = listed(http_error(403, "userRateLimitExceeded", secret_body))
    fake.add("kept-file-0123456789abc", _name())
    with _open(fake, retry_delays=(0,)) as adapter:
        assert len(adapter.list_children()) == 1

    fake = listed(TimeoutError(secret_body))
    with _open(fake) as adapter:
        with pytest.raises(DriveUnavailable, match="30 seconds") as caught:
            adapter.list_children()
    assert_failure_omits_payload(caught.value)

    fake = listed(ConnectionError(secret_body))
    with _open(fake) as adapter:
        with pytest.raises(DriveUnavailable, match="could not be reached") as caught:
            adapter.list_children()
    assert_failure_omits_payload(caught.value)

    fake = FakeDrive(FOLDER_ID)
    fake.fail_get_ids.add(FOLDER_ID)
    fake.get_error = http_error(404, "notFound", secret_body)
    with _open(fake) as adapter:
        with pytest.raises(DrivePermissionError, match="saved Drive folder") as caught:
            adapter.describe_folder()
    assert_failure_omits_payload(caught.value)
    assert "create" not in _methods(fake)


def test_upload_timeout_is_not_success_and_nothing_is_trashed() -> None:
    fake = FakeDrive(FOLDER_ID)
    fake.lose_create_response = True
    fake.lost_create_message = SYNTHETIC_SECRET
    with _open(fake) as adapter:
        with pytest.raises(DriveUploadUncertain, match="did not return a file id") as caught:
            adapter.upload_snapshot(1, SNAPSHOT_BYTES, random_id=RANDOM_ID, when=WHEN)
    assert caught.value.file_id is None
    assert_failure_omits_payload(caught.value)
    assert _methods(fake).count("create") == 1
    assert "list" not in _methods(fake)
    assert "update" not in _methods(fake)
    assert fake.delete_calls == []
    stored = list(fake.files.values())
    assert len(stored) == 1
    assert stored[0].trashed is False
    assert stored[0].content == SNAPSHOT_BYTES

    fake = FakeDrive(FOLDER_ID)
    file_id = "created-id-0123456789ab"
    fake.next_id = file_id
    fake.fail_get_ids.add(file_id)
    fake.get_error = TimeoutError(SYNTHETIC_SECRET)
    with _open(fake) as adapter:
        with pytest.raises(DriveUploadUncertain, match=file_id) as caught:
            adapter.upload_snapshot(1, SNAPSHOT_BYTES, random_id=RANDOM_ID, when=WHEN)
    assert caught.value.file_id == file_id
    assert_failure_omits_payload(caught.value)
    assert "list" not in _methods(fake)
    assert fake.files[file_id].trashed is False
    assert fake.delete_calls == []


def test_update_timeout_is_confirmed_only_when_the_bytes_match() -> None:
    fake = FakeDrive(FOLDER_ID)
    item = fake.add("snap-file-0123456789abc", _name(), content=SNAPSHOT_BYTES)
    fake.update_error = TimeoutError(SYNTHETIC_SECRET)
    with _open(fake) as adapter:
        with pytest.raises(DriveUploadUncertain, match="did not finish") as caught:
            adapter.update_content(item.file_id, b"replacement-bytes")
    assert_failure_omits_payload(caught.value)
    assert fake.files[item.file_id].content == SNAPSHOT_BYTES
    assert fake.files[item.file_id].trashed is False
    assert _methods(fake).count("update") == 1
    assert fake.delete_calls == []

    fake.update_error = TimeoutError(SYNTHETIC_SECRET)
    fake.apply_update_before_error = True
    with _open(fake) as adapter:
        updated = adapter.update_content(item.file_id, b"replacement-bytes")
    assert updated.file_id == item.file_id
    assert fake.files[item.file_id].content == b"replacement-bytes"
    assert _methods(fake).count("update") == 2


def test_listing_lag_still_confirms_the_upload_by_id() -> None:
    slept: list[float] = []
    fake = FakeDrive(FOLDER_ID)
    fake.hide_created = True
    with _open(
        fake,
        list_attempts=3,
        list_delays=(0, 0),
        sleep=slept.append,
    ) as adapter:
        uploaded = adapter.upload_snapshot(1, SNAPSHOT_BYTES, random_id=RANDOM_ID, when=WHEN)
        found = classify_generation(uploaded.listing.files, 1)
    assert uploaded.file.file_id in uploaded.listing.included_by_get
    assert found.current.file_id == uploaded.file.file_id
    assert found.conflict is False
    gets = [
        call
        for call in fake.calls
        if call["method"] == "get" and call.get("fileId") == uploaded.file.file_id
    ]
    assert len(gets) == 1
    assert gets[0]["fields"] == FILE_FIELDS
    assert _methods(fake).count("list") == 3
    assert slept == [0, 0]
    assert fake.delete_calls == []
    assert fake.files[uploaded.file.file_id].trashed is False


def test_a_hidden_known_id_is_loaded_with_files_get() -> None:
    fake = FakeDrive(FOLDER_ID)
    item = fake.add("hidden-file-0123456789a", _name(), content=SNAPSHOT_BYTES)
    fake.hidden_ids.add(item.file_id)
    with _open(fake, list_attempts=2, list_delays=(0,)) as adapter:
        listing = adapter.list_for_generation([item.file_id])
    assert listing.included_by_get == (item.file_id,)
    assert listing.files[0].file_id == item.file_id
    assert any(
        call["method"] == "get" and call.get("fileId") == item.file_id for call in fake.calls
    )


def test_same_generation_conflict_keeps_both_files() -> None:
    fake = FakeDrive(FOLDER_ID)
    earlier = WHEN
    later = WHEN + timedelta(minutes=1)
    older = fake.add(
        "older-file-0123456789ab",
        _name(when=earlier, random_id="aaaaaaaaaaaa"),
        created_time=earlier,
    )
    newer = fake.add(
        "newer-file-0123456789ab",
        _name(when=later, random_id="bbbbbbbbbbbb"),
        created_time=later,
    )
    with _open(fake) as adapter:
        found = classify_generation(adapter.list_children(), 1)
    assert found.conflict is True
    assert found.current.file_id == older.file_id
    assert found.others[0].file_id == newer.file_id
    assert fake.delete_calls == []
    assert "update" not in _methods(fake)


def test_server_time_comes_from_the_date_header(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> datetime:
        raise AssertionError("local clock")

    monkeypatch.setattr("finance.drive_adapter._utc_now", explode)
    fake = FakeDrive(FOLDER_ID)
    with _open(fake) as adapter:
        when = adapter.server_time()
    assert when == datetime(2026, 10, 22, 9, 41, tzinfo=timezone.utc)

    fake.date_header = None
    with _open(fake) as adapter:
        with pytest.raises(DriveError, match="server time"):
            adapter.server_time()


def test_server_time_asks_drive_again_when_the_header_changes() -> None:
    fake = FakeDrive(FOLDER_ID)
    with _open(fake) as adapter:
        first = adapter.server_time()
        fake.date_header = "Wed, 22 Oct 2026 09:44:00 GMT"
        second = adapter.server_time()
    assert first == datetime(2026, 10, 22, 9, 41, tzinfo=timezone.utc)
    assert second == datetime(2026, 10, 22, 9, 44, tzinfo=timezone.utc)
    assert sum(1 for call in fake.calls if call["method"] == "get") >= 2


def test_a_saved_shortcut_is_rejected_and_a_pasted_shortcut_is_followed() -> None:
    fake = FakeDrive(FOLDER_ID)
    fake.folder_mime = SHORTCUT_MIME
    with _open(fake) as adapter:
        with pytest.raises(DriveError, match="shortcut"):
            adapter.list_children()
    assert "list" not in _methods(fake)

    fake = FakeDrive(FOLDER_ID)
    fake.folder_mime = FOLDER_MIME
    fake.aliases[SHORTCUT_ID] = {
        "id": SHORTCUT_ID,
        "name": "link",
        "mimeType": SHORTCUT_MIME,
        "trashed": False,
        "shortcutDetails": {"targetId": FOLDER_ID},
    }
    with _open(fake) as adapter:
        folder = adapter.resolve_shared_folder(SHORTCUT_ID)
    assert folder.folder_id == FOLDER_ID
    assert folder.name == "FinanceTracker test"
    assert folder.owner_email == "a@example.com"
    assert folder.followed_shortcut is True
    gets = [call for call in fake.calls if call["method"] == "get"]
    assert [call["fileId"] for call in gets] == [SHORTCUT_ID, FOLDER_ID]
    assert gets[0]["fields"] == FOLDER_LOOKUP_FIELDS
    assert "update" not in _methods(fake)
    assert fake.delete_calls == []


def test_drive_calls_run_one_at_a_time_on_the_worker() -> None:
    fake = FakeDrive(FOLDER_ID)
    fake.pause_methods.add("list")
    threads: list[int] = []

    def before(method: str) -> None:
        if method == "list":
            threads.append(threading.get_ident())

    fake.before_execute = before
    adapter = _open(fake)

    def run() -> None:
        adapter.list_children()

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    try:
        first.start()
        assert fake.entered.wait(5)
        second.start()
        time.sleep(0.2)
        assert fake.active == 1
        assert _methods(fake).count("list") == 1
        assert adapter.max_concurrent_calls == 1
    finally:
        fake.release.set()
    first.join(5)
    second.join(5)
    adapter.close()
    assert not first.is_alive()
    assert not second.is_alive()
    assert fake.peak == 1
    assert adapter.max_concurrent_calls == 1
    assert _methods(fake).count("list") == 2
    assert threads
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()
    with pytest.raises(DriveError, match="closed"):
        adapter.list_children()


def test_the_real_service_builder_runs_on_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDrive(FOLDER_ID)
    seen: dict = {}

    def build(credentials: object) -> object:
        seen["thread"] = threading.get_ident()
        seen["credentials"] = credentials
        return fake.service()

    monkeypatch.setattr("finance.drive_adapter.build_drive_service", build)
    credentials = object()
    with GoogleDriveAdapter(
        FOLDER_ID,
        credentials=credentials,
        sleep=lambda delay: None,
        retry_delays=(),
        list_delays=(),
    ) as adapter:
        adapter.describe_folder()
    assert seen["credentials"] is credentials
    assert seen["thread"] != threading.get_ident()
