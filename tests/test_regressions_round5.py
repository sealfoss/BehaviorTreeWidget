"""Regression tests for the fifth fix pass (queued worker commands, first-tick ordering, file
writing on restricted folders, display robustness, badges while executing and UX details)."""

from __future__ import annotations

import errno
import json
import math
import os
import threading

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDoubleSpinBox

from behavior_tree_widget import LeafNodeWidget, NodeStatus
from behavior_tree_widget import serialization
from behavior_tree_widget.blackboard import FloatSpinBox, safe_repr
from behavior_tree_widget.nodes import CHILDREN, PARENT

from conftest import drag, fast_config, label_point, run_until_idle, viewport_point


class Quick(LeafNodeWidget):
    TYPE_NAME = "R5Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class Endless(LeafNodeWidget):
    TYPE_NAME = "R5Endless"
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = 0

    def OnRun(self, tree):
        from behavior_tree_widget import Status

        self.runs += 1
        return Status.RUNNING


class Emitter(LeafNodeWidget):
    """Worker leaf that issues ``command`` once, then waits until cancelled."""

    TYPE_NAME = "R5Emitter"
    THREAD_WAIT = 0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.command = "Pause"
        self.issued = threading.Event()
        self.calls = 0

    def OnRun(self, tree):
        self.calls += 1
        if self.calls == 1:
            getattr(tree, self.command)()
            self.issued.set()
            while not self.CancelRequested():
                threading.Event().wait(0.005)
            return False
        return True


@pytest.fixture
def widget(bt):
    for cls in (Quick, Endless, Emitter):
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
    (quick,) = build(widget, Quick)
    config = widget.GetConfig()
    config.tick_interval_ms = 60_000  # a first tick that waited for the timer would never come
    widget.SetConfig(config)
    widget.executionStateChanged.connect(lambda state: widget.Execute() if state == "Running" else None)
    widget.Execute()
    qtbot.waitUntil(lambda: widget.GetExecutionState() == "Idle", timeout=2000)
    assert quick.GetStatus() is NodeStatus.SUCCEEDED


def test_reset_during_execute_still_ticks_at_once(qtbot, widget):
    (quick,) = build(widget, Quick)
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


# ============================================================================ files
def test_write_json_atomic_fails_fast_when_files_cannot_be_created(tmp_path, monkeypatch):
    real_open = os.open

    def deny(path, flags, mode=0o777):
        if str(path).endswith(".json.tmp"):
            raise PermissionError(errno.EACCES, "Access is denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(serialization.os, "open", deny)
    with pytest.raises(PermissionError) as info:
        serialization.write_json_atomic(str(tmp_path / "t.json"), {"a": 1})
    assert "no permission to create files" in str(info.value)
    assert os.listdir(tmp_path) == []


def test_write_json_atomic_reports_missing_folder_and_folder_target(tmp_path):
    with pytest.raises(FileNotFoundError) as info:
        serialization.write_json_atomic(str(tmp_path / "missing" / "t.json"), {})
    assert "does not exist" in str(info.value) and ".tmp" not in str(info.value)
    (tmp_path / "folder.json").mkdir()
    with pytest.raises(IsADirectoryError):
        serialization.write_json_atomic(str(tmp_path / "folder.json"), {})


# ============================================================================ display robustness
class BrokenRepr:
    def __repr__(self):
        raise RuntimeError("no repr")

    __str__ = __repr__


def test_containers_with_broken_repr_still_get_rows(widget):
    widget.SetEntry([BrokenRepr()], "items")
    widget.SetEntry({"k": BrokenRepr()}, "mapping")
    for name in ("items", "mapping"):
        assert widget.blackboardView().row(name) is not None


def test_set_with_broken_repr_is_skipped_when_saving(widget, tmp_path):
    widget.SetEntry({BrokenRepr()}, "bad_set")
    widget.SetEntry(1, "good")
    path = str(tmp_path / "s.json")
    assert widget.SaveTree(path)
    names = [entry["name"] for entry in json.load(open(path, encoding="utf-8"))["blackboard"]]
    assert names == ["good"]


def test_container_tooltips_are_exact_for_small_values(widget):
    widget.SetEntry(list(range(8)), "eight")
    widget.SetEntry({"z": 1, "a": 2}, "ordered")
    assert widget.blackboardView().row("eight").value_widget.toolTip() == repr(list(range(8)))
    assert widget.blackboardView().row("ordered").value_widget.toolTip() == "{'z': 1, 'a': 2}"


def test_safe_repr_of_large_bytes_is_cheap_and_short():
    text = safe_repr(b"x" * 10_000_000)
    assert len(text) <= 500 and "10000000 bytes" in text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 1e-9, 12345.678])
