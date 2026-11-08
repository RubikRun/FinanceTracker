"""Runs session work off the Qt thread, one job at a time.

Drive calls already use the adapter's own thread. This runner is the one the
window waits on, so a save does not freeze the event loop and a poll cannot
overlap a save.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal


class InlineRunner:
    """Runs the job before ``submit`` returns. Tests use this."""

    def __init__(self) -> None:
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(self, fn: Callable[[], object], done: Callable[[object], None]) -> None:
        self._busy = True
        try:
            try:
                result = fn()
            except Exception as exc:
                result = exc
        finally:
            self._busy = False
        done(result)

    def stop(self) -> bool:
        return True


class QtJobRunner(QObject):
    """One background thread. Results are delivered on the Qt thread that created this object."""

    _deliver = Signal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self._deliver.connect(self._on_deliver, Qt.QueuedConnection)
        self._pending = 0
        self._lock = threading.Lock()
        self._stopped = False
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name="finance-session", daemon=True
        )
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._pending > 0

    def submit(self, fn: Callable[[], object], done: Callable[[object], None]) -> None:
        if self._stopped:
            return
        with self._lock:
            self._pending += 1
        self._queue.put((fn, done))

    def stop(self) -> bool:
        """Wait until the current job finishes. True when the thread has exited."""
        if not self._stopped:
            self._stopped = True
            self._queue.put(None)
            self._thread.join(timeout=10)
        return not self._thread.is_alive()

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, done = item
            try:
                result = fn()
            except Exception as exc:
                result = exc
            self._deliver.emit(done, result)

    def _on_deliver(self, done: object, result: object) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
        if callable(done):
            done(result)
