"""Node widgets shown in the behavior tree view.

Four kinds of nodes exist:

* :class:`RootNodeWidget` - the single root of a tree, a Sequence or a Selector
  without a parent.
* :class:`CompositeNodeWidget` - a Sequence or Selector with a parent and children.
* :class:`NegationNodeWidget` - a parent of exactly one child whose result it inverts.
* :class:`LeafNodeWidget` - the base class for user defined nodes. Subclasses
  override :meth:`LeafNodeWidget.OnRun` and may declare editable ``_fields``.

Example::

    class MoveTo(LeafNodeWidget):
        _title = "Move To"
        _fields = {"x": 0.0, "y": 0.0, "speed": 1, "mode": ["walk", "run"], "log": True}

        def OnRun(self, tree):
            tree.SetEntry(self.GetField("x"), "target_x")
            return True
"""

from __future__ import annotations

import copy
import functools
import inspect
import logging
import os
import sys
import threading
import uuid
import weakref
from enum import Enum
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ._runctx import check_not_cancelled, current_token
from ._ui import load_ui

if TYPE_CHECKING:  # pragma: no cover
    from .widget import BehaviorTreeWidget

log = logging.getLogger("behavior_tree_widget")

__all__ = [
    "NodeStatus",
    "ConnectionState",
    "NodeWidget",
    "CompositeNodeWidget",
    "RootNodeWidget",
    "NegationNodeWidget",
    "LeafNodeWidget",
    "UnknownLeafNodeWidget",
    "SEQUENCE",
    "SELECTOR",
    "NEGATION",
]

SEQUENCE = "Sequence"
SELECTOR = "Selector"
COMPOSITE_TYPES = (SEQUENCE, SELECTOR)
NEGATION = "Negation"

INT_MIN = -(2**31)
INT_MAX = 2**31 - 1


class NodeStatus(str, Enum):
    """Execution state displayed by a node's Status label."""

    READY = "Ready"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_py_trees(cls, status) -> "NodeStatus":
        """Map a ``py_trees.common.Status`` to a :class:`NodeStatus`."""
        name = getattr(status, "name", None)
        return {
            "RUNNING": cls.RUNNING,
            "SUCCESS": cls.SUCCEEDED,
            "FAILURE": cls.FAILED,
        }.get(name, cls.READY)


class ConnectionState(Enum):
    """Colour state of a ParentConnection / ChildConnections label."""

    DISCONNECTED = "disconnected"  # red
    PLACING = "placing"  # green
    CONNECTED = "connected"  # blue


CONNECTION_COLORS = {
    ConnectionState.DISCONNECTED: "#d0312d",
    ConnectionState.PLACING: "#1f9d3a",
    ConnectionState.CONNECTED: "#1f63d6",
}

STATUS_COLORS = {
    NodeStatus.READY: "#6f6f6f",
    NodeStatus.RUNNING: "#c77700",
    NodeStatus.SUCCEEDED: "#1b8a36",
    NodeStatus.FAILED: "#c62828",
}

PARENT = "parent"
CHILDREN = "children"


def _connection_style(state: ConnectionState) -> str:
    return (
        f"QLabel {{ background-color: {CONNECTION_COLORS[state]}; color: white; "
        "border-radius: 3px; padding: 1px 4px; }"
    )


def _status_style(status: NodeStatus) -> str:
    weight = "normal" if status is NodeStatus.READY else "bold"
    return f"QLabel {{ color: {STATUS_COLORS[status]}; font-weight: {weight}; }}"


class _WheelGuard(QObject):
    """Stops spin boxes / combo boxes from grabbing wheel events unless they have focus.

    Without this, scrolling the tree view over a node would change field values.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt API)
        if event.type() == QEvent.Type.Wheel and isinstance(watched, QWidget) and not watched.hasFocus():
            event.ignore()
            return True
        return False


_wheel_guard: _WheelGuard | None = None


def _guard_wheel(widget: QWidget) -> None:
    global _wheel_guard
    if _wheel_guard is None:
        _wheel_guard = _WheelGuard()
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.installEventFilter(_wheel_guard)


def relayout(widget: QWidget) -> None:
    """Recompute all layouts below ``widget`` synchronously and shrink/grow it to fit.

    ``adjustSize()`` alone uses stale size hints right after text or visibility
    changes, so layouts are invalidated and activated bottom-up first.
    """
    for child in reversed([widget, *widget.findChildren(QWidget)]):
        layout = child.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
    widget.adjustSize()


def _display_repr(value: Any) -> str:
    """A bounded repr that never raises (for read-only field editors)."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - a broken __repr__
        return f"<{type(value).__name__} object>"
    return text if len(text) <= 500 else text[:497] + "..."


def _item_text(item: Any) -> str:
    """Text of a list field choice; never raises (e.g. an int too long for str())."""
    try:
        return str(item)
    except Exception:  # noqa: BLE001
        return _display_repr(item)


