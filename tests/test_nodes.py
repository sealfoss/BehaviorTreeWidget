"""Tests of the node widgets defined in ``behavior_tree_widget.nodes``.

Covered areas:

* Title label (class ``_title``, class-name fallback, Root / composite display rules,
  thread-safe ``SetTitle``).
* Status label text and colour for every :class:`NodeStatus`.
* ParentConnection / ChildConnections labels: presence per node kind and the
  red (disconnected) / green (placing) / blue (connected) colour states.
* Leaf fields: one row per field made of a read-only key QLineEdit and a typed
  editor, immediate write-back into ``_fields``, list selections, thread-safe
  ``SetField`` / ``SetFieldSelectionIndex``, rebuilding rows, per-instance copies.
* Geometry: FrameFields hidden without fields, node height follows the number
  of fields and the Fields list shows every row without scrolling.
* ``GetChildren`` ordering, ``UnknownLeafNodeWidget``, custom ``UI_FILE`` values
  and the wheel guard of spin boxes / combo boxes.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
import uuid
from importlib import resources

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QLabel,
    QLineEdit,
    QListView,
    QSpinBox,
    QWidget,
)

from behavior_tree_widget import (
    CompositeNodeWidget,
    LeafNodeWidget,
    NodeStatus,
    NodeWidget,
    RootNodeWidget,
    Status,
    UnknownLeafNodeWidget,
)
from behavior_tree_widget import nodes as nodes_module
from behavior_tree_widget.nodes import (
    CHILDREN,
    CONNECTION_COLORS,
    PARENT,
    SELECTOR,
    SEQUENCE,
    STATUS_COLORS,
    ConnectionState,
    field_kind,
)

from conftest import (
    AllFields,
    FailNode,
    NoFields,
    Raise,
    SlowCancelable,
    Succeed,
    drag,
    fast_config,
    label_point,
    run_until_idle,
    viewport_point,
)

MAIN_THREAD = threading.get_ident()


# ============================================================================ helpers
def make_leaf_class(name: str, fields: dict | None = None, title: str | None = None, **attrs) -> type[LeafNodeWidget]:
    """Create a LeafNodeWidget subclass succeeding on the GUI thread.

    ``title`` None leaves ``_title`` unset on the class.
    """
    namespace: dict = {"RUN_IN_THREAD": False, "OnRun": lambda self, tree: True, "__module__": __name__}
    if fields is not None:
        namespace["_fields"] = fields
    if title is not None:
        namespace["_title"] = title
    namespace.update(attrs)
    return type(name, (LeafNodeWidget,), namespace)


def title_label(node: NodeWidget) -> QLabel:
    label = node.findChild(QLabel, "Title")
    assert label is not None
    return label


def status_label(node: NodeWidget) -> QLabel:
    label = node.findChild(QLabel, "Status")
    assert label is not None
    return label


def fields_view(node: NodeWidget) -> QListView:
    view = node.findChild(QListView, "Fields")
    assert view is not None
    return view


def fields_frame(node: NodeWidget) -> QFrame:
    frame = node.findChild(QFrame, "FrameFields")
    assert frame is not None
    return frame


def field_rows(node: NodeWidget) -> list[QWidget]:
    """Row widgets shown in the Fields list, in display order."""
    view = fields_view(node)
    model = view.model()
    if model is None:
        return []
    return [view.indexWidget(model.index(index, 0)) for index in range(model.rowCount())]


def row_of(node: NodeWidget, key: str) -> QWidget:
    """The row of the Fields list holding the editor of ``key``."""
    editor = node.field_editor(key)
    assert editor is not None, f"no editor for field {key!r}"
    rows = [row for row in field_rows(node) if row is not None and row.isAncestorOf(editor)]
    assert len(rows) == 1, f"editor of {key!r} is not in exactly one row of the Fields list"
    return rows[0]


def key_edits(node: NodeWidget, key: str) -> list[QLineEdit]:
    """Read-only QLineEdits of the row of ``key`` other than the value editor."""
    row = row_of(node, key)
    editor = node.field_editor(key)
    return [
        edit
        for edit in row.findChildren(QLineEdit)
        if edit.isReadOnly() and edit is not editor and not editor.isAncestorOf(edit)
    ]


def editor_value(editor: QWidget):
    if isinstance(editor, QCheckBox):
        return editor.isChecked()
    if isinstance(editor, (QSpinBox, QDoubleSpinBox)):
        return editor.value()
    if isinstance(editor, QComboBox):
        return [editor.itemText(i) for i in range(editor.count())], editor.currentIndex()
    if isinstance(editor, QLineEdit):
        return editor.text()
    raise AssertionError(f"unexpected editor {editor!r}")


def run_in_thread(function, *args) -> int:
    """Run ``function(*args)`` on a worker thread, wait for it and return the thread id."""
    errors: list[BaseException] = []
    ident: list[int] = []

    def target():
        ident.append(threading.get_ident())
        try:
            function(*args)
        except BaseException as error:  # noqa: BLE001 - re-raised below
            errors.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    if errors:
        raise errors[0]
    return ident[0]


class Recorder:
    """Records emissions of a signal (called directly in the emitting thread)."""

    def __init__(self, signal):
        self.args: list = []
        self.threads: list[int] = []
        signal.connect(self._slot, Qt.ConnectionType.DirectConnection)

    def _slot(self, *args):
        self.args.append(args[0] if len(args) == 1 else args)
        self.threads.append(threading.get_ident())


def send_wheel(widget: QWidget, delta: int = 120) -> None:
    pos = QPointF(widget.width() / 2.0, widget.height() / 2.0)
    event = QWheelEvent(
        pos,
        QPointF(widget.mapToGlobal(pos.toPoint())),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def packaged_ui_text(name: str) -> str:
    return resources.files("behavior_tree_widget").joinpath("ui", name).read_text(encoding="utf-8")


def write_ui(tmp_path, file_name: str, source: str, replacements: dict[str, str] | None = None) -> str:
    text = packaged_ui_text(source)
    for old, new in (replacements or {}).items():
        assert old in text, f"{old!r} not found in {source}"
        text = text.replace(old, new)
    path = tmp_path / file_name
    path.write_text(text, encoding="utf-8")
    return str(path)


def dominant_channel(color: str) -> str:
    qcolor = QColor(color)
    channels = {"red": qcolor.red(), "green": qcolor.green(), "blue": qcolor.blue()}
    return max(channels, key=channels.get)


# ============================================================================ titles
def test_leaf_title_label_shows_class_title(bt):
    node = bt.AddNode(AllFields, 0, 200)
    assert node.GetTitle() == "All Fields"
    assert title_label(node).text() == "All Fields"


@pytest.mark.parametrize("title", [None, ""], ids=["unset", "empty"])
def test_leaf_title_falls_back_to_class_name(bt, title):
    cls = make_leaf_class("UntitledLeaf", title=title)
    node = bt.AddNode(cls, 0, 200)
    assert node.GetTitle() == "UntitledLeaf"
    assert title_label(node).text() == "UntitledLeaf"


def test_leaf_title_without_tree(qtbot):
    node = make_leaf_class("PlainLeaf", title="Plain Title")()
    qtbot.addWidget(node)
    assert title_label(node).text() == "Plain Title"
    assert node.GetTitle() == "Plain Title"


def test_set_title_updates_label_and_emits_signal(bt):
    node = bt.AddNode(Succeed, 0, 200)
    recorder = Recorder(node.titleChanged)
    node.SetTitle("Renamed")
    assert node.GetTitle() == "Renamed"
    assert title_label(node).text() == "Renamed"
    assert recorder.args == ["Renamed"]
    # Setting the same title again is not a change.
    node.SetTitle("Renamed")
    assert recorder.args == ["Renamed"]


def test_long_title_is_fully_visible(bt):
    node = bt.AddNode(NoFields, 0, 200)
    old_width = node.width()
    title = "A considerably longer title than the default one"
    node.SetTitle(title)
    label = title_label(node)
    assert label.text() == title
    assert node.width() > old_width
    assert label.width() >= label.fontMetrics().horizontalAdvance(title)
    assert node._item.size().toSize() == node.size()


@pytest.mark.parametrize("composite_type", [SEQUENCE, SELECTOR])
def test_root_display_title(qtbot, composite_type):
    root = RootNodeWidget(composite_type)
    qtbot.addWidget(root)
    assert root.GetTitle() == "Root"
    assert title_label(root).text() == f"Root ({composite_type})"


def test_default_tree_root_shows_root_sequence(bt):
    root = bt.GetRootNode()
    assert isinstance(root, RootNodeWidget)
    assert title_label(root).text() == "Root (Sequence)"


def test_root_set_composite_type_updates_display(bt):
    root = bt.GetRootNode()
    recorder = Recorder(root.titleChanged)
    root.SetCompositeType(SELECTOR)
    assert root.GetCompositeType() == SELECTOR
    assert root.GetTitle() == "Root"
    assert title_label(root).text() == "Root (Selector)"
    assert recorder.args  # the displayed title changed
    root.SetCompositeType(SEQUENCE)
    assert title_label(root).text() == "Root (Sequence)"


def test_root_custom_title_keeps_type_suffix(bt):
    root = bt.GetRootNode()
    root.SetTitle("Mission")
    assert title_label(root).text() == "Mission (Sequence)"
    root.SetCompositeType(SELECTOR)
    assert title_label(root).text() == "Mission (Selector)"


@pytest.mark.parametrize("composite_type", [SEQUENCE, SELECTOR])
def test_composite_default_title_is_its_type(bt, composite_type):
    node = bt.AddNode(composite_type, 0, 200)
    assert isinstance(node, CompositeNodeWidget)
    assert node.GetTitle() == composite_type
    assert title_label(node).text() == composite_type


@pytest.mark.parametrize("composite_type", [SEQUENCE, SELECTOR])
def test_composite_custom_title_shows_type_in_parentheses(bt, composite_type):
    node = bt.AddNode(composite_type, 0, 200)
    node.SetTitle("Check doors")
    assert node.GetTitle() == "Check doors"
    assert title_label(node).text() == f"Check doors ({composite_type})"


def test_composite_switch_type_with_default_title_follows_type(bt):
    node = bt.AddNode(SEQUENCE, 0, 200)
    node.SetCompositeType(SELECTOR)
    assert node.GetCompositeType() == SELECTOR
    assert node.GetTitle() == SELECTOR
    assert title_label(node).text() == SELECTOR
    node.SetCompositeType(SEQUENCE)
    assert node.GetTitle() == SEQUENCE
    assert title_label(node).text() == SEQUENCE


def test_composite_switch_type_keeps_custom_title(bt):
    node = bt.AddNode(SEQUENCE, 0, 200)
    node.SetTitle("Doors")
    node.SetCompositeType(SELECTOR)
    assert node.GetTitle() == "Doors"
    assert title_label(node).text() == "Doors (Selector)"


def test_composite_title_naming_the_other_type_shows_real_type(bt):
    node = bt.AddNode(SELECTOR, 0, 200)
    node.SetTitle(SEQUENCE)
    assert title_label(node).text() == "Sequence (Selector)"


def test_composite_type_validation(qtbot, bt):
    with pytest.raises(ValueError):
        CompositeNodeWidget("Parallel")
    node = bt.AddNode(SEQUENCE, 0, 200)
    with pytest.raises(ValueError):
        node.SetCompositeType("Parallel")
    assert node.GetCompositeType() == SEQUENCE


@pytest.mark.parametrize("node_kind", ["leaf", "composite", "root"])
def test_set_title_from_worker_thread_is_applied_on_gui_thread(qtbot, bt, node_kind):
    if node_kind == "leaf":
        node = bt.AddNode(Succeed, 0, 200)
        expected = "From thread"
    elif node_kind == "composite":
        node = bt.AddNode(SEQUENCE, 0, 200)
        expected = "From thread (Sequence)"
    else:
        node = bt.GetRootNode()
        expected = "From thread (Sequence)"
    label = title_label(node)
    old_text = label.text()
    recorder = Recorder(node.titleChanged)

    worker = run_in_thread(node.SetTitle, "From thread")

    assert worker != MAIN_THREAD
    # The value is available at once (thread-safe) ...
    assert node.GetTitle() == "From thread"
    # ... but the widget was not touched from the worker thread: the GUI thread was
    # blocked in join() and has not processed events yet.
    assert label.text() == old_text
    qtbot.waitUntil(lambda: label.text() == expected, timeout=2000)
    assert recorder.args == ["From thread"]
    assert recorder.threads == [MAIN_THREAD]


# ============================================================================ status label
@pytest.mark.parametrize("factory", ["root", "composite", "leaf"])
def test_initial_status_is_ready(bt, factory):
    node = {"root": bt.GetRootNode, "composite": lambda: bt.AddNode(SELECTOR, 0, 200),
            "leaf": lambda: bt.AddNode(Succeed, 0, 200)}[factory]()
    assert node.GetStatus() is NodeStatus.READY
    assert status_label(node).text() == "Ready"
    assert STATUS_COLORS[NodeStatus.READY] in status_label(node).styleSheet()


@pytest.mark.parametrize("status", list(NodeStatus), ids=[s.value for s in NodeStatus])
def test_status_label_text_and_colour(bt, status):
    node = bt.AddNode(Succeed, 0, 200)
    # Start from a different state so that every target status is a change.
    node._set_status(NodeStatus.RUNNING if status is not NodeStatus.RUNNING else NodeStatus.FAILED)
    recorder = Recorder(node.statusChanged)
    node._set_status(status)
    label = status_label(node)
    assert node.GetStatus() is status
    assert label.text() == status.value
    assert STATUS_COLORS[status] in label.styleSheet()
    assert recorder.args == [status.value]


@pytest.mark.parametrize("status", list(NodeStatus), ids=[s.value for s in NodeStatus])
def test_status_text_is_not_clipped_on_a_narrow_node(qtbot, bt, status):
    node = bt.AddNode(make_leaf_class("Tiny", title="A"), 0, 200)
    label = status_label(node)
    node._set_status(status)

    def fits() -> bool:
        label.ensurePolished()
        return label.width() >= label.fontMetrics().horizontalAdvance(status.value)

    qtbot.waitUntil(fits, timeout=2000)
    assert label.text() == status.value
    assert label.mapTo(node, QPoint(label.width(), 0)).x() <= node.width()
    qtbot.waitUntil(lambda: node._item.size().toSize() == node.size(), timeout=2000)


def test_status_texts_and_colours_are_the_required_ones():
    assert [status.value for status in NodeStatus] == ["Ready", "Running", "Succeeded", "Failed"]
    assert set(STATUS_COLORS) == set(NodeStatus)
    assert len(set(STATUS_COLORS.values())) == len(NodeStatus)


@pytest.mark.parametrize(
    "py_trees_status, expected",
    [
        (Status.SUCCESS, NodeStatus.SUCCEEDED),
        (Status.FAILURE, NodeStatus.FAILED),
        (Status.RUNNING, NodeStatus.RUNNING),
        (Status.INVALID, NodeStatus.READY),
    ],
)
def test_node_status_from_py_trees(py_trees_status, expected):
    assert NodeStatus.from_py_trees(py_trees_status) is expected


@pytest.mark.parametrize("leaf_class, expected", [(Succeed, "Succeeded"), (FailNode, "Failed")])
def test_status_label_after_execution(qtbot, bt, leaf_class, expected):
    root = bt.GetRootNode()
    leaf = bt.AddNode(leaf_class, 0, 200)
    bt.Connect(root, leaf)
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert status_label(leaf).text() == expected
    assert status_label(root).text() == expected
    assert STATUS_COLORS[NodeStatus(expected)] in status_label(leaf).styleSheet()


def test_status_label_running_then_ready_after_stop(qtbot, bt):
    root = bt.GetRootNode()
    leaf = bt.AddNode(SlowCancelable, 0, 200)
    bt.Connect(root, leaf)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: status_label(leaf).text() == "Running", timeout=3000)
    assert status_label(root).text() == "Running"
    assert STATUS_COLORS[NodeStatus.RUNNING] in status_label(leaf).styleSheet()
    bt.Stop()
    qtbot.waitUntil(lambda: status_label(leaf).text() == "Ready", timeout=3000)
    assert status_label(root).text() == "Ready"


def test_failed_status_from_exception_keeps_error(qtbot, bt):
    root = bt.GetRootNode()
    leaf = bt.AddNode(Raise, 0, 200)
    bt.Connect(root, leaf)
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert status_label(leaf).text() == "Failed"
    assert leaf.GetError() and "boom" in leaf.GetError()
    assert "boom" in status_label(leaf).toolTip()


# ============================================================================ connection labels
def test_connection_colours_are_red_green_blue():
    assert dominant_channel(CONNECTION_COLORS[ConnectionState.DISCONNECTED]) == "red"
    assert dominant_channel(CONNECTION_COLORS[ConnectionState.PLACING]) == "green"
    assert dominant_channel(CONNECTION_COLORS[ConnectionState.CONNECTED]) == "blue"


@pytest.mark.parametrize("state", list(ConnectionState), ids=[s.name for s in ConnectionState])
@pytest.mark.parametrize(
    "node_kind, kind",
    [("leaf", PARENT), ("composite", PARENT), ("composite", CHILDREN), ("root", CHILDREN)],
)
def test_set_connection_state_colours_label(bt, node_kind, kind, state):
    node = {"leaf": lambda: bt.AddNode(Succeed, 0, 200), "composite": lambda: bt.AddNode(SEQUENCE, 300, 200),
            "root": bt.GetRootNode}[node_kind]()
    label = node.connection_label(kind)
    assert label is not None
    assert label.objectName() == ("ParentConnection" if kind == PARENT else "ChildConnections")
    node.set_connection_state(kind, state)
    assert node.connection_state(kind) is state
    assert CONNECTION_COLORS[state] in label.styleSheet()
    for other in ConnectionState:
        if other is not state:
            assert CONNECTION_COLORS[other] not in label.styleSheet()


def test_new_nodes_start_disconnected_red(bt):
    root = bt.GetRootNode()
    composite = bt.AddNode(SELECTOR, 300, 200)
    leaf = bt.AddNode(Succeed, 0, 200)
    red = CONNECTION_COLORS[ConnectionState.DISCONNECTED]
    for node, kind in ((root, CHILDREN), (composite, PARENT), (composite, CHILDREN), (leaf, PARENT)):
        assert node.connection_state(kind) is ConnectionState.DISCONNECTED
        assert red in node.connection_label(kind).styleSheet()


def test_connect_turns_labels_blue_and_disconnect_turns_them_red(bt):
    root = bt.GetRootNode()
    composite = bt.AddNode(SEQUENCE, 0, 200)
    leaf = bt.AddNode(Succeed, 0, 400)
    blue = CONNECTION_COLORS[ConnectionState.CONNECTED]
    red = CONNECTION_COLORS[ConnectionState.DISCONNECTED]

    bt.Connect(root, composite)
    bt.Connect(composite, leaf)
    for node, kind in ((root, CHILDREN), (composite, PARENT), (composite, CHILDREN), (leaf, PARENT)):
        assert node.connection_state(kind) is ConnectionState.CONNECTED
        assert blue in node.connection_label(kind).styleSheet()

    bt.Disconnect(leaf)
    assert leaf.connection_state(PARENT) is ConnectionState.DISCONNECTED
    assert red in leaf.connection_label(PARENT).styleSheet()
    assert composite.connection_state(CHILDREN) is ConnectionState.DISCONNECTED
    assert red in composite.connection_label(CHILDREN).styleSheet()
    assert composite.connection_state(PARENT) is ConnectionState.CONNECTED


@pytest.mark.parametrize("from_parent_side", [True, False], ids=["from-children-label", "from-parent-label"])
def test_labels_green_while_placing_then_blue(bt, from_parent_side):
    root = bt.GetRootNode()
    leaf = bt.AddNode(Succeed, 0, 200)
    bt.view().centerOn(50, 150)
    start, end = label_point(bt, root, CHILDREN), label_point(bt, leaf, PARENT)
    if not from_parent_side:
        start, end = end, start
    green = CONNECTION_COLORS[ConnectionState.PLACING]

    drag(bt, start, end, release=False)
    try:
        assert root.connection_state(CHILDREN) is ConnectionState.PLACING
        assert green in root.connection_label(CHILDREN).styleSheet()
        # Hovering a compatible label marks it as being placed too.
        assert leaf.connection_state(PARENT) is ConnectionState.PLACING
        assert green in leaf.connection_label(PARENT).styleSheet()
    finally:
        QTest.mouseRelease(bt.view().viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)

    assert leaf.GetParent() is root
    assert root.connection_state(CHILDREN) is ConnectionState.CONNECTED
    assert leaf.connection_state(PARENT) is ConnectionState.CONNECTED


def test_cancelled_placing_returns_label_to_red(bt):
    leaf = bt.AddNode(Succeed, 0, 200)
    bt.view().centerOn(50, 150)
    start = label_point(bt, leaf, PARENT)
    empty = bt.view().mapFromScene(QPointF(-350.0, 420.0))

    drag(bt, start, empty, release=False)
    try:
        assert leaf.connection_state(PARENT) is ConnectionState.PLACING
    finally:
        QTest.mouseRelease(bt.view().viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, empty)

    assert leaf.GetParent() is None
    assert leaf.connection_state(PARENT) is ConnectionState.DISCONNECTED
    assert CONNECTION_COLORS[ConnectionState.DISCONNECTED] in leaf.connection_label(PARENT).styleSheet()


def test_cancelled_placing_from_connected_label_returns_to_blue(bt):
    root = bt.GetRootNode()
    leaf = bt.AddNode(Succeed, 0, 200)
    bt.Connect(root, leaf)
    bt.view().centerOn(50, 150)
    start = label_point(bt, root, CHILDREN)
    empty = bt.view().mapFromScene(QPointF(-350.0, 420.0))

    drag(bt, start, empty, release=False)
    try:
        assert root.connection_state(CHILDREN) is ConnectionState.PLACING
        assert CONNECTION_COLORS[ConnectionState.PLACING] in root.connection_label(CHILDREN).styleSheet()
    finally:
        QTest.mouseRelease(bt.view().viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, empty)

    assert root.GetChildren() == [leaf]
    assert root.connection_state(CHILDREN) is ConnectionState.CONNECTED
    assert CONNECTION_COLORS[ConnectionState.CONNECTED] in root.connection_label(CHILDREN).styleSheet()


def test_root_has_no_parent_connection(bt):
    root = bt.GetRootNode()
    assert not RootNodeWidget.HasParentConnection()
    assert root.connection_label(PARENT) is None
    assert root.connection_state(PARENT) is None
    labels = [label for label in root.findChildren(QLabel, "ParentConnection") if label.isVisibleTo(root)]
    assert labels == []
    root.set_connection_state(PARENT, ConnectionState.CONNECTED)  # no-op, must not fail
    assert root.connection_state(PARENT) is None
    # The root still has a visible ChildConnections label.
    assert root.connection_label(CHILDREN) is not None
    assert root.connection_label(CHILDREN).isVisibleTo(root)


@pytest.mark.parametrize("leaf_class", [Succeed, AllFields, NoFields])
def test_leaf_has_no_child_connections(bt, leaf_class):
    leaf = bt.AddNode(leaf_class, 0, 200)
    assert not leaf.HasChildConnections()
    assert leaf.connection_label(CHILDREN) is None
    assert leaf.connection_state(CHILDREN) is None
    visible = [
        label
        for name in ("ChildConnections", "ChildConnnections")
        for label in leaf.findChildren(QLabel, name)
        if label.isVisibleTo(leaf)
    ]
    assert visible == []
    assert leaf.connection_label(PARENT) is not None
    assert leaf.connection_label(PARENT).isVisibleTo(leaf)


def test_composite_has_both_connection_labels(bt):
    node = bt.AddNode(SEQUENCE, 0, 200)
    for kind in (PARENT, CHILDREN):
        label = node.connection_label(kind)
        assert label is not None and label.isVisibleTo(node)


# ============================================================================ fields: editors
@pytest.mark.parametrize(
    "value, editor_type",
    [
        (True, QCheckBox),
        (False, QCheckBox),
        (0, QSpinBox),
        (-42, QSpinBox),
        (2.5, QDoubleSpinBox),
        ("text", QLineEdit),
        ("", QLineEdit),
        (["a", "b"], QComboBox),
    ],
    ids=["bool-true", "bool-false", "int-zero", "int-negative", "float", "str", "str-empty", "list"],
)
def test_field_editor_type_and_initial_value(bt, value, editor_type):
    cls = make_leaf_class("OneField", {"value": value})
    node = bt.AddNode(cls, 0, 200)
    editor = node.field_editor("value")
    assert type(editor) is editor_type
    assert editor.isEnabled()
    if editor_type is QCheckBox:
        assert editor.isChecked() is value
    elif editor_type in (QSpinBox, QDoubleSpinBox):
        assert editor.value() == pytest.approx(value)
    elif editor_type is QLineEdit:
        assert editor.text() == value
        assert not editor.isReadOnly()
    else:
        assert editor_value(editor) == (value, 0)


def test_bool_field_is_checkbox_not_spinbox(bt):
    node = bt.AddNode(AllFields, 0, 200)
    editor = node.field_editor("enabled")
    assert isinstance(editor, QCheckBox)
    assert not isinstance(editor, QAbstractSpinBox)
    assert node.findChildren(QCheckBox) == [editor]
    # Exactly one QSpinBox (count) and one QDoubleSpinBox (ratio).
    assert [type(w) for w in node.findChildren(QAbstractSpinBox)].count(QSpinBox) == 1
    assert [type(w) for w in node.findChildren(QAbstractSpinBox)].count(QDoubleSpinBox) == 1


def test_all_fields_editor_types(bt):
    node = bt.AddNode(AllFields, 0, 200)
    expected = {"count": QSpinBox, "ratio": QDoubleSpinBox, "label": QLineEdit, "enabled": QCheckBox, "choice": QComboBox}
    for key, editor_type in expected.items():
        assert type(node.field_editor(key)) is editor_type, key


def test_list_field_combo_lists_str_of_each_item(bt):
    items = [1, 2.5, "x", None, (1, 2), True]
    cls = make_leaf_class("MixedList", {"items": items})
    node = bt.AddNode(cls, 0, 200)
    combo = node.field_editor("items")
    assert isinstance(combo, QComboBox)
    assert [combo.itemText(i) for i in range(combo.count())] == [str(item) for item in items]
    assert combo.currentIndex() == 0
    assert node.GetFieldSelectionIndex("items") == 0
    assert node.GetFieldSelection("items") == 1


@pytest.mark.parametrize(
    "value", [{"a": 1}, (1, 2), None, {1, 2}], ids=["dict", "tuple", "none", "set"]
)
def test_unsupported_field_type_is_read_only_disabled_line_edit(bt, value):
    cls = make_leaf_class("OddField", {"odd": value, "fine": 1})
    node = bt.AddNode(cls, 0, 200)
    editor = node.field_editor("odd")
    assert type(editor) is QLineEdit
    assert editor.isReadOnly()
    assert not editor.isEnabled()
    assert editor.text() == repr(value)
    # The row still pairs the key with the editor.
    assert [edit.text() for edit in key_edits(node, "odd")] == ["odd"]
    assert node.GetField("odd") == value


def test_each_row_pairs_read_only_key_line_edit_with_editor(bt):
    node = bt.AddNode(AllFields, 0, 200)
    keys = list(AllFields._fields)
    rows = field_rows(node)
    assert len(rows) == len(keys)
    for index, key in enumerate(keys):
        row = row_of(node, key)
        assert rows[index] is row, f"row {index} does not hold field {key!r}"
        edits = key_edits(node, key)
        assert len(edits) == 1
        key_edit = edits[0]
        assert key_edit.text() == key
        assert key_edit.isReadOnly()
        # The key is shown next to (left of) the value editor on the same line.
        editor = node.field_editor(key)
        key_rect = QRect(key_edit.mapTo(row, QPoint(0, 0)), key_edit.size())
        editor_rect = QRect(editor.mapTo(row, QPoint(0, 0)), editor.size())
        assert key_rect.right() < editor_rect.left()
        assert key_rect.top() < editor_rect.bottom() and editor_rect.top() < key_rect.bottom()


def test_rows_live_in_fields_list_inside_frame_fields(bt):
    node = bt.AddNode(AllFields, 0, 200)
    view = fields_view(node)
    assert fields_frame(node).isAncestorOf(view)
    for key in AllFields._fields:
        assert view.isAncestorOf(node.field_editor(key))


# ============================================================================ fields: editing
@pytest.mark.parametrize(
    "key, edit, expected",
    [
        ("count", lambda e: e.setValue(7), 7),
        ("ratio", lambda e: e.setValue(1.25), 1.25),
        ("label", lambda e: e.setText("world"), "world"),
        ("enabled", lambda e: e.setChecked(False), False),
    ],
    ids=["int", "float", "str", "bool"],
)
def test_editor_change_updates_fields_immediately(bt, key, edit, expected):
    node = bt.AddNode(AllFields, 0, 200)
    changed = Recorder(node.fieldChanged)
    edited = Recorder(node.fieldEdited)
    edit(node.field_editor(key))
    assert node._fields[key] == expected
    assert type(node._fields[key]) is type(expected)
    assert node.GetField(key) == expected
    assert changed.args == [key]
    assert edited.args == [key]


def test_typing_in_string_editor_updates_field_on_each_key(bt):
    node = bt.AddNode(AllFields, 0, 200)
    editor = node.field_editor("label")
    editor.clear()
    assert node._fields["label"] == ""
    QTest.keyClick(editor, Qt.Key.Key_A)
    assert node._fields["label"] == "a"
    QTest.keyClicks(editor, "bc")
    assert node._fields["label"] == "abc"


def test_keyboard_step_in_spin_boxes_updates_fields(bt):
    node = bt.AddNode(AllFields, 0, 200)
    QTest.keyClick(node.field_editor("count"), Qt.Key.Key_Up)
    assert node._fields["count"] == 4
    QTest.keyClick(node.field_editor("ratio"), Qt.Key.Key_Down)
    assert node._fields["ratio"] == pytest.approx(0.4)


def test_clicking_checkbox_indicator_updates_field(bt):
    node = bt.AddNode(AllFields, 0, 200)
    checkbox = node.field_editor("enabled")
    QTest.mouseClick(checkbox, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(6, checkbox.height() // 2))
    assert checkbox.isChecked() is False
    assert node._fields["enabled"] is False


def test_list_selection_updates_selection_accessors(bt):
    node = bt.AddNode(AllFields, 0, 200)
    changed = Recorder(node.fieldChanged)
    edited = Recorder(node.fieldEdited)
    combo = node.field_editor("choice")
    combo.setCurrentIndex(2)
    assert node.GetFieldSelectionIndex("choice") == 2
    assert node.GetFieldSelection("choice") == "blue"
    # The list itself is unchanged.
    assert node._fields["choice"] == ["red", "green", "blue"]
    assert changed.args == ["choice"]
    assert edited.args == ["choice"]


def test_list_selection_returns_original_item(bt):
    cls = make_leaf_class("NumberChoice", {"n": [10, 20, 30]})
    node = bt.AddNode(cls, 0, 200)
    node.field_editor("n").setCurrentIndex(1)
    assert node.GetFieldSelection("n") == 20
    assert isinstance(node.GetFieldSelection("n"), int)


def test_empty_list_field(bt):
    cls = make_leaf_class("EmptyChoice", {"n": []})
    node = bt.AddNode(cls, 0, 200)
    combo = node.field_editor("n")
    assert isinstance(combo, QComboBox)
    assert combo.count() == 0
    assert node.GetFieldSelectionIndex("n") == -1
    assert node.GetFieldSelection("n") is None


def test_selection_api_errors(bt):
    node = bt.AddNode(AllFields, 0, 200)
    with pytest.raises(TypeError):
        node.GetFieldSelectionIndex("count")
    with pytest.raises(TypeError):
        node.SetFieldSelectionIndex("label", 0)
    with pytest.raises(IndexError):
        node.SetFieldSelectionIndex("choice", 3)
    with pytest.raises(IndexError):
        node.SetFieldSelectionIndex("choice", -1)
    with pytest.raises(KeyError):
        node.GetField("missing")


def test_editing_field_marks_tree_modified(bt):
    node = bt.AddNode(AllFields, 0, 200)
    bt._set_modified(False)
    node.field_editor("count").setValue(12)
    assert bt.IsModified()


# ============================================================================ fields: SetField
SET_FIELD_CASES = [
    ("count", 11, 11),
    ("ratio", 3.5, 3.5),
    ("label", "bye", "bye"),
    ("enabled", False, False),
    ("choice", ["x", "y"], (["x", "y"], 0)),
]
SET_FIELD_IDS = ["int", "float", "str", "bool", "list"]


@pytest.mark.parametrize("key, value, shown", SET_FIELD_CASES, ids=SET_FIELD_IDS)
def test_set_field_on_gui_thread_updates_editor(bt, key, value, shown):
    node = bt.AddNode(AllFields, 0, 200)
    editor = node.field_editor(key)
    changed = Recorder(node.fieldChanged)
    edited = Recorder(node.fieldEdited)
    node.SetField(key, value)
    assert node.field_editor(key) is editor  # same kind: editor updated in place
    assert editor_value(editor) == shown
    assert node.GetField(key) == value
    assert changed.args == [key]
    assert edited.args == []  # programmatic changes are not user edits


@pytest.mark.parametrize("key, value, shown", SET_FIELD_CASES, ids=SET_FIELD_IDS)
def test_set_field_from_worker_thread_updates_editor(qtbot, bt, key, value, shown):
    node = bt.AddNode(AllFields, 0, 200)
    editor = node.field_editor(key)
    changed = Recorder(node.fieldChanged)
    run_in_thread(node.SetField, key, value)
    assert node.GetField(key) == value  # stored at once
    qtbot.waitUntil(lambda: editor_value(node.field_editor(key)) == shown, timeout=2000)
    qtbot.waitUntil(lambda: changed.args == [key], timeout=2000)
    assert changed.threads == [MAIN_THREAD]


def test_set_field_selection_index_on_gui_thread(bt):
    node = bt.AddNode(AllFields, 0, 200)
    combo = node.field_editor("choice")
    changed = Recorder(node.fieldChanged)
    edited = Recorder(node.fieldEdited)
    node.SetFieldSelectionIndex("choice", 1)
    assert combo.currentIndex() == 1
    assert node.GetFieldSelection("choice") == "green"
    assert changed.args == ["choice"]
    assert edited.args == []


def test_set_field_selection_index_from_worker_thread(qtbot, bt):
    node = bt.AddNode(AllFields, 0, 200)
    combo = node.field_editor("choice")
    changed = Recorder(node.fieldChanged)
    run_in_thread(node.SetFieldSelectionIndex, "choice", 2)
    assert node.GetFieldSelectionIndex("choice") == 2
    qtbot.waitUntil(lambda: combo.currentIndex() == 2, timeout=2000)
    qtbot.waitUntil(lambda: changed.args == ["choice"], timeout=2000)
    assert changed.threads == [MAIN_THREAD]


def test_set_field_shorter_list_keeps_valid_selection(bt):
    node = bt.AddNode(AllFields, 0, 200)
    node.SetFieldSelectionIndex("choice", 2)
    node.SetField("choice", ["only", "two"])
    combo = node.field_editor("choice")
    index = node.GetFieldSelectionIndex("choice")
    assert 0 <= index < 2
    assert combo.currentIndex() == index
    assert node.GetFieldSelection("choice") == ["only", "two"][index]


@pytest.mark.parametrize(
    "key, value, editor_type",
    [
        ("count", "three", QLineEdit),
        ("ratio", True, QCheckBox),
        ("label", 4, QSpinBox),
        ("enabled", 1, QSpinBox),
        ("choice", 1.5, QDoubleSpinBox),
        ("count", ["a", "b"], QComboBox),
    ],
    ids=["int->str", "float->bool", "str->int", "bool->int", "list->float", "int->list"],
)
def test_set_field_with_different_kind_rebuilds_row(bt, key, value, editor_type):
    node = bt.AddNode(AllFields, 0, 200)
    node.SetField(key, value)
    editor = node.field_editor(key)
    assert type(editor) is editor_type
    shown = editor_value(editor)
    if editor_type is QComboBox:
        assert shown == ([str(item) for item in value], 0)
    else:
        assert shown == value
    assert node.GetField(key) == value
    assert len(field_rows(node)) == len(AllFields._fields)
    assert [edit.text() for edit in key_edits(node, key)] == [key]
    # Editing the new editor writes back into _fields.
    if editor_type is QLineEdit:
        editor.setText("four")
        assert node._fields[key] == "four"
    elif editor_type is QCheckBox:
        editor.setChecked(False)
        assert node._fields[key] is False
    elif editor_type is QSpinBox:
        editor.setValue(9)
        assert node._fields[key] == 9
    elif editor_type is QDoubleSpinBox:
        editor.setValue(2.25)
        assert node._fields[key] == 2.25
    else:
        editor.setCurrentIndex(1)
        assert node.GetFieldSelection(key) == "b"


def test_set_field_with_new_key_adds_row_and_grows_node(bt):
    node = bt.AddNode(AllFields, 0, 200)
    old_height = node.height()
    node.SetField("extra", 9)
    assert isinstance(node.field_editor("extra"), QSpinBox)
    assert node.field_editor("extra").value() == 9
    assert len(field_rows(node)) == len(AllFields._fields) + 1
    assert field_rows(node)[-1] is row_of(node, "extra")
    assert [edit.text() for edit in key_edits(node, "extra")] == ["extra"]
    assert node.height() > old_height
    assert node._item.size().toSize() == node.size()
    # The other editors still work after the rebuild.
    node.field_editor("count").setValue(21)
    assert node._fields["count"] == 21


def test_rebuild_leaves_exactly_one_visible_row_per_field(qtbot, bt):
    node = bt.AddNode(AllFields, 0, 200)
    node.SetField("count", "now text")
    node.SetField("extra", 1.5)
    qtbot.wait(10)
    keys = [*AllFields._fields, "extra"]
    visible_keys = [
        edit.text()
        for edit in node.findChildren(QLineEdit)
        if edit.isReadOnly() and edit.isEnabled() and edit.isVisibleTo(node)
    ]
    assert sorted(visible_keys) == sorted(keys)
    visible_spin_boxes = {w for w in node.findChildren(QAbstractSpinBox) if w.isVisibleTo(node)}
    assert visible_spin_boxes == {node.field_editor("ratio"), node.field_editor("extra")}


def test_rebuild_keeps_values_and_list_selection(bt):
    node = bt.AddNode(AllFields, 0, 200)
    node.field_editor("choice").setCurrentIndex(2)
    node.field_editor("count").setValue(17)
    node.field_editor("enabled").setChecked(False)
    node.SetField("extra", "rebuild")  # new key: every row is rebuilt
    assert node.field_editor("choice").currentIndex() == 2
    assert node.GetFieldSelection("choice") == "blue"
    assert node.field_editor("count").value() == 17
    assert node.field_editor("enabled").isChecked() is False
    assert node.field_editor("label").text() == "hello"


def test_set_field_new_key_from_worker_thread(qtbot, bt):
    node = bt.AddNode(AllFields, 0, 200)
    run_in_thread(node.SetField, "late", "value")
    qtbot.waitUntil(lambda: node.field_editor("late") is not None, timeout=2000)
    assert isinstance(node.field_editor("late"), QLineEdit)
    assert node.field_editor("late").text() == "value"
    assert len(field_rows(node)) == len(AllFields._fields) + 1


def test_queued_updates_for_a_removed_node_are_harmless(qtbot, bt):
    node = bt.AddNode(AllFields, 0, 200)
    run_in_thread(node.SetTitle, "Gone")
    run_in_thread(node.SetField, "count", 5)
    run_in_thread(node.SetField, "brand_new", 1)
    bt.RemoveNode(node)
    qtbot.wait(50)  # queued calls are dropped or skipped without raising
    assert node not in bt.GetNodes()


def test_set_field_on_node_without_fields_shows_frame(bt):
    node = bt.AddNode(NoFields, 0, 200)
    assert not fields_frame(node).isVisibleTo(node)
    old_height = node.height()
    node.SetField("added", True)
    assert fields_frame(node).isVisibleTo(node)
    assert isinstance(node.field_editor("added"), QCheckBox)
    assert node.height() > old_height
    assert node._item.size().toSize() == node.size()


# ============================================================================ fields: copies
def test_class_fields_template_is_deep_copied_per_instance(bt):
    original = {"count": 3, "ratio": 0.5, "label": "hello", "enabled": True, "choice": ["red", "green", "blue"]}
    first = bt.AddNode(AllFields, 0, 200)
    first._fields["choice"].append("purple")
    first._fields["count"] = 100
    second = bt.AddNode(AllFields, 300, 200)
    assert AllFields._fields == original
    assert second._fields == original
    assert second._fields["choice"] is not AllFields._fields["choice"]
    assert first._fields["choice"] is not second._fields["choice"]


def test_nested_field_templates_are_deep_copied(qtbot):
    cls = make_leaf_class("Nested", {"matrix": [[1, 2], [3, 4]]})
    first, second = cls(), cls()
    qtbot.addWidget(first)
    qtbot.addWidget(second)
    first._fields["matrix"][0].append(99)
    assert cls._fields["matrix"] == [[1, 2], [3, 4]]
    assert second._fields["matrix"] == [[1, 2], [3, 4]]


def test_editing_one_instance_does_not_affect_others(bt):
    first = bt.AddNode(AllFields, 0, 200)
    second = bt.AddNode(AllFields, 300, 200)
    first.field_editor("count").setValue(99)
    first.field_editor("choice").setCurrentIndex(2)
    first.SetField("label", "changed")
    assert second.GetField("count") == 3
    assert second.GetFieldSelectionIndex("choice") == 0
    assert second.GetField("label") == "hello"
    assert AllFields._fields["count"] == 3
    assert AllFields._fields["label"] == "hello"


def test_field_getters_return_copies(bt):
    node = bt.AddNode(AllFields, 0, 200)
    node.GetField("choice").append("x")
    node.GetFields()["choice"].append("y")
    node.GetFields()["count"] = 0
    assert node.GetField("choice") == ["red", "green", "blue"]
    assert node.GetField("count") == 3


def test_load_dict_applies_saved_values_over_defaults(bt):
    node = bt.AddNode(AllFields, 0, 200)
    node._load_dict(
        {
            "fields": {
                "count": 8,  # same kind: applied
                "ratio": 2,  # int into a float field: converted
                "label": 5,  # wrong kind: ignored
                "enabled": False,
                "choice": ["a", "b", "c", "d"],
                "removed": "x",  # no longer declared: ignored
            },
            "field_selections": {"choice": 3},
        }
    )
    assert node.GetFields() == {
        "count": 8, "ratio": 2.0, "label": "hello", "enabled": False, "choice": ["a", "b", "c", "d"],
    }
    assert type(node.GetField("ratio")) is float
    assert node.GetFieldSelection("choice") == "d"
    assert node.field_editor("count").value() == 8
    assert node.field_editor("ratio").value() == 2.0
    assert node.field_editor("enabled").isChecked() is False
    assert node.field_editor("choice").currentIndex() == 3
    assert node.field_editor("removed") is None


def test_load_dict_clamps_out_of_range_selection(bt):
    node = bt.AddNode(AllFields, 0, 200)
    node._load_dict({"field_selections": {"choice": 99}})
    assert node.GetFieldSelectionIndex("choice") == 2
    assert node.field_editor("choice").currentIndex() == 2


def test_unknown_leaf_shrinks_when_its_fields_go_away(qtbot):
    node = UnknownLeafNodeWidget("Mystery")
    qtbot.addWidget(node)
    node._load_dict({"fields": {"a": 1, "b": 2.0, "c": "x"}})
    assert fields_frame(node).isVisibleTo(node)
    tall = node.height()
    node._load_dict({"fields": {}})
    assert not fields_frame(node).isVisibleTo(node)
    assert node.height() < tall


@pytest.mark.parametrize(
    "value, kind",
    [(True, "bool"), (0, "int"), (1.0, "float"), ("s", "str"), ([], "list"), ({}, "other"), ((), "other")],
)
def test_field_kind(value, kind):
    assert field_kind(value) == kind


# ============================================================================ geometry
def test_frame_fields_hidden_without_fields(bt):
    empty = bt.AddNode(NoFields, 0, 200)
    full = bt.AddNode(AllFields, 300, 200)
    assert not fields_frame(empty).isVisibleTo(empty)
    assert fields_frame(full).isVisibleTo(full)


def test_node_without_fields_is_smaller(bt):
    empty = bt.AddNode(NoFields, 0, 200)
    one = bt.AddNode(make_leaf_class("OneInt", {"n": 1}, title="No Fields"), 300, 200)
    assert empty.height() < one.height()
    # No room is reserved for the hidden frame: the node is as small as its layout allows.
    assert empty.height() <= empty.minimumSizeHint().height()
    assert empty._item.size().toSize() == empty.size()


def test_node_height_grows_with_number_of_fields(bt):
    heights = []
    for count in (0, 1, 2, 4, 8):
        cls = make_leaf_class(f"Fields{count}", {f"field_{i}": i for i in range(count)}, title="Same")
        node = bt.AddNode(cls, 250 * count, 200)
        heights.append(node.height())
        assert node._item.size().toSize() == node.size()
    assert heights == sorted(heights)
    assert len(set(heights)) == len(heights), heights


@pytest.mark.parametrize("count", [1, 3, 8, 20])
def test_fields_list_shows_all_rows_without_scrollbars(bt, count):
    values = [1, 2.5, "text", False, ["a", "b"]]
    fields = {f"field_{i}": values[i % len(values)] for i in range(count)}
    cls = make_leaf_class(f"Many{count}", fields)
    node = bt.AddNode(cls, 0, 200)
    view = fields_view(node)
    rows = field_rows(node)
    assert len(rows) == count
    total = sum(row.sizeHint().height() for row in rows)
    assert view.height() >= total
    assert view.viewport().height() >= total
    assert view.verticalScrollBar().maximum() == 0
    assert view.horizontalScrollBar().maximum() == 0
    assert not view.verticalScrollBar().isVisible()
    assert not view.horizontalScrollBar().isVisible()
    viewport_rect = view.viewport().rect()
    for row in rows:
        assert row.isVisibleTo(node)
        assert row.height() >= row.sizeHint().height()
        assert viewport_rect.contains(row.geometry()), (row.geometry(), viewport_rect)
    # The whole list lies inside the node, which is fully shown by its scene item.
    bottom = view.mapTo(node, QPoint(0, view.height())).y()
    assert bottom <= node.height()
    assert node._item.size().toSize() == node.size()


# ============================================================================ children ordering
def test_get_children_orders_left_to_right(bt):
    root = bt.GetRootNode()
    right = bt.AddNode(Succeed, 400, 200)
    left = bt.AddNode(Succeed, -200, 200)
    middle = bt.AddNode(Succeed, 100, 260)
    for node in (right, left, middle):
        bt.Connect(root, node)
    assert root.GetChildren() == [left, middle, right]


def test_get_children_ties_broken_top_first(bt):
    root = bt.GetRootNode()
    lower = bt.AddNode(Succeed, 0, 500)
    upper = bt.AddNode(Succeed, 0, 250)
    far_right = bt.AddNode(Succeed, 300, 100)
    for node in (lower, far_right, upper):
        bt.Connect(root, node)
    assert root.GetChildren() == [upper, lower, far_right]


def test_get_children_follows_node_moves(bt):
    root = bt.GetRootNode()
    first = bt.AddNode(Succeed, 0, 200)
    second = bt.AddNode(Succeed, 300, 200)
    bt.Connect(root, first)
    bt.Connect(root, second)
    assert root.GetChildren() == [first, second]
    first._item.setPos(600, 200)
    assert root.GetChildren() == [second, first]


def test_get_children_returns_copy(bt):
    root = bt.GetRootNode()
    leaf = bt.AddNode(Succeed, 0, 200)
    bt.Connect(root, leaf)
    children = root.GetChildren()
    children.clear()
    assert root.GetChildren() == [leaf]
    assert leaf.GetParent() is root


def test_node_parent_child_api(bt):
    root = bt.GetRootNode()
    composite = bt.AddNode(SELECTOR, 0, 200)
    leaf = bt.AddNode(Succeed, 0, 400)
    assert leaf.GetTree() is bt
    composite.SetParent(root)
    root.AddChild(leaf)
    assert composite.GetParent() is root and leaf.GetParent() is root
    leaf.SetParent(composite)  # replaces the previous parent
    assert leaf.GetParent() is composite
    assert root.GetChildren() == [composite]
    assert composite.GetChildren() == [leaf]
    root.RemoveChild(leaf)  # not a child of root: ignored
    assert leaf.GetParent() is composite
    composite.RemoveChild(leaf)
    assert leaf.GetParent() is None and composite.GetChildren() == []
    composite.SetParent(None)
    assert composite.GetParent() is None and root.GetChildren() == []


def test_parent_child_api_requires_a_view(qtbot):
    parent = CompositeNodeWidget(SEQUENCE)
    child = Succeed()
    qtbot.addWidget(parent)
    qtbot.addWidget(child)
    assert child.GetTree() is None
    with pytest.raises(RuntimeError):
        parent.AddChild(child)
    with pytest.raises(RuntimeError):
        child.SetParent(parent)


def test_is_interactive_widget_classification(bt):
    node = bt.AddNode(AllFields, 0, 200)
    for key in AllFields._fields:
        assert nodes_module.is_interactive_widget(node.field_editor(key), node), key
        # The read-only field names are drag areas: dragging on them moves the node.
        assert not nodes_module.is_interactive_widget(key_edits(node, key)[0], node), key
    for name in ("Title", "Status", "ParentConnection"):
        assert not nodes_module.is_interactive_widget(node.findChild(QLabel, name), node), name
    assert not nodes_module.is_interactive_widget(fields_frame(node), node)
    assert not nodes_module.is_interactive_widget(fields_view(node), node)
    assert not nodes_module.is_interactive_widget(fields_view(node).viewport(), node)
    assert not nodes_module.is_interactive_widget(None, node)


# ============================================================================ unknown leaf nodes
def test_unknown_leaf_keeps_type_name_and_fields(qtbot):
    node = UnknownLeafNodeWidget("Mystery")
    qtbot.addWidget(node)
    node._load_dict({"fields": {"a": 1, "b": ["x", "y"], "c": "s"}, "field_selections": {"b": 1}})
    assert isinstance(node, LeafNodeWidget)
    assert node.GetTypeName() == "Mystery"
    assert "Mystery" in title_label(node).text()
    assert node.GetFields() == {"a": 1, "b": ["x", "y"], "c": "s"}
    assert node.GetFieldSelectionIndex("b") == 1
    assert isinstance(node.field_editor("a"), QSpinBox)
    assert node.field_editor("b").currentIndex() == 1
    assert fields_frame(node).isVisibleTo(node)
    with pytest.raises(Exception):
        node.OnRun(None)


def test_unknown_leaf_round_trips_through_save_and_load(qtbot, bt, make_widget, tmp_path):
    root = bt.GetRootNode()
    leaf = bt.AddNode(AllFields, 0, 200)
    bt.Connect(root, leaf)
    leaf.SetField("count", 9)
    leaf.SetField("label", "kept")
    leaf.SetFieldSelectionIndex("choice", 2)
    first_path = tmp_path / "first.json"
    assert bt.SaveTree(str(first_path))

    other = make_widget(node_types=[])
    assert other.LoadTree(str(first_path))
    unknown = [node for node in other.GetNodes() if isinstance(node, UnknownLeafNodeWidget)]
    assert len(unknown) == 1
    node = unknown[0]
    assert node.GetTypeName() == "AllFields"
    assert node.GetFields() == leaf.GetFields()
    assert node.GetFieldSelection("choice") == "blue"
    assert node.GetParent() is other.GetRootNode()
    assert type(node.field_editor("count")) is QSpinBox

    second_path = tmp_path / "second.json"
    assert other.SaveTree(str(second_path))
    saved = {n["id"]: n for n in json.loads(first_path.read_text(encoding="utf-8"))["nodes"]}
    resaved = {n["id"]: n for n in json.loads(second_path.read_text(encoding="utf-8"))["nodes"]}
    original, again = saved[leaf.GetId()], resaved[leaf.GetId()]
    for key in ("type", "title", "fields", "field_selections"):
        assert again.get(key) == original.get(key), key


def test_loaded_unknown_leaf_is_marked_unknown_type(bt, make_widget, tmp_path):
    """README: loading an unregistered type creates a placeholder node marked "unknown type".

    The mark must be visible on the node while the saved title is kept for re-saving.
    """
    bt.AddNode(AllFields, 0, 200)
    path = tmp_path / "marked.json"
    assert bt.SaveTree(str(path))
    other = make_widget(node_types=[])
    assert other.LoadTree(str(path))
    node = next(n for n in other.GetNodes() if isinstance(n, UnknownLeafNodeWidget))
    assert node.GetTitle() == "All Fields"  # saved title kept (saving again loses nothing)
    assert "unknown type" in title_label(node).text()


def test_unknown_leaf_fails_when_executed(qtbot, bt, make_widget, tmp_path):
    root = bt.GetRootNode()
    bt.Connect(root, bt.AddNode(Succeed, 0, 200))
    path = tmp_path / "unknown.json"
    assert bt.SaveTree(str(path))
    other = make_widget(node_types=[])
    assert other.LoadTree(str(path))
    node = next(n for n in other.GetNodes() if isinstance(n, UnknownLeafNodeWidget))
    fast_config(other)
    other.Execute()
    run_until_idle(qtbot, other)
    assert node.GetStatus() is NodeStatus.FAILED
    assert status_label(node).text() == "Failed"


# ============================================================================ custom ui files
def test_custom_leaf_with_absolute_ui_file(bt, tmp_path):
    path = write_ui(tmp_path, "MyLeaf.ui", "LeafNode.ui", {'name="LeafNode"': 'name="MyCustomLeaf"'})
    cls = make_leaf_class("CustomUiLeaf", {"n": 1, "s": "x"}, title="Custom", UI_FILE=path)
    node = bt.AddNode(cls, 0, 200)
    assert node.findChild(QWidget, "MyCustomLeaf") is not None  # loaded from the copy
    assert title_label(node).text() == "Custom"
    assert status_label(node).text() == "Ready"
    assert node.connection_label(PARENT) is not None
    assert isinstance(node.field_editor("n"), QSpinBox)
    assert fields_frame(node).isVisibleTo(node)
    root = bt.GetRootNode()
    bt.Connect(root, node)
    assert node.connection_state(PARENT) is ConnectionState.CONNECTED


def test_custom_leaf_ui_relative_to_defining_module(qtbot, tmp_path, monkeypatch):
    write_ui(tmp_path, "RelLeaf.ui", "LeafNode.ui", {'name="LeafNode"': 'name="RelativeLeafUi"'})
    module_name = f"custom_nodes_{uuid.uuid4().hex}"
    (tmp_path / f"{module_name}.py").write_text(
        "from behavior_tree_widget import LeafNodeWidget\n"
        "\n"
        "class RelLeaf(LeafNodeWidget):\n"
        "    UI_FILE = 'RelLeaf.ui'\n"
        "    _fields = {'n': 1}\n"
        "\n"
        "    def OnRun(self, tree):\n"
        "        return True\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        node = module.RelLeaf()
        qtbot.addWidget(node)
        assert node.findChild(QWidget, "RelativeLeafUi") is not None
        assert title_label(node).text() == "RelLeaf"
        assert isinstance(node.field_editor("n"), QSpinBox)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("missing", ["Title", "Status", "ParentConnection"])
def test_custom_leaf_ui_missing_required_label_raises(qtbot, tmp_path, missing):
    path = write_ui(tmp_path, f"No{missing}.ui", "LeafNode.ui", {f'name="{missing}"': 'name="SomethingElse"'})
    cls = make_leaf_class(f"Missing{missing}", UI_FILE=path)
    with pytest.raises(RuntimeError, match=missing):
        cls()


@pytest.mark.parametrize("missing", ["Title", "Status", "ParentConnection", "ChildConnections"])
def test_custom_composite_ui_missing_required_label_raises(qtbot, tmp_path, missing):
    path = write_ui(tmp_path, f"Composite{missing}.ui", "CompositeNode.ui",
                    {f'name="{missing}"': 'name="SomethingElse"'})
    cls = type(f"CompositeMissing{missing}", (CompositeNodeWidget,), {"UI_FILE": path})
    with pytest.raises(RuntimeError, match=missing):
        cls()


def test_custom_leaf_ui_without_fields_list_still_works(qtbot, tmp_path):
    text = packaged_ui_text("LeafNode.ui")
    start = text.index('    <widget class="QFrame" name="FrameFields">')
    end = text.index("</widget>", text.index('<widget class="QListView" name="Fields"/>')) + len("</widget>")
    path = tmp_path / "NoFieldsList.ui"
    path.write_text(text[:start] + text[end:], encoding="utf-8")
    cls = make_leaf_class("NoListLeaf", {"n": 1}, UI_FILE=str(path))
    node = cls()
    qtbot.addWidget(node)
    assert node.findChild(QListView, "Fields") is None
    assert title_label(node).text() == "NoListLeaf"
    node.SetField("n", 2)
    assert node.GetField("n") == 2


def test_leaf_with_composite_ui_hides_child_connections(qtbot, tmp_path):
    path = write_ui(tmp_path, "LeafFromComposite.ui", "CompositeNode.ui")
    cls = make_leaf_class("LeafFromComposite", UI_FILE=path)
    node = cls()
    qtbot.addWidget(node)
    assert node.connection_label(CHILDREN) is None
    assert not node.findChild(QLabel, "ChildConnections").isVisibleTo(node)
    assert node.connection_label(PARENT).isVisibleTo(node)


def test_missing_ui_file_attribute_raises(qtbot):
    cls = type("NoUi", (NodeWidget,), {"UI_FILE": ""})
    with pytest.raises(RuntimeError):
        cls()


# ============================================================================ type names / misc
def test_type_names(bt):
    assert bt.GetRootNode().GetTypeName() == "Root"
    assert bt.AddNode(SELECTOR, 0, 200).GetTypeName() == SELECTOR
    assert bt.AddNode(AllFields, 300, 200).GetTypeName() == "AllFields"
    named = make_leaf_class("InternalName", TYPE_NAME="PublicName")
    assert bt.AddNode(named, 600, 200).GetTypeName() == "PublicName"


def test_base_leaf_on_run_not_implemented(qtbot):
    node = LeafNodeWidget()
    qtbot.addWidget(node)
    with pytest.raises(NotImplementedError):
        node.OnRun(None)


def test_node_ids_are_unique(bt):
    ids = {bt.AddNode(Succeed, 200 * i, 200).GetId() for i in range(5)}
    ids.add(bt.GetRootNode().GetId())
    assert len(ids) == 6


# ============================================================================ wheel guard
@pytest.mark.parametrize("key", ["count", "ratio", "choice"])
def test_wheel_ignored_by_unfocused_editor(bt, key):
    node = bt.AddNode(AllFields, 0, 200)
    editor = node.field_editor(key)
    assert editor.focusPolicy() == Qt.FocusPolicy.StrongFocus  # wheel does not take focus
    assert not editor.hasFocus()
    before_editor = editor_value(editor)
    before_fields = node.GetFields()
    before_selection = node.GetFieldSelectionIndex("choice")
    for delta in (120, -120, -120):
        send_wheel(editor, delta)
    assert editor_value(editor) == before_editor
    assert node.GetFields() == before_fields
    assert node.GetFieldSelectionIndex("choice") == before_selection
    assert not editor.hasFocus()


@pytest.mark.parametrize("key", ["count", "ratio", "choice"])
def test_wheel_over_unfocused_editor_scrolls_the_view_instead(bt, key):
    node = bt.AddNode(AllFields, 0, 200)
    bt.view().centerOn(150, 250)
    editor = node.field_editor(key)
    before_editor = editor_value(editor)
    before_fields = node.GetFields()
    viewport = bt.view().viewport()
    point = viewport_point(bt, node, editor)
    scrollbar = bt.view().verticalScrollBar()
    before_scroll = scrollbar.value()
    event = QWheelEvent(
        QPointF(point),
        QPointF(viewport.mapToGlobal(point)),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(viewport, event)
    assert editor_value(editor) == before_editor
    assert node.GetFields() == before_fields
    assert scrollbar.value() != before_scroll


def test_same_wheel_event_changes_an_unguarded_spin_box(qtbot):
    """Control: the synthetic wheel event does change a plain spin box."""
    spin = QSpinBox()
    qtbot.addWidget(spin)
    spin.show()
    send_wheel(spin, 120)
    assert spin.value() == 1


@pytest.mark.parametrize(
    "key, delta, expected",
    [("count", 120, 4), ("ratio", 120, pytest.approx(0.6)), ("choice", -120, 1)],
)
def test_wheel_applies_to_focused_editor(qtbot, bt, key, delta, expected):
    node = bt.AddNode(AllFields, 0, 200)
    editor = node.field_editor(key)
    bt.activateWindow()
    qtbot.waitUntil(bt.isActiveWindow, timeout=2000)
    editor.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(editor.hasFocus, timeout=2000)
    send_wheel(editor, delta)
    if key == "choice":
        assert editor.currentIndex() == expected
        assert node.GetFieldSelectionIndex("choice") == expected
    else:
        assert editor.value() == expected
        assert node.GetField(key) == expected


def test_wheel_guard_filter_directly(qtbot):
    guard = nodes_module._WheelGuard()
    spin = QSpinBox()
    qtbot.addWidget(spin)
    spin.show()
    pos = QPointF(5, 5)
    event = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    assert not spin.hasFocus()
    assert guard.eventFilter(spin, event) is True
    assert not event.isAccepted()
