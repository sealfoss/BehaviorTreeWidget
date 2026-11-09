"""Node title / fields attributes: initial values, SetFields, and the deprecated assignment after construction."""

from __future__ import annotations

import threading
import warnings

import pytest
from PySide6.QtWidgets import QLineEdit, QSpinBox

from behavior_tree_widget import ExecutionCancelled, LeafNodeWidget
from behavior_tree_widget._runctx import RunToken, bound


class Plain(LeafNodeWidget):
    _title = "Plain"
    _fields = {"count": 1, "mode": ["a", "b", "c"]}
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True


class PresetBeforeInit(LeafNodeWidget):
    RUN_IN_THREAD = False

    def __init__(self, parent=None):
        self._title = "Preset"
        self._fields = {"speed": 3}
        super().__init__(parent)

    def OnRun(self, tree):
        return True


@pytest.fixture
def make(qtbot):
    """Create a node owned by qtbot (deleted on the GUI thread, never by a worker thread's GC)."""

    def create(cls):
        node = cls()
        qtbot.addWidget(node)
        return node

    return create


def test_class_attributes_and_assignment_before_init_do_not_warn(make):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        plain = make(Plain)
        preset = make(PresetBeforeInit)
    assert plain.GetTitle() == "Plain" and plain.GetFields() == {"count": 1, "mode": ["a", "b", "c"]}
    assert preset.GetTitle() == "Preset" and preset.GetFields() == {"speed": 3}


@pytest.mark.filterwarnings("ignore:assigning self:DeprecationWarning")
def test_assignment_after_construction_warns_at_the_caller_and_still_applies(make):
    node = make(Plain)
    with pytest.warns(DeprecationWarning, match=r"self\._title .* SetTitle") as caught:
        node._title = "Renamed"
    assert caught[0].filename == __file__, "the warning points at the code that assigned"
    assert node.GetTitle() == "Renamed" and node._title_label.text() == "Renamed"
    with pytest.warns(DeprecationWarning, match=r"self\._fields .* SetFields"):
        node._fields = {"label": "x"}
    assert node.GetFields() == {"label": "x"}
    assert isinstance(node.field_editor("label"), QLineEdit)


def test_set_fields_replaces_fields_and_rebuilds_editors(make):
    node = make(Plain)
    node.SetFieldSelectionIndex("mode", 2)
    changes = []
    node.changed.connect(changes.append)
    fields = {"count": 5, "mode": ["a", "b", "c", "d"], "extra": "text"}
    node.SetFields(fields)
    assert node.GetFields() == fields
    fields["count"] = 99
    assert node.GetField("count") == 5, "the node keeps its own copy of the dict"
    assert node.GetFieldSelectionIndex("mode") == 2, "the selection of a field that is still a list is kept"
    assert isinstance(node.field_editor("count"), QSpinBox) and node.field_editor("count").value() == 5
    assert isinstance(node.field_editor("extra"), QLineEdit)
    assert changes == [False], "an edit on the GUI thread while idle"
    node.SetFields({"mode": "no longer a list"})
    with pytest.raises(TypeError):
        node.GetFieldSelectionIndex("mode")
    node.SetFields({})
    assert node.GetFields() == {} and not node._fields_frame.isVisibleTo(node)


@pytest.mark.parametrize("value", [None, ["a"], "fields", 3])
def test_set_fields_needs_a_dict(make, value):
    node = make(Plain)
    with pytest.raises(TypeError, match="dict"):
        node.SetFields(value)
    assert node.GetFields() == {"count": 1, "mode": ["a", "b", "c"]}


def test_set_fields_from_a_worker_thread_is_a_runtime_change_applied_on_the_gui_thread(qtbot, make):
    node = make(Plain)
    changes = []
    node.changed.connect(changes.append)
    thread = threading.Thread(target=lambda: node.SetFields({"count": 7}))
    thread.start()
    thread.join()
    qtbot.waitUntil(lambda: node.field_editor("count") is not None and node.field_editor("mode") is None, timeout=2000)
    assert node.field_editor("count").value() == 7
    assert changes == [True]


def test_set_fields_is_refused_for_a_cancelled_call(make):
    node = make(Plain)
    token = RunToken()
    token.cancel()
    errors = []

    def worker():
        with bound(token):
            try:
                node.SetFields({"count": 2})
            except ExecutionCancelled as error:
                errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert errors and node.GetField("count") == 1


def test_switching_the_composite_type_renames_a_title_that_names_the_type(bt):
    selector = bt.AddNode("Sequence", 0, 200)
    renamed = bt.AddNode("Sequence", 300, 200)
    renamed.SetTitle("Patrol")
    titles = []
    selector.titleChanged.connect(titles.append)
    selector.SetCompositeType("Selector")
    renamed.SetCompositeType("Selector")
    assert selector.GetTitle() == "Selector" and titles == ["Selector"]
    assert renamed.GetTitle() == "Patrol" and renamed.DisplayTitle() == "Patrol (Selector)"
