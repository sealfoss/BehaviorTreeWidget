"""Regression tests for the seventh fix pass (tree replacement while a deferred first tick is
pending, run generations kept in the run tokens, undo in the Double editor, abandoned edits
without keyboard tracking, non-ANSI file names on the demo's command line, huge integers
and deeply nested values in files)."""

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

import behavior_tree_widget
from behavior_tree_widget import LeafNodeWidget, NodeStatus, Status
from behavior_tree_widget import execution
from behavior_tree_widget.blackboard import FloatSpinBox
from behavior_tree_widget.execution import LeafBehaviour
from behavior_tree_widget.serialization import MAX_NESTING, decode_value, encode_value, is_encodable

from conftest import fast_config, run_until_idle


class Endless(LeafNodeWidget):
    TYPE_NAME = "R7Endless"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return Status.RUNNING


class Quick(LeafNodeWidget):
    TYPE_NAME = "R7Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class StopIssuer(LeafNodeWidget):
    """Worker leaf: the first OnRun call issues Stop and then waits until cancelled; later calls succeed."""

    TYPE_NAME = "R7StopIssuer"
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


class Holder(LeafNodeWidget):
    TYPE_NAME = "R7Holder"
    RUN_IN_THREAD = False
    _fields = {"path": ()}

    def OnRun(self, tree):
        return True


TYPES = (Endless, Quick, StopIssuer, Holder)


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


def other_tree_file(make_widget, path, node_type="R7Quick"):
    """Write a tree file holding a root and one ``node_type`` leaf; returns its path."""
    other = make_widget()
    for cls in TYPES:
        other.RegisterNodeType(cls)
    assert other.NewTree(str(path))
    build(other, node_type)
    assert other.SaveTree(str(path))
    return str(path)


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
    target = str(tmp_path / "fresh.json")
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


def test_call_when_idle_waits_for_the_deferred_first_tick(qtbot, widget):
    build(widget, Endless)
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
    (issuer,) = build(widget, StopIssuer)
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
    (endless,) = build(widget, Endless)
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


# ============================================================================ Double editor
@pytest.fixture
def double_row(qtbot, widget):
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


def test_undo_back_to_nan_stores_nan(widget, double_row):
    spin = double_row(math.nan)
    spin.selectAll()
    QTest.keyClicks(spin, "5")
    assert widget.GetEntry("d") == 5.0
    QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "nan"
    assert math.isnan(widget.GetEntry("d"))
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert spin.text() == "nan" and math.isnan(widget.GetEntry("d"))
    widget.SetEntry(5.0, "d")
    assert spin.text() == "5.0"


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


@pytest.mark.parametrize("start, typed", [(math.nan, "0"), (1.0, "1.0000000000001"), (5e-324, "0"), (0.0, "-0")])
def test_an_abandoned_edit_without_keyboard_tracking_reverts(untracked, start, typed):
    spin, reported = untracked(start)
    spin.selectAll()
    QTest.keyClicks(spin, typed)
    assert reported == []
    spin.selectAll()
    QTest.keyClicks(spin, "-")  # unfinished
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert reported == []
    assert spin.text() == repr(start)
    value = spin.float_value()
    assert (math.isnan(value) and math.isnan(start)) or (value == start and repr(value) == repr(start))


def test_a_finished_edit_without_keyboard_tracking_is_reported_once(untracked):
    spin, reported = untracked(math.nan)
    spin.selectAll()
    QTest.keyClicks(spin, "0")
    assert math.isnan(spin.float_value()), "not committed before Enter"
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert reported == [0.0] and spin.text() == "0.0"


# ============================================================================ demo command line
_DEMO_DRIVER = r"""
import sys
from PySide6.QtWidgets import QApplication, QMessageBox
from behavior_tree_widget import demo
QMessageBox.critical = staticmethod(lambda *args: sys.stderr.write(repr(args[1:]) + "\n"))  # never block
windows = []
real = demo.DemoWindow
def make():
    window = real()
    windows.append(window)
    return window
demo.DemoWindow = make
QApplication.exec = lambda self: 0
demo.main(sys.argv[1:])
tree = windows[-1].tree
sys.stdout.buffer.write(repr(tree.GetFilePath()).encode("utf-8"))
tree._set_modified(False)
tree.Shutdown()
"""


