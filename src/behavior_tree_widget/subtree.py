"""The Sub-Tree node: runs a complete behavior tree stored in another file.

A Sub-Tree node is a leaf of the tree it is part of. Its line edit holds the file name of
a behavior tree file (``<name>_BT.json``); a relative name is relative to the folder of
the tree file the node belongs to. Each time the tree is executed, that file is read and
its tree (its Root with everything connected below it) is built behind the scenes, as a
part of the running tree: the Sub-Tree node runs while that tree runs, and succeeds or
fails when its Root succeeds or fails.

The sub-tree has no blackboard of its own: its nodes use the blackboard of the tree being
executed (the sub-tree file's ``_BB.json``, if any, is not loaded). The nodes run are not
shown; Pause, Stop and Restart nodes in it act on the tree being executed.

The node's "Zoom In" menu item (``BehaviorTreeWidget.ZoomIn``) shows the sub-tree in place of
the tree, to look at or edit; a sub-tree shown that way is run as shown, and its nodes show
the status of the nodes run for the Sub-Tree node it was zoomed into from.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QFileDialog, QLineEdit, QToolButton, QWidget

from ._common import TREE_FILE_FILTER
from ._qt import set_text_quietly
from ._runctx import check_not_cancelled
from .nodes import LeafNodeWidget
from .serialization import tree_file_paths

if TYPE_CHECKING:  # pragma: no cover
    pass

log = logging.getLogger("behavior_tree_widget")

__all__ = ["SUB_TREE", "SubTreeNodeWidget"]

SUB_TREE = "Sub-Tree"


class SubTreeNodeWidget(LeafNodeWidget):
    """Runs the behavior tree stored in its ``File`` field (see the module docs).

    ``GetFile`` / ``SetFile`` read and change the file name shown in the line edit (the
    ``"File"`` field, saved with the tree); ``GetResolvedFile`` returns the absolute path
    that is executed. The browse button (``...``) chooses the file with a dialog.
    """

    UI_FILE = "SubTreeNode.ui"
    TYPE_NAME = SUB_TREE
    _title = SUB_TREE
    FILE_FIELD = "File"
    _fields = {FILE_FIELD: ""}
    RUN_IN_THREAD = False
    FILE_FILTER = TREE_FILE_FILTER
    _file_edit: QLineEdit | None = None  # until __init__ finds it (field hooks run before)

    def __init__(self, parent: QWidget | None = None):
        # Folder that a relative file name is relative to, for Sub-Tree nodes inside a sub-tree
        # (their own file's folder). None: the folder of the tree file of the BehaviorTreeWidget.
        self._base_dir: str | None = None
        super().__init__(parent)
        self._file_edit: QLineEdit = self._require_child(QLineEdit, "SubTreeFile")
        self._browse_button: QToolButton = self._require_child(QToolButton, "BrowseSubTree")
        self._file_edit.textEdited.connect(self._on_file_typed)
        self._browse_button.clicked.connect(self.browse)
        self._sync_file_editor()
        self._notify_geometry()

    def _title_tooltip(self) -> str:
        return f"{SUB_TREE} - runs the behavior tree in File: succeeds when it succeeds, fails when it fails"

    def DisplayTitle(self) -> str:
        title = self.GetTitle()
        return title if title == SUB_TREE else f"{title} ({SUB_TREE})"

    def OnRun(self, tree):
        # The executor runs the sub-tree itself (see execution.TreeExecutor._build).
        raise RuntimeError("a Sub-Tree node runs the tree in its file; it cannot run on its own")

    # ------------------------------------------------------------------ file
    def GetFile(self) -> str:
        """The file name in the line edit (``""`` until a file is chosen)."""
        with self._lock:
            value = self._fields.get(self.FILE_FIELD)
        return value if isinstance(value, str) else ""

    def SetFile(self, path: str) -> None:
        """Change the file name (thread-safe). A relative name is relative to the folder of the tree file."""
        check_not_cancelled("SetFile")
        self.SetField(self.FILE_FIELD, os.fspath(path))

    def base_folder(self) -> str:
        """The folder a relative file name is relative to: the folder of the file of the tree the
        node is part of (a sub-tree's own file for the nodes of a sub-tree)."""
        if self._base_dir:
            return self._base_dir
        canvas = self._canvas
        folder = getattr(canvas, "base_folder", None) if canvas is not None else None
        if folder:
            return folder  # a node of a sub-tree shown with Zoom In
        tree = self._tree
        path = tree.GetFilePath() if tree is not None else None
        return os.path.dirname(path) if path else os.getcwd()

    def GetResolvedFile(self) -> str | None:
        """The absolute path of the tree file to run (None while no file is chosen).

        A name that does not exist as typed is also looked for as ``<tree name>_BT.json``,
        so ``patrol`` and ``patrol.json`` find ``patrol_BT.json``.
        """
        name = self.GetFile().strip()
        if not name:
            return None
        path = os.path.normpath(os.path.join(self.base_folder(), os.path.expanduser(name)))
        if not os.path.isfile(path):
            tree_path = tree_file_paths(path)[0]
            if os.path.isfile(tree_path):
                return tree_path
        return path

    def relative_name(self, path: str) -> str:
        """``path`` relative to :meth:`base_folder` if it lies in that folder (or below), else absolute."""
        path = os.path.abspath(path)
        try:
            relative = os.path.relpath(path, self.base_folder())
        except ValueError:  # e.g. another drive on Windows
            return path
        return path if relative == os.pardir or relative.startswith(os.pardir + os.sep) else relative

    def browse(self) -> bool:
        """Choose the file with a file dialog (the browse button). Returns True if a file was chosen."""
        current = self.GetResolvedFile()
        start = os.path.dirname(current) if current else self.base_folder()
        tree = self._tree
        path, _ = QFileDialog.getOpenFileName(tree, "Choose Sub-Tree", start, self.FILE_FILTER)
        if not path:
            return False
        self._set_file_from_user(self.relative_name(path))
        return True

    def file_editor(self) -> QLineEdit:
        """The line edit showing the file name (for tests and customisation)."""
        return self._file_edit

    def browse_button(self) -> QToolButton:
        """The ``...`` button choosing the file (for tests and customisation)."""
        return self._browse_button

    def _set_file_from_user(self, name: str) -> None:
        if name == self.GetFile():
            return
        self._on_editor_value(self.FILE_FIELD, name)  # a user edit (also while the tree executes)
        self._sync_file_editor()

    def _on_file_typed(self, text: str) -> None:
        self._on_editor_value(self.FILE_FIELD, text)
        self._sync_tooltip()

    def _sync_file_editor(self) -> None:
        edit = self._file_edit
        if edit is None:
            return
        set_text_quietly(edit, self.GetFile())
        self._sync_tooltip()

    def _sync_tooltip(self) -> None:
        edit = self._file_edit
        if edit is None:
            return
        try:
            path = self.GetResolvedFile()
        except (OSError, ValueError):
            path = None
        edit.setToolTip(path or "The behavior tree file (<name>_BT.json) this node runs")

    # The "File" field is shown in the line edit instead of the generic field editors.
    def _refresh_all_fields(self) -> None:
        self._sync_file_editor()

    def _refresh_field_editor(self, key: str) -> None:
        self._sync_file_editor()

    def _on_added_to_tree(self) -> None:
        self._sync_tooltip()  # relative names now resolve against the tree file's folder

    def _load_dict(self, data: dict) -> None:
        super()._load_dict(data)
        self._sync_file_editor()
