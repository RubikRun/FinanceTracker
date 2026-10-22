"""Setup command with fake Google sign-in and a fake folder lookup. No network."""

from __future__ import annotations

import json
import re
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from finance import default_data_path
from finance.drive_folder import FOLDER_MIME, SHORTCUT_MIME
from finance.google_auth import (
    REFRESH_TOKEN_USERNAME,
    GoogleAuthorization,
    KeyringSecretStore,
)
from finance.google_setup import (
    FOLDER_LOOKUP_FIELDS,
    REQUEST_TIMEOUT_SECONDS,
    build_drive_service,
    fetch_drive_item,
    run_setup,
)
from finance.profile import (
    config_file,
    encrypted_snapshots_dir,
    ensure_machine_identity,
    load_local_config,
    oauth_client_file,
    save_local_config,
)
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

CLIENT_SECRET = "test-client-secret"
TOKEN = "1//synthetic-refresh-token"
NEW_TOKEN = "1//synthetic-replacement-refresh-token"
FOLDER_ID = "folder-id-0123456789abc"
SHORTCUT_ID = "shortcut-id-0123456789ab"
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


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_secret(self, username: str) -> str | None:
        return self.values.get(username)

    def set_secret(self, username: str, secret: str) -> None:
        self.values[username] = secret

    def delete_secret(self, username: str) -> None:
        self.values.pop(username, None)


class ScriptedPrompt:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str) -> str:
        self.questions.append(question)
        if not self.answers:
            raise AssertionError(question)
        return self.answers.pop(0)


def _write_client() -> None:
    oauth_client_file().write_text(json.dumps(CLIENT), encoding="utf-8")


def _grant(credentials: Credentials) -> None:
    credentials.token = "ya29.synthetic-access"
    credentials.expiry = datetime(2099, 1, 1)


def _folder(file_id: str = FOLDER_ID) -> dict:
    return {
        "id": file_id,
        "name": "Shared Test Folder",
        "mimeType": FOLDER_MIME,
        "trashed": False,
        "owners": [{"displayName": "Test Owner", "emailAddress": "owner@example.com"}],
    }


def _shortcut_reader() -> tuple[list[str], object]:
    seen: list[str] = []

    def read(file_id: str) -> dict:
        seen.append(file_id)
        if file_id == SHORTCUT_ID:
            return {
                "id": SHORTCUT_ID,
                "name": "link",
                "mimeType": SHORTCUT_MIME,
                "trashed": False,
                "shortcutDetails": {"targetId": FOLDER_ID},
            }
        return _folder(FOLDER_ID)

    return seen, read


def _assert_profile_has_no_secret(directory: Path, secret: str) -> None:
    assert list(directory.rglob("token.json")) == []
    assert list(directory.rglob("credentials.json")) == []
    for path in directory.rglob("*"):
        if path.is_file():
            assert secret.encode("utf-8") not in path.read_bytes()


def test_setup_saves_the_shortcut_target_only_after_confirmation(
    isolated_app_profile: Path,
) -> None:
    _write_client()
    legacy = default_data_path()
    before = legacy.stat().st_mtime_ns if legacy.exists() else None
    output = StringIO()
    prompt = ScriptedPrompt(
        [f"https://drive.google.com/drive/folders/{SHORTCUT_ID}?usp=sharing", "y"]
    )
    seen, reader = _shortcut_reader()
    store = MemorySecretStore()

    def ask(question: str) -> str:
        if "Save this folder" in question:
            text = output.getvalue()
            assert "Shared Test Folder" in text
            assert "owner@example.com" in text
            assert "Test Owner" in text
            assert "The link was a shortcut." in text
        return prompt(question)

    code = run_setup(
        prompt=ask,
        output=output,
        secret_store=store,
        refresher=_grant,
        browser_sign_in=lambda config: TOKEN,
        read_metadata=reader,
    )
    text = output.getvalue()
    config = load_local_config()

    assert code == 0
    assert seen == [SHORTCUT_ID, FOLDER_ID]
    assert config.drive_folder_id == FOLDER_ID
    assert config.lock_file_id is None
    assert config.cloud_mode is False
    assert config.machine_id
    assert store.values[REFRESH_TOKEN_USERNAME] == TOKEN
    assert "Google sign-in finished." in text
    assert "No finance data was uploaded." in text
    assert "No token file was written." in text
    assert "Cloud sync is not on." in text
    assert "In production" in text
    assert "7 days" in text
    assert re.search(r"FinanceTracker-profile-[0-9a-f]{16}", text)
    assert list(encrypted_snapshots_dir().iterdir()) == []
    _assert_profile_has_no_secret(isolated_app_profile, TOKEN)
    assert CLIENT_SECRET not in config_file().read_text(encoding="utf-8")
    after = legacy.stat().st_mtime_ns if legacy.exists() else None
    assert before == after


