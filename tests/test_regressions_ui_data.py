"""Regression tests for the round-2 fix pass: canvas, nodes, blackboard, widget / files.

Every test asserts the *intended* behaviour described in the fix-pass notes
(CHANGES_ROUND2.md, sections "Canvas", "Nodes", "Blackboard" and "Widget / files")
and in instructions.txt. The scenarios reproduce the defects found by the first
verification round:

* gestures (move / pan / connect) whose mouse release was lost, e.g. because a
  modal dialog appeared, must end as soon as the mouse shows no button is held
  (or the window loses activation); hovering must not move / pan / connect and
  the next click is handled normally;
* a selected connection line is deselected by any press elsewhere (any button),
  by node / empty-space context menus and by focus moving to another widget,
  but not by a context menu popup taking focus or by window deactivation;
* ``_title`` / ``_fields`` assigned in a subclass ``__init__`` (before or after
  ``super().__init__()``) are shown and saved / loaded consistently;
* worker-thread ``SetField`` calls that change the value kind never raise;
* unknown node placeholders, root titles, node type registration, SetConfig
  validation, window-title escaping, BOM files, unsaveable values;
* blackboard Object fallback, dispose, retiring rows with a focused name editor,
  the container Edit dialog, spin box typing, snapshot restore order;
* Load / New replacing the blackboard, validation before touching the current
  tree and restoring the saved view centre.

Modal UI never blocks: the autouse ``dialogs`` fixture replaces message boxes,
file / input dialogs, ``_run_dialog`` and ``_exec_menu``. Where a scenario needs
a real modal window during a gesture it is *shown* (never ``exec()``-ed) and the
mouse events are sent through the QWindow so Qt's modal blocking applies.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from types import SimpleNamespace

import py_trees
import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSpinBox,
    QToolTip,
    QWidget,
)
from pytestqt.exceptions import capture_exceptions

from behavior_tree_widget import (
    BehaviorTreeWidget,
    CompositeNodeWidget,
    LeafNodeWidget,
    NodeStatus,
    TreeFileError,
    UnknownLeafNodeWidget,
    register_node_type,
)
from behavior_tree_widget import widget as widget_module
from behavior_tree_widget.blackboard import EntryRow, literal_eval_extended, parse_container_text
from behavior_tree_widget.canvas import ConnectionItem, Hit, _Gesture
from behavior_tree_widget.demo import DEMO_NODE_TYPES, DemoWindow
from behavior_tree_widget.nodes import CHILDREN, PARENT, ConnectionState
from behavior_tree_widget.serialization import (
    FORMAT_NAME,
    FORMAT_VERSION,
    decode_value,
    encode_value,
    is_encodable,
    write_json_atomic,
)

from conftest import TEST_NODE_TYPES, SlowCancelable, fast_config, label_point, viewport_point

LEFT = Qt.MouseButton.LeftButton
RIGHT = Qt.MouseButton.RightButton
MIDDLE = Qt.MouseButton.MiddleButton
NO_MOD = Qt.KeyboardModifier.NoModifier
LOGGER = "behavior_tree_widget"


# ============================================================================ node types used here
class PreInitNode(LeafNodeWidget):
    """Assigns instance-level _title / _fields *before* super().__init__()."""

    _title = "Class Title"
    _fields = {"class_field": 1}
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        self._title = "Pre Title"
        self._fields = {"speed": 7, "name": "pre", "mode": ["a", "b", "c"]}
        super().__init__(parent)

    def OnRun(self, tree):
        return True


class PostInitNode(LeafNodeWidget):
    """Assigns instance-level _title / _fields *after* super().__init__()."""

    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._title = "Post Title"
        self._fields = {"ratio": 2.5, "enabled": False, "items": [1, 2, 3]}

    def OnRun(self, tree):
        return True


class PostInitEmptyNode(LeafNodeWidget):
    """The class declares fields; the instance removes them after super().__init__()."""

    _title = "Post Empty"
    _fields = {"x": 1, "y": 2}
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fields = {}

    def OnRun(self, tree):
        return True


class PreInitEmptyNode(LeafNodeWidget):
    """The class declares fields; the instance assigns an empty dict before super().__init__()."""

    _title = "Pre Empty"
    _fields = {"x": 1}
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        self._fields = {}
        super().__init__(parent)

    def OnRun(self, tree):
        return True


class KindSwitcher(LeafNodeWidget):
    """Worker-thread leaf: sets field "value" twice (same kind, then another kind)."""

    _title = "Kind Switcher"
    _fields = {"value": 1}

    def OnRun(self, tree):
        self.SetField("value", 2)
        self.SetField("value", "done")
        return True


class OrderShuffler(LeafNodeWidget):
    """GUI-thread leaf: re-creates entry "a" (moving it last) and adds entry "d"."""

    _title = "Order Shuffler"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        value = tree.GetEntry("a")
        tree.RemoveEntry("a")
        tree.SetEntry(value + 100, "a")
        tree.SetEntry(4, "d")
        return True


class BadLoadNode(LeafNodeWidget):
    """Its _load_dict fails for saved data carrying {"broken": true}."""

    _title = "Bad Load"
    _fields = {"n": 1}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True

    def _load_dict(self, data):
        if data.get("broken"):
            raise ValueError("cannot restore this node")
        super()._load_dict(data)


class TickAction(LeafNodeWidget):
    """GUI-thread leaf running ``TickAction.action(tree)`` (set by the test) inside a tick."""

    _title = "Tick Action"
    RUN_IN_THREAD = False
    action = None
    outcome: dict = {}

    def OnRun(self, tree):
        action, TickAction.action = TickAction.action, None
        if action is not None:
            try:
                TickAction.outcome["result"] = action(tree)
            except Exception as error:  # noqa: BLE001 - reported to the test
                TickAction.outcome["error"] = error
        return True


class BaseNamed(LeafNodeWidget):
    TYPE_NAME = "PublicBaseName"
    _title = "Base Named"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class DerivedNamed(BaseNamed):
    """Inherits TYPE_NAME from BaseNamed; must be registered / saved as "DerivedNamed"."""

    _title = "Derived Named"


class NotRegisteredUpFront(LeafNodeWidget):
    """Only made known to the widget through AddNode(instance) / AddNode(cls)."""

    _title = "Late Registered"
    _fields = {"k": 5}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class AlsoNotRegistered(LeafNodeWidget):
    _title = "Also Late"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class Unreprable:
    """A value that can neither be copied nor saved."""

    def __deepcopy__(self, memo):
        raise TypeError("Unreprable objects cannot be copied")

    def __repr__(self):
        return "<Unreprable>"


LOCAL_TYPES = [
    PreInitNode, PostInitNode, PostInitEmptyNode, PreInitEmptyNode, KindSwitcher, OrderShuffler, BadLoadNode,
    TickAction, BaseNamed, DerivedNamed,
]
ALL_TYPES = list(DEMO_NODE_TYPES) + list(TEST_NODE_TYPES) + LOCAL_TYPES


def make_leaf(name: str, **attrs) -> type[LeafNodeWidget]:
    namespace = {"RUN_IN_THREAD": False, "OnRun": lambda self, tree: True, "__module__": __name__}
    namespace.update(attrs)
    return type(name, (LeafNodeWidget,), namespace)


# ============================================================================ fixtures
@pytest.fixture
def lbt(make_widget, tmp_path):
    """A shown widget with a new tree that also knows this module's node types."""
    widget = make_widget(node_types=ALL_TYPES)
    assert widget.NewTree(str(tmp_path / "tree.json"))
    return widget


@pytest.fixture
def active(qtbot, bt):
    """``bt`` as the active window (focus events are only delivered to the active window)."""
    bt.activateWindow()
    qtbot.waitUntil(bt.isActiveWindow, timeout=2000)
    return bt


@pytest.fixture
def clean_registry():
    """Empty the global node type registry for the test and restore it afterwards."""
    registry = widget_module._global_node_types
    saved = dict(registry)
    registry.clear()
    try:
        yield registry
    finally:
        registry.clear()
        registry.update(saved)


@pytest.fixture
def error_records(caplog):
    """Callable returning the ERROR records logged by the library during the test."""
    caplog.set_level(logging.WARNING, logger=LOGGER)
    return lambda: [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.fixture
def executing(qtbot, bt):
    """Root -> SlowCancelable (running on a worker thread) plus an unconnected Succeed leaf."""
    SlowCancelable.cancelled.clear()
    root = bt.GetRootNode()
    slow = bt.AddNode("SlowCancelable", -150, 200)
    other = bt.AddNode("Succeed", 150, 200)
    bt.Connect(root, slow)
    bt.view().centerOn(0, 150)
    fast_config(bt)
    bt.Execute()
    try:
        qtbot.waitUntil(lambda: slow.GetStatus() is NodeStatus.RUNNING, timeout=3000)
        yield SimpleNamespace(root=root, slow=slow, other=other, view=bt.view())
    finally:
        bt.Stop()


# ============================================================================ helpers: mouse
class Mouse:
    """Sends mouse events either to the view's viewport or through the top-level QWindow.

    Events sent through the QWindow take Qt's real path (QWidgetWindow), so modal
    windows block them exactly as they would block a user's mouse.
    """

    def __init__(self, bt: BehaviorTreeWidget, via_window: bool):
        self.bt = bt
        self.viewport = bt.view().viewport()
        self.via_window = via_window

    def _target(self, point: QPoint):
        if self.via_window:
            window = self.bt.window()
            return window.windowHandle(), self.viewport.mapTo(window, point)
        return self.viewport, point

    def press(self, point: QPoint, button=LEFT) -> None:
        target, pos = self._target(point)
        QTest.mousePress(target, button, NO_MOD, pos)

    def move(self, point: QPoint) -> None:
        target, pos = self._target(point)
        QTest.mouseMove(target, pos)

    def release(self, point: QPoint, button=LEFT) -> None:
        target, pos = self._target(point)
        QTest.mouseRelease(target, button, NO_MOD, pos)

    def drag(self, start: QPoint, end: QPoint, steps: int = 4, button=LEFT) -> None:
        self.press(start, button)
        for step in range(1, steps + 1):
            point = start + (end - start) * (step / steps)
            self.move(QPoint(round(point.x()), round(point.y())))
        self.release(end, button)

    def click(self, point: QPoint, button=LEFT) -> None:
        self.press(point, button)
        self.release(point, button)


class EventCounter(QObject):
    """Counts events of the given types delivered to the watched object."""

    def __init__(self, *types):
        super().__init__()
        self.types = set(types)
        self.count = 0

    def eventFilter(self, watched, event):  # noqa: N802 (Qt API)
        if event.type() in self.types:
            self.count += 1
        return False


SCENARIOS = ["modal_dialog", "release_elsewhere"]


def lose_release(qtbot, bt: BehaviorTreeWidget, mouse: Mouse, point: QPoint, scenario: str) -> None:
    """Release the left button without the view ever receiving the release.

    * ``modal_dialog``: an application-modal dialog appears during the gesture (e.g.
      shown by a slot of executionFinished); the release is sent through the window
      while the dialog is open, so Qt blocks it. The dialog is then closed and the
      tree window activated again.
    * ``release_elsewhere``: the release is delivered to another widget while the
      window stays active (e.g. consumed by a nested event loop), so only the next
      mouse event can reveal that the button is no longer held.
    """
    viewport = bt.view().viewport()
    releases = EventCounter(QEvent.Type.MouseButtonRelease)
    viewport.installEventFilter(releases)
    try:
        if scenario == "modal_dialog":
            dialog = QDialog(bt.window())
            dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            QLabel("Execution finished", dialog)
            dialog.show()
            qtbot.waitUntil(lambda: QApplication.activeModalWidget() is dialog, timeout=2000)
            mouse.release(point)  # blocked by the modal dialog
            dialog.close()
            dialog.deleteLater()
            bt.window().activateWindow()
            qtbot.waitUntil(bt.window().isActiveWindow, timeout=2000)
        else:
            sink = bt.view().parentWidget()  # FrameView: a plain frame that ignores releases
            QTest.mouseRelease(sink, LEFT, NO_MOD, QPoint(2, 2))
            assert bt.window().isActiveWindow(), "precondition: the window stays active"
    finally:
        viewport.removeEventFilter(releases)
    assert releases.count == 0, "precondition: the view never saw the release"


def title_point(bt, node) -> QPoint:
    return viewport_point(bt, node, node._title_label)


def line_point(bt, item: ConnectionItem, percent: float = 0.5) -> QPoint:
    return bt.view().mapFromScene(item.path().pointAtPercent(percent))


def empty_point(bt) -> QPoint:
    point = QPoint(15, 15)
    assert bt.view().hit_test(point).kind == Hit.EMPTY
    return point


def assert_close(a: QPointF, b: QPointF, tolerance: float = 1.0) -> None:
    assert abs(a.x() - b.x()) <= tolerance and abs(a.y() - b.y()) <= tolerance, f"{a} != {b}"


def context_menu(bt, dialogs, point: QPoint):
    """Right click (context menu event) at viewport ``point``; returns the menu shown or None."""
    before = len(dialogs.menus)
    viewport = bt.view().viewport()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point))
    QApplication.sendEvent(viewport, event)
    return dialogs.menus[-1] if len(dialogs.menus) > before else None


