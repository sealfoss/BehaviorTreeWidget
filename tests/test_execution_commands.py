"""Execution commands (Execute, Pause, Stop, Reset) issued from nodes, worker threads, signal handlers and
dialogs: when they take effect and in which order; chained runs; run generations; Restore blackboard values."""

from __future__ import annotations

import os
import threading

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest

from behavior_tree_widget import ExecutionCancelled, NodeStatus, execution

from conftest import fast_config, run_until_idle
from support.round3 import (
    FAILED,
    GUI_THREAD,
    READY,
    RUNNING,
    SUCCEEDED,
    TERMINATE_CASES,
    THREAD_WAITS,
    R3Probe,
    R3ThreadProbe,
    _assert_gui_thread_only,
    _worker_command_setup,
    assert_lifecycle,
    assert_timer_matches_state,
    audit,
    buttons,
    events,
    gate,
    next_tree_file,
    probes,
    qt_messages,
    record,
    rows_in_layout,
    save,
    shown,
    ticks,
    timer_active,
    timer_thread_warnings,
)
from support.round4 import GatedWorker, Quick, Writer, build
from support.round5 import Emitter, Endless
from support.round5 import Quick as R5Quick
from support.round6 import Endless as R6Endless
from support.round6 import Quick as R6Quick
from support.round6 import StopIssuer, in_thread
from support.round7 import Endless as R7Endless
from support.round7 import StopIssuer as R7StopIssuer
from support.round8 import TYPES, Commander, assert_no_more_runs, chained
from support.round8 import Endless as R8Endless
from support.round9 import Quick as R9Quick


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
    fresh = str(tmp_path / "fresh_BT.json")
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
        assert bt.blackboard_store().items() == [("y", "Integer", 3)]
    elif command == "NewTree":
        assert issued["result"] is True
        assert bt.GetFilePath() == os.path.abspath(fresh)
        assert bt.blackboard_store().items() == []
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
    expected = bt.blackboard_store().items()
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
    assert bt.blackboard_store().items() == expected
    view = bt.blackboard_view()
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


# ============================================================================ chained runs do not recurse
def test_execute_from_execution_finished_chains_without_recursion(qtbot, widget):
    """Restarting from executionFinished must return to the event loop between runs."""
    build(widget, Quick)
    fast_config(widget, tick_interval_ms=1)
    runs = {"count": 0, "max_depth": 0}
    target = 400

    def depth():
        import inspect

        return len(inspect.stack(0))

    def again(_result):
        runs["count"] += 1
        runs["max_depth"] = max(runs["max_depth"], depth())
        if runs["count"] < target:
            widget.Execute()

    widget.executionFinished.connect(again)
    widget.Execute()
    qtbot.waitUntil(lambda: runs["count"] >= target, timeout=20000)
    assert runs["max_depth"] < 200, "chained runs must not nest on the stack"
    run_until_idle(qtbot, widget)

def test_stop_issued_during_execute_runs_no_node_code(qtbot, widget):
    (quick,) = build(widget, Quick)
    finished = []
    widget.executionFinished.connect(finished.append)
    widget.executionStateChanged.connect(lambda state: widget.Stop() if state == "Running" else None)
    fast_config(widget)
    widget.Execute()
    qtbot.wait(30)
    assert widget.GetExecutionState() == "Idle"
    assert finished == []
    assert quick.GetStatus() is NodeStatus.READY


