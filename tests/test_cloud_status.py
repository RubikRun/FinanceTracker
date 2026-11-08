"""Status text and the decision to use the cloud window. No Qt and no Drive."""

from __future__ import annotations

from finance.profile import LocalConfig
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
from finance.lock import CHOICE_CREATE_LOCK, CHOICE_RECOVER, CHOICE_TAKE_OVER, CHOICE_TRY_AGAIN
from finance.snapshots import CHOICE_RELOAD_DISCARD
from ui.cloud_startup import cloud_session_required, cloud_start_block
from ui.cloud_status import (
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
    close_prompt,
    cloud_view,
    safe_text,
    unsaved_prompt,
)
from tests.support import SYNTHETIC_SECRET


def _status(mode: str, **kwargs) -> SessionStatus:
    message = kwargs.pop("message", "Status for the window.")
    can_edit = kwargs.pop("can_edit", mode == MODE_EDITABLE)
    return SessionStatus(mode=mode, message=message, can_edit=can_edit, **kwargs)


def test_editable_saved_paused_offline_and_access_lost() -> None:
    saved = cloud_view(_status(MODE_EDITABLE, message="The lock is held."), saving=False)
    assert saved.title == "Connected — saved"
    assert saved.edits_offered is True
    assert saved.action_id is None
    assert saved.tone == "ok"

    dirty = cloud_view(_status(MODE_EDITABLE, dirty=True, message="The upload failed."), saving=False)
    assert dirty.title == "Changes not saved"
    assert dirty.action_id == ACTION_RETRY
    assert dirty.action_label == "Retry save"
    assert dirty.tone == "warn"
    assert "not saved" in dirty.detail

    saving = cloud_view(_status(MODE_EDITABLE, dirty=True), saving=True)
    assert saving.title == "Saving…"
    assert saving.edits_offered is False
    assert saving.action_id is None

    paused = cloud_view(_status(MODE_PAUSED, can_edit=False), saving=False)
    assert paused.title == "Paused — click to resume editing"
    assert paused.action_id == ACTION_RESUME
    assert paused.action_label == "Resume editing"
    assert paused.edits_offered is True

    offline = cloud_view(
        _status(
            MODE_OFFLINE,
            can_edit=False,
            data_time="2026-10-28 16:00:00 UTC",
            message="Offline — read-only.",
        ),
        saving=False,
    )
    assert offline.title == "Offline — read-only, data from 2026-10-28 16:00:00 UTC"
    assert offline.action_id == ACTION_RECONNECT
    assert offline.action_label == "Reconnect"
    assert offline.edits_offered is False

    lost = cloud_view(
        _status(
            MODE_ACCESS_LOST,
            can_edit=False,
            choices=(CHOICE_TRY_AGAIN,),
            message="The lock could not be confirmed.",
        ),
        saving=False,
    )
    assert lost.title == "Access lost — editing disabled"
    assert lost.action_id == ACTION_TRY
    assert lost.action_label == "Try again"
    assert lost.edits_offered is False


def test_read_only_titles_and_lock_actions() -> None:
    held = cloud_view(
        _status(
            MODE_READ_ONLY,
            can_edit=False,
            choices=(CHOICE_TRY_AGAIN,),
            message=(
                "Milena on macOS is editing. The last heartbeat was 2026-10-28 16:00:00 UTC. "
                "This window is read-only."
            ),
        ),
        saving=False,
    )
    assert held.title == "Read-only — Milena on macOS is editing"
    assert held.action_label == "Try again"
    assert held.edits_offered is False

    other_window = cloud_view(
        _status(
            MODE_READ_ONLY,
            can_edit=False,
            message="Another FinanceTracker window is already open on this computer. This window is read-only.",
        ),
        saving=False,
    )
    assert other_window.title == "Read-only — another window is open on this computer"

    recover = cloud_view(
        _status(MODE_READ_ONLY, can_edit=False, choices=(CHOICE_RECOVER,), message="It was not taken yet."),
        saving=False,
    )
    assert recover.action_id == ACTION_RECOVER
    assert recover.action_label == "Take over lock"

    takeover = cloud_view(
        _status(MODE_READ_ONLY, can_edit=False, choices=(CHOICE_TAKE_OVER,), message="You can take it over now."),
        saving=False,
    )
    assert takeover.action_id == ACTION_RECOVER

    missing = cloud_view(
        _status(
            MODE_READ_ONLY,
            can_edit=False,
            choices=(CHOICE_CREATE_LOCK,),
            message="The lock file is missing.",
        ),
        saving=False,
    )
    assert missing.action_label == "Create lock file"
    assert missing.title == "Read-only — the lock file is missing"


