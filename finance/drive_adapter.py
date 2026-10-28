"""Google Drive operations for one configured folder.

The GUI does not call this module. Cloud mode stays off. Nothing here reads
or writes the local finance file, and nothing here stores a passphrase or a
refresh token.

``GoogleDriveAdapter`` is the only object that talks to Drive. A later
session layer can be given that object or any stand-in with the same
methods. Tests pass a fake Drive service; they do not open a network
connection.

Every service call runs on one background thread, one call at a time.
``httplib2`` is not safe to share across threads, and its own default
timeout is 60 seconds, so the service is built on that thread with the
30 second timeout from ``finance.google_setup``. A timed-out upload is not
reported as saved. If Drive returns a file id and then the connection drops,
the adapter reads that id back. It does not look through a folder listing to
decide that the upload failed, and it does not trash or replace anything it
could not confirm.

Listings ask only for direct children of the configured folder, with
``trashed = false`` and explicit fields. Before a download, content update,
or trash, the adapter reads the file again by id and requires all of:

- the file is not in the trash
- the configured folder is a parent
- the name is ``finance_lock.json`` or ``finance_snapshot_g*.enc``

Trash is ``files.update`` with ``trashed`` set. There is no permanent
delete. A file this account cannot trash (``capabilities.canTrash`` is
false, or Drive refuses) is reported as ``not owner — skipped`` and is left
in place. Lock updates write the file's bytes. They do not write
``appProperties``.

Drive's folder listing can lag a few seconds behind ``files.create``. A
listing used after a create is retried, then any id this session just
confirmed with ``files.get`` is added to the result. A missing listing row
does not undo that confirmation. ``classify_generation`` can then see two
files with the same generation; it keeps both and picks the earlier Drive
``createdTime`` as the current one.

Server time comes from the HTTP ``Date`` header of a Drive response. This
computer's clock is not used for that. ``server_time()`` asks Drive on every
call, so a later check cannot keep a clock reading from before the computer
slept.

Shortcut folder ids are resolved by ``resolve_shared_folder`` (it follows
``shortcutDetails.targetId``). The id saved for later calls must already be
the real folder. A saved shortcut is rejected until setup is run again.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from .drive_folder import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    DriveFolder,
    FolderSetupError,
    is_drive_item_id,
    resolve_folder,
)
from .google_setup import (
    FOLDER_LOOKUP_FIELDS,
    REQUEST_TIMEOUT_SECONDS,
    build_drive_service,
)

LOCK_FILE_NAME = "finance_lock.json"
SNAPSHOT_PREFIX = "finance_snapshot_g"
NOT_OWNER_SKIPPED = "not owner — skipped"

FILE_FIELDS = (
    "id,name,parents,"
    "owners(displayName,emailAddress),"
    "capabilities(canTrash),"
    "createdTime,md5Checksum,trashed"
)
LIST_FIELDS = f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"

PAGE_SIZE = 100
MAX_LIST_PAGES = 20
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_DEFAULT_DELAYS = (0.5, 1.0, 2.0)
_LIST_ATTEMPTS = 4

_SNAPSHOT_NAME = re.compile(
    r"^finance_snapshot_g(?P<generation>[0-9]{6})_"
    r"[0-9]{8}T[0-9]{6}Z_"
    r"[0-9a-f]{12}\.enc$"
)
_RANDOM_ID = re.compile(r"^[0-9a-f]{12}$")
_REASON = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_RATE_REASONS = {"userRateLimitExceeded", "rateLimitExceeded", "backendError"}
_AUTH_REASONS = {"authError", "invalidCredentials", "unauthorized"}

_TIMEOUT_MESSAGE = (
    f"Google Drive did not answer within {REQUEST_TIMEOUT_SECONDS} seconds. "
    "Check the network and try again."
)
_NETWORK_MESSAGE = "Google Drive could not be reached. Check the network and try again."
_RATE_MESSAGE = "Google Drive is limiting requests. Wait a moment and try again."
_AUTH_MESSAGE = "Google rejected the saved sign-in. Sign in again in the browser."
_FOLDER_DENIED = (
    "This Google account cannot open the saved Drive folder. "
    "Share the folder as Editor, or run python -m finance.google_setup again."
)
_UNCONFIRMED_NO_ID = (
    "The upload did not finish, and Drive did not return a file id. "
    "It was not confirmed. Nothing was trashed or replaced."
)


class DriveError(Exception):
    """Drive could not complete the operation. The message is safe to show."""


class DriveAuthError(DriveError):
    """The saved Google sign-in was rejected."""


class DrivePermissionError(DriveError):
    """This account cannot see or change that Drive item."""


class DriveUnavailable(DriveError):
    """The network failed, the call timed out, or Drive asked us to back off."""


class DriveUploadUncertain(DriveError):
    """The write was not confirmed. Nothing was trashed or replaced.

    ``file_id`` is set when Drive returned one and the following read did not
    finish. It is ``None`` when no id came back.
    """

    def __init__(self, message: str, file_id: str | None = None) -> None:
        super().__init__(message)
        self.file_id = file_id


class _RetryableDriveError(DriveUnavailable):
    """A failure a read can try again. Callers outside this module do not see it."""


@dataclass(frozen=True)
class DriveOwner:
    """A Drive file owner. This is a Google account, not a finance user."""

    display_name: str
    email: str


@dataclass(frozen=True)
class DriveFile:
    """Metadata for one snapshot or for the lock file. No file bytes."""

    file_id: str
    name: str
    parents: tuple[str, ...]
    owners: tuple[DriveOwner, ...]
    can_trash: bool
    created_time: datetime
    md5_checksum: str
    trashed: bool

    @property
    def generation(self) -> int | None:
        return generation_of_name(self.name)


@dataclass(frozen=True)
class GenerationListing:
    """Children of the configured folder, plus ids filled in from ``files.get``.

    ``included_by_get`` holds ids that were missing from the listing and were
    added because ``files.get`` had already confirmed them, or because a
    follow-up ``files.get`` succeeded. Their presence means the listing lag
    is not a failed save.
    """

    files: tuple[DriveFile, ...]
    included_by_get: tuple[str, ...]


@dataclass(frozen=True)
class UploadedFile:
    """A file that was created and then read back by id."""

    file: DriveFile
    listing: GenerationListing


@dataclass(frozen=True)
class TrashResult:
    """What a trash attempt did. ``skipped_not_owner`` is not a failed save."""

    file_id: str
    trashed: bool
    skipped_not_owner: bool

    @property
    def status_text(self) -> str:
        if self.skipped_not_owner:
            return NOT_OWNER_SKIPPED
        if self.trashed:
            return "trashed"
        return "not trashed"


@dataclass(frozen=True)
class GenerationConflict:
    """Files that share a generation number.

    ``current`` is the one with the earlier Drive ``createdTime``. ``others``
    are kept as well. Nothing in this result has been trashed.
    """

    generation: int
    current: DriveFile
    others: tuple[DriveFile, ...]

    @property
    def conflict(self) -> bool:
        return len(self.others) > 0


class DriveAdapter(Protocol):
    """The methods the session layer will call. Tests can substitute a fake."""

    def describe_folder(self) -> DriveFolder:
        """Name and owner of the configured folder."""

    def resolve_shared_folder(self, pasted: str) -> DriveFolder:
        """Resolve a pasted link or id, following a shortcut to its target folder."""

    def list_children(self) -> tuple[DriveFile, ...]:
        """Direct child snapshots and the lock file. Trashed items are omitted."""

    def list_for_generation(self, known_ids: Sequence[str] = ()) -> GenerationListing:
        """List, retry while a known id is missing, then union those ids."""

    def download(self, file_id: str) -> bytes:
        """Download one guarded file by id."""

    def upload_snapshot(
        self,
        generation: int,
        content: bytes,
        *,
        random_id: str | None = None,
        when: datetime | None = None,
    ) -> UploadedFile:
        """Upload a new snapshot, then confirm that file id."""

    def create_lock(self, content: bytes) -> UploadedFile:
        """Create the lock file. Refuses when one is already in the folder."""

    def read_lock(self, file_id: str) -> bytes:
        """Download the lock file by its cached id."""

    def update_lock(self, file_id: str, content: bytes) -> DriveFile:
        """Replace the lock file's bytes, then read them back."""

    def update_content(self, file_id: str, content: bytes) -> DriveFile:
        """Replace one guarded file's bytes, then read them back."""

    def trash(self, file_id: str) -> TrashResult:
        """Move one guarded file to the trash, or skip it when this account cannot."""

    def server_time(self) -> datetime:
        """Drive's clock from a fresh response's Date header."""

    def close(self) -> None:
        """Stop the background thread after the current call finishes."""


