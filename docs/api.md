# API reference

## `BehaviorTreeWidget(parent=None, node_types=())`

A `QTabWidget` with the tabs "Behavior Tree" and "Blackboard".

| Method | Description |
|---|---|
| `RegisterNodeType(cls)` | Register a leaf class. Raises `TypeError` / `ValueError` if the class is invalid or its type name is taken. |
| `GetNodeTypes()` | Every type that can be added: `"Sequence"`, `"Selector"`, `"Negation"`, `"Evaluation"`, `"Set"`, `"Restart"`, `"Pause"`, `"Stop"`, `"UDP Message"`, `"Print Message"`, `"Sub-Tree"`, then the registered leaf type names (sorted), then `"Notes"` and `"Group"`. |
| `GetNodeType(name)` | The leaf class of a type name (also the built-in `EvaluationNodeWidget`, `SetNodeWidget`, `RestartNodeWidget`, `PauseNodeWidget`, `StopNodeWidget`, `UdpMessageNodeWidget`, `PrintMessageNodeWidget` and `SubTreeNodeWidget`), or `None` for unknown names and for Root, Sequence, Selector, Negation, Notes and Group. |
| `NewTree(path=None)`, `LoadTree(path=None)`, `SaveTree(path=None)` | File operations (see [Tree files](file-formats.md#tree-files): `path` names the tree, stored in `<name>_BT.json` and `<name>_BB.json`). Without a path, a file dialog is shown (and `False` is returned if it is cancelled); errors are shown in message boxes. With a path, `LoadTree` raises `TreeFileError` (a `ValueError`), `NewTree` raises `OSError`, and `SaveTree` raises `OSError`, `ValueError` or `TypeError`. All three return `True` on success. `LoadTree`/`NewTree` called during a tick (e.g. from `OnRun`) check the files at once and replace the tree right after the tick. |
| `ConfirmDiscardChanges()` | Asks whether to save unsaved changes (of the tree and of the sub-trees shown with Zoom In). Returns `False` if the user cancelled. |
| `IsTreeLoaded()`, `GetFilePath()`, `GetBlackboardFilePath()`, `IsModified()` | Tree state: the tree file (`<name>_BT.json`; a file of an earlier version keeps its name until saved) and the blackboard file (`<name>_BB.json`). `IsModified()` is also `True` while a sub-tree shown with Zoom In has unsaved changes. |
| `GetRootNode()`, `GetNodes()` | Nodes of the tree shown (of the sub-tree while zoomed in). |
| `AddNode(node_type, x=0.0, y=0.0)` | Adds a node with its top-left corner at scene position (x, y) and returns it. `node_type` is one of the names returned by `GetNodeTypes()`, a leaf class or a node instance (except a Root); classes are registered automatically. Raises `ValueError` for unknown names (and `"Root"`) and `RuntimeError` while executing. |
| `RemoveNode(node)`, `Connect(parent, child)`, `Disconnect(child)` | Edit the structure. Raise `RuntimeError` while executing; `Connect` raises `ValueError` for invalid connections such as cycles. Connecting a second child to a Negation replaces its child. |
| `GetSelectedNodes()`, `SelectNodes(nodes, add=False)` | The selected nodes; select nodes (`add=True` keeps the current selection, `SelectNodes([])` deselects all). |
| `CopyNodes(nodes=None)` | Copies `nodes` (default: the selected nodes) and the connections between them to the clipboard, as Ctrl+C does. Returns `False` (and leaves the clipboard alone) when there is nothing to copy; the Root is never copied. |
| `PasteNodes()` | Pastes the clipboard's nodes in the free area nearest to where they were copied, as Ctrl+V does, selects them and returns them (`[]` when the clipboard holds no nodes). Raises `RuntimeError` while executing. |
| `PromoteGroupToSubTree(group, overwrite=False)` | Promote to Sub-Tree, as the group's menu does (see [Groups](built-in-nodes.md#groups)): writes the group's contents to `<group name>_BT.json` next to the tree file and replaces the group with a Sub-Tree node, which it returns. Raises `ValueError` (with the reason) when the group cannot be promoted, `FileExistsError` when the file exists and `overwrite` is False, `OSError` when it cannot be written, and `RuntimeError` while executing. |
| `ZoomIn(node, create=False)`, `GoBack(save=None)` | Show the tree of the Sub-Tree node `node` in place of the tree shown / the tree containing it again (see [Zooming into sub-trees](built-in-nodes.md#zooming-into-sub-trees)). The editing methods above (`GetRootNode` … `PromoteGroupToSubTree`) act on the tree shown; files, execution, options and the blackboard stay the tree's own. `ZoomIn` raises `ValueError` (not a Sub-Tree node of the tree shown, no file, a file already shown) or `TreeFileError` (missing or invalid file; `create=True` creates a missing one). `GoBack` returns `False` when no sub-tree is shown or the user cancelled; with unsaved changes, `save=True` saves them, `False` discards them, `None` asks. |
| `GetZoomDepth()`, `GetZoomPath()` | How many sub-trees deep the tree shown is (0: the tree itself), and the tree files from the tree to the one shown. |
| `SaveSubTree()` | Writes the sub-tree shown to its file (the Save button while zoomed in). Raises `RuntimeError` when no sub-tree is shown, and `OSError` when the file cannot be written. |
| `Execute()`, `Pause()`, `Stop()`, `Reset()`, `Configure()` | Execution control, the same as the buttons. |
| `GetExecutionState()` | `"Idle"`, `"Running"` or `"Paused"`. |
| `IsExecuting()` | `True` while running or paused. |
| `GetConfig()`, `SetConfig(config)` | Execution options (`TreeConfig`). `GetConfig()` returns a copy: `c = w.GetConfig(); c.repeat = True; w.SetConfig(c)`. |
| `GetLoopExecution()`, `SetLoopExecution(loop)` | The Loop Execution check box (`TreeConfig.repeat`). |
| `Log(level, message)`, `GetLogLevel()`, `SetLogLevel(level)`, `GetLogFilePath()` | The tree's log (see [The log](user-guide.md#the-log)). `Log` is thread-safe and never blocks; it returns `False` when the message is dropped by the log level. `SetLogLevel` changes `TreeConfig.log_level`. |
| `GetEntry(name)`, `SetEntry(value, name)`, `AddEntry(name, type_name, value=None, reset=False)`, `HasEntry(name)`, `RemoveEntry(name)`, `GetEntryNames()`, `GetEntryType(name)`, `GetEntryReset(name)`, `SetEntryReset(name, reset)`, `MoveEntry(name, offset)`, `GetBlackboardNamespace()` | Blackboard (see [The blackboard](user-guide.md#the-blackboard) and [Reset entries](user-guide.md#reset-entries)). |
| `Shutdown()` | Stops execution for good and releases the blackboard's py_trees keys. Afterwards the blackboard is empty and read-only (`SetEntry` raises `RuntimeError`) and the tree cannot be executed. Call it when your window closes for the last time; it is also called automatically when the application quits. |
| `view()`, `main_view()`, `button(name)`, `loop_execution_check_box()`, `blackboard_view()`, `blackboard_store()`, `tree_logger()`, `executor()` | The underlying objects, for customisation: the canvas of the tree shown (a `QGraphicsView`, e.g. `view().set_zoom(0.85)`; each sub-tree shown has its own), the canvas of the tree itself, a button by name (`"Execute"`, `"Pause"`, `"Stop"`, `"Reset"`, `"Configure"`, `"Save"`, `"Load"`, `"New"`, `"GoBack"`), the Loop Execution check box, the Blackboard tab (`BlackboardView`), the blackboard (`BlackboardStore`), the log (`TreeLogger`) and the executor (`TreeExecutor`). |

Until 0.8.0 five of these accessors had camelCase names: `mainView()`, `loopExecutionCheckBox()`,
`blackboardView()`, `blackboardStore()` and `treeLogger()`. Those names still work but are
deprecated since 0.9.0: they emit a `DeprecationWarning` naming the new method.

When the widget is itself a top-level window, closing it asks to save unsaved changes; it then
calls `Shutdown()` if `Qt.WidgetAttribute.WA_DeleteOnClose` is set, and `Stop()` otherwise.

| Signal | Emitted when |
|---|---|
| `treeLoaded(str path)` / `treeSaved(str path)` | A tree was created or loaded / saved (`path`: its tree file). |
| `blackboardSaved(str path)` | The blackboard was written to the blackboard file `path`. |
| `blackboardSaveFailed(str message)` | Blackboard changes could not be written to the blackboard file (retried on the next change). |
| `subTreePromoted(SubTreeNodeWidget, str path)` | A group was promoted to the sub-tree file `path` and replaced by the Sub-Tree node. |
| `subTreeSaved(str path)` | The sub-tree shown was saved to its file `path`. |
| `zoomChanged(int depth, str path)` | Zoom In / Go Back (or New / Load closing the sub-trees shown): `depth` sub-trees deep (0: the tree itself), showing the tree file `path`. |
| `modifiedChanged(bool)` | The unsaved-changes state (`IsModified()`) changed. |
| `executionStateChanged(str)` | The state becomes `"Idle"`, `"Running"` or `"Paused"`. |
| `executionFinished(str)` | A run-once execution finished: `"Succeeded"` or `"Failed"`. Also `"Failed"` when a tick fails, in either mode. Not emitted by Stop. |
| `nodeStatusChanged(NodeWidget, str)` | A node's status changed. |
| `nodeError(NodeWidget, str)` | A node failed because of an exception or an invalid return value. |
| `nodeSelectionChanged()` | The selected nodes changed (also when another tree is shown). |
| `errorLogged(str)`, `warningLogged(str)`, `debugLogged(str)`, `infoLogged(str)` | A message of that level was logged (see [The log](user-guide.md#the-log)). |
| `messageLogged(int level, str message)` | Any message was logged; `level` is its `LogLevel` number. |

## Node classes

* `NodeWidget` is the base class. Methods: `GetId()`, `GetTypeName()`, `GetTitle()`,
  `SetTitle()`, `GetStatus()` (a `NodeStatus`), `GetError()`, `GetParent()`, `GetChildren()`
  (in execution order), `SetParent()`, `AddChild()`, `RemoveChild()`, `GetTree()`,
  `GetFields()`, `SetFields()`, `GetField()`, `SetField()`, `GetFieldSelection()`,
  `GetFieldSelectionIndex()`, `SetFieldSelectionIndex()`, `CancelRequested()`, and the class
  methods `HasParentConnection()`, `HasChildConnections()` and `MaxChildren()` (`None` for any
  number, `1` for a Negation, `0` for a leaf). Signals: `changed(bool runtime)` for every change
  of saved data, `statusChanged(str)`, `titleChanged(str)`, `fieldChanged(str key)` for any
  change of a field or list selection, and `fieldEdited(str key)` for changes the user makes in
  the node's editors.
* `RootNodeWidget` and `CompositeNodeWidget` add `GetCompositeType()` /
  `SetCompositeType("Sequence"|"Selector")` and `GetMemory()` / `SetMemory(bool)`.
* `NegationNodeWidget` adds `GetChild()` (the child, or `None`).
* `EvaluationNodeWidget` and `SetNodeWidget` (leaf nodes) add `GetValueName()` /
  `SetValueName(name)`, `GetCompareTo()` / `SetCompareTo(name_or_None)`, `UsesLiteral()`,
  `GetLiteralValue()` / `SetLiteralValue(value)` and `GetEffectiveLiteralValue()` (the literal
  converted to the type of the Value Name entry, as it is used). They are thread-safe.
* `RestartNodeWidget`, `PauseNodeWidget` and `StopNodeWidget` are the built-in execution
  control leaf nodes (see [Restart, Pause and Stop](built-in-nodes.md#restart-pause-and-stop)).
* `UdpMessageNodeWidget` is the built-in UDP Message leaf node (see [UDP Message](built-in-nodes.md#udp-message)).
  Its fields are `"Destination IP"`, `"Destination Port"` and `"Message"`.
* `PrintMessageNodeWidget` is the built-in Print Message leaf node (see
  [Print Message](built-in-nodes.md#print-message)). It adds `GetLevel()` / `SetLevel(level)` (a `LogLevel`),
  `GetSource()` / `SetSource(name_or_None)`, `UsesLiteral()`, `GetMessage()` /
  `SetMessage(text)` (thread-safe) and `message_text(tree)` (what it prints now).
* `SubTreeNodeWidget` is the built-in Sub-Tree leaf node (see [Sub-Tree](built-in-nodes.md#sub-tree)). It adds
  `GetFile()` / `SetFile(path)` (the `"File"` field shown in its line edit; `SetFile` is
  thread-safe), `GetResolvedFile()` (the absolute path that is run, or `None`) and `browse()`
  (the `...` button).
* `NotesNodeWidget` is the Notes node (see [Notes](built-in-nodes.md#notes)). It adds `GetText()` /
  `SetText(text)`, `GetNoteSize()` / `SetNoteSize(width, height)` (rounded and limited to
  `MIN_SIZE` … `MAX_SIZE`; `TypeError` / `ValueError` for values that are not finite numbers),
  `IsEditing()`, and the signal `textChanged(str)`. `GetText`, `SetText` and `SetNoteSize` are
  thread-safe. A note has no connections: `HasParentConnection()` and `HasChildConnections()`
  are `False`.
* `GroupNodeWidget` is the Group node (see [Groups](built-in-nodes.md#groups)). Its name is its title. It adds
  `GetGroupSize()` / `SetGroupSize(width, height)` (as for notes, 120 × 60 to 2000 × 2000),
  `GetBorderColor()` (`"#rrggbb"`) / `SetBorderColor(color)` (a `QColor` or a colour name;
  `ValueError` for invalid colours), `GetContainedNodes()` and the signal `colorChanged(str)`.
  `SetGroupSize` and `SetBorderColor` are thread-safe. A group has no connections.
* `BoxNodeWidget` is the base class of `NotesNodeWidget` and `GroupNodeWidget`, the resizable
  boxes (`GetBoxSize()` / `SetBoxSize(width, height)`).
* `LeafNodeWidget`: subclass it (see [Writing leaf nodes](writing-nodes.md)). For tests and
  customisation, `field_editor(key)` returns the editor of a field and `field_key(key)` the
  read-only box showing its name.
* `NodeStatus`: `READY`, `RUNNING`, `SUCCEEDED`, `FAILED` (values `"Ready"`, …).
* `Status` is a re-export of `py_trees.common.Status`, for `OnRun` return values.
* `ExecutionCancelled` is raised when an interrupted `OnRun` call tries to change the tree.
* `TreeConfig` holds the execution options: `tick_interval_ms`, `repeat` (Loop Execution),
  `restore_blackboard`, `default_memory`, `log_level`.
* `LogLevel` is the level of a log message: `NONE` (0), `ERROR` (1), `WARNING` (2), `DEBUG` (3),
  `INFO` (4); `LogLevel.coerce(value)` accepts a level, its number or its name.
  `flush_logs(timeout=5.0)` waits until every log message is written to its file; `TreeLogger`
  is the class of `tree_logger()`.
* `TreeFileError` is raised by `LoadTree(path)` for invalid files.
* `tree_file_paths(path)` returns `(tree file, blackboard file)` of the tree that `path` names,
  for example `("C:/trees/robot_BT.json", "C:/trees/robot_BB.json")` for `"C:/trees/robot.json"`.
* `UnknownLeafNodeWidget` is the placeholder for nodes of unregistered types (see
  [Registering node types](writing-nodes.md#registering-node-types)).
* `register_node_type(cls)` registers a leaf class with every `BehaviorTreeWidget` created
  afterwards; `registered_node_types()` returns those classes by type name.
* `BlackboardStore` (the thread-safe blackboard behind `GetEntry` / `SetEntry`) and
  `BlackboardView` (the Blackboard tab) are exported for customisation. Besides the methods
  behind the widget's blackboard API, the store offers `revision(saved=False)` (a number that
  grows with every change; with `saved=True` only with changes that can alter the blackboard
  file, so changes of Object entries do not count), `batch()` (a context manager grouping bulk
  changes: listeners can check `in_batch()` and redraw once on the signal `batchFinished`),
  `snapshot()` / `restore(snapshot)`, `to_list()` / `load_list(items, replace=False)` /
  `parse_list(items)` (the entries in the form of the blackboard file), `is_capturing()` (an
  execution loop is recording loop-start values) and `client()` (its `py_trees.blackboard.Client`).
* `TreeExecutor.TICK_WAIT_BUDGET` (seconds, default `0.1`) bounds how long one tick waits for
  worker-thread `OnRun` calls (see `THREAD_WAIT` in [Writing leaf nodes](writing-nodes.md#class-attributes));
  set it on the class, or on one widget's executor (`widget.executor().TICK_WAIT_BUDGET = 0.05`).
