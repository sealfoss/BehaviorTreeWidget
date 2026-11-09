"""Loading or creating a tree while the tree executes: the files are checked at once and the tree is
replaced right after the tick; failures put the previous tree back."""

from __future__ import annotations

import logging
import os

import pytest
from PySide6.QtCore import QPointF, QTimer
from PySide6.QtWidgets import QMessageBox
from pytestqt.exceptions import capture_exceptions

from behavior_tree_widget import LeafNodeWidget, NodeStatus, TreeFileError

from conftest import fast_config, run_until_idle
from support.round3 import (
    BAD_CALLS,
    LOGGER,
    SUCCEEDED,
    WHERES,
    R3Probe,
    _busy_scenario,
    _Handle,
    _run_busy_scenario,
    _snapshot_now,
    add_leaf,
    buttons,
    critical_texts,
    describe,
    errors_logged,
    events,
    next_tree_file,
    node_ids,
    probes,
    record,
    save,
    timer_active,
)
from support.round4 import GatedWorker, build
from support.round7 import Endless, Quick, other_tree_file
from support.round8 import RestartOnce, assert_no_more_runs, chained, other_tree_file as r8_other_tree_file


# ============================================================================ LoadTree / NewTree while the executor is busy
@pytest.mark.parametrize("where", WHERES)
@pytest.mark.parametrize("operation", ["LoadTree", "NewTree"])
def test_load_or_new_while_busy_replaces_tree_right_after(qtbot, bt, tmp_path, where, operation):
    other = next_tree_file(tmp_path)
    fresh = str(tmp_path / "fresh_BT.json")
    target = other if operation == "LoadTree" else fresh
    bt.AddEntry("x", "Integer", 0)
    before_run, during_run = _Handle("before"), _Handle("during")
    bt.SetEntry(before_run, "obj")

    def action(tree):
        tree.SetEntry(1, "x")
        tree.SetEntry(during_run, "obj")
        result = tree.LoadTree(other) if operation == "LoadTree" else tree.NewTree(fresh)
        action.file_written = os.path.exists(fresh) and os.path.exists(str(tmp_path / "fresh_BB.json"))
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
        return [item for item in bt.blackboard_store().items() if item[0] != "obj"]

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
        assert bt.blackboard_store().items() == [("y", "Integer", 3)]
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


# ============================================================================ deferred first tick
def test_load_tree_while_the_deferred_first_tick_is_pending(qtbot, widget, make_widget, tmp_path):
    build(widget, Endless)
    fast_config(widget)
    target = other_tree_file(make_widget, tmp_path / "other.json")
    results = []

    def on_ticked(count):
        if count == 2 and not results:
            widget.Stop()
            widget.Execute()  # a restart from a post-tick handler: its first tick is deferred
            assert widget.executor().is_busy()
            results.append(widget.LoadTree(target))

    widget.executor().ticked.connect(on_ticked)
    widget.Execute()
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert results == [True]
    qtbot.waitUntil(lambda: widget.GetFilePath() == os.path.abspath(target), timeout=3000)
    qtbot.wait(30)
    assert widget.GetExecutionState() == "Idle"
    assert [node.TYPE_NAME for node in widget.GetNodes() if isinstance(node, LeafNodeWidget)] == ["R7Quick"]

def test_new_tree_from_an_execution_finished_slot_after_a_chained_execute(qtbot, widget, tmp_path):
    build(widget, Quick)
    fast_config(widget)
    target = str(tmp_path / "fresh_BT.json")
    results = []
    armed = {"on": True}

    def chain(_result):
        if armed["on"]:
            widget.Execute()

    def replace(_result):
        if armed["on"]:
            armed["on"] = False
            results.append(widget.NewTree(target))

    widget.executionFinished.connect(chain)
    widget.executionFinished.connect(replace)
    widget.Execute()
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert results == [True]
    qtbot.waitUntil(lambda: widget.GetFilePath() == os.path.abspath(target), timeout=3000)
    qtbot.wait(30)
    assert widget.GetExecutionState() == "Idle"
    assert widget.GetNodes() == [widget.GetRootNode()]


# ============================================================================ chained runs
def test_load_tree_replaces_the_tree_while_runs_are_chained(qtbot, chained, make_widget, tmp_path):
    widget, runs = chained
    target = r8_other_tree_file(make_widget, tmp_path / "other.json")
    results = []
    QTimer.singleShot(0, lambda: results.append(widget.LoadTree(target)))
    qtbot.waitUntil(lambda: bool(results), timeout=2000)
    assert results == [True]
    assert widget.GetFilePath() == os.path.abspath(target)
    assert [node.TYPE_NAME for node in widget.GetNodes() if isinstance(node, LeafNodeWidget)] == ["R8Endless"]
    assert widget.GetExecutionState() == "Idle"
    assert_no_more_runs(qtbot, runs)


# ============================================================================ stop that restarts
def test_load_tree_from_a_ticked_handler_when_the_stop_restarts_execution(qtbot, widget, make_widget, tmp_path):
    RestartOnce.restarted = False
    build(widget, RestartOnce)
    fast_config(widget)
    target = r8_other_tree_file(make_widget, tmp_path / "target.json", "R8Quick")
    results = []

    def on_ticked(count):
        if count == 3 and not results:
            results.append(widget.LoadTree(target))

    widget.executor().ticked.connect(on_ticked)
    widget.Execute()
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert results == [True]
    qtbot.waitUntil(lambda: widget.GetFilePath() == os.path.abspath(target), timeout=3000)
    assert RestartOnce.restarted
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Idle", timeout=3000)
    assert [node.TYPE_NAME for node in widget.GetNodes() if isinstance(node, LeafNodeWidget)] == ["R8Quick"]