def snapshot_name(generation: int, when: datetime, random_id: str) -> str:
    """``finance_snapshot_g000123_<UTC-stamp>_<random>.enc``.

    The name carries a generation number, a UTC timestamp, and a random id.
    It does not carry amounts, categories, or notes.
    """
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise DriveError("A snapshot generation must be from 1 to 999999.")
    if not 1 <= generation <= 999999:
        raise DriveError("A snapshot generation must be from 1 to 999999.")
    if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
        raise DriveError("A snapshot time must include a time zone.")
    if not isinstance(random_id, str) or _RANDOM_ID.fullmatch(random_id) is None:
        raise DriveError("A snapshot name needs a 12-character random id.")
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{SNAPSHOT_PREFIX}{generation:06d}_{stamp}_{random_id}.enc"


def generation_of_name(name: str) -> int | None:
    """The generation in a snapshot name this app created, or None."""
    if not isinstance(name, str):
        return None
    match = _SNAPSHOT_NAME.fullmatch(name)
    if match is None:
        return None
    return int(match.group("generation"))


def is_app_file_name(name: object) -> bool:
    """True for the lock file and for ``finance_snapshot_g*.enc``."""
    if not isinstance(name, str) or len(name) > 180:
        return False
    if not name.isascii() or not name.isprintable():
        return False
    if name == LOCK_FILE_NAME:
        return True
    if not name.startswith(SNAPSHOT_PREFIX) or not name.endswith(".enc"):
        return False
    return bool(name[len(SNAPSHOT_PREFIX) : -len(".enc")])


