"""How the Evaluation, Set and Print Message nodes follow the blackboard (BlackboardAwareLeaf).

* Their combo boxes follow entries that are added, removed, renamed, retyped or reordered,
  at once (no event loop needed) and with minimal edits (adding one entry inserts one row).
* Inside a ``BlackboardStore.batch()`` they refresh once, at its end; renames are followed at once.
* Nodes that are not in a view (removed ones) ignore the blackboard.
* The node is laid out again only when its size can change.
"""

from __future__ import annotations

import threading
from collections import Counter

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QComboBox, QLabel, QWidget

from behavior_tree_widget import EvaluationNodeWidget, PrintMessageNodeWidget, SetNodeWidget
from behavior_tree_widget.blackboard_aware import PROBLEM_COLOR, BlackboardAwareLeaf, EntryComboBox
from behavior_tree_widget.blackboard_nodes import BlackboardValueNodeWidget

from conftest import tree_file


# ============================================================================ helpers
def box(node, name: str) -> QComboBox:
    found = node.findChild(QComboBox, name)
    assert found is not None, name
    return found


def texts(combo: QComboBox) -> list[str]:
    return [combo.itemText(index) for index in range(combo.count())]


def text_color(widget: QWidget):
    return widget.palette().color(QPalette.ColorRole.ButtonText)


def red(widget: QWidget) -> bool:
    return text_color(widget) == PROBLEM_COLOR


def row_is_red(combo: QComboBox, index: int) -> bool:
    from PySide6.QtCore import Qt

    brush = combo.itemData(index, Qt.ItemDataRole.ForegroundRole)
    return brush is not None and brush.color() == PROBLEM_COLOR


def tooltip(combo: QComboBox, index: int) -> str:
    from PySide6.QtCore import Qt

    return combo.itemData(index, Qt.ItemDataRole.ToolTipRole)


@pytest.fixture
def nodes(bt):
    """An Evaluation node on Integer entry "a", a Set node, and a Print Message node printing "a"."""
    bt.AddEntry("a", "Integer", 1)
    bt.AddEntry("b", "Integer", 2)
    bt.AddEntry("c", "String", "x")
    evaluation = bt.AddNode("Evaluation", 0, 200)
    evaluation.SetValueName("a")
    setter = bt.AddNode("Set", 300, 200)
    printer = bt.AddNode("Print Message", 600, 200)
    printer.SetSource("a")
    return evaluation, setter, printer


@pytest.fixture
def refreshes(monkeypatch):
    """Counts the refreshes of the editors of every blackboard-aware node, by node."""
    counts: Counter = Counter()
    for cls in (BlackboardValueNodeWidget, PrintMessageNodeWidget):
        original = cls.__dict__["_refresh_editors"]

        def counting(self, _original=original):
            counts[self] += 1
            return _original(self)

        monkeypatch.setattr(cls, "_refresh_editors", counting)
    return counts


# ============================================================================ structure changes
def test_the_nodes_share_the_blackboard_aware_base_class():
    for cls in (EvaluationNodeWidget, SetNodeWidget, PrintMessageNodeWidget):
        assert issubclass(cls, BlackboardAwareLeaf)
    assert BlackboardValueNodeWidget.ENTRY_FIELDS == ("ValueName", "CompareTo")
    assert PrintMessageNodeWidget.ENTRY_FIELDS == ("Source",)


def test_combo_boxes_are_entry_combo_boxes_with_the_ui_names_and_tooltips(bt):
    evaluation = bt.AddNode("Evaluation", 0, 200)
    printer = bt.AddNode("Print Message", 300, 200)
    for node, name, tip in (
        (evaluation, "ValueName", "The blackboard value to evaluate"),
        (evaluation, "CompareTo", "The Literal Value, or another blackboard value of the same type"),
        (printer, "Source", None),
    ):
        combos = [child for child in node.findChildren(QComboBox) if child.objectName() == name]
        assert len(combos) == 1, name
        combo = combos[0]
        assert isinstance(combo, EntryComboBox)
        assert combo.isVisibleTo(node)
        if tip is not None:
            assert combo.toolTip() == tip
    assert not isinstance(box(printer, "LogLevel"), EntryComboBox)  # not an entry list


