"""FloatSpinBox: the editor of Double entries, showing any float exactly (also nan and +/-inf)."""

from __future__ import annotations

import math
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QValidator
from PySide6.QtTest import QTest

from behavior_tree_widget.blackboard import FloatSpinBox

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
