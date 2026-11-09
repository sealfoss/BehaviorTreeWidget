"""Zoom In / Go Back: a Sub-Tree node's tree shown (and edited) in place of the main tree.

Requirement: a Sub-Tree node's context menu has "Zoom In", which replaces the tree shown by
the widget with the graph of that sub-tree. The sub-tree can be modified while zoomed in; the
blackboard stays the main tree's. The "Go Back" button (BehaviorTreeView.ui, objectName
GoBack) is disabled by default and enabled while zoomed in; zooming in works over several
levels and Go Back returns one level at a time up to the main tree, where it is disabled again.
"""

from __future__ import annotations

import json
import os

import pytest
import shiboken6
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from behavior_tree_widget import (
    GroupNodeWidget,
    LeafNodeWidget,
    NodeStatus,
    RootNodeWidget,
    Status,
    SubTreeNodeWidget,
    TreeFileError,
)
from behavior_tree_widget import document as document_module
from behavior_tree_widget.canvas import TreeView
from behavior_tree_widget.groups import GROUP
from behavior_tree_widget.serialization import FORMAT_NAME
from behavior_tree_widget.subtree import SUB_TREE

from conftest import fast_config, file_blackboard, read_json, run_until_idle


# ============================================================================ node types
class Mark(LeafNodeWidget):
    """Appends its "label" field to ``Mark.order``; fails when "fail" is checked."""

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


# ============================================================================ helpers
def node_item(node_id, type_name, x=0.0, y=200.0, **extra) -> dict:
    return {"id": node_id, "type": type_name, "title": extra.pop("title", type_name), "x": x, "y": y, **extra}


def tree_document(nodes: list, connections: list, composite: str = "Sequence", tick: int = 100) -> dict:
    root = node_item("root", "Root", 0.0, 0.0, composite=composite, memory=True)
    return {
        "format": FORMAT_NAME,
        "version": 1,
        "config": {"tick_interval_ms": tick, "repeat": False, "restore_blackboard": False, "default_memory": True},
        "nodes": [root, *nodes],
        "connections": [{"parent": parent, "child": child} for parent, child in connections],
    }


def write_tree(path, nodes: list, connections: list, composite: str = "Sequence") -> str:
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(tree_document(nodes, connections, composite), stream)
    return str(path)


def mark(node_id, label, x=0.0, fail=False) -> dict:
    return node_item(node_id, "Mark", x, 200.0, title=label, fields={"label": label, "fail": fail})


def sub(node_id, file, x=0.0) -> dict:
    return node_item(node_id, SUB_TREE, x, 200.0, title=node_id, fields={"File": file})


def same(first, second) -> bool:
    return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(os.path.abspath(str(second)))


def by_id(nodes, node_id):
    return next(node for node in nodes if node.GetId() == node_id)


def by_title(nodes, title):
    return next(node for node in nodes if node.GetTitle() == title)


def labels(bt) -> list:
    return sorted(node.GetField("label") for node in bt.GetNodes() if isinstance(node, Mark))


def menu_action(menu, name: str):
    return next((action for action in menu.actions() if action.objectName() == name), None)


def zoom_action(bt, node):
    return menu_action(bt.view().build_node_menu(node), "ZoomIn")


def add_mark(bt, label, x, y, parent=None, fail=False):
    node = bt.AddNode("Mark", x, y)
    node.SetField("label", label)
    node.SetField("fail", fail)
    node.SetTitle(label)
    if parent is not None:
        bt.Connect(parent, node)
    return node


def add_subtree(bt, file: str = "", x=0.0, y=200.0, connect=True) -> SubTreeNodeWidget:
    node = bt.AddNode(SUB_TREE, x, y)
    if file:
        node.SetFile(file)
    if connect:
        bt.Connect(bt.GetRootNode(), node)
    return node


def group_around(bt, nodes, name, margin=30.0) -> GroupNodeWidget:
    QApplication.processEvents()
    area = QRectF()
    for node in nodes:
        area = area.united(node._item.sceneBoundingRect())
    group = bt.AddNode(GROUP, area.left() - margin, area.top() - margin - 40)
    group.SetGroupSize(area.width() + 2 * margin, area.height() + 2 * margin + 40)
    group.SetTitle(name)
    QApplication.processEvents()
    return group


def execute(qtbot, bt) -> list:
    results = []
    bt.executionFinished.connect(results.append)
    bt.Execute()
    run_until_idle(qtbot, bt)
    bt.executionFinished.disconnect()
    return results


def file_bytes(path) -> bytes:
    with open(path, "rb") as stream:
        return stream.read()


def title(bt) -> str:
    return bt.window().windowTitle()


def entries(path) -> dict:
    return {entry["name"]: entry["value"] for entry in file_blackboard(path) or []}


def live_tree_views(bt) -> list:
    return [view for view in bt.findChildren(TreeView) if shiboken6.isValid(view)]


@pytest.fixture
def tree(bt):
    for cls in (Mark, Spin):
        bt.RegisterNodeType(cls)
    Mark.order = []
    fast_config(bt)
    assert bt.SaveTree(bt.GetFilePath())
    return bt


@pytest.fixture
def chain(tree, tmp_path):
    """main (tree_BT.json): Root -> S (a_BT.json); a: Root -> [to_b (b_BT.json), a1];
    b: Root -> [to_c (c_BT.json), b1]; c: Root -> c1. Returns S."""
    write_tree(tmp_path / "c_BT.json", [mark("c1", "c1")], [("root", "c1")])
    write_tree(tmp_path / "b_BT.json", [sub("to_c", "c_BT.json"), mark("b1", "b1", 300.0)], [("root", "to_c"), ("root", "b1")])
    write_tree(tmp_path / "a_BT.json", [sub("to_b", "b_BT.json"), mark("a1", "a1", 300.0)], [("root", "to_b"), ("root", "a1")])
    node = add_subtree(tree, "a_BT.json")
    node.SetTitle("S")
    assert tree.SaveTree(tree.GetFilePath())
    assert not tree.IsModified()
    return node


# ============================================================================ the button and the menu item
def test_go_back_is_disabled_by_default(make_widget, bt):
    unloaded = make_widget()
    for widget in (unloaded, bt):
        button = widget.button("GoBack")
        assert button.objectName() == "GoBack"
        assert not button.isEnabled()
        assert widget.GetZoomDepth() == 0
        assert widget.GoBack() is False
    assert unloaded.GetZoomPath() == []
    assert len(bt.GetZoomPath()) == 1 and same(bt.GetZoomPath()[0], bt.GetFilePath())
    assert bt.view() is bt.main_view()
    with pytest.raises(RuntimeError):
        bt.SaveSubTree()  # nothing is zoomed into