def menu_action(menu, name: str):
    for candidate in menu.actions():
        if candidate.objectName() == name:
            return candidate
    raise AssertionError(f"menu has no action {name!r}")


# ============================================================================ helpers: files
def node_entry(node_id, type_name, x=0.0, y=0.0, **extra) -> dict:
    entry = {"id": node_id, "type": type_name, "title": extra.pop("title", type_name), "x": x, "y": y}
    entry.update(extra)
    return entry


def tree_document(nodes, connections=(), blackboard=(), **extra) -> dict:
    data = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "nodes": list(nodes),
        "connections": list(connections),
        "blackboard": list(blackboard),
    }
    data.update(extra)
    return data


def write_tree(path, nodes, connections=(), blackboard=(), encoding="utf-8", **extra) -> str:
    text = json.dumps(tree_document(nodes, connections, blackboard, **extra), ensure_ascii=False)
    path.write_text(text, encoding=encoding)
    return str(path)


def read_json(path) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def saved_node(data: dict, node_id: str) -> dict:
    matches = [node for node in data["nodes"] if node["id"] == node_id]
    assert len(matches) == 1
    return matches[0]


def node_by_id(widget, node_id):
    matches = [node for node in widget.GetNodes() if node.GetId() == node_id]
    assert len(matches) == 1, f"expected one node {node_id!r}, found {len(matches)}"
    return matches[0]


def describe(widget) -> dict:
    """Everything about the current tree that a failed load must leave untouched."""
    nodes = {}
    for node in widget.GetNodes():
        pos = node._item.pos()
        info = {
            "class": type(node).__name__,
            "type": node.GetTypeName(),
            "title": node.GetTitle(),
            "label": node._title_label.text(),
            "pos": (round(pos.x(), 2), round(pos.y(), 2)),
            "parent": node.GetParent().GetId() if node.GetParent() is not None else None,
            "children": [child.GetId() for child in node.GetChildren()],
        }
        if isinstance(node, CompositeNodeWidget):
            info["composite"] = (node.GetCompositeType(), node.GetMemory())
        if isinstance(node, LeafNodeWidget):
            info["fields"] = node.GetFields()
        nodes[node.GetId()] = info
    return {
        "nodes": nodes,
        "blackboard": [(name, widget.GetEntryType(name), widget.GetEntry(name)) for name in widget.GetEntryNames()],
        "rows": [row.name for row in widget.blackboardView().rows()],
        "file": widget.GetFilePath(),
        "modified": widget.IsModified(),
        "title": widget.window().windowTitle(),
        "config": widget.GetConfig(),
    }


def view_rows_in_layout(view) -> list[str]:
    layout = view._rows_layout
    names = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if isinstance(widget, EntryRow):
            names.append(widget.name)
    return names


def assert_view_matches_store(qtbot, bt) -> None:
    """Every entry has exactly one visible, current row, in store order; no stale rows remain."""
    qtbot.wait(20)  # let deleteLater() of retired rows run
    view = bt.blackboardView()
    names = bt.GetEntryNames()
    assert view_rows_in_layout(view) == names
    for name in names:
        row = view.row(name)
        assert row is not None, f"no row for entry {name!r}"
        assert row.type_name == bt.GetEntryType(name)
        assert row.name_edit.text() == name
        if view.isVisible():
            assert row.isVisible(), f"the row of {name!r} is hidden"
    contents = view.entry_list.widget()
    stale = [row for row in contents.findChildren(EntryRow) if shiboken6.isValid(row) and view.row(row.name) is not row]
    assert stale == [], f"stale rows left behind: {[row.objectName() for row in stale]}"


# ============================================================================ Canvas: lost mouse release
@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("hover_first", [True, False], ids=["hover", "no_hover"])
def test_lost_release_while_moving_node(qtbot, active, scenario, hover_first):
    bt = active
    view = bt.view()
    leaf = bt.AddNode("NoFields", 0, 200)
    other = bt.AddNode("Succeed", 320, 200)
    view.centerOn(150, 200)
    qtbot.wait(10)
    assert bt.SaveTree(bt.GetFilePath()) and not bt.IsModified()
    mouse = Mouse(bt, via_window=scenario == "modal_dialog")
    start = title_point(bt, leaf)
    original = QPointF(leaf._item.pos())

    mouse.press(start)
    mouse.move(start + QPoint(20, 10))
    mouse.move(start + QPoint(40, 20))
    moved = QPointF(leaf._item.pos())
    assert_close(moved - original, QPointF(40, 20))
    lose_release(qtbot, bt, mouse, start + QPoint(40, 20), scenario)

    if hover_first:
        for delta in (QPoint(90, 60), QPoint(160, 90), QPoint(200, 120)):
            mouse.move(start + delta)
        assert leaf._item.pos() == moved, "hovering without a button must not move the node"
        assert view._gesture is _Gesture.IDLE
        assert bt.IsModified(), "the move made before the release was lost still counts as an edit"

    # The next press-drag is handled normally: dragging the other node moves only that node.
    other_start = title_point(bt, other)
    other_before = QPointF(other._item.pos())
    mouse.drag(other_start, other_start + QPoint(-30, 40))
    assert_close(other._item.pos() - other_before, QPointF(-30, 40))
    assert leaf._item.pos() == moved
    assert view._gesture is _Gesture.IDLE
    assert bt.IsModified()


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("hover_first", [True, False], ids=["hover", "no_hover"])
def test_lost_release_while_panning(qtbot, active, scenario, hover_first):
    bt = active
    view = bt.view()
    leaf = bt.AddNode("NoFields", 0, 200)
    view.centerOn(100, 200)
    qtbot.wait(10)
    mouse = Mouse(bt, via_window=scenario == "modal_dialog")
    start = empty_point(bt)
    center = view.view_center()

    mouse.press(start)
    mouse.move(start + QPoint(10, 5))
    mouse.move(start + QPoint(20, 10))
    assert_close(view.view_center() - center, QPointF(-20, -10))
    lose_release(qtbot, bt, mouse, start + QPoint(20, 10), scenario)
    panned = view.view_center()

    if hover_first:
        for delta in (QPoint(60, 40), QPoint(120, 80)):
            mouse.move(start + delta)
        assert_close(view.view_center(), panned, 0.01)
        assert view._gesture is _Gesture.IDLE

    # The next press on a node moves the node instead of continuing the pan.
    grip = title_point(bt, leaf)
    before = QPointF(leaf._item.pos())
    mouse.drag(grip, grip + QPoint(50, 30))
    assert_close(leaf._item.pos() - before, QPointF(50, 30))
    assert_close(view.view_center(), panned, 0.01)
    assert view._gesture is _Gesture.IDLE


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("hover_first", [True, False], ids=["hover", "no_hover"])
def test_lost_release_while_connecting(qtbot, active, scenario, hover_first):
    bt = active
    view = bt.view()
    root = bt.GetRootNode()
    a = bt.AddNode("NoFields", -160, 260)
    b = bt.AddNode("NoFields", 160, 260)
    view.centerOn(40, 200)
    qtbot.wait(10)
    mouse = Mouse(bt, via_window=scenario == "modal_dialog")
    start = label_point(bt, root, CHILDREN)

    mouse.press(start)
    mouse.move(start + QPoint(0, 30))
    mouse.move(start + QPoint(0, 60))
    assert view.is_connecting() and view.drag_line() is not None
    lose_release(qtbot, bt, mouse, start + QPoint(0, 60), scenario)

    target = label_point(bt, b, PARENT)
    if hover_first:
        mouse.move(target + QPoint(-30, -30))
        mouse.move(target)
        assert not view.is_connecting()
        assert view.drag_line() is None, "the drag line must disappear once the button is known to be up"
        assert b.connection_state(PARENT) is ConnectionState.DISCONNECTED, "hovering must not highlight a target"
        assert root.connection_state(CHILDREN) is ConnectionState.DISCONNECTED

    # A plain click on b's ParentConnection label must not complete the lost connection.
    mouse.click(target)
    assert b.GetParent() is None and a.GetParent() is None
    assert root.GetChildren() == []
    assert view.connections() == []
    assert view.drag_line() is None and not view.is_connecting()
    assert b.connection_state(PARENT) is ConnectionState.DISCONNECTED

    # A new, complete drag connects normally.
    mouse.drag(start, label_point(bt, a, PARENT), steps=6)
    assert a.GetParent() is root
    assert b.GetParent() is None
    assert root.connection_state(CHILDREN) is ConnectionState.CONNECTED


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("field", ["count", "ratio", "label", "enabled"])
def test_lost_release_on_field_editor_does_not_swallow_next_click(qtbot, active, scenario, field):
    """A press on a field editor passes through to the widget; if its release is lost, the next
    press-drag on empty space must still pan ("the next click must be handled normally")."""
    bt = active
    view = bt.view()
    node = bt.AddNode("AllFields", 0, 200)
    view.centerOn(node._item.sceneBoundingRect().center())
    qtbot.wait(10)
    mouse = Mouse(bt, via_window=scenario == "modal_dialog")
    point = viewport_point(bt, node, node.field_editor(field))
    assert view.hit_test(point).kind == Hit.INTERACTIVE
    fields = node.GetFields()
    mouse.press(point)
    lose_release(qtbot, bt, mouse, point, scenario)

    start = empty_point(bt)
    center = view.view_center()
    mouse.drag(start, start + QPoint(40, 30))
    assert_close(view.view_center() - center, QPointF(-40, -30))
    assert node.GetFields() == fields
    assert view._gesture is _Gesture.IDLE


def test_window_deactivation_ends_a_node_move(qtbot, active):
    """Losing window activation during a gesture ends it at once (a release may never come)."""
    bt = active
    leaf = bt.AddNode("NoFields", 0, 200)
    bt.view().centerOn(40, 200)
    qtbot.wait(10)
    mouse = Mouse(bt, via_window=False)
    start = title_point(bt, leaf)
    mouse.press(start)
    mouse.move(start + QPoint(25, 0))
    moved = QPointF(leaf._item.pos())
    other = QWidget()
    qtbot.addWidget(other)
    other.show()
    other.activateWindow()
    qtbot.waitUntil(other.isActiveWindow, timeout=2000)
    assert bt.view()._gesture is _Gesture.IDLE
    mouse.release(start + QPoint(25, 0))  # a late release is harmless
    assert leaf._item.pos() == moved


# ============================================================================ Canvas: deselecting lines
@pytest.fixture
def selected(qtbot, active):
    """Root -> Succeed connected; the line is selected by a left click (the view has focus)."""
    bt = active
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", 0, 220)
    fields = bt.AddNode("AllFields", 260, 220)
    bt.Connect(root, leaf)
    view = bt.view()
    view.centerOn(80, 160)
    qtbot.wait(10)
    item = view.connection_item(leaf)
    QTest.mouseClick(view.viewport(), LEFT, NO_MOD, line_point(bt, item))
    assert view.selected_connection() is item
    assert view.hasFocus()
    return SimpleNamespace(bt=bt, root=root, leaf=leaf, fields=fields, view=view, item=item)


def assert_deselected(ns) -> None:
    assert ns.view.selected_connection() is None
    assert not ns.item.is_selected()
    assert ns.leaf.GetParent() is ns.root  # deselecting never deletes


@pytest.mark.parametrize("where", ["node_title", "parent_label", "empty"])
def test_right_press_deselects_line(selected, where):
    ns = selected
    point = {
        "node_title": lambda: title_point(ns.bt, ns.fields),
        "parent_label": lambda: label_point(ns.bt, ns.fields, PARENT),
        "empty": lambda: empty_point(ns.bt),
    }[where]()
    QTest.mousePress(ns.view.viewport(), RIGHT, NO_MOD, point)
    assert_deselected(ns)
    QTest.mouseRelease(ns.view.viewport(), RIGHT, NO_MOD, point)
    assert_deselected(ns)


