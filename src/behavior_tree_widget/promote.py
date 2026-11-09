"""Promote to Sub-Tree: write the contents of a group to a tree file and replace the group by a
Sub-Tree node running that file (see ``BehaviorTreeWidget.PromoteGroupToSubTree``)."""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF

from ._common import file_key
from .config import TreeConfig
from .geometry import clamp_to_scene
from .groups import GroupNodeWidget
from .nodes import SEQUENCE, CompositeNodeWidget, NodeWidget, RootNodeWidget, relayout
from .notes import BoxNodeWidget
from .registry import ROOT
from .serialization import FORMAT_NAME, FORMAT_VERSION, JSON_EXTENSION, TREE_FILE_SUFFIX, write_json_atomic
from .subtree import SubTreeNodeWidget
from .tree_io import node_data

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import TreeView

log = logging.getLogger("behavior_tree_widget")

__all__ = [
    "INVALID_FILE_NAME_CHARACTERS",
    "Promotion",
    "plan_promotion",
    "replace_group",
    "subtree_path",
    "write_subtree",
]

#: Characters a group name cannot contain when it is promoted to a sub-tree (it names a file).
INVALID_FILE_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def subtree_path(group: GroupNodeWidget, shown_path: str | None, open_paths: Iterable[str]) -> str:
    """The sub-tree file ``<group name>_BT.json`` a group is promoted to, next to ``shown_path``
    (the file of the tree shown). ``open_paths`` are the files of the tree and of the trees
    containing it, which the group must not replace.

    Raises:
        RuntimeError: the tree has no file yet.
        ValueError: the group name cannot be used as a file name.
    """
    if not shown_path:
        raise RuntimeError("the tree has no file yet: create, load or save it first")
    name = group.GetTitle().strip()
    if not name:
        raise ValueError("the group has no name; type one in its title bar")
    if INVALID_FILE_NAME_CHARACTERS.search(name) or name in (".", "..") or name.endswith("."):
        raise ValueError(
            f"the group name {name!r} cannot be used as a file name: it must not contain "
            '< > : " / \\ | ? * and must not end with a dot'
        )
    path = os.path.join(os.path.dirname(shown_path), f"{name}{TREE_FILE_SUFFIX}{JSON_EXTENSION}")
    if file_key(path) in {file_key(open_path) for open_path in open_paths}:
        raise ValueError(
            f"the group name {name!r} names the file of this tree or of a tree containing it; rename the group"
        )
    return path


@dataclass
class Promotion:
    """What promoting a group changes (see :func:`plan_promotion`)."""

    group: GroupNodeWidget
    name: str  # the group name: the title of the Sub-Tree node and the name of its file
    members: list[NodeWidget]  # everything lying completely inside the group (notes and groups too)
    nodes: list[NodeWidget]  # the members that are part of the behavior tree
    tops: list[NodeWidget]  # the nodes whose parent lies outside the group, left to right
    parent: NodeWidget | None  # the node outside the group the top nodes hang from
    root_children: list[NodeWidget]  # the children of the sub-tree's Root


def plan_promotion(view: TreeView, group: GroupNodeWidget) -> Promotion:
    """Check that ``group`` of ``view`` can be promoted and work out what changes.

    Raises:
        ValueError: the group cannot be promoted; the message says why.
    """
    members = view.group_contents(group)
    nodes = [node for node in members if not isinstance(node, BoxNodeWidget)]
    if any(isinstance(node, RootNodeWidget) for node in nodes):
        raise ValueError("the Root lies inside the group; the Root cannot be part of a sub-tree")
    if not nodes:
        raise ValueError("no node lies completely inside the group; enlarge the group or move nodes into it")
    inside = set(nodes)
    for node in nodes:
        outside = [child for child in node.GetChildren() if child not in inside]
        if outside:
            raise ValueError(
                f"node {node.GetTitle()!r} has children outside the group ({outside[0].GetTitle()!r}); "
                "move them into the group or disconnect them"
            )
    tops = sorted((node for node in nodes if node.GetParent() not in inside), key=lambda node: node._order_key())
    parents = list(dict.fromkeys(node.GetParent() for node in tops if node.GetParent() is not None))
    if len(parents) > 1:
        raise ValueError(
            "the nodes in the group are connected to more than one node outside it "
            f"({', '.join(repr(parent.GetTitle()) for parent in parents)}); a Sub-Tree node has one parent"
        )
    parent = parents[0] if parents else None
    root_children = [node for node in tops if node.GetParent() is parent] if parent is not None else tops
    if parent is not None:
        # One node takes their place among the parent's children: they must run one after the other.
        siblings = parent.GetChildren()
        places = [siblings.index(node) for node in root_children]
        between = [node for node in siblings[min(places) : max(places) + 1] if node not in inside]
        if between:
            raise ValueError(
                f"node {between[0].GetTitle()!r} outside the group runs between the group's nodes "
                f"({parent.GetTitle()!r} runs its children left to right); move it into the group "
                "or out of their way"
            )
    return Promotion(group, group.GetTitle().strip(), members, nodes, tops, parent, root_children)


