"""Build and execute py_trees behavior trees graphically with a PySide6 widget.

Quick start::

    from PySide6.QtWidgets import QApplication
    from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget

    class SayHello(LeafNodeWidget):
        _title = "Say Hello"
        _fields = {"name": "world"}

        def OnRun(self, tree):
            tree.SetEntry(f"Hello {self.GetField('name')}!", "greeting")
            return True

    app = QApplication([])
    widget = BehaviorTreeWidget(node_types=[SayHello])
    widget.show()
    app.exec()
"""

import os as _os
import sys as _sys

# py_trees evaluates ``sys.stdout.encoding`` when it is imported. Under pythonw.exe
# (GUI apps without a console) sys.stdout / sys.stderr are None and that import
# would fail, so give them a harmless sink first.
if _sys.stdout is None:
    _sys.stdout = open(_os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if _sys.stderr is None:
    _sys.stderr = open(_os.devnull, "w", encoding="utf-8")  # noqa: SIM115

from py_trees.common import Status

from ._runctx import ExecutionCancelled
from .blackboard import BlackboardStore, BlackboardView
from .blackboard_nodes import EvaluationNodeWidget, SetNodeWidget
from .config import TreeConfig
from .control_nodes import PauseNodeWidget, RestartNodeWidget, StopNodeWidget
from .groups import GroupNodeWidget
from .network_nodes import UdpMessageNodeWidget
from .nodes import (
    CompositeNodeWidget,
    LeafNodeWidget,
    NegationNodeWidget,
    NodeStatus,
    NodeWidget,
    RootNodeWidget,
    UnknownLeafNodeWidget,
)
from .notes import BoxNodeWidget, NotesNodeWidget
from .print_node import PrintMessageNodeWidget
from .serialization import TreeFileError, tree_file_paths
from .subtree import SubTreeNodeWidget
from .tree_log import LogLevel, TreeLogger, flush_logs
from .widget import BehaviorTreeWidget, register_node_type, registered_node_types

__version__ = "0.9.0"

__all__ = [
    "BehaviorTreeWidget",
    "BlackboardStore",
    "BlackboardView",
    "BoxNodeWidget",
    "CompositeNodeWidget",
    "EvaluationNodeWidget",
    "ExecutionCancelled",
    "GroupNodeWidget",
    "LeafNodeWidget",
    "LogLevel",
    "NegationNodeWidget",
    "NodeStatus",
    "NodeWidget",
    "NotesNodeWidget",
    "PauseNodeWidget",
    "PrintMessageNodeWidget",
    "RestartNodeWidget",
    "RootNodeWidget",
    "SetNodeWidget",
    "Status",
    "StopNodeWidget",
    "SubTreeNodeWidget",
    "TreeConfig",
    "TreeFileError",
    "TreeLogger",
    "UdpMessageNodeWidget",
    "UnknownLeafNodeWidget",
    "__version__",
    "flush_logs",
    "register_node_type",
    "registered_node_types",
    "tree_file_paths",
]
