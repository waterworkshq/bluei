"""Tests for the contextual fix engine."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from bluei.engine.context_fix import (
    apply_contextual_fix,
    build_contextual_prompt,
)
from bluei.engine.models import Finding
from bluei.engine.reforge import (
    ContextOverride,
    get_context_rule,
    match_context,
    RefactorClass,
    classify_finding,
)


class TestApplyContextualFixDeterministicSafe:
    """Tests for deterministic_safe strategy routing."""

    def test_deterministic_safe_calls_apply_autofix(self, make_finding):
        """When context match is deterministic_safe, apply_autofix is called."""
        finding = make_finding(rule="ruff-b007", path="analytics/lib/fixtures.py")
        log_file = Path(tempfile.mktemp())
        log_file.touch()
        worktree = Path(tempfile.mkdtemp())

        with patch("bluei.engine.lifecycle.apply_autofix") as mock_autofix:
            mock_autofix.return_value = True
            result = apply_contextual_fix(
                repo_path=Path("/tmp/test-repo"),
                finding=finding,
                log_file=log_file,
                worktree_path=worktree,
            )
            assert result is True
            mock_autofix.assert_called_once()

    def test_no_context_rule_falls_back_to_autofix(self, make_finding):
        """When no context rule exists, falls back to apply_autofix."""
        finding = make_finding(rule="unknown-rule", path="some/file.py")
        log_file = Path(tempfile.mktemp())
        log_file.touch()
        worktree = Path(tempfile.mkdtemp())

        with patch("bluei.engine.lifecycle.apply_autofix") as mock_autofix:
            mock_autofix.return_value = True
            result = apply_contextual_fix(
                repo_path=Path("/tmp/test-repo"),
                finding=finding,
                log_file=log_file,
                worktree_path=worktree,
            )
            assert result is True
            mock_autofix.assert_called_once()

    def test_no_context_match_falls_back_to_autofix(self, make_finding):
        """When no context override matches, falls back to apply_autofix."""
        finding = make_finding(
            rule="ruff-c408", path="app/views/home.py"
        )  # not a migration or test
        log_file = Path(tempfile.mktemp())
        log_file.touch()
        worktree = Path(tempfile.mkdtemp())

        with patch("bluei.engine.lifecycle.apply_autofix") as mock_autofix:
            mock_autofix.return_value = True
            result = apply_contextual_fix(
                repo_path=Path("/tmp/test-repo"),
                finding=finding,
                log_file=log_file,
                worktree_path=worktree,
            )
            assert result is True
            mock_autofix.assert_called_once()


class TestApplyContextualFixSkip:
    """Tests for skip strategy routing."""

    def test_skip_strategy_returns_false(self, make_finding):
        """When context match is skip, returns False without attempting fix."""
        finding = make_finding(rule="ruff-s311", path="core/security/crypto.py")
        log_file = Path(tempfile.mktemp())
        log_file.touch()
        worktree = Path(tempfile.mkdtemp())

        with patch("bluei.engine.lifecycle.apply_autofix") as mock_autofix:
            result = apply_contextual_fix(
                repo_path=Path("/tmp/test-repo"),
                finding=finding,
                log_file=log_file,
                worktree_path=worktree,
            )
            assert result is False
            mock_autofix.assert_not_called()

    def test_c408_in_migration_is_skipped(self, make_finding):
        """ruff-c408 in Django migrations should be skipped (unsafe context)."""
        finding = make_finding(
            rule="ruff-c408", path="analytics/migrations/0001_initial.py"
        )
        log_file = Path(tempfile.mktemp())
        log_file.touch()
        worktree = Path(tempfile.mkdtemp())

        with patch("bluei.engine.lifecycle.apply_autofix") as mock_autofix:
            result = apply_contextual_fix(
                repo_path=Path("/tmp/test-repo"),
                finding=finding,
                log_file=log_file,
                worktree_path=worktree,
                detected_frameworks=["django"],
            )
            assert result is False
            mock_autofix.assert_not_called()


class TestApplyContextualFixLLMWithContext:
    """Tests for llm_with_context strategy routing."""

    def test_llm_with_context_calls_claude(self, make_finding):
        """When context match is llm_with_context, apply_claude_fix is called."""
        finding = make_finding(rule="ruff-b904", path="zerver/middleware.py")
        log_file = Path(tempfile.mktemp())
        log_file.touch()
        worktree = Path(tempfile.mkdtemp())

        with patch("bluei.engine.lifecycle.apply_claude_fix") as mock_claude:
            mock_claude.return_value = (0, "output", "prompt_file")
            result = apply_contextual_fix(
                repo_path=Path("/tmp/test-repo"),
                finding=finding,
                log_file=log_file,
                worktree_path=worktree,
                detected_frameworks=["django"],
            )
            assert result is True
            mock_claude.assert_called_once()
            call_kwargs = mock_claude.call_args.kwargs
            assert "prompt" in call_kwargs
            assert "zerver/middleware.py" in call_kwargs["prompt"]


class TestBuildContextualPrompt:
    """Tests for prompt construction."""

    def test_includes_finding_details(self, make_finding):
        """Prompt includes finding rule, path, snippet, and line."""
        finding = make_finding(
            rule="ruff-b904",
            path="zerver/middleware.py",
            snippet="Sample ruff-b904 issue",
            line=10,
        )
        context = ContextOverride(
            file_patterns=["**/middleware*.py"],
            framework="django",
            fix_strategy="llm_with_context",
            prompt_hint="Preserve exception chain semantics.",
        )
        prompt = build_contextual_prompt(finding, context)
        assert "ruff-b904" in prompt
        assert "zerver/middleware.py" in prompt
        assert "Sample ruff-b904 issue" in prompt
        assert "Line: 10" in prompt

    def test_includes_framework(self, make_finding):
        """Prompt includes framework information."""
        finding = make_finding(rule="ruff-b904", path="zerver/middleware.py")
        context = ContextOverride(
            file_patterns=["**/middleware*.py"],
            framework="django",
            fix_strategy="llm_with_context",
        )
        prompt = build_contextual_prompt(finding, context)
        assert "Framework: django" in prompt

    def test_includes_safety_guidance(self, make_finding):
        """Prompt includes safety guidance from context rule."""
        finding = make_finding(rule="ruff-b904", path="zerver/middleware.py")
        context = ContextOverride(
            file_patterns=["**/middleware*.py"],
            framework="django",
            fix_strategy="llm_with_context",
            prompt_hint="Preserve exception chain semantics.",
        )
        prompt = build_contextual_prompt(finding, context)
        assert "Preserve exception chain semantics." in prompt

    def test_default_safety_guidance_when_none(self, make_finding):
        """Prompt includes default safety guidance when prompt_hint is None."""
        finding = make_finding(rule="ruff-b904", path="zerver/middleware.py")
        context = ContextOverride(
            file_patterns=["**/middleware*.py"],
            framework="django",
            fix_strategy="llm_with_context",
            prompt_hint=None,
        )
        prompt = build_contextual_prompt(finding, context)
        assert "Apply the fix while respecting codebase conventions." in prompt

    def test_includes_instructions(self, make_finding):
        """Prompt includes fix instructions."""
        finding = make_finding(rule="ruff-b904", path="zerver/middleware.py")
        context = ContextOverride(
            file_patterns=["**/middleware*.py"],
            framework="django",
            fix_strategy="llm_with_context",
        )
        prompt = build_contextual_prompt(finding, context)
        assert "Read the file to understand the surrounding context" in prompt
        assert "Apply the minimal fix" in prompt
        assert "Do NOT change unrelated code" in prompt


class TestPrCycleRouting:
    """Tests for pr-cycle routing of CONTEXTUAL_FIX findings."""

    def test_contextual_fix_not_skipped_in_queue_eligibility(self, make_finding):
        """CONTEXTUAL_FIX findings should not be skipped in pr-cycle queue eligibility."""
        # b904 without safe_to_autofix would normally be skipped
        # but with context rules, it should be CONTEXTUAL_FIX
        finding = make_finding(
            rule="ruff-b904", path="zerver/middleware.py", safe_to_autofix=False
        )
        result = classify_finding(finding)
        # This should be CONTEXTUAL_FIX due to the context rule for b904
        # (default_strategy is llm_with_context)
        assert result == RefactorClass.CONTEXTUAL_FIX
