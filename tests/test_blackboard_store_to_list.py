"""BlackboardStore.to_list encodes the stored values directly, under the store's lock."""

from __future__ import annotations

import copy
import logging
import threading

import pytest

from behavior_tree_widget.blackboard import BlackboardStore


class Opaque:
    """A value no tree file can hold."""


@pytest.fixture
def store(qapp):
    store = BlackboardStore()
    yield store
    store.dispose(notify=False)


def test_to_list_encodes_without_copying_the_values_first(store, monkeypatch):
    store.add("path", "List", [1, [2, 3], {"a": (4, 5)}])
    store.add("tags", "Set", {"x", "y"})
    store.add("limits", "Dictionary", {1: "one"}, reset=True)

    def no_copy(*_args, **_kwargs):
        raise AssertionError("to_list must not copy the stored values")

    monkeypatch.setattr(copy, "deepcopy", no_copy)
    assert store.to_list() == [
        {"name": "path", "type": "List", "value": [1, [2, 3], {"a": {"__tuple__": [4, 5]}}]},
        {"name": "tags", "type": "Set", "value": {"__set__": ["x", "y"]}},
        {"name": "limits", "type": "Dictionary", "value": {"__dict__": [[1, "one"]]}, "reset": True},
    ]


def test_to_list_returns_structures_independent_of_the_store(store):
    store.add("path", "List", [[1, 2], {"k": [3]}])
    store.add("table", "Dictionary", {"row": [1]})
    items = store.to_list()
    items[0]["value"][0].append(99)
    items[0]["value"][1]["k"].append(99)
    items[1]["value"]["row"].append(99)
    assert store.get("path") == [[1, 2], {"k": [3]}]
    assert store.get("table") == {"row": [1]}


def test_to_list_skips_objects_and_unsaveable_values_in_entry_order(store, caplog):
    store.add("device", "Object", Opaque())
    store.add("bad", "List", [Opaque()])
    store.add("ok", "Integer", 3)
    problems: list[str] = []
    with caplog.at_level(logging.DEBUG, logger="behavior_tree_widget"):
        assert store.to_list(problems) == [{"name": "ok", "type": "Integer", "value": 3}]
    assert len(problems) == 1 and problems[0].startswith("blackboard entry 'bad' was not saved")
    messages = [record.getMessage() for record in caplog.records]
    assert "Object entry" in messages[0] and "'device'" in messages[0]
    assert messages[1] == problems[0]
    caplog.clear()
    problems.clear()
    with caplog.at_level(logging.WARNING, logger="behavior_tree_widget"):
        store.to_list(problems, quiet=True)
    assert problems and not caplog.records, "quiet: reported in problems only"


def test_to_list_sees_a_consistent_blackboard_while_another_thread_writes(store):
    store.add("counter", "Integer", 0)
    store.add("history", "List", [])
    stop = threading.Event()

    def writer():
        value = 0
        while not stop.is_set():
            value += 1
            store.set("history", list(range(value % 50)))
            store.set("counter", value)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(200):
            items = {item["name"]: item["value"] for item in store.to_list()}
            assert items["history"] == list(range(len(items["history"])))
    finally:
        stop.set()
        thread.join()
