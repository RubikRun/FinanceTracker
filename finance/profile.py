"""Per-user configuration paths and the non-secret local config file.

The finance JSON stays beside the program until cloud mode is turned on by a
later step. This module is where the app keeps everything else that is local
to one computer:

- ``config.json`` — machine id, Drive folder id, lock-file id, cloud-mode flag
- the desktop OAuth client file the user copies in
- the local encrypted-snapshot directory
- the process-lock path
- the lock-session record written while this computer holds the Drive lock
- the known-valid snapshot cache

None of those paths sit next to the source code unless
``FINANCETRACKER_CONFIG_DIR`` is pointed there on purpose. When that variable
is unset, the directory comes from ``platformdirs`` (Windows:
``%LOCALAPPDATA%\\FinanceTracker``, macOS:
``~/Library/Application Support/FinanceTracker``).

When the variable is set, every path above is inside that directory, and the
keyring service name is ``FinanceTracker-profile-`` plus a hash of the
directory. Dry-runs and tests then cannot write the production credential
slot. The hash is used so the service name does not contain the path, which
may include a user name.

``cloud_mode`` stays false. A later step turns it on only after a verified
generation-1 snapshot and the permanent lock file both exist. This module
refuses to save a config that already has it set.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

from .drive_folder import is_drive_item_id

APP_NAME = "FinanceTracker"
CONFIG_DIR_ENV = "FINANCETRACKER_CONFIG_DIR"
PRODUCTION_KEYRING_SERVICE = "FinanceTracker"
PROFILE_KEYRING_PREFIX = "FinanceTracker-profile-"

CONFIG_FORMAT_VERSION = 1
CONFIG_FILENAME = "config.json"
OAUTH_CLIENT_FILENAME = "google_oauth_client.json"
ENCRYPTED_SNAPSHOTS_DIRNAME = "encrypted_snapshots"
PROCESS_LOCK_FILENAME = "process.lock"
LOCK_SESSION_FILENAME = "lock_session.json"
KNOWN_VALID_CACHE_FILENAME = "known_valid.json"


class ConfigError(Exception):
    """The local config file cannot be read or saved. The message is safe to show."""


@dataclass
class LocalConfig:
    """Non-secret settings for one computer. No passphrase and no finance records."""

    format_version: int = CONFIG_FORMAT_VERSION
    machine_id: str = ""
    machine_label: str = ""
    drive_folder_id: str | None = None
    lock_file_id: str | None = None
    cloud_mode: bool = False


def app_config_dir() -> Path:
    """Directory for this computer's FinanceTracker config.

    An empty ``FINANCETRACKER_CONFIG_DIR`` is treated as unset, so a blank
    value cannot drop files into the current working directory by accident.
    """
    override = _override_dir()
    if override is not None:
        return override
    return Path(user_config_dir(APP_NAME, appauthor=False))


def config_file() -> Path:
    return app_config_dir() / CONFIG_FILENAME


def oauth_client_file() -> Path:
    return app_config_dir() / OAUTH_CLIENT_FILENAME


def encrypted_snapshots_dir() -> Path:
    return app_config_dir() / ENCRYPTED_SNAPSHOTS_DIRNAME


def process_lock_path() -> Path:
    return app_config_dir() / PROCESS_LOCK_FILENAME


def lock_session_path() -> Path:
    """Local record of the Drive lock token while this process holds it.

    The file is not the lock. The lock is the Drive file. The record is removed
    when this process releases the Drive lock.
    """
    return app_config_dir() / LOCK_SESSION_FILENAME


def known_valid_cache_path() -> Path:
    return app_config_dir() / KNOWN_VALID_CACHE_FILENAME


def profile_paths() -> dict[str, Path]:
    """Every on-disk profile location, including files a later step will create."""
    return {
        "config_dir": app_config_dir(),
        "config_file": config_file(),
        "oauth_client_file": oauth_client_file(),
        "encrypted_snapshots_dir": encrypted_snapshots_dir(),
        "process_lock": process_lock_path(),
        "lock_session": lock_session_path(),
        "known_valid_cache": known_valid_cache_path(),
    }


def keyring_service_name() -> str:
    """Credential-store service name for the active profile.

    Production is exactly ``FinanceTracker``. An override gets a distinct
    name derived from the directory so it cannot share that slot.
    """
    override = _override_dir()
    if override is None:
        return PRODUCTION_KEYRING_SERVICE
    return service_name_for_directory(override)


def service_name_for_directory(directory: Path) -> str:
    text = str(directory)
    if os.name == "nt":
        text = os.path.normcase(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{PROFILE_KEYRING_PREFIX}{digest}"


def default_machine_label() -> str:
    """A short label for this computer. The host name is not included."""
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Windows":
        return "Windows"
    if system:
        return system
    return "computer"


def ensure_profile_directories() -> Path:
    """Create the config directory and the local encrypted-snapshot directory."""
    root = app_config_dir()
    root.mkdir(parents=True, exist_ok=True)
    encrypted_snapshots_dir().mkdir(parents=True, exist_ok=True)
    return root


def load_local_config() -> LocalConfig:
    """Load ``config.json``. A missing file yields an empty in-memory config.

    The missing file is not created here. Nothing is written.
    """
    path = config_file()
    if not path.exists():
        return LocalConfig()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ConfigError(f"Could not read the configuration file {path}.") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ConfigError("The configuration file is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ConfigError("The configuration file is not a JSON object.")
    return _config_from_mapping(data)


def save_local_config(config: LocalConfig) -> None:
    """Write ``config.json`` atomically. Refuses to turn cloud mode on."""
    if config.cloud_mode:
        raise ConfigError(
            "Cloud mode cannot be turned on here. It stays off until a verified "
            "generation-1 snapshot and the permanent lock file both exist."
        )
    if config.format_version != CONFIG_FORMAT_VERSION:
        raise ConfigError("Refusing to save a configuration file of an unknown format.")
    if not _valid_machine_id(config.machine_id):
        raise ConfigError("Refusing to save a configuration file without a machine id.")
    label = config.machine_label.strip()
    if not label or len(label) > 80 or _has_control_character(label):
        raise ConfigError("Refusing to save a configuration file with this machine label.")
    folder_id = _checked_optional_id(config.drive_folder_id, "drive folder id")
    lock_id = _checked_optional_id(config.lock_file_id, "lock file id")
    payload = {
        "format_version": CONFIG_FORMAT_VERSION,
        "machine_id": config.machine_id,
        "machine_label": label,
        "drive_folder_id": folder_id,
        "lock_file_id": lock_id,
        "cloud_mode": False,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    ensure_profile_directories()
    path = config_file()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    config.machine_label = label
    config.drive_folder_id = folder_id
    config.lock_file_id = lock_id
    config.cloud_mode = False


def ensure_machine_identity(config: LocalConfig | None = None) -> LocalConfig:
    """Return a config with a stable machine id, creating one on first use.

    The id is random and is stored in ``config.json``. It is not the host name.
    Calling this twice returns the same id.
    """
    current = load_local_config() if config is None else config
    changed = False
    if not current.machine_id:
        current.machine_id = secrets.token_hex(32)
        changed = True
    if not current.machine_label.strip():
        current.machine_label = default_machine_label()
        changed = True
    if changed:
        save_local_config(current)
    return current


def _override_dir() -> Path | None:
    raw = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _config_from_mapping(data: dict) -> LocalConfig:
    version = data.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError(
            "This configuration file does not have a supported format version."
        )
    if version != CONFIG_FORMAT_VERSION:
        raise ConfigError(
            f"This configuration file uses format version {version}, "
            "which this version of FinanceTracker does not read."
        )
    machine_id = data.get("machine_id", "")
    if machine_id in ("", None):
        machine_id = ""
    elif not _valid_machine_id(machine_id):
        raise ConfigError(
            "The configuration file has a machine id this app does not accept."
        )
    label = data.get("machine_label", "")
    if label is None:
        label = ""
    if not isinstance(label, str) or len(label) > 80 or _has_control_character(label):
        raise ConfigError(
            "The configuration file has a machine label this app does not accept."
        )
    flag = data.get("cloud_mode", False)
    if not isinstance(flag, bool):
        raise ConfigError(
            "The configuration file has a cloud mode flag this app does not accept."
        )
    return LocalConfig(
        format_version=CONFIG_FORMAT_VERSION,
        machine_id=machine_id,
        machine_label=label,
        drive_folder_id=_optional_id(data, "drive_folder_id"),
        lock_file_id=_optional_id(data, "lock_file_id"),
        cloud_mode=flag,
    )


def _optional_id(data: dict, key: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not is_drive_item_id(value):
        label = key.replace("_", " ")
        raise ConfigError(
            f"The configuration file has a {label} this app does not accept."
        )
    return value


def _checked_optional_id(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not is_drive_item_id(value):
        raise ConfigError(f"Refusing to save this {label}.")
    return value


def _valid_machine_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) < 32 or len(value) > 128:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 for character in value)