def test_declining_the_folder_does_not_save_it(isolated_app_profile: Path) -> None:
    _write_client()
    output = StringIO()
    store = MemorySecretStore()
    prompt = ScriptedPrompt([FOLDER_ID, "n"])

    code = run_setup(
        prompt=prompt,
        output=output,
        secret_store=store,
        refresher=_grant,
        browser_sign_in=lambda config: TOKEN,
        read_metadata=lambda file_id: _folder(file_id),
    )

    assert code == 2
    assert load_local_config().drive_folder_id is None
    assert load_local_config().machine_id
    assert store.values[REFRESH_TOKEN_USERNAME] == TOKEN
    assert "The folder was not saved." in output.getvalue()
    assert "Setup cancelled." not in output.getvalue()
    _assert_profile_has_no_secret(isolated_app_profile, TOKEN)


def test_second_run_reuses_the_saved_sign_in_and_folder() -> None:
    _write_client()
    config = ensure_machine_identity()
    config.drive_folder_id = FOLDER_ID
    save_local_config(config)
    store = MemorySecretStore()
    store.values[REFRESH_TOKEN_USERNAME] = TOKEN
    output = StringIO()
    prompt = ScriptedPrompt(["", ""])

    def browser(config_dict: dict) -> str:
        raise AssertionError("browser should not open")

    code = run_setup(
        prompt=prompt,
        output=output,
        secret_store=store,
        refresher=_grant,
        browser_sign_in=browser,
        read_metadata=lambda file_id: _folder(file_id),
    )

    assert code == 0
    assert "Reused the saved Google sign-in. No browser window was opened." in output.getvalue()
    assert "Keep this folder?" in prompt.questions[-1]
    assert load_local_config().drive_folder_id == FOLDER_ID
    assert load_local_config().cloud_mode is False


def test_rejected_sign_in_is_replaced_in_the_browser() -> None:
    _write_client()
    store = MemorySecretStore()
    store.values[REFRESH_TOKEN_USERNAME] = TOKEN
    calls = {"n": 0}

    def refresh(credentials: Credentials) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            from google.auth.exceptions import RefreshError

            raise RefreshError("invalid_grant")
        _grant(credentials)

    output = StringIO()
    code = run_setup(
        prompt=ScriptedPrompt([FOLDER_ID, "y"]),
        output=output,
        secret_store=store,
        refresher=refresh,
        browser_sign_in=lambda config: NEW_TOKEN,
        read_metadata=lambda file_id: _folder(file_id),
    )

    assert code == 0
    assert "was rejected" in output.getvalue()
    assert store.values[REFRESH_TOKEN_USERNAME] == NEW_TOKEN
    assert calls["n"] == 2


def test_missing_client_file_stops_before_sign_in(isolated_app_profile: Path) -> None:
    output = StringIO()
    called = {"browser": 0}

    def browser(config: dict) -> str:
        called["browser"] += 1
        return TOKEN

    code = run_setup(
        prompt=ScriptedPrompt([]),
        output=output,
        secret_store=MemorySecretStore(),
        browser_sign_in=browser,
        read_metadata=lambda file_id: _folder(file_id),
    )

    assert code == 1
    assert called["browser"] == 0
    assert "not on this computer yet" in output.getvalue()
    assert str(oauth_client_file()) in output.getvalue()
    assert not config_file().exists()
    assert encrypted_snapshots_dir().is_dir()


def test_web_client_is_rejected_before_sign_in() -> None:
    oauth_client_file().write_text(json.dumps({"web": {"client_id": "web"}}), encoding="utf-8")
    output = StringIO()
    code = run_setup(
        prompt=ScriptedPrompt([]),
        output=output,
        secret_store=MemorySecretStore(),
        browser_sign_in=lambda config: TOKEN,
        read_metadata=lambda file_id: _folder(),
    )
    assert code == 1
    assert "Desktop app" in output.getvalue()
    assert not config_file().exists()


def test_storage_failure_is_reported_and_writes_no_token_file(
    isolated_app_profile: Path,
) -> None:
    _write_client()

    class Boom:
        def get_password(self, service: str, username: str) -> None:
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            raise OSError(password)

        def delete_password(self, service: str, username: str) -> None:
            return None

    output = StringIO()
    code = run_setup(
        prompt=ScriptedPrompt([]),
        output=output,
        secret_store=KeyringSecretStore("FinanceTracker-profile-abc", backend=Boom()),
        refresher=_grant,
        browser_sign_in=lambda config: TOKEN,
        read_metadata=lambda file_id: _folder(),
    )

    assert code == 1
    assert "was not saved" in output.getvalue()
    assert "token file" in output.getvalue()
    assert TOKEN not in output.getvalue()
    assert load_local_config().drive_folder_id is None
    _assert_profile_has_no_secret(isolated_app_profile, TOKEN)


