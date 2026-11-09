"""Execution of the graphical tree with py_trees.

When execution starts, the node graph is translated into a py_trees tree:

* Root / Sequence / Selector nodes -> ``py_trees.composites.Sequence`` /
  ``py_trees.composites.Selector`` (children ordered left to right),
* Negation nodes -> ``py_trees.decorators.Inverter`` (a Negation without a child fails),
* Sub-Tree nodes -> :class:`SubTreeBehaviour` over the tree read from the node's file (its
  nodes are created behind the scenes, never shown, and use this tree's blackboard),
* leaf nodes -> :class:`LeafBehaviour`, an adapter calling ``LeafNodeWidget.OnRun``.

Every node that starts running, finishes or is interrupted is written to the tree's log at the
INFO level (see :mod:`behavior_tree_widget.tree_log`), node errors at the ERROR level.

An *execution loop* is one run of the tree from the Root until the Root returns (with Loop
Execution, every repetition is a loop of its own); Stop and Reset / Restart end the loop
in progress. At the end of every loop, blackboard entries whose Reset flag is set get
back the value they had when the loop started.

The tree is ticked on the GUI thread by a QTimer. Leaves whose ``RUN_IN_THREAD`` is
True run ``OnRun`` on a daemon worker thread and report ``RUNNING`` until it returns.

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
import inspect
import logging
import os
import threading
import traceback
import weakref
from concurrent.futures import Future
from concurrent.futures import wait as wait_futures
from typing import TYPE_CHECKING, Any

import py_trees
import shiboken6
from py_trees.common import Status
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal

from ._common import file_key
from ._runctx import RunToken, bound, check_not_cancelled, current_token
from .nodes import SEQUENCE, CompositeNodeWidget, LeafNodeWidget, NegationNodeWidget, NodeStatus, NodeWidget
from .serialization import TreeFileError
from .subtree import SubTreeNodeWidget
from .tree_log import LogLevel

if TYPE_CHECKING:  # pragma: no cover
    from .widget import BehaviorTreeWidget

log = logging.getLogger("behavior_tree_widget")


class ExecutionState(str, enum.Enum):
    IDLE = "Idle"
    RUNNING = "Running"
    PAUSED = "Paused"

    def __str__(self) -> str:
        return self.value


def result_to_status(result: Any) -> tuple[Status, str | None]:
    """Convert an ``OnRun`` return value to a py_trees status (and an error message)."""
    if isinstance(result, Status):
        if result == Status.INVALID:
            return Status.FAILURE, "OnRun returned Status.INVALID"
        return result, None
    if isinstance(result, bool):
        return (Status.SUCCESS if result else Status.FAILURE), None
    return Status.FAILURE, (
        f"OnRun must return True, False or a py_trees.common.Status, not {type(result).__name__}"
        + (" (missing return statement?)" if result is None else "")
    )


def _describe(error: BaseException) -> str:
    try:
        text = str(error)
    except Exception:  # noqa: BLE001 - an exception whose __str__ fails
        text = "<unprintable error>"
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def run_in_daemon_thread(function, name: str) -> Future:
    """Run ``function()`` on a new daemon thread and return a Future for its result.

    Daemon threads are used (instead of a thread pool) so an OnRun call that never
    returns cannot block application exit or later executions.
    """
    future: Future = Future()

    def runner() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = function()
        except BaseException as error:  # noqa: BLE001 - reported through the future
            future.set_exception(error)
        else:
            future.set_result(result)

    threading.Thread(target=runner, name=name, daemon=True).start()
    return future


@functools.lru_cache(maxsize=None)
def _on_run_takes_tree(cls: type) -> bool:
    """True if ``cls.OnRun`` accepts the tree argument (``OnRun(self, tree)``)."""
    try:
        signature = inspect.signature(cls.OnRun)
    except (TypeError, ValueError):
        return True
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional >= 2  # self + tree


def call_on_run(node: LeafNodeWidget, tree: "BehaviorTreeWidget | None") -> Any:
    """Call ``node.OnRun(tree)`` (or ``node.OnRun()`` for overrides without the argument)."""
    if _on_run_takes_tree(type(node)):
        return node.OnRun(tree)
    return node.OnRun()


class LeafBehaviour(py_trees.behaviour.Behaviour):
    """py_trees behaviour that runs a :class:`LeafNodeWidget`'s ``OnStart``/``OnRun``/``OnTerminate``."""

    def __init__(self, node: LeafNodeWidget, executor: "TreeExecutor"):
        super().__init__(name=node.GetTitle())
        self.node = node
        self.executor = executor
        self.tree = executor._owner
        self.error: str | None = None
        self.token: RunToken | None = None
        self.generation = -1  # executor generation of the current run
        self._future: Future | None = None
        self._started = False
        self._active = False

    def initialise(self) -> None:
        self.error = None
        self.feedback_message = ""
        self._future = None
        self._started = False
        self.generation = self.executor._generation
        self.token = RunToken(self.generation, self.executor._token_owner)  # complete before it is published
        self.node._run_token = self.token
        self._active = True

    def update(self) -> Status:
        node = self.node
        if self.executor._aborting or (self.token is not None and self.token.is_cancelled()):
            # A Stop/Reset is pending: run no user code. RUNNING keeps py_trees from
            # terminating the node now; the pending halt interrupts it (OnTerminate(READY)).
            return Status.RUNNING
        previous = node._worker_future
        if self._future is None and previous is not None and not previous.done():
            # An interrupted OnRun call of this node is still finishing; wait for it.
            self.feedback_message = "waiting for the previous OnRun call to return"
            return Status.RUNNING
        if not self._started:
            self._started = True
            try:
                node.OnStart(self.tree)
            except Exception as error:  # noqa: BLE001 - user code
                self._record_error(error, "OnStart")
            if self.executor._aborting:
                return Status.RUNNING  # OnStart issued Stop/Reset
        if self.error is not None:
            return Status.FAILURE
        if node.RUN_IN_THREAD:
            if self._future is None:
                token = self.token
                try:
                    self._future = run_in_daemon_thread(
                        functools.partial(self._run_bound, token), name=f"behavior_tree_widget:{node.GetTitle()}"
                    )
                except Exception as error:  # noqa: BLE001 - e.g. "can't start new thread"
                    self._record_error(error, "OnRun")
                    return Status.FAILURE
                node._worker_future = self._future
                wait = max(0.0, float(getattr(node, "THREAD_WAIT", 0.0) or 0.0))
                if not wait:
                    return Status.RUNNING  # never wait: the result is collected on a later tick
                wait_futures([self._future], timeout=wait)
            if not self._future.done() or self.executor._aborting or self.token.is_cancelled():
                # Still executing, or interrupted meanwhile: an interrupted call's result
                # is ignored; the pending halt / preemption terminates the node.
                return Status.RUNNING
            future, self._future = self._future, None
            error = future.exception()
            if error is not None:
                self._record_error(error, "OnRun")
                return Status.FAILURE
            result = future.result()
        else:
            try:
                result = call_on_run(node, self.tree)
            except Exception as error:  # noqa: BLE001 - user code
                self._record_error(error, "OnRun")
                return Status.FAILURE
        status, message = result_to_status(result)
        if message is not None:
            self.error = message
            log.error("%s: %s", node, message)
            self.executor._log(LogLevel.ERROR, f"{self.executor._label(self)}: {message}")
        self.feedback_message = self.error or ""
        return status

    def _run_bound(self, token: RunToken) -> Any:
        with bound(token):
            return call_on_run(self.node, self.tree)

    def cancel(self) -> None:
        """Cancel the current run (sets its token)."""
        if self.token is not None:
            self.token.cancel()

    def terminate(self, new_status: Status) -> None:
        if self._future is not None:
            # Interrupted while OnRun is still executing on its worker thread.
            self.cancel()
            self._future = None
        elif new_status == Status.INVALID:
            self.cancel()
        if not self._active:
            return
        self._active = False
        if not self._started:
            return
        try:
            self.node.OnTerminate(self.tree, NodeStatus.from_py_trees(new_status))
        except Exception as error:  # noqa: BLE001 - user code
            log.error("%s: OnTerminate raised %s", self.node, _describe(error), exc_info=error)
            self.executor._log(LogLevel.ERROR, f"{self.executor._label(self)}: OnTerminate raised {_describe(error)}")

    def _record_error(self, error: BaseException, where: str) -> None:
        self.error = f"{where} raised {_describe(error)}"
        self.feedback_message = self.error
        try:
            details = "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()
        except Exception:  # noqa: BLE001
            details = ""
        log.error("%s: %s\n%s", self.node, self.error, details)
        self.executor._log(
            LogLevel.ERROR, f"{self.executor._label(self)}: {self.error}" + (f"\n{details}" if details else "")
        )


