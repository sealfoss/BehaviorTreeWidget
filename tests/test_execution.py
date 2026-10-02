"""Execution of behavior trees (execution.py), driven through BehaviorTreeWidget.

Trees are built with ``bt.AddNode`` / ``bt.Connect`` and run with ``bt.Execute()``
using a short tick interval (``fast_config``). Covers composite semantics (Sequence,
Selector, nesting, memory), node statuses and errors, the leaf lifecycle hooks,
worker threads and cancellation, the Execute / Pause / Stop / Reset commands, the
execution options (repeat, restore_blackboard, tick interval), the widget signals
and buttons, and the ``result_to_status`` / ``run_in_daemon_thread`` helpers.
"""

from __future__ import annotations

import itertools
import threading
from concurrent.futures import Future

import py_trees
import pytest
from py_trees.common import Status
from PySide6.QtWidgets import QLabel

from behavior_tree_widget import ExecutionCancelled, LeafNodeWidget, NodeStatus, UnknownLeafNodeWidget
from behavior_tree_widget.execution import result_to_status, run_in_daemon_thread
from conftest import (
    Raise,
    Recorder,
    ReturnNone,
    RunningN,
    SlowCancelable,
    ThreadedSucceed,
    fast_config,
    run_until_idle,
)

READY, RUNNING, SUCCEEDED, FAILED = "Ready", "Running", "Succeeded", "Failed"
ALL_STATUSES = {READY, RUNNING, SUCCEEDED, FAILED}


# ============================================================================ test node types
class ThreadedFail(LeafNodeWidget):
    """Fails on a worker thread."""

    _title = "Threaded Fail"

    def OnRun(self, tree):
        return False


class ThreadedRaise(Raise):
    """Raises ValueError("boom") on a worker thread."""

    _title = "Threaded Raise"
    RUN_IN_THREAD = True


class ThreadedReturnNone(ReturnNone):
    _title = "Threaded Return None"
    RUN_IN_THREAD = True


class ThreadedRunningN(RunningN):
    """RunningN whose OnRun runs on a worker thread; counts OnRun calls."""

    _title = "Threaded Running N"
    RUN_IN_THREAD = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = 0
        self.threads: list[int] = []

    def OnRun(self, tree):
        self.runs += 1
        self.threads.append(threading.get_ident())
        return super().OnRun(tree)


class CountingFail(LeafNodeWidget):
    """Fails on the GUI thread and counts its OnRun calls."""

    _title = "Counting Fail"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = 0

    def OnRun(self, tree):
        self.runs += 1
        return False


class ForeverRunning(LeafNodeWidget):
    """Returns RUNNING forever (GUI thread) and records its lifecycle calls."""

    _title = "Forever Running"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls: list[tuple] = []

    def OnStart(self, tree):
        self.calls.append(("start",))

    def OnRun(self, tree):
        self.calls.append(("run",))
        return Status.RUNNING

    def OnTerminate(self, tree, status):
        self.calls.append(("terminate", status))


class OrderLog(LeafNodeWidget):
    """Appends its ``name`` field to ``self.sink`` (a list shared by the test)."""

    _title = "Order Log"
    _fields = {"name": ""}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        self.sink.append(self.GetField("name"))
        return True


class BlackboardDoubler(LeafNodeWidget):
    """Writes 2 * entry "input" to entry "output" and records the ``tree`` argument."""

    _title = "Doubler"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.trees: list = []

    def OnRun(self, tree):
        self.trees.append(tree)
        tree.SetEntry(tree.GetEntry("input") * 2, "output")
        return True


class ThreadedBlackboardDoubler(BlackboardDoubler):
    _title = "Threaded Doubler"
    RUN_IN_THREAD = True


class StartRaises(LeafNodeWidget):
    """OnStart raises; counts OnRun calls."""

    _title = "Start Raises"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = 0

    def OnStart(self, tree):
        raise RuntimeError("start failed")

    def OnRun(self, tree):
        self.runs += 1
        return True


class ThreadedStartRaises(StartRaises):
    _title = "Threaded Start Raises"
    RUN_IN_THREAD = True


class ReturnsValue(LeafNodeWidget):
    """Returns ``self.result`` from OnRun (GUI thread)."""

    _title = "Returns Value"
    RUN_IN_THREAD = False
    result = None

    def OnRun(self, tree):
        return self.result


class ThreadedReturnsValue(ReturnsValue):
    _title = "Threaded Returns Value"
    RUN_IN_THREAD = True


class Flaky(LeafNodeWidget):
    """Raises ValueError("flaky") while ``self.should_raise`` is True, else succeeds."""

    _title = "Flaky"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.should_raise = True

    def OnRun(self, tree):
        if self.should_raise:
            raise ValueError("flaky")
        return True


class FailingRecorder(Recorder):
    _title = "Failing Recorder"

    def OnRun(self, tree):
        super().OnRun(tree)
        return False


class RaisingRecorder(Recorder):
    _title = "Raising Recorder"

    def OnRun(self, tree):
        super().OnRun(tree)
        raise ValueError("boom")


class ThreadedRecorder(Recorder):
    _title = "Threaded Recorder"
    RUN_IN_THREAD = True


class TerminateRaises(LeafNodeWidget):
    """Succeeds, but OnTerminate raises."""

    _title = "Terminate Raises"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True

    def OnTerminate(self, tree, status):
        raise RuntimeError("terminate failed")


class BlackboardWriter(LeafNodeWidget):
    """Increments entry "x" and creates entry "made_by_run"."""

    _title = "Blackboard Writer"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("x") + 1, "x")
        tree.SetEntry("yes", "made_by_run")
        return True


class ThreadedBlackboardWriter(BlackboardWriter):
    _title = "Threaded Blackboard Writer"
    RUN_IN_THREAD = True


class ThreadRecorder(LeafNodeWidget):
    """Worker-thread leaf recording the thread each lifecycle hook ran on."""

    _title = "Thread Recorder"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.threads: dict[str, int] = {}

    def OnStart(self, tree):
        self.threads["start"] = threading.get_ident()

    def OnRun(self, tree):
        self.threads["run"] = threading.get_ident()
        return True

    def OnTerminate(self, tree, status):
        self.threads["terminate"] = threading.get_ident()


class GateState:
    """Per-test synchronisation shared with Gated / LateWriter nodes."""

    def __init__(self):
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.started: list[int] = []
        self.observed: dict[int, bool] = {}
        self.finished = threading.Event()
        self.errors: list[BaseException] = []


class Gated(LeafNodeWidget):
    """Worker-thread leaf: blocks until ``state.gate`` opens, then records CancelRequested()."""

    _title = "Gated"

    def OnRun(self, tree):
        state = self.state  # bound for the whole call, even if the test replaces it
        with state.lock:
            index = len(state.started)
            state.started.append(index)
        state.gate.wait(10)
        state.observed[index] = self.CancelRequested()
        return True


