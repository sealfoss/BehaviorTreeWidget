"""The py_trees behaviours that execute the nodes of a tree (see :mod:`.execution`).

* :class:`LeafBehaviour` runs a :class:`~behavior_tree_widget.LeafNodeWidget`'s ``OnStart`` /
  ``OnRun`` / ``OnTerminate``, ``OnRun`` on a worker thread when the node's ``RUN_IN_THREAD``
  is True.
* :class:`FailingBehaviour` stands in for a node that cannot run (e.g. a Negation without
  child) and fails with the reason.
* :class:`SubTreeBehaviour` runs the tree of a Sub-Tree node and succeeds or fails with it.
* :class:`TickVisitor` follows every tick: which behaviours ticked and which were running
  before it, and writes the execution of every node to the tree's log at the INFO level.

The behaviours talk to the executor running them only through the methods listed in
:class:`LeafExecutor`.
"""

from __future__ import annotations

import functools
import inspect
import traceback
import weakref
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Callable, Iterable, Protocol

import py_trees
from py_trees.common import Status

from ._runctx import RunToken, bound
from .nodes import LeafNodeWidget, NodeStatus
from .tree_log import LogLevel

if TYPE_CHECKING:  # pragma: no cover
    from .widget import BehaviorTreeWidget

__all__ = [
    "FailingBehaviour",
    "LeafBehaviour",
    "LeafExecutor",
    "SubTreeBehaviour",
    "TickVisitor",
    "call_on_run",
    "describe_error",
    "result_to_status",
]


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


def describe_error(error: BaseException) -> str:
    """``"ValueError: boom"``: the type and text of an exception (never raises)."""
    try:
        text = str(error)
    except Exception:  # noqa: BLE001 - an exception whose __str__ fails
        text = "<unprintable error>"
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


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


class LeafExecutor(Protocol):
    """What a :class:`LeafBehaviour` needs from the executor running it (``TreeExecutor``)."""

    token_owner: object  # marks the run tokens of this executor

    def tree_widget(self) -> "BehaviorTreeWidget":
        """The ``tree`` passed to ``OnStart`` / ``OnRun`` / ``OnTerminate``."""

    def generation(self) -> int:
        """Counts the runs started, stopped and reset: a run token belongs to one generation."""

    def is_aborting(self) -> bool:
        """True while a Stop / Reset issued during the tick is pending: no more node code runs."""

    def label(self, behaviour: py_trees.behaviour.Behaviour) -> str:
        """How the log names the node of ``behaviour``."""

    def report(self, level: LogLevel, message: str, exc_info: BaseException | None = None) -> None:
        """Write ``message`` to the module logger and to the tree's log."""

    def log_message(self, level: LogLevel, message: str) -> None:
        """Write ``message`` to the tree's log only (never raises)."""

    def log_enabled(self, level: LogLevel) -> bool:
        """Whether a message of ``level`` would be written to the tree's log now."""

    def start_call(self, function: Callable[[], Any], name: str) -> Future:
        """Run ``function()`` on a worker thread named ``name``."""

    def wait_for_call(self, future: Future, wait: float) -> bool:
        """Wait up to ``wait`` seconds for a call started in this tick; False if it was not waited for."""


