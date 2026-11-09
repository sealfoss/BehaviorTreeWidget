"""Turning tree file data into nodes and nodes back into tree file data.

The functions here do not depend on a BehaviorTreeWidget: nodes are created through a
:class:`~behavior_tree_widget.registry.NodeTypeRegistry`, and shown trees are read from and
written to a :class:`~behavior_tree_widget.canvas.TreeView`. Loading is done in two steps:
:func:`prepare_tree` validates the data and creates the nodes into a :class:`TreePlan` (nothing
visible changes, and a failure leaves nothing behind), then :func:`install_nodes` adds them to a
view.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QPointF

from .blackboard import BlackboardStore
from .canvas import clamp_to_scene, iter_subtree
from .config import TreeConfig
from .nodes import COMPOSITE_TYPES, SEQUENCE, NodeWidget, RootNodeWidget, UnknownLeafNodeWidget, relayout
from .registry import ROOT, NodeTypeRegistry
from .serialization import FORMAT_NAME, FORMAT_VERSION, TreeFileError, decode_value, encode_value, is_finite_number

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import TreeView

log = logging.getLogger("behavior_tree_widget")

__all__ = [
    "TreePlan",
    "install_nodes",
    "load_detached_tree",
    "new_tree_data",
    "node_data",
    "prepare_connections",
    "prepare_nodes",
    "prepare_tree",
    "tree_data",
]

#: Keys every saved node has; a placeholder keeps all other keys of its saved node as they were.
STANDARD_NODE_KEYS = frozenset({"id", "type", "title", "x", "y", "fields", "field_selections"})


@dataclass
class TreePlan:
    """A validated tree file, ready to be installed in a view."""

    nodes: list[tuple[NodeWidget, QPointF]] = field(default_factory=list)
    connections: list[tuple[str, str]] = field(default_factory=list)
    blackboard: list[tuple[str, str, Any]] = field(default_factory=list)
    resets: set[str] = field(default_factory=set)  # blackboard entries whose Reset flag is set
    config: TreeConfig = field(default_factory=TreeConfig)
    zoom: float | None = None
    center: QPointF | None = None
    blackboard_path: str | None = None  # the blackboard file of the tree
    new_blackboard: bool = False  # NewTree: the blackboard file was just replaced by an empty one

    def discard(self) -> None:
        """Delete the nodes created for the plan (it will not be installed)."""
        for node, _ in self.nodes:
            if shiboken6.isValid(node):
                node.deleteLater()
        self.nodes = []


# ====================================================================== reading
def prepare_tree(data: dict, problems: list[str], registry: NodeTypeRegistry, blackboard: Any = None) -> TreePlan:
    """Validate ``data`` (a tree file) and the ``blackboard`` items (of its blackboard file; None:
    none) and create (but do not add) its nodes. Problems in otherwise valid data are appended to
    ``problems``. Nothing visible changes."""
    plan = TreePlan(config=TreeConfig.from_dict(data.get("config")))
    try:
        prepare_nodes(plan, data.get("nodes", []), problems, registry)
        prepare_connections(plan, data.get("connections", []), problems)
        plan.blackboard, plan.resets, blackboard_problems = BlackboardStore.parse_items(
            [] if blackboard is None else blackboard
        )
        problems.extend(blackboard_problems)
        plan.zoom, plan.center = _view_state(data.get("view"))
    except Exception:
        plan.discard()
        raise
    return plan


def _view_state(view: Any) -> tuple[float | None, QPointF | None]:
    """The saved zoom and view centre (None for values that are missing or invalid)."""
    if not isinstance(view, dict):
        return None, None
    zoom = view.get("zoom")
    center = view.get("center")
    zoom = float(zoom) if is_finite_number(zoom) and zoom > 0 else None
    if isinstance(center, list) and len(center) == 2 and all(is_finite_number(value) for value in center):
        return zoom, QPointF(float(center[0]), float(center[1]))
    return zoom, None


def prepare_nodes(
    plan: TreePlan, items: Any, problems: list[str], registry: NodeTypeRegistry, with_root: bool = True
) -> None:
    """Create the nodes of ``items`` into ``plan``. ``with_root`` False: no Root is created (Root
    items are skipped and none is added when missing), as for pasted nodes."""
    seen_ids: set[str] = set()
    root_seen = False
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"node {index} is not an object and was skipped")
            continue
        node_id = item.get("id")
        type_name = item.get("type")
        saved_title = item.get("title")
        # Messages name nodes by their title (ids are internal).
        label = f"{saved_title!r} (#{index + 1})" if isinstance(saved_title, str) and saved_title else f"#{index + 1}"
        if not isinstance(node_id, str) or not node_id or node_id in seen_ids:
            problems.append(f"node {label} has a missing or duplicate id and was skipped")
            continue
        if not isinstance(type_name, str) or not type_name:
            problems.append(f"node {label} has no type and was skipped")
            continue
        if type_name == ROOT:
            if root_seen or not with_root:
                problems.append(f"extra root node {label} was skipped")
                continue
            root_seen = True
            composite = item.get("composite")
            node: NodeWidget = RootNodeWidget(composite if composite in COMPOSITE_TYPES else SEQUENCE)
        else:
            node, problem = registry.create_saved(type_name, item, label)
            if problem is not None:
                problems.append(problem)
        plan.nodes.append((node, QPointF(0.0, 0.0)))  # registered for cleanup before anything can fail
        seen_ids.add(node_id)
        node._id = node_id
        position = _apply_saved_node_data(node, item, label, problems)
        plan.nodes[-1] = (node, position)
    if not root_seen and with_root:
        problems.append("the file has no root node; a new root was created")
        plan.nodes.insert(0, (RootNodeWidget(), QPointF(0.0, 0.0)))


def _apply_saved_node_data(node: NodeWidget, item: dict, label: str, problems: list[str]) -> QPointF:
    """Give ``node`` the saved data of ``item`` (fields, node-specific data, title) and return its
    position (moved into the drawing area if needed)."""
    extra = dict(item)
    fields = extra.get("fields")
    placeholder = isinstance(node, UnknownLeafNodeWidget)
    if isinstance(fields, dict):
        decoded = {}
        for key, value in fields.items():
            try:
                decoded[key] = decode_value(value)
            except TreeFileError as error:
                if placeholder:
                    node._raw_fields[key] = value
                    problems.append(f"field {key!r} of node {label} could not be read ({error}); it is kept unchanged")
                else:
                    problems.append(f"field {key!r} of node {label} could not be read ({error}); default kept")
        extra["fields"] = decoded
    if placeholder:
        node._raw_extra = {key: value for key, value in item.items() if key not in STANDARD_NODE_KEYS}
    node._load_dict(extra)
    title = item.get("title")
    if isinstance(title, str) and title:
        node._apply_title(title, runtime=False)
    x, y = item.get("x", 0.0), item.get("y", 0.0)
    if not (is_finite_number(x) and is_finite_number(y)):
        problems.append(f"node {label} has an invalid position")
        x, y = 0.0, 0.0
    position, clamped = clamp_to_scene(QPointF(float(x), float(y)))
    if clamped:
        problems.append(f"node {label} was outside the drawing area and was moved inside it")
    return position


def prepare_connections(plan: TreePlan, items: Any, problems: list[str]) -> None:
    """Validate the saved connections against the nodes of ``plan``; invalid ones are skipped."""
    by_id = {node.GetId(): node for node, _ in plan.nodes}
    parent_of: dict[str, str] = {}
    child_count: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"connection {index} is not an object and was skipped")
            continue
        parent_id, child_id = item.get("parent"), item.get("child")
        parent = by_id.get(parent_id) if isinstance(parent_id, str) else None
        child = by_id.get(child_id) if isinstance(child_id, str) else None
        if parent is None or child is None:
            problems.append(f"connection {index} refers to a missing node and was skipped")
            continue
        if child_id in parent_of:
            problems.append(f"connection {index}: node {child.GetTitle()!r} already has a parent; skipped")
            continue
        if parent is child or not parent.HasChildConnections() or not child.HasParentConnection():
            problems.append(f"connection {index} ({parent.GetTitle()!r} -> {child.GetTitle()!r}) is invalid; skipped")
            continue
        limit = parent.MaxChildren()
        if limit is not None and child_count.get(parent_id, 0) >= limit:
            problems.append(
                f"connection {index}: node {parent.GetTitle()!r} accepts only {limit} "
                f"{'child' if limit == 1 else 'children'}; skipped"
            )
            continue
        ancestor: str | None = parent_id
        while ancestor is not None and ancestor != child_id:
            ancestor = parent_of.get(ancestor)
        if ancestor == child_id:
            problems.append(f"connection {index} ({parent.GetTitle()!r} -> {child.GetTitle()!r}) creates a cycle; skipped")
            continue
        parent_of[child_id] = parent_id
        child_count[parent_id] = child_count.get(parent_id, 0) + 1
        plan.connections.append((parent_id, child_id))


def install_nodes(view: TreeView, plan: TreePlan) -> None:
    """Add the nodes and connections of ``plan`` to ``view`` (the plan gives up its nodes)."""
    by_id: dict[str, NodeWidget] = {}
    nodes, plan.nodes = plan.nodes, []
    index = 0
    try:
        for index, (node, pos) in enumerate(nodes):
            view.add_node(node, pos)
            by_id[node.GetId()] = node
    except Exception:
        for node, _ in nodes[index:]:
            if shiboken6.isValid(node) and node._item is None:
                node.deleteLater()
        raise
    for parent_id, child_id in plan.connections:
        view.connect_nodes(by_id[parent_id], by_id[child_id])


def load_detached_tree(data: dict, registry: NodeTypeRegistry, owner: Any, name: str) -> tuple[RootNodeWidget, list[NodeWidget]]:
    """Create the nodes of the tree file ``data`` without showing them: its Root and the nodes
    connected below it, linked as in the file (the nodes of a Sub-Tree node being executed).

    Children keep the order of the file (left to right by their saved positions). The nodes belong
    to ``owner`` (their ``GetTree()``), so they use its blackboard. The caller deletes them.
    ``name`` names the file in messages.

    Raises:
        TreeFileError: the nodes cannot be created.
    """
    problems: list[str] = []
    plan = TreePlan()
    try:
        prepare_nodes(plan, data.get("nodes", []), problems, registry)
        prepare_connections(plan, data.get("connections", []), problems)
    except Exception as error:  # noqa: BLE001
        plan.discard()
        raise TreeFileError(f"'{name}' could not be loaded: {error}") from error
    prepared, plan.nodes = plan.nodes, []
    try:
        by_id = {node.GetId(): node for node, _ in prepared}
        for node, position in prepared:
            node._tree = owner
            relayout(node)  # its size gives its place among its siblings, as in a view
            node._detached_order = (position.x() + node.width() / 2.0, position.y())
        for parent_id, child_id in plan.connections:
            by_id[parent_id]._link(by_id[child_id])
        root = next(node for node, _ in prepared if isinstance(node, RootNodeWidget))
        nodes = iter_subtree(root)
    except Exception as error:  # noqa: BLE001
        for node, _ in prepared:
            if shiboken6.isValid(node):
                node.deleteLater()
        raise TreeFileError(f"'{name}' could not be loaded: {error}") from error
    kept = set(nodes)
    for node, _ in prepared:
        if node not in kept:
            node.deleteLater()  # notes, groups and nodes not connected to the Root
    for problem in problems:
        log.warning("sub-tree %s: %s", name, problem)
    return root, nodes


# ====================================================================== writing
def new_tree_data() -> dict:
    """The tree file of a new tree: default options and a Root only."""
    config = TreeConfig()
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "config": config.to_dict(),
        "nodes": [
            {
                "id": uuid.uuid4().hex,
                "type": ROOT,
                "title": ROOT,
                "x": 0.0,
                "y": 0.0,
                "composite": SEQUENCE,
                "memory": config.default_memory,
            }
        ],
        "connections": [],
    }


def tree_data(
    view: TreeView,
    config: TreeConfig,
    problems: list[str] | None = None,
    quiet: bool = False,
    check_raw: bool = False,
) -> dict:
    """The tree shown in ``view`` with options ``config`` as the JSON-compatible data of a tree file.

    Values that cannot be written are skipped and described in ``problems``; ``quiet`` suppresses
    the log messages (for internal snapshots); ``check_raw`` also skips data of unknown node types
    that json cannot write (see :func:`node_data`).
    """
    problems = problems if problems is not None else []
    nodes = view.nodes()
    connections = [
        {"parent": node.GetId(), "child": child.GetId()} for node in nodes for child in node.GetChildren()
    ]
    center = view.view_center()
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "config": config.to_dict(),
        "view": {"zoom": round(view.zoom(), 4), "center": [round(center.x(), 2), round(center.y(), 2)]},
        "nodes": [node_data(node, problems, quiet, check_raw) for node in nodes],
        "connections": connections,
    }


def json_writable(value: Any) -> bool:
    """Whether ``value`` (kept raw from a file) can be written by json inside a tree file."""
    for _ in range(16):  # room for the document around the value
        value = {"": value}
    try:
        json.dumps(value, indent=2, ensure_ascii=False)
    except RecursionError:
        return False
    return True


def node_data(node: NodeWidget, problems: list[str], quiet: bool = False, check_raw: bool = False) -> dict:
    """One node (in a view) as JSON-compatible data: an item of a tree file's "nodes" list.

    Field values that cannot be written as JSON are left out and described in ``problems``. A
    placeholder keeps the data it could not interpret as it was read; with ``check_raw``, such data
    that json cannot write (nested too deeply) is left out too.
    """
    warn = (lambda message: None) if quiet else log.warning

    def skip(message: str) -> None:
        warn(message)
        problems.append(message)

    def writable(what: str, value: Any) -> bool:
        if not check_raw or json_writable(value):
            return True
        skip(f"{what} of node {node.GetTitle()!r} was not saved: it is nested too deeply to be written")
        return False

    pos = node._item.pos()
    entry: dict[str, Any] = {
        "id": node.GetId(),
        "type": node.GetTypeName(),
        "title": node.GetTitle(),
        "x": round(pos.x(), 2),
        "y": round(pos.y(), 2),
    }
    try:
        extra = node._to_dict()
    except Exception as error:  # noqa: BLE001 - user node data
        problems.append(f"node {node.GetTitle()!r}: its data could not be read ({error})")
        extra = {}
    if isinstance(extra.get("fields"), dict):
        fields = {}
        for key, value in extra["fields"].items():
            if not isinstance(key, str):
                skip(f"field {key!r} of node {node.GetTitle()!r} was not saved: field names must be str")
                continue
            try:
                fields[key] = encode_value(value)
            except TypeError as error:
                skip(f"field {key!r} of node {node.GetTitle()!r} was not saved: {error}")
        extra["fields"] = fields
        selections = extra.get("field_selections")
        if isinstance(selections, dict):
            extra["field_selections"] = {k: v for k, v in selections.items() if k in fields}
    if isinstance(node, UnknownLeafNodeWidget):
        _keep_raw_data(node, extra, writable)
    entry.update(extra)
    return entry


def _keep_raw_data(node: UnknownLeafNodeWidget, extra: dict, writable) -> None:
    """Put the data a placeholder could not interpret back into its saved form, as it was read."""
    extra.setdefault("fields", {}).update(
        {key: value for key, value in node._raw_fields.items() if writable(f"field {key!r}", value)}
    )
    selections = {  # as saved ...
        key: value for key, value in node._raw_selections.items() if writable(f"selection of field {key!r}", value)
    }
    current = extra.get("field_selections") or {}
    for key in node._edited_selections:  # ... except those the user changed
        if key in current:
            selections[key] = current[key]
    if selections:
        extra["field_selections"] = selections
    else:
        extra.pop("field_selections", None)
    for key, value in node._raw_extra.items():
        if key not in extra and writable(f"data {key!r}", value):
            extra[key] = value