def test_only_sub_tree_nodes_have_zoom_in(tree, chain):
    assert zoom_action(tree, tree.GetRootNode()) is None
    assert zoom_action(tree, add_mark(tree, "m", 400, 200)) is None
    assert zoom_action(tree, tree.AddNode("Sequence", 600, 200)) is None
    action = zoom_action(tree, chain)
    assert action is not None and action.isEnabled() and action.text() == "Zoom In"


def test_zoom_in_is_disabled_without_a_file(tree):
    node = add_subtree(tree, "")
    action = zoom_action(tree, node)
    assert action is not None and not action.isEnabled()
    assert "choose a file first" in action.text()
    node.SetFile("missing_BT.json")  # a missing file can still be zoomed into (it is offered to be created)
    assert zoom_action(tree, node).isEnabled()


def test_right_click_on_a_sub_tree_node_offers_zoom_in(tree, chain, dialogs):
    QApplication.processEvents()
    view = tree.view()
    # The title bar of the node (its body holds the File line edit).
    point = view.mapFromScene(chain._item.mapToScene(QPointF(chain.width() / 2.0, 6.0)))
    viewport = view.viewport()
    dialogs.menu_handler = lambda menu: menu_action(menu, "ZoomIn").trigger()
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point)))
    assert dialogs.menus and menu_action(dialogs.menus[-1], "ZoomIn") is not None
    assert tree.GetZoomDepth() == 1


def test_the_menu_item_zooms_in(tree, chain, tmp_path, dialogs):
    main_root = tree.GetRootNode()
    zoom_action(tree, chain).trigger()
    assert tree.GetZoomDepth() == 1
    assert tree.button("GoBack").isEnabled()
    assert tree.GetRootNode() is not main_root
    assert labels(tree) == ["a1"]
    assert by_id(tree.GetNodes(), "to_b").GetFile() == "b_BT.json"
    assert not tree.main_view().isVisible()
    assert tree.view() is not tree.main_view() and tree.view().isVisible()
    assert same(tree.view().base_folder, tmp_path)
    assert dialogs.kinds() == []  # nothing to ask, nothing to report
    assert not tree.IsModified()


# ============================================================================ navigation
def test_three_levels_deep_and_back_to_the_top(qtbot, tree, chain, tmp_path):
    zooms, selections = [], []
    tree.zoomChanged.connect(lambda depth, path: zooms.append((depth, path)))
    tree.nodeSelectionChanged.connect(lambda: selections.append(1))
    main_view = tree.main_view()
    main_nodes = list(tree.GetNodes())
    paths = [tree.GetFilePath(), str(tmp_path / "a_BT.json"), str(tmp_path / "b_BT.json"), str(tmp_path / "c_BT.json")]
    titles = [
        "Behavior Tree - tree_BT.json",
        "Behavior Tree - tree_BT.json > a_BT.json",
        "Behavior Tree - tree_BT.json > a_BT.json > b_BT.json",
        "Behavior Tree - tree_BT.json > a_BT.json > b_BT.json > c_BT.json",
    ]
    assert title(tree) == titles[0]

    views = [main_view]
    tree.ZoomIn(chain)
    views.append(tree.view())
    assert labels(tree) == ["a1"]
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))
    views.append(tree.view())
    assert labels(tree) == ["b1"]
    tree.ZoomIn(by_id(tree.GetNodes(), "to_c"))
    views.append(tree.view())
    assert labels(tree) == ["c1"]
    assert len(set(map(id, views))) == 4
    assert tree.GetZoomDepth() == 3
    assert [os.path.normcase(p) for p in tree.GetZoomPath()] == [os.path.normcase(p) for p in paths]
    assert title(tree) == titles[3]
    assert tree.button("GoBack").isEnabled()
    assert [view.isVisible() for view in views] == [False, False, False, True]
    assert tree.GetFilePath() == paths[0]  # still the main tree's

    for depth in (2, 1, 0):
        assert tree.GoBack() is True
        assert tree.GetZoomDepth() == depth
        assert tree.view() is views[depth] and views[depth].isVisible()
        assert title(tree) == titles[depth]
        assert tree.button("GoBack").isEnabled() == (depth > 0)
        assert len(tree.GetZoomPath()) == depth + 1
    assert tree.GetNodes() == main_nodes
    assert tree.GoBack() is False
    assert not tree.button("GoBack").isEnabled()
    assert [(depth, os.path.normcase(path)) for depth, path in zooms] == [
        (1, os.path.normcase(paths[1])),
        (2, os.path.normcase(paths[2])),
        (3, os.path.normcase(paths[3])),
        (2, os.path.normcase(paths[2])),
        (1, os.path.normcase(paths[1])),
        (0, os.path.normcase(paths[0])),
    ]
    assert len(selections) >= 6  # the selection shown changes with every level
    # The views of the levels left are deleted.
    qtbot.waitUntil(lambda: not any(shiboken6.isValid(view) for view in views[1:]), timeout=3000)
    assert shiboken6.isValid(main_view) and main_view.isVisible()
    assert not tree.IsModified()


def test_the_go_back_button_goes_up_one_level(tree, chain):
    tree.ZoomIn(chain)
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))
    tree.button("GoBack").click()
    assert tree.GetZoomDepth() == 1 and labels(tree) == ["a1"]
    tree.button("GoBack").click()
    assert tree.GetZoomDepth() == 0 and tree.view() is tree.main_view()
    assert not tree.button("GoBack").isEnabled()


def test_zooming_in_again_shows_the_file_as_saved(tree, chain):
    tree.ZoomIn(chain)
    add_mark(tree, "unsaved", 600, 200, tree.GetRootNode())
    assert tree.GoBack(save=False)
    tree.ZoomIn(chain)
    assert labels(tree) == ["a1"]
    assert not tree.IsModified()


