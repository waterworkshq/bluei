"""Tests for bluei.engine.cli.main() — pipeline wiring, early exits, guard conditions."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.cli import main


def _base_args(**overrides):
    defaults = dict(
        repo_path="/tmp/test-repo",
        state_file="/tmp/test-repo/state/state.json",
        log_file="/tmp/test-repo/logs/run.log",
        findings_file="/tmp/test-repo/state/findings.jsonl",
        issues_file="/tmp/test-repo/state/issues.json",
        worktree_root="/tmp/test-repo/worktrees",
        status_file="/tmp/test-repo/state/status.json",
        docs_index_file="/tmp/test-repo/state/docs_index.json",
        lessons_file="/tmp/test-repo/state/LESSONS.md",
        reconcile_only=False,
        run_phase="orchestrated",
        dry_run=True,
        live_github_actions=False,
        auto_merge_sandbox=False,
        smoke_test=False,
        log_lesson="",
        pr_tags="",
        pr_author="qa-bot",
        bot_author="qa-bot",
        explicit_tag="qa-autofix-ok",
        review_feedback="",
        pattern_store_path=None,
        batch_state_file="/tmp/test-repo/state/batches.jsonl",
        fix_engine="deterministic",
        max_issues_per_run=10,
        issue_confidence_threshold=0.7,
        simulate_open_issues=None,
        simulate_open_prs=None,
        migrate_context=False,
        batch_pr_enabled=True,
        max_fix_attempts_per_issue=3,
        max_duplicate_prs_threshold=3,
        no_auto_close_duplicate_prs=False,
        allow_main_commit=False,
        force_push=False,
        max_prs_per_run=2,
        refresh_docs_index=False,
        deterministic_only=False,
        staleness_threshold_seconds=86400,
        allow_unchanged_baseline_failures=True,
        baseline_checks="[]",
        finding_cooldown_seconds=3600,
        auto_rebase_enabled=False,
        rebase_max_prs=5,
        rebase_stats_file=None,
        max_queue_items=None,
        auto_approve=False,
        max_files_changed=5,
        max_loc_diff=200,
        regression_check=False,
        merge_cooldown_minutes=30,
        batch_pr_rules=None,
        batch_pr_split_on_failure=True,
        batch_dedup_hours=24,
        claude_cmd_template="",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestMainSafetyAbort:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch(
        "bluei.engine.cli.assert_safe_repo", side_effect=RuntimeError("not a git repo")
    )
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.run_startup_self_healing", return_value={})
    @patch("bluei.engine.cli.run_docs_index_command", return_value=None)
    @patch("bluei.engine.cli.run_smoke_test_command", return_value=None)
    @patch("bluei.engine.cli.run_refactor_cycle_command", return_value=None)
    @patch("bluei.engine.cli.run_clean_prs_command", return_value=None)
    @patch("bluei.engine.cli.validate_safety", return_value=None)
    def test_unsafe_repo_returns_2(
        self,
        mock_val,
        mock_clean,
        mock_refactor,
        mock_smoke,
        mock_docs,
        mock_heal,
        mock_log,
        mock_assert,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 2


class TestMainLiveModeGuard:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch("bluei.engine.cli.assert_safe_repo")
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.run_startup_self_healing", return_value={})
    @patch("bluei.engine.cli.run_docs_index_command", return_value=None)
    @patch("bluei.engine.cli.run_refactor_cycle_command", return_value=None)
    @patch("bluei.engine.cli.run_smoke_test_command", return_value=None)
    @patch("bluei.engine.cli.run_clean_prs_command", return_value=None)
    @patch("bluei.engine.cli.validate_safety", return_value=None)
    @patch("bluei.engine.cli.get_origin_url", return_value="")
    @patch("bluei.engine.cli.parse_github_repo", return_value=(None, None))
    def test_live_mode_no_github_returns_2(
        self,
        mock_gh,
        mock_origin,
        mock_val,
        mock_clean,
        mock_smoke,
        mock_refactor,
        mock_docs,
        mock_heal,
        mock_log,
        mock_assert,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            live_github_actions=True,
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 2


class TestMainAutoMergeGuard:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch("bluei.engine.cli.assert_safe_repo")
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.run_startup_self_healing", return_value={})
    @patch("bluei.engine.cli.run_docs_index_command", return_value=None)
    @patch("bluei.engine.cli.run_refactor_cycle_command", return_value=None)
    @patch("bluei.engine.cli.run_smoke_test_command", return_value=None)
    @patch("bluei.engine.cli.run_clean_prs_command", return_value=None)
    @patch("bluei.engine.cli.validate_safety", return_value=None)
    @patch(
        "bluei.engine.cli.get_origin_url",
        return_value="https://github.com/user/repo.git",
    )
    @patch("bluei.engine.cli.parse_github_repo", return_value=("user", "repo"))
    @patch("bluei.engine.cli.repo_is_sandbox", return_value=False)
    def test_auto_merge_non_sandbox_returns_2(
        self,
        mock_sandbox,
        mock_gh,
        mock_origin,
        mock_val,
        mock_clean,
        mock_smoke,
        mock_refactor,
        mock_docs,
        mock_heal,
        mock_log,
        mock_assert,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            auto_merge_sandbox=True,
            live_github_actions=False,
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 2


class TestMainReviewLoopGate:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch("bluei.engine.cli.assert_safe_repo")
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.run_startup_self_healing", return_value={})
    @patch("bluei.engine.cli.run_docs_index_command", return_value=None)
    @patch("bluei.engine.cli.run_refactor_cycle_command", return_value=None)
    @patch("bluei.engine.cli.run_smoke_test_command", return_value=None)
    @patch("bluei.engine.cli.run_clean_prs_command", return_value=None)
    @patch("bluei.engine.cli.validate_safety", return_value=None)
    @patch(
        "bluei.engine.cli.get_origin_url",
        return_value="https://github.com/user/repo.git",
    )
    @patch("bluei.engine.cli.parse_github_repo", return_value=("user", "repo"))
    @patch(
        "bluei.engine.cli.review_loop_allowed",
        return_value=(False, "bot author mismatch"),
    )
    def test_review_loop_blocked_returns_4(
        self,
        mock_review,
        mock_gh,
        mock_origin,
        mock_val,
        mock_clean,
        mock_smoke,
        mock_refactor,
        mock_docs,
        mock_heal,
        mock_log,
        mock_assert,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            run_phase="issue-cycle",
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 4


class TestMainReviewFeedbackGate:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch("bluei.engine.cli.assert_safe_repo")
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.run_startup_self_healing", return_value={})
    @patch("bluei.engine.cli.run_docs_index_command", return_value=None)
    @patch("bluei.engine.cli.run_refactor_cycle_command", return_value=None)
    @patch("bluei.engine.cli.run_smoke_test_command", return_value=None)
    @patch("bluei.engine.cli.run_clean_prs_command", return_value=None)
    @patch("bluei.engine.cli.validate_safety", return_value=None)
    @patch(
        "bluei.engine.cli.get_origin_url",
        return_value="https://github.com/user/repo.git",
    )
    @patch("bluei.engine.cli.parse_github_repo", return_value=("user", "repo"))
    @patch("bluei.engine.cli.review_loop_allowed", return_value=(True, "ok"))
    @patch("bluei.engine.cli.classify_review_feedback", return_value="needs-human")
    def test_needs_human_feedback_returns_4(
        self,
        mock_classify,
        mock_review,
        mock_gh,
        mock_origin,
        mock_val,
        mock_clean,
        mock_smoke,
        mock_refactor,
        mock_docs,
        mock_heal,
        mock_log,
        mock_assert,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            run_phase="issue-cycle",
            review_feedback="this architecture is wrong",
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 4


class TestMainReconcileOnly:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch("bluei.engine.cli.assert_safe_repo")
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.run_startup_self_healing", return_value={})
    @patch("bluei.engine.cli.run_docs_index_command", return_value=None)
    @patch("bluei.engine.cli.run_refactor_cycle_command", return_value=None)
    @patch("bluei.engine.cli.run_smoke_test_command", return_value=None)
    @patch("bluei.engine.cli.run_clean_prs_command", return_value=None)
    @patch("bluei.engine.cli.validate_safety", return_value=None)
    @patch(
        "bluei.engine.cli.get_origin_url",
        return_value="https://github.com/user/repo.git",
    )
    @patch("bluei.engine.cli.parse_github_repo", return_value=("user", "repo"))
    @patch("bluei.engine.cli.load_state", return_value={})
    @patch("bluei.engine.cli.reconcile_open_workload")
    @patch("bluei.engine.cli.run_reconcile_only", return_value=0)
    def test_reconcile_only_routes_to_handler(
        self,
        mock_recon,
        mock_workload,
        mock_load,
        mock_gh,
        mock_origin,
        mock_val,
        mock_clean,
        mock_smoke,
        mock_refactor,
        mock_docs,
        mock_heal,
        mock_log,
        mock_assert,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_workload.return_value = (0, 0, {})
        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            reconcile_only=True,
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 0
        mock_recon.assert_called_once()


class TestMainLogLessonOnly:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.append_lesson")
    def test_log_lesson_with_docs_index_exits_0(
        self,
        mock_lesson,
        mock_log,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            run_phase="docs-index",
            log_lesson="something changed and worked",
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 0
        mock_lesson.assert_called_once()


class TestMainMergeCycleSkipsReviewLoop:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch("bluei.engine.cli.assert_safe_repo")
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.run_startup_self_healing", return_value={})
    @patch("bluei.engine.cli.run_docs_index_command", return_value=None)
    @patch("bluei.engine.cli.run_refactor_cycle_command", return_value=None)
    @patch("bluei.engine.cli.run_smoke_test_command", return_value=None)
    @patch("bluei.engine.cli.run_clean_prs_command", return_value=None)
    @patch("bluei.engine.cli.validate_safety", return_value=None)
    @patch(
        "bluei.engine.cli.get_origin_url",
        return_value="https://github.com/user/repo.git",
    )
    @patch("bluei.engine.cli.parse_github_repo", return_value=("user", "repo"))
    @patch("bluei.engine.cli.load_state", return_value={})
    @patch("bluei.engine.cli.reconcile_open_workload")
    @patch("bluei.engine.cli.load_issues", return_value={"issues": []})
    @patch("bluei.engine.cli.run_merge_cycle_phase")
    @patch("bluei.engine.cli.run_finalize_phase", return_value=0)
    def test_merge_cycle_skips_review_loop_check(
        self,
        mock_final,
        mock_merge,
        mock_load_issues,
        mock_workload,
        mock_load,
        mock_gh,
        mock_origin,
        mock_val,
        mock_clean,
        mock_smoke,
        mock_refactor,
        mock_docs,
        mock_heal,
        mock_log,
        mock_assert,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_workload.return_value = (0, 0, {})
        mock_merge.return_value = (0, 0, 0, [], 0, 0, [], {})
        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            run_phase="merge-cycle",
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 0
        mock_merge.assert_called_once()


class TestMainOrchestratedFullPipeline:
    @patch("bluei.engine.cli.parse_args")
    @patch("bluei.engine.cli.normalize_run_phase")
    @patch("bluei.engine.cli.resolve_baseline_checks", return_value={})
    @patch("bluei.engine.cli.is_mnemo_available", return_value=False)
    @patch("bluei.engine.cli.assert_safe_repo")
    @patch("bluei.engine.cli._append_text")
    @patch("bluei.engine.cli.run_startup_self_healing", return_value={})
    @patch("bluei.engine.cli.run_docs_index_command", return_value=None)
    @patch("bluei.engine.cli.run_refactor_cycle_command", return_value=None)
    @patch("bluei.engine.cli.run_smoke_test_command", return_value=None)
    @patch("bluei.engine.cli.run_clean_prs_command", return_value=None)
    @patch("bluei.engine.cli.validate_safety", return_value=None)
    @patch(
        "bluei.engine.cli.get_origin_url",
        return_value="https://github.com/user/repo.git",
    )
    @patch("bluei.engine.cli.parse_github_repo", return_value=("user", "repo"))
    @patch("bluei.engine.cli.review_loop_allowed", return_value=(True, "ok"))
    @patch("bluei.engine.cli.classify_review_feedback", return_value="auto")
    @patch("bluei.engine.cli.load_state", return_value={})
    @patch("bluei.engine.cli.reconcile_open_workload")
    @patch("bluei.engine.cli.load_issues", return_value={"issues": []})
    @patch("bluei.engine.cli.refresh_docs_index")
    @patch("bluei.engine.cli.run_discover_phase")
    @patch("bluei.engine.cli.run_issue_creation_phase")
    @patch("bluei.engine.cli.run_pr_cycle_phase")
    @patch("bluei.engine.cli.run_merge_cycle_phase")
    @patch("bluei.engine.cli.run_finalize_phase", return_value=0)
    def test_orchestrated_calls_all_phases(
        self,
        mock_final,
        mock_merge,
        mock_pr,
        mock_issue,
        mock_disc,
        mock_refresh,
        mock_load_issues,
        mock_workload,
        mock_load,
        mock_classify,
        mock_review,
        mock_gh,
        mock_origin,
        mock_val,
        mock_clean,
        mock_smoke,
        mock_refactor,
        mock_docs,
        mock_heal,
        mock_log,
        mock_assert,
        mock_mnemo,
        mock_baseline,
        mock_norm,
        mock_parse,
        tmp_path,
    ):
        mock_workload.return_value = (0, 0, {})
        mock_disc.return_value = ([], 0, [], [], [], [], True)
        mock_issue.return_value = ([], 0, [])
        mock_pr.return_value = dict(
            created_prs=0,
            open_prs=0,
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            issues_escalated_max_retries=0,
            claude_invocations=0,
            deterministic_invocations=0,
            blocked_reasons=[],
            state={},
            issues_data={"issues": []},
            eligible_findings=[],
        )
        mock_merge.return_value = (0, 0, 0, [], 0, 0, [], {})

        mock_parse.return_value = _base_args(
            repo_path=str(tmp_path),
            run_phase="orchestrated",
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "run.log"),
            findings_file=str(tmp_path / "findings.jsonl"),
            issues_file=str(tmp_path / "issues.json"),
            worktree_root=str(tmp_path / "worktrees"),
            status_file=str(tmp_path / "status.json"),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
            batch_state_file=str(tmp_path / "batches.jsonl"),
        )
        rc = main()
        assert rc == 0
        mock_disc.assert_called_once()
        mock_issue.assert_called_once()
        mock_pr.assert_called_once()
        mock_merge.assert_called_once()
        mock_final.assert_called_once()
