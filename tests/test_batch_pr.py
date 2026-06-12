"""Tests for bluei/engine/batch_pr.py — batch PR creation, fix application, and splitting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.models import (
    BatchGroup,
    BatchRule,
    BatchStatus,
    FixResult,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _mf(make_finding, **overrides):
    """Wrap conftest make_finding with batch_pr-specific defaults."""
    defaults = {
        "finding_id": "f001",
        "rule": "ruff-c408",
        "path": "src/lib/foo.py",
        "line": 42,
        "confidence": 0.72,
        "snippet": "dict()",
    }
    defaults.update(overrides)
    return make_finding(**defaults)


def _make_batch(
    make_finding,
    findings=None,
    fix_results=None,
    status="open",
    retry_count=0,
    split_history=None,
    issues=None,
    branch=None,
    worktree_path=None,
    rule_pattern="ruff-c408",
    group_by="rule",
) -> BatchGroup:
    if findings is None:
        findings = [_mf(make_finding)]
    if issues is None:
        issues = [
            {"finding_id": f.finding_id, "id": f"ISS-{i}"}
            for i, f in enumerate(findings)
        ]
    if fix_results is None:
        fix_results = {
            f.finding_id: FixResult(
                finding_id=f.finding_id, status="success", diff_lines=1
            )
            for f in findings
        }
    return BatchGroup(
        batch_id="batch-test-001",
        rule_pattern=rule_pattern,
        group_by=group_by,
        findings=findings,
        issues=issues,
        status=status,
        worktree_path=worktree_path,
        branch=branch,
        fix_results=fix_results,
        retry_count=retry_count,
        split_history=split_history or [],
    )


def _make_mock_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        repo_path=Path("/tmp/repo"),
        state_file=Path("/tmp/state.json"),
        log_file=Path("/tmp/run.log"),
        findings_file=Path("/tmp/findings.jsonl"),
        issues_file=Path("/tmp/issues.json"),
        worktree_root=Path("/tmp/worktrees"),
        open_issues_cap=20,
        open_prs_cap=10,
        issue_confidence_threshold=0.8,
        max_files_changed=8,
        max_loc_diff=500,
        max_prs_per_run=5,
        max_issues_per_run=10,
        finding_cooldown_seconds=14400,
        merge_cooldown_minutes=30,
        max_fix_attempts_per_issue=3,
        docs_index_file=Path("/tmp/docs_index.json"),
        fix_engine="deterministic",
        claude_cmd_template="claude --print",
        run_phase="active-cycle",
        refresh_docs_index=False,
        live_github_actions=False,
        auto_merge_sandbox=False,
        dry_run=True,
        max_split_depth=3,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── chunk_findings ───────────────────────────────────────────────────


class TestChunkFindingsLocSplit:
    def test_loc_soft_cap_splits_chunk(self, make_finding):
        from bluei.engine.batch_pr import chunk_findings

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(10)
        ]
        rule_config = BatchRule(
            rule_pattern="ruff-c408",
            max_batch_size=50,
            max_files_per_batch=50,
            max_loc_per_batch=3,
        )
        chunks = chunk_findings(findings, rule_config)
        assert len(chunks) >= 2

    def test_empty_findings_returns_empty(self):
        from bluei.engine.batch_pr import chunk_findings

        assert chunk_findings([], BatchRule(rule_pattern="x")) == []


# ── verify_finding_closed ────────────────────────────────────────────


class TestVerifyFindingClosed:
    def test_returns_false_on_exception(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import verify_finding_closed

        f = _mf(make_finding)
        log = tmp_path / "log.txt"
        with patch(
            "bluei.engine.validation.verify_fix_closed", side_effect=OSError("boom")
        ):
            assert verify_finding_closed(tmp_path, f, log) is False

    def test_returns_true_when_verify_succeeds(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import verify_finding_closed

        f = _mf(make_finding)
        log = tmp_path / "log.txt"
        with patch("bluei.engine.validation.verify_fix_closed", return_value=True):
            assert verify_finding_closed(tmp_path, f, log) is True


# ── apply_batch_fixes ────────────────────────────────────────────────


class TestApplyBatchFixes:
    def test_success_tally(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import apply_batch_fixes

        f = _mf(make_finding)
        batch = _make_batch(make_finding, findings=[f])
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        result = FixResult(
            finding_id=f.finding_id,
            status="success",
            diff_lines=1,
            fix_method="autofix",
        )
        with patch("bluei.engine.batch_pr._apply_single_fix", return_value=result):
            successes, failures = apply_batch_fixes(
                batch, tmp_path, tmp_path, args, log
            )
        assert successes == 1
        assert failures == 0

    def test_skipped_tally(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import apply_batch_fixes

        f = _mf(make_finding)
        batch = _make_batch(make_finding, findings=[f])
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        result = FixResult(
            finding_id=f.finding_id, status="skipped", error="not-fixable"
        )
        with patch("bluei.engine.batch_pr._apply_single_fix", return_value=result):
            successes, failures = apply_batch_fixes(
                batch, tmp_path, tmp_path, args, log
            )
        assert successes == 0
        assert failures == 0

    def test_failure_tally(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import apply_batch_fixes

        f = _mf(make_finding)
        batch = _make_batch(make_finding, findings=[f])
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        result = FixResult(finding_id=f.finding_id, status="failed", error="bad-fix")
        with patch("bluei.engine.batch_pr._apply_single_fix", return_value=result):
            successes, failures = apply_batch_fixes(
                batch, tmp_path, tmp_path, args, log
            )
        assert successes == 0
        assert failures == 1


# ── _apply_single_fix (autofix path) ────────────────────────────────


class TestApplySingleFixAutofix:
    def test_autofix_success_returns_success(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=True)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with (
            patch("bluei.engine.lifecycle.apply_autofix", return_value=True),
            patch("bluei.engine.batch_pr.verify_finding_closed", return_value=True),
            patch("bluei.engine.constants.load_llm_fixable_rules", return_value=[]),
        ):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "success"
        assert result.fix_method == "autofix"

    def test_autofix_verify_failed(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=True)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with (
            patch("bluei.engine.lifecycle.apply_autofix", return_value=True),
            patch("bluei.engine.batch_pr.verify_finding_closed", return_value=False),
            patch("bluei.engine.constants.load_llm_fixable_rules", return_value=[]),
        ):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "failed"
        assert result.error == "verification-failed"

    def test_autofix_unavailable_contextual_fallback_success(
        self, tmp_path, make_finding
    ):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=True)
        log = tmp_path / "log.txt"
        args = _make_mock_args(fix_engine="deterministic")

        with (
            patch("bluei.engine.lifecycle.apply_autofix", return_value=False),
            patch("bluei.engine.constants.load_llm_fixable_rules", return_value=[]),
            patch("bluei.engine.context_fix.apply_contextual_fix", return_value=True),
            patch("bluei.engine.batch_pr.verify_finding_closed", return_value=True),
        ):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "success"
        assert result.fix_method == "contextual"

    def test_autofix_unavailable_contextual_verify_failed(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=True)
        log = tmp_path / "log.txt"
        args = _make_mock_args(fix_engine="deterministic")

        with (
            patch("bluei.engine.lifecycle.apply_autofix", return_value=False),
            patch("bluei.engine.constants.load_llm_fixable_rules", return_value=[]),
            patch("bluei.engine.context_fix.apply_contextual_fix", return_value=True),
            patch("bluei.engine.batch_pr.verify_finding_closed", return_value=False),
        ):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "failed"
        assert result.error == "contextual-verification-failed"

    def test_not_safe_not_llm_returns_skipped(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=False, rule="unknown-rule")
        log = tmp_path / "log.txt"
        args = _make_mock_args(fix_engine="deterministic")

        with patch("bluei.engine.constants.load_llm_fixable_rules", return_value=[]):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "skipped"
        assert result.error == "not-llm-fixable"


# ── _apply_single_fix (claude path) ─────────────────────────────────


class TestApplySingleFixClaude:
    def test_claude_success_with_commit_change(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=False, rule="complex-rule")
        log = tmp_path / "log.txt"
        args = _make_mock_args(fix_engine="claude")

        mock_result = MagicMock()
        mock_result.stdout = "abc1234\n"
        mock_result.returncode = 0

        with (
            patch(
                "bluei.engine.constants.load_llm_fixable_rules",
                return_value=["complex-rule"],
            ),
            patch("bluei.engine.validation.build_target_checks", return_value=[]),
            patch(
                "bluei.engine.lifecycle.apply_claude_fix",
                return_value=(0, "done", None),
            ),
            patch(
                "subprocess.run",
                side_effect=[
                    mock_result,
                    MagicMock(stdout="def5678\n"),
                ],
            ),
        ):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "success"
        assert result.fix_method == "claude"

    def test_claude_tools_blocked(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=False, rule="complex-rule")
        log = tmp_path / "log.txt"
        args = _make_mock_args(fix_engine="claude")

        with (
            patch(
                "bluei.engine.constants.load_llm_fixable_rules",
                return_value=["complex-rule"],
            ),
            patch("bluei.engine.validation.build_target_checks", return_value=[]),
            patch(
                "bluei.engine.lifecycle.apply_claude_fix",
                return_value=(
                    0,
                    "Edit tool blocked and all file-modifying tools are blocked",
                    None,
                ),
            ),
            patch("subprocess.run", return_value=MagicMock(stdout="abc1234\n")),
        ):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "failed"
        assert result.error == "claude-tools-blocked"

    def test_claude_no_change(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=False, rule="complex-rule")
        log = tmp_path / "log.txt"
        args = _make_mock_args(fix_engine="claude")

        mock_result = MagicMock()
        mock_result.stdout = "abc1234\n"

        with (
            patch(
                "bluei.engine.constants.load_llm_fixable_rules",
                return_value=["complex-rule"],
            ),
            patch("bluei.engine.validation.build_target_checks", return_value=[]),
            patch(
                "bluei.engine.lifecycle.apply_claude_fix",
                return_value=(1, "nope", None),
            ),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "failed"
        assert result.error is not None and "claude rc=1" in result.error

    def test_claude_diff_detection_no_commit_change(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import _apply_single_fix

        f = _mf(make_finding, safe_to_autofix=False, rule="complex-rule")
        log = tmp_path / "log.txt"
        args = _make_mock_args(fix_engine="claude")

        with (
            patch(
                "bluei.engine.constants.load_llm_fixable_rules",
                return_value=["complex-rule"],
            ),
            patch("bluei.engine.validation.build_target_checks", return_value=[]),
            patch(
                "bluei.engine.lifecycle.apply_claude_fix",
                return_value=(0, "applied fix", None),
            ),
            patch(
                "subprocess.run",
                side_effect=[
                    MagicMock(stdout="abc1234\n"),
                    MagicMock(stdout="abc1234\n"),
                    MagicMock(stdout="file.py | 2 +-\n"),
                ],
            ),
        ):
            result = _apply_single_fix(f, tmp_path, tmp_path, args, log)
        assert result.status == "success"


# ── create_batch_pr ──────────────────────────────────────────────────


class TestCreateBatchPr:
    def test_create_pr_success(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import create_batch_pr

        f = _mf(make_finding)
        batch = _make_batch(make_finding, findings=[f])
        log = tmp_path / "log.txt"

        with patch(
            "bluei.engine.utils.run_capture",
            return_value=(0, "https://github.com/org/repo/pull/42\n"),
        ):
            result = create_batch_pr(batch, "org/repo", log)
        assert result["number"] == 42
        assert "pull/42" in result["url"]

    def test_create_pr_failure_raises(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import create_batch_pr

        f = _mf(make_finding)
        batch = _make_batch(make_finding, findings=[f])
        log = tmp_path / "log.txt"

        with patch("bluei.engine.utils.run_capture", return_value=(1, "error")):
            with pytest.raises(RuntimeError, match="Failed to create batch PR"):
                create_batch_pr(batch, "org/repo", log)

    def test_create_pr_malformed_url(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import create_batch_pr

        f = _mf(make_finding)
        batch = _make_batch(make_finding, findings=[f])
        log = tmp_path / "log.txt"

        with patch("bluei.engine.utils.run_capture", return_value=(0, "some output")):
            result = create_batch_pr(batch, "org/repo", log)
        assert result["number"] is None
        assert result["url"] == ""


# ── link_issues_to_batch_pr ──────────────────────────────────────────


class TestLinkIssuesToBatchPr:
    def test_links_issues_and_comments(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import link_issues_to_batch_pr

        f = _mf(make_finding)
        issues = [{"finding_id": f.finding_id, "github": {"issue_number": 10}}]
        batch = _make_batch(make_finding, findings=[f], issues=issues)
        log = tmp_path / "log.txt"

        with (
            patch("bluei.engine.orchestrator.set_issue_status"),
            patch("bluei.engine.gh.gh_issue_comment"),
        ):
            link_issues_to_batch_pr(
                batch, 42, "https://pr.url", "org/repo", tmp_path, log
            )
        assert issues[0]["github"]["pr_number"] == 42
        assert issues[0]["github"]["batch_id"] == "batch-test-001"

    def test_comment_failure_not_fatal(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import link_issues_to_batch_pr

        f = _mf(make_finding)
        issues = [{"finding_id": f.finding_id, "github": {"issue_number": 10}}]
        batch = _make_batch(make_finding, findings=[f], issues=issues)
        log = tmp_path / "log.txt"

        with (
            patch("bluei.engine.orchestrator.set_issue_status"),
            patch("bluei.engine.gh.gh_issue_comment", side_effect=OSError("fail")),
        ):
            link_issues_to_batch_pr(
                batch, 42, "https://pr.url", "org/repo", tmp_path, log
            )

    def test_issue_without_issue_number_skips_comment(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import link_issues_to_batch_pr

        f = _mf(make_finding)
        issues = [{"finding_id": f.finding_id}]
        batch = _make_batch(make_finding, findings=[f], issues=issues)
        log = tmp_path / "log.txt"

        with (
            patch("bluei.engine.orchestrator.set_issue_status"),
            patch("bluei.engine.gh.gh_issue_comment") as mock_gh,
        ):
            link_issues_to_batch_pr(
                batch, 42, "https://pr.url", "org/repo", tmp_path, log
            )
        mock_gh.assert_not_called()


# ── should_split_batch ───────────────────────────────────────────────


class TestShouldSplitBatch:
    def test_zero_attempted_returns_false(self, make_finding):
        from bluei.engine.batch_pr import should_split_batch

        batch = _make_batch(make_finding, fix_results={})
        assert should_split_batch(batch) is False

    def test_below_threshold_returns_false(self, make_finding):
        from bluei.engine.batch_pr import should_split_batch

        f = _mf(make_finding)
        results = {
            f.finding_id: FixResult(finding_id=f.finding_id, status="success"),
        }
        batch = _make_batch(make_finding, findings=[f], fix_results=results)
        assert should_split_batch(batch) is False

    def test_above_threshold_and_below_depth_returns_true(self, make_finding):
        from bluei.engine.batch_pr import should_split_batch

        f1 = _mf(make_finding, finding_id="f1")
        f2 = _mf(make_finding, finding_id="f2")
        results = {
            f1.finding_id: FixResult(finding_id=f1.finding_id, status="failed"),
            f2.finding_id: FixResult(finding_id=f2.finding_id, status="failed"),
        }
        batch = _make_batch(make_finding, findings=[f1, f2], fix_results=results)
        assert should_split_batch(batch) is True

    def test_at_max_depth_returns_false(self, make_finding):
        from bluei.engine.batch_pr import should_split_batch

        f = _mf(make_finding)
        results = {
            f.finding_id: FixResult(finding_id=f.finding_id, status="failed"),
        }
        batch = _make_batch(
            make_finding, findings=[f], fix_results=results, retry_count=3
        )
        assert should_split_batch(batch, max_depth=3) is False


# ── commit_partial_batch ─────────────────────────────────────────────


class TestCommitPartialBatch:
    def test_no_successful_findings_returns_false(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import commit_partial_batch

        log = tmp_path / "log.txt"
        batch = _make_batch(make_finding)
        assert commit_partial_batch([], batch, log) is False

    def test_no_worktree_returns_false(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import commit_partial_batch

        f = _mf(make_finding)
        batch = _make_batch(make_finding, findings=[f], worktree_path=None, branch=None)
        log = tmp_path / "log.txt"
        assert commit_partial_batch([f], batch, log) is False

    def test_no_branch_returns_false(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import commit_partial_batch

        f = _mf(make_finding)
        batch = _make_batch(
            make_finding, findings=[f], worktree_path=tmp_path, branch=None
        )
        log = tmp_path / "log.txt"
        assert commit_partial_batch([f], batch, log) is False

    def test_no_changes_returns_false(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import commit_partial_batch

        f = _mf(make_finding)
        batch = _make_batch(
            make_finding, findings=[f], worktree_path=tmp_path, branch="br"
        )
        log = tmp_path / "log.txt"

        with (
            patch("bluei.engine.utils.run_no_capture"),
            patch("bluei.engine.git_ops.git_commit_all", return_value="no_changes"),
        ):
            assert commit_partial_batch([f], batch, log) is False

    def test_push_failed_returns_false(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import commit_partial_batch

        f = _mf(make_finding)
        batch = _make_batch(
            make_finding, findings=[f], worktree_path=tmp_path, branch="br"
        )
        log = tmp_path / "log.txt"

        with (
            patch("bluei.engine.utils.run_no_capture"),
            patch("bluei.engine.git_ops.git_commit_all", return_value="committed"),
            patch("bluei.engine.git_ops.git_push_branch", return_value=False),
        ):
            assert commit_partial_batch([f], batch, log) is False

    def test_success_returns_true(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import commit_partial_batch

        f = _mf(make_finding)
        batch = _make_batch(
            make_finding, findings=[f], worktree_path=tmp_path, branch="br"
        )
        log = tmp_path / "log.txt"

        with (
            patch("bluei.engine.utils.run_no_capture"),
            patch("bluei.engine.git_ops.git_commit_all", return_value="committed"),
            patch("bluei.engine.git_ops.git_push_branch", return_value=True),
        ):
            assert commit_partial_batch([f], batch, log) is True


# ── split_batch ──────────────────────────────────────────────────────


class TestSplitBatchExtended:
    def test_split_no_failed_returns_empty(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import split_batch

        f = _mf(make_finding)
        results = {f.finding_id: FixResult(finding_id=f.finding_id, status="success")}
        batch = _make_batch(make_finding, findings=[f], fix_results=results)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        assert split_batch(batch, tmp_path, args, log) == []

    def test_split_at_max_depth_returns_empty(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import split_batch

        f = _mf(make_finding)
        results = {f.finding_id: FixResult(finding_id=f.finding_id, status="failed")}
        batch = _make_batch(
            make_finding, findings=[f], fix_results=results, retry_count=3
        )
        log = tmp_path / "log.txt"
        args = _make_mock_args(max_split_depth=3)

        assert split_batch(batch, tmp_path, args, log) == []

    def test_split_dict_results(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import split_batch

        f = _mf(make_finding)
        results = {f.finding_id: {"status": "failed"}}
        batch = _make_batch(
            make_finding, findings=[f], fix_results=results, retry_count=0
        )
        log = tmp_path / "log.txt"
        args = _make_mock_args(max_split_depth=3)

        sub = split_batch(batch, tmp_path, args, log)
        assert len(sub) == 1

    def test_split_many_failures_halves(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import split_batch

        findings = [_mf(make_finding, finding_id=f"f{i}") for i in range(5)]
        results = {
            f.finding_id: FixResult(finding_id=f.finding_id, status="failed")
            for f in findings
        }
        batch = _make_batch(make_finding, findings=findings, fix_results=results)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        sub = split_batch(batch, tmp_path, args, log)
        assert len(sub) >= 2


# ── handle_batch_failure ─────────────────────────────────────────────


class TestHandleBatchFailureExtended:
    def test_separates_success_and_failed(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import handle_batch_failure

        f1 = _mf(make_finding, finding_id="f1")
        f2 = _mf(make_finding, finding_id="f2")
        results = {
            f1.finding_id: FixResult(finding_id=f1.finding_id, status="success"),
            f2.finding_id: FixResult(finding_id=f2.finding_id, status="failed"),
        }
        batch = _make_batch(make_finding, findings=[f1, f2], fix_results=results)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with (
            patch("bluei.engine.batch_pr.commit_partial_batch", return_value=True),
            patch("bluei.engine.batch_pr.split_batch", return_value=[]),
        ):
            sub = handle_batch_failure(batch, tmp_path, args, log)
        assert batch.status == BatchStatus.SPLIT.value
        assert len(batch.split_history) == 1
        assert batch.split_history[0]["successful_count"] == 1
        assert batch.split_history[0]["failed_count"] == 1

    def test_dict_results_handled(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import handle_batch_failure

        f1 = _mf(make_finding, finding_id="f1")
        f2 = _mf(make_finding, finding_id="f2")
        results = {
            f1.finding_id: {"status": "success"},
            f2.finding_id: {"status": "failed"},
        }
        batch = _make_batch(make_finding, findings=[f1, f2], fix_results=results)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with (
            patch("bluei.engine.batch_pr.commit_partial_batch", return_value=True),
            patch("bluei.engine.batch_pr.split_batch", return_value=[]),
        ):
            sub = handle_batch_failure(batch, tmp_path, args, log)
        assert batch.status == BatchStatus.SPLIT.value

    def test_none_result_treated_as_failed(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import handle_batch_failure

        f1 = _mf(make_finding, finding_id="f1")
        batch = _make_batch(make_finding, findings=[f1], fix_results={})
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with (
            patch("bluei.engine.batch_pr.commit_partial_batch", return_value=False),
            patch("bluei.engine.batch_pr.split_batch", return_value=[]),
        ):
            sub = handle_batch_failure(batch, tmp_path, args, log)
        assert batch.split_history[0]["failed_count"] == 1


# ── recover_interrupted_batch ────────────────────────────────────────


class TestRecoverInterruptedBatch:
    def test_batch_not_found_returns_none(self, tmp_path):
        from bluei.engine.batch_pr import recover_interrupted_batch

        bf = tmp_path / "batches.jsonl"
        bf.write_text(json.dumps({"batch_id": "other", "status": "open"}) + "\n")
        assert recover_interrupted_batch("missing", bf, tmp_path) is None

    def test_wrong_status_returns_none(self, tmp_path):
        from bluei.engine.batch_pr import recover_interrupted_batch

        bf = tmp_path / "batches.jsonl"
        bf.write_text(json.dumps({"batch_id": "b1", "status": "open"}) + "\n")
        assert recover_interrupted_batch("b1", bf, tmp_path) is None

    def test_no_worktree_sets_aborted(self, tmp_path):
        from bluei.engine.batch_pr import recover_interrupted_batch

        bf = tmp_path / "batches.jsonl"
        record = {
            "batch_id": "b1",
            "status": "fixing",
            "findings": [{"finding_id": "f1", "path": "a.py", "line": 1, "rule": "r1"}],
        }
        bf.write_text(json.dumps(record) + "\n")
        with patch("bluei.engine.state.update_batch_record"):
            batch = recover_interrupted_batch("b1", bf, tmp_path)
        assert batch is not None
        assert batch.status == BatchStatus.ABORTED.value

    def test_worktree_exists_pushed_sets_pr_created(self, tmp_path):
        from bluei.engine.batch_pr import recover_interrupted_batch

        bf = tmp_path / "batches.jsonl"
        wt = tmp_path / "wt"
        wt.mkdir()
        record = {
            "batch_id": "b1",
            "status": "fixing",
            "worktree_path": str(wt),
            "branch": "br",
            "findings": [{"finding_id": "f1", "path": "a.py", "line": 1, "rule": "r1"}],
        }
        bf.write_text(json.dumps(record) + "\n")
        with (
            patch(
                "bluei.engine.utils.run_capture",
                return_value=(0, "origin/br\nrefs/heads/br\n"),
            ),
            patch("bluei.engine.state.update_batch_record"),
        ):
            batch = recover_interrupted_batch("b1", bf, tmp_path)
        assert batch is not None
        assert batch.status == BatchStatus.PR_CREATED.value

    def test_worktree_exists_not_pushed_sets_aborted(self, tmp_path):
        from bluei.engine.batch_pr import recover_interrupted_batch

        bf = tmp_path / "batches.jsonl"
        wt = tmp_path / "wt"
        wt.mkdir()
        record = {
            "batch_id": "b1",
            "status": "fixing",
            "worktree_path": str(wt),
            "branch": "br",
            "findings": [{"finding_id": "f1", "path": "a.py", "line": 1, "rule": "r1"}],
        }
        bf.write_text(json.dumps(record) + "\n")
        with (
            patch("bluei.engine.utils.run_capture", return_value=(0, "")),
            patch("bluei.engine.utils.run_no_capture"),
            patch("bluei.engine.state.update_batch_record"),
        ):
            batch = recover_interrupted_batch("b1", bf, tmp_path)
        assert batch is not None
        assert batch.status == BatchStatus.ABORTED.value


# ── process_batch ────────────────────────────────────────────────────


class TestProcessBatch:
    def test_solo_batch_returns_delegated(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        f = _mf(make_finding)
        batch = _make_batch(make_finding, findings=[f])
        log = tmp_path / "log.txt"
        args = _make_mock_args()
        ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is False
        assert detail == "solo-delegated"

    def test_worktree_creation_fails(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with patch("bluei.engine.batch_pr._create_worktree", return_value=False):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is False
        assert detail == "worktree-creation-failed"

    def test_no_successful_fixes(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with (
            patch("bluei.engine.batch_pr._create_worktree", return_value=True),
            patch("bluei.engine.worktree.hydrate_worktree"),
            patch("bluei.engine.batch_pr.apply_batch_fixes", return_value=(0, 3)),
            patch("bluei.engine.utils.run_no_capture"),
        ):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is False
        assert detail == "no-successful-fixes"

    def test_commit_no_changes(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with (
            patch("bluei.engine.batch_pr._create_worktree", return_value=True),
            patch("bluei.engine.worktree.hydrate_worktree"),
            patch("bluei.engine.batch_pr.apply_batch_fixes", return_value=(3, 0)),
            patch("bluei.engine.git_ops.git_commit_all", return_value="no_changes"),
            patch("bluei.engine.utils.run_no_capture"),
        ):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is False
        assert detail == "commit-no-changes"

    def test_push_failed(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args()

        with (
            patch("bluei.engine.batch_pr._create_worktree", return_value=True),
            patch("bluei.engine.worktree.hydrate_worktree"),
            patch("bluei.engine.batch_pr.apply_batch_fixes", return_value=(3, 0)),
            patch("bluei.engine.git_ops.git_commit_all", return_value="committed"),
            patch("bluei.engine.git_ops.git_push_branch", return_value=False),
            patch("bluei.engine.utils.run_no_capture"),
        ):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is False
        assert detail == "push-failed"

    def test_dry_run_returns_true(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args(dry_run=True)

        with (
            patch("bluei.engine.batch_pr._create_worktree", return_value=True),
            patch("bluei.engine.worktree.hydrate_worktree"),
            patch("bluei.engine.batch_pr.apply_batch_fixes", return_value=(3, 0)),
            patch("bluei.engine.git_ops.git_commit_all", return_value="committed"),
            patch("bluei.engine.git_ops.git_push_branch", return_value=True),
            patch("bluei.engine.utils.run_no_capture"),
        ):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is True
        assert detail == "dry-run-pr-simulated"

    def test_live_run_pr_creation_fails(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args(dry_run=False)

        with (
            patch("bluei.engine.batch_pr._create_worktree", return_value=True),
            patch("bluei.engine.worktree.hydrate_worktree"),
            patch("bluei.engine.batch_pr.apply_batch_fixes", return_value=(3, 0)),
            patch("bluei.engine.git_ops.git_commit_all", return_value="committed"),
            patch("bluei.engine.git_ops.git_push_branch", return_value=True),
            patch(
                "bluei.engine.gh.get_origin_url", return_value="https://github.com/o/r"
            ),
            patch("bluei.engine.gh.parse_github_repo", return_value=("o", "r")),
            patch("bluei.engine.gh.find_batch_pr_by_rule", return_value=None),
            patch(
                "bluei.engine.batch_pr.create_batch_pr",
                side_effect=RuntimeError("fail"),
            ),
            patch("bluei.engine.utils.run_no_capture"),
        ):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is False
        assert detail == "pr-creation-failed"

    def test_live_run_success(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args(dry_run=False)

        with (
            patch("bluei.engine.batch_pr._create_worktree", return_value=True),
            patch("bluei.engine.worktree.hydrate_worktree"),
            patch("bluei.engine.batch_pr.apply_batch_fixes", return_value=(3, 0)),
            patch("bluei.engine.git_ops.git_commit_all", return_value="committed"),
            patch("bluei.engine.git_ops.git_push_branch", return_value=True),
            patch(
                "bluei.engine.gh.get_origin_url", return_value="https://github.com/o/r"
            ),
            patch("bluei.engine.gh.parse_github_repo", return_value=("o", "r")),
            patch("bluei.engine.gh.find_batch_pr_by_rule", return_value=None),
            patch(
                "bluei.engine.batch_pr.create_batch_pr",
                return_value={"number": 99, "url": "https://pr"},
            ),
            patch("bluei.engine.batch_pr.link_issues_to_batch_pr"),
            patch("bluei.engine.utils.run_no_capture"),
        ):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is True
        assert detail is not None and "99" in detail

    def test_no_repo_slug(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args(dry_run=False)

        with (
            patch("bluei.engine.gh.get_origin_url", return_value="bad-url"),
            patch("bluei.engine.gh.parse_github_repo", return_value=(None, None)),
        ):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is False
        assert detail == "no-repo-slug"

    def test_duplicate_pr_skipped(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch

        findings = [
            _mf(make_finding, finding_id=f"f{i}", path=f"file{i}.py") for i in range(3)
        ]
        batch = _make_batch(make_finding, findings=findings)
        log = tmp_path / "log.txt"
        args = _make_mock_args(dry_run=False)

        with (
            patch(
                "bluei.engine.gh.get_origin_url", return_value="https://github.com/o/r"
            ),
            patch("bluei.engine.gh.parse_github_repo", return_value=("o", "r")),
            patch(
                "bluei.engine.gh.find_batch_pr_by_rule",
                return_value={"number": 10, "url": "u"},
            ),
        ):
            ok, detail = process_batch(batch, tmp_path, args, log)
        assert ok is False
        assert detail is not None and "duplicate" in detail
