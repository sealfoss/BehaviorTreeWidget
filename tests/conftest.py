"""Shared fixtures for the behavior_tree_widget test-suite.

Every test runs offscreen. Modal UI (message boxes, file dialogs, input dialogs,
context menus and custom dialogs) is replaced by the ``dialogs`` fixture so no
test can block; tests configure the answers they need through that fixture.
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.path.isdir("C:/Windows/Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox  # noqa: E402

from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget, Status  # noqa: E402
from behavior_tree_widget.blackboard import BlackboardView  # noqa: E402
from behavior_tree_widget.canvas import TreeView  # noqa: E402
from behavior_tree_widget.demo import DEMO_NODE_TYPES  # noqa: E402


# ============================================================================ test node types
class Succeed(LeafNodeWidget):
    """Succeeds immediately on the GUI thread."""

    _title = "Succeed"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class FailNode(LeafNodeWidget):
    """Fails immediately on the GUI thread."""

    _title = "Fail Node"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return False


class ThreadedSucceed(LeafNodeWidget):
    """Succeeds on a worker thread and records the thread it ran on."""

    _title = "Threaded Succeed"
    threads: list[int] = []

    def OnRun(self, tree):
        type(self).threads.append(threading.get_ident())
        return True


class Raise(LeafNodeWidget):
    """Raises from OnRun."""

    _title = "Raise"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        raise ValueError("boom")


class ReturnNone(LeafNodeWidget):
    """Forgets to return a value."""

    _title = "Return None"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return None


class SlowCancelable(LeafNodeWidget):
    """Runs on a worker thread until cancelled or ``seconds`` elapsed."""

    _title = "Slow"
    _fields = {"seconds": 5.0}
    cancelled = threading.Event()

    def OnRun(self, tree):
        deadline = time.monotonic() + self.GetField("seconds")
        while time.monotonic() < deadline:
            if self.CancelRequested():
                type(self).cancelled.set()
                return False
            time.sleep(0.005)
        return True


class RunningN(LeafNodeWidget):
    """Returns Status.RUNNING ``ticks`` times, then SUCCESS (GUI thread)."""

    _title = "Running N"
    _fields = {"ticks": 2}
    RUN_IN_THREAD = False

    def OnStart(self, tree):
        self.remaining = self.GetField("ticks")

    def OnRun(self, tree):
        if self.remaining > 0:
            self.remaining -= 1
            return Status.RUNNING
        return Status.SUCCESS


class Recorder(LeafNodeWidget):
    """Records its lifecycle calls into ``self.calls``."""

    _title = "Recorder"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls: list[tuple] = []

    def OnStart(self, tree):
        self.calls.append(("start",))

    def OnRun(self, tree):
        self.calls.append(("run",))
        return True

    def OnTerminate(self, tree, status):
        self.calls.append(("terminate", status.value))


class AllFields(LeafNodeWidget):
    """Declares one field of every supported type."""

    _title = "All Fields"
    _fields = {
        "count": 3,
        "ratio": 0.5,
        "label": "hello",
        "enabled": True,
        "choice": ["red", "green", "blue"],
    }
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class NoFields(LeafNodeWidget):
    _title = "No Fields"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


TEST_NODE_TYPES = [
    Succeed, FailNode, ThreadedSucceed, Raise, ReturnNone, SlowCancelable, RunningN, Recorder, AllFields, NoFields,
]


# ============================================================================ modal UI replacement
class Dialogs:
    """Configurable replacements for modal UI; records what was shown.

    Attributes tests may set:
        save_path / open_path: returned by QFileDialog.getSaveFileName / getOpenFileName ("" = cancel)
        question_answer: returned by QMessageBox.question
        text_answer: (text, ok) returned by QInputDialog.getText
        dialog_handler: callable(dialog) -> bool used for BehaviorTreeWidget/BlackboardView._run_dialog
            (fill the dialog's widgets, return True to accept); default rejects.
        menu_handler: callable(menu) called instead of QMenu.exec for view context menus.
    """

    def __init__(self):
        self.save_path = ""
        self.open_path = ""
        self.question_answer = QMessageBox.StandardButton.Discard
        self.text_answer = ("", False)
        self.dialog_handler = None
        self.menu_handler = None
        self.shown: list[tuple[str, tuple]] = []
        self.menus = []

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.shown]


@pytest.fixture(autouse=True)
def dialogs(monkeypatch) -> Dialogs:
    state = Dialogs()

    def message(kind):
        def show(*args, **kwargs):
            state.shown.append((kind, args))
            if kind == "question":
                return state.question_answer
            return QMessageBox.StandardButton.Ok

        return staticmethod(show)

    for kind in ("question", "warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, kind, message(kind))

    def get_save(*args, **kwargs):
        state.shown.append(("save_dialog", args))
        return (state.save_path, "")

    def get_open(*args, **kwargs):
        state.shown.append(("open_dialog", args))
        return (state.open_path, "")

    def get_text(*args, **kwargs):
        state.shown.append(("text_dialog", args))
        return state.text_answer

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(get_save))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(get_open))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(get_text))

    def run_dialog(self, dialog):
        state.shown.append(("dialog", (dialog,)))
        if state.dialog_handler is None:
            return False
        return bool(state.dialog_handler(dialog))

    monkeypatch.setattr(BehaviorTreeWidget, "_run_dialog", run_dialog)
    monkeypatch.setattr(BlackboardView, "_run_dialog", run_dialog)

    def exec_menu(self, menu, global_pos):
        state.menus.append(menu)
        if state.menu_handler is not None:
            state.menu_handler(menu)

    monkeypatch.setattr(TreeView, "_exec_menu", exec_menu)
    return state


# ============================================================================ widget fixtures
@pytest.fixture
def make_widget(qtbot):
    """Factory creating a shown BehaviorTreeWidget (not yet holding a created/loaded tree)."""
    created = []

    def make(node_types=None, size=(1200, 800)):
        types = list(DEMO_NODE_TYPES) + list(TEST_NODE_TYPES) if node_types is None else list(node_types)
        widget = BehaviorTreeWidget(node_types=types)
        qtbot.addWidget(widget)
        widget.resize(*size)
        widget.show()
        qtbot.wait(10)
        created.append(widget)
        return widget

    yield make
    for widget in created:
        try:
            widget._set_modified(False)
            widget.Shutdown()
        except RuntimeError:
            pass


@pytest.fixture
def bt(make_widget, tmp_path):
    """A shown BehaviorTreeWidget holding a new tree stored in tmp_path/tree.json."""
    widget = make_widget()
    assert widget.NewTree(str(tmp_path / "tree.json"))
    return widget


# ============================================================================ helpers
def viewport_point(bt: BehaviorTreeWidget, node, widget=None) -> QPoint:
    """Viewport coordinates of the centre of ``widget`` (a child of ``node``) or of the node."""
    target = widget if widget is not None else node
    local = target.mapTo(node, QPointF(target.width() / 2.0, target.height() / 2.0)) if target is not node else QPointF(
        node.width() / 2.0, node.height() / 2.0
    )
    return bt.view().mapFromScene(node._item.mapToScene(local))


def label_point(bt: BehaviorTreeWidget, node, kind: str) -> QPoint:
    """Viewport coordinates of the centre of a node's "parent" / "children" connection label."""
    return viewport_point(bt, node, node.connection_label(kind))


def drag(bt: BehaviorTreeWidget, start: QPoint, end: QPoint, steps: int = 4, release: bool = True) -> None:
    """Press at ``start``, move to ``end`` in ``steps`` and release (left button) on the viewport."""
    viewport = bt.view().viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    for step in range(1, steps + 1):
        point = start + (end - start) * (step / steps)
        QTest.mouseMove(viewport, QPoint(round(point.x()), round(point.y())))
    if release:
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)


def click(bt: BehaviorTreeWidget, point: QPoint, button=Qt.MouseButton.LeftButton) -> None:
    QTest.mouseClick(bt.view().viewport(), button, Qt.KeyboardModifier.NoModifier, point)


def run_until_idle(qtbot, bt: BehaviorTreeWidget, timeout: int = 5000) -> None:
    """Wait until an execution started with Execute() finished (run-once mode)."""
    qtbot.waitUntil(lambda: not bt.IsExecuting(), timeout=timeout)


def fast_config(bt: BehaviorTreeWidget, **changes) -> None:
    """Use a short tick interval (and optional other TreeConfig changes)."""
    config = bt.GetConfig()
    config.tick_interval_ms = changes.pop("tick_interval_ms", 5)
    for key, value in changes.items():
        setattr(config, key, value)
    bt.SetConfig(config)