def field_kind(value: Any) -> str:
    """Classify a field value: ``bool``, ``int``, ``float``, ``str``, ``list`` or ``other``."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return "other"


class NodeWidget(QWidget):
    """Base class of every node shown in the behavior tree view.

    A node loads its Designer file (``UI_FILE``) into itself. The file must contain
    ``Title`` and ``Status`` labels; non-root nodes need a ``ParentConnection`` label
    and non-leaf nodes a ``ChildConnections`` label.

    Class attributes a subclass may override:
        UI_FILE: File name of a packaged ``.ui`` file, a path relative to the module
            defining the subclass, or an absolute path.
        TYPE_NAME: Name used to register and save the node type (defaults to the
            class name).
        _title: Default title shown in the Title label.
        _fields: Template of the editable fields (leaf nodes only). It is deep copied
            for each node instance.
    """

    UI_FILE: str = ""
    TYPE_NAME: str | None = None
    _title: str | None = None
    _fields: dict = {}

    statusChanged = Signal(str)
    titleChanged = Signal(str)
    changed = Signal(bool)  # saved data changed (title, fields, ...); True for run-time changes
    fieldChanged = Signal(str)  # any change of a field value / list selection
    fieldEdited = Signal(str)  # a change made by the user in the node's editors
    geometryHintChanged = Signal()
    _queuedCall = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._owner_thread = threading.get_ident()
        self._lock = threading.RLock()
        self._id = uuid.uuid4().hex
        # Values a subclass assigned before calling super().__init__() are kept.
        preset_title = self.__dict__.get("_title")
        self._title = self._default_title() if preset_title is None else str(preset_title)
        preset_fields = self.__dict__.get("_fields")
        if isinstance(preset_fields, dict):
            self._fields = preset_fields
        else:
            template = type(self)._fields
            self._fields = copy.deepcopy(template) if isinstance(template, dict) else {}
        self._field_selections: dict[str, int] = {}
        self._parent: NodeWidget | None = None
        self._children: list[NodeWidget] = []
        self._status = NodeStatus.READY
        self._error: str | None = None
        self._run_token = None  # _runctx.RunToken of the current / last run
        self._worker_future = None  # Future of the last OnRun call started on a worker thread
        self._tree_ref = None  # weak reference to the BehaviorTreeWidget (see _tree)
        self._canvas = None  # canvas.TreeView, set when the node is added to a view
        self._item = None  # canvas.NodeItem embedding this widget

        self._queuedCall.connect(self._run_queued, Qt.ConnectionType.QueuedConnection)

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self._ui = load_ui(self._resolve_ui_file(), self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._ui)

        self._title_label: QLabel = self._require_child(QLabel, "Title")
        self._title_label.setContentsMargins(4, 1, 4, 1)  # keep the box frame off the text
        self._status_label: QLabel = self._require_child(QLabel, "Status")
        self._parent_label: QLabel | None = self._find_child(QLabel, "ParentConnection") if self.HasParentConnection() else None
        self._children_label: QLabel | None = None
        if self.HasChildConnections():
            self._children_label = self._find_child(QLabel, "ChildConnections") or self._find_child(
                QLabel, "ChildConnnections"
            )
        if self.HasParentConnection() and self._parent_label is None:
            raise RuntimeError(f"{self._ui_description()} has no QLabel named 'ParentConnection'")
        if self.HasChildConnections() and self._children_label is None:
            raise RuntimeError(f"{self._ui_description()} has no QLabel named 'ChildConnections'")
        # Hide the connection frames that do not apply (e.g. a custom leaf ui copied from a composite).
        if not self.HasParentConnection():
            self._hide_connection_frame("ParentConnection", "FrameParent")
        if not self.HasChildConnections():
            self._hide_connection_frame("ChildConnections", "FrameChildren")
            self._hide_connection_frame("ChildConnnections", "FrameChildren")
        for label in (self._parent_label, self._children_label):
            if label is not None:
                label.setCursor(Qt.CursorShape.CrossCursor)

        self._refresh_title()
        self._refresh_status()
        self.set_connection_state(PARENT, ConnectionState.DISCONNECTED)
        self.set_connection_state(CHILDREN, ConnectionState.DISCONNECTED)
        self._node_ready = True

    def __setattr__(self, name: str, value: Any) -> None:
        # Assigning _title / _fields (e.g. in a subclass __init__) updates the node's ui.
        if name in ("_title", "_fields") and self.__dict__.get("_node_ready", False):
            check_not_cancelled(f"assigning self.{name}")
            runtime = self._is_runtime_change()
            if name == "_title":
                self._apply_title(value, runtime)
                return
            super().__setattr__(name, value)
            self._call_in_gui_thread(self._on_fields_replaced, runtime)
            return
        super().__setattr__(name, value)

    @property
    def _tree(self) -> BehaviorTreeWidget | None:
        """The owning BehaviorTreeWidget (held weakly to avoid reference cycles)."""
        ref = self.__dict__.get("_tree_ref")
        return ref() if ref is not None else None

    @_tree.setter
    def _tree(self, tree: BehaviorTreeWidget | None) -> None:
        self.__dict__["_tree_ref"] = weakref.ref(tree) if tree is not None else None

    def _apply_title(self, title: Any, runtime: bool) -> None:
        """Store a new title (None -> default title, others -> str) and update the label."""
        title = self._default_title() if title is None else str(title)
        super().__setattr__("_title", title)
        self._call_in_gui_thread(self._on_title_changed, runtime)

    # ------------------------------------------------------------------ ui helpers
    def _default_title(self) -> str:
        title = type(self)._title
        return title if isinstance(title, str) and title else type(self).__name__

    def _resolve_ui_file(self) -> str:
        name = self.UI_FILE
        if not name:
            raise RuntimeError(f"{type(self).__name__} does not define UI_FILE")
        if os.path.isabs(name):
            return name
        # Relative to the module that defines the (sub)class declaring UI_FILE.
        for klass in type(self).__mro__:
            if "UI_FILE" in klass.__dict__:
                module = sys.modules.get(klass.__module__)
                module_file = getattr(module, "__file__", None) if module else None
                if module_file is None:
                    try:
                        module_file = inspect.getfile(klass)
                    except TypeError:
                        module_file = None
                if module_file:
                    candidate = os.path.join(os.path.dirname(os.path.abspath(module_file)), name)
                    if os.path.isfile(candidate) and klass.__module__.split(".")[0] != __package__.split(".")[0]:
                        return candidate
                break
        return name

    def _ui_description(self) -> str:
        return f"The ui file {self.UI_FILE!r} of {type(self).__name__}"

    def _find_child(self, kind: type, name: str):
        if self._ui.objectName() == name and isinstance(self._ui, kind):
            return self._ui
        return self._ui.findChild(kind, name)

    def _require_child(self, kind: type, name: str):
        child = self._find_child(kind, name)
        if child is None:
            raise RuntimeError(f"{self._ui_description()} has no {kind.__name__} named {name!r}")
        return child

    def _hide_connection_frame(self, label_name: str, frame_name: str) -> None:
        frame = self._find_child(QFrame, frame_name)
        if frame is not None and frame is not self._ui:
            frame.hide()
        label = self._find_child(QLabel, label_name)
        if label is not None:
            label.hide()

    def _is_gui_thread(self) -> bool:
        return threading.get_ident() == self._owner_thread

    def _call_in_gui_thread(self, function, *args) -> None:
        """Run ``function(*args)`` now if on the GUI thread, otherwise queue it there."""
        if self._is_gui_thread():
            function(*args)
        else:
            self._queuedCall.emit(functools.partial(function, *args))

    def _run_queued(self, function) -> None:
        try:
            function()
        except RuntimeError:  # widget deleted while the call was queued
            log.debug("queued node update skipped", exc_info=True)
        except Exception:  # noqa: BLE001 - never let an update escape the Qt slot
            log.exception("%s: queued update failed", self)

    def _is_runtime_change(self) -> bool:
        """True if a change made now comes from execution rather than from editing.

        Changes made from worker threads or while the tree executes are run-time
        changes; they do not mark the tree as modified.
        """
        if not self._is_gui_thread():
            return True
        tree = self._tree
        try:
            return tree is not None and tree.IsExecuting()
        except RuntimeError:
            return False

    def _notify_geometry(self) -> None:
        relayout(self)
        self.geometryHintChanged.emit()

    # ------------------------------------------------------------------ identity / title
    def GetId(self) -> str:
        """Unique identifier of this node (used when saving trees)."""
        return self._id

    def GetTypeName(self) -> str:
        """Name under which this node's type is registered and saved."""
        return type(self).__dict__.get("TYPE_NAME") or type(self).__name__

    def GetTitle(self) -> str:
        with self._lock:
            return self._title

    def SetTitle(self, title: str) -> None:
        """Change the title shown on the node (thread-safe)."""
        check_not_cancelled("SetTitle")
        title = str(title)
        if title != self._title:
            self._apply_title(title, self._is_runtime_change())

    def _on_title_changed(self, runtime: bool = False) -> None:
        self._refresh_title()
        self.titleChanged.emit(self.GetTitle())
        self.changed.emit(runtime)
        self._notify_geometry()

    def _on_fields_replaced(self, runtime: bool = False) -> None:
        with self._lock:
            if not isinstance(self._fields, dict):
                super().__setattr__("_fields", {})
            for key in list(self._field_selections):
                if not isinstance(self._fields.get(key), list):
                    del self._field_selections[key]
        self._refresh_all_fields()
        self.changed.emit(runtime)

    def _refresh_all_fields(self) -> None:
        """Hook for nodes that display fields: rebuild all field editors."""

    def DisplayTitle(self) -> str:
        """Text shown in the Title label."""
        return self.GetTitle()

    def _refresh_title(self) -> None:
        self._title_label.setText(self.DisplayTitle())
        self._title_label.setToolTip(self._title_tooltip())

    def _title_tooltip(self) -> str:
        return self.GetTypeName()

    # ------------------------------------------------------------------ status
    def GetStatus(self) -> NodeStatus:
        """Current execution state of this node."""
        return self._status

    def GetError(self) -> str | None:
        """Error message of the last failed run caused by an exception, if any."""
        return self._error

    def _set_status(self, status: NodeStatus, error: str | None = None) -> None:
        """Update the Status label (GUI thread only; used by the executor)."""
        changed = status is not self._status or error != self._error
        self._status = status
        self._error = error
        if changed:
            self._refresh_status()
            self.statusChanged.emit(status.value)
            if self._item is not None:
                self._item.update()

    def _refresh_status(self) -> None:
        self._status_label.setText(self._status.value)
        self._status_label.setStyleSheet(_status_style(self._status))
        self._status_label.setToolTip(self._error or "")

    # ------------------------------------------------------------------ connections
    @classmethod
    def HasParentConnection(cls) -> bool:
        """True if nodes of this type can have a parent."""
        return True

    @classmethod
    def HasChildConnections(cls) -> bool:
        """True if nodes of this type can have children."""
        return False

    @classmethod
    def MaxChildren(cls) -> int | None:
        """How many children a node of this type accepts: ``None`` for any number.

        Connecting one more child to a node that has the maximum replaces its oldest child.
        """
        return None if cls.HasChildConnections() else 0

    def connection_label(self, kind: str) -> QLabel | None:
        """The ParentConnection (``"parent"``) or ChildConnections (``"children"``) label."""
        return self._parent_label if kind == PARENT else self._children_label

    def set_connection_state(self, kind: str, state: ConnectionState) -> None:
        label = self.connection_label(kind)
        if label is not None:
            label.setStyleSheet(_connection_style(state))
            label.setProperty("connectionState", state.value)

    def connection_state(self, kind: str) -> ConnectionState | None:
        label = self.connection_label(kind)
        if label is None:
            return None
        return ConnectionState(label.property("connectionState"))

    def GetParent(self) -> NodeWidget | None:
        with self._lock:
            return self._parent

    def GetChildren(self) -> list[NodeWidget]:
        """Children in execution order: left to right as shown in the view (ties: top first)."""
        with self._lock:
            children = list(self._children)
        return sorted(children, key=lambda node: node._order_key())

    def _order_key(self) -> tuple[float, float]:
        if self._item is None:
            return (0.0, 0.0)
        rect = self._item.sceneBoundingRect()
        return (rect.center().x(), rect.top())

    def SetParent(self, parent: NodeWidget | None) -> None:
        """Connect this node below ``parent`` (``None`` disconnects it). GUI thread only."""
        canvas = self._require_canvas()
        if parent is None:
            canvas.disconnect_node(self)
        else:
            canvas.connect_nodes(parent, self)

    def AddChild(self, child: NodeWidget) -> None:
        """Connect ``child`` below this node. GUI thread only."""
        self._require_canvas().connect_nodes(self, child)

    def RemoveChild(self, child: NodeWidget) -> None:
        """Disconnect ``child`` from this node. GUI thread only."""
        if child.GetParent() is self:
            self._require_canvas().disconnect_node(child)

    def _require_canvas(self):
        if self._canvas is None:
            raise RuntimeError("the node has not been added to a BehaviorTreeWidget")
        return self._canvas

    def GetTree(self) -> BehaviorTreeWidget | None:
        """The BehaviorTreeWidget this node belongs to."""
        return self._tree

    def _on_added_to_tree(self) -> None:
        """Hook called on the GUI thread once the node was added to a tree view (``_tree`` is set)."""

    # ------------------------------------------------------------------ fields
    def GetFields(self) -> dict:
        """A copy of this node's fields."""
        with self._lock:
            return copy.deepcopy(self._fields)

    def GetField(self, key: str) -> Any:
        """Value of field ``key`` (thread-safe). Lists are returned as copies."""
        with self._lock:
            value = self._fields[key]
            return list(value) if isinstance(value, list) else value

    def SetField(self, key: str, value: Any) -> None:
        """Set field ``key`` (creating it if needed) and update its editor (thread-safe)."""
        check_not_cancelled("SetField")
        runtime = self._is_runtime_change()
        with self._lock:
            self._fields[key] = value
            if isinstance(value, list):
                index = self._field_selections.get(key, 0)
                self._field_selections[key] = min(max(index, 0), max(len(value) - 1, 0))
            else:
                self._field_selections.pop(key, None)
        self._call_in_gui_thread(self._on_field_set, key, runtime)

    def _on_field_set(self, key: str, runtime: bool = False) -> None:
        self._refresh_field_editor(key)
        self.fieldChanged.emit(key)
        self.changed.emit(runtime)

    def _refresh_field_editor(self, key: str) -> None:
        """Hook for nodes that display fields: show the current value of ``key``."""

    def GetFieldSelectionIndex(self, key: str) -> int:
        """Index of the item selected in the combo box of list field ``key`` (-1 if empty)."""
        with self._lock:
            value = self._fields[key]
            if not isinstance(value, list):
                raise TypeError(f"field {key!r} is not a list")
            if not value:
                return -1
            return min(max(self._field_selections.get(key, 0), 0), len(value) - 1)

    def GetFieldSelection(self, key: str) -> Any:
        """Item selected in the combo box of list field ``key`` (``None`` if the list is empty)."""
        with self._lock:
            index = self.GetFieldSelectionIndex(key)
            return None if index < 0 else self._fields[key][index]

    def SetFieldSelectionIndex(self, key: str, index: int) -> None:
        """Select item ``index`` of list field ``key`` (thread-safe)."""
        check_not_cancelled("SetFieldSelectionIndex")
        runtime = self._is_runtime_change()
        with self._lock:
            value = self._fields[key]
            if not isinstance(value, list):
                raise TypeError(f"field {key!r} is not a list")
            if not 0 <= index < len(value):
                raise IndexError(f"index {index} out of range for field {key!r}")
            self._field_selections[key] = index
            edited = self.__dict__.get("_edited_selections")
            if edited is not None:
                edited.add(key)
        self._call_in_gui_thread(self._on_field_set, key, runtime)

    # ------------------------------------------------------------------ execution hooks
    def CancelRequested(self) -> bool:
        """True once the running OnRun call should stop early (Stop, Reset, preemption).

        Inside ``OnRun`` on a worker thread this reports the cancellation of that very
        call, even if the node was restarted meanwhile.
        """
        token = current_token()
        if token is None:
            token = self._run_token
        return token is not None and token.is_cancelled()

    # ------------------------------------------------------------------ persistence
    def _to_dict(self) -> dict:
        """Node specific data saved in tree files (besides id, type, title and position)."""
        return {}

    def _load_dict(self, data: dict) -> None:
        """Restore data written by :meth:`_to_dict`."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.GetTitle()!r} {self._id[:8]}>"


_MISSING = object()


class CompositeNodeWidget(NodeWidget):
    """A Sequence or Selector node (executed by ``py_trees.composites``).

    * Sequence: runs children left to right until one fails.
    * Selector: runs children left to right until one succeeds.

    ``memory`` matches the py_trees option: when True a composite resumes from its
    running child on the next tick instead of re-ticking the children before it.
    """

    UI_FILE = "CompositeNode.ui"

    def __init__(self, composite_type: str = SEQUENCE, memory: bool = True, parent: QWidget | None = None):
        if composite_type not in COMPOSITE_TYPES:
            raise ValueError(f"composite_type must be one of {COMPOSITE_TYPES}, not {composite_type!r}")
        self._composite_type = composite_type
        self._memory = bool(memory)
        super().__init__(parent)

    def _default_title(self) -> str:
        return self._composite_type

    @classmethod
    def HasChildConnections(cls) -> bool:
        return True

    def GetTypeName(self) -> str:
        return self._composite_type

    def GetCompositeType(self) -> str:
        """``"Sequence"`` or ``"Selector"``."""
        return self._composite_type

    def SetCompositeType(self, composite_type: str) -> None:
        """Switch between Sequence and Selector (GUI thread only)."""
        if composite_type not in COMPOSITE_TYPES:
            raise ValueError(f"composite_type must be one of {COMPOSITE_TYPES}, not {composite_type!r}")
        if composite_type == self._composite_type:
            return
        old = self._composite_type
        self._composite_type = composite_type
        if self._title == old:
            self._title = composite_type  # __setattr__ refreshes the label and emits changed
        else:
            self._on_title_changed(self._is_runtime_change())

    def GetMemory(self) -> bool:
        return self._memory

    def SetMemory(self, memory: bool) -> None:
        """Set the py_trees ``memory`` flag of this composite."""
        memory = bool(memory)
        if memory == self._memory:
            return
        self._memory = memory
        self._refresh_title()
        self.changed.emit(self._is_runtime_change())

    def DisplayTitle(self) -> str:
        title = self.GetTitle()
        if title == self._composite_type:
            return title
        return f"{title} ({self._composite_type})"

    def _title_tooltip(self) -> str:
        return f"{self._composite_type} - memory {'on' if self._memory else 'off'}"

    def _to_dict(self) -> dict:
        return {"memory": self._memory}

    def _load_dict(self, data: dict) -> None:
        if isinstance(data.get("memory"), bool):
            self.SetMemory(data["memory"])


class RootNodeWidget(CompositeNodeWidget):
    """The root of the tree: a Sequence or Selector without a parent. It cannot be deleted."""

    UI_FILE = "RootNode.ui"
    TYPE_NAME = "Root"

    def __init__(self, composite_type: str = SEQUENCE, memory: bool = True, parent: QWidget | None = None):
        super().__init__(composite_type, memory, parent)

    def _default_title(self) -> str:
        return "Root"

    @classmethod
    def HasParentConnection(cls) -> bool:
        return False

    def GetTypeName(self) -> str:
        return "Root"

    def _to_dict(self) -> dict:
        return {"composite": self._composite_type, "memory": self._memory}

    def _load_dict(self, data: dict) -> None:
        if data.get("composite") in COMPOSITE_TYPES:
            self.SetCompositeType(data["composite"])
        super()._load_dict(data)


class NegationNodeWidget(NodeWidget):
    """Inverts the result of its single child (executed by ``py_trees.decorators.Inverter``).

    The node fails when its child succeeds and succeeds when its child fails; while the
    child runs, it runs. It accepts one child: connecting another child replaces it. A
    Negation without a child fails when it is executed.
    """

    UI_FILE = "NegationNode.ui"
    TYPE_NAME = NEGATION
    _title = NEGATION

    @classmethod
    def HasChildConnections(cls) -> bool:
        return True

    @classmethod
    def MaxChildren(cls) -> int | None:
        return 1

    def GetTypeName(self) -> str:
        return NEGATION

    def GetChild(self) -> NodeWidget | None:
        """The child whose result is inverted (None when it has none)."""
        children = self.GetChildren()
        return children[0] if children else None

    def DisplayTitle(self) -> str:
        title = self.GetTitle()
        return title if title == NEGATION else f"{title} ({NEGATION})"

    def _title_tooltip(self) -> str:
        return f"{NEGATION} - succeeds when its child fails and fails when its child succeeds"


class _FieldRow(QWidget):
    """One row of the Fields list: a read-only key QLineEdit and a value editor."""

    def __init__(self, key: str, editor: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName(f"FieldRow_{key}")
        self.key_edit = QLineEdit(key, self)
        self.key_edit.setObjectName("FieldKey")
        self.key_edit.setReadOnly(True)
        self.key_edit.setToolTip(key)
        self.key_edit.setCursorPosition(0)
        self.key_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.key_edit.setProperty("btDragArea", True)  # dragging on a field name moves the node
        self.editor = editor
        editor.setParent(self)
        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.key_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(4)
        layout.addWidget(self.key_edit, 1)
        layout.addWidget(editor, 1)


class LeafNodeWidget(NodeWidget):
    """Base class for custom leaf nodes: override :meth:`OnRun`.

    Fields: declare a ``_fields`` dict on the subclass (or assign one in
    ``__init__``). Supported value types are ``int`` (QSpinBox), ``float``
    (QDoubleSpinBox), ``str`` (QLineEdit), ``bool`` (QCheckBox) and ``list``
    (QComboBox listing ``str()`` of each item; the chosen item is returned by
    :meth:`GetFieldSelection`). Edits in the UI update ``self._fields`` at once.

    Threading: when ``RUN_IN_THREAD`` is True (default) :meth:`OnRun` runs on a
    worker thread so long operations do not freeze the UI. It must then not touch
    Qt widgets; use ``tree.GetEntry``/``tree.SetEntry``, ``self.GetField``/``SetField``
    and ``self.CancelRequested()`` which are thread-safe. The tick waits up to
    ``THREAD_WAIT`` seconds for the call, so quick calls finish within one tick.
    Set ``RUN_IN_THREAD = False`` to run OnRun on the GUI thread instead (it must
    then return quickly).
    """

    UI_FILE = "LeafNode.ui"
    RUN_IN_THREAD: bool = True
    THREAD_WAIT: float = 0.02
    FLOAT_DECIMALS: int = 6

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._fields_frame: QFrame | None = self._find_child(QFrame, "FrameFields")
        self._fields_view: QListView | None = self._find_child(QListView, "Fields")
        self._editors: dict[str, QWidget] = {}
        self._editor_kinds: dict[str, str] = {}
        self._edited_selections: set = set()  # list fields whose selection was changed after loading
        self._fields_model: QStandardItemModel | None = None
        if self._fields_view is not None:
            view = self._fields_view
            view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            view.setUniformItemSizes(False)
            view.setSpacing(0)
            view.setProperty("btDragArea", True)
            view.viewport().setProperty("btDragArea", True)
        self._rebuild_fields()

    @classmethod
    def HasChildConnections(cls) -> bool:
        return False

    # ------------------------------------------------------------------ user API
    def OnRun(self, tree: BehaviorTreeWidget):
        """Execute this node. Override in subclasses.

        Args:
            tree: The BehaviorTreeWidget running the tree (``tree.GetEntry(name)``,
                ``tree.SetEntry(value, name)`` access the blackboard). Overrides
                may also be declared without this argument (``def OnRun(self)``).

        Returns:
            ``True``/``py_trees.common.Status.SUCCESS`` for success,
            ``False``/``Status.FAILURE`` for failure, or ``Status.RUNNING`` to be
            called again on the next tick.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement OnRun")

    def OnStart(self, tree: BehaviorTreeWidget) -> None:
        """Called on the GUI thread when the node starts running (before OnRun). Optional."""

    def OnTerminate(self, tree: BehaviorTreeWidget, status: NodeStatus) -> None:
        """Called on the GUI thread once when the node stops running.

        ``status`` is Succeeded/Failed when it finished, or Ready when it was
        interrupted (Stop, Reset or preempted by its parent). An interrupted
        worker-thread ``OnRun`` call may still be returning at that moment; it sees
        ``CancelRequested()`` and its result is ignored. Optional.
        """

    # ------------------------------------------------------------------ fields ui
    def _refresh_all_fields(self) -> None:
        if "_editors" in self.__dict__:
            self._rebuild_fields()

    def _rebuild_fields(self) -> None:
        if self._fields_view is None or self._fields_frame is None:
            return
        view = self._fields_view
        old_model = self._fields_model
        old_editors = list(self._editors.values())
        self._editors.clear()
        self._editor_kinds.clear()
        for editor in old_editors:
            if shiboken6.isValid(editor):
                editor.deleteLater()

        with self._lock:
            items = list(self._fields.items())
        model = QStandardItemModel(view)
        view.setModel(model)
        self._fields_model = model
        if old_model is not None:
            old_model.deleteLater()

        rows: list[_FieldRow] = []
        editors: dict = {}
        kinds: dict = {}
        for key, value in items:
            item = QStandardItem()
            item.setEditable(False)
            item.setSelectable(False)
            model.appendRow(item)
            row = _FieldRow(str(key), self._create_editor(key, value))
            editors[key] = row.editor
            kinds[key] = field_kind(value)
            rows.append(row)

        total_height = 0
        max_width = 0
        for index, row in enumerate(rows):
            hint = row.sizeHint()
            model.item(index).setSizeHint(QSize(hint.width(), hint.height()))
            view.setIndexWidget(model.index(index, 0), row)
            total_height += hint.height()
            max_width = max(max_width, hint.width())
        # Only editors of installed rows are kept (a failure above leaves no dangling editors).
        self._editors.update(editors)
        self._editor_kinds.update(kinds)

        if rows:
            frame = 2 * view.frameWidth()
            view.setFixedHeight(total_height + frame)
            view.setMinimumWidth(max_width + frame)
            self._fields_frame.show()
        else:
            self._fields_frame.hide()
        self._notify_geometry()

    def _create_editor(self, key: str, value: Any) -> QWidget:
        kind = field_kind(value)
        if kind == "bool":
            editor = QCheckBox()
            editor.setChecked(value)
            editor.toggled.connect(functools.partial(self._on_editor_value, key))
        elif kind == "int":
            editor = QSpinBox()
            editor.setRange(INT_MIN, INT_MAX)
            editor.setValue(min(max(value, INT_MIN), INT_MAX))
            editor.valueChanged.connect(functools.partial(self._on_editor_value, key))
            _guard_wheel(editor)
        elif kind == "float":
            editor = QDoubleSpinBox()
            editor.setDecimals(self.FLOAT_DECIMALS)
            editor.setRange(-sys.float_info.max, sys.float_info.max)
            editor.setSingleStep(0.1)
            editor.setValue(value)
            editor.valueChanged.connect(functools.partial(self._on_editor_value, key))
            _guard_wheel(editor)
        elif kind == "str":
            editor = QLineEdit()
            editor.setText(value)
            editor.setCursorPosition(0)
            editor.textChanged.connect(functools.partial(self._on_editor_value, key))
        elif kind == "list":
            editor = QComboBox()
            editor.addItems([_item_text(item) for item in value])
            with self._lock:
                index = self._field_selections.setdefault(key, 0)
            if value:
                editor.setCurrentIndex(min(max(index, 0), len(value) - 1))
            editor.currentIndexChanged.connect(functools.partial(self._on_editor_selection, key))
            _guard_wheel(editor)
        else:
            editor = QLineEdit()
            editor.setText(_display_repr(value))
            editor.setCursorPosition(0)
            editor.setReadOnly(True)
            editor.setEnabled(False)
            editor.setToolTip(f"Fields of type {type(value).__name__} cannot be edited")
        editor.setObjectName(f"Field_{key}")
        if kind != "other":
            editor.setToolTip(str(key))
        return editor

    def _on_editor_value(self, key: str, value: Any) -> None:
        with self._lock:
            self._fields[key] = value
        self.fieldChanged.emit(key)
        self.fieldEdited.emit(key)
        self.changed.emit(False)

    def _on_editor_selection(self, key: str, index: int) -> None:
        if index < 0:
            return
        with self._lock:
            self._field_selections[key] = index
            self._edited_selections.add(key)
        self.fieldChanged.emit(key)
        self.fieldEdited.emit(key)
        self.changed.emit(False)

    def _refresh_field_editor(self, key: str) -> None:
        with self._lock:
            value = self._fields.get(key, _MISSING)
            selection = self._field_selections.get(key, 0)
        editor = self._editors.get(key)
        if value is _MISSING or editor is None or self._editor_kinds.get(key) != field_kind(value):
            self._rebuild_fields()
            return
        editor.blockSignals(True)
        try:
            if isinstance(editor, QCheckBox):
                editor.setChecked(bool(value))
            elif isinstance(editor, QSpinBox):
                clamped = min(max(value, INT_MIN), INT_MAX)
                if editor.value() != clamped:
                    editor.setValue(clamped)
            elif isinstance(editor, QDoubleSpinBox):
                if editor.value() != value:
                    editor.setValue(value)
            elif isinstance(editor, QComboBox):
                items = [_item_text(item) for item in value]
                if [editor.itemText(i) for i in range(editor.count())] != items:
                    editor.clear()
                    editor.addItems(items)
                if items:
                    editor.setCurrentIndex(min(max(selection, 0), len(items) - 1))
            elif isinstance(editor, QLineEdit) and not editor.isReadOnly():
                if editor.text() != value:
                    editor.setText(value)
                    if not editor.hasFocus():
                        editor.setCursorPosition(0)  # show the start of long text
            else:
                editor.setText(_display_repr(value))
                editor.setCursorPosition(0)
        finally:
            editor.blockSignals(False)

    def field_editor(self, key: str) -> QWidget | None:
        """The editor widget displaying field ``key`` (for tests and customisation)."""
        return self._editors.get(key)

    # ------------------------------------------------------------------ persistence
    def _to_dict(self) -> dict:
        with self._lock:
            fields = dict(self._fields)
            selections = {
                key: self._field_selections.get(key, 0)
                for key, value in self._fields.items()
                if isinstance(value, list)
            }
        data: dict = {"fields": fields}
        if selections:
            data["field_selections"] = selections
        return data

    def _load_dict(self, data: dict) -> None:
        """Apply saved field values over the class defaults.

        Saved values replace defaults for keys the class still declares when the
        value kind matches (an ``int`` may be loaded into a ``float`` field).
        Saved keys the class no longer declares are ignored.
        """
        saved = data.get("fields")
        selections = data.get("field_selections")
        with self._lock:
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key not in self._fields:
                        log.warning("%s: ignoring saved field %r that the node no longer declares", self, key)
                        continue
                    default_kind = field_kind(self._fields[key])
                    kind = field_kind(value)
                    if kind == default_kind:
                        self._fields[key] = value
                    elif default_kind == "float" and kind == "int":
                        self._fields[key] = float(value)
                    else:
                        log.warning(
                            "%s: ignoring saved field %r of type %s (expected %s)",
                            self, key, type(value).__name__, type(self._fields[key]).__name__,
                        )
            if isinstance(selections, dict):
                for key, index in selections.items():
                    value = self._fields.get(key)
                    if isinstance(value, list) and isinstance(index, int) and not isinstance(index, bool):
                        self._field_selections[key] = min(max(index, 0), max(len(value) - 1, 0))
        self._rebuild_fields()


