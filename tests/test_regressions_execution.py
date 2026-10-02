"""Regression tests for the execution / threading fix pass (CHANGES_ROUND2.md).

Covers:

* per-run cancellation tokens, one ``OnRun`` call per node at a time (Reset,
  Stop + Execute and Selector preemption with a slow worker-thread leaf),
* ``ExecutionCancelled`` for writes from cancelled worker calls (also after
  ``LoadTree`` / ``NewTree`` while a worker runs),
* ``THREAD_WAIT`` and ``OnRun(self)`` without the tree argument,
* Stop / Reset / Execute / Pause issued during a tick (from a GUI-thread
  ``OnRun`` or from a slot running a nested event loop, i.e. a modal dialog),
* run-once completion, tick failures, status refresh of removed nodes,
* the structure lock of the public editing API while running and paused,
* run-time vs. edit changes and the modified flag,
* ``restore_blackboard`` snapshots, ``Shutdown()``, ``closeEvent`` and destroying
  a widget while a worker-thread leaf runs.

Modal dialogs are emulated with a nested ``QEventLoop`` (never ``exec()`` of a
real dialog). Worker threads are synchronised with events instead of sleeps.
"""

from __future__ import annotations

import logging
import threading

import py_trees
import pytest
import shiboken6
from py_trees.common import Status
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QLabel

from behavior_tree_widget import BehaviorTreeWidget, ExecutionCancelled, LeafNodeWidget, NodeStatus
from behavior_tree_widget.execution import LeafBehaviour
from conftest import Succeed, fast_config, run_until_idle

READY, RUNNING, SUCCEEDED, FAILED = "Ready", "Running", "Succeeded", "Failed"
LOGGER = "behavior_tree_widget"


# ============================================================================ synchronisation state
class GateState:
    """Synchronisation shared between a test and its worker-thread leaves."""

    def __init__(self):
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.entered: list[int] = []
        self.exited: list[int] = []
        self.observed: dict[int, bool] = {}  # call index -> CancelRequested() after the gate opened
        self.events: list[str] = []  # ordered: "start" (OnStart, GUI thread), "enterN" / "exitN" (OnRun)
        self.terminated: list[str] = []  # OnTerminate statuses
        self.outcomes: dict[int, dict] = {}  # call index -> {action: "ok" | exception}
        self.done = threading.Event()

    def enter(self) -> int:
        with self.lock:
            index = len(self.entered)
            self.entered.append(index)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.events.append(f"enter{index}")
            return index

    def leave(self, index: int) -> None:
        with self.lock:
            self.active -= 1
            self.exited.append(index)
            self.events.append(f"exit{index}")


@pytest.fixture
def gate():
    state = GateState()
    yield state
    state.gate.set()  # never leave a worker thread blocked


# ============================================================================ test node types
class RxGated(LeafNodeWidget):
    """Worker-thread leaf: blocks until ``state.gate`` opens, records CancelRequested() and returns
    True unless cancelled. Tracks concurrency of its OnRun calls."""

    _title = "Rx Gated"
    THREAD_WAIT = 0.0

    def OnStart(self, tree):
        with self.state.lock:
            self.state.events.append("start")

    def OnRun(self, tree):
        state = self.state  # bound for the whole call
        index = state.enter()
        try:
            state.gate.wait(10)
            state.observed[index] = self.CancelRequested()
            return not state.observed[index]
        finally:
            state.leave(index)

    def OnTerminate(self, tree, status):
        self.state.terminated.append(status.value)


class RxLateWriter(LeafNodeWidget):
    """Worker-thread leaf: blocks until ``state.gate`` opens, then tries every kind of tree change
    (and some reads), recording the outcome of each per OnRun call."""

    _title = "Rx Late Writer"
    _fields = {"count": 1, "choice": ["a", "b", "c"]}
    THREAD_WAIT = 0.0

    def OnRun(self, tree):
        state = self.state
        index = state.enter()
        try:
            state.gate.wait(10)
            store = tree.blackboardStore()
            attempts = {
                "SetEntry": lambda: tree.SetEntry(42, "result"),
                "AddEntry": lambda: tree.AddEntry(f"added{index}", "Integer", 1),
                "store.add": lambda: store.add(f"stored{index}", "String", "s"),
                "store.set": lambda: store.set("result", 43),
                "store.replace": lambda: store.replace("result", "String", "text"),
                "store.rename": lambda: store.rename(f"added{index}", f"renamed{index}"),
                "RemoveEntry": lambda: tree.RemoveEntry(f"renamed{index}"),
                "store.remove": lambda: store.remove(f"stored{index}"),
                "SetField": lambda: self.SetField("count", 99),
                "SetFieldSelectionIndex": lambda: self.SetFieldSelectionIndex("choice", 2),
                "SetTitle": lambda: self.SetTitle(f"changed by call {index}"),
                # reads are always allowed
                "GetEntry": lambda: tree.GetEntry("result"),
                "HasEntry": lambda: tree.HasEntry("result"),
                "GetEntryNames": lambda: tree.GetEntryNames(),
                "GetField": lambda: self.GetField("count"),
                "GetFieldSelection": lambda: self.GetFieldSelection("choice"),
                "GetTitle": lambda: self.GetTitle(),
            }
            outcome: dict = {}
            for name, attempt in attempts.items():
                try:
                    attempt()
                    outcome[name] = "ok"
                except BaseException as error:  # noqa: BLE001 - reported to the test
                    outcome[name] = error
            outcome["CancelRequested"] = self.CancelRequested()
            state.outcomes[index] = outcome
            return True
        finally:
            state.leave(index)
            state.done.set()


WRITE_ACTIONS = [
    "SetEntry", "AddEntry", "store.add", "store.set", "store.replace", "store.rename", "RemoveEntry",
    "store.remove", "SetField", "SetFieldSelectionIndex", "SetTitle",
]
READ_ACTIONS = ["GetEntry", "HasEntry", "GetEntryNames", "GetField", "GetFieldSelection", "GetTitle"]


class RxHook(LeafNodeWidget):
    """GUI-thread leaf: OnRun calls ``self.action(tree, self)`` (if set) and returns its result
    (None -> True). Lifecycle calls are appended to ``self.log`` as ``(hook, tag, ...)``."""

    _title = "Rx Hook"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.log: list[tuple] = []
        self.tag = "hook"
        self.action = None
        self.runs = 0
        self.starts = 0

    def OnStart(self, tree):
        self.starts += 1
        self.log.append(("start", self.tag))

    def OnRun(self, tree):
        self.runs += 1
        self.log.append(("run", self.tag))
        result = self.action(tree, self) if self.action is not None else True
        return True if result is None else result

    def OnTerminate(self, tree, status):
        self.log.append(("terminate", self.tag, status.value))


class RxThreadedHook(RxHook):
    _title = "Rx Threaded Hook"
    RUN_IN_THREAD = True
    THREAD_WAIT = 5.0  # collected within the tick that starts it


class RxSteps(RxHook):
    """GUI-thread leaf returning RUNNING ``self.steps`` times per run, then SUCCESS."""

    _title = "Rx Steps"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.steps = 3
        self._left = 0

    def OnStart(self, tree):
        super().OnStart(tree)
        self._left = self.steps

    def OnRun(self, tree):
        self.runs += 1
        self.log.append(("run", self.tag))
        if self.action is not None:
            self.action(tree, self)
        if self._left > 0:
            self._left -= 1
            return Status.RUNNING
        return Status.SUCCESS


class RxSwitch(LeafNodeWidget):
    """GUI-thread condition: returns the next queued result, else ``default``."""

    _title = "Rx Switch"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.results: list[bool] = []
        self.default = False
        self.runs = 0

    def OnRun(self, tree):
        self.runs += 1
        return self.results.pop(0) if self.results else self.default


