"""Backup list. The session decides what can be restored; this dialog only shows it."""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from finance.snapshots import SOURCE_DRIVE, RecoveryCatalog, RecoveryItem, recovery_label


def _when(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _count(value: int | None) -> str:
    if value is None:
        return ""
    return str(value)


def _where(item: RecoveryItem) -> str:
    if item.source == SOURCE_DRIVE:
        return "Drive"
    return "This computer"


def _note(item: RecoveryItem) -> str:
    parts: list[str] = []
    if item.current:
        parts.append("Current")
    if item.conflict:
        parts.append("Conflicting save")
    return ", ".join(parts)


class RecoveryDialog(QDialog):
    """Lists snapshots. Restore returns the selected key; it does not upload."""

    def __init__(
        self,
        catalog: RecoveryCatalog,
        *,
        can_restore: bool,
        blocked_reason: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._can_restore = can_restore
        self.selected_key: str | None = None
        self.selected_summary = ""
        self.setWindowTitle("Backups")
        self.resize(860, 520)
        self._build(blocked_reason)

    def _build(self, blocked_reason: str) -> None:
        root = QVBoxLayout(self)
        intro = QLabel(
            "Restoring uploads a new snapshot with the selected data. "
            "The current snapshot stays in the folder as a backup."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        if self._catalog.message.strip():
            notice = QLabel(self._catalog.message.strip())
            notice.setWordWrap(True)
            notice.setObjectName("backupNotice")
            root.addWidget(notice)
        if blocked_reason.strip():
            blocked = QLabel(blocked_reason.strip())
            blocked.setWordWrap(True)
            blocked.setObjectName("backupBlocked")
            blocked.setStyleSheet("color: #9a3412;")
            root.addWidget(blocked)

        root.addWidget(QLabel("Can be restored"))
        self._valid = self._table(
            ["Generation", "When", "Saved by", "Spending", "Income", "Where", "Note"]
        )
        self._fill_valid()
        self._valid.itemSelectionChanged.connect(self._sync_restore_button)
        self._valid.itemDoubleClicked.connect(lambda _item: self._choose())
        root.addWidget(self._valid, 1)

        root.addWidget(QLabel("Cannot be restored"))
        self._unusable = self._table(["Generation", "When", "Saved by", "Where", "Reason"])
        self._unusable.setSelectionMode(QTableWidget.NoSelection)
        self._fill_unusable()
        root.addWidget(self._unusable, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._restore_btn = QPushButton("Restore")
        self._restore_btn.setObjectName("restoreBackup")
        self._restore_btn.clicked.connect(self._choose)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(self._restore_btn)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self._sync_restore_button()

    def _table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        return table

    def _fill_valid(self) -> None:
        self._valid.setRowCount(len(self._catalog.valid))
        for row, item in enumerate(self._catalog.valid):
            values = [
                "" if item.generation is None else str(item.generation),
                _when(item.created_at_utc),
                item.saved_by,
                _count(item.spending_count),
                _count(item.income_count),
                _where(item),
                _note(item),
            ]
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column == 0:
                    cell.setData(Qt.UserRole, item.key)
                self._valid.setItem(row, column, cell)
        if not self._catalog.valid:
            self._valid.setRowCount(1)
            self._valid.setItem(0, 0, QTableWidgetItem("None"))

    def _fill_unusable(self) -> None:
        if not self._catalog.unusable:
            self._unusable.setRowCount(1)
            self._unusable.setItem(0, 0, QTableWidgetItem("None"))
            return
        self._unusable.setRowCount(len(self._catalog.unusable))
        for row, item in enumerate(self._catalog.unusable):
            values = [
                "" if item.generation is None else str(item.generation),
                _when(item.created_at_utc),
                item.saved_by,
                _where(item),
                item.reason if not item.conflict else f"{item.reason} Conflicting save.".strip(),
            ]
            for column, text in enumerate(values):
                self._unusable.setItem(row, column, QTableWidgetItem(text))

    def _selected_key(self) -> str | None:
        row = self._valid.currentRow()
        if row < 0 or row >= len(self._catalog.valid):
            return None
        cell = self._valid.item(row, 0)
        if cell is None:
            return None
        key = cell.data(Qt.UserRole)
        if not isinstance(key, str) or not key:
            return None
        return key

    def _selected_item(self) -> RecoveryItem | None:
        key = self._selected_key()
        if key is None:
            return None
        for item in self._catalog.valid:
            if item.key == key:
                return item
        return None

    def _sync_restore_button(self) -> None:
        item = self._selected_item()
        enabled = (
            self._can_restore
            and item is not None
            and not item.current
            and not item.invalid
        )
        self._restore_btn.setEnabled(enabled)

    def _choose(self) -> None:
        item = self._selected_item()
        if item is None or item.current or item.invalid or not self._can_restore:
            return
        self.selected_key = item.key
        self.selected_summary = recovery_label(item)
        self.accept()
