"""Graphics view that displays and edits the behavior tree.

Nodes are :class:`~behavior_tree_widget.nodes.NodeWidget` instances embedded in the
scene with :class:`QGraphicsProxyWidget`. All mouse gestures are interpreted by
:class:`TreeView` itself:

* press on a ParentConnection / ChildConnections label and drag -> create a connection
* press on an input widget of a node (line edit, spin box, ...) -> normal widget input
* press anywhere else on a node and drag -> move the node
* press on empty space or on a connection line and drag (or middle button
  anywhere) -> pan the view
* click on a connection line -> select it (Delete/Backspace removes it); any other
  click deselects it
* right click -> context menu (node: rename/type/memory/delete, empty: add node)
* Ctrl + mouse wheel -> zoom
"""

from __future__ import annotations

import enum
import math
import weakref
from typing import TYPE_CHECKING, Callable

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsProxyWidget,
    QGraphicsScene,
    QGraphicsView,
    QInputDialog,
    QMenu,
    QWidget,
)

from .nodes import (
    CHILDREN,
    COMPOSITE_TYPES,
    CONNECTION_COLORS,
    PARENT,
    STATUS_COLORS,
    CompositeNodeWidget,
    ConnectionState,
    NodeStatus,
    NodeWidget,
    RootNodeWidget,
    is_interactive_widget,
)

if TYPE_CHECKING:  # pragma: no cover
    from .widget import BehaviorTreeWidget

SCENE_EXTENT = 100_000.0
SCENE_MARGIN = 2_000.0  # room kept for a node's size inside the scene rect
LINE_WIDTH = 2.5
SELECTED_LINE_WIDTH = 3.5
LINE_HIT_WIDTH = 12.0
LABEL_HIT_MARGIN = 4
MIN_ZOOM = 0.2
MAX_ZOOM = 3.0

LINE_COLOR = QColor(CONNECTION_COLORS[ConnectionState.CONNECTED])  # blue
SELECTED_LINE_COLOR = QColor(CONNECTION_COLORS[ConnectionState.PLACING])  # green
VALID_DRAG_COLOR = QColor(CONNECTION_COLORS[ConnectionState.PLACING])  # green
INVALID_DRAG_COLOR = QColor(CONNECTION_COLORS[ConnectionState.DISCONNECTED])  # red


def clamp_to_scene(pos: QPointF) -> tuple[QPointF, bool]:
    """Keep a node position inside the scrollable scene area. Returns ``(pos, clamped)``.

    NaN coordinates become 0 and infinite ones the nearest edge.
    """
    limit = SCENE_EXTENT / 2 - SCENE_MARGIN
    clamped = False
    coordinates = []
    for value in (pos.x(), pos.y()):
        if math.isnan(value):
            value, clamped = 0.0, True
        bounded = min(max(value, -limit), limit)
        clamped = clamped or bounded != value
        coordinates.append(bounded)
    return QPointF(*coordinates), clamped


class _OutsideClickFilter(QObject):
    """Deselects the view's connection when the mouse is pressed anywhere else in its window."""

    def __init__(self, view: "TreeView"):
        super().__init__(view)
        self._view = view

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt API)
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        view = self._view
        if view._selected_connection is None or not isinstance(watched, QWidget):
            return False
        try:
            if QApplication.activePopupWidget() is not None or watched.window() is not view.window():
                return False
            viewport = view.viewport()
            if watched is viewport or viewport.isAncestorOf(watched) or watched is view:
                return False  # handled by the view itself
            view.select_connection(None)
        except RuntimeError:  # objects being destroyed
            pass
        return False


def connection_path(start: QPointF, end: QPointF) -> QPainterPath:
    """Vertical S-shaped cubic bezier from a parent anchor to a child anchor."""
    path = QPainterPath(start)
    bend = max(40.0, abs(end.y() - start.y()) * 0.5)
    path.cubicTo(start + QPointF(0.0, bend), end - QPointF(0.0, bend), end)
    return path


def anchor_point(node: NodeWidget, kind: str) -> QPointF:
    """Scene position where a connection attaches to ``node``'s connection label.

    ChildConnections lines leave from the bottom centre of the label, ParentConnection
    lines arrive at the top centre of the label.
    """
    label = node.connection_label(kind)
    item = node._item
    if label is None or item is None:
        raise ValueError(f"{node!r} has no {kind} connection")
    y = float(label.height()) if kind == CHILDREN else 0.0
    local = label.mapTo(node, QPointF(label.width() / 2.0, y))
    return item.mapToScene(local)


