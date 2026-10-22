"""Drive folder links and shortcut resolution. No network."""

from __future__ import annotations

import pytest

from finance.drive_folder import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    FolderSetupError,
    parse_drive_item_id,
    resolve_folder,
)
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

FOLDER_ID = "folder-id-0123456789abc"
OTHER_ID = "other-folder-0123456789a"
SHORTCUT_ID = "shortcut-id-0123456789ab"


def _folder(file_id: str = FOLDER_ID, **overrides: object) -> dict:
    resource = {
        "id": file_id,
        "name": "Shared Test Folder",
        "mimeType": FOLDER_MIME,
        "trashed": False,
        "owners": [
            {"displayName": "Test Owner", "emailAddress": "owner@example.com"}
        ],
    }
    resource.update(overrides)
    return resource


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        (FOLDER_ID, FOLDER_ID),
        (f"  {FOLDER_ID}  ", FOLDER_ID),
        (f'"{FOLDER_ID}"', FOLDER_ID),
        (f"https://drive.google.com/drive/folders/{FOLDER_ID}", FOLDER_ID),
        (
            f"https://drive.google.com/drive/u/0/folders/{FOLDER_ID}?usp=sharing",
            FOLDER_ID,
        ),
        (f"https://drive.google.com/open?id={FOLDER_ID}", FOLDER_ID),
        (f"https://drive.google.com/folderview?id={FOLDER_ID}&usp=sharing", FOLDER_ID),
        (f"https://drive.google.com/file/d/{FOLDER_ID}/view", FOLDER_ID),
        (f"https://docs.google.com/document/d/{FOLDER_ID}/edit", FOLDER_ID),
        (f"drive.google.com/drive/folders/{FOLDER_ID}", FOLDER_ID),
    ],
)
def test_parse_drive_item_id(pasted: str, expected: str) -> None:
    assert parse_drive_item_id(pasted) == expected


@pytest.mark.parametrize(
    "pasted",
    ["", "   ", "short", "https://example.com/folders/" + FOLDER_ID, "not a link"],
)
def test_parse_rejects_unusable_text(pasted: str) -> None:
    with pytest.raises(FolderSetupError):
        parse_drive_item_id(pasted)


def test_resolve_folder_reads_the_folder_once() -> None:
    seen: list[str] = []

    def read(file_id: str) -> dict:
        seen.append(file_id)
        return _folder(file_id)

    folder = resolve_folder(
        f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing",
        read,
    )

    assert seen == [FOLDER_ID]
    assert folder.folder_id == FOLDER_ID
    assert folder.name == "Shared Test Folder"
    assert folder.owner_display_name == "Test Owner"
    assert folder.owner_email == "owner@example.com"
    assert folder.followed_shortcut is False


def test_resolve_follows_a_shortcut_and_uses_the_target_id() -> None:
    seen: list[str] = []

    def read(file_id: str) -> dict:
        seen.append(file_id)
        if file_id == SHORTCUT_ID:
            return {
                "id": SHORTCUT_ID,
                "name": "link",
                "mimeType": SHORTCUT_MIME,
                "trashed": False,
                "shortcutDetails": {"targetId": FOLDER_ID},
            }
        return _folder(FOLDER_ID)

    folder = resolve_folder(SHORTCUT_ID, read)

    assert seen == [SHORTCUT_ID, FOLDER_ID]
    assert folder.folder_id == FOLDER_ID
    assert folder.followed_shortcut is True


def test_resolve_uses_the_id_drive_returns() -> None:
    def read(file_id: str) -> dict:
        assert file_id == FOLDER_ID
        return _folder(OTHER_ID)

    folder = resolve_folder(FOLDER_ID, read)
    assert folder.folder_id == OTHER_ID


def test_shortcut_loop_fails() -> None:
    def read(file_id: str) -> dict:
        return {
            "id": file_id,
            "name": "loop",
            "mimeType": SHORTCUT_MIME,
            "shortcutDetails": {"targetId": file_id},
        }

    with pytest.raises(FolderSetupError, match="itself"):
        resolve_folder(SHORTCUT_ID, read)


def test_shortcut_without_a_target_fails() -> None:
    def read(file_id: str) -> dict:
        return {"id": file_id, "name": "link", "mimeType": SHORTCUT_MIME}

    with pytest.raises(FolderSetupError, match="no target"):
        resolve_folder(SHORTCUT_ID, read)


def test_long_shortcut_chain_fails() -> None:
    ids = [f"shortcut-hop-{index:02d}-012345" for index in range(6)]

    def read(file_id: str) -> dict:
        index = ids.index(file_id)
        return {
            "id": file_id,
            "name": "link",
            "mimeType": SHORTCUT_MIME,
            "shortcutDetails": {"targetId": ids[index + 1]},
        }

    with pytest.raises(FolderSetupError, match="too long"):
        resolve_folder(ids[0], read)


def test_trashed_folder_fails() -> None:
    def read(file_id: str) -> dict:
        return _folder(file_id, trashed=True)

    with pytest.raises(FolderSetupError, match="trash"):
        resolve_folder(FOLDER_ID, read)


def test_non_folder_fails() -> None:
    def read(file_id: str) -> dict:
        return _folder(file_id, mimeType="application/vnd.google-apps.document")

    with pytest.raises(FolderSetupError, match="not a folder"):
        resolve_folder(FOLDER_ID, read)


def test_missing_name_fails() -> None:
    def read(file_id: str) -> dict:
        return _folder(file_id, name="  ")

    with pytest.raises(FolderSetupError, match="folder name"):
        resolve_folder(FOLDER_ID, read)


def test_missing_owner_is_blank() -> None:
    def read(file_id: str) -> dict:
        resource = _folder(file_id)
        resource["owners"] = []
        return resource

    folder = resolve_folder(FOLDER_ID, read)
    assert folder.owner_display_name == ""
    assert folder.owner_email == ""


def test_reader_errors_do_not_include_the_backend_message() -> None:
    def read(file_id: str) -> dict:
        raise RuntimeError(SYNTHETIC_SECRET)

    with pytest.raises(FolderSetupError) as caught:
        resolve_folder(FOLDER_ID, read)
    assert "Could not read that Drive item" in str(caught.value)
    assert_failure_omits_payload(caught.value)


def test_folder_setup_error_from_the_reader_is_kept() -> None:
    def read(file_id: str) -> dict:
        raise FolderSetupError("That folder was not found, or this Google account cannot see it.")

    with pytest.raises(FolderSetupError, match="not found"):
        resolve_folder(FOLDER_ID, read)