# ============================================================================ editing a sub-tree
def test_the_editing_api_acts_on_the_tree_shown(tree, chain, tmp_path):
    main_nodes = list(tree.main_view().nodes())
    tree.ZoomIn(chain)
    root = tree.GetRootNode()
    assert isinstance(root, RootNodeWidget) and root.GetId() == "root"
    added = add_mark(tree, "a2", 600, 200, root)
    assert added in tree.GetNodes() and added.GetParent() is root
    assert added not in tree.main_view().nodes()
    assert tree.main_view().nodes() == main_nodes
    tree.SelectNodes([added])
    assert tree.GetSelectedNodes() == [added]
    tree.Disconnect(added)
    assert added.GetParent() is None
    tree.RemoveNode(added)
    assert added not in tree.GetNodes()
    with pytest.raises(ValueError):
        tree.Connect(chain, root)  # a node of the hidden main tree
    # The file, configuration and blackboard stay the main tree's.
    assert same(tree.GetFilePath(), tmp_path / "tree_BT.json")
    assert tree.GetConfig().tick_interval_ms == 5  # a_BT.json has 100
    assert tree.IsModified()


def test_edits_of_the_sub_tree_mark_the_widget_modified(tree, chain):
    changes = []
    tree.modifiedChanged.connect(changes.append)
    tree.ZoomIn(chain)
    QApplication.processEvents()
    assert not tree.IsModified() and changes == []
    by_title(tree.GetNodes(), "a1").SetTitle("renamed")
    assert tree.IsModified() and changes == [True]
    assert tree.GoBack(save=False)
    assert not tree.IsModified() and changes == [True, False]  # the main tree was never changed


def test_a_field_edit_in_the_sub_tree_marks_it_modified(tree, chain):
    tree.ZoomIn(chain)
    by_id(tree.GetNodes(), "a1").SetField("fail", True)
    assert tree.IsModified()
    assert tree.SaveSubTree() is True
    assert not tree.IsModified()
    item = by_id_item(read_json(tree.GetZoomPath()[-1]), "a1")
    assert item["fields"]["fail"] is True


def by_id_item(document, node_id) -> dict:
    return next(item for item in document["nodes"] if item["id"] == node_id)


def test_the_save_button_writes_the_sub_tree_file(tree, chain, tmp_path, dialogs):
    path = tmp_path / "a_BT.json"
    main_before = file_bytes(tree.GetFilePath())
    saved_sub, saved_tree, changes = [], [], []
    tree.subTreeSaved.connect(saved_sub.append)
    tree.treeSaved.connect(saved_tree.append)
    tree.modifiedChanged.connect(changes.append)
    tree.ZoomIn(chain)
    added = add_mark(tree, "a2", 600, 200, tree.GetRootNode())
    assert tree.IsModified()
    tree.button("Save").click()
    assert "save_dialog" not in dialogs.kinds()
    assert dialogs.kinds() == []
    data = read_json(path)
    assert data["format"] == FORMAT_NAME
    assert "blackboard" not in data
    assert data["config"]["tick_interval_ms"] == 100  # the sub-tree's own options are kept
    assert by_id_item(data, added.GetId())["fields"]["label"] == "a2"
    assert {"parent": "root", "child": added.GetId()} in data["connections"]
    assert {item["id"] for item in data["nodes"]} == {"root", "to_b", "a1", added.GetId()}
    assert not os.path.exists(tmp_path / "a_BB.json")
    assert file_bytes(tree.GetFilePath()) == main_before
    assert [os.path.normcase(p) for p in saved_sub] == [os.path.normcase(str(path))]
    assert saved_tree == []
    assert not tree.IsModified()
    assert changes == [True, False]
    assert tree.GetZoomDepth() == 1  # saving does not go back


def test_save_sub_tree_writes_the_level_shown(tree, chain, tmp_path):
    tree.ZoomIn(chain)
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))
    by_id(tree.GetNodes(), "b1").SetTitle("b-renamed")
    a_before = file_bytes(tmp_path / "a_BT.json")
    assert tree.SaveSubTree() is True
    assert by_id_item(read_json(tmp_path / "b_BT.json"), "b1")["title"] == "b-renamed"
    assert file_bytes(tmp_path / "a_BT.json") == a_before
    assert not os.path.exists(tmp_path / "b_BB.json")
    assert not tree.IsModified()


def test_save_tree_while_zoomed_saves_the_main_tree_only(tree, chain, tmp_path):
    tree.ZoomIn(chain)
    add_mark(tree, "a2", 600, 200, tree.GetRootNode())
    config = tree.GetConfig()
    config.tick_interval_ms = 7
    tree.SetConfig(config)  # a change of the main tree
    a_before = file_bytes(tmp_path / "a_BT.json")
    assert tree.SaveTree(tree.GetFilePath())
    main = read_json(tree.GetFilePath())
    assert main["config"]["tick_interval_ms"] == 7
    assert {item["type"] for item in main["nodes"]} == {"Root", SUB_TREE}  # not the sub-tree's nodes
    assert file_bytes(tmp_path / "a_BT.json") == a_before
    assert tree.IsModified()  # the sub-tree still has unsaved changes
    assert tree.GetZoomDepth() == 1
    assert title(tree) == "Behavior Tree - tree_BT.json > a_BT.json"


# ============================================================================ going back with unsaved changes
@pytest.mark.parametrize(
    "answer, depth, written",
    [
        (QMessageBox.StandardButton.Save, 0, True),
        (QMessageBox.StandardButton.Discard, 0, False),
        (QMessageBox.StandardButton.Cancel, 1, False),
    ],
)
def test_go_back_asks_to_save_a_modified_sub_tree(tree, chain, tmp_path, dialogs, answer, depth, written):
    path = tmp_path / "a_BT.json"
    before = file_bytes(path)
    tree.ZoomIn(chain)
    added = add_mark(tree, "a2", 600, 200, tree.GetRootNode())
    dialogs.question_answer = answer
    tree.button("GoBack").click()
    questions = [args for kind, args in dialogs.shown if kind == "question"]
    assert len(questions) == 1
    assert "a_BT.json" in questions[0][2]
    buttons = questions[0][3]
    for button in (QMessageBox.StandardButton.Save, QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Cancel):
        assert buttons & button
    assert tree.GetZoomDepth() == depth
    assert (file_bytes(path) != before) == written
    if written:
        assert any(item["id"] == added.GetId() for item in read_json(path)["nodes"])
    assert tree.IsModified() == (depth == 1)
    dialogs.question_answer = QMessageBox.StandardButton.Discard


def test_go_back_does_not_ask_without_changes(tree, chain, tmp_path, dialogs):
    path = tmp_path / "a_BT.json"
    before = os.stat(path).st_mtime_ns, file_bytes(path)
    tree.ZoomIn(chain)
    dialogs.question_answer = QMessageBox.StandardButton.Cancel
    tree.button("GoBack").click()
    assert dialogs.kinds() == []
    assert tree.GetZoomDepth() == 0
    assert (os.stat(path).st_mtime_ns, file_bytes(path)) == before
    dialogs.question_answer = QMessageBox.StandardButton.Discard


