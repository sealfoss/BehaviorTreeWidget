"""Tests of node selection and of copying / pasting nodes (Ctrl+C / Ctrl+V).

Requirement (feature request 1): if one or more nodes are selected and the user presses
Ctrl+C and then Ctrl+V, the nodes are copied and pasted in the areas nearest to the
original nodes that they fit in on the graph.

Selection: a click selects a node, Ctrl + click toggles and Shift + click adds nodes,
Ctrl / Shift + drag on empty space selects with a rectangle, Ctrl+A selects all, a click
on empty space or Escape deselects. Dragging a selected node moves the whole selection.
"""

from __future__ import annotations

import json
import math
import random
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QByteArray, QMimeData, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit

from behavior_tree_widget import (
    EvaluationNodeWidget,
    NegationNodeWidget,
    RootNodeWidget,
    UnknownLeafNodeWidget,
)
from behavior_tree_widget.canvas import PASTE_GAP, Hit, SelectionBandItem, anchor_point, nearest_free_offset
from behavior_tree_widget.nodes import PARENT
from behavior_tree_widget.widget import CLIPBOARD_MIME_TYPE

from conftest import AllFields, Succeed, click, drag, fast_config, viewport_point

LEFT = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


# ============================================================================ helpers
@pytest.fixture(autouse=True)
def empty_clipboard(qapp):
    QGuiApplication.clipboard().clear()
    yield
    QGuiApplication.clipboard().clear()


def title_point(bt, node) -> QPoint:
    return viewport_point(bt, node, node._title_label)


def click_node(bt, node, modifiers=NO_MOD) -> None:
    QTest.mouseClick(bt.view().viewport(), LEFT, modifiers, title_point(bt, node))


def key(bt, key_code, modifiers=NO_MOD) -> None:
    QTest.keyClick(bt.view(), key_code, modifiers)


def copy_paste(bt) -> list:
    """Ctrl+C then Ctrl+V on the view; returns the nodes added."""
    before = set(bt.GetNodes())
    key(bt, Qt.Key.Key_C, CTRL)
    key(bt, Qt.Key.Key_V, CTRL)
    return [node for node in bt.GetNodes() if node not in before]


def rect(node) -> QRectF:
    return node._item.sceneBoundingRect()


def assert_no_overlaps(bt, gap: float = PASTE_GAP) -> None:
    """No two nodes overlap; pasted nodes (selected) keep ``gap`` from the others."""
    nodes = bt.GetNodes()
    pasted = set(bt.GetSelectedNodes())
    for index, a in enumerate(nodes):
        for b in nodes[index + 1:]:
            if (a in pasted) == (b in pasted):
                continue  # only pasted <-> existing pairs are checked
            margin = gap - 0.01
            grown = rect(b).adjusted(-margin, -margin, margin, margin)
            assert not rect(a).intersects(grown), f"{a!r} {rect(a)} overlaps {b!r} {rect(b)}"


def scene_point(bt, x: float, y: float) -> QPoint:
    return bt.view().mapFromScene(QPointF(x, y))


@pytest.fixture
def three(bt):
    """Root, and three unconnected leaves in a row below it; the view shows all of them."""
    root = bt.GetRootNode()
    a = bt.AddNode("Succeed", -300, 250)
    b = bt.AddNode("FailNode", 0, 250)
    c = bt.AddNode("NoFields", 300, 250)
    bt.view().centerOn(40, 220)
    empty = scene_point(bt, 150, 520)
    assert bt.view().hit_test(empty).kind == Hit.EMPTY
    return SimpleNamespace(root=root, a=a, b=b, c=c, empty=empty, view=bt.view())


# ============================================================================ selecting
def test_nothing_is_selected_initially(bt):
    assert bt.GetSelectedNodes() == []


def test_click_selects_a_node(bt, three):
    changes = []
    bt.nodeSelectionChanged.connect(lambda: changes.append(bt.GetSelectedNodes()))
    click_node(bt, three.a)
    assert bt.GetSelectedNodes() == [three.a]
    assert three.view.is_node_selected(three.a)
    assert changes == [[three.a]]
    click_node(bt, three.b)
    assert bt.GetSelectedNodes() == [three.b]


