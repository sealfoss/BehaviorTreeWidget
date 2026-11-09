"""The tree view: node positions, view positioning, presses inside and outside the view, context
menus, and the view while the tree executes."""

from __future__ import annotations

import json
import logging

import pytest
import shiboken6
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMenu, QPushButton, QWidget
from pytestqt.exceptions import capture_exceptions

from behavior_tree_widget import BehaviorTreeWidget
from behavior_tree_widget.canvas import Hit, _Gesture
from behavior_tree_widget.nodes import PARENT

from conftest import drag, fast_config, label_point, viewport_point
from support.round3 import (
    CTRL,
    LEFT,
    LOGGER,
    NO_MOD,
    OUTSIDE_TARGETS,
    POSITION_LIMIT,
    R3Probe,
    R3Untitled,
    _frame_background,
    _hidden_tree_view_widget,
    _root_top,
    active,
    context_menu,
    describe,
    empty_point,
    executing,
    flush_deferred_deletes,
    line_point,
    menu_action,
    next_tree_file,
    node_entry,
    record,
    save,
    selected,
    title_point,
    warning_texts,
    warnings_logged,
    write_tree,
)
from support.round4 import Quick, build
from support.round5 import Endless
from support.round5 import Quick as R5Quick


# ============================================================================ positions
def test_add_node_clamps_positions_to_the_scene(qtbot, bt):
    limit = POSITION_LIMIT
    scene_rect = bt.view().scene().sceneRect()
    cases = [
        (("Succeed", 1e7, -1e7), (limit, -limit)),
        (("Sequence", -60_000.0, 10.0), (-limit, 10.0)),
        ((R3Untitled, 49_000.5, 47_999.0), (limit, 47_999.0)),
        (("Selector", limit, -limit), (limit, -limit)),
        (("Succeed", float("inf"), float("-inf")), (limit, -limit)),
    ]
    instance = R3Probe()
    cases.append(((instance, 1e9, 1e9), (limit, limit)))
    for (node_type, x, y), (ex, ey) in cases:
        node = bt.AddNode(node_type, x, y)
        pos = node._item.pos()
        assert (pos.x(), pos.y()) == (ex, ey), f"AddNode({node_type!r}, {x}, {y}) placed at {pos}"
        assert scene_rect.contains(node._item.sceneBoundingRect()), "the node must lie inside the scrollable scene"

@pytest.mark.parametrize("x,y", [(float("nan"), 5.0), (5.0, float("nan"))], ids=["nan-x", "nan-y"])
def test_add_node_never_places_a_node_at_a_non_finite_position(qtbot, bt, tmp_path, x, y):
    """AddNode keeps positions inside the scene area: a NaN coordinate must be rejected (ValueError) or
    replaced by a finite, in-area value - never stored (and later saved as a bare NaN token)."""
    nodes_before = bt.GetNodes()
    try:
        node = bt.AddNode("Succeed", x, y)
    except ValueError:
        assert bt.GetNodes() == nodes_before
        return
    pos = node._item.pos()
    assert all(v == v and abs(v) <= POSITION_LIMIT for v in (pos.x(), pos.y())), (
        f"AddNode({x}, {y}) placed the node at ({pos.x()}, {pos.y()})"
    )
    out = tmp_path / "nan_BT.json"
    assert bt.SaveTree(str(out))
    assert "NaN" not in out.read_text(encoding="utf-8")