@pytest.mark.parametrize("save", [True, False])
def test_go_back_save_argument(tree, chain, tmp_path, dialogs, save):
    path = tmp_path / "a_BT.json"
    before = file_bytes(path)
    saved = []
    tree.subTreeSaved.connect(saved.append)
    tree.ZoomIn(chain)
    add_mark(tree, "a2", 600, 200, tree.GetRootNode())
    assert tree.GoBack(save=save) is True
    assert dialogs.kinds() == []
    assert tree.GetZoomDepth() == 0
    assert (file_bytes(path) != before) == save
    assert len(saved) == int(save)
    assert not tree.IsModified()
    tree.ZoomIn(chain)
    assert labels(tree) == (["a1", "a2"] if save else ["a1"])


def test_go_back_keeps_the_level_when_saving_fails(tree, chain, monkeypatch, dialogs):
    tree.ZoomIn(chain)
    add_mark(tree, "a2", 600, 200, tree.GetRootNode())

    def fail(*_args, **_kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(document_module, "write_json_atomic", fail)  # sub-trees are written by their document
    with pytest.raises(OSError):
        tree.GoBack(save=True)
    assert tree.GetZoomDepth() == 1 and tree.IsModified()
    dialogs.question_answer = QMessageBox.StandardButton.Save
    tree.button("GoBack").click()
    assert "critical" in dialogs.kinds()
    assert tree.GetZoomDepth() == 1 and tree.IsModified()
    with pytest.raises(OSError):
        tree.SaveSubTree()
    dialogs.question_answer = QMessageBox.StandardButton.Discard


# ============================================================================ the blackboard
def test_the_blackboard_stays_the_main_trees(qtbot, tree, chain, tmp_path):
    tree.AddEntry("mode", "String", "idle")
    qtbot.waitUntil(lambda: entries(tree.GetFilePath()) == {"mode": "idle"}, timeout=3000)
    with open(tmp_path / "a_BB.json", "w", encoding="utf-8") as stream:  # never loaded
        json.dump({"format": "behavior_tree_widget_blackboard", "version": 1,
                   "blackboard": [{"name": "other", "type": "Integer", "value": 1}]}, stream)
    a_bb = file_bytes(tmp_path / "a_BB.json")
    tree.ZoomIn(chain)
    assert tree.GetEntryNames() == ["mode"]
    assert same(tree.GetBlackboardFilePath(), tmp_path / "tree_BB.json")
    tree.AddEntry("count", "Integer", 3)
    tree.SetEntry("busy", "mode")
    qtbot.waitUntil(lambda: entries(tree.GetFilePath()) == {"mode": "busy", "count": 3}, timeout=3000)
    assert file_bytes(tmp_path / "a_BB.json") == a_bb
    assert not tree.IsModified()  # blackboard changes are written at once, not unsaved changes


def test_a_set_node_added_while_zoomed_writes_the_main_blackboard(qtbot, tree, chain, tmp_path):
    tree.AddEntry("seen", "Bool", False)
    tree.ZoomIn(chain)
    set_node = tree.AddNode("Set", 600, 200)
    set_node.SetValueName("seen")
    set_node.SetLiteralValue(True)
    tree.Connect(tree.GetRootNode(), set_node)
    a_before = file_bytes(tmp_path / "a_BT.json")
    assert execute(qtbot, tree) == ["Succeeded"]
    assert tree.GetEntry("seen") is True
    qtbot.waitUntil(lambda: entries(tree.GetFilePath()).get("seen") is True, timeout=3000)
    assert not os.path.exists(tmp_path / "a_BB.json")
    assert file_bytes(tmp_path / "a_BT.json") == a_before  # run as shown, not saved
    assert tree.IsModified()


# ============================================================================ executing while zoomed in
def test_execute_runs_the_main_tree_with_the_shown_unsaved_changes(qtbot, tree, chain, tmp_path):
    a_before = file_bytes(tmp_path / "a_BT.json")
    tree.ZoomIn(chain)
    add_mark(tree, "a2", 600, 200, tree.GetRootNode())
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["c1", "b1", "a1", "a2"]
    assert file_bytes(tmp_path / "a_BT.json") == a_before
    assert chain.GetStatus() is NodeStatus.SUCCEEDED  # the Sub-Tree node of the (hidden) main tree
    # A deeper level's unsaved change is run as well.
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))
    by_id(tree.GetNodes(), "b1").SetField("fail", True)
    Mark.order = []
    assert execute(qtbot, tree) == ["Failed"]
    assert Mark.order == ["c1", "b1"]
    assert chain.GetStatus() is NodeStatus.FAILED


def test_every_sub_tree_node_of_the_shown_file_runs_it_as_shown(qtbot, tree, chain):
    second = add_subtree(tree, "a_BT.json", 400.0)
    second.SetTitle("S2")
    tree.ZoomIn(chain)
    add_mark(tree, "a2", 600, 200, tree.GetRootNode())
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["c1", "b1", "a1", "a2"] * 2


def test_the_nodes_shown_mirror_the_statuses_of_their_run(qtbot, tree, chain):
    tree.ZoomIn(chain)
    level_a = list(tree.GetNodes())
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))
    level_b = list(tree.GetNodes())
    changed = []
    tree.nodeStatusChanged.connect(lambda node, status: changed.append((node, status)))
    assert execute(qtbot, tree) == ["Succeeded"]
    for node in level_a + level_b:
        assert node.GetStatus() is NodeStatus.SUCCEEDED, node.GetTitle()
    b1 = by_id(level_b, "b1")
    assert (b1, "Succeeded") in changed
    assert any(node is by_id(level_a, "a1") for node, _ in changed)
    # Reset shows them Ready again.
    tree.Reset()
    assert all(node.GetStatus() is NodeStatus.READY for node in level_a + level_b)


def test_failures_inside_a_shown_sub_tree_show_on_its_nodes(qtbot, tree, chain):
    tree.ZoomIn(chain)
    a1 = by_id(tree.GetNodes(), "a1")
    a1.SetField("fail", True)
    assert execute(qtbot, tree) == ["Failed"]
    assert a1.GetStatus() is NodeStatus.FAILED
    assert tree.GetRootNode().GetStatus() is NodeStatus.FAILED
    assert by_id(tree.GetNodes(), "to_b").GetStatus() is NodeStatus.SUCCEEDED


