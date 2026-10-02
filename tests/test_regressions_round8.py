"""Regression tests for the eighth fix pass (commands while runs are chained from
executionFinished, commands sent to another tree from OnRun, tree replacement when a stop
restarts execution, the Double editor without keyboard tracking and with decimal commas,
list fields whose items cannot be converted to text, deeply nested placeholder data and
the demo's command line)."""

from __future__ import annotations

import json
import math
import os
import sys
import threading

import pytest
from PySide6.QtCore import QFile, QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox

from behavior_tree_widget import LeafNodeWidget, NodeStatus, Status
from behavior_tree_widget.blackboard import FloatSpinBox
from behavior_tree_widget.demo import _unconsumed_arguments
from behavior_tree_widget.nodes import UnknownLeafNodeWidget

from conftest import fast_config


class Quick(LeafNodeWidget):
    TYPE_NAME = "R8Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class Endless(LeafNodeWidget):
    TYPE_NAME = "R8Endless"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return Status.RUNNING


class RestartOnce(LeafNodeWidget):
    """Runs forever; when it is interrupted the first time, it starts the tree again."""

    TYPE_NAME = "R8RestartOnce"
    RUN_IN_THREAD = False
    restarted = False

    def OnRun(self, tree):
        return Status.RUNNING

    def OnTerminate(self, tree, status):
        if status is NodeStatus.READY and not type(self).restarted:
            type(self).restarted = True
            tree.Execute()


class Commander(LeafNodeWidget):
    """Worker leaf that sends ``command`` to ``target`` (another tree) and keeps running a while."""

    TYPE_NAME = "R8Commander"
    THREAD_WAIT = 0.0
    target = None
    command = "Execute"
    sent = threading.Event()

    def OnRun(self, tree):
        cls = type(self)
        if not cls.sent.is_set():
            getattr(cls.target, cls.command)()
            cls.sent.set()
        threading.Event().wait(0.15)
        return True


class Chooser(LeafNodeWidget):
    TYPE_NAME = "R8Chooser"
    RUN_IN_THREAD = False
    _fields = {"a": 1, "choices": ["x"], "b": 1}

    def OnRun(self, tree):
        return True


class Holder(LeafNodeWidget):
    TYPE_NAME = "R8Holder"
    RUN_IN_THREAD = False
    _fields = {"path": frozenset()}

    def OnRun(self, tree):
        return True


TYPES = (Quick, Endless, RestartOnce, Commander, Chooser, Holder)


@pytest.fixture
def widget(bt):
    for cls in TYPES:
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


def other_tree_file(make_widget, path, node_type="R8Endless"):
    other = make_widget()
    for cls in TYPES:
        other.RegisterNodeType(cls)
    assert other.NewTree(str(path))
    build(other, node_type)
    assert other.SaveTree(str(path))
    return str(path)


# ============================================================================ chained runs
@pytest.fixture
def chained(qtbot, widget):
    """A one-leaf tree whose runs finish in their first tick, restarted from executionFinished."""
    build(widget, Quick)
    fast_config(widget)
    runs = []
    chain = {"on": True}

    def on_finished(result):
        runs.append(result)
        if chain["on"]:
            widget.Execute()

    widget.executionFinished.connect(on_finished)
    widget.Execute()
    qtbot.waitUntil(lambda: len(runs) >= 5, timeout=3000)
    yield widget, runs
    chain["on"] = False
    widget.Stop()


def assert_no_more_runs(qtbot, runs):
    count = len(runs)
    qtbot.wait(100)
    assert len(runs) == count, "the chain went on"


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


def test_load_tree_replaces_the_tree_while_runs_are_chained(qtbot, chained, make_widget, tmp_path):
    widget, runs = chained
    target = other_tree_file(make_widget, tmp_path / "other.json")
    results = []
    QTimer.singleShot(0, lambda: results.append(widget.LoadTree(target)))
    qtbot.waitUntil(lambda: bool(results), timeout=2000)
    assert results == [True]
    assert widget.GetFilePath() == os.path.abspath(target)
    assert [node.TYPE_NAME for node in widget.GetNodes() if isinstance(node, LeafNodeWidget)] == ["R8Endless"]
    assert widget.GetExecutionState() == "Idle"
    assert_no_more_runs(qtbot, runs)


