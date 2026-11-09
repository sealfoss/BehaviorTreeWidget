"""FloatSpinBox: the editor of Double entries, showing any float exactly (also nan and +/-inf)."""

from __future__ import annotations

import math
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QValidator
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDoubleSpinBox

from behavior_tree_widget.blackboard import FloatSpinBox

from conftest import assert_blackboard_in_file
from support.round6 import double_row
from support.round7 import untracked

DBL_MAX = sys.float_info.max
ACCEPTABLE = QValidator.State.Acceptable
INTERMEDIATE = QValidator.State.Intermediate
INVALID = QValidator.State.Invalid


@pytest.fixture
def box(qtbot):
    spin = FloatSpinBox()
    qtbot.addWidget(spin)
    spin.show()
    return spin


@pytest.fixture
def clipboard(qapp):
    board = QGuiApplication.clipboard()
    yield board
    board.clear()


def same_float(a: float, b: float) -> bool:
    """Equal values, with nan equal to nan and -0.0 different from 0.0."""
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)


def type_text(box: FloatSpinBox, text: str) -> None:
    """Replace the text of ``box`` by typing ``text``."""
    box.lineEdit().selectAll()
    QTest.keyClicks(box, text)


# ============================================================================ showing values
@pytest.mark.parametrize(
    "value",
    [0.1, 1 / 3, 1e-09, 123456789.123456789, -1.5e-300, 1e300, -0.0, DBL_MAX, -DBL_MAX,
     5e-324, 2.2250738585072014e-308, 1.5e-320],  # the smallest subnormal, the smallest normal, a subnormal
)
def test_set_float_shows_the_shortest_exact_text_and_keeps_the_value(box, value):
    box.set_float(value)
    assert box.lineEdit().text() == repr(value)
    assert same_float(box.float_value(), value)


@pytest.mark.parametrize(
    "value, text, qt_value",
    [(math.nan, "nan", 0.0), (math.inf, "inf", DBL_MAX), (-math.inf, "-inf", -DBL_MAX)],
)
def test_nan_and_infinities_are_shown_and_kept(box, value, text, qt_value):
    box.set_float(value)
    assert box.lineEdit().text() == text
    assert same_float(box.float_value(), value)
    assert box.value() == qt_value  # Qt's own copy of the value


def test_set_float_emits_no_signals(box):
    changes = []
    box.valueChanged.connect(changes.append)
    for value in (1.5, math.nan, math.inf, 1e-09, -0.0):
        box.set_float(value)
    assert changes == []


def test_steps_with_the_arrows_are_taken_as_values(box):
    box.setSingleStep(1.0)
    box.set_float(1.5)
    box.stepBy(1)
    assert box.float_value() == 2.5
    assert box.lineEdit().text() == "2.5"


# ============================================================================ typing
@pytest.mark.parametrize(
    "typed, expected",
    [("inf", math.inf), ("-inf", -math.inf), ("Infinity", math.inf), ("1e-09", 1e-09), ("-0.25", -0.25),
     ("1,5", 1.5), (repr(DBL_MAX), DBL_MAX)],
)
def test_typed_values_are_taken_exactly(box, typed, expected):
    box.set_float(3.0)
    changes = []
    box.valueChanged.connect(changes.append)
    type_text(box, typed)
    assert same_float(box.float_value(), expected)
    assert changes, "keyboard tracking is on: the value is reported while it is typed"


def undo(box: FloatSpinBox) -> None:
    QTest.keyClick(box, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)


def test_a_typed_comma_is_a_decimal_point_and_keeps_the_undo_history(box):
    box.set_float(3.0)
    type_text(box, "2,75")
    assert box.lineEdit().text() == "2.75"
    assert box.float_value() == 2.75
    undo(box)
    # Had the validator replaced the text (to turn the comma into a point), undo would stop at "2.".
    assert box.lineEdit().text() == "3.0"


