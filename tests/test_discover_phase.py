"""Tests for bluei.engine.commands.discover — discovery routing, cooldown, refactor classification."""

import json
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


def test_incomplete_discovery_persists_but_does_not_route(tmp_path):
    finding = _finding(rule="complex-method")

    def partial(repo_path, **kwargs):
        kwargs["outcomes"]["ruff"] = {"status": "failed", "count": 0, "reason": "timeout"}
        return [finding]

    with (
        patch("bluei.engine.orchestrator.discover_findings", side_effect=partial),
        patch("bluei.engine.orchestrator.route_findings_with_intent") as route,
        patch("bluei.engine.commands.discover.filter_findings_by_cooldown") as cooldown,
    ):
        result = run_discover_phase(
            repo_path=tmp_path, docs_index_file=tmp_path / "docs.json",
            findings_file=tmp_path / "findings.jsonl", log_file=tmp_path / "log",
            state={}, issues_data={"issues": []}, args=_args(),
            run_issue_cycle=True, run_pr_cycle=True,
        )
    assert result[0] == [finding] and result[1] == 1
    assert result[2:5] == ([], [], []) and result[-1] is True
    assert "discovery incomplete" in result[-2][0]
    route.assert_not_called()
    cooldown.assert_not_called()


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
        return_value=(False, "guard-block: open issues=25 meets/exceeds issue cap=20"),
    )
    @patch("bluei.engine.commands.discover.count_actionable_issues", return_value=25)
    @patch("bluei.engine.commands.discover._append_text")
    def test_cap_hit_still_runs_fresh_discovery(
        self,
        mock_log,
        mock_count,
        mock_guard,
        mock_filter,
        mock_append,
        mock_discover,
        mock_classify,
    ):
        f1 = _finding(finding_id="f001")
        mock_discover.return_value = [f1]
        mock_filter.return_value = ([f1], [])

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

        findings, written, eligible, suppressed, routed, blocked, cap_ok = result
        # Cap gates new admissions only — the scan itself stays fresh.
        mock_discover.assert_called_once()
        assert findings == [f1] and written == 1
        assert cap_ok is False
        assert blocked == [
            "guard-block: open issues=25 meets/exceeds issue cap=20"
        ]
        # Findings still route so the existing backlog stays repairable.
        assert eligible == [f1]

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


def _issue_for(finding_id, status="open"):
    return {"issue_id": f"QA-{finding_id}", "finding_id": finding_id, "status": status}


def _ruff_alias_loader(group):
    def _load(path, aliases=None):
        for fid in group:
            aliases[fid] = set(group)
        return {}

    return _load


class TestRuffAliasIdentity:
    """Ruff aliases reuse the Markdown routing logic with family labels."""

    @patch("bluei.engine.commands.discover._load_ruff_identities")
    @patch("bluei.engine.orchestrator.discover_findings")
    @patch("bluei.engine.commands.discover.append_findings", return_value=1)
    @patch("bluei.engine.commands.discover._append_text")
    def test_ruff_multiple_active_aliases_fail_closed(
        self, mock_log, mock_append, mock_discover, mock_ruff_load
    ):
        finding = _finding(finding_id="a1", rule="ruff-E501")
        mock_discover.return_value = [finding]
        mock_ruff_load.side_effect = _ruff_alias_loader({"a1", "a2"})
        issues = {"issues": [_issue_for("a1"), _issue_for("a2")]}

        result = run_discover_phase(
            repo_path=Path("/repo"),
            docs_index_file=Path("/docs.json"),
            findings_file=Path("/findings.json"),
            log_file=Path("/test.log"),
            state={},
            issues_data=issues,
            args=_args(),
            run_issue_cycle=True,
            run_pr_cycle=False,
        )

        findings, written, eligible, suppressed, routed, blocked, cap_ok = result
        assert eligible == [] and routed == []
        assert suppressed == [finding]
        assert cap_ok is True
        assert any("Ruff alias conflict for a1" in reason for reason in blocked)

    @patch("bluei.engine.reforge.classify_finding")
    @patch("bluei.engine.commands.discover.filter_findings_by_cooldown")
    @patch("bluei.engine.commands.discover._load_ruff_identities")
    @patch("bluei.engine.orchestrator.discover_findings")
    @patch("bluei.engine.commands.discover.append_findings", return_value=1)
    @patch("bluei.engine.commands.discover._append_text")
    def test_ruff_alias_rebinds_to_active_issue_id(
        self, mock_log, mock_append, mock_discover, mock_ruff_load,
        mock_filter, mock_classify,
    ):
        # Earliest-ID adoption bound the finding to a1, but the live issue
        # tracks a2: routing must rebind so no duplicate is admitted below cap.
        finding = _finding(finding_id="a1", rule="ruff-E501")
        mock_discover.return_value = [finding]
        mock_ruff_load.side_effect = _ruff_alias_loader({"a1", "a2"})
        mock_filter.return_value = ([finding], [])
        issues = {"issues": [_issue_for("a2")]}

        result = run_discover_phase(
            repo_path=Path("/repo"),
            docs_index_file=Path("/docs.json"),
            findings_file=Path("/findings.json"),
            log_file=Path("/test.log"),
            state={},
            issues_data=issues,
            args=_args(),
            run_issue_cycle=True,
            run_pr_cycle=False,
        )

        findings, written, eligible, suppressed, routed, blocked, cap_ok = result
        assert finding.finding_id == "a2"
        assert eligible == [finding]
        assert not any("alias conflict" in reason for reason in blocked)

    @patch("bluei.engine.commands.discover._load_markdown_identities")
    @patch("bluei.engine.orchestrator.discover_findings")
    @patch("bluei.engine.commands.discover.append_findings", return_value=1)
    @patch("bluei.engine.commands.discover._append_text")
    def test_markdown_conflict_outcome_keeps_compatibility_key(
        self, mock_log, mock_append, mock_discover, mock_md_load, tmp_path
    ):
        finding = _finding(finding_id="m1", rule="md-MD009")
        mock_discover.return_value = [finding]

        def _load(path, aliases=None):
            for fid in ("m1", "m2"):
                aliases[fid] = {"m1", "m2"}
            return {}

        mock_md_load.side_effect = _load
        from bluei.engine.commands.context import RunContext

        ctx = RunContext(
            args=_args(), repo_path=tmp_path, log_file=tmp_path / "run.log",
            findings_file=tmp_path / "findings.jsonl",
            docs_index_file=tmp_path / "docs.json",
            status_file=tmp_path / "status.json", state={},
            issues_data={"issues": [_issue_for("m1"), _issue_for("m2")]},
            run_id="r1", run_issue_cycle=True, run_pr_cycle=False,
        )
        result = run_discover_phase(ctx)

        assert any("Markdown alias conflict for m1" in r for r in result[5])
        current = json.loads((tmp_path / "discovery-current.json").read_text())
        assert current["detector_outcomes"]["markdown-identity"]["status"] == "failed"
        assert current["detector_outcomes"]["finding-identity"]["status"] == "failed"
