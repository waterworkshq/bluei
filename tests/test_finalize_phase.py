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
    ct.cycle_savings.return_value = 0.0
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


def _finalize_kwargs(tmp_path, **overrides):
    """Build the kwargs dict for run_finalize_phase with sane defaults.

    Mirrors every parameter the function reads off ``kwargs`` so individual
    tests can override only the field they care about.
    """
    defaults = dict(
        state_file=tmp_path / "state.json",
        issues_file=tmp_path / "issues.json",
        status_file=tmp_path / "status.json",
        findings_file=tmp_path / "findings.json",
        log_file=tmp_path / "test.log",
        lessons_file=tmp_path / "lessons.md",
        repo_path=tmp_path,
        args=_args(),
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
        ledger_records=[],
        pattern_store=None,
    )
    defaults.update(overrides)
    return defaults


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


class TestFinalizeContextArg:
    """Cover the positional RunContext extraction path (lines 20-54).

    The existing tests all exercise the ``**kwargs`` branch; this class
    drives the ``if args:`` branch by passing a ``RunContext`` positionally.
    """

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_extracts_fields_from_positional_ctx(
        self, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        from bluei.engine.commands.context import RunContext

        issues_data = {
            "issues": [
                {"status": "open"},
                {"status": "resolved_verified"},
            ]
        }
        ctx = RunContext(
            args=_args(),
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            gh_repo_slug="acme/widget",
            state={"marker": "from-ctx"},
            issues_data=issues_data,
            open_issues=3,
            open_prs=2,
            fix_attempts=1,
            fixes_verified=1,
            created_prs=1,
            created_issues=[{"issue_id": "i1"}],
            cost_tracker=_cost_tracker(),
            cost_log_path=tmp_path / "cost.json",
        )

        captured = StringIO()
        with patch("sys.stdout", captured):
            rc = run_finalize_phase(ctx)

        assert rc == 0
        # save_state received the ctx's state dict (positional call)
        assert mock_ss.call_args[0][1]["marker"] == "from-ctx"
        # save_issues received the ctx's issues_data object
        assert mock_si.call_args[0][1] is issues_data
        # status artifact metrics reflect ctx values
        metrics = mock_status.call_args[1]["run_metrics"]
        assert metrics["issues_created"] == 1
        assert metrics["prs_created"] == 1
        assert metrics["unresolved_open"] == 1  # one open, one resolved


class TestFinalizeHealthEnrich:
    """Cover the post-status cost-enrichment block (lines 144-158)."""

    @patch("bluei.engine.health.enrich_health_with_cost")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_enriches_and_rewrites_status_file(
        self, mock_si, mock_ss, mock_status, mock_log, mock_enrich, tmp_path
    ):
        status_file = tmp_path / "status.json"
        status_file.write_text(json.dumps({"score": 80, "components": {}}))

        def mutate(summary, **kwargs):
            summary["enriched"] = True
            return summary

        mock_enrich.side_effect = mutate

        kwargs = _finalize_kwargs(tmp_path, status_file=status_file)
        rc = run_finalize_phase(**kwargs)

        assert rc == 0
        mock_enrich.assert_called_once()
        # cost_log_path and total_runs (max of claude_invocations, 1) are passed through
        assert mock_enrich.call_args[1]["cost_log_path"] == kwargs["cost_log_path"]
        assert mock_enrich.call_args[1]["total_runs"] == 1  # max(0, 1)
        # status file was rewritten with the enriched data, sorted keys
        rewritten = json.loads(status_file.read_text())
        assert rewritten["enriched"] is True
        assert rewritten["score"] == 80

    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_swallows_invalid_status_file_json(
        self, mock_si, mock_ss, mock_status, mock_log, tmp_path
    ):
        status_file = tmp_path / "status.json"
        status_file.write_text("not-valid-json{")

        rc = run_finalize_phase(**_finalize_kwargs(tmp_path, status_file=status_file))

        assert rc == 0  # JSONDecodeError was swallowed

    @patch("bluei.engine.health.enrich_health_with_cost")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_skips_enrich_when_status_file_missing(
        self, mock_si, mock_ss, mock_status, mock_log, mock_enrich, tmp_path
    ):
        # status_file does not exist on disk
        status_file = tmp_path / "absent.json"

        rc = run_finalize_phase(**_finalize_kwargs(tmp_path, status_file=status_file))

        assert rc == 0
        mock_enrich.assert_not_called()


class TestFinalizeEscalation:
    """Cover escalation routing, notification delivery, and cycle escalation.

    These tests override the module-level ``_skip_escalation_analysis``
    autouse fixture by re-patching ``run_escalation_checks`` (and friends)
    via decorators — decorator patches stack inside the fixture's patch and
    therefore win.
    """

    @patch("bluei.engine.escalation.check_cycle_escalation", return_value=None)
    @patch("bluei.engine.escalation.log_escalation_event")
    @patch("bluei.engine.escalation.handle_max_duplicate_escalation")
    @patch("bluei.engine.escalation.run_escalation_checks")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_max_duplicate_prs_finding_invokes_handler(
        self,
        mock_si,
        mock_ss,
        mock_status,
        mock_log,
        mock_run_esc,
        mock_handle,
        mock_log_evt,
        mock_check,
        tmp_path,
    ):
        mock_run_esc.return_value = [
            {
                "type": "max_duplicate_prs",
                "detail": "too many PRs",
                "finding_id": "f1",
            }
        ]
        mock_handle.return_value = {
            "closed_prs": [11],
            "close_failed": [],
            "routed_findings": ["f1"],
            "paused_findings": ["f1"],
        }

        rc = run_finalize_phase(
            **_finalize_kwargs(
                tmp_path,
                args=_args(run_phase="merge-cycle", dry_run=True),
            )
        )

        assert rc == 0
        mock_handle.assert_called_once()
        # dry_run=True (and no_auto_close_duplicate_prs=False) propagates as dry_run=True
        assert mock_handle.call_args[1]["dry_run"] is True
        assert mock_handle.call_args[1]["repo_slug"] == "acme/widget"
        # escalation summary line was logged
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("max-duplicates:" in c and "closed=1" in c for c in log_calls)
        # cycle escalation still runs (returns None → no log event)
        mock_check.assert_called_once()
        mock_log_evt.assert_not_called()

    @patch("bluei.engine.escalation.check_cycle_escalation", return_value=None)
    @patch("bluei.engine.escalation.handle_max_duplicate_escalation")
    @patch("bluei.engine.escalation.run_escalation_checks")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_non_max_duplicate_finding_skips_handler(
        self,
        mock_si,
        mock_ss,
        mock_status,
        mock_log,
        mock_run_esc,
        mock_handle,
        mock_check,
        tmp_path,
    ):
        mock_run_esc.return_value = [
            {
                "type": "consecutive_merge_failure",
                "detail": "merges keep failing",
            }
        ]

        rc = run_finalize_phase(
            **_finalize_kwargs(
                tmp_path,
                args=_args(run_phase="merge-cycle", dry_run=True),
            )
        )

        assert rc == 0
        # handler only fires for max_duplicate_prs findings
        mock_handle.assert_not_called()
        # the escalation summary line is still logged
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("escalation: 1 pattern(s) detected" in c for c in log_calls)

    @patch("bluei.engine.escalation.check_cycle_escalation", return_value=None)
    @patch("bluei.engine.notify.deliver_escalations")
    @patch("bluei.engine.escalation.run_escalation_checks")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_delivers_notifications_when_not_dry_run(
        self,
        mock_si,
        mock_ss,
        mock_status,
        mock_log,
        mock_run_esc,
        mock_deliver,
        mock_check,
        tmp_path,
    ):
        mock_run_esc.return_value = [
            {
                "type": "consecutive_merge_failure",
                "detail": "3 merges failed in a row",
            }
        ]
        mock_deliver.return_value = [
            SimpleNamespace(success=True),
            SimpleNamespace(success=False),
        ]

        rc = run_finalize_phase(
            **_finalize_kwargs(
                tmp_path,
                args=_args(run_phase="pr-cycle", dry_run=False),
            )
        )

        assert rc == 0
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args[1]["repo_name"] == "acme/widget"
        # notification summary line: 1 delivered, 1 failed
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any(
            "notifications:" in c and "1 delivered" in c and "1 failed" in c
            for c in log_calls
        )

    @patch("bluei.engine.escalation.check_cycle_escalation", return_value=None)
    @patch("bluei.engine.notify.deliver_escalations")
    @patch("bluei.engine.escalation.run_escalation_checks")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_skips_notifications_in_dry_run(
        self,
        mock_si,
        mock_ss,
        mock_status,
        mock_log,
        mock_run_esc,
        mock_deliver,
        mock_check,
        tmp_path,
    ):
        mock_run_esc.return_value = [
            {"type": "consecutive_merge_failure", "detail": "..."}
        ]

        rc = run_finalize_phase(
            **_finalize_kwargs(
                tmp_path,
                args=_args(run_phase="pr-cycle", dry_run=True),
            )
        )

        assert rc == 0
        mock_deliver.assert_not_called()

    @patch("bluei.engine.escalation.log_escalation_event")
    @patch("bluei.engine.escalation.check_cycle_escalation")
    @patch("bluei.engine.escalation.run_escalation_checks")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_cycle_escalation_classifies_history_and_logs_event(
        self,
        mock_si,
        mock_ss,
        mock_status,
        mock_log,
        mock_run_esc,
        mock_check,
        mock_log_evt,
        tmp_path,
    ):
        # No top-level escalation findings → skip handler + notify blocks,
        # but still drive the cycle-escalation loop below them.
        mock_run_esc.return_value = []
        event = {
            "type": "cycle_escalation",
            "finding_id": "f1",
            "detail": "Finding f1 failed 3 consecutive cycles",
        }
        mock_check.return_value = event

        issues_data = {
            "issues": [
                # failure via fix_failed_verification
                {
                    "finding_id": "f1",
                    "history": [{"event": "fix_failed_verification", "at": "t1"}],
                },
                # failure via needs-human-* status
                {
                    "finding_id": "f2",
                    "history": [
                        {"event": "needs-human-max-retries-exceeded", "at": "t2"}
                    ],
                },
                # success via resolved_verified
                {
                    "finding_id": "f3",
                    "history": [{"event": "resolved_verified", "at": "t3"}],
                },
                # success via pr_opened
                {"finding_id": "f4", "history": [{"event": "pr_opened", "at": "t4"}]},
                # success via resolved_merged
                {
                    "finding_id": "f5",
                    "history": [{"event": "resolved_merged", "at": "t5"}],
                },
                # no history → skipped
                {"finding_id": "f6"},
                # empty history → skipped
                {"finding_id": "f7", "history": []},
                # neither failure nor success → skipped
                {
                    "finding_id": "f8",
                    "history": [{"event": "something_else", "at": "t8"}],
                },
            ]
        }

        rc = run_finalize_phase(
            **_finalize_kwargs(
                tmp_path,
                args=_args(run_phase="merge-cycle", max_fix_attempts_per_issue=3),
                issues_data=issues_data,
            )
        )

        assert rc == 0
        # cycle log passed to check_cycle_escalation classified all 5 relevant issues
        cycle_log = mock_check.call_args[0][1]
        assert len(cycle_log) == 5
        statuses = {e["finding_id"]: e["status"] for e in cycle_log}
        assert statuses["f1"] == "failure"
        assert statuses["f2"] == "failure"
        assert statuses["f3"] == "success"
        assert statuses["f4"] == "success"
        assert statuses["f5"] == "success"
        # cycle_type carried from args.run_phase
        assert all(e["cycle_type"] == "merge-cycle" for e in cycle_log)
        # EscalationConfig.threshold came from args.max_fix_attempts_per_issue
        esc_config = mock_check.call_args[0][0]
        assert esc_config.consecutive_failure_threshold == 3
        # event was logged and detail written to log file
        mock_log_evt.assert_called_once_with(esc_config, event)
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("escalation-cycle:" in c for c in log_calls)

    @patch("bluei.engine.escalation.log_escalation_event")
    @patch("bluei.engine.escalation.check_cycle_escalation", return_value=None)
    @patch("bluei.engine.escalation.run_escalation_checks")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_no_cycle_event_logged_when_no_breach(
        self,
        mock_si,
        mock_ss,
        mock_status,
        mock_log,
        mock_run_esc,
        mock_check,
        mock_log_evt,
        tmp_path,
    ):
        mock_run_esc.return_value = []

        rc = run_finalize_phase(
            **_finalize_kwargs(
                tmp_path,
                args=_args(run_phase="merge-cycle"),
                issues_data={
                    "issues": [
                        {
                            "finding_id": "f1",
                            "history": [
                                {"event": "fix_failed_verification", "at": "t1"}
                            ],
                        },
                    ]
                },
            )
        )

        assert rc == 0
        # cycle_log was still built (one failure entry) but no escalation fired
        cycle_log = mock_check.call_args[0][1]
        assert len(cycle_log) == 1
        assert cycle_log[0]["status"] == "failure"
        mock_log_evt.assert_not_called()

    @patch("bluei.engine.escalation.check_cycle_escalation", return_value=None)
    @patch("bluei.engine.escalation.run_escalation_checks")
    @patch("bluei.engine.commands.finalize._append_text")
    @patch("bluei.engine.commands.finalize.update_status_artifact")
    @patch("bluei.engine.commands.finalize.save_state")
    @patch("bluei.engine.commands.finalize.save_issues")
    def test_cycle_escalation_skipped_for_non_cycle_phase(
        self,
        mock_si,
        mock_ss,
        mock_status,
        mock_log,
        mock_run_esc,
        mock_check,
        tmp_path,
    ):
        # orchestrated is not in ("merge-cycle", "pr-cycle")
        rc = run_finalize_phase(
            **_finalize_kwargs(
                tmp_path,
                args=_args(run_phase="orchestrated"),
            )
        )

        assert rc == 0
        mock_run_esc.assert_not_called()
        mock_check.assert_not_called()
