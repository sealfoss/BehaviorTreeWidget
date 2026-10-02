"""Cancellation tokens of running leaf nodes.

Every run of a leaf node (from the moment it starts until it finishes or is
interrupted) owns a :class:`RunToken`. Worker threads executing ``OnRun`` have
their run's token bound to the thread, so

* ``NodeWidget.CancelRequested()`` reports the cancellation of *that* call even if
  the node has been restarted in the meantime, and
* a cancelled call can no longer change the tree: blackboard writes and node
  setters made from it raise :class:`ExecutionCancelled`.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class ExecutionCancelled(RuntimeError):
    """Raised when an ``OnRun`` call that was cancelled (Stop, Reset, preemption,
    loading another tree) tries to modify the blackboard or its node."""


class RunToken:
    """Cancellation flag of one run of a leaf node."""

    __slots__ = ("_event", "generation", "owner")

    def __init__(self, generation: int = -1, owner: object = None) -> None:
        self._event = threading.Event()
        self.generation = generation  # executor generation of the run (never changes)
        self.owner = owner  # identifies the executor running it (never changes)

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


_local = threading.local()


def current_token() -> RunToken | None:
    """The token bound to the calling (worker) thread, if any."""
    return getattr(_local, "token", None)


@contextmanager
def bound(token: RunToken) -> Iterator[None]:
    """Bind ``token`` to the calling thread for the duration of the block."""
    previous = current_token()
    _local.token = token
    try:
        yield
    finally:
        _local.token = previous


def check_not_cancelled(action: str) -> None:
    """Raise :class:`ExecutionCancelled` if the calling thread runs a cancelled OnRun call."""
    token = current_token()
    if token is not None and token.is_cancelled():
        raise ExecutionCancelled(f"{action} refused: this OnRun call was cancelled (Stop, Reset or preemption)")