# ============================================================================ cancelled callers
@pytest.mark.parametrize("command", ["Stop", "Reset", "Execute", "Pause"])
def test_cancelled_worker_cannot_control_execution(qtbot, widget, command):
    (gated,) = build(widget, GatedWorker)
    calls = []

    def late_command(tree):
        calls.append(command)
        if len(calls) == 1:  # only the interrupted (first) call issues the command
            getattr(tree, command)()

    gated.after = late_command
    fast_config(widget)
    widget.Execute()
    assert gated.entered.wait(5)
    widget.Stop()  # cancels the running call
    widget.Execute()  # a new run starts (waiting for the old call to return)
    gated.gate.set()
    qtbot.waitUntil(lambda: len(calls) == 2, timeout=5000)  # the new run's own OnRun call
    qtbot.wait(30)
    assert len(gated.errors) == 1 and isinstance(gated.errors[0], ExecutionCancelled)
    assert widget.GetExecutionState() != "Paused", "the late Pause must be refused"
    # The new run was neither stopped, reset nor restarted by the late command: it finished normally.
    run_until_idle(qtbot, widget)
    assert widget.GetRootNode().GetStatus() is NodeStatus.SUCCEEDED
    widget.Stop()


# ============================================================================ blackboard snapshot semantics
def test_switching_restore_off_during_a_run_keeps_the_results(qtbot, widget):
    widget.AddEntry("x", "Integer", 0)
    build(widget, Writer, GatedWorker)
    fast_config(widget, restore_blackboard=True)
    widget.Execute()
    qtbot.waitUntil(lambda: widget.GetEntry("x") == 1)
    config = widget.GetConfig()
    config.restore_blackboard = False
    widget.SetConfig(config)
    widget.Stop()
    assert widget.GetEntry("x") == 1

def test_user_edit_after_a_run_is_not_undone_by_reset(qtbot, widget):
    widget.AddEntry("x", "Integer", 0)
    build(widget, Writer)
    fast_config(widget, restore_blackboard=True)
    widget.Execute()
    run_until_idle(qtbot, widget)
    assert widget.GetEntry("x") == 1
    row = widget.blackboard_view().row("x")
    row.value_widget.setValue(42)  # a deliberate edit in the Blackboard tab while idle
    widget.Reset()
    assert widget.GetEntry("x") == 42

def test_reset_after_a_run_still_restores_without_edits(qtbot, widget):
    widget.AddEntry("x", "Integer", 0)
    build(widget, Writer)
    fast_config(widget, restore_blackboard=True)
    widget.Execute()
    run_until_idle(qtbot, widget)
    widget.Reset()
    assert widget.GetEntry("x") == 0


# ============================================================================ queued worker commands
@pytest.mark.parametrize("command", ["Pause", "Stop", "Reset", "Execute"])
def test_worker_command_queued_before_a_restart_is_dropped(qtbot, widget, command):
    (emitter,) = build(widget, Emitter)
    emitter.command = command
    fast_config(widget)
    widget.Execute()
    assert emitter.issued.wait(5)
    # Before the queued command is delivered, the GUI restarts the tree.
    widget.Stop()
    widget.Execute()
    run_until_idle(qtbot, widget)  # the restarted run completes: the stale command had no effect
    assert widget.GetRootNode().GetStatus() is NodeStatus.SUCCEEDED
    assert emitter.calls == 2

def test_worker_command_is_carried_out_when_nothing_changed(qtbot, widget):
    (emitter,) = build(widget, Emitter)
    emitter.command = "Pause"
    fast_config(widget)
    widget.Execute()
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Paused", timeout=5000)
    widget.Stop()


# ============================================================================ first tick ordering
def test_harmless_command_during_execute_does_not_delay_the_first_tick(qtbot, widget):
    (quick,) = build(widget, R5Quick)
    config = widget.GetConfig()
    config.tick_interval_ms = 60_000  # a first tick that waited for the timer would never come
    widget.SetConfig(config)
    widget.executionStateChanged.connect(lambda state: widget.Execute() if state == "Running" else None)
    widget.Execute()
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Idle", timeout=2000)
    assert quick.GetStatus() is NodeStatus.SUCCEEDED

def test_reset_during_execute_still_ticks_at_once(qtbot, widget):
    (quick,) = build(widget, R5Quick)
    config = widget.GetConfig()
    config.tick_interval_ms = 60_000
    widget.SetConfig(config)
    fired = []

    def on_state(state):
        if state == "Running" and not fired:
            fired.append(True)
            widget.Reset()

    widget.executionStateChanged.connect(on_state)
    widget.Execute()
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Idle", timeout=2000)
    assert quick.GetStatus() is NodeStatus.SUCCEEDED