class FailingBehaviour(py_trees.behaviour.Behaviour):
    """Stands in for a node that cannot run (e.g. a Negation without a child): fails with ``error``."""

    def __init__(self, name: str, error: str):
        super().__init__(name=name)
        self.error = error

    def update(self) -> Status:
        self.feedback_message = self.error
        return Status.FAILURE


class SubTreeBehaviour(py_trees.decorators.Decorator):
    """Runs the tree of a Sub-Tree node: runs, succeeds and fails as that tree's Root does.

    When the sub-tree fails because one of its nodes failed with an error, ``error`` names
    that node and its error.
    """

    def __init__(self, name: str, child: py_trees.behaviour.Behaviour, leaves: list):
        super().__init__(name=name, child=child)
        self.leaves = leaves  # the sub-tree's leaf behaviours (for error messages)
        self.error: str | None = None

    def initialise(self) -> None:
        self.error = None
        self.feedback_message = ""

    def update(self) -> Status:
        status = self.decorated.status
        if status == Status.INVALID:  # not expected after a tick; never report "not run" as a result
            status = Status.FAILURE
        self.error = self._failure_error() if status == Status.FAILURE else None
        self.feedback_message = self.error or ""
        return status

    def _failure_error(self) -> str | None:
        for behaviour in self.leaves:
            error = getattr(behaviour, "error", None)
            if error and behaviour.status == Status.FAILURE:
                return f"{behaviour.name}: {error}"
        return None


