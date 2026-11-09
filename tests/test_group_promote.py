"""Promote to Sub-Tree: a group's nodes become a sub-tree file run by a Sub-Tree node.

Requirement: the group's right-click menu has "Promote to Sub-Tree", which writes the group
to a behavior tree file named f"{group-name}_BT.json" (without a blackboard file) and
replaces the group with a Sub-Tree node loaded from that file.
"""

from __future__ import annotations

import json
import os

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from behavior_tree_widget import GroupNodeWidget, LeafNodeWidget, NotesNodeWidget, SubTreeNodeWidget
from behavior_tree_widget.groups import GROUP
from behavior_tree_widget.serialization import FORMAT_NAME

from conftest import fast_config, run_until_idle
from support.round10 import Mark as R10Mark, legacy_tree


class Mark(LeafNodeWidget):
    """Appends its "label" field to ``Mark.order``; fails when "fail" is checked."""

    _fields = {"label": "", "fail": False}
    RUN_IN_THREAD = False
    order: list = []

    def OnRun(self, tree):
        type(self).order.append(self.GetField("label"))
        return not self.GetField("fail")


@pytest.fixture
def tree(bt):
    bt.RegisterNodeType(Mark)
    Mark.order = []
    fast_config(bt)
    return bt


def add_mark(bt, label, x, y, fail=False):
    node = bt.AddNode("Mark", x, y)
    node.SetField("label", label)
    node.SetField("fail", fail)
    node.SetTitle(label)
    return node


def add_group(bt, x, y, width, height, name=None) -> GroupNodeWidget:
    group = bt.AddNode(GROUP, x, y)
    group.SetGroupSize(width, height)
    if name:
        group.SetTitle(name)
    QApplication.processEvents()
    return group


def group_around(bt, nodes, name=None, margin=30.0) -> GroupNodeWidget:
    """A group enclosing ``nodes`` (with ``margin`` pixels around them; the title bar above them)."""
    QApplication.processEvents()
    area = QRectF()
    for node in nodes:
        area = area.united(node._item.sceneBoundingRect())
    return add_group(
        bt, area.left() - margin, area.top() - margin - 40, area.width() + 2 * margin, area.height() + 2 * margin + 40, name
    )


def read(path) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def execute(qtbot, bt) -> list:
    results = []
    bt.executionFinished.connect(results.append)
    bt.Execute()
    run_until_idle(qtbot, bt)
    bt.executionFinished.disconnect()
    return results


def patrol(bt):
    """Root -> [before, Sequence(patrol: a, b) in a group, after]."""
    before = add_mark(bt, "before", -500, 200)
    sequence = bt.AddNode("Sequence", 0, 260)
    a = add_mark(bt, "a", -150, 420)
    b = add_mark(bt, "b", 150, 420)
    after = add_mark(bt, "after", 600, 200)
    root = bt.GetRootNode()
    for child in (before, sequence, after):
        bt.Connect(root, child)
    bt.Connect(sequence, a)
    bt.Connect(sequence, b)
    group = group_around(bt, [sequence, a, b], "patrol")
    return group, sequence, a, b


# ============================================================================ promoting
def test_promote_writes_the_sub_tree_file_and_replaces_the_group(qtbot, tree, tmp_path):
    group, sequence, a, b = patrol(tree)
    note = tree.AddNode("Notes", a._item.pos().x(), 360)
    note.SetNoteSize(80, 40)
    note.SetText("patrol notes")
    QApplication.processEvents()
    assert set(group.GetContainedNodes()) == {sequence, a, b, note}
    tree._set_modified(False)
    promoted = []
    tree.subTreePromoted.connect(lambda node, path: promoted.append((node, path)))
    sub = tree.PromoteGroupToSubTree(group)

    path = tmp_path / "patrol_BT.json"
    assert os.path.isfile(path)
    assert not os.path.exists(tmp_path / "patrol_BB.json")  # a sub-tree has no blackboard file
    assert promoted == [(sub, str(path))]
    # The group and its contents are replaced by a Sub-Tree node running the file.
    for node in (group, sequence, a, b, note):
        assert node not in tree.GetNodes()
    assert isinstance(sub, SubTreeNodeWidget) and sub in tree.GetNodes()
    assert sub.GetTitle() == "patrol" and sub.GetFile() == "patrol_BT.json"
    assert sub.GetResolvedFile() == str(path)
    assert [child.GetTitle() for child in tree.GetRootNode().GetChildren()] == ["before", "patrol", "after"]
    assert tree.GetSelectedNodes() == [sub]
    assert tree.IsModified()