@pytest.mark.parametrize("where", ["node", "empty"])
def test_right_click_context_menu_on_node_or_empty_space_deselects_line(selected, dialogs, where):
    ns = selected
    point = title_point(ns.bt, ns.leaf) if where == "node" else empty_point(ns.bt)
    QTest.mousePress(ns.view.viewport(), RIGHT, NO_MOD, point)
    menu = context_menu(ns.bt, dialogs, point)
    QTest.mouseRelease(ns.view.viewport(), RIGHT, NO_MOD, point)
    assert menu is not None and menu.objectName() == ("NodeMenu" if where == "node" else "AddNodeMenu")
    assert_deselected(ns)


def test_context_menu_event_alone_on_node_deselects_line(selected, dialogs):
    ns = selected
    seen = []
    dialogs.menu_handler = lambda menu: seen.append(ns.view.selected_connection())
    menu = context_menu(ns.bt, dialogs, title_point(ns.bt, ns.root))
    assert menu is not None and menu.objectName() == "NodeMenu"
    assert seen == [None], "the line must already be deselected while the node menu is open"
    assert_deselected(ns)


@pytest.mark.parametrize("where", ["node_title", "field_editor", "empty"])
def test_middle_press_deselects_line(selected, where):
    ns = selected
    point = {
        "node_title": lambda: title_point(ns.bt, ns.fields),
        "field_editor": lambda: viewport_point(ns.bt, ns.fields, ns.fields.field_editor("label")),
        "empty": lambda: empty_point(ns.bt),
    }[where]()
    QTest.mousePress(ns.view.viewport(), MIDDLE, NO_MOD, point)
    assert_deselected(ns)
    QTest.mouseRelease(ns.view.viewport(), MIDDLE, NO_MOD, point)
    assert_deselected(ns)


@pytest.mark.parametrize("how", ["click", "set_focus"])
def test_focus_moving_to_another_widget_deselects_line(qtbot, selected, how):
    ns = selected
    button = ns.bt.button("Reset")
    if how == "click":
        QTest.mouseClick(button, LEFT)
    else:
        button.setFocus(Qt.FocusReason.MouseFocusReason)
    qtbot.waitUntil(lambda: not ns.view.hasFocus(), timeout=1000)
    assert_deselected(ns)


def test_context_menu_popup_taking_focus_keeps_line_selected(qtbot, selected, dialogs):
    """Right-clicking the selected line opens its menu; the popup takes focus but the line stays selected."""
    ns = selected
    during = {}

    def show_popup(menu):
        menu.popup(ns.view.viewport().mapToGlobal(line_point(ns.bt, ns.item)))
        qtbot.waitUntil(menu.isVisible, timeout=1000)
        qtbot.wait(10)
        during["selected"] = ns.view.selected_connection()
        during["focus"] = QApplication.focusWidget()
        menu.close()
        qtbot.wait(10)

    dialogs.menu_handler = show_popup
    menu = context_menu(ns.bt, dialogs, line_point(ns.bt, ns.item))
    assert menu is not None and menu.objectName() == "ConnectionMenu"
    assert during["selected"] is ns.item, "a popup taking focus must not deselect the line"
    assert ns.view.selected_connection() is ns.item
    # the selection is still usable: Delete removes the connection
    ns.view.setFocus()
    QTest.keyClick(ns.view, Qt.Key.Key_Delete)
    assert ns.leaf.GetParent() is None
    assert ns.view.connections() == []


def test_unrelated_popup_taking_focus_keeps_line_selected(qtbot, selected):
    ns = selected
    menu = QMenu(ns.view)
    menu.addAction("something")
    menu.popup(ns.view.mapToGlobal(QPoint(20, 20)))
    qtbot.waitUntil(menu.isVisible, timeout=1000)
    assert ns.view.selected_connection() is ns.item
    menu.close()
    qtbot.wait(10)
    assert ns.view.selected_connection() is ns.item
    menu.deleteLater()


def test_window_deactivation_keeps_line_selected(qtbot, selected):
    ns = selected
    other = QWidget()
    qtbot.addWidget(other)
    other.show()
    other.activateWindow()
    qtbot.waitUntil(other.isActiveWindow, timeout=2000)
    assert ns.view.selected_connection() is ns.item, "window deactivation must not deselect"
    ns.bt.activateWindow()
    qtbot.waitUntil(ns.bt.isActiveWindow, timeout=2000)
    assert ns.view.selected_connection() is ns.item
    ns.view.setFocus()
    QTest.keyClick(ns.view, Qt.Key.Key_Delete)
    assert ns.leaf.GetParent() is None


def test_press_on_line_selects_it_and_dragging_from_it_pans(qtbot, active):
    bt = active
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", 40, 240)
    bt.Connect(root, leaf)
    view = bt.view()
    view.centerOn(60, 160)
    qtbot.wait(10)
    item = view.connection_item(leaf)
    start = line_point(bt, item, 0.4)
    assert view.hit_test(start).kind == Hit.CONNECTION
    center = view.view_center()
    leaf_pos, root_pos = QPointF(leaf._item.pos()), QPointF(root._item.pos())

    QTest.mousePress(view.viewport(), LEFT, NO_MOD, start)
    assert view.selected_connection() is item, "pressing on a line selects it"
    for step in range(1, 5):
        QTest.mouseMove(view.viewport(), start + QPoint(20 * step, 10 * step))
    QTest.mouseRelease(view.viewport(), LEFT, NO_MOD, start + QPoint(80, 40))

    assert_close(view.view_center() - center, QPointF(-80, -40))
    assert view.selected_connection() is item and item.is_selected()
    assert leaf._item.pos() == leaf_pos and root._item.pos() == root_pos
    assert leaf.GetParent() is root


# ============================================================================ Canvas: structure lock / rename while executing
def _structure_operations(ns, bt):
    root, slow, other, view = ns.root, ns.slow, ns.other, ns.view
    return {
        "view.add_node": lambda: view.add_node(ns.spare, QPointF(0, 500)),
        "view.remove_node": lambda: view.remove_node(other),
        "view.connect_nodes": lambda: view.connect_nodes(root, other),
        "view.disconnect_node": lambda: view.disconnect_node(slow),
        "AddNode(name)": lambda: bt.AddNode("Succeed", 0, 500),
        "AddNode(cls)": lambda: bt.AddNode(NotRegisteredUpFront, 0, 500),
        "AddNode(instance)": lambda: bt.AddNode(ns.spare, 0, 500),
        "RemoveNode": lambda: bt.RemoveNode(other),
        "Connect": lambda: bt.Connect(root, other),
        "Disconnect": lambda: bt.Disconnect(slow),
        "SetParent(parent)": lambda: other.SetParent(root),
        "SetParent(None)": lambda: slow.SetParent(None),
        "AddChild": lambda: root.AddChild(other),
        "RemoveChild": lambda: root.RemoveChild(slow),
    }


STRUCTURE_OPERATIONS = [
    "view.add_node", "view.remove_node", "view.connect_nodes", "view.disconnect_node", "AddNode(name)",
    "AddNode(cls)", "AddNode(instance)", "RemoveNode", "Connect", "Disconnect", "SetParent(parent)",
    "SetParent(None)", "AddChild", "RemoveChild",
]


@pytest.mark.parametrize("paused", [False, True], ids=["running", "paused"])
@pytest.mark.parametrize("operation", STRUCTURE_OPERATIONS)
def test_structure_api_raises_while_executing(qtbot, bt, executing, operation, paused):
    ns = executing
    ns.spare = NotRegisteredUpFront()
    if paused:
        bt.Pause()
        assert bt.GetExecutionState() == "Paused"
    before = describe(bt)["nodes"]
    try:
        with pytest.raises(RuntimeError):
            _structure_operations(ns, bt)[operation]()
        assert describe(bt)["nodes"] == before
        assert ns.slow.GetParent() is ns.root and ns.other.GetParent() is None
    finally:
        if ns.spare._item is None:
            ns.spare.deleteLater()


def test_rename_from_context_menu_marks_modified_while_executing(qtbot, bt, dialogs, executing):
    ns = executing
    bt._set_modified(False)
    dialogs.text_answer = ("Renamed While Running", True)
    dialogs.menu_handler = lambda menu: menu_action(menu, "Rename").trigger()
    menu = context_menu(bt, dialogs, title_point(bt, ns.slow))
    assert menu is not None and menu.objectName() == "NodeMenu"
    assert ns.slow.GetTitle() == "Renamed While Running"
    assert bt.IsModified(), "a rename by the user is an edit even while the tree executes"


def test_programmatic_title_change_while_executing_is_a_runtime_change(qtbot, bt, executing):
    ns = executing
    bt._set_modified(False)
    events = []
    ns.other.changed.connect(events.append)
    ns.other.SetTitle("Changed By Code")
    assert events == [True]
    assert not bt.IsModified()


# ============================================================================ Nodes: _title / _fields in subclass __init__
def editor_kinds(node) -> dict:
    return {key: type(node.field_editor(key)).__name__ for key in node.GetFields()}


def test_title_and_fields_assigned_before_super_init_are_kept_and_shown(lbt):
    node = lbt.AddNode("PreInitNode", 0, 200)
    assert node.GetTitle() == "Pre Title"
    assert node._title_label.text() == "Pre Title"
    assert node.GetFields() == {"speed": 7, "name": "pre", "mode": ["a", "b", "c"]}
    assert editor_kinds(node) == {"speed": "QSpinBox", "name": "QLineEdit", "mode": "QComboBox"}
    assert node.field_editor("class_field") is None
    assert node.field_editor("speed").value() == 7
    assert node.field_editor("name").text() == "pre"
    assert node._fields_frame.isVisibleTo(node)


def test_title_and_fields_assigned_after_super_init_are_shown(lbt):
    node = lbt.AddNode("PostInitNode", 0, 200)
    assert node.GetTitle() == "Post Title"
    assert node._title_label.text() == "Post Title"
    assert node.GetFields() == {"ratio": 2.5, "enabled": False, "items": [1, 2, 3]}
    assert editor_kinds(node) == {"ratio": "QDoubleSpinBox", "enabled": "QCheckBox", "items": "QComboBox"}
    assert node.field_editor("ratio").value() == 2.5
    combo = node.field_editor("items")
    assert [combo.itemText(i) for i in range(combo.count())] == ["1", "2", "3"]
    assert node._fields_frame.isVisibleTo(node)
    # the node grew to show the fields
    empty = lbt.AddNode("NoFields", 300, 200)
    assert node.height() > empty.height()


@pytest.mark.parametrize("type_name", ["PostInitEmptyNode", "PreInitEmptyNode"])
def test_empty_fields_assigned_in_init_hide_frame_fields(lbt, type_name):
    node = lbt.AddNode(type_name, 0, 200)
    assert node.GetFields() == {}
    assert not node._fields_frame.isVisibleTo(node)
    assert node._editors == {}


def test_editor_edits_update_instance_fields(lbt):
    node = lbt.AddNode("PostInitNode", 0, 200)
    node.field_editor("ratio").setValue(4.25)
    node.field_editor("enabled").setChecked(True)
    node.field_editor("items").setCurrentIndex(2)
    assert node.GetField("ratio") == 4.25
    assert node.GetField("enabled") is True
    assert node.GetFieldSelection("items") == 3


def test_init_assigned_title_and_fields_round_trip(qtbot, lbt, make_widget, tmp_path):
    pre = lbt.AddNode("PreInitNode", -200, 200)
    post = lbt.AddNode("PostInitNode", 200, 200)
    empty = lbt.AddNode("PostInitEmptyNode", 500, 200)
    pre.SetField("speed", 11)
    pre.SetFieldSelectionIndex("mode", 2)
    post.field_editor("ratio").setValue(0.75)
    post.SetTitle("Renamed Post")
    path = str(tmp_path / "init.json")
    assert lbt.SaveTree(path)

    data = read_json(path)
    assert saved_node(data, pre.GetId())["title"] == "Pre Title"
    assert saved_node(data, pre.GetId())["fields"] == {"speed": 11, "name": "pre", "mode": ["a", "b", "c"]}
    assert saved_node(data, post.GetId())["title"] == "Renamed Post"
    assert saved_node(data, post.GetId())["fields"] == {"ratio": 0.75, "enabled": False, "items": [1, 2, 3]}
    assert saved_node(data, empty.GetId())["fields"] == {}

    loaded = make_widget(node_types=ALL_TYPES)
    assert loaded.LoadTree(path)
    qtbot.wait(10)
    lpre, lpost, lempty = (node_by_id(loaded, n.GetId()) for n in (pre, post, empty))
    assert type(lpre) is PreInitNode and type(lpost) is PostInitNode
    assert lpre.GetTitle() == "Pre Title" and lpre._title_label.text() == "Pre Title"
    assert lpre.GetFields() == {"speed": 11, "name": "pre", "mode": ["a", "b", "c"]}
    assert lpre.GetFieldSelectionIndex("mode") == 2
    assert lpre.field_editor("speed").value() == 11
    assert lpre.field_editor("mode").currentIndex() == 2
    assert lpost.GetTitle() == "Renamed Post" and lpost._title_label.text() == "Renamed Post"
    assert lpost.GetFields() == {"ratio": 0.75, "enabled": False, "items": [1, 2, 3]}
    assert lpost.field_editor("ratio").value() == 0.75
    assert lempty.GetFields() == {} and not lempty._fields_frame.isVisibleTo(lempty)
    assert not loaded.IsModified()

    again = str(tmp_path / "again.json")
    assert loaded.SaveTree(again)
    assert read_json(again)["nodes"] == data["nodes"]


