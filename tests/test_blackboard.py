"""Tests of the blackboard: BlackboardStore, the BehaviorTreeWidget blackboard API and BlackboardView.

Requirements (instructions.txt):
    * the "Blackboard" tab (BlackBoardEntries.ui) lists entries in the EntryList scroll area;
    * every entry is named via its ValueName line edit and has a Value editor
      (Integer -> spin box, Float/Double -> double spin box, String -> line edit, ...);
    * entries are readable/settable by name through ``GetEntry(name)`` / ``SetEntry(value, name)``;
    * each entry has a Select checkbox, SelectAll (de)selects every entry, AddEntry adds an
      entry of the EntryType through a popup dialog and RemoveEntries removes the selected ones.

Documented behaviour (blackboard.py / widget.py docstrings) is tested as well: the typed
coercion rules, the py_trees backing store (one private namespace per widget), thread safety,
snapshot/restore and to_list/load_list persistence.
"""

from __future__ import annotations

import json
import logging
import math
import threading

import py_trees
import pytest
from py_trees.blackboard import Blackboard
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QToolButton,
)

from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget
from behavior_tree_widget.blackboard import (
    ENTRY_TYPES,
    INT_MAX,
    INT_MIN,
    AddEntryDialog,
    BlackboardStore,
    BlackboardView,
    EditContainerDialog,
    EntryRow,
    canonical_type,
    coerce_value,
    container_items,
    infer_type,
    parse_container_text,
)

from conftest import fast_config, run_until_idle

USER_TYPES = ["Integer", "Double", "String", "Bool", "List", "Dictionary", "Set"]
ALL_TYPES = USER_TYPES + ["Object"]
CONTAINER_TYPES = ["List", "Dictionary", "Set"]
CLIENT_ATTRIBUTE_NAMES = [
    "name", "get", "set", "exists", "namespace", "read", "write",
    "unregister", "unregister_key", "register_key", "remappings", "unique_identifier",
    "namespaces", "exclusive", "required", "keys", "id",
]
EXAMPLE_VALUES = {
    "Integer": 42,
    "Double": 2.5,
    "String": "text",
    "Bool": True,
    "List": [1, "two", 3.0],
    "Dictionary": {"a": 1, "b": [2]},
    "Set": {1, 2, 3},
}


class Opaque:
    """An object that cannot be encoded to JSON (stored as an Object entry)."""

    def __repr__(self):
        return "Opaque()"


# ============================================================================ helpers / fixtures
@pytest.fixture
def store(qapp):
    """A standalone BlackboardStore (disposed at teardown)."""
    created = BlackboardStore()
    yield created
    created.dispose()


@pytest.fixture
def view(bt):
    """The BlackboardView of the ``bt`` widget, made the current tab."""
    bt.setCurrentIndex(bt.indexOf(bt.blackboardView()))
    return bt.blackboardView()


def storage_keys(namespace: str) -> set[str]:
    return {key for key in Blackboard.storage if key.startswith(namespace + "/")}


def layout_names(view: BlackboardView) -> list[str]:
    """Names of the rows in the order they are laid out inside the EntryList scroll area."""
    layout = view.entry_list.widget().layout()
    names = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if isinstance(widget, EntryRow):
            names.append(widget.name)
    return names


def row_names(view: BlackboardView) -> list[str]:
    return [row.name for row in view.rows()]


def accept_add_dialog(name, value=None, record=None):
    """dialog_handler filling an AddEntryDialog with ``name`` / ``value`` and pressing OK."""

    def handler(dlg):
        assert isinstance(dlg, AddEntryDialog)
        if record is not None:
            record.append(dlg)
        dlg.name_edit.setText(name)
        editor = dlg.value_editor
        if value is not None:
            if isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                editor.setValue(value)
            elif isinstance(editor, QCheckBox):
                editor.setChecked(value)
            else:
                editor.setText(value)
        dlg._try_accept()
        return dlg.result() == QDialog.DialogCode.Accepted

    return handler


def click_add(view: BlackboardView, type_name: str) -> None:
    index = view.entry_type.findText(type_name)
    assert index >= 0, f"{type_name} missing from EntryType"
    view.entry_type.setCurrentIndex(index)
    view.add_button.click()


def mark_saved(bt: BehaviorTreeWidget, tmp_path) -> None:
    assert bt.SaveTree(str(tmp_path / "saved.json"))
    assert not bt.IsModified()


# ============================================================================ canonical_type / infer_type
@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_canonical_type_keeps_canonical_names(type_name):
    assert canonical_type(type_name) == type_name


@pytest.mark.parametrize(
    "alias, expected",
    [
        ("Float", "Double"), ("float", "Double"), ("FLOAT", "Double"), ("double", "Double"),
        ("int", "Integer"), ("integer", "Integer"), (" Int ", "Integer"),
        ("str", "String"), ("string", "String"),
        ("bool", "Bool"), ("Boolean", "Bool"),
        ("list", "List"), ("dict", "Dictionary"), ("Dict", "Dictionary"), ("dictionary", "Dictionary"),
        ("set", "Set"), ("object", "Object"),
    ],
)
def test_canonical_type_aliases(alias, expected):
    assert canonical_type(alias) == expected


@pytest.mark.parametrize("bad", ["Number", "", "Tuple", "Floats", None, 5])
def test_canonical_type_rejects_unknown_types(bad):
    with pytest.raises(ValueError):
        canonical_type(bad)


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, "Bool"), (False, "Bool"),
        (0, "Integer"), (-7, "Integer"), (INT_MAX, "Integer"), (INT_MIN, "Integer"),
        (INT_MAX + 1, "Object"), (INT_MIN - 1, "Object"),
        (1.5, "Double"), (0.0, "Double"), (float("inf"), "Double"),
        ("", "String"), ("abc", "String"),
        ([], "List"), ([1, 2], "List"), ((1, 2), "List"),
        ({}, "Dictionary"), ({"a": 1}, "Dictionary"),
        ({1, 2}, "Set"), (frozenset({1}), "Set"),
        (None, "Object"), (b"bytes", "Object"), (Opaque(), "Object"), (1 + 2j, "Object"),
    ],
)
def test_infer_type(value, expected):
    assert infer_type(value) == expected


# ============================================================================ coercion rules
@pytest.mark.parametrize(
    "type_name, value, expected",
    [
        ("Integer", 5, 5),
        ("Integer", INT_MIN, INT_MIN),
        ("Integer", INT_MAX, INT_MAX),
        ("Integer", 3.0, 3),
        ("Integer", -2.0, -2),
        ("Double", 1, 1.0),
        ("Double", 2.5, 2.5),
        ("Double", -0.125, -0.125),
        ("Double", float("inf"), float("inf")),
        ("String", "", ""),
        ("String", "hello", "hello"),
        ("Bool", True, True),
        ("Bool", False, False),
        ("List", [1, 2], [1, 2]),
        ("List", (1, "a"), [1, "a"]),
        ("List", [], []),
        ("Dictionary", {"a": 1, 2: "b"}, {"a": 1, 2: "b"}),
        ("Dictionary", {}, {}),
        ("Set", {1, 2}, {1, 2}),
        ("Set", [1, 2, 2], {1, 2}),
        ("Set", (3, 4), {3, 4}),
        ("Set", frozenset({5}), {5}),
        ("Set", {}, set()),
        ("Set", [], set()),
    ],
)
def test_coerce_value_accepts_compatible_values(type_name, value, expected):
    result = coerce_value(type_name, value)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize("value", [None, 5, "text", Opaque(), [1], {"a": 1}, INT_MAX + 10])
def test_coerce_value_object_accepts_anything_by_reference(value):
    assert coerce_value("Object", value) is value


