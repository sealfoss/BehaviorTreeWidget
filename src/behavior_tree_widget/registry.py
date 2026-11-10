"""Node types: the built-in kinds every widget offers and the leaf types applications register.

Each built-in node type is described once, by a :class:`NodeKind` in :data:`BUILTIN_KINDS`
(in add-node menu order): how it is created, which menu section offers it, and how its saved
data is recognised. A :class:`NodeTypeRegistry` combines those kinds with the leaf types
registered with one widget (and globally with :func:`register_node_type`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .blackboard_aware import BlackboardAwareLeaf
from .blackboard_nodes import BlackboardValueNodeWidget, EvaluationNodeWidget, SetNodeWidget
from .control_nodes import PauseNodeWidget, RestartNodeWidget, StopNodeWidget
from .groups import GROUP, GroupNodeWidget
from .network_nodes import UDP_MESSAGE, UdpMessageNodeWidget
from .nodes import (
    COMPOSITE_TYPES,
    NEGATION,
    SELECTOR,
    SEQUENCE,
    CompositeNodeWidget,
    LeafNodeWidget,
    NegationNodeWidget,
    NodeWidget,
    UnknownLeafNodeWidget,
)
from .notes import NOTES, NotesNodeWidget
from .print_node import PRINT_MESSAGE, PrintMessageNodeWidget
from .subtree import SUB_TREE, SubTreeNodeWidget

log = logging.getLogger("behavior_tree_widget")

__all__ = [
    "BUILTIN_KINDS",
    "BUILTIN_LEAF_TYPES",
    "PARENT_TYPES",
    "RESERVED_TYPE_NAMES",
    "ROOT",
    "YIELDING_TYPE_NAMES",
    "NodeKind",
    "NodeTypeRegistry",
    "register_node_type",
    "registered_node_types",
]

ROOT = "Root"


@dataclass(frozen=True)
class NodeKind:
    """A node type every BehaviorTreeWidget offers without registration.

    Attributes:
        name: The type name, saved in tree files and shown in the add-node menu.
        group: The section of the add-node menu offering it ("composite", "blackboard",
            "execution", "network", "log", "subtree" or "notes").
        create: Creates a node; its argument is the memory flag of new composites.
        leaf_class: The class of a built-in leaf type (None for composites, Negation, Notes and Group).
        yielding: Added after version 0.5.0, so an application may already use the name for a leaf
            type of its own; that type then takes the name, and this kind is not offered there.
        saved_keys: Keys only this kind writes to a saved node (Notes and Group): they tell its
            saved data from that of an application's leaf type of the same name.
    """

    name: str
    group: str
    create: Callable[[bool], NodeWidget]
    leaf_class: type[LeafNodeWidget] | None = None
    yielding: bool = False
    saved_keys: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_box(self) -> bool:
        """True for Notes and Group: resizable boxes that are not part of the behavior tree."""
        return self.group == "notes"

    def recognises(self, item: dict) -> bool:
        """Whether the saved node ``item`` (with this kind's name) was saved by this built-in kind.

        Asked for yielding kinds only: an application may have saved leaf nodes of its own
        under the name, whose data must not be read as (and lost to) the built-in node's.
        """
        if self.leaf_class is None:
            # A note writes text / width / height and a group width / height / color; a leaf
            # writes "fields" (version 0.5.0 also adds an empty "fields" to a placeholder it kept).
            return bool(self.saved_keys & item.keys()) or "fields" not in item
        fields = item.get("fields")
        return not isinstance(fields, dict) or set(fields) <= set(self.leaf_class._fields)


def _composite(type_name: str) -> Callable[[bool], NodeWidget]:
    return lambda memory: CompositeNodeWidget(type_name, memory=memory)


def _leaf(cls: type[LeafNodeWidget]) -> Callable[[bool], NodeWidget]:
    return lambda _memory: cls()


#: The built-in node types, in add-node menu order.
BUILTIN_KINDS: tuple[NodeKind, ...] = (
    NodeKind(SEQUENCE, "composite", _composite(SEQUENCE)),
    NodeKind(SELECTOR, "composite", _composite(SELECTOR)),
    NodeKind(NEGATION, "composite", lambda _memory: NegationNodeWidget()),
    NodeKind("Evaluation", "blackboard", _leaf(EvaluationNodeWidget), EvaluationNodeWidget),
    NodeKind("Set", "blackboard", _leaf(SetNodeWidget), SetNodeWidget),
    NodeKind("Restart", "execution", _leaf(RestartNodeWidget), RestartNodeWidget),
    NodeKind("Pause", "execution", _leaf(PauseNodeWidget), PauseNodeWidget),
    NodeKind("Stop", "execution", _leaf(StopNodeWidget), StopNodeWidget),
    NodeKind(UDP_MESSAGE, "network", _leaf(UdpMessageNodeWidget), UdpMessageNodeWidget, yielding=True),
    NodeKind(PRINT_MESSAGE, "log", _leaf(PrintMessageNodeWidget), PrintMessageNodeWidget, yielding=True),
    NodeKind(SUB_TREE, "subtree", _leaf(SubTreeNodeWidget), SubTreeNodeWidget, yielding=True),
    NodeKind(NOTES, "notes", lambda _memory: NotesNodeWidget(), yielding=True,
             saved_keys=frozenset({"text", "width", "height"})),
    NodeKind(GROUP, "notes", lambda _memory: GroupNodeWidget(), yielding=True,
             saved_keys=frozenset({"width", "height", "color"})),
)
KINDS: dict[str, NodeKind] = {kind.name: kind for kind in BUILTIN_KINDS}

#: Leaf node types every BehaviorTreeWidget offers, by type name.
BUILTIN_LEAF_TYPES: dict[str, type[LeafNodeWidget]] = {
    kind.name: kind.leaf_class for kind in BUILTIN_KINDS if kind.leaf_class is not None
}
#: Node types with children offered by the add-node menu, in menu order.
PARENT_TYPES = (*COMPOSITE_TYPES, NEGATION)
#: Built-in type names an application's own leaf type may take (see NodeKind.yielding).
YIELDING_TYPE_NAMES = frozenset(kind.name for kind in BUILTIN_KINDS if kind.yielding)
#: Type names an application's leaf type can never take.
RESERVED_TYPE_NAMES = frozenset({ROOT, *(kind.name for kind in BUILTIN_KINDS if not kind.yielding)})


# ====================================================================== registration
def _type_name_of(cls: type) -> str:
    """Registration name of a node class: its own ``TYPE_NAME`` or its class name."""
    return cls.__dict__.get("TYPE_NAME") or cls.__name__


def _validate_leaf_class(cls: Any) -> str:
    if not isinstance(cls, type) or not issubclass(cls, LeafNodeWidget):
        raise TypeError(f"node types must be subclasses of LeafNodeWidget, not {cls!r}")
    abstract = (LeafNodeWidget, BlackboardAwareLeaf, BlackboardValueNodeWidget)
    if cls in abstract or issubclass(cls, UnknownLeafNodeWidget):
        raise TypeError(f"{cls.__name__} cannot be registered; register a subclass that implements OnRun")
    name = _type_name_of(cls)
    if not isinstance(name, str) or not name:
        raise ValueError(f"{cls.__name__}.TYPE_NAME must be a non-empty string")
    if name in RESERVED_TYPE_NAMES and BUILTIN_LEAF_TYPES.get(name) is not cls:
        raise ValueError(f"{name!r} is a built-in node type name; set a different TYPE_NAME on {cls.__name__}")
    return name


def _register(registry: dict[str, type[LeafNodeWidget]], cls: type[LeafNodeWidget]) -> str:
    name = _validate_leaf_class(cls)
    existing = registry.get(name)
    if (
        existing is not None
        and existing is not cls
        and not (name in YIELDING_TYPE_NAMES and existing is BUILTIN_LEAF_TYPES.get(name))
    ):
        raise ValueError(
            f"node type name {name!r} is already registered to {existing.__module__}.{existing.__qualname__}; "
            f"give {cls.__qualname__} a different TYPE_NAME"
        )
    registry[name] = cls
    return name


#: Leaf types registered with register_node_type, offered by every widget created afterwards.
_global_node_types: dict[str, type[LeafNodeWidget]] = {}


def register_node_type(cls: type[LeafNodeWidget]) -> type[LeafNodeWidget]:
    """Class decorator registering a leaf node type with every BehaviorTreeWidget created afterwards.

    Such widgets offer the type in their right-click menu and can load trees that use it::

        @register_node_type
        class Beep(LeafNodeWidget):
            def OnRun(self, tree):
                print("beep")
                return True
    """
    _register(_global_node_types, cls)
    return cls


def registered_node_types() -> dict[str, type[LeafNodeWidget]]:
    """Leaf node types registered with :func:`register_node_type`, by type name."""
    return dict(_global_node_types)


# ====================================================================== the types of one widget
class NodeTypeRegistry:
    """The node types of one BehaviorTreeWidget: the built-in kinds and its registered leaf types."""

    def __init__(self, leaf_types: dict[str, type[LeafNodeWidget]] | None = None):
        self._leaves: dict[str, type[LeafNodeWidget]] = {**BUILTIN_LEAF_TYPES, **(leaf_types or {})}

    def register(self, cls: type[LeafNodeWidget]) -> str:
        """Register the leaf class ``cls`` (see :func:`register_node_type`); returns its type name.

        Raises:
            TypeError: ``cls`` is not a LeafNodeWidget subclass that can be registered.
            ValueError: its type name is reserved or registered to another class.
        """
        return _register(self._leaves, cls)

    def leaf_class(self, type_name: str) -> type[LeafNodeWidget] | None:
        """The leaf class of ``type_name`` (built-in leaves included), or None."""
        return self._leaves.get(type_name)

    def offers(self, type_name: str) -> bool:
        """True if ``type_name`` is a built-in kind that this widget offers: no application's leaf
        type took its name."""
        kind = KINDS.get(type_name)
        if kind is None:
            return False
        if kind.leaf_class is not None:
            return self._leaves.get(type_name) is kind.leaf_class
        return type_name not in self._leaves

    def type_names(self) -> list[str]:
        """Sequence, Selector, Negation and the offered built-in leaves, the application's leaf types
        (sorted), then Notes and Group (see BehaviorTreeWidget.GetNodeTypes)."""
        offered = [kind for kind in BUILTIN_KINDS if self.offers(kind.name)]
        registered = sorted(name for name in self._leaves if not self.offers(name))
        return [
            *(kind.name for kind in offered if not kind.is_box),
            *registered,
            *(kind.name for kind in offered if kind.is_box),
        ]

    def menu_entries(self) -> list[tuple[str, str, str]]:
        """``(menu section, type name, label)`` of every type the add-node menu offers, in menu order.

        A leaf type is labelled with its title, followed by its type name when another type has the
        same title. The application's leaf types come after the built-in ones, sorted by title.
        """
        leaves = []
        for name, cls in self._leaves.items():
            title = cls._title if isinstance(cls._title, str) and cls._title else cls.__name__
            leaves.append((title, name))
        titles = [title for title, _ in leaves]
        title_of = {name: title for title, name in leaves}

        def label(title: str, name: str) -> str:
            return title if titles.count(title) == 1 or self.offers(name) else f"{title} ({name})"

        entries = []
        for kind in BUILTIN_KINDS:
            if not self.offers(kind.name) or kind.is_box:
                continue
            text = label(title_of[kind.name], kind.name) if kind.leaf_class else kind.name
            entries.append((kind.group, kind.name, text))
        for title, name in sorted(leaves, key=lambda pair: (pair[0].lower(), pair[1])):
            if not self.offers(name):
                entries.append(("leaf", name, label(title, name)))
        entries += [
            (kind.group, kind.name, kind.name) for kind in BUILTIN_KINDS if kind.is_box and self.offers(kind.name)
        ]
        return entries

    def create(self, type_name: str, memory: bool = True) -> NodeWidget:
        """A new node of ``type_name`` (a built-in kind or a registered leaf type, not Root).

        ``memory`` is the memory flag of a new Sequence or Selector.

        Raises:
            ValueError: the type is unknown here.
        """
        kind = KINDS.get(type_name)
        if kind is not None and self.offers(type_name):
            return kind.create(memory)
        cls = self._leaves.get(type_name)
        if cls is None:
            raise ValueError(f"unknown node type {type_name!r}")
        return cls()

    def create_saved(self, type_name: str, item: dict, label: str) -> tuple[NodeWidget, str | None]:
        """A node for the saved node ``item`` of ``type_name`` (not Root), and a problem to report.

        Types that are not registered, saved data of an application's leaf type whose name is now a
        built-in kind, and leaf classes that fail to be created give placeholders
        (:class:`UnknownLeafNodeWidget`) that keep the saved data. ``label`` names the node in messages.
        """
        not_registered = f"node type {type_name!r} is not registered; a placeholder node was created"
        kind = KINDS.get(type_name)
        if kind is not None and not kind.yielding and kind.leaf_class is None:
            return kind.create(True), None  # Sequence, Selector, Negation (their memory is loaded later)
        if kind is not None and kind.yielding and self.offers(type_name) and not kind.recognises(item):
            # Saved by an application's own leaf type of that name, which is not registered now.
            return UnknownLeafNodeWidget(type_name), not_registered
        if kind is not None and kind.is_box and self.offers(type_name):
            return kind.create(True), None
        cls = self._leaves.get(type_name)
        if cls is None:
            return UnknownLeafNodeWidget(type_name), not_registered
        try:
            return cls(), None
        except Exception as error:
            log.exception("could not create node of type %r", type_name)
            return UnknownLeafNodeWidget(type_name), (
                f"node {label}: {type_name} could not be created ({error}); a placeholder was used"
            )