def test_ctrl_click_toggles_and_shift_click_adds(bt, three):
    click_node(bt, three.a)
    click_node(bt, three.c, CTRL)
    assert bt.GetSelectedNodes() == [three.a, three.c]
    click_node(bt, three.a, CTRL)
    assert bt.GetSelectedNodes() == [three.c]
    click_node(bt, three.b, SHIFT)
    click_node(bt, three.b, SHIFT)  # Shift only adds
    assert bt.GetSelectedNodes() == [three.b, three.c]
    positions = [node._item.pos() for node in (three.a, three.b, three.c)]
    QTest.mousePress(three.view.viewport(), LEFT, CTRL, title_point(bt, three.a))
    QTest.mouseMove(three.view.viewport(), title_point(bt, three.a) + QPoint(40, 40))
    QTest.mouseRelease(three.view.viewport(), LEFT, CTRL, title_point(bt, three.a) + QPoint(40, 40))
    assert [node._item.pos() for node in (three.a, three.b, three.c)] == positions  # a modifier click never moves


def test_click_on_empty_space_deselects(bt, three):
    bt.SelectNodes([three.a, three.b])
    click(bt, three.empty)
    assert bt.GetSelectedNodes() == []


def test_panning_keeps_the_selection(bt, three):
    bt.SelectNodes([three.a])
    center = three.view.view_center()
    drag(bt, three.empty, three.empty + QPoint(60, -30))
    assert three.view.view_center() != center
    assert bt.GetSelectedNodes() == [three.a]
    middle = Qt.MouseButton.MiddleButton
    QTest.mousePress(three.view.viewport(), middle, NO_MOD, three.empty)
    QTest.mouseRelease(three.view.viewport(), middle, NO_MOD, three.empty)
    assert bt.GetSelectedNodes() == [three.a]


def test_click_on_a_connection_line_deselects_the_nodes(bt, three):
    bt.Connect(three.root, three.b)
    line = three.view.connection_item(three.b)
    bt.SelectNodes([three.a])
    click(bt, three.view.mapFromScene(line.path().pointAtPercent(0.5)))
    assert three.view.selected_connection() is line
    assert bt.GetSelectedNodes() == []


def test_escape_deselects(bt, three):
    bt.SelectNodes([three.a, three.c])
    key(bt, Qt.Key.Key_Escape)
    assert bt.GetSelectedNodes() == []


def test_ctrl_a_selects_every_node(bt, three):
    three.view.setFocus()
    key(bt, Qt.Key.Key_A, CTRL)
    assert bt.GetSelectedNodes() == bt.GetNodes()


@pytest.mark.parametrize("modifier", [CTRL, SHIFT])
def test_dragging_a_rectangle_selects_the_nodes_it_touches(bt, three, modifier):
    bt.SelectNodes([three.c])
    viewport = three.view.viewport()
    start = scene_point(bt, -350, 200)
    end = scene_point(bt, rect(three.b).left() + 5, rect(three.b).top() + 5)  # touches b's corner
    assert three.view.hit_test(start).kind == Hit.EMPTY
    center = three.view.view_center()
    QTest.mousePress(viewport, LEFT, modifier, start)
    QTest.mouseMove(viewport, end)
    bands = [item for item in three.view.scene().items() if isinstance(item, SelectionBandItem)]
    assert len(bands) == 1 and bands[0].isVisible()
    assert set(bt.GetSelectedNodes()) == {three.a, three.b, three.c}  # live, added to the selection
    QTest.mouseMove(viewport, scene_point(bt, -200, 280))  # shrink: b no longer touched
    assert set(bt.GetSelectedNodes()) == {three.a, three.c}
    QTest.mouseRelease(viewport, LEFT, modifier, scene_point(bt, -200, 280))
    assert set(bt.GetSelectedNodes()) == {three.a, three.c}
    assert [item for item in three.view.scene().items() if isinstance(item, SelectionBandItem)] == []
    assert three.view.view_center() == center  # no panning


def test_a_rectangle_click_without_dragging_keeps_the_selection(bt, three):
    bt.SelectNodes([three.b])
    QTest.mouseClick(three.view.viewport(), LEFT, CTRL, three.empty)
    assert bt.GetSelectedNodes() == [three.b]


