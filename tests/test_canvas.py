"""Mouse / keyboard interaction tests of the tree view (``behavior_tree_widget.canvas``).

Every gesture is driven with real ``QTest`` mouse and key events sent to
``bt.view().viewport()`` (or to the view for key events), exactly as a user would
produce them. Requirements come from ``instructions.txt``:

* drag ChildConnections <-> ParentConnection to connect (red / green / blue line and labels),
* click a line to select it (green), click elsewhere to deselect, Delete removes it,
* a new parent replaces the old one,
* drag on empty space pans, drag on a node moves it, input widgets keep working,
* right click: node menu (delete ...) and empty-space menu (add node at that point),
* the view is disabled until a tree was created or loaded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QColor, QContextMenuEvent, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QStyle, QStyleOptionButton, QStyleOptionSpinBox

from behavior_tree_widget.canvas import (
    MAX_ZOOM,
    MIN_ZOOM,
    ConnectionItem,
    DragLineItem,
    Hit,
    anchor_point,
)
from behavior_tree_widget.nodes import (
    CHILDREN,
    CONNECTION_COLORS,
    PARENT,
    CompositeNodeWidget,
    ConnectionState,
    NodeStatus,
)

from conftest import click, drag, fast_config, label_point, viewport_point

BLUE = QColor(CONNECTION_COLORS[ConnectionState.CONNECTED])
GREEN = QColor(CONNECTION_COLORS[ConnectionState.PLACING])
RED = QColor(CONNECTION_COLORS[ConnectionState.DISCONNECTED])

LEFT = Qt.MouseButton.LeftButton
MIDDLE = Qt.MouseButton.MiddleButton
NO_MOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier


# ============================================================================ helpers
def press(bt, point: QPoint, button=LEFT) -> None:
    QTest.mousePress(bt.view().viewport(), button, NO_MOD, point)


def move(bt, point: QPoint) -> None:
    QTest.mouseMove(bt.view().viewport(), point)


def release(bt, point: QPoint, button=LEFT) -> None:
    QTest.mouseRelease(bt.view().viewport(), button, NO_MOD, point)


def drag_with(bt, start: QPoint, end: QPoint, button=LEFT, steps: int = 4) -> None:
    """Like conftest.drag but with any mouse button."""
    press(bt, start, button)
    for step in range(1, steps + 1):
        point = start + (end - start) * (step / steps)
        move(bt, QPoint(round(point.x()), round(point.y())))
    release(bt, end, button)


def double_click(bt, point: QPoint, button=LEFT) -> None:
    """A double click as the platform delivers it: press, release, double-click press, release.

    (``QTest.mouseDClick`` only sends the lone MouseButtonDblClick event, which a real mouse never does.)
    """
    viewport = bt.view().viewport()
    press(bt, point, button)
    release(bt, point, button)
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(point), QPointF(viewport.mapToGlobal(point)), button, button, NO_MOD
    )
    QApplication.sendEvent(viewport, event)
    release(bt, point, button)


def key(bt, key_code, modifiers=NO_MOD) -> None:
    QTest.keyClick(bt.view(), key_code, modifiers)


def scene_to_view(bt, point: QPointF) -> QPoint:
    return bt.view().mapFromScene(point)


def line_point(bt, item: ConnectionItem, percent: float = 0.5) -> QPoint:
    """Viewport position of a point on a connection line."""
    return scene_to_view(bt, item.path().pointAtPercent(percent))


def title_point(bt, node) -> QPoint:
    return viewport_point(bt, node, node._title_label)


def status_point(bt, node) -> QPoint:
    return viewport_point(bt, node, node._status_label)


def empty_point(bt) -> QPoint:
    """A viewport point far away from every node and line."""
    point = QPoint(15, 15)
    assert bt.view().hit_test(point).kind == Hit.EMPTY
    return point


def checkbox_indicator_point(bt, node, checkbox) -> QPoint:
    """Viewport position of the centre of a check box's indicator (its clickable area)."""
    option = QStyleOptionButton()
    option.initFrom(checkbox)
    rect = checkbox.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, checkbox)
    local = checkbox.mapTo(node, QPointF(rect.center()))
    return scene_to_view(bt, node._item.mapToScene(local))


def spin_button_point(bt, node, spin_box, up: bool = True) -> QPoint:
    """Viewport position of the centre of a spin box's up / down arrow button."""
    option = QStyleOptionSpinBox()
    option.initFrom(spin_box)
    option.subControls = QStyle.SubControl.SC_SpinBoxUp | QStyle.SubControl.SC_SpinBoxDown
    option.frame = spin_box.hasFrame()
    option.stepEnabled = (
        QAbstractSpinBox.StepEnabledFlag.StepUpEnabled | QAbstractSpinBox.StepEnabledFlag.StepDownEnabled
    )
    control = QStyle.SubControl.SC_SpinBoxUp if up else QStyle.SubControl.SC_SpinBoxDown
    rect = spin_box.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, spin_box)
    assert rect.isValid()
    local = spin_box.mapTo(node, QPointF(rect.center()))
    return scene_to_view(bt, node._item.mapToScene(local))


def combo_popup_item_point(bt, combo, row: int) -> QPoint:
    """Viewport position of row ``row`` of an open combo box popup (embedded in the scene)."""
    popup = combo.view()
    container = popup.window()
    proxy = container.graphicsProxyWidget()
    assert proxy is not None, "the combo popup is not embedded in the scene"
    rect = popup.visualRect(popup.model().index(row, 0))
    local = popup.viewport().mapTo(container, QPointF(rect.center()))
    return scene_to_view(bt, proxy.mapToScene(local))


def context_menu(bt, dialogs, point: QPoint):
    """Right click at viewport ``point``; returns the menu shown (or None when no menu was shown)."""
    before = len(dialogs.menus)
    viewport = bt.view().viewport()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point))
    QApplication.sendEvent(viewport, event)
    return dialogs.menus[-1] if len(dialogs.menus) > before else None


def action(menu, name: str) -> QAction:
    for candidate in menu.actions():
        if candidate.objectName() == name:
            return candidate
    raise AssertionError(f"menu has no action named {name!r}: {[a.objectName() for a in menu.actions()]}")


