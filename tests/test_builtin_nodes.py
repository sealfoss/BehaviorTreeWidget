"""Tests of the built-in Negation, Evaluation and Set nodes.

Requirements (feature request "Three new nodes"):

* Negation: a node with a parent and one child; it returns False when the child returns
  True and True when the child returns False.
* Evaluation: a leaf with a "ValueName" combo box listing every blackboard value, a
  "CompareTo" combo box listing "Literal" and the other values of the same type, and a
  "LiteralValue" widget - enabled and visible only while "Literal" is chosen - whose kind
  follows the ValueName entry's type (Integer: spin box, Float: double spin box, String:
  line edit, Bool: check box). It returns True when the ValueName value equals the
  CompareTo value (or the literal), False otherwise.
* Set: the same UI; when run it sets the ValueName entry to the CompareTo value or the
  literal.
"""

from __future__ import annotations

import json
import logging

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from behavior_tree_widget import (
    CompositeNodeWidget,
    EvaluationNodeWidget,
    LeafNodeWidget,
    NegationNodeWidget,
    NodeStatus,
    SetNodeWidget,
)
from behavior_tree_widget.blackboard_nodes import (
    BlackboardValueNodeWidget,
    convert_literal,
    values_equal,
)
from behavior_tree_widget.nodes import CHILDREN, PARENT, ConnectionState

from conftest import (
    FailNode,
    RunningN,
    Succeed,
    click,
    drag,
    fast_config,
    label_point,
    run_until_idle,
    viewport_point,
)

SUCCEEDED, FAILED, RUNNING, READY = "Succeeded", "Failed", "Running", "Ready"
ALL_TYPES = {
    "i": ("Integer", 5),
    "i2": ("Integer", 9),
    "d": ("Double", 1.5),
    "d2": ("Double", 2.5),
    "s": ("String", "abc"),
    "b": ("Bool", True),
    "l": ("List", [1, 2]),
    "m": ("Dictionary", {"k": 1}),
    "z": ("Set", {1, 2}),
}


# ============================================================================ helpers
def record(signal) -> list:
    values: list = []
    signal.connect(lambda *args: values.append(args[0] if len(args) == 1 else args))
    return values


def run(qtbot, bt, **config) -> None:
    fast_config(bt, **config)
    bt.Execute()
    run_until_idle(qtbot, bt)


def combo(node, name: str) -> QComboBox:
    box = node.findChild(QComboBox, name)
    assert box is not None, name
    return box


def texts(box: QComboBox) -> list[str]:
    return [box.itemText(index) for index in range(box.count())]


def choose(box: QComboBox, text: str) -> None:
    """Pick ``text`` in a combo box as the user does (emits currentIndexChanged)."""
    index = box.findText(text)
    assert index >= 0, f"{text!r} not in {texts(box)}"
    box.setCurrentIndex(index)


def literal(node) -> QWidget:
    editor = node.literal_editor()
    assert editor.objectName() == "LiteralValue"
    assert node.findChild(QWidget, "LiteralValue") is editor
    return editor


def literal_shown(node) -> bool:
    editor = literal(node)
    label = node.findChild(QLabel, "LiteralValueLabel")
    assert label.isVisibleTo(node) is editor.isVisibleTo(node)
    return editor.isVisibleTo(node)


def add_entries(bt, names=None) -> None:
    for name, (type_name, value) in ALL_TYPES.items():
        if names is None or name in names:
            bt.AddEntry(name, type_name, value)


def under_root(bt, *nodes) -> None:
    for node in nodes:
        bt.Connect(bt.GetRootNode(), node)


# ============================================================================ Negation: structure
def test_negation_is_a_built_in_node_type(bt):
    assert "Negation" in bt.GetNodeTypes()
    node = bt.AddNode("Negation", 0, 200)
    assert isinstance(node, NegationNodeWidget)
    assert not isinstance(node, (CompositeNodeWidget, LeafNodeWidget))
    assert node.GetTypeName() == "Negation"
    assert node.GetTitle() == "Negation"
    assert node.findChild(QLabel, "Title").text() == "Negation"
    assert node.findChild(QLabel, "Status").text() == READY


def test_negation_has_a_parent_and_a_single_child_connection(bt):
    node = bt.AddNode("Negation", 0, 200)
    assert NegationNodeWidget.HasParentConnection() and NegationNodeWidget.HasChildConnections()
    assert NegationNodeWidget.MaxChildren() == 1
    parent_label = node.connection_label(PARENT)
    child_label = node.connection_label(CHILDREN)
    assert parent_label is not None and parent_label.isVisibleTo(node) and parent_label.text() == "Parent"
    assert child_label is not None and child_label.isVisibleTo(node) and child_label.text() == "Child"
    assert node.connection_state(PARENT) is ConnectionState.DISCONNECTED
    assert node.connection_state(CHILDREN) is ConnectionState.DISCONNECTED


