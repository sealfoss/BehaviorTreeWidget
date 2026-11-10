"""Translation of the node graph into a py_trees tree (used by ``TreeExecutor`` when execution starts).

* Root / Sequence / Selector nodes -> ``py_trees.composites.Sequence`` / ``Selector`` (children
  ordered left to right),
* Negation nodes -> ``py_trees.decorators.Inverter`` (a Negation without a child fails),
* Sub-Tree nodes -> :class:`~behavior_tree_widget.behaviours.SubTreeBehaviour` over the tree read
  from the node's file (its nodes are created behind the scenes, never shown, and use the
  blackboard of the tree being executed),
* leaf nodes -> :class:`~behavior_tree_widget.behaviours.LeafBehaviour`.
"""

from __future__ import annotations

import os
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import py_trees

from ._common import file_key
from .behaviours import FailingBehaviour, LeafBehaviour, SubTreeBehaviour
from .nodes import SEQUENCE, CompositeNodeWidget, LeafNodeWidget, NegationNodeWidget, NodeWidget, RootNodeWidget
from .serialization import TreeFileError
from .subtree import SubTreeNodeWidget
from .tree_log import LogLevel

if TYPE_CHECKING:  # pragma: no cover
    from .behaviours import LeafExecutor
    from .execution_host import ExecutionHost

__all__ = ["BuiltTree", "TreeBuilder"]

#: Where a hidden node of a sub-tree lies: the ids of the Sub-Tree nodes leading to it, then its own id.
Place = tuple[str, ...]


@dataclass
class BuiltTree:
    """A py_trees tree built from the node graph."""

    tree: py_trees.trees.BehaviourTree
    behaviours: dict[NodeWidget, py_trees.behaviour.Behaviour] = field(default_factory=dict)
    labels: dict[py_trees.behaviour.Behaviour, str] = field(default_factory=dict)  # how the log names each node
    nodes: dict[py_trees.behaviour.Behaviour, NodeWidget] = field(default_factory=dict)  # behaviours -> nodes
    subtree_nodes: list[NodeWidget] = field(default_factory=list)  # hidden nodes created for Sub-Tree nodes


class TreeBuilder:
    """Builds the py_trees tree of a Root (see the module docs).

    ``subtree_nodes`` lists the hidden nodes created for Sub-Tree nodes so far: the caller deletes
    them when it no longer needs the tree, also when :meth:`build` raised.
    """

    def __init__(self, executor: LeafExecutor, host: ExecutionHost, subtree_calls: dict[Place, Future]):
        """``subtree_calls``: interrupted ``OnRun`` calls of released hidden nodes that are still
        running, by place; the node built for the same place waits for its call."""
        self._executor = executor
        self._host = host
        self._subtree_calls = subtree_calls
        self.behaviours: dict[NodeWidget, py_trees.behaviour.Behaviour] = {}
        self.labels: dict[py_trees.behaviour.Behaviour, str] = {}
        self.nodes: dict[py_trees.behaviour.Behaviour, NodeWidget] = {}
        self.subtree_nodes: list[NodeWidget] = []

    def build(self, root: RootNodeWidget, tree_file: str | None) -> BuiltTree:
        """The py_trees tree of ``root``; ``tree_file``: the file of its tree (a sub-tree that runs
        that file again is refused instead of being built forever)."""
        chain = (file_key(tree_file),) if tree_file else ()
        tree = py_trees.trees.BehaviourTree(root=self._build(root, chain, None))
        return BuiltTree(tree, self.behaviours, self.labels, self.nodes, self.subtree_nodes)

    def _build(
        self,
        node: NodeWidget,
        chain: tuple[str, ...],
        leaves: list | None,
        scope: tuple[str, ...] = (),
        titles: tuple[str, ...] = (),
    ) -> py_trees.behaviour.Behaviour:
        """``chain``: the sub-tree files ``node`` lies in; ``leaves`` collects a sub-tree's leaves;
        ``scope``: the ids of the Sub-Tree nodes leading to ``node``; ``titles``: their titles."""
        behaviour: py_trees.behaviour.Behaviour
        if isinstance(node, CompositeNodeWidget):
            composite = (
                py_trees.composites.Sequence if node.GetCompositeType() == SEQUENCE else py_trees.composites.Selector
            )
            behaviour = composite(
                name=node.GetTitle(),
                memory=node.GetMemory(),
                children=[self._build(child, chain, leaves, scope, titles) for child in node.GetChildren()],
            )
        elif isinstance(node, NegationNodeWidget):
            child = node.GetChild()
            if child is not None:
                behaviour = py_trees.decorators.Inverter(
                    name=node.GetTitle(), child=self._build(child, chain, leaves, scope, titles)
                )
            else:
                behaviour = FailingBehaviour(
                    node.GetTitle(), "a Negation needs a child: connect the node whose result it inverts"
                )
        elif isinstance(node, SubTreeNodeWidget):
            behaviour = self._build_subtree(node, chain, scope, titles)
        elif isinstance(node, LeafNodeWidget):
            behaviour = LeafBehaviour(node, self._executor)
        else:
            raise TypeError(f"unsupported node type {type(node).__name__}")
        label = "".join(f"'{title}' > " for title in titles) + f"'{node.GetTitle()}' ({node.GetTypeName()})"
        self.behaviours[node] = behaviour
        self.labels[behaviour] = label
        self.nodes[behaviour] = node
        if isinstance(behaviour, FailingBehaviour):
            self._executor.report(LogLevel.WARNING, f"{label}: {behaviour.error}")
        if leaves is not None and isinstance(behaviour, (LeafBehaviour, FailingBehaviour, SubTreeBehaviour)):
            leaves.append(behaviour)
        return behaviour

    def _build_subtree(
        self, node: SubTreeNodeWidget, chain: tuple[str, ...], scope: tuple[str, ...], titles: tuple[str, ...]
    ) -> py_trees.behaviour.Behaviour:
        try:
            path = node.GetResolvedFile()
            if path is None:
                raise TreeFileError("no sub-tree file is chosen (File)")
            key = file_key(path)
            if key in chain:
                raise TreeFileError(
                    f"'{os.path.basename(path)}' contains this Sub-Tree node itself "
                    "(directly or through other sub-trees), which would never end"
                )
            sub_root, nodes = self._host.load_subtree(path)
        except (TreeFileError, OSError) as error:
            return FailingBehaviour(node.GetTitle(), f"the sub-tree cannot be run: {error}")
        self.subtree_nodes.extend(nodes)
        folder = os.path.dirname(path)
        inner_scope = (*scope, node.GetId())
        for inner in nodes:
            if isinstance(inner, SubTreeNodeWidget):
                inner._base_dir = folder  # their file names are relative to this sub-tree's file
            place = (*inner_scope, inner.GetId())
            inner._subtree_place = place
            call = self._subtree_calls.get(place)
            if call is not None and not call.done():
                inner._worker_future = call  # an interrupted call of the previous run: wait for it
        leaves: list = []
        built = self._build(sub_root, (*chain, key), leaves, inner_scope, (*titles, node.GetTitle()))
        return SubTreeBehaviour(node.GetTitle(), built, leaves)
