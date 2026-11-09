"""Moving blackboard entries up and down, and the "Resets" check box.

Requirements:
1) The "Reset" check box shown for blackboard entries is labelled "Resets" (the API names
   GetEntryReset / SetEntryReset and the file key "reset" are unchanged).
2) Up and down arrows on each blackboard entry move that key/value up or down in the list of
   entries; the moves persist (the blackboard file lists the entries in display order).
   With "Restore blackboard values on Stop / Reset" (TreeConfig.restore_blackboard), Stop and
   Reset put the entries back in the order they had when the run started (a move made during
   the run is undone like any other change); a move made while the tree is idle persists.
"""

from __future__ import annotations

import os
import random
import threading
from xml.etree import ElementTree

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QToolButton

import behavior_tree_widget.blackboard as blackboard_module
from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget, Status
from behavior_tree_widget._ui import _read_ui
from behavior_tree_widget.blackboard import ENTRY_TYPES, BlackboardStore

from conftest import assert_blackboard_in_file, fast_config, file_blackboard, read_json, run_until_idle, tree_file

LEFT = Qt.MouseButton.LeftButton

SAMPLE_VALUES = {
    "Integer": 3,
    "Double": 1.5,
    "String": "text",
    "Bool": True,
    "List": [1, 2],
    "Dictionary": {"k": 1},
    "Set": {1, 2},
    "Object": object(),
}


# ============================================================================ test node types
class Forever(LeafNodeWidget):
    """Adds 1 to "count" and keeps running (GUI thread)."""

    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("count") + 1, "count")
        return Status.RUNNING


class ThreadedMover(LeafNodeWidget):
    """Moves entry "c" to the top of the list from a worker thread, then succeeds."""

    results: list = []

    def OnRun(self, tree):
        type(self).results.append((threading.current_thread() is threading.main_thread(), tree.MoveEntry("c", -10)))
        return True


class Bump(LeafNodeWidget):
    """Adds 1 to "count" and succeeds (GUI thread)."""

    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(tree.GetEntry("count") + 1, "count")
        return True


@pytest.fixture
def tree(bt):
    for cls in (Forever, ThreadedMover, Bump):
        bt.RegisterNodeType(cls)
    ThreadedMover.results = []
    fast_config(bt)
    return bt


@pytest.fixture
def store(qapp):
    store = BlackboardStore()
    yield store
    store.dispose(notify=False)


# ============================================================================ helpers
def add_entries(bt, *names):
    for index, name in enumerate(names):
        bt.AddEntry(name, "Integer", index)


def layout_names(view) -> list[str]:
    """Names of the rows in the order of the view's layout (what the user sees)."""
    layout = view._rows_layout
    widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
    return [widget.name for widget in widgets if widget is not None]


def arrow_states(view) -> list[tuple[bool, bool]]:
    """(up enabled, down enabled) of every row, in display order."""
    return [(row.move_up_button.isEnabled(), row.move_down_button.isEnabled()) for row in view.rows()]


def expected_states(count: int) -> list[tuple[bool, bool]]:
    return [(index > 0, index < count - 1) for index in range(count)]


def assert_view_in_order(view, names: list[str]) -> None:
    assert view._store.names() == names
    assert [row.name for row in view.rows()] == names
    assert layout_names(view) == names
    assert arrow_states(view) == expected_states(len(names))


def wait_view_in_order(qtbot, view, names: list[str], timeout: int = 3000) -> None:
    qtbot.waitUntil(
        lambda: layout_names(view) == names and arrow_states(view) == expected_states(len(names)), timeout=timeout
    )
    assert_view_in_order(view, names)


def file_names(bt) -> list[str]:
    return [item["name"] for item in (file_blackboard(bt.GetFilePath()) or [])]


def show_blackboard(qtbot, bt) -> None:
    bt.setCurrentWidget(bt.blackboardView())
    qtbot.waitUntil(lambda: bt.blackboardView().isVisible(), timeout=2000)
    QApplication.processEvents()


def click_arrow(qtbot, bt, name: str, direction: str) -> None:
    """Click the up/down arrow of entry ``name`` with the mouse (the Blackboard tab is shown first)."""
    show_blackboard(qtbot, bt)
    row = bt.blackboardView().row(name)
    button = row.move_up_button if direction == "up" else row.move_down_button
    qtbot.waitUntil(button.isVisible, timeout=2000)
    QTest.mouseClick(button, LEFT)


def strip_from_entry_ui(monkeypatch, folder, *child_names: str) -> None:
    """Make the entry ui files load without the widgets ``child_names`` (as a custom ui file
    would): copies of the files without them are written to ``folder`` and loaded instead."""
    original = blackboard_module.load_ui

    def stripped_copy(name: str) -> str:
        root = ElementTree.fromstring(_read_ui(name))
        for parent in list(root.iter()):
            for item in list(parent):
                widget = item.find("widget")
                if item.tag == "item" and widget is not None and widget.get("name") in child_names:
                    parent.remove(item)
        path = folder / f"stripped_{name}"
        path.write_bytes(ElementTree.tostring(root, encoding="utf-8"))
        return str(path)

    def load(name, parent=None):
        if str(name).endswith("Entry.ui") and not os.path.isabs(str(name)):
            name = stripped_copy(str(name))
        return original(name, parent)

    monkeypatch.setattr(blackboard_module, "load_ui", load)


def chain(bt, *types):
    nodes = []
    for index, type_name in enumerate(types):
        node = bt.AddNode(type_name, index * 220, 200)
        bt.Connect(bt.GetRootNode(), node)
        nodes.append(node)
    return nodes


