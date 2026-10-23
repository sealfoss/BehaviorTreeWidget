"""Tests of the Notes node (``behavior_tree_widget.notes``).

Requirements (feature request "notes node"):

* the right-click menu can drop a Notes node on the tree;
* it is essentially a label with a visible border;
* it can be resized by grabbing its edges or corners;
* its text is automatically drawn as large as the size of the note allows;
* it has no other function than documenting parts of the tree (no connections, never executed);
* clicking its text area lets the user alter the text.

Trees written by earlier versions must keep loading unchanged.
"""

from __future__ import annotations

import json
import math
import threading

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QFont, QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget, NodeStatus, NotesNodeWidget
from behavior_tree_widget.canvas import NOTE_Z_BASE, SCENE_MARGIN, Hit
from behavior_tree_widget.notes import (
    BOTTOM,
    LEFT,
    NOTES,
    RIGHT,
    TEXT_FLAGS,
    TOP,
    ResizeHandle,
    fitted_pixel_size,
)

from conftest import drag, fast_config, label_point, run_until_idle, viewport_point

LEFT_BUTTON = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


# ============================================================================ helpers
def add_note(bt: BehaviorTreeWidget, x: float = 200.0, y: float = 260.0, text: str = "") -> NotesNodeWidget:
    note = bt.AddNode(NOTES, x, y)
    if text:
        note.SetText(text)
    QApplication.processEvents()
    return note


def note_point(bt: BehaviorTreeWidget, note: NotesNodeWidget, x: float, y: float) -> QPoint:
    """Viewport position of point (x, y) in note coordinates."""
    return bt.view().mapFromScene(note._item.mapToScene(QPointF(x, y)))


def text_point(bt: BehaviorTreeWidget, note: NotesNodeWidget) -> QPoint:
    """Viewport position inside the text area of the note, away from every handle."""
    return note_point(bt, note, note.width() / 2.0, note.height() / 2.0)


def handle(note: NotesNodeWidget, edges: int) -> ResizeHandle:
    return next(h for h in note.resize_handles() if h.edges == edges)


def handle_point(bt: BehaviorTreeWidget, note: NotesNodeWidget, edges: int) -> QPoint:
    return viewport_point(bt, note, handle(note, edges))


def click_at(bt: BehaviorTreeWidget, point: QPoint, modifiers=NO_MOD) -> None:
    QTest.mouseClick(bt.view().viewport(), LEFT_BUTTON, modifiers, point)


def type_text(bt: BehaviorTreeWidget, text: str) -> None:
    QTest.keyClicks(bt.view().viewport(), text)


def key(bt: BehaviorTreeWidget, key_code, modifiers=NO_MOD) -> None:
    QTest.keyClick(bt.view().viewport(), key_code, modifiers)


def empty_point(bt: BehaviorTreeWidget) -> QPoint:
    point = QPoint(15, 15)
    assert bt.view().hit_test(point).kind == Hit.EMPTY
    return point


def context_menu(bt: BehaviorTreeWidget, dialogs, point: QPoint):
    before = len(dialogs.menus)
    viewport = bt.view().viewport()
    QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point)))
    assert len(dialogs.menus) == before + 1
    return dialogs.menus[-1]


def action(menu, name: str):
    for candidate in menu.actions():
        if candidate.objectName() == name:
            return candidate
    raise AssertionError(f"menu has no action {name!r}")


def fits(font: QFont, pixels: int, size: QSize, text: str) -> bool:
    probe = QFont(font)
    probe.setPixelSize(pixels)
    bounds = QFontMetrics(probe).boundingRect(QRect(0, 0, size.width(), size.height()), int(TEXT_FLAGS), text)
    return bounds.width() <= size.width() and bounds.height() <= size.height()


def label_pixels(note: NotesNodeWidget) -> int:
    return note.text_label().font().pixelSize()


def scene_rect(note: NotesNodeWidget) -> tuple[float, float, float, float]:
    rect = note._item.sceneBoundingRect()
    return rect.left(), rect.top(), rect.width(), rect.height()


def saved_nodes(path) -> list[dict]:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)["nodes"]


# ============================================================================ the menu
def test_notes_is_the_last_entry_of_the_add_menu(bt, dialogs):
    menu = context_menu(bt, dialogs, empty_point(bt))
    actions = [a for a in menu.actions() if a.objectName().startswith("Add_") or a.isSeparator()]
    assert actions[-1].objectName() == "Add_Notes"
    assert actions[-1].text() == "Notes"
    assert actions[-2].isSeparator()  # in its own group: it is not part of the behavior tree
    assert bt._node_menu_entries()[-1] == ("notes", NOTES, NOTES)


