"""Geometry of the tree view: the scene area, connection curves and placement (internal).

Pure helpers without view state, used by :mod:`.canvas`, :mod:`.items` and the widget.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPainterPath

from .nodes import CHILDREN

if TYPE_CHECKING:  # pragma: no cover
    from .nodes import NodeWidget

__all__ = [
    "PASTE_GAP",
    "SCENE_EXTENT",
    "SCENE_MARGIN",
    "anchor_point",
    "clamp_to_scene",
    "connection_path",
    "iter_subtree",
    "nearest_free_offset",
]

SCENE_EXTENT = 100_000.0
SCENE_MARGIN = 2_000.0  # room kept for a node's size inside the scene rect
PASTE_GAP = 20.0  # minimum distance between pasted nodes and the nodes already in the view


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


def nearest_free_offset(rects: Iterable[QRectF], obstacles: Iterable[QRectF], gap: float = PASTE_GAP) -> QPointF:
    """The shortest offset that moves ``rects`` together (keeping their arrangement) off ``obstacles``.

    Moved rectangles keep at least ``gap`` from every obstacle (exactly ``gap`` is allowed).
    Ties prefer moving right, then down. The search is exact: an offset is blocked inside
    the (open) rectangles where a moved rectangle would overlap an enlarged obstacle, and
    the nearest free offset lies on an edge line of those regions, nearest lines first.
    """
    group = [(r.left(), r.top(), r.right(), r.bottom()) for r in rects]
    walls = [(o.left() - gap, o.top() - gap, o.right() + gap, o.bottom() + gap) for o in obstacles]
    blocked = [
        (wx1 - gx2, wy1 - gy2, wx2 - gx1, wy2 - gy1)
        for gx1, gy1, gx2, gy2 in group
        for wx1, wy1, wx2, wy2 in walls
    ]
    if not any(x1 < 0.0 < x2 and y1 < 0.0 < y2 for x1, y1, x2, y2 in blocked):
        return QPointF(0.0, 0.0)

    def nearest_on_line(regions: list, value: float, vertical: bool) -> float:
        """Free coordinate nearest to 0 on the line x = value (vertical) or y = value."""
        spans = sorted(
            (y1, y2) if vertical else (x1, x2)
            for x1, y1, x2, y2 in regions
            if (x1 < value < x2 if vertical else y1 < value < y2)
        )
        low = high = None  # the merged span containing 0, if any
        for start, end in spans:
            if high is not None and start < high:
                high = max(high, end)
                continue
            if high is not None and low < 0.0 < high:
                break  # the span containing 0 is complete
            low, high = start, end
        if low is None or not low < 0.0 < high:
            return 0.0
        return high if high <= -low else low

    best: tuple | None = None

    def consider(dx: float, dy: float) -> None:
        nonlocal best
        key = (round(math.hypot(dx, dy), 6), dx < 0.0, dy < 0.0, abs(dy))
        if best is None or key < best[0]:
            best = (key, dx, dy)

    # The two axes give a first, usually close, candidate; only regions nearer than it matter.
    consider(nearest_on_line(blocked, 0.0, vertical=False), 0.0)
    consider(0.0, nearest_on_line(blocked, 0.0, vertical=True))
    limit = best[0][0] + 1e-6
    near = [
        region
        for region in blocked
        if math.hypot(max(region[0], -region[2], 0.0), max(region[1], -region[3], 0.0)) < limit
    ]
    for vertical in (True, False):
        lines = sorted({value for region in near for value in (region[0::2] if vertical else region[1::2])}, key=abs)
        for value in lines:
            if abs(value) > best[0][0] + 1e-6:
                break
            other = nearest_on_line(near, value, vertical)
            if vertical:
                consider(value, other)
            else:
                consider(other, value)
    return QPointF(best[1], best[2])


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


def iter_subtree(node: NodeWidget) -> list[NodeWidget]:
    """``node`` and all of its descendants (depth first, children in execution order)."""
    result = [node]
    for child in node.GetChildren():
        result.extend(iter_subtree(child))
    return result
