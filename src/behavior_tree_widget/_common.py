"""Constants and small helpers shared by several modules (internal)."""

from __future__ import annotations

import os

__all__ = ["INT_MAX", "INT_MIN", "TREE_FILE_FILTER", "file_key"]

#: Range of Integer blackboard entries and of ``int`` field editors (32 bit, like QSpinBox).
INT_MIN = -(2**31)
INT_MAX = 2**31 - 1

#: File dialog filter offering behavior tree files (``<name>_BT.json``) first.
TREE_FILE_FILTER = "Behavior Tree (*_BT.json);;JSON Files (*.json);;All Files (*)"


def file_key(path: str) -> str:
    """Identifies a file however its path is written (case, separators, ``..``, links)."""
    return os.path.normcase(os.path.realpath(path))
