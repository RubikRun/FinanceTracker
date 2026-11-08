"""Main application window: person tabs, period selector, summary and tables."""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from finance.config import (
    DISPLAY_DATE_FORMAT,
    USER_BORIS,
    USER_MILENA,
    VIEW_BOTH_LABEL,
    format_amount,
)
from finance.session import SessionError
from finance.store import FinanceStore, month_range

from .add_dialog import AddDialog
from .cloud_status import CloudView
from .spending_chart_dialog import SpendingChartDialog

# Tab indices — keep in sync with the order tabs are added in `_build_ui`.
_TAB_BORIS = 0
_TAB_MILENA = 1
_TAB_BOTH = 2

# Column index of the "Who" column in both tables (hidden in personal modes).
_WHO_COLUMN = 1

_ACTIVITY_EVENTS = (
    QEvent.KeyPress,
    QEvent.MouseButtonPress,
    QEvent.MouseButtonDblClick,
    QEvent.Wheel,
    QEvent.MouseMove,
    QEvent.Shortcut,
)

_TONE_STYLE = {
    "ok": "font-size: 13px; font-weight: bold; color: #166534;",
    "pending": "font-size: 13px; font-weight: bold; color: #1f2937;",
    "warn": (
        "font-size: 13px; font-weight: bold; color: #9a3412; "
        "background: #fff7ed; padding: 2px 6px;"
    ),
    "muted": "font-size: 13px; font-weight: bold; color: #1f2937;",
}


class _NoFocusDelegate(QStyledItemDelegate):
    """Paints cells without the focus rectangle.

    The default style draws a thin focus outline on the current cell (visible
    as a small line on the cell's edge even when the whole row is selected).
    Stripping the focus state before painting removes only that outline while
    leaving row selection and keyboard navigation intact.
    """

    def paint(self, painter, option, index):
        option.state &= ~QStyle.State_HasFocus
        super().paint(painter, option, index)


class _ActivityFilter(QObject):
    """Forwards key and mouse events to the session without calling Drive itself."""

    def __init__(self, host) -> None:
        super().__init__()
        self._host = host

    def eventFilter(self, watched, event) -> bool:
        del watched
        if event.type() in _ACTIVITY_EVENTS:
            self._host.note_activity()
        return False


