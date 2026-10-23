"""Manual check that two Google accounts can share one Drive folder.

Run ``python -m finance.drive_check``. This is the Step 5 go/no-go check.
It uploads a few small synthetic files into the folder already saved for
this profile. It does not read or write the local finance file, it does not
ask for the finance passphrase, and it does not turn cloud mode on.

``FINANCETRACKER_CONFIG_DIR`` must be set to a dry-run profile. The command
refuses the production profile and refuses a profile inside the source
tree. Use the empty shared test folder from the README, then leave that
folder empty again before any later sync step.

The bytes it writes are fixed markers such as ``marker=snapshot-a``. They
are not finance records. A file whose contents are not one of those markers
is left unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import timezone
from typing import TextIO

from .config import default_data_path
from .drive_adapter import (
    LOCK_FILE_NAME,
    DriveAdapter,
    DriveError,
    GoogleDriveAdapter,
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
from .google_setup import REQUEST_TIMEOUT_SECONDS
from .profile import (
    CONFIG_DIR_ENV,
    PRODUCTION_KEYRING_SERVICE,
    ConfigError,
    app_config_dir,
    keyring_service_name,
    load_local_config,
    oauth_client_file,
)

PROBE_GENERATION = 1
SNAPSHOT_V1 = b"finance-tracker-drive-check\nmarker=snapshot-a\n"
SNAPSHOT_V2 = b"finance-tracker-drive-check\nmarker=snapshot-b\n"
LOCK_V1 = b'{"format_version":1,"state":"free","marker":"drive-check-a"}\n'
LOCK_V2 = b'{"format_version":1,"state":"free","marker":"drive-check-b"}\n'

class DriveCheckError(Exception):
    """This check will not start. The message is safe to show."""


class CheckCancelled(Exception):
    """The user did not type the folder name."""


_CHECK_ERRORS = (
    DriveError,
    DriveCheckError,
    FolderSetupError,
    ConfigError,
    CredentialStoreError,
    NeedsInteractiveSignIn,
    OAuthClientError,
    AuthorizationError,
)


def run_drive_check(
    argv: list[str] | None = None,
    *,
    prompt: Callable[[str], str] | None = None,
    output: TextIO | None = None,
    opener: Callable[[str], DriveAdapter] | None = None,
) -> int:
    """Run one subcommand. Returns 0 on success, 2 when cancelled, 1 otherwise."""
    write = output if output is not None else sys.stdout
    ask = prompt if prompt is not None else _console_prompt
    arguments = list(sys.argv[1:] if argv is None else argv)
    before = _finance_file_stamp()
    code = 1
    try:
        try:
            parsed = _parser().parse_args(arguments)
        except SystemExit as exc:
            status = exc.code
            return status if isinstance(status, int) else 1
        code = _run_parsed(parsed, ask, write, opener)
    except CheckCancelled:
        print("Drive check cancelled.", file=write)
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
    return code


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        return run_drive_check(argv)
    except KeyboardInterrupt:
        print("\nDrive check cancelled.")
        return 2


def _run_parsed(parsed, prompt, output, opener) -> int:
    _reject_unsafe_profile()
    config = load_local_config()
    if config.cloud_mode:
        raise DriveCheckError(
            "Cloud mode is already on for this profile. This check will not change it."
        )
    if not config.drive_folder_id:
        raise DriveCheckError(
            "No Drive folder is saved for this profile. "
            "Run python -m finance.google_setup first, with FINANCETRACKER_CONFIG_DIR "
            "still set to this dry-run profile."
        )
    print(f"Profile: {app_config_dir()}", file=output)
    print(f"Credential store: {keyring_service_name()}", file=output)
    print("The local finance file is not read or written.", file=output)
    print(f"Saved folder id: {config.drive_folder_id}", file=output)
    open_adapter = opener if opener is not None else _open_live
    adapter = open_adapter(config.drive_folder_id)
    try:
        if parsed.command == "ping":
            return _ping(adapter, output)
        if parsed.command == "create":
            return _create(adapter, prompt, output)
        if parsed.command == "partner":
            return _partner(adapter, parsed.snapshot_id, parsed.lock_id, prompt, output)
        if parsed.command == "finish":
            return _finish(adapter, parsed.snapshot_id, parsed.lock_id, prompt, output)
        raise DriveCheckError("Choose create, partner, finish, or ping.")
    finally:
        adapter.close()


def _ping(adapter: DriveAdapter, output: TextIO) -> int:
    print("Asking Drive for the saved folder and its clock...", file=output)
    folder = adapter.describe_folder()
    when = adapter.server_time()
    print(f"Folder name: {folder.name}", file=output)
    print(f"Owner: {_owner_text(folder.owner_display_name, folder.owner_email)}", file=output)
    printed = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"Drive time: {printed}", file=output)
    print(
        "Drive answered. With the network disconnected, this command should "
        f"fail within {REQUEST_TIMEOUT_SECONDS} seconds instead of hanging.",
        file=output,
    )
    return 0


def _create(adapter: DriveAdapter, prompt, output: TextIO) -> int:
    folder = adapter.describe_folder()
    _confirm_folder(prompt, output, folder.name, folder.owner_display_name, folder.owner_email)
    listing = adapter.list_for_generation(())
    locks = [item for item in listing.files if item.name == LOCK_FILE_NAME]
    if len(locks) > 1:
        print(
            "More than one lock file is in this folder. Remove the extra file "
            "in the Drive website. Nothing was uploaded.",
            file=output,
        )
        return 1
    lock_id = None
    if len(locks) == 1:
        current = adapter.read_lock(locks[0].file_id)
        if current not in (LOCK_V1, LOCK_V2):
            print(
                "The folder already has a lock file that is not from this check. "
                "Nothing was uploaded.",
                file=output,
            )
            return 1
        lock_id = locks[0].file_id
        print(f"Keeping the existing lock file {lock_id}.", file=output)
    uploaded = adapter.upload_snapshot(PROBE_GENERATION, SNAPSHOT_V1)
    if lock_id is None:
        created = adapter.create_lock(LOCK_V1)
        lock_id = created.file.file_id
    print("Uploaded a synthetic Drive check. These are not finance records.", file=output)
    print(f"Snapshot id: {uploaded.file.file_id}", file=output)
    print(f"Snapshot name: {uploaded.file.name}", file=output)
    print(f"Lock id: {lock_id}", file=output)
    print("files.get of the new snapshot id succeeded.", file=output)
    if uploaded.file.file_id in uploaded.listing.included_by_get:
        print(
            "The folder listing had not caught up. The snapshot is still treated as saved.",
            file=output,
        )
    else:
        print("The folder listing already included the new snapshot.", file=output)
    print(
        "On the other Google account, run: "
        f"python -m finance.drive_check partner {uploaded.file.file_id} {lock_id}",
        file=output,
    )
    return 0


def _partner(adapter, snapshot_id: str, lock_id: str, prompt, output: TextIO) -> int:
    _require_id(snapshot_id)
    _require_id(lock_id)
    folder = adapter.describe_folder()
    _confirm_folder(prompt, output, folder.name, folder.owner_display_name, folder.owner_email)
    listing = adapter.list_for_generation((snapshot_id, lock_id))
    present = {item.file_id for item in listing.files}
    if snapshot_id not in present or lock_id not in present:
        print("The folder listing does not include both file ids.", file=output)
        return 1
    print("Both files are visible in the shared folder.", file=output)
    snapshot = adapter.download(snapshot_id)
    if snapshot == SNAPSHOT_V1:
        adapter.update_content(snapshot_id, SNAPSHOT_V2)
        print("Downloaded the snapshot by id and updated its contents.", file=output)
    elif snapshot == SNAPSHOT_V2:
        print("The snapshot already had the updated check contents.", file=output)
    else:
        _mismatch(output, "snapshot", len(snapshot))
        return 1
    lock = adapter.read_lock(lock_id)
    if lock == LOCK_V1:
        adapter.update_lock(lock_id, LOCK_V2)
        print("Updated the lock file contents. The other account should read this id.", file=output)
    elif lock == LOCK_V2:
        print("The lock already had the updated check contents.", file=output)
    else:
        _mismatch(output, "lock", len(lock))
        return 1
    if adapter.download(snapshot_id) != SNAPSHOT_V2 or adapter.read_lock(lock_id) != LOCK_V2:
        print("Reading the files back did not show the updated check contents.", file=output)
        return 1
    result = adapter.trash(snapshot_id)
    print(result.status_text, file=output)
    if result.trashed or not result.skipped_not_owner:
        print(
            "This account was able to trash the file, or the trash result was not a skip. "
            "The file should still be in the folder. Nothing was permanently deleted.",
            file=output,
        )
        return 1
    print("The other account's file is still in the folder.", file=output)
    print(
        "On the account that created the files, run: "
        f"python -m finance.drive_check finish {snapshot_id} {lock_id}",
        file=output,
    )
    return 0


def _finish(adapter, snapshot_id: str, lock_id: str, prompt, output: TextIO) -> int:
    _require_id(snapshot_id)
    _require_id(lock_id)
    folder = adapter.describe_folder()
    _confirm_folder(prompt, output, folder.name, folder.owner_display_name, folder.owner_email)
    snapshot = adapter.download(snapshot_id)
    if snapshot != SNAPSHOT_V2:
        _mismatch(output, "snapshot", len(snapshot))
        return 1
    lock = adapter.read_lock(lock_id)
    if lock != LOCK_V2:
        _mismatch(output, "lock", len(lock))
        return 1
    print(
        "The lock contents match the update from the other account, read back by file id.",
        file=output,
    )
    snapshot_result = adapter.trash(snapshot_id)
    lock_result = adapter.trash(lock_id)
    print(f"Snapshot: {snapshot_result.status_text}", file=output)
    print(f"Lock: {lock_result.status_text}", file=output)
    if not snapshot_result.trashed or not lock_result.trashed:
        print(
            "This account could not move both files to the trash. "
            "They were not permanently deleted.",
            file=output,
        )
        return 1
    print(
        "Both files were moved to this account's Drive trash. They were not permanently deleted.",
        file=output,
    )
    print(
        "In the Drive website, confirm the test folder is empty, both files are in "
        "this account's trash, and nothing outside the test folder changed.",
        file=output,
    )
    print("Leave the folder empty before the next sync step.", file=output)
    return 0


def _confirm_folder(prompt, output, name: str, display_name: str, email: str) -> None:
    print(f"Folder name: {name}", file=output)
    print(f"Owner: {_owner_text(display_name, email)}", file=output)
    print(
        "This check only uses that folder. Type the folder name exactly to continue.",
        file=output,
    )
    typed = prompt("Folder name: ").strip()
    if typed != name:
        raise CheckCancelled


def _mismatch(output: TextIO, label: str, size: int) -> None:
    print(
        f"The {label} contents are not the drive-check marker. It was not changed.",
        file=output,
    )
    print(f"Length: {size} bytes.", file=output)


def _require_id(file_id: str) -> None:
    if not is_drive_item_id(file_id):
        raise DriveCheckError("That Drive file id cannot be used.")


def _owner_text(display_name: str, email: str) -> str:
    if display_name and email:
        return f"{display_name} <{email}>"
    return display_name or email or "unknown"


def _reject_unsafe_profile() -> None:
    raw = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if not raw:
        raise DriveCheckError(
            "Set FINANCETRACKER_CONFIG_DIR to a dry-run profile before this check. "
            "The production profile is refused."
        )
    directory = app_config_dir()
    project = default_data_path().resolve().parent
    if directory == project or project in directory.parents or directory in project.parents:
        raise DriveCheckError(
            "FINANCETRACKER_CONFIG_DIR points at the FinanceTracker source directory. "
            "Choose a folder outside the project."
        )
    if keyring_service_name() == PRODUCTION_KEYRING_SERVICE:
        raise DriveCheckError(
            "Refusing to run with the production Google credential."
        )


def _open_live(folder_id: str) -> GoogleDriveAdapter:
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
        prog="python -m finance.drive_check",
        description=(
            "Shared-folder Drive check. Uses the dry-run profile and synthetic files only."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="Upload one synthetic snapshot and a lock file.")
    partner = commands.add_parser(
        "partner",
        help="From the other account: list, download, update, and try to trash.",
    )
    partner.add_argument("snapshot_id")
    partner.add_argument("lock_id")
    finish = commands.add_parser(
        "finish",
        help="From the creating account: confirm the update, then trash both files.",
    )
    finish.add_argument("snapshot_id")
    finish.add_argument("lock_id")
    commands.add_parser("ping", help="Read the folder and Drive's clock, or time out.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