class NodeItem(QGraphicsProxyWidget):
    """Scene item embedding a :class:`NodeWidget`."""

    def __init__(self, node: NodeWidget, view: "TreeView"):
        super().__init__()
        self.node = node
        self.view = view
        self.setWidget(node)
        self.setZValue(1.0)
        node._item = self
        node.geometryHintChanged.connect(self._sync_size)
        self.geometryChanged.connect(self._on_geometry_changed)

    def _sync_size(self) -> None:
        self.resize(self.node.sizeHint().expandedTo(self.node.minimumSizeHint()))
        self.resize(self.node.size())
        self.view._on_node_geometry(self.node)

    def _on_geometry_changed(self) -> None:
        self.view._on_node_geometry(self.node)

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        super().paint(painter, option, widget)
        status = self.node.GetStatus()
        running = status is NodeStatus.RUNNING
        color = QColor(STATUS_COLORS[status]) if status is not NodeStatus.READY else QColor(120, 120, 120)
        width = 3.0 if running else 1.5
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(color, width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = width / 2.0
        painter.drawRoundedRect(self.boundingRect().adjusted(inset, inset, -inset, -inset), 4.0, 4.0)
        painter.restore()


class ConnectionItem(QGraphicsPathItem):
    """A parent -> child connection line (blue, green when selected) with an order badge."""

    BADGE_RADIUS = 8.0

    def __init__(self, parent_node: NodeWidget, child_node: NodeWidget):
        super().__init__()
        self.parent_node = parent_node
        self.child_node = child_node
        self._selected = False
        self._order = 0
        self.setZValue(-1.0)
        self._apply_pen()
        self.update_path()

    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self._apply_pen()
            self.update()

    def color(self) -> QColor:
        return SELECTED_LINE_COLOR if self._selected else LINE_COLOR

    def order(self) -> int:
        return self._order

    def set_order(self, order: int) -> None:
        if order != self._order:
            self.prepareGeometryChange()
            self._order = order
            self.update()

    def _apply_pen(self) -> None:
        pen = QPen(self.color(), SELECTED_LINE_WIDTH if self._selected else LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self.setPen(pen)

    def update_path(self) -> None:
        self.setPath(connection_path(anchor_point(self.parent_node, CHILDREN), anchor_point(self.child_node, PARENT)))

    def _badge_center(self) -> QPointF:
        end = self.path().pointAtPercent(1.0)
        return QPointF(end.x(), end.y() - self.BADGE_RADIUS - 6.0)

    def _badge_rect(self) -> QRectF:
        r = self.BADGE_RADIUS
        c = self._badge_center()
        return QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(LINE_HIT_WIDTH)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        return stroker.createStroke(self.path())

    def boundingRect(self) -> QRectF:
        rect = self.path().boundingRect().adjusted(-LINE_HIT_WIDTH, -LINE_HIT_WIDTH, LINE_HIT_WIDTH, LINE_HIT_WIDTH)
        if self._order:
            rect = rect.united(self._badge_rect().adjusted(-2, -2, 2, 2))
        return rect

    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(self.pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())
        if self._order:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.color()))
            painter.drawEllipse(self._badge_rect())
            font = QFont(painter.font())
            font.setPointSizeF(7.5)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(QColor("white")))
            painter.drawText(self._badge_rect(), Qt.AlignmentFlag.AlignCenter, str(self._order))
        painter.restore()


