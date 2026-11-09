"""Helpers, node types and fixtures of the regression tests that came from
test_regressions_round3.py (the tests now live in the feature files that use them)."""

from __future__ import annotations

import functools
import json
import logging
import os
import stat
import sys
import threading
from types import SimpleNamespace

import py_trees
import pytest
from py_trees.common import Status
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QPoint, Qt, QTimer, qInstallMessageHandler
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel
from pytestqt.exceptions import capture_exceptions

from behavior_tree_widget import (
    BehaviorTreeWidget,
    BlackboardStore,
    CompositeNodeWidget,
    LeafNodeWidget,
    NodeStatus,
    TreeFileError,
)
from behavior_tree_widget.canvas import SCENE_EXTENT, SCENE_MARGIN, Hit
from behavior_tree_widget.execution import TreeExecutor
from behavior_tree_widget.nodes import NodeWidget
from behavior_tree_widget.serialization import FORMAT_NAME, FORMAT_VERSION

from conftest import SlowCancelable, fast_config, viewport_point

READY, RUNNING, SUCCEEDED, FAILED = "Ready", "Running", "Succeeded", "Failed"


LOGGER = "behavior_tree_widget"


LEFT = Qt.MouseButton.LeftButton


NO_MOD = Qt.KeyboardModifier.NoModifier


CTRL = Qt.KeyboardModifier.ControlModifier


GUI_THREAD = threading.get_ident()  # test modules are imported on the main (GUI) thread


POSITION_LIMIT = SCENE_EXTENT / 2 - SCENE_MARGIN  # 48000


WINDOWS = sys.platform == "win32"


# ============================================================================ node types
class R3Probe(LeafNodeWidget):
    """GUI-thread leaf recording its lifecycle into ``self.log`` as ``(hook, tag, ...)``.

    ``on_start(tree, node)``, ``on_run(tree, node)`` and ``on_terminate(tree, node, status)``
    run inside the hooks when set. OnRun returns ``on_run``'s result when it is not None,
    otherwise RUNNING ``steps`` times per run and then True.
    """

    _title = "R3 Probe"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.log: list[tuple] = []
        self.tag = "probe"
        self.steps = 0
        self._left = 0
        self.on_start = None
        self.on_run = None
        self.on_terminate = None
        self.threads: dict[str, list[int]] = {"start": [], "run": [], "terminate": []}

    def OnStart(self, tree):
        self.threads["start"].append(threading.get_ident())
        self.log.append(("start", self.tag))
        self._left = self.steps
        if self.on_start is not None:
            self.on_start(tree, self)

    def OnRun(self, tree):
        self.threads["run"].append(threading.get_ident())
        self.log.append(("run", self.tag))
        if self.on_run is not None:
            result = self.on_run(tree, self)
            if result is not None:
                return result
        if self._left > 0:
            self._left -= 1
            return Status.RUNNING
        return True

    def OnTerminate(self, tree, status):
        self.threads["terminate"].append(threading.get_ident())
        self.log.append(("terminate", self.tag, status.value))
        if self.on_terminate is not None:
            self.on_terminate(tree, self, status)


class R3ThreadProbe(R3Probe):
    """Same as R3Probe, but OnRun runs on a worker thread (the tick does not wait by default)."""

    _title = "R3 Thread Probe"
    RUN_IN_THREAD = True
    THREAD_WAIT = 0.0


class R3Untitled(LeafNodeWidget):
    """Declares no _title: the default title is the class name."""

    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class R3LateAssigner(LeafNodeWidget):
    """Worker-thread leaf: waits for ``state["gate"]`` and then assigns ``_title`` / ``_fields``."""

    _title = "R3 Late Assigner"
    _fields = {"n": 1}
    THREAD_WAIT = 0.0

    def OnRun(self, tree):
        state = self.state
        index = state.setdefault("calls", 0)
        state["calls"] = index + 1
        if index > 0:
            return True  # a restarted run (after Reset) does nothing
        state["entered"].set()
        try:
            state["gate"].wait(10)
            outcome = {}
            attempts = {
                "_title": lambda: setattr(self, "_title", "late title"),
                "_title=None": lambda: setattr(self, "_title", None),
                "_fields": lambda: setattr(self, "_fields", {"n": 99}),
            }
            for name, attempt in attempts.items():
                try:
                    attempt()
                    outcome[name] = "ok"
                except BaseException as error:  # noqa: BLE001 - reported to the test
                    outcome[name] = error
            outcome["CancelRequested"] = self.CancelRequested()
            state["outcome"] = outcome
            return True
        finally:
            state["done"].set()


