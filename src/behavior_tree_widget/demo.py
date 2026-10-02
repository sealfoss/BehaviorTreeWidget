"""Demo application with a few example leaf nodes.

Run it with ``python -m behavior_tree_widget [tree.json]`` or the
``behavior-tree-widget-demo`` command installed with the package.
"""

from __future__ import annotations

import logging
import random
import sys
import time

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QMessageBox

from . import BehaviorTreeWidget, LeafNodeWidget, Status

log = logging.getLogger("behavior_tree_widget.demo")


class Wait(LeafNodeWidget):
    """Waits for a number of seconds (runs on a worker thread, can be cancelled)."""

    _title = "Wait"
    _fields = {"seconds": 1.0}

    def OnRun(self, tree):
        deadline = time.monotonic() + max(0.0, self.GetField("seconds"))
        while time.monotonic() < deadline:
            if self.CancelRequested():
                return False
            time.sleep(0.02)
        return True


class LogMessage(LeafNodeWidget):
    """Writes a message to the log and to the "last_message" blackboard entry."""

    _title = "Log Message"
    _fields = {"message": "Hello from the behavior tree!", "level": ["INFO", "WARNING", "ERROR"]}
    RUN_IN_THREAD = False  # quick, runs on the GUI thread

    def OnRun(self, tree):
        level = getattr(logging, self.GetFieldSelection("level"), logging.INFO)
        message = self.GetField("message")
        log.log(level, message)
        tree.SetEntry(message, "last_message")
        return True


class IncrementCounter(LeafNodeWidget):
    """Adds ``step`` to an Integer blackboard entry (created when missing)."""

    _title = "Increment Counter"
    _fields = {"entry": "counter", "step": 1}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        name = self.GetField("entry")
        value = tree.GetEntry(name) if tree.HasEntry(name) else 0
        tree.SetEntry(value + self.GetField("step"), name)
        return True


class CounterAtLeast(LeafNodeWidget):
    """Condition: succeeds when an Integer blackboard entry is >= ``threshold``."""

    _title = "Counter At Least"
    _fields = {"entry": "counter", "threshold": 3}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        name = self.GetField("entry")
        return tree.HasEntry(name) and tree.GetEntry(name) >= self.GetField("threshold")


class RandomOutcome(LeafNodeWidget):
    """Succeeds with the given probability."""

    _title = "Random Outcome"
    _fields = {"success_probability": 0.5}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return random.random() < self.GetField("success_probability")


class MoveTo(LeafNodeWidget):
    """Simulates moving to (x, y); shows every field type. Writes "position" to the blackboard."""

    _title = "Move To"
    _fields = {
        "x": 5.0,
        "y": 2.5,
        "speed": 5,
        "mode": ["walk", "run", "crawl"],
        "avoid_obstacles": True,
        "label": "target",
    }

    def OnRun(self, tree):
        position = tree.GetEntry("position") if tree.HasEntry("position") else [0.0, 0.0]
        target = [self.GetField("x"), self.GetField("y")]
        factor = {"walk": 1.0, "run": 2.0, "crawl": 0.25}[self.GetFieldSelection("mode")]
        step = max(1, self.GetField("speed")) * factor * 0.05
        while True:
            if self.CancelRequested():
                return False
            delta = [t - p for t, p in zip(target, position)]
            distance = (delta[0] ** 2 + delta[1] ** 2) ** 0.5
            if distance <= step:
                tree.SetEntry(target, "position")
                return True
            position = [p + d / distance * step for p, d in zip(position, delta)]
            tree.SetEntry([round(p, 3) for p in position], "position")
            time.sleep(0.05)


class Fail(LeafNodeWidget):
    """Always fails."""

    _title = "Fail"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return False


class RunningTicks(LeafNodeWidget):
    """Returns RUNNING for ``ticks`` ticks, then succeeds (polling style, GUI thread)."""

    _title = "Running For Ticks"
    _fields = {"ticks": 3}
    RUN_IN_THREAD = False

    def OnStart(self, tree):
        self._remaining = self.GetField("ticks")

    def OnRun(self, tree):
        if self._remaining > 0:
            self._remaining -= 1
            return Status.RUNNING
        return True


