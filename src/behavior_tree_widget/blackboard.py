"""Blackboard: typed named values shared by the nodes of a tree.

:class:`BlackboardStore` keeps the values in a ``py_trees`` blackboard (one private
namespace per widget, see :meth:`BlackboardStore.namespace`) and is safe to use from
any thread. :class:`BlackboardView` is the "Blackboard" tab listing the entries.

Entry types and their editors:

============  ==================  =====================================
Type          Python value        Editor (ui file)
============  ==================  =====================================
Integer       int (32 bit)        QSpinBox (IntegerEntry.ui)
Double        float               QDoubleSpinBox (FloatEntry.ui)
String        str                 QLineEdit (StringEntry.ui)
Bool          bool                QCheckBox (BoolEntry.ui)
List          list                QComboBox + Edit button (ListEntry.ui)
Dictionary    dict                QComboBox + Edit button (ListEntry.ui)
Set           set                 QComboBox + Edit button (ListEntry.ui)
Object        anything else       read-only text (StringEntry.ui); only
                                  created by ``SetEntry`` from code
============  ==================  =====================================
"""

from __future__ import annotations

import ast
import copy
import logging
import math
import reprlib
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any

import py_trees
import shiboken6
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeyEvent, QKeySequence, QValidator
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ._runctx import check_not_cancelled
from ._ui import load_ui
from .serialization import decode_value, encode_value

log = logging.getLogger("behavior_tree_widget")

INT_MIN = -(2**31)
INT_MAX = 2**31 - 1
FLOAT_DECIMALS = 8
DISPLAY_LIMIT = 500


@dataclass(frozen=True)
class EntryType:
    name: str
    ui_file: str
    value_widget: str
    default: Any
    user_creatable: bool = True
    container: bool = False
    example: str = ""


ENTRY_TYPES: dict[str, EntryType] = {
    "Integer": EntryType("Integer", "IntegerEntry.ui", "Value", 0),
    "Double": EntryType("Double", "FloatEntry.ui", "Value", 0.0),
    "String": EntryType("String", "StringEntry.ui", "Value", ""),
    "Bool": EntryType("Bool", "BoolEntry.ui", "Value", False),
    "List": EntryType("List", "ListEntry.ui", "Values", [], container=True, example="[1, 2.5, 'text']"),
    "Dictionary": EntryType("Dictionary", "ListEntry.ui", "Values", {}, container=True, example="{'key': 'value', 'n': 3}"),
    "Set": EntryType("Set", "ListEntry.ui", "Values", set(), container=True, example="{1, 2, 3}"),
    "Object": EntryType("Object", "StringEntry.ui", "Value", None, user_creatable=False),
}

TYPE_ALIASES = {
    "int": "Integer", "integer": "Integer",
    "double": "Double", "float": "Double",
    "str": "String", "string": "String",
    "bool": "Bool", "boolean": "Bool",
    "list": "List",
    "dict": "Dictionary", "dictionary": "Dictionary",
    "set": "Set",
    "object": "Object",
}

INVALID_NAME_CHARACTERS = "./"


def canonical_type(type_name: str) -> str:
    """Return the canonical entry type name for ``type_name`` (case-insensitive, aliases allowed)."""
    if type_name in ENTRY_TYPES:
        return type_name
    canonical = TYPE_ALIASES.get(str(type_name).strip().lower())
    if canonical is None:
        raise ValueError(f"unknown blackboard entry type {type_name!r}; expected one of {', '.join(ENTRY_TYPES)}")
    return canonical