class R3LiveAssigner(LeafNodeWidget):
    """Worker-thread leaf assigning a non-str _title and new _fields (not cancelled)."""

    _title = "R3 Live Assigner"
    _fields = {"n": 1}
    THREAD_WAIT = 5.0

    def OnRun(self, tree):
        self._title = 7
        self._fields = {"n": 2, "label": "set by worker"}
        return True


class R3Worker(LeafNodeWidget):
    """Worker-thread leaf running ``self.body(tree, node)`` (THREAD_WAIT: the tick waits for it)."""

    _title = "R3 Worker"
    THREAD_WAIT = 5.0

    def OnRun(self, tree):
        body = getattr(self, "body", None)
        if body is not None:
            body(tree, self)
        return True


R3_TYPES = [R3Probe, R3ThreadProbe, R3Untitled, R3LateAssigner, R3LiveAssigner, R3Worker]


# ============================================================================ fixtures
@pytest.fixture
def gate():
    """An event worker threads wait on; always set at teardown so no thread stays blocked."""
    event = threading.Event()
    yield event
    event.set()


@pytest.fixture
def qt_messages():
    """Qt log messages (qDebug / qWarning / qCritical, from any thread) emitted during the test."""
    messages: list[tuple] = []
    lock = threading.Lock()

    def handler(mode, context, message):
        with lock:
            messages.append((mode, str(message), threading.get_ident()))

    previous = qInstallMessageHandler(handler)
    try:
        yield messages
    finally:
        qInstallMessageHandler(previous)


