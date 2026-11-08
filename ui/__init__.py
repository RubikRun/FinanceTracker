"""Qt (PySide6) user interface for FinanceTracker.

The window calls ``FinanceStore`` for records and queries. When a Drive folder
is saved on this computer, startup goes through ``finance.session`` and the
window does not write the local finance file.
"""
