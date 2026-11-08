"""Open the cloud session, then show the main window.

Until a Drive folder is saved on this computer, ``main.py`` keeps using the
local finance file. After a folder is saved, this module is the only startup
path and it does not write that file.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication

from finance.drive_adapter import DriveAdapter, DriveError, DriveUnavailable, GoogleDriveAdapter
from finance.drive_folder import is_drive_item_id
from finance.google_auth import (
    AuthorizationError,
    CredentialStoreError,
    GoogleAuthorization,
    NeedsInteractiveSignIn,
    OAuthClientError,
    default_secret_store,
    load_desktop_client,
)
from finance.lock import TIMER_POLL_SECONDS
from finance.profile import ConfigError, LocalConfig, load_local_config, oauth_client_file
from finance.session import MODE_CLOSED, FinanceSession, SessionError, SessionStatus

from .cloud_host import CloudHost
from .cloud_open import continue_open
from .cloud_prompts import QtCloudPrompts
from .cloud_status import safe_text
from .main_window import MainWindow
from .session_runner import QtJobRunner

_START_ERRORS = (
    AuthorizationError,
    CredentialStoreError,
    NeedsInteractiveSignIn,
    OAuthClientError,
    ConfigError,
    DriveError,
    SessionError,
    OSError,
)


def cloud_session_required(config: LocalConfig) -> bool:
    """True when the window must use the cloud session instead of the local file."""
    return bool(config.cloud_mode or config.drive_folder_id)


def cloud_start_block(config: LocalConfig) -> str | None:
    """Why startup must stop before opening a dataset. None when it can continue."""
    if config.cloud_mode and not config.drive_folder_id:
        return (
            "Cloud mode is on, but no Drive folder is saved on this computer. "
            "The local finance file was not opened."
        )
    if config.drive_folder_id and not is_drive_item_id(config.drive_folder_id):
        return (
            "The saved Drive folder id cannot be used. "
            "The local finance file was not opened."
        )
    return None


class OfflineDrive:
    """Stand-in used when Google sign-in cannot be refreshed. Every call fails the same way."""

    def close(self) -> None:
        return None

    def __getattr__(self, name: str):
        if name == "close":
            return self.close

        def unavailable(*_args, **_kwargs):
            raise DriveUnavailable(
                "Google Drive could not be reached. Check the network and try again."
            )

        return unavailable


class _QuitFilter(QObject):
    """Turn Quit into a window close so Cmd+Q and the app menu can ask about unsaved changes."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self._window = window

    def eventFilter(self, watched, event) -> bool:
        del watched
        if event.type() == QEvent.Quit and not self._window.accepts_quit():
            self._window.close()
            return True
        return False


