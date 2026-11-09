"""The Reset check box of blackboard entries.

Requirement: every blackboard entry has a "Reset" check box. When it is checked, at the end
of the execution loop the entry's value is reset to the value it had at the beginning of
that execution loop.
"""

from __future__ import annotations

import json
import threading

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox

from behavior_tree_widget import LeafNodeWidget, Status
from behavior_tree_widget.blackboard import ENTRY_TYPES, BlackboardStore

from conftest import assert_blackboard_in_file, fast_config, run_until_idle


class Bump(LeafNodeWidget):
    """Adds 1 to the "count" entry and records the value it saw first."""

    RUN_IN_THREAD = False
    seen: list = []

    def OnRun(self, tree):
        type(self).seen.append(tree.GetEntry("count"))
        tree.SetEntry(tree.GetEntry("count") + 1, "count")
        return True


class BumpTwiceAcrossTicks(LeafNodeWidget):
    """Adds 1 to "count" on each of two ticks, then succeeds (the loop spans several ticks)."""

    RUN_IN_THREAD = False

    def OnStart(self, tree):
        self.left = 2

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("count") + 1, "count")
        self.left -= 1
        return Status.RUNNING if self.left else True


class Forever(LeafNodeWidget):
    """Adds 1 to "count" and keeps running."""

    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("count") + 1, "count")
        return Status.RUNNING


class ThreadedBump(LeafNodeWidget):
    """Adds 1 to "count" on a worker thread."""

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("count") + 1, "count")
        return True


@pytest.fixture
def tree(bt):
    for cls in (Bump, BumpTwiceAcrossTicks, Forever, ThreadedBump):
        bt.RegisterNodeType(cls)
    Bump.seen = []
    fast_config(bt)
    return bt


def chain(bt, *types):
    nodes = []
    for index, type_name in enumerate(types):
        node = bt.AddNode(type_name, index * 220, 200)
        bt.Connect(bt.GetRootNode(), node)
        nodes.append(node)
    return nodes


# ============================================================================ the check box
@pytest.mark.parametrize("type_name", [name for name, spec in ENTRY_TYPES.items()])
def test_every_entry_type_has_a_reset_check_box(bt, type_name):
    bt.AddEntry("entry", type_name, None if type_name != "Object" else object())
    row = bt.blackboardView().row("entry")
    assert isinstance(row.reset_box, QCheckBox)
    assert row.reset_box.text() == "Resets"
    assert row.reset_box.isVisibleTo(row)
    assert not row.reset_box.isChecked()
    assert "loop" in row.reset_box.toolTip()


def test_the_header_names_the_reset_column(bt):
    from PySide6.QtWidgets import QLabel

    label = bt.blackboardView().findChild(QLabel, "LabelEntryReset")
    assert label is not None and label.text() == "Resets"


def test_checking_the_box_sets_the_flag(bt):
    bt.AddEntry("count", "Integer", 0)
    box = bt.blackboardView().row("count").reset_box
    box.setChecked(True)
    assert bt.GetEntryReset("count")
    box.setChecked(False)
    assert not bt.GetEntryReset("count")


def test_the_box_follows_the_flag_set_from_code(qtbot, bt):
    bt.AddEntry("count", "Integer", 0)
    bt.SetEntryReset("count", True)
    assert bt.blackboardView().row("count").reset_box.isChecked()
    thread = threading.Thread(target=lambda: bt.SetEntryReset("count", False))
    thread.start()
    thread.join(5)
    qtbot.waitUntil(lambda: not bt.blackboardView().row("count").reset_box.isChecked(), timeout=2000)
    assert not bt.GetEntryReset("count")


def test_add_entry_can_check_the_box(bt):
    bt.AddEntry("count", "Integer", 0, reset=True)
    assert bt.GetEntryReset("count")
    assert bt.blackboardView().row("count").reset_box.isChecked()


def test_the_flag_needs_an_existing_entry(bt):
    with pytest.raises(KeyError):
        bt.GetEntryReset("missing")
    with pytest.raises(KeyError):
        bt.SetEntryReset("missing", True)


def test_the_flag_follows_a_renamed_entry_and_goes_with_a_removed_one(bt):
    bt.AddEntry("count", "Integer", 0, reset=True)
    bt.blackboardStore().rename("count", "total")
    assert bt.GetEntryReset("total")
    assert bt.blackboardView().row("total").reset_box.isChecked()
    bt.RemoveEntry("total")
    bt.AddEntry("total", "Integer", 0)
    assert not bt.GetEntryReset("total")


def test_the_flag_survives_a_change_of_type(bt):
    bt.AddEntry("value", "Integer", 0, reset=True)
    bt.blackboardStore().replace("value", "String", "text")
    assert bt.GetEntryReset("value")
    assert bt.blackboardView().row("value").reset_box.isChecked()  # the new row shows it too


