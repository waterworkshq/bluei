"""Tests for bluei.engine.commands.early_exits — safety validation and early-exit commands."""

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bluei.engine.commands.early_exits import (
    run_clean_prs_command,
    run_docs_index_command,
    run_refactor_cycle_command,
    run_smoke_test_command,
    validate_safety,
)


def _args(**overrides):
    defaults = dict(
        run_phase="full",
        dry_run=False,
        force_push=False,
        max_prs_per_run=2,
        open_issues_cap=20,
        open_prs_cap=5,
        merge_cooldown_minutes=30,
        finding_cooldown_seconds=600,
        staleness_threshold_seconds=3600,
        max_fix_attempts_per_issue=3,
        refresh_docs_index=False,
        auto_approve=False,
        max_queue_items=5,
        smoke_test=False,
        stale_pr_hours=48,
        stale_dedup_window=24,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestValidateSafety:
    def test_all_valid_returns_none(self):
        result = validate_safety(args=_args(), log_file=Path("/tmp/x.log"))
        assert result is None

    def test_force_push_aborts(self):
        captured = StringIO()
        with patch("sys.stdout", captured):
            result = validate_safety(
                args=_args(force_push=True), log_file=Path("/tmp/x.log")
            )
        assert result == 2
        assert "force push" in captured.getvalue()

    @pytest.mark.parametrize("val", [0, 3, -1, 100])
    def test_max_prs_per_run_invalid_aborts(self, val):
        captured = StringIO()
        with patch("sys.stdout", captured):
            result = validate_safety(
                args=_args(max_prs_per_run=val), log_file=Path("/tmp/x.log")
            )
        assert result == 2
        assert "max-prs-per-run" in captured.getvalue()

    @pytest.mark.parametrize("val", [9, 51, 0])
    def test_open_issues_cap_invalid_aborts(self, val):
        result = validate_safety(
            args=_args(open_issues_cap=val), log_file=Path("/tmp/x.log")
        )
        assert result == 2

    @pytest.mark.parametrize("val", [0, 11, -1])
    def test_open_prs_cap_invalid_aborts(self, val):
        result = validate_safety(
            args=_args(open_prs_cap=val), log_file=Path("/tmp/x.log")
        )
        assert result == 2

    def test_negative_merge_cooldown_aborts(self):
        result = validate_safety(
            args=_args(merge_cooldown_minutes=-1), log_file=Path("/tmp/x.log")
        )
        assert result == 2

    def test_negative_finding_cooldown_aborts(self):
        result = validate_safety(
            args=_args(finding_cooldown_seconds=-1), log_file=Path("/tmp/x.log")
        )
        assert result == 2

    def test_zero_staleness_aborts(self):
        result = validate_safety(
            args=_args(staleness_threshold_seconds=0), log_file=Path("/tmp/x.log")
        )
        assert result == 2

    def test_zero_max_fix_attempts_aborts(self):
        result = validate_safety(
            args=_args(max_fix_attempts_per_issue=0), log_file=Path("/tmp/x.log")
        )
        assert result == 2

    @pytest.mark.parametrize("prs", [1, 2])
    def test_valid_prs_per_run_passes(self, prs):
        assert (
            validate_safety(
                args=_args(max_prs_per_run=prs), log_file=Path("/tmp/x.log")
            )
            is None
        )


class TestRunSmokeTestCommand:
    def test_skips_when_not_smoke_test(self):
        result = run_smoke_test_command(
            repo_path=Path("/tmp/r"),
            log_file=Path("/tmp/x.log"),
            args=_args(run_phase="full", smoke_test=False),
        )
        assert result is None

    @patch("bluei.engine.validation.run_smoke_test")
    def test_passes_returns_zero(self, mock_st):
        mock_st.return_value = {
            "passed": True,
            "checks": {"git": True, "worktree": True, "linter": True},
            "duration_ms": 42,
            "errors": [],
        }
        captured = StringIO()
        with patch("sys.stdout", captured):
            result = run_smoke_test_command(
                repo_path=Path("/tmp/r"),
                log_file=Path("/tmp/x.log"),
                args=_args(run_phase="smoke-test"),
            )
        assert result == 0
        assert "PASS" in captured.getvalue()

    @patch("bluei.engine.validation.run_smoke_test")
    def test_fails_returns_one(self, mock_st):
        mock_st.return_value = {
            "passed": False,
            "checks": {"git": True, "worktree": False, "linter": True},
            "duration_ms": 99,
            "errors": ["worktree not found"],
        }
        captured = StringIO()
        with patch("sys.stdout", captured):
            result = run_smoke_test_command(
                repo_path=Path("/tmp/r"),
                log_file=Path("/tmp/x.log"),
                args=_args(run_phase="smoke-test"),
            )
        assert result == 1
        assert "FAIL" in captured.getvalue()


class TestRunRefactorCycleCommand:
    def test_skips_when_not_refactor_cycle(self):
        result = run_refactor_cycle_command(
            repo_path=Path("/tmp/r"),
            worktree_root=Path("/tmp/w"),
            log_file=Path("/tmp/x.log"),
            args=_args(run_phase="full"),
        )
        assert result is None

    @patch("bluei.engine.lifecycle.process_refactor_queue")
    def test_returns_zero_on_success(self, mock_rq):
        mock_rq.return_value = {
            "processed": ["a"],
            "approved": [],
            "pending": [],
            "failed": [],
        }
        captured = StringIO()
        with patch("sys.stdout", captured):
            result = run_refactor_cycle_command(
                repo_path=Path("/tmp/r"),
                worktree_root=Path("/tmp/w"),
                log_file=Path("/tmp/x.log"),
                args=_args(run_phase="refactor-cycle"),
            )
        assert result == 0
        assert "processed=1" in captured.getvalue()


class TestRunDocsIndexCommand:
    def test_skips_unrelated_phase(self):
        result = run_docs_index_command(
            repo_path=Path("/tmp/r"),
            docs_index_file=Path("/tmp/docs.json"),
            log_file=Path("/tmp/x.log"),
            args=_args(run_phase="full"),
        )
        assert result is None

    @patch("bluei.engine.git_utils.refresh_docs_index")
    def test_docs_index_phase_returns_zero(self, mock_rdi):
        mock_rdi.return_value = [{"file": "a.py"}]
        captured = StringIO()
        with patch("sys.stdout", captured):
            result = run_docs_index_command(
                repo_path=Path("/tmp/r"),
                docs_index_file=Path("/tmp/docs.json"),
                log_file=Path("/tmp/x.log"),
                args=_args(run_phase="docs-index"),
            )
        assert result == 0
        assert "entries=1" in captured.getvalue()


class TestRunCleanPrsCommand:
    def test_skips_unrelated_phase(self):
        result = run_clean_prs_command(
            repo_path=Path("/tmp/r"),
            log_file=Path("/tmp/x.log"),
            args=_args(run_phase="full"),
        )
        assert result is None

    @patch("bluei.engine.clean_prs.clean_stale_prs")
    @patch("bluei.engine.gh.parse_github_repo")
    @patch("bluei.engine.gh.get_origin_url")
    def test_clean_prs_returns_zero(self, mock_origin, mock_parse, mock_clean):
        mock_origin.return_value = "https://github.com/acme/widget"
        mock_parse.return_value = ("acme", "widget")
        mock_clean.return_value = {"closed": 2, "duplicates": 1, "stale": 3}
        captured = StringIO()
        with patch("sys.stdout", captured):
            result = run_clean_prs_command(
                repo_path=Path("/tmp/r"),
                log_file=Path("/tmp/x.log"),
                args=_args(run_phase="clean-prs"),
            )
        assert result == 0
        assert "closed=2" in captured.getvalue()
