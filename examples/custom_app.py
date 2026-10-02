"""Example: embedding BehaviorTreeWidget in your own application.

Run with the package installed::

    python examples/custom_app.py            # start with an empty tree (click New or Load)
    python examples/custom_app.py --sample   # build a sample tree in code first

It shows how to

* write leaf nodes with fields (``_fields``) and logic (``OnRun``),
* run long operations on a worker thread and support Stop (``CancelRequested``),
* read and write the blackboard (``tree.GetEntry`` / ``tree.SetEntry``),
* prepare the blackboard entries the nodes need whenever a tree is created or loaded,
* build and connect a tree from code and react to the widget's signals.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

from behavior_tree_widget import BehaviorTreeWidget, LeafNodeWidget, Status

# Blackboard entries the example nodes use: name -> (type, initial value).
REQUIRED_ENTRIES = {"battery": ("Double", 35.0), "location": ("String", "dock")}


def ensure_entries(tree: BehaviorTreeWidget) -> None:
    """Create the entries the nodes need (New starts empty; a loaded file may lack them)."""
    for name, (type_name, value) in REQUIRED_ENTRIES.items():
        if not tree.HasEntry(name):
            tree.AddEntry(name, type_name, value)


class CheckBattery(LeafNodeWidget):
    """Condition: succeeds when the 'battery' blackboard entry is above a threshold."""

    _title = "Check Battery"
    _fields = {"minimum": 20.0}
    RUN_IN_THREAD = False  # a quick check: run on the GUI thread

    def OnRun(self, tree):
        return tree.HasEntry("battery") and tree.GetEntry("battery") >= self.GetField("minimum")


class Charge(LeafNodeWidget):
    """Charges the battery to 100 % (runs on a worker thread; Stop cancels it)."""

    _title = "Charge"
    _fields = {"rate_per_second": 40.0}

    def OnRun(self, tree):
        while tree.GetEntry("battery") < 100.0:
            if self.CancelRequested():
                return False
            time.sleep(0.1)
            tree.SetEntry(min(100.0, tree.GetEntry("battery") + self.GetField("rate_per_second") / 10.0), "battery")
        return True


class Drive(LeafNodeWidget):
    """Drives to a named waypoint, consuming battery."""

    _title = "Drive"
    _fields = {"waypoint": ["kitchen", "garage", "garden"], "seconds": 1.5, "fast": False}

    def OnRun(self, tree):
        drain = 30.0 if self.GetField("fast") else 15.0
        end = time.monotonic() + self.GetField("seconds")
        while time.monotonic() < end:
            if self.CancelRequested():
                return False
            time.sleep(0.05)
        tree.SetEntry(max(0.0, tree.GetEntry("battery") - drain), "battery")
        tree.SetEntry(self.GetFieldSelection("waypoint"), "location")
        return True


class Blink(LeafNodeWidget):
    """Polling style node: returns RUNNING for a few ticks on the GUI thread."""

    _title = "Blink"
    _fields = {"times": 3}
    RUN_IN_THREAD = False

    def OnStart(self, tree):
        self.remaining = self.GetField("times")

    def OnRun(self, tree):
        self.remaining -= 1
        return Status.RUNNING if self.remaining > 0 else Status.SUCCESS


def build_sample_tree(widget: BehaviorTreeWidget) -> None:
    """Root(Sequence) -> [Selector -> [Check Battery, Charge], Drive, Blink]."""
    path = os.path.join(tempfile.gettempdir(), "behavior_tree_widget_example.json")
    widget.NewTree(path)  # creates the file, enables the editor and sets the window title

    root = widget.GetRootNode()
    ensure_power = widget.AddNode("Selector", -380, 170)
    check = widget.AddNode(CheckBattery, -660, 330)
    charge = widget.AddNode(Charge, -330, 330)
    drive = widget.AddNode(Drive, -20, 170)
    blink = widget.AddNode(Blink, 330, 170)
    ensure_power.SetTitle("Ensure Power")

    widget.Connect(root, ensure_power)
    widget.Connect(root, drive)
    widget.Connect(root, blink)
    widget.Connect(ensure_power, check)
    widget.Connect(ensure_power, charge)
    widget.view().set_zoom(0.85)  # show the whole sample tree
    widget.view().centerOn(-90, 290)
    widget.SaveTree(path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.tree = BehaviorTreeWidget(node_types=[CheckBattery, Charge, Drive, Blink])
        self.setCentralWidget(self.tree)
        # New/Load replace the blackboard, so (re)create the entries the nodes need.
        self.tree.treeLoaded.connect(lambda _path: ensure_entries(self.tree))

    def closeEvent(self, event):  # noqa: N802 - Qt API
        # Offer to save unsaved changes, then stop execution and release resources.
        if not self.tree.ConfirmDiscardChanges():
            event.ignore()
            return
        self.tree.Shutdown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    tree = window.tree
    status = QLabel("Click New or Load to start.")
    window.statusBar().addWidget(status, 1)

    def on_finished(result: str) -> None:
        battery = tree.GetEntry("battery") if tree.HasEntry("battery") else float("nan")
        location = tree.GetEntry("location") if tree.HasEntry("location") else "?"
        status.setText(f"Finished: {result} - battery {battery:.0f}%, location {location}")

    tree.executionFinished.connect(on_finished)
    tree.nodeError.connect(lambda node, message: status.setText(f"{node.GetTitle()} failed: {message}"))

    window.resize(1100, 750)
    window.show()
    if "--sample" in sys.argv:
        build_sample_tree(tree)
        status.setText("Sample tree built - press Execute.")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
