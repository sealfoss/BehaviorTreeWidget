"""Helpers, node types and fixtures of the regression tests that came from
test_regressions_round6.py (the tests now live in the feature files that use them)."""

from __future__ import annotations

import sys
import threading

import pytest

from behavior_tree_widget import LeafNodeWidget, Status
from behavior_tree_widget.blackboard import FloatSpinBox


DBL_MAX = sys.float_info.max


class Endless(LeafNodeWidget):
    TYPE_NAME = "R6Endless"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return Status.RUNNING


class Quick(LeafNodeWidget):
    TYPE_NAME = "R6Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class StopIssuer(LeafNodeWidget):
    """Worker leaf: the first OnRun call issues Stop and then waits until cancelled; later calls succeed."""

    TYPE_NAME = "R6StopIssuer"
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


def build(widget, *types):
    root = widget.GetRootNode()
    nodes = []
    for index, node_type in enumerate(types):
        node = widget.AddNode(node_type, index * 320.0, 220.0)
        widget.Connect(root, node)
        nodes.append(node)
    return nodes


def in_thread(function):
    thread = threading.Thread(target=function, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()


# ============================================================================ files
_ENCODE_SCRIPT = """
import json, sys
from behavior_tree_widget.serialization import encode_value
value = {
    "words": {"alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"},
    "mixed": frozenset({1, 2.5, "x", (1, "a"), ("b", 2), frozenset({"p", "q"}), None, True}),
    # the repr of these depends on the hash seed
    "nested": {frozenset(pair) for pair in ("ab", "ac", "ad", "bc", "bd", "cd", "ae", "be", "ce", "de")},
}
sys.stdout.write(json.dumps(encode_value(value), sort_keys=True))
"""


# ============================================================================ Double entry editor
@pytest.fixture
def double_row(qtbot, widget):
    """Returns make(value) -> the focused FloatSpinBox of a Double entry "d" holding ``value``."""

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
