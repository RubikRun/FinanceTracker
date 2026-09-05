"""Modal dialog with a pie chart of spending split by category."""
from __future__ import annotations

import calendar
from decimal import Decimal
from typing import Optional

from PySide6.QtCharts import QChart, QChartView, QPieSeries
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from finance.config import (
    USER_BORIS,
    USER_MILENA,
    VIEW_BOTH_LABEL,
    chart_color_for,
    format_amount,
)
from finance.store import FinanceStore, month_range


def _period_tuple(data) -> Optional[tuple[int, int]]:
    """Normalize combo item data to `(year, month)`."""
    if data is None:
        return None
    try:
        year, month = data
        return (int(year), int(month))
    except (TypeError, ValueError):
        return None


class SpendingChartDialog(QDialog):
    """Pie chart of spending by category, with month and person selectors.

    Opens on the same period and person as the main window, then lets you
    change either without affecting the main window. An empty period shows a
    short explanation instead of a blank chart.
    """

    def __init__(
        self,
        store: FinanceStore,
        initial_period: tuple[int, int],
        initial_user: Optional[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Spending by category")
        self.setModal(True)
        self.resize(720, 560)
        self._build_ui()
        self._select_person(initial_user)
        self._reload_periods(prefer=initial_period)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Period:"))
        self.period_combo = QComboBox()
        self.period_combo.setMinimumWidth(160)
        self.period_combo.currentIndexChanged.connect(self._refresh_chart)
        top.addWidget(self.period_combo)

        top.addSpacing(16)
        top.addWidget(QLabel("Person:"))
        self.person_combo = QComboBox()
        self.person_combo.addItem(USER_BORIS, USER_BORIS)
        self.person_combo.addItem(USER_MILENA, USER_MILENA)
        self.person_combo.addItem(VIEW_BOTH_LABEL, "")
        self.person_combo.currentIndexChanged.connect(self._on_person_changed)
        top.addWidget(self.person_combo)
        top.addStretch(1)
        layout.addLayout(top)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.body, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

    def _select_person(self, user: Optional[str]) -> None:
        self.person_combo.blockSignals(True)
        if user:
            index = self.person_combo.findData(user)
            self.person_combo.setCurrentIndex(index if index >= 0 else 0)
        else:
            self.person_combo.setCurrentIndex(self.person_combo.count() - 1)
        self.person_combo.blockSignals(False)

    def _selected_user(self) -> Optional[str]:
        data = self.person_combo.currentData()
        return data if data else None

    def _find_period_index(self, period) -> int:
        want = _period_tuple(period)
        if want is None:
            return -1
        for i in range(self.period_combo.count()):
            if _period_tuple(self.period_combo.itemData(i)) == want:
                return i
        return -1

    def _on_person_changed(self) -> None:
        prefer = _period_tuple(self.period_combo.currentData())
        self._reload_periods(prefer=prefer)

    def _reload_periods(self, prefer: Optional[tuple[int, int]] = None) -> None:
        self.period_combo.blockSignals(True)
        self.period_combo.clear()
        for year, month in self.store.available_months(self._selected_user()):
            label = f"{calendar.month_name[month]} {year}"
            self.period_combo.addItem(label, (year, month))

        if self.period_combo.count() > 0:
            index = 0
            if prefer is not None:
                found = self._find_period_index(prefer)
                if found >= 0:
                    index = found
            self.period_combo.setCurrentIndex(index)
        self.period_combo.blockSignals(False)
        self._refresh_chart()

    def _refresh_chart(self) -> None:
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        period = _period_tuple(self.period_combo.currentData())
        if period is None:
            self.body_layout.addWidget(self._empty_label(), 1, Qt.AlignCenter)
            return

        year, month = period
        start, end = month_range(year, month)
        slices = self.store.spending_by_category(start, end, self._selected_user())
        if not slices:
            self.body_layout.addWidget(self._empty_label(), 1, Qt.AlignCenter)
            return

        total = sum((amount for _, amount in slices), Decimal("0"))
        total_label = QLabel(f"Total spent: {format_amount(total)}")
        self.body_layout.addWidget(total_label)
        self.body_layout.addWidget(self._make_chart(slices, total), 1)

    @staticmethod
    def _empty_label() -> QLabel:
        empty = QLabel("No spending in this period.")
        empty.setStyleSheet("color: #666;")
        return empty

    def _make_chart(
        self, slices: list[tuple[str, Decimal]], total: Decimal
    ) -> QChartView:
        series = QPieSeries()
        series.setPieSize(0.58)
        series.setHorizontalPosition(0.35)

        for category, amount in slices:
            percent = (amount / total * 100) if total else Decimal("0")
            label = f"{category}  {format_amount(amount)}  ({percent:.1f}%)"
            pie_slice = series.append(label, float(amount))
            pie_slice.setLabelVisible(False)
            # Don't connect hovered to setExploded directly: PySide can't wrap
            # that C++ method as a slot ("No Wrapper found").
            pie_slice.hovered.connect(
                lambda hovered, s=pie_slice: s.setExploded(hovered)
            )

        chart = QChart()
        chart.addSeries(series)
        # Theme colours are applied on addSeries; set ours afterwards so they stick.
        for pie_slice, (category, _) in zip(series.slices(), slices):
            pie_slice.setColor(QColor(chart_color_for(category)))
        chart.legend().setAlignment(Qt.AlignRight)
        chart.legend().setShowToolTips(True)
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        view = QChartView(chart)
        view.setRenderHint(QPainter.Antialiasing)
        return view