def test_the_sub_tree_file_holds_the_group_contents_under_a_new_root(tree, tmp_path):
    group, sequence, a, b = patrol(tree)
    note = tree.AddNode("Notes", a._item.pos().x(), 360)
    note.SetNoteSize(80, 40)
    sequence_id, a_id, b_id, note_id = sequence.GetId(), a.GetId(), b.GetId(), note.GetId()
    a_pos = (a._item.pos().x(), a._item.pos().y())
    tree.PromoteGroupToSubTree(group)
    data = read(tmp_path / "patrol_BT.json")
    assert data["format"] == FORMAT_NAME and "blackboard" not in data
    nodes = {item["id"]: item for item in data["nodes"]}
    roots = [item for item in data["nodes"] if item["type"] == "Root"]
    assert len(roots) == 1
    root = roots[0]
    assert root["composite"] == "Sequence"  # the type of the parent (the main Root)
    assert set(nodes) == {root["id"], sequence_id, a_id, b_id, note_id}
    assert (nodes[a_id]["x"], nodes[a_id]["y"]) == a_pos  # positions are kept
    assert nodes[a_id]["fields"] == {"label": "a", "fail": False}
    assert nodes[note_id]["type"] == "Notes"
    connections = {(item["parent"], item["child"]) for item in data["connections"]}
    assert connections == {(root["id"], sequence_id), (sequence_id, a_id), (sequence_id, b_id)}
    assert root["y"] < min(item["y"] for item in data["nodes"] if item is not root)


def test_the_promoted_tree_runs_as_before(qtbot, tree):
    group, *_ = patrol(tree)
    assert execute(qtbot, tree) == ["Succeeded"]
    before = list(Mark.order)
    Mark.order.clear()
    tree.PromoteGroupToSubTree(group)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == before == ["before", "a", "b", "after"]


def test_the_promoted_tree_survives_save_and_load(qtbot, make_widget, tree, tmp_path):
    group, *_ = patrol(tree)
    tree.PromoteGroupToSubTree(group)
    assert tree.SaveTree(tree.GetFilePath())
    other = make_widget(node_types=[Mark])
    assert other.LoadTree(str(tmp_path / "tree_BT.json"))
    fast_config(other)
    Mark.order.clear()
    assert execute(qtbot, other) == ["Succeeded"]
    assert Mark.order == ["before", "a", "b", "after"]


def test_the_sub_tree_file_can_be_opened_as_a_tree(make_widget, tree, tmp_path):
    group, *_ = patrol(tree)
    tree.PromoteGroupToSubTree(group)
    other = make_widget(node_types=[Mark])
    assert other.LoadTree(str(tmp_path / "patrol_BT.json"))
    root = other.GetRootNode()
    assert [node.GetTitle() for node in root.GetChildren()] == ["Sequence"]
    assert [node.GetTitle() for node in root.GetChildren()[0].GetChildren()] == ["a", "b"]
    assert other.GetEntryNames() == []


def test_the_root_takes_the_composite_type_of_the_parent(qtbot, tree, tmp_path):
    selector = tree.AddNode("Selector", 0, 200)
    tree.Connect(tree.GetRootNode(), selector)
    first = add_mark(tree, "first", -200, 400, fail=True)
    second = add_mark(tree, "second", 100, 400)
    for node in (first, second):
        tree.Connect(selector, node)
    selector.SetMemory(False)
    group = group_around(tree, [first, second], "choice")
    tree.PromoteGroupToSubTree(group)
    root = next(item for item in read(tmp_path / "choice_BT.json")["nodes"] if item["type"] == "Root")
    assert (root["composite"], root["memory"]) == ("Selector", False)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["first", "second"]


