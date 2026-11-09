# Writing leaf nodes

Leaf nodes contain your application logic. Subclass `LeafNodeWidget`, override `OnRun` and
register the class with the widget.

```python
import time
from behavior_tree_widget import LeafNodeWidget, Status


class MoveTo(LeafNodeWidget):
    _title = "Move To"
    _fields = {
        "x": 0.0,                          # float -> QDoubleSpinBox
        "y": 0.0,
        "speed": 5,                        # int   -> QSpinBox
        "mode": ["walk", "run"],           # list  -> QComboBox (user picks one item)
        "avoid_obstacles": True,           # bool  -> QCheckBox
        "label": "target",                 # str   -> QLineEdit
    }

    def OnRun(self, tree):
        mode = self.GetFieldSelection("mode")          # item selected in the combo box
        for _ in range(100):
            if self.CancelRequested():                 # Stop / Reset was pressed
                return False
            time.sleep(0.01)
        tree.SetEntry([self.GetField("x"), self.GetField("y")], "position")
        return True
```

## Class attributes

| Attribute | Meaning |
|---|---|
| `_title` | Default title shown in the node's Title label. Defaults to the class name. Users can rename individual nodes (right-click → Rename…). |
| `_fields` | Dict of field name → default value. Supported types: `int` (a 32-bit spin box), `float` (a spin box with `FLOAT_DECIMALS`, default 6, decimals), `str`, `bool` and `list`. Values of other types are shown read-only. Each node gets its own deep copy of the dict. Fields are shown on the node and saved with the tree. A node without fields hides its fields frame. |
| `RUN_IN_THREAD` | `True` (default): `OnRun` runs on a worker thread, so long operations do not freeze the UI. `False`: `OnRun` runs on the GUI thread; use this for quick checks, or when `OnRun` touches Qt widgets. Worker threads are daemon threads that are reused from one call to the next; a call that never returns keeps one of them, and never blocks the application from exiting. |
| `THREAD_WAIT` | Seconds (default `0.02`) the tick waits for a worker-thread `OnRun` call it has just started, so quick calls finish within one tick. With `0` the node always shows *Running* first. The waits of one tick share a budget, `TreeExecutor.TICK_WAIT_BUDGET` (default `0.1` s): the first call a tick waits for gets its whole `THREAD_WAIT`, later calls only what is left of the budget. Calls that are not waited for show *Running* and finish on a later tick. |
| `TYPE_NAME` | Name used in saved files and for registration. Defaults to the class name and is not inherited by subclasses. It must be unique and must not be a built-in type name: `Root`, `Sequence`, `Selector`, `Negation`, `Evaluation`, `Set`, `Restart`, `Pause` or `Stop`. The names of the built-in types added in 0.6.0 to 0.8.0, `UDP Message`, `Notes`, `Sub-Tree`, `Group` and `Print Message`, are allowed, so applications that already used them keep working: your type then takes the name, and that built-in node is not offered by widgets your type is registered with. |
| `UI_FILE` | Optional custom Qt Designer file for the node: an absolute path, or a path relative to the module that defines the class. It must contain the named widgets `Title`, `Status` and `ParentConnection`, and may contain `FrameFields` + `Fields`: a plain widget that the field rows are put in (a name box and an editor per field, in a grid). In `.ui` files written for earlier versions, `Fields` is a `QListView`; such a list is replaced by a plain widget, so these files keep working. |

You can also assign `self._title` and `self._fields` in your class's `__init__`, before calling
`super().__init__()`: the node starts with the assigned values. Afterwards, change them with
`SetTitle`, `SetField` and `SetFields` (which replaces all fields at once). Assigning
`self._title` or `self._fields` after `super().__init__()` still updates the node, but is
deprecated since 0.9.0 and emits a `DeprecationWarning`.

## Methods you override

| Method | When it is called |
|---|---|
| `OnRun(self, tree)` **(required)** | Called when the node is ticked and none of its calls is in progress. With `RUN_IN_THREAD = False`, that is once per tick while the node runs. With `RUN_IN_THREAD = True`, it is called once on a worker thread; the node shows *Running* while later ticks wait for it. `tree` is the `BehaviorTreeWidget`. You may also declare `OnRun(self)` and use `self.GetTree()`. Return `True` / `Status.SUCCESS` (Succeeded), `False` / `Status.FAILURE` (Failed), or `Status.RUNNING` (not finished; `OnRun` is called again on the next tick). Raising an exception, or returning anything else (for example, forgetting `return`), makes the node fail. The error is logged and shown in the Status label's tooltip. |
| `OnStart(self, tree)` | Called on the GUI thread each time the node starts running, before its first `OnRun`. |
| `OnTerminate(self, tree, status)` | Called on the GUI thread once, when the node stops running. `status` is `NodeStatus.SUCCEEDED` or `FAILED` when it finished, or `NodeStatus.READY` when it was interrupted (Stop, Reset, preempted by its parent, or loading another tree). |

