"""The tree log: messages about a tree, written to a log file and announced with Qt signals.

Every :class:`~behavior_tree_widget.BehaviorTreeWidget` has a log. Messages have one of
these levels, by precedence (lower numbers are more important):

====  =========  ==============================================================
0     NONE       no message; as the log level, logging is switched off
1     ERROR      a node or the tree failed with an error
2     WARNING    a problem: a file loaded with problems, a value not saved, ...
3     DEBUG      messages of Print Message nodes (their default level)
4     INFO       the execution of every node, execution started / stopped, ...
====  =========  ==============================================================

The log level of the tree (``TreeConfig.log_level``, INFO by default) is the least important
level that is logged: a message whose level number is higher is dropped, and NONE drops
everything. Each message that is logged is written to the log file and announced with the
signal of its level (``errorLogged``, ``warningLogged``, ``debugLogged``, ``infoLogged``) and
with ``messageLogged(level, message)``.

The log file is ``<tree name>_<month>-<day>-<year>_<hour>-<minute>-<second>.log`` in the
folder the application was started from, named with the time the application was started
(when it imported this package). It holds every message logged while that tree was the
tree of the widget; messages logged before a tree was created or loaded are written to the
first log file. Several trees loaded one after the other each get their own file (the same
tree loaded again appends to its file).

Logging never blocks: the stdlib ``logging`` machinery hands the records to one background
thread (``QueueHandler`` / ``QueueListener``) that writes every log file, and the signals are
emitted from the event loop of the GUI thread (logging from a worker thread or from a slot
of these signals is safe).
"""

from __future__ import annotations

import atexit
import collections
import datetime
import enum
import logging
import logging.handlers
import os
import queue
import threading
from typing import Any, cast, override

from PySide6.QtCore import QMetaMethod, QObject, Qt, Signal

__all__ = ["APP_START", "START_FOLDER", "LogLevel", "TreeLogger", "flush_logs", "log_file_name"]

_log = logging.getLogger("behavior_tree_widget")


