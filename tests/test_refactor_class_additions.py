#!/usr/bin/env python3
"""Tests for new REFACTOR_CLASS_RULES entries (debt-todo-marker, rust-clone-unnecessary).

These rules route directly to human review via REFACTOR_CLASS_RULES, not through
context rules. This is because the skip→CLAUDE_FIX asymmetry in classify_finding()
would send them to LLM instead of human review if they were context rules with
default_strategy="skip".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from bluei.engine.reforge import (
    REFACTOR_CLASS_RULES,
    RefactorClass,
    classify_finding,
)


class TestRefactorClassMembership:
    def test_debt_todo_marker_in_rules(self):
        assert "debt-todo-marker" in REFACTOR_CLASS_RULES

    def test_rust_clone_unnecessary_in_rules(self):
        assert "rust-clone-unnecessary" in REFACTOR_CLASS_RULES

    def test_existing_rules_still_present(self):
        assert "xo-max-lines" in REFACTOR_CLASS_RULES
        assert "xo-complexity" in REFACTOR_CLASS_RULES

    def test_total_count(self):
        assert len(REFACTOR_CLASS_RULES) == 4


class TestDebtTodoMarkerRouting:
    def test_routes_to_refactor_class(self, make_finding):
        finding = make_finding(
            path="src/main.py",
            rule="debt-todo-marker",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_routes_to_refactor_class_in_vendor(self, make_finding):
        finding = make_finding(
            path="vendor/lib/main.py",
            rule="debt-todo-marker",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_routes_to_refactor_class_in_tests(self, make_finding):
        finding = make_finding(
            path="tests/test_main.py",
            rule="debt-todo-marker",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_sets_refactor_class_on_finding(self, make_finding):
        finding = make_finding(
            path="src/app.py",
            rule="debt-todo-marker",
            safe_to_autofix=False,
            quick_win=False,
        )
        classify_finding(finding)
        assert finding.refactor_class == RefactorClass.REFACTOR_CLASS.value


class TestRustCloneUnnecessaryRouting:
    def test_routes_to_refactor_class(self, make_finding):
        finding = make_finding(
            path="src/main.rs",
            rule="rust-clone-unnecessary",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_routes_to_refactor_class_in_generated(self, make_finding):
        finding = make_finding(
            path="src/generated/types.rs",
            rule="rust-clone-unnecessary",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_routes_to_refactor_class_in_tests(self, make_finding):
        finding = make_finding(
            path="tests/integration.rs",
            rule="rust-clone-unnecessary",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_sets_refactor_class_on_finding(self, make_finding):
        finding = make_finding(
            path="src/lib.rs",
            rule="rust-clone-unnecessary",
            safe_to_autofix=False,
            quick_win=False,
        )
        classify_finding(finding)
        assert finding.refactor_class == RefactorClass.REFACTOR_CLASS.value