def test_dirty_without_the_lock_stays_visible() -> None:
    view = cloud_view(
        _status(MODE_ACCESS_LOST, can_edit=False, dirty=True, message="Editing is disabled."),
        saving=False,
    )
    assert view.title == "Changes not saved — editing disabled"
    assert view.action_id == ACTION_DISCARD
    assert view.edits_offered is False
    assert "still in this window" in view.detail


def test_close_and_unsaved_prompts() -> None:
    clean = _status(MODE_EDITABLE)
    assert close_prompt(clean, saving=False, busy=False) is None

    saving = close_prompt(clean, saving=True, busy=False)
    assert saving is not None
    assert saving[2][0] == (CLOSE_WAIT, "Wait for the save")
    assert saving[2][1] == (CLOSE_CANCEL, "Keep the window open")
    assert "still uploading" in saving[1]

    dirty = close_prompt(_status(MODE_EDITABLE, dirty=True), saving=False, busy=False)
    assert dirty is not None
    labels = [item[1] for item in dirty[2]]
    assert labels == ["Retry Save", "Discard Unsaved Changes", "Cancel"]
    assert dirty[2][0][0] == CLOSE_RETRY
    assert dirty[2][1][0] == CLOSE_DISCARD

    title, message, options = unsaved_prompt(
        _status(MODE_EDITABLE, dirty=True, message="The newest snapshot is ahead.")
    )
    assert title == "Changes not saved"
    assert "ahead" in message
    assert options[1][0] == CHOICE_RELOAD_DISCARD
    assert options[0][1] == "Keep the unsaved change"


def test_setup_and_closed_have_no_edit_button() -> None:
    setup = cloud_view(_status(MODE_SETUP, can_edit=False, message="Nothing has been uploaded."), saving=False)
    closed = cloud_view(_status(MODE_CLOSED, can_edit=False, message="The session is closed."), saving=False)
    assert setup.edits_offered is False
    assert closed.title == "Closed"
    assert closed.edits_offered is False


def test_safe_text_hides_an_unexpected_payload() -> None:
    shown = safe_text(RuntimeError(SYNTHETIC_SECRET))
    assert SYNTHETIC_SECRET not in shown
    assert "Nothing was reported as saved" in shown
    assert safe_text(SessionError("Editing is disabled. Nothing was changed.")) == (
        "Editing is disabled. Nothing was changed."
    )


def test_cloud_window_is_required_only_after_a_folder_is_saved() -> None:
    assert cloud_session_required(LocalConfig()) is False
    assert cloud_start_block(LocalConfig()) is None
    assert cloud_session_required(LocalConfig(drive_folder_id="folder-id-0123456789abc")) is True
    assert cloud_start_block(LocalConfig(drive_folder_id="folder-id-0123456789abc")) is None
    assert cloud_session_required(LocalConfig(cloud_mode=True)) is True
    blocked = cloud_start_block(LocalConfig(cloud_mode=True))
    assert blocked is not None
    assert "was not opened" in blocked
    invalid = cloud_start_block(LocalConfig(drive_folder_id="short"))
    assert invalid is not None
    assert "was not opened" in invalid
