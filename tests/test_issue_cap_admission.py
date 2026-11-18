"""The open-issue cap gates new admissions only.

The scan stays fresh and existing issues (refactor refresh, recurrence
reopen) are never blocked by the cap — only creating a new issue is.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
        max_issues_per_run=0,
        open_issues_cap=2,
        repo_path="/tmp/repo",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _routed_item(finding_id="f001"):
    work = MagicMock()
    work.phase.value = "planning"
    return {
        "finding": _finding(finding_id=finding_id),
        "refactor_work": work,
        "queued_work_id": f"qw-{finding_id}",
        "reason": "structural",
    }


def _issue(finding_id, status="open"):
    return {
        "issue_id": f"QA-{finding_id}",
        "finding_id": finding_id,
        "status": status,
    }


class TestRefactorAdmissionCap:
    """Refactor loop: the guard fires only for NEW issues, never refresh."""

    @patch("bluei.engine.orchestrator.set_issue_status")
    @patch("bluei.engine.orchestrator.ensure_issue_for_finding")
    @patch("bluei.engine.orchestrator.find_issue_for_finding", return_value=None)
    def test_cap_full_blocks_new_refactor_issue(
        self, mock_find, mock_ensure, mock_status, tmp_path
    ):
        issues_data = {"issues": [_issue("e1"), _issue("e2")]}

        created, open_issues, blocked = run_issue_creation_phase(
            issues_data=issues_data,
            eligible_findings=[],
            refactor_routed_items=[_routed_item("f001")],
            log_file=tmp_path / "log",
            args=_args(),
            gh_repo_slug="acme/widget",
            open_issues=2,
        )

        mock_ensure.assert_not_called()
        assert created == []
        assert open_issues == 2
        assert len(blocked) == 1
        assert "meets/exceeds issue cap" in blocked[0]
        assert len(issues_data["issues"]) == 2

    @patch("bluei.engine.orchestrator.set_issue_status")
    @patch("bluei.engine.orchestrator.ensure_issue_for_finding")
    def test_cap_full_still_refreshes_existing_refactor_issue(
        self, mock_ensure, mock_status, tmp_path
    ):
        existing = _issue("f001")
        issues_data = {"issues": [existing, _issue("e2")]}
        mock_ensure.return_value = existing

        created, open_issues, blocked = run_issue_creation_phase(
            issues_data=issues_data,
            eligible_findings=[],
            refactor_routed_items=[_routed_item("f001")],
            log_file=tmp_path / "log",
            args=_args(),
            gh_repo_slug="acme/widget",
            open_issues=2,
        )

        mock_ensure.assert_called_once()
        assert created == []
        assert open_issues == 2
        assert blocked == []


class TestBatchAdmissionCap:
    """Batch creation: admissions within one run never exceed the cap."""

    def test_batch_creation_never_exceeds_cap(self, tmp_path):
        issues_data = {"issues": [_issue("e1"), _issue("e2")]}
        eligible = [_finding(finding_id=f"f00{i}") for i in range(5)]

        with patch("bluei.engine.constants.AGENT_ROOT", tmp_path):
            created, open_issues, blocked = run_issue_creation_phase(
                issues_data=issues_data,
                eligible_findings=eligible,
                refactor_routed_items=[],
                log_file=tmp_path / "log",
                args=_args(max_issues_per_run=10, open_issues_cap=3),
                gh_repo_slug="acme/widget",
                open_issues=2,
            )

        assert len(created) == 1
        assert open_issues == 3
        assert len(issues_data["issues"]) == 3
        assert any("meets/exceeds issue cap" in reason for reason in blocked)

    def test_recurrence_reopen_respects_full_cap(self, tmp_path):
        existing = {
            "issue_id": "QA-f001",
            "finding_id": "f001",
            "status": "resolved_verified",
            "github": {"issue_number": 5},
        }
        issues_data = {"issues": [_issue("e1"), _issue("e2"), existing]}
        eligible = [
            _finding(finding_id="f001", rule="md-MD001", confidence=0.9)
        ]

        with patch("bluei.engine.constants.AGENT_ROOT", tmp_path):
            created, open_issues, blocked = run_issue_creation_phase(
                issues_data=issues_data,
                eligible_findings=eligible,
                refactor_routed_items=[],
                log_file=tmp_path / "log",
                args=_args(dry_run=False, max_issues_per_run=1),
                gh_repo_slug="acme/widget",
                open_issues=2,
            )

        assert existing["status"] == "resolved_verified"
        assert created == []
        assert open_issues == 2
        assert any("meets/exceeds issue cap" in reason for reason in blocked)