# ============================================================================ saving
def test_the_flag_is_written_to_the_blackboard_file(bt):
    bt.AddEntry("count", "Integer", 0)
    bt.AddEntry("other", "Integer", 0)
    bt.blackboardView().row("count").reset_box.setChecked(True)
    assert_blackboard_in_file(bt)
    with open(bt.GetBlackboardFilePath(), encoding="utf-8") as stream:
        items = json.load(stream)["blackboard"]
    assert items == [
        {"name": "count", "type": "Integer", "value": 0, "reset": True},
        {"name": "other", "type": "Integer", "value": 0},
    ]
    assert not bt.IsModified()  # like value changes, not an unsaved change


def test_the_flag_is_loaded(make_widget, bt, tmp_path):
    bt.AddEntry("count", "Integer", 0, reset=True)
    assert bt.SaveTree(str(tmp_path / "flags"))
    other = make_widget()
    assert other.LoadTree(str(tmp_path / "flags_BT.json"))
    assert other.GetEntryReset("count")
    assert other.blackboardView().row("count").reset_box.isChecked()


def test_store_parse_items_reads_the_flags():
    entries, reset, problems = BlackboardStore.parse_items(
        [{"name": "a", "type": "Integer", "value": 1, "reset": True}, {"name": "b", "type": "Integer", "value": 2}]
    )
    assert entries == [("a", "Integer", 1), ("b", "Integer", 2)]
    assert reset == {"a"} and problems == []
    assert BlackboardStore.parse_list([{"name": "a", "type": "Integer", "value": 1, "reset": True}]) == (
        [("a", "Integer", 1)],
        [],
    )


# ============================================================================ run once
def test_a_flagged_entry_is_reset_when_the_run_ends(qtbot, tree):
    tree.AddEntry("count", "Integer", 5, reset=True)
    tree.AddEntry("total", "Integer", 0)
    chain(tree, "Bump", "Bump")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert Bump.seen == [5, 6]  # the tree saw its changes during the loop ...
    assert tree.GetEntry("count") == 5  # ... and the value is back at the end
    assert_blackboard_in_file(tree)


def test_an_unflagged_entry_keeps_its_value(qtbot, tree):
    tree.AddEntry("count", "Integer", 5)
    chain(tree, "Bump", "Bump")
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert tree.GetEntry("count") == 7


def test_the_reset_happens_before_execution_finished_is_emitted(qtbot, tree):
    tree.AddEntry("count", "Integer", 5, reset=True)
    chain(tree, "Bump")
    seen = []
    tree.executionFinished.connect(lambda result: seen.append(tree.GetEntry("count")))
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert seen == [5]


def test_the_reset_is_not_an_edit_of_the_tree(qtbot, tree):
    tree.AddEntry("count", "Integer", 5, reset=True)
    chain(tree, "Bump")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert not tree.IsModified()


def test_a_loop_spanning_several_ticks(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "BumpTwiceAcrossTicks")
    values = []
    tree.executor().ticked.connect(lambda _count: values.append(tree.GetEntry("count")))
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert values[0] == 1  # changed inside the loop
    assert tree.GetEntry("count") == 0


# ============================================================================ loop execution
def test_each_loop_starts_from_the_loop_start_value(qtbot, tree):
    tree.AddEntry("count", "Integer", 10, reset=True)
    chain(tree, "Bump", "Bump")
    tree.SetLoopExecution(True)
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: len(Bump.seen) >= 8, timeout=5000)
    finally:
        tree.Stop()
    assert Bump.seen[:8] == [10, 11] * 4
    assert tree.GetEntry("count") == 10


def test_without_the_flag_values_carry_over_between_loops(qtbot, tree):
    tree.AddEntry("count", "Integer", 10)
    chain(tree, "Bump")
    tree.SetLoopExecution(True)
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: len(Bump.seen) >= 4, timeout=5000)
    finally:
        tree.Stop()
    assert Bump.seen[:4] == [10, 11, 12, 13]


def test_a_value_set_between_loops_is_the_next_loop_start_value(qtbot, tree):
    tree.AddEntry("count", "Integer", 10, reset=True)
    chain(tree, "Bump")
    tree.SetLoopExecution(True)

    def edit_once(_count):
        if len(Bump.seen) == 2 and tree.GetEntry("count") == 10:
            tree.SetEntry(50, "count")  # between two loops (the root returned in this tick)

    tree.executor().ticked.connect(edit_once)
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: len(Bump.seen) >= 5, timeout=5000)
    finally:
        tree.Stop()
    assert Bump.seen[:5] == [10, 10, 50, 50, 50]


# ============================================================================ Stop, Reset, Restart
def test_stop_ends_the_loop_and_resets(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Forever")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 3, timeout=5000)
    tree.Stop()
    assert tree.GetEntry("count") == 0
    assert_blackboard_in_file(tree)


def test_reset_while_running_starts_a_new_loop(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Forever")
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: tree.GetEntry("count") >= 3, timeout=5000)
        tree.Reset()
        assert tree.GetEntry("count") == 0
        assert tree.IsExecuting()
        qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)  # the new loop changes it again
        tree.SetEntry(100, "count")  # a change during the new loop is reset too
    finally:
        tree.Stop()
    assert tree.GetEntry("count") == 0


def test_a_restart_node_resets(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Bump", "Restart")
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: len(Bump.seen) >= 4, timeout=5000)
    finally:
        tree.Stop()
    assert Bump.seen[:4] == [0, 0, 0, 0]


