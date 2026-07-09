"""FinanceTracker entry point. Launches the Qt UI.

Run with:  python main.py
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from finance.config import default_data_path
from finance.store import FinanceStore
from ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    # Use Qt's cross-platform Fusion style. It looks the same on Windows and
    # macOS and, unlike the native Windows 11 style, doesn't draw an accent
    # bar on the selected table row.
    app.setStyle("Fusion")
    store = FinanceStore.load(default_data_path())
    window = MainWindow(store)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