def run_in_thread(target):
    errors: list[BaseException] = []

    def run():
        try:
            target()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    assert not errors, errors


# ============================================================================ the arrows
@pytest.mark.parametrize("type_name", list(ENTRY_TYPES))
def test_every_entry_type_has_up_and_down_arrows(bt, type_name):
    bt.AddEntry("entry", type_name, SAMPLE_VALUES[type_name])
    row = bt.blackboardView().row("entry")
    up, down = row.move_up_button, row.move_down_button
    assert isinstance(up, QToolButton) and isinstance(down, QToolButton)
    assert up.objectName() == "MoveUp" and down.objectName() == "MoveDown"
    assert up.arrowType() == Qt.ArrowType.UpArrow
    assert down.arrowType() == Qt.ArrowType.DownArrow
    assert up.isVisibleTo(row) and down.isVisibleTo(row)
    assert "up" in up.toolTip().lower() and "down" in down.toolTip().lower()
    # they come from the entry's ui file (its FrameOrder frame)
    assert up.parentWidget().objectName() == "FrameOrder"
    assert down.parentWidget() is up.parentWidget()
    # a click must not take the focus from a value being edited
    assert up.focusPolicy() == Qt.FocusPolicy.NoFocus and down.focusPolicy() == Qt.FocusPolicy.NoFocus


@pytest.mark.parametrize("type_name", list(ENTRY_TYPES))
def test_every_entry_type_can_be_moved_with_its_arrows(bt, type_name):
    bt.AddEntry("first", "Integer", 0)
    bt.AddEntry("entry", type_name, SAMPLE_VALUES[type_name])
    view = bt.blackboardView()
    view.row("entry").move_up_button.click()
    assert_view_in_order(view, ["entry", "first"])
    view.row("entry").move_down_button.click()
    assert_view_in_order(view, ["first", "entry"])
    assert bt.GetEntryType("entry") == type_name


def test_arrows_are_created_for_a_ui_file_without_them(monkeypatch, qtbot, bt, tmp_path):
    strip_from_entry_ui(monkeypatch, tmp_path, "MoveUp", "MoveDown")
    add_entries(bt, "a", "b", "c")
    view = bt.blackboardView()
    row = view.row("b")
    assert row.move_up_button.objectName() == "MoveUp" and row.move_down_button.objectName() == "MoveDown"
    assert row.move_up_button.arrowType() == Qt.ArrowType.UpArrow
    assert row.move_down_button.arrowType() == Qt.ArrowType.DownArrow
    assert row.move_up_button.isVisibleTo(row) and row.move_down_button.isVisibleTo(row)
    assert row.move_up_button.toolTip() and row.move_down_button.toolTip()
    assert arrow_states(view) == expected_states(3)
    click_arrow(qtbot, bt, "c", "up")
    assert_view_in_order(view, ["a", "c", "b"])


# ============================================================================ enabled arrows
def test_a_single_entry_cannot_move(bt):
    bt.AddEntry("only", "Integer", 1)
    assert arrow_states(bt.blackboardView()) == [(False, False)]


def test_two_entries(bt):
    add_entries(bt, "a", "b")
    assert arrow_states(bt.blackboardView()) == [(False, True), (True, False)]


@pytest.mark.parametrize("count", [3, 4, 7])
def test_three_or_more_entries(bt, count):
    add_entries(bt, *[f"e{index}" for index in range(count)])
    assert arrow_states(bt.blackboardView()) == expected_states(count)


def test_states_follow_added_entries(bt):
    view = bt.blackboardView()
    bt.AddEntry("a", "Integer", 0)
    assert arrow_states(view) == expected_states(1)
    bt.AddEntry("b", "String", "x")
    assert arrow_states(view) == expected_states(2)
    bt.SetEntry([1], "c")  # created by SetEntry
    assert arrow_states(view) == expected_states(3)
    assert view.row("b").move_down_button.isEnabled()  # no longer the last one


def test_states_follow_removed_entries(bt):
    add_entries(bt, "a", "b", "c", "d")
    view = bt.blackboardView()
    bt.RemoveEntry("d")
    assert_view_in_order(view, ["a", "b", "c"])
    assert not view.row("c").move_down_button.isEnabled()  # the new last entry
    bt.RemoveEntry("a")
    assert_view_in_order(view, ["b", "c"])
    assert not view.row("b").move_up_button.isEnabled()  # the new first entry
    bt.RemoveEntry("c")
    assert_view_in_order(view, ["b"])


def test_states_follow_entries_removed_with_the_remove_button(bt):
    add_entries(bt, "a", "b", "c", "d")
    view = bt.blackboardView()
    view.row("a").select_box.setChecked(True)
    view.row("d").select_box.setChecked(True)
    view.remove_button.click()
    assert_view_in_order(view, ["b", "c"])


def test_states_follow_a_rename(qtbot, bt):
    add_entries(bt, "a", "b", "c")
    view = bt.blackboardView()
    bt.blackboardStore().rename("c", "last")
    assert_view_in_order(view, ["a", "b", "last"])
    edit = view.row("a").name_edit  # renamed by the user
    edit.setText("first")
    edit.editingFinished.emit()
    assert_view_in_order(view, ["first", "b", "last"])
    view.row("first").move_down_button.click()
    assert_view_in_order(view, ["b", "first", "last"])


