"""The cached order of a node's children (NodeWidget.GetChildren) and the order badges that show it."""

from __future__ import annotations

import random

import pytest
from PySide6.QtCore import QPoint, QPointF

from behavior_tree_widget import CompositeNodeWidget
from behavior_tree_widget.canvas import ConnectionItem

from conftest import Succeed, drag, viewport_point


def full_order(parent):
    """The order GetChildren must give: left to right by centre, then top; ties in connection order."""
    return sorted(list(parent._children), key=lambda node: node._order_key())


def badges(view, parent) -> list[int]:
    return [view.connection_item(child).order() for child in parent.GetChildren()]


@pytest.fixture
def fan(bt):
    """Root -> Sequence -> nine leaves side by side."""
    root = bt.GetRootNode()
    seq = bt.AddNode("Sequence", 0, 160)
    bt.Connect(root, seq)
    leaves = [bt.AddNode("Succeed", index * 150 - 600, 320) for index in range(9)]
    for leaf in leaves:
        bt.Connect(seq, leaf)
    return bt, seq, leaves


def test_random_moves_keep_the_cached_order_exact(fan):
    bt, seq, leaves = fan
    view = bt.view()
    rng = random.Random(7)
    for _ in range(300):
        leaf = rng.choice(leaves)
        # Few distinct places, so that children often share a centre or a top.
        leaf._item.setPos(QPointF(rng.choice(range(-900, 901, 75)), rng.choice((300.0, 320.0, 340.0))))
        assert seq.GetChildren() == full_order(seq)
        assert badges(view, seq) == list(range(1, 10))


def test_children_at_the_same_place_keep_the_order_they_were_connected_in(fan):
    bt, seq, leaves = fan
    first, second = leaves[2], leaves[6]  # connected in this order
    second._item.setPos(first._item.pos())
    children = seq.GetChildren()
    assert children.index(first) < children.index(second)
    assert children == full_order(seq)
    first._item.setPos(first._item.pos() + QPointF(500, 0))
    first._item.setPos(second._item.pos())  # back onto the same place, after the other moved there
    children = seq.GetChildren()
    assert children.index(first) < children.index(second)
    assert children == full_order(seq)


def test_only_the_badges_of_children_that_change_place_are_rewritten(fan, monkeypatch):
    bt, seq, leaves = fan
    rewritten = []
    original = ConnectionItem.set_order

    def recording(self, order):
        rewritten.append(self.child_node)
        original(self, order)

    monkeypatch.setattr(ConnectionItem, "set_order", recording)
    leaves[4]._item.setPos(leaves[4]._item.pos() + QPointF(0, 30))  # same place in the order
    assert rewritten == []
    leaves[4]._item.setPos(leaves[4]._item.pos() + QPointF(320, 0))  # past two siblings
    assert set(rewritten) == {leaves[4], leaves[5], leaves[6]}
    assert badges(bt.view(), seq) == list(range(1, 10))
    assert seq.GetChildren().index(leaves[4]) == 6


def test_while_locked_the_order_follows_moves_and_the_badges_wait(fan):
    bt, seq, leaves = fan
    view = bt.view()
    first = leaves[0]
    view.set_locked(True)
    try:
        first._item.setPos(leaves[-1]._item.pos() + QPointF(200, 0))
        assert seq.GetChildren()[-1] is first, "the next Execute uses the new order"
        assert view.connection_item(first).order() == 1, "the badges keep the order of the running tree"
    finally:
        view.set_locked(False)
    assert view.connection_item(first).order() == 9
    assert badges(view, seq) == list(range(1, 10))


def test_moving_selected_siblings_together_keeps_the_order_exact(fan, qtbot):
    bt, seq, leaves = fan
    bt.SelectNodes([leaves[1], leaves[2], leaves[3]])
    start = viewport_point(bt, leaves[2], leaves[2]._title_label)
    bt.view().centerOn(leaves[2]._item.sceneBoundingRect().center())
    qtbot.wait(20)
    start = viewport_point(bt, leaves[2], leaves[2]._title_label)
    drag(bt, start, start + QPoint(470, 0), steps=7)
    assert seq.GetChildren() == full_order(seq)
    assert badges(bt.view(), seq) == list(range(1, 10))


def test_connecting_and_disconnecting_children_refreshes_the_order(fan):
    bt, seq, leaves = fan
    bt.Disconnect(leaves[4])
    assert leaves[4] not in seq.GetChildren()
    assert seq.GetChildren() == full_order(seq)
    extra = bt.AddNode("Succeed", -10, 330)
    bt.Connect(seq, extra)
    assert seq.GetChildren() == full_order(seq)
    assert badges(bt.view(), seq) == list(range(1, 10))


def test_get_children_returns_a_copy(fan):
    bt, seq, leaves = fan
    children = seq.GetChildren()
    children.reverse()
    assert seq.GetChildren() == full_order(seq)


def test_nodes_outside_a_view_are_ordered_by_their_saved_place(qapp):
    parent = CompositeNodeWidget()
    children = [Succeed(), Succeed(), Succeed()]
    try:
        for child, x in zip(children, (300.0, -50.0, 120.0)):
            child.__dict__["_detached_order"] = (x, 0.0)
            parent._link(child)
        assert parent.GetChildren() == [children[1], children[2], children[0]]
        # A child linked by hand (as loaders did before _link existed) is seen as well.
        extra = Succeed()
        children.append(extra)
        extra.__dict__["_detached_order"] = (0.0, 0.0)
        with parent._lock:
            parent._children.append(extra)
        extra._parent = parent
        assert parent.GetChildren() == [children[1], extra, children[2], children[0]]
        parent._unlink(children[1])
        assert parent.GetChildren() == [extra, children[2], children[0]]
    finally:
        for node in (parent, *children):
            node.deleteLater()


def test_child_moved_reports_how_the_place_changed(fan):
    bt, seq, leaves = fan
    seq._order_nodes = None
    assert seq._child_moved(leaves[0]) is None, "nothing cached yet"
    seq.GetChildren()
    assert seq._child_moved(leaves[0]) == (0, 0), "it did not move"
    leaves[0]._item.setPos(leaves[0]._item.pos() + QPointF(400, 0))  # the view reports this move itself
    assert seq.GetChildren().index(leaves[0]) == 2
