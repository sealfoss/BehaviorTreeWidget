"""The node type registry (registry.py): built-in kinds, registered leaf types, saved data."""

from __future__ import annotations

import pytest

from behavior_tree_widget import LeafNodeWidget, UnknownLeafNodeWidget
from behavior_tree_widget.blackboard_aware import BlackboardAwareLeaf
from behavior_tree_widget.blackboard_nodes import BlackboardValueNodeWidget
from behavior_tree_widget.groups import GroupNodeWidget
from behavior_tree_widget.nodes import CompositeNodeWidget, NegationNodeWidget
from behavior_tree_widget.notes import NotesNodeWidget
from behavior_tree_widget.registry import (
    BUILTIN_KINDS,
    BUILTIN_LEAF_TYPES,
    KINDS,
    RESERVED_TYPE_NAMES,
    YIELDING_TYPE_NAMES,
    NodeTypeRegistry,
)


class Beep(LeafNodeWidget):
    _title = "Beep"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class OwnNotes(LeafNodeWidget):
    """An application's own leaf type that took a name which is now a built-in kind."""

    TYPE_NAME = "Notes"
    _title = "Notes"
    _fields = {"text": ""}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class OwnUdp(LeafNodeWidget):
    TYPE_NAME = "UDP Message"
    _title = "Beep"  # the same title as another type: labelled with its type name too
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


@pytest.fixture
def created(qtbot):
    """Collects nodes a test creates, owned by qtbot (deleted on the GUI thread)."""

    def keep(node):
        qtbot.addWidget(node)
        return node

    return keep


def test_the_kinds_table_defines_the_name_sets():
    names = [kind.name for kind in BUILTIN_KINDS]
    assert names[:3] == ["Sequence", "Selector", "Negation"]
    assert names[-2:] == ["Notes", "Group"]
    assert set(BUILTIN_LEAF_TYPES) == {kind.name for kind in BUILTIN_KINDS if kind.leaf_class is not None}
    assert {"UDP Message", "Print Message", "Sub-Tree", "Notes", "Group"} == YIELDING_TYPE_NAMES
    assert {"Root", *names} - YIELDING_TYPE_NAMES == RESERVED_TYPE_NAMES


def test_type_names_and_menu_follow_the_kinds_and_the_registered_types():
    registry = NodeTypeRegistry()
    assert registry.type_names() == [kind.name for kind in BUILTIN_KINDS]
    registry.register(Beep)
    names = registry.type_names()
    assert names.index("Sub-Tree") < names.index("Beep") < names.index("Notes")
    sections = [section for section, _, _ in registry.menu_entries()]
    assert sections == sorted(sections, key=["composite", "blackboard", "execution", "network", "log", "subtree", "leaf", "notes"].index)


def test_an_application_type_takes_a_yielding_name(created):
    registry = NodeTypeRegistry()
    registry.register(Beep)
    registry.register(OwnNotes)
    registry.register(OwnUdp)
    assert not registry.offers("Notes") and not registry.offers("UDP Message")
    assert registry.offers("Group") and registry.offers("Sequence") and registry.offers("Evaluation")
    names = registry.type_names()
    assert "Notes" in names[: names.index("Group")] and names[-1] == "Group", "now a registered leaf type"
    entries = {name: (section, label) for section, name, label in registry.menu_entries()}
    assert entries["Notes"][0] == "leaf" and entries["UDP Message"] == ("leaf", "Beep (UDP Message)")
    assert entries["Beep"] == ("leaf", "Beep (Beep)")
    assert isinstance(created(registry.create("Notes")), OwnNotes)


@pytest.mark.parametrize("cls", [LeafNodeWidget, BlackboardAwareLeaf, BlackboardValueNodeWidget, UnknownLeafNodeWidget])
def test_base_classes_cannot_be_registered(cls):
    with pytest.raises(TypeError):
        NodeTypeRegistry().register(cls)


def test_reserved_names_cannot_be_registered():
    class Stop(LeafNodeWidget):
        def OnRun(self, tree):
            return True

    with pytest.raises(ValueError, match="built-in"):
        NodeTypeRegistry().register(Stop)


def test_create_uses_the_memory_of_new_composites(created):
    registry = NodeTypeRegistry()
    sequence = created(registry.create("Sequence", memory=False))
    assert isinstance(sequence, CompositeNodeWidget) and sequence.GetMemory() is False
    assert isinstance(created(registry.create("Negation")), NegationNodeWidget)
    with pytest.raises(ValueError, match="unknown node type"):
        registry.create("Nope")


def test_create_saved_recognises_the_data_of_built_in_kinds(created):
    registry = NodeTypeRegistry()
    note, problem = registry.create_saved("Notes", {"type": "Notes", "text": "hi"}, "#1")
    assert isinstance(created(note), NotesNodeWidget) and problem is None
    group, problem = registry.create_saved("Group", {"type": "Group", "color": "#ff0000"}, "#2")
    assert isinstance(created(group), GroupNodeWidget) and problem is None
    # Saved by an application's leaf type called "Notes" that is not registered now: kept as a placeholder.
    placeholder, problem = registry.create_saved("Notes", {"type": "Notes", "fields": {"x": 1}}, "#3")
    assert isinstance(created(placeholder), UnknownLeafNodeWidget) and "not registered" in problem
    leaf, problem = registry.create_saved("UDP Message", {"fields": {"Message": "x"}}, "#4")
    assert type(created(leaf)) is BUILTIN_LEAF_TYPES["UDP Message"] and problem is None
    other, problem = registry.create_saved("UDP Message", {"fields": {"payload": "x"}}, "#5")
    assert isinstance(created(other), UnknownLeafNodeWidget) and problem
    unknown, problem = registry.create_saved("Mystery", {}, "#6")
    assert isinstance(created(unknown), UnknownLeafNodeWidget) and "'Mystery'" in problem


def test_create_saved_uses_an_application_type_that_took_the_name(created):
    registry = NodeTypeRegistry({"Notes": OwnNotes})
    node, problem = registry.create_saved("Notes", {"type": "Notes", "fields": {"text": "a"}}, "#1")
    assert isinstance(created(node), OwnNotes) and problem is None


def test_a_leaf_class_that_fails_to_build_gives_a_placeholder(created):
    class Broken(LeafNodeWidget):
        def __init__(self, parent=None):
            raise RuntimeError("no device")

        def OnRun(self, tree):
            return True

    registry = NodeTypeRegistry()
    registry.register(Broken)
    node, problem = registry.create_saved("Broken", {}, "'b' (#1)")
    assert isinstance(created(node), UnknownLeafNodeWidget)
    assert problem == "node 'b' (#1): Broken could not be created (no device); a placeholder was used"


def test_kind_lookup_by_name():
    assert KINDS["Group"].is_box and KINDS["Notes"].is_box and not KINDS["Sequence"].is_box