def test_load_clamps_positions_and_reports_it(qtbot, make_widget, dialogs, tmp_path, caplog):
    limit = POSITION_LIMIT
    path = write_tree(
        tmp_path / "far.json",
        [
            node_entry("r", "Root"),
            node_entry("far", "Succeed", 1e6, 20.0),
            node_entry("neg", "Succeed", -3e5, -3e5),
            node_entry("edge", "Succeed", limit, -limit),
            node_entry("near", "Succeed", 100.0, 200.0),
        ],
    )
    widget = make_widget()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert widget.LoadTree(path)
    positions = {node.GetId(): (node._item.pos().x(), node._item.pos().y()) for node in widget.GetNodes()}
    assert positions["far"] == (limit, 20.0)
    assert positions["neg"] == (-limit, -limit)
    assert positions["edge"] == (limit, -limit)
    assert positions["near"] == (100.0, 200.0)
    scene_rect = widget.view().scene().sceneRect()
    assert all(scene_rect.contains(node._item.sceneBoundingRect()) for node in widget.GetNodes())
    logged = " ".join(warnings_logged(caplog))
    # Nodes are named by title and position in the file ("far" is #2, "neg" #3, ...), not by id.
    assert "(#2)" in logged and "(#3)" in logged
    assert "(#4)" not in logged and "(#5)" not in logged
    assert "'far'" not in logged, "internal ids are not shown to the user"

    interactive = make_widget()
    dialogs.open_path = path
    interactive.button("Load").click()
    texts = warning_texts(dialogs)
    assert len(texts) == 1, dialogs.kinds()
    assert "(#2)" in texts[0] and "(#3)" in texts[0]
    assert "(#4)" not in texts[0] and "(#5)" not in texts[0]

    # clamped positions are saved as such: loading them again reports nothing
    resaved = str(tmp_path / "resaved.json")
    assert widget.SaveTree(resaved)
    caplog.clear()
    third = make_widget()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert third.LoadTree(resaved)
    assert warnings_logged(caplog) == []


# ============================================================================ deferred view positioning
@pytest.mark.parametrize("operation", ["NewTree", "LoadTree-without-view", "LoadTree-with-view"])
def test_view_positioning_waits_for_the_tree_view_to_be_shown(qtbot, make_widget, tmp_path, operation):
    widget = _hidden_tree_view_widget(qtbot, make_widget)
    if operation == "NewTree":
        assert widget.NewTree(str(tmp_path / "t.json"))
    elif operation == "LoadTree-without-view":
        assert widget.LoadTree(next_tree_file(tmp_path))
    else:
        path = write_tree(
            tmp_path / "view.json",
            [node_entry("r", "Root"), node_entry("w", "Succeed", 600, 450)],
            view={"zoom": 1.25, "center": [640.0, 480.0]},
        )
        assert widget.LoadTree(path)
    widget.setCurrentIndex(0)
    qtbot.waitUntil(widget.view().isVisible, timeout=2000)
    qtbot.wait(50)
    if operation == "LoadTree-with-view":
        center = widget.view().view_center()
        assert abs(center.x() - 640.0) <= 2 and abs(center.y() - 480.0) <= 2, center
        assert widget.view().zoom() == pytest.approx(1.25)
    else:
        assert abs(_root_top(widget) - 40) <= 2, (
            f"the root must be framed at the top of the now visible view (top at {_root_top(widget)} px)"
        )

