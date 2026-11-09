"""Regressions found by the review of the 0.7.0 features (two tree files, Reset entries,
Sub-Tree and Group nodes)."""

from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest
import shiboken6
from PySide6.QtWidgets import QApplication, QMessageBox

from behavior_tree_widget import GroupNodeWidget, LeafNodeWidget, Status
from behavior_tree_widget.serialization import FORMAT_NAME, write_json_files_atomic

from conftest import fast_config, file_blackboard


def read(path) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def legacy_tree(path, blackboard=()) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({
            "format": FORMAT_NAME,
            "version": 1,
            "nodes": [{"id": "root", "type": "Root", "title": "Root", "x": 0.0, "y": 0.0}],
            "connections": [],
            "blackboard": list(blackboard),
        }, stream)


# ============================================================================ writing both files
def test_write_json_files_atomic_puts_back_what_it_wrote(tmp_path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text('{"old": 1}', encoding="utf-8")
    os.makedirs(second)  # cannot be written
    with pytest.raises(OSError):
        write_json_files_atomic([(str(first), {"new": 1}), (str(second), {"new": 2})])
    assert first.read_text(encoding="utf-8") == '{"old": 1}'
    fresh = tmp_path / "fresh.json"
    with pytest.raises(OSError):
        write_json_files_atomic([(str(fresh), {"new": 1}), (str(second), {"new": 2})])
    assert not fresh.exists()
    assert sorted(os.listdir(tmp_path)) == ["a.json", "b.json"]  # no temporary files left


def test_a_failed_new_tree_keeps_the_tree_file(bt, tmp_path):
    bt.AddNode("Succeed", 0, 200)
    assert bt.SaveTree(bt.GetFilePath())
    before = read(bt.GetFilePath())
    os.chmod(bt.GetBlackboardFilePath(), stat.S_IREAD)
    try:
        with pytest.raises(OSError):
            bt.NewTree(str(tmp_path / "tree.json"))
    finally:
        os.chmod(bt.GetBlackboardFilePath(), stat.S_IREAD | stat.S_IWRITE)
    assert read(bt.GetFilePath()) == before
    assert len(bt.GetNodes()) == 2 and not bt.IsModified()


def test_a_failed_new_tree_from_the_button_keeps_the_tree_file(bt, tmp_path, dialogs):
    bt.AddNode("Succeed", 0, 200)
    assert bt.SaveTree(bt.GetFilePath())
    before = read(bt.GetFilePath())
    os.remove(bt.GetBlackboardFilePath())
    os.makedirs(bt.GetBlackboardFilePath())
    dialogs.save_path = str(tmp_path / "tree_BT.json")
    assert not bt.NewTree()
    assert "critical" in dialogs.kinds()
    assert read(bt.GetFilePath()) == before


# ============================================================================ names of files of earlier versions
def test_a_group_cannot_take_the_name_a_legacy_tree_is_saved_to(make_widget, tmp_path):
    legacy_tree(tmp_path / "robot.json")
    widget = make_widget()
    assert widget.LoadTree(str(tmp_path / "robot.json"))
    widget.AddNode("Succeed", 0, 600)
    group = widget.AddNode("Group", -100, 520)
    group.SetGroupSize(500, 300)
    group.SetTitle("robot")
    QApplication.processEvents()
    with pytest.raises(ValueError, match="names the file of this tree"):
        widget.PromoteGroupToSubTree(group)
    assert not os.path.exists(tmp_path / "robot_BT.json")


@pytest.mark.parametrize("answer, replaced", [(QMessageBox.StandardButton.Yes, True), (QMessageBox.StandardButton.No, False)])
def test_the_close_prompt_asks_before_replacing_a_newer_tree_file(
    make_widget, tmp_path, dialogs, monkeypatch, answer, replaced
):
    legacy_tree(tmp_path / "robot.json")
    newer = {"format": FORMAT_NAME, "version": 1, "nodes": [], "connections": [], "comment": "newer"}
    (tmp_path / "robot_BT.json").write_text(json.dumps(newer), encoding="utf-8")
    widget = make_widget()
    assert widget.LoadTree(str(tmp_path / "robot.json"))
    widget.AddNode("Succeed", 0, 200)
    answers = iter([QMessageBox.StandardButton.Save, answer])
    original_question = QMessageBox.question

    def question(*args, **kwargs):
        dialogs.question_answer = next(answers, QMessageBox.StandardButton.Discard)
        return original_question(*args, **kwargs)

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    dialogs.save_path = ""  # "No" offers the save dialog; cancelling it keeps the window open
    assert widget.ConfirmDiscardChanges() == replaced
    assert [kind for kind in dialogs.kinds() if kind == "question"] == ["question", "question"]
    assert (read(tmp_path / "robot_BT.json").get("comment") != "newer") == replaced
    assert ("save_dialog" in dialogs.kinds()) == (not replaced)


def test_the_save_dialog_asks_when_a_tree_name_is_typed_without_extension(bt, tmp_path, dialogs):
    (tmp_path / "robot_BT.json").write_text("{}", encoding="utf-8")
    dialogs.save_path = str(tmp_path / "robot_BT")  # the dialog only confirmed "robot_BT"
    dialogs.question_answer = QMessageBox.StandardButton.No
    assert not bt.SaveTree()
    assert "question" in dialogs.kinds()
    assert (tmp_path / "robot_BT.json").read_text(encoding="utf-8") == "{}"


# ============================================================================ Reset entries and the blackboard file
class Bump(LeafNodeWidget):
    """Raises "counter" to 3, then keeps running (the loop never ends by itself)."""

    RUN_IN_THREAD = False

    def OnRun(self, tree):
        if tree.GetEntry("counter") < 3:
            tree.SetEntry(tree.GetEntry("counter") + 1, "counter")
        return Status.RUNNING


def running(qtbot, bt):
    bt.RegisterNodeType(Bump)
    fast_config(bt)
    bt.AddEntry("counter", "Integer", 0, reset=True)
    bt.Connect(bt.GetRootNode(), bt.AddNode("Bump", 0, 200))
    assert bt.SaveTree(bt.GetFilePath())
    bt.Execute()
    qtbot.waitUntil(lambda: bt.GetEntry("counter") == 3, timeout=3000)
    qtbot.waitUntil(lambda: file_blackboard(bt.GetFilePath())[0]["value"] == 3, timeout=3000)


def test_reloading_the_running_tree_loads_the_reset_values(qtbot, bt):
    running(qtbot, bt)
    assert bt.LoadTree(bt.GetFilePath())
    qtbot.wait(300)
    assert bt.GetEntry("counter") == 0
    assert file_blackboard(bt.GetFilePath())[0]["value"] == 0
    assert bt.GetEntryReset("counter")


def test_shutdown_during_a_loop_writes_the_reset_values(qtbot, bt):
    running(qtbot, bt)
    path = bt.GetFilePath()
    bt.Shutdown()
    assert file_blackboard(path)[0]["value"] == 0


class ShutdownFromOnRun(LeafNodeWidget):
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        tree.SetEntry(5, "counter")
        tree.Shutdown()
        return True


def test_shutdown_during_a_tick_writes_the_reset_values(qtbot, bt):
    bt.RegisterNodeType(ShutdownFromOnRun)
    fast_config(bt)
    bt.AddEntry("counter", "Integer", 1, reset=True)
    bt.Connect(bt.GetRootNode(), bt.AddNode("ShutdownFromOnRun", 0, 200))
    path = bt.GetFilePath()
    bt.Execute()
    qtbot.waitUntil(lambda: bt.blackboard_store().is_disposed(), timeout=3000)
    assert file_blackboard(path)[0]["value"] == 1


# ============================================================================ sub-tree nodes on worker threads
class Stubborn(LeafNodeWidget):
    """Ignores cancellation for a while on its worker thread, then touches its widget."""

    _fields = {"seconds": 0.4}
    lock = threading.Lock()
    active = 0
    most = 0
    touched: list = []
    errors: list = []

    def OnRun(self, tree):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.most = max(cls.most, cls.active)
        try:
            time.sleep(self.GetField("seconds"))
            try:
                cls.touched.append(shiboken6.isValid(self) and self.objectName() is not None)
            except RuntimeError as error:
                cls.errors.append(repr(error))
            return True
        finally:
            with cls.lock:
                cls.active -= 1


def test_a_sub_tree_node_never_runs_two_calls_at_once(qtbot, make_widget, bt, tmp_path):
    Stubborn.active = Stubborn.most = 0
    Stubborn.touched, Stubborn.errors = [], []
    author = make_widget(node_types=[Stubborn])
    assert author.NewTree(str(tmp_path / "sub"))
    author.Connect(author.GetRootNode(), author.AddNode(Stubborn, 0, 200))
    assert author.SaveTree(author.GetFilePath())
    bt.RegisterNodeType(Stubborn)
    fast_config(bt)
    sub = bt.AddNode("Sub-Tree", 0, 200)
    sub.SetFile("sub_BT.json")
    bt.Connect(bt.GetRootNode(), sub)
    bt.Execute()
    qtbot.waitUntil(lambda: Stubborn.active == 1, timeout=3000)
    first = bt.executor().subtree_nodes()
    bt.Stop()
    bt.Execute()  # the new run's node waits for the interrupted call
    qtbot.waitUntil(lambda: not bt.IsExecuting(), timeout=5000)
    assert Stubborn.most == 1
    # The released nodes lived until their call returned (the call could use them), then went away.
    assert Stubborn.touched == [True, True] and Stubborn.errors == []
    qtbot.waitUntil(lambda: not any(shiboken6.isValid(node) for node in first), timeout=3000)


# ============================================================================ Promote to Sub-Tree
class Mark(LeafNodeWidget):
    RUN_IN_THREAD = False
    order: list = []

    def OnRun(self, tree):
        type(self).order.append(self.GetTitle())
        return True


def test_promote_refuses_a_group_with_an_outside_sibling_between_its_nodes(bt):
    bt.RegisterNodeType(Mark)
    nodes = {}
    for title, x, y in (("A", -400, 300), ("X", 0, 700), ("B", 400, 300)):
        nodes[title] = bt.AddNode("Mark", x, y)
        nodes[title].SetTitle(title)
        bt.Connect(bt.GetRootNode(), nodes[title])
    group = bt.AddNode("Group", -450, 250)
    group.SetGroupSize(1100, 250)
    QApplication.processEvents()
    assert set(group.GetContainedNodes()) == {nodes["A"], nodes["B"]}
    with pytest.raises(ValueError, match="'X' outside the group runs between"):
        bt.PromoteGroupToSubTree(group)
    assert bt.GetRootNode().GetChildren() == [nodes["A"], nodes["X"], nodes["B"]]
    assert group in bt.GetNodes()


def test_a_pasted_default_named_group_always_gets_the_next_number(bt):
    group = bt.AddNode("Group", 0, 600)
    assert bt.CopyNodes([group])
    bt.RemoveNode(group)
    pasted = bt.PasteNodes()
    assert [node.GetTitle() for node in pasted if isinstance(node, GroupNodeWidget)] == ["group_002"]
