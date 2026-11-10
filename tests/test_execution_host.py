"""TreeExecutor driven through an ExecutionHost (no BehaviorTreeWidget), and its CommandQueue."""

from __future__ import annotations

from PySide6.QtCore import QEventLoop, QObject, QPointF, QTimer

from behavior_tree_widget import LeafNodeWidget, LogLevel, NodeStatus, RootNodeWidget, TreeConfig, TreeFileError
from behavior_tree_widget._workers import WorkerPool
from behavior_tree_widget.blackboard import BlackboardStore
from behavior_tree_widget.canvas import TreeView
from behavior_tree_widget.execution import CommandQueue, ExecutionHost, TreeExecutor, WidgetExecutionHost


class Probe(LeafNodeWidget):
    """Records the ``tree`` it is run with and writes the blackboard through it."""

    _title = "Probe"

    def OnRun(self, tree):
        type(self).trees.append(tree)
        tree.store.set("visits", tree.store.get("visits") + 1)
        return True


class StandInTree:
    """What the nodes get as ``tree`` in this test."""

    def __init__(self, store):
        self.store = store


class StandInHost:
    """An ExecutionHost without a BehaviorTreeWidget: one view, a blackboard and a message list."""

    def __init__(self, view: TreeView, store: BlackboardStore, config: TreeConfig):
        self.view = view
        self.store = store
        self.options = config
        self.messages: list[tuple[LogLevel, str]] = []
        self.tree = StandInTree(store)

    def tree_widget(self):
        return self.tree

    def root(self):
        return self.view.root()

    def tree_file(self):
        return None

    def config(self):
        return self.options

    def blackboard(self):
        return self.store

    def log(self, level, message):
        self.messages.append((level, message))

    def log_enabled(self, level):
        return True

    def load_subtree(self, path):
        raise TreeFileError("this host runs no sub-trees")

    def shown_nodes(self):
        return self.view.nodes()

    def shown_twins(self):
        return {}

    def shown_twins_key(self):
        return ()


def test_the_widget_adapter_provides_every_method_of_the_protocol():
    names = {name for name in vars(ExecutionHost) if not name.startswith("_")}
    assert names, "the protocol lists methods"
    assert all(callable(getattr(WidgetExecutionHost, name, None)) for name in names)
    assert all(callable(getattr(StandInHost, name, None)) for name in names)


def test_an_executor_runs_a_tree_through_a_stand_in_host(qtbot):
    Probe.trees = []
    view = TreeView()
    store = BlackboardStore()
    owner = QObject()
    pool = WorkerPool()
    try:
        store.add("visits", "Integer", 0)
        root = RootNodeWidget()
        view.add_node(root, QPointF(0.0, 0.0))
        probes = []
        for index in range(3):
            probe = Probe()
            view.add_node(probe, QPointF(index * 200.0, 200.0))
            view.connect_nodes(root, probe)
            probes.append(probe)
        host = StandInHost(view, store, TreeConfig(tick_interval_ms=5))
        executor = TreeExecutor(owner, host=host, pool=pool)
        finished = []
        executor.finished.connect(finished.append)
        executor.start()
        qtbot.waitUntil(lambda: finished == ["Succeeded"], timeout=5000)
        assert [probe.GetStatus() for probe in probes] == [NodeStatus.SUCCEEDED] * 3
        assert root.GetStatus() is NodeStatus.SUCCEEDED
        assert Probe.trees == [host.tree] * 3
        assert store.get("visits") == 3
        assert pool.started_count() >= 1, "the OnRun calls ran on the pool given to the executor"
        messages = [message for _, message in host.messages]
        assert messages[0] == "Execution started"
        assert "Node 'Probe' (Probe) executed: Succeeded" in messages
        assert messages[-1] == "Execution finished: Succeeded"
        executor.stop()
        assert root.GetStatus() is NodeStatus.READY
    finally:
        store.dispose(notify=False)
        view.clear()
        view.deleteLater()
        owner.deleteLater()


def test_command_queue_sections():
    queue = CommandQueue()
    assert queue.busy == 0 and not queue.in_after_tick() and not queue.in_post_tick_handler()
    with queue.carrying_out():
        with queue.carrying_out():
            assert queue.busy == 2
        assert queue.busy == 1
    assert queue.busy == 0
    with queue.after_tick():
        assert queue.in_after_tick() and queue.in_post_tick_handler()
    assert not queue.in_after_tick()


def test_an_event_loop_opened_after_a_tick_is_not_a_post_tick_handler(qapp):
    queue = CommandQueue()
    seen = {}
    with queue.after_tick():
        loop = QEventLoop()

        def inside():
            seen["after_tick"] = queue.in_after_tick()
            seen["handler"] = queue.in_post_tick_handler()
            loop.quit()

        QTimer.singleShot(0, inside)
        loop.exec()
    assert seen == {"after_tick": True, "handler": False}
