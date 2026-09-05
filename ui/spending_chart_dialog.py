"""Modal dialog with a pie chart of spending split by category."""
from __future__ import annotations

from decimal import Decimal

from PySide6.QtCharts import QChart, QChartView, QPieSeries
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from finance.config import chart_color_for, format_amount


class SpendingChartDialog(QDialog):
    """Shows how spending in a period is divided between categories.

    `slices` is `(category, amount)` pairs, typically from
    `FinanceStore.spending_by_category`. An empty list shows a short
    explanation instead of a blank chart.
    """

    def __init__(
        self,
        period_label: str,
        who: str,
        slices: list[tuple[str, Decimal]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Spending by category")
        self.setModal(True)
        self.resize(720, 520)
        self._build_ui(period_label, who, slices)

    def _build_ui(
        self,
        period_label: str,
        who: str,
        slices: list[tuple[str, Decimal]],
    ) -> None:
        layout = QVBoxLayout(self)

        heading = QLabel(f"{period_label} — {who}")
        heading.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(heading)

        if not slices:
            empty = QLabel("No spending in this period.")
            empty.setStyleSheet("color: #666;")
            layout.addWidget(empty, 1, Qt.AlignCenter)
        else:
            total = sum((amount for _, amount in slices), Decimal("0"))
            total_label = QLabel(f"Total spent: {format_amount(total)}")
            layout.addWidget(total_label)
            layout.addWidget(self._make_chart(slices, total), 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

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
