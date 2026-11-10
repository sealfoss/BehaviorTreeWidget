"""Helpers, node types and fixtures of the regression tests that came from
test_regressions_round10.py (the tests now live in the feature files that use them)."""

from __future__ import annotations

import json
import threading
import time

import shiboken6

from behavior_tree_widget import LeafNodeWidget, Status
from behavior_tree_widget.serialization import FORMAT_NAME

from conftest import fast_config, file_blackboard


def read(path) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def legacy_tree(path, blackboard=()) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({
            "format": FORMAT_NAME,
            "version": 1,
            "nodes": [{"id": "root", "type": "Root", "title": "Root", "x": 0.0, "y": 0.0}],
            "connections": [],
            "blackboard": list(blackboard),
        }, stream)


# ============================================================================ Reset entries and the blackboard file
class Bump(LeafNodeWidget):
    """Raises "counter" to 3, then keeps running (the loop never ends by itself)."""

    RUN_IN_THREAD = False

    def OnRun(self, tree):
        if tree.GetEntry("counter") < 3:
            tree.SetEntry(tree.GetEntry("counter") + 1, "counter")
        return Status.RUNNING


def running(qtbot, bt):
    bt.RegisterNodeType(Bump)
    fast_config(bt)
    bt.AddEntry("counter", "Integer", 0, reset=True)
    bt.Connect(bt.GetRootNode(), bt.AddNode("Bump", 0, 200))
    assert bt.SaveTree(bt.GetFilePath())
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetEntry("counter") == 3, timeout=3000)
    qtbot.waitUntil(lambda: file_blackboard(bt.GetFilePath())[0]["value"] == 3, timeout=3000)


class ShutdownFromOnRun(LeafNodeWidget):
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(5, "counter")
        tree.Shutdown()
        return True


# ============================================================================ sub-tree nodes on worker threads
class Stubborn(LeafNodeWidget):
    """Ignores cancellation for a while on its worker thread, then touches its widget."""

    _fields = {"seconds": 0.4}
    lock = threading.Lock()
    active = 0
    most = 0
    touched: list = []
    errors: list = []

    def OnRun(self, tree):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.most = max(cls.most, cls.active)
        try:
            time.sleep(self.GetField("seconds"))
            try:
                cls.touched.append(shiboken6.isValid(self) and self.objectName() is not None)
            except RuntimeError as error:
                cls.errors.append(repr(error))
            return True
        finally:
            with cls.lock:
                cls.active -= 1


# ============================================================================ Promote to Sub-Tree
class Mark(LeafNodeWidget):
    RUN_IN_THREAD = False
    order: list = []

    def OnRun(self, tree):
        type(self).order.append(self.GetTitle())
        return True
