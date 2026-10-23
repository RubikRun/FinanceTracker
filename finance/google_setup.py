"""First-run Google sign-in and shared-folder confirmation.

Run ``python -m finance.google_setup``. This asks for the desktop OAuth client
file, opens a browser when no refresh token is stored, and saves a Drive
folder id only after showing that folder's name and owner.

It does not upload finance data, does not ask for the finance passphrase, and
does not turn cloud mode on. The FinanceTracker window still reads and writes
the local JSON file. A later step turns cloud mode on only after a verified
generation-1 snapshot and the permanent lock file both exist.

The Drive call here is only ``files.get`` for the pasted folder (or for a
shortcut's target). The request timeout is 30 seconds. Listing, download,
upload, trash, and the lock file are in ``finance.drive_adapter``.
``python -m finance.drive_check`` is the manual shared-folder check. The
FinanceTracker window still does not call either of them.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from typing import TextIO

import google_auth_httplib2
import httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .drive_folder import (
    DriveFolder,
    FolderSetupError,
    normalize_pasted_reference,
    resolve_folder,
)
from .google_auth import (
    AuthorizationError,
    CredentialStoreError,
    GoogleAuthorization,
    NeedsInteractiveSignIn,
    OAuthClientError,
    SecretStore,
    default_secret_store,
    load_desktop_client,
)
from .profile import (
    ConfigError,
    config_file,
    ensure_machine_identity,
    ensure_profile_directories,
    keyring_service_name,
    load_local_config,
    oauth_client_file,
    save_local_config,
)

REQUEST_TIMEOUT_SECONDS = 30
FOLDER_LOOKUP_FIELDS = (
    "id,name,mimeType,trashed,"
    "owners(displayName,emailAddress),"
    "shortcutDetails(targetId)"
)

_SETUP_ERRORS = (
    ConfigError,
    CredentialStoreError,
    NeedsInteractiveSignIn,
    OAuthClientError,
    AuthorizationError,
    FolderSetupError,
)


class SetupCancelled(Exception):
    """The user stopped setup. Not an error in the credential store or Drive."""


def build_drive_service(credentials: Credentials):
    """A Drive v3 client whose HTTP calls give up after 30 seconds.

    Discovery is not downloaded and is not cached into the working directory.
    """
    http = httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS)
    authorized = google_auth_httplib2.AuthorizedHttp(credentials, http=http)
    return build("drive", "v3", http=authorized, cache_discovery=False)


def fetch_drive_item(service, file_id: str) -> dict:
    """``files.get`` one item by id. Does not list, update, trash, or delete."""
    try:
        resource = (
            service.files()
            .get(
                fileId=file_id,
                fields=FOLDER_LOOKUP_FIELDS,
                supportsAllDrives=True,
            )
            .execute(num_retries=0)
        )
    except HttpError as exc:
        raise FolderSetupError(_http_message(exc)) from None
    except TimeoutError:
        raise FolderSetupError(
            f"Google Drive did not answer within {REQUEST_TIMEOUT_SECONDS} seconds. "
            "Check the network and try again."
        ) from None
    except Exception:
        raise FolderSetupError(
            "Could not read that Drive folder. Check the link, sharing, and network, then try again."
        ) from None
    if not isinstance(resource, dict):
        raise FolderSetupError("Drive did not return that folder.")
    return resource


def metadata_reader_for(credentials: Credentials) -> Callable[[str], dict]:
    service = build_drive_service(credentials)

    def read(file_id: str) -> dict:
        return fetch_drive_item(service, file_id)

    return read


def run_setup(
    *,
    prompt: Callable[[str], str] | None = None,
    output: TextIO | None = None,
    authorizer: GoogleAuthorization | None = None,
    secret_store: SecretStore | None = None,
    refresher: Callable[[Credentials], None] | None = None,
    browser_sign_in: Callable[[dict], str] | None = None,
    read_metadata: Callable[[str], Mapping[str, object]] | None = None,
) -> int:
    """Sign in and, after confirmation, save the shared folder id.

    Returns 0 when the folder id is saved, 2 when the user cancels, and 1
    when setup cannot continue. Cloud mode is not turned on.
    """
    write = output if output is not None else sys.stdout
    ask = prompt if prompt is not None else _console_prompt
    try:
        return _run_setup(
            prompt=ask,
            output=write,
            authorizer=authorizer,
            secret_store=secret_store,
            refresher=refresher,
            browser_sign_in=browser_sign_in,
            read_metadata=read_metadata,
        )
    except SetupCancelled:
        print("Setup cancelled.", file=write)
        return 2
    except _SETUP_ERRORS as exc:
        print(str(exc), file=write)
        return 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        return run_setup()
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        return 2


def _run_setup(
    *,
    prompt: Callable[[str], str],
    output: TextIO,
    authorizer: GoogleAuthorization | None,
    secret_store: SecretStore | None,
    refresher: Callable[[Credentials], None] | None,
    browser_sign_in: Callable[[dict], str] | None,
    read_metadata: Callable[[str], Mapping[str, object]] | None,
) -> int:
    ensure_profile_directories()
    client_config = load_desktop_client(oauth_client_file())
    existing = load_local_config()
    if existing.cloud_mode:
        raise ConfigError(
            "Cloud mode is already turned on for this profile. "
            "This setup command will not change it."
        )
    ensure_machine_identity(existing)
    if authorizer is None:
        store = secret_store if secret_store is not None else default_secret_store()
        authorizer = GoogleAuthorization(
            store,
            client_config,
            refresher=refresher,
            browser_sign_in=browser_sign_in,
        )
    print("Checking for a saved Google sign-in...", file=output)
    result = authorizer.authorize(interactive=True)
    if result.replaced_rejected_sign_in:
        print(
            "The saved Google sign-in was rejected. A browser window was opened to sign in again.",
            file=output,
        )
    elif result.used_browser:
        print(
            "Google sign-in finished. The refresh token is in the operating system credential store.",
            file=output,
        )
    else:
        print(
            "Reused the saved Google sign-in. No browser window was opened.",
            file=output,
        )
    reader = read_metadata if read_metadata is not None else metadata_reader_for(result.credentials)
    config = load_local_config()
    pasted = _ask_folder_text(prompt, output, config.drive_folder_id)
    folder = resolve_folder(pasted, reader)
    _print_folder(output, folder)
    already_saved = config.drive_folder_id == folder.folder_id
    question = (
        "Keep this folder?"
        if already_saved
        else "Save this folder on this computer?"
    )
    if not _ask_yes_no(prompt, output, question, default_yes=already_saved):
        print("The folder was not saved.", file=output)
        print("The Google sign-in remains in the credential store.", file=output)
        return 2
    config.drive_folder_id = folder.folder_id
    config.cloud_mode = False
    save_local_config(config)
    print("Saved the shared folder on this computer.", file=output)
    print(f"Configuration: {config_file()}", file=output)
    print(
        f"Credential store entry: {keyring_service_name()} (refresh token only).",
        file=output,
    )
    print("No token file was written.", file=output)
    print("No finance data was uploaded.", file=output)
    print(
        "Cloud sync is not on. The FinanceTracker window still uses the local finance file.",
        file=output,
    )
    print(
        "In Google Cloud, the OAuth user type must be External and the publishing "
        "status must be In production. If the app is still in Testing, this sign-in "
        "expires after 7 days. Run this command again after 7 days. It should reuse "
        "the saved sign-in without a new consent screen.",
        file=output,
    )
    return 0


def _ask_folder_text(
    prompt: Callable[[str], str],
    output: TextIO,
    current_folder_id: str | None,
) -> str:
    if current_folder_id:
        print(f"A folder ID is already saved: {current_folder_id}", file=output)
        print("Press Enter to keep it, or paste a new link or ID.", file=output)
    else:
        print("Paste the shared Google Drive folder link or folder ID.", file=output)
    answer = normalize_pasted_reference(prompt("Folder link or ID: "))
    if answer:
        return answer
    if current_folder_id:
        return current_folder_id
    raise SetupCancelled


def _ask_yes_no(
    prompt: Callable[[str], str],
    output: TextIO,
    question: str,
    *,
    default_yes: bool,
) -> bool:
    hint = " [Y/n] " if default_yes else " [y/N] "
    for _ in range(3):
        answer = prompt(question + hint).strip().lower()
        if answer == "":
            return default_yes
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.", file=output)
    raise SetupCancelled


def _print_folder(output: TextIO, folder: DriveFolder) -> None:
    if folder.followed_shortcut:
        print("The link was a shortcut. The folder below is the target.", file=output)
    print(f"Folder name: {folder.name}", file=output)
    print(f"Owner: {_format_owner(folder)}", file=output)
    print(f"Folder ID: {folder.folder_id}", file=output)


def _format_owner(folder: DriveFolder) -> str:
    if folder.owner_display_name and folder.owner_email:
        return f"{folder.owner_display_name} <{folder.owner_email}>"
    return folder.owner_display_name or folder.owner_email or "unknown"


def _http_message(exc: HttpError) -> str:
    status = _status_code(exc)
    if status == 404:
        return "That folder was not found, or this Google account cannot see it."
    if status == 403:
        return (
            "This Google account cannot open that folder. "
            "Share the folder with this account as Editor."
        )
    return (
        "Could not read that Drive folder. Check the link, sharing, and network, then try again."
    )


def _status_code(exc: HttpError) -> int | None:
    raw = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _console_prompt(question: str) -> str:
    try:
        return input(question)
    except EOFError:
        raise SetupCancelled from None


if __name__ == "__main__":
    sys.exit(main())
