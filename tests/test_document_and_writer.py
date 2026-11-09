"""TreeDocument (document.py) and BlackboardFileWriter (blackboard_file.py) on their own."""

from __future__ import annotations

import json
import os

import pytest

from behavior_tree_widget import document as document_module
from behavior_tree_widget.blackboard import BlackboardStore
from behavior_tree_widget.blackboard_file import BlackboardFileWriter
from behavior_tree_widget.canvas import TreeView
from behavior_tree_widget.config import TreeConfig
from behavior_tree_widget.document import TreeDocument
from behavior_tree_widget.nodes import CompositeNodeWidget, RootNodeWidget
from behavior_tree_widget.serialization import read_blackboard_file, read_tree_file
from behavior_tree_widget.tree_log import LogLevel

from conftest import Succeed


@pytest.fixture
def show(qtbot):
    """Show a document (holding only a Root) in a new view."""

    def make(document: TreeDocument) -> TreeView:
        view = TreeView(document=document)
        qtbot.addWidget(view)
        view.add_node(RootNodeWidget(), (0.0, 0.0))
        return view

    return make


def test_a_document_writes_its_tree_file(show, tmp_path):
    path = str(tmp_path / "tree_BT.json")
    document = TreeDocument(path, TreeConfig(tick_interval_ms=40))
    view = show(document)
    assert document.view is view and view.document is document
    assert document.folder == str(tmp_path) and view.base_folder == str(tmp_path)
    assert document.write() == []
    data = read_tree_file(path)
    assert data["config"]["tick_interval_ms"] == 40 and [node["type"] for node in data["nodes"]] == ["Root"]
    assert "blackboard" not in data
    assert document.path == path and not document.modified, "writing does not change the file state"


def test_a_document_writes_back_the_blackboard_section_of_an_old_file(show, tmp_path):
    entries = [{"name": "a", "type": "Integer", "value": 1}]
    document = TreeDocument(str(tmp_path / "old.json"), legacy_blackboard=entries)
    show(document)
    other = str(tmp_path / "copy.json")
    document.write(other)
    assert read_tree_file(other)["blackboard"] == entries


def test_a_document_without_a_file_or_a_view_cannot_be_written(show, tmp_path):
    with pytest.raises(ValueError):
        TreeDocument().write()
    with pytest.raises(RuntimeError):
        TreeDocument(str(tmp_path / "t_BT.json")).write()  # not shown: no positions to save


def test_data_too_deep_for_json_is_left_out_and_reported(show, tmp_path, monkeypatch):
    calls = []
    original = document_module.write_json_atomic

    def deep_once(path, data):
        calls.append(data)
        if len(calls) == 1:
            raise RecursionError("too deep")
        original(path, data)

    monkeypatch.setattr(document_module, "write_json_atomic", deep_once)
    document = TreeDocument(str(tmp_path / "t_BT.json"))
    show(document)
    document.write()
    assert len(calls) == 2, "written again, leaving out what json cannot write"


def test_the_document_holds_the_nodes_and_links_of_its_view(qtbot):
    view = TreeView()
    qtbot.addWidget(view)
    document = view.document
    root, sequence, leaf = RootNodeWidget(), CompositeNodeWidget(), Succeed()
    for x, node in enumerate((root, sequence, leaf)):
        view.add_node(node, (x * 200.0, x * 150.0))
    assert document.nodes() == view.nodes() == [root, sequence, leaf]
    assert document.root() is root and len(document) == 3 and leaf in document
    view.connect_nodes(root, sequence)
    view.connect_nodes(sequence, leaf)
    assert leaf.GetParent() is sequence and sequence.GetChildren() == [leaf]
    assert not document.can_connect(leaf, root), "a cycle"
    view.remove_node(sequence)
    assert document.nodes() == [root, leaf] and leaf.GetParent() is None
    assert document.number(root) < document.number(leaf) and document.number(sequence) == -1


def test_a_document_without_a_view_links_nodes(qtbot):
    document = TreeDocument()
    root, leaf, other = RootNodeWidget(), Succeed(), Succeed()
    for node in (root, leaf, other):
        qtbot.addWidget(node)
        document.add(node)
    with pytest.raises(ValueError, match="one root"):
        document.add(RootNodeWidget())
    with pytest.raises(ValueError, match="already"):
        document.add(leaf)
    assert document.can_connect(root, leaf) and not document.can_connect(leaf, other), "a leaf takes no children"
    document.link(root, leaf)
    assert root.GetChildren() == [leaf] and leaf.GetParent() is root
    assert document.unlink(leaf) is root and document.unlink(leaf) is None
    document.remove(root)
    assert document.root() is None
    with pytest.raises(ValueError):
        document.remove(root)


# ---------------------------------------------------------------------- the blackboard file writer
@pytest.fixture
def store(qapp):
    store = BlackboardStore()
    yield store
    store.dispose(notify=False)


@pytest.fixture
def writer(store, qtbot):
    reports = []
    writer = BlackboardFileWriter(store, lambda: 10_000, lambda level, message: reports.append((level, message)))
    writer.reports = reports
    return writer


def test_the_writer_writes_when_forced_or_changed(writer, store, tmp_path):
    path = str(tmp_path / "t_BB.json")
    written = []
    writer.written.connect(written.append)
    writer.adopt(path, file_holds_store=False)
    assert writer.is_active() and not writer.is_pending()
    store.add("a", "Integer", 1)
    assert writer.write(force=False) and written == [path]
    assert read_blackboard_file(path) == [{"name": "a", "type": "Integer", "value": 1}]
    assert writer.write(force=False) and written == [path], "unchanged: not written again"
    assert writer.write(force=True) and written == [path, path]


def test_changes_are_written_together_at_the_end_of_the_interval(writer, store, tmp_path, qtbot):
    path = str(tmp_path / "t_BB.json")
    writer.adopt(path, file_holds_store=False)
    writer.write(force=True)  # starts an interval of 10 s
    store.add("a", "Integer", 1)
    writer.schedule()
    store.set("a", 2)
    writer.schedule()
    assert writer.is_pending()
    assert read_blackboard_file(path) == []
    writer.flush()
    assert not writer.is_pending()
    assert read_blackboard_file(path) == [{"name": "a", "type": "Integer", "value": 2}]


def test_a_failed_write_is_reported_once_and_retried(writer, store, tmp_path):
    folder = tmp_path / "missing"
    path = str(folder / "t_BB.json")
    failures = []
    writer.failed.connect(failures.append)
    writer.adopt(path, file_holds_store=False)
    store.add("a", "Integer", 1)
    assert not writer.write(force=False)
    assert not writer.write(force=False)
    assert len(failures) == 2 and writer.error == failures[0]
    assert [level for level, _ in writer.reports] == [LogLevel.WARNING], "reported once"
    os.makedirs(folder)
    assert writer.write(force=False), "retried although nothing changed"
    assert writer.error is None
    assert writer.reports[-1][0] == LogLevel.INFO and "again" in writer.reports[-1][1]
    with open(path, encoding="utf-8") as stream:
        assert json.load(stream)["blackboard"][0]["name"] == "a"


def test_a_file_that_holds_the_store_is_not_rewritten(writer, store, tmp_path):
    path = str(tmp_path / "t_BB.json")
    store.add("a", "Integer", 1)
    writer.adopt(path, file_holds_store=True)
    assert writer.write(force=False)
    assert not os.path.exists(path)