def test_states_follow_a_move(bt):
    add_entries(bt, "a", "b", "c")
    view = bt.blackboardView()
    bt.MoveEntry("a", 2)
    assert_view_in_order(view, ["b", "c", "a"])
    assert not view.row("a").move_down_button.isEnabled() and view.row("a").move_up_button.isEnabled()
    assert not view.row("b").move_up_button.isEnabled() and view.row("b").move_down_button.isEnabled()


def test_states_follow_a_change_of_type(bt):
    add_entries(bt, "a", "b", "c")
    view = bt.blackboardView()
    old_row = view.row("b")
    bt.blackboardStore().replace("b", "String", "text")
    new_row = view.row("b")
    assert new_row is not old_row and new_row.type_name == "String"
    assert_view_in_order(view, ["a", "b", "c"])
    assert new_row.move_up_button.isEnabled() and new_row.move_down_button.isEnabled()
    bt.blackboardStore().replace("c", "List", [1])
    assert_view_in_order(view, ["a", "b", "c"])
    assert not view.row("c").move_down_button.isEnabled()


def test_states_follow_entries_added_from_a_worker_thread(qtbot, bt):
    add_entries(bt, "a", "b")
    view = bt.blackboardView()
    run_in_thread(lambda: bt.AddEntry("c", "Integer", 2))
    wait_view_in_order(qtbot, view, ["a", "b", "c"])


def test_a_move_before_the_row_of_a_worker_thread_entry_exists(qtbot, bt):
    add_entries(bt, "a", "b")
    view = bt.blackboardView()
    run_in_thread(lambda: bt.AddEntry("x", "Integer", 9))  # its row is created when the queued signal arrives
    assert bt.MoveEntry("x", -2) == 0
    wait_view_in_order(qtbot, view, ["x", "a", "b"])


# ============================================================================ clicking the arrows
def test_clicking_the_down_arrow_moves_the_entry_down(qtbot, bt):
    add_entries(bt, "a", "b", "c")
    click_arrow(qtbot, bt, "a", "down")
    assert_view_in_order(bt.blackboardView(), ["b", "a", "c"])


def test_clicking_the_up_arrow_moves_the_entry_up(qtbot, bt):
    add_entries(bt, "a", "b", "c")
    click_arrow(qtbot, bt, "c", "up")
    assert_view_in_order(bt.blackboardView(), ["a", "c", "b"])


def test_clicking_a_disabled_arrow_does_nothing(qtbot, bt):
    add_entries(bt, "a", "b")
    view = bt.blackboardView()
    edited = []
    view.entryEdited.connect(edited.append)
    click_arrow(qtbot, bt, "a", "up")
    click_arrow(qtbot, bt, "b", "down")
    assert_view_in_order(view, ["a", "b"])
    assert edited == []


def test_repeated_clicks_move_an_entry_to_the_other_end(qtbot, bt):
    add_entries(bt, "a", "b", "c", "d")
    view = bt.blackboardView()
    for _ in range(5):  # one click more than needed: the arrow is disabled at the bottom
        click_arrow(qtbot, bt, "a", "down")
    assert_view_in_order(view, ["b", "c", "d", "a"])
    for _ in range(5):
        click_arrow(qtbot, bt, "a", "up")
    assert_view_in_order(view, ["a", "b", "c", "d"])


def test_rows_are_shown_in_the_new_order(qtbot, bt):
    add_entries(bt, "a", "b", "c")
    view = bt.blackboardView()
    click_arrow(qtbot, bt, "c", "up")
    click_arrow(qtbot, bt, "c", "up")

    def tops():
        return [row.mapTo(view, QPoint(0, 0)).y() for row in view.rows()]

    qtbot.waitUntil(lambda: tops() == sorted(tops()) and len(set(tops())) == 3, timeout=2000)
    assert [row.name for row in view.rows()] == ["c", "a", "b"]


def test_a_moved_row_keeps_its_widgets_value_flag_and_selection(qtbot, bt):
    bt.AddEntry("a", "Integer", 1)
    bt.AddEntry("b", "String", "bee", reset=True)
    view = bt.blackboardView()
    row = view.row("b")
    row.select_box.setChecked(True)
    click_arrow(qtbot, bt, "b", "up")
    assert view.row("b") is row  # moved, not re-created
    assert row.value_widget.text() == "bee" and bt.GetEntry("b") == "bee"
    assert row.reset_box.isChecked() and bt.GetEntryReset("b")
    assert row.is_selected()
    assert_view_in_order(view, ["b", "a"])


def test_a_name_being_edited_survives_a_click_on_an_arrow(qtbot, bt):
    add_entries(bt, "a", "b", "c")
    view = bt.blackboardView()
    show_blackboard(qtbot, bt)
    bt.activateWindow()
    qtbot.waitUntil(bt.isActiveWindow, timeout=2000)
    edit = view.row("b").name_edit
    edit.setFocus()
    qtbot.waitUntil(edit.hasFocus, timeout=2000)
    edit.selectAll()
    QTest.keyClicks(edit, "bee")
    QTest.mouseClick(view.row("b").move_up_button, LEFT)
    QTest.keyClick(edit, Qt.Key.Key_Return)
    QApplication.processEvents()
    assert_view_in_order(view, ["bee", "a", "c"])


def test_clicking_emits_entry_edited(qtbot, bt):
    add_entries(bt, "a", "b")
    edited = []
    bt.blackboardView().entryEdited.connect(edited.append)
    click_arrow(qtbot, bt, "b", "up")
    assert edited == ["b"]