@pytest.mark.parametrize(
    "type_name, value, error",
    [
        ("Integer", 3.5, TypeError),
        ("Integer", float("nan"), TypeError),
        ("Integer", float("inf"), TypeError),
        ("Integer", "3", TypeError),
        ("Integer", None, TypeError),
        ("Integer", [1], TypeError),
        ("Integer", INT_MAX + 1, ValueError),
        ("Integer", INT_MIN - 1, ValueError),
        ("Integer", 2.0**40, ValueError),
        ("Double", True, TypeError),
        ("Double", False, TypeError),
        ("Double", "1.5", TypeError),
        ("Double", None, TypeError),
        ("Double", 10**400, ValueError),
        ("String", 1, TypeError),
        ("String", None, TypeError),
        ("String", b"bytes", TypeError),
        ("Bool", 1, TypeError),
        ("Bool", 0, TypeError),
        ("Bool", "True", TypeError),
        ("Bool", None, TypeError),
        ("List", "abc", TypeError),
        ("List", {1, 2}, TypeError),
        ("List", {"a": 1}, TypeError),
        ("List", None, TypeError),
        ("Dictionary", [("a", 1)], TypeError),
        ("Dictionary", "{}", TypeError),
        ("Dictionary", None, TypeError),
        ("Set", "abc", TypeError),
        ("Set", {"a": 1}, TypeError),
        ("Set", [[1], [2]], TypeError),
        ("Set", None, TypeError),
    ],
)
def test_coerce_value_rejects_incompatible_values(type_name, value, error):
    with pytest.raises(error):
        coerce_value(type_name, value)


@pytest.mark.parametrize("value", [True, False])
def test_integer_entries_reject_bool(value):
    """Bool is its own entry type: like Double, Integer must not silently turn True into 1."""
    with pytest.raises(TypeError):
        coerce_value("Integer", value)


def test_coerce_value_unknown_type_raises():
    with pytest.raises(ValueError):
        coerce_value("Tuple", (1, 2))


@pytest.mark.parametrize("type_name", CONTAINER_TYPES)
def test_coerce_value_deep_copies_containers(type_name):
    inner = (1, 2) if type_name == "Set" else [1, 2]
    source = {"k": inner} if type_name == "Dictionary" else [inner]
    result = coerce_value(type_name, source)
    assert result is not source
    if type_name == "List":
        assert result[0] is not inner
        inner.append(3)
        assert result == [[1, 2]]
    elif type_name == "Dictionary":
        assert result["k"] is not inner
        inner.append(3)
        assert result == {"k": [1, 2]}
    else:
        assert result == {(1, 2)}


@pytest.mark.parametrize(
    "type_name, text, expected",
    [
        ("List", "[1, 2.5, 'text']", [1, 2.5, "text"]),
        ("List", "(1, 2)", [1, 2]),
        ("List", "", []),
        ("List", "   ", []),
        ("Dictionary", "{'key': 'value', 'n': 3}", {"key": "value", "n": 3}),
        ("Dictionary", "{}", {}),
        ("Dictionary", "", {}),
        ("Set", "{1, 2, 3}", {1, 2, 3}),
        ("Set", "[1, 1, 2]", {1, 2}),
        ("Set", "{}", set()),
        ("Set", "set()", set()),
        ("Set", "", set()),
    ],
)
def test_parse_container_text_valid(type_name, text, expected):
    assert parse_container_text(type_name, text) == expected


@pytest.mark.parametrize(
    "type_name, text",
    [
        ("List", "not a literal"),
        ("List", "[1, 2"),
        ("List", "{'a': 1}"),
        ("List", "42"),
        ("Dictionary", "[1, 2]"),
        ("Dictionary", "__import__('os')"),
        ("Set", "[[1], [2]]"),
        ("Set", "{'a': 1}"),
        ("Set", "'abc'"),
    ],
)
def test_parse_container_text_invalid_raises_value_error(type_name, text):
    with pytest.raises(ValueError):
        parse_container_text(type_name, text)


def test_container_items_display_strings():
    assert container_items("List", [1, "a", 2.5]) == ["1", "a", "2.5"]
    assert container_items("Dictionary", {"a": 1, "b": [2]}) == ["a: 1", "b: [2]"]
    assert container_items("Set", {3, 1, 2}) == ["1", "2", "3"]
    assert container_items("Set", set()) == []


# ============================================================================ BlackboardStore basics
@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_add_uses_type_default_value(store, type_name):
    store.add("entry", type_name)
    assert store.type_of("entry") == type_name
    expected = ENTRY_TYPES[type_name].default
    value = store.get("entry")
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize("type_name", CONTAINER_TYPES)
def test_container_defaults_are_not_shared(store, type_name):
    store.add("one", type_name)
    store.add("two", type_name)
    first = Blackboard.storage[store.key("one")]
    second = Blackboard.storage[store.key("two")]
    assert first is not second
    assert first is not ENTRY_TYPES[type_name].default
    assert second is not ENTRY_TYPES[type_name].default


def test_add_with_alias_and_value(store):
    store.add("ratio", "Float", 1)
    assert store.type_of("ratio") == "Double"
    assert store.get("ratio") == 1.0 and isinstance(store.get("ratio"), float)


def test_add_rejects_incompatible_value_and_unknown_type(store):
    with pytest.raises(TypeError):
        store.add("x", "Integer", "text")
    with pytest.raises(ValueError):
        store.add("y", "Tuple", (1,))
    assert store.names() == []
    assert storage_keys(store.namespace()) == set()


@pytest.mark.parametrize("type_name, value", list(EXAMPLE_VALUES.items()) + [("Object", Opaque())])
def test_values_are_mirrored_in_py_trees_storage(store, type_name, value):
    store.add("entry", type_name, value)
    key = store.key("entry")
    assert key == f"{store.namespace()}/entry"
    assert key in Blackboard.storage
    if type_name == "Object":
        assert Blackboard.storage[key] is value
    else:
        assert Blackboard.storage[key] == value


def test_set_updates_py_trees_storage(store):
    store.set("count", 1)
    store.set("count", 2)
    assert Blackboard.storage[store.key("count")] == 2
    assert store.get("count") == 2


def test_namespace_is_private_and_absolute(store, qapp):
    other = BlackboardStore()
    try:
        assert store.namespace().startswith("/")
        assert "/" not in store.namespace()[1:]
        assert store.namespace() != other.namespace()
        assert store.client().namespace == store.namespace()
    finally:
        other.dispose()


def test_py_trees_client_in_same_namespace_shares_values(bt):
    """Plain py_trees behaviours can use the widget's entries through its namespace."""
    bt.SetEntry(5, "shared")
    client = py_trees.blackboard.Client(name="external", namespace=bt.GetBlackboardNamespace())
    try:
        client.register_key(key="shared", access=py_trees.common.Access.WRITE)
        assert client.shared == 5
        client.shared = 9
        assert bt.GetEntry("shared") == 9
    finally:
        client.unregister(clear=False)


# ============================================================================ names
@pytest.mark.parametrize("name", ["", " ", "\t", " leading", "trailing ", "a.b", ".", "a/b", "/abs", "x/"])
def test_invalid_names_raise_value_error(store, name):
    with pytest.raises(ValueError):
        store.validate_name(name)
    with pytest.raises(ValueError):
        store.add(name, "Integer", 1)
    with pytest.raises(ValueError):
        store.set(name, 1)
    assert store.names() == []
    assert storage_keys(store.namespace()) == set()


@pytest.mark.parametrize("name", [None, 5, b"name", ("a",)])
def test_non_string_names_raise_type_error(store, name):
    with pytest.raises(TypeError):
        store.validate_name(name)
    with pytest.raises(TypeError):
        store.add(name, "Integer", 1)
    assert store.names() == []


def test_duplicate_names_are_rejected(store):
    store.add("dup", "Integer", 1)
    with pytest.raises(ValueError):
        store.add("dup", "String", "x")
    assert store.type_of("dup") == "Integer"
    assert store.get("dup") == 1
    assert store.validate_name("dup", allow_existing="dup") == "dup"


@pytest.mark.parametrize("name", CLIENT_ATTRIBUTE_NAMES)
def test_names_colliding_with_py_trees_client_attributes(bt, name):
    store = bt.blackboardStore()
    client = store.client()
    bt.SetEntry(1, name)
    assert bt.HasEntry(name)
    assert bt.GetEntry(name) == 1
    bt.SetEntry(2, name)
    assert bt.GetEntry(name) == 2
    assert Blackboard.storage[store.key(name)] == 2
    # the client itself is not affected by the entry
    assert client.name == "BehaviorTreeWidget"
    assert client.namespace == store.namespace()
    assert callable(client.get) and callable(client.set)
    bt.SetEntry("other", "other_entry")
    assert bt.GetEntry("other_entry") == "other"
    # rename and remove work too
    store.rename(name, name + "_renamed")
    assert bt.GetEntry(name + "_renamed") == 2
    store.rename(name + "_renamed", name)
    bt.RemoveEntry(name)
    assert not bt.HasEntry(name)
    assert store.key(name) not in Blackboard.storage


