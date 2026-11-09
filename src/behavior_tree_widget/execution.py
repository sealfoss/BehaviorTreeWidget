"""Execution of the graphical tree with py_trees.

When execution starts, the node graph is translated into a py_trees tree (see
:mod:`.tree_builder`): Root / Sequence / Selector nodes become ``py_trees.composites``, Negation
nodes ``py_trees.decorators.Inverter``, Sub-Tree nodes a decorator over the tree of their file
and leaf nodes a :class:`~behavior_tree_widget.behaviours.LeafBehaviour` calling
``LeafNodeWidget.OnRun``.

Every node that starts running, finishes or is interrupted is written to the tree's log at the
INFO level (see :mod:`behavior_tree_widget.tree_log`), node errors at the ERROR level.

An *execution loop* is one run of the tree from the Root until the Root returns (with Loop
Execution, every repetition is a loop of its own); Stop and Reset / Restart end the loop
in progress. At the end of every loop, blackboard entries whose Reset flag is set get
back the value they had when the loop started.

The tree is ticked on the GUI thread by a QTimer. Leaves whose ``RUN_IN_THREAD`` is
True run ``OnRun`` on a daemon worker thread (reused from call to call) and report
``RUNNING`` until it returns.

Lifecycle of one run of a leaf node::

    OnStart (GUI thread) -> OnRun ... OnRun (until it returns a final result)
    -> OnTerminate (GUI thread, once)

A run interrupted by Stop, Reset, preemption or loading another tree is cancelled:
its token is set (``CancelRequested()``), ``OnTerminate(..., READY)`` is called, the
result of a still executing ``OnRun`` call is ignored and that call can no longer
modify the tree. A node never executes two ``OnRun`` calls at the same time: a new
run waits (showing Running) until an interrupted call has returned.
"""

from __future__ import annotations

import enum
import functools
import logging
import threading
import time
from concurrent.futures import Future
from concurrent.futures import wait as wait_futures
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterator

import py_trees
import shiboken6
from py_trees.common import Status
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal

from ._runctx import RunToken, check_not_cancelled, current_token
from ._workers import WorkerPool, leaf_pool, run_in_daemon_thread
from .behaviours import (
    FailingBehaviour,
    LeafBehaviour,
    SubTreeBehaviour,
    TickVisitor,
    call_on_run,
    describe_error,
    result_to_status,
)
from .execution_host import ExecutionHost, WidgetExecutionHost
from .nodes import NodeStatus, NodeWidget
from .tree_builder import TreeBuilder
from .tree_log import PYTHON_LEVELS, LogLevel

if TYPE_CHECKING:  # pragma: no cover
    from .widget import BehaviorTreeWidget

log = logging.getLogger("behavior_tree_widget")

__all__ = [
    "CommandQueue",
    "ExecutionHost",
    "ExecutionState",
    "FailingBehaviour",
    "LeafBehaviour",
    "RunToken",
    "SubTreeBehaviour",
    "TreeExecutor",
    "WidgetExecutionHost",
    "WorkerPool",
    "call_on_run",
    "result_to_status",
    "run_in_daemon_thread",
]

class ExecutionState(str, enum.Enum):
    IDLE = "Idle"
    RUNNING = "Running"
    PAUSED = "Paused"

    def __str__(self) -> str:
        return self.value


class CommandQueue:
    """The order in which execution commands are carried out (GUI thread only).

    ``pending`` holds the commands (names) and callables issued while a tick, another command or a
    deferred first tick was in progress; they are carried out afterwards, in the order they were
    issued. ``busy`` counts the commands being carried out (:meth:`carrying_out`), and
    :meth:`after_tick` marks the section after a tick in which its signals are emitted.
    """

    def __init__(self) -> None:
        self.pending: list = []
        self.busy = 0
        self._after_tick_levels: list[int] = []  # event-loop level of each after-tick section in progress

    @contextmanager
    def carrying_out(self) -> Iterator[None]:
        """A command (or the end of a run) is being carried out: anything issued meanwhile is queued."""
        self.busy += 1
        try:
            yield
        finally:
            self.busy -= 1

    @contextmanager
    def after_tick(self) -> Iterator[None]:
        """The signals of a tick are being emitted (a run started from them gets its first tick from
        the event loop, so chained runs do not recurse)."""
        self._after_tick_levels.append(QThread.currentThread().loopLevel())
        try:
            yield
        finally:
            self._after_tick_levels.pop()

    def in_after_tick(self) -> bool:
        """True while the signals of a tick are emitted, also in event loops opened meanwhile."""
        return bool(self._after_tick_levels)

    def in_post_tick_handler(self) -> bool:
        """True in code run by the signals emitted after a tick, but not in an event loop that
        code opened (e.g. a modal dialog): events delivered there come from elsewhere."""
        levels = self._after_tick_levels
        return bool(levels) and QThread.currentThread().loopLevel() <= levels[-1]