class RxCountingRaise(LeafNodeWidget):
    _title = "Rx Counting Raise"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = 0

    def OnRun(self, tree):
        self.runs += 1
        raise ValueError("boom")


class RxCounting(LeafNodeWidget):
    """GUI-thread leaf returning ``self.result`` and counting its OnRun calls."""

    _title = "Rx Counting"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = 0
        self.result = True

    def OnRun(self, tree):
        self.runs += 1
        return self.result


class RxQuick(LeafNodeWidget):
    """Quick worker-thread leaf with a generous THREAD_WAIT (the tick waits for the result)."""

    _title = "Rx Quick"
    THREAD_WAIT = 5.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.threads: list[int] = []

    def OnRun(self, tree):
        self.threads.append(threading.get_ident())
        return True


class RxQuickDefaultWait(RxQuick):
    """Quick worker-thread leaf using the default THREAD_WAIT."""

    _title = "Rx Quick Default"
    THREAD_WAIT = LeafNodeWidget.THREAD_WAIT


class RxQuickNoWait(RxQuick):
    _title = "Rx Quick No Wait"
    THREAD_WAIT = 0


class RxNoTreeArg(LeafNodeWidget):
    """Declares ``OnRun(self)`` without the tree argument (GUI thread)."""

    _title = "Rx No Tree Arg"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls: list[tuple] = []

    def OnRun(self):
        tree = self.GetTree()
        tree.SetEntry(tree.GetEntry("n") + 1, "n")
        self.calls.append((threading.get_ident(), tree))
        return True


class RxNoTreeArgThreaded(RxNoTreeArg):
    _title = "Rx No Tree Arg Threaded"
    RUN_IN_THREAD = True


class RxVarArgs(LeafNodeWidget):
    """Declares ``OnRun(self, *args)``: receives the tree."""

    _title = "Rx Var Args"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.received: list[tuple] = []

    def OnRun(self, *args):
        self.received.append(args)
        return True


class RxRuntimeWriter(LeafNodeWidget):
    """Worker-thread leaf changing the blackboard, a field, a list selection and its title every run."""

    _title = "Rx Runtime Writer"
    _fields = {"count": 0, "choice": ["x", "y"]}
    THREAD_WAIT = 0.0

    def OnRun(self, tree):
        value = tree.GetEntry("counter") + 1
        tree.SetEntry(value, "counter")
        tree.SetEntry(f"run {value}", "made_by_run")
        self.SetField("count", value)
        self.SetFieldSelectionIndex("choice", value % 2)
        self.SetTitle(f"Writer {value}")
        return True


class RxRuntimeWriterGui(RxRuntimeWriter):
    _title = "Rx Runtime Writer Gui"
    RUN_IN_THREAD = False


# ============================================================================ helpers
def add_leaf(bt, node_type, parent=None, x=0.0, y=220.0):
    node = bt.AddNode(node_type, x, y)
    bt.Connect(parent if parent is not None else bt.GetRootNode(), node)
    return node


def record(signal) -> list:
    values: list = []
    signal.connect(lambda *args: values.append(args[0] if len(args) == 1 else args))
    return values


def status_label(node) -> QLabel:
    label = node.findChild(QLabel, "Status")
    assert label is not None
    return label


def shown(nodes) -> list[str]:
    """Status of each node (checking the Status label shows the same text)."""
    result = []
    for node in nodes:
        value = node.GetStatus().value
        assert status_label(node).text() == value
        result.append(value)
    return result


def nested_loop(ms: int, during=None, at: int | None = None) -> None:
    """Emulate a modal dialog: run a nested event loop for ``ms`` ms, calling ``during`` inside it."""
    loop = QEventLoop()
    if during is not None:
        QTimer.singleShot(at if at is not None else max(1, ms // 3), during)
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def errors_logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith(LOGGER)]


def flush_deferred_deletes() -> None:
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def keys_of(namespace: str) -> list[str]:
    return sorted(key for key in py_trees.blackboard.Blackboard.storage if key.startswith(namespace))


def ticks(bt) -> int:
    return bt.executor().tick_count()


def save(bt) -> None:
    assert bt.SaveTree(bt.GetFilePath())
    assert not bt.IsModified()


def make_other_tree_file(qtbot, bt, path) -> bool:
    """Save a tree file with a single Integer entry y = 3 using a throw-away blackboard state.

    The widget keeps its own file afterwards (SaveTree(path) switches the current file).
    """
    original = bt.GetFilePath()
    bt.AddEntry("y", "Integer", 3)
    ok = bt.SaveTree(path)
    bt.RemoveEntry("y")
    if original:
        assert bt.SaveTree(original)
    return ok


# ============================================================================ one OnRun call per node
@pytest.mark.parametrize("interrupt", ["Reset", "Stop+Execute"])
def test_restarted_node_waits_for_its_interrupted_on_run_call(qtbot, bt, gate, interrupt):
    leaf = add_leaf(bt, RxGated)
    leaf.state = gate
    finished = record(bt.executionFinished)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: gate.entered == [0])
    assert leaf.CancelRequested() is False
    if interrupt == "Reset":
        bt.Reset()
        assert leaf.CancelRequested() is True, "Reset must cancel the running call's token at once"
    else:
        bt.Stop()
        assert leaf.CancelRequested() is True, "Stop must cancel the running call's token at once"
        bt.Execute()
    start_ticks = ticks(bt)
    qtbot.waitUntil(lambda: ticks(bt) >= start_ticks + 6)
    # The new run exists (fresh token) but waits, showing Running, for the old call to return.
    assert leaf.CancelRequested() is False
    assert gate.entered == [0]
    assert gate.events == ["start", "enter0"], "OnStart of the new run must wait for the old OnRun call"
    assert shown([leaf]) == [RUNNING]
    assert bt.GetExecutionState() == "Running"

    gate.gate.set()
    run_until_idle(qtbot, bt)
    assert gate.max_active == 1
    assert gate.observed == {0: True, 1: False}, "the interrupted call must keep seeing CancelRequested()"
    assert gate.events == ["start", "enter0", "exit0", "start", "enter1", "exit1"]
    assert shown([bt.GetRootNode(), leaf]) == [SUCCEEDED, SUCCEEDED]
    assert finished == [SUCCEEDED]


def test_repeated_restarts_never_overlap_on_run_calls(qtbot, bt, gate):
    """Several Stop/Execute and Reset cycles while a call is blocked: still only one call at a time."""
    leaf = add_leaf(bt, RxGated)
    leaf.state = gate
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: gate.entered == [0])
    for _ in range(3):
        bt.Reset()
        qtbot.wait(15)
        bt.Stop()
        bt.Execute()
        qtbot.wait(15)
    assert gate.entered == [0]
    assert gate.events == ["start", "enter0"]
    gate.gate.set()
    run_until_idle(qtbot, bt)
    assert gate.max_active == 1
    assert gate.observed == {0: True, 1: False}
    assert shown([leaf]) == [SUCCEEDED]


def test_preempted_slow_leaf_is_not_restarted_while_its_call_still_runs(qtbot, bt, gate):
    """Selector(memory=False) [condition, slow worker leaf] in repeat mode: after the condition
    preempts the slow leaf, the next round restarts it only once the interrupted call returned."""
    root = bt.GetRootNode()
    root.SetCompositeType("Selector")
    root.SetMemory(False)
    switch = add_leaf(bt, RxSwitch, x=-200.0)
    slow = add_leaf(bt, RxGated, x=200.0)
    slow.state = gate
    fast_config(bt, repeat=True)
    bt.Execute()
    qtbot.waitUntil(lambda: gate.entered == [0] and shown([slow]) == [RUNNING])
    first_token_cancelled = slow.CancelRequested()
    switch.results = [True]  # the condition succeeds once: preempts the slow leaf
    qtbot.waitUntil(lambda: not switch.results)
    runs = switch.runs
    qtbot.waitUntil(lambda: switch.runs >= runs + 6)  # several new rounds
    assert first_token_cancelled is False
    assert gate.entered == [0], "a second OnRun call started while the interrupted one still runs"
    assert gate.events == ["start", "enter0"]
    assert shown([slow]) == [RUNNING]
    gate.gate.set()
    qtbot.waitUntil(lambda: len(gate.observed) >= 2)
    bt.Stop()
    assert gate.max_active == 1
    assert gate.observed[0] is True, "the preempted call must see CancelRequested()"
    assert gate.observed[1] is False
    assert gate.events[:4] == ["start", "enter0", "exit0", "start"]