DEMO_NODE_TYPES = [Wait, LogMessage, IncrementCounter, CounterAtLeast, RandomOutcome, MoveTo, Fail, RunningTicks]


class DemoWindow(QMainWindow):
    """Main window hosting a BehaviorTreeWidget with the demo node types."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Behavior Tree")
        self.tree = BehaviorTreeWidget(node_types=DEMO_NODE_TYPES)
        self.setCentralWidget(self.tree)
        self._status = QLabel("Click New to create a tree or Load to open one.")
        self.statusBar().addWidget(self._status, 1)
        self.tree.executionStateChanged.connect(lambda state: self._status.setText(f"Execution: {state}"))
        self.tree.executionFinished.connect(lambda result: self._status.setText(f"Execution finished: {result}"))
        self.tree.nodeError.connect(lambda node, message: self._status.setText(f"{node.GetTitle()}: {message}"))
        self.tree.treeLoaded.connect(lambda path: self._status.setText(f"Loaded {path}"))
        self.tree.treeSaved.connect(lambda path: self._status.setText(f"Saved {path}"))
        self.resize(1000, 720)

    def closeEvent(self, event):  # noqa: N802
        if not self.tree.ConfirmDiscardChanges():
            event.ignore()
            return
        self.tree.Shutdown()
        super().closeEvent(event)


def _unconsumed_arguments(argv: list[str], arguments: list[str]) -> list[str]:
    """``argv`` without the entries QApplication consumed (its options, e.g. ``-style fusion``).

    ``arguments`` is ``QApplication.arguments()``: Qt only removes entries, but it decodes
    them with the local 8-bit code page, which garbles other characters (e.g. in file
    names on Windows). So the kept entries are taken from ``argv``. If that is ambiguous,
    ``arguments`` is returned.
    """

    def same(arg: str, qt_arg: str) -> bool:
        return arg == qt_arg or QFile.decodeName(QFile.encodeName(arg)) == qt_arg

    # Qt removes options ("-name") and option values; a value follows its option with only
    # removed entries in between. Follow every way of removing entries that fits:
    # (index into arguments, removed options not yet followed by a removed value) -> up to
    # two distinct kept lists. The result is used only if all of them keep the same strings.
    removable = len(argv) - len(arguments)  # the number of entries Qt consumed
    states: dict[tuple[int, int], set[tuple[str, ...]]] = {(1, 0): {tuple(argv[:1])}}
    for position, arg in enumerate(argv[1:], 1):
        following: dict[tuple[int, int], set[tuple[str, ...]]] = {}
        for (index, options), kept in states.items():
            if index < len(arguments) and same(arg, arguments[index]):
                following.setdefault((index + 1, 0), set()).update(k + (arg,) for k in kept)
            if position - index < removable and (arg.startswith("-") or options):
                key = (index, options + 1 if arg.startswith("-") else options - 1)
                following.setdefault(key, set()).update(kept)
        states = {key: set(list(kept)[:2]) for key, kept in following.items()}
    results = set().union(*(kept for (index, _), kept in states.items() if index == len(arguments)))
    return list(results.pop()) if len(results) == 1 else list(arguments)


def main(argv: list[str] | None = None) -> int:
    """Start the demo application. An optional argument names a tree file to load."""
    argv = list(sys.argv if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = QApplication.instance()
    if app is None:
        app = QApplication(argv)
        arguments = _unconsumed_arguments(argv, app.arguments())  # without Qt options such as -style
    else:
        arguments = argv
    window = DemoWindow()
    window.show()
    if len(arguments) > 1:
        try:
            window.tree.LoadTree(arguments[1])
        except Exception as error:  # noqa: BLE001
            log.error("could not load %s: %s", arguments[1], error)
            QMessageBox.critical(window, "Load Behavior Tree", f"Could not load '{arguments[1]}':\n{error}")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
