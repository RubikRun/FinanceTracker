"""Modal dialog for adding or editing an income or spending record."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from finance.config import CATEGORIES, CURRENCY_SYMBOL

# Highlight applied to a field whose value failed validation.
_ERROR_STYLE = "border: 1px solid red;"


class AddDialog(QDialog):
    """Collects user input for a new or existing record.

    The dialog only gathers and validates input; it does not touch the store.
    The active person (`for_user`) is assigned automatically by the caller —
    there is no person picker here, so you can't accidentally create a record
    under the wrong name while in Boris or Milena mode.
    Pass `initial` (same keys as `result_data`) to open in edit mode with
    fields pre-filled. On success `result_data` holds a dict the caller uses
    to create or update the record.
    """

    def __init__(
        self, for_user: str, parent=None, initial: Optional[dict] = None
    ) -> None:
        super().__init__(parent)
        self.for_user = for_user
        self._editing = initial is not None
        self.setWindowTitle(
            f"Edit for {for_user}" if self._editing else f"Add for {for_user}"
        )
        self.setModal(True)
        self.result_data: Optional[dict] = None
        self._build_ui()
        if initial is not None:
            self._prefill(initial)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        context = QLabel(f"This record will be saved under <b>{self.for_user}</b>.")
        context.setWordWrap(True)
        layout.addWidget(context)

        # --- Income / Spending selector ---
        type_row = QHBoxLayout()
        self.spending_radio = QRadioButton("Spending")
        self.income_radio = QRadioButton("Income")
        self.spending_radio.setChecked(True)
        self._type_group = QButtonGroup(self)
        self._type_group.addButton(self.spending_radio)
        self._type_group.addButton(self.income_radio)
        type_row.addWidget(self.spending_radio)
        type_row.addWidget(self.income_radio)
        type_row.addStretch(1)
        layout.addLayout(type_row)

        # --- Fields ---
        form = QFormLayout()

        self.amount_edit = QLineEdit()
        self.amount_edit.setPlaceholderText("e.g. 14.20")
        self.amount_label = QLabel(f"Amount ({CURRENCY_SYMBOL})")
        form.addRow(self.amount_label, self.amount_edit)

        self.category_combo = QComboBox()
        self.category_combo.addItems(CATEGORIES)
        self.category_label = QLabel("Category")
        form.addRow(self.category_label, self.category_combo)

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd/MM/yyyy")
        self.date_edit.setDate(QDate.currentDate())
        form.addRow("Date", self.date_edit)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("optional")
        form.addRow("Note", self.note_edit)

        layout.addLayout(form)

        # --- Inline error message ---
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: red;")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        # --- Buttons ---
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.add_btn = QPushButton("Save" if self._editing else "Add")
        self.add_btn.setDefault(True)
        button_row.addWidget(self.cancel_btn)
        button_row.addWidget(self.add_btn)
        layout.addLayout(button_row)

        # --- Wiring ---
        self.add_btn.clicked.connect(self._on_add)
        self.cancel_btn.clicked.connect(self.reject)
        self.spending_radio.toggled.connect(self._update_category_visibility)
        self._update_category_visibility()

    def _prefill(self, data: dict) -> None:
        if data.get("type") == "income":
            self.income_radio.setChecked(True)
        else:
            self.spending_radio.setChecked(True)

        self.amount_edit.setText(f"{data['amount']:.2f}")
        on_date = data["date"]
        self.date_edit.setDate(QDate(on_date.year, on_date.month, on_date.day))
        self.note_edit.setText(data.get("note", ""))

        category = data.get("category")
        if category:
            if self.category_combo.findText(category) < 0:
                self.category_combo.addItem(category)
            self.category_combo.setCurrentText(category)

    def _update_category_visibility(self) -> None:
        # Category only applies to spending records.
        is_spending = self.spending_radio.isChecked()
        self.category_label.setVisible(is_spending)
        self.category_combo.setVisible(is_spending)

    def _on_add(self) -> None:
        self._clear_errors()

        amount = self._validate_amount()
        if amount is None:
            self.amount_edit.setStyleSheet(_ERROR_STYLE)
            self._show_error("Amount must be a number greater than 0.")
            return

        qdate = self.date_edit.date()
        on_date = date(qdate.year(), qdate.month(), qdate.day())
        note = self.note_edit.text().strip()

        if self.spending_radio.isChecked():
            self.result_data = {
                "type": "spending",
                "amount": amount,
                "category": self.category_combo.currentText(),
                "date": on_date,
                "note": note,
                "user": self.for_user,
            }
        else:
            self.result_data = {
                "type": "income",
                "amount": amount,
                "date": on_date,
                "note": note,
                "user": self.for_user,
            }
        self.accept()

    def _validate_amount(self) -> Optional[Decimal]:
        # Accept both '14.20' and '14,20' for convenience.
        text = self.amount_edit.text().strip().replace(",", ".")
        if not text:
            return None
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
        if value <= 0:
            return None
        return value

    def _clear_errors(self) -> None:
        self.amount_edit.setStyleSheet("")
        self.error_label.setVisible(False)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)
