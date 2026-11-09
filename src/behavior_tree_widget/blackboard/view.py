"""The Blackboard tab: :class:`BlackboardView` lists the entries of a :class:`BlackboardStore`, one
:class:`EntryRow` per entry, and the dialogs adding an entry and editing a container value."""

from __future__ import annotations

import logging
import sys
from typing import Any

import shiboken6
from PySide6.QtCore import QPoint, QSignalBlocker, Qt, Signal
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

from .._common import INT_MAX, INT_MIN
from .._qt import set_text_quietly
from .._ui import load_ui
from .float_spinbox import FloatSpinBox
from .store import BlackboardStore
from .values import (
    ENTRY_TYPES,
    _same_value,
    canonical_type,
    coerce_value,
    container_items,
    literal_text,
    parse_container_text,
    safe_repr,
)

__all__ = [
    "FLOAT_DECIMALS",
    "MOVE_DOWN_TOOLTIP",
    "MOVE_UP_TOOLTIP",
    "RESET_TOOLTIP",
    "AddEntryDialog",
    "BlackboardView",
    "EditContainerDialog",
    "EntryRow",
    "create_value_editor",
    "editor_value",
]

log = logging.getLogger("behavior_tree_widget")

#: Decimals of the Double editor in the Add Entry dialog (the Blackboard tab shows Doubles exactly).
FLOAT_DECIMALS = 8


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


RESET_TOOLTIP = (
    "Resets: at the end of every execution loop, this entry goes back to the value it had when the loop started"
)
MOVE_UP_TOOLTIP = "Move this entry up in the list"
MOVE_DOWN_TOOLTIP = "Move this entry down in the list"


class EntryRow(QWidget):
    """One blackboard entry: Select checkbox, name QLineEdit, value editor, Resets checkbox and
    the arrows moving the entry up and down in the list."""

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
        self.reset_box: QCheckBox | None = self._ui.findChild(QCheckBox, "Reset")
        if self.reset_box is None:  # a ui file without the Resets check box
            self.reset_box = QCheckBox("Resets", self._ui)
            self.reset_box.setObjectName("Reset")
            self._ui.layout().addWidget(self.reset_box)
        self.reset_box.setToolTip(RESET_TOOLTIP)
        self.move_up_button: QToolButton = self._move_button("MoveUp", Qt.ArrowType.UpArrow, MOVE_UP_TOOLTIP)
        self.move_down_button: QToolButton = self._move_button("MoveDown", Qt.ArrowType.DownArrow, MOVE_DOWN_TOOLTIP)
        self.move_up_button.clicked.connect(lambda _checked=False: self._on_move_clicked(-1))
        self.move_down_button.clicked.connect(lambda _checked=False: self._on_move_clicked(1))

        self.name_edit.setText(name)
        self.name_edit.setToolTip(f"{type_name} entry - edit to rename")
        self.name_edit.editingFinished.connect(self._on_name_edited)
        self.select_box.toggled.connect(self.selectionToggled)
        self.refresh_reset()
        self.reset_box.toggled.connect(self._on_reset_toggled)

        widget = self.value_widget
        if type_name == "Double" and isinstance(widget, QDoubleSpinBox) and not isinstance(widget, FloatSpinBox):
            # Show any float exactly (and nan/inf) instead of 8 fixed decimals.
            replacement = FloatSpinBox(widget.parentWidget())
            replacement.setObjectName(widget.objectName())
            replacement.setSizePolicy(widget.sizePolicy())
            replacement.setMinimumSize(widget.minimumSize())
            layout = widget.parentWidget().layout()
            layout.replaceWidget(widget, replacement)
            widget.hide()  # until it is deleted
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

    def _move_button(self, name: str, arrow: Qt.ArrowType, tooltip: str) -> QToolButton:
        """The arrow button ``name`` of the ui file (created when the file has none)."""
        button = self._ui.findChild(QToolButton, name)
        if button is None:
            button = QToolButton(self._ui)
            button.setObjectName(name)
            button.setArrowType(arrow)
            self._ui.layout().addWidget(button)
        if not button.toolTip():
            button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # a click must not end the editing of a value
        return button

    # ------------------------------------------------------------------ state
    def set_move_enabled(self, up: bool, down: bool) -> None:
        """Enable the arrows that can move the entry (not up from the top, not down from the bottom)."""
        self.move_up_button.setEnabled(up)
        self.move_down_button.setEnabled(down)

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
        for widget in (
            self.name_edit, self.value_widget, self.select_box, self.reset_box, self.move_up_button, self.move_down_button
        ):
            if widget is not None and shiboken6.isValid(widget):
                widget.blockSignals(True)

    def is_reset(self) -> bool:
        """True while the Reset check box is checked."""
        return self.reset_box.isChecked()

    def refresh_reset(self) -> None:
        """Show the store's Reset flag of the entry in the Reset check box."""
        try:
            reset = self._store.is_reset(self.name)
        except KeyError:
            return
        box = self.reset_box
        if box.isChecked() != reset:
            blocked = box.blockSignals(True)
            try:
                box.setChecked(reset)
            finally:
                box.blockSignals(blocked)

    def _on_move_clicked(self, offset: int) -> None:
        if self._is_current():
            self._view.move_entry(self.name, offset)

    def _on_reset_toggled(self, checked: bool) -> None:
        if not self._is_current():
            return
        try:
            self._store.set_reset(self.name, checked)
        except (KeyError, RuntimeError) as error:
            log.warning("could not change the Reset flag of blackboard entry %r: %s", self.name, error)
            self.refresh_reset()

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
        store.entryResetChanged.connect(self._on_entry_reset_changed)
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

    def move_entry(self, name: str, offset: int) -> int:
        """Move entry ``name`` ``offset`` places down the list (up when negative), as its arrows
        do. Returns its new index (unchanged at either end of the list; -1 if it failed)."""
        try:
            before = self._store.names().index(name)
            index = self._store.move(name, offset)
        except (KeyError, ValueError, RuntimeError) as error:
            log.warning("could not move blackboard entry %r: %s", name, error)
            return -1
        if index != before:
            self.entryEdited.emit(name)
        return index

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

    def _on_entry_reset_changed(self, name: str) -> None:
        row = self._rows.get(name)
        if row is not None and shiboken6.isValid(row):
            row.refresh_reset()

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
        if [widget for widget in current if widget is not None] != rows:
            for index, row in enumerate(rows):
                self._rows_layout.removeWidget(row)
                self._rows_layout.insertWidget(index, row)
        self._update_move_buttons()

    def _update_move_buttons(self) -> None:
        rows = self.rows()
        for index, row in enumerate(rows):
            if shiboken6.isValid(row):
                row.set_move_enabled(index > 0, index < len(rows) - 1)

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
                self._update_move_buttons()
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