class MainWindow(QMainWindow):
    def __init__(self, store: FinanceStore, host=None, poll_ms: int = 10_000) -> None:
        super().__init__()
        self.store = store
        self._host = host
        self._poll_ms = poll_ms
        # Active person filter: a user name, or None for the combined view.
        self._view_user: Optional[str] = USER_BORIS
        self._cloud_view: CloudView | None = None
        self._quit_accepted = False
        self._close_started = False
        self._shut_down = False
        self._add_dialog_factory = AddDialog
        self._delete_confirmation = None
        self._activity: _ActivityFilter | None = None
        self._poll_timer: QTimer | None = None
        self.setWindowTitle("Finance Tracker")
        self.resize(860, 720 if host is not None else 660)
        self._build_ui()
        if host is None:
            self.sync_panel.hide()
            self.backups_btn.hide()
            self._apply_mode_ui()
            self._reload_periods()
            return
        setter = getattr(host.prompts, "set_parent", None)
        if setter is not None:
            setter(self)
        self._install_cloud_runtime()
        host.attach(self)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Person tabs (primary navigation) ---
        self.mode_tabs = QTabBar()
        self.mode_tabs.setExpanding(False)
        self.mode_tabs.setDrawBase(False)
        self.mode_tabs.addTab(USER_BORIS)
        self.mode_tabs.addTab(USER_MILENA)
        # Qt uses '&' as a mnemonic marker in tab text; '&&' shows a literal '&'.
        self.mode_tabs.addTab(VIEW_BOTH_LABEL.replace("&", "&&"))
        self.mode_tabs.currentChanged.connect(self._on_mode_changed)
        root.addWidget(self.mode_tabs)

        self.sync_panel = QWidget()
        self.sync_panel.setObjectName("syncPanel")
        sync_layout = QVBoxLayout(self.sync_panel)
        sync_layout.setContentsMargins(0, 4, 0, 4)
        sync_row = QHBoxLayout()
        self.sync_label = QLabel("")
        self.sync_label.setObjectName("syncStatus")
        self.sync_action = QPushButton("")
        self.sync_action.setObjectName("syncAction")
        self.sync_action.clicked.connect(self._on_sync_action)
        self.sync_action.hide()
        sync_row.addWidget(self.sync_label)
        sync_row.addWidget(self.sync_action)
        sync_row.addStretch(1)
        sync_layout.addLayout(sync_row)
        self.sync_detail = QLabel("")
        self.sync_detail.setObjectName("syncDetail")
        self.sync_detail.setWordWrap(True)
        self.sync_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.sync_detail.setStyleSheet("color: #4b5563;")
        sync_layout.addWidget(self.sync_detail)
        root.addWidget(self.sync_panel)

        # --- Top bar: Add button + period selector ---
        top = QHBoxLayout()
        self.add_btn = QPushButton("+ Add")
        self.add_btn.clicked.connect(self._on_add)
        top.addWidget(self.add_btn)

        # Shown in the combined view instead of the Add button.
        self.view_only_label = QLabel(
            "View only — switch to Boris or Milena to add a record."
        )
        self.view_only_label.setStyleSheet("color: #666;")
        top.addWidget(self.view_only_label)

        top.addSpacing(20)
        top.addWidget(QLabel("Period:"))
        self.period_combo = QComboBox()
        self.period_combo.setMinimumWidth(160)
        self.period_combo.currentIndexChanged.connect(self._refresh_view)
        top.addWidget(self.period_combo)

        self.chart_btn = QPushButton("Spending chart")
        self.chart_btn.clicked.connect(self._on_spending_chart)
        top.addWidget(self.chart_btn)

        self.backups_btn = QPushButton("Backups")
        self.backups_btn.clicked.connect(self._on_backups)
        top.addWidget(self.backups_btn)

        top.addStretch(1)
        root.addLayout(top)

        # --- Summary line ---
        summary = QHBoxLayout()
        self.earned_label = QLabel()
        self.spent_label = QLabel()
        self.gap_label = QLabel()
        self.earned_label.setStyleSheet("font-size: 14px;")
        self.spent_label.setStyleSheet("font-size: 14px;")
        summary.addWidget(self.earned_label)
        summary.addStretch(1)
        summary.addWidget(self.spent_label)
        summary.addStretch(1)
        summary.addWidget(self.gap_label)
        root.addLayout(summary)

        # --- Tables (resizable split) ---
        splitter = QSplitter(Qt.Vertical)

        spending_box = QGroupBox("Spending")
        spending_layout = QVBoxLayout(spending_box)
        # Columns: Date, Who, Category, Amount, Note  (Who hidden in personal modes)
        self.spending_table = QTableWidget(0, 5)
        self.spending_table.setHorizontalHeaderLabels(
            ["Date", "Who", "Category", "Amount", "Note"]
        )
        self._setup_table(self.spending_table, [90, 80, 130, 110, 200])
        self.spending_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.spending_table.customContextMenuRequested.connect(
            lambda pos: self._on_table_context_menu(self.spending_table, pos)
        )
        spending_layout.addWidget(self.spending_table)
        splitter.addWidget(spending_box)

        income_box = QGroupBox("Income")
        income_layout = QVBoxLayout(income_box)
        # Columns: Date, Who, Amount, Note  (Who hidden in personal modes)
        self.income_table = QTableWidget(0, 4)
        self.income_table.setHorizontalHeaderLabels(["Date", "Who", "Amount", "Note"])
        self._setup_table(self.income_table, [90, 80, 110, 200])
        self.income_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.income_table.customContextMenuRequested.connect(
            lambda pos: self._on_table_context_menu(self.income_table, pos)
        )
        income_layout.addWidget(self.income_table)
        splitter.addWidget(income_box)

        root.addWidget(splitter, 1)

    @staticmethod
    def _setup_table(table: QTableWidget, column_widths: list[int]) -> None:
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setItemDelegate(_NoFocusDelegate(table))  # no focus outline on cells
        # Use a neutral grey for the selected row instead of the system
        # highlight colour (which can be a jarring accent like orange).
        table.setStyleSheet(
            "QTableView::item:selected { background-color: #454545; color: white; }"
        )
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        # Interactive mode lets the user resize columns by dragging the
        # boundary between two headers. The last column (Note) still stretches
        # to fill leftover width so the table has no awkward empty gap.
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        for column, width in enumerate(column_widths):
            table.setColumnWidth(column, width)

    # ------------------------------------------------------------------ #
    # Mode (Boris / Milena / Both)
    # ------------------------------------------------------------------ #
    def _on_mode_changed(self, index: int) -> None:
        if index == _TAB_BORIS:
            self._view_user = USER_BORIS
        elif index == _TAB_MILENA:
            self._view_user = USER_MILENA
        else:
            self._view_user = None  # combined view

        # Keep the selected month when switching tabs if it still has data.
        previous_period = self.period_combo.currentData()
        self._apply_mode_ui()
        self._reload_periods(prefer=previous_period)

    def _apply_mode_ui(self) -> None:
        """Show/hide Add vs view-only hint, and the Who column."""
        is_combined = self._view_user is None
        edits = self._host is None or (
            self._cloud_view is not None and self._cloud_view.edits_offered
        )
        saving = self._cloud_view is not None and self._cloud_view.saving
        show_add = not is_combined and (edits or saving)
        self.add_btn.setVisible(show_add)
        self.add_btn.setEnabled(edits and not is_combined)
        self.view_only_label.setVisible(is_combined)
        if self._host is None:
            self.backups_btn.hide()
        else:
            self.backups_btn.show()
            self.backups_btn.setEnabled(not saving)

        # In a personal mode the Who column is redundant — every row is that person.
        # In the combined view it's essential so you can tell records apart.
        self.spending_table.setColumnHidden(_WHO_COLUMN, not is_combined)
        self.income_table.setColumnHidden(_WHO_COLUMN, not is_combined)

        if is_combined:
            self.setWindowTitle(f"Finance Tracker — {VIEW_BOTH_LABEL}")
        else:
            self.setWindowTitle(f"Finance Tracker — {self._view_user}")
            self.add_btn.setText(f"+ Add for {self._view_user}")

    # ------------------------------------------------------------------ #
    # Data binding
    # ------------------------------------------------------------------ #
    @staticmethod
    def _period_tuple(data) -> Optional[tuple[int, int]]:
        """Normalize combo item data to `(year, month)`.

        Qt may round-trip the Python tuple as a list (or other sequence), so
        callers should not rely on `QComboBox.findData` matching a tuple.
        """
        if data is None:
            return None
        try:
            year, month = data
            return (int(year), int(month))
        except (TypeError, ValueError):
            return None

    def _find_period_index(self, period) -> int:
        want = self._period_tuple(period)
        if want is None:
            return -1
        for i in range(self.period_combo.count()):
            if self._period_tuple(self.period_combo.itemData(i)) == want:
                return i
        return -1

    def _reload_periods(self, prefer: Optional[tuple[int, int]] = None) -> None:
        """Rebuild the period dropdown from months that have records in this view."""
        self.period_combo.blockSignals(True)
        self.period_combo.clear()
        for year, month in self.store.available_months(self._view_user):
            label = f"{calendar.month_name[month]} {year}"
            self.period_combo.addItem(label, (year, month))
        self.period_combo.blockSignals(False)

        if self.period_combo.count() == 0:
            self._refresh_view()
            return

        index = 0
        if prefer is not None:
            found = self._find_period_index(prefer)
            if found >= 0:
                index = found
        self.period_combo.setCurrentIndex(index)
        self._refresh_view()

    def _refresh_view(self) -> None:
        period = self._period_tuple(self.period_combo.currentData())
        self.chart_btn.setEnabled(period is not None)
        if period is None:
            self._fill_spending([])
            self._fill_income([])
            self._update_summary(Decimal("0"), Decimal("0"))
            return

        year, month = period
        start, end = month_range(year, month)
        user = self._view_user
        spending = sorted(
            self.store.spending_between(start, end, user), key=lambda r: (r.date, r.id)
        )
        income = sorted(
            self.store.income_between(start, end, user), key=lambda r: (r.date, r.id)
        )
        self._fill_spending(spending)
        self._fill_income(income)
        self._update_summary(
            self.store.total_earned(start, end, user),
            self.store.total_spent(start, end, user),
        )

    def _fill_spending(self, records) -> None:
        self.spending_table.setRowCount(len(records))
        for row, record in enumerate(records):
            date_item = QTableWidgetItem(record.date.strftime(DISPLAY_DATE_FORMAT))
            date_item.setData(Qt.UserRole, record.id)  # record id for edit
            self.spending_table.setItem(row, 0, date_item)
            self.spending_table.setItem(row, 1, QTableWidgetItem(record.user))
            self.spending_table.setItem(row, 2, QTableWidgetItem(record.category))
            self.spending_table.setItem(
                row, 3, QTableWidgetItem(format_amount(record.amount))
            )
            self.spending_table.setItem(row, 4, QTableWidgetItem(record.note))

    def _fill_income(self, records) -> None:
        self.income_table.setRowCount(len(records))
        for row, record in enumerate(records):
            date_item = QTableWidgetItem(record.date.strftime(DISPLAY_DATE_FORMAT))
            date_item.setData(Qt.UserRole, record.id)
            self.income_table.setItem(row, 0, date_item)
            self.income_table.setItem(row, 1, QTableWidgetItem(record.user))
            self.income_table.setItem(
                row, 2, QTableWidgetItem(format_amount(record.amount))
            )
            self.income_table.setItem(row, 3, QTableWidgetItem(record.note))

    def _update_summary(self, earned: Decimal, spent: Decimal) -> None:
        gap = earned - spent
        self.earned_label.setText(f"Earned: {format_amount(earned)}")
        self.spent_label.setText(f"Spent: {format_amount(spent)}")
        color = "green" if gap >= 0 else "red"
        self.gap_label.setText(f"Gap: {format_amount(gap)}")
        self.gap_label.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {color};"
        )

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def _on_add(self) -> None:
        self._with_edit(self._add_from_dialog)

    def _add_from_dialog(self) -> None:
        if self._add_record(save=self._host is None) and self._host is not None:
            self._host.after_local_change()

    def _add_record(self, *, save: bool) -> bool:
        if self._view_user is None:
            return False  # Combined view is view-only (button is hidden, but be safe).

        period = self._period_tuple(self.period_combo.currentData())
        default_date = date(period[0], period[1], 1) if period else None
        dialog = self._add_dialog_factory(
            for_user=self._view_user, parent=self, default_date=default_date
        )
        if not dialog.exec():
            return False  # Cancelled

        data = dialog.result_data
        try:
            if data["type"] == "spending":
                self.store.add_spending(
                    amount=data["amount"],
                    category=data["category"],
                    on_date=data["date"],
                    user=data["user"],
                    note=data["note"],
                )
            else:
                self.store.add_income(
                    amount=data["amount"],
                    on_date=data["date"],
                    user=data["user"],
                    note=data["note"],
                )
        except SessionError as exc:
            QMessageBox.warning(self, "Finance Tracker", str(exc))
            return False
        if save:
            self.store.save()

        # A new record may introduce a new month; rebuild and then jump to the
        # month of the record we just added so the user sees it immediately.
        target = (data["date"].year, data["date"].month)
        self._reload_periods(prefer=target)
        return True

    def _on_backups(self) -> None:
        if self._host is not None:
            self._host.open_backups()

    def _on_spending_chart(self) -> None:
        period = self._period_tuple(self.period_combo.currentData())
        if period is None:
            return

        dialog = SpendingChartDialog(
            store=self.store,
            initial_period=period,
            initial_user=self._view_user,
            parent=self,
        )
        dialog.exec()

    def _on_table_context_menu(self, table: QTableWidget, pos) -> None:
        index = table.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        id_item = table.item(row, 0)
        if id_item is None:
            return
        record_id = id_item.data(Qt.UserRole)
        if record_id is None:
            return

        table.selectRow(row)
        if self._cloud_view is not None and not self._cloud_view.edits_offered:
            return
        menu = QMenu(self)
        edit_action = menu.addAction("Edit")
        delete_action = menu.addAction("Delete")
        chosen = menu.exec(table.viewport().mapToGlobal(pos))
        if chosen == edit_action:
            self._on_edit(int(record_id))
        elif chosen == delete_action:
            self._on_delete(int(record_id))

    def _on_edit(self, record_id: int) -> None:
        self._with_edit(lambda: self._edit_record(record_id))

    def _edit_record(self, record_id: int) -> None:
        spending = self.store.get_spending(record_id)
        income = self.store.get_income(record_id)
        record = spending if spending is not None else income
        if record is None:
            if self._host is not None:
                QMessageBox.information(
                    self, "Finance Tracker", "That record is not in the current data."
                )
            return

        initial = {
            "type": "spending" if spending is not None else "income",
            "amount": record.amount,
            "date": record.date,
            "note": record.note,
        }
        if spending is not None:
            initial["category"] = spending.category

        old_period = (record.date.year, record.date.month)

        dialog = self._add_dialog_factory(for_user=record.user, parent=self, initial=initial)
        if not dialog.exec():
            return

        data = dialog.result_data
        try:
            self.store.update_record(
                record_id,
                record_type=data["type"],
                amount=data["amount"],
                on_date=data["date"],
                note=data["note"],
                category=data.get("category", ""),
            )
        except SessionError as exc:
            QMessageBox.warning(self, "Finance Tracker", str(exc))
            return
        save = self._host is None
        if save:
            self.store.save()

        new_period = (data["date"].year, data["date"].month)
        if new_period == old_period:
            # Stay on the month the user was viewing; only the row contents change.
            self._refresh_view()
        else:
            # Date moved to another month (the old month may now be empty).
            self._reload_periods(prefer=new_period)
        if self._host is not None:
            self._host.after_local_change()

    def _on_delete(self, record_id: int) -> None:
        self._with_edit(lambda: self._delete_record(record_id))

    def _delete_record(self, record_id: int) -> None:
        spending = self.store.get_spending(record_id)
        income = self.store.get_income(record_id)
        record = spending if spending is not None else income
        if record is None:
            if self._host is not None:
                QMessageBox.information(
                    self, "Finance Tracker", "That record is not in the current data."
                )
            return

        when = record.date.strftime(DISPLAY_DATE_FORMAT)
        amount = format_amount(record.amount)
        if spending is not None:
            kind = "spending"
            detail = f"{amount} ({spending.category}) on {when}"
        else:
            kind = "income"
            detail = f"{amount} on {when}"

        question = f"Delete this {kind} for {record.user}?\n{detail}"
        if self._delete_confirmation is not None:
            confirmed = self._delete_confirmation(question)
        else:
            answer = QMessageBox.question(
                self,
                "Delete record",
                question,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            confirmed = answer == QMessageBox.Yes
        if not confirmed:
            return

        period = self._period_tuple(self.period_combo.currentData())
        try:
            self.store.delete_record(record_id)
        except SessionError as exc:
            QMessageBox.warning(self, "Finance Tracker", str(exc))
            return
        if self._host is None:
            self.store.save()

        if period is not None and period in self.store.available_months(self._view_user):
            self._refresh_view()
        else:
            self._reload_periods(prefer=period)
        if self._host is not None:
            self._host.after_local_change()

    def _with_edit(self, run) -> None:
        if self._host is None:
            run()
            return
        self._host.begin_mutation(run)

    def apply_view(self, view: CloudView, *, refresh_records: bool) -> None:
        self._cloud_view = view
        if self._host is not None and self._host.session.store is not None:
            self.store = self._host.session.store
        self._show_sync(view)
        self._apply_mode_ui()
        if refresh_records:
            current = self._period_tuple(self.period_combo.currentData())
            self._reload_periods(prefer=current)

    def quit_accepted(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._quit_accepted = True
        QTimer.singleShot(0, self.close)

    def close_cancelled(self) -> None:
        self._close_started = False

    def accepts_quit(self) -> bool:
        return self._host is None or self._quit_accepted

    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        if self._poll_timer is not None:
            self._poll_timer.stop()
        if self._activity is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self._activity)
            self._activity = None
        if self._host is not None:
            self._host.shutdown()

    def handle_commit_data(self, manager) -> None:
        """Windows shutdown and logoff. A prompt is possible only when the OS allows it."""
        if self._host is None or self._shut_down or self._quit_accepted:
            return
        allows = False
        try:
            allows = bool(manager.allowsInteraction())
        except Exception:
            allows = False
        if not allows:
            self._host.release_if_idle()
            return
        if self._host.close_synchronously():
            self._quit_accepted = True
            return
        try:
            manager.cancel()
        except Exception:
            return

    def closeEvent(self, event) -> None:
        if self._host is None or self._quit_accepted:
            event.accept()
            return
        event.ignore()
        if self._close_started:
            return
        self._close_started = True
        self._host.request_close()

    def _install_cloud_runtime(self) -> None:
        app = QApplication.instance()
        if app is not None:
            self._activity = _ActivityFilter(self._host)
            app.installEventFilter(self._activity)
        if self._poll_ms > 0:
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(self._poll_ms)
            self._poll_timer.timeout.connect(self._poll_cloud)
            self._poll_timer.start()

    def _poll_cloud(self) -> None:
        if self._host is not None and not self._shut_down:
            self._host.poll()

    def _on_sync_action(self) -> None:
        if self._host is not None:
            self._host.primary_action()

    def _show_sync(self, view: CloudView) -> None:
        self.sync_panel.show()
        self.sync_label.setText(view.title)
        self.sync_label.setStyleSheet(_TONE_STYLE.get(view.tone, _TONE_STYLE["muted"]))
        self.sync_detail.setText(view.detail)
        if view.action_label:
            self.sync_action.setText(view.action_label)
            self.sync_action.show()
            self.sync_action.setEnabled(not view.saving)
        else:
            self.sync_action.hide()
