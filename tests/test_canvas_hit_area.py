"""Connection lines are hit-tested by Qt from a cached area; a connection drag looks for labels
only when the mouse moved far enough."""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QPainterPathStroker
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QGraphicsPathItem

from behavior_tree_widget.canvas import LINE_HIT_WIDTH, LINE_WIDTH, ConnectionItem, Hit
from behavior_tree_widget.geometry import anchor_point
from behavior_tree_widget.nodes import CHILDREN, PARENT

from conftest import drag, label_point


@pytest.fixture
def line(bt, qtbot):
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", 160, 260)
    bt.Connect(root, leaf)
    bt.view().centerOn(100, 200)
    qtbot.wait(20)
    return bt, bt.view(), root, leaf, bt.view().connection_item(leaf)


def normal_at(path, percent: float) -> QPointF:
    """Unit vector across the curve at ``percent``."""
    angle = math.radians(path.angleAtPercent(percent))
    return QPointF(math.sin(angle), math.cos(angle))  # perpendicular to (cos, -sin)


def test_connection_items_leave_hit_testing_to_qt(line):
    bt, view, root, leaf, item = line
    assert "shape" not in vars(ConnectionItem) and "boundingRect" not in vars(ConnectionItem)
    assert QGraphicsPathItem.pen(item).style() == Qt.PenStyle.NoPen, "Qt's shape is then the hit area itself"
    assert item.shape() == item.hit_area()
    assert item.pen().widthF() == LINE_WIDTH and item.pen().color() == item.color()
    path = item.path()
    assert path.pointAtPercent(0.0) == anchor_point(root, CHILDREN)
    assert path.pointAtPercent(1.0) == anchor_point(leaf, PARENT)


def test_the_hit_area_is_the_line_twelve_pixels_wide_and_the_badge(line):
    bt, view, root, leaf, item = line
    area, curve = item.hit_area(), item.path()
    half = LINE_HIT_WIDTH / 2.0
    for percent in (0.1, 0.3, 0.5, 0.7):
        point = curve.pointAtPercent(percent)
        across = normal_at(curve, percent)
        assert area.contains(point)
        assert area.contains(point + across * (half - 1.0)) and area.contains(point - across * (half - 1.0))
        assert not area.contains(point + across * (half + 2.0)) and not area.contains(point - across * (half + 2.0))
    center, radius = item._badge_center(), item.BADGE_RADIUS
    for degrees in range(0, 360, 30):
        rim = center + QPointF(math.cos(math.radians(degrees)), math.sin(math.radians(degrees))) * (radius - 0.5)
        assert area.contains(rim), degrees
    assert item.boundingRect().contains(item._badge_rect())


@pytest.mark.parametrize("offset", [(0, 200), (300, 160), (-300, 160), (250, -260), (-10, 30), (0, -150)])
def test_the_badge_never_cancels_the_line(bt, offset):
    """The badge and the line overlap; whichever way the line runs, both keep taking clicks."""
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", offset[0], offset[1])
    bt.Connect(root, leaf)
    item = bt.view().connection_item(leaf)
    area, curve = item.hit_area(), item.path()
    assert area.contains(item._badge_center())
    for percent in (0.02, 0.25, 0.5, 0.75, 0.98, 1.0):
        assert area.contains(curve.pointAtPercent(percent)), percent


def test_hit_test_finds_lines_and_their_badges(line):
    bt, view, root, leaf, item = line
    curve = item.path()
    middle = curve.pointAtPercent(0.5)
    across = normal_at(curve, 0.5)
    assert view.hit_test(view.mapFromScene(middle)).connection is item
    assert view.hit_test(view.mapFromScene(middle + across * 4.0)).kind == Hit.CONNECTION
    assert view.hit_test(view.mapFromScene(middle + across * 10.0)).kind == Hit.EMPTY
    rim = item._badge_center() + QPointF(item.BADGE_RADIUS - 1.0, 0.0)
    assert view.hit_test(view.mapFromScene(rim)).connection is item


def test_the_hit_area_follows_moves_and_the_badge(line):
    bt, view, root, leaf, item = line
    old_middle = item.path().pointAtPercent(0.5)
    leaf._item.setPos(leaf._item.pos() + QPointF(-260, 90))
    assert not item.hit_area().contains(old_middle)
    assert item.hit_area().contains(item.path().pointAtPercent(0.5))
    rim = item._badge_center() + QPointF(item.BADGE_RADIUS - 0.5, 0.0)
    assert item.order() == 1 and item.hit_area().contains(rim)
    item.set_order(0)  # no badge
    assert not item.hit_area().contains(rim)
    assert not item.boundingRect().contains(item._badge_rect())
    item.set_order(1)
    assert item.hit_area().contains(rim)


def test_a_connection_drag_looks_for_labels_only_when_the_mouse_moved_two_pixels(line, monkeypatch):
    bt, view, root, leaf, item = line
    other = bt.AddNode("Succeed", -200, 260)
    calls = []
    original = view.hit_test

    def counting(pos):
        calls.append(QPoint(pos))
        return original(pos)

    monkeypatch.setattr(view, "hit_test", counting)
    start = label_point(bt, root, CHILDREN)
    target = label_point(bt, other, PARENT)
    drag(bt, start, target, steps=3, release=False)
    assert view.is_connecting() and view.drag_line().is_valid(), "over the compatible label"
    viewport = view.viewport()
    calls.clear()
    QTest.mouseMove(viewport, target + QPoint(1, 0))
    assert calls == [], "moved one pixel: no new look for a label"
    QTest.mouseMove(viewport, target + QPoint(3, 0))
    assert len(calls) == 1
    view.set_zoom(view.zoom() * 1.1)  # the scene moved under the mouse
    calls.clear()
    QTest.mouseMove(viewport, target + QPoint(3, 1))
    assert len(calls) == 1, "a new transform always looks again"
    target = label_point(bt, other, PARENT)
    QTest.mouseMove(viewport, target)
    calls.clear()
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target)
    assert calls, "the release always looks for the label it ends on"
    assert other.GetParent() is root


def test_a_badge_drawn_against_the_direction_of_the_outline_is_turned_round(line, monkeypatch):
    """Overlapping outlines of opposite direction cancel out with the winding fill rule; the
    badge is then added the other way round (Qt's stroker is not documented to keep a direction)."""
    bt, view, root, leaf, item = line
    original = QPainterPathStroker.createStroke

    def reversed_stroke(self, path):
        outline = original(self, path).toReversed()
        outline.setFillRule(Qt.FillRule.OddEvenFill)  # also a fill rule that would leave the overlap empty
        return outline

    monkeypatch.setattr(QPainterPathStroker, "createStroke", reversed_stroke)
    item.update_path()
    area = item.hit_area()
    assert area.contains(item._badge_center())
    for percent in (0.5, 0.9, 0.98):
        assert area.contains(item.path().pointAtPercent(percent)), percent
