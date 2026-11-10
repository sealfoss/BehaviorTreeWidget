"""The Group node: a named, resizable box whose contents move with it.

Requirements: a simple box, resizable by grabbing its sides like a note (comment); a line
edit at the top left holds the group name, by default f"group_{num:03d}" with num counted
by the BehaviorTreeWidget; widgets lying inside the group (notes included) move with it;
the right-click menu has a border colour option (and Promote to Sub-Tree, see
test_group_promote.py).
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QColor, QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMenu

from behavior_tree_widget import BehaviorTreeWidget, GroupNodeWidget, LeafNodeWidget
from behavior_tree_widget.canvas import GROUP_Z_BASE, NOTE_Z_BASE, Hit
from behavior_tree_widget.groups import DEFAULT_GROUP_COLOR, GROUP, GROUP_COLORS, default_group_name
from behavior_tree_widget.notes import BOTTOM, LEFT, RIGHT, TOP

from conftest import drag, fast_config, viewport_point

LEFT_BUTTON = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier


# ============================================================================ helpers
def add_group(bt: BehaviorTreeWidget, x: float = -300.0, y: float = 150.0, size=(600, 400)) -> GroupNodeWidget:
    group = bt.AddNode(GROUP, x, y)
    group.SetGroupSize(*size)
    QApplication.processEvents()
    return group


def point_in(bt, node, x: float, y: float) -> QPoint:
    """Viewport position of (x, y) in node coordinates."""
    return bt.view().mapFromScene(node._item.mapToScene(QPointF(x, y)))


def header_point(bt, group: GroupNodeWidget) -> QPoint:
    """A point of the title bar right of the name editor (away from the handles)."""
    rect = group.header_rect()
    return point_in(bt, group, rect.right() - 16, rect.center().y())


def interior_point(bt, group: GroupNodeWidget) -> QPoint:
    return point_in(bt, group, group.width() * 0.8, group.height() * 0.85)


def handle_point(bt, group: GroupNodeWidget, edges: int) -> QPoint:
    handle = next(h for h in group.resize_handles() if h.edges == edges)
    return viewport_point(bt, group, handle)


def scene_pos(node) -> tuple[float, float]:
    pos = node._item.pos()
    return pos.x(), pos.y()


def context_menu(bt, dialogs, point: QPoint):
    before = len(dialogs.menus)
    viewport = bt.view().viewport()
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point)))
    assert len(dialogs.menus) == before + 1
    return dialogs.menus[-1]


def action(menu, name: str):
    """The action named ``name`` in ``menu`` or one of its submenus.

    Found with findChild, not through QAction.menu(): before PySide6 6.11, the wrapper that
    QAction.menu() returns deletes the submenu when it is garbage collected.
    """
    found = menu.findChild(QAction, name)
    if found is None:
        raise AssertionError(f"menu has no action {name!r}")
    return found


def saved_nodes(path) -> list[dict]:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)["nodes"]


# ============================================================================ adding groups and their names
def test_group_is_offered_after_notes(bt):
    assert bt.GetNodeTypes()[-2:] == ["Notes", GROUP]
    assert bt._node_menu_entries()[-2:] == [("notes", "Notes", "Notes"), ("notes", GROUP, GROUP)]
    assert bt.GetNodeType(GROUP) is None


def test_choosing_group_in_the_menu_adds_one_at_the_click(bt, dialogs):
    point = QPoint(400, 600)
    assert bt.view().hit_test(point).kind == Hit.EMPTY
    dialogs.menu_handler = lambda menu: action(menu, "Add_Group").trigger()
    context_menu(bt, dialogs, point)
    group = next(node for node in bt.GetNodes() if isinstance(node, GroupNodeWidget))
    assert group._item.pos() == bt.view().mapToScene(point)
    assert group.GetTitle() == "group_001"
    assert group.GetGroupSize() == GroupNodeWidget.DEFAULT_SIZE
    assert group.GetBorderColor() == DEFAULT_GROUP_COLOR
    assert bt.IsModified()


def test_default_names_count_up_with_two_padding_zeros(bt):
    names = [bt.AddNode(GROUP, 0, 600 + 300 * index).GetTitle() for index in range(3)]
    assert names == ["group_001", "group_002", "group_003"]
    assert default_group_name(12) == "group_012" and default_group_name(100) == "group_100"


def test_the_counter_belongs_to_the_widget(make_widget, bt):
    assert bt.AddNode(GROUP, 0, 600).GetTitle() == "group_001"
    other = make_widget()
    assert other.AddNode(GROUP, 0, 600).GetTitle() == "group_001"
    assert bt.AddNode(GROUP, 0, 900).GetTitle() == "group_002"


def test_default_names_skip_names_in_use_and_keep_counting(bt):
    first = bt.AddNode(GROUP, 0, 600)
    second = bt.AddNode(GROUP, 0, 900)
    second.SetTitle("group_007")
    assert bt.AddNode(GROUP, 0, 1200).GetTitle() == "group_008"
    first.SetTitle("patrol")
    bt.RemoveNode(second)
    assert bt.AddNode(GROUP, 0, 1500).GetTitle() == "group_009"  # an incrementing number


def test_new_and_load_count_from_the_groups_of_the_tree(bt, make_widget, tmp_path):
    bt.AddNode(GROUP, 0, 600)
    bt.AddNode(GROUP, 0, 900).SetTitle("group_005")
    assert bt.SaveTree(str(tmp_path / "groups"))
    other = make_widget()
    assert other.LoadTree(str(tmp_path / "groups_BT.json"))
    assert other.AddNode(GROUP, 0, 1200).GetTitle() == "group_006"
    assert other.NewTree(str(tmp_path / "fresh"))
    assert other.AddNode(GROUP, 0, 600).GetTitle() == "group_001"


def test_after_load_the_counter_starts_after_the_groups_of_the_tree_even_once_they_are_deleted(bt, tmp_path):
    for index in range(5):
        bt.AddNode(GROUP, 0, 600 + 300 * index)
    assert bt.SaveTree(str(tmp_path / "groups"))
    assert bt.LoadTree(str(tmp_path / "groups_BT.json"))
    for group in [node for node in bt.GetNodes() if isinstance(node, GroupNodeWidget)]:
        bt.RemoveNode(group)
    assert bt.AddNode(GROUP, 0, 600).GetTitle() == "group_006"  # a group name becomes a file name on Promote


def test_a_pasted_copy_gets_the_next_default_name(bt):
    group = bt.AddNode(GROUP, 0, 600)
    named = bt.AddNode(GROUP, 0, 900)
    named.SetTitle("patrol")
    assert bt.CopyNodes([group, named])
    pasted = bt.PasteNodes()
    titles = sorted(node.GetTitle() for node in pasted)
    assert titles == ["group_003", "patrol"]  # the counter only counts up; a name of its own is kept


# ============================================================================ the name editor
def test_the_name_is_edited_in_a_line_edit_at_the_top_left(bt):
    group = add_group(bt)
    editor = group.name_editor()
    assert isinstance(editor, QLineEdit) and editor.objectName() == "GroupName"
    assert editor.text() == "group_001"
    top_left = editor.mapTo(group, QPoint(0, 0))
    assert top_left.x() < 20 and top_left.y() < 20
    assert group.header_rect().contains(QPointF(top_left) + QPointF(2, 2))


def test_typing_a_name_renames_the_group(qtbot, bt):
    group = add_group(bt)
    bt._set_modified(False)
    editor = group.name_editor()
    QTest.mouseClick(bt.view().viewport(), LEFT_BUTTON, NO_MOD, viewport_point(bt, group, editor))
    editor.selectAll()
    QTest.keyClicks(editor, "patrol")
    assert group.GetTitle() == "patrol"
    assert bt.IsModified()
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert not editor.hasFocus()
    assert bt.view().hasFocus()


def test_escape_restores_the_name(qtbot, bt):
    group = add_group(bt)
    editor = group.name_editor()
    QTest.mouseClick(bt.view().viewport(), LEFT_BUTTON, NO_MOD, viewport_point(bt, group, editor))
    editor.selectAll()
    QTest.keyClicks(editor, "typo")
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    assert group.GetTitle() == "group_001"
    assert editor.text() == "group_001"


def test_an_empty_name_is_not_kept(qtbot, bt):
    group = add_group(bt)
    editor = group.name_editor()
    QTest.mouseClick(bt.view().viewport(), LEFT_BUTTON, NO_MOD, viewport_point(bt, group, editor))
    editor.selectAll()
    QTest.keyClick(editor, Qt.Key.Key_Delete)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert group.GetTitle() == "group_001"
    assert editor.text() == "group_001"


def test_set_title_shows_in_the_editor(bt):
    group = add_group(bt)
    group.SetTitle("navigation")
    assert group.name_editor().text() == "navigation"
    group.SetTitle("a much longer group name than before")
    assert group.name_editor().width() > group.name_editor().fontMetrics().horizontalAdvance("navigation")


def test_rename_in_the_menu(bt, dialogs):
    group = add_group(bt)
    dialogs.text_answer = ("renamed", True)
    dialogs.menu_handler = lambda menu: action(menu, "Rename").trigger()
    context_menu(bt, dialogs, header_point(bt, group))
    assert group.GetTitle() == "renamed" and group.name_editor().text() == "renamed"


def test_the_name_editor_leaves_room_to_grab_the_title_bar(bt):
    group = add_group(bt, size=(GroupNodeWidget.MIN_SIZE[0], 200))
    group.SetTitle("x" * 200)
    editor = group.name_editor()
    assert editor.geometry().right() <= group.width() - GroupNodeWidget.NAME_GRAB_ROOM
    assert bt.view().hit_test(header_point(bt, group)).kind == Hit.NODE


# ============================================================================ clicks on a group
def test_the_title_bar_selects_the_group(bt):
    group = add_group(bt)
    hit = bt.view().hit_test(header_point(bt, group))
    assert hit.kind == Hit.NODE and hit.node is group
    QTest.mouseClick(bt.view().viewport(), LEFT_BUTTON, NO_MOD, header_point(bt, group))
    assert bt.GetSelectedNodes() == [group]


def test_the_inside_of_a_group_lets_clicks_through(bt):
    group = add_group(bt)
    point = interior_point(bt, group)
    assert bt.view().hit_test(point).kind == Hit.EMPTY
    leaf = bt.AddNode("Succeed", 0, 300)
    QApplication.processEvents()
    hit = bt.view().hit_test(viewport_point(bt, leaf, leaf._title_label))
    assert hit.node is leaf  # a node inside the group gets its clicks


def test_a_rectangle_inside_the_group_selects_its_nodes_not_the_group(bt):
    group = add_group(bt, -300, 150, (700, 450))
    leaf = bt.AddNode("Succeed", -100, 300)
    QApplication.processEvents()
    start = point_in(bt, group, 120, 90)
    end = viewport_point(bt, leaf) + QPoint(20, 20)
    drag(bt, start, end)
    assert bt.GetSelectedNodes() == [leaf]


def test_a_rectangle_over_the_title_bar_selects_the_group(bt):
    group = add_group(bt)
    start = header_point(bt, group) + QPoint(-10, -30)
    drag(bt, start, header_point(bt, group) + QPoint(5, 0))
    assert bt.GetSelectedNodes() == [group]


def test_right_click_inside_opens_the_add_menu(bt, dialogs):
    group = add_group(bt)
    menu = context_menu(bt, dialogs, interior_point(bt, group))
    assert menu.objectName() == "AddNodeMenu"


def test_the_group_menu(bt, dialogs):
    group = add_group(bt)
    menu = context_menu(bt, dialogs, header_point(bt, group))
    names = [a.objectName() for a in menu.actions() if a.objectName()]
    assert names == ["Rename", "SelectContents", "PromoteToSubTree", "Delete"]
    submenu = menu.findChild(QMenu, "BorderColor")
    assert [a.text() for a in menu.actions() if a is submenu.menuAction()] == ["Border Color"]
    assert [a.text() for a in submenu.actions() if a.text()] == [*GROUP_COLORS, "Custom..."]
    assert action(menu, "Color_Blue").isChecked()


# ============================================================================ resizing
@pytest.mark.parametrize(
    "edges, delta, expected",
    [
        (RIGHT, (60, 0), (0, 0, 60, 0)),
        (BOTTOM, (0, 50), (0, 0, 0, 50)),
        (LEFT, (-40, 0), (-40, 0, 40, 0)),
        (TOP, (0, -30), (0, -30, 0, 30)),
        (BOTTOM | RIGHT, (70, 30), (0, 0, 70, 30)),
        (TOP | LEFT, (40, 20), (40, 20, -40, -20)),
    ],
)
def test_dragging_an_edge_or_corner_resizes_the_group(bt, edges, delta, expected):
    group = add_group(bt)
    leaf = bt.AddNode("Succeed", -100, 300)
    QApplication.processEvents()
    leaf_pos = scene_pos(leaf)
    rect = group._item.sceneBoundingRect()
    bt._set_modified(False)
    start = handle_point(bt, group, edges)
    drag(bt, start, start + QPoint(*delta))
    dx, dy, dwidth, dheight = expected
    after = group._item.sceneBoundingRect()
    assert (after.left(), after.top(), after.width(), after.height()) == (
        rect.left() + dx, rect.top() + dy, rect.width() + dwidth, rect.height() + dheight
    )
    assert group.GetGroupSize() == (rect.width() + dwidth, rect.height() + dheight)
    assert scene_pos(leaf) == leaf_pos  # resizing never moves the contents
    assert bt.IsModified()


def test_group_size_limits(bt):
    group = add_group(bt)
    group.SetGroupSize(1, 1)
    assert group.GetGroupSize() == GroupNodeWidget.MIN_SIZE
    group.SetGroupSize(10_000, 10_000)
    assert group.GetGroupSize() == (GroupNodeWidget.MAX_SIZE, GroupNodeWidget.MAX_SIZE)
    with pytest.raises(TypeError):
        group.SetGroupSize("1", 2)
    with pytest.raises(ValueError):
        group.SetGroupSize(float("nan"), 2)


# ============================================================================ moving
def test_dragging_the_title_bar_moves_everything_inside(bt):
    group = add_group(bt, -300, 150, (700, 500))
    sequence = bt.AddNode("Sequence", -50, 230)
    leaf = bt.AddNode("Succeed", -150, 400)
    note = bt.AddNode("Notes", 150, 380)
    inner = bt.AddNode(GROUP, 150, 220)
    inner.SetGroupSize(150, 100)
    outside = bt.AddNode("Succeed", 600, 300)
    partly = bt.AddNode("Succeed", 340, 560)  # sticks out of the bottom right of the group
    bt.Connect(bt.GetRootNode(), sequence)
    bt.Connect(sequence, leaf)
    QApplication.processEvents()
    assert set(group.GetContainedNodes()) == {sequence, leaf, note, inner}
    before = {node: scene_pos(node) for node in bt.GetNodes()}
    line = bt.view().connection_item(leaf).path().pointAtPercent(1.0)
    bt._set_modified(False)
    start = header_point(bt, group)
    drag(bt, start, start + QPoint(80, 40))
    for node in (group, sequence, leaf, note, inner):
        assert scene_pos(node) == (before[node][0] + 80, before[node][1] + 40)
    for node in (outside, partly, bt.GetRootNode()):
        assert scene_pos(node) == before[node]
    assert bt.view().connection_item(leaf).path().pointAtPercent(1.0) == line + QPointF(80, 40)
    assert bt.IsModified()
    assert bt.GetSelectedNodes() == [group]  # only the group is selected


def test_moving_a_selected_group_with_other_nodes(bt):
    group = add_group(bt, -300, 150, (400, 300))
    inside = bt.AddNode("Succeed", -250, 250)
    other = bt.AddNode("Succeed", 400, 250)
    QApplication.processEvents()
    bt.SelectNodes([group, other])
    before = {node: scene_pos(node) for node in (group, inside, other)}
    start = viewport_point(bt, other, other._title_label)
    drag(bt, start, start + QPoint(-30, 60))
    for node in (group, inside, other):
        assert scene_pos(node) == (before[node][0] - 30, before[node][1] + 60)


def test_moving_a_node_inside_the_group_moves_only_that_node(bt):
    group = add_group(bt)
    leaf = bt.AddNode("Succeed", -100, 300)
    QApplication.processEvents()
    before = scene_pos(group)
    start = viewport_point(bt, leaf, leaf._title_label)
    drag(bt, start, start + QPoint(30, 30))
    assert scene_pos(group) == before
    assert scene_pos(leaf) == (-70.0, 330.0)


def test_a_node_moved_into_the_group_moves_with_it(bt):
    group = add_group(bt, -300, 150, (500, 400))
    leaf = bt.AddNode("Succeed", 400, 250)
    QApplication.processEvents()
    assert leaf not in group.GetContainedNodes()
    start = viewport_point(bt, leaf, leaf._title_label)
    drag(bt, start, start + QPoint(-550, 0))
    assert leaf in group.GetContainedNodes()
    before = scene_pos(leaf)
    start = header_point(bt, group)
    drag(bt, start, start + QPoint(0, 50))
    assert scene_pos(leaf) == (before[0], before[1] + 50)


def test_groups_stay_below_notes_nodes_and_lines(bt):
    group = add_group(bt)
    note = bt.AddNode("Notes", -200, 300)
    leaf = bt.AddNode("Succeed", 0, 300)
    bt.Connect(bt.GetRootNode(), leaf)
    QApplication.processEvents()
    start = header_point(bt, group)
    drag(bt, start, start + QPoint(5, 5))  # moving (raising) the group keeps it below
    line = bt.view().connection_item(leaf)
    assert GROUP_Z_BASE < group._item.zValue() < NOTE_Z_BASE < note._item.zValue() < line.zValue() < leaf._item.zValue()


def test_groups_can_be_moved_resized_and_renamed_while_executing(qtbot, bt, dialogs):
    group = add_group(bt)
    leaf = bt.AddNode("SlowCancelable", -100, 300)
    bt.Connect(bt.GetRootNode(), leaf)
    fast_config(bt)
    bt.Execute()
    try:
        before = scene_pos(leaf)
        start = header_point(bt, group)
        drag(bt, start, start + QPoint(20, 0))
        assert scene_pos(leaf) == (before[0] + 20, before[1])
        start = handle_point(bt, group, RIGHT)
        drag(bt, start, start + QPoint(30, 0))
        assert group.GetGroupSize()[0] == 630
        group.SetTitle("running")
        dialogs.menu_handler = lambda menu: action(menu, "Color_Red").trigger()
        context_menu(bt, dialogs, header_point(bt, group))
        assert group.GetBorderColor() == GROUP_COLORS["Red"]
        menu = context_menu(bt, dialogs, header_point(bt, group))
        assert not action(menu, "PromoteToSubTree").isEnabled()
        assert not action(menu, "Delete").isEnabled()
    finally:
        bt.Stop()


# ============================================================================ colour
@pytest.mark.parametrize("name", list(GROUP_COLORS))
def test_choosing_a_border_color(bt, dialogs, name):
    group = add_group(bt)
    bt._set_modified(False)
    changes = []
    group.colorChanged.connect(changes.append)
    dialogs.menu_handler = lambda menu: action(menu, f"Color_{name}").trigger()
    context_menu(bt, dialogs, header_point(bt, group))
    assert group.GetBorderColor() == GROUP_COLORS[name]
    assert (changes == [GROUP_COLORS[name]]) == (name != "Blue")
    assert bt.IsModified() == (name != "Blue")
    assert GROUP_COLORS[name] in group._ui.styleSheet()


def test_a_custom_border_color(bt, dialogs, monkeypatch):
    group = add_group(bt)
    asked = []

    def choose(view, initial):
        asked.append(initial.name())
        return QColor("#123456")

    monkeypatch.setattr(type(bt.view()), "_choose_color", choose)
    dialogs.menu_handler = lambda menu: action(menu, "Color_Custom").trigger()
    context_menu(bt, dialogs, header_point(bt, group))
    assert asked == [DEFAULT_GROUP_COLOR]
    assert group.GetBorderColor() == "#123456"
    menu = context_menu(bt, dialogs, header_point(bt, group))
    assert action(menu, "Color_Custom").isChecked()


def test_cancelling_the_custom_color_changes_nothing(bt, dialogs, monkeypatch):
    group = add_group(bt)
    monkeypatch.setattr(type(bt.view()), "_choose_color", lambda view, initial: None)
    dialogs.menu_handler = lambda menu: action(menu, "Color_Custom").trigger()
    context_menu(bt, dialogs, header_point(bt, group))
    assert group.GetBorderColor() == DEFAULT_GROUP_COLOR


def test_set_border_color_from_code(qtbot, bt):
    group = add_group(bt)
    group.SetBorderColor("red")
    assert group.GetBorderColor() == "#ff0000"
    group.SetBorderColor(QColor(10, 20, 30, 40))
    assert group.GetBorderColor() == "#0a141e"
    with pytest.raises(ValueError):
        group.SetBorderColor("not a color")
    with pytest.raises(TypeError):
        group.SetBorderColor(42)


# ============================================================================ selection, deletion, copy
def test_select_contents(bt, dialogs):
    group = add_group(bt)
    leaf = bt.AddNode("Succeed", -100, 300)
    note = bt.AddNode("Notes", 50, 300)
    bt.AddNode("Succeed", 600, 300)
    QApplication.processEvents()
    dialogs.menu_handler = lambda menu: action(menu, "SelectContents").trigger()
    context_menu(bt, dialogs, header_point(bt, group))
    assert set(bt.GetSelectedNodes()) == {group, leaf, note}


def test_delete_removes_only_the_group(bt):
    group = add_group(bt)
    leaf = bt.AddNode("Succeed", -100, 300)
    QApplication.processEvents()
    bt.SelectNodes([group])
    bt.view().setFocus()
    QTest.keyClick(bt.view().viewport(), Qt.Key.Key_Delete)
    assert group not in bt.GetNodes() and leaf in bt.GetNodes()


def test_delete_after_editing_the_name_deletes_the_group(bt):
    group = add_group(bt)
    editor = group.name_editor()
    QTest.mouseClick(bt.view().viewport(), LEFT_BUTTON, NO_MOD, viewport_point(bt, group, editor))
    QTest.keyClick(editor, Qt.Key.Key_Return)
    bt.SelectNodes([group])
    QTest.keyClick(bt.view().viewport(), Qt.Key.Key_Delete)
    assert group not in bt.GetNodes()


def test_copy_and_paste_a_group_with_its_contents(bt):
    group = add_group(bt)
    group.SetBorderColor("#2e9d4f")
    sequence = bt.AddNode("Sequence", -50, 230)
    leaf = bt.AddNode("Succeed", -100, 400)
    bt.Connect(sequence, leaf)
    QApplication.processEvents()
    bt.SelectNodes([group, *group.GetContainedNodes()])
    assert bt.CopyNodes()
    pasted = bt.PasteNodes()
    copy = next(node for node in pasted if isinstance(node, GroupNodeWidget))
    assert copy.GetBorderColor() == "#2e9d4f" and copy.GetGroupSize() == (600, 400)
    assert len(copy.GetContainedNodes()) == 2  # the copies lie inside the copy


# ============================================================================ saving
def test_groups_are_saved_and_loaded(make_widget, bt, tmp_path):
    group = add_group(bt, -320, 140, (512, 333))
    group.SetTitle("navigation")
    group.SetBorderColor("#8a5cc2")
    assert bt.SaveTree(str(tmp_path / "groups"))
    item = next(node for node in saved_nodes(tmp_path / "groups_BT.json") if node["type"] == GROUP)
    assert item["title"] == "navigation"
    assert (item["x"], item["y"], item["width"], item["height"], item["color"]) == (-320.0, 140.0, 512, 333, "#8a5cc2")
    assert "fields" not in item
    other = make_widget()
    assert other.LoadTree(str(tmp_path / "groups_BT.json"))
    loaded = next(node for node in other.GetNodes() if isinstance(node, GroupNodeWidget))
    assert loaded.GetTitle() == "navigation" and loaded.name_editor().text() == "navigation"
    assert loaded.GetGroupSize() == (512, 333) and loaded.GetBorderColor() == "#8a5cc2"
    assert scene_pos(loaded) == (-320.0, 140.0)


def test_invalid_saved_group_data_keeps_the_defaults(make_widget, tmp_path):
    data = {
        "format": "behavior_tree_widget",
        "version": 1,
        "nodes": [
            {"id": "r", "type": "Root", "title": "Root", "x": 0, "y": 0},
            {"id": "g", "type": GROUP, "title": "g", "x": 0, "y": 200, "width": "wide", "height": 300, "color": "nope"},
        ],
    }
    (tmp_path / "bad_BT.json").write_text(json.dumps(data), encoding="utf-8")
    widget = make_widget()
    assert widget.LoadTree(str(tmp_path / "bad_BT.json"))
    group = next(node for node in widget.GetNodes() if isinstance(node, GroupNodeWidget))
    assert group.GetGroupSize() == (GroupNodeWidget.DEFAULT_SIZE[0], 300)
    assert group.GetBorderColor() == DEFAULT_GROUP_COLOR


def test_groups_are_never_executed(qtbot, bt):
    add_group(bt)
    leaf = bt.AddNode("Succeed", -100, 300)
    bt.Connect(bt.GetRootNode(), leaf)
    fast_config(bt)
    results = []
    bt.executionFinished.connect(results.append)
    bt.Execute()
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert results == ["Succeeded"]
    assert not GroupNodeWidget.HasParentConnection() and not GroupNodeWidget.HasChildConnections()


class Group(LeafNodeWidget):
    """An application's own leaf type named like the built-in Group."""

    _fields = {"members": 3}

    def OnRun(self, tree):
        return True


