"""Main application window: person tabs, period selector, summary and tables."""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
from finance.store import FinanceStore, month_range

from .add_dialog import AddDialog

# Tab indices — keep in sync with the order tabs are added in `_build_ui`.
_TAB_BORIS = 0
_TAB_MILENA = 1
_TAB_BOTH = 2

# Column index of the "Who" column in both tables (hidden in personal modes).
_WHO_COLUMN = 1


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


class MainWindow(QMainWindow):
    def __init__(self, store: FinanceStore) -> None:
        super().__init__()
        self.store = store
        # Active person filter: a user name, or None for the combined view.
        self._view_user: Optional[str] = USER_BORIS
        self.setWindowTitle("Finance Tracker")
        self.resize(860, 660)
        self._build_ui()
        self._apply_mode_ui()
        self._reload_periods()

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
        self.add_btn.setVisible(not is_combined)
        self.view_only_label.setVisible(is_combined)

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
        if self._view_user is None:
            return  # Combined view is view-only (button is hidden, but be safe).

        period = self._period_tuple(self.period_combo.currentData())
        default_date = date(period[0], period[1], 1) if period else None
        dialog = AddDialog(
            for_user=self._view_user, parent=self, default_date=default_date
        )
        if not dialog.exec():
            return  # Cancelled

        data = dialog.result_data
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
        self.store.save()

        # A new record may introduce a new month; rebuild and then jump to the
        # month of the record we just added so the user sees it immediately.
        target = (data["date"].year, data["date"].month)
        self._reload_periods(prefer=target)

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
        menu = QMenu(self)
        edit_action = menu.addAction("Edit")
        delete_action = menu.addAction("Delete")
        chosen = menu.exec(table.viewport().mapToGlobal(pos))
        if chosen == edit_action:
            self._on_edit(int(record_id))
        elif chosen == delete_action:
            self._on_delete(int(record_id))

    def _on_edit(self, record_id: int) -> None:
        spending = self.store.get_spending(record_id)
        income = self.store.get_income(record_id)
        record = spending if spending is not None else income
        if record is None:
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

        dialog = AddDialog(for_user=record.user, parent=self, initial=initial)
        if not dialog.exec():
            return

        data = dialog.result_data
        self.store.update_record(
            record_id,
            record_type=data["type"],
            amount=data["amount"],
            on_date=data["date"],
            note=data["note"],
            category=data.get("category", ""),
        )
        self.store.save()

        new_period = (data["date"].year, data["date"].month)
        if new_period == old_period:
            # Stay on the month the user was viewing; only the row contents change.
            self._refresh_view()
        else:
            # Date moved to another month (the old month may now be empty).
            self._reload_periods(prefer=new_period)

    def _on_delete(self, record_id: int) -> None:
        spending = self.store.get_spending(record_id)
        income = self.store.get_income(record_id)
        record = spending if spending is not None else income
        if record is None:
            return

        when = record.date.strftime(DISPLAY_DATE_FORMAT)
        amount = format_amount(record.amount)
        if spending is not None:
            kind = "spending"
            detail = f"{amount} ({spending.category}) on {when}"
        else:
            kind = "income"
            detail = f"{amount} on {when}"

        answer = QMessageBox.question(
            self,
            "Delete record",
            f"Delete this {kind} for {record.user}?\n{detail}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        period = self._period_tuple(self.period_combo.currentData())
        self.store.delete_record(record_id)
        self.store.save()

        if period is not None and period in self.store.available_months(self._view_user):
            self._refresh_view()
        else:
            self._reload_periods(prefer=period)
