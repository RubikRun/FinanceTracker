"""Encrypted snapshots, generation checks, and tiered retention.

``finance.session`` calls this module. The GUI does not call it directly.
Nothing here writes the plaintext finance file. A snapshot is canonical JSON bytes,
encrypted, uploaded as a new file, then downloaded by that file id. It counts
only after the decrypted bytes match the bytes that were encrypted.

Retention keeps the 20 newest known-valid snapshots and the newest known-valid
snapshot of each of the last 30 UTC days, and it never leaves fewer than 5
known-valid snapshots. Pruning moves a file to the trash only when this
account owns it and this computer has already verified it. A file that fails
decryption or validation is left in place.

The passphrase is kept in memory for this object only. It is not written to
the cache, the local copy, or Drive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .crypto import (
    CryptoError,
    DecryptedSnapshot,
    SnapshotMetadata,
    decrypt,
    encrypt,
    read_unverified_metadata,
)
from .drive_adapter import (
    NOT_OWNER_SKIPPED,
    DriveAdapter,
    DriveError,
    DriveFile,
    DriveUnavailable,
    DriveUploadUncertain,
    classify_generation,
    generation_of_name,
)
from .profile import encrypted_snapshots_dir, known_valid_cache_path
from .store import FinanceStore

KEEP_RECENT_SNAPSHOTS = 20
KEEP_DAILY_DAYS = 30
MIN_KNOWN_VALID = 5
LOCAL_ENCRYPTED_COPIES = 10

CACHE_FORMAT_VERSION = 1
_MAX_GENERATION = 999999
_RANDOM_ID = re.compile(r"^[0-9a-f]{12}$")

OUTCOME_SAVED = "saved"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_REFUSED = "refused"
OUTCOME_CONFLICT = "conflict"
OUTCOME_FAILED = "failed"

OUTCOME_LOADED = "loaded"
OUTCOME_EMPTY = "empty"
OUTCOME_UNREADABLE = "unreadable"
OUTCOME_UNAVAILABLE = "unavailable"

CHOICE_RETRY_PASSPHRASE = "retry_passphrase"
CHOICE_INSPECT_RECOVER = "inspect_recover"
CHOICE_CANCEL = "cancel"
CHOICE_RELOAD_DISCARD = "reload_discard"
CHOICE_KEEP_AND_RETRY = "keep_and_retry"

_UNREADABLE_CHOICES = (
    CHOICE_RETRY_PASSPHRASE,
    CHOICE_INSPECT_RECOVER,
    CHOICE_CANCEL,
)
_REFUSED_CHOICES = (CHOICE_RELOAD_DISCARD, CHOICE_KEEP_AND_RETRY)
_FAILED_CHOICES = (CHOICE_KEEP_AND_RETRY, CHOICE_RELOAD_DISCARD)

_NAME_MISMATCH = "The snapshot name does not match its generation."
_INVALID_DOCUMENT = "The snapshot is not a valid finance document."
_BYTE_MISMATCH = "The uploaded snapshot did not match the data that was saved."
_DECRYPT_FAILED = "The snapshot could not be decrypted."
_MALFORMED = "The snapshot envelope could not be read."
_NOT_READY = "Load the latest snapshot before saving."
_EXHAUSTED = "The snapshot generation number is exhausted."
_UNCHANGED = "Nothing has changed since the last saved snapshot."
_CONFLICT_HELD = (
    "A conflicting snapshot is waiting. Both copies were kept. "
    "Reload to continue. Another snapshot was not uploaded."
)


class SnapshotError(Exception):
    """The snapshot store cannot run. The message is safe to show."""


class _InvalidSnapshot(Exception):
    """One snapshot failed validation. The message is a fixed sentence."""


@dataclass(frozen=True)
class SkippedSnapshot:
    """A snapshot that was not used. ``reason`` does not include file bytes."""

    file_id: str
    name: str
    generation: int | None
    reason: str


@dataclass(frozen=True)
class PruneAction:
    """What happened when an older snapshot was considered for trash."""

    file_id: str
    name: str
    status: str


@dataclass(frozen=True)
class SaveResult:
    """The outcome of one save. It does not carry plaintext or the passphrase."""

    outcome: str
    message: str
    generation: int | None = None
    file_id: str | None = None
    base_generation: int | None = None
    remote_generation: int | None = None
    choices: tuple[str, ...] = ()
    current_file_id: str | None = None
    conflict_file_ids: tuple[str, ...] = ()
    prune: tuple[PruneAction, ...] = ()
    included_by_get: tuple[str, ...] = ()
    adopted: bool = False

    def __repr__(self) -> str:
        return (
            f"SaveResult(outcome={self.outcome!r}, generation={self.generation!r}, "
            f"file_id={self.file_id!r})"
        )


@dataclass(frozen=True)
class LoadResult:
    """The outcome of loading. ``store`` is None when nothing was opened."""

    outcome: str
    message: str
    store: FinanceStore | None = None
    base_generation: int = 0
    file_id: str | None = None
    generation: int | None = None
    metadata: SnapshotMetadata | None = None
    skipped: tuple[SkippedSnapshot, ...] = ()
    choices: tuple[str, ...] = ()
    conflict_file_ids: tuple[str, ...] = ()
    local_copy_names: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            f"LoadResult(outcome={self.outcome!r}, generation={self.generation!r}, "
            f"file_id={self.file_id!r})"
        )


@dataclass(frozen=True)
class SnapshotDescription:
    """Recovery-list facts. Counts come from the header, not from decrypted JSON."""

    file_id: str
    name: str
    generation: int | None
    created_at_utc: datetime | None
    spending_count: int | None
    income_count: int | None
    next_id: int | None
    owner_labels: tuple[str, ...]
    known_valid: bool
    invalid: bool
    md5_checksum: str
    reason: str = ""
    conflict: bool = False

    def __repr__(self) -> str:
        return (
            f"SnapshotDescription(file_id={self.file_id!r}, "
            f"generation={self.generation!r}, known_valid={self.known_valid!r})"
        )


SOURCE_DRIVE = "drive"
SOURCE_LOCAL = "local"
SAVED_BY_THIS_COMPUTER = "This computer"


@dataclass(frozen=True)
class RecoveryItem:
    """One row in the backup list. It has no notes, amounts, or passphrase."""

    key: str
    source: str
    name: str
    generation: int | None
    created_at_utc: datetime | None
    spending_count: int | None
    income_count: int | None
    next_id: int | None
    saved_by: str
    known_valid: bool
    conflict: bool
    current: bool
    invalid: bool
    reason: str

    def __repr__(self) -> str:
        return (
            f"RecoveryItem(source={self.source!r}, generation={self.generation!r}, "
            f"invalid={self.invalid!r})"
        )


@dataclass(frozen=True)
class RecoveryCatalog:
    """Valid snapshots first, then copies that failed validation."""

    valid: tuple[RecoveryItem, ...]
    unusable: tuple[RecoveryItem, ...]
    message: str = ""

    def __repr__(self) -> str:
        return (
            f"RecoveryCatalog(valid={len(self.valid)}, unusable={len(self.unusable)})"
        )


@dataclass(frozen=True)
class _CacheEntry:
    md5: str
    name: str
    generation: int
    created_at_utc: datetime | None = None
    spending_count: int | None = None
    income_count: int | None = None
    next_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class KnownSnapshot:
    """A verified snapshot the retention rule can see. No finance text."""

    file_id: str
    generation: int
    created_at_utc: datetime
    drive_created_at: datetime
    random_id: str


def retention_keep_ids(
    snapshots: Sequence[KnownSnapshot],
    *,
    now: datetime,
) -> frozenset[str]:
    """Ids to keep from an already-verified set.

    ``now`` is UTC. The daily window is the last ``KEEP_DAILY_DAYS`` UTC dates,
    including today. Days with no snapshot add nothing. When fewer than
    ``MIN_KNOWN_VALID`` verified snapshots exist, every one of them is kept.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise SnapshotError("Retention time must include a time zone.")
    if len(snapshots) <= MIN_KNOWN_VALID:
        return frozenset(item.file_id for item in snapshots)
    ordered = sorted(snapshots, key=_known_sort_key, reverse=True)
    keep = {item.file_id for item in ordered[:KEEP_RECENT_SNAPSHOTS]}
    today = now.astimezone(timezone.utc).date()
    earliest = today - timedelta(days=KEEP_DAILY_DAYS - 1)
    seen_days: set[object] = set()
    for item in ordered:
        day = item.created_at_utc.astimezone(timezone.utc).date()
        if day < earliest or day > today or day in seen_days:
            continue
        seen_days.add(day)
        keep.add(item.file_id)
    if len(keep) < MIN_KNOWN_VALID:
        for item in ordered:
            keep.add(item.file_id)
            if len(keep) >= MIN_KNOWN_VALID:
                break
    return frozenset(keep)


