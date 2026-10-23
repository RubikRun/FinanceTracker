"""In-memory stand-in for the Drive v3 ``files()`` resource.

Production code never imports this module. It models the two facts the
adapter has to get right: a file belongs to the account that created it,
and only that account has ``capabilities.canTrash``. A folder listing can
also hide a file that ``files.get`` already returns.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

_EPOCH = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "status"


def http_error(status: int, reason: str, message: str) -> HttpError:
    body = json.dumps(
        {"error": {"errors": [{"reason": reason}], "message": message}}
    ).encode("utf-8")
    return HttpError(_Response(status), body)


@dataclass
class FakeFile:
    file_id: str
    name: str
    parents: tuple[str, ...]
    content: bytes
    can_trash: bool
    created_time: datetime
    trashed: bool = False
    owners: tuple[dict[str, str], ...] = ()

    def metadata(self) -> dict:
        return {
            "id": self.file_id,
            "name": self.name,
            "parents": list(self.parents),
            "owners": [dict(owner) for owner in self.owners],
            "capabilities": {"canTrash": self.can_trash},
            "createdTime": _stamp(self.created_time),
            "md5Checksum": hashlib.md5(self.content).hexdigest(),
            "trashed": self.trashed,
        }


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class FakeDrive:
    """The files one Drive account can see inside a test."""

    folder_id: str
    folder_name: str = "FinanceTracker test"
    folder_mime: str = "application/vnd.google-apps.folder"
    folder_trashed: bool = False
    owner_name: str = "Account A"
    owner_email: str = "a@example.com"
    date_header: str | None = "Wed, 22 Oct 2026 09:41:00 GMT"
    honor_query: bool = True
    page_size: int = 100
    hide_created: bool = False
    lose_create_response: bool = False
    apply_lost_create: bool = True
    lost_create_message: str = "create response lost"
    incomplete_lists: int = 0
    endless_pages: bool = False
    repeat_token: bool = False
    fail_list_remaining: int = 0
    list_error: BaseException | None = None
    get_error: BaseException | None = None
    update_error: BaseException | None = None
    apply_update_before_error: bool = False
    files: dict[str, FakeFile] = field(default_factory=dict)
    aliases: dict[str, dict] = field(default_factory=dict)
    hidden_ids: set[str] = field(default_factory=set)
    fail_get_ids: set[str] = field(default_factory=set)
    refuse_trash_ids: set[str] = field(default_factory=set)
    calls: list[dict] = field(default_factory=list)
    delete_calls: list[dict] = field(default_factory=list)
    pause_methods: set[str] = field(default_factory=set)
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    peak: int = 0
    active: int = 0
    _serial: int = 0
    _paused_once: bool = False
    _page_serial: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    next_id: str | None = None
    before_execute: Callable[[str], None] | None = None

    def service(self) -> _Service:
        return _Service(self)

    def add(
        self,
        file_id: str,
        name: str,
        *,
        parents: tuple[str, ...] | None = None,
        content: bytes = b"synthetic",
        can_trash: bool = True,
        created_time: datetime | None = None,
        trashed: bool = False,
        email: str | None = None,
        display_name: str | None = None,
    ) -> FakeFile:
        owner = {
            "displayName": display_name or self.owner_name,
            "emailAddress": email or self.owner_email,
        }
        item = FakeFile(
            file_id=file_id,
            name=name,
            parents=self._parents(parents),
            content=content,
            can_trash=can_trash,
            created_time=created_time or self._clock(),
            trashed=trashed,
            owners=(owner,),
        )
        self.files[file_id] = item
        return item

    def execute(
        self,
        method: str,
        kwargs: dict,
        num_retries: int,
        callbacks: list,
    ) -> object:
        recorded = {
            key: value
            for key, value in kwargs.items()
            if key != "media_body"
        }
        media = kwargs.get("media_body")
        if media is not None:
            recorded["content"] = _media_bytes(media)
        recorded["method"] = method
        recorded["num_retries"] = num_retries
        self.calls.append(recorded)
        if self.before_execute is not None:
            self.before_execute(method)
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self._maybe_pause(method)
            result = self._dispatch(method, kwargs)
        finally:
            with self._lock:
                self.active -= 1
        response: dict[str, object] = {"status": 200}
        if self.date_header is not None:
            response["date"] = self.date_header
        for callback in callbacks:
            callback(response)
        return result

    def _maybe_pause(self, method: str) -> None:
        if method not in self.pause_methods or self._paused_once:
            return
        self._paused_once = True
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("the test did not release the Drive call")

    def _dispatch(self, method: str, kwargs: dict) -> object:
        if method == "list":
            return self._list(kwargs)
        if method == "get":
            return self._get(kwargs["fileId"])
        if method == "get_media":
            return self._media(kwargs["fileId"])
        if method == "create":
            return self._create(kwargs)
        if method == "update":
            return self._update(kwargs)
        if method == "delete":
            self.delete_calls.append(kwargs)
            raise AssertionError("permanent delete")
        raise AssertionError(f"unexpected Drive method {method}")

    def _list(self, kwargs: dict) -> dict:
        if self.fail_list_remaining:
            self.fail_list_remaining -= 1
            if self.list_error is None:
                raise AssertionError("list failure was not set")
            raise self.list_error
        if self.incomplete_lists:
            self.incomplete_lists -= 1
            return {"files": [], "incompleteSearch": True}
        if self.repeat_token:
            return {"files": [], "nextPageToken": "again"}
        selected = self._visible(str(kwargs.get("q", "")))
        if self.endless_pages:
            self._page_serial += 1
            files = [selected[0].metadata()] if selected else []
            return {"files": files, "nextPageToken": f"page-{self._page_serial}"}
        start = int(kwargs.get("pageToken") or "0")
        chunk = selected[start : start + self.page_size]
        following = start + self.page_size
        body: dict = {"files": [item.metadata() for item in chunk]}
        if following < len(selected):
            body["nextPageToken"] = str(following)
        return body

    def _visible(self, query: str) -> list[FakeFile]:
        items = [
            item
            for item in self.files.values()
            if item.file_id not in self.hidden_ids
        ]
        if not self.honor_query:
            return sorted(items, key=lambda item: (item.created_time, item.file_id))
        folder = ""
        marker = "' in parents"
        if marker in query:
            folder = query.split(marker, 1)[0].split("'")[-1]
        require_names = "finance_snapshot_g" in query
        selected = []
        for item in items:
            if folder and folder not in item.parents:
                continue
            if "trashed = false" in query and item.trashed:
                continue
            if require_names and not _name_matches_query(item.name):
                continue
            selected.append(item)
        return sorted(selected, key=lambda item: (item.created_time, item.file_id))

    def _get(self, file_id: str) -> dict:
        if file_id in self.fail_get_ids:
            self.fail_get_ids.remove(file_id)
            if self.get_error is None:
                raise AssertionError("get failure was not set")
            raise self.get_error
        if file_id == self.folder_id:
            return self._folder_metadata()
        if file_id in self.aliases:
            return dict(self.aliases[file_id])
        item = self.files.get(file_id)
        if item is None:
            raise http_error(404, "notFound", "missing")
        return item.metadata()

    def _media(self, file_id: str) -> bytes:
        item = self.files.get(file_id)
        if item is None:
            raise http_error(404, "notFound", "missing")
        return item.content

    def _create(self, kwargs: dict) -> dict:
        body = kwargs.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        content = kwargs.get("content")
        if content is None and kwargs.get("media_body") is not None:
            content = _media_bytes(kwargs["media_body"])
        if not isinstance(content, bytes):
            content = b""
        parents = body.get("parents") or []
        item = self.add(
            self._new_id(),
            str(body.get("name", "")),
            parents=tuple(parent for parent in parents if isinstance(parent, str)),
            content=content,
            can_trash=True,
        )
        if self.hide_created:
            self.hidden_ids.add(item.file_id)
        if self.lose_create_response:
            self.lose_create_response = False
            if not self.apply_lost_create:
                self.files.pop(item.file_id, None)
            raise TimeoutError(self.lost_create_message)
        return item.metadata()

    def _update(self, kwargs: dict) -> dict:
        file_id = kwargs["fileId"]
        item = self.files.get(file_id)
        if item is None:
            raise http_error(404, "notFound", "missing")
        body = kwargs.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        if body.get("trashed") is True:
            if not item.can_trash or file_id in self.refuse_trash_ids:
                raise http_error(403, "insufficientFilePermissions", "not owner")
            item.trashed = True
        media = kwargs.get("media_body")
        if media is not None:
            if self.update_error is not None:
                error = self.update_error
                self.update_error = None
                if self.apply_update_before_error:
                    item.content = _media_bytes(media)
                raise error
            item.content = _media_bytes(media)
        return item.metadata()

    def _folder_metadata(self) -> dict:
        resource: dict = {
            "id": self.folder_id,
            "name": self.folder_name,
            "mimeType": self.folder_mime,
            "trashed": self.folder_trashed,
            "owners": [
                {"displayName": self.owner_name, "emailAddress": self.owner_email}
            ],
        }
        if self.folder_mime == "application/vnd.google-apps.shortcut":
            resource["shortcutDetails"] = {"targetId": self.folder_id}
        return resource

    def _new_id(self) -> str:
        if self.next_id is not None:
            file_id = self.next_id
            self.next_id = None
            return file_id
        self._serial += 1
        return f"created-{self._serial:04d}-abcdefghij"

    def _clock(self) -> datetime:
        self._serial += 1
        return _EPOCH + timedelta(seconds=self._serial)

    def _parents(self, parents: tuple[str, ...] | None) -> tuple[str, ...]:
        if parents is None:
            return (self.folder_id,)
        return parents


def _name_matches_query(name: str) -> bool:
    return name == "finance_lock.json" or "finance_snapshot_g" in name


def _media_bytes(media: object) -> bytes:
    size = media.size()  # type: ignore[attr-defined]
    data = media.getbytes(0, size)  # type: ignore[attr-defined]
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise AssertionError("upload was not bytes")
    return data


class _Request:
    def __init__(self, drive: FakeDrive, method: str, kwargs: dict) -> None:
        self._drive = drive
        self._method = method
        self._kwargs = kwargs
        self._callbacks: list = []

    def add_response_callback(self, callback: Callable[[dict], None]) -> None:
        self._callbacks.append(callback)

    def execute(self, num_retries: int = 0) -> object:
        return self._drive.execute(self._method, self._kwargs, num_retries, self._callbacks)


class _Files:
    def __init__(self, drive: FakeDrive) -> None:
        self._drive = drive

    def list(self, **kwargs: object) -> _Request:
        return _Request(self._drive, "list", kwargs)

    def get(self, **kwargs: object) -> _Request:
        return _Request(self._drive, "get", kwargs)

    def get_media(self, **kwargs: object) -> _Request:
        return _Request(self._drive, "get_media", kwargs)

    def create(self, **kwargs: object) -> _Request:
        return _Request(self._drive, "create", kwargs)

    def update(self, **kwargs: object) -> _Request:
        return _Request(self._drive, "update", kwargs)

    def delete(self, **kwargs: object) -> _Request:
        return _Request(self._drive, "delete", kwargs)


class _Service:
    def __init__(self, drive: FakeDrive) -> None:
        self._drive = drive

    def files(self) -> _Files:
        return _Files(self._drive)
