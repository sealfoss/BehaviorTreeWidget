"""The :class:`BehaviorTreeWidget`: a tabbed editor and executor for py_trees behavior trees."""

from __future__ import annotations

import atexit
import functools
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import shiboken6
from PySide6.QtCore import QByteArray, QCoreApplication, QMimeData, QPointF, QRectF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ._common import TREE_FILE_FILTER, file_key
from ._ui import load_ui
from .blackboard import BlackboardStore, BlackboardView
from .blackboard_nodes import BlackboardValueNodeWidget, EvaluationNodeWidget, SetNodeWidget
from .control_nodes import PauseNodeWidget, RestartNodeWidget, StopNodeWidget
from .canvas import TreeView, clamp_to_scene, iter_subtree, nearest_free_offset
from .config import ConfigureDialog, TreeConfig
from .execution import ExecutionState, LeafBehaviour, TreeExecutor
from .groups import GROUP, GroupNodeWidget, default_group_name, default_group_number
from .network_nodes import UDP_MESSAGE, UdpMessageNodeWidget
from .nodes import (
    COMPOSITE_TYPES,
    NEGATION,
    SEQUENCE,
    CompositeNodeWidget,
    LeafNodeWidget,
    NegationNodeWidget,
    NodeWidget,
    RootNodeWidget,
    UnknownLeafNodeWidget,
    relayout,
)
from .notes import NOTES, BoxNodeWidget, NotesNodeWidget
from .print_node import PRINT_MESSAGE, PrintMessageNodeWidget
from .serialization import (
    FORMAT_NAME,
    FORMAT_VERSION,
    JSON_EXTENSION,
    TREE_FILE_SUFFIX,
    TreeFileError,
    blackboard_document,
    blackboard_path_for,
    decode_value,
    encode_value,
    is_blackboard_file_name,
    is_finite_number,
    read_blackboard_file,
    read_tree_file,
    tree_file_paths,
    tree_name,
    write_json_atomic,
    write_json_files_atomic,
)
from .subtree import SUB_TREE, SubTreeNodeWidget
from .tree_log import START_FOLDER, LogLevel, TreeLogger, close_log_file, log_file_name

log = logging.getLogger("behavior_tree_widget")

__all__ = ["BehaviorTreeWidget", "register_node_type", "registered_node_types"]

#: Built-in leaf node types that work with the blackboard, by type name.
BLACKBOARD_LEAF_TYPES: dict[str, type[LeafNodeWidget]] = {"Evaluation": EvaluationNodeWidget, "Set": SetNodeWidget}
#: Built-in leaf node types that control the execution of their tree, by type name.
EXECUTION_LEAF_TYPES: dict[str, type[LeafNodeWidget]] = {
    "Restart": RestartNodeWidget,
    "Pause": PauseNodeWidget,
    "Stop": StopNodeWidget,
}
#: Built-in leaf node types that communicate over the network, by type name.
NETWORK_LEAF_TYPES: dict[str, type[LeafNodeWidget]] = {UDP_MESSAGE: UdpMessageNodeWidget}
#: Built-in leaf node types that write to the tree's log, by type name.
LOG_LEAF_TYPES: dict[str, type[LeafNodeWidget]] = {PRINT_MESSAGE: PrintMessageNodeWidget}
#: Built-in leaf node types that run other trees, by type name.
SUBTREE_LEAF_TYPES: dict[str, type[LeafNodeWidget]] = {SUB_TREE: SubTreeNodeWidget}
#: Leaf node types every BehaviorTreeWidget offers, by type name.
BUILTIN_LEAF_TYPES: dict[str, type[LeafNodeWidget]] = {
    **BLACKBOARD_LEAF_TYPES,
    **EXECUTION_LEAF_TYPES,
    **NETWORK_LEAF_TYPES,
    **LOG_LEAF_TYPES,
    **SUBTREE_LEAF_TYPES,
}
#: Node types with children offered by the add-node menu, in menu order.
PARENT_TYPES = (*COMPOSITE_TYPES, NEGATION)
#: Built-in type names added after version 0.5.0. Applications may already use them for leaf
#: types of their own: such a type keeps the name in the widgets it is registered with (the
#: built-in node is then not offered there), so the application and its trees work as before.
YIELDING_TYPE_NAMES = frozenset({UDP_MESSAGE, NOTES, SUB_TREE, GROUP, PRINT_MESSAGE})
RESERVED_TYPE_NAMES = frozenset({"Root", *PARENT_TYPES, *BUILTIN_LEAF_TYPES}) - YIELDING_TYPE_NAMES
#: Characters a group name cannot contain when it is promoted to a sub-tree (it names a file).
INVALID_FILE_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
BUTTON_NAMES = ("Execute", "Pause", "Stop", "Reset", "Configure", "Save", "Load", "New", "GoBack")
LOOP_CHECKBOX_NAME = "LoopExecution"
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
# Text-presentation symbols (U+FE0E) instead of the colour emoji of the .ui file, so disabled
# buttons are drawn greyed out like any other text.
BUTTON_GLYPHS = {
    "Execute": "\u25B6\uFE0E",
    "Pause": "\u23F8\uFE0E",
    "Stop": "\u23F9\uFE0E",
    "Reset": "\u21BB",
    "Configure": "\u2699\uFE0E",
}
SYMBOL_FONTS = ["Segoe UI Symbol", "DejaVu Sans", "Noto Sans Symbols2", "Noto Sans Symbols", "Apple Symbols"]
SAVE_ERRORS = (OSError, ValueError, TypeError, RecursionError)


def _json_writable(value: Any) -> bool:
    """Whether ``value`` (kept raw from a file) can be written by json inside a tree file."""
    for _ in range(16):  # room for the document around the value
        value = {"": value}
    try:
        json.dumps(value, indent=2, ensure_ascii=False)
    except RecursionError:
        return False
    return True

_global_node_types: dict[str, type[LeafNodeWidget]] = {}


def _type_name_of(cls: type) -> str:
    """Registration name of a node class: its own ``TYPE_NAME`` or its class name."""
    return cls.__dict__.get("TYPE_NAME") or cls.__name__


def _validate_leaf_class(cls: Any) -> str:
    if not isinstance(cls, type) or not issubclass(cls, LeafNodeWidget):
        raise TypeError(f"node types must be subclasses of LeafNodeWidget, not {cls!r}")
    if cls in (LeafNodeWidget, BlackboardValueNodeWidget) or issubclass(cls, UnknownLeafNodeWidget):
        raise TypeError(f"{cls.__name__} cannot be registered; register a subclass that implements OnRun")
    name = _type_name_of(cls)
    if not isinstance(name, str) or not name:
        raise ValueError(f"{cls.__name__}.TYPE_NAME must be a non-empty string")
    if name in RESERVED_TYPE_NAMES and BUILTIN_LEAF_TYPES.get(name) is not cls:
        raise ValueError(f"{name!r} is a built-in node type name; set a different TYPE_NAME on {cls.__name__}")
    return name


def _register(registry: dict[str, type[LeafNodeWidget]], cls: type[LeafNodeWidget]) -> str:
    name = _validate_leaf_class(cls)
    existing = registry.get(name)
    if existing is not None and existing is not cls and not (name in YIELDING_TYPE_NAMES and existing is BUILTIN_LEAF_TYPES.get(name)):
        raise ValueError(
            f"node type name {name!r} is already registered to {existing.__module__}.{existing.__qualname__}; "
            f"give {cls.__qualname__} a different TYPE_NAME"
        )
    registry[name] = cls
    return name


def register_node_type(cls: type[LeafNodeWidget]) -> type[LeafNodeWidget]:
    """Class decorator registering a leaf node type with every BehaviorTreeWidget created afterwards.

    Such widgets offer the type in their right-click menu and can load trees that use it::

        @register_node_type
        class Beep(LeafNodeWidget):
            def OnRun(self, tree):
                print("beep")
                return True
    """
    _register(_global_node_types, cls)
    return cls


def registered_node_types() -> dict[str, type[LeafNodeWidget]]:
    """Leaf node types registered with :func:`register_node_type`, by type name."""
    return dict(_global_node_types)


@dataclass
class _TreePlan:
    """A validated tree file, ready to be installed in the view."""

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
        for node, _ in self.nodes:
            if shiboken6.isValid(node):
                node.deleteLater()
        self.nodes = []


@dataclass(eq=False)
class _ZoomLevel:
    """A sub-tree shown in place of the tree that contains it (see BehaviorTreeWidget.ZoomIn)."""

    view: TreeView
    path: str  # its tree file
    config: TreeConfig  # the options in its file, written back when it is saved
    scope: tuple[str, ...]  # ids of the Sub-Tree nodes zoomed into, from the main tree to this one
    entry: SubTreeNodeWidget  # the Sub-Tree node zoomed into (it lies in the level below)
    modified: bool = False
    # The "blackboard" section of a single-file tree of an earlier version, written back unchanged
    # when the sub-tree is saved (a sub-tree uses the blackboard of the tree running it).
    legacy_blackboard: Any = None


STANDARD_NODE_KEYS = frozenset({"id", "type", "title", "x", "y", "fields", "field_selections"})
NOTE_KEYS = frozenset({"text", "width", "height"})
GROUP_KEYS = frozenset({"width", "height", "color"})


def _saved_as_builtin(type_name: str, item: dict) -> bool:
    """Whether a saved node of a type in YIELDING_TYPE_NAMES was saved by the built-in node.

    Before these types were built in, an application could save leaf nodes of its own with
    these names; their data must not be read as (and lost to) the built-in node's.
    """
    if type_name in (NOTES, GROUP):
        # Notes write text / width / height and groups width / height / color; a leaf writes
        # "fields" (version 0.5.0 also adds an empty "fields" to a node it kept as a placeholder).
        return bool((NOTE_KEYS if type_name == NOTES else GROUP_KEYS) & item.keys()) or "fields" not in item
    fields = item.get("fields")
    return not isinstance(fields, dict) or set(fields) <= set(BUILTIN_LEAF_TYPES[type_name]._fields)


def _cleanup_after_destroy(store: BlackboardStore, executor: TreeExecutor, log_closer) -> None:
    """Release resources of a BehaviorTreeWidget destroyed without Shutdown()."""
    close_log_file(log_closer)  # the log file is closed once what was logged is written
    executor._shut_down = True
    executor._aborting = True  # a tick in progress runs no more node code
    for behaviour in list(getattr(executor, "_behaviours", {}).values()):
        if isinstance(behaviour, LeafBehaviour):
            behaviour.cancel()
    try:
        store.dispose(notify=False)  # the Blackboard tab is being destroyed as well
    except Exception:  # noqa: BLE001
        log.debug("blackboard cleanup failed", exc_info=True)


