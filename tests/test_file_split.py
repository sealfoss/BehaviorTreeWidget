"""A tree is stored in two files: <name>_BT.json (the tree) and <name>_BB.json (its blackboard).

Requirement: split the blackboard store file from the behavior tree configuration file;
the files are named f"{tree-name}_BT.json" and f"{tree-name}_BB.json".
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from PySide6.QtWidgets import QMessageBox

from behavior_tree_widget import TreeFileError, tree_file_paths
from behavior_tree_widget.serialization import (
    BLACKBOARD_FORMAT_NAME,
    FORMAT_NAME,
    blackboard_path_for,
    is_blackboard_file_name,
    read_blackboard_file,
    tree_name,
)

from conftest import assert_blackboard_in_file, fast_config, run_until_idle


def read(path) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def write(path, data) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(data, stream)


def legacy_tree(blackboard: list) -> dict:
    """A tree file as written by version 0.6.0: one file holding the blackboard too."""
    return {
        "format": FORMAT_NAME,
        "version": 1,
        "config": {"tick_interval_ms": 100, "repeat": False, "restore_blackboard": False, "default_memory": True},
        "nodes": [{"id": "root", "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True}],
        "connections": [],
        "blackboard": blackboard,
    }


# ============================================================================ names
@pytest.mark.parametrize(
    "name, expected",
    [
        ("robot", "robot"),
        ("robot.json", "robot"),
        ("robot.JSON", "robot"),
        ("robot_BT.json", "robot"),
        ("robot_BB.json", "robot"),
        ("robot_bt.json", "robot"),
        ("my.robot_BT.json", "my.robot"),
        ("_BT.json", "_BT"),
        ("robot_BT_BT.json", "robot_BT"),
    ],
)
def test_tree_name(name, expected):
    assert tree_name(os.path.join("folder", name)) == expected


def test_tree_file_paths(tmp_path):
    folder = str(tmp_path)
    for name in ("robot", "robot.json", "robot_BT.json", "robot_BB.json"):
        assert tree_file_paths(os.path.join(folder, name)) == (
            os.path.join(folder, "robot_BT.json"),
            os.path.join(folder, "robot_BB.json"),
        )
    assert blackboard_path_for(os.path.join(folder, "legacy.json")) == os.path.join(folder, "legacy_BB.json")
    assert is_blackboard_file_name("x_BB.json") and is_blackboard_file_name("x_bb.json")
    assert not is_blackboard_file_name("x_BT.json") and not is_blackboard_file_name("_BB.json")


# ============================================================================ New / Save / Load
@pytest.mark.parametrize("given", ["robot", "robot.json", "robot_BT.json", "robot_BB.json", "robot_bt.json"])
def test_new_tree_writes_the_tree_file_and_the_blackboard_file(make_widget, tmp_path, given):
    widget = make_widget()
    assert widget.NewTree(str(tmp_path / given))
    assert {name.lower() for name in os.listdir(tmp_path)} == {"robot_bt.json", "robot_bb.json"}
    assert os.path.normcase(widget.GetFilePath()) == os.path.normcase(str(tmp_path / "robot_BT.json"))
    assert os.path.normcase(widget.GetBlackboardFilePath()) == os.path.normcase(str(tmp_path / "robot_BB.json"))
    tree = read(widget.GetFilePath())
    assert tree["format"] == FORMAT_NAME and "blackboard" not in tree
    assert [node["type"] for node in tree["nodes"]] == ["Root"]
    assert read(widget.GetBlackboardFilePath()) == {"format": BLACKBOARD_FORMAT_NAME, "version": 1, "blackboard": []}
    assert widget.window().windowTitle().endswith("robot_BT.json")


def test_new_tree_replaces_an_old_blackboard_file(make_widget, tmp_path):
    write(tmp_path / "robot_BB.json", {"format": BLACKBOARD_FORMAT_NAME, "version": 1,
                                       "blackboard": [{"name": "old", "type": "Integer", "value": 1}]})
    widget = make_widget()
    assert widget.NewTree(str(tmp_path / "robot"))
    assert widget.GetEntryNames() == []
    assert read(tmp_path / "robot_BB.json")["blackboard"] == []


def test_new_tree_of_the_same_name_drops_pending_blackboard_writes(qtbot, bt, tmp_path):
    bt.BLACKBOARD_SAVE_INTERVAL_MS = 60_000
    bt.AddEntry("old", "Integer", 1)
    bt._save_blackboard_now()
    bt.SetEntry(2, "old")  # due for tree_BB.json, not written yet
    assert bt.NewTree(str(tmp_path / "tree"))
    qtbot.wait(20)
    assert read(tmp_path / "tree_BB.json")["blackboard"] == []
    assert bt.GetEntryNames() == []


def test_new_tree_of_the_same_name_while_reset_entries_run(qtbot, bt, tmp_path):
    bt.AddEntry("count", "Integer", 0, reset=True)
    node = bt.AddNode("IncrementCounter", 0, 200)
    node.SetField("entry", "count")
    bt.Connect(bt.GetRootNode(), node)
    fast_config(bt)
    bt.SetLoopExecution(True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.executor().tick_count() >= 3, timeout=3000)
    assert bt.NewTree(str(tmp_path / "tree_BT.json"))  # stopping resets "count" for the old tree
    qtbot.wait(300)
    assert read(tmp_path / "tree_BB.json")["blackboard"] == []


def test_save_writes_both_files(bt, tmp_path):
    bt.AddEntry("count", "Integer", 3)
    bt.AddNode("Succeed", 0, 200)
    assert bt.SaveTree(str(tmp_path / "saved.json"))
    assert bt.GetFilePath() == str(tmp_path / "saved_BT.json")
    assert bt.GetBlackboardFilePath() == str(tmp_path / "saved_BB.json")
    assert not os.path.exists(tmp_path / "saved.json")
    tree = read(tmp_path / "saved_BT.json")
    assert "blackboard" not in tree and len(tree["nodes"]) == 2
    assert read(tmp_path / "saved_BB.json")["blackboard"] == [{"name": "count", "type": "Integer", "value": 3}]
    assert not bt.IsModified()


def test_save_round_trip(make_widget, bt, tmp_path):
    bt.AddEntry("count", "Integer", 3, reset=True)
    bt.AddEntry("names", "List", ["a", "b"])
    leaf = bt.AddNode("Succeed", 0, 200)
    bt.Connect(bt.GetRootNode(), leaf)
    assert bt.SaveTree(str(tmp_path / "trip"))
    other = make_widget()
    assert other.LoadTree(str(tmp_path / "trip_BT.json"))
    assert other.GetEntryNames() == ["count", "names"]
    assert other.GetEntry("names") == ["a", "b"]
    assert other.GetEntryReset("count") and not other.GetEntryReset("names")
    assert [node.GetTitle() for node in other.GetRootNode().GetChildren()] == ["Succeed"]
    assert not other.IsModified()


@pytest.mark.parametrize("given", ["trip_BT.json", "trip_BB.json", "trip", "trip.json"])
def test_load_accepts_either_file_or_the_tree_name(make_widget, bt, tmp_path, given):
    bt.AddEntry("count", "Integer", 3)
    assert bt.SaveTree(str(tmp_path / "trip_BT.json"))
    other = make_widget()
    assert other.LoadTree(str(tmp_path / given))
    assert other.GetFilePath() == str(tmp_path / "trip_BT.json")
    assert other.GetBlackboardFilePath() == str(tmp_path / "trip_BB.json")
    assert other.GetEntry("count") == 3


def test_saving_to_another_name_leaves_the_old_files(bt, tmp_path):
    bt.AddEntry("count", "Integer", 1)
    assert_blackboard_in_file(bt)
    old_tree = read(tmp_path / "tree_BT.json")
    assert bt.SaveTree(str(tmp_path / "copy_BT.json"))
    bt.SetEntry(2, "count")
    assert_blackboard_in_file(bt)
    assert read(tmp_path / "copy_BB.json")["blackboard"][0]["value"] == 2
    assert read(tmp_path / "tree_BB.json")["blackboard"][0]["value"] == 1
    assert read(tmp_path / "tree_BT.json") == old_tree


def test_pending_blackboard_changes_go_to_the_old_file_before_saving_elsewhere(bt, tmp_path):
    bt.BLACKBOARD_SAVE_INTERVAL_MS = 60_000
    bt.AddEntry("count", "Integer", 1)
    bt._save_blackboard_now()
    bt.SetEntry(5, "count")  # due for tree_BB.json, not written yet
    assert bt.SaveTree(str(tmp_path / "other"))
    assert read(tmp_path / "tree_BB.json")["blackboard"][0]["value"] == 5
    assert read(tmp_path / "other_BB.json")["blackboard"][0]["value"] == 5


# ============================================================================ blackboard changes
def test_blackboard_changes_write_only_the_blackboard_file(qtbot, bt):
    tree_before = read(bt.GetFilePath())
    tree_mtime = os.stat(bt.GetFilePath()).st_mtime_ns
    saved = []
    bt.blackboardSaved.connect(saved.append)
    bt.AddEntry("speed", "Double", 1.5)
    assert_blackboard_in_file(bt)
    assert read(bt.GetBlackboardFilePath())["blackboard"] == [{"name": "speed", "type": "Double", "value": 1.5}]
    assert read(bt.GetFilePath()) == tree_before
    assert os.stat(bt.GetFilePath()).st_mtime_ns == tree_mtime
    assert saved and saved[-1] == bt.GetBlackboardFilePath()


def test_node_edits_stay_unsaved_while_the_blackboard_is_written(qtbot, bt):
    bt.AddNode("Succeed", 0, 200)
    assert bt.IsModified()
    bt.AddEntry("n", "Integer", 1)
    qtbot.waitUntil(lambda: read(bt.GetBlackboardFilePath())["blackboard"] != [], timeout=3000)
    assert bt.IsModified()  # the node is not in the tree file yet
    assert len(read(bt.GetFilePath())["nodes"]) == 1


def test_a_read_only_blackboard_file_reports_the_failure(qtbot, bt):
    failures = []
    bt.blackboardSaveFailed.connect(failures.append)
    path = bt.GetBlackboardFilePath()
    os.chmod(path, stat.S_IREAD)
    try:
        bt.AddEntry("n", "Integer", 1)
        qtbot.waitUntil(lambda: bool(failures), timeout=3000)
        assert path in failures[0]
        assert bt.IsModified()
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    bt.SetEntry(2, "n")  # the next change tries again
    qtbot.waitUntil(lambda: read(path)["blackboard"] == [{"name": "n", "type": "Integer", "value": 2}], timeout=3000)


def test_save_reports_a_blackboard_file_that_cannot_be_written(bt, tmp_path, dialogs):
    bt.AddEntry("n", "Integer", 1)
    os.makedirs(tmp_path / "locked_BB.json")  # a folder: the blackboard file cannot be written
    dialogs.save_path = str(tmp_path / "locked_BT.json")
    failures = []
    bt.blackboardSaveFailed.connect(failures.append)
    assert bt.SaveTree()
    assert os.path.isfile(tmp_path / "locked_BT.json")
    assert failures and bt.IsModified()
    assert "warning" in dialogs.kinds()


def test_runtime_blackboard_changes_are_written_while_executing(qtbot, bt):
    bt.AddEntry("count", "Integer", 0)
    node = bt.AddNode("IncrementCounter", 0, 200)
    node.SetField("entry", "count")
    bt.Connect(bt.GetRootNode(), node)
    fast_config(bt)
    bt.SetLoopExecution(True)
    assert bt.SaveTree(bt.GetFilePath())
    bt.Execute()
    try:
        qtbot.waitUntil(
            lambda: [item["value"] for item in read(bt.GetBlackboardFilePath())["blackboard"]][:1] >= [3], timeout=5000
        )
    finally:
        bt.Stop()
    assert_blackboard_in_file(bt)


# ============================================================================ files of earlier versions
def test_a_legacy_tree_file_loads_with_its_blackboard(make_widget, tmp_path):
    write(tmp_path / "old.json", legacy_tree([{"name": "count", "type": "Integer", "value": 7}]))
    widget = make_widget()
    assert widget.LoadTree(str(tmp_path / "old.json"))
    assert widget.GetEntry("count") == 7
    assert widget.GetFilePath() == str(tmp_path / "old.json")
    assert widget.GetBlackboardFilePath() == str(tmp_path / "old_BB.json")
    assert not widget.IsModified()
    assert sorted(os.listdir(tmp_path)) == ["old.json"]  # loading writes nothing


def test_a_legacy_tree_gets_its_blackboard_file_at_the_first_change(qtbot, make_widget, tmp_path):
    write(tmp_path / "old.json", legacy_tree([{"name": "count", "type": "Integer", "value": 7}]))
    before = read(tmp_path / "old.json")
    widget = make_widget()
    assert widget.LoadTree(str(tmp_path / "old.json"))
    widget.SetEntry(8, "count")
    assert_blackboard_in_file(widget)
    assert read(tmp_path / "old_BB.json")["blackboard"] == [{"name": "count", "type": "Integer", "value": 8}]
    assert read(tmp_path / "old.json") == before  # the old file is never changed
    # Loading the old file again takes the newer blackboard file.
    other = make_widget()
    assert other.LoadTree(str(tmp_path / "old.json"))
    assert other.GetEntry("count") == 8


def test_saving_a_legacy_tree_writes_the_two_files(make_widget, tmp_path):
    write(tmp_path / "old.json", legacy_tree([{"name": "count", "type": "Integer", "value": 7}]))
    widget = make_widget()
    assert widget.LoadTree(str(tmp_path / "old.json"))
    widget.AddNode("Succeed", 0, 200)
    assert widget.SaveTree(widget.GetFilePath())
    assert widget.GetFilePath() == str(tmp_path / "old_BT.json")
    assert "blackboard" not in read(tmp_path / "old_BT.json")
    assert read(tmp_path / "old_BB.json")["blackboard"] == [{"name": "count", "type": "Integer", "value": 7}]


def test_the_close_prompt_saves_a_legacy_tree_as_the_two_files(make_widget, tmp_path, dialogs):
    write(tmp_path / "old.json", legacy_tree([]))
    widget = make_widget()
    assert widget.LoadTree(str(tmp_path / "old.json"))
    widget.AddNode("Succeed", 0, 200)
    dialogs.question_answer = QMessageBox.StandardButton.Save
    assert widget.ConfirmDiscardChanges()
    assert os.path.isfile(tmp_path / "old_BT.json") and os.path.isfile(tmp_path / "old_BB.json")
    assert widget.GetFilePath() == str(tmp_path / "old_BT.json")


def test_a_tree_file_without_blackboard_file_has_an_empty_blackboard(make_widget, bt, tmp_path, dialogs):
    bt.AddEntry("count", "Integer", 1)
    assert bt.SaveTree(str(tmp_path / "plain"))
    os.remove(tmp_path / "plain_BB.json")
    other = make_widget()
    other.AddEntry("before", "Integer", 1)
    dialogs.open_path = str(tmp_path / "plain_BT.json")
    assert other.LoadTree()
    assert other.GetEntryNames() == []
    assert "warning" not in dialogs.kinds()  # not a problem: e.g. a sub-tree file
    assert not os.path.exists(tmp_path / "plain_BB.json")


# ============================================================================ damaged blackboard files
@pytest.mark.parametrize(
    "content, message",
    [
        ("{not json", "not a valid JSON file"),
        (json.dumps({"format": "something else", "version": 1, "blackboard": []}), "not a blackboard file"),
        (json.dumps({"format": BLACKBOARD_FORMAT_NAME, "version": 99, "blackboard": []}), "newer version"),
        (json.dumps({"format": BLACKBOARD_FORMAT_NAME, "version": 0, "blackboard": []}), "invalid format version"),
        (json.dumps({"format": BLACKBOARD_FORMAT_NAME, "version": 1, "blackboard": {"a": 1}}), "must be a list"),
    ],
)
def test_a_damaged_blackboard_file_is_not_loaded(make_widget, bt, tmp_path, content, message):
    assert bt.SaveTree(str(tmp_path / "broken"))
    (tmp_path / "broken_BB.json").write_text(content, encoding="utf-8")
    other = make_widget()
    assert other.NewTree(str(tmp_path / "current"))
    other.AddEntry("kept", "Integer", 1)
    with pytest.raises(TreeFileError, match=message):
        other.LoadTree(str(tmp_path / "broken_BT.json"))
    assert other.GetFilePath() == str(tmp_path / "current_BT.json")  # the current tree is untouched
    assert other.GetEntryNames() == ["kept"]
    with pytest.raises(TreeFileError, match=message):
        read_blackboard_file(str(tmp_path / "broken_BB.json"))


def test_invalid_entries_of_a_blackboard_file_are_reported_and_skipped(make_widget, tmp_path, dialogs):
    write(tmp_path / "t_BT.json", {key: value for key, value in legacy_tree([]).items() if key != "blackboard"})
    write(tmp_path / "t_BB.json", {"format": BLACKBOARD_FORMAT_NAME, "version": 1, "blackboard": [
        {"name": "good", "type": "Integer", "value": 1, "reset": True},
        {"name": "bad", "type": "Integer", "value": "x"},
        {"name": "flag", "type": "Bool", "value": True, "reset": "yes"},
    ]})
    widget = make_widget()
    dialogs.open_path = str(tmp_path / "t_BT.json")
    assert widget.LoadTree()
    assert widget.GetEntryNames() == ["good", "flag"]
    assert widget.GetEntryReset("good") and not widget.GetEntryReset("flag")
    warnings = [args for kind, args in dialogs.shown if kind == "warning"]
    assert warnings and "'bad'" in warnings[0][2] and "'reset'" in warnings[0][2]


# ============================================================================ dialogs
def test_the_save_dialog_suggests_the_tree_file(bt, dialogs):
    dialogs.save_path = ""
    assert not bt.SaveTree()
    _, args = [entry for entry in dialogs.shown if entry[0] == "save_dialog"][-1]
    assert args[2].endswith("tree_BT.json")


def test_saving_under_a_plain_name_asks_before_replacing_the_tree_file(bt, tmp_path, dialogs):
    (tmp_path / "target_BT.json").write_text("{}", encoding="utf-8")
    dialogs.save_path = str(tmp_path / "target.json")
    dialogs.question_answer = QMessageBox.StandardButton.No
    assert not bt.SaveTree()
    assert (tmp_path / "target_BT.json").read_text(encoding="utf-8") == "{}"
    dialogs.question_answer = QMessageBox.StandardButton.Yes
    assert bt.SaveTree()
    assert read(tmp_path / "target_BT.json")["format"] == FORMAT_NAME
    assert bt.GetFilePath() == str(tmp_path / "target_BT.json")


def test_saving_under_a_new_plain_name_does_not_ask(bt, tmp_path, dialogs):
    dialogs.save_path = str(tmp_path / "fresh")
    assert bt.SaveTree()
    assert "question" not in dialogs.kinds()
    assert os.path.isfile(tmp_path / "fresh_BT.json") and os.path.isfile(tmp_path / "fresh_BB.json")


def test_loading_the_blackboard_file_from_the_dialog_loads_its_tree(make_widget, bt, tmp_path, dialogs):
    bt.AddEntry("n", "Integer", 4)
    assert bt.SaveTree(str(tmp_path / "pick"))
    other = make_widget()
    dialogs.open_path = str(tmp_path / "pick_BB.json")
    assert other.LoadTree()
    assert other.GetFilePath() == str(tmp_path / "pick_BT.json")
    assert other.GetEntry("n") == 4


def test_loading_a_tree_runs_with_the_loaded_blackboard(qtbot, make_widget, bt, tmp_path):
    bt.AddEntry("flag", "Bool", True)
    check = bt.AddNode("Evaluation", 0, 200)
    check.SetValueName("flag")
    check.SetLiteralValue(True)
    bt.Connect(bt.GetRootNode(), check)
    assert bt.SaveTree(str(tmp_path / "run"))
    other = make_widget()
    assert other.LoadTree(str(tmp_path / "run_BT.json"))
    fast_config(other)
    results = []
    other.executionFinished.connect(results.append)
    other.Execute()
    run_until_idle(qtbot, other)
    assert results == ["Succeeded"]