class _ExecutionLog(py_trees.visitors.VisitorBase):
    """Writes the execution of every node to the tree's log (INFO): a node that starts running,
    finishes (Succeeded / Failed), executes within one tick, or is interrupted by its parent."""

    RESULTS = {Status.SUCCESS: "Succeeded", Status.FAILURE: "Failed", Status.RUNNING: "Running"}

    def __init__(self, executor: "TreeExecutor", labels: dict):
        super().__init__(full=False)
        self._executor_ref = weakref.ref(executor)
        self.labels = labels  # {behaviour: "'Title' (Type)"}
        self._before: dict | None = None  # status of every behaviour before the tick
        self._visited: set = set()

    def initialise(self) -> None:
        executor = self._executor_ref()
        if executor is None or not executor._log_enabled(LogLevel.INFO):
            self._before = None
            return
        self._before = {behaviour: behaviour.status for behaviour in self.labels}
        self._visited = set()

    def run(self, behaviour: py_trees.behaviour.Behaviour) -> None:
        if self._before is None or behaviour not in self.labels:
            return
        self._visited.add(behaviour)
        before, after = self._before.get(behaviour), behaviour.status
        result = self.RESULTS.get(after)
        if result is None:
            return
        if before != Status.RUNNING:
            what = "started: Running" if after == Status.RUNNING else f"executed: {result}"
        elif after != Status.RUNNING:
            what = f"finished: {result}"
        else:
            return  # still running
        error = getattr(behaviour, "error", None) if after == Status.FAILURE else None
        if error:
            what += f" ({error})"
        self._write(f"Node {self.labels[behaviour]} {what}")

    def finalise(self) -> None:
        if self._before is None:
            return
        for behaviour, before in self._before.items():
            if before == Status.RUNNING and behaviour not in self._visited and behaviour.status == Status.INVALID:
                self._write(f"Node {self.labels[behaviour]} interrupted")
        self._before = None
        self._visited = set()

    def _write(self, message: str) -> None:
        executor = self._executor_ref()
        if executor is not None:
            executor._log(LogLevel.INFO, message)


