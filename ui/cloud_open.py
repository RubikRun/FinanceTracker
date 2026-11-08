"""Startup choices before the main window is shown.

Import, empty-dataset creation, and a wrong passphrase stay here so the
window only opens once a store exists. Session calls go through ``call``
so the Qt thread can keep processing events.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from finance.lock import CHOICE_REVERIFY, CHOICE_TRY_AGAIN
from finance.session import (
    CHOICE_CANCEL_CLOSE,
    CHOICE_CREATE_EMPTY,
    CHOICE_DISCARD,
    CHOICE_IMPORT,
    CHOICE_RESTORE_LOCAL,
    CHOICE_RETRY_SAVE,
    MODE_CLOSED,
    MODE_SETUP,
    FinanceSession,
    SessionError,
    SessionStatus,
)
from finance.snapshots import (
    CHOICE_CANCEL,
    CHOICE_INSPECT_RECOVER,
    CHOICE_RETRY_PASSPHRASE,
)

_Call = Callable[[Callable[[], object]], object]

_CHOICE_LABELS = {
    CHOICE_IMPORT: "Import the local finance file",
    CHOICE_CREATE_EMPTY: "Create an empty dataset",
    CHOICE_RESTORE_LOCAL: "Restore a local encrypted copy",
    CHOICE_CANCEL: "Cancel",
    CHOICE_RETRY_PASSPHRASE: "Try the passphrase again",
    CHOICE_INSPECT_RECOVER: "Backup browsing is not available yet",
    CHOICE_RETRY_SAVE: "Try again",
    CHOICE_TRY_AGAIN: "Try again",
    CHOICE_REVERIFY: "Try again",
    CHOICE_DISCARD: "Discard",
    CHOICE_CANCEL_CLOSE: "Cancel",
}

_PASSPHRASE_INTRO = (
    "Enter the finance passphrase. It is not saved on this computer."
)
_NEW_PASSPHRASE_INTRO = (
    "Choose the finance passphrase and type it again. "
    "A forgotten passphrase makes the data and every backup permanently unreadable. "
    "Store it in a password manager. Changing the passphrase is not supported. "
    "It is not saved on this computer."
)
_BACKUP_INTRO = (
    "Choose a folder for the plaintext backup. The backup is not encrypted. "
    "Keep it outside the shared Drive folder."
)


def continue_open(
    session: FinanceSession,
    prompts,
    status: SessionStatus,
    *,
    call: _Call | None = None,
) -> SessionStatus | None:
    """Follow setup choices until a dataset is open, or the user stops.

    Returns None when the user cancels. The returned status has a store when
    the window should open.
    """
    invoke = call or (lambda fn: fn())
    shown_backup: set[str] = set()
    while True:
        _note_backup(prompts, status, shown_backup)
        if status.mode == MODE_CLOSED:
            return None
        if _window_ready(session, status):
            return status
        options = _options(status)
        if not options:
            prompts.warn("Finance Tracker", status.message or "The dataset could not be opened.")
            return None
        picked = prompts.choose("Finance Tracker", status.message, options)
        if picked is None or picked in (CHOICE_CANCEL, CHOICE_CANCEL_CLOSE):
            return None
        nxt = _dispatch(session, prompts, picked, invoke)
        if nxt is None:
            continue
        status = nxt


def _dispatch(session, prompts, picked: str, invoke: _Call) -> SessionStatus | None:
    if picked == CHOICE_IMPORT:
        return _import(session, prompts, invoke)
    if picked == CHOICE_CREATE_EMPTY:
        return _create_empty(session, prompts, invoke)
    if picked == CHOICE_RESTORE_LOCAL:
        return _as_status(invoke(session.restore_local_copy))
    if picked == CHOICE_INSPECT_RECOVER:
        prompts.info(
            "Recovery",
            "Backup browsing is not in the window yet. Nothing was changed.",
        )
        return None
    if picked in (CHOICE_RETRY_PASSPHRASE, CHOICE_TRY_AGAIN, CHOICE_REVERIFY):
        return _open_again(session, prompts, invoke)
    prompts.warn("Finance Tracker", "That choice is not available. Nothing was changed.")
    return None


def _open_again(session, prompts, invoke: _Call) -> SessionStatus | None:
    phrase = prompts.passphrase(_PASSPHRASE_INTRO)
    if not phrase:
        return None
    try:
        return _as_status(invoke(lambda: session.open_with_passphrase(phrase)))
    finally:
        phrase = ""


def _import(session, prompts, invoke: _Call) -> SessionStatus | None:
    pair = prompts.new_passphrase(_NEW_PASSPHRASE_INTRO)
    if pair is None:
        return None
    first, second = pair
    directory = prompts.directory(_BACKUP_INTRO)
    if directory is None:
        return None
    try:
        return _as_status(
            invoke(lambda: session.import_legacy(first, second, Path(directory)))
        )
    finally:
        first = ""
        second = ""


def _create_empty(session, prompts, invoke: _Call) -> SessionStatus | None:
    if not prompts.confirm(
        "Create an empty dataset?",
        "This uploads an empty encrypted dataset to the Drive folder. "
        "If this computer should already have your data, cancel and check the folder.",
    ):
        return None
    pair = prompts.new_passphrase(_NEW_PASSPHRASE_INTRO)
    if pair is None:
        return None
    first, second = pair
    try:
        return _as_status(invoke(lambda: session.create_empty(first, second)))
    finally:
        first = ""
        second = ""


def _window_ready(session: FinanceSession, status: SessionStatus) -> bool:
    return session.store is not None and status.mode not in (MODE_SETUP, MODE_CLOSED)


def _options(status: SessionStatus) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for choice in status.choices:
        label = _CHOICE_LABELS.get(choice)
        if label is None:
            continue
        found.append((choice, label))
    return tuple(found)


def _note_backup(prompts, status: SessionStatus, shown: set[str]) -> None:
    path = status.backup_path
    if not path or path in shown:
        return
    shown.add(path)
    prompts.info(
        "Plaintext backup",
        "A plaintext backup was written. It is not encrypted. "
        "Keep it outside the shared Drive folder.\n\n"
        f"{path}",
    )


def _as_status(value: object) -> SessionStatus:
    if isinstance(value, Exception):
        raise value
    if not isinstance(value, SessionStatus):
        raise SessionError("The session did not return a status.")
    return value
