"""Tests of behavior_tree_widget.widget: structure, state, file operations, window title and node types.

Covered here:

* the BehaviorTreeWidget layout (two tabs, FrameView housing the TreeView, execution buttons),
* the initial state (no tree loaded, view disabled, root node present),
* New / Load / Save through the (faked) file dialogs and programmatically, including errors,
* the window title ``f"Behavior Tree - {filename}"`` (top-level widget and QMainWindow),
* full save / load round trips and robustness against damaged tree files,
* modified tracking and ConfirmDiscardChanges,
* Configure, node type registration, Shutdown, independence of widgets and the demo window.
"""

from __future__ import annotations

import json
import os
import shutil

import py_trees
import pytest
import shiboken6
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QFrame, QLabel, QMainWindow, QMessageBox, QPushButton, QScrollArea, QTabWidget, QWidget

from behavior_tree_widget import (
    BehaviorTreeWidget,
    CompositeNodeWidget,
    LeafNodeWidget,
    NodeStatus,
    NodeWidget,
    RootNodeWidget,
    Status,
    TreeConfig,
    TreeFileError,
    UnknownLeafNodeWidget,
    register_node_type,
    registered_node_types,
)
from behavior_tree_widget import widget as widget_module
from behavior_tree_widget.blackboard import BlackboardView
from behavior_tree_widget.canvas import TreeView
from behavior_tree_widget.config import ConfigureDialog
from behavior_tree_widget.demo import DEMO_NODE_TYPES, DemoWindow
from behavior_tree_widget.serialization import FORMAT_NAME, FORMAT_VERSION, read_tree_file

from conftest import (
    TEST_NODE_TYPES,
    AllFields,
    FailNode,
    SlowCancelable,
    Succeed,
    click,
    drag,
    fast_config,
    viewport_point,
)

SAVE = QMessageBox.StandardButton.Save
DISCARD = QMessageBox.StandardButton.Discard
CANCEL = QMessageBox.StandardButton.Cancel

ALL_BUTTONS = ("Execute", "Pause", "Stop", "Reset", "Configure", "Save", "Load", "New")


def expected_title(filename: str) -> str:
    return f"Behavior Tree - {filename}"


# ============================================================================ local node types
class ForeverRunning(LeafNodeWidget):
    """Returns RUNNING on every tick (GUI thread) and records the OnTerminate statuses."""

    _title = "Forever Running"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.terminated: list = []

    def OnRun(self, tree):
        return Status.RUNNING

    def OnTerminate(self, tree, status):
        self.terminated.append(status)


# ============================================================================ helpers
def node_entry(node_id: str, type_name: str, x: float = 0.0, y: float = 0.0, **extra) -> dict:
    """One node of a tree file."""
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


def write_tree_file(path, nodes, connections=(), blackboard=(), **extra) -> str:
    """Write a tree file by hand (independent of SaveTree) and return its path."""
    path.write_text(json.dumps(tree_document(nodes, connections, blackboard, **extra)), encoding="utf-8")
    return str(path)


def simple_tree_file(path) -> str:
    """Root(Selector) -> Succeed 'Leaf A'."""
    return write_tree_file(
        path,
        [node_entry("r", "Root", composite="Selector", memory=True), node_entry("a", "Succeed", 40, 220, title="Leaf A")],
        [{"parent": "r", "child": "a"}],
    )


def read_json(path) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def saved_ids(path) -> set[str]:
    return {node["id"] for node in read_json(path)["nodes"]}


def node_by_id(widget: BehaviorTreeWidget, node_id: str) -> NodeWidget:
    matches = [node for node in widget.GetNodes() if node.GetId() == node_id]
    assert len(matches) == 1, f"expected one node with id {node_id!r}, found {len(matches)}"
    return matches[0]


def position(node: NodeWidget) -> tuple[float, float]:
    pos = node._item.pos()
    return (round(pos.x(), 2), round(pos.y(), 2))


def describe_tree(widget: BehaviorTreeWidget) -> dict:
    """Everything that should survive a save / load round trip, keyed by node id."""
    result = {}
    for node in widget.GetNodes():
        parent = node.GetParent()
        info = {
            "class": type(node).__name__,
            "type": node.GetTypeName(),
            "title": node.GetTitle(),
            "pos": position(node),
            "parent": parent.GetId() if parent is not None else None,
            "children": [child.GetId() for child in node.GetChildren()],
        }
        if isinstance(node, CompositeNodeWidget):
            info["composite"] = node.GetCompositeType()
            info["memory"] = node.GetMemory()
        if isinstance(node, LeafNodeWidget):
            fields = node.GetFields()
            info["fields"] = fields
            info["selections"] = {
                key: node.GetFieldSelectionIndex(key) for key, value in fields.items() if isinstance(value, list)
            }
        result[node.GetId()] = info
    return result


def blackboard_contents(widget: BehaviorTreeWidget) -> list[tuple[str, str, object]]:
    return [(name, widget.GetEntryType(name), widget.GetEntry(name)) for name in widget.GetEntryNames()]


def mark_saved(widget: BehaviorTreeWidget) -> None:
    assert widget.SaveTree(widget.GetFilePath())
    assert not widget.IsModified()


# ============================================================================ fixtures
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
def saved_bt(bt):
    """``bt`` holding Root -> AllFields (connected) and an unconnected Succeed, saved (not modified)."""
    connected = bt.AddNode("AllFields", -260, 200)
    loose = bt.AddNode("Succeed", 260, 200)
    bt.Connect(bt.GetRootNode(), connected)
    mark_saved(bt)
    return bt, connected, loose


@pytest.fixture
def main_window(qtbot):
    """A QMainWindow whose central widget is a BehaviorTreeWidget (not shown yet)."""
    window = QMainWindow()
    qtbot.addWidget(window)
    tree = BehaviorTreeWidget(node_types=[Succeed, AllFields])
    window.setCentralWidget(tree)
    window.setWindowTitle("Host Application")
    window.resize(1000, 700)
    yield window, tree
    tree._set_modified(False)
    tree.Shutdown()


@pytest.fixture
def unshown_widget(qtbot):
    """Factory for BehaviorTreeWidgets that are *not* shown."""
    created = []

    def make(node_types=None):
        types = list(DEMO_NODE_TYPES) + list(TEST_NODE_TYPES) if node_types is None else list(node_types)
        widget = BehaviorTreeWidget(node_types=types)
        qtbot.addWidget(widget)
        created.append(widget)
        return widget

    yield make
    for widget in created:
        try:
            widget._set_modified(False)
            widget.Shutdown()
        except RuntimeError:
            pass


def build_rich_tree(bt: BehaviorTreeWidget) -> dict[str, NodeWidget]:
    """Populate ``bt`` with every kind of node, field, connection, blackboard entry and option."""
    root = bt.GetRootNode()
    root.SetCompositeType("Selector")
    root.SetMemory(False)

    sequence = bt.AddNode("Sequence", -300, 160)
    sequence.SetTitle("Main Sequence")
    selector = bt.AddNode("Selector", 300, 160)
    selector.SetMemory(False)

    fields = bt.AddNode("AllFields", -560, 360)
    fields.SetTitle("Configured Fields")
    fields.SetField("count", -17)
    fields.SetField("ratio", 0.1234567891)
    fields.SetField("label", "héllo wörld")
    fields.SetField("enabled", False)
    fields.SetField("choice", ["north", "south", "east", "west"])
    fields.SetFieldSelectionIndex("choice", 3)

    succeed = bt.AddNode("Succeed", -200, 360)
    fail = bt.AddNode("FailNode", 250, 360)
    slow = bt.AddNode("SlowCancelable", 560, 360)
    slow.SetField("seconds", 0.25)
    loose = bt.AddNode("NoFields", 900, 700)  # not connected to anything

    # Connected in an order that differs from the left-to-right child order.
    bt.Connect(root, selector)
    bt.Connect(root, sequence)
    bt.Connect(sequence, succeed)
    bt.Connect(sequence, fields)
    bt.Connect(selector, slow)
    bt.Connect(selector, fail)

    for name, type_name, value in BLACKBOARD_ENTRIES:
        bt.AddEntry(name, type_name, value)

    bt.SetConfig(RICH_CONFIG)
    bt.view().set_zoom(1.5)
    return {
        "root": root, "sequence": sequence, "selector": selector, "fields": fields,
        "succeed": succeed, "fail": fail, "slow": slow, "loose": loose,
    }


