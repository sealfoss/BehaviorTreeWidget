"""The Sub-Tree node: runs the behavior tree of another file and succeeds or fails with it.

Requirement: a sub-tree widget runs a complete behavior tree behind the scenes and returns
true or false when that tree succeeds or fails. It has a line edit showing the file name of
the sub-tree's .json file. Only one blackboard is loaded: the sub-tree uses the blackboard
of the main tree.
"""

from __future__ import annotations

import json
import os
import threading

import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QToolButton

from behavior_tree_widget import LeafNodeWidget, NodeStatus, Status, SubTreeNodeWidget
from behavior_tree_widget.serialization import BLACKBOARD_FORMAT_NAME, FORMAT_NAME
from behavior_tree_widget.subtree import SUB_TREE

from conftest import SlowCancelable, fast_config, run_until_idle
from support.round10 import Stubborn


class Mark(LeafNodeWidget):
    """Appends its "label" field to ``Mark.order`` and succeeds (or fails with "fail" checked)."""

    _fields = {"label": "", "fail": False}
    RUN_IN_THREAD = False
    order: list = []

    def OnRun(self, tree):
        type(self).order.append(self.GetField("label"))
        return not self.GetField("fail")


class Spin(LeafNodeWidget):
    """Runs for "ticks" ticks, then succeeds."""

    _fields = {"ticks": 2}
    RUN_IN_THREAD = False

    def OnStart(self, tree):
        self.left = self.GetField("ticks")

    def OnRun(self, tree):
        if self.left:
            self.left -= 1
            return Status.RUNNING
        return True


def node_item(node_id, type_name, x=0.0, y=200.0, **extra) -> dict:
    return {"id": node_id, "type": type_name, "title": extra.pop("title", type_name), "x": x, "y": y, **extra}


def tree_document(nodes: list, connections: list, composite: str = "Sequence") -> dict:
    root = node_item("root", "Root", 0.0, 0.0, composite=composite, memory=True)
    return {
        "format": FORMAT_NAME,
        "version": 1,
        "config": {"tick_interval_ms": 100, "repeat": False, "restore_blackboard": False, "default_memory": True},
        "nodes": [root, *nodes],
        "connections": [{"parent": parent, "child": child} for parent, child in connections],
    }


def write_tree(path, nodes: list, connections: list, composite: str = "Sequence") -> str:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(tree_document(nodes, connections, composite), stream)
    return str(path)


def mark(node_id, label, x=0.0, fail=False) -> dict:
    return node_item(node_id, "Mark", x, 200.0, fields={"label": label, "fail": fail})


def marks_file(path, *labels, composite="Sequence", fail=()) -> str:
    nodes = [mark(f"m{index}", label, index * 200.0, label in fail) for index, label in enumerate(labels)]
    return write_tree(path, nodes, [("root", f"m{index}") for index in range(len(labels))], composite)


@pytest.fixture
def tree(bt):
    for cls in (Mark, Spin):
        bt.RegisterNodeType(cls)
    Mark.order = []
    fast_config(bt)
    return bt


def add_subtree(bt, file: str = "", x=0.0, y=200.0, connect=True) -> SubTreeNodeWidget:
    node = bt.AddNode(SUB_TREE, x, y)
    if file:
        node.SetFile(file)
    if connect:
        bt.Connect(bt.GetRootNode(), node)
    return node


def execute(qtbot, bt) -> list:
    results = []
    bt.executionFinished.connect(results.append)
    bt.Execute()
    run_until_idle(qtbot, bt)
    bt.executionFinished.disconnect()
    return results


# ============================================================================ the node
def test_sub_tree_is_a_built_in_type(bt):
    names = bt.GetNodeTypes()
    assert names[names.index("Print Message") + 1] == SUB_TREE
    assert bt.GetNodeType(SUB_TREE) is SubTreeNodeWidget
    entries = bt._node_menu_entries()
    assert [entry for entry in entries if entry[0] == "subtree"] == [("subtree", SUB_TREE, SUB_TREE)]
    groups = [group for group, _, _ in entries]
    assert groups.index("network") < groups.index("log") < groups.index("subtree") < groups.index("leaf")