@pytest.mark.parametrize("name", ["Ωmega.json", "日本.json", "Bäume.json"])
@pytest.mark.parametrize("qt_options", [[], ["-style", "fusion"]])
def test_demo_loads_a_file_whose_name_is_not_in_the_ansi_code_page(make_widget, tmp_path, name, qt_options):
    target = other_tree_file(make_widget, tmp_path / name)
    other_tree_file(make_widget, tmp_path / "Omega.json")  # the "best fit" of Ωmega.json
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(behavior_tree_widget.__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
    result = subprocess.run(
        [sys.executable, "-c", _DEMO_DRIVER, "demo", *qt_options, target],
        env=env, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode("utf-8") == repr(os.path.abspath(target))


# ============================================================================ files
@pytest.mark.parametrize(
    "value", [{10**5000, 3}, [10**5000], 10**5000, {"a": (10**5000,)}], ids=["set", "list", "int", "dict"]
)
def test_integers_too_long_for_json_are_not_encodable(value):
    assert not is_encodable(value)
    with pytest.raises(TypeError, match="integers this large"):
        encode_value(value)


def test_a_huge_integer_entry_is_skipped_when_saving(widget, make_widget, tmp_path):
    widget.SetEntry({10**5000, 3}, "huge_set")
    widget.SetEntry([10**5000], "huge_list")
    widget.SetEntry(7, "keep_me")
    path = str(tmp_path / "saved.json")
    assert widget.SaveTree(path)
    with open(path, encoding="utf-8") as handle:
        names = [entry["name"] for entry in json.load(handle)["blackboard"]]
    assert "keep_me" in names and "huge_set" not in names and "huge_list" not in names


def _nested(kind, depth):
    value = 1
    for _ in range(depth):
        if kind == "tuple":
            value = (value,)
        elif kind == "frozenset":
            value = frozenset([value])
        elif kind == "list":
            value = [value]
        elif kind == "dict":
            value = {"k": value}
        elif kind == "int-key":  # written as {"__dict__": [[key, value]]}
            value = {1: value}
        else:  # a tuple key is itself a level
            value = {(1, "a"): value} if isinstance(value, int) else {(1,): value}
    return value


@pytest.mark.parametrize("kind", ["tuple", "frozenset", "list", "dict", "int-key", "tuple-key"])
def test_everything_that_is_written_can_be_read_back(kind):
    depth = MAX_NESTING - 1 if kind == "tuple-key" else MAX_NESTING
    deepest = _nested(kind, depth)
    assert decode_value(json.loads(json.dumps(encode_value(deepest)))) == deepest
    with pytest.raises(TypeError, match="nested too deeply"):
        encode_value(_nested(kind, depth + 1))


def test_a_deeply_nested_field_is_reported_when_saving_instead_of_lost_when_loading(widget, make_widget, tmp_path):
    (holder,) = build(widget, Holder)
    holder.SetField("path", _nested("tuple", MAX_NESTING + 50))
    path = str(tmp_path / "deep.json")
    assert widget.SaveTree(path)
    with open(path, encoding="utf-8") as handle:
        saved = json.load(handle)
    fields = [node.get("fields", {}) for node in saved["nodes"] if node.get("type") == "R7Holder"]
    assert fields and "path" not in fields[0]

    holder.SetField("path", _nested("tuple", 200))
    assert widget.SaveTree(path)
    other = make_widget()
    other.RegisterNodeType(Holder)
    assert other.LoadTree(path)
    (loaded,) = [node for node in other.GetNodes() if isinstance(node, Holder)]
    assert loaded.GetField("path") == _nested("tuple", 200)