def infer_type(value: Any) -> str:
    """Entry type used when :meth:`BlackboardStore.set` creates an entry for ``value``."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Integer" if INT_MIN <= value <= INT_MAX else "Object"
    if isinstance(value, float):
        return "Double"
    if isinstance(value, str):
        return "String"
    if isinstance(value, (list, tuple)):
        return "List"
    if isinstance(value, dict):
        return "Dictionary"
    if isinstance(value, (set, frozenset)):
        return "Set"
    return "Object"


def _deepcopy_container(type_name: str, value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception as error:  # noqa: BLE001 - e.g. "cannot pickle '_thread.lock' object"
        raise TypeError(
            f"{type_name} entries must hold copyable values ({error}); "
            f"store such values in an Object entry: AddEntry(name, 'Object', value)"
        ) from error


def coerce_value(type_name: str, value: Any) -> Any:
    """Validate ``value`` for an entry of ``type_name`` and return the value to store.

    Raises:
        TypeError: the value has an incompatible type.
        ValueError: the value is out of range (e.g. an Integer outside 32 bits).
    """
    if type_name == "Integer":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"Integer entries need an int, not {type(value).__name__}")
        value = int(value)
        if not INT_MIN <= value <= INT_MAX:
            raise ValueError(f"{value} is outside the Integer entry range [{INT_MIN}, {INT_MAX}]")
        return value
    if type_name == "Double":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Double entries need a float, not {type(value).__name__}")
        try:
            return float(value)
        except OverflowError as error:
            raise ValueError(f"{value} is too large for a Double entry") from error
    if type_name == "String":
        if not isinstance(value, str):
            raise TypeError(f"String entries need a str, not {type(value).__name__}")
        return value
    if type_name == "Bool":
        if not isinstance(value, bool):
            raise TypeError(f"Bool entries need a bool, not {type(value).__name__}")
        return value
    if type_name == "List":
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"List entries need a list, not {type(value).__name__}")
        return _deepcopy_container(type_name, list(value))
    if type_name == "Dictionary":
        if not isinstance(value, dict):
            raise TypeError(f"Dictionary entries need a dict, not {type(value).__name__}")
        return _deepcopy_container(type_name, value)
    if type_name == "Set":
        if isinstance(value, dict) and not value:
            return set()
        if not isinstance(value, (set, frozenset, list, tuple)):
            raise TypeError(f"Set entries need a set, not {type(value).__name__}")
        items = _deepcopy_container(type_name, list(value))
        try:
            return set(items)
        except TypeError as error:
            raise TypeError(f"Set entries can only hold hashable values: {error}") from error
    if type_name == "Object":
        return value
    raise ValueError(f"unknown blackboard entry type {type_name!r}")


class _NonFiniteNames(ast.NodeTransformer):
    """Accept ``nan`` / ``inf`` (as written by ``repr``) inside container literals."""

    NAMES = {"nan": float("nan"), "inf": float("inf"), "infinity": float("inf")}

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802 - ast API
        value = self.NAMES.get(node.id.lower())
        if value is None:
            return node
        return ast.copy_location(ast.Constant(value=value), node)


def literal_eval_extended(text: str) -> Any:
    """``ast.literal_eval`` that also accepts ``nan``, ``inf`` and ``-inf``."""
    tree = ast.parse(text.strip(), mode="eval")
    tree = ast.fix_missing_locations(_NonFiniteNames().visit(tree))
    return ast.literal_eval(tree)


def parse_container_text(type_name: str, text: str) -> Any:
    """Parse a Python literal typed by the user for a List / Dictionary / Set entry."""
    text = text.strip()
    if not text:
        return coerce_value(type_name, ENTRY_TYPES[type_name].default)
    try:
        value = literal_eval_extended(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as error:
        example = ENTRY_TYPES[type_name].example
        raise ValueError(f"Enter a Python literal such as {example}") from error
    try:
        return coerce_value(type_name, value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{error}. Example: {ENTRY_TYPES[type_name].example}") from error


def safe_str(value: Any) -> str:
    """``str(value)`` that never raises (falls back to a bounded repr)."""
    try:
        return _shorten(str(value))
    except Exception:  # noqa: BLE001 - a broken __str__
        return safe_repr(value)


def container_items(type_name: str, value: Any) -> list[str]:
    """Strings shown in the combo box of a container entry."""
    if type_name == "Dictionary":
        return [f"{safe_str(key)}: {safe_str(item)}" for key, item in value.items()]
    if type_name == "Set":
        items = [safe_str(item) for item in value]
        return sorted(items)
    return [safe_str(item) for item in value]


def _shorten(text: str) -> str:
    return text if len(text) <= DISPLAY_LIMIT else text[: DISPLAY_LIMIT - 3] + "..."


_bounded_repr = reprlib.Repr()
_bounded_repr.maxstring = DISPLAY_LIMIT
_bounded_repr.maxother = DISPLAY_LIMIT
_bounded_repr.maxlong = DISPLAY_LIMIT
for _name in ("maxlist", "maxtuple", "maxset", "maxfrozenset", "maxdeque", "maxarray", "maxdict"):
    setattr(_bounded_repr, _name, 100)
_bounded_repr.maxlevel = 10


def safe_repr(value: Any) -> str:
    """A repr for display: bounded in length (and, for big containers and bytes, in cost),
    exact for small containers, and never raising."""
    try:
        if isinstance(value, (bytes, bytearray, memoryview)) and len(value) > DISPLAY_LIMIT:
            head = bytes(value[: DISPLAY_LIMIT // 4])
            return _shorten(f"{head!r}... ({len(value)} bytes)")
        if isinstance(value, (list, tuple, set, frozenset, dict)) and len(value) <= 100:
            return _shorten(repr(value))  # exact, in the value's own order
        return _shorten(_bounded_repr.repr(value))
    except Exception:  # noqa: BLE001 - a broken __repr__
        return f"<{type(value).__name__} object>"


def literal_text(value: Any) -> str:
    """Python literal text of a container value for the Edit dialog ('' if it has none)."""
    try:
        return repr(value)
    except Exception:  # noqa: BLE001
        return ""


_DBL_MAX = sys.float_info.max


def _parse_float_text(text: str) -> float | None:
    """The float typed in ``text`` (``,`` accepted as decimal point), or None (not a number, or nan)."""
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return None if math.isnan(value) else value


class FloatSpinBox(QDoubleSpinBox):
    """Double spin box that shows any float exactly (no fixed decimals), nan and +/-inf.

    Used for Double blackboard entries. Values are shown in the shortest form that
    round-trips (``repr``, e.g. ``1e-09``), so nudging a tiny value does not lose it.
    Qt keeps its own copy of the value (rounded to 323 decimals, clamped to the float
    range, 0 for nan), so the exact value is kept alongside: :meth:`float_value` returns
    it, and ``valueChanged`` is also emitted when a typed value differs from the exact
    one but not from Qt's copy (e.g. typing ``0`` over ``nan``); without keyboard
    tracking, when editing is finished. Typing ``inf``/``-inf`` gives infinity, ``,`` is
    accepted as decimal point, nan cannot be typed (but undo can bring it back).
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._exact: float | None = 0.0  # the value shown ...
        self._exact_at: float | None = 0.0  # ... while Qt's value is this (None: being set)
        # Keyboard tracking off: a typed value equal to Qt's copy of the value shown,
        # committed and reported at editingFinished.
        self._pending_exact: float | None = None
        self._pending_report = False
        self.setDecimals(323)
        self.setRange(-_DBL_MAX, _DBL_MAX)
        # Connected after QAbstractSpinBox's own handler, so these run after Qt has
        # interpreted the text.
        self.valueChanged.connect(self._on_own_value_changed)
        self.lineEdit().textChanged.connect(self._on_text_changed)
        self.editingFinished.connect(self._on_editing_finished)

    # ------------------------------------------------------------------ value
    def set_float(self, value: float) -> None:
        """Show ``value`` (any float, including nan and +/-inf) without emitting signals."""
        blocked = self.blockSignals(True)
        try:
            self._pending_exact, self._pending_report = None, False
            self._exact, self._exact_at = value, None
            if math.isnan(value):
                self.setValue(0.0)
            else:
                self.setValue(max(-_DBL_MAX, min(_DBL_MAX, value)))
            self._exact_at = self.value()
            text = self.textFromValue(self._exact_at)
            if self.lineEdit().text() != text:
                self.lineEdit().setText(text)
        finally:
            self.blockSignals(blocked)

    def float_value(self) -> float:
        """The value shown: exact, nan and +/-inf included."""
        value = self.value()
        if self._exact is not None and value == self._exact_at:
            return self._exact
        return self._default_exact(value)

    @staticmethod
    def _default_exact(value: float) -> float:
        # Qt's value clamps infinity to the largest float.
        return math.copysign(math.inf, value) if abs(value) >= _DBL_MAX else value

    @staticmethod
    def _consistent(typed: float, value: float) -> bool:
        """Whether Qt's ``value`` can be its (clamped, rounded) copy of ``typed``."""
        if math.isinf(typed):
            return value == math.copysign(_DBL_MAX, typed)
        return math.isclose(typed, value, rel_tol=1e-12, abs_tol=1e-322)

    def _adopt(self, typed: float) -> None:
        self._exact, self._exact_at = typed, self.value()

    def _on_own_value_changed(self, value: float) -> None:
        self._pending_exact, self._pending_report = None, False
        typed = _parse_float_text(self.lineEdit().text())
        if typed is not None and self._consistent(typed, value):
            self._exact, self._exact_at = typed, value  # e.g. a subnormal number, or "inf"
        elif value != self._exact_at:
            self._exact, self._exact_at = self._default_exact(value), value
        # else: e.g. Enter pressed on "nan" - the value shown stays

    def _on_text_changed(self, text: str) -> None:
        tracking = self.keyboardTracking()
        if not tracking:
            self._pending_exact, self._pending_report = None, False
        typed = _parse_float_text(text)
        if typed is None:
            if text.strip().lower() != "nan" or math.isnan(self.float_value()):
                return  # not a value (yet)
            # nan cannot be typed (validate), so undo/redo brought back a nan shown before;
            # Qt rejects the text and keeps its value.
            typed = math.nan
        elif not self._consistent(typed, self.value()):
            return  # Qt has not taken it (yet)
        else:
            exact = self.float_value()
            if typed == exact and math.copysign(1.0, typed) == math.copysign(1.0, exact):
                return  # nothing new (e.g. Qt already reported the change)
        # A new value that Qt does not see as a change (it equals Qt's copy of the old one).
        if tracking:
            self._adopt(typed)
            self.valueChanged.emit(self.value())
        else:
            self._pending_exact, self._pending_report = typed, True

    def _on_editing_finished(self) -> None:
        pending, self._pending_exact = self._pending_exact, None
        if self._pending_report:
            self._pending_report = False
            if pending is not None and self.value() == self._exact_at:
                self._exact = pending
                self.valueChanged.emit(self.value())

    def stepBy(self, steps: int) -> None:  # noqa: N802 - Qt API
        text = self.lineEdit().text()
        typed = _parse_float_text(text)
        if (
            not self.keyboardTracking()
            and typed is not None
            and self.validate(text, len(text))[0] == QValidator.State.Acceptable
            and self.valueFromText(text) != self.value()
        ):
            exact = self.float_value()
            if typed != exact or math.copysign(1.0, typed) != math.copysign(1.0, exact):
                # Qt first takes the typed value (without valueChanged), then steps from it
                # and reports the step: the result must not be taken for the old exact value.
                self._pending_exact, self._pending_report = None, False
                self._exact, self._exact_at = typed, self.valueFromText(text)
        super().stepBy(steps)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        # A typed or pasted "," goes into the text as "." through QLineEdit's own insert
        # path (a text that validate() has to change is set anew, which clears undo).
        edit = self.lineEdit()
        if not edit.isReadOnly():
            if event.matches(QKeySequence.StandardKey.Paste):
                clipboard = QGuiApplication.clipboard().text()
                if "," in clipboard:
                    # Its own undo step, like QLineEdit's paste (insert() alone would merge it
                    # with the typing before and after it).
                    self._separate_undo(edit)
                    edit.insert(clipboard.replace(",", "."))
                    self._separate_undo(edit)
                    event.accept()
                    return
            elif "," in event.text():
                replaced = QKeyEvent(
                    event.type(), event.key(), event.modifiers(), event.text().replace(",", "."),
                    event.isAutoRepeat(), event.count(),
                )
                super().keyPressEvent(replaced)
                event.setAccepted(replaced.isAccepted())
                return
        super().keyPressEvent(event)

    @staticmethod
    def _separate_undo(edit: QLineEdit) -> None:
        """Make the next edit start a new undo step, as QLineEdit's own paste does. QLineEdit
        starts one when the cursor moves, when a selection is removed (so a selection needs
        nothing) and on clear() (which changes nothing in an empty text)."""
        if edit.hasSelectedText():
            return
        position, size = edit.cursorPosition(), len(edit.text())
        if size:
            edit.setCursorPosition(0 if position else size)
            edit.setCursorPosition(position)
        else:
            edit.clear()

    # ------------------------------------------------------------------ text
    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt API
        if self._pending_exact is not None and value == self._exact_at:
            return repr(self._pending_exact)
        if self._exact is not None and (self._exact_at is None or value == self._exact_at):
            return repr(self._exact)
        value = float(value)
        if abs(value) >= _DBL_MAX and _parse_float_text(self.lineEdit().text()) == value:
            return repr(value)  # the largest float was typed, not infinity
        return repr(self._default_exact(value))

    def valueFromText(self, text: str) -> float:  # noqa: N802 - Qt API
        value = _parse_float_text(text)
        if value is None:
            return self.value()
        exact = self._exact
        if (
            self._exact_at is not None
            and exact is not None
            and value == exact
            and math.copysign(1.0, value) == math.copysign(1.0, exact)
        ):
            return self._exact_at  # the value shown: keep Qt's (rounded) copy, nothing changed
        return max(-_DBL_MAX, min(_DBL_MAX, value))

    def validate(self, text: str, position: int):  # noqa: D102 - Qt API
        normalized = text.replace(",", ".")
        stripped = normalized.strip()
        try:
            number = float(stripped)
        except ValueError:
            number = None
        if number is not None:
            state = QValidator.State.Invalid if math.isnan(number) else QValidator.State.Acceptable
            return (state, normalized, position)
        partial = stripped.lower()
        if partial in ("", "-", "+", ".", "-.", "+.") or partial.endswith(("e", "e-", "e+")):
            return (QValidator.State.Intermediate, normalized, position)
        for word in ("inf", "-inf", "+inf", "infinity", "-infinity", "+infinity"):
            if word.startswith(partial):
                return (QValidator.State.Intermediate, normalized, position)
        return (QValidator.State.Invalid, text, position)


