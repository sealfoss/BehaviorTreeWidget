"""Trees written by earlier versions load unchanged, and earlier versions keep the new nodes.

``data/tree_0_5_0.json`` was written by behavior_tree_widget 0.5.0 (every built-in node type
of that version and a leaf with fields of every kind). ``data/tree_0_6_0_resaved_by_0_5_0.json``
holds a UDP Message and a Notes node written by this version, then loaded and saved again by
0.5.0, which knew neither type (it kept them as placeholders).
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from behavior_tree_widget import (
    CompositeNodeWidget,
    EvaluationNodeWidget,
    LeafNodeWidget,
    NegationNodeWidget,
    NotesNodeWidget,
    PauseNodeWidget,
    RestartNodeWidget,
    SetNodeWidget,
    StopNodeWidget,
    UdpMessageNodeWidget,
    UnknownLeafNodeWidget,
    register_node_type,
)
from behavior_tree_widget import widget as widget_module

from conftest import AllFields, run_until_idle

DATA = Path(__file__).parent / "data"


def load(bt, tmp_path, name: str, caplog) -> dict:
    """Load a copy of data file ``name`` (no problems allowed) and return its JSON."""
    path = tmp_path / name
    shutil.copyfile(DATA / name, path)
    with caplog.at_level(logging.WARNING, logger="behavior_tree_widget"):
        assert bt.LoadTree(str(path))
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert not bt.IsModified()
    return json.loads(path.read_text(encoding="utf-8"))


def by_title(bt) -> dict:
    return {node.GetTitle(): node for node in bt.GetNodes()}


def test_a_tree_written_by_0_5_0_loads_unchanged(bt, tmp_path, caplog):
    load(bt, tmp_path, "tree_0_5_0.json", caplog)
    nodes = by_title(bt)
    root = bt.GetRootNode()
    assert root.GetCompositeType() == "Selector" and root.GetMemory()
    main = nodes["Main"]
    assert type(main) is CompositeNodeWidget and main.GetCompositeType() == "Sequence" and not main.GetMemory()
    negation = nodes["Negation"]
    assert type(negation) is NegationNodeWidget
    assert root.GetChildren() == [main, negation]
    fields, evaluation, setter = main.GetChildren()
    assert type(fields) is AllFields
    assert fields.GetFields() == {
        "count": 7, "ratio": 0.5, "label": "saved by 0.5.0", "enabled": True, "choice": ["red", "green", "blue"],
    }
    assert fields.GetFieldSelection("choice") == "blue"
    assert type(evaluation) is EvaluationNodeWidget and evaluation.GetValueName() == "counter"
    assert evaluation.GetLiteralValue() == 4
    assert type(setter) is SetNodeWidget and setter.GetValueName() == "speed" and setter.GetLiteralValue() == 2.5
    assert [type(child) for child in negation.GetChildren()] == [PauseNodeWidget]
    assert type(nodes["Restart"]) is RestartNodeWidget and nodes["Restart"].GetParent() is None
    assert type(nodes["Stop"]) is StopNodeWidget and nodes["Stop"].GetParent() is None
    assert not any(isinstance(node, (UnknownLeafNodeWidget, NotesNodeWidget)) for node in bt.GetNodes())
    assert [(name, bt.GetEntryType(name), bt.GetEntry(name)) for name in bt.GetEntryNames()] == [
        ("counter", "Integer", 4), ("speed", "Double", 1.5), ("names", "List", ["a", "b"]),
    ]
    assert bt.GetConfig().tick_interval_ms == 5 and not bt.GetLoopExecution()


def test_a_tree_written_by_0_5_0_is_saved_again_with_the_same_data(bt, tmp_path, caplog):
    original = load(bt, tmp_path, "tree_0_5_0.json", caplog)
    path = tmp_path / "resaved.json"
    assert bt.SaveTree(str(path))
    resaved = json.loads(path.read_text(encoding="utf-8"))
    for key in ("format", "version", "config", "connections", "blackboard"):
        assert resaved[key] == original[key], key
    assert sorted(resaved["nodes"], key=lambda node: node["id"]) == sorted(original["nodes"], key=lambda node: node["id"])


def test_a_tree_written_by_0_5_0_executes_as_before(qtbot, bt, tmp_path, caplog):
    load(bt, tmp_path, "tree_0_5_0.json", caplog)
    finished = []
    bt.executionFinished.connect(finished.append)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert finished == ["Succeeded"]  # Main succeeds, so the Selector root never reaches the Negation
    assert bt.GetEntry("speed") == 2.5


def test_new_nodes_kept_by_0_5_0_load_back(bt, tmp_path, caplog):
    data = load(bt, tmp_path, "tree_0_6_0_resaved_by_0_5_0.json", caplog)
    assert any(node.get("fields") == {} for node in data["nodes"] if node["type"] == "Notes")  # added by 0.5.0
    (udp,) = [node for node in bt.GetNodes() if isinstance(node, UdpMessageNodeWidget)]
    assert udp.GetParent() is bt.GetRootNode()
    assert udp.GetFields() == {"Destination IP": "192.168.1.20", "Destination Port": 9000, "Message": "hello é ✓"}
    (note,) = [node for node in bt.GetNodes() if isinstance(node, NotesNodeWidget)]
    assert note.GetText() == "First line\nSecond line é✓"
    assert note.GetNoteSize() == (300, 150)
    assert (note._item.pos().x(), note._item.pos().y()) == (150.0, 180.0)
    assert not any(isinstance(node, UnknownLeafNodeWidget) for node in bt.GetNodes())


# ============================================================================ applications that already use the new names
class Notes(LeafNodeWidget):
    """An application's own leaf type named like the built-in Notes node (legal in 0.5.0)."""

    _fields = {"recipient": "bob"}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class MyUdp(LeafNodeWidget):
    """An application's own leaf type with the type name of the built-in UDP Message node."""

    TYPE_NAME = "UDP Message"
    _fields = {"target": "robot"}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


