"""Widgets that are dropped instead of closed are destroyed quietly."""

from __future__ import annotations

import gc

from PySide6.QtWidgets import QApplication

from behavior_tree_widget import BehaviorTreeWidget
from behavior_tree_widget.canvas import TreeView


def test_a_shown_widget_dropped_without_closing_is_destroyed_without_errors(qapp, capfd, tmp_path):
    for index in range(3):
        widget = BehaviorTreeWidget()
        widget.show()
        QApplication.processEvents()
        widget.NewTree(str(tmp_path / f"t{index}_BT.json"))
        widget.Shutdown()
        del widget
        gc.collect()  # Qt hides the views while the widget is destroyed
        QApplication.processEvents()
    assert "Error calling Python override" not in capfd.readouterr().err


def test_view_events_after_python_cleared_the_view_are_harmless(qtbot, capfd):
    # Python's cycle collector may clear the attributes of a view before Qt destroys the widget
    # holding it; Qt then hides the view, which calls its event handlers.
    view = TreeView()
    qtbot.addWidget(view)
    view.show()
    attributes = dict(view.__dict__)
    view.__dict__.clear()
    try:
        view.hide()
        view.setEnabled(False)
        view.clearFocus()
    finally:
        view.__dict__.update(attributes)
    assert "Error calling Python override" not in capfd.readouterr().err