class LogLevel(enum.IntEnum):
    """Precedence of a log message (lower numbers are more important)."""

    NONE = 0
    ERROR = 1
    WARNING = 2
    DEBUG = 3
    INFO = 4

    @property
    def label(self) -> str:
        """``"3: DEBUG"``: how the level is offered in the drop-down lists."""
        return f"{self.value}: {'None' if self is LogLevel.NONE else self.name}"

    @classmethod
    def coerce(cls, value: Any) -> LogLevel:
        """A LogLevel from a LogLevel, its number (0 - 4) or its name (``"debug"``).

        Raises:
            ValueError: ``value`` names no level.
            TypeError: ``value`` is of another type (e.g. a bool).
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            raise TypeError("a log level is a LogLevel, an int from 0 to 4 or a level name, not a bool")
        if isinstance(value, int):
            try:
                return cls(value)
            except ValueError:
                raise ValueError(f"log levels are numbered 0 to 4, not {value}") from None
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                raise ValueError(f"unknown log level {value!r}") from None
        raise TypeError(f"a log level is a LogLevel, an int from 0 to 4 or a level name, not {type(value).__name__}")


#: The stdlib logging level each message level is written with (its name shows in the file).
PYTHON_LEVELS = {
    LogLevel.ERROR: logging.ERROR,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
}

#: When the application was started (it imported this package): the time in the log file names.
APP_START = datetime.datetime.now()
#: The folder the application was started from: the folder of the log files.
START_FOLDER = os.getcwd()
LOG_TIME_FORMAT = "%m-%d-%Y_%H-%M-%S"
LOG_EXTENSION = ".log"


def log_file_name(tree_name: str, start: datetime.datetime | None = None) -> str:
    """``<tree name>_<month>-<day>-<year>_<hour>-<minute>-<second>.log`` for the application start
    (or ``start``)."""
    return f"{tree_name}_{(start or APP_START).strftime(LOG_TIME_FORMAT)}{LOG_EXTENSION}"


# ====================================================================== the writer thread
# One queue and one thread write the log files of every tree log, in the order the messages
# were logged. Records carry the _LogFile they belong to (attribute bt_target).
_queue: queue.SimpleQueue = queue.SimpleQueue()
_listener: logging.handlers.QueueListener | None = None
_listener_lock = threading.Lock()
_unflushed: set = set()  # _LogFiles written since the last flush (writer thread only)


class _Dispatch(logging.Handler):
    """Hands each record to the log file it belongs to (runs on the writer thread)."""

    @override
    def handle(self, record: logging.LogRecord) -> bool:
        barrier = getattr(record, "bt_barrier", None)
        target = getattr(record, "bt_target", None)
        try:
            if target is not None:
                target.process(record)
            # The files are flushed when the writer has caught up (not after every record,
            # which costs a system call each): at most a burst of messages is not on disk yet.
            if barrier is not None or _queue.empty():
                while _unflushed:
                    _unflushed.pop().flush()
        except Exception:  # noqa: BLE001 - the writer thread must survive anything
            try:
                _log.debug("writing a log record failed", exc_info=True)
            except Exception:  # noqa: BLE001
                pass
        if barrier is not None:
            barrier.set()
        return True

    @override
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - handle() is used
        self.handle(record)


def _ensure_writer() -> None:
    global _listener
    with _listener_lock:
        if _listener is None:
            listener = logging.handlers.QueueListener(_queue, _Dispatch())
            listener.start()
            _listener = listener
            atexit.register(_stop_writer)


def _stop_writer() -> None:
    """At exit: write what is still queued and close the files."""
    global _listener
    with _listener_lock:
        listener, _listener = _listener, None
    if listener is not None:
        try:
            listener.stop()
        except Exception:  # noqa: BLE001
            pass


def flush_logs(timeout: float = 5.0) -> bool:
    """Wait until every message logged so far is written to its log file.

    Returns False if that took longer than ``timeout`` seconds. Logging never waits for the
    files; call this before reading a log file (e.g. in tests).
    """
    if _listener is None:
        return True
    event = threading.Event()
    _queue.put(logging.makeLogRecord({"msg": "", "bt_barrier": event}))
    return event.wait(timeout)


class _LogFile:
    """The log file of one tree log. Used on the writer thread only.

    Records logged before the first file is chosen (or while the file cannot be opened) are
    kept (the last ``BUFFER`` of them) and written to the next file chosen, as soon as it is
    chosen (also when nothing else is ever logged to it).
    """

    BUFFER = 1000

    def __init__(self, formatter: logging.Formatter):
        self._formatter = formatter
        self._path: str | None = None
        self._handler: logging.FileHandler | None = None
        self._buffer: collections.deque = collections.deque(maxlen=self.BUFFER)
        self._failed: set[str] = set()
        self._closed = False

    def process(self, record: logging.LogRecord) -> None:
        action = getattr(record, "bt_action", None)
        if action == "switch":
            self._close_handler()
            self._path = cast(Any, record).bt_path  # set by set_file (logging.makeLogRecord)
            if self._buffer:
                self._open()  # the messages kept so far go to this file at once
        elif action == "close":
            if self._buffer:
                self._open()  # a last chance for kept messages (e.g. the file was not writable)
            self._close_handler()
            self._closed = True
            self._buffer.clear()
        elif not self._closed:
            handler = self._open()
            if handler is not None:
                handler.handle(record)
                _unflushed.add(self)
            else:
                self._buffer.append(record)

    def flush(self) -> None:
        if self._handler is not None:
            self._handler.flush()

    def _open(self) -> logging.FileHandler | None:
        """Open the chosen file (when the first record for it comes) and write the kept records.
        Returns the handler writing the file, or None when there is no file to write to."""
        if self._handler is not None:
            return self._handler
        path = self._path
        if path is None or path in self._failed:
            return None
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            handler = _UnflushedFileHandler(path, mode="a", encoding="utf-8")
        except OSError as error:
            self._failed.add(path)  # reported once
            _log.warning("the log file '%s' cannot be written: %s", path, error)
            return None
        handler.setFormatter(self._formatter)
        self._handler = handler
        while self._buffer:
            handler.handle(self._buffer.popleft())
        return handler

    def _close_handler(self) -> None:
        handler, self._handler = self._handler, None
        _unflushed.discard(self)
        if handler is not None:
            handler.close()


class _UnflushedFileHandler(logging.FileHandler):
    """A FileHandler that leaves flushing to the writer thread (see _Dispatch.handle)."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if self.stream is None:
            if self.mode != "w" or not self._closed:  # type: ignore[attr-defined]  # set by FileHandler; not in typeshed
                self.stream = self._open()
            if self.stream is None:
                return
        try:
            self.stream.write(self.format(record) + self.terminator)
        except RecursionError:
            raise
        except Exception:  # noqa: BLE001 - as logging.StreamHandler.emit
            self.handleError(record)


class _FileQueueHandler(logging.handlers.QueueHandler):
    """Queues the records of one tree log for its log file."""

    def __init__(self, sink: _LogFile):
        super().__init__(_queue)
        self._sink = sink

    @override
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # The records are made by TreeLogger.log for this queue only (a plain message, no
        # arguments, no exception): no need for QueueHandler's copy of the formatted record.
        record.bt_target = self._sink
        return record


# ====================================================================== the log of one widget
#: The signal announcing the messages of each level.
LEVEL_SIGNALS = {
    LogLevel.ERROR: "errorLogged",
    LogLevel.WARNING: "warningLogged",
    LogLevel.DEBUG: "debugLogged",
    LogLevel.INFO: "infoLogged",
}


