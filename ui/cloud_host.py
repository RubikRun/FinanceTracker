"""Connects the window to one FinanceSession.

The window mutates the in-memory store, then asks this host to upload a
snapshot. Drive work runs on ``runner``. The Qt thread only sets an activity
flag; applying it waits for the poll job, so a Drive call cannot freeze the
window.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from finance.drive_adapter import DriveAdapter
from finance.session import (
    MODE_CLOSED,
    MODE_EDITABLE,
    MODE_OFFLINE,
    MODE_PAUSED,
    FinanceSession,
    SessionStatus,
)
from finance.snapshots import CHOICE_RELOAD_DISCARD, RecoveryCatalog

from .cloud_status import (
    ACTION_CREATE_LOCK,
    ACTION_DISCARD,
    ACTION_RECONNECT,
    ACTION_RECOVER,
    ACTION_RESUME,
    ACTION_RETRY,
    ACTION_TRY,
    CLOSE_CANCEL,
    CLOSE_DISCARD,
    CLOSE_RETRY,
    CLOSE_WAIT,
    CloudView,
    close_prompt,
    cloud_view,
    safe_text,
    unsaved_prompt,
)


class CloudHost:
    """One open cloud session and the jobs the window asks for."""

    def __init__(
        self,
        session: FinanceSession,
        runner,
        prompts,
        drive: DriveAdapter,
        *,
        offline_only: bool = False,
        connector: Callable[[], DriveAdapter] | None = None,
        session_factory: Callable[[DriveAdapter], FinanceSession] | None = None,
    ) -> None:
        self.session = session
        self.runner = runner
        self.prompts = prompts
        self._drive = drive
        self._offline_only = offline_only
        self._connector = connector
        self._session_factory = session_factory
        self.saving = False
        self._interaction = False
        self._direct = False
        self._shut = False
        self._close_after_save = False
        self._showed_legacy = False
        self._view: CloudView | None = None
        self._listener = None
        self._activity_lock = threading.Lock()
        self._activity_pending = False
        self.recovery_dialog = None

    def attach(self, listener) -> None:
        self._listener = listener
        self._present(
            self.session.status,
            refresh=True,
            announce_failure=False,
            announce_reload=True,
        )

    def note_activity(self) -> None:
        """Record a key or a click. The next poll applies it on the session thread."""
        if self._shut:
            return
        with self._activity_lock:
            self._activity_pending = True

    def poll(self) -> None:
        if (
            self._shut
            or self._direct
            or self.saving
            or self._interaction
            or self.runner.busy
        ):
            return
        self._run(self._poll_job, self._after_poll)

    def begin_mutation(self, proceed: Callable[[], None]) -> None:
        """Run ``proceed`` once editing is allowed. A paused session resumes first."""
        if self._shut or self.saving or self._interaction or self._direct:
            return

        def start(_status=None) -> None:
            if self._shut or self.saving:
                self._interaction = False
                return
            self._interaction = True
            mode = self.session.status.mode
            if mode == MODE_PAUSED:
                self._resume(then=proceed)
                return
            if not self.session.can_edit:
                self._interaction = False
                self._present(
                    self.session.status,
                    refresh=False,
                    announce_failure=False,
                    announce_reload=False,
                )
                return
            try:
                proceed()
            finally:
                if not self.saving:
                    self._interaction = False

        if self.runner.busy:
            self._interaction = True
            self._run(lambda: self.session.status, start)
            return
        start()

    def after_local_change(self) -> None:
        """The window already changed the in-memory store. Upload one snapshot."""
        if self._shut:
            return
        if self._listener is not None:
            self._listener.apply_view(
                cloud_view(self.session.status, saving=True),
                refresh_records=True,
            )
        self.saving = True
        self._interaction = True
        self._present(
            self.session.status,
            refresh=False,
            announce_failure=False,
            announce_reload=False,
        )
        self._run(self.session.save, self._after_save)

    def retry_save(self) -> None:
        if self._shut or self.saving or self._direct or not self.session.can_edit:
            return
        self.saving = True
        self._interaction = True
        self._present(
            self.session.status,
            refresh=False,
            announce_failure=False,
            announce_reload=False,
        )
        self._run(self.session.save, self._after_save)

    def primary_action(self) -> None:
        view = self._view
        if (
            view is None
            or view.action_id is None
            or self._shut
            or self.saving
            or self._interaction
            or self._direct
        ):
            return
        action = view.action_id
        if action == ACTION_RETRY:
            self.retry_save()
            return
        if action == ACTION_DISCARD:
            self._interaction = True
            self._run(self.session.reload_discard, self._after_status)
            return
        if action == ACTION_RESUME:
            self._interaction = True
            self._resume(then=None)
            return
        if action == ACTION_TRY:
            self._interaction = True
            self._run(self.session.retry_lock, self._after_status)
            return
        if action == ACTION_RECONNECT:
            self._reconnect()
            return
        if action == ACTION_RECOVER:
            if not self.prompts.confirm("Take the lock?", self.session.status.message):
                return
            self._interaction = True
            self._run(self.session.recover_lock, self._after_status)
            return
        if action == ACTION_CREATE_LOCK:
            if not self.prompts.confirm(
                "Create the lock file?",
                self.session.status.message,
            ):
                return
            self._interaction = True
            self._run(self.session.create_missing_lock, self._after_status)

    def open_backups(self) -> None:
        """Show Drive and local snapshots. Restore runs only after confirmation."""
        if (
            self._shut
            or self.saving
            or self._interaction
            or self._direct
            or self.runner.busy
        ):
            return
        self._interaction = True
        self._submit(
            self.session.list_backups,
            self._show_backups,
            lambda result: isinstance(result, RecoveryCatalog),
        )

    def request_close(self) -> None:
        """Ask when a save is running or the session is dirty. Otherwise release the lock."""
        if self._shut:
            return
        if self.session.status.mode == MODE_CLOSED:
            self._tell_quit()
            return
        prompt = close_prompt(
            self.session.status,
            saving=self.saving,
            busy=self.runner.busy,
            dirty=self.session.dirty,
        )
        if prompt is None:
            self._interaction = True
            self._run(lambda: self.session.close(), self._after_closed)
            return
        title, message, options = prompt
        choice = self.prompts.choose(title, message, options, warning=True)
        if choice == CLOSE_WAIT:
            self._close_after_save = True
            return
        if choice == CLOSE_RETRY:
            self._close_after_save = True
            self.retry_save()
            return
        if choice == CLOSE_DISCARD:
            self._interaction = True
            self._close_after_save = False
            self._run(lambda: self.session.close(discard=True), self._after_closed)
            return
        self._close_after_save = False
        self._tell_cancel()

    def close_synchronously(self) -> bool:
        """Finish a system-shutdown close before the caller returns.

        Used when the operating system is waiting. The session runner must be
        idle; a save that is already uploading is left to finish, and the lock
        stays.
        """
        if self.session.status.mode == MODE_CLOSED:
            return True
        if self.saving or self.runner.busy or self._direct:
            self.prompts.choose(
                "Saving",
                "A save is still uploading. This window stays open and keeps the lock.",
                ((CLOSE_CANCEL, "Keep the window open"),),
                warning=True,
            )
            return False
        self._direct = True
        try:
            if self.session.dirty:
                prompt = close_prompt(
                    self.session.status,
                    saving=False,
                    busy=False,
                    dirty=True,
                )
                message = self.session.status.message
                options: tuple[tuple[str, str], ...] = ()
                if prompt is not None:
                    _title, message, options = prompt
                choice = self.prompts.choose(
                    "Unsaved changes", message, options, warning=True
                )
                if choice == CLOSE_RETRY:
                    status = self.session.save()
                    self._present(
                        status,
                        refresh=True,
                        announce_failure=False,
                        announce_reload=False,
                    )
                    if self.session.dirty:
                        return False
                elif choice == CLOSE_DISCARD:
                    self.session.close(discard=True)
                    return self.session.status.mode == MODE_CLOSED
                else:
                    return False
            self.session.close()
            return self.session.status.mode == MODE_CLOSED
        except Exception as exc:
            self.prompts.warn("Finance Tracker", safe_text(exc))
            return self.session.status.mode == MODE_CLOSED
        finally:
            self._direct = False

    def release_if_idle(self) -> None:
        """Release the lock when nothing is unsaved and no job is running."""
        if self._shut or self.session.status.mode == MODE_CLOSED:
            return
        if self.saving or self.runner.busy or self._direct or self.session.dirty:
            return
        self._direct = True
        try:
            self.session.close()
        except Exception:
            return
        finally:
            self._direct = False

    def shutdown(self) -> None:
        """Stop jobs, then release a clean lock. A dirty session keeps the lock."""
        if self._shut:
            return
        self._shut = True
        stopped = self.runner.stop()
        if stopped:
            self._best_effort_release()
            drive = self._drive
            self._drive = None
            if drive is not None:
                drive.close()

    def _poll_job(self) -> SessionStatus:
        self._apply_activity()
        return self.session.poll()

    def _apply_activity(self) -> None:
        with self._activity_lock:
            pending = self._activity_pending
            self._activity_pending = False
        if pending:
            self.session.note_activity()

    def _resume(self, then: Callable[[], None] | None) -> None:
        def done(status: SessionStatus) -> None:
            self._present(
                status, refresh=True, announce_failure=False, announce_reload=True
            )
            if then is None:
                self._interaction = False
                self._maybe_continue_close()
                return
            if not status.can_edit:
                self._interaction = False
                self.prompts.warn("Read-only", status.message)
                self._maybe_continue_close()
                return
            try:
                then()
            finally:
                if not self.saving:
                    self._interaction = False

        self._run(self.session.resume, done)

    def _reconnect(self) -> None:
        phrase = self.prompts.passphrase(
            "Enter the finance passphrase. It is not saved on this computer."
        )
        if not phrase:
            return
        self._interaction = True
        if self._offline_only and self._connector is not None and self._session_factory is not None:
            self._run(lambda: self._open_fresh(phrase), self._after_status)
        else:
            self._run(lambda: self.session.open_with_passphrase(phrase), self._after_status)

    def _open_fresh(self, phrase: str) -> SessionStatus:
        drive = self._connector()
        assert self._session_factory is not None
        fresh = self._session_factory(drive)
        try:
            status = fresh.open_with_passphrase(phrase)
        except Exception:
            fresh.close(discard=True)
            drive.close()
            raise
        finally:
            phrase = ""
        if fresh.store is not None and status.mode != MODE_OFFLINE:
            self._swap(fresh, drive)
            self._offline_only = False
            return status
        fresh.close(discard=True)
        drive.close()
        return status

    def _swap(self, fresh: FinanceSession, drive: DriveAdapter) -> None:
        old_session = self.session
        old_drive = self._drive
        self.session = fresh
        self._drive = drive
        try:
            old_session.close(discard=True)
        finally:
            if old_drive is not None and old_drive is not drive:
                old_drive.close()

    def _after_save(self, status: SessionStatus) -> None:
        self.saving = False
        self._present(status, refresh=True, announce_failure=False, announce_reload=False)
        if self._close_after_save and not self.runner.busy:
            self._close_after_save = False
            self._interaction = False
            self.request_close()
            return
        if status.dirty:
            self._offer_unsaved(status)
            return
        if "conflicting save" in status.message.lower():
            self.prompts.warn("Conflicting save", status.message)
        self._interaction = False

    def _show_backups(self, catalog: RecoveryCatalog) -> None:
        try:
            if self._shut:
                return
            can_restore = (
                self.session.can_edit
                and not self.session.dirty
                and not self.saving
                and self.session.status.mode == MODE_EDITABLE
            )
            if self.session.dirty:
                blocked = "Save or discard unsaved changes before restoring."
            elif self.session.status.mode == MODE_PAUSED:
                blocked = "Resume editing before restoring. Nothing has been changed."
            elif not can_restore:
                blocked = "Restoring needs the editing lock. Nothing has been changed."
            else:
                blocked = ""
            dialog = self._make_recovery_dialog(catalog, can_restore, blocked)
            accepted = bool(dialog.exec())
            key = getattr(dialog, "selected_key", None)
            label = getattr(dialog, "selected_summary", "") or "the selected backup"
            if not accepted or not isinstance(key, str) or not key or not can_restore:
                return
            if not self.prompts.confirm(
                "Restore this backup?",
                "This uploads a new snapshot with the data from "
                f"{label}. The current snapshot stays in the folder as a backup.",
            ):
                return
            self.saving = True
            self._present(
                self.session.status,
                refresh=False,
                announce_failure=False,
                announce_reload=False,
            )
            self._run(lambda: self.session.restore_backup(key), self._after_restore)
        finally:
            if not self.saving:
                self._interaction = False

    def _after_restore(self, status: SessionStatus) -> None:
        self.saving = False
        self._present(status, refresh=True, announce_failure=False, announce_reload=False)
        lowered = status.message.lower()
        failed = status.dirty or any(
            phrase in lowered
            for phrase in (
                "nothing was",
                "not uploaded",
                "not finished",
                "could not",
                "cannot",
                "needs the",
                "left in place",
                "unchanged",
            )
        )
        if failed:
            self.prompts.warn("Restore", status.message)
        else:
            self.prompts.info("Restore", status.message)
        self._interaction = False

    def _make_recovery_dialog(self, catalog: RecoveryCatalog, can_restore: bool, blocked: str):
        factory = self.recovery_dialog
        parent = self._dialog_parent()
        if factory is None:
            from .recovery_dialog import RecoveryDialog

            factory = RecoveryDialog
        return factory(
            catalog,
            can_restore=can_restore,
            blocked_reason=blocked,
            parent=parent,
        )

    def _dialog_parent(self):
        listener = self._listener
        if listener is None:
            return None
        try:
            from PySide6.QtWidgets import QWidget
        except Exception:
            return None
        if isinstance(listener, QWidget):
            return listener
        return None

    def _offer_unsaved(self, status: SessionStatus) -> None:
        title, message, options = unsaved_prompt(status)
        picked = self.prompts.choose(title, message, options, warning=True)
        if picked == CHOICE_RELOAD_DISCARD:
            self._interaction = True
            self._run(self.session.reload_discard, self._after_status)
            return
        self._interaction = False

    def _after_poll(self, status: SessionStatus) -> None:
        self._present(status, refresh=status.reloaded, announce_failure=False, announce_reload=False)
        self._maybe_continue_close()

    def _after_status(self, status: SessionStatus) -> None:
        self._interaction = False
        self._present(status, refresh=True, announce_failure=False, announce_reload=True)
        self._maybe_continue_close()

    def _after_closed(self, status: SessionStatus) -> None:
        self._interaction = False
        self._present(status, refresh=False, announce_failure=False, announce_reload=False)
        if status.mode == MODE_CLOSED:
            self._tell_quit()
            return
        self._tell_cancel()

    def _maybe_continue_close(self) -> None:
        if self._close_after_save and not self.runner.busy and not self.saving:
            self._close_after_save = False
            self.request_close()

    def _present(
        self,
        status: SessionStatus,
        *,
        refresh: bool,
        announce_failure: bool,
        announce_reload: bool,
    ) -> None:
        del announce_failure
        if self._shut or self._listener is None:
            return
        if status.dirty != self.session.dirty:
            status = replace(status, dirty=self.session.dirty)
        view = cloud_view(status, saving=self.saving)
        self._view = view
        self._listener.apply_view(view, refresh_records=refresh or status.reloaded)
        if announce_reload and status.reloaded:
            self.prompts.info(
                "Data updated",
                "The data was updated from the other computer.",
            )
        notice = status.legacy_notice.strip()
        if announce_reload and notice and not self._showed_legacy:
            self._showed_legacy = True
            self.prompts.info("Local file", notice)

    def _run(self, fn: Callable[[], object], done: Callable[[SessionStatus], None]) -> None:
        self._submit(fn, done, lambda result: isinstance(result, SessionStatus))

    def _submit(self, fn: Callable[[], object], done: Callable, accept: Callable[[object], bool]) -> None:
        if self._shut:
            return

        def deliver(result: object) -> None:
            if self._shut:
                return
            if isinstance(result, Exception):
                self.saving = False
                self._interaction = False
                self.prompts.warn("Finance Tracker", safe_text(result))
                self._present(
                    self.session.status,
                    refresh=False,
                    announce_failure=False,
                    announce_reload=False,
                )
                self._maybe_continue_close()
                return
            if not accept(result):
                self.saving = False
                self._interaction = False
                self.prompts.warn(
                    "Finance Tracker",
                    "The operation could not be finished. Nothing was reported as saved.",
                )
                return
            done(result)

        self.runner.submit(fn, deliver)

    def _best_effort_release(self) -> None:
        try:
            if self.session.status.mode == MODE_CLOSED or self.session.dirty:
                return
            self.session.close()
        except Exception:
            return

    def _tell_quit(self) -> None:
        if self._listener is not None:
            self._listener.quit_accepted()

    def _tell_cancel(self) -> None:
        if self._listener is not None:
            self._listener.close_cancelled()
