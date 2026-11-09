"""Opt-in performance tests: ``pytest -m perf`` (deselected by default; run them without -n).

Each test times one of the cases measured while 0.9.0 was optimised. The limits are about three
times what the case takes on the development machine (never below 50 ms), so the tests catch the
regressions those optimisations removed (work that grows with entries x nodes, whole-viewport
repaints, hit tests calling Python for every line, a thread per OnRun call), not machine noise.
Each limit's comment gives the measured value and, where known, the value before the optimisation.
"""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QWidget

from behavior_tree_widget import LeafNodeWidget, LogLevel, NodeStatus, Status, flush_logs

from conftest import AllFields, Succeed

pytestmark = pytest.mark.perf


def elapsed(function, repeat: int = 1) -> float:
    """Seconds the fastest of ``repeat`` calls of ``function`` took."""
    best = None
    for _ in range(repeat):
        start = time.perf_counter()
        function()
        took = time.perf_counter() - start
        best = took if best is None else min(best, took)
    return best


def assert_within(took: float, limit: float, what: str) -> None:
    print(f"\n{what}: {took * 1000:.1f} ms (limit {limit * 1000:.0f} ms)")
    assert took <= limit, f"{what} took {took * 1000:.1f} ms, more than {limit * 1000:.0f} ms"


class Forever(LeafNodeWidget):
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return Status.RUNNING


class QuickThreaded(LeafNodeWidget):
    """Succeeds at once on a worker thread and records the thread it ran on."""

    threads: set[int] = set()

    def OnRun(self, tree):
        type(self).threads.add(threading.get_ident())
        return True


def fan(bt, count: int = 500):
    """Root -> Sequence -> ``count`` Succeed leaves in a row: one parent with very many children."""
    sequence = bt.AddNode("Sequence", 0, 200)
    bt.Connect(bt.GetRootNode(), sequence)
    leaves = []
    for index in range(count):
        leaf = bt.AddNode("Succeed", (index - count / 2) * 220, 400)
        bt.Connect(sequence, leaf)
        leaves.append(leaf)
    QApplication.processEvents()
    return sequence, leaves


def blackboard_tree(widget, nodes: int, entries: int) -> None:
    for index in range(entries):
        widget.AddEntry(f"e{index}", "Integer", index)
    root = widget.GetRootNode()
    for index in range(nodes):
        node = widget.AddNode("Evaluation", (index - nodes / 2) * 230, 300)
        node.SetValueName(f"e{index % entries}")
        widget.Connect(root, node)


# ---------------------------------------------------------------------- nodes listing blackboard entries
def test_loading_many_nodes_that_list_the_blackboard(make_widget, tmp_path):
    source = make_widget()
    path = str(tmp_path / "evaluations_BT.json")
    assert source.NewTree(path)
    blackboard_tree(source, nodes=100, entries=50)
    assert source.SaveTree(path)
    target = make_widget()
    took = elapsed(lambda: target.LoadTree(path))
    took += elapsed(lambda: target.LoadTree(path))  # again: the nodes being replaced must not refresh
    # measured 1.2 s for both loads; 9.7 s for one reload before 0.9.0 (every node refreshed per entry)
    assert_within(took, 3.7, "loading 100 Evaluation nodes and 50 entries twice")


def test_adding_entries_while_many_nodes_list_the_blackboard(bt):
    blackboard_tree(bt, nodes=100, entries=50)
    took = elapsed(lambda: [bt.AddEntry(f"new{index}", "Integer", index) for index in range(10)])
    # measured 0.14 s; before 0.9.0 each node refilled both combo boxes for every entry (about 5 s)
    assert_within(took, 0.42, "10 AddEntry calls with 100 Evaluation nodes and 50 entries")


# ---------------------------------------------------------------------- the view
def test_hit_testing_in_a_fan_of_500_lines(bt):
    fan(bt)
    view = bt.view()
    points = [view.mapFromScene(QPointF(x * 3.0, 300.0)) for x in range(200)]
    took = elapsed(lambda: [view.hit_test(point) for point in points], repeat=3)
    # measured 0.12 s; 2.4 s before 0.9.0 (Python shape() and boundingRect() for every line)
    assert_within(took, 0.35, "200 hit tests in a fan of 500 connection lines")


