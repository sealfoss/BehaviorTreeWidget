"""Regression tests for the round-3 fix pass (CHANGES_ROUND3.md; CHANGES_ROUND2.md still applies).

Every test asserts the *intended* behaviour of the round-3 notes:

Execution command dispatch
    * Stop / Reset / Execute / Pause issued from a worker-thread ``OnRun`` (after
      ``THREAD_WAIT`` elapsed and while the tick still waits for the call): tokens are
      cancelled at once, everything else happens on the GUI thread (state, labels,
      ``OnTerminate``, timers, blackboard restore); the interrupted call's result is
      ignored; Qt never warns about timers used from another thread.
    * Stop / Reset from ``OnStart`` (GUI and threaded leaves): ``OnRun`` never runs,
      ``OnTerminate(Ready)`` is called.
    * commands issued while another command is carried out (``OnTerminate`` during
      Stop / Reset, ``stateChanged("Running")`` slots) are queued and run afterwards;
      no ``OnRun`` after ``OnTerminate`` of the same run, no active timer while
      Idle / Paused.
    * run-once completion happens after the tick: ``ticked`` / ``finished`` are emitted
      before commands issued from a ``stateChanged("Idle")`` slot run.
    * ``call_when_idle``; the executor's blackboard restore never marks the tree
      modified; ``Shutdown`` disables the execution buttons.

Widget
    * LoadTree / NewTree while the executor is busy (OnRun, nodeStatusChanged, nested
      event loop, OnTerminate, stateChanged): validated at once (errors raise at once
      and change nothing), the tree is replaced right after the tick, True is returned.
    * failed (deferred) installs restore the previous tree; the view is unlocked first;
      interactive NewTree reports install failures.
    * the structure / file API refuses non-GUI threads; execution / entry API works
      from any thread; node positions are clamped; the destroyed cleanup is silent.
    * the deferred initial view positioning follows the TreeView being shown and is
      cancelled by user pan / zoom.

Blackboard, nodes / canvas, files
    * Object entries are never saved (no warning), not loaded, kept by New / Load;
      invalid entry names in files are skipped; the load order follows the file.
    * ``_title`` coercion; cancelled workers cannot assign ``_title`` / ``_fields``;
      context-menu Rename is a user edit while executing; stale menu actions do
      nothing; a stale passthrough gesture releases the scene's mouse grab; presses
      outside the view deselect a connection (not while a popup is open).
    * UnknownLeafNodeWidget keeps undecodable fields / unknown keys unchanged;
      read_tree_file converts parser failures; write_json_atomic keeps hidden / system
      attributes, names read-only targets, retries and overwrites held files in place.

Modal UI never blocks: the autouse ``dialogs`` fixture replaces every dialog and menu.
Worker threads are synchronised with events; no test depends on sleeps for ordering.
"""

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
import shiboken6
from py_trees.common import Status
from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QPoint,
    QPointF,
    Qt,
    QtMsgType,
    QTimer,
    qInstallMessageHandler,
)
from PySide6.QtGui import QContextMenuEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QWidget
from pytestqt.exceptions import capture_exceptions

from behavior_tree_widget import (
    BehaviorTreeWidget,
    BlackboardStore,
    CompositeNodeWidget,
    ExecutionCancelled,
    LeafNodeWidget,
    NodeStatus,
    TreeFileError,
    UnknownLeafNodeWidget,
)
from behavior_tree_widget import serialization as serialization_module
from behavior_tree_widget.canvas import SCENE_EXTENT, SCENE_MARGIN, Hit, _Gesture
from behavior_tree_widget.execution import TreeExecutor
from behavior_tree_widget.nodes import NodeWidget
from behavior_tree_widget.serialization import FORMAT_NAME, FORMAT_VERSION, read_tree_file, write_json_atomic
from conftest import SlowCancelable, fast_config, run_until_idle, viewport_point

READY, RUNNING, SUCCEEDED, FAILED = "Ready", "Running", "Succeeded", "Failed"
LOGGER = "behavior_tree_widget"
LEFT = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier
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
        "rows": [row.name for row in widget.blackboardView().rows()],
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


# ============================================================================ harness sanity
def test_qt_message_capture_sees_cross_thread_timer_warnings(qapp, qt_messages):
    """The assertions "no 'Timers cannot be stopped from another thread' warning" below are only
    meaningful if the capture sees that warning: provoke it with a plain QTimer."""
    timer = QTimer()
    timer.start(1000)
    thread = threading.Thread(target=timer.stop)
    thread.start()
    thread.join(5)
    timer.stop()
    assert any("Timers cannot be stopped from another thread" in message for message in timer_thread_warnings(qt_messages))


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


@THREAD_WAITS
def test_stop_from_worker_thread_on_run(qtbot, bt, audit, qt_messages, gate, capfd, thread_wait):
    ns = _worker_command_setup(bt, audit, "Stop", thread_wait, gate)
    finished = record(bt.executionFinished)
    states = record(bt.executionStateChanged)
    bt.Execute()
    qtbot.waitUntil(lambda: ns.info["issued"].is_set() and bt.GetExecutionState() == "Idle", timeout=5000)
    assert ns.info["cancel_after_command"] is True, "Stop from a worker must cancel the running call's token at once"
    assert bt.GetEntry("x") == 0, "restore_blackboard must restore the value written before Stop"
    assert shown([ns.root, ns.w, ns.g]) == [READY] * 3
    assert not bt.view().is_locked()
    assert buttons(bt) == {"Execute": True, "Pause": False, "Stop": False, "Reset": True}
    assert events(ns.log, "w") == ["start", "run", "terminate:Ready"]
    assert ns.w.threads["terminate"] == [GUI_THREAD]
    assert events(ns.log, "g") == []
    assert not timer_active(bt)

    gate.set()
    qtbot.waitUntil(lambda: ns.info.get("returned", False), timeout=5000)
    qtbot.waitUntil(lambda: ns.w._worker_future is not None and ns.w._worker_future.done(), timeout=5000)
    qtbot.wait(40)
    assert bt.GetExecutionState() == "Idle"
    assert finished == [], "the interrupted call's result must be ignored"
    assert SUCCEEDED not in ns.w_statuses
    assert shown([ns.root, ns.w, ns.g]) == [READY] * 3
    assert states == ["Running", "Idle"]
    assert len(ns.info["calls"]) == 1 and ns.info["calls"][0] != GUI_THREAD
    assert bt.GetEntry("x") == 0
    assert not bt.IsModified()
    assert not timer_active(bt)
    _assert_gui_thread_only(audit, qt_messages, ns.w, ns.g)
    assert "Timers cannot" not in capfd.readouterr().err


@THREAD_WAITS
def test_reset_from_worker_thread_on_run(qtbot, bt, audit, qt_messages, gate, capfd, thread_wait):
    ns = _worker_command_setup(bt, audit, "Reset", thread_wait, gate)
    ns.info["second_result"] = False  # the restarted run fails: proves the interrupted True was ignored
    finished = record(bt.executionFinished)
    bt.Execute()
    qtbot.waitUntil(lambda: ("terminate", "w", READY) in ns.log, timeout=5000)
    assert ns.info["cancel_after_command"] is True, "Reset from a worker must cancel the running call's token at once"
    assert set(ns.w.threads["terminate"]) == {GUI_THREAD}
    assert bt.GetEntry("x") == 0, "restore_blackboard must restore the value written before Reset"
    if not thread_wait:
        assert bt.GetExecutionState() == "Running", "Reset keeps executing"
        count = ticks(bt)
        qtbot.waitUntil(lambda: ticks(bt) >= count + 4, timeout=3000)
        # the restarted run waits (Running) until the interrupted call has returned
        assert events(ns.log, "w") == ["start", "run", "terminate:Ready"]
        assert shown([ns.w]) == [RUNNING]
    gate.set()
    run_until_idle(qtbot, bt)
    qtbot.wait(20)
    assert events(ns.log, "w") == ["start", "run", "terminate:Ready", "start", "run", "terminate:Failed"]
    assert finished == [FAILED], "the interrupted call's True must not complete the restarted run"
    assert SUCCEEDED not in ns.w_statuses
    assert events(ns.log, "g") == []
    assert len(ns.info["calls"]) == 2
    assert bt.GetEntry("x") == 0
    assert not bt.IsModified()
    assert not timer_active(bt)
    _assert_gui_thread_only(audit, qt_messages, ns.w, ns.g)
    assert "Timers cannot" not in capfd.readouterr().err