@pytest.mark.parametrize("name", ["température", "日本語", "with space", "emoji 🎉", "tab\tinside", "1st", "a-b_c:d"])
def test_unicode_and_space_names(bt, view, name):
    bt.SetEntry(3.5, name)
    assert bt.GetEntry(name) == 3.5
    assert bt.GetEntryType(name) == "Double"
    assert Blackboard.storage[bt.blackboardStore().key(name)] == 3.5
    row = view.row(name)
    assert row is not None
    assert row.name_edit.text() == name
    bt.SetEntry(-1.25, name)
    assert row.value_widget.value() == -1.25


# ============================================================================ set / replace / remove / rename
def test_set_existing_entry_coerces_and_keeps_type(store):
    store.add("count", "Integer", 1)
    store.set("count", 4.0)
    assert store.get("count") == 4 and type(store.get("count")) is int
    with pytest.raises(TypeError):
        store.set("count", "five")
    with pytest.raises(TypeError):
        store.set("count", 4.5)
    with pytest.raises(ValueError):
        store.set("count", INT_MAX + 1)
    assert store.get("count") == 4
    assert store.type_of("count") == "Integer"


@pytest.mark.parametrize(
    "type_name, value, expected",
    [
        ("List", (1, 2), [1, 2]),
        ("Set", [1, 2, 2], {1, 2}),
        ("Set", (3,), {3}),
        ("Set", {}, set()),
        ("Set", frozenset({4}), {4}),
        ("Dictionary", {"k": (1,)}, {"k": (1,)}),
        ("Double", 3, 3.0),
        ("Integer", 8.0, 8),
    ],
)
def test_set_existing_entries_accept_documented_inputs(bt, type_name, value, expected):
    bt.AddEntry("entry", type_name)
    bt.SetEntry(value, "entry")
    result = bt.GetEntry("entry")
    assert result == expected and type(result) is type(expected)
    assert bt.GetEntryType("entry") == type_name


def test_set_creates_entries_with_inferred_type(store):
    values = {"i": 1, "d": 1.5, "s": "x", "b": False, "l": (1, 2), "m": {"k": 1}, "st": {1}, "o": Opaque(),
              "big": 2**40, "none": None}
    for name, value in values.items():
        store.set(name, value)
    assert {name: store.type_of(name) for name in values} == {
        "i": "Integer", "d": "Double", "s": "String", "b": "Bool", "l": "List", "m": "Dictionary",
        "st": "Set", "o": "Object", "big": "Object", "none": "Object",
    }
    assert store.get("l") == [1, 2]
    assert store.get("big") == 2**40
    assert store.get("none") is None


def test_object_entries_accept_any_value_later(store):
    store.set("thing", Opaque())
    store.set("thing", "now a string")
    assert store.type_of("thing") == "Object"
    assert store.get("thing") == "now a string"
    store.set("thing", None)
    assert store.get("thing") is None


def test_rename_keeps_value_order_and_moves_py_trees_key(store):
    store.add("a", "Integer", 1)
    store.add("b", "List", [1, 2])
    store.add("c", "String", "z")
    old_key = store.key("b")
    store.rename("b", "renamed")
    assert store.names() == ["a", "renamed", "c"]
    assert store.get("renamed") == [1, 2]
    assert store.type_of("renamed") == "List"
    assert not store.has("b")
    assert old_key not in Blackboard.storage
    assert Blackboard.storage[store.key("renamed")] == [1, 2]
    with pytest.raises(KeyError):
        store.get("b")
    # the renamed entry is writable, the old one is gone for good
    store.set("renamed", [3])
    assert store.get("renamed") == [3]
    store.set("b", "new")
    assert store.type_of("b") == "String"
    assert store.names() == ["a", "renamed", "c", "b"]


@pytest.mark.parametrize("new", ["", " x", "a.b", "a/b", "other"])
def test_rename_to_invalid_or_existing_name_fails_without_change(store, new):
    store.add("entry", "Integer", 7)
    store.add("other", "Bool", True)
    with pytest.raises(ValueError):
        store.rename("entry", new)
    assert store.names() == ["entry", "other"]
    assert store.get("entry") == 7
    assert store.get("other") is True
    assert Blackboard.storage[store.key("entry")] == 7


def test_rename_missing_entry_raises_key_error(store):
    with pytest.raises(KeyError):
        store.rename("missing", "new")


def test_rename_to_same_name_is_a_no_op(store, qtbot):
    store.add("same", "Integer", 3)
    renamed = []
    store.entryRenamed.connect(lambda old, new: renamed.append((old, new)))
    store.rename("same", "same")
    assert store.get("same") == 3
    assert renamed == []


def test_remove_unregisters_key(store):
    store.add("gone", "Integer", 1)
    key = store.key("gone")
    store.remove("gone")
    assert not store.has("gone")
    assert store.names() == []
    assert key not in Blackboard.storage
    assert key not in Blackboard.metadata
    assert key not in store.client().write
    with pytest.raises(KeyError):
        store.get("gone")
    with pytest.raises(KeyError):
        store.remove("gone")
    # the name can be reused with another type
    store.add("gone", "String", "back")
    assert store.get("gone") == "back"


def test_replace_retypes_entry_in_place(store):
    store.add("a", "Integer", 1)
    store.add("b", "Integer", 2)
    store.add("c", "Integer", 3)
    retyped, changed = [], []
    store.entryRetyped.connect(retyped.append)
    store.entryChanged.connect(changed.append)
    store.replace("b", "String", "two")
    assert store.type_of("b") == "String"
    assert store.get("b") == "two"
    assert store.names() == ["a", "b", "c"]
    assert Blackboard.storage[store.key("b")] == "two"
    store.replace("b", "String", "deux")
    assert retyped == ["b"]
    assert changed == ["b"]
    store.replace("d", "Float", 4)
    assert store.type_of("d") == "Double" and store.get("d") == 4.0
    with pytest.raises(TypeError):
        store.replace("a", "Bool", "yes")
    assert store.type_of("a") == "Integer" and store.get("a") == 1


def test_store_signals(store):
    events = []
    store.entryAdded.connect(lambda name: events.append(("added", name)))
    store.entryChanged.connect(lambda name: events.append(("changed", name)))
    store.entryRenamed.connect(lambda old, new: events.append(("renamed", old, new)))
    store.entryRetyped.connect(lambda name: events.append(("retyped", name)))
    store.entryRemoved.connect(lambda name: events.append(("removed", name)))
    store.set("x", 1)
    store.set("x", 2)
    store.rename("x", "y")
    store.replace("y", "String", "s")
    store.remove("y")
    assert events == [("added", "x"), ("changed", "x"), ("renamed", "x", "y"), ("retyped", "y"), ("removed", "y")]


def test_clear_removes_every_entry(store):
    for index in range(3):
        store.set(f"e{index}", index)
    store.clear()
    assert store.names() == []
    assert storage_keys(store.namespace()) == set()


# ============================================================================ copies
@pytest.mark.parametrize(
    "value",
    [[1, [2, 3]], {"a": [1], "b": {"c": 2}}, {1, 2}],
    ids=["list", "dict", "set"],
)
def test_get_entry_returns_copies_of_containers(bt, value):
    bt.SetEntry(value, "container")
    first = bt.GetEntry("container")
    assert first == value
    if isinstance(first, list):
        first[1].append(99)
        first.append("extra")
    elif isinstance(first, dict):
        first["a"].append(99)
        first["b"]["c"] = 0
        first["new"] = 1
    else:
        first.add(99)
    assert bt.GetEntry("container") == value
    assert Blackboard.storage[bt.blackboardStore().key("container")] == value


def test_set_entry_copies_the_given_container(bt):
    source = [[1], {"k": [2]}]
    bt.SetEntry(source, "copied")
    source[0].append(5)
    source[1]["k"].append(6)
    source.append("more")
    assert bt.GetEntry("copied") == [[1], {"k": [2]}]