def action_texts(menu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def wheel(bt, steps: float, modifiers=CTRL, point: QPoint | None = None) -> None:
    viewport = bt.view().viewport()
    pos = QPointF(viewport.rect().center() if point is None else point)
    event = QWheelEvent(
        pos,
        QPointF(viewport.mapToGlobal(pos.toPoint())),
        QPoint(0, 0),
        QPoint(0, round(120 * steps)),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(viewport, event)


def assert_label(node, kind: str, state: ConnectionState) -> None:
    """The label's logical state and its displayed colour both match ``state``."""
    assert node.connection_state(kind) is state, f"{node!r} {kind} label is {node.connection_state(kind)}, expected {state}"
    assert CONNECTION_COLORS[state] in node.connection_label(kind).styleSheet()


def drag_items(bt) -> list[DragLineItem]:
    return [item for item in bt.view().scene().items() if isinstance(item, DragLineItem)]


def assert_points_close(a: QPointF, b: QPointF, tolerance: float = 0.51) -> None:
    assert abs(a.x() - b.x()) <= tolerance and abs(a.y() - b.y()) <= tolerance, f"{a} != {b}"


def assert_path_follows(item: ConnectionItem) -> None:
    """The connection line runs from the parent's ChildConnections to the child's ParentConnection."""
    path = item.path()
    assert_points_close(path.pointAtPercent(0.0), anchor_point(item.parent_node, CHILDREN), 0.01)
    assert_points_close(path.pointAtPercent(1.0), anchor_point(item.child_node, PARENT), 0.01)


# ============================================================================ fixtures
@pytest.fixture
def pair(bt):
    """Root at (0, 0) and an unconnected Succeed leaf below it, both visible."""
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", 0, 200)
    bt.view().centerOn(50, 150)
    return SimpleNamespace(root=root, leaf=leaf, view=bt.view())


@pytest.fixture
def world(bt):
    """Root, two composites and three leaves, nothing connected, all visible.

    Layout (scene coordinates of the top-left corners)::

        seq_a (-300, 0)     root (0, 0)        seq_b (300, 0)
        leaf_a (-300, 220)  leaf_b (0, 220)    leaf_c (300, 220)
    """
    root = bt.GetRootNode()
    ns = SimpleNamespace(
        root=root,
        seq_a=bt.AddNode("Sequence", -300, 0),
        seq_b=bt.AddNode("Selector", 300, 0),
        leaf_a=bt.AddNode("Succeed", -300, 220),
        leaf_b=bt.AddNode("FailNode", 0, 220),
        leaf_c=bt.AddNode("NoFields", 300, 220),
        view=bt.view(),
    )
    bt.view().centerOn(50, 150)
    return ns


@pytest.fixture
def connected(pair, bt):
    """``pair`` with the leaf connected below the root."""
    bt.Connect(pair.root, pair.leaf)
    pair.item = pair.view.connection_item(pair.leaf)
    return pair


@pytest.fixture
def fields_node(bt):
    """An AllFields leaf (one editor of every type) centred in the view."""
    node = bt.AddNode("AllFields", 0, 200)
    bt.view().centerOn(node._item.sceneBoundingRect().center())
    return node


@pytest.fixture
def executing(qtbot, bt):
    """Root -> Slow (running on a worker thread) plus unconnected nodes; execution is stopped afterwards."""
    root = bt.GetRootNode()
    slow = bt.AddNode("SlowCancelable", -150, 200)
    other = bt.AddNode("Succeed", 150, 200)
    seq = bt.AddNode("Sequence", 350, 0)
    bt.Connect(root, slow)
    view = bt.view()
    view.centerOn(100, 150)
    fast_config(bt)
    bt.Execute()
    try:
        qtbot.waitUntil(lambda: slow.GetStatus() is NodeStatus.RUNNING, timeout=3000)
        assert bt.IsExecuting()
        yield SimpleNamespace(root=root, slow=slow, other=other, seq=seq, view=view, item=view.connection_item(slow))
    finally:
        bt.Stop()


# ============================================================================ creating connections by dragging
def test_drag_children_to_parent_label_creates_blue_connection(bt, pair):
    drag(bt, label_point(bt, pair.root, CHILDREN), label_point(bt, pair.leaf, PARENT))

    assert pair.leaf.GetParent() is pair.root
    assert pair.root.GetChildren() == [pair.leaf]
    item = pair.view.connection_item(pair.leaf)
    assert isinstance(item, ConnectionItem)
    assert item.scene() is pair.view.scene()
    assert item.color() == BLUE
    assert item.pen().color() == BLUE
    assert not item.is_selected()
    assert_label(pair.root, CHILDREN, ConnectionState.CONNECTED)
    assert_label(pair.leaf, PARENT, ConnectionState.CONNECTED)


def test_drag_parent_to_children_label_creates_blue_connection(bt, pair):
    drag(bt, label_point(bt, pair.leaf, PARENT), label_point(bt, pair.root, CHILDREN))

    assert pair.leaf.GetParent() is pair.root
    item = pair.view.connection_item(pair.leaf)
    assert item is not None and item.color() == BLUE
    assert item.parent_node is pair.root and item.child_node is pair.leaf
    assert_label(pair.root, CHILDREN, ConnectionState.CONNECTED)
    assert_label(pair.leaf, PARENT, ConnectionState.CONNECTED)


def test_connection_line_runs_between_the_two_labels(bt, pair):
    drag(bt, label_point(bt, pair.root, CHILDREN), label_point(bt, pair.leaf, PARENT))
    assert_path_follows(pair.view.connection_item(pair.leaf))


def test_drag_builds_multi_level_tree_with_composites(bt, world):
    """Root -> Sequence -> leaf and Root -> Selector, all made with the mouse."""
    drag(bt, label_point(bt, world.root, CHILDREN), label_point(bt, world.seq_a, PARENT))
    drag(bt, label_point(bt, world.leaf_a, PARENT), label_point(bt, world.seq_a, CHILDREN))
    drag(bt, label_point(bt, world.seq_b, PARENT), label_point(bt, world.root, CHILDREN))

    assert world.seq_a.GetParent() is world.root
    assert world.seq_b.GetParent() is world.root
    assert world.leaf_a.GetParent() is world.seq_a
    assert world.root.GetChildren() == [world.seq_a, world.seq_b]
    assert len(world.view.connections()) == 3
    assert all(item.color() == BLUE for item in world.view.connections())
    assert_label(world.seq_a, PARENT, ConnectionState.CONNECTED)
    assert_label(world.seq_a, CHILDREN, ConnectionState.CONNECTED)
    assert_label(world.seq_b, CHILDREN, ConnectionState.DISCONNECTED)


def test_drag_connect_emits_signals(qtbot, bt, pair):
    with qtbot.waitSignal(pair.view.connectionAdded, timeout=1000) as blocker:
        drag(bt, label_point(bt, pair.root, CHILDREN), label_point(bt, pair.leaf, PARENT))
    assert blocker.args == [pair.root, pair.leaf]
    assert bt.IsModified()


def test_release_position_decides_the_target(bt, pair):
    """The pointer jumps from empty space straight to the target label on release."""
    empty = empty_point(bt)
    press(bt, label_point(bt, pair.root, CHILDREN))
    move(bt, empty)
    release(bt, label_point(bt, pair.leaf, PARENT))
    assert pair.leaf.GetParent() is pair.root
    assert pair.view.drag_line() is None


# ============================================================================ while dragging
def test_drag_line_is_red_away_from_labels_and_source_label_green(bt, pair):
    source = label_point(bt, pair.root, CHILDREN)
    press(bt, source)
    move(bt, source + QPoint(150, 40))
    try:
        line = pair.view.drag_line()
        assert pair.view.is_connecting()
        assert isinstance(line, DragLineItem)
        assert line.scene() is pair.view.scene()
        assert not line.is_valid()
        assert line.color() == RED
        assert line.pen().color() == RED
        assert_label(pair.root, CHILDREN, ConnectionState.PLACING)
        assert_label(pair.leaf, PARENT, ConnectionState.DISCONNECTED)
    finally:
        release(bt, source + QPoint(150, 40))


def test_drag_line_is_green_over_compatible_label_and_target_label_green(bt, pair):
    target = label_point(bt, pair.leaf, PARENT)
    drag(bt, label_point(bt, pair.root, CHILDREN), target, release=False)
    try:
        line = pair.view.drag_line()
        assert line is not None and line.is_valid()
        assert line.color() == GREEN
        assert line.pen().color() == GREEN
        assert_label(pair.root, CHILDREN, ConnectionState.PLACING)
        assert_label(pair.leaf, PARENT, ConnectionState.PLACING)
        assert pair.leaf.GetParent() is None  # nothing is connected before the release
    finally:
        release(bt, empty_point(bt))


def test_drag_from_parent_label_is_green_over_children_label(bt, pair):
    drag(bt, label_point(bt, pair.leaf, PARENT), label_point(bt, pair.root, CHILDREN), release=False)
    try:
        assert pair.view.drag_line().color() == GREEN
        assert_label(pair.leaf, PARENT, ConnectionState.PLACING)
        assert_label(pair.root, CHILDREN, ConnectionState.PLACING)
    finally:
        release(bt, empty_point(bt))


def test_leaving_compatible_label_turns_line_and_target_red_again(bt, pair):
    target = label_point(bt, pair.leaf, PARENT)
    drag(bt, label_point(bt, pair.root, CHILDREN), target, release=False)
    try:
        assert pair.view.drag_line().color() == GREEN
        move(bt, target + QPoint(200, 60))
        assert pair.view.drag_line().color() == RED
        assert_label(pair.leaf, PARENT, ConnectionState.DISCONNECTED)
        assert_label(pair.root, CHILDREN, ConnectionState.PLACING)
    finally:
        release(bt, target + QPoint(200, 60))


def test_hovering_connected_compatible_label_turns_it_green_then_blue_again(bt, world):
    """A child that already has a parent is still a compatible target (the parent is replaced)."""
    bt.Connect(world.root, world.leaf_c)
    target = label_point(bt, world.leaf_c, PARENT)
    drag(bt, label_point(bt, world.seq_b, CHILDREN), target, release=False)
    try:
        assert world.view.drag_line().color() == GREEN
        assert_label(world.leaf_c, PARENT, ConnectionState.PLACING)
        move(bt, empty_point(bt))
        assert world.view.drag_line().color() == RED
        assert_label(world.leaf_c, PARENT, ConnectionState.CONNECTED)
    finally:
        release(bt, empty_point(bt))
    assert world.leaf_c.GetParent() is world.root


@pytest.mark.parametrize("source_kind", [CHILDREN, PARENT])
def test_drag_line_runs_from_source_label_to_cursor(bt, pair, source_kind):
    source_node = pair.root if source_kind == CHILDREN else pair.leaf
    start = label_point(bt, source_node, source_kind)
    cursor = start + QPoint(-180, 90 if source_kind == CHILDREN else -90)
    drag(bt, start, cursor, release=False)
    try:
        path = pair.view.drag_line().path()
        ends = [path.pointAtPercent(0.0), path.pointAtPercent(1.0)]
        anchor = anchor_point(source_node, source_kind)
        cursor_scene = pair.view.mapToScene(cursor)
        assert any(abs(p.x() - anchor.x()) < 0.01 and abs(p.y() - anchor.y()) < 0.01 for p in ends), (ends, anchor)
        assert any(abs(p.x() - cursor_scene.x()) < 0.01 and abs(p.y() - cursor_scene.y()) < 0.01 for p in ends)
    finally:
        release(bt, cursor)


def test_release_over_compatible_label_turns_both_labels_blue_and_removes_drag_line(bt, pair):
    drag(bt, label_point(bt, pair.root, CHILDREN), label_point(bt, pair.leaf, PARENT))
    assert pair.view.drag_line() is None
    assert drag_items(bt) == []
    assert not pair.view.is_connecting()
    assert_label(pair.root, CHILDREN, ConnectionState.CONNECTED)
    assert_label(pair.leaf, PARENT, ConnectionState.CONNECTED)


@pytest.mark.parametrize("where", ["empty", "node_body", "source_label"])
def test_release_elsewhere_removes_line_without_connecting(bt, pair, where):
    source = label_point(bt, pair.root, CHILDREN)
    target_label = label_point(bt, pair.leaf, PARENT)
    end = {
        "empty": empty_point(bt),
        "node_body": status_point(bt, pair.leaf),
        "source_label": source,
    }[where]
    press(bt, source)
    move(bt, target_label)  # hover the compatible label on the way
    move(bt, end)
    release(bt, end)

    assert pair.view.drag_line() is None
    assert drag_items(bt) == []
    assert pair.view.connections() == []
    assert pair.leaf.GetParent() is None
    assert_label(pair.root, CHILDREN, ConnectionState.DISCONNECTED)
    assert_label(pair.leaf, PARENT, ConnectionState.DISCONNECTED)


def test_click_on_label_without_dragging_does_nothing(bt, pair):
    click(bt, label_point(bt, pair.root, CHILDREN))
    assert pair.view.drag_line() is None and not pair.view.is_connecting()
    assert pair.view.connections() == []
    assert_label(pair.root, CHILDREN, ConnectionState.DISCONNECTED)


def test_right_click_during_drag_shows_no_menu_and_keeps_dragging(bt, dialogs, pair):
    target = label_point(bt, pair.leaf, PARENT)
    drag(bt, label_point(bt, pair.root, CHILDREN), target, release=False)
    assert context_menu(bt, dialogs, target) is None
    assert pair.view.is_connecting()
    release(bt, target)
    assert pair.leaf.GetParent() is pair.root


@pytest.mark.parametrize("where", ["children_label", "parent_label", "title", "empty"])
def test_double_click_leaves_no_gesture_behind(bt, pair, where):
    point = {
        "children_label": lambda: label_point(bt, pair.root, CHILDREN),
        "parent_label": lambda: label_point(bt, pair.leaf, PARENT),
        "title": lambda: title_point(bt, pair.leaf),
        "empty": lambda: empty_point(bt),
    }[where]()
    center = pair.view.view_center()
    positions = {node: node._item.pos() for node in bt.GetNodes()}
    double_click(bt, point)
    move(bt, point + QPoint(70, 55))  # hover without any button pressed
    assert pair.view.drag_line() is None and not pair.view.is_connecting()
    assert drag_items(bt) == []
    assert {node: node._item.pos() for node in bt.GetNodes()} == positions
    assert_points_close(pair.view.view_center(), center, 0.01)
    assert pair.view.connections() == []
    assert_label(pair.root, CHILDREN, ConnectionState.DISCONNECTED)
    assert_label(pair.leaf, PARENT, ConnectionState.DISCONNECTED)


def test_cancelled_drag_from_connected_label_keeps_existing_connection_blue(bt, connected):
    """Dragging from an already connected label and dropping nowhere leaves the connection alone."""
    source = label_point(bt, connected.leaf, PARENT)
    press(bt, source)
    move(bt, source + QPoint(150, 80))
    assert_label(connected.leaf, PARENT, ConnectionState.PLACING)
    release(bt, source + QPoint(150, 80))
    assert connected.leaf.GetParent() is connected.root
    assert connected.view.connection_item(connected.leaf) is connected.item
    assert_label(connected.leaf, PARENT, ConnectionState.CONNECTED)
    assert_label(connected.root, CHILDREN, ConnectionState.CONNECTED)


def test_dropping_on_the_current_parent_does_not_duplicate_the_connection(bt, connected):
    drag(bt, label_point(bt, connected.root, CHILDREN), label_point(bt, connected.leaf, PARENT))
    assert connected.view.connections() == [connected.item]
    assert connected.root.GetChildren() == [connected.leaf]


# ============================================================================ incompatible targets
def _incompatible_case(bt, world, case):
    """(source node, source kind, target node, target kind) of an incompatible drag."""
    if case == "parent_to_parent":
        return world.leaf_a, PARENT, world.leaf_b, PARENT
    if case == "children_to_children":
        return world.root, CHILDREN, world.seq_a, CHILDREN
    if case == "own_label_children_to_parent":
        return world.seq_a, CHILDREN, world.seq_a, PARENT
    if case == "own_label_parent_to_children":
        return world.seq_b, PARENT, world.seq_b, CHILDREN
    if case == "cycle":
        bt.Connect(world.seq_a, world.seq_b)  # seq_a is now an ancestor of seq_b
        return world.seq_b, CHILDREN, world.seq_a, PARENT
    if case == "cycle_deep":
        bt.Connect(world.seq_a, world.seq_b)
        bt.Connect(world.seq_b, world.leaf_c)  # unrelated, just to have a deeper tree
        return world.seq_b, CHILDREN, world.seq_a, PARENT
    raise AssertionError(case)


INCOMPATIBLE = [
    "parent_to_parent",
    "children_to_children",
    "own_label_children_to_parent",
    "own_label_parent_to_children",
    "cycle",
    "cycle_deep",
]


@pytest.mark.parametrize("case", INCOMPATIBLE)
def test_incompatible_target_keeps_line_red_and_target_label_unchanged(bt, world, case):
    source, source_kind, target, target_kind = _incompatible_case(bt, world, case)
    target_state = target.connection_state(target_kind)
    drag(bt, label_point(bt, source, source_kind), label_point(bt, target, target_kind), release=False)
    try:
        line = world.view.drag_line()
        assert line is not None
        assert not line.is_valid()
        assert line.color() == RED
        assert (target, target_kind) != (source, source_kind)
        assert target.connection_state(target_kind) is target_state  # e.g. red stays red
        assert target.connection_state(target_kind) is not ConnectionState.PLACING
    finally:
        release(bt, empty_point(bt))


@pytest.mark.parametrize("case", INCOMPATIBLE)
def test_incompatible_target_does_not_connect(bt, world, case):
    source, source_kind, target, target_kind = _incompatible_case(bt, world, case)
    before = {node: node.GetParent() for node in bt.GetNodes()}
    connections_before = set(world.view.connections())
    drag(bt, label_point(bt, source, source_kind), label_point(bt, target, target_kind))

    assert {node: node.GetParent() for node in bt.GetNodes()} == before
    assert set(world.view.connections()) == connections_before
    assert world.view.drag_line() is None
    if case in ("parent_to_parent", "children_to_children"):
        assert_label(source, source_kind, ConnectionState.DISCONNECTED)
        assert_label(target, target_kind, ConnectionState.DISCONNECTED)


# ============================================================================ replacing a parent
def test_new_parent_replaces_old_connection_and_old_parent_turns_red(bt, world):
    bt.Connect(world.root, world.leaf_b)
    old_item = world.view.connection_item(world.leaf_b)

    drag(bt, label_point(bt, world.seq_b, CHILDREN), label_point(bt, world.leaf_b, PARENT))

    assert world.leaf_b.GetParent() is world.seq_b
    assert world.root.GetChildren() == []
    assert world.seq_b.GetChildren() == [world.leaf_b]
    new_item = world.view.connection_item(world.leaf_b)
    assert new_item is not old_item and new_item.parent_node is world.seq_b
    assert old_item.scene() is None
    assert world.view.connections() == [new_item]
    assert new_item.color() == BLUE
    assert_label(world.root, CHILDREN, ConnectionState.DISCONNECTED)
    assert_label(world.seq_b, CHILDREN, ConnectionState.CONNECTED)
    assert_label(world.leaf_b, PARENT, ConnectionState.CONNECTED)


def test_old_parent_with_remaining_children_stays_blue(bt, world):
    bt.Connect(world.root, world.leaf_a)
    bt.Connect(world.root, world.leaf_b)

    drag(bt, label_point(bt, world.seq_b, CHILDREN), label_point(bt, world.leaf_b, PARENT))

    assert world.root.GetChildren() == [world.leaf_a]
    assert world.leaf_b.GetParent() is world.seq_b
    assert_label(world.root, CHILDREN, ConnectionState.CONNECTED)
    assert len(world.view.connections()) == 2


def test_dragging_connected_parent_label_to_new_parent_replaces_connection(qtbot, bt, world):
    bt.Connect(world.root, world.seq_a)
    with qtbot.waitSignal(world.view.connectionRemoved, timeout=1000) as removed:
        drag(bt, label_point(bt, world.seq_a, PARENT), label_point(bt, world.seq_b, CHILDREN))
    assert removed.args == [world.root, world.seq_a]
    assert world.seq_a.GetParent() is world.seq_b
    assert world.root.GetChildren() == []
    assert len(world.view.connections()) == 1
    assert_label(world.root, CHILDREN, ConnectionState.DISCONNECTED)


# ============================================================================ selecting connections
def test_click_on_line_selects_it_green(qtbot, bt, connected):
    with qtbot.waitSignal(connected.view.connectionSelectionChanged, timeout=1000) as blocker:
        click(bt, line_point(bt, connected.item))
    assert blocker.args == [connected.item]
    assert connected.view.selected_connection() is connected.item
    assert connected.item.is_selected()
    assert connected.item.color() == GREEN
    assert connected.item.pen().color() == GREEN


@pytest.mark.parametrize("percent", [0.2, 0.5, 0.8])
def test_click_anywhere_along_the_line_selects_it(bt, connected, percent):
    click(bt, line_point(bt, connected.item, percent))
    assert connected.view.selected_connection() is connected.item


@pytest.mark.parametrize("where", ["empty", "node_title", "node_status", "label", "field"])
def test_click_elsewhere_deselects_line_blue(bt, connected, where):
    fields = bt.AddNode("AllFields", 250, 200)
    click(bt, line_point(bt, connected.item))
    assert connected.item.is_selected()

    point = {
        "empty": lambda: empty_point(bt),
        "node_title": lambda: title_point(bt, connected.root),
        "node_status": lambda: status_point(bt, connected.leaf),
        "label": lambda: label_point(bt, fields, PARENT),
        "field": lambda: viewport_point(bt, fields, fields.field_editor("label")),
    }[where]()
    click(bt, point)

    assert connected.view.selected_connection() is None
    assert not connected.item.is_selected()
    assert connected.item.color() == BLUE
    assert connected.item.pen().color() == BLUE
    assert connected.leaf.GetParent() is connected.root  # deselecting does not delete


def test_click_slightly_beside_the_line_selects_it(bt, connected):
    """The thin line has a tolerant hit area (a few pixels on each side)."""
    point = line_point(bt, connected.item) + QPoint(4, 0)
    click(bt, point)
    assert connected.view.selected_connection() is connected.item


def test_right_click_on_node_deselects_line(bt, dialogs, connected):
    """"Clicking anywhere else after a connection line has been selected deselects it" - right clicks too
    (a right click on empty space already deselects)."""
    click(bt, line_point(bt, connected.item))
    assert connected.item.is_selected()
    menu = context_menu(bt, dialogs, title_point(bt, connected.root))
    assert menu is not None and menu.objectName() == "NodeMenu"
    assert connected.view.selected_connection() is None
    assert connected.item.color() == BLUE


def test_click_other_line_moves_selection(bt, world):
    bt.Connect(world.root, world.leaf_a)
    bt.Connect(world.root, world.leaf_c)
    first = world.view.connection_item(world.leaf_a)
    second = world.view.connection_item(world.leaf_c)

    click(bt, line_point(bt, first))
    assert first.is_selected() and not second.is_selected()
    click(bt, line_point(bt, second))

    assert world.view.selected_connection() is second
    assert second.color() == GREEN
    assert not first.is_selected()
    assert first.color() == BLUE


# ============================================================================ keyboard
@pytest.mark.parametrize("key_code", [Qt.Key.Key_Delete, Qt.Key.Key_Backspace], ids=["Delete", "Backspace"])
def test_delete_keys_remove_selected_connection(qtbot, bt, connected, key_code):
    click(bt, line_point(bt, connected.item))
    with qtbot.waitSignal(connected.view.connectionRemoved, timeout=1000) as blocker:
        key(bt, key_code)
    assert blocker.args == [connected.root, connected.leaf]
    assert connected.leaf.GetParent() is None
    assert connected.root.GetChildren() == []
    assert connected.view.connections() == []
    assert connected.item.scene() is None
    assert connected.view.selected_connection() is None
    assert_label(connected.root, CHILDREN, ConnectionState.DISCONNECTED)
    assert_label(connected.leaf, PARENT, ConnectionState.DISCONNECTED)
    assert set(bt.GetNodes()) == {connected.root, connected.leaf}  # nodes are kept


def test_delete_key_only_removes_the_selected_connection(bt, world):
    bt.Connect(world.root, world.leaf_a)
    bt.Connect(world.root, world.leaf_c)
    click(bt, line_point(bt, world.view.connection_item(world.leaf_c)))
    key(bt, Qt.Key.Key_Delete)
    assert world.leaf_c.GetParent() is None
    assert world.leaf_a.GetParent() is world.root
    assert_label(world.root, CHILDREN, ConnectionState.CONNECTED)


def test_delete_key_without_selection_does_nothing(bt, connected):
    click(bt, empty_point(bt))
    key(bt, Qt.Key.Key_Delete)
    key(bt, Qt.Key.Key_Backspace)
    assert connected.leaf.GetParent() is connected.root
    assert connected.view.connections() == [connected.item]


def test_escape_cancels_drag(bt, pair):
    target = label_point(bt, pair.leaf, PARENT)
    drag(bt, label_point(bt, pair.root, CHILDREN), target, release=False)
    assert pair.view.drag_line() is not None
    key(bt, Qt.Key.Key_Escape)
    assert pair.view.drag_line() is None
    assert not pair.view.is_connecting()
    assert drag_items(bt) == []
    assert_label(pair.root, CHILDREN, ConnectionState.DISCONNECTED)
    assert_label(pair.leaf, PARENT, ConnectionState.DISCONNECTED)
    release(bt, target)  # releasing over the label afterwards must not connect
    assert pair.leaf.GetParent() is None
    assert pair.view.connections() == []


def test_escape_deselects_connection(bt, connected):
    click(bt, line_point(bt, connected.item))
    key(bt, Qt.Key.Key_Escape)
    assert connected.view.selected_connection() is None
    assert connected.item.color() == BLUE
    assert connected.leaf.GetParent() is connected.root


def test_backspace_in_focused_field_edits_text_not_connections(bt, connected):
    fields = bt.AddNode("AllFields", 250, 200)
    click(bt, line_point(bt, connected.item))
    editor = fields.field_editor("label")
    click(bt, viewport_point(bt, fields, editor))  # deselects the line, focuses the editor
    key(bt, Qt.Key.Key_End)
    key(bt, Qt.Key.Key_Backspace)
    assert fields.GetField("label") == "hell"
    assert connected.leaf.GetParent() is connected.root


def test_selecting_line_takes_keyboard_focus_from_field_editor(bt, connected):
    """After editing a field, clicking a line makes Backspace delete the line, not field text."""
    fields = bt.AddNode("AllFields", 250, 200)
    click(bt, viewport_point(bt, fields, fields.field_editor("label")))
    click(bt, line_point(bt, connected.item))
    key(bt, Qt.Key.Key_Backspace)
    assert connected.leaf.GetParent() is None
    assert fields.GetField("label") == "hello"


# ============================================================================ connection context menu
def test_connection_menu_delete_connection(bt, dialogs, connected):
    menu = context_menu(bt, dialogs, line_point(bt, connected.item))
    assert menu is not None
    assert connected.view.selected_connection() is connected.item  # right click selects it
    delete = action(menu, "DeleteConnection")
    assert delete.text() == "Delete Connection"
    assert delete.isEnabled()
    delete.trigger()
    assert connected.leaf.GetParent() is None
    assert connected.view.connections() == []
    assert connected.view.selected_connection() is None
    assert_label(connected.root, CHILDREN, ConnectionState.DISCONNECTED)
    assert_label(connected.leaf, PARENT, ConnectionState.DISCONNECTED)


# ============================================================================ panning
def test_left_drag_on_empty_space_pans_opposite_to_drag(bt, pair):
    view = pair.view
    center = view.view_center()
    root_pos = pair.root._item.pos()
    start = empty_point(bt)
    drag(bt, start, start + QPoint(120, 70))
    moved = view.view_center() - center
    assert_points_close(moved, QPointF(-120, -70), 1.0)
    assert pair.root._item.pos() == root_pos  # nodes stay where they are in the scene


def test_pan_moves_nodes_on_screen_with_the_cursor(bt, pair):
    before = title_point(bt, pair.leaf)
    start = empty_point(bt)
    drag(bt, start, start + QPoint(-90, 40))
    after = title_point(bt, pair.leaf)
    assert after - before == QPoint(-90, 40)


@pytest.mark.parametrize("zoom", [0.5, 1.0, 2.0])
def test_pan_keeps_grabbed_scene_point_under_cursor(bt, pair, zoom):
    pair.view.set_zoom(zoom)
    start = QPoint(40, 60)
    assert pair.view.hit_test(start).kind == Hit.EMPTY
    grabbed = pair.view.mapToScene(start)
    end = start + QPoint(150, 90)
    drag(bt, start, end, steps=6)
    assert_points_close(pair.view.mapToScene(end), grabbed, 1.0 / zoom + 0.01)


def test_drag_starting_on_connection_line_pans(bt, connected):
    """The line lies outside of any node: "clicking and dragging ... outside of any node should pan"."""
    view = connected.view
    center = view.view_center()
    start = line_point(bt, connected.item)
    assert view.hit_test(start).kind == Hit.CONNECTION
    drag(bt, start, start + QPoint(90, 40))
    assert_points_close(view.view_center() - center, QPointF(-90, -40), 1.0)
    assert connected.leaf.GetParent() is connected.root


@pytest.mark.parametrize("where", ["title", "parent_label", "field", "empty"])
def test_middle_drag_pans_even_over_nodes(bt, pair, where):
    fields = bt.AddNode("AllFields", 250, 200)
    view = pair.view
    start = {
        "title": lambda: title_point(bt, pair.leaf),
        "parent_label": lambda: label_point(bt, pair.leaf, PARENT),
        "field": lambda: viewport_point(bt, fields, fields.field_editor("count")),
        "empty": lambda: empty_point(bt),
    }[where]()
    center = view.view_center()
    leaf_pos, fields_pos = pair.leaf._item.pos(), fields._item.pos()
    count = fields.GetField("count")

    drag_with(bt, start, start + QPoint(-100, -50), button=MIDDLE)

    assert_points_close(view.view_center() - center, QPointF(100, 50), 1.0)
    assert pair.leaf._item.pos() == leaf_pos and fields._item.pos() == fields_pos
    assert view.drag_line() is None and view.connections() == []
    assert fields.GetField("count") == count


# ============================================================================ moving nodes
@pytest.mark.parametrize("grip", ["title", "status"])
def test_drag_title_or_status_moves_child_node_and_line_follows(bt, connected, grip):
    view = connected.view
    center = view.view_center()
    start_pos = connected.leaf._item.pos()
    start = title_point(bt, connected.leaf) if grip == "title" else status_point(bt, connected.leaf)

    drag(bt, start, start + QPoint(140, 60))

    assert_points_close(connected.leaf._item.pos(), start_pos + QPointF(140, 60), 0.01)
    assert_points_close(view.view_center(), center, 0.01)  # moving is not panning
    assert connected.leaf.GetParent() is connected.root
    assert_path_follows(connected.item)


@pytest.mark.parametrize("grip", ["title", "status"])
def test_drag_parent_node_moves_it_and_line_start_follows(bt, connected, grip):
    start_pos = connected.root._item.pos()
    start = title_point(bt, connected.root) if grip == "title" else status_point(bt, connected.root)
    drag(bt, start, start + QPoint(-160, -30))
    assert_points_close(connected.root._item.pos(), start_pos + QPointF(-160, -30), 0.01)
    assert_path_follows(connected.item)


def test_moving_composite_updates_lines_to_parent_and_children(bt, world):
    bt.Connect(world.root, world.seq_a)
    bt.Connect(world.seq_a, world.leaf_a)
    start = title_point(bt, world.seq_a)
    drag(bt, start, start + QPoint(40, 100))
    assert_path_follows(world.view.connection_item(world.seq_a))
    assert_path_follows(world.view.connection_item(world.leaf_a))


def test_drag_on_node_margin_moves_node(bt, pair):
    node = pair.leaf
    local = QPointF(2.0, node.height() / 2.0)
    start = scene_to_view(bt, node._item.mapToScene(local))
    assert bt.view().hit_test(start).kind == Hit.NODE
    pos = node._item.pos()
    drag(bt, start, start + QPoint(-35, 45))
    assert_points_close(node._item.pos(), pos + QPointF(-35, 45), 0.01)


def test_line_follows_node_resize_after_rename(bt, dialogs, connected):
    width = connected.leaf.width()
    dialogs.text_answer = ("A much longer title for this leaf node", True)
    action(context_menu(bt, dialogs, title_point(bt, connected.leaf)), "Rename").trigger()
    assert connected.leaf.width() > width
    assert_path_follows(connected.item)
    dialogs.text_answer = ("A much longer title for the root node too", True)
    action(context_menu(bt, dialogs, title_point(bt, connected.root)), "Rename").trigger()
    assert_path_follows(connected.item)


def test_line_follows_composite_type_switch(bt, dialogs, world):
    bt.Connect(world.root, world.seq_a)
    bt.Connect(world.seq_a, world.leaf_a)
    world.seq_a.SetTitle("Custom")  # title becomes "Custom (Sequence)" -> "Custom (Selector)"
    action(context_menu(bt, dialogs, title_point(bt, world.seq_a)), "Type_Selector").trigger()
    assert world.seq_a._title_label.text() == "Custom (Selector)"
    assert_path_follows(world.view.connection_item(world.seq_a))
    assert_path_follows(world.view.connection_item(world.leaf_a))


def test_moving_node_emits_modified(qtbot, bt, pair):
    bt._set_modified(False)
    with qtbot.waitSignal(pair.view.modified, timeout=1000):
        start = title_point(bt, pair.leaf)
        drag(bt, start, start + QPoint(30, 30))
    assert bt.IsModified()


def test_click_on_node_without_moving_does_not_modify_tree(bt, pair):
    bt._set_modified(False)
    pos = pair.leaf._item.pos()
    click(bt, title_point(bt, pair.leaf))
    assert pair.leaf._item.pos() == pos
    assert not bt.IsModified()


def test_node_stops_following_after_release(bt, pair):
    start = title_point(bt, pair.leaf)
    drag(bt, start, start + QPoint(30, 20))
    pos = pair.leaf._item.pos()
    move(bt, start + QPoint(150, 120))
    assert pair.leaf._item.pos() == pos


def test_drag_on_fields_area_outside_editors_moves_node(bt, fields_node):
    """The Fields list is a drag area: grabbing it between the editors moves the node."""
    row = fields_node.field_editor("label").parentWidget()
    local = row.mapTo(fields_node, QPointF(1.0, row.height() / 2.0))
    start = scene_to_view(bt, fields_node._item.mapToScene(local))
    assert bt.view().hit_test(start).kind == Hit.NODE
    pos = fields_node._item.pos()
    drag(bt, start, start + QPoint(50, 25))
    assert_points_close(fields_node._item.pos(), pos + QPointF(50, 25), 0.01)


# ============================================================================ field editors receive input
def test_click_line_edit_and_type(bt, fields_node):
    editor = fields_node.field_editor("label")
    pos = fields_node._item.pos()
    click(bt, viewport_point(bt, fields_node, editor))
    key(bt, Qt.Key.Key_A, CTRL)
    QTest.keyClicks(bt.view(), "world")
    assert fields_node.GetField("label") == "world"
    assert editor.text() == "world"
    assert fields_node._item.pos() == pos


def test_click_spin_box_and_type(bt, fields_node):
    editor = fields_node.field_editor("count")
    pos = fields_node._item.pos()
    click(bt, viewport_point(bt, fields_node, editor))
    key(bt, Qt.Key.Key_A, CTRL)
    QTest.keyClicks(bt.view(), "42")
    assert fields_node.GetField("count") == 42
    assert fields_node._item.pos() == pos


def test_click_double_spin_box_and_type(bt, fields_node):
    editor = fields_node.field_editor("ratio")
    click(bt, viewport_point(bt, fields_node, editor))
    key(bt, Qt.Key.Key_A, CTRL)
    QTest.keyClicks(bt.view(), "7")
    assert fields_node.GetField("ratio") == pytest.approx(7.0)


def test_click_check_box_toggles_it(bt, fields_node):
    editor = fields_node.field_editor("enabled")
    pos = fields_node._item.pos()
    point = checkbox_indicator_point(bt, fields_node, editor)
    click(bt, point)
    assert fields_node.GetField("enabled") is False
    assert not editor.isChecked()
    click(bt, point)
    assert fields_node.GetField("enabled") is True
    assert fields_node._item.pos() == pos


def test_click_combo_box_and_choose_with_keys(qtbot, bt, fields_node):
    editor = fields_node.field_editor("choice")
    pos = fields_node._item.pos()
    click(bt, viewport_point(bt, fields_node, editor))
    qtbot.waitUntil(lambda: editor.view().isVisible(), timeout=2000)
    key(bt, Qt.Key.Key_Down)
    key(bt, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not editor.view().isVisible(), timeout=2000)
    assert fields_node.GetFieldSelectionIndex("choice") == 1
    assert fields_node.GetFieldSelection("choice") == "green"
    assert fields_node._item.pos() == pos


def test_click_combo_box_and_choose_item_with_mouse(qtbot, bt, fields_node):
    editor = fields_node.field_editor("choice")
    pos = fields_node._item.pos()
    click(bt, viewport_point(bt, fields_node, editor))
    qtbot.waitUntil(lambda: editor.view().isVisible(), timeout=2000)
    # QComboBox ignores releases during the double-click interval after the popup opened
    qtbot.wait(QApplication.doubleClickInterval() + 50)
    point = combo_popup_item_point(bt, editor, 2)
    assert bt.view().hit_test(point).kind == Hit.INTERACTIVE
    click(bt, point)
    qtbot.waitUntil(lambda: not editor.view().isVisible(), timeout=2000)
    assert fields_node.GetFieldSelection("choice") == "blue"
    assert editor.currentText() == "blue"
    assert fields_node._item.pos() == pos


def test_view_gestures_work_after_closing_combo_popup_by_clicking_outside(qtbot, bt, fields_node):
    editor = fields_node.field_editor("choice")
    click(bt, viewport_point(bt, fields_node, editor))
    qtbot.waitUntil(lambda: editor.view().isVisible(), timeout=2000)
    click(bt, empty_point(bt))  # closes the popup
    qtbot.waitUntil(lambda: not editor.view().isVisible(), timeout=2000)
    assert fields_node.GetFieldSelectionIndex("choice") == 0
    # the next gestures behave normally again: pan, then move the node
    center = bt.view().view_center()
    start = empty_point(bt)
    drag(bt, start, start + QPoint(40, 30))
    assert_points_close(bt.view().view_center() - center, QPointF(-40, -30), 1.0)
    pos = fields_node._item.pos()
    start = title_point(bt, fields_node)
    drag(bt, start, start + QPoint(25, 35))
    assert_points_close(fields_node._item.pos(), pos + QPointF(25, 35), 0.01)


@pytest.mark.parametrize("field, up, expected", [("count", True, 4), ("count", False, 2), ("ratio", True, 0.6)])
def test_click_spin_box_arrows(qtbot, bt, fields_node, field, up, expected):
    editor = fields_node.field_editor(field)
    point = spin_button_point(bt, fields_node, editor, up)
    assert bt.view().hit_test(point).kind == Hit.INTERACTIVE
    click(bt, point)
    qtbot.wait(700)  # longer than the auto-repeat delay: a lost release would keep stepping
    assert fields_node.GetField(field) == pytest.approx(expected)


@pytest.mark.parametrize("field", ["label", "count", "ratio", "enabled"])
def test_dragging_on_a_field_editor_does_not_move_the_node(bt, fields_node, field):
    editor = fields_node.field_editor(field)
    if field == "enabled":
        start = checkbox_indicator_point(bt, fields_node, editor)
    else:
        start = viewport_point(bt, fields_node, editor)
    assert bt.view().hit_test(start).kind == Hit.INTERACTIVE
    pos = fields_node._item.pos()
    center = bt.view().view_center()
    drag(bt, start, start + QPoint(60, 45))
    assert fields_node._item.pos() == pos
    assert_points_close(bt.view().view_center(), center, 0.01)


def test_click_on_empty_space_ends_field_editing(bt, fields_node):
    editor = fields_node.field_editor("label")
    click(bt, viewport_point(bt, fields_node, editor))
    click(bt, empty_point(bt))
    QTest.keyClicks(bt.view(), "zzz")
    assert fields_node.GetField("label") == "hello"


def test_plain_wheel_over_unfocused_spin_box_does_not_change_field(bt, fields_node):
    editor = fields_node.field_editor("count")
    click(bt, empty_point(bt))
    wheel(bt, 3, modifiers=NO_MOD, point=viewport_point(bt, fields_node, editor))
    assert fields_node.GetField("count") == 3


# ============================================================================ node context menus
@pytest.mark.parametrize("where", ["title", "status", "parent_label"])
def test_right_click_node_shows_node_menu(bt, dialogs, pair, where):
    point = {
        "title": title_point(bt, pair.leaf),
        "status": status_point(bt, pair.leaf),
        "parent_label": label_point(bt, pair.leaf, PARENT),
    }[where]
    menu = context_menu(bt, dialogs, point)
    assert menu is not None and menu.objectName() == "NodeMenu"
    names = [a.objectName() for a in menu.actions() if not a.isSeparator()]
    assert names == ["Rename", "Delete"]  # leaves have no composite options
    assert action(menu, "Delete").isEnabled()


@pytest.mark.parametrize("which", ["root", "composite", "leaf"])
def test_rename_from_node_menu(bt, dialogs, world, which):
    node = {"root": world.root, "composite": world.seq_a, "leaf": world.leaf_a}[which]
    dialogs.text_answer = ("  Renamed Node  ", True)
    menu = context_menu(bt, dialogs, title_point(bt, node))
    action(menu, "Rename").trigger()
    assert "text_dialog" in dialogs.kinds()
    assert node.GetTitle() == "Renamed Node"
    assert node._title_label.text().startswith("Renamed Node")


def test_rename_cancelled_keeps_title(bt, dialogs, pair):
    dialogs.text_answer = ("Other", False)
    menu = context_menu(bt, dialogs, title_point(bt, pair.leaf))
    action(menu, "Rename").trigger()
    assert pair.leaf.GetTitle() == "Succeed"


def test_delete_from_node_menu_removes_node_and_its_connections(qtbot, bt, dialogs, world):
    bt.Connect(world.root, world.seq_a)
    bt.Connect(world.seq_a, world.leaf_a)
    parent_item = world.view.connection_item(world.seq_a)
    child_item = world.view.connection_item(world.leaf_a)

    menu = context_menu(bt, dialogs, title_point(bt, world.seq_a))
    delete = action(menu, "Delete")
    assert delete.isEnabled()
    with qtbot.waitSignal(world.view.nodeRemoved, timeout=1000):
        delete.trigger()

    assert world.seq_a not in bt.GetNodes()
    assert world.leaf_a in bt.GetNodes()
    assert world.leaf_a.GetParent() is None
    assert world.root.GetChildren() == []
    assert world.view.connections() == []
    assert parent_item.scene() is None and child_item.scene() is None
    assert_label(world.root, CHILDREN, ConnectionState.DISCONNECTED)
    assert_label(world.leaf_a, PARENT, ConnectionState.DISCONNECTED)


def test_deleting_node_clears_selected_connection(bt, dialogs, connected):
    click(bt, line_point(bt, connected.item))
    menu = context_menu(bt, dialogs, title_point(bt, connected.leaf))
    action(menu, "Delete").trigger()
    assert connected.view.selected_connection() is None
    assert connected.view.connections() == []


def test_root_delete_action_is_disabled(bt, dialogs, connected):
    menu = context_menu(bt, dialogs, title_point(bt, connected.root))
    delete = action(menu, "Delete")
    assert not delete.isEnabled()
    delete.trigger()  # even if triggered programmatically nothing happens
    assert bt.GetRootNode() is connected.root
    assert connected.root in bt.GetNodes()
    assert connected.leaf.GetParent() is connected.root


@pytest.mark.parametrize("which", ["root", "composite"])
def test_composite_type_switch_from_menu(bt, dialogs, world, which):
    node = world.root if which == "root" else world.seq_a
    menu = context_menu(bt, dialogs, title_point(bt, node))
    sequence, selector = action(menu, "Type_Sequence"), action(menu, "Type_Selector")
    assert sequence.isCheckable() and sequence.isChecked() and sequence.isEnabled()
    assert not selector.isChecked()

    selector.trigger()

    assert node.GetCompositeType() == "Selector"
    assert "Selector" in node._title_label.text()
    menu = context_menu(bt, dialogs, title_point(bt, node))
    assert action(menu, "Type_Selector").isChecked()
    action(menu, "Type_Sequence").trigger()
    assert node.GetCompositeType() == "Sequence"


def test_composite_title_follows_type_switch(bt, dialogs, world):
    menu = context_menu(bt, dialogs, title_point(bt, world.seq_a))
    action(menu, "Type_Selector").trigger()
    assert world.seq_a.GetTitle() == "Selector"
    assert world.seq_a._title_label.text() == "Selector"


@pytest.mark.parametrize("which", ["root", "composite"])
def test_memory_toggle_from_menu(bt, dialogs, world, which):
    node = world.root if which == "root" else world.seq_b
    initial = node.GetMemory()
    menu = context_menu(bt, dialogs, title_point(bt, node))
    memory = action(menu, "Memory")
    assert memory.isCheckable() and memory.isEnabled()
    assert memory.isChecked() is initial

    memory.trigger()
    assert node.GetMemory() is (not initial)

    menu = context_menu(bt, dialogs, title_point(bt, node))
    assert action(menu, "Memory").isChecked() is (not initial)
    action(menu, "Memory").trigger()
    assert node.GetMemory() is initial


# ============================================================================ empty-space menu
def test_empty_space_menu_lists_composites_and_registered_leaf_titles(bt, dialogs, pair):
    menu = context_menu(bt, dialogs, empty_point(bt))
    assert menu is not None and menu.objectName() == "AddNodeMenu"
    texts = action_texts(menu)
    assert "Sequence" in texts and "Selector" in texts
    leaf_titles = {bt.GetNodeType(name)._title for name in bt.GetNodeTypes() if bt.GetNodeType(name) is not None}
    assert leaf_titles  # the fixture registers many node types
    for title in leaf_titles:
        assert title in texts, f"{title!r} missing from the add menu"
    assert texts.index("Sequence") < texts.index("Selector")
    add_actions = [a for a in menu.actions() if a.objectName().startswith("Add_")]
    assert all(a.isEnabled() for a in add_actions)
    assert len(add_actions) == len(bt.GetNodeTypes())


@pytest.mark.parametrize(
    "type_name, kind",
    [("Sequence", "composite"), ("Selector", "composite"), ("Succeed", "leaf"), ("AllFields", "leaf")],
)
def test_add_node_from_menu_places_top_left_at_click(qtbot, bt, dialogs, pair, type_name, kind):
    point = QPoint(250, 120)
    assert bt.view().hit_test(point).kind == Hit.EMPTY
    before = set(bt.GetNodes())
    menu = context_menu(bt, dialogs, point)
    with qtbot.waitSignal(pair.view.nodeAdded, timeout=1000):
        action(menu, f"Add_{type_name}").trigger()

    added = set(bt.GetNodes()) - before
    assert len(added) == 1
    node = added.pop()
    assert node.GetTypeName() == type_name
    assert isinstance(node, CompositeNodeWidget) is (kind == "composite")
    assert_points_close(node._item.pos(), pair.view.mapToScene(point), 0.01)
    assert node.GetParent() is None
    assert_label(node, PARENT, ConnectionState.DISCONNECTED)


def test_add_node_position_respects_zoom_and_pan(bt, dialogs, pair):
    pair.view.set_zoom(1.6)
    start = empty_point(bt)
    drag(bt, start, start + QPoint(-70, 30))  # pan a bit first
    point = QPoint(300, 200)
    assert pair.view.hit_test(point).kind == Hit.EMPTY
    menu = context_menu(bt, dialogs, point)
    action(menu, "Add_Selector").trigger()
    node = bt.GetNodes()[-1]
    assert node.GetCompositeType() == "Selector"
    assert_points_close(node._item.pos(), pair.view.mapToScene(point), 0.01)


def test_right_click_empty_space_deselects_connection(bt, dialogs, connected):
    click(bt, line_point(bt, connected.item))
    context_menu(bt, dialogs, empty_point(bt))
    assert connected.view.selected_connection() is None


# ============================================================================ locked while executing
def test_drag_connect_does_not_connect_while_executing(bt, executing):
    assert executing.view.is_locked()
    drag(bt, label_point(bt, executing.root, CHILDREN), label_point(bt, executing.other, PARENT))
    assert executing.other.GetParent() is None
    drag(bt, label_point(bt, executing.seq, PARENT), label_point(bt, executing.root, CHILDREN))
    assert executing.seq.GetParent() is None
    assert executing.view.connections() == [executing.item]
    assert executing.view.drag_line() is None


def test_delete_key_does_not_delete_connection_while_executing(bt, executing):
    click(bt, line_point(bt, executing.item))
    executing.view.select_connection(executing.item)
    key(bt, Qt.Key.Key_Delete)
    key(bt, Qt.Key.Key_Backspace)
    assert executing.slow.GetParent() is executing.root
    assert executing.view.connections() == [executing.item]


def test_menus_disable_structure_actions_while_executing(bt, dialogs, executing):
    menu = context_menu(bt, dialogs, empty_point(bt))
    add_actions = [a for a in menu.actions() if a.objectName().startswith("Add_")]
    assert add_actions and not any(a.isEnabled() for a in add_actions)
    before = set(bt.GetNodes())
    add_actions[0].trigger()
    assert set(bt.GetNodes()) == before

    menu = context_menu(bt, dialogs, title_point(bt, executing.other))
    delete = action(menu, "Delete")
    assert not delete.isEnabled()
    delete.trigger()
    assert executing.other in bt.GetNodes()

    menu = context_menu(bt, dialogs, title_point(bt, executing.seq))
    assert not action(menu, "Type_Selector").isEnabled()
    assert not action(menu, "Memory").isEnabled()
    assert not action(menu, "Delete").isEnabled()

    menu = context_menu(bt, dialogs, line_point(bt, executing.item))
    delete_connection = action(menu, "DeleteConnection")
    assert not delete_connection.isEnabled()
    delete_connection.trigger()
    assert executing.slow.GetParent() is executing.root


def test_moving_nodes_works_while_executing(bt, executing):
    pos = executing.slow._item.pos()
    start = title_point(bt, executing.slow)
    drag(bt, start, start + QPoint(-40, 60))
    assert_points_close(executing.slow._item.pos(), pos + QPointF(-40, 60), 0.01)
    assert_path_follows(executing.item)
    assert bt.IsExecuting()


def test_panning_works_while_executing(bt, executing):
    center = executing.view.view_center()
    start = empty_point(bt)
    drag(bt, start, start + QPoint(80, 50))
    assert_points_close(executing.view.view_center() - center, QPointF(-80, -50), 1.0)
    center = executing.view.view_center()
    drag_with(bt, title_point(bt, executing.other), title_point(bt, executing.other) + QPoint(-30, 20), MIDDLE)
    assert_points_close(executing.view.view_center() - center, QPointF(30, -20), 1.0)


def test_structure_is_locked_while_paused(bt, executing):
    bt.Pause()
    assert bt.GetExecutionState() == "Paused"
    drag(bt, label_point(bt, executing.root, CHILDREN), label_point(bt, executing.other, PARENT))
    assert executing.other.GetParent() is None


def test_structure_unlocks_after_stop(bt, executing):
    bt.Stop()
    assert not executing.view.is_locked()
    drag(bt, label_point(bt, executing.root, CHILDREN), label_point(bt, executing.other, PARENT))
    assert executing.other.GetParent() is executing.root


def test_starting_execution_cancels_a_drag_in_progress(qtbot, bt, pair):
    target = label_point(bt, pair.leaf, PARENT)
    drag(bt, label_point(bt, pair.root, CHILDREN), target, release=False)
    assert pair.view.is_connecting()
    slow = bt.AddNode("SlowCancelable", 400, 400)
    bt.Connect(pair.root, slow)
    fast_config(bt)
    try:
        bt.Execute()
        assert pair.view.drag_line() is None and not pair.view.is_connecting()
        release(bt, target)
        assert pair.leaf.GetParent() is None
    finally:
        bt.Stop()


# ============================================================================ disabled until New / Load
@pytest.fixture
def unloaded(make_widget):
    widget = make_widget()
    widget.view().centerOn(widget.GetRootNode()._item.sceneBoundingRect().center())
    return widget


def test_view_is_disabled_before_new_or_load(unloaded):
    assert not unloaded.view().isEnabled()
    assert not unloaded.IsTreeLoaded()
    assert unloaded.GetRootNode() is not None  # the root is already in place


def test_disabled_view_ignores_node_drag(unloaded):
    root = unloaded.GetRootNode()
    pos = root._item.pos()
    start = title_point(unloaded, root)
    drag(unloaded, start, start + QPoint(80, 60))
    assert root._item.pos() == pos


def test_disabled_view_ignores_pan(unloaded):
    view = unloaded.view()
    center = view.view_center()
    drag(unloaded, QPoint(10, 10), QPoint(130, 90))
    drag_with(unloaded, QPoint(10, 10), QPoint(130, 90), MIDDLE)
    assert_points_close(view.view_center(), center, 0.01)


def test_disabled_view_ignores_connection_drag(unloaded):
    root = unloaded.GetRootNode()
    start = label_point(unloaded, root, CHILDREN)
    drag(unloaded, start, start + QPoint(0, 120), release=False)
    try:
        assert unloaded.view().drag_line() is None
        assert not unloaded.view().is_connecting()
        assert_label(root, CHILDREN, ConnectionState.DISCONNECTED)
    finally:
        release(unloaded, start + QPoint(0, 120))


def test_disabled_view_shows_no_context_menu(unloaded, dialogs):
    root = unloaded.GetRootNode()
    assert context_menu(unloaded, dialogs, QPoint(10, 10)) is None
    assert context_menu(unloaded, dialogs, title_point(unloaded, root)) is None
    assert len(unloaded.GetNodes()) == 1


def test_disabled_view_ignores_ctrl_wheel(unloaded):
    zoom = unloaded.view().zoom()
    wheel(unloaded, 2)
    assert unloaded.view().zoom() == pytest.approx(zoom)


def test_view_accepts_input_after_new_tree(unloaded, tmp_path):
    assert unloaded.NewTree(str(tmp_path / "t.json"))
    assert unloaded.view().isEnabled()
    root = unloaded.GetRootNode()
    unloaded.view().centerOn(root._item.sceneBoundingRect().center())
    pos = root._item.pos()
    start = title_point(unloaded, root)
    drag(unloaded, start, start + QPoint(40, 30))
    assert_points_close(root._item.pos(), pos + QPointF(40, 30), 0.01)


# ============================================================================ zoom
def test_ctrl_wheel_zooms_in_and_out(bt, pair):
    view = pair.view
    assert view.zoom() == pytest.approx(1.0)
    wheel(bt, 1)
    zoomed_in = view.zoom()
    assert zoomed_in > 1.0
    wheel(bt, -1)
    assert view.zoom() == pytest.approx(1.0)
    wheel(bt, -1)
    assert view.zoom() < 1.0


def test_ctrl_wheel_zoom_is_clamped(bt, pair):
    view = pair.view
    for _ in range(40):
        wheel(bt, 1)
        assert view.zoom() <= MAX_ZOOM + 1e-9
    assert view.zoom() == pytest.approx(MAX_ZOOM)
    for _ in range(60):
        wheel(bt, -1)
        assert view.zoom() >= MIN_ZOOM - 1e-9
    assert view.zoom() == pytest.approx(MIN_ZOOM)


@pytest.mark.parametrize("steps, expected", [(50, MAX_ZOOM), (-50, MIN_ZOOM)])
def test_single_large_ctrl_wheel_step_is_clamped(bt, pair, steps, expected):
    wheel(bt, steps)
    assert pair.view.zoom() == pytest.approx(expected)


def test_wheel_without_ctrl_does_not_zoom(bt, pair):
    wheel(bt, 2, modifiers=NO_MOD)
    wheel(bt, -3, modifiers=NO_MOD)
    assert pair.view.zoom() == pytest.approx(1.0)


def test_gestures_work_when_zoomed(bt, pair):
    """Connecting and moving still hit the right labels / distances at another zoom level."""
    for _ in range(3):
        wheel(bt, 1)
    pair.view.centerOn(50, 150)
    zoom = pair.view.zoom()
    assert zoom > 1.2
    drag(bt, label_point(bt, pair.root, CHILDREN), label_point(bt, pair.leaf, PARENT))
    assert pair.leaf.GetParent() is pair.root
    pos = pair.leaf._item.pos()
    start = title_point(bt, pair.leaf)
    drag(bt, start, start + QPoint(round(40 * zoom), 0))
    assert_points_close(pair.leaf._item.pos(), pos + QPointF(40, 0), 1.0)


# ============================================================================ order badges
@pytest.fixture
def three_children(bt):
    root = bt.GetRootNode()
    left = bt.AddNode("Succeed", -250, 220)
    middle = bt.AddNode("FailNode", 0, 220)
    right = bt.AddNode("NoFields", 250, 220)
    bt.view().centerOn(50, 150)
    # connect in a scrambled order with the mouse
    for child in (middle, right, left):
        drag(bt, label_point(bt, root, CHILDREN), label_point(bt, child, PARENT))
    return SimpleNamespace(root=root, left=left, middle=middle, right=right, view=bt.view())


def orders(view, *children) -> list[int]:
    return [view.connection_item(child).order() for child in children]


def test_order_badges_number_children_left_to_right(three_children):
    t = three_children
    assert t.root.GetChildren() == [t.left, t.middle, t.right]
    assert orders(t.view, t.left, t.middle, t.right) == [1, 2, 3]


def test_order_badges_update_after_moving_child(bt, three_children):
    t = three_children
    start = title_point(bt, t.left)
    drag(bt, start, start + QPoint(650, 0))  # left child now right of every other child
    assert t.root.GetChildren() == [t.middle, t.right, t.left]
    assert orders(t.view, t.middle, t.right, t.left) == [1, 2, 3]

    start = title_point(bt, t.right)
    drag(bt, start, start + QPoint(-500, 0))  # right child moves to the far left
    assert t.root.GetChildren() == [t.right, t.middle, t.left]
    assert orders(t.view, t.right, t.middle, t.left) == [1, 2, 3]


def test_order_badges_renumber_after_deleting_a_connection(bt, three_children):
    t = three_children
    click(bt, line_point(bt, t.view.connection_item(t.middle)))
    key(bt, Qt.Key.Key_Delete)
    assert t.root.GetChildren() == [t.left, t.right]
    assert orders(t.view, t.left, t.right) == [1, 2]


def test_order_badges_are_per_parent(bt, world):
    bt.Connect(world.root, world.leaf_b)
    bt.Connect(world.seq_a, world.leaf_a)
    assert world.view.connection_item(world.leaf_b).order() == 1
    assert world.view.connection_item(world.leaf_a).order() == 1


# ============================================================================ hit_test
@pytest.fixture
def hit_scene(bt):
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", 0, 200)
    fields = bt.AddNode("AllFields", -330, 200)
    bt.Connect(root, leaf)
    bt.view().centerOn(0, 200)
    return SimpleNamespace(root=root, leaf=leaf, fields=fields, view=bt.view(), item=bt.view().connection_item(leaf))


def test_hit_test_empty(bt, hit_scene):
    hit = hit_scene.view.hit_test(QPoint(5, 5))
    assert hit.kind == Hit.EMPTY and hit.node is None and hit.connection is None


@pytest.mark.parametrize("grip", ["title", "status"])
def test_hit_test_node_body(bt, hit_scene, grip):
    point = title_point(bt, hit_scene.leaf) if grip == "title" else status_point(bt, hit_scene.leaf)
    hit = hit_scene.view.hit_test(point)
    assert hit.kind == Hit.NODE
    assert hit.node is hit_scene.leaf


@pytest.mark.parametrize("which, kind", [("leaf", PARENT), ("root", CHILDREN), ("fields", PARENT)])
def test_hit_test_connection_labels(bt, hit_scene, which, kind):
    node = getattr(hit_scene, which)
    hit = hit_scene.view.hit_test(label_point(bt, node, kind))
    assert hit.kind == Hit.LABEL
    assert hit.node is node
    assert hit.label_kind == kind


@pytest.mark.parametrize("field", ["label", "count", "ratio", "choice"])
def test_hit_test_interactive_field_editors(bt, hit_scene, field):
    editor = hit_scene.fields.field_editor(field)
    hit = hit_scene.view.hit_test(viewport_point(bt, hit_scene.fields, editor))
    assert hit.kind == Hit.INTERACTIVE
    assert hit.node is hit_scene.fields
    assert hit.widget is editor or editor.isAncestorOf(hit.widget)


def test_hit_test_check_box_indicator_is_interactive(bt, hit_scene):
    editor = hit_scene.fields.field_editor("enabled")
    hit = hit_scene.view.hit_test(checkbox_indicator_point(bt, hit_scene.fields, editor))
    assert hit.kind == Hit.INTERACTIVE and hit.widget is editor


def test_hit_test_connection_line(bt, hit_scene):
    hit = hit_scene.view.hit_test(line_point(bt, hit_scene.item))
    assert hit.kind == Hit.CONNECTION
    assert hit.connection is hit_scene.item


def test_hit_test_root_has_no_parent_label(bt, hit_scene):
    """The root's top edge is node body: it has no ParentConnection label."""
    root = hit_scene.root
    assert hit_scene.root.connection_label(PARENT) is None
    top = scene_to_view(bt, root._item.mapToScene(QPointF(root.width() / 2.0, 2.0)))
    hit = hit_scene.view.hit_test(top)
    assert hit.kind == Hit.NODE and hit.node is root


def test_hit_test_ignores_drag_line(bt, hit_scene):
    start = label_point(bt, hit_scene.root, CHILDREN)
    end = QPoint(40, 40)
    drag(bt, start, end, release=False)
    try:
        assert hit_scene.view.drag_line() is not None
        assert hit_scene.view.hit_test(end).kind == Hit.EMPTY
    finally:
        release(bt, end)


def test_hit_test_node_on_top_wins_over_line(bt, hit_scene):
    """A node lying over a connection line is hit instead of the line."""
    over = bt.AddNode("NoFields", 0, 0)
    mid = hit_scene.item.path().pointAtPercent(0.5)
    over._item.setPos(mid - QPointF(over.width() / 2.0, over.height() / 2.0))
    hit = hit_scene.view.hit_test(scene_to_view(bt, mid))
    assert hit.kind in (Hit.NODE, Hit.LABEL)
    assert hit.node is over
