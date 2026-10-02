"""Regression tests for the fourth fix pass (command chaining, cancelled callers, snapshots,
event filter lifetime, reference cycles, view centre drift and data edge cases)."""

from __future__ import annotations

import gc
import json
import threading
import weakref

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF
from PySide6.QtWidgets import QApplication

from behavior_tree_widget import BehaviorTreeWidget, ExecutionCancelled, LeafNodeWidget, NodeStatus, Status

from conftest import fast_config, run_until_idle


class Quick(LeafNodeWidget):
    """Finishes within the first tick (GUI thread)."""

    TYPE_NAME = "R4Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class GatedWorker(LeafNodeWidget):
    """Worker-thread leaf that blocks until ``gate`` opens, then runs ``after(tree)``."""

    TYPE_NAME = "R4Gated"
    THREAD_WAIT = 0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.after = None
        self.errors: list[BaseException] = []
        self.done = threading.Event()

    def OnRun(self, tree):
        self.entered.set()
        self.gate.wait(10)
        try:
            if self.after is not None:
                self.after(tree)
        except BaseException as error:  # noqa: BLE001 - recorded for the test
            self.errors.append(error)
        finally:
            self.done.set()
        return True


class Writer(LeafNodeWidget):
    TYPE_NAME = "R4Writer"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("x") + 1, "x")
        return True


@pytest.fixture
def widget(bt):
    for cls in (Quick, GatedWorker, Writer):
        bt.RegisterNodeType(cls)
    return bt


def build(widget, *types):
    root = widget.GetRootNode()
    nodes = []
    for index, node_type in enumerate(types):
        node = widget.AddNode(node_type, index * 320.0, 220.0)
        widget.Connect(root, node)
        nodes.append(node)
    return nodes


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


# ============================================================================ blackboard snapshot semantics
def test_load_while_running_does_not_roll_back_object_entries(qtbot, widget, tmp_path):
    other = str(tmp_path / "other.json")
    widget.SaveTree(other)
    widget.SaveTree(str(tmp_path / "tree.json"))
    handle_before, handle_during = object(), object()
    widget.SetEntry(handle_before, "robot")
    build(widget, GatedWorker)
    fast_config(widget, restore_blackboard=True)
    widget.Execute()
    widget.SetEntry(handle_during, "robot")
    widget._set_modified(False)
    assert widget.LoadTree(other)
    assert widget.GetEntry("robot") is handle_during


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
    row = widget.blackboardView().row("x")
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


# ============================================================================ event filter lifetime
def test_outside_click_filter_is_only_installed_while_a_connection_is_selected(widget):
    (quick,) = build(widget, Quick)
    view = widget.view()
    assert view._outside_filter_installed is False
    view.select_connection(view.connection_item(quick))
    assert view._outside_filter_installed is True
    view.select_connection(None)
    assert view._outside_filter_installed is False
    view.select_connection(view.connection_item(quick))
    view.hide()
    assert view._outside_filter_installed is False


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


# ============================================================================ view centre
def test_view_centre_does_not_drift_over_save_load_cycles(qtbot, widget, tmp_path):
    path = str(tmp_path / "centre.json")
    widget.view().centerOn(QPointF(123.25, 456.75))
    widget.SaveTree(path)
    first = json.load(open(path, encoding="utf-8"))["view"]["center"]
    for _ in range(5):
        assert widget.LoadTree(path)
        qtbot.wait(5)
        widget.SaveTree(path)
    last = json.load(open(path, encoding="utf-8"))["view"]["center"]
    assert abs(first[0] - last[0]) < 0.51 and abs(first[1] - last[1]) < 0.51


# ============================================================================ data edge cases
def test_unknown_placeholder_keeps_selections_of_raw_fields(make_widget, tmp_path):
    path = tmp_path / "raw.json"
    data = {
        "format": "behavior_tree_widget",
        "version": 1,
        "nodes": [
            {"id": "r", "type": "Root", "title": "Root", "x": 0, "y": 0},
            {"id": "m", "type": "Missing", "title": "Missing", "x": 0, "y": 200,
             "fields": {"choice": {"__set__": [[1, 2]]}, "plain": ["a", "b"]},
             "field_selections": {"choice": 1, "plain": 1}},
        ],
        "connections": [{"parent": "r", "child": "m"}],
        "blackboard": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    widget = make_widget(node_types=[])
    assert widget.LoadTree(str(path))
    widget.SaveTree(str(path))
    node = [n for n in json.load(open(path, encoding="utf-8"))["nodes"] if n["id"] == "m"][0]
    assert node["fields"]["choice"] == {"__set__": [[1, 2]]}
    assert node["field_selections"] == {"choice": 1, "plain": 1}


class BrokenRepr:
    def __repr__(self):
        raise RuntimeError("no repr")


def test_object_entry_with_broken_repr_still_gets_a_row(widget):
    widget.SetEntry(BrokenRepr(), "broken")
    row = widget.blackboardView().row("broken")
    assert row is not None
    assert "BrokenRepr" in row.value_widget.text()


def test_non_str_field_keys_are_skipped_when_saving(widget, dialogs, tmp_path, caplog):
    (quick,) = build(widget, Quick)
    quick.SetField((1, 2), "tuple key")
    quick.SetField("ok", 5)
    path = str(tmp_path / "keys.json")
    assert widget.SaveTree(path)
    saved = [n for n in json.load(open(path, encoding="utf-8"))["nodes"] if n["type"] == "R4Quick"][0]
    assert saved["fields"] == {"ok": 5}
    assert any("(1, 2)" in record.getMessage() for record in caplog.records)


class PreTitled(LeafNodeWidget):
    TYPE_NAME = "R4PreTitled"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        self._title = 123  # a non-str title assigned before super().__init__()
        super().__init__(parent)

    def OnRun(self, tree):
        return True


def test_non_str_title_assigned_before_super_is_coerced(widget):
    node = widget.AddNode(PreTitled, 0, 200)
    assert node.GetTitle() == "123"
    assert node._title_label.text() == "123"
