# Proposed improvements to behavior-tree-widget 0.8.0

Proposals for runtime performance, readability and modularity. Every item names the code it
concerns (`file:line` in `src/behavior_tree_widget/` of 0.8.0), the evidence behind it, the
proposed change and a rough effort estimate.

**Status:** version 0.9.0 implements these proposals. A status line under each item says what
was done, what was done differently and why, and what was measured; [CHANGELOG.md](CHANGELOG.md)
summarises the results.

Assessed on 2026-11-09 against the working tree of branch `box-select-delete-control-nodes`
(Python 3.14.3, PySide6 6.11.2, py_trees 2.6.0, Windows 11). Method:

* read `README.md` (1329 lines, the project's documentation) and all 20 modules (13.4k lines);
* ran the test suite offscreen: **2850 passed, 1 skipped in 3 min 22 s**;
* wrote and ran 32 feature checks that exercise every README section through the public API
  (node types and registration, tree editing, each built-in node, execution semantics, the
  blackboard and Resets, the log, the two-file format and the 0.5.0 compatibility file,
  copy/paste, notes, groups and promotion, Sub-Tree nodes, Zoom In / Go Back, configuration,
  threading rules, Shutdown, the demo app): **all 32 pass**; no README claim was found wrong;
* rendered the widget with a sample tree (idle, running, Blackboard tab) and checked the output
  against the README (status colours, order badges, connection colours, notes and groups
  stacked below nodes, "Resets" check box and order arrows);
* micro-benchmarked the hot paths and profiled the slow ones (numbers in Appendix A).

Correctness is in good shape. The proposals below are about cost, size and structure.

---

## 1. Runtime performance

Ordered by expected gain per unit of effort. The first three matter for real trees; the rest
show up only with hundreds of nodes but are cheap to fix.

### P1. Every change repaints the whole viewport

**Status: implemented in 0.9.0.** `SmartViewportUpdate`; in addition, connection lines outside the repainted area are not painted. Pixel tests compare what the window shows with a complete repaint (`tests/test_canvas_repaint.py`). Repaint after one status change, 500 nodes: 217 ms before, 3.6 ms after.

* **Evidence**: one node's status change with 500 nodes in the view costs 226 ms to repaint in
  the current mode and 45 ms with `MinimalViewportUpdate` or `SmartViewportUpdate` (offscreen
  software rendering; the 5x ratio is what matters). During execution every tick that changes a
  status triggers one such repaint, on the GUI thread.
* **Cause**: `canvas.py:489` sets `QGraphicsView.ViewportUpdateMode.FullViewportUpdate`.
* **Proposal**: use `SmartViewportUpdate`. The items already report correct bounds: `NodeItem.paint`
  draws inside `boundingRect()` (pen 3.0 at an inset of 1.5), `ConnectionItem.boundingRect` includes
  the hit width and the badge, the band and drag line are plain items, and the grid in
  `drawBackground` is stateless. Add a rendering test (grab before/after a status change) to catch
  stale-pixel artifacts.
* **Effort**: trivial. **Risk**: low; revert if a proxy widget leaves artifacts.

### P2. Blackboard-aware nodes refresh once per entry per node: O(entries x nodes)

**Status: (a) implemented in 0.9.0; (b) implemented differently.** Instead of a shared model, an `EntryComboBox` (`blackboard_aware.py`) applies the minimal edit to the rows it shows: a combo box backed by a shared model changes its current row by itself when the chosen entry is removed, which would change the node's field, while a removed entry must stay chosen and be shown as missing. Refreshes stay synchronous outside batches (combo boxes are current right after `AddEntry`), run once at the end of `BlackboardStore.batch()`, and nodes that are not in a view ignore the store; a loaded tree's blackboard is installed before its nodes. Loading 100 Evaluation nodes and 50 entries into a new widget: 40.7 s before, 0.95 s after; loading it again into a widget holding it: 3.3 s, 0.92 s; refreshing 40 nodes after `AddEntry`: 40.7 ms, 2.4 ms; `AddEntry` with 20 Evaluation nodes: 11.6 ms, 4.8 ms (most of the rest is the Blackboard tab adding its row).

* **Evidence**: loading a tree with 100 Evaluation nodes and 50 entries takes **9.7 s**; the same
  tree with 100 plain leaves takes 0.33 s. `AddEntry` with 20 Evaluation nodes present costs 14 ms
  per entry versus 0.02 ms without them. The profile shows 5100 `QComboBox.addItem` calls and 4000
  layout invalidations for one load.
* **Cause**: `_install_plan` (`widget.py:2377-2387`) adds the nodes first and then the entries one by
  one; each `store.replace`/`add` emits `entryAdded`, and every Evaluation / Set / Print Message
  node answers with a full `_refresh_editors` (`blackboard_nodes.py:327-373`,
  `print_node.py:194-227`): both combo boxes are cleared and refilled (`_fill`, 375-395) and
  `_notify_geometry` runs `relayout` on the node.
* **Proposal** (do a, consider b):
  a. Coalesce refreshes: `_refresh_editors` schedules itself with a single-shot 0 ms timer so any
     number of store signals in one event-loop turn produce one refresh; call `_notify_geometry`
     only when the literal editor's kind or visibility changed. In `_install_plan`, install the
     blackboard before the nodes (or block the store's signals during install and emit one
     `entriesReordered`), so each node refreshes once in `_on_added_to_tree`.
  b. Share one model: keep a `QStandardItemModel` of `(name, type)` rows in the owner, let the
     Value Name combos use it directly (`setModel`) and the Compare To / Source combos through a
     `QSortFilterProxyModel` per node (same type, not itself, plus the "(missing)" row). Adding an
     entry becomes one model insert and no per-node Python work.
* **Expected gain**: load of N blackboard nodes with M entries from N x M refreshes to N; `AddEntry`
  back to sub-millisecond.
* **Effort**: a small; b medium.

### P3. Node construction parses a `.ui` file with a fresh `QUiLoader` every time

**Status: implemented in 0.9.0, item 3 with one change.** 1: one shared `QUiLoader`, which brought most of the gain (creating a loader scanned the Designer plugin folders). 2: `tools/compile_ui.py` writes the forms to `_forms/`, committed; the SHA-256 recorded for each `.ui` file makes an edited but not recompiled file fall back to `QUiLoader`, and the tests fail until the forms are compiled again. 3: the rows are one `QGridLayout`, without list view, model or row widgets, but the name boxes stay read-only line edits: they look like before and need no Python code to paint, where an eliding label would. Measured before item 3: `load_ui("LeafNode.ui")` 1.09 ms before, 0.24 ms after; a leaf without fields 1.69 ms, 0.85 ms; a five-field leaf 3.36 ms, 2.51 ms; loading 502 nodes 1.96 s, 1.55 s.

* **Evidence**: a field-less leaf costs 2.6 ms to construct, of which `load_ui` is 1.7 ms
  (creating the `QUiLoader` ~0.9 ms, `load()` ~0.9 ms); a five-field leaf costs 5.9 ms, 3.8 ms of it
  in `_rebuild_fields`. Consequences: `LoadTree` of 502 nodes 2.0 s, `PasteNodes` of 501 nodes
  2.5 s, `AddNode`+`Connect` x500 1.6 s.
* **Cause**: `_ui.py:54` constructs a new `QUiLoader` per call and parses the XML per node;
  `nodes.py:924-976` builds, per node, a `QStandardItemModel`, a `QListView` with one index widget
  per field, and a `_FieldRow` (read-only `QLineEdit` for the key + the editor + an `QHBoxLayout`).
* **Proposal**:
  1. One module-level `QUiLoader` (`_ui.py`): halves `load_ui` for free.
  2. Compile the packaged `.ui` files with `pyside6-uic` at build time into `ui/*_ui.py` and use
     the generated `setupUi` for built-in nodes; keep `QUiLoader` only for user `UI_FILE` paths.
     Removes XML parsing and turns `findChild` lookups into attributes.
  3. Replace the list-view-of-index-widgets by a `QFormLayout`/`QGridLayout` of `QLabel` keys and
     editors (keep the `btDragArea` property on the labels so dragging on a key still moves the
     node): roughly half the widgets per field, no model, no `sizeHint` pass.
* **Expected gain**: 2-3x faster node creation, which is the whole cost of load and paste.
* **Effort**: 1 trivial; 2 small (build step, `pyproject` package data); 3 medium.

### P4. Children order is re-derived from scene geometry on every call

**Status: implemented in 0.9.0.** Each parent keeps its children in order; a moved child is put back in place by bisection and only the order numbers between its old and new place are redrawn (exact ties fall back to a full sort). `GetChildren()` with 500 children: 0.84 ms before, 0.003 ms after; moving one of 500 siblings: 0.95 ms, 0.048 ms.

* **Evidence**: `GetChildren()` on a node with 500 children costs 0.9 ms per call; moving one of
  500 siblings costs 1 ms per mouse move because `_on_node_geometry` -> `_refresh_order` re-sorts
  the siblings each time. `_tree_data`, `TreeExecutor._build`, `iter_subtree`, `_copy_nodes` and
  `PromoteGroupToSubTree` all call `GetChildren()` for every node.
* **Cause**: `nodes.py:546-557` sorts by `item.sceneBoundingRect()` on each call.
* **Proposal**: cache the ordered children per parent with a dirty flag set from
  `_on_node_geometry` (child moved or resized) and from connect/disconnect; `_refresh_order` only
  rewrites badges when the order actually changed.
* **Effort**: small.

### P5. Per-tick work proportional to the whole tree, not to what ticked

**Status: implemented in 0.9.0.** The visitor records the behaviours a tick visited; those and the ones running before the tick are refreshed, in build order, so signals keep their order. The first tick of a run and any tick after the shown sub-trees changed refresh everything; the twin map is cached and invalidated on Zoom In / Go Back. 50 ticks of 500 leaves of which one runs: 15.6 / 17.9 ms before (log off / INFO), 5.9 / 6.0 ms after; when every leaf ticks: 197 / 534 ms, 190 / 520 ms, which is py_trees and the INFO lines themselves. Left open: py_trees walks the whole tree once per tick for "full" visitors although none is added (about 0.1 ms at 500 nodes); skipping it would mean overriding its tick.

* **Evidence**: 50 ticks of a memory-less Selector over 500 leaves cost 218 ms with logging off
  and 540 ms at INFO. Part of the INFO cost is legitimate (500 "executed" lines per tick), but
  `_refresh_statuses` visits every behaviour every tick and `_ExecutionLog.initialise` snapshots
  every behaviour's status every tick.
* **Cause**: `execution.py:973-1000` and `324-357`; when a sub-tree is shown, `_shown_twins()`
  (`widget.py:2045-2048`) also rebuilds its whole map each tick.
* **Proposal**: the visitor already sees exactly the behaviours that ticked (`run`): collect them
  into a set and let `_refresh_statuses` apply statuses to that set plus the behaviours that were
  RUNNING before the tick (the "interrupted -> Ready" case). Keep a per-behaviour `last_status`
  updated incrementally instead of the per-tick dict. Cache `_shown_twins()` and invalidate it on
  node added/removed and on zoom changes.
* **Effort**: small-medium.

### P6. Hit testing with many overlapping connections

**Status: implemented differently in 0.9.0.** The premise was wrong: even Qt's own C++ `shape()` costs 4.1 ms per query here, because Qt strokes the curve again at every call. So `ConnectionItem` keeps its hit area (the stroke and the order number) as a stored path with no pen, which Qt tests without calling Python; `path()` and `pen()` still give the curve and the visible pen. A connection drag looks for a label again only after the mouse moved 2 pixels or the view changed. In a fan of 500 lines: 12.2 ms before, 0.59 ms after (Qt checking the 235 lines that really overlap the point); on empty canvas: 4.45 ms, 0.042 ms. As a side effect, the whole order number of a line now selects it.

* **Evidence**: `scene.items(pos)` costs 12 ms with 500 siblings fanning out of one parent (235
  connection strokes genuinely overlap the probed points) and 4.5 ms even on empty canvas;
  `hit_test` runs on every mouse move while connecting or hovering. Changing the BSP depth or
  disabling the index made no difference, and neither did caching the stroked shape. With fewer
  than ~20 children per composite this is sub-millisecond, so it is a stress-case cost.
* **Cause**: `ConnectionItem` overrides `shape()` and `boundingRect()` in Python
  (`canvas.py:350-360`), so Qt's hit loop makes two Python virtual calls per candidate item.
* **Proposal**: let `QGraphicsPathItem` do it in C++: give the item a pen of width
  `LINE_HIT_WIDTH` (the default `shape()` strokes with the pen), draw the visible 2.5 px line in
  `paint()` (already custom), and put the badge circle into the path (or a child
  `QGraphicsEllipseItem`) so the default `boundingRect()` covers it. Then delete both overrides.
  Also skip `hit_test` in `mouseMoveEvent` when the pointer moved less than 2 px.
* **Effort**: small.

### P7. A new thread per `OnRun` call and a GUI-thread wait per started leaf

**Status: implemented in 0.9.0.** `WorkerPool` (`_workers.py`): daemon threads that are reused, a new one only when all are busy, idle ones end after 60 s, a stuck call holds one and never blocks exit. `run_in_daemon_thread` is unchanged (a test requires a new thread per call). The per-tick budget is the class attribute `TreeExecutor.TICK_WAIT_BUDGET = 0.1` s (a `TreeConfig` option was considered; a class attribute keeps tree files unchanged). 200 quick worker-thread leaves: 54.8 ms and 600 threads before, 37.2 ms and 1 thread after; the longest tick starting 10 leaves of 15 ms: 157 ms, 110 ms.

* **Evidence**: a Sequence of 200 quick worker-thread leaves completes in one tick of 67 ms
  (~0.33 ms per leaf: thread start plus the `THREAD_WAIT` poll).
* **Cause**: `execution.py:97-116` starts a `threading.Thread` per call (deliberately, so an `OnRun`
  that never returns cannot block exit); `199-202` blocks the GUI thread up to `THREAD_WAIT` (20 ms)
  for each newly started threaded leaf, so k quick leaves in one tick can stall up to k x 20 ms.
* **Proposal**: keep the daemon semantics but reuse threads: a small pool of daemon workers fed by
  a queue, growing when all workers are busy (a stuck call then occupies one worker, as today it
  occupies one thread). Budget the wait per tick (e.g. the first threaded leaf gets `THREAD_WAIT`,
  later ones share what is left) so the GUI stall is bounded per tick.
* **Effort**: medium. **Risk**: cancellation semantics are subtle; the existing tests cover them well.

### P8. Smaller items

**Status: mostly implemented in 0.9.0.** `to_list()` encodes under the lock without copying first (1.4x to 1.8x faster); the blackboard file writer compares `BlackboardStore.revision(saved=True)` and does not encode at all when nothing the file holds changed; `get()` still copies containers, since callers may change what they get. `TreeView` keeps its root, keeps its nodes in a dict and sorts the selection only when asked; the grid is drawn with `drawLines`; `nearest_free_offset` is unchanged. The `relayout` cache was not done: it costs 0.16 ms per title change, which is not on a hot path, and a cached list of layouts would go stale whenever a node replaces editors (literal editors, entry combo boxes, field rows); P3.3 (fewer widgets per node) and P2 (fewer relayouts of the nodes listing the blackboard) reduced the cost it targeted.

* `BlackboardStore.get` deep-copies containers per call (`blackboard.py:609-613`): a 5000-item
  list costs 0.4 ms per read; `to_list` copies and then encodes. Encode directly under the lock in
  `to_list` and let `_write_blackboard_file` (`widget.py:1322`) compare a change counter from the
  store instead of re-encoding and comparing the whole list every 250 ms.
* `relayout` (`nodes.py:177-188`) calls `findChildren(QWidget)` and invalidates every layout on each
  title change (0.16 ms per `SetTitle`); cache the layout list per node and refresh it in
  `_rebuild_fields`.
* `TreeView.root()` and `selected_nodes()` scan `_nodes` linearly (`canvas.py:562-580`); keep a
  `_root` reference and keep the selection in view order.
* `drawBackground` (`canvas.py:1777-1801`) issues one `drawLine` per grid line; batch with
  `drawLines`.
* `nearest_free_offset` scales well (38 ms for 50 rectangles against 1600 obstacles); leave it.
* Field updates are cheap (`SetField` 0.04 ms); no change needed there.

---

## 2. Readability

### R1. `__setattr__` interception of `_title` / `_fields`

**Status: implemented in 0.9.0.** Class-level defaults replace every `__dict__` guard; assigning `self._title` / `self._fields` after construction emits a `DeprecationWarning` (and still works); internal code uses `_apply_title` / `_replace_fields`; `SetFields(fields)` is new. The test suite turns the deprecation into an error, so internal use fails the tests.

`nodes.py:317-328` makes assignments to `self._title` and `self._fields` update the UI. It is the
reason for the `self.__dict__.get("_name")` idiom (17 sites in 8 files) and for `super().__setattr__`
calls, and it makes attribute order in constructors fragile (`subtree.py:57-61`,
`print_node.py:63-65`, `groups.py:108-110` set attributes before `super().__init__()` to dodge it).

Proposal: keep the documented contract (class attributes, and assignment in `__init__` before
`super().__init__()`, which `NodeWidget.__init__` already reads from `__dict__`) and route later
changes through `SetTitle` / `SetField`. Emit a `DeprecationWarning` for post-construction
assignment for one release, then drop `__setattr__`. Initialise the guarded attributes to `None`
before `super().__init__()` and remove the `__dict__.get` guards.

### R2. Two log channels written by hand everywhere

**Status: implemented in 0.9.0.** `BehaviorTreeWidget._report` and the executor's `report` write the module logger and the tree log with one text.

Most warnings are written twice: `log.warning(...)` for the module logger and
`self._log(LogLevel.WARNING, ...)` for the tree log (`widget.py:1330-1331, 1878-1879, 2113-2114,
2524-2525`; `execution.py:222-223, 261-263, 776-787`). Proposal: one `_report(level, message,
exc_info=None)` on the widget and the executor that writes both, or a `logging.Handler` on the
`behavior_tree_widget` logger that forwards records carrying a `widget` attribute to that widget's
`TreeLogger`.

### R3. Long methods

**Status: implemented in 0.9.0, except the last row.** `mousePressEvent` dispatches per hit kind; `_tick` is `_tick_tree` + `_after_tick`; `LeafBehaviour.update` is five steps; Promote to Sub-Tree is `plan_promotion` / `write_subtree` / `replace_group` (`promote.py`); `_prepare_nodes` is `tree_io.prepare_nodes` + `_apply_saved_node_data`; `_install_loaded_tree` uses `_settle_blackboard_file`, `_snapshot_current` and `_restore_previous`; `EntryRow.__init__` uses `_reset_check_box`, `_build_value_editor` and `_wire_signals`. `demo._unconsumed_arguments` was rewritten as a short memoized recursion with the same results, not replaced: argparse cannot tell which entries Qt consumed (the value of `-style fusion` looks like a file name), and `QCommandLineParser` reads `QApplication.arguments()`, which garbles non-ASCII file names on Windows, the reason the function exists.

| Method | Lines | Suggested split |
|---|---|---|
| `TreeView.mousePressEvent` (`canvas.py:955-1054`) | ~100 | one handler per `Hit` kind: `_press_label`, `_press_node`, `_press_resize`, `_press_empty` |
| `TreeExecutor._tick` (`execution.py:895-971`) | ~75 | `_tick_tree()` returning an outcome, `_after_tick(outcome)` for the epilogue |
| `LeafBehaviour.update` (`execution.py:167-225`) | ~60 | `_wait_for_previous_call`, `_start`, `_run_threaded`, `_run_inline` |
| `BehaviorTreeWidget.PromoteGroupToSubTree` (`widget.py:1763-1898`) | 135 | `_validate_promotion` -> `_subtree_document` -> `_write_subtree_file` -> `_replace_group` |
| `BehaviorTreeWidget._prepare_nodes` (`widget.py:2237-2326`) | 90 | `_node_from_item(item)` + `_apply_saved_node_data(node, item)` |
| `BehaviorTreeWidget._install_loaded_tree` (`widget.py:2453-2532`) | 80 | `_settle_blackboard_file(plan)`, `_snapshot_current()`, `_restore_previous()` |
| `EntryRow.__init__` (`blackboard.py:1195-1268`) | 75 | `_build_value_editor(type_name)` and `_wire_signals()` |
| `demo._unconsumed_arguments` (`demo.py:181-209`) | 30 | replace by `QCommandLineParser`, or `argparse.parse_known_args` run before `QApplication` |

### R4. Repeated micro-patterns that deserve helpers

**Status: implemented in 0.9.0.** `QSignalBlocker` replaces every manual pair; `set_text_quietly` is in `_qt.py`; `BlackboardAwareLeaf` and `EntryComboBox` (`blackboard_aware.py`) replace the duplicated code; `INT_MIN` / `INT_MAX`, `file_key` and the file filter are in `_common.py`; the five predicates are `NodeTypeRegistry.offers()`.

* `blocked = w.blockSignals(True); try: ... finally: w.blockSignals(blocked)` appears 22 times in
  6 files. PySide6 ships `QSignalBlocker`: `with QSignalBlocker(widget): ...`.
* "set the text, and move the cursor to 0 if the editor is not focused" is repeated for every line
  edit that mirrors a value (`subtree.py:164-171`, `groups.py:149-157`, `print_node.py:215-222`,
  `nodes.py:1067-1071`, `blackboard.py:1377-1381`): `set_text_quietly(edit, text)`.
* The combo-box filling with a red "(missing)" row and tooltips exists twice (`blackboard_nodes.py:375-395`
  and `print_node.py:229-247`), as do `_on_added_to_tree`, `_entry_types`, `_on_entries_changed` and
  `_on_entry_renamed` (`blackboard_nodes.py:283-318`, `print_node.py:154-185`). A
  `BlackboardAwareLeaf` mixin plus an `EntryComboBox` widget removes ~120 duplicated lines.
* Constants defined twice: `INT_MIN`/`INT_MAX` (`nodes.py:90-91`, `blackboard.py:68-69`),
  `_file_key` (`execution.py:371-373`, `widget.py:2028-2030`), `FILE_FILTER` (`subtree.py:55`,
  `widget.py:325`).
* Five predicates for "is this built-in type offered here" (`widget.py:475-495`:
  `_is_builtin`, `_has_builtin`, `_offers_notes`, `_offers_groups`, `_offers_box`) can be two.

### R5. Naming, stale comments, unused code

**Status: implemented in 0.9.0.** The accessors use snake_case, like `view()`, `button()` and the `TreeView` API: `main_view()`, `blackboard_view()`, `blackboard_store()`, `tree_logger()`, `loop_execution_check_box()`; the camelCase names are deprecated aliases. The stale comment is fixed. `NodeFactory`, `TreeView.center_on_node` and `NotesNodeWidget._resize_note` are deleted; `connection_item`, `load_list`, `parse_list`, `is_capturing` and `client()` are used by tests and are now documented as public.

* The accessor group mixes styles: `view()`, `mainView()`, `blackboardView()`, `button()`,
  `treeLogger()` next to the CamelCase API (`GetRootNode`). Pick one style for accessors.
* Stale comment: `widget.py:327-328` says blackboard changes are "written to the tree file"; since
  the file split they go to the blackboard file.
* `nodes.py:1-12` says "Four kinds of nodes exist" and mentions Notes only; Groups and the
  `BoxNodeWidget` base are missing. `canvas.py:442` describes `Hit.RESIZE` as a note handle; it is
  also a group handle.
* Not used inside the package (tests or users only, or nothing): `NodeFactory` (`canvas.py:1836`),
  `BlackboardStore.load_list`, `parse_list`, `is_capturing`, `client()`,
  `TreeView.center_on_node`, `connection_item`. Delete, or document as public.
* `NotesNodeWidget._resize_note` (`notes.py:509-511`) only forwards to `_resize_box`.

### R6. Documentation shape

**Status: implemented in 0.9.0.** [docs/](docs) holds the user guide, built-in nodes, writing nodes, file formats, API reference, development and design notes; [CHANGELOG.md](CHANGELOG.md) the changes; README.md the overview, installation and quick start. `tools/build_release.py` reads the version from `__version__`, and the install instructions name no version.

`README.md` is the only documentation and carries the version history inline ("added in 0.6.0 to
0.8.0", compatibility notes per version). Proposal: a `docs/` folder (user guide, writing nodes,
file formats, design notes, i.e. the current sections 3-13), a `CHANGELOG.md`, and a README of
quick start plus links. Read the version from `__version__` in a release script instead of
repeating `0.8.0` in the install instructions.

### R7. Tests

**Status: implemented in 0.9.0, except the shared node module.** The 150 tests of the regression round files are in the feature files they test (five new ones: `test_execution_commands.py`, `test_execution_loading.py`, `test_canvas_view.py`, `test_widget_lifecycle.py`, `test_demo.py`), with their helpers in `tests/support/`; all 3139 test ids were compared before and after. Opt-in performance tests are in `tests/perf` (`pytest -m perf`). The test node types of `conftest.py` and the demo nodes were not merged: they differ in titles, type names and defaults that tests check. pytest-xdist runs the suite in about 30 s on 16 workers instead of 3.5 minutes.

The suite is thorough (2850 tests) but 5k of its 30k lines live in `test_regressions_round3.py`
to `test_regressions_round10.py`, named by review round rather than by feature. Proposal: fold
those tests into the feature files they exercise, keep one `test_regressions.py` for genuinely
cross-cutting cases, and add `tests/perf/` with the benchmarks from Appendix A as opt-in tests
(`-m perf`) so P1-P6 cannot regress silently. The fixture node types in `conftest.py` and the demo
nodes overlap (`Succeed`/`Fail`, `RunningN`/`RunningTicks`); one shared module would do.

---

## 3. Modularity

### M1. Split `widget.py` (2532 lines) by responsibility

**Status: implemented in 0.9.0.** `widget.py`: 2532 lines before, about 1700 after (mostly API docstrings); the new modules are `registry.py`, `tree_io.py`, `document.py`, `zoom.py`, `promote.py`, `clipboard.py` and `blackboard_file.py`. Names imported from `widget.py` before still are importable from it.

| Lines today | Responsibility | Proposed module |
|---|---|---|
| 82-110, 169-219 | node type registry: `_register`, `_validate_leaf_class`, built-in/yielding/reserved names, `register_node_type` | `registry.py`: a `NodeTypeRegistry` class; the widget owns one, the decorator fills a global one |
| 222-275, 1444-1560, 2213-2407 | `_TreePlan`, `_prepare_*`, `_install_nodes`, `_node_data`, `_tree_data`, `_new_tree_data`, `_saved_as_builtin`, `_json_writable` | `tree_io.py`: pure functions over a node factory, testable without a widget |
| 115-145, 1562-1681 | clipboard format, copy, paste, exit guard | `clipboard.py` |
| 1283-1342 | throttled blackboard file writes, last-written state, skipped values, error state | `blackboard_file.py`: a `BlackboardFile` object with `saved`/`failed` signals |
| 1734-1922 | promote a group to a sub-tree | `promote.py` |
| 243-256, 1924-2211 | `_ZoomLevel` and the zoom stack | `zoom.py`: a `ZoomStack` |
| rest | tabs, buttons, signals, public API delegating to the above | `widget.py` (~900 lines) |

### M2. A document model separate from the view

**Status: implemented in 0.9.0, in two steps.** First `TreeDocument` took a shown tree's file state (path, options, unsaved changes, the blackboard section of old files) and writing it, for the tree itself and every zoom level. Then the document took the nodes and their links: a `TreeView` shows a document, and the hidden nodes executing a Sub-Tree node form a document that is not shown.

`TreeView` is both the scene and the model (`_nodes`, `_connections`, order, selection), and "the
tree itself" versus "the sub-tree shown" is encoded as `_main_view` versus `_levels[-1].view`,
`_modified` versus `level.modified`, with branching throughout `widget.py` (`_view` property,
`_tree_data(level=...)`, `_main_root`, `_all_views`, `_on_data_changed` resolving the level by view,
`_save_tree` next to `_save_level`). `_load_subtree` (`widget.py:1684-1732`) has to fake a
view-less graph by appending to `_children` directly and storing `_detached_order`.

Proposal: a `TreeDocument` (path, config, nodes, connections, modified flag, legacy blackboard
section, base folder) owned by every level including level 0; `TreeView` renders and edits a
document; the widget holds a stack of documents. Save, modified tracking and file naming become one
code path, and sub-trees load headlessly as documents without a view.

### M3. Explicit internal interfaces instead of private attribute access across modules

**Status: implemented in 0.9.0.** `NodeWidget._link` / `_unlink` / `scene_rect`; the `ExecutionHost` protocol and `WidgetExecutionHost` (`execution_host.py`), the only place the executor reads the widget; behaviours use executor methods (`generation()`, `is_aborting()`, ...), and `TreeExecutor.abandon()` replaces the widget poking at executor state when it is destroyed.

79 places in `canvas.py`, `execution.py` and `widget.py` reach into other objects' privates
(`node._item/_children/_parent/_lock/_canvas`, `owner._blackboard/_config/_levels/_shown_twins()`,
`executor._owner/_generation/_aborting/_token_owner`). Both `canvas.connect_nodes`
(`canvas.py:727-730`) and `widget._load_subtree` (`1713-1718`) mutate `_children`/`_parent` under the
node locks by hand.

Proposal: small internal methods and protocols: `NodeWidget._link(child)` / `_unlink(child)`,
`NodeWidget.scene_rect()`; an `ExecutionHost` protocol the executor takes (`root()`, `config()`,
`blackboard()`, `log()`, `visible_twins()`), so `TreeExecutor` can be tested against a stub; and
executor methods (`generation()`, `is_aborting()`) for `LeafBehaviour`. No behaviour change, but
each module's dependencies become visible.

### M4. Split `canvas.py` (1837 lines)

**Status: implemented in 0.9.0.** `geometry.py`, `items.py`, `gestures.py` (one class per gesture; `view._gesture` is a property giving the gesture kind) and `menus.py`; `canvas.py` re-exports the old names.

`geometry.py` (`clamp_to_scene`, `nearest_free_offset`, `connection_path`, `anchor_point`,
`iter_subtree`), `items.py` (`NodeItem`, `ConnectionItem`, `DragLineItem`, `SelectionBandItem`),
`gestures.py` (one class per gesture: pan, move, resize, connect, band select, each with
`begin/update/finish`; the view dispatches by `_Gesture`), `menus.py` (the three `build_*_menu`
functions and the group actions). `TreeView` keeps the model API, selection, locking and painting.

### M5. Make `blackboard.py` (1648 lines) a package

**Status: implemented in 0.9.0.** `blackboard/` with `store.py`, `values.py`, `view.py` and `float_spinbox.py`; `FloatSpinBox` has 55 unit tests of its own.

`blackboard/values.py` (`ENTRY_TYPES`, `canonical_type`, `infer_type`, `coerce_value`,
`parse_container_text`, `safe_repr`, ...), `blackboard/float_spinbox.py` (`FloatSpinBox`: 200 lines
of careful float handling that deserve their own unit tests), `blackboard/store.py`
(`BlackboardStore`), `blackboard/view.py` (`EntryRow`, `BlackboardView`, the dialogs).
`blackboard_nodes.py` and `print_node.py` then depend on `values` only.

### M6. Split `execution.py`

**Status: implemented in 0.9.0.** `behaviours.py`, `tree_builder.py`, `execution_host.py`, `_workers.py`, and a `CommandQueue` in `execution.py` (`TreeExecutor._command` stays there).

`behaviours.py` (`LeafBehaviour`, `FailingBehaviour`, `SubTreeBehaviour`, `_ExecutionLog`),
`tree_builder.py` (`_build`, `execution.py:738-836`, returning `(tree, behaviours, labels,
subtree_nodes)`), and a `CommandQueue` for the ~120 lines of ordering rules in `_command`,
`_run_command`, `_drain`, `_pending`, `_busy`, `_epilogue*` (`532-627`), which are subtle enough to
merit direct unit tests.

### M7. One table for node kinds

**Status: implemented in 0.9.0.** `registry.BUILTIN_KINDS`: one `NodeKind` per built-in type (menu section, factory, how its saved data is recognised); creating, offering, the add-node menu and loading all read it.

`_create_node` (`widget.py:525-539`), `_prepare_nodes` (`2257-2291`), `_node_menu_entries`
(`497-523`) and `_saved_as_builtin` (`263-274`) each special-case Root, composites, Negation, Notes
and Group by name. A `NodeKind` descriptor per class (`menu_group`, `create()`, `is_structural`,
`recognises_saved(item)`) registered in the registry makes adding a built-in node (as 0.6 to 0.8
did five times) a one-table change.

### M8. Tooling that supports the above

**Status: implemented in 0.9.0.** ruff and mypy are configured in `pyproject.toml` (the generated forms excluded; each ignored ruff rule has its reason there). mypy went from 199 errors to none, with three `type: ignore`s for gaps in the Qt and typeshed stubs. The GitHub Actions workflow `.github/workflows/ci.yml` runs ruff, mypy and `tools/compile_ui.py --check` on Ubuntu, the tests on Windows with Python 3.10 and 3.13, and builds the wheel and sdist as an artifact. The wheels are still committed to `dist/`, as this repository does; `tools/build_release.py` builds them.

* Add `[tool.ruff]` (the `noqa` codes in use imply `E`, `N`, `BLE`, `SIM`) and `[tool.mypy]` to
  `pyproject.toml`; the package ships `py.typed` but is not type-checked.
* A CI workflow: ruff, mypy, pytest offscreen, `python -m build`; publish wheels as release assets
  instead of committing `dist/*.whl` (the working tree currently deletes the 0.6.0 wheels and adds
  the 0.8.0 ones).
* A `pyside6-uic` step for P3 / the generated `ui/*_ui.py`.

---

## Suggested order

1. P1 (one line, large effect during execution), P3.1 (one line), P2a, P4, P6, P8 relayout cache.
2. R1, R2, R4 helpers, R5 clean-ups, M8 tooling.
3. M1 + M3 (registry, tree I/O, clipboard, blackboard file, interfaces), then M4 / M5 / M6.
4. P2b, P3.2/3.3, P5, P7, M2, M7 (each a self-contained refactor with the existing suite as the net).

---

## Appendix A: measurements

Offscreen Qt, software rendering, Python 3.14.3; best of a few runs. Stress-case numbers (500
nodes) are there to expose scaling, not to represent typical trees.

| Measurement | Result |
|---|---|
| Test suite (2851 tests) | 3 min 22 s |
| `load_ui("LeafNode.ui")` | 1.2 ms per call (QUiLoader construction ~0.9 ms of it) |
| Construct a field-less leaf / a five-field leaf | 2.6 ms / 5.9 ms |
| `AddNode`+`Connect` x500 | 1.62 s |
| `SaveTree`, 502 nodes | 7 ms |
| `LoadTree`, 502 nodes | 2.0 s |
| `LoadTree`, 100 plain leaves + 50 entries | 0.33 s |
| `LoadTree`, 100 Evaluation nodes + 50 entries | **9.7 s** |
| `AddEntry` with 20 Evaluation nodes present / none | 14 ms / 0.02 ms per entry |
| `CopyNodes` / `PasteNodes`, 501 nodes | 7 ms / 2.5 s |
| `GetChildren()` with 500 children | 0.9 ms per call |
| Move one of 500 siblings (`setPos`) | 1.0 ms per move |
| `SetTitle` (five-field node) / `SetField` | 0.16 ms / 0.04 ms |
| `scene.items(pos)` with 500 siblings under one parent | 12 ms (fan), 4.5 ms (empty canvas) |
| Repaint after one status change, 500 nodes: Full / Minimal / Smart | 226 / 45 / 45 ms |
| 50 ticks, memory-less Selector over 500 leaves: log NONE / INFO | 218 / 540 ms |
| Sequence of 200 quick worker-thread leaves, one run | 67 ms, 1 tick |
| `SetEntry` on an existing Integer (idle) | 29 us |
| `GetEntry` of a 5000-item list (deep copy) | 0.4 ms |
| `Log()` enqueue / flush of 10 000 messages | 4.3 us per call / 40 ms |
| `nearest_free_offset`, 50 rects vs 100 / 400 / 1600 obstacles | 3.5 / 9.9 / 38 ms |

## Appendix B: reproducing

The scripts live in this session's scratch folder and can be copied into `tests/perf/`:

* `C:\Users\reed\AppData\Local\Temp\claude\3477146f2a7347f4\afd338fb-7df6-4188-a923-1cbca8afdbbc\scratchpad\verify_features.py` (the 32 README checks)
* `C:\Users\reed\AppData\Local\Temp\claude\3477146f2a7347f4\afd338fb-7df6-4188-a923-1cbca8afdbbc\scratchpad\bench.py`, `bench2.py`, `bench3.py` (timings, profiles, hit-test attribution)
* `C:\Users\reed\AppData\Local\Temp\claude\3477146f2a7347f4\afd338fb-7df6-4188-a923-1cbca8afdbbc\scratchpad\screenshot.py` (renders `shot_tree_idle.png`, `shot_tree_running.png`, `shot_blackboard.png`)

Run each with `.venv/Scripts/python -I <script>`; they set `QT_QPA_PLATFORM=offscreen` themselves.
