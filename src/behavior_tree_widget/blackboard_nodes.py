"""Built-in leaf nodes working on blackboard entries: Evaluation and Set.

Both nodes show three editors, named as in their ``.ui`` files:

* ``ValueName`` - a combo box listing every blackboard entry: the entry the node works on.
* ``CompareTo`` - a combo box offering "Literal" and the other entries of the same type.
* ``LiteralValue`` - shown and enabled while "Literal" is chosen. Its editor matches the
  type of the ``ValueName`` entry: a spin box for Integer, a double spin box for Double, a
  line edit for String, a check box for Bool, and a line edit taking a Python literal
  (such as ``[1, 2]``) for List, Dictionary and Set entries.

The choices are the node's fields and are saved with the tree: ``ValueName`` (``""``
until an entry is chosen), ``CompareTo`` (an entry name, or ``None`` for the literal) and
``LiteralValue``. The literal is used with the type of the ``ValueName`` entry. The nodes
keep their choices when entries disappear (they show them as missing, in red) and
follow entries that are renamed.

* :class:`EvaluationNodeWidget` succeeds when the ``ValueName`` entry equals the
  ``CompareTo`` entry or the literal, and fails otherwise.
* :class:`SetNodeWidget` sets the ``ValueName`` entry to the value of the ``CompareTo``
  entry or to the literal, and succeeds.

Both fail with an error (shown in the Status label's tooltip) when no entry is chosen or
an entry they need does not exist.
"""

from __future__ import annotations

import copy
import logging
import math
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit, QSizePolicy, QSpinBox, QWidget

from .blackboard import (
    ENTRY_TYPES,
    INT_MAX,
    INT_MIN,
    FloatSpinBox,
    _same_value,
    coerce_value,
    infer_type,
    literal_text,
    parse_container_text,
)
from .nodes import LeafNodeWidget, _guard_wheel

if TYPE_CHECKING:  # pragma: no cover
    from .widget import BehaviorTreeWidget

log = logging.getLogger("behavior_tree_widget")

__all__ = [
    "BlackboardValueNodeWidget",
    "EvaluationNodeWidget",
    "SetNodeWidget",
    "LITERAL",
    "convert_literal",
    "values_equal",
]

VALUE_NAME = "ValueName"
COMPARE_TO = "CompareTo"
LITERAL_VALUE = "LiteralValue"
LITERAL = "Literal"  # text of the CompareTo choice that uses the LiteralValue
SCALAR_TYPES = ("Integer", "Double", "String", "Bool")
CONTAINER_TYPES = ("List", "Dictionary", "Set")
LITERAL_TYPES = SCALAR_TYPES + CONTAINER_TYPES
PROBLEM_COLOR = QColor("#c62828")


def literal_type(value: Any) -> str | None:
    """Blackboard entry type of a literal value (None for values that cannot be literals)."""
    type_name = infer_type(value)
    return type_name if type_name in LITERAL_TYPES else None


