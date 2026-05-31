#!/usr/bin/env python3
"""Tests for context rule registry and classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "core"))

from bluei.engine.reforge import (
    ContextOverride,
    ContextRule,
    RefactorClass,
    classify_finding,
    get_context_rule,
    match_context,
)


def test_context_rule_c408_django_migration(make_finding):
    finding = make_finding(
        path="zerver/migrations/0001_initial.py",
        rule="ruff-c408",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding, detected_frameworks=["django"])
    assert result == RefactorClass.REFACTOR_CLASS, (
        f"Expected REFACTOR_CLASS, got {result}"
    )


def test_context_rule_c408_test_file(make_finding):
    finding = make_finding(
        path="zerver/test_views.py",
        rule="ruff-c408",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_c408_app_code(make_finding):
    finding = make_finding(
        path="zerver/views.py", rule="ruff-c408", safe_to_autofix=False, quick_win=False
    )
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, (
        f"Expected SIMPLE_FIX (default), got {result}"
    )


def test_context_rule_b904_middleware(make_finding):
    finding = make_finding(
        path="zerver/middleware.py",
        rule="ruff-b904",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, (
        f"Expected CONTEXTUAL_FIX, got {result}"
    )


def test_context_rule_b904_app_code(make_finding):
    finding = make_finding(
        path="zerver/views.py", rule="ruff-b904", safe_to_autofix=False, quick_win=False
    )
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, (
        f"Expected CONTEXTUAL_FIX (default), got {result}"
    )


def test_context_rule_b007_fixtures(make_finding):
    finding = make_finding(
        path="zerver/fixtures.py",
        rule="ruff-b007",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_b007_app_code(make_finding):
    finding = make_finding(
        path="zerver/views.py", rule="ruff-b007", safe_to_autofix=False, quick_win=False
    )
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, (
        f"Expected CONTEXTUAL_FIX (default), got {result}"
    )


def test_context_rule_s311_test_file(make_finding):
    finding = make_finding(
        path="zerver/test_views.py",
        rule="ruff-s311",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_s311_fixtures(make_finding):
    finding = make_finding(
        path="zerver/fixtures.py",
        rule="ruff-s311",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_s311_app_code(make_finding):
    finding = make_finding(
        path="zerver/views.py", rule="ruff-s311", safe_to_autofix=False, quick_win=False
    )
    result = classify_finding(finding)
    assert result == RefactorClass.CLAUDE_FIX, (
        f"Expected CLAUDE_FIX (default=skip), got {result}"
    )


def test_classify_finding_no_context_rule_unknown_rule(make_finding):
    finding = make_finding(
        path="zerver/views.py",
        rule="totally-unknown-non-ruff-rule",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.CASCADE_FIX, (
        f"Expected CASCADE_FIX (unknown rule), got {result}"
    )


def test_classify_finding_refactor_class_takes_precedence(make_finding):
    finding = make_finding(
        path="zerver/views.py",
        rule="xo-max-lines",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.REFACTOR_CLASS, (
        f"Expected REFACTOR_CLASS, got {result}"
    )


def test_classify_finding_claude_fix_takes_precedence(make_finding):
    finding = make_finding(
        path="zerver/views.py",
        rule="type-explicit-any",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, (
        f"Expected CONTEXTUAL_FIX (context rule default), got {result}"
    )


def test_match_context_no_match_returns_none():
    rule = get_context_rule("ruff-c408")
    assert rule is not None
    result = match_context("zerver/views.py", rule)
    assert result is None, f"Expected None for non-matching path, got {result}"


def test_match_context_migration_match():
    rule = get_context_rule("ruff-c408")
    assert rule is not None
    result = match_context(
        "zerver/migrations/0001_initial.py", rule, detected_frameworks=["django"]
    )
    assert result is not None
    assert result.fix_strategy == "skip"
    assert result.framework == "django"


def test_match_context_test_file_match():
    rule = get_context_rule("ruff-c408")
    assert rule is not None
    result = match_context("zerver/test_views.py", rule)
    assert result is not None
    assert result.fix_strategy == "deterministic_safe"


def test_get_context_rule_returns_rule():
    rule = get_context_rule("ruff-c408")
    assert rule is not None
    assert rule.rule == "ruff-c408"
    assert rule.default_strategy == "deterministic"


def test_get_context_rule_unknown_returns_none():
    rule = get_context_rule("ruff-unknown-rule")
    assert rule is None


def test_context_rule_b904_tests_dir(make_finding):
    finding = make_finding(
        path="zerver/tests/test_views.py",
        rule="ruff-b904",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, (
        f"Expected CONTEXTUAL_FIX (default), got {result}"
    )


def test_context_rule_c408_tests_dir(make_finding):
    finding = make_finding(
        path="zerver/tests/test_views.py",
        rule="ruff-c408",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_c408_test_underscore_file(make_finding):
    finding = make_finding(
        path="zerver/views_test.py",
        rule="ruff-c408",
        safe_to_autofix=False,
        quick_win=False,
    )
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"
