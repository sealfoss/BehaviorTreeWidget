"""The generated forms of the packaged .ui files (tools/compile_ui.py) and how load_ui uses them."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QObject
from PySide6.QtGui import QCursor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import QBoxLayout, QGridLayout, QLayout, QSpacerItem, QWidget

import behavior_tree_widget._ui as ui_module
from behavior_tree_widget._forms import FORMS
from behavior_tree_widget._ui import _load_with_loader, _make_builder, _read_ui, load_ui, packaged_ui_names, ui_hash

ROOT = Path(__file__).resolve().parent.parent
COMPILE_SCRIPT = ROOT / "tools" / "compile_ui.py"
RECOMPILE = "run python tools/compile_ui.py to generate the forms again"


# ============================================================================ the forms are up to date
def test_every_packaged_ui_file_has_an_up_to_date_form():
    names = packaged_ui_names()
    assert names, "no packaged .ui files found"
    missing = [name for name in names if name not in FORMS]
    assert not missing, f"no generated form for {missing}: {RECOMPILE}"
    stale = [name for name in names if ui_hash(_read_ui(name)) != FORMS[name].sha256]
    assert not stale, f"{stale} changed since their forms were generated: {RECOMPILE}"
    orphans = sorted(set(FORMS) - set(names))
    assert not orphans, f"forms of .ui files that no longer exist: {orphans}: {RECOMPILE}"


@pytest.mark.parametrize("name", sorted(FORMS))
def test_every_form_module_defines_its_ui_class(name):
    form = FORMS[name]
    module = importlib.import_module(f"behavior_tree_widget._forms.{form.module}")
    assert hasattr(getattr(module, form.ui_class), "setupUi")


def test_the_hash_ignores_line_endings():
    assert ui_hash(b"<ui>\r\n<widget/>\r\n</ui>\r\n") == ui_hash(b"<ui>\n<widget/>\n</ui>\n")
    assert ui_hash(b"<ui>a</ui>") != ui_hash(b"<ui>b</ui>")


def _compile_script():
    spec = importlib.util.spec_from_file_location("compile_ui_script", COMPILE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_compile_script_hashes_as_load_ui_does():
    script = _compile_script()
    for data in (b"<ui>\r\n</ui>\r\n", b"<ui>\n</ui>\n", "<ui>ä</ui>".encode()):
        assert script.ui_hash(data) == ui_hash(data)


def test_the_compile_script_check_passes():
    result = subprocess.run(
        [sys.executable, str(COMPILE_SCRIPT), "--check"],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{len(FORMS)} forms are up to date" in result.stdout


# ============================================================================ forms build what QUiLoader builds
#: Dynamic properties only QUiLoader leaves behind: PySide's pointer bookkeeping on every object ...
LOADER_ONLY_DYNAMIC_PROPERTIES = {"_PySideInvalidatePtr"}
#: ... and the margins and grid spacings QFormBuilder also stores on layouts (it applies them with
#: setContentsMargins / setHorizontalSpacing / setVerticalSpacing too, which are compared through
#: the contentsMargins property and the spacings in _describe). Nothing in the package reads them.
LOADER_ONLY_LAYOUT_PROPERTIES = {
    "leftMargin", "topMargin", "rightMargin", "bottomMargin", "horizontalSpacing", "verticalSpacing",
}


def _value(value):
    """A comparable form of a property value."""
    if isinstance(value, QIcon):
        return ("QIcon", value.isNull())
    if isinstance(value, QCursor):  # no value equality in PySide
        return ("QCursor", value.shape())
    if isinstance(value, (QPixmap, QImage)):  # no value equality in PySide
        return (type(value).__name__, value.isNull(), value.size())
    if isinstance(value, QObject):  # e.g. a buddy or a parent widget: compare by name
        return (type(value).__name__, value.objectName())
    return value


def _read(obj: QObject, name: str):
    try:
        return _value(obj.property(name))
    except RuntimeError as error:  # a type PySide cannot convert: compared by its error
        return ("unreadable", str(error))


def _properties(obj: QObject) -> dict:
    meta = obj.metaObject()
    result = {}
    for index in range(meta.propertyCount()):
        prop = meta.property(index)
        if prop.isReadable():
            result[prop.name()] = _read(obj, prop.name())
    loader_only = LOADER_ONLY_DYNAMIC_PROPERTIES
    if isinstance(obj, QLayout):
        loader_only = loader_only | LOADER_ONLY_LAYOUT_PROPERTIES
    for name in obj.dynamicPropertyNames():
        name = bytes(name).decode()
        # _q_...: Qt's own bookkeeping, not set by the form.
        if not name.startswith("_q_") and name not in loader_only:
            result[f"dynamic:{name}"] = _read(obj, name)
    return result


def _layout_items(layout: QLayout) -> list:
    items = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is not None:
            entry = ("widget", item.widget().objectName())
        elif item.layout() is not None:
            entry = ("layout", item.layout().objectName())
        elif isinstance(item, QSpacerItem) or item.spacerItem() is not None:
            spacer = item.spacerItem()
            policy = spacer.sizePolicy()
            entry = ("spacer", spacer.sizeHint(), policy.horizontalPolicy(), policy.verticalPolicy())
        else:
            entry = ("item", type(item).__name__)
        if isinstance(layout, QBoxLayout):
            entry += (layout.stretch(index),)
        elif isinstance(layout, QGridLayout):
            entry += (layout.getItemPosition(index),)
        entry += (item.alignment(),)
        items.append(entry)
    return items


def _describe(obj: QObject) -> dict:
    """Class, name, every property and the children (widgets and layouts) of ``obj``, recursively."""
    description = {"class": obj.metaObject().className(), "name": obj.objectName(), "properties": _properties(obj)}
    if isinstance(obj, QLayout):
        description["items"] = _layout_items(obj)
        if isinstance(obj, QGridLayout):
            description["spacing"] = (obj.horizontalSpacing(), obj.verticalSpacing())
    children = [child for child in obj.children() if isinstance(child, (QWidget, QLayout))]
    description["children"] = sorted((_describe(child) for child in children), key=lambda d: (d["class"], d["name"]))
    return description


def _differences(left, right, path="") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        found = []
        for key in sorted(set(left) | set(right), key=str):
            if key not in left or key not in right:
                found.append(f"{path}/{key}: only in {'form' if key in left else 'QUiLoader'}")
            else:
                found += _differences(left[key], right[key], f"{path}/{key}")
        return found
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [d for index, (a, b) in enumerate(zip(left, right)) for d in _differences(a, b, f"{path}[{index}]")]
    return [] if left == right else [f"{path}: form {left!r} != QUiLoader {right!r}"]


@pytest.mark.parametrize("name", packaged_ui_names())
def test_a_form_builds_the_same_widgets_as_quiloader(qapp, name):
    from_form = _make_builder(FORMS[name])(None)
    from_loader = _load_with_loader(name, None)
    try:
        assert type(from_form) is type(from_loader)
        assert from_form.metaObject().className() == FORMS[name].widget_class
        differences = _differences(_describe(from_form), _describe(from_loader))
        assert not differences, "\n".join(differences[:40])
    finally:
        from_form.deleteLater()
        from_loader.deleteLater()


def test_the_widget_gets_the_parent(qapp):
    parent = QWidget()
    try:
        widget = load_ui("LeafNode.ui", parent)
        assert widget.parent() is parent
        assert parent.findChild(QWidget, "ParentConnection") is not None
    finally:
        parent.deleteLater()


# ============================================================================ which way load_ui takes
@pytest.fixture
def loads(monkeypatch):
    """Records the files load_ui hands to QUiLoader; forms are looked up afresh."""
    calls: list[str] = []
    original = ui_module._load_with_loader

    def spy(name_or_path, parent):
        calls.append(name_or_path)
        return original(name_or_path, parent)

    monkeypatch.setattr(ui_module, "_load_with_loader", spy)
    monkeypatch.setattr(ui_module, "_builders", {})
    return calls


def test_packaged_files_are_built_from_their_forms(qapp, loads):
    for name in packaged_ui_names():
        load_ui(name).deleteLater()
    assert loads == []


def test_a_packaged_file_changed_since_its_form_was_generated_is_loaded_with_quiloader(qapp, loads, monkeypatch):
    original = _read_ui("LeafNode.ui")
    marker = b'<widget class="QFrame" name="FrameStatus">'
    assert marker in original
    edited = original.replace(
        marker, b'<widget class="QLabel" name="AddedInDesigner"/></item><item>' + marker, 1
    )
    monkeypatch.setitem(ui_module._cache, "LeafNode.ui", edited)  # as if the file was edited
    widget = load_ui("LeafNode.ui")
    try:
        assert loads == ["LeafNode.ui"]
        assert widget.findChild(QWidget, "AddedInDesigner") is not None, "the edited file is what is shown"
        assert widget.findChild(QWidget, "Title") is not None
    finally:
        widget.deleteLater()
    load_ui("NegationNode.ui").deleteLater()  # other packaged files still use their forms
    assert loads == ["LeafNode.ui"]


def test_absolute_paths_are_loaded_with_quiloader(qapp, loads, tmp_path):
    path = tmp_path / "LeafNode.ui"  # a file with the name of a packaged one
    path.write_bytes(_read_ui("LeafNode.ui"))
    widget = load_ui(str(path))
    try:
        assert loads == [str(path)]
        assert widget.findChild(QWidget, "Title") is not None
    finally:
        widget.deleteLater()


def test_unknown_packaged_names_still_raise(qapp, loads):
    with pytest.raises(FileNotFoundError):
        load_ui("NoSuchForm.ui")


def test_a_form_whose_module_is_missing_falls_back_to_quiloader(qapp, loads, monkeypatch):
    broken = dict(FORMS)
    broken["LeafNode.ui"] = FORMS["LeafNode.ui"]._replace(module="ui_DoesNotExist")
    monkeypatch.setattr(ui_module, "FORMS", broken)
    widget = load_ui("LeafNode.ui")
    try:
        assert loads == ["LeafNode.ui"]
        assert widget.findChild(QWidget, "Title") is not None
    finally:
        widget.deleteLater()


def test_one_quiloader_serves_every_load(qapp, monkeypatch, tmp_path):
    created = []
    real = ui_module.QUiLoader

    class CountingLoader(real):
        def __init__(self, *args):
            super().__init__(*args)
            created.append(self)

    monkeypatch.setattr(ui_module, "QUiLoader", CountingLoader)
    monkeypatch.setattr(ui_module, "_loader", None)
    paths = []
    for name in ("LeafNode.ui", "RootNode.ui", "BoolEntry.ui"):
        path = tmp_path / name
        path.write_bytes(_read_ui(name))
        paths.append(str(path))
    for path in paths * 2:
        load_ui(path).deleteLater()
    assert len(created) == 1


def test_a_failed_load_names_the_file_and_the_shared_loader_keeps_working(qapp, tmp_path):
    path = tmp_path / "Broken.ui"
    path.write_bytes(b"<ui version='4.0'><widget class='NoSuchWidgetClass' name='X'/></ui>")
    with pytest.raises(RuntimeError, match="Broken.ui"):
        load_ui(str(path))
    good = tmp_path / "LeafNode.ui"
    good.write_bytes(_read_ui("LeafNode.ui"))
    widget = load_ui(str(good))  # the next load through the same QUiLoader
    try:
        assert widget.findChild(QWidget, "Title") is not None
    finally:
        widget.deleteLater()


def test_node_widgets_are_built_from_forms(qapp, loads):
    from behavior_tree_widget import (
        GroupNodeWidget,
        NegationNodeWidget,
        NotesNodeWidget,
        PrintMessageNodeWidget,
        RootNodeWidget,
        SubTreeNodeWidget,
    )

    nodes = [cls() for cls in (RootNodeWidget, NegationNodeWidget, NotesNodeWidget, GroupNodeWidget,
                               PrintMessageNodeWidget, SubTreeNodeWidget)]
    try:
        assert loads == []
        assert all(node._title_label is not None for node in nodes)
    finally:
        for node in nodes:
            node.deleteLater()


@pytest.mark.skipif(os.environ.get("BTW_SKIP_UIC") == "1", reason="pyside6-uic not wanted")
def test_generated_forms_match_what_uic_generates_now(tmp_path):
    """The committed forms are what pyside6-uic generates from the .ui files (ignoring the header
    comment, which names the uic version)."""
    script = _compile_script()
    try:
        uic = script.find_uic()
    except SystemExit:
        pytest.skip("pyside6-uic is not installed")
    forms_folder = ROOT / "src" / "behavior_tree_widget" / "_forms"
    for name, form in sorted(FORMS.items()):
        target = tmp_path / f"{form.module}.py"
        assert script.compile_ui(uic, ROOT / "src" / "behavior_tree_widget" / "ui" / name, target) == form.ui_class

        def body(text: str) -> str:
            return "\n".join(line for line in text.splitlines() if not line.startswith("##"))

        committed = (forms_folder / f"{form.module}.py").read_text(encoding="utf-8")
        assert body(target.read_text(encoding="utf-8")) == body(committed), f"{name}: {RECOMPILE}"