def test_title_and_fields_assigned_from_worker_thread_are_applied_on_gui_thread(qtbot, lbt, error_records):
    node = lbt.AddNode("NoFields", 0, 200)
    assert not node._fields_frame.isVisibleTo(node)
    lbt._set_modified(False)
    runtime_flags = []
    node.changed.connect(runtime_flags.append)

    def worker():
        node._title = "From Worker"
        node._fields = {"count": 3, "label": "w"}

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    qtbot.waitUntil(lambda: node.field_editor("count") is not None, timeout=2000)
    assert node._title_label.text() == "From Worker"
    assert node._fields_frame.isVisibleTo(node)
    assert editor_kinds(node) == {"count": "QSpinBox", "label": "QLineEdit"}
    assert runtime_flags and all(runtime_flags), "worker-thread changes are run-time changes"
    assert not lbt.IsModified()
    assert error_records() == []


# ============================================================================ Nodes: worker-thread SetField kind changes
KIND_SEQUENCES = {
    "int->int->str": (1, [2, "text"], QLineEdit, "text"),
    "str->str->int": ("a", ["b", 5], QSpinBox, 5),
    "int->int->float": (1, [3, 2.5], QDoubleSpinBox, 2.5),
    "float->float->int": (0.5, [1.5, 7], QSpinBox, 7),
    "list->list->bool": ([1], [[1, 2], True], QCheckBox, True),
    "bool->bool->list": (False, [True, ["x", "y"]], QComboBox, ["x", "y"]),
    "str->str->list->str": ("a", ["b", ["p"], "c"], QLineEdit, "c"),
}


@pytest.mark.parametrize("case", list(KIND_SEQUENCES))
def test_worker_thread_set_field_same_kind_then_kind_change(qtbot, bt, error_records, case):
    initial, updates, editor_type, final = KIND_SEQUENCES[case]
    node = bt.AddNode("NoFields", 0, 200)
    node.SetField("value", initial)
    assert node.field_editor("value") is not None

    def worker():
        for value in updates:
            node.SetField("value", value)

    with capture_exceptions() as exceptions:
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        qtbot.waitUntil(lambda: isinstance(node.field_editor("value"), editor_type), timeout=2000)
        qtbot.wait(20)  # every queued refresh has run
    assert exceptions == []
    assert error_records() == []
    assert node.GetField("value") == final
    editor = node.field_editor("value")
    assert type(editor) is editor_type
    if isinstance(editor, QLineEdit):
        assert editor.text() == final and not editor.isReadOnly()
    elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
        assert editor.value() == final
    elif isinstance(editor, QCheckBox):
        assert editor.isChecked() is final
    else:
        assert [editor.itemText(i) for i in range(editor.count())] == [str(item) for item in final]
    assert node._fields_frame.isVisibleTo(node)


def test_on_run_kind_change_during_execution(qtbot, lbt, error_records):
    root = lbt.GetRootNode()
    node = lbt.AddNode("KindSwitcher", 0, 200)
    lbt.Connect(root, node)
    fast_config(lbt)
    lbt._set_modified(False)
    with capture_exceptions() as exceptions:
        lbt.Execute()
        qtbot.waitUntil(lambda: not lbt.IsExecuting(), timeout=5000)
        qtbot.waitUntil(lambda: isinstance(node.field_editor("value"), QLineEdit), timeout=2000)
        qtbot.wait(20)
    assert exceptions == []
    assert error_records() == []
    assert node.GetStatus() is NodeStatus.SUCCEEDED
    assert node.GetField("value") == "done"
    assert node.field_editor("value").text() == "done"
    assert not lbt.IsModified(), "OnRun changes are run-time changes"


# ============================================================================ Nodes: misc fix-pass items
def test_dragging_on_field_name_moves_the_node(qtbot, bt):
    node = bt.AddNode("AllFields", 0, 200)
    bt.view().centerOn(node._item.sceneBoundingRect().center())
    qtbot.wait(10)
    row = node.field_editor("count").parentWidget()
    key_edit = row.key_edit
    assert key_edit.isReadOnly()
    start = viewport_point(bt, node, key_edit)
    assert bt.view().hit_test(start).kind == Hit.NODE
    before = QPointF(node._item.pos())
    count = node.GetField("count")
    mouse = Mouse(bt, via_window=False)
    mouse.drag(start, start + QPoint(60, 35))
    assert_close(node._item.pos() - before, QPointF(60, 35))
    assert node.GetField("count") == count


def test_changed_signal_runtime_flag_and_modified_marking(qtbot, bt):
    leaf = bt.AddNode("AllFields", 0, 200)
    seq = bt.AddNode("Sequence", 300, 0)
    bt._set_modified(False)
    events = []
    leaf.changed.connect(lambda runtime: events.append(("leaf", runtime)))
    seq.changed.connect(lambda runtime: events.append(("seq", runtime)))

    leaf.SetTitle("New Title")
    assert events[-1] == ("leaf", False) and bt.IsModified()
    bt._set_modified(False)

    leaf.field_editor("count").setValue(9)
    assert events[-1] == ("leaf", False) and bt.IsModified()
    bt._set_modified(False)

    leaf.field_editor("choice").setCurrentIndex(1)
    assert events[-1] == ("leaf", False) and bt.IsModified()
    bt._set_modified(False)

    seq.SetMemory(not seq.GetMemory())
    assert events[-1] == ("seq", False) and bt.IsModified()
    bt._set_modified(False)

    seq.SetCompositeType("Selector")
    assert events[-1] == ("seq", False) and bt.IsModified()
    bt._set_modified(False)

    count = len(events)
    thread = threading.Thread(target=lambda: (leaf.SetField("label", "worker"), leaf.SetFieldSelectionIndex("choice", 2)))
    thread.start()
    thread.join()
    qtbot.waitUntil(lambda: len(events) >= count + 2, timeout=2000)
    assert events[count:] == [("leaf", True), ("leaf", True)]
    assert not bt.IsModified(), "worker-thread changes must not mark the tree modified"


def test_unknown_leaf_title_marker_and_round_trip(qtbot, make_widget, tmp_path):
    path = write_tree(
        tmp_path / "unknown.json",
        [
            node_entry("r", "Root", composite="Sequence", memory=True),
            node_entry("u1", "MysteryType", 0, 200, title="My Mystery", fields={"speed": 3, "mode": ["a", "b"]},
                       field_selections={"mode": 1}),
            {"id": "u2", "type": "OtherMystery", "x": 300.0, "y": 200.0},
        ],
        [{"parent": "r", "child": "u1"}],
    )
    widget = make_widget()
    assert widget.LoadTree(path)
    u1, u2 = node_by_id(widget, "u1"), node_by_id(widget, "u2")
    assert isinstance(u1, UnknownLeafNodeWidget) and isinstance(u2, UnknownLeafNodeWidget)
    assert u1.GetTitle() == "My Mystery"
    assert u1._title_label.text() == "My Mystery (unknown type)"
    assert u2.GetTitle() == "OtherMystery", "without a saved title the type name is the title"
    assert u2._title_label.text() == "OtherMystery (unknown type)"
    assert u1.GetTypeName() == "MysteryType"
    assert u1.GetFields() == {"speed": 3, "mode": ["a", "b"]}

    out = str(tmp_path / "resaved.json")
    assert widget.SaveTree(out)
    data = read_json(out)
    s1, s2 = saved_node(data, "u1"), saved_node(data, "u2")
    assert (s1["type"], s1["title"]) == ("MysteryType", "My Mystery")
    assert s1["fields"] == {"speed": 3, "mode": ["a", "b"]}
    assert s1.get("field_selections") == {"mode": 1}
    assert (s2["type"], s2["title"]) == ("OtherMystery", "OtherMystery")
    assert "unknown type" not in json.dumps(data)

    u1.SetTitle("Renamed Mystery")
    assert u1._title_label.text() == "Renamed Mystery (unknown type)"
    assert widget.SaveTree(out)
    assert saved_node(read_json(out), "u1")["title"] == "Renamed Mystery"


def test_unknown_leaf_constructed_directly(qapp):
    node = UnknownLeafNodeWidget("Gizmo")
    try:
        assert node.GetTitle() == "Gizmo"
        assert node._title_label.text() == "Gizmo (unknown type)"
        assert node.GetTypeName() == "Gizmo"
    finally:
        node.deleteLater()


def test_root_titled_sequence_with_selector_type_round_trips(qtbot, bt, make_widget, tmp_path):
    root = bt.GetRootNode()
    root.SetCompositeType("Selector")
    root.SetTitle("Sequence")
    seq = bt.AddNode("Selector", 300, 0)
    seq.SetTitle("Sequence")
    assert root._title_label.text() == "Sequence (Selector)"
    path = str(tmp_path / "root.json")
    assert bt.SaveTree(path)

    loaded = make_widget()
    assert loaded.LoadTree(path)
    lroot = loaded.GetRootNode()
    assert lroot.GetCompositeType() == "Selector"
    assert lroot.GetTitle() == "Sequence"
    assert lroot._title_label.text() == "Sequence (Selector)"
    lseq = node_by_id(loaded, seq.GetId())
    assert (lseq.GetCompositeType(), lseq.GetTitle()) == ("Selector", "Sequence")
    again = str(tmp_path / "again.json")
    assert loaded.SaveTree(again)
    assert read_json(again)["nodes"] == read_json(path)["nodes"]


@pytest.mark.parametrize("title", ["Root", "Selector", "Custom"])
def test_root_saved_title_applied_after_composite_type(qtbot, make_widget, tmp_path, title):
    path = write_tree(tmp_path / "t.json", [node_entry("r", "Root", title=title, composite="Selector", memory=False)])
    widget = make_widget()
    assert widget.LoadTree(path)
    root = widget.GetRootNode()
    assert (root.GetCompositeType(), root.GetMemory(), root.GetTitle()) == ("Selector", False, title)


# ============================================================================ Widget: node type registration
def test_add_node_instance_registers_its_class(qtbot, bt, dialogs, tmp_path, make_widget):
    assert bt.GetNodeType("NotRegisteredUpFront") is None
    node = bt.AddNode(NotRegisteredUpFront(), 0, 200)
    assert bt.GetNodeType("NotRegisteredUpFront") is NotRegisteredUpFront
    assert "NotRegisteredUpFront" in bt.GetNodeTypes()
    menu = context_menu(bt, dialogs, empty_point(bt))
    assert "Add_NotRegisteredUpFront" in [action.objectName() for action in menu.actions()]
    node.SetField("k", 9)
    path = str(tmp_path / "late.json")
    assert bt.SaveTree(path)
    # loading into the same widget recreates the class (no placeholder)
    assert bt.LoadTree(path)
    loaded = node_by_id(bt, node.GetId())
    assert type(loaded) is NotRegisteredUpFront
    assert loaded.GetField("k") == 9


def test_add_node_class_registers_and_instantiates_it(bt):
    node = bt.AddNode(AlsoNotRegistered, 10, 20)
    assert type(node) is AlsoNotRegistered
    assert bt.GetNodeType("AlsoNotRegistered") is AlsoNotRegistered
    assert node in bt.GetNodes()


def test_add_node_instance_with_colliding_name_raises_and_adds_nothing(bt):
    impostor_cls = make_leaf("Succeed")  # same name as the registered conftest.Succeed
    impostor = impostor_cls()
    before = bt.GetNodes()
    try:
        with pytest.raises(ValueError):
            bt.AddNode(impostor, 0, 0)
        assert bt.GetNodes() == before
        assert bt.GetNodeType("Succeed") is not impostor_cls
        with pytest.raises(ValueError):
            bt.AddNode(impostor_cls, 0, 0)
        assert bt.GetNodes() == before
    finally:
        impostor.deleteLater()