class LeafBehaviour(py_trees.behaviour.Behaviour):
    """py_trees behaviour that runs a :class:`LeafNodeWidget`'s ``OnStart``/``OnRun``/``OnTerminate``.

    Lifecycle of one run: ``OnStart`` (GUI thread) -> ``OnRun`` ... ``OnRun`` (until it returns a
    final result) -> ``OnTerminate`` (GUI thread, once). A worker-thread ``OnRun`` call reports
    Running until it returns; an interrupted call's result is ignored, and the node waits for it
    to return before it runs again.
    """

    def __init__(self, node: LeafNodeWidget, executor: LeafExecutor):
        super().__init__(name=node.GetTitle())
        self.node = node
        self.executor = executor
        self.tree = executor.tree_widget()
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
        self.generation = self.executor.generation()
        self.token = RunToken(self.generation, self.executor.token_owner)  # complete before it is published
        self.node._run_token = self.token
        self._active = True

    def update(self) -> Status:
        if self.executor.is_aborting() or (self.token is not None and self.token.is_cancelled()):
            # A Stop/Reset is pending: run no user code. RUNNING keeps py_trees from terminating the
            # node now; the pending halt interrupts it (OnTerminate(READY)).
            return Status.RUNNING
        if self._waiting_for_previous_call():
            return Status.RUNNING
        if not self._started:
            self._start()
            if self.executor.is_aborting():
                return Status.RUNNING  # OnStart issued Stop/Reset
        if self.error is not None:
            return Status.FAILURE
        return self._run_threaded() if self.node.RUN_IN_THREAD else self._run_inline()

    # ------------------------------------------------------------------ the steps of update()
    def _waiting_for_previous_call(self) -> bool:
        """True while an interrupted ``OnRun`` call of this node is still returning (a node never
        runs two calls at once)."""
        previous = self.node._worker_future
        if self._future is None and previous is not None and not previous.done():
            self.feedback_message = "waiting for the previous OnRun call to return"
            return True
        return False

    def _start(self) -> None:
        """Call ``OnStart`` (once per run, before the first ``OnRun``)."""
        self._started = True
        try:
            self.node.OnStart(self.tree)
        except Exception as error:  # noqa: BLE001 - user code
            self._record_error(error, "OnStart")

    def _run_inline(self) -> Status:
        """Call ``OnRun`` on the GUI thread."""
        try:
            result = call_on_run(self.node, self.tree)
        except Exception as error:  # noqa: BLE001 - user code
            self._record_error(error, "OnRun")
            return Status.FAILURE
        return self._result_status(result)

    def _run_threaded(self) -> Status:
        """Start ``OnRun`` on a worker thread, or collect the call started on an earlier tick."""
        if self._future is None:
            if not self._start_call():
                return Status.FAILURE
            wait = max(0.0, float(getattr(self.node, "THREAD_WAIT", 0.0) or 0.0))
            if not self.executor.wait_for_call(self._future, wait):
                return Status.RUNNING  # not waited for: the result is collected on a later tick
        if not self._future.done() or self.executor.is_aborting() or self.token.is_cancelled():
            # Still executing, or interrupted meanwhile: an interrupted call's result is ignored;
            # the pending halt / preemption terminates the node.
            return Status.RUNNING
        future, self._future = self._future, None
        error = future.exception()
        if error is not None:
            self._record_error(error, "OnRun")
            return Status.FAILURE
        return self._result_status(future.result())

    def _start_call(self) -> bool:
        """Start ``OnRun`` on a worker thread; False (error recorded) if that failed."""
        token = self.token
        try:
            self._future = self.executor.start_call(
                functools.partial(self._run_bound, token), f"behavior_tree_widget:{self.node.GetTitle()}"
            )
        except Exception as error:  # noqa: BLE001 - e.g. "can't start new thread"
            self._record_error(error, "OnRun")
            return False
        self.node._worker_future = self._future
        return True

    def _result_status(self, result: Any) -> Status:
        """The status of an ``OnRun`` return value (an invalid one is an error)."""
        status, message = result_to_status(result)
        if message is not None:
            self.error = message
            self.executor.report(LogLevel.ERROR, f"{self.executor.label(self)}: {message}")
        self.feedback_message = self.error or ""
        return status

    def _run_bound(self, token: RunToken) -> Any:
        with bound(token):
            return call_on_run(self.node, self.tree)

    # ------------------------------------------------------------------ end of a run
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
            self.executor.report(
                LogLevel.ERROR, f"{self.executor.label(self)}: OnTerminate raised {describe_error(error)}", exc_info=error
            )

    def _record_error(self, error: BaseException, where: str) -> None:
        self.error = f"{where} raised {describe_error(error)}"
        self.feedback_message = self.error
        try:
            details = "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()
        except Exception:  # noqa: BLE001
            details = ""
        self.executor.report(
            LogLevel.ERROR, f"{self.executor.label(self)}: {self.error}" + (f"\n{details}" if details else "")
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


class TickVisitor(py_trees.visitors.VisitorBase):
    """Follows every tick of the tree it is added to.

    After a tick, :attr:`visited` holds the behaviours that ticked in it (in tick order, which is
    the order the tree was built in), :attr:`running_before` those that were running when it
    started, and :attr:`not_ticked` those of them that did not tick (interrupted by their
    parent). At the INFO log level it writes the execution of every node to the tree's log: a
    node that starts running, finishes (Succeeded / Failed), executes within one tick, or is
    interrupted by its parent.

    Only the behaviours that ticked or were running are looked at, so the cost of a tick
    follows what ran in it, not the size of the tree.
    """

    RESULTS = {Status.SUCCESS: "Succeeded", Status.FAILURE: "Failed", Status.RUNNING: "Running"}

    def __init__(self, executor: Any, labels: dict):
        super().__init__(full=False)
        self._executor_ref = weakref.ref(executor)
        self.labels = labels  # {behaviour: "'Title' (Type)"}, in the order the tree was built
        self._order: dict | None = None  # {behaviour: index in labels}, made when first needed
        self.running: set = set()  # behaviours running after the last tick
        self.running_before: set = set()  # behaviours running when the current / last tick started
        self.visited: dict = {}  # behaviours that ticked in the current / last tick (an ordered set)
        self.not_ticked: list = []  # behaviours of running_before that did not tick, in build order
        self._running_now: set = set()  # visited behaviours that reported RUNNING
        self._logging = False

    def initialise(self) -> None:
        # Statuses may have changed between ticks (Stop, Reset, a new loop): keep what still runs.
        running = self.running
        self.running_before = {behaviour for behaviour in running if behaviour.status is Status.RUNNING}
        self.visited = {}
        self.not_ticked = []
        self._running_now = set()
        executor = self._executor_ref()
        self._logging = executor is not None and executor.log_enabled(LogLevel.INFO)

    def run(self, behaviour: py_trees.behaviour.Behaviour) -> None:
        visited = self.visited
        if behaviour in visited or behaviour not in self.labels:
            return
        visited[behaviour] = None
        after = behaviour.status
        if after is Status.RUNNING:
            self._running_now.add(behaviour)
        if not self._logging:
            return
        result = self.RESULTS.get(after)
        if result is None:
            return
        if behaviour not in self.running_before:
            what = "started: Running" if after is Status.RUNNING else f"executed: {result}"
        elif after is not Status.RUNNING:
            what = f"finished: {result}"
        else:
            return  # still running
        error = getattr(behaviour, "error", None) if after is Status.FAILURE else None
        if error:
            what += f" ({error})"
        self._write(f"Node {self.labels[behaviour]} {what}")

    def finalise(self) -> None:
        visited = self.visited
        not_ticked = [behaviour for behaviour in self.running_before if behaviour not in visited]
        if len(not_ticked) > 1:
            not_ticked = self.in_build_order(not_ticked)
        self.not_ticked = not_ticked
        if self._logging:
            for behaviour in not_ticked:
                if behaviour.status is Status.INVALID:
                    self._write(f"Node {self.labels[behaviour]} interrupted")
        self.running = {
            behaviour for behaviour in (*self._running_now, *not_ticked) if behaviour.status is Status.RUNNING
        }

    def changed(self) -> list:
        """The behaviours whose status may have changed in the last tick, in build order: those
        that ticked, and those that were running (an interrupted one is not ticked)."""
        if not self.not_ticked:
            return list(self.visited)  # ticks visit the behaviours in the order they were built
        return self.in_build_order([*self.visited, *self.not_ticked])

    def in_build_order(self, behaviours: Iterable) -> list:
        order = self._order
        if order is None:
            order = self._order = {behaviour: index for index, behaviour in enumerate(self.labels)}
        return sorted(behaviours, key=lambda behaviour: order.get(behaviour, -1))

    def _write(self, message: str) -> None:
        executor = self._executor_ref()
        if executor is not None:
            executor.log_message(LogLevel.INFO, message)