def test_on_start_runs_on_the_gui_thread_from_the_first_update(qtbot, bt, gate):
    gui = threading.get_ident()
    seen: list[int] = []

    class StartRecorder(RxGated):
        TYPE_NAME = "RxStartRecorder"

        def OnStart(self, tree):
            seen.append(threading.get_ident())
            super().OnStart(tree)

    leaf = add_leaf(bt, StartRecorder)
    leaf.state = gate
    gate.gate.set()
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert seen == [gui]
    assert gate.events == ["start", "enter0", "exit0"]


# ============================================================================ ExecutionCancelled
def assert_cancelled_writes_refused(outcome: dict, missing_ok: bool = False) -> None:
    """Every write raised ExecutionCancelled; reads were allowed (``missing_ok``: reads of entries
    that no longer exist - disposed or replaced blackboard - may raise KeyError)."""
    for action in WRITE_ACTIONS:
        assert isinstance(outcome[action], ExecutionCancelled), f"{action}: {outcome[action]!r}"
    for action in READ_ACTIONS:
        result = outcome[action]
        if missing_ok and isinstance(result, KeyError):
            continue
        assert result == "ok", f"{action} (a read) failed: {result!r}"
    assert outcome["CancelRequested"] is True


@pytest.mark.parametrize("interrupt", ["Stop", "Pause+Stop", "Shutdown"])
def test_cancelled_worker_cannot_change_blackboard_or_node(qtbot, bt, gate, interrupt):
    bt.AddEntry("result", "Integer", 7)
    writer = add_leaf(bt, RxLateWriter)
    writer.state = gate
    fast_config(bt)
    save(bt)
    changes = record(writer.changed)
    bt.Execute()
    qtbot.waitUntil(lambda: gate.entered == [0])
    if interrupt == "Pause+Stop":
        bt.Pause()
        bt.Stop()
    else:
        getattr(bt, interrupt)()
    gate.gate.set()
    assert gate.done.wait(5)
    qtbot.wait(30)  # deliver anything queued by the worker
    assert_cancelled_writes_refused(gate.outcomes[0], missing_ok=interrupt == "Shutdown")
    if interrupt != "Shutdown":
        assert bt.GetEntryNames() == ["result"]
        assert bt.GetEntry("result") == 7
    assert writer.GetField("count") == 1
    assert writer.GetFieldSelectionIndex("choice") == 0
    assert writer.GetTitle() == "Rx Late Writer"
    assert changes == []
    assert bt.IsModified() is False


def test_reset_cancels_only_the_interrupted_call(qtbot, bt, gate):
    """After Reset the interrupted call's writes are refused; the restarted call writes normally."""
    bt.AddEntry("result", "Integer", 7)
    writer = add_leaf(bt, RxLateWriter)
    writer.state = gate
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: gate.entered == [0])
    bt.Reset()
    gate.gate.set()
    run_until_idle(qtbot, bt)
    assert_cancelled_writes_refused(gate.outcomes[0])
    second = gate.outcomes[1]
    assert all(value == "ok" for key, value in second.items() if key != "CancelRequested"), second
    assert second["CancelRequested"] is False
    qtbot.wait(20)
    assert bt.GetEntry("result") == "text"
    assert writer.GetField("count") == 99
    assert writer.GetFieldSelectionIndex("choice") == 2
    assert writer.GetTitle() == "changed by call 1"
    assert gate.max_active == 1


@pytest.mark.parametrize("operation", ["LoadTree", "NewTree"])
def test_worker_of_previous_tree_cannot_touch_the_loaded_tree(qtbot, bt, gate, tmp_path, operation):
    # tree B: one Integer entry "result" = 1
    bt.AddEntry("result", "Integer", 1)
    other = str(tmp_path / "other.json")
    assert bt.SaveTree(other)
    # tree A (current): a worker leaf that will write late
    assert bt.NewTree(str(tmp_path / "a.json"))
    bt.AddEntry("result", "Integer", 5)
    writer = add_leaf(bt, RxLateWriter)
    writer.state = gate
    fast_config(bt, restore_blackboard=True)
    save(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: gate.entered == [0])
    if operation == "LoadTree":
        assert bt.LoadTree(other)
        expected = [("result", "Integer", 1)]
    else:
        assert bt.NewTree(str(tmp_path / "fresh.json"))
        expected = []
    assert bt.GetExecutionState() == "Idle"
    modified = record(bt.modifiedChanged)
    gate.gate.set()
    assert gate.done.wait(5)
    qtbot.wait(50)
    assert_cancelled_writes_refused(gate.outcomes[0], missing_ok=operation == "NewTree")
    assert bt.blackboardStore().items() == expected
    assert bt.IsModified() is False
    assert modified == []
    assert writer not in bt.GetNodes()


# ============================================================================ THREAD_WAIT
def test_thread_wait_default_value():
    assert LeafNodeWidget.THREAD_WAIT == pytest.approx(0.02)


def test_quick_threaded_leaf_completes_within_the_tick_that_starts_it(qtbot, bt):
    root = bt.GetRootNode()
    leaf = add_leaf(bt, RxQuick)
    changes = []
    bt.nodeStatusChanged.connect(lambda node, status: changes.append(status) if node is leaf else None)
    finished = record(bt.executionFinished)
    fast_config(bt, tick_interval_ms=1000)
    bt.Execute()  # the first tick runs synchronously
    assert bt.GetExecutionState() == "Idle"
    assert ticks(bt) == 1
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]
    assert changes == [SUCCEEDED], "the leaf must never show Running"
    assert finished == [SUCCEEDED]
    assert len(leaf.threads) == 1 and leaf.threads[0] != threading.get_ident()


def test_thread_wait_zero_reports_running_on_the_starting_tick(qtbot, bt):
    leaf = add_leaf(bt, RxQuickNoWait)
    per_tick = []
    bt.executor().ticked.connect(lambda _n: per_tick.append(leaf.GetStatus().value))
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert per_tick[0] == RUNNING
    assert per_tick[-1] == SUCCEEDED
    assert len(leaf.threads) == 1


@pytest.mark.parametrize("composite", ["Sequence", "Selector"])
def test_reactive_composite_of_quick_threaded_leaves_completes_in_one_tick(qtbot, bt, composite):
    root = bt.GetRootNode()
    root.SetCompositeType(composite)
    root.SetMemory(False)
    leaves = [add_leaf(bt, RxQuick, x=i * 300.0) for i in range(3)]
    fast_config(bt, tick_interval_ms=1000)
    bt.Execute()
    assert bt.GetExecutionState() == "Idle"
    assert ticks(bt) == 1
    if composite == "Sequence":
        assert shown([root, *leaves]) == [SUCCEEDED] * 4
        assert [len(leaf.threads) for leaf in leaves] == [1, 1, 1]
    else:
        assert shown([root, *leaves]) == [SUCCEEDED, SUCCEEDED, READY, READY]


def test_sequence_without_memory_of_default_wait_threaded_leaves_completes(qtbot, bt):
    root = bt.GetRootNode()
    root.SetMemory(False)
    leaves = [add_leaf(bt, RxQuickDefaultWait, x=i * 300.0) for i in range(4)]
    finished = record(bt.executionFinished)
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt, timeout=10000)
    assert finished == [SUCCEEDED]
    assert shown([root, *leaves]) == [SUCCEEDED] * 5


