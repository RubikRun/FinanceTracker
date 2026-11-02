"""Manual check for the cloud session and one-time migration.

Run ``python -m finance.session_check``. ``FINANCETRACKER_CONFIG_DIR`` must be
a dry-run profile that already has a Google sign-in and a test folder. The
production profile is refused.

``migrate`` reads a copy of a finance file that you pass with ``--source``.
It refuses the real ``finance_data.json`` next to the program. The copy is
not modified. A dated plaintext backup is written to ``--backup-dir``, which
must be outside the shared Drive folder and outside the local encrypted-copy
folder. ``create-empty`` uploads an empty generation-1 snapshot. ``open``
loads the newest snapshot.

The FinanceTracker window does not use this session yet. Turning cloud mode
on here affects only this dry-run profile.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .config import USER_BORIS, USERS, default_data_path
from .drive_adapter import DriveAdapter, DriveError, GoogleDriveAdapter
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
    keyring_service_name,
    load_local_config,
    oauth_client_file,
    save_local_config,
)
from .session import (
    MODE_ACCESS_LOST,
    MODE_CLOSED,
    MODE_EDITABLE,
    MODE_OFFLINE,
    MODE_PAUSED,
    MODE_READ_ONLY,
    MODE_SETUP,
    CHOICE_RETRY_PASSPHRASE,
    FinanceSession,
    SessionError,
    SessionStatus,
)

_OPENED = (MODE_EDITABLE, MODE_READ_ONLY, MODE_PAUSED, MODE_OFFLINE)


class SessionCheckError(Exception):
    """This check will not start. The message is safe to show."""


class CheckCancelled(Exception):
    """The user did not confirm the folder."""


_CHECK_ERRORS = (
    DriveError,
    SessionCheckError,
    SessionError,
    FolderSetupError,
    ConfigError,
    CredentialStoreError,
    NeedsInteractiveSignIn,
    OAuthClientError,
    AuthorizationError,
)


def run_session_check(
    argv: list[str] | None = None,
    *,
    prompt: Callable[[str], str] | None = None,
    output: TextIO | None = None,
    opener: Callable[[str], DriveAdapter] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Run one subcommand. Returns 0 on success, 2 when cancelled, and 1 otherwise."""
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
        code = _run_parsed(parsed, ask, write, opener, sleep)
    except CheckCancelled:
        print("Session check cancelled.", file=write)
        code = 2
    except _CHECK_ERRORS as exc:
        print(str(exc), file=write)
        code = 1
    if _finance_file_stamp() != before:
        print("The finance file next to the program changed. This check must not do that.", file=write)
        return 1
    return code


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        return run_session_check(argv)
    except KeyboardInterrupt:
        print("\nSession check cancelled.")
        return 2


def _run_parsed(parsed, prompt, output: TextIO, opener, sleep) -> int:
    _reject_unsafe_profile()
    config = load_local_config()
    folder_id = parsed.folder_id or config.drive_folder_id
    if not folder_id:
        raise SessionCheckError(
            "No Drive folder is saved for this profile. "
            "Run python -m finance.google_setup first, with FINANCETRACKER_CONFIG_DIR "
            "still set to this dry-run profile."
        )
    previous_folder_id = config.drive_folder_id
    if parsed.folder_id and parsed.folder_id != config.drive_folder_id:
        print(
            "Using the folder id from the command for this run. "
            "It is kept only if cloud mode is turned on.",
            file=output,
        )
    legacy = _legacy_for(parsed)
    if parsed.command in ("migrate", "create-empty") and config.cloud_mode:
        raise SessionCheckError(
            "Cloud mode is already on for this profile. "
            "This command will not import or create another dataset."
        )
    print(f"Profile: {app_config_dir()}", file=output)
    print(
        "The FinanceTracker window does not use this session yet. "
        "Cloud mode here applies only to this dry-run profile.",
        file=output,
    )
    print(
        "A forgotten passphrase makes the data and every backup permanently unreadable. "
        "Store it in a password manager.",
        file=output,
    )
    open_adapter = opener if opener is not None else _open_live
    adapter = open_adapter(folder_id)
    session: FinanceSession | None = None
    try:
        folder = adapter.describe_folder()
        _confirm_folder(prompt, output, folder.name, folder.owner_display_name, folder.owner_email)
        session = FinanceSession(
            adapter,
            folder_id=folder_id,
            owner_label=parsed.owner,
            legacy_path=legacy,
            sleep=sleep,
        )
        if parsed.command == "migrate":
            code = _migrate(session, prompt, output, Path(parsed.backup_dir))
        elif parsed.command == "create-empty":
            code = _create_empty(session, prompt, output)
        else:
            code = _open(session, prompt, output)
        memory = app_config_dir() / "snapshot-memory.json"
        if memory.exists():
            print("A plaintext snapshot file was written. That should not happen.", file=output)
            return 1
        return code
    finally:
        if session is not None:
            session.close(discard=True)
        adapter.close()
        _restore_folder_id(parsed.folder_id, previous_folder_id)


def _migrate(session: FinanceSession, prompt, output: TextIO, backup_dir: Path) -> int:
    print(
        "The source file will only be read. A plaintext backup is written first. "
        "Keep that backup outside the shared Drive folder.",
        file=output,
    )
    first = prompt("Passphrase: ")
    second = prompt("Passphrase again: ")
    status = session.import_legacy(first, second, backup_dir)
    _print_status(status, output)
    if status.mode == MODE_EDITABLE and load_local_config().cloud_mode:
        return 0
    return 1


