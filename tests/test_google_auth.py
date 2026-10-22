"""Google sign-in with a fake credential store. No browser and no network."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from keyring.errors import KeyringError, PasswordDeleteError

from finance.google_auth import (
    DRIVE_SCOPE,
    REFRESH_TOKEN_USERNAME,
    WINDOWS_CREDENTIAL_BLOB_BYTES,
    AuthorizationError,
    CredentialStoreError,
    GoogleAuthorization,
    KeyringSecretStore,
    NeedsInteractiveSignIn,
    OAuthClientError,
    browser_sign_in_flow,
    credential_blob_size,
    credentials_from_refresh_token,
    default_refresh,
    load_desktop_client,
)
from finance.profile import PRODUCTION_KEYRING_SERVICE, oauth_client_file
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

CLIENT_SECRET = "test-client-secret"
CLIENT = {
    "installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "project_id": "test-project",
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}
TOKEN = "1//synthetic-refresh-token"
ROTATED = "1//synthetic-rotated-refresh-token"
SERVICE = "FinanceTracker-profile-abc"


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_secret(self, username: str) -> str | None:
        return self.values.get(username)

    def set_secret(self, username: str, secret: str) -> None:
        self.values[username] = secret

    def delete_secret(self, username: str) -> None:
        self.values.pop(username, None)


class FakeKeyring:
    def __init__(self, error: BaseException | None = None) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.error = error
        self.set_calls: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        if self.error is not None and not isinstance(self.error, PasswordDeleteError):
            raise self.error
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_calls.append((service, username, password))
        if self.error is not None:
            raise self.error
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.values:
            raise PasswordDeleteError(service)
        if self.error is not None:
            raise self.error
        del self.values[(service, username)]


def _grant(credentials: Credentials, refresh_token: str | None = None) -> None:
    credentials.token = "ya29.synthetic-access"
    credentials.expiry = datetime(2099, 1, 1)
    if refresh_token is not None:
        # google.oauth2.credentials.Credentials.refresh writes this attribute
        # when Google rotates the refresh token. The public property is read-only.
        credentials._refresh_token = refresh_token


def _authorization(
    store: MemorySecretStore,
    refresher,
    browser,
) -> GoogleAuthorization:
    return GoogleAuthorization(
        store,
        CLIENT,
        refresher=refresher,
        browser_sign_in=browser,
    )


def test_stored_refresh_token_is_refreshed_without_opening_a_browser() -> None:
    store = MemorySecretStore()
    store.values[REFRESH_TOKEN_USERNAME] = TOKEN
    seen: list[Credentials] = []

    def refresh(credentials: Credentials) -> None:
        seen.append(credentials)
        assert credentials.token is None
        assert credentials.refresh_token == TOKEN
        assert list(credentials.scopes) == [DRIVE_SCOPE]
        _grant(credentials)

    def browser(config: dict) -> str:
        raise AssertionError("browser should not open")

    result = _authorization(store, refresh, browser).authorize(interactive=True)

    assert result.used_browser is False
    assert result.replaced_rejected_sign_in is False
    assert result.credentials.valid
    assert result.credentials.token == "ya29.synthetic-access"
    assert store.values[REFRESH_TOKEN_USERNAME] == TOKEN
    assert CLIENT_SECRET not in store.values[REFRESH_TOKEN_USERNAME]
    assert len(seen) == 1


def test_missing_credentials_open_the_browser_and_store_only_the_refresh_token() -> None:
    store = MemorySecretStore()
    calls: list[dict] = []

    def refresh(credentials: Credentials) -> None:
        assert credentials.refresh_token == TOKEN
        _grant(credentials)

    def browser(config: dict) -> str:
        calls.append(config)
        return TOKEN

    result = _authorization(store, refresh, browser).authorize(interactive=True)

    assert result.used_browser is True
    assert calls == [CLIENT]
    assert store.values[REFRESH_TOKEN_USERNAME] == TOKEN
    assert not store.values[REFRESH_TOKEN_USERNAME].startswith("{")


def test_missing_credentials_without_interaction_ask_for_sign_in() -> None:
    store = MemorySecretStore()

    def browser(config: dict) -> str:
        raise AssertionError("browser should not open")

    with pytest.raises(NeedsInteractiveSignIn, match="No Google sign-in") as caught:
        _authorization(store, _grant, browser).authorize(interactive=False)
    assert_failure_omits_payload(caught.value, TOKEN)
    assert store.values == {}


def test_revoked_refresh_token_returns_to_interactive_sign_in() -> None:
    store = MemorySecretStore()
    store.values[REFRESH_TOKEN_USERNAME] = TOKEN
    calls = {"refresh": 0}

    def refresh(credentials: Credentials) -> None:
        calls["refresh"] += 1
        if calls["refresh"] == 1:
            raise RefreshError(TOKEN)
        assert credentials.refresh_token == ROTATED
        _grant(credentials)

    def browser(config: dict) -> str:
        return ROTATED

    result = _authorization(store, refresh, browser).authorize(interactive=True)

    assert result.used_browser is True
    assert result.replaced_rejected_sign_in is True
    assert store.values[REFRESH_TOKEN_USERNAME] == ROTATED
    assert calls["refresh"] == 2


def test_revoked_refresh_token_without_interaction_has_a_clear_message() -> None:
    store = MemorySecretStore()
    store.values[REFRESH_TOKEN_USERNAME] = TOKEN

    def refresh(credentials: Credentials) -> None:
        raise RefreshError(TOKEN)

    def browser(config: dict) -> str:
        raise AssertionError("browser should not open")

    with pytest.raises(NeedsInteractiveSignIn, match="revoked or expired") as caught:
        _authorization(store, refresh, browser).authorize(interactive=False)
    assert_failure_omits_payload(caught.value, TOKEN)
    assert REFRESH_TOKEN_USERNAME not in store.values


def test_network_failure_keeps_the_saved_refresh_token() -> None:
    store = MemorySecretStore()
    store.values[REFRESH_TOKEN_USERNAME] = TOKEN

    def refresh(credentials: Credentials) -> None:
        raise ConnectionError(TOKEN)

    def browser(config: dict) -> str:
        raise AssertionError("browser should not open")

    with pytest.raises(AuthorizationError, match="left in place") as caught:
        _authorization(store, refresh, browser).authorize(interactive=True)
    assert_failure_omits_payload(caught.value, TOKEN)
    assert store.values[REFRESH_TOKEN_USERNAME] == TOKEN


def test_rotated_refresh_token_replaces_the_stored_one() -> None:
    store = MemorySecretStore()
    store.values[REFRESH_TOKEN_USERNAME] = TOKEN

    def refresh(credentials: Credentials) -> None:
        _grant(credentials, ROTATED)

    result = _authorization(store, refresh, lambda config: TOKEN).authorize(interactive=True)
    assert result.used_browser is False
    assert store.values[REFRESH_TOKEN_USERNAME] == ROTATED
    assert credential_blob_size(ROTATED) <= WINDOWS_CREDENTIAL_BLOB_BYTES


def test_a_cleared_refresh_token_stays_the_one_already_stored() -> None:
    store = MemorySecretStore()
    store.values[REFRESH_TOKEN_USERNAME] = TOKEN

    def refresh(credentials: Credentials) -> None:
        credentials.token = "ya29.synthetic-access"
        credentials.expiry = datetime(2099, 1, 1)
        credentials._refresh_token = None

    result = _authorization(store, refresh, lambda config: TOKEN).authorize(interactive=True)
    assert result.used_browser is False
    assert store.values[REFRESH_TOKEN_USERNAME] == TOKEN
    assert result.credentials.refresh_token == TOKEN


def test_browser_that_returns_no_refresh_token_stores_nothing() -> None:
    store = MemorySecretStore()

    def browser(config: dict) -> str:
        return ""

    with pytest.raises(NeedsInteractiveSignIn, match="refresh token"):
        _authorization(store, _grant, browser).authorize(interactive=True)
    assert store.values == {}


def test_unavailable_secure_storage_fails_without_a_token_file(
    isolated_app_profile: Path,
) -> None:
    backend = FakeKeyring(error=OSError(TOKEN))
    store = KeyringSecretStore(SERVICE, backend=backend)

    with pytest.raises(CredentialStoreError, match="was not saved") as caught:
        store.set_secret(REFRESH_TOKEN_USERNAME, TOKEN)

    assert "token file" in str(caught.value)
    assert "OSError" in str(caught.value)
    assert_failure_omits_payload(caught.value, TOKEN)
    assert backend.values == {}
    assert list(isolated_app_profile.rglob("token.json")) == []
    for path in isolated_app_profile.rglob("*"):
        if path.is_file():
            assert TOKEN.encode("utf-8") not in path.read_bytes()


def test_keyring_error_is_also_a_storage_failure() -> None:
    backend = FakeKeyring(error=KeyringError(TOKEN))
    store = KeyringSecretStore(SERVICE, backend=backend)
    with pytest.raises(CredentialStoreError, match="KeyringError") as caught:
        store.set_secret(REFRESH_TOKEN_USERNAME, TOKEN)
    assert_failure_omits_payload(caught.value, TOKEN)
    assert "token file" in str(caught.value)


def test_refresh_token_written_to_the_store_fits_the_windows_limit() -> None:
    backend = FakeKeyring()
    store = KeyringSecretStore(SERVICE, backend=backend)
    fitting = "a" * 1280
    oversized = "b" * 1281
    full_document = json.dumps(
        {
            "token": "ya29." + ("x" * 2000),
            "refresh_token": TOKEN,
            "client_id": CLIENT["installed"]["client_id"],
            "client_secret": CLIENT_SECRET,
            "scopes": [DRIVE_SCOPE],
        }
    )

    assert credential_blob_size(TOKEN) <= WINDOWS_CREDENTIAL_BLOB_BYTES
    assert credential_blob_size(fitting) == WINDOWS_CREDENTIAL_BLOB_BYTES
    assert credential_blob_size(oversized) > WINDOWS_CREDENTIAL_BLOB_BYTES
    assert credential_blob_size(full_document) > WINDOWS_CREDENTIAL_BLOB_BYTES

    store.set_secret(REFRESH_TOKEN_USERNAME, TOKEN)
    store.set_secret(REFRESH_TOKEN_USERNAME, fitting)
    assert backend.values[(SERVICE, REFRESH_TOKEN_USERNAME)] == fitting
    assert backend.set_calls[-1] == (SERVICE, REFRESH_TOKEN_USERNAME, fitting)

    with pytest.raises(CredentialStoreError, match="2560"):
        store.set_secret(REFRESH_TOKEN_USERNAME, oversized)
    with pytest.raises(CredentialStoreError, match="was not saved"):
        store.set_secret(REFRESH_TOKEN_USERNAME, full_document)
    assert backend.values[(SERVICE, REFRESH_TOKEN_USERNAME)] == fitting
    assert CLIENT_SECRET not in fitting
    assert not fitting.startswith("{")


def test_empty_and_multiline_secrets_are_refused() -> None:
    store = KeyringSecretStore(SERVICE, backend=FakeKeyring())
    with pytest.raises(CredentialStoreError, match="empty"):
        store.set_secret(REFRESH_TOKEN_USERNAME, "")
    with pytest.raises(CredentialStoreError, match="control characters"):
        store.set_secret(REFRESH_TOKEN_USERNAME, TOKEN + "\n")


def test_delete_of_a_missing_secret_is_fine() -> None:
    store = KeyringSecretStore(SERVICE, backend=FakeKeyring())
    store.delete_secret(REFRESH_TOKEN_USERNAME)


def test_production_slot_is_refused_while_the_profile_override_is_set() -> None:
    backend = FakeKeyring()
    store = KeyringSecretStore(PRODUCTION_KEYRING_SERVICE, backend=backend)
    with pytest.raises(CredentialStoreError, match="FINANCETRACKER_CONFIG_DIR"):
        store.set_secret(REFRESH_TOKEN_USERNAME, TOKEN)
    assert backend.set_calls == []


def test_tests_cannot_construct_the_real_credential_store() -> None:
    with pytest.raises(CredentialStoreError, match="Tests cannot"):
        KeyringSecretStore(SERVICE)


def test_production_service_name_is_used_only_when_the_override_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FINANCETRACKER_CONFIG_DIR", raising=False)
    backend = FakeKeyring()
    store = KeyringSecretStore(PRODUCTION_KEYRING_SERVICE, backend=backend)
    store.set_secret(REFRESH_TOKEN_USERNAME, TOKEN)
    assert backend.values[(PRODUCTION_KEYRING_SERVICE, REFRESH_TOKEN_USERNAME)] == TOKEN


def test_browser_sign_in_requests_only_the_full_drive_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeFlow:
        def run_local_server(self, **kwargs: object) -> SimpleNamespace:
            captured["kwargs"] = kwargs
            return SimpleNamespace(refresh_token=TOKEN)

    def from_client_config(config: dict, scopes: list[str]) -> FakeFlow:
        captured["config"] = config
        captured["scopes"] = list(scopes)
        return FakeFlow()

    monkeypatch.setattr(InstalledAppFlow, "from_client_config", from_client_config)
    assert browser_sign_in_flow(CLIENT) == TOKEN
    assert captured["scopes"] == [DRIVE_SCOPE]
    assert "drive.file" not in "".join(captured["scopes"])
    assert captured["kwargs"]["prompt"] == "consent"
    assert captured["kwargs"]["access_type"] == "offline"
    assert captured["kwargs"]["port"] == 0
    assert captured["config"] is CLIENT


def test_browser_sign_in_without_a_refresh_token_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFlow:
        def run_local_server(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(refresh_token=None)

    monkeypatch.setattr(
        InstalledAppFlow,
        "from_client_config",
        lambda config, scopes: FakeFlow(),
    )
    with pytest.raises(NeedsInteractiveSignIn, match="refresh token"):
        browser_sign_in_flow(CLIENT)


def test_default_refresh_calls_google_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def refresh(self: Credentials, request: object) -> None:
        seen.append(request)
        self.token = "ya29.synthetic-access"
        self.expiry = datetime(2099, 1, 1)

    monkeypatch.setattr(Credentials, "refresh", refresh)
    credentials = credentials_from_refresh_token(CLIENT, TOKEN)
    default_refresh(credentials)
    assert type(seen[0]).__name__ == "Request"
    assert credentials.valid
    assert credentials.has_scopes([DRIVE_SCOPE])


def test_missing_client_file_names_the_destination_and_not_a_secret(
    isolated_app_profile: Path,
) -> None:
    with pytest.raises(OAuthClientError, match="not on this computer yet") as caught:
        load_desktop_client(oauth_client_file())
    assert str(oauth_client_file()) in str(caught.value)
    assert "same file" in str(caught.value)
    assert_failure_omits_payload(caught.value)


def test_web_client_is_rejected(isolated_app_profile: Path) -> None:
    oauth_client_file().write_text(
        json.dumps({"web": {"client_id": "web-client", "client_secret": CLIENT_SECRET}}),
        encoding="utf-8",
    )
    with pytest.raises(OAuthClientError, match="Desktop app") as caught:
        load_desktop_client(oauth_client_file())
    assert_failure_omits_payload(caught.value, CLIENT_SECRET)


def test_invalid_client_json_does_not_echo_the_file(isolated_app_profile: Path) -> None:
    oauth_client_file().write_text("{" + SYNTHETIC_SECRET, encoding="utf-8")
    with pytest.raises(OAuthClientError, match="not valid JSON") as caught:
        load_desktop_client(oauth_client_file())
    assert_failure_omits_payload(caught.value)


def test_finance_package_does_not_name_a_token_file() -> None:
    root = Path(__file__).resolve().parents[1] / "finance"
    for path in root.glob("*.py"):
        assert "token.json" not in path.read_text(encoding="utf-8")
