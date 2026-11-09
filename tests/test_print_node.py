"""The built-in Print Message node: writes a literal or the value of a blackboard entry to the tree's log.

Requirement: a print message node that prints either a string literal to the log or the value
of a key in the blackboard; its UI is like the other nodes' and has a drop-down choosing the
log level, DEBUG (3) by default.
"""

from __future__ import annotations

import json
import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit

from behavior_tree_widget import (
    BehaviorTreeWidget,
    LeafNodeWidget,
    LogLevel,
    NodeStatus,
    PrintMessageNodeWidget,
    UnknownLeafNodeWidget,
    flush_logs,
)
from behavior_tree_widget.print_node import PRINT_MESSAGE, value_text

from conftest import AllFields, fast_config, read_json, run_until_idle

LEVEL_LABELS = ["0: None", "1: ERROR", "2: WARNING", "3: DEBUG", "4: INFO"]
RED = "#c62828"


# ============================================================================ helpers
@pytest.fixture
def logs(monkeypatch, tmp_path):
    folder = tmp_path / "logs"
    monkeypatch.setattr(BehaviorTreeWidget, "LOG_FOLDER", str(folder))
    return folder


@pytest.fixture
def tree(logs, make_widget, tmp_path, qtbot):
    widget = make_widget()
    assert widget.NewTree(str(tmp_path / "printer.json"))
    fast_config(widget)
    qtbot.wait(1)
    return widget


def combo(node, name: str) -> QComboBox:
    box = node.findChild(QComboBox, name)
    assert box is not None, name
    return box


def texts(box: QComboBox) -> list[str]:
    return [box.itemText(index) for index in range(box.count())]


def choose(box: QComboBox, text: str) -> None:
    """Pick ``text`` as the user does (emits currentIndexChanged)."""
    index = box.findText(text)
    assert index >= 0, f"{text!r} not in {texts(box)}"
    box.setCurrentIndex(index)


def message_shown(node) -> bool:
    edit = node.findChild(QLineEdit, "Message")
    label = node.findChild(QLabel, "MessageLabel")
    assert label.isVisibleTo(node) is edit.isVisibleTo(node)
    return edit.isVisibleTo(node)


def add_print(bt, x=0.0, connect=True, **settings) -> PrintMessageNodeWidget:
    node = bt.AddNode(PRINT_MESSAGE, x, 220)
    if "level" in settings:
        node.SetLevel(settings["level"])
    if "source" in settings:
        node.SetSource(settings["source"])
    if "message" in settings:
        node.SetMessage(settings["message"])
    if connect:
        bt.Connect(bt.GetRootNode(), node)
    return node


def record(signal) -> list:
    values: list = []
    signal.connect(lambda *args: values.append(args[0] if len(args) == 1 else args))
    return values


def execute(qtbot, bt) -> list:
    results = record(bt.executionFinished)
    bt.Execute()
    run_until_idle(qtbot, bt)
    return results


def log_entries(bt) -> list[tuple[str, str]]:
    assert flush_logs()
    found = []
    with open(bt.GetLogFilePath(), encoding="utf-8") as stream:
        for line in stream.read().splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[2] in ("ERROR", "WARNING", "DEBUG", "INFO"):
                found.append((parts[2], parts[3]))
            elif found:
                found[-1] = (found[-1][0], found[-1][1] + "\n" + line)
    return found


def red(widget) -> bool:
    return widget.palette().color(QPalette.ColorRole.ButtonText).name() == RED


# ============================================================================ the node type
def test_print_message_is_a_built_in_type_with_the_other_built_ins(tree):
    names = tree.GetNodeTypes()
    assert PRINT_MESSAGE == "Print Message"
    assert names.index("UDP Message") < names.index(PRINT_MESSAGE) < names.index("Sub-Tree")
    assert tree.GetNodeType(PRINT_MESSAGE) is PrintMessageNodeWidget
    entries = tree._node_menu_entries()
    assert [entry for entry in entries if entry[1] == PRINT_MESSAGE] == [("log", PRINT_MESSAGE, PRINT_MESSAGE)]
    groups = [group for group, _, _ in entries]
    assert groups.index("network") < groups.index("log") < groups.index("subtree") < groups.index("leaf")