@THREAD_WAITS
def test_execute_from_worker_thread_on_run_does_not_restart(qtbot, bt, audit, qt_messages, gate, thread_wait):
    ns = _worker_command_setup(bt, audit, "Execute", thread_wait, gate)
    finished = record(bt.executionFinished)
    states = record(bt.executionStateChanged)
    bt.Execute()
    qtbot.waitUntil(ns.info["issued"].is_set, timeout=5000)
    count = ticks(bt)
    qtbot.waitUntil(lambda: ticks(bt) >= count + 2 or not bt.IsExecuting(), timeout=3000)
    assert ns.info["cancel_after_command"] is False, "Execute must not cancel the running call"
    gate.set()
    run_until_idle(qtbot, bt)
    qtbot.wait(30)
    assert finished == [SUCCEEDED]
    assert states == ["Running", "Idle"]
    assert events(ns.log, "w") == ["start", "run", "terminate:Succeeded"]
    assert events(ns.log, "g") == ["start", "run", "run", "run", "terminate:Succeeded"]
    assert len(ns.info["calls"]) == 1
    assert bt.GetEntry("x") == 5, "a completed run-once execution does not restore the blackboard"
    assert not timer_active(bt)
    _assert_gui_thread_only(audit, qt_messages, ns.w, ns.g)


@THREAD_WAITS
def test_pause_from_worker_thread_on_run(qtbot, bt, audit, qt_messages, gate, thread_wait):
    ns = _worker_command_setup(bt, audit, "Pause", thread_wait, gate)
    finished = record(bt.executionFinished)
    bt.Execute()
    qtbot.waitUntil(lambda: ns.info["issued"].is_set() and bt.GetExecutionState() == "Paused", timeout=5000)
    assert ns.info["cancel_after_command"] is False, "Pause must not cancel the running call"
    assert not timer_active(bt), "the tick timer must be stopped (on the GUI thread) while paused"
    assert bt.view().is_locked()
    assert buttons(bt) == {"Execute": True, "Pause": False, "Stop": True, "Reset": True}
    count = ticks(bt)
    gate.set()
    qtbot.waitUntil(lambda: ns.info.get("returned", False), timeout=5000)
    qtbot.wait(40)
    assert bt.GetExecutionState() == "Paused"
    assert ticks(bt) == count, "no tick while paused"
    if thread_wait:
        # collected within the tick that started it; g started in that tick
        assert shown([ns.w, ns.g]) == [SUCCEEDED, RUNNING]
    else:
        assert shown([ns.w]) == [RUNNING], "the result is collected on the next tick after resuming"
        assert events(ns.log, "g") == []
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert finished == [SUCCEEDED]
    assert events(ns.log, "w") == ["start", "run", "terminate:Succeeded"]
    assert len(ns.info["calls"]) == 1
    assert bt.GetEntry("x") == 5
    assert not timer_active(bt)
    _assert_gui_thread_only(audit, qt_messages, ns.w, ns.g)


def test_execution_and_entry_api_work_from_any_thread(qtbot, bt, audit, qt_messages):
    """Execute / Pause / Reset / Stop / GetEntry / SetEntry from a plain (non-worker) thread."""
    audit.attach(bt)
    bt.AddEntry("n", "Integer", 0)
    log, (a,) = probes(bt, (R3Probe, "a"))
    a.steps = 100_000
    fast_config(bt)
    save(bt)
    errors = []

    def in_thread(action):
        def run():
            try:
                action()
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()

    in_thread(lambda: bt.SetEntry(bt.GetEntry("n") + 41, "n"))
    assert bt.GetEntry("n") == 41
    in_thread(bt.Execute)
    qtbot.waitUntil(lambda: bt.GetExecutionState() == "Running" and ("run", "a") in log, timeout=3000)
    in_thread(bt.Pause)
    qtbot.waitUntil(lambda: bt.GetExecutionState() == "Paused", timeout=3000)
    assert not timer_active(bt)
    in_thread(bt.Execute)
    qtbot.waitUntil(lambda: bt.GetExecutionState() == "Running", timeout=3000)
    in_thread(bt.Reset)
    qtbot.waitUntil(lambda: events(log, "a").count("start") >= 2, timeout=3000)
    assert "terminate:Ready" in events(log, "a")
    in_thread(bt.Stop)
    qtbot.waitUntil(lambda: bt.GetExecutionState() == "Idle", timeout=3000)
    assert errors == []
    assert not timer_active(bt)
    assert not bt.view().is_locked()
    assert_lifecycle(log, "a")
    assert audit.off_gui_thread() == []
    assert timer_thread_warnings(qt_messages) == []
    assert not bt.IsModified(), "SetEntry from another thread is a run-time change"


# ============================================================================ Stop / Reset from OnStart
@pytest.mark.parametrize("command", ["Stop", "Reset"])
@pytest.mark.parametrize("leaf_class", [R3Probe, R3ThreadProbe], ids=["gui-leaf", "threaded-leaf"])
def test_stop_or_reset_from_on_start_never_calls_on_run(qtbot, bt, audit, qt_messages, command, leaf_class):
    audit.attach(bt)
    bt.AddEntry("x", "Integer", 0)
    log, (a, b) = probes(bt, (leaf_class, "a"), (R3Probe, "b"))
    a.THREAD_WAIT = 5.0
    seen = []

    def on_start(tree, node):
        if events(log, "a").count("start") == 1:
            tree.SetEntry(5, "x")
            getattr(tree, command)()
            seen.append((tree.GetExecutionState(), node.CancelRequested()))

    a.on_start = on_start
    finished = record(bt.executionFinished)
    fast_config(bt, restore_blackboard=True)
    save(bt)
    bt.Execute()
    assert seen == [("Running", True)], "the command is deferred until the tick ends; the run is cancelled at once"
    if command == "Stop":
        qtbot.wait(40)
        assert bt.GetExecutionState() == "Idle"
        assert events(log, "a") == ["start", "terminate:Ready"]
        assert a.threads["run"] == [], "OnRun must not be called after OnStart issued Stop"
        assert events(log, "b") == []
        assert finished == []
        assert shown([bt.GetRootNode(), a, b]) == [READY] * 3
        assert not bt.view().is_locked()
    else:
        run_until_idle(qtbot, bt)
        assert events(log, "a") == ["start", "terminate:Ready", "start", "run", "terminate:Succeeded"]
        assert len(a.threads["run"]) == 1, "OnRun must not be called in the run whose OnStart issued Reset"
        assert events(log, "b") == ["start", "run", "terminate:Succeeded"]
        assert finished == [SUCCEEDED]
        assert shown([bt.GetRootNode(), a, b]) == [SUCCEEDED] * 3
    assert bt.GetEntry("x") == 0
    assert set(a.threads["terminate"]) == {GUI_THREAD}
    assert not timer_active(bt)
    assert not bt.IsModified()
    assert audit.off_gui_thread() == []
    assert timer_thread_warnings(qt_messages) == []


def test_stop_during_a_tick_stops_the_tick_timer_at_once(qtbot, bt):
    log, (a, b) = probes(bt, (R3Probe, "a"), (R3Probe, "b"))
    b.steps = 10
    seen = []

    def stop(tree, node):
        tree.Stop()
        seen.append((timer_active(bt), tree.GetExecutionState(), node.CancelRequested()))

    a.on_run = stop
    fast_config(bt)
    bt.Execute()
    assert seen == [(False, "Running", True)], "Stop during a tick stops the timer at once; the rest is deferred"
    assert bt.GetExecutionState() == "Idle"
    assert events(log, "b") == []


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


@pytest.mark.parametrize(
    "initial,outer,inner,expected", TERMINATE_CASES, ids=[f"{i}-{o}-then-{n}" for i, o, n, _ in TERMINATE_CASES]
)
def test_command_from_on_terminate_is_queued(qtbot, bt, audit, qt_messages, initial, outer, inner, expected):
    audit.attach(bt)
    log, (a,) = probes(bt, (R3Probe, "a"))
    a.steps = 100_000
    issued = []

    def on_terminate(tree, node, status):
        if status is NodeStatus.READY and not issued:
            issued.append(tree.GetExecutionState())
            getattr(tree, inner)()
            issued.append(tree.GetExecutionState())

    a.on_terminate = on_terminate
    fast_config(bt, tick_interval_ms=10)
    bt.Execute()
    if initial == "Paused":
        bt.Pause()
    assert bt.GetExecutionState() == initial
    before = len(log)
    getattr(bt, outer)()
    assert len(issued) == 2
    assert issued[0] == issued[1] == initial, f"{inner} was carried out inside OnTerminate instead of being queued"
    assert bt.GetExecutionState() == expected
    assert_timer_matches_state(bt)
    assert_lifecycle(log, "a")
    new = events(log[before:], "a")
    if expected == "Running":
        assert new == ["terminate:Ready", "start", "run"], new
    else:
        assert new == ["terminate:Ready"], new
    count, length = ticks(bt), len(log)
    qtbot.wait(50)
    assert_lifecycle(log, "a")
    assert_timer_matches_state(bt)
    if expected != "Running":
        assert ticks(bt) == count and len(log) == length, "the tree was ticked while Idle / Paused"
    bt.Stop()
    assert bt.GetExecutionState() == "Idle"
    assert not timer_active(bt)
    assert_lifecycle(log, "a")
    assert not bt.view().is_locked()
    assert audit.off_gui_thread() == []
    assert timer_thread_warnings(qt_messages) == []