# ============================================================================ OnRun(self)
@pytest.mark.parametrize("node_class", [RxNoTreeArg, RxNoTreeArgThreaded], ids=["gui-thread", "worker-thread"])
def test_on_run_without_tree_argument(qtbot, bt, node_class, caplog):
    bt.SetEntry(0, "n")
    leaf = add_leaf(bt, node_class)
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert shown([bt.GetRootNode(), leaf]) == [SUCCEEDED, SUCCEEDED]
    assert leaf.GetError() is None
    assert len(leaf.calls) == 1
    thread, tree = leaf.calls[0]
    assert tree is bt
    assert (thread == threading.get_ident()) is (node_class is RxNoTreeArg)
    assert bt.GetEntry("n") == 1
    assert errors_logged(caplog) == []


def test_on_run_with_var_args_receives_the_tree(qtbot, bt):
    leaf = add_leaf(bt, RxVarArgs)
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert leaf.received == [(bt,)]
    assert shown([leaf]) == [SUCCEEDED]


# ============================================================================ commands from a GUI-thread OnRun
def hooks(bt, *specs, log=None):
    """Add RxHook-like leaves below the root (left to right); ``specs`` are (class, tag) pairs."""
    log = [] if log is None else log
    nodes = []
    for index, (cls, tag) in enumerate(specs):
        node = add_leaf(bt, cls, x=index * 300.0)
        node.log = log
        node.tag = tag
        nodes.append(node)
    return log, nodes


@pytest.mark.parametrize("second", [RxHook, RxThreadedHook], ids=["gui-sibling", "threaded-sibling"])
def test_stop_from_on_run_runs_no_more_user_code_and_restores_blackboard(qtbot, bt, second):
    bt.AddEntry("x", "Integer", 0)
    log, (first, other) = hooks(bt, (RxHook, "a"), (second, "b"))

    def stop(tree, node):
        tree.SetEntry(5, "x")
        tree.Stop()
        assert tree.GetExecutionState() == "Running", "Stop is deferred until the tick ends"

    first.action = stop
    other.action = lambda tree, node: tree.SetEntry(99, "x")
    finished = record(bt.executionFinished)
    states = record(bt.executionStateChanged)
    fast_config(bt, restore_blackboard=True)
    bt.Execute()
    qtbot.wait(30)
    assert bt.GetExecutionState() == "Idle"
    assert [entry for entry in log if entry[1] == "b"] == [], "no user code may run after a deferred Stop"
    assert first.runs == 1 and first.starts == 1
    assert bt.GetEntry("x") == 0, "the restored blackboard was overwritten"
    assert shown([bt.GetRootNode(), first, other]) == [READY, READY, READY]
    assert finished == []
    assert states == ["Running", "Idle"]
    assert not bt.view().is_locked()


def test_reset_from_on_run_restarts_the_tree_after_the_tick(qtbot, bt):
    bt.AddEntry("x", "Integer", 0)
    log, (first, other) = hooks(bt, (RxHook, "a"), (RxHook, "b"))

    def reset_once(tree, node):
        tree.SetEntry(tree.GetEntry("x") + 1, "x")
        if node.runs == 1:
            tree.Reset()

    first.action = reset_once
    other.action = lambda tree, node: tree.SetEntry(tree.GetEntry("x") + 10, "x")
    finished = record(bt.executionFinished)
    fast_config(bt, restore_blackboard=True)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert first.runs == 2 and first.starts == 2
    assert other.runs == 1 and other.starts == 1
    runs_of_b = [i for i, entry in enumerate(log) if entry == ("run", "b")]
    second_start_of_a = [i for i, entry in enumerate(log) if entry == ("start", "a")][1]
    assert runs_of_b and runs_of_b[0] > second_start_of_a, "b must not run in the tick that called Reset"
    assert bt.GetEntry("x") == 11  # the Reset restored x to 0 before the second round
    assert finished == [SUCCEEDED]
    assert shown([bt.GetRootNode(), first, other]) == [SUCCEEDED] * 3


def test_execute_from_on_run_while_running_does_not_restart(qtbot, bt):
    log, (first, other) = hooks(bt, (RxSteps, "a"), (RxHook, "b"))
    first.steps = 3
    # Execute while the tree is running (only on ticks that do not finish the run)
    first.action = lambda tree, node: tree.Execute() if node._left > 0 else None
    finished = record(bt.executionFinished)
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    qtbot.wait(30)
    assert first.starts == 1 and first.runs == 4
    assert other.runs == 1
    assert finished == [SUCCEEDED]
    assert bt.GetExecutionState() == "Idle"


def test_pause_from_on_run_pauses_after_the_tick(qtbot, bt):
    log, (first, other) = hooks(bt, (RxSteps, "a"), (RxHook, "b"))
    first.steps = 2

    def pause_once(tree, node):
        if node.runs == 1:
            tree.Pause()
            assert tree.GetExecutionState() == "Running"

    first.action = pause_once
    finished = record(bt.executionFinished)
    fast_config(bt)
    bt.Execute()
    assert bt.GetExecutionState() == "Paused"
    count = ticks(bt)
    qtbot.wait(40)
    assert ticks(bt) == count and first.runs == 1
    assert shown([first]) == [RUNNING]
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert first.starts == 1 and first.runs == 3
    assert other.runs == 1
    assert finished == [SUCCEEDED]


@pytest.mark.parametrize("command", ["Stop", "Reset"])
@pytest.mark.parametrize("composite", ["Sequence", "Selector"])
def test_running_sibling_is_only_interrupted_after_a_deferred_command(qtbot, bt, command, composite):
    """Reactive composite [hook, long-running sibling]: when the hook issues Stop/Reset on a later
    tick, the running sibling gets no more user code in that tick (no OnRun, no OnTerminate(Failed));
    it is interrupted afterwards with exactly one OnTerminate(Ready)."""
    root = bt.GetRootNode()
    root.SetCompositeType(composite)
    root.SetMemory(False)
    log, (hook, runner) = hooks(bt, (RxHook, "hook"), (RxSteps, "runner"))
    runner.steps = 1000
    marker = []

    def command_on_third_run(tree, node):
        if node.runs == 3:
            getattr(tree, command)()
            marker.append(len(log))
        return composite == "Sequence"  # Sequence: succeed; Selector: fail -> the runner is ticked

    hook.action = command_on_third_run
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: bool(marker), timeout=3000)
    qtbot.wait(20)
    after = log[marker[0]:]
    runner_after = [entry for entry in after if entry[1] == "runner"]
    if command == "Stop":
        assert runner_after == [("terminate", "runner", READY)], runner_after
        assert bt.GetExecutionState() == "Idle"
    else:
        assert runner_after[0] == ("terminate", "runner", READY), runner_after
        assert ("terminate", "runner", FAILED) not in runner_after
        bt.Stop()


@pytest.mark.parametrize("command", ["Stop", "Reset"])
def test_deferred_command_cancels_worker_token_immediately(qtbot, bt, gate, command):
    root = bt.GetRootNode()
    root.SetMemory(False)
    log, (hook,) = hooks(bt, (RxHook, "hook"))
    slow = add_leaf(bt, RxGated, x=400.0)
    slow.state = gate
    seen = []

    def command_on_second_run(tree, node):
        if node.runs == 2:
            getattr(tree, command)()
            seen.append(slow.CancelRequested())

    hook.action = command_on_second_run
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: bool(seen), timeout=3000)
    assert seen == [True]
    gate.gate.set()
    qtbot.waitUntil(lambda: 0 in gate.observed)
    assert gate.observed[0] is True
    assert FAILED not in gate.terminated, gate.terminated
    bt.Stop()
    assert gate.max_active == 1


