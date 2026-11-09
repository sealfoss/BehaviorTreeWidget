"""Blackboard entry types and their values: validation, conversion, parsing and display text."""

from __future__ import annotations

import ast
import copy
import math
import reprlib
from dataclasses import dataclass
from typing import Any

from .._common import INT_MAX, INT_MIN

__all__ = [
    "DISPLAY_LIMIT",
    "ENTRY_TYPES",
    "INVALID_NAME_CHARACTERS",
    "TYPE_ALIASES",
    "EntryType",
    "canonical_type",
    "coerce_value",
    "container_items",
    "infer_type",
    "literal_eval_extended",
    "literal_text",
    "parse_container_text",
    "safe_repr",
    "safe_str",
]

#: Display texts (combo box items, tooltips, read-only Object values) are cut to this many characters.
DISPLAY_LIMIT = 500


@dataclass(frozen=True)
class EntryType:
    name: str
    ui_file: str
    value_widget: str
    default: Any
    user_creatable: bool = True
    container: bool = False
    example: str = ""


ENTRY_TYPES: dict[str, EntryType] = {
    "Integer": EntryType("Integer", "IntegerEntry.ui", "Value", 0),
    "Double": EntryType("Double", "FloatEntry.ui", "Value", 0.0),
    "String": EntryType("String", "StringEntry.ui", "Value", ""),
    "Bool": EntryType("Bool", "BoolEntry.ui", "Value", False),
    "List": EntryType("List", "ListEntry.ui", "Values", [], container=True, example="[1, 2.5, 'text']"),
    "Dictionary": EntryType(
        "Dictionary", "ListEntry.ui", "Values", {}, container=True, example="{'key': 'value', 'n': 3}"
    ),
    "Set": EntryType("Set", "ListEntry.ui", "Values", set(), container=True, example="{1, 2, 3}"),
    "Object": EntryType("Object", "StringEntry.ui", "Value", None, user_creatable=False),
}

TYPE_ALIASES = {
    "int": "Integer", "integer": "Integer",
    "double": "Double", "float": "Double",
    "str": "String", "string": "String",
    "bool": "Bool", "boolean": "Bool",
    "list": "List",
    "dict": "Dictionary", "dictionary": "Dictionary",
    "set": "Set",
    "object": "Object",
}

INVALID_NAME_CHARACTERS = "./"


def canonical_type(type_name: object) -> str:
    """Return the canonical entry type name for ``type_name`` (case-insensitive, aliases allowed)."""
    if type_name in ENTRY_TYPES:
        return type_name
    canonical = TYPE_ALIASES.get(str(type_name).strip().lower())
    if canonical is None:
        raise ValueError(f"unknown blackboard entry type {type_name!r}; expected one of {', '.join(ENTRY_TYPES)}")
    return canonical


