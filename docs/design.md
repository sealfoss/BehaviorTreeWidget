# Notes and design decisions

* **Children order** is the left-to-right position on the canvas, as in most graphical behavior
  tree editors. The small numbers on the connection lines show it.
* **Loop Execution** is the existing `TreeConfig.repeat` option, so the check box and
  Configure → Execution can never disagree, and the choice is saved with the tree. A new loop
  starts on the tick after the Root returned rather than in the same tick: a tree whose nodes all
  finish at once would otherwise loop forever without returning control to Qt.
* **Copy and paste** use the system clipboard with the MIME type
  `application/x-behavior-tree-widget-nodes`, holding JSON in the format of a tree file's
  `nodes` and `connections`. Pasting therefore behaves like loading those nodes: the copies get
  new ids, values that cannot be written as JSON keep their defaults, and unknown node types
  become placeholders. Placement is exact: of all positions where the copied group (moved as a
  whole) keeps 20 pixels from every node, the one closest to the original position is used
  (ties go right, then down). On Qt platforms without a system clipboard (`offscreen`,
  `minimal`, ...), copied nodes are removed from the in-process clipboard at exit, because
  PySide6 can crash while destroying such a clipboard holding them.
* **Releasing a connection drag over the canvas** opens the same menu as a right click, and the
  chosen node is connected to the dragged label, as in most node editors. It is placed so that
  its own connection label lies where the line was released.
* **Negation** is built on `py_trees.decorators.Inverter`. It is not a `CompositeNodeWidget`
  (it has no Sequence/Selector type and no memory option); `MaxChildren()` limits it to one
  child.
* **Evaluation and Set** store their choices as fields, so they are saved, loaded, copied and
  pasted like any leaf node's fields. Their combo boxes refresh only when entries are added,
  removed, renamed, retyped or reordered, not when values change. Entry names are kept even
  while an entry does not exist, because a tree's nodes are created before its blackboard is
  loaded.
* **Notes** are nodes of the canvas (so selecting, moving, copying, deleting and saving work as
  for any node) without connections, which keeps them out of execution. They are stacked below
  the other nodes and the lines so they can never hide the tree. The font size is the largest
  one, found by a binary search over pixel sizes, at which Qt's word-wrapped layout of the whole
  text fits the note. A single click edits the text, as the request asked; dragging moves the
  note, and the edges and corners are resize handles. The text area starts beyond the handles,
  so a click on the text never resizes.
* **New built-in type names yield** to an application's own leaf types: 0.5.0 allowed any
  name except the built-in ones of that time, so an application may already have a `Notes`,
  `UDP Message`, `Sub-Tree`, `Group` or `Print Message` type. Reserving these names would break such
  applications and their trees. A saved node with one of these names is read as the built-in
  node only if its data has that node's form (a note's `text` / `width` / `height`, a group's
  `width` / `height` / `color`, or only the fields of the built-in leaf).