class UnknownLeafNodeWidget(LeafNodeWidget):
    """Placeholder for a saved node whose type is not registered.

    It keeps the saved type name, title and fields so saving the tree again loses
    nothing, shows "(unknown type)" after its title and fails when executed.
    """

    RUN_IN_THREAD = False

    def __init__(self, type_name: str, parent: QWidget | None = None):
        self._unknown_type = str(type_name)
        self._raw_fields: dict = {}  # saved field values that could not be decoded (kept as saved)
        self._raw_extra: dict = {}  # other saved per-node keys (kept as saved)
        self._raw_selections: dict = {}  # saved field_selections (kept as saved)
        super().__init__(parent)

    def _default_title(self) -> str:
        return self._unknown_type

    def GetTypeName(self) -> str:
        return self._unknown_type

    def DisplayTitle(self) -> str:
        return f"{self.GetTitle()} (unknown type)"

    def _title_tooltip(self) -> str:
        return f"Node type {self._unknown_type!r} is not registered with this BehaviorTreeWidget (unknown type)"

    def OnRun(self, tree):
        raise RuntimeError(f"node type {self._unknown_type!r} is not registered")

    def _load_dict(self, data: dict) -> None:
        saved = data.get("fields")
        selections = data.get("field_selections")
        self._raw_selections = dict(selections) if isinstance(selections, dict) else {}
        with self._lock:
            self._field_selections = (
                {key: index for key, index in selections.items() if isinstance(index, int) and not isinstance(index, bool)}
                if isinstance(selections, dict)
                else {}
            )
        # Assigning _fields rebuilds the editors (see NodeWidget.__setattr__). The loader passes
        # freshly decoded values, so no deep copy (which fails on deeply nested sets).
        self._fields = dict(saved) if isinstance(saved, dict) else {}


INTERACTIVE_WIDGET_TYPES = (
    QLineEdit,
    QAbstractSpinBox,
    QAbstractButton,
    QComboBox,
    QAbstractSlider,
    QTextEdit,
    QPlainTextEdit,
    QAbstractItemView,
)


def is_interactive_widget(widget: QWidget | None, stop_at: QWidget) -> bool:
    """True if a click on ``widget`` should go to the widget rather than drag the node.

    Input widgets (line edits, spin boxes, buttons, check boxes, combo boxes, sliders,
    text edits and item views) are interactive. Labels, frames and plain containers
    are drag areas, as is anything with the dynamic property ``btDragArea`` set.
    """
    current = widget
    while current is not None and current is not stop_at:
        if current.property("btDragArea"):
            return False
        if isinstance(current, INTERACTIVE_WIDGET_TYPES):
            return True
        current = current.parentWidget()
    return False
