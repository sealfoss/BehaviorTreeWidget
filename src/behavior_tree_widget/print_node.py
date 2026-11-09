"""Built-in leaf node that writes a message to the tree's log: Print Message.

The node shows three editors, named as in ``PrintNode.ui``:

* ``LogLevel`` - a combo box offering the log levels (see :class:`~behavior_tree_widget.LogLevel`);
  the message is logged at the chosen level. The default is ``3: DEBUG``; ``0: None`` prints
  nothing.
* ``Source`` - a combo box offering "Literal" and every blackboard entry: what is printed.
* ``Message`` - the text printed while "Literal" is chosen (shown only then).

When an entry is chosen, the node prints ``<entry name> = <value>`` (a String entry's text as
it is, other values as Python literals). The choices are the node's fields and are saved with
the tree: ``Level`` (an int, 0 - 4), ``Source`` (an entry name, or ``None`` for the literal)
and ``Message``. The node follows entries that are renamed and keeps its choice when the entry
disappears (it is then shown as missing, in red). It succeeds once the message was handed to
the log (also when the log level of the tree drops it) and fails with an error when the chosen
entry does not exist.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QSizePolicy, QWidget

from .blackboard import safe_repr
from .blackboard_nodes import LITERAL, PROBLEM_COLOR, _show_problem
from .nodes import LeafNodeWidget, _guard_wheel
from .tree_log import LogLevel

if TYPE_CHECKING:  # pragma: no cover
    from .widget import BehaviorTreeWidget

log = logging.getLogger("behavior_tree_widget")

__all__ = ["PRINT_MESSAGE", "PrintMessageNodeWidget", "value_text"]

PRINT_MESSAGE = "Print Message"
DEFAULT_LEVEL = LogLevel.DEBUG


def value_text(value: Any) -> str:
    """How a blackboard value is printed: text as it is, anything else as a (bounded) Python literal."""
    return value if isinstance(value, str) else safe_repr(value)


class PrintMessageNodeWidget(LeafNodeWidget):
    """Writes ``Message`` (or the value of the ``Source`` entry) to the tree's log at ``Level``."""

    UI_FILE = "PrintNode.ui"
    TYPE_NAME = PRINT_MESSAGE
    _title = PRINT_MESSAGE
    RUN_IN_THREAD = False
    LEVEL_FIELD = "Level"
    SOURCE_FIELD = "Source"
    MESSAGE_FIELD = "Message"
    _fields = {LEVEL_FIELD: int(DEFAULT_LEVEL), SOURCE_FIELD: None, MESSAGE_FIELD: ""}

    def __init__(self, parent: QWidget | None = None):
        self._store = None  # BlackboardStore of the tree the node was added to
        super().__init__(parent)
        self._level_box: QComboBox = self._require_child(QComboBox, "LogLevel")
        self._source_box: QComboBox = self._require_child(QComboBox, "Source")
        self._message_edit: QLineEdit = self._require_child(QLineEdit, "Message")
        self._message_label: QLabel | None = self._find_child(QLabel, "MessageLabel")
        self._source_tooltip = self._source_box.toolTip()
        self._level_box.clear()
        for level in LogLevel:
            self._level_box.addItem(level.label, int(level))
        for box in (self._level_box, self._source_box):
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(12)
            box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            _guard_wheel(box)
        self._message_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._level_box.currentIndexChanged.connect(self._on_level_chosen)
        self._source_box.currentIndexChanged.connect(self._on_source_chosen)
        self._message_edit.textEdited.connect(self._on_message_typed)
        self._refresh_editors()

    def _title_tooltip(self) -> str:
        return f"{PRINT_MESSAGE} - writes the Message, or the value of a blackboard entry, to the tree's log"

    # ------------------------------------------------------------------ user API
    def GetLevel(self) -> LogLevel:
        """The level the message is logged at."""
        with self._lock:
            value = self._fields.get(self.LEVEL_FIELD)
        try:
            return LogLevel.coerce(value)
        except (TypeError, ValueError):
            return DEFAULT_LEVEL

    def SetLevel(self, level: LogLevel | int | str) -> None:
        """Choose the level (a LogLevel, its number or its name) (thread-safe)."""
        self.SetField(self.LEVEL_FIELD, int(LogLevel.coerce(level)))

    def GetSource(self) -> str | None:
        """The blackboard entry whose value is printed, or None when the Message is printed."""
        with self._lock:
            name = self._fields.get(self.SOURCE_FIELD)
        return name if isinstance(name, str) and name else None

    def SetSource(self, name: str | None) -> None:
        """Print the value of entry ``name`` (None: print the Message) (thread-safe)."""
        if name is not None and not isinstance(name, str):
            raise TypeError(f"Source must be a str or None, not {type(name).__name__}")
        self.SetField(self.SOURCE_FIELD, name or None)

    def UsesLiteral(self) -> bool:
        """True when "Literal" is chosen: the Message is printed."""
        return self.GetSource() is None

    def GetMessage(self) -> str:
        with self._lock:
            value = self._fields.get(self.MESSAGE_FIELD)
        return value if isinstance(value, str) else ""

    def SetMessage(self, message: str) -> None:
        """Change the text printed while "Literal" is chosen (thread-safe)."""
        if not isinstance(message, str):
            raise TypeError(f"Message must be a str, not {type(message).__name__}")
        self.SetField(self.MESSAGE_FIELD, message)

    def message_text(self, tree: BehaviorTreeWidget | None) -> str:
        """The text the node prints now.

        Raises:
            ValueError: the chosen entry does not exist.
        """
        source = self.GetSource()
        if source is None:
            return self.GetMessage()
        if tree is None:
            raise RuntimeError("the node is not part of a BehaviorTreeWidget")
        try:
            value = tree.GetEntry(source)
        except KeyError:
            raise ValueError(f"the blackboard has no entry named {source!r} (Source)") from None
        return f"{source} = {value_text(value)}"

    def OnRun(self, tree):
        level = self.GetLevel()
        if level is LogLevel.NONE:
            return True
        tree.Log(level, self.message_text(tree))
        return True

    # ------------------------------------------------------------------ blackboard
    def _on_added_to_tree(self) -> None:
        tree = self._tree
        store = tree.blackboardStore() if tree is not None else None
        if store is not None and store is not self._store:
            self._store = store
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
        with self._lock:
            renamed = self._fields.get(self.SOURCE_FIELD) == old
            if renamed:
                self._fields[self.SOURCE_FIELD] = new
        if renamed:
            runtime = self._is_runtime_change()
            self.fieldChanged.emit(self.SOURCE_FIELD)
            self.changed.emit(runtime)
        self._refresh_editors()

    # ------------------------------------------------------------------ editors
    def _refresh_all_fields(self) -> None:
        self._refresh_editors()

    def _refresh_field_editor(self, key: str) -> None:
        self._refresh_editors()

    def _refresh_editors(self) -> None:
        """Show the node's choices and the blackboard's current entries in the editors."""
        if "_level_box" not in self.__dict__ or not shiboken6.isValid(self._level_box):
            return
        level = self.GetLevel()
        source = self.GetSource()
        message = self.GetMessage()
        blocked = self._level_box.blockSignals(True)
        try:
            self._level_box.setCurrentIndex(max(0, self._level_box.findData(int(level))))
        finally:
            self._level_box.blockSignals(blocked)

        types = self._entry_types()
        items = [(LITERAL, None, "Print the Message", False)]
        items += [(entry, entry, f"{kind} entry: print its value", False) for entry, kind in types.items()]
        if source is not None and source not in types:
            items.append((f"{source} (missing)", source, f"The blackboard has no entry named {source!r}", True))
        self._fill_source(items, source)

        edit = self._message_edit
        if edit.text() != message:
            blocked = edit.blockSignals(True)
            try:
                edit.setText(message)
                if not edit.hasFocus():
                    edit.setCursorPosition(0)
            finally:
                edit.blockSignals(blocked)
        use_literal = source is None
        edit.setVisible(use_literal)
        if self._message_label is not None:
            self._message_label.setVisible(use_literal)
        self._notify_geometry()

    def _fill_source(self, items: list[tuple[str, Any, str, bool]], current: str | None) -> None:
        box = self._source_box
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
            index = 0 if current is None else next((i for i, item in enumerate(items) if item[1] == current), 0)
            box.setCurrentIndex(index)
        finally:
            box.blockSignals(blocked)
        problem = items[index][3]
        _show_problem(box, problem)
        box.setToolTip(items[index][2] if problem else self._source_tooltip)

    # ------------------------------------------------------------------ user edits
    def _on_level_chosen(self, index: int) -> None:
        level = self._level_box.itemData(index) if index >= 0 else None
        if not isinstance(level, int):
            return
        with self._lock:
            if self._fields.get(self.LEVEL_FIELD) == level:
                return
        self._on_editor_value(self.LEVEL_FIELD, level)

    def _on_source_chosen(self, index: int) -> None:
        if index < 0:
            return
        data = self._source_box.itemData(index)
        source = data if isinstance(data, str) and data else None
        with self._lock:
            if self._fields.get(self.SOURCE_FIELD) == source:
                return
        self._on_editor_value(self.SOURCE_FIELD, source)
        self._refresh_editors()

    def _on_message_typed(self, text: str) -> None:
        self._on_editor_value(self.MESSAGE_FIELD, text)

    # ------------------------------------------------------------------ persistence
    def _to_dict(self) -> dict:
        with self._lock:
            return {"fields": dict(self._fields)}

    def _load_dict(self, data: dict) -> None:
        saved = data.get("fields")
        if isinstance(saved, dict):
            with self._lock:
                if self.LEVEL_FIELD in saved:
                    try:
                        self._fields[self.LEVEL_FIELD] = int(LogLevel.coerce(saved[self.LEVEL_FIELD]))
                    except (TypeError, ValueError):
                        log.warning("%s: ignoring saved Level %r", self, saved[self.LEVEL_FIELD])
                if self.SOURCE_FIELD in saved:
                    source = saved[self.SOURCE_FIELD]
                    if source is None or isinstance(source, str):
                        self._fields[self.SOURCE_FIELD] = source or None
                    else:
                        log.warning("%s: ignoring saved Source of type %s", self, type(source).__name__)
                message = saved.get(self.MESSAGE_FIELD)
                if isinstance(message, str):
                    self._fields[self.MESSAGE_FIELD] = message
                elif message is not None:
                    log.warning("%s: ignoring saved Message of type %s", self, type(message).__name__)
        self._refresh_editors()
