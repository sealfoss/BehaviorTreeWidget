"""Graphics items of the tree view: embedded nodes, connection lines, the drag line and the
selection rectangle (internal; :mod:`.canvas` re-exports them)."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsProxyWidget, QGraphicsRectItem, QWidget

from .geometry import anchor_point, connection_path
from .nodes import CHILDREN, CONNECTION_COLORS, PARENT, STATUS_COLORS, ConnectionState, NodeStatus, NodeWidget
from .notes import BoxNodeWidget

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import TreeView

__all__ = [
    "INVALID_DRAG_COLOR",
    "LINE_COLOR",
    "LINE_HIT_WIDTH",
    "LINE_WIDTH",
    "SELECTED_LINE_COLOR",
    "SELECTED_LINE_WIDTH",
    "SELECTION_COLOR",
    "VALID_DRAG_COLOR",
    "ConnectionItem",
    "DragLineItem",
    "NodeItem",
    "SelectionBandItem",
]

LINE_WIDTH = 2.5
SELECTED_LINE_WIDTH = 3.5
LINE_HIT_WIDTH = 12.0

LINE_COLOR = QColor(CONNECTION_COLORS[ConnectionState.CONNECTED])  # blue
SELECTED_LINE_COLOR = QColor(CONNECTION_COLORS[ConnectionState.PLACING])  # green
VALID_DRAG_COLOR = QColor(CONNECTION_COLORS[ConnectionState.PLACING])  # green
INVALID_DRAG_COLOR = QColor(CONNECTION_COLORS[ConnectionState.DISCONNECTED])  # red
SELECTION_COLOR = QColor("#0a84ff")  # outline of selected nodes and of the selection rectangle


class NodeItem(QGraphicsProxyWidget):
    """Scene item embedding a :class:`NodeWidget`."""

    def __init__(self, node: NodeWidget, view: TreeView):
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

    @override
    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        super().paint(painter, option, widget)  # type: ignore[arg-type]  # Qt passes no widget when painting off screen
        selected = self.view.is_node_selected(self.node)
        note = isinstance(self.node, BoxNodeWidget)  # a note or group has no status, and its own border
        if note and not selected:
            return
        status = self.node.GetStatus()
        running = status is NodeStatus.RUNNING
        color = QColor(STATUS_COLORS[status]) if status is not NodeStatus.READY else QColor(120, 120, 120)
        width = 3.0 if running else 1.5
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if selected:
            # Selected: a tinted node inside a wide selection outline; the status border stays inside it.
            tint = QColor(SELECTION_COLOR)
            tint.setAlpha(28)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(self.boundingRect(), 4.0, 4.0)
            painter.setPen(QPen(SELECTION_COLOR, 3.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.boundingRect().adjusted(1.5, 1.5, -1.5, -1.5), 4.0, 4.0)
            inset = 3.0 + width / 2.0
        else:
            inset = width / 2.0
        if not note:
            painter.setPen(QPen(color, width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.boundingRect().adjusted(inset, inset, -inset, -inset), 4.0, 4.0)
        painter.restore()


class ConnectionItem(QGraphicsPathItem):
    """A parent -> child connection line (blue, green when selected) with an order badge.

    Hit testing stays in Qt's C++ code: the item's own (C++) path is the area that takes
    clicks, the line ``LINE_HIT_WIDTH`` wide plus the order badge, computed once whenever the
    line or its badge changes. The item has no (C++) pen, so Qt uses that area as the item's
    shape and its bounds as the bounding rect, without calling Python and without stroking
    the curve on every query. :meth:`path` and :meth:`pen` return the curve and the pen it is
    drawn with; :meth:`hit_area` returns the area taking clicks.
    """

    BADGE_RADIUS = 8.0
    BADGE_MARGIN = 1.0  # around the badge in the hit area and the bounds (antialiasing)

    def __init__(self, parent_node: NodeWidget, child_node: NodeWidget):
        super().__init__()
        self.parent_node = parent_node
        self.child_node = child_node
        self._selected = False
        self._order = 0
        self._curve = QPainterPath()
        self._line_pen = QPen()
        QGraphicsPathItem.setPen(self, QPen(Qt.PenStyle.NoPen))  # Qt's shape is then the hit area itself
        # paint() gets the exposed rect, to skip lines that only pass by a repainted area.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)
        self.setZValue(-1.0)
        self._apply_pen()
        self.update_path()

    # ------------------------------------------------------------------ state
    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self._apply_pen()
            self.update()  # the line stays inside the hit area: the bounds do not change

    def color(self) -> QColor:
        return SELECTED_LINE_COLOR if self._selected else LINE_COLOR

    def order(self) -> int:
        return self._order

    def set_order(self, order: int) -> None:
        if order != self._order:
            had_badge = bool(self._order)
            self._order = order
            if had_badge != bool(order):
                self._update_hit_area()  # the badge appears or disappears: the hit area and the bounds change
            else:
                self.update()  # only the number changes

    def _apply_pen(self) -> None:
        pen = QPen(self.color(), SELECTED_LINE_WIDTH if self._selected else LINE_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self._line_pen = pen

    # ------------------------------------------------------------------ geometry
    @override
    def path(self) -> QPainterPath:
        """The connection curve, from the parent's ChildConnections label to the child's ParentConnection label."""
        return QPainterPath(self._curve)

    @override
    def pen(self) -> QPen:
        """The pen the line is drawn with (blue, green and wider when selected)."""
        return QPen(self._line_pen)

    def hit_area(self) -> QPainterPath:
        """The area that takes clicks, in item coordinates: the line ``LINE_HIT_WIDTH`` wide and the badge."""
        return QGraphicsPathItem.path(self)

    def update_path(self) -> None:
        """Follow the connection labels of the two nodes (call it when either node moves or resizes)."""
        self._curve = connection_path(anchor_point(self.parent_node, CHILDREN), anchor_point(self.child_node, PARENT))
        self._update_hit_area()

    def _update_hit_area(self) -> None:
        stroker = QPainterPathStroker()
        stroker.setWidth(LINE_HIT_WIDTH)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        area = stroker.createStroke(self._curve)
        area.setFillRule(Qt.FillRule.WindingFill)  # overlapping outlines add up instead of leaving holes
        if self._order:
            badge = QPainterPath()
            badge.addEllipse(self._badge_rect().adjusted(*self._badge_margins()))
            combined = QPainterPath(area)
            combined.addPath(badge)
            if not combined.contains(self._badge_center()):
                # The badge was drawn the other way round than the line's outline where they
                # overlap, which cancels out with the winding fill rule: reverse the badge.
                combined = QPainterPath(area)
                combined.addPath(badge.toReversed())
            area = combined
        QGraphicsPathItem.setPath(self, area)

    def _badge_margins(self) -> tuple[float, float, float, float]:
        margin = self.BADGE_MARGIN
        return (-margin, -margin, margin, margin)

    def _badge_center(self) -> QPointF:
        end = self._curve.pointAtPercent(1.0)
        return QPointF(end.x(), end.y() - self.BADGE_RADIUS - 6.0)

    def _badge_rect(self) -> QRectF:
        r = self.BADGE_RADIUS
        c = self._badge_center()
        return QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)

    # ------------------------------------------------------------------ painting
    @override
    def paint(self, painter: QPainter, option, widget: QWidget | None = None) -> None:
        exposed = option.exposedRect  # an invalid rect: not known, paint everything
        if (
            exposed.isValid()
            and not exposed.contains(self.boundingRect())
            and not QGraphicsPathItem.path(self).intersects(exposed)
        ):
            return  # the repainted area only lies within the bounds, not on the line (long lines in a fan)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(self._line_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._curve)
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


class SelectionBandItem(QGraphicsRectItem):
    """Rectangle drawn while nodes are selected by dragging over empty space."""

    def __init__(self):
        super().__init__()
        self.setZValue(1e9)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        pen = QPen(SELECTION_COLOR, 1.0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)  # one pixel wide at every zoom
        self.setPen(pen)
        fill = QColor(SELECTION_COLOR)
        fill.setAlpha(36)
        self.setBrush(fill)