def test_pause_does_not_end_the_loop(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Bump", "Pause", "Bump")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetExecutionState() == "Paused", timeout=5000)
    assert tree.GetEntry("count") == 1  # still the loop's value while paused
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert Bump.seen == [0, 1]
    assert tree.GetEntry("count") == 0


def test_a_tick_error_ends_the_loop(qtbot, tree, monkeypatch):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
    import py_trees

    monkeypatch.setattr(py_trees.trees.BehaviourTree, "tick", lambda self, *a, **k: 1 / 0)
    run_until_idle(qtbot, tree)
    assert tree.GetEntry("count") == 0


# ============================================================================ flags, entries and threads
def test_checking_the_box_during_a_loop_uses_the_loop_start_value(qtbot, tree):
    tree.AddEntry("count", "Integer", 0)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 3, timeout=5000)
    tree.blackboardView().row("count").reset_box.setChecked(True)
    tree.Stop()
    assert tree.GetEntry("count") == 0


def test_unchecking_the_box_during_a_loop_keeps_the_value(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 3, timeout=5000)
    tree.SetEntryReset("count", False)
    tree.Stop()
    assert tree.GetEntry("count") >= 3


def test_user_edits_during_a_loop_are_reset_too(qtbot, tree):
    tree.AddEntry("count", "Integer", 0)
    tree.AddEntry("mode", "String", "idle", reset=True)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 1, timeout=5000)
    tree.blackboardView().row("mode").value_widget.setText("busy")
    assert tree.GetEntry("mode") == "busy"
    tree.Stop()
    assert tree.GetEntry("mode") == "idle"


def test_edits_while_idle_are_not_reset(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Bump")
    tree.Execute()
    run_until_idle(qtbot, tree)
    tree.SetEntry(42, "count")
    tree.Reset()  # idle: no loop in progress
    assert tree.GetEntry("count") == 42
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert Bump.seen == [0, 42]
    assert tree.GetEntry("count") == 42


def test_worker_thread_changes_are_reset(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "ThreadedBump", "ThreadedBump")
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert tree.GetEntry("count") == 0


def test_a_renamed_entry_is_reset_under_its_new_name(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
    tree.Pause()
    tree.blackboardStore().rename("count", "renamed")
    tree.SetEntry(99, "renamed")
    tree.Stop()
    assert tree.GetEntry("renamed") == 0


def test_an_entry_retyped_during_the_loop_gets_its_type_back(qtbot, tree):
    tree.AddEntry("count", "Integer", 0)
    tree.AddEntry("value", "Integer", 3, reset=True)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 1, timeout=5000)
    tree.blackboardStore().replace("value", "String", "three")
    tree.Stop()
    assert tree.GetEntryType("value") == "Integer" and tree.GetEntry("value") == 3


def test_entries_added_or_removed_during_the_loop(qtbot, tree):
    tree.AddEntry("count", "Integer", 0)
    tree.AddEntry("gone", "Integer", 1, reset=True)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 1, timeout=5000)
    tree.SetEntry(5, "gone")
    tree.RemoveEntry("gone")
    tree.AddEntry("new", "Integer", 7, reset=True)
    tree.SetEntry(8, "new")
    tree.Stop()
    assert not tree.HasEntry("gone")  # a removed entry is not brought back
    assert tree.GetEntry("new") == 8  # it had no value when the loop started


def test_containers_are_reset_to_a_copy(qtbot, tree):
    tree.AddEntry("count", "Integer", 0)
    tree.AddEntry("items", "List", [1, 2], reset=True)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 1, timeout=5000)
    tree.SetEntry([1, 2, 3], "items")
    tree.Stop()
    assert tree.GetEntry("items") == [1, 2]


def test_object_entries_are_reset_to_the_same_object(qtbot, tree):
    first, second = object(), object()
    tree.AddEntry("count", "Integer", 0)
    tree.AddEntry("device", "Object", first, reset=True)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 1, timeout=5000)
    tree.SetEntry(second, "device")
    tree.Stop()
    assert tree.GetEntry("device") is first


def test_reset_and_restore_blackboard_work_together(qtbot, tree):
    config = tree.GetConfig()
    config.restore_blackboard = True
    tree.SetConfig(config)
    tree.AddEntry("count", "Integer", 0, reset=True)
    tree.AddEntry("other", "Integer", 0)
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
    tree.SetEntry(9, "other")
    tree.Stop()
    assert tree.GetEntry("count") == 0 and tree.GetEntry("other") == 0


def test_no_capture_is_left_behind(qtbot, tree):
    tree.AddEntry("count", "Integer", 0, reset=True)
    chain(tree, "Bump")
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert not tree.blackboardStore().is_capturing()
    forever = chain(tree, "Forever")[0]
    tree.Execute()
    qtbot.waitUntil(lambda: forever.GetStatus().value == "Running", timeout=5000)
    assert tree.blackboardStore().is_capturing()  # while a loop runs
    tree.Stop()
    QApplication.processEvents()
    assert not tree.blackboardStore().is_capturing()
