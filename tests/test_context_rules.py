#!/usr/bin/env python3
"""Tests for context rule registry and classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'core'))

from sandbox_local_runner.models import Finding
from sandbox_local_runner.reforge import (
    ContextOverride,
    ContextRule,
    RefactorClass,
    classify_finding,
    get_context_rule,
    match_context,
)


def make_finding(
    path: str,
    rule: str,
    safe_to_autofix: bool = False,
    finding_id: str = "test-1",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        repo="test-repo",
        path=path,
        line=10,
        rule=rule,
        snippet="test snippet",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=safe_to_autofix,
    )


def test_context_rule_c408_django_migration():
    finding = make_finding("zerver/migrations/0001_initial.py", "ruff-c408")
    result = classify_finding(finding)
    assert result == RefactorClass.REFACTOR_CLASS, f"Expected REFACTOR_CLASS, got {result}"


def test_context_rule_c408_test_file():
    finding = make_finding("zerver/test_views.py", "ruff-c408")
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_c408_app_code():
    finding = make_finding("zerver/views.py", "ruff-c408")
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX (default), got {result}"


def test_context_rule_b904_middleware():
    finding = make_finding("zerver/middleware.py", "ruff-b904")
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, f"Expected CONTEXTUAL_FIX, got {result}"


def test_context_rule_b904_app_code():
    finding = make_finding("zerver/views.py", "ruff-b904")
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, f"Expected CONTEXTUAL_FIX (default), got {result}"


def test_context_rule_b007_fixtures():
    finding = make_finding("zerver/fixtures.py", "ruff-b007")
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_b007_app_code():
    finding = make_finding("zerver/views.py", "ruff-b007")
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, f"Expected CONTEXTUAL_FIX (default), got {result}"


def test_context_rule_s311_test_file():
    finding = make_finding("zerver/test_views.py", "ruff-s311")
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_s311_fixtures():
    finding = make_finding("zerver/fixtures.py", "ruff-s311")
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_s311_app_code():
    finding = make_finding("zerver/views.py", "ruff-s311")
    result = classify_finding(finding)
    assert result == RefactorClass.CLAUDE_FIX, f"Expected CLAUDE_FIX (default=skip), got {result}"


def test_classify_finding_no_context_rule_unknown_rule():
    finding = make_finding("zerver/views.py", "ruff-unknown-rule")
    result = classify_finding(finding)
    assert result == RefactorClass.CLAUDE_FIX, f"Expected CLAUDE_FIX (unknown rule), got {result}"


def test_classify_finding_refactor_class_takes_precedence():
    finding = make_finding("zerver/views.py", "xo-max-lines")
    result = classify_finding(finding)
    assert result == RefactorClass.REFACTOR_CLASS, f"Expected REFACTOR_CLASS, got {result}"


def test_classify_finding_claude_fix_takes_precedence():
    finding = make_finding("zerver/views.py", "type-explicit-any")
    result = classify_finding(finding)
    assert result == RefactorClass.CLAUDE_FIX, f"Expected CLAUDE_FIX, got {result}"


def test_match_context_no_match_returns_none():
    rule = get_context_rule("ruff-c408")
    assert rule is not None
    result = match_context("zerver/views.py", rule)
    assert result is None, f"Expected None for non-matching path, got {result}"


def test_match_context_migration_match():
    rule = get_context_rule("ruff-c408")
    assert rule is not None
    result = match_context("zerver/migrations/0001_initial.py", rule)
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


def test_context_rule_b904_tests_dir():
    finding = make_finding("zerver/tests/test_views.py", "ruff-b904")
    result = classify_finding(finding)
    assert result == RefactorClass.CONTEXTUAL_FIX, f"Expected CONTEXTUAL_FIX (default), got {result}"


def test_context_rule_c408_tests_dir():
    finding = make_finding("zerver/tests/test_views.py", "ruff-c408")
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"


def test_context_rule_c408_test_underscore_file():
    finding = make_finding("zerver/views_test.py", "ruff-c408")
    result = classify_finding(finding)
    assert result == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {result}"