def test_an_application_type_named_group_takes_the_name(make_widget, tmp_path):
    widget = make_widget(node_types=[Group])
    assert GROUP not in [entry[1] for entry in widget._node_menu_entries() if entry[0] == "notes"]
    node = widget.AddNode(GROUP, 0, 0)
    assert type(node) is Group
    with pytest.raises(ValueError):
        widget.AddNode(GroupNodeWidget(), 0, 0)
    assert widget.NewTree(str(tmp_path / "app"))
    widget.AddNode(GROUP, 0, 200).SetField("members", 5)
    assert widget.SaveTree(widget.GetFilePath())
    plain = make_widget(node_types=[])
    assert plain.LoadTree(widget.GetFilePath())
    placeholder = next(node for node in plain.GetNodes() if node.GetTypeName() == GROUP)
    assert not isinstance(placeholder, GroupNodeWidget)  # its data is not read as a built-in group
    assert placeholder.GetField("members") == 5


# ============================================================================ Promote to Sub-Tree
def test_a_pasted_default_named_group_always_gets_the_next_number(bt):
    group = bt.AddNode("Group", 0, 600)
    assert bt.CopyNodes([group])
    bt.RemoveNode(group)
    pasted = bt.PasteNodes()
    assert [node.GetTitle() for node in pasted if isinstance(node, GroupNodeWidget)] == ["group_002"]