# ============================================================================ commands from slots / nested event loops
@pytest.mark.parametrize("signal", ["nodeError", "nodeStatusChanged", "executionFinished"])
def test_modal_dialog_in_slot_during_final_tick_does_not_rerun_tree(qtbot, bt, signal):
    root = bt.GetRootNode()
    leaf = add_leaf(bt, RxCountingRaise)
    fast_config(bt, tick_interval_ms=5)
    finished = record(bt.executionFinished)
    states = record(bt.executionStateChanged)
    depth = {"now": 0, "max": 0, "calls": 0}

    def show_dialog(*args):
        if signal == "nodeStatusChanged" and args != (root, FAILED):
            return
        depth["calls"] += 1
        depth["now"] += 1
        depth["max"] = max(depth["max"], depth["now"])
        nested_loop(120)
        depth["now"] -= 1

    getattr(bt, signal).connect(show_dialog)
    bt.Execute()
    qtbot.waitUntil(lambda: depth["now"] == 0 and not bt.IsExecuting(), timeout=5000)
    qtbot.wait(60)
    assert leaf.runs == 1, "a finished run-once tree was ticked again"
    assert depth["calls"] == 1 and depth["max"] == 1
    assert finished == [FAILED]
    assert states == ["Running", "Idle"]
    assert ticks(bt) == 1
    assert shown([root, leaf]) == [FAILED, FAILED]


@pytest.mark.parametrize("command", ["Stop", "Reset", "Pause", "Execute"])
def test_command_from_modal_dialog_in_status_slot(qtbot, bt, command):
    """A nodeStatusChanged slot shows a 'modal dialog' (nested loop) during a non-final tick and the
    command is issued while it is open (e.g. the user clicks a button / the slot calls the API)."""
    bt.AddEntry("x", "Integer", 0)
    log, (first, second) = hooks(bt, (RxHook, "a"), (RxSteps, "b"))
    second.steps = 3
    first.action = lambda tree, node: tree.SetEntry(tree.GetEntry("x") + 1, "x")
    finished = record(bt.executionFinished)
    states = record(bt.executionStateChanged)
    fast_config(bt, tick_interval_ms=5, restore_blackboard=True)
    opened = []

    def on_status(node, status):
        if node is first and status == SUCCEEDED and not opened:
            opened.append(ticks(bt))
            nested_loop(100, during=getattr(bt, command))

    bt.nodeStatusChanged.connect(on_status)
    bt.Execute()
    qtbot.waitUntil(lambda: bool(opened) and not bt.executor()._ticking, timeout=3000)
    if command == "Stop":
        qtbot.wait(40)
        assert bt.GetExecutionState() == "Idle"
        assert second.runs == 1, "user code ran after the deferred Stop"
        assert shown([bt.GetRootNode(), first, second]) == [READY] * 3
        assert bt.GetEntry("x") == 0
        assert finished == []
        assert states == ["Running", "Idle"]
    elif command == "Pause":
        assert bt.GetExecutionState() == "Paused"
        count = ticks(bt)
        qtbot.wait(40)
        assert ticks(bt) == count and second.runs == 1
        bt.Execute()
        run_until_idle(qtbot, bt)
        assert first.runs == 1 and second.starts == 1 and second.runs == 4
        assert finished == [SUCCEEDED]
        assert states == ["Running", "Paused", "Running", "Idle"]
    elif command == "Reset":
        run_until_idle(qtbot, bt)
        assert first.runs == 2 and second.starts == 2
        assert bt.GetEntry("x") == 1  # restored to 0 by the Reset, then incremented once more
        assert finished == [SUCCEEDED]
        assert states == ["Running", "Idle"]
    else:  # Execute while running: no restart
        run_until_idle(qtbot, bt)
        assert first.runs == 1 and second.starts == 1 and second.runs == 4
        assert finished == [SUCCEEDED]
        assert states == ["Running", "Idle"]
    assert shown([first]) == ([READY] if command == "Stop" else [SUCCEEDED])


@pytest.mark.parametrize("threaded", [False, True], ids=["gui-writer", "threaded-writer"])
def test_stop_clicked_during_modal_dialog_in_on_run(qtbot, bt, threaded):
    """A GUI-thread OnRun shows a 'modal dialog' and the user clicks Stop meanwhile: the sibling
    that follows never runs and the restored blackboard is not overwritten."""
    bt.AddEntry("x", "Integer", 0)
    log, (asker, writer) = hooks(bt, (RxHook, "asker"), (RxThreadedHook if threaded else RxHook, "writer"))

    def ask(tree, node):
        tree.SetEntry(5, "x")
        nested_loop(100, during=tree.button("Stop").click)
        return True

    asker.action = ask
    writer.action = lambda tree, node: tree.SetEntry(99, "x")
    finished = record(bt.executionFinished)
    fast_config(bt, tick_interval_ms=5, restore_blackboard=True)
    bt.Execute()
    run_until_idle(qtbot, bt)
    qtbot.wait(40)
    assert asker.runs == 1, "the tree was ticked re-entrantly during the modal dialog"
    assert [entry for entry in log if entry[1] == "writer"] == []
    assert bt.GetEntry("x") == 0
    assert finished == []
    assert shown([bt.GetRootNode(), asker, writer]) == [READY] * 3


def test_stop_from_status_slot_resets_every_node(qtbot, bt):
    root = bt.GetRootNode()
    root.SetCompositeType("Selector")
    fail = add_leaf(bt, "FailNode", x=-200.0)
    runner = add_leaf(bt, "RunningN", x=200.0)
    runner.SetField("ticks", 50)
    finished = record(bt.executionFinished)

    def on_status(node, status):
        if node is fail and status == FAILED:
            bt.Stop()

    bt.nodeStatusChanged.connect(on_status)
    fast_config(bt)
    bt.Execute()
    qtbot.wait(40)
    assert bt.GetExecutionState() == "Idle"
    assert shown([root, fail, runner]) == [READY] * 3
    assert finished == []


def test_signals_after_completion_see_a_consistent_state(qtbot, bt):
    root = bt.GetRootNode()
    leaf = add_leaf(bt, "RunningN")
    leaf.SetField("ticks", 2)
    seen = []

    def snapshot(*_args):
        seen.append(
            (
                bt.GetExecutionState(),
                bt.view().is_locked(),
                bt.button("Stop").isEnabled(),
                bt.button("Execute").isEnabled(),
                root.GetStatus().value,
            )
        )

    bt.executionFinished.connect(snapshot)
    last_tick = []
    bt.executor().ticked.connect(lambda n: last_tick.append((n, bt.GetExecutionState(), root.GetStatus().value)))
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert seen == [("Idle", False, False, True, SUCCEEDED)]
    assert last_tick[-1] == (3, "Idle", SUCCEEDED)
    assert [n for n, _, _ in last_tick] == [1, 2, 3]


def test_finished_root_is_not_reticked_in_run_once_mode(qtbot, bt):
    leaf = add_leaf(bt, RxCounting)
    finished = record(bt.executionFinished)
    fast_config(bt, tick_interval_ms=1)
    for _ in range(5):
        bt.Execute()
        run_until_idle(qtbot, bt)
    qtbot.wait(30)
    assert leaf.runs == 5
    assert finished == [SUCCEEDED] * 5