def test_adding_an_entry_updates_every_combo_box_at_once(nodes, bt):
    evaluation, setter, printer = nodes
    bt.AddEntry("d", "Integer", 4)
    assert texts(box(evaluation, "ValueName")) == ["a", "b", "c", "d"]
    assert texts(box(evaluation, "CompareTo")) == ["Literal", "b", "d"]
    assert texts(box(setter, "ValueName")) == ["a", "b", "c", "d"]
    assert texts(box(printer, "Source")) == ["Literal", "a", "b", "c", "d"]
    assert box(evaluation, "ValueName").currentText() == "a"
    assert box(printer, "Source").currentText() == "a"


def test_removing_an_entry_updates_every_combo_box_at_once(nodes, bt):
    evaluation, setter, printer = nodes
    bt.RemoveEntry("b")
    assert texts(box(evaluation, "ValueName")) == ["a", "c"]
    assert texts(box(evaluation, "CompareTo")) == ["Literal"]
    assert texts(box(printer, "Source")) == ["Literal", "a", "c"]


def test_renaming_an_entry_renames_rows_and_chosen_names(nodes, bt):
    evaluation, _, printer = nodes
    bt.blackboardStore().rename("a", "alpha")
    assert evaluation.GetValueName() == "alpha"
    assert printer.GetSource() == "alpha"
    assert texts(box(evaluation, "ValueName")) == ["alpha", "b", "c"]
    assert box(evaluation, "ValueName").currentText() == "alpha"
    assert texts(box(printer, "Source")) == ["Literal", "alpha", "b", "c"]
    assert box(printer, "Source").currentText() == "alpha"


def test_retyping_an_entry_updates_rows_and_their_tooltips(nodes, bt):
    evaluation, _, _ = nodes
    values = box(evaluation, "ValueName")
    assert tooltip(values, 1) == "Integer entry"
    bt.blackboardStore().replace("b", "Double", 2.5)
    assert tooltip(values, 1) == "Double entry"
    assert texts(box(evaluation, "CompareTo")) == ["Literal"]  # b is no longer an Integer entry


def test_reordering_entries_reorders_rows(nodes, bt):
    evaluation, _, printer = nodes
    bt.MoveEntry("c", -2)
    assert texts(box(evaluation, "ValueName")) == ["c", "a", "b"]
    assert box(evaluation, "ValueName").currentText() == "a"
    assert texts(box(printer, "Source")) == ["Literal", "c", "a", "b"]


def test_entries_added_on_worker_threads_reach_the_combo_boxes(nodes, bt, qtbot):
    evaluation, _, _ = nodes
    thread = threading.Thread(target=lambda: bt.SetEntry(7, "from_thread"))
    thread.start()
    thread.join()
    qtbot.waitUntil(lambda: "from_thread" in texts(box(evaluation, "ValueName")), timeout=3000)


# ============================================================================ problem rows
def test_missing_and_wrong_type_choices_are_shown_in_red(nodes, bt):
    evaluation, setter, printer = nodes
    evaluation.SetCompareTo("c")  # a String entry: not comparable with an Integer
    compare = box(evaluation, "CompareTo")
    assert compare.currentText() == "c (String)"
    assert row_is_red(compare, compare.currentIndex()) and red(compare)
    assert "String" in compare.toolTip()

    setter.SetValueName("gone")
    values = box(setter, "ValueName")
    assert values.currentText() == "gone (missing)"
    assert row_is_red(values, values.currentIndex()) and red(values)
    assert "no entry named 'gone'" in values.toolTip()

    printer.SetSource("gone")
    source = box(printer, "Source")
    assert source.currentText() == "gone (missing)" and red(source)

    bt.AddEntry("gone", "Integer", 0)  # the missing entry appears: no red any more
    assert values.currentText() == "gone" and not red(values)
    assert values.toolTip() == "The blackboard value to set"  # the tooltip of SetNode.ui
    assert source.currentText() == "gone" and not red(source)
    assert not any(row_is_red(values, index) for index in range(values.count()))


