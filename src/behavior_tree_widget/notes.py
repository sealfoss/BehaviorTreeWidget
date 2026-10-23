"""The Notes node: a resizable box of text that documents a part of the tree.

A Notes node is not part of the behavior tree: it has no connections, is never executed
and has no status. It only shows its text, word wrapped and drawn with the largest font
that fits the box. Dragging an edge or a corner of the box resizes it; a click on the
text edits it (the text changes as it is typed; Escape or a click elsewhere ends editing).

Notes are drawn below the connection lines and the other nodes, so a large note can
serve as a panel behind a group of nodes.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QTextCursor, QTextOption
from PySide6.QtWidgets import QFrame, QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget

from ._runctx import check_not_cancelled
from .nodes import NodeWidget

log = logging.getLogger("behavior_tree_widget")

__all__ = ["NOTES", "NotesNodeWidget", "ResizeHandle", "fitted_pixel_size"]

NOTES = "Notes"

#: Edges of a note moved by a resize handle (a corner handle moves two).
LEFT, TOP, RIGHT, BOTTOM = 1, 2, 4, 8

_EDGE_NAMES = {
    LEFT: "Left",
    TOP: "Top",
    RIGHT: "Right",
    BOTTOM: "Bottom",
    TOP | LEFT: "TopLeft",
    TOP | RIGHT: "TopRight",
    BOTTOM | LEFT: "BottomLeft",
    BOTTOM | RIGHT: "BottomRight",
}
_EDGE_CURSORS = {
    LEFT: Qt.CursorShape.SizeHorCursor,
    RIGHT: Qt.CursorShape.SizeHorCursor,
    TOP: Qt.CursorShape.SizeVerCursor,
    BOTTOM: Qt.CursorShape.SizeVerCursor,
    TOP | LEFT: Qt.CursorShape.SizeFDiagCursor,
    BOTTOM | RIGHT: Qt.CursorShape.SizeFDiagCursor,
    TOP | RIGHT: Qt.CursorShape.SizeBDiagCursor,
    BOTTOM | LEFT: Qt.CursorShape.SizeBDiagCursor,
}

TEXT_MARGIN = 7  # between the border and the text: the text starts beyond the resize handles
TEXT_FLAGS = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
MIN_PIXEL_SIZE = 1
MAX_PIXEL_SIZE = 2000
TEXT_STYLE = "QLabel { color: #2b2b2b; background: transparent; }"
PLACEHOLDER_STYLE = "QLabel { color: #8c7a35; background: transparent; }"
EDITOR_STYLE = "QPlainTextEdit { color: #2b2b2b; background-color: #fffbe3; border: none; }"


def fitted_pixel_size(font: QFont, size: QSize, text: str, flags: Any = TEXT_FLAGS) -> int:
    """The largest pixel size at which ``text`` drawn with ``font`` and ``flags`` fits in ``size``.

    With word wrapping, lines are broken between words only, so a word too long for the
    width makes the font smaller as well. Returns :data:`MIN_PIXEL_SIZE` when nothing fits.
    """
    width, height = size.width(), size.height()
    if width <= 0 or height <= 0 or not text:
        return MIN_PIXEL_SIZE
    rect = QRect(0, 0, width, height)
    probe = QFont(font)

    def fits(pixels: int) -> bool:
        probe.setPixelSize(pixels)
        bounds = QFontMetrics(probe).boundingRect(rect, int(flags), text)
        return bounds.width() <= width and bounds.height() <= height

    low, high = MIN_PIXEL_SIZE, max(MIN_PIXEL_SIZE, min(height, MAX_PIXEL_SIZE))
    while low < high:  # the result is a size that was measured to fit (or the minimum)
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


class ResizeHandle(QWidget):
    """Invisible strip along an edge (or square at a corner) of a note; dragging it resizes the note.

    The tree view does the resizing (see ``TreeView``); the handle only marks the area,
    with ``edges`` (a combination of LEFT, TOP, RIGHT and BOTTOM) telling which edges move.
    """

    THICKNESS = 6
    CORNER = 8

    def __init__(self, edges: int, parent: QWidget):
        super().__init__(parent)
        self.edges = edges
        self.setObjectName(f"ResizeHandle{_EDGE_NAMES[edges]}")
        self.setCursor(_EDGE_CURSORS[edges])
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class _FitLabel(QLabel):
    """Label drawing its plain, word-wrapped text with the largest font that fits it."""

    resized = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setIndent(0)
        self.setMargin(TEXT_MARGIN)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    # The note's size is chosen by the user: the text never asks for room.
    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt API)
        return False

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, 0)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, 0)

    def text_rect(self) -> QRect:
        """Where the text is drawn, in label coordinates (as QLabel lays out plain text)."""
        margin = self.margin()
        return self.contentsRect().adjusted(margin, margin, -margin, -margin)

    def show_text(self, text: str) -> None:
        self.setText(text)
        self.fit()

    def fit(self) -> None:
        """Use the largest font with which the text fits."""
        pixels = fitted_pixel_size(self.font(), self.text_rect().size(), self.text())
        if self.font().pixelSize() != pixels:
            font = QFont(self.font())
            font.setPixelSize(pixels)
            self.setFont(font)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.fit()
        self.resized.emit()


class _NoteEditor(QPlainTextEdit):
    """Editor shown over the text of a note while it is edited."""

    escapePressed = Signal()
    focusLost = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("NoteEditor")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WordWrap)  # breaks lines where the label does
        self.document().setDocumentMargin(0)
        self.setStyleSheet(EDITOR_STYLE)
        self.hide()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.escapePressed.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:  # not for the editor's own context menu
            self.focusLost.emit()


class NotesNodeWidget(NodeWidget):
    """A note documenting a part of the tree: a resizable box of text (see the module docs).

    It has no connections and is never executed. ``GetText``/``SetText`` and
    ``GetNoteSize``/``SetNoteSize`` read and change it from code; ``textChanged`` is
    emitted whenever its text changes. The text and size are saved with the tree.
    """

    UI_FILE = "NotesNode.ui"
    TYPE_NAME = NOTES
    _title = NOTES

    DEFAULT_SIZE = (220, 120)
    MIN_SIZE = (60, 30)
    MAX_SIZE = 2000  # per side: room the view keeps for a node's size (canvas.SCENE_MARGIN)
    PLACEHOLDER = "Click to write a note"

    textChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        self._text = ""
        self._note_size = QSize(*self.DEFAULT_SIZE)
        self._editing = False
        super().__init__(parent)
        self._title_label.hide()
        self._status_label.hide()
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout = self._ui.layout()
        if layout is None:
            layout = QVBoxLayout(self._ui)
            layout.setContentsMargins(0, 0, 0, 0)
        self._text_label = _FitLabel(self._ui)
        self._text_label.setObjectName("NoteText")
        layout.addWidget(self._text_label)
        self._editor = _NoteEditor(self._text_label)
        self._editor.textChanged.connect(self._on_editor_text_changed)
        self._editor.escapePressed.connect(self._on_editor_escape)
        self._editor.focusLost.connect(self.finish_editing)
        self._text_label.resized.connect(self._sync_editor)
        # Children of the node itself, so they lie above the text and the editor.
        self._handles = [ResizeHandle(edges, self) for edges in _EDGE_NAMES]
        self._refresh_text()
        self._notify_geometry()

    @classmethod
    def HasParentConnection(cls) -> bool:
        return False

    def GetTypeName(self) -> str:
        return NOTES

    def _title_tooltip(self) -> str:
        return f"{NOTES} - documents the tree; it is never executed"

    # ------------------------------------------------------------------ text
    def GetText(self) -> str:
        """The note's text (thread-safe)."""
        with self._lock:
            return self._text

    def SetText(self, text: str) -> None:
        """Replace the note's text (thread-safe)."""
        check_not_cancelled("SetText")
        text = str(text)
        runtime = self._is_runtime_change()
        with self._lock:
            if text == self._text:
                return
            self._text = text
        self._call_in_gui_thread(self._on_text_set, runtime)

    def _on_text_set(self, runtime: bool = False) -> None:
        text = self.GetText()
        if self._editing and self._editor.toPlainText() != text:
            blocked = self._editor.blockSignals(True)
            try:
                self._editor.setPlainText(text)
            finally:
                self._editor.blockSignals(blocked)
        self._refresh_text()
        self.textChanged.emit(text)
        self.changed.emit(runtime)

    def _on_editor_text_changed(self) -> None:
        text = self._editor.toPlainText()
        with self._lock:
            if text == self._text:
                return
            self._text = text
        self._refresh_text()
        self.textChanged.emit(text)
        self.changed.emit(False)  # typed by the user, also while the tree executes

    def _refresh_text(self) -> None:
        text = self.GetText()
        label = self._text_label
        style = TEXT_STYLE if text else PLACEHOLDER_STYLE
        if label.styleSheet() != style:
            label.setStyleSheet(style)
        label.show_text(text or self.PLACEHOLDER)
        self._sync_editor()

    def text_label(self) -> QLabel:
        """The label showing the text (for tests and customisation)."""
        return self._text_label

    def text_editor(self) -> QPlainTextEdit:
        """The editor shown while the text is edited (for tests and customisation)."""
        return self._editor

    # ------------------------------------------------------------------ editing
    def IsEditing(self) -> bool:
        """True while the text editor is shown."""
        return self._editing

    def begin_editing(self, position: QPoint | QPointF | None = None) -> None:
        """Show the text editor and give it the keyboard (GUI thread).

        ``position`` (in node coordinates) places the text cursor there; without it the
        cursor goes to the end of the text.
        """
        editor = self._editor
        if not self._editing:
            self._editing = True
            blocked = editor.blockSignals(True)
            try:
                editor.setPlainText(self.GetText())
            finally:
                editor.blockSignals(blocked)
            self._sync_editor()
            editor.show()
        if self._item is not None:
            self._item.setFocus(Qt.FocusReason.MouseFocusReason)
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        if position is None:
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
        else:
            point = position.toPoint() if isinstance(position, QPointF) else QPoint(position)
            cursor = editor.cursorForPosition(editor.viewport().mapFrom(self, point))
        editor.setTextCursor(cursor)

    def finish_editing(self) -> None:
        """Hide the text editor (the text is kept as typed)."""
        if not self._editing:
            return
        self._editing = False
        editor = self._editor
        if editor.hasFocus() or self.focusWidget() is editor:
            editor.clearFocus()
        editor.hide()

    def _on_editor_escape(self) -> None:
        canvas = self._canvas
        if canvas is not None:
            canvas._clear_scene_focus()  # the view gets the keyboard back (e.g. Delete deletes the note)
        self.finish_editing()

    def _sync_editor(self) -> None:
        """Lay the editor over the label's text with the label's font."""
        editor = self.__dict__.get("_editor")
        if editor is None or not self._editing:
            return
        editor.setGeometry(self._text_label.text_rect())
        if editor.font() != self._text_label.font():
            editor.setFont(self._text_label.font())

    # ------------------------------------------------------------------ size
    def GetNoteSize(self) -> tuple[int, int]:
        """``(width, height)`` of the note."""
        size = self._note_size
        return size.width(), size.height()

    def SetNoteSize(self, width: float, height: float) -> None:
        """Resize the note, keeping its top-left corner (thread-safe).

        The size is rounded and limited to ``MIN_SIZE`` .. ``MAX_SIZE``.

        Raises:
            TypeError: a size is not a number.
            ValueError: a size is not finite.
        """
        check_not_cancelled("SetNoteSize")
        size = self.clamped_size(width, height)
        runtime = self._is_runtime_change()
        self._call_in_gui_thread(self._on_size_set, size, runtime)

    def _on_size_set(self, size: tuple[int, int], runtime: bool = False) -> None:
        if size != self.GetNoteSize():
            self._resize_note(*size)
            self.changed.emit(runtime)

    @classmethod
    def clamped_size(cls, width: Any, height: Any) -> tuple[int, int]:
        """``(width, height)`` rounded and limited to ``MIN_SIZE`` .. ``MAX_SIZE``."""
        result = []
        for value, minimum in ((width, cls.MIN_SIZE[0]), (height, cls.MIN_SIZE[1])):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"note sizes must be numbers, not {type(value).__name__}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"note sizes must be finite, not {value!r}")
            result.append(min(max(round(value), minimum), cls.MAX_SIZE))
        return result[0], result[1]

    def _resize_note(self, width: float, height: float) -> tuple[int, int]:
        """Apply a size (clamped) without reporting a change; returns it. GUI thread only.

        The tree view uses it while the user drags a handle and reports the change once.
        """
        width, height = self.clamped_size(width, height)
        size = QSize(width, height)
        if size != self._note_size:
            self._note_size = size
            self._notify_geometry()
        return width, height

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._note_size)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._note_size)

    def _notify_geometry(self) -> None:
        # Unlike other nodes, a note does not take the size of its contents.
        self.updateGeometry()
        self.resize(self._note_size)
        self._place_handles()
        self.geometryHintChanged.emit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_handles()

    def _place_handles(self) -> None:
        handles = self.__dict__.get("_handles")
        if not handles:
            return
        thickness, corner = ResizeHandle.THICKNESS, ResizeHandle.CORNER
        width, height = self.width(), self.height()
        geometry = {
            TOP | LEFT: (0, 0, corner, corner),
            TOP | RIGHT: (width - corner, 0, corner, corner),
            BOTTOM | LEFT: (0, height - corner, corner, corner),
            BOTTOM | RIGHT: (width - corner, height - corner, corner, corner),
            TOP: (corner, 0, width - 2 * corner, thickness),
            BOTTOM: (corner, height - thickness, width - 2 * corner, thickness),
            LEFT: (0, corner, thickness, height - 2 * corner),
            RIGHT: (width - thickness, corner, thickness, height - 2 * corner),
        }
        for handle in handles:
            handle.setGeometry(*geometry[handle.edges])
            handle.raise_()

    def resize_handles(self) -> list[ResizeHandle]:
        """The eight resize handles (for tests and customisation)."""
        return list(self._handles)

    # ------------------------------------------------------------------ persistence
    def _to_dict(self) -> dict:
        width, height = self.GetNoteSize()
        return {"text": self.GetText(), "width": width, "height": height}

    def _load_dict(self, data: dict) -> None:
        text = data.get("text")
        if isinstance(text, str):
            with self._lock:
                self._text = text
            self._refresh_text()
        elif text is not None:
            log.warning("%s: ignoring saved text of type %s", self, type(text).__name__)
        size = list(self.GetNoteSize())
        for index, key in enumerate(("width", "height")):
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                log.warning("%s: ignoring saved %s %r", self, key, value)
                continue
            size[index] = value
        self._resize_note(*size)
