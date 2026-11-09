"""Copying nodes to the clipboard and pasting them (Ctrl+C / Ctrl+V).

Copied nodes are JSON in the format of a tree file's ``nodes`` and ``connections``, under the
MIME type :data:`CLIPBOARD_MIME_TYPE`, so they can be pasted into another BehaviorTreeWidget, also
in another application. Pasting works like loading those nodes: the copies get new ids, values
that cannot be written as JSON keep their defaults, and unknown node types become placeholders.
"""

from __future__ import annotations

import atexit
import json
import logging
import uuid
from typing import TYPE_CHECKING, Callable, Iterable

import shiboken6
from PySide6.QtCore import QByteArray, QMimeData
from PySide6.QtGui import QGuiApplication

from .canvas import clamp_to_scene, nearest_free_offset
from .groups import GroupNodeWidget, default_group_number
from .nodes import NodeWidget, RootNodeWidget
from .registry import ROOT, NodeTypeRegistry
from .serialization import FORMAT_VERSION
from .tree_io import TreePlan, node_data, prepare_connections, prepare_nodes

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import TreeView

log = logging.getLogger("behavior_tree_widget")

__all__ = ["CLIPBOARD_FORMAT", "CLIPBOARD_MIME_TYPE", "clipboard_data", "copy_nodes", "paste_nodes"]

#: Clipboard format of copied nodes: JSON holding "nodes" and "connections" as in tree files.
CLIPBOARD_MIME_TYPE = "application/x-behavior-tree-widget-nodes"
CLIPBOARD_FORMAT = "behavior_tree_widget_nodes"
#: Qt platforms with a system clipboard (others, e.g. offscreen, keep it inside the process).
NATIVE_CLIPBOARD_PLATFORMS = frozenset({"windows", "cocoa", "xcb", "wayland", "android", "ios"})
_clipboard_guard_installed = False


def _drop_copied_nodes() -> None:
    """At exit: remove copied nodes from a clipboard that lives inside the process."""
    if QGuiApplication.instance() is None:
        return
    try:
        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is not None and mime.hasFormat(CLIPBOARD_MIME_TYPE):
            clipboard.clear()
    except RuntimeError:  # the application is being destroyed
        pass


def _guard_clipboard_at_exit() -> None:
    """Platforms without a system clipboard (offscreen, minimal, ...) keep it inside the process,
    and PySide6 can crash while destroying such a clipboard holding copied nodes at exit (e.g. at
    the end of a test run). The nodes are dropped from it at exit; they could not outlive the
    process anyway. A system clipboard keeps them, so they can still be pasted after exit."""
    global _clipboard_guard_installed
    if _clipboard_guard_installed or QGuiApplication.platformName().split("-")[0] in NATIVE_CLIPBOARD_PLATFORMS:
        return
    atexit.register(_drop_copied_nodes)
    _clipboard_guard_installed = True


def copy_nodes(view: TreeView, nodes: Iterable[NodeWidget]) -> bool:
    """Put ``nodes`` of ``view`` (except the Root) and the connections between them on the clipboard.

    Returns False, leaving the clipboard alone, when there is nothing to copy.
    """
    wanted = set(nodes)
    copied = [node for node in view.nodes() if node in wanted and not isinstance(node, RootNodeWidget)]
    if not copied:
        return False
    ids = {node.GetId() for node in copied}
    connections = [
        {"parent": node.GetId(), "child": child.GetId()}
        for node in copied
        for child in node.GetChildren()
        if child.GetId() in ids
    ]
    text = None
    problems: list[str] = []
    for check_raw in (False, True):
        # As when saving: data of unknown node types that json cannot write is left out.
        problems = []
        data = {
            "format": CLIPBOARD_FORMAT,
            "version": FORMAT_VERSION,
            "nodes": [node_data(node, problems, quiet=True, check_raw=check_raw) for node in copied],
            "connections": connections,
        }
        try:
            text = json.dumps(data, ensure_ascii=True)
            break
        except RecursionError:
            continue
    if text is None:
        log.warning("the selected nodes could not be copied")
        return False
    for problem in problems:
        log.warning("copy: %s", problem)  # e.g. a field value that cannot be written as JSON
    mime = QMimeData()
    mime.setData(CLIPBOARD_MIME_TYPE, QByteArray(text.encode("ascii")))
    QGuiApplication.clipboard().setMimeData(mime)
    _guard_clipboard_at_exit()
    return True


