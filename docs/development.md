# Building, testing and project layout

## Setting up

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"      # Windows (use .venv/bin/... elsewhere)
```

The `test` extra installs pytest, pytest-qt, pytest-timeout and pytest-xdist.

## Running the tests

```bash
.venv/Scripts/python -m pytest -n auto      # the whole suite, offscreen, on all cores
.venv/Scripts/python -m pytest              # the same in one process (several minutes)
.venv/Scripts/python -m pytest -m perf      # the opt-in performance tests in tests/perf
```

* The tests run offscreen (`QT_QPA_PLATFORM=offscreen`, set by `tests/conftest.py`), so they
  open no windows. `-n auto` runs them on every core with pytest-xdist; each worker process
  has its own `QApplication`.
* Modal UI (message boxes, file dialogs, input dialogs, context menus) is replaced by the
  `dialogs` fixture of `tests/conftest.py`, so no test can block.
* The performance tests in `tests/perf/` are not run by default. They check that the costs
  measured for 0.9.0 (see [CHANGELOG.md](../CHANGELOG.md)) do not come back, with generous
  limits, since the times depend on the machine.
* `DeprecationWarning`s raised by the package's deprecated names are errors in the test suite
  (`filterwarnings` in `pyproject.toml`), so internal code cannot use them. A test that checks
  a deprecated name opts out with
  `@pytest.mark.filterwarnings("ignore:...:DeprecationWarning")`.
* `tests/data/` holds tree files written by earlier versions, for the compatibility tests.

## Checking the code

```bash
.venv/Scripts/python -m ruff check src tests tools examples   # lint (configuration: pyproject.toml)
.venv/Scripts/python -m mypy                                  # type check (configuration: pyproject.toml)
.venv/Scripts/python -m mypy --platform linux                 # type check as the CI does (on Ubuntu)
```

The generated forms in `src/behavior_tree_widget/_forms/` are excluded from both. mypy also
reports the use of deprecated names and every override without `@override` (`typing.override`):
a method that overrides one must be marked, and `@override` on a method that overrides nothing,
such as a misspelled Qt event handler, is an error. mypy checks
the code for the platform it runs on: code under a `sys.platform` check is only checked there
(code under an `os.name` check is checked everywhere, so Windows-only code belongs under
`sys.platform == "win32"`).

The GitHub Actions workflow `.github/workflows/ci.yml` runs on every push and pull request: ruff,
mypy and `tools/compile_ui.py --check` on Ubuntu, the test suite on Windows (offscreen,
`pytest -n auto`) with Python 3.13 and 3.14 and once more with Python 3.13 and the oldest
PySide6 and py_trees supported (the pip constraints in `.github/minimum-versions.txt`), and then
builds the wheel and the source distribution, which it keeps as the `dist` artifact of the run. Each failed test is also an annotation of the run
(`pytest_terminal_summary` in `tests/conftest.py`), so it can be read without the log.

## Editing the Qt Designer files

The `.ui` files in `src/behavior_tree_widget/ui/` can be edited with `pyside6-designer`. The
package does not parse them at run time: each one is compiled into a Python form in
`src/behavior_tree_widget/_forms/`, which builds the widgets several times faster than
`QUiLoader`. After editing a `.ui` file, compile the forms again:

```bash
.venv/Scripts/python tools/compile_ui.py            # regenerate _forms/
.venv/Scripts/python tools/compile_ui.py --check    # exit status 1 if a form is missing or out of date
```

The index `_forms/__init__.py` records the SHA-256 of every `.ui` file it was compiled from. A
`.ui` file whose hash does not match (edited, not compiled again) is still loaded, with
`QUiLoader`, so it works, only slower; the test suite fails until the forms are compiled again.
`.ui` files of your own node classes (`UI_FILE`) are always loaded with `QUiLoader`.

## Building a release

```bash
.venv/Scripts/pip install build
.venv/Scripts/python tools/build_release.py
```

`tools/build_release.py` reads the version from `behavior_tree_widget.__version__`, deletes the
`build/` folder (setuptools never removes stale files from it, and they would end up in the
wheel), checks that the forms are up to date, builds the source distribution and the wheel into
`dist/` (`behavior_tree_widget-<version>.tar.gz` and `behavior_tree_widget-<version>-py3-none-any.whl`)
and removes the files of other versions from `dist/`.

## Project layout

```
pyproject.toml
CHANGELOG.md
docs/                    the documentation (README.md links to it)
src/behavior_tree_widget/
    __init__.py          public API
    __main__.py          python -m behavior_tree_widget: the demo
    widget.py            BehaviorTreeWidget: the tabs and buttons; ties the modules below together
    registry.py          node types: one NodeKind per built-in type, NodeTypeRegistry, register_node_type
    document.py          TreeDocument: a tree's nodes and links, its file, options and unsaved changes
    tree_io.py           tree file data to nodes (in two steps: prepare, install) and nodes to data
    zoom.py              sub-trees shown with Zoom In: ZoomLevel, ZoomStack
    promote.py           Promote to Sub-Tree
    clipboard.py         copy and paste of nodes
    blackboard_file.py   BlackboardFileWriter: writes the blackboard file as the blackboard changes
    canvas.py            TreeView: the graphics view showing a tree and editing it
    geometry.py          scene area, connection curves, placement of pasted nodes (internal)
    items.py             graphics items: nodes, connection lines, drag line, selection band
    gestures.py          mouse gestures of the view, one class each (internal)
    menus.py             context menus of the view (internal)
    nodes.py             NodeWidget, RootNodeWidget, CompositeNodeWidget, NegationNodeWidget, LeafNodeWidget
    blackboard_aware.py  BlackboardAwareLeaf (nodes listing the entries) and EntryComboBox
    blackboard_nodes.py  the built-in Evaluation and Set leaf nodes
    control_nodes.py     the built-in Restart, Pause and Stop leaf nodes
    network_nodes.py     the built-in UDP Message leaf node
    print_node.py        the built-in Print Message leaf node
    subtree.py           the built-in Sub-Tree leaf node (SubTreeNodeWidget)
    notes.py             the Notes node (NotesNodeWidget) and the resizable-box base (BoxNodeWidget)
    groups.py            the Group node (GroupNodeWidget)
    execution.py         TreeExecutor: commands, ticking, statuses, execution loops
    tree_builder.py      the node graph to a py_trees tree (when execution starts)
    behaviours.py        the py_trees behaviours running the nodes (leaves, sub-trees, the log visitor)
    execution_host.py    ExecutionHost: what the executor needs from the widget
    _workers.py          WorkerPool: the reused daemon threads running OnRun calls (internal)
    _runctx.py           cancellation tokens of running nodes (internal)
    blackboard/          the blackboard:
        store.py             BlackboardStore: thread-safe, typed, backed by py_trees
        values.py            entry types: validation, conversion, parsing, display text
        view.py              the Blackboard tab (BlackboardView, EntryRow, dialogs)
        float_spinbox.py     FloatSpinBox: the exact editor of Double entries
    tree_log.py          the tree's log: LogLevel, TreeLogger, the log files and their writer thread
    serialization.py     JSON file formats (tree file and blackboard file, their names)
    config.py            TreeConfig and the Configure dialog
    demo.py              demo application and example nodes
    _ui.py               loading of .ui files: generated forms, or QUiLoader (internal)
    _forms/              Python forms generated from ui/*.ui by tools/compile_ui.py (do not edit)
    _common.py, _qt.py   small shared constants and Qt helpers (internal)
    ui/*.ui              Qt Designer files (editable with pyside6-designer)
tools/
    compile_ui.py        compiles ui/*.ui into _forms/
    build_release.py     builds the wheel and the source distribution into dist/
tests/                   pytest + pytest-qt test suite (tests/data: tree files of earlier versions;
                         tests/perf: opt-in performance tests)
examples/                example application
dist/                    the wheel and source distribution of the current version
```

Internal modules (names starting with `_`, and those marked internal above) may change in any
release. The rest is what the [API reference](api.md) describes; `TreeView`, the node classes
and the `blackboard` package are documented for customisation.
