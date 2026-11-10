"""BlackboardStore: the thread-safe, typed blackboard of a BehaviorTreeWidget, backed by py_trees."""

from __future__ import annotations

import builtins  # set[...] in this class would name its set() method
import copy
import itertools
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import py_trees
import shiboken6
from PySide6.QtCore import QObject, Signal

from .._runctx import check_not_cancelled
from ..serialization import decode_value, encode_value
from .values import (
    ENTRY_TYPES,
    INVALID_NAME_CHARACTERS,
    _copy_value,
    _equal_values,
    canonical_type,
    coerce_value,
    infer_type,
)

__all__ = ["BlackboardSnapshot", "BlackboardStore"]

log = logging.getLogger("behavior_tree_widget")


class BlackboardSnapshot(list):
    """What :meth:`BlackboardStore.snapshot` returns: a list of ``(name, type, value)`` tuples in
    display order, which also holds the Reset flags and which entry is which.

    With those, :meth:`BlackboardStore.restore` gives entries renamed since the snapshot their
    names back (as renames, which the nodes using them follow) and puts back the Reset flags of
    every entry, also of entries removed since. A plain list of tuples can be restored too.
    """

    def __init__(self, entries=(), resets=(), ids: dict[str, int] | None = None, store: str = "") -> None:
        super().__init__(entries)
        self.resets: frozenset[str] = frozenset(resets)  # names of the entries with the Reset flag
        self.ids: dict[str, int] = dict(ids or {})  # {name: identity of the entry in its store}
        self.store = store  # namespace of the store the snapshot was taken from


