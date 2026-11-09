"""The Group node: a named, resizable box holding a part of the tree.

A group is not part of the behavior tree: it has no connections, is never executed and
has no status. Its name, which is the node's title, is edited in the line edit at its
top left. Dragging the group by its title bar also moves every node, note and group lying
completely inside it; dragging an edge or a corner resizes it, as for notes.

Groups are drawn below everything else, notes included, so the nodes they hold stay
visible: only the title bar and the edges of a group take mouse clicks, and clicks on
its inside go to the canvas (e.g. to draw a selection rectangle or to add a node there).

The right-click menu chooses the border colour, selects the group's contents, and
promotes the group to a sub-tree (see ``BehaviorTreeWidget.PromoteGroupToSubTree``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QLineEdit, QWidget

from ._qt import set_text_quietly
from ._runctx import check_not_cancelled
from .notes import BoxNodeWidget

log = logging.getLogger("behavior_tree_widget")

__all__ = [
    "DEFAULT_GROUP_COLOR",
    "GROUP",
    "GROUP_COLORS",
    "GroupNodeWidget",
    "default_group_name",
    "default_group_number",
]

GROUP = "Group"
DEFAULT_NAME_PREFIX = "group_"
_DEFAULT_NAME = re.compile(r"group_(\d+)\Z")

#: Border colours offered by the right-click menu, by name.
GROUP_COLORS = {
    "Blue": "#3d7cc9",
    "Green": "#2e9d4f",
    "Red": "#d04545",
    "Orange": "#e08a1e",
    "Purple": "#8a5cc2",
    "Teal": "#1f9e9e",
    "Gray": "#7a7a7a",
}
DEFAULT_GROUP_COLOR = GROUP_COLORS["Blue"]


def default_group_name(number: int) -> str:
    """The default name of group ``number``: ``group_001`` for 1 (three digits, padded with zeros)."""
    return f"{DEFAULT_NAME_PREFIX}{number:03d}"


def default_group_number(name: str) -> int | None:
    """``number`` if ``name`` is a default group name (``group_<number>``), else None."""
    match = _DEFAULT_NAME.match(name) if isinstance(name, str) else None
    return int(match.group(1)) if match else None


def parse_color(color: Any) -> QColor:
    """A valid, opaque QColor for ``color`` (a QColor or a colour name such as ``"#3d7cc9"``).

    Raises:
        TypeError: ``color`` is neither a QColor nor a str.
        ValueError: ``color`` is not a valid colour.
    """
    if isinstance(color, QColor):
        parsed = QColor(color)
    elif isinstance(color, str):
        parsed = QColor(color.strip())
    else:
        raise TypeError(f"colors must be a QColor or a color name such as '#3d7cc9', not {type(color).__name__}")
    if not parsed.isValid():
        raise ValueError(f"{color!r} is not a valid color")
    parsed.setAlpha(255)
    return parsed


class GroupNodeWidget(BoxNodeWidget):
    """A named box holding a part of the tree (see the module docs).

    The group's name is its title (``GetTitle`` / ``SetTitle``). ``GetBorderColor`` /
    ``SetBorderColor`` read and change the border colour (``colorChanged`` is emitted), and
    ``GetGroupSize`` / ``SetGroupSize`` the size. ``GetContainedNodes`` lists the nodes lying
    completely inside the group, which move with it. The name, size and colour are saved.
    """

    UI_FILE = "GroupNode.ui"
    TYPE_NAME = GROUP
    _title = GROUP

    DEFAULT_SIZE = (360, 240)
    MIN_SIZE = (120, 60)
    MAX_SIZE = 2000  # per side: room the view keeps for a node's size (geometry.SCENE_MARGIN)
    NAME_MIN_WIDTH = 60
    NAME_GRAB_ROOM = 28  # part of the title bar right of the name kept free for dragging the group
    _name_edit: QLineEdit | None = None  # until __init__ finds it (title hooks run before)

    colorChanged = Signal(str)  # "#rrggbb"

    def __init__(self, parent: QWidget | None = None):
        self._color = QColor(DEFAULT_GROUP_COLOR)
        super().__init__(parent)
        # Only the frame drawn by the ui (translucent inside) is painted: the nodes below show through.
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._header: QFrame = self._require_child(QFrame, "GroupHeader")
        self._header.setProperty("btDragArea", True)  # the title bar moves the group
        self._name_edit: QLineEdit = self._require_child(QLineEdit, "GroupName")
        self._name_edit.setText(self.GetTitle())
        self._name_edit.setCursorPosition(0)
        self._name_at_focus = self.GetTitle()
        self._name_edit.textEdited.connect(self._on_name_typed)
        self._name_edit.textChanged.connect(self._fit_name_edit)
        self._name_edit.editingFinished.connect(self._on_name_finished)
        self._name_edit.installEventFilter(self)
        self._create_handles()
        self._apply_style()
        self._fit_name_edit()

    @classmethod
    def _size_noun(cls) -> str:
        return "group"

    def GetTypeName(self) -> str:
        return GROUP

    def _title_tooltip(self) -> str:
        return f"{GROUP} - drag its title bar to move it with everything inside it"

    # ------------------------------------------------------------------ name
    def name_editor(self) -> QLineEdit:
        """The line edit showing the group name (for tests and customisation)."""
        return self._name_edit

    def _refresh_title(self) -> None:
        super()._refresh_title()
        edit = self._name_edit
        if edit is None:
            return
        title = self.GetTitle()
        if set_text_quietly(edit, title):
            self._fit_name_edit()
        edit.setToolTip(f"Group name: {title}" if title else "Group name")

    def _on_name_typed(self, text: str) -> None:
        if text != self.GetTitle():
            self._apply_title(text, runtime=False)  # typed by the user, also while the tree executes

    def _on_name_finished(self) -> None:
        text = self._name_edit.text().strip()
        if not text:
            text = self._name_at_focus.strip() or self._default_title()  # a group always has a name
        if text != self.GetTitle():
            self._apply_title(text, runtime=False)
        if self._name_edit.text() != text:
            self._refresh_title()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._name_edit and watched is not None:
            kind = event.type()
            if kind == QEvent.Type.FocusIn:
                self._name_at_focus = self.GetTitle()
            elif kind == QEvent.Type.KeyPress:
                key = event.key()
                if key == Qt.Key.Key_Escape:
                    if self._name_at_focus and self._name_at_focus != self.GetTitle():
                        self._apply_title(self._name_at_focus, runtime=False)  # undo the typing
                    self._leave_name_editor()
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._leave_name_editor()
                    return True
        return super().eventFilter(watched, event)

    def _leave_name_editor(self) -> None:
        """Give the keyboard back to the tree view (e.g. so Delete deletes the selected group)."""
        canvas = self._canvas
        if canvas is not None:
            canvas._clear_scene_focus()
        if self._name_edit.hasFocus():
            self._name_edit.clearFocus()

    def _fit_name_edit(self, *_args) -> None:
        """Size the name editor to its text, leaving part of the title bar free to grab the group."""
        edit = self._name_edit
        if edit is None:
            return
        text = edit.text() or edit.placeholderText()
        wanted = edit.fontMetrics().horizontalAdvance(text) + 24  # frame, padding and the cursor
        layout = self._header.layout()
        margins = layout.contentsMargins().left() + layout.contentsMargins().right() if layout is not None else 16
        available = self._box_size.width() - margins - self.NAME_GRAB_ROOM
        edit.setFixedWidth(max(self.NAME_MIN_WIDTH, min(wanted, available)))

    # ------------------------------------------------------------------ colour
    def GetBorderColor(self) -> str:
        """The border colour as ``"#rrggbb"``."""
        return self._color.name()

    def SetBorderColor(self, color: QColor | str) -> None:
        """Change the border colour (a QColor or a colour name such as ``"#3d7cc9"``; thread-safe).

        Raises:
            TypeError, ValueError: ``color`` is not a valid colour.
        """
        check_not_cancelled("SetBorderColor")
        parsed = parse_color(color)
        runtime = self._is_runtime_change()
        self._call_in_gui_thread(self._apply_color, parsed.name(), runtime)

    def _apply_color(self, color: str, runtime: bool = False) -> bool:
        """Use ``color`` (GUI thread); returns True if it changed."""
        parsed = parse_color(color)
        if parsed == self._color:
            return False
        self._color = parsed
        self._apply_style()
        self.colorChanged.emit(parsed.name())
        self.changed.emit(runtime)
        return True

    def _apply_style(self) -> None:
        color = self._color
        red, green, blue, name = color.red(), color.green(), color.blue(), color.name()
        self._ui.setStyleSheet(
            f"QFrame#GroupNode {{ background-color: rgba({red}, {green}, {blue}, 22); "
            f"border: 2px solid {name}; border-radius: 6px; }}\n"
            f"QFrame#GroupHeader {{ background-color: rgba({red}, {green}, {blue}, 85); border: none; "
            "border-top-left-radius: 5px; border-top-right-radius: 5px; }\n"
            "QLineEdit#GroupName { background-color: rgba(255, 255, 255, 220); color: #1e1e1e; "
            f"border: 1px solid {name}; border-radius: 3px; padding: 1px 4px; font-weight: bold; }}"
        )

    # ------------------------------------------------------------------ size and contents
    def GetGroupSize(self) -> tuple[int, int]:
        """``(width, height)`` of the group."""
        return self.GetBoxSize()

    def SetGroupSize(self, width: float, height: float) -> None:
        """Resize the group, keeping its top-left corner (thread-safe); limited to ``MIN_SIZE`` .. ``MAX_SIZE``.

        Raises:
            TypeError: a size is not a number.
            ValueError: a size is not finite.
        """
        check_not_cancelled("SetGroupSize")
        self._set_box_size(width, height)

    def _notify_geometry(self) -> None:
        super()._notify_geometry()
        self._fit_name_edit()

    def header_rect(self) -> QRectF:
        """The title bar (holding the name), in node coordinates."""
        header = self._header
        return QRectF(QPointF(header.mapTo(self, QPoint(0, 0))), QSizeF(header.size()))

    def scene_header_rect(self) -> QRectF:
        """The title bar in scene coordinates (an empty rectangle while the group is not in a view)."""
        if self._item is None:
            return QRectF()
        return self._item.mapRectToScene(self.header_rect())

    def GetContainedNodes(self) -> list:
        """The nodes (notes and groups included) lying completely inside the group, in the
        order they were added to the view. They move with the group. GUI thread only."""
        canvas = self._canvas
        return canvas.group_contents(self) if canvas is not None else []

    # ------------------------------------------------------------------ persistence
    def _to_dict(self) -> dict:
        width, height = self.GetBoxSize()
        return {"width": width, "height": height, "color": self.GetBorderColor()}

    def _load_dict(self, data: dict) -> None:
        color = data.get("color")
        if color is not None:
            try:
                parsed = parse_color(color)
            except (TypeError, ValueError):
                log.warning("%s: ignoring saved color %r", self, color)
            else:
                self._color = parsed
                self._apply_style()
        self._load_box_size(data)