class LateWriter(LeafNodeWidget):
    """Worker-thread leaf: blocks until ``state.gate`` opens, then writes entry "late"."""

    _title = "Late Writer"

    def OnRun(self, tree):
        state = self.state
        state.started.append(0)
        state.gate.wait(10)
        try:
            tree.SetEntry(42, "late")
        except BaseException as error:  # noqa: BLE001 - reported to the test
            state.errors.append(error)
            raise
        finally:
            state.finished.set()
        return True


# ============================================================================ fixtures / helpers
@pytest.fixture(autouse=True)
def _reset_shared_node_state():
    ThreadedSucceed.threads.clear()
    SlowCancelable.cancelled.clear()
    yield


@pytest.fixture
def gate_state():
    state = GateState()
    yield state
    state.gate.set()  # never leave a worker thread blocked


def build(bt, root_type="Sequence", children=(), memory=True):
    """Build a tree below the root from a nested spec and return ``(root, nodes)``.

    ``children`` items are leaf type names / classes, or ``(composite, [children])`` /
    ``(composite, memory, [children])`` tuples. ``nodes`` lists the created nodes in
    depth-first order. Every node gets its own column, so siblings are ordered left to
    right in spec order. ``memory`` is used for the root and every composite that does
    not specify its own.
    """
    root = bt.GetRootNode()
    root.SetCompositeType(root_type)
    root.SetMemory(memory)
    nodes = []
    column = itertools.count()

    def add(parent, spec, depth):
        if isinstance(spec, tuple):
            kind, kids = spec[0], spec[-1]
            node = bt.AddNode(kind, next(column) * 400.0, depth * 300.0)
            node.SetMemory(spec[1] if len(spec) == 3 else memory)
        else:
            kids = ()
            node = bt.AddNode(spec, next(column) * 400.0, depth * 300.0)
        bt.Connect(parent, node)
        nodes.append(node)
        for kid in kids:
            add(node, kid, depth + 1)

    for spec in children:
        add(root, spec, 1)
    return root, nodes


def run(qtbot, bt, timeout=5000, **config):
    """Execute the tree with a fast tick interval and wait until it finished."""
    fast_config(bt, **config)
    bt.Execute()
    run_until_idle(qtbot, bt, timeout=timeout)


def record(signal) -> list:
    """Connect a recorder to ``signal``; single-argument emissions are stored unwrapped."""
    values: list = []
    signal.connect(lambda *args: values.append(args[0] if len(args) == 1 else args))
    return values


def status_label(node) -> QLabel:
    label = node.findChild(QLabel, "Status")
    assert label is not None
    return label


def shown(nodes) -> list[str]:
    """Status of each node, checking that the Status label shows the same text."""
    result = []
    for node in nodes:
        value = node.GetStatus().value
        assert status_label(node).text() == value, f"{node!r}: label {status_label(node).text()!r} != {value!r}"
        result.append(value)
    return result


def ticks(bt) -> int:
    return bt.executor().tick_count()


# ============================================================================ Sequence / Selector semantics
COMPOSITE_CASES = [
    pytest.param("Sequence", ["Succeed", "Succeed"], SUCCEEDED, [SUCCEEDED, SUCCEEDED], id="seq-all-succeed"),
    pytest.param("Sequence", ["Succeed", "FailNode", "Succeed"], FAILED, [SUCCEEDED, FAILED, READY], id="seq-stops-at-failure"),
    pytest.param("Sequence", ["FailNode", "Succeed"], FAILED, [FAILED, READY], id="seq-first-fails"),
    pytest.param("Selector", ["FailNode", "Succeed", "FailNode"], SUCCEEDED, [FAILED, SUCCEEDED, READY], id="sel-stops-at-success"),
    pytest.param("Selector", ["FailNode", "FailNode"], FAILED, [FAILED, FAILED], id="sel-all-fail"),
    pytest.param("Selector", ["Succeed", "FailNode"], SUCCEEDED, [SUCCEEDED, READY], id="sel-first-succeeds"),
    pytest.param(
        "Sequence",
        [("Selector", ["FailNode", "Succeed"]), ("Sequence", ["Succeed", "Succeed"])],
        SUCCEEDED,
        [SUCCEEDED, FAILED, SUCCEEDED, SUCCEEDED, SUCCEEDED, SUCCEEDED],
        id="nested-seq-of-sel-and-seq",
    ),
    pytest.param(
        "Selector",
        [("Sequence", ["Succeed", "FailNode"]), ("Sequence", ["Succeed"])],
        SUCCEEDED,
        [FAILED, SUCCEEDED, FAILED, SUCCEEDED, SUCCEEDED],
        id="nested-sel-falls-back-to-second-seq",
    ),
    pytest.param(
        "Sequence",
        [("Selector", ["FailNode", "FailNode"]), "Succeed"],
        FAILED,
        [FAILED, FAILED, FAILED, READY],
        id="nested-failing-selector-stops-seq",
    ),
    pytest.param(
        "Selector",
        [("Sequence", [("Selector", ["FailNode"]), "Succeed"]), "Succeed"],
        SUCCEEDED,
        [FAILED, FAILED, FAILED, READY, SUCCEEDED],
        id="three-levels-deep",
    ),
]


@pytest.mark.parametrize("memory", [True, False], ids=["memory", "no-memory"])
@pytest.mark.parametrize("root_type, children, expected_root, expected_nodes", COMPOSITE_CASES)
def test_composite_semantics(qtbot, bt, memory, root_type, children, expected_root, expected_nodes):
    root, nodes = build(bt, root_type, children, memory=memory)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert shown([root]) == [expected_root]
    assert shown(nodes) == expected_nodes
    assert finished == [expected_root]


def test_composite_semantics_with_threaded_leaves(qtbot, bt):
    # Memory only: without memory py_trees re-ticks earlier children every tick, and a
    # threaded leaf always reports RUNNING on its first tick, so it would restart forever.
    root, nodes = build(
        bt, "Sequence", ["ThreadedSucceed", ("Selector", [ThreadedFail, "ThreadedSucceed"]), ThreadedFail, "Succeed"]
    )
    run(qtbot, bt)
    assert shown([root, *nodes]) == [FAILED, SUCCEEDED, SUCCEEDED, FAILED, SUCCEEDED, FAILED, READY]


@pytest.mark.parametrize("composite, expected", [("Sequence", SUCCEEDED), ("Selector", FAILED)])
def test_empty_root(qtbot, bt, composite, expected):
    """An empty Sequence succeeds and an empty Selector fails (py_trees semantics)."""
    root = bt.GetRootNode()
    root.SetCompositeType(composite)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert shown([root]) == [expected]
    assert finished == [expected]


