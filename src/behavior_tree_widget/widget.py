"""The :class:`BehaviorTreeWidget`: a tabbed editor and executor for py_trees behavior trees.

The widget ties the parts together; the work is done in modules of their own:

* :mod:`.registry` - the node types it offers and the leaf types applications register,
* :mod:`.tree_io` - tree file data to nodes and back; :mod:`.document` - the file state of a tree,
* :mod:`.zoom` - sub-trees shown with Zoom In; :mod:`.promote` - Promote to Sub-Tree,
* :mod:`.clipboard` - copy and paste; :mod:`.blackboard_file` - writing the blackboard file,
* :mod:`.canvas` - the view; :mod:`.execution` - running the tree; :mod:`.tree_log` - the log.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import warnings
from typing import Any, Iterable

import shiboken6
from PySide6.QtCore import QCoreApplication, QPointF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QFont
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
from .blackboard_file import SAVE_ERRORS, BlackboardFileWriter
from .canvas import TreeView, clamp_to_scene
from .clipboard import CLIPBOARD_FORMAT, CLIPBOARD_MIME_TYPE, copy_nodes, paste_nodes
from .config import ConfigureDialog, TreeConfig
from .document import TreeDocument
from .execution import ExecutionState, TreeExecutor
from .groups import GROUP, GroupNodeWidget, default_group_name, default_group_number
from .nodes import LeafNodeWidget, NodeWidget, RootNodeWidget, UnknownLeafNodeWidget
from .notes import NOTES, NotesNodeWidget
from .promote import INVALID_FILE_NAME_CHARACTERS, plan_promotion, replace_group, subtree_path, write_subtree
from .registry import (
    BUILTIN_LEAF_TYPES,
    PARENT_TYPES,
    RESERVED_TYPE_NAMES,
    YIELDING_TYPE_NAMES,
    NodeTypeRegistry,
    _global_node_types,
    _register,
    register_node_type,
    registered_node_types,
)
from .serialization import (
    JSON_EXTENSION,
    TREE_FILE_SUFFIX,
    TreeFileError,
    blackboard_document,
    blackboard_path_for,
    is_blackboard_file_name,
    read_blackboard_file,
    read_tree_file,
    tree_file_paths,
    tree_name,
    write_json_atomic,
    write_json_files_atomic,
)
from .subtree import SubTreeNodeWidget
from .tree_io import TreePlan, install_nodes, load_detached_tree, new_tree_data, prepare_tree
from .tree_log import PYTHON_LEVELS, START_FOLDER, LogLevel, TreeLogger, close_log_file, log_file_name
from .zoom import ZoomLevel, ZoomStack

log = logging.getLogger("behavior_tree_widget")

__all__ = ["BehaviorTreeWidget", "register_node_type", "registered_node_types"]

# Names other code imported from this module before it was split up.
_COMPATIBILITY_EXPORTS = (
    BUILTIN_LEAF_TYPES,
    CLIPBOARD_FORMAT,
    CLIPBOARD_MIME_TYPE,
    INVALID_FILE_NAME_CHARACTERS,
    PARENT_TYPES,
    RESERVED_TYPE_NAMES,
    YIELDING_TYPE_NAMES,
    _global_node_types,
)

BUTTON_NAMES = ("Execute", "Pause", "Stop", "Reset", "Configure", "Save", "Load", "New", "GoBack")
LOOP_CHECKBOX_NAME = "LoopExecution"
# Text-presentation symbols (U+FE0E) instead of the colour emoji of the .ui file, so disabled
# buttons are drawn greyed out like any other text.
BUTTON_GLYPHS = {
    "Execute": "▶︎",
    "Pause": "⏸︎",
    "Stop": "⏹︎",
    "Reset": "↻",
    "Configure": "⚙︎",
}
SYMBOL_FONTS = ["Segoe UI Symbol", "DejaVu Sans", "Noto Sans Symbols2", "Noto Sans Symbols", "Apple Symbols"]


def _deprecated_accessor(old: str, new: str) -> None:
    warnings.warn(
        f"BehaviorTreeWidget.{old}() is deprecated; use {new}() instead", DeprecationWarning, stacklevel=3
    )


def _cleanup_after_destroy(store: BlackboardStore, executor: TreeExecutor, log_closer) -> None:
    """Release resources of a BehaviorTreeWidget destroyed without Shutdown()."""
    close_log_file(log_closer)  # the log file is closed once what was logged is written
    executor.abandon()  # no more node code runs; running OnRun calls are cancelled
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
    #: Blackboard changes are written to the blackboard file at most this often (ms); changes
    #: made in between are written together at the end of the interval.
    BLACKBOARD_SAVE_INTERVAL_MS = 250
    #: Folder of the log files; None: the folder the application was started from.
    LOG_FOLDER: str | None = None

    def __init__(self, parent: QWidget | None = None, node_types: Iterable[type[LeafNodeWidget]] = ()):
        super().__init__(parent)
        self.setObjectName("BehaviorTreeWidget")
        self._gui_thread = threading.get_ident()
        self._registry = NodeTypeRegistry(_global_node_types)
        self._tree_loaded = False
        self._reported_modified = False  # IsModified() as last announced by modifiedChanged
        self._levels = ZoomStack()  # sub-trees shown with Zoom In, the one shown last at the top
        self._tree_log = TreeLogger(self, emitter=self)  # emits errorLogged ... messageLogged of the widget
        self._loading = False
        self._shut_down = False
        self._view_target: QPointF | None = None  # scene point to centre on; None = frame the root
        self._view_target_pending = False
        self._view_settled = False
        # Deferred view positioning; owned by the widget so it never fires after deletion.
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.timeout.connect(self._apply_pending_view_target)
        self._group_counter = 0  # number of the last default group name given (group_001, ...)

        # ---- "Behavior Tree" tab (BehaviorTreeView.ui)
        self._tree_page = load_ui("BehaviorTreeView.ui")
        frame_view: QFrame = self._tree_page.findChild(QFrame, "FrameView")
        frame_layout = QVBoxLayout(frame_view)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        self._frame_view = frame_view
        self._frame_layout = frame_layout
        self._main_view = TreeView(self, frame_view)  # the tree itself; sub-trees get views of their own
        frame_layout.addWidget(self._main_view)
        # The tree itself: its nodes, file and options (sub-trees shown with Zoom In have their own).
        self._document = self._main_view.document
        self._tree_log.set_level(self._config.log_level)
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
        # The blackboard file (<name>_BB.json): blackboard changes are written to it only; the
        # tree file holds the nodes, connections and options and changes on Save only.
        self._blackboard_file = BlackboardFileWriter(
            self._blackboard, lambda: self.BLACKBOARD_SAVE_INTERVAL_MS, self._report, self
        )
        self._blackboard_file.written.connect(self.blackboardSaved)
        self._blackboard_file.failed.connect(self._on_blackboard_write_failed)

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

        self._connect_view(self._main_view)
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

    # ================================================================== the tree itself
    @property
    def _config(self) -> TreeConfig:
        """The options of the tree itself (also while a sub-tree is shown)."""
        return self._document.config

    @property
    def _view(self) -> TreeView:
        """The view of the tree shown: the tree itself, or the sub-tree zoomed into last."""
        top = self._levels.top
        return top.view if top is not None else self._main_view

    def _shown_document(self) -> TreeDocument:
        """The document of the tree shown."""
        top = self._levels.top
        return top.document if top is not None else self._document

    def _documents(self) -> list[TreeDocument]:
        """The tree itself and the sub-trees shown, from the bottom up."""
        return [self._document, *(level.document for level in self._levels)]

    def _connect_view(self, view: TreeView) -> None:
        """Follow the changes made in a view of the tree or of a sub-tree shown."""
        view.modified.connect(self._on_view_modified)
        view.nodeAdded.connect(self._on_node_added)
        view.nodeSelectionChanged.connect(self.nodeSelectionChanged)

    # ================================================================== node types
    def RegisterNodeType(self, cls: type[LeafNodeWidget]) -> None:
        """Make the leaf node class ``cls`` available in this widget (menu and loading).

        Raises:
            TypeError: ``cls`` is not a LeafNodeWidget subclass.
            ValueError: its type name is reserved or registered to another class.
        """
        self._registry.register(cls)

    def GetNodeTypes(self) -> list[str]:
        """Names of every node type that can be added: Sequence, Selector, Negation, the
        built-in leaf types Evaluation, Set, Restart, Pause, Stop, UDP Message, Print Message
        and Sub-Tree, the registered leaf types (sorted), then Notes and Group."""
        return self._registry.type_names()

    def GetNodeType(self, type_name: str) -> type[LeafNodeWidget] | None:
        """The leaf class of ``type_name``, including the built-in leaf types such as Evaluation,
        Restart and Sub-Tree (None for unknown types and for Sequence, Selector, Negation, Root,
        Notes and Group)."""
        return self._registry.leaf_class(type_name)

    def _node_menu_entries(self) -> list[tuple[str, str, str]]:
        """``(group, type_name, label)`` of every type offered by the add-node menu."""
        return self._registry.menu_entries()

    def _create_node(self, type_name: str) -> NodeWidget:
        """A new node of ``type_name`` for this tree (a new group gets the next default name)."""
        node = self._registry.create(type_name, memory=self._config.default_memory)
        if isinstance(node, GroupNodeWidget):
            node._apply_title(self._next_group_name(), runtime=False)
        return node

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
                self._registry.register(type(node))
            elif isinstance(node, RootNodeWidget):
                raise ValueError("a tree can only have one root node")
            elif isinstance(node, NotesNodeWidget) and not self._registry.offers(NOTES):
                raise ValueError(f"{NOTES!r} is the name of a registered leaf type in this widget; notes are not available")
            elif isinstance(node, GroupNodeWidget) and not self._registry.offers(GROUP):
                raise ValueError(f"{GROUP!r} is the name of a registered leaf type in this widget; groups are not available")
        elif isinstance(node_type, type):
            self._registry.register(node_type)
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

    def _copy_nodes(self, nodes: Iterable[NodeWidget]) -> bool:
        """Put ``nodes`` of the tree shown (except the Root) on the clipboard (Ctrl+C)."""
        return copy_nodes(self._view, nodes)

    def _paste_nodes(self) -> list[NodeWidget]:
        """Paste the clipboard's nodes into the tree shown (Ctrl+V)."""
        return paste_nodes(self._view, self._registry, self._next_group_name)

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
            self._report(LogLevel.ERROR, f"The tree could not be executed: {error}", exc_info=True)
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
            self._apply_config(config)
            self._mark_modified()

    def _apply_config(self, config: TreeConfig) -> None:
        """Use ``config`` as the options of the tree itself."""
        self._document.config = config
        self._executor.set_interval(config.tick_interval_ms)
        self._tree_log.set_level(config.log_level)
        self._sync_loop_box()

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

    def tree_logger(self) -> TreeLogger:
        """The :class:`~behavior_tree_widget.tree_log.TreeLogger` writing the tree's log."""
        return self._tree_log

    def _log(self, level: LogLevel, message: str) -> None:
        """Write ``message`` to the tree's log."""
        self._tree_log.log(level, message)

    def _report(self, level: LogLevel, message: str, exc_info: Any = None) -> None:
        """Write ``message`` to the library's logger (``"behavior_tree_widget"``) and to the tree's log."""
        log.log(PYTHON_LEVELS[level], "%s", message, exc_info=exc_info)
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
        return self._document.path

    def GetBlackboardFilePath(self) -> str | None:
        """Path of the current blackboard file, ``<tree name>_BB.json`` (None before New / Load / Save).

        Every change of the blackboard is written to this file (see :meth:`SaveTree`).
        """
        return self._blackboard_file.path

    def IsModified(self) -> bool:
        """True if the tree changed since it was last loaded or saved, or a sub-tree shown with
        :meth:`ZoomIn` has unsaved changes."""
        return any(document.modified for document in self._documents())

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
        data = new_tree_data()
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
            current = self._document.path
            suggestion = tree_file_paths(current)[0] if current else None
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
        shown = {file_key(sub_path) for sub_path in self._levels.paths()}
        if shown & {file_key(path), file_key(blackboard_path)}:
            raise ValueError(
                f"'{os.path.basename(path)}' is the file of a sub-tree shown with Zoom In; "
                "go back first or choose another name"
            )
        writer = self._blackboard_file
        if blackboard_path != writer.path:
            writer.flush()  # blackboard changes still due for the current file
        problems = self._document.write(path)
        blackboard_problems: list[str] = []
        self._blackboard.to_list(blackboard_problems)  # blackboard values that cannot be written (logged)
        problems.extend(blackboard_problems)
        writer.note_reported(blackboard_problems)  # not logged again by later writes
        self._document.path = path
        self._set_log_file(path)
        writer.retarget(blackboard_path)  # written below, with the rest of the tree
        self._set_tree_loaded(True)
        self._set_modified(False)
        self._update_window_title()
        blackboard_written = writer.write(force=True)  # failures leave the tree modified
        if report_problems and not blackboard_written:
            QMessageBox.warning(
                self, "Save Behavior Tree", f"The tree was saved, but its blackboard was not:\n{writer.error}"
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
        levels = self._levels.modified()
        if levels:
            names = [os.path.basename(self._document.path or "")] if self._document.modified else []
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
            if not self._document.modified:
                return True
            if self._document.path:
                current = os.path.abspath(self._document.path)
                if not self._confirm_replacing("Save Behavior Tree", current, tree_file_paths(current)[0]):
                    # A file of an earlier version whose <name>_BT.json exists: choose where to save.
                    return self.SaveTree()
                try:
                    self._require_gui_thread("SaveTree")
                    self._save_tree(current, report_problems=True)
                    return True
                except SAVE_ERRORS as error:
                    QMessageBox.critical(self, "Save Behavior Tree", f"Could not save '{self._document.path}':\n{error}")
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
        self._blackboard_file.flush()
        self._blackboard.dispose()
        self._tree_log.close()
        if shiboken6.isValid(self):
            self._update_buttons()

    # ================================================================== accessors (tests / customisation)
    def view(self) -> TreeView:
        """The QGraphicsView showing the tree shown (a sub-tree's own view while zoomed in)."""
        return self._view

    def main_view(self) -> TreeView:
        """The QGraphicsView of the tree itself (hidden while a sub-tree is shown)."""
        return self._main_view

    def blackboard_view(self) -> BlackboardView:
        """The Blackboard tab."""
        return self._blackboard_view

    def blackboard_store(self) -> BlackboardStore:
        """The blackboard (thread-safe), behind GetEntry / SetEntry."""
        return self._blackboard

    def executor(self) -> TreeExecutor:
        return self._executor

    def button(self, name: str) -> QPushButton:
        """One of the buttons: Execute, Pause, Stop, Reset, Configure, Save, Load, New, GoBack."""
        return self._buttons[name]

    def loop_execution_check_box(self) -> QCheckBox:
        """The "Loop Execution" check box of the execution controls."""
        return self._loop_box

    # Names of the accessors before 0.9.0 (deprecated).
    def mainView(self) -> TreeView:  # noqa: N802
        _deprecated_accessor("mainView", "main_view")
        return self.main_view()

    def blackboardView(self) -> BlackboardView:  # noqa: N802
        _deprecated_accessor("blackboardView", "blackboard_view")
        return self.blackboard_view()

    def blackboardStore(self) -> BlackboardStore:  # noqa: N802
        _deprecated_accessor("blackboardStore", "blackboard_store")
        return self.blackboard_store()

    def treeLogger(self) -> TreeLogger:  # noqa: N802
        _deprecated_accessor("treeLogger", "tree_logger")
        return self.tree_logger()

    def loopExecutionCheckBox(self) -> QCheckBox:  # noqa: N802
        _deprecated_accessor("loopExecutionCheckBox", "loop_execution_check_box")
        return self.loop_execution_check_box()

    # ================================================================== internals: dialogs
    def _run_dialog(self, dialog: QDialog) -> bool:
        """Show a modal dialog (separate method so tests can drive dialogs)."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _suggested_path(self, file_name: str) -> str:
        current = self._document.path
        directory = os.path.dirname(current) if current else os.getcwd()
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
        top = self._levels.top
        if top is not None:
            self._save_level_interactively(top)
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
        """Mark the tree itself as modified or not (sub-trees: _set_document_modified)."""
        self._set_document_modified(self._document, modified)

    def _set_document_modified(self, document: TreeDocument, modified: bool) -> None:
        document.modified = bool(modified)
        self._announce_modified()

    def _announce_modified(self) -> None:
        modified = self.IsModified()
        if modified != self._reported_modified:
            self._reported_modified = modified
            self.modifiedChanged.emit(modified)

    def _mark_modified(self, *_args) -> None:
        if not self._loading:
            self._set_modified(True)

    def _document_of_view(self, view: Any) -> TreeDocument | None:
        """The document shown in ``view`` (the tree itself or a sub-tree shown), or None."""
        if view is self._main_view:
            return self._document
        level = self._levels.level_of_view(view)
        return level.document if level is not None else None

    def _on_view_modified(self) -> None:
        document = self._document_of_view(self.sender())
        if document is not None and not self._loading:
            self._set_document_modified(document, True)

    def _on_data_changed(self, runtime: bool) -> None:
        # Values written while executing (or by worker threads) are not edits of the tree file.
        if runtime or self._loading:
            return
        node = self.sender()
        document = self._document_of_view(node._canvas) if isinstance(node, NodeWidget) else None
        self._set_document_modified(document or self._document, True)

    def _on_blackboard_changed(self, runtime: bool) -> None:
        if self._loading:
            return  # the blackboard of the file being installed
        if self._writes_blackboard():
            self._blackboard_file.schedule()  # changes by the user and by the tree are written to the file
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
            self._blackboard_file.schedule()  # the order is part of the blackboard file
        else:
            self._mark_modified()

    def _on_blackboard_reset_changed(self, _name: str) -> None:
        # A Reset flag is part of the blackboard file; switching it is not a value edit.
        if self._loading:
            return
        if self._writes_blackboard():
            self._blackboard_file.schedule()
        else:
            self._mark_modified()

    # ================================================================== internals: blackboard file updates
    def _writes_blackboard(self) -> bool:
        """True while blackboard changes are written to the blackboard file (a tree was created, loaded
        or saved, and the blackboard was not released by Shutdown, which writes what is still due)."""
        return self._tree_loaded and self._blackboard_file.is_active()

    def _save_blackboard_now(self) -> None:
        """Write the blackboard changes that wait for the end of the interval now."""
        self._blackboard_file.write_due()

    def _on_blackboard_write_failed(self, message: str) -> None:
        # The changes are not in the file: Save and the close prompt offer to save them.
        self._set_modified(True)
        self.blackboardSaveFailed.emit(message)

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
        if self._document.path:
            # The tree file, followed by the sub-tree files zoomed into ("robot_BT.json > patrol_BT.json").
            names = [os.path.basename(path) for path in (self._document.path, *self._levels.paths())]
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
            self._blackboard_file.flush()
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
        rect = root.scene_rect() if root is not None else None
        if rect is not None:
            viewport_height = max(view.viewport().height(), 200)
            zoom = view.zoom() or 1.0
            view.centerOn(QPointF(rect.center().x(), rect.top() - 40.0 / zoom + viewport_height / (2.0 * zoom)))

    # ================================================================== internals: tree data
    def _tree_data(
        self,
        problems: list[str] | None = None,
        quiet: bool = False,
        check_raw: bool = False,
        level: ZoomLevel | None = None,
    ) -> dict:
        """The tree (or the sub-tree of ``level``) as the JSON-compatible data of a tree file (see
        :meth:`TreeDocument.data`; the blackboard is in its own file)."""
        document = level.document if level is not None else self._document
        return document.data(problems, quiet, check_raw)

    # ================================================================== sub-trees
    def _load_subtree(self, path: str) -> tuple[RootNodeWidget, list[NodeWidget]]:
        """Create the nodes of the tree file ``path`` for a Sub-Tree node being executed: its Root
        and the nodes connected below it, linked as in the file but not shown.

        A sub-tree shown with Zoom In is taken as it is shown, with its unsaved changes. The
        nodes belong to this widget (``GetTree()``), so they use its blackboard. The caller
        deletes them.

        Raises:
            TreeFileError: the file cannot be read or is not a tree file.
        """
        level = self._levels.level_of_file(path)
        data = level.document.data(quiet=True) if level is not None else read_tree_file(path)
        return load_detached_tree(data, self._registry, self, os.path.basename(path))

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
        promotion = plan_promotion(view, group)
        if os.path.exists(path) and not overwrite:
            raise FileExistsError(f"'{path}' already exists")
        for problem in write_subtree(path, promotion, view, self._config):
            self._report(LogLevel.WARNING, f"promote {promotion.name}: {problem}")
        subtree = replace_group(view, promotion, os.path.basename(path))
        self._log(LogLevel.INFO, f"Group '{promotion.name}' promoted to the sub-tree '{path}'")
        self.subTreePromoted.emit(subtree, path)
        return subtree

    def _subtree_path_for(self, group: GroupNodeWidget) -> str:
        """The sub-tree file ``<group name>_BT.json`` a group is promoted to (see promote.subtree_path)."""
        return subtree_path(group, self._shown_path(), self._open_paths())

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
        level = self._levels.top
        if level is None:
            return False
        if level.document.modified:
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
        if self._levels.top is level:
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
        if not self._document.path:
            return []
        return [self._document.path, *self._levels.paths()]

    def SaveSubTree(self) -> bool:
        """Write the sub-tree shown (see :meth:`ZoomIn`) to its file, as the Save button does
        while a sub-tree is shown. Its file keeps its name; no blackboard file is written (a
        sub-tree uses the blackboard of the tree running it). Returns True.

        Raises:
            RuntimeError: no sub-tree is shown.
            OSError, ValueError, TypeError: the file could not be written.
        """
        self._require_gui_thread("SaveSubTree")
        level = self._levels.top
        if level is None:
            raise RuntimeError("no sub-tree is shown; SaveTree saves the tree")
        self._save_level(level, report_problems=False)
        return True

    # ------------------------------------------------------------------ zoom internals
    def _all_views(self) -> list[TreeView]:
        return [self._main_view, *self._levels.views()]

    def _all_nodes(self) -> list[NodeWidget]:
        """The nodes of the tree and of the sub-trees shown with Zoom In."""
        return [node for view in self._all_views() for node in view.nodes()]

    def _main_root(self) -> RootNodeWidget | None:
        """The Root of the tree itself (the tree that is executed)."""
        return self._main_view.root()

    def _shown_path(self) -> str | None:
        """The file of the tree shown."""
        return self._shown_document().path

    def _open_paths(self) -> list[str]:
        """The files of the tree (also the one a file of an earlier version is saved to) and of
        the sub-trees shown."""
        path = self._document.path
        paths = [path, tree_file_paths(path)[0]] if path else []
        return paths + self._levels.paths()

    def _shown_twins(self) -> dict[tuple[tuple[str, ...], str], NodeWidget]:
        """``{(scope, node id): node}`` of the nodes of the sub-trees shown (see ZoomStack.twins)."""
        return self._levels.twins()

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
                write_json_atomic(path, new_tree_data())
                self._log(LogLevel.INFO, f"New sub-tree created: '{path}'")
        if file_key(path) in {file_key(open_path) for open_path in self._open_paths()}:
            raise ValueError(
                f"'{os.path.basename(path)}' is the file of this tree or of a tree containing it; "
                "a sub-tree cannot contain itself"
            )
        data = read_tree_file(path)
        problems: list[str] = []
        try:
            plan = prepare_tree(data, problems, self._registry)
        except TreeFileError:
            raise
        except Exception as error:  # noqa: BLE001
            raise TreeFileError(f"'{os.path.basename(path)}' could not be loaded: {error}") from error
        document = TreeDocument(path, plan.config, legacy_blackboard=data.get("blackboard"))
        view = TreeView(self, self._frame_view, document)  # relative sub-tree file names are relative to its folder
        view.hide()
        self._loading = True
        try:
            install_nodes(view, plan)
        except Exception as error:  # noqa: BLE001
            plan.discard()
            view.clear()
            view.deleteLater()
            raise TreeFileError(f"'{os.path.basename(path)}' could not be loaded: {error}") from error
        finally:
            self._loading = False
        parent_scope = self._levels.top.scope if self._levels else ()
        level = ZoomLevel(document, (*parent_scope, node.GetId()), node)
        self._connect_view(view)
        for added in view.nodes():
            self._on_node_added(added)  # added before the connection above
        view.set_locked(self._executor.state() is not ExecutionState.IDLE)
        previous = self._view
        self._levels.push(level)
        self._executor.invalidate_shown_twins()
        self._frame_layout.addWidget(view)
        previous.hide()
        view.show()
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        view.set_zoom(plan.zoom if plan.zoom is not None else 1.0)
        self._center_view(view, plan.center)
        # Centred again once the view has its final size (from the event loop).
        center = QPointF(plan.center) if plan.center is not None else None
        QTimer.singleShot(0, view, lambda: self._center_view(view, center) if self._levels.level_of_view(view) else None)
        for problem in problems:
            self._report(LogLevel.WARNING, f"{os.path.basename(path)}: {problem}")
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

    def _close_level(self, level: ZoomLevel) -> None:
        """Stop showing ``level`` (the top one) and show the tree below it; its changes are dropped."""
        self._levels.remove(level)
        self._executor.invalidate_shown_twins()
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
            self._close_level(self._levels.top)
        self._after_zoom_change()

    def _save_level(self, level: ZoomLevel, report_problems: bool) -> list[str]:
        """Write the sub-tree of ``level`` to its file; returns what was left out."""
        problems = level.document.write()
        self._set_document_modified(level.document, False)
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

    def _save_level_interactively(self, level: ZoomLevel) -> bool:
        try:
            self._save_level(level, report_problems=True)
        except SAVE_ERRORS as error:
            QMessageBox.critical(self, "Save Sub-Tree", f"Could not save '{level.path}':\n{error}")
            return False
        return True

    # ================================================================== internals: loading
    def _install_plan(self, plan: TreePlan) -> None:
        """Replace the tree itself (and its blackboard) by the tree of ``plan``."""
        if self._executor.state() is not ExecutionState.IDLE:
            raise RuntimeError("execution could not be stopped")
        self._close_all_levels()
        view = self._main_view
        view.set_locked(False)
        view.clear()
        self._apply_config(plan.config)
        # The blackboard first: each node that lists entries then fills its editors once, when added.
        store = self._blackboard
        wanted = [name for name, _, _ in plan.blackboard]
        kept = [name for name in store.names() if name not in wanted and store.type_of(name) == "Object"]
        with store.batch():
            for name in store.names():
                if name not in wanted and name not in kept:
                    store.remove(name)
            for name, type_name, value in plan.blackboard:
                store.replace(name, type_name, value)
                store.set_reset(name, name in plan.resets)
            store.reorder(wanted + kept)
        install_nodes(view, plan)
        self._group_counter = 0  # default group names count from the groups of this tree
        view.set_zoom(plan.zoom if plan.zoom is not None else 1.0)
        self._set_view_target(plan.center)

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
            plan = prepare_tree(data, problems, self._registry, blackboard)
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

    def _settle_blackboard_file(self, plan: TreePlan) -> None:
        """Before ``plan`` replaces the tree: write the blackboard changes still due for the file
        being replaced, or drop them when the tree's blackboard file was just replaced by NewTree."""
        writer = self._blackboard_file
        same_blackboard = self._same_file(plan.blackboard_path, writer.path)
        if plan.new_blackboard and same_blackboard:
            # A new tree of the same name: its (empty) blackboard file replaced the old one, which
            # must not get the old tree's pending changes.
            writer.cancel_pending()
            return
        due = writer.is_pending()
        writer.flush()  # changes still due for the file being replaced
        if same_blackboard and due and writer.error is None:
            # The tree being loaded is this one, and its blackboard file changed after it was
            # read (e.g. stopping the run reset entries): load the file as it is now.
            self._reread_blackboard(plan)

    @staticmethod
    def _reread_blackboard(plan: TreePlan) -> None:
        """Take the entries of ``plan``'s blackboard file again (it was just written by this widget)."""
        try:
            items = read_blackboard_file(plan.blackboard_path)
        except TreeFileError:  # keep the entries read before
            log.debug("could not read the blackboard file again", exc_info=True)
            return
        plan.blackboard, plan.resets, _ = BlackboardStore.parse_items(items)

    def _snapshot_current(self) -> tuple[dict | None, list | None, list[str]]:
        """The tree itself, its blackboard and its Reset flags, to put back if installing another
        tree fails (``(None, None, [])`` when there is nothing to put back)."""
        if not self._main_view.nodes():
            return None, None, []
        try:
            return self._tree_data(quiet=True), self._blackboard.snapshot(), self._blackboard.reset_names()
        except Exception:  # noqa: BLE001
            log.debug("could not snapshot the current tree", exc_info=True)
            return None, None, []

    def _restore_previous(self, tree: dict | None, blackboard: list | None, resets: list[str]) -> None:
        """Put back the tree taken by :meth:`_snapshot_current` (installing another one failed)."""
        if tree is None:
            return
        try:
            self._install_plan(prepare_tree(tree, [], self._registry))
            if blackboard is not None:
                self._blackboard.restore(blackboard)
                for name in resets:
                    if self._blackboard.has(name):
                        self._blackboard.set_reset(name, True)
        except Exception:  # noqa: BLE001
            log.exception("could not restore the previous tree")

    def _install_loaded_tree(
        self, plan: TreePlan, path: str, interactive: bool, problems: list[str], deferred: bool = False
    ) -> None:
        self._close_all_levels()  # sub-trees shown with Zoom In: their unsaved changes are discarded
        self._settle_blackboard_file(plan)
        previous = self._snapshot_current()
        self._loading = True
        try:
            if self._executor.is_busy() or self._executor.state() is not ExecutionState.IDLE:
                self._executor.stop()
            self._install_plan(plan)
        except Exception as error:  # noqa: BLE001 - unexpected; put the previous tree back
            log.exception("loading %s failed", path)
            plan.discard()
            self._restore_previous(*previous)
            failure = TreeFileError(f"'{os.path.basename(path)}' could not be loaded: {error}")
            if not deferred:
                raise failure from error
            if interactive:
                QMessageBox.critical(self, "Load Behavior Tree", str(failure))
            return
        finally:
            self._loading = False

        self._document.path = path
        self._set_log_file(path)
        # What the blackboard file holds: the entries just read, so an unchanged blackboard is not
        # written. (For a file without blackboard file, the first change creates it.)
        file_exists = bool(plan.blackboard_path) and os.path.exists(plan.blackboard_path)
        self._blackboard_file.adopt(plan.blackboard_path, file_holds_store=file_exists)
        self._set_tree_loaded(True)
        self._set_modified(False)
        self._update_buttons()
        self._update_window_title()
        self._log(LogLevel.INFO, f"{'New tree created' if plan.new_blackboard else 'Tree loaded'}: '{path}'")
        for problem in problems:
            self._report(LogLevel.WARNING, f"{os.path.basename(path)}: {problem}")
        if problems and interactive:
            QMessageBox.warning(
                self,
                "Load Behavior Tree",
                f"'{os.path.basename(path)}' was loaded with problems:\n\n" + "\n".join(f"- {p}" for p in problems),
            )
        self.treeLoaded.emit(path)
