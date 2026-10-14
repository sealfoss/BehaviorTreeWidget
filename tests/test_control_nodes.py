"""Tests of the built-in Restart, Pause and Stop nodes.

Requirements (feature request "Add nodes to play, pause, and stop the behavior tree"):

* Restart: restarts the tree from the beginning (a node only runs while its tree plays,
  so "play" means "start over"); it is named Restart.
* Pause: pauses the tree when it is reached; Execute resumes it.
* Stop: stops the tree, as the Stop button does.
"""

from __future__ import annotations

import pytest

from behavior_tree_widget import (
    BehaviorTreeWidget,
    LeafNodeWidget,
    NodeStatus,
    PauseNodeWidget,
    RestartNodeWidget,
    StopNodeWidget,
)

from conftest import Recorder, fast_config, run_until_idle

READY = NodeStatus.READY
RUNNING = NodeStatus.RUNNING
SUCCEEDED = NodeStatus.SUCCEEDED


def runs(node: Recorder) -> int:
    return node.calls.count(("run",))


def sequence(bt: BehaviorTreeWidget, *type_names: str) -> list:
    """Connect a node of each type below the Root, left to right (the execution order)."""
    root = bt.GetRootNode()
    nodes = []
    for index, type_name in enumerate(type_names):
        node = bt.AddNode(type_name, -300 + 200 * index, 220)
        bt.Connect(root, node)
        nodes.append(node)
    return nodes


def record(signal) -> list:
    seen = []
    signal.connect(lambda *args: seen.append(args[0] if len(args) == 1 else args))
    return seen


# ============================================================================ node types
def test_control_nodes_are_built_in_types(bt):
    names = bt.GetNodeTypes()
    assert names[names.index("Set") + 1 : names.index("Set") + 4] == ["Restart", "Pause", "Stop"]
    assert bt.GetNodeType("Restart") is RestartNodeWidget
    assert bt.GetNodeType("Pause") is PauseNodeWidget
    assert bt.GetNodeType("Stop") is StopNodeWidget


def test_add_menu_lists_the_control_nodes_in_their_own_group(bt):
    entries = bt._node_menu_entries()
    groups = [group for group, _, _ in entries]
    execution = [(name, label) for group, name, label in entries if group == "execution"]
    assert execution == [("Restart", "Restart"), ("Pause", "Pause"), ("Stop", "Stop")]
    assert groups.index("execution") > groups.index("blackboard")
    assert groups.index("execution") < groups.index("leaf")


@pytest.mark.parametrize("name", ["Restart", "Pause", "Stop"])
def test_control_node_names_are_reserved(bt, name):
    custom = type("Custom", (LeafNodeWidget,), {"TYPE_NAME": name, "OnRun": lambda self, tree: True})
    with pytest.raises(ValueError, match="built-in node type name"):
        bt.RegisterNodeType(custom)


def test_control_nodes_are_saved_and_loaded(bt, tmp_path):
    sequence(bt, "Restart", "Pause", "Stop")
    path = str(tmp_path / "control.json")
    assert bt.SaveTree(path)
    assert bt.LoadTree(path)
    children = bt.GetRootNode().GetChildren()
    assert [type(node) for node in children] == [RestartNodeWidget, PauseNodeWidget, StopNodeWidget]


# ============================================================================ Pause
def test_pause_node_pauses_the_tree_before_the_next_node_runs(qtbot, bt):
    first, pause, last = sequence(bt, "Recorder", "Pause", "Recorder")
    fast_config(bt)
    finished = record(bt.executionFinished)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetExecutionState() == "Paused", timeout=2000)
    qtbot.wait(30)  # stays paused
    assert bt.GetExecutionState() == "Paused"
    assert runs(first) == 1 and runs(last) == 0
    assert pause.GetStatus() is RUNNING
    assert finished == []

    bt.Execute()  # resume
    run_until_idle(qtbot, bt)
    assert pause.GetStatus() is SUCCEEDED
    assert runs(first) == 1 and runs(last) == 1
    assert finished == ["Succeeded"]


def test_pause_node_pauses_again_after_stop_and_execute(qtbot, bt):
    first, pause, last = sequence(bt, "Recorder", "Pause", "Recorder")
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetExecutionState() == "Paused", timeout=2000)
    bt.Stop()
    assert pause.GetStatus() is READY
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetExecutionState() == "Paused", timeout=2000)
    assert runs(first) == 2 and runs(last) == 0
    bt.Stop()


def test_pause_node_pauses_every_loop(qtbot, bt):
    pause, last = sequence(bt, "Pause", "Recorder")
    fast_config(bt)
    bt.SetLoopExecution(True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetExecutionState() == "Paused", timeout=2000)
    assert runs(last) == 0
    bt.Execute()
    qtbot.waitUntil(lambda: runs(last) == 1 and bt.GetExecutionState() == "Paused", timeout=2000)
    qtbot.wait(30)
    assert runs(last) == 1
    bt.Stop()


# ============================================================================ Stop
def test_stop_node_stops_the_tree_like_the_stop_button(qtbot, bt):
    first, stop, last = sequence(bt, "Recorder", "Stop", "Recorder")
    fast_config(bt)
    states = record(bt.executionStateChanged)
    finished = record(bt.executionFinished)
    bt.Execute()
    qtbot.waitUntil(lambda: not bt.IsExecuting(), timeout=2000)
    assert states == ["Running", "Idle"]
    assert finished == []  # stopped, not finished
    assert runs(first) == 1 and runs(last) == 0
    assert first.calls[-1] == ("terminate", "Succeeded")
    assert all(node.GetStatus() is READY for node in bt.GetNodes())


# ============================================================================ Restart
def test_restart_node_starts_the_tree_over_without_stopping(qtbot, bt):
    first, restart, last = sequence(bt, "Recorder", "Restart", "Recorder")
    fast_config(bt)
    states = record(bt.executionStateChanged)
    bt.Execute()
    qtbot.waitUntil(lambda: runs(first) >= 3, timeout=2000)
    assert bt.GetExecutionState() == "Running"
    assert states == ["Running"]  # never idle in between
    assert runs(last) == 0  # nothing after the Restart node runs
    bt.Stop()
    assert all(node.GetStatus() is READY for node in bt.GetNodes())


def test_restart_node_restores_the_blackboard_when_configured(qtbot, bt):
    bt.AddEntry("count", "Integer", 0)
    seen = []

    class Count(LeafNodeWidget):
        RUN_IN_THREAD = False

        def OnRun(self, tree):
            seen.append(tree.GetEntry("count"))
            tree.SetEntry(tree.GetEntry("count") + 1, "count")
            return True

    bt.RegisterNodeType(Count)
    sequence(bt, "Count", "Restart")
    fast_config(bt, restore_blackboard=True)
    bt.Execute()
    qtbot.waitUntil(lambda: len(seen) >= 3, timeout=2000)
    bt.Stop()
    assert seen[:3] == [0, 0, 0]  # every restart begins with the values the run started with
