"""Short status text for the cloud window.

The session decides what is allowed. This module only turns that into the
words and buttons the window shows. It does not talk to Drive or Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from finance.drive_adapter import DriveError
from finance.lock import (
    CHOICE_CREATE_LOCK,
    CHOICE_RECOVER,
    CHOICE_RESUME,
    CHOICE_REVERIFY,
    CHOICE_TAKE_OVER,
    CHOICE_TRY_AGAIN,
    LockError,
)
from finance.profile import ConfigError
from finance.session import (
    MODE_ACCESS_LOST,
    MODE_CLOSED,
    MODE_EDITABLE,
    MODE_OFFLINE,
    MODE_PAUSED,
    MODE_READ_ONLY,
    MODE_SETUP,
    SessionError,
    SessionStatus,
)
from finance.snapshots import CHOICE_RELOAD_DISCARD, SnapshotError

ACTION_RETRY = "retry_save"
ACTION_RESUME = "resume"
ACTION_TRY = "try_again"
ACTION_RECONNECT = "reconnect"
ACTION_RECOVER = "recover"
ACTION_CREATE_LOCK = "create_lock"
ACTION_DISCARD = "discard_unsaved"

CLOSE_WAIT = "wait"
CLOSE_CANCEL = "cancel"
CLOSE_RETRY = "retry_save"
CLOSE_DISCARD = "discard_unsaved"

_SAFE_ERRORS = (SessionError, DriveError, SnapshotError, LockError, ConfigError, OSError)
_EDITING = re.compile(r"([A-Za-z]+ on [^.]{1,80}) is editing")
_HOLDS = re.compile(r"([A-Za-z]+ on [^.]{1,80}) holds it")


@dataclass(frozen=True)
class CloudView:
    """What the status row shows. ``edits_offered`` includes paused, where a click resumes first."""

    title: str
    detail: str
    action_id: str | None
    action_label: str | None
    edits_offered: bool
    saving: bool
    tone: str


def safe_text(exc: BaseException) -> str:
    """A message that can be shown. Unexpected errors stay generic."""
    if isinstance(exc, _SAFE_ERRORS):
        text = str(exc).strip()
        if text and len(text) <= 600 and "Traceback" not in text:
            return text
    return (
        f"The operation could not be finished ({type(exc).__name__}). "
        "Nothing was reported as saved."
    )


def cloud_view(status: SessionStatus, *, saving: bool) -> CloudView:
    """The status row for one session state."""
    detail = _detail(status)
    if saving:
        return CloudView(
            title="Saving…",
            detail=detail,
            action_id=None,
            action_label=None,
            edits_offered=False,
            saving=True,
            tone="pending",
        )
    if status.dirty and status.mode == MODE_EDITABLE and status.can_edit:
        return CloudView(
            title="Changes not saved",
            detail=detail,
            action_id=ACTION_RETRY,
            action_label="Retry save",
            edits_offered=True,
            saving=False,
            tone="warn",
        )
    if status.dirty:
        return CloudView(
            title="Changes not saved — editing disabled",
            detail=detail,
            action_id=ACTION_DISCARD,
            action_label="Discard unsaved changes",
            edits_offered=False,
            saving=False,
            tone="warn",
        )
    if status.mode == MODE_EDITABLE and status.can_edit:
        return CloudView(
            title="Connected — saved",
            detail=detail,
            action_id=None,
            action_label=None,
            edits_offered=True,
            saving=False,
            tone="ok",
        )
    if status.mode == MODE_PAUSED:
        return CloudView(
            title="Paused — click to resume editing",
            detail=detail,
            action_id=ACTION_RESUME,
            action_label="Resume editing",
            edits_offered=True,
            saving=False,
            tone="muted",
        )
    if status.mode == MODE_OFFLINE:
        when = status.data_time or "an unknown time"
        return CloudView(
            title=f"Offline — read-only, data from {when}",
            detail=detail,
            action_id=ACTION_RECONNECT,
            action_label="Reconnect",
            edits_offered=False,
            saving=False,
            tone="muted",
        )
    if status.mode == MODE_ACCESS_LOST:
        action = _try_action(status)
        return CloudView(
            title="Access lost — editing disabled",
            detail=detail,
            action_id=action[0] if action else None,
            action_label=action[1] if action else None,
            edits_offered=False,
            saving=False,
            tone="warn",
        )
    if status.mode == MODE_READ_ONLY:
        action_id, action_label, title = _read_only_action(status)
        return CloudView(
            title=title,
            detail=detail,
            action_id=action_id,
            action_label=action_label,
            edits_offered=False,
            saving=False,
            tone="muted",
        )
    if status.mode == MODE_CLOSED:
        title = "Closed"
    elif status.mode == MODE_SETUP:
        title = "Setup"
    else:
        title = "Finance Tracker"
    return CloudView(
        title=title,
        detail=detail,
        action_id=None,
        action_label=None,
        edits_offered=False,
        saving=False,
        tone="muted",
    )


def close_prompt(
    status: SessionStatus, *, saving: bool, busy: bool, dirty: bool | None = None
) -> tuple[str, str, tuple[tuple[str, str], ...]] | None:
    """The close dialog. ``None`` means the lock can be released without asking.

    ``dirty`` overrides the snapshot on ``status``. A mutation updates the live
    session before the next published status.
    """
    is_dirty = status.dirty if dirty is None else dirty
    if saving:
        return (
            "Saving",
            "A save is still uploading. The lock stays with this window until that upload finishes.",
            (
                (CLOSE_WAIT, "Wait for the save"),
                (CLOSE_CANCEL, "Keep the window open"),
            ),
        )
    if busy:
        return (
            "Saving",
            "A Drive update is still running. The lock stays with this window until it finishes.",
            (
                (CLOSE_WAIT, "Wait for the save"),
                (CLOSE_CANCEL, "Keep the window open"),
            ),
        )
    if is_dirty:
        return (
            "Unsaved changes",
            "Unsaved changes are still in this window. Closing can retry the save, "
            "or discard them and return to the last saved snapshot. "
            "Cancel leaves the window open and keeps the lock.",
            (
                (CLOSE_RETRY, "Retry Save"),
                (CLOSE_DISCARD, "Discard Unsaved Changes"),
                (CLOSE_CANCEL, "Cancel"),
            ),
        )
    return None


def unsaved_prompt(status: SessionStatus) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """After a save that did not stick. Keeping the change is one of the choices."""
    return (
        "Changes not saved",
        status.message,
        (
            ("keep", "Keep the unsaved change"),
            (CHOICE_RELOAD_DISCARD, "Reload and discard unsaved changes"),
        ),
    )


def _detail(status: SessionStatus) -> str:
    parts: list[str] = []
    message = status.message.strip()
    if message:
        parts.append(message)
    notice = status.legacy_notice.strip()
    if notice and notice not in message:
        parts.append(notice)
    if status.dirty and not _mentions_unsaved(message):
        parts.append("The change is still in this window and is not saved.")
    return "\n".join(parts)


def _mentions_unsaved(message: str) -> bool:
    lowered = message.lower()
    return "still here" in lowered or "not saved" in lowered or "unsaved" in lowered


def _try_action(status: SessionStatus) -> tuple[str, str] | None:
    if CHOICE_TRY_AGAIN in status.choices or CHOICE_REVERIFY in status.choices:
        return ACTION_TRY, "Try again"
    return None


def _read_only_action(status: SessionStatus) -> tuple[str | None, str | None, str]:
    if CHOICE_TAKE_OVER in status.choices or CHOICE_RECOVER in status.choices:
        return ACTION_RECOVER, "Take over lock", "Read-only — the lock needs confirmation"
    if CHOICE_CREATE_LOCK in status.choices:
        return ACTION_CREATE_LOCK, "Create lock file", "Read-only — the lock file is missing"
    if CHOICE_RESUME in status.choices:
        return ACTION_RESUME, "Resume editing", "Paused — click to resume editing"
    if CHOICE_TRY_AGAIN in status.choices or CHOICE_REVERIFY in status.choices:
        return ACTION_TRY, "Try again", _read_only_title(status.message)
    return None, None, _read_only_title(status.message)


def _read_only_title(message: str) -> str:
    match = _EDITING.search(message) or _HOLDS.search(message)
    if match:
        return f"Read-only — {match.group(1)} is editing"
    if "Another FinanceTracker window" in message:
        return "Read-only — another window is open on this computer"
    sentence = message.split(".", 1)[0].strip()
    if sentence:
        return f"Read-only — {sentence}"
    return "Read-only"
