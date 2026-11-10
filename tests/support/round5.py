"""Helpers, node types and fixtures of the regression tests that came from
test_regressions_round5.py (the tests now live in the feature files that use them)."""

from __future__ import annotations

import threading

from behavior_tree_widget import LeafNodeWidget


class Quick(LeafNodeWidget):
    TYPE_NAME = "R5Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class Endless(LeafNodeWidget):
    TYPE_NAME = "R5Endless"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = 0

    def OnRun(self, tree):
        from behavior_tree_widget import Status

        self.runs += 1
        return Status.RUNNING


class Emitter(LeafNodeWidget):
    """Worker leaf that issues ``command`` once, then waits until cancelled."""

    TYPE_NAME = "R5Emitter"
    THREAD_WAIT = 0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.command = "Pause"
        self.issued = threading.Event()
        self.calls = 0

    def OnRun(self, tree):
        self.calls += 1
        if self.calls == 1:
            getattr(tree, self.command)()
            self.issued.set()
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


# ============================================================================ display robustness
class BrokenRepr:
    def __repr__(self):
        raise RuntimeError("no repr")

    __str__ = __repr__