def write_0_5_0_tree(path) -> None:
    """A tree as 0.5.0 saved it for an application with its own Notes and "UDP Message" leaf types."""
    data = {
        "format": "behavior_tree_widget",
        "version": 1,
        "nodes": [
            {"id": "root", "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True},
            {"id": "n", "type": "Notes", "title": "Notes", "x": -200.0, "y": 200.0, "fields": {"recipient": "alice"}},
            {"id": "u", "type": "UDP Message", "title": "MyUdp", "x": 200.0, "y": 200.0, "fields": {"target": "dock"}},
        ],
        "connections": [{"parent": "root", "child": "n"}, {"parent": "root", "child": "u"}],
        "blackboard": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def clean_registry():
    registry = widget_module._global_node_types
    saved = dict(registry)
    registry.clear()
    try:
        yield registry
    finally:
        registry.clear()
        registry.update(saved)


@pytest.mark.parametrize("how", ["node_types", "RegisterNodeType", "register_node_type"])
def test_an_application_type_named_like_a_new_built_in_keeps_its_name(make_widget, clean_registry, tmp_path, how):
    if how == "register_node_type":
        register_node_type(Notes)
        register_node_type(MyUdp)
        widget = make_widget(node_types=[AllFields])
    elif how == "RegisterNodeType":
        widget = make_widget(node_types=[AllFields])
        widget.RegisterNodeType(Notes)
        widget.RegisterNodeType(MyUdp)
    else:
        widget = make_widget(node_types=[AllFields, Notes, MyUdp])
    assert widget.GetNodeType("Notes") is Notes and widget.GetNodeType("UDP Message") is MyUdp
    names = widget.GetNodeTypes()
    assert names.count("Notes") == 1 and names.count("UDP Message") == 1
    entries = widget._node_menu_entries()
    assert sorted((group, name) for group, name, _ in entries if name in ("Notes", "UDP Message")) == [
        ("leaf", "Notes"), ("leaf", "UDP Message"),
    ]  # offered as the application's leaf types; the built-in nodes are not offered
    assert widget.NewTree(str(tmp_path / "new.json"))
    assert type(widget.AddNode("Notes", 0, 300)) is Notes
    assert type(widget.AddNode("UDP Message", 300, 300)) is MyUdp
    with pytest.raises(ValueError):
        widget.AddNode(NotesNodeWidget(), 600, 300)

    path = tmp_path / "old.json"
    write_0_5_0_tree(path)
    assert widget.LoadTree(str(path))
    note_leaf, udp_leaf = widget.GetRootNode().GetChildren()
    assert type(note_leaf) is Notes and note_leaf.GetFields() == {"recipient": "alice"}
    assert type(udp_leaf) is MyUdp and udp_leaf.GetFields() == {"target": "dock"}


def test_an_unregistered_application_type_named_like_a_new_built_in_becomes_a_placeholder(bt, tmp_path):
    path = tmp_path / "old.json"
    write_0_5_0_tree(path)
    assert bt.LoadTree(str(path))
    children = bt.GetRootNode().GetChildren()
    assert [type(child) for child in children] == [UnknownLeafNodeWidget, UnknownLeafNodeWidget]
    assert [child.GetTypeName() for child in children] == ["Notes", "UDP Message"]
    resaved = tmp_path / "resaved.json"
    assert bt.SaveTree(str(resaved))
    nodes = {node["id"]: node for node in json.loads(resaved.read_text(encoding="utf-8"))["nodes"]}
    assert nodes["n"]["fields"] == {"recipient": "alice"} and "text" not in nodes["n"]
    assert nodes["u"]["fields"] == {"target": "dock"}
    connections = json.loads(resaved.read_text(encoding="utf-8"))["connections"]
    assert {"parent": "root", "child": "n"} in connections and {"parent": "root", "child": "u"} in connections


def test_a_built_in_node_type_can_still_be_registered_again(bt):
    bt.RegisterNodeType(UdpMessageNodeWidget)  # as for the other built-in types: allowed, nothing changes
    assert bt.GetNodeType("UDP Message") is UdpMessageNodeWidget
    assert bt._node_menu_entries()[-1] == ("notes", "Notes", "Notes")
