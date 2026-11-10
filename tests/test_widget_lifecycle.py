"""The widget's life: Shutdown, destruction without Shutdown, the threads its API may be called from,
and reference cycles."""

from __future__ import annotations

import gc
import logging
import os
import threading
import weakref

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QtMsgType
from pytestqt.exceptions import capture_exceptions

from behavior_tree_widget import BehaviorTreeWidget, NodeStatus

from conftest import fast_config, run_until_idle
from support.round3 import (
    LOGGER,
    R3_TYPES,
    R3Probe,
    R3Worker,
    _api_calls,
    add_leaf,
    assert_lifecycle,
    buttons,
    describe,
    errors_logged,
    events,
    flush_deferred_deletes,
    keys_of,
    next_tree_file,
    probes,
    qt_messages,
    save,
    ticks,
    timer_active,
    timer_thread_warnings,
)
from support.round4 import Quick


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


# ============================================================================ non-GUI threads
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
    for name in ("from_thread_new", "from_thread_save", "dialog_path"):
        for suffix in (".json", "_BT.json", "_BB.json"):
            assert not os.path.exists(tmp_path / f"{name}{suffix}"), f"{name}{suffix}"
    assert dialogs.shown == [], "no dialog may be opened from a worker thread"
    assert child.GetParent() is root and loose.GetParent() is None


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
        store, executor = widget.blackboard_store(), widget.executor()
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


# ============================================================================ cancelled callers
def test_shutdown_from_worker_thread_raises(qtbot, widget):
    errors = []

    def call():
        try:
            widget.Shutdown()
        except RuntimeError as error:
            errors.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    thread.join(5)
    assert len(errors) == 1 and "GUI thread" in str(errors[0])
    assert widget.button("Execute").isEnabled()


# ============================================================================ no reference cycles
def test_closed_top_level_widget_is_freed(qtbot, tmp_path):
    destroyed = []
    widget = BehaviorTreeWidget(node_types=[Quick])
    widget.show()
    assert widget.NewTree(str(tmp_path / "t.json"))
    widget.Connect(widget.GetRootNode(), widget.AddNode(Quick, 0, 200))
    widget.destroyed.connect(lambda *_: destroyed.append(True))
    ref = weakref.ref(widget)
    widget._set_modified(False)
    widget.close()
    del widget
    for _ in range(5):
        gc.collect()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qtbot.wait(10)
    assert ref() is None, "a closed and dropped top-level BehaviorTreeWidget must be collectable"
    assert destroyed == [True]