def run_cloud(app: QApplication, config: LocalConfig | None = None) -> int:
    """Sign in, open the dataset, and run the window. Returns the Qt exit code."""
    prompts = QtCloudPrompts()
    try:
        loaded = config if config is not None else load_local_config()
    except ConfigError as exc:
        prompts.warn("Finance Tracker", str(exc))
        return 1
    blocked = cloud_start_block(loaded)
    if blocked is not None:
        prompts.warn("Finance Tracker", blocked)
        return 1
    folder_id = loaded.drive_folder_id
    if not folder_id:
        prompts.warn(
            "Finance Tracker",
            "No Drive folder is saved on this computer. The local finance file was not opened.",
        )
        return 1

    notice = prompts.working("Signing in to Google. A browser window may open.")
    offline_only = False
    drive: DriveAdapter
    try:
        try:
            drive = connect_drive(folder_id)
        except AuthorizationError as exc:
            prompts.close_working(notice)
            notice = None
            if not prompts.confirm(
                "Google Drive",
                str(exc) + "\n\nOpen the saved encrypted copy on this computer? It is read-only.",
            ):
                return 1
            drive = OfflineDrive()
            offline_only = True
        except _START_ERRORS as exc:
            prompts.close_working(notice)
            notice = None
            prompts.warn("Finance Tracker", safe_text(exc))
            return 1
    finally:
        prompts.close_working(notice)

    if not offline_only and not loaded.cloud_mode:
        try:
            folder = drive.describe_folder()
        except DriveUnavailable:
            folder = None
        except DriveError as exc:
            prompts.warn("Finance Tracker", safe_text(exc))
            drive.close()
            return 1
        if folder is not None:
            owner = _owner_text(folder.owner_display_name, folder.owner_email)
            summary = (
                f"Folder: {folder.name}\nOwner: {owner}\n\n"
                "Encrypted snapshots will be saved only in this folder."
            )
            if not prompts.confirm("Use this Drive folder?", summary):
                drive.close()
                return 0

    unlocked = prompts.unlock()
    if unlocked is None:
        drive.close()
        return 0
    owner_label, passphrase = unlocked

    def session_factory(adapter: DriveAdapter) -> FinanceSession:
        return FinanceSession(
            adapter,
            folder_id=folder_id,
            owner_label=owner_label,
        )

    runner = QtJobRunner()
    session = session_factory(drive)

    def call(fn: Callable[[], object]) -> object:
        return _wait_for(app, runner, prompts, fn)

    try:
        status = call(lambda: session.open_with_passphrase(passphrase))
    except Exception as exc:
        prompts.warn("Finance Tracker", safe_text(exc))
        _stop(session, runner, drive)
        return 1
    finally:
        passphrase = ""
    if not isinstance(status, SessionStatus):
        prompts.warn(
            "Finance Tracker",
            "The dataset could not be opened. Nothing was reported as saved.",
        )
        _stop(session, runner, drive)
        return 1

    try:
        status = continue_open(session, prompts, status, call=call)
    except Exception as exc:
        prompts.warn("Finance Tracker", safe_text(exc))
        _stop(session, runner, drive)
        return 1
    if status is None or session.store is None:
        _stop(session, runner, drive)
        return 0

    host = CloudHost(
        session,
        runner,
        prompts,
        drive,
        offline_only=offline_only,
        connector=lambda: connect_drive(folder_id),
        session_factory=session_factory,
    )
    window = MainWindow(
        session.store,
        host=host,
        poll_ms=int(TIMER_POLL_SECONDS * 1000),
    )
    prompts.set_parent(window)
    window.show()
    app.setQuitOnLastWindowClosed(True)
    app.installEventFilter(_QuitFilter(window))
    app.aboutToQuit.connect(window.shutdown)
    commit = getattr(app, "commitDataRequest", None)
    if commit is not None:
        commit.connect(window.handle_commit_data)
    try:
        return app.exec()
    finally:
        window.shutdown()


def connect_drive(folder_id: str) -> GoogleDriveAdapter:
    """Build the Drive adapter for the saved folder. May open a browser."""
    if not is_drive_item_id(folder_id):
        raise SessionError("The saved Drive folder id cannot be used.")
    client = load_desktop_client(oauth_client_file())
    authorization = GoogleAuthorization(default_secret_store(), client)
    result = authorization.authorize(interactive=True)
    return GoogleDriveAdapter(folder_id, credentials=result.credentials)


def _wait_for(app: QApplication, runner: QtJobRunner, prompts: QtCloudPrompts, fn) -> object:
    """Run ``fn`` on the session thread and keep the Qt thread processing events."""
    notice = prompts.working("Contacting Google Drive…")
    holder: dict[str, object] = {}
    finished = threading.Event()

    def deliver(result: object) -> None:
        holder["result"] = result
        finished.set()

    try:
        runner.submit(fn, deliver)
        while not finished.is_set():
            app.processEvents()
            finished.wait(0.05)
        result = holder.get("result")
        if isinstance(result, Exception):
            raise result
        return result
    finally:
        prompts.close_working(notice)


def _owner_text(display_name: str, email: str) -> str:
    if display_name and email:
        return f"{display_name} <{email}>"
    return display_name or email or "unknown"


def _stop(session: FinanceSession, runner: QtJobRunner, drive: DriveAdapter) -> None:
    runner.stop()
    try:
        if session.status.mode != MODE_CLOSED:
            session.close(discard=True)
    except Exception:
        pass
    drive.close()