# ============================================================================ batches
def test_a_batch_refreshes_each_node_once_at_its_end(nodes, bt, refreshes):
    evaluation, setter, printer = nodes
    store = bt.blackboardStore()
    with store.batch():
        bt.AddEntry("d", "Integer", 4)
        bt.AddEntry("e", "Integer", 5)
        bt.RemoveEntry("b")
        bt.MoveEntry("c", -1)
        store.replace("e", "String", "now text")
        assert sum(refreshes.values()) == 0
        assert texts(box(evaluation, "ValueName")) == ["a", "b", "c"]  # not refreshed yet
    assert refreshes == Counter({evaluation: 1, setter: 1, printer: 1})
    assert texts(box(evaluation, "ValueName")) == ["c", "a", "d", "e"]
    assert texts(box(evaluation, "CompareTo")) == ["Literal", "d"]  # e became a String entry
    assert texts(box(printer, "Source")) == ["Literal", "c", "a", "d", "e"]


def test_renames_inside_a_batch_are_followed_at_once(nodes, bt):
    evaluation, _, printer = nodes
    store = bt.blackboardStore()
    with store.batch():
        store.rename("a", "alpha")
        assert evaluation.GetValueName() == "alpha"
        assert printer.GetSource() == "alpha"
    assert box(evaluation, "ValueName").currentText() == "alpha"
    assert box(printer, "Source").currentText() == "alpha"


def test_values_changes_do_not_refresh_the_nodes(nodes, bt, refreshes):
    bt.SetEntry(5, "a")
    bt.SetEntryReset("a", True)
    assert sum(refreshes.values()) == 0


def make_tree(bt, path, entries: int) -> None:
    for index in range(entries):
        bt.AddEntry(f"e{index}", "Integer", index)
    for index in range(6):
        node = bt.AddNode("Evaluation" if index % 2 else "Set", index * 240, 200)
        node.SetValueName(f"e{index % entries}")
        bt.Connect(bt.GetRootNode(), node)
    bt.Connect(bt.GetRootNode(), bt.AddNode("Print Message", 0, 500))
    bt.SaveTree(str(path))


def test_loading_a_tree_refreshes_each_node_as_often_whatever_the_number_of_entries(make_widget, tmp_path, refreshes):
    per_node = {}
    for entries in (2, 25):
        source = make_widget()
        source.NewTree(str(tmp_path / f"src{entries}.json"))
        make_tree(source, tmp_path / f"t{entries}.json", entries)
        target = make_widget()
        refreshes.clear()
        assert target.LoadTree(tree_file(tmp_path / f"t{entries}.json"))
        loaded = [node for node in target.GetNodes() if isinstance(node, BlackboardAwareLeaf)]
        assert len(loaded) == 7
        per_node[entries] = sorted(refreshes[node] for node in loaded)
        assert texts(box(loaded[0], "ValueName" if not isinstance(loaded[0], PrintMessageNodeWidget) else "Source"))
    assert per_node[2] == per_node[25], "refreshes must not grow with the number of entries"
    assert max(per_node[25]) <= 4


# ============================================================================ nodes that are not shown
def test_removed_nodes_ignore_the_blackboard(nodes, bt, monkeypatch):
    evaluation, _, printer = nodes
    calls = []
    for node in (evaluation, printer):
        monkeypatch.setattr(node, "_refresh_editors", lambda node=node: calls.append(node))
    bt.RemoveNode(evaluation)
    bt.RemoveNode(printer)
    bt.SaveTree(bt.GetFilePath())
    assert not bt.IsModified()
    store = bt.blackboardStore()
    bt.AddEntry("d", "Integer", 4)
    store.rename("a", "alpha")
    with store.batch():
        bt.AddEntry("e", "Integer", 5)
    assert calls == []
    assert evaluation.GetValueName() == "a"  # a removed node no longer follows renames ...
    assert printer.GetSource() == "a"
    assert not bt.IsModified()  # ... so it cannot mark the tree as modified