def test_cloud_mode_already_on_is_left_alone() -> None:
    _write_client()
    config = ensure_machine_identity()
    text = config_file().read_text(encoding="utf-8").replace(
        '"cloud_mode": false',
        '"cloud_mode": true',
    )
    config_file().write_text(text, encoding="utf-8")
    output = StringIO()

    def browser(config_dict: dict) -> str:
        raise AssertionError("browser should not open")

    code = run_setup(
        prompt=ScriptedPrompt([]),
        output=output,
        secret_store=MemorySecretStore(),
        browser_sign_in=browser,
        read_metadata=lambda file_id: _folder(),
    )

    assert code == 1
    assert "Cloud mode is already turned on" in output.getvalue()
    assert '"cloud_mode": true' in config_file().read_text(encoding="utf-8")
    assert load_local_config().drive_folder_id is None


def test_unusable_answers_cancel(isolated_app_profile: Path) -> None:
    _write_client()
    output = StringIO()
    code = run_setup(
        prompt=ScriptedPrompt([FOLDER_ID, "maybe", "maybe", "maybe"]),
        output=output,
        secret_store=MemorySecretStore(),
        refresher=_grant,
        browser_sign_in=lambda config: TOKEN,
        read_metadata=lambda file_id: _folder(),
    )
    assert code == 2
    assert "Please answer y or n." in output.getvalue()
    assert "Setup cancelled." in output.getvalue()
    assert load_local_config().drive_folder_id is None


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "status"


class _Files:
    def __init__(self, payload: object = None, error: BaseException | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []
        self.num_retries: int | None = None

    def get(self, **kwargs: object) -> "_Files":
        self.calls.append(kwargs)
        return self

    def delete(self, **kwargs: object) -> None:
        raise AssertionError("permanent delete is not used")

    def update(self, **kwargs: object) -> None:
        raise AssertionError("update is not used")

    def execute(self, num_retries: int | None = None) -> object:
        self.num_retries = num_retries
        if self.error is not None:
            raise self.error
        return self.payload


class _Service:
    def __init__(self, files: _Files) -> None:
        self._files = files

    def files(self) -> _Files:
        return self._files


def test_folder_lookup_is_a_get_by_id_with_shortcut_fields() -> None:
    files = _Files(payload=_folder())
    resource = fetch_drive_item(_Service(files), FOLDER_ID)
    assert resource["id"] == FOLDER_ID
    assert files.calls == [
        {
            "fileId": FOLDER_ID,
            "fields": FOLDER_LOOKUP_FIELDS,
            "supportsAllDrives": True,
        }
    ]
    assert "shortcutDetails" in FOLDER_LOOKUP_FIELDS
    assert files.num_retries == 0


def test_folder_lookup_hides_drive_error_bodies() -> None:
    body = json.dumps({"error": {"message": SYNTHETIC_SECRET}}).encode("utf-8")
    files = _Files(error=HttpError(_Response(404), body))
    with pytest.raises(Exception) as caught:
        fetch_drive_item(_Service(files), FOLDER_ID)
    assert "not found" in str(caught.value)
    assert_failure_omits_payload(caught.value)

    files = _Files(error=HttpError(_Response(403), body))
    with pytest.raises(Exception, match="Editor") as denied:
        fetch_drive_item(_Service(files), FOLDER_ID)
    assert_failure_omits_payload(denied.value)

    files = _Files(error=TimeoutError(SYNTHETIC_SECRET))
    with pytest.raises(Exception, match=str(REQUEST_TIMEOUT_SECONDS)) as timed_out:
        fetch_drive_item(_Service(files), FOLDER_ID)
    assert_failure_omits_payload(timed_out.value)


def test_drive_service_uses_a_thirty_second_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_http(timeout: int | None = None) -> str:
        captured["timeout"] = timeout
        return "http"

    def fake_authorized(credentials: object, http: object = None) -> str:
        captured["http"] = http
        captured["credentials"] = credentials
        return "authed"

    def fake_build(api: str, version: str, http: object = None, cache_discovery: bool | None = None) -> str:
        captured["api"] = api
        captured["version"] = version
        captured["authed"] = http
        captured["cache_discovery"] = cache_discovery
        return "service"

    monkeypatch.setattr("finance.google_setup.httplib2.Http", fake_http)
    monkeypatch.setattr(
        "finance.google_setup.google_auth_httplib2.AuthorizedHttp",
        fake_authorized,
    )
    monkeypatch.setattr("finance.google_setup.build", fake_build)
    credentials = object()
    assert build_drive_service(credentials) == "service"  # type: ignore[arg-type]
    assert captured["timeout"] == REQUEST_TIMEOUT_SECONDS
    assert captured["http"] == "http"
    assert captured["credentials"] is credentials
    assert captured["api"] == "drive"
    assert captured["version"] == "v3"
    assert captured["authed"] == "authed"
    assert captured["cache_discovery"] is False


def test_setup_uses_the_injected_authorizer_only_after_the_client_file_loads() -> None:
    """A provided authorizer does not skip the desktop-client check."""
    output = StringIO()

    class Unused(GoogleAuthorization):
        def authorize(self, *, interactive: bool):
            raise AssertionError("should not run")

    code = run_setup(prompt=ScriptedPrompt([]), output=output, authorizer=Unused)  # type: ignore[arg-type]
    assert code == 1
    assert "not on this computer yet" in output.getvalue()