def infer_type(value: Any) -> str:
    """Entry type used when :meth:`BlackboardStore.set` creates an entry for ``value``."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Integer" if INT_MIN <= value <= INT_MAX else "Object"
    if isinstance(value, float):
        return "Double"
    if isinstance(value, str):
        return "String"
    if isinstance(value, (list, tuple)):
        return "List"
    if isinstance(value, dict):
        return "Dictionary"
    if isinstance(value, (set, frozenset)):
        return "Set"
    return "Object"


def _deepcopy_container(type_name: str, value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception as error:
        raise TypeError(
            f"{type_name} entries must hold copyable values ({error}); "
            f"store such values in an Object entry: AddEntry(name, 'Object', value)"
        ) from error


def coerce_value(type_name: str, value: Any) -> Any:
    """Validate ``value`` for an entry of ``type_name`` and return the value to store.

    Raises:
        TypeError: the value has an incompatible type.
        ValueError: the value is out of range (e.g. an Integer outside 32 bits).
    """
    if type_name == "Integer":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"Integer entries need an int, not {type(value).__name__}")
        value = int(value)
        if not INT_MIN <= value <= INT_MAX:
            raise ValueError(f"{value} is outside the Integer entry range [{INT_MIN}, {INT_MAX}]")
        return value
    if type_name == "Double":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Double entries need a float, not {type(value).__name__}")
        try:
            return float(value)
        except OverflowError as error:
            raise ValueError(f"{value} is too large for a Double entry") from error
    if type_name == "String":
        if not isinstance(value, str):
            raise TypeError(f"String entries need a str, not {type(value).__name__}")
        return value
    if type_name == "Bool":
        if not isinstance(value, bool):
            raise TypeError(f"Bool entries need a bool, not {type(value).__name__}")
        return value
    if type_name == "List":
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"List entries need a list, not {type(value).__name__}")
        return _deepcopy_container(type_name, list(value))
    if type_name == "Dictionary":
        if not isinstance(value, dict):
            raise TypeError(f"Dictionary entries need a dict, not {type(value).__name__}")
        return _deepcopy_container(type_name, value)
    if type_name == "Set":
        if isinstance(value, dict) and not value:
            return set()
        if not isinstance(value, (set, frozenset, list, tuple)):
            raise TypeError(f"Set entries need a set, not {type(value).__name__}")
        items = _deepcopy_container(type_name, list(value))
        try:
            return set(items)
        except TypeError as error:
            raise TypeError(f"Set entries can only hold hashable values: {error}") from error
    if type_name == "Object":
        return value
    raise ValueError(f"unknown blackboard entry type {type_name!r}")


class _NonFiniteNames(ast.NodeTransformer):
    """Accept ``nan`` / ``inf`` (as written by ``repr``) inside container literals."""

    NAMES = {"nan": float("nan"), "inf": float("inf"), "infinity": float("inf")}

    def visit_Name(self, node: ast.Name) -> ast.AST:
        value = self.NAMES.get(node.id.lower())
        if value is None:
            return node
        return ast.copy_location(ast.Constant(value=value), node)


def literal_eval_extended(text: str) -> Any:
    """``ast.literal_eval`` that also accepts ``nan``, ``inf`` and ``-inf``."""
    tree = ast.parse(text.strip(), mode="eval")
    tree = ast.fix_missing_locations(_NonFiniteNames().visit(tree))
    return ast.literal_eval(tree)


def parse_container_text(type_name: str, text: str) -> Any:
    """Parse a Python literal typed by the user for a List / Dictionary / Set entry."""
    text = text.strip()
    if not text:
        return coerce_value(type_name, ENTRY_TYPES[type_name].default)
    try:
        value = literal_eval_extended(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as error:
        example = ENTRY_TYPES[type_name].example
        raise ValueError(f"Enter a Python literal such as {example}") from error
    try:
        return coerce_value(type_name, value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{error}. Example: {ENTRY_TYPES[type_name].example}") from error


def safe_str(value: Any) -> str:
    """``str(value)`` that never raises (falls back to a bounded repr)."""
    try:
        return _shorten(str(value))
    except Exception:  # noqa: BLE001 - a broken __str__
        return safe_repr(value)


def container_items(type_name: str, value: Any) -> list[str]:
    """Strings shown in the combo box of a container entry."""
    if type_name == "Dictionary":
        return [f"{safe_str(key)}: {safe_str(item)}" for key, item in value.items()]
    if type_name == "Set":
        items = [safe_str(item) for item in value]
        return sorted(items)
    return [safe_str(item) for item in value]


def _shorten(text: str) -> str:
    return text if len(text) <= DISPLAY_LIMIT else text[: DISPLAY_LIMIT - 3] + "..."


_bounded_repr = reprlib.Repr()
_bounded_repr.maxstring = DISPLAY_LIMIT
_bounded_repr.maxother = DISPLAY_LIMIT
_bounded_repr.maxlong = DISPLAY_LIMIT
for _name in ("maxlist", "maxtuple", "maxset", "maxfrozenset", "maxdeque", "maxarray", "maxdict"):
    setattr(_bounded_repr, _name, 100)
_bounded_repr.maxlevel = 10


def safe_repr(value: Any) -> str:
    """A repr for display: bounded in length (and, for big containers and bytes, in cost),
    exact for small containers, and never raising."""
    try:
        if isinstance(value, (bytes, bytearray, memoryview)) and len(value) > DISPLAY_LIMIT:
            head = bytes(value[: DISPLAY_LIMIT // 4])
            return _shorten(f"{head!r}... ({len(value)} bytes)")
        if isinstance(value, (list, tuple, set, frozenset, dict)) and len(value) <= 100:
            return _shorten(repr(value))  # exact, in the value's own order
        return _shorten(_bounded_repr.repr(value))
    except Exception:  # noqa: BLE001 - a broken __repr__
        return f"<{type(value).__name__} object>"


def literal_text(value: Any) -> str:
    """Python literal text of a container value for the Edit dialog ('' if it has none)."""
    try:
        return repr(value)
    except Exception:  # noqa: BLE001
        return ""


def _same_value(a: Any, b: Any) -> bool:
    """Whether two scalar entry values are the same (nan equals nan, -0.0 differs from 0.0)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001
        return False


def _equal_values(a: Any, b: Any) -> bool:
    """Whether two entry values are equal (scalars as :func:`_same_value`; containers with ``==``)."""
    containers = (list, tuple, set, frozenset, dict)
    if isinstance(a, containers) or isinstance(b, containers):
        try:
            return type(a) is type(b) and bool(a == b)
        except Exception:  # noqa: BLE001
            return False
    return _same_value(a, b)


def _copy_value(type_name: str, value: Any) -> Any:
    if ENTRY_TYPES[type_name].container:
        return copy.deepcopy(value)
    return value