def test_only_the_run_of_the_entered_sub_tree_node_is_mirrored(qtbot, tree, tmp_path):
    write_tree(tmp_path / "spin_BT.json", [node_item("sp", "Spin", fields={"ticks": 3})], [("root", "sp")])
    first = add_subtree(tree, "spin_BT.json", 0.0)
    second = add_subtree(tree, "spin_BT.json", 400.0)
    tree.ZoomIn(second)
    shown_spin = by_id(tree.GetNodes(), "sp")
    changed = []
    tree.nodeStatusChanged.connect(lambda node, status: changed.append((node, status)))
    assert execute(qtbot, tree) == ["Succeeded"]
    events = [(node, status) for node, status in changed]

    def index(node, status):
        return next(i for i, (n, s) in enumerate(events) if n is node and s == status)

    # While the first node runs the file, the node shown stays Ready; it follows the second run.
    assert index(shown_spin, "Running") > index(first, "Succeeded")
    assert index(shown_spin, "Running") > index(first, "Running")
    assert shown_spin.GetStatus() is NodeStatus.SUCCEEDED
    assert sum(1 for n, s in events if n is shown_spin and s == "Running") == 1


def test_all_views_are_locked_while_executing(qtbot, tree, tmp_path):
    write_tree(tmp_path / "spin_BT.json", [node_item("sp", "Spin", fields={"ticks": 100000})], [("root", "sp")])
    node = add_subtree(tree, "spin_BT.json")
    assert tree.SaveTree(tree.GetFilePath())
    tree.ZoomIn(node)
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: tree.GetExecutionState() == "Running", timeout=3000)
        assert tree.view().is_locked() and tree.main_view().is_locked()
        with pytest.raises(RuntimeError):
            tree.AddNode("Mark", 600, 200)
        with pytest.raises(RuntimeError):
            tree.RemoveNode(by_id(tree.GetNodes(), "sp"))
        menu = tree.view().build_node_menu(by_id(tree.GetNodes(), "sp"))
        assert not menu_action(menu, "Delete").isEnabled()
        shown_spin = by_id(tree.GetNodes(), "sp")
        qtbot.waitUntil(lambda: shown_spin.GetStatus() is NodeStatus.RUNNING, timeout=3000)
        assert not tree.IsModified()
    finally:
        tree.Stop()
    assert not tree.view().is_locked() and not tree.main_view().is_locked()
    assert by_id(tree.GetNodes(), "sp").GetStatus() is NodeStatus.READY
    add_mark(tree, "after", 600, 200)  # editable again


def test_zoom_in_and_go_back_while_executing(qtbot, tree, tmp_path, dialogs):
    write_tree(tmp_path / "spin_BT.json", [node_item("sp", "Spin", fields={"ticks": 100000})], [("root", "sp")])
    node = add_subtree(tree, "spin_BT.json")
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: node.GetStatus() is NodeStatus.RUNNING, timeout=3000)
        action = zoom_action(tree, node)
        assert action.isEnabled()
        action.trigger()
        assert dialogs.kinds() == []
        assert tree.GetZoomDepth() == 1 and tree.IsExecuting()
        assert tree.view().is_locked()
        shown_spin = by_id(tree.GetNodes(), "sp")
        qtbot.waitUntil(lambda: shown_spin.GetStatus() is NodeStatus.RUNNING, timeout=3000)
        with pytest.raises(RuntimeError):
            tree.AddNode("Mark", 600, 200)
        assert tree.GoBack() is True
        assert tree.GetZoomDepth() == 0 and tree.GetExecutionState() == "Running"
        assert tree.view() is tree.main_view() and tree.view().is_locked()
        tree.ZoomIn(node)  # and in again
        assert tree.GetZoomDepth() == 1
    finally:
        tree.Stop()
    assert not tree.view().is_locked()


# ============================================================================ what cannot be zoomed into
def test_zoom_in_refuses_other_nodes(tree, chain, tmp_path):
    with pytest.raises(ValueError):
        tree.ZoomIn(tree.GetRootNode())
    with pytest.raises(ValueError):
        tree.ZoomIn(add_mark(tree, "m", 400, 200))
    with pytest.raises(ValueError):
        tree.ZoomIn(add_subtree(tree, "", 600, 200, connect=False))  # no file
    loose = SubTreeNodeWidget()  # not part of the tree
    loose.SetFile("a_BT.json")
    try:
        with pytest.raises(ValueError):
            tree.ZoomIn(loose)
    finally:
        loose.deleteLater()
    tree.ZoomIn(chain)
    with pytest.raises(ValueError):
        tree.ZoomIn(chain)  # a node of the tree below, not of the tree shown
    assert tree.GetZoomDepth() == 1


def test_zoom_in_needs_a_loaded_tree(make_widget):
    widget = make_widget()
    node = SubTreeNodeWidget()
    node.SetFile("a_BT.json")
    try:
        with pytest.raises(RuntimeError):
            widget.ZoomIn(node)
    finally:
        node.deleteLater()


@pytest.mark.parametrize(
    "file, content",
    [
        ("missing_BT.json", None),
        ("broken_BT.json", "{oops"),
        ("other_BT.json", json.dumps({"format": "something else"})),
    ],
)
def test_zoom_in_refuses_missing_and_invalid_files(qtbot, tree, tmp_path, file, content):
    if content is not None:
        (tmp_path / file).write_text(content, encoding="utf-8")
    node = add_subtree(tree, file)
    views = len(live_tree_views(tree))
    zooms = []
    tree.zoomChanged.connect(lambda *args: zooms.append(args))
    with pytest.raises(TreeFileError):
        tree.ZoomIn(node)
    assert tree.GetZoomDepth() == 0 and zooms == []
    assert tree.view() is tree.main_view() and tree.main_view().isVisible()
    assert not tree.button("GoBack").isEnabled()
    qtbot.waitUntil(lambda: len(live_tree_views(tree)) == views, timeout=3000)
    if content is None:
        assert not os.path.exists(tmp_path / file)  # not created without create=True


def test_a_tree_cannot_be_zoomed_into_from_inside_itself(tree, chain, tmp_path):
    to_main = add_subtree(tree, "tree.json", 400.0)  # resolves to tree_BT.json, the main tree's file
    with pytest.raises(ValueError):
        tree.ZoomIn(to_main)
    tree.RemoveNode(to_main)
    tree.ZoomIn(chain)
    to_a = add_subtree(tree, "a_BT.json", 600.0)  # the file shown
    with pytest.raises(ValueError):
        tree.ZoomIn(to_a)
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))
    for name in ("a_BT.json", "tree_BT.json"):  # files of trees containing it
        node = add_subtree(tree, name, 600.0)
        with pytest.raises(ValueError):
            tree.ZoomIn(node)
    assert tree.GetZoomDepth() == 2
    # The file of a level that was left can be shown again.
    assert tree.GoBack(save=False)
    assert tree.GoBack(save=False)
    tree.ZoomIn(chain)
    assert tree.GetZoomDepth() == 1