def test_call_when_idle_is_called_while_runs_are_chained(qtbot, chained):
    widget, runs = chained
    called = []
    QTimer.singleShot(0, lambda: widget.executor().call_when_idle(lambda: called.append(True)))
    qtbot.waitUntil(lambda: bool(called), timeout=2000)


@pytest.mark.parametrize("second", ["call_when_idle", "Execute"])
def test_nothing_overtakes_commands_queued_behind_a_first_tick(qtbot, widget, second):
    build(widget, Endless)
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
    build(b, Endless)
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


# ============================================================================ stop that restarts
def test_load_tree_from_a_ticked_handler_when_the_stop_restarts_execution(qtbot, widget, make_widget, tmp_path):
    RestartOnce.restarted = False
    build(widget, RestartOnce)
    fast_config(widget)
    target = other_tree_file(make_widget, tmp_path / "target.json", "R8Quick")
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


# ============================================================================ Double editor
@pytest.fixture
def untracked(qtbot):
    def make(value):
        spin = FloatSpinBox()
        qtbot.addWidget(spin)
        spin.setKeyboardTracking(False)
        spin.set_float(value)
        spin.show()
        spin.activateWindow()
        qtbot.waitUntil(spin.isActiveWindow, timeout=2000)
        spin.setFocus()
        qtbot.waitUntil(spin.hasFocus, timeout=1000)
        reported = []
        spin.valueChanged.connect(lambda _value: reported.append(spin.float_value()))
        return spin, reported

    return make


@pytest.mark.parametrize("start", [math.nan, 5e-324, -0.0])
@pytest.mark.parametrize("typed, key", [("1", Qt.Key.Key_Down), ("-1", Qt.Key.Key_Up), ("10", Qt.Key.Key_PageDown)])
def test_stepping_from_a_typed_value_without_keyboard_tracking(untracked, start, typed, key):
    spin, reported = untracked(start)
    spin.selectAll()
    QTest.keyClicks(spin, typed)
    QTest.keyClick(spin, key)
    assert spin.text() == "0.0" and spin.float_value() == 0.0
    assert reported == [0.0]
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert spin.text() == "0.0" and reported == [0.0]


@pytest.mark.parametrize("value", [5e-324, 1.5e-323, sys.float_info.min, 1.0, math.nan])
def test_enter_without_editing_reports_nothing_without_keyboard_tracking(untracked, value):
    spin, reported = untracked(value)
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert reported == []
    assert spin.text() == repr(value)


@pytest.fixture
def double_row(qtbot, widget):
    def make(value):
        widget.SetEntry(value, "d")
        widget._set_modified(False)
        widget.setCurrentIndex(1)
        widget.activateWindow()
        qtbot.waitUntil(widget.isActiveWindow, timeout=2000)
        spin = widget.blackboardView().row("d").value_widget
        spin.setFocus()
        qtbot.waitUntil(spin.hasFocus, timeout=1000)
        return spin

    return make


def test_undo_after_typing_a_decimal_comma(widget, double_row):
    spin = double_row(7.0)
    spin.selectAll()
    QTest.keyClicks(spin, "2,5")
    assert widget.GetEntry("d") == 2.5 and spin.text() == "2.5"
    for _ in range(3):
        QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "7.0" and widget.GetEntry("d") == 7.0


