"""The file state of a behavior tree shown in a view: :class:`TreeDocument`.

The tree itself and every sub-tree shown with Zoom In is a document: a view showing its nodes,
the tree file it is stored in, the options of that file and whether it has unsaved changes.
Saving, reading back what is shown and tracking unsaved changes work the same way for all of
them.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from .config import TreeConfig
from .serialization import write_json_atomic
from .tree_io import tree_data

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import TreeView

__all__ = ["TreeDocument"]


class TreeDocument:
    """One behavior tree shown in a view, and the file it belongs to.

    Attributes:
        view: The view showing the tree (its nodes and connections).
        path: The tree file (None until the tree was created, loaded or saved).
        config: The options stored in the tree file.
        modified: True while the tree has changes that are not in its file.
        legacy_blackboard: The "blackboard" section of a single-file tree of an earlier version.
            A sub-tree writes it back unchanged when it is saved (it uses the blackboard of the
            tree running it); the tree itself keeps its blackboard in a blackboard file.
    """

    def __init__(
        self,
        view: TreeView,
        path: str | None = None,
        config: TreeConfig | None = None,
        legacy_blackboard: Any = None,
    ):
        self.view = view
        self.path = path
        self.config = config if config is not None else TreeConfig()
        self.modified = False
        self.legacy_blackboard = legacy_blackboard

    @property
    def folder(self) -> str | None:
        """The folder of the tree file (relative sub-tree file names are relative to it)."""
        return os.path.dirname(self.path) if self.path else None

    def data(self, problems: list[str] | None = None, quiet: bool = False, check_raw: bool = False) -> dict:
        """The tree as shown, as the JSON-compatible data of its tree file (see :func:`tree_io.tree_data`)."""
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
