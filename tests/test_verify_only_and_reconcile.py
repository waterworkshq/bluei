"""Tests for bluei.engine.commands.verify_only and reconcile — early-exit phase handlers."""

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.verify_only import run_verify_only
from bluei.engine.commands.reconcile import run_reconcile_only
from bluei.engine.models import Finding


def _make_finding(**overrides):
    defaults = dict(
        finding_id="f001",
        repo="test-repo",
        path="src/main.py",
        line=42,
        rule="ruff-c408",
        snippet="dict(a=1)",
        confidence=0.72,
        quick_win=True,
        safe_to_autofix=True,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def _args(**overrides):
    defaults = dict(
        dry_run=False,
        live_github_actions=False,
        workspace=None,
        repo_path="/tmp/repo",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestVerifyOnly:
    def test_marks_still_firing_as_failed(self, tmp_path):
        findings = [_make_finding(rule="ruff-c408", path="src/main.py")]
        issues_data = {
            "issues": [
                {
                    "issue_id": "i1",
                    "rule": "ruff-c408",
                    "path": "src/main.py",
                    "status": "open",
                }
            ]
        }
        state = {}
        captured = StringIO()
        with (
            patch("bluei.engine.commands.verify_only.save_issues") as mock_si,
            patch("bluei.engine.commands.verify_only.save_state") as mock_ss,
            patch("bluei.engine.commands.verify_only.update_status_artifact"),
            patch("bluei.engine.orchestrator.set_issue_status") as mock_sis,
            patch("sys.stdout", captured),
        ):
            rc = run_verify_only(
                findings=findings,
                issues_data=issues_data,
                issues_file=tmp_path / "issues.json",
                state_file=tmp_path / "state.json",
                state=state,
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                args=_args(),
                reconcile_event={"reason": "test"},
                previous_last_run_at=None,
                open_issues=1,
                open_prs=0,
                written_findings=1,
                suppressed_findings=[],
                blocked_reasons=[],
            )

        assert rc == 0
        failed_calls = [
            c for c in mock_sis.call_args_list if c[0][1] == "fix_failed_verification"
        ]
        assert len(failed_calls) == 1
        assert "fixes_failed_verification=1" in captured.getvalue()

    def test_marks_resolved_when_not_firing(self, tmp_path):
        findings = [_make_finding(rule="ruff-c408", path="src/main.py")]
        issues_data = {
            "issues": [
                {
                    "issue_id": "i1",
                    "rule": "other-rule",
                    "path": "other.py",
                    "status": "open",
                }
            ]
        }
        state = {}
        captured = StringIO()
        with (
            patch("bluei.engine.commands.verify_only.save_issues"),
            patch("bluei.engine.commands.verify_only.save_state"),
            patch("bluei.engine.commands.verify_only.update_status_artifact"),
            patch("bluei.engine.orchestrator.set_issue_status") as mock_sis,
            patch("sys.stdout", captured),
        ):
            rc = run_verify_only(
                findings=findings,
                issues_data=issues_data,
                issues_file=tmp_path / "issues.json",
                state_file=tmp_path / "state.json",
                state=state,
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                args=_args(),
                reconcile_event={"reason": "test"},
                previous_last_run_at=None,
                open_issues=1,
                open_prs=0,
                written_findings=1,
                suppressed_findings=[],
                blocked_reasons=[],
            )

        assert rc == 0
        failed_calls = [
            c for c in mock_sis.call_args_list if c[0][1] == "fix_failed_verification"
        ]
        assert len(failed_calls) == 1
        assert "fixes_failed_verification=1" in captured.getvalue()

    def test_marks_resolved_when_not_firing(self, tmp_path):
        findings = [_make_finding(rule="ruff-c408", path="src/main.py")]
        issues_data = {
            "issues": [
                {
                    "issue_id": "i1",
                    "rule": "other-rule",
                    "path": "other.py",
                    "status": "open",
                }
            ]
        }
        state = {}
        captured = StringIO()
        with (
            patch("bluei.engine.commands.verify_only.save_issues"),
            patch("bluei.engine.commands.verify_only.save_state"),
            patch("bluei.engine.commands.verify_only.update_status_artifact"),
            patch("bluei.engine.orchestrator.set_issue_status") as mock_sis,
            patch("sys.stdout", captured),
        ):
            rc = run_verify_only(
                findings=findings,
                issues_data=issues_data,
                issues_file=tmp_path / "issues.json",
                state_file=tmp_path / "state.json",
                state=state,
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                args=_args(),
                reconcile_event={"reason": "test"},
                previous_last_run_at=None,
                open_issues=1,
                open_prs=0,
                written_findings=1,
                suppressed_findings=[],
                blocked_reasons=[],
            )

        assert rc == 0
        verified_calls = [
            c for c in mock_sis.call_args_list if c[0][1] == "resolved_verified"
        ]
        assert len(verified_calls) == 1
        assert "fixes_verified=1" in captured.getvalue()

    def test_empty_findings_resolves_all_issues(self, tmp_path):
        issues_data = {
            "issues": [
                {"issue_id": "i1", "rule": "a", "path": "x.py", "status": "open"},
                {"issue_id": "i2", "rule": "b", "path": "y.py", "status": "open"},
            ]
        }
        state = {}
        captured = StringIO()
        with (
            patch("bluei.engine.commands.verify_only.save_issues"),
            patch("bluei.engine.commands.verify_only.save_state"),
            patch("bluei.engine.commands.verify_only.update_status_artifact"),
            patch("bluei.engine.orchestrator.set_issue_status") as mock_sis,
            patch("sys.stdout", captured),
        ):
            rc = run_verify_only(
                findings=[],
                issues_data=issues_data,
                issues_file=tmp_path / "issues.json",
                state_file=tmp_path / "state.json",
                state=state,
                status_file=tmp_path / "status.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                args=_args(),
                reconcile_event={"reason": "test"},
                previous_last_run_at=None,
                open_issues=2,
                open_prs=0,
                written_findings=0,
                suppressed_findings=[],
                blocked_reasons=[],
            )

        assert rc == 0
        verified_calls = [
            c for c in mock_sis.call_args_list if c[0][1] == "resolved_verified"
        ]
        assert len(verified_calls) == 2


class TestReconcileOnly:
    def test_saves_state_and_returns_zero(self, tmp_path):
        state = {"open_issues": 3, "open_prs": 1}
        captured = StringIO()
        with (
            patch("bluei.engine.commands.reconcile.save_state") as mock_ss,
            patch("bluei.engine.commands.reconcile.update_status_artifact"),
            patch("sys.stdout", captured),
        ):
            rc = run_reconcile_only(
                state_file=tmp_path / "state.json",
                state=state,
                status_file=tmp_path / "status.json",
                issues_file=tmp_path / "issues.json",
                findings_file=tmp_path / "findings.json",
                log_file=tmp_path / "test.log",
                args=_args(),
                reconcile_event={"reason": "scheduled"},
                previous_last_run_at=None,
                open_issues=3,
                open_prs=1,
            )

        assert rc == 0
        mock_ss.assert_called_once()
        assert "RECONCILE-ONLY" in captured.getvalue()
        assert "open_issues=3" in captured.getvalue()