def test_type_name_is_not_inherited(qtbot, lbt, tmp_path, make_widget):
    probe = BaseNamed()
    try:
        assert probe.GetTypeName() == "PublicBaseName"
    finally:
        probe.deleteLater()
    derived = lbt.AddNode("DerivedNamed", 0, 200)
    base = lbt.AddNode("PublicBaseName", 300, 200)
    assert derived.GetTypeName() == "DerivedNamed"
    assert type(base) is BaseNamed and base.GetTypeName() == "PublicBaseName"
    assert lbt.GetNodeType("DerivedNamed") is DerivedNamed
    assert lbt.GetNodeType("PublicBaseName") is BaseNamed
    path = str(tmp_path / "names.json")
    assert lbt.SaveTree(path)
    data = read_json(path)
    assert saved_node(data, derived.GetId())["type"] == "DerivedNamed"
    assert saved_node(data, base.GetId())["type"] == "PublicBaseName"
    loaded = make_widget(node_types=[BaseNamed, DerivedNamed])
    assert loaded.LoadTree(path)
    assert type(node_by_id(loaded, derived.GetId())) is DerivedNamed
    assert type(node_by_id(loaded, base.GetId())) is BaseNamed


def test_registering_base_and_derived_classes_does_not_collide(make_widget):
    widget = make_widget(node_types=[BaseNamed, DerivedNamed])
    assert widget.GetNodeType("PublicBaseName") is BaseNamed
    assert widget.GetNodeType("DerivedNamed") is DerivedNamed


def test_register_node_type_method_rejects_name_collision(bt):
    first = make_leaf("Collider")
    second = make_leaf("Collider")
    bt.RegisterNodeType(first)
    bt.RegisterNodeType(first)  # the same class again is fine
    with pytest.raises(ValueError):
        bt.RegisterNodeType(second)
    assert bt.GetNodeType("Collider") is first
    third = make_leaf("Anything", TYPE_NAME="Collider")
    with pytest.raises(ValueError):
        bt.RegisterNodeType(third)
    assert bt.GetNodeType("Collider") is first


def test_register_node_type_decorator_rejects_name_collision(clean_registry):
    first = register_node_type(make_leaf("DecoratedCollider"))
    assert register_node_type(first) is first
    with pytest.raises(ValueError):
        register_node_type(make_leaf("DecoratedCollider"))
    assert clean_registry["DecoratedCollider"] is first


# ============================================================================ Widget: SetConfig
@pytest.mark.parametrize(
    "changes",
    [
        {"tick_interval_ms": "100"},
        {"tick_interval_ms": 50.0},
        {"tick_interval_ms": True},
        {"tick_interval_ms": None},
        {"repeat": 1},
        {"repeat": "yes"},
        {"restore_blackboard": None},
        {"default_memory": 0},
    ],
    ids=lambda changes: "-".join(f"{k}={v!r}" for k, v in changes.items()),
)
def test_set_config_rejects_wrong_types(bt, changes):
    before = bt.GetConfig()
    config = bt.GetConfig()
    for key, value in changes.items():
        setattr(config, key, value)
    bt._set_modified(False)
    with pytest.raises(TypeError):
        bt.SetConfig(config)
    assert bt.GetConfig() == before
    assert not bt.IsModified()


@pytest.mark.parametrize("value", [None, {"tick_interval_ms": 10}, "config"])
def test_set_config_rejects_non_config(bt, value):
    with pytest.raises(TypeError):
        bt.SetConfig(value)


@pytest.mark.parametrize("interval,expected", [(0, 1), (-50, 1), (1, 1), (60000, 60000), (60001, 60000), (10**9, 60000)])
def test_set_config_clamps_tick_interval(bt, interval, expected):
    config = bt.GetConfig()
    config.tick_interval_ms = interval
    bt.SetConfig(config)
    assert bt.GetConfig().tick_interval_ms == expected
    assert bt.executor()._timer.interval() == expected
    assert config.tick_interval_ms == interval, "the caller's config object is not modified"


# ============================================================================ Widget: window title / files
@pytest.mark.parametrize(
    "filename,shown",
    [("a[*]b.json", "a[*]b.json"), ("[*].json", "[*].json"), ("x[*][*]y.json", "x[*][*]y.json"), ("tree.json", "tree.json")],
)
@pytest.mark.parametrize("window_modified", [False, True])
def test_window_title_escapes_qt_placeholder(qtbot, bt, tmp_path, filename, shown, window_modified):
    bt.setWindowModified(window_modified)
    bt._file_path = str(tmp_path / filename)  # '*' is not allowed in Windows file names
    bt._update_window_title()
    qtbot.wait(5)
    assert bt.windowHandle() is not None
    assert bt.windowHandle().title() == f"Behavior Tree - {shown}"


def test_file_with_utf8_bom_loads(qtbot, make_widget, tmp_path):
    path = write_tree(
        tmp_path / "bom.json",
        [node_entry("r", "Root", composite="Selector"), node_entry("n", "Succeed", 0, 200, title="Größe ✓")],
        [{"parent": "r", "child": "n"}],
        [{"name": "grüße", "type": "String", "value": "naïve"}],
        encoding="utf-8-sig",
    )
    with open(path, "rb") as stream:
        assert stream.read(3) == b"\xef\xbb\xbf"
    widget = make_widget()
    assert widget.LoadTree(path)
    node = node_by_id(widget, "n")
    assert node.GetTitle() == "Größe ✓"
    assert node.GetParent() is widget.GetRootNode()
    assert widget.GetEntry("grüße") == "naïve"


def test_interactive_load_of_bom_file_shows_no_error(make_widget, dialogs, tmp_path):
    path = write_tree(tmp_path / "bom2.json", [node_entry("r", "Root")], encoding="utf-8-sig")
    widget = make_widget()
    dialogs.open_path = path
    widget.button("Load").click()
    assert widget.IsTreeLoaded()
    assert "critical" not in dialogs.kinds() and "warning" not in dialogs.kinds()


def self_referencing_list() -> list:
    value = [1, "two"]
    value.append(value)
    return value


def test_save_skips_self_referencing_list_entry(qtbot, bt, dialogs, tmp_path):
    bt.SetEntry(self_referencing_list(), "loop")
    bt.SetEntry(5, "fine")
    path = str(tmp_path / "loop.json")
    # programmatic: saved, the value is skipped
    assert bt.SaveTree(path) is True
    names = [entry["name"] for entry in read_json(path)["blackboard"]]
    assert names == ["fine"]
    # interactive: no exception escapes; a warning names the skipped entry
    dialogs.save_path = str(tmp_path / "loop2.json")
    with capture_exceptions() as exceptions:
        bt.button("Save").click()
    assert exceptions == []
    assert "critical" not in dialogs.kinds()
    warnings = [args for kind, args in dialogs.shown if kind == "warning"]
    assert len(warnings) == 1 and "loop" in warnings[0][2]
    assert [entry["name"] for entry in read_json(tmp_path / "loop2.json")["blackboard"]] == ["fine"]
    assert bt.GetFilePath() == os.path.abspath(dialogs.save_path)


def test_save_skips_self_referencing_list_field(qtbot, bt, dialogs, tmp_path):
    node = bt.AddNode("NoFields", 0, 200)
    node.SetField("loop", self_referencing_list())
    node.SetField("ok", 3)
    path = str(tmp_path / "field_loop.json")
    assert bt.SaveTree(path)
    assert saved_node(read_json(path), node.GetId())["fields"] == {"ok": 3}
    dialogs.save_path = path
    with capture_exceptions() as exceptions:
        bt.button("Save").click()
    assert exceptions == []
    warnings = [args for kind, args in dialogs.shown if kind == "warning"]
    assert len(warnings) == 1 and "loop" in warnings[0][2]


def test_lone_surrogate_strings_save_and_load_back(qtbot, bt, make_widget, dialogs, tmp_path):
    text_entry = "a\ud800b"
    text_field = "x\udfffy"
    bt.SetEntry(text_entry, "odd")
    node = bt.AddNode("AllFields", 0, 200)
    node.SetField("label", text_field)
    node.SetTitle("title \udc80")
    path = str(tmp_path / "surrogates.json")
    assert bt.SaveTree(path)
    with open(path, "rb") as stream:
        stream.read().decode("utf-8")  # the file is valid UTF-8

    loaded = make_widget()
    assert loaded.LoadTree(path)
    assert loaded.GetEntry("odd") == text_entry
    lnode = node_by_id(loaded, node.GetId())
    assert lnode.GetField("label") == text_field
    assert lnode.GetTitle() == "title \udc80"

    # interactive save works as well
    dialogs.save_path = str(tmp_path / "surrogates2.json")
    with capture_exceptions() as exceptions:
        bt.button("Save").click()
    assert exceptions == []
    assert "critical" not in dialogs.kinds()
    assert read_json(dialogs.save_path)["blackboard"][0]["value"] == text_entry


@pytest.mark.parametrize(
    "value_factory",
    [threading.Lock, lambda: [threading.Lock()], Unreprable, lambda: {"k": Unreprable()}],
    ids=["lock", "list_of_locks", "uncopyable_object", "dict_with_uncopyable"],
)
def test_save_with_uncopyable_field_value(qtbot, bt, dialogs, tmp_path, value_factory):
    node = bt.AddNode("AllFields", 0, 200)
    node.SetField("handle", value_factory())
    path = str(tmp_path / "uncopyable.json")
    # programmatic: documented to skip values that cannot be written
    assert bt.SaveTree(path) is True
    fields = saved_node(read_json(path), node.GetId())["fields"]
    assert "handle" not in fields
    assert fields["count"] == 3 and fields["label"] == "hello"
    # interactive: no exception escapes, a warning lists the field
    dialogs.save_path = str(tmp_path / "uncopyable2.json")
    with capture_exceptions() as exceptions:
        bt.button("Save").click()
    assert exceptions == []
    assert "critical" not in dialogs.kinds()
    warnings = [args for kind, args in dialogs.shown if kind == "warning"]
    assert len(warnings) == 1 and "handle" in warnings[0][2]
    assert os.path.exists(dialogs.save_path)


def test_node_data_that_cannot_be_read_is_reported_not_raised(qtbot, lbt, dialogs, tmp_path, monkeypatch):
    node = lbt.AddNode("PostInitNode", 0, 200)

    def broken(self):
        raise RuntimeError("no data today")

    monkeypatch.setattr(PostInitNode, "_to_dict", broken)
    dialogs.save_path = str(tmp_path / "broken.json")
    with capture_exceptions() as exceptions:
        lbt.button("Save").click()
    assert exceptions == []
    warnings = [args for kind, args in dialogs.shown if kind == "warning"]
    assert len(warnings) == 1 and "Post Title" in warnings[0][2]
    assert saved_node(read_json(dialogs.save_path), node.GetId())["title"] == "Post Title"


@pytest.mark.parametrize("bad", ["missing_dir", "nul"])
def test_save_errors_raise_programmatically_and_show_critical_interactively(bt, dialogs, tmp_path, bad):
    path = str(tmp_path / "missing" / "t.json") if bad == "missing_dir" else str(tmp_path / "a\0b.json")
    expected = OSError if bad == "missing_dir" else ValueError
    with pytest.raises(expected):
        bt.SaveTree(path)
    dialogs.save_path = path
    with capture_exceptions() as exceptions:
        bt.button("Save").click()
    assert exceptions == []
    assert dialogs.kinds()[-1] == "critical"
    assert bt.GetFilePath() == str(tmp_path / "tree.json")


@pytest.mark.parametrize("bad", ["missing_dir", "nul"])
def test_new_tree_errors_raise_programmatically_and_show_critical_interactively(bt, dialogs, tmp_path, bad):
    path = str(tmp_path / "missing" / "t.json") if bad == "missing_dir" else str(tmp_path / "n\0.json")
    before = describe(bt)
    with pytest.raises((OSError, ValueError)):
        bt.NewTree(path)
    dialogs.save_path = path
    with capture_exceptions() as exceptions:
        bt.button("New").click()
    assert exceptions == []
    assert dialogs.kinds()[-1] == "critical"
    assert describe(bt) == before


def test_confirm_discard_changes_save_failure_shows_critical(bt, dialogs, tmp_path):
    bt.AddNode("Succeed", 0, 200)
    assert bt.IsModified()
    bt._file_path = str(tmp_path / "gone" / "tree.json")
    dialogs.question_answer = QMessageBox.StandardButton.Save
    dialogs.save_path = str(tmp_path / "other.json")
    with capture_exceptions() as exceptions:
        assert bt.ConfirmDiscardChanges() is False
        bt.button("New").click()
        bt.button("Load").click()
    assert exceptions == []
    assert dialogs.kinds().count("critical") == 3
    assert "save_dialog" not in dialogs.kinds() and "open_dialog" not in dialogs.kinds()
    assert bt.IsModified()