def _known_sort_key(item: KnownSnapshot) -> tuple:
    return (item.generation, item.drive_created_at, item.random_id, item.file_id)


class KnownValidCache:
    """Local record of verified file ids and checksums. No finance text."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._verified: dict[str, _CacheEntry] = {}
        self._headers: dict[str, _CacheEntry] = {}
        self._invalid: dict[str, _CacheEntry] = {}
        self._load()

    def verified(self, item: DriveFile) -> _CacheEntry | None:
        return _current(self._verified, item)

    def header(self, item: DriveFile) -> _CacheEntry | None:
        found = _current(self._verified, item)
        if found is not None:
            return found
        return _current(self._headers, item)

    def invalid(self, item: DriveFile) -> _CacheEntry | None:
        return _current(self._invalid, item)

    def stale_verified(self, item: DriveFile) -> _CacheEntry | None:
        """A verified entry whose checksum or name no longer matches Drive."""
        entry = self._verified.get(item.file_id)
        if entry is None or self.verified(item) is not None:
            return None
        return entry

    def remember_verified(self, item: DriveFile, metadata: SnapshotMetadata) -> None:
        entry = _entry_from_metadata(item, metadata)
        self._verified[item.file_id] = entry
        self._headers[item.file_id] = entry
        self._invalid.pop(item.file_id, None)
        self._write()

    def remember_header(self, item: DriveFile, metadata: SnapshotMetadata) -> None:
        if self.verified(item) is not None:
            return
        self._headers[item.file_id] = _entry_from_metadata(item, metadata)
        self._write()

    def mark_invalid(self, item: DriveFile, reason: str) -> None:
        if not item.md5_checksum:
            return
        self._verified.pop(item.file_id, None)
        self._invalid[item.file_id] = _CacheEntry(
            md5=item.md5_checksum,
            name=item.name,
            generation=item.generation or 0,
            reason=reason,
        )
        self._write()

    def forget(self, file_id: str) -> None:
        changed = False
        for bucket in (self._verified, self._headers, self._invalid):
            if bucket.pop(file_id, None) is not None:
                changed = True
        if changed:
            self._write()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise SnapshotError(
                "The known-valid cache could not be read. It was not deleted."
            ) from None
        if not isinstance(data, dict) or data.get("format_version") != CACHE_FORMAT_VERSION:
            raise SnapshotError(
                "The known-valid cache uses a format this version does not read."
            )
        self._verified = _bucket(data.get("verified"))
        self._headers = _bucket(data.get("headers"))
        self._invalid = _bucket(data.get("invalid"))

    def _write(self) -> None:
        payload = {
            "format_version": CACHE_FORMAT_VERSION,
            "verified": {key: _entry_dict(value) for key, value in self._verified.items()},
            "headers": {key: _entry_dict(value) for key, value in self._headers.items()},
            "invalid": {key: _entry_dict(value) for key, value in self._invalid.items()},
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _replace_text(self.path, text)


def _current(bucket: Mapping[str, _CacheEntry], item: DriveFile) -> _CacheEntry | None:
    entry = bucket.get(item.file_id)
    if entry is None or not item.md5_checksum or entry.md5 != item.md5_checksum:
        return None
    if entry.name != item.name:
        return None
    return entry


def _entry_from_metadata(item: DriveFile, metadata: SnapshotMetadata) -> _CacheEntry:
    return _CacheEntry(
        md5=item.md5_checksum,
        name=item.name,
        generation=metadata.generation,
        created_at_utc=metadata.created_at_utc,
        spending_count=metadata.spending_count,
        income_count=metadata.income_count,
        next_id=metadata.next_id,
    )


def _entry_dict(entry: _CacheEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "md5": entry.md5,
        "name": entry.name,
        "generation": entry.generation,
    }
    if entry.created_at_utc is not None:
        payload["created_at_utc"] = _stamp(entry.created_at_utc)
    if entry.spending_count is not None:
        payload["spending_count"] = entry.spending_count
    if entry.income_count is not None:
        payload["income_count"] = entry.income_count
    if entry.next_id is not None:
        payload["next_id"] = entry.next_id
    if entry.reason is not None:
        payload["reason"] = entry.reason
    return payload


def _bucket(value: object) -> dict[str, _CacheEntry]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SnapshotError(
            "The known-valid cache uses a format this version does not read."
        )
    parsed: dict[str, _CacheEntry] = {}
    for file_id, raw in value.items():
        entry = _parse_entry(raw)
        if isinstance(file_id, str) and entry is not None:
            parsed[file_id] = entry
    return parsed


def _parse_entry(raw: object) -> _CacheEntry | None:
    if not isinstance(raw, dict):
        return None
    md5 = raw.get("md5")
    name = raw.get("name")
    generation = raw.get("generation")
    if not isinstance(md5, str) or not isinstance(name, str):
        return None
    if isinstance(generation, bool) or not isinstance(generation, int):
        return None
    created = raw.get("created_at_utc")
    when: datetime | None = None
    if isinstance(created, str):
        when = _parse_stamp(created)
    return _CacheEntry(
        md5=md5,
        name=name,
        generation=generation,
        created_at_utc=when,
        spending_count=_optional_int(raw.get("spending_count")),
        income_count=_optional_int(raw.get("income_count")),
        next_id=_optional_int(raw.get("next_id")),
        reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class LocalEncryptedCopies:
    """The most recent encrypted snapshots on this computer. Plaintext is refused."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def write(self, name: str, envelope: bytes) -> None:
        filename = _snapshot_filename(name)
        if not envelope.startswith(b"FTEN"):
            raise SnapshotError("Refusing to store a local snapshot that is not encrypted.")
        self.directory.mkdir(parents=True, exist_ok=True)
        _replace_bytes(self.directory / filename, envelope)
        self._trim()

    def read_matching(self, name: str, md5: str) -> bytes | None:
        if not md5:
            return None
        try:
            filename = _snapshot_filename(name)
        except SnapshotError:
            return None
        path = self.directory / filename
        if not path.is_file():
            return None
        data = path.read_bytes()
        if hashlib.md5(data).hexdigest() != md5:
            return None
        return data

    def names(self) -> tuple[str, ...]:
        """Snapshot names, most recently saved on this computer first.

        A file just written stays even when its generation number is lower
        than an older practice snapshot. The generation in the name is not
        the order.
        """
        if not self.directory.is_dir():
            return ()
        found: list[tuple[int, str, str]] = []
        for path in self.directory.iterdir():
            if not path.is_file() or generation_of_name(path.name) is None:
                continue
            try:
                modified = path.stat().st_mtime_ns
            except OSError:
                modified = 0
            found.append((modified, _snapshot_stamp(path.name), path.name))
        found.sort(reverse=True)
        return tuple(item[2] for item in found)

    def _trim(self) -> None:
        kept = self.names()
        for name in kept[LOCAL_ENCRYPTED_COPIES:]:
            path = self.directory / name
            if path.is_file():
                path.unlink()