def test_defaults_and_editors(tree):
    node = add_print(tree, connect=False)
    assert isinstance(node, PrintMessageNodeWidget)
    assert node.GetTypeName() == PRINT_MESSAGE and node.GetTitle() == PRINT_MESSAGE
    assert node.findChild(QLabel, "Title").text() == PRINT_MESSAGE
    assert node.HasParentConnection() and not node.HasChildConnections()
    assert node.GetFields() == {"Level": 3, "Source": None, "Message": ""}
    assert node.GetLevel() is LogLevel.DEBUG
    assert node.GetSource() is None and node.UsesLiteral() and node.GetMessage() == ""
    level = combo(node, "LogLevel")
    assert texts(level) == LEVEL_LABELS
    assert [level.itemData(index) for index in range(level.count())] == [0, 1, 2, 3, 4]
    assert level.currentText() == "3: DEBUG"
    source = combo(node, "Source")
    assert texts(source) == ["Literal"] and source.currentText() == "Literal"
    assert isinstance(node.findChild(QLineEdit, "Message"), QLineEdit)
    assert message_shown(node)
    assert not red(source)


def test_the_source_lists_literal_and_every_entry(tree):
    node = add_print(tree, connect=False)
    tree.AddEntry("count", "Integer", 1)
    tree.AddEntry("name", "String", "bob")
    source = combo(node, "Source")
    assert texts(source) == ["Literal", "count", "name"]
    tree.AddEntry("flag", "Bool", True)
    assert texts(source) == ["Literal", "count", "name", "flag"]
    tree.MoveEntry("flag", -2)
    assert texts(source) == ["Literal", "flag", "count", "name"]
    tree.RemoveEntry("count")
    assert texts(source) == ["Literal", "flag", "name"]
    later = add_print(tree, x=300, connect=False)  # a node added later lists the entries too
    assert texts(combo(later, "Source")) == ["Literal", "flag", "name"]


def test_the_message_is_shown_only_for_literal(tree):
    tree.AddEntry("count", "Integer", 1)
    node = add_print(tree, connect=False)
    source = combo(node, "Source")
    choose(source, "count")
    assert node.GetSource() == "count" and not node.UsesLiteral()
    assert not message_shown(node)
    choose(source, "Literal")
    assert node.GetSource() is None and node.UsesLiteral()
    assert message_shown(node)
    node.SetSource("count")
    assert source.currentText() == "count" and not message_shown(node)
    node.SetSource(None)
    assert source.currentText() == "Literal" and message_shown(node)


def test_editing_the_node_marks_the_tree_modified(tree, qtbot):
    tree.AddEntry("count", "Integer", 1)
    node = add_print(tree, connect=False)
    assert tree.SaveTree(tree.GetFilePath())
    assert not tree.IsModified()
    edited = record(node.fieldEdited)

    choose(combo(node, "LogLevel"), "1: ERROR")
    assert node.GetLevel() is LogLevel.ERROR and node.GetField("Level") == 1
    assert tree.IsModified()
    assert tree.SaveTree(tree.GetFilePath())

    choose(combo(node, "Source"), "count")
    assert node.GetSource() == "count"
    assert tree.IsModified()
    choose(combo(node, "Source"), "Literal")
    assert tree.SaveTree(tree.GetFilePath())

    edit = node.findChild(QLineEdit, "Message")
    edit.setFocus()
    QTest.keyClicks(edit, "hi there")
    assert node.GetMessage() == "hi there"
    assert tree.IsModified()
    assert edited == ["Level", "Source", "Source"] + ["Message"] * len("hi there")


def test_setters_update_the_editors_and_check_their_values(tree):
    node = add_print(tree, connect=False)
    node.SetLevel("warning")
    assert node.GetLevel() is LogLevel.WARNING and combo(node, "LogLevel").currentText() == "2: WARNING"
    node.SetLevel(LogLevel.INFO)
    assert combo(node, "LogLevel").currentText() == "4: INFO"
    node.SetMessage("text")
    assert node.findChild(QLineEdit, "Message").text() == "text"
    with pytest.raises(ValueError):
        node.SetLevel(9)
    with pytest.raises(TypeError):
        node.SetSource(5)
    with pytest.raises(TypeError):
        node.SetMessage(None)
    assert node.GetFields() == {"Level": 4, "Source": None, "Message": "text"}
    node.SetSource("")  # empty: the literal
    assert node.GetSource() is None