def test_children_run_left_to_right_regardless_of_insertion_order(qtbot, bt):
    root = bt.GetRootNode()
    sink: list[str] = []
    for name, x in (("right", 900.0), ("left", -500.0), ("middle", 200.0)):
        node = bt.AddNode(OrderLog, x, 300.0)
        node.SetField("name", name)
        node.sink = sink
        bt.Connect(root, node)
    run(qtbot, bt)
    assert sink == ["left", "middle", "right"]


def test_graph_is_translated_to_py_trees_behaviours(qtbot, bt):
    root, (sequence, succeed, forever, fail) = build(
        bt, "Selector", [("Sequence", False, ["Succeed", ForeverRunning]), "FailNode"], memory=True
    )
    fast_config(bt)
    bt.Execute()
    bt.Pause()
    tree = bt.executor().tree()
    assert isinstance(tree, py_trees.trees.BehaviourTree)
    assert isinstance(tree.root, py_trees.composites.Selector)
    assert tree.root.memory is True
    py_sequence = tree.root.children[0]
    assert isinstance(py_sequence, py_trees.composites.Sequence)
    assert py_sequence.memory is False
    assert [child.name for child in py_sequence.children] == ["Succeed", "Forever Running"]
    assert all(isinstance(child, py_trees.behaviour.Behaviour) for child in py_sequence.children)
    assert bt.executor().behaviour_for(root) is tree.root
    assert bt.executor().behaviour_for(sequence) is py_sequence
    assert bt.executor().behaviour_for(fail) is tree.root.children[1]
    bt.Stop()
    assert bt.executor().tree() is None


# ============================================================================ memory
@pytest.mark.parametrize("nested", [False, True], ids=["at-root", "nested"])
@pytest.mark.parametrize(
    "composite, first, memory, expected_runs",
    [
        ("Sequence", "Recorder", True, 1),
        ("Sequence", "Recorder", False, 3),
        ("Selector", CountingFail, True, 1),
        ("Selector", CountingFail, False, 3),
    ],
)
def test_memory_controls_reticking_of_earlier_children(qtbot, bt, composite, first, memory, expected_runs, nested):
    """With memory a composite resumes its RUNNING child; without, earlier children are re-ticked."""
    children = [first, "RunningN"]
    if nested:
        root, nodes = build(bt, "Sequence", [(composite, memory, children)], memory=True)
        _, first_node, running = nodes
    else:
        root, (first_node, running) = build(bt, composite, children, memory=memory)
    running.SetField("ticks", 2)
    run(qtbot, bt)
    runs = first_node.runs if isinstance(first_node, CountingFail) else first_node.calls.count(("run",))
    assert runs == expected_runs
    assert shown([root, running]) == [SUCCEEDED, SUCCEEDED]


@pytest.mark.parametrize("memory", [True, False], ids=["memory", "no-memory"])
def test_selector_keeps_showing_failed_child_while_later_sibling_runs(qtbot, bt, memory):
    """py_trees invalidates the failed child of a memory Selector; the view keeps its last result."""
    root, (fail, running) = build(bt, "Selector", ["FailNode", "RunningN"], memory=memory)
    running.SetField("ticks", 3)
    per_tick = []
    bt.executor().ticked.connect(lambda _n: per_tick.append((fail.GetStatus().value, running.GetStatus().value)))
    fail_changes = []
    bt.nodeStatusChanged.connect(lambda node, status: fail_changes.append(status) if node is fail else None)
    run(qtbot, bt)
    assert per_tick == [(FAILED, RUNNING)] * 3 + [(FAILED, SUCCEEDED)]
    assert fail_changes == [FAILED]
    assert shown([root, fail, running]) == [SUCCEEDED, FAILED, SUCCEEDED]


@pytest.mark.parametrize("memory, preempted", [(False, True), (True, False)], ids=["no-memory", "memory"])
def test_selector_preemption_by_higher_priority_child(qtbot, bt, memory, preempted):
    """Without memory a higher priority child that starts succeeding interrupts the running child."""
    bt.SetEntry(0, "counter")
    root, (condition, forever) = build(bt, "Selector", ["CounterAtLeast", ForeverRunning], memory=memory)
    condition.SetField("threshold", 1)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: forever.calls.count(("run",)) >= 2)
    bt.SetEntry(1, "counter")
    if preempted:
        run_until_idle(qtbot, bt)
        assert shown([root, condition, forever]) == [SUCCEEDED, SUCCEEDED, READY]
        assert forever.calls[-1] == ("terminate", NodeStatus.READY)
    else:
        runs = forever.calls.count(("run",))
        qtbot.waitUntil(lambda: forever.calls.count(("run",)) >= runs + 3)
        assert bt.GetExecutionState() == "Running"
        assert shown([root, condition, forever]) == [RUNNING, FAILED, RUNNING]
        bt.Stop()


def test_preempted_threaded_leaf_is_cancelled(qtbot, bt):
    bt.SetEntry(0, "counter")
    root, (condition, slow) = build(bt, "Selector", ["CounterAtLeast", "SlowCancelable"], memory=False)
    condition.SetField("threshold", 1)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: slow.GetStatus() is NodeStatus.RUNNING)
    SlowCancelable.cancelled.clear()
    bt.SetEntry(1, "counter")
    run_until_idle(qtbot, bt)
    qtbot.waitUntil(SlowCancelable.cancelled.is_set, timeout=2000)
    assert shown([root, condition, slow]) == [SUCCEEDED, SUCCEEDED, READY]


# ============================================================================ disconnected nodes
def test_disconnected_nodes_are_ignored_and_stay_ready(qtbot, bt):
    root, (leaf, forever) = build(bt, "Sequence", ["Succeed", ForeverRunning])
    lonely_raise = bt.AddNode("Raise", 0, 800)
    lonely_fail = bt.AddNode("FailNode", 400, 800)
    orphan_selector = bt.AddNode("Selector", 800, 800)
    orphan_child = bt.AddNode("Raise", 800, 1100)
    bt.Connect(orphan_selector, orphan_child)
    disconnected = [lonely_raise, lonely_fail, orphan_selector, orphan_child]

    fast_config(bt)
    bt.Execute()
    assert bt.executor().behaviour_for(forever) is not None
    for node in disconnected:
        assert bt.executor().behaviour_for(node) is None
    bt.Stop()
    bt.Disconnect(forever)
    run(qtbot, bt)
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]
    assert shown(disconnected + [forever]) == [READY] * 5
    assert all(node.GetError() is None for node in disconnected)


def test_empty_root_with_disconnected_nodes(qtbot, bt):
    root = bt.GetRootNode()
    stray = [bt.AddNode("FailNode", 0, 300), bt.AddNode("Raise", 400, 300)]
    run(qtbot, bt)
    assert shown([root]) == [SUCCEEDED]
    assert shown(stray) == [READY, READY]