@pytest.mark.parametrize(
    "sequence,expected",
    [(("Execute", "Pause"), "Paused"), (("Pause", "Execute"), "Running"), (("Execute", "Stop"), "Idle")],
    ids=["execute-then-pause", "pause-then-execute", "execute-then-stop"],
)
def test_commands_queued_during_stop_run_in_order(qtbot, bt, sequence, expected):
    log, (a,) = probes(bt, (R3Probe, "a"))
    a.steps = 100_000
    issued = []

    def on_terminate(tree, node, status):
        if status is NodeStatus.READY and not issued:
            for command in sequence:
                getattr(tree, command)()
                issued.append((command, tree.GetExecutionState()))

    a.on_terminate = on_terminate
    fast_config(bt)
    bt.Execute()
    bt.Stop()
    assert [state for _, state in issued] == ["Running"] * len(sequence), "commands must be queued during Stop"
    assert bt.GetExecutionState() == expected
    assert_timer_matches_state(bt)
    assert_lifecycle(log, "a")
    if sequence[0] == "Execute":
        assert events(log, "a")[3:5] == ["start", "run"], "the queued Execute started a new run first"
    bt.Stop()
    assert not timer_active(bt)


@pytest.mark.parametrize("trigger", ["start", "resume"])
@pytest.mark.parametrize("command", ["Stop", "Pause", "Reset", "Execute"])
def test_command_from_running_state_slot(qtbot, bt, audit, qt_messages, trigger, command):
    audit.attach(bt)
    log, (a,) = probes(bt, (R3Probe, "a"))
    a.steps = 3
    armed = {"on": trigger == "start", "fired": 0, "state_after": None}

    def on_state(state):
        if state == "Running" and armed["on"] and not armed["fired"]:
            armed["fired"] += 1
            getattr(bt, command)()
            armed["state_after"] = bt.GetExecutionState()

    bt.executionStateChanged.connect(on_state)
    finished = record(bt.executionFinished)
    fast_config(bt)
    if trigger == "resume":
        bt.Execute()
        bt.Pause()
        armed["on"] = True
    bt.Execute()
    assert armed["fired"] == 1
    assert armed["state_after"] == "Running", "the command must be queued while the start command is carried out"
    assert_lifecycle(log, "a")
    assert_timer_matches_state(bt)
    # A command issued while Execute is carried out takes effect before any node code runs
    # (at "start"); on "resume" the node had already started in the earlier Execute.
    if command in ("Stop", "Pause") and trigger == "start":
        assert events(log, "a") == [], "no node code may run before the command issued during Execute"
    if command == "Stop":
        assert bt.GetExecutionState() == "Idle"
        assert shown([bt.GetRootNode(), a]) == [READY, READY]
        if trigger == "start":
            assert events(log, "a") == [], "no node code may run after a Stop issued during Execute"
        else:
            assert events(log, "a")[-1] == "terminate:Ready"
        assert not bt.view().is_locked()
        assert buttons(bt) == {"Execute": True, "Pause": False, "Stop": False, "Reset": True}
        qtbot.wait(40)
        assert finished == []
    elif command == "Pause":
        assert bt.GetExecutionState() == "Paused"
        assert buttons(bt) == {"Execute": True, "Pause": False, "Stop": True, "Reset": True}
        count = ticks(bt)
        qtbot.wait(40)
        assert ticks(bt) == count
        bt.Execute()
        run_until_idle(qtbot, bt)
        assert finished == [SUCCEEDED]
    else:
        run_until_idle(qtbot, bt)
        assert finished == [SUCCEEDED]
        starts = events(log, "a").count("start")
        if command == "Execute":
            assert starts == 1, "Execute while running must not restart the tree"
        elif trigger == "start":
            # Reset issued during Execute: the tree starts from scratch before any node code ran.
            assert starts == 1
        else:
            assert starts == 2 and "terminate:Ready" in events(log, "a")
    assert_lifecycle(log, "a")
    assert not timer_active(bt) or bt.GetExecutionState() == "Running"
    assert audit.off_gui_thread() == []
    assert timer_thread_warnings(qt_messages) == []


# ============================================================================ run-once completion after the tick
@pytest.mark.parametrize("command", ["Stop", "Reset", "LoadTree", "NewTree", "Execute"])
def test_command_from_idle_state_slot_runs_after_finished(qtbot, bt, tmp_path, command):
    other = next_tree_file(tmp_path)
    fresh = str(tmp_path / "fresh.json")
    bt.AddEntry("x", "Integer", 0)
    log, (w,) = probes(bt, (R3Probe, "w"))
    w.on_run = lambda tree, node: tree.SetEntry(tree.GetEntry("x") + 1, "x")
    fast_config(bt, restore_blackboard=True)
    save(bt)
    old_file, old_root = bt.GetFilePath(), bt.GetRootNode()
    trace = []
    bt.executionStateChanged.connect(lambda state: trace.append(("state", state)))
    bt.executor().ticked.connect(
        lambda n: trace.append(("ticked", n, bt.GetExecutionState(), bt.executor().tree() is None, timer_active(bt)))
    )
    bt.executionFinished.connect(
        lambda status: trace.append(
            ("finished", status, bt.GetEntry("x") if bt.HasEntry("x") else None, bt.GetFilePath(), bt.GetRootNode() is old_root)
        )
    )
    issued = {}

    def on_state(state):
        if state == "Idle" and not issued:
            issued["busy"] = bt.executor().is_busy()
            issued["ticking"] = bt.executor()._ticking
            if command == "LoadTree":
                issued["result"] = bt.LoadTree(other)
            elif command == "NewTree":
                issued["result"] = bt.NewTree(fresh)
            else:
                getattr(bt, command)()
            trace.append(("issued",))

    bt.executionStateChanged.connect(on_state)
    bt.Execute()
    if command == "Execute":
        run_until_idle(qtbot, bt)
    qtbot.wait(40)
    assert issued["ticking"] is False, "run-once completion must happen after the tick"
    assert issued["busy"] is True, "run-once completion happens inside a busy section"
    finished = [entry for entry in trace if entry[0] == "finished"]
    assert len(finished) == (2 if command == "Execute" else 1), trace
    first = trace.index(finished[0])
    idle = trace.index(("state", "Idle"))
    issued_at = trace.index(("issued",))
    ticked_at = max(i for i, entry in enumerate(trace[:first]) if entry[0] == "ticked")
    assert idle < issued_at < ticked_at < first, trace
    assert trace[ticked_at][2:] == ("Idle", True, False), "ticked must see a consistent Idle state"
    assert finished[0][1:] == (SUCCEEDED, 1, old_file, True), "the queued command ran before executionFinished"
    assert bt.GetExecutionState() == "Idle"
    assert not bt.IsModified()
    if command == "Stop":
        assert bt.GetEntry("x") == 1
    elif command == "Reset":
        assert bt.GetEntry("x") == 0
    elif command == "LoadTree":
        assert issued["result"] is True
        assert bt.GetFilePath() == os.path.abspath(other)
        assert bt.blackboardStore().items() == [("y", "Integer", 3)]
    elif command == "NewTree":
        assert issued["result"] is True
        assert bt.GetFilePath() == os.path.abspath(fresh)
        assert bt.blackboardStore().items() == []
    else:
        restart = trace.index(("state", "Running"), first)
        assert restart > first, "Execute from the Idle slot must start the next run after executionFinished"
        assert finished[1][1:3] == (SUCCEEDED, 2)
        assert events(log, "w").count("start") == 2


def test_call_when_idle(qtbot, bt):
    executor = bt.executor()
    calls = []
    executor.call_when_idle(lambda: calls.append(("idle", executor.is_busy())))
    assert calls == [("idle", False)], "call_when_idle runs the function at once when idle"

    log, (a, b) = probes(bt, (R3Probe, "a"), (R3Probe, "b"))
    b.steps = 50

    def on_run(tree, node):
        if events(log, "a").count("run") == 1:
            executor.call_when_idle(lambda: calls.append(("before-stop", executor.is_busy(), tree.GetExecutionState())))
            tree.Stop()
            executor.call_when_idle(lambda: calls.append(("after-stop", executor.is_busy(), tree.GetExecutionState())))
            calls.append(("in-on-run",))

    a.on_run = on_run
    fast_config(bt)
    bt.Execute()
    assert calls[1:] == [("in-on-run",), ("before-stop", False, "Running"), ("after-stop", False, "Idle")]

    calls.clear()
    b.on_terminate = lambda tree, node, status: executor.call_when_idle(
        lambda: calls.append(("after-terminate", tree.GetExecutionState(), shown([a, b])))
    )
    a.on_run = None
    bt.Execute()
    qtbot.waitUntil(lambda: b.GetStatus() is NodeStatus.RUNNING, timeout=2000)
    bt.Stop()
    assert calls == [("after-terminate", "Idle", [READY, READY])]


