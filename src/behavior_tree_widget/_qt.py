"""Small Qt helpers shared by the widgets (internal).

To change a widget without emitting its signals, use ``QSignalBlocker`` as a context
manager (``with QSignalBlocker(widget): ...``); it restores the previous state on exit.
"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QLineEdit

__all__ = ["set_text_quietly"]


def set_text_quietly(edit: QLineEdit, text: str) -> bool:
    """Show ``text`` in ``edit`` without emitting its signals. Returns True if the text changed.

    Unless the user is typing in it, the cursor goes to the start, so a long text shows its beginning.
    """
    if edit.text() == text:
        return False
    with QSignalBlocker(edit):
        edit.setText(text)
        if not edit.hasFocus():
            edit.setCursorPosition(0)
    return True
