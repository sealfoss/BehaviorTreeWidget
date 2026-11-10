"""Blackboard: typed named values shared by the nodes of a tree.

:class:`BlackboardStore` keeps the values in a ``py_trees`` blackboard (one private
namespace per widget, see :meth:`BlackboardStore.namespace`) and is safe to use from
any thread. :class:`BlackboardView` is the "Blackboard" tab listing the entries.

Entry types and their editors:

============  ==================  =====================================
Type          Python value        Editor (ui file)
============  ==================  =====================================
Integer       int (32 bit)        QSpinBox (IntegerEntry.ui)
Double        float               QDoubleSpinBox (FloatEntry.ui)
String        str                 QLineEdit (StringEntry.ui)
Bool          bool                QCheckBox (BoolEntry.ui)
List          list                QComboBox + Edit button (ListEntry.ui)
Dictionary    dict                QComboBox + Edit button (ListEntry.ui)
Set           set                 QComboBox + Edit button (ListEntry.ui)
Object        anything else       read-only text (StringEntry.ui); only
                                  created by ``SetEntry`` from code
============  ==================  =====================================

The package is split into :mod:`.values` (entry types, validation, parsing and display text),
:mod:`.float_spinbox` (the Double editor), :mod:`.store` (:class:`BlackboardStore`) and
:mod:`.view` (the Blackboard tab). Every name is importable from here, as before the split.
"""

from __future__ import annotations

from .._common import INT_MAX, INT_MIN
from .float_spinbox import _DBL_MAX as _DBL_MAX
from .float_spinbox import FloatSpinBox
from .float_spinbox import _parse_float_text as _parse_float_text
from .store import BlackboardSnapshot, BlackboardStore
from .values import (
    DISPLAY_LIMIT,
    ENTRY_TYPES,
    INVALID_NAME_CHARACTERS,
    TYPE_ALIASES,
    EntryType,
    canonical_type,
    coerce_value,
    container_items,
    infer_type,
    literal_eval_extended,
    literal_text,
    parse_container_text,
    safe_repr,
    safe_str,
)
from .values import _bounded_repr as _bounded_repr
from .values import _copy_value as _copy_value
from .values import _deepcopy_container as _deepcopy_container
from .values import _equal_values as _equal_values
from .values import _NonFiniteNames as _NonFiniteNames
from .values import _same_value as _same_value
from .values import _shorten as _shorten
from .view import (
    FLOAT_DECIMALS,
    MOVE_DOWN_TOOLTIP,
    MOVE_UP_TOOLTIP,
    RESET_TOOLTIP,
    AddEntryDialog,
    BlackboardView,
    EditContainerDialog,
    EntryRow,
    create_value_editor,
    editor_value,
)
from .view import _ValueDialogBase as _ValueDialogBase

__all__ = [
    "DISPLAY_LIMIT",
    "ENTRY_TYPES",
    "FLOAT_DECIMALS",
    "INT_MAX",
    "INT_MIN",
    "INVALID_NAME_CHARACTERS",
    "MOVE_DOWN_TOOLTIP",
    "MOVE_UP_TOOLTIP",
    "RESET_TOOLTIP",
    "TYPE_ALIASES",
    "AddEntryDialog",
    "BlackboardSnapshot",
    "BlackboardStore",
    "BlackboardView",
    "EditContainerDialog",
    "EntryRow",
    "EntryType",
    "FloatSpinBox",
    "canonical_type",
    "coerce_value",
    "container_items",
    "create_value_editor",
    "editor_value",
    "infer_type",
    "literal_eval_extended",
    "literal_text",
    "parse_container_text",
    "safe_repr",
    "safe_str",
]
