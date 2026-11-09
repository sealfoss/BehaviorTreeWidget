"""EntryComboBox: a combo box listing blackboard entries, updated with minimal edits."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QComboBox, QGridLayout, QLineEdit, QSizePolicy, QWidget

from behavior_tree_widget.blackboard_aware import PROBLEM_COLOR, ComboItem, EntryComboBox


def rows(combo: QComboBox) -> list[tuple]:
    return [
        (
            combo.itemText(index),
            combo.itemData(index),
            combo.itemData(index, Qt.ItemDataRole.ToolTipRole),
            combo.itemData(index, Qt.ItemDataRole.ForegroundRole) is not None,
        )
        for index in range(combo.count())
    ]


@pytest.fixture
def combo(qtbot):
    combo = EntryComboBox()
    qtbot.addWidget(combo)
    combo.set_normal_tooltip("normal")
    return combo


@pytest.fixture
def calls(monkeypatch):
    counts = {"insertItem": 0, "removeItem": 0, "setItemText": 0}
    for method in counts:
        original = getattr(QComboBox, method)

        def counting(self, *args, _original=original, _method=method):
            counts[_method] += 1
            return _original(self, *args)

        monkeypatch.setattr(EntryComboBox, method, counting)
    return counts


ENTRIES = [ComboItem(name, name, "Integer entry") for name in ("a", "b", "c", "d")]


def test_set_items_shows_rows_with_data_tooltips_and_problems(combo):
    combo.set_items([ComboItem("Literal", None, "Use it"), *ENTRIES, ComboItem("x (missing)", "x", "no x", True)], "b")
    assert rows(combo) == [
        ("Literal", None, "Use it", False),
        ("a", "a", "Integer entry", False),
        ("b", "b", "Integer entry", False),
        ("c", "c", "Integer entry", False),
        ("d", "d", "Integer entry", False),
        ("x (missing)", "x", "no x", True),
    ]
    assert combo.currentText() == "b"
    assert combo.items()[0] == ComboItem("Literal", None, "Use it")


def test_plain_tuples_are_accepted(combo):
    combo.set_items([("a", "a", "tip", False), ("b", "b", "tip", True)], "a")
    assert rows(combo) == [("a", "a", "tip", False), ("b", "b", "tip", True)]


def test_current_none_chooses_a_leading_literal_row_or_nothing(combo):
    combo.set_items([ComboItem("Literal", None, "")] + ENTRIES, None)
    assert combo.currentIndex() == 0
    combo.set_items(ENTRIES, None)
    assert combo.currentIndex() == -1
    combo.set_items(ENTRIES, "zzz")  # not offered
    assert combo.currentIndex() == -1


def test_set_items_emits_no_signals(combo):
    seen = []
    combo.currentIndexChanged.connect(seen.append)
    combo.currentTextChanged.connect(seen.append)
    combo.set_items(ENTRIES, "a")
    combo.set_items(ENTRIES, "c")
    combo.set_items(ENTRIES[1:], "d")
    assert seen == []


def test_unchanged_items_make_no_edits(combo, calls):
    combo.set_items(ENTRIES, "a")
    for key in calls:
        calls[key] = 0
    combo.set_items(list(ENTRIES), "a")
    assert calls == {"insertItem": 0, "removeItem": 0, "setItemText": 0}


@pytest.mark.parametrize(
    "new, inserts, removes, renames",
    [
        (ENTRIES + [ComboItem("e", "e", "Integer entry")], 1, 0, 0),  # appended
        ([ComboItem("z", "z", "")] + ENTRIES, 1, 0, 0),  # prepended
        (ENTRIES[:2] + [ComboItem("n", "n", "")] + ENTRIES[2:], 1, 0, 0),  # inserted in the middle
        (ENTRIES[:1] + ENTRIES[2:], 0, 1, 0),  # removed in the middle
        (ENTRIES[:1] + [ComboItem("B", "B", "Integer entry")] + ENTRIES[2:], 0, 0, 1),  # renamed in place
        ([ENTRIES[1], ENTRIES[0]] + ENTRIES[2:], 0, 0, 2),  # two rows swapped
        ([], 0, 4, 0),
    ],
)
def test_only_the_rows_that_differ_are_edited(combo, calls, new, inserts, removes, renames):
    combo.set_items(ENTRIES, "a")
    for key in calls:
        calls[key] = 0
    combo.set_items(new, "a")
    assert (calls["insertItem"], calls["removeItem"], calls["setItemText"]) == (inserts, removes, renames)
    assert [row[:2] for row in rows(combo)] == [(item.text, item.data) for item in new]


def test_tooltip_and_problem_changes_of_kept_rows_are_applied(combo):
    combo.set_items(ENTRIES, "a")
    changed = [ENTRIES[0], ComboItem("b", "b", "Double entry", True), *ENTRIES[2:]]
    combo.set_items(changed, "a")
    assert rows(combo)[1] == ("b", "b", "Double entry", True)
    combo.set_items(ENTRIES, "a")
    assert rows(combo)[1] == ("b", "b", "Integer entry", False)


def test_a_chosen_problem_row_turns_the_box_red_and_explains_itself(combo):
    items = ENTRIES + [ComboItem("x (missing)", "x", "The blackboard has no entry named 'x'", True)]
    combo.set_items(items, "x")
    assert combo.palette().color(QPalette.ColorRole.ButtonText) == PROBLEM_COLOR
    assert combo.toolTip() == "The blackboard has no entry named 'x'"
    combo.set_items(items, "a")
    assert combo.palette().color(QPalette.ColorRole.ButtonText) != PROBLEM_COLOR
    assert combo.toolTip() == "normal"


def test_changes_made_from_outside_are_repaired(combo):
    combo.set_items(ENTRIES, "a")
    combo.clear()
    combo.set_items(ENTRIES, "b")
    assert [row[0] for row in rows(combo)] == ["a", "b", "c", "d"]
    assert combo.currentText() == "b"


def test_replacing_a_combo_box_keeps_its_place_name_and_properties(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    layout = QGridLayout(parent)
    before = QLineEdit(parent)
    old = QComboBox(parent)
    after = QLineEdit(parent)
    layout.addWidget(before, 0, 0)
    layout.addWidget(old, 0, 1)
    layout.addWidget(after, 0, 2)
    QWidget.setTabOrder(before, old)
    QWidget.setTabOrder(old, after)
    old.setObjectName("ValueName")
    old.setToolTip("Pick an entry")
    old.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
    old.setMaxVisibleItems(7)
    combo = EntryComboBox.replacing(old)
    assert parent.findChild(QComboBox, "ValueName") is combo
    assert old.objectName() == "" and old.isHidden()
    assert combo.parentWidget() is parent
    assert layout.itemAtPosition(0, 1).widget() is combo
    assert combo.toolTip() == "Pick an entry"
    assert combo.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Minimum
    assert combo.maxVisibleItems() == 7
    combo.set_items([ComboItem("x (missing)", "x", "missing", True)], "x")
    combo.set_items([ComboItem("a", "a", "")], "a")
    assert combo.toolTip() == "Pick an entry"  # the normal tooltip is the replaced box's
    assert old.nextInFocusChain() is combo
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # the replaced box is deleted
    assert before.nextInFocusChain() is combo  # it took the replaced box's place in the focus chain
    assert combo.nextInFocusChain() is after
    assert EntryComboBox.replacing(combo) is combo
