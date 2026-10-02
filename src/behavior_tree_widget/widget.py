"""The :class:`BehaviorTreeWidget`: a tabbed editor and executor for py_trees behavior trees."""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import shiboken6
from PySide6.QtCore import QCoreApplication, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ._ui import load_ui
from .blackboard import BlackboardStore, BlackboardView
from .canvas import TreeView, clamp_to_scene
from .config import ConfigureDialog, TreeConfig
from .execution import ExecutionState, LeafBehaviour, TreeExecutor
from .nodes import (
    COMPOSITE_TYPES,
    SEQUENCE,
    CompositeNodeWidget,
    LeafNodeWidget,
    NodeWidget,
    RootNodeWidget,
    UnknownLeafNodeWidget,
)
from .serialization import (
    FORMAT_NAME,
    FORMAT_VERSION,
    TreeFileError,
    decode_value,
    encode_value,
    is_finite_number,
    read_tree_file,
    write_json_atomic,
)

log = logging.getLogger("behavior_tree_widget")

__all__ = ["BehaviorTreeWidget", "register_node_type", "registered_node_types"]

RESERVED_TYPE_NAMES = frozenset({"Root", *COMPOSITE_TYPES})
BUTTON_NAMES = ("Execute", "Pause", "Stop", "Reset", "Configure", "Save", "Load", "New")
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
    if cls is LeafNodeWidget or issubclass(cls, UnknownLeafNodeWidget):
        raise TypeError(f"{cls.__name__} cannot be registered; register a subclass that implements OnRun")
    name = _type_name_of(cls)
    if not isinstance(name, str) or not name:
        raise ValueError(f"{cls.__name__}.TYPE_NAME must be a non-empty string")
    if name in RESERVED_TYPE_NAMES:
        raise ValueError(f"{name!r} is a built-in node type name; set a different TYPE_NAME on {cls.__name__}")
    return name


def _register(registry: dict[str, type[LeafNodeWidget]], cls: type[LeafNodeWidget]) -> str:
    name = _validate_leaf_class(cls)
    existing = registry.get(name)
    if existing is not None and existing is not cls:
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
    config: TreeConfig = field(default_factory=TreeConfig)
    zoom: float | None = None
    center: QPointF | None = None

    def discard(self) -> None:
        for node, _ in self.nodes:
            if shiboken6.isValid(node):
                node.deleteLater()
        self.nodes = []


STANDARD_NODE_KEYS = frozenset({"id", "type", "title", "x", "y", "fields", "field_selections"})