# ============================================================================ MoveEntry API
def test_move_entry_returns_the_new_index(bt):
    add_entries(bt, "a", "b", "c")
    assert bt.MoveEntry("a", 1) == 1
    assert bt.GetEntryNames() == ["b", "a", "c"]
    assert bt.MoveEntry("a", -1) == 0
    assert bt.GetEntryNames() == ["a", "b", "c"]
    assert_view_in_order(bt.blackboardView(), ["a", "b", "c"])


def test_move_entry_several_places(bt):
    add_entries(bt, "a", "b", "c", "d", "e")
    assert bt.MoveEntry("a", 3) == 3
    assert bt.GetEntryNames() == ["b", "c", "d", "a", "e"]
    assert bt.MoveEntry("e", -4) == 0
    assert bt.GetEntryNames() == ["e", "b", "c", "d", "a"]
    assert bt.MoveEntry("c", 2) == 4
    assert bt.GetEntryNames() == ["e", "b", "d", "a", "c"]
    assert_view_in_order(bt.blackboardView(), ["e", "b", "d", "a", "c"])


@pytest.mark.parametrize(
    "name, offset, index, order",
    [
        ("a", -1, 0, ["a", "b", "c"]),
        ("c", 1, 2, ["a", "b", "c"]),
        ("a", 100, 2, ["b", "c", "a"]),
        ("c", -100, 0, ["c", "a", "b"]),
        ("b", 10**30, 2, ["a", "c", "b"]),
        ("b", -(10**30), 0, ["b", "a", "c"]),
        ("b", 0, 1, ["a", "b", "c"]),
    ],
)
def test_move_entry_stops_at_either_end(bt, name, offset, index, order):
    add_entries(bt, "a", "b", "c")
    reordered = []
    bt.blackboardStore().entriesReordered.connect(lambda: reordered.append(True))
    assert bt.MoveEntry(name, offset) == index
    assert bt.GetEntryNames() == order
    assert len(reordered) == (0 if order == ["a", "b", "c"] else 1)
    assert_view_in_order(bt.blackboardView(), order)


def test_move_entry_with_a_single_entry(bt):
    bt.AddEntry("only", "Integer", 1)
    assert bt.MoveEntry("only", 1) == 0
    assert bt.MoveEntry("only", -1) == 0
    assert bt.GetEntryNames() == ["only"]


def test_move_entry_needs_an_existing_entry(bt):
    add_entries(bt, "a", "b")
    with pytest.raises(KeyError):
        bt.MoveEntry("missing", 1)
    assert bt.GetEntryNames() == ["a", "b"]


@pytest.mark.parametrize("offset", ["1", 1.0, 0.5, True, False, None, [1]])
def test_move_entry_needs_an_int_offset(bt, offset):
    add_entries(bt, "a", "b")
    with pytest.raises(TypeError):
        bt.MoveEntry("a", offset)
    assert bt.GetEntryNames() == ["a", "b"]


def test_move_entry_after_shutdown(bt):
    add_entries(bt, "a", "b")
    bt.Shutdown()
    with pytest.raises(RuntimeError):
        bt.MoveEntry("a", 1)


def test_store_move(store):
    reordered = []
    store.entriesReordered.connect(lambda: reordered.append(True))
    for name in ("a", "b", "c"):
        store.add(name, "Integer", 0)
    assert store.move("a", 2) == 2
    assert store.names() == ["b", "c", "a"]
    assert list(store.types()) == ["b", "c", "a"]
    assert [name for name, _, _ in store.items()] == ["b", "c", "a"]
    assert len(reordered) == 1
    assert store.move("a", 1) == 2  # already last
    assert len(reordered) == 1
    with pytest.raises(KeyError):
        store.move("missing", 1)
    with pytest.raises(TypeError):
        store.move("a", 1.0)
    store.dispose()
    with pytest.raises(RuntimeError):
        store.move("a", -1)


def test_store_move_keeps_values_types_and_flags(store):
    store.add("a", "Integer", 1, reset=True)
    store.add("b", "List", [1, 2])
    store.add("c", "String", "x")
    store.move("c", -2)
    assert store.items() == [("c", "String", "x"), ("a", "Integer", 1), ("b", "List", [1, 2])]
    assert store.reset_names() == ["a"]
    assert store.to_list() == [
        {"name": "c", "type": "String", "value": "x"},
        {"name": "a", "type": "Integer", "value": 1, "reset": True},
        {"name": "b", "type": "List", "value": [1, 2]},
    ]


# ============================================================================ entryEdited
def test_view_move_entry_emits_entry_edited_only_when_the_entry_moved(bt):
    add_entries(bt, "a", "b", "c")
    view = bt.blackboardView()
    edited = []
    view.entryEdited.connect(edited.append)
    assert view.move_entry("a", -1) == 0
    assert view.move_entry("b", 0) == 1
    assert view.move_entry("c", 5) == 2
    assert edited == []
    assert view.move_entry("a", 1) == 1
    assert edited == ["a"]
    assert view.move_entry("c", -9) == 0
    assert edited == ["a", "c"]
    assert_view_in_order(view, ["c", "b", "a"])


def test_view_move_entry_of_a_missing_entry(bt):
    add_entries(bt, "a", "b")
    view = bt.blackboardView()
    edited = []
    view.entryEdited.connect(edited.append)
    assert view.move_entry("missing", 1) == -1
    assert edited == []
    bt.Shutdown()
    assert view.move_entry("a", 1) == -1
    assert edited == []