def _create_empty(session: FinanceSession, prompt, output: TextIO) -> int:
    first = prompt("Passphrase: ")
    second = prompt("Passphrase again: ")
    status = session.create_empty(first, second)
    _print_status(status, output)
    if status.mode == MODE_EDITABLE and load_local_config().cloud_mode:
        return 0
    return 1


def _open(session: FinanceSession, prompt, output: TextIO) -> int:
    passphrase = prompt("Passphrase: ")
    status = session.open_with_passphrase(passphrase)
    _print_status(status, output)
    attempts = 0
    while CHOICE_RETRY_PASSPHRASE in status.choices and status.mode == MODE_SETUP and attempts < 2:
        attempts += 1
        passphrase = prompt("Passphrase: ")
        status = session.open_with_passphrase(passphrase)
        _print_status(status, output)
    if status.mode in _OPENED:
        return 0
    return 1


def _restore_folder_id(override: str | None, previous: str | None) -> None:
    if not override or override == previous:
        return
    try:
        config = load_local_config()
    except ConfigError:
        return
    if config.cloud_mode or config.drive_folder_id == previous:
        return
    config.drive_folder_id = previous
    save_local_config(config)


def _legacy_for(parsed) -> Path:
    if parsed.command == "migrate":
        source = Path(parsed.source)
        _reject_real_finance_file(source)
        return source
    if parsed.legacy:
        legacy = Path(parsed.legacy)
        _reject_real_finance_file(legacy)
        return legacy
    return app_config_dir() / "not-the-real-finance-file.json"


def _print_status(status: SessionStatus, output: TextIO) -> None:
    print(f"Mode: {status.mode}", file=output)
    print(f"Editing: {'yes' if status.can_edit else 'no'}", file=output)
    if status.generation is not None:
        print(f"Generation: {status.generation}", file=output)
    if status.data_time:
        print(f"Data time: {status.data_time}", file=output)
    if status.dirty:
        print("Unsaved changes are still in memory.", file=output)
    print(status.message, file=output)
    if status.legacy_notice:
        print(status.legacy_notice, file=output)
    if status.choices:
        print("Choices: " + ", ".join(status.choices), file=output)
    if status.backup_path:
        print(f"Plaintext backup: {status.backup_path}", file=output)
        print(
            "That backup is still plaintext. Keep it outside the shared Drive folder.",
            file=output,
        )
    if status.mode == MODE_ACCESS_LOST:
        print("Editing is disabled.", file=output)
    if status.mode == MODE_CLOSED:
        print("The session is closed.", file=output)


def _confirm_folder(prompt, output, name: str, display_name: str, email: str) -> None:
    print(f"Folder name: {name}", file=output)
    owner = display_name or email or "unknown"
    if display_name and email:
        owner = f"{display_name} <{email}>"
    print(f"Owner: {owner}", file=output)
    print("This check only uses that folder. Type the folder name exactly to continue.", file=output)
    typed = prompt("Folder name: ").strip()
    if typed != name:
        raise CheckCancelled


def _reject_unsafe_profile() -> None:
    raw = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if not raw:
        raise SessionCheckError(
            "Set FINANCETRACKER_CONFIG_DIR to a dry-run profile before this check. "
            "The production profile is refused."
        )
    directory = app_config_dir()
    project = default_data_path().resolve().parent
    if directory == project or project in directory.parents or directory in project.parents:
        raise SessionCheckError(
            "FINANCETRACKER_CONFIG_DIR points at the FinanceTracker source directory. "
            "Choose a folder outside the project."
        )
    if keyring_service_name() == PRODUCTION_KEYRING_SERVICE:
        raise SessionCheckError("Refusing to run with the production Google credential.")


def _reject_real_finance_file(path: Path) -> None:
    if path.expanduser().resolve() == default_data_path().resolve():
        raise SessionCheckError(
            "Refusing to use the finance file next to the program. "
            "Pass a copy that contains only synthetic records."
        )


def _open_live(folder_id: str) -> GoogleDriveAdapter:
    if not is_drive_item_id(folder_id):
        raise SessionCheckError("The Drive folder id cannot be used.")
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
        if question.startswith("Passphrase"):
            return getpass.getpass(question)
        return input(question)
    except EOFError:
        raise CheckCancelled from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m finance.session_check",
        description=(
            "Cloud session check for a dry-run profile. "
            "Does not read the finance file next to the program."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--owner", choices=USERS, default=USER_BORIS)
    common.add_argument(
        "--folder-id",
        default=None,
        help="Use this Drive folder for this run only. It is not saved.",
    )
    common.add_argument(
        "--legacy",
        default=None,
        help="Optional copy of a finance file to notice. The real file is refused.",
    )
    commands.add_parser("open", parents=[common], help="Open the newest snapshot.")
    migrate = commands.add_parser(
        "migrate",
        parents=[common],
        help="Import a copy of a finance file into an empty test folder.",
    )
    migrate.add_argument("--source", required=True, help="A copy, not the real finance file.")
    migrate.add_argument(
        "--backup-dir",
        required=True,
        help="Folder for the plaintext backup. Not the shared Drive folder.",
    )
    commands.add_parser(
        "create-empty",
        parents=[common],
        help="Create an empty encrypted dataset in the test folder.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