* **Two files per tree.** The tree file changes only when you save, while the blackboard
  changes all the time as the tree runs; in separate files, a blackboard change no longer
  rewrites the tree (see [Writing only the blackboard](#writing-only-the-blackboard)), and a
  tree file can be run as a sub-tree by other trees without bringing a blackboard along. The
  name you type is reduced to the tree name (`.json`, `_BT` and `_BB` removed), so any of the
  three names finds the tree. Files of earlier versions are read but never rewritten in the
  old form.
* **Reset entries** record, during each execution loop, the value an entry had just before
  its first change in that loop (a copy-on-first-write record kept by the blackboard store).
  At the end of the loop, the entries whose box is checked at that moment get those values
  back. Only changed entries are recorded, so the cost follows the changes, not the size of
  the blackboard, and an entry whose box is checked during a loop still gets its true
  loop-start value. A loop ends when the Root returns (and with Stop, Reset or a Restart node)
  rather than when the next loop begins, so the values between two loops, in the Blackboard
  tab and in the file, are the reset ones.
* **Sub-trees run inside the running tree.** The Sub-Tree node's file is read at each Execute
  and its nodes are created (not shown) and built into the py_trees tree under a decorator,
  instead of running a second `BehaviourTree` from the node. So the sub-tree is ticked with the
  tree, its leaves are cancelled by Stop at once like any other, its nodes use the one
  blackboard, and preemption by a parent works as for any subtree. A sub-tree that would
  contain itself is detected by its file (also through other sub-trees) and fails instead of
  building forever.
* **Groups move what lies completely inside them**, decided when the drag starts, rather than
  keeping a list of members: there is nothing to keep in sync when nodes are added, deleted,
  pasted or moved, and what moves is what you see inside the box. Only the title bar and the
  edges take clicks, so the nodes inside a group stay as easy to click, connect and select as
  without the group. The default name number is a counter of the widget (as the request
  asked); it only counts up, so a default name is never given twice in a session, even after
  groups are renamed or deleted.
* **Promote to Sub-Tree** keeps the tree's behaviour: the new Root takes the type and memory of
  the parent the group's top nodes hang from (a Selector parent gives a Selector Root, so
  `Selector(a, Selector(x, y), b)` runs like `Selector(a, x, y, b)`), and the Sub-Tree node
  takes their place among the parent's children. Groups whose nodes cannot be replaced by one
  node (children outside the group, or several parents outside it) are refused with the
  reason rather than changing what the tree does.
* **Zoom In shows a sub-tree in a view of its own**, stacked over the tree's view, instead of
  replacing the tree's nodes: the tree stays exactly as it was (unsaved changes, selection,
  statuses, the node objects your code holds) and can keep executing while you look into a
  sub-tree, and Go Back is instant. Each sub-tree shown is a separate document with its own
  file and unsaved-changes state, which is why Save saves the tree shown and every prompt names
  the files concerned. Executing runs a shown sub-tree from what is shown, so what you see is
  what runs, and the nodes it runs report their status to the nodes shown, matched by node id
  and by the Sub-Tree nodes zoomed through (the same file can be used by several Sub-Tree
  nodes; only the one zoomed into is shown).
