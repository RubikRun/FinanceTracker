"""Drive lock, same-computer process lock, and idle release.

The GUI and the CLI do not call this module. Cloud mode stays off. Nothing
here reads or writes the finance file, and nothing here deletes the Drive
lock. The lock is the contents of ``finance_lock.json``. It is read and
updated by the cached file id. ``appProperties`` are not used.

Timings, all based on Drive's clock (the HTTP ``Date`` header), not on this
computer's clock:

- heartbeat about every 30 seconds
- stale after 3 minutes
- confirmation wait of 5 seconds after taking the lock
- idle release after 10 minutes with no activity, and only when nothing is unsaved

A take writes a new random token, waits, and keeps the lock only if that
token is still there. A heartbeat or a release that sees a different token
writes nothing. A stale lock from another computer is taken only after the
caller confirms the token and heartbeat that were shown. If this computer's
machine id is on the lock and the local process lock is free, the previous
process is gone and the caller can take it immediately.

The background timer calls ``poll``. Tests call ``poll`` themselves and do
not wait for real minutes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .config import USERS
from .drive_adapter import LOCK_FILE_NAME, DriveAdapter, DriveError, DriveUploadUncertain
from .profile import (
    ConfigError,
    LocalConfig,
    ensure_machine_identity,
    lock_session_path,
    process_lock_path,
    save_local_config,
)

HEARTBEAT_INTERVAL = timedelta(seconds=30)
STALE_AFTER = timedelta(minutes=3)
ACQUIRE_CONFIRMATION_DELAY = timedelta(seconds=5)
IDLE_RELEASE_AFTER = timedelta(minutes=10)
TIMER_POLL_SECONDS = 10

LOCK_FORMAT_VERSION = 1
STATE_HELD = "held"
STATE_FREE = "free"

MODE_CLOSED = "closed"
MODE_EDITABLE = "editable"
MODE_READ_ONLY = "read_only"
MODE_PAUSED = "paused"
MODE_STALE = "stale"
MODE_TAKEOVER = "takeover"
MODE_LOST = "lost"
MODE_DUPLICATE = "duplicate"
MODE_MISSING = "missing"
MODE_UNREADABLE = "unreadable"
MODE_UNAVAILABLE = "unavailable"
MODE_UNVERIFIED = "unverified"
MODE_CHANGED = "changed"

CHOICE_TRY_AGAIN = "try_again"
CHOICE_RECOVER = "recover"
CHOICE_TAKE_OVER = "take_over"
CHOICE_CREATE_LOCK = "create_lock"
CHOICE_RESUME = "resume"
CHOICE_REVERIFY = "reverify"

_STALE_MINUTES = int(STALE_AFTER.total_seconds() // 60)
_MAX_LOCK_BYTES = 8192
_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_ID = re.compile(r"^[0-9a-f]{32,128}$")
_DISPLAY_LABEL = re.compile(r"[A-Za-z0-9 ]{1,32}")
_UNREADABLE = "The lock file is not one this app can read. It was not changed."
_SESSION_VERSION = 1


class LockError(Exception):
    """The lock file cannot be used. The message is safe to show."""


class GenerationView(Protocol):
    """Newest snapshot generation, without putting Drive calls in the GUI."""

    def base_generation(self) -> int:
        """Generation this session last loaded."""

    def newest_generation(self) -> int:
        """Newest generation currently in Drive."""

    def reload(self) -> int:
        """Load that generation and return it."""


@dataclass(frozen=True)
class LockDocument:
    """The JSON stored in the Drive lock file. No finance records."""

    format_version: int
    state: str
    session_token: str
    owner_label: str
    machine_id: str
    machine_label: str
    acquired_at_utc: datetime | None
    heartbeat_at_utc: datetime | None

    def __repr__(self) -> str:
        return f"LockDocument(state={self.state!r}, owner_label={self.owner_label!r})"


@dataclass(frozen=True)
class LockStatus:
    """What the caller can do. ``reviewed_token`` is for the confirmation step."""

    mode: str
    message: str
    can_edit: bool
    choices: tuple[str, ...] = ()
    owner_label: str = ""
    machine_id: str = ""
    machine_label: str = ""
    heartbeat_at_utc: datetime | None = None
    reviewed_token: str = ""
    reloaded: bool = False
    lock_file_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"LockStatus(mode={self.mode!r}, can_edit={self.can_edit!r}, "
            f"reloaded={self.reloaded!r})"
        )


def blank_lock() -> LockDocument:
    """A free lock with no session. Used when the file is first created."""
    return LockDocument(
        format_version=LOCK_FORMAT_VERSION,
        state=STATE_FREE,
        session_token="",
        owner_label="",
        machine_id="",
        machine_label="",
        acquired_at_utc=None,
        heartbeat_at_utc=None,
    )


def encode_lock(document: LockDocument) -> bytes:
    """UTF-8 JSON for the lock file. The same document always encodes the same way."""
    _require_document(document)
    payload = {
        "format_version": document.format_version,
        "state": document.state,
        "session_token": document.session_token,
        "owner_label": document.owner_label,
        "machine_id": document.machine_id,
        "machine_label": document.machine_label,
        "acquired_at_utc": _stamp(document.acquired_at_utc),
        "heartbeat_at_utc": _stamp(document.heartbeat_at_utc),
    }
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def decode_lock(payload: bytes) -> LockDocument:
    """Read a lock file. A bad file raises ``LockError`` and includes none of its bytes."""
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_LOCK_BYTES:
        raise LockError(_UNREADABLE)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise LockError(_UNREADABLE) from None
    if not isinstance(data, dict):
        raise LockError(_UNREADABLE)
    version = data.get("format_version")
    if isinstance(version, bool) or version != LOCK_FORMAT_VERSION:
        raise LockError(_UNREADABLE)
    state = data.get("state")
    if state not in (STATE_HELD, STATE_FREE):
        raise LockError(_UNREADABLE)
    token = data.get("session_token", "")
    if not isinstance(token, str):
        raise LockError(_UNREADABLE)
    if state == STATE_HELD and _TOKEN.fullmatch(token) is None:
        raise LockError(_UNREADABLE)
    if state == STATE_FREE and token not in ("",) and _TOKEN.fullmatch(token) is None:
        token = ""
    owner = _text_field(data.get("owner_label", ""), required=state == STATE_HELD)
    if state == STATE_HELD and owner not in USERS:
        raise LockError(_UNREADABLE)
    machine_id = data.get("machine_id", "")
    if state == STATE_HELD:
        if not isinstance(machine_id, str) or _MACHINE_ID.fullmatch(machine_id) is None:
            raise LockError(_UNREADABLE)
    elif machine_id in ("", None):
        machine_id = ""
    elif not isinstance(machine_id, str) or _MACHINE_ID.fullmatch(machine_id) is None:
        machine_id = ""
    label = _text_field(data.get("machine_label", ""), required=False)
    acquired = _parse_time(data.get("acquired_at_utc"))
    heartbeat = _parse_time(data.get("heartbeat_at_utc"))
    if state == STATE_HELD and (acquired is None or heartbeat is None):
        raise LockError(_UNREADABLE)
    return LockDocument(
        format_version=LOCK_FORMAT_VERSION,
        state=state,
        session_token=token if state == STATE_HELD or _TOKEN.fullmatch(token or "") else "",
        owner_label=owner,
        machine_id=machine_id if isinstance(machine_id, str) else "",
        machine_label=label,
        acquired_at_utc=acquired,
        heartbeat_at_utc=heartbeat,
    )


class ProcessLock:
    """One non-blocking OS file lock. No Qt. A crash closes the handle and frees it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR)
        handle = os.fdopen(fd, "r+b")
        try:
            _lock_handle(handle)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            _unlock_handle(handle)
        except OSError:
            pass
        handle.close()