def test_pasting_a_number_with_a_comma_is_one_undo_step(box, clipboard):
    box.set_float(0.0)
    clipboard.setText("2,25")
    box.lineEdit().selectAll()
    QTest.keyClick(box, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert box.lineEdit().text() == "2.25"
    assert box.float_value() == 2.25
    undo(box)
    assert box.lineEdit().text() == "0.0"


def test_nan_cannot_be_typed(box):
    box.set_float(1.0)
    type_text(box, "nan")
    assert not math.isnan(box.float_value())


def test_typing_over_nan_reports_a_value_equal_to_qts_copy_at_once(box):
    box.set_float(math.nan)  # Qt's own copy of nan is 0.0
    changes = []
    box.valueChanged.connect(changes.append)
    type_text(box, "0")
    assert changes == [0.0]
    assert same_float(box.float_value(), 0.0)


def test_without_keyboard_tracking_such_a_value_is_reported_when_editing_finishes(box):
    box.setKeyboardTracking(False)
    box.set_float(math.nan)
    changes = []
    box.valueChanged.connect(changes.append)
    type_text(box, "0")
    assert changes == []
    assert math.isnan(box.float_value()), "not taken before editing finishes"
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert changes == [0.0]
    assert same_float(box.float_value(), 0.0)


def test_without_keyboard_tracking_typing_reports_nothing_until_editing_finishes(box):
    box.setKeyboardTracking(False)
    box.set_float(1.0)
    changes = []
    box.valueChanged.connect(changes.append)
    type_text(box, "1e-09")
    assert changes == []
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert changes, "reported when editing finished"
    assert box.float_value() == 1e-09


def test_stepping_starts_from_the_typed_value(box):
    box.setKeyboardTracking(False)
    box.setSingleStep(1.0)
    box.set_float(1e-09)
    changes = []
    box.valueChanged.connect(changes.append)
    type_text(box, "5")
    assert changes == []
    box.stepBy(1)
    assert box.float_value() == 6.0
    assert box.lineEdit().text() == "6.0"
    assert changes[-1] == 6.0


def test_a_step_landing_on_qts_copy_of_the_old_value_is_not_taken_for_the_old_value(box):
    # nan is shown while Qt's own copy is 0.0; -1 + 1 is that copy, but the result is 0.0, not nan.
    box.setKeyboardTracking(False)
    box.setSingleStep(1.0)
    box.set_float(math.nan)
    type_text(box, "-1")
    box.stepBy(1)
    assert same_float(box.float_value(), 0.0)
    assert box.lineEdit().text() == "0.0"


# ============================================================================ validation
@pytest.mark.parametrize(
    "text, state",
    [
        ("1.5", ACCEPTABLE), ("-2", ACCEPTABLE), ("1e5", ACCEPTABLE), ("inf", ACCEPTABLE), ("-Infinity", ACCEPTABLE),
        ("1,5", ACCEPTABLE), (" 3 ", ACCEPTABLE),
        ("", INTERMEDIATE), ("-", INTERMEDIATE), ("+", INTERMEDIATE), (".", INTERMEDIATE), ("-.", INTERMEDIATE),
        ("1e", INTERMEDIATE), ("1e-", INTERMEDIATE), ("2E+", INTERMEDIATE), ("in", INTERMEDIATE),
        ("-infin", INTERMEDIATE),
        ("nan", INVALID), ("-nan", INVALID), ("abc", INVALID), ("1.2.3", INVALID), ("1e5x", INVALID),
    ],
)
def test_validate_states(box, text, state):
    assert box.validate(text, len(text))[0] == state


def test_validate_turns_a_comma_into_a_decimal_point(box):
    state, text, position = box.validate("1,5", 3)
    assert (state, text, position) == (ACCEPTABLE, "1.5", 3)


# ============================================================================ display robustness
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 1e-9, 12345.678])
def test_double_rows_show_special_and_tiny_values(widget, value):
    widget.SetEntry(value, "d")
    spin = widget.blackboard_view().row("d").value_widget
    assert isinstance(spin, QDoubleSpinBox) and isinstance(spin, FloatSpinBox)
    assert spin.text() == repr(value)
    stored = widget.GetEntry("d")
    assert stored == value or (math.isnan(stored) and math.isnan(value))