def _same_value(a: Any, b: Any) -> bool:
    """Whether two scalar entry values are the same (nan equals nan, -0.0 differs from 0.0)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001
        return False


def _copy_value(type_name: str, value: Any) -> Any:
    if ENTRY_TYPES[type_name].container:
        return copy.deepcopy(value)
    return value


class BlackboardStore(QObject):
    """Thread-safe typed blackboard backed by a ``py_trees.blackboard.Client``.

    Signals are emitted from the thread that made the change; connect them to
    methods of QObjects living in the GUI thread so Qt queues them. ``changed``
    carries ``True`` for run-time changes (made from a worker thread or while
    the tree executes), which do not count as edits of the tree file.
    """

    entryAdded = Signal(str)
    entryRemoved = Signal(str)
    entryChanged = Signal(str)
    entryRenamed = Signal(str, str)  # old name, new name
    entryRetyped = Signal(str)
    entriesReordered = Signal()
    changed = Signal(bool)  # any change; True for run-time changes

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._types: dict[str, str] = {}
        self._disposed = False
        self._runtime_active = False
        self._gui_thread = threading.get_ident()
        self._namespace = f"/behavior_tree_widget_{uuid.uuid4().hex[:12]}"
        self._client = py_trees.blackboard.Client(name="BehaviorTreeWidget", namespace=self._namespace)

    # ------------------------------------------------------------------ queries
    def namespace(self) -> str:
        """py_trees blackboard namespace holding this store's keys (e.g. ``/behavior_tree_widget_ab12``)."""
        return self._namespace

    def key(self, name: str) -> str:
        """Absolute py_trees blackboard key of entry ``name``."""
        return f"{self._namespace}/{name}"

    def client(self) -> py_trees.blackboard.Client:
        return self._client

    def names(self) -> list[str]:
        with self._lock:
            return list(self._types)

    def types(self) -> dict[str, str]:
        """``{name: type}`` of every entry, in display order."""
        with self._lock:
            return dict(self._types)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._types

    def type_of(self, name: str) -> str:
        with self._lock:
            try:
                return self._types[name]
            except (KeyError, TypeError):
                raise KeyError(f"no blackboard entry named {name!r}") from None

    def get(self, name: str) -> Any:
        """Value of entry ``name``; containers are returned as copies. Raises KeyError."""
        with self._lock:
            type_name = self.type_of(name)
            return _copy_value(type_name, self._client.get(self.key(name)))

    def items(self) -> list[tuple[str, str, Any]]:
        """``(name, type, value)`` for every entry, in display order."""
        with self._lock:
            return [(name, type_name, self.get(name)) for name, type_name in self._types.items()]

    def is_disposed(self) -> bool:
        return self._disposed

    # ------------------------------------------------------------------ names
    @staticmethod
    def check_name_format(name: Any) -> str:
        """Check the format of an entry name (not its uniqueness); raise ValueError/TypeError."""
        if not isinstance(name, str):
            raise TypeError(f"entry names must be str, not {type(name).__name__}")
        if not name.strip():
            raise ValueError("Entry names cannot be empty.")
        if name != name.strip():
            raise ValueError("Entry names cannot start or end with spaces.")
        bad = [char for char in INVALID_NAME_CHARACTERS if char in name]
        if bad:
            raise ValueError(f"Entry names cannot contain {' or '.join(repr(c) for c in bad)}.")
        return name

    def validate_name(self, name: Any, *, allow_existing: str | None = None) -> str:
        """Check a new entry name; return it or raise ValueError/TypeError with a message."""
        self.check_name_format(name)
        with self._lock:
            if name in self._types and name != allow_existing:
                raise ValueError(f"An entry named {name!r} already exists.")
        return name

    # ------------------------------------------------------------------ changes
    def set_runtime_active(self, active: bool) -> None:
        """Mark changes made from now on as run-time changes (set while the tree executes)."""
        self._runtime_active = bool(active)

    def _is_runtime(self) -> bool:
        return self._runtime_active or threading.get_ident() != self._gui_thread

    def _check_writable(self, action: str) -> None:
        check_not_cancelled(action)
        if self._disposed:
            raise RuntimeError("the blackboard is no longer available (BehaviorTreeWidget.Shutdown was called)")

    def _notify(self, runtime: bool) -> None:
        self.changed.emit(runtime)

    def add(self, name: str, type_name: str, value: Any = None) -> None:
        """Create entry ``name`` of ``type_name`` (default value if ``value`` is None)."""
        type_name = canonical_type(type_name)
        if value is None and type_name != "Object":
            value = ENTRY_TYPES[type_name].default
        value = coerce_value(type_name, value)
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("AddEntry")
            self.validate_name(name)
            self._client.register_key(key=name, access=py_trees.common.Access.WRITE)
            self._client.set(self.key(name), value, overwrite=True)
            self._types[name] = type_name
        self.entryAdded.emit(name)
        self._notify(runtime)

    def set(self, name: str, value: Any) -> None:
        """Set entry ``name``, creating it (type inferred from ``value``) if needed.

        A new list/dict/set whose items cannot be copied is stored as an Object entry.
        """
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("SetEntry")
            type_name = self._types.get(name)
            if type_name is None:
                inferred = infer_type(value)
                try:
                    self.add(name, inferred, value)
                except TypeError:
                    if not ENTRY_TYPES[inferred].container:
                        raise
                    self.add(name, "Object", value)
                return
            value = coerce_value(type_name, value)
            self._client.set(self.key(name), value, overwrite=True)
        self.entryChanged.emit(name)
        self._notify(runtime)

    def replace(self, name: str, type_name: str, value: Any) -> None:
        """Set entry ``name`` to ``value`` with a (possibly different) type, creating it if needed."""
        type_name = canonical_type(type_name)
        value = coerce_value(type_name, value)
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("SetEntry")
            old_type = self._types.get(name)
            if old_type is None:
                self.add(name, type_name, value)
                return
            self._client.set(self.key(name), value, overwrite=True)
            self._types[name] = type_name
        if old_type == type_name:
            self.entryChanged.emit(name)
        else:
            self.entryRetyped.emit(name)
        self._notify(runtime)

    def remove(self, name: str) -> None:
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("RemoveEntry")
            self.type_of(name)
            del self._types[name]
            self._client.unregister_key(key=name, clear=True)
        self.entryRemoved.emit(name)
        self._notify(runtime)

    def rename(self, old: str, new: str) -> None:
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("Renaming an entry")
            self.type_of(old)
            if new == old:
                return
            self.validate_name(new)
            value = self._client.get(self.key(old))
            self._client.unregister_key(key=old, clear=True)
            self._client.register_key(key=new, access=py_trees.common.Access.WRITE)
            self._client.set(self.key(new), value, overwrite=True)
            # keep the display position of the renamed entry
            self._types = {(new if key == old else key): kind for key, kind in self._types.items()}
        self.entryRenamed.emit(old, new)
        self._notify(runtime)

    def clear(self) -> None:
        for name in self.names():
            self.remove(name)

    def reorder(self, names: list[str]) -> None:
        """Put the entries in the order of ``names`` (others follow in their current order)."""
        with self._lock:
            ordered = [name for name in names if name in self._types]
            ordered += [name for name in self._types if name not in ordered]
            changed = ordered != list(self._types)
            if changed:
                self._types = {name: self._types[name] for name in ordered}
        if changed:
            self.entriesReordered.emit()

    # ------------------------------------------------------------------ snapshots / persistence
    def snapshot(self) -> list[tuple[str, str, Any]]:
        """Copy of all entries (for restoring later with :meth:`restore`).

        Values are deep copied, except Object entries, which are run-time objects
        (e.g. device handles) and are kept by reference.
        """
        with self._lock:
            result = []
            for name, type_name in self._types.items():
                value = self._client.get(self.key(name))
                if type_name != "Object":
                    try:
                        value = copy.deepcopy(value)
                    except Exception:  # noqa: BLE001 - uncopyable objects are kept by reference
                        pass
                result.append((name, type_name, value))
            return result

    def restore(self, snapshot: list[tuple[str, str, Any]]) -> None:
        """Make the blackboard contain exactly the entries of ``snapshot`` (in its order)."""
        with self._lock:
            if self._disposed:
                return
            wanted = [name for name, _, _ in snapshot]
            for name in self.names():
                if name not in wanted:
                    self.remove(name)
            for name, type_name, value in snapshot:
                if type_name != "Object":
                    try:
                        value = copy.deepcopy(value)
                    except Exception:  # noqa: BLE001
                        pass
                self.replace(name, type_name, value)
            self.reorder(wanted)

    def to_list(self, problems: list[str] | None = None, quiet: bool = False) -> list[dict]:
        """Entries as JSON-compatible dicts.

        Object entries (run-time values set from code) are never saved. Other values
        that cannot be written as JSON are skipped; a message is logged and appended to
        ``problems`` when given.
        """
        result = []
        for name, type_name, value in self.items():
            if type_name == "Object":
                log.debug("blackboard entry %r is an Object entry (a run-time value) and is not saved", name)
                continue
            try:
                encoded = encode_value(value)
            except TypeError as error:
                message = f"blackboard entry {name!r} was not saved: {error}"
                if not quiet:
                    log.warning(message)
                if problems is not None:
                    problems.append(message)
                continue
            result.append({"name": name, "type": type_name, "value": encoded})
        return result

    @staticmethod
    def parse_list(items: Any) -> tuple[list[tuple[str, str, Any]], list[str]]:
        """Validate :meth:`to_list` data; returns ``(entries, problems)`` without changing anything."""
        entries: list[tuple[str, str, Any]] = []
        problems: list[str] = []
        if not isinstance(items, list):
            return entries, ["the blackboard section is not a list"]
        seen: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"blackboard item {index} is not an object")
                continue
            name = item.get("name")
            try:
                if not isinstance(name, str):
                    raise TypeError("the entry name is missing")
                BlackboardStore.check_name_format(name)
                if name in seen:
                    raise ValueError("duplicate entry name")
                type_name = canonical_type(item.get("type"))
                if type_name == "Object":
                    raise ValueError("Object entries hold run-time values and are not loaded from files")
                value = coerce_value(type_name, decode_value(item.get("value")))
            except (TypeError, ValueError, KeyError) as error:
                problems.append(f"blackboard entry {name!r}: {error}")
                continue
            seen.add(name)
            entries.append((name, type_name, value))
        return entries, problems

    def load_list(self, items: Any, replace: bool = False) -> list[str]:
        """Add or overwrite entries from :meth:`to_list` data. Returns problems found.

        With ``replace=True`` every entry that is not in ``items`` is removed first,
        except Object entries (run-time objects set from code, which are never saved).
        """
        entries, problems = self.parse_list(items)
        if replace:
            wanted = {name for name, _, _ in entries}
            for name in self.names():
                if name not in wanted and self.type_of(name) != "Object":
                    self.remove(name)
        for name, type_name, value in entries:
            try:
                if self.has(name):
                    self.replace(name, type_name, value)
                else:
                    self.add(name, type_name, value)
            except (TypeError, ValueError, KeyError) as error:
                problems.append(f"blackboard entry {name!r}: {error}")
        return problems

    def dispose(self, notify: bool = True) -> None:
        """Remove this store's keys and client from the global py_trees blackboard.

        Afterwards the store refuses changes (RuntimeError). With ``notify`` the
        ``entryRemoved`` signal is emitted for every entry.
        """
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
            names = list(self._types)
            self._types.clear()
            try:
                self._client.unregister(clear=True)
            except Exception:  # noqa: BLE001 - e.g. Blackboard.clear() was called globally
                log.debug("could not unregister blackboard client", exc_info=True)
        if notify and shiboken6.isValid(self):
            for name in names:
                self.entryRemoved.emit(name)


