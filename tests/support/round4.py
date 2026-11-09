"""Helpers, node types and fixtures of the regression tests that came from
test_regressions_round4.py (the tests now live in the feature files that use them)."""

from __future__ import annotations

import threading

from behavior_tree_widget import LeafNodeWidget


class Quick(LeafNodeWidget):
    """Finishes within the first tick (GUI thread)."""

    TYPE_NAME = "R4Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class GatedWorker(LeafNodeWidget):
    """Worker-thread leaf that blocks until ``gate`` opens, then runs ``after(tree)``."""

    TYPE_NAME = "R4Gated"
    THREAD_WAIT = 0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.after = None
        self.errors: list[BaseException] = []
        self.done = threading.Event()

    def OnRun(self, tree):
        self.entered.set()
        self.gate.wait(10)
        try:
            if self.after is not None:
                self.after(tree)
        except BaseException as error:  # noqa: BLE001 - recorded for the test
            self.errors.append(error)
        finally:
            self.done.set()
        return True


class Writer(LeafNodeWidget):
    TYPE_NAME = "R4Writer"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("x") + 1, "x")
        return True


def build(widget, *types):
    root = widget.GetRootNode()
    nodes = []
    for index, node_type in enumerate(types):
        node = widget.AddNode(node_type, index * 320.0, 220.0)
        widget.Connect(root, node)
        nodes.append(node)
    return nodes


# ============================================================================ data edge cases
class BrokenRepr:
    def __repr__(self):
        raise RuntimeError("no repr")


class PreTitled(LeafNodeWidget):
    TYPE_NAME = "R4PreTitled"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        self._title = 123  # a non-str title assigned before super().__init__()
        super().__init__(parent)

    def OnRun(self, tree):
        return True