def test_unconnected_contents_become_the_children_of_the_root(qtbot, tree, tmp_path):
    a = add_mark(tree, "a", -100, 300)
    b = add_mark(tree, "b", 300, 300)
    group = group_around(tree, [a, b], "loose")
    sub = tree.PromoteGroupToSubTree(group)
    assert sub.GetParent() is None
    data = read(tmp_path / "loose_BT.json")
    root_id = next(item["id"] for item in data["nodes"] if item["type"] == "Root")
    assert [item["child"] for item in data["connections"] if item["parent"] == root_id] == [a.GetId(), b.GetId()]
    tree.Connect(tree.GetRootNode(), sub)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["a", "b"]


def test_unconnected_contents_stay_unconnected_next_to_connected_ones(tree, tmp_path):
    connected = add_mark(tree, "connected", -100, 300)
    tree.Connect(tree.GetRootNode(), connected)
    loose = add_mark(tree, "loose", 300, 300)
    group = group_around(tree, [connected, loose], "mixed")
    sub = tree.PromoteGroupToSubTree(group)
    assert sub.GetParent() is tree.GetRootNode()
    data = read(tmp_path / "mixed_BT.json")
    assert loose.GetId() not in {item["child"] for item in data["connections"]}
    assert loose.GetId() in {item["id"] for item in data["nodes"]}


def test_nested_groups_go_to_the_file(tree, tmp_path):
    group, sequence, a, b = patrol(tree)
    rect = sequence._item.sceneBoundingRect()
    inner = add_group(tree, rect.left() - 5, rect.top() - 5, rect.width() + 10, rect.height() + 10, "inner")
    tree.PromoteGroupToSubTree(group)
    assert inner not in tree.GetNodes()
    items = read(tmp_path / "patrol_BT.json")["nodes"]
    assert [item["title"] for item in items if item["type"] == GROUP] == ["inner"]


def test_promote_keeps_the_place_among_the_siblings(tree):
    root = tree.GetRootNode()
    left = add_mark(tree, "left", -600, 200)
    middle = add_mark(tree, "middle", 0, 260)
    right = add_mark(tree, "right", 600, 200)
    for node in (left, middle, right):
        tree.Connect(root, node)
    group = group_around(tree, [middle], "mid")
    sub = tree.PromoteGroupToSubTree(group)
    assert root.GetChildren() == [left, sub, right]
    center = sub._item.sceneBoundingRect().center().x()
    assert left._item.sceneBoundingRect().center().x() < center < right._item.sceneBoundingRect().center().x()


def test_a_negation_parent_gets_the_sub_tree_as_its_child(qtbot, tree):
    negation = tree.AddNode("Negation", 0, 200)
    tree.Connect(tree.GetRootNode(), negation)
    failing = add_mark(tree, "inner", 0, 400, fail=True)
    tree.Connect(negation, failing)
    group = group_around(tree, [failing], "inverted")
    sub = tree.PromoteGroupToSubTree(group)
    assert negation.GetChild() is sub
    assert execute(qtbot, tree) == ["Succeeded"]


# ============================================================================ groups that cannot be promoted
def test_the_root_cannot_be_promoted(tree):
    group = add_group(tree, -100, -100, 500, 300)
    with pytest.raises(ValueError, match="Root"):
        tree.PromoteGroupToSubTree(group)


def test_an_empty_group_cannot_be_promoted(tree):
    group = add_group(tree, 0, 600, 300, 200)
    tree.AddNode("Notes", 20, 650)  # notes alone are not a tree
    with pytest.raises(ValueError, match="no node lies completely inside"):
        tree.PromoteGroupToSubTree(group)


def test_children_outside_the_group_block_the_promotion(tree):
    sequence = tree.AddNode("Sequence", 0, 260)
    inside = add_mark(tree, "inside", 0, 420)
    outside = add_mark(tree, "outside", 0, 900)
    tree.Connect(sequence, inside)
    tree.Connect(sequence, outside)
    group = group_around(tree, [sequence, inside])
    with pytest.raises(ValueError, match="children outside the group"):
        tree.PromoteGroupToSubTree(group)
    assert group in tree.GetNodes() and sequence in tree.GetNodes()


