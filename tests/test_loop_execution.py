"""Tests of the "Loop Execution" check box of the execution controls.

Requirements (feature request 3):

* a check box "Loop Execution" in the controls frame, unchecked by default;
* checked: the tree is executed again immediately after the root returns, with the
  values in the nodes and in the blackboard kept as they were when the root returned
  (execution is seamless between loops);
* unchecked: execution stops once the root returns.

The check box shows and edits ``TreeConfig.repeat`` (the Configure dialog's
Execution option), which is saved with the tree.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QFrame

from behavior_tree_widget import LeafNodeWidget, NodeStatus, TreeConfig
from behavior_tree_widget.config import ConfigureDialog

from conftest import FailNode, Recorder, RunningN, fast_config, run_until_idle

SUCCEEDED, FAILED = "Succeeded", "Failed"


class LoopCounter(LeafNodeWidget):
    """Counts its runs in the blackboard entry "loops" and in its own field "runs"."""

    TYPE_NAME = "LoopTestCounter"
    _fields = {"runs": 0}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("loops") + 1 if tree.HasEntry("loops") else 1, "loops")
        self.SetField("runs", self.GetField("runs") + 1)
        self.attribute_runs = getattr(self, "attribute_runs", 0) + 1  # plain Python state
        return True


class TickCounter(LeafNodeWidget):
    """Succeeds every time; records the executor tick count of each run."""

    TYPE_NAME = "LoopTestTicks"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ticks: list[int] = []

    def OnRun(self, tree):
        self.ticks.append(tree.executor().tick_count())
        return True


# ============================================================================ helpers
def record(signal) -> list:
    values: list = []
    signal.connect(lambda *args: values.append(args[0] if len(args) == 1 else args))
    return values


def box(bt) -> QCheckBox:
    return bt.loopExecutionCheckBox()


def click_box(bt) -> None:
    """Click the check box as the user does."""
    QTest.mouseClick(box(bt), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(6, box(bt).height() // 2))


def leaf(bt, cls, x=0.0, y=220.0):
    node = bt.AddNode(cls, x, y)
    bt.Connect(bt.GetRootNode(), node)
    return node


# ============================================================================ the check box
def test_loop_execution_check_box_is_in_the_controls_frame(make_widget):
    widget = make_widget()
    frame = widget.widget(0).findChild(QFrame, "FrameExecution")
    found = frame.findChild(QCheckBox, "LoopExecution")
    assert found is not None and found is box(widget)
    assert found.text() == "Loop Execution"
    assert found.toolTip()


def test_loop_execution_is_unchecked_by_default(make_widget, tmp_path):
    widget = make_widget()
    assert not box(widget).isChecked()
    assert widget.GetLoopExecution() is False
    assert widget.NewTree(str(tmp_path / "new.json"))
    assert not box(widget).isChecked()
    assert widget.GetConfig().repeat is False


def test_check_box_is_disabled_until_a_tree_exists(make_widget, tmp_path):
    widget = make_widget()
    assert not box(widget).isEnabled()
    assert widget.NewTree(str(tmp_path / "new.json"))
    assert box(widget).isEnabled()


def test_clicking_the_check_box_switches_looping_on_and_off(bt):
    assert not bt.IsModified()
    changed = record(bt.modifiedChanged)
    click_box(bt)
    assert box(bt).isChecked()
    assert bt.GetLoopExecution() is True and bt.GetConfig().repeat is True
    assert changed == [True]  # saved with the tree: an edit
    click_box(bt)
    assert not box(bt).isChecked() and bt.GetLoopExecution() is False


def test_the_check_box_follows_the_configuration(bt, dialogs):
    bt.SetLoopExecution(True)
    assert box(bt).isChecked()
    bt.SetConfig(TreeConfig(repeat=False))
    assert not box(bt).isChecked()

    def choose_repeat(dialog):
        assert isinstance(dialog, ConfigureDialog)
        dialog.run_mode.setCurrentIndex(1)
        return True

    dialogs.dialog_handler = choose_repeat
    bt.Configure()
    assert box(bt).isChecked() and bt.GetLoopExecution()

    def check_dialog(dialog):
        assert dialog.run_mode.currentText() == ConfigureDialog.REPEAT_TEXT  # the dialog shows the same option
        return False

    dialogs.dialog_handler = check_dialog
    bt.Configure()


def test_the_check_box_stays_usable_while_executing(qtbot, bt):
    leaf(bt, RunningN).SetField("ticks", 10_000)
    fast_config(bt)
    bt.Execute()
    assert bt.IsExecuting()
    assert box(bt).isEnabled()
    click_box(bt)
    assert bt.GetLoopExecution()
    bt.Stop()


def test_loop_setting_is_saved_and_loaded(make_widget, bt, tmp_path):
    click_box(bt)
    path = tmp_path / "loop.json"
    assert bt.SaveTree(str(path))
    assert json.loads(path.read_text(encoding="utf-8"))["config"]["repeat"] is True

    other = make_widget()
    assert other.LoadTree(str(path))
    assert box(other).isChecked() and other.GetLoopExecution()
    assert not other.IsModified()
    assert other.NewTree(str(tmp_path / "fresh.json"))
    assert not box(other).isChecked()


# ============================================================================ looping
def test_checked_the_tree_is_executed_again_after_the_root_returns(qtbot, bt):
    recorder = leaf(bt, Recorder)
    finished = record(bt.executionFinished)
    fast_config(bt)
    click_box(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: recorder.calls.count(("run",)) >= 4, timeout=5000)
    assert bt.IsExecuting() and bt.GetExecutionState() == "Running"
    loops = recorder.calls.count(("run",))
    assert recorder.calls[: 3 * loops] == [("start",), ("run",), ("terminate", SUCCEEDED)] * loops
    assert finished == []  # execution never finishes while looping
    bt.Stop()
    assert not bt.IsExecuting()


def test_each_loop_starts_on_the_tick_after_the_root_returned(qtbot, bt):
    """The root returns on every tick, so every tick runs a new loop: no tick is spent idle."""
    counter = leaf(bt, TickCounter)
    fast_config(bt)
    bt.SetLoopExecution(True)
    bt.Execute()
    qtbot.waitUntil(lambda: len(counter.ticks) >= 6, timeout=5000)
    bt.Stop()
    assert counter.ticks[:6] == [0, 1, 2, 3, 4, 5]


def test_loops_keep_blackboard_and_node_values(qtbot, bt):
    counter = leaf(bt, LoopCounter)
    bt.SetEntry(0, "loops")
    fast_config(bt, restore_blackboard=True)  # restoring happens on Stop / Reset only
    bt.SetLoopExecution(True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetEntry("loops") >= 5, timeout=5000)
    bt.Pause()
    loops = bt.GetEntry("loops")
    assert counter.GetField("runs") == loops  # the field kept counting from loop to loop
    assert counter.attribute_runs == loops
    assert counter.GetStatus() is NodeStatus.SUCCEEDED
    bt.Stop()


def test_loops_continue_from_the_values_of_the_previous_loop(qtbot, bt):
    """A Set node and an Evaluation node: the value written by one loop is seen by the next."""
    bt.AddEntry("flag", "Bool", False)
    root = bt.GetRootNode()
    root.SetCompositeType("Selector")
    check = bt.AddNode("Evaluation", -150, 220)  # succeeds once the flag is set
    check.SetValueName("flag")
    check.SetLiteralValue(True)
    setter = bt.AddNode("Set", 150, 220)  # first loop only: sets the flag
    setter.SetValueName("flag")
    setter.SetLiteralValue(True)
    bt.Connect(root, check)
    bt.Connect(root, setter)
    statuses = []
    bt.executor().ticked.connect(lambda _: statuses.append((check.GetStatus().value, setter.GetStatus().value)))
    fast_config(bt)
    bt.SetLoopExecution(True)
    bt.Execute()
    qtbot.waitUntil(lambda: len(statuses) >= 3, timeout=5000)
    bt.Stop()
    assert statuses[0] == (FAILED, SUCCEEDED)  # loop 1: not set yet -> the Set node runs
    assert statuses[1][0] == SUCCEEDED and statuses[2][0] == SUCCEEDED  # later loops see the value


def test_looping_continues_after_the_root_fails(qtbot, bt):
    fail = leaf(bt, FailNode)
    runs = []
    bt.nodeStatusChanged.connect(lambda node, status: runs.append(status) if node is fail else None)
    finished = record(bt.executionFinished)
    fast_config(bt)
    bt.SetLoopExecution(True)
    bt.Execute()
    qtbot.waitUntil(lambda: runs.count(FAILED) >= 3, timeout=5000)
    assert bt.IsExecuting() and finished == []
    bt.Stop()


def test_stop_ends_looping_and_resets_the_nodes(qtbot, bt):
    counter = leaf(bt, LoopCounter)
    fast_config(bt)
    bt.SetLoopExecution(True)
    bt.Execute()
    qtbot.waitUntil(lambda: counter.GetField("runs") >= 2, timeout=5000)
    bt.Stop()
    runs = counter.GetField("runs")
    qtbot.wait(50)
    assert counter.GetField("runs") == runs
    assert not bt.IsExecuting()
    assert counter.GetStatus() is NodeStatus.READY


# ============================================================================ not looping
def test_unchecked_execution_stops_when_the_root_returns(qtbot, bt):
    recorder = leaf(bt, Recorder)
    finished = record(bt.executionFinished)
    fast_config(bt)
    assert not box(bt).isChecked()
    bt.Execute()
    run_until_idle(qtbot, bt)
    qtbot.wait(50)
    assert finished == [SUCCEEDED]
    assert recorder.calls == [("start",), ("run",), ("terminate", SUCCEEDED)]


def test_unchecking_while_looping_stops_after_the_current_loop(qtbot, bt):
    counter = bt.AddNode(LoopCounter, -150, 220)
    slow = bt.AddNode(RunningN, 150, 220)
    slow.SetField("ticks", 4)
    bt.Connect(bt.GetRootNode(), counter)
    bt.Connect(bt.GetRootNode(), slow)
    finished = record(bt.executionFinished)
    fast_config(bt)
    click_box(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: counter.GetField("runs") >= 2 and slow.GetStatus() is NodeStatus.RUNNING, timeout=5000)
    runs = counter.GetField("runs")
    click_box(bt)  # uncheck during a loop
    assert not bt.GetLoopExecution()
    run_until_idle(qtbot, bt)
    assert finished == [SUCCEEDED]  # the current loop was completed ...
    assert slow.GetStatus() is NodeStatus.SUCCEEDED
    assert counter.GetField("runs") == runs  # ... and no new one was started


def test_checking_while_running_makes_the_tree_loop(qtbot, bt):
    slow = leaf(bt, RunningN)
    slow.SetField("ticks", 3)
    starts = []
    bt.nodeStatusChanged.connect(lambda node, status: starts.append(status) if node is slow and status == "Running" else None)
    finished = record(bt.executionFinished)
    fast_config(bt)
    bt.Execute()
    assert bt.IsExecuting()
    click_box(bt)  # before the root returns
    qtbot.waitUntil(lambda: len(starts) >= 3, timeout=5000)
    assert bt.IsExecuting() and finished == []
    bt.Stop()


@pytest.mark.parametrize("loop", [False, True])
def test_set_loop_execution_api(bt, loop):
    bt.SetLoopExecution(loop)
    assert bt.GetLoopExecution() is loop
    assert box(bt).isChecked() is loop
    assert bt.GetConfig().repeat is loop