# ============================================================================ Blackboard
def test_integer_entries_reject_bool(bt):
    bt.AddEntry("n", "Integer", 1)
    with pytest.raises(TypeError):
        bt.SetEntry(True, "n")
    with pytest.raises(TypeError):
        bt.AddEntry("m", "Integer", False)
    assert bt.GetEntry("n") == 1 and not bt.HasEntry("m")
    bt.SetEntry(True, "flag")
    assert bt.GetEntryType("flag") == "Bool"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: [threading.Lock(), threading.Lock()],
        lambda: {"lock": threading.Lock()},
        lambda: {threading.Lock()},
        lambda: (threading.Lock(),),
    ],
    ids=["list", "dict", "set", "tuple"],
)
def test_set_entry_with_uncopyable_container_creates_object_entry(qtbot, bt, factory):
    value = factory()
    bt.SetEntry(value, "locks")
    assert bt.GetEntryType("locks") == "Object"
    assert bt.GetEntry("locks") is value
    row = bt.blackboardView().row("locks")
    assert row is not None and row.type_name == "Object"
    assert row.value_widget.isReadOnly()


def test_uncopyable_value_on_existing_container_entry_raises_helpful_type_error(bt):
    bt.AddEntry("items", "List", [1])
    with pytest.raises(TypeError, match="Object"):
        bt.SetEntry([threading.Lock()], "items")
    with pytest.raises(TypeError):
        bt.AddEntry("more", "List", [threading.Lock()])
    assert bt.GetEntry("items") == [1] and not bt.HasEntry("more")


def test_list_of_locks_object_entry_is_skipped_when_saving(bt, dialogs, tmp_path):
    bt.SetEntry([threading.Lock()], "locks")
    bt.SetEntry("x", "text")
    dialogs.save_path = str(tmp_path / "locks.json")
    bt.button("Save").click()
    assert [entry["name"] for entry in read_json(dialogs.save_path)["blackboard"]] == ["text"]
    # Object entries are run-time values: silently not saved (no warning on every save).
    assert "warning" not in dialogs.kinds()


def test_dispose_clears_rows_and_later_writes_raise(qtbot, bt):
    for index, name in enumerate(["a", "b", "c"]):
        bt.SetEntry(index, name)
    bt.SetEntry([1, 2], "items")
    view = bt.blackboardView()
    store = bt.blackboardStore()
    removed = []
    store.entryRemoved.connect(removed.append)
    bt.setCurrentIndex(1)
    qtbot.wait(10)

    store.dispose()
    qtbot.wait(20)
    assert sorted(removed) == ["a", "b", "c", "items"]
    assert view.rows() == []
    assert all(view.row(name) is None for name in ["a", "b", "c", "items"])
    assert view_rows_in_layout(view) == []
    assert [row for row in view.entry_list.widget().findChildren(EntryRow) if shiboken6.isValid(row)] == []
    assert bt.GetEntryNames() == []

    writes = {
        "SetEntry existing": lambda: bt.SetEntry(5, "a"),
        "SetEntry new": lambda: bt.SetEntry(5, "new"),
        "AddEntry": lambda: bt.AddEntry("new", "Integer", 1),
        "RemoveEntry": lambda: bt.RemoveEntry("a"),
        "rename": lambda: store.rename("a", "z"),
        "replace": lambda: store.replace("a", "String", "s"),
    }
    for label, write in writes.items():
        with pytest.raises(RuntimeError):
            write()
    with pytest.raises(KeyError):
        bt.GetEntry("a")


def test_shutdown_disposes_and_view_is_cleared(qtbot, bt):
    bt.SetEntry(1, "a")
    bt.SetEntry("s", "b")
    ns = bt.GetBlackboardNamespace()
    bt.Shutdown()
    qtbot.wait(20)
    assert bt.blackboardView().rows() == []
    assert not [key for key in py_trees.blackboard.Blackboard.storage if key.startswith(ns + "/")]
    with pytest.raises(RuntimeError):
        bt.SetEntry(2, "a")


def _focus_uncommitted_rename(qtbot, bt, name="x"):
    """Type a new name into entry ``name``'s ValueName editor without committing it."""
    bt.AddEntry(name, "Integer", 1)
    bt.AddEntry("y", "Integer", 2)
    bt.setCurrentIndex(1)
    bt.activateWindow()
    qtbot.waitUntil(bt.isActiveWindow, timeout=2000)
    row = bt.blackboardView().row(name)
    row.name_edit.setFocus()
    qtbot.waitUntil(row.name_edit.hasFocus, timeout=1000)
    row.name_edit.selectAll()
    QTest.keyClicks(row.name_edit, "renamed")
    assert row.name_edit.text() == "renamed"
    return row


def _worker(action):
    thread = threading.Thread(target=action)
    thread.start()
    thread.join()


CHANGES_WHILE_RENAMING = {
    "retype_gui": lambda bt: bt.blackboardStore().replace("x", "String", "hello"),
    "remove_gui": lambda bt: bt.RemoveEntry("x"),
    "remove_then_set_gui": lambda bt: (bt.RemoveEntry("x"), bt.SetEntry("hello", "x")),
    "retype_worker": lambda bt: _worker(lambda: bt.blackboardStore().replace("x", "Bool", True)),
    "remove_worker": lambda bt: _worker(lambda: bt.RemoveEntry("x")),
    "remove_then_set_worker": lambda bt: _worker(lambda: (bt.RemoveEntry("x"), bt.SetEntry("hello", "x"))),
}


@pytest.mark.parametrize("change", list(CHANGES_WHILE_RENAMING))
def test_entry_changed_while_its_name_editor_has_uncommitted_text(qtbot, bt, error_records, change):
    _focus_uncommitted_rename(qtbot, bt)
    with capture_exceptions() as exceptions:
        CHANGES_WHILE_RENAMING[change](bt)
        qtbot.wait(50)
    assert exceptions == []
    assert error_records() == []
    assert "renamed" not in bt.GetEntryNames(), "the uncommitted name must not be applied"
    assert not QToolTip.isVisible(), "no rename error must be shown"
    assert_view_matches_store(qtbot, bt)

    # later (re-)created entries are visible and usable
    if not bt.HasEntry("x"):
        bt.SetEntry("again", "x")
    else:
        bt.RemoveEntry("x")
        bt.SetEntry(3.5, "x")
    assert_view_matches_store(qtbot, bt)
    row = bt.blackboardView().row("x")
    assert row.isVisible()
    QToolTip.hideText()


def _exec_like(dialog, during):
    """Emulate QDialog.exec(): show the dialog and run a nested event loop until it finishes."""
    loop = QEventLoop()
    dialog.finished.connect(loop.quit)
    dialog.destroyed.connect(loop.quit)
    dialog.setModal(True)
    dialog.show()
    QTimer.singleShot(0, during)
    safety = QTimer()
    safety.setSingleShot(True)
    safety.timeout.connect(loop.quit)
    safety.start(5000)
    loop.exec()
    safety.stop()
    if not shiboken6.isValid(dialog):
        return False
    return dialog.result() == QDialog.DialogCode.Accepted


def _when(condition, action):
    """Run ``action`` from the event loop as soon as ``condition()`` is true."""
    timer = QTimer()
    timer.setInterval(5)

    def check():
        if condition():
            timer.stop()
            action()

    timer.timeout.connect(check)
    timer.start()
    return timer


@pytest.mark.parametrize("remover", ["gui", "worker"])
@pytest.mark.parametrize("outcome", ["reject", "accept"])
def test_entry_removed_while_edit_dialog_is_open(qtbot, bt, dialogs, error_records, remover, outcome):
    bt.AddEntry("items", "List", [1, 2])
    bt.setCurrentIndex(1)
    view = bt.blackboardView()
    row = view.row("items")
    info = {}
    keep = []

    def handler(dialog):
        info["parent_is_view"] = dialog.parentWidget() is view

        def during():
            if remover == "gui":
                bt.RemoveEntry("items")
            else:
                _worker(lambda: bt.RemoveEntry("items"))

            def finish():
                if outcome == "accept":
                    dialog.text_edit.setText("[7, 8]")
                    dialog._try_accept()
                else:
                    dialog.reject()

            keep.append(_when(lambda: view.row("items") is None, finish))

        return _exec_like(dialog, during)

    dialogs.dialog_handler = handler
    with capture_exceptions() as exceptions:
        row.edit_button.click()
        qtbot.wait(20)
    assert exceptions == []
    assert error_records() == []
    assert info["parent_is_view"], "the Edit dialog is parented to the view, not to the row"
    if outcome == "reject":
        assert not bt.HasEntry("items")
    assert_view_matches_store(qtbot, bt)


def test_entry_retyped_while_edit_dialog_is_open(qtbot, bt, dialogs, error_records):
    bt.AddEntry("items", "List", [1, 2])
    bt.setCurrentIndex(1)
    view = bt.blackboardView()
    keep = []

    def handler(dialog):
        def during():
            bt.blackboardStore().replace("items", "String", "now a string")

            def finish():
                dialog.text_edit.setText("[7, 8]")
                dialog._try_accept()

            keep.append(_when(lambda: view.row("items") is not None and view.row("items").type_name == "String", finish))

        return _exec_like(dialog, during)

    dialogs.dialog_handler = handler
    with capture_exceptions() as exceptions:
        view.row("items").edit_button.click()
        qtbot.wait(20)
    assert exceptions == []
    assert error_records() == []
    assert bt.GetEntryType("items") == "String"
    assert bt.GetEntry("items") == "now a string"
    assert_view_matches_store(qtbot, bt)


@pytest.mark.parametrize(
    "type_name,text,check",
    [
        ("List", "[nan, inf, -inf, 1.5]", lambda v: math.isnan(v[0]) and v[1:] == [math.inf, -math.inf, 1.5]),
        ("List", "[NaN, Infinity, -INF]", lambda v: math.isnan(v[0]) and v[1:] == [math.inf, -math.inf]),
        ("Dictionary", "{'a': nan, 'b': -inf}", lambda v: math.isnan(v["a"]) and v["b"] == -math.inf),
        ("Set", "{inf, -inf, 2}", lambda v: v == {math.inf, -math.inf, 2}),
        ("List", "[(nan, 1), [inf]]", lambda v: math.isnan(v[0][0]) and v[1] == [math.inf]),
    ],
)
def test_edit_dialog_accepts_nan_and_inf(qtbot, bt, dialogs, type_name, text, check):
    bt.AddEntry("values", type_name)
    bt.setCurrentIndex(1)
    row = bt.blackboardView().row("values")
    result = {}

    def handler(dialog):
        dialog.text_edit.setText(text)
        dialog.buttons.button(dialog.buttons.StandardButton.Ok).click()
        result["error"] = dialog.error_label.text() if dialog.error_label.isVisibleTo(dialog) else ""
        return dialog.result() == QDialog.DialogCode.Accepted

    dialogs.dialog_handler = handler
    row.edit_button.click()
    assert result["error"] == ""
    assert check(bt.GetEntry("values"))


def test_container_parsing_accepts_non_finite_names():
    assert math.isnan(literal_eval_extended("nan"))
    assert literal_eval_extended("-inf") == -math.inf
    value = parse_container_text("List", "[nan, inf, -inf]")
    assert math.isnan(value[0]) and value[1:] == [math.inf, -math.inf]
    with pytest.raises(ValueError):
        parse_container_text("List", "[nan, undefined_name]")


def _type_keys(qtbot, spin, text):
    """Type ``text`` key by key; returns the editor text after every key."""
    texts = []
    for char in text:
        QTest.keyClick(spin, char)
        texts.append(spin.lineEdit().text())
    return texts


def test_typing_into_double_entry_spin_box_is_not_reformatted(qtbot, bt):
    bt.AddEntry("d", "Double", 0.0)
    bt.setCurrentIndex(1)
    bt.activateWindow()
    qtbot.waitUntil(bt.isActiveWindow, timeout=2000)
    spin = bt.blackboardView().row("d").value_widget
    assert isinstance(spin, QDoubleSpinBox)
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus, timeout=1000)
    spin.selectAll()
    typed = "12.5"  # FloatSpinBox always shows "." (and accepts "," too)
    texts = _type_keys(qtbot, spin, typed)
    assert texts == [typed[: i + 1] for i in range(len(typed))], "the text was reformatted while typing"
    assert bt.GetEntry("d") == 12.5
    QTest.keyClicks(spin, "25")
    assert bt.GetEntry("d") == pytest.approx(12.525)
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert bt.GetEntry("d") == pytest.approx(12.525)
    assert spin.value() == pytest.approx(12.525)


