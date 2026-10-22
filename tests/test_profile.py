"""Per-user config paths. The production profile and keyring slot are not used."""

from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path

import pytest
from platformdirs import user_config_dir
from platformdirs.macos import MacOS
from platformdirs.windows import Windows

from finance.profile import (
    APP_NAME,
    CONFIG_DIR_ENV,
    PRODUCTION_KEYRING_SERVICE,
    ConfigError,
    app_config_dir,
    config_file,
    default_machine_label,
    encrypted_snapshots_dir,
    ensure_machine_identity,
    ensure_profile_directories,
    keyring_service_name,
    known_valid_cache_path,
    load_local_config,
    process_lock_path,
    profile_paths,
    save_local_config,
    service_name_for_directory,
)
from tests.support import SYNTHETIC_SECRET, assert_failure_omits_payload

FOLDER_ID = "folder-id-0123456789abc"
LOCK_ID = "lock-file-id-0123456789"


def test_override_keeps_every_profile_path_and_the_keyring_name_together(
    isolated_app_profile: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = isolated_app_profile.resolve()
    production = Path(user_config_dir(APP_NAME, appauthor=False)).resolve()
    repo = Path(__file__).resolve().parents[1]

    for path in profile_paths().values():
        resolved = path.resolve()
        assert resolved == root or root in resolved.parents
        assert resolved != production and production not in resolved.parents
        assert repo not in resolved.parents
        assert resolved != repo

    service = keyring_service_name()
    assert service != PRODUCTION_KEYRING_SERVICE
    assert re.fullmatch(r"FinanceTracker-profile-[0-9a-f]{16}", service)
    assert str(root).lower() not in service.lower()

    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "other-profile"))
    assert keyring_service_name() != service
    assert keyring_service_name() != PRODUCTION_KEYRING_SERVICE


def test_default_config_dir_is_the_platform_user_dir_not_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    expected = Path(user_config_dir(APP_NAME, appauthor=False))
    directory = app_config_dir()
    repo = Path(__file__).resolve().parents[1]

    assert directory == expected
    assert keyring_service_name() == PRODUCTION_KEYRING_SERVICE
    assert directory.resolve() != repo.resolve()
    assert repo.resolve() not in directory.resolve().parents
    assert directory.resolve() not in repo.resolve().parents


def test_windows_and_macos_directories_are_outside_the_repository() -> None:
    repo = Path(__file__).resolve().parents[1]
    windows = Path(Windows(appname=APP_NAME, appauthor=False).user_config_dir)
    macos_text = MacOS(appname=APP_NAME, appauthor=False).user_config_dir.replace("\\", "/")

    assert windows.parts[-3:] == ("AppData", "Local", APP_NAME)
    assert macos_text.endswith("Library/Application Support/FinanceTracker")
    for path in (windows, Path(macos_text)):
        resolved = path.resolve()
        assert resolved != repo.resolve()
        assert repo.resolve() not in resolved.parents
        assert resolved not in repo.resolve().parents


def test_blank_override_uses_the_production_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_DIR_ENV, "   ")
    assert keyring_service_name() == PRODUCTION_KEYRING_SERVICE
    assert app_config_dir() == Path(user_config_dir(APP_NAME, appauthor=False))


def test_relative_override_is_resolved_from_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_DIR_ENV, "relative-profile")
    assert app_config_dir() == (tmp_path / "relative-profile").resolve()
    assert app_config_dir().is_absolute()


def test_service_name_hides_the_directory() -> None:
    left = service_name_for_directory(Path(r"C:\Users\Example\Profile"))
    right = service_name_for_directory(Path(r"c:\users\example\profile"))
    assert re.fullmatch(r"FinanceTracker-profile-[0-9a-f]{16}", left)
    assert "Example" not in left
    assert "Users" not in left
    if os.name == "nt":
        assert left == right
    else:
        assert left != right
    assert service_name_for_directory(Path(r"C:\one")) != service_name_for_directory(
        Path(r"C:\two")
    )


def test_machine_id_is_created_once_and_is_not_the_host_name() -> None:
    first = ensure_machine_identity()
    second = ensure_machine_identity()
    text = config_file().read_text(encoding="utf-8")

    assert first.machine_id == second.machine_id
    assert len(first.machine_id) == 64
    assert first.machine_label == default_machine_label()
    assert first.cloud_mode is False
    assert list(config_file().parent.glob("*.tmp")) == []
    hostname = platform.node()
    if hostname:
        assert hostname not in text
    assert encrypted_snapshots_dir().is_dir()
    assert list(encrypted_snapshots_dir().iterdir()) == []
    assert not process_lock_path().exists()
    assert not known_valid_cache_path().exists()


def test_each_profile_gets_its_own_machine_id(
    isolated_app_profile: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = ensure_machine_identity().machine_id
    other = tmp_path / "second-profile"
    other.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(other))
    second = ensure_machine_identity().machine_id
    assert first != second
    monkeypatch.setenv(CONFIG_DIR_ENV, str(isolated_app_profile))
    assert load_local_config().machine_id == first


def test_missing_config_file_is_not_created() -> None:
    loaded = load_local_config()
    assert loaded.machine_id == ""
    assert loaded.drive_folder_id is None
    assert loaded.lock_file_id is None
    assert loaded.cloud_mode is False
    assert not config_file().exists()


def test_folder_and_lock_ids_round_trip() -> None:
    config = ensure_machine_identity()
    config.drive_folder_id = FOLDER_ID
    config.lock_file_id = LOCK_ID
    save_local_config(config)

    loaded = load_local_config()
    assert loaded.drive_folder_id == FOLDER_ID
    assert loaded.lock_file_id == LOCK_ID
    assert loaded.machine_id == config.machine_id
    assert loaded.cloud_mode is False
    assert '"cloud_mode": false' in config_file().read_text(encoding="utf-8")


def test_save_refuses_to_enable_cloud_mode() -> None:
    config = ensure_machine_identity()
    config.cloud_mode = True
    with pytest.raises(ConfigError, match="Cloud mode cannot be turned on"):
        save_local_config(config)
    assert load_local_config().cloud_mode is False


def test_darwin_label_is_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("finance.profile.platform.system", lambda: "Darwin")
    assert default_machine_label() == "macOS"


def test_malformed_config_does_not_echo_the_file() -> None:
    ensure_profile_directories()
    config_file().write_text(
        '{"format_version": 1, "machine_id": "' + SYNTHETIC_SECRET + '"}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="machine id") as caught:
        load_local_config()
    assert_failure_omits_payload(caught.value)


def test_unknown_format_version_does_not_echo_extra_fields() -> None:
    ensure_profile_directories()
    config_file().write_text(
        json.dumps({"format_version": 99, "note": SYNTHETIC_SECRET}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="format version 99") as caught:
        load_local_config()
    assert_failure_omits_payload(caught.value)


def test_invalid_json_does_not_echo_the_file() -> None:
    ensure_profile_directories()
    config_file().write_text("{" + SYNTHETIC_SECRET, encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON") as caught:
        load_local_config()
    assert_failure_omits_payload(caught.value)