def test_dragging_a_selected_node_moves_the_whole_selection(qtbot, bt, three):
    bt.Connect(three.root, three.a)
    bt.SelectNodes([three.a, three.c])
    before = {node: QPointF(node._item.pos()) for node in (three.a, three.b, three.c)}
    bt._set_modified(False)
    drag(bt, title_point(bt, three.a), title_point(bt, three.a) + QPoint(30, 45))
    assert three.a._item.pos() == before[three.a] + QPointF(30, 45)
    assert three.c._item.pos() == before[three.c] + QPointF(30, 45)
    assert three.b._item.pos() == before[three.b]  # not selected: stays
    assert bt.GetSelectedNodes() == [three.a, three.c]
    assert bt.IsModified()
    end = three.view.connection_item(three.a).path().pointAtPercent(1.0)
    assert (end - anchor_point(three.a, PARENT)).manhattanLength() < 0.01  # the line follows


def test_dragging_an_unselected_node_moves_only_it_and_selects_it(bt, three):
    bt.SelectNodes([three.a, three.c])
    before = QPointF(three.c._item.pos())
    drag(bt, title_point(bt, three.b), title_point(bt, three.b) + QPoint(20, 20))
    assert bt.GetSelectedNodes() == [three.b]
    assert three.c._item.pos() == before


def test_click_on_one_of_several_selected_nodes_selects_only_it(bt, three):
    bt.SelectNodes([three.a, three.b, three.c])
    click_node(bt, three.b)
    assert bt.GetSelectedNodes() == [three.b]


def test_deleted_nodes_leave_the_selection(bt, three):
    bt.SelectNodes([three.a, three.b])
    bt.RemoveNode(three.a)
    assert bt.GetSelectedNodes() == [three.b]


def test_loading_another_tree_clears_the_selection(bt, three, tmp_path):
    path = tmp_path / "other.json"
    assert bt.SaveTree(str(path))
    bt.SelectNodes(bt.GetNodes())
    assert bt.LoadTree(str(path))
    assert bt.GetSelectedNodes() == []


def test_select_nodes_api(bt, three):
    bt.SelectNodes([three.a])
    bt.SelectNodes([three.c], add=True)
    assert bt.GetSelectedNodes() == [three.a, three.c]
    other = Succeed()  # not part of the tree: ignored
    bt.SelectNodes([other])
    assert bt.GetSelectedNodes() == []
    other.deleteLater()


def test_selection_works_while_executing(qtbot, bt, three):
    bt.Connect(three.root, bt.AddNode("SlowCancelable", -600, 250))
    fast_config(bt)
    bt.Execute()
    try:
        click_node(bt, three.a)
        click_node(bt, three.b, CTRL)
        assert bt.GetSelectedNodes() == [three.a, three.b]
    finally:
        bt.Stop()


# ============================================================================ copy / paste
def test_ctrl_c_ctrl_v_pastes_a_copy_beside_the_original(bt, three):
    three.b.SetTitle("Renamed")
    click_node(bt, three.b)
    [copy] = copy_paste(bt)
    assert type(copy) is type(three.b) and copy.GetTitle() == "Renamed"
    assert copy.GetId() != three.b.GetId()
    assert copy.GetParent() is None
    assert bt.GetSelectedNodes() == [copy]  # the pasted nodes become the selection
    assert_no_overlaps(bt)
    # the nearest free spot: the copy's distance from the original is the smallest that fits
    offset = copy._item.pos() - three.b._item.pos()
    original = rect(three.b)
    nearest = min(original.width(), original.height()) + PASTE_GAP
    assert math.hypot(offset.x(), offset.y()) == pytest.approx(nearest, abs=0.01)
    assert bt.IsModified()


def test_the_view_scrolls_to_show_pasted_nodes(bt, three):
    bt.SelectNodes([three.b])
    three.view.setFocus()
    key(bt, Qt.Key.Key_C, CTRL)
    three.view.centerOn(5000, 5000)  # the user panned far away
    before = set(bt.GetNodes())
    key(bt, Qt.Key.Key_V, CTRL)
    [copy] = [node for node in bt.GetNodes() if node not in before]
    visible = three.view.mapToScene(three.view.viewport().rect()).boundingRect()
    assert visible.contains(rect(copy))
    offset = copy._item.pos() - three.b._item.pos()
    assert math.hypot(offset.x(), offset.y()) < 200  # still placed beside the original


def test_pasting_visible_nodes_does_not_scroll(bt, three):
    bt.SelectNodes([three.b])
    three.view.setFocus()
    center = three.view.view_center()
    copy_paste(bt)
    assert three.view.view_center() == center


