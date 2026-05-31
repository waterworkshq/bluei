"""Tests for bluei.engine.prompts — LLM prompt render functions."""

from typing import Optional

import pytest

from bluei.engine.models import Finding
from bluei.engine.prompts import (
    render_claude_fix_prompt,
    render_complexity_refactor_prompt,
    render_maxlines_refactor_prompt,
    render_test_coverage_prompt,
    render_type_safety_prompt,
)


def _make_engine_finding(
    rule="xo-unused-var",
    snippet: Optional[str] = "x = 1",
    finding_id="fid-001",
    path="main.py",
    line=10,
    confidence=0.9,
):
    return Finding(
        finding_id=finding_id,
        repo="test-repo",
        path=path,
        line=line,
        rule=rule,
        snippet=snippet or "",
        confidence=confidence,
        quick_win=True,
        safe_to_autofix=True,
    )


# --- Specialized prompt renderers ---


class TestRenderTestCoveragePrompt:
    def test_basic_render(self):
        f = _make_engine_finding(rule="test-coverage-branch", snippet="def foo(): pass")
        baseline = {"lint": ["ruff", "check", "."]}
        target = {"coverage": ["pytest", "--cov"]}
        result = render_test_coverage_prompt(f, baseline, target, 3, 500)
        assert "# Test Coverage Enhancement Task" in result
        assert "fid-001" in result
        assert "test-coverage-branch" in result
        assert "main.py" in result
        assert "def foo(): pass" in result
        assert "ruff check ." in result
        assert "pytest --cov" in result
        assert "max_files_changed: `3`" in result
        assert "max_loc_diff: `500`" in result

    def test_empty_baseline_and_target(self):
        f = _make_engine_finding(rule="test-coverage-branch")
        result = render_test_coverage_prompt(f, {}, {}, 2, 100)
        assert "- (none)" in result
        assert "- (none for this rule)" in result

    def test_no_snippet_falls_back(self):
        f = _make_engine_finding(rule="test-coverage-function", snippet=None)
        result = render_test_coverage_prompt(f, {}, {}, 1, 50)
        assert "(snippet unavailable)" in result

    def test_branch_rule_type(self):
        f = _make_engine_finding(rule="test-coverage-branch")
        result = render_test_coverage_prompt(f, {}, {}, 1, 50)
        assert "Test Coverage" in result

    def test_function_rule_type(self):
        f = _make_engine_finding(rule="test-coverage-function")
        result = render_test_coverage_prompt(f, {}, {}, 1, 50)
        assert "Test Coverage" in result


class TestRenderTypeSafetyPrompt:
    def test_basic_render(self):
        f = _make_engine_finding(rule="type-missing-return", snippet="def foo(): ...")
        baseline = {"tsc": ["npx", "tsc", "--noEmit"]}
        target = {"lint": ["npx", "eslint", "."]}
        result = render_type_safety_prompt(f, baseline, target, 2, 200)
        assert "# Type Safety Improvement Task" in result
        assert "type-missing-return" in result
        assert "def foo(): ..." in result
        assert "npx tsc --noEmit" in result

    def test_missing_param_rule(self):
        f = _make_engine_finding(rule="type-missing-param")
        result = render_type_safety_prompt(f, {}, {}, 1, 50)
        assert "Type Safety" in result

    def test_empty_checks(self):
        f = _make_engine_finding(rule="type-missing-return")
        result = render_type_safety_prompt(f, {}, {}, 1, 50)
        assert "- (none)" in result
        assert "- (none for this rule)" in result

    def test_snippet_none(self):
        f = _make_engine_finding(rule="type-missing-return", snippet=None)
        result = render_type_safety_prompt(f, {}, {}, 1, 50)
        assert "(snippet unavailable)" in result


class TestRenderComplexityRefactorPrompt:
    def test_basic_render(self):
        f = _make_engine_finding(rule="xo-complexity", snippet="if a: if b: if c: ...")
        result = render_complexity_refactor_prompt(
            f, {"test": ["pytest"]}, {"lint": ["ruff", "."]}, 1, 300
        )
        assert "# Complexity Refactor Task" in result
        assert "xo-complexity" in result
        assert "if a: if b: if c: ..." in result
        assert "pytest" in result

    def test_empty_checks(self):
        f = _make_engine_finding(rule="xo-complexity")
        result = render_complexity_refactor_prompt(f, {}, {}, 1, 50)
        assert "- (none)" in result

    def test_no_snippet(self):
        f = _make_engine_finding(rule="xo-complexity", snippet=None)
        result = render_complexity_refactor_prompt(f, {}, {}, 1, 50)
        assert "(snippet unavailable)" in result