def test_nodes_in_the_view_that_follow_a_rename_modify_the_tree(nodes, bt):
    bt.SaveTree(bt.GetFilePath())
    assert not bt.IsModified()
    bt.blackboardStore().rename("a", "alpha")
    assert bt.IsModified()  # their fields (saved in the tree file) changed


# ============================================================================ minimal edits
@pytest.fixture
def edits(monkeypatch):
    """Counts insertItem / removeItem / clear calls of every EntryComboBox, by combo box."""
    counts: Counter = Counter()
    for method in ("insertItem", "removeItem", "clear"):
        original = getattr(QComboBox, method)

        def counting(self, *args, _original=original, _method=method):
            counts[(self, _method)] += 1
            return _original(self, *args)

        monkeypatch.setattr(EntryComboBox, method, counting)
    return counts


def test_one_entry_more_or_less_is_one_row_more_or_less(bt, edits):
    for index in range(10):
        bt.AddEntry(f"e{index}", "Integer", index)
    evaluation = bt.AddNode("Evaluation", 0, 200)
    evaluation.SetValueName("e0")
    printer = bt.AddNode("Print Message", 300, 200)
    combos = [box(evaluation, "ValueName"), box(evaluation, "CompareTo"), box(printer, "Source")]
    edits.clear()
    bt.AddEntry("e10", "Integer", 10)
    for combo in combos:
        assert edits[(combo, "insertItem")] == 1, combo.objectName()
        assert edits[(combo, "removeItem")] == 0
        assert edits[(combo, "clear")] == 0
    edits.clear()
    bt.RemoveEntry("e5")
    for combo in combos:
        assert edits[(combo, "insertItem")] == 0
        assert edits[(combo, "removeItem")] == 1
    edits.clear()
    bt.blackboardStore().rename("e3", "x3")  # changed in place
    bt.MoveEntry("e7", -1)
    assert sum(edits.values()) == 0
    assert texts(combos[0]) == ["e0", "e1", "e2", "x3", "e4", "e7", "e6", "e8", "e9", "e10"]
    assert texts(combos[2]) == ["Literal", "e0", "e1", "e2", "x3", "e4", "e7", "e6", "e8", "e9", "e10"]


# ============================================================================ layout
def test_the_node_is_laid_out_again_only_when_its_size_can_change(nodes, bt, monkeypatch):
    evaluation, _, printer = nodes
    calls = []
    for node in (evaluation, printer):
        monkeypatch.setattr(node, "_notify_geometry", lambda node=node: calls.append(node))
    bt.AddEntry("d", "Integer", 4)
    bt.RemoveEntry("d")
    bt.MoveEntry("c", -1)
    evaluation.SetLiteralValue(3)
    printer.SetMessage("hello")
    assert calls == []
    evaluation.SetCompareTo("b")  # the Literal Value editor is hidden
    assert calls == [evaluation]
    evaluation.SetCompareTo(None)
    assert calls == [evaluation, evaluation]
    calls.clear()
    printer.SetSource(None)  # the Message editor is shown
    assert calls == [printer]
    calls.clear()
    evaluation.SetValueName("c")  # a String entry: the literal editor becomes a line edit
    assert calls == [evaluation]


def test_the_literal_editor_and_label_follow_the_choice(nodes):
    evaluation, _, printer = nodes
    label = evaluation.findChild(QLabel, "LiteralValueLabel")
    evaluation.SetCompareTo("b")
    assert evaluation.literal_editor().isHidden() and label.isHidden()
    evaluation.SetCompareTo(None)
    assert not evaluation.literal_editor().isHidden() and not label.isHidden()
    assert printer.findChild(QLabel, "MessageLabel").isHidden()  # Source "a" is chosen
