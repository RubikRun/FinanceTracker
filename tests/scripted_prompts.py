"""Scripted stand-in for the cloud window's questions."""

from __future__ import annotations


class ScriptedPrompts:
    """Returns canned answers. An unexpected choice fails the test."""

    def __init__(
        self,
        *,
        choices: list | None = None,
        passphrases: list | None = None,
        pairs: list | None = None,
        directories: list | None = None,
        confirms: list | None = None,
    ) -> None:
        self._choices = list(choices or [])
        self._passphrases = list(passphrases or [])
        self._pairs = list(pairs or [])
        self._directories = list(directories or [])
        self._confirms = list(confirms if confirms is not None else [True])
        self.warnings: list[tuple[str, str]] = []
        self.infos: list[tuple[str, str]] = []
        self.asked: list[tuple] = []

    def warn(self, title: str, message: str) -> None:
        self.warnings.append((title, message))

    def info(self, title: str, message: str) -> None:
        self.infos.append((title, message))

    def confirm(self, title: str, message: str, *, default_yes: bool = False) -> bool:
        del default_yes
        self.asked.append(("confirm", title, message))
        if not self._confirms:
            return False
        return bool(self._confirms.pop(0))

    def choose(self, title: str, message: str, options, *, warning: bool = False):
        self.asked.append(("choose", title, message, tuple(options), warning))
        if not self._choices:
            raise AssertionError(f"unexpected choice: {title}")
        return self._choices.pop(0)

    def passphrase(self, intro: str) -> str | None:
        self.asked.append(("passphrase", intro))
        if not self._passphrases:
            return None
        value = self._passphrases.pop(0)
        return value

    def new_passphrase(self, intro: str):
        self.asked.append(("new_passphrase", intro))
        if not self._pairs:
            return None
        return self._pairs.pop(0)

    def unlock(self):
        phrase = self.passphrase("unlock")
        if not phrase:
            return None
        return ("Boris", phrase)

    def directory(self, intro: str):
        self.asked.append(("directory", intro))
        if not self._directories:
            return None
        return self._directories.pop(0)

    def working(self, text: str):
        del text
        return None

    def close_working(self, dialog) -> None:
        del dialog

    def set_parent(self, parent) -> None:
        del parent
