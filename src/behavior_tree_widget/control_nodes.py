"""Built-in leaf nodes that control the execution of their own tree: Restart, Pause and Stop.

Each one issues its command when it is ticked and reports *Running*, so no node after it
runs in that tick (Execute, Pause, Stop and Reset issued during a tick take effect right
after it).
"""

from __future__ import annotations

from py_trees.common import Status

from .nodes import LeafNodeWidget


class RestartNodeWidget(LeafNodeWidget):
    """Restarts the tree: every node goes back to Ready and the next tick starts at the Root again."""

    TYPE_NAME = "Restart"
    _title = "Restart"
    RUN_IN_THREAD = False

    def _title_tooltip(self) -> str:
        return f"{self.TYPE_NAME} - restarts the tree from the Root"

    def OnRun(self, tree):
        tree.Reset()  # while running, Reset starts the tree over on the next tick
        return Status.RUNNING


class PauseNodeWidget(LeafNodeWidget):
    """Pauses the tree when it is reached; when Execute resumes the tree, the node succeeds."""

    TYPE_NAME = "Pause"
    _title = "Pause"
    RUN_IN_THREAD = False
    _pause_requested = False

    def _title_tooltip(self) -> str:
        return f"{self.TYPE_NAME} - pauses the tree here; it continues when Execute is pressed"

    def OnStart(self, tree) -> None:
        self._pause_requested = False

    def OnRun(self, tree):
        if self._pause_requested:
            return True  # resumed
        self._pause_requested = True
        tree.Pause()
        return Status.RUNNING


class StopNodeWidget(LeafNodeWidget):
    """Stops the tree, as the Stop button does: execution ends and every node goes back to Ready."""

    TYPE_NAME = "Stop"
    _title = "Stop"
    RUN_IN_THREAD = False

    def _title_tooltip(self) -> str:
        return f"{self.TYPE_NAME} - stops the tree, as the Stop button does"

    def OnRun(self, tree):
        tree.Stop()
        return Status.RUNNING
