"""The tree log: a log file per tree, one Qt signal per level, execution messages, never blocking.

Requirements (feature request "logging"):

* the log is written to a file in the folder the application is run from, named
  ``<tree name>_<month>-<day>-<year>_<hour>-<minute>-<second>.log`` with the time the
  application was started;
* levels by precedence 0: None, 1: ERROR, 2: WARNING, 3: DEBUG, 4: INFO; the log level of the
  tree (``TreeConfig.log_level``, INFO by default) drops less important messages, None drops all;
* one Qt signal per level, emitted for every message of that level (and ``messageLogged``);
* the file contains every level; the execution of each node is logged at INFO;
* logging never blocks.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication, QComboBox

from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget, LogLevel, Status, TreeConfig, flush_logs
from behavior_tree_widget import tree_log
from behavior_tree_widget import widget as widget_module
from behavior_tree_widget.serialization import FORMAT_NAME
from behavior_tree_widget.subtree import SUB_TREE
from behavior_tree_widget.tree_log import APP_START, log_file_name

from conftest import fast_config, read_json, run_until_idle

LABELS = ["0: None", "1: ERROR", "2: WARNING", "3: DEBUG", "4: INFO"]
ENTRY = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (ERROR|WARNING|DEBUG|INFO) +(.*)$")


# ============================================================================ helpers
class Gate(LeafNodeWidget):
    """Fails the first time it runs, succeeds afterwards (to preempt a lower-priority child)."""

    _title = "Gate"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        self.runs = getattr(self, "runs", 0) + 1
        return self.runs > 1


class Spin(LeafNodeWidget):
    """Runs for a long time (``ticks`` ticks)."""

    _title = "Spin"
    _fields = {"ticks": 1000}
    RUN_IN_THREAD = False

    def OnStart(self, tree):
        self.left = self.GetField("ticks")

    def OnRun(self, tree):
        if self.left:
            self.left -= 1
            return Status.RUNNING
        return True


class ThreadLogger(LeafNodeWidget):
    """Logs from OnRun on its worker thread."""

    _title = "Thread Logger"
    threads: list = []

    def OnRun(self, tree):
        type(self).threads.append(threading.get_ident())
        return tree.Log(LogLevel.DEBUG, "logged from a worker thread")


@pytest.fixture
def logs(monkeypatch, tmp_path):
    """A log folder of the test's own (it does not exist yet)."""
    folder = tmp_path / "logs"
    monkeypatch.setattr(BehaviorTreeWidget, "LOG_FOLDER", str(folder))
    return folder


@pytest.fixture
def tree(logs, make_widget, tmp_path, qtbot):
    """A widget holding the new tree "robot" (log file robot_<start>.log in ``logs``)."""
    widget = make_widget()
    for cls in (Gate, Spin, ThreadLogger):
        widget.RegisterNodeType(cls)
    assert widget.NewTree(str(tmp_path / "robot.json"))
    fast_config(widget)
    qtbot.wait(1)  # deliver the signals of the messages logged so far
    return widget


def deliver(qtbot) -> None:
    """Let the GUI event loop emit the signals of everything logged so far."""
    assert flush_logs()
    QApplication.processEvents()
    qtbot.wait(5)


def record(signal) -> list:
    values: list = []
    signal.connect(lambda *args: values.append(args[0] if len(args) == 1 else args))
    return values


def read_log(path) -> str:
    assert flush_logs(), "the log files were not written in time"
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def entries(path) -> list[tuple[str, str]]:
    """(level name, message) of every message in a log file; continuation lines (e.g. a
    traceback) are part of the message before them."""
    result: list[list[str]] = []
    for line in read_log(path).splitlines():
        match = ENTRY.match(line)
        if match:
            result.append([match.group(2), match.group(3)])
        else:
            assert result, f"the log file starts with a line that is not a message: {line!r}"
            result[-1][1] += "\n" + line
    return [(level, message) for level, message in result]


def messages(path) -> list[str]:
    return [message for _, message in entries(path)]


def run_messages(bt) -> list[str]:
    """The messages of the last execution (from its "Execution started")."""
    found = messages(bt.GetLogFilePath())
    start = len(found) - 1 - found[::-1].index("Execution started")
    return found[start:]


def under_root(bt, *type_names) -> list:
    nodes = []
    for index, name in enumerate(type_names):
        node = bt.AddNode(name, index * 220, 220)
        bt.Connect(bt.GetRootNode(), node)
        nodes.append(node)
    return nodes