# ============================================================================ statuses
def test_node_status_values_are_the_four_documented_strings():
    assert [status.value for status in NodeStatus] == [READY, RUNNING, SUCCEEDED, FAILED]
    assert str(NodeStatus.SUCCEEDED) == SUCCEEDED
    assert NodeStatus.from_py_trees(Status.RUNNING) is NodeStatus.RUNNING
    assert NodeStatus.from_py_trees(Status.SUCCESS) is NodeStatus.SUCCEEDED
    assert NodeStatus.from_py_trees(Status.FAILURE) is NodeStatus.FAILED
    assert NodeStatus.from_py_trees(Status.INVALID) is NodeStatus.READY


def test_statuses_during_execution_are_exact_strings(qtbot, bt):
    root, nodes = build(
        bt, "Sequence", ["RunningN", ("Selector", ["FailNode", "ThreadedSucceed"]), "Succeed", "FailNode", "Succeed"]
    )
    assert shown([root, *nodes]) == [READY] * (len(nodes) + 1)
    emitted = record(bt.nodeStatusChanged)
    labels: set[str] = set()
    bt.executor().ticked.connect(lambda _n: labels.update(status_label(n).text() for n in [root, *nodes]))
    run(qtbot, bt)
    assert emitted, "nodeStatusChanged was never emitted"
    assert all(isinstance(status, str) for _node, status in emitted)
    assert {status for _node, status in emitted} <= ALL_STATUSES
    assert labels <= ALL_STATUSES
    assert all(isinstance(node.GetStatus(), NodeStatus) for node in [root, *nodes])
    assert shown([root, *nodes]) == [FAILED, SUCCEEDED, SUCCEEDED, FAILED, SUCCEEDED, SUCCEEDED, FAILED, READY]


def test_root_shows_running_while_a_leaf_runs(qtbot, bt):
    root, (leaf,) = build(bt, "Sequence", ["RunningN"])
    leaf.SetField("ticks", 3)
    per_tick = []
    bt.executor().ticked.connect(lambda _n: per_tick.append((root.GetStatus().value, leaf.GetStatus().value)))
    run(qtbot, bt)
    assert per_tick == [(RUNNING, RUNNING)] * 3 + [(SUCCEEDED, SUCCEEDED)]


@pytest.mark.parametrize("n", [0, 1, 3])
def test_running_n_returns_running_n_times(qtbot, bt, n):
    root, (leaf,) = build(bt, "Sequence", ["RunningN"])
    leaf.SetField("ticks", n)
    per_tick = []
    bt.executor().ticked.connect(lambda _n: per_tick.append(leaf.GetStatus().value))
    run(qtbot, bt)
    assert per_tick == [RUNNING] * n + [SUCCEEDED]
    assert ticks(bt) == n + 1


def test_threaded_leaf_returning_running_is_called_again(qtbot, bt):
    root, (leaf,) = build(bt, "Sequence", [ThreadedRunningN])
    leaf.SetField("ticks", 3)
    run(qtbot, bt)
    assert leaf.runs == 4
    assert all(ident != threading.get_ident() for ident in leaf.threads)
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]


# ============================================================================ worker threads
class ThreadedNoWait(ThreadedSucceed):
    """ThreadedSucceed that never waits for its worker within the tick."""

    TYPE_NAME = "ThreadedNoWait"
    THREAD_WAIT = 0.0


def test_threaded_leaf_runs_off_the_gui_thread_and_shows_running(qtbot, bt):
    """Without THREAD_WAIT a worker-thread leaf reports Running on the tick that starts it."""
    root, (leaf,) = build(bt, "Sequence", [ThreadedNoWait])
    per_tick = []
    bt.executor().ticked.connect(lambda _n: per_tick.append(leaf.GetStatus().value))
    changes = []
    bt.nodeStatusChanged.connect(lambda node, status: changes.append(status) if node is leaf else None)
    run(qtbot, bt)
    assert len(ThreadedSucceed.threads) == 1
    assert ThreadedSucceed.threads[0] != threading.get_ident()
    assert per_tick[0] == RUNNING
    assert per_tick[-1] == SUCCEEDED
    assert changes == [RUNNING, SUCCEEDED]
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]


def test_on_start_and_on_terminate_run_on_the_gui_thread_for_threaded_leaves(qtbot, bt):
    _, (leaf,) = build(bt, "Sequence", [ThreadRecorder])
    run(qtbot, bt)
    gui = threading.get_ident()
    assert leaf.threads["start"] == gui
    assert leaf.threads["terminate"] == gui
    assert leaf.threads["run"] != gui


@pytest.mark.parametrize("node_class", [BlackboardDoubler, ThreadedBlackboardDoubler], ids=["gui-thread", "worker-thread"])
def test_on_run_receives_the_widget_and_can_use_the_blackboard(qtbot, bt, node_class):
    bt.SetEntry(21, "input")
    root, (leaf,) = build(bt, "Sequence", [node_class])
    run(qtbot, bt)
    assert leaf.trees == [bt]
    assert bt.GetEntry("output") == 42
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]
    qtbot.waitUntil(lambda: bt.blackboardView().row("output") is not None)


# ============================================================================ errors
@pytest.mark.parametrize("node_type", ["Raise", ThreadedRaise], ids=["gui-thread", "worker-thread"])
def test_exception_in_on_run_fails_the_node_with_error(qtbot, bt, node_type):
    root, (leaf,) = build(bt, "Sequence", [node_type])
    errors = record(bt.nodeError)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert shown([root, leaf]) == [FAILED, FAILED]
    error = leaf.GetError()
    assert error is not None and "ValueError" in error and "boom" in error
    tooltip = status_label(leaf).toolTip()
    assert "ValueError" in tooltip and "boom" in tooltip
    assert (leaf, error) in errors
    assert finished == [FAILED]


@pytest.mark.parametrize("node_type", ["ReturnNone", ThreadedReturnNone], ids=["gui-thread", "worker-thread"])
def test_on_run_returning_none_fails_with_helpful_message(qtbot, bt, node_type):
    root, (leaf,) = build(bt, "Sequence", [node_type])
    errors = record(bt.nodeError)
    run(qtbot, bt)
    assert shown([root, leaf]) == [FAILED, FAILED]
    error = leaf.GetError()
    assert error is not None
    assert "None" in error and "return" in error.lower()
    assert "True" in error and "False" in error  # tells the developer what to return instead
    assert error in status_label(leaf).toolTip()
    assert [message for node, message in errors if node is leaf] == [error]


