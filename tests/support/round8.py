"""Helpers, node types and fixtures of the regression tests that came from
test_regressions_round8.py (the tests now live in the feature files that use them)."""

from __future__ import annotations

import json
import threading

import pytest
from PySide6.QtCore import QFile

from behavior_tree_widget import LeafNodeWidget, NodeStatus, Status
from behavior_tree_widget.blackboard import FloatSpinBox

from conftest import fast_config, tree_file


class Quick(LeafNodeWidget):
    TYPE_NAME = "R8Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class Endless(LeafNodeWidget):
    TYPE_NAME = "R8Endless"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return Status.RUNNING


class RestartOnce(LeafNodeWidget):
    """Runs forever; when it is interrupted the first time, it starts the tree again."""

    TYPE_NAME = "R8RestartOnce"
    RUN_IN_THREAD = False
    restarted = False

    def OnRun(self, tree):
        return Status.RUNNING

    def OnTerminate(self, tree, status):
        if status is NodeStatus.READY and not type(self).restarted:
            type(self).restarted = True
            tree.Execute()


class Commander(LeafNodeWidget):
    """Worker leaf that sends ``command`` to ``target`` (another tree) and keeps running a while."""

    TYPE_NAME = "R8Commander"
    THREAD_WAIT = 0.0
    target = None
    command = "Execute"
    sent = threading.Event()

    def OnRun(self, tree):
        cls = type(self)
        if not cls.sent.is_set():
            getattr(cls.target, cls.command)()
            cls.sent.set()
        threading.Event().wait(0.15)
        return True


class Chooser(LeafNodeWidget):
    TYPE_NAME = "R8Chooser"
    RUN_IN_THREAD = False
    _fields = {"a": 1, "choices": ["x"], "b": 1}

    def OnRun(self, tree):
        return True


class Holder(LeafNodeWidget):
    TYPE_NAME = "R8Holder"
    RUN_IN_THREAD = False
    _fields = {"path": frozenset()}

    def OnRun(self, tree):
        return True


TYPES = (Quick, Endless, RestartOnce, Commander, Chooser, Holder)


def build(widget, *types):
    root = widget.GetRootNode()
    nodes = []
    for index, node_type in enumerate(types):
        node = widget.AddNode(node_type, index * 320.0, 220.0)
        widget.Connect(root, node)
        nodes.append(node)
    return nodes


def other_tree_file(make_widget, path, node_type="R8Endless"):
    """Write a tree holding a root and one ``node_type`` leaf (named after ``path``); returns the
    path of its tree file (``<name>_BT.json``)."""
    other = make_widget()
    for cls in TYPES:
        other.RegisterNodeType(cls)
    assert other.NewTree(str(path))
    build(other, node_type)
    assert other.SaveTree(str(path))
    return tree_file(path)


# ============================================================================ chained runs
@pytest.fixture
def chained(qtbot, widget):
    """A one-leaf tree whose runs finish in their first tick, restarted from executionFinished."""
    build(widget, Quick)
    fast_config(widget)
    runs = []
    chain = {"on": True}

    def on_finished(result):
        runs.append(result)
        if chain["on"]:
            widget.Execute()

    widget.executionFinished.connect(on_finished)
    widget.Execute()
    qtbot.waitUntil(lambda: len(runs) >= 5, timeout=3000)
    yield widget, runs
    chain["on"] = False
    widget.Stop()


def assert_no_more_runs(qtbot, runs):
    count = len(runs)
    qtbot.wait(100)
    assert len(runs) == count, "the chain went on"


# ============================================================================ Double editor
@pytest.fixture
def untracked(qtbot):
    def make(value):
        spin = FloatSpinBox()
        qtbot.addWidget(spin)
        spin.setKeyboardTracking(False)
        spin.set_float(value)
        spin.show()
        spin.activateWindow()
        qtbot.waitUntil(spin.isActiveWindow, timeout=2000)
        spin.setFocus()
        qtbot.waitUntil(spin.hasFocus, timeout=1000)
        reported = []
        spin.valueChanged.connect(lambda _value: reported.append(spin.float_value()))
        return spin, reported

    return make


@pytest.fixture
def double_row(qtbot, widget):
    def make(value):
        widget.SetEntry(value, "d")
        widget._set_modified(False)
        widget.setCurrentIndex(1)
        widget.activateWindow()
        qtbot.waitUntil(widget.isActiveWindow, timeout=2000)
        spin = widget.blackboard_view().row("d").value_widget
        spin.setFocus()
        qtbot.waitUntil(spin.hasFocus, timeout=1000)
        return spin

    return make


# ============================================================================ placeholders
def _nested_frozenset(depth):
    value = frozenset({1})
    for level in range(depth - 1):
        value = frozenset({value, level})
    return value


def _json_window():
    """A nesting depth that json.loads reads but json.dumps(indent=2) cannot write, or None."""

    def loads_ok(depth):
        try:
            json.loads("[" * depth + "]" * depth)
        except RecursionError:
            return False
        return True

    def dumps_ok(depth):
        value = []
        for _ in range(depth):
            value = [value]
        try:
            json.dumps(value, indent=2)
        except RecursionError:
            return False
        return True

    def limit(ok):
        low, high = 1, 200_000
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if ok(middle) else (low, middle - 1)
        return low

    dumps_limit, loads_limit = limit(dumps_ok), limit(loads_ok)
    if loads_limit - dumps_limit < 1000:
        return None
    return (dumps_limit + loads_limit) // 2


# ============================================================================ demo command line
def _round_trip(text):
    return QFile.decodeName(QFile.encodeName(text))