def test_pasted_fields_and_selections_are_copies(bt):
    node = bt.AddNode(AllFields, 0, 250)
    node.SetField("count", 42)
    node.SetField("label", "copied")
    node.SetFieldSelectionIndex("choice", 2)
    bt.view().centerOn(rect(node).center())
    click_node(bt, node)
    [copy] = copy_paste(bt)
    assert isinstance(copy, AllFields)
    assert copy.GetFields() == node.GetFields()
    assert copy.GetFieldSelection("choice") == "blue"
    copy.SetField("count", 1)
    assert node.GetField("count") == 42


def test_paste_keeps_the_connections_between_the_copied_nodes(bt):
    root = bt.GetRootNode()
    sequence = bt.AddNode("Sequence", 0, 200)
    sequence.SetMemory(False)
    first = bt.AddNode("Succeed", -120, 380)
    second = bt.AddNode("FailNode", 120, 380)
    outside = bt.AddNode("NoFields", 400, 380)
    bt.Connect(root, sequence)
    bt.Connect(sequence, first)
    bt.Connect(sequence, second)
    bt.Connect(sequence, outside)  # not copied
    bt.view().centerOn(0, 300)
    bt.SelectNodes([sequence, first, second])
    bt.view().setFocus()
    pasted = copy_paste(bt)
    assert len(pasted) == 3
    by_type = {node.GetTypeName(): node for node in pasted}
    new_sequence, new_first, new_second = by_type["Sequence"], by_type["Succeed"], by_type["FailNode"]
    assert new_sequence.GetMemory() is False
    assert new_sequence.GetParent() is None  # the connection to the (not copied) root is not copied
    assert new_sequence.GetChildren() == [new_first, new_second]  # same order
    assert sequence.GetChildren() == [first, second, outside]  # the originals are untouched
    # the copies keep their arrangement
    assert new_first._item.pos() - new_sequence._item.pos() == first._item.pos() - sequence._item.pos()
    assert new_second._item.pos() - new_sequence._item.pos() == second._item.pos() - sequence._item.pos()
    assert set(bt.GetSelectedNodes()) == set(pasted)
    assert_no_overlaps(bt)


def test_the_root_is_never_copied(bt, three):
    bt.Connect(three.root, three.a)
    bt.SelectNodes([three.root, three.a])
    three.view.setFocus()
    pasted = copy_paste(bt)
    assert [type(node) for node in pasted] == [type(three.a)]
    assert len([node for node in bt.GetNodes() if isinstance(node, RootNodeWidget)]) == 1


def test_copying_only_the_root_leaves_the_clipboard_alone(bt, three):
    QGuiApplication.clipboard().setText("keep me")
    bt.SelectNodes([three.root])
    three.view.setFocus()
    key(bt, Qt.Key.Key_C, CTRL)
    assert QGuiApplication.clipboard().text() == "keep me"
    assert bt.CopyNodes() is False


def test_repeated_pastes_each_find_free_space(bt, three):
    bt.SelectNodes([three.b])
    three.view.setFocus()
    key(bt, Qt.Key.Key_C, CTRL)
    copies = []
    for _ in range(4):
        before = set(bt.GetNodes())
        key(bt, Qt.Key.Key_V, CTRL)
        copies += [node for node in bt.GetNodes() if node not in before]
    assert len(copies) == 4
    nodes = bt.GetNodes()
    for index, a in enumerate(nodes):
        for b in nodes[index + 1:]:
            assert not rect(a).intersects(rect(b)), f"{a!r} overlaps {b!r}"


def test_paste_finds_space_between_crowded_nodes(bt):
    """Copies of a node surrounded on three sides go to the free side."""
    center = bt.AddNode("Succeed", 0, 300)
    size = rect(center).size()
    w, h = size.width(), size.height()
    bt.AddNode("Succeed", w + 10, 300)  # right
    bt.AddNode("Succeed", 0, 300 + h + 10)  # below
    bt.AddNode("Succeed", -w - 10, 300)  # left
    bt.view().centerOn(rect(center).center())
    bt.SelectNodes([center])
    bt.view().setFocus()
    [copy] = copy_paste(bt)
    assert_no_overlaps(bt)
    assert copy._item.pos().x() == pytest.approx(0.0)
    assert copy._item.pos().y() < 300  # above: the only near free side


