"""Graphics view that displays and edits the behavior tree.

Nodes are :class:`~behavior_tree_widget.nodes.NodeWidget` instances embedded in the
scene with :class:`QGraphicsProxyWidget`. All mouse gestures are interpreted by
:class:`TreeView` itself:

* press on a ParentConnection / ChildConnections label and drag -> create a connection;
  releasing over empty space opens the add-node menu and connects the chosen node
* press on an input widget of a node (line edit, spin box, ...) -> normal widget input
* press anywhere else on a node -> select it; drag -> move it (with the other
  selected nodes when it is selected); Ctrl + click toggles, Shift + click adds it
* Notes nodes: drag an edge or a corner -> resize the note; click the text (without
  dragging) -> edit it (Escape or a click elsewhere ends editing)
* Group nodes: drag the title bar -> move the group with every node lying completely
  inside it; drag an edge or a corner -> resize it; clicks inside a group go to what lies
  below it (nodes, or the canvas itself)
* press on empty space or on a connection line and drag -> select the nodes touched
  by a rectangle (Shift + drag adds them to the selection); a click there deselects
  the nodes
* Ctrl + drag (except on connection labels and input widgets) or middle button drag
  anywhere -> pan the view
* click on a connection line -> select it; any other click deselects it
* Delete/Backspace -> delete the selected nodes and connection (the root only loses
  its connections)
* Ctrl+C / Ctrl+V -> copy / paste the selected nodes; Ctrl+A -> select every node
* right click -> context menu (node: rename/select children/type/memory/delete,
  note: edit text/delete, group: rename/select contents/border color/promote to
  sub-tree/delete, empty: add node)
* Ctrl + mouse wheel -> zoom

The view is split over a few modules: :mod:`.geometry` (scene area, connection curves,
placement), :mod:`.items` (the graphics items), :mod:`.gestures` (one class per mouse
gesture) and :mod:`.menus` (the context menus). This module re-exports their public names.
"""

from __future__ import annotations

import logging
import math
import weakref
from collections.abc import Iterable
from typing import TYPE_CHECKING

import shiboken6
from PySide6.QtCore import QEvent, QLineF, QObject, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QColorDialog, QGraphicsScene, QGraphicsView, QInputDialog, QMenu, QWidget

from . import menus
from .document import TreeDocument
from .geometry import (
    PASTE_GAP,
    SCENE_EXTENT,
    SCENE_MARGIN,
    anchor_point,
    clamp_to_scene,
    connection_path,
    iter_subtree,
    nearest_free_offset,
)
from .gestures import (
    BandGesture,
    ConnectGesture,
    Gesture,
    GestureKind,
    MoveGesture,
    PanGesture,
    PassThroughGesture,
    ResizeGesture,
)
from .groups import GroupNodeWidget
from .items import (
    INVALID_DRAG_COLOR,
    LINE_COLOR,
    LINE_HIT_WIDTH,
    LINE_WIDTH,
    SELECTED_LINE_COLOR,
    SELECTED_LINE_WIDTH,
    SELECTION_COLOR,
    VALID_DRAG_COLOR,
    ConnectionItem,
    DragLineItem,
    NodeItem,
    SelectionBandItem,
)
from .nodes import (
    CHILDREN,
    PARENT,
    CompositeNodeWidget,
    ConnectionState,
    NodeWidget,
    RootNodeWidget,
    is_interactive_widget,
)
from .notes import BoxNodeWidget, NotesNodeWidget, ResizeHandle
from .subtree import SubTreeNodeWidget

if TYPE_CHECKING:  # pragma: no cover
    from .widget import BehaviorTreeWidget

log = logging.getLogger("behavior_tree_widget")

__all__ = [
    "GROUP_Z_BASE",
    "INVALID_DRAG_COLOR",
    "LABEL_HIT_MARGIN",
    "LINE_COLOR",
    "LINE_HIT_WIDTH",
    "LINE_WIDTH",
    "MAX_ZOOM",
    "MIN_ZOOM",
    "NOTE_Z_BASE",
    "PASTE_GAP",
    "SCENE_EXTENT",
    "SCENE_MARGIN",
    "SELECTED_LINE_COLOR",
    "SELECTED_LINE_WIDTH",
    "SELECTION_COLOR",
    "VALID_DRAG_COLOR",
    "ConnectionItem",
    "DragLineItem",
    "Hit",
    "NodeItem",
    "SelectionBandItem",
    "TreeView",
    "anchor_point",
    "clamp_to_scene",
    "connection_path",
    "iter_subtree",
    "nearest_free_offset",
]