def test_other_node_kinds_report_their_child_limits():
    assert CompositeNodeWidget.MaxChildren() is None
    assert LeafNodeWidget.MaxChildren() == 0
    assert EvaluationNodeWidget.MaxChildren() == 0


def test_connecting_a_second_child_replaces_the_first(bt):
    negation = bt.AddNode("Negation", 0, 200)
    first = bt.AddNode("Succeed", -100, 400)
    second = bt.AddNode("FailNode", 100, 400)
    bt.Connect(negation, first)
    assert negation.GetChild() is first
    bt.Connect(negation, second)
    assert negation.GetChildren() == [second]
    assert negation.GetChild() is second
    assert first.GetParent() is None and second.GetParent() is negation
    assert first.connection_state(PARENT) is ConnectionState.DISCONNECTED
    assert negation.connection_state(CHILDREN) is ConnectionState.CONNECTED
    assert [item.child_node for item in bt.view().connections()] == [second]


def test_dragging_a_second_child_onto_a_negation_replaces_the_first(bt):
    negation = bt.AddNode("Negation", 0, 200)
    first = bt.AddNode("Succeed", -120, 380)
    second = bt.AddNode("FailNode", 120, 380)
    bt.view().centerOn(40, 330)
    drag(bt, label_point(bt, negation, CHILDREN), label_point(bt, first, PARENT))
    assert negation.GetChild() is first
    # the line is green (compatible) over the second child's Parent label: it may replace the first
    drag(bt, label_point(bt, negation, CHILDREN), label_point(bt, second, PARENT), release=False)
    assert bt.view().drag_line().is_valid()
    QTest.mouseRelease(bt.view().viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       label_point(bt, second, PARENT))
    assert negation.GetChildren() == [second]
    assert first.GetParent() is None


def test_a_negation_can_be_a_child_and_have_a_composite_child(bt):
    negation = bt.AddNode("Negation", 0, 200)
    sequence = bt.AddNode("Sequence", 0, 400)
    bt.Connect(bt.GetRootNode(), negation)
    bt.Connect(negation, sequence)
    assert negation.GetParent() is bt.GetRootNode()
    assert sequence.GetParent() is negation
    with pytest.raises(ValueError):
        bt.Connect(sequence, negation)  # a cycle


def test_negation_menu_has_rename_and_delete_but_no_composite_options(bt, dialogs):
    negation = bt.AddNode("Negation", 0, 200)
    menu = bt.view().build_node_menu(negation)
    names = [action.objectName() for action in menu.actions() if not action.isSeparator()]
    assert names == ["Rename", "Delete"]
    menu.deleteLater()


def test_renamed_negation_shows_its_type(bt):
    negation = bt.AddNode("Negation", 0, 200)
    negation.SetTitle("Not Done")
    assert negation.findChild(QLabel, "Title").text() == "Not Done (Negation)"


# ============================================================================ Negation: execution
@pytest.mark.parametrize("child, child_status, expected", [(Succeed, SUCCEEDED, FAILED), (FailNode, FAILED, SUCCEEDED)])
def test_negation_inverts_its_child(qtbot, bt, child, child_status, expected):
    negation = bt.AddNode("Negation", 0, 200)
    leaf = bt.AddNode(child, 0, 400)
    bt.Connect(bt.GetRootNode(), negation)
    bt.Connect(negation, leaf)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [expected]
    assert leaf.GetStatus().value == child_status
    assert negation.GetStatus().value == expected
    assert negation.findChild(QLabel, "Status").text() == expected


def test_negation_runs_while_its_child_runs(qtbot, bt):
    negation = bt.AddNode("Negation", 0, 200)
    leaf = bt.AddNode(RunningN, 0, 400)
    leaf.SetField("ticks", 3)
    bt.Connect(bt.GetRootNode(), negation)
    bt.Connect(negation, leaf)
    seen = []
    bt.executor().ticked.connect(lambda _count: seen.append((leaf.GetStatus().value, negation.GetStatus().value)))
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert seen[0] == (RUNNING, RUNNING)
    assert seen[-1] == (SUCCEEDED, FAILED)
    assert finished == [FAILED]


def test_double_negation_restores_the_result(qtbot, bt):
    outer = bt.AddNode("Negation", 0, 200)
    inner = bt.AddNode("Negation", 0, 350)
    leaf = bt.AddNode(Succeed, 0, 500)
    bt.Connect(bt.GetRootNode(), outer)
    bt.Connect(outer, inner)
    bt.Connect(inner, leaf)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [SUCCEEDED]
    assert [node.GetStatus().value for node in (outer, inner, leaf)] == [SUCCEEDED, FAILED, SUCCEEDED]