@pytest.mark.parametrize("type_name", ["Negation", "Selector", "Evaluation", "Set"])
def test_built_in_nodes_are_pasted_with_their_settings(bt, type_name):
    bt.AddEntry("i", "Integer", 1)
    bt.AddEntry("i2", "Integer", 2)
    node = bt.AddNode(type_name, 0, 250)
    if type_name in ("Evaluation", "Set"):
        node.SetValueName("i")
        node.SetCompareTo("i2")
    elif type_name == "Selector":
        node.SetMemory(False)
    bt.view().centerOn(rect(node).center())
    bt.SelectNodes([node])
    bt.view().setFocus()
    [copy] = copy_paste(bt)
    assert copy.GetTypeName() == type_name and type(copy) is type(node)
    if type_name in ("Evaluation", "Set"):
        assert copy.GetValueName() == "i" and copy.GetCompareTo() == "i2"
    elif type_name == "Selector":
        assert copy.GetCompositeType() == "Selector" and copy.GetMemory() is False


def test_ctrl_c_in_a_field_editor_copies_text_not_nodes(bt):
    node = bt.AddNode(AllFields, 0, 250)
    bt.view().centerOn(rect(node).center())
    click_node(bt, node)
    editor = node.field_editor("label")
    click(bt, viewport_point(bt, node, editor))
    key(bt, Qt.Key.Key_A, CTRL)
    key(bt, Qt.Key.Key_C, CTRL)
    assert QGuiApplication.clipboard().text() == "hello"
    assert not QGuiApplication.clipboard().mimeData().hasFormat(CLIPBOARD_MIME_TYPE)
    count = len(bt.GetNodes())
    key(bt, Qt.Key.Key_V, CTRL)  # pastes the text into the editor
    assert len(bt.GetNodes()) == count
    assert node.GetField("label") == "hello"
    key(bt, Qt.Key.Key_A, CTRL)  # selects the editor's text, not the nodes
    assert isinstance(editor, QLineEdit) and editor.selectedText() == "hello"
    assert bt.GetSelectedNodes() == [node]


def test_application_shortcuts_do_not_take_the_view_keys(bt, three):
    """A window-wide Ctrl+C / Ctrl+V action (e.g. an Edit menu) does not steal the keys from the view."""
    triggered = []
    for sequence in (QKeySequence.StandardKey.Copy, QKeySequence.StandardKey.Paste):
        shortcut = QAction(bt)
        shortcut.setShortcut(QKeySequence(sequence))
        shortcut.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        shortcut.triggered.connect(lambda _=False, s=sequence: triggered.append(s))
        bt.addAction(shortcut)
    click_node(bt, three.a)
    pasted = copy_paste(bt)
    assert len(pasted) == 1
    assert triggered == []


def test_paste_with_text_or_nothing_on_the_clipboard_does_nothing(bt, three):
    three.view.setFocus()
    count = len(bt.GetNodes())
    key(bt, Qt.Key.Key_V, CTRL)
    QGuiApplication.clipboard().setText("just text")
    key(bt, Qt.Key.Key_V, CTRL)
    assert len(bt.GetNodes()) == count
    assert bt.PasteNodes() == []


@pytest.mark.parametrize("payload", [
    b"not json",
    b'{"format": "something else", "version": 1, "nodes": []}',
    b'{"format": "behavior_tree_widget_nodes", "version": 99, "nodes": []}',
    b'{"format": "behavior_tree_widget_nodes", "version": 1, "nodes": {}}',
    b'{"format": "behavior_tree_widget_nodes", "version": 1, "nodes": [3, {"type": "Succeed"}]}',
])
def test_damaged_clipboard_data_is_ignored(bt, three, payload):
    mime = QMimeData()
    mime.setData(CLIPBOARD_MIME_TYPE, QByteArray(payload))
    QGuiApplication.clipboard().setMimeData(mime)
    count = len(bt.GetNodes())
    assert bt.PasteNodes() == []
    assert len(bt.GetNodes()) == count


def test_clipboard_holds_the_tree_file_node_format(bt, three):
    bt.Connect(three.root, three.a)
    assert bt.CopyNodes([three.a, three.b])
    data = json.loads(bytes(QGuiApplication.clipboard().mimeData().data(CLIPBOARD_MIME_TYPE)).decode("ascii"))
    assert data["format"] == "behavior_tree_widget_nodes" and data["version"] == 1
    assert [node["type"] for node in data["nodes"]] == ["Succeed", "FailNode"]
    assert data["connections"] == []  # the root was not copied