def test_object_entries_are_returned_by_reference(bt):
    thing = Opaque()
    bt.SetEntry(thing, "thing")
    assert bt.GetEntryType("thing") == "Object"
    assert bt.GetEntry("thing") is thing


# ============================================================================ snapshot / restore
def _populate(store):
    store.add("i", "Integer", 1)
    store.add("d", "Double", 0.5)
    store.add("s", "String", "text")
    store.add("b", "Bool", True)
    store.add("l", "List", [1, [2]])
    store.add("m", "Dictionary", {"k": [1]})
    store.add("st", "Set", {1, 2})
    store.add("o", "Object", Opaque())


def test_snapshot_is_a_deep_copy(store):
    _populate(store)
    snapshot = store.snapshot()
    assert [(name, kind) for name, kind, _ in snapshot] == [
        ("i", "Integer"), ("d", "Double"), ("s", "String"), ("b", "Bool"),
        ("l", "List"), ("m", "Dictionary"), ("st", "Set"), ("o", "Object"),
    ]
    values = {name: value for name, _, value in snapshot}
    assert values["l"] == [1, [2]]
    Blackboard.storage[store.key("l")][1].append(3)  # mutate the live value in place
    assert values["l"] == [1, [2]]
    values["m"]["k"].append(9)  # mutate the snapshot
    assert store.get("m") == {"k": [1]}


def test_restore_makes_the_store_match_the_snapshot_exactly(store):
    _populate(store)
    snapshot = store.snapshot()
    expected = [(name, kind, value) for name, kind, value in snapshot if kind != "Object"]
    # modify values, types, names; add and remove entries
    store.set("i", 99)
    store.set("l", [])
    store.replace("s", "Integer", 5)
    store.remove("b")
    store.rename("d", "d2")
    store.add("extra", "String", "new")
    store.restore(snapshot)
    assert sorted(store.names()) == sorted(name for name, _, _ in snapshot)
    current = {name: (kind, value) for name, kind, value in store.snapshot()}
    for name, kind, value in expected:
        assert current[name] == (kind, value), name
    assert current["o"][0] == "Object"
    assert not store.has("extra") and not store.has("d2")
    assert storage_keys(store.namespace()) == {store.key(name) for name, _, _ in snapshot}
    # restoring again from the same snapshot gives fresh copies
    Blackboard.storage[store.key("l")].append("changed")
    store.restore(snapshot)
    assert store.get("l") == [1, [2]]


def test_restore_keeps_entry_order(store):
    _populate(store)
    snapshot = store.snapshot()
    store.remove("b")
    store.rename("d", "d2")
    store.restore(snapshot)
    assert store.names() == [name for name, _, _ in snapshot]


def test_restore_keeps_uncopyable_objects_by_reference(store):
    lock = threading.Lock()
    store.set("lock", lock)
    snapshot = store.snapshot()
    store.set("lock", None)
    store.restore(snapshot)
    assert store.get("lock") is lock


def test_restore_empty_snapshot_clears_store(store):
    _populate(store)
    store.restore([])
    assert store.names() == []
    assert storage_keys(store.namespace()) == set()


# ============================================================================ to_list / load_list
def test_to_list_load_list_round_trip_through_json(store, qapp):
    store.add("i", "Integer", -5)
    store.add("d", "Double", 1.25)
    store.add("s", "String", "ünïcode")
    store.add("b", "Bool", False)
    store.add("l", "List", [1, (2, 3), {"x": {4}}])
    store.add("m", "Dictionary", {"a": 1, 2: "int key", (1, 2): [3]})
    store.add("st", "Set", {1, "two", (3, 4)})
    store.add("none", "Object", None)
    store.set("big", 2**40)
    data = json.loads(json.dumps(store.to_list()))
    # Object entries ("none", "big") hold run-time values and are never saved.
    assert [item["name"] for item in data] == ["i", "d", "s", "b", "l", "m", "st"]
    assert all(set(item) == {"name", "type", "value"} for item in data)

    other = BlackboardStore()
    try:
        assert other.load_list(data) == []
        assert other.snapshot() == [entry for entry in store.snapshot() if entry[1] != "Object"]
    finally:
        other.dispose()


def test_object_entries_in_files_are_skipped_with_a_problem(store):
    problems = store.load_list([{"name": "o", "type": "Object", "value": 1}, {"name": "i", "type": "Integer", "value": 2}])
    assert len(problems) == 1 and "'o'" in problems[0]
    assert store.names() == ["i"]


@pytest.mark.parametrize("name", ["", " x", "x ", "a.b", "a/b"])
def test_invalid_names_in_files_are_skipped_with_a_problem(store, name):
    problems = store.load_list([{"name": name, "type": "Integer", "value": 1}, {"name": "ok", "type": "Integer", "value": 2}])
    assert len(problems) == 1
    assert store.names() == ["ok"]


def test_to_list_skips_unencodable_object_entries(store, caplog):
    store.add("before", "Integer", 1)
    store.set("opaque", Opaque())
    store.set("nested_opaque", [Opaque()])
    store.add("after", "String", "x")
    with caplog.at_level(logging.WARNING, logger="behavior_tree_widget"):
        data = store.to_list()
    assert [item["name"] for item in data] == ["before", "after"]
    assert "opaque" in caplog.text


@pytest.mark.parametrize(
    "item, fragment",
    [
        ("not a dict", "not an object"),
        ({"name": "x", "type": "Tuple", "value": [1]}, "'x'"),
        ({"name": "x", "type": None, "value": 1}, "'x'"),
        ({"name": "x", "type": "Integer", "value": "text"}, "'x'"),
        ({"name": "x", "type": "Integer", "value": 2**40}, "'x'"),
        ({"name": "", "type": "Integer", "value": 1}, "''"),
        ({"name": "a.b", "type": "Integer", "value": 1}, "'a.b'"),
        ({"name": None, "type": "Integer", "value": 1}, "None"),
        ({"type": "Integer", "value": 1}, "None"),
        ({"name": "x", "type": "Set", "value": {"__set__": [[1, 2]]}}, "'x'"),
    ],
)
def test_load_list_reports_problems_and_keeps_going(store, item, fragment):
    problems = store.load_list([{"name": "good1", "type": "Integer", "value": 1}, item,
                                {"name": "good2", "type": "Float", "value": 2}])
    assert len(problems) == 1
    assert fragment in problems[0]
    assert store.names() == ["good1", "good2"]
    assert store.get("good2") == 2.0 and store.type_of("good2") == "Double"


def test_load_list_overwrites_and_retypes_existing_entries(store):
    store.add("a", "Integer", 1)
    store.add("b", "String", "keep")
    problems = store.load_list([
        {"name": "a", "type": "String", "value": "now text"},
        {"name": "c", "type": "Set", "value": {"__set__": [1, 2]}},
        {"name": "c", "type": "Set", "value": {"__set__": [3]}},
    ])
    # A duplicated name is reported; the first occurrence is kept.
    assert len(problems) == 1 and "'c'" in problems[0] and "duplicate" in problems[0]
    assert store.names() == ["a", "b", "c"]
    assert store.type_of("a") == "String" and store.get("a") == "now text"
    assert store.get("b") == "keep"
    assert store.get("c") == {1, 2}


def test_load_list_replace_removes_other_entries_but_keeps_objects(store):
    marker = object()
    store.add("old", "Integer", 1)
    store.add("obj", "Object", marker)
    problems = store.load_list([{"name": "new", "type": "String", "value": "x"}], replace=True)
    assert problems == []
    assert store.names() == ["obj", "new"]
    assert store.get("obj") is marker


# ============================================================================ dispose / widget isolation
def test_dispose_removes_keys_and_client(qapp):
    store = BlackboardStore()
    store.set("a", 1)
    store.set("b", [1])
    identifier = store.client().unique_identifier
    namespace = store.namespace()
    assert identifier in Blackboard.clients
    store.dispose()
    assert identifier not in Blackboard.clients
    assert storage_keys(namespace) == set()
    assert not any(key.startswith(namespace + "/") for key in Blackboard.metadata)
    assert store.names() == []
    with pytest.raises(KeyError):
        store.get("a")
    store.dispose()  # idempotent


def test_widget_shutdown_disposes_blackboard(make_widget):
    widget = make_widget()
    widget.SetEntry(1, "x")
    namespace = widget.GetBlackboardNamespace()
    assert storage_keys(namespace)
    widget.Shutdown()
    assert storage_keys(namespace) == set()
    assert widget.GetEntryNames() == []


