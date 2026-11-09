"""Sub-trees shown with Zoom In: :class:`ZoomLevel` and the :class:`ZoomStack` of a widget.

Zoom In shows the tree of a Sub-Tree node in a view of its own, stacked over the view of the tree
containing it; Go Back removes the top level. Each level is a :class:`TreeDocument` (its own file,
options and unsaved-changes state) plus the Sub-Tree node it was zoomed into from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

from ._common import file_key
from .document import TreeDocument

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import TreeView
    from .nodes import NodeWidget
    from .subtree import SubTreeNodeWidget

__all__ = ["ZoomLevel", "ZoomStack"]


@dataclass(eq=False)
class ZoomLevel:
    """A sub-tree shown in place of the tree that contains it."""

    document: TreeDocument
    scope: tuple[str, ...]  # ids of the Sub-Tree nodes zoomed into, from the tree itself to this one
    entry: SubTreeNodeWidget  # the Sub-Tree node zoomed into (it lies in the level below)

    @property
    def view(self) -> TreeView:
        return self.document.view

    @property
    def path(self) -> str:
        return self.document.path


class ZoomStack:
    """The sub-trees shown with Zoom In, the one shown last at the top."""

    def __init__(self) -> None:
        self._levels: list[ZoomLevel] = []

    def __len__(self) -> int:
        return len(self._levels)

    def __bool__(self) -> bool:
        return bool(self._levels)

    def __iter__(self) -> Iterator[ZoomLevel]:
        return iter(list(self._levels))

    def __reversed__(self) -> Iterator[ZoomLevel]:
        return reversed(list(self._levels))

    def __getitem__(self, index: int) -> ZoomLevel:
        return self._levels[index]

    @property
    def top(self) -> ZoomLevel | None:
        """The level shown, or None while the tree itself is shown."""
        return self._levels[-1] if self._levels else None

    def push(self, level: ZoomLevel) -> None:
        self._levels.append(level)

    def remove(self, level: ZoomLevel) -> None:
        self._levels.remove(level)

    def views(self) -> list[TreeView]:
        return [level.view for level in self._levels]

    def paths(self) -> list[str]:
        return [level.path for level in self._levels]

    def modified(self) -> list[ZoomLevel]:
        """The levels with unsaved changes, from the bottom up."""
        return [level for level in self._levels if level.document.modified]

    def level_of_view(self, view: object) -> ZoomLevel | None:
        return next((level for level in self._levels if level.view is view), None)

    def level_of_file(self, path: str) -> ZoomLevel | None:
        """The level showing the tree file ``path`` (however the path is written), if any."""
        if not self._levels:
            return None
        key = file_key(path)
        return next((level for level in self._levels if file_key(level.path) == key), None)

    def twins(self) -> dict[tuple[tuple[str, ...], str], NodeWidget]:
        """``{(scope, node id): node}`` of the nodes shown: the executor shows the status of a hidden
        sub-tree node it runs on the shown node with the same place."""
        return {(level.scope, node.GetId()): node for level in self._levels for node in level.view.nodes()}
