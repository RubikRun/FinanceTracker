"""The CLI stops before the legacy file once cloud mode is on."""

from __future__ import annotations

from pathlib import Path

import cli
from finance.profile import (
    config_file,
    enable_cloud_mode,
    ensure_machine_identity,
    ensure_profile_directories,
)
from finance.store import FinanceStore


def _enable() -> None:
    config = ensure_machine_identity()
    config.drive_folder_id = "folder-id-0123456789abc"
    config.lock_file_id = "lock-file-id-0123456789"
    enable_cloud_mode(config, verified_generation=1)


def _stamp(path: Path) -> tuple[bool, bytes | None]:
    if not path.exists():
        return False, None
    return True, path.read_bytes()


def test_cloud_mode_is_checked_before_the_store_is_loaded() -> None:
    text = Path("cli.py").read_text(encoding="utf-8")
    assert text.index("cloud_mode") < text.index("FinanceStore.load")


def test_every_command_stops_before_the_legacy_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    target = tmp_path / "finance_data.json"
    target.write_text('{"version": 2, "note": "leave-this"}\n', encoding="utf-8")
    before = _stamp(target)
    monkeypatch.setattr(cli, "default_data_path", lambda: target)
    _enable()

    commands = [
        [],
        ["--help"],
        ["add-spending", "Boris", "1.00", "Misc", "2026-10-01"],
        ["add-income", "Milena", "2.00", "2026-10-02"],
        ["summary", "2026", "10"],
        ["list", "2026", "10", "--user", "Boris"],
    ]
    for argv in commands:
        assert cli.main(argv) == 1
        captured = capsys.readouterr()
        assert "does not support synced data yet" in captured.err
        assert "leave-this" not in captured.out
        assert "leave-this" not in captured.err
        assert _stamp(target) == before


def test_without_cloud_mode_the_cli_still_uses_the_local_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    target = tmp_path / "finance_data.json"
    monkeypatch.setattr(cli, "default_data_path", lambda: target)

    assert cli.main(["summary", "2026", "10"]) == 0
    assert "Earned:" in capsys.readouterr().out
    assert not target.exists()

    assert cli.main(["add-spending", "Boris", "1.50", "Misc", "2026-10-03"]) == 0
    capsys.readouterr()
    loaded = FinanceStore.load(target)
    assert len(loaded.spending) == 1
    assert loaded.spending[0].user == "Boris"


def test_unreadable_config_does_not_open_the_legacy_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    ensure_profile_directories()
    config_file().write_text("{", encoding="utf-8")
    target = tmp_path / "finance_data.json"
    monkeypatch.setattr(cli, "default_data_path", lambda: target)

    assert cli.main(["summary", "2026", "10"]) == 1
    captured = capsys.readouterr()
    assert "not read or written" in captured.err
    assert not target.exists()
