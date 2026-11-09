# User guide

## Using the editor

| Action | How |
|---|---|
| Add a node | Right-click empty canvas (or the inside of a group) and choose Sequence, Selector, Negation, Evaluation, Set, Restart, Pause, Stop, UDP Message, Print Message, Sub-Tree, one of your leaf types (sorted by title), Notes or Group. The node is placed at the click position. |
| Add a connected node | Drag from a **Children** or **Parent** label and release over empty canvas (or a line). The Add Node menu opens there, as for a right click. The node you choose is placed where you released and connected to the label you dragged from: below a Children label, or above a Parent label (replacing an old parent). A node that cannot be connected that way, such as a leaf as a parent, is added unconnected. Closing the menu adds nothing. |
| Delete / rename a node | Select nodes and press **Delete** (or Backspace), or right-click a node → Delete / Rename…. The Root cannot be deleted; pressing Delete with the Root selected deletes its connections instead. Notes have **Edit Text** instead of Rename…; a group is also renamed in its title bar. |
| Edit or resize a note | Click its text to edit it; drag an edge or a corner to resize it (see [Notes](built-in-nodes.md#notes)). |
| Groups | Drag a group's title bar to move it with everything inside it; drag an edge or a corner to resize it; right-click its title bar for Rename…, Select Contents, Border Color and Promote to Sub-Tree (see [Groups](built-in-nodes.md#groups)). Clicks inside a group go to what lies below it. |
| Change a composite | Right-click a Sequence, Selector or the Root → Sequence or Selector, and Memory on/off. |
| Zoom into a sub-tree | Right-click a Sub-Tree node → **Zoom In** shows its tree in place of this one, to look at or edit; **Go Back** returns (see [Zooming into sub-trees](built-in-nodes.md#zooming-into-sub-trees)). |
| Connect | Press on a **Children** label (a Negation's **Child** label) and drag to another node's **Parent** label, or the other way round. While you drag, the line is red, and turns green over a compatible label. Release there to connect; the line turns blue. Releasing over a node cancels; releasing over empty canvas opens the Add Node menu (see above). Connecting a node that already has a parent replaces its old connection, and so does connecting a second child to a Negation. Cycles are not allowed. |
| Select / delete a connection | Click the line or its order number; it turns green. Press **Delete** (or Backspace) to delete it. Clicking anywhere else deselects it. Right-click a line → Delete Connection also works. |
| Select nodes | Click a node; selected nodes have a blue outline. Clicking one of several selected nodes (without dragging) selects only it. **Ctrl + click** adds or removes a node, **Shift + click** adds one. **Drag** on empty canvas (or from a line, or inside a group) selects every node the rectangle touches (a group: its title bar); **Shift + drag** adds them to the selection. Right-click a Sequence, Selector, Negation or the Root → **Select Children** selects it and all of its descendants. **Ctrl+A** selects all nodes. Click empty canvas (without dragging) or press **Esc** to deselect. |
| Move nodes | Drag a node anywhere except its Parent/Children labels and input fields; dragging on a field's name works, and so does dragging on a note's text (a note's edges and corners resize it) and on a group's title bar (moving everything inside the group too). Dragging a selected node moves every selected node. |
| Copy and paste nodes | Select nodes, press **Ctrl+C**, then **Ctrl+V**. The copies are placed in the free area nearest to the original nodes, at least 20 pixels from every other node, keeping their arrangement and the connections between them. They become the selection, and the view scrolls to show them if needed. Each Ctrl+V pastes another set. The Root is never copied, and connections to nodes that were not copied are not copied. While an input field of a node has the keyboard, Ctrl+C / Ctrl+V / Ctrl+A act on its text instead. Copied nodes can be pasted into another BehaviorTreeWidget, also in another application; node types it does not know become placeholders. |
| Pan | **Ctrl + drag** anywhere except on a Parent/Children label or an input field, or drag with the middle mouse button anywhere. The mouse wheel (without Ctrl) scrolls the view. Panning keeps the selection. |
| Zoom | Ctrl + mouse wheel, from 0.2× to 3×. |
| Edit fields | Use the editors on leaf nodes; changes take effect immediately. |

Connection label colours:
* **red**: not connected
* **green**: a connection is being placed
* **blue**: connected

The numbers on connection lines show the execution order of a composite's children. The order
is **left to right** as drawn (for ties, the higher node comes first); move nodes to reorder.
While a tree executes, the numbers keep showing the order the running tree uses; moving nodes
changes the order at the next Execute.
If a Sequence, Selector, Negation or Root node's title differs from its type, the type is shown
in brackets, for example "Root (Sequence)".

While a tree is executing (running or paused), its structure is locked:
* Nodes and connections cannot be added or removed, and Ctrl+V pastes nothing.
* The composite type, Memory, Delete and Promote to Sub-Tree menu items, and the Delete key,
  are disabled.
* `AddNode`, `RemoveNode`, `Connect`, `Disconnect`, `PasteNodes`, `PromoteGroupToSubTree`,
  `NodeWidget.SetParent`, `AddChild` and `RemoveChild` raise `RuntimeError`.

You can still select, copy, move and rename nodes, edit fields and the blackboard, edit and
resize notes, move, resize, rename and recolour groups, and zoom into sub-trees and back. This
applies to the sub-trees shown with Zoom In as well.
`SetCompositeType` / `SetMemory` called from code take effect at the next Execute.

## The blackboard

The **Blackboard** tab lists named entries that nodes share:

* **Add an entry:** choose a type in the combo box and click **Add Entry**. A dialog asks for
  the name and the initial value.
* **Remove entries:** tick entries (or **Select All**) and click **Remove Entries**.
* **Edit an entry:** names and values can be edited in place. List, Dictionary and Set values
  are edited with their **Edit…** button, as a Python literal such as `[1, 2.5, 'text']` or
  `{'key': 3}`; `nan` and `inf` are accepted.
* **Resets:** check an entry's **Resets** box to give it back, at the end of every execution
  loop, the value it had when that loop started (see [Reset entries](#reset-entries)).
* **Order:** the up and down arrows at the end of an entry move it one place up or down the
  list (the top entry's up arrow and the bottom entry's down arrow are disabled). The order is
  the order of the blackboard file, so it is kept, and it is the order in which the combo boxes
  of the Evaluation, Set and Print Message nodes list the entries.
* **Changes are written to the blackboard file automatically.** The blackboard is stored in
  its own file, `<tree name>_BB.json`, next to the tree file `<tree name>_BT.json` (see
  [Tree files](file-formats.md#tree-files)). Whenever an entry is added, removed, renamed, retyped, moved,
  changes its value or its Resets box, by you, by your code or by the running tree, the
  blackboard file is rewritten. Details:
  * Only the blackboard file changes. The tree file stays as it was last saved, so other edits
    (nodes, fields, options) remain unsaved until you click **Save**.
  * Blackboard changes are therefore never "unsaved changes". If the file cannot be written
    (for example, it is read-only), the warning is logged once, `blackboardSaveFailed(str)` is
    emitted, the tree counts as modified so the close prompt offers to save it, and the next
    change tries again.
  * Writes are throttled: at most one every `BLACKBOARD_SAVE_INTERVAL_MS` (250 ms). Changes
    in between are written together at the end of the interval, and anything still due is
    written before another tree is loaded or created, when the tree is saved to another file,
    and on `Shutdown()`. `blackboardSaved(str path)` is emitted after each write, with the
    path of the blackboard file.
  * Each write replaces the whole file safely: a new copy is written next to it and then
    swapped in, so an interrupted write never damages the file. See
    [Writing only the blackboard](design.md#writing-only-the-blackboard).
  * Nothing is written before a tree has been created, loaded or saved, and Object entries
    (never saved) cause no writes.

| Type | Python type | Editor |
|---|---|---|
| Integer | `int` (32-bit) | spin box |
| Double | `float` | double spin box showing the exact value (also `nan`, `inf`); type `inf` / `-inf` for infinity, `,` is accepted as decimal point |
| String | `str` | line edit |
| Bool | `bool` | check box |
| List | `list` | combo box listing `str()` of each item |
| Dictionary | `dict` | combo box listing `key: value` |
| Set | `set` | combo box |
| Object | anything else | read-only text; only created from code, never saved |

From code (see [Threading rules](writing-nodes.md#threading-rules)):

```python
widget.SetEntry(3, "counter")          # creates an Integer entry (type inferred) or updates it
widget.GetEntry("counter")             # -> 3   (KeyError if it does not exist)
widget.AddEntry("speed", "Double", 1.5)
widget.AddEntry("visits", "Integer", 0, reset=True)   # with its Resets box checked
widget.SetEntryReset("speed", True), widget.GetEntryReset("speed")
widget.MoveEntry("speed", -1)          # one place up the list (-> its new index); +1: down
widget.HasEntry("speed"), widget.GetEntryNames(), widget.GetEntryType("speed")
widget.RemoveEntry("speed")
```

* `SetEntry(value, name)` creates missing entries, inferring the type:
  * `bool` → Bool
  * `int` within ±2³¹ → Integer; larger ints → Object
  * `float` → Double, `str` → String
  * `list`/`tuple` → List (stored as a list), `dict` → Dictionary, `set`/`frozenset` → Set
  * anything else, including `None` → Object
  * A new list/dict/set holding objects that cannot be copied (locks, devices, …) → Object.
* `AddEntry(name, type_name, value=None, reset=False)` accepts these type names,
  case-insensitive: Integer/Int, Double/Float, String/Str, Bool/Boolean, List,
  Dictionary/Dict, Set, Object. If `value` is None, the entry gets the type's default value.
  `reset=True` checks its Resets box.
* `SetEntry` on an existing entry needs a compatible value: an `int` (not a `bool`) or an
  integral `float` for Integer, an `int` or `float` (not `bool`) for Double, and so on.
  Otherwise it raises `TypeError` / `ValueError`.
* `GetEntry` returns copies of List/Dictionary/Set values; call `SetEntry` to publish changes.
* `MoveEntry(name, offset)` moves an entry `offset` places down the list (up when negative),
  stopping at either end, and returns its new index; `GetEntryNames()` lists the entries in
  that order. Moving is thread-safe. Like the other changes made while the tree executes, a
  move made then is undone by "Restore blackboard values on Stop / Reset" when that option is
  on (it restores the blackboard as it was when execution started, order included).
* Entry names must be non-empty, must not start or end with spaces, and cannot contain `.` or
  `/`.
* **Object entries are run-time values.** Use them for things like device handles: they are
  never written to tree files and are kept when a tree is created or loaded.
* Each entry is stored in the py_trees blackboard under the key
  `widget.GetBlackboardNamespace() + "/" + name`, for example
  `/behavior_tree_widget_79daae42568b/counter`. Plain py_trees code can read it with
  `py_trees.blackboard.Blackboard.get(key)` or a Client with READ access. Write through
  `SetEntry`, so values are validated and shown in the tab.

### Reset entries

Every entry has a **Resets** check box (`SetEntryReset(name, reset)` / `GetEntryReset(name)`
from code, thread-safe). At the end of every *execution loop*, an entry whose box is checked
gets back the value it had when that loop started:

* An execution loop is one run of the tree from the Root until the Root returns (succeeds or
  fails). With Loop Execution on, every repetition is a loop of its own, so each one starts
  from the same values. **Stop**, **Reset** (and a Restart node) also end the loop in
  progress; after Reset the tree starts over with a new loop. **Pause** does not end it.
* The reset happens when the Root returns, before the next loop starts and, in run-once mode,
  before `executionFinished` is emitted. Inside a loop, nodes see every change as usual.
* Every change counts, whoever made it: the nodes, worker threads, your code, or edits in the
  Blackboard tab while the tree runs. Edits made while the tree is idle are not undone: they
  are the values the next loop starts with.
* Checking the box during a loop resets the entry at the end of that loop; unchecking it keeps
  the entry's current value.
* An entry renamed during a loop is reset under its new name; one whose type changed gets its
  type back. An entry created during a loop is left alone (it had no value when the loop
  started), and an entry removed during a loop is not brought back.
* List, Dictionary and Set values are restored as copies; Object entries get the very object
  they held back.
* Resetting is a run-time change: it is written to the blackboard file but is not an unsaved
  change. "Restore blackboard values on Stop / Reset" (Configure) is independent: it restores
  every entry to its value when execution started, on Stop and Reset only.
* The Resets box is saved in the blackboard file (`"reset": true`).

## Execution semantics

| Button | Effect |
|---|---|
| Execute ▶ | Builds a py_trees tree from the nodes connected to the Root (reading the files of Sub-Tree nodes) and starts ticking it. Resumes when paused. While a sub-tree is shown, the tree itself is executed. |
| Pause ⏸ | Stops ticking. A worker-thread `OnRun` already in progress continues. |
| Stop ⏹ | Stops ticking, interrupts running nodes (`CancelRequested()` becomes true, `OnTerminate(…, READY)`), and resets every node to *Ready*. |
| Reset 🔄 | Resets every node to *Ready* without halting; a running tree starts over on its next tick. |
| Configure ⚙ | Opens the options dialog (below). |
| Loop Execution ☐ | Unchecked (the default): execution stops when the Root returns (succeeds or fails). Checked: each time the Root returns, the tree is executed again, starting on the next tick (the tick interval is the only pause). |

* **Loop Execution** keeps everything as it was when the Root returned: blackboard entries
  (except those with **Resets** checked, see [Reset entries](#reset-entries)), node fields and
  any other state of your nodes carry over into the next loop, as if execution never paused. Only the Status labels go back to *Ready* at the start of each loop, so they show
  the progress of the current loop. "Restore blackboard values on Stop / Reset" still applies
  only to Stop and Reset. `executionFinished` is not emitted while looping (except after a
  failed tick, see below). The check box can be
  switched while the tree runs: unchecking it lets the current loop finish and then stops;
  checking it keeps a running tree going. It is the same option as Configure → Execution
  (`TreeConfig.repeat`, `GetLoopExecution()` / `SetLoopExecution()`) and is saved with the tree.
* A **Sequence** runs its children left to right until one fails, and a **Selector** until one
  succeeds (`py_trees.composites.Sequence` / `Selector`). The Root is a Sequence by default;
  switch it to a Selector from its right-click menu.
* A **Negation** succeeds when its child fails and fails when its child succeeds
  (`py_trees.decorators.Inverter`); see [Built-in nodes](built-in-nodes.md#built-in-nodes).
* A **Sub-Tree** node runs the tree of its file as part of the running tree and succeeds or
  fails with that tree's Root (see [Sub-Tree](built-in-nodes.md#sub-tree)).
* **Memory** is on by default, and can be set per composite from the right-click menu. With
  memory, a composite resumes from its running child on the next tick instead of re-evaluating
  earlier children every tick (py_trees `memory=True`). With memory off, earlier children are
  re-run every tick, so they should be quick conditions.
* Status labels:
  * *Ready*: not run yet
  * *Running*
  * *Succeeded*
  * *Failed*

  A node keeps showing its last result until the tree is reset or executed again. Nodes that
  are not connected to the Root, Notes and Groups are ignored.
* An **execution loop** runs from the start of the tree until the Root returns; with Loop
  Execution every repetition is a loop of its own, and Stop and Reset end the loop in progress.
  At the end of every loop, blackboard entries with **Resets** checked get their loop-start
  value back (see [Reset entries](#reset-entries)).
* Blackboard changes made by running nodes are written to the blackboard file as they happen (see
  [The blackboard](#the-blackboard)). Other values written by running nodes (fields, titles)
  do not count as unsaved changes, but they are part of the tree: a later Save writes them.
  Enable "Restore blackboard values on Stop / Reset" to undo blackboard changes; the restored
  values are then written to the file. Changes your code makes on the GUI thread while the
  tree is idle, such as a `SetField` at start-up, count as edits.
* Execute, Pause, Stop and Reset called during a tick (from `OnRun`, a signal handler or a
  dialog) are carried out right after that tick. After a Stop or Reset, no more node code runs
  in that tick. Commands issued while another command is being carried out are queued.
* An exception in a node's `OnRun` / `OnStart` only fails that node. If the tick itself fails
  (an error inside py_trees), execution stops, the Root shows *Failed* with the error in its
  Status tooltip, and `executionFinished("Failed")` is emitted, also with Loop Execution on.
* Every node that runs is written to [the tree's log](#the-log) at the INFO level, and node
  errors at the ERROR level.

Configure options (saved with the tree):

| Option | Default | Meaning |
|---|---|---|
| Tick interval | 100 ms | Time between ticks (1–60000 ms). |
| Execution | Run once | *Run once* stops when the Root succeeds or fails; *Loop* executes the tree again each time. The same option as the Loop Execution check box. |
| Restore blackboard values on Stop / Reset | off | Takes a snapshot of the blackboard when execution starts and restores it on Stop, or on Reset (also after a completed run). Blackboard edits you make while the tree is idle discard the snapshot. |
| New Sequence / Selector nodes use memory | on | Default memory flag for new composites. |
| Log level | 4: INFO | The least important messages written to [the tree's log](#the-log): `0: None` (nothing), `1: ERROR`, `2: WARNING`, `3: DEBUG` or `4: INFO` (everything). |

## The log

Every tree has a log. Its messages have one of four levels; the numbers give their precedence
(lower numbers are more important):

| Level | Logged |
|---|---|
| `0: None` | No message has this level. As the log level it switches logging off; a Print Message node set to it prints nothing. |
| `1: ERROR` | A node failed with an error (an exception in `OnRun`, `OnStart` or `OnTerminate`, with its traceback, or a wrong return value), the tick failed, or the tree could not be executed. |
| `2: WARNING` | Problems: a tree or sub-tree file loaded with problems, values left out when saving, a blackboard file that cannot be written, a Negation without child or a Sub-Tree node that cannot run. |
| `3: DEBUG` | The default level of Print Message nodes. |
| `4: INFO` | The execution of every node (see below); execution started, paused, resumed, stopped, reset and finished; trees created, loaded and saved; zooming into sub-trees and back; groups promoted to sub-trees. |

* **The log level** (Configure → Log level, `TreeConfig.log_level`, `GetLogLevel()` /
  `SetLogLevel(level)`) is the least important level that is logged: a message whose number is
  higher is dropped, and `0: None` drops everything. The default, `4: INFO`, logs everything.
  It is saved with the tree.
* **The log file** holds every message that is logged, whatever its level. It is named
  `<tree name>_<month>-<day>-<year>_<hour>-<minute>-<second>.log`, for example
  `robot_11-09-2026_15-04-05.log`, with the time the application was started (when it
  imported `behavior_tree_widget`), and lies in the folder the application was started from
  (its working directory at that time). Set `BehaviorTreeWidget.LOG_FOLDER` to use another
  folder. `GetLogFilePath()` returns the file (None before a tree was created or loaded).
* The file follows the tree: New, Load and saving under another name switch to the new tree's
  file, so trees opened one after the other get files of their own, and opening a tree again
  appends to its file. Messages logged before the first tree go to the first file. The file is
  created with its first message, so a tree whose log level is `0: None` creates none. Lines
  look like this:

  ```text
  2026-11-09 15:04:07.412 INFO    Execution started
  2026-11-09 15:04:07.413 DEBUG   patrol started
  2026-11-09 15:04:07.413 INFO    Node 'Print Message' (Print Message) executed: Succeeded
  2026-11-09 15:04:07.414 INFO    Node 'Move To' (MoveTo) started: Running
  2026-11-09 15:04:07.918 INFO    Node 'Move To' (MoveTo) finished: Succeeded
  2026-11-09 15:04:07.919 INFO    Node 'patrol' > 'Check' (Evaluation) executed: Failed (the blackboard has no entry named 'x')
  2026-11-09 15:04:07.920 INFO    Node 'Root' (Root) finished: Failed
  2026-11-09 15:04:07.920 INFO    Execution finished: Failed
  ```

* **Node execution**: each node that runs is logged at INFO when it starts running
  (`started: Running`), when it finishes after running (`finished: Succeeded` / `Failed`),
  when it runs and finishes within one tick (`executed: Succeeded` / `Failed`), and when its
  parent interrupts it (`interrupted`). Composites, the Root, Negations and Sub-Tree nodes are
  logged like leaves. Nodes inside sub-trees are named after the Sub-Tree nodes leading to them
  (`'patrol' > 'Check'`). A failed node's error follows its result.
* **Signals**: every message that is logged is announced with the signal of its level,
  `errorLogged(str)`, `warningLogged(str)`, `debugLogged(str)` or `infoLogged(str)`, and with
  `messageLogged(int level, str message)`. They are emitted from the GUI thread's event loop,
  shortly after the message was logged (also for messages logged on worker threads).
* **Write your own messages** with `widget.Log(level, message)` (`level`: a `LogLevel`, its
  number or its name, such as `"warning"`), from any thread, for example in `OnRun`:
  `tree.Log(LogLevel.INFO, "arrived")`. It returns False when the message was dropped.
* **Logging never blocks**: `Log` hands the message to Python's `logging` (a `QueueHandler`),
  and one background thread (`QueueListener`) writes every log file, flushing it whenever it
  has written everything queued. `flush_logs(timeout=5.0)` waits until everything logged so
  far is in the files, for example before reading a file.
  `Shutdown()` closes the file once everything logged is written; later messages are dropped.
* The tree's messages are not passed to the application's own `logging` handlers (they do not
  propagate), so the node-by-node messages do not fill its console. The library's own warnings
  (logger `"behavior_tree_widget"`) still go there, as before. A problem the library reports to
  both (a node error, a file loaded with problems, a blackboard file that cannot be written) has
  the same text in both, for example `'Raise' (Raise): OnRun raised ValueError: boom` or
  `The tick failed: ...`.

```python
from behavior_tree_widget import LogLevel, flush_logs

widget.errorLogged.connect(lambda message: status_bar.showMessage(message))
widget.SetLogLevel(LogLevel.DEBUG)          # everything except the node-by-node INFO messages
widget.Log(LogLevel.WARNING, "battery low")
flush_logs()
print(open(widget.GetLogFilePath(), encoding="utf-8").read())
```