def _snapshot_filename(name: str) -> str:
    if name != Path(name).name or generation_of_name(name) is None:
        raise SnapshotError("Refusing to store a local file that is not a snapshot.")
    return name


_SNAPSHOT_STAMP = re.compile(
    r"^finance_snapshot_g[0-9]{6}_(?P<stamp>[0-9]{8}T[0-9]{6}Z)_"
)


def _snapshot_stamp(name: str) -> str:
    match = _SNAPSHOT_STAMP.search(name)
    if match is None:
        return ""
    return match.group("stamp")


def _replace_text(path: Path, text: str) -> None:
    _replace(path, lambda handle: handle.write(text), "w")


def _replace_bytes(path: Path, payload: bytes) -> None:
    _replace(path, lambda handle: handle.write(payload), "wb")


def _replace(path: Path, write: Callable, mode: str) -> None:
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, mode) as handle:
            write(handle)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


class SnapshotRepository:
    """Save and load encrypted snapshots through a Drive adapter.

    ``load_latest`` and ``save`` derive a key with scrypt and call Drive.
    Call them off the GUI thread. The Drive adapter runs its own calls on one
    background thread; this object must not be used from that thread.
    """

    def __init__(
        self,
        drive: DriveAdapter,
        passphrase: str,
        *,
        cache_path: Path | None = None,
        local_dir: Path | None = None,
        memory_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(passphrase, str) or passphrase == "":
            raise SnapshotError("A passphrase is required.")
        self._drive = drive
        self._passphrase = passphrase
        self._clock = clock
        local = local_dir or encrypted_snapshots_dir()
        self._cache = KnownValidCache(cache_path or known_valid_cache_path())
        self._copies = LocalEncryptedCopies(local)
        self._memory_path = memory_path or (local.parent / "snapshot-memory.json")
        self._ready = False
        self._base_generation = 0
        self._committed: bytes | None = None
        self._session_ids: list[str] = []
        self._ignored_ids: set[str] = set()
        self._hold: SaveResult | None = None

    def load_latest(self) -> LoadResult:
        """Open the newest snapshot that decrypts and validates.

        An empty folder loads nothing and does not create a dataset. A folder
        whose snapshots all fail does not create a blank dataset either.
        """
        self._hold = None
        try:
            listing = self._drive.list_for_generation(())
        except DriveError as exc:
            return self._unavailable(exc)
        snapshots = _snapshot_files(listing.files)
        if not snapshots:
            self._ready = True
            self._base_generation = 0
            self._committed = None
            return LoadResult(
                outcome=OUTCOME_EMPTY,
                message="There is no snapshot in the Drive folder.",
                base_generation=0,
                local_copy_names=self._copies.names(),
            )
        ordered = sorted(snapshots, key=_load_order)
        skipped: list[SkippedSnapshot] = []
        decrypt_failures: list[DriveFile] = []
        for item in ordered:
            invalid = self._cache.invalid(item)
            if invalid is not None:
                skipped.append(_skipped(item, invalid.reason or _INVALID_DOCUMENT))
                continue
            try:
                envelope = self._envelope(item)
                header = read_unverified_metadata(envelope)
            except DriveUnavailable as exc:
                return self._unavailable(exc)
            except DriveError:
                skipped.append(_skipped(item, _MALFORMED))
                continue
            except CryptoError:
                skipped.append(_skipped(item, _MALFORMED))
                self._cache.mark_invalid(item, _MALFORMED)
                continue
            if header.generation != item.generation:
                skipped.append(_skipped(item, _NAME_MISMATCH))
                self._cache.mark_invalid(item, _NAME_MISMATCH)
                continue
            try:
                decrypted = decrypt(envelope, self._passphrase)
                store = self._store_from_plaintext(
                    decrypted.plaintext, decrypted.metadata, item.name
                )
            except CryptoError:
                decrypt_failures.append(item)
                skipped.append(_skipped(item, _DECRYPT_FAILED))
                continue
            except _InvalidSnapshot as exc:
                skipped.append(_skipped(item, str(exc)))
                self._cache.mark_invalid(item, str(exc))
                continue
            for failed in decrypt_failures:
                self._cache.mark_invalid(failed, _DECRYPT_FAILED)
            self._cache.remember_verified(item, decrypted.metadata)
            self._copies.write(item.name, envelope)
            self._ready = True
            self._base_generation = item.generation or 0
            self._committed = decrypted.plaintext
            conflicts = _same_generation(ordered, item.generation)
            message = f"Opened generation {item.generation}."
            if conflicts:
                message = (
                    f"Opened generation {item.generation}. "
                    "Another snapshot uses that generation and was kept."
                )
            return LoadResult(
                outcome=OUTCOME_LOADED,
                message=message,
                store=store,
                base_generation=self._base_generation,
                file_id=item.file_id,
                generation=item.generation,
                metadata=decrypted.metadata,
                skipped=tuple(skipped),
                conflict_file_ids=conflicts,
                local_copy_names=self._copies.names(),
            )
        self._ready = False
        return LoadResult(
            outcome=OUTCOME_UNREADABLE,
            message=(
                "Snapshots are in the folder, but none of them could be opened. "
                "You can try the passphrase again, inspect the copies, or cancel. "
                "An empty dataset was not created."
            ),
            skipped=tuple(skipped),
            choices=_UNREADABLE_CHOICES,
            local_copy_names=self._copies.names(),
        )

    def reload(self) -> LoadResult:
        """Load Drive again. The previous store object is left unchanged."""
        return self.load_latest()

    def base_generation(self) -> int:
        """Generation last loaded or saved. Zero means no snapshot has been accepted."""
        return self._base_generation

    def newest_generation(self) -> int:
        """Newest generation in the folder listing. Zero when the folder has none."""
        listing = self._drive.list_for_generation(tuple(self._session_ids))
        return self._newest_generation(listing.files)

    def save(self, store: FinanceStore) -> SaveResult:
        """Upload a new generation when the document changed and the base still matches."""
        if not isinstance(store, FinanceStore):
            raise TypeError("store must be a FinanceStore")
        if self._hold is not None:
            return self._hold
        if not self._ready:
            return _failed(_NOT_READY, choices=())
        try:
            return self._save(store)
        except DriveUploadUncertain as exc:
            return _failed(str(exc), choices=_FAILED_CHOICES)
        except (DriveError, SnapshotError) as exc:
            return _failed(str(exc), choices=_FAILED_CHOICES)

    def describe(self) -> tuple[SnapshotDescription, ...]:
        """List snapshots from headers and the cache. This does not decrypt."""
        listing = self._drive.list_for_generation(())
        described: list[SnapshotDescription] = []
        for item in _snapshot_files(listing.files):
            described.append(self._describe_one(item))
        described.sort(key=lambda item: item.generation or 0, reverse=True)
        return _with_conflicts(described)

    def recovery_catalog(
        self,
        *,
        current_file_id: str | None,
        current_generation: int | None,
    ) -> RecoveryCatalog:
        """Drive snapshots plus local copies. Counts come from headers, not decryption."""
        return _assemble_catalog(
            list(self.describe()),
            _local_headers(self._copies.directory),
            current_file_id=current_file_id,
            current_generation=current_generation,
            message="",
        )

    def plaintext_for_recovery(self, key: str) -> bytes:
        """Decrypt one selected snapshot and return its canonical JSON bytes.

        This does not upload. A failure raises ``SnapshotError`` and leaves Drive alone.
        """
        source, ident = _split_recovery_key(key)
        if source == SOURCE_DRIVE:
            listing = self._drive.list_for_generation(())
            matches = [
                item for item in _snapshot_files(listing.files) if item.file_id == ident
            ]
            if len(matches) != 1:
                raise SnapshotError(
                    "That snapshot is not in the Drive folder. Nothing was uploaded."
                )
            item = matches[0]
            if self._cache.invalid(item) is not None:
                raise SnapshotError(
                    "That snapshot failed validation and cannot be restored. Nothing was uploaded."
                )
            try:
                envelope = self._envelope(item)
            except DriveError as exc:
                raise SnapshotError(str(exc)) from None
            name = item.name
        else:
            try:
                name = _snapshot_filename(ident)
            except SnapshotError:
                raise SnapshotError(
                    "That backup could not be found. Nothing was uploaded."
                ) from None
            path = self._copies.directory / name
            if not path.is_file():
                raise SnapshotError(
                    "That local copy could not be found. Nothing was uploaded."
                )
            try:
                envelope = path.read_bytes()
            except OSError:
                raise SnapshotError(
                    "That local copy could not be read. Nothing was uploaded."
                ) from None
        try:
            decrypted = decrypt(envelope, self._passphrase)
            store = self._store_from_plaintext(
                decrypted.plaintext, decrypted.metadata, name
            )
        except CryptoError:
            raise SnapshotError(
                "That snapshot could not be decrypted. Nothing was uploaded."
            ) from None
        except _InvalidSnapshot:
            raise SnapshotError(
                "That snapshot is not a valid finance document. Nothing was uploaded."
            ) from None
        return store.to_json_bytes()

    def open_newest_local(self) -> tuple[FinanceStore, int] | None:
        """The newest local copy that decrypts, or None when this computer has none.

        Copies that do not open are skipped. If every copy fails, this raises
        ``SnapshotError`` and does not upload.
        """
        names = self._copies.names()
        if not names:
            return None
        saw_crypto = False
        saw_bad = False
        for name in names:
            path = self._copies.directory / name
            try:
                envelope = path.read_bytes()
            except OSError:
                saw_bad = True
                continue
            if not envelope.startswith(b"FTEN"):
                saw_bad = True
                continue
            try:
                header = read_unverified_metadata(envelope)
            except CryptoError:
                saw_bad = True
                continue
            if header.generation != generation_of_name(name):
                saw_bad = True
                continue
            try:
                decrypted = decrypt(envelope, self._passphrase)
                store = self._store_from_plaintext(
                    decrypted.plaintext, decrypted.metadata, name
                )
            except CryptoError:
                saw_crypto = True
                continue
            except _InvalidSnapshot:
                saw_bad = True
                continue
            return store, decrypted.metadata.generation
        if saw_crypto:
            raise SnapshotError(
                "The local encrypted copies did not open with this passphrase. Nothing was uploaded."
            )
        if saw_bad:
            raise SnapshotError(
                "The copies on this computer are not valid encrypted snapshots. Nothing was uploaded."
            )
        return None

    def _save(self, store: FinanceStore) -> SaveResult:
        listing = self._drive.list_for_generation(tuple(self._session_ids))
        self._refresh_changed(listing.files)
        remote = self._newest_generation(listing.files)
        if remote != self._base_generation:
            return SaveResult(
                outcome=OUTCOME_REFUSED,
                message=(
                    f"The newest snapshot in Drive is generation {remote}, "
                    f"and this session is based on generation {self._base_generation}. "
                    "The save was not uploaded. The change is still here. "
                    "Reload discards it and shows the Drive copy. "
                    "Keep and retry tries this save again after you have looked."
                ),
                base_generation=self._base_generation,
                remote_generation=remote,
                choices=_REFUSED_CHOICES,
            )
        document = store.to_json_bytes()
        if self._committed is not None and document == self._committed:
            return SaveResult(
                outcome=OUTCOME_UNCHANGED,
                message=_UNCHANGED,
                generation=self._base_generation,
                base_generation=self._base_generation,
                adopted=True,
            )
        if self._base_generation >= _MAX_GENERATION:
            return _failed(_EXHAUSTED)
        generation = self._base_generation + 1
        when = self._now()
        metadata = _metadata_for(document, generation, when)
        envelope = encrypt(document, self._passphrase, metadata)
        uploaded = self._drive.upload_snapshot(generation, envelope, when=when)
        file_id = uploaded.file.file_id
        self._remember_session(file_id)
        try:
            downloaded = self._drive.download(file_id)
        except DriveError as exc:
            return _failed(str(exc), file_id=file_id, choices=_FAILED_CHOICES)
        try:
            opened = self._verify_upload(uploaded.file, downloaded, document, metadata)
        except (CryptoError, _InvalidSnapshot) as exc:
            reason = _DECRYPT_FAILED if isinstance(exc, CryptoError) else str(exc)
            self._ignore(uploaded.file, reason)
            return _failed(
                reason,
                generation=generation,
                file_id=file_id,
                choices=_FAILED_CHOICES,
            )
        conflict_ids = self._conflict_ids(uploaded.listing.files, generation)
        adopted = True
        current_id = file_id
        outcome = OUTCOME_SAVED
        message = f"Saved generation {generation}."
        if conflict_ids:
            visible = [
                item
                for item in _snapshot_files(uploaded.listing.files)
                if item.generation == generation and not self._ignored(item)
            ]
            conflict = classify_generation(visible, generation)
            current_id = conflict.current.file_id
            adopted = current_id == file_id
            outcome = OUTCOME_CONFLICT
            message = (
                f"A conflicting save was detected for generation {generation}. "
                "Both snapshots were kept. The earlier one is current, and the "
                "other remains available. Nothing was discarded."
            )
        self._ignored_ids.discard(file_id)
        self._cache.remember_verified(uploaded.file, opened.metadata)
        self._copies.write(uploaded.file.name, downloaded)
        prune = self._prune(uploaded.listing.files, now=when, protected=file_id)
        if adopted:
            self._base_generation = generation
            self._committed = document
        result = SaveResult(
            outcome=outcome,
            message=message,
            generation=generation,
            file_id=file_id,
            base_generation=self._base_generation,
            current_file_id=current_id,
            conflict_file_ids=conflict_ids,
            prune=prune,
            included_by_get=uploaded.listing.included_by_get,
            adopted=adopted,
            choices=(CHOICE_RELOAD_DISCARD,) if outcome == OUTCOME_CONFLICT and not adopted else (),
        )
        if outcome == OUTCOME_CONFLICT and not adopted:
            self._hold = SaveResult(
                outcome=OUTCOME_CONFLICT,
                message=_CONFLICT_HELD,
                generation=generation,
                file_id=file_id,
                base_generation=self._base_generation,
                current_file_id=current_id,
                conflict_file_ids=conflict_ids,
                choices=(CHOICE_RELOAD_DISCARD,),
            )
        if prune and any(action.status == "failed" for action in prune):
            result = replace(
                result,
                message=(
                    result.message
                    + " Some older snapshots could not be moved to the trash. "
                    "They were left in place."
                ),
            )
        return result

    def _verify_upload(
        self,
        item: DriveFile,
        envelope: bytes,
        intended: bytes,
        metadata: SnapshotMetadata,
    ) -> DecryptedSnapshot:
        decrypted = decrypt(envelope, self._passphrase)
        if decrypted.plaintext != intended:
            raise _InvalidSnapshot(_BYTE_MISMATCH)
        if decrypted.metadata != metadata:
            raise _InvalidSnapshot(_INVALID_DOCUMENT)
        self._store_from_plaintext(decrypted.plaintext, decrypted.metadata, item.name)
        return decrypted

    def _store_from_plaintext(
        self,
        plaintext: bytes,
        metadata: SnapshotMetadata,
        name: str,
    ) -> FinanceStore:
        if generation_of_name(name) != metadata.generation:
            raise _InvalidSnapshot(_NAME_MISMATCH)
        try:
            store = FinanceStore.from_json_bytes(plaintext, self._memory_path)
        except Exception:
            raise _InvalidSnapshot(_INVALID_DOCUMENT) from None
        if store.to_json_bytes() != plaintext:
            raise _InvalidSnapshot(_INVALID_DOCUMENT)
        if (
            len(store.spending) != metadata.spending_count
            or len(store.income) != metadata.income_count
            or store._next_id != metadata.next_id
        ):
            raise _InvalidSnapshot(_INVALID_DOCUMENT)
        return store

    def _envelope(self, item: DriveFile) -> bytes:
        if self._cache.verified(item) is not None or self._cache.header(item) is not None:
            local = self._copies.read_matching(item.name, item.md5_checksum)
            if local is not None:
                return local
        return self._drive.download(item.file_id)

    def _refresh_changed(self, files: Sequence[DriveFile]) -> None:
        """Decrypt a previously verified snapshot only when its checksum changed."""
        for item in _snapshot_files(files):
            if self._cache.stale_verified(item) is None:
                continue
            envelope = self._drive.download(item.file_id)
            try:
                header = read_unverified_metadata(envelope)
                if header.generation != item.generation:
                    self._ignore(item, _NAME_MISMATCH)
                    continue
                decrypted = decrypt(envelope, self._passphrase)
                self._store_from_plaintext(
                    decrypted.plaintext, decrypted.metadata, item.name
                )
            except CryptoError:
                self._ignore(item, _DECRYPT_FAILED)
                continue
            except _InvalidSnapshot as exc:
                self._ignore(item, str(exc))
                continue
            self._ignored_ids.discard(item.file_id)
            self._cache.remember_verified(item, decrypted.metadata)

    def _prune(
        self,
        files: Sequence[DriveFile],
        *,
        now: datetime,
        protected: str,
    ) -> tuple[PruneAction, ...]:
        known: list[KnownSnapshot] = []
        by_id: dict[str, DriveFile] = {}
        for item in _snapshot_files(files):
            entry = self._cache.verified(item)
            if entry is None or entry.created_at_utc is None or item.generation is None:
                continue
            by_id[item.file_id] = item
            known.append(
                KnownSnapshot(
                    file_id=item.file_id,
                    generation=item.generation,
                    created_at_utc=entry.created_at_utc,
                    drive_created_at=item.created_time,
                    random_id=_random_id(item.name),
                )
            )
        if len(known) < MIN_KNOWN_VALID:
            return ()
        keep = set(retention_keep_ids(known, now=now))
        keep.add(protected)
        actions: list[PruneAction] = []
        for item in by_id.values():
            if item.file_id in keep:
                continue
            try:
                result = self._drive.trash(item.file_id)
            except DriveError:
                actions.append(PruneAction(item.file_id, item.name, "failed"))
                continue
            if result.trashed:
                self._cache.forget(item.file_id)
                self._drop_session(item.file_id)
                actions.append(PruneAction(item.file_id, item.name, "trashed"))
            elif result.skipped_not_owner:
                actions.append(PruneAction(item.file_id, item.name, NOT_OWNER_SKIPPED))
            else:
                actions.append(PruneAction(item.file_id, item.name, "failed"))
        return tuple(actions)

    def _describe_one(self, item: DriveFile) -> SnapshotDescription:
        owners = _owner_labels(item)
        invalid = self._cache.invalid(item)
        if invalid is not None:
            return _description(
                item,
                owners,
                known_valid=False,
                invalid=True,
                reason=invalid.reason or _INVALID_DOCUMENT,
            )
        verified = self._cache.verified(item)
        if verified is not None:
            return _description(
                item,
                owners,
                known_valid=True,
                invalid=False,
                entry=verified,
            )
        cached_header = self._cache.header(item)
        if cached_header is not None:
            return _description(
                item,
                owners,
                known_valid=False,
                invalid=False,
                entry=cached_header,
            )
        try:
            envelope = self._drive.download(item.file_id)
            metadata = read_unverified_metadata(envelope)
        except DriveError:
            return _description(
                item,
                owners,
                known_valid=False,
                invalid=True,
                reason="The snapshot could not be read.",
            )
        except CryptoError:
            return _description(
                item,
                owners,
                known_valid=False,
                invalid=True,
                reason=_MALFORMED,
            )
        if metadata.generation != item.generation:
            self._cache.mark_invalid(item, _NAME_MISMATCH)
            return _description(
                item,
                owners,
                known_valid=False,
                invalid=True,
                reason=_NAME_MISMATCH,
            )
        self._cache.remember_header(item, metadata)
        return _description(
            item,
            owners,
            known_valid=False,
            invalid=False,
            entry=self._cache.header(item),
        )

    def _now(self) -> datetime:
        current = self._clock() if self._clock is not None else self._drive.server_time()
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise SnapshotError("The snapshot clock must include a time zone.")
        return current.astimezone(timezone.utc).replace(microsecond=0)

    def _unavailable(self, exc: DriveError) -> LoadResult:
        self._ready = False
        return LoadResult(
            outcome=OUTCOME_UNAVAILABLE,
            message=str(exc),
            local_copy_names=self._copies.names(),
            choices=(CHOICE_KEEP_AND_RETRY, CHOICE_CANCEL),
        )

    def _remember_session(self, file_id: str) -> None:
        if file_id not in self._session_ids:
            self._session_ids.append(file_id)

    def _drop_session(self, file_id: str) -> None:
        self._session_ids = [item for item in self._session_ids if item != file_id]

    def _ignore(self, item: DriveFile, reason: str) -> None:
        self._ignored_ids.add(item.file_id)
        self._cache.mark_invalid(item, reason)

    def _ignored(self, item: DriveFile) -> bool:
        return item.file_id in self._ignored_ids or self._cache.invalid(item) is not None

    def _newest_generation(self, files: Sequence[DriveFile]) -> int:
        generations = [
            item.generation
            for item in _snapshot_files(files)
            if item.generation is not None and not self._ignored(item)
        ]
        if not generations:
            return 0
        return max(generations)

    def _conflict_ids(self, files: Sequence[DriveFile], generation: int) -> tuple[str, ...]:
        matches = [
            item.file_id
            for item in _snapshot_files(files)
            if item.generation == generation and not self._ignored(item)
        ]
        if len(matches) < 2:
            return ()
        return tuple(matches)


def _snapshot_files(files: Sequence[DriveFile]) -> list[DriveFile]:
    return [item for item in files if item.generation is not None and not item.trashed]


def _load_order(item: DriveFile) -> tuple:
    """Higher generation first. Within one generation, the earlier Drive time wins."""
    return (
        -(item.generation or 0),
        item.created_time,
        _random_id(item.name),
        item.file_id,
    )


def _random_id(name: str) -> str:
    piece = name[:-4].rsplit("_", 1)[-1] if name.endswith(".enc") else ""
    if _RANDOM_ID.fullmatch(piece):
        return piece
    return ""


def _same_generation(files: Sequence[DriveFile], generation: int | None) -> tuple[str, ...]:
    if generation is None:
        return ()
    matches = [item.file_id for item in files if item.generation == generation]
    if len(matches) < 2:
        return ()
    return tuple(matches)


def _skipped(item: DriveFile, reason: str) -> SkippedSnapshot:
    return SkippedSnapshot(item.file_id, item.name, item.generation, reason)


def _failed(
    message: str,
    *,
    generation: int | None = None,
    file_id: str | None = None,
    choices: tuple[str, ...] = _FAILED_CHOICES,
) -> SaveResult:
    return SaveResult(
        outcome=OUTCOME_FAILED,
        message=message,
        generation=generation,
        file_id=file_id,
        choices=choices,
    )


def _metadata_for(document: bytes, generation: int, when: datetime) -> SnapshotMetadata:
    try:
        data = json.loads(document.decode("utf-8"))
        spending = data["spending"]
        income = data["income"]
        next_id = data["next_id"]
        if not isinstance(spending, list) or not isinstance(income, list):
            raise TypeError
        if isinstance(next_id, bool) or not isinstance(next_id, int):
            raise TypeError
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise SnapshotError("The finance document could not be saved.") from None
    return SnapshotMetadata(
        generation=generation,
        created_at_utc=when,
        spending_count=len(spending),
        income_count=len(income),
        next_id=next_id,
    )


def _description(
    item: DriveFile,
    owners: tuple[str, ...],
    *,
    known_valid: bool,
    invalid: bool,
    entry: _CacheEntry | None = None,
    reason: str = "",
) -> SnapshotDescription:
    return SnapshotDescription(
        file_id=item.file_id,
        name=item.name,
        generation=item.generation,
        created_at_utc=None if entry is None else entry.created_at_utc,
        spending_count=None if entry is None else entry.spending_count,
        income_count=None if entry is None else entry.income_count,
        next_id=None if entry is None else entry.next_id,
        owner_labels=owners,
        known_valid=known_valid,
        invalid=invalid,
        md5_checksum=item.md5_checksum,
        reason=reason,
    )


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(value: str) -> datetime | None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class _LocalHeader:
    name: str
    generation: int | None
    created_at_utc: datetime | None
    spending_count: int | None
    income_count: int | None
    next_id: int | None
    invalid: bool
    reason: str


_EARLIEST = datetime(1970, 1, 1, tzinfo=timezone.utc)


def recovery_catalog_uncached(
    drive: DriveAdapter,
    directory: Path,
    *,
    current_file_id: str | None,
    current_generation: int | None,
) -> RecoveryCatalog:
    """List a folder that has no open snapshot repository. This does not decrypt."""
    listing = drive.list_for_generation(())
    described = [
        _describe_header(drive, item) for item in _snapshot_files(listing.files)
    ]
    return _assemble_catalog(
        list(_with_conflicts(described)),
        _local_headers(directory),
        current_file_id=current_file_id,
        current_generation=current_generation,
        message="",
    )


def local_recovery_catalog(
    directory: Path,
    *,
    current_generation: int | None,
    message: str,
) -> RecoveryCatalog:
    """Local encrypted copies only. Used when Drive cannot be listed."""
    return _assemble_catalog(
        [],
        _local_headers(directory),
        current_file_id=None,
        current_generation=current_generation,
        message=message,
    )


def recovery_label(item: RecoveryItem) -> str:
    """One backup described without notes or amounts."""
    when = (
        item.created_at_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if item.created_at_utc is not None
        else "an unknown time"
    )
    where = "Drive" if item.source == SOURCE_DRIVE else "this computer"
    spending = "?" if item.spending_count is None else str(item.spending_count)
    income = "?" if item.income_count is None else str(item.income_count)
    generation = "unknown" if item.generation is None else str(item.generation)
    text = (
        f"generation {generation} from {when}, saved by {item.saved_by}, "
        f"on {where}, {spending} spending records and {income} income records"
    )
    if item.conflict:
        text += ", from a conflicting save"
    return text


def summary_text(catalog: RecoveryCatalog) -> str:
    """A backup list safe to show. It does not include notes or amounts."""
    lines: list[str] = []
    if catalog.message.strip():
        lines.append(catalog.message.strip())
    if catalog.valid:
        lines.append("Snapshots that can be restored:")
        for item in catalog.valid:
            suffix = " Current." if item.current else ""
            lines.append(f"- {recovery_label(item)}.{suffix}")
    else:
        lines.append("No snapshot can be restored.")
    if catalog.unusable:
        lines.append("Copies that cannot be restored:")
        for item in catalog.unusable:
            generation = "unknown" if item.generation is None else str(item.generation)
            where = "Drive" if item.source == SOURCE_DRIVE else "this computer"
            reason = item.reason or "It could not be used."
            if item.conflict:
                reason = f"{reason} Conflicting save."
            lines.append(f"- Generation {generation} on {where}: {reason}")
    lines.append("Nothing was changed.")
    return "\n".join(lines)


def _owner_labels(item: DriveFile) -> tuple[str, ...]:
    return tuple(owner.display_name or owner.email for owner in item.owners)


def _with_conflicts(
    items: Sequence[SnapshotDescription],
) -> tuple[SnapshotDescription, ...]:
    counts: dict[int, int] = {}
    for item in items:
        if item.generation is None:
            continue
        counts[item.generation] = counts.get(item.generation, 0) + 1
    marked: list[SnapshotDescription] = []
    for item in items:
        conflict = item.generation is not None and counts.get(item.generation, 0) > 1
        if item.conflict == conflict:
            marked.append(item)
        else:
            marked.append(replace(item, conflict=conflict))
    return tuple(marked)


def _describe_header(drive: DriveAdapter, item: DriveFile) -> SnapshotDescription:
    owners = _owner_labels(item)
    try:
        envelope = drive.download(item.file_id)
        metadata = read_unverified_metadata(envelope)
    except DriveError:
        return _description(
            item,
            owners,
            known_valid=False,
            invalid=True,
            reason="The snapshot could not be read.",
        )
    except CryptoError:
        return _description(
            item,
            owners,
            known_valid=False,
            invalid=True,
            reason=_MALFORMED,
        )
    if metadata.generation != item.generation:
        return _description(
            item,
            owners,
            known_valid=False,
            invalid=True,
            reason=_NAME_MISMATCH,
        )
    return _description(
        item,
        owners,
        known_valid=False,
        invalid=False,
        entry=_entry_from_metadata(item, metadata),
    )


def _local_headers(directory: Path) -> tuple[_LocalHeader, ...]:
    found: list[_LocalHeader] = []
    for name in LocalEncryptedCopies(directory).names():
        path = directory / name
        try:
            envelope = path.read_bytes()
        except OSError:
            found.append(
                _LocalHeader(
                    name,
                    generation_of_name(name),
                    None,
                    None,
                    None,
                    None,
                    True,
                    "The snapshot could not be read.",
                )
            )
            continue
        try:
            metadata = read_unverified_metadata(envelope)
        except CryptoError:
            found.append(
                _LocalHeader(
                    name,
                    generation_of_name(name),
                    None,
                    None,
                    None,
                    None,
                    True,
                    _MALFORMED,
                )
            )
            continue
        if metadata.generation != generation_of_name(name):
            found.append(
                _LocalHeader(
                    name,
                    generation_of_name(name),
                    metadata.created_at_utc,
                    None,
                    None,
                    None,
                    True,
                    _NAME_MISMATCH,
                )
            )
            continue
        found.append(
            _LocalHeader(
                name,
                metadata.generation,
                metadata.created_at_utc,
                metadata.spending_count,
                metadata.income_count,
                metadata.next_id,
                False,
                "",
            )
        )
    return tuple(found)


def _assemble_catalog(
    described: Sequence[SnapshotDescription],
    local_headers: Sequence[_LocalHeader],
    *,
    current_file_id: str | None,
    current_generation: int | None,
    message: str,
) -> RecoveryCatalog:
    valid_drive_names = {item.name for item in described if not item.invalid}
    rows: list[RecoveryItem] = []
    for item in described:
        rows.append(
            RecoveryItem(
                key=f"{SOURCE_DRIVE}:{item.file_id}",
                source=SOURCE_DRIVE,
                name=item.name,
                generation=item.generation,
                created_at_utc=item.created_at_utc,
                spending_count=item.spending_count,
                income_count=item.income_count,
                next_id=item.next_id,
                saved_by=_saved_by(item.owner_labels),
                known_valid=item.known_valid,
                conflict=False,
                current=bool(current_file_id) and item.file_id == current_file_id,
                invalid=item.invalid,
                reason=item.reason,
            )
        )
    for local in local_headers:
        if not local.invalid and local.name in valid_drive_names:
            continue
        rows.append(
            RecoveryItem(
                key=f"{SOURCE_LOCAL}:{local.name}",
                source=SOURCE_LOCAL,
                name=local.name,
                generation=local.generation,
                created_at_utc=local.created_at_utc,
                spending_count=local.spending_count,
                income_count=local.income_count,
                next_id=local.next_id,
                saved_by=SAVED_BY_THIS_COMPUTER,
                known_valid=False,
                conflict=False,
                current=False,
                invalid=local.invalid,
                reason=local.reason,
            )
        )
    counts: dict[int, int] = {}
    for row in rows:
        if row.generation is None:
            continue
        counts[row.generation] = counts.get(row.generation, 0) + 1
    marked: list[RecoveryItem] = []
    for row in rows:
        conflict = row.generation is not None and counts.get(row.generation, 0) > 1
        marked.append(replace(row, conflict=conflict))
    marked.sort(key=_recovery_sort_key, reverse=True)
    valid = [item for item in marked if not item.invalid]
    unusable = [item for item in marked if item.invalid]
    if current_generation is not None and not any(item.current for item in valid):
        for index, item in enumerate(valid):
            if item.generation == current_generation:
                valid[index] = replace(item, current=True)
                break
    return RecoveryCatalog(tuple(valid), tuple(unusable), message)


def _recovery_sort_key(item: RecoveryItem) -> tuple:
    when = item.created_at_utc or _EARLIEST
    return (
        item.generation if item.generation is not None else -1,
        when,
        item.name,
        item.key,
    )


def _saved_by(labels: Sequence[str]) -> str:
    cleaned = [label.strip() for label in labels if isinstance(label, str) and label.strip()]
    if not cleaned:
        return "Unknown account"
    text = ", ".join(cleaned)
    if len(text) > 120:
        return text[:117] + "..."
    return text


def _split_recovery_key(key: str) -> tuple[str, str]:
    if not isinstance(key, str):
        raise SnapshotError("That backup could not be found. Nothing was uploaded.")
    source, separator, ident = key.partition(":")
    if separator != ":" or source not in (SOURCE_DRIVE, SOURCE_LOCAL) or not ident:
        raise SnapshotError("That backup could not be found. Nothing was uploaded.")
    return source, ident