# ============================================================================ restore_blackboard is a run-time change
@pytest.mark.parametrize("mode", ["reset-idle-after-run", "stop-while-running", "reset-while-running", "stop-while-paused"])
def test_blackboard_restore_never_marks_the_tree_modified(qtbot, bt, mode):
    bt.AddEntry("x", "Integer", 0)
    bt.AddEntry("ratio", "Double", 0.5)
    bt.AddEntry("gone", "String", "keep me")
    bt.AddEntry("kind", "Integer", 1)
    bt.AddEntry("items", "List", [1, 2])
    bt.AddEntry("flag", "Bool", False)
    log, (w,) = probes(bt, (R3Probe, "w"))
    w.steps = 0 if mode == "reset-idle-after-run" else 100_000

    def write(tree, node):
        if events(log, "w").count("run") == 1:
            tree.SetEntry(5, "x")
            tree.SetEntry(2.25, "ratio")
            tree.RemoveEntry("gone")
            tree.RemoveEntry("kind")
            tree.SetEntry("text", "kind")
            tree.SetEntry([9], "items")
            tree.SetEntry(True, "flag")
            tree.SetEntry("new", "made")

    w.on_run = write
    fast_config(bt, restore_blackboard=True)
    save(bt)
    expected = bt.blackboardStore().items()
    modified = record(bt.modifiedChanged)
    bt.Execute()
    if mode == "reset-idle-after-run":
        run_until_idle(qtbot, bt)
    elif mode == "stop-while-paused":
        bt.Pause()
    QCoreApplication.processEvents()
    assert bt.GetEntry("x") == 5 and bt.GetEntryType("kind") == "String"
    if mode.startswith("reset"):
        bt.Reset()
    else:
        bt.Stop()
    qtbot.wait(30)
    assert bt.blackboardStore().items() == expected
    view = bt.blackboardView()
    assert rows_in_layout(view) == [name for name, _, _ in expected]
    assert view.row("x").value_widget.value() == 0
    assert view.row("ratio").value_widget.value() == 0.5
    assert view.row("gone").value_widget.text() == "keep me"
    assert view.row("kind").type_name == "Integer"
    assert view.row("made") is None
    assert bt.IsModified() is False, "the executor's blackboard restore is not an edit of the tree"
    assert modified == []
    bt.Stop()
    qtbot.wait(20)
    assert bt.IsModified() is False and modified == []


# ============================================================================ Shutdown
@pytest.mark.parametrize("when", ["idle", "running", "paused", "from-on-run", "from-running-slot"])
def test_shutdown_disables_the_execution_buttons(qtbot, bt, caplog, when):
    log, (a, b) = probes(bt, (R3Probe, "a"), (R3Probe, "b"))
    a.steps = 0 if when == "from-on-run" else 100_000
    fast_config(bt)
    if when == "from-on-run":
        a.on_run = lambda tree, node: tree.Shutdown()
        bt.Execute()
    elif when == "from-running-slot":
        bt.executionStateChanged.connect(lambda state: bt.Shutdown() if state == "Running" else None)
        bt.Execute()
    else:
        if when in ("running", "paused"):
            bt.Execute()
        if when == "paused":
            bt.Pause()
        bt.Shutdown()
    assert bt.GetExecutionState() == "Idle"
    assert not bt.view().is_locked()
    assert not timer_active(bt)
    assert buttons(bt) == {"Execute": False, "Pause": False, "Stop": False, "Reset": False}
    if when == "from-on-run":
        assert events(log, "b") == [], "no user code may run after Shutdown in the same tick"
    assert_lifecycle(log, "a")
    length, count = len(log), ticks(bt)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for name in ("Execute", "Pause", "Stop", "Reset"):
            bt.button(name).click()
        bt.Execute()
    qtbot.wait(30)
    assert bt.GetExecutionState() == "Idle"
    assert len(log) == length and ticks(bt) == count
    assert buttons(bt) == {"Execute": False, "Pause": False, "Stop": False, "Reset": False}


@pytest.mark.parametrize("source", ["on_terminate", "idle_state_slot"])
def test_work_queued_during_shutdown_is_carried_out(qtbot, bt, source):
    """Shutdown() stops execution like Stop: work queued meanwhile (call_when_idle from OnTerminate or
    from a stateChanged("Idle") slot) must run once that stop has finished."""
    log, (a,) = probes(bt, (R3Probe, "a"))
    a.steps = 100_000
    ran = []

    def queue_work(tree):
        ran.append("queued")
        tree.executor().call_when_idle(lambda: ran.append(("ran", tree.GetExecutionState(), tree.executor().is_busy())))

    if source == "on_terminate":
        a.on_terminate = lambda tree, node, status: queue_work(tree) if status is NodeStatus.READY and not ran else None
    else:
        bt.executionStateChanged.connect(lambda state: queue_work(bt) if state == "Idle" and not ran else None)
    fast_config(bt)
    bt.Execute()
    bt.Shutdown()
    assert bt.GetExecutionState() == "Idle"
    assert ran[:1] == ["queued"], "precondition: work was queued during Shutdown's stop"
    qtbot.wait(30)
    assert ran == ["queued", ("ran", "Idle", False)], (
        "work queued while Shutdown() stops execution is never carried out "
        f"(still pending: {bt.executor()._pending!r})"
    )


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


@pytest.mark.parametrize("where", WHERES)
@pytest.mark.parametrize("operation", ["LoadTree", "NewTree"])
def test_load_or_new_while_busy_replaces_tree_right_after(qtbot, bt, tmp_path, where, operation):
    other = next_tree_file(tmp_path)
    fresh = str(tmp_path / "fresh.json")
    target = other if operation == "LoadTree" else fresh
    bt.AddEntry("x", "Integer", 0)
    before_run, during_run = _Handle("before"), _Handle("during")
    bt.SetEntry(before_run, "obj")

    def action(tree):
        tree.SetEntry(1, "x")
        tree.SetEntry(during_run, "obj")
        result = tree.LoadTree(other) if operation == "LoadTree" else tree.NewTree(fresh)
        action.file_written = os.path.exists(fresh)
        return result

    log, a, b, info = _busy_scenario(qtbot, bt, where, action)
    fast_config(bt, restore_blackboard=True)
    save(bt)
    old_ids, old_file = node_ids(bt), bt.GetFilePath()
    loaded = record(bt.treeLoaded)
    finished = record(bt.executionFinished)
    exceptions = _run_busy_scenario(qtbot, bt, where, info)
    assert exceptions == []
    assert info.get("error") is None, repr(info.get("error"))
    assert info["result"] is True
    inside = info["inside"]
    assert inside["busy"] is True
    assert inside["ids"] == old_ids, "the tree must only be replaced after the current tick / command"
    assert inside["file"] == old_file
    if where != "on_terminate_during_stop":
        assert inside["state"] == "Running"
    if operation == "NewTree":
        assert action.file_written, "NewTree writes the new file at once"
    if where == "nested_loop":
        assert info["end_of_loop"]["ids"] == old_ids
        assert info["end_of_loop"]["state"] == "Running"
    # replaced right after the tick
    assert bt.GetExecutionState() == "Idle"
    assert not bt.view().is_locked()
    assert not timer_active(bt)
    assert bt.GetFilePath() == os.path.abspath(target)
    assert bt.GetEntry("obj") is during_run, (
        "the previous run's blackboard snapshot must be discarded: the Object entry kept by the load was "
        "rolled back to its pre-run value"
    )

    def saved_items():
        return [item for item in bt.blackboardStore().items() if item[0] != "obj"]

    if operation == "LoadTree":
        assert node_ids(bt) == {"r2", "n2"}
        assert saved_items() == [("y", "Integer", 3)], "the previous run's snapshot was restored"
    else:
        assert len(bt.GetNodes()) == 1 and bt.GetRootNode() is not None
        assert saved_items() == []
    assert bt.GetEntryNames()[-1] == "obj"
    assert a not in bt.GetNodes() and b not in bt.GetNodes()
    assert loaded == [os.path.abspath(target)]
    assert finished == []
    assert bt.IsModified() is False
    assert bt.window().windowTitle() == f"Behavior Tree - {os.path.basename(target)}"
    assert all(node.GetStatus() is NodeStatus.READY for node in bt.GetNodes())
    after_call = events(log[info["log_length"]:], "b")
    assert "start" not in after_call and "run" not in after_call, f"b ran after the load: {after_call}"
    items = saved_items()
    bt.Reset()  # no stale snapshot of the previous tree may be restored
    assert saved_items() == items
    assert bt.GetEntry("obj") is during_run
    finished.clear()
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert finished == [SUCCEEDED]


