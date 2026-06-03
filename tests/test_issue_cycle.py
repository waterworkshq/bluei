"""Tests for bluei.engine.commands.issue_cycle — issue creation phase routing."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.issue_cycle import run_issue_creation_phase
from bluei.engine.models import Finding


def _finding(**overrides):
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
        live_github_actions=False,
        dry_run=True,
        issue_confidence_threshold=0.5,
        max_issues_per_run=10,
        open_issues_cap=20,
        repo_path="/tmp/repo",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _routed_item(finding_id="f001", **kw):
    mock_work = MagicMock()
    mock_work.phase.value = "planning"
    return {
        "finding": _finding(finding_id=finding_id),
        "refactor_work": mock_work,
        "queued_work_id": f"qw-{finding_id}",
        "reason": "structural",
        **kw,
    }


class TestIssueCreationRefactorRouting:
    @patch("bluei.engine.orchestrator.set_issue_status")
    @patch("bluei.engine.orchestrator.ensure_issue_for_finding")
    @patch("bluei.engine.orchestrator.find_issue_for_finding", return_value=None)
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.state.count_actionable_issues", return_value=1)
    def test_refactor_items_get_needs_human_status(
        self, mock_count, mock_log, mock_find, mock_ensure, mock_status
    ):
        issue = {"issue_id": "i1", "finding_id": "f001", "status": "open"}
        mock_ensure.return_value = issue
        routed = [_routed_item()]

        created, open_issues, blocked = run_issue_creation_phase(
            issues_data={"issues": []},
            eligible_findings=[],
            refactor_routed_items=routed,
            log_file=Path("/tmp/test.log"),
            args=_args(),
            gh_repo_slug="acme/widget",
            open_issues=1,
        )

        assert len(created) == 1
        assert open_issues == 2
        refactor_calls = [
            c
            for c in mock_status.call_args_list
            if c[0][1] == "needs-human-refactor-review"
        ]
        assert len(refactor_calls) == 1
        created_issue = created[0]
        assert created_issue["refactor"]["queue_work_id"] == "qw-f001"

    @patch("bluei.engine.orchestrator.set_issue_status")
    @patch("bluei.engine.orchestrator.ensure_issue_for_finding", return_value=None)
    @patch("bluei.engine.orchestrator.find_issue_for_finding", return_value=None)
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.state.count_actionable_issues", return_value=0)
    def test_ensure_returns_none_skips(
        self, mock_count, mock_log, mock_find, mock_ensure, mock_status
    ):
        routed = [_routed_item()]

        created, open_issues, blocked = run_issue_creation_phase(
            issues_data={"issues": []},
            eligible_findings=[],
            refactor_routed_items=routed,
            log_file=Path("/tmp/test.log"),
            args=_args(),
            gh_repo_slug="acme/widget",
            open_issues=0,
        )

        assert created == []


class TestIssueCreationEligibleFindings:
    @patch("bluei.engine.orchestrator.set_issue_status")
    @patch("bluei.engine.orchestrator.create_issues_for_findings")
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.state.guard_open_issues", return_value=(True, "ok"))
    @patch("bluei.engine.state.count_actionable_issues", return_value=0)
    def test_creates_issues_from_eligible(
        self, mock_count, mock_guard, mock_log, mock_create, mock_status
    ):
        f1 = _finding(finding_id="f001")
        issue1 = {"issue_id": "i1", "finding_id": "f001", "status": "open"}
        mock_create.side_effect = [[issue1], []]

        created, open_issues, blocked = run_issue_creation_phase(
            issues_data={"issues": []},
            eligible_findings=[f1],
            refactor_routed_items=[],
            log_file=Path("/tmp/test.log"),
            args=_args(max_issues_per_run=10),
            gh_repo_slug="acme/widget",
            open_issues=0,
        )

        assert len(created) == 1
        assert open_issues == 1
        assert blocked == []

    @patch("bluei.engine.orchestrator.set_issue_status")
    @patch("bluei.engine.orchestrator.create_issues_for_findings")
    @patch("bluei.engine.state._append_text")
    @patch(
        "bluei.engine.state.guard_open_issues",
        return_value=(False, "cap hit"),
    )
    @patch("bluei.engine.state.count_actionable_issues", return_value=20)
    def test_cap_hit_blocks_creation(
        self, mock_count, mock_guard, mock_log, mock_create, mock_status
    ):
        created, open_issues, blocked = run_issue_creation_phase(
            issues_data={"issues": []},
            eligible_findings=[_finding()],
            refactor_routed_items=[],
            log_file=Path("/tmp/test.log"),
            args=_args(open_issues_cap=20),
            gh_repo_slug="acme/widget",
            open_issues=20,
        )

        assert created == []
        assert len(blocked) == 1

    @patch("bluei.engine.orchestrator.set_issue_status")
    @patch("bluei.engine.orchestrator.create_issues_for_findings")
    @patch("bluei.engine.gh.create_or_update_github_issue")
    @patch("bluei.engine.gh.finding_from_issue_record")
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.state.guard_open_issues", return_value=(True, "ok"))
    @patch("bluei.engine.state.count_actionable_issues", return_value=0)
    def test_live_mode_creates_github_issues(
        self,
        mock_count,
        mock_guard,
        mock_log,
        mock_ffir,
        mock_gh_issue,
        mock_create,
        mock_status,
    ):
        f1 = _finding(finding_id="f001")
        mock_ffir.return_value = f1
        mock_gh_issue.return_value = {
            "number": 42,
            "url": "https://github.com/acme/widget/issues/42",
        }
        issue1 = {"issue_id": "i1", "finding_id": "f001", "status": "open"}
        mock_create.side_effect = [[issue1], []]

        created, open_issues, blocked = run_issue_creation_phase(
            issues_data={"issues": []},
            eligible_findings=[f1],
            refactor_routed_items=[],
            log_file=Path("/tmp/test.log"),
            args=_args(live_github_actions=True),
            gh_repo_slug="acme/widget",
            open_issues=0,
        )

        mock_gh_issue.assert_called_once()
        assert created[0]["github"]["issue_number"] == 42

    @patch("bluei.engine.orchestrator.set_issue_status")
    @patch("bluei.engine.orchestrator.create_issues_for_findings")
    @patch("bluei.engine.state._append_text")
    @patch("bluei.engine.state.guard_open_issues", return_value=(True, "ok"))
    @patch("bluei.engine.state.count_actionable_issues", return_value=0)
    def test_suppressed_issues_not_created(
        self, mock_count, mock_guard, mock_log, mock_create, mock_status
    ):
        suppressed = {
            "issue_id": "SUPPRESSED",
            "finding_id": "f001",
            "rule": "ruff-c408",
            "reason": "cross-cycle",
        }
        mock_create.return_value = [suppressed]

        created, open_issues, blocked = run_issue_creation_phase(
            issues_data={"issues": []},
            eligible_findings=[_finding()],
            refactor_routed_items=[],
            log_file=Path("/tmp/test.log"),
            args=_args(),
            gh_repo_slug="acme/widget",
            open_issues=0,
        )

        assert created == []
        assert open_issues == 0
