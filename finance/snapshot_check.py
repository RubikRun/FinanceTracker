"""Manual check for encrypted snapshot retention.

Run ``python -m finance.snapshot_check``. This creates synthetic encrypted
snapshots in the Drive folder already saved for this profile. It does not
read or write the local finance file, it does not turn cloud mode on, and
it does not use a real finance passphrase.

``FINANCETRACKER_CONFIG_DIR`` must be set to a dry-run profile. The command
refuses the production profile and refuses a profile inside the source
tree. Empty the shared test folder first. ``fill`` writes 22 snapshots from
one account. ``extend`` writes two more from the other account and must not
trash the first account's files. ``open`` decrypts every remaining snapshot.
``clean`` moves this account's snapshots to the trash. Nothing is
permanently deleted.

The fixed passphrase below is only for these synthetic files.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from .config import default_data_path
from .crypto import CryptoError, decrypt
from .drive_adapter import (
    LOCK_FILE_NAME,
    NOT_OWNER_SKIPPED,
    DriveAdapter,
    DriveError,
    DriveFile,
    GoogleDriveAdapter,
    generation_of_name,
)
from .drive_folder import FolderSetupError, is_drive_item_id
from .google_auth import (
    AuthorizationError,
    CredentialStoreError,
    GoogleAuthorization,
    NeedsInteractiveSignIn,
    OAuthClientError,
    default_secret_store,
    load_desktop_client,
)
from .profile import (
    CONFIG_DIR_ENV,
    PRODUCTION_KEYRING_SERVICE,
    ConfigError,
    app_config_dir,
    encrypted_snapshots_dir,
    keyring_service_name,
    load_local_config,
    oauth_client_file,
)
from .snapshots import (
    KEEP_RECENT_SNAPSHOTS,
    LOCAL_ENCRYPTED_COPIES,
    OUTCOME_EMPTY,
    OUTCOME_LOADED,
    OUTCOME_SAVED,
    SnapshotRepository,
)
from .store import FinanceStore

PASSPHRASE = "snapshot-check-passphrase"
MARKER = "synthetic-snapshot-check-marker"
FILL_COUNT = KEEP_RECENT_SNAPSHOTS + 2
EXTEND_COUNT = 2

_MARKER_BYTES = MARKER.encode("utf-8")


class SnapshotCheckError(Exception):
    """This check will not start. The message is safe to show."""


class CheckCancelled(Exception):
    """The user did not type the folder name."""


_CHECK_ERRORS = (
    DriveError,
    SnapshotCheckError,
    FolderSetupError,
    ConfigError,
    CredentialStoreError,
    NeedsInteractiveSignIn,
    OAuthClientError,
    AuthorizationError,
    CryptoError,
)


def run_snapshot_check(
    argv: list[str] | None = None,
    *,
    prompt: Callable[[str], str] | None = None,
    output: TextIO | None = None,
    opener: Callable[[str], DriveAdapter] | None = None,
    save_count: int | None = None,
) -> int:
    """Run one subcommand. Returns 0 on success, 2 when cancelled, 1 otherwise."""
    write = output if output is not None else sys.stdout
    ask = prompt if prompt is not None else _console_prompt
    arguments = list(sys.argv[1:] if argv is None else argv)
    before = _finance_file_stamp()
    memory = _memory_path()
    memory_existed = memory.exists()
    code = 1
    try:
        try:
            parsed = _parser().parse_args(arguments)
        except SystemExit as exc:
            status = exc.code
            return status if isinstance(status, int) else 1
        code = _run_parsed(parsed, ask, write, opener, save_count)
    except CheckCancelled:
        print("Snapshot check cancelled.", file=write)
        code = 2
    except _CHECK_ERRORS as exc:
        print(str(exc), file=write)
        code = 1
    if _finance_file_stamp() != before:
        print(
            "The local finance file changed. This check must not do that.",
            file=write,
        )
        return 1
    if memory.exists() != memory_existed:
        print("A plaintext snapshot file was written. This check must not do that.", file=write)
        return 1
    return code


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        return run_snapshot_check(argv)
    except KeyboardInterrupt:
        print("\nSnapshot check cancelled.")
        return 2


def _run_parsed(parsed, prompt, output, opener, save_count: int | None) -> int:
    _reject_unsafe_profile()
    config = load_local_config()
    if config.cloud_mode:
        raise SnapshotCheckError(
            "Cloud mode is already on for this profile. This check will not change it."
        )
    if not config.drive_folder_id:
        raise SnapshotCheckError(
            "No Drive folder is saved for this profile. "
            "Run python -m finance.google_setup first, with FINANCETRACKER_CONFIG_DIR "
            "still set to this dry-run profile."
        )
    count = _count_for(parsed.command, save_count)
    print(f"Profile: {app_config_dir()}", file=output)
    print(f"Credential store: {keyring_service_name()}", file=output)
    print("The local finance file is not read or written.", file=output)
    print(
        "These snapshots use a fixed test passphrase. It is not a finance passphrase. "
        "Do not reuse it for real data.",
        file=output,
    )
    print(f"Test passphrase: {PASSPHRASE}", file=output)
    open_adapter = opener if opener is not None else _open_live
    adapter = open_adapter(config.drive_folder_id)
    try:
        folder = adapter.describe_folder()
        _confirm_folder(prompt, output, folder.name, folder.owner_display_name, folder.owner_email)
        if parsed.command == "fill":
            return _fill(adapter, output, count)
        if parsed.command == "extend":
            return _extend(adapter, output, count)
        if parsed.command == "open":
            return _open(adapter, output)
        if parsed.command == "clean":
            return _clean(adapter, output)
        raise SnapshotCheckError("Choose fill, extend, open, or clean.")
    finally:
        adapter.close()


def _fill(adapter: DriveAdapter, output: TextIO, count: int) -> int:
    children = adapter.list_children()
    if children:
        print(
            "This folder is not empty. Empty it in the Drive website before fill. "
            "Nothing was uploaded.",
            file=output,
        )
        return 1
    repo = _repository(adapter)
    loaded = repo.load_latest()
    if loaded.outcome != OUTCOME_EMPTY:
        print(loaded.message, file=output)
        return 1
    store = FinanceStore(_memory_path())
    trashed: list[str] = []
    for index in range(1, count + 1):
        _add_synthetic(store, index)
        saved = repo.save(store)
        if saved.outcome != OUTCOME_SAVED:
            print(saved.message, file=output)
            print("The save was not confirmed. Earlier synthetic snapshots were left in place.", file=output)
            return 1
        trashed.extend(action.name for action in saved.prune if action.status == "trashed")
    remaining = _remaining(count)
    return _report_folder(
        adapter,
        output,
        expected_remaining=remaining,
        expected_trashed=count - remaining,
        trashed=trashed,
    )


def _extend(adapter: DriveAdapter, output: TextIO, count: int) -> int:
    before = _snapshots(adapter.list_children())
    if not before:
        print("There is no snapshot to extend. Run fill from the first account first.", file=output)
        return 1
    owned_before = {item.file_id: item.can_trash for item in before}
    repo = _repository(adapter)
    loaded = repo.load_latest()
    if loaded.outcome != OUTCOME_LOADED or loaded.store is None:
        print(loaded.message, file=output)
        return 1
    store = loaded.store
    for index in range(1, count + 1):
        _add_synthetic(store, 1000 + index)
        saved = repo.save(store)
        if saved.outcome != OUTCOME_SAVED:
            print(saved.message, file=output)
            return 1
        for action in saved.prune:
            if action.status == NOT_OWNER_SKIPPED:
                print(f"{action.name}: {NOT_OWNER_SKIPPED}", file=output)
            elif action.status == "trashed":
                print(f"Moved to this account's trash: {action.name}", file=output)
    after = {item.file_id for item in _snapshots(adapter.list_children())}
    removed = [file_id for file_id in owned_before if file_id not in after]
    stolen = [file_id for file_id in removed if not owned_before[file_id]]
    if stolen:
        print(
            "This account removed a snapshot it does not own. Those files should still be in the folder.",
            file=output,
        )
        return 1
    others = sum(1 for can_trash in owned_before.values() if not can_trash)
    print(
        f"Left {others} snapshot(s) this account cannot trash. They are still in the folder.",
        file=output,
    )
    return _open(adapter, output)


def _open(adapter: DriveAdapter, output: TextIO) -> int:
    snapshots = _snapshots(adapter.list_children())
    if not snapshots:
        print("There is no snapshot in the folder.", file=output)
        return 1
    failures = 0
    for item in snapshots:
        problem = _inspect_bytes(adapter.download(item.file_id))
        if problem is None:
            print(f"Opened generation {item.generation}.", file=output)
            continue
        failures += 1
        print(f"Generation {item.generation}: {problem}", file=output)
    local_problem = _inspect_local(encrypted_snapshots_dir())
    if local_problem is not None:
        print(local_problem, file=output)
        failures += 1
    else:
        print(
            f"Local encrypted copies: {encrypted_snapshots_dir()}",
            file=output,
        )
        print(
            "Each local file is an encrypted snapshot and does not contain the synthetic marker.",
            file=output,
        )
    if failures:
        return 1
    print("Cloud mode was not turned on.", file=output)
    if load_local_config().cloud_mode:
        print("Cloud mode is on. This check must not do that.", file=output)
        return 1
    return 0


def _clean(adapter: DriveAdapter, output: TextIO) -> int:
    snapshots = _snapshots(adapter.list_children())
    if not snapshots:
        print("There is no snapshot to move to the trash.", file=output)
        return 0
    for item in snapshots:
        result = adapter.trash(item.file_id)
        print(f"{item.name}: {result.status_text}", file=output)
    print(
        "Files this account owns were moved to its Drive trash. They were not permanently deleted. "
        "Run clean on the other account for files it owns, then leave the folder empty.",
        file=output,
    )
    return 0


def _report_folder(
    adapter: DriveAdapter,
    output: TextIO,
    *,
    expected_remaining: int,
    expected_trashed: int,
    trashed: Sequence[str],
) -> int:
    remaining = _snapshots(adapter.list_children())
    print(f"Snapshots still in the folder: {len(remaining)}.", file=output)
    print(f"Moved to this account's trash by this check: {len(trashed)}.", file=output)
    for name in trashed:
        print(f"Trash: {name}", file=output)
    if len(remaining) != expected_remaining or len(trashed) != expected_trashed:
        print(
            f"Expected {expected_remaining} snapshots to remain and {expected_trashed} "
            "to be moved to the trash. Nothing was permanently deleted.",
            file=output,
        )
        return 1
    opened = _open(adapter, output)
    if opened != 0:
        return opened
    print(
        "In the Drive website, confirm the trashed names are in this account's trash, "
        "the folder has only the remaining encrypted snapshots, and nothing outside the folder changed.",
        file=output,
    )
    return 0


def _repository(adapter: DriveAdapter) -> SnapshotRepository:
    return SnapshotRepository(adapter, PASSPHRASE, memory_path=_memory_path())


def _add_synthetic(store: FinanceStore, index: int) -> None:
    store.add_spending(
        amount=Decimal(index),
        category="Misc",
        on_date=date(2026, 1, 1),
        user="Boris",
        note=f"{MARKER}-{index}",
        recorded_on=date(2026, 1, 2),
    )


def _inspect_bytes(data: bytes) -> str | None:
    if _MARKER_BYTES in data:
        return "A file contains the synthetic marker before decryption."
    if not data.startswith(b"FTEN"):
        return "A file is not an encrypted snapshot."
    try:
        opened = decrypt(data, PASSPHRASE)
    except CryptoError as exc:
        return str(exc)
    if _MARKER_BYTES not in opened.plaintext:
        return "A decrypted snapshot did not contain the synthetic marker."
    try:
        FinanceStore.from_json_bytes(opened.plaintext, _memory_path())
    except Exception:
        return "A decrypted snapshot is not a valid finance document."
    return None


def _inspect_local(directory: Path) -> str | None:
    if not directory.is_dir():
        return "The local encrypted-copy folder was not created."
    files = [path for path in directory.iterdir() if path.is_file()]
    if any(path.suffix == ".tmp" for path in files):
        return "A temporary file was left in the local encrypted-copy folder."
    snapshots = [path for path in files if generation_of_name(path.name) is not None]
    if len(snapshots) != len(files):
        return "The local folder contains a file that is not an encrypted snapshot."
    if not snapshots:
        return "The local folder has no encrypted snapshot."
    if len(snapshots) > LOCAL_ENCRYPTED_COPIES:
        return "The local folder has more than 10 encrypted snapshots."
    for path in snapshots:
        data = path.read_bytes()
        if not data.startswith(b"FTEN") or _MARKER_BYTES in data:
            return "A local file is not an encrypted snapshot."
    return None


def _snapshots(files: Sequence[DriveFile]) -> list[DriveFile]:
    return [item for item in files if item.generation is not None and not item.trashed]


def _remaining(count: int) -> int:
    if count <= KEEP_RECENT_SNAPSHOTS:
        return count
    return KEEP_RECENT_SNAPSHOTS


def _count_for(command: str, save_count: int | None) -> int:
    if save_count is not None:
        if save_count < 1:
            raise SnapshotCheckError("The snapshot count must be at least 1.")
        return save_count
    if command == "extend":
        return EXTEND_COUNT
    return FILL_COUNT


def _memory_path() -> Path:
    return encrypted_snapshots_dir().parent / "snapshot-memory.json"


def _confirm_folder(prompt, output, name: str, display_name: str, email: str) -> None:
    print(f"Folder name: {name}", file=output)
    owner = display_name or email or "unknown"
    if display_name and email:
        owner = f"{display_name} <{email}>"
    print(f"Owner: {owner}", file=output)
    print(
        "This check only uses that folder. Type the folder name exactly to continue.",
        file=output,
    )
    typed = prompt("Folder name: ").strip()
    if typed != name:
        raise CheckCancelled


def _reject_unsafe_profile() -> None:
    raw = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if not raw:
        raise SnapshotCheckError(
            "Set FINANCETRACKER_CONFIG_DIR to a dry-run profile before this check. "
            "The production profile is refused."
        )
    directory = app_config_dir()
    project = default_data_path().resolve().parent
    if directory == project or project in directory.parents or directory in project.parents:
        raise SnapshotCheckError(
            "FINANCETRACKER_CONFIG_DIR points at the FinanceTracker source directory. "
            "Choose a folder outside the project."
        )
    if keyring_service_name() == PRODUCTION_KEYRING_SERVICE:
        raise SnapshotCheckError("Refusing to run with the production Google credential.")


def _open_live(folder_id: str) -> GoogleDriveAdapter:
    if not is_drive_item_id(folder_id):
        raise SnapshotCheckError("The saved Drive folder id cannot be used.")
    client = load_desktop_client(oauth_client_file())
    authorization = GoogleAuthorization(default_secret_store(), client)
    result = authorization.authorize(interactive=True)
    return GoogleDriveAdapter(folder_id, credentials=result.credentials)


def _finance_file_stamp() -> tuple[bool, int | None]:
    path = default_data_path()
    if not path.exists():
        return False, None
    return True, path.stat().st_mtime_ns


def _console_prompt(question: str) -> str:
    try:
        return input(question)
    except EOFError:
        raise CheckCancelled from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m finance.snapshot_check",
        description=(
            "Encrypted snapshot check. Uses the dry-run profile and synthetic records only."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fill", help="Upload synthetic snapshots and apply retention.")
    commands.add_parser("extend", help="Save more snapshots without trashing the other account's files.")
    commands.add_parser("open", help="Decrypt every remaining snapshot.")
    commands.add_parser("clean", help="Move this account's snapshots to the trash.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
