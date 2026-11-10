"""FloatSpinBox: the editor of Double entries, showing any float exactly (also nan and +/-inf)."""

from __future__ import annotations

import math
import sys
from typing import override

from PySide6.QtCore import QSignalBlocker
from PySide6.QtGui import QGuiApplication, QKeyEvent, QKeySequence, QValidator
from PySide6.QtWidgets import QDoubleSpinBox, QLineEdit, QWidget

__all__ = ["FloatSpinBox"]


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
        with QSignalBlocker(self):
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

    @override
    def stepBy(self, steps: int) -> None:
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

    @override
    def keyPressEvent(self, event: QKeyEvent) -> None:
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
    @override
    def textFromValue(self, value: float) -> str:
        if self._pending_exact is not None and value == self._exact_at:
            return repr(self._pending_exact)
        if self._exact is not None and (self._exact_at is None or value == self._exact_at):
            return repr(self._exact)
        value = float(value)
        if abs(value) >= _DBL_MAX and _parse_float_text(self.lineEdit().text()) == value:
            return repr(value)  # the largest float was typed, not infinity
        return repr(self._default_exact(value))

    @override
    def valueFromText(self, text: str) -> float:
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

    @override
    def validate(self, text: str, position: int):
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
