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
        assert PASSPHRASE not in _texts(window)
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