* **The log** uses Python's `logging`: a `QueueHandler` per tree and one `QueueListener` thread
  writing every file with `FileHandler`s, so `Log` only puts a record on a queue and never waits
  for the disk. The per-level Qt signals are batched and emitted from the GUI thread's event
  loop, so a slot that updates a widget is always called on the GUI thread, also for messages
  of worker threads. The levels keep the numbers of the request (0 None to 4 INFO; INFO is the
  most detailed level here, unlike in Python's `logging`), and the log level is a tree option
  so the Configure dialog can change it. The file name uses the time the package was imported,
  as the closest point to "the moment the app was executed" that Python can know portably.
  Node executions are logged by a py_trees visitor that compares each node's status before and
  after the tick, so a node is logged when it starts, finishes or is interrupted, not on every
  tick it keeps running.
* **Print Message** writes through the same log, so its messages appear in the file in order
  with the node executions around them, and its level follows the same threshold.
* **Moving entries** changes the order of the blackboard, which is already the order of the
  blackboard file; so moves are saved as any other blackboard change, without a tree save.
  A move is an edit like any other: made while the tree is idle it is kept (and discards the
  snapshot of "Restore blackboard values on Stop / Reset"), made during a run it is undone
  with the rest of that run's changes when that option restores the snapshot.
* **UDP Message** resolves its address with `socket.getaddrinfo`, so host names and IPv6 work.
  IPv4 is preferred because a receiver bound to `127.0.0.1` would miss a message sent to
  `localhost` over IPv6.
* **Threading**: the tree is ticked on the GUI thread, following py_trees' guidance that
  `update()` must never block. Long-running leaf work runs on worker threads. The threads are
  daemon threads, so an `OnRun` that never returns cannot keep the application from exiting;
  since 0.9.0 they are reused (`WorkerPool`), and a pool grows only when all of its threads are
  busy, so a call that never returns occupies one thread, as before, and never delays another.
* **Blackboard** values are stored in py_trees' blackboard, with a namespace per widget so
  several widgets can coexist. They are guarded by a lock, so nodes can use them from any
  thread.
* The Designer files from the original project are used as they are, with two fixes:
  * The `ChildConnnections` label in `CompositeNode.ui` was renamed `ChildConnections`. The old
    name is still accepted in custom ui files.
  * Button tooltips that referred to "Video" were corrected.
* The Blackboard tab is `ui/BlackBoardView.ui`, whose top-level widget is named
  `BlackBoardEntries`. `BoolEntry.ui` is used for the Bool type; Dictionary and Set entries
  reuse `ListEntry.ui`.
* Under `pythonw.exe` (no console), `import py_trees` would fail because `sys.stdout` is
  `None`. In that case the package installs a harmless stdout/stderr sink before importing
  py_trees.

## Structure and performance (0.9.0)

Version 0.9.0 implemented the proposals of [improvements.md](../improvements.md), which also
records the measurements behind them.

* **One module per job.** `widget.py` ties the parts together; the node types (`registry.py`),
  loading and saving (`tree_io.py`, `document.py`), the zoom stack, Promote to Sub-Tree, copy
  and paste, the blackboard file, the canvas (view, items, gestures, menus), execution
  (executor, tree builder, behaviours) and the blackboard (a package) each live in a module of
  their own, and talk through small explicit interfaces instead of each other's private
  attributes. Names that tests or applications imported from the old modules are still
  importable from them.
* **Node kinds in one table.** Every built-in node type is described once, by a `NodeKind` in
  `registry.BUILTIN_KINDS`: its add-node menu section, how it is created, and how its saved
  data is told from that of an application's leaf type with the same name. Adding a built-in
  node is a change to that table.
* **Tree documents.** The tree itself, every sub-tree shown with Zoom In, and the hidden nodes
  that run a Sub-Tree node are `TreeDocument`s: a tree's nodes and links, its file, its options
  and whether it has unsaved changes. Saving, reading back what is shown and tracking unsaved
  changes are one code path for all of them; a `TreeView` shows a document.
* **Generated forms** replace parsing the `.ui` files at run time: creating a node builds its
  widgets from Python code compiled from its `.ui` file (`tools/compile_ui.py`), and all nodes
  share one `QUiLoader` for the files that are still parsed. The hash recorded with each form
  makes an edited but not recompiled `.ui` file fall back to `QUiLoader`, so it still works.
* **Field rows** are one grid of name boxes and editors. The name boxes stay read-only line
  edits, which look like the editors next to them and need no Python code to paint.
* **Nodes that list the blackboard** (Evaluation, Set, Print Message) change only the rows of
  their combo boxes that differ, and while a whole blackboard is replaced (`BlackboardStore.batch()`,
  used when a tree is loaded) they refresh once at the end. A model shared by all combo boxes
  was considered and not used: a combo box backed by a shared model changes its current row by
  itself when the chosen entry is removed, which would change the node's field; a removed entry
  must instead stay chosen and be shown as missing.
* **A tick refreshes what ticked.** The visitor that writes the execution log also records the
  behaviours a tick visited; only those, and the ones that were running before the tick, have
  their status shown again, so a tick costs what it runs, not what the tree holds.
* **The waits of one tick share a budget**, `TICK_WAIT_BUDGET` (0.1 s): the first worker-thread
  call a tick waits for gets its `THREAD_WAIT`, later ones only what is left of the budget. So a
  tick that starts many slow calls cannot keep the GUI waiting for the sum of their
  `THREAD_WAIT`s; calls that are not waited for finish on a later tick.
* **The canvas repaints what changed** (`SmartViewportUpdate` instead of repainting the whole
  view for every change), keeps each parent's children in order instead of sorting them at every
  question, and gives each connection line its hit area as a stored path, so finding what lies
  under the mouse calls no Python code per line. The whole order number of a line now selects
  the line, not only the part of it that overlaps the line's hit area.
* **Explicit methods instead of attribute magic.** Assigning `self._title` / `self._fields` used
  to update a constructed node through `__setattr__`; it still does, but `SetTitle` and
  `SetFields` are the way to do it, and the assignment is deprecated. The accessors of the
  underlying objects (`main_view()`, `blackboard_store()`, ...) use the snake_case of `view()`,
  `button()` and the `TreeView` methods; their camelCase names are deprecated.

## Writing only the blackboard

Blackboard changes are written to the blackboard file only, safely, as Save does: the file is
written to a temporary file that then replaces it, so an interrupted write (a crash, power
loss, a full disk) never leaves a damaged file. The tree file is not touched, so the cost of a
blackboard write depends on the size of the blackboard, not of the tree. Measured with 3000
nodes (a 0.9 MB tree file) and 20 entries on Windows with an SSD: Save takes about 55 ms, and
writing a blackboard change about 1.4 ms (about 10 ms in version 0.6.0, which rewrote the
whole tree file for every change).