def test_the_node_shows_its_file_in_a_line_edit(bt):
    node = add_subtree(bt, connect=False)
    editor = node.file_editor()
    assert isinstance(editor, QLineEdit) and editor.objectName() == "SubTreeFile"
    assert isinstance(node.browse_button(), QToolButton)
    assert editor.text() == "" and node.GetFile() == ""
    node.SetFile("patrol_BT.json")
    assert editor.text() == "patrol_BT.json"
    assert node.GetField("File") == "patrol_BT.json"
    assert node.HasParentConnection() and not node.HasChildConnections()
    assert node.DisplayTitle() == SUB_TREE
    node.SetTitle("patrol")
    assert node.DisplayTitle() == f"patrol ({SUB_TREE})"


def test_typing_a_file_name_is_an_edit(bt):
    node = add_subtree(bt, connect=False)
    bt._set_modified(False)
    edited = []
    node.fieldEdited.connect(edited.append)
    node.file_editor().setFocus()
    QTest.keyClicks(node.file_editor(), "patrol")
    assert node.GetFile() == "patrol"
    assert edited and edited[-1] == "File"
    assert bt.IsModified()


def test_the_browse_button_chooses_a_file_relative_to_the_tree(bt, tmp_path, dialogs):
    os.makedirs(tmp_path / "subtrees")
    target = marks_file(tmp_path / "subtrees" / "patrol_BT.json", "a")
    node = add_subtree(bt, connect=False)
    bt._set_modified(False)
    dialogs.open_path = target
    QTest.mouseClick(node.browse_button(), Qt.MouseButton.LeftButton)
    assert node.GetFile() == os.path.join("subtrees", "patrol_BT.json")
    assert node.file_editor().text() == node.GetFile()
    assert node.GetResolvedFile() == target
    assert bt.IsModified()
    _, args = [entry for entry in dialogs.shown if entry[0] == "open_dialog"][-1]
    assert args[2] == str(tmp_path)  # starts in the tree's folder


def test_the_browse_button_keeps_files_of_other_folders_absolute(bt, tmp_path_factory, dialogs):
    other = tmp_path_factory.mktemp("elsewhere")
    target = marks_file(other / "far_BT.json", "a")
    node = add_subtree(bt, connect=False)
    dialogs.open_path = target
    assert node.browse()
    assert node.GetFile() == target


def test_cancelling_the_browse_dialog_changes_nothing(bt, dialogs):
    node = add_subtree(bt, "patrol_BT.json", connect=False)
    dialogs.open_path = ""
    assert not node.browse()
    assert node.GetFile() == "patrol_BT.json"


def test_resolving_the_file_name(bt, tmp_path):
    node = add_subtree(bt, connect=False)
    assert node.GetResolvedFile() is None
    node.SetFile("patrol_BT.json")
    assert node.GetResolvedFile() == str(tmp_path / "patrol_BT.json")
    marks_file(tmp_path / "patrol_BT.json", "a")
    for name in ("patrol", "patrol.json"):
        node.SetFile(name)
        assert node.GetResolvedFile() == str(tmp_path / "patrol_BT.json")
    absolute = str(tmp_path / "x" / "y_BT.json")
    node.SetFile(absolute)
    assert node.GetResolvedFile() == absolute
    assert node.file_editor().toolTip() == absolute


def test_the_file_is_saved_loaded_copied_and_pasted(make_widget, bt, tmp_path):
    node = add_subtree(bt, "patrol_BT.json")
    node.SetTitle("patrol")
    assert bt.SaveTree(bt.GetFilePath())
    with open(bt.GetFilePath(), encoding="utf-8") as stream:
        item = next(entry for entry in json.load(stream)["nodes"] if entry["type"] == SUB_TREE)
    assert item["fields"] == {"File": "patrol_BT.json"} and item["title"] == "patrol"
    other = make_widget()
    assert other.LoadTree(bt.GetFilePath())
    loaded = next(n for n in other.GetNodes() if isinstance(n, SubTreeNodeWidget))
    assert loaded.GetFile() == "patrol_BT.json" and loaded.file_editor().text() == "patrol_BT.json"
    assert loaded.GetParent() is other.GetRootNode()
    assert bt.CopyNodes([node])
    pasted = bt.PasteNodes()
    assert pasted[0].GetFile() == "patrol_BT.json"


