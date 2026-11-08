"""Cloud window decisions against a fake Drive. No real finance file."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from finance.drive_adapter import LOCK_FILE_NAME
from finance.lock import LockDocument, decode_lock, encode_lock
from finance.session import (
    CHOICE_CREATE_EMPTY,
    CHOICE_IMPORT,
    CHOICE_RESTORE_LOCAL,
    MODE_EDITABLE,
    MODE_PAUSED,
    MODE_READ_ONLY,
    MODE_SETUP,
    FinanceSession,
    SessionStatus,
)
from finance.snapshots import CHOICE_CANCEL, CHOICE_INSPECT_RECOVER, CHOICE_RELOAD_DISCARD, CHOICE_RETRY_PASSPHRASE
from tests.fake_drive import FakeDrive
from tests.scripted_prompts import ScriptedPrompts
from tests.support import SYNTHETIC_SECRET
from tests.test_session import (
    LOCK_ID,
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
from ui.cloud_open import continue_open
from ui.cloud_status import CLOSE_CANCEL, CLOSE_DISCARD, CLOSE_RETRY, CLOSE_WAIT
from ui.session_runner import InlineRunner


class RecordingListener:
    def __init__(self) -> None:
        self.views = []
        self.quit = False
        self.cancelled = False

    def apply_view(self, view, *, refresh_records: bool) -> None:
        self.views.append((view, refresh_records))

    def quit_accepted(self) -> None:
        self.quit = True

    def close_cancelled(self) -> None:
        self.cancelled = True

    @property
    def view(self):
        return self.views[-1][0]


class GateRunner:
    """Holds jobs until the test finishes them one at a time."""

    def __init__(self) -> None:
        self.jobs: list = []
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(self, fn, done) -> None:
        self._busy = True
        self.jobs.append((fn, done))

    def finish(self) -> None:
        fn, done = self.jobs.pop(0)
        try:
            result = fn()
        except Exception as exc:
            result = exc
        self._busy = bool(self.jobs)
        done(result)
        self._busy = bool(self.jobs)

    def stop(self) -> bool:
        self.jobs.clear()
        self._busy = False
        return True


def _secret_hidden(fake: FakeDrive) -> None:
    raw = SYNTHETIC_SECRET.encode("utf-8")
    for item in fake.files.values():
        if SYNTHETIC_SECRET in item.name:
            raise AssertionError("a Drive name contained the synthetic note")
        if item.name.endswith(".enc") and raw in item.content:
            raise AssertionError("an encrypted snapshot contained the synthetic note")


def _lock_bytes(fake: FakeDrive) -> bytes:
    for item in fake.files.values():
        if item.name == LOCK_FILE_NAME and not item.trashed:
            return item.content
    raise AssertionError("lock file missing")


@pytest.fixture
def opened(tmp_path):
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    _plant(fake, _records(tmp_path, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(fake)
    stamp = _finance_stamp()
    status = session.open_with_passphrase(PASSPHRASE)
    assert status.mode == MODE_EDITABLE
    prompts = ScriptedPrompts()
    host = CloudHost(session, InlineRunner(), prompts, adapter)
    listener = RecordingListener()
    host.attach(listener)
    try:
        yield host, listener, fake, session, prompts, tmp_path
    finally:
        host.shutdown()
        assert _finance_stamp() == stamp
        _secret_hidden(fake)


def _add(session: FinanceSession, amount: str = "4.00", note: str = SYNTHETIC_SECRET) -> None:
    assert session.store is not None
    session.store.add_income(
        amount=amount,
        on_date=date(2026, 10, 6),
        user="Boris",
        note=note,
    )


def test_add_edit_and_delete_each_save_one_snapshot(opened) -> None:
    host, listener, fake, session, _prompts, _root = opened
    assert listener.view.title == "Connected — saved"
    assert len(_snapshots(fake)) == 1

    _add(session)
    host.after_local_change()
    assert listener.view.title == "Connected — saved"
    assert len(_snapshots(fake)) == 2
    assert session.dirty is False

    assert session.store is not None
    added = next(record for record in session.store.income if record.user == "Boris")
    session.store.update_record(
        added.id,
        record_type="income",
        amount="5.00",
        on_date=date(2026, 10, 8),
        note="edited",
    )
    host.after_local_change()
    assert len(_snapshots(fake)) == 3

    session.store.delete_record(added.id)
    host.after_local_change()
    assert len(_snapshots(fake)) == 4
    assert session.dirty is False
    assert session.store is not None
    assert all(record.user != "Boris" for record in session.store.income)

    host.request_close()
    assert listener.quit is True
    assert len(_snapshots(fake)) == 4
    assert decode_lock(_lock_bytes(fake)).state == "free"


def test_a_failed_upload_keeps_the_change_and_the_previous_snapshot(opened) -> None:
    host, listener, fake, session, prompts, _root = opened
    prompts._choices.append("keep")
    fake.lose_create_response = True
    fake.apply_lost_create = False
    _add(session, note="outage-note")
    host.after_local_change()
    assert listener.view.title == "Changes not saved"
    assert session.dirty is True
    assert len(_snapshots(fake)) == 1
    assert session.store is not None
    assert any(record.note == "outage-note" for record in session.store.income)
    assert any(item[0] == "choose" and item[4] is True for item in prompts.asked)
    assert "outage-note" not in listener.view.detail


def test_a_generation_mismatch_can_keep_or_reload(opened) -> None:
    host, listener, fake, session, prompts, root = opened
    prompts._choices.append("keep")
    _plant(fake, _records(root, "9.00"), 2, "snap-0002-abcdefghij")
    _add(session)
    host.after_local_change()
    assert session.dirty is True
    assert len(_snapshots(fake)) == 2
    assert listener.view.title == "Changes not saved"
    assert session.store is not None
    assert len(session.store.income) == 2

    prompts._choices.append(CHOICE_RELOAD_DISCARD)
    host.primary_action()
    assert session.dirty is False
    assert session.generation == 2
    assert session.store is not None
    assert len(session.store.income) == 1
    assert session.store.income[0].amount == Decimal("9")
    assert listener.view.title == "Connected — saved"


def test_losing_the_lock_disables_editing_and_does_not_upload(opened) -> None:
    host, listener, fake, session, prompts, _root = opened
    prompts._choices.append("keep")
    _held_lock(fake)
    _add(session, note="kept-local")
    host.after_local_change()
    assert session.can_edit is False
    assert session.dirty is True
    assert len(_snapshots(fake)) == 1
    assert listener.view.title == "Changes not saved — editing disabled"
    assert listener.view.edits_offered is False
    assert session.store is not None
    assert any(record.note == "kept-local" for record in session.store.income)
    assert "kept-local" not in listener.view.detail


def test_idle_pause_resume_reloads_and_activity_resets_the_timer(opened) -> None:
    host, listener, fake, session, prompts, root = opened
    host.note_activity()
    fake.date_header = _http_date(NOW + timedelta(minutes=11))
    host.poll()
    assert session.status.mode == MODE_EDITABLE

    fake.date_header = _http_date(NOW + timedelta(minutes=22))
    host.poll()
    assert session.status.mode == MODE_PAUSED
    assert listener.view.title == "Paused — click to resume editing"
    assert listener.view.edits_offered is True

    _plant(fake, _records(root, "8.00"), 2, "snap-0002-abcdefghij")
    seen: list[str] = []
    host.begin_mutation(lambda: seen.append("dialog"))
    assert seen == ["dialog"]
    assert session.generation == 2
    assert any(title == "Data updated" for title, _message in prompts.infos)
    assert listener.view.title == "Connected — saved"


def test_resume_stays_read_only_when_someone_else_holds_the_lock(opened) -> None:
    host, _listener, fake, session, prompts, _root = opened
    fake.date_header = _http_date(NOW + timedelta(minutes=11))
    host.poll()
    assert session.status.mode == MODE_PAUSED
    when = NOW + timedelta(minutes=12)
    fake.date_header = _http_date(when)
    document = LockDocument(
        format_version=1,
        state="held",
        session_token="ab" * 32,
        owner_label="Milena",
        machine_id="cd" * 32,
        machine_label="macOS",
        acquired_at_utc=when,
        heartbeat_at_utc=when,
    )
    fake.add(LOCK_ID, LOCK_FILE_NAME, content=encode_lock(document), created_time=when)
    seen: list[str] = []
    host.begin_mutation(lambda: seen.append("dialog"))
    assert seen == []
    assert session.status.mode == MODE_READ_ONLY
    assert prompts.warnings
    assert "Milena" in prompts.warnings[-1][1]


def test_close_cancel_keeps_the_lock_and_discard_drops_the_change(opened) -> None:
    host, listener, fake, session, prompts, _root = opened
    _add(session)
    prompts._choices.append(CLOSE_CANCEL)
    host.request_close()
    assert listener.cancelled is True
    assert listener.quit is False
    assert session.dirty is True
    assert decode_lock(_lock_bytes(fake)).state == "held"
    assert len(_snapshots(fake)) == 1

    prompts._choices.append(CLOSE_DISCARD)
    host.request_close()
    assert listener.quit is True
    assert session.status.mode == "closed"
    assert decode_lock(_lock_bytes(fake)).state == "free"
    assert len(_snapshots(fake)) == 1
    assert session.store is not None
    assert all(record.user != "Boris" for record in session.store.income)


def test_close_retry_saves_once_then_releases_the_lock(opened) -> None:
    host, listener, fake, session, prompts, _root = opened
    _add(session)
    prompts._choices.append(CLOSE_RETRY)
    host.request_close()
    assert listener.quit is True
    assert len(_snapshots(fake)) == 2
    assert decode_lock(_lock_bytes(fake)).state == "free"
    assert session.dirty is False


def test_waiting_for_an_upload_does_not_release_the_lock(tmp_path) -> None:
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    _plant(fake, _records(tmp_path, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(fake)
    assert session.open_with_passphrase(PASSPHRASE).mode == MODE_EDITABLE
    prompts = ScriptedPrompts(choices=[CLOSE_WAIT])
    runner = GateRunner()
    host = CloudHost(session, runner, prompts, adapter)
    listener = RecordingListener()
    host.attach(listener)
    try:
        _add(session)
        host.after_local_change()
        assert listener.view.title == "Saving…"
        assert runner.jobs
        host.request_close()
        assert listener.quit is False
        assert decode_lock(_lock_bytes(fake)).state == "held"
        assert len(_snapshots(fake)) == 1
        runner.finish()
        assert len(_snapshots(fake)) == 2
        assert listener.quit is False
        assert runner.jobs
        runner.finish()
        assert listener.quit is True
        assert decode_lock(_lock_bytes(fake)).state == "free"
        assert len(_snapshots(fake)) == 2
    finally:
        host.shutdown()
        _secret_hidden(fake)


def test_system_close_releases_a_clean_lock(opened) -> None:
    host, _listener, fake, _session, _prompts, _root = opened
    assert host.close_synchronously() is True
    assert decode_lock(_lock_bytes(fake)).state == "free"


def test_system_close_can_discard(opened) -> None:
    host, _listener, fake, session, prompts, _root = opened
    _add(session)
    prompts._choices.append(CLOSE_DISCARD)
    assert host.close_synchronously() is True
    assert decode_lock(_lock_bytes(fake)).state == "free"
    assert len(_snapshots(fake)) == 1
    assert session.store is not None
    assert all(record.user != "Boris" for record in session.store.income)


def test_release_if_idle_leaves_a_dirty_lock(opened) -> None:
    host, _listener, fake, session, _prompts, _root = opened
    _add(session)
    host.release_if_idle()
    assert decode_lock(_lock_bytes(fake)).state == "held"
    assert session.dirty is True
    assert session.status.mode == MODE_EDITABLE


def test_startup_mismatch_cancel_and_restore_message(tmp_path) -> None:
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    source = _records(tmp_path, "14.20")
    original = source.to_json_bytes()
    session._legacy_path.write_bytes(original)
    stamp = _finance_stamp()
    try:
        preview = session.open_with_passphrase("not-checked-yet")
        assert preview.mode == MODE_SETUP
        mismatch = ScriptedPrompts(
            choices=[CHOICE_IMPORT, CHOICE_CANCEL],
            pairs=[("alpha", "beta")],
            directories=[tmp_path / "backups"],
        )
        assert continue_open(session, mismatch, preview) is None
        assert session._legacy_path.read_bytes() == original
        assert _snapshots(fake) == []
        assert any("did not match" in item[2] for item in mismatch.asked if item[0] == "choose")

        stayed = continue_open(
            session,
            ScriptedPrompts(
                choices=[CHOICE_CREATE_EMPTY, CHOICE_CANCEL],
                pairs=[(PASSPHRASE, PASSPHRASE)],
                confirms=[False],
            ),
            preview,
        )
        assert stayed is None
        assert _snapshots(fake) == []

        restore_prompts = ScriptedPrompts(choices=[CHOICE_RESTORE_LOCAL, CHOICE_CANCEL])
        assert continue_open(session, restore_prompts, preview) is None
        assert any(
            "not available" in item[2] for item in restore_prompts.asked if item[0] == "choose"
        )
    finally:
        session.close(discard=True)
        adapter.close()
        assert _finance_stamp() == stamp
        assert session._legacy_path.read_bytes() == original


def test_wrong_passphrase_can_be_retried_from_startup(tmp_path) -> None:
    fake = FakeDrive("folder-id-0123456789abc")
    fake.date_header = _http_date(NOW)
    adapter = _adapter(fake)
    session = _session(adapter, tmp_path)
    _plant(fake, _records(tmp_path, "20.00"), 1, "snap-0001-abcdefghij")
    _free_lock(fake)
    try:
        failed = session.open_with_passphrase("wrong-passphrase-value")
        prompts = ScriptedPrompts(
            choices=[CHOICE_RETRY_PASSPHRASE],
            passphrases=[PASSPHRASE],
        )
        opened_status = continue_open(session, prompts, failed)
        assert opened_status is not None
        assert opened_status.mode == MODE_EDITABLE
        assert PASSPHRASE not in opened_status.message
        assert "wrong-passphrase-value" not in opened_status.message
    finally:
        session.close(discard=True)
        adapter.close()


def test_backup_browsing_choice_does_not_change_anything() -> None:
    class FakeSession:
        store = None
        calls: list[str]

        def __init__(self) -> None:
            self.calls = []

        def restore_local_copy(self):
            self.calls.append("restore")
            return SessionStatus(mode=MODE_SETUP, message="unused", can_edit=False)

    session = FakeSession()
    prompts = ScriptedPrompts(choices=[CHOICE_INSPECT_RECOVER, CHOICE_CANCEL])
    status = SessionStatus(
        mode=MODE_SETUP,
        message="No snapshot could be decrypted.",
        can_edit=False,
        choices=(CHOICE_INSPECT_RECOVER, CHOICE_CANCEL),
    )
    assert continue_open(session, prompts, status) is None
    assert session.calls == []
    assert any("not in the window" in message for _title, message in prompts.infos)