BAD_CALLS = {
    "LoadTree-invalid-json": ("LoadTree", "bad.json", TreeFileError),
    "LoadTree-missing-file": ("LoadTree", "missing.json", TreeFileError),
    "LoadTree-not-a-tree": ("LoadTree", "other.json", TreeFileError),
    "NewTree-missing-directory": ("NewTree", os.path.join("no_such_dir", "new.json"), OSError),
}


@pytest.mark.parametrize("where", WHERES)
@pytest.mark.parametrize("bad", list(BAD_CALLS))
def test_invalid_load_while_busy_raises_at_once_and_changes_nothing(qtbot, bt, tmp_path, where, bad):
    operation, name, expected_error = BAD_CALLS[bad]
    (tmp_path / "bad.json").write_text('{"format": "behavior_tree_widget", "version": 1, "nodes": [', encoding="utf-8")
    (tmp_path / "other.json").write_text('{"format": "something else", "version": 1}', encoding="utf-8")
    path = str(tmp_path / name)
    bt.AddEntry("x", "Integer", 0)
    log, a, b, info = _busy_scenario(qtbot, bt, where, lambda tree: getattr(tree, operation)(path))
    fast_config(bt)
    save(bt)
    before = describe(bt)
    loaded = record(bt.treeLoaded)
    finished = record(bt.executionFinished)
    exceptions = _run_busy_scenario(qtbot, bt, where, info)
    assert exceptions == []
    assert isinstance(info.get("error"), expected_error), repr(info.get("error"))
    assert "result" not in info
    if where != "on_terminate_during_stop":
        assert finished == [SUCCEEDED], "execution must continue after a failed LoadTree / NewTree"
        assert events(log, "b")[-1] == "terminate:Succeeded"
    assert describe(bt) == before
    assert loaded == []
    assert bt.GetExecutionState() == "Idle"
    assert not bt.view().is_locked()
    if operation == "NewTree":
        assert not os.path.exists(path)