# ====================================================================== widgets
class _ValueDialogBase(QDialog):
    def __init__(self, parent: QWidget | None):
        super().__init__(parent)
        self.error_label = QLabel(self)
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setStyleSheet("QLabel { color: #c62828; }")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self._try_accept)
        self.buttons.rejected.connect(self.reject)

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _try_accept(self) -> None:
        try:
            self._validate()
        except (TypeError, ValueError) as error:
            self.show_error(str(error))
            return
        self.accept()

    def _validate(self) -> None:
        raise NotImplementedError


def create_value_editor(type_name: str, parent: QWidget | None = None) -> QWidget:
    """Editor used in the Add Entry dialog for a value of ``type_name``."""
    if type_name == "Integer":
        editor = QSpinBox(parent)
        editor.setRange(INT_MIN, INT_MAX)
    elif type_name == "Double":
        editor = QDoubleSpinBox(parent)
        editor.setDecimals(FLOAT_DECIMALS)
        editor.setRange(-sys.float_info.max, sys.float_info.max)
    elif type_name == "Bool":
        editor = QCheckBox(parent)
    else:
        editor = QLineEdit(parent)
        if ENTRY_TYPES[type_name].container:
            editor.setPlaceholderText(ENTRY_TYPES[type_name].example)
    editor.setObjectName("EntryValue")
    return editor