def test_choosing_notes_in_the_menu_drops_a_note_at_the_click(qtbot, bt, dialogs):
    point = QPoint(420, 330)
    assert bt.view().hit_test(point).kind == Hit.EMPTY
    before = set(bt.GetNodes())
    dialogs.menu_handler = lambda menu: action(menu, "Add_Notes").trigger()
    context_menu(bt, dialogs, point)
    added = set(bt.GetNodes()) - before
    assert len(added) == 1
    note = added.pop()
    assert type(note) is NotesNodeWidget and note.GetTypeName() == NOTES
    assert note._item.pos() == bt.view().mapToScene(point)
    assert note.GetNoteSize() == NotesNodeWidget.DEFAULT_SIZE
    assert note.GetText() == ""
    assert note.text_label().text() == NotesNodeWidget.PLACEHOLDER
    assert bt.IsModified()


def test_notes_is_a_node_type_but_not_a_leaf_type(bt):
    assert bt.GetNodeTypes()[-1] == NOTES
    assert bt.GetNodeType(NOTES) is None
    assert isinstance(bt.AddNode(NOTES, 0, 300), NotesNodeWidget)
    assert isinstance(bt.AddNode(NotesNodeWidget(), 300, 300), NotesNodeWidget)


def test_notes_cannot_be_added_while_executing(qtbot, bt, dialogs):
    bt.Connect(bt.GetRootNode(), bt.AddNode("Succeed", 0, 200))
    bt.SetLoopExecution(True)
    fast_config(bt)
    bt.Execute()
    try:
        menu = context_menu(bt, dialogs, empty_point(bt))
        assert not action(menu, "Add_Notes").isEnabled()
        with pytest.raises(RuntimeError):
            bt.AddNode(NOTES, 0, 400)
    finally:
        bt.Stop()