@pytest.mark.parametrize("node_class", [ReturnsValue, ThreadedReturnsValue], ids=["gui-thread", "worker-thread"])
@pytest.mark.parametrize(
    "result, fragment",
    [(Status.INVALID, "INVALID"), (1, "int"), ("yes", "str"), ([True], "list")],
    ids=["invalid-status", "int", "str", "list"],
)
def test_on_run_returning_an_unsupported_value_fails(qtbot, bt, node_class, result, fragment):
    root, (leaf,) = build(bt, "Sequence", [node_class])
    leaf.result = result
    run(qtbot, bt)
    assert shown([root, leaf]) == [FAILED, FAILED]
    assert leaf.GetError() is not None and fragment in leaf.GetError()


@pytest.mark.parametrize("node_class", [ReturnsValue, ThreadedReturnsValue], ids=["gui-thread", "worker-thread"])
@pytest.mark.parametrize(
    "result, expected",
    [(True, SUCCEEDED), (False, FAILED), (Status.SUCCESS, SUCCEEDED), (Status.FAILURE, FAILED)],
    ids=["True", "False", "SUCCESS", "FAILURE"],
)
def test_on_run_supported_return_values(qtbot, bt, node_class, result, expected):
    root, (leaf,) = build(bt, "Sequence", [node_class])
    leaf.result = result
    errors = record(bt.nodeError)
    run(qtbot, bt)
    assert shown([root, leaf]) == [expected, expected]
    assert leaf.GetError() is None
    assert status_label(leaf).toolTip() == ""
    assert errors == []


@pytest.mark.parametrize("node_class", [StartRaises, ThreadedStartRaises], ids=["gui-thread", "worker-thread"])
def test_exception_in_on_start_fails_the_node(qtbot, bt, node_class):
    root, (leaf,) = build(bt, "Sequence", [node_class])
    errors = record(bt.nodeError)
    run(qtbot, bt)
    assert shown([root, leaf]) == [FAILED, FAILED]
    error = leaf.GetError()
    assert error is not None and "RuntimeError" in error and "start failed" in error
    assert (leaf, error) in errors


def test_unknown_node_type_fails_when_executed(qtbot, bt):
    root = bt.GetRootNode()
    placeholder = bt.AddNode(UnknownLeafNodeWidget("MissingType"), 0, 300)
    bt.Connect(root, placeholder)
    run(qtbot, bt)
    assert shown([root, placeholder]) == [FAILED, FAILED]
    assert "not registered" in placeholder.GetError()


def test_error_is_cleared_when_the_node_later_succeeds(qtbot, bt):
    root, (leaf,) = build(bt, "Sequence", [Flaky])
    run(qtbot, bt)
    assert leaf.GetError() is not None and "flaky" in leaf.GetError()
    leaf.should_raise = False
    run(qtbot, bt)
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]
    assert leaf.GetError() is None
    assert status_label(leaf).toolTip() == ""


# ============================================================================ lifecycle hooks
@pytest.mark.parametrize(
    "node_type, result",
    [("Recorder", SUCCEEDED), (ThreadedRecorder, SUCCEEDED), (FailingRecorder, FAILED), (RaisingRecorder, FAILED)],
    ids=["succeeds", "threaded", "fails", "raises"],
)
def test_recorder_lifecycle_order(qtbot, bt, node_type, result):
    _, (leaf,) = build(bt, "Sequence", [node_type])
    run(qtbot, bt)
    assert leaf.calls == [("start",), ("run",), ("terminate", result)]
    assert leaf.GetStatus().value == result


def test_exception_in_on_terminate_does_not_break_execution(qtbot, bt):
    root, nodes = build(bt, "Sequence", [TerminateRaises, "Succeed"])
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert shown([root, *nodes]) == [SUCCEEDED, SUCCEEDED, SUCCEEDED]
    assert finished == [SUCCEEDED]


def test_on_terminate_called_once_per_run(qtbot, bt):
    _, (leaf, fail) = build(bt, "Sequence", ["Recorder", "FailNode"])
    for _ in range(3):
        run(qtbot, bt)
    assert leaf.calls == [("start",), ("run",), ("terminate", SUCCEEDED)] * 3


def test_on_terminate_receives_node_status(qtbot, bt):
    _, (forever,) = build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    bt.Execute()
    bt.Stop()
    status = forever.calls[-1][1]
    assert isinstance(status, NodeStatus)
    assert status is NodeStatus.READY


@pytest.mark.parametrize("command", ["Stop", "Reset"])
def test_interrupted_leaf_is_terminated_with_ready(qtbot, bt, command):
    _, (forever,) = build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: forever.calls.count(("run",)) >= 2)
    getattr(bt, command)()
    assert forever.calls[-1] == ("terminate", NodeStatus.READY)
    assert forever.calls.count(("start",)) == 1
    if command == "Reset":
        qtbot.waitUntil(lambda: forever.calls.count(("start",)) == 2)
        bt.Stop()
        assert forever.calls[-1] == ("terminate", NodeStatus.READY)
    starts = forever.calls.count(("start",))
    terminates = [call for call in forever.calls if call[0] == "terminate"]
    assert len(terminates) == starts


# ============================================================================ Execute / Pause / Stop / Reset
def test_execute_without_a_tree_does_nothing(qtbot, make_widget):
    widget = make_widget()
    assert not widget.IsTreeLoaded()
    widget.Execute()
    assert widget.GetExecutionState() == "Idle"
    assert not widget.IsExecuting()
    assert widget.GetRootNode().GetStatus() is NodeStatus.READY


def test_execute_while_running_does_not_restart(qtbot, bt):
    _, (forever,) = build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: ticks(bt) >= 3)
    before = ticks(bt)
    bt.Execute()
    assert ticks(bt) >= before
    assert forever.calls.count(("start",)) == 1
    assert bt.GetExecutionState() == "Running"
    bt.Stop()


def test_pause_stops_ticking_and_execute_resumes(qtbot, bt):
    root, (leaf,) = build(bt, "Sequence", ["RunningN"])
    leaf.SetField("ticks", 30)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: ticks(bt) >= 3)
    bt.Pause()
    assert bt.GetExecutionState() == "Paused"
    assert bt.IsExecuting()
    paused_at = ticks(bt)
    qtbot.wait(150)
    assert ticks(bt) == paused_at
    assert shown([root, leaf]) == [RUNNING, RUNNING]
    bt.Execute()
    assert bt.GetExecutionState() == "Running"
    run_until_idle(qtbot, bt)
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]
    assert ticks(bt) == 31  # resumed where it paused, not restarted


def test_pause_when_idle_is_a_no_op(qtbot, bt):
    states = record(bt.executionStateChanged)
    bt.Pause()
    assert bt.GetExecutionState() == "Idle"
    assert states == []


