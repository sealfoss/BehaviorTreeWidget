"""A behavior tree and its file: :class:`TreeDocument`.

A document holds a tree's nodes and the links between them, the tree file it is stored in, the
options of that file and whether it has unsaved changes. The tree itself and every sub-tree shown
with Zoom In is a document shown in a :class:`~behavior_tree_widget.canvas.TreeView`; the nodes
built to execute a Sub-Tree node form a document that is not shown at all. Saving, reading back
what is shown and tracking unsaved changes work the same way for every document.
"""

from __future__ import annotations

import itertools
import os
from typing import TYPE_CHECKING, Any

from .config import TreeConfig
from .nodes import NodeWidget, RootNodeWidget
from .serialization import write_json_atomic
from .tree_io import tree_data

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import TreeView

__all__ = ["TreeDocument"]


class TreeDocument:
    """A behavior tree: its nodes and their links, and the file it belongs to.

    The links between nodes live in the nodes (``GetParent`` / ``GetChildren``); the document
    keeps its nodes in the order they were added, knows its Root and changes the links. A view
    showing the document adds and removes the items drawing them (GUI thread).

    Attributes:
        view: The view showing the tree, or None (set by the view).
        path: The tree file (None until the tree was created, loaded or saved).
        config: The options stored in the tree file.
        modified: True while the tree has changes that are not in its file.
        legacy_blackboard: The "blackboard" section of a single-file tree of an earlier version.
            A sub-tree writes it back unchanged when it is saved (it uses the blackboard of the
            tree running it); the tree itself keeps its blackboard in a blackboard file.
    """

    def __init__(self, path: str | None = None, config: TreeConfig | None = None, legacy_blackboard: Any = None):
        self.view: TreeView | None = None
        self.path = path
        self.config = config if config is not None else TreeConfig()
        self.modified = False
        self.legacy_blackboard = legacy_blackboard
        self._nodes: dict[NodeWidget, int] = {}  # every node, with a number giving the order they were added in
        self._numbers = itertools.count()
        self._root: RootNodeWidget | None = None

    @property
    def folder(self) -> str | None:
        """The folder of the tree file (relative sub-tree file names are relative to it)."""
        return os.path.dirname(self.path) if self.path else None

    # ================================================================== the graph
    def nodes(self) -> list[NodeWidget]:
        """Every node, in the order they were added."""
        return list(self._nodes)

    def __contains__(self, node: object) -> bool:
        return node in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def number(self, node: NodeWidget) -> int:
        """Where ``node`` comes in the order the nodes were added (-1 for other nodes)."""
        return self._nodes.get(node, -1)

    def root(self) -> RootNodeWidget | None:
        return self._root

    def add(self, node: NodeWidget) -> None:
        """Make ``node`` (not linked to any node) part of the tree.

        Raises:
            ValueError: ``node`` is part of the tree already, or is a second Root.
        """
        if node in self._nodes:
            raise ValueError(f"{node!r} is part of the tree already")
        if isinstance(node, RootNodeWidget):
            if self._root is not None:
                raise ValueError("a tree can only have one root node")
            self._root = node
        self._nodes[node] = next(self._numbers)

    def remove(self, node: NodeWidget) -> None:
        """Take ``node`` out of the tree (its links were removed first).

        Raises:
            ValueError: ``node`` is not part of the tree.
        """
        if self._nodes.pop(node, None) is None:
            raise ValueError(f"{node!r} is not part of this tree")
        if node is self._root:
            self._root = None

    def can_connect(self, parent: NodeWidget, child: NodeWidget) -> bool:
        """True if ``child`` may become a child of ``parent``: both are part of the tree, ``parent``
        takes children, ``child`` takes a parent, and no cycle would result."""
        if parent is child or parent not in self._nodes or child not in self._nodes:
            return False
        if not parent.HasChildConnections() or not child.HasParentConnection():
            return False
        ancestor: NodeWidget | None = parent
        while ancestor is not None:  # child must not be an ancestor of parent
            if ancestor is child:
                return False
            ancestor = ancestor._parent
        return True

    def link(self, parent: NodeWidget, child: NodeWidget) -> None:
        """Make ``child`` (without a parent) the last child of ``parent`` (checked with :meth:`can_connect`)."""
        parent._link(child)

    def unlink(self, child: NodeWidget) -> NodeWidget | None:
        """Remove the link between ``child`` and its parent; returns the parent (None: it had none)."""
        parent = child._parent
        if parent is not None:
            parent._unlink(child)
        return parent

    # ================================================================== the file
    def data(self, problems: list[str] | None = None, quiet: bool = False, check_raw: bool = False) -> dict:
        """The tree as shown, as the JSON-compatible data of its tree file (see :func:`tree_io.tree_data`).

        Raises:
            RuntimeError: the document is not shown in a view.
        """
        if self.view is None:
            raise RuntimeError("only a tree shown in a view can be written to a file")
        data = tree_data(self.view, self.config, problems, quiet, check_raw)
        if self.legacy_blackboard is not None:
            data["blackboard"] = self.legacy_blackboard  # kept as it was read
        return data

    def write(self, path: str | None = None) -> list[str]:
        """Write the tree to ``path`` (default: its own file), replacing the file safely.

        Values that cannot be written are left out; the returned list describes them. Data of
        unknown node types kept as read from a (hand-written) file may be nested too deeply to be
        written: it is left out as well. Does not change :attr:`path` or :attr:`modified`.

        Raises:
            OSError, ValueError, TypeError: the file could not be written.
        """
        target = path or self.path
        if not target:
            raise ValueError("the tree has no file")
        problems: list[str] = []
        try:
            write_json_atomic(target, self.data(problems))
        except RecursionError:
            problems.clear()
            write_json_atomic(target, self.data(problems, check_raw=True))
        return problems
