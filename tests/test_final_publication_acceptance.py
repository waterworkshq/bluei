"""Final publication acceptance for _finalize_pr_for_issue.

Stage-1 boundary: a run that commits and pushes but fails to obtain a PR
number must be reported as blocked/failed — it cannot claim a locally
verified fix, count a verified fix, or post positive comments. These tests
are parent-isolated: they call the finalizer directly and mock every
git/gh side effect; nothing here exercises _process_one_issue.
"""

from types import SimpleNamespace
from unittest.mock import patch

from bluei.engine.commands.pr_cycle import _finalize_pr_for_issue
# Fixtures are owned by this feature's tests; reuse rather than duplicate.
from tests.test_finalize_pr import finalize_args, sample_finding


def _finalize(fa, finding, args):
    return _finalize_pr_for_issue(
        issue=fa.issue,
        issue_github=fa.issue_github,
        issue_number=fa.issue_number,
        issue_url=fa.issue_url,
        finding=finding,
        worktree_path=fa.worktree_path,
        worktree_branch=fa.worktree_branch,
        repo_path=fa.repo_path,
        gh_repo_slug=fa.gh_repo_slug,
        log_file=fa.log_file,
        args=args,
        state=fa.state,
    )


@patch("bluei.engine.commands.pr_cycle.git_commit_all", return_value="committed")
@patch("bluei.engine.commands.pr_cycle.git_push_branch", return_value=True)
@patch("bluei.engine.commands.pr_cycle.create_or_update_github_pr")
@patch("bluei.engine.commands.pr_cycle.set_issue_status")
@patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
@patch("bluei.engine.commands.pr_cycle.gh_issue_comment")
@patch("bluei.engine.commands.pr_cycle.gh_pr_comment")
def test_pr_number_none_is_blocked_failure(
    mock_pr_comment, mock_issue_comment, mock_mark, mock_status,
    mock_create_pr, mock_push, mock_commit, sample_finding, finalize_args,
):
    """Commit+push succeed but PR creation yields no number: fail loudly,
    claim nothing, comment nothing positive."""
    mock_create_pr.return_value = {"number": None, "url": ""}
    args = SimpleNamespace(live_github_actions=True, dry_run=False)

    result = _finalize(finalize_args, sample_finding, args)

    assert result.success is False
    assert result.should_break is True
    assert result.run_status == "needs-human-pr-creation-failed"
    assert result.fixes_failed_verification_delta == 1
    assert result.fixes_verified_delta == 0
    assert result.created_prs_delta == 0
    assert "needs-human-pr-creation-failed" in result.blocked_reasons_additions
    # No positive comments on failure.
    mock_issue_comment.assert_not_called()
    mock_pr_comment.assert_not_called()
    # Cannot claim a local verified fix: no publication entry, no learning.
    assert "created" not in finalize_args.state
    mock_mark.assert_not_called()


@patch("bluei.engine.commands.pr_cycle.git_commit_all", return_value="committed")
@patch("bluei.engine.commands.pr_cycle.git_push_branch", return_value=True)
@patch("bluei.engine.commands.pr_cycle.create_or_update_github_pr")
@patch("bluei.engine.commands.pr_cycle.set_issue_status")
@patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
@patch("bluei.engine.commands.pr_cycle.gh_issue_comment")
@patch("bluei.engine.commands.pr_cycle.gh_pr_comment")
def test_live_pr_success_counts_one_verified(
    mock_pr_comment, mock_issue_comment, mock_mark, mock_status,
    mock_create_pr, mock_push, mock_commit, sample_finding, finalize_args,
):
    mock_create_pr.return_value = {"number": 7, "url": "https://github.com/test/pull/7"}
    args = SimpleNamespace(live_github_actions=True, dry_run=False)

    result = _finalize(finalize_args, sample_finding, args)

    assert result.success is True
    assert result.run_status == "pr-live-created"
    assert result.fixes_verified_delta == 1
    assert result.fixes_failed_verification_delta == 0
    assert result.created_prs_delta == 1
    assert result.open_prs_delta == 1
    assert result.should_break is False


@patch("bluei.engine.commands.pr_cycle.git_commit_all", return_value="no_changes")
@patch("bluei.engine.commands.pr_cycle.set_issue_status")
@patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
@patch("bluei.engine.commands.pr_cycle.gh_issue_comment")
def test_no_changes_success_without_verified_count(
    mock_comment, mock_mark, mock_status, mock_commit,
    sample_finding, finalize_args,
):
    """Already-closed finding is a no-op success: verified counter stays 0
    and learning records the noop action, not a verified fix."""
    args = SimpleNamespace(live_github_actions=True, dry_run=False)

    result = _finalize(finalize_args, sample_finding, args)

    assert result.success is True
    assert result.run_status == "resolved-verified-noop"
    assert result.fixes_verified_delta == 0
    assert result.created_prs_delta == 0
    mock_mark.assert_called_once()
    assert mock_mark.call_args.kwargs["action"] == "resolved-verified-noop"


@patch("bluei.engine.commands.pr_cycle.git_commit_all", return_value="committed")
@patch("bluei.engine.commands.pr_cycle.git_push_branch", return_value=True)
@patch("bluei.engine.commands.pr_cycle.create_or_update_github_pr")
@patch("bluei.engine.commands.pr_cycle.set_issue_status")
@patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
def test_dry_run_without_pr_does_not_trip_failure_gate(
    mock_mark, mock_status, mock_create_pr, mock_push, mock_commit,
    sample_finding, finalize_args,
):
    """The failure gate is live-publication-only: in dry-run a missing PR
    number is the expected local path, not a blocked failure."""
    mock_create_pr.return_value = {"number": None, "url": ""}
    args = SimpleNamespace(live_github_actions=True, dry_run=True)

    result = _finalize(finalize_args, sample_finding, args)

    assert result.success is True
    assert result.should_break is False
    assert result.run_status == "local-resolved-verified"
    assert finalize_args.state["created"][0]["type"] == "local_fix"