class LockSession:
    """One computer's view of the Drive lock.

    Drive calls go through the adapter, which runs them one at a time.
    ``start_timer`` polls from a background thread. The GUI is not wired here.
    """

    def __init__(
        self,
        drive: DriveAdapter,
        *,
        owner_label: str,
        config: LocalConfig | None = None,
        process_lock: ProcessLock | None = None,
        session_path: Path | None = None,
        generations: GenerationView | None = None,
        sleep: Callable[[float], None] | None = None,
        confirmation_delay: timedelta = ACQUIRE_CONFIRMATION_DELAY,
        heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
        stale_after: timedelta = STALE_AFTER,
        idle_after: timedelta = IDLE_RELEASE_AFTER,
    ) -> None:
        if owner_label not in USERS:
            raise LockError("The lock owner must be Boris or Milena.")
        self._drive = drive
        self._owner_label = owner_label
        self._config = ensure_machine_identity(config)
        if _MACHINE_ID.fullmatch(self._config.machine_id) is None:
            raise LockError("This computer does not have a machine id yet.")
        self._machine_id = self._config.machine_id
        self._machine_label = self._config.machine_label.strip() or "computer"
        self.process_lock = process_lock or ProcessLock(process_lock_path())
        self._session_path = session_path or lock_session_path()
        self._generations = generations
        self._sleep = sleep or _real_sleep
        self._confirmation_delay = confirmation_delay
        self._heartbeat_interval = heartbeat_interval
        self._stale_after = stale_after
        self._idle_after = idle_after
        self._file_id: str | None = self._config.lock_file_id
        self._token: str | None = None
        self._mode = MODE_CLOSED
        self._dirty = False
        self._activity = False
        self._last_activity: datetime | None = None
        self._verified_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None
        self._snapshots_seen = False
        self._closed = False
        self._mutex = threading.Lock()
        self._stop = threading.Event()
        self._timer: threading.Thread | None = None
        self._timer_interval = TIMER_POLL_SECONDS
        self._status = self._make(
            MODE_CLOSED,
            "The lock has not been opened.",
            can_edit=False,
        )

    @property
    def status(self) -> LockStatus:
        with self._mutex:
            return self._status

    def start(self) -> LockStatus:
        """Read the lock and take it when it is free."""
        with self._mutex:
            return self._guard(self._start_locked)

    def heartbeat(self) -> LockStatus:
        """Read the lock, then update the heartbeat only if this session's token is still there."""
        with self._mutex:
            return self._guard(self._reverify)

    def confirm_for_save(self) -> LockStatus:
        """Read the lock before a save. This does not write the lock."""
        with self._mutex:
            return self._guard(self._confirm_for_save)

    def reverify(self) -> LockStatus:
        """Fresh read after a gap. A matching token is refreshed. A different token is left alone."""
        with self._mutex:
            return self._guard(self._reverify)

    def release(self, *, discard: bool = False) -> LockStatus:
        """Set the lock to free when this session still holds it and nothing unsaved remains."""
        with self._mutex:
            return self._guard(lambda: self._release(closing=False, discard=discard))

    def recover(self, review: LockStatus) -> LockStatus:
        """Take a lock that was shown to the caller, if it still has the same token and heartbeat."""
        with self._mutex:
            return self._guard(lambda: self._recover(review))

    def create_missing_lock(self) -> LockStatus:
        """Create the lock file after the caller confirms, then look again for a second one."""
        with self._mutex:
            return self._guard(self._create_missing)

    def note_activity(self) -> None:
        """The GUI reports a key or a click. The next ``poll`` applies it using Drive's clock."""
        with self._mutex:
            self._activity = True

    def set_dirty(self, dirty: bool) -> None:
        with self._mutex:
            self._dirty = bool(dirty)

    def poll(self) -> LockStatus:
        """Heartbeat, re-check after a gap, or release when idle and clean."""
        with self._mutex:
            return self._guard(self._poll)

    def resume(self) -> LockStatus:
        """Take the lock again after idle. Reload first when Drive has a newer generation."""
        with self._mutex:
            return self._guard(self._resume)

    def start_timer(self, *, interval: float = TIMER_POLL_SECONDS) -> None:
        """Poll on a background thread. Tests that call ``poll`` themselves do not need this."""
        with self._mutex:
            if self._timer is not None or self._closed:
                return
            self._stop.clear()
            self._timer_interval = interval
            self._timer = threading.Thread(
                target=self._timer_loop,
                name="finance-lock",
                daemon=True,
            )
            self._timer.start()

    def stop_timer(self) -> None:
        self._stop.set()
        timer = self._timer
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=5)
        self._timer = None

    def close(self) -> LockStatus:
        """Release the Drive lock when this session holds it and nothing is unsaved."""
        self.stop_timer()
        with self._mutex:
            if self._closed:
                return self._status
        self._closed = True
        try:
            if self._token and not self._dirty:
                return self._release(closing=True)
            self.process_lock.release()
            if self._token and self._dirty:
                return self._publish(
                    self._mode if self._mode != MODE_CLOSED else MODE_EDITABLE,
                    "Unsaved changes are still here. The lock was not released.",
                    can_edit=False,
                )
            return self._status
        except LockError:
            self._forget_ownership()
            self.process_lock.release()
            return self._publish(MODE_UNREADABLE, _UNREADABLE, can_edit=False)
        except DriveError as exc:
            self.process_lock.release()
            return self._unavailable(exc)

    def _guard(self, action: Callable[[], LockStatus]) -> LockStatus:
        if self._closed:
            return self._publish(
                MODE_CLOSED,
                "This lock session is closed.",
                can_edit=False,
            )
        try:
            return action()
        except DriveUploadUncertain as exc:
            return self._unavailable(exc)
        except DriveError as exc:
            return self._unavailable(exc)
        except ConfigError as exc:
            return self._unavailable_message(str(exc))
        except LockError as exc:
            return self._publish(MODE_UNREADABLE, str(exc), can_edit=False)

    def _start_locked(self) -> LockStatus:
        if not self.process_lock.try_acquire():
            return self._other_window()
        return self._decide()

    def _decide(self) -> LockStatus:
        found = self._locate()
        if isinstance(found, LockStatus):
            return found
        return self._decide_document(found)

    def _decide_document(self, file_id: str) -> LockStatus:
        self._file_id = file_id
        current, now = self._read()
        if current.state == STATE_FREE:
            return self._claim()
        if (
            self._token
            and current.session_token == self._token
            and self._fresh(current, now)
        ):
            self._verified_at = now
            self._last_heartbeat_at = current.heartbeat_at_utc
            return self._editable(current)
        if current.machine_id == self._machine_id:
            return self._takeover_offer(current)
        if not self._fresh(current, now):
            return self._stale_offer(current)
        return self._readonly(current, raced=False)

    def _claim(self) -> LockStatus:
        if not self._file_id:
            raise LockError(_UNREADABLE)
        now = self._sample_time()
        token = secrets.token_hex(32)
        payload = encode_lock(self._held_document(now, token))
        try:
            self._drive.update_lock(self._file_id, payload)
        except DriveUploadUncertain:
            return self._confirm_uncertain(token)
        except DriveError as exc:
            return self._confirm_uncertain(token, exc)
        self._sleep(self._confirmation_delay.total_seconds())
        return self._confirm_token(token)

    def _confirm_uncertain(self, token: str, exc: DriveError | None = None) -> LockStatus:
        try:
            current, _now = self._read()
        except DriveError as read_error:
            self._token = token
            return self._unverified(read_error)
        if current.state == STATE_HELD and current.session_token == token:
            self._sleep(self._confirmation_delay.total_seconds())
            return self._confirm_token(token)
        if current.state == STATE_FREE:
            return self._publish(
                MODE_UNAVAILABLE,
                "The lock was not acquired. Try again.",
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN,),
            )
        if exc is not None and current.session_token != token:
            return self._readonly(current, raced=True)
        return self._readonly(current, raced=True)

    def _confirm_token(self, token: str) -> LockStatus:
        try:
            current, now = self._read()
        except DriveError as exc:
            self._token = token
            return self._unverified(exc)
        if current.state != STATE_HELD or current.session_token != token:
            self._token = None
            self._clear_session_file()
            return self._readonly(current, raced=True)
        self._token = token
        self._verified_at = now
        self._last_heartbeat_at = current.heartbeat_at_utc or now
        self._last_activity = now
        self._activity = False
        self._write_session_file(token)
        return self._editable(current)

    def _reverify(self) -> LockStatus:
        if not self._token or not self._file_id:
            return self._status
        if not self.process_lock.held and not self.process_lock.try_acquire():
            return self._other_window()
        current, now = self._read()
        if current.state != STATE_HELD or current.session_token != self._token:
            return self._lost(current)
        return self._write_heartbeat(current, now)

    def _write_heartbeat(self, current: LockDocument, now: datetime) -> LockStatus:
        updated = replace(current, heartbeat_at_utc=now)
        self._drive.update_lock(self._file_id or "", encode_lock(updated))
        self._verified_at = now
        self._last_heartbeat_at = now
        return self._editable(updated)

    def _confirm_for_save(self) -> LockStatus:
        if self._mode != MODE_EDITABLE or not self._token or not self._file_id:
            return self._publish(
                self._mode,
                "Editing is disabled until this window holds the lock.",
                can_edit=False,
                choices=self._status.choices,
            )
        now = self._sample_time()
        if self._verified_at is None or now - self._verified_at >= self._stale_after:
            return self._needs_reverify(
                "This session has not confirmed the lock recently. "
                "The lock was not changed. Confirm it again before saving."
            )
        current, now = self._read()
        if current.state != STATE_HELD or current.session_token != self._token:
            return self._lost(current)
        if (
            current.heartbeat_at_utc is None
            or now - current.heartbeat_at_utc >= self._stale_after
        ):
            return self._needs_reverify(
                "The last heartbeat is too old to save. The lock was not changed. "
                "Confirm the lock again before saving."
            )
        self._verified_at = now
        return self._editable(current)

    def _release(self, *, closing: bool, discard: bool = False) -> LockStatus:
        if self._dirty and not discard:
            return self._publish(
                MODE_EDITABLE if self._token else self._mode,
                "Unsaved changes are still here. The lock was not released.",
                can_edit=bool(self._token) and self._mode == MODE_EDITABLE,
            )
        if not self._token or not self._file_id:
            if closing:
                self.process_lock.release()
            return self._status
        current, _now = self._read()
        if current.state != STATE_HELD or current.session_token != self._token:
            self._forget_ownership()
            if closing:
                self.process_lock.release()
            return self._lost(current)
        freed = LockDocument(
            format_version=LOCK_FORMAT_VERSION,
            state=STATE_FREE,
            session_token="",
            owner_label=current.owner_label,
            machine_id=current.machine_id,
            machine_label=current.machine_label,
            acquired_at_utc=current.acquired_at_utc,
            heartbeat_at_utc=current.heartbeat_at_utc,
        )
        self._drive.update_lock(self._file_id, encode_lock(freed))
        self._forget_ownership()
        if closing:
            self.process_lock.release()
        return self._publish(
            MODE_PAUSED,
            "Editing is paused and the lock is free. Resume editing to take it again.",
            can_edit=False,
            choices=(CHOICE_RESUME,),
        )

    def _recover(self, review: LockStatus) -> LockStatus:
        if not self.process_lock.held and not self.process_lock.try_acquire():
            return self._other_window()
        if review.mode not in (MODE_STALE, MODE_TAKEOVER):
            return self._publish(
                review.mode,
                "This lock is not waiting to be taken. It was not changed.",
                can_edit=False,
                choices=review.choices,
            )
        if not review.lock_file_id and not self._file_id:
            return self._publish(MODE_UNREADABLE, _UNREADABLE, can_edit=False)
        self._file_id = review.lock_file_id or self._file_id
        current, now = self._read()
        if (
            current.state != STATE_HELD
            or current.session_token != review.reviewed_token
            or current.heartbeat_at_utc != review.heartbeat_at_utc
        ):
            return self._changed()
        if review.mode == MODE_TAKEOVER and current.machine_id != self._machine_id:
            return self._changed()
        if (
            review.mode == MODE_STALE
            and current.machine_id != self._machine_id
            and self._fresh(current, now)
        ):
            return self._readonly(current, raced=False)
        return self._claim()

    def _create_missing(self) -> LockStatus:
        if not self.process_lock.try_acquire():
            return self._other_window()
        found = self._locate()
        if isinstance(found, str):
            return self._decide_document(found)
        if found.mode != MODE_MISSING:
            return found
        try:
            created = self._drive.create_lock(encode_lock(blank_lock()))
        except DriveError:
            again = self._locate()
            if isinstance(again, str):
                return self._decide_document(again)
            return again
        again = self._locate()
        if isinstance(again, str):
            if again != created.file.file_id:
                return self._duplicate()
            return self._decide_document(again)
        if again.mode == MODE_MISSING:
            self._remember_id(created.file.file_id)
            return self._decide_document(created.file.file_id)
        return again

    def _poll(self) -> LockStatus:
        if self._mode != MODE_EDITABLE or not self._token:
            return self._status
        now = self._sample_time()
        if self._activity:
            self._last_activity = now
            self._activity = False
        if self._verified_at is None or now - self._verified_at >= self._stale_after:
            status = self._reverify()
            if not status.can_edit:
                return status
            now = self._verified_at or now
        if (
            not self._dirty
            and self._last_activity is not None
            and now - self._last_activity >= self._idle_after
        ):
            return self._release(closing=False)
        if (
            self._last_heartbeat_at is None
            or now - self._last_heartbeat_at >= self._heartbeat_interval
        ):
            return self._reverify()
        return self._status

    def _resume(self) -> LockStatus:
        if self._dirty:
            return self._status
        reloaded = self._reload_if_needed()
        status = self._start_locked()
        if status.can_edit:
            reloaded = self._reload_if_needed() or reloaded
            status = self._status
        return self._note(status, reloaded)

    def _reload_if_needed(self) -> bool:
        view = self._generations
        if view is None:
            return False
        if view.newest_generation() == view.base_generation():
            return False
        view.reload()
        return True

    def _timer_loop(self) -> None:
        while not self._stop.wait(self._timer_interval):
            try:
                self.poll()
            except Exception:
                self._timer_failed()

    def _timer_failed(self) -> None:
        with self._mutex:
            if self._token:
                self._publish(
                    MODE_UNVERIFIED,
                    "The lock heartbeat stopped. Editing is disabled until the lock is confirmed.",
                    can_edit=False,
                    choices=(CHOICE_REVERIFY,),
                )

    def _locate(self) -> str | LockStatus:
        listing = self._drive.list_children()
        locks = [item for item in listing if item.name == LOCK_FILE_NAME]
        self._snapshots_seen = any(item.generation is not None for item in listing)
        if len(locks) > 1:
            return self._duplicate()
        if not locks:
            return self._missing()
        found = locks[0].file_id
        cached = self._config.lock_file_id
        if cached and cached != found:
            return self._publish(
                MODE_DUPLICATE,
                "The lock file in this folder is not the one saved on this computer. "
                "Nothing was changed. Remove the extra lock file in the Drive website.",
                can_edit=False,
            )
        self._remember_id(found)
        return found

    def _read(self) -> tuple[LockDocument, datetime]:
        if not self._file_id:
            raise LockError(_UNREADABLE)
        payload = self._drive.read_lock(self._file_id)
        current = decode_lock(payload)
        return current, self._sample_time()

    def _sample_time(self) -> datetime:
        when = self._drive.server_time()
        if when.tzinfo is None or when.utcoffset() is None:
            raise LockError("Google Drive did not provide a server time.")
        return when.astimezone(timezone.utc)

    def _fresh(self, document: LockDocument, now: datetime) -> bool:
        if document.heartbeat_at_utc is None:
            return False
        return now - document.heartbeat_at_utc < self._stale_after

    def _remember_id(self, file_id: str) -> None:
        self._file_id = file_id
        if self._config.lock_file_id == file_id:
            return
        self._config.lock_file_id = file_id
        save_local_config(self._config)

    def _held_document(self, now: datetime, token: str) -> LockDocument:
        return LockDocument(
            format_version=LOCK_FORMAT_VERSION,
            state=STATE_HELD,
            session_token=token,
            owner_label=self._owner_label,
            machine_id=self._machine_id,
            machine_label=self._machine_label,
            acquired_at_utc=now,
            heartbeat_at_utc=now,
        )

    def _editable(self, document: LockDocument) -> LockStatus:
        self._mode = MODE_EDITABLE
        return self._publish(
            MODE_EDITABLE,
            f"The lock is held by this window for {self._owner_label}.",
            can_edit=True,
            document=document,
        )

    def _readonly(self, document: LockDocument, *, raced: bool) -> LockStatus:
        holder = _holder_phrase(document)
        when = _human_time(document.heartbeat_at_utc)
        if raced:
            message = (
                "Another session took the lock while this one was confirming it. "
                f"{holder} holds it. This window is read-only, and the lock was not overwritten."
            )
        else:
            message = (
                f"{holder} is editing. The last heartbeat was {when}. "
                "This window is read-only. You can try again."
            )
        return self._publish(
            MODE_READ_ONLY,
            message,
            can_edit=False,
            choices=(CHOICE_TRY_AGAIN,),
            document=document,
        )

    def _stale_offer(self, document: LockDocument) -> LockStatus:
        holder = _holder_phrase(document)
        when = _human_time(document.heartbeat_at_utc)
        return self._publish(
            MODE_STALE,
            (
                f"{holder} is still shown on the lock, but the last heartbeat was {when}, "
                f"which is more than {_STALE_MINUTES} minutes ago. "
                "Confirm that FinanceTracker is closed there before taking the lock. "
                "It was not taken yet."
            ),
            can_edit=False,
            choices=(CHOICE_RECOVER,),
            document=document,
            reviewed=True,
        )

    def _takeover_offer(self, document: LockDocument) -> LockStatus:
        return self._publish(
            MODE_TAKEOVER,
            "This computer already holds the lock, and no other FinanceTracker window "
            "is running. You can take it over now. It was not taken yet.",
            can_edit=False,
            choices=(CHOICE_TAKE_OVER,),
            document=document,
            reviewed=True,
        )

    def _lost(self, document: LockDocument) -> LockStatus:
        self._forget_ownership()
        holder = _holder_phrase(document)
        if document.state == STATE_HELD:
            message = (
                f"{holder} holds the lock. Editing is disabled, and the lock was not overwritten."
            )
        else:
            message = (
                "This window no longer holds the lock. Editing is disabled, "
                "and the lock was not overwritten."
            )
        return self._publish(
            MODE_LOST,
            message,
            can_edit=False,
            choices=(CHOICE_TRY_AGAIN,),
            document=document,
        )

    def _duplicate(self) -> LockStatus:
        return self._publish(
            MODE_DUPLICATE,
            "More than one lock file is in this folder. The owner of the extra file "
            "must remove it in the Drive website. Neither file was changed.",
            can_edit=False,
        )

    def _missing(self) -> LockStatus:
        if self._snapshots_seen:
            message = (
                "The lock file is missing, and snapshots are in the folder. "
                "The lock was not recreated. You can recreate it, and the folder "
                "will be checked again so a second lock file is not ignored."
            )
        else:
            message = "The lock file is missing. Nothing was created."
        return self._publish(
            MODE_MISSING,
            message,
            can_edit=False,
            choices=(CHOICE_CREATE_LOCK,),
        )

    def _changed(self) -> LockStatus:
        return self._publish(
            MODE_CHANGED,
            "The lock changed after it was shown. It was not taken. "
            "Look at the current lock and try again.",
            can_edit=False,
            choices=(CHOICE_TRY_AGAIN,),
        )

    def _other_window(self) -> LockStatus:
        return self._publish(
            MODE_READ_ONLY,
            "Another FinanceTracker window is already open on this computer. "
            "This window is read-only.",
            can_edit=False,
        )

    def _needs_reverify(self, message: str) -> LockStatus:
        self._mode = MODE_UNVERIFIED
        return self._publish(
            MODE_UNVERIFIED,
            message,
            can_edit=False,
            choices=(CHOICE_REVERIFY,),
        )

    def _unverified(self, exc: DriveError) -> LockStatus:
        self._mode = MODE_UNVERIFIED
        text = str(exc).strip() or "The lock could not be confirmed."
        return self._publish(
            MODE_UNVERIFIED,
            text,
            can_edit=False,
            choices=(CHOICE_REVERIFY,),
        )

    def _unavailable(self, exc: DriveError) -> LockStatus:
        self._mode = MODE_UNAVAILABLE
        return self._unavailable_message(str(exc))

    def _unavailable_message(self, message: str) -> LockStatus:
        self._mode = MODE_UNAVAILABLE
        return self._publish(MODE_UNAVAILABLE, message, can_edit=False, choices=(CHOICE_TRY_AGAIN,))

    def _forget_ownership(self) -> None:
        self._token = None
        self._verified_at = None
        self._last_heartbeat_at = None
        self._clear_session_file()

    def _note(self, status: LockStatus, reloaded: bool) -> LockStatus:
        if not reloaded:
            return status
        noted = replace(
            status,
            reloaded=True,
            message=status.message + " The data was updated from the other computer.",
        )
        self._status = noted
        return noted

    def _write_session_file(self, token: str) -> None:
        payload = {
            "format_version": _SESSION_VERSION,
            "machine_id": self._machine_id,
            "session_token": token,
        }
        text = json.dumps(payload, indent=2) + "\n"
        try:
            _replace_text(self._session_path, text)
        except OSError:
            return

    def _clear_session_file(self) -> None:
        try:
            self._session_path.unlink(missing_ok=True)
        except OSError:
            return

    def _make(
        self,
        mode: str,
        message: str,
        *,
        can_edit: bool,
        choices: tuple[str, ...] = (),
    ) -> LockStatus:
        return LockStatus(
            mode=mode,
            message=message,
            can_edit=can_edit,
            choices=choices,
            lock_file_id=self._file_id,
        )

    def _publish(
        self,
        mode: str,
        message: str,
        *,
        can_edit: bool,
        choices: tuple[str, ...] = (),
        document: LockDocument | None = None,
        reviewed: bool = False,
    ) -> LockStatus:
        status = LockStatus(
            mode=mode,
            message=message,
            can_edit=can_edit,
            choices=choices,
            owner_label=document.owner_label if document else "",
            machine_id=document.machine_id if document else "",
            machine_label=_display_machine(document.machine_label) if document else "",
            heartbeat_at_utc=document.heartbeat_at_utc if document else None,
            reviewed_token=document.session_token if document and reviewed else "",
            lock_file_id=self._file_id,
        )
        self._mode = mode
        self._status = status
        return status