# ============================================================================ tick failure
@pytest.mark.parametrize("fail_on_update", [1, 3])
def test_tick_failure_stops_execution_and_reports_on_root(qtbot, bt, monkeypatch, caplog, fail_on_update):
    root = bt.GetRootNode()
    leaf = add_leaf(bt, "RunningN")
    leaf.SetField("ticks", 10)
    original = LeafBehaviour.update
    calls = [0]

    def failing_update(self):
        calls[0] += 1
        if calls[0] >= fail_on_update:
            raise RuntimeError("adapter exploded")
        return original(self)

    monkeypatch.setattr(LeafBehaviour, "update", failing_update)
    statuses = record(bt.nodeStatusChanged)
    errors = record(bt.nodeError)
    finished = record(bt.executionFinished)
    states = record(bt.executionStateChanged)
    fast_config(bt)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        bt.Execute()
        run_until_idle(qtbot, bt)
    expected_error = "Tick failed: RuntimeError: adapter exploded"
    assert finished == [FAILED]
    assert errors == [(root, expected_error)]
    root_statuses = [status for node, status in statuses if node is root]
    assert root_statuses and root_statuses[-1] == FAILED
    assert shown([root, leaf]) == [FAILED, READY]
    assert root.GetError() == expected_error
    assert status_label(root).toolTip() == expected_error
    assert states == ["Running", "Idle"]
    assert not bt.view().is_locked()
    assert bt.button("Execute").isEnabled() and not bt.button("Stop").isEnabled()
    assert any("tick failed" in message for message in errors_logged(caplog))
    count = ticks(bt)
    qtbot.wait(30)
    assert ticks(bt) == count
    # the widget stays usable
    monkeypatch.setattr(LeafBehaviour, "update", original)
    leaf.SetField("ticks", 1)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert finished == [FAILED, SUCCEEDED]
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]
    assert root.GetError() is None


# ============================================================================ removed nodes
def test_status_refresh_skips_nodes_removed_from_the_view(qtbot, bt):
    """White-box: _refresh_statuses must skip nodes removed from the view (and deleted) while the
    py_trees tree still references them (the path used by clear() for Load / New)."""
    root = bt.GetRootNode()
    keep = add_leaf(bt, "RunningN", x=-200.0)
    keep.SetField("ticks", 3)
    gone = add_leaf(bt, "RunningN", x=200.0)
    gone.SetField("ticks", 1)
    fast_config(bt)
    bt.Execute()
    assert shown([keep]) == [RUNNING]
    bt.view()._remove_node(gone)  # internal removal, bypassing the structure lock
    flush_deferred_deletes()
    assert not shiboken6.isValid(gone)
    run_until_idle(qtbot, bt)
    assert shown([root, keep]) == [SUCCEEDED, SUCCEEDED]
    assert gone not in bt.GetNodes()