def test_api_moves_do_not_emit_entry_edited(bt):
    add_entries(bt, "a", "b")
    edited = []
    bt.blackboardView().entryEdited.connect(edited.append)
    bt.MoveEntry("b", -1)
    assert edited == []


# ============================================================================ the blackboard file
def test_a_move_is_written_to_the_blackboard_file(bt):
    add_entries(bt, "a", "b", "c")
    assert bt.SaveTree(bt.GetFilePath())
    tree_before = read_json(tree_file(bt.GetFilePath()))
    assert bt.MoveEntry("c", -2) == 0
    assert_blackboard_in_file(bt)
    assert file_names(bt) == ["c", "a", "b"]
    assert read_json(tree_file(bt.GetFilePath())) == tree_before  # only the blackboard file is written
    assert not bt.IsModified()


def test_a_click_on_an_arrow_is_written_to_the_blackboard_file(qtbot, bt):
    bt.AddEntry("a", "Integer", 1, reset=True)
    bt.AddEntry("b", "Dictionary", {"k": [1, 2]})
    bt.AddEntry("c", "Bool", True)
    assert bt.SaveTree(bt.GetFilePath())
    saved = []
    bt.blackboardSaved.connect(saved.append)
    click_arrow(qtbot, bt, "a", "down")
    assert_blackboard_in_file(bt)
    assert file_blackboard(bt.GetFilePath()) == [
        {"name": "b", "type": "Dictionary", "value": {"k": [1, 2]}},
        {"name": "a", "type": "Integer", "value": 1, "reset": True},
        {"name": "c", "type": "Bool", "value": True},
    ]
    assert saved and saved[-1] == bt.GetBlackboardFilePath()
    assert not bt.IsModified()


def test_moves_are_written_once_after_the_interval(qtbot, bt):
    add_entries(bt, "a", "b", "c", "d")
    assert bt.SaveTree(bt.GetFilePath())
    assert_blackboard_in_file(bt)
    saved = []
    bt.blackboardSaved.connect(saved.append)
    bt.MoveEntry("d", -3)
    bt.MoveEntry("a", 1)
    bt.MoveEntry("b", 2)
    assert file_names(bt) == ["a", "b", "c", "d"]  # not written yet: the save is debounced
    qtbot.waitUntil(lambda: file_names(bt) == ["d", "a", "c", "b"], timeout=3000)
    qtbot.wait(BehaviorTreeWidget.BLACKBOARD_SAVE_INTERVAL_MS * 2)
    assert saved == [bt.GetBlackboardFilePath()]
    assert not bt.IsModified()


def test_a_move_of_an_object_entry_writes_nothing_new(qtbot, bt):
    add_entries(bt, "a", "b")
    bt.AddEntry("device", "Object", object())
    assert bt.SaveTree(bt.GetFilePath())
    assert_blackboard_in_file(bt)
    bt.MoveEntry("device", -2)
    assert bt.GetEntryNames() == ["device", "a", "b"]
    qtbot.wait(BehaviorTreeWidget.BLACKBOARD_SAVE_INTERVAL_MS * 2)
    assert file_names(bt) == ["a", "b"]  # Object entries are never saved
    assert not bt.IsModified()


def test_the_order_is_kept_when_the_tree_is_loaded_again(make_widget, bt):
    bt.AddEntry("a", "Integer", 1)
    bt.AddEntry("b", "String", "bee", reset=True)
    bt.AddEntry("c", "List", [3])
    bt.AddEntry("d", "Double", 4.5)
    assert bt.SaveTree(bt.GetFilePath())
    bt.MoveEntry("d", -3)  # d, a, b, c
    bt.MoveEntry("a", 1)  # d, b, a, c
    order = ["d", "b", "a", "c"]
    assert_blackboard_in_file(bt)
    other = make_widget()
    assert other.LoadTree(bt.GetFilePath())
    assert other.GetEntryNames() == order
    assert [other.GetEntry(name) for name in order] == [4.5, "bee", 1, [3]]
    assert other.GetEntryReset("b")
    assert_view_in_order(other.blackboardView(), order)
    assert not other.IsModified()


def test_loading_the_same_tree_right_after_a_move_keeps_the_order(bt):
    add_entries(bt, "a", "b", "c")
    assert bt.SaveTree(bt.GetFilePath())
    assert_blackboard_in_file(bt)
    bt.MoveEntry("c", -2)  # not written yet when the tree is loaded
    assert bt.LoadTree(bt.GetFilePath())
    assert bt.GetEntryNames() == ["c", "a", "b"]
    assert file_names(bt) == ["c", "a", "b"]
    assert_view_in_order(bt.blackboardView(), ["c", "a", "b"])


def test_loading_a_tree_puts_the_entries_of_the_widget_in_the_file_order(make_widget, bt, tmp_path):
    add_entries(bt, "a", "b", "c")
    bt.MoveEntry("a", 2)
    assert bt.SaveTree(str(tmp_path / "moved"))
    other = make_widget()
    assert other.NewTree(str(tmp_path / "other.json"))
    add_entries(other, "a", "b", "c")
    assert other.LoadTree(str(tmp_path / "moved_BT.json"))
    assert_view_in_order(other.blackboardView(), ["b", "c", "a"])


def test_save_as_writes_the_order(bt, tmp_path):
    add_entries(bt, "a", "b", "c")
    bt.MoveEntry("b", 1)
    assert bt.SaveTree(str(tmp_path / "copy"))
    assert [item["name"] for item in read_json(tmp_path / "copy_BB.json")["blackboard"]] == ["a", "c", "b"]