def test_typing_into_integer_entry_spin_box(qtbot, bt):
    bt.AddEntry("i", "Integer", 0)
    bt.setCurrentIndex(1)
    bt.activateWindow()
    qtbot.waitUntil(bt.isActiveWindow, timeout=2000)
    spin = bt.blackboardView().row("i").value_widget
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus, timeout=1000)
    spin.selectAll()
    assert _type_keys(qtbot, spin, "-305") == ["-", "-3", "-30", "-305"]
    assert bt.GetEntry("i") == -305


def test_value_set_from_code_still_updates_spin_box(qtbot, bt):
    bt.AddEntry("d", "Double", 0.0)
    spin = bt.blackboardView().row("d").value_widget
    bt.SetEntry(3.25, "d")
    assert spin.value() == 3.25
    _worker(lambda: bt.SetEntry(-1.5, "d"))
    qtbot.waitUntil(lambda: spin.value() == -1.5, timeout=2000)


def test_restore_keeps_snapshot_order_in_the_view(qtbot, bt):
    for index, name in enumerate(["a", "b", "c"]):
        bt.SetEntry(index, name)
    store = bt.blackboardStore()
    view = bt.blackboardView()
    snapshot = store.snapshot()
    store.remove("a")
    bt.SetEntry(10, "a")
    bt.SetEntry(3, "d")
    bt.RemoveEntry("b")
    bt.SetEntry("bee", "b")
    assert view_rows_in_layout(view) == ["c", "a", "d", "b"]
    reordered = []
    store.entriesReordered.connect(lambda: reordered.append(True))

    store.restore(snapshot)
    assert store.names() == ["a", "b", "c"]
    assert [(n, bt.GetEntry(n)) for n in store.names()] == [("a", 0), ("b", 1), ("c", 2)]
    assert reordered == [True]
    assert view_rows_in_layout(view) == ["a", "b", "c"]
    assert [row.name for row in view.rows()] == ["a", "b", "c"]
    assert_view_matches_store(qtbot, bt)


def test_reset_restores_blackboard_order_after_run_once(qtbot, lbt):
    for index, name in enumerate(["a", "b", "c"]):
        lbt.SetEntry(index, name)
    root = lbt.GetRootNode()
    node = lbt.AddNode("OrderShuffler", 0, 200)
    lbt.Connect(root, node)
    fast_config(lbt, restore_blackboard=True)
    lbt.Execute()
    qtbot.waitUntil(lambda: not lbt.IsExecuting(), timeout=5000)
    assert lbt.GetEntryNames() == ["b", "c", "a", "d"]
    lbt.Reset()
    assert lbt.GetEntryNames() == ["a", "b", "c"]
    assert [lbt.GetEntry(n) for n in "abc"] == [0, 1, 2]
    assert view_rows_in_layout(lbt.blackboardView()) == ["a", "b", "c"]


def test_store_changed_signal_runtime_flag(qtbot, bt, executing):
    store = bt.blackboardStore()
    flags = []
    store.changed.connect(flags.append)
    bt.SetEntry(1, "while_running")
    assert flags == [True]
    bt.Stop()
    flags.clear()
    bt.SetEntry(2, "idle")
    assert flags == [False]
    _worker(lambda: bt.SetEntry(3, "idle"))
    qtbot.waitUntil(lambda: len(flags) == 2, timeout=2000)
    assert flags == [False, True]


def test_parse_list_validates_without_changing_and_reports_duplicates(bt):
    bt.SetEntry(1, "a")
    items = [
        {"name": "a", "type": "Integer", "value": 5},
        {"name": "a", "type": "String", "value": "dup"},
        {"name": "b", "type": "Nope", "value": 1},
        {"name": "c", "type": "Double", "value": 1.5},
    ]
    store = bt.blackboardStore()
    entries, problems = store.parse_list(items)
    assert entries == [("a", "Integer", 5), ("c", "Double", 1.5)]
    assert len(problems) == 2
    assert any("duplicate" in problem for problem in problems)
    assert bt.GetEntry("a") == 1 and not bt.HasEntry("c")


def test_load_list_replace_keeps_object_entries(bt):
    handle = object()
    bt.SetEntry(handle, "handle")
    bt.SetEntry(1, "old")
    bt.SetEntry(2, "kept")
    store = bt.blackboardStore()
    problems = store.load_list([{"name": "kept", "type": "Integer", "value": 20}, {"name": "new", "type": "String", "value": "n"}], replace=True)
    assert problems == []
    assert bt.GetEntryNames() == ["handle", "kept", "new"]
    assert bt.GetEntry("handle") is handle and bt.GetEntry("kept") == 20
    problems = store.load_list([{"name": "extra", "type": "Bool", "value": True}])
    assert problems == [] and bt.GetEntryNames() == ["handle", "kept", "new", "extra"]


# ============================================================================ Widget: Load / New replace the blackboard
def test_load_replaces_blackboard_but_keeps_object_entries(qtbot, bt, tmp_path):
    handle = threading.Lock()
    bt.SetEntry(handle, "handle")
    bt.SetEntry(1, "old")
    bt.SetEntry("before", "shared")
    path = write_tree(
        tmp_path / "bb.json",
        [node_entry("r", "Root")],
        blackboard=[
            {"name": "shared", "type": "String", "value": "after"},
            {"name": "fresh", "type": "Integer", "value": 5},
        ],
    )
    assert bt.LoadTree(path)
    assert set(bt.GetEntryNames()) == {"handle", "shared", "fresh"}
    assert bt.GetEntry("handle") is handle
    assert bt.GetEntry("shared") == "after" and bt.GetEntry("fresh") == 5
    assert not bt.HasEntry("old")
    assert_view_matches_store(qtbot, bt)
    assert not bt.IsModified()


def test_new_tree_empties_blackboard_but_keeps_object_entries(qtbot, bt, tmp_path):
    handle = object()
    bt.SetEntry(handle, "handle")
    bt.SetEntry(1, "old")
    bt.SetEntry([1, 2], "items")
    assert bt.NewTree(str(tmp_path / "fresh.json"))
    assert bt.GetEntryNames() == ["handle"]
    assert bt.GetEntry("handle") is handle
    assert_view_matches_store(qtbot, bt)


# ============================================================================ Widget: validation before touching the tree
def test_unhashable_ids_load_with_problems(qtbot, make_widget, dialogs, tmp_path):
    path = write_tree(
        tmp_path / "ids.json",
        [
            node_entry("r", "Root"),
            node_entry("a", "Succeed", 0, 200),
            node_entry("b", "Succeed", 200, 200),
            {"id": ["not", "hashable"], "type": "Succeed", "x": 0, "y": 0},
            {"id": {"x": 1}, "type": "Succeed", "x": 0, "y": 0},
        ],
        [
            {"parent": ["r"], "child": "a"},
            {"parent": "r", "child": {"id": "b"}},
            {"parent": {"r": 1}, "child": ["b"]},
            {"parent": "r", "child": "b"},
        ],
    )
    widget = make_widget()
    assert widget.LoadTree(path) is True  # programmatic: problems are logged, not raised
    assert sorted(node.GetId() for node in widget.GetNodes()) == ["a", "b", "r"]
    assert node_by_id(widget, "b").GetParent() is widget.GetRootNode()
    assert node_by_id(widget, "a").GetParent() is None

    other = make_widget()
    dialogs.open_path = path
    with capture_exceptions() as exceptions:
        other.button("Load").click()
    assert exceptions == []
    assert other.IsTreeLoaded()
    warnings = [args for kind, args in dialogs.shown if kind == "warning"]
    assert len(warnings) == 1
    message = warnings[0][2]
    assert "connection 0" in message and "connection 1" in message and "connection 2" in message
    assert "critical" not in dialogs.kinds()


def _current_tree(bt):
    root = bt.GetRootNode()
    leaf = bt.AddNode("AllFields", -200, 220)
    seq = bt.AddNode("Selector", 200, 0)
    bt.Connect(root, leaf)
    bt.Connect(root, seq)
    leaf.SetField("count", 42)
    bt.SetEntry(7, "counter")
    bt.SetEntry([1, 2], "items")
    assert bt.SaveTree(bt.GetFilePath())
    seq.SetTitle("Unsaved Title")  # modified, not saved
    return describe(bt)


def test_failed_validation_leaves_current_tree_untouched(qtbot, lbt, dialogs, tmp_path):
    before = _current_tree(lbt)
    path = write_tree(
        tmp_path / "bad.json",
        [node_entry("r", "Root", composite="Selector"), node_entry("x", "BadLoadNode", 0, 200, broken=True)],
        blackboard=[{"name": "fromfile", "type": "Integer", "value": 1}],
    )
    with pytest.raises(TreeFileError):
        lbt.LoadTree(path)
    assert describe(lbt) == before

    dialogs.open_path = path
    dialogs.question_answer = QMessageBox.StandardButton.Discard
    with capture_exceptions() as exceptions:
        lbt.button("Load").click()
    assert exceptions == []
    assert dialogs.kinds()[-1] == "critical"
    assert describe(lbt) == before
    assert not lbt.HasEntry("fromfile")


def test_unexpected_install_failure_restores_previous_tree(qtbot, lbt, tmp_path, monkeypatch):
    before = _current_tree(lbt)
    path = write_tree(
        tmp_path / "other.json",
        [node_entry("r2", "Root"), node_entry("n2", "Succeed", 0, 200)],
        [{"parent": "r2", "child": "n2"}],
        [{"name": "fromfile", "type": "Integer", "value": 1}],
    )
    view = lbt.view()
    original = view.connect_nodes
    calls = {"n": 0}

    def fail_once(parent, child):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated failure while installing")
        return original(parent, child)

    monkeypatch.setattr(view, "connect_nodes", fail_once)
    with pytest.raises(TreeFileError):
        lbt.LoadTree(path)
    after = describe(lbt)
    assert after["nodes"] == before["nodes"]
    assert after["blackboard"] == before["blackboard"]
    assert after["rows"] == before["rows"]
    assert after["file"] == before["file"]
    assert after["title"] == before["title"]
    assert after["modified"] == before["modified"]
    assert not lbt.HasEntry("fromfile")


def test_undecodable_field_value_keeps_default(qtbot, make_widget, dialogs, tmp_path):
    path = write_tree(
        tmp_path / "field.json",
        [
            node_entry("r", "Root"),
            node_entry("f", "AllFields", 0, 200, fields={
                "count": {"__dict__": [[1]]},
                "label": {"__set__": [[1, 2]]},
                "ratio": 0.25,
            }),
        ],
    )
    widget = make_widget()
    assert widget.LoadTree(path)
    node = node_by_id(widget, "f")
    assert node.GetField("count") == 3
    assert node.GetField("label") == "hello"
    assert node.GetField("ratio") == 0.25
    other = make_widget()
    dialogs.open_path = path
    other.button("Load").click()
    warnings = [args for kind, args in dialogs.shown if kind == "warning"]
    assert len(warnings) == 1 and "'count'" in warnings[0][2] and "'label'" in warnings[0][2]


def test_non_finite_positions_zoom_and_center_are_rejected(qtbot, make_widget, tmp_path):
    document = tree_document(
        [node_entry("r", "Root"), node_entry("a", "Succeed", float("nan"), 200.0), node_entry("b", "Succeed", 50.0, float("inf"))],
        view={"zoom": float("inf"), "center": [float("nan"), 0.0]},
    )
    path = tmp_path / "nonfinite.json"
    path.write_text(json.dumps(document), encoding="utf-8")  # writes NaN / Infinity tokens
    widget = make_widget()
    assert widget.LoadTree(str(path))
    for node_id in ("a", "b"):
        pos = node_by_id(widget, node_id)._item.pos()
        assert math.isfinite(pos.x()) and math.isfinite(pos.y())
    assert widget.view().zoom() == 1.0
    center = widget.view().view_center()
    assert math.isfinite(center.x()) and math.isfinite(center.y())


# ============================================================================ Widget: Load / New while a tick is in progress
def _next_tree_file(tmp_path) -> str:
    return write_tree(
        tmp_path / "next.json",
        [node_entry("r2", "Root"), node_entry("n2", "Succeed", 0, 200, title="Next Leaf")],
        [{"parent": "r2", "child": "n2"}],
        [{"name": "next_entry", "type": "Integer", "value": 2}],
    )