def execute(qtbot, bt) -> list:
    results = record(bt.executionFinished)
    bt.Execute()
    run_until_idle(qtbot, bt)
    return results


def tree_document(nodes: list, connections: list, config: dict | None = None) -> dict:
    root = {"id": "root", "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True}
    return {
        "format": FORMAT_NAME,
        "version": 1,
        "config": {"tick_interval_ms": 5, "repeat": False, "restore_blackboard": False, "default_memory": True,
                   **(config or {})},
        "nodes": [root, *nodes],
        "connections": [{"parent": parent, "child": child} for parent, child in connections],
    }


def write_tree(path, nodes=(), connections=(), config=None) -> str:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(tree_document(list(nodes), list(connections), config), stream)
    return str(path)


def files_in(folder) -> list[str]:
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


# ============================================================================ levels
def test_levels_have_the_requested_numbers_and_labels():
    assert [(level.name, int(level)) for level in LogLevel] == [
        ("NONE", 0), ("ERROR", 1), ("WARNING", 2), ("DEBUG", 3), ("INFO", 4),
    ]
    assert [level.label for level in LogLevel] == LABELS
    assert LogLevel.ERROR < LogLevel.WARNING < LogLevel.DEBUG < LogLevel.INFO  # lower = more important


@pytest.mark.parametrize(
    "value, expected",
    [
        (LogLevel.DEBUG, LogLevel.DEBUG), (0, LogLevel.NONE), (1, LogLevel.ERROR), (4, LogLevel.INFO),
        ("debug", LogLevel.DEBUG), (" Warning ", LogLevel.WARNING), ("INFO", LogLevel.INFO), ("none", LogLevel.NONE),
    ],
)
def test_coerce_accepts_levels_numbers_and_names(value, expected):
    assert LogLevel.coerce(value) is expected


@pytest.mark.parametrize("value, error", [
    (5, ValueError), (-1, ValueError), ("verbose", ValueError), ("", ValueError),
    (True, TypeError), (False, TypeError), (2.0, TypeError), (None, TypeError), ([1], TypeError),
])
def test_coerce_rejects_other_values(value, error):
    with pytest.raises(error):
        LogLevel.coerce(value)


# ============================================================================ the file name
def test_log_file_name_format_is_zero_padded():
    assert log_file_name("robot", datetime.datetime(2026, 1, 2, 3, 4, 5)) == "robot_01-02-2026_03-04-05.log"
    assert log_file_name("my tree", datetime.datetime(2026, 12, 31, 23, 59, 58)) == "my tree_12-31-2026_23-59-58.log"


def test_log_file_is_named_with_the_application_start(tree, logs):
    start = APP_START
    assert start <= datetime.datetime.now()
    expected = (
        f"robot_{start.month:02d}-{start.day:02d}-{start.year}_{start.hour:02d}-{start.minute:02d}-{start.second:02d}.log"
    )
    assert log_file_name("robot") == expected
    path = tree.GetLogFilePath()
    assert os.path.isabs(path)
    assert path == os.path.join(str(logs), expected)
    assert os.path.exists(path)  # "New tree created" was written
    assert files_in(logs) == [expected]


def test_without_a_log_folder_the_file_is_in_the_folder_the_application_started_from(monkeypatch, make_widget, tmp_path):
    assert tree_log.START_FOLDER == os.getcwd()
    start_folder = tmp_path / "started_here"
    monkeypatch.setattr(BehaviorTreeWidget, "LOG_FOLDER", None)
    monkeypatch.setattr(widget_module, "START_FOLDER", str(start_folder))  # never write into the repository
    bt = make_widget()
    assert bt.GetLogFilePath() is None
    (tmp_path / "trees").mkdir()
    assert bt.NewTree(str(tmp_path / "trees" / "patrol_BT.json"))
    assert bt.GetLogFilePath() == os.path.join(str(start_folder), log_file_name("patrol"))
    assert "New tree created: '" in read_log(bt.GetLogFilePath())


# ============================================================================ signals
def test_every_level_has_a_signal_and_message_logged_gets_them_all(tree, qtbot):
    deliver(qtbot)
    by_level = {
        "error": record(tree.errorLogged),
        "warning": record(tree.warningLogged),
        "debug": record(tree.debugLogged),
        "info": record(tree.infoLogged),
    }
    every = record(tree.messageLogged)
    assert tree.Log(LogLevel.ERROR, "an error")
    assert tree.Log(2, "a warning")
    assert tree.Log("debug", "a debug message")
    assert tree.Log("INFO", "an info")
    assert tree.Log(LogLevel.ERROR, "a second error")
    assert not tree.Log(LogLevel.NONE, "nothing")
    assert every == [] and by_level["error"] == []  # emitted later, from the event loop
    qtbot.waitUntil(lambda: len(every) == 5, timeout=2000)
    assert by_level == {
        "error": ["an error", "a second error"],
        "warning": ["a warning"],
        "debug": ["a debug message"],
        "info": ["an info"],
    }
    assert every == [(1, "an error"), (2, "a warning"), (3, "a debug message"), (4, "an info"), (1, "a second error")]
    deliver(qtbot)
    assert len(every) == 5


def test_the_widget_emits_the_messages_of_its_tree_logger(tree, qtbot):
    deliver(qtbot)
    own = record(tree.tree_logger().warningLogged)  # the widget's logger announces through the widget
    widget = record(tree.warningLogged)
    every = record(tree.messageLogged)
    tree.tree_logger().log(LogLevel.WARNING, "through the logger")
    qtbot.waitUntil(lambda: widget == ["through the logger"], timeout=2000)
    assert every == [(2, "through the logger")]
    deliver(qtbot)
    assert own == []


def test_a_standalone_tree_logger_emits_its_own_signals(qtbot, tmp_path):
    logger = tree_log.TreeLogger()
    try:
        errors = record(logger.errorLogged)
        every = record(logger.messageLogged)
        logger.set_file(str(tmp_path / "standalone.log"))
        assert logger.path() == str(tmp_path / "standalone.log")
        assert logger.log("error", "standalone error")
        assert not logger.log("none", "dropped")
        qtbot.waitUntil(lambda: every == [(1, "standalone error")], timeout=2000)
        assert errors == ["standalone error"]
        assert messages(tmp_path / "standalone.log") == ["standalone error"]
    finally:
        logger.close()
    assert not logger.log("error", "after close")
    assert messages(tmp_path / "standalone.log") == ["standalone error"]


def test_messages_are_delivered_only_to_connected_level_signals(tree, qtbot):
    deliver(qtbot)
    errors = record(tree.errorLogged)  # nothing listens to the other signals
    for level in (LogLevel.INFO, LogLevel.ERROR, LogLevel.DEBUG):
        assert tree.Log(level, f"{level.name} text")
    qtbot.waitUntil(lambda: errors == ["ERROR text"], timeout=2000)
    assert messages(tree.GetLogFilePath())[-3:] == ["INFO text", "ERROR text", "DEBUG text"]  # all written


def test_messages_that_are_not_text_are_logged_as_text(tree, qtbot):
    deliver(qtbot)
    got = record(tree.debugLogged)

    class Broken:
        def __str__(self):
            raise RuntimeError("no text")

    assert tree.Log("debug", 42)
    assert tree.Log("debug", Broken())
    assert tree.Log("debug", "unicode: héllo ✓")
    qtbot.waitUntil(lambda: len(got) == 3, timeout=2000)
    assert got == ["42", "<Broken object>", "unicode: héllo ✓"]
    assert messages(tree.GetLogFilePath())[-3:] == got


def test_logging_from_a_slot_of_the_signals_works(tree, qtbot):
    deliver(qtbot)
    got = []

    def answer(message):
        got.append(message)
        if message == "ping":
            assert tree.Log("info", "pong")

    tree.infoLogged.connect(answer)
    tree.Log("info", "ping")
    qtbot.waitUntil(lambda: got == ["ping", "pong"], timeout=2000)
    assert messages(tree.GetLogFilePath())[-2:] == ["ping", "pong"]


# ============================================================================ the file
def test_the_file_holds_every_level_with_its_name_and_time(tree):
    for level in (LogLevel.ERROR, LogLevel.WARNING, LogLevel.DEBUG, LogLevel.INFO):
        assert tree.Log(level, f"{level.name.lower()} message")
    text = read_log(tree.GetLogFilePath())
    found = entries(tree.GetLogFilePath())
    assert found[-4:] == [
        ("ERROR", "error message"), ("WARNING", "warning message"), ("DEBUG", "debug message"), ("INFO", "info message"),
    ]
    stamp = ENTRY.match(text.splitlines()[-1]).group(1)
    written = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f")
    assert abs((datetime.datetime.now() - written).total_seconds()) < 60
    assert found[0] == ("INFO", f"New tree created: '{tree.GetFilePath()}'")


def test_multi_line_messages_are_written_whole(tree):
    assert tree.Log("warning", "first line\nsecond line")
    assert entries(tree.GetLogFilePath())[-1] == ("WARNING", "first line\nsecond line")


# ============================================================================ the log level
def test_the_log_level_drops_less_important_messages(tree, qtbot):
    assert tree.GetLogLevel() is LogLevel.INFO
    assert tree.GetConfig().log_level == 4
    deliver(qtbot)
    every = record(tree.messageLogged)
    tree.SetLogLevel(LogLevel.WARNING)
    assert tree.GetLogLevel() is LogLevel.WARNING and tree.GetConfig().log_level == 2
    assert tree.tree_logger().level() is LogLevel.WARNING
    assert [tree.Log(level, f"level {int(level)}") for level in LogLevel] == [False, True, True, False, False]
    tree.SetLogLevel("debug")
    assert tree.GetLogLevel() is LogLevel.DEBUG
    assert [tree.Log(level, f"then {int(level)}") for level in LogLevel] == [False, True, True, True, False]
    tree.SetLogLevel(1)
    assert [tree.Log(level, f"last {int(level)}") for level in LogLevel] == [False, True, False, False, False]
    qtbot.waitUntil(lambda: len(every) == 6, timeout=2000)
    expected = ["level 1", "level 2", "then 1", "then 2", "then 3", "last 1"]
    assert [message for _, message in every] == expected
    assert messages(tree.GetLogFilePath())[-6:] == expected


def test_log_level_none_logs_nothing(tree, qtbot):
    deliver(qtbot)
    every = record(tree.messageLogged)
    before = read_log(tree.GetLogFilePath())
    tree.SetLogLevel(LogLevel.NONE)
    assert tree.GetLogLevel() is LogLevel.NONE
    assert not any(tree.Log(level, "dropped") for level in LogLevel)
    tree.AddNode("Raise", 0, 220)
    tree.Connect(tree.GetRootNode(), tree.GetNodes()[-1])
    assert execute(qtbot, tree) == ["Failed"]
    deliver(qtbot)
    assert every == []
    assert read_log(tree.GetLogFilePath()) == before


def test_no_log_file_is_created_while_nothing_is_logged(logs, make_widget, tmp_path, qtbot):
    path = write_tree(
        tmp_path / "quiet_BT.json",
        [{"id": "r", "type": "Raise", "title": "Raise", "x": 0.0, "y": 220.0}],
        [("root", "r")],
        config={"log_level": 0},
    )
    bt = make_widget()
    assert bt.LoadTree(path)
    assert bt.GetLogLevel() is LogLevel.NONE
    assert bt.GetLogFilePath() == os.path.join(str(logs), log_file_name("quiet"))
    assert not bt.Log(LogLevel.ERROR, "dropped")
    assert execute(qtbot, bt) == ["Failed"]
    assert flush_logs()
    assert not os.path.exists(bt.GetLogFilePath())
    assert files_in(logs) == []


def test_a_widget_without_tree_that_logs_nothing_creates_no_file(logs, make_widget):
    bt = make_widget()
    assert bt.GetLogFilePath() is None
    bt.Shutdown()
    assert flush_logs()
    assert files_in(logs) == []


# ============================================================================ threads / blocking
def test_logging_from_worker_threads(tree, qtbot):
    deliver(qtbot)
    got = []
    slot_threads = set()

    def on_message(level, message):
        got.append((level, message))
        slot_threads.add(threading.get_ident())

    tree.messageLogged.connect(on_message)

    def work(number):
        for index in range(25):
            assert tree.Log("warning", f"worker {number} message {index}")

    workers = [threading.Thread(target=work, args=(number,)) for number in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    qtbot.waitUntil(lambda: len(got) == 100, timeout=3000)
    assert slot_threads == {threading.get_ident()}  # the signals are emitted on the GUI thread
    assert {level for level, _ in got} == {2}
    written = messages(tree.GetLogFilePath())
    for number in range(4):
        expected = [f"worker {number} message {index}" for index in range(25)]
        assert [message for _, message in got if message.startswith(f"worker {number} ")] == expected
        assert [message for message in written if message.startswith(f"worker {number} ")] == expected


def test_a_node_running_on_a_worker_thread_can_log(tree, qtbot):
    ThreadLogger.threads = []
    under_root(tree, "ThreadLogger")
    got = record(tree.debugLogged)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert ThreadLogger.threads and ThreadLogger.threads[0] != threading.get_ident()
    qtbot.waitUntil(lambda: got == ["logged from a worker thread"], timeout=2000)
    assert ("DEBUG", "logged from a worker thread") in entries(tree.GetLogFilePath())


def test_logging_never_waits_for_the_file(tree, qtbot, monkeypatch):
    path = tree.GetLogFilePath()
    assert flush_logs()
    original = logging.FileHandler.format
    delay = 0.05

    def slow_format(handler, record):  # every file handler formats each record it writes
        if os.path.normcase(handler.baseFilename) == os.path.normcase(path):
            time.sleep(delay)
        return original(handler, record)

    monkeypatch.setattr(logging.FileHandler, "format", slow_format)
    got = record(tree.debugLogged)
    count = 40  # writing them takes 2 s
    started = time.perf_counter()
    for index in range(count):
        assert tree.Log(LogLevel.DEBUG, f"slow {index}")
    elapsed = time.perf_counter() - started
    assert elapsed < count * delay / 4, f"Log() waited for the file ({elapsed:.2f} s)"
    assert not flush_logs(timeout=0.01)  # still being written
    qtbot.waitUntil(lambda: len(got) == count, timeout=1000)  # the signals do not wait for the file either
    assert flush_logs(timeout=15)
    assert messages(path)[-count:] == [f"slow {index}" for index in range(count)]


# ============================================================================ node execution
def test_a_node_executed_within_one_tick_is_logged(tree, qtbot):
    under_root(tree, "Succeed")
    assert execute(qtbot, tree) == ["Succeeded"]
    assert run_messages(tree) == [
        "Execution started",
        "Node 'Succeed' (Succeed) executed: Succeeded",
        "Node 'Root' (Root) executed: Succeeded",
        "Execution finished: Succeeded",
    ]
    assert {level for level, message in entries(tree.GetLogFilePath()) if message.startswith(("Node ", "Execution "))} == {
        "INFO"
    }


def test_a_running_node_is_logged_when_it_starts_and_finishes(tree, qtbot):
    node = under_root(tree, "RunningN")[0]
    node.SetTitle("waiter")
    node.SetField("ticks", 3)
    assert execute(qtbot, tree) == ["Succeeded"]
    assert run_messages(tree) == [
        "Execution started",
        "Node 'waiter' (RunningN) started: Running",
        "Node 'Root' (Root) started: Running",
        "Node 'waiter' (RunningN) finished: Succeeded",
        "Node 'Root' (Root) finished: Succeeded",
        "Execution finished: Succeeded",
    ]


def test_failing_nodes_are_logged_with_their_error(tree, qtbot):
    tree.GetRootNode().SetCompositeType("Selector")
    under_root(tree, "FailNode", "ReturnNone", "Succeed")
    assert execute(qtbot, tree) == ["Succeeded"]
    error = "OnRun must return True, False or a py_trees.common.Status, not NoneType (missing return statement?)"
    assert run_messages(tree) == [
        "Execution started",
        "Node 'Fail Node' (FailNode) executed: Failed",
        f"'Return None' (ReturnNone): {error}",
        f"Node 'Return None' (ReturnNone) executed: Failed ({error})",
        "Node 'Succeed' (Succeed) executed: Succeeded",
        "Node 'Root' (Root) executed: Succeeded",
        "Execution finished: Succeeded",
    ]
    found = entries(tree.GetLogFilePath())
    assert ("ERROR", f"'Return None' (ReturnNone): {error}") in found


def test_a_raising_node_is_logged_at_error_with_its_traceback(tree, qtbot):
    under_root(tree, "Raise")
    errors = record(tree.errorLogged)
    assert execute(qtbot, tree) == ["Failed"]
    qtbot.waitUntil(lambda: len(errors) == 1, timeout=2000)
    message = errors[0]
    assert message.startswith("'Raise' (Raise): OnRun raised ValueError: boom\nTraceback (most recent call last):")
    assert 'raise ValueError("boom")' in message
    assert message.rstrip().endswith("ValueError: boom")
    found = entries(tree.GetLogFilePath())
    assert ("ERROR", message) in found
    assert run_messages(tree)[-3:] == [
        "Node 'Raise' (Raise) executed: Failed (OnRun raised ValueError: boom)",
        "Node 'Root' (Root) executed: Failed",
        "Execution finished: Failed",
    ]


def test_a_node_preempted_by_its_parent_is_logged_as_interrupted(tree, qtbot):
    root = tree.GetRootNode()
    root.SetCompositeType("Selector")
    root.SetMemory(False)  # a higher-priority child that succeeds interrupts the running one
    under_root(tree, "Gate", "Spin")
    assert execute(qtbot, tree) == ["Succeeded"]
    assert run_messages(tree) == [
        "Execution started",
        "Node 'Gate' (Gate) executed: Failed",
        "Node 'Spin' (Spin) started: Running",
        "Node 'Root' (Root) started: Running",
        "Node 'Gate' (Gate) executed: Succeeded",
        "Node 'Root' (Root) finished: Succeeded",
        "Node 'Spin' (Spin) interrupted",
        "Execution finished: Succeeded",
    ]


def test_nodes_of_a_sub_tree_are_prefixed_with_the_sub_tree_node(tree, qtbot, tmp_path):
    sub_path = write_tree(
        tmp_path / "patrol_BT.json",
        [
            {"id": "a", "type": "Succeed", "title": "inner", "x": 0.0, "y": 220.0},
            {"id": "b", "type": "Raise", "title": "boom", "x": 220.0, "y": 220.0},
        ],
        [("root", "a"), ("root", "b")],
    )
    node = tree.AddNode(SUB_TREE, 0, 220)
    node.SetFile(sub_path)
    node.SetTitle("patrol")
    tree.Connect(tree.GetRootNode(), node)
    assert execute(qtbot, tree) == ["Failed"]
    found = run_messages(tree)
    assert found[:2] == ["Execution started", "Node 'patrol' > 'inner' (Succeed) executed: Succeeded"]
    assert found[2].startswith("'patrol' > 'boom' (Raise): OnRun raised ValueError: boom\nTraceback")
    assert found[3:7] == [
        "Node 'patrol' > 'boom' (Raise) executed: Failed (OnRun raised ValueError: boom)",
        "Node 'patrol' > 'Root' (Root) executed: Failed",
        "Node 'patrol' (Sub-Tree) executed: Failed (boom: OnRun raised ValueError: boom)",
        "Node 'Root' (Root) executed: Failed",
    ]
    assert found[-1] == "Execution finished: Failed"


def test_execution_start_pause_resume_and_stop_are_logged(tree, qtbot):
    node = under_root(tree, "Spin")[0]
    tree.Execute()
    qtbot.waitUntil(lambda: node.GetStatus().value == "Running", timeout=2000)
    tree.Pause()
    assert tree.GetExecutionState() == "Paused"
    tree.Execute()
    assert tree.GetExecutionState() == "Running"
    tree.Stop()
    assert not tree.IsExecuting()
    found = run_messages(tree)
    assert found[:3] == ["Execution started", "Node 'Spin' (Spin) started: Running", "Node 'Root' (Root) started: Running"]
    assert found[-3:] == ["Execution paused", "Execution resumed", "Execution stopped"]
    assert not any(message.startswith("Execution finished") for message in found)


def test_loop_execution_restarts_are_logged(tree, qtbot):
    under_root(tree, "Succeed")
    tree.SetLoopExecution(True)
    tree.Execute()
    qtbot.waitUntil(
        lambda: messages(tree.GetLogFilePath()).count("Node 'Succeed' (Succeed) executed: Succeeded") >= 3, timeout=3000
    )
    tree.Stop()
    found = run_messages(tree)
    assert "Loop Execution: the tree starts again from the Root" in found
    assert found[-1] == "Execution stopped"


def test_node_execution_is_not_logged_below_info(tree, qtbot):
    under_root(tree, "Succeed", "Raise")
    tree.SetLogLevel(LogLevel.DEBUG)
    before = len(entries(tree.GetLogFilePath()))
    assert execute(qtbot, tree) == ["Failed"]
    added = entries(tree.GetLogFilePath())[before:]
    assert [level for level, _ in added] == ["ERROR"]
    assert added[0][1].startswith("'Raise' (Raise): OnRun raised ValueError: boom")


# ============================================================================ the file follows the tree
def test_the_log_file_follows_new_load_and_save_as(logs, make_widget, tmp_path):
    bt = make_widget()
    assert bt.NewTree(str(tmp_path / "alpha.json"))
    alpha = bt.GetLogFilePath()
    assert alpha == os.path.join(str(logs), log_file_name("alpha"))
    assert bt.Log("debug", "first in alpha")

    assert bt.NewTree(str(tmp_path / "beta_BT.json"))
    beta = bt.GetLogFilePath()
    assert beta == os.path.join(str(logs), log_file_name("beta"))
    assert bt.Log("debug", "in beta")

    assert bt.SaveTree(str(tmp_path / "gamma.json"))  # save as: the tree is gamma now
    gamma = bt.GetLogFilePath()
    assert gamma == os.path.join(str(logs), log_file_name("gamma"))
    assert bt.Log("debug", "in gamma")

    assert bt.LoadTree(str(tmp_path / "alpha_BT.json"))
    assert bt.GetLogFilePath() == alpha
    assert bt.Log("debug", "alpha again")
    assert bt.SaveTree(str(tmp_path / "alpha_BT.json"))  # the same tree: the same file
    assert bt.GetLogFilePath() == alpha

    alpha_file = tmp_path / "alpha_BT.json"
    assert messages(alpha) == [
        f"New tree created: '{alpha_file}'",
        "first in alpha",
        f"Tree loaded: '{alpha_file}'",
        "alpha again",
        f"Tree saved to '{alpha_file}'",
    ]
    assert messages(beta) == [f"New tree created: '{tmp_path / 'beta_BT.json'}'", "in beta"]
    assert messages(gamma) == [f"Tree saved to '{tmp_path / 'gamma_BT.json'}'", "in gamma"]
    assert files_in(logs) == sorted(os.path.basename(path) for path in (alpha, beta, gamma))


def test_messages_logged_before_a_tree_go_to_the_first_file(logs, make_widget, tmp_path):
    bt = make_widget()
    assert bt.Log(LogLevel.WARNING, "before any tree")
    assert bt.Log(LogLevel.DEBUG, "also before")
    assert bt.GetLogFilePath() is None
    assert flush_logs()
    assert files_in(logs) == []
    assert bt.NewTree(str(tmp_path / "first.json"))
    assert entries(bt.GetLogFilePath()) == [
        ("WARNING", "before any tree"),
        ("DEBUG", "also before"),
        ("INFO", f"New tree created: '{tmp_path / 'first_BT.json'}'"),
    ]
    assert bt.NewTree(str(tmp_path / "second.json"))
    assert messages(bt.GetLogFilePath()) == [f"New tree created: '{tmp_path / 'second_BT.json'}'"]


def test_early_messages_reach_the_first_file_even_when_that_tree_logs_nothing(logs, make_widget, tmp_path):
    path = write_tree(tmp_path / "strict_BT.json", config={"log_level": 1})  # "Tree loaded" (INFO) is dropped
    bt = make_widget()
    assert bt.Log(LogLevel.WARNING, "logged before any tree")
    assert bt.LoadTree(path)
    bt.Shutdown()
    assert flush_logs()
    assert os.path.exists(bt.GetLogFilePath())
    assert messages(bt.GetLogFilePath()) == ["logged before any tree"]


def test_shutdown_closes_the_log(tree, qtbot):
    deliver(qtbot)
    every = record(tree.messageLogged)
    assert tree.Log(LogLevel.ERROR, "before shutdown")
    tree.Shutdown()
    assert not tree.Log(LogLevel.ERROR, "after shutdown")
    assert not tree.tree_logger().enabled(LogLevel.ERROR)
    found = messages(tree.GetLogFilePath())
    assert found[-1] == "before shutdown"
    assert "after shutdown" not in found
    deliver(qtbot)
    assert (1, "after shutdown") not in every


# ============================================================================ configuration
def test_the_configure_dialog_offers_the_log_level(tree, dialogs, make_widget):
    assert tree.SaveTree(tree.GetFilePath())
    seen = {}

    def choose_warning(dialog):
        box = dialog.findChild(QComboBox, "LogLevel")
        seen["items"] = [box.itemText(index) for index in range(box.count())]
        seen["data"] = [box.itemData(index) for index in range(box.count())]
        seen["current"] = box.currentText()
        box.setCurrentIndex(box.findText("2: WARNING"))
        return True

    dialogs.dialog_handler = choose_warning
    tree.Configure()
    assert seen == {"items": LABELS, "data": [0, 1, 2, 3, 4], "current": "4: INFO"}
    assert tree.GetLogLevel() is LogLevel.WARNING
    assert tree.tree_logger().level() is LogLevel.WARNING
    assert tree.IsModified()
    assert not tree.Log("info", "dropped") and tree.Log("warning", "kept")

    def look(dialog):
        seen["again"] = dialog.findChild(QComboBox, "LogLevel").currentText()
        dialog.findChild(QComboBox, "LogLevel").setCurrentIndex(0)
        return False  # Cancel: nothing changes

    dialogs.dialog_handler = look
    tree.Configure()
    assert seen["again"] == "2: WARNING"
    assert tree.GetLogLevel() is LogLevel.WARNING

    assert tree.SaveTree(tree.GetFilePath())
    assert read_json(tree.GetFilePath())["config"]["log_level"] == 2
    other = make_widget()
    assert other.LoadTree(tree.GetFilePath())
    assert other.GetLogLevel() is LogLevel.WARNING
    assert other.tree_logger().level() is LogLevel.WARNING
    assert not other.Log("debug", "dropped")


def test_the_default_log_level_is_not_saved(tree):
    assert tree.SaveTree(tree.GetFilePath())
    assert "log_level" not in read_json(tree.GetFilePath())["config"]
    tree.SetLogLevel(LogLevel.NONE)
    assert tree.SaveTree(tree.GetFilePath())
    assert read_json(tree.GetFilePath())["config"]["log_level"] == 0
    tree.SetLogLevel(LogLevel.INFO)
    assert tree.IsModified()
    assert tree.SaveTree(tree.GetFilePath())
    assert "log_level" not in read_json(tree.GetFilePath())["config"]


def test_a_new_tree_starts_at_info(tree, tmp_path):
    tree.SetLogLevel(LogLevel.ERROR)
    assert tree.NewTree(str(tmp_path / "fresh.json"))
    assert tree.GetLogLevel() is LogLevel.INFO
    assert tree.tree_logger().level() is LogLevel.INFO


@pytest.mark.parametrize("value, error", [(5, ValueError), (-1, ValueError), (True, TypeError), ("2", TypeError), (2.0, TypeError), (None, TypeError)])
def test_set_config_rejects_invalid_log_levels(tree, value, error):
    config = tree.GetConfig()
    config.log_level = value
    with pytest.raises(error):
        tree.SetConfig(config)
    assert tree.GetLogLevel() is LogLevel.INFO
    assert tree.tree_logger().level() is LogLevel.INFO


def test_set_config_stores_a_log_level_enum_as_int(tree):
    config = tree.GetConfig()
    config.log_level = LogLevel.DEBUG
    tree.SetConfig(config)
    assert type(tree.GetConfig().log_level) is int and tree.GetConfig().log_level == 3
    assert tree.GetLogLevel() is LogLevel.DEBUG


@pytest.mark.parametrize("value", [7, "loud", True, None])
def test_set_log_level_rejects_invalid_levels(tree, value):
    with pytest.raises((ValueError, TypeError)):
        tree.SetLogLevel(value)
    assert tree.GetLogLevel() is LogLevel.INFO


@pytest.mark.parametrize("saved, expected", [(0, 0), (1, 1), (3, 3), (9, 4), (-1, 4), (True, 4), ("1", 4), (2.0, 4), (None, 4)])
def test_from_dict_ignores_invalid_log_levels(saved, expected):
    assert TreeConfig.from_dict({"log_level": saved}).log_level == expected


def test_to_dict_leaves_out_the_default_log_level():
    assert "log_level" not in TreeConfig().to_dict()
    assert TreeConfig(log_level=3).to_dict()["log_level"] == 3
    assert TreeConfig.from_dict(TreeConfig(log_level=0).to_dict()).log_level == 0


def test_a_tree_file_with_an_invalid_log_level_loads_at_info(logs, make_widget, tmp_path):
    bt = make_widget()
    assert bt.LoadTree(write_tree(tmp_path / "odd_BT.json", config={"log_level": 12}))
    assert bt.GetLogLevel() is LogLevel.INFO
    assert bt.LoadTree(write_tree(tmp_path / "errors_BT.json", config={"log_level": 1}))
    assert bt.GetLogLevel() is LogLevel.ERROR
    assert bt.tree_logger().level() is LogLevel.ERROR


# ============================================================================ the Python loggers
def test_tree_log_records_do_not_propagate_to_the_python_loggers(tree, qtbot, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="behavior_tree_widget")
    under_root(tree, "Succeed")
    assert tree.Log("error", "tree log only 4711")
    assert execute(qtbot, tree) == ["Succeeded"]
    found = messages(tree.GetLogFilePath())
    assert "tree log only 4711" in found and "Node 'Succeed' (Succeed) executed: Succeeded" in found
    assert "4711" not in caplog.text
    assert "executed: Succeeded" not in caplog.text
    assert "Execution started" not in caplog.text