def test_before_a_tree_exists_a_move_marks_the_tree_modified(make_widget, tmp_path):
    widget = make_widget()
    add_entries(widget, "a", "b", "c")
    widget._set_modified(False)
    assert widget.MoveEntry("a", -1) == 0  # nothing moved
    assert not widget.IsModified()
    assert widget.MoveEntry("c", -1) == 1
    assert widget.IsModified()
    assert widget.GetBlackboardFilePath() is None
    widget._set_modified(False)
    widget.blackboardView().row("a").move_down_button.click()  # the user moves an entry
    assert widget.IsModified()
    assert widget.GetEntryNames() == ["c", "a", "b"]
    assert widget.SaveTree(str(tmp_path / "later"))
    assert [item["name"] for item in read_json(tmp_path / "later_BB.json")["blackboard"]] == ["c", "a", "b"]
    assert not widget.IsModified()


# ============================================================================ moving during execution
def test_moving_while_the_tree_runs(qtbot, tree):
    fast_config(tree, restore_blackboard=False)
    add_entries(tree, "count", "a", "b")
    chain(tree, "Forever")
    tree.SetLoopExecution(True)
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
        click_arrow(qtbot, tree, "b", "up")
        assert tree.MoveEntry("b", -1) == 0
        assert tree.IsExecuting()
        assert_view_in_order(tree.blackboardView(), ["b", "count", "a"])
        qtbot.waitUntil(lambda: file_names(tree) == ["b", "count", "a"], timeout=3000)  # written while running
        qtbot.waitUntil(lambda: tree.GetEntry("count") >= 6, timeout=5000)  # the tree keeps running
    finally:
        tree.Stop()
    assert tree.GetEntryNames() == ["b", "count", "a"]
    assert_blackboard_in_file(tree)


@pytest.mark.parametrize("end", ["Stop", "Reset"])
def test_without_restore_blackboard_a_move_during_a_run_is_kept(qtbot, tree, end):
    fast_config(tree, restore_blackboard=False)
    add_entries(tree, "count", "a", "b")
    chain(tree, "Forever")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
        click_arrow(qtbot, tree, "b", "up")
        tree.MoveEntry("b", -1)
        getattr(tree, end)()
        assert tree.GetEntryNames() == ["b", "count", "a"]
        assert tree.GetEntry("count") >= 2  # values are kept too
    finally:
        tree.Stop()
    assert_view_in_order(tree.blackboardView(), ["b", "count", "a"])
    assert_blackboard_in_file(tree)
    assert file_names(tree) == ["b", "count", "a"]


def test_with_restore_blackboard_stop_undoes_a_move_clicked_during_the_run(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    tree.AddEntry("count", "Integer", 0)
    tree.AddEntry("a", "Integer", 1)
    tree.AddEntry("b", "String", "bee")
    chain(tree, "Forever")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
    click_arrow(qtbot, tree, "b", "up")
    click_arrow(qtbot, tree, "b", "up")
    assert tree.GetEntryNames() == ["b", "count", "a"]
    qtbot.waitUntil(lambda: file_names(tree) == ["b", "count", "a"], timeout=3000)  # written during the run
    tree.SetEntry(50, "a")
    tree.Stop()
    assert tree.GetEntryNames() == ["count", "a", "b"]  # the order of the run's start ...
    assert [tree.GetEntry(name) for name in ("count", "a", "b")] == [0, 1, "bee"]  # ... and its values
    assert_view_in_order(tree.blackboardView(), ["count", "a", "b"])
    assert_blackboard_in_file(tree)
    assert file_blackboard(tree.GetFilePath()) == [
        {"name": "count", "type": "Integer", "value": 0},
        {"name": "a", "type": "Integer", "value": 1},
        {"name": "b", "type": "String", "value": "bee"},
    ]


def test_with_restore_blackboard_stop_undoes_an_api_move_during_the_run(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "count", "a", "b", "c")
    chain(tree, "Forever")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
    tree.MoveEntry("count", 3)
    tree.Stop()
    assert tree.GetEntry("count") == 0
    assert_view_in_order(tree.blackboardView(), ["count", "a", "b", "c"])
    assert_blackboard_in_file(tree)
    assert file_names(tree) == ["count", "a", "b", "c"]


def test_with_restore_blackboard_stop_restores_the_snapshot_order_after_moves_removals_and_additions(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "count", "a", "b", "c")
    chain(tree, "Forever")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
    tree.MoveEntry("c", -3)  # c, count, a, b
    tree.RemoveEntry("a")  # c, count, b
    tree.AddEntry("x", "Integer", 9)  # c, count, b, x
    tree.MoveEntry("x", -3)  # x, c, count, b
    tree.Stop()
    order = ["count", "a", "b", "c"]  # x is gone, a is back, the order of the run's start
    assert tree.GetEntryNames() == order
    assert [tree.GetEntry(name) for name in order] == [0, 1, 2, 3]
    assert_view_in_order(tree.blackboardView(), order)
    assert_blackboard_in_file(tree)
    assert file_names(tree) == order


def test_with_restore_blackboard_reset_while_running_undoes_a_move(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "count", "a", "b")
    chain(tree, "Forever")
    tree.Execute()
    try:
        qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
        tree.MoveEntry("b", -2)
        tree.Reset()
        assert tree.IsExecuting()
        assert tree.GetEntryNames() == ["count", "a", "b"]
        assert_view_in_order(tree.blackboardView(), ["count", "a", "b"])
        qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)  # the new loop runs
        tree.MoveEntry("a", 1)  # a move in the run that goes on after Reset ...
        assert tree.GetEntryNames() == ["count", "b", "a"]
    finally:
        tree.Stop()
    assert tree.GetEntryNames() == ["count", "a", "b"]  # ... is undone by Stop
    assert tree.GetEntry("count") == 0
    assert_view_in_order(tree.blackboardView(), ["count", "a", "b"])


