"""The worker threads running OnRun calls (behavior_tree_widget._workers.WorkerPool)."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from behavior_tree_widget import LeafNodeWidget, NodeStatus
from behavior_tree_widget._workers import WorkerPool, leaf_pool

from conftest import fast_config, run_until_idle

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__import__("behavior_tree_widget").__file__)))


def wait_for(predicate, timeout=5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.002)


def test_sequential_calls_reuse_one_worker():
    pool = WorkerPool()
    threads = []
    for index in range(20):
        future = pool.submit(lambda: threads.append(threading.current_thread()), f"bt-call-{index}")
        future.result(timeout=5)
        wait_for(lambda: pool.idle_count() == 1)  # back to waiting before the next call
    assert pool.started_count() == 1
    assert len({id(thread) for thread in threads}) == 1
    assert threads[0].daemon is True


def test_a_busy_worker_never_delays_another_call():
    pool = WorkerPool()
    gate = threading.Event()
    blocked = pool.submit(lambda: gate.wait(10) and "released", "bt-blocked")
    quick = pool.submit(lambda: "quick", "bt-quick")
    assert quick.result(timeout=5) == "quick"  # a second worker took it
    assert not blocked.done()
    assert pool.started_count() == 2
    gate.set()
    assert blocked.result(timeout=5) == "released"


def test_a_call_that_never_returns_occupies_one_worker_only():
    pool = WorkerPool()
    stuck = threading.Event()
    pool.submit(stuck.wait, "bt-stuck")  # never released
    for index in range(10):
        assert pool.submit(lambda index=index: index, f"bt-{index}").result(timeout=5) == index
        wait_for(lambda: pool.idle_count() == 1)
    assert pool.started_count() == 2  # the stuck one and one reused for the rest


def test_the_worker_carries_the_name_of_the_call_it_runs():
    pool = WorkerPool()
    names = []
    for name in ("behavior_tree_widget:Move To", "behavior_tree_widget:Wait"):
        pool.submit(lambda: names.append(threading.current_thread().name), name).result(timeout=5)
        wait_for(lambda: pool.idle_count() == 1)
    assert names == ["behavior_tree_widget:Move To", "behavior_tree_widget:Wait"]
    worker = next(thread for thread in threading.enumerate() if thread.name == WorkerPool.IDLE_NAME)
    assert worker.daemon


@pytest.mark.parametrize(
    "error", [ValueError("bad"), SystemExit(3), KeyboardInterrupt()], ids=lambda error: type(error).__name__
)
def test_exceptions_are_reported_and_the_worker_survives(error):
    pool = WorkerPool()

    def work():
        raise error

    assert pool.submit(work, "bt-raises").exception(timeout=5) is error
    wait_for(lambda: pool.idle_count() == 1)
    assert pool.submit(lambda: "after", "bt-after").result(timeout=5) == "after"
    assert pool.started_count() == 1


def test_idle_workers_end_after_the_idle_timeout():
    pool = WorkerPool(idle_timeout=0.05)
    gates = [threading.Event() for _ in range(3)]
    futures = [pool.submit(gate.wait, f"bt-{index}") for index, gate in enumerate(gates)]
    assert pool.worker_count() == 3
    for gate in gates:
        gate.set()
    for future in futures:
        future.result(timeout=5)
    wait_for(lambda: pool.worker_count() == 0)
    assert pool.idle_count() == 0
    assert pool.submit(lambda: "again", "bt-again").result(timeout=5) == "again"  # a new worker starts


@pytest.mark.parametrize("attempt", range(3))
def test_no_call_is_lost_when_workers_end_while_calls_arrive(attempt):
    """Calls submitted from several threads while workers keep timing out: every call runs once,
    and afterwards every worker ends. (With queue.SimpleQueue, whose get(timeout=...) can wait
    forever before Python 3.13, some workers never ended in about one attempt in seven.)"""
    pool = WorkerPool(idle_timeout=0.001)
    results = []
    lock = threading.Lock()

    def submitter(seed: int) -> None:
        rng = random.Random(seed + 100 * attempt)
        futures = []
        for index in range(60):
            value = (seed, index)
            delay = rng.choice([0.0, 0.0, 0.0005, 0.002])

            def work(value=value, delay=delay):
                if delay:
                    time.sleep(delay)
                with lock:
                    results.append(value)
                return value

            futures.append((value, pool.submit(work, f"bt-{seed}-{index}")))
            if rng.random() < 0.3:
                time.sleep(rng.choice([0.0005, 0.002]))
        for value, future in futures:
            assert future.result(timeout=10) == value

    threads = [threading.Thread(target=submitter, args=(seed,)) for seed in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(results) == sorted((seed, index) for seed in range(6) for index in range(60))
    wait_for(lambda: pool.worker_count() == 0)


def test_a_worker_that_cannot_start_leaves_nothing_behind(monkeypatch):
    pool = WorkerPool()
    ran = []

    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    with pytest.raises(RuntimeError):
        pool.submit(lambda: ran.append("early"), "bt-refused")
    monkeypatch.undo()
    assert pool.worker_count() == 0 and pool.started_count() == 0
    assert pool.submit(lambda: ran.append("later") or "ok", "bt-later").result(timeout=5) == "ok"
    time.sleep(0.05)
    assert ran == ["later"], "the refused call must never run"


def test_a_stuck_call_does_not_keep_the_application_from_exiting():
    script = textwrap.dedent(
        f"""
        import sys, threading
        sys.path.insert(0, {SRC!r})
        from behavior_tree_widget._workers import WorkerPool
        pool = WorkerPool()
        pool.submit(threading.Event().wait, "bt-stuck")
        pool.submit(lambda: None, "bt-quick").result(timeout=5)
        print("done", flush=True)
        """
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "done"


class Hold(LeafNodeWidget):
    """Worker-thread leaf that runs until ``Hold.gate`` is set, whether it is cancelled or not."""

    _title = "Hold"
    gate = threading.Event()

    def OnRun(self, tree):
        Hold.gate.wait(30)
        return True


class Quick(LeafNodeWidget):
    _title = "Quick"
    THREAD_WAIT = 5.0

    def OnRun(self, tree):
        return True


def test_leaf_calls_run_on_the_shared_pool(qtbot, bt):
    bt.RegisterNodeType(Quick)
    root = bt.GetRootNode()
    for index in range(5):
        bt.Connect(root, bt.AddNode("Quick", index * 220.0, 200.0))
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    started = leaf_pool().started_count()
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert leaf_pool().started_count() == started, "the second run reuses the workers of the first"
    assert all(node.GetStatus() is NodeStatus.SUCCEEDED for node in bt.GetNodes())


def test_an_interrupted_call_holds_a_worker_while_the_tree_goes_on(qtbot, bt):
    """Stop interrupts a call that keeps running (it ignores CancelRequested for a while); a new
    run of another node is not delayed by it."""
    bt.RegisterNodeType(Hold)
    bt.RegisterNodeType(Quick)
    Hold.gate.clear()
    hold = bt.AddNode("Hold", 0.0, 200.0)
    bt.Connect(bt.GetRootNode(), hold)
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(lambda: hold.GetStatus() is NodeStatus.RUNNING, timeout=3000)
    bt.Stop()
    quick = bt.AddNode("Quick", 300.0, 200.0)
    bt.Disconnect(hold)
    bt.Connect(bt.GetRootNode(), quick)
    finished = []
    bt.executionFinished.connect(finished.append)
    try:
        bt.Execute()
        run_until_idle(qtbot, bt)
        assert finished == ["Succeeded"]
        assert not hold._worker_future.done(), "the interrupted call is still running"
    finally:
        Hold.gate.set()