# ============================================================================ looks: a label with a border
def test_a_note_is_a_bordered_label(bt):
    note = add_note(bt, text="Remember")
    label = note.text_label()
    assert isinstance(label, QLabel) and label.isVisible()
    assert label.wordWrap() and label.textFormat() == Qt.TextFormat.PlainText
    assert not note._title_label.isVisible() and not note._status_label.isVisible()
    image = note.grab().toImage()
    border = QColor("#a68b2c")
    middle_y = note.height() // 2
    assert image.pixelColor(0, middle_y) == border
    assert image.pixelColor(note.width() - 1, middle_y) == border
    assert image.pixelColor(note.width() // 2, 0) == border
    assert image.pixelColor(note.width() // 2, note.height() - 1) == border
    assert image.pixelColor(3, middle_y) != border  # the inside is the note's background


def test_a_note_has_no_connections_and_cannot_be_connected(bt):
    note = add_note(bt)
    leaf = bt.AddNode("Succeed", 0, 500)
    assert not note.HasParentConnection() and not note.HasChildConnections() and note.MaxChildren() == 0
    assert note.connection_label("parent") is None and note.connection_label("children") is None
    with pytest.raises(ValueError):
        bt.Connect(bt.GetRootNode(), note)
    with pytest.raises(ValueError):
        bt.Connect(note, leaf)
    assert note.GetParent() is None and note.GetChildren() == []


def test_releasing_a_connection_drag_on_a_note_connects_nothing(qtbot, bt, dialogs):
    note = add_note(bt, 300, 300)
    drag(bt, label_point(bt, bt.GetRootNode(), "children"), text_point(bt, note))
    assert dialogs.menus == []
    assert bt.GetRootNode().GetChildren() == []
    assert not note.IsEditing()


def test_a_note_is_never_executed(qtbot, bt):
    leaf = bt.AddNode("Succeed", 0, 200)
    bt.Connect(bt.GetRootNode(), leaf)
    note = add_note(bt, 300, 200, text="Not executed")
    fast_config(bt)
    finished = []
    bt.executionFinished.connect(finished.append)
    bt.Execute()
    assert bt.executor().behaviour_for(note) is None
    run_until_idle(qtbot, bt)
    assert finished == ["Succeeded"]
    assert leaf.GetStatus() is NodeStatus.SUCCEEDED
    assert note.GetStatus() is NodeStatus.READY
    assert note.GetText() == "Not executed"


def test_a_tree_holding_only_notes_executes_like_an_empty_tree(qtbot, bt):
    add_note(bt, 300, 200, text="Nothing to do")
    fast_config(bt)
    finished = []
    bt.executionFinished.connect(finished.append)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert finished == ["Succeeded"]  # an empty Sequence succeeds


def test_a_note_menu_offers_edit_text_and_delete_only(bt, dialogs):
    note = add_note(bt)
    menu = context_menu(bt, dialogs, text_point(bt, note))
    names = [a.objectName() for a in menu.actions() if not a.isSeparator()]
    assert names == ["EditText", "Delete"]


# ============================================================================ text size
def test_the_text_uses_the_largest_font_that_fits(bt):
    note = add_note(bt, text="Check the battery before the patrol starts")
    label = note.text_label()
    size = label.text_rect().size()
    pixels = label_pixels(note)
    assert pixels == fitted_pixel_size(label.font(), size, label.text())
    assert fits(label.font(), pixels, size, label.text())
    assert not fits(label.font(), pixels + 1, size, label.text())
    assert pixels > 12  # larger than ordinary text: the note is far from full


def test_the_text_grows_and_shrinks_with_the_note(bt):
    note = add_note(bt, text="Grow with the note")
    small = label_pixels(note)
    note.SetNoteSize(600, 300)
    QApplication.processEvents()
    large = label_pixels(note)
    assert large > small
    assert fits(note.text_label().font(), large, note.text_label().text_rect().size(), note.GetText())
    note.SetNoteSize(*NotesNodeWidget.MIN_SIZE)
    QApplication.processEvents()
    assert label_pixels(note) < small


def test_more_text_means_a_smaller_font(bt):
    note = add_note(bt, text="Short")
    short = label_pixels(note)
    note.SetText("A much longer note that needs several lines in the same box")
    assert label_pixels(note) < short
    assert fits(note.text_label().font(), label_pixels(note), note.text_label().text_rect().size(), note.GetText())


def test_a_long_word_is_never_cut(bt):
    note = add_note(bt, text="Supercalifragilisticexpialidocious")
    width = note.text_label().text_rect().width()
    font = QFont(note.text_label().font())
    assert QFontMetrics(font).horizontalAdvance(note.GetText()) <= width


def test_the_text_size_follows_a_resize_drag(qtbot, bt):
    note = add_note(bt, 100, 300, text="Drag me bigger")
    before = label_pixels(note)
    drag(bt, handle_point(bt, note, BOTTOM | RIGHT), handle_point(bt, note, BOTTOM | RIGHT) + QPoint(200, 100))
    assert label_pixels(note) > before


def test_the_placeholder_is_also_fitted(bt):
    note = add_note(bt)
    label = note.text_label()
    assert label.text() == NotesNodeWidget.PLACEHOLDER
    assert label_pixels(note) == fitted_pixel_size(label.font(), label.text_rect().size(), NotesNodeWidget.PLACEHOLDER)


@pytest.mark.parametrize(
    "size, text",
    [(QSize(0, 50), "text"), (QSize(50, 0), "text"), (QSize(-5, -5), "text"), (QSize(100, 100), "")],
)
def test_fitted_pixel_size_of_nothing_is_the_minimum(size, text):
    assert fitted_pixel_size(QFont(), size, text) == 1


def test_fitted_pixel_size_is_limited_by_the_height():
    font = QFont()
    pixels = fitted_pixel_size(font, QSize(2000, 40), "x")
    assert 1 < pixels <= 40
    assert fits(font, pixels, QSize(2000, 40), "x")


# ============================================================================ resizing
@pytest.mark.parametrize(
    "edges, delta, expected",
    [
        # (edges grabbed, mouse movement, (dx, dy, dwidth, dheight) of the note)
        (RIGHT, (60, 25), (0, 0, 60, 0)),
        (BOTTOM, (25, 40), (0, 0, 0, 40)),
        (LEFT, (-50, 25), (-50, 0, 50, 0)),
        (TOP, (25, -30), (0, -30, 0, 30)),
        (BOTTOM | RIGHT, (70, 30), (0, 0, 70, 30)),
        (TOP | LEFT, (-40, -20), (-40, -20, 40, 20)),
        (TOP | RIGHT, (30, -20), (0, -20, 30, 20)),
        (BOTTOM | LEFT, (-30, 20), (-30, 0, 30, 20)),
        (TOP | LEFT, (40, 20), (40, 20, -40, -20)),  # shrinking
    ],
)
def test_dragging_an_edge_or_corner_resizes_the_note(qtbot, bt, edges, delta, expected):
    note = add_note(bt, 200, 300, text="Resize me")
    x, y, width, height = scene_rect(note)
    bt._set_modified(False)
    start = handle_point(bt, note, edges)
    drag(bt, start, start + QPoint(*delta))
    dx, dy, dwidth, dheight = expected
    assert scene_rect(note) == (x + dx, y + dy, width + dwidth, height + dheight)
    assert note.GetNoteSize() == (width + dwidth, height + dheight)
    assert note.size() == QSize(width + dwidth, height + dheight)
    assert bt.IsModified()
    assert not note.IsEditing()
    assert bt.GetSelectedNodes() == [note]


def test_resizing_keeps_the_opposite_edges_at_the_minimum_size(qtbot, bt):
    note = add_note(bt, 200, 300)
    x, y, width, height = scene_rect(note)
    start = handle_point(bt, note, TOP | LEFT)
    drag(bt, start, start + QPoint(600, 600))
    min_width, min_height = NotesNodeWidget.MIN_SIZE
    assert note.GetNoteSize() == (min_width, min_height)
    assert scene_rect(note) == (x + width - min_width, y + height - min_height, min_width, min_height)


def test_a_note_is_at_most_max_size_and_fits_the_scene_margin(bt):
    assert NotesNodeWidget.MAX_SIZE <= SCENE_MARGIN
    note = add_note(bt)
    note.SetNoteSize(10**9, 5000.0)
    QApplication.processEvents()
    assert note.GetNoteSize() == (NotesNodeWidget.MAX_SIZE, NotesNodeWidget.MAX_SIZE)
    assert note._item.size().toSize() == QSize(NotesNodeWidget.MAX_SIZE, NotesNodeWidget.MAX_SIZE)


def test_resizing_follows_the_zoom(qtbot, bt):
    bt.view().set_zoom(2.0)
    note = add_note(bt, 0, 200)
    width = note.GetNoteSize()[0]
    start = handle_point(bt, note, RIGHT)
    drag(bt, start, start + QPoint(100, 0))
    assert note.GetNoteSize()[0] == width + 50  # 100 view pixels are 50 scene units at 2x


def test_resize_handles_cover_the_edges_and_corners(bt):
    note = add_note(bt)
    width, height = note.width(), note.height()
    view = bt.view()
    for local, edges in [
        ((1, height / 2), LEFT),
        ((width - 2, height / 2), RIGHT),
        ((width / 2, 1), TOP),
        ((width / 2, height - 2), BOTTOM),
        ((1, 1), TOP | LEFT),
        ((width - 2, 1), TOP | RIGHT),
        ((1, height - 2), BOTTOM | LEFT),
        ((width - 2, height - 2), BOTTOM | RIGHT),
    ]:
        hit = view.hit_test(note_point(bt, note, *local))
        assert hit.kind == Hit.RESIZE and hit.node is note and hit.widget.edges == edges, (local, edges)
    assert view.hit_test(text_point(bt, note)).kind == Hit.NODE


@pytest.mark.parametrize(
    "edges, shape",
    [
        (LEFT, Qt.CursorShape.SizeHorCursor),
        (RIGHT, Qt.CursorShape.SizeHorCursor),
        (TOP, Qt.CursorShape.SizeVerCursor),
        (BOTTOM, Qt.CursorShape.SizeVerCursor),
        (TOP | LEFT, Qt.CursorShape.SizeFDiagCursor),
        (BOTTOM | RIGHT, Qt.CursorShape.SizeFDiagCursor),
        (TOP | RIGHT, Qt.CursorShape.SizeBDiagCursor),
        (BOTTOM | LEFT, Qt.CursorShape.SizeBDiagCursor),
    ],
)
def test_hovering_a_handle_shows_a_resize_cursor(qtbot, bt, edges, shape):
    note = add_note(bt)
    assert handle(note, edges).cursor().shape() == shape
    point = handle_point(bt, note, edges)
    QTest.mouseMove(bt.view().viewport(), point + QPoint(0, 1))
    QTest.mouseMove(bt.view().viewport(), point)
    assert bt.view().viewport().cursor().shape() == shape


def test_a_click_on_a_handle_selects_without_editing(qtbot, bt):
    note = add_note(bt, text="text")
    other = bt.AddNode("Succeed", 0, 600)
    bt.SelectNodes([note, other])
    size = note.GetNoteSize()
    bt._set_modified(False)
    click_at(bt, handle_point(bt, note, RIGHT))
    assert bt.GetSelectedNodes() == [note]  # as a click on one of several selected nodes
    assert note.GetNoteSize() == size and not bt.IsModified()
    assert not note.IsEditing()


def test_ctrl_click_on_a_handle_toggles_the_selection(qtbot, bt):
    note = add_note(bt)
    click_at(bt, handle_point(bt, note, BOTTOM), CTRL)
    assert bt.GetSelectedNodes() == [note]
    click_at(bt, handle_point(bt, note, BOTTOM), CTRL)
    assert bt.GetSelectedNodes() == []


def test_shift_click_on_a_handle_adds_to_the_selection(qtbot, bt):
    note = add_note(bt)
    bt.SelectNodes([bt.GetRootNode()])
    click_at(bt, handle_point(bt, note, TOP), SHIFT)
    assert set(bt.GetSelectedNodes()) == {bt.GetRootNode(), note}


def test_right_click_on_a_handle_opens_the_note_menu(bt, dialogs):
    note = add_note(bt)
    menu = context_menu(bt, dialogs, handle_point(bt, note, LEFT))
    assert menu.objectName() == "NodeMenu"
    assert action(menu, "EditText")


def test_notes_can_be_resized_while_executing(qtbot, bt):
    note = add_note(bt, 300, 300)
    bt.Connect(bt.GetRootNode(), bt.AddNode("Succeed", 0, 200))
    bt.SetLoopExecution(True)
    fast_config(bt)
    bt.Execute()
    try:
        start = handle_point(bt, note, RIGHT)
        drag(bt, start, start + QPoint(40, 0))
        assert note.GetNoteSize()[0] == NotesNodeWidget.DEFAULT_SIZE[0] + 40
    finally:
        bt.Stop()


def test_removing_a_note_while_it_is_resized_ends_the_gesture(qtbot, bt):
    note = add_note(bt)
    viewport = bt.view().viewport()
    start = handle_point(bt, note, RIGHT)
    QTest.mousePress(viewport, LEFT_BUTTON, NO_MOD, start)
    QTest.mouseMove(viewport, start + QPoint(20, 0))
    bt.RemoveNode(note)
    QTest.mouseMove(viewport, start + QPoint(40, 0))
    QTest.mouseRelease(viewport, LEFT_BUTTON, NO_MOD, start + QPoint(40, 0))
    assert note not in bt.GetNodes()


# ============================================================================ editing the text
def test_clicking_the_text_edits_it(qtbot, bt):
    note = add_note(bt, text="Old")
    bt._set_modified(False)
    seen = []
    note.textChanged.connect(seen.append)
    click_at(bt, text_point(bt, note))
    assert note.IsEditing()
    editor = note.text_editor()
    assert editor.isVisible() and editor.toPlainText() == "Old"
    assert bt.view().scene().focusItem() is note._item
    assert bt.GetSelectedNodes() == [note]
    key(bt, Qt.Key.Key_End)
    type_text(bt, " and new")
    assert note.GetText() == "Old and new"
    assert note.text_label().text() == "Old and new"
    assert seen[-1] == "Old and new"
    assert bt.IsModified()


def test_the_editor_shows_the_text_with_the_label_font(qtbot, bt):
    note = add_note(bt, text="Same size while editing")
    click_at(bt, text_point(bt, note))
    editor = note.text_editor()
    assert editor.font().pixelSize() == label_pixels(note)
    type_text(bt, "!" * 40)  # more text: smaller font, in the label and in the editor
    assert editor.font().pixelSize() == label_pixels(note)
    assert editor.geometry() == note.text_label().text_rect()


def test_the_click_places_the_text_cursor(qtbot, bt):
    note = add_note(bt, 100, 300, text="First words of the note")
    label_rect = note.text_label().text_rect()
    top_left = note.text_label().mapTo(note, label_rect.topLeft())
    click_at(bt, note_point(bt, note, top_left.x() + 1, top_left.y() + 2))
    assert note.IsEditing()
    assert note.text_editor().textCursor().position() == 0


def test_newlines_can_be_typed(qtbot, bt):
    note = add_note(bt, text="Line one")
    click_at(bt, text_point(bt, note))
    key(bt, Qt.Key.Key_End, CTRL)
    key(bt, Qt.Key.Key_Return)
    type_text(bt, "Line two")
    assert note.GetText() == "Line one\nLine two"


def test_escape_ends_editing_and_keeps_the_note_selected(qtbot, bt):
    note = add_note(bt, text="Keep")
    click_at(bt, text_point(bt, note))
    type_text(bt, "x")
    key(bt, Qt.Key.Key_Escape)
    assert not note.IsEditing()
    assert not note.text_editor().isVisible()
    assert bt.view().scene().focusItem() is None
    assert bt.GetSelectedNodes() == [note]
    assert "x" in note.GetText()
    key(bt, Qt.Key.Key_Delete)  # the view has the keyboard again: Delete deletes the note
    assert note not in bt.GetNodes()


def test_keys_go_to_the_text_while_editing(qtbot, bt):
    note = add_note(bt, text="abc")
    other = bt.AddNode("Succeed", 0, 600)
    click_at(bt, text_point(bt, note))
    key(bt, Qt.Key.Key_End, CTRL)
    key(bt, Qt.Key.Key_Backspace)
    key(bt, Qt.Key.Key_A, CTRL)  # selects the text, not the nodes
    assert note in bt.GetNodes()
    assert note.GetText() == "ab"
    assert bt.GetSelectedNodes() == [note]
    key(bt, Qt.Key.Key_Delete)  # deletes the selected text
    assert note.GetText() == "" and note in bt.GetNodes() and other in bt.GetNodes()
    assert note.text_label().text() == NotesNodeWidget.PLACEHOLDER


def test_a_click_on_empty_canvas_ends_editing(qtbot, bt):
    note = add_note(bt, text="text")
    click_at(bt, text_point(bt, note))
    click_at(bt, empty_point(bt))
    assert not note.IsEditing() and not note.text_editor().isVisible()
    assert bt.GetSelectedNodes() == []


def test_a_click_on_another_node_ends_editing(qtbot, bt):
    note = add_note(bt, 400, 300, text="text")
    leaf = bt.AddNode("AllFields", -200, 300)
    QApplication.processEvents()
    click_at(bt, text_point(bt, note))
    click_at(bt, viewport_point(bt, leaf, leaf._title_label))
    assert not note.IsEditing()
    click_at(bt, text_point(bt, note))
    click_at(bt, viewport_point(bt, leaf, leaf.field_editor("label")))  # an input field of another node
    assert not note.IsEditing()
    type_text(bt, "!")
    assert leaf.GetField("label") != "hello" and note.GetText() == "text"


def test_editing_another_note_ends_editing_of_the_first(qtbot, bt):
    first = add_note(bt, 100, 300, text="first")
    second = add_note(bt, 500, 300, text="second")
    click_at(bt, text_point(bt, first))
    click_at(bt, text_point(bt, second))
    assert not first.IsEditing() and second.IsEditing()
    type_text(bt, "!")
    assert first.GetText() == "first" and second.GetText().count("!") == 1


def test_a_click_in_the_editor_keeps_editing(qtbot, bt):
    note = add_note(bt, text="some words here")
    click_at(bt, text_point(bt, note))
    assert bt.view().hit_test(text_point(bt, note)).kind == Hit.INTERACTIVE
    click_at(bt, text_point(bt, note))
    assert note.IsEditing()


def test_dragging_the_text_moves_the_note_without_editing(qtbot, bt):
    note = add_note(bt, 200, 300, text="Move me")
    x, y, width, height = scene_rect(note)
    bt._set_modified(False)
    drag(bt, text_point(bt, note), text_point(bt, note) + QPoint(80, 40))
    assert scene_rect(note) == (x + 80, y + 40, width, height)
    assert not note.IsEditing()
    assert bt.IsModified()


@pytest.mark.parametrize("modifiers", [CTRL, SHIFT])
def test_ctrl_or_shift_click_selects_without_editing(qtbot, bt, modifiers):
    note = add_note(bt, text="text")
    click_at(bt, text_point(bt, note), modifiers)
    assert bt.GetSelectedNodes() == [note]
    assert not note.IsEditing()


def test_edit_text_in_the_menu_edits_the_text(qtbot, bt, dialogs):
    note = add_note(bt, text="Menu")
    dialogs.menu_handler = lambda menu: action(menu, "EditText").trigger()
    context_menu(bt, dialogs, text_point(bt, note))
    assert note.IsEditing()
    assert note.text_editor().textCursor().position() == len("Menu")  # at the end
    type_text(bt, " edit")
    assert note.GetText() == "Menu edit"


def test_notes_can_be_edited_while_executing(qtbot, bt):
    note = add_note(bt, 300, 300, text="Run")
    bt.Connect(bt.GetRootNode(), bt.AddNode("Succeed", 0, 200))
    bt.SetLoopExecution(True)
    fast_config(bt)
    bt._set_modified(False)
    bt.Execute()
    try:
        click_at(bt, text_point(bt, note))
        key(bt, Qt.Key.Key_End, CTRL)
        type_text(bt, "ning")
        assert note.GetText() == "Running"
        assert bt.IsModified()  # typed by the user: an edit, also while executing
    finally:
        bt.Stop()


def test_deleting_a_note_while_it_is_edited(qtbot, bt, dialogs):
    note = add_note(bt, text="bye")
    click_at(bt, text_point(bt, note))
    dialogs.menu_handler = lambda menu: action(menu, "Delete").trigger()
    context_menu(bt, dialogs, handle_point(bt, note, LEFT))
    assert note not in bt.GetNodes()


# ============================================================================ API
def test_set_text_from_code(qtbot, bt):
    note = add_note(bt)
    bt._set_modified(False)
    seen = []
    note.textChanged.connect(seen.append)
    note.SetText("From code")
    assert note.GetText() == "From code" and note.text_label().text() == "From code"
    assert seen == ["From code"]
    assert bt.IsModified()
    note.SetText("From code")  # unchanged: no signal
    assert seen == ["From code"]


def test_set_text_while_editing_updates_the_editor(qtbot, bt):
    note = add_note(bt, text="before")
    click_at(bt, text_point(bt, note))
    note.SetText("after")
    assert note.text_editor().toPlainText() == "after"
    assert note.GetText() == "after"


def test_set_text_from_a_worker_thread(qtbot, bt):
    note = add_note(bt)
    done = threading.Event()

    class Annotate(LeafNodeWidget):
        def OnRun(self, tree):
            note.SetText("written by a worker")
            done.set()
            return True

    bt.RegisterNodeType(Annotate)
    bt.Connect(bt.GetRootNode(), bt.AddNode("Annotate", 0, 200))
    fast_config(bt)
    bt._set_modified(False)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert done.is_set()
    qtbot.waitUntil(lambda: note.text_label().text() == "written by a worker", timeout=2000)
    assert note.GetText() == "written by a worker"
    assert not bt.IsModified()  # a run-time change


def test_set_note_size_from_code(qtbot, bt):
    note = add_note(bt)
    bt._set_modified(False)
    pos = note._item.pos()
    note.SetNoteSize(301.4, 155.6)
    QApplication.processEvents()
    assert note.GetNoteSize() == (301, 156)
    assert note._item.size().toSize() == QSize(301, 156)
    assert note._item.pos() == pos
    assert bt.IsModified()
    note.SetNoteSize(1, 1)
    assert note.GetNoteSize() == NotesNodeWidget.MIN_SIZE


@pytest.mark.parametrize(
    "width, height, error",
    [("100", 100, TypeError), (100, None, TypeError), (True, 100, TypeError), (math.nan, 100, ValueError), (100, math.inf, ValueError)],
)
def test_set_note_size_rejects_invalid_sizes(bt, width, height, error):
    note = add_note(bt)
    with pytest.raises(error):
        note.SetNoteSize(width, height)
    assert note.GetNoteSize() == NotesNodeWidget.DEFAULT_SIZE


# ============================================================================ stacking order
def test_notes_stay_below_the_nodes_and_lines(qtbot, bt):
    note = add_note(bt, -150, -40)
    note.SetNoteSize(500, 400)
    leaf = bt.AddNode("Succeed", 0, 220)
    bt.Connect(bt.GetRootNode(), leaf)
    QApplication.processEvents()
    line = bt.view().connection_item(leaf)
    for _ in range(2):
        assert note._item.zValue() < line.zValue() < leaf._item.zValue()
        assert note._item.zValue() < bt.GetRootNode()._item.zValue()
        assert NOTE_Z_BASE < note._item.zValue() < -1
        hit = bt.view().hit_test(viewport_point(bt, leaf, leaf._title_label))
        assert hit.node is leaf  # the node lying on the note gets the click
        click_at(bt, note_point(bt, note, 30, 380))  # clicking the note does not raise it above the nodes
        assert note.IsEditing()
        key(bt, Qt.Key.Key_Escape)


def test_a_click_on_a_note_raises_it_above_other_notes(qtbot, bt):
    first = add_note(bt, 100, 300)
    second = add_note(bt, 150, 330)
    assert second._item.zValue() > first._item.zValue()
    click_at(bt, note_point(bt, first, 40, 20))
    assert first._item.zValue() > second._item.zValue()


# ============================================================================ selection, copy, delete
def test_notes_are_selected_by_the_rectangle_and_select_all(qtbot, bt):
    note = add_note(bt, 300, 300)
    drag(bt, note_point(bt, note, -30, -30), note_point(bt, note, 30, 30))
    assert bt.GetSelectedNodes() == [note]
    QTest.keyClick(bt.view(), Qt.Key.Key_A, CTRL)
    assert note in bt.GetSelectedNodes()


def test_selected_notes_are_deleted_with_the_delete_key(qtbot, bt):
    note = add_note(bt)
    click_at(bt, handle_point(bt, note, RIGHT))
    QTest.keyClick(bt.view(), Qt.Key.Key_Delete)
    assert note not in bt.GetNodes()


def test_notes_are_copied_and_pasted(qtbot, bt):
    note = add_note(bt, 300, 300, text="Copy me\nplease")
    note.SetNoteSize(260, 140)
    QApplication.processEvents()
    assert bt.CopyNodes([note])
    pasted = bt.PasteNodes()
    assert len(pasted) == 1
    copy = pasted[0]
    assert type(copy) is NotesNodeWidget and copy is not note and copy.GetId() != note.GetId()
    assert copy.GetText() == "Copy me\nplease"
    assert copy.GetNoteSize() == (260, 140)
    assert not copy._item.sceneBoundingRect().intersects(note._item.sceneBoundingRect())
    assert copy._item.zValue() < bt.GetRootNode()._item.zValue()


def test_moving_a_note_with_other_selected_nodes(qtbot, bt):
    note = add_note(bt, 300, 300)
    leaf = bt.AddNode("Succeed", -200, 300)
    QApplication.processEvents()
    bt.SelectNodes([note, leaf])
    note_pos, leaf_pos = note._item.pos(), leaf._item.pos()
    drag(bt, text_point(bt, note), text_point(bt, note) + QPoint(30, 50))
    assert note._item.pos() == note_pos + QPointF(30, 50)
    assert leaf._item.pos() == leaf_pos + QPointF(30, 50)
    assert note._item.zValue() < leaf._item.zValue()
    assert not note.IsEditing()


# ============================================================================ files
def test_notes_are_saved_and_loaded(qtbot, bt, tmp_path):
    note = add_note(bt, 120.0, 340.0, text="Line 1\nLine 2 é✓")
    note.SetNoteSize(333, 177)
    path = tmp_path / "notes.json"
    assert bt.SaveTree(str(path))
    saved = [item for item in saved_nodes(path) if item["type"] == NOTES]
    assert saved == [
        {
            "id": note.GetId(),
            "type": NOTES,
            "title": NOTES,
            "x": 120.0,
            "y": 340.0,
            "text": "Line 1\nLine 2 é✓",
            "width": 333,
            "height": 177,
        }
    ]
    assert bt.LoadTree(str(path))
    loaded = [node for node in bt.GetNodes() if isinstance(node, NotesNodeWidget)]
    assert len(loaded) == 1
    loaded = loaded[0]
    assert loaded.GetText() == "Line 1\nLine 2 é✓"
    assert loaded.GetNoteSize() == (333, 177)
    assert loaded._item.size().toSize() == QSize(333, 177)
    assert loaded._item.pos() == QPointF(120.0, 340.0)
    assert not bt.IsModified()
    assert loaded._item.zValue() < bt.GetRootNode()._item.zValue()


def write_tree(path, nodes: list[dict]) -> None:
    data = {
        "format": "behavior_tree_widget",
        "version": 1,
        "nodes": [{"id": "root", "type": "Root", "title": "Root", "x": 0, "y": 0}, *nodes],
        "connections": [],
        "blackboard": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def test_invalid_saved_note_values_keep_the_defaults(bt, tmp_path, dialogs):
    path = tmp_path / "odd.json"
    write_tree(
        path,
        [
            {"id": "a", "type": NOTES, "x": 0, "y": 300, "text": 5, "width": "wide", "height": True},
            {"id": "b", "type": NOTES, "x": 300, "y": 300, "width": 1, "height": 10**9},
            {"id": "c", "type": NOTES, "x": 600, "y": 300},
        ],
    )
    assert bt.LoadTree(str(path))
    notes = {node.GetId(): node for node in bt.GetNodes() if isinstance(node, NotesNodeWidget)}
    assert notes["a"].GetText() == "" and notes["a"].GetNoteSize() == NotesNodeWidget.DEFAULT_SIZE
    assert notes["b"].GetNoteSize() == (NotesNodeWidget.MIN_SIZE[0], NotesNodeWidget.MAX_SIZE)
    assert notes["c"].GetText() == "" and notes["c"].GetNoteSize() == NotesNodeWidget.DEFAULT_SIZE
    assert all(node.GetTitle() == NOTES for node in notes.values())


def test_a_connection_to_a_note_in_a_file_is_skipped(bt, tmp_path):
    path = tmp_path / "connected.json"
    data = {
        "format": "behavior_tree_widget",
        "version": 1,
        "nodes": [
            {"id": "root", "type": "Root", "title": "Root", "x": 0, "y": 0},
            {"id": "note", "type": NOTES, "title": NOTES, "x": 0, "y": 300, "text": "t", "width": 200, "height": 100},
        ],
        "connections": [{"parent": "root", "child": "note"}],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    assert bt.LoadTree(str(path))
    assert bt.GetRootNode().GetChildren() == []
    assert any(isinstance(node, NotesNodeWidget) and node.GetText() == "t" for node in bt.GetNodes())


# ============================================================================ keyboard after editing ended elsewhere
def test_keys_work_again_after_editing_ended_by_another_window(qtbot, bt):
    note = add_note(bt, text="hello")
    click_at(bt, text_point(bt, note))
    assert note.IsEditing()
    other = QLineEdit()
    qtbot.addWidget(other)
    other.show()
    other.activateWindow()
    other.setFocus()
    qtbot.waitUntil(lambda: not note.IsEditing(), timeout=2000)
    bt.window().activateWindow()
    bt.view().setFocus()
    qtbot.wait(20)
    assert not note.text_editor().isVisible()
    QTest.keyClick(bt.view(), Qt.Key.Key_A, CTRL)
    assert set(bt.GetSelectedNodes()) == set(bt.GetNodes())
    bt.SelectNodes([note])
    QTest.keyClick(bt.view(), Qt.Key.Key_Delete)
    assert note not in bt.GetNodes()


def test_keys_work_after_editing_ended_by_a_label_click_while_executing(qtbot, bt):
    note = add_note(bt, 300, 300, text="hello")
    leaf = bt.AddNode("Succeed", 0, 200)
    bt.Connect(bt.GetRootNode(), leaf)
    bt.SetLoopExecution(True)
    fast_config(bt)
    bt.Execute()
    try:
        click_at(bt, text_point(bt, note))
        assert note.IsEditing()
        click_at(bt, label_point(bt, leaf, "parent"))  # does nothing else while executing
        assert not note.IsEditing()
        assert not bt.view()._editor_has_focus()
        bt.SelectNodes([note])
        QTest.keyClick(bt.view(), Qt.Key.Key_Escape)
        assert bt.GetSelectedNodes() == []
    finally:
        bt.Stop()


def test_a_click_that_moves_a_little_still_edits(qtbot, bt):
    note = add_note(bt, 200, 300, text="steady")
    pos = note._item.pos()
    bt._set_modified(False)
    start = text_point(bt, note)
    viewport = bt.view().viewport()
    QTest.mousePress(viewport, LEFT_BUTTON, NO_MOD, start)
    QTest.mouseMove(viewport, start + QPoint(1, 1))
    QTest.mouseRelease(viewport, LEFT_BUTTON, NO_MOD, start + QPoint(1, 1))
    assert note.IsEditing()
    assert note._item.pos() == pos and not bt.IsModified()


def test_a_drag_beyond_the_threshold_moves_the_note_by_the_full_distance(qtbot, bt):
    note = add_note(bt, 200, 300, text="go")
    pos = note._item.pos()
    distance = QApplication.startDragDistance() + 5
    start = text_point(bt, note)
    viewport = bt.view().viewport()
    QTest.mousePress(viewport, LEFT_BUTTON, NO_MOD, start)
    QTest.mouseMove(viewport, start + QPoint(1, 0))
    QTest.mouseMove(viewport, start + QPoint(distance, 0))
    QTest.mouseRelease(viewport, LEFT_BUTTON, NO_MOD, start + QPoint(distance, 0))
    assert note._item.pos() == pos + QPointF(distance, 0)
    assert not note.IsEditing()