def _cleanup_after_destroy(store: BlackboardStore, executor: TreeExecutor) -> None:
    """Release resources of a BehaviorTreeWidget destroyed without Shutdown()."""
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

    treeLoaded = Signal(str)  # path of the tree file after New / Load
    treeSaved = Signal(str)  # path the tree was saved to
    modifiedChanged = Signal(bool)
    executionStateChanged = Signal(str)  # "Idle", "Running", "Paused"
    executionFinished = Signal(str)  # "Succeeded" / "Failed" (run-once mode)
    nodeStatusChanged = Signal(object, str)  # node, "Ready"/"Running"/"Succeeded"/"Failed"
    nodeError = Signal(object, str)  # node, error message

    TREE_TAB_TITLE = "Behavior Tree"
    BLACKBOARD_TAB_TITLE = "Blackboard"
    FILE_FILTER = "Behavior Tree (*.json);;All Files (*)"
    WINDOW_TITLE_FORMAT = "Behavior Tree - {filename}"

    def __init__(self, parent: QWidget | None = None, node_types: Iterable[type[LeafNodeWidget]] = ()):
        super().__init__(parent)
        self.setObjectName("BehaviorTreeWidget")
        self._gui_thread = threading.get_ident()
        self._node_types: dict[str, type[LeafNodeWidget]] = dict(_global_node_types)
        self._config = TreeConfig()
        self._file_path: str | None = None
        self._tree_loaded = False
        self._modified = False
        self._loading = False
        self._shut_down = False
        self._view_target: QPointF | None = None  # scene point to centre on; None = frame the root
        self._view_target_pending = False
        self._view_settled = False
        # Deferred view positioning; owned by the widget so it never fires after deletion.
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.timeout.connect(self._apply_pending_view_target)

        # ---- "Behavior Tree" tab (BehaviorTreeView.ui)
        self._tree_page = load_ui("BehaviorTreeView.ui")
        frame_view: QFrame = self._tree_page.findChild(QFrame, "FrameView")
        frame_layout = QVBoxLayout(frame_view)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        self._view = TreeView(self, frame_view)
        frame_layout.addWidget(self._view)
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

        self._view.modified.connect(self._mark_modified)
        self._view.nodeAdded.connect(self._on_node_added)
        self._view.shown.connect(self._on_view_shown)
        self._view.navigated.connect(self._on_view_navigated)
        self._blackboard_view.entryEdited.connect(self._on_blackboard_edited)
        self._blackboard.changed.connect(self._on_blackboard_changed)

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.Shutdown)
        # Cancel running nodes and release the py_trees keys even if Shutdown() is never called.
        self.destroyed.connect(functools.partial(_cleanup_after_destroy, self._blackboard, self._executor))

        for cls in node_types:
            self.RegisterNodeType(cls)

        self._loading = True
        try:
            self._view.add_node(RootNodeWidget(memory=self._config.default_memory), QPointF(0.0, 0.0))
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
        """Names of every node type that can be added: Sequence, Selector and the leaf types."""
        return [*COMPOSITE_TYPES, *sorted(self._node_types)]

    def GetNodeType(self, type_name: str) -> type[LeafNodeWidget] | None:
        """The leaf class registered as ``type_name`` (None for unknown or built-in types)."""
        return self._node_types.get(type_name)

    def _node_menu_entries(self) -> list[tuple[str, str, str]]:
        """``(group, type_name, label)`` of every type offered by the add-node menu."""
        entries = [("composite", name, name) for name in COMPOSITE_TYPES]
        leaves = []
        for name, cls in self._node_types.items():
            title = cls._title if isinstance(cls._title, str) and cls._title else cls.__name__
            leaves.append((title, name))
        titles = [title for title, _ in leaves]
        for title, name in sorted(leaves, key=lambda pair: (pair[0].lower(), pair[1])):
            label = title if titles.count(title) == 1 else f"{title} ({name})"
            entries.append(("leaf", name, label))
        return entries

    def _create_node(self, type_name: str) -> NodeWidget:
        if type_name in COMPOSITE_TYPES:
            return CompositeNodeWidget(type_name, memory=self._config.default_memory)
        cls = self._node_types.get(type_name)
        if cls is None:
            raise ValueError(f"unknown node type {type_name!r}")
        return cls()

    def _require_gui_thread(self, name: str) -> None:
        if threading.get_ident() != self._gui_thread:
            raise RuntimeError(f"BehaviorTreeWidget.{name}() must be called from the GUI thread")

    # ================================================================== tree editing API
    def GetRootNode(self) -> RootNodeWidget | None:
        """The root node of the tree."""
        return self._view.root()

    def GetNodes(self) -> list[NodeWidget]:
        """Every node in the view (connected or not)."""
        return self._view.nodes()

    def AddNode(self, node_type: str | type[LeafNodeWidget] | NodeWidget, x: float = 0.0, y: float = 0.0) -> NodeWidget:
        """Add a node with its top-left corner at scene position (x, y) and return it.

        ``node_type`` is a type name ("Sequence", "Selector" or a registered leaf
        type), a leaf class or a leaf node instance (their class is registered
        automatically).

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

    def AddEntry(self, name: str, type_name: str, value: Any = None) -> None:
        """Create entry ``name`` of ``type_name`` (Integer, Double/Float, String, Bool, List,
        Dictionary, Set or Object) with ``value`` or the type's default value."""
        self._blackboard.add(name, type_name, value)

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
        interval = config.tick_interval_ms
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise TypeError("TreeConfig.tick_interval_ms must be an int")
        config = config.copy()
        config.tick_interval_ms = min(max(interval, TreeConfig.MIN_TICK_MS), TreeConfig.MAX_TICK_MS)
        if config != self._config:
            self._config = config
            self._executor.set_interval(config.tick_interval_ms)
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

    # ================================================================== files
    def IsTreeLoaded(self) -> bool:
        """True once a tree was created (New), loaded or saved."""
        return self._tree_loaded

    def GetFilePath(self) -> str | None:
        """Path of the current tree file (None before New / Load / Save)."""
        return self._file_path

    def IsModified(self) -> bool:
        """True if the tree changed since it was last loaded or saved."""
        return self._modified

    def NewTree(self, path: str | None = None) -> bool:
        """Create a new tree containing only the root and store it in ``path``.

        Without ``path`` the user is asked for a file. Returns True on success. The
        blackboard is cleared, except Object entries (run-time objects set from code).

        Raises:
            OSError: ``path`` was given and the file could not be written.
        """
        self._require_gui_thread("NewTree")
        interactive = path is None
        if interactive:
            if not self.ConfirmDiscardChanges():
                return False
            path = self._ask_save_path("New Behavior Tree", self._suggested_path("behavior_tree.json"))
            if not path:
                return False
        path = os.path.abspath(path)
        data = self._new_tree_data()
        try:
            write_json_atomic(path, data)
        except SAVE_ERRORS as error:
            if not interactive:
                raise
            QMessageBox.critical(self, "New Behavior Tree", f"Could not create '{path}':\n{error}")
            return False
        try:
            self._apply_tree_data(data, path, interactive)
        except TreeFileError as error:
            if not interactive:
                raise
            QMessageBox.critical(self, "New Behavior Tree", str(error))
            return False
        return True

    def LoadTree(self, path: str | None = None) -> bool:
        """Load the tree stored in ``path`` (asks for a file when None). Returns True on success.

        The blackboard is replaced by the entries saved in the file; Object entries
        (run-time objects set from code, which are never saved) are kept.

        If called while the tree is being ticked (e.g. from a node's ``OnRun``), the
        file is read and checked at once and the tree is replaced right after that tick.

        Raises:
            TreeFileError: ``path`` was given and the file is not a valid tree file.
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
        path = os.path.abspath(path)
        try:
            data = read_tree_file(path)
            self._apply_tree_data(data, path, interactive)
        except TreeFileError as error:
            if not interactive:
                raise
            QMessageBox.critical(self, "Load Behavior Tree", str(error))
            return False
        return True

    def SaveTree(self, path: str | None = None) -> bool:
        """Save the tree to ``path`` (asks for a file when None). Returns True on success.

        Values that cannot be written to JSON (e.g. Object blackboard entries) are
        skipped; interactively the user is told which.

        Raises:
            OSError, ValueError, TypeError: ``path`` was given and saving failed.
        """
        self._require_gui_thread("SaveTree")
        interactive = path is None
        if interactive:
            path = self._ask_save_path("Save Behavior Tree", self._file_path or self._suggested_path("behavior_tree.json"))
            if not path:
                return False
        path = os.path.abspath(path)
        try:
            self._save_tree(path, report_problems=interactive)
        except SAVE_ERRORS as error:
            if not interactive:
                raise
            QMessageBox.critical(self, "Save Behavior Tree", f"Could not save '{path}':\n{error}")
            return False
        return True

    def _save_tree(self, path: str, report_problems: bool) -> None:
        """Write the tree to ``path``; with ``report_problems`` the user is told what was left out."""
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
        self._file_path = path
        self._set_tree_loaded(True)
        self._set_modified(False)
        self._update_window_title()
        if problems and report_problems:
            QMessageBox.warning(
                self,
                "Save Behavior Tree",
                "The tree was saved, but some values cannot be stored in a file and were left out:\n\n"
                + "\n".join(f"- {problem}" for problem in problems),
            )
        self.treeSaved.emit(path)

    def ConfirmDiscardChanges(self) -> bool:
        """Ask to save unsaved changes. Returns False if the user cancelled."""
        if not (self._tree_loaded and self._modified):
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved Changes",
            "The behavior tree has unsaved changes. Save them first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            if self._file_path:
                try:
                    self._require_gui_thread("SaveTree")
                    self._save_tree(os.path.abspath(self._file_path), report_problems=True)
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
        self._blackboard.dispose()
        if shiboken6.isValid(self):
            self._update_buttons()

    # ================================================================== accessors (tests / customisation)
    def view(self) -> TreeView:
        """The QGraphicsView showing the tree."""
        return self._view

    def blackboardView(self) -> BlackboardView:  # noqa: N802
        return self._blackboard_view

    def blackboardStore(self) -> BlackboardStore:  # noqa: N802
        return self._blackboard

    def executor(self) -> TreeExecutor:
        return self._executor

    def button(self, name: str) -> QPushButton:
        """One of the execution buttons: Execute, Pause, Stop, Reset, Configure, Save, Load, New."""
        return self._buttons[name]

    # ================================================================== internals: dialogs
    def _run_dialog(self, dialog: QDialog) -> bool:
        """Show a modal dialog (separate method so tests can drive dialogs)."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _suggested_path(self, file_name: str) -> str:
        directory = os.path.dirname(self._file_path) if self._file_path else os.getcwd()
        return os.path.join(directory, file_name) if file_name else directory

    def _ask_save_path(self, caption: str, suggestion: str) -> str:
        path, _ = QFileDialog.getSaveFileName(self, caption, suggestion, self.FILE_FILTER)
        if path and not os.path.splitext(path)[1]:
            path += ".json"
        return path

    def _on_save_clicked(self) -> None:
        self.SaveTree()

    def _on_load_clicked(self) -> None:
        self.LoadTree()

    def _on_new_clicked(self) -> None:
        self.NewTree()

    # ================================================================== internals: state
    def _set_tree_loaded(self, loaded: bool) -> None:
        self._tree_loaded = loaded
        self._view.setEnabled(loaded)
        self._update_buttons()

    def _set_modified(self, modified: bool) -> None:
        if modified != self._modified:
            self._modified = modified
            self.modifiedChanged.emit(modified)

    def _mark_modified(self, *_args) -> None:
        if not self._loading:
            self._set_modified(True)

    def _on_data_changed(self, runtime: bool) -> None:
        # Values written while executing (or by worker threads) are not edits of the tree file.
        if not runtime:
            self._mark_modified()

    def _on_blackboard_changed(self, runtime: bool) -> None:
        if not runtime:
            self._mark_modified()
            if not self._loading and self._executor.state() is ExecutionState.IDLE:
                # A deliberate edit after a run: Reset must not silently undo it.
                self._executor.discard_snapshot()

    def _on_blackboard_edited(self, *_args) -> None:
        self._mark_modified()
        if self._executor.state() is ExecutionState.IDLE:
            self._executor.discard_snapshot()

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
        self._buttons["Execute"].setToolTip("Resume execution" if state is ExecutionState.PAUSED else "Execute the tree")

    def _update_window_title(self) -> None:
        if self._file_path:
            # "[*]" is Qt's window-modified placeholder; "[*][*]" displays a literal "[*]".
            name = os.path.basename(self._file_path).replace("[*]", "[*][*]")
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
        self._view.set_locked(state != ExecutionState.IDLE.value)
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
        if self._view_target is not None:
            self._view.centerOn(self._view_target)
        else:
            root = self.GetRootNode()
            if root is not None and root._item is not None:
                rect = root._item.sceneBoundingRect()
                viewport_height = max(self._view.viewport().height(), 200)
                zoom = self._view.zoom() or 1.0
                self._view.centerOn(
                    QPointF(rect.center().x(), rect.top() - 40.0 / zoom + viewport_height / (2.0 * zoom))
                )
        if self._view_settled and self._view.isVisible():
            self._view_target_pending = False

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
            "blackboard": [],
        }

    def _tree_data(self, problems: list[str] | None = None, quiet: bool = False, check_raw: bool = False) -> dict:
        """The current tree as JSON-compatible data. Unsaveable values are skipped (see ``problems``);
        ``quiet`` suppresses the log messages (used for internal snapshots); ``check_raw`` also
        skips data of unknown node types that json cannot write."""
        problems = problems if problems is not None else []
        warn = (lambda message: None) if quiet else log.warning

        def writable(node: NodeWidget, what: str, value: Any) -> bool:
            if not check_raw or _json_writable(value):
                return True
            message = f"{what} of node {node.GetTitle()!r} was not saved: it is nested too deeply to be written"
            warn(message)
            problems.append(message)
            return False
        nodes = []
        for node in self._view.nodes():
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
                    {key: value for key, value in node._raw_fields.items() if writable(node, f"field {key!r}", value)}
                )
                selections = {  # as saved ...
                    key: value
                    for key, value in node._raw_selections.items()
                    if writable(node, f"selection of field {key!r}", value)
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
                    if key not in extra and writable(node, f"data {key!r}", value):
                        extra[key] = value
            entry.update(extra)
            nodes.append(entry)
        connections = []
        for node in self._view.nodes():
            for child in node.GetChildren():
                connections.append({"parent": node.GetId(), "child": child.GetId()})
        center = self._view.view_center()
        return {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "config": self._config.to_dict(),
            "view": {"zoom": round(self._view.zoom(), 4), "center": [round(center.x(), 2), round(center.y(), 2)]},
            "nodes": nodes,
            "connections": connections,
            "blackboard": self._blackboard.to_list(problems, quiet=quiet),
        }

    def _prepare_tree(self, data: dict, problems: list[str]) -> _TreePlan:
        """Validate ``data`` and create (but do not add) its nodes. Nothing visible changes."""
        plan = _TreePlan(config=TreeConfig.from_dict(data.get("config")))
        try:
            self._prepare_nodes(plan, data.get("nodes", []), problems)
            self._prepare_connections(plan, data.get("connections", []), problems)
            plan.blackboard, blackboard_problems = BlackboardStore.parse_list(data.get("blackboard", []))
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

    def _prepare_nodes(self, plan: _TreePlan, items: Any, problems: list[str]) -> None:
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
                if root_seen:
                    problems.append(f"extra root node {label} was skipped")
                    continue
                root_seen = True
                composite = item.get("composite")
                node: NodeWidget = RootNodeWidget(composite if composite in COMPOSITE_TYPES else SEQUENCE)
            elif type_name in COMPOSITE_TYPES:
                node = CompositeNodeWidget(type_name)
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
                node._title = title
            x, y = item.get("x", 0.0), item.get("y", 0.0)
            if not (is_finite_number(x) and is_finite_number(y)):
                problems.append(f"node {label} has an invalid position")
                x, y = 0.0, 0.0
            position, clamped = clamp_to_scene(QPointF(float(x), float(y)))
            if clamped:
                problems.append(f"node {label} was outside the drawing area and was moved inside it")
            plan.nodes[-1] = (node, position)
        if not root_seen:
            problems.append("the file has no root node; a new root was created")
            plan.nodes.insert(0, (RootNodeWidget(), QPointF(0.0, 0.0)))

    @staticmethod
    def _prepare_connections(plan: _TreePlan, items: Any, problems: list[str]) -> None:
        by_id = {node.GetId(): node for node, _ in plan.nodes}
        parent_of: dict[str, str] = {}
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
            ancestor: str | None = parent_id
            while ancestor is not None and ancestor != child_id:
                ancestor = parent_of.get(ancestor)
            if ancestor == child_id:
                problems.append(f"connection {index} ({parent.GetTitle()!r} -> {child.GetTitle()!r}) creates a cycle; skipped")
                continue
            parent_of[child_id] = parent_id
            plan.connections.append((parent_id, child_id))

    def _install_plan(self, plan: _TreePlan) -> None:
        if self._executor.state() is not ExecutionState.IDLE:
            raise RuntimeError("execution could not be stopped")
        self._view.set_locked(False)
        self._view.clear()
        self._config = plan.config
        self._executor.set_interval(self._config.tick_interval_ms)
        by_id: dict[str, NodeWidget] = {}
        for node, pos in plan.nodes:
            self._view.add_node(node, pos)
            by_id[node.GetId()] = node
        plan.nodes = []
        for parent_id, child_id in plan.connections:
            self._view.connect_nodes(by_id[parent_id], by_id[child_id])
        store = self._blackboard
        wanted = [name for name, _, _ in plan.blackboard]
        kept = [name for name in store.names() if name not in wanted and store.type_of(name) == "Object"]
        for name in store.names():
            if name not in wanted and name not in kept:
                store.remove(name)
        for name, type_name, value in plan.blackboard:
            store.replace(name, type_name, value)
        store.reorder(wanted + kept)
        self._view.set_zoom(plan.zoom if plan.zoom is not None else 1.0)
        self._set_view_target(plan.center)

    def _apply_tree_data(self, data: dict, path: str, interactive: bool) -> None:
        problems: list[str] = []
        try:
            plan = self._prepare_tree(data, problems)
        except TreeFileError:
            raise
        except Exception as error:  # noqa: BLE001
            raise TreeFileError(f"'{os.path.basename(path)}' could not be loaded: {error}") from error

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

    def _install_loaded_tree(
        self, plan: _TreePlan, path: str, interactive: bool, problems: list[str], deferred: bool = False
    ) -> None:
        previous_tree = previous_blackboard = None
        if self._view.nodes():
            try:
                previous_tree = self._tree_data(quiet=True)
                previous_blackboard = self._blackboard.snapshot()
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
        self._set_tree_loaded(True)
        self._set_modified(False)
        self._update_buttons()
        self._update_window_title()
        for problem in problems:
            log.warning("%s: %s", os.path.basename(path), problem)
        if problems and interactive:
            QMessageBox.warning(
                self,
                "Load Behavior Tree",
                f"'{os.path.basename(path)}' was loaded with problems:\n\n" + "\n".join(f"- {p}" for p in problems),
            )
        self.treeLoaded.emit(path)