@pytest.mark.parametrize("gesture", ["pan", "ctrl-wheel-zoom", "wheel-scroll"])
def test_user_navigation_cancels_the_pending_view_positioning(qtbot, make_widget, tmp_path, gesture):
    widget = _hidden_tree_view_widget(qtbot, make_widget)
    assert widget.NewTree(str(tmp_path / "t.json"))
    widget.setCurrentIndex(0)  # the view is shown; the re-centre is pending until the event loop runs
    view = widget.view()
    viewport = view.viewport()
    navigated = record(view.navigated)
    if gesture == "pan":
        start = QPoint(15, 15)
        QTest.mousePress(viewport, LEFT, CTRL, start)
        for step in range(1, 5):
            QTest.mouseMove(viewport, start + QPoint(10 * step, 25 * step))
        QTest.mouseRelease(viewport, LEFT, CTRL, start + QPoint(40, 100))
    else:
        pos = QPointF(200, 150)
        modifiers = Qt.KeyboardModifier.ControlModifier if gesture == "ctrl-wheel-zoom" else NO_MOD
        event = QWheelEvent(
            pos, QPointF(viewport.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(viewport, event)
    assert navigated, "precondition: the gesture counts as user navigation"
    top, zoom, center = _root_top(widget), view.zoom(), view.view_center()
    qtbot.wait(60)
    assert view.zoom() == pytest.approx(zoom)
    assert abs(_root_top(widget) - top) <= 1, "the view jumped back after the user panned / zoomed"
    after = view.view_center()
    assert abs(after.x() - center.x()) <= 1 and abs(after.y() - center.y()) <= 1


# ============================================================================ context menus
@pytest.mark.parametrize("which", ["running-leaf", "idle-composite", "root"])
def test_rename_from_menu_is_a_user_edit_while_executing(qtbot, bt, dialogs, executing, which):
    ns = executing
    node = {"running-leaf": ns.slow, "idle-composite": ns.selector, "root": ns.root}[which]
    bt._set_modified(False)
    flags = record(node.changed)
    dialogs.text_answer = ("Renamed While Running", True)
    dialogs.menu_handler = lambda menu: menu_action(menu, "Rename").trigger()
    menu = context_menu(bt, dialogs, title_point(bt, node))
    assert menu is not None and menu.objectName() == "NodeMenu"
    assert bt.IsExecuting()
    assert node.GetTitle() == "Renamed While Running"
    assert flags == [False], "a rename by the user emits changed(False) even while executing"
    assert bt.IsModified()

@pytest.mark.parametrize("when", ["while-open", "after-close"])
@pytest.mark.parametrize("kind", ["composite", "leaf"])
@pytest.mark.parametrize("removal", ["RemoveNode", "LoadTree", "NewTree"])
def test_stale_node_menu_actions_do_nothing(qtbot, bt, dialogs, tmp_path, removal, kind, when):
    root = bt.GetRootNode()
    node = bt.AddNode("Selector" if kind == "composite" else "Succeed", 200, 220)
    bt.Connect(root, node)
    bt.AddNode("Sequence", -200, 220)
    bt.view().centerOn(node._item.sceneBoundingRect().center())
    qtbot.wait(10)
    other = next_tree_file(tmp_path)
    save(bt)
    dialogs.text_answer = ("Renamed", True)
    outcome = {}

    def remove():
        if removal == "RemoveNode":
            bt.RemoveNode(node)
        elif removal == "LoadTree":
            assert bt.LoadTree(other)
        else:
            assert bt.NewTree(str(tmp_path / "fresh.json"))

    def trigger_all(menu):
        names = [action.objectName() for action in menu.actions() if action.objectName()]
        assert "Rename" in names and "Delete" in names
        if kind == "composite":
            assert {"Type_Sequence", "Type_Selector", "Memory"} <= set(names)
        before = (describe(bt), len(dialogs.shown))
        with capture_exceptions() as exceptions:
            for name in names:
                menu_action(menu, name).trigger()
        outcome.update(before=before, after=(describe(bt), len(dialogs.shown)), exceptions=exceptions)

    def handler(menu):
        if when == "while-open":
            remove()
            trigger_all(menu)

    dialogs.menu_handler = handler
    menu = context_menu(bt, dialogs, title_point(bt, node))
    assert menu is not None and menu.objectName() == "NodeMenu"
    if when == "after-close":
        remove()
        flush_deferred_deletes()
        if removal == "RemoveNode":
            assert not shiboken6.isValid(node)
        trigger_all(menu)
    assert outcome["exceptions"] == []
    assert outcome["after"] == outcome["before"], "a stale menu action changed the tree"
    assert "text_dialog" not in dialogs.kinds(), "Rename of a removed node must not ask for a title"


# ============================================================================ canvas: stale passthrough grab
@pytest.mark.parametrize("next_press", ["empty", "other-node"])
def test_press_after_lost_passthrough_release_releases_the_scene_grab(qtbot, active, next_press):
    bt = active
    view = bt.view()
    node = bt.AddNode("AllFields", 0, 220)
    other = bt.AddNode("NoFields", 320, 220)
    view.centerOn(node._item.sceneBoundingRect().center())
    qtbot.wait(10)
    viewport = view.viewport()
    point = viewport_point(bt, node, node.field_editor("label"))
    assert view.hit_test(point).kind == Hit.INTERACTIVE
    QTest.mousePress(viewport, LEFT, NO_MOD, point)
    assert view._gesture is _Gesture.PASSTHROUGH
    assert view.scene().mouseGrabberItem() is node._item, "precondition: the node holds the implicit scene grab"
    QTest.mouseRelease(view.parentWidget(), LEFT, NO_MOD, QPoint(2, 2))  # the release goes elsewhere
    assert view._gesture is _Gesture.PASSTHROUGH
    if next_press == "empty":
        start = empty_point(bt)
        center = view.view_center()
        QTest.mousePress(viewport, LEFT, CTRL, start)
        assert view.scene().mouseGrabberItem() is None, "the stale implicit grab must be released"
        assert view._gesture is _Gesture.PANNING
        for step in range(1, 5):
            QTest.mouseMove(viewport, start + QPoint(10 * step, 5 * step))
        QTest.mouseRelease(viewport, LEFT, CTRL, start + QPoint(40, 20))
        after = view.view_center()
        assert abs((center.x() - after.x()) - 40) <= 1 and abs((center.y() - after.y()) - 20) <= 1
    else:
        grip = viewport_point(bt, other, other._title_label)
        before = QPointF(other._item.pos())
        QTest.mousePress(viewport, LEFT, NO_MOD, grip)
        assert view.scene().mouseGrabberItem() is None
        assert view._gesture is _Gesture.MOVING_NODE
        for step in range(1, 5):
            QTest.mouseMove(viewport, grip + QPoint(0, 10 * step))
        QTest.mouseRelease(viewport, LEFT, NO_MOD, grip + QPoint(0, 40))
        moved = other._item.pos() - before
        assert abs(moved.x()) <= 1 and abs(moved.y() - 40) <= 1
    assert view._gesture is _Gesture.IDLE


# ============================================================================ canvas: presses outside the view
@pytest.mark.parametrize("via_window", [False, True], ids=["to-widget", "through-window"])
@pytest.mark.parametrize("target", list(OUTSIDE_TARGETS))
def test_press_outside_the_view_deselects_the_connection(qtbot, selected, target, via_window):
    ns = selected
    widget, point = OUTSIDE_TARGETS[target](ns.bt)
    if target in ("execution-frame-background", "tree-page-margin"):
        assert widget.childAt(point) is None, "precondition: the press hits the background"
    if via_window:
        window = ns.bt.window()
        handle, pos = window.windowHandle(), widget.mapTo(window, point)
        QTest.mousePress(handle, LEFT, NO_MOD, pos)
        assert ns.view.selected_connection() is None
        QTest.mouseRelease(handle, LEFT, NO_MOD, pos)
    else:
        QTest.mousePress(widget, LEFT, NO_MOD, point)
        assert ns.view.selected_connection() is None
        QTest.mouseRelease(widget, LEFT, NO_MOD, point)
    assert ns.view.selected_connection() is None
    assert not ns.item.is_selected()
    assert ns.leaf.GetParent() is ns.root, "deselecting never deletes the connection"
    assert ns.bt.currentIndex() == 0

def test_press_while_a_popup_is_open_keeps_the_selection(qtbot, selected):
    ns = selected
    widget, point = _frame_background(ns.bt)
    menu = QMenu(ns.bt)
    menu.addAction("something")
    menu.popup(ns.view.mapToGlobal(QPoint(30, 30)))
    try:
        qtbot.waitUntil(menu.isVisible, timeout=1000)
        assert QApplication.activePopupWidget() is menu
        QTest.mousePress(widget, LEFT, NO_MOD, point)
        QTest.mouseRelease(widget, LEFT, NO_MOD, point)
        assert ns.view.selected_connection() is ns.item, "a press while a popup is active must be ignored"
    finally:
        menu.close()
        qtbot.wait(10)
        menu.deleteLater()
    assert QApplication.activePopupWidget() is None
    QTest.mousePress(widget, LEFT, NO_MOD, point)
    QTest.mouseRelease(widget, LEFT, NO_MOD, point)
    assert ns.view.selected_connection() is None

def test_press_elsewhere_in_the_hosting_window_deselects(qtbot, tmp_path):
    """The widget embedded in an application window: a press on another widget of that window deselects."""
    host = QWidget()
    qtbot.addWidget(host)
    layout = QHBoxLayout(host)
    side = QLabel("side panel", host)
    side.setMinimumWidth(150)
    layout.addWidget(side)
    tree = BehaviorTreeWidget(host, node_types=[R3Probe])
    layout.addWidget(tree, 1)
    host.resize(1300, 800)
    host.show()
    qtbot.waitExposed(host)
    try:
        assert tree.window() is host
        assert tree.NewTree(str(tmp_path / "t.json"))
        leaf = tree.AddNode("R3Probe", 0, 220)
        tree.Connect(tree.GetRootNode(), leaf)
        view = tree.view()
        view.centerOn(80, 160)
        qtbot.wait(10)
        item = view.connection_item(leaf)
        QTest.mouseClick(view.viewport(), LEFT, NO_MOD, line_point(tree, item))
        assert view.selected_connection() is item
        QTest.mousePress(side, LEFT, NO_MOD, QPoint(5, 5))
        assert view.selected_connection() is None
        QTest.mouseRelease(side, LEFT, NO_MOD, QPoint(5, 5))
        assert leaf.GetParent() is tree.GetRootNode()
    finally:
        tree._set_modified(False)
        tree.Shutdown()

def test_outside_click_filter_is_owned_by_the_view(qtbot, tmp_path):
    from behavior_tree_widget.canvas import _OutsideClickFilter

    widget = BehaviorTreeWidget()
    widget.show()
    assert widget.NewTree(str(tmp_path / "t.json"))
    filters = widget.view().findChildren(_OutsideClickFilter)
    assert len(filters) == 1
    other = QWidget()
    qtbot.addWidget(other)
    button = QPushButton("elsewhere", other)
    other.show()
    widget._set_modified(False)
    widget.Shutdown()
    with capture_exceptions() as exceptions:
        shiboken6.delete(widget)
        QTest.mouseClick(button, LEFT)
        qtbot.wait(10)
    assert exceptions == []
    assert not shiboken6.isValid(filters[0]), "the application event filter must be deleted with its view"

def test_press_in_another_window_keeps_the_selection(qtbot, selected):
    ns = selected
    other = QWidget()
    qtbot.addWidget(other)
    button = QPushButton("elsewhere", other)
    other.show()
    qtbot.waitExposed(other)
    QTest.mousePress(button, LEFT, NO_MOD, QPoint(5, 5))
    QTest.mouseRelease(button, LEFT, NO_MOD, QPoint(5, 5))
    assert ns.view.selected_connection() is ns.item


# ============================================================================ event filter lifetime
def test_outside_click_filter_is_only_installed_while_a_connection_is_selected(widget):
    (quick,) = build(widget, Quick)
    view = widget.view()
    assert view._outside_filter_installed is False
    view.select_connection(view.connection_item(quick))
    assert view._outside_filter_installed is True
    view.select_connection(None)
    assert view._outside_filter_installed is False
    view.select_connection(view.connection_item(quick))
    view.hide()
    assert view._outside_filter_installed is False


# ============================================================================ view centre
def test_view_centre_does_not_drift_over_save_load_cycles(qtbot, widget, tmp_path):
    path = str(tmp_path / "centre_BT.json")
    widget.view().centerOn(QPointF(123.25, 456.75))
    widget.SaveTree(path)
    first = json.load(open(path, encoding="utf-8"))["view"]["center"]
    for _ in range(5):
        assert widget.LoadTree(path)
        qtbot.wait(5)
        widget.SaveTree(path)
    last = json.load(open(path, encoding="utf-8"))["view"]["center"]
    assert abs(first[0] - last[0]) < 0.51 and abs(first[1] - last[1]) < 0.51


# ============================================================================ display robustness
def test_view_center_with_a_zero_height_viewport(widget):
    view = widget.view()
    view.centerOn(QPointF(4000, -2500))
    before = view.view_center()
    view.resize(view.width(), 0)
    after = view.view_center()
    assert abs(after.x() - before.x()) < 600 and after != QPointF(0, 0)


# ============================================================================ canvas while executing
def test_connection_labels_do_not_move_nodes_while_executing(qtbot, widget):
    (endless,) = build(widget, Endless)
    widget.view().centerOn(100, 150)
    fast_config(widget)
    widget.Execute()
    before = endless._item.pos()
    start = label_point(widget, endless, PARENT)
    drag(widget, start, start + QPoint(120, 60))
    assert endless._item.pos() == before
    widget.Stop()

def test_order_badges_keep_the_running_order_until_execution_ends(qtbot, widget):
    first, second = build(widget, Endless, R5Quick)
    root = widget.GetRootNode()
    view = widget.view()
    fast_config(widget)
    widget.Execute()
    second._item.setPos(first._item.pos() - QPointF(700, 0))  # move the second child to the far left
    assert view.connection_item(first).order() == 1, "badges show the order of the running tree"
    assert view.connection_item(second).order() == 2
    widget.Stop()
    assert view.connection_item(second).order() == 1, "after execution the badges follow the positions"
    assert root.GetChildren()[0] is second

def test_outside_click_filter_is_restored_after_hide_and_show(widget):
    (quick,) = build(widget, R5Quick)
    view = widget.view()
    view.select_connection(view.connection_item(quick))
    view.hide()
    assert view._outside_filter_installed is False
    view.show()
    assert view._outside_filter_installed is True
