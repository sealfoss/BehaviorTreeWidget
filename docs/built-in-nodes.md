# Built-in nodes

Besides the Root, Sequence and Selector, every `BehaviorTreeWidget` offers nine built-in node
types in its right-click menu, and Notes and Groups. They need no registration.

## Negation

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

## Evaluation and Set

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
* The combo boxes are `EntryComboBox`es (module `blackboard_aware`), which change only the rows
  that differ when the blackboard changes. In a custom `.ui` file (`UI_FILE` of a subclass), the
  `ValueName` and `CompareTo` combo boxes are replaced by `EntryComboBox`es that keep their
  place in the layout, object name, tooltip, size policy and the other properties listed in
  `EntryComboBox.COPIED_PROPERTIES`.

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

## Restart, Pause and Stop

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

## UDP Message

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

## Print Message

A leaf node that writes a message to [the tree's log](user-guide.md#the-log) each time it runs. It shows
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
* As for Evaluation and Set, the `Source` combo box of a custom `.ui` file is replaced by an
  `EntryComboBox`.

```python
from behavior_tree_widget import LogLevel

hello = widget.AddNode("Print Message", 0, 200)
hello.SetMessage("patrol started")          # printed at DEBUG, the default level
battery = widget.AddNode("Print Message", 300, 200)
battery.SetSource("battery")                # prints "battery = 80"
battery.SetLevel(LogLevel.WARNING)          # or 2, or "warning"
```

## Sub-Tree

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

### Zooming into sub-trees

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

## Notes

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

## Groups

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