# ============================================================================ running a sub-tree
@pytest.mark.parametrize(
    "labels, composite, fail, result",
    [
        (("a", "b"), "Sequence", (), "Succeeded"),
        (("a", "b"), "Sequence", ("a",), "Failed"),
        (("a", "b"), "Selector", ("a",), "Succeeded"),
        (("a", "b"), "Selector", ("a", "b"), "Failed"),
    ],
)
def test_the_node_succeeds_or_fails_with_its_tree(qtbot, tree, tmp_path, labels, composite, fail, result):
    marks_file(tmp_path / "sub_BT.json", *labels, composite=composite, fail=fail)
    node = add_subtree(tree, "sub_BT.json")
    assert execute(qtbot, tree) == [result]
    assert node.GetStatus().value == result
    assert node.GetError() is None


def test_the_sub_tree_runs_in_the_order_of_its_file(qtbot, tree, tmp_path):
    nodes = [mark("left", "left", -300.0), mark("right", "right", 300.0), mark("middle", "middle", 0.0)]
    write_tree(tmp_path / "order_BT.json", nodes, [("root", "right"), ("root", "left"), ("root", "middle")])
    add_subtree(tree, "order_BT.json")
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["left", "middle", "right"]


def test_nested_composites_and_negations(qtbot, tree, tmp_path):
    nodes = [
        node_item("sel", "Selector", 0.0, 200.0, memory=True),
        node_item("neg", "Negation", -100.0, 400.0),
        mark("ok", "inverted", -100.0),
        mark("then", "then", 200.0),
    ]
    nodes[2]["y"] = 600.0
    write_tree(tmp_path / "nested_BT.json", nodes, [("root", "sel"), ("sel", "neg"), ("neg", "ok"), ("sel", "then")])
    add_subtree(tree, "nested_BT.json")
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["inverted", "then"]  # the Negation failed, so the Selector went on


def test_the_sub_tree_uses_the_main_blackboard(qtbot, tree, tmp_path):
    tree.AddEntry("mode", "String", "idle")
    tree.AddEntry("seen", "Bool", False)
    nodes = [
        node_item("check", "Evaluation", 0.0, 200.0, fields={"ValueName": "mode", "CompareTo": None, "LiteralValue": "idle"}),
        node_item("set", "Set", 300.0, 200.0, fields={"ValueName": "seen", "CompareTo": None, "LiteralValue": True}),
    ]
    write_tree(tmp_path / "bb_BT.json", nodes, [("root", "check"), ("root", "set")])
    with open(tmp_path / "bb_BB.json", "w", encoding="utf-8") as stream:  # never loaded
        json.dump({"format": BLACKBOARD_FORMAT_NAME, "version": 1,
                   "blackboard": [{"name": "mode", "type": "String", "value": "other"},
                                  {"name": "extra", "type": "Integer", "value": 1}]}, stream)
    add_subtree(tree, "bb_BT.json")
    assert execute(qtbot, tree) == ["Succeeded"]
    assert tree.GetEntry("seen") is True
    assert tree.GetEntryNames() == ["mode", "seen"]  # only one blackboard: the main tree's


def test_a_running_sub_tree_keeps_the_node_running(qtbot, tree, tmp_path):
    write_tree(tmp_path / "spin_BT.json", [node_item("s", "Spin", fields={"ticks": 3})], [("root", "s")])
    node = add_subtree(tree, "spin_BT.json")
    statuses = []
    node.statusChanged.connect(statuses.append)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert statuses == ["Ready", "Running", "Succeeded"] or statuses == ["Running", "Succeeded"]