def editor_value(type_name: str, editor: QWidget) -> Any:
    if isinstance(editor, QSpinBox):
        return editor.value()
    if isinstance(editor, QDoubleSpinBox):
        return editor.value()
    if isinstance(editor, QCheckBox):
        return editor.isChecked()
    text = editor.text()
    if ENTRY_TYPES[type_name].container:
        return parse_container_text(type_name, text)
    return text


class AddEntryDialog(_ValueDialogBase):
    """Popup asking for the name and initial value of a new blackboard entry."""

    def __init__(self, type_name: str, store: BlackboardStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.type_name = canonical_type(type_name)
        self._store = store
        self._value: Any = None
        self.setWindowTitle(f"Add {self.type_name} Entry")
        self.setObjectName("AddEntryDialog")
        self.name_edit = QLineEdit(self)
        self.name_edit.setObjectName("EntryName")
        self.name_edit.setPlaceholderText("Entry Name")
        self.value_editor = create_value_editor(self.type_name, self)
        form = QFormLayout()
        form.addRow("Name:", self.name_edit)
        form.addRow("Value:", self.value_editor)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        if ENTRY_TYPES[self.type_name].container:
            hint = QLabel(f"Python literal, e.g. {ENTRY_TYPES[self.type_name].example}", self)
            hint.setEnabled(False)
            layout.addWidget(hint)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)
        self.name_edit.setFocus()

    def _validate(self) -> None:
        name = self._store.validate_name(self.name_edit.text().strip())
        value = coerce_value(self.type_name, editor_value(self.type_name, self.value_editor))
        self._entry = (name, value)

    def entry(self) -> tuple[str, Any]:
        """``(name, value)`` entered by the user (after the dialog was accepted)."""
        return self._entry


