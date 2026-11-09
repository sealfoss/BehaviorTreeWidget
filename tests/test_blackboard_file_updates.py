"""The blackboard file is updated whenever the blackboard's keys or values change.

Requirement: the behavior tree writes its blackboard file (``<name>_BB.json``) whenever the keys
or values stored in the blackboard are modified, by the tree itself or by the user. Only the
blackboard file is written: the tree file is left alone, other edits (nodes, configuration, ...)
stay unsaved until Save.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading

import pytest

from behavior_tree_widget import LeafNodeWidget

from conftest import assert_blackboard_in_file, fast_config, file_blackboard, run_until_idle


def read(path) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def entry(name: str, type_name: str, value) -> dict:
    return {"name": name, "type": type_name, "value": value}


def file_value(bt, name: str, default=None):
    """Value of blackboard entry ``name`` in the blackboard file (``default`` when it is not there yet)."""
    return next((item["value"] for item in file_blackboard(bt.GetFilePath()) if item["name"] == name), default)


class Counter(LeafNodeWidget):
    """Increments the "count" entry on a worker thread."""

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("count") + 1, "count")
        return True


# ============================================================================ who changes the blackboard
def test_an_entry_added_from_code_is_written(bt):
    bt.AddEntry("speed", "Double", 1.5)
    assert_blackboard_in_file(bt)
    assert file_blackboard(bt.GetFilePath()) == [entry("speed", "Double", 1.5)]


def test_every_kind_of_change_is_written(bt):
    bt.AddEntry("a", "Integer", 1)
    bt.AddEntry("b", "List", [1, 2])
    assert_blackboard_in_file(bt)
    bt.SetEntry(5, "a")
    assert_blackboard_in_file(bt)
    bt.blackboard_store().rename("b", "items")
    assert_blackboard_in_file(bt)
    assert [item["name"] for item in file_blackboard(bt.GetFilePath())] == ["a", "items"]
    bt.blackboard_store().reorder(["items", "a"])
    assert_blackboard_in_file(bt)
    assert [item["name"] for item in file_blackboard(bt.GetFilePath())] == ["items", "a"]
    bt.blackboard_store().replace("a", "String", "five")
    assert_blackboard_in_file(bt)
    bt.RemoveEntry("items")
    assert_blackboard_in_file(bt)
    assert file_blackboard(bt.GetFilePath()) == [entry("a", "String", "five")]


def test_a_user_edit_in_the_blackboard_tab_is_written(bt):
    bt.AddEntry("counter", "Integer", 1)
    assert_blackboard_in_file(bt)
    bt.blackboard_view().row("counter").value_widget.setValue(42)
    assert_blackboard_in_file(bt)
    assert file_blackboard(bt.GetFilePath()) == [entry("counter", "Integer", 42)]


def test_changes_made_by_the_running_tree_are_written(qtbot, bt):
    bt.AddEntry("count", "Integer", 0)
    bt.RegisterNodeType(Counter)
    bt.Connect(bt.GetRootNode(), bt.AddNode("Counter", 0, 200))
    fast_config(bt)
    bt.SetLoopExecution(True)
    assert bt.SaveTree(bt.GetFilePath())  # the tree edits above are saved
    bt.Execute()
    try:
        qtbot.waitUntil(lambda: file_value(bt, "count", 0) >= 3, timeout=5000)
        assert bt.IsExecuting()  # written while the tree runs
    finally:
        bt.Stop()
    assert_blackboard_in_file(bt)


def test_restored_values_are_written_on_stop(qtbot, bt):
    bt.AddEntry("count", "Integer", 0)
    bt.RegisterNodeType(Counter)
    bt.Connect(bt.GetRootNode(), bt.AddNode("Counter", 0, 200))
    fast_config(bt, restore_blackboard=True)
    bt.SetLoopExecution(True)
    assert bt.SaveTree(bt.GetFilePath())  # the tree edits above are saved
    bt.Execute()
    qtbot.waitUntil(lambda: file_value(bt, "count", 0) >= 2, timeout=5000)
    bt.Stop()
    assert bt.GetEntry("count") == 0
    assert_blackboard_in_file(bt)
    assert file_blackboard(bt.GetFilePath()) == [entry("count", "Integer", 0)]


def test_changes_from_a_worker_thread_are_written(qtbot, bt):
    bt.AddEntry("value", "Integer", 0)
    assert_blackboard_in_file(bt)
    thread = threading.Thread(target=lambda: bt.SetEntry(7, "value"))
    thread.start()
    thread.join(5)
    qtbot.waitUntil(lambda: file_blackboard(bt.GetFilePath()) == [entry("value", "Integer", 7)], timeout=3000)


# ============================================================================ only the blackboard is written
def test_other_unsaved_edits_stay_unsaved(qtbot, bt):
    path = bt.GetFilePath()
    saved_nodes = read(path)["nodes"]
    bt.AddNode("Succeed", 0, 300)  # an unsaved edit of the tree
    assert bt.IsModified()
    bt.SetEntry(3, "counter")
    qtbot.waitUntil(lambda: file_blackboard(path) == [entry("counter", "Integer", 3)], timeout=3000)
    assert read(path)["nodes"] == saved_nodes  # the new node is not saved by the blackboard update
    assert bt.IsModified()  # the node is still an unsaved change


def test_the_rest_of_the_file_is_kept_as_it_was(qtbot, bt, tmp_path):
    """A hand-written tree file in the single-file format of earlier versions (holding the blackboard
    as well) is never rewritten by blackboard updates: the blackboard goes to <name>_BB.json."""
    path = tmp_path / "hand_written.json"
    blackboard_path = tmp_path / "hand_written_BB.json"
    document = {
        "format": "behavior_tree_widget",
        "version": 1,
        "comment": "left as it is by every blackboard update",
        "nodes": [{"id": "r", "type": "Root", "title": "Root", "x": 0, "y": 0}],
        "connections": [],
        "blackboard": [entry("a", "Integer", 1)],
        "config": {"tick_interval_ms": 50},
    }
    text = json.dumps(document)
    path.write_text(text, encoding="utf-8")
    assert bt.LoadTree(str(path))
    assert bt.GetEntry("a") == 1  # the blackboard of the old file is loaded
    assert bt.GetFilePath() == os.path.abspath(str(path))
    assert bt.GetBlackboardFilePath() == os.path.abspath(str(blackboard_path))
    assert not blackboard_path.exists()  # created at the first change of the blackboard
    written = []
    bt.blackboardSaved.connect(written.append)
    bt.SetEntry(2, "a")
    qtbot.waitUntil(lambda: blackboard_path.exists() and file_blackboard(path) == [entry("a", "Integer", 2)], timeout=3000)
    assert read(blackboard_path) == {
        "format": "behavior_tree_widget_blackboard",
        "version": 1,
        "blackboard": [entry("a", "Integer", 2)],
    }
    assert written == [os.path.abspath(str(blackboard_path))]
    assert path.read_text(encoding="utf-8") == text  # the old file is not touched at all
    assert sorted(os.listdir(tmp_path)) == ["hand_written.json", "hand_written_BB.json", "tree_BB.json", "tree_BT.json"]


def test_object_entries_do_not_cause_writes(qtbot, bt):
    assert_blackboard_in_file(bt)
    written = []
    bt.blackboardSaved.connect(written.append)
    bt.SetEntry(object(), "device")
    qtbot.wait(400)
    assert written == []
    assert file_blackboard(bt.GetFilePath()) == []


def test_unsaveable_values_are_reported_once(qtbot, bt, caplog):
    with caplog.at_level(logging.WARNING, logger="behavior_tree_widget"):
        bt.SetEntry([object()], "odd")
        qtbot.wait(300)
        bt.SetEntry([object(), object()], "odd")
        qtbot.wait(300)
    messages = [record.getMessage() for record in caplog.records if "'odd'" in record.getMessage()]
    assert len(messages) == 1
    assert file_blackboard(bt.GetFilePath()) == []


# ============================================================================ when it is written
def test_rapid_changes_are_written_together(qtbot, bt):
    assert_blackboard_in_file(bt)
    written = []
    bt.blackboardSaved.connect(written.append)
    for value in range(200):
        bt.SetEntry(value, "fast")
    assert_blackboard_in_file(bt)
    assert file_blackboard(bt.GetFilePath()) == [entry("fast", "Integer", 199)]
    assert len(written) <= 2


def test_writes_are_at_most_one_interval_apart(qtbot, bt):
    bt.BLACKBOARD_SAVE_INTERVAL_MS = 200
    written = []
    bt.blackboardSaved.connect(lambda path: written.append(path))
    bt.SetEntry(1, "x")
    qtbot.waitUntil(lambda: len(written) == 1, timeout=2000)
    bt.SetEntry(2, "x")
    qtbot.wait(60)
    assert len(written) == 1  # within the interval: waits for its end
    qtbot.waitUntil(lambda: len(written) == 2, timeout=2000)
    assert file_blackboard(bt.GetFilePath()) == [entry("x", "Integer", 2)]


def test_pending_changes_are_written_before_another_tree_is_loaded(bt, tmp_path):
    first = bt.GetFilePath()
    other = tmp_path / "other.json"
    assert bt.SaveTree(str(other))
    assert bt.LoadTree(first)
    bt.SetEntry(9, "last")
    assert bt.LoadTree(str(other))  # at once, before the timer fires
    assert file_blackboard(first) == [entry("last", "Integer", 9)]
    assert file_blackboard(str(other)) == []
    assert not bt.HasEntry("last")


def test_pending_changes_are_written_on_shutdown(bt):
    bt.SetEntry(4, "late")
    bt.Shutdown()
    assert file_blackboard(bt.GetFilePath()) == [entry("late", "Integer", 4)]


def test_save_as_writes_pending_changes_to_the_old_file_and_later_ones_to_the_new(qtbot, bt, tmp_path):
    first = bt.GetFilePath()
    bt.SetEntry(1, "n")
    second = str(tmp_path / "second.json")
    assert bt.SaveTree(second)
    assert file_blackboard(first) == [entry("n", "Integer", 1)]
    bt.SetEntry(2, "n")
    qtbot.waitUntil(lambda: file_blackboard(second) == [entry("n", "Integer", 2)], timeout=3000)
    assert file_blackboard(first) == [entry("n", "Integer", 1)]


def test_nothing_is_written_before_a_tree_exists(qtbot, make_widget):
    widget = make_widget()
    written = []
    widget.blackboardSaved.connect(written.append)
    widget.SetEntry(1, "early")
    qtbot.wait(300)
    assert written == [] and widget.GetFilePath() is None


# ============================================================================ failures
@pytest.mark.skipif(os.name != "nt", reason="read-only files are a Windows permission")
def test_a_failed_write_is_reported_and_retried(qtbot, bt, caplog):
    path = bt.GetBlackboardFilePath()
    failures = []
    bt.blackboardSaveFailed.connect(failures.append)
    os.chmod(path, stat.S_IREAD)
    try:
        with caplog.at_level(logging.WARNING, logger="behavior_tree_widget"):
            bt.SetEntry(1, "x")
            qtbot.waitUntil(lambda: len(failures) == 1, timeout=3000)
            bt.SetEntry(2, "x")
            qtbot.waitUntil(lambda: len(failures) == 2, timeout=3000)
        assert bt.IsModified()  # not in the file: the close prompt offers to save it
        assert len([r for r in caplog.records if "Could not write the blackboard" in r.getMessage()]) == 1
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    bt.SetEntry(3, "x")
    qtbot.waitUntil(lambda: file_blackboard(path) == [entry("x", "Integer", 3)], timeout=3000)
