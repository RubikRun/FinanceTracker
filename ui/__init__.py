"""Qt (PySide6) user interface for FinanceTracker.

This layer is a thin client over the `finance` core package. It never reads or
writes the JSON file directly; it only calls `FinanceStore`.
"""
