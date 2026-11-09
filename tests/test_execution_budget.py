"""How long one tick waits for the worker-thread OnRun calls it starts (TreeExecutor.TICK_WAIT_BUDGET)."""

from __future__ import annotations

import time

import pytest

from behavior_tree_widget import LeafNodeWidget, NodeStatus
from behavior_tree_widget.execution import TreeExecutor


class Sleepy(LeafNodeWidget):
    """Worker-thread leaf sleeping "seconds"; the tick may wait up to 1 s for it."""

    _title = "Sleepy"
    _fields = {"seconds": 0.12}
    THREAD_WAIT = 1.0

    def OnRun(self, tree):
        time.sleep(self.GetField("seconds"))
        return True


class Instant(LeafNodeWidget):
    """Quick worker-thread leaf; the tick may wait up to 5 s for it."""

    _title = "Instant"
    THREAD_WAIT = 5.0

    def OnRun(self, tree):
        return True


@pytest.fixture
def tree(bt):
    bt.RegisterNodeType(Sleepy)
    bt.RegisterNodeType(Instant)
    config = bt.GetConfig()
    config.tick_interval_ms = 60000  # the test ticks by hand
    bt.SetConfig(config)
    return bt


def leaves(bt, *types):
    nodes = [bt.AddNode(name, index * 220.0, 200.0) for index, name in enumerate(types)]
    for node in nodes:
        bt.Connect(bt.GetRootNode(), node)
    return nodes


def statuses(nodes) -> list[str]:
    return [node.GetStatus().value for node in nodes]


def tick(bt) -> float:
    start = time.perf_counter()
    bt.executor()._tick()
    return time.perf_counter() - start


def returned(node) -> None:
    """Wait until the node's worker-thread OnRun call has returned."""
    node._worker_future.result(timeout=5)


def test_the_default_budget():
    assert pytest.approx(0.1) == TreeExecutor.TICK_WAIT_BUDGET


def test_quick_calls_finish_within_the_tick_that_starts_them(tree):
    nodes = leaves(tree, "Instant", "Instant", "Instant")
    tree.Execute()  # the first tick runs at once
    assert statuses(nodes) == ["Succeeded"] * 3
    assert tree.executor().tick_count() == 1


def test_calls_started_after_the_budget_is_spent_are_collected_on_a_later_tick(tree):
    slow, quick = leaves(tree, "Sleepy", "Instant")
    # Well over the budget: the tick measures its wait from when it starts waiting, which can be
    # a little after the call started when the machine is busy.
    slow.SetField("seconds", 0.3)
    tree.Execute()
    # The tick waited for the slow call (longer than the budget), so it does not wait for the
    # quick one it starts next: that one shows Running although it returns at once.
    assert statuses([slow, quick]) == ["Succeeded", "Running"]
    assert tree.GetExecutionState() == "Running"
    returned(quick)
    tick(tree)
    assert statuses([slow, quick]) == ["Succeeded", "Succeeded"]
    assert tree.GetExecutionState() == "Idle"


def test_the_budget_bounds_how_long_a_tick_keeps_the_gui_waiting(tree, monkeypatch):
    nodes = leaves(tree, *["Sleepy"] * 8)
    for node in nodes:
        node.SetField("seconds", 0.06)
    durations = []
    original = TreeExecutor._tick_tree

    def timed(self):
        start = time.perf_counter()
        try:
            return original(self)
        finally:
            durations.append(time.perf_counter() - start)

    monkeypatch.setattr(TreeExecutor, "_tick_tree", timed)
    tree.Execute()
    deadline = time.monotonic() + 20
    while tree.IsExecuting():
        assert time.monotonic() < deadline
        time.sleep(0.01)
        tick(tree)
    assert statuses(nodes) == ["Succeeded"] * 8
    # Waiting all calls out would take 8 x 0.06 s in one tick; after the first call a tick waits
    # only for what is left of TICK_WAIT_BUDGET (0.1 s).
    assert max(durations) < 0.1 + 0.25, durations
    assert len(durations) > 2


def test_the_first_call_a_tick_waits_for_gets_its_whole_thread_wait(tree):
    (slow,) = leaves(tree, "Sleepy")
    slow.SetField("seconds", 0.3)  # three times the budget, well within its THREAD_WAIT (1 s)
    tree.Execute()
    assert statuses([slow]) == ["Succeeded"]
    assert tree.executor().tick_count() == 1


def test_later_calls_wait_only_for_what_is_left_of_the_budget(tree):
    quick, slow = leaves(tree, "Instant", "Sleepy")
    slow.SetField("seconds", 0.6)  # within its THREAD_WAIT (1 s), far beyond what the budget leaves
    start = time.perf_counter()
    tree.Execute()  # the first tick runs at once
    elapsed = time.perf_counter() - start
    assert statuses([quick, slow]) == ["Succeeded", "Running"]
    assert elapsed < 0.45, f"the first tick kept the GUI waiting {elapsed:.3f} s"
    returned(slow)
    tick(tree)
    assert statuses([quick, slow]) == ["Succeeded", "Succeeded"]


def test_a_budget_of_zero_never_waits(tree):
    tree.executor().TICK_WAIT_BUDGET = 0.0
    first, second = leaves(tree, "Instant", "Instant")
    tree.Execute()
    assert statuses([first, second]) == ["Running", "Ready"]
    returned(first)
    tick(tree)  # collects the first, starts the second without waiting for it
    assert statuses([first, second]) == ["Succeeded", "Running"]
    returned(second)
    tick(tree)
    assert statuses([first, second]) == ["Succeeded", "Succeeded"]


def test_a_large_budget_waits_for_every_call(tree):
    tree.executor().TICK_WAIT_BUDGET = 10.0
    nodes = leaves(tree, "Sleepy", "Sleepy", "Instant")
    tree.Execute()
    assert statuses(nodes) == ["Succeeded"] * 3
    assert tree.executor().tick_count() == 1


def test_every_tick_has_its_own_budget(tree):
    first, second, third = leaves(tree, "Sleepy", "Sleepy", "Instant")
    tree.Execute()
    assert statuses([first, second, third]) == ["Succeeded", "Running", "Ready"]
    returned(second)
    tick(tree)  # collects the second; the third starts in a tick that has not waited yet
    assert statuses([first, second, third]) == ["Succeeded", "Succeeded", "Succeeded"]
    assert NodeStatus.SUCCEEDED is tree.GetRootNode().GetStatus()
