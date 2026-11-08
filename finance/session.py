"""UI-independent finance session: passphrase, lock, snapshots, and migration.

The window uses this module once a Drive folder is saved on the computer.
The CLI does not call it, except to notice that cloud mode is on and refuse
to run.

Startup order, once a Drive connection and a passphrase are available:

1. local config
2. the Drive folder can be opened
3. the newest valid snapshot, still read-only (a wrong passphrase stops here)
4. the lock
5. if this session took the lock, reload when Drive has a newer generation

Only the editable mode can change records. Read-only, paused, offline, and
access-lost cannot. Saves upload a new encrypted snapshot. They do not write
the legacy plaintext file. Restoring a backup uploads it as the next
generation and leaves the previous snapshot in place until retention runs.

Cloud mode is turned on only after a verified snapshot and the permanent lock
file both exist. A retry reuses that lock file. The original plaintext file
is never modified.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import USERS, default_data_path
from .crypto import CryptoError, decrypt, read_unverified_metadata
from .drive_adapter import (
    LOCK_FILE_NAME,
    DriveAdapter,
    DriveAuthError,
    DriveError,
    DrivePermissionError,
    DriveUnavailable,
    generation_of_name,
)
from .drive_folder import is_drive_item_id
from .lock import (
    ACQUIRE_CONFIRMATION_DELAY,
    CHOICE_RECOVER,
    CHOICE_TAKE_OVER,
    CHOICE_TRY_AGAIN,
    IDLE_RELEASE_AFTER,
    MODE_CLOSED,
    MODE_DUPLICATE,
    MODE_EDITABLE,
    MODE_LOST,
    MODE_MISSING,
    MODE_PAUSED,
    MODE_READ_ONLY,
    MODE_UNAVAILABLE,
    MODE_UNREADABLE,
    MODE_UNVERIFIED,
    LockSession,
    LockStatus,
    blank_lock,
    encode_lock,
)
from .profile import (
    ConfigError,
    app_config_dir,
    enable_cloud_mode,
    encrypted_snapshots_dir,
    ensure_machine_identity,
    known_valid_cache_path,
    load_local_config,
    save_local_config,
)
from .snapshots import (
    CHOICE_CANCEL,
    CHOICE_RELOAD_DISCARD,
    CHOICE_RETRY_PASSPHRASE,
    OUTCOME_CONFLICT,
    OUTCOME_EMPTY,
    OUTCOME_LOADED,
    OUTCOME_REFUSED,
    OUTCOME_SAVED,
    OUTCOME_UNAVAILABLE,
    OUTCOME_UNCHANGED,
    OUTCOME_UNREADABLE,
    LoadResult,
    LocalEncryptedCopies,
    RecoveryCatalog,
    SaveResult,
    SkippedSnapshot,
    SnapshotError,
    SnapshotRepository,
    local_recovery_catalog,
    recovery_catalog_uncached,
)
from .store import FinanceStore

MODE_OFFLINE = "offline"
MODE_ACCESS_LOST = "access_lost"
MODE_SETUP = "setup"

CHOICE_IMPORT = "import_legacy"
CHOICE_CREATE_EMPTY = "create_empty"
CHOICE_RESTORE_LOCAL = "restore_local"
CHOICE_RETRY_SAVE = "retry_save"
CHOICE_DISCARD = "discard_unsaved"
CHOICE_CANCEL_CLOSE = "cancel_close"

_PASSPHRASE_MISMATCH = (
    "The two passphrases did not match. Nothing was uploaded and the local file was not changed."
)
_PASSPHRASE_WARNING = (
    "A forgotten passphrase makes the data and every backup permanently unreadable. "
    "Store it in a password manager. Changing the passphrase is not supported."
)
_OFFLINE_MARKERS = (
    "could not be reached",
    "did not answer",
    "limiting requests",
    "Check the network",
)
_LOCK_BLOCKED = (MODE_DUPLICATE, MODE_MISSING, MODE_UNREADABLE)


class SessionError(Exception):
    """The session cannot do that. The message is safe to show."""


@dataclass(frozen=True)
class SessionStatus:
    """What the caller can do next. Messages do not include finance records."""

    mode: str
    message: str
    can_edit: bool
    choices: tuple[str, ...] = ()
    generation: int | None = None
    base_generation: int = 0
    dirty: bool = False
    reloaded: bool = False
    legacy_notice: str = ""
    backup_path: str | None = None
    data_time: str | None = None
    lock_mode: str = ""

    def __repr__(self) -> str:
        return (
            f"SessionStatus(mode={self.mode!r}, can_edit={self.can_edit!r}, "
            f"generation={self.generation!r}, dirty={self.dirty!r})"
        )


class _SessionStore(FinanceStore):
    """In-memory store. ``save`` never writes a file."""

    def __init__(
        self,
        path: Path,
        spending,
        income,
        next_id: int,
        allows_edit: Callable[[], bool],
        changed: Callable[[], None],
    ) -> None:
        super().__init__(path, spending, income, next_id)
        self._allows_edit = allows_edit
        self._changed = changed

    @classmethod
    def adopt(
        cls,
        source: FinanceStore,
        path: Path,
        allows_edit: Callable[[], bool],
        changed: Callable[[], None],
    ) -> _SessionStore:
        clone = FinanceStore.from_json_bytes(source.to_json_bytes(), path)
        return cls(path, clone.spending, clone.income, clone._next_id, allows_edit, changed)

    def save(self) -> None:
        raise SessionError(
            "This dataset is saved as an encrypted snapshot. The local finance file was not written."
        )

    def add_spending(self, amount, category, on_date, user, note="", recorded_on=None):
        self._require_edit()
        record = super().add_spending(
            amount, category, on_date, user, note, recorded_on=recorded_on
        )
        self._changed()
        return record

    def add_income(self, amount, on_date, user, note="", recorded_on=None):
        self._require_edit()
        record = super().add_income(amount, on_date, user, note, recorded_on=recorded_on)
        self._changed()
        return record

    def update_record(self, record_id, record_type, amount, on_date, note="", category=""):
        self._require_edit()
        record = super().update_record(
            record_id, record_type, amount, on_date, note=note, category=category
        )
        self._changed()
        return record

    def delete_record(self, record_id):
        self._require_edit()
        record = super().delete_record(record_id)
        self._changed()
        return record

    def _require_edit(self) -> None:
        if not self._allows_edit():
            raise SessionError("Editing is disabled. Nothing was changed.")


class FinanceSession:
    """One computer's cloud session. Drive calls go through the injected adapter."""

    def __init__(
        self,
        drive: DriveAdapter,
        *,
        folder_id: str,
        owner_label: str,
        legacy_path: Path | None = None,
        memory_path: Path | None = None,
        local_dir: Path | None = None,
        cache_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        confirmation_delay: timedelta = ACQUIRE_CONFIRMATION_DELAY,
        idle_after: timedelta = IDLE_RELEASE_AFTER,
    ) -> None:
        if owner_label not in USERS:
            raise SessionError("The lock owner must be Boris or Milena.")
        if not is_drive_item_id(folder_id):
            raise SessionError("The Drive folder id cannot be used.")
        self._drive = drive
        self._folder_id = folder_id
        self._owner_label = owner_label
        self._legacy_path = Path(legacy_path) if legacy_path is not None else default_data_path()
        self._memory_path = (
            Path(memory_path) if memory_path is not None else app_config_dir() / "snapshot-memory.json"
        )
        if self._memory_path.resolve() == self._legacy_path.resolve():
            raise SessionError(
                "The session cannot use the legacy finance file as its memory path."
            )
        self._local_dir = Path(local_dir) if local_dir is not None else encrypted_snapshots_dir()
        self._cache_path = Path(cache_path) if cache_path is not None else known_valid_cache_path()
        self._clock = clock
        self._sleep = sleep
        self._confirmation_delay = confirmation_delay
        self._idle_after = idle_after
        self._repo: SnapshotRepository | None = None
        self._lock: LockSession | None = None
        self._store: _SessionStore | None = None
        self._committed: bytes | None = None
        self._generation: int | None = None
        self._snapshot_id: str | None = None
        self._data_time: str | None = None
        self._skipped: tuple[SkippedSnapshot, ...] = ()
        self._legacy_notice = ""
        self._reviewed: LockStatus | None = None
        self._block_message = ""
        self._closed = False
        self._mode = MODE_SETUP
        self._status = SessionStatus(
            mode=MODE_SETUP,
            message="The session has not opened a dataset.",
            can_edit=False,
        )
        self._ensure_config()

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def store(self) -> FinanceStore | None:
        return self._store

    @property
    def can_edit(self) -> bool:
        return self._mode == MODE_EDITABLE and not self._closed

    @property
    def dirty(self) -> bool:
        if self._store is None:
            return False
        if self._committed is None:
            return True
        return self._store.to_json_bytes() != self._committed

    @property
    def generation(self) -> int | None:
        return self._generation

    def open_with_passphrase(self, passphrase: str) -> SessionStatus:
        """Load the newest snapshot, then try to take the lock.

        A wrong passphrase returns before the lock is taken. Drive being
        unreachable opens the local encrypted copy read-only, when one exists.
        """
        if self._closed:
            return self._publish(MODE_CLOSED, "This session is closed.", can_edit=False)
        if self.dirty:
            return self._publish(
                self._mode,
                "Unsaved changes are still here. Save or discard them before opening again.",
                can_edit=self.can_edit,
                choices=(CHOICE_RETRY_SAVE, CHOICE_DISCARD, CHOICE_CANCEL_CLOSE),
            )
        if not isinstance(passphrase, str) or passphrase == "":
            return self._publish(
                MODE_SETUP,
                "A passphrase is required.",
                can_edit=False,
                choices=(CHOICE_RETRY_PASSPHRASE,),
            )
        self._ensure_config()
        unreachable = self._startup_drive_problem(passphrase)
        if unreachable is not None:
            return unreachable
        try:
            self._repo = self._new_repo(passphrase)
            loaded = self._repo.load_latest()
        except SnapshotError as exc:
            self._repo = None
            return self._publish(
                MODE_SETUP,
                str(exc),
                can_edit=False,
                choices=(CHOICE_RETRY_PASSPHRASE,),
            )
        if loaded.outcome == OUTCOME_UNAVAILABLE:
            if _means_offline(loaded.message):
                return self._open_offline(passphrase, loaded.message)
            return self._publish(
                MODE_ACCESS_LOST,
                loaded.message,
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN,),
            )
        if loaded.outcome == OUTCOME_EMPTY:
            return self._setup_status()
        if loaded.outcome == OUTCOME_UNREADABLE:
            self._repo = None
            self._store = None
            self._committed = None
            return self._publish(
                MODE_SETUP,
                loaded.message,
                can_edit=False,
                choices=loaded.choices or (CHOICE_RETRY_PASSPHRASE, CHOICE_CANCEL),
            )
        if loaded.outcome != OUTCOME_LOADED or loaded.store is None:
            self._repo = None
            return self._publish(
                MODE_ACCESS_LOST,
                loaded.message,
                can_edit=False,
                choices=(CHOICE_CANCEL,),
            )
        self._adopt(loaded, notice=True)
        return self._take_lock_after_load()

    def import_legacy(
        self,
        passphrase: str,
        confirmation: str,
        backup_dir: Path,
    ) -> SessionStatus:
        """Copy the legacy file aside, then upload it as generation 1.

        The source file is only read. A mismatched passphrase stops before
        that read. Cloud mode stays off until the upload is verified and the
        lock file exists.
        """
        if self._closed:
            return self._publish(
                MODE_CLOSED, "This session is closed. Nothing was imported.", can_edit=False
            )
        mismatched = self._passphrase_mismatch(passphrase, confirmation)
        if mismatched is not None:
            return mismatched
        try:
            if load_local_config().cloud_mode:
                return self._publish(
                    MODE_SETUP,
                    "Cloud mode is already on. The local file was not imported.",
                    can_edit=False,
                    choices=(CHOICE_CANCEL,),
                )
        except ConfigError as exc:
            return self._publish(MODE_ACCESS_LOST, str(exc), can_edit=False)
        if not self._legacy_path.is_file():
            return self._publish(
                MODE_SETUP,
                "There is no local finance file to import. Nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_CREATE_EMPTY, CHOICE_CANCEL),
            )
        try:
            original = self._legacy_path.read_bytes()
            parsed = FinanceStore.from_json_bytes(original, self._memory_path)
        except Exception:
            return self._publish(
                MODE_SETUP,
                "The local finance file could not be read. It was not changed, and nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_CANCEL,),
            )
        self._ensure_config()
        problem = self._require_drive()
        if problem is not None:
            return problem
        try:
            if self._remote_snapshot_count() > 0:
                return self._publish(
                    MODE_SETUP,
                    "This Drive folder already has a snapshot. The local file was not imported.",
                    can_edit=False,
                    choices=(CHOICE_CANCEL,),
                )
        except DriveError as exc:
            return self._publish(
                MODE_ACCESS_LOST,
                str(exc),
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN,),
            )
        try:
            backup = self._write_backup(original, Path(backup_dir))
        except SessionError as exc:
            return self._publish(MODE_SETUP, str(exc), can_edit=False, choices=(CHOICE_CANCEL,))
        except OSError:
            return self._publish(
                MODE_SETUP,
                "The plaintext backup could not be written. Nothing was uploaded, "
                "and the local file was not changed.",
                can_edit=False,
                choices=(CHOICE_CANCEL,),
            )
        return self._upload_dataset(passphrase, parsed, backup)

    def create_empty(self, passphrase: str, confirmation: str) -> SessionStatus:
        """Create a new empty dataset after the caller confirms the passphrase twice."""
        if self._closed:
            return self._publish(
                MODE_CLOSED, "This session is closed. Nothing was created.", can_edit=False
            )
        mismatched = self._passphrase_mismatch(passphrase, confirmation)
        if mismatched is not None:
            return mismatched
        try:
            if load_local_config().cloud_mode:
                return self._publish(
                    MODE_SETUP,
                    "Cloud mode is already on. An empty dataset was not created.",
                    can_edit=False,
                    choices=(CHOICE_CANCEL,),
                )
        except ConfigError as exc:
            return self._publish(MODE_ACCESS_LOST, str(exc), can_edit=False)
        if self._legacy_path.is_file():
            return self._publish(
                MODE_SETUP,
                "A local finance file is on this computer. It was not replaced. "
                "Import it, or move it aside before creating an empty dataset. Nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_IMPORT, CHOICE_CANCEL),
            )
        self._ensure_config()
        problem = self._require_drive()
        if problem is not None:
            return problem
        try:
            if self._remote_snapshot_count() > 0:
                return self._publish(
                    MODE_SETUP,
                    "This Drive folder already has a snapshot. An empty dataset was not created.",
                    can_edit=False,
                    choices=(CHOICE_CANCEL,),
                )
        except DriveError as exc:
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        return self._upload_dataset(passphrase, FinanceStore(self._memory_path), None)

    def list_backups(self) -> RecoveryCatalog:
        """Drive snapshots and local encrypted copies, newest first.

        Record counts come from the snapshot header and Drive owners. This does
        not decrypt every file and does not include notes.
        """
        if self._repo is not None:
            try:
                return self._repo.recovery_catalog(
                    current_file_id=self._snapshot_id,
                    current_generation=self._generation,
                )
            except DriveError as exc:
                return local_recovery_catalog(
                    self._local_dir,
                    current_generation=self._generation,
                    message=str(exc) + " Showing encrypted copies on this computer.",
                )
        try:
            return recovery_catalog_uncached(
                self._drive,
                self._local_dir,
                current_file_id=self._snapshot_id,
                current_generation=self._generation,
            )
        except DriveError as exc:
            return local_recovery_catalog(
                self._local_dir,
                current_generation=self._generation,
                message=str(exc) + " Showing encrypted copies on this computer.",
            )

    def restore_backup(self, key: str) -> SessionStatus:
        """Upload the selected snapshot's data as the next generation.

        The copy that is current stays in Drive until the new snapshot is
        verified. A dirty session, or one that does not hold the lock, is
        refused before anything is uploaded.
        """
        if self._closed:
            return self._publish(
                MODE_CLOSED, "This session is closed. Nothing was changed.", can_edit=False
            )
        if (
            self._mode != MODE_EDITABLE
            or self._store is None
            or self._repo is None
            or self._lock is None
        ):
            return self._publish(
                self._mode,
                "Restore needs the editing lock. Nothing was changed.",
                can_edit=False,
                choices=self._status.choices,
            )
        if self.dirty:
            return self._publish(
                MODE_EDITABLE,
                "Unsaved changes are still here. Save or discard them before restoring. "
                "Nothing was changed.",
                can_edit=True,
                choices=(CHOICE_RETRY_SAVE, CHOICE_RELOAD_DISCARD),
            )
        try:
            plaintext = self._repo.plaintext_for_recovery(key)
        except SnapshotError as exc:
            return self._publish(MODE_EDITABLE, str(exc), can_edit=True)
        if self._committed is not None and plaintext == self._committed:
            return self._publish(
                MODE_EDITABLE,
                "This is already the latest saved data. Nothing was uploaded.",
                can_edit=True,
            )
        confirmed = self._lock.confirm_for_save()
        if not confirmed.can_edit:
            return self._from_lock(confirmed)
        previous = self._store
        restored = FinanceStore.from_json_bytes(plaintext, self._memory_path)
        self._store = _SessionStore.adopt(
            restored, self._memory_path, self._allows_edit, self._after_mutation
        )
        self._lock.set_dirty(True)
        try:
            result = self._repo.save(self._store)
        except SnapshotError as exc:
            self._store = previous
            self._lock.set_dirty(False)
            return self._publish(
                MODE_EDITABLE,
                str(exc) + " The previous snapshot was left in place.",
                can_edit=True,
            )
        if result.outcome == OUTCOME_REFUSED:
            self._store = previous
            self._lock.set_dirty(False)
            return self._publish(
                MODE_EDITABLE,
                "Drive has a newer snapshot, so this restore was not uploaded. "
                "The data on screen is unchanged.",
                can_edit=True,
                choices=(CHOICE_RELOAD_DISCARD,),
            )
        accepted = result.outcome in (OUTCOME_SAVED, OUTCOME_CONFLICT) and result.adopted
        if not accepted:
            self._store = previous
            self._lock.set_dirty(False)
            message = result.message
            if "left in place" not in message.lower() and "not trashed" not in message.lower():
                message += " The previous snapshot was left in place."
            return self._publish(
                MODE_EDITABLE, message, can_edit=True, choices=result.choices
            )
        return self._apply_save(result)

    def restore_local_copy(self) -> SessionStatus:
        """Upload the newest local encrypted copy when the Drive folder has no snapshot."""
        if self._closed:
            return self._publish(
                MODE_CLOSED, "This session is closed. Nothing was uploaded.", can_edit=False
            )
        if self._repo is None:
            return self._publish(
                MODE_SETUP,
                "Enter the passphrase before restoring a local copy. Nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_RETRY_PASSPHRASE, CHOICE_CANCEL),
            )
        self._ensure_config()
        problem = self._require_drive()
        if problem is not None:
            return problem
        try:
            if self._remote_snapshot_count() > 0:
                return self._publish(
                    MODE_SETUP,
                    "This Drive folder already has a snapshot. The local copy was not uploaded.",
                    can_edit=False,
                    choices=(CHOICE_CANCEL,),
                )
        except DriveError as exc:
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        try:
            opened = self._repo.open_newest_local()
        except SnapshotError as exc:
            return self._publish(
                MODE_SETUP,
                str(exc),
                can_edit=False,
                choices=self._status.choices or (CHOICE_RETRY_PASSPHRASE, CHOICE_CANCEL),
            )
        if opened is None:
            return self._publish(
                MODE_SETUP,
                "There is no local encrypted copy to restore. Nothing was uploaded.",
                can_edit=False,
                choices=self._status.choices or (CHOICE_CREATE_EMPTY, CHOICE_CANCEL),
            )
        store, source_generation = opened
        status = self._install_local_dataset(store)
        if (
            status.mode == MODE_EDITABLE
            and status.can_edit
            and not status.dirty
            and status.message.startswith("Saved generation")
        ):
            return self._publish(
                status.mode,
                status.message
                + f" The data came from the local encrypted copy of generation {source_generation}.",
                can_edit=True,
                choices=status.choices,
                reloaded=status.reloaded,
                backup_path=status.backup_path,
            )
        return status

    def save(self) -> SessionStatus:
        """Upload one new snapshot when this session still holds the lock and something changed."""
        if self._closed:
            return self._publish(MODE_CLOSED, "This session is closed. Nothing was saved.", can_edit=False)
        if (
            self._mode != MODE_EDITABLE
            or self._store is None
            or self._repo is None
            or self._lock is None
        ):
            return self._publish(
                self._mode,
                "Editing is disabled. Nothing was saved.",
                can_edit=False,
                choices=self._status.choices,
            )
        if not self.dirty:
            return self._publish(
                MODE_EDITABLE,
                "Nothing has changed since the last saved snapshot.",
                can_edit=True,
            )
        self._lock.set_dirty(True)
        confirmed = self._lock.confirm_for_save()
        if not confirmed.can_edit:
            return self._from_lock(confirmed)
        try:
            result = self._repo.save(self._store)
        except SnapshotError as exc:
            return self._publish(
                MODE_EDITABLE,
                str(exc),
                can_edit=True,
                choices=(CHOICE_RETRY_SAVE, CHOICE_RELOAD_DISCARD),
            )
        return self._apply_save(result)

    def reload_discard(self) -> SessionStatus:
        """Drop unsaved changes and load the newest Drive snapshot again."""
        if self._repo is None:
            return self._publish(
                self._mode, "There is no Drive snapshot to reload.", can_edit=False
            )
        if self._lock is not None:
            self._lock.set_dirty(False)
        try:
            loaded = self._repo.reload()
        except DriveError as exc:
            if self._lock is not None:
                self._lock.set_dirty(self.dirty)
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        if loaded.outcome != OUTCOME_LOADED or loaded.store is None:
            return self._publish(
                MODE_ACCESS_LOST,
                loaded.message,
                can_edit=False,
                choices=loaded.choices or (CHOICE_CANCEL,),
            )
        self._adopt(loaded, notice=True)
        if self._lock is not None:
            self._lock.set_dirty(False)
            return self._from_lock(self._lock.status, reloaded=True)
        return self._publish(self._mode, loaded.message, can_edit=False, reloaded=True)

    def close(self, *, discard: bool = False) -> SessionStatus:
        """Release the lock when nothing unsaved remains.

        A dirty session stays open and keeps the lock until the caller saves,
        discards, or cancels the close.
        """
        if self._closed:
            return self._status
        if self.dirty and not discard:
            return self._publish(
                self._mode,
                "Unsaved changes are still here. The lock was not released.",
                can_edit=self._mode == MODE_EDITABLE,
                choices=(CHOICE_RETRY_SAVE, CHOICE_DISCARD, CHOICE_CANCEL_CLOSE),
            )
        if discard and self.dirty:
            self._restore_committed()
            if self._lock is not None:
                self._lock.set_dirty(False)
        if self._lock is not None:
            self._lock.close()
        self._closed = True
        return self._publish(MODE_CLOSED, "The session is closed.", can_edit=False)

    def poll(self) -> SessionStatus:
        """Heartbeat, or release the lock when the session is idle and clean."""
        if self._closed or self._lock is None:
            return self._status
        self._sync_dirty()
        return self._from_lock(self._lock.poll())

    def resume(self) -> SessionStatus:
        """Take the lock again after idle. Reload first when Drive has a newer generation."""
        if self._closed or self._lock is None:
            return self._status
        self._sync_dirty()
        if self.dirty:
            return self._publish(
                self._mode,
                "Unsaved changes are still here. Save or discard them before resuming.",
                can_edit=self._mode == MODE_EDITABLE,
                choices=(CHOICE_RETRY_SAVE, CHOICE_DISCARD),
            )
        status = self._lock.resume()
        if status.can_edit:
            self._maybe_enable()
        return self._from_lock(status)

    def note_activity(self) -> None:
        """The GUI reports a key or a click. Idle release uses the next poll."""
        if self._lock is not None and not self._closed:
            self._lock.note_activity()

    def retry_lock(self) -> SessionStatus:
        """Try to take the lock again. Reload when Drive changed while this session waited."""
        if self._closed or self._repo is None or self._store is None:
            return self._status
        self._ensure_lock_session()
        return self._after_lock_attempt(self._lock.start())

    def create_missing_lock(self) -> SessionStatus:
        """Create the one lock file after the caller confirms, then take it."""
        if self._closed or self._repo is None or self._lock is None:
            return self._publish(
                MODE_SETUP,
                "Open the dataset before creating a lock file.",
                can_edit=False,
            )
        return self._after_lock_attempt(self._lock.create_missing_lock())

    def recover_lock(self) -> SessionStatus:
        """Take a stale lock, or this computer's own lock, after it was shown."""
        if self._closed or self._lock is None or self._reviewed is None:
            return self._publish(
                self._mode, "There is no lock waiting to be taken.", can_edit=False
            )
        return self._after_lock_attempt(self._lock.recover(self._reviewed))

    def base_generation(self) -> int:
        if self._repo is None:
            return 0
        return self._repo.base_generation()

    def newest_generation(self) -> int:
        if self._repo is None:
            return 0
        return self._repo.newest_generation()

    def reload(self) -> int:
        """GenerationView hook used by the lock. Must not call back into the lock."""
        if self._repo is None:
            return 0
        loaded = self._repo.reload()
        if loaded.outcome == OUTCOME_LOADED and loaded.store is not None:
            self._adopt(loaded, notice=True)
        return self.base_generation()

    def _take_lock_after_load(self) -> SessionStatus:
        self._ensure_lock_session()
        assert self._lock is not None
        return self._after_lock_attempt(self._lock.start())

    def _after_lock_attempt(self, status: LockStatus) -> SessionStatus:
        reloaded = False
        if status.can_edit:
            changed = self._reload_if_generation_changed()
            if changed == "lost":
                return self._publish(
                    MODE_ACCESS_LOST,
                    self._block_message,
                    can_edit=False,
                    choices=(CHOICE_TRY_AGAIN,),
                )
            reloaded = changed == "reloaded"
            assert self._lock is not None
            status = self._lock.status
        if status.mode not in _LOCK_BLOCKED:
            self._maybe_enable()
        return self._from_lock(status, reloaded=reloaded)

    def _reload_if_generation_changed(self) -> str:
        if self._repo is None:
            return "same"
        try:
            newest = self._repo.newest_generation()
        except DriveError as exc:
            self._block_message = str(exc)
            return "lost"
        if newest == self._repo.base_generation():
            return "same"
        try:
            loaded = self._repo.reload()
        except DriveError as exc:
            self._block_message = str(exc)
            return "lost"
        if loaded.outcome != OUTCOME_LOADED or loaded.store is None:
            self._block_message = loaded.message
            return "lost"
        self._adopt(loaded, notice=True)
        return "reloaded"

    def _install_local_dataset(self, source: FinanceStore) -> SessionStatus:
        """Create or reuse the lock, then upload ``source`` when the folder is still empty."""
        ensured = self._ensure_lock_file()
        if isinstance(ensured, SessionStatus):
            return ensured
        self._ensure_lock_session()
        assert self._lock is not None and self._repo is not None
        status = self._lock.start()
        if not status.can_edit:
            return self._from_lock(status)
        try:
            loaded = self._repo.load_latest()
        except (SnapshotError, DriveError) as exc:
            return self._publish(
                MODE_EDITABLE,
                str(exc),
                can_edit=True,
                choices=(CHOICE_RETRY_SAVE, CHOICE_CANCEL),
            )
        if loaded.outcome == OUTCOME_LOADED and loaded.store is not None:
            self._adopt(loaded, notice=True)
            self._maybe_enable()
            return self._publish(
                MODE_EDITABLE,
                "A snapshot was already in the folder. The local copy was not uploaded over it.",
                can_edit=True,
            )
        if loaded.outcome != OUTCOME_EMPTY:
            return self._publish(
                MODE_EDITABLE,
                loaded.message,
                can_edit=True,
                choices=loaded.choices or (CHOICE_CANCEL,),
            )
        self._store = _SessionStore.adopt(
            source, self._memory_path, self._allows_edit, self._after_mutation
        )
        self._committed = None
        self._generation = None
        self._snapshot_id = None
        self._data_time = None
        self._legacy_notice = self._notice_for_legacy()
        self._lock.set_dirty(True)
        try:
            result = self._repo.save(self._store)
        except SnapshotError as exc:
            return self._publish(
                MODE_EDITABLE,
                str(exc),
                can_edit=True,
                choices=(CHOICE_RETRY_SAVE, CHOICE_CANCEL),
            )
        return self._apply_save(result)

    def _upload_dataset(
        self,
        passphrase: str,
        source: FinanceStore,
        backup: Path | None,
    ) -> SessionStatus:
        backup_text = None if backup is None else str(backup)
        ensured = self._ensure_lock_file()
        if isinstance(ensured, SessionStatus):
            return self._with_backup(ensured, backup_text)
        self._ensure_lock_session()
        assert self._lock is not None
        status = self._lock.start()
        if not status.can_edit:
            return self._with_backup(self._from_lock(status), backup_text)
        try:
            self._repo = self._new_repo(passphrase)
            loaded = self._repo.load_latest()
        except (SnapshotError, DriveError) as exc:
            return self._with_backup(
                self._publish(
                    MODE_EDITABLE,
                    str(exc),
                    can_edit=True,
                    choices=(CHOICE_RETRY_SAVE, CHOICE_CANCEL),
                ),
                backup_text,
            )
        if loaded.outcome == OUTCOME_LOADED and loaded.store is not None:
            self._adopt(loaded, notice=False)
            self._maybe_enable()
            return self._with_backup(
                self._publish(
                    MODE_EDITABLE,
                    "A snapshot was already in the folder. The local file was not imported over it.",
                    can_edit=True,
                ),
                backup_text,
            )
        if loaded.outcome != OUTCOME_EMPTY:
            return self._with_backup(
                self._publish(
                    MODE_EDITABLE,
                    loaded.message,
                    can_edit=True,
                    choices=loaded.choices or (CHOICE_RETRY_SAVE, CHOICE_CANCEL),
                ),
                backup_text,
            )
        self._store = _SessionStore.adopt(
            source, self._memory_path, self._allows_edit, self._after_mutation
        )
        self._committed = None
        self._generation = None
        self._legacy_notice = ""
        self._lock.set_dirty(True)
        try:
            result = self._repo.save(self._store)
        except SnapshotError as exc:
            return self._with_backup(
                self._publish(
                    MODE_EDITABLE,
                    str(exc),
                    can_edit=True,
                    choices=(CHOICE_RETRY_SAVE, CHOICE_CANCEL),
                ),
                backup_text,
            )
        applied = self._apply_save(result)
        if backup_text and applied.can_edit and not applied.dirty:
            applied = self._publish(
                applied.mode,
                applied.message + " The original file was not changed. The backup is still plaintext.",
                can_edit=applied.can_edit,
                choices=applied.choices,
                reloaded=applied.reloaded,
                backup_path=backup_text,
            )
            return applied
        return self._with_backup(applied, backup_text)

    def _apply_save(self, result: SaveResult) -> SessionStatus:
        accepted = result.outcome in (OUTCOME_SAVED, OUTCOME_UNCHANGED, OUTCOME_CONFLICT) and result.adopted
        if accepted and self._store is not None and self._lock is not None:
            self._committed = self._store.to_json_bytes()
            if result.generation is not None:
                self._generation = result.generation
            if result.adopted and (result.current_file_id or result.file_id):
                self._snapshot_id = result.current_file_id or result.file_id
            self._lock.set_dirty(False)
            self._maybe_enable()
            return self._publish(MODE_EDITABLE, result.message, can_edit=True, choices=result.choices)
        if self._lock is not None:
            self._lock.set_dirty(True)
        choices = result.choices or (CHOICE_RETRY_SAVE, CHOICE_RELOAD_DISCARD)
        return self._publish(MODE_EDITABLE, result.message, can_edit=True, choices=choices)

    def _setup_status(self) -> SessionStatus:
        try:
            cloud = load_local_config().cloud_mode
        except ConfigError as exc:
            return self._publish(MODE_ACCESS_LOST, str(exc), can_edit=False)
        try:
            files = self._drive.list_children()
        except DriveError as exc:
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        locks = [item for item in files if item.name == LOCK_FILE_NAME]
        if len(locks) > 1:
            return self._publish(
                MODE_READ_ONLY,
                "More than one lock file is in this folder. The owner of the extra file "
                "must remove it in the Drive website. Nothing was created.",
                can_edit=False,
                choices=(CHOICE_CANCEL,),
                legacy_notice="",
            )
        counts = self._legacy_counts()
        local_names = LocalEncryptedCopies(self._local_dir).names()
        choices: list[str] = []
        parts = [_PASSPHRASE_WARNING]
        if len(locks) == 1:
            parts.append(
                "A lock file is already in this folder and will be reused. "
                "A second one will not be created."
            )
        if counts == "unreadable":
            parts.append("A local finance file was found but could not be read. Nothing was uploaded.")
        elif isinstance(counts, tuple):
            spending, income = counts
            parts.append(
                f"A local finance file has {spending} spending records and {income} income records. "
                "Importing it does not show transaction notes."
            )
            if not cloud:
                choices.append(CHOICE_IMPORT)
        else:
            parts.append(
                "There is no snapshot in this Drive folder and no local finance file on this computer. "
                "If this computer should already have data, the folder or the sharing is probably wrong."
            )
            if not cloud:
                choices.append(CHOICE_CREATE_EMPTY)
        if local_names:
            choices.append(CHOICE_RESTORE_LOCAL)
            parts.append(
                "A local encrypted copy is on this computer. "
                "Restoring it uploads that copy as a new encrypted snapshot and creates the lock file."
            )
        if cloud:
            parts.append(
                "Cloud mode is already on, and this folder has no snapshot. "
                "An empty dataset was not created."
            )
        else:
            parts.append("Nothing has been uploaded.")
        choices.append(CHOICE_CANCEL)
        return self._publish(
            MODE_SETUP,
            " ".join(parts),
            can_edit=False,
            choices=tuple(dict.fromkeys(choices)),
            legacy_notice="",
        )

    def _startup_drive_problem(self, passphrase: str) -> SessionStatus | None:
        try:
            self._drive.describe_folder()
        except DriveUnavailable as exc:
            return self._open_offline(passphrase, str(exc))
        except DrivePermissionError as exc:
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        except DriveAuthError as exc:
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        except DriveError as exc:
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        return None

    def _require_drive(self) -> SessionStatus | None:
        try:
            self._drive.describe_folder()
        except DrivePermissionError as exc:
            return self._publish(
                MODE_ACCESS_LOST,
                str(exc) + " Nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN,),
            )
        except DriveAuthError as exc:
            return self._publish(
                MODE_ACCESS_LOST,
                str(exc) + " Nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN,),
            )
        except DriveUnavailable as exc:
            return self._publish(
                MODE_ACCESS_LOST,
                str(exc) + " Nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN,),
            )
        except DriveError as exc:
            return self._publish(
                MODE_ACCESS_LOST,
                str(exc) + " Nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN,),
            )
        return None

    def _open_offline(self, passphrase: str, drive_message: str) -> SessionStatus:
        self._repo = None
        opened = self._read_local_copy(passphrase)
        if opened == "passphrase":
            self._store = None
            self._committed = None
            return self._publish(
                MODE_SETUP,
                "Google Drive could not be reached. The encrypted copies on this computer "
                "did not open with this passphrase. The local finance file was not used.",
                can_edit=False,
                choices=(CHOICE_RETRY_PASSPHRASE, CHOICE_CANCEL),
            )
        if opened == "malformed":
            self._store = None
            self._committed = None
            return self._publish(
                MODE_ACCESS_LOST,
                "Google Drive could not be reached. The copies on this computer "
                "are not encrypted snapshots. The local finance file was not used.",
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN, CHOICE_CANCEL),
            )
        if not isinstance(opened, SessionStatus):
            return self._publish(
                MODE_ACCESS_LOST,
                drive_message + " There is no local encrypted copy to open. Editing is disabled.",
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN, CHOICE_CANCEL),
            )
        return opened

    def _read_local_copy(self, passphrase: str) -> SessionStatus | str | None:
        names = LocalEncryptedCopies(self._local_dir).names()
        if not names:
            return None
        saw_auth = False
        saw_malformed = False
        for name in names:
            try:
                envelope = (self._local_dir / name).read_bytes()
            except OSError:
                continue
            try:
                header = read_unverified_metadata(envelope)
            except CryptoError:
                saw_malformed = True
                continue
            if header.generation != generation_of_name(name):
                continue
            try:
                decrypted = decrypt(envelope, passphrase)
            except CryptoError:
                saw_auth = True
                continue
            try:
                parsed = FinanceStore.from_json_bytes(decrypted.plaintext, self._memory_path)
            except Exception:
                continue
            if parsed.to_json_bytes() != decrypted.plaintext:
                continue
            self._store = _SessionStore.adopt(
                parsed, self._memory_path, self._allows_edit, self._after_mutation
            )
            self._committed = self._store.to_json_bytes()
            self._generation = decrypted.metadata.generation
            self._data_time = _human_time(decrypted.metadata.created_at_utc)
            self._legacy_notice = self._notice_for_legacy()
            return self._publish(
                MODE_OFFLINE,
                (
                    f"Offline — read-only. This data is generation {decrypted.metadata.generation} "
                    f"from {self._data_time}. Google Drive could not be reached. Editing is disabled."
                ),
                can_edit=False,
                choices=(CHOICE_TRY_AGAIN,),
            )
        if saw_auth:
            return "passphrase"
        if saw_malformed:
            return "malformed"
        return None

    def _ensure_lock_file(self) -> str | SessionStatus:
        try:
            locks = self._lock_files()
        except DriveError as exc:
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        if len(locks) > 1:
            return self._publish(
                MODE_READ_ONLY,
                "More than one lock file is in this folder. The owner of the extra file "
                "must remove it in the Drive website. Nothing was uploaded.",
                can_edit=False,
                choices=(CHOICE_CANCEL,),
            )
        if len(locks) == 1:
            self._remember_lock(locks[0].file_id)
            return locks[0].file_id
        try:
            created = self._drive.create_lock(encode_lock(blank_lock()))
        except DriveError as exc:
            try:
                locks = self._lock_files()
            except DriveError:
                return self._publish(
                    MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
                )
            if len(locks) == 1:
                self._remember_lock(locks[0].file_id)
                return locks[0].file_id
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        self._remember_lock(created.file.file_id)
        try:
            locks = self._lock_files()
        except DriveError as exc:
            return self._publish(
                MODE_ACCESS_LOST, str(exc), can_edit=False, choices=(CHOICE_TRY_AGAIN,)
            )
        ids = {item.file_id for item in locks}
        if len(ids) > 1 or (ids and created.file.file_id not in ids):
            return self._publish(
                MODE_READ_ONLY,
                "More than one lock file is in this folder. Nothing further was uploaded.",
                can_edit=False,
                choices=(CHOICE_CANCEL,),
            )
        return created.file.file_id

    def _lock_files(self):
        return [item for item in self._drive.list_children() if item.name == LOCK_FILE_NAME]

    def _remote_snapshot_count(self) -> int:
        return sum(1 for item in self._drive.list_children() if item.generation is not None)

    def _remember_lock(self, file_id: str) -> None:
        config = load_local_config()
        if config.lock_file_id == file_id and config.drive_folder_id == self._folder_id:
            return
        config.drive_folder_id = self._folder_id
        config.lock_file_id = file_id
        save_local_config(config)

    def _maybe_enable(self) -> None:
        if self._repo is None:
            return
        generation = self._repo.base_generation()
        if generation < 1:
            return
        config = load_local_config()
        if config.cloud_mode:
            return
        if not config.lock_file_id or config.drive_folder_id != self._folder_id:
            return
        if self._lock is not None and self._lock.status.mode in _LOCK_BLOCKED:
            return
        enable_cloud_mode(config, verified_generation=generation)

    def _ensure_lock_session(self) -> None:
        if self._lock is not None:
            return
        kwargs = {
            "owner_label": self._owner_label,
            "generations": self,
            "confirmation_delay": self._confirmation_delay,
            "idle_after": self._idle_after,
        }
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        self._lock = LockSession(self._drive, **kwargs)

    def _ensure_config(self) -> None:
        config = ensure_machine_identity()
        if config.drive_folder_id == self._folder_id:
            return
        if config.cloud_mode and config.drive_folder_id:
            raise SessionError(
                "Cloud mode is on for a different Drive folder. The folder was not changed."
            )
        config.drive_folder_id = self._folder_id
        save_local_config(config)

    def _new_repo(self, passphrase: str) -> SnapshotRepository:
        return SnapshotRepository(
            self._drive,
            passphrase,
            cache_path=self._cache_path,
            local_dir=self._local_dir,
            memory_path=self._memory_path,
            clock=self._clock,
        )

    def _adopt(self, loaded: LoadResult, *, notice: bool) -> None:
        if loaded.store is None:
            return
        self._store = _SessionStore.adopt(
            loaded.store, self._memory_path, self._allows_edit, self._after_mutation
        )
        self._committed = self._store.to_json_bytes()
        self._generation = loaded.generation
        self._snapshot_id = loaded.file_id
        self._skipped = loaded.skipped
        if loaded.metadata is not None:
            self._data_time = _human_time(loaded.metadata.created_at_utc)
        self._legacy_notice = self._notice_for_legacy() if notice else ""

    def _restore_committed(self) -> None:
        if self._committed is None:
            self._store = None
            self._generation = None
            return
        restored = FinanceStore.from_json_bytes(self._committed, self._memory_path)
        self._store = _SessionStore.adopt(
            restored, self._memory_path, self._allows_edit, self._after_mutation
        )

    def _allows_edit(self) -> bool:
        return self.can_edit

    def _after_mutation(self) -> None:
        if self._lock is not None:
            self._lock.set_dirty(self.dirty)

    def _sync_dirty(self) -> None:
        if self._lock is not None:
            self._lock.set_dirty(self.dirty)

    def _passphrase_mismatch(self, passphrase: str, confirmation: str) -> SessionStatus | None:
        if (
            not isinstance(passphrase, str)
            or not isinstance(confirmation, str)
            or passphrase == ""
            or passphrase != confirmation
        ):
            return self._publish(
                MODE_SETUP,
                _PASSPHRASE_MISMATCH,
                can_edit=False,
                choices=(CHOICE_IMPORT, CHOICE_CREATE_EMPTY, CHOICE_CANCEL),
            )
        return None

    def _legacy_counts(self):
        if not self._legacy_path.is_file():
            return None
        try:
            parsed = FinanceStore.from_json_bytes(
                self._legacy_path.read_bytes(), self._memory_path
            )
        except Exception:
            return "unreadable"
        return (len(parsed.spending), len(parsed.income))

    def _notice_for_legacy(self) -> str:
        if not self._legacy_path.is_file():
            return ""
        return (
            "A local finance file was found on this computer and was not imported. "
            "The opened data is the Drive snapshot."
        )

    def _write_backup(self, original: bytes, backup_dir: Path) -> Path:
        directory = backup_dir.expanduser().resolve()
        snapshots = self._local_dir.resolve()
        if directory == snapshots or _is_relative_to(directory, snapshots) or _is_relative_to(snapshots, directory):
            raise SessionError(
                "The backup folder cannot be the local encrypted-snapshot folder. Nothing was uploaded."
            )
        when = self._clock() if self._clock is not None else datetime.now(timezone.utc)
        stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = directory / f"finance_data-{stamp}-{secrets.token_hex(3)}.json"
        if dest.resolve() == self._legacy_path.resolve():
            raise SessionError(
                "The backup would replace the original file. Nothing was uploaded."
            )
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_bytes(dest, original)
        return dest

    def _from_lock(self, status: LockStatus, *, reloaded: bool = False) -> SessionStatus:
        if CHOICE_RECOVER in status.choices or CHOICE_TAKE_OVER in status.choices:
            self._reviewed = status
        else:
            self._reviewed = None
        mode = _mode_for_lock(status)
        message = status.message
        if (reloaded or status.reloaded) and "updated from the other computer" not in message:
            message += " The data was updated from the other computer."
        suffix = self._skipped_suffix()
        if suffix and suffix not in message:
            message += suffix
        return self._publish(
            mode,
            message,
            can_edit=bool(status.can_edit) and mode == MODE_EDITABLE,
            choices=status.choices,
            reloaded=reloaded or status.reloaded,
            lock_mode=status.mode,
        )

    def _skipped_suffix(self) -> str:
        if not self._skipped:
            return ""
        reasons = sorted({item.reason for item in self._skipped})
        return " Skipped snapshots: " + "; ".join(reasons) + "."

    def _with_backup(self, status: SessionStatus, backup_text: str | None) -> SessionStatus:
        if not backup_text or status.backup_path:
            return status
        return self._publish(
            status.mode,
            status.message,
            can_edit=status.can_edit,
            choices=status.choices,
            reloaded=status.reloaded,
            backup_path=backup_text,
            legacy_notice=status.legacy_notice,
            lock_mode=status.lock_mode,
        )

    def _publish(
        self,
        mode: str,
        message: str,
        *,
        can_edit: bool,
        choices: tuple[str, ...] = (),
        reloaded: bool = False,
        backup_path: str | None = None,
        legacy_notice: str | None = None,
        lock_mode: str | None = None,
    ) -> SessionStatus:
        self._mode = mode
        if lock_mode is None and self._lock is not None:
            lock_mode = self._lock.status.mode
        status = SessionStatus(
            mode=mode,
            message=message,
            can_edit=can_edit,
            choices=choices,
            generation=self._generation,
            base_generation=self.base_generation(),
            dirty=self.dirty,
            reloaded=reloaded,
            legacy_notice=self._legacy_notice if legacy_notice is None else legacy_notice,
            backup_path=backup_path,
            data_time=self._data_time,
            lock_mode=lock_mode or "",
        )
        self._status = status
        return status


def _mode_for_lock(status: LockStatus) -> str:
    if status.mode == MODE_EDITABLE and status.can_edit:
        return MODE_EDITABLE
    if status.mode == MODE_PAUSED:
        return MODE_PAUSED
    if status.mode in (MODE_LOST, MODE_UNVERIFIED, MODE_UNAVAILABLE):
        return MODE_ACCESS_LOST
    return MODE_READ_ONLY


def _means_offline(message: str) -> bool:
    return any(marker in message for marker in _OFFLINE_MARKERS)


def _human_time(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _atomic_bytes(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