def _run_tick_scenario(qtbot, widget, dialogs, where, action):
    """Execute Root -> leaf and perform ``action(widget)`` while a tick is in progress."""
    root = widget.GetRootNode()
    TickAction.outcome = {}
    if where == "status_slot":
        leaf = widget.AddNode("Succeed", 0, 200)

        def on_status(node, status):
            if node is leaf and status == "Succeeded" and not TickAction.outcome:
                try:
                    TickAction.outcome["result"] = action(widget)
                except Exception as error:  # noqa: BLE001
                    TickAction.outcome["error"] = error

        widget.nodeStatusChanged.connect(on_status)
    else:
        leaf = widget.AddNode("TickAction", 0, 200)
        if where == "on_run":
            TickAction.action = action
        else:  # "nested_loop": the user clicks a button while OnRun shows a modal dialog (nested event loop)
            def nested(tree):
                loop = QEventLoop()
                QTimer.singleShot(0, lambda: action(tree))
                QTimer.singleShot(30, loop.quit)
                loop.exec()
                return True

            TickAction.action = nested
    widget.Connect(root, leaf)
    fast_config(widget)
    previous_ids = {node.GetId() for node in widget.GetNodes()}
    with capture_exceptions() as exceptions:
        widget.Execute()
        qtbot.waitUntil(lambda: bool(TickAction.outcome) and not widget.IsExecuting(), timeout=5000)
        qtbot.wait(30)
    TickAction.action = None
    return previous_ids, exceptions


def _assert_whole_tree(widget, previous_ids, is_new) -> None:
    """The widget holds either the previous tree or the complete new one, never a partial / empty tree."""
    nodes = widget.GetNodes()
    ids = {node.GetId() for node in nodes}
    assert widget.GetRootNode() is not None, f"the view was left without a root node (nodes: {sorted(ids)})"
    assert ids == previous_ids or is_new(widget), f"half-loaded tree: {sorted(ids)}"


@pytest.mark.parametrize("where", ["on_run", "status_slot"])
def test_load_tree_during_a_tick_never_leaves_a_partial_tree(qtbot, lbt, dialogs, tmp_path, where):
    path = _next_tree_file(tmp_path)
    previous_ids, exceptions = _run_tick_scenario(qtbot, lbt, dialogs, where, lambda tree: tree.LoadTree(path))
    assert exceptions == []
    error = TickAction.outcome.get("error")
    assert error is None or isinstance(error, TreeFileError), repr(error)
    _assert_whole_tree(lbt, previous_ids, lambda w: {n.GetId() for n in w.GetNodes()} == {"r2", "n2"})
    assert not lbt.IsExecuting()


def test_load_button_in_nested_loop_during_a_tick_never_leaves_a_partial_tree(qtbot, lbt, dialogs, tmp_path):
    path = _next_tree_file(tmp_path)
    dialogs.open_path = path
    dialogs.question_answer = QMessageBox.StandardButton.Discard
    previous_ids, exceptions = _run_tick_scenario(
        qtbot, lbt, dialogs, "nested_loop", lambda tree: tree.button("Load").click()
    )
    assert exceptions == []
    assert "open_dialog" in dialogs.kinds()
    _assert_whole_tree(lbt, previous_ids, lambda w: {n.GetId() for n in w.GetNodes()} == {"r2", "n2"})


@pytest.mark.parametrize("where", ["on_run", "status_slot"])
def test_new_tree_during_a_tick_never_leaves_a_partial_tree(qtbot, lbt, dialogs, tmp_path, where):
    fresh = str(tmp_path / "fresh.json")
    previous_ids, exceptions = _run_tick_scenario(qtbot, lbt, dialogs, where, lambda tree: tree.NewTree(fresh))
    assert exceptions == []
    error = TickAction.outcome.get("error")
    assert error is None or isinstance(error, (TreeFileError, OSError)), repr(error)
    _assert_whole_tree(
        lbt, previous_ids,
        lambda w: len(w.GetNodes()) == 1 and w.GetFilePath() == os.path.abspath(fresh),
    )


# ============================================================================ Widget: view centre restore
VIEW_CENTER = (640.0, 480.0)
VIEW_ZOOM = 1.25


@pytest.fixture
def view_file(tmp_path):
    return write_tree(
        tmp_path / "view.json",
        [node_entry("r", "Root"), node_entry("w", "Wait", 600, 450)],
        view={"zoom": VIEW_ZOOM, "center": list(VIEW_CENTER)},
    )


def assert_view_restored(widget) -> None:
    center = widget.view().view_center()
    assert abs(center.x() - VIEW_CENTER[0]) <= 2.0 and abs(center.y() - VIEW_CENTER[1]) <= 2.0, (
        f"saved centre {VIEW_CENTER} replaced by ({center.x():.1f}, {center.y():.1f})"
    )
    assert widget.view().zoom() == pytest.approx(VIEW_ZOOM)


def test_view_center_restored_when_loaded_before_show(qtbot, view_file):
    widget = BehaviorTreeWidget(node_types=DEMO_NODE_TYPES)
    qtbot.addWidget(widget)
    try:
        widget.resize(1000, 700)
        assert widget.LoadTree(view_file)
        widget.show()
        qtbot.waitExposed(widget)
        qtbot.wait(50)
        assert_view_restored(widget)
    finally:
        widget._set_modified(False)
        widget.Shutdown()


@pytest.mark.parametrize("order", ["show_then_load", "load_then_show"])
def test_view_center_restored_in_demo_sequence(qtbot, view_file, order):
    """demo.main(): window.show(); window.tree.LoadTree(path); app.exec() (and the reverse order)."""
    window = DemoWindow()
    qtbot.addWidget(window)
    if order == "show_then_load":
        window.show()
        assert window.tree.LoadTree(view_file)
    else:
        assert window.tree.LoadTree(view_file)
        window.show()
    qtbot.waitExposed(window)
    qtbot.wait(50)
    assert_view_restored(window.tree)
    assert window.windowTitle() == "Behavior Tree - view.json"


def test_view_center_restored_when_loaded_after_event_loop_ran(qtbot, make_widget, view_file):
    widget = make_widget(node_types=DEMO_NODE_TYPES)
    qtbot.wait(50)
    assert widget.LoadTree(view_file)
    qtbot.wait(50)
    assert_view_restored(widget)


def test_widget_deleted_right_after_show_and_load_raises_nothing(qtbot, view_file):
    widget = BehaviorTreeWidget(node_types=DEMO_NODE_TYPES)
    widget.show()
    assert widget.LoadTree(view_file)
    widget.Shutdown()
    with capture_exceptions() as exceptions:
        shiboken6.delete(widget)
        qtbot.wait(30)
    assert exceptions == []


# ============================================================================ Widget: close / destroy
def test_close_top_level_widget_stops_but_does_not_shut_down(qtbot, bt, dialogs, executing):
    ns = executing
    dialogs.question_answer = QMessageBox.StandardButton.Discard
    assert bt.close()
    assert not bt.IsExecuting()
    assert SlowCancelable.cancelled.wait(2)
    assert not bt.blackboardStore().is_disposed()
    bt.SetEntry(1, "still_writable")
    bt.show()
    qtbot.waitExposed(bt)
    bt.Execute()
    assert bt.IsExecuting(), "a closed (not deleted) window can execute again after being shown"
    bt.Stop()


def test_close_cancelled_keeps_widget_executing(qtbot, bt, dialogs, executing):
    bt._set_modified(True)
    dialogs.question_answer = QMessageBox.StandardButton.Cancel
    assert not bt.close()
    assert bt.isVisible()
    assert bt.IsExecuting()


def test_close_with_delete_on_close_shuts_down(qtbot, tmp_path, dialogs):
    SlowCancelable.cancelled.clear()
    widget = BehaviorTreeWidget(node_types=[SlowCancelable])
    widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    widget.show()
    assert widget.NewTree(str(tmp_path / "t.json"))
    slow = widget.AddNode("SlowCancelable", 0, 200)
    widget.Connect(widget.GetRootNode(), slow)
    widget.SetEntry(1, "a")
    store = widget.blackboardStore()
    ns = widget.GetBlackboardNamespace()
    fast_config(widget)
    widget.Execute()
    qtbot.waitUntil(lambda: slow.GetStatus() is NodeStatus.RUNNING, timeout=3000)
    destroyed = []
    widget.destroyed.connect(lambda *_: destroyed.append(True))
    dialogs.question_answer = QMessageBox.StandardButton.Discard
    with capture_exceptions() as exceptions:
        assert widget.close()
        assert store.is_disposed()
        assert SlowCancelable.cancelled.wait(2)

        def deleted() -> bool:
            # Flush deferred deletes explicitly: Qt < 6.7 does not run them inside nested loops.
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            return bool(destroyed)

        qtbot.waitUntil(deleted, timeout=2000)
    assert exceptions == []
    assert not [key for key in py_trees.blackboard.Blackboard.storage if key.startswith(ns + "/")]


def _destroy_without_shutdown(qtbot, tmp_path, with_entries: bool):
    SlowCancelable.cancelled.clear()
    widget = BehaviorTreeWidget(node_types=[SlowCancelable])
    widget.show()
    assert widget.NewTree(str(tmp_path / "t.json"))
    slow = widget.AddNode("SlowCancelable", 0, 200)
    widget.Connect(widget.GetRootNode(), slow)
    if with_entries:
        widget.SetEntry(1, "a")
        widget.SetEntry([1, 2], "items")
    ns = widget.GetBlackboardNamespace()
    client_name = widget.blackboardStore().client().unique_identifier
    fast_config(widget)
    widget.Execute()
    qtbot.waitUntil(lambda: slow.GetStatus() is NodeStatus.RUNNING, timeout=3000)
    with capture_exceptions() as exceptions:
        shiboken6.delete(widget)
        qtbot.wait(30)
    return SimpleNamespace(exceptions=exceptions, ns=ns, client=client_name)


def test_destroyed_without_shutdown_cancels_nodes_and_releases_keys(qtbot, tmp_path):
    result = _destroy_without_shutdown(qtbot, tmp_path, with_entries=False)
    assert result.exceptions == []
    assert SlowCancelable.cancelled.wait(2), "running nodes must be cancelled"
    assert result.client not in py_trees.blackboard.Blackboard.clients


def test_destroyed_without_shutdown_with_blackboard_entries_raises_nothing(qtbot, tmp_path):
    result = _destroy_without_shutdown(qtbot, tmp_path, with_entries=True)
    assert SlowCancelable.cancelled.wait(2), "running nodes must be cancelled"
    assert not [key for key in py_trees.blackboard.Blackboard.storage if key.startswith(result.ns + "/")]
    assert result.exceptions == [], (
        "destroying the widget without Shutdown() must not raise: " + "; ".join(str(e[1]) for e in result.exceptions)
    )


# ============================================================================ serialization (Widget / files section)
def test_frozenset_has_its_own_tag_and_round_trips():
    value = {"f": frozenset({1, 2}), "s": {3}}
    encoded = encode_value(value)
    assert encoded == {"f": {"__frozenset__": [1, 2]}, "s": {"__set__": [3]}}
    decoded = decode_value(encoded)
    assert type(decoded["f"]) is frozenset and type(decoded["s"]) is set
    assert decoded == value


def test_decoded_set_elements_and_dict_keys_become_frozensets():
    decoded = decode_value({"__set__": [{"__set__": [1, 2]}]})
    assert decoded == {frozenset({1, 2})}
    decoded = decode_value({"__dict__": [[{"__set__": [1]}, "v"]]})
    assert decoded == {frozenset({1}): "v"}


def test_circular_references_are_not_encodable():
    value = self_referencing_list()
    with pytest.raises(TypeError, match="circular"):
        encode_value(value)
    assert not is_encodable(value)
    mapping = {}
    mapping["self"] = mapping
    assert not is_encodable(mapping)
    shared = [1]
    assert is_encodable([shared, shared]), "a value referenced twice is not circular"


@pytest.mark.parametrize("payload", [[[1]], [[1, 2, 3]], [["a"]], ["ab"], [5]])
def test_malformed_dict_pairs_raise_tree_file_error(payload):
    with pytest.raises(TreeFileError):
        decode_value({"__dict__": payload})


def test_write_json_atomic_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.json"
    write_json_atomic(str(target), {"text": "x\ud800y"})
    with open(target, "rb") as stream:
        stream.read().decode("utf-8")
    assert read_json(target) == {"text": "x\ud800y"}
    (tmp_path / "adir.json").mkdir()
    with pytest.raises(OSError):
        write_json_atomic(str(tmp_path / "adir.json"), {"a": 1})
    assert sorted(os.listdir(tmp_path)) == ["adir.json", "out.json"]


def test_write_json_atomic_follows_symlinks(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted here")
    write_json_atomic(str(link), {"a": 1})
    assert os.path.islink(link)
    assert read_json(real) == {"a": 1}