def classify_generation(
    files: Sequence[DriveFile],
    generation: int,
) -> GenerationConflict:
    """Pick the earliest file of ``generation`` without dropping the others."""
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise DriveError("A snapshot generation must be a positive number.")
    matches = [item for item in files if item.generation == generation]
    if not matches:
        raise DriveError(
            f"Generation {generation} is not in the folder listing."
        )
    ordered = sorted(matches, key=lambda item: (item.created_time, item.file_id))
    return GenerationConflict(
        generation=generation,
        current=ordered[0],
        others=tuple(ordered[1:]),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GoogleDriveAdapter:
    """Drive v3 calls limited to one folder and one background thread."""

    def __init__(
        self,
        folder_id: str,
        *,
        credentials: Credentials | None = None,
        service: object | None = None,
        sleep: Callable[[float], None] | None = None,
        retry_delays: Sequence[float] = _DEFAULT_DELAYS,
        list_attempts: int = _LIST_ATTEMPTS,
        list_delays: Sequence[float] = _DEFAULT_DELAYS,
    ) -> None:
        if not is_drive_item_id(folder_id):
            raise DriveError("The saved Drive folder id cannot be used.")
        if (credentials is None) == (service is None):
            raise DriveError(
                "A Drive adapter needs Google credentials or a stand-in service."
            )
        if list_attempts < 1:
            raise DriveError("The folder listing needs at least one attempt.")
        self._folder_id = folder_id
        self._credentials = credentials
        self._injected_service = service
        self._service = None
        self._sleep = sleep or time.sleep
        self._retry_delays = tuple(retry_delays)
        self._list_attempts = list_attempts
        self._list_delays = tuple(list_delays)
        self._folder: DriveFolder | None = None
        self._server_time: datetime | None = None
        self._closed = False
        self._worker_ident: int | None = None
        self._active = 0
        self._peak = 0
        self._state = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="finance-drive",
        )

    def __enter__(self) -> GoogleDriveAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def max_concurrent_calls(self) -> int:
        """Highest number of Drive calls that were inside the worker at once."""
        return self._peak

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def describe_folder(self) -> DriveFolder:
        return self._call(self._ensure_folder)

    def resolve_shared_folder(self, pasted: str) -> DriveFolder:
        return self._call(self._resolve_shared_folder, pasted)

    def list_children(self) -> tuple[DriveFile, ...]:
        return self._call(self._list_children)

    def list_for_generation(self, known_ids: Sequence[str] = ()) -> GenerationListing:
        return self._call(self._list_for_generation, tuple(known_ids), {})

    def download(self, file_id: str) -> bytes:
        return self._call(self._download, file_id)

    def upload_snapshot(
        self,
        generation: int,
        content: bytes,
        *,
        random_id: str | None = None,
        when: datetime | None = None,
    ) -> UploadedFile:
        name = snapshot_name(
            generation,
            when or _utc_now(),
            random_id or secrets.token_hex(6),
        )
        return self._call(self._upload_named, name, content)

    def create_lock(self, content: bytes) -> UploadedFile:
        return self._call(self._create_lock, content)

    def read_lock(self, file_id: str) -> bytes:
        return self._call(self._read_lock, file_id)

    def update_lock(self, file_id: str, content: bytes) -> DriveFile:
        return self._call(self._update_lock, file_id, content)

    def update_content(self, file_id: str, content: bytes) -> DriveFile:
        return self._call(self._update_content, file_id, content)

    def trash(self, file_id: str) -> TrashResult:
        return self._call(self._trash, file_id)

    def server_time(self) -> datetime:
        return self._call(self._server_time_now)

    def _call(self, func: Callable, *args: object) -> object:
        if self._closed:
            raise DriveError("The Drive connection is closed.")
        if self._worker_ident is not None and threading.get_ident() == self._worker_ident:
            raise DriveError(
                "A Drive call tried to start another Drive call on the same connection."
            )
        future = self._executor.submit(self._run, func, args)
        return future.result()

    def _run(self, func: Callable, args: tuple[object, ...]) -> object:
        with self._state:
            self._worker_ident = threading.get_ident()
            self._active += 1
            self._peak = max(self._peak, self._active)
        try:
            return func(*args)
        finally:
            with self._state:
                self._active -= 1

    def _files(self):
        if self._service is None:
            if self._injected_service is not None:
                self._service = self._injected_service
            else:
                self._service = build_drive_service(self._credentials)
        return self._service.files()

    def _ensure_folder(self) -> DriveFolder:
        if self._folder is not None:
            return self._folder
        try:
            resource = self._execute_retrying(
                lambda: self._files().get(
                    fileId=self._folder_id,
                    fields=FOLDER_LOOKUP_FIELDS,
                    supportsAllDrives=True,
                )
            )
        except DrivePermissionError:
            raise DrivePermissionError(_FOLDER_DENIED) from None
        self._folder = _folder_from_resource(resource, self._folder_id)
        return self._folder

    def _resolve_shared_folder(self, pasted: str) -> DriveFolder:
        def read(file_id: str) -> dict:
            try:
                resource = self._execute_retrying(
                    lambda: self._files().get(
                        fileId=file_id,
                        fields=FOLDER_LOOKUP_FIELDS,
                        supportsAllDrives=True,
                    )
                )
            except DriveError as exc:
                raise FolderSetupError(str(exc)) from None
            if not isinstance(resource, dict):
                raise FolderSetupError("Drive did not return that item.")
            return resource

        return resolve_folder(pasted, read)

    def _list_children(self) -> tuple[DriveFile, ...]:
        return self._list_for_generation((), {}).files

    def _list_for_generation(
        self,
        known_ids: Sequence[str],
        remembered: Mapping[str, DriveFile],
    ) -> GenerationListing:
        ordered_ids: list[str] = []
        for file_id in known_ids:
            if file_id in ordered_ids:
                continue
            if not is_drive_item_id(file_id):
                raise DriveError("A Drive file id was not usable.")
            ordered_ids.append(file_id)
        attempts = self._list_attempts if ordered_ids else 1
        missing = list(ordered_ids)
        found: list[DriveFile] = []
        for attempt in range(attempts):
            found = self._list_pages()
            present = {item.file_id for item in found}
            missing = [file_id for file_id in ordered_ids if file_id not in present]
            if not missing:
                return GenerationListing(tuple(_sorted_files(found)), ())
            if attempt + 1 < attempts and self._list_delays:
                self._sleep(self._list_delays[min(attempt, len(self._list_delays) - 1)])
        by_id = {item.file_id: item for item in found}
        retrieved: list[str] = []
        for file_id in missing:
            remembered_file = remembered.get(file_id)
            if remembered_file is not None:
                if remembered_file.file_id != file_id:
                    raise DriveError("The confirmed file id did not match.")
                _require_operable(remembered_file, self._folder_id)
                by_id[file_id] = remembered_file
            else:
                by_id[file_id] = self._get_guarded(file_id)
            retrieved.append(file_id)
        return GenerationListing(tuple(_sorted_files(by_id.values())), tuple(retrieved))

    def _list_pages(self) -> list[DriveFile]:
        self._ensure_folder()
        attempt = 0
        while True:
            try:
                return self._list_pages_once()
            except _RetryableDriveError as exc:
                if attempt >= len(self._retry_delays):
                    raise DriveUnavailable(str(exc)) from None
                self._sleep(self._retry_delays[attempt])
                attempt += 1

    def _list_pages_once(self) -> list[DriveFile]:
        items: list[DriveFile] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        query = _child_query(self._folder_id)
        for _ in range(MAX_LIST_PAGES):
            kwargs: dict = {
                "q": query,
                "fields": LIST_FIELDS,
                "pageSize": PAGE_SIZE,
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            }
            if page_token is not None:
                kwargs["pageToken"] = page_token
            response = self._execute_retrying(lambda kw=kwargs: self._files().list(**kw))
            if not isinstance(response, dict):
                raise DriveError("Drive returned a folder listing this app cannot read.")
            if response.get("incompleteSearch") is True:
                raise _RetryableDriveError(
                    "Google Drive returned an incomplete folder listing. Try again."
                )
            files = response.get("files") or []
            if not isinstance(files, list):
                raise DriveError("Drive returned a folder listing this app cannot read.")
            for resource in files:
                if isinstance(resource, dict):
                    item = _parse_listed_file(resource, self._folder_id)
                    if item is not None:
                        items.append(item)
            token = response.get("nextPageToken")
            if not token:
                return items
            if not isinstance(token, str) or token in seen_tokens:
                raise DriveError("Drive returned a folder listing this app cannot read.")
            seen_tokens.add(token)
            page_token = token
        raise DriveError("This folder has too many matching files to list in one pass.")

    def _upload_named(self, name: str, content: bytes) -> UploadedFile:
        _check_content(content)
        if not is_app_file_name(name):
            raise DriveError(
                "Refusing to upload a file whose name is not a snapshot or the lock."
            )
        self._ensure_folder()
        media = MediaIoBaseUpload(
            io.BytesIO(content),
            mimetype="application/octet-stream",
            resumable=False,
        )
        try:
            response = self._execute_once(
                self._files().create(
                    body={"name": name, "parents": [self._folder_id]},
                    media_body=media,
                    fields=FILE_FIELDS,
                    supportsAllDrives=True,
                )
            )
        except DriveUnavailable:
            raise DriveUploadUncertain(_UNCONFIRMED_NO_ID) from None
        raw_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(raw_id, str) or not is_drive_item_id(raw_id):
            raise DriveUploadUncertain(_UNCONFIRMED_NO_ID)
        file_id = raw_id
        try:
            confirmed = self._get_guarded(file_id)
        except (DriveUnavailable, DrivePermissionError):
            raise DriveUploadUncertain(_unconfirmed_with_id(file_id), file_id=file_id) from None
        if confirmed.name != name or self._folder_id not in confirmed.parents:
            raise DriveUploadUncertain(
                "The file id was read back, but it is not the file this upload created. "
                "Nothing was trashed or replaced.",
                file_id=file_id,
            )
        try:
            listing = self._list_for_generation((file_id,), {file_id: confirmed})
        except DriveUnavailable:
            raise DriveUploadUncertain(
                f"The new file {file_id} was read back by id, but the folder listing did not finish. "
                "The file was left in place. Nothing was trashed or replaced.",
                file_id=file_id,
            ) from None
        if all(item.file_id != file_id for item in listing.files):
            raise DriveUploadUncertain(
                f"The new file {file_id} was read back by id, but it could not be included "
                "in the folder listing. The file was left in place. Nothing was trashed or replaced.",
                file_id=file_id,
            )
        return UploadedFile(confirmed, listing)

    def _create_lock(self, content: bytes) -> UploadedFile:
        listing = self._list_for_generation((), {})
        if any(item.name == LOCK_FILE_NAME for item in listing.files):
            raise DriveError(
                "A lock file is already in this folder. It was left in place, "
                "and a second one was not created."
            )
        return self._upload_named(LOCK_FILE_NAME, content)

    def _download(self, file_id: str) -> bytes:
        self._get_guarded(file_id)
        return self._download_bytes(file_id)

    def _read_lock(self, file_id: str) -> bytes:
        current = self._get_guarded(file_id)
        if current.name != LOCK_FILE_NAME:
            raise DriveError("That file is not the lock file, so it was not read as the lock.")
        return self._download_bytes(file_id)

    def _update_lock(self, file_id: str, content: bytes) -> DriveFile:
        current = self._get_guarded(file_id)
        if current.name != LOCK_FILE_NAME:
            raise DriveError("That file is not the lock file, so it was not changed.")
        return self._write_content(file_id, content)

    def _update_content(self, file_id: str, content: bytes) -> DriveFile:
        self._get_guarded(file_id)
        return self._write_content(file_id, content)

    def _write_content(self, file_id: str, content: bytes) -> DriveFile:
        _check_content(content)
        media = MediaIoBaseUpload(
            io.BytesIO(content),
            mimetype="application/octet-stream",
            resumable=False,
        )
        try:
            self._execute_once(
                self._files().update(
                    fileId=file_id,
                    media_body=media,
                    fields=FILE_FIELDS,
                    supportsAllDrives=True,
                )
            )
        except DriveUnavailable:
            return self._content_after_uncertain_write(file_id, content)
        return self._require_downloaded(file_id, content)

    def _content_after_uncertain_write(self, file_id: str, content: bytes) -> DriveFile:
        try:
            downloaded = self._download_bytes(file_id)
        except DriveError:
            raise DriveUploadUncertain(_unconfirmed_write(file_id), file_id=file_id) from None
        if downloaded != content:
            raise DriveUploadUncertain(_unconfirmed_write(file_id), file_id=file_id)
        return self._get_guarded(file_id)

    def _require_downloaded(self, file_id: str, content: bytes) -> DriveFile:
        downloaded = self._download_bytes(file_id)
        if downloaded != content:
            raise DriveError(
                f"File {file_id} was read back, but its contents did not match what was sent. "
                "Nothing was trashed."
            )
        return self._get_guarded(file_id)

    def _trash(self, file_id: str) -> TrashResult:
        current = self._get_guarded(file_id)
        if not current.can_trash:
            return TrashResult(file_id=file_id, trashed=False, skipped_not_owner=True)
        try:
            response = self._execute_once(
                self._files().update(
                    fileId=file_id,
                    body={"trashed": True},
                    fields=FILE_FIELDS,
                    supportsAllDrives=True,
                )
            )
        except DrivePermissionError:
            return TrashResult(file_id=file_id, trashed=False, skipped_not_owner=True)
        except DriveUnavailable:
            raise DriveUploadUncertain(
                f"Moving file {file_id} to the trash did not finish. "
                "Nothing was permanently deleted.",
                file_id=file_id,
            ) from None
        if isinstance(response, Mapping) and response.get("trashed") is True:
            return TrashResult(file_id=file_id, trashed=True, skipped_not_owner=False)
        item = _parse_file(
            self._execute_retrying(
                lambda: self._files().get(
                    fileId=file_id,
                    fields=FILE_FIELDS,
                    supportsAllDrives=True,
                )
            )
        )
        if not item.trashed:
            raise DriveError(
                f"Drive did not confirm that file {file_id} is in the trash. "
                "It was not permanently deleted."
            )
        return TrashResult(file_id=file_id, trashed=True, skipped_not_owner=False)

    def _get_guarded(self, file_id: str) -> DriveFile:
        if not is_drive_item_id(file_id):
            raise DriveError("A Drive file id was not usable.")
        self._ensure_folder()
        resource = self._execute_retrying(
            lambda: self._files().get(
                fileId=file_id,
                fields=FILE_FIELDS,
                supportsAllDrives=True,
            )
        )
        item = _parse_file(resource)
        _require_operable(item, self._folder_id)
        return item

    def _download_bytes(self, file_id: str) -> bytes:
        payload = self._execute_retrying(
            lambda: self._files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            )
        )
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes):
            raise DriveError("Drive did not return the file bytes.")
        return payload

    def _server_time_now(self) -> datetime:
        # Drop the cached header first. A missing Date on this response must
        # not fall back to an older reading or to this computer's clock.
        self._server_time = None
        try:
            resource = self._execute_retrying(
                lambda: self._files().get(
                    fileId=self._folder_id,
                    fields=FOLDER_LOOKUP_FIELDS,
                    supportsAllDrives=True,
                )
            )
        except DrivePermissionError:
            raise DrivePermissionError(_FOLDER_DENIED) from None
        self._folder = _folder_from_resource(resource, self._folder_id)
        if self._server_time is None:
            raise DriveError(
                "Google Drive did not provide a server time. "
                "The lock cannot use this computer's clock instead."
            )
        return self._server_time

    def _execute_once(self, request: object) -> object:
        try:
            return self._execute_attempt(request)
        except _RetryableDriveError as exc:
            raise DriveUnavailable(str(exc)) from None

    def _execute_retrying(self, factory: Callable[[], object]) -> object:
        attempt = 0
        while True:
            try:
                return self._execute_attempt(factory())
            except _RetryableDriveError as exc:
                if attempt >= len(self._retry_delays):
                    raise DriveUnavailable(str(exc)) from None
                self._sleep(self._retry_delays[attempt])
                attempt += 1

    def _execute_attempt(self, request: object) -> object:
        if hasattr(request, "add_response_callback"):
            request.add_response_callback(self._remember_response_date)
        try:
            return request.execute(num_retries=0)
        except HttpError as exc:
            raise _map_http(exc) from None
        except RefreshError:
            raise DriveAuthError(_AUTH_MESSAGE) from None
        except TimeoutError:
            raise _RetryableDriveError(_TIMEOUT_MESSAGE) from None
        except (
            ConnectionError,
            socket.gaierror,
            ssl.SSLError,
            httplib2.HttpLib2Error,
            TransportError,
        ):
            raise _RetryableDriveError(_NETWORK_MESSAGE) from None

    def _remember_response_date(self, response: object) -> None:
        try:
            getter = getattr(response, "get", None)
            if getter is None:
                return
            header = getter("date")
            if not isinstance(header, str):
                header = getter("Date")
            if not isinstance(header, str):
                return
            parsed = parsedate_to_datetime(header)
        except Exception:
            return
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return
        self._server_time = parsed.astimezone(timezone.utc)