def test_execute_then_pause_queued_from_a_post_tick_handler_keeps_their_order(qtbot, widget):
    (endless,) = build(widget, Endless)
    fast_config(widget)
    armed = {"on": False}

    def on_terminate(tree, status, original=endless.OnTerminate):
        if status is NodeStatus.READY and armed["on"]:
            armed["on"] = False
            tree.Execute()
            tree.Pause()

    endless.OnTerminate = on_terminate
    ticks = []

    def on_ticked(count):
        ticks.append(count)
        if len(ticks) == 2:
            armed["on"] = True
            widget.Stop()  # issued from a post-tick handler

    widget.executor().ticked.connect(on_ticked)
    widget.Execute()
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Paused", timeout=3000)
    qtbot.wait(30)
    assert widget.executor().tick_count() == 1, "the restarted run ticks once before it is paused"
    widget.Stop()


# ============================================================================ background-thread commands
@pytest.mark.parametrize(
    "start_running, commands, transitions",
    [
        (False, ("Execute", "Stop"), ["Running", "Idle"]),
        (True, ("Stop", "Execute"), ["Idle", "Running"]),
        (False, ("Execute", "Pause"), ["Running", "Paused"]),
        (True, ("Reset", "Stop"), ["Idle"]),
        (True, ("Pause", "Execute", "Stop"), ["Paused", "Running", "Idle"]),
    ],
)
def test_commands_from_a_background_thread_are_all_carried_out_in_order(
    qtbot, widget, start_running, commands, transitions
):
    build(widget, R6Endless)
    fast_config(widget)
    if start_running:
        widget.Execute()
        assert widget.GetExecutionState() == "Running"
    states = []
    widget.executionStateChanged.connect(states.append)
    in_thread(lambda: [getattr(widget, name)() for name in commands])
    qtbot.waitUntil(lambda: states == transitions, timeout=3000)
    qtbot.wait(50)
    assert states == transitions
    assert widget.GetExecutionState() == transitions[-1]
    widget.Stop()

def test_a_blackboard_edit_does_not_drop_an_execute_from_a_background_thread(qtbot, widget):
    (quick,) = build(widget, R6Quick)
    fast_config(widget)
    in_thread(widget.Execute)  # queued for the GUI thread
    widget.SetEntry(1, "counter")  # edits the blackboard while the tree is idle
    run_until_idle(qtbot, widget)
    qtbot.waitUntil(lambda: quick.GetStatus() is NodeStatus.SUCCEEDED, timeout=3000)

def test_a_stale_stop_from_a_worker_does_not_cancel_a_new_run(qtbot, widget, monkeypatch):
    """The worker's Stop is issued for run A; meanwhile the GUI restarts (run B).

    Only run A's nodes may be cancelled: run B must complete instead of hanging with
    cancelled nodes and a dropped Stop.
    """
    (issuer,) = build(widget, StopIssuer)
    fast_config(widget)
    executor = widget.executor()
    real_cancel_runs = executor._cancel_runs
    about_to_cancel, restarted = threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    def cancel_runs(generation=None):
        if threading.get_ident() != main_thread and not about_to_cancel.is_set():
            about_to_cancel.set()
            assert restarted.wait(5)
        real_cancel_runs(generation)

    monkeypatch.setattr(executor, "_cancel_runs", cancel_runs)
    widget.Execute()
    qtbot.waitUntil(about_to_cancel.is_set, timeout=5000)
    widget.Stop()
    widget.Execute()  # run B; its node waits for run A's OnRun call to return
    restarted.set()
    run_until_idle(qtbot, widget)
    assert widget.GetRootNode().GetStatus() is NodeStatus.SUCCEEDED
    assert issuer.calls == 2


