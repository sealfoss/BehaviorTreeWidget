"""Threads running the ``OnRun`` calls of worker-thread leaf nodes (internal).

Every thread is a daemon thread: an ``OnRun`` call that never returns can neither block
application exit nor later executions.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable

__all__ = ["WorkerPool", "leaf_pool", "run_in_daemon_thread"]


def _run(future: Future, function: Callable[[], Any]) -> None:
    """Run ``function()`` and report its result or exception through ``future``."""
    if not future.set_running_or_notify_cancel():
        return
    try:
        result = function()
    except BaseException as error:  # noqa: BLE001 - reported through the future
        future.set_exception(error)
    else:
        future.set_result(result)


def run_in_daemon_thread(function: Callable[[], Any], name: str) -> Future:
    """Run ``function()`` on a new daemon thread named ``name`` and return a Future for its result."""
    future: Future = Future()
    threading.Thread(target=_run, args=(future, function), name=name, daemon=True).start()
    return future


class WorkerPool:
    """Daemon threads that run calls and are reused from one call to the next.

    :meth:`submit` hands a call to an idle worker or, when every worker is busy, starts a new
    one: a call never waits for another, so an ``OnRun`` call that never returns occupies one
    worker, as it occupied one thread before. A worker that stays idle for ``idle_timeout``
    seconds ends. While it runs a call, a worker carries the call's name (for example
    ``behavior_tree_widget:Move To``).
    """

    IDLE_NAME = "behavior_tree_widget:idle"

    def __init__(self, idle_timeout: float = 60.0) -> None:
        self.idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._tasks: queue.SimpleQueue = queue.SimpleQueue()
        self._idle = 0  # waiting workers that no queued call was promised to
        self._workers = 0  # workers alive
        self._started = 0  # workers started so far

    def submit(self, function: Callable[[], Any], name: str) -> Future:
        """Run ``function()`` on a worker named ``name``; returns a Future for its result.

        Raises:
            RuntimeError: a new worker was needed and could not be started.
        """
        future: Future = Future()
        task = (future, function, name)
        with self._lock:
            reuse = self._idle > 0
            if reuse:
                self._idle -= 1  # promised to this call: a worker waiting now takes it
            else:
                self._workers += 1
                self._started += 1
        if reuse:
            self._tasks.put(task)
            return future
        try:
            # The new worker runs this call first, so a worker that cannot start leaves nothing queued.
            threading.Thread(target=self._work, args=(task,), name=name, daemon=True).start()
        except BaseException:
            with self._lock:
                self._workers -= 1
                self._started -= 1
            raise
        return future

    def worker_count(self) -> int:
        """Workers alive (running a call or waiting for one)."""
        with self._lock:
            return self._workers

    def idle_count(self) -> int:
        """Workers waiting for a call."""
        with self._lock:
            return self._idle

    def started_count(self) -> int:
        """Workers started since the pool was created."""
        with self._lock:
            return self._started

    def _work(self, task: tuple | None) -> None:
        thread = threading.current_thread()
        while task is not None:
            future, function, name = task
            task = None
            thread.name = name
            _run(future, function)
            future = function = None  # nothing of a finished call (or its node) is kept while idle
            thread.name = self.IDLE_NAME
            task = self._next_task()

    def _next_task(self) -> tuple | None:
        """Wait for the next call; None when this worker ends (idle for ``idle_timeout`` seconds)."""
        with self._lock:
            self._idle += 1
        while True:
            try:
                return self._tasks.get(timeout=self.idle_timeout)
            except queue.Empty:
                with self._lock:
                    if self._idle > 0:  # a worker no call was promised to: this one ends
                        self._idle -= 1
                        self._workers -= 1
                        return None
                # Every waiting worker has a call promised to it, this one included: it is coming.


_leaf_pool: WorkerPool | None = None
_leaf_pool_lock = threading.Lock()


def leaf_pool() -> WorkerPool:
    """The pool running the ``OnRun`` calls of worker-thread leaf nodes (created when first needed)."""
    global _leaf_pool
    with _leaf_pool_lock:
        if _leaf_pool is None:
            _leaf_pool = WorkerPool()
        return _leaf_pool