def _child_query(folder_id: str) -> str:
    return (
        f"'{folder_id}' in parents and trashed = false and "
        f"(name = '{LOCK_FILE_NAME}' or name contains '{SNAPSHOT_PREFIX}')"
    )


def _check_content(content: object) -> None:
    if not isinstance(content, bytes):
        raise DriveError("A Drive upload needs bytes.")
    if not content:
        raise DriveError("Refusing to upload an empty file.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise DriveError("Refusing to upload a file larger than 8 MiB.")


def _unconfirmed_with_id(file_id: str) -> str:
    return (
        f"The upload returned file id {file_id}, but reading it back did not finish. "
        "It was not confirmed. Nothing was trashed or replaced."
    )


def _unconfirmed_write(file_id: str) -> str:
    return (
        f"The update of file {file_id} did not finish, and the bytes read back "
        "did not match what was sent. It was not confirmed. Nothing was trashed."
    )


def _folder_from_resource(resource: object, folder_id: str) -> DriveFolder:
    if not isinstance(resource, dict):
        raise DriveError("Drive did not return the configured folder.")
    if resource.get("trashed") is True:
        raise DriveError("The configured Drive folder is in the trash.")
    mime = resource.get("mimeType")
    if mime == SHORTCUT_MIME:
        raise DriveError(
            "The saved Drive id is a shortcut. Run python -m finance.google_setup "
            "again and confirm the folder it shows. The saved id must be the real folder."
        )
    if mime != FOLDER_MIME:
        raise DriveError("The saved Drive id is not a folder.")
    if resource.get("id") != folder_id:
        raise DriveError(
            "Drive returned a different folder id than the one saved on this computer."
        )
    name = resource.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DriveError("Drive did not return the folder name.")
    display_name, email = _first_owner(resource)
    return DriveFolder(
        folder_id=folder_id,
        name=name.strip(),
        owner_display_name=display_name,
        owner_email=email,
        followed_shortcut=False,
    )


def _parse_listed_file(resource: Mapping, folder_id: str) -> DriveFile | None:
    try:
        item = _parse_file(resource)
    except DriveError:
        return None
    if item.trashed or folder_id not in item.parents or not is_app_file_name(item.name):
        return None
    return item


def _parse_file(resource: object) -> DriveFile:
    if not isinstance(resource, Mapping):
        raise DriveError("Drive did not return that file.")
    file_id = resource.get("id")
    if not is_drive_item_id(file_id):
        raise DriveError("Drive did not return a file id.")
    name = resource.get("name")
    if not isinstance(name, str):
        raise DriveError(f"Drive did not return a name for file {file_id}.")
    parents = resource.get("parents") or []
    if not isinstance(parents, list):
        raise DriveError(f"Drive did not return parents for file {file_id}.")
    parent_ids = tuple(parent for parent in parents if isinstance(parent, str))
    checksum = resource.get("md5Checksum")
    if not isinstance(checksum, str):
        checksum = ""
    return DriveFile(
        file_id=file_id,
        name=name,
        parents=parent_ids,
        owners=_owners(resource),
        can_trash=_can_trash(resource),
        created_time=_parse_time(resource.get("createdTime")),
        md5_checksum=checksum,
        trashed=resource.get("trashed") is True,
    )


def _require_operable(item: DriveFile, folder_id: str) -> None:
    if item.trashed:
        raise DriveError(f"File {item.file_id} is in the trash, so it was not changed.")
    if folder_id not in item.parents:
        raise DriveError(
            f"File {item.file_id} is not in the configured Drive folder, so it was not changed."
        )
    if not is_app_file_name(item.name):
        raise DriveError(
            f"File {item.file_id} is not a FinanceTracker snapshot or lock file, "
            "so it was not changed."
        )


def _owners(resource: Mapping) -> tuple[DriveOwner, ...]:
    raw = resource.get("owners")
    if not isinstance(raw, list):
        return ()
    owners: list[DriveOwner] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        display = item.get("displayName")
        email = item.get("emailAddress")
        owners.append(
            DriveOwner(
                display.strip() if isinstance(display, str) else "",
                email.strip() if isinstance(email, str) else "",
            )
        )
    return tuple(owners)


def _first_owner(resource: Mapping) -> tuple[str, str]:
    owners = _owners(resource)
    if not owners:
        return "", ""
    return owners[0].display_name, owners[0].email


def _can_trash(resource: Mapping) -> bool:
    capabilities = resource.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return False
    return capabilities.get("canTrash") is True


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise DriveError("Drive did not return when that file was created.")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise DriveError("Drive returned a created time this app cannot read.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DriveError("Drive returned a created time this app cannot read.")
    return parsed.astimezone(timezone.utc)


def _sorted_files(files: object) -> list[DriveFile]:
    return sorted(
        files,
        key=lambda item: (
            item.generation is None,
            -(item.generation or 0),
            item.created_time,
            item.file_id,
        ),
    )


def _map_http(exc: HttpError) -> DriveError:
    status = _status(exc)
    reason = _reason(exc)
    if status == 401 or reason in _AUTH_REASONS:
        return DriveAuthError(_AUTH_MESSAGE)
    if status == 429 or status in {408, 500, 502, 503, 504} or reason in _RATE_REASONS:
        if status == 429 or reason in _RATE_REASONS:
            return _RetryableDriveError(_RATE_MESSAGE)
        return _RetryableDriveError(
            "Google Drive returned an error. Check the network and try again."
        )
    if status == 404:
        return DrivePermissionError(
            "That Drive item was not found, or this Google account cannot see it."
        )
    if status == 403:
        return DrivePermissionError(
            "This Google account cannot change that Drive item. "
            "The shared folder needs to be shared as Editor."
        )
    return DriveUnavailable("Google Drive returned an error. Check the network and try again.")


def _status(exc: HttpError) -> int | None:
    raw = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _reason(exc: HttpError) -> str:
    content = exc.content
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if not isinstance(content, str):
        return ""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return ""
    reason = errors[0].get("reason")
    if isinstance(reason, str) and _REASON.fullmatch(reason):
        return reason
    return ""
