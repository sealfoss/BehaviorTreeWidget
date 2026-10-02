"""Loading of Qt Designer ``.ui`` files shipped with the package (or supplied by users)."""

from __future__ import annotations

import os
import threading
from importlib import resources

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import QWidget

_cache: dict[str, bytes] = {}
_cache_lock = threading.Lock()


def packaged_ui_names() -> list[str]:
    """Names of the ``.ui`` files bundled in ``behavior_tree_widget/ui``."""
    folder = resources.files(__package__).joinpath("ui")
    return sorted(entry.name for entry in folder.iterdir() if entry.name.endswith(".ui"))


def _read_ui(name_or_path: str) -> bytes:
    with _cache_lock:
        cached = _cache.get(name_or_path)
    if cached is not None:
        return cached
    if os.path.isabs(name_or_path):
        with open(name_or_path, "rb") as stream:
            data = stream.read()
    else:
        resource = resources.files(__package__).joinpath("ui", name_or_path)
        if not resource.is_file():
            raise FileNotFoundError(f"No packaged ui file named {name_or_path!r}")
        data = resource.read_bytes()
    with _cache_lock:
        _cache[name_or_path] = data
    return data


def load_ui(name_or_path: str, parent: QWidget | None = None) -> QWidget:
    """Instantiate the widget described by a ``.ui`` file.

    Args:
        name_or_path: Either the file name of a ``.ui`` bundled with this package
            (e.g. ``"LeafNode.ui"``) or an absolute path to any ``.ui`` file.
        parent: Optional parent for the created widget.
    """
    data = _read_ui(name_or_path)
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError(f"Could not open ui data for {name_or_path!r}")
    loader = QUiLoader()
    try:
        widget = loader.load(buffer, parent)
    finally:
        buffer.close()
    if widget is None:
        raise RuntimeError(f"Could not load {name_or_path!r}: {loader.errorString()}")
    return widget