def test_nudging_a_tiny_double_keeps_it(widget):
    widget.SetEntry(1e-9, "tiny")
    spin = widget.blackboard_view().row("tiny").value_widget
    spin.stepUp()
    assert widget.GetEntry("tiny") == pytest.approx(1.000000001)


# ============================================================================ Double entry editor
def test_enter_on_a_nan_entry_keeps_it_and_does_not_modify_the_tree(widget, double_row):
    spin = double_row(math.nan)
    assert spin.text() == "nan"
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert math.isnan(widget.GetEntry("d"))
    assert spin.text() == "nan"
    assert not widget.IsModified()

@pytest.mark.parametrize("value", [2.5, math.inf, 5e-324, 0.1 + 0.2, DBL_MAX])
def test_enter_without_editing_does_not_modify_the_tree(widget, double_row, value):
    spin = double_row(value)
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert widget.GetEntry("d") == value
    assert spin.text() == repr(value)
    assert not widget.IsModified()

def test_typing_zero_over_nan_stores_zero(widget, double_row):
    spin = double_row(math.nan)
    spin.selectAll()
    QTest.keyClicks(spin, "0")
    assert widget.GetEntry("d") == 0.0
    assert_blackboard_in_file(widget)
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert widget.GetEntry("d") == 0.0 and spin.text() == "0.0"

def test_an_unfinished_edit_of_nan_goes_back_to_nan(widget, double_row):
    spin = double_row(math.nan)
    spin.selectAll()
    QTest.keyClicks(spin, "-")
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert math.isnan(widget.GetEntry("d")) and spin.text() == "nan"

def test_a_decimal_comma_is_accepted(widget, double_row):
    spin = double_row(1.0)
    spin.selectAll()
    QTest.keyClicks(spin, "2,5")
    assert widget.GetEntry("d") == 2.5
    assert spin.text() == "2.5"

@pytest.mark.parametrize(
    "typed, expected", [("5e-324", 5e-324), ("-inf", -math.inf), (repr(DBL_MAX), DBL_MAX), ("1e309", math.inf)]
)
def test_typed_values_are_stored_exactly(widget, double_row, typed, expected):
    spin = double_row(1.0)
    spin.selectAll()
    QTest.keyClicks(spin, typed)
    assert widget.GetEntry("d") == expected
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert widget.GetEntry("d") == expected
    assert spin.float_value() == expected

def test_typing_zero_over_a_subnormal_value_stores_zero(widget, double_row):
    spin = double_row(5e-324)
    assert spin.text() == "5e-324"
    spin.selectAll()
    QTest.keyClicks(spin, "0")
    assert widget.GetEntry("d") == 0.0

def test_negative_zero_is_a_change(widget, double_row):
    spin = double_row(0.0)
    spin.selectAll()
    QTest.keyClicks(spin, "-0")
    stored = widget.GetEntry("d")
    assert stored == 0.0 and math.copysign(1.0, stored) == -1.0

def test_float_spin_box_without_keyboard_tracking_reports_on_enter(qtbot):
    spin = FloatSpinBox()
    qtbot.addWidget(spin)
    spin.setKeyboardTracking(False)
    spin.set_float(math.nan)
    spin.show()
    spin.activateWindow()
    qtbot.waitUntil(spin.isActiveWindow, timeout=2000)
    spin.setFocus()
    reported = []
    spin.valueChanged.connect(lambda _value: reported.append(spin.float_value()))
    spin.selectAll()
    QTest.keyClicks(spin, "0")
    assert reported == []
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert reported == [0.0] and spin.text() == "0.0"


