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

## Documentation

| Document | Contents |
|---|---|
| [Writing leaf nodes](docs/writing-nodes.md) | `LeafNodeWidget`: fields, `OnRun` / `OnStart` / `OnTerminate`, worker threads and cancellation, registering node types, threading rules |
| [Built-in nodes](docs/built-in-nodes.md) | Negation, Evaluation and Set, Restart / Pause / Stop, UDP Message, Print Message, Sub-Tree (and Zoom In), Notes, Groups |
| [User guide](docs/user-guide.md) | Using the editor, the blackboard, execution semantics and options, the log |
| [Tree files](docs/file-formats.md) | The tree file and the blackboard file, compatibility with earlier versions |
| [API reference](docs/api.md) | `BehaviorTreeWidget` methods and signals, the node classes |
| [Building, testing and project layout](docs/development.md) | Development setup, tests, tools, the modules of the package |
| [Notes and design decisions](docs/design.md) | Why things work the way they do |
| [Changes](CHANGELOG.md) | What changed in each version |

---

## Installation

Requirements: Python ≥ 3.13, PySide6 ≥ 6.9.1 and py_trees ≥ 2.2.1. pip installs these
automatically.

The wheel of the current version is in the `dist/` folder of this repository, named
`behavior_tree_widget-<version>-py3-none-any.whl`. From the repository folder:

```bash
pip install --find-links dist behavior-tree-widget
```

See [Building a release](docs/development.md#building-a-release) to create it yourself.

To use the widget in another project, install the wheel into that project's environment. To
declare it as a dependency, point to the wheel file or to a folder of wheels:

```text
# requirements.txt
behavior-tree-widget @ file:///C:/path/to/behavior_tree_widget-<version>-py3-none-any.whl
```

```toml
# pyproject.toml of your project
[project]
dependencies = ["behavior-tree-widget>=0.9"]
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
