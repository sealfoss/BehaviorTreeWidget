"""The status shown by every node after each tick, refreshed only for the nodes that ticked or were
running (TreeExecutor._refresh_statuses / behaviours.TickVisitor)."""

from __future__ import annotations

import json
import os
import random

import pytest

from behavior_tree_widget import LeafNodeWidget, LogLevel, NodeStatus, Status
from behavior_tree_widget.serialization import FORMAT_NAME
from behavior_tree_widget.subtree import SUB_TREE

from conftest import run_until_idle


class Scripted(LeafNodeWidget):
    """GUI-thread leaf returning the next result of its random script (RUNNING / SUCCESS / FAILURE)."""

    _title = "Scripted"
    _fields = {"seed": 0}
    RUN_IN_THREAD = False

    def OnStart(self, tree):
        if not hasattr(self, "rng"):
            self.rng = random.Random(self.GetField("seed"))

    def OnRun(self, tree):
        return self.rng.choice([Status.RUNNING, Status.RUNNING, Status.SUCCESS, Status.FAILURE])


class Toggle(LeafNodeWidget):
    """GUI-thread condition returning ``Toggle.result``."""

    _title = "Toggle"
    RUN_IN_THREAD = False
    result = False

    def OnRun(self, tree):
        return Toggle.result


class Forever(LeafNodeWidget):
    _title = "Forever"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return Status.RUNNING


class Once(LeafNodeWidget):
    _title = "Once"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


@pytest.fixture
def tree(bt):
    for cls in (Scripted, Toggle, Forever, Once):
        bt.RegisterNodeType(cls)
    config = bt.GetConfig()
    config.tick_interval_ms = 60000  # the test ticks by hand
    bt.SetConfig(config)
    return bt


def shown(bt) -> dict:
    return {node: (node.GetStatus(), node.GetError()) for node in bt.GetNodes()}


def assert_complete(bt) -> None:
    """A full refresh after the tick changes nothing: the incremental one missed no node."""
    before = shown(bt)
    executor = bt.executor()
    executor.invalidate_shown_twins()
    executor._refresh_statuses()
    after = shown(bt)
    missed = {node.GetTitle(): (before[node], after[node]) for node in before if before[node] != after[node]}
    assert missed == {}


def random_tree(bt, rng: random.Random) -> None:
    root = bt.GetRootNode()
    root.SetCompositeType(rng.choice(["Sequence", "Selector"]))
    root.SetMemory(rng.random() < 0.5)
    x = [0.0]

    def add(parent, depth):
        for _ in range(rng.randint(1, 4)):
            x[0] += 230.0
            kind = rng.random()
            if depth < 3 and kind < 0.3:
                node = bt.AddNode(rng.choice(["Sequence", "Selector"]), x[0], 200.0 * depth)
                node.SetMemory(rng.random() < 0.5)
                bt.Connect(parent, node)
                add(node, depth + 1)
            elif depth < 3 and kind < 0.4:
                node = bt.AddNode("Negation", x[0], 200.0 * depth)
                bt.Connect(parent, node)
                leaf = bt.AddNode("Scripted", x[0], 200.0 * (depth + 1))
                leaf.SetField("seed", rng.randrange(10**6))
                bt.Connect(node, leaf)
            else:
                leaf = bt.AddNode("Scripted", x[0], 200.0 * depth)
                leaf.SetField("seed", rng.randrange(10**6))
                bt.Connect(parent, leaf)

    add(root, 1)


@pytest.mark.parametrize("level", [LogLevel.NONE, LogLevel.INFO], ids=["log-off", "log-info"])
@pytest.mark.parametrize("seed", range(12))
def test_every_node_shows_what_a_full_refresh_would_show(tree, seed, level):
    rng = random.Random(seed)
    random_tree(tree, rng)
    tree.SetLoopExecution(True)
    tree.SetLogLevel(level)
    tree.Execute()  # first tick
    assert_complete(tree)
    executor = tree.executor()
    for step in range(60):
        if step == 25:
            tree.Reset()  # starts over from the Root
        elif step == 40:
            tree.Pause()
            tree.Execute()
        executor._tick()
        assert_complete(tree)
    tree.Stop()
    assert all(node.GetStatus() is NodeStatus.READY for node in tree.GetNodes())