# ============================================================================ first tick ordering
def test_execute_then_pause_directly_from_a_ticked_handler_ticks_once(qtbot, widget):
    build(widget, R6Endless)
    fast_config(widget)
    armed = {"on": True}

    def on_ticked(count):
        if count == 2 and armed["on"]:
            armed["on"] = False
            widget.Stop()
            widget.Execute()
            widget.Pause()

    widget.executor().ticked.connect(on_ticked)
    widget.Execute()
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Paused", timeout=3000)
    qtbot.wait(30)
    assert widget.executor().tick_count() == 1, "the restarted run ticks once before it is paused"
    assert widget.GetExecutionState() == "Paused"
    widget.Stop()


# ============================================================================ deferred first tick
def test_call_when_idle_waits_for_the_deferred_first_tick(qtbot, widget):
    build(widget, R7Endless)
    fast_config(widget)
    seen = []

    def on_ticked(count):
        if count == 2 and not seen:
            seen.append("armed")
            widget.Stop()
            widget.Execute()
            widget.Pause()
            widget.executor().call_when_idle(
                lambda: seen.append((widget.executor().tick_count(), widget.GetExecutionState()))
            )

    widget.executor().ticked.connect(on_ticked)
    widget.Execute()
    qtbot.waitUntil(lambda: len(seen) == 2, timeout=3000)
    assert seen[1] == (1, "Paused")
    widget.Stop()


# ============================================================================ run generations
def test_a_worker_command_keeps_the_generation_of_its_own_run(qtbot, widget, monkeypatch):
    """The GUI restarts the tree between the worker's cancellation check and the rest of its Stop."""
    (issuer,) = build(widget, R7StopIssuer)
    fast_config(widget)
    real_check = execution.check_not_cancelled
    checked, restarted = threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    def check(action):
        real_check(action)
        if threading.get_ident() != main_thread and not checked.is_set():
            checked.set()
            assert restarted.wait(5)

    monkeypatch.setattr(execution, "check_not_cancelled", check)
    widget.Execute()
    qtbot.waitUntil(checked.is_set, timeout=5000)
    widget.Stop()
    widget.Execute()  # run B
    restarted.set()
    run_until_idle(qtbot, widget)
    assert widget.GetRootNode().GetStatus() is NodeStatus.SUCCEEDED
    assert issuer.calls == 2

def test_cancel_runs_uses_the_generation_of_the_token(qtbot, widget):
    (endless,) = build(widget, R7Endless)
    fast_config(widget)
    widget.Execute()
    executor = widget.executor()
    behaviour = executor.behaviour_for(endless)
    qtbot.waitUntil(lambda: behaviour.token is not None, timeout=2000)
    old = behaviour.token.generation
    widget.Reset()
    qtbot.waitUntil(lambda: behaviour.token is not None and behaviour.token.generation != old, timeout=2000)
    token = behaviour.token
    behaviour.generation = old  # what a stale reader could see
    executor._cancel_runs(old)
    assert not token.is_cancelled(), "only runs of the given generation are cancelled"
    executor._cancel_runs(token.generation)
    assert token.is_cancelled()
    widget.Stop()


# ============================================================================ chained runs
def test_stop_ends_chained_runs(qtbot, chained):
    widget, runs = chained
    QTimer.singleShot(0, widget.Stop)
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Idle", timeout=2000)
    assert_no_more_runs(qtbot, runs)
    assert widget.executor()._pending == []

def test_the_stop_button_ends_chained_runs(qtbot, chained):
    widget, runs = chained
    QTimer.singleShot(0, lambda: QTest.mouseClick(widget.button("Stop"), Qt.MouseButton.LeftButton))
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Idle", timeout=2000)
    assert_no_more_runs(qtbot, runs)

def test_pause_pauses_chained_runs_and_execute_resumes_them(qtbot, chained):
    widget, runs = chained
    QTimer.singleShot(0, widget.Pause)
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Paused", timeout=2000)
    assert_no_more_runs(qtbot, runs)
    count = len(runs)
    widget.Execute()
    qtbot.waitUntil(lambda: len(runs) > count + 2, timeout=2000)