def test_undo_after_pasting_a_decimal_comma_brings_nan_back(widget, double_row):
    spin = double_row(math.nan)
    QGuiApplication.clipboard().setText(" 2,5 ")
    spin.selectAll()
    QTest.keyClick(spin, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert widget.GetEntry("d") == 2.5
    QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "nan" and math.isnan(widget.GetEntry("d"))


# ============================================================================ list fields
def test_a_list_field_item_that_cannot_be_converted_to_text(widget):
    (node,) = build(widget, Chooser)
    node.SetField("choices", [10**5000, 7])
    combo = node._editors["choices"]
    assert isinstance(combo, QComboBox)
    assert [combo.itemText(i) for i in range(combo.count())] == ["<int object>", "7"]
    node.SetField("b", 2.5)  # another kind: the editors are rebuilt
    node.SetField("a", 5)
    node.SetField("choices", ["x", "y"])
    assert node.GetField("a") == 5 and node.GetField("b") == 2.5 and node.GetField("choices") == ["x", "y"]


def test_a_node_class_with_such_a_default_can_be_created(widget):
    class HugeChoices(LeafNodeWidget):
        TYPE_NAME = "R8HugeChoices"
        RUN_IN_THREAD = False
        _fields = {"choices": [10**5000, 1]}

        def OnRun(self, tree):
            return True

    widget.RegisterNodeType(HugeChoices)
    node = widget.AddNode(HugeChoices, 0, 200)
    assert node.GetFieldSelection("choices") == 10**5000


# ============================================================================ placeholders
def _nested_frozenset(depth):
    value = frozenset({1})
    for level in range(depth - 1):
        value = frozenset({value, level})
    return value


def test_a_deep_set_field_survives_a_node_type_that_is_not_registered(widget, make_widget, tmp_path):
    (holder,) = build(widget, Holder)
    deep = _nested_frozenset(240)
    holder.SetField("path", deep)
    first = str(tmp_path / "first.json")
    assert widget.SaveTree(first)

    without = make_widget()  # R8Holder is not registered here
    assert without.LoadTree(first)
    (placeholder,) = [node for node in without.GetNodes() if isinstance(node, UnknownLeafNodeWidget)]
    second = str(tmp_path / "second.json")
    assert without.SaveTree(second)

    again = make_widget()
    again.RegisterNodeType(Holder)
    assert again.LoadTree(second)
    (loaded,) = [node for node in again.GetNodes() if isinstance(node, Holder)]
    assert loaded.GetField("path") == deep


def _json_window():
    """A nesting depth that json.loads reads but json.dumps(indent=2) cannot write, or None."""

    def loads_ok(depth):
        try:
            json.loads("[" * depth + "]" * depth)
        except RecursionError:
            return False
        return True

    def dumps_ok(depth):
        value = []
        for _ in range(depth):
            value = [value]
        try:
            json.dumps(value, indent=2)
        except RecursionError:
            return False
        return True

    def limit(ok):
        low, high = 1, 200_000
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if ok(middle) else (low, middle - 1)
        return low

    dumps_limit, loads_limit = limit(dumps_ok), limit(loads_ok)
    if loads_limit - dumps_limit < 1000:
        return None
    return (dumps_limit + loads_limit) // 2


def test_deep_raw_placeholder_data_does_not_block_saving(widget, make_widget, tmp_path):
    depth = _json_window()
    if depth is None:
        pytest.skip("json reads and writes the same depths on this Python")
    base = str(tmp_path / "base.json")
    assert widget.SaveTree(base)
    with open(base, encoding="utf-8") as handle:
        text = json.dumps(json.load(handle))
    node = '{"id": "n1", "type": "R8NotRegistered", "title": "Deep", "x": 0, "y": 200, "fields": {"deep": %s, "ok": 1}}'
    text = text.replace('"nodes": [', '"nodes": [' + node % ("[" * depth + "]" * depth) + ", ", 1)
    hand_written = tmp_path / "hand.json"
    hand_written.write_text(text, encoding="utf-8")

    other = make_widget()
    assert other.LoadTree(str(hand_written))  # the deep field is kept as read
    out = str(tmp_path / "out.json")
    assert other.SaveTree(out)
    with open(out, encoding="utf-8") as handle:
        saved = json.load(handle)
    (placeholder,) = [n for n in saved["nodes"] if n["type"] == "R8NotRegistered"]
    assert placeholder["fields"] == {"ok": 1}


# ============================================================================ demo command line
def _round_trip(text):
    return QFile.decodeName(QFile.encodeName(text))


@pytest.mark.parametrize(
    "argv, kept",
    [
        (["demo", "-qwindowtitle", "日.json", "本.json"], ["demo", "本.json"]),
        (["demo", "-qwindowtitle", "Ωmega.json", "Omega.json"], ["demo", "Omega.json"]),
        (["demo", "-stylesheet", "Ωmega.json", "Omega.json"], ["demo", "Omega.json"]),
        (["demo", "-style", "fusion", "Bäume.json"], ["demo", "Bäume.json"]),
        (["demo", "Ωmega.json", "-style", "fusion"], ["demo", "Ωmega.json"]),
        (["demo", "tree.json"], ["demo", "tree.json"]),
        (["demo"], ["demo"]),
    ],
)
def test_unconsumed_arguments(argv, kept):
    arguments = [_round_trip(arg) for arg in kept]  # what Qt reports
    assert _unconsumed_arguments(argv, arguments) == kept