def test_the_menu_reports_what_cannot_be_zoomed_into(tree, chain, tmp_path, dialogs):
    (tmp_path / "broken_BT.json").write_text("{oops", encoding="utf-8")
    broken = add_subtree(tree, "broken_BT.json", 400.0)
    zoom_action(tree, broken).trigger()
    assert dialogs.kinds() == ["warning"]
    assert "cannot be shown" in dialogs.shown[0][1][2]
    assert tree.GetZoomDepth() == 0
    tree.ZoomIn(chain)
    to_a = add_subtree(tree, "a_BT.json", 600.0)
    zoom_action(tree, to_a).trigger()
    assert dialogs.kinds() == ["warning", "warning"]
    assert tree.GetZoomDepth() == 1


# ============================================================================ creating a missing sub-tree file
def test_zoom_in_with_create_writes_a_new_sub_tree_file(tree, tmp_path):
    node = add_subtree(tree, "fresh")
    assert tree.SaveTree(tree.GetFilePath())
    with pytest.raises(TreeFileError):
        tree.ZoomIn(node)
    assert not os.path.exists(tmp_path / "fresh_BT.json")
    tree.ZoomIn(node, create=True)
    path = tmp_path / "fresh_BT.json"
    assert os.path.isfile(path)
    assert not os.path.exists(tmp_path / "fresh_BB.json")
    data = read_json(path)
    assert data["format"] == FORMAT_NAME
    assert [item["type"] for item in data["nodes"]] == ["Root"] and data["connections"] == []
    assert tree.GetZoomDepth() == 1 and same(tree.GetZoomPath()[-1], path)
    assert [type(n) for n in tree.GetNodes()] == [RootNodeWidget]
    assert not tree.IsModified()
    assert node.GetResolvedFile() is not None and same(node.GetResolvedFile(), path)
    # The new sub-tree can be edited and saved like any other.
    add_mark(tree, "new", 0, 200, tree.GetRootNode())
    assert tree.GoBack(save=True)
    assert [item["type"] for item in read_json(path)["nodes"]] == ["Root", "Mark"]


def test_create_keeps_an_existing_file(tree, tmp_path):
    write_tree(tmp_path / "kept_BT.json", [mark("k", "kept")], [("root", "k")])
    node = add_subtree(tree, "kept_BT.json")
    tree.ZoomIn(node, create=True)
    assert labels(tree) == ["kept"]


@pytest.mark.parametrize("answer, created", [(QMessageBox.StandardButton.Yes, True), (QMessageBox.StandardButton.No, False)])
def test_the_menu_offers_to_create_a_missing_file(tree, tmp_path, dialogs, answer, created):
    node = add_subtree(tree, "fresh")
    dialogs.question_answer = answer
    zoom_action(tree, node).trigger()
    questions = [args for kind, args in dialogs.shown if kind == "question"]
    assert len(questions) == 1
    assert "fresh_BT.json" in questions[0][2] and "Create it" in questions[0][2]
    assert os.path.exists(tmp_path / "fresh_BT.json") == created
    assert tree.GetZoomDepth() == int(created)
    assert "warning" not in dialogs.kinds()
    dialogs.question_answer = QMessageBox.StandardButton.Discard


# ============================================================================ New / Load while zoomed in
def test_load_tree_closes_the_sub_trees_shown(make_widget, tree, chain, tmp_path, dialogs):
    other = write_tree(tmp_path / "other_BT.json", [mark("o1", "o1")], [("root", "o1")])
    a_before, b_before = file_bytes(tmp_path / "a_BT.json"), file_bytes(tmp_path / "b_BT.json")
    tree.ZoomIn(chain)
    add_mark(tree, "unsaved-a", 600, 200, tree.GetRootNode())
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))
    add_mark(tree, "unsaved-b", 600, 200, tree.GetRootNode())
    zooms = []
    tree.zoomChanged.connect(lambda depth, path: zooms.append(depth))
    assert tree.LoadTree(other)
    assert dialogs.kinds() == []  # LoadTree(path) does not ask
    assert tree.GetZoomDepth() == 0 and zooms[-1] == 0
    assert not tree.button("GoBack").isEnabled()
    assert tree.view() is tree.main_view() and tree.main_view().isVisible()
    assert labels(tree) == ["o1"]
    assert title(tree) == "Behavior Tree - other_BT.json"
    assert not tree.IsModified()
    assert file_bytes(tmp_path / "a_BT.json") == a_before and file_bytes(tmp_path / "b_BT.json") == b_before


def test_new_tree_closes_the_sub_trees_shown(tree, chain, tmp_path):
    tree.ZoomIn(chain)
    add_mark(tree, "unsaved", 600, 200, tree.GetRootNode())
    assert tree.NewTree(str(tmp_path / "fresh_main"))
    assert tree.GetZoomDepth() == 0
    assert not tree.button("GoBack").isEnabled()
    assert [type(n) for n in tree.GetNodes()] == [RootNodeWidget]
    assert not tree.IsModified()
    assert title(tree) == "Behavior Tree - fresh_main_BT.json"


def test_interactive_load_asks_about_the_modified_sub_trees(tree, chain, tmp_path, dialogs):
    other = write_tree(tmp_path / "other_BT.json", [mark("o1", "o1")], [("root", "o1")])
    tree.ZoomIn(chain)
    add_mark(tree, "unsaved", 600, 200, tree.GetRootNode())
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))  # not modified
    dialogs.question_answer = QMessageBox.StandardButton.Cancel
    dialogs.open_path = other
    assert tree.LoadTree() is False
    text = [args for kind, args in dialogs.shown if kind == "question"][-1][2]
    assert "'a_BT.json'" in text and "b_BT.json" not in text and "tree_BT.json" not in text
    assert tree.GetZoomDepth() == 2 and tree.IsModified()
    # A change of the main tree is listed as well.
    config = tree.GetConfig()
    config.tick_interval_ms = 9
    tree.SetConfig(config)
    assert tree.LoadTree() is False
    text = [args for kind, args in dialogs.shown if kind == "question"][-1][2]
    assert "'tree_BT.json'" in text and "'a_BT.json'" in text
    dialogs.question_answer = QMessageBox.StandardButton.Discard
    a_before = file_bytes(tmp_path / "a_BT.json")
    assert tree.LoadTree() is True
    assert tree.GetZoomDepth() == 0 and labels(tree) == ["o1"]
    assert file_bytes(tmp_path / "a_BT.json") == a_before


