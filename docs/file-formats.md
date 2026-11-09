# Tree files

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
* A sub-tree file written by [Promote to Sub-Tree](built-in-nodes.md#groups) is an ordinary tree file without
  blackboard file; it can be opened as a tree of its own.
* A sub-tree shown with [Zoom In](built-in-nodes.md#zooming-into-sub-trees) is saved to its own file (Save while
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
* Version 0.9.0 reads and writes the same files as 0.8.0.
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
    [The blackboard](user-guide.md#the-blackboard)).
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
