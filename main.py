"""FinanceTracker entry point. Launches the Qt UI.

Run with:  python main.py

When this computer has a saved Drive folder, or cloud mode is already on,
the window uses the encrypted session. Otherwise it still opens the local
finance file.
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from finance.config import default_data_path
from finance.profile import ConfigError, load_local_config
from finance.store import FinanceStore
from ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    # Use Qt's cross-platform Fusion style. It looks the same on Windows and
    # macOS and, unlike the native Windows 11 style, doesn't draw an accent
    # bar on the selected table row.
    app.setStyle("Fusion")
    try:
        config = load_local_config()
    except ConfigError as exc:
        QMessageBox.critical(None, "Finance Tracker", str(exc))
        return 1
    if config.cloud_mode or config.drive_folder_id:
        from ui.cloud_startup import run_cloud

        return run_cloud(app, config)
    store = FinanceStore.load(default_data_path())
    window = MainWindow(store)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