def test_set_entry_after_shutdown_does_not_leak_keys(make_widget):
    """Shutdown releases the widget's py_trees keys; a late write (e.g. from a node thread that
    ignored cancellation) must not register keys in the global blackboard again."""
    widget = make_widget()
    widget.SetEntry(1, "x")
    namespace = widget.GetBlackboardNamespace()
    widget.Shutdown()
    try:
        widget.SetEntry(2, "late")
    except RuntimeError:
        pass
    try:
        widget.SetEntry(3, "x")
    except (RuntimeError, KeyError):
        pass
    assert storage_keys(namespace) == set()
    assert not any(key.startswith(namespace + "/") for key in Blackboard.metadata)


def test_two_widgets_do_not_share_entries(make_widget):
    first, second = make_widget(), make_widget()
    assert first.GetBlackboardNamespace() != second.GetBlackboardNamespace()
    first.SetEntry(1, "shared_name")
    second.SetEntry("two", "shared_name")
    first.SetEntry(True, "only_first")
    assert first.GetEntry("shared_name") == 1
    assert second.GetEntry("shared_name") == "two"
    assert second.GetEntryType("shared_name") == "String"
    assert not second.HasEntry("only_first")
    assert second.GetEntryNames() == ["shared_name"]
    assert first.blackboardView().row("only_first") is not None
    assert second.blackboardView().row("only_first") is None
    first.Shutdown()
    assert second.GetEntry("shared_name") == "two"
    assert Blackboard.storage[second.blackboardStore().key("shared_name")] == "two"


# ============================================================================ widget-level API
def test_widget_blackboard_api(bt):
    assert bt.GetEntryNames() == []
    bt.AddEntry("count", "Integer")
    bt.AddEntry("ratio", "Float", 0.5)
    bt.AddEntry("flags", "set", [1, 2])
    bt.AddEntry("thing", "Object")
    bt.SetEntry("hi", "greeting")
    assert bt.GetEntryNames() == ["count", "ratio", "flags", "thing", "greeting"]
    assert [bt.GetEntryType(name) for name in bt.GetEntryNames()] == ["Integer", "Double", "Set", "Object", "String"]
    assert bt.GetEntry("count") == 0
    assert bt.GetEntry("ratio") == 0.5
    assert bt.GetEntry("flags") == {1, 2}
    assert bt.GetEntry("thing") is None
    assert bt.HasEntry("greeting") and not bt.HasEntry("missing")
    bt.RemoveEntry("ratio")
    assert bt.GetEntryNames() == ["count", "flags", "thing", "greeting"]
    assert bt.GetBlackboardNamespace() == bt.blackboardStore().namespace()


def test_widget_blackboard_api_errors(bt):
    with pytest.raises(KeyError):
        bt.GetEntry("missing")
    with pytest.raises(KeyError):
        bt.GetEntryType("missing")
    with pytest.raises(KeyError):
        bt.RemoveEntry("missing")
    bt.AddEntry("x", "Integer", 1)
    with pytest.raises(ValueError):
        bt.AddEntry("x", "Integer", 2)
    with pytest.raises(TypeError):
        bt.SetEntry("text", "x")
    with pytest.raises(TypeError):
        bt.SetEntry(1, 123)
    with pytest.raises(ValueError):
        bt.SetEntry(1, "bad.name")
    with pytest.raises(ValueError):
        bt.AddEntry("y", "Complex", 1)
    assert bt.GetEntryNames() == ["x"]
    assert bt.GetEntry("x") == 1


def test_set_entry_argument_order_is_value_then_name(bt):
    bt.SetEntry("value", "name")
    assert bt.GetEntry("name") == "value"
    assert not bt.HasEntry("value")


def test_new_tree_clears_entries_but_keeps_object_entries(bt, tmp_path):
    """New starts with an empty blackboard; Object entries (run-time objects from code) survive."""
    marker = object()
    bt.SetEntry(3, "cleared")
    bt.SetEntry(marker, "runtime_object")
    assert bt.NewTree(str(tmp_path / "other.json"))
    assert not bt.HasEntry("cleared")
    assert bt.GetEntry("runtime_object") is marker
    assert bt.GetEntryType("runtime_object") == "Object"


# ============================================================================ threading
def _run_threads(count, target):
    errors = []

    def wrapper(index):
        try:
            target(index)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=wrapper, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    return errors


def test_concurrent_set_and_get_from_worker_threads(qtbot, bt, view, monkeypatch, caplog):
    gui_thread = threading.get_ident()
    refresh_threads = set()
    original_refresh = EntryRow.refresh_value

    def recording_refresh(self):
        refresh_threads.add(threading.get_ident())
        return original_refresh(self)

    monkeypatch.setattr(EntryRow, "refresh_value", recording_refresh)
    bt.AddEntry("counter", "Integer", 0)
    bt.AddEntry("shared_list", "List", [])
    lock = threading.Lock()
    iterations = 150
    workers = 8

    def work(index):
        for step in range(iterations):
            bt.SetEntry(step, f"own_{index}")
            assert bt.GetEntry(f"own_{index}") == step
            with lock:
                bt.SetEntry(bt.GetEntry("counter") + 1, "counter")
            bt.SetEntry([index, step], "shared_list")
            value = bt.GetEntry("shared_list")
            assert isinstance(value, list) and len(value) == 2
            bt.SetEntry(float(step), f"ratio_{index}")
            bt.SetEntry(f"text {step}", f"text_{index}")

    with caplog.at_level(logging.WARNING, logger="behavior_tree_widget"):
        errors = _run_threads(workers, work)
    assert errors == []
    assert bt.GetEntry("counter") == workers * iterations
    for index in range(workers):
        assert bt.GetEntry(f"own_{index}") == iterations - 1
        assert bt.GetEntry(f"text_{index}") == f"text {iterations - 1}"

    def rows_in_sync():
        if row_names(view) != bt.GetEntryNames() or layout_names(view) != bt.GetEntryNames():
            return False
        return (
            view.row("counter").value_widget.value() == workers * iterations
            and all(view.row(f"own_{i}").value_widget.value() == iterations - 1 for i in range(workers))
            and all(view.row(f"text_{i}").value_widget.text() == f"text {iterations - 1}" for i in range(workers))
            and all(view.row(f"ratio_{i}").value_widget.value() == float(iterations - 1) for i in range(workers))
        )

    qtbot.waitUntil(rows_in_sync, timeout=10000)
    assert refresh_threads == {gui_thread}
    assert "could not" not in caplog.text


def test_entries_created_from_worker_thread_appear_in_view(qtbot, bt, view):
    names = [f"worker_{index}" for index in range(25)]

    def work(_index):
        for position, name in enumerate(names):
            bt.SetEntry(position, name)

    assert _run_threads(1, work) == []
    assert bt.GetEntryNames() == names
    qtbot.waitUntil(lambda: row_names(view) == names and layout_names(view) == names, timeout=5000)
    for position, name in enumerate(names):
        row = view.row(name)
        assert isinstance(row.value_widget, QSpinBox)
        assert row.value_widget.value() == position
        assert row.name_edit.text() == name


def test_concurrent_add_and_remove_keeps_view_consistent(qtbot, bt, view):
    def work(index):
        for step in range(60):
            name = f"flicker_{index}"
            bt.SetEntry(step, name)
            bt.RemoveEntry(name)
            bt.SetEntry(step, f"stay_{index}")

    assert _run_threads(4, work) == []
    expected = [f"stay_{index}" for index in range(4)]
    assert sorted(bt.GetEntryNames()) == expected
    qtbot.waitUntil(
        lambda: row_names(view) == bt.GetEntryNames() and layout_names(view) == bt.GetEntryNames(), timeout=5000
    )
    assert all(view.row(name).value_widget.value() == 59 for name in expected)
    assert view.row("flicker_0") is None


class WriteBlackboard(LeafNodeWidget):
    """Threaded leaf writing the blackboard from its worker thread."""

    _title = "Write Blackboard"
    threads: list[int] = []

    def OnRun(self, tree):
        type(self).threads.append(threading.get_ident())
        tree.SetEntry(tree.GetEntry("runs") + 1, "runs")
        tree.SetEntry(f"run {tree.GetEntry('runs')}", "created_by_node")
        return True