def test_stop_resets_every_node_and_cancels_threaded_leaf(qtbot, bt):
    root, nodes = build(bt, "Sequence", ["Succeed", ("Selector", ["FailNode", "SlowCancelable"])])
    succeed, selector, fail, slow = nodes
    finished = record(bt.executionFinished)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: slow.GetStatus() is NodeStatus.RUNNING)
    assert shown([root, *nodes]) == [RUNNING, SUCCEEDED, RUNNING, FAILED, RUNNING]
    SlowCancelable.cancelled.clear()
    bt.Stop()
    assert bt.GetExecutionState() == "Idle"
    assert not bt.IsExecuting()
    assert shown([root, *nodes]) == [READY] * 5
    qtbot.waitUntil(SlowCancelable.cancelled.is_set, timeout=2000)
    assert finished == []
    qtbot.wait(30)
    assert shown([root, *nodes]) == [READY] * 5


def test_stop_while_paused(qtbot, bt):
    root, (forever,) = build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    bt.Execute()
    bt.Pause()
    bt.Stop()
    assert bt.GetExecutionState() == "Idle"
    assert shown([root, forever]) == [READY, READY]
    assert forever.calls[-1] == ("terminate", NodeStatus.READY)


def test_stop_during_threaded_on_run_that_later_sets_entry(qtbot, bt, gate_state):
    """A cancelled OnRun call can no longer write the blackboard (ExecutionCancelled)."""
    root, (writer,) = build(bt, "Sequence", [LateWriter])
    writer.state = gate_state
    bt.SaveTree(bt.GetFilePath())
    fast_config(bt)
    bt._set_modified(False)
    bt.Execute()
    qtbot.waitUntil(lambda: gate_state.started == [0])
    bt.Stop()
    gate_state.gate.set()
    assert gate_state.finished.wait(5)
    qtbot.wait(50)  # deliver queued notifications
    assert len(gate_state.errors) == 1 and isinstance(gate_state.errors[0], ExecutionCancelled)
    assert not bt.HasEntry("late")
    assert bt.IsModified() is False
    assert bt.GetExecutionState() == "Idle"
    assert shown([root, writer]) == [READY, READY]
    gate_state.errors.clear()
    gate_state.finished.clear()
    run(qtbot, bt)  # the widget remains usable; the new run writes normally
    assert shown([root, writer]) == [SUCCEEDED, SUCCEEDED]
    assert bt.GetEntry("late") == 42


def test_reset_while_running_restarts_and_completes(qtbot, bt):
    root, (leaf,) = build(bt, "Sequence", ["RunningN"])
    leaf.SetField("ticks", 20)
    finished = record(bt.executionFinished)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: ticks(bt) >= 5)
    after_reset = record(bt.executor().ticked)
    bt.Reset()
    assert shown([root, leaf]) == [READY, READY]
    assert bt.GetExecutionState() == "Running"
    assert bt.view().is_locked()
    run_until_idle(qtbot, bt)
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]
    assert finished == [SUCCEEDED]
    assert len(after_reset) == 21  # the whole tree started over


def test_reset_while_paused_keeps_paused_state(qtbot, bt):
    root, (leaf,) = build(bt, "Sequence", ["RunningN"])
    leaf.SetField("ticks", 5)
    fast_config(bt)
    bt.Execute()
    bt.Pause()
    bt.Reset()
    assert bt.GetExecutionState() == "Paused"
    assert shown([root, leaf]) == [READY, READY]
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert shown([root, leaf]) == [SUCCEEDED, SUCCEEDED]


@pytest.mark.parametrize("command", ["Reset", "Stop"])
def test_reset_or_stop_after_finished_run_sets_ready(qtbot, bt, command):
    root, nodes = build(bt, "Sequence", ["Succeed", "Raise", "Succeed"])
    run(qtbot, bt)
    assert shown([root, *nodes]) == [FAILED, SUCCEEDED, FAILED, READY]
    states = record(bt.executionStateChanged)
    getattr(bt, command)()
    assert shown([root, *nodes]) == [READY] * 4
    assert all(node.GetError() is None for node in [root, *nodes])
    assert status_label(nodes[1]).toolTip() == ""
    assert bt.GetExecutionState() == "Idle"
    assert states == []


@pytest.mark.parametrize("interrupt", ["reset", "stop-then-execute"])
def test_interrupted_threaded_on_run_keeps_seeing_cancel_request(qtbot, bt, gate_state, interrupt):
    """CancelRequested() must stay True for the interrupted OnRun call even after the node restarts."""
    _, (node,) = build(bt, "Sequence", [Gated])
    node.state = gate_state
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: len(gate_state.started) == 1)
    if interrupt == "reset":
        bt.Reset()
    else:
        bt.Stop()
        bt.Execute()
    # A node never runs two OnRun calls at once: the restarted node waits (Running)
    # until the interrupted call has returned.
    qtbot.wait(60)
    assert len(gate_state.started) == 1
    assert node.GetStatus() is NodeStatus.RUNNING
    gate_state.gate.set()
    qtbot.waitUntil(lambda: len(gate_state.observed) == 2)
    bt.Stop()
    assert gate_state.observed[0] is True, "the interrupted OnRun call must see CancelRequested()"
    assert gate_state.observed[1] is False, "the new OnRun call must not be cancelled"


# ============================================================================ signals / buttons / locking
def test_execution_state_signal_sequence(qtbot, bt):
    build(bt, "Sequence", [ForeverRunning])
    states = record(bt.executionStateChanged)
    fast_config(bt)
    bt.Execute()
    assert bt.GetExecutionState() == "Running"
    bt.Pause()
    assert bt.GetExecutionState() == "Paused"
    bt.Execute()
    bt.Stop()
    assert states == ["Running", "Paused", "Running", "Idle"]


def test_execution_state_signal_for_immediate_completion(qtbot, bt):
    build(bt, "Sequence", ["Succeed"])
    states = record(bt.executionStateChanged)
    run(qtbot, bt)
    assert states == ["Running", "Idle"]


@pytest.mark.parametrize(
    "root_type, leaf, expected",
    [
        ("Sequence", "Succeed", SUCCEEDED),
        ("Sequence", "FailNode", FAILED),
        ("Selector", "FailNode", FAILED),
        ("Sequence", "ThreadedSucceed", SUCCEEDED),
        ("Sequence", "Raise", FAILED),
    ],
)
def test_execution_finished_signal(qtbot, bt, root_type, leaf, expected):
    build(bt, root_type, [leaf])
    fast_config(bt)
    with qtbot.waitSignal(bt.executionFinished, timeout=5000) as blocker:
        bt.Execute()
    assert blocker.args == [expected]
    run_until_idle(qtbot, bt)


def test_execution_finished_emitted_once(qtbot, bt):
    build(bt, "Sequence", ["RunningN"])
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    qtbot.wait(30)
    assert finished == [SUCCEEDED]


