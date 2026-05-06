#!/usr/bin/env python3
"""Tests for qa_agent/report.py."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.report import ReportGenerator
from qa_agent.models import (
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
        assert "**Health Score:** N/A" in result

    def test_with_health_score(self):
        repo = make_repo()
        health = HealthScore(
            score=88.0,
            components={"lint": 90.0, "test_gaps": 85.0},
            calculated_at="2026-03-22T00:00:00Z",
        )
        result = self.rg.generate_markdown_report(repo, None, health, [], {})
        assert "**Health Score:** 88.0" in result
        assert "Good 🟢" in result  # 88 is "good" band

    def test_with_findings_by_category(self):
        repo = make_repo()
        findings = {
            "lint": 3,
            "complexity": 2,
            "test_gaps": 1,
        }
        result = self.rg.generate_markdown_report(repo, None, None, [], findings)
        assert "## Findings Breakdown" in result
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
        result = self.rg.generate_markdown_report(repo, None, None, [], {}, review_care=review_care)
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
        result = self.rg.generate_markdown_report(repo, None, None, [], {}, review_care=review_care)
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
        result = self.rg.generate_markdown_report(repo, None, None, [], {}, review_care=review_care)
        assert "### Monitored Safety" in result
        assert "**Live Rollout Mode:** limited" in result
        assert "**Safety Circuit Open:** True" in result
        assert "**Auto Rollback Active:** True" in result
        assert "negative-feedback-ratio-0.67-over-threshold-0.30" in result
        assert "**Operator Action Summary:** disable-guarded-live-review-and-fallback-to-shadow" in result
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
        assert "**Total Findings:** 12" in result
        assert "**Total Fixes Applied:** 5" in result
        assert "**Total PRs Created:** 3" in result
        assert "**Total Merges:** 1" in result

    def test_generated_by_footer(self):
        repo = make_repo()
        result = self.rg.generate_markdown_report(repo, None, None, [], {})
        assert "QA Agent v2.0.0" in result


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
