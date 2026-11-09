# Proposed improvements to behavior-tree-widget 0.8.0

Proposals for runtime performance, readability and modularity. Nothing here has been applied;
every item names the code it concerns (`file:line` in `src/behavior_tree_widget/`), the
evidence behind it, the proposed change and a rough effort estimate.

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

Most warnings are written twice: `log.warning(...)` for the module logger and
`self._log(LogLevel.WARNING, ...)` for the tree log (`widget.py:1330-1331, 1878-1879, 2113-2114,
2524-2525`; `execution.py:222-223, 261-263, 776-787`). Proposal: one `_report(level, message,
exc_info=None)` on the widget and the executor that writes both, or a `logging.Handler` on the
`behavior_tree_widget` logger that forwards records carrying a `widget` attribute to that widget's
`TreeLogger`.

### R3. Long methods

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

`README.md` is the only documentation and carries the version history inline ("added in 0.6.0 to
0.8.0", compatibility notes per version). Proposal: a `docs/` folder (user guide, writing nodes,
file formats, design notes, i.e. the current sections 3-13), a `CHANGELOG.md`, and a README of
quick start plus links. Read the version from `__version__` in a release script instead of
repeating `0.8.0` in the install instructions.

### R7. Tests

The suite is thorough (2850 tests) but 5k of its 30k lines live in `test_regressions_round3.py`
to `test_regressions_round10.py`, named by review round rather than by feature. Proposal: fold
those tests into the feature files they exercise, keep one `test_regressions.py` for genuinely
cross-cutting cases, and add `tests/perf/` with the benchmarks from Appendix A as opt-in tests
(`-m perf`) so P1-P6 cannot regress silently. The fixture node types in `conftest.py` and the demo
nodes overlap (`Succeed`/`Fail`, `RunningN`/`RunningTicks`); one shared module would do.

---

## 3. Modularity

### M1. Split `widget.py` (2532 lines) by responsibility

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

`geometry.py` (`clamp_to_scene`, `nearest_free_offset`, `connection_path`, `anchor_point`,
`iter_subtree`), `items.py` (`NodeItem`, `ConnectionItem`, `DragLineItem`, `SelectionBandItem`),
`gestures.py` (one class per gesture: pan, move, resize, connect, band select, each with
`begin/update/finish`; the view dispatches by `_Gesture`), `menus.py` (the three `build_*_menu`
functions and the group actions). `TreeView` keeps the model API, selection, locking and painting.

### M5. Make `blackboard.py` (1648 lines) a package

`blackboard/values.py` (`ENTRY_TYPES`, `canonical_type`, `infer_type`, `coerce_value`,
`parse_container_text`, `safe_repr`, ...), `blackboard/float_spinbox.py` (`FloatSpinBox`: 200 lines
of careful float handling that deserve their own unit tests), `blackboard/store.py`
(`BlackboardStore`), `blackboard/view.py` (`EntryRow`, `BlackboardView`, the dialogs).
`blackboard_nodes.py` and `print_node.py` then depend on `values` only.

### M6. Split `execution.py`

`behaviours.py` (`LeafBehaviour`, `FailingBehaviour`, `SubTreeBehaviour`, `_ExecutionLog`),
`tree_builder.py` (`_build`, `execution.py:738-836`, returning `(tree, behaviours, labels,
subtree_nodes)`), and a `CommandQueue` for the ~120 lines of ordering rules in `_command`,
`_run_command`, `_drain`, `_pending`, `_busy`, `_epilogue*` (`532-627`), which are subtle enough to
merit direct unit tests.

### M7. One table for node kinds

`_create_node` (`widget.py:525-539`), `_prepare_nodes` (`2257-2291`), `_node_menu_entries`
(`497-523`) and `_saved_as_builtin` (`263-274`) each special-case Root, composites, Negation, Notes
and Group by name. A `NodeKind` descriptor per class (`menu_group`, `create()`, `is_structural`,
`recognises_saved(item)`) registered in the registry makes adding a built-in node (as 0.6 to 0.8
did five times) a one-table change.

### M8. Tooling that supports the above

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