class BlackboardStore(QObject):
    """Thread-safe typed blackboard backed by a ``py_trees.blackboard.Client``.

    Signals are emitted from the thread that made the change; connect them to
    methods of QObjects living in the GUI thread so Qt queues them. ``changed``
    carries ``True`` for run-time changes (made from a worker thread or while
    the tree executes), which do not count as edits of the tree file.
    """

    entryAdded = Signal(str)
    entryRemoved = Signal(str)
    entryChanged = Signal(str)
    entryRenamed = Signal(str, str)  # old name, new name
    entryRetyped = Signal(str)
    entriesReordered = Signal()
    entryResetChanged = Signal(str)  # the Reset flag of an entry was switched (see set_reset)
    changed = Signal(bool)  # any change of a value, name, type or the order; True for run-time changes
    batchFinished = Signal()  # the outermost batch() ended (see batch)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._types: dict[str, str] = {}
        self._reset: set[str] = set()  # entries put back to their loop-start value after each loop
        # The identity of each entry: it follows renames, so a snapshot can tell a renamed entry
        # from a removed one (see restore).
        self._ids: dict[str, int] = {}
        self._next_id = itertools.count(1)
        # While an execution loop runs: the value each entry had when the loop started, recorded
        # just before its first change in the loop ({name: (type, value)}, or None for an entry
        # created during the loop, which has no loop-start value); None while no loop runs.
        self._loop_capture: dict[str, tuple[str, Any] | None] | None = None
        self._disposed = False
        self._runtime_active = False
        self._revision = 0  # counts every change (see revision)
        self._saved_revision = 0  # counts the changes that can alter what to_list() returns
        self._batch_depth = 0  # nesting of batch()
        self._gui_thread = threading.get_ident()
        self._namespace = f"/behavior_tree_widget_{uuid.uuid4().hex[:12]}"
        self._client = py_trees.blackboard.Client(name="BehaviorTreeWidget", namespace=self._namespace)

    # ------------------------------------------------------------------ queries
    def namespace(self) -> str:
        """py_trees blackboard namespace holding this store's keys (e.g. ``/behavior_tree_widget_ab12``)."""
        return self._namespace

    def key(self, name: str) -> str:
        """Absolute py_trees blackboard key of entry ``name``."""
        return f"{self._namespace}/{name}"

    def client(self) -> py_trees.blackboard.Client:
        """The ``py_trees.blackboard.Client`` holding this store's keys (public: for code that works
        with py_trees directly, e.g. to inspect the keys it registered)."""
        return self._client

    def names(self) -> list[str]:
        with self._lock:
            return list(self._types)

    def types(self) -> dict[str, str]:
        """``{name: type}`` of every entry, in display order."""
        with self._lock:
            return dict(self._types)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._types

    def type_of(self, name: str) -> str:
        with self._lock:
            try:
                return self._types[name]
            except (KeyError, TypeError):
                raise KeyError(f"no blackboard entry named {name!r}") from None

    def get(self, name: str) -> Any:
        """Value of entry ``name``; containers are returned as copies. Raises KeyError."""
        with self._lock:
            type_name = self.type_of(name)
            return _copy_value(type_name, self._client.get(self.key(name)))

    def items(self) -> list[tuple[str, str, Any]]:
        """``(name, type, value)`` for every entry, in display order."""
        with self._lock:
            return [(name, type_name, self.get(name)) for name, type_name in self._types.items()]

    def is_disposed(self) -> bool:
        return self._disposed

    # ------------------------------------------------------------------ change tracking
    def revision(self, saved: bool = False) -> int:
        """A number that grows with every change of the blackboard (thread-safe).

        With ``saved``, only the changes that can alter what :meth:`to_list` returns (what the
        blackboard file holds) count; changes of Object entries, which are never saved, do not.
        """
        with self._lock:
            return self._saved_revision if saved else self._revision

    def _bump_locked(self, saved: bool) -> None:
        """Count a change (the caller holds the lock); ``saved``: it can alter :meth:`to_list`."""
        self._revision += 1
        if saved:
            self._saved_revision += 1

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Group several changes, e.g. while a whole blackboard is replaced.

        Every change still emits its own signals. Listeners that redraw when entries are added,
        removed, renamed, retyped or reordered can check :meth:`in_batch` and redraw once when
        :attr:`batchFinished` is emitted, at the end of the outermost batch. Batches nest.
        """
        with self._lock:
            self._batch_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._batch_depth -= 1
                finished = self._batch_depth == 0
            if finished and shiboken6.isValid(self):
                self.batchFinished.emit()

    def in_batch(self) -> bool:
        """True while :meth:`batch` is in progress (see there)."""
        with self._lock:
            return self._batch_depth > 0

    # ------------------------------------------------------------------ names
    @staticmethod
    def check_name_format(name: Any) -> str:
        """Check the format of an entry name (not its uniqueness); raise ValueError/TypeError."""
        if not isinstance(name, str):
            raise TypeError(f"entry names must be str, not {type(name).__name__}")
        if not name.strip():
            raise ValueError("Entry names cannot be empty.")
        if name != name.strip():
            raise ValueError("Entry names cannot start or end with spaces.")
        bad = [char for char in INVALID_NAME_CHARACTERS if char in name]
        if bad:
            raise ValueError(f"Entry names cannot contain {' or '.join(repr(c) for c in bad)}.")
        return name

    def validate_name(self, name: Any, *, allow_existing: str | None = None) -> str:
        """Check a new entry name; return it or raise ValueError/TypeError with a message."""
        self.check_name_format(name)
        with self._lock:
            if name in self._types and name != allow_existing:
                raise ValueError(f"An entry named {name!r} already exists.")
        return name

    # ------------------------------------------------------------------ changes
    def set_runtime_active(self, active: bool) -> None:
        """Mark changes made from now on as run-time changes (set while the tree executes)."""
        self._runtime_active = bool(active)

    def _is_runtime(self) -> bool:
        return self._runtime_active or threading.get_ident() != self._gui_thread

    def _check_writable(self, action: str) -> None:
        check_not_cancelled(action)
        if self._disposed:
            raise RuntimeError("the blackboard is no longer available (BehaviorTreeWidget.Shutdown was called)")

    def _notify(self, runtime: bool) -> None:
        self.changed.emit(runtime)

    # ------------------------------------------------------------------ Reset flags
    def is_reset(self, name: str) -> bool:
        """True if entry ``name`` goes back to its loop-start value at the end of each execution loop."""
        with self._lock:
            self.type_of(name)
            return name in self._reset

    def reset_names(self) -> list[str]:
        """Names of the entries whose Reset flag is set, in display order."""
        with self._lock:
            return [name for name in self._types if name in self._reset]

    def set_reset(self, name: str, reset: bool) -> None:
        """Set the Reset flag of entry ``name`` (thread-safe; raises KeyError if it does not exist).

        At the end of every execution loop (the Root returned, or execution was stopped or
        restarted), an entry with the flag gets back the value it had when the loop started.
        """
        reset = bool(reset)
        with self._lock:
            self._check_writable("Changing the Reset flag of an entry")
            self.type_of(name)
            if (name in self._reset) == reset:
                return
            if reset:
                self._reset.add(name)
            else:
                self._reset.discard(name)
            self._bump_locked(self._types[name] != "Object")
        self.entryResetChanged.emit(name)

    # ------------------------------------------------------------------ execution loops
    def start_loop_capture(self) -> None:
        """Start recording the loop-start value of every entry changed from now on (an execution
        loop begins). Each entry's value is recorded just before its first change."""
        with self._lock:
            self._loop_capture = {}

    def finish_loop_capture(self) -> dict[str, tuple[str, Any]]:
        """Stop recording and return ``{name: (type, value)}``: the loop-start value of every
        entry changed since :meth:`start_loop_capture` (empty when no loop was recorded).
        Entries created during the loop are left out: they had no value when it started."""
        with self._lock:
            captured, self._loop_capture = self._loop_capture, None
        return {name: start for name, start in (captured or {}).items() if start is not None}

    def is_capturing(self) -> bool:
        """True while an execution loop records loop-start values, between :meth:`start_loop_capture`
        and :meth:`finish_loop_capture` (public, e.g. for diagnostics and tests)."""
        with self._lock:
            return self._loop_capture is not None

    def _capture_locked(self, name: str) -> None:
        """Record the value of ``name`` before it changes (caller holds the lock)."""
        capture = self._loop_capture
        if capture is None or name in capture or name not in self._types:
            return
        type_name = self._types[name]
        value = self._client.get(self.key(name))
        if type_name != "Object":
            try:
                value = copy.deepcopy(value)
            except Exception:  # noqa: BLE001 - uncopyable objects are kept by reference
                pass
        capture[name] = (type_name, value)

    def reset_entries(self, captured: dict[str, tuple[str, Any]]) -> list[str]:
        """Put every entry with the Reset flag back to its value in ``captured`` (see
        :meth:`finish_loop_capture`). Returns the names of the entries that changed."""
        changed = []
        for name, (type_name, value) in captured.items():
            with self._lock:
                if self._disposed or name not in self._reset or self._types.get(name) is None:
                    continue
                current_type = self._types[name]
                current = self._client.get(self.key(name))
            if current_type == type_name and _equal_values(current, value):
                continue
            if type_name != "Object":
                try:
                    value = copy.deepcopy(value)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.replace(name, type_name, value)
            except (TypeError, ValueError, KeyError, RuntimeError) as error:
                log.warning("could not reset blackboard entry %r: %s", name, error)
                continue
            changed.append(name)
        return changed

    def add(self, name: str, type_name: str, value: Any = None, reset: bool = False) -> None:
        """Create entry ``name`` of ``type_name`` (default value if ``value`` is None).

        ``reset`` sets its Reset flag (see :meth:`set_reset`).
        """
        type_name = canonical_type(type_name)
        if value is None and type_name != "Object":
            value = ENTRY_TYPES[type_name].default
        value = coerce_value(type_name, value)
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("AddEntry")
            self.validate_name(name)
            self._client.register_key(key=name, access=py_trees.common.Access.WRITE)
            self._client.set(self.key(name), value, overwrite=True)
            self._types[name] = type_name
            self._ids[name] = next(self._next_id)
            if self._loop_capture is not None:
                self._loop_capture[name] = None  # created during the loop: it has no loop-start value
            if reset:
                self._reset.add(name)
            else:
                self._reset.discard(name)
            self._bump_locked(type_name != "Object")
        self.entryAdded.emit(name)
        self._notify(runtime)

    def set(self, name: str, value: Any) -> None:
        """Set entry ``name``, creating it (type inferred from ``value``) if needed.

        A new list/dict/set whose items cannot be copied is stored as an Object entry.
        """
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("SetEntry")
            type_name = self._types.get(name)
            if type_name is None:
                inferred = infer_type(value)
                try:
                    self.add(name, inferred, value)
                except TypeError:
                    if not ENTRY_TYPES[inferred].container:
                        raise
                    self.add(name, "Object", value)
                return
            value = coerce_value(type_name, value)
            self._capture_locked(name)
            self._client.set(self.key(name), value, overwrite=True)
            self._bump_locked(type_name != "Object")
        self.entryChanged.emit(name)
        self._notify(runtime)

    def replace(self, name: str, type_name: str, value: Any) -> None:
        """Set entry ``name`` to ``value`` with a (possibly different) type, creating it if needed."""
        type_name = canonical_type(type_name)
        value = coerce_value(type_name, value)
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("SetEntry")
            old_type = self._types.get(name)
            if old_type is None:
                self.add(name, type_name, value)
                return
            self._capture_locked(name)
            self._client.set(self.key(name), value, overwrite=True)
            self._types[name] = type_name
            self._bump_locked(old_type != "Object" or type_name != "Object")
        if old_type == type_name:
            self.entryChanged.emit(name)
        else:
            self.entryRetyped.emit(name)
        self._notify(runtime)

    def remove(self, name: str) -> None:
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("RemoveEntry")
            type_name = self.type_of(name)
            del self._types[name]
            del self._ids[name]
            self._bump_locked(type_name != "Object")
            self._reset.discard(name)
            if self._loop_capture is not None:
                self._loop_capture.pop(name, None)  # a removed entry is not brought back
            self._client.unregister_key(key=name, clear=True)
        self.entryRemoved.emit(name)
        self._notify(runtime)

    def rename(self, old: str, new: str) -> None:
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("Renaming an entry")
            self.type_of(old)
            if new == old:
                return
            self.validate_name(new)
            value = self._client.get(self.key(old))
            self._client.unregister_key(key=old, clear=True)
            self._client.register_key(key=new, access=py_trees.common.Access.WRITE)
            self._client.set(self.key(new), value, overwrite=True)
            # keep the display position of the renamed entry
            self._types = {(new if key == old else key): kind for key, kind in self._types.items()}
            self._ids[new] = self._ids.pop(old)
            if old in self._reset:  # the Reset flag and the loop-start value follow the entry
                self._reset.discard(old)
                self._reset.add(new)
            if self._loop_capture is not None and old in self._loop_capture:
                self._loop_capture[new] = self._loop_capture.pop(old)
            self._bump_locked(self._types[new] != "Object")
        self.entryRenamed.emit(old, new)
        self._notify(runtime)

    def clear(self) -> None:
        with self.batch():
            for name in self.names():
                self.remove(name)

    def move(self, name: str, offset: int) -> int:
        """Move entry ``name`` ``offset`` places down the list (up when negative), stopping at
        either end; returns its new index (thread-safe; KeyError if it does not exist).

        The order is the order of the Blackboard tab and of the blackboard file.
        """
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError(f"offset must be an int, not {type(offset).__name__}")
        runtime = self._is_runtime()
        with self._lock:
            self._check_writable("Moving an entry")
            self.type_of(name)
            names = list(self._types)
            index = names.index(name)
            target = min(max(index + offset, 0), len(names) - 1)
            if target == index:
                return index
            names.insert(target, names.pop(index))
            self._types = {entry: self._types[entry] for entry in names}
            self._bump_locked(self._types[name] != "Object")
        self.entriesReordered.emit()
        self._notify(runtime)  # an edit like a value change (or a run-time change from a running tree)
        return target

    def reorder(self, names: list[str]) -> None:
        """Put the entries in the order of ``names`` (others follow in their current order)."""
        with self._lock:
            ordered = [name for name in names if name in self._types]
            ordered += [name for name in self._types if name not in ordered]
            changed = ordered != list(self._types)
            if changed:
                self._types = {name: self._types[name] for name in ordered}
                self._bump_locked(True)
        if changed:
            self.entriesReordered.emit()

    # ------------------------------------------------------------------ snapshots / persistence
    def snapshot(self) -> BlackboardSnapshot:
        """Copy of all entries and their Reset flags (for restoring later with :meth:`restore`).

        Values are deep copied, except Object entries, which are run-time objects
        (e.g. device handles) and are kept by reference.
        """
        with self._lock:
            result = BlackboardSnapshot(
                resets=self._reset & self._types.keys(), ids=self._ids, store=self._namespace
            )
            for name, type_name in self._types.items():
                value = self._client.get(self.key(name))
                if type_name != "Object":
                    try:
                        value = copy.deepcopy(value)
                    except Exception:  # noqa: BLE001 - uncopyable objects are kept by reference
                        pass
                result.append((name, type_name, value))
            return result

    def restore(self, snapshot: list[tuple[str, str, Any]]) -> None:
        """Make the blackboard contain exactly the entries of ``snapshot`` (in its order).

        Given a :class:`BlackboardSnapshot` of this store, entries renamed since get their old
        names back by being renamed, and every entry gets back its Reset flag. Given a plain
        list, an entry keeps its Reset flag, and one that is created again has none.
        """
        with self.batch(), self._lock:
            if self._disposed:
                return
            wanted = [name for name, _, _ in snapshot]
            if isinstance(snapshot, BlackboardSnapshot) and snapshot.store == self._namespace:
                self._undo_renames_locked(snapshot.ids)
            for name in self.names():
                if name not in wanted:
                    self.remove(name)
            for name, type_name, value in snapshot:
                if type_name != "Object":
                    try:
                        value = copy.deepcopy(value)
                    except Exception:  # noqa: BLE001
                        pass
                self.replace(name, type_name, value)
            self.reorder(wanted)
            if isinstance(snapshot, BlackboardSnapshot):
                for name in wanted:
                    self.set_reset(name, name in snapshot.resets)

    def _undo_renames_locked(self, ids: dict[str, int]) -> None:
        """Remove the entries created since a snapshot with these ``{name: identity}`` and rename
        the entries renamed since back (caller holds the lock)."""
        names = {identity: name for name, identity in ids.items()}
        for name in [name for name in self._types if self._ids[name] not in names]:
            self.remove(name)
        # {current name: name in the snapshot} of the renamed entries
        pending = {name: names[self._ids[name]] for name in self._types if names[self._ids[name]] != name}
        while pending:
            free = [current for current, old in pending.items() if old not in self._types]
            if free:
                current = free[0]
                self.rename(current, pending.pop(current))
            else:  # every old name is taken by another renamed entry (names were swapped)
                current, old = pending.popitem()
                aside = next(f"{current}~{n}" for n in itertools.count(1) if f"{current}~{n}" not in self._types)
                self.rename(current, aside)
                pending[aside] = old

    def to_list(self, problems: list[str] | None = None, quiet: bool = False) -> list[dict]:
        """Entries as JSON-compatible dicts (``name``, ``type``, ``value``, and ``reset: true``
        for entries whose Reset flag is set).

        Object entries (run-time values set from code) are never saved. Other values
        that cannot be written as JSON are skipped; a message is logged and appended to
        ``problems`` when given.

        The values are encoded under the lock, straight from the store: no copies are made first
        (the encoding builds new structures and keeps no reference to the stored values).
        """
        result = []
        notes: list[tuple[str, str]] = []  # ("object" | "problem", name or message), logged after the lock
        with self._lock:
            for name, type_name in self._types.items():
                if type_name == "Object":
                    notes.append(("object", name))
                    continue
                try:
                    encoded = encode_value(self._client.get(self.key(name)))
                except TypeError as error:
                    notes.append(("problem", f"blackboard entry {name!r} was not saved: {error}"))
                    continue
                item = {"name": name, "type": type_name, "value": encoded}
                if name in self._reset:
                    item["reset"] = True
                result.append(item)
        for kind, detail in notes:
            if kind == "object":
                log.debug("blackboard entry %r is an Object entry (a run-time value) and is not saved", detail)
                continue
            if not quiet:
                log.warning(detail)
            if problems is not None:
                problems.append(detail)
        return result

    @staticmethod
    def parse_list(items: Any) -> tuple[list[tuple[str, str, Any]], list[str]]:
        """Validate :meth:`to_list` data; returns ``(entries, problems)`` without changing anything.

        Public: checks the ``blackboard`` list of a file before using it (:meth:`parse_items` also
        returns the entries whose Reset flag is set).
        """
        entries, _, problems = BlackboardStore.parse_items(items)
        return entries, problems

    @staticmethod
    def parse_items(items: Any) -> tuple[list[tuple[str, str, Any]], builtins.set[str], list[str]]:
        """Validate :meth:`to_list` data without changing anything.

        Returns ``(entries, reset, problems)``: ``(name, type, value)`` of the valid entries,
        the names of those whose Reset flag is set, and messages about the invalid ones.
        """
        entries: list[tuple[str, str, Any]] = []
        reset: set[str] = set()
        problems: list[str] = []
        if not isinstance(items, list):
            return entries, reset, ["the blackboard section is not a list"]
        seen: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"blackboard item {index} is not an object")
                continue
            name = item.get("name")
            try:
                if not isinstance(name, str):
                    raise TypeError("the entry name is missing")
                BlackboardStore.check_name_format(name)
                if name in seen:
                    raise ValueError("duplicate entry name")
                type_name = canonical_type(item.get("type"))
                if type_name == "Object":
                    raise ValueError("Object entries hold run-time values and are not loaded from files")
                value = coerce_value(type_name, decode_value(item.get("value")))
            except (TypeError, ValueError, KeyError) as error:
                problems.append(f"blackboard entry {name!r}: {error}")
                continue
            seen.add(name)
            entries.append((name, type_name, value))
            flag = item.get("reset", False)
            if flag is True:
                reset.add(name)
            elif flag is not False:
                problems.append(f"blackboard entry {name!r}: 'reset' must be true or false, not {flag!r}; it is off")
        return entries, reset, problems

    def load_list(self, items: Any, replace: bool = False) -> list[str]:
        """Add or overwrite entries from :meth:`to_list` data. Returns problems found.

        With ``replace=True`` every entry that is not in ``items`` is removed first,
        except Object entries (run-time objects set from code, which are never saved).
        Public: the inverse of :meth:`to_list`, for code that keeps blackboards of its own.
        """
        entries, reset, problems = self.parse_items(items)
        with self.batch():
            if replace:
                wanted = {name for name, _, _ in entries}
                for name in self.names():
                    if name not in wanted and self.type_of(name) != "Object":
                        self.remove(name)
            for name, type_name, value in entries:
                try:
                    if self.has(name):
                        self.replace(name, type_name, value)
                    else:
                        self.add(name, type_name, value)
                    self.set_reset(name, name in reset)
                except (TypeError, ValueError, KeyError) as error:
                    problems.append(f"blackboard entry {name!r}: {error}")
        return problems

    def dispose(self, notify: bool = True) -> None:
        """Remove this store's keys and client from the global py_trees blackboard.

        Afterwards the store refuses changes (RuntimeError). With ``notify`` the
        ``entryRemoved`` signal is emitted for every entry.
        """
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
            names = list(self._types)
            self._types.clear()
            self._ids.clear()
            self._bump_locked(True)
            self._reset.clear()
            self._loop_capture = None
            try:
                self._client.unregister(clear=True)
            except Exception:
                log.debug("could not unregister blackboard client", exc_info=True)
        if notify and shiboken6.isValid(self):
            with self.batch():
                for name in names:
                    self.entryRemoved.emit(name)
