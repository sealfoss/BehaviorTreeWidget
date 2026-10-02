"""Execution of the graphical tree with py_trees.

When execution starts, the node graph is translated into a py_trees tree:

* Root / Sequence / Selector nodes -> ``py_trees.composites.Sequence`` /
  ``py_trees.composites.Selector`` (children ordered left to right),
* leaf nodes -> :class:`LeafBehaviour`, an adapter calling ``LeafNodeWidget.OnRun``.

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

from ._runctx import RunToken, bound, check_not_cancelled, current_token
from .nodes import SEQUENCE, CompositeNodeWidget, LeafNodeWidget, NodeStatus, NodeWidget

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

    def _record_error(self, error: BaseException, where: str) -> None:
        self.error = f"{where} raised {_describe(error)}"
        self.feedback_message = self.error
        try:
            details = "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()
        except Exception:  # noqa: BLE001
            details = ""
        log.error("%s: %s\n%s", self.node, self.error, details)


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

    COMMANDS = ("start", "pause", "stop", "reset")

    def __init__(self, owner: "BehaviorTreeWidget"):
        super().__init__(owner)
        self._owner_ref = weakref.ref(owner)  # no reference cycle with the owning widget
        self._gui_thread = threading.get_ident()
        self._state = ExecutionState.IDLE
        self._tree: py_trees.trees.BehaviourTree | None = None
        self._behaviours: dict[NodeWidget, py_trees.behaviour.Behaviour] = {}
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

    @property
    def _owner(self) -> "BehaviorTreeWidget":
        owner = self._owner_ref()
        if owner is None:
            raise RuntimeError("the BehaviorTreeWidget has been deleted")
        return owner

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
        self.set_interval(self._owner._config.tick_interval_ms)
        self._set_state(ExecutionState.RUNNING)
        if self._shut_down or self._state is not ExecutionState.RUNNING:
            return False
        self._timer.start()
        return True

    def _do_pause(self) -> bool:
        if self._state is ExecutionState.RUNNING:
            self._timer.stop()
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
        self._set_all_ready()
        if was_active:
            self._restore_blackboard()
        self._blackboard_snapshot = None
        self._set_state(ExecutionState.IDLE)
        self._timer.stop()
        return False

    def _do_reset(self) -> bool:
        self._generation += 1
        self._halt_tree()
        idle = self._state is ExecutionState.IDLE
        if idle:
            self._tree = None
            self._behaviours = {}
        self._set_all_ready()
        self._restore_blackboard()
        if idle:
            self._blackboard_snapshot = None
        self._tick_count = 0
        return False

    def discard_snapshot(self) -> None:
        """Forget the blackboard snapshot (e.g. the tree is being replaced)."""
        self._blackboard_snapshot = None

    # ------------------------------------------------------------------ internals
    def _set_state(self, state: ExecutionState) -> None:
        if state is not self._state:
            self._state = state
            store = self._owner._blackboard
            store.set_runtime_active(state is not ExecutionState.IDLE)
            self.stateChanged.emit(state.value)

    def _build(self) -> None:
        root = self._owner.GetRootNode()
        if root is None:
            raise RuntimeError("the tree has no root node")
        behaviours: dict[NodeWidget, py_trees.behaviour.Behaviour] = {}

        def build(node: NodeWidget) -> py_trees.behaviour.Behaviour:
            if isinstance(node, CompositeNodeWidget):
                composite = (
                    py_trees.composites.Sequence
                    if node.GetCompositeType() == SEQUENCE
                    else py_trees.composites.Selector
                )
                behaviour = composite(
                    name=node.GetTitle(),
                    memory=node.GetMemory(),
                    children=[build(child) for child in node.GetChildren()],
                )
            elif isinstance(node, LeafNodeWidget):
                behaviour = LeafBehaviour(node, self)
            else:
                raise TypeError(f"unsupported node type {type(node).__name__}")
            behaviours[node] = behaviour
            return behaviour

        self._tree = py_trees.trees.BehaviourTree(root=build(root))
        self._behaviours = behaviours

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
                else:
                    finished = root.status
            if finished is None:
                tree.tick()
                did_tick = True
                self._tick_count += 1
                if not self._aborting:
                    self._refresh_statuses()
                    if root.status in (Status.SUCCESS, Status.FAILURE) and not config.repeat:
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
                self._busy += 1
                try:
                    self._do_stop()
                    root_node = self._owner.GetRootNode()
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
        """
        for node, behaviour in list(self._behaviours.items()):
            if node._canvas is None or not shiboken6.isValid(node):
                continue  # removed from the view
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
        for node in self._owner.GetNodes():
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
