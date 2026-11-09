"""Writing a tree's blackboard to its blackboard file (``<tree name>_BB.json``) as it changes."""

from __future__ import annotations

import logging
import math
import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .blackboard import BlackboardStore
from .serialization import blackboard_document, write_json_atomic
from .tree_log import LogLevel

log = logging.getLogger("behavior_tree_widget")

__all__ = ["BlackboardFileWriter", "SAVE_ERRORS"]

#: Errors writing a file can raise.
SAVE_ERRORS = (OSError, ValueError, TypeError, RecursionError)


class BlackboardFileWriter(QObject):
    """Writes a blackboard to its blackboard file, safely, whenever it changes (GUI thread).

    Writes are throttled: at most one per ``interval_ms()`` milliseconds; changes made in between
    are written together at the end of the interval. Nothing is written while the file already
    holds the blackboard (e.g. only Object entries, which are never saved, changed). A failed
    write is reported once per new error and tried again at the next change.

    Signals:
        written(path): the blackboard was written to ``path``.
        failed(message): the blackboard could not be written (emitted at every failed write).
    """

    written = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        store: BlackboardStore,
        interval_ms: Callable[[], int],
        report: Callable[[LogLevel, str], None],
        parent: QObject | None = None,
    ):
        """``interval_ms`` gives the throttling interval when a write is scheduled; ``report(level,
        message)`` writes a message to the library's logger and to the tree's log."""
        super().__init__(parent)
        self._store = store
        self._interval_ms = interval_ms
        self._report = report
        self.path: str | None = None  # the blackboard file
        self.error: str | None = None  # the error of the last write, while it fails
        self._written: list | None = None  # the entries as last written to the file or read from it
        self._written_revision: int | None = None  # store.revision(saved=True) at that time
        self._pending = False
        self._skipped: set[str] = set()  # values that cannot be written, reported once each
        self._last_write = 0.0  # time.monotonic() of the last write
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.write_due)

    # ------------------------------------------------------------------ state
    def is_active(self) -> bool:
        """True while changes are written: there is a file and the blackboard was not released."""
        return bool(self.path) and not self._store.is_disposed()

    def is_pending(self) -> bool:
        """True while changes wait for the end of the interval."""
        return self._pending

    def adopt(self, path: str | None, file_holds_store: bool) -> None:
        """Write to ``path`` from now on. ``file_holds_store``: the file holds the blackboard as it
        is now (it was just read), so an unchanged blackboard is not written."""
        self.path = path
        self.cancel_pending()
        self.error = None
        self._skipped.clear()
        self._written_revision = self._store.revision(saved=True) if file_holds_store else None
        self._written = self._store.to_list(quiet=True) if file_holds_store else None

    def retarget(self, path: str) -> None:
        """Write to ``path`` from now on (the tree was saved under that name); the next write is forced."""
        self.path = path
        self.cancel_pending()
        self.error = None

    def note_reported(self, problems: list[str]) -> None:
        """Values the user was told about already (by Save): later writes do not log them again."""
        self._skipped.update(problems)

    # ------------------------------------------------------------------ writing
    def schedule(self) -> None:
        """Write soon: at once from the event loop, or at the end of the current interval."""
        self._pending = True
        if not self._timer.isActive():
            elapsed_ms = (time.monotonic() - self._last_write) * 1000.0
            self._timer.start(max(0, math.ceil(self._interval_ms() - elapsed_ms)))

    def cancel_pending(self) -> None:
        """Forget the changes waiting to be written (e.g. the file was replaced by a new, empty one)."""
        self._pending = False
        self._timer.stop()

    def flush(self) -> None:
        """Write the changes that are still due (before the file or the blackboard goes away)."""
        if self._pending:
            self.write_due()

    def write_due(self) -> None:
        """Write the changes that wait (the end of the interval)."""
        self._timer.stop()
        if not self._pending:
            return
        self._pending = False
        if self.is_active():
            self.write(force=False)

    def write(self, force: bool) -> bool:
        """Write the blackboard file now; returns False if that failed.

        Without ``force`` nothing is written when the file already holds the blackboard.
        """
        path = self.path
        revision = self._store.revision(saved=True)  # read first: later changes are written next time
        if not force and revision == self._written_revision and self.error is None:
            return True  # nothing the file holds changed (e.g. only Object entries changed)
        problems: list[str] = []
        blackboard = self._store.to_list(problems, quiet=True)
        for problem in problems:  # e.g. a value of a type that cannot be written as JSON
            if problem not in self._skipped:
                self._skipped.add(problem)
                log.warning("%s", problem)
        if not force and blackboard == self._written and self.error is None:
            self._written_revision = revision
            return True  # e.g. a value was changed and changed back
        self._last_write = time.monotonic()
        try:
            write_json_atomic(path, blackboard_document(blackboard))
        except SAVE_ERRORS as error:
            message = f"Could not write the blackboard to '{path}': {error}"
            if message != self.error:
                self._report(LogLevel.WARNING, message)
            self.error = message
            self.failed.emit(message)
            return False
        self._written = blackboard
        self._written_revision = revision
        if self.error is not None:
            self._report(LogLevel.INFO, f"The blackboard was written to '{path}' again")
            self.error = None
        self.written.emit(path)
        return True
