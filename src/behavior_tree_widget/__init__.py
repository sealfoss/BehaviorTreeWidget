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

from py_trees.common import Status  # noqa: E402

from ._runctx import ExecutionCancelled  # noqa: E402

from .blackboard import BlackboardStore, BlackboardView  # noqa: E402
from .blackboard_nodes import EvaluationNodeWidget, SetNodeWidget  # noqa: E402
from .config import TreeConfig  # noqa: E402
from .nodes import (  # noqa: E402
    CompositeNodeWidget,
    LeafNodeWidget,
    NegationNodeWidget,
    NodeStatus,
    NodeWidget,
    RootNodeWidget,
    UnknownLeafNodeWidget,
)
from .serialization import TreeFileError  # noqa: E402
from .widget import BehaviorTreeWidget, register_node_type, registered_node_types  # noqa: E402

__version__ = "0.2.0"

__all__ = [
    "BehaviorTreeWidget",
    "BlackboardStore",
    "BlackboardView",
    "CompositeNodeWidget",
    "EvaluationNodeWidget",
    "ExecutionCancelled",
    "LeafNodeWidget",
    "NegationNodeWidget",
    "NodeStatus",
    "NodeWidget",
    "RootNodeWidget",
    "SetNodeWidget",
    "Status",
    "TreeConfig",
    "TreeFileError",
    "UnknownLeafNodeWidget",
    "register_node_type",
    "registered_node_types",
    "__version__",
]
