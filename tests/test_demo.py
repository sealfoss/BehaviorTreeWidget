"""The demo application: its command line."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from PySide6.QtWidgets import QApplication

import behavior_tree_widget
from behavior_tree_widget import demo
from behavior_tree_widget.demo import _unconsumed_arguments

from support.round7 import _DEMO_DRIVER, other_tree_file
from support.round8 import _round_trip


# ============================================================================ demo command line
def test_demo_loads_the_file_given_on_the_command_line(qtbot, tmp_path, monkeypatch, dialogs):
    windows = []
    real_window = demo.DemoWindow

    def make_window():
        window = real_window()
        qtbot.addWidget(window)
        windows.append(window)
        return window

    monkeypatch.setattr(demo, "DemoWindow", make_window)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    path = tmp_path / "tree_BT.json"  # the tree file written by NewTree (its blackboard: tree_BB.json)
    source = real_window()
    qtbot.addWidget(source)
    assert source.tree.NewTree(str(path))
    source.tree.Shutdown()

    assert demo.main(["demo", str(path)]) == 0
    assert windows[-1].tree.IsTreeLoaded()
    assert windows[-1].tree.GetFilePath() == os.path.abspath(str(path))

    assert demo.main(["demo", str(tmp_path / "missing.json")]) == 0
    assert "critical" in dialogs.kinds()
    assert not windows[-1].tree.IsTreeLoaded()
    for window in windows:
        window.tree._set_modified(False)
        window.tree.Shutdown()


# ============================================================================ demo command line
@pytest.mark.parametrize("name", ["Ωmega.json", "日本.json", "Bäume.json"])
@pytest.mark.parametrize("qt_options", [[], ["-style", "fusion"]])
def test_demo_loads_a_file_whose_name_is_not_in_the_ansi_code_page(make_widget, tmp_path, name, qt_options):
    target = other_tree_file(make_widget, tmp_path / name)
    other_tree_file(make_widget, tmp_path / "Omega.json")  # the "best fit" of Ωmega.json
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(behavior_tree_widget.__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
    result = subprocess.run(
        [sys.executable, "-c", _DEMO_DRIVER, "demo", *qt_options, target],
        env=env, capture_output=True, timeout=120, cwd=str(tmp_path),  # its log file goes there
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode("utf-8") == repr(os.path.abspath(target))


# ============================================================================ demo command line
@pytest.mark.parametrize(
    "argv, kept",
    [
        (["demo", "-qwindowtitle", "日.json", "本.json"], ["demo", "本.json"]),
        (["demo", "-qwindowtitle", "Ωmega.json", "Omega.json"], ["demo", "Omega.json"]),
        (["demo", "-stylesheet", "Ωmega.json", "Omega.json"], ["demo", "Omega.json"]),
        (["demo", "-style", "fusion", "Bäume.json"], ["demo", "Bäume.json"]),
        (["demo", "Ωmega.json", "-style", "fusion"], ["demo", "Ωmega.json"]),
        (["demo", "tree.json"], ["demo", "tree.json"]),
        (["demo"], ["demo"]),
    ],
)
def test_unconsumed_arguments(argv, kept):
    arguments = [_round_trip(arg) for arg in kept]  # what Qt reports
    assert _unconsumed_arguments(argv, arguments) == kept