`OnStart` and `OnTerminate` must accept the `tree` argument; only `OnRun` may omit it.

With `RUN_IN_THREAD = True`, a node still in `OnRun` shows *Running*, the tree keeps ticking
and the UI stays responsive. A running call cannot be killed, so when the node is interrupted:

* `self.CancelRequested()` becomes `True` for that call. Check it regularly in long loops and
  return early. The result of an interrupted call is ignored.
* The call can no longer change the tree. `tree.SetEntry`, `self.SetField`, `self.SetFields`,
  `self.SetTitle`, assigning `self._title` / `self._fields` (deprecated), and the execution
  commands raise `behavior_tree_widget.ExecutionCancelled`.
* A node never runs two `OnRun` calls at once. If the node is started again while its
  interrupted call is still returning, it shows *Running* and waits for that call first.

## Thread-safe helpers available inside `OnRun`

| Call | Purpose |
|---|---|
| `tree.GetEntry(name)` / `tree.SetEntry(value, name)` | Read or write the blackboard. |
| `tree.HasEntry(name)` | Check whether an entry exists. |
| `tree.Stop()`, `tree.Reset()`, `tree.Execute()`, `tree.Pause()` | Control execution; carried out on the GUI thread. |
| `tree.Log(level, message)` | Write to [the tree's log](user-guide.md#the-log), e.g. `tree.Log(LogLevel.DEBUG, "found it")`; never blocks. |
| `self.GetField(key)` / `self.SetField(key, value)` | Read or change a field; the editor updates on the GUI thread. |
| `self.GetFields()` / `self.SetFields(fields)` | A copy of all fields / replace all fields with a dict (the node keeps a copy; selections of list fields that stay lists are kept). |
| `self.GetFieldSelection(key)` / `GetFieldSelectionIndex(key)` / `SetFieldSelectionIndex(key, index)` | Selected item / index of a list field, or select another item. The list in `_fields` stays the list of choices. |
| `self.CancelRequested()` | `True` once the running call should stop early. |
| `self.GetTitle()` / `self.SetTitle(text)` | Node title. |

In worker-thread nodes, do **not** touch Qt widgets directly; use the calls above.

## Registering node types

```python
from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget, register_node_type


class Beep(LeafNodeWidget):
    def OnRun(self, tree):
        return True


widget = BehaviorTreeWidget(node_types=[Beep])   # when creating the widget ...
widget.RegisterNodeType(Beep)                     # ... or later (registering again is allowed)


@register_node_type        # alternative: every BehaviorTreeWidget created AFTER this is offered Buzz
class Buzz(LeafNodeWidget):
    def OnRun(self, tree):
        return True
```

Registered types appear in the canvas right-click menu, and are needed to load trees that use
them. If a tree uses a node type that is not registered, loading it creates a placeholder node
marked "(unknown type)". The placeholder keeps all of the node's saved data and fails when
executed, so saving the tree again loses nothing.

## Threading rules

* `Execute`, `Pause`, `Stop`, `Reset`, `Log` and the blackboard methods (`GetEntry`, `SetEntry`,
  `MoveEntry`, …) may be called from any thread, for example inside a worker-thread `OnRun`. Commands are
  carried out on the GUI thread, in the order they were issued. A command that an `OnRun`
  call issues to its own tree is dropped if that run was stopped, reset or restarted before
  the command arrived, so it cannot affect a later run. Commands to another tree, or from
  other threads, are always carried out.
* The node getters and setters listed under "Thread-safe helpers" may be called from any thread.
* Structure, selection, clipboard, file and configuration methods (`AddNode`, `RemoveNode`,
  `Connect`, `Disconnect`, `SelectNodes`, `CopyNodes`, `PasteNodes`, `PromoteGroupToSubTree`,
  `ZoomIn`, `GoBack`, `SaveSubTree`, `NewTree`, `LoadTree`, `SaveTree`, `SetConfig`,
  `SetLogLevel`, `SetLoopExecution`, `Shutdown`) must be called on the GUI thread. Otherwise
  they raise `RuntimeError`.
* The tree is ticked on the GUI thread. py_trees is not thread-safe; only leaf `OnRun` calls
  run on worker threads. Those threads are reused, and each one is named after the node whose
  call it runs (`behavior_tree_widget:<title>`) while it runs it.
