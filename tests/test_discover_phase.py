"""Tests for bluei.engine.commands.discover — discovery routing, cooldown, refactor classification."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.discover import run_discover_phase
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
        run_phase="full",
        live_github_actions=False,
        dry_run=False,
        issue_confidence_threshold=0.5,
        open_issues_cap=20,
        finding_cooldown_seconds=600,
        repo_path="/tmp/repo",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestDiscoverPhaseRouting:
    @patch("bluei.engine.orchestrator.route_findings_with_intent")
    @patch("bluei.engine.reforge.classify_finding")
    @patch("bluei.engine.utils.is_path_tracked", return_value=True)
    @patch("bluei.engine.orchestrator.discover_findings")
    @patch("bluei.engine.commands.discover.append_findings", return_value=2)
    @patch("bluei.engine.commands.discover.filter_findings_by_cooldown")
    @patch(
        "bluei.engine.commands.discover.guard_open_issues", return_value=(True, "ok")
    )
    @patch("bluei.engine.commands.discover.count_actionable_issues", return_value=0)
    @patch("bluei.engine.commands.discover._append_text")
    def test_separates_refactor_from_eligible(
        self,
        mock_log,
        mock_count,
        mock_guard,
        mock_filter,
        mock_append,
        mock_discover,
        mock_tracked,
        mock_classify,
        mock_route,
    ):
        f1 = _finding(finding_id="f001", rule="ruff-c408")
        f2 = _finding(finding_id="f002", rule="complex-method")
        mock_discover.return_value = [f1, f2]
        mock_filter.return_value = ([f1, f2], [])

        from bluei.engine.reforge import RefactorClass

        def classify(f):
            if f.rule == "complex-method":
                return RefactorClass.REFACTOR_CLASS
            return "quick-win"

        mock_classify.side_effect = classify
        mock_route.return_value = {
            "refactor_queue": [
                {
                    "finding": f2,
                    "refactor_work": MagicMock(),
                    "queued_work_id": "qw1",
                    "reason": "planning",
                }
            ],
            "skipped": [],
        }

        result = run_discover_phase(
            repo_path=Path("/repo"),
            docs_index_file=Path("/docs.json"),
            findings_file=Path("/findings.json"),
            log_file=Path("/test.log"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            run_issue_cycle=True,
            run_pr_cycle=True,
        )

        findings, written, eligible, suppressed, routed, blocked, cap_ok = result
        assert len(eligible) == 1
        assert eligible[0].finding_id == "f001"
        assert len(routed) == 1
        assert routed[0]["finding"].finding_id == "f002"

    @patch("bluei.engine.reforge.classify_finding")
    @patch("bluei.engine.utils.is_path_tracked", return_value=True)
    @patch("bluei.engine.orchestrator.discover_findings")
    @patch("bluei.engine.commands.discover.append_findings", return_value=0)
    @patch("bluei.engine.commands.discover.filter_findings_by_cooldown")
    @patch(
        "bluei.engine.commands.discover.guard_open_issues", return_value=(True, "ok")
    )
    @patch("bluei.engine.commands.discover.count_actionable_issues", return_value=0)
    @patch("bluei.engine.commands.discover._append_text")
    def test_no_findings_returns_empty(
        self,
        mock_log,
        mock_count,
        mock_guard,
        mock_filter,
        mock_append,
        mock_discover,
        mock_tracked,
        mock_classify,
    ):
        mock_discover.return_value = []
        mock_filter.return_value = ([], [])

        result = run_discover_phase(
            repo_path=Path("/repo"),
            docs_index_file=Path("/docs.json"),
            findings_file=Path("/findings.json"),
            log_file=Path("/test.log"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            run_issue_cycle=False,
            run_pr_cycle=True,
        )

        findings, written, eligible, suppressed, routed, blocked, cap_ok = result
        assert findings == []
        assert eligible == []
        assert routed == []

    @patch("bluei.engine.reforge.classify_finding")
    @patch("bluei.engine.orchestrator.discover_findings")
    @patch("bluei.engine.commands.discover.append_findings", return_value=1)
    @patch("bluei.engine.commands.discover.filter_findings_by_cooldown")
    @patch(
        "bluei.engine.commands.discover.guard_open_issues",
        return_value=(False, "cap hit"),
    )
    @patch("bluei.engine.commands.discover.count_actionable_issues", return_value=25)
    @patch("bluei.engine.commands.discover._append_text")
    def test_cap_hit_blocks_discovery(
        self,
        mock_log,
        mock_count,
        mock_guard,
        mock_filter,
        mock_append,
        mock_discover,
        mock_classify,
    ):
        mock_discover.return_value = []
        mock_filter.return_value = ([], [])

        result = run_discover_phase(
            repo_path=Path("/repo"),
            docs_index_file=Path("/docs.json"),
            findings_file=Path("/findings.json"),
            log_file=Path("/test.log"),
            state={},
            issues_data={"issues": []},
            args=_args(open_issues_cap=20),
            run_issue_cycle=True,
            run_pr_cycle=False,
        )

        _, _, _, _, _, blocked, cap_ok = result
        assert cap_ok is False
        assert len(blocked) == 1

    @patch("bluei.engine.reforge.classify_finding")
    @patch("bluei.engine.utils.is_path_tracked")
    @patch("bluei.engine.orchestrator.discover_findings")
    @patch("bluei.engine.commands.discover.append_findings", return_value=2)
    @patch("bluei.engine.commands.discover.filter_findings_by_cooldown")
    @patch(
        "bluei.engine.commands.discover.guard_open_issues", return_value=(True, "ok")
    )
    @patch("bluei.engine.commands.discover.count_actionable_issues", return_value=0)
    @patch("bluei.engine.commands.discover._append_text")
    def test_live_mode_filters_untracked(
        self,
        mock_log,
        mock_count,
        mock_guard,
        mock_filter,
        mock_append,
        mock_discover,
        mock_tracked,
        mock_classify,
    ):
        f1 = _finding(finding_id="f001", path="tracked.py")
        f2 = _finding(finding_id="f002", path="untracked.py")
        mock_discover.return_value = [f1, f2]
        mock_filter.return_value = ([f1, f2], [])
        mock_tracked.side_effect = lambda repo, p: p == "tracked.py"

        result = run_discover_phase(
            repo_path=Path("/repo"),
            docs_index_file=Path("/docs.json"),
            findings_file=Path("/findings.json"),
            log_file=Path("/test.log"),
            state={},
            issues_data={"issues": []},
            args=_args(live_github_actions=True),
            run_issue_cycle=False,
            run_pr_cycle=True,
        )

        _, _, eligible, _, _, _, _ = result
        assert len(eligible) == 1
        assert eligible[0].finding_id == "f001"

    @patch("bluei.engine.orchestrator.discover_findings")
    @patch("bluei.engine.commands.discover.append_findings", return_value=0)
    @patch("bluei.engine.commands.discover.filter_findings_by_cooldown")
    @patch("bluei.engine.commands.discover._append_text")
    def test_cooldown_suppresses_findings(
        self, mock_log, mock_filter, mock_append, mock_discover
    ):
        f1 = _finding(finding_id="f001")
        mock_discover.return_value = [f1]
        mock_filter.return_value = ([], [f1])

        result = run_discover_phase(
            repo_path=Path("/repo"),
            docs_index_file=Path("/docs.json"),
            findings_file=Path("/findings.json"),
            log_file=Path("/test.log"),
            state={},
            issues_data={"issues": []},
            args=_args(),
            run_issue_cycle=False,
            run_pr_cycle=False,
        )

        _, _, eligible, suppressed, _, _, _ = result
        assert eligible == []
        assert len(suppressed) == 1