def test_double_rows_show_special_and_tiny_values(widget, value):
    widget.SetEntry(value, "d")
    spin = widget.blackboardView().row("d").value_widget
    assert isinstance(spin, QDoubleSpinBox) and isinstance(spin, FloatSpinBox)
    assert spin.text() == repr(value)
    stored = widget.GetEntry("d")
    assert stored == value or (math.isnan(stored) and math.isnan(value))


def test_nudging_a_tiny_double_keeps_it(widget):
    widget.SetEntry(1e-9, "tiny")
    spin = widget.blackboardView().row("tiny").value_widget
    spin.stepUp()
    assert widget.GetEntry("tiny") == pytest.approx(1.000000001)


def test_view_center_with_a_zero_height_viewport(widget):
    view = widget.view()
    view.centerOn(QPointF(4000, -2500))
    before = view.view_center()
    view.resize(view.width(), 0)
    after = view.view_center()
    assert abs(after.x() - before.x()) < 600 and after != QPointF(0, 0)


# ============================================================================ canvas while executing
def test_connection_labels_do_not_move_nodes_while_executing(qtbot, widget):
    (endless,) = build(widget, Endless)
    widget.view().centerOn(100, 150)
    fast_config(widget)
    widget.Execute()
    before = endless._item.pos()
    start = label_point(widget, endless, PARENT)
    drag(widget, start, start + QPoint(120, 60))
    assert endless._item.pos() == before
    widget.Stop()


def test_order_badges_keep_the_running_order_until_execution_ends(qtbot, widget):
    first, second = build(widget, Endless, Quick)
    root = widget.GetRootNode()
    view = widget.view()
    fast_config(widget)
    widget.Execute()
    second._item.setPos(first._item.pos() - QPointF(700, 0))  # move the second child to the far left
    assert view.connection_item(first).order() == 1, "badges show the order of the running tree"
    assert view.connection_item(second).order() == 2
    widget.Stop()
    assert view.connection_item(second).order() == 1, "after execution the badges follow the positions"
    assert root.GetChildren()[0] is second


def test_outside_click_filter_is_restored_after_hide_and_show(widget):
    (quick,) = build(widget, Quick)
    view = widget.view()
    view.select_connection(view.connection_item(quick))
    view.hide()
    assert view._outside_filter_installed is False
    view.show()
    assert view._outside_filter_installed is True


# ============================================================================ UX details
def test_execution_buttons_use_text_symbols(widget):
    for name in ("Execute", "Pause", "Stop", "Reset", "Configure"):
        text = widget.button(name).text()
        assert text and all(ord(char) < 0x1F000 for char in text), (name, text)


def test_field_editor_shows_the_start_of_long_text_after_set_field(widget):
    class Texty(LeafNodeWidget):
        TYPE_NAME = "R5Texty"
        _fields = {"text": "short"}
        RUN_IN_THREAD = False

        def OnRun(self, tree):
            return True

    node = widget.AddNode(Texty, 0, 200)
    node.SetField("text", "START " + "x" * 200)
    assert node.field_editor("text").cursorPosition() == 0


def test_unknown_placeholder_keeps_selections_verbatim(make_widget, tmp_path):
    path = tmp_path / "raw.json"
    selections = {"plain": "1", "flag": True, "neg": -3, "big": 99}
    data = {
        "format": "behavior_tree_widget",
        "version": 1,
        "nodes": [
            {"id": "r", "type": "Root", "title": "Root", "x": 0, "y": 0},
            {"id": "m", "type": "Missing", "title": "Missing", "x": 0, "y": 200,
             "fields": {"plain": ["a", "b"], "nosel": ["x", "y"]}, "field_selections": selections},
        ],
        "connections": [],
        "blackboard": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    widget = make_widget(node_types=[])
    assert widget.LoadTree(str(path))
    assert widget.SaveTree(str(path))
    node = [n for n in json.load(open(path, encoding="utf-8"))["nodes"] if n["id"] == "m"][0]
    assert node["field_selections"] == selections