class BehaviorTreeWidget(QTabWidget):
    """Tabbed widget to build, run, save and load py_trees behavior trees graphically.

    The "Behavior Tree" tab shows the editable tree and the execution buttons; the
    "Blackboard" tab lists the blackboard entries shared by the nodes.

    Args:
        parent: Parent widget.
        node_types: Leaf node classes (subclasses of :class:`LeafNodeWidget`) to offer
            in addition to the ones registered with :func:`register_node_type`.
    """

    treeLoaded = Signal(str)  # path of the tree file (<name>_BT.json) after New / Load
    treeSaved = Signal(str)  # path of the tree file the tree was saved to
    modifiedChanged = Signal(bool)
    executionStateChanged = Signal(str)  # "Idle", "Running", "Paused"
    executionFinished = Signal(str)  # "Succeeded" / "Failed" (run-once mode)
    nodeStatusChanged = Signal(object, str)  # node, "Ready"/"Running"/"Succeeded"/"Failed"
    nodeError = Signal(object, str)  # node, error message
    nodeSelectionChanged = Signal()  # the selected nodes changed (GetSelectedNodes)
    blackboardSaved = Signal(str)  # path of the blackboard file (<name>_BB.json) the blackboard was written to
    blackboardSaveFailed = Signal(str)  # error message: the changed blackboard could not be written
    subTreePromoted = Signal(object, str)  # the new Sub-Tree node, path of the sub-tree file written
    subTreeSaved = Signal(str)  # path of the sub-tree file a sub-tree shown with Zoom In was saved to
    zoomChanged = Signal(int, str)  # zoom depth (0: the tree itself), path of the tree file shown
    # The tree's log (see tree_log): one signal per level, and one for every message.
    errorLogged = Signal(str)
    warningLogged = Signal(str)
    debugLogged = Signal(str)
    infoLogged = Signal(str)
    messageLogged = Signal(int, str)  # level (LogLevel value), message

    TREE_TAB_TITLE = "Behavior Tree"
    BLACKBOARD_TAB_TITLE = "Blackboard"
    FILE_FILTER = TREE_FILE_FILTER
    WINDOW_TITLE_FORMAT = "Behavior Tree - {filename}"
    #: Blackboard changes are written to the tree file at most this often (ms); changes made in
    #: between are written together at the end of the interval.
    BLACKBOARD_SAVE_INTERVAL_MS = 250
    #: Folder of the log files; None: the folder the application was started from.
    LOG_FOLDER: str | None = None

    def __init__(self, parent: QWidget | None = None, node_types: Iterable[type[LeafNodeWidget]] = ()):
        super().__init__(parent)
        self.setObjectName("BehaviorTreeWidget")
        self._gui_thread = threading.get_ident()
        self._node_types: dict[str, type[LeafNodeWidget]] = {**BUILTIN_LEAF_TYPES, **_global_node_types}
        self._config = TreeConfig()
        self._file_path: str | None = None
        self._tree_loaded = False
        self._modified = False  # the tree itself (sub-trees shown with Zoom In: _ZoomLevel.modified)
        self._reported_modified = False  # IsModified() as last announced by modifiedChanged
        self._levels: list[_ZoomLevel] = []  # sub-trees shown with Zoom In, the one shown last
        self._tree_log = TreeLogger(self, emitter=self)  # emits errorLogged ... messageLogged of the widget
        self._tree_log.set_level(self._config.log_level)
        self._loading = False
        self._shut_down = False
        self._view_target: QPointF | None = None  # scene point to centre on; None = frame the root
        self._view_target_pending = False
        self._view_settled = False
        # Deferred view positioning; owned by the widget so it never fires after deletion.
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.timeout.connect(self._apply_pending_view_target)
        # The blackboard file (<name>_BB.json) of the tree, and the entries as last written to it
        # or read from it. Blackboard changes are written to that file only; the tree file holds
        # the nodes, connections and options and changes on Save only.
        self._blackboard_path: str | None = None
        self._blackboard_written: list | None = None
        # BlackboardStore.revision(saved=True) when _blackboard_written was taken (None: unknown).
        self._blackboard_written_revision: int | None = None
        self._group_counter = 0  # number of the last default group name given (group_001, ...)
        self._blackboard_save_pending = False
        self._blackboard_save_error: str | None = None
        self._blackboard_skipped: set[str] = set()  # values that cannot be written, reported once each
        self._last_blackboard_save = 0.0  # time.monotonic() of the last write
        self._blackboard_timer = QTimer(self)
        self._blackboard_timer.setSingleShot(True)
        self._blackboard_timer.timeout.connect(self._save_blackboard_now)

        # ---- "Behavior Tree" tab (BehaviorTreeView.ui)
        self._tree_page = load_ui("BehaviorTreeView.ui")
        frame_view: QFrame = self._tree_page.findChild(QFrame, "FrameView")
        frame_layout = QVBoxLayout(frame_view)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        self._frame_view = frame_view
        self._frame_layout = frame_layout
        self._main_view = TreeView(self, frame_view)  # the tree itself; sub-trees get views of their own
        frame_layout.addWidget(self._main_view)
        self._buttons: dict[str, QPushButton] = {}
        for name in BUTTON_NAMES:
            button = self._tree_page.findChild(QPushButton, name)
            if button is None:
                raise RuntimeError(f"BehaviorTreeView.ui has no QPushButton named {name!r}")
            self._buttons[name] = button
            if name in BUTTON_GLYPHS:
                button.setText(BUTTON_GLYPHS[name])
                font = QFont(button.font())
                font.setFamilies(SYMBOL_FONTS + font.families())
                button.setFont(font)
        self._loop_box: QCheckBox = self._tree_page.findChild(QCheckBox, LOOP_CHECKBOX_NAME)
        if self._loop_box is None:
            raise RuntimeError(f"BehaviorTreeView.ui has no QCheckBox named {LOOP_CHECKBOX_NAME!r}")
        self.addTab(self._tree_page, self.TREE_TAB_TITLE)

        # ---- "Blackboard" tab (BlackBoardView.ui)
        self._blackboard = BlackboardStore(self)
        self._blackboard_view = BlackboardView(self._blackboard)
        self.addTab(self._blackboard_view, self.BLACKBOARD_TAB_TITLE)

        # ---- execution
        self._executor = TreeExecutor(self)
        self._executor.stateChanged.connect(self._on_execution_state)
        self._executor.finished.connect(self.executionFinished)
        self._executor.nodeStatusChanged.connect(self.nodeStatusChanged)
        self._executor.nodeError.connect(self.nodeError)

        # ---- wiring
        buttons = self._buttons
        buttons["Execute"].clicked.connect(self.Execute)
        buttons["Pause"].clicked.connect(self.Pause)
        buttons["Stop"].clicked.connect(self.Stop)
        buttons["Reset"].clicked.connect(self.Reset)
        buttons["Configure"].clicked.connect(self.Configure)
        buttons["Save"].clicked.connect(self._on_save_clicked)
        buttons["Load"].clicked.connect(self._on_load_clicked)
        buttons["New"].clicked.connect(self._on_new_clicked)
        buttons["GoBack"].clicked.connect(self._on_go_back_clicked)
        buttons["GoBack"].setToolTip("Go back to the tree containing the sub-tree shown")
        self._loop_box.setChecked(self._config.repeat)
        self._loop_box.toggled.connect(self.SetLoopExecution)

        self._main_view.modified.connect(self._mark_modified)
        self._main_view.nodeAdded.connect(self._on_node_added)
        self._main_view.nodeSelectionChanged.connect(self.nodeSelectionChanged)
        self._main_view.shown.connect(self._on_view_shown)
        self._main_view.navigated.connect(self._on_view_navigated)
        self._blackboard_view.entryEdited.connect(self._on_blackboard_edited)
        self._blackboard.changed.connect(self._on_blackboard_changed)
        self._blackboard.entriesReordered.connect(self._on_blackboard_reordered)
        self._blackboard.entryResetChanged.connect(self._on_blackboard_reset_changed)

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.Shutdown)
        # Cancel running nodes and release the py_trees keys even if Shutdown() is never called.
        self.destroyed.connect(
            functools.partial(_cleanup_after_destroy, self._blackboard, self._executor, self._tree_log.closer_record())
        )

        for cls in node_types:
            self.RegisterNodeType(cls)

        self._loading = True
        try:
            self._main_view.add_node(RootNodeWidget(memory=self._config.default_memory), QPointF(0.0, 0.0))
        finally:
            self._loading = False
        self._set_view_target(None)
        self._set_tree_loaded(False)

    # ================================================================== node types
    def RegisterNodeType(self, cls: type[LeafNodeWidget]) -> None:
        """Make the leaf node class ``cls`` available in this widget (menu and loading).

        Raises:
            TypeError: ``cls`` is not a LeafNodeWidget subclass.
            ValueError: its type name is reserved or registered to another class.
        """
        _register(self._node_types, cls)

    def GetNodeTypes(self) -> list[str]:
        """Names of every node type that can be added: Sequence, Selector, Negation, the
        built-in leaf types Evaluation, Set, Restart, Pause, Stop, UDP Message, Print Message
        and Sub-Tree, the registered leaf types (sorted), then Notes and Group."""
        builtin = [name for name in BUILTIN_LEAF_TYPES if self._is_builtin(name)]
        registered = sorted(name for name in self._node_types if not self._is_builtin(name))
        boxes = [name for name in (NOTES, GROUP) if self._offers_box(name)]
        return [*PARENT_TYPES, *builtin, *registered, *boxes]

    def GetNodeType(self, type_name: str) -> type[LeafNodeWidget] | None:
        """The leaf class of ``type_name``, including the built-in leaf types such as Evaluation,
        Restart and Sub-Tree (None for unknown types and for Sequence, Selector, Negation, Root,
        Notes and Group)."""
        return self._node_types.get(type_name)

    def _is_builtin(self, type_name: str) -> bool:
        """True if ``type_name`` is a built-in leaf type of this widget (not an application's own type)."""
        cls = self._node_types.get(type_name)
        return cls is not None and cls is BUILTIN_LEAF_TYPES.get(type_name)

    def _has_builtin(self, type_name: str) -> bool:
        """True if ``type_name`` names a built-in node type in this widget (Notes, Group or a built-in leaf)."""
        return self._offers_box(type_name) if type_name in (NOTES, GROUP) else self._is_builtin(type_name)

    def _offers_notes(self) -> bool:
        """False when an application registered a leaf type of its own named "Notes"."""
        return self._offers_box(NOTES)

    def _offers_groups(self) -> bool:
        """False when an application registered a leaf type of its own named "Group"."""
        return self._offers_box(GROUP)

    def _offers_box(self, type_name: str) -> bool:
        """Whether the built-in Notes / Group type is offered (an application's own leaf type of
        that name takes its place)."""
        return type_name not in self._node_types

    def _node_menu_entries(self) -> list[tuple[str, str, str]]:
        """``(group, type_name, label)`` of every type offered by the add-node menu."""
        entries = [("composite", name, name) for name in PARENT_TYPES]
        leaves = []
        for name, cls in self._node_types.items():
            title = cls._title if isinstance(cls._title, str) and cls._title else cls.__name__
            leaves.append((title, name))
        titles = [title for title, _ in leaves]

        def label(title: str, name: str) -> str:
            return title if titles.count(title) == 1 or self._is_builtin(name) else f"{title} ({name})"

        for group, types in (
            ("blackboard", BLACKBOARD_LEAF_TYPES),
            ("execution", EXECUTION_LEAF_TYPES),
            ("network", NETWORK_LEAF_TYPES),
            ("log", LOG_LEAF_TYPES),
            ("subtree", SUBTREE_LEAF_TYPES),
        ):
            entries += [(group, name, label(title, name)) for title, name in leaves if name in types and self._is_builtin(name)]
        for title, name in sorted(leaves, key=lambda pair: (pair[0].lower(), pair[1])):
            if not self._is_builtin(name):
                entries.append(("leaf", name, label(title, name)))
        for name in (NOTES, GROUP):
            if self._offers_box(name):
                entries.append(("notes", name, name))
        return entries

    def _create_node(self, type_name: str) -> NodeWidget:
        if type_name in COMPOSITE_TYPES:
            return CompositeNodeWidget(type_name, memory=self._config.default_memory)
        if type_name == NEGATION:
            return NegationNodeWidget()
        if type_name == NOTES and self._offers_notes():
            return NotesNodeWidget()
        if type_name == GROUP and self._offers_groups():
            group = GroupNodeWidget()
            group._apply_title(self._next_group_name(), runtime=False)
            return group
        cls = self._node_types.get(type_name)
        if cls is None:
            raise ValueError(f"unknown node type {type_name!r}")
        return cls()

    def _next_group_name(self) -> str:
        """The next default group name, ``group_<number>``: the number counts up from the highest
        one given so far (or used by a group of the tree), skipping names in use."""
        names = {node.GetTitle() for node in self._view.nodes() if isinstance(node, GroupNodeWidget)}
        used = [number for number in map(default_group_number, names) if number is not None]
        number = max([self._group_counter, *used]) + 1
        while default_group_name(number) in names:
            number += 1
        self._group_counter = number
        return default_group_name(number)

    def _require_gui_thread(self, name: str) -> None:
        if threading.get_ident() != self._gui_thread:
            raise RuntimeError(f"BehaviorTreeWidget.{name}() must be called from the GUI thread")

    @property
    def _view(self) -> TreeView:
        """The view of the tree shown: the tree itself, or the sub-tree zoomed into last."""
        return self._levels[-1].view if self._levels else self._main_view

    # ================================================================== tree editing API
    def GetRootNode(self) -> RootNodeWidget | None:
        """The root node of the tree shown (of the sub-tree while zoomed in, see :meth:`ZoomIn`)."""
        return self._view.root()

    def GetNodes(self) -> list[NodeWidget]:
        """Every node of the tree shown (connected or not)."""
        return self._view.nodes()

    def AddNode(self, node_type: str | type[LeafNodeWidget] | NodeWidget, x: float = 0.0, y: float = 0.0) -> NodeWidget:
        """Add a node with its top-left corner at scene position (x, y) and return it.

        ``node_type`` is a type name (one of :meth:`GetNodeTypes`, e.g. "Sequence" or
        "Notes"), a leaf class or a node instance other than a Root (the class of a leaf
        is registered automatically).

        Raises:
            RuntimeError: the tree is executing.
        """
        self._require_gui_thread("AddNode")
        self._view._check_unlocked()
        position, _ = clamp_to_scene(QPointF(float(x), float(y)))
        if isinstance(node_type, NodeWidget):
            node = node_type
            if isinstance(node, LeafNodeWidget) and not isinstance(node, UnknownLeafNodeWidget):
                _register(self._node_types, type(node))
            elif isinstance(node, RootNodeWidget):
                raise ValueError("a tree can only have one root node")
            elif isinstance(node, NotesNodeWidget) and not self._offers_notes():
                raise ValueError(f"{NOTES!r} is the name of a registered leaf type in this widget; notes are not available")
            elif isinstance(node, GroupNodeWidget) and not self._offers_groups():
                raise ValueError(f"{GROUP!r} is the name of a registered leaf type in this widget; groups are not available")
        elif isinstance(node_type, type):
            _register(self._node_types, node_type)
            node = node_type()
        else:
            node = self._create_node(str(node_type))
        self._view.add_node(node, position)
        return node

    def RemoveNode(self, node: NodeWidget) -> None:
        """Delete ``node`` (the root cannot be deleted). Raises RuntimeError while executing."""
        self._require_gui_thread("RemoveNode")
        self._view.remove_node(node)

    def Connect(self, parent: NodeWidget, child: NodeWidget) -> None:
        """Make ``child`` a child of ``parent`` (replacing its previous parent).

        Raises:
            ValueError: the connection is not allowed (e.g. it would create a cycle).
            RuntimeError: the tree is executing.
        """
        self._require_gui_thread("Connect")
        self._view.connect_nodes(parent, child)

    def Disconnect(self, child: NodeWidget) -> None:
        """Remove the connection between ``child`` and its parent. Raises RuntimeError while executing."""
        self._require_gui_thread("Disconnect")
        self._view.disconnect_node(child)

    # ================================================================== selection / clipboard API
    def GetSelectedNodes(self) -> list[NodeWidget]:
        """The selected nodes (in the order they were added)."""
        return self._view.selected_nodes()

    def SelectNodes(self, nodes: Iterable[NodeWidget], add: bool = False) -> None:
        """Select ``nodes`` (``add`` keeps the current selection; ``SelectNodes([])`` clears it)."""
        self._require_gui_thread("SelectNodes")
        self._view.select_nodes(nodes, add=add)

    def CopyNodes(self, nodes: Iterable[NodeWidget] | None = None) -> bool:
        """Copy ``nodes`` (default: the selected nodes) to the clipboard, as Ctrl+C does.

        The connections between the copied nodes are copied too. The Root is never copied.
        Returns False when there was nothing to copy (the clipboard is then left unchanged).
        """
        self._require_gui_thread("CopyNodes")
        return self._copy_nodes(self._view.selected_nodes() if nodes is None else nodes)

    def PasteNodes(self) -> list[NodeWidget]:
        """Paste the nodes on the clipboard, as Ctrl+V does, and return them (they become the selection).

        The copies keep their arrangement and the connections between them, and are placed
        in the free area nearest to where the copied nodes were. Returns ``[]`` when the
        clipboard holds no nodes.

        Raises:
            RuntimeError: the tree is executing.
        """
        self._require_gui_thread("PasteNodes")
        self._view._check_unlocked()
        return self._paste_nodes()

    # ================================================================== loop execution
    def GetLoopExecution(self) -> bool:
        """True if the tree runs again each time the root returns (the "Loop Execution" check box)."""
        return self._config.repeat

    def SetLoopExecution(self, loop: bool) -> None:
        """Check or uncheck "Loop Execution" (``TreeConfig.repeat``); saved with the tree.

        Looping starts the tree again on the tick after the root returns, keeping the
        blackboard and node values as they are. Unchecked, execution stops once the root
        returns. A change applies to a running tree at once.
        """
        self._require_gui_thread("SetLoopExecution")
        config = self._config.copy()
        config.repeat = bool(loop)
        self.SetConfig(config)
        self._sync_loop_box()  # also when unchanged (e.g. a click while the tree cannot change)

    # ================================================================== blackboard API
    def GetEntry(self, name: str) -> Any:
        """Value of blackboard entry ``name`` (thread-safe). Raises KeyError if missing."""
        return self._blackboard.get(name)

    def SetEntry(self, value: Any, name: str) -> None:
        """Set blackboard entry ``name`` to ``value`` (thread-safe).

        The entry is created when it does not exist; its type is inferred from
        ``value`` (int -> Integer, float -> Double, str -> String, bool -> Bool,
        list -> List, dict -> Dictionary, set -> Set, anything else -> Object).
        Setting an existing entry requires a compatible value (TypeError otherwise).
        """
        self._blackboard.set(name, value)

    def AddEntry(self, name: str, type_name: str, value: Any = None, reset: bool = False) -> None:
        """Create entry ``name`` of ``type_name`` (Integer, Double/Float, String, Bool, List,
        Dictionary, Set or Object) with ``value`` or the type's default value. ``reset`` checks
        its Reset box (see :meth:`SetEntryReset`)."""
        self._blackboard.add(name, type_name, value, reset=reset)

    def GetEntryReset(self, name: str) -> bool:
        """True if the Reset box of entry ``name`` is checked (thread-safe). Raises KeyError if missing."""
        return self._blackboard.is_reset(name)

    def SetEntryReset(self, name: str, reset: bool) -> None:
        """Check or uncheck the Reset box of entry ``name`` (thread-safe; KeyError if missing).

        At the end of every execution loop (when the Root returns, or when execution is
        stopped or restarted), an entry with Reset checked gets back the value it had when
        that loop started. The flag is saved in the blackboard file.
        """
        self._blackboard.set_reset(name, reset)

    def MoveEntry(self, name: str, offset: int) -> int:
        """Move entry ``name`` ``offset`` places down the list of entries (up when negative),
        as its arrows in the Blackboard tab do; it stops at either end. Returns its new index
        (thread-safe; KeyError if it does not exist). The order is saved in the blackboard file."""
        return self._blackboard.move(name, offset)

    def HasEntry(self, name: str) -> bool:
        return self._blackboard.has(name)

    def RemoveEntry(self, name: str) -> None:
        self._blackboard.remove(name)

    def GetEntryNames(self) -> list[str]:
        return self._blackboard.names()

    def GetEntryType(self, name: str) -> str:
        return self._blackboard.type_of(name)

    def GetBlackboardNamespace(self) -> str:
        """py_trees blackboard namespace holding this widget's entries."""
        return self._blackboard.namespace()

    # ================================================================== execution API
    def Execute(self) -> None:
        """Start executing the tree (or resume it when paused). Callable from any thread."""
        if not self._tree_loaded:
            return
        if threading.get_ident() != self._gui_thread:
            self._executor.start()  # carried out on the GUI thread
            return
        try:
            self._executor.start()
        except Exception as error:  # noqa: BLE001
            log.exception("could not start execution")
            self._log(LogLevel.ERROR, f"The tree could not be executed: {error}")
            QMessageBox.critical(self, "Execute", f"The tree could not be executed:\n{error}")

    def Pause(self) -> None:
        """Pause execution (Execute resumes it)."""
        self._executor.pause()

    def Stop(self) -> None:
        """Halt execution and reset every node to Ready."""
        self._executor.stop()

    def Reset(self) -> None:
        """Reset every node to Ready without halting execution."""
        self._executor.reset()

    def GetExecutionState(self) -> str:
        """"Idle", "Running" or "Paused"."""
        return self._executor.state().value

    def IsExecuting(self) -> bool:
        """True while the tree is running or paused."""
        return self._executor.state() is not ExecutionState.IDLE

    def GetConfig(self) -> TreeConfig:
        """A copy of the tree options."""
        return self._config.copy()

    def SetConfig(self, config: TreeConfig) -> None:
        """Replace the tree options (the tick interval is clamped to 1..60000 ms).

        Raises:
            TypeError: ``config`` is not a TreeConfig or holds values of the wrong type.
        """
        self._require_gui_thread("SetConfig")
        if not isinstance(config, TreeConfig):
            raise TypeError("config must be a TreeConfig")
        for name in ("repeat", "restore_blackboard", "default_memory"):
            if not isinstance(getattr(config, name), bool):
                raise TypeError(f"TreeConfig.{name} must be a bool")
        level = config.log_level
        if isinstance(level, bool) or not isinstance(level, int):
            raise TypeError("TreeConfig.log_level must be an int (a LogLevel)")
        if not LogLevel.NONE <= level <= LogLevel.INFO:
            raise ValueError(f"TreeConfig.log_level must be between 0 and 4, not {level}")
        interval = config.tick_interval_ms
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise TypeError("TreeConfig.tick_interval_ms must be an int")
        config = config.copy()
        config.tick_interval_ms = min(max(interval, TreeConfig.MIN_TICK_MS), TreeConfig.MAX_TICK_MS)
        config.log_level = int(level)
        if config != self._config:
            self._config = config
            self._executor.set_interval(config.tick_interval_ms)
            self._tree_log.set_level(config.log_level)
            self._sync_loop_box()
            self._mark_modified()

    def Configure(self) -> None:
        """Open the Configure dialog."""
        dialog = ConfigureDialog(self._config, self)
        try:
            if self._run_dialog(dialog) and shiboken6.isValid(dialog):
                self.SetConfig(dialog.config())
        finally:
            if shiboken6.isValid(dialog):
                dialog.deleteLater()

    # ================================================================== log
    def Log(self, level: LogLevel | int | str, message: str) -> bool:
        """Write ``message`` to the tree's log at ``level`` (a :class:`LogLevel`, its number or its
        name). Callable from any thread (e.g. from ``OnRun``); it never blocks.

        The message is written to the log file and announced with the signal of its level
        (``errorLogged`` ... ``infoLogged``) and ``messageLogged``. Returns False when it was
        dropped: its level is NONE or less important than the log level (:meth:`GetLogLevel`).
        """
        return self._tree_log.log(level, message)

    def GetLogLevel(self) -> LogLevel:
        """The least important level that is logged (``TreeConfig.log_level``; INFO by default)."""
        return LogLevel(self._config.log_level)

    def SetLogLevel(self, level: LogLevel | int | str) -> None:
        """Change the log level (saved with the tree, like the other options). NONE (0) switches
        logging off; INFO (4) logs everything, including the execution of every node."""
        self._require_gui_thread("SetLogLevel")
        config = self._config.copy()
        config.log_level = int(LogLevel.coerce(level))
        self.SetConfig(config)

    def GetLogFilePath(self) -> str | None:
        """The log file of the tree, ``<tree name>_<month>-<day>-<year>_<hour>-<minute>-<second>.log``
        in :attr:`LOG_FOLDER` (by default the folder the application was started from), named
        with the time the application was started. None before a tree was created or loaded. The
        file is created with the first message written to it."""
        return self._tree_log.path()

    def treeLogger(self) -> TreeLogger:  # noqa: N802
        """The :class:`~behavior_tree_widget.tree_log.TreeLogger` writing the tree's log."""
        return self._tree_log

    def _log(self, level: LogLevel, message: str) -> None:
        self._tree_log.log(level, message)

    def _set_log_file(self, path: str) -> None:
        """Log to the file of the tree stored in ``path`` from now on."""
        folder = self.LOG_FOLDER or START_FOLDER
        self._tree_log.set_file(os.path.join(folder, log_file_name(tree_name(path))))

    # ================================================================== files
    def IsTreeLoaded(self) -> bool:
        """True once a tree was created (New), loaded or saved."""
        return self._tree_loaded

    def GetFilePath(self) -> str | None:
        """Path of the current tree file, ``<tree name>_BT.json`` (None before New / Load / Save).

        A file written by an earlier version keeps its own name until the tree is saved.
        """
        return self._file_path

    def GetBlackboardFilePath(self) -> str | None:
        """Path of the current blackboard file, ``<tree name>_BB.json`` (None before New / Load / Save).

        Every change of the blackboard is written to this file (see :meth:`SaveTree`).
        """
        return self._blackboard_path

    def IsModified(self) -> bool:
        """True if the tree changed since it was last loaded or saved, or a sub-tree shown with
        :meth:`ZoomIn` has unsaved changes."""
        return self._modified or any(level.modified for level in self._levels)

    def NewTree(self, path: str | None = None) -> bool:
        """Create a new tree containing only the root and store it.

        A tree is stored in two files named after the tree: ``<name>_BT.json`` (the tree)
        and ``<name>_BB.json`` (its blackboard), in the folder of ``path``. ``path`` names the
        tree: ``robot``, ``robot.json``, ``robot_BT.json`` and ``robot_BB.json`` all create
        ``robot_BT.json`` and ``robot_BB.json``. Without ``path`` the user is asked for a file.
        Returns True on success. The blackboard is cleared, except Object entries (run-time
        objects set from code), and the blackboard file is replaced by an empty one.

        Raises:
            OSError: ``path`` was given and a file could not be written.
        """
        self._require_gui_thread("NewTree")
        interactive = path is None
        if interactive:
            if not self.ConfirmDiscardChanges():
                return False
            path = self._ask_save_path("New Behavior Tree", self._suggested_path(f"behavior_tree{TREE_FILE_SUFFIX}.json"))
            if not path:
                return False
        tree_path, blackboard_path = tree_file_paths(os.path.abspath(path))
        data = self._new_tree_data()
        try:
            # Both files or neither: a tree file is never replaced when its blackboard file cannot be.
            write_json_files_atomic([(tree_path, data), (blackboard_path, blackboard_document([]))])
        except SAVE_ERRORS as error:
            if not interactive:
                raise
            QMessageBox.critical(self, "New Behavior Tree", f"Could not create '{tree_path}':\n{error}")
            return False
        try:
            self._apply_tree_data(
                data, tree_path, interactive, blackboard=[], blackboard_path=blackboard_path, new_blackboard=True
            )
        except TreeFileError as error:
            if not interactive:
                raise
            QMessageBox.critical(self, "New Behavior Tree", str(error))
            return False
        return True

    def LoadTree(self, path: str | None = None) -> bool:
        """Load a tree (asks for a file when ``path`` is None). Returns True on success.

        ``path`` is the tree file, ``<name>_BT.json``; its blackboard is read from
        ``<name>_BB.json`` next to it. The blackboard file may also be given (the tree is
        loaded), and so may the tree's name (``robot`` or ``robot.json`` load
        ``robot_BT.json`` when no file of that name exists). A tree file of an earlier
        version (one file holding the blackboard as well) is read as it is; its blackboard
        file is created at the first change of the blackboard.

        The blackboard is replaced by the entries of the blackboard file (empty when there is
        none, e.g. for a sub-tree file); Object entries (run-time objects set from code,
        which are never saved) are kept.

        If called while the tree is being ticked (e.g. from a node's ``OnRun``), the
        files are read and checked at once and the tree is replaced right after that tick.

        Raises:
            TreeFileError: ``path`` was given and the tree or its blackboard file is not valid.
        """
        self._require_gui_thread("LoadTree")
        interactive = path is None
        if interactive:
            if not self.ConfirmDiscardChanges():
                return False
            path, _ = QFileDialog.getOpenFileName(
                self, "Load Behavior Tree", self._suggested_path(""), self.FILE_FILTER
            )
            if not path:
                return False
        try:
            tree_path = self._tree_file_to_load(os.path.abspath(path))
            data = read_tree_file(tree_path)
            blackboard_path = blackboard_path_for(tree_path)
            if os.path.exists(blackboard_path):
                blackboard = read_blackboard_file(blackboard_path)
            else:
                # A file of an earlier version holds its blackboard itself; a sub-tree file has none.
                blackboard = data.get("blackboard", [])
            self._apply_tree_data(data, tree_path, interactive, blackboard=blackboard, blackboard_path=blackboard_path)
        except TreeFileError as error:
            if not interactive:
                raise
            QMessageBox.critical(self, "Load Behavior Tree", str(error))
            return False
        return True

    @staticmethod
    def _same_file(first: str | None, second: str | None) -> bool:
        if not first or not second:
            return False
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))

    @staticmethod
    def _tree_file_to_load(path: str) -> str:
        """The tree file LoadTree reads for ``path`` (see LoadTree)."""
        tree_path = tree_file_paths(path)[0]
        if is_blackboard_file_name(path) or (not os.path.exists(path) and os.path.exists(tree_path)):
            return tree_path
        return path

    def SaveTree(self, path: str | None = None) -> bool:
        """Save the tree (asks for a file when ``path`` is None). Returns True on success.

        This saves the tree itself, also while a sub-tree is shown (see :meth:`ZoomIn`; the Save
        button then saves the sub-tree, see :meth:`SaveSubTree`).

        The tree is written to ``<name>_BT.json`` and its blackboard to ``<name>_BB.json``,
        in the folder of ``path`` (see :meth:`NewTree` for how ``path`` names the tree).
        Values that cannot be written to JSON (e.g. Object blackboard entries) are
        skipped; interactively the user is told which. If the tree file was written but the
        blackboard file could not be, True is returned, ``blackboardSaveFailed`` is emitted
        and the tree stays marked as modified (interactively the user is told).

        Raises:
            OSError, ValueError, TypeError: ``path`` was given and the tree file could not be written.
        """
        self._require_gui_thread("SaveTree")
        interactive = path is None
        if interactive:
            suggestion = tree_file_paths(self._file_path)[0] if self._file_path else None
            path = self._ask_save_path(
                "Save Behavior Tree", suggestion or self._suggested_path(f"behavior_tree{TREE_FILE_SUFFIX}.json")
            )
            if not path:
                return False
        path = tree_file_paths(os.path.abspath(path))[0]
        try:
            self._save_tree(path, report_problems=interactive)
        except SAVE_ERRORS as error:
            if not interactive:
                raise
            QMessageBox.critical(self, "Save Behavior Tree", f"Could not save '{path}':\n{error}")
            return False
        return True

    def _save_tree(self, path: str, report_problems: bool) -> None:
        """Write the tree to the tree file ``path`` (``<name>_BT.json``) and the blackboard to its
        blackboard file; with ``report_problems`` the user is told what was left out."""
        path = tree_file_paths(path)[0]
        blackboard_path = blackboard_path_for(path)
        shown = {file_key(level.path) for level in self._levels}
        if shown & {file_key(path), file_key(blackboard_path)}:
            raise ValueError(
                f"'{os.path.basename(path)}' is the file of a sub-tree shown with Zoom In; "
                "go back first or choose another name"
            )
        if blackboard_path != self._blackboard_path:
            self._flush_blackboard_save()  # blackboard changes still due for the current file
        problems: list[str] = []
        data = self._tree_data(problems)
        try:
            write_json_atomic(path, data)
        except RecursionError:
            # Data of unknown node types kept as read from a (hand-written) file may be
            # nested too deeply to be written: leave out what cannot be written.
            problems.clear()
            data = self._tree_data(problems, check_raw=True)
            write_json_atomic(path, data)
        blackboard_problems: list[str] = []
        self._blackboard.to_list(blackboard_problems)  # blackboard values that cannot be written (logged)
        problems.extend(blackboard_problems)
        self._blackboard_skipped.update(blackboard_problems)  # not logged again by later writes
        self._file_path = path
        self._set_log_file(path)
        self._blackboard_path = blackboard_path
        self._blackboard_save_pending = False  # written below, with the rest of the tree
        self._blackboard_timer.stop()
        self._blackboard_save_error = None
        self._set_tree_loaded(True)
        self._set_modified(False)
        self._update_window_title()
        blackboard_written = self._write_blackboard_file(force=True)  # failures leave the tree modified
        if report_problems and not blackboard_written:
            QMessageBox.warning(
                self, "Save Behavior Tree", f"The tree was saved, but its blackboard was not:\n{self._blackboard_save_error}"
            )
        self._log(LogLevel.INFO, f"Tree saved to '{path}'")
        for problem in problems:
            self._log(LogLevel.WARNING, f"{os.path.basename(path)}: {problem}")
        if problems and report_problems:
            QMessageBox.warning(
                self,
                "Save Behavior Tree",
                "The tree was saved, but some values cannot be stored in a file and were left out:\n\n"
                + "\n".join(f"- {problem}" for problem in problems),
            )
        self.treeSaved.emit(path)

    def ConfirmDiscardChanges(self) -> bool:
        """Ask to save unsaved changes (of the tree and of the sub-trees shown with Zoom In).
        Returns False if the user cancelled."""
        if not (self._tree_loaded and self.IsModified()):
            return True
        levels = [level for level in self._levels if level.modified]
        if levels:
            names = [os.path.basename(self._file_path or "")] if self._modified else []
            names += [os.path.basename(level.path) for level in levels]
            text = "There are unsaved changes in " + ", ".join(f"'{name}'" for name in names) + ". Save them first?"
        else:
            text = "The behavior tree has unsaved changes. Save them first?"
        answer = QMessageBox.question(
            self,
            "Unsaved Changes",
            text,
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            for level in reversed(levels):
                if not self._save_level_interactively(level):
                    return False
            if not self._modified:
                return True
            if self._file_path:
                current = os.path.abspath(self._file_path)
                if not self._confirm_replacing("Save Behavior Tree", current, tree_file_paths(current)[0]):
                    # A file of an earlier version whose <name>_BT.json exists: choose where to save.
                    return self.SaveTree()
                try:
                    self._require_gui_thread("SaveTree")
                    self._save_tree(current, report_problems=True)
                    return True
                except SAVE_ERRORS as error:
                    QMessageBox.critical(self, "Save Behavior Tree", f"Could not save '{self._file_path}':\n{error}")
                    return False
            return self.SaveTree()
        return answer == QMessageBox.StandardButton.Discard

    def Shutdown(self) -> None:
        """Stop execution for good and release the py_trees blackboard keys of this widget.

        Call it when the window hosting the widget is closed. Afterwards the tree can
        no longer be executed and the blackboard no longer accepts changes.
        """
        self._require_gui_thread("Shutdown")
        if self._shut_down:
            return
        self._shut_down = True
        self._executor.shutdown()
        # The loop in progress ends now, also when the stop has to wait for the current tick
        # (Shutdown called from a node): entries with Reset get their loop-start value back,
        # and that is written below, before the blackboard goes away.
        self._executor._end_loop()
        self._flush_blackboard_save()
        self._blackboard.dispose()
        self._tree_log.close()
        if shiboken6.isValid(self):
            self._update_buttons()

    # ================================================================== accessors (tests / customisation)
    def view(self) -> TreeView:
        """The QGraphicsView showing the tree shown (a sub-tree's own view while zoomed in)."""
        return self._view

    def mainView(self) -> TreeView:  # noqa: N802
        """The QGraphicsView of the tree itself (hidden while a sub-tree is shown)."""
        return self._main_view

    def blackboardView(self) -> BlackboardView:  # noqa: N802
        return self._blackboard_view

    def blackboardStore(self) -> BlackboardStore:  # noqa: N802
        return self._blackboard

    def executor(self) -> TreeExecutor:
        return self._executor

    def button(self, name: str) -> QPushButton:
        """One of the buttons: Execute, Pause, Stop, Reset, Configure, Save, Load, New, GoBack."""
        return self._buttons[name]

    def loopExecutionCheckBox(self) -> QCheckBox:  # noqa: N802
        """The "Loop Execution" check box of the execution controls."""
        return self._loop_box

    # ================================================================== internals: dialogs
    def _run_dialog(self, dialog: QDialog) -> bool:
        """Show a modal dialog (separate method so tests can drive dialogs)."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _suggested_path(self, file_name: str) -> str:
        directory = os.path.dirname(self._file_path) if self._file_path else os.getcwd()
        return os.path.join(directory, file_name) if file_name else directory

    def _ask_save_path(self, caption: str, suggestion: str) -> str:
        """Ask for the file of a tree; returns its tree file (``<name>_BT.json``), or "" if cancelled.

        The dialog confirms replacing the file chosen in it; when the tree file is another one
        (e.g. ``robot_BT.json`` for ``robot.json``) and exists, replacing it is confirmed here.
        """
        chosen, _ = QFileDialog.getSaveFileName(self, caption, suggestion, self.FILE_FILTER)
        if not chosen:
            return ""
        chosen = os.path.abspath(chosen)  # the file the dialog confirmed replacing, if it exists
        path = chosen if os.path.splitext(chosen)[1] else chosen + JSON_EXTENSION
        tree_path = tree_file_paths(path)[0]
        return tree_path if self._confirm_replacing(caption, chosen, tree_path) else ""

    def _confirm_replacing(self, caption: str, chosen: str, tree_path: str) -> bool:
        """When the tree file ``tree_path`` written for the chosen file ``chosen`` is another,
        existing file, ask whether to replace it. Returns True to go on."""
        if os.path.normcase(tree_path) == os.path.normcase(chosen) or not os.path.exists(tree_path):
            return True
        answer = QMessageBox.question(
            self,
            caption,
            f"A tree is stored in two files, '{os.path.basename(tree_path)}' and "
            f"'{os.path.basename(blackboard_path_for(tree_path))}'.\n\n"
            f"'{os.path.basename(tree_path)}' already exists. Replace it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_save_clicked(self) -> None:
        if self._levels:
            self._save_level_interactively(self._levels[-1])
        else:
            self.SaveTree()

    def _on_go_back_clicked(self) -> None:
        self.GoBack()

    def _on_load_clicked(self) -> None:
        self.LoadTree()

    def _on_new_clicked(self) -> None:
        self.NewTree()

    # ================================================================== internals: state
    def _set_tree_loaded(self, loaded: bool) -> None:
        self._tree_loaded = loaded
        self._main_view.setEnabled(loaded)
        self._update_buttons()

    def _set_modified(self, modified: bool) -> None:
        """Mark the tree itself as modified or not (sub-trees: _set_level_modified)."""
        self._modified = bool(modified)
        self._announce_modified()

    def _set_level_modified(self, level: _ZoomLevel, modified: bool) -> None:
        level.modified = bool(modified)
        self._announce_modified()

    def _announce_modified(self) -> None:
        modified = self.IsModified()
        if modified != self._reported_modified:
            self._reported_modified = modified
            self.modifiedChanged.emit(modified)

    def _mark_modified(self, *_args) -> None:
        if not self._loading:
            self._set_modified(True)

    def _level_of_view(self, view: Any) -> _ZoomLevel | None:
        return next((level for level in self._levels if level.view is view), None)

    def _on_level_view_modified(self) -> None:
        level = self._level_of_view(self.sender())
        if level is not None and not self._loading:
            self._set_level_modified(level, True)

    def _on_data_changed(self, runtime: bool) -> None:
        # Values written while executing (or by worker threads) are not edits of the tree file.
        if runtime or self._loading:
            return
        node = self.sender()
        level = self._level_of_view(getattr(node, "_canvas", None)) if isinstance(node, NodeWidget) else None
        if level is not None:
            self._set_level_modified(level, True)
        else:
            self._mark_modified()

    def _on_blackboard_changed(self, runtime: bool) -> None:
        if self._loading:
            return  # the blackboard of the file being installed
        if self._writes_blackboard():
            self._schedule_blackboard_save()  # changes by the user and by the tree are written to the file
        elif not runtime:
            self._mark_modified()
        if not runtime and self._executor.state() is ExecutionState.IDLE:
            # A deliberate edit after a run: Reset must not silently undo it.
            self._executor.discard_snapshot()

    def _on_blackboard_edited(self, *_args) -> None:
        if not self._writes_blackboard():
            self._mark_modified()
        if self._executor.state() is ExecutionState.IDLE:
            self._executor.discard_snapshot()

    def _on_blackboard_reordered(self) -> None:
        if self._loading:
            return
        if self._writes_blackboard():
            self._schedule_blackboard_save()  # the order is part of the blackboard file
        else:
            self._mark_modified()

    def _on_blackboard_reset_changed(self, _name: str) -> None:
        # A Reset flag is part of the blackboard file; switching it is not a value edit.
        if self._loading:
            return
        if self._writes_blackboard():
            self._schedule_blackboard_save()
        else:
            self._mark_modified()

    # ================================================================== internals: blackboard file updates
    def _writes_blackboard(self) -> bool:
        """True while blackboard changes are written to the blackboard file (a tree was created, loaded
        or saved, and the blackboard was not released by Shutdown, which writes what is still due)."""
        return self._tree_loaded and bool(self._blackboard_path) and not self._blackboard.is_disposed()

    def _schedule_blackboard_save(self) -> None:
        """Write the blackboard soon: at once from the event loop, or at the end of the current interval."""
        self._blackboard_save_pending = True
        if not self._blackboard_timer.isActive():
            elapsed_ms = (time.monotonic() - self._last_blackboard_save) * 1000.0
            self._blackboard_timer.start(max(0, math.ceil(self.BLACKBOARD_SAVE_INTERVAL_MS - elapsed_ms)))

    def _flush_blackboard_save(self) -> None:
        """Write blackboard changes that are still due (before the file or the blackboard goes away)."""
        if self._blackboard_save_pending:
            self._save_blackboard_now()

    def _save_blackboard_now(self) -> None:
        """Write the blackboard to the blackboard file (the tree file is not touched)."""
        self._blackboard_timer.stop()
        if not self._blackboard_save_pending:
            return
        self._blackboard_save_pending = False
        if not self._blackboard_path or self._blackboard.is_disposed():
            return
        self._write_blackboard_file(force=False)

    def _write_blackboard_file(self, force: bool) -> bool:
        """Write the blackboard file now; returns False if that failed (reported as for any
        blackboard write: logged, ``blackboardSaveFailed``, and the tree counts as modified).
        Without ``force`` nothing is written when the file already holds the blackboard."""
        path = self._blackboard_path
        revision = self._blackboard.revision(saved=True)  # read first: later changes are written next time
        if not force and revision == self._blackboard_written_revision and self._blackboard_save_error is None:
            return True  # nothing the file holds changed (e.g. only Object entries changed)
        problems: list[str] = []
        blackboard = self._blackboard.to_list(problems, quiet=True)
        for problem in problems:  # e.g. a value of a type that cannot be written as JSON
            if problem not in self._blackboard_skipped:
                self._blackboard_skipped.add(problem)
                log.warning("%s", problem)
        if not force and blackboard == self._blackboard_written and self._blackboard_save_error is None:
            self._blackboard_written_revision = revision
            return True  # e.g. a value was changed and changed back
        self._last_blackboard_save = time.monotonic()
        try:
            write_json_atomic(path, blackboard_document(blackboard))
        except SAVE_ERRORS as error:
            message = f"Could not write the blackboard to '{path}': {error}"
            if message != self._blackboard_save_error:
                log.warning("%s", message)
                self._log(LogLevel.WARNING, message)
            self._blackboard_save_error = message
            self._set_modified(True)  # the changes are not in the file: Save and the close prompt offer to save them
            self.blackboardSaveFailed.emit(message)
            return False
        self._blackboard_written = blackboard
        self._blackboard_written_revision = revision
        if self._blackboard_save_error is not None:
            log.info("the blackboard was written to '%s' again", path)
            self._log(LogLevel.INFO, f"The blackboard was written to '{path}' again")
            self._blackboard_save_error = None
        self.blackboardSaved.emit(path)
        return True

    def _update_buttons(self) -> None:
        state = self._executor.state()
        loaded = self._tree_loaded
        runnable = loaded and not self._shut_down
        self._buttons["Execute"].setEnabled(runnable and state is not ExecutionState.RUNNING)
        self._buttons["Pause"].setEnabled(runnable and state is ExecutionState.RUNNING)
        self._buttons["Stop"].setEnabled(runnable and state is not ExecutionState.IDLE)
        self._buttons["Reset"].setEnabled(runnable)
        self._buttons["Configure"].setEnabled(loaded)
        self._buttons["Save"].setEnabled(loaded)
        self._buttons["Load"].setEnabled(True)
        self._buttons["New"].setEnabled(True)
        self._buttons["GoBack"].setEnabled(bool(self._levels))
        self._buttons["Execute"].setToolTip("Resume execution" if state is ExecutionState.PAUSED else "Execute the tree")
        self._loop_box.setEnabled(loaded)

    def _sync_loop_box(self) -> None:
        """Make the Loop Execution check box show ``TreeConfig.repeat`` (without reacting to it)."""
        box = self._loop_box
        if shiboken6.isValid(box) and box.isChecked() != self._config.repeat:
            with QSignalBlocker(box):
                box.setChecked(self._config.repeat)

    def _update_window_title(self) -> None:
        if self._file_path:
            # The tree file, followed by the sub-tree files zoomed into ("robot_BT.json > patrol_BT.json").
            names = [os.path.basename(path) for path in (self._file_path, *(level.path for level in self._levels))]
            # "[*]" is Qt's window-modified placeholder; "[*][*]" displays a literal "[*]".
            name = " > ".join(names).replace("[*]", "[*][*]")
            self.window().setWindowTitle(self.WINDOW_TITLE_FORMAT.format(filename=name))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # The widget may have been embedded in its window after the tree was loaded.
        self._update_window_title()

    def _on_view_shown(self) -> None:
        if self._view_target_pending:
            # The final viewport size is known once the event loop has run.
            self._view_timer.start(0)

    def _on_view_navigated(self) -> None:
        # The user panned or zoomed: never jump back to a pending target.
        self._view_target_pending = False

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.isWindow():
            self._flush_blackboard_save()
        if self.isWindow() and not self.ConfirmDiscardChanges():
            event.ignore()
            return
        super().closeEvent(event)
        if self.isWindow():
            if self.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose):
                self.Shutdown()
            else:
                self.Stop()  # a closed window may be shown again

    def _on_execution_state(self, state: str) -> None:
        for view in self._all_views():
            view.set_locked(state != ExecutionState.IDLE.value)
        self._update_buttons()
        self.executionStateChanged.emit(state)

    def _on_node_added(self, node: NodeWidget) -> None:
        node.changed.connect(self._on_data_changed)

    # ================================================================== internals: view position
    def _set_view_target(self, center: QPointF | None) -> None:
        """Centre the view on ``center`` (None: show the root at the top) now and once shown."""
        self._view_target = QPointF(center) if center is not None else None
        self._view_target_pending = True
        self._apply_view_target()

    def _apply_pending_view_target(self) -> None:
        if self._view_target_pending:
            self._view_settled = True
            self._apply_view_target()

    def _apply_view_target(self) -> None:
        self._center_view(self._main_view, self._view_target)
        if self._view_settled and self._main_view.isVisible():
            self._view_target_pending = False

    @staticmethod
    def _center_view(view: TreeView, center: QPointF | None) -> None:
        """Centre ``view`` on ``center``, or (None) show its root at the top."""
        if center is not None:
            view.centerOn(center)
            return
        root = view.root()
        if root is not None and root._item is not None:
            rect = root._item.sceneBoundingRect()
            viewport_height = max(view.viewport().height(), 200)
            zoom = view.zoom() or 1.0
            view.centerOn(QPointF(rect.center().x(), rect.top() - 40.0 / zoom + viewport_height / (2.0 * zoom)))

    # ================================================================== internals: (de)serialisation
    def _new_tree_data(self) -> dict:
        config = TreeConfig()
        return {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "config": config.to_dict(),
            "nodes": [
                {
                    "id": uuid.uuid4().hex,
                    "type": "Root",
                    "title": "Root",
                    "x": 0.0,
                    "y": 0.0,
                    "composite": SEQUENCE,
                    "memory": config.default_memory,
                }
            ],
            "connections": [],
        }

    def _tree_data(
        self,
        problems: list[str] | None = None,
        quiet: bool = False,
        check_raw: bool = False,
        level: _ZoomLevel | None = None,
    ) -> dict:
        """The tree (or the sub-tree of ``level``) as the JSON-compatible data of a tree file (the
        blackboard is in its own file, see :meth:`_write_blackboard_file`). Unsaveable values are
        skipped (see ``problems``); ``quiet`` suppresses the log messages (used for internal
        snapshots); ``check_raw`` also skips data of unknown node types that json cannot write."""
        problems = problems if problems is not None else []
        view = level.view if level is not None else self._main_view
        config = level.config if level is not None else self._config
        nodes = [self._node_data(node, problems, quiet, check_raw) for node in view.nodes()]
        connections = []
        for node in view.nodes():
            for child in node.GetChildren():
                connections.append({"parent": node.GetId(), "child": child.GetId()})
        center = view.view_center()
        return {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "config": config.to_dict(),
            "view": {"zoom": round(view.zoom(), 4), "center": [round(center.x(), 2), round(center.y(), 2)]},
            "nodes": nodes,
            "connections": connections,
        }

    @staticmethod
    def _node_data(node: NodeWidget, problems: list[str], quiet: bool = False, check_raw: bool = False) -> dict:
        """One node as JSON-compatible data (an item of a tree file's "nodes" list); see _tree_data."""
        warn = (lambda message: None) if quiet else log.warning

        def writable(what: str, value: Any) -> bool:
            if not check_raw or _json_writable(value):
                return True
            message = f"{what} of node {node.GetTitle()!r} was not saved: it is nested too deeply to be written"
            warn(message)
            problems.append(message)
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
                    message = f"field {key!r} of node {node.GetTitle()!r} was not saved: field names must be str"
                    warn(message)
                    problems.append(message)
                    continue
                try:
                    fields[key] = encode_value(value)
                except TypeError as error:
                    message = f"field {key!r} of node {node.GetTitle()!r} was not saved: {error}"
                    warn(message)
                    problems.append(message)
            extra["fields"] = fields
            selections = extra.get("field_selections")
            if isinstance(selections, dict):
                extra["field_selections"] = {k: v for k, v in selections.items() if k in fields}
        if isinstance(node, UnknownLeafNodeWidget):
            # Keep what the placeholder could not interpret exactly as it was read.
            extra.setdefault("fields", {}).update(
                {key: value for key, value in node._raw_fields.items() if writable(f"field {key!r}", value)}
            )
            selections = {  # as saved ...
                key: value
                for key, value in node._raw_selections.items()
                if writable(f"selection of field {key!r}", value)
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
        entry.update(extra)
        return entry

    # ================================================================== internals: copy / paste
    def _copy_nodes(self, nodes: Iterable[NodeWidget]) -> bool:
        """Put ``nodes`` (except the Root) and the connections between them on the clipboard."""
        wanted = set(nodes)
        copied = [node for node in self._view.nodes() if node in wanted and not isinstance(node, RootNodeWidget)]
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
        for check_raw in (False, True):
            # As when saving: data of unknown node types that json cannot write is left out.
            problems: list[str] = []
            data = {
                "format": CLIPBOARD_FORMAT,
                "version": FORMAT_VERSION,
                "nodes": [self._node_data(node, problems, quiet=True, check_raw=check_raw) for node in copied],
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

    @staticmethod
    def _clipboard_data() -> dict | None:
        """The nodes on the clipboard (a checked :meth:`_copy_nodes` document), or None."""
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

    def _paste_nodes(self) -> list[NodeWidget]:
        data = self._clipboard_data()
        if data is None:
            return []
        # New ids for the copies; the Root is never pasted.
        ids: dict[str, str] = {}
        items = []
        for item in data["nodes"]:
            if not isinstance(item, dict) or item.get("type") == "Root":
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
        plan = _TreePlan()
        try:
            self._prepare_nodes(plan, items, problems, with_root=False)
            self._prepare_connections(plan, connections, problems)
        except Exception:
            plan.discard()
            raise
        obstacles = [node._item.sceneBoundingRect() for node in self._view.nodes() if node._item is not None]
        prepared, plan.nodes = plan.nodes, []
        pasted: list[NodeWidget] = []
        try:
            # Added where the copied nodes were (their size is known once added), then moved
            # together to the nearest free area.
            for node, pos in prepared:
                self._view.add_node(node, pos)
                pasted.append(node)
        except Exception:
            for node, _ in prepared[len(pasted):]:
                if shiboken6.isValid(node):
                    node.deleteLater()
            raise
        offset = nearest_free_offset([node._item.sceneBoundingRect() for node in pasted], obstacles)
        for node in pasted:
            node._item.setPos(clamp_to_scene(node._item.pos() + offset)[0])
        by_id = {node.GetId(): node for node in pasted}
        for parent_id, child_id in plan.connections:
            self._view.connect_nodes(by_id[parent_id], by_id[child_id])
        # A copy of a group with a default name gets the next default name (group names are file names).
        for node in pasted:
            if isinstance(node, GroupNodeWidget) and default_group_number(node.GetTitle()) is not None:
                node._apply_title(self._next_group_name())
        self._view.select_nodes(pasted)
        for problem in problems:
            log.warning("paste: %s", problem)
        return pasted

    # ================================================================== sub-trees
    def _load_subtree(self, path: str) -> tuple[RootNodeWidget, list[NodeWidget]]:
        """Create the nodes of the tree file ``path`` for a Sub-Tree node: its Root and the nodes
        connected below it, linked as in the file but not shown (they are never added to a view).

        A sub-tree shown with Zoom In is taken as it is shown, with its unsaved changes. Children
        keep the order of the file (left to right by their saved positions). The nodes belong to
        this widget (``GetTree()``), so they use its blackboard. The caller deletes them.

        Raises:
            TreeFileError: the file cannot be read or is not a tree file.
        """
        level = self._open_level(path)
        data = self._tree_data(quiet=True, level=level) if level is not None else read_tree_file(path)
        name = os.path.basename(path)
        problems: list[str] = []
        plan = _TreePlan()
        try:
            self._prepare_nodes(plan, data.get("nodes", []), problems)
            self._prepare_connections(plan, data.get("connections", []), problems)
        except Exception as error:  # noqa: BLE001
            plan.discard()
            raise TreeFileError(f"'{name}' could not be loaded: {error}") from error
        prepared, plan.nodes = plan.nodes, []
        try:
            by_id = {node.GetId(): node for node, _ in prepared}
            for node, position in prepared:
                node._tree = self
                relayout(node)  # its size gives its place among its siblings, as in a view
                node._detached_order = (position.x() + node.width() / 2.0, position.y())
            for parent_id, child_id in plan.connections:
                parent, child = by_id[parent_id], by_id[child_id]
                with parent._lock:
                    parent._children.append(child)
                with child._lock:
                    child._parent = parent
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

    def _subtree_path_for(self, group: GroupNodeWidget) -> str:
        """The sub-tree file ``<group name>_BT.json`` a group is promoted to (next to the tree file).

        Raises:
            RuntimeError: the tree has no file yet.
            ValueError: the group name cannot be used as a file name.
        """
        shown = self._shown_path()
        if not shown:
            raise RuntimeError("the tree has no file yet: create, load or save it first")
        name = group.GetTitle().strip()
        if not name:
            raise ValueError("the group has no name; type one in its title bar")
        if INVALID_FILE_NAME_CHARACTERS.search(name) or name in (".", "..") or name.endswith("."):
            raise ValueError(
                f"the group name {name!r} cannot be used as a file name: it must not contain "
                '< > : " / \\ | ? * and must not end with a dot'
            )
        path = os.path.join(os.path.dirname(shown), f"{name}{TREE_FILE_SUFFIX}{JSON_EXTENSION}")
        key = os.path.normcase(os.path.realpath(path))
        # The tree's own file (also the one a file of an earlier version, robot.json, is saved to)
        # and the files of the sub-trees shown with Zoom In.
        own = {file_key(p) for p in self._open_paths()}
        if key in own:
            raise ValueError(
                f"the group name {name!r} names the file of this tree or of a tree containing it; rename the group"
            )
        return path

    def PromoteGroupToSubTree(self, group: GroupNodeWidget, overwrite: bool = False) -> SubTreeNodeWidget:
        """Turn ``group`` into a sub-tree: its contents are written to ``<group name>_BT.json``
        and the group is replaced by a Sub-Tree node running that file.

        ``group`` is a group of the tree shown (also of a sub-tree shown with :meth:`ZoomIn`).
        The file lies in the folder of that tree's file; no blackboard file is written for it (a
        sub-tree uses the blackboard of the tree that runs it). It gets a new Root with the
        group's top nodes as children, left to right: the nodes inside the group whose parent
        lies outside it. When they have a parent outside the group, the Root takes that
        parent's composite type and memory, and the Sub-Tree node is connected to that parent
        where the leftmost of them was; group contents without such a parent stay unconnected
        in the file. When no top node has a parent, all of them become children of the Root.
        Notes and groups inside the group are written to the file as well. The group, its
        contents and the connections to them are removed from this tree, which counts as
        modified; the new Sub-Tree node (titled with the group name) is selected and returned.

        Raises:
            RuntimeError: the tree is executing, or has no file yet.
            ValueError: the group cannot be promoted; the message says why (no node inside it,
                the Root inside it, a node inside with children outside, top nodes with
                different parents outside, or a name that cannot be a file name).
            FileExistsError: the file exists and ``overwrite`` is False.
            OSError: the file could not be written.
        """
        self._require_gui_thread("PromoteGroupToSubTree")
        self._view._check_unlocked()
        view = self._view
        if not isinstance(group, GroupNodeWidget) or group not in view.nodes():
            raise ValueError("only a group of this tree can be promoted to a sub-tree")
        path = self._subtree_path_for(group)
        name = group.GetTitle().strip()
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
        if os.path.exists(path) and not overwrite:
            raise FileExistsError(f"'{path}' already exists")

        # ---- the sub-tree file
        if isinstance(parent, CompositeNodeWidget):
            composite, memory = parent.GetCompositeType(), parent.GetMemory()
        else:
            composite, memory = SEQUENCE, self._config.default_memory
        area = QRectF()
        for node in nodes:
            area = area.united(node._item.sceneBoundingRect())
        children_area = QRectF()
        for node in root_children:
            children_area = children_area.united(node._item.sceneBoundingRect())
        probe = RootNodeWidget(composite, memory)
        relayout(probe)
        root_width = probe.width()
        probe.deleteLater()
        root_id = uuid.uuid4().hex
        root_item = {
            "id": root_id,
            "type": "Root",
            "title": "Root",
            "x": round(children_area.center().x() - root_width / 2.0, 2),
            "y": round(area.top() - 160.0, 2),
            "composite": composite,
            "memory": memory,
        }
        connections = [{"parent": root_id, "child": node.GetId()} for node in root_children]
        connections += [{"parent": node.GetId(), "child": child.GetId()} for node in nodes for child in node.GetChildren()]
        config = TreeConfig(tick_interval_ms=self._config.tick_interval_ms, default_memory=self._config.default_memory)
        center = area.united(QRectF(root_item["x"], root_item["y"], root_width, 1.0)).center()
        problems: list[str] = []
        for check_raw in (False, True):
            problems.clear()
            document = {
                "format": FORMAT_NAME,
                "version": FORMAT_VERSION,
                "config": config.to_dict(),
                "view": {"zoom": round(view.zoom(), 4), "center": [round(center.x(), 2), round(center.y(), 2)]},
                "nodes": [root_item, *(self._node_data(node, problems, quiet=True, check_raw=check_raw) for node in members)],
                "connections": connections,
            }
            try:
                write_json_atomic(path, document)
                break
            except RecursionError:
                if check_raw:
                    raise ValueError("the group's nodes hold data nested too deeply to be written") from None
        for problem in problems:
            log.warning("promote %s: %s", name, problem)
            self._log(LogLevel.WARNING, f"promote {name}: {problem}")

        # ---- replace the group by a Sub-Tree node running the file
        anchor = root_children[0] if root_children else tops[0]
        anchor_rect = anchor._item.sceneBoundingRect()
        for node in [group, *members]:
            if node in view.nodes():
                view.remove_node(node)
        subtree = SubTreeNodeWidget()
        subtree._apply_title(name)
        subtree.SetFile(os.path.basename(path))
        item = view.add_node(subtree, anchor_rect.topLeft())
        subtree._notify_geometry()
        item.setPos(clamp_to_scene(QPointF(anchor_rect.center().x() - subtree.width() / 2.0, anchor_rect.top()))[0])
        if parent is not None:
            view.connect_nodes(parent, subtree)
        view.select_nodes([subtree])
        self._log(LogLevel.INFO, f"Group '{name}' promoted to the sub-tree '{path}'")
        self.subTreePromoted.emit(subtree, path)
        return subtree

    def _promote_group_interactively(self, group: GroupNodeWidget) -> SubTreeNodeWidget | None:
        """Promote to Sub-Tree from the group's menu: confirms replacing an existing file and reports problems."""
        caption = "Promote to Sub-Tree"
        try:
            path = self._subtree_path_for(group)
            overwrite = False
            if os.path.exists(path):
                answer = QMessageBox.question(
                    self,
                    caption,
                    f"'{os.path.basename(path)}' already exists. Replace it with the group's nodes?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return None
                overwrite = True
            return self.PromoteGroupToSubTree(group, overwrite=overwrite)
        except (ValueError, RuntimeError) as error:
            QMessageBox.warning(self, caption, f"The group cannot be promoted to a sub-tree:\n{error}")
        except OSError as error:
            QMessageBox.critical(self, caption, f"The sub-tree file could not be written:\n{error}")
        return None

    # ================================================================== zoom into sub-trees
    def ZoomIn(self, node: SubTreeNodeWidget, create: bool = False) -> None:
        """Show the tree of the Sub-Tree node ``node`` in place of the tree shown (its "Zoom In"
        menu item); :meth:`GoBack` returns to the tree containing it.

        The sub-tree can be edited as any tree; the Save button (:meth:`SaveSubTree`) writes it
        to its file. Zooming in again from a Sub-Tree node inside it shows a sub-tree of the
        sub-tree, and so on. The blackboard stays the tree's own; executing runs the tree (a
        sub-tree that is shown runs as it is shown, unsaved changes included) and the nodes of
        the sub-tree shown show their status when it is run by ``node``. While the tree
        executes, the sub-tree can be looked at, but not changed, as the tree.

        With ``create``, a missing file is created first, as a tree holding only a Root (named
        ``<name>_BT.json`` for the name in the node's File box).

        Raises:
            ValueError: ``node`` is not a Sub-Tree node of the tree shown, it has no file, or
                its file is already shown (a tree containing itself).
            TreeFileError: the file does not exist (and ``create`` is False) or is not a tree file.
            OSError: the file could not be created.
        """
        self._require_gui_thread("ZoomIn")
        self._zoom_in(node, create)

    def GoBack(self, save: bool | None = None) -> bool:
        """Show the tree containing the sub-tree shown again (the Go Back button). Returns False
        when no sub-tree is shown or the user cancelled.

        When the sub-tree shown has unsaved changes, ``save`` decides: True writes them to its
        file first, False discards them, and None (the button) asks the user.

        Raises:
            OSError: ``save`` is True and the file could not be written (the sub-tree stays shown).
        """
        self._require_gui_thread("GoBack")
        if not self._levels:
            return False
        level = self._levels[-1]
        if level.modified:
            if save is None:
                answer = QMessageBox.question(
                    self,
                    "Go Back",
                    f"'{os.path.basename(level.path)}' has unsaved changes. Save them first?",
                    QMessageBox.StandardButton.Save
                    | QMessageBox.StandardButton.Discard
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Save,
                )
                if answer == QMessageBox.StandardButton.Save:
                    if not self._save_level_interactively(level):
                        return False
                elif answer != QMessageBox.StandardButton.Discard:
                    return False
            elif save:
                self._save_level(level, report_problems=False)
        if self._levels and self._levels[-1] is level:
            self._close_level(level)
            self._log(LogLevel.INFO, f"Back to '{self._shown_path()}'")
            self._after_zoom_change()
        return True

    def GetZoomDepth(self) -> int:
        """How many sub-trees deep the tree shown is (0: the tree itself is shown)."""
        return len(self._levels)

    def GetZoomPath(self) -> list[str]:
        """The tree files from the tree itself to the tree shown (one file while not zoomed in;
        empty before a tree was created or loaded)."""
        if not self._file_path:
            return []
        return [self._file_path, *(level.path for level in self._levels)]

    def SaveSubTree(self) -> bool:
        """Write the sub-tree shown (see :meth:`ZoomIn`) to its file, as the Save button does
        while a sub-tree is shown. Its file keeps its name; no blackboard file is written (a
        sub-tree uses the blackboard of the tree running it). Returns True.

        Raises:
            RuntimeError: no sub-tree is shown.
            OSError, ValueError, TypeError: the file could not be written.
        """
        self._require_gui_thread("SaveSubTree")
        if not self._levels:
            raise RuntimeError("no sub-tree is shown; SaveTree saves the tree")
        self._save_level(self._levels[-1], report_problems=False)
        return True

    # ------------------------------------------------------------------ zoom internals
    def _all_views(self) -> list[TreeView]:
        return [self._main_view, *(level.view for level in self._levels)]

    def _all_nodes(self) -> list[NodeWidget]:
        """The nodes of the tree and of the sub-trees shown with Zoom In."""
        return [node for view in self._all_views() for node in view.nodes()]

    def _main_root(self) -> RootNodeWidget | None:
        """The Root of the tree itself (the tree that is executed)."""
        return self._main_view.root()

    def _shown_path(self) -> str | None:
        """The file of the tree shown."""
        return self._levels[-1].path if self._levels else self._file_path

    def _open_paths(self) -> list[str]:
        """The files of the tree (also the one a file of an earlier version is saved to) and of
        the sub-trees shown."""
        paths = [self._file_path, tree_file_paths(self._file_path)[0]] if self._file_path else []
        return paths + [level.path for level in self._levels]

    def _open_level(self, path: str) -> _ZoomLevel | None:
        """The zoom level showing the tree file ``path``, if any."""
        if not self._levels:
            return None
        key = file_key(path)
        return next((level for level in self._levels if file_key(level.path) == key), None)

    def _shown_twins(self) -> dict[tuple[tuple[str, ...], str], NodeWidget]:
        """``{(scope, node id): node}`` of the nodes of the sub-trees shown: the executor shows the
        status of a hidden sub-tree node it runs on the node with the same place."""
        return {(level.scope, node.GetId()): node for level in self._levels for node in level.view.nodes()}

    def _zoom_in(self, node: SubTreeNodeWidget, create: bool = False) -> list[str]:
        """ZoomIn; returns the problems found in the file (logged as warnings)."""
        if not self._tree_loaded:
            raise RuntimeError("no tree is loaded: create or load one first")
        if not isinstance(node, SubTreeNodeWidget) or node not in self._view.nodes():
            raise ValueError("only a Sub-Tree node of the tree shown can be zoomed into")
        path = node.GetResolvedFile()
        if path is None:
            raise ValueError("the Sub-Tree node has no file; type a file name in its File box or choose one")
        if not os.path.isfile(path):
            if not create:
                raise TreeFileError(f"'{path}' does not exist")
            path = tree_file_paths(path)[0]
            if not os.path.isfile(path):
                write_json_atomic(path, self._new_tree_data())
                self._log(LogLevel.INFO, f"New sub-tree created: '{path}'")
        if file_key(path) in {file_key(open_path) for open_path in self._open_paths()}:
            raise ValueError(
                f"'{os.path.basename(path)}' is the file of this tree or of a tree containing it; "
                "a sub-tree cannot contain itself"
            )
        data = read_tree_file(path)
        problems: list[str] = []
        try:
            plan = self._prepare_tree(data, problems)
        except TreeFileError:
            raise
        except Exception as error:  # noqa: BLE001
            raise TreeFileError(f"'{os.path.basename(path)}' could not be loaded: {error}") from error
        view = TreeView(self, self._frame_view)
        view.base_folder = os.path.dirname(path)  # relative sub-tree file names inside it
        view.hide()
        self._loading = True
        try:
            self._install_nodes(view, plan)
        except Exception as error:  # noqa: BLE001
            plan.discard()
            view.clear()
            view.deleteLater()
            raise TreeFileError(f"'{os.path.basename(path)}' could not be loaded: {error}") from error
        finally:
            self._loading = False
        parent_scope = self._levels[-1].scope if self._levels else ()
        level = _ZoomLevel(view, path, plan.config, (*parent_scope, node.GetId()), node)
        level.legacy_blackboard = data.get("blackboard")
        view.modified.connect(self._on_level_view_modified)
        view.nodeAdded.connect(self._on_node_added)
        view.nodeSelectionChanged.connect(self.nodeSelectionChanged)
        for added in view.nodes():
            self._on_node_added(added)  # added before the connection above
        view.set_locked(self._executor.state() is not ExecutionState.IDLE)
        previous = self._view
        self._levels.append(level)
        self._frame_layout.addWidget(view)
        previous.hide()
        view.show()
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        view.set_zoom(plan.zoom if plan.zoom is not None else 1.0)
        self._center_view(view, plan.center)
        # Centred again once the view has its final size (from the event loop).
        center = QPointF(plan.center) if plan.center is not None else None
        QTimer.singleShot(0, view, lambda: self._center_view(view, center) if self._level_of_view(view) else None)
        for problem in problems:
            log.warning("%s: %s", os.path.basename(path), problem)
            self._log(LogLevel.WARNING, f"{os.path.basename(path)}: {problem}")
        self._log(LogLevel.INFO, f"Zoomed into the sub-tree '{path}' of '{node.GetTitle()}'")
        self._after_zoom_change()
        return problems

    def _zoom_in_interactively(self, node: SubTreeNodeWidget) -> bool:
        """Zoom In from the node's menu: offers to create a missing file and reports problems."""
        caption = "Zoom In"
        try:
            path = node.GetResolvedFile()
            create = False
            if path is not None and not os.path.isfile(path):
                answer = QMessageBox.question(
                    self,
                    caption,
                    f"'{os.path.basename(tree_file_paths(path)[0])}' does not exist yet.\n\n"
                    "Create it as a new sub-tree and show it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return False
                create = True
            problems = self._zoom_in(node, create)
        except (ValueError, RuntimeError, TreeFileError, OSError) as error:
            QMessageBox.warning(self, caption, f"The sub-tree cannot be shown:\n{error}")
            return False
        if problems:
            QMessageBox.warning(
                self,
                caption,
                f"'{os.path.basename(self._shown_path() or '')}' was loaded with problems:\n\n"
                + "\n".join(f"- {problem}" for problem in problems),
            )
        return True

    def _after_zoom_change(self) -> None:
        self._update_buttons()
        self._update_window_title()
        self._announce_modified()
        self.nodeSelectionChanged.emit()
        self.zoomChanged.emit(len(self._levels), self._shown_path() or "")

    def _close_level(self, level: _ZoomLevel) -> None:
        """Stop showing ``level`` (the last one) and show the tree below it; its changes are dropped."""
        self._levels.remove(level)
        view = level.view
        self._frame_layout.removeWidget(view)
        view.hide()
        view.clear()
        view.deleteLater()
        shown = self._view
        shown.show()
        shown.setFocus(Qt.FocusReason.OtherFocusReason)

    def _close_all_levels(self) -> None:
        """Show the tree itself again; unsaved changes of the sub-trees shown are discarded."""
        if not self._levels:
            return
        while self._levels:
            self._close_level(self._levels[-1])
        self._after_zoom_change()

    def _save_level(self, level: _ZoomLevel, report_problems: bool) -> list[str]:
        """Write the sub-tree of ``level`` to its file; returns what was left out."""
        problems: list[str] = []
        data = self._tree_data(problems, level=level)
        if level.legacy_blackboard is not None:
            data["blackboard"] = level.legacy_blackboard  # kept as it was read
        try:
            write_json_atomic(level.path, data)
        except RecursionError:
            problems.clear()
            data = self._tree_data(problems, check_raw=True, level=level)
            if level.legacy_blackboard is not None:
                data["blackboard"] = level.legacy_blackboard
            write_json_atomic(level.path, data)
        self._set_level_modified(level, False)
        self._log(LogLevel.INFO, f"Sub-tree saved to '{level.path}'")
        for problem in problems:
            self._log(LogLevel.WARNING, f"{os.path.basename(level.path)}: {problem}")
        if problems and report_problems:
            QMessageBox.warning(
                self,
                "Save Sub-Tree",
                "The sub-tree was saved, but some values cannot be stored in a file and were left out:\n\n"
                + "\n".join(f"- {problem}" for problem in problems),
            )
        self.subTreeSaved.emit(level.path)
        return problems

    def _save_level_interactively(self, level: _ZoomLevel) -> bool:
        try:
            self._save_level(level, report_problems=True)
        except SAVE_ERRORS as error:
            QMessageBox.critical(self, "Save Sub-Tree", f"Could not save '{level.path}':\n{error}")
            return False
        return True

    def _prepare_tree(self, data: dict, problems: list[str], blackboard: Any = None) -> _TreePlan:
        """Validate ``data`` (a tree file) and the ``blackboard`` items (of its blackboard file; None:
        none) and create (but do not add) its nodes. Nothing visible changes."""
        plan = _TreePlan(config=TreeConfig.from_dict(data.get("config")))
        try:
            self._prepare_nodes(plan, data.get("nodes", []), problems)
            self._prepare_connections(plan, data.get("connections", []), problems)
            plan.blackboard, plan.resets, blackboard_problems = BlackboardStore.parse_items(
                [] if blackboard is None else blackboard
            )
            problems.extend(blackboard_problems)
            view = data.get("view")
            if isinstance(view, dict):
                zoom = view.get("zoom")
                if is_finite_number(zoom) and zoom > 0:
                    plan.zoom = float(zoom)
                center = view.get("center")
                if isinstance(center, list) and len(center) == 2 and all(is_finite_number(v) for v in center):
                    plan.center = QPointF(float(center[0]), float(center[1]))
        except Exception:
            plan.discard()
            raise
        return plan

    def _prepare_nodes(self, plan: _TreePlan, items: Any, problems: list[str], with_root: bool = True) -> None:
        """Create the nodes of ``items`` into ``plan``. ``with_root`` False: no Root is created
        (Root items are skipped and none is added when missing), as for pasted nodes."""
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
            if type_name == "Root":
                if root_seen or not with_root:
                    problems.append(f"extra root node {label} was skipped")
                    continue
                root_seen = True
                composite = item.get("composite")
                node: NodeWidget = RootNodeWidget(composite if composite in COMPOSITE_TYPES else SEQUENCE)
            elif type_name in COMPOSITE_TYPES:
                node = CompositeNodeWidget(type_name)
            elif type_name == NEGATION:
                node = NegationNodeWidget()
            elif (
                type_name in YIELDING_TYPE_NAMES
                and self._has_builtin(type_name)
                and not _saved_as_builtin(type_name, item)
            ):
                # Saved by an application's own leaf type of that name, which is not registered now.
                problems.append(f"node type {type_name!r} is not registered; a placeholder node was created")
                node = UnknownLeafNodeWidget(type_name)
            elif type_name == NOTES and self._offers_notes():
                node = NotesNodeWidget()
            elif type_name == GROUP and self._offers_groups():
                node = GroupNodeWidget()
            elif type_name in self._node_types:
                try:
                    node = self._node_types[type_name]()
                except Exception as error:  # noqa: BLE001 - user class failed to build
                    log.exception("could not create node of type %r", type_name)
                    problems.append(
                        f"node {label}: {type_name} could not be created ({error}); a placeholder was used"
                    )
                    node = UnknownLeafNodeWidget(type_name)
            else:
                problems.append(f"node type {type_name!r} is not registered; a placeholder node was created")
                node = UnknownLeafNodeWidget(type_name)
            plan.nodes.append((node, QPointF(0.0, 0.0)))  # registered for cleanup before anything can fail
            seen_ids.add(node_id)
            node._id = node_id
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
            plan.nodes[-1] = (node, position)
        if not root_seen and with_root:
            problems.append("the file has no root node; a new root was created")
            plan.nodes.insert(0, (RootNodeWidget(), QPointF(0.0, 0.0)))

    @staticmethod
    def _prepare_connections(plan: _TreePlan, items: Any, problems: list[str]) -> None:
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

    def _install_plan(self, plan: _TreePlan) -> None:
        if self._executor.state() is not ExecutionState.IDLE:
            raise RuntimeError("execution could not be stopped")
        self._close_all_levels()
        view = self._main_view
        view.set_locked(False)
        view.clear()
        self._config = plan.config
        self._executor.set_interval(self._config.tick_interval_ms)
        self._tree_log.set_level(self._config.log_level)
        self._sync_loop_box()
        self._install_nodes(view, plan)
        store = self._blackboard
        wanted = [name for name, _, _ in plan.blackboard]
        kept = [name for name in store.names() if name not in wanted and store.type_of(name) == "Object"]
        with store.batch():  # the nodes that list entries refresh once, at the end
            for name in store.names():
                if name not in wanted and name not in kept:
                    store.remove(name)
            for name, type_name, value in plan.blackboard:
                store.replace(name, type_name, value)
                store.set_reset(name, name in plan.resets)
            store.reorder(wanted + kept)
        self._group_counter = 0  # default group names count from the groups of this tree
        view.set_zoom(plan.zoom if plan.zoom is not None else 1.0)
        self._set_view_target(plan.center)

    @staticmethod
    def _install_nodes(view: TreeView, plan: _TreePlan) -> None:
        """Add the nodes and connections of ``plan`` to ``view``."""
        by_id: dict[str, NodeWidget] = {}
        nodes, plan.nodes = plan.nodes, []
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

    def _apply_tree_data(
        self,
        data: dict,
        path: str,
        interactive: bool,
        blackboard: Any = None,
        blackboard_path: str | None = None,
        new_blackboard: bool = False,
    ) -> None:
        problems: list[str] = []
        try:
            plan = self._prepare_tree(data, problems, blackboard)
        except TreeFileError:
            raise
        except Exception as error:  # noqa: BLE001
            raise TreeFileError(f"'{os.path.basename(path)}' could not be loaded: {error}") from error
        plan.blackboard_path = blackboard_path or blackboard_path_for(path)
        plan.new_blackboard = new_blackboard

        if not self._executor.is_busy():
            self._executor.discard_snapshot()  # the blackboard is replaced by the file anyway
            self._executor.stop()  # carried out at once
        if self._executor.is_busy():
            # Called during a tick or an execution command (e.g. a node that loads the next
            # tree), or code reacting to the Stop above restarted execution from a post-tick
            # signal handler: stop, then replace the tree right after that tick.
            self._executor.stop()
            self._executor.discard_snapshot()  # the blackboard is replaced anyway
            self._executor.call_when_idle(
                functools.partial(self._install_loaded_tree, plan, path, interactive, problems, deferred=True)
            )
            return
        self._install_loaded_tree(plan, path, interactive, problems)

    @staticmethod
    def _reread_blackboard(plan: _TreePlan) -> None:
        """Take the entries of ``plan``'s blackboard file again (it was just written by this widget)."""
        try:
            items = read_blackboard_file(plan.blackboard_path)
        except TreeFileError:  # keep the entries read before
            log.debug("could not read the blackboard file again", exc_info=True)
            return
        plan.blackboard, plan.resets, _ = BlackboardStore.parse_items(items)

    def _install_loaded_tree(
        self, plan: _TreePlan, path: str, interactive: bool, problems: list[str], deferred: bool = False
    ) -> None:
        self._close_all_levels()  # sub-trees shown with Zoom In: their unsaved changes are discarded
        same_blackboard = self._same_file(plan.blackboard_path, self._blackboard_path)
        if plan.new_blackboard and same_blackboard:
            # A new tree of the same name: its (empty) blackboard file replaced the old one, which
            # must not get the old tree's pending changes.
            self._blackboard_save_pending = False
            self._blackboard_timer.stop()
        else:
            due = self._blackboard_save_pending
            self._flush_blackboard_save()  # changes still due for the file being replaced
            if same_blackboard and due and self._blackboard_save_error is None:
                # The tree being loaded is this one, and its blackboard file changed after it was
                # read (e.g. stopping the run reset entries): load the file as it is now.
                self._reread_blackboard(plan)
        previous_tree = previous_blackboard = None
        previous_resets: list[str] = []
        if self._main_view.nodes():
            try:
                previous_tree = self._tree_data(quiet=True)
                previous_blackboard = self._blackboard.snapshot()
                previous_resets = self._blackboard.reset_names()
            except Exception:  # noqa: BLE001
                log.debug("could not snapshot the current tree", exc_info=True)
        self._loading = True
        try:
            if self._executor.is_busy() or self._executor.state() is not ExecutionState.IDLE:
                self._executor.stop()
            self._install_plan(plan)
        except Exception as error:  # noqa: BLE001 - unexpected; put the previous tree back
            log.exception("loading %s failed", path)
            plan.discard()
            if previous_tree is not None:
                try:
                    self._install_plan(self._prepare_tree(previous_tree, []))
                    if previous_blackboard is not None:
                        self._blackboard.restore(previous_blackboard)
                        for name in previous_resets:
                            if self._blackboard.has(name):
                                self._blackboard.set_reset(name, True)
                except Exception:  # noqa: BLE001
                    log.exception("could not restore the previous tree")
            failure = TreeFileError(f"'{os.path.basename(path)}' could not be loaded: {error}")
            if not deferred:
                raise failure from error
            if interactive:
                QMessageBox.critical(self, "Load Behavior Tree", str(failure))
            return
        finally:
            self._loading = False

        self._file_path = path
        self._set_log_file(path)
        self._blackboard_path = plan.blackboard_path
        # What the blackboard file holds: the entries just read, so an unchanged blackboard is not
        # written. (For a file without blackboard file, the first change creates it.)
        file_exists = bool(plan.blackboard_path) and os.path.exists(plan.blackboard_path)
        self._blackboard_written_revision = self._blackboard.revision(saved=True) if file_exists else None
        self._blackboard_written = self._blackboard.to_list(quiet=True) if file_exists else None
        self._blackboard_save_pending = False  # the blackboard is the file's
        self._blackboard_timer.stop()
        self._blackboard_save_error = None
        self._blackboard_skipped.clear()
        self._set_tree_loaded(True)
        self._set_modified(False)
        self._update_buttons()
        self._update_window_title()
        self._log(LogLevel.INFO, f"{'New tree created' if plan.new_blackboard else 'Tree loaded'}: '{path}'")
        for problem in problems:
            log.warning("%s: %s", os.path.basename(path), problem)
            self._log(LogLevel.WARNING, f"{os.path.basename(path)}: {problem}")
        if problems and interactive:
            QMessageBox.warning(
                self,
                "Load Behavior Tree",
                f"'{os.path.basename(path)}' was loaded with problems:\n\n" + "\n".join(f"- {p}" for p in problems),
            )
        self.treeLoaded.emit(path)
