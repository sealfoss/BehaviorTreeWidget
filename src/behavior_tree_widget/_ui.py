"""Loading of Qt Designer ``.ui`` files shipped with the package (or supplied by users).

The packaged files are built from the Python forms that ``tools/compile_ui.py`` generates from
them (package :mod:`behavior_tree_widget._forms`): creating the widgets directly is several times
faster than parsing the XML. A packaged file whose content no longer matches its form (it was
edited and not compiled again) and every other file (e.g. a ``NodeWidget.UI_FILE`` path) are
loaded with one shared ``QUiLoader`` instead, so they still work.

Widgets are only created on the GUI thread: :func:`load_ui` is called there, so the shared loader
is never used by two threads at once.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import threading
from collections.abc import Callable
from importlib import resources

from PySide6 import QtWidgets
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import QWidget

from ._forms import FORMS, Form

__all__ = ["load_ui", "packaged_ui_names", "ui_hash"]

log = logging.getLogger("behavior_tree_widget")

_cache: dict[str, bytes] = {}
_cache_lock = threading.Lock()
#: The QUiLoader of every load that does not use a generated form (created on first use). Creating
#: a loader scans the plugin folders for Designer plugins, which costs as much as loading a form.
_loader: QUiLoader | None = None
#: Packaged file name -> function building its form, or None when the form cannot be used (see
#: _form_builder). Filled on first use, so each packaged file is hashed once per process.
_builders: dict[str, Callable[[QWidget | None], QWidget] | None] = {}


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


def ui_hash(data: bytes) -> str:
    """SHA-256 of a ``.ui`` file's content with ``\\r\\n`` read as ``\\n``, as ``_forms`` records it
    (so the hash does not depend on the line endings a checkout gave the file)."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def load_ui(name_or_path: str, parent: QWidget | None = None) -> QWidget:
    """Instantiate the widget described by a ``.ui`` file (GUI thread).

    Args:
        name_or_path: Either the file name of a ``.ui`` bundled with this package
            (e.g. ``"LeafNode.ui"``) or an absolute path to any ``.ui`` file.
        parent: Optional parent for the created widget.

    A bundled file is built from its generated form while that is up to date (see the module
    docs); the widget and its children are the same either way.
    """
    builder = _form_builder(name_or_path)
    if builder is not None:
        return builder(parent)
    return _load_with_loader(name_or_path, parent)


def _form_builder(name: str) -> Callable[[QWidget | None], QWidget] | None:
    """The function building the packaged form ``name``; None when ``name`` is not a packaged file
    with a generated form, or the file changed since its form was generated."""
    form = FORMS.get(name)
    if form is None:
        return None
    if name not in _builders:
        builder = None
        try:
            if ui_hash(_read_ui(name)) == form.sha256:
                builder = _make_builder(form)
            else:
                log.debug("%s changed since its form was generated; loading it with QUiLoader", name)
        except (OSError, ImportError, AttributeError):  # a missing file or generated module
            log.debug("the generated form of %s cannot be used; loading it with QUiLoader", name, exc_info=True)
        _builders[name] = builder
    return _builders[name]


def _make_builder(form: Form) -> Callable[[QWidget | None], QWidget]:
    module = importlib.import_module(f"{__package__}._forms.{form.module}")
    ui_class = getattr(module, form.ui_class)
    widget_class = getattr(QtWidgets, form.widget_class)

    def build(parent: QWidget | None) -> QWidget:
        widget = widget_class(parent)
        ui_class().setupUi(widget)
        return widget

    return build


def _load_with_loader(name_or_path: str, parent: QWidget | None) -> QWidget:
    global _loader
    data = _read_ui(name_or_path)
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError(f"Could not open ui data for {name_or_path!r}")
    if _loader is None:
        _loader = QUiLoader()
    try:
        widget = _loader.load(buffer, parent)
    except RuntimeError as error:  # PySide raises when the loader creates no widget
        raise RuntimeError(f"Could not load {name_or_path!r}: {_loader.errorString() or error}") from error
    finally:
        buffer.close()
    if widget is None:
        raise RuntimeError(f"Could not load {name_or_path!r}: {_loader.errorString()}")
    return widget