# ============================================================================ printing
def test_a_literal_is_printed_at_the_chosen_level(tree, qtbot):
    add_print(tree, message="hello world")
    add_print(tree, x=250, level="error", message="an error")
    add_print(tree, x=500, level=LogLevel.INFO, message="for info")
    add_print(tree, x=750, level=2, message="a warning")
    debug, error, info, warning = (record(tree.debugLogged), record(tree.errorLogged), record(tree.infoLogged),
                                   record(tree.warningLogged))
    assert execute(qtbot, tree) == ["Succeeded"]
    qtbot.waitUntil(lambda: "a warning" in warning, timeout=2000)
    assert debug == ["hello world"] and error == ["an error"] and warning == ["a warning"]
    assert "for info" in info
    found = log_entries(tree)
    for entry in [("DEBUG", "hello world"), ("ERROR", "an error"), ("INFO", "for info"), ("WARNING", "a warning")]:
        assert entry in found
    assert all(node.GetStatus() is NodeStatus.SUCCEEDED for node in tree.GetRootNode().GetChildren())


def test_an_empty_literal_is_printed_as_an_empty_message(tree, qtbot):
    add_print(tree)
    debug = record(tree.debugLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    qtbot.waitUntil(lambda: debug == [""], timeout=2000)


ENTRIES = [
    ("count", "Integer", 5, "count = 5"),
    ("ratio", "Double", 1.5, "ratio = 1.5"),
    ("name", "String", "it's bob", "name = it's bob"),
    ("empty", "String", "", "empty = "),
    ("flag", "Bool", True, "flag = True"),
    ("items", "List", [1, "two"], "items = [1, 'two']"),
    ("table", "Dictionary", {"k": 1}, "table = {'k': 1}"),
    ("bag", "Set", {3}, "bag = {3}"),
]


def test_the_value_of_an_entry_is_printed_with_its_name(tree, qtbot):
    for index, (name, type_name, value, _) in enumerate(ENTRIES):
        tree.AddEntry(name, type_name, value)
        add_print(tree, x=index * 250, source=name)
    debug = record(tree.debugLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    qtbot.waitUntil(lambda: len(debug) == len(ENTRIES), timeout=2000)
    assert debug == [text for _, _, _, text in ENTRIES]
    assert [entry for entry in log_entries(tree) if entry[0] == "DEBUG"] == [("DEBUG", text) for _, _, _, text in ENTRIES]


def test_the_value_printed_is_the_value_at_run_time(tree, qtbot):
    tree.AddEntry("count", "Integer", 1)
    node = add_print(tree, source="count")
    assert node.message_text(tree) == "count = 1"
    tree.SetEntry(42, "count")
    debug = record(tree.debugLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    qtbot.waitUntil(lambda: debug == ["count = 42"], timeout=2000)


def test_value_text():
    assert value_text("plain") == "plain"
    assert value_text(3) == "3"
    assert value_text([1, "a"]) == "[1, 'a']"
    assert value_text(None) == "None"


def test_an_object_entry_is_printed_without_raising(tree, qtbot):
    class Thing:
        def __repr__(self):
            raise RuntimeError("no repr")

    tree.SetEntry(Thing(), "thing")
    add_print(tree, source="thing")
    debug = record(tree.debugLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    qtbot.waitUntil(lambda: len(debug) == 1, timeout=2000)
    assert debug[0].startswith("thing = <Thing ")  # e.g. "<Thing instance at 0x...>" (reprlib)
    assert tree.GetRootNode().GetChildren()[0].GetStatus() is NodeStatus.SUCCEEDED


# ============================================================================ level and threshold
def test_level_none_prints_nothing_and_succeeds(tree, qtbot):
    node = add_print(tree, level=LogLevel.NONE, message="never printed")
    missing = add_print(tree, x=250, level=0, source="no_such_entry")
    every = record(tree.messageLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    QApplication.processEvents()
    assert node.GetStatus() is NodeStatus.SUCCEEDED and missing.GetStatus() is NodeStatus.SUCCEEDED
    assert not any("never printed" in message or "no_such_entry" in message for _, message in every)
    assert not any(level == "DEBUG" or "never printed" in message for level, message in log_entries(tree))


def test_a_message_below_the_log_level_is_dropped_and_the_node_succeeds(tree, qtbot):
    tree.SetLogLevel(LogLevel.WARNING)
    quiet = add_print(tree, message="debug text")  # DEBUG: dropped
    loud = add_print(tree, x=250, level="error", message="error text")
    every = record(tree.messageLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    qtbot.waitUntil(lambda: (1, "error text") in every, timeout=2000)
    assert all(message != "debug text" for _, message in every)
    assert quiet.GetStatus() is NodeStatus.SUCCEEDED and loud.GetStatus() is NodeStatus.SUCCEEDED
    found = log_entries(tree)
    assert ("ERROR", "error text") in found
    assert all(message != "debug text" for _, message in found)


# ============================================================================ missing entries / renames
def test_a_missing_entry_is_shown_in_red_and_the_node_fails_with_an_error(tree, qtbot):
    tree.AddEntry("count", "Integer", 1)
    node = add_print(tree, source="count")
    source = combo(node, "Source")
    tree.RemoveEntry("count")
    assert node.GetSource() == "count"  # the choice is kept
    assert texts(source) == ["Literal", "count (missing)"]
    assert source.currentText() == "count (missing)"
    assert red(source)
    brush = source.itemData(1, Qt.ItemDataRole.ForegroundRole)
    assert brush is not None and brush.color().name() == RED
    assert "count" in source.toolTip()
    assert not message_shown(node)

    errors = record(tree.errorLogged)
    assert execute(qtbot, tree) == ["Failed"]
    assert node.GetStatus() is NodeStatus.FAILED
    assert "the blackboard has no entry named 'count' (Source)" in node.GetError()
    qtbot.waitUntil(lambda: len(errors) == 1, timeout=2000)
    assert errors[0].startswith(
        "'Print Message' (Print Message): OnRun raised ValueError: the blackboard has no entry named 'count' (Source)"
    )

    tree.AddEntry("count", "Integer", 7)  # back: nothing was lost
    assert texts(source) == ["Literal", "count"] and source.currentText() == "count"
    assert not red(source)
    debug = record(tree.debugLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    qtbot.waitUntil(lambda: debug == ["count = 7"], timeout=2000)


def test_a_source_set_from_code_that_does_not_exist_is_missing(tree):
    node = add_print(tree, connect=False, source="ghost")
    assert combo(node, "Source").currentText() == "ghost (missing)"
    assert red(combo(node, "Source"))


def test_the_node_follows_a_renamed_entry(tree, qtbot):
    tree.AddEntry("count", "Integer", 3)
    node = add_print(tree, source="count")
    other = add_print(tree, x=250, message="literal stays")
    assert tree.SaveTree(tree.GetFilePath())
    changed = record(node.fieldChanged)
    tree.blackboardStore().rename("count", "total")
    assert node.GetSource() == "total"
    assert combo(node, "Source").currentText() == "total"
    assert texts(combo(node, "Source")) == ["Literal", "total"]
    assert not red(combo(node, "Source"))
    assert other.GetSource() is None and combo(other, "Source").currentText() == "Literal"
    assert "Source" in changed
    assert tree.IsModified()  # the node's Source changed: the tree file must be saved
    debug = record(tree.debugLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    qtbot.waitUntil(lambda: debug == ["total = 3", "literal stays"], timeout=2000)


# ============================================================================ persistence
def test_the_choices_are_saved_and_loaded(tree, make_widget):
    tree.AddEntry("count", "Integer", 2)
    literal = add_print(tree, level="error", message="saved text")
    entry = add_print(tree, x=250, level=LogLevel.INFO, source="count")
    plain = add_print(tree, x=500)
    for node, title in ((literal, "lit"), (entry, "ent"), (plain, "plain")):
        node.SetTitle(title)
    assert tree.SaveTree(tree.GetFilePath())
    saved = {node["title"]: node for node in read_json(tree.GetFilePath())["nodes"]}
    assert saved["lit"]["type"] == PRINT_MESSAGE
    assert saved["lit"]["fields"] == {"Level": 1, "Source": None, "Message": "saved text"}
    assert saved["ent"]["fields"] == {"Level": 4, "Source": "count", "Message": ""}
    assert saved["plain"]["fields"] == {"Level": 3, "Source": None, "Message": ""}

    other = make_widget()
    assert other.LoadTree(tree.GetFilePath())
    loaded = {node.GetTitle(): node for node in other.GetRootNode().GetChildren()}
    assert all(type(node) is PrintMessageNodeWidget for node in loaded.values())
    assert loaded["lit"].GetFields() == {"Level": 1, "Source": None, "Message": "saved text"}
    assert combo(loaded["lit"], "LogLevel").currentText() == "1: ERROR"
    assert loaded["lit"].findChild(QLineEdit, "Message").text() == "saved text"
    assert message_shown(loaded["lit"])
    assert loaded["ent"].GetSource() == "count" and loaded["ent"].GetLevel() is LogLevel.INFO
    assert combo(loaded["ent"], "Source").currentText() == "count"
    assert combo(loaded["ent"], "LogLevel").currentText() == "4: INFO"
    assert not message_shown(loaded["ent"])
    assert loaded["plain"].GetLevel() is LogLevel.DEBUG
    assert not other.IsModified()


def test_invalid_saved_values_are_ignored(logs, make_widget, tmp_path):
    document = {
        "format": "behavior_tree_widget",
        "version": 1,
        "nodes": [
            {"id": "root", "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True},
            {"id": "a", "type": PRINT_MESSAGE, "title": "a", "x": 0.0, "y": 220.0,
             "fields": {"Level": 9, "Source": 5, "Message": 3}},
            {"id": "b", "type": PRINT_MESSAGE, "title": "b", "x": 250.0, "y": 220.0,
             "fields": {"Level": "warning", "Source": "", "Message": "ok"}},
        ],
        "connections": [{"parent": "root", "child": "a"}, {"parent": "root", "child": "b"}],
    }
    path = tmp_path / "odd_BT.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    bt = make_widget()
    assert bt.LoadTree(str(path))
    a, b = bt.GetRootNode().GetChildren()
    assert type(a) is PrintMessageNodeWidget and type(b) is PrintMessageNodeWidget
    assert a.GetFields() == {"Level": 3, "Source": None, "Message": ""}
    assert b.GetFields() == {"Level": 2, "Source": None, "Message": "ok"}


# ============================================================================ the name yields
class AppPrint(LeafNodeWidget):
    """An application's own leaf type that was named "Print Message" before it was built in."""

    TYPE_NAME = "Print Message"
    _title = "Print Message"
    _fields = {"recipient": "printer-1"}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


@pytest.mark.parametrize("how", ["node_types", "RegisterNodeType"])
def test_an_application_type_may_take_the_name(logs, make_widget, tmp_path, how):
    if how == "node_types":
        bt = make_widget(node_types=[AllFields, AppPrint])
    else:
        bt = make_widget(node_types=[AllFields])
        bt.RegisterNodeType(AppPrint)
    assert bt.GetNodeType(PRINT_MESSAGE) is AppPrint
    assert bt.GetNodeTypes().count(PRINT_MESSAGE) == 1
    assert [entry for entry in bt._node_menu_entries() if entry[1] == PRINT_MESSAGE] == [
        ("leaf", PRINT_MESSAGE, PRINT_MESSAGE)
    ]
    assert bt.NewTree(str(tmp_path / "app.json"))
    node = bt.AddNode(PRINT_MESSAGE, 0, 220)
    assert type(node) is AppPrint
    node.SetField("recipient", "printer-2")
    bt.Connect(bt.GetRootNode(), node)
    assert bt.SaveTree(bt.GetFilePath())

    other = make_widget(node_types=[AppPrint])
    assert other.LoadTree(bt.GetFilePath())
    loaded = other.GetRootNode().GetChildren()[0]
    assert type(loaded) is AppPrint and loaded.GetFields() == {"recipient": "printer-2"}

    plain = make_widget()  # without the application's type: a placeholder keeping its data
    assert plain.LoadTree(bt.GetFilePath())
    placeholder = plain.GetRootNode().GetChildren()[0]
    assert isinstance(placeholder, UnknownLeafNodeWidget)
    assert placeholder.GetFields() == {"recipient": "printer-2"}


def test_the_built_in_class_can_be_registered_again(tree):
    tree.RegisterNodeType(PrintMessageNodeWidget)
    assert tree.GetNodeType(PRINT_MESSAGE) is PrintMessageNodeWidget
    assert [entry for entry in tree._node_menu_entries() if entry[1] == PRINT_MESSAGE] == [
        ("log", PRINT_MESSAGE, PRINT_MESSAGE)
    ]


def test_another_application_type_cannot_take_the_name_twice(make_widget):
    class OtherPrint(LeafNodeWidget):
        TYPE_NAME = "Print Message"

        def OnRun(self, tree):
            return True

    bt = make_widget(node_types=[AppPrint])
    with pytest.raises(ValueError):
        bt.RegisterNodeType(OtherPrint)