#: Hidden sub-tree nodes whose widget was destroyed while their OnRun call still ran (kept, see
#: TreeExecutor._on_subtree_call_done).
_orphaned_nodes: list = []


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
    """

    stateChanged = Signal(str)  # "Idle", "Running", "Paused"
    finished = Signal(str)  # "Succeeded" / "Failed" when a run-once execution completes
    ticked = Signal(int)  # tick count since execution started
    nodeStatusChanged = Signal(object, str)  # node, "Ready"/"Running"/"Succeeded"/"Failed"
    nodeError = Signal(object, str)  # node, error message
    _commandRequested = Signal(str, int)  # (command, generation) from worker threads, run on the GUI thread
    _subtreeNodeIdle = Signal(object)  # a released sub-tree node's OnRun call returned (from its worker thread)

    COMMANDS = ("start", "pause", "stop", "reset")

    def __init__(self, owner: "BehaviorTreeWidget"):
        super().__init__(owner)
        self._owner_ref = weakref.ref(owner)  # no reference cycle with the owning widget
        self._gui_thread = threading.get_ident()
        self._state = ExecutionState.IDLE
        self._tree: py_trees.trees.BehaviourTree | None = None
        self._behaviours: dict[NodeWidget, py_trees.behaviour.Behaviour] = {}
        self._labels: dict[py_trees.behaviour.Behaviour, str] = {}  # how the log names each node
        self._subtree_nodes: list[NodeWidget] = []  # hidden nodes created for Sub-Tree nodes
        # OnRun calls of released sub-tree nodes still running, by the place of their node (the ids
        # of the Sub-Tree nodes leading to it and its own id): the node built for the same place
        # by the next Execute waits for that call, as a node of the view waits for its own.
        self._subtree_calls: dict[tuple[str, ...], Future] = {}
        self._tick_count = 0
        self._ticking = False
        self._aborting = False
        self._busy = 0
        self._pending: list = []  # command names or callables
        self._shut_down = False
        self._blackboard_snapshot = None
        # Incremented whenever a run starts, is stopped or reset (or the tree replaced): a
        # command queued from a worker thread is dropped if the generation changed meanwhile.
        self._generation = 0
        self._token_owner = object()  # marks the run tokens of this executor
        self._epilogue = 0  # > 0 while signals are emitted after a tick
        self._epilogue_levels: list[int] = []  # event-loop level of each of those post-tick sections
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
        owner = self._owner_ref()
        if owner is None:
            raise RuntimeError("the BehaviorTreeWidget has been deleted")
        return owner

    # ------------------------------------------------------------------ log
    def _log(self, level: LogLevel, message: str) -> None:
        owner = self._owner_ref()
        if owner is not None:
            try:
                owner._log(level, message)
            except RuntimeError:  # the widget is being destroyed
                pass

    def _log_enabled(self, level: LogLevel) -> bool:
        owner = self._owner_ref()
        try:
            return owner is not None and owner._tree_log.enabled(level)
        except RuntimeError:
            return False

    def _label(self, behaviour: py_trees.behaviour.Behaviour) -> str:
        """How the log names the node of ``behaviour``."""
        label = self._labels.get(behaviour)
        return label if label is not None else repr(behaviour.name)

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
        return self._ticking or self._busy > 0 or self._first_tick.isActive()

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
        if self.is_busy() or self._pending:
            self._pending.append(function)  # never before items queued earlier
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
            self._pending.append("stop")
            return
        try:
            self._run_command("stop")
        except RuntimeError:  # the widget is being destroyed
            log.debug("shutdown while the widget is being destroyed", exc_info=True)
            return
        self._drain()

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
            self._pending.append(name)
            return
        if name in ("stop", "pause") and not self._in_post_tick_handler() and not self._busy and self._first_tick.isActive():
            # Issued from the event loop (e.g. the Stop button while runs are chained from
            # executionFinished): takes effect before the pending first tick.
            self._first_tick.stop()
        if self.is_busy() or self._pending:
            # Carried out after the current command or the deferred first tick, and never
            # before items queued earlier.
            self._pending.append(name)
            self._drain()
            return
        self._run_command(name)
        self._drain()

    def _in_post_tick_handler(self) -> bool:
        """True in code run by the signals emitted after a tick, but not in an event loop
        that code opened (e.g. a modal dialog): events delivered there come from elsewhere."""
        return bool(self._epilogue_levels) and QThread.currentThread().loopLevel() <= self._epilogue_levels[-1]

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
            if self._epilogue:
                self._first_tick.start()
            else:
                self._tick()

    def _run_command(self, name: str) -> None:
        queued_before = len(self._pending)
        self._busy += 1
        try:
            tick_now = getattr(self, f"_do_{name}")()
        finally:
            self._busy -= 1
        # A command issued while this one was carried out (e.g. Stop from a
        # stateChanged('Running') slot) takes effect before any node code runs; the first
        # tick follows it if the run is still going.
        issued_meanwhile = len(self._pending) > queued_before
        if not tick_now:
            return
        if issued_meanwhile:
            self._pending.append(functools.partial(self._first_tick_if_fresh, self._tree))
        elif self._epilogue:
            self._first_tick.start()  # started from a post-tick signal handler
        else:
            self._tick()

    def _drain(self) -> None:
        """Carry out commands queued while a tick or command was in progress."""
        # While a deferred first tick is pending, the rest of the queue waits for it so
        # queued commands keep their order (the first tick drains them).
        while self._pending and not self.is_busy():
            item = self._pending.pop(0)
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
        if self._state is ExecutionState.IDLE:
            self._build()
            self._generation += 1
            self._tick_count = 0
            self._set_all_ready()
            config = self._owner._config
            self._blackboard_snapshot = self._owner._blackboard.snapshot() if config.restore_blackboard else None
            self._begin_loop()
            self._log(LogLevel.INFO, "Execution started")
        else:
            self._log(LogLevel.INFO, "Execution resumed")
        self.set_interval(self._owner._config.tick_interval_ms)
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
        self._tree = None
        self._behaviours = {}
        self._labels = {}
        self._release_subtree_nodes()
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
            self._tree = None
            self._behaviours = {}
            self._labels = {}
            self._release_subtree_nodes()
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
        owner = self._owner_ref()
        if owner is not None and not owner._blackboard.is_disposed():
            owner._blackboard.start_loop_capture()

    def _end_loop(self) -> None:
        """The execution loop in progress ends: entries with the Reset flag get their loop-start value back."""
        owner = self._owner_ref()
        if owner is None:
            return
        store = owner._blackboard
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
            store = self._owner._blackboard
            store.set_runtime_active(state is not ExecutionState.IDLE)
            self.stateChanged.emit(state.value)

    def _build(self) -> None:
        owner = self._owner
        root = owner._main_root()  # the tree itself, also while a sub-tree is shown
        if root is None:
            raise RuntimeError("the tree has no root node")
        self._release_subtree_nodes()
        behaviours: dict[NodeWidget, py_trees.behaviour.Behaviour] = {}
        labels: dict[py_trees.behaviour.Behaviour, str] = {}
        subtree_nodes: list[NodeWidget] = []

        def build(
            node: NodeWidget,
            chain: tuple[str, ...],
            leaves: list | None,
            scope: tuple[str, ...] = (),
            titles: tuple[str, ...] = (),
        ) -> py_trees.behaviour.Behaviour:
            """``chain``: the sub-tree files ``node`` lies in; ``leaves`` collects a sub-tree's leaves;
            ``scope``: the ids of the Sub-Tree nodes leading to ``node``; ``titles``: their titles."""
            if isinstance(node, CompositeNodeWidget):
                composite = (
                    py_trees.composites.Sequence
                    if node.GetCompositeType() == SEQUENCE
                    else py_trees.composites.Selector
                )
                behaviour = composite(
                    name=node.GetTitle(),
                    memory=node.GetMemory(),
                    children=[build(child, chain, leaves, scope, titles) for child in node.GetChildren()],
                )
            elif isinstance(node, NegationNodeWidget):
                child = node.GetChild()
                if child is not None:
                    behaviour = py_trees.decorators.Inverter(
                        name=node.GetTitle(), child=build(child, chain, leaves, scope, titles)
                    )
                else:
                    message = "a Negation needs a child: connect the node whose result it inverts"
                    log.warning("%s: %s", node, message)
                    behaviour = FailingBehaviour(node.GetTitle(), message)
            elif isinstance(node, SubTreeNodeWidget):
                behaviour = build_subtree(node, chain, scope, titles)
            elif isinstance(node, LeafNodeWidget):
                behaviour = LeafBehaviour(node, self)
            else:
                raise TypeError(f"unsupported node type {type(node).__name__}")
            behaviours[node] = behaviour
            labels[behaviour] = "".join(f"'{title}' > " for title in titles) + f"'{node.GetTitle()}' ({node.GetTypeName()})"
            if isinstance(behaviour, FailingBehaviour):
                self._log(LogLevel.WARNING, f"{labels[behaviour]}: {behaviour.error}")
            if leaves is not None and isinstance(behaviour, (LeafBehaviour, FailingBehaviour, SubTreeBehaviour)):
                leaves.append(behaviour)
            return behaviour

        def build_subtree(
            node: SubTreeNodeWidget, chain: tuple[str, ...], scope: tuple[str, ...], titles: tuple[str, ...]
        ) -> py_trees.behaviour.Behaviour:
            try:
                path = node.GetResolvedFile()
                if path is None:
                    raise TreeFileError("no sub-tree file is chosen (File)")
                key = file_key(path)
                if key in chain:
                    raise TreeFileError(
                        f"'{os.path.basename(path)}' contains this Sub-Tree node itself "
                        "(directly or through other sub-trees), which would never end"
                    )
                sub_root, nodes = owner._load_subtree(path)
            except (TreeFileError, OSError) as error:
                message = f"the sub-tree cannot be run: {error}"
                log.warning("%s: %s", node, message)
                return FailingBehaviour(node.GetTitle(), message)
            subtree_nodes.extend(nodes)
            folder = os.path.dirname(path)
            inner_scope = (*scope, node.GetId())
            for inner in nodes:
                if isinstance(inner, SubTreeNodeWidget):
                    inner._base_dir = folder  # their file names are relative to this sub-tree's file
                place = (*inner_scope, inner.GetId())
                inner.__dict__["_subtree_place"] = place
                call = self._subtree_calls.get(place)
                if call is not None and not call.done():
                    inner._worker_future = call  # an interrupted call of the previous run: wait for it
            leaves: list = []
            inner = build(sub_root, (*chain, key), leaves, inner_scope, (*titles, node.GetTitle()))
            return SubTreeBehaviour(node.GetTitle(), inner, leaves)

        main_path = owner.GetFilePath()
        try:
            tree = py_trees.trees.BehaviourTree(root=build(root, (file_key(main_path),) if main_path else (), None))
        except BaseException:
            self._subtree_nodes = subtree_nodes
            self._release_subtree_nodes()
            raise
        tree.add_visitor(_ExecutionLog(self, labels))
        self._tree = tree
        self._behaviours = behaviours
        self._labels = labels
        self._subtree_nodes = subtree_nodes

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

    def _tick(self) -> None:
        if self.is_busy() or self._tree is None or self._state is not ExecutionState.RUNNING:
            return
        self._ticking = True
        finished: Status | None = None
        error: BaseException | None = None
        did_tick = False
        try:
            tree = self._tree
            config = self._owner._config
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
                    finished = root.status
            if finished is None:
                tree.tick()
                did_tick = True
                self._tick_count += 1
                if not self._aborting:
                    self._refresh_statuses()
                    if root.status in (Status.SUCCESS, Status.FAILURE):
                        self._end_loop()  # the Root returned: this loop is complete
                        if not config.repeat:
                            finished = root.status
        except Exception as caught:  # noqa: BLE001
            error = caught
        finally:
            self._ticking = False
            aborted, self._aborting = self._aborting, False

        self._epilogue += 1
        self._epilogue_levels.append(QThread.currentThread().loopLevel())
        try:
            if error is not None:
                log.error("behavior tree tick failed: %s", _describe(error), exc_info=error)
                self._log(LogLevel.ERROR, f"The tick failed: {_describe(error)}")
                self._busy += 1
                try:
                    self._do_stop()
                    root_node = self._owner._main_root()
                    if root_node is not None:
                        self._apply_status(root_node, NodeStatus.FAILED, f"Tick failed: {_describe(error)}")
                finally:
                    self._busy -= 1
                self.finished.emit(NodeStatus.FAILED.value)
                self._drain()
                return
            if aborted:
                self._drain()  # the deferred Stop / Reset
                return
            if finished is not None:
                self._busy += 1
                try:
                    self._timer.stop()
                    self._tree = None
                    self._behaviours = {}
                    self._labels = {}
                    self._release_subtree_nodes()
                    self._log(LogLevel.INFO, f"Execution finished: {NodeStatus.from_py_trees(finished).value}")
                    self._set_state(ExecutionState.IDLE)
                finally:
                    self._busy -= 1
            if did_tick:
                self.ticked.emit(self._tick_count)
            if finished is not None:
                self.finished.emit(NodeStatus.from_py_trees(finished).value)
            self._drain()
        finally:
            self._epilogue_levels.pop()
            self._epilogue -= 1

    def _refresh_statuses(self) -> None:
        """Show each node's latest state.

        py_trees sets finished children back to INVALID in some situations (e.g. a
        Selector with memory invalidates the children that failed before its running
        child). Such nodes keep showing their last result (Succeeded / Failed) until
        the tree is reset; a node interrupted while running goes back to Ready.

        The hidden nodes running a Sub-Tree node's tree show their status on the nodes of that
        sub-tree when it is shown (zoomed into from that Sub-Tree node).
        """
        owner = self._owner
        twins = owner._shown_twins() if owner._levels else {}
        for node, behaviour in list(self._behaviours.items()):
            if not shiboken6.isValid(node):
                continue
            if node._canvas is None:
                place = node.__dict__.get("_subtree_place")
                node = twins.get((place[:-1], place[-1])) if place and twins else None
                if node is None or not shiboken6.isValid(node):
                    continue  # removed from the view, or a hidden node whose sub-tree is not shown
            if behaviour.status == Status.INVALID:
                if node.GetStatus() is NodeStatus.RUNNING:
                    self._apply_status(node, NodeStatus.READY)
                continue
            status = NodeStatus.from_py_trees(behaviour.status)
            error = getattr(behaviour, "error", None) if status is NodeStatus.FAILED else None
            self._apply_status(node, status, error)

    def _apply_status(self, node: NodeWidget, status: NodeStatus, error: str | None = None) -> None:
        previous_status, previous_error = node.GetStatus(), node.GetError()
        node._set_status(status, error)
        if status is not previous_status:
            self.nodeStatusChanged.emit(node, status.value)
        if error and error != previous_error:
            self.nodeError.emit(node, error)

    def _set_all_ready(self) -> None:
        for node in self._owner._all_nodes():  # also the nodes of the sub-trees shown
            if shiboken6.isValid(node):
                self._apply_status(node, NodeStatus.READY)

    def _restore_blackboard(self) -> None:
        if self._blackboard_snapshot is None:
            return
        if not self._owner._config.restore_blackboard:
            self._blackboard_snapshot = None  # the option was switched off meanwhile
            return
        store = self._owner._blackboard
        # Undoing run-time values is itself a run-time change (not an edit of the tree).
        store.set_runtime_active(True)
        try:
            store.restore(self._blackboard_snapshot)
        finally:
            store.set_runtime_active(self._state is not ExecutionState.IDLE)
