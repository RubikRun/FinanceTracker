"""The manual session check refuses the real finance file and a mismatched passphrase."""

from __future__ import annotations

import subprocess
import sys
from io import StringIO
from pathlib import Path

from finance import default_data_path
from finance.drive_adapter import GoogleDriveAdapter
from finance.profile import (
    CONFIG_DIR_ENV,
    ensure_machine_identity,
    load_local_config,
    save_local_config,
)
from finance.session_check import run_session_check
from tests.fake_drive import FakeDrive
from tests.support import SYNTHETIC_SECRET

FOLDER_ID = "folder-id-0123456789abc"


def _adapter(fake: FakeDrive) -> GoogleDriveAdapter:
    return GoogleDriveAdapter(
        FOLDER_ID,
        service=fake.service(),
        sleep=lambda _delay: None,
        retry_delays=(),
        list_delays=(),
        list_attempts=1,
    )


def _save_folder() -> None:
    config = ensure_machine_identity()
    config.drive_folder_id = FOLDER_ID
    save_local_config(config)


def _stamp() -> tuple[bool, int | None]:
    path = default_data_path()
    if not path.exists():
        return False, None
    return True, path.stat().st_mtime_ns


def test_running_the_module_prints_usage() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "finance.session_check"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "usage" in combined.lower()
    assert combined.strip() != ""


def test_refuses_without_a_dry_run_profile(monkeypatch) -> None:
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    code = run_session_check(["open"], output=StringIO())
    assert code == 1


def test_refuses_the_real_finance_file(tmp_path: Path) -> None:
    _save_folder()
    before = _stamp()
    output = StringIO()

    def opener(_folder_id: str):
        raise AssertionError("the real finance file must be refused before Drive is opened")

    code = run_session_check(
        ["migrate", "--source", str(default_data_path()), "--backup-dir", str(tmp_path)],
        output=output,
        opener=opener,
        sleep=lambda _delay: None,
    )
    text = output.getvalue()
    assert code == 1
    assert "next to the program" in text
    assert SYNTHETIC_SECRET not in text
    assert _stamp() == before
    assert load_local_config().cloud_mode is False


def test_mismatched_passphrase_leaves_the_copy_and_the_folder(
    tmp_path: Path,
) -> None:
    _save_folder()
    source = tmp_path / "copy.json"
    original = ('{"note":"' + SYNTHETIC_SECRET + '"}\n').encode("utf-8")
    source.write_bytes(original)
    backup = tmp_path / "backups"
    fake = FakeDrive(FOLDER_ID)
    output = StringIO()
    answers = iter(("FinanceTracker test", "one", "two"))
    before = _stamp()

    def prompt(_question: str) -> str:
        return next(answers)

    def opener(folder_id: str) -> GoogleDriveAdapter:
        assert folder_id == FOLDER_ID
        return _adapter(fake)

    code = run_session_check(
        ["migrate", "--source", str(source), "--backup-dir", str(backup)],
        prompt=prompt,
        output=output,
        opener=opener,
        sleep=lambda _delay: None,
    )
    text = output.getvalue()
    assert code == 1
    assert "did not match" in text
    assert SYNTHETIC_SECRET not in text
    assert source.read_bytes() == original
    assert not backup.exists()
    assert fake.calls == [] or all(call["method"] != "create" for call in fake.calls)
    assert load_local_config().cloud_mode is False
    assert _stamp() == before
    assert not (tmp_path / "not-written.json").exists()
