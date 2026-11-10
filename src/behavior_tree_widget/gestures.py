"""Mouse gestures of the tree view (internal; used by :class:`~behavior_tree_widget.canvas.TreeView`).

A mouse press on the view starts at most one gesture. The view hands the gesture the mouse
moves and the release, and ends it. Each gesture keeps its own state:

* :class:`PanGesture` - Ctrl + drag, or a middle button drag: scrolls the view. A Ctrl + click
  without dragging toggles the node clicked in the selection.
* :class:`MoveGesture` - dragging a node moves it, with the other selected nodes and with
  everything lying inside the groups being moved. A click (without dragging) on one of several
  selected nodes selects only it; a click on the text of a note edits it.
* :class:`ResizeGesture` - dragging a resize handle resizes a note or a group.
* :class:`ConnectGesture` - dragging from a connection label draws the drag line (green over a
  compatible label) and connects on release, or offers the add-node menu over empty canvas.
* :class:`BandGesture` - dragging over empty canvas selects the nodes the rectangle touches.
* :class:`PassThroughGesture` - the press went to an input widget of a node or to a popup: the
  view forwards the events to the scene until the button is released.

``TreeView._gesture`` reports the :class:`GestureKind` of the gesture in progress.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, override

from PySide6.QtCore import QPoint, QPointF, QRectF, QSizeF, Qt
from PySide6.QtWidgets import QApplication, QGraphicsView

from .geometry import anchor_point
from .groups import GroupNodeWidget
from .items import DragLineItem, NodeItem, SelectionBandItem
from .nodes import CHILDREN, NodeWidget
from .notes import BOTTOM, LEFT, RIGHT, TOP, BoxNodeWidget, NotesNodeWidget, ResizeHandle

if TYPE_CHECKING:  # pragma: no cover
    from PySide6.QtGui import QTransform

    from .canvas import TreeView

__all__ = [
    "BandGesture",
    "ConnectGesture",
    "Gesture",
    "GestureKind",
    "MoveGesture",
    "PanGesture",
    "PassThroughGesture",
    "ResizeGesture",
]

LEFT_BUTTON = Qt.MouseButton.LeftButton


class GestureKind(enum.Enum):
    """What the mouse is doing in the view (``TreeView._gesture``)."""

    IDLE = 0
    PANNING = 1
    MOVING_NODE = 2
    CONNECTING = 3
    PASSTHROUGH = 4
    SELECTING = 5
    RESIZING = 6


class Gesture:
    """A gesture in progress (see the module docs). The view starts it, hands it the mouse
    moves and the release, and ends it with :meth:`TreeView._reset_gesture`."""

    kind = GestureKind.IDLE

    def __init__(self, view: TreeView):
        self.view = view

    def holds(self, buttons: Qt.MouseButton) -> bool:
        """True while the mouse button driving the gesture is among the pressed ``buttons``."""
        return bool(buttons & LEFT_BUTTON)

    def move(self, pos: QPoint, event) -> None:
        """The mouse moved to viewport position ``pos``."""
        event.accept()

    def release(self, pos: QPoint, event) -> None:
        """A mouse button was released at ``pos``; the gesture ends if it is its own button."""

    def cancel(self) -> None:
        """Clean up after the view ended the gesture without completing it."""

    def abandon(self) -> bool:
        """The release was lost (e.g. a modal dialog took the mouse). Returns True if the gesture
        changed the tree (moved or resized nodes), which the view then reports."""
        return False

    def involves(self, node: NodeWidget) -> bool:
        """True if the gesture holds ``node``: it ends when that node is removed."""
        return False

    def forget(self, node: NodeWidget) -> None:
        """``node`` is being removed from the view."""


class PassThroughGesture(Gesture):
    """The press went to an input widget or a popup: events go to the scene until the release."""

    kind = GestureKind.PASSTHROUGH

    @override
    def holds(self, buttons: Qt.MouseButton) -> bool:
        return True

    @override
    def move(self, pos: QPoint, event) -> None:
        QGraphicsView.mouseMoveEvent(self.view, event)

    @override
    def release(self, pos: QPoint, event) -> None:
        view = self.view
        QGraphicsView.mouseReleaseEvent(view, event)
        if not (event.buttons() & LEFT_BUTTON) and view._active is self:
            view._active = None

    @override
    def abandon(self) -> bool:
        # The node that received the press still holds the scene's implicit mouse grab.
        grabber = self.view.scene().mouseGrabberItem()
        if isinstance(grabber, NodeItem):
            grabber.ungrabMouse()
        return False


class PanGesture(Gesture):
    """Scrolls the view while ``button`` is held (see the module docs)."""

    kind = GestureKind.PANNING

    def __init__(self, view: TreeView, pos: QPoint, button: Qt.MouseButton, toggles: NodeWidget | None = None):
        super().__init__(view)
        view._clear_scene_focus()
        view._last_view_pos = QPoint(pos)
        self.button = button
        self.origin = QPoint(pos)
        self.panned = False
        self.toggles = toggles  # a Ctrl + click (without dragging) on this node toggles it in the selection

    @override
    def holds(self, buttons: Qt.MouseButton) -> bool:
        return bool(buttons & self.button)

    @override
    def move(self, pos: QPoint, event) -> None:
        view = self.view
        if (
            not self.panned
            and self.button == LEFT_BUTTON
            and (pos - self.origin).manhattanLength() < QApplication.startDragDistance()
        ):
            event.accept()  # not a drag yet: a Ctrl + click that moves a little is still a click
            return
        delta = pos - view._last_view_pos
        if not delta.isNull():
            if not self.panned:
                self.panned = True
                view.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            view.horizontalScrollBar().setValue(view.horizontalScrollBar().value() - delta.x())
            view.verticalScrollBar().setValue(view.verticalScrollBar().value() - delta.y())
            view.navigated.emit()
        view._last_view_pos = pos
        event.accept()

    @override
    def release(self, pos: QPoint, event) -> None:
        if event.button() != self.button:
            return
        view = self.view
        toggled = self.toggles if not self.panned else None
        view._reset_gesture()
        if toggled is not None and toggled in view.document:
            if view.is_node_selected(toggled):
                view.deselect_nodes([toggled])
            else:
                view.select_nodes([toggled], add=True)


class MoveGesture(Gesture):
    """Moves ``node``, together with the other selected nodes if it is selected, and with what
    lies inside the groups being moved."""

    kind = GestureKind.MOVING_NODE

    def __init__(self, view: TreeView, node: NodeWidget, pos: QPoint, click_selects: NodeWidget | None = None):
        super().__init__(view)
        view._clear_scene_focus()
        self.node = node
        self.moved = False
        self.origin = QPoint(pos)  # viewport position of the press
        self.press_scene_pos = view.mapToScene(pos)
        self.click_selects = click_selects  # a click (no drag) on it selects only it
        members = view.selected_nodes() if view.is_node_selected(node) else [node]
        shown = [(member, member._item) for member in view._with_group_contents(members)]
        items = [(member, item) for member, item in shown if item is not None]
        self.group: list[tuple[NodeWidget, QPointF]] = [(member, QPointF(item.pos())) for member, item in items]
        # Raise the moved nodes above the others, keeping their stacking order and the grabbed one on top.
        for member, _item in sorted(items, key=lambda pair: (pair[0] is node, pair[1].zValue())):
            view.raise_node(member)
        view.viewport().setCursor(Qt.CursorShape.SizeAllCursor)

    @override
    def move(self, pos: QPoint, event) -> None:
        view = self.view
        if (
            not self.moved
            and isinstance(self.node, NotesNodeWidget)
            and (pos - self.origin).manhattanLength() < QApplication.startDragDistance()
        ):
            event.accept()  # not a drag yet: a click on a note that moves a little still edits it
            return
        view._last_view_pos = pos
        offset = view.mapToScene(pos) - self.press_scene_pos
        if not self.moved and (abs(offset.x()) > 0 or abs(offset.y()) > 0):
            self.moved = True
        for member, start in self.group:
            if member._item is not None:
                member._item.setPos(start + offset)
        event.accept()

    @override
    def release(self, pos: QPoint, event) -> None:
        if event.button() != LEFT_BUTTON:
            return
        view = self.view
        moved, clicked, node, press = self.moved, self.click_selects, self.node, self.press_scene_pos
        view._reset_gesture()
        if moved:
            view.modified.emit()
            return
        if clicked is not None:
            view.select_nodes([clicked])  # a click on one of several selected nodes selects only it
        if isinstance(node, NotesNodeWidget) and view._is_current(node) and node._item is not None:
            node.begin_editing(node._item.mapFromScene(press))  # a click on the text of a note edits it

    @override
    def abandon(self) -> bool:
        return self.moved

    @override
    def involves(self, node: NodeWidget) -> bool:
        return node is self.node or any(member is node for member, _ in self.group)


class ResizeGesture(Gesture):
    """Resizes the note or group ``node`` by dragging ``handle``; the opposite edges stay in place."""

    kind = GestureKind.RESIZING

    def __init__(
        self,
        view: TreeView,
        node: BoxNodeWidget,
        handle: ResizeHandle,
        pos: QPoint,
        click_selects: NodeWidget | None = None,
    ):
        super().__init__(view)
        view._clear_scene_focus()
        self.node = node
        self.edges = handle.edges  # notes.LEFT, TOP, RIGHT, BOTTOM moved by the handle
        self.resized = False
        self.press_scene_pos = view.mapToScene(pos)
        item = node._item
        assert item is not None, "a node being resized is in the view"
        self.start = QRectF(item.pos(), QSizeF(*node.GetBoxSize()))  # the node's scene rectangle
        self.click_selects = click_selects
        view.raise_node(node)
        view.viewport().setCursor(handle.cursor())

    @override
    def move(self, pos: QPoint, event) -> None:
        self.view._last_view_pos = pos
        self.update(pos)
        event.accept()

    def update(self, pos: QPoint) -> None:
        """Move the grabbed edges to the mouse."""
        node = self.node
        if node._item is None:
            return
        offset = self.view.mapToScene(pos) - self.press_scene_pos
        start, edges = self.start, self.edges
        width, height = start.width(), start.height()
        if edges & LEFT:
            width -= offset.x()
        elif edges & RIGHT:
            width += offset.x()
        if edges & TOP:
            height -= offset.y()
        elif edges & BOTTOM:
            height += offset.y()
        width, height = node._resize_box(width, height)
        x = start.right() - width if edges & LEFT else start.left()
        y = start.bottom() - height if edges & TOP else start.top()
        node._item.setPos(QPointF(x, y))
        if (width, height) != (start.width(), start.height()):
            self.resized = True

    @override
    def release(self, pos: QPoint, event) -> None:
        if event.button() != LEFT_BUTTON:
            return
        view = self.view
        view._last_view_pos = pos
        self.update(pos)
        resized, clicked = self.resized, self.click_selects
        view._reset_gesture()
        if resized:
            view.modified.emit()
        elif clicked is not None:
            view.select_nodes([clicked])

    @override
    def abandon(self) -> bool:
        return self.resized

    @override
    def involves(self, node: NodeWidget) -> bool:
        return node is self.node


class BandGesture(Gesture):
    """Selects the nodes touched by a rectangle dragged open from the press; with ``add`` they
    are added to the selection, otherwise they replace it (so a click deselects every node)."""

    kind = GestureKind.SELECTING

    def __init__(self, view: TreeView, pos: QPoint, add: bool = False):
        super().__init__(view)
        view._clear_scene_focus()
        self.origin = view.mapToScene(pos)
        self.base: dict[NodeWidget, None] = dict(view._selected_nodes) if add else {}
        self.band: SelectionBandItem | None = SelectionBandItem()
        view.scene().addItem(self.band)

    @override
    def move(self, pos: QPoint, event) -> None:
        self.view._last_view_pos = pos
        self.update(pos)
        event.accept()

    def update(self, pos: QPoint) -> None:
        view = self.view
        if self.band is None:
            return
        rect = QRectF(self.origin, view.mapToScene(pos)).normalized()
        self.band.setRect(rect)
        if not rect.isNull():
            view.select_connection(None)  # a drag from a line selects nodes, not the line
        touched: list[NodeWidget] = []
        if rect.width() > 0 and rect.height() > 0:
            for item in view.scene().items(rect, Qt.ItemSelectionMode.IntersectsItemBoundingRect):
                node = item.node if isinstance(item, NodeItem) else None
                if node is None or node not in view.document:
                    continue
                # A group is selected by touching its title bar (a rectangle inside it selects its nodes).
                if isinstance(node, GroupNodeWidget) and not node.scene_header_rect().intersects(rect):
                    continue
                touched.append(node)
        view._set_selection({**self.base, **dict.fromkeys(touched)})

    @override
    def release(self, pos: QPoint, event) -> None:
        if event.button() != LEFT_BUTTON:
            return
        self.update(pos)
        self.view._reset_gesture()

    @override
    def cancel(self) -> None:
        band, self.band = self.band, None
        if band is not None and band.scene() is self.view.scene():
            self.view.scene().removeItem(band)

    @override
    def forget(self, node: NodeWidget) -> None:
        self.base.pop(node, None)


class ConnectGesture(Gesture):
    """Drags a connection from the label ``label_kind`` (``"parent"`` / ``"children"``) of ``node``."""

    kind = GestureKind.CONNECTING
    #: The compatible label under the mouse is looked for again once the mouse moved this far (px).
    HOVER_TEST_DISTANCE = 2

    def __init__(self, view: TreeView, node: NodeWidget, label_kind: str):
        super().__init__(view)
        view._clear_scene_focus()
        self.source: tuple[NodeWidget, str] | None = (node, label_kind)
        self.hover: tuple[NodeWidget, str] | None = None  # the compatible label under the mouse
        self.line: DragLineItem | None = DragLineItem()
        self._tested: tuple[QPoint, QTransform] | None = None  # (viewport position, transform) of the last look
        view.scene().addItem(self.line)

    def begin(self, pos: QPoint) -> None:
        """Show the source label as being placed and draw the line to ``pos`` (the view is active)."""
        source = self.source
        assert source is not None, "set by __init__"
        self.view._refresh_label_states(source[0])
        self.update(pos)

    def placing(self) -> set[tuple[NodeWidget, str]]:
        """The labels shown in the "placing" colour: the source and the compatible label hovered."""
        return {pair for pair in (self.source, self.hover) if pair is not None}

    @override
    def involves(self, node: NodeWidget) -> bool:
        return any(pair is not None and pair[0] is node for pair in (self.source, self.hover))

    @override
    def move(self, pos: QPoint, event) -> None:
        self.view._last_view_pos = pos
        self.update(pos)
        event.accept()

    def update(self, pos: QPoint) -> None:
        """Look for a compatible label under ``pos`` (unless the mouse barely moved) and redraw the line."""
        view = self.view
        transform = view.viewportTransform()
        tested = self._tested
        if tested is None or tested[1] != transform or (pos - tested[0]).manhattanLength() >= self.HOVER_TEST_DISTANCE:
            self._tested = (QPoint(pos), transform)
            target = view._compatible_target(view.hit_test(pos))
            if target != self.hover:
                previous, self.hover = self.hover, target
                if previous is not None:
                    view._refresh_label_states(previous[0])
                if target is not None:
                    view._refresh_label_states(target[0])
        self.update_line(view.mapToScene(pos))

    def invalidate_hover_test(self) -> None:
        """Look for a label at the next move again (nodes moved or resized)."""
        self._tested = None

    def update_line(self, cursor: QPointF) -> None:
        """Draw the drag line from the source label to scene position ``cursor``."""
        if self.line is None or self.source is None:
            return
        node, label_kind = self.source
        self.line.set_valid(self.hover is not None)
        self.line.set_endpoints(anchor_point(node, label_kind), label_kind, cursor)

    @override
    def release(self, pos: QPoint, event) -> None:
        if event.button() != LEFT_BUTTON:
            return
        view = self.view
        view._last_view_pos = pos
        hit = view.hit_test(pos)
        target = view._compatible_target(hit)
        if target is None and not view._locked and hit.kind in (hit.EMPTY, hit.CONNECTION):
            # Released over the graph: offer the add-node menu there, connected to the source.
            event.accept()
            view._offer_connected_node(pos, event.globalPosition().toPoint())
            return
        view._finish_connecting(target)

    def detach_line(self) -> DragLineItem | None:
        """Take the drag line out of the gesture (it then stays in the scene until the caller removes it)."""
        line, self.line = self.line, None
        return line

    @override
    def cancel(self) -> None:
        self.finish(None)

    def finish(self, target: tuple[NodeWidget, str] | None) -> None:
        """End the drag (the view already ended the gesture): remove the line and connect the source
        to ``target`` (a node and its label kind) when that is allowed."""
        view = self.view
        line, self.line = self.line, None
        if line is not None and line.scene() is view.scene():
            view.scene().removeItem(line)
        source, hover = self.source, self.hover
        self.source = self.hover = None
        for pair in (source, hover):
            if pair is not None and pair[0] in view.document:
                view._refresh_label_states(pair[0])
        if source is not None and target is not None and not view._locked:
            source_node, source_kind = source
            parent, child = (source_node, target[0]) if source_kind == CHILDREN else (target[0], source_node)
            if view.can_connect(parent, child):
                view.connect_nodes(parent, child)
