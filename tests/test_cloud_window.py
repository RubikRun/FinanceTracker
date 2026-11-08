"""Qt checks for the cloud window. Drive is fake and the finance file is not used."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date
from decimal import Decimal

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from finance.snapshots import SOURCE_DRIVE, RecoveryCatalog, RecoveryItem
from finance.store import FinanceStore
from tests.fake_drive import FakeDrive
from tests.scripted_prompts import ScriptedPrompts
from tests.support import SYNTHETIC_SECRET
from tests.test_session import (
    NOW,
    PASSPHRASE,
    _adapter,
    _finance_stamp,
    _free_lock,
    _held_lock,
    _http_date,
    _plant,
    _records,
    _session,
    _snapshots,
)
from ui.cloud_host import CloudHost
from ui.cloud_prompts import _PassphraseDialog
from ui.main_window import MainWindow
from ui.recovery_dialog import RecoveryDialog
from ui.session_runner import InlineRunner, QtJobRunner


@pytest.fixture(scope="module")
def qapp():
    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    return application


class _Dialog:
    def __init__(self, data: dict) -> None:
        self.result_data = data

    def exec(self) -> bool:
        return True


class _Dialogs:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        if "initial" in kwargs:
            data = {
                "type": "income",
                "amount": Decimal("5.00"),
                "date": date(2026, 10, 8),
                "note": "edited",
                "user": kwargs["for_user"],
            }
        else:
            data = {
                "type": "income",
                "amount": Decimal("4.00"),
                "date": date(2026, 10, 6),
                "note": SYNTHETIC_SECRET,
                "user": kwargs["for_user"],
            }
        return _Dialog(data)


def _secret_hidden(fake: FakeDrive) -> None:
    raw = SYNTHETIC_SECRET.encode("utf-8")
    for item in fake.files.values():
        if SYNTHETIC_SECRET in item.name or (item.name.endswith(".enc") and raw in item.content):
            raise AssertionError("Drive metadata contained the synthetic note")


def _texts(window: MainWindow) -> str:
    return "\n".join(
        (
            window.windowTitle(),
            window.sync_label.text(),
            window.sync_detail.text(),
            window.sync_action.text(),
        )
    )


def test_local_window_still_writes_its_file_and_hides_sync(qapp, tmp_path) -> None:
    del qapp
    path = tmp_path / "local.json"
    store = FinanceStore(path)
    window = MainWindow(store)
    window.show()
    window._add_dialog_factory = _Dialogs()
    window._on_add()
    assert window.sync_panel.isHidden()
    assert window.backups_btn.isHidden()
    reloaded = FinanceStore.load(path)
    assert len(reloaded.income) == 1
    assert reloaded.income[0].amount == Decimal("4.00")
    assert reloaded.income[0].user == "Boris"
    window.close()


def test_read_only_window_names_the_lock_holder(qapp, tmp_path) -> None:
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    _plant(fake, _records(tmp_path, "20.00"), 1, "snap-0001-abcdefghij")
    _held_lock(fake)
    stamp = _finance_stamp()
    assert session.open_with_passphrase(PASSPHRASE).mode == "read_only"
    prompts = ScriptedPrompts()
    host = CloudHost(session, InlineRunner(), prompts, adapter)
    window = MainWindow(session.store, host=host, poll_ms=0)
    window.show()
    qapp.processEvents()
    try:
        assert window.sync_label.text() == "Read-only — Milena on macOS is editing"
        assert window.sync_action.text() == "Try again"
        assert window.add_btn.isHidden()
        assert window.backups_btn.isVisible()
        assert PASSPHRASE not in _texts(window)

        seen: dict[str, object] = {}

        class _Look:
            def __init__(self, catalog, *, can_restore, blocked_reason, parent=None) -> None:
                self.selected_key = None
                self.selected_summary = ""
                seen["can"] = can_restore
                seen["reason"] = blocked_reason
                seen["count"] = len(catalog.valid)

            def exec(self) -> int:
                return 0

        host.recovery_dialog = _Look
        window.backups_btn.click()
        assert seen["can"] is False
        assert "lock" in str(seen["reason"]).lower()
        assert seen["count"] == 1
        assert len(_snapshots(fake)) == 1
        assert PASSPHRASE not in str(seen["reason"])
    finally:
        window.shutdown()
        assert _finance_stamp() == stamp
        _secret_hidden(fake)


def test_window_add_edit_delete_and_a_clean_close_save_one_snapshot_each(qapp, tmp_path) -> None:
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    _plant(fake, _records(tmp_path, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(fake)
    stamp = _finance_stamp()
    assert session.open_with_passphrase(PASSPHRASE).mode == "editable"
    prompts = ScriptedPrompts()
    host = CloudHost(session, InlineRunner(), prompts, adapter)
    window = MainWindow(session.store, host=host, poll_ms=0)
    window._add_dialog_factory = _Dialogs()
    window._delete_confirmation = lambda _text: True
    window.show()
    qapp.processEvents()
    try:
        window._on_add()
        assert len(_snapshots(fake)) == 2
        assert window.sync_label.text() == "Connected — saved"
        assert window.income_table.rowCount() == 1

        from_table = window.income_table.item(0, 0).data(Qt.UserRole)
        window._on_edit(int(from_table))
        assert len(_snapshots(fake)) == 3

        from_table = window.income_table.item(0, 0).data(Qt.UserRole)
        window._on_delete(int(from_table))
        assert len(_snapshots(fake)) == 4
        assert window.sync_label.text() == "Connected — saved"
        assert PASSPHRASE not in _texts(window)

        window.close()
        qapp.processEvents()
        assert window.isHidden()
        assert len(_snapshots(fake)) == 4
        assert session.status.mode == "closed"
    finally:
        window.shutdown()
        assert _finance_stamp() == stamp
        _secret_hidden(fake)


def test_save_stays_on_screen_as_saving_until_drive_finishes(qapp, tmp_path) -> None:
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    _plant(fake, _records(tmp_path, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(fake)
    stamp = _finance_stamp()
    assert session.open_with_passphrase(PASSPHRASE).mode == "editable"
    prompts = ScriptedPrompts()
    runner = QtJobRunner()
    host = CloudHost(session, runner, prompts, adapter)
    window = MainWindow(session.store, host=host, poll_ms=0)
    dialogs = _Dialogs()
    window._add_dialog_factory = dialogs
    window.show()
    qapp.processEvents()
    fake.pause_methods.add("create")
    try:
        window._on_add()
        assert dialogs.calls == 1
        assert window.sync_label.text() == "Saving…"
        assert window.add_btn.isEnabled() is False
        deadline = 0
        while not fake.entered.is_set() and deadline < 200:
            qapp.processEvents()
            fake.entered.wait(0.05)
            deadline += 1
        assert fake.entered.is_set()
        assert window.sync_label.text() == "Saving…"
        window._on_add()
        assert dialogs.calls == 1
        fake.release.set()
        deadline = time.monotonic() + 20
        while window.sync_label.text() == "Saving…" and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.05)
        assert window.sync_label.text() == "Connected — saved"
        assert len(_snapshots(fake)) == 2
        assert PASSPHRASE not in _texts(window)
    finally:
        fake.release.set()
        window.shutdown()
        assert _finance_stamp() == stamp
        _secret_hidden(fake)


def test_passphrase_dialog_clears_the_fields(qapp) -> None:
    del qapp
    mismatch = _PassphraseDialog(None, intro="Type it twice.", confirm=True, ask_owner=False)
    mismatch._phrase.setText("one-secret-value")
    mismatch._again.setText("other-secret-value")
    mismatch.submit_for_test()
    assert mismatch._phrase.text() == ""
    assert mismatch._again.text() == ""
    assert mismatch._pair is None
    assert mismatch.result() != QDialog.Accepted
    assert "did not match" in mismatch._error.text()

    matched = _PassphraseDialog(None, intro="Type it twice.", confirm=True, ask_owner=False)
    matched._phrase.setText("same-secret-value")
    matched._again.setText("same-secret-value")
    matched.submit_for_test()
    assert matched._phrase.text() == ""
    assert matched._again.text() == ""
    assert matched.take_pair() == ("same-secret-value", "same-secret-value")
    assert matched._pair is None


def test_recovery_dialog_shows_counts_and_hides_notes(qapp) -> None:
    hidden_note = "hidden-note-should-not-appear"
    catalog = RecoveryCatalog(
        valid=(
            RecoveryItem(
                key="drive:current-file-012345",
                source=SOURCE_DRIVE,
                name="finance_snapshot_g000002_20261028T160000Z_bbbbbbbbbbbb.enc",
                generation=2,
                created_at_utc=NOW,
                spending_count=0,
                income_count=2,
                next_id=3,
                saved_by="Account A",
                known_valid=True,
                conflict=False,
                current=True,
                invalid=False,
                reason="",
            ),
            RecoveryItem(
                key="drive:older-file-01234567",
                source=SOURCE_DRIVE,
                name="finance_snapshot_g000001_20261028T160000Z_aaaaaaaaaaaa.enc",
                generation=1,
                created_at_utc=NOW,
                spending_count=0,
                income_count=1,
                next_id=2,
                saved_by="Account A",
                known_valid=True,
                conflict=True,
                current=False,
                invalid=False,
                reason="",
            ),
        ),
        unusable=(
            RecoveryItem(
                key="drive:bad-file-0123456789",
                source=SOURCE_DRIVE,
                name="finance_snapshot_g000003_20261028T160000Z_cccccccccccc.enc",
                generation=3,
                created_at_utc=None,
                spending_count=None,
                income_count=None,
                next_id=None,
                saved_by="Account A",
                known_valid=False,
                conflict=False,
                current=False,
                invalid=True,
                reason="The snapshot envelope could not be read.",
            ),
        ),
    )
    dialog = RecoveryDialog(catalog, can_restore=True, blocked_reason="", parent=None)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog._valid.rowCount() == 2
        assert dialog._unusable.rowCount() == 1
        assert dialog._unusable.item(0, 4).text().startswith("The snapshot envelope")
        assert dialog._restore_btn.isEnabled() is False
        dialog._valid.selectRow(0)
        qapp.processEvents()
        assert dialog._restore_btn.isEnabled() is False
        dialog._valid.selectRow(1)
        qapp.processEvents()
        assert dialog._restore_btn.isEnabled() is True
        assert "Conflicting save" in dialog._valid.item(1, 6).text()
        dialog._restore_btn.click()
        assert dialog.selected_key == "drive:older-file-01234567"
        shown = dialog.selected_summary + " ".join(
            dialog._valid.item(row, column).text()
            for row in range(dialog._valid.rowCount())
            for column in range(dialog._valid.columnCount())
        )
        assert hidden_note not in shown
        assert "Account A" in shown
        assert "1" in dialog._valid.item(1, 4).text()
    finally:
        dialog.close()


def test_backups_button_restores_the_older_snapshot(qapp, tmp_path) -> None:
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    _plant(fake, _records(tmp_path, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(fake)
    stamp = _finance_stamp()
    assert session.open_with_passphrase(PASSPHRASE).mode == "editable"
    session.store.add_income(
        amount="4.00",
        on_date=date(2026, 10, 6),
        user="Boris",
        note=SYNTHETIC_SECRET,
    )
    assert session.save().generation == 2
    prompts = ScriptedPrompts()
    host = CloudHost(session, InlineRunner(), prompts, adapter)
    window = MainWindow(session.store, host=host, poll_ms=0)
    window.show()
    qapp.processEvents()

    class _Picker:
        def __init__(self, catalog, *, can_restore, blocked_reason, parent=None) -> None:
            self.selected_key = None
            self.selected_summary = ""
            self.can_restore = can_restore
            choices = [item for item in catalog.valid if not item.current]
            if choices and can_restore:
                chosen = choices[-1]
                self.selected_key = chosen.key
                self.selected_summary = f"generation {chosen.generation}"

        def exec(self) -> int:
            return 1 if self.selected_key else 0

    host.recovery_dialog = _Picker
    try:
        assert window.backups_btn.isEnabled()
        window.backups_btn.click()
        qapp.processEvents()
        assert session.generation == 3
        assert session.dirty is False
        assert session.store is not None
        assert len(session.store.income) == 1
        assert session.store.income[0].amount == Decimal("20")
        assert len(_snapshots(fake)) == 3
        assert any(item[0] == "confirm" for item in prompts.asked)
        confirm = next(item for item in prompts.asked if item[0] == "confirm")
        assert SYNTHETIC_SECRET not in confirm[2]
        assert PASSPHRASE not in confirm[2]
        assert "generation 1" in confirm[2]
        assert window.sync_label.text() == "Connected — saved"
        assert any(title == "Restore" for title, _message in prompts.infos)
    finally:
        window.shutdown()
        assert _finance_stamp() == stamp
        _secret_hidden(fake)


def test_cancelling_restore_confirmation_uploads_nothing(qapp, tmp_path) -> None:
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    _plant(fake, _records(tmp_path, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(fake)
    stamp = _finance_stamp()
    assert session.open_with_passphrase(PASSPHRASE).mode == "editable"
    session.store.add_income(
        amount="4.00",
        on_date=date(2026, 10, 6),
        user="Boris",
        note="second",
    )
    assert session.save().generation == 2
    prompts = ScriptedPrompts(confirms=[False])
    host = CloudHost(session, InlineRunner(), prompts, adapter)
    window = MainWindow(session.store, host=host, poll_ms=0)
    window.show()

    class _Picker:
        def __init__(self, catalog, *, can_restore, blocked_reason, parent=None) -> None:
            self.can_restore = can_restore
            choices = [item for item in catalog.valid if not item.current]
            self.selected_key = choices[-1].key if choices else None
            self.selected_summary = "generation 1"

        def exec(self) -> int:
            return 1 if self.selected_key else 0

    host.recovery_dialog = _Picker
    try:
        window.backups_btn.click()
        assert session.generation == 2
        assert len(_snapshots(fake)) == 2
        assert session.store is not None
        assert len(session.store.income) == 2
    finally:
        window.shutdown()
        assert _finance_stamp() == stamp
        _secret_hidden(fake)