class EditContainerDialog(_ValueDialogBase):
    """Popup editing the value of a List / Dictionary / Set entry as a Python literal."""

    def __init__(self, name: str, type_name: str, value: Any, parent: QWidget | None = None):
        super().__init__(parent)
        self.type_name = type_name
        self.setWindowTitle(f"Edit {name}")
        self.setObjectName("EditContainerDialog")
        self.text_edit = QLineEdit(self)
        self.text_edit.setObjectName("ContainerText")
        self.text_edit.setText(literal_text(value))
        self.text_edit.setCursorPosition(0)
        self.text_edit.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow(f"{type_name}:", self.text_edit)
        layout.addLayout(form)
        hint = QLabel(f"Python literal, e.g. {ENTRY_TYPES[type_name].example}", self)
        hint.setEnabled(False)
        layout.addWidget(hint)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

    def _validate(self) -> None:
        self._value = parse_container_text(self.type_name, self.text_edit.text())

    def value(self) -> Any:
        return self._value


class EntryRow(QWidget):
    """One blackboard entry: Select checkbox, name QLineEdit and value editor."""

    selectionToggled = Signal()

    def __init__(self, view: "BlackboardView", name: str, type_name: str):
        super().__init__()
        self._view = view
        self._store = view.store()
        self._retired = False
        self.name = name
        self.type_name = type_name
        spec = ENTRY_TYPES[type_name]
        self.setObjectName(f"Entry_{name}")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._ui = load_ui(spec.ui_file, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._ui)

        self.select_box: QCheckBox = self._ui.findChild(QCheckBox, "Select")
        self.name_edit: QLineEdit = self._ui.findChild(QLineEdit, "ValueName")
        self.value_widget: QWidget = self._ui.findChild(QWidget, spec.value_widget) or self._ui.findChild(
            QWidget, "Value"
        )
        if self.select_box is None or self.name_edit is None or self.value_widget is None:
            raise RuntimeError(f"{spec.ui_file} must contain Select, ValueName and {spec.value_widget} widgets")
        self.edit_button: QToolButton | None = None

        self.name_edit.setText(name)
        self.name_edit.setToolTip(f"{type_name} entry - edit to rename")
        self.name_edit.editingFinished.connect(self._on_name_edited)
        self.select_box.toggled.connect(self.selectionToggled)

        widget = self.value_widget
        if type_name == "Double" and isinstance(widget, QDoubleSpinBox) and not isinstance(widget, FloatSpinBox):
            # Show any float exactly (and nan/inf) instead of 8 fixed decimals.
            replacement = FloatSpinBox(widget.parentWidget())
            replacement.setObjectName(widget.objectName())
            replacement.setSizePolicy(widget.sizePolicy())
            replacement.setMinimumSize(widget.minimumSize())
            layout = widget.parentWidget().layout()
            layout.replaceWidget(widget, replacement)
            widget.deleteLater()
            widget = self.value_widget = replacement
        if type_name == "Object":
            widget.setReadOnly(True)
            widget.setToolTip("Set from code; not editable here")
        elif isinstance(widget, QAbstractSpinBox):
            widget.setKeyboardTracking(True)
            widget.valueChanged.connect(self._on_value_edited)
        elif isinstance(widget, QCheckBox):
            widget.toggled.connect(self._on_value_edited)
        elif isinstance(widget, QLineEdit):
            widget.textChanged.connect(self._on_value_edited)
        elif isinstance(widget, QComboBox):
            widget.setPlaceholderText("(empty)")
            self.edit_button = QToolButton(self._ui)
            self.edit_button.setObjectName("EditValue")
            self.edit_button.setText("Edit...")
            self.edit_button.setToolTip(f"Edit the {type_name.lower()} as a Python literal")
            self.edit_button.clicked.connect(self._on_edit_clicked)
            frame = self._ui.findChild(QFrame, "FrameValue")
            container = frame.layout() if frame is not None and frame.layout() is not None else self._ui.layout()
            container.addWidget(self.edit_button)
        self.refresh_value()

    # ------------------------------------------------------------------ state
    def is_selected(self) -> bool:
        return self.select_box.isChecked()

    def set_selected(self, selected: bool) -> None:
        self.select_box.blockSignals(True)
        self.select_box.setChecked(selected)
        self.select_box.blockSignals(False)

    def set_name(self, name: str) -> None:
        self.name = name
        self.setObjectName(f"Entry_{name}")
        if self.name_edit.text() != name:
            self.name_edit.setText(name)

    def retire(self) -> None:
        """Detach the row from the view (it is about to be deleted)."""
        self._retired = True
        for widget in (self.name_edit, self.value_widget, self.select_box):
            if shiboken6.isValid(widget):
                widget.blockSignals(True)

    def _is_current(self) -> bool:
        return (
            not self._retired
            and shiboken6.isValid(self)
            and shiboken6.isValid(self.name_edit)
            and shiboken6.isValid(self.value_widget)
            and shiboken6.isValid(self._view)
            and self._view.row(self.name) is self
        )

    def refresh_value(self) -> None:
        """Show the store's current value in the editor (without echoing it back)."""
        try:
            value = self._store.get(self.name)
        except KeyError:
            return
        widget = self.value_widget
        widget.blockSignals(True)
        try:
            if self.type_name == "Object":
                text = safe_repr(value)
                widget.setText(text)
                widget.setCursorPosition(0)
                widget.setToolTip(text)
            elif isinstance(widget, FloatSpinBox):
                # Do not reformat the text while the user is typing the same value.
                if not _same_value(widget.float_value(), value):
                    widget.set_float(value)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                if widget.value() != value:
                    widget.setValue(value)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(value)
                widget.setText("True" if value else "False")
            elif isinstance(widget, QLineEdit):
                if widget.text() != value:
                    widget.setText(value)
                    if not widget.hasFocus():
                        widget.setCursorPosition(0)  # show the start of long text
            elif isinstance(widget, QComboBox):
                items = container_items(self.type_name, value)
                current = widget.currentIndex()
                if [widget.itemText(i) for i in range(widget.count())] != items:
                    widget.clear()
                    widget.addItems(items)
                if items:
                    widget.setCurrentIndex(min(max(current, 0), len(items) - 1))
                widget.setToolTip(safe_repr(value))
        finally:
            widget.blockSignals(False)

    # ------------------------------------------------------------------ user edits
    def _on_value_edited(self, value: Any) -> None:
        if not self._is_current():
            return
        if isinstance(self.value_widget, FloatSpinBox):
            value = self.value_widget.float_value()
        try:
            unchanged = _same_value(self._store.get(self.name), value)
        except KeyError:
            unchanged = False
        if unchanged:
            return  # e.g. Enter pressed: nothing to store, the tree is not modified
        try:
            self._store.set(self.name, value)
        except (TypeError, ValueError, KeyError, RuntimeError) as error:
            log.warning("could not set blackboard entry %r: %s", self.name, error)
            self.refresh_value()
            return
        if isinstance(self.value_widget, QCheckBox):
            self.value_widget.setText("True" if value else "False")
        self._view.entryEdited.emit(self.name)

    def _on_name_edited(self) -> None:
        if not self._is_current():
            return
        new = self.name_edit.text().strip()
        if new == self.name:
            self.name_edit.setText(self.name)
            return
        old = self.name
        try:
            self._store.rename(old, new)
        except (TypeError, ValueError, KeyError, RuntimeError) as error:
            self.name_edit.setText(self.name)
            QToolTip.showText(
                self.name_edit.mapToGlobal(QPoint(0, self.name_edit.height())), str(error), self.name_edit
            )
            return
        self._view.entryEdited.emit(new)

    def _on_edit_clicked(self) -> None:
        if not self._is_current():
            return
        name, view = self.name, self._view
        try:
            value = self._store.get(name)
        except KeyError:
            return
        # Parented to the view: the row may be replaced while the dialog is open.
        dialog = EditContainerDialog(name, self.type_name, value, view)
        try:
            if view._run_dialog(dialog) and shiboken6.isValid(dialog):
                view.store().set(name, dialog.value())
                view.entryEdited.emit(name)
        except (TypeError, ValueError, KeyError, RuntimeError) as error:
            log.warning("could not set blackboard entry %r: %s", name, error)
        finally:
            if shiboken6.isValid(dialog):
                dialog.deleteLater()