@pytest.mark.parametrize("level", [LogLevel.NONE, LogLevel.INFO], ids=["log-off", "log-info"])
def test_a_preempted_node_goes_back_to_ready(tree, level):
    tree.SetLogLevel(level)
    root = tree.GetRootNode()
    root.SetCompositeType("Selector")
    root.SetMemory(False)
    Toggle.result = False
    toggle = tree.AddNode("Toggle", 0.0, 200.0)
    forever = tree.AddNode("Forever", 300.0, 200.0)
    tree.Connect(root, toggle)
    tree.Connect(root, forever)
    tree.Execute()
    assert (toggle.GetStatus(), forever.GetStatus()) == (NodeStatus.FAILED, NodeStatus.RUNNING)
    changed = []
    tree.nodeStatusChanged.connect(lambda node, status: changed.append((node.GetTitle(), status)))
    Toggle.result = True
    tree.executor()._tick()
    assert (toggle.GetStatus(), forever.GetStatus()) == (NodeStatus.SUCCEEDED, NodeStatus.READY)
    # Signals come in the order the tree was built (the interrupted node between its siblings).
    assert changed == [("Toggle", "Succeeded"), ("Forever", "Ready"), ("Root", "Succeeded")]


def test_only_nodes_that_ticked_or_ran_are_refreshed(tree):
    """A memory Sequence resumes at its running child: the finished children are not looked at."""
    once = tree.AddNode("Once", 0.0, 200.0)
    forever = tree.AddNode("Forever", 300.0, 200.0)
    tree.Connect(tree.GetRootNode(), once)
    tree.Connect(tree.GetRootNode(), forever)
    tree.Execute()
    assert once.GetStatus() is NodeStatus.SUCCEEDED
    once._set_status(NodeStatus.READY)  # shown wrongly, behind the executor's back
    tree.executor()._tick()
    assert once.GetStatus() is NodeStatus.READY, "a node that did not tick is not refreshed"
    tree.executor().invalidate_shown_twins()
    tree.executor()._tick()
    assert once.GetStatus() is NodeStatus.SUCCEEDED, "invalidate_shown_twins refreshes every node"


# ============================================================================ sub-trees shown with Zoom In
def write_tree(path, nodes, connections) -> str:
    root = {"id": "root", "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True}
    document = {
        "format": FORMAT_NAME,
        "version": 1,
        "config": {"tick_interval_ms": 100, "repeat": False, "restore_blackboard": False, "default_memory": True},
        "nodes": [root, *nodes],
        "connections": [{"parent": parent, "child": child} for parent, child in connections],
    }
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(document, stream)
    return str(path)


def by_id(nodes, node_id):
    return next(node for node in nodes if node.GetId() == node_id)


def test_zooming_in_while_executing_shows_the_nodes_that_already_finished(tree, tmp_path, qtbot):
    write_tree(
        tmp_path / "inner_BT.json",
        [
            {"id": "once", "type": "Once", "title": "Once", "x": 0.0, "y": 200.0},
            {"id": "forever", "type": "Forever", "title": "Forever", "x": 300.0, "y": 200.0},
        ],
        [("root", "once"), ("root", "forever")],
    )
    sub = tree.AddNode(SUB_TREE, 0.0, 200.0)
    sub.SetFile("inner_BT.json")
    tree.Connect(tree.GetRootNode(), sub)
    tree.Execute()
    tree.executor()._tick()
    assert sub.GetStatus() is NodeStatus.RUNNING
    tree.ZoomIn(sub)
    once, forever = by_id(tree.GetNodes(), "once"), by_id(tree.GetNodes(), "forever")
    assert once.GetStatus() is NodeStatus.READY  # not refreshed yet
    tree.executor()._tick()  # "once" does not tick any more (memory), but the sub-tree is new
    assert (once.GetStatus(), forever.GetStatus()) == (NodeStatus.SUCCEEDED, NodeStatus.RUNNING)
    tree.GoBack(save=False)
    tree.executor()._tick()
    tree.ZoomIn(sub)  # a new view with new nodes
    once, forever = by_id(tree.GetNodes(), "once"), by_id(tree.GetNodes(), "forever")
    tree.executor()._tick()
    assert (once.GetStatus(), forever.GetStatus()) == (NodeStatus.SUCCEEDED, NodeStatus.RUNNING)
    tree.Stop()
    assert (once.GetStatus(), forever.GetStatus()) == (NodeStatus.READY, NodeStatus.READY)


def test_a_finished_run_of_a_shown_sub_tree_is_shown_completely(tree, tmp_path, qtbot):
    write_tree(
        tmp_path / "inner_BT.json",
        [{"id": "once", "type": "Once", "title": "Once", "x": 0.0, "y": 200.0}],
        [("root", "once")],
    )
    sub = tree.AddNode(SUB_TREE, 0.0, 200.0)
    sub.SetFile(os.path.join(str(tmp_path), "inner_BT.json"))
    tree.Connect(tree.GetRootNode(), sub)
    tree.ZoomIn(sub)
    shown_once = by_id(tree.GetNodes(), "once")
    config = tree.GetConfig()
    config.tick_interval_ms = 5
    tree.SetConfig(config)
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert shown_once.GetStatus() is NodeStatus.SUCCEEDED
    assert tree.GetRootNode().GetStatus() is NodeStatus.SUCCEEDED
