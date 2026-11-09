"""BlackboardStore.revision() / batch() and the blackboard file writer that relies on them."""

from __future__ import annotations

import pytest

from behavior_tree_widget.blackboard import BlackboardStore


@pytest.fixture
def store(qapp):
    store = BlackboardStore()
    yield store
    store.dispose(notify=False)


def test_every_change_counts_and_saved_changes_are_counted_separately(store):
    seen = [store.revision()]

    def step():
        seen.append(store.revision())
        assert seen[-1] > seen[-2]

    store.add("a", "Integer", 1)
    step()
    store.set("a", 2)
    step()
    store.replace("a", "Double", 2.5)
    step()
    store.rename("a", "b")
    step()
    store.add("c", "String", "x")
    step()
    store.move("c", -1)
    step()
    store.set_reset("c", True)
    step()
    store.remove("c")
    step()
    assert store.revision(saved=True) == store.revision()  # no Object entry was involved


def test_object_entries_do_not_count_as_saved_changes(store):
    store.add("handle", "Object", object())
    saved = store.revision(saved=True)
    total = store.revision()
    store.set("handle", object())
    store.rename("handle", "device")
    store.set_reset("device", True)
    store.move("device", 0)
    assert store.revision() > total
    assert store.revision(saved=True) == saved
    store.replace("device", "Integer", 3)  # an Object entry becoming a saved one
    assert store.revision(saved=True) > saved


def test_unchanged_operations_do_not_count(store):
    store.add("a", "Integer", 1)
    store.add("b", "Integer", 2)
    before = store.revision()
    store.move("a", -1)  # already first
    store.reorder(["a", "b"])  # same order
    store.set_reset("a", False)  # already off
    store.rename("a", "a")
    assert store.revision() == before


def test_batch_emits_once_at_the_end_of_the_outermost_batch(store):
    finished = []
    store.batchFinished.connect(lambda: finished.append(store.in_batch()))
    assert not store.in_batch()
    with store.batch():
        assert store.in_batch()
        with store.batch():
            store.add("a", "Integer", 1)
        assert finished == []
        assert store.in_batch()
        store.add("b", "Integer", 2)
    assert finished == [False]  # emitted after the batch ended
    assert not store.in_batch()


def test_batch_ends_when_the_block_raises(store):
    finished = []
    store.batchFinished.connect(lambda: finished.append(True))
    with pytest.raises(ValueError):
        with store.batch():
            raise ValueError("boom")
    assert finished == [True]
    assert not store.in_batch()


@pytest.mark.parametrize("operation", ["restore", "load_list", "clear", "dispose"])
def test_bulk_operations_are_one_batch(store, operation):
    store.add("a", "Integer", 1)
    store.add("b", "String", "x")
    snapshot = store.snapshot()
    items = store.to_list()
    store.add("c", "Bool", True)
    removed = []
    in_batch_during_signals = []
    finished = []
    store.entryRemoved.connect(lambda name: (removed.append(name), in_batch_during_signals.append(store.in_batch())))
    store.batchFinished.connect(lambda: finished.append(True))
    if operation == "restore":
        store.restore(snapshot)
    elif operation == "load_list":
        store.load_list(items, replace=True)
    elif operation == "clear":
        store.clear()
    else:
        store.dispose()
    assert removed, "the operation removes entries"
    assert all(in_batch_during_signals)
    assert finished == [True]


def test_object_only_changes_do_not_rewrite_or_encode_the_blackboard_file(bt, qtbot, monkeypatch):
    bt.AddEntry("speed", "Double", 1.0)
    saved = []
    bt.blackboardSaved.connect(saved.append)
    qtbot.waitUntil(lambda: bool(saved), timeout=3000)
    saved.clear()
    calls = []
    original = BlackboardStore.to_list

    def counting(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BlackboardStore, "to_list", counting)
    bt.SetEntry(object(), "device")  # an Object entry: never saved
    bt.SetEntry(object(), "device")
    bt._save_blackboard_now()
    bt._blackboard_save_pending = True
    bt._save_blackboard_now()
    assert saved == []
    assert calls == [], "nothing the file holds changed, so the blackboard is not encoded again"
    bt.SetEntry(2.0, "speed")
    qtbot.waitUntil(lambda: bool(saved), timeout=3000)
    assert calls, "a saved entry changed: the file is written"
