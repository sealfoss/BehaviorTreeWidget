"""Tests of the add-node menu opened by releasing a connection drag over the graph.

Requirement (feature request 2): if the user drags a line off a node's Children or
Parent label and releases the mouse before making a connection, the new-node popup
appears as if the user had right-clicked the graph there.

The node chosen from that menu is added at the release point and connected to the label
the line came from (when that connection is possible).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from behavior_tree_widget import CompositeNodeWidget, NegationNodeWidget
from behavior_tree_widget.canvas import DragLineItem, Hit, anchor_point
from behavior_tree_widget.nodes import CHILDREN, PARENT, ConnectionState, NodeStatus

from conftest import drag, fast_config, label_point, viewport_point

LEFT = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier


# ============================================================================ helpers
def press(bt, point: QPoint) -> None:
    QTest.mousePress(bt.view().viewport(), LEFT, NO_MOD, point)


def move(bt, point: QPoint) -> None:
    QTest.mouseMove(bt.view().viewport(), point)


def release(bt, point: QPoint) -> None:
    QTest.mouseRelease(bt.view().viewport(), LEFT, NO_MOD, point)


def drag_line_items(bt) -> list:
    return [item for item in bt.view().scene().items() if isinstance(item, DragLineItem)]


def action(menu, name: str):
    for candidate in menu.actions():
        if candidate.objectName() == name:
            return candidate
    raise AssertionError(f"menu has no action {name!r}")


def menu_signature(menu) -> list[tuple[str, str, bool]]:
    return [(a.objectName(), a.text(), a.isEnabled()) for a in menu.actions() if not a.isSeparator()]


def right_click_menu(bt, dialogs, point: QPoint):
    before = len(dialogs.menus)
    viewport = bt.view().viewport()
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point)))
    assert len(dialogs.menus) == before + 1
    return dialogs.menus[-1]


def drag_off(bt, dialogs, start: QPoint, end: QPoint, choose: str | None = None):
    """Drag a line from ``start`` and release it at ``end``; pick ``choose`` in the menu shown.

    Returns the menu shown (or None) and the node added (or None).
    """
    before = set(bt.GetNodes())
    shown = len(dialogs.menus)

    def pick(menu):
        if choose is not None:
            action(menu, f"Add_{choose}").trigger()

    dialogs.menu_handler = pick
    drag(bt, start, end)
    dialogs.menu_handler = None
    added = set(bt.GetNodes()) - before
    assert len(added) <= 1
    menu = dialogs.menus[-1] if len(dialogs.menus) > shown else None
    return menu, (added.pop() if added else None)


def close(a: QPointF, b: QPointF, tolerance: float = 0.51) -> bool:
    return abs(a.x() - b.x()) <= tolerance and abs(a.y() - b.y()) <= tolerance


@pytest.fixture
def scene(bt):
    """Root at (0, 0), an unconnected leaf at (0, 220); ``empty`` is a free viewport point below them."""
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", 0, 220)
    view = bt.view()
    view.centerOn(150, 250)
    empty = view.mapFromScene(QPointF(330.0, 380.0))
    assert view.hit_test(empty).kind == Hit.EMPTY
    return SimpleNamespace(root=root, leaf=leaf, view=view, empty=empty)


# ============================================================================ the menu
@pytest.mark.parametrize("source", ["children", "parent"])
def test_releasing_over_empty_space_opens_the_add_node_menu(bt, dialogs, scene, source):
    start = label_point(bt, scene.root, CHILDREN) if source == "children" else label_point(bt, scene.leaf, PARENT)
    menu, added = drag_off(bt, dialogs, start, scene.empty)
    assert menu is not None and menu.objectName() == "AddNodeMenu"
    assert added is None  # nothing chosen
    # the same menu as a right click on the graph at that point
    assert menu_signature(menu) == menu_signature(right_click_menu(bt, dialogs, scene.empty))
    assert "Add_Negation" in [a.objectName() for a in menu.actions()]


@pytest.mark.parametrize("type_name", ["Sequence", "Selector", "Negation", "Succeed", "AllFields", "Evaluation", "Set"])
def test_the_chosen_node_is_added_at_the_release_point_as_a_child(bt, dialogs, scene, type_name):
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.root, CHILDREN), scene.empty, choose=type_name)
    assert node is not None and node.GetTypeName() == type_name
    assert node.GetParent() is scene.root
    assert scene.root.GetChildren() == [node]
    release_point = scene.view.mapToScene(scene.empty)
    assert close(anchor_point(node, PARENT), release_point)  # its Parent label sits where the line ended
    assert node.connection_state(PARENT) is ConnectionState.CONNECTED
    assert scene.root.connection_state(CHILDREN) is ConnectionState.CONNECTED
    assert scene.view.connection_item(node) is not None
    assert bt.IsModified()


@pytest.mark.parametrize("type_name", ["Sequence", "Selector", "Negation"])
def test_dragging_from_a_parent_label_adds_a_connected_parent(bt, dialogs, scene, type_name):
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.leaf, PARENT), scene.empty, choose=type_name)
    assert node.GetTypeName() == type_name
    assert scene.leaf.GetParent() is node
    assert node.GetChildren() == [scene.leaf]
    assert close(anchor_point(node, CHILDREN), scene.view.mapToScene(scene.empty))


@pytest.mark.parametrize("type_name", ["Succeed", "Evaluation"])
def test_a_leaf_chosen_from_a_parent_drag_is_added_unconnected(bt, dialogs, scene, type_name):
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.leaf, PARENT), scene.empty, choose=type_name)
    assert node.GetTypeName() == type_name
    assert node.GetParent() is None and scene.leaf.GetParent() is None
    # placed like a node added by a right click: top-left corner at the point
    assert close(node._item.pos(), scene.view.mapToScene(scene.empty), 0.01)


def test_a_new_parent_replaces_the_old_one(bt, dialogs, scene):
    bt.Connect(scene.root, scene.leaf)
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.leaf, PARENT), scene.empty, choose="Selector")
    assert scene.leaf.GetParent() is node
    assert scene.root.GetChildren() == []
    assert scene.root.connection_state(CHILDREN) is ConnectionState.DISCONNECTED


def test_a_new_child_of_a_negation_replaces_its_child(bt, dialogs, scene):
    negation = bt.AddNode("Negation", 300, 0)
    bt.Connect(negation, scene.leaf)
    menu, node = drag_off(bt, dialogs, label_point(bt, negation, CHILDREN), scene.empty, choose="FailNode")
    assert negation.GetChildren() == [node]
    assert scene.leaf.GetParent() is None
    assert isinstance(negation, NegationNodeWidget)


def test_the_new_node_keeps_working_as_a_tree_node(qtbot, bt, dialogs, scene):
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.root, CHILDREN), scene.empty, choose="FailNode")
    fast_config(bt)
    finished = []
    bt.executionFinished.connect(finished.append)
    bt.Execute()
    qtbot.waitUntil(lambda: not bt.IsExecuting(), timeout=5000)
    assert finished == ["Failed"] and node.GetStatus() is NodeStatus.FAILED


def test_closing_the_menu_without_a_choice_changes_nothing(bt, dialogs, scene):
    bt._set_modified(False)
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.root, CHILDREN), scene.empty)
    assert menu is not None and node is None
    assert drag_line_items(bt) == [] and scene.view.drag_line() is None
    assert not scene.view.is_connecting()
    assert scene.view.connections() == []
    assert scene.root.connection_state(CHILDREN) is ConnectionState.DISCONNECTED
    assert not bt.IsModified()


def test_the_line_stays_visible_while_the_menu_is_open(bt, dialogs, scene):
    seen = []

    def inspect(menu):
        lines = drag_line_items(bt)
        seen.append(len(lines))
        if lines:
            seen.append(close(lines[0].path().pointAtPercent(1.0), scene.view.mapToScene(scene.empty), 1.0))

    dialogs.menu_handler = inspect
    drag(bt, label_point(bt, scene.root, CHILDREN), scene.empty)
    assert seen == [1, True]
    assert drag_line_items(bt) == []


@pytest.mark.parametrize("where", ["node_body", "incompatible_label", "own_label", "field_editor"])
def test_no_menu_when_released_over_a_node(bt, dialogs, scene, where):
    fields = bt.AddNode("AllFields", 300, 0)
    end = {
        "node_body": lambda: viewport_point(bt, scene.leaf, scene.leaf._status_label),
        "incompatible_label": lambda: label_point(bt, fields, PARENT),  # Parent -> Parent
        "own_label": lambda: label_point(bt, scene.leaf, PARENT),
        "field_editor": lambda: viewport_point(bt, fields, fields.field_editor("label")),
    }[where]()
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.leaf, PARENT), end)
    assert menu is None and node is None
    assert drag_line_items(bt) == []
    assert scene.leaf.GetParent() is None


def test_a_click_on_a_label_opens_no_menu(bt, dialogs, scene):
    QTest.mouseClick(bt.view().viewport(), LEFT, NO_MOD, label_point(bt, scene.root, CHILDREN))
    assert dialogs.menus == []


def test_releasing_over_a_connection_line_opens_the_menu(bt, dialogs, scene):
    other = bt.AddNode("Succeed", 300, 220)
    bt.Connect(scene.root, other)
    line = scene.view.connection_item(other)
    on_line = scene.view.mapFromScene(line.path().pointAtPercent(0.5))
    assert scene.view.hit_test(on_line).kind == Hit.CONNECTION
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.leaf, PARENT), on_line, choose="Sequence")
    assert menu is not None and node.GetChildren() == [scene.leaf]


def test_a_compatible_label_still_connects_without_a_menu(bt, dialogs, scene):
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.root, CHILDREN), label_point(bt, scene.leaf, PARENT))
    assert menu is None and node is None
    assert scene.leaf.GetParent() is scene.root


def test_escape_cancels_the_drag_without_a_menu(bt, dialogs, scene):
    press(bt, label_point(bt, scene.root, CHILDREN))
    move(bt, scene.empty)
    QTest.keyClick(bt.view(), Qt.Key.Key_Escape)
    release(bt, scene.empty)
    assert dialogs.menus == []
    assert drag_line_items(bt) == []


def test_no_menu_while_executing(qtbot, bt, dialogs, scene):
    slow = bt.AddNode("SlowCancelable", -300, 220)
    bt.Connect(scene.root, slow)
    fast_config(bt)
    bt.Execute()
    try:
        assert bt.IsExecuting()
        drag(bt, label_point(bt, scene.root, CHILDREN), scene.empty)
        drag(bt, label_point(bt, scene.leaf, PARENT), scene.empty)
        assert dialogs.menus == []
    finally:
        bt.Stop()


def test_the_release_point_is_used_at_any_zoom(bt, dialogs, scene):
    scene.view.set_zoom(1.7)
    scene.view.centerOn(scene.root._item.sceneBoundingRect().center() + QPointF(60, 120))
    end = QPoint(scene.view.viewport().width() - 30, scene.view.viewport().height() - 30)
    assert scene.view.hit_test(end).kind == Hit.EMPTY
    menu, node = drag_off(bt, dialogs, label_point(bt, scene.root, CHILDREN), end, choose="Sequence")
    assert isinstance(node, CompositeNodeWidget)
    assert close(anchor_point(node, PARENT), scene.view.mapToScene(end))


def test_a_source_node_deleted_while_the_menu_is_open_is_not_connected(bt, dialogs, scene):
    def delete_then_choose(menu):
        bt.RemoveNode(scene.leaf)
        action(menu, "Add_Sequence").trigger()

    before = set(bt.GetNodes())
    dialogs.menu_handler = delete_then_choose
    drag(bt, label_point(bt, scene.leaf, PARENT), scene.empty)
    added = set(bt.GetNodes()) - before
    assert len(added) == 1
    node = added.pop()
    assert node.GetChildren() == [] and node.GetParent() is None