def test_interactive_new_asks_about_the_modified_sub_trees(tree, chain, tmp_path, dialogs):
    tree.ZoomIn(chain)
    add_mark(tree, "unsaved", 600, 200, tree.GetRootNode())
    dialogs.question_answer = QMessageBox.StandardButton.Cancel
    assert tree.NewTree() is False
    assert "save_dialog" not in dialogs.kinds()
    assert tree.GetZoomDepth() == 1
    dialogs.question_answer = QMessageBox.StandardButton.Discard
    dialogs.save_path = str(tmp_path / "brand_new_BT.json")
    assert tree.NewTree() is True
    assert tree.GetZoomDepth() == 0
    assert same(tree.GetFilePath(), tmp_path / "brand_new_BT.json")


def test_confirm_discard_changes_saves_every_modified_sub_tree(tree, chain, tmp_path, dialogs):
    tree.ZoomIn(chain)
    a2 = add_mark(tree, "a2", 600, 200, tree.GetRootNode())
    tree.ZoomIn(by_id(tree.GetNodes(), "to_b"))
    b2 = add_mark(tree, "b2", 600, 200, tree.GetRootNode())
    config = tree.GetConfig()
    config.tick_interval_ms = 11
    tree.SetConfig(config)
    dialogs.question_answer = QMessageBox.StandardButton.Save
    assert tree.ConfirmDiscardChanges() is True
    assert "save_dialog" not in dialogs.kinds()
    assert any(item["id"] == a2.GetId() for item in read_json(tmp_path / "a_BT.json")["nodes"])
    assert any(item["id"] == b2.GetId() for item in read_json(tmp_path / "b_BT.json")["nodes"])
    assert read_json(tree.GetFilePath())["config"]["tick_interval_ms"] == 11
    assert not tree.IsModified()
    assert tree.GetZoomDepth() == 2  # nothing was closed
    dialogs.question_answer = QMessageBox.StandardButton.Discard


def test_confirm_discard_changes_without_changes_does_not_ask(tree, chain, dialogs):
    tree.ZoomIn(chain)
    assert tree.ConfirmDiscardChanges() is True
    assert dialogs.kinds() == []


# ============================================================================ sub-trees in other folders
def test_relative_file_names_resolve_against_the_folder_of_the_sub_tree_shown(qtbot, tree, tmp_path):
    lib = tmp_path / "lib"
    write_tree(lib / "leaf_BT.json", [mark("deep", "deep")], [("root", "deep")])
    write_tree(tmp_path / "leaf_BT.json", [mark("decoy", "decoy")], [("root", "decoy")])  # never used
    write_tree(lib / "a_BT.json", [sub("to_leaf", "leaf_BT.json"), mark("a1", "a1", 300.0)],
               [("root", "to_leaf"), ("root", "a1")])
    node = add_subtree(tree, os.path.join("lib", "a_BT.json"))
    tree.ZoomIn(node)
    assert same(tree.view().base_folder, lib)
    inner = by_id(tree.GetNodes(), "to_leaf")
    assert same(inner.GetResolvedFile(), lib / "leaf_BT.json")
    tree.ZoomIn(inner)
    assert labels(tree) == ["deep"]
    assert same(tree.GetZoomPath()[-1], lib / "leaf_BT.json")
    assert title(tree) == "Behavior Tree - tree_BT.json > a_BT.json > leaf_BT.json"
    assert tree.GoBack()
    # A new Sub-Tree node in the sub-tree: its new file is created next to the sub-tree's file.
    fresh = add_subtree(tree, "fresh", 600.0)
    assert same(fresh.GetResolvedFile(), lib / "fresh")
    tree.ZoomIn(fresh, create=True)
    assert os.path.isfile(lib / "fresh_BT.json")
    assert not os.path.exists(tmp_path / "fresh_BT.json")
    assert tree.GoBack(save=False) and tree.GoBack(save=False)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["deep", "a1"]


def test_a_shown_sub_tree_in_a_sub_folder_runs_as_shown(qtbot, tree, tmp_path):
    lib = tmp_path / "lib"
    write_tree(lib / "leaf_BT.json", [mark("deep", "deep")], [("root", "deep")])
    write_tree(lib / "a_BT.json", [sub("to_leaf", "leaf_BT.json")], [("root", "to_leaf")])
    node = add_subtree(tree, os.path.join("lib", "a_BT.json"))
    tree.ZoomIn(node)
    tree.ZoomIn(by_id(tree.GetNodes(), "to_leaf"))
    add_mark(tree, "deeper", 600, 200, tree.GetRootNode())  # unsaved, two levels down
    shown = list(tree.GetNodes())
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["deep", "deeper"]
    assert all(n.GetStatus() is NodeStatus.SUCCEEDED for n in shown)


# ============================================================================ promote / copy / paste inside a sub-tree
def test_promote_a_group_inside_a_sub_tree(tree, tmp_path):
    lib = tmp_path / "lib"
    write_tree(lib / "a_BT.json", [mark("a1", "a1")], [("root", "a1")])
    node = add_subtree(tree, os.path.join("lib", "a_BT.json"))
    assert tree.SaveTree(tree.GetFilePath())
    main_before = file_bytes(tree.GetFilePath())
    tree.ZoomIn(node)
    root = tree.GetRootNode()
    x = add_mark(tree, "x", 600, 500, root)
    y = add_mark(tree, "y", 900, 500, root)
    group = group_around(tree, [x, y], "grp")
    promoted = []
    tree.subTreePromoted.connect(lambda n, p: promoted.append((n, p)))
    subtree = tree.PromoteGroupToSubTree(group)
    assert len(promoted) == 1 and same(promoted[0][1], lib / "grp_BT.json")
    assert os.path.isfile(lib / "grp_BT.json")
    assert not os.path.exists(tmp_path / "grp_BT.json")
    assert not os.path.exists(lib / "grp_BB.json")
    assert subtree in tree.GetNodes() and subtree.GetParent() is root
    assert same(subtree.GetResolvedFile(), lib / "grp_BT.json")
    assert x not in tree.GetNodes() and group not in tree.GetNodes()
    assert tree.IsModified()
    assert file_bytes(tree.GetFilePath()) == main_before
    assert subtree not in tree.main_view().nodes()
    tree.ZoomIn(subtree)
    assert labels(tree) == ["x", "y"]
    assert tree.GetZoomDepth() == 2