# ============================================================================ structure lock
@pytest.mark.parametrize("paused", [False, True], ids=["running", "paused"])
def test_structure_edits_raise_while_executing_and_run_completes(qtbot, bt, paused, caplog):
    root = bt.GetRootNode()
    leaf = add_leaf(bt, "RunningN", x=-200.0)
    leaf.SetField("ticks", 3)
    loose = bt.AddNode("Succeed", 200.0, 400.0)
    selector = bt.AddNode("Selector", 500.0, 400.0)
    fast_config(bt, tick_interval_ms=20)
    save(bt)
    finished = record(bt.executionFinished)
    bt.Execute()
    if paused:
        bt.Pause()
        assert bt.GetExecutionState() == "Paused"
    nodes_before = bt.GetNodes()
    attempts = {
        "RemoveNode(connected)": lambda: bt.RemoveNode(leaf),
        "RemoveNode(loose)": lambda: bt.RemoveNode(loose),
        "Connect": lambda: bt.Connect(root, loose),
        "Connect(reparent)": lambda: bt.Connect(selector, leaf),
        "Disconnect": lambda: bt.Disconnect(leaf),
        "AddNode(name)": lambda: bt.AddNode("Succeed", 0, 600),
        "AddNode(composite)": lambda: bt.AddNode("Sequence", 0, 600),
        "AddNode(class)": lambda: bt.AddNode(RxCounting, 0, 600),
        "SetParent(None)": lambda: leaf.SetParent(None),
        "SetParent(root)": lambda: loose.SetParent(root),
        "SetParent(reparent)": lambda: leaf.SetParent(selector),
        "AddChild": lambda: root.AddChild(loose),
        "RemoveChild": lambda: root.RemoveChild(leaf),
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for name, attempt in attempts.items():
            with pytest.raises(RuntimeError):
                attempt()
        instance = Succeed()
        try:
            with pytest.raises(RuntimeError):
                bt.AddNode(instance, 0, 600)
        finally:
            instance.deleteLater()
        assert bt.GetNodes() == nodes_before
        assert root.GetChildren() == [leaf] and leaf.GetParent() is root
        assert loose.GetParent() is None and selector.GetChildren() == []
        assert bt.GetExecutionState() == ("Paused" if paused else "Running")
        assert bt.IsModified() is False
        if paused:
            bt.Execute()
        run_until_idle(qtbot, bt)
        qtbot.wait(30)
    assert finished == [SUCCEEDED]
    assert shown([root, leaf, loose, selector]) == [SUCCEEDED, SUCCEEDED, READY, READY]
    assert errors_logged(caplog) == []
    # editing works again once idle
    bt.Connect(root, loose)
    leaf.SetParent(selector)
    bt.RemoveNode(selector)
    assert bt.AddNode("Succeed", 0, 600) in bt.GetNodes()


# ============================================================================ run-time vs edit changes
@pytest.mark.parametrize("writer_class", [RxRuntimeWriter, RxRuntimeWriterGui], ids=["worker", "gui-thread"])
def test_runtime_changes_never_mark_the_tree_modified(qtbot, bt, writer_class):
    bt.AddEntry("counter", "Integer", 0)
    writer = add_leaf(bt, writer_class)
    fast_config(bt, tick_interval_ms=1)
    save(bt)
    runtime_flags = record(writer.changed)
    store_flags = record(bt.blackboardStore().changed)
    modified = record(bt.modifiedChanged)
    for run_index in range(15):
        bt.Execute()
        run_until_idle(qtbot, bt)
        assert bt.IsModified() is False, f"modified right after run {run_index}"
        QCoreApplication.processEvents()  # deliver queued run-time changes after the run ended
        qtbot.wait(1)
        assert bt.IsModified() is False, f"modified after the queued changes of run {run_index}"
    qtbot.wait(20)
    assert modified == []
    assert bt.GetEntry("counter") == 15
    assert writer.GetTitle() == "Writer 15"
    assert writer.GetField("count") == 15
    assert runtime_flags and all(flag is True for flag in runtime_flags)
    assert store_flags and all(flag is True for flag in store_flags)


def test_user_edits_in_editors_mark_the_tree_modified(qtbot, bt):
    bt.AddEntry("counter", "Integer", 0)
    writer = add_leaf(bt, RxRuntimeWriter)
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    qtbot.wait(10)
    save(bt)
    flags = record(writer.changed)
    writer.field_editor("count").setValue(1234)
    assert writer.GetField("count") == 1234
    assert flags == [False]
    assert bt.IsModified() is True
    save(bt)
    bt.blackboardView().row("counter").value_widget.setValue(77)
    assert bt.GetEntry("counter") == 77
    assert bt.IsModified() is True


def test_edits_while_paused_programmatic_are_runtime_user_edits_are_not(qtbot, bt):
    bt.AddEntry("counter", "Integer", 0)
    runner = add_leaf(bt, "RunningN")
    runner.SetField("ticks", 100)
    writer = bt.AddNode(RxRuntimeWriter, 400.0, 400.0)  # not connected
    fast_config(bt)
    save(bt)
    bt.Execute()
    bt.Pause()
    flags = record(writer.changed)
    # programmatic setters on the GUI thread while executing are run-time changes
    writer.SetField("count", 5)
    writer.SetFieldSelectionIndex("choice", 1)
    writer.SetTitle("Renamed by code")
    bt.SetEntry(3, "counter")
    qtbot.wait(10)
    assert flags == [True, True, True]
    assert bt.IsModified() is False
    # the user editing the node's or the blackboard's editors is an edit of the tree
    writer.field_editor("count").setValue(6)
    assert flags[-1] is False
    assert bt.IsModified() is True
    save(bt)
    bt.blackboardView().row("counter").value_widget.setValue(9)
    assert bt.GetEntry("counter") == 9
    assert bt.IsModified() is True
    bt.Stop()


# ============================================================================ restore_blackboard
class _Marker:
    def __init__(self, label):
        self.label = label


def completed_run_with_writes(qtbot, bt):
    """x: 0 -> 1, "made" created during the run, Object entry "obj" replaced during the run."""
    before, after = _Marker("before"), _Marker("after")
    bt.AddEntry("x", "Integer", 0)
    bt.SetEntry(before, "obj")
    assert bt.GetEntryType("obj") == "Object"
    log, (writer,) = hooks(bt, (RxHook, "w"))

    def write(tree, node):
        tree.SetEntry(tree.GetEntry("x") + 1, "x")
        tree.SetEntry("yes", "made")
        tree.SetEntry(after, "obj")

    writer.action = write
    fast_config(bt, restore_blackboard=True)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert bt.GetEntry("x") == 1 and bt.GetEntry("made") == "yes" and bt.GetEntry("obj") is after
    return before, after


def test_reset_after_completed_run_restores_pre_run_values_once(qtbot, bt):
    before, _after = completed_run_with_writes(qtbot, bt)
    bt.Reset()
    assert bt.GetEntryNames() == ["x", "obj"]
    assert bt.GetEntry("x") == 0
    assert bt.GetEntry("obj") is not None and bt.GetEntry("obj").label == "before"
    assert bt.blackboardView().row("made") is None
    # the snapshot is consumed: a later Reset does not roll back again
    bt.SetEntry(7, "x")
    bt.Reset()
    assert bt.GetEntry("x") == 7


def test_stop_while_idle_after_completed_run_does_not_restore(qtbot, bt):
    _before, after = completed_run_with_writes(qtbot, bt)
    bt.Stop()
    assert bt.GetEntry("x") == 1
    assert bt.GetEntry("made") == "yes"
    assert bt.GetEntry("obj") is after
    bt.Reset()  # the snapshot was discarded by Stop
    assert bt.GetEntry("x") == 1
    assert bt.GetEntry("made") == "yes"


def test_stop_while_running_restores(qtbot, bt):
    bt.AddEntry("x", "Integer", 0)
    runner = add_leaf(bt, RxSteps)
    runner.steps = 100
    runner.action = lambda tree, node: tree.SetEntry(tree.GetEntry("x") + 1, "x")
    fast_config(bt, restore_blackboard=True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetEntry("x") >= 2)
    bt.Stop()
    assert bt.GetEntry("x") == 0


@pytest.mark.parametrize("operation", ["NewTree", "LoadTree"])
def test_load_or_new_after_completed_run_does_not_roll_back(qtbot, bt, tmp_path, operation):
    other = str(tmp_path / "other.json")
    if operation == "LoadTree":
        assert make_other_tree_file(qtbot, bt, other)
    _before, after = completed_run_with_writes(qtbot, bt)
    if operation == "NewTree":
        assert bt.NewTree(str(tmp_path / "fresh.json"))
        expected_names = ["obj"]
    else:
        assert bt.LoadTree(other)
        expected_names = ["y", "obj"]
    assert bt.GetEntry("obj") is after, "Object entries keep their run-time value"
    assert sorted(bt.GetEntryNames()) == sorted(expected_names)
    bt.Reset()  # no stale snapshot of the previous tree may be restored
    assert sorted(bt.GetEntryNames()) == sorted(expected_names)
    assert bt.GetEntry("obj") is after
    if operation == "LoadTree":
        assert bt.GetEntry("y") == 3
    assert bt.IsModified() is False


@pytest.mark.parametrize("where", ["OnRun", "nodeStatusChanged"])
@pytest.mark.parametrize("operation", ["NewTree", "LoadTree"])
def test_load_or_new_during_a_tick(qtbot, bt, tmp_path, operation, where):
    """LoadTree / NewTree called while a tick is in progress (from a GUI-thread OnRun, e.g. a node that
    loads the next mission, or from a nodeStatusChanged slot) must load the new tree: execution stops,
    the new tree is installed and its blackboard is not overwritten by the previous tree's
    restore_blackboard snapshot."""
    other = str(tmp_path / "other.json")
    assert make_other_tree_file(qtbot, bt, other)
    bt.AddEntry("x", "Integer", 0)
    log, (leaf,) = hooks(bt, (RxHook, "leaf"))
    root = bt.GetRootNode()
    outcome = []

    def load(*_args):
        try:
            if operation == "NewTree":
                outcome.append(bt.NewTree(str(tmp_path / "fresh.json")))
            else:
                outcome.append(bt.LoadTree(other))
        except Exception as error:  # noqa: BLE001 - reported below
            outcome.append(error)

    def action(tree, node):
        tree.SetEntry(1, "x")
        if where == "OnRun":
            load()

    leaf.action = action
    if where == "nodeStatusChanged":
        bt.nodeStatusChanged.connect(
            lambda node, status: load() if node is root and status == SUCCEEDED and not outcome else None
        )
    fast_config(bt, restore_blackboard=True)
    save(bt)
    bt.Execute()
    qtbot.wait(40)
    assert outcome == [True], outcome
    assert bt.GetExecutionState() == "Idle"
    assert not bt.view().is_locked()
    new_root = bt.GetRootNode()
    assert new_root is not None, "the widget was left without a tree"
    assert leaf not in bt.GetNodes()
    expected = [] if operation == "NewTree" else [("y", "Integer", 3)]
    assert bt.blackboardStore().items() == expected
    assert bt.IsModified() is False
    assert all(node.GetStatus() is NodeStatus.READY for node in bt.GetNodes())
    finished = record(bt.executionFinished)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert finished == [SUCCEEDED]


@pytest.mark.parametrize("command", ["Stop", "Reset", "LoadTree"])
def test_command_from_idle_state_slot_after_completion_keeps_finished_signal(qtbot, bt, tmp_path, command):
    """executionStateChanged("Idle") is emitted while the final tick of a run-once execution is still
    in progress. A Stop / Reset / LoadTree issued from that slot comes after the run completed, so the
    completion is still reported (executionFinished exactly once) and Stop (while idle) does not
    restore the blackboard, while Reset (while idle) restores the kept snapshot."""
    other = str(tmp_path / "other.json")
    assert make_other_tree_file(qtbot, bt, other)
    bt.AddEntry("x", "Integer", 0)
    log, (writer,) = hooks(bt, (RxHook, "w"))
    writer.action = lambda tree, node: tree.SetEntry(1, "x")
    fast_config(bt, restore_blackboard=True)
    finished = record(bt.executionFinished)
    ticked = record(bt.executor().ticked)
    issued = []

    def on_state(state):
        if state == "Idle" and not issued:
            issued.append(state)
            if command == "LoadTree":
                issued.append(bt.LoadTree(other))
            else:
                getattr(bt, command)()

    bt.executionStateChanged.connect(on_state)
    bt.Execute()
    qtbot.wait(40)
    assert issued[0] == "Idle"
    assert bt.GetExecutionState() == "Idle"
    assert writer.runs == 1
    if command == "Stop":
        assert bt.GetEntry("x") == 1
    elif command == "Reset":
        assert bt.GetEntry("x") == 0
    else:
        assert issued[1] is True
        assert bt.blackboardStore().items() == [("y", "Integer", 3)]
    assert finished == [SUCCEEDED], "the completed run was not reported"
    assert ticked == [1]


# ============================================================================ Shutdown
@pytest.mark.parametrize("paused", [False, True], ids=["running", "paused"])
def test_shutdown_while_executing(qtbot, bt, paused, caplog):
    bt.SetEntry(1, "x")
    runner = add_leaf(bt, RxSteps)
    runner.steps = 1000
    fast_config(bt)
    states = record(bt.executionStateChanged)
    bt.Execute()
    if paused:
        bt.Pause()
    assert bt.view().is_locked()
    key = bt.blackboardStore().key("x")
    assert py_trees.blackboard.Blackboard.exists(key)
    bt.Shutdown()
    assert states[-1] == "Idle"
    assert bt.GetExecutionState() == "Idle"
    assert not bt.view().is_locked()
    assert not bt.button("Pause").isEnabled()
    assert not bt.button("Stop").isEnabled()
    assert runner.log[-1] == ("terminate", "hook", READY)
    # blackboard released and rows cleared
    assert not py_trees.blackboard.Blackboard.exists(key)
    assert bt.GetEntryNames() == []
    assert bt.blackboardView().row("x") is None
    with pytest.raises(RuntimeError):
        bt.SetEntry(2, "x")
    with pytest.raises(RuntimeError):
        bt.AddEntry("y", "Integer", 1)
    # later Execute is a (logged) no-op
    runs, count = runner.runs, ticks(bt)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        bt.Execute()
        bt.button("Execute").click()
    qtbot.wait(40)
    assert bt.GetExecutionState() == "Idle"
    assert runner.runs == runs and ticks(bt) == count
    assert any("Shutdown" in record.getMessage() for record in caplog.records)
    assert not bt.view().is_locked()


def test_shutdown_cancels_worker_and_later_execute_is_refused(qtbot, bt, gate):
    leaf = add_leaf(bt, RxGated)
    leaf.state = gate
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: gate.entered == [0])
    bt.Shutdown()
    gate.gate.set()
    qtbot.waitUntil(lambda: 0 in gate.observed)
    assert gate.observed[0] is True
    bt.Execute()
    qtbot.wait(40)
    assert gate.entered == [0]
    assert bt.GetExecutionState() == "Idle"


