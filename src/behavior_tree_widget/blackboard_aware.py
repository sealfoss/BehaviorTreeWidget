"""Leaf nodes whose editors list the blackboard's entries, and the combo box they use.

:class:`BlackboardAwareLeaf` is the base class of the built-in Evaluation, Set and Print
Message nodes. It connects a node to the blackboard of the tree it is added to, makes the
fields named in ``ENTRY_FIELDS`` (which hold entry names) follow entries that are renamed,
and refreshes the node's editors when entries are added, removed, renamed, retyped or
reordered:

* at once, so the editors always show the blackboard as it is;
* once, at the end of a ``BlackboardStore.batch()`` (e.g. while the blackboard of a loaded
  tree is installed), instead of once per change;
* not at all while the node is not in a view: it was removed (and waits to be deleted), or it
  is a hidden node of a sub-tree being executed. Such a node does not follow renames either.

:class:`EntryComboBox` lists entries (and "Literal") and updates itself with minimal edits:
adding one entry to a blackboard of fifty inserts one row instead of rebuilding fifty.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, override

import shiboken6
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import QComboBox, QWidget

from .nodes import LeafNodeWidget

if TYPE_CHECKING:  # pragma: no cover
    from .blackboard import BlackboardStore

log = logging.getLogger("behavior_tree_widget")

__all__ = ["LITERAL", "PROBLEM_COLOR", "BlackboardAwareLeaf", "ComboItem", "EntryComboBox", "show_problem"]

LITERAL = "Literal"  # text of the choice that uses the node's own value instead of an entry
PROBLEM_COLOR = QColor("#c62828")  # text of a missing entry, or of an entry of the wrong type


def show_problem(widget: QWidget, problem: bool) -> None:
    """Draw the text of ``widget`` in red (``problem``) or in the inherited colours."""
    if not problem:
        widget.setPalette(QPalette())  # back to the inherited palette
        return
    palette = QPalette(widget.palette())
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(role, PROBLEM_COLOR)
    widget.setPalette(palette)


class ComboItem(NamedTuple):
    """One row of an :class:`EntryComboBox`."""

    text: str
    data: Any  # an entry name, or None for the "Literal" row
    tooltip: str
    problem: bool = False  # drawn in red: a missing entry, or one the node cannot use


class EntryComboBox(QComboBox):
    """Combo box listing blackboard entries, updated with minimal edits (see :meth:`set_items`).

    The rows it shows are kept here, so an update never reads them back from Qt. A chosen row
    marked as a problem turns the box red and its tooltip says why; otherwise the box shows its
    normal tooltip (the one it had when it was created by :meth:`replacing`).
    """

    #: Properties copied from the combo box an EntryComboBox replaces (besides the size policy).
    COPIED_PROPERTIES = (
        "objectName", "toolTip", "statusTip", "whatsThis", "accessibleName", "accessibleDescription",
        "minimumSize", "maximumSize", "focusPolicy", "contextMenuPolicy", "sizeAdjustPolicy",
        "minimumContentsLength", "maxVisibleItems", "iconSize", "frame", "placeholderText", "styleSheet",
    )

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[ComboItem] = []
        self._normal_tooltip = ""
        self._problem_shown = False

    @classmethod
    def replacing(cls, box: QComboBox) -> EntryComboBox:
        """A new EntryComboBox in the place of ``box`` (e.g. a QComboBox created by a ``.ui`` file),
        which is deleted: same parent, place in the layout and in the focus chain, name, tooltip,
        size policy and the other :attr:`COPIED_PROPERTIES`; an explicitly set font or palette too."""
        if isinstance(box, cls):
            return box
        combo = cls(box.parentWidget())
        for name in cls.COPIED_PROPERTIES:
            combo.setProperty(name, box.property(name))
        combo.setSizePolicy(box.sizePolicy())
        if box.testAttribute(Qt.WidgetAttribute.WA_SetFont):
            combo.setFont(box.font())
        if box.testAttribute(Qt.WidgetAttribute.WA_SetPalette):
            combo.setPalette(box.palette())
        combo._normal_tooltip = box.toolTip()
        parent = box.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is not None:
            layout.replaceWidget(box, combo)
        if parent is not None:
            QWidget.setTabOrder(box, combo)  # takes the place of ``box`` once ``box`` is deleted
        box.setObjectName("")  # findChild must not find it before it is deleted
        box.hide()
        box.deleteLater()
        return combo

    def items(self) -> list[ComboItem]:
        """The rows shown (a copy)."""
        return list(self._items)

    def set_normal_tooltip(self, tooltip: str) -> None:
        """The tooltip shown while the chosen row is not a problem."""
        self._normal_tooltip = tooltip
        if not self._problem_shown:
            self.setToolTip(tooltip)

    def set_items(self, items: Sequence[ComboItem | tuple], current: Any) -> None:
        """Show ``items`` and choose the row whose data is ``current``; emits no signals.

        ``items`` are :class:`ComboItem` rows (or ``(text, data, tooltip, problem)`` tuples).
        Rows are changed in place and only the rows that differ are inserted or removed.
        ``current`` None chooses the first row if its data is None (the "Literal" row), and no
        row otherwise (the placeholder text is shown).
        """
        new = [item if isinstance(item, ComboItem) else ComboItem(*item) for item in items]
        with QSignalBlocker(self):
            if self.count() != len(self._items):  # changed from outside (e.g. clear()): start over
                self.clear()
                self._items = []
            self._apply(new)
            index = self._index_of(new, current)
            if self.currentIndex() != index:
                self.setCurrentIndex(index)
        problem = index >= 0 and new[index].problem
        if problem != self._problem_shown:
            show_problem(self, problem)
            self._problem_shown = problem
        tooltip = new[index].tooltip if problem else self._normal_tooltip
        if self.toolTip() != tooltip:
            self.setToolTip(tooltip)

    @staticmethod
    def _index_of(items: list[ComboItem], current: Any) -> int:
        if current is None:
            return 0 if items and items[0].data is None else -1
        return next((index for index, item in enumerate(items) if item.data is not None and item.data == current), -1)

    def _apply(self, new: list[ComboItem]) -> None:
        """Turn the rows shown into ``new``: keep the rows both start and end with, change the rows
        in between in place, then insert or remove the rows one list has more of."""
        old = self._items
        start, limit = 0, min(len(old), len(new))
        while start < limit and old[start][:2] == new[start][:2]:  # same text and data
            start += 1
        end_old, end_new = len(old), len(new)
        while end_old > start and end_new > start and old[end_old - 1][:2] == new[end_new - 1][:2]:
            end_old -= 1
            end_new -= 1
        common = min(end_old, end_new) - start
        for index in range(start, start + common):
            self._update_row(index, old[index], new[index])
        for _ in range(end_old - start - common):
            self.removeItem(start + common)
        for index in range(start + common, end_new):
            self._insert_row(index, new[index])
        shift = len(new) - len(old)
        for index in [*range(start), *range(end_new, len(new))]:  # kept rows: tooltip / problem may differ
            before = old[index if index < start else index - shift]
            if before != new[index]:
                self._set_roles(index, new[index], before)
        self._items = new

    def _insert_row(self, index: int, item: ComboItem) -> None:
        self.insertItem(index, item.text, item.data)
        self._set_roles(index, item, None)

    def _update_row(self, index: int, before: ComboItem, item: ComboItem) -> None:
        if before.text != item.text:
            self.setItemText(index, item.text)
        if before.data != item.data or type(before.data) is not type(item.data):
            self.setItemData(index, item.data)
        self._set_roles(index, item, before)

    def _set_roles(self, index: int, item: ComboItem, before: ComboItem | None) -> None:
        if before is None or before.tooltip != item.tooltip:
            self.setItemData(index, item.tooltip, Qt.ItemDataRole.ToolTipRole)
        if before is None or before.problem != item.problem:
            brush = QBrush(PROBLEM_COLOR) if item.problem else None
            if brush is not None or before is not None:
                self.setItemData(index, brush, Qt.ItemDataRole.ForegroundRole)


class BlackboardAwareLeaf(LeafNodeWidget):
    """Base class of leaf nodes whose editors list the blackboard's entries (see the module docs).

    Subclasses name the fields that hold entry names in ``ENTRY_FIELDS`` and implement
    :meth:`_refresh_editors`, which shows the node's choices and the current entries.
    """

    #: Fields holding the name of a blackboard entry: they follow entries that are renamed.
    ENTRY_FIELDS: tuple[str, ...] = ()
    RUN_IN_THREAD = False

    def __init__(self, parent: QWidget | None = None):
        self._store: BlackboardStore | None = None  # the blackboard of the tree the node was added to
        self._refresh_due = False  # a refresh waits for the end of a blackboard batch
        super().__init__(parent)

    # ------------------------------------------------------------------ blackboard
    @override
    def _on_added_to_tree(self) -> None:
        tree = self._tree
        store = tree.blackboard_store() if tree is not None else None
        if store is not None and store is not self._store:
            self._store = store
            # Structure changes only (not values); queued when they come from worker threads.
            store.entryAdded.connect(self._on_entries_changed)
            store.entryRemoved.connect(self._on_entries_changed)
            store.entryRetyped.connect(self._on_entries_changed)
            store.entriesReordered.connect(self._on_entries_changed)
            store.entryRenamed.connect(self._on_entry_renamed)
            store.batchFinished.connect(self._on_store_batch_finished)
        self._request_refresh()

    def _entry_types(self) -> dict[str, str]:
        """``{name: type}`` of the entries of the node's blackboard, in display order."""
        store = self._store
        if store is None or not shiboken6.isValid(store):
            return {}
        return store.types()

    def _request_refresh(self) -> None:
        """Refresh the editors now, or at the end of the blackboard batch in progress."""
        store = self._store
        if store is not None and shiboken6.isValid(store) and store.in_batch():
            self._refresh_due = True
            return
        self._refresh_due = False
        self._refresh_editors()

    def _on_entries_changed(self, *_args) -> None:
        if self._canvas is not None:  # not shown: nothing to refresh
            self._request_refresh()

    def _on_store_batch_finished(self) -> None:
        if self._refresh_due:
            self._refresh_due = False
            if self._canvas is not None:
                self._refresh_editors()

    def _on_entry_renamed(self, old: str, new: str) -> None:
        """Follow a renamed entry: the fields naming it name it by its new name (at once, also
        during a batch); the editors are refreshed as for any other change."""
        if self._canvas is None:
            return  # removed from its view: its fields are no longer part of a tree
        renamed = []
        with self._lock:
            for key in self.ENTRY_FIELDS:
                if self._fields.get(key) == old:
                    self._fields[key] = new
                    renamed.append(key)
        if renamed:
            runtime = self._is_runtime_change()
            for key in renamed:
                self.fieldChanged.emit(key)
            self.changed.emit(runtime)
        self._request_refresh()

    # ------------------------------------------------------------------ editors
    @override
    def _refresh_all_fields(self) -> None:
        self._refresh_editors()

    @override
    def _refresh_field_editor(self, key: str) -> None:
        self._refresh_editors()

    def _refresh_editors(self) -> None:
        """Show the node's choices and the blackboard's current entries in the editors (subclasses)."""
