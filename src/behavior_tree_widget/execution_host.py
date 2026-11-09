"""What the executor needs from the widget whose tree it executes.

:class:`ExecutionHost` lists it; :class:`WidgetExecutionHost` provides it for a
:class:`~behavior_tree_widget.BehaviorTreeWidget`. The executor (``TreeExecutor``) only uses the
host, so the widget's internals can change without touching the executor, and the executor
can be tested against a stand-in host.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from .blackboard import BlackboardStore
    from .config import TreeConfig
    from .nodes import NodeWidget, RootNodeWidget
    from .tree_log import LogLevel
    from .widget import BehaviorTreeWidget

__all__ = ["ExecutionHost", "WidgetExecutionHost"]

#: ``(scope, node id)``: where a node of a sub-tree shown with Zoom In lies; ``scope`` holds the
#: ids of the Sub-Tree nodes zoomed into, from the tree itself to the sub-tree.
TwinKey = tuple[tuple[str, ...], str]


class ExecutionHost(Protocol):
    """What ``TreeExecutor`` needs from the widget whose tree it executes.

    Every method may raise ``RuntimeError`` once the widget has been deleted.
    """

    def tree_widget(self) -> "BehaviorTreeWidget":
        """The object passed to the nodes' ``OnStart`` / ``OnRun`` / ``OnTerminate`` as ``tree``."""

    def root(self) -> "RootNodeWidget | None":
        """The Root of the tree that is executed: the tree itself, also while a sub-tree is shown."""

    def tree_file(self) -> str | None:
        """The file of the tree that is executed (None before it was saved, created or loaded)."""

    def config(self) -> "TreeConfig":
        """The execution options."""

    def blackboard(self) -> "BlackboardStore":
        """The blackboard the nodes use."""

    def log(self, level: "LogLevel", message: str) -> None:
        """Write ``message`` to the tree's log (never blocks)."""

    def log_enabled(self, level: "LogLevel") -> bool:
        """Whether a message of ``level`` would be written to the tree's log now."""

    def load_subtree(self, path: str) -> "tuple[RootNodeWidget, list[NodeWidget]]":
        """Create the (hidden) nodes of the tree file ``path`` for a Sub-Tree node: its Root and the
        nodes connected below it. Raises TreeFileError or OSError when the file cannot be used."""

    def shown_nodes(self) -> "list[NodeWidget]":
        """Every node shown: the tree's and those of the sub-trees shown with Zoom In."""

    def shown_twins(self) -> "dict[TwinKey, NodeWidget]":
        """The nodes of the sub-trees shown with Zoom In, by place (empty while none is shown)."""

    def shown_twins_key(self) -> object:
        """A value that changes whenever :meth:`shown_twins` would return other nodes."""


class WidgetExecutionHost:
    """:class:`ExecutionHost` of a :class:`~behavior_tree_widget.BehaviorTreeWidget` (held weakly)."""

    def __init__(self, widget: "BehaviorTreeWidget"):
        self._widget_ref = weakref.ref(widget)  # no reference cycle with the owning widget

    def _widget(self) -> "BehaviorTreeWidget":
        widget = self._widget_ref()
        if widget is None:
            raise RuntimeError("the BehaviorTreeWidget has been deleted")
        return widget

    def tree_widget(self) -> "BehaviorTreeWidget":
        return self._widget()

    def root(self) -> "RootNodeWidget | None":
        return self._widget()._main_root()

    def tree_file(self) -> str | None:
        return self._widget().GetFilePath()

    def config(self) -> "TreeConfig":
        return self._widget()._config

    def blackboard(self) -> "BlackboardStore":
        return self._widget()._blackboard

    def log(self, level: "LogLevel", message: str) -> None:
        self._widget()._log(level, message)

    def log_enabled(self, level: "LogLevel") -> bool:
        return self._widget()._tree_log.enabled(level)

    def load_subtree(self, path: str) -> "tuple[RootNodeWidget, list[NodeWidget]]":
        return self._widget()._load_subtree(path)

    def shown_nodes(self) -> "list[NodeWidget]":
        return self._widget()._all_nodes()

    def shown_twins(self) -> "dict[TwinKey, NodeWidget]":
        widget = self._widget()
        return widget._shown_twins() if widget._levels else {}

    def shown_twins_key(self) -> object:
        # The sub-trees shown (each zoom level and its view): nodes cannot be added to or removed
        # from them while the tree executes, so they change only by zooming in and going back.
        return tuple((id(level), id(level.view)) for level in self._widget()._levels)