@pytest.mark.parametrize("valid", [True, False], ids=["valid-file", "invalid-file"])
def test_load_button_in_nested_loop_during_tick(qtbot, bt, dialogs, tmp_path, valid):
    other = next_tree_file(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    dialogs.open_path = other if valid else str(bad)
    dialogs.question_answer = QMessageBox.StandardButton.Discard

    def click(tree):
        before = len(dialogs.shown)
        tree.button("Load").click()
        click.kinds = [kind for kind, _ in dialogs.shown[before:]]

    log, a, b, info = _busy_scenario(qtbot, bt, "nested_loop", click)
    fast_config(bt)
    save(bt)
    before = describe(bt)
    old_ids = node_ids(bt)
    finished = record(bt.executionFinished)
    exceptions = _run_busy_scenario(qtbot, bt, "nested_loop", info)
    assert exceptions == []
    assert info["inside"]["ids"] == old_ids
    if valid:
        assert click.kinds == ["open_dialog"]
        assert node_ids(bt) == {"r2", "n2"}
        assert bt.blackboardStore().items() == [("y", "Integer", 3)]
        assert finished == []
    else:
        assert click.kinds == ["open_dialog", "critical"], "the error is reported at once"
        assert describe(bt) == before
        assert finished == [SUCCEEDED]
    assert bt.GetExecutionState() == "Idle"


@pytest.mark.parametrize("operation", ["LoadTree", "NewTree"])
def test_load_or_new_from_running_state_slot(qtbot, bt, tmp_path, operation):
    other = next_tree_file(tmp_path)
    fresh = str(tmp_path / "fresh.json")
    log, (a,) = probes(bt, (R3Probe, "a"))
    a.steps = 100_000
    fast_config(bt)
    save(bt)
    old_ids = node_ids(bt)
    info = {}

    def on_state(state):
        if state == "Running" and not info:
            info["result"] = bt.LoadTree(other) if operation == "LoadTree" else bt.NewTree(fresh)
            info["inside"] = _snapshot_now(bt)

    bt.executionStateChanged.connect(on_state)
    with capture_exceptions() as exceptions:
        bt.Execute()
    assert exceptions == []
    assert info["result"] is True
    assert info["inside"]["ids"] == old_ids
    assert bt.GetExecutionState() == "Idle"
    assert not timer_active(bt)
    assert not bt.view().is_locked()
    if operation == "LoadTree":
        assert node_ids(bt) == {"r2", "n2"}
    else:
        assert len(bt.GetNodes()) == 1
    assert a not in bt.GetNodes()
    assert buttons(bt) == {"Execute": True, "Pause": False, "Stop": False, "Reset": True}


@pytest.mark.parametrize("interactive", [False, True], ids=["programmatic", "load-button"])
def test_failed_deferred_install_restores_previous_tree(qtbot, bt, dialogs, tmp_path, monkeypatch, caplog, interactive):
    other = next_tree_file(tmp_path)
    bt.AddEntry("x", "Integer", 0)
    bt.AddEntry("items", "List", [1, 2])
    dialogs.open_path = other
    dialogs.question_answer = QMessageBox.StandardButton.Discard

    def action(tree):
        tree.SetEntry(1, "x")
        if interactive:
            tree.button("Load").click()
            return True
        return tree.LoadTree(other)

    log, a, b, info = _busy_scenario(qtbot, bt, "on_run", action)
    fast_config(bt)
    save(bt)
    before = describe(bt)
    view = bt.view()
    original = view.connect_nodes
    failures = []

    def fail_new_tree(parent, child):
        if parent.GetId() == "r2" and not failures:
            failures.append(child.GetId())
            raise RuntimeError("simulated failure while installing")
        return original(parent, child)

    monkeypatch.setattr(view, "connect_nodes", fail_new_tree)
    loaded = record(bt.treeLoaded)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        exceptions = _run_busy_scenario(qtbot, bt, "on_run", info)
    assert exceptions == []
    assert info.get("error") is None and info["result"] is True
    assert failures == ["n2"], "precondition: the deferred install failed"
    after = describe(bt)
    assert after["nodes"] == before["nodes"], "the previous tree was not restored"
    assert after["file"] == before["file"]
    assert after["title"] == before["title"]
    assert after["config"] == before["config"]
    assert after["rows"] == ["x", "items"]
    assert bt.GetEntryNames() == ["x", "items"]
    assert bt.GetEntry("x") == 1 and bt.GetEntry("items") == [1, 2]
    assert not bt.HasEntry("y")
    assert loaded == []
    assert bt.GetExecutionState() == "Idle"
    assert not bt.view().is_locked()
    assert errors_logged(caplog), "the failed install must be logged"
    assert not bt.IsModified()
    if interactive:
        assert any("could not be loaded" in text for text in critical_texts(dialogs))
    # the restored tree is usable
    monkeypatch.setattr(view, "connect_nodes", original)
    finished = record(bt.executionFinished)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert finished == [SUCCEEDED]


class _Handle:
    """A run-time object (e.g. a device connection) stored in an Object entry; it can be deep-copied."""

    def __init__(self, label):
        self.label = label
        self.state = [label]


@pytest.mark.parametrize("operation", ["LoadTree", "NewTree"])
def test_failed_install_keeps_object_entries_identical(qtbot, bt, tmp_path, monkeypatch, operation):
    """A failed install restores the previous tree; the run-time objects of Object entries (kept by New /
    Load) must still be the application's objects afterwards, not copies of them."""
    other = next_tree_file(tmp_path)
    handle = _Handle("robot")
    bt.SetEntry(handle, "robot")
    bt.SetEntry(3, "n")
    save(bt)
    before = describe(bt)
    view = bt.view()
    if operation == "LoadTree":
        original = view.connect_nodes

        def fail_once(parent, child):
            if parent.GetId() == "r2":
                raise RuntimeError("simulated install failure")
            return original(parent, child)

        monkeypatch.setattr(view, "connect_nodes", fail_once)
        call = lambda: bt.LoadTree(other)  # noqa: E731
    else:
        original_add = view.add_node
        failed = []

        def fail_first(node, pos=QPointF(0, 0)):
            if not failed:
                failed.append(node)
                raise RuntimeError("simulated install failure")
            return original_add(node, pos)

        monkeypatch.setattr(view, "add_node", fail_first)
        call = lambda: bt.NewTree(str(tmp_path / "fresh.json"))  # noqa: E731
    with pytest.raises(TreeFileError):
        call()
    after = describe(bt)
    assert after["nodes"] == before["nodes"] and after["file"] == before["file"]
    assert bt.GetEntryNames() == ["robot", "n"]
    assert bt.GetEntry("n") == 3
    assert bt.GetEntry("robot") is handle, (
        "after a failed load the Object entry holds a deep copy of the application's object "
        f"({bt.GetEntry('robot')!r} is not {handle!r})"
    )


@pytest.mark.parametrize("operation", ["LoadTree", "NewTree"])
def test_install_unlocks_the_view_first(qtbot, bt, tmp_path, operation):
    other = next_tree_file(tmp_path)
    bt.view().set_locked(True)  # e.g. left locked by an interrupted execution
    assert bt.GetExecutionState() == "Idle"
    if operation == "LoadTree":
        assert bt.LoadTree(other)
        assert node_ids(bt) == {"r2", "n2"}
    else:
        assert bt.NewTree(str(tmp_path / "fresh.json"))
        assert len(bt.GetNodes()) == 1
    assert not bt.view().is_locked()
    bt.AddNode("Succeed", 0, 400)


@pytest.mark.parametrize("interactive", [True, False], ids=["new-button", "programmatic"])
def test_new_tree_install_failure(qtbot, bt, dialogs, tmp_path, monkeypatch, interactive):
    leaf = add_leaf(bt, "Succeed")
    bt.SetEntry(3, "n")
    save(bt)
    before = describe(bt)
    view = bt.view()
    original = view.add_node
    failures = []

    def fail_first(node, pos=QPointF(0, 0)):
        if not failures:
            failures.append(node)
            raise RuntimeError("simulated add_node failure")
        return original(node, pos)

    monkeypatch.setattr(view, "add_node", fail_first)
    fresh = str(tmp_path / "fresh.json")
    if interactive:
        dialogs.save_path = fresh
        with capture_exceptions() as exceptions:
            bt.button("New").click()
        assert exceptions == []
        assert dialogs.kinds()[-1] == "critical", dialogs.kinds()
    else:
        with pytest.raises(TreeFileError):
            bt.NewTree(fresh)
    assert failures, "precondition: the install failed"
    after = describe(bt)
    assert after["nodes"] == before["nodes"]
    assert after["file"] == before["file"]
    assert after["blackboard"] == before["blackboard"]
    assert after["title"] == before["title"]
    assert bt.GetFilePath() != os.path.abspath(fresh)
    assert {node["type"] for node in after["nodes"].values()} == {"Root", "Succeed"}
    assert leaf is not None


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


@pytest.mark.parametrize("thread_kind", ["plain-thread", "worker-on-run"])
def test_structure_and_file_api_raise_from_non_gui_threads(qtbot, bt, dialogs, tmp_path, thread_kind):
    other = next_tree_file(tmp_path)
    dialogs.open_path = other
    dialogs.save_path = str(tmp_path / "dialog_path.json")
    root = bt.GetRootNode()
    child = add_leaf(bt, "Succeed", x=-300.0)
    loose = bt.AddNode("Succeed", 300, 400)
    save(bt)
    calls = _api_calls(bt, tmp_path, other, loose, child)
    results: dict = {}

    def run_all():
        for name, call in calls.items():
            try:
                call()
                results[name] = "ok"
            except BaseException as error:  # noqa: BLE001 - reported below
                results[name] = error

    if thread_kind == "plain-thread":
        before = describe(bt)
        thread = threading.Thread(target=run_all)
        thread.start()
        thread.join(10)
        assert not thread.is_alive()
    else:
        worker = add_leaf(bt, R3Worker, x=300.0)
        worker.body = lambda tree, node: run_all()
        fast_config(bt)
        save(bt)
        before = describe(bt)
        bt.Execute()
        run_until_idle(qtbot, bt)
    qtbot.wait(30)
    for name in calls:
        assert isinstance(results[name], RuntimeError), f"{name} from a non-GUI thread: {results[name]!r}"
    assert describe(bt) == before
    assert not os.path.exists(tmp_path / "from_thread_new.json")
    assert not os.path.exists(tmp_path / "from_thread_save.json")
    assert not os.path.exists(tmp_path / "dialog_path.json")
    assert dialogs.shown == [], "no dialog may be opened from a worker thread"
    assert child.GetParent() is root and loose.GetParent() is None


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
    out = tmp_path / "nan.json"
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


# ============================================================================ destroyed cleanup
@pytest.mark.parametrize("running", [True, False], ids=["executing", "idle"])
@pytest.mark.parametrize("how", ["delete", "deleteLater"])
def test_destroyed_cleanup_is_silent_and_shuts_the_executor_down(qtbot, tmp_path, qt_messages, caplog, running, how):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    widget = BehaviorTreeWidget(node_types=R3_TYPES)
    try:
        widget.resize(800, 600)
        widget.show()
        assert widget.NewTree(str(tmp_path / "t.json"))
        widget.AddEntry("x", "Integer", 1)
        widget.SetEntry("text", "s")
        widget.SetEntry([1, 2], "items")
        widget.SetEntry(object(), "obj")
        log, (a,) = probes(widget, (R3Probe, "a"))
        a.steps = 100_000
        fast_config(widget)
        widget._set_modified(False)
        if running:
            widget.Execute()
            assert widget.IsExecuting()
        token = a._run_token
        store, executor = widget.blackboardStore(), widget.executor()
        namespace = widget.GetBlackboardNamespace()
        removed, changed = [], []
        store.entryRemoved.connect(lambda name: removed.append(name))
        store.changed.connect(lambda runtime: changed.append(runtime))
    except BaseException:
        widget.Shutdown()
        widget.deleteLater()
        raise
    with capture_exceptions() as exceptions:
        if how == "delete":
            shiboken6.delete(widget)
        else:
            widget.deleteLater()
            flush_deferred_deletes()
        qtbot.waitUntil(lambda: not shiboken6.isValid(widget), timeout=2000)
        qtbot.wait(30)
    assert exceptions == []
    assert executor._shut_down is True, "the destroyed cleanup must mark the executor shut down"
    assert store.is_disposed()
    assert removed == [] and changed == [], "the destroyed cleanup must not emit blackboard signals"
    assert keys_of(namespace) == []
    if running:
        assert token is not None and token.is_cancelled()
    assert errors_logged(caplog) == []
    serious = [m for mode, m, _ in qt_messages if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg)]
    assert serious == []
    assert timer_thread_warnings(qt_messages) == []


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
        QTest.mousePress(viewport, LEFT, NO_MOD, start)
        for step in range(1, 5):
            QTest.mouseMove(viewport, start + QPoint(10 * step, 25 * step))
        QTest.mouseRelease(viewport, LEFT, NO_MOD, start + QPoint(40, 100))
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


# ============================================================================ Object entries / names / order
def test_object_entries_are_never_saved_and_cause_no_warning(qtbot, bt, dialogs, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    bt.AddEntry("n", "Integer", 1)
    marker = object()
    bt.SetEntry(marker, "obj")
    bt.SetEntry(2**40, "big")  # too large for an Integer entry: stored as Object
    bt.AddEntry("explicit", "Object", [1, 2])  # encodable, but still a run-time Object entry
    assert [bt.GetEntryType(name) for name in ("obj", "big", "explicit")] == ["Object"] * 3
    path = tmp_path / "saved.json"
    dialogs.save_path = str(path)
    caplog.clear()
    bt.button("Save").click()
    assert dialogs.kinds() == ["save_dialog"], "saving Object entries must not show a warning"
    assert read_json(path)["blackboard"] == [{"name": "n", "type": "Integer", "value": 1}]
    assert warnings_logged(caplog) == []
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and r.name.startswith(LOGGER)]
    assert any("'obj'" in message for message in debug), "skipping an Object entry is logged at debug level"
    assert bt.SaveTree(str(tmp_path / "programmatic.json"))
    assert read_json(tmp_path / "programmatic.json")["blackboard"] == [{"name": "n", "type": "Integer", "value": 1}]
    assert warnings_logged(caplog) == []
    assert not bt.IsModified()
    # kept by New / Load, with their run-time values
    assert bt.NewTree(str(tmp_path / "fresh.json"))
    assert bt.GetEntryNames() == ["obj", "big", "explicit"]
    assert bt.GetEntry("obj") is marker
    assert bt.LoadTree(str(path))
    assert bt.GetEntryNames() == ["n", "obj", "big", "explicit"]
    assert bt.GetEntry("obj") is marker