def button_states(bt) -> dict[str, bool]:
    return {name: bt.button(name).isEnabled() for name in ("Execute", "Pause", "Stop")}


IDLE_BUTTONS = {"Execute": True, "Pause": False, "Stop": False}
RUNNING_BUTTONS = {"Execute": False, "Pause": True, "Stop": True}
PAUSED_BUTTONS = {"Execute": True, "Pause": False, "Stop": True}


def test_buttons_follow_execution_state(qtbot, bt):
    build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    assert button_states(bt) == IDLE_BUTTONS
    bt.Execute()
    assert button_states(bt) == RUNNING_BUTTONS
    bt.Pause()
    assert button_states(bt) == PAUSED_BUTTONS
    bt.Execute()
    assert button_states(bt) == RUNNING_BUTTONS
    bt.Stop()
    assert button_states(bt) == IDLE_BUTTONS


def test_buttons_after_run_completes(qtbot, bt):
    build(bt, "Sequence", ["RunningN"])
    seen = []
    bt.executor().ticked.connect(lambda _n: seen.append(button_states(bt)))
    run(qtbot, bt)
    assert seen[0] == RUNNING_BUTTONS
    assert button_states(bt) == IDLE_BUTTONS


def test_execution_buttons_disabled_without_a_tree(qtbot, make_widget):
    widget = make_widget()
    assert button_states(widget) == {"Execute": False, "Pause": False, "Stop": False}


def test_buttons_drive_execution(qtbot, bt):
    root, (forever,) = build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    bt.button("Execute").click()
    assert bt.GetExecutionState() == "Running"
    bt.button("Pause").click()
    assert bt.GetExecutionState() == "Paused"
    bt.button("Execute").click()
    assert bt.GetExecutionState() == "Running"
    bt.button("Reset").click()
    assert shown([root, forever]) == [READY, READY]
    assert bt.GetExecutionState() == "Running"
    bt.button("Stop").click()
    assert bt.GetExecutionState() == "Idle"
    assert shown([root, forever]) == [READY, READY]


def test_structure_locked_only_while_executing(qtbot, bt):
    build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    assert not bt.view().is_locked()
    bt.Execute()
    assert bt.view().is_locked()
    bt.Pause()
    assert bt.view().is_locked()
    bt.Execute()
    assert bt.view().is_locked()
    bt.Stop()
    assert not bt.view().is_locked()


def test_structure_unlocked_after_run_completes(qtbot, bt):
    build(bt, "Sequence", ["RunningN"])
    locked_during = []
    bt.executor().ticked.connect(lambda _n: locked_during.append(bt.view().is_locked()))
    run(qtbot, bt)
    assert locked_during[0] is True
    assert not bt.view().is_locked()


# ============================================================================ configuration
def test_tick_interval_change_while_running_updates_timer(qtbot, bt):
    build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: ticks(bt) >= 3)
    timer = bt.executor()._timer
    fast_config(bt, tick_interval_ms=60_000)
    assert timer.interval() == 60_000
    assert timer.isActive()
    slow_count = ticks(bt)
    qtbot.wait(200)
    assert ticks(bt) == slow_count
    fast_config(bt, tick_interval_ms=5)
    assert timer.interval() == 5
    qtbot.waitUntil(lambda: ticks(bt) >= slow_count + 5, timeout=3000)
    bt.Stop()


def test_tick_interval_is_used_when_execution_starts(qtbot, bt):
    build(bt, "Sequence", [ForeverRunning])
    fast_config(bt, tick_interval_ms=60_000)
    bt.Execute()
    assert ticks(bt) == 1  # first tick happens at once
    qtbot.wait(150)
    assert ticks(bt) == 1
    bt.Stop()


def test_repeat_mode_keeps_ticking_and_restarts_rounds(qtbot, bt):
    _, (increment, recorder) = build(bt, "Sequence", ["IncrementCounter", "Recorder"])
    finished = record(bt.executionFinished)
    fast_config(bt, repeat=True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.HasEntry("counter") and bt.GetEntry("counter") >= 3)
    assert bt.IsExecuting()
    assert bt.GetExecutionState() == "Running"
    rounds = recorder.calls.count(("run",))
    assert rounds >= 3
    assert recorder.calls == [("start",), ("run",), ("terminate", SUCCEEDED)] * rounds
    assert finished == []
    bt.Stop()
    value = bt.GetEntry("counter")
    qtbot.wait(50)
    assert bt.GetEntry("counter") == value
    assert not bt.IsExecuting()


def test_repeat_mode_keeps_ticking_after_failure(qtbot, bt):
    root, (fail,) = build(bt, "Sequence", [CountingFail])
    finished = record(bt.executionFinished)
    fast_config(bt, repeat=True)
    bt.Execute()
    qtbot.waitUntil(lambda: fail.runs >= 4)
    assert bt.GetExecutionState() == "Running"
    assert shown([root, fail]) == [FAILED, FAILED]
    assert finished == []
    bt.Stop()


def test_disabling_repeat_while_running_finishes_after_current_round(qtbot, bt):
    build(bt, "Sequence", ["IncrementCounter", "RunningN"])
    finished = record(bt.executionFinished)
    fast_config(bt, repeat=True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.HasEntry("counter") and bt.GetEntry("counter") >= 2)
    fast_config(bt, repeat=False)
    run_until_idle(qtbot, bt)
    assert finished == [SUCCEEDED]


def test_restore_blackboard_on_stop(qtbot, bt):
    bt.SetEntry(10, "counter")
    build(bt, "Sequence", ["IncrementCounter", ForeverRunning])
    fast_config(bt, restore_blackboard=True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetEntry("counter") == 11)
    bt.Stop()
    assert bt.GetEntry("counter") == 10


def test_restore_blackboard_removes_entries_created_during_the_run(qtbot, bt):
    bt.SetEntry(10, "counter")
    build(bt, "Sequence", ["LogMessage", ForeverRunning])
    fast_config(bt, restore_blackboard=True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.HasEntry("last_message"))
    bt.Stop()
    assert not bt.HasEntry("last_message")
    assert bt.GetEntryNames() == ["counter"]


def test_restore_blackboard_on_reset(qtbot, bt):
    bt.SetEntry(10, "counter")
    build(bt, "Sequence", ["IncrementCounter", ForeverRunning])
    fast_config(bt, restore_blackboard=True)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetEntry("counter") == 11)
    bt.Pause()
    bt.Reset()
    assert bt.GetEntry("counter") == 10
    assert bt.GetExecutionState() == "Paused"
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetEntry("counter") == 11)
    bt.Stop()
    assert bt.GetEntry("counter") == 10  # the snapshot taken at Execute survives Reset