def test_paste_into_another_widget(make_widget, bt, three, tmp_path):
    three.a.SetTitle("Shared")
    assert bt.CopyNodes([three.a])
    other = make_widget()
    assert other.NewTree(str(tmp_path / "other.json"))
    pasted = other.PasteNodes()
    assert len(pasted) == 1 and pasted[0].GetTitle() == "Shared"
    assert pasted[0] in other.GetNodes() and pasted[0] not in bt.GetNodes()


def test_paste_into_a_widget_without_the_node_type_creates_a_placeholder(make_widget, bt, three, tmp_path):
    node = bt.AddNode(AllFields, 0, 600)
    assert bt.CopyNodes([node])
    other = make_widget(node_types=[])
    assert other.NewTree(str(tmp_path / "plain.json"))
    [pasted] = other.PasteNodes()
    assert isinstance(pasted, UnknownLeafNodeWidget)
    assert pasted.GetTypeName() == "AllFields"
    assert pasted.GetFields() == node.GetFields()


def test_copy_and_paste_api(bt, three):
    assert bt.CopyNodes([three.a, three.c])
    pasted = bt.PasteNodes()
    assert sorted(node.GetTypeName() for node in pasted) == ["NoFields", "Succeed"]
    assert bt.GetSelectedNodes() == pasted
    assert bt.CopyNodes([]) is False


def test_paste_is_refused_while_executing_and_copy_still_works(qtbot, bt, three):
    bt.Connect(three.root, bt.AddNode("SlowCancelable", -600, 250))
    fast_config(bt)
    bt.Execute()
    try:
        assert bt.CopyNodes([three.a])
        count = len(bt.GetNodes())
        with pytest.raises(RuntimeError):
            bt.PasteNodes()
        three.view.setFocus()
        key(bt, Qt.Key.Key_V, CTRL)  # ignored
        assert len(bt.GetNodes()) == count
    finally:
        bt.Stop()
    assert len(bt.PasteNodes()) == 1


def test_pasted_nodes_can_be_moved_together(bt):
    sequence = bt.AddNode("Sequence", 0, 200)
    leaf = bt.AddNode("Succeed", 0, 380)
    bt.Connect(sequence, leaf)
    bt.view().centerOn(0, 300)
    bt.SelectNodes([sequence, leaf])
    bt.view().setFocus()
    new_sequence, new_leaf = sorted(copy_paste(bt), key=lambda node: node._item.pos().y())
    bt.view().centerOn(rect(new_sequence).center())
    before = new_leaf._item.pos()
    drag(bt, title_point(bt, new_sequence), title_point(bt, new_sequence) + QPoint(15, 25))
    assert new_leaf._item.pos() == before + QPointF(15, 25)
    assert new_leaf.GetParent() is new_sequence


def test_a_negation_with_its_child_is_pasted_whole(bt):
    negation = bt.AddNode("Negation", 0, 200)
    leaf = bt.AddNode("Succeed", 0, 380)
    bt.Connect(negation, leaf)
    bt.SelectNodes([negation, leaf])
    bt.view().setFocus()
    pasted = copy_paste(bt)
    [new_negation] = [node for node in pasted if isinstance(node, NegationNodeWidget)]
    assert new_negation.GetChild() in pasted and new_negation.GetChild() is not leaf
    assert negation.GetChild() is leaf


def test_evaluation_copy_follows_the_blackboard(bt):
    bt.AddEntry("x", "String", "a")
    node = bt.AddNode("Evaluation", 0, 250)
    node.SetValueName("x")
    bt.SelectNodes([node])
    bt.view().setFocus()
    [copy] = copy_paste(bt)
    assert isinstance(copy, EvaluationNodeWidget)
    bt.blackboardStore().rename("x", "y")
    assert copy.GetValueName() == "y" and node.GetValueName() == "y"


