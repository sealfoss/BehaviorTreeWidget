"""Names deprecated in 0.9.0 keep working and say what replaces them."""

from __future__ import annotations

import pytest

RENAMED_ACCESSORS = {
    "mainView": "main_view",
    "blackboardView": "blackboard_view",
    "blackboardStore": "blackboard_store",
    "treeLogger": "tree_logger",
    "loopExecutionCheckBox": "loop_execution_check_box",
}


@pytest.mark.filterwarnings("ignore:BehaviorTreeWidget:DeprecationWarning")  # checked explicitly below
@pytest.mark.parametrize("old, new", RENAMED_ACCESSORS.items())
def test_renamed_accessors_warn_and_return_the_same_object(bt, old, new):
    with pytest.warns(DeprecationWarning, match=rf"BehaviorTreeWidget\.{old}\(\) is deprecated; use {new}\(\)") as caught:
        result = getattr(bt, old)()
    assert result is getattr(bt, new)()
    assert caught[0].filename == __file__, "the warning points at the caller"


@pytest.mark.parametrize("old, new", RENAMED_ACCESSORS.items())
def test_renamed_accessors_are_marked_deprecated_for_type_checkers(old, new):
    """warnings.deprecated (PEP 702) sets __deprecated__, which type checkers and editors show."""
    from behavior_tree_widget import BehaviorTreeWidget

    assert getattr(BehaviorTreeWidget, old).__deprecated__ == f"BehaviorTreeWidget.{old}() is deprecated; use {new}() instead"