def _real_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _require_document(document: LockDocument) -> None:
    if document.format_version != LOCK_FORMAT_VERSION:
        raise LockError("Refusing to write a lock file of an unknown format.")
    if document.state not in (STATE_HELD, STATE_FREE):
        raise LockError("Refusing to write a lock file in this state.")
    if document.state == STATE_HELD:
        if _TOKEN.fullmatch(document.session_token) is None:
            raise LockError("Refusing to write a lock file without a session token.")
        if document.owner_label not in USERS:
            raise LockError("Refusing to write a lock file for this owner.")
        if _MACHINE_ID.fullmatch(document.machine_id) is None:
            raise LockError("Refusing to write a lock file without a machine id.")
        if document.acquired_at_utc is None or document.heartbeat_at_utc is None:
            raise LockError("Refusing to write a lock file without a time.")
    elif document.session_token not in ("",):
        raise LockError("Refusing to write a free lock that still has a session token.")


def _text_field(value: object, *, required: bool) -> str:
    if value in ("", None):
        if required:
            raise LockError(_UNREADABLE)
        return ""
    if not isinstance(value, str) or len(value) > 80 or _controls(value):
        raise LockError(_UNREADABLE)
    return value


def _controls(value: str) -> bool:
    return any(ord(character) < 32 for character in value)


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise LockError(_UNREADABLE)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise LockError(_UNREADABLE) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LockError(_UNREADABLE)
    return parsed.astimezone(timezone.utc)


def _stamp(when: datetime | None) -> str | None:
    if when is None:
        return None
    if when.tzinfo is None or when.utcoffset() is None:
        raise LockError("A lock time must include a time zone.")
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _human_time(when: datetime | None) -> str:
    if when is None:
        return "an unknown time"
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _holder_phrase(document: LockDocument) -> str:
    who = document.owner_label if document.owner_label in USERS else "Someone else"
    where = _display_machine(document.machine_label)
    return f"{who} on {where}"


def _display_machine(label: str) -> str:
    if _DISPLAY_LABEL.fullmatch(label or "") is None:
        return "another computer"
    return label


def _lock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            return
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _replace_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