# ============================================================================ closeEvent / destruction
def test_close_without_delete_on_close_stops_and_widget_stays_usable(qtbot, bt, gate):
    assert bt.isWindow()
    assert not bt.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    bt.SetEntry(5, "counter")
    key = bt.blackboardStore().key("counter")
    leaf = add_leaf(bt, RxGated)
    leaf.state = gate
    fast_config(bt)
    save(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: gate.entered == [0])
    assert bt.close()
    assert not bt.isVisible()
    assert bt.GetExecutionState() == "Idle"
    assert not bt.view().is_locked()
    assert leaf.CancelRequested() is True
    assert py_trees.blackboard.Blackboard.exists(key), "close without WA_DeleteOnClose must not shut down"

    bt.show()
    qtbot.wait(10)
    assert bt.isVisible()
    assert bt.GetEntry("counter") == 5
    bt.SetEntry(6, "counter")
    assert bt.GetEntry("counter") == 6
    assert bt.blackboardView().row("counter") is not None
    extra = add_leaf(bt, "Succeed", x=400.0)
    gate.gate.set()
    finished = record(bt.executionFinished)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert finished == [SUCCEEDED]
    assert shown([bt.GetRootNode(), leaf, extra]) == [SUCCEEDED] * 3
    assert gate.observed == {0: True, 1: False}


def test_close_with_delete_on_close_shuts_down(qtbot, tmp_path, gate):
    widget = BehaviorTreeWidget(node_types=[RxGated])
    closed = False
    try:
        widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        widget.resize(800, 600)
        widget.show()
        assert widget.NewTree(str(tmp_path / "t.json"))
        widget.SetEntry(1, "x")
        namespace = widget.GetBlackboardNamespace()
        leaf = add_leaf(widget, RxGated)
        leaf.state = gate
        fast_config(widget)
        widget._set_modified(False)
        states = record(widget.executionStateChanged)
        widget.Execute()
        qtbot.waitUntil(lambda: gate.entered == [0])
        assert widget.close()
        closed = True
        # Shutdown happened in closeEvent (before the deferred deletion)
        assert states[-1] == "Idle"
        assert keys_of(namespace) == []
        with pytest.raises(RuntimeError):
            widget.SetEntry(2, "x")
        widget.Execute()  # refused after Shutdown
        assert widget.GetExecutionState() == "Idle"
    finally:
        if not closed and shiboken6.isValid(widget):
            widget.Shutdown()
            widget.deleteLater()
    flush_deferred_deletes()
    qtbot.waitUntil(lambda: not shiboken6.isValid(widget), timeout=2000)
    gate.gate.set()
    qtbot.waitUntil(lambda: 0 in gate.observed)
    assert gate.observed[0] is True
    assert gate.entered == [0]


@pytest.mark.parametrize("worker", ["gated", "late-writer"])
def test_destroying_widget_while_threaded_leaf_runs(qtbot, tmp_path, gate, worker):
    """deleteLater without Shutdown(): the destroyed signal cancels running nodes and releases keys."""
    node_class = RxGated if worker == "gated" else RxLateWriter
    widget = BehaviorTreeWidget(node_types=[node_class])
    try:
        widget.show()
        assert widget.NewTree(str(tmp_path / "t.json"))
        widget.AddEntry("result", "Integer", 1)
        namespace = widget.GetBlackboardNamespace()
        assert keys_of(namespace)
        leaf = add_leaf(widget, node_class)
        leaf.state = gate
        fast_config(widget)
        widget._set_modified(False)
        widget.Execute()
        qtbot.waitUntil(lambda: gate.entered == [0])
    except BaseException:
        widget.Shutdown()
        widget.deleteLater()
        raise
    widget.deleteLater()
    flush_deferred_deletes()
    qtbot.waitUntil(lambda: not shiboken6.isValid(widget), timeout=2000)
    assert keys_of(namespace) == [], "py_trees keys of a destroyed widget must be released"
    gate.gate.set()
    if worker == "gated":
        qtbot.waitUntil(lambda: 0 in gate.observed)
        assert gate.observed[0] is True, "the running leaf's token must be cancelled"
    else:
        assert gate.done.wait(5)
        assert_cancelled_writes_refused(gate.outcomes[0], missing_ok=True)
    qtbot.wait(30)
    assert keys_of(namespace) == []


def test_destroying_idle_widget_with_blackboard_entries_raises_nothing(qtbot, tmp_path):
    """The destroyed-signal cleanup (no Shutdown call) must not raise from the blackboard view's slots."""
    widget = BehaviorTreeWidget()
    widget.show()
    assert widget.NewTree(str(tmp_path / "t.json"))
    widget.AddEntry("x", "Integer", 1)
    widget.SetEntry("text", "s")
    namespace = widget.GetBlackboardNamespace()
    widget._set_modified(False)
    widget.deleteLater()
    flush_deferred_deletes()
    qtbot.waitUntil(lambda: not shiboken6.isValid(widget), timeout=2000)
    assert keys_of(namespace) == []


# ============================================================================ misc token / repeat checks
def test_gui_thread_leaf_token_is_cancelled_by_stop_and_fresh_on_the_next_run(qtbot, bt):
    runner = add_leaf(bt, RxSteps)
    runner.steps = 1000
    fast_config(bt)
    bt.Execute()
    assert runner.CancelRequested() is False
    bt.Stop()
    assert runner.CancelRequested() is True
    bt.Execute()
    assert runner.CancelRequested() is False
    bt.Reset()
    assert runner.CancelRequested() is True
    qtbot.waitUntil(lambda: runner.starts == 3)
    assert runner.CancelRequested() is False
    bt.Stop()


def test_repeat_mode_restarts_rounds_and_never_reports_finished(qtbot, bt):
    leaf = add_leaf(bt, RxHook)
    finished = record(bt.executionFinished)
    fast_config(bt, repeat=True)
    bt.Execute()
    qtbot.waitUntil(lambda: leaf.starts >= 5)
    assert bt.GetExecutionState() == "Running"
    bt.Stop()
    assert finished == []
    assert leaf.runs == leaf.starts
