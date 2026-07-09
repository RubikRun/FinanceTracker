"""Main application window: period selector, summary and the two tables."""
from __future__ import annotations

import calendar
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from finance.config import DISPLAY_DATE_FORMAT, format_amount
from finance.store import FinanceStore, month_range

from .add_dialog import AddDialog


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
        self.setWindowTitle("Finance Tracker")
        self.resize(820, 640)
        self._build_ui()
        self._reload_periods()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Top bar: Add button + period selector ---
        top = QHBoxLayout()
        self.add_btn = QPushButton("+ Add")
        self.add_btn.clicked.connect(self._on_add)
        top.addWidget(self.add_btn)
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
        self.spending_table = QTableWidget(0, 4)
        self.spending_table.setHorizontalHeaderLabels(
            ["Date", "Category", "Amount", "Note"]
        )
        self._setup_table(self.spending_table, [90, 130, 110, 200])
        spending_layout.addWidget(self.spending_table)
        splitter.addWidget(spending_box)

        income_box = QGroupBox("Income")
        income_layout = QVBoxLayout(income_box)
        self.income_table = QTableWidget(0, 3)
        self.income_table.setHorizontalHeaderLabels(["Date", "Amount", "Note"])
        self._setup_table(self.income_table, [90, 110, 200])
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
    # Data binding
    # ------------------------------------------------------------------ #
    def _reload_periods(self) -> None:
        """Rebuild the period dropdown from the months that have records."""
        self.period_combo.blockSignals(True)
        self.period_combo.clear()
        for year, month in self.store.available_months():
            label = f"{calendar.month_name[month]} {year}"
            self.period_combo.addItem(label, (year, month))
        self.period_combo.blockSignals(False)
        if self.period_combo.count():
            self.period_combo.setCurrentIndex(0)
        self._refresh_view()

    def _refresh_view(self) -> None:
        period = self.period_combo.currentData()
        if period is None:
            self._fill_spending([])
            self._fill_income([])
            self._update_summary(Decimal("0"), Decimal("0"))
            return

        year, month = period
        start, end = month_range(year, month)
        spending = sorted(self.store.spending_between(start, end), key=lambda r: r.date)
        income = sorted(self.store.income_between(start, end), key=lambda r: r.date)
        self._fill_spending(spending)
        self._fill_income(income)
        self._update_summary(
            self.store.total_earned(start, end), self.store.total_spent(start, end)
        )

    def _fill_spending(self, records) -> None:
        self.spending_table.setRowCount(len(records))
        for row, record in enumerate(records):
            date_item = QTableWidgetItem(record.date.strftime(DISPLAY_DATE_FORMAT))
            date_item.setData(Qt.UserRole, record.id)  # keep id for future use
            self.spending_table.setItem(row, 0, date_item)
            self.spending_table.setItem(row, 1, QTableWidgetItem(record.category))
            self.spending_table.setItem(row, 2, QTableWidgetItem(format_amount(record.amount)))
            self.spending_table.setItem(row, 3, QTableWidgetItem(record.note))

    def _fill_income(self, records) -> None:
        self.income_table.setRowCount(len(records))
        for row, record in enumerate(records):
            date_item = QTableWidgetItem(record.date.strftime(DISPLAY_DATE_FORMAT))
            date_item.setData(Qt.UserRole, record.id)
            self.income_table.setItem(row, 0, date_item)
            self.income_table.setItem(row, 1, QTableWidgetItem(format_amount(record.amount)))
            self.income_table.setItem(row, 2, QTableWidgetItem(record.note))

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
        dialog = AddDialog(self)
        if not dialog.exec():
            return  # Cancelled

        data = dialog.result_data
        if data["type"] == "spending":
            self.store.add_spending(
                amount=data["amount"],
                category=data["category"],
                on_date=data["date"],
                note=data["note"],
            )
        else:
            self.store.add_income(
                amount=data["amount"],
                on_date=data["date"],
                note=data["note"],
            )
        self.store.save()

        # A new record may introduce a new month; rebuild and then jump to the
        # month of the record we just added so the user sees it immediately.
        target = (data["date"].year, data["date"].month)
        self._reload_periods()
        index = self.period_combo.findData(target)
        if index >= 0:
            self.period_combo.setCurrentIndex(index)
