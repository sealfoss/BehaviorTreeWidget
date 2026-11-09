# behavior-tree-widget

A PySide6 widget for **building and executing [py_trees](https://py-trees.readthedocs.io/en/devel/)
behavior trees graphically**. Drop the `BehaviorTreeWidget` into your Qt application and write
your own leaf nodes in Python. Users can then draw the tree, edit node fields, run it with
py_trees, and inspect or modify the blackboard.

* **Behavior Tree tab**: a pannable, zoomable canvas with a Root node, Sequence, Selector and
  Negation nodes, the built-in Evaluation and Set blackboard nodes, the Restart, Pause and Stop
  execution nodes, the UDP Message node, the Print Message node (writes to the tree's log), the
  Sub-Tree node (runs another tree file), and your custom leaf nodes.
  You draw connections with the mouse, select nodes, and copy and paste them. Resizable Notes
  document parts of the tree, and resizable, named Groups hold parts of it: a group moves with
  everything inside it and can be promoted to a sub-tree file. **Zoom In** on a Sub-Tree node
  shows and edits its tree in place of yours, and **Go Back** returns.
* **Blackboard tab**: named Integer, Double, String, Bool, List, Dictionary and Set entries that
  nodes read and write (`GetEntry` / `SetEntry`). An entry with **Resets** checked gets its
  loop-start value back at the end of every execution loop. Arrows move entries up and down
  the list.
* **Execution**: Execute, Pause, Stop, Reset and Configure buttons and a Loop Execution check
  box. Every node shows *Ready*, *Running*, *Succeeded* or *Failed*. Execution uses
  `py_trees.composites.Sequence` / `Selector`, `py_trees.decorators.Inverter` and a
  `py_trees.trees.BehaviourTree`, ticked by a Qt timer.
* **Log**: every tree writes a log file (`<name>_<month>-<day>-<year>_<hour>-<minute>-<second>.log`)
  with messages at the levels ERROR, WARNING, DEBUG and INFO, including the execution of every
  node, and emits a Qt signal for each message.
* **Files**: New, Save and Load trees. A tree is stored in two files: `<name>_BT.json` (the
  tree: nodes, positions, fields, connections and options) and `<name>_BB.json` (its
  blackboard), which is updated whenever the blackboard changes.

---

## Contents

1. [Installation](#installation)
2. [Quick start](#quick-start)
3. [Writing leaf nodes](#writing-leaf-nodes)
4. [Built-in nodes](#built-in-nodes)
5. [The blackboard](#the-blackboard)
6. [Using the editor](#using-the-editor)
7. [Execution semantics](#execution-semantics)
8. [The log](#the-log)
9. [Tree files](#tree-files)
10. [API reference](#api-reference)
11. [Threading rules](#threading-rules)
12. [Building, testing and project layout](#building-testing-and-project-layout)
13. [Notes and design decisions](#notes-and-design-decisions)

---

## Installation

Requirements: Python ≥ 3.10, PySide6 ≥ 6.5 and py_trees ≥ 2.2.1. pip installs these
automatically.

```bash
pip install behavior_tree_widget-0.8.0-py3-none-any.whl
```

The wheel is in the `dist/` folder of this repository. See
[Building](#building-testing-and-project-layout) to create it yourself.

To use the widget in another project, install the wheel into that project's environment. To
declare it as a dependency, point to the wheel file or to a folder of wheels:

```text
# requirements.txt
behavior-tree-widget @ file:///C:/path/to/behavior_tree_widget-0.8.0-py3-none-any.whl
```

```toml
# pyproject.toml of your project
[project]
dependencies = ["behavior-tree-widget>=0.8"]
# install with:  pip install --find-links C:/path/to/wheels -e .
```

Then `import behavior_tree_widget` (see [Quick start](#quick-start)).

Try the bundled demo, which registers a few example nodes:

```bash
behavior-tree-widget-demo               # GUI launcher (no console window on Windows)
python -m behavior_tree_widget          # same, but shows the demo's log output in the console
python -m behavior_tree_widget my_tree_BT.json   # open a tree directly
```

## Quick start

```python
import sys
from PySide6.QtWidgets import QApplication, QMainWindow
from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget


class SayHello(LeafNodeWidget):
    _title = "Say Hello"                      # text of the node's Title label
    _fields = {"name": "world", "times": 1}   # editable fields shown on the node

    def OnRun(self, tree):
        for _ in range(self.GetField("times")):
            print(f"Hello {self.GetField('name')}!")
        tree.SetEntry(self.GetField("name"), "greeted")   # write the blackboard
        return True                                        # True = Succeeded, False = Failed


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.tree = BehaviorTreeWidget(node_types=[SayHello])
        self.setCentralWidget(self.tree)

    def closeEvent(self, event):
        if not self.tree.ConfirmDiscardChanges():   # offers to save unsaved changes
            event.ignore()
            return
        self.tree.Shutdown()                         # stops execution, releases the blackboard
        super().closeEvent(event)


app = QApplication(sys.argv)
window = MainWindow()
window.show()
sys.exit(app.exec())
```

To use it:

1. Click **New** to create a tree (its files `<name>_BT.json` and `<name>_BB.json`). The
   editor stays disabled until a tree is created or loaded.
2. Right-click the canvas to add nodes.
3. Drag from a node's **Children** label to another node's **Parent** label to connect them, or
   release the line on empty canvas to add a new node connected to it.
4. Click **Execute**.

A fuller example, which also builds a tree from code, is in
[`examples/custom_app.py`](examples/custom_app.py).

## Writing leaf nodes

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

### Class attributes

| Attribute | Meaning |
|---|---|
| `_title` | Default title shown in the node's Title label. Defaults to the class name. Users can rename individual nodes (right-click → Rename…). |
| `_fields` | Dict of field name → default value. Supported types: `int` (a 32-bit spin box), `float` (a spin box with `FLOAT_DECIMALS`, default 6, decimals), `str`, `bool` and `list`. Values of other types are shown read-only. Each node gets its own deep copy of the dict. Fields are shown on the node and saved with the tree. A node without fields hides its fields frame. |
| `RUN_IN_THREAD` | `True` (default): `OnRun` runs on a worker thread, so long operations do not freeze the UI. `False`: `OnRun` runs on the GUI thread; use this for quick checks, or when `OnRun` touches Qt widgets. |
| `THREAD_WAIT` | Seconds (default `0.02`) the tick waits for a worker-thread `OnRun` call it has just started, so quick calls finish within one tick. With `0` the node always shows *Running* first. |
| `TYPE_NAME` | Name used in saved files and for registration. Defaults to the class name and is not inherited by subclasses. It must be unique and must not be a built-in type name: `Root`, `Sequence`, `Selector`, `Negation`, `Evaluation`, `Set`, `Restart`, `Pause` or `Stop`. The names of the built-in types added in 0.6.0 to 0.8.0, `UDP Message`, `Notes`, `Sub-Tree`, `Group` and `Print Message`, are allowed, so applications that already used them keep working: your type then takes the name, and that built-in node is not offered by widgets your type is registered with. |
| `UI_FILE` | Optional custom Qt Designer file for the node: an absolute path, or a path relative to the module that defines the class. It must contain the named widgets `Title`, `Status` and `ParentConnection`, and may contain `FrameFields` + `Fields` (QListView). |

You can also assign `_title` and `_fields` in your class's `__init__`, before or after calling
`super().__init__()`, and the node shows the assigned values. At run time use `SetTitle` and
`SetField`.

### Methods you override

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
* The call can no longer change the tree. `tree.SetEntry`, `self.SetField`, `self.SetTitle`,
  assigning `self._title` / `self._fields`, and the execution commands raise
  `behavior_tree_widget.ExecutionCancelled`.
* A node never runs two `OnRun` calls at once. If the node is started again while its
  interrupted call is still returning, it shows *Running* and waits for that call first.

### Thread-safe helpers available inside `OnRun`

| Call | Purpose |
|---|---|
| `tree.GetEntry(name)` / `tree.SetEntry(value, name)` | Read or write the blackboard. |
| `tree.HasEntry(name)` | Check whether an entry exists. |
| `tree.Stop()`, `tree.Reset()`, `tree.Execute()`, `tree.Pause()` | Control execution; carried out on the GUI thread. |
| `tree.Log(level, message)` | Write to [the tree's log](#the-log), e.g. `tree.Log(LogLevel.DEBUG, "found it")`; never blocks. |
| `self.GetField(key)` / `self.SetField(key, value)` | Read or change a field; the editor updates on the GUI thread. |
| `self.GetFieldSelection(key)` / `GetFieldSelectionIndex(key)` / `SetFieldSelectionIndex(key, index)` | Selected item / index of a list field, or select another item. The list in `_fields` stays the list of choices. |
| `self.CancelRequested()` | `True` once the running call should stop early. |
| `self.GetTitle()` / `self.SetTitle(text)` | Node title. |

In worker-thread nodes, do **not** touch Qt widgets directly; use the calls above.

### Registering node types

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

## Built-in nodes

Besides the Root, Sequence and Selector, every `BehaviorTreeWidget` offers nine built-in node
types in its right-click menu, and Notes and Groups. They need no registration.

### Negation

A node with a **Parent** label and a single **Child** label that inverts the result of its child
(`py_trees.decorators.Inverter`):

| Child | Negation |
|---|---|
| *Succeeded* | *Failed* |
| *Failed* | *Succeeded* |
| *Running* | *Running* |

A Negation has one child. Connecting another child replaces the current one, just as connecting
a node that already has a parent replaces its old parent. A Negation without a child fails when
it is executed, and its Status tooltip says why.

### Evaluation and Set

Leaf nodes that check or change blackboard values without any code. Both show three editors:

| Editor | Purpose |
|---|---|
| **Value Name** (combo box `ValueName`) | The blackboard entry the node works on. Lists every entry of the blackboard. |
| **Compare To** (Evaluation) / **Set To** (Set) (combo box `CompareTo`) | *Literal*, or another entry of the same type as the Value Name entry. |
| **Literal Value** (`LiteralValue`) | Shown and enabled only while *Literal* is chosen. Its editor follows the type of the Value Name entry: a spin box for Integer, a double spin box for Double, a line edit for String, a check box for Bool, and a line edit taking a Python literal (`[1, 2]`, `{'a': 1}`, `{1, 2}`) for List, Dictionary and Set. |

* **Evaluation** succeeds when the Value Name entry equals the Compare To entry (or the literal)
  and fails otherwise. Values are compared with `==`, except that a Bool never equals a number.
* **Set** sets the Value Name entry to the value of the Set To entry (or to the literal) and
  succeeds.
* Both run on the GUI thread (`RUN_IN_THREAD = False`). They fail with an error (Status tooltip,
  `nodeError` signal) when no entry is chosen, when an entry they need does not exist, or when
  the Value Name entry is an Object entry and *Literal* is chosen (Object entries can only be
  compared with, or set to, another entry).
* The combo boxes follow the blackboard: new entries appear, and renamed entries are followed.
  A chosen entry that disappears stays chosen and is shown in red as "(missing)", so removing and
  re-adding an entry, or loading a tree, loses nothing.
* Choosing a Value Name of another type resets Compare To to *Literal* and converts the literal
  where possible (for example, an Integer literal `5` becomes `5.0` for a Double entry and `"5"`
  for a String entry).
* The choices are the node's fields and are saved with the tree: `ValueName` (`""` until an entry
  is chosen), `CompareTo` (an entry name, or `null` for *Literal*) and `LiteralValue`. The
  literal is used with the type of the Value Name entry.

From code:

```python
check = widget.AddNode("Evaluation", 0, 200)
check.SetValueName("battery_low")   # the entry to evaluate
check.SetCompareTo(None)            # None = Literal; or the name of another entry
check.SetLiteralValue(True)

setter = widget.AddNode("Set", 300, 200)
setter.SetValueName("mode")
setter.SetLiteralValue("patrol")
```

### Restart, Pause and Stop

Leaf nodes that control the execution of their own tree when they are reached, like the buttons:

| Node | When it is ticked |
|---|---|
| **Restart** | Starts the tree over: every node goes back to *Ready* (and the blackboard is restored if "Restore blackboard values on Stop / Reset" is on), and the next tick starts at the Root again. Execution keeps running; this is what Reset does while the tree runs. |
| **Pause** | Pauses the tree, like a breakpoint. The node shows *Running* while the tree is paused there; click **Execute** to continue: the node then succeeds and the tree goes on with the next node. |
| **Stop** | Stops the tree, as the Stop button does: execution ends and every node goes back to *Ready*. `executionFinished` is not emitted. |

Each one returns *Running* to the tree when it issues its command, so no node after it runs in
that tick. Only the Pause node's Status label shows *Running* (while the tree is paused there):
Restart and Stop take effect before the labels of that tick are refreshed.
A tree that reaches a Restart node on every run never ends by itself (like Loop Execution);
press Stop to end it.

### UDP Message

A leaf node that sends a UDP message each time it runs. Its fields:

| Field | Type | Default | Meaning |
|---|---|---|---|
| **Destination IP** | string | `127.0.0.1` | An IPv4 or IPv6 address, or a host name. Spaces around it are ignored. |
| **Destination Port** | integer | `5005` | 1 to 65535. |
| **Message** | string | empty | Sent as one datagram, encoded as UTF-8. An empty message sends an empty datagram. |

* The node succeeds once the datagram has been sent. UDP does not report whether it arrived.
* It fails, with the reason in its Status tooltip and the `nodeError` signal, when Destination
  IP is empty or cannot be resolved, the port is outside 1 to 65535, a field holds a value of
  the wrong type (set from code), or the system refuses to send the message (for example, one
  longer than a datagram can hold, or a network that cannot be reached).
* Broadcast addresses such as `255.255.255.255` are allowed. For a host name with both IPv4
  and IPv6 addresses, such as `localhost`, the IPv4 address is used.
* `OnRun` runs on a worker thread, so a slow host name lookup does not freeze the window. If
  the tree is stopped during the lookup, nothing is sent.

```python
udp = widget.AddNode("UDP Message", 0, 200)
udp.SetField("Destination IP", "192.168.1.20")
udp.SetField("Destination Port", 9000)
udp.SetField("Message", "patrol started")
```

### Print Message

A leaf node that writes a message to [the tree's log](#the-log) each time it runs. It shows
three editors, like the Evaluation and Set nodes:

| Editor | Purpose |
|---|---|
| **Level** (combo box `LogLevel`) | The level the message is logged at: `0: None`, `1: ERROR`, `2: WARNING`, `3: DEBUG` (the default) or `4: INFO`. `0: None` prints nothing. |
| **Print** (combo box `Source`) | *Literal* prints the Message; a blackboard entry prints `<entry name> = <value>`. Lists every entry of the blackboard. |
| **Message** (line edit `Message`) | The text printed while *Literal* is chosen (shown only then). |

* An entry's value is printed as it is for a String entry, and as a Python literal otherwise
  (`battery = 80`, `path = [1, 2]`, `name = robo`).
* The node succeeds once the message is handed to the log, also when the tree's log level
  drops it (for example DEBUG messages while the log level is WARNING). It fails with an error
  (Status tooltip, `nodeError` signal) when the chosen entry does not exist.
* As in the Evaluation and Set nodes, renamed entries are followed, and a chosen entry that
  disappears stays chosen and is shown in red as "(missing)".
* It runs on the GUI thread and never waits for the log file to be written.
* The choices are the node's fields and are saved with the tree: `Level` (0 to 4), `Source`
  (an entry name, or `null` for *Literal*) and `Message`.

```python
from behavior_tree_widget import LogLevel

hello = widget.AddNode("Print Message", 0, 200)
hello.SetMessage("patrol started")          # printed at DEBUG, the default level
battery = widget.AddNode("Print Message", 300, 200)
battery.SetSource("battery")                # prints "battery = 80"
battery.SetLevel(LogLevel.WARNING)          # or 2, or "warning"
```

### Sub-Tree

A leaf node that runs a complete behavior tree stored in another file, and succeeds or fails
with it. Use it to reuse a tree in several places, or to keep a large tree readable (see also
[Promote to Sub-Tree](#groups)).

* Its **File** line edit holds the name of the tree file to run, `<name>_BT.json`. Type it, or
  click **...** to choose the file. A relative name is relative to the folder of the tree
  file the node belongs to; the browse button stores files of that folder (or below it) as
  relative names, so trees and their sub-trees can move together. A name that does not exist
  as typed is also looked for as `<name>_BT.json`, so `patrol` finds `patrol_BT.json`. The
  tooltip of the line edit shows the full path.
* **Execution**: each time the tree is executed (Execute while idle), the file is read and its
  tree (the Root and everything connected below it) is built behind the scenes as part of the
  running tree: the Sub-Tree node runs while that tree runs, and succeeds when its Root
  succeeds and fails when its Root fails. Its nodes are not shown (but see
  [Zooming into sub-trees](#zooming-into-sub-trees)); they are created for the run and deleted
  afterwards, and they run exactly as they would in their own file (their
  order is the left-to-right order of the file, they run on worker threads when their class
  says so, and Stop cancels them). As in the tree itself, a node never runs two `OnRun` calls
  at once: after Stop and Execute, a sub-tree node waits for its interrupted call to return.
* **One blackboard**: the sub-tree has no blackboard of its own. Its nodes read and write the
  blackboard of the tree that runs it; a `_BB.json` next to the sub-tree file is not loaded.
  Pause, Stop and Restart nodes in the sub-tree act on the tree that runs it.
* The options of the sub-tree file (tick interval, Loop Execution, ...) are not used: it runs
  at the pace of the tree that runs it, once each time the Sub-Tree node runs.
* The node fails, with the reason in its Status tooltip and the `nodeError` signal, when no
  file is chosen, the file cannot be read or is not a tree file, or the sub-tree contains
  itself (directly, or through the sub-trees it runs). When a node inside the sub-tree fails
  with an error, the Sub-Tree node's tooltip names that node and its error.
* Sub-trees can run sub-trees; the names in a sub-tree file are relative to that file's folder.
  The same sub-tree can be used by several Sub-Tree nodes.
* Node types the sub-tree uses must be registered with the widget that runs it (unknown types
  become placeholders, which fail).
* **Zoom In** (right-click the node): shows and edits the sub-tree in place of the tree; see
  [Zooming into sub-trees](#zooming-into-sub-trees).

```python
sub = widget.AddNode("Sub-Tree", 0, 200)
sub.SetFile("patrol_BT.json")          # relative to the folder of the tree file
sub.GetFile(), sub.GetResolvedFile()   # "patrol_BT.json", "C:/.../patrol_BT.json"
widget.Connect(widget.GetRootNode(), sub)
```

#### Zooming into sub-trees

Right-click a Sub-Tree node and choose **Zoom In** to show the sub-tree's graph in place of the
tree. The sub-tree is a tree like any other: add, connect, move and delete nodes, and edit
their fields. Zoom into a Sub-Tree node inside it to go one level deeper, and so on. The
**Go Back** button (next to New) shows the tree containing the sub-tree again, one level at a
time; it is enabled only while a sub-tree is shown. The window title shows the path, for
example `Behavior Tree - robot_BT.json > patrol_BT.json > dock_BT.json`.

* **Save** while a sub-tree is shown writes the sub-tree to its own file, without asking for a
  file name (the Sub-Tree node keeps running that file). No `_BB.json` is written for it; a
  single-file tree of an earlier version keeps its `"blackboard"` section unchanged. The tree
  itself cannot be saved under the name of a sub-tree shown (`SaveTree` raises `ValueError`).
* **Go Back** with unsaved changes asks whether to save them first (Save, Discard or Cancel).
  New, Load and closing the window ask about every tree with unsaved changes: the tree and the
  sub-trees shown. A tree loaded or created replaces all of them.
* **One blackboard**: the Blackboard tab stays the tree's own. Nodes of the sub-tree shown read
  and write it, entries added while zoomed in belong to the tree, and blackboard changes are
  written to the tree's `_BB.json` as usual.
* **Execution** always runs the tree itself (Execute, Pause, Stop, Reset and the options are
  the tree's). A sub-tree that is shown runs as it is shown, unsaved changes included, and its
  nodes show their status while the Sub-Tree node it was zoomed into from runs them. As in the
  tree, the structure of a sub-tree cannot be changed while the tree executes; zooming in and
  going back can.
* A missing file: **Zoom In** offers to create it as a new tree holding only a Root
  (`<name>_BT.json` for the name in the File box), so sub-trees can be written top-down. A
  Sub-Tree node without a file cannot be zoomed into, and neither can a file that is already
  shown (the tree's own file or a file of a tree containing the sub-tree).
* Relative file names of Sub-Tree nodes in the sub-tree are relative to the sub-tree's folder,
  and a group promoted there is written next to the sub-tree's file.

```python
widget.ZoomIn(sub)                  # show patrol_BT.json (create=True creates a missing file)
widget.GetZoomDepth()               # 1
widget.GetNodes()                   # the nodes of the sub-tree shown
widget.AddNode("Wait", 0, 200)      # added to the sub-tree
widget.SaveSubTree()                # write patrol_BT.json
widget.GoBack()                     # the tree again (save=True/False decides about unsaved changes)
```

### Notes

A Notes node documents a part of the tree. It is a box of text with a visible border, and has
no other function: it has no connections, is never executed, and has no status.

* **Add one**: right-click the canvas and choose **Notes**, next to last in the menu. A new
  note is empty and shows "Click to write a note".
* **Edit the text**: click the text (without dragging), or right-click the note → **Edit Text**.
  An editor opens over the text. Changes apply as you type, and Enter starts a new line. Press
  **Esc**, or click anywhere else, to finish editing. A click on a note also selects it, so
  after Esc, **Delete** deletes the note.
* **Resize**: drag an edge or a corner. The mouse cursor changes to a resize arrow over them.
  The edges you do not drag stay in place. A note is 60 × 30 to 2000 × 2000 pixels.
* **Text size**: the text is word-wrapped and drawn with the largest font with which all of it
  fits in the note. It grows as you enlarge the note and shrinks as you type more. Words are
  never split, so a long word also limits the size.
* Drag a note to move it. Notes can be selected, copied, pasted and deleted like other nodes.
  While editing, Ctrl+A / Ctrl+C / Ctrl+V and Delete act on the text.
* Notes are drawn **below** the other nodes and the connection lines (and above groups), so a
  large note can serve as a labelled panel behind a group of nodes. A click on a node lying on a
  note goes to the node.
* Notes can be resized and edited while the tree executes, but, like other nodes, not added or
  deleted.

From code (`NotesNodeWidget`):

```python
note = widget.AddNode("Notes", 400, 200)
note.SetText("Charging logic:\nretries 3 times, then reports an error")
note.SetNoteSize(320, 160)          # width, height
note.GetText(), note.GetNoteSize()
```

### Groups

A Group is a named box that holds a part of the tree. Like a note, it is not part of the
behavior tree: it has no connections, is never executed and has no status.

* **Add one**: right-click the canvas and choose **Group**, the last entry of the menu.
* **Name**: the line edit at the top left of the group holds its name (the node's title).
  New groups are named `group_001`, `group_002`, ...: the number is a counter of the
  `BehaviorTreeWidget` that counts up with each group given a default name (padded with
  zeros to three digits). It starts after the highest default-named group of a tree that
  is loaded, and again at `group_001` after New. A pasted copy of a default-named group gets
  the next number. Click the name to edit it; Enter or a click elsewhere finishes, Esc
  restores the previous name, and an empty name is not kept. **Rename...** in the menu works
  as well.
* **Move**: drag the title bar (the strip holding the name). Every node, note and group lying
  **completely** inside the group moves with it, connections included. A node that only
  overlaps the group stays where it is. Dragging a node that lies inside the group moves only
  that node; drop a node into the group to make it move with the group from then on.
  When the group is selected together with other nodes, all of them move together.
* **Resize**: drag an edge or a corner, as for notes. Resizing never moves the nodes inside.
  A group is 120 × 60 to 2000 × 2000 pixels.
* **Clicks inside a group go through it**: only the title bar and the edges belong to the
  group. A click or a drag inside it acts on what lies below: a node, or the canvas (a
  selection rectangle, or the add-node menu on a right-click). Groups are drawn below
  everything else, notes included.
* **Selection**: click the title bar to select the group. A selection rectangle selects a
  group when it touches its title bar, so a rectangle drawn inside a group selects only the
  nodes. Right-click the title bar → **Select Contents** selects the group and everything
  inside it (e.g. to copy, paste or delete all of it). **Delete** deletes only the group.
* **Border Color**: right-click the title bar → **Border Color** offers Blue (the default),
  Green, Red, Orange, Purple, Teal and Gray, and **Custom...** for any colour.
* **Promote to Sub-Tree**: right-click the title bar → **Promote to Sub-Tree** writes the
  group's contents to the tree file `<group name>_BT.json`, in the folder of the current
  tree file, and replaces the group and its contents with a [Sub-Tree](#sub-tree) node
  running that file:
  * The file gets a new Root whose children are the group's *top nodes*: the nodes inside
    the group whose parent lies outside it, left to right. When they have a parent outside
    the group, the Root takes that parent's type (Sequence or Selector) and memory, so the
    tree runs as before, and the Sub-Tree node is connected to that parent where the
    leftmost of them was. Nodes in the group without a parent stay unconnected in the file
    then; when none of the top nodes has a parent, all of them become children of the Root.
  * Notes and groups inside the group are written to the file too. No blackboard file is
    written: a sub-tree uses the blackboard of the tree that runs it.
  * The Sub-Tree node is titled with the group name and selected. The tree counts as
    modified (Save it to keep the change); the sub-tree file is written at once. If the file
    exists, you are asked whether to replace it.
  * A group cannot be promoted, and you are told why, when no node lies completely inside
    it, when the Root lies inside it, when a node inside it has children outside it, when
    its top nodes have different parents outside it (a Sub-Tree node has one parent), when a
    child of that parent outside the group runs between them (one node cannot keep that
    order), when
    its name cannot be a file name (it must not contain `< > : " / \ | ? *` or end with a
    dot), when the name is the name of the current tree, or while the tree executes.
* Groups can be moved, resized, renamed and recoloured while the tree executes, but, like
  other nodes, not added, deleted or promoted.

From code (`GroupNodeWidget`):

```python
group = widget.AddNode("Group", -300, 150)    # named group_001, group_002, ...
group.SetTitle("patrol")                      # the group name
group.SetGroupSize(600, 400)                  # width, height
group.SetBorderColor("#2e9d4f")               # a colour name or a QColor
group.GetContainedNodes()                     # the nodes lying completely inside it
sub = widget.PromoteGroupToSubTree(group)     # writes patrol_BT.json, returns the Sub-Tree node
```

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
  [Tree files](#tree-files)). Whenever an entry is added, removed, renamed, retyped, moved,
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
    [Writing only the blackboard](#writing-only-the-blackboard).
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

From code (see [Threading rules](#threading-rules)):

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

## Using the editor

| Action | How |
|---|---|
| Add a node | Right-click empty canvas (or the inside of a group) and choose Sequence, Selector, Negation, Evaluation, Set, Restart, Pause, Stop, UDP Message, Print Message, Sub-Tree, one of your leaf types (sorted by title), Notes or Group. The node is placed at the click position. |
| Add a connected node | Drag from a **Children** or **Parent** label and release over empty canvas (or a line). The Add Node menu opens there, as for a right click. The node you choose is placed where you released and connected to the label you dragged from: below a Children label, or above a Parent label (replacing an old parent). A node that cannot be connected that way, such as a leaf as a parent, is added unconnected. Closing the menu adds nothing. |
| Delete / rename a node | Select nodes and press **Delete** (or Backspace), or right-click a node → Delete / Rename…. The Root cannot be deleted; pressing Delete with the Root selected deletes its connections instead. Notes have **Edit Text** instead of Rename…; a group is also renamed in its title bar. |
| Edit or resize a note | Click its text to edit it; drag an edge or a corner to resize it (see [Notes](#notes)). |
| Groups | Drag a group's title bar to move it with everything inside it; drag an edge or a corner to resize it; right-click its title bar for Rename…, Select Contents, Border Color and Promote to Sub-Tree (see [Groups](#groups)). Clicks inside a group go to what lies below it. |
| Change a composite | Right-click a Sequence, Selector or the Root → Sequence or Selector, and Memory on/off. |
| Zoom into a sub-tree | Right-click a Sub-Tree node → **Zoom In** shows its tree in place of this one, to look at or edit; **Go Back** returns (see [Zooming into sub-trees](#zooming-into-sub-trees)). |
| Connect | Press on a **Children** label (a Negation's **Child** label) and drag to another node's **Parent** label, or the other way round. While you drag, the line is red, and turns green over a compatible label. Release there to connect; the line turns blue. Releasing over a node cancels; releasing over empty canvas opens the Add Node menu (see above). Connecting a node that already has a parent replaces its old connection, and so does connecting a second child to a Negation. Cycles are not allowed. |
| Select / delete a connection | Click the line; it turns green. Press **Delete** (or Backspace) to delete it. Clicking anywhere else deselects it. Right-click a line → Delete Connection also works. |
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
  (`py_trees.decorators.Inverter`); see [Built-in nodes](#built-in-nodes).
* A **Sub-Tree** node runs the tree of its file as part of the running tree and succeeds or
  fails with that tree's Root (see [Sub-Tree](#sub-tree)).
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
  (logger `"behavior_tree_widget"`) still go there, as before.

```python
from behavior_tree_widget import LogLevel, flush_logs

widget.errorLogged.connect(lambda message: status_bar.showMessage(message))
widget.SetLogLevel(LogLevel.DEBUG)          # everything except the node-by-node INFO messages
widget.Log(LogLevel.WARNING, "battery low")
flush_logs()
print(open(widget.GetLogFilePath(), encoding="utf-8").read())
```

## Tree files

A tree is stored in two files named after the tree, in the same folder:

| File | Holds | Written |
|---|---|---|
| `<tree name>_BT.json` | the behavior tree: options, view, nodes (positions, fields, notes, groups) and connections | by New and Save |
| `<tree name>_BB.json` | the blackboard: its entries, their values and Resets boxes (in list order) | by New and Save, and at every change of the blackboard |

The file name you choose names the tree: `robot`, `robot.json`, `robot_BT.json` and
`robot_BB.json` all mean the tree `robot`, stored in `robot_BT.json` and `robot_BB.json`
(`tree_file_paths(path)` returns both paths). `GetFilePath()` is the tree file and
`GetBlackboardFilePath()` the blackboard file.

* **New** asks for a file name and creates a tree that contains only the Root, with default
  options, and an empty blackboard file (replacing an old one of that name).
* **Save** writes the current tree and its blackboard to the files of the name you choose. When
  you type a name without `_BT` and its tree file exists, you are asked whether to replace it.
  A file is replaced only once the new content is complete, so a failed save never leaves a
  damaged file. If the tree file was written but the blackboard file could not be, you are told
  and the tree stays marked as modified.
* **Load** opens a tree file (`*_BT.json`); its blackboard is read from the `_BB.json` file next
  to it. Choosing the `_BB.json` file loads its tree as well. A tree file without blackboard
  file, such as a sub-tree file, loads with an empty blackboard.
* A sub-tree file written by [Promote to Sub-Tree](#groups) is an ordinary tree file without
  blackboard file; it can be opened as a tree of its own.
* A sub-tree shown with [Zoom In](#zooming-into-sub-trees) is saved to its own file (Save while
  it is shown), keeping that file's options; no blackboard file is written for it.

After New, Load or Save, the widget sets the title of its top-level window (`widget.window()`)
to `Behavior Tree - <file name>` (the tree file; while sub-trees are shown, their files follow:
`robot_BT.json > patrol_BT.json`). To change the text, set
`BehaviorTreeWidget.WINDOW_TITLE_FORMAT`. The buttons ask you to save unsaved changes before
New or Load replace the tree. The methods called with a path (`NewTree(path)`, `LoadTree(path)`)
do not ask. Use `if widget.ConfirmDiscardChanges(): widget.LoadTree(path)` for the same
behaviour.

A tree file (`<name>_BT.json`) is JSON:

```json
{
  "format": "behavior_tree_widget",
  "version": 1,
  "config": {"tick_interval_ms": 100, "repeat": false, "restore_blackboard": false, "default_memory": true},
  "view": {"zoom": 1.0, "center": [0.0, 200.0]},
  "nodes": [
    {"id": "4f0c…", "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": true},
    {"id": "9a1d…", "type": "MoveTo", "title": "Move To", "x": -40.0, "y": 180.0,
     "fields": {"x": 1.5, "y": 0.0, "speed": 5, "mode": ["walk", "run"], "avoid_obstacles": true, "label": "target"},
     "field_selections": {"mode": 1}},
    {"id": "77b2…", "type": "Negation", "title": "Negation", "x": 300.0, "y": 180.0},
    {"id": "e1c4…", "type": "Evaluation", "title": "Evaluation", "x": 260.0, "y": 340.0,
     "fields": {"ValueName": "counter", "CompareTo": null, "LiteralValue": 3}},
    {"id": "5b8e…", "type": "UDP Message", "title": "UDP Message", "x": -300.0, "y": 180.0,
     "fields": {"Destination IP": "127.0.0.1", "Destination Port": 5005, "Message": "started"}},
    {"id": "c03a…", "type": "Notes", "title": "Notes", "x": 520.0, "y": 0.0,
     "text": "Patrol loop:\nchecks the counter", "width": 220, "height": 120},
    {"id": "d7a0…", "type": "Group", "title": "group_001", "x": -340.0, "y": 140.0,
     "width": 600, "height": 400, "color": "#3d7cc9"},
    {"id": "b912…", "type": "Sub-Tree", "title": "patrol", "x": 600.0, "y": 180.0,
     "fields": {"File": "patrol_BT.json"}}
  ],
  "connections": [
    {"parent": "4f0c…", "child": "9a1d…"},
    {"parent": "4f0c…", "child": "77b2…"},
    {"parent": "77b2…", "child": "e1c4…"},
    {"parent": "4f0c…", "child": "b912…"}
  ]
}
```

`config.repeat` is the Loop Execution check box. `config.log_level` (0 to 4) is written only when
it is not 4, the default. A Notes node stores its `text`, `width` and
`height` (in pixels), and a Group its name (`title`), `width`, `height` and border `color`;
they never appear in `connections`. A Sub-Tree node stores its file name in the field `File`.

The blackboard file (`<name>_BB.json`) is JSON too. The entries are in the order of the
Blackboard tab, and `"reset": true` marks entries whose Resets box is checked:

```json
{
  "format": "behavior_tree_widget_blackboard",
  "version": 1,
  "blackboard": [
    {"name": "counter", "type": "Integer", "value": 3, "reset": true},
    {"name": "mode", "type": "String", "value": "patrol"}
  ]
}
```

**Compatibility**
* Version 0.6.0 and earlier stored the blackboard in the tree file itself (a `"blackboard"`
  list in a single `.json` file). Such files load as they are: the tree from the file, the
  blackboard from its `"blackboard"` list. They keep their name (`robot.json`) until the tree
  is saved, which writes `robot_BT.json` and `robot_BB.json`; the old file is never changed.
  If `robot_BT.json` already exists (e.g. an old file opened again after it was saved), saving
  from the close prompt asks whether to replace it.
  Blackboard changes made before that go to `robot_BB.json`, which from then on is the
  blackboard of `robot.json` too (a blackboard file takes precedence over a `"blackboard"`
  list).
* The tree file format version is still 1. Version 0.6.0 opens a `_BT.json` file with an empty
  blackboard and shows Group and Sub-Tree nodes as "(unknown type)" placeholders that keep
  their data. Versions before 0.8.0 show Print Message nodes as placeholders that keep their
  data, and ignore `config.log_level`.
* Version 0.5.0 opens files that contain UDP Message or Notes nodes: it shows them as
  "(unknown type)" placeholders and keeps their data, so saving there loses nothing and this
  version shows them again.
* A file in which an application saved leaf nodes of its own named `Notes`, `UDP Message`,
  `Sub-Tree`, `Group` or `Print Message` loads them into that application's types when they are
  registered.
  When they are not, they become placeholders that keep their data, as before; they are never
  read as the built-in nodes.

**Fields**
* Saved field values override the class defaults for fields the class still declares. A saved
  `int` may load into a `float` field. Saved fields the class no longer declares are ignored.
* A saved value of another type, other than int for a float field, is ignored and a warning is
  logged.
* A damaged value keeps the default, and you are told.
* For list fields, the saved list (the choices) replaces the class default. Choices added to the
  class later do not appear in older files.

**Blackboard**
* The blackboard belongs to the tree:
  * **Load** replaces the blackboard with the blackboard file's entries, in the file's order.
  * **New** starts with an empty blackboard.
  * Only Object entries survive both. Create your application's other entries after New/Load,
    for example in a `treeLoaded` handler.
  * Entries added before the first New/Load are discarded.
  * Every change of the blackboard is written to the blackboard file at once (see
    [The blackboard](#the-blackboard)).
  * Sub-trees have no blackboard of their own: they use the blackboard of the tree that runs
    them.
* Values that cannot be written as JSON are skipped when saving, and the Save button tells you
  which. This applies to blackboard entries and to node fields. When the blackboard is written
  automatically, each such entry is logged once. Object entries are skipped silently. Values
  that cannot be written include:
  * unsupported types;
  * circular references;
  * containers nested more than 256 levels deep;
  * integers longer than Python's `sys.get_int_max_str_digits()` digits.
* Tuples, sets, frozensets and dicts with non-string keys are stored with small tags such as
  `{"__set__": [...]}`, so they load back unchanged.
* `nan` and `inf` are written as `NaN` and `Infinity`, as Python's `json` module does.

**Loading**
* The tree file and the blackboard file are validated completely before the current tree is
  replaced. A tree whose files cannot be loaded (including a blackboard file that is not valid
  JSON or not a blackboard file) leaves the current tree untouched.
* Problems in otherwise valid files are reported and skipped: unknown node types become
  placeholders; invalid connections (including a second child of a Negation) and invalid
  blackboard entries (and Reset values other than `true` / `false`) are dropped; nodes outside
  the drawing area are moved inside it.

## API reference

### `BehaviorTreeWidget(parent=None, node_types=())`

A `QTabWidget` with the tabs "Behavior Tree" and "Blackboard".

| Method | Description |
|---|---|
| `RegisterNodeType(cls)` | Register a leaf class. Raises `TypeError` / `ValueError` if the class is invalid or its type name is taken. |
| `GetNodeTypes()` | Every type that can be added: `"Sequence"`, `"Selector"`, `"Negation"`, `"Evaluation"`, `"Set"`, `"Restart"`, `"Pause"`, `"Stop"`, `"UDP Message"`, `"Print Message"`, `"Sub-Tree"`, then the registered leaf type names (sorted), then `"Notes"` and `"Group"`. |
| `GetNodeType(name)` | The leaf class of a type name (also the built-in `EvaluationNodeWidget`, `SetNodeWidget`, `RestartNodeWidget`, `PauseNodeWidget`, `StopNodeWidget`, `UdpMessageNodeWidget`, `PrintMessageNodeWidget` and `SubTreeNodeWidget`), or `None` for unknown names and for Root, Sequence, Selector, Negation, Notes and Group. |
| `NewTree(path=None)`, `LoadTree(path=None)`, `SaveTree(path=None)` | File operations (see [Tree files](#tree-files): `path` names the tree, stored in `<name>_BT.json` and `<name>_BB.json`). Without a path, a file dialog is shown (and `False` is returned if it is cancelled); errors are shown in message boxes. With a path, `LoadTree` raises `TreeFileError` (a `ValueError`), `NewTree` raises `OSError`, and `SaveTree` raises `OSError`, `ValueError` or `TypeError`. All three return `True` on success. `LoadTree`/`NewTree` called during a tick (e.g. from `OnRun`) check the files at once and replace the tree right after the tick. |
| `ConfirmDiscardChanges()` | Asks whether to save unsaved changes (of the tree and of the sub-trees shown with Zoom In). Returns `False` if the user cancelled. |
| `IsTreeLoaded()`, `GetFilePath()`, `GetBlackboardFilePath()`, `IsModified()` | Tree state: the tree file (`<name>_BT.json`; a file of an earlier version keeps its name until saved) and the blackboard file (`<name>_BB.json`). `IsModified()` is also `True` while a sub-tree shown with Zoom In has unsaved changes. |
| `GetRootNode()`, `GetNodes()` | Nodes of the tree shown (of the sub-tree while zoomed in). |
| `AddNode(node_type, x=0.0, y=0.0)` | Adds a node with its top-left corner at scene position (x, y) and returns it. `node_type` is one of the names returned by `GetNodeTypes()`, a leaf class or a node instance (except a Root); classes are registered automatically. Raises `ValueError` for unknown names (and `"Root"`) and `RuntimeError` while executing. |
| `RemoveNode(node)`, `Connect(parent, child)`, `Disconnect(child)` | Edit the structure. Raise `RuntimeError` while executing; `Connect` raises `ValueError` for invalid connections such as cycles. Connecting a second child to a Negation replaces its child. |
| `GetSelectedNodes()`, `SelectNodes(nodes, add=False)` | The selected nodes; select nodes (`add=True` keeps the current selection, `SelectNodes([])` deselects all). |
| `CopyNodes(nodes=None)` | Copies `nodes` (default: the selected nodes) and the connections between them to the clipboard, as Ctrl+C does. Returns `False` (and leaves the clipboard alone) when there is nothing to copy; the Root is never copied. |
| `PasteNodes()` | Pastes the clipboard's nodes in the free area nearest to where they were copied, as Ctrl+V does, selects them and returns them (`[]` when the clipboard holds no nodes). Raises `RuntimeError` while executing. |
| `PromoteGroupToSubTree(group, overwrite=False)` | Promote to Sub-Tree, as the group's menu does (see [Groups](#groups)): writes the group's contents to `<group name>_BT.json` next to the tree file and replaces the group with a Sub-Tree node, which it returns. Raises `ValueError` (with the reason) when the group cannot be promoted, `FileExistsError` when the file exists and `overwrite` is False, `OSError` when it cannot be written, and `RuntimeError` while executing. |
| `ZoomIn(node, create=False)`, `GoBack(save=None)` | Show the tree of the Sub-Tree node `node` in place of the tree shown / the tree containing it again (see [Zooming into sub-trees](#zooming-into-sub-trees)). The editing methods above (`GetRootNode` … `PromoteGroupToSubTree`) act on the tree shown; files, execution, options and the blackboard stay the tree's own. `ZoomIn` raises `ValueError` (not a Sub-Tree node of the tree shown, no file, a file already shown) or `TreeFileError` (missing or invalid file; `create=True` creates a missing one). `GoBack` returns `False` when no sub-tree is shown or the user cancelled; with unsaved changes, `save=True` saves them, `False` discards them, `None` asks. |
| `GetZoomDepth()`, `GetZoomPath()` | How many sub-trees deep the tree shown is (0: the tree itself), and the tree files from the tree to the one shown. |
| `SaveSubTree()` | Writes the sub-tree shown to its file (the Save button while zoomed in). Raises `RuntimeError` when no sub-tree is shown, and `OSError` when the file cannot be written. |
| `Execute()`, `Pause()`, `Stop()`, `Reset()`, `Configure()` | Execution control, the same as the buttons. |
| `GetExecutionState()` | `"Idle"`, `"Running"` or `"Paused"`. |
| `IsExecuting()` | `True` while running or paused. |
| `GetConfig()`, `SetConfig(config)` | Execution options (`TreeConfig`). `GetConfig()` returns a copy: `c = w.GetConfig(); c.repeat = True; w.SetConfig(c)`. |
| `GetLoopExecution()`, `SetLoopExecution(loop)` | The Loop Execution check box (`TreeConfig.repeat`). |
| `Log(level, message)`, `GetLogLevel()`, `SetLogLevel(level)`, `GetLogFilePath()` | The tree's log (see [The log](#the-log)). `Log` is thread-safe and never blocks; it returns `False` when the message is dropped by the log level. `SetLogLevel` changes `TreeConfig.log_level`. |
| `GetEntry(name)`, `SetEntry(value, name)`, `AddEntry(name, type_name, value=None, reset=False)`, `HasEntry(name)`, `RemoveEntry(name)`, `GetEntryNames()`, `GetEntryType(name)`, `GetEntryReset(name)`, `SetEntryReset(name, reset)`, `MoveEntry(name, offset)`, `GetBlackboardNamespace()` | Blackboard (see [The blackboard](#the-blackboard) and [Reset entries](#reset-entries)). |
| `Shutdown()` | Stops execution for good and releases the blackboard's py_trees keys. Afterwards the blackboard is empty and read-only (`SetEntry` raises `RuntimeError`) and the tree cannot be executed. Call it when your window closes for the last time; it is also called automatically when the application quits. |
| `view()`, `mainView()`, `button(name)`, `loopExecutionCheckBox()`, `blackboardView()`, `blackboardStore()`, `treeLogger()` | The underlying Qt objects, for customisation: the canvas of the tree shown (a `QGraphicsView`, e.g. `view().set_zoom(0.85)`; each sub-tree shown has its own), the canvas of the tree itself, a button by name (`"Execute"`, `"Pause"`, `"Stop"`, `"Reset"`, `"Configure"`, `"Save"`, `"Load"`, `"New"`, `"GoBack"`), the Loop Execution check box, the Blackboard tab (`BlackboardView`), the blackboard (`BlackboardStore`) and the log (`TreeLogger`). |

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
| `errorLogged(str)`, `warningLogged(str)`, `debugLogged(str)`, `infoLogged(str)` | A message of that level was logged (see [The log](#the-log)). |
| `messageLogged(int level, str message)` | Any message was logged; `level` is its `LogLevel` number. |

### Node classes

* `NodeWidget` is the base class. Methods: `GetId()`, `GetTypeName()`, `GetTitle()`,
  `SetTitle()`, `GetStatus()` (a `NodeStatus`), `GetError()`, `GetParent()`, `GetChildren()`
  (in execution order), `SetParent()`, `AddChild()`, `RemoveChild()`, `GetTree()`,
  `GetFields()`, `GetField()`, `SetField()`, `GetFieldSelection()`,
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
  control leaf nodes (see [Restart, Pause and Stop](#restart-pause-and-stop)).
* `UdpMessageNodeWidget` is the built-in UDP Message leaf node (see [UDP Message](#udp-message)).
  Its fields are `"Destination IP"`, `"Destination Port"` and `"Message"`.
* `PrintMessageNodeWidget` is the built-in Print Message leaf node (see
  [Print Message](#print-message)). It adds `GetLevel()` / `SetLevel(level)` (a `LogLevel`),
  `GetSource()` / `SetSource(name_or_None)`, `UsesLiteral()`, `GetMessage()` /
  `SetMessage(text)` (thread-safe) and `message_text(tree)` (what it prints now).
* `SubTreeNodeWidget` is the built-in Sub-Tree leaf node (see [Sub-Tree](#sub-tree)). It adds
  `GetFile()` / `SetFile(path)` (the `"File"` field shown in its line edit; `SetFile` is
  thread-safe), `GetResolvedFile()` (the absolute path that is run, or `None`) and `browse()`
  (the `...` button).
* `NotesNodeWidget` is the Notes node (see [Notes](#notes)). It adds `GetText()` /
  `SetText(text)`, `GetNoteSize()` / `SetNoteSize(width, height)` (rounded and limited to
  `MIN_SIZE` … `MAX_SIZE`; `TypeError` / `ValueError` for values that are not finite numbers),
  `IsEditing()`, and the signal `textChanged(str)`. `GetText`, `SetText` and `SetNoteSize` are
  thread-safe. A note has no connections: `HasParentConnection()` and `HasChildConnections()`
  are `False`.
* `GroupNodeWidget` is the Group node (see [Groups](#groups)). Its name is its title. It adds
  `GetGroupSize()` / `SetGroupSize(width, height)` (as for notes, 120 × 60 to 2000 × 2000),
  `GetBorderColor()` (`"#rrggbb"`) / `SetBorderColor(color)` (a `QColor` or a colour name;
  `ValueError` for invalid colours), `GetContainedNodes()` and the signal `colorChanged(str)`.
  `SetGroupSize` and `SetBorderColor` are thread-safe. A group has no connections.
* `BoxNodeWidget` is the base class of `NotesNodeWidget` and `GroupNodeWidget`, the resizable
  boxes (`GetBoxSize()` / `SetBoxSize(width, height)`).
* `LeafNodeWidget`: subclass it (see above).
* `NodeStatus`: `READY`, `RUNNING`, `SUCCEEDED`, `FAILED` (values `"Ready"`, …).
* `Status` is a re-export of `py_trees.common.Status`, for `OnRun` return values.
* `ExecutionCancelled` is raised when an interrupted `OnRun` call tries to change the tree.
* `TreeConfig` holds the execution options: `tick_interval_ms`, `repeat` (Loop Execution),
  `restore_blackboard`, `default_memory`, `log_level`.
* `LogLevel` is the level of a log message: `NONE` (0), `ERROR` (1), `WARNING` (2), `DEBUG` (3),
  `INFO` (4); `LogLevel.coerce(value)` accepts a level, its number or its name.
  `flush_logs(timeout=5.0)` waits until every log message is written to its file; `TreeLogger`
  is the class of `treeLogger()`.
* `TreeFileError` is raised by `LoadTree(path)` for invalid files.
* `tree_file_paths(path)` returns `(tree file, blackboard file)` of the tree that `path` names,
  for example `("C:/trees/robot_BT.json", "C:/trees/robot_BB.json")` for `"C:/trees/robot.json"`.
* `UnknownLeafNodeWidget` is the placeholder for nodes of unregistered types (see
  [Registering node types](#registering-node-types)).
* `register_node_type(cls)` registers a leaf class with every `BehaviorTreeWidget` created
  afterwards; `registered_node_types()` returns those classes by type name.
* `BlackboardStore` (the thread-safe blackboard behind `GetEntry` / `SetEntry`) and
  `BlackboardView` (the Blackboard tab) are exported for customisation.

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
  run on worker threads.

## Building, testing and project layout

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"      # Windows (use .venv/bin/pip elsewhere)
.venv/Scripts/python -m pytest              # runs the test suite offscreen
.venv/Scripts/pip install build
.venv/Scripts/python -m build               # -> dist/behavior_tree_widget-0.8.0-py3-none-any.whl
```

```
pyproject.toml
src/behavior_tree_widget/
    __init__.py        public API
    widget.py          BehaviorTreeWidget (tabs, buttons, files, zooming into sub-trees)
    canvas.py          graphics view: nodes, connections, mouse gestures, context menus
    nodes.py           NodeWidget, RootNodeWidget, CompositeNodeWidget, NegationNodeWidget, LeafNodeWidget
    blackboard_nodes.py  the built-in Evaluation and Set leaf nodes
    control_nodes.py   the built-in Restart, Pause and Stop leaf nodes
    network_nodes.py   the built-in UDP Message leaf node
    print_node.py      the built-in Print Message leaf node
    subtree.py         the built-in Sub-Tree leaf node (SubTreeNodeWidget)
    tree_log.py        the tree's log: LogLevel, TreeLogger, the log files and their writer thread
    notes.py           the Notes node (NotesNodeWidget) and the resizable-box base (BoxNodeWidget)
    groups.py          the Group node (GroupNodeWidget)
    execution.py       py_trees adapter and ticking
    blackboard.py      blackboard store (py_trees backed) and the Blackboard tab
    serialization.py   JSON file formats (tree file and blackboard file, their names)
    config.py          TreeConfig and the Configure dialog
    demo.py            demo application and example nodes (__main__.py runs it)
    _runctx.py         cancellation tokens of running nodes (internal)
    _ui.py             loading of the packaged .ui files (internal)
    ui/*.ui            Qt Designer files (editable with pyside6-designer), including
                       NegationNode.ui, EvaluationNode.ui, SetNode.ui, PrintNode.ui,
                       NotesNode.ui, SubTreeNode.ui and GroupNode.ui
tests/                 pytest + pytest-qt test suite (tests/data: tree files of earlier versions)
examples/              example application
```

## Notes and design decisions

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
  `update()` must never block. Long-running leaf work runs on worker threads.
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

### Writing only the blackboard

Blackboard changes are written to the blackboard file only, safely, as Save does: the file is
written to a temporary file that then replaces it, so an interrupted write (a crash, power
loss, a full disk) never leaves a damaged file. The tree file is not touched, so the cost of a
blackboard write depends on the size of the blackboard, not of the tree. Measured with 3000
nodes (a 0.9 MB tree file) and 20 entries on Windows with an SSD: Save takes about 55 ms, and
writing a blackboard change about 1.4 ms (about 10 ms in version 0.6.0, which rewrote the
whole tree file for every change).