def _parse_number(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


def convert_literal(value: Any, type_name: str) -> Any:
    """``value`` as a literal for an entry of ``type_name``.

    Numbers, booleans and number text are converted between Integer, Double and Bool,
    anything becomes text for String, and a value that cannot be converted gives the
    type's default (0, 0.0, "", False or an empty container). Object (and unknown)
    types keep the value as it is.
    """
    if type_name == "Integer":
        if isinstance(value, int):  # also bool
            return min(max(int(value), INT_MIN), INT_MAX)
        number = value if isinstance(value, float) else _parse_number(value)
        if number is None or not math.isfinite(number):
            return 0
        return min(max(round(number), INT_MIN), INT_MAX)
    if type_name == "Double":
        if isinstance(value, (int, float)):  # also bool
            try:
                return float(value)
            except OverflowError:  # an int too large for a float
                return 0.0
        number = _parse_number(value)
        return 0.0 if number is None else number
    if type_name == "String":
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return str(value) if isinstance(value, (bool, int, float)) else literal_text(value)
    if type_name == "Bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return False
    if type_name in CONTAINER_TYPES:
        try:
            return coerce_value(type_name, value)
        except (TypeError, ValueError):
            pass
        if isinstance(value, str):
            try:
                return parse_container_text(type_name, value)
            except ValueError:
                pass
        return coerce_value(type_name, ENTRY_TYPES[type_name].default)
    return value


def values_equal(a: Any, b: Any) -> bool:
    """``a == b`` for blackboard values, except that a bool only equals a bool."""
    if isinstance(a, bool) is not isinstance(b, bool):
        return False
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001 - e.g. an Object entry with a failing __eq__
        return False


def _same(a: Any, b: Any) -> bool:
    """Whether storing ``b`` over ``a`` changes nothing (same type and value)."""
    if isinstance(a, float) and isinstance(b, float):
        return _same_value(a, b)
    return type(a) is type(b) and values_equal(a, b)


def _show_problem(widget: QWidget, problem: bool) -> None:
    """Draw the text of ``widget`` in red (``problem``) or in the inherited colours."""
    if not problem:
        widget.setPalette(QPalette())  # back to the inherited palette
        return
    palette = QPalette(widget.palette())
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(role, PROBLEM_COLOR)
    widget.setPalette(palette)


class BlackboardValueNodeWidget(LeafNodeWidget):
    """Base class of the Evaluation and Set nodes: the ValueName, CompareTo and LiteralValue editors.

    Subclasses set ``UI_FILE`` (a file with ``ValueName`` and ``CompareTo`` combo boxes
    and a ``LiteralValue`` placeholder widget) and implement ``OnRun`` with
    :meth:`_operands`.
    """

    RUN_IN_THREAD = False
    _fields = {VALUE_NAME: "", COMPARE_TO: None, LITERAL_VALUE: None}

    def __init__(self, parent: QWidget | None = None):
        self._store = None  # BlackboardStore of the tree the node was added to
        super().__init__(parent)
        self._value_box: QComboBox = self._require_child(QComboBox, VALUE_NAME)
        self._compare_box: QComboBox = self._require_child(QComboBox, COMPARE_TO)
        self._literal_editor: QWidget = self._require_child(QWidget, LITERAL_VALUE)
        self._literal_label: QLabel | None = self._find_child(QLabel, "LiteralValueLabel")
        self._literal_type: str | None = "placeholder"  # entry type the literal editor was made for
        self._box_tooltips = {box: box.toolTip() for box in (self._value_box, self._compare_box)}
        for box in (self._value_box, self._compare_box):
            # A width that does not depend on the entry names, so choosing an entry never
            # resizes the node (and never changes the order of its siblings).
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(12)
            box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            _guard_wheel(box)
        self._value_box.setPlaceholderText("Select a value")
        self._value_box.currentIndexChanged.connect(self._on_value_name_chosen)
        self._compare_box.currentIndexChanged.connect(self._on_compare_to_chosen)
        self._refresh_editors()

    # ------------------------------------------------------------------ user API
    def GetValueName(self) -> str:
        """Name of the blackboard entry the node works on (``""`` when none is chosen)."""
        with self._lock:
            name = self._fields.get(VALUE_NAME)
        return name if isinstance(name, str) else ""

    def SetValueName(self, name: str) -> None:
        """Choose the blackboard entry the node works on (thread-safe)."""
        if not isinstance(name, str):
            raise TypeError(f"ValueName must be a str, not {type(name).__name__}")
        self.SetField(VALUE_NAME, name)

    def GetCompareTo(self) -> str | None:
        """Name of the entry chosen in CompareTo, or None when the Literal Value is used."""
        with self._lock:
            name = self._fields.get(COMPARE_TO)
        return name if isinstance(name, str) and name else None

    def SetCompareTo(self, name: str | None) -> None:
        """Use the entry ``name`` (or the Literal Value when ``name`` is None) (thread-safe)."""
        if name is not None and not isinstance(name, str):
            raise TypeError(f"CompareTo must be a str or None, not {type(name).__name__}")
        self.SetField(COMPARE_TO, name or None)

    def UsesLiteral(self) -> bool:
        """True when "Literal" is chosen in CompareTo."""
        return self.GetCompareTo() is None

    def GetLiteralValue(self) -> Any:
        """The Literal Value as stored (see :meth:`GetEffectiveLiteralValue`); containers are copies."""
        with self._lock:
            value = self._fields.get(LITERAL_VALUE)
            return copy.deepcopy(value) if isinstance(value, (list, dict, set)) else value

    def SetLiteralValue(self, value: Any) -> None:
        """Set the Literal Value: a bool, int, float, str, list, dict or set (thread-safe)."""
        if value is not None and literal_type(value) is None:
            raise TypeError(f"a Literal Value cannot be of type {type(value).__name__}")
        self.SetField(LITERAL_VALUE, value)

    def GetEffectiveLiteralValue(self) -> Any:
        """The Literal Value converted to the type of the ValueName entry, as it is used and shown."""
        type_name = self._entry_types().get(self.GetValueName())
        literal = self.GetLiteralValue()
        return convert_literal(literal, type_name) if type_name in LITERAL_TYPES else literal

    def literal_editor(self) -> QWidget:
        """The widget currently named ``LiteralValue`` (its class follows the entry type)."""
        return self._literal_editor

    # ------------------------------------------------------------------ execution helpers
    def _operands(self, tree: BehaviorTreeWidget | None) -> tuple[str, Any]:
        """``(entry name, value of the CompareTo entry or the converted literal)``.

        Raises:
            ValueError: no entry is chosen, a chosen entry does not exist, or a literal
                cannot be used with the entry's type.
        """
        with self._lock:
            name = self._fields.get(VALUE_NAME)
            compare = self._fields.get(COMPARE_TO)
            literal = self._fields.get(LITERAL_VALUE)
        if not isinstance(name, str) or not name:
            raise ValueError("no blackboard value is selected (Value Name)")
        if tree is None:
            raise RuntimeError("the node is not part of a BehaviorTreeWidget")
        try:
            type_name = tree.GetEntryType(name)
        except KeyError:
            raise ValueError(f"the blackboard has no entry named {name!r}") from None
        if isinstance(compare, str) and compare:
            try:
                return name, tree.GetEntry(compare)
            except KeyError:
                raise ValueError(f"the blackboard has no entry named {compare!r} (Compare To)") from None
        if type_name not in LITERAL_TYPES:
            raise ValueError(f"{type_name} entries cannot be used with a Literal Value; choose an entry in Compare To")
        return name, convert_literal(literal, type_name)

    # ------------------------------------------------------------------ blackboard
    def _on_added_to_tree(self) -> None:
        tree = self._tree
        store = tree.blackboardStore() if tree is not None else None
        if store is not None and store is not self._store:
            self._store = store
            # Structure changes only; queued when they come from worker threads.
            store.entryAdded.connect(self._on_entries_changed)
            store.entryRemoved.connect(self._on_entries_changed)
            store.entryRetyped.connect(self._on_entries_changed)
            store.entriesReordered.connect(self._on_entries_changed)
            store.entryRenamed.connect(self._on_entry_renamed)
        self._refresh_editors()

    def _entry_types(self) -> dict[str, str]:
        store = self._store
        if store is None or not shiboken6.isValid(store):
            return {}
        return store.types()

    def _on_entries_changed(self, *_args) -> None:
        self._refresh_editors()

    def _on_entry_renamed(self, old: str, new: str) -> None:
        """Follow a renamed entry."""
        renamed = []
        with self._lock:
            for key in (VALUE_NAME, COMPARE_TO):
                if self._fields.get(key) == old:
                    self._fields[key] = new
                    renamed.append(key)
        if renamed:
            runtime = self._is_runtime_change()
            for key in renamed:
                self.fieldChanged.emit(key)
            self.changed.emit(runtime)
        self._refresh_editors()

    # ------------------------------------------------------------------ editors
    def _refresh_all_fields(self) -> None:
        self._refresh_editors()

    def _refresh_field_editor(self, key: str) -> None:
        self._refresh_editors()

    def _refresh_editors(self) -> None:
        """Show the node's choices and the blackboard's current entries in the editors."""
        if "_value_box" not in self.__dict__ or not shiboken6.isValid(self._value_box):
            return
        types = self._entry_types()
        with self._lock:
            name = self._fields.get(VALUE_NAME)
            compare = self._fields.get(COMPARE_TO)
            literal = self._fields.get(LITERAL_VALUE)
        name = name if isinstance(name, str) else ""
        compare = compare if isinstance(compare, str) and compare else None
        value_type = types.get(name)

        items = [(entry, entry, f"{kind} entry", False) for entry, kind in types.items()]
        if name and value_type is None:
            items.append((f"{name} (missing)", name, f"The blackboard has no entry named {name!r}", True))
        self._fill(self._value_box, items, name or None)

        items = [(LITERAL, None, "Use the Literal Value", False)]
        if value_type is not None:
            items += [
                (entry, entry, f"{kind} entry", False)
                for entry, kind in types.items()
                if kind == value_type and entry != name
            ]
        if compare is not None and all(data != compare for _, data, _, _ in items):
            # Set from code (or a file): shown, and marked, although the list would not offer it.
            if compare not in types:
                items.append((f"{compare} (missing)", compare, f"The blackboard has no entry named {compare!r}", True))
            elif compare == name:
                items.append((f"{compare} (itself)", compare, "Compare To names the Value Name entry itself", True))
            else:
                kind = types[compare]
                tooltip = f"{compare!r} is an entry of type {kind}, not {value_type or 'the Value Name type'}"
                items.append((f"{compare} ({kind})", compare, tooltip, True))
        self._fill(self._compare_box, items, compare)

        kind = value_type if value_type is not None else literal_type(literal)
        if kind != self._literal_type:
            self._replace_literal_editor(kind)
        self._show_literal(literal, kind)
        use_literal = compare is None
        self._literal_editor.setVisible(use_literal)
        self._literal_editor.setEnabled(use_literal and kind in LITERAL_TYPES)
        if self._literal_label is not None:
            self._literal_label.setVisible(use_literal)
        self._notify_geometry()

    def _fill(self, box: QComboBox, items: list[tuple[str, Any, str, bool]], current: Any) -> None:
        """Show ``items`` (text, data, tooltip, problem) in ``box`` and select the one whose data is ``current``."""
        blocked = box.blockSignals(True)
        try:
            shown = [(box.itemText(index), box.itemData(index)) for index in range(box.count())]
            if shown != [(text, data) for text, data, _, _ in items]:
                box.clear()
                for index, (text, data, tooltip, problem) in enumerate(items):
                    box.addItem(text, data)
                    box.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)
                    if problem:
                        box.setItemData(index, QBrush(PROBLEM_COLOR), Qt.ItemDataRole.ForegroundRole)
            index = next((i for i, item in enumerate(items) if item[1] is not None and item[1] == current), -1)
            if current is None and items and items[0][1] is None:
                index = 0  # the Literal choice
            box.setCurrentIndex(index)
        finally:
            box.blockSignals(blocked)
        problem = index >= 0 and items[index][3]
        _show_problem(box, problem)
        box.setToolTip(items[index][2] if problem else self._box_tooltips[box])

    def _replace_literal_editor(self, kind: str | None) -> None:
        old = self._literal_editor
        editor = self._make_literal_editor(kind)
        old.parentWidget().layout().replaceWidget(old, editor)
        old.setObjectName("")  # findChild must not find the old editor before it is deleted
        old.hide()
        old.deleteLater()
        self._literal_editor = editor
        self._literal_type = kind

    def _make_literal_editor(self, kind: str | None) -> QWidget:
        editor: QWidget
        if kind == "Integer":
            editor = QSpinBox()
            editor.setRange(INT_MIN, INT_MAX)
            editor.setKeyboardTracking(True)
            editor.valueChanged.connect(self._on_literal_edited)
            _guard_wheel(editor)
        elif kind == "Double":
            editor = FloatSpinBox()  # a QDoubleSpinBox showing any float exactly
            editor.setKeyboardTracking(True)
            editor.valueChanged.connect(self._on_literal_edited)
            _guard_wheel(editor)
        elif kind == "Bool":
            editor = QCheckBox()
            editor.toggled.connect(self._on_literal_edited)
        else:
            editor = QLineEdit()
            if kind in ("String", *CONTAINER_TYPES):
                editor.textChanged.connect(self._on_literal_edited)
                if kind != "String":
                    editor.setPlaceholderText(ENTRY_TYPES[kind].example)
                    editor.setToolTip(f"A Python literal, for example {ENTRY_TYPES[kind].example}")
            else:
                editor.setReadOnly(True)
                editor.setPlaceholderText("Select a value first" if kind is None else f"No literal for {kind}")
        editor.setObjectName(LITERAL_VALUE)
        # The combo boxes decide the width (a Double's long text must not widen the node).
        editor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        return editor

    def _show_literal(self, literal: Any, kind: str | None) -> None:
        editor = self._literal_editor
        value = convert_literal(literal, kind) if kind in LITERAL_TYPES else literal
        blocked = editor.blockSignals(True)
        try:
            if isinstance(editor, FloatSpinBox):
                if not _same_value(editor.float_value(), value):
                    editor.set_float(value)
            elif isinstance(editor, QSpinBox):
                if editor.value() != value:
                    editor.setValue(value)
            elif isinstance(editor, QCheckBox):
                editor.setChecked(bool(value))
                editor.setText("True" if value else "False")
            elif isinstance(editor, QLineEdit):
                if kind == "String":
                    text = value
                elif kind in CONTAINER_TYPES:
                    text = editor.text()
                    try:
                        typed = parse_container_text(kind, text)
                    except ValueError:
                        typed = None
                        if editor.hasFocus():
                            return  # keep a literal being typed
                    if typed is None or not values_equal(typed, value):
                        text = literal_text(value)
                    self._mark_invalid_literal(None)
                else:
                    text = ""
                if editor.text() != text:
                    editor.setText(text)
                    if not editor.hasFocus():
                        editor.setCursorPosition(0)
        finally:
            editor.blockSignals(blocked)

    def _mark_invalid_literal(self, message: str | None) -> None:
        """Show (``message``) or clear the error of a container literal being typed."""
        editor = self._literal_editor
        if not isinstance(editor, QLineEdit) or self._literal_type not in CONTAINER_TYPES:
            return
        _show_problem(editor, message is not None)
        editor.setToolTip(message or f"A Python literal, for example {ENTRY_TYPES[self._literal_type].example}")

    # ------------------------------------------------------------------ user edits
    def _on_value_name_chosen(self, index: int) -> None:
        name = self._value_box.itemData(index) if index >= 0 else None
        if not isinstance(name, str):
            return
        types = self._entry_types()
        new_type = types.get(name)
        edited = []
        with self._lock:
            if self._fields.get(VALUE_NAME) != name:
                self._fields[VALUE_NAME] = name
                edited.append(VALUE_NAME)
            compare = self._fields.get(COMPARE_TO)
            if isinstance(compare, str) and compare and (compare == name or types.get(compare) != new_type):
                self._fields[COMPARE_TO] = None  # only entries of the same type can be compared
                edited.append(COMPARE_TO)
            if new_type in LITERAL_TYPES:
                literal = self._fields.get(LITERAL_VALUE)
                converted = convert_literal(literal, new_type)
                if not _same(literal, converted):
                    self._fields[LITERAL_VALUE] = converted
                    edited.append(LITERAL_VALUE)
        self._report_edits(edited)
        self._refresh_editors()

    def _on_compare_to_chosen(self, index: int) -> None:
        if index < 0:
            return
        data = self._compare_box.itemData(index)
        compare = data if isinstance(data, str) and data else None
        with self._lock:
            if self._fields.get(COMPARE_TO) == compare:
                return
            self._fields[COMPARE_TO] = compare
        self._report_edits([COMPARE_TO])
        self._refresh_editors()

    def _on_literal_edited(self, *_args) -> None:
        editor, kind = self._literal_editor, self._literal_type
        if isinstance(editor, FloatSpinBox):
            value: Any = editor.float_value()
        elif isinstance(editor, QSpinBox):
            value = editor.value()
        elif isinstance(editor, QCheckBox):
            value = editor.isChecked()
            editor.setText("True" if value else "False")
        elif kind in CONTAINER_TYPES:
            try:
                value = parse_container_text(kind, editor.text())
            except ValueError as error:
                self._mark_invalid_literal(str(error))
                return
            self._mark_invalid_literal(None)
        else:
            value = editor.text()
        with self._lock:
            if _same(self._fields.get(LITERAL_VALUE), value):
                return
            self._fields[LITERAL_VALUE] = value
        self._report_edits([LITERAL_VALUE])

    def _report_edits(self, keys: list[str]) -> None:
        """Signal fields changed by the user in the node's editors (edits, not run-time changes)."""
        for key in keys:
            self.fieldChanged.emit(key)
            self.fieldEdited.emit(key)
        if keys:
            self.changed.emit(False)

    # ------------------------------------------------------------------ persistence
    def _to_dict(self) -> dict:
        with self._lock:
            return {"fields": dict(self._fields)}

    def _load_dict(self, data: dict) -> None:
        saved = data.get("fields")
        if isinstance(saved, dict):
            with self._lock:
                name = saved.get(VALUE_NAME)
                if isinstance(name, str):
                    self._fields[VALUE_NAME] = name
                elif name is not None:
                    log.warning("%s: ignoring saved ValueName of type %s", self, type(name).__name__)
                if COMPARE_TO in saved:
                    compare = saved[COMPARE_TO]
                    if compare is None or isinstance(compare, str):
                        self._fields[COMPARE_TO] = compare or None
                    else:
                        log.warning("%s: ignoring saved CompareTo of type %s", self, type(compare).__name__)
                if LITERAL_VALUE in saved:
                    literal = saved[LITERAL_VALUE]
                    if literal is None or literal_type(literal) is not None:
                        self._fields[LITERAL_VALUE] = literal
                    else:
                        log.warning("%s: ignoring saved LiteralValue of type %s", self, type(literal).__name__)
        self._refresh_editors()


class EvaluationNodeWidget(BlackboardValueNodeWidget):
    """Succeeds when the ValueName entry equals the CompareTo entry (or the Literal Value), else fails."""

    UI_FILE = "EvaluationNode.ui"
    TYPE_NAME = "Evaluation"
    _title = "Evaluation"

    def OnRun(self, tree):
        name, other = self._operands(tree)
        return values_equal(tree.GetEntry(name), other)


class SetNodeWidget(BlackboardValueNodeWidget):
    """Sets the ValueName entry to the CompareTo entry's value (or to the Literal Value), then succeeds."""

    UI_FILE = "SetNode.ui"
    TYPE_NAME = "Set"
    _title = "Set"

    def OnRun(self, tree):
        name, value = self._operands(tree)
        tree.SetEntry(value, name)
        return True
