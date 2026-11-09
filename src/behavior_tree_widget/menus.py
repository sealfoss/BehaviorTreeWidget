"""Context menus of the tree view (internal; built by :class:`~behavior_tree_widget.canvas.TreeView`).

The functions only build the menus. What their actions do are methods of the view
(``_add_node_from_menu``, ``_rename_node``, ``_set_composite_type``, ...), so that an
application, or a test, can replace them on the view.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF
from PySide6.QtGui import QActionGroup, QColor, QIcon, QPixmap
from PySide6.QtWidgets import QMenu

from .groups import GROUP_COLORS, GroupNodeWidget
from .nodes import COMPOSITE_TYPES, CompositeNodeWidget, NodeWidget, RootNodeWidget
from .notes import NotesNodeWidget
from .subtree import SubTreeNodeWidget

if TYPE_CHECKING:  # pragma: no cover
    from .canvas import TreeView
    from .items import ConnectionItem

__all__ = ["build_add_menu", "build_connection_menu", "build_node_menu", "color_icon"]


def build_add_menu(view: TreeView, scene_pos: QPointF, connect_to: tuple[NodeWidget, str] | None = None) -> QMenu:
    """Menu listing every available node type; choosing one adds it at ``scene_pos``.

    With ``connect_to`` (a node and its ``"parent"`` / ``"children"`` label) the new node is
    also connected to that label when the connection is possible.
    """
    menu = QMenu(view)
    menu.setObjectName("AddNodeMenu")
    title = menu.addAction("Add Node")
    title.setEnabled(False)
    owner = view._owner
    entries = owner._node_menu_entries() if owner is not None else []
    locked = view.is_locked()
    previous_group = None
    for group, type_name, label in entries:
        if previous_group is not None and group != previous_group:
            menu.addSeparator()
        previous_group = group
        action = menu.addAction(label)
        action.setObjectName(f"Add_{type_name}")
        action.setData(type_name)
        action.setEnabled(not locked)
        action.triggered.connect(
            lambda _checked=False, t=type_name, p=QPointF(scene_pos), c=connect_to: view._add_node_from_menu(t, p, c)
        )
    if locked:
        menu.addSeparator()
        note = menu.addAction("Stop execution to add nodes")
        note.setEnabled(False)
    return menu


def build_node_menu(view: TreeView, node: NodeWidget) -> QMenu:
    """Context menu of ``node``: rename (a note: edit its text), Zoom In (a Sub-Tree node), select
    children, composite options, group options (select contents, border color, promote to
    sub-tree) and delete."""
    menu = QMenu(view)
    menu.setObjectName("NodeMenu")
    locked = view.is_locked()
    if isinstance(node, GroupNodeWidget):
        _add_group_actions(view, menu, node)
    elif isinstance(node, NotesNodeWidget):
        edit = menu.addAction("Edit Text")
        edit.setObjectName("EditText")
        edit.triggered.connect(lambda _checked=False, n=node: view._edit_note(n))
    else:
        rename = menu.addAction("Rename...")
        rename.setObjectName("Rename")
        rename.triggered.connect(lambda _checked=False, n=node: view._rename_node(n))
    if isinstance(node, SubTreeNodeWidget):
        zoom = menu.addAction("Zoom In")
        zoom.setObjectName("ZoomIn")
        zoom.setToolTip("Show the tree in this node's file in place of this tree (Go Back returns here).")
        if not node.GetFile().strip():
            zoom.setEnabled(False)
            zoom.setText("Zoom In (choose a file first)")
        zoom.triggered.connect(lambda _checked=False, n=node: view._zoom_in(n))
    if node.HasChildConnections():
        select_children = menu.addAction("Select Children")
        select_children.setObjectName("SelectChildren")
        select_children.setEnabled(bool(node._children))
        select_children.setToolTip("Select this node and all of its descendants.")
        select_children.triggered.connect(lambda _checked=False, n=node: view._select_subtree(n))
    if isinstance(node, CompositeNodeWidget):
        menu.addSeparator()
        types = QActionGroup(menu)
        types.setExclusive(True)
        for composite_type in COMPOSITE_TYPES:
            action = menu.addAction(composite_type)
            action.setObjectName(f"Type_{composite_type}")
            action.setCheckable(True)
            action.setChecked(node.GetCompositeType() == composite_type)
            action.setEnabled(not locked)
            types.addAction(action)
            action.triggered.connect(lambda _checked=False, n=node, t=composite_type: view._set_composite_type(n, t))
        memory = menu.addAction("Memory")
        memory.setObjectName("Memory")
        memory.setCheckable(True)
        memory.setChecked(node.GetMemory())
        memory.setEnabled(not locked)
        memory.setToolTip("Resume from the running child on the next tick (py_trees memory).")
        memory.toggled.connect(lambda checked, n=node: view._set_memory(n, checked))
    menu.addSeparator()
    delete = menu.addAction("Delete")
    delete.setObjectName("Delete")
    if isinstance(node, RootNodeWidget):
        delete.setEnabled(False)
        delete.setText("Delete (the root node cannot be deleted)")
    elif locked:
        delete.setEnabled(False)
        delete.setText("Delete (stop execution first)")
    delete.triggered.connect(lambda _checked=False, n=node: view._delete_node_from_menu(n))
    return menu


def _add_group_actions(view: TreeView, menu: QMenu, group: GroupNodeWidget) -> None:
    rename = menu.addAction("Rename...")
    rename.setObjectName("Rename")
    rename.triggered.connect(lambda _checked=False, n=group: view._rename_node(n))
    contents = menu.addAction("Select Contents")
    contents.setObjectName("SelectContents")
    contents.setToolTip("Select this group and every node lying completely inside it.")
    contents.triggered.connect(lambda _checked=False, n=group: view._select_group_contents(n))
    colors = menu.addMenu("Border Color")
    colors.setObjectName("BorderColor")
    current = group.GetBorderColor()
    palette = QActionGroup(colors)
    palette.setExclusive(True)
    for name, color in GROUP_COLORS.items():
        action = colors.addAction(color_icon(color), name)
        action.setObjectName(f"Color_{name}")
        action.setCheckable(True)
        action.setChecked(color == current)
        palette.addAction(action)
        action.triggered.connect(lambda _checked=False, n=group, c=color: view._set_group_color(n, c))
    colors.addSeparator()
    custom = colors.addAction("Custom...")
    custom.setObjectName("Color_Custom")
    custom.setCheckable(True)
    custom.setChecked(current not in GROUP_COLORS.values())
    palette.addAction(custom)
    custom.triggered.connect(lambda _checked=False, n=group: view._choose_group_color(n))
    menu.addSeparator()
    promote = menu.addAction("Promote to Sub-Tree")
    promote.setObjectName("PromoteToSubTree")
    promote.setToolTip(
        "Write the nodes in this group to <group name>_BT.json and replace the group with a "
        "Sub-Tree node running that file."
    )
    if view.is_locked():
        promote.setEnabled(False)
        promote.setText("Promote to Sub-Tree (stop execution first)")
    promote.triggered.connect(lambda _checked=False, n=group: view._promote_group(n))


def build_connection_menu(view: TreeView, item: ConnectionItem) -> QMenu:
    """Context menu of a connection line: Delete Connection."""
    menu = QMenu(view)
    menu.setObjectName("ConnectionMenu")
    delete = menu.addAction("Delete Connection")
    delete.setObjectName("DeleteConnection")
    delete.setEnabled(not view.is_locked())
    delete.triggered.connect(lambda _checked=False, c=item: view._delete_connection(c))
    return menu


def color_icon(color: str | QColor) -> QIcon:
    """A small square of ``color`` for the colour menu entries."""
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)
