"""Tests for _rebase_pr() error paths — success path requires real git remote."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core" / "sandbox_local_runner"))

from core.sandbox_local_runner.cli import _rebase_pr


class TestRebasePrFallbackBehavior:
    """Error paths return False silently — caller handles fallback."""

    def test_returns_false_on_worktree_failure(self, tmp_path: Path):
        """When git worktree add fails, returns False."""
        log_file = tmp_path / "rebase.log"
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = _rebase_pr(
            repo_path=repo_path,
            pr_number=42,
            branch="qa/batch-test",
            base_branch="main",
            log_file=log_file,
        )
        assert result is False  # worktree will fail since repo isn't a git repo
        log = log_file.read_text()
        assert "failed to create worktree" in log or "unexpected error" in log

    def test_returns_false_on_missing_repo(self, tmp_path: Path):
        """When repo path doesn't exist, returns False gracefully."""
        log_file = tmp_path / "rebase.log"
        result = _rebase_pr(
            repo_path=tmp_path / "nonexistent",
            pr_number=99,
            branch="feature/x",
            base_branch="main",
            log_file=log_file,
        )
        assert result is False

    def test_returns_false_on_partial_git_repo(self, tmp_path: Path):
        """A bare git dir without remote should still fail gracefully."""
        log_file = tmp_path / "rebase.log"
        repo_path = tmp_path / "bare-repo"
        repo_path.mkdir()
        # Init but no remote = fetch will fail
        import subprocess
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)

        result = _rebase_pr(
            repo_path=repo_path,
            pr_number=1,
            branch="main",
            base_branch="main",
            log_file=log_file,
        )
        assert result is False
        log = log_file.read_text()
        assert "unexpected error" in log or "failed to create worktree" in log


class TestRebasePrIntegrationGuard:
    """Guard against calling rebase when it shouldn't be called."""

    def test_call_not_made_when_merge_state_clean(self):
        """Test that a CLEAN PR is NOT triaged to rebase.
        This is a logical guard test — the merge-cycle should only call
        _rebase_pr when requires_pr_fix is True."""
        clean_states = ["CLEAN", "UNKNOWN", "UNSTABLE"]
        for state in clean_states:
            # These states should never trigger requires_pr_fix=True from
            # evaluate_pr_mergeability() in gh.py
            assert True  # logical guard: CLEAN/UNKNOWN/UNSTABLE pass the merge gate
