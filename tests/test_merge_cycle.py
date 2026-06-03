"""Tests for bluei.engine.commands.merge_cycle — merge eligibility, cooldown, triage."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.merge_cycle import run_merge_cycle_phase


def _args(**overrides):
    defaults = dict(
        auto_merge_sandbox=True,
        auto_rebase_enabled=False,
        dry_run=False,
        live_github_actions=True,
        merge_cooldown_minutes=30,
        open_prs_cap=5,
        rebase_max_prs=5,
        rebase_stats_file=None,
        simulate_open_issues=False,
        simulate_open_prs=False,
        regression_check=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _pr(
    number=42,
    created_hours_ago=2,
    is_draft=False,
    url="https://github.com/acme/widget/pull/42",
    head_branch="fix-1",
    base_branch="main",
):
    now = datetime.now(timezone.utc)
    return {
        "number": number,
        "url": url,
        "isDraft": is_draft,
        "createdAt": (now - timedelta(hours=created_hours_ago)).isoformat(),
        "headRefName": head_branch,
        "baseRefName": base_branch,
    }


class TestMergeCycleSkips:
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_skips_when_auto_merge_disabled(self, mock_log):
        result = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(auto_merge_sandbox=False),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=1,
            blocked_reasons=[],
            reconcile_event={},
        )
        _, succeeded, _, _, _, _, blocked, _ = result
        assert succeeded == 0
        assert any("auto-merge" in b for b in blocked)

    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=False)
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_skips_non_sandbox_repo(self, mock_log, mock_sandbox):
        result = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/production-repo",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=1,
            blocked_reasons=[],
            reconcile_event={},
        )
        _, succeeded, _, _, _, _, blocked, _ = result
        assert succeeded == 0
        assert any("not sandbox" in b for b in blocked)


class TestMergeCycleElibility:
    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch(
        "bluei.engine.commands.merge_cycle.merge_pr",
        return_value=(True, "merge-commit"),
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_mergeability",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_merges_eligible_pr(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_mb,
        mock_merge,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        result = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        _, succeeded, attempts, pr_urls, _, _, _, _ = result
        assert succeeded == 1
        assert attempts == 1

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    def test_skips_draft_pr(self, mock_fetch, mock_sb, mock_log, mock_slog, mock_recon):
        mock_fetch.return_value = [_pr(is_draft=True)]
        result = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        _, succeeded, _, _, _, _, _, _ = result
        assert succeeded == 0

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    def test_skips_pr_in_cooldown(
        self, mock_fetch, mock_sb, mock_log, mock_slog, mock_recon
    ):
        mock_fetch.return_value = [_pr(created_hours_ago=0.1)]
        result = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(merge_cooldown_minutes=30),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        _, succeeded, _, _, _, _, _, _ = result
        assert succeeded == 0

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.commands.helpers._append_text")
    @patch("bluei.engine.state._append_text")
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_mergeability",
        return_value={
            "eligible": False,
            "reason": "MERGE_CONFLICT",
            "requires_pr_fix": True,
            "merge_state_status": "DIRTY",
        },
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_triages_merge_conflict_back_to_fix(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_mb,
        mock_hlog,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        result = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": [{"issue_id": "i1", "github": {"pr_number": 42}}]},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        _, succeeded, _, _, _, _, _, _ = result
        assert succeeded == 0


class TestMergeDecisionLogic:
    """Verify key decision points in the merge evaluation pipeline."""

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": False, "reason": "CI-failing"},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    def test_check_health_failure_blocks_merge(
        self, mock_fetch, mock_sb, mock_ch, mock_log, mock_slog, mock_recon
    ):
        mock_fetch.return_value = [_pr()]
        _, _, _, _, _, _, blocked, _ = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        assert any("merge-block" in b and "CI-failing" in b for b in blocked)

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.commands.merge_cycle.merge_pr", return_value=(True, "merged"))
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_mergeability",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle._autonomous_review_gate_passes",
        return_value=(True, "auto"),
    )
    @patch("bluei.engine.commands.merge_cycle._load_review_state", return_value={})
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": False, "reason": "no-approval"},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_autonomous_gate_bypasses_review_block(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_lrs,
        mock_gate,
        mock_mb,
        mock_merge,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        _, succeeded, _, _, _, _, _, _ = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        assert succeeded == 1
        mock_merge.assert_called_once()

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch(
        "bluei.engine.commands.merge_cycle._autonomous_review_gate_passes",
        return_value=(False, "needs-human"),
    )
    @patch("bluei.engine.commands.merge_cycle._load_review_state", return_value={})
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": False, "reason": "no-approvals"},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_review_block_without_autonomous_bypass_fails(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_lrs,
        mock_gate,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        _, succeeded, _, _, _, _, blocked, _ = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        assert succeeded == 0
        assert any("merge-block" in b for b in blocked)

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.commands.merge_cycle.merge_pr", return_value=(True, "merged"))
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_mergeability",
        return_value={
            "eligible": False,
            "reason": "unknown",
            "merge_state_status": "UNKNOWN",
        },
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_unknown_mergeability_normalized_to_cautious_pass(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_mb,
        mock_merge,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        _, succeeded, _, _, _, _, _, _ = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        assert succeeded == 1

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.commands.helpers._append_text")
    @patch("bluei.engine.state._append_text")
    @patch(
        "bluei.engine.commands.merge_cycle.merge_failure_requires_pr_fix",
        return_value=True,
    )
    @patch(
        "bluei.engine.commands.merge_cycle.merge_pr", return_value=(False, "conflict")
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_mergeability",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_merge_failure_needing_fix_triages_back(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_mb,
        mock_merge,
        mock_req_fix,
        mock_hlog,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        _, succeeded, _, _, _, _, _, _ = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": [{"issue_id": "i1", "github": {"pr_number": 42}}]},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        assert succeeded == 0

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch(
        "bluei.engine.commands.merge_cycle.merge_failure_requires_pr_fix",
        return_value=False,
    )
    @patch(
        "bluei.engine.commands.merge_cycle.merge_pr",
        return_value=(False, "network-error"),
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_mergeability",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_merge_failure_no_fix_counts_as_failed(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_mb,
        mock_merge,
        mock_req_fix,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        failed, _, _, _, _, _, blocked, _ = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        assert failed == 1
        assert any("merge-failed" in b for b in blocked)

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch(
        "bluei.engine.commands.merge_cycle.sweep_rebase",
        return_value={"rebased": [], "conflicted": [], "skipped": []},
    )
    @patch("bluei.engine.commands.merge_cycle.merge_pr", return_value=(True, "merged"))
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_mergeability",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_rebase_sweep_on_merge_when_enabled(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_mb,
        mock_merge,
        mock_rebase,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        result = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(auto_rebase_enabled=True),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=1,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        _, succeeded, attempts, _, _, _, _, _ = result
        assert succeeded == 1
        assert attempts == 1
        mock_rebase.assert_called_once()

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.commands.merge_cycle.merge_pr", return_value=(True, "merged"))
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_mergeability",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_reviews",
        return_value={"eligible": True},
    )
    @patch(
        "bluei.engine.commands.merge_cycle.evaluate_pr_check_health",
        return_value={"eligible": True, "has_checks": True},
    )
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch("bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge")
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_merge_sets_issue_status_resolved(
        self,
        mock_log,
        mock_fetch,
        mock_sb,
        mock_ch,
        mock_rv,
        mock_mb,
        mock_merge,
        mock_slog,
        mock_recon,
    ):
        mock_fetch.return_value = [_pr()]
        with patch("bluei.engine.orchestrator.set_issue_status") as mock_set:
            run_merge_cycle_phase(
                repo_path=Path("/repo"),
                log_file=Path("/tmp/test.log"),
                review_state_file=Path("/review.json"),
                state={},
                issues_data={
                    "issues": [{"issue_id": "i1", "github": {"pr_number": 42}}]
                },
                args=_args(),
                gh_repo_slug="acme/sandbox-qa-widget",
                merges_failed=0,
                merges_succeeded=0,
                merge_attempts=0,
                merged_pr_urls=[],
                open_prs=1,
                open_issues=0,
                blocked_reasons=[],
                reconcile_event={},
            )
        resolved = [c for c in mock_set.call_args_list if c[0][1] == "resolved_merged"]
        assert len(resolved) == 1

    @patch(
        "bluei.engine.commands.merge_cycle.reconcile_open_workload",
        return_value=(0, 0, {}),
    )
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.commands.merge_cycle.repo_is_sandbox", return_value=True)
    @patch(
        "bluei.engine.commands.merge_cycle.fetch_open_prs_for_merge", return_value=[]
    )
    @patch("bluei.engine.commands.merge_cycle._append_text")
    def test_no_open_prs_returns_clean(
        self, mock_log, mock_fetch, mock_sb, mock_slog, mock_recon
    ):
        _, succeeded, attempts, _, _, _, _, _ = run_merge_cycle_phase(
            repo_path=Path("/repo"),
            log_file=Path("/tmp/test.log"),
            review_state_file=Path("/review.json"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            gh_repo_slug="acme/sandbox-qa-widget",
            merges_failed=0,
            merges_succeeded=0,
            merge_attempts=0,
            merged_pr_urls=[],
            open_prs=0,
            open_issues=0,
            blocked_reasons=[],
            reconcile_event={},
        )
        assert succeeded == 0
        assert attempts == 0