class DragLineItem(QGraphicsPathItem):
    """Temporary line drawn while a connection is being dragged (red / green)."""

    def __init__(self):
        super().__init__()
        self.setZValue(1e9)
        self._valid = False
        self.set_valid(False)

    def is_valid(self) -> bool:
        return self._valid

    def color(self) -> QColor:
        return QColor(VALID_DRAG_COLOR if self._valid else INVALID_DRAG_COLOR)

    def set_valid(self, valid: bool) -> None:
        self._valid = valid
        pen = QPen(self.color(), LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self.setPen(pen)

    def set_endpoints(self, source: QPointF, source_kind: str, cursor: QPointF) -> None:
        if source_kind == CHILDREN:
            self.setPath(connection_path(source, cursor))
        else:
            self.setPath(connection_path(cursor, source))


class _Gesture(enum.Enum):
    IDLE = 0
    PANNING = 1
    MOVING_NODE = 2
    CONNECTING = 3
    PASSTHROUGH = 4


class Hit:
    """Result of :meth:`TreeView.hit_test`."""

    EMPTY = "empty"
    NODE = "node"
    LABEL = "label"
    INTERACTIVE = "interactive"
    CONNECTION = "connection"

    def __init__(self, kind: str, node: NodeWidget | None = None, label_kind: str | None = None,
                 connection: ConnectionItem | None = None, widget: QWidget | None = None):
        self.kind = kind
        self.node = node
        self.label_kind = label_kind
        self.connection = connection
        self.widget = widget

    def __repr__(self) -> str:
        return f"Hit({self.kind!r}, node={self.node!r}, label={self.label_kind!r})"


class TreeView(QGraphicsView):
    """Editable view of the behavior tree graph."""

    modified = Signal()
    nodeAdded = Signal(object)
    nodeRemoved = Signal(object)
    connectionAdded = Signal(object, object)  # parent, child
    connectionRemoved = Signal(object, object)  # parent, child
    connectionSelectionChanged = Signal(object)  # ConnectionItem or None
    shown = Signal()  # the view became visible
    navigated = Signal()  # the user panned or zoomed

    HINT_TEXT = "Click New to create a behavior tree or Load to open one."

    def __init__(self, owner: "BehaviorTreeWidget | None" = None, parent: QWidget | None = None):
        super().__init__(parent)
        # Weak reference: the view is a child of the owner; a strong one would form a
        # reference cycle that keeps a closed top-level BehaviorTreeWidget alive.
        self._owner_ref = weakref.ref(owner) if owner is not None else None
        scene = QGraphicsScene(self)
        scene.setSceneRect(-SCENE_EXTENT / 2, -SCENE_EXTENT / 2, SCENE_EXTENT, SCENE_EXTENT)
        self.setScene(scene)
        self.setObjectName("TreeView")
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self._nodes: list[NodeWidget] = []
        self._connections: dict[NodeWidget, ConnectionItem] = {}  # keyed by child node
        self._selected_connection: ConnectionItem | None = None
        self._locked = False
        self._z_counter = 1.0

        self._gesture = _Gesture.IDLE
        self._last_view_pos = QPoint()
        self._press_scene_pos = QPointF()
        self._move_node: NodeWidget | None = None
        self._move_start_pos = QPointF()
        self._moved = False
        self._connect_source: tuple[NodeWidget, str] | None = None
        self._hover_target: tuple[NodeWidget, str] | None = None
        self._drag_line: DragLineItem | None = None

        # Installed on the application only while a connection is selected (see
        # select_connection), so it costs nothing the rest of the time.
        self._outside_filter = _OutsideClickFilter(self)
        self._outside_filter_installed = False

    @property
    def _owner(self) -> "BehaviorTreeWidget | None":
        return self._owner_ref() if self._owner_ref is not None else None

    def _set_outside_filter(self, install: bool) -> None:
        app = QApplication.instance()
        if app is None or install == self._outside_filter_installed:
            return
        if install:
            app.installEventFilter(self._outside_filter)
        else:
            app.removeEventFilter(self._outside_filter)
        self._outside_filter_installed = install

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._set_outside_filter(False)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._set_outside_filter(self._selected_connection is not None)
        self.shown.emit()

    # ================================================================== model
    def nodes(self) -> list[NodeWidget]:
        return list(self._nodes)

    def root(self) -> RootNodeWidget | None:
        for node in self._nodes:
            if isinstance(node, RootNodeWidget):
                return node
        return None

    def connections(self) -> list[ConnectionItem]:
        return list(self._connections.values())

    def connection_item(self, child: NodeWidget) -> ConnectionItem | None:
        return self._connections.get(child)

    def selected_connection(self) -> ConnectionItem | None:
        return self._selected_connection

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        """Lock the tree structure (connections, adding and deleting nodes).

        While locked, the order badges keep showing the order the running tree was built
        with; moving nodes re-numbers them when execution ends.
        """
        was_locked = self._locked
        self._locked = bool(locked)
        if self._locked and self._gesture is _Gesture.CONNECTING:
            self._finish_connecting(None)
        if was_locked and not self._locked:
            for node in self._nodes:
                if node._children:
                    self._refresh_order(node)

    def _check_unlocked(self) -> None:
        if self._locked:
            raise RuntimeError("the tree structure cannot be changed while the tree is executing; stop it first")

    def add_node(self, node: NodeWidget, pos: QPointF | tuple[float, float] = QPointF(0, 0)) -> NodeItem:
        """Add ``node`` with its top-left corner at scene position ``pos``."""
        self._check_unlocked()
        if node._item is not None:
            raise ValueError(f"{node!r} is already part of a tree view")
        if isinstance(node, RootNodeWidget) and self.root() is not None:
            raise ValueError("a tree can only have one root node")
        item = NodeItem(node, self)
        self.scene().addItem(item)
        item.setPos(QPointF(*pos) if isinstance(pos, tuple) else QPointF(pos))
        self._z_counter += 1.0
        item.setZValue(self._z_counter)
        node._canvas = self
        node._tree = self._owner
        self._nodes.append(node)
        self._refresh_label_states(node)
        self.nodeAdded.emit(node)
        self.modified.emit()
        return item

    def remove_node(self, node: NodeWidget) -> None:
        """Delete ``node`` and all its connections. The root cannot be removed."""
        if isinstance(node, RootNodeWidget):
            raise ValueError("the root node cannot be deleted")
        self._check_unlocked()
        self._remove_node(node)
        self.modified.emit()

    def _remove_node(self, node: NodeWidget) -> None:
        if node not in self._nodes:
            raise ValueError(f"{node!r} is not part of this view")
        if self._move_node is node:
            self._reset_gesture()
        if (self._connect_source and self._connect_source[0] is node) or (
            self._hover_target and self._hover_target[0] is node
        ):
            self._finish_connecting(None)
        self._disconnect(node)
        for child in list(node._children):
            self._disconnect(child)
        self._nodes.remove(node)
        item = node._item
        node._canvas = None
        node._item = None
        if item is not None:
            item.geometryChanged.disconnect(item._on_geometry_changed)
            self.scene().removeItem(item)
            item.deleteLater()  # also deletes the embedded node widget
        self.nodeRemoved.emit(node)

    def clear(self) -> None:
        """Remove every node, including the root."""
        self._reset_gesture()
        self.select_connection(None)
        for node in list(self._nodes):
            self._remove_node(node)

    def can_connect(self, parent: NodeWidget, child: NodeWidget) -> bool:
        """True if ``child`` may be connected below ``parent`` (no self links, no cycles)."""
        if parent is child or parent not in self._nodes or child not in self._nodes:
            return False
        if not parent.HasChildConnections() or not child.HasParentConnection():
            return False
        ancestor = parent
        while ancestor is not None:  # child must not be an ancestor of parent
            if ancestor is child:
                return False
            ancestor = ancestor._parent
        return True

    def connect_nodes(self, parent: NodeWidget, child: NodeWidget) -> None:
        """Connect ``child`` below ``parent``, replacing any previous parent of ``child``."""
        self._check_unlocked()
        if not self.can_connect(parent, child):
            raise ValueError(f"cannot connect {child!r} below {parent!r}")
        if child._parent is parent:
            return
        if child._parent is not None:
            self._disconnect(child)
        with parent._lock:
            parent._children.append(child)
        with child._lock:
            child._parent = parent
        item = ConnectionItem(parent, child)
        self.scene().addItem(item)
        self._connections[child] = item
        self._refresh_order(parent)
        self._refresh_label_states(parent)
        self._refresh_label_states(child)
        self.connectionAdded.emit(parent, child)
        self.modified.emit()

    def disconnect_node(self, child: NodeWidget) -> None:
        """Remove the connection between ``child`` and its parent (if any)."""
        self._check_unlocked()
        self._disconnect(child)

    def _disconnect(self, child: NodeWidget) -> None:
        parent = child._parent
        if parent is None:
            return
        item = self._connections.pop(child, None)
        if item is not None:
            if item is self._selected_connection:
                self.select_connection(None)
            self.scene().removeItem(item)
        with parent._lock:
            if child in parent._children:
                parent._children.remove(child)
        with child._lock:
            child._parent = None
        self._refresh_order(parent)
        self._refresh_label_states(parent)
        self._refresh_label_states(child)
        self.connectionRemoved.emit(parent, child)
        self.modified.emit()

    def select_connection(self, item: ConnectionItem | None) -> None:
        if item is self._selected_connection:
            return
        if self._selected_connection is not None:
            self._selected_connection.set_selected(False)
        self._selected_connection = item
        if item is not None:
            item.set_selected(True)
        self._set_outside_filter(item is not None)
        self.connectionSelectionChanged.emit(item)

    def _refresh_order(self, parent: NodeWidget, force: bool = False) -> None:
        if self._locked and not force:
            return  # keep the order of the running tree (see set_locked)
        for index, child in enumerate(parent.GetChildren(), start=1):
            item = self._connections.get(child)
            if item is not None:
                item.set_order(index)

    def _refresh_label_states(self, node: NodeWidget) -> None:
        placing = {pair for pair in (self._connect_source, self._hover_target) if pair is not None}
        if node.HasParentConnection():
            if (node, PARENT) in placing:
                state = ConnectionState.PLACING
            else:
                state = ConnectionState.CONNECTED if node._parent is not None else ConnectionState.DISCONNECTED
            node.set_connection_state(PARENT, state)
        if node.HasChildConnections():
            if (node, CHILDREN) in placing:
                state = ConnectionState.PLACING
            else:
                state = ConnectionState.CONNECTED if node._children else ConnectionState.DISCONNECTED
            node.set_connection_state(CHILDREN, state)

    def _on_node_geometry(self, node: NodeWidget) -> None:
        if node not in self._nodes:
            return
        item = self._connections.get(node)
        if item is not None:
            item.update_path()
        for child in list(node._children):
            child_item = self._connections.get(child)
            if child_item is not None:
                child_item.update_path()
        if node._parent is not None:
            self._refresh_order(node._parent)
        if self._gesture is _Gesture.CONNECTING and self._connect_source and self._connect_source[0] is node:
            self._update_drag_line(self.mapToScene(self._last_view_pos))

    # ================================================================== view helpers
    def next_z(self) -> float:
        self._z_counter += 1.0
        return self._z_counter

    def center_on_node(self, node: NodeWidget) -> None:
        if node._item is not None:
            self.centerOn(node._item.sceneBoundingRect().center())

    def zoom(self) -> float:
        return self.transform().m11()

    def set_zoom(self, zoom: float) -> None:
        """Set the zoom factor (clamped to MIN_ZOOM..MAX_ZOOM) keeping the view centre."""
        zoom = min(max(float(zoom), MIN_ZOOM), MAX_ZOOM)
        center = self.view_center()
        self.resetTransform()
        self.scale(zoom, zoom)
        self.centerOn(center)

    def view_center(self) -> QPointF:
        """Scene point at the exact centre of the viewport (also for a zero-size viewport)."""
        inverse, _ = self.viewportTransform().inverted()
        viewport = self.viewport()
        return inverse.map(QPointF(viewport.width() / 2.0, viewport.height() / 2.0))

    def hit_test(self, view_pos: QPoint) -> Hit:
        """Classify what lies under viewport position ``view_pos``."""
        scene_pos = self.mapToScene(view_pos)
        for item in self.items(view_pos):
            if isinstance(item, NodeItem):
                node = item.node
                local = item.mapFromScene(scene_pos)
                label_kind = self._label_at(node, local)
                if label_kind is not None:
                    return Hit(Hit.LABEL, node, label_kind=label_kind)
                widget = node.childAt(local.toPoint())
                if widget is not None and is_interactive_widget(widget, node):
                    return Hit(Hit.INTERACTIVE, node, widget=widget)
                return Hit(Hit.NODE, node, widget=widget)
            if isinstance(item, ConnectionItem):
                return Hit(Hit.CONNECTION, connection=item)
            if isinstance(item, DragLineItem):
                continue
            # Any other item: a widget created by a proxied widget (e.g. a combo box popup).
            return Hit(Hit.INTERACTIVE)
        return Hit(Hit.EMPTY)

    @staticmethod
    def _label_at(node: NodeWidget, local: QPointF) -> str | None:
        point = local.toPoint()
        for kind in (PARENT, CHILDREN):
            label = node.connection_label(kind)
            if label is None or not label.isVisibleTo(node):
                continue
            top_left = label.mapTo(node, QPoint(0, 0))
            rect = QRect(top_left, label.size()).adjusted(
                -LABEL_HIT_MARGIN, -LABEL_HIT_MARGIN, LABEL_HIT_MARGIN, LABEL_HIT_MARGIN
            )
            if rect.contains(point):
                return kind
        return None

    def _compatible_target(self, hit: Hit) -> tuple[NodeWidget, str] | None:
        if self._connect_source is None or hit.kind != Hit.LABEL:
            return None
        source_node, source_kind = self._connect_source
        if hit.node is source_node or hit.label_kind == source_kind:
            return None
        parent, child = (source_node, hit.node) if source_kind == CHILDREN else (hit.node, source_node)
        if not self.can_connect(parent, child):
            return None
        return (hit.node, hit.label_kind)

    def _clear_scene_focus(self) -> None:
        scene = self.scene()
        if scene.focusItem() is not None:
            scene.setFocusItem(None)
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    # ================================================================== mouse gestures
    def _gesture_buttons_held(self, buttons) -> bool:
        """True if the mouse button driving the current gesture is still pressed."""
        if self._gesture is _Gesture.PANNING:
            return bool(buttons & (Qt.MouseButton.LeftButton | Qt.MouseButton.MiddleButton))
        if self._gesture in (_Gesture.MOVING_NODE, _Gesture.CONNECTING):
            return bool(buttons & Qt.MouseButton.LeftButton)
        return True

    def _abandon_gesture(self) -> None:
        """End a gesture whose mouse release was lost (e.g. a modal dialog took the mouse)."""
        moved = self._gesture is _Gesture.MOVING_NODE and self._moved
        passthrough = self._gesture is _Gesture.PASSTHROUGH
        self._reset_gesture()
        if passthrough:
            # The node that received the press still holds the scene's implicit mouse grab.
            grabber = self.scene().mouseGrabberItem()
            if isinstance(grabber, NodeItem):
                grabber.ungrabMouse()
        if moved:
            self.modified.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._gesture is not _Gesture.IDLE:
            # A press while a gesture is active means its release was lost (or a second
            # button was pressed during widget input): end the stale gesture first.
            if self._gesture is _Gesture.PASSTHROUGH or not self._gesture_buttons_held(
                event.buttons() & ~event.button()
            ):
                self._abandon_gesture()
            else:
                event.accept()
                return
        pos = event.position().toPoint()
        self._last_view_pos = pos
        grabber = self.scene().mouseGrabberItem()
        if grabber is not None and (not grabber.isVisible() or isinstance(grabber, NodeItem)):
            # A grab left behind (e.g. by a combo box popup closed with the keyboard):
            # a real popup is a separate, visible item.
            grabber.ungrabMouse()
        if self.scene().mouseGrabberItem() is not None:
            # A proxied popup (e.g. an open combo box list) grabs the mouse; let the scene
            # deliver the click so the popup can close or select an item.
            self.select_connection(None)
            self._gesture = _Gesture.PASSTHROUGH
            super().mousePressEvent(event)
            return

        hit = self.hit_test(pos)
        if hit.kind != Hit.CONNECTION or event.button() != Qt.MouseButton.LeftButton:
            self.select_connection(None)
        if event.button() == Qt.MouseButton.MiddleButton:
            self._begin_pan(pos)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        if hit.kind == Hit.CONNECTION:
            # Select the line; dragging from it pans the view (it is outside of any node).
            self.select_connection(hit.connection)
            self._begin_pan(pos)
            event.accept()
        elif hit.kind == Hit.LABEL:
            if not self._locked:
                self._begin_connecting(hit.node, hit.label_kind, pos)
            # While executing, connection labels do nothing (they never move the node).
            event.accept()
        elif hit.kind == Hit.INTERACTIVE:
            if hit.node is not None and hit.node._item is not None:
                hit.node._item.setZValue(self.next_z())
            self._gesture = _Gesture.PASSTHROUGH
            super().mousePressEvent(event)
        elif hit.kind == Hit.NODE:
            self._begin_move(hit.node, pos)
            event.accept()
        else:
            self._begin_pan(pos)
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._gesture not in (_Gesture.IDLE, _Gesture.PASSTHROUGH) and not self._gesture_buttons_held(
            event.buttons()
        ):
            self._abandon_gesture()
        gesture = self._gesture
        if gesture is _Gesture.PANNING:
            delta = pos - self._last_view_pos
            if not delta.isNull():
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
                self.navigated.emit()
            self._last_view_pos = pos
            event.accept()
        elif gesture is _Gesture.MOVING_NODE:
            self._last_view_pos = pos
            if self._move_node is not None and self._move_node._item is not None:
                offset = self.mapToScene(pos) - self._press_scene_pos
                if not self._moved and (abs(offset.x()) > 0 or abs(offset.y()) > 0):
                    self._moved = True
                self._move_node._item.setPos(self._move_start_pos + offset)
            event.accept()
        elif gesture is _Gesture.CONNECTING:
            self._last_view_pos = pos
            self._update_connecting(pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        gesture = self._gesture
        pos = event.position().toPoint()
        if gesture is _Gesture.PASSTHROUGH:
            super().mouseReleaseEvent(event)
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                self._gesture = _Gesture.IDLE
            return
        if gesture is _Gesture.IDLE:
            super().mouseReleaseEvent(event)
            return
        if gesture is _Gesture.PANNING and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            self._reset_gesture()
        elif gesture is _Gesture.MOVING_NODE and event.button() == Qt.MouseButton.LeftButton:
            moved = self._moved
            self._reset_gesture()
            if moved:
                self.modified.emit()
        elif gesture is _Gesture.CONNECTING and event.button() == Qt.MouseButton.LeftButton:
            self._last_view_pos = pos
            target = self._compatible_target(self.hit_test(pos))
            self._finish_connecting(target)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self.hit_test(event.position().toPoint())
            if hit.kind != Hit.INTERACTIVE:
                self.mousePressEvent(event)
                return
        super().mouseDoubleClickEvent(event)

    def _begin_pan(self, pos: QPoint) -> None:
        self._clear_scene_focus()
        self._gesture = _Gesture.PANNING
        self._last_view_pos = pos
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)

    def _begin_move(self, node: NodeWidget, pos: QPoint) -> None:
        self._clear_scene_focus()
        self._gesture = _Gesture.MOVING_NODE
        self._move_node = node
        self._moved = False
        self._press_scene_pos = self.mapToScene(pos)
        self._move_start_pos = node._item.pos()
        node._item.setZValue(self.next_z())
        self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)

    def _begin_connecting(self, node: NodeWidget, kind: str, pos: QPoint) -> None:
        self._clear_scene_focus()
        self._gesture = _Gesture.CONNECTING
        self._connect_source = (node, kind)
        self._hover_target = None
        self._drag_line = DragLineItem()
        self.scene().addItem(self._drag_line)
        self._refresh_label_states(node)
        self._update_connecting(pos)

    def _update_connecting(self, pos: QPoint) -> None:
        target = self._compatible_target(self.hit_test(pos))
        if target != self._hover_target:
            previous = self._hover_target
            self._hover_target = target
            if previous is not None:
                self._refresh_label_states(previous[0])
            if target is not None:
                self._refresh_label_states(target[0])
        self._update_drag_line(self.mapToScene(pos))

    def _update_drag_line(self, cursor: QPointF) -> None:
        if self._drag_line is None or self._connect_source is None:
            return
        node, kind = self._connect_source
        self._drag_line.set_valid(self._hover_target is not None)
        self._drag_line.set_endpoints(anchor_point(node, kind), kind, cursor)

    def _finish_connecting(self, target: tuple[NodeWidget, str] | None) -> None:
        source = self._connect_source
        hover = self._hover_target
        if self._drag_line is not None:
            self.scene().removeItem(self._drag_line)
            self._drag_line = None
        self._connect_source = None
        self._hover_target = None
        self._gesture = _Gesture.IDLE
        for pair in (source, hover):
            if pair is not None and pair[0] in self._nodes:
                self._refresh_label_states(pair[0])
        if source is not None and target is not None and not self._locked:
            source_node, source_kind = source
            parent, child = (source_node, target[0]) if source_kind == CHILDREN else (target[0], source_node)
            if self.can_connect(parent, child):
                self.connect_nodes(parent, child)

    def _reset_gesture(self) -> None:
        if self._gesture is _Gesture.CONNECTING:
            self._finish_connecting(None)
        self._gesture = _Gesture.IDLE
        self._move_node = None
        self._moved = False
        self.viewport().unsetCursor()

    def is_connecting(self) -> bool:
        return self._gesture is _Gesture.CONNECTING

    def drag_line(self) -> DragLineItem | None:
        return self._drag_line

    # ================================================================== keyboard / wheel
    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected_connection is not None:
            if not self._locked:
                self.disconnect_node(self._selected_connection.child_node)
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._gesture is _Gesture.CONNECTING:
                self._finish_connecting(None)
                event.accept()
                return
            if self._selected_connection is not None:
                self.select_connection(None)
                event.accept()
                return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        reason = event.reason()
        if reason == Qt.FocusReason.PopupFocusReason:
            return  # a context menu opened; keep the selection for its actions
        if reason == Qt.FocusReason.ActiveWindowFocusReason:
            # The window lost activation (e.g. a modal dialog): a release may never come.
            if self._gesture is not _Gesture.PASSTHROUGH:
                self._abandon_gesture()
            return
        # Focus moved elsewhere (clicked another widget or tabbed away): "clicking
        # anywhere else" deselects a selected connection.
        self.select_connection(None)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                factor = math.pow(1.15, steps)
                zoom = self.zoom()
                factor = min(max(zoom * factor, MIN_ZOOM), MAX_ZOOM) / zoom
                self.scale(factor, factor)
                self.navigated.emit()
            event.accept()
            return
        super().wheelEvent(event)
        self.navigated.emit()

    # ================================================================== context menus
    def contextMenuEvent(self, event) -> None:  # noqa: N802
        if self._gesture is not _Gesture.IDLE:
            event.accept()
            return
        hit = self.hit_test(event.pos())
        if hit.kind != Hit.CONNECTION:
            self.select_connection(None)
        if hit.kind == Hit.INTERACTIVE:
            # Embedded widgets only receive context menu events while their proxy has focus.
            if hit.node is not None and hit.node._item is not None:
                hit.node._item.setFocus(Qt.FocusReason.MouseFocusReason)
            if hit.widget is not None:
                hit.widget.setFocus(Qt.FocusReason.MouseFocusReason)
            super().contextMenuEvent(event)
            return
        if hit.kind in (Hit.NODE, Hit.LABEL):
            menu = self.build_node_menu(hit.node)
        elif hit.kind == Hit.CONNECTION:
            self.select_connection(hit.connection)
            menu = self.build_connection_menu(hit.connection)
        else:
            menu = self.build_add_menu(self.mapToScene(event.pos()))
        self._exec_menu(menu, event.globalPos())
        event.accept()

    def _exec_menu(self, menu: QMenu, global_pos: QPoint) -> None:
        """Show ``menu`` modally (separate method so tests can replace it)."""
        menu.exec(global_pos)
        menu.deleteLater()

    def build_add_menu(self, scene_pos: QPointF) -> QMenu:
        """Menu listing every available node type; choosing one adds it at ``scene_pos``."""
        menu = QMenu(self)
        menu.setObjectName("AddNodeMenu")
        title = menu.addAction("Add Node")
        title.setEnabled(False)
        entries = self._owner._node_menu_entries() if self._owner is not None else []
        previous_group = None
        for group, type_name, label in entries:
            if previous_group is not None and group != previous_group:
                menu.addSeparator()
            previous_group = group
            action = menu.addAction(label)
            action.setObjectName(f"Add_{type_name}")
            action.setData(type_name)
            action.setEnabled(not self._locked)
            action.triggered.connect(lambda _checked=False, t=type_name, p=QPointF(scene_pos): self._add_node_from_menu(t, p))
        if self._locked:
            menu.addSeparator()
            note = menu.addAction("Stop execution to add nodes")
            note.setEnabled(False)
        return menu

    def _add_node_from_menu(self, type_name: str, scene_pos: QPointF) -> None:
        if self._locked or self._owner is None:
            return
        node = self._owner._create_node(type_name)
        self.add_node(node, scene_pos)

    def build_node_menu(self, node: NodeWidget) -> QMenu:
        """Context menu of ``node``: rename, composite options and delete."""
        menu = QMenu(self)
        menu.setObjectName("NodeMenu")
        rename = menu.addAction("Rename...")
        rename.setObjectName("Rename")
        rename.triggered.connect(lambda _checked=False, n=node: self._rename_node(n))
        if isinstance(node, CompositeNodeWidget):
            menu.addSeparator()
            group = QActionGroup(menu)
            group.setExclusive(True)
            for composite_type in COMPOSITE_TYPES:
                action = menu.addAction(composite_type)
                action.setObjectName(f"Type_{composite_type}")
                action.setCheckable(True)
                action.setChecked(node.GetCompositeType() == composite_type)
                action.setEnabled(not self._locked)
                group.addAction(action)
                action.triggered.connect(
                    lambda _checked=False, n=node, t=composite_type: self._set_composite_type(n, t)
                )
            memory = menu.addAction("Memory")
            memory.setObjectName("Memory")
            memory.setCheckable(True)
            memory.setChecked(node.GetMemory())
            memory.setEnabled(not self._locked)
            memory.setToolTip("Resume from the running child on the next tick (py_trees memory).")
            memory.toggled.connect(lambda checked, n=node: self._set_memory(n, checked))
        menu.addSeparator()
        delete = menu.addAction("Delete")
        delete.setObjectName("Delete")
        if isinstance(node, RootNodeWidget):
            delete.setEnabled(False)
            delete.setText("Delete (the root node cannot be deleted)")
        elif self._locked:
            delete.setEnabled(False)
            delete.setText("Delete (stop execution first)")
        delete.triggered.connect(lambda _checked=False, n=node: self._delete_node_from_menu(n))
        return menu

    def build_connection_menu(self, item: ConnectionItem) -> QMenu:
        menu = QMenu(self)
        menu.setObjectName("ConnectionMenu")
        delete = menu.addAction("Delete Connection")
        delete.setObjectName("DeleteConnection")
        delete.setEnabled(not self._locked)
        delete.triggered.connect(lambda _checked=False, c=item: self._delete_connection(c))
        return menu

    def _delete_connection(self, item: ConnectionItem) -> None:
        if not self._locked and self._connections.get(item.child_node) is item:
            self.disconnect_node(item.child_node)

    def _delete_node_from_menu(self, node: NodeWidget) -> None:
        if self._locked or isinstance(node, RootNodeWidget) or not self._is_current(node):
            return
        self.remove_node(node)

    def _is_current(self, node: NodeWidget) -> bool:
        return node in self._nodes and shiboken6.isValid(node)

    def _rename_node(self, node: NodeWidget) -> None:
        if not self._is_current(node):
            return
        text, ok = QInputDialog.getText(self, "Rename Node", "Title:", text=node.GetTitle())
        if ok and text.strip() and self._is_current(node) and text.strip() != node.GetTitle():
            node._apply_title(text.strip(), runtime=False)  # a user edit, also while executing
            self.modified.emit()

    def _set_composite_type(self, node: CompositeNodeWidget, composite_type: str) -> None:
        if not self._locked and self._is_current(node) and node.GetCompositeType() != composite_type:
            node.SetCompositeType(composite_type)
            self.modified.emit()

    def _set_memory(self, node: CompositeNodeWidget, memory: bool) -> None:
        if not self._locked and self._is_current(node) and node.GetMemory() != memory:
            node.SetMemory(memory)
            self.modified.emit()

    # ================================================================== painting
    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            if not self.isEnabled():
                self._reset_gesture()
            self.viewport().update()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        palette = self.palette()
        base = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Base)
        painter.fillRect(rect, base)
        dark = base.lightness() < 128
        minor = QColor(255, 255, 255, 18) if dark else QColor(0, 0, 0, 16)
        major = QColor(255, 255, 255, 36) if dark else QColor(0, 0, 0, 34)
        step = 20.0
        if self.zoom() * step < 6:
            return
        left = math.floor(rect.left() / step) * step
        top = math.floor(rect.top() / step) * step
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        x = left
        while x < rect.right():
            painter.setPen(QPen(major if round(x / step) % 5 == 0 else minor, 0))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += step
        y = top
        while y < rect.bottom():
            painter.setPen(QPen(major if round(y / step) % 5 == 0 else minor, 0))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += step
        painter.restore()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        if self.isEnabled():
            return
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        area = self.viewport().rect()
        painter.fillRect(area, QColor(128, 128, 128, 60))
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF(), 9.0) + 2.0)
        painter.setFont(font)
        flags = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
        bounds = area.adjusted(24, 24, -24, -24)
        text_rect = painter.boundingRect(bounds, int(flags), self.HINT_TEXT)
        box = QRectF(text_rect).adjusted(-14, -10, 14, 10)
        box.moveCenter(QPointF(area.center().x(), area.top() + area.height() * 0.66))
        palette = self.palette()
        painter.setPen(QPen(palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Mid), 1))
        painter.setBrush(palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText))
        painter.drawText(box, flags, self.HINT_TEXT)
        painter.restore()


def iter_subtree(node: NodeWidget) -> "list[NodeWidget]":
    """``node`` and all of its descendants (depth first, children in execution order)."""
    result = [node]
    for child in node.GetChildren():
        result.extend(iter_subtree(child))
    return result


NodeFactory = Callable[[str], NodeWidget]
