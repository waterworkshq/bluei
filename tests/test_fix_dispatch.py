"""Tests for bluei.engine.fix_dispatch — unified fix dispatch."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.fix_dispatch import (
    FixDispatchResult,
    FixStrategy,
    dispatch_fix,
)


@pytest.fixture
def sample_finding():
    from bluei.engine.models import Finding

    return Finding(
        finding_id="test-finding-1",
        repo="test-repo",
        path="test.py",
        line=10,
        rule="test-rule",
        snippet="def test(): pass",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )


@pytest.fixture
def mock_paths(tmp_path):
    return {
        "repo_path": tmp_path / "repo",
        "worktree_path": tmp_path / "worktree",
        "log_file": tmp_path / "log.txt",
        "pattern_store_path": tmp_path / "patterns",
    }


class TestFixDispatchResult:
    def test_default_values(self):
        result = FixDispatchResult(success=True)
        assert result.success is True
        assert result.method_used is None
        assert result.error is None
        assert result.replay_pattern_id is None
        assert result.cost == 0.0

    def test_with_error(self):
        result = FixDispatchResult(success=False, error="test error")
        assert result.success is False
        assert result.error == "test error"


class TestDispatchMinimal:
    @patch("bluei.engine.fix_dispatch._try_autofix", return_value=True)
    def test_autofix_succeeds(self, mock_autofix, sample_finding, mock_paths):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.MINIMAL,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
        )
        assert result.success is True
        assert result.method_used == "autofix"
        mock_autofix.assert_called_once()

    @patch("bluei.engine.fix_dispatch._try_autofix", return_value=False)
    def test_autofix_fails(self, mock_autofix, sample_finding, mock_paths):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.MINIMAL,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
        )
        assert result.success is False
        assert result.error == "autofix-failed"

    @patch("bluei.engine.fix_dispatch._try_autofix", return_value=True)
    @patch(
        "bluei.engine.fix_dispatch._try_pattern_replay", return_value=(True, "pid-1")
    )
    def test_replay_succeeds(
        self, mock_replay, mock_autofix, sample_finding, mock_paths
    ):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.MINIMAL,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
            pattern_store=MagicMock(),
        )
        assert result.success is True
        assert result.method_used == "replay"
        assert result.replay_pattern_id == "pid-1"
        mock_autofix.assert_not_called()


class TestDispatchAutofixFirst:
    @patch("bluei.engine.fix_dispatch._try_autofix", return_value=True)
    def test_autofix_succeeds(self, mock_autofix, sample_finding, mock_paths):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.AUTOFIX_FIRST,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
        )
        assert result.success is True
        assert result.method_used == "autofix"

    @patch("bluei.engine.fix_dispatch._try_contextual_fix", return_value=True)
    @patch("bluei.engine.fix_dispatch._try_autofix", return_value=False)
    def test_contextual_fallback(
        self, mock_autofix, mock_contextual, sample_finding, mock_paths
    ):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.AUTOFIX_FIRST,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
        )
        assert result.success is True
        assert result.method_used == "contextual"

    @patch("bluei.engine.fix_dispatch._try_claude_fix", return_value=(0, "", None))
    @patch("bluei.engine.fix_dispatch._try_contextual_fix", return_value=False)
    @patch("bluei.engine.fix_dispatch._try_autofix", return_value=False)
    def test_claude_fallback(
        self, mock_autofix, mock_contextual, mock_claude, sample_finding, mock_paths
    ):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.AUTOFIX_FIRST,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
            claude_cmd_template="echo test",
        )
        assert result.success is True
        assert result.method_used == "claude"


class TestDispatchCascadeFirst:
    @patch("bluei.engine.fix_dispatch._try_cascade_fix", return_value=True)
    def test_cascade_succeeds(self, mock_cascade, sample_finding, mock_paths):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.CASCADE_FIRST,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
        )
        assert result.success is True
        assert result.method_used == "cascade"

    @patch("bluei.engine.fix_dispatch._try_claude_fix", return_value=(0, "", None))
    @patch("bluei.engine.fix_dispatch._try_cascade_fix", return_value=False)
    def test_claude_fallback(
        self, mock_cascade, mock_claude, sample_finding, mock_paths
    ):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.CASCADE_FIRST,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
            baseline_checks={},
            target_checks={},
            claude_cmd_template="echo test",
        )
        assert result.success is True
        assert result.method_used == "claude"


class TestDispatchContextAware:
    @patch("bluei.engine.fix_dispatch._try_contextual_fix", return_value=True)
    def test_contextual_succeeds(self, mock_contextual, sample_finding, mock_paths):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.CONTEXT_AWARE,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
        )
        assert result.success is True
        assert result.method_used == "contextual"

    @patch("bluei.engine.fix_dispatch._try_contextual_fix", return_value=False)
    def test_contextual_fails(self, mock_contextual, sample_finding, mock_paths):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.CONTEXT_AWARE,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
        )
        assert result.success is False
        assert result.error == "contextual-failed"


class TestDispatchReplayFirst:
    @patch(
        "bluei.engine.fix_dispatch._try_pattern_replay", return_value=(True, "pid-1")
    )
    def test_replay_succeeds(self, mock_replay, sample_finding, mock_paths):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.REPLAY_FIRST,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
            pattern_store=MagicMock(),
        )
        assert result.success is True
        assert result.method_used == "replay"
        assert result.replay_pattern_id == "pid-1"

    @patch("bluei.engine.fix_dispatch._try_autofix", return_value=True)
    @patch("bluei.engine.fix_dispatch._try_pattern_replay", return_value=(False, None))
    def test_autofix_fallback(
        self, mock_replay, mock_autofix, sample_finding, mock_paths
    ):
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy=FixStrategy.REPLAY_FIRST,
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
            pattern_store=MagicMock(),
        )
        assert result.success is True
        assert result.method_used == "autofix"

    def test_unknown_strategy(self, sample_finding, mock_paths):
        # Create a mock strategy that's not in the enum
        result = dispatch_fix(
            finding=sample_finding,
            worktree_path=mock_paths["worktree_path"],
            strategy="invalid_strategy",
            repo_path=mock_paths["repo_path"],
            log_file=mock_paths["log_file"],
        )
        assert result.success is False
        assert "unknown-strategy" in result.error