def test_contents_with_different_parents_block_the_promotion(tree):
    first = tree.AddNode("Sequence", -400, 200)
    second = tree.AddNode("Sequence", 400, 200)
    for node in (first, second):
        tree.Connect(tree.GetRootNode(), node)
    a = add_mark(tree, "a", -100, 500)
    b = add_mark(tree, "b", 300, 500)
    tree.Connect(first, a)
    tree.Connect(second, b)
    group = group_around(tree, [a, b])
    with pytest.raises(ValueError, match="more than one node outside"):
        tree.PromoteGroupToSubTree(group)


@pytest.mark.parametrize("name", ["a/b", "a\\b", "what?", "star*", "dots.", "  "])
def test_group_names_that_are_no_file_names_are_refused(tree, tmp_path, name):
    group = group_around(tree, [add_mark(tree, "a", 0, 600)])
    group.SetTitle(name)
    with pytest.raises(ValueError):
        tree.PromoteGroupToSubTree(group)
    assert sorted(os.listdir(tmp_path)) == ["tree_BB.json", "tree_BT.json"]


def test_a_group_named_like_the_tree_is_refused(tree):
    group = group_around(tree, [add_mark(tree, "a", 0, 600)], "tree")
    with pytest.raises(ValueError, match="names the file of this tree"):
        tree.PromoteGroupToSubTree(group)


def test_an_existing_file_is_replaced_only_when_asked(tree, tmp_path):
    (tmp_path / "patrol_BT.json").write_text("{}", encoding="utf-8")
    group, *_ = patrol(tree)
    with pytest.raises(FileExistsError):
        tree.PromoteGroupToSubTree(group)
    assert (tmp_path / "patrol_BT.json").read_text(encoding="utf-8") == "{}"
    tree.PromoteGroupToSubTree(group, overwrite=True)
    assert read(tmp_path / "patrol_BT.json")["format"] == FORMAT_NAME


def test_promote_is_refused_while_executing(qtbot, tree):
    group, *_ = patrol(tree)
    tree.AddNode("SlowCancelable", 900, 200)
    tree.Connect(tree.GetRootNode(), tree.GetNodes()[-1])
    tree.SetLoopExecution(True)
    tree.Execute()
    try:
        with pytest.raises(RuntimeError):
            tree.PromoteGroupToSubTree(group)
    finally:
        tree.Stop()


def test_only_groups_of_the_tree_can_be_promoted(make_widget, tree):
    other = make_widget()
    with pytest.raises(ValueError):
        tree.PromoteGroupToSubTree(other.GetRootNode())


# ============================================================================ the menu
def menu_on(bt, dialogs, group):
    rect = group.header_rect()
    point = bt.view().mapFromScene(group._item.mapToScene(QPointF(rect.right() - 16, rect.center().y())))
    viewport = bt.view().viewport()
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point)))


def promote_action(menu):
    return next(a for a in menu.actions() if a.objectName() == "PromoteToSubTree")


def test_promote_from_the_menu(tree, tmp_path, dialogs):
    group, *_ = patrol(tree)
    dialogs.menu_handler = lambda menu: promote_action(menu).trigger()
    menu_on(tree, dialogs, group)
    assert os.path.isfile(tmp_path / "patrol_BT.json")
    assert group not in tree.GetNodes()
    assert "question" not in dialogs.kinds() and "warning" not in dialogs.kinds()


@pytest.mark.parametrize("answer, replaced", [(QMessageBox.StandardButton.Yes, True), (QMessageBox.StandardButton.No, False)])
def test_the_menu_asks_before_replacing_a_file(tree, tmp_path, dialogs, answer, replaced):
    (tmp_path / "patrol_BT.json").write_text("{}", encoding="utf-8")
    group, *_ = patrol(tree)
    dialogs.question_answer = answer
    dialogs.menu_handler = lambda menu: promote_action(menu).trigger()
    menu_on(tree, dialogs, group)
    assert "question" in dialogs.kinds()
    assert (group not in tree.GetNodes()) == replaced
    assert ((tmp_path / "patrol_BT.json").read_text(encoding="utf-8") != "{}") == replaced


