"""The ignore rules cover secrets and finance files. Fixtures under tests/ stay tracked."""

from __future__ import annotations

from pathlib import Path


def test_gitignore_covers_local_secrets_and_finance_files() -> None:
    text = Path(".gitignore").read_text(encoding="utf-8")
    required = (
        "finance_data.json",
        "finance_data-*.json",
        "*.tmp",
        "*.enc",
        "config.json",
        "known_valid.json",
        "lock_session.json",
        "encrypted_snapshots/",
        "client_secret*.json",
        "credentials.json",
        "token.json",
        "google_oauth_client.json",
    )
    rules = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    missing = [pattern for pattern in required if pattern not in rules]
    assert missing == []
    assert "tests/" not in rules
    assert not any(rule.startswith("tests") for rule in rules)
