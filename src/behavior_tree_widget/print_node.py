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
disappears (it is then shown as missing, in red); see
:class:`~behavior_tree_widget.blackboard_aware.BlackboardAwareLeaf`. It succeeds once the
message was handed to the log (also when the log level of the tree drops it) and fails with an
error when the chosen entry does not exist.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QSizePolicy, QWidget

from ._qt import set_text_quietly
from .blackboard import safe_repr
from .blackboard_aware import LITERAL, BlackboardAwareLeaf, ComboItem, EntryComboBox
from .nodes import _guard_wheel
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


class PrintMessageNodeWidget(BlackboardAwareLeaf):
    """Writes ``Message`` (or the value of the ``Source`` entry) to the tree's log at ``Level``.

    The ``Source`` combo box of the ui file is replaced by an :class:`EntryComboBox` with the
    same name, place, tooltip and size policy.
    """

    UI_FILE = "PrintNode.ui"
    TYPE_NAME = PRINT_MESSAGE
    _title = PRINT_MESSAGE
    LEVEL_FIELD = "Level"
    SOURCE_FIELD = "Source"
    MESSAGE_FIELD = "Message"
    ENTRY_FIELDS = (SOURCE_FIELD,)
    _fields = {LEVEL_FIELD: int(DEFAULT_LEVEL), SOURCE_FIELD: None, MESSAGE_FIELD: ""}

    def __init__(self, parent: QWidget | None = None):
        self._level_box: QComboBox | None = None
        self._source_box: EntryComboBox | None = None
        self._message_edit: QLineEdit | None = None
        self._message_label: QLabel | None = None
        self._laid_out = False  # the editors were laid out once
        super().__init__(parent)
        self._level_box = self._require_child(QComboBox, "LogLevel")
        self._source_box = EntryComboBox.replacing(self._require_child(QComboBox, "Source"))
        self._message_edit = self._require_child(QLineEdit, "Message")
        self._message_label = self._find_child(QLabel, "MessageLabel")
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

    # ------------------------------------------------------------------ editors
    def _refresh_editors(self) -> None:
        """Show the node's choices and the blackboard's current entries in the editors."""
        level_box, source_box, edit = self._level_box, self._source_box, self._message_edit
        if level_box is None or source_box is None or edit is None or not shiboken6.isValid(level_box):
            return  # still being built, or being destroyed
        level = self.GetLevel()
        source = self.GetSource()
        index = max(0, level_box.findData(int(level)))
        if level_box.currentIndex() != index:
            with QSignalBlocker(level_box):
                level_box.setCurrentIndex(index)
        source_box.set_items(self._source_items(self._entry_types(), source), source)
        set_text_quietly(edit, self.GetMessage())
        use_literal = source is None
        hidden = edit.isHidden()
        edit.setVisible(use_literal)
        if self._message_label is not None:
            self._message_label.setVisible(use_literal)
        if not self._laid_out or edit.isHidden() != hidden:
            # Only this changes the size of the node: the combo boxes have a fixed width.
            self._laid_out = True
            self._notify_geometry()

    @staticmethod
    def _source_items(types: dict[str, str], source: str | None) -> list[ComboItem]:
        """Rows of the Source box: Literal, every entry, and the chosen one marked missing if it does not exist."""
        items = [ComboItem(LITERAL, None, "Print the Message")]
        items += [ComboItem(entry, entry, f"{kind} entry: print its value") for entry, kind in types.items()]
        if source is not None and source not in types:
            missing = f"The blackboard has no entry named {source!r}"
            items.append(ComboItem(f"{source} (missing)", source, missing, True))
        return items

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