class TimerProxy:
    """Stands in for the executor's QTimer and records the thread of every call."""

    def __init__(self, timer, audit):
        self._timer = timer
        self._audit = audit

    def __getattr__(self, name):
        attribute = getattr(self._timer, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            self._audit.record(f"QTimer.{name}")
            return attribute(*args, **kwargs)

        return call


class ThreadAudit:
    """Records on which thread execution internals (state, labels, timers, restore) run."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls: list[tuple[str, int]] = []

    def record(self, what: str) -> None:
        with self._lock:
            self.calls.append((what, threading.get_ident()))

    def off_gui_thread(self) -> list[str]:
        with self._lock:
            return [what for what, thread in self.calls if thread != GUI_THREAD]

    def names(self) -> set[str]:
        with self._lock:
            return {what for what, _ in self.calls}

    def attach(self, widget: BehaviorTreeWidget) -> None:
        executor = widget.executor()
        if not isinstance(executor._timer, TimerProxy):
            executor._timer = TimerProxy(executor._timer, self)


AUDITED = [
    (NodeWidget, "_set_status"),
    (NodeWidget, "_refresh_status"),
    (TreeExecutor, "_run_command"),
    (TreeExecutor, "_set_state"),
    (TreeExecutor, "_halt_tree"),
    (TreeExecutor, "_restore_blackboard"),
    (TreeExecutor, "_set_all_ready"),
    (BlackboardStore, "restore"),
    (BlackboardStore, "set_runtime_active"),
]


@pytest.fixture
def audit(monkeypatch):
    state = ThreadAudit()
    for cls, name in AUDITED:
        original = getattr(cls, name)

        def make_wrapper(original=original, label=f"{cls.__name__}.{name}"):
            @functools.wraps(original)
            def wrapper(self, *args, **kwargs):
                state.record(label)
                return original(self, *args, **kwargs)

            return wrapper

        monkeypatch.setattr(cls, name, make_wrapper())
    return state


@pytest.fixture
def active(qtbot, bt):
    """``bt`` as the active window (focus / activation dependent behaviour)."""
    bt.activateWindow()
    qtbot.waitUntil(bt.isActiveWindow, timeout=2000)
    return bt


@pytest.fixture
def executing(qtbot, bt):
    """Root -> SlowCancelable (running on a worker thread) and an unconnected Selector."""
    SlowCancelable.cancelled.clear()
    root = bt.GetRootNode()
    slow = bt.AddNode("SlowCancelable", -150, 220)
    selector = bt.AddNode("Selector", 200, 220)
    bt.Connect(root, slow)
    bt.view().centerOn(0, 180)
    fast_config(bt)
    bt.Execute()
    try:
        qtbot.waitUntil(lambda: slow.GetStatus() is NodeStatus.RUNNING, timeout=3000)
        yield SimpleNamespace(root=root, slow=slow, selector=selector, view=bt.view())
    finally:
        bt.Stop()


# ============================================================================ helpers
def record(signal) -> list:
    values: list = []
    signal.connect(lambda *args: values.append(args[0] if len(args) == 1 else args))
    return values


def add_leaf(bt, node_type, parent=None, x=0.0, y=220.0):
    node = bt.AddNode(node_type, x, y)
    bt.Connect(parent if parent is not None else bt.GetRootNode(), node)
    return node


def probes(bt, *specs, log=None):
    """Add probe leaves below the root, left to right; ``specs`` are ``(class, tag)`` pairs."""
    log = [] if log is None else log
    nodes = []
    for index, (cls, tag) in enumerate(specs):
        node = add_leaf(bt, cls, x=index * 300.0)
        node.log = log
        node.tag = tag
        nodes.append(node)
    return log, nodes


def events(log, tag) -> list[str]:
    """Lifecycle of one probe: "start", "run", "terminate:<status>"."""
    result = []
    for entry in log:
        if entry[1] != tag:
            continue
        result.append(entry[0] if entry[0] != "terminate" else f"terminate:{entry[2]}")
    return result


def assert_lifecycle(log, tag) -> None:
    """Every run is OnStart, OnRun..., OnTerminate: no OnRun / OnTerminate outside a run."""
    phase = "idle"
    sequence = events(log, tag)
    for kind in sequence:
        if kind == "start":
            assert phase == "idle", f"OnStart during a run of {tag}: {sequence}"
            phase = "running"
        elif kind == "run":
            assert phase == "running", f"OnRun of {tag} outside a run (after OnTerminate): {sequence}"
        else:
            assert phase == "running", f"OnTerminate of {tag} outside a run: {sequence}"
            phase = "idle"


def shown(nodes) -> list[str]:
    """Status of each node; the Status label must show the same text."""
    result = []
    for node in nodes:
        value = node.GetStatus().value
        label = node.findChild(QLabel, "Status")
        assert label is not None and label.text() == value, f"{node}: label {label.text()!r} != {value!r}"
        result.append(value)
    return result


def ticks(bt) -> int:
    return bt.executor().tick_count()


def timer_active(bt) -> bool:
    return bt.executor()._timer.isActive()


def assert_timer_matches_state(bt) -> None:
    state = bt.GetExecutionState()
    assert timer_active(bt) is (state == "Running"), f"tick timer active={timer_active(bt)} while {state}"


def save(bt) -> None:
    assert bt.SaveTree(bt.GetFilePath())
    assert not bt.IsModified()


def buttons(bt) -> dict[str, bool]:
    return {name: bt.button(name).isEnabled() for name in ("Execute", "Pause", "Stop", "Reset")}


def timer_thread_warnings(messages) -> list[str]:
    return [message for _, message, _ in messages if "Timer" in message or "another thread" in message]


def errors_logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith(LOGGER)]


def warnings_logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith(LOGGER)]


def nested_loop(ms: int, during=None, at: int = 0) -> None:
    """Emulate a modal dialog: run a nested event loop for ``ms`` ms, calling ``during`` inside it."""
    loop = QEventLoop()
    if during is not None:
        QTimer.singleShot(at, during)
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def flush_deferred_deletes() -> None:
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def keys_of(namespace: str) -> list[str]:
    return sorted(key for key in py_trees.blackboard.Blackboard.storage if key.startswith(namespace + "/"))


def node_entry(node_id, type_name, x=0.0, y=0.0, **extra) -> dict:
    entry = {"id": node_id, "type": type_name, "title": extra.pop("title", type_name), "x": x, "y": y}
    entry.update(extra)
    return entry


def write_tree(path, nodes, connections=(), blackboard=(), **extra) -> str:
    data = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "nodes": list(nodes),
        "connections": list(connections),
        "blackboard": list(blackboard),
    }
    data.update(extra)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def next_tree_file(tmp_path, name="next.json") -> str:
    """Root r2 -> Succeed n2 and one Integer entry y = 3."""
    return write_tree(
        tmp_path / name,
        [node_entry("r2", "Root"), node_entry("n2", "Succeed", 0, 220, title="Next Leaf")],
        [{"parent": "r2", "child": "n2"}],
        [{"name": "y", "type": "Integer", "value": 3}],
    )


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


def node_ids(widget) -> set[str]:
    return {node.GetId() for node in widget.GetNodes()}


def describe(widget) -> dict:
    """Everything about the current tree that must stay untouched (statuses excluded)."""
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
        "rows": [row.name for row in widget.blackboard_view().rows()],
        "file": widget.GetFilePath(),
        "modified": widget.IsModified(),
        "title": widget.window().windowTitle(),
        "config": widget.GetConfig(),
    }


def rows_in_layout(view) -> list[str]:
    from behavior_tree_widget.blackboard import EntryRow

    names = []
    layout = view._rows_layout
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if isinstance(widget, EntryRow):
            names.append(widget.name)
    return names


def title_point(bt, node) -> QPoint:
    return viewport_point(bt, node, node._title_label)


def line_point(bt, item, percent: float = 0.5) -> QPoint:
    return bt.view().mapFromScene(item.path().pointAtPercent(percent))


def empty_point(bt) -> QPoint:
    point = QPoint(15, 15)
    assert bt.view().hit_test(point).kind == Hit.EMPTY
    return point


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


def warning_texts(dialogs) -> list[str]:
    return [" ".join(str(arg) for arg in args) for kind, args in dialogs.shown if kind == "warning"]


def critical_texts(dialogs) -> list[str]:
    return [" ".join(str(arg) for arg in args) for kind, args in dialogs.shown if kind == "critical"]


# ============================================================================ commands from a worker-thread OnRun
def _worker_command_setup(bt, audit, command, thread_wait, gate):
    """Root -> [worker probe w, GUI probe g (2 RUNNING steps)]; w's first OnRun call writes x = 5,
    issues ``command`` from the worker thread and (after-THREAD_WAIT variant) blocks on ``gate``."""
    audit.attach(bt)
    bt.AddEntry("x", "Integer", 0)
    log, (w, g) = probes(bt, (R3ThreadProbe, "w"), (R3Probe, "g"))
    w.THREAD_WAIT = thread_wait
    g.steps = 2
    info = {"calls": [], "issued": threading.Event(), "second_result": True}

    def on_run(tree, node):
        index = len(info["calls"])
        info["calls"].append(threading.get_ident())
        if index > 0:
            return info["second_result"]
        tree.SetEntry(5, "x")
        getattr(tree, command)()
        info["cancel_after_command"] = node.CancelRequested()
        info["issued"].set()
        if not thread_wait:
            gate.wait(10)  # keep the call running after the tick returned
        info["returned"] = True
        return True

    w.on_run = on_run
    w_statuses = []
    bt.nodeStatusChanged.connect(lambda node, status: w_statuses.append(status) if node is w else None)
    fast_config(bt, restore_blackboard=True)
    save(bt)
    return SimpleNamespace(log=log, w=w, g=g, info=info, w_statuses=w_statuses, root=bt.GetRootNode())


def _assert_gui_thread_only(audit, qt_messages, *probe_nodes) -> None:
    assert audit.off_gui_thread() == [], "execution internals ran on a worker thread"
    assert "QTimer.stop" in audit.names() or "QTimer.start" in audit.names()
    assert timer_thread_warnings(qt_messages) == []
    for node in probe_nodes:
        assert set(node.threads["start"]) <= {GUI_THREAD}, "OnStart must run on the GUI thread"
        assert set(node.threads["terminate"]) <= {GUI_THREAD}, "OnTerminate must run on the GUI thread"


THREAD_WAITS = pytest.mark.parametrize("thread_wait", [0.0, 5.0], ids=["after-thread-wait", "during-thread-wait"])


# ============================================================================ commands issued while a command is carried out
TERMINATE_CASES = [
    # (state before, outer command, command issued from OnTerminate(Ready), state after the outer command)
    ("Paused", "Stop", "Execute", "Running"),
    ("Running", "Stop", "Execute", "Running"),
    ("Running", "Stop", "Pause", "Idle"),
    ("Paused", "Stop", "Reset", "Idle"),
    ("Running", "Stop", "Stop", "Idle"),
    ("Running", "Reset", "Stop", "Idle"),
    ("Running", "Reset", "Pause", "Paused"),
    ("Paused", "Reset", "Execute", "Running"),
    ("Paused", "Reset", "Stop", "Idle"),
]


# ============================================================================ LoadTree / NewTree while the executor is busy
WHERES = ["on_run", "status_slot", "nested_loop", "on_terminate_during_stop"]


def _snapshot_now(bt) -> dict:
    return {
        "ids": node_ids(bt),
        "state": bt.GetExecutionState(),
        "file": bt.GetFilePath(),
        "ticking": bt.executor()._ticking,
        "busy": bt.executor().is_busy(),
    }


def _busy_scenario(qtbot, bt, where, action):
    """Root -> [probe a, probe b (5 steps)]; perform ``action(bt)`` while the executor is busy."""
    log, (a, b) = probes(bt, (R3Probe, "a"), (R3Probe, "b"))
    b.steps = 5
    info: dict = {}

    def call():
        info["log_length"] = len(log)
        try:
            info["result"] = action(bt)
        except Exception as error:  # noqa: BLE001 - reported to the test
            info["error"] = error
        info["inside"] = _snapshot_now(bt)

    if where == "on_run":
        a.on_run = lambda tree, node: call() if not info else None
    elif where == "status_slot":
        bt.nodeStatusChanged.connect(
            lambda node, status: call() if node is a and status == SUCCEEDED and not info else None
        )
    elif where == "nested_loop":
        def nested(tree, node):
            if info:
                return None
            nested_loop(60, during=call)
            info["end_of_loop"] = _snapshot_now(bt)
            return None

        a.on_run = nested
    else:  # on_terminate_during_stop: a runs until the test calls Stop
        a.steps = 100_000
        a.on_terminate = lambda tree, node, status: call() if status is NodeStatus.READY and not info else None
    return log, a, b, info


def _run_busy_scenario(qtbot, bt, where, info):
    with capture_exceptions() as exceptions:
        bt.Execute()
        if where == "on_terminate_during_stop":
            qtbot.waitUntil(lambda: bt.GetExecutionState() == "Running", timeout=2000)
            bt.Stop()
        qtbot.waitUntil(lambda: "inside" in info and not bt.IsExecuting(), timeout=5000)
        qtbot.wait(40)
    return exceptions


BAD_CALLS = {
    "LoadTree-invalid-json": ("LoadTree", "bad.json", TreeFileError),
    "LoadTree-missing-file": ("LoadTree", "missing.json", TreeFileError),
    "LoadTree-not-a-tree": ("LoadTree", "other.json", TreeFileError),
    "NewTree-missing-directory": ("NewTree", os.path.join("no_such_dir", "new.json"), OSError),
}


class _Handle:
    """A run-time object (e.g. a device connection) stored in an Object entry; it can be deep-copied."""

    def __init__(self, label):
        self.label = label
        self.state = [label]


# ============================================================================ non-GUI threads
def _api_calls(bt, tmp_path, other, loose, child):
    root = bt.GetRootNode()
    config = bt.GetConfig()
    config.tick_interval_ms = 777
    return {
        "AddNode(name)": lambda: bt.AddNode("Succeed", 0, 600),
        "AddNode(cls)": lambda: bt.AddNode(R3Untitled, 0, 600),
        "AddNode(composite)": lambda: bt.AddNode("Selector", 0, 600),
        "RemoveNode": lambda: bt.RemoveNode(loose),
        "Connect": lambda: bt.Connect(root, loose),
        "Disconnect": lambda: bt.Disconnect(child),
        "NewTree(path)": lambda: bt.NewTree(str(tmp_path / "from_thread_new.json")),
        "NewTree()": lambda: bt.NewTree(),
        "LoadTree(path)": lambda: bt.LoadTree(other),
        "LoadTree()": lambda: bt.LoadTree(),
        "SaveTree(path)": lambda: bt.SaveTree(str(tmp_path / "from_thread_save.json")),
        "SaveTree()": lambda: bt.SaveTree(),
        "SetConfig": lambda: bt.SetConfig(config),
    }


# ============================================================================ deferred view positioning
def _root_top(widget) -> int:
    root = widget.GetRootNode()
    return widget.view().mapFromScene(root._item.sceneBoundingRect().topLeft()).y()


def _hidden_tree_view_widget(qtbot, make_widget):
    """A widget whose Blackboard tab is current and whose window shrank while the tree page was hidden."""
    widget = make_widget(size=(1200, 800))
    qtbot.wait(30)
    widget.setCurrentIndex(1)
    qtbot.wait(10)
    widget.resize(900, 500)
    qtbot.wait(30)
    assert not widget.view().isVisible()
    return widget


# ============================================================================ _title / _fields assignment
class _Titled:
    def __str__(self):
        return "from __str__"


# ============================================================================ canvas: presses outside the view
@pytest.fixture
def selected(qtbot, active):
    bt = active
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", 0, 220)
    bt.Connect(root, leaf)
    view = bt.view()
    view.centerOn(80, 160)
    qtbot.wait(10)
    item = view.connection_item(leaf)
    QTest.mouseClick(view.viewport(), LEFT, NO_MOD, line_point(bt, item))
    assert view.selected_connection() is item
    assert view.hasFocus()
    return SimpleNamespace(bt=bt, root=root, leaf=leaf, view=view, item=item)


def _frame_background(bt):
    frame = bt._tree_page.findChild(QFrame, "FrameExecution")
    point = QPoint(frame.width() - 6, frame.height() // 2)
    return frame, point


def _page_margin(bt):
    page = bt._tree_page
    return page, QPoint(2, page.height() // 2)


def _disabled_button(name):
    def target(bt):
        button = bt.button(name)
        assert not button.isEnabled(), f"precondition: {name} is disabled"
        return button, QPoint(button.width() // 2, button.height() // 2)

    return target


def _current_tab(bt):
    bar = bt.tabBar()
    return bar, bar.tabRect(bt.currentIndex()).center()


OUTSIDE_TARGETS = {
    "execution-frame-background": _frame_background,
    "tree-page-margin": _page_margin,
    "disabled-stop-button": _disabled_button("Stop"),
    "disabled-pause-button": _disabled_button("Pause"),
    "tab-bar": _current_tab,
}


# ============================================================================ files
def _attributes(path) -> int:
    import ctypes

    function = ctypes.windll.kernel32.GetFileAttributesW
    function.restype = ctypes.c_uint32
    return function(str(path))


def _set_attributes(path, attributes: int) -> None:
    import ctypes

    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes)


HIDDEN, SYSTEM = 0x2, 0x4


@pytest.fixture
def read_only_file(tmp_path):
    target = tmp_path / "ro_BT.json"  # named like a tree file: SaveTree writes to it
    target.write_text('{"old": 1}\n', encoding="utf-8")
    os.chmod(target, stat.S_IREAD)
    yield target
    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


class _OsProxy:
    """``os`` for the serialization module with a replaced ``replace``."""

    def __init__(self, replace):
        self.replace = replace

    def __getattr__(self, name):
        return getattr(os, name)
