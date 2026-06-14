#!/usr/bin/env python3
"""Tests for bluei/app/report.py."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bluei.app.report import ReportGenerator
from bluei.app.models import (
    Repo,
    RepoConfig,
    HealthScore,
    Baseline,
    RepoStatus,
)


def make_repo(name="test-repo", language="python") -> Repo:
    config = RepoConfig(
        id=f"repo-{name}",
        name=name,
        path=f"/tmp/{name}",
        language=language,
    )
    return Repo(
        config=config,
        status=RepoStatus.READY,
        current_findings_count=5,
        current_health_score=82.0,
        total_fixes=2,
        total_prs=1,
        total_merges=0,
    )


class TestReportGeneratorFormatScoreBand:
    def setup_method(self):
        self.rg = ReportGenerator()

    def test_band_excellent(self):
        assert self.rg._format_score_band(95) == "Excellent 🟢"
        assert self.rg._format_score_band(100) == "Excellent 🟢"

    def test_band_good(self):
        assert self.rg._format_score_band(85) == "Good 🟢"

    def test_band_needs_work(self):
        assert self.rg._format_score_band(65) == "Needs Work 🟡"
        assert self.rg._format_score_band(50) == "Needs Work 🟡"

    def test_band_poor(self):
        assert self.rg._format_score_band(40) == "Poor 🟠"
        assert self.rg._format_score_band(30) == "Poor 🟠"

    def test_band_critical(self):
        assert self.rg._format_score_band(20) == "Critical 🔴"
        assert self.rg._format_score_band(0) == "Critical 🔴"


class TestReportGeneratorMarkdown:
    def setup_method(self):
        self.rg = ReportGenerator()

    def test_generates_header(self):
        repo = make_repo("mark-down-test")
        result = self.rg.generate_markdown_report(repo, None, None, [], {})
        assert f"# QA Report: mark-down-test" in result
        assert "**Language:** python" in result

    def test_no_health_shows_na(self):
        repo = make_repo()
        result = self.rg.generate_markdown_report(repo, None, None, [], {})
        assert "**Vitality:** N/A" in result

    def test_with_health_score(self):
        repo = make_repo()
        health = HealthScore(
            score=88.0,
            components={"lint": 90.0, "test_gaps": 85.0},
            calculated_at="2026-03-22T00:00:00Z",
        )
        result = self.rg.generate_markdown_report(repo, None, health, [], {})
        assert "**Vitality:** 88.0" in result
        assert "Good 🟢" in result  # 88 is "good" band

    def test_with_findings_by_category(self):
        repo = make_repo()
        findings = {
            "lint": 3,
            "complexity": 2,
            "test_gaps": 1,
        }
        result = self.rg.generate_markdown_report(repo, None, None, [], findings)
        assert "## Specks Breakdown" in result
        assert "| Lint | 3 |" in result  # .title() applied
        assert "| Complexity | 2 |" in result

    def test_no_findings_empty_message(self):
        repo = make_repo()
        result = self.rg.generate_markdown_report(repo, None, None, [], {})
        assert "No findings data available" in result

    def test_with_health_history(self):
        repo = make_repo()
        history = [
            {"timestamp": "2026-03-20T00:00:00Z", "score": 80.0, "findings_count": 7},
            {"timestamp": "2026-03-21T00:00:00Z", "score": 82.0, "findings_count": 5},
        ]
        result = self.rg.generate_markdown_report(repo, None, None, history, {})
        assert "## Health History" in result
        assert "80.0" in result
        assert "82.0" in result

    def test_with_baseline_improvement(self):
        repo = make_repo()
        baseline = Baseline(
            id="b1",
            repo_id="repo-test",
            captured_at="2026-03-01T00:00:00Z",
            findings_total=10,
            findings_by_category={},
            findings_by_severity={},
            health_score=70.0,
            health_components={},
            findings_file="/tmp/baseline.jsonl",
        )
        health = HealthScore(
            score=85.0,
            components={},
            calculated_at="2026-03-22T00:00:00Z",
        )
        result = self.rg.generate_markdown_report(repo, baseline, health, [], {})
        assert "improved by 15.0 points" in result

    def test_with_baseline_decline(self):
        repo = make_repo()
        baseline = Baseline(
            id="b1",
            repo_id="repo-test",
            captured_at="2026-03-01T00:00:00Z",
            findings_total=5,
            findings_by_category={},
            findings_by_severity={},
            health_score=90.0,
            health_components={},
            findings_file="/tmp/baseline.jsonl",
        )
        health = HealthScore(
            score=75.0,
            components={},
            calculated_at="2026-03-22T00:00:00Z",
        )
        result = self.rg.generate_markdown_report(repo, baseline, health, [], {})
        assert "declined by 15.0 points" in result

    def test_review_care_section_active_prs(self):
        repo = make_repo()
        review_care = {
            "active_managed_prs": 2,
            "review_blocked_prs": 0,
            "retry_eligible_prs": 1,
            "retry_planned_prs": 0,
            "retry_prepared_prs": 0,
            "retry_executed_prs": 0,
            "pending_push_prs": 0,
            "failed_push_prs": 0,
            "retry_failed_prs": 0,
            "retry_exhausted_prs": 0,
            "merge_ready_prs": 0,
            "paused_prs": 0,
            "pending_push_prs_detail": [],
            "failed_push_prs_detail": [],
            "exhausted_prs_detail": [],
        }
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, review_care=review_care
        )
        assert "## Review Care Status" in result
        assert "Active Managed PRs:** 2" in result

    def test_review_care_pending_push_detail(self):
        repo = make_repo()
        review_care = {
            "active_managed_prs": 1,
            "review_blocked_prs": 0,
            "retry_eligible_prs": 0,
            "retry_planned_prs": 0,
            "retry_prepared_prs": 0,
            "retry_executed_prs": 0,
            "pending_push_prs": 1,
            "failed_push_prs": 0,
            "retry_failed_prs": 0,
            "retry_exhausted_prs": 0,
            "merge_ready_prs": 0,
            "paused_prs": 0,
            "pending_push_prs_detail": [
                {
                    "pr_number": 33,
                    "branch": "qa/live-complexity",
                    "changed_files": ["src/a.ts", "src/b.ts"],
                    "push_target_branch": "main",
                }
            ],
            "failed_push_prs_detail": [],
            "exhausted_prs_detail": [],
        }
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, review_care=review_care
        )
        assert "### Pending Push Approval" in result
        assert "#33" in result

    def test_review_care_monitored_safety_detail(self):
        repo = make_repo()
        review_care = {
            "active_managed_prs": 1,
            "review_blocked_prs": 0,
            "retry_eligible_prs": 0,
            "retry_planned_prs": 0,
            "retry_prepared_prs": 0,
            "retry_executed_prs": 0,
            "pending_push_prs": 0,
            "failed_push_prs": 0,
            "retry_failed_prs": 0,
            "retry_exhausted_prs": 0,
            "merge_ready_prs": 0,
            "paused_prs": 0,
            "pending_push_prs_detail": [],
            "failed_push_prs_detail": [],
            "exhausted_prs_detail": [],
            "live_rollout_mode": "limited",
            "guarded_live_review": True,
            "safety_circuit_open": True,
            "safety_failure_count": 3,
            "safety_cooldown_until": "2026-04-11T16:30:00Z",
            "auto_rollback_active": True,
            "auto_rollback_reason": "negative-feedback-ratio-0.67-over-threshold-0.30",
            "auto_rollback_triggered_at": "2026-04-11T16:00:00Z",
            "operator_action_required": True,
            "operator_action_summary": "disable-guarded-live-review-and-fallback-to-shadow",
            "suggested_review_care_patch": {
                "guarded_live_review": False,
                "live_rollout_mode": "shadow",
            },
        }
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, review_care=review_care
        )
        assert "### Monitored Safety" in result
        assert "**Live Rollout Mode:** limited" in result
        assert "**Safety Circuit Open:** True" in result
        assert "**Auto Rollback Active:** True" in result
        assert "negative-feedback-ratio-0.67-over-threshold-0.30" in result
        assert (
            "**Operator Action Summary:** disable-guarded-live-review-and-fallback-to-shadow"
            in result
        )
        assert "`guarded_live_review: False`" in result
        assert "`live_rollout_mode: shadow`" in result

    def test_metrics_section(self):
        repo = make_repo(
            name="metrics-test",
            language="typescript",
        )
        repo.current_findings_count = 12
        repo.total_fixes = 5
        repo.total_prs = 3
        repo.total_merges = 1
        result = self.rg.generate_markdown_report(repo, None, None, [], {})
        assert "**Total Specks:** 12" in result
        assert "**Total Fixes Applied:** 5" in result
        assert "**Total PRs Created:** 3" in result
        assert "**Total Merges:** 1" in result

    def test_generated_by_footer(self):
        repo = make_repo()
        result = self.rg.generate_markdown_report(repo, None, None, [], {})
        assert "generated by bluei" in result.lower()


class TestReportGeneratorFromStateDir:
    """I2: ReportGenerator.from_state_dir() bridges canonical JSON to markdown."""

    def _setup_state(self, tmp_path, repo_name="state-demo"):
        state_dir = tmp_path / "repos" / repo_name / "state"
        state_dir.mkdir(parents=True)

        status = {
            "current_counts": {"open_issues": 4, "open_prs": 1},
            "health": {"score": 76.5},
            "language": "python",
            "last_run_at": "2026-02-01T08:30:00Z",
            "detector_catalog": [
                {"rule": "ruff-c408", "category": "lint", "language": "python"},
            ],
        }
        (state_dir / "status.json").write_text(json.dumps(status))

        findings = [
            {"rule": "ruff-c408", "path": "a.py", "line": 1, "severity": "medium"},
            {"rule": "ruff-c408", "path": "b.py", "line": 5, "severity": "medium"},
            {"rule": "debt-old-api", "path": "c.py", "line": 10, "severity": "low"},
        ]
        (state_dir / "findings.jsonl").write_text(
            "\n".join(json.dumps(f) for f in findings) + "\n"
        )

        health_history = [
            {"timestamp": "2026-01-30T10:00:00Z", "score": 70.0, "findings_count": 6},
            {"timestamp": "2026-02-01T08:30:00Z", "score": 76.5, "findings_count": 3},
        ]
        (state_dir / "health_history.jsonl").write_text(
            "\n".join(json.dumps(h) for h in health_history) + "\n"
        )

        return state_dir

    def test_returns_markdown_string(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        markdown = ReportGenerator.from_state_dir(
            repo_name="state-demo",
            state_dir=state_dir,
            workspace=tmp_path,
        )
        assert isinstance(markdown, str)
        assert "# QA Report: state-demo" in markdown

    def test_bridges_repo_metadata(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        markdown = ReportGenerator.from_state_dir(
            repo_name="state-demo",
            state_dir=state_dir,
            workspace=tmp_path,
        )
        assert "**Language:** python" in markdown
        assert "**Vitality:** 76.5" in markdown

    def test_bridges_findings_by_category(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        markdown = ReportGenerator.from_state_dir(
            repo_name="state-demo",
            state_dir=state_dir,
            workspace=tmp_path,
        )
        assert "## Specks Breakdown" in markdown
        # ruff-c408 infers to 'lint', debt-old-api infers to 'debt'
        assert "| Lint | 2 |" in markdown
        assert "| Debt | 1 |" in markdown

    def test_bridges_history_table(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        markdown = ReportGenerator.from_state_dir(
            repo_name="state-demo",
            state_dir=state_dir,
            workspace=tmp_path,
        )
        assert "## Health History" in markdown
        # The bridged history uses ISO timestamps; date column shows YYYY-MM-DD.
        assert "2026-01-30" in markdown
        assert "2026-02-01" in markdown

    def test_bridges_total_findings_metric(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        markdown = ReportGenerator.from_state_dir(
            repo_name="state-demo",
            state_dir=state_dir,
            workspace=tmp_path,
        )
        # 3 findings in the jsonl
        assert "**Total Specks:** 3" in markdown

    def test_works_with_empty_state_dir(self, tmp_path):
        state_dir = tmp_path / "empty" / "state"
        state_dir.mkdir(parents=True)
        markdown = ReportGenerator.from_state_dir(
            repo_name="empty-repo",
            state_dir=state_dir,
            workspace=tmp_path,
        )
        assert "# QA Report: empty-repo" in markdown
        assert "**Vitality:** N/A" in markdown

    def test_uses_repo_path_when_provided(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        markdown = ReportGenerator.from_state_dir(
            repo_name="state-demo",
            state_dir=state_dir,
            repo_path="/custom/path/to/repo",
            workspace=tmp_path,
        )
        assert "**Repository:** /custom/path/to/repo" in markdown


class TestEmergentRulesUsesEngineReport:
    """I1: emergent_rules delegates inference to engine.report."""

    def test_emergent_rules_no_longer_defines_local_inferrers(self):
        """The local _infer_language/_infer_category definitions should be
        gone — the canonical versions live in bluei.engine.report."""
        import bluei.app.emergent_rules as mod

        # If the duplicates still exist as module-level definitions, they'd
        # show up here. The imported canonical functions are still accessible
        # but the local def lines are gone.
        source = open(mod.__file__).read()
        # Look for the def lines (not import lines)
        assert "def _infer_language(" not in source
        assert "def _infer_category(" not in source

    def test_emergent_rules_imports_from_engine_report(self):
        import bluei.app.emergent_rules as mod

        assert hasattr(mod, "_infer_category")
        assert hasattr(mod, "infer_language_from_path")
        # _infer_category must be the SAME function object as engine.report's
        from bluei.engine.report import _infer_category as engine_cat

        assert mod._infer_category is engine_cat

    def test_proposed_rule_uses_engine_regex_category(self):
        """With the consolidation, the proposed emergent rule's category
        reflects the richer engine.report regex catalog, not the old
        substring heuristic."""
        from bluei.app.emergent_rules import propose_rules_from_findings
        from bluei.engine.models import Finding

        findings = [
            Finding(
                finding_id=f"f-{i}",
                repo="demo",
                path=f"src/api/{i}.py",
                line=1,
                rule="ruff-c408",
                snippet="",
                confidence=0.8,
                quick_win=False,
                safe_to_autofix=False,
            )
            for i in range(3)
        ]
        proposals = propose_rules_from_findings(
            findings, run_id="r1", min_observations=2
        )
        assert len(proposals) == 1
        # Engine maps ruff-* → 'lint' (richer than old 'no match → lint' default)
        assert proposals[0].category == "lint"

    def test_proposed_rule_language_uses_path_suffix(self):
        from bluei.app.emergent_rules import propose_rules_from_findings
        from bluei.engine.models import Finding

        findings = [
            Finding(
                finding_id=f"f-{i}",
                repo="demo",
                path=f"src/api/{i}.go",
                line=1,
                rule="some-rule",
                snippet="",
                confidence=0.8,
                quick_win=False,
                safe_to_autofix=False,
            )
            for i in range(3)
        ]
        proposals = propose_rules_from_findings(
            findings, run_id="r1", min_observations=2
        )
        assert len(proposals) == 1
        assert proposals[0].language == "go"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