def test_the_menu_explains_why_a_group_cannot_be_promoted(tree, dialogs):
    group = add_group(tree, 0, 600, 300, 200)
    dialogs.menu_handler = lambda menu: promote_action(menu).trigger()
    menu_on(tree, dialogs, group)
    warnings = [args for kind, args in dialogs.shown if kind == "warning"]
    assert warnings and "no node lies completely inside" in warnings[0][2]
    assert group in tree.GetNodes()


def test_the_menu_reports_a_file_that_cannot_be_written(tree, tmp_path, dialogs):
    group, *_ = patrol(tree)
    os.makedirs(tmp_path / "patrol_BT.json")  # a folder of that name
    dialogs.question_answer = QMessageBox.StandardButton.Yes
    dialogs.menu_handler = lambda menu: promote_action(menu).trigger()
    menu_on(tree, dialogs, group)
    assert "critical" in dialogs.kinds()
    assert group in tree.GetNodes()


class Unsaveable(LeafNodeWidget):
    _fields = {"handle": 1}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


def test_values_that_cannot_be_written_are_left_out(tree, tmp_path):
    tree.RegisterNodeType(Unsaveable)
    node = tree.AddNode("Unsaveable", 0, 600)
    node.SetField("handle", object())
    group = group_around(tree, [node], "odd")
    tree.PromoteGroupToSubTree(group)
    item = next(entry for entry in read(tmp_path / "odd_BT.json")["nodes"] if entry["type"] == "Unsaveable")
    assert item["fields"] == {}


def test_notes_only_inside_a_group_with_nodes_are_kept_as_notes(make_widget, tree, tmp_path):
    group, sequence, a, b = patrol(tree)
    note = tree.AddNode("Notes", a._item.pos().x(), 360)
    note.SetNoteSize(80, 40)
    note.SetText("kept")
    tree.PromoteGroupToSubTree(group)
    other = make_widget(node_types=[Mark])
    assert other.LoadTree(str(tmp_path / "patrol_BT.json"))
    notes = [node for node in other.GetNodes() if isinstance(node, NotesNodeWidget)]
    assert [note.GetText() for note in notes] == ["kept"]


# ============================================================================ names of files of earlier versions
def test_a_group_cannot_take_the_name_a_legacy_tree_is_saved_to(make_widget, tmp_path):
    legacy_tree(tmp_path / "robot.json")
    widget = make_widget()
    assert widget.LoadTree(str(tmp_path / "robot.json"))
    widget.AddNode("Succeed", 0, 600)
    group = widget.AddNode("Group", -100, 520)
    group.SetGroupSize(500, 300)
    group.SetTitle("robot")
    QApplication.processEvents()
    with pytest.raises(ValueError, match="names the file of this tree"):
        widget.PromoteGroupToSubTree(group)
    assert not os.path.exists(tmp_path / "robot_BT.json")


# ============================================================================ Promote to Sub-Tree
def test_promote_refuses_a_group_with_an_outside_sibling_between_its_nodes(bt):
    bt.RegisterNodeType(R10Mark)
    nodes = {}
    for title, x, y in (("A", -400, 300), ("X", 0, 700), ("B", 400, 300)):
        nodes[title] = bt.AddNode("Mark", x, y)
        nodes[title].SetTitle(title)
        bt.Connect(bt.GetRootNode(), nodes[title])
    group = bt.AddNode("Group", -450, 250)
    group.SetGroupSize(1100, 250)
    QApplication.processEvents()
    assert set(group.GetContainedNodes()) == {nodes["A"], nodes["B"]}
    with pytest.raises(ValueError, match="'X' outside the group runs between"):
        bt.PromoteGroupToSubTree(group)
    assert bt.GetRootNode().GetChildren() == [nodes["A"], nodes["X"], nodes["B"]]
    assert group in bt.GetNodes()