def test_node_writes_from_worker_thread_during_execution(qtbot, bt, view, tmp_path):
    WriteBlackboard.threads.clear()
    bt.AddEntry("runs", "Integer", 0)
    leaf = bt.AddNode(WriteBlackboard, 0, 200)
    bt.Connect(bt.GetRootNode(), leaf)
    fast_config(bt)
    mark_saved(bt, tmp_path)
    for run in range(1, 4):
        bt.Execute()
        run_until_idle(qtbot, bt)
        assert bt.GetEntry("runs") == run
        qtbot.waitUntil(
            lambda: view.row("created_by_node") is not None
            and view.row("created_by_node").value_widget.text() == f"run {run}"
            and view.row("runs").value_widget.value() == run,
            timeout=5000,
        )
    assert WriteBlackboard.threads and threading.get_ident() not in WriteBlackboard.threads
    assert layout_names(view) == ["runs", "created_by_node"]
    qtbot.wait(20)
    # values written by running nodes are not edits of the tree file
    assert not bt.IsModified()


def test_worker_thread_rename_and_retype_update_view(qtbot, bt, view):
    bt.AddEntry("first", "Integer", 1)
    bt.AddEntry("second", "Integer", 2)
    store = bt.blackboardStore()

    def work(_index):
        store.rename("first", "renamed")
        store.replace("second", "String", "text")

    assert _run_threads(1, work) == []
    qtbot.waitUntil(lambda: layout_names(view) == ["renamed", "second"], timeout=5000)
    qtbot.waitUntil(lambda: isinstance(view.row("second").value_widget, QLineEdit), timeout=5000)
    assert view.row("renamed").value_widget.value() == 1
    assert view.row("second").value_widget.text() == "text"


# ============================================================================ BlackboardView structure
def test_blackboard_tab_contains_the_view(bt):
    view = bt.blackboardView()
    index = bt.indexOf(view)
    assert index >= 0
    assert bt.tabText(index) == "Blackboard"
    assert isinstance(view.entry_list, QScrollArea) and view.entry_list.objectName() == "EntryList"
    assert view.select_all.objectName() == "SelectAll"
    assert view.add_button.objectName() == "AddEntry"
    assert view.remove_button.objectName() == "RemoveEntries"
    assert view.entry_type.objectName() == "EntryType"


def test_entry_type_combo_items(view):
    items = [view.entry_type.itemText(index) for index in range(view.entry_type.count())]
    assert items == USER_TYPES


@pytest.mark.parametrize(
    "type_name, value, widget_class",
    [
        ("Integer", 3, QSpinBox),
        ("Double", 1.5, QDoubleSpinBox),
        ("String", "abc", QLineEdit),
        ("Bool", True, QCheckBox),
        ("List", [1, 2], QComboBox),
        ("Dictionary", {"a": 1}, QComboBox),
        ("Set", {1}, QComboBox),
        ("Object", Opaque(), QLineEdit),
    ],
)
def test_rows_use_the_documented_editors(bt, view, type_name, value, widget_class):
    bt.AddEntry("entry", type_name, value)
    row = view.row("entry")
    assert row is not None and row.type_name == type_name
    assert isinstance(row.value_widget, widget_class)
    assert isinstance(row.select_box, QCheckBox) and row.select_box.objectName() == "Select"
    assert isinstance(row.name_edit, QLineEdit) and row.name_edit.objectName() == "ValueName"
    assert row.name_edit.text() == "entry"
    assert view.entry_list.widget().isAncestorOf(row)
    if type_name in CONTAINER_TYPES:
        assert isinstance(row.edit_button, QToolButton)
    else:
        assert row.edit_button is None


def test_rows_appear_in_store_order(bt, view):
    for name in ["zeta", "alpha", "mid"]:
        bt.SetEntry(1, name)
    assert layout_names(view) == ["zeta", "alpha", "mid"]
    bt.blackboardStore().rename("alpha", "beta")
    assert layout_names(view) == ["zeta", "beta", "mid"]
    bt.blackboardStore().replace("zeta", "String", "now text")
    assert layout_names(view) == ["zeta", "beta", "mid"]
    assert isinstance(view.row("zeta").value_widget, QLineEdit)
    bt.RemoveEntry("beta")
    bt.SetEntry(2.0, "last")
    assert layout_names(view) == ["zeta", "mid", "last"]
    assert row_names(view) == bt.GetEntryNames()


@pytest.mark.parametrize(
    "type_name, value, check",
    [
        ("Integer", -12, lambda w: w.value() == -12),
        ("Double", 0.125, lambda w: w.value() == 0.125),
        ("String", "shown", lambda w: w.text() == "shown"),
        ("Bool", True, lambda w: w.isChecked() and w.text() == "True"),
        ("List", [3, "x"], lambda w: [w.itemText(i) for i in range(w.count())] == ["3", "x"]),
        ("Dictionary", {"k": 2}, lambda w: [w.itemText(i) for i in range(w.count())] == ["k: 2"]),
        ("Set", {2, 1}, lambda w: [w.itemText(i) for i in range(w.count())] == ["1", "2"]),
    ],
)
def test_programmatic_set_entry_updates_row(bt, view, type_name, value, check):
    bt.AddEntry("entry", type_name)
    bt.SetEntry(value, "entry")
    assert check(view.row("entry").value_widget)


# ============================================================================ AddEntry button
@pytest.mark.parametrize(
    "type_name, typed, expected",
    [
        ("Integer", -42, -42),
        ("Double", 3.25, 3.25),
        ("String", "hello world", "hello world"),
        ("Bool", True, True),
        ("List", "[1, 2.5, 'text']", [1, 2.5, "text"]),
        ("Dictionary", "{'key': 'value', 'n': 3}", {"key": "value", "n": 3}),
        ("Set", "{1, 2, 3}", {1, 2, 3}),
    ],
)
def test_add_entry_button_adds_every_type(bt, view, dialogs, tmp_path, type_name, typed, expected):
    mark_saved(bt, tmp_path)
    shown = []
    dialogs.dialog_handler = accept_add_dialog("new_entry", typed, shown)
    click_add(view, type_name)
    assert len(shown) == 1
    assert shown[0].type_name == type_name
    assert bt.HasEntry("new_entry")
    assert bt.GetEntryType("new_entry") == type_name
    assert bt.GetEntry("new_entry") == expected
    row = view.row("new_entry")
    assert row is not None and row.type_name == type_name
    assert layout_names(view) == ["new_entry"]
    assert bt.IsModified()


@pytest.mark.parametrize("type_name", USER_TYPES)
def test_add_entry_dialog_defaults(bt, view, dialogs, type_name):
    dialogs.dialog_handler = accept_add_dialog("defaulted")
    click_add(view, type_name)
    assert bt.GetEntryType("defaulted") == type_name
    assert bt.GetEntry("defaulted") == ENTRY_TYPES[type_name].default


@pytest.mark.parametrize(
    "type_name, editor_class",
    [
        ("Integer", QSpinBox), ("Double", QDoubleSpinBox), ("String", QLineEdit), ("Bool", QCheckBox),
        ("List", QLineEdit), ("Dictionary", QLineEdit), ("Set", QLineEdit),
    ],
)
def test_add_entry_dialog_editor_kinds(bt, view, dialogs, type_name, editor_class):
    shown = []
    dialogs.dialog_handler = lambda dlg: (shown.append(dlg), False)[-1]
    click_add(view, type_name)
    assert len(shown) == 1
    dlg = shown[0]
    assert isinstance(dlg, AddEntryDialog)
    assert type_name in dlg.windowTitle()
    assert isinstance(dlg.name_edit, QLineEdit)
    assert isinstance(dlg.value_editor, editor_class)
    if type_name == "Integer":
        assert dlg.value_editor.minimum() == INT_MIN
        assert dlg.value_editor.maximum() == INT_MAX
    assert dlg.error_label.isHidden()
    assert bt.GetEntryNames() == []


def test_add_entry_cancelled_adds_nothing(bt, view, dialogs, tmp_path):
    mark_saved(bt, tmp_path)
    dialogs.dialog_handler = None  # reject
    click_add(view, "Integer")
    assert "dialog" in dialogs.kinds()
    assert bt.GetEntryNames() == []
    assert view.rows() == []
    assert not bt.IsModified()


