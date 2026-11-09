"""The view repaints only what changed (SmartViewportUpdate) and never leaves stale pixels.

The check: what the window shows (its backing store, read with QScreen.grabWindow) must equal a
complete repaint of the viewport (QWidget.grab paints everything anew). A gesture or a change
that repaints too little leaves a difference, e.g. the ghost of a node that moved away.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsItem, QGraphicsView, QStyleOptionGraphicsItem

from behavior_tree_widget import NodeStatus
from behavior_tree_widget.canvas import SELECTION_COLOR, ConnectionItem
from behavior_tree_widget.nodes import CHILDREN, STATUS_COLORS

from conftest import drag, label_point, viewport_point


def settle(qtbot, ms: int = 30) -> None:
    qtbot.wait(ms)
    QApplication.processEvents()


def shown(view) -> QImage:
    """What the window shows of the viewport now (the backing store, as last painted)."""
    viewport = view.viewport()
    window = viewport.window()
    top_left = viewport.mapTo(window, QPoint(0, 0))
    pixmap = window.screen().grabWindow(window.winId(), top_left.x(), top_left.y(), viewport.width(), viewport.height())
    return pixmap.toImage().convertToFormat(QImage.Format.Format_RGB32)


def rendered(view) -> QImage:
    """The viewport painted completely anew."""
    return view.viewport().grab().toImage().convertToFormat(QImage.Format.Format_RGB32)


def assert_no_stale_pixels(qtbot, view) -> None:
    settle(qtbot)
    on_screen, complete = shown(view), rendered(view)
    assert on_screen.size() == complete.size()
    if on_screen != complete:
        differing = [
            (x, y)
            for y in range(0, complete.height(), 2)
            for x in range(0, complete.width(), 2)
            if on_screen.pixel(x, y) != complete.pixel(x, y)
        ]
        pytest.fail(f"the window shows stale pixels: {len(differing)} sampled pixels differ, e.g. at {differing[:5]}")


def close_to(color: QColor, expected: QColor, tolerance: int = 40) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(color.getRgb()[:3], expected.getRgb()[:3]))


def shows_color(view, scene_rect: QRectF, color: QColor, tolerance: int = 40) -> bool:
    """True if the window shows ``color`` somewhere in ``scene_rect``."""
    image = shown(view)
    area = view.mapFromScene(scene_rect).boundingRect().intersected(image.rect())
    return any(
        close_to(QColor(image.pixel(x, y)), color, tolerance)
        for y in range(area.top(), area.bottom() + 1)
        for x in range(area.left(), area.right() + 1)
    )


def left_border(node) -> QRectF:
    """A strip along the left edge of ``node`` (its status border and its selection outline)."""
    rect = node._item.sceneBoundingRect()
    return QRectF(rect.left(), rect.top() + 20.0, 3.0, rect.height() - 40.0)


@pytest.fixture
def scene(bt, qtbot):
    """Root -> Sequence -> two leaves, all visible."""
    root = bt.GetRootNode()
    seq = bt.AddNode("Sequence", 0, 160)
    left = bt.AddNode("Succeed", -160, 320)
    right = bt.AddNode("Succeed", 140, 320)
    bt.Connect(root, seq)
    bt.Connect(seq, left)
    bt.Connect(seq, right)
    bt.view().centerOn(0, 250)
    settle(qtbot, 60)
    return bt, bt.view(), seq, left, right


def test_the_view_repaints_only_what_changes(bt):
    assert bt.view().viewportUpdateMode() == QGraphicsView.ViewportUpdateMode.SmartViewportUpdate


def test_a_status_change_repaints_the_border_of_the_node(qtbot, scene):
    bt, view, seq, left, right = scene
    assert_no_stale_pixels(qtbot, view)
    for status in (NodeStatus.RUNNING, NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.READY):
        left._set_status(status)
        assert_no_stale_pixels(qtbot, view)
        expected = QColor(STATUS_COLORS[status]) if status is not NodeStatus.READY else QColor(120, 120, 120)
        assert shows_color(view, left_border(left), expected, 30), status
        others = [QColor(STATUS_COLORS[other]) for other in NodeStatus if other not in (status, NodeStatus.READY)]
        assert not any(shows_color(view, left_border(left), other, 30) for other in others), status


def test_moving_a_node_leaves_no_ghost(qtbot, scene):
    bt, view, seq, left, right = scene
    old_center = left._item.sceneBoundingRect().center()
    for offset in (QPointF(-120, 90), QPointF(260, -30), QPointF(-10, 400)):
        left._item.setPos(left._item.pos() + offset)
        assert_no_stale_pixels(qtbot, view)
    assert not left._item.sceneBoundingRect().contains(old_center)


def test_dragging_nodes_with_the_mouse_leaves_no_ghost(qtbot, scene):
    bt, view, seq, left, right = scene
    start = viewport_point(bt, left, left._title_label)
    drag(bt, start, start + QPoint(170, 60), steps=6)
    assert_no_stale_pixels(qtbot, view)
    bt.SelectNodes([seq, left, right])
    start = viewport_point(bt, seq, seq._title_label)
    drag(bt, start, start + QPoint(-90, 80), steps=5)
    assert_no_stale_pixels(qtbot, view)


def test_selection_outlines_appear_and_disappear(qtbot, scene):
    bt, view, seq, left, right = scene
    bt.SelectNodes([left])
    assert_no_stale_pixels(qtbot, view)
    assert shows_color(view, left_border(left), SELECTION_COLOR, 20)
    bt.SelectNodes([right])
    assert_no_stale_pixels(qtbot, view)
    assert not shows_color(view, left_border(left), SELECTION_COLOR, 20)
    assert shows_color(view, left_border(right), SELECTION_COLOR, 20)
    bt.SelectNodes([])
    assert_no_stale_pixels(qtbot, view)


def test_order_badges_appear_change_and_disappear(qtbot, scene):
    bt, view, seq, left, right = scene
    item = view.connection_item(left)
    assert item.order() == 1
    assert shows_color(view, item._badge_rect(), item.color(), 20)
    # Swap the two leaves: the numbers change.
    left_pos, right_pos = QPointF(left._item.pos()), QPointF(right._item.pos())
    left._item.setPos(right_pos + QPointF(40, 0))
    right._item.setPos(left_pos)
    assert view.connection_item(left).order() == 2 and view.connection_item(right).order() == 1
    assert_no_stale_pixels(qtbot, view)
    # Disconnect: the line and its badge disappear.
    badge = view.connection_item(left)._badge_rect()
    color = view.connection_item(left).color()
    bt.Disconnect(left)
    assert_no_stale_pixels(qtbot, view)
    assert not shows_color(view, badge, color, 20)


def test_a_selected_connection_and_the_drag_line_leave_no_trace(qtbot, scene):
    bt, view, seq, left, right = scene
    line = view.connection_item(right)
    view.select_connection(line)
    assert_no_stale_pixels(qtbot, view)
    view.select_connection(None)
    assert_no_stale_pixels(qtbot, view)
    # Drag a connection from the Sequence's Children label to empty canvas, then cancel with Escape.
    start = label_point(bt, seq, CHILDREN)
    drag(bt, start, start + QPoint(260, 200), steps=5, release=False)
    assert view.drag_line() is not None
    assert_no_stale_pixels(qtbot, view)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start + QPoint(260, 200))
    assert view.drag_line() is None
    assert_no_stale_pixels(qtbot, view)


def test_a_selection_band_leaves_nothing_behind(qtbot, scene):
    bt, view, seq, left, right = scene
    empty = view.mapFromScene(QPointF(-420, 120))
    drag(bt, empty, empty + QPoint(300, 260), steps=6, release=False)
    assert_no_stale_pixels(qtbot, view)  # the rectangle is shown
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, empty + QPoint(300, 260))
    assert_no_stale_pixels(qtbot, view)  # and gone
    assert left in bt.GetSelectedNodes()


def test_resizing_a_note_and_moving_a_group_leave_no_ghost(qtbot, scene):
    bt, view, seq, left, right = scene
    note = bt.AddNode("Notes", 300, 120)
    note.SetText("a note")
    group = bt.AddNode("Group", -480, 260)
    settle(qtbot)
    handle = next(h for h in note.resize_handles() if h.objectName() == "ResizeHandleBottomRight")
    start = viewport_point(bt, note, handle)
    drag(bt, start, start + QPoint(-60, -40), steps=4)
    assert_no_stale_pixels(qtbot, view)
    header = view.mapFromScene(group.scene_header_rect().center()) + QPoint(60, 0)
    drag(bt, header, header + QPoint(80, 90), steps=4)
    assert_no_stale_pixels(qtbot, view)


def test_zooming_and_panning_repaint_everything(qtbot, scene):
    bt, view, seq, left, right = scene
    view.set_zoom(0.6)
    assert_no_stale_pixels(qtbot, view)
    start = view.mapFromScene(QPointF(-400, 500))
    drag(bt, start, start + QPoint(70, -50), steps=4, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert_no_stale_pixels(qtbot, view)


def test_a_line_is_only_painted_where_the_repainted_area_meets_it(scene):
    """paint() returns at once when the area being repainted lies within the line's bounds but
    off the line itself (common for the long lines of a wide fan); the stale-pixel checks above
    show that nothing that should be painted is skipped."""
    bt, view, seq, left, right = scene
    item = view.connection_item(right)
    bounds = item.boundingRect()

    def painted(exposed: QRectF) -> bool:
        image = QImage(int(bounds.width()) + 4, int(bounds.height()) + 4, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(-bounds.left() + 2, -bounds.top() + 2)
        option = QStyleOptionGraphicsItem()
        option.exposedRect = exposed
        item.paint(painter, option)
        painter.end()
        return any(image.pixelColor(x, y).alpha() for y in range(image.height()) for x in range(image.width()))

    assert item.flags() & QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption
    curve = item.path()
    on_line = curve.pointAtPercent(0.5)
    assert painted(QRectF(bounds))
    assert painted(QRectF(on_line - QPointF(3, 3), on_line + QPointF(3, 3)))
    # A corner of the bounds that the S-shaped line does not pass through.
    corner = QRectF(bounds.topRight() - QPointF(8, 0), bounds.topRight() + QPointF(0, 8))
    assert not item.hit_area().intersects(corner)
    assert not painted(corner)