def test_object_entries_in_files_are_skipped_with_a_problem(qtbot, make_widget, dialogs, tmp_path, caplog):
    path = write_tree(
        tmp_path / "objects.json",
        [node_entry("r", "Root")],
        blackboard=[
            {"name": "a", "type": "Integer", "value": 1},
            {"name": "o", "type": "Object", "value": 5},
            {"name": "b", "type": "String", "value": "s"},
        ],
    )
    widget = make_widget()
    marker = object()
    widget.SetEntry(marker, "o")  # a run-time Object entry of the same name is kept, not replaced
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert widget.LoadTree(path)
    assert widget.GetEntryNames() == ["a", "b", "o"]
    assert widget.GetEntry("o") is marker
    assert any("'o'" in message for message in warnings_logged(caplog))

    other = make_widget()
    dialogs.open_path = path
    other.button("Load").click()
    texts = warning_texts(dialogs)
    assert len(texts) == 1 and "'o'" in texts[0] and "Object" in texts[0]
    assert other.GetEntryNames() == ["a", "b"]


def test_invalid_entry_names_in_files_are_skipped(qtbot, make_widget, dialogs, tmp_path, caplog):
    invalid = ["", "   ", " lead", "trail ", "a.b", "a/b", 5, None]
    items = [{"name": name, "type": "Integer", "value": index} for index, name in enumerate(invalid)]
    items += [{"name": "good", "type": "Integer", "value": 1}, {"name": "also good", "type": "String", "value": "x"}]
    items.append({"type": "Integer", "value": 3})  # no name at all
    path = write_tree(tmp_path / "names.json", [node_entry("r", "Root")], blackboard=items)
    entries, problems = BlackboardStore.parse_list(items)
    assert [name for name, _, _ in entries] == ["good", "also good"]
    assert len(problems) == len(invalid) + 1
    widget = make_widget()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert widget.LoadTree(path), "invalid entry names must never fail a load"
    assert widget.GetEntryNames() == ["good", "also good"]
    assert len([m for m in warnings_logged(caplog) if "blackboard entry" in m]) == len(invalid) + 1
    other = make_widget()
    dialogs.open_path = path
    other.button("Load").click()
    texts = warning_texts(dialogs)
    assert len(texts) == 1 and texts[0].count("- blackboard entry") == len(invalid) + 1
    assert other.GetEntryNames() == ["good", "also good"]


@pytest.mark.parametrize(
    "name,valid",
    [("x", True), ("with space", True), ("ünï", True), ("", False), ("  ", False), (" x", False),
     ("x ", False), ("a.b", False), ("a/b", False), (5, False), (None, False)],
)
def test_check_name_format(name, valid):
    if valid:
        assert BlackboardStore.check_name_format(name) == name
    else:
        with pytest.raises((ValueError, TypeError)):
            BlackboardStore.check_name_format(name)


def test_load_order_follows_the_file(qtbot, bt, tmp_path):
    marker = object()
    bt.AddEntry("c", "Integer", 1)
    bt.SetEntry(marker, "obj")
    bt.AddEntry("a", "String", "old")
    bt.AddEntry("zz", "Integer", 9)
    path = write_tree(
        tmp_path / "order.json",
        [node_entry("r", "Root")],
        blackboard=[
            {"name": "b", "type": "Integer", "value": 2},
            {"name": "a", "type": "Integer", "value": 1},
            {"name": "c", "type": "String", "value": "three"},
        ],
    )
    assert bt.LoadTree(path)
    assert bt.GetEntryNames() == ["b", "a", "c", "obj"]
    assert bt.blackboardStore().items()[:3] == [("b", "Integer", 2), ("a", "Integer", 1), ("c", "String", "three")]
    qtbot.wait(20)
    assert rows_in_layout(bt.blackboardView()) == ["b", "a", "c", "obj"]
    assert [row.name for row in bt.blackboardView().rows()] == ["b", "a", "c", "obj"]


def test_store_reorder_and_dispose_notify(qapp):
    store = BlackboardStore()
    reordered = record(store.entriesReordered)
    for name in ("a", "b", "c"):
        store.add(name, "Integer", 1)
    store.reorder(["c", "missing", "a"])
    assert store.names() == ["c", "a", "b"]
    assert len(reordered) == 1
    store.reorder(["c", "a"])
    assert len(reordered) == 1, "no signal when the order does not change"
    removed = record(store.entryRemoved)
    store.dispose(notify=False)
    assert removed == [] and store.is_disposed()
    with pytest.raises(RuntimeError):
        store.add("d", "Integer", 1)
    with pytest.raises(RuntimeError):
        store.set("a", 2)

    other = BlackboardStore()
    for name in ("x", "y"):
        other.add(name, "String", name)
    removed = record(other.entryRemoved)
    other.dispose(notify=True)
    assert removed == ["x", "y"]
    other.dispose(notify=True)
    assert removed == ["x", "y"], "dispose is idempotent"
    store.deleteLater()
    other.deleteLater()


# ============================================================================ read_tree_file
@pytest.mark.parametrize("content", ["deep-nesting", "5000-digit-int"])
def test_unparseable_json_raises_tree_file_error(qtbot, bt, dialogs, tmp_path, content):
    if content == "deep-nesting":
        depth = 200_000
        text = '{"format": "behavior_tree_widget", "version": 1, "nodes": ' + "[" * depth + "]" * depth + "}"
    else:
        text = (
            '{"format": "behavior_tree_widget", "version": 1, "nodes": [], "blackboard": '
            '[{"name": "big", "type": "Integer", "value": ' + "9" * 5000 + "}]}"
        )
    path = tmp_path / "evil.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(TreeFileError):
        read_tree_file(str(path))
    bt.AddEntry("n", "Integer", 1)
    add_leaf(bt, "Succeed")
    save(bt)
    before = describe(bt)
    with pytest.raises(TreeFileError):
        bt.LoadTree(str(path))
    assert describe(bt) == before
    dialogs.open_path = str(path)
    with capture_exceptions() as exceptions:
        bt.button("Load").click()
    assert exceptions == []
    assert dialogs.kinds()[-1] == "critical"
    assert "evil.json" in critical_texts(dialogs)[-1]
    assert describe(bt) == before


# ============================================================================ _title / _fields assignment
class _Titled:
    def __str__(self):
        return "from __str__"


def test_title_assignment_is_coerced(qtbot, bt, tmp_path):
    root = bt.GetRootNode()
    leaf = bt.AddNode(R3Probe, 0, 220)
    untitled = bt.AddNode(R3Untitled, 300, 220)
    selector = bt.AddNode("Selector", -300, 220)
    save(bt)
    flags = record(leaf.changed)
    titles = record(leaf.titleChanged)
    for value, expected in [(42, "42"), (2.5, "2.5"), (_Titled(), "from __str__"), (None, "R3 Probe"), ("Named", "Named")]:
        leaf._title = value
        assert leaf.GetTitle() == expected and type(leaf.GetTitle()) is str
        assert leaf._title == expected
        assert leaf._title_label.text() == expected
    assert titles == ["42", "2.5", "from __str__", "R3 Probe", "Named"]
    assert flags == [False] * 5, "assignments on the GUI thread while idle are edits"
    assert bt.IsModified()
    untitled._title = None
    assert untitled.GetTitle() == "R3Untitled"
    selector._title = None
    assert selector.GetTitle() == "Selector" and selector._title_label.text() == "Selector"
    root._title = 3
    assert root.GetTitle() == "3"
    root._title = None
    assert root.GetTitle() == "Root"
    leaf._title = 1234
    out = str(tmp_path / "titles.json")
    assert bt.SaveTree(out)
    assert saved_node(read_json(out), leaf.GetId())["title"] == "1234"


@pytest.mark.parametrize("interrupt", ["Stop", "Reset"])
def test_cancelled_worker_cannot_assign_title_or_fields(qtbot, bt, gate, interrupt):
    leaf = add_leaf(bt, R3LateAssigner)
    state = {"entered": threading.Event(), "gate": gate, "done": threading.Event()}
    leaf.state = state
    fast_config(bt)
    save(bt)
    flags = record(leaf.changed)
    bt.Execute()
    assert state["entered"].wait(5)
    getattr(bt, interrupt)()
    gate.set()
    assert state["done"].wait(5)
    qtbot.wait(30)
    outcome = state["outcome"]
    for name in ("_title", "_title=None", "_fields"):
        assert isinstance(outcome[name], ExecutionCancelled), f"{name}: {outcome[name]!r}"
    assert outcome["CancelRequested"] is True
    assert leaf.GetTitle() == "R3 Late Assigner"
    assert leaf._title_label.text() == "R3 Late Assigner"
    assert leaf.GetFields() == {"n": 1}
    assert leaf.field_editor("n").value() == 1
    assert flags == []
    assert not bt.IsModified()
    bt.Stop()