# ============================================================================ Double editor
def test_undo_back_to_nan_stores_nan(widget, double_row):
    spin = double_row(math.nan)
    spin.selectAll()
    QTest.keyClicks(spin, "5")
    assert widget.GetEntry("d") == 5.0
    QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "nan"
    assert math.isnan(widget.GetEntry("d"))
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert spin.text() == "nan" and math.isnan(widget.GetEntry("d"))
    widget.SetEntry(5.0, "d")
    assert spin.text() == "5.0"

@pytest.mark.parametrize("start, typed", [(math.nan, "0"), (1.0, "1.0000000000001"), (5e-324, "0"), (0.0, "-0")])
def test_an_abandoned_edit_without_keyboard_tracking_reverts(untracked, start, typed):
    spin, reported = untracked(start)
    spin.selectAll()
    QTest.keyClicks(spin, typed)
    assert reported == []
    spin.selectAll()
    QTest.keyClicks(spin, "-")  # unfinished
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert reported == []
    assert spin.text() == repr(start)
    value = spin.float_value()
    assert (math.isnan(value) and math.isnan(start)) or (value == start and repr(value) == repr(start))

def test_a_finished_edit_without_keyboard_tracking_is_reported_once(untracked):
    spin, reported = untracked(math.nan)
    spin.selectAll()
    QTest.keyClicks(spin, "0")
    assert math.isnan(spin.float_value()), "not committed before Enter"
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert reported == [0.0] and spin.text() == "0.0"


# ============================================================================ Double editor
@pytest.mark.parametrize("start", [math.nan, 5e-324, -0.0])
@pytest.mark.parametrize("typed, key", [("1", Qt.Key.Key_Down), ("-1", Qt.Key.Key_Up), ("10", Qt.Key.Key_PageDown)])
def test_stepping_from_a_typed_value_without_keyboard_tracking(untracked, start, typed, key):
    spin, reported = untracked(start)
    spin.selectAll()
    QTest.keyClicks(spin, typed)
    QTest.keyClick(spin, key)
    assert spin.text() == "0.0" and spin.float_value() == 0.0
    assert reported == [0.0]
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert spin.text() == "0.0" and reported == [0.0]

@pytest.mark.parametrize("value", [5e-324, 1.5e-323, sys.float_info.min, 1.0, math.nan])
def test_enter_without_editing_reports_nothing_without_keyboard_tracking(untracked, value):
    spin, reported = untracked(value)
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert reported == []
    assert spin.text() == repr(value)

def test_undo_after_typing_a_decimal_comma(widget, double_row):
    spin = double_row(7.0)
    spin.selectAll()
    QTest.keyClicks(spin, "2,5")
    assert widget.GetEntry("d") == 2.5 and spin.text() == "2.5"
    for _ in range(3):
        QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "7.0" and widget.GetEntry("d") == 7.0

def test_undo_after_pasting_a_decimal_comma_brings_nan_back(widget, double_row):
    spin = double_row(math.nan)
    QGuiApplication.clipboard().setText(" 2,5 ")
    spin.selectAll()
    QTest.keyClick(spin, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert widget.GetEntry("d") == 2.5
    QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "nan" and math.isnan(widget.GetEntry("d"))


# ============================================================================ Double editor: pasting
def test_a_pasted_decimal_comma_is_its_own_undo_step(qtbot, widget):
    widget.SetEntry(7.0, "d")
    widget.setCurrentIndex(1)
    widget.activateWindow()
    qtbot.waitUntil(widget.isActiveWindow, timeout=2000)
    spin = widget.blackboard_view().row("d").value_widget
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus, timeout=1000)
    QGuiApplication.clipboard().setText(",5")
    spin.selectAll()
    QTest.keyClicks(spin, "1")
    QTest.keyClick(spin, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "1.5" and widget.GetEntry("d") == 1.5
    QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "1" and widget.GetEntry("d") == 1.0