class TreeLogger(QObject):
    """The log of one BehaviorTreeWidget (see the module docs). Thread-safe."""

    errorLogged = Signal(str)
    warningLogged = Signal(str)
    debugLogged = Signal(str)
    infoLogged = Signal(str)
    messageLogged = Signal(int, str)  # level (LogLevel value), message
    _deliver = Signal()

    FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s"
    DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    def __init__(self, parent: QObject | None = None, emitter: QObject | None = None):
        """``emitter``: the object whose signals of the same names announce the messages (for
        example the BehaviorTreeWidget); by default the logger's own."""
        super().__init__(parent)
        # Any object with the signals errorLogged ... messageLogged (looked up by name).
        self._emitter: Any = emitter if emitter is not None else self
        self._level = LogLevel.INFO
        self._lock = threading.Lock()
        self._pending: collections.deque = collections.deque()  # (level, message) not signalled yet
        self._scheduled = False
        self._closed = False
        self._path: str | None = None
        self._sink = _LogFile(logging.Formatter(self.FORMAT, self.DATE_FORMAT))
        # A logger of its own, not registered with logging.getLogger (which keeps every logger
        # for good): it does not propagate, so the application's handlers do not get the
        # node-by-node execution messages.
        self._logger = logging.Logger("behavior_tree_widget.tree", logging.DEBUG)
        self._logger.propagate = False
        self._logger.addHandler(_FileQueueHandler(self._sink))
        self._deliver.connect(self._on_deliver, Qt.ConnectionType.QueuedConnection)
        _ensure_writer()

    # ------------------------------------------------------------------ level
    def level(self) -> LogLevel:
        return self._level

    def set_level(self, level: Any) -> None:
        self._level = LogLevel.coerce(level)

    def enabled(self, level: LogLevel) -> bool:
        """Whether a message of ``level`` would be logged now."""
        return not self._closed and level is not LogLevel.NONE and level <= self._level

    # ------------------------------------------------------------------ messages
    def log(self, level: Any, message: Any) -> bool:
        """Log ``message`` at ``level`` (any thread; never blocks). Returns False if it was
        dropped (its level is NONE or less important than the log level, or the log is closed)."""
        level = LogLevel.coerce(level)
        if not self.enabled(level):
            return False
        if not isinstance(message, str):
            try:
                message = str(message)
            except Exception:  # noqa: BLE001 - a broken __str__
                message = f"<{type(message).__name__} object>"
        # makeRecord + handle: Logger.log would also look up the calling code, which costs more
        # than everything else here and is not written.
        record = self._logger.makeRecord(self._logger.name, PYTHON_LEVELS[level], "", 0, message, (), None)
        record.bt_level = int(level)
        self._logger.handle(record)
        with self._lock:
            self._pending.append((level, message))
            schedule = not self._scheduled
            self._scheduled = True
        if schedule:
            try:
                self._deliver.emit()  # the signals are emitted from the GUI thread's event loop
            except RuntimeError:  # the widget is being destroyed
                pass
        return True

    def _on_deliver(self) -> None:
        with self._lock:
            items = list(self._pending)
            self._pending.clear()
            self._scheduled = False
        emitter = self._emitter
        # Only the signals somebody listens to are emitted (a run logs every node).
        signals = {}
        for level, name in LEVEL_SIGNALS.items():
            signal = getattr(emitter, name)
            if emitter.isSignalConnected(QMetaMethod.fromSignal(signal)):
                signals[level] = signal
        any_message = emitter.messageLogged
        any_connected = emitter.isSignalConnected(QMetaMethod.fromSignal(any_message))
        for level, message in items:
            signal = signals.get(level)
            if signal is not None:
                signal.emit(message)
            if any_connected:
                any_message.emit(int(level), message)

    # ------------------------------------------------------------------ file
    def path(self) -> str | None:
        """The log file messages are written to now (None before a tree name was given). The
        file is created with the first message written to it."""
        return self._path

    def set_file(self, path: str) -> None:
        """Write the messages logged from now on to ``path`` (earlier ones stay where they went)."""
        path = os.path.abspath(path)
        if self._closed or path == self._path:
            return
        self._path = path
        _queue.put(logging.makeLogRecord({"bt_target": self._sink, "bt_action": "switch", "bt_path": path}))

    def close(self) -> None:
        """Write what was logged and close the file; later messages are dropped."""
        if self._closed:
            return
        self._closed = True
        _queue.put(self.closer_record())

    def closer_record(self) -> logging.LogRecord:
        """A record closing this log's file when the writer thread reaches it."""
        return logging.makeLogRecord({"bt_target": self._sink, "bt_action": "close"})


def close_log_file(record: logging.LogRecord) -> None:
    """Queue a record made by :meth:`TreeLogger.closer_record` (works after the logger is gone)."""
    _queue.put(record)