def test_negation_inside_a_selector(qtbot, bt):
    root = bt.GetRootNode()
    root.SetCompositeType("Selector")
    negation = bt.AddNode("Negation", -150, 200)
    bt.Connect(negation, bt.AddNode(Succeed, -150, 400))
    fallback = bt.AddNode(Succeed, 150, 200)
    bt.Connect(root, negation)
    bt.Connect(root, fallback)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [SUCCEEDED]
    assert negation.GetStatus() is NodeStatus.FAILED
    assert fallback.GetStatus() is NodeStatus.SUCCEEDED


def test_negation_without_a_child_fails_with_an_error(qtbot, bt):
    negation = bt.AddNode("Negation", 0, 200)
    bt.Connect(bt.GetRootNode(), negation)
    errors = record(bt.nodeError)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [FAILED]
    assert negation.GetStatus() is NodeStatus.FAILED
    assert "child" in negation.GetError()
    assert negation.findChild(QLabel, "Status").toolTip() == negation.GetError()
    assert errors == [(negation, negation.GetError())]


def test_unconnected_negation_is_ignored(qtbot, bt):
    bt.AddNode("Negation", 300, 200)
    bt.Connect(bt.GetRootNode(), bt.AddNode(Succeed, 0, 200))
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [SUCCEEDED]


# ============================================================================ Negation: files
def test_negation_is_saved_and_loaded(make_widget, bt, tmp_path):
    negation = bt.AddNode("Negation", 40, 220)
    negation.SetTitle("Not Failed")
    leaf = bt.AddNode(FailNode, 40, 420)
    bt.Connect(bt.GetRootNode(), negation)
    bt.Connect(negation, leaf)
    path = tmp_path / "negation.json"
    assert bt.SaveTree(str(path))
    saved = {node["id"]: node for node in json.loads(path.read_text(encoding="utf-8"))["nodes"]}
    assert saved[negation.GetId()]["type"] == "Negation"
    assert saved[negation.GetId()]["title"] == "Not Failed"

    other = make_widget()
    assert other.LoadTree(str(path))
    loaded = {node.GetId(): node for node in other.GetNodes()}
    copy = loaded[negation.GetId()]
    assert isinstance(copy, NegationNodeWidget)
    assert copy.GetTitle() == "Not Failed"
    assert (copy._item.pos().x(), copy._item.pos().y()) == (40.0, 220.0)
    assert copy.GetChild() is loaded[leaf.GetId()]
    assert copy.GetParent() is other.GetRootNode()


