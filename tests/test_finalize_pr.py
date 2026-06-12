"""Tests for _finalize_pr_for_issue — PR finalization after fix verification."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.models import Finding
from bluei.engine.commands.pr_cycle import FinalizeResult, _finalize_pr_for_issue


@pytest.fixture
def sample_finding():
    return Finding(
        finding_id="test-finding-12345678",
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
def finalize_args(tmp_path):
    """Common arguments for _finalize_pr_for_issue."""
    issue = {
        "id": "ISS-1",
        "issue_id": "ISS-1",
        "github": {"issue_number": 42, "issue_url": "https://github.com/test/issue/42"},
    }
    issue_github = issue["github"]
    state = {"finding_activity": {}}

    return SimpleNamespace(
        issue=issue,
        issue_github=issue_github,
        issue_number=42,
        issue_url="https://github.com/test/issue/42",
        worktree_path=tmp_path / "worktree",
        worktree_branch="bluei/test-branch",
        repo_path=tmp_path / "repo",
        gh_repo_slug="owner/repo",
        log_file=tmp_path / "log.txt",
        state=state,
    )


class TestFinalizeResult:
    def test_default_values(self):
        result = FinalizeResult(success=True)
        assert result.success is True
        assert result.pr_number is None
        assert result.pr_url == ""
        assert result.run_status == ""
        assert result.should_break is False
        assert result.fixes_verified_delta == 0
        assert result.fixes_failed_verification_delta == 0
        assert result.created_prs_delta == 0
        assert result.open_prs_delta == 0
        assert result.blocked_reasons_additions == []


class TestFinalizeNoChanges:
    """Test the no_changes path (resolved-verified-noop)."""

    @patch("bluei.engine.commands.pr_cycle.git_commit_all", return_value="no_changes")
    @patch("bluei.engine.commands.pr_cycle.set_issue_status")
    @patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
    @patch("bluei.engine.commands.pr_cycle.gh_issue_comment")
    def test_no_changes_returns_success(
        self,
        mock_comment,
        mock_mark,
        mock_status,
        mock_commit,
        sample_finding,
        finalize_args,
    ):
        args = SimpleNamespace(live_github_actions=True, dry_run=False)

        result = _finalize_pr_for_issue(
            issue=finalize_args.issue,
            issue_github=finalize_args.issue_github,
            issue_number=finalize_args.issue_number,
            issue_url=finalize_args.issue_url,
            finding=sample_finding,
            worktree_path=finalize_args.worktree_path,
            worktree_branch=finalize_args.worktree_branch,
            repo_path=finalize_args.repo_path,
            gh_repo_slug=finalize_args.gh_repo_slug,
            log_file=finalize_args.log_file,
            args=args,
            state=finalize_args.state,
        )

        assert result.success is True
        assert result.run_status == "resolved-verified-noop"
        assert result.fixes_verified_delta == 1
        assert result.should_break is False
        mock_mark.assert_called_once()
        mock_comment.assert_called_once()


class TestFinalizeCommitFailed:
    """Test the commit_failed path."""

    @patch("bluei.engine.commands.pr_cycle.git_commit_all", return_value="error")
    @patch("bluei.engine.commands.pr_cycle.set_issue_status")
    def test_commit_failed_should_break(
        self, mock_status, mock_commit, sample_finding, finalize_args
    ):
        args = SimpleNamespace(live_github_actions=True, dry_run=False)

        result = _finalize_pr_for_issue(
            issue=finalize_args.issue,
            issue_github=finalize_args.issue_github,
            issue_number=finalize_args.issue_number,
            issue_url=finalize_args.issue_url,
            finding=sample_finding,
            worktree_path=finalize_args.worktree_path,
            worktree_branch=finalize_args.worktree_branch,
            repo_path=finalize_args.repo_path,
            gh_repo_slug=finalize_args.gh_repo_slug,
            log_file=finalize_args.log_file,
            args=args,
            state=finalize_args.state,
        )

        assert result.success is False
        assert result.should_break is True
        assert result.run_status == "needs-human-commit-failed"
        assert result.fixes_failed_verification_delta == 1
        assert "needs-human-commit-failed" in result.blocked_reasons_additions


class TestFinalizePushFailed:
    """Test the push_failed path."""

    @patch("bluei.engine.commands.pr_cycle.git_commit_all", return_value="committed")
    @patch("bluei.engine.commands.pr_cycle.git_push_branch", return_value=False)
    @patch("bluei.engine.commands.pr_cycle.set_issue_status")
    def test_push_failed_should_break(
        self, mock_status, mock_push, mock_commit, sample_finding, finalize_args
    ):
        args = SimpleNamespace(live_github_actions=True, dry_run=False)

        result = _finalize_pr_for_issue(
            issue=finalize_args.issue,
            issue_github=finalize_args.issue_github,
            issue_number=finalize_args.issue_number,
            issue_url=finalize_args.issue_url,
            finding=sample_finding,
            worktree_path=finalize_args.worktree_path,
            worktree_branch=finalize_args.worktree_branch,
            repo_path=finalize_args.repo_path,
            gh_repo_slug=finalize_args.gh_repo_slug,
            log_file=finalize_args.log_file,
            args=args,
            state=finalize_args.state,
        )

        assert result.success is False
        assert result.should_break is True
        assert result.run_status == "needs-human-push-failed"
        assert result.fixes_failed_verification_delta == 1
        assert "needs-human-push-failed" in result.blocked_reasons_additions


class TestFinalizeSuccessLive:
    """Test successful PR creation in live mode."""

    @patch("bluei.engine.commands.pr_cycle.git_commit_all", return_value="committed")
    @patch("bluei.engine.commands.pr_cycle.git_push_branch", return_value=True)
    @patch("bluei.engine.commands.pr_cycle.create_or_update_github_pr")
    @patch("bluei.engine.commands.pr_cycle.set_issue_status")
    @patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
    @patch("bluei.engine.commands.pr_cycle.gh_issue_comment")
    @patch("bluei.engine.commands.pr_cycle.gh_pr_comment")
    def test_live_pr_creation_success(
        self,
        mock_pr_comment,
        mock_issue_comment,
        mock_mark,
        mock_status,
        mock_create_pr,
        mock_push,
        mock_commit,
        sample_finding,
        finalize_args,
    ):
        mock_create_pr.return_value = {
            "number": 99,
            "url": "https://github.com/test/pull/99",
        }
        args = SimpleNamespace(live_github_actions=True, dry_run=False)

        result = _finalize_pr_for_issue(
            issue=finalize_args.issue,
            issue_github=finalize_args.issue_github,
            issue_number=finalize_args.issue_number,
            issue_url=finalize_args.issue_url,
            finding=sample_finding,
            worktree_path=finalize_args.worktree_path,
            worktree_branch=finalize_args.worktree_branch,
            repo_path=finalize_args.repo_path,
            gh_repo_slug=finalize_args.gh_repo_slug,
            log_file=finalize_args.log_file,
            args=args,
            state=finalize_args.state,
        )

        assert result.success is True
        assert result.pr_number == 99
        assert result.pr_url == "https://github.com/test/pull/99"
        assert result.run_status == "pr-live-created"
        assert result.fixes_verified_delta == 1
        assert result.created_prs_delta == 1
        assert result.open_prs_delta == 1
        assert result.should_break is False
        assert finalize_args.issue_github["pr_number"] == 99
        assert finalize_args.issue_github["pr_url"] == "https://github.com/test/pull/99"


class TestFinalizeNonLive:
    """Test non-live (dry-run) path."""

    @patch("bluei.engine.commands.pr_cycle.set_issue_status")
    @patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
    def test_non_live_success(
        self, mock_mark, mock_status, sample_finding, finalize_args
    ):
        args = SimpleNamespace(live_github_actions=False, dry_run=True)

        result = _finalize_pr_for_issue(
            issue=finalize_args.issue,
            issue_github=finalize_args.issue_github,
            issue_number=finalize_args.issue_number,
            issue_url=finalize_args.issue_url,
            finding=sample_finding,
            worktree_path=finalize_args.worktree_path,
            worktree_branch=finalize_args.worktree_branch,
            repo_path=finalize_args.repo_path,
            gh_repo_slug=finalize_args.gh_repo_slug,
            log_file=finalize_args.log_file,
            args=args,
            state=finalize_args.state,
        )

        assert result.success is True
        assert result.pr_number is None
        assert result.pr_url == ""
        assert result.run_status == "pr-simulated-resolved-verified"
        assert result.fixes_verified_delta == 1
        assert result.created_prs_delta == 1
        assert result.open_prs_delta == 1
        assert result.should_break is False
        # Should have created a state entry
        assert len(finalize_args.state.get("created", [])) == 1