@pytest.mark.parametrize("bad_name", ["", "   ", "a.b", "a/b", "existing"])
def test_add_entry_dialog_rejects_invalid_names(bt, view, dialogs, bad_name):
    bt.AddEntry("existing", "Integer", 5)
    outcome = {}

    def handler(dlg):
        dlg.name_edit.setText(bad_name)
        dlg.value_editor.setValue(1)
        dlg._try_accept()
        outcome["accepted"] = dlg.result() == QDialog.DialogCode.Accepted
        outcome["error_shown"] = not dlg.error_label.isHidden()
        outcome["error_text"] = dlg.error_label.text()
        # the user corrects the name and presses OK again
        dlg.name_edit.setText("fixed")
        dlg._try_accept()
        return dlg.result() == QDialog.DialogCode.Accepted

    dialogs.dialog_handler = handler
    click_add(view, "Integer")
    assert outcome["accepted"] is False
    assert outcome["error_shown"] is True
    assert outcome["error_text"]
    assert bt.GetEntryNames() == ["existing", "fixed"]
    assert bt.GetEntry("existing") == 5
    assert bt.GetEntry("fixed") == 1


def test_add_entry_dialog_strips_surrounding_spaces_of_name(bt, view, dialogs):
    dialogs.dialog_handler = accept_add_dialog("  padded  ", "x")
    click_add(view, "String")
    assert bt.GetEntryNames() == ["padded"]


@pytest.mark.parametrize(
    "type_name, text",
    [
        ("List", "{'a': 1}"),
        ("List", "not python"),
        ("List", "[1, 2"),
        ("Dictionary", "[1, 2]"),
        ("Dictionary", "{'a': }"),
        ("Set", "[[1], [2]]"),
        ("Set", "{'a': 1}"),
    ],
)
def test_add_entry_dialog_rejects_bad_literals(bt, view, dialogs, type_name, text):
    outcome = {}

    def handler(dlg):
        dlg.name_edit.setText("container")
        dlg.value_editor.setText(text)
        dlg._try_accept()
        outcome["accepted"] = dlg.result() == QDialog.DialogCode.Accepted
        outcome["error_shown"] = not dlg.error_label.isHidden()
        outcome["error_text"] = dlg.error_label.text()
        return outcome["accepted"]

    dialogs.dialog_handler = handler
    click_add(view, type_name)
    assert outcome["accepted"] is False
    assert outcome["error_shown"] is True
    assert outcome["error_text"]
    assert not bt.HasEntry("container")
    assert view.rows() == []


# ============================================================================ selection / removal
def _make_rows(bt, names=("a", "b", "c")):
    for index, name in enumerate(names):
        bt.SetEntry(index, name)


def test_select_all_checks_and_unchecks_every_row(bt, view):
    _make_rows(bt)
    view.select_all.click()
    assert view.select_all.isChecked()
    assert all(row.is_selected() and row.select_box.isChecked() for row in view.rows())
    view.select_all.click()
    assert not view.select_all.isChecked()
    assert not any(row.is_selected() for row in view.rows())


def test_toggling_rows_updates_select_all(bt, view):
    _make_rows(bt)
    rows = view.rows()
    for row in rows[:-1]:
        row.select_box.click()
        assert not view.select_all.isChecked()
    rows[-1].select_box.click()
    assert view.select_all.isChecked()
    rows[0].select_box.click()
    assert not view.select_all.isChecked()
    rows[0].select_box.click()
    assert view.select_all.isChecked()


def test_select_all_with_no_rows_stays_unchecked(view):
    view.select_all.click()
    assert not view.select_all.isChecked()


def test_new_entry_clears_select_all(bt, view):
    _make_rows(bt)
    view.select_all.click()
    assert view.select_all.isChecked()
    bt.SetEntry(1, "newcomer")
    assert not view.select_all.isChecked()
    assert not view.row("newcomer").is_selected()
    assert all(view.row(name).is_selected() for name in "abc")


def test_removing_the_only_unselected_entry_checks_select_all(bt, view):
    _make_rows(bt)
    view.row("b").select_box.click()
    view.row("c").select_box.click()
    assert not view.select_all.isChecked()
    bt.RemoveEntry("a")
    assert view.select_all.isChecked()


def test_remove_entries_removes_only_selected(bt, view, tmp_path):
    _make_rows(bt, ("a", "b", "c", "d"))
    mark_saved(bt, tmp_path)
    view.row("b").select_box.click()
    view.row("d").select_box.click()
    view.remove_button.click()
    assert bt.GetEntryNames() == ["a", "c"]
    assert row_names(view) == ["a", "c"] and layout_names(view) == ["a", "c"]
    assert not any(row.is_selected() for row in view.rows())
    assert not view.select_all.isChecked()
    store = bt.blackboardStore()
    assert storage_keys(store.namespace()) == {store.key("a"), store.key("c")}
    assert bt.IsModified()


def test_remove_entries_after_select_all_removes_everything(bt, view):
    _make_rows(bt)
    view.select_all.click()
    view.remove_button.click()
    assert bt.GetEntryNames() == []
    assert view.rows() == [] and layout_names(view) == []
    assert not view.select_all.isChecked()


def test_remove_entries_with_nothing_selected_is_a_no_op(bt, view, tmp_path):
    _make_rows(bt)
    mark_saved(bt, tmp_path)
    view.remove_button.click()
    assert bt.GetEntryNames() == ["a", "b", "c"]
    assert not bt.IsModified()


def test_retype_keeps_row_selection(bt, view):
    _make_rows(bt)
    view.row("b").select_box.click()
    bt.blackboardStore().replace("b", "Bool", True)
    row = view.row("b")
    assert row.type_name == "Bool"
    assert row.is_selected()


# ============================================================================ editing values in the view
@pytest.mark.parametrize(
    "type_name, initial, edit, expected",
    [
        ("Integer", 1, lambda w: w.setValue(77), 77),
        ("Integer", 1, lambda w: w.setValue(INT_MIN), INT_MIN),
        ("Double", 1.0, lambda w: w.setValue(-2.75), -2.75),
        ("String", "old", lambda w: w.setText("new text"), "new text"),
        ("Bool", False, lambda w: w.setChecked(True), True),
        ("Bool", True, lambda w: w.setChecked(False), False),
    ],
)
def test_editing_value_widgets_updates_store_immediately(bt, view, tmp_path, type_name, initial, edit, expected):
    bt.AddEntry("entry", type_name, initial)
    mark_saved(bt, tmp_path)
    edit(view.row("entry").value_widget)
    value = bt.GetEntry("entry")
    assert value == expected and type(value) is type(expected)
    assert bt.GetEntryType("entry") == type_name
    assert bt.IsModified()


def test_typing_into_integer_and_string_rows(qtbot, bt, view):
    bt.AddEntry("number", "Integer", 0)
    bt.AddEntry("word", "String", "")
    spin = view.row("number").value_widget
    spin.selectAll()
    qtbot.keyClicks(spin, "123")
    assert bt.GetEntry("number") == 123
    line = view.row("word").value_widget
    qtbot.keyClicks(line, "abc")
    assert bt.GetEntry("word") == "abc"
    qtbot.keyClick(line, Qt.Key.Key_Backspace)
    assert bt.GetEntry("word") == "ab"


def test_bool_checkbox_text_shows_value(bt, view):
    bt.AddEntry("flag", "Bool", False)
    box = view.row("flag").value_widget
    assert box.text() == "False" and not box.isChecked()
    box.click()
    assert bt.GetEntry("flag") is True
    assert box.text() == "True"
    bt.SetEntry(False, "flag")
    assert box.text() == "False" and not box.isChecked()


def test_object_rows_are_read_only(qtbot, bt, view, tmp_path):
    thing = Opaque()
    bt.SetEntry(thing, "thing")
    row = view.row("thing")
    widget = row.value_widget
    assert isinstance(widget, QLineEdit)
    assert widget.isReadOnly()
    assert widget.text() == "Opaque()"
    mark_saved(bt, tmp_path)
    qtbot.keyClicks(widget, "typed")
    assert bt.GetEntry("thing") is thing
    assert widget.text() == "Opaque()"
    assert not bt.IsModified()
    bt.SetEntry(12345678901234, "thing")
    assert widget.text() == "12345678901234"


