"""Tests for bluei.engine.gh — GitHub API interactions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.gh import (
    create_or_update_github_issue,
    create_or_update_github_pr,
    evaluate_pr_check_health,
    evaluate_pr_mergeability,
    evaluate_pr_regression,
    evaluate_pr_reviews,
    fetch_github_live_counts,
    finding_dedupe_marker,
    finding_from_issue_record,
    find_batch_pr_by_rule,
    find_existing_github_issue,
    find_existing_github_pr,
    gh_issue_close,
    gh_issue_comment,
    gh_json,
    gh_pr_comment,
    get_origin_url,
    merge_failure_requires_pr_fix,
    merge_pr,
    parse_github_repo,
    parse_issue_number_from_url,
    parse_pr_number_from_url,
    repo_is_sandbox,
    fetch_open_prs_for_merge,
)


class TestFindExistingGithubIssue:
    """Finding dedupe marker matching in issues."""

    def test_finds_matching_issue(self, tmp_path: Path) -> None:
        finding_id = "finding-abc-123"
        marker = finding_dedupe_marker(finding_id)
        mock_payload = [
            {
                "number": 42,
                "title": "Bug",
                "url": "https://github.com/o/r/issues/42",
                "state": "open",
                "body": f"Some text\n{marker}\nmore text",
            }
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_existing_github_issue("owner/repo", finding_id, cwd=tmp_path)
            assert result is not None
            assert result["number"] == 42

    def test_returns_none_when_no_match(self, tmp_path: Path) -> None:
        mock_payload = [
            {"number": 1, "body": "no marker here"},
            {"number": 2, "body": "also no marker"},
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_existing_github_issue(
                "owner/repo", "finding-xyz", cwd=tmp_path
            )
            assert result is None

    def test_returns_none_on_invalid_payload(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=None):
            result = find_existing_github_issue(
                "owner/repo", "finding-xyz", cwd=tmp_path
            )
            assert result is None

    def test_returns_none_on_empty_payload(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=[]):
            result = find_existing_github_issue(
                "owner/repo", "finding-xyz", cwd=tmp_path
            )
            assert result is None

    def test_handles_missing_body_field(self, tmp_path: Path) -> None:
        finding_id = "finding-42"
        marker = finding_dedupe_marker(finding_id)
        mock_payload = [
            {"number": 7, "body": marker},
            {"number": 8},  # Missing body field
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_existing_github_issue("owner/repo", finding_id, cwd=tmp_path)
            assert result is not None
            assert result["number"] == 7


class TestFindExistingGithubPr:
    """PR dedup logic."""

    def test_finds_matching_pr(self, tmp_path: Path) -> None:
        finding_id = "finding-pr-99"
        marker = finding_dedupe_marker(finding_id)
        mock_payload = [
            {
                "number": 10,
                "title": "Fix",
                "url": "https://github.com/o/r/pull/10",
                "state": "open",
                "body": marker,
                "headRefName": "fix/foo",
            },
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_existing_github_pr("owner/repo", finding_id, cwd=tmp_path)
            assert result is not None
            assert result["number"] == 10

    def test_returns_none_when_no_match(self, tmp_path: Path) -> None:
        mock_payload = [
            {"number": 11, "body": "different finding"},
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_existing_github_pr(
                "owner/repo", "finding-nonexistent", cwd=tmp_path
            )
            assert result is None

    def test_returns_none_on_invalid_payload(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=None):
            result = find_existing_github_pr("owner/repo", "finding-xyz", cwd=tmp_path)
            assert result is None

    def test_dedupe_marker_format(self) -> None:
        marker = finding_dedupe_marker("find-me")
        assert marker == "[finding_id:find-me]"


class TestFindBatchPrByRule:
    """Batch PR dedup with branch prefix matching."""

    def test_finds_matching_batch_pr(self, tmp_path: Path) -> None:
        from datetime import datetime, timezone, timedelta

        recent_date = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        mock_payload = [
            {
                "number": 20,
                "title": "batch fix",
                "headRefName": "qa/batch-c408-2026-05-13",
                "createdAt": recent_date,
                "url": "https://github.com/o/r/pull/20",
            },
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_batch_pr_by_rule(
                "owner/repo", "ruff-c408", cwd=tmp_path, max_age_hours=48
            )
            assert result is not None
            assert result["number"] == 20

    def test_returns_none_for_wrong_branch_prefix(self, tmp_path: Path) -> None:
        mock_payload = [
            {
                "number": 21,
                "headRefName": "other-branch",
                "createdAt": "2025-05-13T00:00:00Z",
                "url": "",
            },
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_batch_pr_by_rule(
                "owner/repo", "ruff-c408", cwd=tmp_path, max_age_hours=48
            )
            assert result is None

    def test_returns_none_for_old_pr(self, tmp_path: Path) -> None:
        mock_payload = [
            {
                "number": 22,
                "headRefName": "qa/batch-c408-old",
                "createdAt": "2025-01-01T00:00:00Z",
                "url": "",
            },
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_batch_pr_by_rule(
                "owner/repo", "ruff-c408", cwd=tmp_path, max_age_hours=1
            )
            assert result is None

    def test_returns_none_on_invalid_payload(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=None):
            result = find_batch_pr_by_rule("owner/repo", "ruff-c408", cwd=tmp_path)
            assert result is None

    def test_returns_none_for_empty_list(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=[]):
            result = find_batch_pr_by_rule("owner/repo", "ruff-c408", cwd=tmp_path)
            assert result is None

    def test_handles_missing_created_at(self, tmp_path: Path) -> None:
        mock_payload = [
            {
                "number": 23,
                "headRefName": "qa/batch-c408-nodate",
                "createdAt": None,
                "url": "",
            },
        ]
        with patch("bluei.engine.gh.gh_json", return_value=mock_payload):
            result = find_batch_pr_by_rule("owner/repo", "ruff-c408", cwd=tmp_path)
            assert result is None


class TestParseIssueNumberFromUrl:
    """URL parsing for issue numbers."""

    def test_parses_valid_url(self) -> None:
        url = "https://github.com/owner/repo/issues/42"
        result = parse_issue_number_from_url(url)
        assert result == 42

    def test_returns_none_for_pr_url(self) -> None:
        url = "https://github.com/owner/repo/pull/42"
        result = parse_issue_number_from_url(url)
        assert result is None

    def test_returns_none_for_other_slug(self) -> None:
        result = parse_issue_number_from_url("https://github.com/o/r/other/42")
        assert result is None

    def test_returns_none_for_none(self) -> None:
        result = parse_issue_number_from_url(None)
        assert result is None

    def test_returns_none_for_empty(self) -> None:
        result = parse_issue_number_from_url("")
        assert result is None

    def test_parses_issue_number_from_any_url(self) -> None:
        """The function extracts any /issues/N from a URL ending, not just GitHub."""
        result = parse_issue_number_from_url("https://example.com/issues/42")
        assert result == 42


class TestParsePrNumberFromUrl:
    """PR URL parsing."""

    def test_parses_valid_url(self) -> None:
        url = "https://github.com/owner/repo/pull/99"
        result = parse_pr_number_from_url(url)
        assert result == 99

    def test_returns_none_for_issue_url(self) -> None:
        url = "https://github.com/owner/repo/issues/99"
        result = parse_pr_number_from_url(url)
        assert result is None

    def test_returns_none_for_none(self) -> None:
        result = parse_pr_number_from_url(None)
        assert result is None

    def test_returns_none_for_empty(self) -> None:
        result = parse_pr_number_from_url("")
        assert result is None

    def test_handles_multiple_digit_numbers(self) -> None:
        url = "https://github.com/o/r/pull/12345"
        result = parse_pr_number_from_url(url)
        assert result == 12345


# --- Phase 10: additional gh module tests ---


class TestGetOriginUrl:
    def test_returns_url_on_success(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.run_capture",
            return_value=(0, "https://github.com/owner/repo.git\n"),
        ):
            result = get_origin_url(tmp_path)
            assert result == "https://github.com/owner/repo.git"

    def test_returns_empty_on_failure(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(1, "")):
            result = get_origin_url(tmp_path)
            assert result == ""


class TestParseGithubRepo:
    def test_https_url(self) -> None:
        owner, repo = parse_github_repo("https://github.com/acme/project")
        assert owner == "acme"
        assert repo == "project"

    def test_https_url_with_git(self) -> None:
        owner, repo = parse_github_repo("https://github.com/acme/project.git")
        assert owner == "acme"
        assert repo == "project"

    def test_ssh_url(self) -> None:
        owner, repo = parse_github_repo("git@github.com:acme/project")
        assert owner == "acme"
        assert repo == "project"

    def test_non_github_url(self) -> None:
        owner, repo = parse_github_repo("https://gitlab.com/acme/project")
        assert owner == ""
        assert repo == ""

    def test_too_many_parts(self) -> None:
        owner, repo = parse_github_repo("https://github.com/a/b/c")
        assert owner == ""
        assert repo == ""

    def test_empty_string(self) -> None:
        owner, repo = parse_github_repo("")
        assert owner == ""
        assert repo == ""


class TestGhJson:
    def test_returns_parsed_json(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(0, '{"key": "value"}')):
            result = gh_json(["gh", "test"], cwd=tmp_path)
            assert result == {"key": "value"}

    def test_returns_none_on_nonzero(self, tmp_path: Path) -> None:
        with (
            patch("bluei.engine.gh.time.sleep"),
            patch("bluei.engine.gh.run_capture", return_value=(1, "error")),
        ):
            result = gh_json(["gh", "test"], cwd=tmp_path)
            assert result is None

    def test_returns_none_on_bad_json(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(0, "not json")):
            result = gh_json(["gh", "test"], cwd=tmp_path)
            assert result is None


class TestGhIssueComment:
    def test_success(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(0, "")):
            assert gh_issue_comment("o/r", 42, "body", tmp_path) is True

    def test_failure(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(1, "error")):
            assert gh_issue_comment("o/r", 42, "body", tmp_path) is False


class TestGhIssueClose:
    def test_success(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(0, "")):
            assert gh_issue_close("o/r", 42, "closing", tmp_path) is True

    def test_failure(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(1, "err")):
            assert gh_issue_close("o/r", 42, "closing", tmp_path) is False


class TestGhPrComment:
    def test_success(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(0, "")):
            assert gh_pr_comment("o/r", 10, "body", tmp_path) is True

    def test_failure(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(1, "err")):
            assert gh_pr_comment("o/r", 10, "body", tmp_path) is False


class TestFindingFromIssueRecord:
    def test_reconstructs_finding(self) -> None:
        issue = {
            "finding_id": "fid-123",
            "path": "src/main.py",
            "rule": "test-rule",
            "snippet": "test snippet",
            "repo": "/tmp/repo",
            "line": 42,
            "confidence": 0.85,
            "quick_win": True,
            "safe_to_autofix": False,
        }
        result = finding_from_issue_record(issue)
        assert result is not None
        assert result.finding_id == "fid-123"
        assert result.path == "src/main.py"
        assert result.rule == "test-rule"

    def test_returns_none_for_missing_fields(self) -> None:
        assert finding_from_issue_record({}) is None
        assert finding_from_issue_record({"finding_id": "x"}) is None
        assert finding_from_issue_record({"finding_id": "x", "path": "p"}) is None

    def test_handles_bad_line_value(self) -> None:
        issue = {
            "finding_id": "fid",
            "path": "p",
            "rule": "r",
            "line": "not_a_number",
        }
        result = finding_from_issue_record(issue)
        assert result is not None
        assert result.line == 0

    def test_handles_bad_confidence_value(self) -> None:
        issue = {
            "finding_id": "fid",
            "path": "p",
            "rule": "r",
            "confidence": "bad",
        }
        result = finding_from_issue_record(issue)
        assert result is not None
        assert result.confidence == 0.0

    def test_rule_aliases(self) -> None:
        issue = {
            "finding_id": "fid",
            "path": "p",
            "rule": "max-lines",
        }
        result = finding_from_issue_record(issue)
        assert result is not None
        assert result.rule == "xo-max-lines"

    def test_default_repo(self) -> None:
        issue = {
            "finding_id": "fid",
            "path": "p",
            "rule": "r",
        }
        result = finding_from_issue_record(issue)
        assert result is not None
        assert result.repo == "qa-sandbox-repo"

    def test_whitespace_fields_stripped(self) -> None:
        issue = {
            "finding_id": "  fid  ",
            "path": "  p  ",
            "rule": "  r  ",
            "snippet": "  s  ",
        }
        result = finding_from_issue_record(issue)
        assert result is not None
        assert result.finding_id == "fid"
        assert result.path == "p"


class TestRepoIsSandbox:
    def test_sandbox_repo(self) -> None:
        assert repo_is_sandbox("owner/qa-sandbox-repo") is True

    def test_non_sandbox_repo(self) -> None:
        assert repo_is_sandbox("owner/real-repo") is False

    def test_self_merge_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLUEI_SELF_MERGE_REPOS", "owner/my-repo,other/repo2")
        assert repo_is_sandbox("owner/my-repo") is True
        assert repo_is_sandbox("other/repo2") is True
        assert repo_is_sandbox("owner/other") is False

    def test_empty_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BLUEI_SELF_MERGE_REPOS", raising=False)
        assert repo_is_sandbox("owner/real-repo") is False


class TestFetchOpenPrsForMerge:
    def test_returns_sorted_prs(self, tmp_path: Path) -> None:
        prs = [
            {"number": 3, "createdAt": "2025-01-03", "isDraft": True},
            {"number": 1, "createdAt": "2025-01-01", "isDraft": False},
            {"number": 2, "createdAt": "2025-01-02", "isDraft": False},
        ]
        with patch("bluei.engine.gh.gh_json", return_value=prs):
            result = fetch_open_prs_for_merge("o/r", tmp_path)
        assert len(result) == 3
        assert result[0]["number"] == 1
        assert result[2]["number"] == 3

    def test_returns_empty_on_invalid_payload(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=None):
            result = fetch_open_prs_for_merge("o/r", tmp_path)
        assert result == []


class TestEvaluatePrCheckHealth:
    def test_no_payload_returns_eligible(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=None):
            result = evaluate_pr_check_health("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert result["has_checks"] is False

    def test_empty_rollup(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value={"statusCheckRollup": []}):
            result = evaluate_pr_check_health("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert result["has_checks"] is False

    def test_failing_check(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            return_value={"statusCheckRollup": [{"conclusion": "FAILURE"}]},
        ):
            result = evaluate_pr_check_health("o/r", 1, tmp_path)
        assert result["eligible"] is False
        assert "FAILURE" in result["reason"]

    def test_pending_check(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            return_value={"statusCheckRollup": [{"state": "PENDING"}]},
        ):
            result = evaluate_pr_check_health("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert "pending" in result["reason"]

    def test_passing_check(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            return_value={"statusCheckRollup": [{"conclusion": "SUCCESS"}]},
        ):
            result = evaluate_pr_check_health("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert "pass" in result["reason"]


class TestEvaluatePrMergeability:
    def test_no_payload_proceeds(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=None):
            result = evaluate_pr_mergeability("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert result["merge_state_status"] == "UNKNOWN"

    def test_dirty_state(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json", return_value={"mergeStateStatus": "DIRTY"}
        ):
            result = evaluate_pr_mergeability("o/r", 1, tmp_path)
        assert result["eligible"] is False
        assert result["requires_pr_fix"] is True

    def test_behind_state(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json", return_value={"mergeStateStatus": "BEHIND"}
        ):
            result = evaluate_pr_mergeability("o/r", 1, tmp_path)
        assert result["eligible"] is False
        assert result["requires_pr_fix"] is True

    def test_unknown_state(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json", return_value={"mergeStateStatus": "UNKNOWN"}
        ):
            result = evaluate_pr_mergeability("o/r", 1, tmp_path)
        assert result["eligible"] is True

    def test_blocked_state(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json", return_value={"mergeStateStatus": "BLOCKED"}
        ):
            result = evaluate_pr_mergeability("o/r", 1, tmp_path)
        assert result["eligible"] is False
        assert result["reason"] == "merge-state-blocked"

    def test_clean_state(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json", return_value={"mergeStateStatus": "CLEAN"}
        ):
            result = evaluate_pr_mergeability("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert "pass" in result["reason"]

    def test_unstable_with_passing_checks(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            side_effect=[
                {"mergeStateStatus": "UNSTABLE"},
                {"statusCheckRollup": [{"conclusion": "SUCCESS"}]},
            ],
        ):
            result = evaluate_pr_mergeability("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert "unstable" in result["reason"]

    def test_unstable_with_failing_checks(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            side_effect=[
                {"mergeStateStatus": "UNSTABLE"},
                {"statusCheckRollup": [{"conclusion": "FAILURE"}]},
            ],
        ):
            result = evaluate_pr_mergeability("o/r", 1, tmp_path)
        assert result["eligible"] is False


class TestEvaluatePrReviews:
    def test_no_payload_blocks(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=None):
            result = evaluate_pr_reviews("o/r", 1, tmp_path)
        assert result["eligible"] is False

    def test_changes_requested(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            return_value={
                "latestReviews": [{"state": "CHANGES_REQUESTED"}],
                "baseRefName": "main",
            },
        ):
            result = evaluate_pr_reviews("o/r", 1, tmp_path)
        assert result["eligible"] is False
        assert "changes-requested" in result["reason"]

    def test_pending_review(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            return_value={
                "latestReviews": [{"state": "PENDING"}],
                "baseRefName": "main",
            },
        ):
            result = evaluate_pr_reviews("o/r", 1, tmp_path)
        assert result["eligible"] is False
        assert "review-pending" in result["reason"]

    def test_approved(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            return_value={
                "latestReviews": [{"state": "APPROVED"}],
                "baseRefName": "main",
            },
        ):
            result = evaluate_pr_reviews("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert "review-check-pass" in result["reason"]

    def test_no_reviews_no_protection(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            side_effect=[
                {"latestReviews": [], "baseRefName": "main"},
                None,
            ],
        ):
            result = evaluate_pr_reviews("o/r", 1, tmp_path)
        assert result["eligible"] is True
        assert "no-reviews-no-protection" in result["reason"]

    def test_no_reviews_with_protection(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            side_effect=[
                {"latestReviews": [], "baseRefName": "main"},
                {"required_pull_request_reviews": {"require_code_owner_reviews": True}},
            ],
        ):
            result = evaluate_pr_reviews("o/r", 1, tmp_path)
        assert result["eligible"] is False
        assert "no-reviews-but-protection" in result["reason"]

    def test_commented_only_treated_as_no_review(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            side_effect=[
                {
                    "latestReviews": [{"state": "COMMENTED"}, {"state": "DISMISSED"}],
                    "baseRefName": "main",
                },
                None,
            ],
        ):
            result = evaluate_pr_reviews("o/r", 1, tmp_path)
        assert result["eligible"] is True

    def test_no_approval_with_substantive_reviews(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.gh_json",
            return_value={
                "latestReviews": [
                    {"state": "APPROVED"},
                    {"state": "CHANGES_REQUESTED"},
                ],
                "baseRefName": "main",
            },
        ):
            result = evaluate_pr_reviews("o/r", 1, tmp_path)
        assert result["eligible"] is False


class TestMergeFailureRequiresPrFix:
    def test_conflict_markers(self) -> None:
        assert merge_failure_requires_pr_fix("merge conflict detected") is True

    def test_behind_branch(self) -> None:
        assert merge_failure_requires_pr_fix("is behind the base branch") is True

    def test_not_mergeable(self) -> None:
        assert merge_failure_requires_pr_fix("not mergeable") is True

    def test_clean_merge(self) -> None:
        assert merge_failure_requires_pr_fix("all good") is False

    def test_whitespace_handling(self) -> None:
        assert merge_failure_requires_pr_fix("  Merge Conflict found  ") is True


class TestMergePr:
    def test_dry_run(self, tmp_path: Path) -> None:
        success, reason = merge_pr("o/r", 42, dry_run=True, cwd=tmp_path)
        assert success is True
        assert "dry-run" in reason

    def test_success(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(0, "merged")):
            success, reason = merge_pr("o/r", 42, dry_run=False, cwd=tmp_path)
        assert success is True
        assert reason == "merged"

    def test_failure(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(1, "merge failed")):
            success, reason = merge_pr("o/r", 42, dry_run=False, cwd=tmp_path)
        assert success is False
        assert "failed" in reason

    def test_failure_empty_output(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.run_capture", return_value=(1, "")):
            success, reason = merge_pr("o/r", 42, dry_run=False, cwd=tmp_path)
        assert success is False
        assert "rc=1" in reason


class TestCreateOrUpdateGithubIssue:
    def test_dry_run_new_issue(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        with patch("bluei.engine.gh.find_existing_github_issue", return_value=None):
            result = create_or_update_github_issue("o/r", finding, True, log, tmp_path)
        assert result["created"] is True
        assert result["number"] is None

    def test_dry_run_existing_issue(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        existing = {"number": 7, "url": "https://github.com/o/r/issues/7"}
        with patch("bluei.engine.gh.find_existing_github_issue", return_value=existing):
            result = create_or_update_github_issue("o/r", finding, True, log, tmp_path)
        assert result["created"] is False
        assert result["number"] == 7

    def test_creates_new_issue(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        url = "https://github.com/o/r/issues/99"
        with (
            patch("bluei.engine.gh.find_existing_github_issue", return_value=None),
            patch("bluei.engine.gh.run_capture", return_value=(0, url)),
        ):
            result = create_or_update_github_issue("o/r", finding, False, log, tmp_path)
        assert result["created"] is True
        assert result["number"] == 99

    def test_create_failure(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        with (
            patch("bluei.engine.gh.find_existing_github_issue", return_value=None),
            patch("bluei.engine.gh.run_capture", return_value=(1, "error")),
        ):
            result = create_or_update_github_issue("o/r", finding, False, log, tmp_path)
        assert result["created"] is False
        assert "error" in result

    def test_create_url_parse_fails_finds_after(
        self, tmp_path: Path, make_finding
    ) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        with (
            patch(
                "bluei.engine.gh.find_existing_github_issue",
                side_effect=[None, {"number": 55, "url": "url"}],
            ),
            patch("bluei.engine.gh.run_capture", return_value=(0, "bad-url")),
        ):
            result = create_or_update_github_issue("o/r", finding, False, log, tmp_path)
        assert result["created"] is True
        assert result["number"] == 55

    def test_comments_existing_issue(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        existing = {"number": 7, "url": "https://github.com/o/r/issues/7"}
        with (
            patch("bluei.engine.gh.find_existing_github_issue", return_value=existing),
            patch(
                "bluei.engine.gh.gh_issue_comment", return_value=True
            ) as mock_comment,
        ):
            result = create_or_update_github_issue("o/r", finding, False, log, tmp_path)
        assert result["created"] is False
        mock_comment.assert_called_once()


class TestCreateOrUpdateGithubPr:
    def test_dry_run_new_pr(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        with patch("bluei.engine.gh.find_existing_github_pr", return_value=None):
            result = create_or_update_github_pr(
                "o/r", finding, "fix-branch", None, True, log, tmp_path
            )
        assert result["created"] is True

    def test_reuses_existing_pr(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        existing = {"number": 15, "url": "https://github.com/o/r/pull/15"}
        with patch("bluei.engine.gh.find_existing_github_pr", return_value=existing):
            result = create_or_update_github_pr(
                "o/r", finding, "fix-branch", None, False, log, tmp_path
            )
        assert result["created"] is False
        assert result["number"] == 15

    def test_creates_new_pr(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        url = "https://github.com/o/r/pull/20"
        with (
            patch("bluei.engine.gh.find_existing_github_pr", return_value=None),
            patch("bluei.engine.gh.run_capture", return_value=(0, url)),
        ):
            result = create_or_update_github_pr(
                "o/r", finding, "fix-branch", 42, False, log, tmp_path
            )
        assert result["created"] is True
        assert result["number"] == 20

    def test_create_failure(self, tmp_path: Path, make_finding) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        with (
            patch("bluei.engine.gh.find_existing_github_pr", return_value=None),
            patch("bluei.engine.gh.run_capture", return_value=(1, "error")),
        ):
            result = create_or_update_github_pr(
                "o/r", finding, "fix-branch", None, False, log, tmp_path
            )
        assert result["created"] is False
        assert "error" in result

    def test_create_url_parse_fails_finds_after(
        self, tmp_path: Path, make_finding
    ) -> None:
        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        with (
            patch(
                "bluei.engine.gh.find_existing_github_pr",
                side_effect=[None, {"number": 33, "url": "url"}],
            ),
            patch("bluei.engine.gh.run_capture", return_value=(0, "bad-url")),
        ):
            result = create_or_update_github_pr(
                "o/r", finding, "fix-branch", None, False, log, tmp_path
            )
        assert result["created"] is True
        assert result["number"] == 33


class TestFetchGithubLiveCounts:
    def test_non_github_origin(self, tmp_path: Path) -> None:
        with patch(
            "bluei.engine.gh.get_origin_url", return_value="https://gitlab.com/o/r"
        ):
            counts, status = fetch_github_live_counts(tmp_path)
        assert counts is None
        assert "non-github" in status

    def test_parse_failure(self, tmp_path: Path) -> None:
        with (
            patch("bluei.engine.gh.get_origin_url", return_value="https://github.com/"),
            patch("bluei.engine.gh.parse_github_repo", return_value=("", "")),
        ):
            counts, status = fetch_github_live_counts(tmp_path)
        assert counts is None
        assert "parse-failed" in status

    def test_api_failure(self, tmp_path: Path) -> None:
        with (
            patch(
                "bluei.engine.gh.get_origin_url", return_value="https://github.com/o/r"
            ),
            patch("bluei.engine.gh.parse_github_repo", return_value=("o", "r")),
            patch("bluei.engine.gh.run_capture", return_value=(1, "error")),
        ):
            counts, status = fetch_github_live_counts(tmp_path)
        assert counts is None
        assert "unavailable" in status

    def test_success(self, tmp_path: Path) -> None:
        payload = {
            "data": {
                "repository": {
                    "issues": {"totalCount": 5},
                    "pullRequests": {"totalCount": 3},
                }
            }
        }
        with (
            patch(
                "bluei.engine.gh.get_origin_url", return_value="https://github.com/o/r"
            ),
            patch("bluei.engine.gh.parse_github_repo", return_value=("o", "r")),
            patch("bluei.engine.gh.run_capture", return_value=(0, json.dumps(payload))),
        ):
            counts, status = fetch_github_live_counts(tmp_path)
        assert counts == {"open_issues": 5, "open_prs": 3}
        assert "live-state" in status

    def test_invalid_response(self, tmp_path: Path) -> None:
        with (
            patch(
                "bluei.engine.gh.get_origin_url", return_value="https://github.com/o/r"
            ),
            patch("bluei.engine.gh.parse_github_repo", return_value=("o", "r")),
            patch("bluei.engine.gh.run_capture", return_value=(0, "not json")),
        ):
            counts, status = fetch_github_live_counts(tmp_path)
        assert counts is None
        assert "invalid" in status


class TestEvaluatePrRegression:
    def test_pr_info_unavailable(self, tmp_path: Path) -> None:
        with patch("bluei.engine.gh.gh_json", return_value=None):
            result = evaluate_pr_regression("o/r", 1, tmp_path)
        assert result["score"] == 0.0
        assert result["action"] == "safe-to-merge"

    def test_full_regression_flow(self, tmp_path: Path) -> None:
        pr_info = {
            "number": 1,
            "headRefName": "fix-branch",
            "baseRefName": "main",
            "title": "Fix",
        }
        diff_output = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- /dev/null\n"
            "+++ b/src/app.py\n"
            "@@ -0,0 +1 @@\n"
            "+x = 1\n"
        )
        with (
            patch(
                "bluei.engine.gh.gh_json",
                side_effect=[
                    pr_info,
                    {"statusCheckRollup": [{"conclusion": "SUCCESS"}]},
                ],
            ),
            patch("bluei.engine.gh.run_capture", return_value=(0, diff_output)),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
            patch("bluei.engine.regression.compute_regression_score", return_value=0.1),
        ):
            result = evaluate_pr_regression("o/r", 1, tmp_path)
        assert result["has_diff"] is True
        assert result["action"] == "safe-to-merge"

    def test_high_score_blocks_merge(self, tmp_path: Path) -> None:
        pr_info = {
            "number": 1,
            "headRefName": "fix",
            "baseRefName": "main",
            "title": "T",
        }
        with (
            patch(
                "bluei.engine.gh.gh_json",
                side_effect=[
                    pr_info,
                    {"statusCheckRollup": []},
                ],
            ),
            patch("bluei.engine.gh.run_capture", return_value=(0, "")),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
            patch("bluei.engine.regression.compute_regression_score", return_value=0.8),
        ):
            result = evaluate_pr_regression("o/r", 1, tmp_path)
        assert result["action"] == "block-merge"

    def test_medium_score_requires_review(self, tmp_path: Path) -> None:
        pr_info = {
            "number": 1,
            "headRefName": "fix",
            "baseRefName": "main",
            "title": "T",
        }
        with (
            patch(
                "bluei.engine.gh.gh_json",
                side_effect=[
                    pr_info,
                    {"statusCheckRollup": []},
                ],
            ),
            patch("bluei.engine.gh.run_capture", return_value=(0, "")),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
            patch("bluei.engine.regression.compute_regression_score", return_value=0.4),
        ):
            result = evaluate_pr_regression("o/r", 1, tmp_path)
        assert result["action"] == "review-required"

    def test_test_deletion_detected(self, tmp_path: Path) -> None:
        pr_info = {
            "number": 1,
            "headRefName": "fix",
            "baseRefName": "main",
            "title": "T",
        }
        diff_output = (
            "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
            "--- a/tests/test_foo.py\n"
            "+++ /dev/null\n"
        )
        with (
            patch(
                "bluei.engine.gh.gh_json",
                side_effect=[
                    pr_info,
                    {"statusCheckRollup": []},
                ],
            ),
            patch("bluei.engine.gh.run_capture", return_value=(0, diff_output)),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
            patch("bluei.engine.regression.compute_regression_score", return_value=0.1),
        ):
            result = evaluate_pr_regression("o/r", 1, tmp_path)
        assert "tests/test_foo.py" in result["removed_tests"]
        assert result["has_regressions"] is True

    def test_export_change_detected(self, tmp_path: Path) -> None:
        pr_info = {
            "number": 1,
            "headRefName": "fix",
            "baseRefName": "main",
            "title": "T",
        }
        export_findings = [{"type": "module_init_deleted", "path": "src/mod.py"}]
        with (
            patch(
                "bluei.engine.gh.gh_json",
                side_effect=[
                    pr_info,
                    {"statusCheckRollup": []},
                ],
            ),
            patch("bluei.engine.gh.run_capture", return_value=(0, "")),
            patch(
                "bluei.engine.regression._find_export_changes",
                return_value=export_findings,
            ),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
            patch("bluei.engine.regression.compute_regression_score", return_value=0.1),
        ):
            result = evaluate_pr_regression("o/r", 1, tmp_path)
        assert len(result["export_changes"]) == 1
        assert (
            "module_init_deleted" in result["export_changes"][0].lower()
            or "Init module deleted" in result["export_changes"][0]
        )

    def test_no_diff(self, tmp_path: Path) -> None:
        pr_info = {
            "number": 1,
            "headRefName": "fix",
            "baseRefName": "main",
            "title": "T",
        }
        with (
            patch(
                "bluei.engine.gh.gh_json",
                side_effect=[
                    pr_info,
                    {"statusCheckRollup": []},
                ],
            ),
            patch("bluei.engine.gh.run_capture", return_value=(1, "")),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
            patch("bluei.engine.regression.compute_regression_score", return_value=0.0),
        ):
            result = evaluate_pr_regression("o/r", 1, tmp_path)
        assert result["has_diff"] is False