@dataclass
class _TickOutcome:
    """What a tick did (see TreeExecutor._tick_tree)."""

    did_tick: bool = False  # the tree was ticked (not only finished)
    finished: Status | None = None  # the Root returned and the run ends (run once)
    error: BaseException | None = None  # the tick itself failed
    aborted: bool = False  # Stop / Reset was issued during the tick


#: Hidden sub-tree nodes whose widget was destroyed while their OnRun call still ran (kept, see
#: TreeExecutor._on_subtree_call_done).
_orphaned_nodes: list = []

#: The state a node shows for the status of its behaviour (INVALID: see _refresh_statuses).
_SHOWN_STATUS = {Status.RUNNING: NodeStatus.RUNNING, Status.SUCCESS: NodeStatus.SUCCEEDED, Status.FAILURE: NodeStatus.FAILED}


class TreeExecutor(QObject):
    """Builds and ticks the py_trees tree for a :class:`BehaviorTreeWidget`.

    Commands (Execute/Pause/Stop/Reset):

    * called from a worker thread, they are carried out on the GUI thread (Stop and
      Reset cancel running nodes at once);
    * called while a tick is in progress (from ``OnRun``/``OnStart``/``OnTerminate``,
      a signal emitted during the tick, or a dialog opened during it), they are
      carried out right after that tick; after a Stop/Reset no more node code runs in
      that tick;
    * called while another command is being carried out (e.g. from ``OnTerminate``
      during Stop, or from a ``stateChanged`` slot), they are carried out after it.

    Waiting for worker threads: a tick waits up to a leaf's ``THREAD_WAIT`` for an ``OnRun``
    call it has just started on a worker thread, so quick calls finish within the tick. Once a
    tick has spent :attr:`TICK_WAIT_BUDGET` seconds waiting for such calls, the calls started
    later in that tick are not waited for: their nodes show Running and their results are
    collected on a later tick. That bounds how long one tick can keep the GUI waiting.

    The executor uses the widget only through an :class:`~.execution_host.ExecutionHost`.
    """

    stateChanged = Signal(str)  # "Idle", "Running", "Paused"
    finished = Signal(str)  # "Succeeded" / "Failed" when a run-once execution completes
    ticked = Signal(int)  # tick count since execution started
    nodeStatusChanged = Signal(object, str)  # node, "Ready"/"Running"/"Succeeded"/"Failed"
    nodeError = Signal(object, str)  # node, error message
    _commandRequested = Signal(str, int)  # (command, generation) from worker threads, run on the GUI thread
    _subtreeNodeIdle = Signal(object)  # a released sub-tree node's OnRun call returned (from its worker thread)

    COMMANDS = ("start", "pause", "stop", "reset")
    #: Seconds one tick may spend waiting for worker-thread OnRun calls it started (see the class docs).
    TICK_WAIT_BUDGET = 0.1

    def __init__(self, owner: "BehaviorTreeWidget", host: ExecutionHost | None = None, pool: WorkerPool | None = None):
        """``host``: what the executor needs from ``owner`` (by default a
        :class:`~.execution_host.WidgetExecutionHost`); ``pool``: the worker threads running
        ``OnRun`` calls (by default the threads shared by every executor)."""
        super().__init__(owner)
        self._host: ExecutionHost = host if host is not None else WidgetExecutionHost(owner)
        self._pool = pool
        self._gui_thread = threading.get_ident()
        self._state = ExecutionState.IDLE
        self._tree: py_trees.trees.BehaviourTree | None = None
        self._behaviours: dict[NodeWidget, py_trees.behaviour.Behaviour] = {}
        self._labels: dict[py_trees.behaviour.Behaviour, str] = {}  # how the log names each node
        self._nodes_of: dict[py_trees.behaviour.Behaviour, NodeWidget] = {}  # the node of each behaviour
        self._visitor: TickVisitor | None = None
        self._subtree_nodes: list[NodeWidget] = []  # hidden nodes created for Sub-Tree nodes
        # OnRun calls of released sub-tree nodes still running, by the place of their node (the ids
        # of the Sub-Tree nodes leading to it and its own id): the node built for the same place
        # by the next Execute waits for that call, as a node of the view waits for its own.
        self._subtree_calls: dict[tuple[str, ...], Future] = {}
        # The nodes of the sub-trees shown, by place (see _shown_twins), and what they were made from.
        self._twins: dict | None = None
        self._twins_key: object = None
        self._tick_count = 0
        self._ticking = False
        self._aborting = False
        self._tick_waited = 0.0  # seconds the tick in progress spent waiting for worker-thread calls
        self._queue = CommandQueue()
        self._shut_down = False
        self._blackboard_snapshot = None
        # Incremented whenever a run starts, is stopped or reset (or the tree replaced): a
        # command queued from a worker thread is dropped if the generation changed meanwhile.
        self._generation = 0
        self._token_owner = object()  # marks the run tokens of this executor
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # First tick of a run started from a signal handler after a tick: delivered from the
        # event loop, so chained runs (e.g. Execute from executionFinished) do not recurse.
        self._first_tick = QTimer(self)
        self._first_tick.setSingleShot(True)
        self._first_tick.setInterval(0)
        self._first_tick.timeout.connect(self._on_first_tick)
        self._commandRequested.connect(self._on_worker_command, Qt.ConnectionType.QueuedConnection)
        self._subtreeNodeIdle.connect(self._delete_subtree_node, Qt.ConnectionType.QueuedConnection)

    @property
    def _owner(self) -> "BehaviorTreeWidget":
        """The BehaviorTreeWidget (RuntimeError once it was deleted)."""
        return self._host.tree_widget()

    @property
    def _pending(self) -> list:
        """The commands and callables waiting to be carried out (see :class:`CommandQueue`)."""
        return self._queue.pending

    def host(self) -> ExecutionHost:
        return self._host

    # ------------------------------------------------------------------ used by the behaviours (LeafExecutor)
    @property
    def token_owner(self) -> object:
        """Marks the run tokens of this executor."""
        return self._token_owner

    def tree_widget(self) -> "BehaviorTreeWidget":
        """The ``tree`` passed to the nodes' OnStart / OnRun / OnTerminate."""
        return self._host.tree_widget()

    def generation(self) -> int:
        return self._generation

    def is_aborting(self) -> bool:
        """True while a Stop / Reset issued during the tick is pending: no more node code runs."""
        return self._aborting

    def label(self, behaviour: py_trees.behaviour.Behaviour) -> str:
        """How the log names the node of ``behaviour``."""
        label = self._labels.get(behaviour)
        return label if label is not None else repr(behaviour.name)

    _label = label

    def report(self, level: LogLevel, message: str, exc_info: BaseException | None = None) -> None:
        """Write ``message`` to the library's logger (with the traceback of ``exc_info``) and to the
        tree's log."""
        log.log(PYTHON_LEVELS.get(level, logging.INFO), "%s", message, exc_info=exc_info)
        self.log_message(level, message)

    _report = report

    def log_message(self, level: LogLevel, message: str) -> None:
        """Write ``message`` to the tree's log only (never raises)."""
        try:
            self._host.log(level, message)
        except RuntimeError:  # the widget is being destroyed
            pass

    _log = log_message

    def log_enabled(self, level: LogLevel) -> bool:
        """Whether a message of ``level`` would be written to the tree's log now."""
        try:
            return self._host.log_enabled(level)
        except RuntimeError:
            return False

    _log_enabled = log_enabled

    def start_call(self, function: Callable[[], Any], name: str) -> Future:
        """Run ``function()`` (an ``OnRun`` call) on a worker thread named ``name``."""
        pool = self._pool if self._pool is not None else leaf_pool()
        return pool.submit(function, name)

    def wait_for_call(self, future: Future, wait: float) -> bool:
        """Wait up to ``wait`` seconds (a leaf's ``THREAD_WAIT``) for a worker-thread call just
        started in this tick. Returns False when it was not waited for: ``wait`` is 0, or the tick
        already spent :attr:`TICK_WAIT_BUDGET` seconds waiting."""
        if wait <= 0.0 or self._tick_waited >= self.TICK_WAIT_BUDGET:
            return False
        started = time.perf_counter()
        try:
            wait_futures([future], timeout=wait)
        finally:
            self._tick_waited += time.perf_counter() - started
        return True

    # ------------------------------------------------------------------ properties
    def state(self) -> ExecutionState:
        return self._state

    def tick_count(self) -> int:
        return self._tick_count

    def tree(self) -> py_trees.trees.BehaviourTree | None:
        """The py_trees tree being executed (None when idle)."""
        return self._tree

    def behaviour_for(self, node: NodeWidget) -> py_trees.behaviour.Behaviour | None:
        return self._behaviours.get(node)

    def is_busy(self) -> bool:
        """True while a tick or a command is being carried out, or a deferred first tick is pending."""
        return self._ticking or self._queue.busy > 0 or self._first_tick.isActive()

    def set_interval(self, interval_ms: int) -> None:
        self._timer.setInterval(max(1, int(interval_ms)))

    # ------------------------------------------------------------------ commands
    def start(self) -> None:
        """Execute: start a new run when idle, resume when paused."""
        self._command("start")

    def pause(self) -> None:
        self._command("pause")

    def stop(self) -> None:
        """Halt execution and reset every node to Ready."""
        self._command("stop")

    def reset(self) -> None:
        """Reset every node to Ready without changing the running / paused state."""
        self._command("reset")

    def call_when_idle(self, function) -> None:
        """Call ``function()`` now, or after the current tick / command (GUI thread only)."""
        if self.is_busy() or self._queue.pending:
            self._queue.pending.append(function)  # never before items queued earlier
            self._drain()
        else:
            function()

    def shutdown(self) -> None:
        """Stop execution for good (used when the widget is closed for the last time)."""
        self._shut_down = True
        self._cancel_runs()
        if threading.get_ident() != self._gui_thread:
            self._commandRequested.emit("stop", -1)
            return
        self._timer.stop()
        self._first_tick.stop()
        if self.is_busy():
            if self._ticking:
                self._aborting = True
            self._queue.pending.append("stop")
            return
        try:
            self._run_command("stop")
        except RuntimeError:  # the widget is being destroyed
            log.debug("shutdown while the widget is being destroyed", exc_info=True)
            return
        self._drain()

    def abandon(self) -> None:
        """The widget was destroyed without ``shutdown()``: run no more node code and cancel the
        running calls (any thread; nothing else is touched)."""
        self._shut_down = True
        self._aborting = True  # a tick in progress runs no more node code
        for behaviour in list(self._behaviours.values()):
            if isinstance(behaviour, LeafBehaviour):
                behaviour.cancel()

    def _command(self, name: str) -> None:
        if name not in self.COMMANDS:
            raise ValueError(f"unknown command {name!r}")
        if threading.get_ident() != self._gui_thread:
            # An interrupted OnRun call must not control a later run or another tree.
            check_not_cancelled(name.capitalize() if name != "start" else "Execute")
            # Commands from an OnRun call of this tree belong to that call's run: they are
            # dropped if the run ended meanwhile. Commands from other threads (or from an OnRun
            # call of another tree) are always carried out, in order.
            token = current_token()
            generation = token.generation if token is not None and token.owner is self._token_owner else -1
            if name in ("stop", "reset"):
                # thread-safe; only the runs this command is meant for (the rest happens on the GUI thread)
                self._cancel_runs(None if generation < 0 else generation)
            self._commandRequested.emit(name, generation)
            return
        if self._ticking:
            if name in ("stop", "reset"):
                self._aborting = True
                self._cancel_runs()
                if name == "stop":
                    self._timer.stop()
            self._queue.pending.append(name)
            return
        queue = self._queue
        if name in ("stop", "pause") and not queue.in_post_tick_handler() and not queue.busy and self._first_tick.isActive():
            # Issued from the event loop (e.g. the Stop button while runs are chained from
            # executionFinished): takes effect before the pending first tick.
            self._first_tick.stop()
        if self.is_busy() or queue.pending:
            # Carried out after the current command or the deferred first tick, and never
            # before items queued earlier.
            queue.pending.append(name)
            self._drain()
            return
        self._run_command(name)
        self._drain()

    def _in_post_tick_handler(self) -> bool:
        return self._queue.in_post_tick_handler()

    def _on_worker_command(self, name: str, generation: int) -> None:
        """A command issued on a worker thread, delivered on the GUI thread."""
        if generation >= 0 and generation != self._generation:
            # Meanwhile the run was stopped, reset or restarted, or the tree replaced: the
            # command was meant for a run that no longer exists.
            log.debug("dropped %r command issued for an earlier run", name)
            return
        self._command(name)

    def _on_first_tick(self) -> None:
        self._tick()
        self._drain()  # items queued behind the start (the tick may have returned early)

    def _first_tick_if_fresh(self, tree) -> None:
        """Give a run its first tick if commands queued during its start did not stop it."""
        if tree is not None and tree is self._tree and self._state is ExecutionState.RUNNING and self._tick_count == 0:
            if self._queue.in_after_tick():
                self._first_tick.start()
            else:
                self._tick()

    def _run_command(self, name: str) -> None:
        queue = self._queue
        queued_before = len(queue.pending)
        with queue.carrying_out():
            tick_now = getattr(self, f"_do_{name}")()
        # A command issued while this one was carried out (e.g. Stop from a
        # stateChanged('Running') slot) takes effect before any node code runs; the first
        # tick follows it if the run is still going.
        issued_meanwhile = len(queue.pending) > queued_before
        if not tick_now:
            return
        if issued_meanwhile:
            queue.pending.append(functools.partial(self._first_tick_if_fresh, self._tree))
        elif queue.in_after_tick():
            self._first_tick.start()  # started from a post-tick signal handler
        else:
            self._tick()

    def _drain(self) -> None:
        """Carry out commands queued while a tick or command was in progress."""
        # While a deferred first tick is pending, the rest of the queue waits for it so
        # queued commands keep their order (the first tick drains them).
        pending = self._queue.pending
        while pending and not self.is_busy():
            item = pending.pop(0)
            try:
                if callable(item):
                    item()
                else:
                    self._run_command(item)
            except Exception:  # noqa: BLE001 - never let a queued command escape a Qt slot
                log.exception("deferred execution command %r failed", item)

    # ------------------------------------------------------------------ command bodies (GUI thread)
    def _do_start(self) -> bool:
        if self._shut_down:
            log.warning("execution is not possible after Shutdown()")
            return False
        if self._state is ExecutionState.RUNNING:
            return False
        host = self._host
        if self._state is ExecutionState.IDLE:
            self._build()
            self._generation += 1
            self._tick_count = 0
            self._set_all_ready()
            config = host.config()
            self._blackboard_snapshot = host.blackboard().snapshot() if config.restore_blackboard else None
            self._begin_loop()
            self._log(LogLevel.INFO, "Execution started")
        else:
            self._log(LogLevel.INFO, "Execution resumed")
        self.set_interval(host.config().tick_interval_ms)
        self._set_state(ExecutionState.RUNNING)
        if self._shut_down or self._state is not ExecutionState.RUNNING:
            return False
        self._timer.start()
        return True

    def _do_pause(self) -> bool:
        if self._state is ExecutionState.RUNNING:
            self._timer.stop()
            self._log(LogLevel.INFO, "Execution paused")
            self._set_state(ExecutionState.PAUSED)
        return False

    def _do_stop(self) -> bool:
        was_active = self._state is not ExecutionState.IDLE
        self._generation += 1
        self._timer.stop()
        self._first_tick.stop()
        self._halt_tree()
        self._forget_tree()
        self._set_all_ready()
        self._end_loop()  # Stop ends the loop in progress
        if was_active:
            self._log(LogLevel.INFO, "Execution stopped")
            self._restore_blackboard()
        self._blackboard_snapshot = None
        self._set_state(ExecutionState.IDLE)
        self._timer.stop()
        return False

    def _do_reset(self) -> bool:
        self._generation += 1
        self._halt_tree()
        idle = self._state is ExecutionState.IDLE
        self._log(LogLevel.INFO, "Tree reset" if idle else "Tree reset: execution starts again from the Root")
        if idle:
            self._forget_tree()
        self._set_all_ready()
        self._end_loop()  # the tree starts over: the loop in progress ends ...
        self._restore_blackboard()
        if idle:
            self._blackboard_snapshot = None
        else:
            self._begin_loop()  # ... and the next tick starts a new one
        self._tick_count = 0
        return False

    def discard_snapshot(self) -> None:
        """Forget the blackboard snapshot (e.g. the tree is being replaced)."""
        self._blackboard_snapshot = None

    # ------------------------------------------------------------------ execution loops
    def _begin_loop(self) -> None:
        """An execution loop starts: record the loop-start values of the entries changed from now on."""
        try:
            store = self._host.blackboard()
        except RuntimeError:  # the widget was deleted
            return
        if not store.is_disposed():
            store.start_loop_capture()

    def _end_loop(self) -> None:
        """The execution loop in progress ends: entries with the Reset flag get their loop-start value back."""
        try:
            store = self._host.blackboard()
        except RuntimeError:  # the widget was deleted
            return
        captured = store.finish_loop_capture()
        if not captured or store.is_disposed():
            return
        # Undoing run-time values is itself a run-time change (not an edit of the tree).
        store.set_runtime_active(True)
        try:
            store.reset_entries(captured)
        except Exception:  # noqa: BLE001 - e.g. a signal handler failed; never stop execution for it
            log.exception("resetting blackboard entries at the end of the loop failed")
        finally:
            store.set_runtime_active(self._state is not ExecutionState.IDLE)

    # ------------------------------------------------------------------ internals
    def _set_state(self, state: ExecutionState) -> None:
        if state is not self._state:
            self._state = state
            self._host.blackboard().set_runtime_active(state is not ExecutionState.IDLE)
            self.stateChanged.emit(state.value)

    def _build(self) -> None:
        host = self._host
        root = host.root()  # the tree itself, also while a sub-tree is shown
        if root is None:
            raise RuntimeError("the tree has no root node")
        self._release_subtree_nodes()
        builder = TreeBuilder(self, host, self._subtree_calls)
        try:
            built = builder.build(root, host.tree_file())
        except BaseException:
            self._subtree_nodes = builder.subtree_nodes
            self._release_subtree_nodes()
            raise
        visitor = TickVisitor(self, built.labels)
        built.tree.add_visitor(visitor)
        self._tree = built.tree
        self._behaviours = built.behaviours
        self._labels = built.labels
        self._nodes_of = built.nodes
        self._subtree_nodes = built.subtree_nodes
        self._visitor = visitor
        self.invalidate_shown_twins()  # the first refresh of a run shows every node's state

    def _forget_tree(self) -> None:
        """The tree that was executed is no longer needed (the run ended or the tree is replaced)."""
        self._tree = None
        self._behaviours = {}
        self._labels = {}
        self._nodes_of = {}
        self._visitor = None
        self._release_subtree_nodes()

    def _release_subtree_nodes(self) -> None:
        """Delete the hidden nodes created for Sub-Tree nodes (the tree they were built for is gone).

        A node whose (interrupted) OnRun call is still running on its worker thread is deleted
        once that call returns, so the call never meets a deleted widget; meanwhile the node
        built for the same place by the next Execute waits for it (see ``_build``).
        """
        nodes, self._subtree_nodes = self._subtree_nodes, []
        self._subtree_calls = {place: call for place, call in self._subtree_calls.items() if not call.done()}
        for node in nodes:
            if not shiboken6.isValid(node):
                continue
            call = node._worker_future
            if call is None or call.done():
                node.deleteLater()
                continue
            place = node.__dict__.get("_subtree_place")
            if place is not None:
                self._subtree_calls[place] = call
            call.add_done_callback(functools.partial(self._on_subtree_call_done, node))

    def _on_subtree_call_done(self, node: NodeWidget, _call: Future) -> None:
        """The OnRun call of a released sub-tree node returned (on its worker thread)."""
        try:
            self._subtreeNodeIdle.emit(node)  # deleted on the GUI thread
        except RuntimeError:  # the executor is gone (the widget was destroyed): never delete the
            _orphaned_nodes.append(node)  # node here, on a worker thread; it is kept until exit

    def _delete_subtree_node(self, node: NodeWidget) -> None:
        if shiboken6.isValid(node):
            node.deleteLater()

    def subtree_nodes(self) -> list[NodeWidget]:
        """The hidden nodes running the trees of the Sub-Tree nodes while the tree executes (for tests)."""
        return list(self._subtree_nodes)

    def _halt_tree(self) -> None:
        """Interrupt every running node (terminate -> OnTerminate, cancel tokens)."""
        if self._tree is None:
            return
        try:
            self._tree.root.stop(Status.INVALID)
        except Exception:  # noqa: BLE001 - e.g. user OnTerminate failures are logged already
            log.exception("error while halting the behavior tree")
            self._cancel_runs()

    def _cancel_runs(self, generation: int | None = None) -> None:
        """Cancel the tokens of the node runs in progress (safe from any thread).

        With ``generation``, only runs started in that executor generation are cancelled.
        """
        for behaviour in list(self._behaviours.values()):
            if isinstance(behaviour, LeafBehaviour) and behaviour._active:
                token = behaviour.token  # read once: checked and cancelled together
                if token is not None and (generation is None or token.generation == generation):
                    token.cancel()

    # ------------------------------------------------------------------ ticks
    def _tick(self) -> None:
        if self.is_busy() or self._tree is None or self._state is not ExecutionState.RUNNING:
            return
        outcome = self._tick_tree()
        with self._queue.after_tick():
            self._after_tick(outcome)

    def _tick_tree(self) -> _TickOutcome:
        """Tick the tree once; a Root that returned in the previous tick first starts a new loop
        (Loop Execution) or ends the run. Commands issued by node code meanwhile are queued."""
        outcome = _TickOutcome()
        self._ticking = True
        self._tick_waited = 0.0
        try:
            tree = self._tree
            config = self._host.config()
            root = tree.root
            if root.status in (Status.SUCCESS, Status.FAILURE):
                if config.repeat:
                    # Start every round from a clean tree. py_trees Selectors do not
                    # reset their children on a new round, which would leave stale statuses.
                    root.stop(Status.INVALID)
                    self._set_all_ready()
                    self._begin_loop()
                    self._log(LogLevel.INFO, "Loop Execution: the tree starts again from the Root")
                else:
                    outcome.finished = root.status
            if outcome.finished is None:
                tree.tick()
                outcome.did_tick = True
                self._tick_count += 1
                if not self._aborting:
                    self._refresh_statuses()
                    if root.status in (Status.SUCCESS, Status.FAILURE):
                        self._end_loop()  # the Root returned: this loop is complete
                        if not config.repeat:
                            outcome.finished = root.status
        except Exception as caught:  # noqa: BLE001
            outcome.error = caught
        finally:
            self._ticking = False
            outcome.aborted, self._aborting = self._aborting, False
        return outcome

    def _after_tick(self, outcome: _TickOutcome) -> None:
        """Finish a tick: stop after a failed tick, end a finished run, emit the tick's signals and
        carry out what was issued meanwhile."""
        error = outcome.error
        if error is not None:
            self.report(LogLevel.ERROR, f"The tick failed: {describe_error(error)}", exc_info=error)
            with self._queue.carrying_out():
                self._do_stop()
                root_node = self._host.root()
                if root_node is not None:
                    self._apply_status(root_node, NodeStatus.FAILED, f"Tick failed: {describe_error(error)}")
            self.finished.emit(NodeStatus.FAILED.value)
            self._drain()
            return
        if outcome.aborted:
            self._drain()  # the deferred Stop / Reset
            return
        if outcome.finished is not None:
            with self._queue.carrying_out():
                self._timer.stop()
                self._forget_tree()
                self._log(LogLevel.INFO, f"Execution finished: {NodeStatus.from_py_trees(outcome.finished).value}")
                self._set_state(ExecutionState.IDLE)
        if outcome.did_tick:
            self.ticked.emit(self._tick_count)
        if outcome.finished is not None:
            self.finished.emit(NodeStatus.from_py_trees(outcome.finished).value)
        self._drain()

    # ------------------------------------------------------------------ statuses
    def invalidate_shown_twins(self) -> None:
        """The sub-trees shown with Zoom In changed: the next status refresh looks them up again
        and shows the state of every node. (A change is also noticed by itself, through
        ``ExecutionHost.shown_twins_key``.)"""
        self._twins = None

    def _shown_twins(self) -> tuple[dict, bool]:
        """``({(scope, node id): node}, changed)``: the nodes of the sub-trees shown, and whether
        they changed since the last refresh."""
        key = self._host.shown_twins_key()
        if self._twins is None or key != self._twins_key:
            self._twins = self._host.shown_twins()
            self._twins_key = key
            return self._twins, True
        return self._twins, False

    def _refresh_statuses(self) -> None:
        """Show each node's latest state.

        Only the nodes whose behaviour ticked, or was running before the tick (a node interrupted
        by its parent is not ticked), can change: the cost follows what ran, not the size of the
        tree. Every node is refreshed on the first tick of a run and when the sub-trees shown
        changed.

        py_trees sets finished children back to INVALID in some situations (e.g. a
        Selector with memory invalidates the children that failed before its running
        child). Such nodes keep showing their last result (Succeeded / Failed) until
        the tree is reset; a node interrupted while running goes back to Ready.

        The hidden nodes running a Sub-Tree node's tree show their status on the nodes of that
        sub-tree when it is shown (zoomed into from that Sub-Tree node).
        """
        twins, everything = self._shown_twins()
        visitor = self._visitor
        nodes_of = self._nodes_of
        behaviours = list(nodes_of) if everything or visitor is None else visitor.changed()
        is_valid = shiboken6.isValid
        for behaviour in behaviours:
            node = nodes_of.get(behaviour)
            if node is None or not is_valid(node):
                continue
            if node._canvas is None:
                place = node.__dict__.get("_subtree_place")
                node = twins.get((place[:-1], place[-1])) if place and twins else None
                if node is None or not is_valid(node):
                    continue  # removed from the view, or a hidden node whose sub-tree is not shown
            status = behaviour.status
            if status is Status.INVALID:
                if node.GetStatus() is NodeStatus.RUNNING:
                    self._apply_status(node, NodeStatus.READY)
                continue
            shown = _SHOWN_STATUS[status]
            error = getattr(behaviour, "error", None) if shown is NodeStatus.FAILED else None
            if shown is not node.GetStatus() or error != node.GetError():
                self._apply_status(node, shown, error)

    def _apply_status(self, node: NodeWidget, status: NodeStatus, error: str | None = None) -> None:
        previous_status, previous_error = node.GetStatus(), node.GetError()
        node._set_status(status, error)
        if status is not previous_status:
            self.nodeStatusChanged.emit(node, status.value)
        if error and error != previous_error:
            self.nodeError.emit(node, error)

    def _set_all_ready(self) -> None:
        for node in self._host.shown_nodes():  # also the nodes of the sub-trees shown
            if shiboken6.isValid(node):
                self._apply_status(node, NodeStatus.READY)

    def _restore_blackboard(self) -> None:
        if self._blackboard_snapshot is None:
            return
        if not self._host.config().restore_blackboard:
            self._blackboard_snapshot = None  # the option was switched off meanwhile
            return
        store = self._host.blackboard()
        # Undoing run-time values is itself a run-time change (not an edit of the tree).
        store.set_runtime_active(True)
        try:
            store.restore(self._blackboard_snapshot)
        finally:
            store.set_runtime_active(self._state is not ExecutionState.IDLE)
