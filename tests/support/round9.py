"""Helpers, node types and fixtures of the regression tests that came from
test_regressions_round9.py (the tests now live in the feature files that use them)."""

from __future__ import annotations

from behavior_tree_widget import LeafNodeWidget


class Quick(LeafNodeWidget):
    TYPE_NAME = "R9Quick"
    RUN_IN_THREAD = False

    def OnRun(self, tree):
        return True