def test_copied_nodes_do_not_crash_the_process_at_exit(tmp_path):
    """Without a system clipboard (offscreen), PySide6 crashed destroying a clipboard holding
    copied nodes at exit; the widget drops them from such a clipboard at exit."""
    script = tmp_path / "copy_and_exit.py"
    script.write_text(textwrap.dedent("""
        import os, sys
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        from behavior_tree_widget import BehaviorTreeWidget
        widget = BehaviorTreeWidget()
        assert widget.NewTree(sys.argv[1])
        assert widget.CopyNodes([widget.AddNode("Negation", 0, 200)])
        widget._set_modified(False)
        widget.Shutdown()
        print("copied")
    """), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "tree.json")], capture_output=True, text=True, timeout=120
    )
    assert "copied" in result.stdout, result.stderr
    assert result.returncode == 0, f"exit code {result.returncode}: {result.stderr}"


# ============================================================================ placement
def test_nearest_free_offset_without_obstacles_is_zero():
    assert nearest_free_offset([QRectF(0, 0, 10, 10)], []) == QPointF(0, 0)
    assert nearest_free_offset([QRectF(0, 0, 10, 10)], [QRectF(100, 100, 10, 10)]) == QPointF(0, 0)


def test_nearest_free_offset_moves_by_the_smaller_extent():
    wide = QRectF(0, 0, 100, 40)
    assert nearest_free_offset([wide], [QRectF(wide)], gap=20) == QPointF(0, 60)  # below
    tall = QRectF(0, 0, 40, 100)
    assert nearest_free_offset([tall], [QRectF(tall)], gap=20) == QPointF(60, 0)  # right


def test_nearest_free_offset_prefers_right_then_down_on_ties():
    square = QRectF(0, 0, 50, 50)
    assert nearest_free_offset([square], [QRectF(square)], gap=10) == QPointF(60, 0)
    blocked_right = [QRectF(square), QRectF(60, 0, 50, 50)]
    assert nearest_free_offset([square], blocked_right, gap=10) == QPointF(0, 60)


def test_nearest_free_offset_allows_exactly_the_gap():
    group = [QRectF(0, 0, 10, 10)]
    offset = nearest_free_offset(group, [QRectF(0, 0, 10, 10)], gap=5)
    assert offset == QPointF(15, 0)


def test_nearest_free_offset_moves_groups_as_a_whole():
    group = [QRectF(0, 0, 10, 10), QRectF(30, 0, 10, 10)]  # a gap of 20 between them
    obstacles = [QRectF(r) for r in group] + [QRectF(12, 30, 16, 10)]
    offset = nearest_free_offset(group, obstacles, gap=2)
    for r in group:
        moved = r.translated(offset)
        for o in obstacles:
            assert not moved.intersects(o.adjusted(-1.99, -1.99, 1.99, 1.99))


def _free(group, obstacles, dx, dy, gap):
    for r in group:
        moved = r.translated(dx, dy)
        for o in obstacles:
            if (moved.left() < o.right() + gap - 1e-9 and moved.right() > o.left() - gap + 1e-9
                    and moved.top() < o.bottom() + gap - 1e-9 and moved.bottom() > o.top() - gap + 1e-9):
                return False
    return True


@pytest.mark.parametrize("seed", range(12))
def test_nearest_free_offset_is_free_and_nothing_nearer_is(seed):
    rng = random.Random(seed)
    obstacles = [
        QRectF(rng.uniform(-500, 500), rng.uniform(-300, 300), rng.uniform(40, 220), rng.uniform(40, 160))
        for _ in range(rng.randint(2, 18))
    ]
    group = [QRectF(r) for r in rng.sample(obstacles, rng.randint(1, 3))]
    gap = 20.0
    offset = nearest_free_offset(group, obstacles, gap)
    assert _free(group, obstacles, offset.x(), offset.y(), gap)
    distance = math.hypot(offset.x(), offset.y())
    radius = 0.0
    while radius < distance - 3.0:  # probe circles closer than the result: all blocked
        steps = max(12, int(2 * math.pi * radius / 4.0))
        for step in range(steps):
            angle = 2 * math.pi * step / steps
            assert not _free(group, obstacles, radius * math.cos(angle), radius * math.sin(angle), gap)
        radius += 4.0


def test_pasting_many_nodes_stays_fast(bt):
    import time

    nodes = [bt.AddNode("Succeed", (index % 12) * 110.0, 200.0 + (index // 12) * 110.0) for index in range(96)]
    bt.SelectNodes(nodes[:24])
    assert bt.CopyNodes()
    started = time.perf_counter()
    pasted = bt.PasteNodes()
    assert len(pasted) == 24
    assert time.perf_counter() - started < 5.0
    assert_no_overlaps(bt)