BLACKBOARD_ENTRIES = [
    ("integer", "Integer", -42),
    ("double", "Double", 3.25),
    ("string", "String", "text value"),
    ("flag", "Bool", True),
    ("items", "List", [1, 2.5, "three", [4, 5], (6, 7)]),
    ("mapping", "Dictionary", {"a": 1, "nested": {"b": [2]}, 3: "int key"}),
    ("unique", "Set", {1, 2, 3}),
    ("thing", "Object", ("tuple", 1)),
]

RICH_CONFIG = TreeConfig(tick_interval_ms=250, repeat=True, restore_blackboard=True, default_memory=False)


@pytest.fixture
def round_trip(bt, make_widget, tmp_path):
    """(original widget, freshly loaded widget, saved path, original nodes) after a save / load cycle."""
    nodes = build_rich_tree(bt)
    path = str(tmp_path / "rich.json")
    assert bt.SaveTree(path)
    loaded = make_widget()
    assert loaded.LoadTree(path)
    return bt, loaded, path, nodes


# ============================================================================ structure
def test_widget_is_tab_widget_with_tree_and_blackboard_tabs(make_widget):
    widget = make_widget()
    assert isinstance(widget, QTabWidget)
    assert widget.count() == 2
    assert [widget.tabText(index) for index in range(widget.count())] == ["Behavior Tree", "Blackboard"]


def test_blackboard_tab_hosts_blackboard_view(make_widget):
    widget = make_widget()
    page = widget.widget(1)
    assert page is widget.blackboardView()
    assert isinstance(page, BlackboardView)
    assert page.findChild(QScrollArea, "EntryList") is not None


def test_frame_view_contains_the_tree_view(make_widget):
    widget = make_widget()
    page = widget.widget(0)
    frame_view = page.findChild(QFrame, "FrameView")
    assert frame_view is not None
    view = widget.view()
    assert isinstance(view, TreeView)
    assert view.parentWidget() is frame_view
    assert frame_view.layout() is not None and frame_view.layout().indexOf(view) >= 0


def test_frame_execution_contains_all_buttons(make_widget):
    widget = make_widget()
    frame_execution = widget.widget(0).findChild(QFrame, "FrameExecution")
    assert frame_execution is not None
    for name in ALL_BUTTONS:
        button = frame_execution.findChild(QPushButton, name)
        assert button is not None, name
        assert widget.button(name) is button


# ============================================================================ initial state
def test_initial_state_has_no_tree_and_disabled_view(make_widget):
    widget = make_widget()
    assert widget.IsTreeLoaded() is False
    assert widget.view().isEnabled() is False
    assert widget.GetFilePath() is None
    assert widget.IsModified() is False
    assert widget.IsExecuting() is False
    assert widget.GetExecutionState() == "Idle"


@pytest.mark.parametrize("name", ALL_BUTTONS)
def test_initially_only_load_and_new_are_enabled(make_widget, name):
    widget = make_widget()
    assert widget.button(name).isEnabled() is (name in ("Load", "New"))


def test_initial_tree_already_has_a_root_node(make_widget):
    widget = make_widget()
    root = widget.GetRootNode()
    assert isinstance(root, RootNodeWidget)
    assert widget.GetNodes() == [root]
    assert root.GetTitle() == "Root"
    assert root.GetCompositeType() in ("Sequence", "Selector")
    assert root.GetParent() is None
    assert root.GetChildren() == []


def test_execute_does_nothing_before_a_tree_exists(make_widget, dialogs):
    widget = make_widget()
    widget.Execute()
    assert widget.IsExecuting() is False
    assert dialogs.shown == []


# ============================================================================ New
def test_new_via_button_creates_valid_tree_file(make_widget, dialogs, tmp_path):
    widget = make_widget()
    path = tmp_path / "created.json"
    dialogs.save_path = str(path)
    widget.button("New").click()

    assert path.is_file()
    data = read_tree_file(str(path))  # validates the format
    assert data["format"] == FORMAT_NAME
    assert data["version"] == FORMAT_VERSION
    assert [node["type"] for node in data["nodes"]] == ["Root"]
    assert data["connections"] == []
    assert widget.IsTreeLoaded() is True
    assert widget.view().isEnabled() is True
    assert widget.GetFilePath() == os.path.abspath(str(path))
    assert widget.IsModified() is False
    assert dialogs.kinds() == ["save_dialog"]

    other = make_widget()
    assert other.LoadTree(str(path))
    assert len(other.GetNodes()) == 1 and isinstance(other.GetRootNode(), RootNodeWidget)


@pytest.mark.parametrize("filename", ["tree.json", "my tree.json", "v1.2.final.json", "bäume ünïcode.json"])
def test_new_sets_title_of_top_level_widget(make_widget, dialogs, tmp_path, filename):
    widget = make_widget()
    dialogs.save_path = str(tmp_path / filename)
    widget.button("New").click()
    assert widget.isWindow()
    assert widget.windowTitle() == expected_title(filename)


def test_new_file_dialog_offers_json_filter(make_widget, dialogs):
    widget = make_widget()
    dialogs.save_path = ""
    widget.button("New").click()
    kind, args = dialogs.shown[-1]
    assert kind == "save_dialog"
    assert any(isinstance(arg, str) and "*.json" in arg for arg in args)


@pytest.mark.parametrize("name", ALL_BUTTONS)
def test_buttons_after_new(bt, name):
    assert bt.button(name).isEnabled() is (name not in ("Pause", "Stop"))


def test_new_cancelled_on_fresh_widget_changes_nothing(make_widget, dialogs, tmp_path):
    widget = make_widget()
    title = widget.windowTitle()
    dialogs.save_path = ""
    assert widget.NewTree() is False
    widget.button("New").click()
    assert widget.IsTreeLoaded() is False
    assert widget.view().isEnabled() is False
    assert widget.GetFilePath() is None
    assert widget.windowTitle() == title
    assert list(tmp_path.iterdir()) == []