@pytest.mark.parametrize("name", ["a", "tree"])
def test_promote_inside_a_sub_tree_refuses_the_names_of_open_files(tree, chain, tmp_path, name):
    tree.ZoomIn(chain)
    x = add_mark(tree, "x", 600, 500, tree.GetRootNode())
    group = group_around(tree, [x], name)
    before = file_bytes(tmp_path / f"{name}_BT.json")
    with pytest.raises(ValueError):
        tree.PromoteGroupToSubTree(group, overwrite=True)
    assert file_bytes(tmp_path / f"{name}_BT.json") == before
    assert x in tree.GetNodes() and group in tree.GetNodes()


def test_copy_from_the_main_tree_and_paste_into_a_sub_tree(tree, chain, tmp_path):
    copied = add_mark(tree, "copied", 600, 200, tree.GetRootNode())
    assert tree.SaveTree(tree.GetFilePath())
    main_count = len(tree.GetNodes())
    assert tree.CopyNodes([copied])
    tree.ZoomIn(chain)
    assert tree.CopyNodes([copied]) is False  # not a node of the tree shown
    pasted = tree.PasteNodes()
    assert len(pasted) == 1 and pasted[0] in tree.GetNodes()
    assert pasted[0].GetField("label") == "copied" and pasted[0] is not copied
    assert tree.GetSelectedNodes() == pasted
    assert len(tree.main_view().nodes()) == main_count
    assert tree.IsModified()
    assert tree.SaveSubTree()
    assert any(item["id"] == pasted[0].GetId() for item in read_json(tmp_path / "a_BT.json")["nodes"])
    assert not tree.IsModified()


def test_copy_inside_a_sub_tree_and_paste_into_the_main_tree(tree, chain):
    tree.ZoomIn(chain)
    a1 = by_id(tree.GetNodes(), "a1")
    assert tree.CopyNodes([a1])
    assert tree.GoBack()
    pasted = tree.PasteNodes()
    assert len(pasted) == 1 and pasted[0] in tree.main_view().nodes()
    assert pasted[0].GetField("label") == "a1"


# ============================================================================ more details
def test_discarded_changes_are_not_run(qtbot, tree, chain):
    tree.ZoomIn(chain)
    add_mark(tree, "discarded", 600, 200, tree.GetRootNode())
    assert tree.GoBack(save=False)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert Mark.order == ["c1", "b1", "a1"]


def test_the_browse_button_of_a_node_in_a_sub_tree_starts_in_its_folder(tree, tmp_path, dialogs):
    lib = tmp_path / "lib"
    write_tree(lib / "a_BT.json", [mark("a1", "a1")], [("root", "a1")])
    write_tree(lib / "other_BT.json", [mark("o1", "o1")], [("root", "o1")])
    node = add_subtree(tree, os.path.join("lib", "a_BT.json"))
    tree.ZoomIn(node)
    inner = add_subtree(tree, "", 600.0)
    dialogs.open_path = str(lib / "other_BT.json")
    assert inner.browse()
    start = [args for kind, args in dialogs.shown if kind == "open_dialog"][-1][2]
    assert same(start, lib)
    assert inner.GetFile() == "other_BT.json"  # relative to the sub-tree's folder
    assert same(inner.GetResolvedFile(), lib / "other_BT.json")
    assert same(inner.file_editor().toolTip(), lib / "other_BT.json")
    assert tree.IsModified()


def test_the_menu_reports_problems_of_the_file_shown(tree, tmp_path, dialogs):
    document = tree_document([mark("p1", "p1")], [("root", "p1"), ("root", "nowhere")])
    with open(tmp_path / "flawed_BT.json", "w", encoding="utf-8") as stream:
        json.dump(document, stream)
    node = add_subtree(tree, "flawed_BT.json")
    assert tree.SaveTree(tree.GetFilePath())
    zoom_action(tree, node).trigger()
    assert tree.GetZoomDepth() == 1 and labels(tree) == ["p1"]
    assert dialogs.kinds() == ["warning"]
    assert "was loaded with problems" in dialogs.shown[0][1][2]
    assert tree.GoBack()
    dialogs.shown.clear()
    tree.ZoomIn(node)  # the API does not ask or report
    assert dialogs.kinds() == []


def test_a_level_opened_while_paused_is_locked(qtbot, tree, tmp_path):
    write_tree(tmp_path / "spin_BT.json", [node_item("sp", "Spin", fields={"ticks": 100000})], [("root", "sp")])
    node = add_subtree(tree, "spin_BT.json")
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: node.GetStatus() is NodeStatus.RUNNING, timeout=3000)
        tree.Pause()
        assert tree.GetExecutionState() == "Paused"
        tree.ZoomIn(node)
        assert tree.view().is_locked()
        with pytest.raises(RuntimeError):
            tree.PasteNodes()
        assert tree.GoBack()
    finally:
        tree.Stop()


# ============================================================================ suspected bugs
def test_save_tree_refuses_the_file_of_a_sub_tree_shown(tree, chain, tmp_path):
    path = tmp_path / "a_BT.json"
    before = file_bytes(path)
    tree.ZoomIn(chain)
    try:
        tree.SaveTree(str(path))
    except (ValueError, RuntimeError, OSError):
        pass
    keys = [os.path.normcase(os.path.abspath(p)) for p in tree.GetZoomPath()]
    assert len(set(keys)) == len(keys), keys  # ZoomIn never allows a file twice in the stack
    assert file_bytes(path) == before


def test_saving_a_legacy_sub_tree_file_keeps_its_blackboard(tree, tmp_path):
    blackboard = [{"name": "count", "type": "Integer", "value": 7}]
    document = tree_document([mark("o1", "o1")], [("root", "o1")])
    document["blackboard"] = blackboard
    with open(tmp_path / "old.json", "w", encoding="utf-8") as stream:
        json.dump(document, stream)
    node = add_subtree(tree, "old.json")
    tree.ZoomIn(node)
    by_id(tree.GetNodes(), "o1").SetTitle("renamed")
    assert tree.SaveSubTree()
    kept = read_json(tmp_path / "old.json").get("blackboard")
    if kept is None and os.path.exists(tmp_path / "old_BB.json"):
        kept = read_json(tmp_path / "old_BB.json").get("blackboard")
    assert kept == blackboard