def test_worker_thread_nodes_run_and_stop_cancels_them(qtbot, tree, tmp_path):
    SlowCancelable.cancelled.clear()
    write_tree(tmp_path / "slow_BT.json", [node_item("s", "SlowCancelable", fields={"seconds": 30.0})], [("root", "s")])
    node = add_subtree(tree, "slow_BT.json")
    tree.Execute()
    qtbot.waitUntil(lambda: node.GetStatus() is NodeStatus.RUNNING, timeout=3000)
    tree.Stop()
    assert SlowCancelable.cancelled.wait(5)
    assert node.GetStatus() is NodeStatus.READY


def test_the_inner_nodes_are_hidden_and_deleted_afterwards(qtbot, tree, tmp_path):
    write_tree(tmp_path / "spin_BT.json", [node_item("s", "Spin", fields={"ticks": 50})], [("root", "s")])
    add_subtree(tree, "spin_BT.json")
    count = len(tree.GetNodes())
    tree.Execute()
    qtbot.waitUntil(lambda: tree.executor().tick_count() >= 2, timeout=3000)
    inner = tree.executor().subtree_nodes()
    assert [type(node).__name__ for node in inner] == ["RootNodeWidget", "Spin"]
    assert all(node._item is None and not node.isVisible() for node in inner)
    assert all(node.GetTree() is tree for node in inner)
    assert len(tree.GetNodes()) == count
    tree.Stop()
    assert tree.executor().subtree_nodes() == []
    qtbot.waitUntil(lambda: not any(shiboken6.isValid(node) for node in inner), timeout=3000)


def test_the_file_is_read_at_every_execute(qtbot, tree, tmp_path):
    path = marks_file(tmp_path / "sub_BT.json", "first")
    add_subtree(tree, "sub_BT.json")
    assert execute(qtbot, tree) == ["Succeeded"]
    marks_file(path, "second", fail=("second",))
    assert execute(qtbot, tree) == ["Failed"]
    assert Mark.order == ["first", "second"]


def test_loop_execution_runs_the_sub_tree_every_loop(qtbot, tree, tmp_path):
    marks_file(tmp_path / "sub_BT.json", "loop")
    add_subtree(tree, "sub_BT.json")
    tree.SetLoopExecution(True)
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: len(Mark.order) >= 3, timeout=5000)
    finally:
        tree.Stop()


def test_a_pause_node_in_the_sub_tree_pauses_the_main_tree(qtbot, tree, tmp_path):
    write_tree(tmp_path / "p_BT.json", [node_item("p", "Pause"), mark("after", "after", 300.0)], [("root", "p"), ("root", "after")])
    add_subtree(tree, "p_BT.json")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetExecutionState() == "Paused", timeout=3000)
    assert Mark.order == []
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert Mark.order == ["after"]


# ============================================================================ sub-trees that cannot run
@pytest.mark.parametrize(
    "file, content, message",
    [
        ("", None, "no sub-tree file is chosen"),
        ("missing_BT.json", None, "Could not read"),
        ("broken_BT.json", "{oops", "not a valid JSON file"),
        ("other_BT.json", json.dumps({"format": "x"}), "not a behavior tree file"),
    ],
)
def test_a_sub_tree_that_cannot_be_loaded_fails(qtbot, tree, tmp_path, file, content, message):
    if content is not None:
        (tmp_path / file).write_text(content, encoding="utf-8")
    node = add_subtree(tree, file)
    errors = []
    tree.nodeError.connect(lambda failed, error: errors.append((failed, error)))
    assert execute(qtbot, tree) == ["Failed"]
    assert node.GetStatus() is NodeStatus.FAILED
    assert message in node.GetError() and "cannot be run" in node.GetError()
    assert errors and errors[0][0] is node


def test_an_error_inside_the_sub_tree_is_reported_on_the_node(qtbot, tree, tmp_path):
    write_tree(tmp_path / "raise_BT.json", [node_item("r", "Raise", title="Exploder")], [("root", "r")])
    node = add_subtree(tree, "raise_BT.json")
    assert execute(qtbot, tree) == ["Failed"]
    assert node.GetError().startswith("Exploder: OnRun raised ValueError: boom")


