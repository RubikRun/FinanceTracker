"""Manual check for the Drive lock.

Run ``python -m finance.lock_check``. This talks to the Drive folder already
saved for this profile. It does not read or write the local finance file, it
does not turn cloud mode on, and it does not delete the lock file.

``FINANCETRACKER_CONFIG_DIR`` must be set to a dry-run profile. The command
refuses the production profile and refuses a profile inside the source tree.
Empty the shared test folder first, or leave only synthetic snapshots in it.
Snapshot files are left where they are.

``hold`` takes the lock and heartbeats until you press Enter, then releases
it. ``open`` reports the lock. If someone else holds it, the report is
read-only and the file is left unchanged. ``plant`` writes a lock held by
another computer so you can see the confirmation prompt. ``idle`` takes the
lock and waits for the idle release.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TextIO

from .config import USER_BORIS, USERS, default_data_path
from .drive_adapter import (
    LOCK_FILE_NAME,
    DriveAdapter,
    DriveError,
    DriveFile,
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
from .lock import (
    CHOICE_CREATE_LOCK,
    CHOICE_RECOVER,
    CHOICE_TAKE_OVER,
    IDLE_RELEASE_AFTER,
    MODE_PAUSED,
    STALE_AFTER,
    TIMER_POLL_SECONDS,
    LockDocument,
    LockError,
    LockSession,
    LockStatus,
    encode_lock,
)
from .profile import (
    CONFIG_DIR_ENV,
    PRODUCTION_KEYRING_SERVICE,
    ConfigError,
    app_config_dir,
    ensure_machine_identity,
    keyring_service_name,
    load_local_config,
    oauth_client_file,
)

OTHER_MACHINE_ID = "ab" * 32
OTHER_MACHINE_LABEL = "Other computer"
_PHRASES = {
    CHOICE_CREATE_LOCK: "CREATE LOCK",
    CHOICE_RECOVER: "RECOVER",
    CHOICE_TAKE_OVER: "TAKE OVER",
}


class LockCheckError(Exception):
    """This check will not start. The message is safe to show."""


class CheckCancelled(Exception):
    """The user did not type the folder name."""


_CHECK_ERRORS = (
    DriveError,
    LockCheckError,
    LockError,
    FolderSetupError,
    ConfigError,
    CredentialStoreError,
    NeedsInteractiveSignIn,
    OAuthClientError,
    AuthorizationError,
)


def run_lock_check(
    argv: list[str] | None = None,
    *,
    prompt: Callable[[str], str] | None = None,
    output: TextIO | None = None,
    opener: Callable[[str], DriveAdapter] | None = None,
    confirmation_sleep: Callable[[float], None] | None = None,
    idle_after: timedelta | None = None,
    poll_sleep: Callable[[float], None] | None = None,
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
        code = _run_parsed(
            parsed,
            ask,
            write,
            opener,
            confirmation_sleep,
            idle_after,
            poll_sleep if poll_sleep is not None else time.sleep,
        )
    except CheckCancelled:
        print("Lock check cancelled.", file=write)
        code = 2
    except _CHECK_ERRORS as exc:
        print(str(exc), file=write)
        code = 1
    if _finance_file_stamp() != before:
        print("The local finance file changed. This check must not do that.", file=write)
        return 1
    return code


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        return run_lock_check(argv)
    except KeyboardInterrupt:
        print("\nLock check cancelled.")
        return 2


def _run_parsed(
    parsed,
    prompt,
    output,
    opener,
    confirmation_sleep,
    idle_after: timedelta | None,
    poll_sleep: Callable[[float], None],
) -> int:
    _reject_unsafe_profile()
    config = load_local_config()
    if config.cloud_mode:
        raise LockCheckError(
            "Cloud mode is already on for this profile. This check will not change it."
        )
    if not config.drive_folder_id:
        raise LockCheckError(
            "No Drive folder is saved for this profile. "
            "Run python -m finance.google_setup first, with FINANCETRACKER_CONFIG_DIR "
            "still set to this dry-run profile."
        )
    if parsed.owner not in USERS:
        raise LockCheckError("The owner must be Boris or Milena.")
    print(f"Profile: {app_config_dir()}", file=output)
    print(f"Credential store: {keyring_service_name()}", file=output)
    print("The local finance file is not read or written.", file=output)
    print("Cloud mode stays off. The lock file is not deleted.", file=output)
    open_adapter = opener if opener is not None else _open_live
    adapter = open_adapter(config.drive_folder_id)
    session: LockSession | None = None
    try:
        folder = adapter.describe_folder()
        _confirm_folder(prompt, output, folder.name, folder.owner_display_name, folder.owner_email)
        if parsed.command == "plant":
            return _plant(adapter, output, owner=parsed.owner, fresh=parsed.fresh)
        session = LockSession(
            adapter,
            owner_label=parsed.owner,
            sleep=confirmation_sleep,
            idle_after=idle_after or IDLE_RELEASE_AFTER,
        )
        if parsed.command == "idle":
            return _idle(session, output, poll_sleep, idle_after or IDLE_RELEASE_AFTER)
        return _open_or_hold(session, prompt, output, holding=parsed.command == "hold")
    finally:
        if session is not None:
            session.close()
        adapter.close()


def _open_or_hold(session: LockSession, prompt, output: TextIO, *, holding: bool) -> int:
    status = session.start()
    _print_status(status, output)
    status = _confirm_choice(session, status, prompt, output)
    _print_status(status, output)
    if not status.can_edit:
        if holding:
            print("The lock was not taken, so this window will not stay open.", file=output)
            return 1
        return 0
    print(
        "Heartbeat is about every 30 seconds. Press Enter to release the lock.",
        file=output,
    )
    session.start_timer()
    prompt("Release: ")
    released = session.close()
    _print_status(released, output)
    if load_local_config().cloud_mode:
        print("Cloud mode was turned on. This check must not do that.", file=output)
        return 1
    print("Cloud mode was not turned on.", file=output)
    return 0


def _idle(
    session: LockSession,
    output: TextIO,
    poll_sleep: Callable[[float], None],
    idle_after: timedelta,
) -> int:
    status = session.start()
    _print_status(status, output)
    if not status.can_edit:
        print("The lock was not taken, so idle release was not started.", file=output)
        return 1
    limit = idle_after + timedelta(seconds=90)
    print(
        "Not recording activity. The lock should become free after about "
        f"{int(idle_after.total_seconds() // 60)} minutes.",
        file=output,
    )
    deadline = time.monotonic() + limit.total_seconds()
    while time.monotonic() < deadline:
        status = session.poll()
        _print_status(status, output)
        if status.mode == MODE_PAUSED:
            print("The lock is free.", file=output)
            return 0
        if not status.can_edit:
            return 1
        poll_sleep(TIMER_POLL_SECONDS)
    print("The lock was still held when the wait ended.", file=output)
    return 1


def _confirm_choice(session: LockSession, status: LockStatus, prompt, output: TextIO) -> LockStatus:
    phrase = ""
    choice = ""
    for item in status.choices:
        if item in _PHRASES:
            choice = item
            phrase = _PHRASES[item]
            break
    if not phrase:
        return status
    print(f"Type {phrase} to continue. Anything else leaves the lock unchanged.", file=output)
    typed = prompt("Confirmation: ").strip()
    if typed != phrase:
        print("Nothing was changed.", file=output)
        return status
    if choice == CHOICE_CREATE_LOCK:
        return session.create_missing_lock()
    if choice in (CHOICE_RECOVER, CHOICE_TAKE_OVER):
        return session.recover(status)
    return status


def _plant(adapter: DriveAdapter, output: TextIO, *, owner: str, fresh: bool) -> int:
    before = _snapshot_ids(adapter.list_children())
    locks = [item for item in adapter.list_children() if item.name == LOCK_FILE_NAME]
    if len(locks) > 1:
        print(
            "More than one lock file is in this folder. Remove the extra file "
            "in the Drive website. Nothing was changed.",
            file=output,
        )
        return 1
    now = adapter.server_time()
    heartbeat = now if fresh else now - (STALE_AFTER + timedelta(minutes=1))
    document = LockDocument(
        format_version=1,
        state="held",
        session_token="cd" * 32,
        owner_label=owner,
        machine_id=OTHER_MACHINE_ID,
        machine_label=OTHER_MACHINE_LABEL,
        acquired_at_utc=heartbeat,
        heartbeat_at_utc=heartbeat,
    )
    payload = encode_lock(document)
    if locks:
        adapter.update_lock(locks[0].file_id, payload)
        print(f"Updated lock file {locks[0].file_id}.", file=output)
    else:
        created = adapter.create_lock(payload)
        print(f"Created lock file {created.file.file_id}.", file=output)
    after = _snapshot_ids(adapter.list_children())
    if after != before:
        print("Snapshot files changed. They should have been left in place.", file=output)
        return 1
    kind = "fresh" if fresh else "stale"
    print(
        f"The lock is {kind} and held by {owner} on {OTHER_MACHINE_LABEL}. "
        "Snapshot files were left in place.",
        file=output,
    )
    if load_local_config().cloud_mode:
        print("Cloud mode was turned on. This check must not do that.", file=output)
        return 1
    print("Cloud mode was not turned on.", file=output)
    return 0


def _print_status(status: LockStatus, output: TextIO) -> None:
    print(f"Mode: {status.mode}", file=output)
    print(status.message, file=output)
    if status.lock_file_id:
        print(f"Lock file id: {status.lock_file_id}", file=output)


def _snapshot_ids(files: Sequence[DriveFile]) -> tuple[str, ...]:
    return tuple(sorted(item.file_id for item in files if item.generation is not None))


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
        raise LockCheckError(
            "Set FINANCETRACKER_CONFIG_DIR to a dry-run profile before this check. "
            "The production profile is refused."
        )
    directory = app_config_dir()
    project = default_data_path().resolve().parent
    if directory == project or project in directory.parents or directory in project.parents:
        raise LockCheckError(
            "FINANCETRACKER_CONFIG_DIR points at the FinanceTracker source directory. "
            "Choose a folder outside the project."
        )
    if keyring_service_name() == PRODUCTION_KEYRING_SERVICE:
        raise LockCheckError("Refusing to run with the production Google credential.")


def _open_live(folder_id: str) -> GoogleDriveAdapter:
    if not is_drive_item_id(folder_id):
        raise LockCheckError("The saved Drive folder id cannot be used.")
    ensure_machine_identity()
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
        prog="python -m finance.lock_check",
        description="Drive lock check. Uses the dry-run profile. No finance file is written.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    owner = argparse.ArgumentParser(add_help=False)
    owner.add_argument("--owner", choices=USERS, default=USER_BORIS)
    commands.add_parser(
        "hold",
        parents=[owner],
        help="Take the lock until you press Enter, then release it.",
    )
    commands.add_parser(
        "open",
        parents=[owner],
        help="Report the lock, and take it when it is free.",
    )
    plant = commands.add_parser(
        "plant",
        parents=[owner],
        help="Write a lock held by another computer.",
    )
    plant.add_argument(
        "--fresh",
        action="store_true",
        help="Heartbeat is current. Without this, the heartbeat is stale.",
    )
    commands.add_parser(
        "idle",
        parents=[owner],
        help="Take the lock and wait until idle release frees it.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