#: What the mouse is doing (``TreeView._gesture``); the name earlier versions used.
_Gesture = GestureKind

LABEL_HIT_MARGIN = 4
MIN_ZOOM = 0.2
MAX_ZOOM = 3.0
GRID_STEP = 20.0  # scene units between grid lines; every fifth line is darker
NOTE_Z_BASE = -1e9  # notes are stacked below the connection lines (z -1) and the other nodes
GROUP_Z_BASE = -2e9  # groups are stacked below everything else, notes included


class _OutsideClickFilter(QObject):
    """Deselects the view's connection when the mouse is pressed anywhere else in its window."""

    def __init__(self, view: TreeView):
        super().__init__(view)
        self._view = view

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
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


class Hit:
    """Result of :meth:`TreeView.hit_test`."""

    EMPTY = "empty"
    NODE = "node"
    LABEL = "label"
    INTERACTIVE = "interactive"
    CONNECTION = "connection"
    RESIZE = "resize"  # a resize handle (``widget``) of a note or a group

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
    nodeSelectionChanged = Signal()  # the selected nodes changed (see selected_nodes)
    shown = Signal()  # the view became visible
    navigated = Signal()  # the user panned or zoomed

    HINT_TEXT = "Click New to create a behavior tree or Load to open one."

    # Read by event handlers, which Qt can also call while the view is being destroyed (it hides
    # its children then), after Python cleared the instance's attributes: safe defaults.
    _outside_filter_installed = False
    _selected_connection: ConnectionItem | None = None
    _active: Gesture | None = None

    def __init__(
        self,
        owner: BehaviorTreeWidget | None = None,
        parent: QWidget | None = None,
        document: TreeDocument | None = None,
    ):
        """``document``: the tree shown (by default a new, empty one)."""
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
        # Only what changed is repainted: every item paints inside its bounding rect.
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        #: The tree shown: its nodes, their links and its file. The view adds the items drawing them.
        self.document = document if document is not None else TreeDocument()
        self.document.view = self
        self._connections: dict[NodeWidget, ConnectionItem] = {}  # keyed by child node
        self._selected_connection: ConnectionItem | None = None
        self._selected_nodes: dict[NodeWidget, None] = {}  # an ordered set
        self._locked = False
        self._z_counter = 1.0
        self._note_z_counter = NOTE_Z_BASE
        self._group_z_counter = GROUP_Z_BASE

        self._active: Gesture | None = None  # the mouse gesture in progress (see _gesture)
        self._last_view_pos = QPoint()  # viewport position of the latest press or gesture move

        # Installed on the application only while a connection is selected (see
        # select_connection), so it costs nothing the rest of the time.
        self._outside_filter = _OutsideClickFilter(self)
        self._outside_filter_installed = False

    @property
    def _owner(self) -> BehaviorTreeWidget | None:
        return self._owner_ref() if self._owner_ref is not None else None

    @property
    def base_folder(self) -> str | None:
        """The folder relative sub-tree file names of the Sub-Tree nodes shown are relative to:
        the folder of the tree file shown (None while it has no file)."""
        return self.document.folder

    @property
    def _gesture(self) -> GestureKind:
        """What the mouse is doing: the kind of the gesture in progress, or ``IDLE``."""
        active = self._active
        return active.kind if active is not None else GestureKind.IDLE

    def _set_outside_filter(self, install: bool) -> None:
        app = QApplication.instance()
        if app is None or install == self._outside_filter_installed:
            return
        if install:
            app.installEventFilter(self._outside_filter)
        else:
            app.removeEventFilter(self._outside_filter)
        self._outside_filter_installed = install

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._set_outside_filter(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._set_outside_filter(self._selected_connection is not None)
        self.shown.emit()

    # ================================================================== model
    def nodes(self) -> list[NodeWidget]:
        """Every node of the tree shown, in the order they were added."""
        return self.document.nodes()

    def root(self) -> RootNodeWidget | None:
        return self.document.root()

    def connections(self) -> list[ConnectionItem]:
        return list(self._connections.values())

    def connection_item(self, child: NodeWidget) -> ConnectionItem | None:
        """The line connecting ``child`` to its parent (None when it has no parent in this view)."""
        return self._connections.get(child)

    def selected_connection(self) -> ConnectionItem | None:
        return self._selected_connection

    # ------------------------------------------------------------------ node selection
    def selected_nodes(self) -> list[NodeWidget]:
        """The selected nodes, in the order they were added to the view."""
        return sorted(self._selected_nodes, key=self.document.number)

    def is_node_selected(self, node: NodeWidget) -> bool:
        return node in self._selected_nodes

    def select_nodes(self, nodes: Iterable[NodeWidget], add: bool = False) -> None:
        """Select ``nodes`` (nodes not in this view are ignored); ``add`` keeps the current selection."""
        wanted = dict.fromkeys(node for node in nodes if node in self.document)
        self._set_selection({**self._selected_nodes, **wanted} if add else wanted)

    def deselect_nodes(self, nodes: Iterable[NodeWidget]) -> None:
        unwanted = set(nodes)
        self._set_selection({node: None for node in self._selected_nodes if node not in unwanted})

    def clear_node_selection(self) -> None:
        self._set_selection({})

    def select_all_nodes(self) -> None:
        self._set_selection(dict.fromkeys(self.document.nodes()))

    def _set_selection(self, selection: dict[NodeWidget, None]) -> None:
        changed = selection.keys() ^ self._selected_nodes.keys()
        if not changed:
            return
        self._selected_nodes = selection
        for node in changed:
            if node._item is not None:
                node._item.update()
        self.nodeSelectionChanged.emit()

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        """Lock the tree structure (connections, adding and deleting nodes).

        While locked, the order badges keep showing the order the running tree was built
        with; moving nodes re-numbers them when execution ends.
        """
        was_locked = self._locked
        self._locked = bool(locked)
        if self._locked and isinstance(self._active, ConnectGesture):
            self._finish_connecting(None)
        if was_locked and not self._locked:
            for node in self.document.nodes():
                if node._children:
                    self._refresh_order(node)

    def _check_unlocked(self) -> None:
        if self._locked:
            raise RuntimeError("the tree structure cannot be changed while the tree is executing; stop it first")

    def add_node(self, node: NodeWidget, pos: QPointF | tuple[float, float] | None = None) -> NodeItem:
        """Add ``node`` with its top-left corner at scene position ``pos`` (default: the origin)."""
        self._check_unlocked()
        if node._item is not None:
            raise ValueError(f"{node!r} is already part of a tree view")
        if isinstance(node, RootNodeWidget) and self.document.root() is not None:
            raise ValueError("a tree can only have one root node")
        item = NodeItem(node, self)
        self.scene().addItem(item)
        item.setPos(QPointF() if pos is None else QPointF(*pos) if isinstance(pos, tuple) else QPointF(pos))
        self.raise_node(node)
        node._canvas = self
        node._tree = self._owner
        self.document.add(node)
        self._refresh_label_states(node)
        node._on_added_to_tree()
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
        if node not in self.document:
            raise ValueError(f"{node!r} is not part of this view")
        active = self._active
        if active is not None and active.involves(node) and not isinstance(active, ConnectGesture):
            self._reset_gesture()  # a node being moved or resized
        if node in self._selected_nodes:
            self.deselect_nodes([node])
        active = self._active
        if active is not None:
            active.forget(node)
            if isinstance(active, ConnectGesture) and active.involves(node):
                self._finish_connecting(None)
        self._disconnect(node)
        for child in list(node._children):
            self._disconnect(child)
        self.document.remove(node)
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
        self.clear_node_selection()
        for node in self.document.nodes():
            self._remove_node(node)

    def can_connect(self, parent: NodeWidget, child: NodeWidget) -> bool:
        """True if ``child`` may be connected below ``parent`` (no self links, no cycles)."""
        return self.document.can_connect(parent, child)

    def connect_nodes(self, parent: NodeWidget, child: NodeWidget) -> None:
        """Connect ``child`` below ``parent``, replacing any previous parent of ``child``.

        A parent that already has as many children as it accepts (see
        ``NodeWidget.MaxChildren``, e.g. a Negation) loses its oldest child first.
        """
        self._check_unlocked()
        if not self.can_connect(parent, child):
            raise ValueError(f"cannot connect {child!r} below {parent!r}")
        if child._parent is parent:
            return
        if child._parent is not None:
            self._disconnect(child)
        limit = parent.MaxChildren()
        while limit is not None and parent._children and len(parent._children) >= limit:
            self._disconnect(parent._children[0])
        self.document.link(parent, child)
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
        self.document.unlink(child)
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

    def _refresh_order(self, parent: NodeWidget, force: bool = False, moved: NodeWidget | None = None) -> None:
        """Number the connection badges of ``parent``'s children in execution order.

        ``moved``: the child that moved or changed its size; only the badges of the children
        whose place changed are rewritten.
        """
        change = parent._child_moved(moved) if moved is not None else None  # also while locked
        if self._locked and not force:
            return  # keep the order of the running tree (see set_locked)
        children = parent.GetChildren()
        if change is not None:
            first, last = sorted(change)
            if first == last:
                return  # the order did not change
            indices = range(first, last + 1)
        else:
            indices = range(len(children))
        connections = self._connections
        for index in indices:
            item = connections.get(children[index])
            if item is not None:
                item.set_order(index + 1)

    def _refresh_label_states(self, node: NodeWidget) -> None:
        active = self._active
        placing = active.placing() if isinstance(active, ConnectGesture) else ()
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
        if node not in self.document:
            return
        connections = self._connections
        item = connections.get(node)
        if item is not None:
            item.update_path()
        for child in list(node._children):
            child_item = connections.get(child)
            if child_item is not None:
                child_item.update_path()
        if node._parent is not None:
            self._refresh_order(node._parent, moved=node)
        active = self._active
        if isinstance(active, ConnectGesture):
            active.invalidate_hover_test()  # labels may have moved under the mouse
            if active.source is not None and active.source[0] is node:
                active.update_line(self.mapToScene(self._last_view_pos))

    # ================================================================== view helpers
    def next_z(self) -> float:
        self._z_counter += 1.0
        return self._z_counter

    def raise_node(self, node: NodeWidget) -> None:
        """Stack ``node`` above the others; notes stay below the connection lines and the other
        nodes, and groups below everything else."""
        if node._item is None:
            return
        if isinstance(node, GroupNodeWidget):
            self._group_z_counter += 1.0
            node._item.setZValue(self._group_z_counter)
        elif isinstance(node, NotesNodeWidget):
            self._note_z_counter += 1.0
            node._item.setZValue(self._note_z_counter)
        else:
            node._item.setZValue(self.next_z())

    def group_contents(self, group: GroupNodeWidget) -> list[NodeWidget]:
        """The nodes (notes and groups included) lying completely inside ``group``, in view order."""
        area = group.scene_rect()
        if area is None:
            return []
        contents = []
        for node in self.document.nodes():
            rect = node.scene_rect() if node is not group else None
            if rect is not None and area.contains(rect):
                contents.append(node)
        return contents

    def _with_group_contents(self, nodes: list[NodeWidget]) -> list[NodeWidget]:
        """``nodes`` and the contents of the groups among them (which move with their group)."""
        result = dict.fromkeys(nodes)
        for node in nodes:
            if isinstance(node, GroupNodeWidget):
                result.update(dict.fromkeys(self.group_contents(node)))
        return list(result)

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
                if isinstance(widget, ResizeHandle):
                    return Hit(Hit.RESIZE, node, widget=widget)
                if isinstance(node, GroupNodeWidget) and not node.header_rect().contains(local):
                    continue  # the inside of a group: the click goes to what lies below it
                if widget is not None and is_interactive_widget(widget, node):
                    return Hit(Hit.INTERACTIVE, node, widget=widget)
                return Hit(Hit.NODE, node, widget=widget)
            if isinstance(item, ConnectionItem):
                return Hit(Hit.CONNECTION, connection=item)
            if isinstance(item, (DragLineItem, SelectionBandItem)):
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
        active = self._active
        source = active.source if isinstance(active, ConnectGesture) else None
        if source is None or hit.kind != Hit.LABEL:
            return None
        source_node, source_kind = source
        node, label_kind = hit.node, hit.label_kind  # set for every label hit
        if node is None or label_kind is None or node is source_node or label_kind == source_kind:
            return None
        parent, child = (source_node, node) if source_kind == CHILDREN else (node, source_node)
        if not self.can_connect(parent, child):
            return None
        return (node, label_kind)

    def _clear_scene_focus(self) -> None:
        scene = self.scene()
        if scene.focusItem() is not None:
            scene.setFocusItem(None)  # type: ignore[arg-type]  # Qt takes nullptr: no item has the focus
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    # ================================================================== mouse gestures
    def _abandon_gesture(self) -> None:
        """End a gesture whose mouse release was lost (e.g. a modal dialog took the mouse)."""
        active = self._active
        changed = active.abandon() if active is not None else False
        self._reset_gesture()
        if changed:
            self.modified.emit()

    def _reset_gesture(self) -> None:
        """End the gesture in progress without completing it."""
        active, self._active = self._active, None
        if active is not None:
            active.cancel()
        self.viewport().unsetCursor()

    def mousePressEvent(self, event) -> None:
        active = self._active
        if active is not None:
            # A press while a gesture is active means its release was lost (or a second
            # button was pressed during widget input): end the stale gesture first.
            if active.kind is GestureKind.PASSTHROUGH or not active.holds(event.buttons() & ~event.button()):
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
            self._active = PassThroughGesture(self)
            super().mousePressEvent(event)
            return

        hit = self.hit_test(pos)
        # A press anywhere but in the editor of a note being edited ends that editing.
        self._finish_note_editing(keep=hit.node if hit.kind == Hit.INTERACTIVE else None)
        if hit.kind != Hit.CONNECTION or event.button() != Qt.MouseButton.LeftButton:
            self.select_connection(None)
        if event.button() == Qt.MouseButton.MiddleButton:
            self._begin_pan(pos, Qt.MouseButton.MiddleButton)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._PRESS_HANDLERS.get(hit.kind, TreeView._press_empty)(self, event, hit, pos)

    @staticmethod
    def _modifiers(event) -> tuple[bool, bool]:
        """``(toggle, extend)``: Ctrl and Shift held during a press."""
        modifiers = event.modifiers()
        return (
            bool(modifiers & Qt.KeyboardModifier.ControlModifier),
            bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
        )

    def _press_connection(self, event, hit: Hit, pos: QPoint) -> None:
        """Select the line; dragging from it selects nodes, as on empty space (it is outside of
        any node), or pans with Ctrl."""
        self.select_connection(hit.connection)
        self._press_empty(event, hit, pos)

    def _press_label(self, event, hit: Hit, pos: QPoint) -> None:
        node, label_kind = hit.node, hit.label_kind
        assert node is not None and label_kind is not None, "a label hit names its node and label"
        if not self._locked:
            self._begin_connecting(node, label_kind, pos)
        # While executing, connection labels do nothing (they never move the node).
        event.accept()

    def _press_interactive(self, event, hit: Hit, pos: QPoint) -> None:
        if hit.node is not None:
            self.raise_node(hit.node)
        self._active = PassThroughGesture(self)
        super().mousePressEvent(event)

    def _press_resize(self, event, hit: Hit, pos: QPoint) -> None:
        node, handle = hit.node, hit.widget
        assert isinstance(node, BoxNodeWidget) and isinstance(handle, ResizeHandle), "a resize hit names both"
        toggle, extend = self._modifiers(event)
        if toggle:
            # As on a node: Ctrl + drag pans, Ctrl + click toggles the note in the selection.
            self._begin_pan(pos, Qt.MouseButton.LeftButton, toggles=node)
        elif extend:
            self._clear_scene_focus()
            self.select_nodes([node], add=True)
        else:
            selected = self.is_node_selected(node)
            if not selected:
                self.select_nodes([node])
            self._begin_resize(node, handle, pos, click_selects=node if selected else None)
        event.accept()

    def _press_node(self, event, hit: Hit, pos: QPoint) -> None:
        node = hit.node
        assert node is not None, "a node hit names its node"
        toggle, extend = self._modifiers(event)
        if toggle:
            # Ctrl + drag pans; Ctrl + click (without dragging) toggles the node in the selection.
            self._begin_pan(pos, Qt.MouseButton.LeftButton, toggles=node)
        elif extend:
            # Shift + click adds the node to the selection.
            self._clear_scene_focus()
            self.select_nodes([node], add=True)
        else:
            # Pressing a selected node keeps the selection, so dragging moves all of it.
            selected = self.is_node_selected(node)
            if not selected:
                self.select_nodes([node])
            self._begin_move(node, pos, click_selects=node if selected else None)
        event.accept()

    def _press_empty(self, event, hit: Hit, pos: QPoint) -> None:
        """Empty space: drag selects with a rectangle (Shift adds to the selection), Ctrl pans."""
        toggle, extend = self._modifiers(event)
        if toggle:
            self._begin_pan(pos, Qt.MouseButton.LeftButton)
        else:
            self._begin_band(pos, add=extend)
        event.accept()

    _PRESS_HANDLERS = {
        Hit.CONNECTION: _press_connection,
        Hit.LABEL: _press_label,
        Hit.INTERACTIVE: _press_interactive,
        Hit.RESIZE: _press_resize,
        Hit.NODE: _press_node,
    }

    def mouseMoveEvent(self, event) -> None:
        active = self._active
        if active is not None and active.kind is not GestureKind.PASSTHROUGH and not active.holds(event.buttons()):
            self._abandon_gesture()
            active = self._active
        if active is None:
            super().mouseMoveEvent(event)
        else:
            active.move(event.position().toPoint(), event)

    def mouseReleaseEvent(self, event) -> None:
        active = self._active
        if active is None:
            super().mouseReleaseEvent(event)
            return
        active.release(event.position().toPoint(), event)
        if active.kind is not GestureKind.PASSTHROUGH:
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self.hit_test(event.position().toPoint())
            if hit.kind != Hit.INTERACTIVE:
                self.mousePressEvent(event)
                return
        super().mouseDoubleClickEvent(event)

    def _begin_pan(self, pos: QPoint, button: Qt.MouseButton, toggles: NodeWidget | None = None) -> None:
        self._active = PanGesture(self, pos, button, toggles)

    def _begin_move(self, node: NodeWidget, pos: QPoint, click_selects: NodeWidget | None = None) -> None:
        """Start moving ``node``, together with the other selected nodes if it is selected."""
        self._active = MoveGesture(self, node, pos, click_selects)

    def _begin_resize(
        self, node: BoxNodeWidget, handle: ResizeHandle, pos: QPoint, click_selects: NodeWidget | None = None
    ) -> None:
        """Start resizing the note or group ``node`` by dragging ``handle``."""
        self._active = ResizeGesture(self, node, handle, pos, click_selects)

    def _begin_band(self, pos: QPoint, add: bool = False) -> None:
        """Start selecting the nodes touched by a rectangle dragged open from ``pos`` (see BandGesture)."""
        gesture = BandGesture(self, pos, add)
        self._active = gesture
        gesture.update(pos)

    def _begin_connecting(self, node: NodeWidget, kind: str, pos: QPoint) -> None:
        gesture = ConnectGesture(self, node, kind)
        self._active = gesture
        gesture.begin(pos)

    def _finish_connecting(self, target: tuple[NodeWidget, str] | None) -> None:
        """End a connection drag; connect the source to ``target`` (a node and its label kind) if allowed."""
        active = self._active
        if not isinstance(active, ConnectGesture):
            return
        self._active = None
        active.finish(target)

    def _offer_connected_node(self, pos: QPoint, global_pos: QPoint) -> None:
        """End a connection drag released over the graph: show the add-node menu at ``pos``.

        The menu is the one shown by a right click there; the node chosen from it is added
        at the release point and connected to the label the drag started from. The drag
        line stays visible while the menu is open.
        """
        active = self._active
        if not isinstance(active, ConnectGesture):
            return
        source = active.source
        line = active.detach_line()
        self._finish_connecting(None)
        try:
            if source is not None:
                menu = self.build_add_menu(self.mapToScene(pos), connect_to=source)
                self._exec_menu(menu, global_pos)
        finally:
            if line is not None and line.scene() is self.scene():
                self.scene().removeItem(line)

    def _finish_note_editing(self, keep: NodeWidget | None = None) -> None:
        """End the text editing of every note except ``keep``."""
        for node in self.document.nodes():
            if node is not keep and isinstance(node, NotesNodeWidget) and node.IsEditing():
                node.finish_editing()

    def is_connecting(self) -> bool:
        return isinstance(self._active, ConnectGesture)

    def drag_line(self) -> DragLineItem | None:
        active = self._active
        return active.line if isinstance(active, ConnectGesture) else None

    # ================================================================== keyboard / wheel
    def _edit_command(self, event) -> str | None:
        """The editing command (copy, paste, select_all, delete) bound to a key event, if any."""
        if event.matches(QKeySequence.StandardKey.Copy):
            return "copy"
        if event.matches(QKeySequence.StandardKey.Paste):
            return "paste"
        if event.matches(QKeySequence.StandardKey.SelectAll):
            return "select_all"
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and (
            self._selected_nodes or self._selected_connection is not None
        ):
            return "delete"
        return None

    def _editor_has_focus(self) -> bool:
        """True while an input widget of a node has the keyboard (its own copy/paste apply)."""
        item = self.scene().focusItem()
        if isinstance(item, NodeItem) and isinstance(item.node, NotesNodeWidget) and not item.node.IsEditing():
            # A note has no input widget besides its text editor; its item can keep the scene focus
            # after editing ended (e.g. while the window was inactive), which must not block the keys.
            return False
        if (
            isinstance(item, NodeItem)
            and isinstance(item.node, GroupNodeWidget)
            and not item.node.name_editor().hasFocus()
        ):
            return False  # likewise for a group whose name editor does not have the keyboard
        return item is not None

    def event(self, event) -> bool:
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and not self._editor_has_focus()
            and self._edit_command(event) is not None
        ):
            event.accept()  # the keys go to keyPressEvent, not to an application shortcut
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if isinstance(self._active, ConnectGesture):
                self._finish_connecting(None)
                event.accept()
                return
            if self._selected_connection is not None:
                self.select_connection(None)
                event.accept()
                return
            if self._selected_nodes and not self._editor_has_focus():
                self.clear_node_selection()
                event.accept()
                return
        command = None if self._editor_has_focus() else self._edit_command(event)
        if command is not None:
            event.accept()
            if command == "select_all":
                self.select_all_nodes()
            elif command == "copy":
                self.copy_selection()
            elif command == "delete":
                self.delete_selection()
            else:
                self.paste()
            return
        super().keyPressEvent(event)

    def delete_selection(self) -> bool:
        """Delete the selected connection and the selected nodes (Delete key); nothing while locked.

        The root is never deleted: when it is selected, its connections are deleted instead.
        Returns True if anything was deleted.
        """
        if self._locked:
            return False
        deleted = False
        if self._selected_connection is not None:
            self.disconnect_node(self._selected_connection.child_node)
            deleted = True
        for node in self.selected_nodes():
            if isinstance(node, RootNodeWidget):
                for child in list(node._children):
                    self.disconnect_node(child)
                    deleted = True
            else:
                self.remove_node(node)
                deleted = True
        return deleted

    def copy_selection(self) -> bool:
        """Copy the selected nodes to the clipboard (Ctrl+C). Returns True if something was copied."""
        if self._owner is None:
            return False
        try:
            return self._owner._copy_nodes(self.selected_nodes())
        except Exception:
            log.exception("copying nodes failed")
            return False

    def paste(self) -> list[NodeWidget]:
        """Paste nodes from the clipboard next to their originals (Ctrl+V); nothing while locked.

        The view scrolls as little as needed to show the pasted nodes.
        """
        if self._owner is None or self._locked:
            return []
        try:
            pasted = self._owner._paste_nodes()
        except Exception:
            log.exception("pasting nodes failed")
            return []
        if pasted:
            area = QRectF()
            for node in pasted:
                rect = node.scene_rect()
                if rect is not None:  # pasted nodes are in this view
                    area = area.united(rect)
            self.ensureVisible(area, 40, 40)
        return pasted

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        reason = event.reason()
        if reason == Qt.FocusReason.PopupFocusReason:
            return  # a context menu opened; keep the selection for its actions
        if reason == Qt.FocusReason.ActiveWindowFocusReason:
            # The window lost activation (e.g. a modal dialog): a release may never come.
            if self._gesture is not GestureKind.PASSTHROUGH:
                self._abandon_gesture()
            return
        # Focus moved elsewhere (clicked another widget or tabbed away): "clicking
        # anywhere else" deselects a selected connection.
        self.select_connection(None)

    def wheelEvent(self, event) -> None:
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
    def contextMenuEvent(self, event) -> None:
        if self._gesture is not GestureKind.IDLE:
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
        if hit.kind in (Hit.NODE, Hit.LABEL, Hit.RESIZE) and hit.node is not None:
            menu = self.build_node_menu(hit.node)
        elif hit.kind == Hit.CONNECTION and hit.connection is not None:
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

    def build_add_menu(self, scene_pos: QPointF, connect_to: tuple[NodeWidget, str] | None = None) -> QMenu:
        """Menu listing every available node type; choosing one adds it at ``scene_pos``.

        With ``connect_to`` (a node and its ``"parent"`` / ``"children"`` label) the new
        node is also connected to that label when the connection is possible.
        """
        return menus.build_add_menu(self, scene_pos, connect_to)

    def build_node_menu(self, node: NodeWidget) -> QMenu:
        """Context menu of ``node`` (see :func:`.menus.build_node_menu`)."""
        return menus.build_node_menu(self, node)

    def build_connection_menu(self, item: ConnectionItem) -> QMenu:
        return menus.build_connection_menu(self, item)

    # ------------------------------------------------------------------ menu actions
    def _add_node_from_menu(
        self, type_name: str, scene_pos: QPointF, connect_to: tuple[NodeWidget, str] | None = None
    ) -> NodeWidget | None:
        if self._locked or self._owner is None:
            return None
        node = self._owner._create_node(type_name)
        item = self.add_node(node, scene_pos)
        if connect_to is not None and self._is_current(connect_to[0]):
            source, source_kind = connect_to
            parent, child = (source, node) if source_kind == CHILDREN else (node, source)
            if self.can_connect(parent, child):
                # Put the new node's own connection label where the line was released.
                node._notify_geometry()  # lay the new node out now: its label positions are needed
                anchor = anchor_point(node, PARENT if node is child else CHILDREN)
                item.setPos(item.pos() + (scene_pos - anchor))
                self.connect_nodes(parent, child)
        return node

    def _select_group_contents(self, group: GroupNodeWidget) -> None:
        if self._is_current(group):
            self.select_nodes([group, *self.group_contents(group)])

    def _set_group_color(self, group: GroupNodeWidget, color: QColor | str) -> None:
        if self._is_current(group) and group._apply_color(QColor(color).name(), runtime=False):
            self.modified.emit()  # a user edit, also while executing

    def _choose_group_color(self, group: GroupNodeWidget) -> None:
        if not self._is_current(group):
            return
        color = self._choose_color(QColor(group.GetBorderColor()))
        if color is not None and color.isValid() and self._is_current(group):
            self._set_group_color(group, color)

    def _choose_color(self, initial: QColor) -> QColor | None:
        """Ask for a colour (separate method so tests can replace the dialog); None if cancelled."""
        color = QColorDialog.getColor(initial, self, "Group Border Color")
        return color if color.isValid() else None

    def _zoom_in(self, node: SubTreeNodeWidget) -> None:
        if self._owner is None or not self._is_current(node):
            return
        self._owner._zoom_in_interactively(node)

    def _promote_group(self, group: GroupNodeWidget) -> None:
        if self._locked or self._owner is None or not self._is_current(group):
            return
        self._owner._promote_group_interactively(group)

    def _delete_connection(self, item: ConnectionItem) -> None:
        if not self._locked and self._connections.get(item.child_node) is item:
            self.disconnect_node(item.child_node)

    def _delete_node_from_menu(self, node: NodeWidget) -> None:
        if self._locked or isinstance(node, RootNodeWidget) or not self._is_current(node):
            return
        self.remove_node(node)

    def _is_current(self, node: NodeWidget) -> bool:
        return node in self.document and shiboken6.isValid(node)

    def _select_subtree(self, node: NodeWidget) -> None:
        if self._is_current(node):
            self.select_nodes(iter_subtree(node))

    def _edit_note(self, node: NotesNodeWidget) -> None:
        if self._is_current(node):
            node.begin_editing()

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
    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            if not self.isEnabled():
                self._reset_gesture()
            self.viewport().update()

    def drawBackground(self, painter: QPainter, rect: QRectF | QRect) -> None:
        palette = self.palette()
        base = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Base)
        painter.fillRect(rect, base)
        step = GRID_STEP
        if self.zoom() * step < 6:
            return
        dark = base.lightness() < 128
        minor = QColor(255, 255, 255, 18) if dark else QColor(0, 0, 0, 16)
        major = QColor(255, 255, 255, 36) if dark else QColor(0, 0, 0, 34)
        lines: dict[bool, list[QLineF]] = {False: [], True: []}  # minor and major (every fifth) lines
        top, bottom, left, right = rect.top(), rect.bottom(), rect.left(), rect.right()
        x = math.floor(left / step) * step
        while x < right:
            lines[round(x / step) % 5 == 0].append(QLineF(x, top, x, bottom))
            x += step
        y = math.floor(top / step) * step
        while y < bottom:
            lines[round(y / step) % 5 == 0].append(QLineF(left, y, right, y))
            y += step
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for is_major, batch in lines.items():
            if batch:
                painter.setPen(QPen(major if is_major else minor, 0))
                painter.drawLines(batch)
        painter.restore()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        if not self.isEnabled():
            # The hint (drawForeground) is fixed to the viewport, but a scroll moves the pixels
            # already painted (SmartViewportUpdate) and repaints only the uncovered strip.
            self.viewport().update()

    def drawForeground(self, painter: QPainter, rect: QRectF | QRect) -> None:
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