def test_an_unknown_node_type_in_the_sub_tree_fails(qtbot, tree, tmp_path):
    write_tree(tmp_path / "unknown_BT.json", [node_item("u", "NotRegisteredAnywhere")], [("root", "u")])
    node = add_subtree(tree, "unknown_BT.json")
    assert execute(qtbot, tree) == ["Failed"]
    assert "not registered" in node.GetError()


def test_a_sub_tree_containing_itself_fails_instead_of_recursing(qtbot, tree, tmp_path):
    nodes = [node_item("s", SUB_TREE, fields={"File": "loop_BT.json"})]
    write_tree(tmp_path / "loop_BT.json", nodes, [("root", "s")])
    node = add_subtree(tree, "loop_BT.json")
    assert execute(qtbot, tree) == ["Failed"]
    assert "contains this Sub-Tree node itself" in node.GetError()


def test_a_sub_tree_running_the_main_tree_fails(qtbot, tree, tmp_path):
    nodes = [node_item("s", SUB_TREE, fields={"File": "tree_BT.json"})]
    write_tree(tmp_path / "back_BT.json", nodes, [("root", "s")])
    add_subtree(tree, "back_BT.json")
    assert tree.SaveTree(tree.GetFilePath())
    assert execute(qtbot, tree) == ["Failed"]


def test_nested_sub_trees_resolve_names_from_their_own_folder(qtbot, tree, tmp_path):
    os.makedirs(tmp_path / "lib")
    marks_file(tmp_path / "lib" / "leaf_BT.json", "deepest")
    write_tree(tmp_path / "lib" / "middle_BT.json", [node_item("s", SUB_TREE, fields={"File": "leaf_BT.json"}),
                                                       mark("m", "middle", 300.0)], [("root", "s"), ("root", "m")])
    add_subtree(tree, os.path.join("lib", "middle_BT.json"))
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["deepest", "middle"]


def test_the_same_sub_tree_twice_is_not_recursion(qtbot, tree, tmp_path):
    marks_file(tmp_path / "twice_BT.json", "x")
    add_subtree(tree, "twice_BT.json", 0.0)
    add_subtree(tree, "twice_BT.json", 400.0)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["x", "x"]


def test_on_run_is_never_called(bt):
    node = SubTreeNodeWidget()
    with pytest.raises(RuntimeError):
        node.OnRun(bt)
    node.deleteLater()


def test_set_file_from_a_worker_thread(qtbot, bt):
    node = add_subtree(bt, connect=False)
    thread = threading.Thread(target=lambda: node.SetFile("from_thread_BT.json"))
    thread.start()
    thread.join(5)
    qtbot.waitUntil(lambda: node.file_editor().text() == "from_thread_BT.json", timeout=2000)
    QApplication.processEvents()


# ============================================================================ sub-tree nodes on worker threads
def test_a_sub_tree_node_never_runs_two_calls_at_once(qtbot, make_widget, bt, tmp_path):
    Stubborn.active = Stubborn.most = 0
    Stubborn.touched, Stubborn.errors = [], []
    author = make_widget(node_types=[Stubborn])
    assert author.NewTree(str(tmp_path / "sub"))
    author.Connect(author.GetRootNode(), author.AddNode(Stubborn, 0, 200))
    assert author.SaveTree(author.GetFilePath())
    bt.RegisterNodeType(Stubborn)
    fast_config(bt)
    sub = bt.AddNode("Sub-Tree", 0, 200)
    sub.SetFile("sub_BT.json")
    bt.Connect(bt.GetRootNode(), sub)
    bt.Execute()
    qtbot.waitUntil(lambda: Stubborn.active == 1, timeout=3000)
    first = bt.executor().subtree_nodes()
    bt.Stop()
    bt.Execute()  # the new run's node waits for the interrupted call
    qtbot.waitUntil(lambda: not bt.IsExecuting(), timeout=5000)
    assert Stubborn.most == 1
    # The released nodes lived until their call returned (the call could use them), then went away.
    assert Stubborn.touched == [True, True] and Stubborn.errors == []
    qtbot.waitUntil(lambda: not any(shiboken6.isValid(node) for node in first), timeout=3000)