def _united(rects: Iterable[QRectF]) -> QRectF:
    area = QRectF()
    for rect in rects:
        area = area.united(rect)
    return area


def _subtree_document(
    promotion: Promotion, view: TreeView, config: TreeConfig, problems: list[str], check_raw: bool
) -> dict:
    """The tree file of the sub-tree: a new Root over the group's top nodes, and the group's contents.

    The Root takes the type and memory of the parent the top nodes hang from, so the tree runs as
    before; it is placed above the area of the nodes.
    """
    parent = promotion.parent
    if isinstance(parent, CompositeNodeWidget):
        composite, memory = parent.GetCompositeType(), parent.GetMemory()
    else:
        composite, memory = SEQUENCE, config.default_memory
    area = _united(node.scene_rect() for node in promotion.nodes)
    children_area = _united(node.scene_rect() for node in promotion.root_children)
    probe = RootNodeWidget(composite, memory)  # its width centres the Root over its children
    relayout(probe)
    root_width = probe.width()
    probe.deleteLater()
    root_id = uuid.uuid4().hex
    root_item = {
        "id": root_id,
        "type": ROOT,
        "title": ROOT,
        "x": round(children_area.center().x() - root_width / 2.0, 2),
        "y": round(area.top() - 160.0, 2),
        "composite": composite,
        "memory": memory,
    }
    connections = [{"parent": root_id, "child": node.GetId()} for node in promotion.root_children]
    connections += [
        {"parent": node.GetId(), "child": child.GetId()} for node in promotion.nodes for child in node.GetChildren()
    ]
    file_config = TreeConfig(tick_interval_ms=config.tick_interval_ms, default_memory=config.default_memory)
    center = area.united(QRectF(root_item["x"], root_item["y"], root_width, 1.0)).center()
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "config": file_config.to_dict(),
        "view": {"zoom": round(view.zoom(), 4), "center": [round(center.x(), 2), round(center.y(), 2)]},
        "nodes": [
            root_item,
            *(node_data(node, problems, quiet=True, check_raw=check_raw) for node in promotion.members),
        ],
        "connections": connections,
    }


def write_subtree(path: str, promotion: Promotion, view: TreeView, config: TreeConfig) -> list[str]:
    """Write the sub-tree file of ``promotion`` to ``path``; returns the values left out of it.

    No blackboard file is written: a sub-tree uses the blackboard of the tree that runs it.

    Raises:
        OSError: the file could not be written.
        ValueError: the group's nodes hold data nested too deeply to be written.
    """
    problems: list[str] = []
    for check_raw in (False, True):
        problems.clear()
        try:
            write_json_atomic(path, _subtree_document(promotion, view, config, problems, check_raw))
            break
        except RecursionError:
            if check_raw:
                raise ValueError("the group's nodes hold data nested too deeply to be written") from None
    return problems


def replace_group(view: TreeView, promotion: Promotion, file_name: str) -> SubTreeNodeWidget:
    """Replace the group and its contents in ``view`` by a Sub-Tree node running ``file_name``.

    The node takes the place of the leftmost top node (connected to their parent where it was),
    is titled with the group name and is selected.
    """
    anchor = promotion.root_children[0] if promotion.root_children else promotion.tops[0]
    anchor_rect = anchor.scene_rect()
    for node in [promotion.group, *promotion.members]:
        if node in view.nodes():
            view.remove_node(node)
    subtree = SubTreeNodeWidget()
    subtree._apply_title(promotion.name)
    subtree.SetFile(file_name)
    item = view.add_node(subtree, anchor_rect.topLeft())
    subtree._notify_geometry()
    item.setPos(clamp_to_scene(QPointF(anchor_rect.center().x() - subtree.width() / 2.0, anchor_rect.top()))[0])
    if promotion.parent is not None:
        view.connect_nodes(promotion.parent, subtree)
    view.select_nodes([subtree])
    return subtree
