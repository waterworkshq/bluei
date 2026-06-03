"""Tests for bluei.engine.commands.finalize — state save, status artifact, lessons, escalation."""

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.finalize import run_finalize_phase


@pytest.fixture(autouse=True)
def _skip_escalation_analysis():
    with patch("bluei.engine.escalation.run_escalation_checks", return_value=[]):
        yield


def _cost_tracker():
    ct = MagicMock()
    ct.cycle_total.return_value = 0.05
    ct.warned.return_value = False
    ct.exceeded_limit.return_value = False
    return ct


def _args(**overrides):
    defaults = dict(
        run_phase="orchestrated",
        dry_run=True,
        live_github_actions=False,
        reconcile_only=False,
        max_fix_attempts_per_issue=3,
        max_duplicate_prs_threshold=3,
        no_auto_close_duplicate_prs=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestFinalizeBasicState:
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_saves_state_and_issues(
        self, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        issues_data = {"issues": [{"status": "resolved_verified"}]}
        captured = StringIO()
        with patch("sys.stdout", captured):
            rc = run_finalize_phase(
                state_file=tmp_path / "state.json",
                issues_file=tmp_path / "issues.json",
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                lessons_file=tmp_path / "lessons.md",
                repo_path=tmp_path,
                args=_args(),
                state={},
                issues_data=issues_data,
                reconcile_event={},
                previous_last_run_at=None,
                open_issues=1,
                open_prs=0,
                findings=[],
                written_findings=0,
                created_issues=[],
                suppressed_findings=[],
                blocked_reasons=[],
                fix_attempts=0,
                fixes_verified=0,
                fixes_failed_verification=0,
                created_prs=0,
                issues_escalated_max_retries=0,
                merge_attempts=0,
                merges_succeeded=0,
                merges_failed=0,
                merged_pr_urls=[],
                claude_invocations=0,
                opencode_invocations=0,
                deterministic_invocations=0,
                cost_tracker=_cost_tracker(),
                cost_log_path=tmp_path / "cost.json",
                gh_repo_slug="acme/widget",
            )

        assert rc == 0
        mock_si.assert_called_once()
        mock_ss.assert_called_once()
        assert "unresolved_open" not in str(mock_ss.call_args) or "0" in str(
            mock_ss.call_args
        )

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_counts_unresolved_issues(
        self, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        issues_data = {
            "issues": [
                {"status": "resolved_verified"},
                {"status": "open"},
                {"status": "fix_attempted"},
            ]
        }
        captured = StringIO()
        with patch("sys.stdout", captured):
            rc = run_finalize_phase(
                state_file=tmp_path / "state.json",
                issues_file=tmp_path / "issues.json",
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                lessons_file=tmp_path / "lessons.md",
                repo_path=tmp_path,
                args=_args(),
                state={},
                issues_data=issues_data,
                reconcile_event={},
                previous_last_run_at=None,
                open_issues=2,
                open_prs=0,
                findings=[],
                written_findings=0,
                created_issues=[],
                suppressed_findings=[],
                blocked_reasons=[],
                fix_attempts=0,
                fixes_verified=0,
                fixes_failed_verification=0,
                created_prs=0,
                issues_escalated_max_retries=0,
                merge_attempts=0,
                merges_succeeded=0,
                merges_failed=0,
                merged_pr_urls=[],
                claude_invocations=0,
                opencode_invocations=0,
                deterministic_invocations=0,
                cost_tracker=_cost_tracker(),
                cost_log_path=tmp_path / "cost.json",
                gh_repo_slug="acme/widget",
            )

        assert rc == 0
        status_call = mock_status.call_args
        metrics = status_call[1]["run_metrics"]
        assert metrics["unresolved_open"] == 2

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_run_mode_active_sim(
        self, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        captured = StringIO()
        with patch("sys.stdout", captured):
            run_finalize_phase(
                state_file=tmp_path / "state.json",
                issues_file=tmp_path / "issues.json",
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                lessons_file=tmp_path / "lessons.md",
                repo_path=tmp_path,
                args=_args(dry_run=True, live_github_actions=False),
                state={},
                issues_data={"issues": []},
                reconcile_event={},
                previous_last_run_at=None,
                open_issues=0,
                open_prs=0,
                findings=[],
                written_findings=0,
                created_issues=[],
                suppressed_findings=[],
                blocked_reasons=[],
                fix_attempts=0,
                fixes_verified=0,
                fixes_failed_verification=0,
                created_prs=0,
                issues_escalated_max_retries=0,
                merge_attempts=0,
                merges_succeeded=0,
                merges_failed=0,
                merged_pr_urls=[],
                claude_invocations=0,
                opencode_invocations=0,
                deterministic_invocations=0,
                cost_tracker=_cost_tracker(),
                cost_log_path=tmp_path / "cost.json",
                gh_repo_slug="acme/widget",
            )

        status_call = mock_status.call_args
        assert status_call[1]["run_mode"] == "DRY-RUN-ORCHESTRATED"


class TestFinalizeLessons:
    @patch("bluei.engine.commands.finalize.append_lesson")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_appends_lesson_on_activity(
        self, mock_si, mock_ss, mock_status, mock_log, mock_lesson, tmp_path
    ):
        captured = StringIO()
        with patch("sys.stdout", captured):
            run_finalize_phase(
                state_file=tmp_path / "state.json",
                issues_file=tmp_path / "issues.json",
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                lessons_file=tmp_path / "lessons.md",
                repo_path=tmp_path,
                args=_args(run_phase="pr-cycle"),
                state={},
                issues_data={"issues": []},
                reconcile_event={},
                previous_last_run_at=None,
                open_issues=0,
                open_prs=0,
                findings=[],
                written_findings=0,
                created_issues=[{"issue_id": "i1"}],
                suppressed_findings=[],
                blocked_reasons=[],
                fix_attempts=1,
                fixes_verified=1,
                fixes_failed_verification=0,
                created_prs=1,
                issues_escalated_max_retries=0,
                merge_attempts=0,
                merges_succeeded=0,
                merges_failed=0,
                merged_pr_urls=[],
                claude_invocations=0,
                opencode_invocations=0,
                deterministic_invocations=0,
                cost_tracker=_cost_tracker(),
                cost_log_path=tmp_path / "cost.json",
                gh_repo_slug="acme/widget",
            )

        mock_lesson.assert_called_once()
        call_kwargs = mock_lesson.call_args[1]
        assert "1 fixes verified" in call_kwargs["what_changed"]
        assert "1 PRs created" in call_kwargs["what_changed"]
        assert "1 issues flagged" in call_kwargs["what_worked"]

    @patch("bluei.engine.commands.finalize.append_lesson")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_no_lesson_when_no_activity(
        self, mock_si, mock_ss, mock_status, mock_log, mock_lesson, tmp_path
    ):
        captured = StringIO()
        with patch("sys.stdout", captured):
            run_finalize_phase(
                state_file=tmp_path / "state.json",
                issues_file=tmp_path / "issues.json",
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                lessons_file=tmp_path / "lessons.md",
                repo_path=tmp_path,
                args=_args(run_phase="pr-cycle"),
                state={},
                issues_data={"issues": []},
                reconcile_event={},
                previous_last_run_at=None,
                open_issues=0,
                open_prs=0,
                findings=[],
                written_findings=0,
                created_issues=[],
                suppressed_findings=[],
                blocked_reasons=[],
                fix_attempts=0,
                fixes_verified=0,
                fixes_failed_verification=0,
                created_prs=0,
                issues_escalated_max_retries=0,
                merge_attempts=0,
                merges_succeeded=0,
                merges_failed=0,
                merged_pr_urls=[],
                claude_invocations=0,
                opencode_invocations=0,
                deterministic_invocations=0,
                cost_tracker=_cost_tracker(),
                cost_log_path=tmp_path / "cost.json",
                gh_repo_slug="acme/widget",
            )

        mock_lesson.assert_not_called()


class TestLessonContent:
    """Verify the lesson text constructed from run metrics."""

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    @patch("bluei.engine.commands.finalize.append_lesson")
    def test_failed_verification_produces_what_broke(
        self, mock_lesson, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        from bluei.engine.commands.finalize import run_finalize_phase

        args = SimpleNamespace(
            run_phase="pr-cycle",
            dry_run=True,
            live_github_actions=False,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
            rebase_stats_file=None,
        )
        run_finalize_phase(
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            args=args,
            state={},
            issues_data={"issues": []},
            reconcile_event={},
            previous_last_run_at=None,
            open_issues=0,
            open_prs=0,
            findings=[],
            written_findings=0,
            created_issues=[],
            suppressed_findings=[],
            blocked_reasons=[],
            fix_attempts=3,
            fixes_verified=0,
            fixes_failed_verification=3,
            created_prs=0,
            issues_escalated_max_retries=0,
            merge_attempts=0,
            merges_succeeded=0,
            merges_failed=0,
            merged_pr_urls=[],
            claude_invocations=0,
            opencode_invocations=0,
            deterministic_invocations=0,
            cost_tracker=_cost_tracker(),
            cost_log_path=tmp_path / "cost.json",
            gh_repo_slug="acme/widget",
        )
        mock_lesson.assert_called_once()
        assert "3 fixes failed verification" in mock_lesson.call_args[1]["what_broke"]
        assert mock_lesson.call_args[1]["what_changed"] == ""
        assert mock_lesson.call_args[1]["what_worked"] == ""

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    @patch("bluei.engine.commands.finalize.append_lesson")
    def test_fixes_verified_plus_prs_produce_what_changed(
        self, mock_lesson, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        from bluei.engine.commands.finalize import run_finalize_phase

        args = SimpleNamespace(
            run_phase="pr-cycle",
            dry_run=True,
            live_github_actions=False,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
            rebase_stats_file=None,
        )
        run_finalize_phase(
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            args=args,
            state={},
            issues_data={"issues": []},
            reconcile_event={},
            previous_last_run_at=None,
            open_issues=0,
            open_prs=0,
            findings=[],
            written_findings=0,
            created_issues=[{"issue_id": "i1"}],
            suppressed_findings=[],
            blocked_reasons=[],
            fix_attempts=5,
            fixes_verified=5,
            fixes_failed_verification=0,
            created_prs=2,
            issues_escalated_max_retries=0,
            merge_attempts=0,
            merges_succeeded=0,
            merges_failed=0,
            merged_pr_urls=[],
            claude_invocations=0,
            opencode_invocations=0,
            deterministic_invocations=0,
            cost_tracker=_cost_tracker(),
            cost_log_path=tmp_path / "cost.json",
            gh_repo_slug="acme/widget",
        )
        assert "5 fixes verified" in mock_lesson.call_args[1]["what_changed"]
        assert "2 PRs created" in mock_lesson.call_args[1]["what_changed"]
        assert "1 issues flagged" in mock_lesson.call_args[1]["what_worked"]
        assert mock_lesson.call_args[1]["what_broke"] == ""

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    @patch("bluei.engine.commands.finalize.append_lesson")
    def test_merges_succeeded_recorded_in_what_worked(
        self, mock_lesson, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        from bluei.engine.commands.finalize import run_finalize_phase

        args = SimpleNamespace(
            run_phase="merge-cycle",
            dry_run=True,
            live_github_actions=False,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
            rebase_stats_file=None,
        )
        run_finalize_phase(
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            args=args,
            state={},
            issues_data={"issues": []},
            reconcile_event={},
            previous_last_run_at=None,
            open_issues=0,
            open_prs=0,
            findings=[],
            written_findings=0,
            created_issues=[],
            suppressed_findings=[],
            blocked_reasons=[],
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            created_prs=0,
            issues_escalated_max_retries=0,
            merge_attempts=3,
            merges_succeeded=3,
            merges_failed=0,
            merged_pr_urls=["http://pr/1"],
            claude_invocations=0,
            opencode_invocations=0,
            deterministic_invocations=0,
            cost_tracker=_cost_tracker(),
            cost_log_path=tmp_path / "cost.json",
            gh_repo_slug="acme/widget",
        )
        assert "3 merges succeeded" in mock_lesson.call_args[1]["what_worked"]

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    @patch("bluei.engine.commands.finalize.append_lesson")
    def test_no_lesson_for_non_orchestrated_phases(
        self, mock_lesson, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        from bluei.engine.commands.finalize import run_finalize_phase

        args = SimpleNamespace(
            run_phase="verify-only",
            dry_run=True,
            live_github_actions=False,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
            rebase_stats_file=None,
        )
        run_finalize_phase(
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            args=args,
            state={},
            issues_data={"issues": []},
            reconcile_event={},
            previous_last_run_at=None,
            open_issues=0,
            open_prs=0,
            findings=[],
            written_findings=0,
            created_issues=[],
            suppressed_findings=[],
            blocked_reasons=[],
            fix_attempts=5,
            fixes_verified=5,
            fixes_failed_verification=0,
            created_prs=0,
            issues_escalated_max_retries=0,
            merge_attempts=0,
            merges_succeeded=0,
            merges_failed=0,
            merged_pr_urls=[],
            claude_invocations=0,
            opencode_invocations=0,
            deterministic_invocations=0,
            cost_tracker=_cost_tracker(),
            cost_log_path=tmp_path / "cost.json",
            gh_repo_slug="acme/widget",
        )
        mock_lesson.assert_not_called()

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    @patch("bluei.engine.commands.finalize.append_lesson")
    def test_lesson_includes_all_three_parts_when_activity(
        self, mock_lesson, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        from bluei.engine.commands.finalize import run_finalize_phase

        args = SimpleNamespace(
            run_phase="orchestrated",
            dry_run=True,
            live_github_actions=False,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
            rebase_stats_file=None,
        )
        run_finalize_phase(
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            args=args,
            state={},
            issues_data={"issues": []},
            reconcile_event={},
            previous_last_run_at=None,
            open_issues=0,
            open_prs=0,
            findings=[],
            written_findings=0,
            created_issues=[{"issue_id": "i1"}],
            suppressed_findings=[],
            blocked_reasons=[],
            fix_attempts=5,
            fixes_verified=3,
            fixes_failed_verification=2,
            created_prs=1,
            issues_escalated_max_retries=0,
            merge_attempts=1,
            merges_succeeded=1,
            merges_failed=0,
            merged_pr_urls=["http://pr"],
            claude_invocations=0,
            opencode_invocations=0,
            deterministic_invocations=0,
            cost_tracker=_cost_tracker(),
            cost_log_path=tmp_path / "cost.json",
            gh_repo_slug="acme/widget",
        )
        assert "2 fixes failed verification" in mock_lesson.call_args[1]["what_broke"]
        assert "3 fixes verified" in mock_lesson.call_args[1]["what_changed"]
        assert "1 PRs created" in mock_lesson.call_args[1]["what_changed"]
        assert "1 issues flagged" in mock_lesson.call_args[1]["what_worked"]
        assert "1 merges succeeded" in mock_lesson.call_args[1]["what_worked"]


class TestRunModeConstruction:
    """Verify the run-mode string logic."""

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_dry_run_live_produces_dry_run_live_mode(
        self, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        from bluei.engine.commands.finalize import run_finalize_phase

        args = SimpleNamespace(
            run_phase="orchestrated",
            dry_run=True,
            live_github_actions=True,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
            rebase_stats_file=None,
        )
        run_finalize_phase(
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            args=args,
            state={},
            issues_data={"issues": []},
            reconcile_event={},
            previous_last_run_at=None,
            open_issues=0,
            open_prs=0,
            findings=[],
            written_findings=0,
            created_issues=[],
            suppressed_findings=[],
            blocked_reasons=[],
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            created_prs=0,
            issues_escalated_max_retries=0,
            merge_attempts=0,
            merges_succeeded=0,
            merges_failed=0,
            merged_pr_urls=[],
            claude_invocations=0,
            opencode_invocations=0,
            deterministic_invocations=0,
            cost_tracker=_cost_tracker(),
            cost_log_path=tmp_path / "cost.json",
            gh_repo_slug="acme/widget",
        )
        call = mock_status.call_args
        assert call[1]["run_mode"] == "DRY-RUN-LIVE-ORCHESTRATED"

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_active_sim_produces_active_sim_mode(
        self, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        from bluei.engine.commands.finalize import run_finalize_phase

        args = SimpleNamespace(
            run_phase="pr-cycle",
            dry_run=False,
            live_github_actions=False,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
            rebase_stats_file=None,
        )
        run_finalize_phase(
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            args=args,
            state={},
            issues_data={"issues": []},
            reconcile_event={},
            previous_last_run_at=None,
            open_issues=0,
            open_prs=0,
            findings=[],
            written_findings=0,
            created_issues=[],
            suppressed_findings=[],
            blocked_reasons=[],
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            created_prs=0,
            issues_escalated_max_retries=0,
            merge_attempts=0,
            merges_succeeded=0,
            merges_failed=0,
            merged_pr_urls=[],
            claude_invocations=0,
            opencode_invocations=0,
            deterministic_invocations=0,
            cost_tracker=_cost_tracker(),
            cost_log_path=tmp_path / "cost.json",
            gh_repo_slug="acme/widget",
        )
        call = mock_status.call_args
        assert call[1]["run_mode"] == "ACTIVE-SIM-PR-CYCLE"
