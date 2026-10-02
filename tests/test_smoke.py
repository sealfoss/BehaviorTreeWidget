"""Minimal end-to-end smoke test of the shared fixtures."""

from behavior_tree_widget.nodes import CHILDREN, PARENT, ConnectionState

from conftest import drag, label_point, run_until_idle, fast_config


def test_build_connect_and_run(qtbot, bt):
    root = bt.GetRootNode()
    leaf = bt.AddNode("Succeed", 0, 200)
    bt.view().centerOn(50, 150)
    drag(bt, label_point(bt, root, CHILDREN), label_point(bt, leaf, PARENT))
    assert leaf.GetParent() is root
    assert leaf.connection_state(PARENT) is ConnectionState.CONNECTED
    fast_config(bt)
    bt.Execute()
    run_until_idle(qtbot, bt)
    assert root.GetStatus().value == "Succeeded"
    assert leaf.GetStatus().value == "Succeeded"