def test_loading_a_negation_with_two_children_keeps_the_first(make_widget, bt, tmp_path, caplog):
    negation = bt.AddNode("Negation", 0, 200)
    first = bt.AddNode(Succeed, -100, 400)
    second = bt.AddNode(FailNode, 100, 400)
    bt.Connect(negation, first)
    path = tmp_path / "two.json"
    assert bt.SaveTree(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["connections"].append({"parent": negation.GetId(), "child": second.GetId()})  # hand-edited file
    path.write_text(json.dumps(data), encoding="utf-8")

    other = make_widget()
    with caplog.at_level(logging.WARNING, logger="behavior_tree_widget"):
        assert other.LoadTree(str(path))
    loaded = {node.GetId(): node for node in other.GetNodes()}
    assert loaded[negation.GetId()].GetChildren() == [loaded[first.GetId()]]
    assert loaded[second.GetId()].GetParent() is None
    assert any("accepts only 1 child" in message for message in caplog.messages)


# ============================================================================ Evaluation / Set: ui
@pytest.mark.parametrize("type_name, cls, title, second_label", [
    ("Evaluation", EvaluationNodeWidget, "Evaluation", "Compare To"),
    ("Set", SetNodeWidget, "Set", "Set To"),
])
def test_blackboard_nodes_are_built_in_leaf_types(bt, type_name, cls, title, second_label):
    assert type_name in bt.GetNodeTypes()
    assert bt.GetNodeType(type_name) is cls
    node = bt.AddNode(type_name, 0, 200)
    assert type(node) is cls
    assert isinstance(node, LeafNodeWidget)
    assert node.GetTitle() == title
    assert node.connection_label(PARENT) is not None and node.connection_label(CHILDREN) is None
    for name in ("ValueName", "CompareTo"):
        assert isinstance(node.findChild(QComboBox, name), QComboBox)
    assert node.findChild(QWidget, "LiteralValue") is not None
    labels = {label.objectName(): label.text() for label in node.findChildren(QLabel)}
    assert labels["ValueNameLabel"] == "Value Name"
    assert labels["CompareToLabel"] == second_label
    assert labels["LiteralValueLabel"] == "Literal Value"


@pytest.mark.parametrize("type_name", ["Evaluation", "Set"])
def test_value_name_lists_every_blackboard_entry(bt, type_name):
    add_entries(bt)
    bt.SetEntry(object(), "obj")  # an Object entry (set from code)
    node = bt.AddNode(type_name, 0, 200)
    box = combo(node, "ValueName")
    assert texts(box) == [*ALL_TYPES, "obj"]
    assert box.currentIndex() == -1 and box.placeholderText() == "Select a value"
    assert node.GetValueName() == ""


def test_new_node_starts_with_literal_and_a_disabled_literal_editor(bt):
    add_entries(bt)
    node = bt.AddNode("Evaluation", 0, 200)
    assert texts(combo(node, "CompareTo")) == ["Literal"]
    assert combo(node, "CompareTo").currentText() == "Literal"
    assert node.UsesLiteral() and node.GetCompareTo() is None
    assert literal_shown(node)
    assert not literal(node).isEnabled()  # nothing to type before a value is chosen


def test_value_name_follows_blackboard_changes(bt):
    bt.AddEntry("a", "Integer", 1)
    node = bt.AddNode("Evaluation", 0, 200)
    box = combo(node, "ValueName")
    bt.AddEntry("b", "String", "x")
    assert texts(box) == ["a", "b"]
    choose(box, "b")
    bt.RemoveEntry("a")
    assert texts(box) == ["b"] and box.currentText() == "b"
    bt.AddEntry("c", "Bool", False)
    assert texts(box) == ["b", "c"] and box.currentText() == "b"


def test_entries_created_by_a_running_worker_thread_appear(qtbot, bt):
    class Creator(LeafNodeWidget):
        TYPE_NAME = "BuiltinTestCreator"

        def OnRun(self, tree):  # runs on a worker thread
            tree.SetEntry(42, "created_at_run_time")
            return True

    node = bt.AddNode("Evaluation", 300, 200)
    bt.Connect(bt.GetRootNode(), bt.AddNode(Creator, 0, 200))
    run(qtbot, bt)
    qtbot.waitUntil(lambda: "created_at_run_time" in texts(combo(node, "ValueName")), timeout=2000)


def test_compare_to_lists_literal_and_the_other_entries_of_the_same_type(bt):
    add_entries(bt)
    bt.AddEntry("i3", "Integer", 0)
    node = bt.AddNode("Evaluation", 0, 200)
    choose(combo(node, "ValueName"), "i")
    assert texts(combo(node, "CompareTo")) == ["Literal", "i2", "i3"]
    choose(combo(node, "ValueName"), "s")
    assert texts(combo(node, "CompareTo")) == ["Literal"]  # no other String entry
    choose(combo(node, "ValueName"), "d2")
    assert texts(combo(node, "CompareTo")) == ["Literal", "d"]


@pytest.mark.parametrize("entry, widget_class, value", [
    ("i", QSpinBox, 0),
    ("d", QDoubleSpinBox, 0.0),
    ("s", QLineEdit, ""),
    ("b", QCheckBox, False),
])
def test_literal_value_widget_matches_the_entry_type(bt, entry, widget_class, value):
    add_entries(bt)
    node = bt.AddNode("Evaluation", 0, 200)
    choose(combo(node, "ValueName"), entry)
    editor = literal(node)
    assert isinstance(editor, widget_class)
    assert node.findChild(widget_class, "LiteralValue") is editor
    assert literal_shown(node) and editor.isEnabled()
    assert node.GetLiteralValue() == value and type(node.GetLiteralValue()) is type(value)


@pytest.mark.parametrize("entry", ["l", "m", "z"])
def test_container_entries_take_a_python_literal(bt, entry):
    add_entries(bt)
    node = bt.AddNode("Evaluation", 0, 200)
    choose(combo(node, "ValueName"), entry)
    editor = literal(node)
    assert isinstance(editor, QLineEdit) and editor.isEnabled()
    editor.setText({"l": "[3, 'x']", "m": "{'a': 2}", "z": "{4, 5}"}[entry])
    assert node.GetLiteralValue() == {"l": [3, "x"], "m": {"a": 2}, "z": {4, 5}}[entry]
    stored = node.GetLiteralValue()
    editor.setText("[1, ")  # not a literal (yet): the stored value is kept and the text is red
    assert node.GetLiteralValue() == stored
    assert editor.palette().color(QPalette.ColorRole.Text).name() == "#c62828"
    editor.setText({"l": "[]", "m": "{}", "z": "set()"}[entry])
    assert editor.palette().color(QPalette.ColorRole.Text).name() != "#c62828"


def test_literal_value_is_shown_only_while_literal_is_chosen(bt):
    add_entries(bt)
    node = bt.AddNode("Evaluation", 0, 200)
    choose(combo(node, "ValueName"), "i")
    with_literal = node._item.size().height()
    choose(combo(node, "CompareTo"), "i2")
    assert node.GetCompareTo() == "i2" and not node.UsesLiteral()
    assert not literal_shown(node)
    assert not literal(node).isEnabled()
    assert node._item.size().height() < with_literal  # the node shrinks
    choose(combo(node, "CompareTo"), "Literal")
    assert node.GetCompareTo() is None
    assert literal_shown(node) and literal(node).isEnabled()
    assert node._item.size().height() == with_literal


def test_editing_the_literal_widgets_updates_the_node(bt):
    add_entries(bt)
    node = bt.AddNode("Set", 0, 200)
    edits = record(node.fieldEdited)
    choose(combo(node, "ValueName"), "i")
    literal(node).setValue(17)
    assert node.GetLiteralValue() == 17
    choose(combo(node, "ValueName"), "d")
    literal(node).setValue(2.25)
    assert node.GetLiteralValue() == 2.25
    choose(combo(node, "ValueName"), "s")
    literal(node).setText("hello")
    assert node.GetLiteralValue() == "hello"
    choose(combo(node, "ValueName"), "b")
    literal(node).setChecked(True)
    assert node.GetLiteralValue() is True
    assert literal(node).text() == "True"
    assert "LiteralValue" in edits and "ValueName" in edits
    assert bt.IsModified()


def test_typing_into_the_literal_spin_box_in_the_view(qtbot, bt):
    add_entries(bt)
    node = bt.AddNode("Evaluation", 0, 200)
    choose(combo(node, "ValueName"), "i")
    bt.view().centerOn(node._item.sceneBoundingRect().center())
    click(bt, viewport_point(bt, node, literal(node)))
    QTest.keyClick(bt.view(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(bt.view(), "23")
    assert node.GetLiteralValue() == 23


def test_choosing_a_value_name_with_the_keyboard_in_the_view(qtbot, bt):
    bt.AddEntry("first", "Integer", 1)
    bt.AddEntry("second", "String", "x")
    node = bt.AddNode("Evaluation", 0, 200)
    bt.view().centerOn(node._item.sceneBoundingRect().center())
    box = combo(node, "ValueName")
    click(bt, viewport_point(bt, node, box))
    qtbot.waitUntil(lambda: box.view().isVisible(), timeout=2000)
    QTest.keyClick(bt.view(), Qt.Key.Key_Down)
    QTest.keyClick(bt.view(), Qt.Key.Key_Down)
    QTest.keyClick(bt.view(), Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not box.view().isVisible(), timeout=2000)
    assert node.GetValueName() == "second"
    assert isinstance(literal(node), QLineEdit)


def test_changing_the_value_type_resets_compare_to_and_converts_the_literal(bt):
    add_entries(bt)
    node = bt.AddNode("Evaluation", 0, 200)
    choose(combo(node, "ValueName"), "i")
    literal(node).setValue(5)
    choose(combo(node, "ValueName"), "d")  # Integer -> Double keeps the number
    assert node.GetLiteralValue() == 5.0 and isinstance(node.GetLiteralValue(), float)
    choose(combo(node, "CompareTo"), "d2")
    choose(combo(node, "ValueName"), "s")  # d2 is no String entry: back to Literal
    assert node.GetCompareTo() is None and literal_shown(node)
    assert node.GetLiteralValue() == "5.0"
    choose(combo(node, "ValueName"), "d2")  # "d2" itself was the CompareTo before; Literal stays
    assert texts(combo(node, "CompareTo")) == ["Literal", "d"]


def test_missing_entries_are_kept_and_shown_in_red(bt):
    add_entries(bt, ["i", "i2"])
    node = bt.AddNode("Evaluation", 0, 200)
    node.SetValueName("i")
    node.SetCompareTo("i2")
    bt.RemoveEntry("i2")
    compare = combo(node, "CompareTo")
    assert compare.currentText() == "i2 (missing)"
    assert node.GetCompareTo() == "i2"
    assert compare.palette().color(QPalette.ColorRole.ButtonText).name() == "#c62828"
    bt.RemoveEntry("i")
    value = combo(node, "ValueName")
    assert value.currentText() == "i (missing)"
    assert node.GetValueName() == "i"
    bt.AddEntry("i", "Integer", 3)  # the entries come back: nothing is lost
    bt.AddEntry("i2", "Integer", 3)
    assert value.currentText() == "i" and compare.currentText() == "i2"
    assert compare.palette().color(QPalette.ColorRole.ButtonText).name() != "#c62828"


def test_compare_to_entries_the_list_would_not_offer_are_marked(bt):
    add_entries(bt, ["i", "s"])
    node = bt.AddNode("Evaluation", 0, 200)
    node.SetValueName("i")
    node.SetCompareTo("s")  # from code: not offered by the combo box, shown as a problem
    box = combo(node, "CompareTo")
    assert box.currentText() == "s (String)"
    assert box.toolTip() == "'s' is an entry of type String, not Integer"
    node.SetCompareTo("i")
    assert box.currentText() == "i (itself)"


def test_renaming_an_entry_updates_the_nodes(bt):
    add_entries(bt, ["i", "i2"])
    node = bt.AddNode("Set", 0, 200)
    node.SetValueName("i")
    node.SetCompareTo("i2")
    bt._set_modified(False)
    bt.blackboardStore().rename("i", "count")
    bt.blackboardStore().rename("i2", "limit")
    assert node.GetValueName() == "count" and node.GetCompareTo() == "limit"
    assert combo(node, "ValueName").currentText() == "count"
    assert combo(node, "CompareTo").currentText() == "limit"
    assert bt.IsModified()


def test_renaming_an_entry_in_the_blackboard_tab_updates_the_nodes(bt):
    add_entries(bt, ["i"])
    node = bt.AddNode("Evaluation", 0, 200)
    node.SetValueName("i")
    row = bt.blackboardView().row("i")
    row.name_edit.setText("renamed")
    row.name_edit.editingFinished.emit()
    assert node.GetValueName() == "renamed"


@pytest.mark.parametrize("value, type_name, expected", [
    (5, "Double", 5.0),
    (2.6, "Integer", 3),
    ("12", "Integer", 12),
    ("x", "Integer", 0),
    (True, "Integer", 1),
    (5, "String", "5"),
    (None, "String", ""),
    (0, "Bool", False),
    ("yes", "Bool", True),
    ([1, 2], "Set", {1, 2}),
    ("[1, 2]", "List", [1, 2]),
    (3, "List", []),
    (None, "Dictionary", {}),
])
def test_convert_literal(value, type_name, expected):
    converted = convert_literal(value, type_name)
    assert converted == expected and type(converted) is type(expected)


def test_values_equal_does_not_mix_bools_and_numbers():
    assert values_equal(1, 1.0)
    assert values_equal([1, {2}], [1, {2}])
    assert not values_equal(True, 1)
    assert not values_equal(0, False)
    assert values_equal(False, False)


# ============================================================================ Evaluation / Set: API
def test_typed_api(bt):
    add_entries(bt, ["i", "i2"])
    node = bt.AddNode("Evaluation", 0, 200)
    node.SetValueName("i")
    node.SetCompareTo("i2")
    assert node.GetValueName() == "i" and node.GetCompareTo() == "i2" and not node.UsesLiteral()
    node.SetCompareTo(None)
    assert node.UsesLiteral()
    node.SetLiteralValue(4)
    assert node.GetLiteralValue() == 4 and node.GetEffectiveLiteralValue() == 4
    assert literal(node).value() == 4
    assert node.GetFields() == {"ValueName": "i", "CompareTo": None, "LiteralValue": 4}
    with pytest.raises(TypeError):
        node.SetValueName(3)
    with pytest.raises(TypeError):
        node.SetCompareTo(3)
    with pytest.raises(TypeError):
        node.SetLiteralValue(object())


def test_effective_literal_follows_the_entry_type(bt):
    bt.AddEntry("d", "Double", 0.0)
    node = bt.AddNode("Evaluation", 0, 200)
    node.SetValueName("d")
    node.SetLiteralValue(2)  # an int literal for a Double entry
    assert node.GetEffectiveLiteralValue() == 2.0 and isinstance(node.GetEffectiveLiteralValue(), float)
    assert isinstance(literal(node), QDoubleSpinBox) and literal(node).value() == 2.0


# ============================================================================ Evaluation: execution
@pytest.mark.parametrize("entry, equal, different", [
    ("i", 5, 6),
    ("d", 1.5, 1.25),
    ("s", "abc", "abd"),
    ("b", True, False),
    ("l", [1, 2], [2, 1]),
    ("m", {"k": 1}, {"k": 2}),
    ("z", {2, 1}, {1}),
])
def test_evaluation_compares_with_the_literal(qtbot, bt, entry, equal, different):
    add_entries(bt)
    node = bt.AddNode("Evaluation", 0, 200)
    under_root(bt, node)
    choose(combo(node, "ValueName"), entry)
    node.SetLiteralValue(equal)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    node.SetLiteralValue(different)
    run(qtbot, bt)
    assert finished == [SUCCEEDED, FAILED]
    assert node.GetStatus() is NodeStatus.FAILED and node.GetError() is None


def test_evaluation_compares_with_another_entry(qtbot, bt):
    add_entries(bt, ["i", "i2"])
    node = bt.AddNode("Evaluation", 0, 200)
    under_root(bt, node)
    choose(combo(node, "ValueName"), "i")
    choose(combo(node, "CompareTo"), "i2")
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    bt.SetEntry(5, "i2")
    run(qtbot, bt)
    assert finished == [FAILED, SUCCEEDED]


def test_evaluation_uses_the_literal_with_the_entry_type(qtbot, bt):
    bt.AddEntry("d", "Double", 3.0)
    node = bt.AddNode("Evaluation", 0, 200)
    under_root(bt, node)
    node.SetValueName("d")
    node.SetLiteralValue("3")  # text literal for a Double entry: used as 3.0
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [SUCCEEDED]


def test_evaluation_reads_values_written_earlier_in_the_same_run(qtbot, bt):
    bt.AddEntry("count", "Integer", 0)
    setter = bt.AddNode("Set", -150, 200)
    check = bt.AddNode("Evaluation", 150, 200)
    under_root(bt, setter, check)
    setter.SetValueName("count")
    setter.SetLiteralValue(7)
    check.SetValueName("count")
    check.SetLiteralValue(7)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [SUCCEEDED]
    assert [setter.GetStatus(), check.GetStatus()] == [NodeStatus.SUCCEEDED, NodeStatus.SUCCEEDED]
    assert bt.GetEntry("count") == 7


@pytest.mark.parametrize("setup, message", [
    (lambda node: None, "no blackboard value is selected"),
    (lambda node: node.SetValueName("ghost"), "no entry named 'ghost'"),
    (lambda node: (node.SetValueName("i"), node.SetCompareTo("ghost")), "no entry named 'ghost' (Compare To)"),
    (lambda node: node.SetValueName("obj"), "Object entries cannot be used with a Literal Value"),
])
@pytest.mark.parametrize("type_name", ["Evaluation", "Set"])
def test_misconfigured_nodes_fail_with_an_error(qtbot, bt, setup, message, type_name):
    bt.AddEntry("i", "Integer", 1)
    bt.SetEntry(object(), "obj")
    node = bt.AddNode(type_name, 0, 200)
    under_root(bt, node)
    setup(node)
    errors = record(bt.nodeError)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [FAILED]
    assert node.GetStatus() is NodeStatus.FAILED
    assert message in node.GetError()
    assert errors and errors[0][0] is node


# ============================================================================ Set: execution
@pytest.mark.parametrize("entry, value", [
    ("i", 42),
    ("d", -0.5),
    ("s", "new text"),
    ("b", False),
    ("l", ["a", 1]),
    ("m", {"x": [1]}),
    ("z", {7}),
])
def test_set_writes_the_literal(qtbot, bt, entry, value):
    add_entries(bt)
    node = bt.AddNode("Set", 0, 200)
    under_root(bt, node)
    choose(combo(node, "ValueName"), entry)
    node.SetLiteralValue(value)
    finished = record(bt.executionFinished)
    run(qtbot, bt)
    assert finished == [SUCCEEDED]
    assert bt.GetEntry(entry) == value
    assert bt.GetEntryType(entry) == ALL_TYPES[entry][0]


def test_set_copies_another_entry(qtbot, bt):
    bt.AddEntry("target", "List", [])
    bt.AddEntry("source", "List", [1, [2, 3]])
    node = bt.AddNode("Set", 0, 200)
    under_root(bt, node)
    choose(combo(node, "ValueName"), "target")
    choose(combo(node, "CompareTo"), "source")
    run(qtbot, bt)
    assert bt.GetEntry("target") == [1, [2, 3]]
    bt.SetEntry([9], "source")
    assert bt.GetEntry("target") == [1, [2, 3]]  # a copy, not the same list


def test_set_does_not_mark_the_tree_modified(qtbot, bt):
    bt.AddEntry("i", "Integer", 0)
    node = bt.AddNode("Set", 0, 200)
    under_root(bt, node)
    node.SetValueName("i")
    node.SetLiteralValue(3)
    fast_config(bt)
    assert bt.SaveTree(bt.GetFilePath())
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert bt.GetEntry("i") == 3
    assert not bt.IsModified()  # a run-time change


# ============================================================================ Evaluation / Set: files and registration
def test_blackboard_nodes_are_saved_and_loaded(make_widget, bt, tmp_path):
    add_entries(bt)
    evaluation = bt.AddNode("Evaluation", -200, 200)
    evaluation.SetValueName("z")
    evaluation.SetLiteralValue({3, 4})
    setter = bt.AddNode("Set", 200, 200)
    setter.SetValueName("i")
    setter.SetCompareTo("i2")
    path = tmp_path / "bb.json"
    assert bt.SaveTree(str(path))
    saved = {node["id"]: node for node in json.loads(path.read_text(encoding="utf-8"))["nodes"]}
    assert saved[evaluation.GetId()]["type"] == "Evaluation"
    assert saved[evaluation.GetId()]["fields"] == {"ValueName": "z", "CompareTo": None, "LiteralValue": {"__set__": [3, 4]}}
    # set from code: the literal is stored as given (None) and used as the entry type's default
    assert saved[setter.GetId()]["fields"] == {"ValueName": "i", "CompareTo": "i2", "LiteralValue": None}

    other = make_widget()
    assert other.LoadTree(str(path))
    loaded = {node.GetId(): node for node in other.GetNodes()}
    copy = loaded[evaluation.GetId()]
    assert isinstance(copy, EvaluationNodeWidget)
    assert copy.GetValueName() == "z" and copy.UsesLiteral() and copy.GetLiteralValue() == {3, 4}
    assert combo(copy, "ValueName").currentText() == "z"  # the blackboard is loaded after the nodes
    assert literal(copy).text() in ("{3, 4}", "{4, 3}")
    copy = loaded[setter.GetId()]
    assert isinstance(copy, SetNodeWidget)
    assert combo(copy, "ValueName").currentText() == "i"
    assert combo(copy, "CompareTo").currentText() == "i2"
    assert not literal_shown(copy)
    assert not other.IsModified()


def test_invalid_saved_values_are_ignored(make_widget, bt, tmp_path, caplog):
    node = bt.AddNode("Evaluation", 0, 200)
    path = tmp_path / "bad.json"
    assert bt.SaveTree(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["nodes"]:
        if item["id"] == node.GetId():
            # wrong types, and an int too large for an Integer entry
            item["fields"] = {"ValueName": 3, "CompareTo": ["x"], "LiteralValue": 2**40}
    path.write_text(json.dumps(data), encoding="utf-8")
    other = make_widget()
    with caplog.at_level(logging.WARNING, logger="behavior_tree_widget"):
        assert other.LoadTree(str(path))
    loaded = {item.GetId(): item for item in other.GetNodes()}[node.GetId()]
    assert loaded.GetValueName() == "" and loaded.GetCompareTo() is None and loaded.GetLiteralValue() is None
    assert sum("ignoring saved" in message for message in caplog.messages) == 3


def test_registering_the_built_in_classes_is_allowed(bt):
    bt.RegisterNodeType(EvaluationNodeWidget)
    bt.RegisterNodeType(SetNodeWidget)
    assert bt.GetNodeType("Evaluation") is EvaluationNodeWidget


def _leaf(class_name: str, type_name: str | None = None) -> type:
    namespace = {"RUN_IN_THREAD": False, "OnRun": lambda self, tree: True}
    if type_name is not None:
        namespace["TYPE_NAME"] = type_name
    return type(class_name, (LeafNodeWidget,), namespace)


@pytest.mark.parametrize("cls", [
    _leaf("NegationLike", "Negation"),
    _leaf("EvaluationLike", "Evaluation"),
    _leaf("SetLike", "Set"),
    _leaf("Negation"),
    _leaf("Evaluation"),
    _leaf("Set"),
], ids=lambda cls: f"{cls.__name__}:{cls.__dict__.get('TYPE_NAME')}")
def test_built_in_type_names_are_reserved(bt, cls):
    with pytest.raises(ValueError):
        bt.RegisterNodeType(cls)


def test_the_shared_base_class_cannot_be_registered(bt):
    with pytest.raises(TypeError):
        bt.RegisterNodeType(BlackboardValueNodeWidget)


def test_add_menu_groups_the_built_in_nodes(bt, dialogs):
    menu = bt.view().build_add_menu(bt.view().mapToScene(QPoint(20, 20)))
    names = [action.objectName() if not action.isSeparator() else "|" for action in menu.actions()]
    assert names[:9] == ["", "Add_Sequence", "Add_Selector", "Add_Negation", "|", "Add_Evaluation", "Add_Set", "|", names[8]]
    assert names[8].startswith("Add_") and names[8] not in ("Add_Evaluation", "Add_Set")
    texts_by_name = {action.objectName(): action.text() for action in menu.actions()}
    assert texts_by_name["Add_Negation"] == "Negation"
    assert texts_by_name["Add_Evaluation"] == "Evaluation"
    assert texts_by_name["Add_Set"] == "Set"
    menu.deleteLater()


def test_a_user_leaf_titled_like_a_built_in_is_disambiguated(make_widget):
    class Setter(LeafNodeWidget):
        _title = "Set"
        RUN_IN_THREAD = False

        def OnRun(self, tree):
            return True

    widget = make_widget(node_types=[Setter])
    labels = {name: label for _, name, label in widget._node_menu_entries()}
    assert labels["Set"] == "Set"
    assert labels["Setter"] == "Set (Setter)"


def test_new_blackboard_nodes_work_on_an_empty_blackboard(bt):
    nodes = [bt.view()._add_node_from_menu(name, bt.view().mapToScene(QPoint(40, 40))) for name in ("Evaluation", "Set")]
    assert [node.GetTypeName() for node in nodes] == ["Evaluation", "Set"]
    for node in nodes:
        assert texts(combo(node, "ValueName")) == [] and texts(combo(node, "CompareTo")) == ["Literal"]
        assert literal_shown(node) and not literal(node).isEnabled()
    bt.AddEntry("entry", "String", "")
    for node in nodes:
        assert texts(combo(node, "ValueName")) == ["entry"]