def test_object_row_shortens_long_repr(bt, view):
    bt.SetEntry(10**600, "huge")
    assert bt.GetEntryType("huge") == "Object"
    text = view.row("huge").value_widget.text()
    assert len(text) <= 500 and "..." in text  # a bounded repr (reprlib) of the huge value
    assert text.startswith("1000")


def test_row_display_does_not_echo_back_into_store(bt, view):
    """refresh_value shows the store's value without writing the (rounded) display value back."""
    precise = 0.123456789123
    bt.SetEntry(precise, "precise")
    spin = view.row("precise").value_widget
    assert spin.value() == pytest.approx(precise, abs=1e-8)
    assert bt.GetEntry("precise") == precise


def test_entry_edited_signal_only_for_ui_edits(bt, view, dialogs):
    edited = []
    view.entryEdited.connect(edited.append)
    bt.SetEntry(1, "code")
    bt.SetEntry(2, "code")
    bt.blackboardStore().rename("code", "code2")
    assert edited == []
    view.row("code2").value_widget.setValue(5)
    assert edited == ["code2"]
    dialogs.dialog_handler = accept_add_dialog("ui", "text")
    click_add(view, "String")
    assert edited == ["code2", "ui"]


def test_rename_via_value_name(qtbot, bt, view, tmp_path):
    bt.AddEntry("a", "Integer", 1)
    bt.AddEntry("old", "List", [1, 2])
    bt.AddEntry("c", "Integer", 3)
    mark_saved(bt, tmp_path)
    store = bt.blackboardStore()
    old_key = store.key("old")
    row = view.row("old")
    row.name_edit.selectAll()
    qtbot.keyClicks(row.name_edit, "new name")
    qtbot.keyClick(row.name_edit, Qt.Key.Key_Return)
    assert bt.GetEntryNames() == ["a", "new name", "c"]
    assert bt.GetEntry("new name") == [1, 2]
    assert bt.GetEntryType("new name") == "List"
    assert not bt.HasEntry("old")
    assert old_key not in Blackboard.storage
    assert Blackboard.storage[store.key("new name")] == [1, 2]
    assert view.row("new name") is row and view.row("old") is None
    assert row.name == "new name" and row.name_edit.text() == "new name"
    assert layout_names(view) == ["a", "new name", "c"]
    assert bt.IsModified()


def test_rename_via_value_name_strips_spaces(bt, view):
    bt.AddEntry("old", "Integer", 1)
    row = view.row("old")
    row.name_edit.setText("  spaced  ")
    row.name_edit.editingFinished.emit()
    assert bt.GetEntryNames() == ["spaced"]
    assert row.name_edit.text() == "spaced"


@pytest.mark.parametrize("bad", ["", "   ", "a.b", "a/b", "other"])
def test_invalid_rename_reverts_value_name(bt, view, tmp_path, bad):
    bt.AddEntry("entry", "Integer", 4)
    bt.AddEntry("other", "Integer", 5)
    mark_saved(bt, tmp_path)
    row = view.row("entry")
    row.name_edit.setText(bad)
    row.name_edit.editingFinished.emit()
    assert row.name_edit.text() == "entry"
    assert row.name == "entry"
    assert bt.GetEntryNames() == ["entry", "other"]
    assert bt.GetEntry("entry") == 4 and bt.GetEntry("other") == 5
    assert not bt.IsModified()


@pytest.mark.parametrize(
    "type_name, initial, text, expected, items",
    [
        ("List", [1], "[4, 'five', 6.5]", [4, "five", 6.5], ["4", "five", "6.5"]),
        ("Dictionary", {}, "{'x': 1, 'y': [2]}", {"x": 1, "y": [2]}, ["x: 1", "y: [2]"]),
        ("Set", {9}, "{3, 1, 2}", {1, 2, 3}, ["1", "2", "3"]),
    ],
)
def test_container_edit_button_changes_value(bt, view, dialogs, tmp_path, type_name, initial, text, expected, items):
    bt.AddEntry("box", type_name, initial)
    mark_saved(bt, tmp_path)
    shown = []

    def handler(dlg):
        shown.append(dlg)
        assert isinstance(dlg, EditContainerDialog)
        dlg.text_edit.setText(text)
        dlg._try_accept()
        return dlg.result() == QDialog.DialogCode.Accepted

    dialogs.dialog_handler = handler
    row = view.row("box")
    row.edit_button.click()
    assert len(shown) == 1
    assert bt.GetEntry("box") == expected
    assert bt.GetEntryType("box") == type_name
    combo = row.value_widget
    assert [combo.itemText(i) for i in range(combo.count())] == items
    assert bt.IsModified()


@pytest.mark.parametrize(
    "type_name, value",
    [("List", []), ("List", [1, "a", (2, 3)]), ("Dictionary", {"a": {1, 2}}), ("Set", set()), ("Set", {"x", 1})],
)
def test_container_edit_dialog_prefilled_and_accepts_unchanged(bt, view, dialogs, type_name, value):
    bt.AddEntry("box", type_name, value)
    seen = {}

    def handler(dlg):
        seen["text"] = dlg.text_edit.text()
        dlg._try_accept()
        seen["error"] = dlg.error_label.text()
        return dlg.result() == QDialog.DialogCode.Accepted

    dialogs.dialog_handler = handler
    view.row("box").edit_button.click()
    assert seen["text"] == repr(bt.GetEntry("box"))
    assert seen["error"] == ""
    assert bt.GetEntry("box") == coerce_value(type_name, value)


@pytest.mark.parametrize("text", ["{'a': 1}", "garbage", "[1,"])
def test_container_edit_dialog_invalid_literal_keeps_value(bt, view, dialogs, tmp_path, text):
    bt.AddEntry("box", "List", [1, 2])
    mark_saved(bt, tmp_path)
    outcome = {}

    def handler(dlg):
        dlg.text_edit.setText(text)
        dlg._try_accept()
        outcome["accepted"] = dlg.result() == QDialog.DialogCode.Accepted
        outcome["error_shown"] = not dlg.error_label.isHidden() and bool(dlg.error_label.text())
        return outcome["accepted"]

    dialogs.dialog_handler = handler
    view.row("box").edit_button.click()
    assert outcome == {"accepted": False, "error_shown": True}
    assert bt.GetEntry("box") == [1, 2]
    assert not bt.IsModified()


def test_container_edit_dialog_cancel_keeps_value(bt, view, dialogs, tmp_path):
    bt.AddEntry("box", "Dictionary", {"a": 1})
    mark_saved(bt, tmp_path)

    def handler(dlg):
        dlg.text_edit.setText("{'b': 2}")
        return False

    dialogs.dialog_handler = handler
    view.row("box").edit_button.click()
    assert bt.GetEntry("box") == {"a": 1}
    assert not bt.IsModified()


@pytest.mark.parametrize("action", ["add", "remove", "rename", "edit_value", "edit_container"])
def test_ui_edits_mark_tree_modified(bt, view, dialogs, tmp_path, action):
    bt.AddEntry("number", "Integer", 1)
    bt.AddEntry("items", "List", [1])
    mark_saved(bt, tmp_path)
    modified_signals = []
    bt.modifiedChanged.connect(modified_signals.append)
    if action == "add":
        dialogs.dialog_handler = accept_add_dialog("added", "x")
        click_add(view, "String")
    elif action == "remove":
        view.row("number").select_box.click()
        view.remove_button.click()
    elif action == "rename":
        row = view.row("number")
        row.name_edit.setText("renamed")
        row.name_edit.editingFinished.emit()
    elif action == "edit_value":
        view.row("number").value_widget.setValue(9)
    else:
        def handler(dlg):
            dlg.text_edit.setText("[1, 2, 3]")
            dlg._try_accept()
            return dlg.result() == QDialog.DialogCode.Accepted

        dialogs.dialog_handler = handler
        view.row("items").edit_button.click()
    assert bt.IsModified()
    assert modified_signals == [True]


def test_selection_changes_do_not_mark_tree_modified(bt, view, tmp_path):
    _make_rows(bt)
    mark_saved(bt, tmp_path)
    view.select_all.click()
    view.row("a").select_box.click()
    assert not bt.IsModified()