def test_with_restore_blackboard_a_move_while_paused_is_undone_by_stop(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "count", "a", "b")
    chain(tree, "Forever")
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
    tree.Pause()
    qtbot.waitUntil(lambda: tree.GetExecutionState() == "Paused", timeout=5000)
    click_arrow(qtbot, tree, "count", "down")  # paused: still part of the run
    assert tree.GetEntryNames() == ["a", "count", "b"]
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 4, timeout=5000)
    assert tree.GetEntryNames() == ["a", "count", "b"]
    tree.Stop()
    assert tree.GetEntryNames() == ["count", "a", "b"]
    assert tree.GetEntry("count") == 0
    assert_view_in_order(tree.blackboardView(), ["count", "a", "b"])


def test_with_restore_blackboard_reset_after_a_run_undoes_a_move_made_by_the_tree(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "a", "b", "c")
    chain(tree, "ThreadedMover")
    tree.Execute()
    run_until_idle(qtbot, tree)
    wait_view_in_order(qtbot, tree.blackboardView(), ["c", "a", "b"])  # a run-once keeps its result ...
    tree.Reset()
    assert tree.GetEntryNames() == ["a", "b", "c"]  # ... until Reset restores the blackboard
    assert_view_in_order(tree.blackboardView(), ["a", "b", "c"])


def test_with_restore_blackboard_a_click_while_idle_after_a_run_persists(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "count", "a", "b")
    chain(tree, "Bump")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert tree.GetEntry("count") == 1
    click_arrow(qtbot, tree, "b", "up")  # a deliberate edit while idle: Reset must not undo it
    tree.Reset()
    assert tree.GetEntryNames() == ["count", "b", "a"]
    assert tree.GetEntry("count") == 1  # the restore snapshot was discarded
    assert_view_in_order(tree.blackboardView(), ["count", "b", "a"])
    tree.Stop()
    assert tree.GetEntryNames() == ["count", "b", "a"]
    assert_blackboard_in_file(tree)
    assert file_names(tree) == ["count", "b", "a"]


def test_with_restore_blackboard_a_value_set_from_code_while_idle_after_a_run_persists(qtbot, tree):
    """The behaviour an API move is compared with in the test below."""
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "count", "a", "b")
    chain(tree, "Bump")
    tree.Execute()
    run_until_idle(qtbot, tree)
    tree.SetEntry(42, "a")
    tree.Reset()
    assert tree.GetEntry("a") == 42 and tree.GetEntry("count") == 1


def test_with_restore_blackboard_an_api_move_while_idle_after_a_run_persists(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "count", "a", "b")
    chain(tree, "Bump")
    tree.Execute()
    run_until_idle(qtbot, tree)
    tree.MoveEntry("b", -1)
    tree.Reset()
    assert tree.GetEntryNames() == ["count", "b", "a"]
    assert_view_in_order(tree.blackboardView(), ["count", "b", "a"])


def test_with_restore_blackboard_a_move_before_the_run_is_its_start_order(qtbot, tree):
    fast_config(tree, restore_blackboard=True)
    add_entries(tree, "count", "a", "b")
    chain(tree, "Forever")
    tree.MoveEntry("b", -2)  # b, count, a
    tree.Execute()
    qtbot.waitUntil(lambda: tree.GetEntry("count") >= 2, timeout=5000)
    tree.MoveEntry("b", 2)
    tree.Stop()
    assert tree.GetEntryNames() == ["b", "count", "a"]
    assert_view_in_order(tree.blackboardView(), ["b", "count", "a"])


def test_a_node_on_a_worker_thread_can_move_entries(qtbot, tree):
    add_entries(tree, "a", "b", "c")
    chain(tree, "ThreadedMover")
    assert tree.SaveTree(tree.GetFilePath())
    tree.Execute()
    run_until_idle(qtbot, tree)
    assert ThreadedMover.results == [(False, 0)]
    wait_view_in_order(qtbot, tree.blackboardView(), ["c", "a", "b"])
    assert_blackboard_in_file(tree)
    assert file_names(tree) == ["c", "a", "b"]


# ============================================================================ worker threads
def test_moving_from_a_worker_thread(qtbot, bt):
    add_entries(bt, "a", "b", "c")
    assert bt.SaveTree(bt.GetFilePath())
    results = []
    run_in_thread(lambda: results.append(bt.MoveEntry("c", -2)))
    assert results == [0]
    assert bt.GetEntryNames() == ["c", "a", "b"]  # the store changed at once
    wait_view_in_order(qtbot, bt.blackboardView(), ["c", "a", "b"])
    assert_blackboard_in_file(bt)
    assert file_names(bt) == ["c", "a", "b"]


def test_moves_from_several_threads_at_once(qtbot, bt):
    names = [f"e{index}" for index in range(6)]
    add_entries(bt, *names)
    errors = []

    def mover(seed):
        rng = random.Random(seed)
        try:
            for _ in range(200):
                bt.MoveEntry(rng.choice(names), rng.randint(-3, 3))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=mover, args=(seed,)) for seed in range(4)]
    for thread in threads:
        thread.start()
    for _ in range(100):
        bt.MoveEntry(names[0], 1)  # the GUI thread moves entries too
        bt.MoveEntry(names[0], -1)
    for thread in threads:
        thread.join(10)
    assert errors == []
    final = bt.GetEntryNames()
    assert sorted(final) == sorted(names)
    wait_view_in_order(qtbot, bt.blackboardView(), final)


