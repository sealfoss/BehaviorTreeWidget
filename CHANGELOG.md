# Changes

## 0.9.0 (2026-11-09)

Faster, and split into modules of one job each, with no change to the file formats: 0.9.0 reads
and writes the same files as 0.8.0. It implements the proposals of
[improvements.md](improvements.md), which records the reasoning and the status of each one.

### Performance

Measured on Windows with offscreen rendering, with the same scripts before and after (see the
appendix of [improvements.md](improvements.md)); the stress cases use 500 nodes to show how
costs grow. The 0.9.0 column was measured while other test runs shared the machine, so it is an
upper bound. Rows marked * were measured by the change's own benchmark instead.

| Measurement | 0.8.0 | 0.9.0 |
|---|---|---|
| Repaint after one node's status changes, 500 nodes | 226 ms | 3.7 ms |
| Finding what lies under the mouse, 500 lines fanning out of one parent | 12.7 ms | 0.52 ms |
| Finding what lies under the mouse, empty canvas, same tree | 4.5 ms | 0.04 ms |
| `GetChildren()` of a node with 500 children | 0.89 ms | 0.003 ms |
| Moving one of 500 siblings (per mouse move) | 0.99 ms | 0.07 ms |
| Loading a tree of 100 Evaluation nodes and 50 blackboard entries again | 9.7 s | 0.44 s |
| `AddEntry` while 20 Evaluation nodes exist | 14.2 ms | 1.4 ms |
| Creating a leaf node without fields / with five fields | 1.69 / 3.30 ms | 0.71 / 1.89 ms |
| Changing the title / a field of a node with five fields | 2.0 / 1.8 ms | 0.16 / 0.47 ms |
| Loading / pasting 502 nodes | 1.98 / 2.47 s | 1.28 / 1.70 s |
| 50 ticks of 500 leaves of which only one runs, log off / INFO * | 15.6 / 17.9 ms | 5.9 / 6.0 ms |
| One run of a Sequence of 200 quick worker-thread leaves | 67 ms, 600 threads | 41 ms, 1 thread |
| Longest tick starting 10 worker-thread leaves of 15 ms each * | 157 ms | 110 ms |
| `BlackboardStore.to_list()`: 20 integers and a 5000-item list | 1.22 ms | 0.81 ms |
| The test suite | 3.5 min (2850 tests) | about 30 s (3141 tests, `pytest -n 16`) |

Ticks in which every one of 500 leaves runs cost the same as before (about 190 ms for 50 ticks
with the log off): the time goes to py_trees and to the nodes themselves.

* The view repaints only what changed (`SmartViewportUpdate`), and lines outside the repainted
  area are not painted.
* Each connection line keeps its hit area as a stored path, so finding what lies under the
  mouse calls no Python code per line; a connection drag looks for a label again only after the
  mouse moved 2 pixels.
* Each parent keeps its children in order instead of sorting them at every question; a moved
  child is moved in that order and only the order numbers in between are redrawn.
* Evaluation, Set and Print Message nodes change only the rows of their combo boxes that differ,
  refresh once while a whole blackboard is replaced (a loaded tree's blackboard is installed
  before its nodes), and ignore the blackboard while they are not in a view.
* Nodes are built from Python forms generated from the `.ui` files (`tools/compile_ui.py`), and
  the `.ui` files still parsed share one `QUiLoader`.
* Field rows are one grid of name boxes and editors instead of a list view holding a widget per
  row.
* A tick shows the status of the behaviours it visited and of those that were running, not of
  every node of the tree.
* Worker threads are reused (`WorkerPool`) instead of starting a thread per `OnRun` call.
* The blackboard file writer skips encoding the blackboard when nothing it holds changed (for
  example when only Object entries changed), and `to_list()` encodes values without copying them
  first.

### Behaviour changes

* The waits of one tick for worker-thread `OnRun` calls share a budget,
  `TreeExecutor.TICK_WAIT_BUDGET` (0.1 s). The first call a tick waits for still gets its whole
  `THREAD_WAIT`; later calls get only what is left of the budget, and none once it is spent
  (their nodes show *Running* and finish on a later tick). Before, two slow calls with a large
  `THREAD_WAIT` could hold one tick for the sum of their waits.
* Worker threads are named `behavior_tree_widget:<title>` only while they run a call; a thread
  idle for 60 seconds ends.
