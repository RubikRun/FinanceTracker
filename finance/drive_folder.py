"""Parse a pasted Drive link and resolve it to a real folder.

Google sometimes hands out a shortcut (``application/vnd.google-apps.shortcut``)
instead of the folder itself. Setup follows ``shortcutDetails.targetId`` and
then shows the target folder's name and owner. The metadata reader is injected
so tests do not call Google. The setup command supplies a reader that uses
``files.get``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_DRIVE_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_MAX_READS = 4
_DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}


class FolderSetupError(Exception):
    """The pasted link cannot be used. The message is safe to show."""


@dataclass(frozen=True)
class DriveFolder:
    """A folder the user can confirm. No finance data."""

    folder_id: str
    name: str
    owner_display_name: str
    owner_email: str
    followed_shortcut: bool


def is_drive_item_id(value: object) -> bool:
    return isinstance(value, str) and _DRIVE_ITEM_ID.fullmatch(value) is not None


def parse_drive_item_id(value: str) -> str:
    """Return the Drive item id from a bare id or a Drive URL."""
    text = normalize_pasted_reference(value)
    if not text:
        raise FolderSetupError("Paste the shared folder link or the folder ID.")
    if "://" not in text and "/" not in text and "?" not in text:
        if is_drive_item_id(text):
            return text
        raise FolderSetupError("That is not a Google Drive folder link or folder ID.")
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _DRIVE_HOSTS:
        raise FolderSetupError("That link is not a Google Drive link.")
    parts = [part for part in parsed.path.split("/") if part]
    item_id = _id_from_path(parts)
    if item_id is None:
        raw = parse_qs(parsed.query).get("id", [""])[0]
        if is_drive_item_id(raw):
            item_id = raw
    if item_id is None:
        raise FolderSetupError("Could not find a folder ID in that link.")
    return item_id


def normalize_pasted_reference(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def resolve_folder(
    pasted: str,
    read_metadata: Callable[[str], Mapping[str, object]],
) -> DriveFolder:
    """Resolve ``pasted`` to a folder, following a shortcut when needed.

    ``read_metadata`` receives a Drive item id and returns the ``files.get``
    resource. A shortcut is followed at most a few times. A loop, a trashed
    item, or anything that is not a folder is rejected.
    """
    file_id = parse_drive_item_id(pasted)
    followed = False
    seen: set[str] = set()
    for _ in range(_MAX_READS):
        if file_id in seen:
            raise FolderSetupError("That Drive shortcut points at itself.")
        seen.add(file_id)
        meta = _read(read_metadata, file_id)
        if meta.get("trashed") is True:
            raise FolderSetupError("That Drive item is in the trash.")
        mime = meta.get("mimeType")
        if mime == SHORTCUT_MIME:
            file_id = _shortcut_target(meta)
            followed = True
            continue
        if mime != FOLDER_MIME:
            raise FolderSetupError(
                "That link is not a folder. Paste the shared FinanceTracker folder link."
            )
        return _folder_from_metadata(meta, followed_shortcut=followed)
    raise FolderSetupError("That Drive shortcut chain is too long.")


def _id_from_path(parts: list[str]) -> str | None:
    for marker in ("folders", "d"):
        if marker not in parts:
            continue
        index = parts.index(marker)
        if index + 1 < len(parts) and is_drive_item_id(parts[index + 1]):
            return parts[index + 1]
    return None


def _read(
    read_metadata: Callable[[str], Mapping[str, object]],
    file_id: str,
) -> Mapping[str, object]:
    try:
        meta = read_metadata(file_id)
    except FolderSetupError:
        raise
    except Exception:
        raise FolderSetupError(
            "Could not read that Drive item. Check the link, sharing, and network, then try again."
        ) from None
    if not isinstance(meta, Mapping):
        raise FolderSetupError("Drive did not return that item.")
    return meta


def _shortcut_target(meta: Mapping[str, object]) -> str:
    details = meta.get("shortcutDetails")
    target = ""
    if isinstance(details, Mapping):
        raw = details.get("targetId")
        if isinstance(raw, str):
            target = raw
    if not is_drive_item_id(target):
        raise FolderSetupError("That Drive shortcut has no target.")
    return target


def _folder_from_metadata(
    meta: Mapping[str, object],
    *,
    followed_shortcut: bool,
) -> DriveFolder:
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise FolderSetupError("Drive did not return the folder name.")
    folder_id = meta.get("id")
    if not is_drive_item_id(folder_id):
        raise FolderSetupError("Drive did not return a folder ID.")
    display_name, email = _owner(meta)
    return DriveFolder(
        folder_id=folder_id,
        name=name.strip(),
        owner_display_name=display_name,
        owner_email=email,
        followed_shortcut=followed_shortcut,
    )


def _owner(meta: Mapping[str, object]) -> tuple[str, str]:
    owners = meta.get("owners")
    if not isinstance(owners, list) or not owners:
        return "", ""
    owner = owners[0]
    if not isinstance(owner, Mapping):
        return "", ""
    display = owner.get("displayName")
    email = owner.get("emailAddress")
    display_text = display.strip() if isinstance(display, str) else ""
    email_text = email.strip() if isinstance(email, str) else ""
    return display_text, email_text