def test_store_moves_from_threads_keep_every_entry(store):
    names = [f"e{index}" for index in range(8)]
    for name in names:
        store.add(name, "Integer", 0)
    errors = []

    def mover(seed):
        rng = random.Random(seed)
        try:
            for _ in range(500):
                index = store.move(rng.choice(names), rng.randint(-5, 5))
                assert 0 <= index < len(names)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=mover, args=(seed,)) for seed in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert sorted(store.names()) == sorted(names)


# ============================================================================ BlackboardStore.restore
def test_store_restore_puts_moved_entries_back_in_the_snapshot_order(store):
    for index, name in enumerate(("a", "b", "c")):
        store.add(name, "Integer", index)
    snapshot = store.snapshot()
    store.move("c", -2)
    store.set("a", 100)
    reordered = []
    store.entriesReordered.connect(lambda: reordered.append(True))
    store.restore(snapshot)
    assert store.items() == [("a", "Integer", 0), ("b", "Integer", 1), ("c", "Integer", 2)]
    assert reordered == [True]


def test_store_restore_without_a_move_does_not_reorder(store):
    for index, name in enumerate(("a", "b", "c")):
        store.add(name, "Integer", index)
    snapshot = store.snapshot()
    store.set("b", 7)
    reordered = []
    store.entriesReordered.connect(lambda: reordered.append(True))
    store.restore(snapshot)
    assert store.names() == ["a", "b", "c"] and store.get("b") == 1
    assert reordered == []


def test_store_restore_after_moves_removals_renames_and_additions(store):
    for index, name in enumerate(("a", "b", "c", "d")):
        store.add(name, "Integer", index)
    snapshot = store.snapshot()
    store.move("d", -3)  # d, a, b, c
    store.remove("b")  # d, a, c
    store.rename("c", "c2")  # d, a, c2
    store.add("x", "String", "new")
    store.move("x", -9)  # x, d, a, c2
    store.restore(snapshot)
    assert store.items() == [("a", "Integer", 0), ("b", "Integer", 1), ("c", "Integer", 2), ("d", "Integer", 3)]


def test_restore_puts_the_view_back_in_the_snapshot_order(bt):
    add_entries(bt, "a", "b", "c")
    store = bt.blackboardStore()
    snapshot = store.snapshot()
    bt.MoveEntry("a", 2)  # b, c, a
    bt.RemoveEntry("c")
    store.restore(snapshot)
    assert_view_in_order(bt.blackboardView(), ["a", "b", "c"])


# ============================================================================ texts
@pytest.mark.parametrize("type_name", list(ENTRY_TYPES))
def test_every_entry_row_says_resets(bt, type_name):
    bt.AddEntry("entry", type_name, SAMPLE_VALUES[type_name])
    box = bt.blackboardView().row("entry").reset_box
    assert isinstance(box, QCheckBox)
    assert box.text() == "Resets"
    assert box.toolTip().startswith("Resets")


def test_a_ui_file_without_the_check_box_gets_one_saying_resets(monkeypatch, bt, tmp_path):
    strip_from_entry_ui(monkeypatch, tmp_path, "Reset")
    bt.AddEntry("entry", "Integer", 1)
    box = bt.blackboardView().row("entry").reset_box
    assert box.objectName() == "Reset" and box.text() == "Resets"
    box.setChecked(True)
    assert bt.GetEntryReset("entry")


def test_the_header_names_the_resets_and_order_columns(qtbot, bt):
    view = bt.blackboardView()
    resets = view.findChild(QLabel, "LabelEntryReset")
    order = view.findChild(QLabel, "LabelEntryOrder")
    assert resets is not None and resets.text() == "Resets"
    assert order is not None and order.text() == "Order"
    assert "Resets" in resets.toolTip()
    assert order.toolTip()
    show_blackboard(qtbot, bt)
    qtbot.waitUntil(lambda: order.isVisible() and resets.isVisible(), timeout=2000)
    assert order.mapTo(view, QPoint(0, 0)).x() > resets.mapTo(view, QPoint(0, 0)).x()  # Order comes after Resets


def test_the_arrows_sit_after_the_resets_box(qtbot, bt):
    add_entries(bt, "a", "b")
    show_blackboard(qtbot, bt)
    view = bt.blackboardView()
    row = view.row("a")
    qtbot.waitUntil(lambda: row.move_up_button.isVisible(), timeout=2000)

    def x(widget):
        return widget.mapTo(view, QPoint(0, 0)).x()

    assert x(row.reset_box) < x(row.move_up_button) < x(row.move_down_button)


def test_the_flag_api_and_file_key_are_unchanged(bt):
    add_entries(bt, "a", "b")
    bt.SetEntryReset("b", True)
    assert bt.GetEntryReset("b")
    bt.MoveEntry("b", -1)
    assert bt.GetEntryReset("b") and not bt.GetEntryReset("a")
    assert bt.blackboardView().row("b").reset_box.isChecked()
    assert_blackboard_in_file(bt)
    assert file_blackboard(bt.GetFilePath()) == [
        {"name": "b", "type": "Integer", "value": 1, "reset": True},
        {"name": "a", "type": "Integer", "value": 0},
    ]
