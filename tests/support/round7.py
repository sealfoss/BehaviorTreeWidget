"""Helpers, node types and fixtures of the regression tests that came from
test_regressions_round7.py (the tests now live in the feature files that use them)."""

from __future__ import annotations

import threading

import pytest

from behavior_tree_widget import LeafNodeWidget, Status
from behavior_tree_widget.blackboard import FloatSpinBox

from conftest import tree_file


class Endless(LeafNodeWidget):
    TYPE_NAME = "R7Endless"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return Status.RUNNING


class Quick(LeafNodeWidget):
    TYPE_NAME = "R7Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class StopIssuer(LeafNodeWidget):
    """Worker leaf: the first OnRun call issues Stop and then waits until cancelled; later calls succeed."""

    TYPE_NAME = "R7StopIssuer"
    THREAD_WAIT = 0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = 0

    def OnRun(self, tree):
        self.calls += 1
        if self.calls == 1:
            tree.Stop()
            while not self.CancelRequested():
                threading.Event().wait(0.005)
            return False
        return True


class Holder(LeafNodeWidget):
    TYPE_NAME = "R7Holder"
    RUN_IN_THREAD = False
    _fields = {"path": ()}

    def OnRun(self, tree):
        return True


TYPES = (Endless, Quick, StopIssuer, Holder)


def build(widget, *types):
    root = widget.GetRootNode()
    nodes = []
    for index, node_type in enumerate(types):
        node = widget.AddNode(node_type, index * 320.0, 220.0)
        widget.Connect(root, node)
        nodes.append(node)
    return nodes


def other_tree_file(make_widget, path, node_type="R7Quick"):
    """Write a tree holding a root and one ``node_type`` leaf (named after ``path``); returns the
    path of its tree file (``<name>_BT.json``)."""
    other = make_widget()
    for cls in TYPES:
        other.RegisterNodeType(cls)
    assert other.NewTree(str(path))
    build(other, node_type)
    assert other.SaveTree(str(path))
    return tree_file(path)


# ============================================================================ Double editor
@pytest.fixture
def double_row(qtbot, widget):
    def make(value):
        widget.SetEntry(value, "d")
        widget._set_modified(False)
        widget.setCurrentIndex(1)
        widget.activateWindow()
        qtbot.waitUntil(widget.isActiveWindow, timeout=2000)
        spin = widget.blackboard_view().row("d").value_widget
        assert isinstance(spin, FloatSpinBox)
        spin.setFocus()
        qtbot.waitUntil(spin.hasFocus, timeout=1000)
        return spin

    return make


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


# ============================================================================ demo command line
_DEMO_DRIVER = r"""
import sys
from PySide6.QtWidgets import QApplication, QMessageBox
from behavior_tree_widget import demo
QMessageBox.critical = staticmethod(lambda *args: sys.stderr.write(repr(args[1:]) + "\n"))  # never block
windows = []
real = demo.DemoWindow
def make():
    window = real()
    windows.append(window)
    return window
demo.DemoWindow = make
QApplication.exec = lambda self: 0
demo.main(sys.argv[1:])
tree = windows[-1].tree
sys.stdout.buffer.write(repr(tree.GetFilePath()).encode("utf-8"))
tree._set_modified(False)
tree.Shutdown()
"""


# ============================================================================ files
def _nested(kind, depth):
    value = 1
    for _ in range(depth):
        if kind == "tuple":
            value = (value,)
        elif kind == "frozenset":
            value = frozenset([value])
        elif kind == "list":
            value = [value]
        elif kind == "dict":
            value = {"k": value}
        elif kind == "int-key":  # written as {"__dict__": [[key, value]]}
            value = {1: value}
        else:  # a tuple key is itself a level
            value = {(1, "a"): value} if isinstance(value, int) else {(1,): value}
    return value