class BlackboardView(QWidget):
    """The Blackboard tab (BlackBoardView.ui): lists, adds and removes entries."""

    entryEdited = Signal(str)  # the user added, removed, renamed or edited an entry in this view

    def __init__(self, store: BlackboardStore, parent: QWidget | None = None):
        super().__init__(parent)
        self._store = store
        self._rows: dict[str, EntryRow] = {}
        self._ui = load_ui("BlackBoardView.ui", self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._ui)

        self.select_all: QCheckBox = self._ui.findChild(QCheckBox, "SelectAll")
        self.entry_list: QScrollArea = self._ui.findChild(QScrollArea, "EntryList")
        self.entry_type: QComboBox = self._ui.findChild(QComboBox, "EntryType")
        self.add_button: QPushButton = self._ui.findChild(QPushButton, "AddEntry")
        self.remove_button: QPushButton = self._ui.findChild(QPushButton, "RemoveEntries")

        contents = self.entry_list.widget()
        self._rows_layout = QVBoxLayout(contents)
        self._rows_layout.setContentsMargins(2, 2, 2, 2)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch(1)

        self.entry_type.clear()
        self.entry_type.addItems([name for name, spec in ENTRY_TYPES.items() if spec.user_creatable])

        self.select_all.clicked.connect(self._on_select_all)
        self.add_button.clicked.connect(self.add_entry_interactively)
        self.remove_button.clicked.connect(self.remove_selected)
        self.add_button.setToolTip("Add an entry of the selected type")
        self.remove_button.setToolTip("Remove the selected entries")

        store.entryAdded.connect(self._on_entry_added)
        store.entryRemoved.connect(self._on_entry_removed)
        store.entryChanged.connect(self._on_entry_changed)
        store.entryRenamed.connect(self._on_entry_renamed)
        store.entryRetyped.connect(self._on_entry_retyped)
        store.entriesReordered.connect(self._on_entries_reordered)
        for name in store.names():
            self._sync_row(name)

    # ------------------------------------------------------------------ access
    def store(self) -> BlackboardStore:
        return self._store

    def row(self, name: str) -> EntryRow | None:
        return self._rows.get(name)

    def rows(self) -> list[EntryRow]:
        return [self._rows[name] for name in self._store.names() if name in self._rows]

    # ------------------------------------------------------------------ actions
    def _run_dialog(self, dialog: QDialog) -> bool:
        """Show a modal dialog (separate method so tests can drive dialogs)."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    def add_entry_interactively(self) -> None:
        """AddEntry button: ask for the name and value of an entry of the selected type."""
        dialog = AddEntryDialog(self.entry_type.currentText(), self._store, self)
        try:
            if self._run_dialog(dialog):
                name, value = dialog.entry()
                self._store.add(name, dialog.type_name, value)
                self.entryEdited.emit(name)
        except (TypeError, ValueError, RuntimeError) as error:
            log.warning("could not add blackboard entry: %s", error)
        finally:
            if shiboken6.isValid(dialog):
                dialog.deleteLater()

    def remove_selected(self) -> None:
        """RemoveEntries button: remove every selected entry."""
        for name in [row.name for row in self.rows() if row.is_selected()]:
            if self._store.has(name):
                try:
                    self._store.remove(name)
                except (KeyError, RuntimeError) as error:
                    log.warning("could not remove blackboard entry %r: %s", name, error)
                    continue
                self.entryEdited.emit(name)
        self._update_select_all()

    def _on_select_all(self, checked: bool) -> None:
        for row in self._rows.values():
            row.set_selected(checked)
        self._update_select_all()

    def _update_select_all(self) -> None:
        rows = list(self._rows.values())
        all_selected = bool(rows) and all(row.is_selected() for row in rows)
        self.select_all.blockSignals(True)
        self.select_all.setChecked(all_selected)
        self.select_all.blockSignals(False)

    # ------------------------------------------------------------------ store signals
    def _on_entry_added(self, name: str) -> None:
        self._sync_row(name)

    def _on_entry_removed(self, name: str) -> None:
        self._sync_row(name)

    def _on_entry_changed(self, name: str) -> None:
        self._sync_row(name)

    def _on_entry_retyped(self, name: str) -> None:
        self._sync_row(name)

    def _on_entry_renamed(self, old: str, new: str) -> None:
        row = self._rows.get(old)
        if row is not None and new not in self._rows and self._store.has(new) and not self._store.has(old):
            del self._rows[old]
            self._rows[new] = row
            row.set_name(new)
        self._sync_row(old)
        self._sync_row(new)
        self._on_entries_reordered()  # queued notifications may have inserted rows out of order

    def _on_entries_reordered(self) -> None:
        rows = self.rows()
        current = [self._rows_layout.itemAt(i).widget() for i in range(self._rows_layout.count())]
        if [widget for widget in current if widget is not None] == rows:
            return
        for index, row in enumerate(rows):
            self._rows_layout.removeWidget(row)
            self._rows_layout.insertWidget(index, row)

    def _retire_row(self, name: str) -> EntryRow | None:
        row = self._rows.pop(name, None)
        if row is None or not shiboken6.isValid(row):
            return None
        row.retire()  # block its signals: hiding a focused editor emits editingFinished
        self._rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        return row

    def _sync_row(self, name: str) -> None:
        """Make the row of ``name`` match the store (create, update, retype or remove it)."""
        try:
            type_name = self._store.type_of(name)
        except KeyError:
            type_name = None
        row = self._rows.get(name)
        if type_name is None:
            if row is not None:
                self._retire_row(name)
                self._update_select_all()
            return
        selected = False
        if row is not None and row.type_name != type_name:
            selected = row.is_selected()
            self._retire_row(name)
            row = None
        if row is None:
            row = EntryRow(self, name, type_name)
            row.set_selected(selected)
            row.selectionToggled.connect(self._update_select_all)
            self._rows[name] = row
            names = [n for n in self._store.names() if n in self._rows]
            self._rows_layout.insertWidget(names.index(name) if name in names else len(names), row)
            self._update_select_all()
            self._on_entries_reordered()
        else:
            row.refresh_value()