def test_repainting_after_one_status_change_among_500_nodes(bt):
    _sequence, leaves = fan(bt)
    leaf = leaves[250]
    app = QApplication.instance()

    def change_and_paint() -> None:
        for _ in range(5):
            leaf._set_status(NodeStatus.RUNNING if leaf.GetStatus() is NodeStatus.READY else NodeStatus.READY)
            app.sendPostedEvents()
            app.processEvents()

    took = elapsed(change_and_paint, repeat=3)
    # measured 18 ms; 1.1 s before 0.9.0 (the whole viewport was repainted)
    assert_within(took, 0.055, "5 status changes and repaints with 500 nodes")


def test_children_order_of_a_parent_with_500_children(bt):
    sequence, _leaves = fan(bt)
    took = elapsed(lambda: [sequence.GetChildren() for _ in range(200)], repeat=3)
    # measured 0.5 ms; 0.17 s before 0.9.0 (sorted by scene geometry on every call)
    assert_within(took, 0.05, "200 GetChildren calls with 500 children")


# ---------------------------------------------------------------------- execution
def test_ticks_with_one_running_leaf_among_500(bt):
    root = bt.GetRootNode()  # a Sequence with memory: later ticks only tick the running leaf
    for index in range(499):
        bt.Connect(root, bt.AddNode("Succeed", (index - 250) * 220, 300))
    bt.Connect(root, bt.AddNode(Forever, 60_000, 300))
    config = bt.GetConfig()
    config.tick_interval_ms = 60_000  # ticked by the test only
    bt.SetConfig(config)
    bt.Execute()  # the first tick runs every leaf
    executor = bt.executor()
    took = elapsed(lambda: [executor._tick() for _ in range(500)])
    bt.Stop()
    # measured 64 ms; about 0.17 s before 0.9.0 (every node was refreshed every tick). The limit guards
    # against per-tick work growing with the whole tree again, not against that 2.6x.
    assert_within(took, 0.2, "500 ticks with one running leaf among 500")


def test_quick_threaded_leaves_reuse_worker_threads(bt, qtbot):
    QuickThreaded.threads = set()
    root = bt.GetRootNode()
    for index in range(200):
        bt.Connect(root, bt.AddNode(QuickThreaded, (index - 100) * 220, 300))
    config = bt.GetConfig()
    config.tick_interval_ms = 1
    bt.SetConfig(config)

    def run() -> None:
        bt.Execute()
        qtbot.waitUntil(lambda: not bt.IsExecuting(), timeout=10_000)

    took = elapsed(run)
    # measured 1 thread and 38 ms; before 0.9.0 every call started a thread of its own (200 here)
    assert len(QuickThreaded.threads) <= 10, f"{len(QuickThreaded.threads)} threads ran 200 quick OnRun calls"
    assert_within(took, 0.12, "a Sequence of 200 quick worker-thread leaves")


# ---------------------------------------------------------------------- nodes and the log
@pytest.mark.parametrize("cls, limit", [(Succeed, 0.42), (AllFields, 1.15)], ids=["no-fields", "five-fields"])
def test_node_construction(qtbot, cls, limit):
    parent = QWidget()
    qtbot.addWidget(parent)
    took = elapsed(lambda: [cls(parent) for _ in range(200)])
    # measured 0.14 s (no fields) and 0.38 s (five fields); 0.34 s and 0.67 s before 0.9.0
    assert_within(took, limit, f"200 {cls.__name__} nodes")


def test_logging_never_waits_for_the_file(bt):
    took = elapsed(lambda: [bt.Log(LogLevel.INFO, f"message {index}") for index in range(10_000)])
    flush_logs()
    # measured 45 to 84 ms: Log only queues the message; writing it to the file would cost far more
    assert_within(took, 0.25, "10000 Log calls")