def test_worker_title_assignment_is_coerced_and_applied_on_the_gui_thread(qtbot, bt, monkeypatch):
    threads = []
    original = NodeWidget._on_title_changed

    def spy(self, *args, **kwargs):
        threads.append(threading.get_ident())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(NodeWidget, "_on_title_changed", spy)
    leaf = add_leaf(bt, R3LiveAssigner)
    fast_config(bt)
    save(bt)
    flags = record(leaf.changed)
    bt.Execute()
    run_until_idle(qtbot, bt)
    qtbot.waitUntil(lambda: leaf._title_label.text() == "7", timeout=2000)
    qtbot.wait(20)
    assert leaf.GetTitle() == "7" and type(leaf.GetTitle()) is str
    assert leaf.GetFields() == {"n": 2, "label": "set by worker"}
    assert leaf.field_editor("label") is not None and leaf.field_editor("label").text() == "set by worker"
    assert threads and set(threads) == {GUI_THREAD}
    assert flags and all(flag is True for flag in flags), flags
    assert not bt.IsModified()


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
        QTest.mousePress(viewport, LEFT, NO_MOD, start)
        assert view.scene().mouseGrabberItem() is None, "the stale implicit grab must be released"
        assert view._gesture is _Gesture.PANNING
        for step in range(1, 5):
            QTest.mouseMove(viewport, start + QPoint(10 * step, 5 * step))
        QTest.mouseRelease(viewport, LEFT, NO_MOD, start + QPoint(40, 20))
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


# ============================================================================ files
def test_unknown_leaf_keeps_undecodable_fields_and_unknown_keys(qtbot, make_widget, tmp_path):
    raw_fields = {
        "bad_dict": {"__dict__": [[1]]},
        "bad_set": {"__set__": [[1, 2], "x"]},
        "bad_nested": {"__tuple__": [{"__dict__": ["ab"]}]},
    }
    good_fields = {"count": 3, "mode": ["a", "b"], "pair": {"__tuple__": [1, "a"]}, "text": "hi"}
    unknown_keys = {
        "custom": {"z": [1, {"k": None}], "a": 1.5, "nested": {"__set__": "not a list"}},
        "priority": 7,
        "flag": None,
        "memory": True,
        "notes": "ünïcode ✓",
    }
    node = node_entry("u", "Mystery", 10.0, 220.0, title="M", fields={**good_fields, **raw_fields},
                      field_selections={"mode": 1}, **unknown_keys)
    path = write_tree(tmp_path / "u.json", [node_entry("r", "Root"), node], [{"parent": "r", "child": "u"}])
    widget = make_widget()
    assert widget.LoadTree(path)
    unknown = node_by_id(widget, "u")
    assert isinstance(unknown, UnknownLeafNodeWidget)
    out = tmp_path / "out.json"
    assert widget.SaveTree(str(out))
    saved = saved_node(read_json(out), "u")
    for key, value in raw_fields.items():
        assert json.dumps(saved["fields"][key], ensure_ascii=False) == json.dumps(value, ensure_ascii=False), key
    for key, value in unknown_keys.items():
        assert json.dumps(saved[key], ensure_ascii=False) == json.dumps(value, ensure_ascii=False), key
    for key, value in good_fields.items():
        assert saved["fields"][key] == value, key
    assert saved["field_selections"] == {"mode": 1}
    assert (saved["type"], saved["title"]) == ("Mystery", "M")
    # a second round trip is stable
    again = make_widget()
    assert again.LoadTree(str(out))
    out2 = tmp_path / "out2.json"
    assert again.SaveTree(str(out2))
    assert saved_node(read_json(out2), "u") == saved


def _attributes(path) -> int:
    import ctypes

    function = ctypes.windll.kernel32.GetFileAttributesW
    function.restype = ctypes.c_uint32
    return function(str(path))


def _set_attributes(path, attributes: int) -> None:
    import ctypes

    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes)


HIDDEN, SYSTEM = 0x2, 0x4


@pytest.mark.skipif(not WINDOWS, reason="Windows file attributes")
@pytest.mark.parametrize("attributes", [HIDDEN, HIDDEN | SYSTEM], ids=["hidden", "hidden-system"])
def test_write_json_atomic_keeps_hidden_and_system_attributes(tmp_path, attributes):
    target = tmp_path / "hidden.json"
    target.write_text("{}", encoding="utf-8")
    _set_attributes(target, attributes)
    assert _attributes(target) & attributes == attributes
    try:
        write_json_atomic(str(target), {"a": 1})
        assert read_json(target) == {"a": 1}
        assert _attributes(target) & attributes == attributes, hex(_attributes(target))
        assert os.listdir(tmp_path) == ["hidden.json"]
    finally:
        _set_attributes(target, 0x80)  # FILE_ATTRIBUTE_NORMAL


@pytest.fixture
def read_only_file(tmp_path):
    target = tmp_path / "ro.json"
    target.write_text('{"old": 1}\n', encoding="utf-8")
    os.chmod(target, stat.S_IREAD)
    yield target
    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


def test_write_json_atomic_read_only_target_names_it(tmp_path, read_only_file, monkeypatch):
    monkeypatch.setattr(serialization_module, "time", SimpleNamespace(sleep=lambda seconds: None))
    with pytest.raises(PermissionError) as info:
        write_json_atomic(str(read_only_file), {"new": 2})
    message = str(info.value)
    assert os.path.realpath(str(read_only_file)) in message or str(read_only_file) in message, message
    assert read_only_file.read_text(encoding="utf-8") == '{"old": 1}\n'
    assert os.listdir(tmp_path) == ["ro.json"], "no temp file may be left behind"


@pytest.mark.parametrize("interactive", [False, True], ids=["programmatic", "save-button"])
def test_save_tree_to_read_only_file(qtbot, bt, dialogs, tmp_path, read_only_file, monkeypatch, interactive):
    monkeypatch.setattr(serialization_module, "time", SimpleNamespace(sleep=lambda seconds: None))
    add_leaf(bt, "Succeed")
    if interactive:
        dialogs.save_path = str(read_only_file)
        with capture_exceptions() as exceptions:
            bt.button("Save").click()
        assert exceptions == []
        texts = critical_texts(dialogs)
        assert len(texts) == 1 and "ro.json" in texts[0]
    else:
        with pytest.raises(PermissionError) as info:
            bt.SaveTree(str(read_only_file))
        assert "ro.json" in str(info.value)
    assert read_only_file.read_text(encoding="utf-8") == '{"old": 1}\n'
    assert sorted(os.listdir(tmp_path)) == ["ro.json", "tree.json"]
    assert bt.GetFilePath() == str(tmp_path / "tree.json")


@pytest.mark.skipif(not WINDOWS, reason="an open file blocks os.replace only on Windows")
def test_write_json_atomic_never_overwrites_a_file_held_open(tmp_path, monkeypatch):
    """A file another program keeps open is left intact (no risky in-place overwrite)."""
    monkeypatch.setattr(serialization_module, "time", SimpleNamespace(sleep=lambda seconds: None))
    target = tmp_path / "held.json"
    original = '{"old": true, "padding": "' + "x" * 200 + '"}\n'
    target.write_text(original, encoding="utf-8")
    handle = open(target, encoding="utf-8")  # noqa: SIM115 - another program keeps the file open
    try:
        if os.name == "nt":
            with pytest.raises(PermissionError) as info:
                write_json_atomic(str(target), {"new": 1})
            assert "held.json" in str(info.value)
        else:
            write_json_atomic(str(target), {"new": 1})  # POSIX allows replacing an open file
    finally:
        handle.close()
    if os.name == "nt":
        assert target.read_text(encoding="utf-8") == original
    assert os.listdir(tmp_path) == ["held.json"]


class _OsProxy:
    """``os`` for the serialization module with a replaced ``replace``."""

    def __init__(self, replace):
        self.replace = replace

    def __getattr__(self, name):
        return getattr(os, name)


@pytest.mark.parametrize("scenario", ["transient", "always-existing", "always-new"])
def test_write_json_atomic_retries_replace(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(serialization_module, "time", SimpleNamespace(sleep=lambda seconds: None))
    attempts = []
    real_replace = os.replace

    def flaky(source, destination):
        attempts.append(destination)
        if scenario == "transient" and len(attempts) >= 3:
            return real_replace(source, destination)
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(serialization_module, "os", _OsProxy(flaky))
    target = tmp_path / "target.json"
    if scenario != "always-new":
        target.write_text("{}", encoding="utf-8")
    if scenario in ("always-new", "always-existing"):
        with pytest.raises(PermissionError) as info:
            write_json_atomic(str(target), {"a": 1})
        assert "target.json" in str(info.value)
        assert len(attempts) == 5
        assert os.listdir(tmp_path) == ([] if scenario == "always-new" else ["target.json"])
        if scenario == "always-existing":
            assert read_json(target) == {}  # left untouched
    else:
        write_json_atomic(str(target), {"a": 1})
        assert read_json(target) == {"a": 1}
        assert len(attempts) == (3 if scenario == "transient" else 5)
        assert os.listdir(tmp_path) == ["target.json"]
