"""Qt dialogs for the cloud window. The passphrase is not copied or logged."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from finance.config import USER_BORIS, USER_MILENA


class QtCloudPrompts:
    """Modal questions. The passphrase fields are cleared before the dialog closes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        self._parent = parent

    def set_parent(self, parent: QWidget | None) -> None:
        self._parent = parent

    def warn(self, title: str, message: str) -> None:
        QMessageBox.warning(self._parent, title, message)

    def info(self, title: str, message: str) -> None:
        QMessageBox.information(self._parent, title, message)

    def confirm(self, title: str, message: str, *, default_yes: bool = False) -> bool:
        default = QMessageBox.Yes if default_yes else QMessageBox.No
        answer = QMessageBox.question(
            self._parent,
            title,
            message,
            QMessageBox.Yes | QMessageBox.No,
            default,
        )
        return answer == QMessageBox.Yes

    def choose(
        self,
        title: str,
        message: str,
        options: tuple[tuple[str, str], ...],
        *,
        warning: bool = False,
    ) -> str | None:
        box = QMessageBox(self._parent)
        box.setWindowTitle(title)
        box.setText(message)
        box.setIcon(QMessageBox.Warning if warning else QMessageBox.Information)
        mapping: dict[str, QPushButton] = {}
        for choice_id, label in options:
            mapping[choice_id] = box.addButton(label, QMessageBox.ActionRole)
        box.exec()
        clicked = box.clickedButton()
        for choice_id, button in mapping.items():
            if clicked == button:
                return choice_id
        return None

    def passphrase(self, intro: str) -> str | None:
        dialog = _PassphraseDialog(self._parent, intro=intro, confirm=False, ask_owner=False)
        return _one(dialog)

    def new_passphrase(self, intro: str) -> tuple[str, str] | None:
        dialog = _PassphraseDialog(self._parent, intro=intro, confirm=True, ask_owner=False)
        return dialog.take_pair()

    def unlock(self) -> tuple[str, str] | None:
        dialog = _PassphraseDialog(
            self._parent,
            intro=(
                "Who is using this computer, and what is the finance passphrase? "
                "The passphrase is not saved."
            ),
            confirm=False,
            ask_owner=True,
        )
        pair = dialog.take_pair()
        if pair is None:
            return None
        phrase, owner = pair
        return owner, phrase

    def directory(self, intro: str) -> Path | None:
        self.info("Plaintext backup", intro)
        chosen = QFileDialog.getExistingDirectory(
            self._parent,
            "Folder for the plaintext backup",
        )
        if not chosen:
            return None
        return Path(chosen)

    def working(self, text: str) -> QDialog:
        dialog = QDialog(self._parent)
        dialog.setWindowTitle("Finance Tracker")
        dialog.setWindowFlag(Qt.WindowCloseButtonHint, False)
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        dialog.resize(440, 120)
        dialog.show()
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        return dialog

    def close_working(self, dialog: QDialog | None) -> None:
        if dialog is not None:
            dialog.close()


def _one(dialog: "_PassphraseDialog") -> str | None:
    taken = dialog.take_pair()
    if taken is None:
        return None
    phrase, _owner = taken
    return phrase


class _PassphraseDialog(QDialog):
    """Asks for a passphrase. Widgets are cleared as soon as the value is accepted."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        intro: str,
        confirm: bool,
        ask_owner: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Finance Tracker")
        self.setModal(True)
        self._confirm = confirm
        self._ask_owner = ask_owner
        self._pair: tuple[str, str] | None = None
        self._build(intro)

    def take_pair(self) -> tuple[str, str] | None:
        if self._pair is None and self.exec() != QDialog.Accepted:
            self._wipe()
            return None
        pair = self._pair
        self._pair = None
        self._wipe()
        return pair

    def submit_for_test(self) -> None:
        """The OK button uses this. Tests call it without showing the dialog."""
        self._submit()

    def _build(self, intro: str) -> None:
        layout = QVBoxLayout(self)
        text = QLabel(intro)
        text.setWordWrap(True)
        layout.addWidget(text)

        if self._ask_owner:
            row = QHBoxLayout()
            self._boris = QRadioButton(USER_BORIS)
            self._milena = QRadioButton(USER_MILENA)
            self._boris.setChecked(True)
            group = QButtonGroup(self)
            group.addButton(self._boris)
            group.addButton(self._milena)
            row.addWidget(self._boris)
            row.addWidget(self._milena)
            row.addStretch(1)
            layout.addLayout(row)
        else:
            self._boris = None
            self._milena = None

        self._phrase = _password_field()
        layout.addWidget(QLabel("Passphrase"))
        layout.addWidget(self._phrase)
        if self._confirm:
            self._again = _password_field()
            layout.addWidget(QLabel("Passphrase again"))
            layout.addWidget(self._again)
        else:
            self._again = None

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #9a3412;")
        self._error.hide()
        layout.addWidget(self._error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Continue")
        ok.clicked.connect(self._submit)
        ok.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
        self.resize(460, 220)
        self._phrase.setFocus()

    def _submit(self) -> None:
        first = self._phrase.text()
        second = self._again.text() if self._again is not None else ""
        if first == "":
            self._show_error("A passphrase is required.")
            self._wipe()
            return
        if self._again is not None and first != second:
            self._show_error("The two passphrases did not match.")
            self._wipe()
            return
        owner = USER_BORIS
        if self._milena is not None and self._milena.isChecked():
            owner = USER_MILENA
        self._pair = (first, owner if self._ask_owner else second)
        self._wipe()
        self.accept()

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()

    def _wipe(self) -> None:
        self._phrase.clear()
        if self._again is not None:
            self._again.clear()

    def reject(self) -> None:
        self._pair = None
        self._wipe()
        super().reject()


def _password_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.Password)
    field.setDragEnabled(False)
    return field