def test_call_when_idle_is_called_while_runs_are_chained(qtbot, chained):
    widget, runs = chained
    called = []
    QTimer.singleShot(0, lambda: widget.executor().call_when_idle(lambda: called.append(True)))
    qtbot.waitUntil(lambda: bool(called), timeout=2000)

@pytest.mark.parametrize("second", ["call_when_idle", "Execute"])
def test_nothing_overtakes_commands_queued_behind_a_first_tick(qtbot, widget, second):
    build(widget, R8Endless)
    fast_config(widget)
    seen = []
    phase = {"n": 0}

    def on_ticked(count):
        if phase["n"] == 0 and count == 2:
            phase["n"] = 1
            widget.Stop()
            widget.Execute()
            widget.Pause()  # queued behind the restarted run's first tick
        elif phase["n"] == 1 and count == 1:  # that first tick; the Pause is still queued
            phase["n"] = 2
            if second == "call_when_idle":
                widget.executor().call_when_idle(lambda: seen.append(widget.GetExecutionState()))
            else:
                widget.Execute()  # issued after the Pause: resumes it
                seen.append(None)

    widget.executor().ticked.connect(on_ticked)
    widget.Execute()
    qtbot.waitUntil(lambda: bool(seen), timeout=3000)
    qtbot.wait(30)
    if second == "call_when_idle":
        assert seen == ["Paused"]
    else:
        assert widget.GetExecutionState() == "Running"
    widget.Stop()


# ============================================================================ commands to another tree
@pytest.mark.parametrize("command", ["Execute", "Stop"])
def test_an_on_run_call_can_control_another_tree(qtbot, make_widget, tmp_path, command):
    a, b = make_widget(), make_widget()
    for index, tree in enumerate((a, b)):
        for cls in TYPES:
            tree.RegisterNodeType(cls)
        assert tree.NewTree(str(tmp_path / f"t{index}.json"))
        fast_config(tree)
    build(a, Commander)
    build(b, R8Endless)
    for _ in range(3):  # different run generations in the two trees
        a.Reset()
    Commander.target, Commander.command = b, command
    Commander.sent = threading.Event()
    if command == "Stop":
        b.Execute()
        assert b.GetExecutionState() == "Running"
    a.Execute()
    assert Commander.sent.wait(5)
    expected = "Running" if command == "Execute" else "Idle"
    qtbot.waitUntil(lambda: b.GetExecutionState() == expected, timeout=3000)
    qtbot.waitUntil(lambda: not a.IsExecuting(), timeout=3000)
    b.Stop()


# ============================================================================ commands delivered inside a dialog opened by a chaining handler
@pytest.mark.parametrize("command", ["Stop", "Pause"])
@pytest.mark.parametrize("source", ["timer", "thread"])
def test_a_command_delivered_inside_a_dialog_opened_by_a_chaining_handler(qtbot, widget, command, source):
    widget.Connect(widget.GetRootNode(), widget.AddNode(R9Quick, 0, 220))
    fast_config(widget)
    runs = []
    chain = {"on": True}

    def issue():
        if source == "timer":
            getattr(widget, command)()
        else:
            thread = threading.Thread(target=getattr(widget, command), daemon=True)
            thread.start()
            thread.join(5)

    def on_finished(result):
        runs.append(result)
        if not chain["on"]:
            return
        widget.Execute()
        if len(runs) == 3:  # a "modal dialog": an event loop opened by the handler
            loop = QEventLoop()
            QTimer.singleShot(100, issue)
            QTimer.singleShot(300, loop.quit)
            loop.exec()

    widget.executionFinished.connect(on_finished)
    widget.Execute()
    expected = "Idle" if command == "Stop" else "Paused"
    qtbot.waitUntil(lambda: widget.GetExecutionState() == expected, timeout=3000)
    count = len(runs)
    qtbot.wait(100)
    assert len(runs) == count and widget.GetExecutionState() == expected
    chain["on"] = False
    widget.Stop()
