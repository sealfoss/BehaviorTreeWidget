"""Each kind of press starts its gesture object; the view's bookkeeping (root, node order, selection
order, grid) after the split of the canvas module."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from behavior_tree_widget import gestures
from behavior_tree_widget.canvas import GRID_STEP, Hit, TreeView, _Gesture
from behavior_tree_widget.nodes import PARENT

from conftest import label_point, viewport_point

LEFT = Qt.MouseButton.LeftButton
NO_MODIFIER = Qt.KeyboardModifier.NoModifier


@pytest.fixture
def world(bt, qtbot):
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", -40, 200)
    fields = bt.AddNode("AllFields", 260, 200)
    note = bt.AddNode("Notes", -420, 160)
    bt.Connect(root, leaf)
    bt.view().centerOn(0, 200)
    qtbot.wait(20)
    return bt, bt.view(), root, leaf, fields, note


def press_point(world, where: str) -> QPoint:
    bt, view, root, leaf, fields, note = world
    if where == "node":
        return viewport_point(bt, leaf, leaf._title_label)
    if where == "label":
        return label_point(bt, leaf, PARENT)
    if where == "empty":
        return view.mapFromScene(QPointF(-150, 520))
    if where == "input":
        return viewport_point(bt, fields, fields.field_editor("label"))
    if where == "resize":
        handle = next(h for h in note.resize_handles() if h.objectName() == "ResizeHandleRight")
        return viewport_point(bt, note, handle)
    if where == "line":
        return view.mapFromScene(view.connection_item(leaf).path().pointAtPercent(0.5))
    raise AssertionError(where)


@pytest.mark.parametrize(
    "where, button, modifiers, kind, gesture",
    [
        ("node", LEFT, NO_MODIFIER, _Gesture.MOVING_NODE, gestures.MoveGesture),
        ("label", LEFT, NO_MODIFIER, _Gesture.CONNECTING, gestures.ConnectGesture),
        ("empty", LEFT, NO_MODIFIER, _Gesture.SELECTING, gestures.BandGesture),
        ("line", LEFT, NO_MODIFIER, _Gesture.SELECTING, gestures.BandGesture),
        ("node", LEFT, Qt.KeyboardModifier.ControlModifier, _Gesture.PANNING, gestures.PanGesture),
        ("empty", LEFT, Qt.KeyboardModifier.ControlModifier, _Gesture.PANNING, gestures.PanGesture),
        ("node", Qt.MouseButton.MiddleButton, NO_MODIFIER, _Gesture.PANNING, gestures.PanGesture),
        ("input", LEFT, NO_MODIFIER, _Gesture.PASSTHROUGH, gestures.PassThroughGesture),
        ("resize", LEFT, NO_MODIFIER, _Gesture.RESIZING, gestures.ResizeGesture),
    ],
)
def test_each_press_starts_its_gesture_and_the_release_ends_it(world, where, button, modifiers, kind, gesture):
    bt, view, *_ = world
    point = press_point(world, where)
    viewport = view.viewport()
    assert view._gesture is _Gesture.IDLE and view._active is None
    QTest.mousePress(viewport, button, modifiers, point)
    assert view._gesture is kind
    assert type(view._active) is gesture
    if where == "line":
        assert view.selected_connection() is not None
    QTest.mouseRelease(viewport, button, modifiers, point)
    if kind is _Gesture.PASSTHROUGH:
        QTest.keyClick(view, Qt.Key.Key_Escape)  # leave the line edit
    assert view._gesture is _Gesture.IDLE and view._active is None


def test_every_hit_kind_has_a_press_handler():
    handled = set(TreeView._PRESS_HANDLERS) | {Hit.EMPTY}
    assert handled == {Hit.EMPTY, Hit.NODE, Hit.LABEL, Hit.INTERACTIVE, Hit.CONNECTION, Hit.RESIZE}


def test_a_lost_release_ends_the_gesture_at_the_next_press(world):
    bt, view, root, leaf, fields, note = world
    start = press_point(world, "node")
    viewport = view.viewport()
    QTest.mousePress(viewport, LEFT, NO_MODIFIER, start)
    QTest.mouseMove(viewport, start + QPoint(30, 20))
    assert view._gesture is _Gesture.MOVING_NODE
    modified = []
    view.modified.connect(lambda: modified.append(True))
    view._abandon_gesture()  # e.g. a modal dialog took the mouse
    assert view._gesture is _Gesture.IDLE and modified == [True], "the move is reported once"


def test_the_view_keeps_its_root(bt):
    view = bt.view()
    root = bt.GetRootNode()
    assert view.root() is root
    view.clear()
    assert view.root() is None and view.nodes() == []


def test_selected_nodes_come_in_view_order_whatever_the_selection_order(bt):
    nodes = [bt.AddNode("Succeed", x, 200) for x in (-300, -100, 100, 300)]
    bt.SelectNodes([nodes[3], nodes[0]])
    bt.SelectNodes([nodes[2]], add=True)
    assert bt.GetSelectedNodes() == [nodes[0], nodes[2], nodes[3]]
    bt.RemoveNode(nodes[2])
    assert bt.GetSelectedNodes() == [nodes[0], nodes[3]]
    assert bt.view().nodes() == [bt.GetRootNode(), nodes[0], nodes[1], nodes[3]]


def test_the_grid_has_darker_lines_every_fifth_step(bt, qtbot):
    view = bt.view()
    view.set_zoom(1.0)
    view.centerOn(GRID_STEP * 52, GRID_STEP * 52)  # an empty part of the scene
    qtbot.wait(20)
    image = view.viewport().grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    y = view.mapFromScene(QPointF(0, GRID_STEP * 52 + GRID_STEP / 2)).y()

    def darkest_near(scene_x: float) -> int:
        x = view.mapFromScene(QPointF(scene_x, 0)).x()
        return min(QColor(image.pixel(px, y)).lightness() for px in range(x - 1, x + 2))

    background = QColor(image.pixel(view.mapFromScene(QPointF(GRID_STEP * 52.5, 0)).x(), y)).lightness()
    major, minor = darkest_near(GRID_STEP * 50), darkest_near(GRID_STEP * 51)
    assert major < minor < background
