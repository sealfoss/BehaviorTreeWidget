"""Regression tests for the sixth fix pass (commands from background threads, the worker
Stop / cancel race, Execute+Pause from a post-tick handler, stable set order in files, the
Double entry editor and the demo's command line)."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import behavior_tree_widget
from behavior_tree_widget import LeafNodeWidget, NodeStatus, Status
from behavior_tree_widget import demo
from behavior_tree_widget.blackboard import FloatSpinBox

from conftest import fast_config, run_until_idle

DBL_MAX = sys.float_info.max


class Endless(LeafNodeWidget):
    TYPE_NAME = "R6Endless"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return Status.RUNNING


class Quick(LeafNodeWidget):
    TYPE_NAME = "R6Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class StopIssuer(LeafNodeWidget):
    """Worker leaf: the first OnRun call issues Stop and then waits until cancelled; later calls succeed."""

    TYPE_NAME = "R6StopIssuer"
    THREAD_WAIT = 0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = 0

    def OnRun(self, tree):
        self.calls += 1
        if self.calls == 1:
            tree.Stop()
            while not self.CancelRequested():
                threading.Event().wait(0.005)
            return False
        return True


@pytest.fixture
def widget(bt):
    for cls in (Endless, Quick, StopIssuer):
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


def in_thread(function):
    thread = threading.Thread(target=function, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()


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
    build(widget, Endless)
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
    (quick,) = build(widget, Quick)
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
    build(widget, Endless)
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


# ============================================================================ files
_ENCODE_SCRIPT = """
import json, sys
from behavior_tree_widget.serialization import encode_value
value = {
    "words": {"alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"},
    "mixed": frozenset({1, 2.5, "x", (1, "a"), ("b", 2), frozenset({"p", "q"}), None, True}),
    # the repr of these depends on the hash seed
    "nested": {frozenset(pair) for pair in ("ab", "ac", "ad", "bc", "bd", "cd", "ae", "be", "ce", "de")},
}
sys.stdout.write(json.dumps(encode_value(value), sort_keys=True))
"""


def test_sets_are_saved_in_the_same_order_whatever_the_hash_seed():
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(behavior_tree_widget.__file__)))
    outputs = set()
    for seed in ("0", "1", "2", "3", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed, QT_QPA_PLATFORM="offscreen")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
        result = subprocess.run(
            [sys.executable, "-c", _ENCODE_SCRIPT], env=env, capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout)
    assert len(outputs) == 1
    decoded = json.loads(outputs.pop())
    assert decoded["words"]["__set__"] == sorted(decoded["words"]["__set__"], key=json.dumps)


# ============================================================================ Double entry editor
@pytest.fixture
def double_row(qtbot, widget):
    """Returns make(value) -> the focused FloatSpinBox of a Double entry "d" holding ``value``."""

    def make(value):
        widget.SetEntry(value, "d")
        widget._set_modified(False)
        widget.setCurrentIndex(1)
        widget.activateWindow()
        qtbot.waitUntil(widget.isActiveWindow, timeout=2000)
        spin = widget.blackboardView().row("d").value_widget
        assert isinstance(spin, FloatSpinBox)
        spin.setFocus()
        qtbot.waitUntil(spin.hasFocus, timeout=1000)
        return spin

    return make


def test_enter_on_a_nan_entry_keeps_it_and_does_not_modify_the_tree(widget, double_row):
    spin = double_row(math.nan)
    assert spin.text() == "nan"
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert math.isnan(widget.GetEntry("d"))
    assert spin.text() == "nan"
    assert not widget.IsModified()


@pytest.mark.parametrize("value", [2.5, math.inf, 5e-324, 0.1 + 0.2, DBL_MAX])
def test_enter_without_editing_does_not_modify_the_tree(widget, double_row, value):
    spin = double_row(value)
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert widget.GetEntry("d") == value
    assert spin.text() == repr(value)
    assert not widget.IsModified()


def test_typing_zero_over_nan_stores_zero(widget, double_row):
    spin = double_row(math.nan)
    spin.selectAll()
    QTest.keyClicks(spin, "0")
    assert widget.GetEntry("d") == 0.0
    assert widget.IsModified()
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert widget.GetEntry("d") == 0.0 and spin.text() == "0.0"


def test_an_unfinished_edit_of_nan_goes_back_to_nan(widget, double_row):
    spin = double_row(math.nan)
    spin.selectAll()
    QTest.keyClicks(spin, "-")
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert math.isnan(widget.GetEntry("d")) and spin.text() == "nan"


def test_a_decimal_comma_is_accepted(widget, double_row):
    spin = double_row(1.0)
    spin.selectAll()
    QTest.keyClicks(spin, "2,5")
    assert widget.GetEntry("d") == 2.5
    assert spin.text() == "2.5"


@pytest.mark.parametrize(
    "typed, expected", [("5e-324", 5e-324), ("-inf", -math.inf), (repr(DBL_MAX), DBL_MAX), ("1e309", math.inf)]
)
def test_typed_values_are_stored_exactly(widget, double_row, typed, expected):
    spin = double_row(1.0)
    spin.selectAll()
    QTest.keyClicks(spin, typed)
    assert widget.GetEntry("d") == expected
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert widget.GetEntry("d") == expected
    assert spin.float_value() == expected


def test_typing_zero_over_a_subnormal_value_stores_zero(widget, double_row):
    spin = double_row(5e-324)
    assert spin.text() == "5e-324"
    spin.selectAll()
    QTest.keyClicks(spin, "0")
    assert widget.GetEntry("d") == 0.0


def test_negative_zero_is_a_change(widget, double_row):
    spin = double_row(0.0)
    spin.selectAll()
    QTest.keyClicks(spin, "-0")
    stored = widget.GetEntry("d")
    assert stored == 0.0 and math.copysign(1.0, stored) == -1.0


def test_float_spin_box_without_keyboard_tracking_reports_on_enter(qtbot):
    spin = FloatSpinBox()
    qtbot.addWidget(spin)
    spin.setKeyboardTracking(False)
    spin.set_float(math.nan)
    spin.show()
    spin.activateWindow()
    qtbot.waitUntil(spin.isActiveWindow, timeout=2000)
    spin.setFocus()
    reported = []
    spin.valueChanged.connect(lambda _value: reported.append(spin.float_value()))
    spin.selectAll()
    QTest.keyClicks(spin, "0")
    assert reported == []
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert reported == [0.0] and spin.text() == "0.0"


# ============================================================================ demo command line
def test_demo_loads_the_file_given_on_the_command_line(qtbot, tmp_path, monkeypatch, dialogs):
    windows = []
    real_window = demo.DemoWindow

    def make_window():
        window = real_window()
        qtbot.addWidget(window)
        windows.append(window)
        return window

    monkeypatch.setattr(demo, "DemoWindow", make_window)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    path = tmp_path / "tree.json"
    source = real_window()
    qtbot.addWidget(source)
    assert source.tree.NewTree(str(path))
    source.tree.Shutdown()

    assert demo.main(["demo", str(path)]) == 0
    assert windows[-1].tree.IsTreeLoaded()
    assert windows[-1].tree.GetFilePath() == os.path.abspath(str(path))

    assert demo.main(["demo", str(tmp_path / "missing.json")]) == 0
    assert "critical" in dialogs.kinds()
    assert not windows[-1].tree.IsTreeLoaded()
    for window in windows:
        window.tree._set_modified(False)
        window.tree.Shutdown()