def test_restore_blackboard_on_reset_after_finished_run(qtbot, bt):
    """Reset restores the values captured when execution started, also after the run completed."""
    bt.SetEntry(10, "counter")
    build(bt, "Sequence", ["IncrementCounter"])
    run(qtbot, bt, restore_blackboard=True)
    assert bt.GetEntry("counter") == 11
    bt.Reset()
    assert bt.GetEntry("counter") == 10


@pytest.mark.parametrize("node_class", [BlackboardWriter, ThreadedBlackboardWriter], ids=["gui-thread", "worker-thread"])
def test_blackboard_writes_by_running_nodes_do_not_mark_tree_modified(qtbot, bt, node_class):
    bt.SetEntry(1, "x")
    build(bt, "Sequence", [node_class])
    fast_config(bt)
    assert bt.SaveTree(bt.GetFilePath())
    assert not bt.IsModified()
    bt.Execute()
    run_until_idle(qtbot, bt)
    qtbot.waitUntil(lambda: bt.blackboardView().row("made_by_run") is not None)
    assert bt.GetEntry("x") == 2
    assert not bt.IsModified()


def test_blackboard_kept_without_restore_option(qtbot, bt):
    bt.SetEntry(10, "counter")
    build(bt, "Sequence", ["IncrementCounter", ForeverRunning])
    fast_config(bt, restore_blackboard=False)
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetEntry("counter") == 11)
    bt.Pause()
    bt.Reset()
    assert bt.GetEntry("counter") == 11
    bt.Stop()
    assert bt.GetEntry("counter") == 11


# ============================================================================ re-execution
def test_execute_again_uses_the_edited_graph(qtbot, bt):
    root, (fail,) = build(bt, "Sequence", ["FailNode"])
    run(qtbot, bt)
    assert shown([root, fail]) == [FAILED, FAILED]

    bt.Disconnect(fail)
    succeed = bt.AddNode("Succeed", 400, 300)
    bt.Connect(root, succeed)
    run(qtbot, bt)
    assert shown([root, succeed, fail]) == [SUCCEEDED, SUCCEEDED, READY]

    bt.RemoveNode(succeed)
    running = bt.AddNode("RunningN", 800, 300)
    running.SetField("ticks", 4)
    bt.Connect(root, running)
    run(qtbot, bt)
    assert shown([root, running, fail]) == [SUCCEEDED, SUCCEEDED, READY]
    assert ticks(bt) == 5


def test_execute_again_uses_changed_composite_type(qtbot, bt):
    root, nodes = build(bt, "Sequence", ["FailNode", "Succeed"])
    run(qtbot, bt)
    assert shown([root, *nodes]) == [FAILED, FAILED, READY]
    root.SetCompositeType("Selector")
    run(qtbot, bt)
    assert shown([root, *nodes]) == [SUCCEEDED, FAILED, SUCCEEDED]


def test_execute_again_uses_new_child_order(qtbot, bt):
    root = bt.GetRootNode()
    sink: list[str] = []
    nodes = {}
    for name, x in (("a", 0.0), ("b", 400.0)):
        node = bt.AddNode(OrderLog, x, 300.0)
        node.SetField("name", name)
        node.sink = sink
        bt.Connect(root, node)
        nodes[name] = node
    run(qtbot, bt)
    assert sink == ["a", "b"]
    nodes["a"]._item.setPos(800.0, 300.0)
    sink.clear()
    run(qtbot, bt)
    assert sink == ["b", "a"]


def test_execute_after_stop_rebuilds(qtbot, bt):
    root, (forever,) = build(bt, "Sequence", [ForeverRunning])
    fast_config(bt)
    bt.Execute()
    bt.Stop()
    bt.Disconnect(forever)
    succeed = bt.AddNode("Succeed", 400, 300)
    bt.Connect(root, succeed)
    run(qtbot, bt)
    assert shown([root, succeed, forever]) == [SUCCEEDED, SUCCEEDED, READY]
    assert forever.calls.count(("start",)) == 1


# ============================================================================ result_to_status
@pytest.mark.parametrize(
    "result, expected",
    [
        (True, Status.SUCCESS),
        (False, Status.FAILURE),
        (Status.SUCCESS, Status.SUCCESS),
        (Status.FAILURE, Status.FAILURE),
        (Status.RUNNING, Status.RUNNING),
    ],
    ids=["True", "False", "SUCCESS", "FAILURE", "RUNNING"],
)
def test_result_to_status_accepts_documented_values(result, expected):
    assert result_to_status(result) == (expected, None)


@pytest.mark.parametrize(
    "result, fragments",
    [
        (None, ["NoneType", "missing return"]),
        (Status.INVALID, ["INVALID"]),
        (1, ["int"]),
        (0, ["int"]),
        (1.0, ["float"]),
        ("SUCCESS", ["str"]),
        ([], ["list"]),
        (object(), ["object"]),
    ],
    ids=["None", "INVALID", "1", "0", "float", "str", "list", "object"],
)
def test_result_to_status_rejects_other_values(result, fragments):
    status, message = result_to_status(result)
    assert status is Status.FAILURE
    assert message
    for fragment in fragments:
        assert fragment in message


def test_result_to_status_message_names_valid_return_values():
    _, message = result_to_status("oops")
    assert "True" in message and "False" in message and "Status" in message


# ============================================================================ run_in_daemon_thread
def test_run_in_daemon_thread_returns_result_from_named_daemon_thread():
    info = {}

    def work():
        thread = threading.current_thread()
        info.update(name=thread.name, daemon=thread.daemon, ident=threading.get_ident())
        return 42

    future = run_in_daemon_thread(work, "bt-test-worker")
    assert isinstance(future, Future)
    assert future.result(timeout=5) == 42
    assert info["name"] == "bt-test-worker"
    assert info["daemon"] is True
    assert info["ident"] != threading.get_ident()


def test_run_in_daemon_thread_does_not_block_the_caller():
    gate = threading.Event()
    future = run_in_daemon_thread(lambda: gate.wait(5) and "done", "bt-test-blocked")
    assert not future.done()
    gate.set()
    assert future.result(timeout=5) == "done"


@pytest.mark.parametrize("error", [ValueError("bad"), SystemExit(3), KeyboardInterrupt()], ids=lambda e: type(e).__name__)
def test_run_in_daemon_thread_reports_exceptions_through_the_future(error):
    def work():
        raise error

    future = run_in_daemon_thread(work, "bt-test-raises")
    assert future.exception(timeout=5) is error
    assert future.done()


def test_run_in_daemon_thread_starts_a_new_thread_per_call():
    threads = []
    futures = [run_in_daemon_thread(lambda: threads.append(threading.current_thread()), f"bt-{i}") for i in range(3)]
    for future in futures:
        future.result(timeout=5)
    assert sorted(thread.name for thread in threads) == ["bt-0", "bt-1", "bt-2"]
    assert len({id(thread) for thread in threads}) == 3
