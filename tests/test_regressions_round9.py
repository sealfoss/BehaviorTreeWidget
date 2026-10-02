"""Regression tests for the ninth fix pass (Stop/Pause from an event loop opened by a
post-tick handler while runs are chained, values left out when saving from the
unsaved-changes prompt, and undo steps of a pasted decimal comma)."""

from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from behavior_tree_widget import LeafNodeWidget

from conftest import fast_config


class Quick(LeafNodeWidget):
    TYPE_NAME = "R9Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


@pytest.fixture
def widget(bt):
    bt.RegisterNodeType(Quick)
    return bt


@pytest.mark.parametrize("command", ["Stop", "Pause"])
@pytest.mark.parametrize("source", ["timer", "thread"])
def test_a_command_delivered_inside_a_dialog_opened_by_a_chaining_handler(qtbot, widget, command, source):
    widget.Connect(widget.GetRootNode(), widget.AddNode(Quick, 0, 220))
    fast_config(widget)
    runs = []
    chain = {"on": True}

    def issue():
        if source == "timer":
            getattr(widget, command)()
        else:
            thread = threading.Thread(target=getattr(widget, command), daemon=True)
            thread.start()
            thread.join(5)

    def on_finished(result):
        runs.append(result)
        if not chain["on"]:
            return
        widget.Execute()
        if len(runs) == 3:  # a "modal dialog": an event loop opened by the handler
            loop = QEventLoop()
            QTimer.singleShot(100, issue)
            QTimer.singleShot(300, loop.quit)
            loop.exec()

    widget.executionFinished.connect(on_finished)
    widget.Execute()
    expected = "Idle" if command == "Stop" else "Paused"
    qtbot.waitUntil(lambda: widget.GetExecutionState() == expected, timeout=3000)
    count = len(runs)
    qtbot.wait(100)
    assert len(runs) == count and widget.GetExecutionState() == expected
    chain["on"] = False
    widget.Stop()


def test_saving_from_the_unsaved_changes_prompt_reports_what_was_left_out(widget, dialogs):
    widget.SetEntry([object()], "unsaveable")
    assert widget.IsModified()
    dialogs.question_answer = QMessageBox.StandardButton.Save
    assert widget.ConfirmDiscardChanges()
    assert not widget.IsModified()
    assert dialogs.kinds()[-2:] == ["question", "warning"]
    assert "unsaveable" in str(dialogs.shown[-1][1])


def test_a_pasted_decimal_comma_is_its_own_undo_step(qtbot, widget):
    widget.SetEntry(7.0, "d")
    widget.setCurrentIndex(1)
    widget.activateWindow()
    qtbot.waitUntil(widget.isActiveWindow, timeout=2000)
    spin = widget.blackboardView().row("d").value_widget
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus, timeout=1000)
    QGuiApplication.clipboard().setText(",5")
    spin.selectAll()
    QTest.keyClicks(spin, "1")
    QTest.keyClick(spin, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "1.5" and widget.GetEntry("d") == 1.5
    QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert spin.text() == "1" and widget.GetEntry("d") == 1.0