def test_new_cancelled_keeps_current_tree(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    before = describe_tree(bt)
    path, title = bt.GetFilePath(), bt.windowTitle()
    content = open(path, encoding="utf-8").read()
    dialogs.save_path = ""
    bt.button("New").click()
    assert describe_tree(bt) == before
    assert bt.GetFilePath() == path
    assert bt.windowTitle() == title
    assert bt.IsModified() is False
    assert open(path, encoding="utf-8").read() == content


@pytest.mark.parametrize("button", ["New", "Save"])
def test_json_extension_appended_when_name_has_none(bt, dialogs, tmp_path, button):
    dialogs.save_path = str(tmp_path / "noext")
    bt.button(button).click()
    expected = tmp_path / "noext.json"
    assert expected.is_file()
    assert not (tmp_path / "noext").exists()
    assert bt.GetFilePath() == os.path.abspath(str(expected))
    assert bt.windowTitle() == expected_title("noext.json")


def test_existing_extension_is_kept(bt, dialogs, tmp_path):
    dialogs.save_path = str(tmp_path / "tree.bt")
    bt.button("Save").click()
    assert (tmp_path / "tree.bt").is_file()
    assert not (tmp_path / "tree.bt.json").exists()
    assert bt.windowTitle() == expected_title("tree.bt")


def test_new_replaces_tree_and_clears_blackboard(bt, qtbot, tmp_path):
    marker = object()
    bt.Connect(bt.GetRootNode(), bt.AddNode("Succeed", 0, 200))
    bt.SetEntry(5, "cleared")
    bt.SetEntry(marker, "kept")
    old_root_id = bt.GetRootNode().GetId()
    path = str(tmp_path / "second.json")
    with qtbot.waitSignal(bt.treeLoaded, timeout=1000) as blocker:
        assert bt.NewTree(path)
    assert blocker.args == [os.path.abspath(path)]
    assert len(bt.GetNodes()) == 1
    assert isinstance(bt.GetRootNode(), RootNodeWidget)
    assert bt.GetRootNode().GetId() != old_root_id
    assert not bt.HasEntry("cleared")
    assert bt.GetEntry("kept") is marker  # Object entries (run-time objects) are kept
    assert read_json(path)["blackboard"] == []
    assert bt.windowTitle() == expected_title("second.json")
    assert bt.IsModified() is False


def test_root_is_visible_after_new(bt):
    view = bt.view()
    root = bt.GetRootNode()
    center = view.mapFromScene(root._item.sceneBoundingRect().center())
    assert view.viewport().rect().contains(center)


# ============================================================================ window title in a QMainWindow
def test_new_sets_title_of_main_window(main_window, dialogs, tmp_path):
    window, tree = main_window
    window.show()
    dialogs.save_path = str(tmp_path / "main.json")
    tree.button("New").click()
    assert tree.IsTreeLoaded()
    assert window.windowTitle() == expected_title("main.json")


def test_load_sets_title_of_main_window(main_window, dialogs, tmp_path):
    window, tree = main_window
    window.show()
    dialogs.open_path = simple_tree_file(tmp_path / "loaded.json")
    tree.button("Load").click()
    assert tree.IsTreeLoaded()
    assert window.windowTitle() == expected_title("loaded.json")


def test_save_as_updates_title_of_main_window(main_window, dialogs, tmp_path):
    window, tree = main_window
    window.show()
    assert tree.NewTree(str(tmp_path / "first.json"))
    dialogs.save_path = str(tmp_path / "second.json")
    tree.button("Save").click()
    assert window.windowTitle() == expected_title("second.json")


def test_title_applied_when_widget_is_embedded_after_new(qtbot, tmp_path):
    tree = BehaviorTreeWidget(node_types=[Succeed])
    window = QMainWindow()
    qtbot.addWidget(window)
    try:
        assert tree.NewTree(str(tmp_path / "early.json"))
        window.setCentralWidget(tree)
        window.show()
        qtbot.waitUntil(lambda: window.windowTitle() == expected_title("early.json"), timeout=1000)
        qtbot.wait(20)  # let deferred work queued by showEvent run while the window still exists
    finally:
        tree.Shutdown()


def test_window_destroyed_right_after_show_raises_no_error(qtbot):
    """showEvent queues deferred framing work; it must not run against a destroyed widget.

    pytest-qt fails the test if an exception escapes into the Qt event loop.
    """
    window = QMainWindow()
    tree = BehaviorTreeWidget(node_types=[Succeed])
    window.setCentralWidget(tree)
    window.show()
    tree.Shutdown()
    shiboken6.delete(window)  # destroyed before the event loop ran (e.g. parent deleted synchronously)
    qtbot.wait(30)


# ============================================================================ Load
def test_load_via_dialog(make_widget, dialogs, tmp_path):
    path = simple_tree_file(tmp_path / "input.json")
    widget = make_widget()
    dialogs.open_path = path
    widget.button("Load").click()

    assert widget.IsTreeLoaded() is True
    assert widget.view().isEnabled() is True
    assert widget.GetFilePath() == os.path.abspath(path)
    assert widget.windowTitle() == expected_title("input.json")
    assert widget.IsModified() is False
    root = widget.GetRootNode()
    assert root.GetId() == "r"
    assert root.GetCompositeType() == "Selector"
    [child] = root.GetChildren()
    assert isinstance(child, Succeed)
    assert child.GetTitle() == "Leaf A"
    assert position(child) == (40.0, 220.0)
    assert dialogs.kinds() == ["open_dialog"]
    _, args = dialogs.shown[0]
    assert any(isinstance(arg, str) and "*.json" in arg for arg in args)


def test_programmatic_load_updates_title_and_state(make_widget, qtbot, tmp_path):
    path = simple_tree_file(tmp_path / "direct.json")
    widget = make_widget()
    with qtbot.waitSignal(widget.treeLoaded, timeout=1000) as blocker:
        assert widget.LoadTree(path) is True
    assert blocker.args == [os.path.abspath(path)]
    assert widget.IsTreeLoaded()
    assert widget.windowTitle() == expected_title("direct.json")


def test_load_cancelled_changes_nothing(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    before = describe_tree(bt)
    path = bt.GetFilePath()
    dialogs.open_path = ""
    assert bt.LoadTree() is False
    assert describe_tree(bt) == before
    assert bt.GetFilePath() == path


def _document_bytes(**changes) -> bytes:
    data = tree_document([node_entry("r", "Root")])
    data.update(changes)
    return json.dumps(data).encode("utf-8")


INVALID_FILES = {
    "missing file": None,
    "empty file": b"",
    "not json": b"{not json",
    "invalid utf-8": b"\xff\xfe\x80 not text",
    "json array": b"[]",
    "json string": b'"tree"',
    "wrong format": _document_bytes(format="something_else"),
    "missing format": json.dumps({"version": FORMAT_VERSION, "nodes": []}).encode(),
    "missing version": json.dumps({"format": FORMAT_NAME, "nodes": []}).encode(),
    "newer version": _document_bytes(version=FORMAT_VERSION + 1),
    "version zero": _document_bytes(version=0),
    "version string": _document_bytes(version="1"),
    "version bool": _document_bytes(version=True),
    "nodes not a list": _document_bytes(nodes={"id": "r", "type": "Root"}),
    "connections not a list": _document_bytes(connections={"parent": "r"}),
    "blackboard not a list": _document_bytes(blackboard="entries"),
    "config not an object": _document_bytes(config=[1, 2]),
}


def make_invalid_file(tmp_path, case: str) -> str:
    path = tmp_path / f"invalid_{case.replace(' ', '_')}.json"
    content = INVALID_FILES[case]
    if content is not None:
        path.write_bytes(content)
    return str(path)


@pytest.mark.parametrize("case", list(INVALID_FILES))
def test_interactive_load_of_invalid_file_shows_critical_and_keeps_tree(saved_bt, dialogs, tmp_path, case):
    bt, connected, loose = saved_bt
    before = describe_tree(bt)
    path, title = bt.GetFilePath(), bt.windowTitle()
    dialogs.open_path = make_invalid_file(tmp_path, case)

    bt.button("Load").click()

    assert "critical" in dialogs.kinds()
    assert bt.IsTreeLoaded() is True
    assert bt.view().isEnabled() is True
    assert bt.GetFilePath() == path
    assert bt.windowTitle() == title
    assert bt.IsModified() is False
    assert describe_tree(bt) == before
    assert all(node._item is not None for node in bt.GetNodes())


@pytest.mark.parametrize("case", list(INVALID_FILES))
def test_programmatic_load_of_invalid_file_raises_tree_file_error(saved_bt, dialogs, tmp_path, case):
    bt, connected, loose = saved_bt
    before = describe_tree(bt)
    path = bt.GetFilePath()
    with pytest.raises(TreeFileError):
        bt.LoadTree(make_invalid_file(tmp_path, case))
    assert "critical" not in dialogs.kinds()
    assert bt.GetFilePath() == path
    assert describe_tree(bt) == before


def test_failed_load_on_fresh_widget_keeps_it_unloaded(make_widget, tmp_path):
    widget = make_widget()
    with pytest.raises(TreeFileError):
        widget.LoadTree(str(tmp_path / "does_not_exist.json"))
    assert widget.IsTreeLoaded() is False
    assert widget.view().isEnabled() is False
    assert widget.GetFilePath() is None
    assert isinstance(widget.GetRootNode(), RootNodeWidget)


def test_load_replaces_blackboard_entries_but_keeps_objects(make_widget, tmp_path):
    path = write_tree_file(
        tmp_path / "bb.json",
        [node_entry("r", "Root")],
        blackboard=[{"name": "from_file", "type": "Integer", "value": 7}, {"name": "both", "type": "String", "value": "file"}],
    )
    widget = make_widget()
    marker = object()
    widget.SetEntry("mine", "own")
    widget.SetEntry("widget", "both")
    widget.SetEntry(marker, "runtime")
    assert widget.LoadTree(path)
    assert not widget.HasEntry("own")
    assert widget.GetEntry("from_file") == 7
    assert widget.GetEntry("both") == "file"
    assert widget.GetEntry("runtime") is marker


# ============================================================================ Save
def test_save_via_dialog_to_new_name(bt, dialogs, qtbot, tmp_path):
    leaf = bt.AddNode("Succeed", 100, 200)
    assert bt.IsModified()
    target = tmp_path / "renamed.json"
    dialogs.save_path = str(target)
    with qtbot.waitSignal(bt.treeSaved, timeout=1000) as blocker:
        bt.button("Save").click()
    assert target.is_file()
    assert leaf.GetId() in saved_ids(target)
    read_tree_file(str(target))
    assert bt.GetFilePath() == os.path.abspath(str(target))
    assert bt.windowTitle() == expected_title("renamed.json")
    assert bt.IsModified() is False
    assert blocker.args == [os.path.abspath(str(target))]


def test_save_dialog_suggests_current_file(bt, dialogs):
    dialogs.save_path = ""
    bt.button("Save").click()
    kind, args = dialogs.shown[-1]
    assert kind == "save_dialog"
    assert bt.GetFilePath() in args


def test_save_cancelled_keeps_modified_and_file(bt, dialogs):
    path = bt.GetFilePath()
    content = open(path, encoding="utf-8").read()
    bt.AddNode("Succeed", 100, 200)
    dialogs.save_path = ""
    assert bt.SaveTree() is False
    assert bt.IsModified() is True
    assert open(path, encoding="utf-8").read() == content


def test_programmatic_save_to_current_file(bt):
    leaf = bt.AddNode("FailNode", 100, 200)
    assert bt.SaveTree(bt.GetFilePath()) is True
    assert leaf.GetId() in saved_ids(bt.GetFilePath())
    assert bt.IsModified() is False


def test_save_skips_blackboard_values_that_cannot_be_saved(bt, caplog, dialogs, tmp_path):
    bt.SetEntry(object(), "runtime_object")  # Object entries are run-time values, never saved
    bt.SetEntry([1, 2], "items")
    bt.SetEntry(4, "number")
    with bt.blackboardStore()._lock:
        # A List whose value cannot be written as JSON (bytes) is skipped with a warning.
        bt.blackboardStore()._client.set(bt.blackboardStore().key("items"), [b"raw"], overwrite=True)
    with caplog.at_level("WARNING", logger="behavior_tree_widget"):
        assert bt.SaveTree(bt.GetFilePath()) is True
    entries = read_json(bt.GetFilePath())["blackboard"]
    assert entries == [{"name": "number", "type": "Integer", "value": 4}]
    assert any("items" in record.getMessage() for record in caplog.records)
    assert not any("runtime_object" in record.getMessage() for record in caplog.records)
    assert bt.IsModified() is False
    dialogs.save_path = str(tmp_path / "interactive.json")
    bt.button("Save").click()
    assert "warning" in dialogs.kinds()  # the user is told about the skipped List


def test_save_on_fresh_widget_counts_as_loaded(make_widget, tmp_path):
    widget = make_widget()
    path = str(tmp_path / "saved_first.json")
    assert widget.SaveTree(path)
    assert widget.IsTreeLoaded() is True
    assert widget.view().isEnabled() is True
    assert widget.GetFilePath() == os.path.abspath(path)
    assert widget.windowTitle() == expected_title("saved_first.json")


def test_programmatic_save_to_unwritable_location_raises(bt, dialogs, tmp_path):
    bt.AddNode("Succeed", 100, 200)
    path, title = bt.GetFilePath(), bt.windowTitle()
    with pytest.raises(OSError):
        bt.SaveTree(str(tmp_path / "missing_dir" / "tree.json"))
    assert dialogs.shown == []
    assert bt.GetFilePath() == path
    assert bt.windowTitle() == title
    assert bt.IsModified() is True


def test_programmatic_save_onto_directory_raises_and_leaves_no_temp_file(bt, tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(OSError):
        bt.SaveTree(str(folder))
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["folder", "tree.json"]
    assert list(folder.iterdir()) == []


def test_interactive_save_to_unwritable_location_shows_critical(bt, dialogs, tmp_path):
    bt.AddNode("Succeed", 100, 200)
    path, title = bt.GetFilePath(), bt.windowTitle()
    dialogs.save_path = str(tmp_path / "missing_dir" / "tree.json")
    bt.button("Save").click()
    assert dialogs.kinds() == ["save_dialog", "critical"]
    assert bt.GetFilePath() == path
    assert bt.windowTitle() == title
    assert bt.IsModified() is True


def test_new_tree_to_unwritable_location(make_widget, dialogs, tmp_path):
    widget = make_widget()
    with pytest.raises(OSError):
        widget.NewTree(str(tmp_path / "missing_dir" / "tree.json"))
    assert dialogs.shown == []
    dialogs.save_path = str(tmp_path / "missing_dir" / "tree.json")
    widget.button("New").click()
    assert "critical" in dialogs.kinds()
    assert widget.IsTreeLoaded() is False
    assert widget.GetFilePath() is None


# ============================================================================ round trip
def test_round_trip_preserves_every_node(round_trip):
    original, loaded, path, nodes = round_trip
    assert describe_tree(loaded) == describe_tree(original)


def test_round_trip_node_types_titles_and_positions(round_trip):
    original, loaded, path, nodes = round_trip
    assert len(loaded.GetNodes()) == len(original.GetNodes()) == len(nodes) == 8
    for node in original.GetNodes():
        copy = node_by_id(loaded, node.GetId())
        assert type(copy) is type(node)
        assert copy.GetTypeName() == node.GetTypeName()
        assert copy.GetTitle() == node.GetTitle()
        assert position(copy) == position(node)
    assert node_by_id(loaded, nodes["sequence"].GetId()).GetTitle() == "Main Sequence"
    assert node_by_id(loaded, nodes["fields"].GetId()).GetTitle() == "Configured Fields"


def test_round_trip_leaf_fields_of_every_type(round_trip):
    original, loaded, path, nodes = round_trip
    fields = node_by_id(loaded, nodes["fields"].GetId())
    assert fields.GetFields() == {
        "count": -17,
        "ratio": 0.1234567891,
        "label": "héllo wörld",
        "enabled": False,
        "choice": ["north", "south", "east", "west"],
    }
    assert fields.GetFieldSelectionIndex("choice") == 3
    assert fields.GetFieldSelection("choice") == "west"
    assert fields.field_editor("choice").currentText() == "west"
    assert fields.field_editor("count").value() == -17
    assert fields.field_editor("enabled").isChecked() is False
    assert node_by_id(loaded, nodes["slow"].GetId()).GetField("seconds") == 0.25


def test_round_trip_composites_and_root(round_trip):
    original, loaded, path, nodes = round_trip
    root = loaded.GetRootNode()
    assert root.GetId() == nodes["root"].GetId()
    assert root.GetCompositeType() == "Selector"
    assert root.GetMemory() is False
    sequence = node_by_id(loaded, nodes["sequence"].GetId())
    selector = node_by_id(loaded, nodes["selector"].GetId())
    assert (sequence.GetCompositeType(), sequence.GetMemory()) == ("Sequence", True)
    assert (selector.GetCompositeType(), selector.GetMemory()) == ("Selector", False)


def test_round_trip_connections_and_child_order(round_trip):
    original, loaded, path, nodes = round_trip
    ids = {name: node.GetId() for name, node in nodes.items()}

    def children(name):
        return [child.GetId() for child in node_by_id(loaded, ids[name]).GetChildren()]

    assert children("root") == [ids["sequence"], ids["selector"]]
    assert children("sequence") == [ids["fields"], ids["succeed"]]
    assert children("selector") == [ids["fail"], ids["slow"]]
    assert node_by_id(loaded, ids["loose"]).GetParent() is None
    assert len(loaded.view().connections()) == 6


def test_round_trip_blackboard_entries_of_every_type(round_trip):
    original, loaded, path, nodes = round_trip
    # Object entries hold run-time values and are never saved.
    assert blackboard_contents(loaded) == [
        (name, type_name, value if type_name != "List" else list(value))
        for name, type_name, value in BLACKBOARD_ENTRIES
        if type_name != "Object"
    ]
    assert blackboard_contents(loaded) == [entry for entry in blackboard_contents(original) if entry[1] != "Object"]


def test_round_trip_config(round_trip):
    original, loaded, path, nodes = round_trip
    assert loaded.GetConfig() == RICH_CONFIG
    assert read_json(path)["config"] == RICH_CONFIG.to_dict()


def test_round_trip_view_zoom(round_trip):
    original, loaded, path, nodes = round_trip
    assert loaded.view().zoom() == pytest.approx(1.5)


def test_round_trip_view_center(round_trip):
    original, loaded, path, nodes = round_trip
    expected = original.view().view_center()
    actual = loaded.view().view_center()
    assert abs(actual.x() - expected.x()) <= 2.0
    assert abs(actual.y() - expected.y()) <= 2.0


def test_round_trip_resave_is_identical(round_trip, tmp_path):
    original, loaded, path, nodes = round_trip
    second = str(tmp_path / "resaved.json")
    assert loaded.SaveTree(second)
    first_data, second_data = read_json(path), read_json(second)
    for key in ("format", "version", "config", "blackboard", "connections"):
        assert second_data[key] == first_data[key], key
    assert sorted(second_data["nodes"], key=lambda n: n["id"]) == sorted(first_data["nodes"], key=lambda n: n["id"])
    assert second_data["view"]["zoom"] == first_data["view"]["zoom"]


def test_loaded_round_trip_tree_is_unmodified(round_trip, qtbot):
    original, loaded, path, nodes = round_trip
    qtbot.wait(50)  # deferred layout / framing work must not count as an edit
    assert loaded.IsModified() is False
    assert loaded.IsTreeLoaded() is True
    assert loaded.GetFilePath() == os.path.abspath(path)


def test_zoom_and_center_restored_when_loaded_before_show(round_trip, unshown_widget, qtbot):
    original, loaded, path, nodes = round_trip
    hidden = unshown_widget()
    hidden.resize(1200, 800)
    assert hidden.LoadTree(path)
    hidden.show()
    qtbot.wait(50)
    assert hidden.view().zoom() == pytest.approx(1.5)
    expected = original.view().view_center()
    actual = hidden.view().view_center()
    assert abs(actual.x() - expected.x()) <= 2.0 and abs(actual.y() - expected.y()) <= 2.0, (
        f"saved view centre ({expected.x():.1f}, {expected.y():.1f}) was replaced by "
        f"({actual.x():.1f}, {actual.y():.1f}) when the widget was shown"
    )


# ============================================================================ unknown types / damaged files
UNKNOWN_FIELDS = {"speed": 3, "name": "x", "ratio": 0.5, "on": True, "mode": ["a", "b", "c"], "pair": (1, 2)}


def unknown_type_file(tmp_path) -> str:
    fields = dict(UNKNOWN_FIELDS)
    fields["pair"] = {"__tuple__": [1, 2]}
    return write_tree_file(
        tmp_path / "unknown.json",
        [
            node_entry("r", "Root"),
            node_entry("m", "Mystery", 40, 220, title="Mystery Node", fields=fields, field_selections={"mode": 2}),
        ],
        [{"parent": "r", "child": "m"}],
    )


def test_unknown_node_type_creates_placeholder_and_warns(make_widget, dialogs, tmp_path):
    widget = make_widget(node_types=[Succeed])
    dialogs.open_path = unknown_type_file(tmp_path)
    widget.button("Load").click()

    assert widget.IsTreeLoaded()
    assert "warning" in dialogs.kinds()
    warning_args = [args for kind, args in dialogs.shown if kind == "warning"][0]
    assert any("Mystery" in str(arg) for arg in warning_args)
    node = node_by_id(widget, "m")
    assert isinstance(node, UnknownLeafNodeWidget)
    assert node.GetTypeName() == "Mystery"
    assert node.GetTitle() == "Mystery Node"
    assert node.GetFields() == UNKNOWN_FIELDS
    assert node.GetFieldSelectionIndex("mode") == 2
    assert node.GetParent() is widget.GetRootNode()
    assert widget.IsModified() is False


def test_unknown_node_type_programmatic_load_logs_warning(make_widget, dialogs, tmp_path, caplog):
    widget = make_widget(node_types=[Succeed])
    with caplog.at_level("WARNING", logger="behavior_tree_widget"):
        assert widget.LoadTree(unknown_type_file(tmp_path))
    assert isinstance(node_by_id(widget, "m"), UnknownLeafNodeWidget)
    assert any("Mystery" in record.getMessage() for record in caplog.records)


def test_resaving_unknown_node_preserves_type_and_fields(make_widget, tmp_path, clean_registry):
    widget = make_widget(node_types=[Succeed])
    assert widget.LoadTree(unknown_type_file(tmp_path))
    out = tmp_path / "resaved.json"
    assert widget.SaveTree(str(out))

    [saved] = [node for node in read_json(out)["nodes"] if node["id"] == "m"]
    assert saved["type"] == "Mystery"
    assert saved["title"] == "Mystery Node"
    assert (saved["x"], saved["y"]) == (40, 220)
    assert saved["fields"]["pair"] == {"__tuple__": [1, 2]}
    assert {key: value for key, value in saved["fields"].items() if key != "pair"} == {
        key: value for key, value in UNKNOWN_FIELDS.items() if key != "pair"
    }
    assert saved["field_selections"] == {"mode": 2}
    assert {"parent": "r", "child": "m"} in read_json(out)["connections"]

    class Mystery(LeafNodeWidget):
        _title = "Mystery"
        _fields = {"speed": 1, "name": "", "ratio": 0.0, "on": False, "mode": ["a", "b", "c"], "pair": (0, 0)}
        RUN_IN_THREAD = False

        def OnRun(self, tree):
            return True

    knows = make_widget(node_types=[Succeed, Mystery])
    assert knows.LoadTree(str(out))
    real = node_by_id(knows, "m")
    assert isinstance(real, Mystery)
    assert real.GetFields() == UNKNOWN_FIELDS
    assert real.GetFieldSelection("mode") == "c"


def test_file_without_root_gets_a_root(make_widget, dialogs, tmp_path):
    dialogs.open_path = write_tree_file(
        tmp_path / "rootless.json",
        [node_entry("s", "Sequence", 0, 100), node_entry("a", "Succeed", 100, 250)],
        [{"parent": "s", "child": "a"}],
    )
    widget = make_widget()
    widget.button("Load").click()
    assert widget.IsTreeLoaded()
    root = widget.GetRootNode()
    assert isinstance(root, RootNodeWidget)
    assert len(widget.GetNodes()) == 3
    assert sum(isinstance(node, RootNodeWidget) for node in widget.GetNodes()) == 1
    assert node_by_id(widget, "a").GetParent() is node_by_id(widget, "s")
    assert "warning" in dialogs.kinds()


def test_extra_roots_are_skipped(make_widget, dialogs, tmp_path):
    dialogs.open_path = write_tree_file(
        tmp_path / "two_roots.json",
        [
            node_entry("r1", "Root", composite="Selector"),
            node_entry("r2", "Root", 400, 0, composite="Sequence"),
            node_entry("a", "Succeed", 0, 200),
            node_entry("b", "FailNode", 400, 200),
        ],
        [{"parent": "r1", "child": "a"}, {"parent": "r2", "child": "b"}],
    )
    widget = make_widget()
    widget.button("Load").click()
    roots = [node for node in widget.GetNodes() if isinstance(node, RootNodeWidget)]
    assert len(roots) == 1
    assert roots[0].GetId() == "r1"
    assert roots[0].GetCompositeType() == "Selector"
    assert {node.GetId() for node in widget.GetNodes()} == {"r1", "a", "b"}
    assert node_by_id(widget, "a").GetParent() is roots[0]
    assert node_by_id(widget, "b").GetParent() is None
    assert "warning" in dialogs.kinds()


def test_invalid_connections_are_skipped(make_widget, dialogs, tmp_path):
    dialogs.open_path = write_tree_file(
        tmp_path / "connections.json",
        [
            node_entry("r", "Root"),
            node_entry("s1", "Sequence", -200, 150),
            node_entry("s2", "Selector", 200, 150),
            node_entry("s3", "Sequence", 600, 150),
            node_entry("a", "Succeed", -200, 350),
            node_entry("b", "FailNode", 200, 350),
        ],
        [
            {"parent": "r", "child": "s1"},  # valid
            {"parent": "s1", "child": "s2"},  # valid
            {"parent": "s2", "child": "s1"},  # cycle
            {"parent": "s3", "child": "s3"},  # self connection
            {"parent": "s1", "child": "a"},  # valid
            {"parent": "s2", "child": "a"},  # second parent
            {"parent": "ghost", "child": "b"},  # unknown parent id
            {"parent": "s2", "child": "ghost"},  # unknown child id
            {"parent": "a", "child": "b"},  # leaf cannot have children
            {"parent": "s2", "child": "r"},  # root cannot have a parent
            "not an object",
            {"parent": "s2", "child": "b"},  # valid
        ],
    )
    widget = make_widget()
    widget.button("Load").click()

    assert widget.IsTreeLoaded()

    def parent_id(node_id):
        parent = node_by_id(widget, node_id).GetParent()
        return parent.GetId() if parent is not None else None

    assert {node_id: parent_id(node_id) for node_id in ("r", "s1", "s2", "s3", "a", "b")} == {
        "r": None, "s1": "r", "s2": "s1", "s3": None, "a": "s1", "b": "s2",
    }
    assert len(widget.view().connections()) == 4
    assert "warning" in dialogs.kinds()


def test_duplicate_and_malformed_node_entries_are_skipped(make_widget, tmp_path):
    path = write_tree_file(
        tmp_path / "nodes.json",
        [
            node_entry("r", "Root"),
            node_entry("a", "Succeed", 0, 200),
            node_entry("a", "FailNode", 300, 200),  # duplicate id
            {"type": "Succeed", "x": 0, "y": 0},  # no id
            {"id": "t", "x": 0, "y": 0},  # no type
            "not an object",
            node_entry("p", "Succeed", x="left", y=None),  # invalid position
        ],
    )
    widget = make_widget()
    assert widget.LoadTree(path)
    assert {node.GetId() for node in widget.GetNodes()} == {"r", "a", "p"}
    assert isinstance(node_by_id(widget, "a"), Succeed)
    assert position(node_by_id(widget, "p")) == (0.0, 0.0)


def test_file_connection_order_does_not_change_left_to_right_order(make_widget, tmp_path):
    path = write_tree_file(
        tmp_path / "order.json",
        [node_entry("r", "Root"), node_entry("right", "Succeed", 400, 200), node_entry("left", "FailNode", -400, 200)],
        [{"parent": "r", "child": "right"}, {"parent": "r", "child": "left"}],
    )
    widget = make_widget()
    assert widget.LoadTree(path)
    assert [child.GetId() for child in widget.GetRootNode().GetChildren()] == ["left", "right"]


# ============================================================================ modified tracking
EDITS = {
    "add node": lambda bt, connected, loose: bt.AddNode("FailNode", 500, 500),
    "remove node": lambda bt, connected, loose: bt.RemoveNode(loose),
    "connect": lambda bt, connected, loose: bt.Connect(bt.GetRootNode(), loose),
    "disconnect": lambda bt, connected, loose: bt.Disconnect(connected),
    "rename node": lambda bt, connected, loose: loose.SetTitle("Renamed"),
    "int field editor": lambda bt, connected, loose: connected.field_editor("count").setValue(99),
    "float field editor": lambda bt, connected, loose: connected.field_editor("ratio").setValue(1.75),
    "str field editor": lambda bt, connected, loose: connected.field_editor("label").setText("typed"),
    "bool field editor": lambda bt, connected, loose: connected.field_editor("enabled").setChecked(False),
    "list field editor": lambda bt, connected, loose: connected.field_editor("choice").setCurrentIndex(2),
    "SetField": lambda bt, connected, loose: connected.SetField("ratio", 1.5),
    "SetFieldSelectionIndex": lambda bt, connected, loose: connected.SetFieldSelectionIndex("choice", 1),
    "SetCompositeType": lambda bt, connected, loose: bt.GetRootNode().SetCompositeType("Selector"),
    "SetMemory": lambda bt, connected, loose: bt.GetRootNode().SetMemory(False),
    "SetEntry": lambda bt, connected, loose: bt.SetEntry(3, "counter"),
    "AddEntry": lambda bt, connected, loose: bt.AddEntry("names", "List", ["a"]),
    "SetConfig": lambda bt, connected, loose: fast_config(bt),
}


@pytest.mark.parametrize("edit", list(EDITS))
def test_edits_mark_tree_modified(saved_bt, edit):
    bt, connected, loose = saved_bt
    changes = []
    bt.modifiedChanged.connect(changes.append)
    EDITS[edit](bt, connected, loose)
    assert bt.IsModified() is True
    assert changes == [True]


def test_memory_menu_action_marks_modified(saved_bt):
    bt, connected, loose = saved_bt
    menu = bt.view().build_node_menu(bt.GetRootNode())
    [memory] = [action for action in menu.actions() if action.objectName() == "Memory"]
    memory.trigger()
    assert bt.GetRootNode().GetMemory() is False
    assert bt.IsModified() is True
    menu.deleteLater()


def test_moving_a_node_marks_modified(saved_bt):
    bt, connected, loose = saved_bt
    bt.view().centerOn(loose._item)
    start = viewport_point(bt, loose, loose.findChild(QLabel, "Title"))
    before = position(loose)
    drag(bt, start, start + QPoint(60, 40))
    assert position(loose) != before
    assert bt.IsModified() is True


def test_clicking_a_node_without_moving_it_does_not_mark_modified(saved_bt):
    bt, connected, loose = saved_bt
    bt.view().centerOn(loose._item)
    before = position(loose)
    click(bt, viewport_point(bt, loose, loose.findChild(QLabel, "Title")))
    assert position(loose) == before
    assert bt.IsModified() is False


def test_execution_does_not_mark_modified(qtbot, saved_bt):
    bt, connected, loose = saved_bt
    counter = bt.AddNode("IncrementCounter", 600, 200)
    bt.Connect(bt.GetRootNode(), counter)
    fast_config(bt)
    mark_saved(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: not bt.IsExecuting(), timeout=3000)
    assert bt.GetEntry("counter") == 1
    assert bt.GetRootNode().GetStatus() is NodeStatus.SUCCEEDED
    assert bt.IsModified() is False


@pytest.mark.parametrize("operation", ["save", "save as", "load", "new"])
def test_save_load_and_new_clear_modified(saved_bt, dialogs, tmp_path, operation):
    bt, connected, loose = saved_bt
    other = simple_tree_file(tmp_path / "other.json")
    bt.AddNode("FailNode", 500, 500)
    assert bt.IsModified()
    changes = []
    bt.modifiedChanged.connect(changes.append)
    dialogs.question_answer = DISCARD
    if operation == "save":
        bt.SaveTree(bt.GetFilePath())
    elif operation == "save as":
        dialogs.save_path = str(tmp_path / "save_as.json")
        bt.button("Save").click()
    elif operation == "load":
        dialogs.open_path = other
        bt.button("Load").click()
    else:
        dialogs.save_path = str(tmp_path / "fresh.json")
        bt.button("New").click()
    assert bt.IsModified() is False
    assert changes == [False]


# ============================================================================ ConfirmDiscardChanges
def test_confirm_does_not_ask_without_changes(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    assert bt.ConfirmDiscardChanges() is True
    assert dialogs.shown == []


def test_confirm_save_answer_saves_to_current_file(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    extra = bt.AddNode("FailNode", 500, 500)
    dialogs.question_answer = SAVE
    assert bt.ConfirmDiscardChanges() is True
    assert dialogs.kinds() == ["question"]
    assert extra.GetId() in saved_ids(bt.GetFilePath())
    assert bt.IsModified() is False


def test_confirm_discard_answer_does_not_save(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    extra = bt.AddNode("FailNode", 500, 500)
    dialogs.question_answer = DISCARD
    assert bt.ConfirmDiscardChanges() is True
    assert dialogs.kinds() == ["question"]
    assert extra.GetId() not in saved_ids(bt.GetFilePath())


def test_confirm_cancel_answer_returns_false(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    extra = bt.AddNode("FailNode", 500, 500)
    dialogs.question_answer = CANCEL
    assert bt.ConfirmDiscardChanges() is False
    assert extra.GetId() not in saved_ids(bt.GetFilePath())
    assert bt.IsModified() is True


def test_confirm_save_failure_returns_false(make_widget, dialogs, tmp_path):
    folder = tmp_path / "sub"
    folder.mkdir()
    widget = make_widget()
    assert widget.NewTree(str(folder / "tree.json"))
    widget.AddNode("Succeed", 100, 200)
    shutil.rmtree(folder)
    dialogs.question_answer = SAVE
    assert widget.ConfirmDiscardChanges() is False
    assert dialogs.kinds() == ["question", "critical"]
    assert widget.IsModified() is True


@pytest.mark.parametrize("button", ["Load", "New"])
def test_load_and_new_honour_cancel(saved_bt, dialogs, tmp_path, button):
    bt, connected, loose = saved_bt
    extra = bt.AddNode("FailNode", 500, 500)
    path, title = bt.GetFilePath(), bt.windowTitle()
    dialogs.open_path = simple_tree_file(tmp_path / "other.json")
    dialogs.save_path = str(tmp_path / "fresh.json")
    dialogs.question_answer = CANCEL
    bt.button(button).click()
    assert dialogs.kinds() == ["question"]
    assert extra in bt.GetNodes()
    assert bt.GetFilePath() == path
    assert bt.windowTitle() == title
    assert bt.IsModified() is True
    assert not (tmp_path / "fresh.json").exists()


@pytest.mark.parametrize("button", ["Load", "New"])
def test_load_and_new_after_discard(saved_bt, dialogs, tmp_path, button):
    bt, connected, loose = saved_bt
    original = bt.GetFilePath()
    extra = bt.AddNode("FailNode", 500, 500)
    dialogs.open_path = simple_tree_file(tmp_path / "other.json")
    dialogs.save_path = str(tmp_path / "fresh.json")
    dialogs.question_answer = DISCARD
    bt.button(button).click()
    expected = "other.json" if button == "Load" else "fresh.json"
    assert dialogs.kinds() == ["question", "open_dialog" if button == "Load" else "save_dialog"]
    assert bt.GetFilePath() == os.path.abspath(str(tmp_path / expected))
    assert bt.windowTitle() == expected_title(expected)
    assert extra.GetId() not in saved_ids(original)


@pytest.mark.parametrize("button", ["Load", "New"])
def test_load_and_new_after_save_answer_save_first(saved_bt, dialogs, tmp_path, button):
    bt, connected, loose = saved_bt
    original = bt.GetFilePath()
    extra = bt.AddNode("FailNode", 500, 500)
    dialogs.open_path = simple_tree_file(tmp_path / "other.json")
    dialogs.save_path = str(tmp_path / "fresh.json")
    dialogs.question_answer = SAVE
    bt.button(button).click()
    assert extra.GetId() in saved_ids(original)
    assert bt.GetFilePath() != original
    assert bt.IsModified() is False


def test_closing_modified_top_level_widget_asks(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    bt.AddNode("FailNode", 500, 500)
    dialogs.question_answer = CANCEL
    assert bt.close() is False
    assert bt.isVisible()
    dialogs.question_answer = DISCARD
    assert bt.close() is True
    assert not bt.isVisible()


# ============================================================================ Load / New while executing
def start_forever_running(qtbot, bt) -> ForeverRunning:
    runner = bt.AddNode(ForeverRunning, 0, 220)
    bt.Connect(bt.GetRootNode(), runner)
    fast_config(bt)
    mark_saved(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: runner.GetStatus() is NodeStatus.RUNNING, timeout=2000)
    assert bt.IsExecuting()
    return runner


@pytest.mark.parametrize("paused", [False, True], ids=["running", "paused"])
@pytest.mark.parametrize("operation", ["LoadTree", "NewTree", "Load button", "New button"])
def test_load_or_new_while_executing_stops_execution_first(qtbot, bt, dialogs, tmp_path, operation, paused):
    runner = start_forever_running(qtbot, bt)
    if paused:
        bt.Pause()
        assert bt.GetExecutionState() == "Paused"
    states = []
    bt.executionStateChanged.connect(lambda state: states.append((state, runner in bt.GetNodes())))
    other = simple_tree_file(tmp_path / "other.json")
    fresh = str(tmp_path / "fresh.json")
    if operation == "LoadTree":
        assert bt.LoadTree(other)
    elif operation == "NewTree":
        assert bt.NewTree(fresh)
    elif operation == "Load button":
        dialogs.open_path = other
        bt.button("Load").click()
    else:
        dialogs.save_path = fresh
        bt.button("New").click()

    assert bt.IsExecuting() is False
    assert bt.GetExecutionState() == "Idle"
    assert states == [("Idle", True)], "execution must stop while the old tree is still in place"
    assert runner.terminated == [NodeStatus.READY]
    assert bt.view().is_locked() is False
    assert bt.GetFilePath() == os.path.abspath(other if "Load" in operation else fresh)
    assert runner not in bt.GetNodes()
    assert bt.button("Execute").isEnabled() and not bt.button("Stop").isEnabled()
    assert not bt.button("Pause").isEnabled()


def test_load_while_threaded_leaf_runs_cancels_it(qtbot, bt, tmp_path):
    SlowCancelable.cancelled.clear()
    slow = bt.AddNode("SlowCancelable", 0, 220)
    bt.Connect(bt.GetRootNode(), slow)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: slow.GetStatus() is NodeStatus.RUNNING, timeout=2000)
    assert bt.LoadTree(simple_tree_file(tmp_path / "other.json"))
    assert not bt.IsExecuting()
    assert SlowCancelable.cancelled.wait(3.0)


# ============================================================================ Configure
def test_configure_button_opens_configure_dialog_with_current_values(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    bt.SetConfig(TreeConfig(tick_interval_ms=321, repeat=True, restore_blackboard=False, default_memory=True))
    seen = {}

    def handler(dialog):
        seen["dialog"] = dialog
        seen["config"] = dialog.config()
        return False

    dialogs.dialog_handler = handler
    bt.button("Configure").click()
    assert dialogs.kinds() == ["dialog"]
    assert isinstance(seen["dialog"], ConfigureDialog)
    assert seen["config"] == bt.GetConfig()


def test_configure_accept_applies_config_and_marks_modified(saved_bt, dialogs):
    bt, connected, loose = saved_bt

    def handler(dialog):
        dialog.tick_interval.setValue(250)
        dialog.run_mode.setCurrentIndex(1)
        dialog.restore_blackboard.setChecked(True)
        dialog.default_memory.setChecked(False)
        return True

    dialogs.dialog_handler = handler
    bt.button("Configure").click()
    assert bt.GetConfig() == TreeConfig(tick_interval_ms=250, repeat=True, restore_blackboard=True, default_memory=False)
    assert bt.IsModified() is True
    assert bt.AddNode("Sequence", 0, 400).GetMemory() is False
    mark_saved(bt)
    assert read_json(bt.GetFilePath())["config"] == {
        "tick_interval_ms": 250, "repeat": True, "restore_blackboard": True, "default_memory": False,
    }


def test_configure_reject_changes_nothing(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    before = bt.GetConfig()

    def handler(dialog):
        dialog.tick_interval.setValue(999)
        dialog.run_mode.setCurrentIndex(1)
        return False

    dialogs.dialog_handler = handler
    bt.button("Configure").click()
    assert bt.GetConfig() == before
    assert bt.IsModified() is False


def test_configure_accept_without_changes_is_not_a_modification(saved_bt, dialogs):
    bt, connected, loose = saved_bt
    dialogs.dialog_handler = lambda dialog: True
    bt.button("Configure").click()
    assert dialogs.kinds() == ["dialog"]
    assert bt.IsModified() is False


def test_set_config_validates_and_get_config_returns_copy(bt):
    with pytest.raises(TypeError):
        bt.SetConfig({"tick_interval_ms": 5})
    config = bt.GetConfig()
    config.tick_interval_ms = 999
    assert bt.GetConfig().tick_interval_ms != 999


# ============================================================================ node type registration
class _NotALeaf(QWidget):
    pass


@pytest.mark.parametrize(
    "candidate",
    [_NotALeaf, QWidget, NodeWidget, CompositeNodeWidget, RootNodeWidget, LeafNodeWidget, UnknownLeafNodeWidget, "Succeed", 42],
    ids=lambda value: getattr(value, "__name__", repr(value)),
)
def test_register_node_type_rejects_non_leaf_classes(make_widget, candidate):
    widget = make_widget()
    before = widget.GetNodeTypes()
    with pytest.raises(TypeError):
        widget.RegisterNodeType(candidate)
    assert widget.GetNodeTypes() == before


def _leaf_class(class_name: str, type_name: str | None) -> type:
    namespace = {"RUN_IN_THREAD": False, "OnRun": lambda self, tree: True}
    if type_name is not None:
        namespace["TYPE_NAME"] = type_name
    return type(class_name, (LeafNodeWidget,), namespace)


RESERVED = {
    "TYPE_NAME Root": ("RootLike", "Root"),
    "TYPE_NAME Sequence": ("SequenceLike", "Sequence"),
    "TYPE_NAME Selector": ("SelectorLike", "Selector"),
    "class Root": ("Root", None),
    "class Sequence": ("Sequence", None),
    "class Selector": ("Selector", None),
}


@pytest.mark.parametrize("case", list(RESERVED))
def test_register_node_type_rejects_reserved_names(make_widget, case):
    widget = make_widget()
    before = widget.GetNodeTypes()
    with pytest.raises(ValueError):
        widget.RegisterNodeType(_leaf_class(*RESERVED[case]))
    assert widget.GetNodeTypes() == before


@pytest.mark.parametrize("case", ["TYPE_NAME Root", "class Sequence"])
def test_register_node_type_decorator_rejects_reserved_names(clean_registry, case):
    with pytest.raises(ValueError):
        register_node_type(_leaf_class(*RESERVED[case]))
    assert registered_node_types() == {}


def test_register_node_type_decorator_rejects_non_leaf(clean_registry):
    with pytest.raises(TypeError):
        register_node_type(_NotALeaf)
    assert registered_node_types() == {}


def test_constructor_rejects_invalid_node_types(qtbot):
    with pytest.raises(TypeError):
        BehaviorTreeWidget(node_types=[_NotALeaf])


def test_register_node_type_decorator_registers_globally(make_widget, clean_registry, tmp_path):
    @register_node_type
    class Beep(LeafNodeWidget):
        _title = "Beep"
        _fields = {"volume": 3}
        RUN_IN_THREAD = False

        def OnRun(self, tree):
            return True

    assert isinstance(Beep, type) and issubclass(Beep, LeafNodeWidget)  # the decorator returns the class
    assert registered_node_types() == {"Beep": Beep}

    widget = make_widget(node_types=[])
    assert "Beep" in widget.GetNodeTypes()
    assert widget.GetNodeType("Beep") is Beep
    assert widget.NewTree(str(tmp_path / "beep.json"))
    node = widget.AddNode("Beep", 0, 200)
    assert isinstance(node, Beep)
    assert widget.SaveTree(widget.GetFilePath())

    loader = make_widget(node_types=[])
    assert loader.LoadTree(widget.GetFilePath())
    assert isinstance(node_by_id(loader, node.GetId()), Beep)


def test_register_node_type_decorator_respects_type_name(make_widget, clean_registry):
    @register_node_type
    class Internal(LeafNodeWidget):
        TYPE_NAME = "PublicName"
        RUN_IN_THREAD = False

        def OnRun(self, tree):
            return True

    assert registered_node_types() == {"PublicName": Internal}
    widget = make_widget(node_types=[])
    assert widget.GetNodeType("PublicName") is Internal
    assert widget.AddNode("PublicName", 0, 200).GetTypeName() == "PublicName"


def test_registered_node_types_returns_a_copy(clean_registry):
    @register_node_type
    class Copied(LeafNodeWidget):
        def OnRun(self, tree):
            return True

    snapshot = registered_node_types()
    snapshot.clear()
    assert registered_node_types() == {"Copied": Copied}


def test_widget_registration_is_local_to_the_widget(make_widget, clean_registry):
    first = make_widget(node_types=[Succeed])
    second = make_widget(node_types=[Succeed])
    first.RegisterNodeType(FailNode)
    assert "FailNode" in first.GetNodeTypes()
    assert "FailNode" not in second.GetNodeTypes()
    assert registered_node_types() == {}


def test_get_node_types_lists_composites_then_sorted_leaves(make_widget, clean_registry):
    widget = make_widget(node_types=[Succeed, FailNode, AllFields])
    assert widget.GetNodeTypes() == [
        "Sequence", "Selector", "Negation", "Evaluation", "Set", "AllFields", "FailNode", "Succeed",
    ]
    assert widget.GetNodeType("Succeed") is Succeed
    assert widget.GetNodeType("Sequence") is None
    assert widget.GetNodeType("Negation") is None
    assert widget.GetNodeType("Unknown") is None


def test_add_node_with_unregistered_class_registers_it(make_widget, clean_registry, tmp_path):
    widget = make_widget(node_types=[Succeed])
    assert widget.NewTree(str(tmp_path / "auto.json"))
    assert widget.GetNodeType("AllFields") is None
    node = widget.AddNode(AllFields, 30, 200)
    assert isinstance(node, AllFields)
    assert node in widget.GetNodes()
    assert widget.GetNodeType("AllFields") is AllFields
    assert "AllFields" in widget.GetNodeTypes()
    assert isinstance(widget.AddNode("AllFields", 300, 200), AllFields)


def test_add_node_rejects_unknown_and_invalid_types(bt):
    count = len(bt.GetNodes())
    with pytest.raises(ValueError):
        bt.AddNode("NoSuchType")
    with pytest.raises(ValueError):
        bt.AddNode("Root")
    with pytest.raises(TypeError):
        bt.AddNode(_NotALeaf)
    assert len(bt.GetNodes()) == count


# ============================================================================ Shutdown / independence
def test_shutdown_is_idempotent_and_releases_blackboard_keys(bt):
    bt.SetEntry(1, "value")
    key = bt.blackboardStore().key("value")
    assert py_trees.blackboard.Blackboard.exists(key)
    bt.Shutdown()
    bt.Shutdown()
    assert not py_trees.blackboard.Blackboard.exists(key)
    assert bt.IsExecuting() is False


def test_shutdown_stops_execution(qtbot, bt):
    runner = start_forever_running(qtbot, bt)
    bt.Shutdown()
    assert bt.IsExecuting() is False
    ticks = bt.executor().tick_count()
    qtbot.wait(50)
    assert bt.executor().tick_count() == ticks
    assert runner.terminated == [NodeStatus.READY]
    bt.Shutdown()


def test_two_widgets_have_independent_trees_and_blackboards(make_widget, tmp_path):
    first, second = make_widget(), make_widget()
    assert first.NewTree(str(tmp_path / "first.json"))
    assert second.NewTree(str(tmp_path / "second.json"))

    leaf = first.AddNode("Succeed", 0, 200)
    first.Connect(first.GetRootNode(), leaf)
    assert leaf in first.GetNodes() and leaf not in second.GetNodes()
    assert len(second.GetNodes()) == 1
    assert second.GetRootNode().GetChildren() == []
    assert first.GetRootNode() is not second.GetRootNode()

    first.SetEntry(1, "shared")
    second.SetEntry("text", "shared")
    first.SetEntry(2.5, "only_first")
    assert first.GetEntry("shared") == 1
    assert second.GetEntry("shared") == "text"
    assert not second.HasEntry("only_first")
    assert first.GetBlackboardNamespace() != second.GetBlackboardNamespace()

    fast_config(first)
    assert second.GetConfig() == TreeConfig()
    assert first.windowTitle() == expected_title("first.json")
    assert second.windowTitle() == expected_title("second.json")

    first.Shutdown()
    assert second.GetEntry("shared") == "text"
    assert second.GetEntryNames() == ["shared"]


# ============================================================================ demo window
def test_demo_window_constructs_and_closes_without_prompts(qtbot, dialogs):
    window = DemoWindow()
    qtbot.addWidget(window)
    window.show()
    assert isinstance(window.tree, BehaviorTreeWidget)
    assert window.centralWidget() is window.tree
    assert window.tree.IsTreeLoaded() is False
    for cls in DEMO_NODE_TYPES:
        assert cls.__name__ in window.tree.GetNodeTypes()
    assert window.close() is True
    assert not window.isVisible()
    assert dialogs.shown == []


def test_demo_window_with_unmodified_tree_closes_without_prompts(qtbot, dialogs, tmp_path):
    window = DemoWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.tree.NewTree(str(tmp_path / "demo.json"))
    assert window.windowTitle() == expected_title("demo.json")
    assert window.close() is True
    assert dialogs.shown == []


def test_demo_window_with_modified_tree_asks_before_closing(qtbot, dialogs, tmp_path):
    window = DemoWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.tree.NewTree(str(tmp_path / "demo.json"))
    window.tree.AddNode("Wait", 0, 200)
    dialogs.question_answer = CANCEL
    assert window.close() is False
    assert window.isVisible()
    assert dialogs.kinds() == ["question"]
    dialogs.question_answer = DISCARD
    assert window.close() is True
    assert not window.isVisible()


# ============================================================================ damaged values inside valid files
def test_malformed_field_value_keeps_default_and_warns(make_widget, dialogs, tmp_path):
    """One unreadable field value does not make the whole file unloadable."""
    path = tmp_path / "bad_field.json"
    path.write_bytes(
        _document_bytes(
            nodes=[
                node_entry("r", "Root"),
                node_entry("a", "AllFields", 0, 200, fields={"choice": {"__set__": [[1, 2]]}, "count": 9}),
            ]
        )
    )
    widget = make_widget()
    dialogs.open_path = str(path)
    widget.button("Load").click()
    assert "warning" in dialogs.kinds() and "critical" not in dialogs.kinds()
    (leaf,) = [node for node in widget.GetNodes() if node.GetTypeName() == "AllFields"]
    assert leaf.GetField("choice") == ["red", "green", "blue"]  # class default kept
    assert leaf.GetField("count") == 9


@pytest.mark.parametrize(
    "connection",
    [
        pytest.param({"parent": ["r"], "child": "a"}, id="list-parent-id"),
        pytest.param({"parent": "r", "child": {"id": "a"}}, id="object-child-id"),
        pytest.param({"parent": 1, "child": "a"}, id="int-parent-id"),
    ],
)
def test_connections_with_unhashable_ids_are_skipped(make_widget, tmp_path, connection):
    path = tmp_path / "ids.json"
    path.write_bytes(
        _document_bytes(nodes=[node_entry("r", "Root"), node_entry("a", "Succeed", 0, 200)], connections=[connection])
    )
    widget = make_widget()
    assert widget.LoadTree(str(path))
    assert widget.GetFilePath() == str(path)
    (leaf,) = [node for node in widget.GetNodes() if node.GetTypeName() == "Succeed"]
    assert leaf.GetParent() is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 10**400])
def test_non_finite_positions_and_view_values_are_rejected(make_widget, tmp_path, bad):
    data = json.loads(_document_bytes(nodes=[node_entry("r", "Root"), node_entry("a", "Succeed", bad, 200)]))
    data["view"] = {"zoom": bad, "center": [bad, 0]}
    path = tmp_path / "nonfinite.json"
    path.write_text(json.dumps(data))
    widget = make_widget()
    assert widget.LoadTree(str(path))
    (leaf,) = [node for node in widget.GetNodes() if node.GetTypeName() == "Succeed"]
    assert leaf._item.pos().x() == 0.0
    assert widget.view().zoom() == 1.0
