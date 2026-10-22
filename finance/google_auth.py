"""Google sign-in for one computer, with the refresh token kept in the OS keyring.

The desktop client id and secret stay in the client JSON file in the profile
directory. The only value written to the credential store is the refresh
token. A full credentials document is not stored: Windows Credential Manager
accepts 2560 bytes, and keyring 25 writes that blob as UTF-16, so the practical
limit is 1280 BMP characters. A serialized credentials object can pass that
limit and can also contain the client secret.

There is no plaintext token file. If the credential store raises — including
errors that are not ``KeyringError``, which is what Windows can do — the
sign-in fails with a fixed message and nothing is written beside it.

The scope is ``https://www.googleapis.com/auth/drive``. ``drive.file`` cannot
see files the other account's copy of this app creates inside the shared
folder. The README section "Google sign-in setup" explains that choice. The
finance passphrase is never stored here.

Tests pass a fake credential backend. While ``PYTEST_CURRENT_TEST`` is set,
constructing the real backend is refused. While ``FINANCETRACKER_CONFIG_DIR``
is set, the production service name ``FinanceTracker`` is refused.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from keyring.errors import PasswordDeleteError

from .profile import CONFIG_DIR_ENV, PRODUCTION_KEYRING_SERVICE

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
REFRESH_TOKEN_USERNAME = "google-drive-refresh-token"

# keyring's Windows backend stores CredentialBlob with create_unicode_buffer
# and drops the terminating wide null. The bytes that count are the UTF-16-LE
# encoding of the secret. 1280 BMP characters are exactly 2560 bytes, which
# is Credential Manager's CRED_MAX_CREDENTIAL_BLOB_SIZE.
WINDOWS_CREDENTIAL_BLOB_BYTES = 2560

_MISSING_SIGN_IN = (
    "No Google sign-in is stored on this computer. A browser window is required to sign in."
)
_REJECTED_SIGN_IN = (
    "Google rejected the saved sign-in. It may have been revoked or expired. "
    "Sign in again in the browser."
)
_REFRESH_UNAVAILABLE = (
    "The saved Google sign-in could not be refreshed. Check the network and try again. "
    "The saved sign-in was left in place."
)
_NO_ACCESS_TOKEN = (
    "Google did not return a usable sign-in. Try again when the network is available."
)
_NO_REFRESH_TOKEN = (
    "Google did not return a refresh token, so nothing was saved. "
    "Remove FinanceTracker's access under Google Account permissions and sign in again."
)
_STORAGE_UNAVAILABLE = (
    "Secure credential storage is unavailable, so the Google sign-in was not saved. "
    "FinanceTracker does not write a token file."
)
_TOKEN_TOO_LARGE = (
    "The Google refresh token does not fit in Windows Credential Manager "
    f"({WINDOWS_CREDENTIAL_BLOB_BYTES} bytes). It was not saved. "
    "Only the refresh token is stored, never a full credentials file."
)
_EMPTY_TOKEN = "Refusing to store an empty Google refresh token."
_CONTROL_CHARACTERS = "Refusing to store a refresh token that contains control characters."
_TESTS_CANNOT_USE_REAL_STORE = (
    "Tests cannot use the real operating-system credential store."
)
_PRODUCTION_SLOT_BLOCKED = (
    "Refusing to use the production Google credential while FINANCETRACKER_CONFIG_DIR is set."
)


class CredentialStoreError(Exception):
    """The OS credential store could not hold the refresh token. Nothing was written to a file."""


class NeedsInteractiveSignIn(Exception):
    """The user has to sign in in the browser. The message is safe to show."""


class OAuthClientError(Exception):
    """The desktop client JSON is missing or is not a desktop client."""


class AuthorizationError(Exception):
    """Sign-in could not be refreshed, and the saved token was left as it was."""


class SecretStore(Protocol):
    def get_secret(self, username: str) -> str | None:
        """Return the stored secret, or None when this username has none."""

    def set_secret(self, username: str, secret: str) -> None:
        """Store ``secret`` under ``username``."""

    def delete_secret(self, username: str) -> None:
        """Remove ``username``. Missing entries are not an error."""


@dataclass(frozen=True)
class AuthorizationResult:
    """Usable Drive credentials and how they were obtained."""

    credentials: Credentials
    used_browser: bool
    replaced_rejected_sign_in: bool


class KeyringSecretStore:
    """Refresh-token store backed by the operating-system credential store.

    ``backend`` is the ``keyring`` module, or a fake with the same
    ``get_password`` / ``set_password`` / ``delete_password`` methods. Tests
    pass a fake. The real backend is refused while pytest is running.
    """

    def __init__(self, service_name: str, backend: Any = None) -> None:
        self.service_name = service_name
        if backend is None:
            if os.environ.get("PYTEST_CURRENT_TEST"):
                raise CredentialStoreError(_TESTS_CANNOT_USE_REAL_STORE)
            import keyring

            backend = keyring
        self._backend = backend

    def get_secret(self, username: str) -> str | None:
        self._reject_production_slot_during_override()
        try:
            value = self._backend.get_password(self.service_name, username)
        except Exception as exc:
            raise CredentialStoreError(_storage_message(exc)) from None
        if value is None:
            return None
        if not isinstance(value, str):
            raise CredentialStoreError(_STORAGE_UNAVAILABLE)
        return value

    def set_secret(self, username: str, secret: str) -> None:
        self._reject_production_slot_during_override()
        ensure_refresh_token_fits(secret)
        try:
            self._backend.set_password(self.service_name, username, secret)
        except Exception as exc:
            raise CredentialStoreError(_storage_message(exc, secret)) from None

    def delete_secret(self, username: str) -> None:
        self._reject_production_slot_during_override()
        try:
            self._backend.delete_password(self.service_name, username)
        except PasswordDeleteError:
            return
        except Exception as exc:
            raise CredentialStoreError(_storage_message(exc)) from None

    def _reject_production_slot_during_override(self) -> None:
        if self.service_name != PRODUCTION_KEYRING_SERVICE:
            return
        if os.environ.get(CONFIG_DIR_ENV, "").strip():
            raise CredentialStoreError(_PRODUCTION_SLOT_BLOCKED)


def _storage_message(exc: BaseException, secret: str = "") -> str:
    """Fixed text plus the exception type. The backend message is not included.

    ``secret`` is accepted so callers can show they had it, and so a backend
    message that echoes the refresh token cannot be appended later by mistake.
    """
    del secret
    return f"{_STORAGE_UNAVAILABLE} ({type(exc).__name__})"


class GoogleAuthorization:
    """Load a saved refresh token, refresh it, or open the browser to get one."""

    def __init__(
        self,
        store: SecretStore,
        client_config: dict,
        refresher: Callable[[Credentials], None] | None = None,
        browser_sign_in: Callable[[dict], str] | None = None,
    ) -> None:
        self._store = store
        self._client_config = client_config
        self._refresh = refresher or default_refresh
        self._browser_sign_in = browser_sign_in or browser_sign_in_flow

    def authorize(self, *, interactive: bool) -> AuthorizationResult:
        stored = self._store.get_secret(REFRESH_TOKEN_USERNAME)
        if not stored:
            if not interactive:
                raise NeedsInteractiveSignIn(_MISSING_SIGN_IN)
            token = self._sign_in_and_store()
            return AuthorizationResult(
                self._refresh_credentials(token),
                used_browser=True,
                replaced_rejected_sign_in=False,
            )
        try:
            credentials = self._refresh_credentials(stored)
        except RefreshError:
            self._store.delete_secret(REFRESH_TOKEN_USERNAME)
            if not interactive:
                raise NeedsInteractiveSignIn(_REJECTED_SIGN_IN) from None
            token = self._sign_in_and_store()
            try:
                credentials = self._refresh_credentials(token)
            except RefreshError:
                self._store.delete_secret(REFRESH_TOKEN_USERNAME)
                raise NeedsInteractiveSignIn(_REJECTED_SIGN_IN) from None
            return AuthorizationResult(
                credentials,
                used_browser=True,
                replaced_rejected_sign_in=True,
            )
        return AuthorizationResult(
            credentials,
            used_browser=False,
            replaced_rejected_sign_in=False,
        )

    def _sign_in_and_store(self) -> str:
        token = self._browser_sign_in(self._client_config)
        if not isinstance(token, str) or not token:
            raise NeedsInteractiveSignIn(_NO_REFRESH_TOKEN)
        self._store.set_secret(REFRESH_TOKEN_USERNAME, token)
        return token

    def _refresh_credentials(self, refresh_token: str) -> Credentials:
        credentials = credentials_from_refresh_token(self._client_config, refresh_token)
        try:
            self._refresh(credentials)
        except RefreshError:
            raise
        except Exception:
            raise AuthorizationError(_REFRESH_UNAVAILABLE) from None
        if not credentials.valid:
            raise AuthorizationError(_NO_ACCESS_TOKEN)
        rotated = credentials.refresh_token
        if isinstance(rotated, str) and rotated and rotated != refresh_token:
            self._store.set_secret(REFRESH_TOKEN_USERNAME, rotated)
        if not credentials.refresh_token:
            # The public property is read-only. Keep the token we already stored
            # on the object so a later refresh in this session still has it.
            credentials._refresh_token = refresh_token
        return credentials


def default_secret_store() -> KeyringSecretStore:
    """The real credential store for the active profile."""
    from .profile import keyring_service_name

    return KeyringSecretStore(keyring_service_name())


def credential_blob_size(secret: str) -> int:
    """Bytes Credential Manager would store for ``secret``, excluding the terminator."""
    return len(secret.encode("utf-16-le"))


def ensure_refresh_token_fits(secret: str) -> None:
    """Reject a secret the Windows credential store cannot hold.

    Called before ``set_password``. A secret that fails this check is not
    handed to keyring and is not written to a file.
    """
    if not isinstance(secret, str) or secret == "":
        raise CredentialStoreError(_EMPTY_TOKEN)
    if any(ord(character) < 32 for character in secret):
        raise CredentialStoreError(_CONTROL_CHARACTERS)
    if credential_blob_size(secret) > WINDOWS_CREDENTIAL_BLOB_BYTES:
        raise CredentialStoreError(_TOKEN_TOO_LARGE)


def load_desktop_client(path: Path | str) -> dict:
    """Read and check a Google desktop client JSON file.

    Returns the whole file mapping, which is what
    ``InstalledAppFlow.from_client_config`` expects. A web client is rejected.
    The client secret is not copied into the message.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise OAuthClientError(
            "The desktop OAuth client file is not on this computer yet.\n\n"
            f"Copy the same client JSON downloaded from Google Cloud to:\n{file_path}\n\n"
            "Both computers use this same file. Do not create a second OAuth client.\n"
            'The steps are in the README section "Google sign-in setup".'
        )
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        raise OAuthClientError(
            f"Could not read the OAuth client file {file_path}."
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise OAuthClientError(
            f"{file_path.name} is not valid JSON. Download the desktop client file again."
        ) from None
    if not isinstance(data, dict):
        raise OAuthClientError(
            f"{file_path.name} is not a desktop OAuth client file."
        )
    if "installed" not in data:
        if "web" in data:
            raise OAuthClientError(
                "This OAuth file is a web client. Create a Desktop app credential "
                "and copy that JSON file instead."
            )
        raise OAuthClientError(
            'This OAuth file has no desktop client (expected an "installed" section).'
        )
    installed = data["installed"]
    if not isinstance(installed, dict):
        raise OAuthClientError(
            f"{file_path.name} is not a desktop OAuth client file."
        )
    for key in ("client_id", "client_secret", "token_uri", "auth_uri"):
        value = installed.get(key)
        if not isinstance(value, str) or not value:
            raise OAuthClientError(
                f"The desktop client file is missing {key}. Download it from Google Cloud again."
            )
    return data


def credentials_from_refresh_token(client_config: dict, refresh_token: str) -> Credentials:
    """Build credentials that still need ``refresh`` before Drive will accept them."""
    installed = client_config["installed"]
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=installed["token_uri"],
        client_id=installed["client_id"],
        client_secret=installed["client_secret"],
        scopes=[DRIVE_SCOPE],
    )


def default_refresh(credentials: Credentials) -> None:
    """Refresh an access token. Raises ``RefreshError`` when Google rejects the refresh token."""
    credentials.refresh(Request())


def browser_sign_in_flow(client_config: dict) -> str:
    """Open the browser for a desktop-app consent and return only the refresh token.

    ``prompt=consent`` and offline access are what make Google issue a refresh
    token for an already-approved client. The access token is discarded.
    """
    flow = InstalledAppFlow.from_client_config(client_config, scopes=[DRIVE_SCOPE])
    credentials = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message=(
            "If the browser does not open, paste this address into it:\n{url}"
        ),
        success_message="FinanceTracker sign-in is complete. You can close this window.",
    )
    token = credentials.refresh_token
    if not isinstance(token, str) or not token:
        raise NeedsInteractiveSignIn(_NO_REFRESH_TOKEN)
    return token