* Messages written both to the logger `"behavior_tree_widget"` and to the tree's log now have the
  same text in both. Node errors in the logger use the node's log label
  (`'Raise' (Raise): OnRun raised ValueError: boom` instead of the node's `repr`), and a failed
  tick is logged as `The tick failed: ...` instead of `behavior tree tick failed: ...`.
* Clicking anywhere on a connection's order number selects the connection (before, only the
  part of the number lying within the line's hit area did).
* Custom `.ui` files: a `QListView` named `Fields` is replaced by a plain widget holding the
  field rows; the `ValueName`, `CompareTo` and `Source` combo boxes of Evaluation, Set and Print
  Message nodes are replaced by `EntryComboBox`es that keep their place, name and properties.
* Fixed: a retyped entry now gets its new tooltip in the combo boxes of Evaluation, Set and Print
  Message nodes, and nodes removed from a tree no longer follow renamed entries (which marked the
  tree as modified).

### New

* `NodeWidget.SetFields(fields)` replaces all fields of a node (thread-safe; the node keeps a
  copy).
* `LeafNodeWidget.field_key(key)`: the box showing a field's name.
* `BehaviorTreeWidget.main_view()`, `blackboard_view()`, `blackboard_store()`, `tree_logger()` and
  `loop_execution_check_box()` (see Deprecated).
* `BlackboardStore.revision(saved=False)`, `batch()`, `in_batch()` and the signal
  `batchFinished`.
* `TreeExecutor.TICK_WAIT_BUDGET`.
* `tools/compile_ui.py` (compile the `.ui` files after editing them) and `tools/build_release.py`
  (build the wheel and the source distribution).

### Deprecated

These still work and emit a `DeprecationWarning` that names the replacement:

* Assigning `self._title` or `self._fields` after a node was constructed: use `SetTitle` and
  `SetFields`. Assigning them in `__init__` before `super().__init__()`, or as class attributes,
  is unchanged.
* `mainView()`, `blackboardView()`, `blackboardStore()`, `treeLogger()` and
  `loopExecutionCheckBox()`: use `main_view()`, `blackboard_view()`, `blackboard_store()`,
  `tree_logger()` and `loop_execution_check_box()`.

### Fixed

* A shown widget that is dropped without being closed no longer prints "Error calling Python
  override" messages while it is garbage collected (the view's hide event ran after Python had
  cleared the view's attributes).

### Structure

* `widget.py` (2532 lines) is split into `registry.py` (node types; every built-in type is one
  `NodeKind` in a table), `tree_io.py`, `document.py` (`TreeDocument`: a tree's nodes, file,
  options and unsaved changes, for the tree itself and every sub-tree shown), `zoom.py`,
  `promote.py`, `clipboard.py` and `blackboard_file.py`.
* `canvas.py` is split into `geometry.py`, `items.py`, `gestures.py` (one class per mouse
  gesture) and `menus.py`; `TreeView` stays in `canvas.py`.
* `execution.py` is split into `behaviours.py`, `tree_builder.py`, `execution_host.py` (the
  `ExecutionHost` interface: what the executor needs from the widget) and `_workers.py`.
* `blackboard.py` is a package: `store.py`, `values.py`, `view.py`, `float_spinbox.py`.
* `blackboard_aware.py`: `BlackboardAwareLeaf`, the base of Evaluation, Set and Print Message,
  and `EntryComboBox`.
* Every name that could be imported from the old modules can still be imported from them.
* The documentation moved from one README into [docs/](docs); tests are grouped by feature, with
  opt-in performance tests in `tests/perf` (`pytest -m perf`); ruff, mypy and a GitHub Actions
  workflow check the code.

## 0.8.0 (2026-11-09)

Version 0.7.0 was not released separately; 0.8.0 includes its changes.

* Group node: a named, resizable box that moves everything inside it, with a choice of border
  colours; **Promote to Sub-Tree** writes its contents to `<group name>_BT.json` and replaces it
  by a Sub-Tree node.
* Sub-Tree node: runs the tree of another file as part of the running tree, with the blackboard
  of the tree running it. **Zoom In** shows a sub-tree in place of the tree, to look at or edit;
  **Go Back** returns.
* Print Message node: writes a literal or a blackboard entry to the tree's log at a chosen level.
* The tree's log: one file per tree, levels None / ERROR / WARNING / DEBUG / INFO, the execution
  of every node, a Qt signal per level, and a writer thread so logging never blocks.
* A tree is stored in two files, `<name>_BT.json` (the tree) and `<name>_BB.json` (its
  blackboard); the blackboard file is written, throttled and safely, at every blackboard change.
  Single-file trees of 0.6.0 and earlier still load.
* Blackboard: the **Resets** check box gives an entry its loop-start value back at the end of
  every execution loop; up and down arrows reorder the entries, and the order is saved.

## 0.6.0 (2026-10-23)

* UDP Message node: sends a message to an IPv4 or IPv6 address or a host name.
* Notes node: a resizable box of text that documents a part of the tree, drawn with the largest
  font that fits.
* Blackboard changes are saved to the tree file as they happen.
* Trees written by 0.5.0 load unchanged, and 0.5.0 keeps the UDP Message and Notes nodes of
  0.6.0 trees as placeholders with their data, so a tree can go back and forth.

## 0.5.0 (2026-10-14)

* Selection rectangle (drag on empty canvas; Shift adds), Ctrl + drag pans, Ctrl + click toggles
  a node in the selection.
* **Select Children** selects a node and all of its descendants.
* Delete / Backspace deletes the selected nodes and connection; with the Root selected, its
  connections.
* Restart, Pause and Stop nodes, which control the execution of their own tree.
