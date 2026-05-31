"""Tests for bluei/app/report.py — PDF generation, review care edge cases, metrics."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from bluei.app.models import (
    Baseline,
    HealthScore,
    Repo,
    RepoConfig,
    RepoStatus,
)
from bluei.app.report import ReportGenerator


def make_repo(name="test-repo", language="python") -> Repo:
    config = RepoConfig(
        id=f"repo-{name}",
        name=name,
        path=f"/tmp/{name}",
        language=language,
    )
    repo = Repo(
        config=config,
        status=RepoStatus.READY,
        current_findings_count=5,
        current_health_score=82.0,
        total_fixes=2,
        total_prs=1,
        total_merges=0,
    )
    repo.onboarded_at = "2026-01-15T00:00:00Z"
    repo.last_run_at = "2026-03-20T12:00:00Z"
    return repo


class TestReportGeneratorPDF:
    def test_generate_pdf_falls_back_to_markdown_when_skill_missing(self, tmp_path):
        rg = ReportGenerator(workspace=tmp_path)
        repo = make_repo()
        output = tmp_path / "reports" / "test.pdf"
        result = rg.generate_pdf(repo, None, None, [], {}, output_path=output)
        assert result.suffix == ".md"
        assert result.exists()
        content = result.read_text()
        assert "QA Report: test-repo" in content

    def test_generate_pdf_uses_default_output_path(self, tmp_path):
        rg = ReportGenerator(workspace=tmp_path)
        repo = make_repo()
        result = rg.generate_pdf(repo, None, None, [], {})
        assert result.parent.name == "reports"

    @patch("subprocess.run")
    def test_generate_pdf_calls_script(self, mock_run, tmp_path):
        skill_dir = tmp_path / "skills" / "pdf-report" / "scripts"
        skill_dir.mkdir(parents=True)
        gen_script = skill_dir / "generate_pdf.py"
        gen_script.write_text("# dummy script")

        mock_run.return_value = MagicMock(returncode=0)

        rg = ReportGenerator(
            pdf_report_skill_path=tmp_path / "skills" / "pdf-report",
            workspace=tmp_path,
        )
        repo = make_repo()
        output = tmp_path / "test-output.pdf"
        result = rg.generate_pdf(repo, None, None, [], {}, output_path=output)

        assert result == output
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_generate_pdf_falls_back_on_failure(self, mock_run, tmp_path):
        skill_dir = tmp_path / "skills" / "pdf-report" / "scripts"
        skill_dir.mkdir(parents=True)
        gen_script = skill_dir / "generate_pdf.py"
        gen_script.write_text("# dummy script")

        mock_run.return_value = MagicMock(returncode=1, stderr="error")

        rg = ReportGenerator(
            pdf_report_skill_path=tmp_path / "skills" / "pdf-report",
            workspace=tmp_path,
        )
        repo = make_repo()
        output = tmp_path / "test-output.pdf"
        result = rg.generate_pdf(repo, None, None, [], {}, output_path=output)

        assert result.suffix == ".md"

    def test_generate_pdf_uses_venv_python(self, tmp_path):
        skill_dir = tmp_path / "skills" / "pdf-report"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        gen_script = scripts_dir / "generate_pdf.py"
        gen_script.write_text("# dummy")
        venv_python = skill_dir / ".venv" / "bin" / "python3"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/bash")

        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            rg = ReportGenerator(
                pdf_report_skill_path=skill_dir,
                workspace=tmp_path,
            )
            repo = make_repo()
            rg.generate_pdf(repo, None, None, [], {})
            called_python = mock_run.call_args[0][0][0]
            assert str(venv_python) == called_python


class TestReportGeneratorReviewCareEdgeCases:
    def setup_method(self):
        self.rg = ReportGenerator()

    def test_failed_push_detail_section(self):
        repo = make_repo()
        review_care = {
            "active_managed_prs": 1,
            "review_blocked_prs": 0,
            "retry_eligible_prs": 0,
            "retry_planned_prs": 0,
            "retry_prepared_prs": 0,
            "retry_executed_prs": 0,
            "pending_push_prs": 0,
            "failed_push_prs": 1,
            "retry_failed_prs": 0,
            "retry_exhausted_prs": 0,
            "merge_ready_prs": 0,
            "paused_prs": 0,
            "pending_push_prs_detail": [],
            "failed_push_prs_detail": [
                {
                    "pr_number": 42,
                    "branch": "qa/fix-bug",
                    "push_status": "conflict",
                }
            ],
            "exhausted_prs_detail": [],
        }
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, review_care=review_care
        )
        assert "### Push Failures" in result
        assert "#42" in result
        assert "conflict" in result

    def test_exhausted_prs_detail_section(self):
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
            "retry_exhausted_prs": 1,
            "merge_ready_prs": 0,
            "paused_prs": 0,
            "pending_push_prs_detail": [],
            "failed_push_prs_detail": [],
            "exhausted_prs_detail": [
                {"pr_number": 55, "branch": "qa/exhausted", "attempts": 5}
            ],
        }
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, review_care=review_care
        )
        assert "### Exhausted PRs" in result
        assert "#55" in result
        assert "5" in result

    def test_last_review_cycle_at(self):
        repo = make_repo()
        review_care = {
            "active_managed_prs": 1,
            "pending_push_prs_detail": [],
            "failed_push_prs_detail": [],
            "exhausted_prs_detail": [],
            "last_review_cycle_at": "2026-04-10T15:30:00Z",
        }
        result = self.rg.generate_markdown_report(
            repo, None, None, [], {}, review_care=review_care
        )
        assert "Last Review Cycle" in result
        assert "2026-04-10" in result


class TestReportGeneratorMetrics:
    def test_onboarded_at_shown(self):
        rg = ReportGenerator()
        repo = make_repo()
        repo.onboarded_at = "2026-02-01T00:00:00Z"
        result = rg.generate_markdown_report(repo, None, None, [], {})
        assert "Onboarded:** 2026-02-01" in result

    def test_last_run_at_shown(self):
        rg = ReportGenerator()
        repo = make_repo()
        repo.last_run_at = "2026-03-25T10:00:00Z"
        result = rg.generate_markdown_report(repo, None, None, [], {})
        assert "Last Run:** 2026-03-25" in result

    def test_format_component(self):
        rg = ReportGenerator()
        result = rg._format_component("test", 75.0)
        assert "75.0%" in result
        assert "░" in result

    def test_no_components_shows_message(self):
        rg = ReportGenerator()
        repo = make_repo()
        health = HealthScore(
            score=80.0, components={}, calculated_at="2026-01-01T00:00:00Z"
        )
        result = rg.generate_markdown_report(repo, None, health, [], {})
        assert "No component data available" in result