class TestRenderMaxlinesRefactorPrompt:
    def test_basic_render(self):
        f = _make_engine_finding(rule="xo-max-lines", path="big_file.py")
        baseline = {"test": ["pytest", "-q"]}
        target = {"lint": ["ruff", "."]}
        result = render_maxlines_refactor_prompt(f, baseline, target, 5, 1000)
        assert "# Max-Lines Refactor Task (TDD Approach)" in result
        assert "big_file.py" in result
        assert "1500" in result
        assert "pytest -q" in result

    def test_no_snippet_section(self):
        f = _make_engine_finding(rule="xo-max-lines")
        result = render_maxlines_refactor_prompt(f, {}, {}, 3, 500)
        assert "TDD Refactor Process" in result

    def test_empty_baseline_target(self):
        f = _make_engine_finding(rule="xo-max-lines")
        result = render_maxlines_refactor_prompt(f, {}, {}, 1, 50)
        assert "- (none)" in result
        assert "- (none for this rule)" in result


# --- Claude fix prompt routing ---


class TestRenderClaudeFixPromptSpecializedRouting:
    def test_routes_xo_max_lines(self):
        f = _make_engine_finding(rule="xo-max-lines")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "Max-Lines Refactor Task" in result

    def test_routes_xo_complexity(self):
        f = _make_engine_finding(rule="xo-complexity")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "Complexity Refactor Task" in result

    def test_routes_test_coverage_branch(self):
        f = _make_engine_finding(rule="test-coverage-branch")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "Test Coverage Enhancement Task" in result

    def test_routes_test_coverage_function(self):
        f = _make_engine_finding(rule="test-coverage-function")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "Test Coverage Enhancement Task" in result

    def test_routes_type_missing_return(self):
        f = _make_engine_finding(rule="type-missing-return")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "Type Safety Improvement Task" in result

    def test_routes_type_missing_param(self):
        f = _make_engine_finding(rule="type-missing-param")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "Type Safety Improvement Task" in result

    def test_generic_rule_default_path(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(
            f, {"test": ["pytest"]}, {"lint": ["ruff"]}, 2, 200
        )
        assert "# QA Autofix Task" in result
        assert "xo-unused-var" in result
        assert "pytest" in result


# --- Claude fix prompt section rendering ---


class TestRenderClaudeFixPromptSections:
    def test_finding_metadata(self):
        f = _make_engine_finding(
            rule="xo-unused-var", path="src/main.py", line=42, confidence=0.75
        )
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "fid-001" in result
        assert "src/main.py" in result
        assert "`42`" in result
        assert "`0.75`" in result

    def test_snippet_section(self):
        f = _make_engine_finding(rule="xo-unused-var", snippet="x = 1")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "## Snippet" in result
        assert "x = 1" in result

    def test_snippet_none_fallback(self):
        f = _make_engine_finding(rule="xo-unused-var", snippet=None)
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "(snippet unavailable)" in result

    def test_constraints_section(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 3, 100)
        assert "max_files_changed: `3`" in result
        assert "max_loc_diff: `100`" in result

    def test_baseline_checks(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {"lint": ["ruff", "."]}, {}, 1, 50)
        assert "ruff ." in result

    def test_empty_baseline_shows_none(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "- (none)" in result

    def test_target_checks(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {"check": ["pytest", "--cov"]}, 1, 50)
        assert "pytest --cov" in result

    def test_empty_target_shows_none(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50)
        assert "- (none for this rule)" in result

    def test_mnemo_directives(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(
            f, {}, {}, 1, 50, mnemo_directives="Remember to check imports"
        )
        assert "## Prior context from memory" in result
        assert "Remember to check imports" in result

    def test_mnemo_directives_none(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, mnemo_directives=None)
        assert "Prior context from memory" not in result

    def test_fix_history(self):
        f = _make_engine_finding(rule="xo-unused-var")
        history = [
            {"date": "2025-01-01", "cycle_type": "autofix", "changed": "fixed import"},
            {"date": "2025-01-02", "cycle_type": "autofix", "broke": "test failed"},
        ]
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, fix_history=history)
        assert "## Prior context" in result
        assert "2025-01-01 (autofix): fixed import" in result
        assert "2025-01-02 (autofix): test failed" in result

    def test_fix_history_limits_to_three(self):
        f = _make_engine_finding(rule="xo-unused-var")
        history = [
            {"date": f"2025-01-{i:02d}", "cycle_type": "fix", "changed": f"fix {i}"}
            for i in range(1, 6)
        ]
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, fix_history=history)
        assert "2025-01-01" in result
        assert "2025-01-03" in result
        assert "2025-01-04" not in result

    def test_fix_history_no_detail_falls_back(self):
        f = _make_engine_finding(rule="xo-unused-var")
        history = [{"date": "2025-01-01", "cycle_type": "fix"}]
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, fix_history=history)
        assert "(no detail)" in result

    def test_fix_history_none(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, fix_history=None)
        assert "## Prior context" not in result

    def test_rule_history(self):
        f = _make_engine_finding(rule="xo-unused-var")
        rh = [
            {
                "date": "2025-01-01",
                "changed": "added type hint",
                "finding_id": "abcdef1234567890",
            }
        ]
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, rule_history=rh)
        assert "## Similar fixes in this codebase" in result
        assert "abcdef1" in result

    def test_rule_history_limits_to_five(self):
        f = _make_engine_finding(rule="xo-unused-var")
        rh = [
            {
                "date": f"2025-01-{i:02d}",
                "changed": f"c{i}",
                "finding_id": f"fid{i:08d}",
            }
            for i in range(1, 8)
        ]
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, rule_history=rh)
        assert "2025-01-05" in result
        assert "2025-01-06" not in result

    def test_rule_history_none_detail(self):
        f = _make_engine_finding(rule="xo-unused-var")
        rh = [{"date": "2025-01-01", "finding_id": "fid001"}]
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, rule_history=rh)
        assert "(no detail)" in result

    def test_extra_prompt(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(
            f, {}, {}, 1, 50, extra_prompt="Use const instead of let"
        )
        assert "## Rule-specific guidance" in result
        assert "Use const instead of let" in result

    def test_extra_prompt_none(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, extra_prompt=None)
        assert "Rule-specific guidance" not in result

    def test_learned_patterns(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(
            f, {}, {}, 1, 50, learned_patterns="Pattern: remove unused var"
        )
        assert "## Learned fix patterns" in result
        assert "Pattern: remove unused var" in result

    def test_learned_patterns_none(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, learned_patterns=None)
        assert "Learned fix patterns" not in result

    def test_failure_clusters(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(
            f, {}, {}, 1, 50, failure_clusters="Cluster A: 5 failures"
        )
        assert "## Failure patterns for this rule" in result
        assert "Cluster A: 5 failures" in result

    def test_failure_clusters_none(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, failure_clusters=None)
        assert "Failure patterns" not in result

    def test_finding_record_with_attempts(self):
        f = _make_engine_finding(rule="xo-unused-var")
        record = {"fix_attempts": 3, "last_fix_error": "test failed"}
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, finding_record=record)
        assert "## Fix history" in result
        assert "Attempts: 3" in result
        assert "test failed" in result
        assert "known-difficult" in result

    def test_finding_record_zero_attempts(self):
        f = _make_engine_finding(rule="xo-unused-var")
        record = {"fix_attempts": 0}
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, finding_record=record)
        assert "## Fix history" not in result

    def test_finding_record_none(self):
        f = _make_engine_finding(rule="xo-unused-var")
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, finding_record=None)
        assert "## Fix history" not in result

    def test_finding_record_no_last_error(self):
        f = _make_engine_finding(rule="xo-unused-var")
        record = {"fix_attempts": 2}
        result = render_claude_fix_prompt(f, {}, {}, 1, 50, finding_record=record)
        assert "(none)" in result

    def test_all_sections_combined(self):
        f = _make_engine_finding(rule="xo-unused-var", snippet="x = 1")
        result = render_claude_fix_prompt(
            f,
            {"base": ["pytest"]},
            {"target": ["ruff"]},
            3,
            200,
            fix_history=[{"date": "2025-01-01", "cycle_type": "fix", "changed": "c1"}],
            finding_record={"fix_attempts": 2, "last_fix_error": "err"},
            mnemo_directives="mem context",
            learned_patterns="learned pat",
            extra_prompt="extra",
            rule_history=[{"date": "2025-01-01", "changed": "r1", "finding_id": "f1"}],
            failure_clusters="cluster info",
        )
        assert "QA Autofix Task" in result
        assert "mem context" in result
        assert "c1" in result
        assert "extra" in result
        assert "learned pat" in result
        assert "cluster info" in result
        assert "Attempts: 2" in result