def clipboard_data() -> dict | None:
    """The nodes on the clipboard (a checked :func:`copy_nodes` document), or None."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None or not mime.hasFormat(CLIPBOARD_MIME_TYPE):
        return None
    try:
        data = json.loads(bytes(mime.data(CLIPBOARD_MIME_TYPE)).decode("utf-8"))
    except (ValueError, RecursionError) as error:  # also UnicodeDecodeError
        log.warning("the clipboard holds damaged node data: %s", error)
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("format") != CLIPBOARD_FORMAT
        or not isinstance(version, int)
        or isinstance(version, bool)
        or not 1 <= version <= FORMAT_VERSION
        or not isinstance(data.get("nodes"), list)
        or not isinstance(data.get("connections", []), list)
    ):
        log.warning("the clipboard holds node data this version cannot read")
        return None
    return data


def paste_nodes(view: TreeView, registry: NodeTypeRegistry, next_group_name: Callable[[], str]) -> list[NodeWidget]:
    """Paste the clipboard's nodes into ``view`` and return them (they become the selection).

    The copies keep their arrangement and the connections between them, and are moved together
    to the free area nearest to where the copied nodes were. A copy of a group with a default name
    gets the next default name (``next_group_name()``). Returns ``[]`` when the clipboard holds no
    nodes.
    """
    data = clipboard_data()
    if data is None:
        return []
    # New ids for the copies; the Root is never pasted.
    ids: dict[str, str] = {}
    items = []
    for item in data["nodes"]:
        if not isinstance(item, dict) or item.get("type") == ROOT:
            continue
        old = item.get("id")
        if isinstance(old, str) and old not in ids:
            ids[old] = uuid.uuid4().hex
            items.append({**item, "id": ids[old]})
    connections = [
        {"parent": ids[item["parent"]], "child": ids[item["child"]]}
        for item in data.get("connections", [])
        if isinstance(item, dict) and item.get("parent") in ids and item.get("child") in ids
    ]
    problems: list[str] = []
    plan = TreePlan()
    try:
        prepare_nodes(plan, items, problems, registry, with_root=False)
        prepare_connections(plan, connections, problems)
    except Exception:
        plan.discard()
        raise
    obstacles = [rect for rect in (node.scene_rect() for node in view.nodes()) if rect is not None]
    prepared, plan.nodes = plan.nodes, []
    pasted: list[NodeWidget] = []
    try:
        # Added where the copied nodes were (their size is known once added), then moved
        # together to the nearest free area.
        for node, pos in prepared:
            view.add_node(node, pos)
            pasted.append(node)
    except Exception:
        for node, _ in prepared[len(pasted):]:
            if shiboken6.isValid(node):
                node.deleteLater()
        raise
    offset = nearest_free_offset([node.scene_rect() for node in pasted], obstacles)
    for node in pasted:
        node._item.setPos(clamp_to_scene(node._item.pos() + offset)[0])
    by_id = {node.GetId(): node for node in pasted}
    for parent_id, child_id in plan.connections:
        view.connect_nodes(by_id[parent_id], by_id[child_id])
    # A copy of a group with a default name gets the next default name (group names are file names).
    for node in pasted:
        if isinstance(node, GroupNodeWidget) and default_group_number(node.GetTitle()) is not None:
            node._apply_title(next_group_name())
    view.select_nodes(pasted)
    for problem in problems:
        log.warning("paste: %s", problem)
    return pasted
