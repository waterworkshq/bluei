#!/usr/bin/env python3
"""Report generation for QA Agent."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import os
import subprocess
import tempfile

from .models import Repo, HealthScore, Baseline


class ReportGenerator:
    """Generates health reports for repositories."""
    
    def __init__(self, pdf_report_skill_path: Optional[Path] = None, workspace: Optional[Path] = None):
        self.workspace = (workspace or Path(os.environ.get('QA_AGENT_WORKSPACE', Path(__file__).resolve().parents[1]))).resolve()
        env_skill = os.environ.get('QA_AGENT_PDF_REPORT_SKILL')
        default_skill = self.workspace.parent / 'skills' / 'pdf-report'
        self.pdf_report_skill = Path(env_skill).expanduser().resolve() if env_skill else (pdf_report_skill_path or default_skill).resolve()
        self.generate_script = self.pdf_report_skill / 'scripts' / 'generate_pdf.py'
    
    def _format_score_band(self, score: float) -> str:
        if score >= 90:
            return "Excellent 🟢"
        elif score >= 70:
            return "Good 🟢"
        elif score >= 50:
            return "Needs Work 🟡"
        elif score >= 30:
            return "Poor 🟠"
        else:
            return "Critical 🔴"
    
    def _format_component(self, name: str, score: float) -> str:
        bar_length = int(score / 5)  # 20 blocks max
        bar = "█" * bar_length + "░" * (20 - bar_length)
        return f"{bar} {score:.1f}%"
    
    def generate_markdown_report(self,
                                  repo: Repo,
                                  baseline: Optional[Baseline],
                                  health: Optional[HealthScore],
                                  history: List[Dict],
                                  findings_by_category: Dict[str, int],
                                  review_care: Optional[Dict] = None) -> str:
        """Generate a markdown report for a repository."""

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            f"# QA Report: {repo.config.name}",
            "",
            f"**Generated:** {now}",
            f"**Repository:** {repo.config.path}",
            f"**Language:** {repo.config.language}",
            "",
            "---",
            "",
            "## Executive Summary",
            "",
        ]

        if health:
            lines.extend([
                f"**Health Score:** {health.score}/100 ({self._format_score_band(health.score)})",
                "",
            ])
        else:
            lines.extend([
                "**Health Score:** N/A",
                "",
            ])

        # Improvement metrics
        if baseline and health:
            if baseline.health_score != health.score:
                delta = health.score - baseline.health_score
                direction = "improved" if delta > 0 else "declined"
                lines.extend([
                    f"**Change since baseline:** {direction} by {abs(delta):.1f} points",
                    "",
                ])

        lines.extend([
            "---",
            "",
            "## Health Score Components",
            "",
        ])

        if health and health.components:
            for component, score in health.components.items():
                component_name = component.replace("_", " ").title()
                lines.extend([
                    f"### {component_name}",
                    "",
                    f"```\n{self._format_component(component, score)}\n```",
                    "",
                ])
        else:
            lines.extend([
                "No component data available.",
                "",
            ])

        # Findings breakdown
        lines.extend([
            "---",
            "",
            "## Findings Breakdown",
            "",
        ])

        if findings_by_category:
            lines.extend([
                "| Category | Count |",
                "|----------|-------|",
            ])
            for category, count in sorted(findings_by_category.items(), key=lambda x: -x[1]):
                lines.append(f"| {category.replace('_', ' ').title()} | {count} |")
            lines.append("")
        else:
            lines.extend([
                "No findings data available.",
                "",
            ])

        # Health history
        if history:
            lines.extend([
                "---",
                "",
                "## Health History",
                "",
                "| Date | Score | Findings |",
                "|------|-------|----------|",
            ])
            for h in history[-10:]:  # Last 10 entries
                date = h.get('timestamp', 'N/A')[:10]
                score = h.get('score', 0)
                findings = h.get('findings_count', 0)
                lines.append(f"| {date} | {score:.1f} | {findings} |")
            lines.append("")

        # Review Care section
        if review_care and review_care.get('active_managed_prs', 0) > 0:
            lines.extend([
                "---",
                "",
                "## Review Care Status",
                "",
                f"- **Active Managed PRs:** {review_care.get('active_managed_prs', 0)}",
                f"- **Blocked by Review:** {review_care.get('review_blocked_prs', 0)}",
                f"- **Retry Eligible:** {review_care.get('retry_eligible_prs', 0)}",
                f"- **Retry Planned:** {review_care.get('retry_planned_prs', 0)}",
                f"- **Retry Prepared:** {review_care.get('retry_prepared_prs', 0)}",
                f"- **Retry Executed:** {review_care.get('retry_executed_prs', 0)}",
                f"- **Pending Push Approval:** {review_care.get('pending_push_prs', 0)}",
                f"- **Push Failed:** {review_care.get('failed_push_prs', 0)}",
                f"- **Retry Failed:** {review_care.get('retry_failed_prs', 0)}",
                f"- **Retry Exhausted:** {review_care.get('retry_exhausted_prs', 0)}",
                f"- **Merge Ready:** {review_care.get('merge_ready_prs', 0)}",
                f"- **Paused (Loop Guard):** {review_care.get('paused_prs', 0)}",
                "",
            ])
            if review_care.get('last_review_cycle_at'):
                lines.extend([
                    f"- **Last Review Cycle:** {review_care.get('last_review_cycle_at')[:19]}",
                    "",
                ])
            if any(
                key in review_care
                for key in (
                    'live_rollout_mode',
                    'guarded_live_review',
                    'safety_circuit_open',
                    'safety_failure_count',
                    'safety_cooldown_until',
                    'auto_rollback_active',
                    'auto_rollback_reason',
                    'auto_rollback_triggered_at',
                )
            ):
                lines.extend([
                    "### Monitored Safety",
                    "",
                    f"- **Live Rollout Mode:** {review_care.get('live_rollout_mode', 'unknown')}",
                    f"- **Guarded Live Review:** {review_care.get('guarded_live_review', False)}",
                    f"- **Safety Circuit Open:** {review_care.get('safety_circuit_open', False)}",
                    f"- **Safety Failure Count:** {review_care.get('safety_failure_count', 0)}",
                ])
                if review_care.get('safety_cooldown_until'):
                    lines.append(f"- **Safety Cooldown Until:** {review_care.get('safety_cooldown_until')[:19]}")
                lines.append(f"- **Auto Rollback Active:** {review_care.get('auto_rollback_active', False)}")
                if review_care.get('auto_rollback_reason'):
                    lines.append(f"- **Auto Rollback Reason:** {review_care.get('auto_rollback_reason')}")
                if review_care.get('auto_rollback_triggered_at'):
                    lines.append(f"- **Auto Rollback Triggered At:** {review_care.get('auto_rollback_triggered_at')[:19]}")
                if review_care.get('operator_action_required'):
                    lines.append(f"- **Operator Action Required:** {review_care.get('operator_action_required')}")
                if review_care.get('operator_action_summary'):
                    lines.append(f"- **Operator Action Summary:** {review_care.get('operator_action_summary')}")
                patch = review_care.get('suggested_review_care_patch')
                if isinstance(patch, dict) and patch:
                    lines.append("- **Suggested review_care patch:**")
                    for key, value in patch.items():
                        lines.append(f"  - `{key}: {value}`")
                lines.append("")
            if review_care.get('pending_push_prs_detail'):
                lines.extend([
                    "### Pending Push Approval",
                    "",
                    "| PR | Branch | Files | Target Branch |",
                    "|----|--------|-------|---------------|",
                ])
                for pr in review_care.get('pending_push_prs_detail', []):
                    lines.append(f"| #{pr.get('pr_number', '?')} | {pr.get('branch', '?')} | {len(pr.get('changed_files', []))} | {pr.get('push_target_branch') or pr.get('branch', '?')} |")
                lines.append("")
            if review_care.get('failed_push_prs_detail'):
                lines.extend([
                    "### Push Failures",
                    "",
                    "| PR | Branch | Push Status |",
                    "|----|--------|-------------|",
                ])
                for pr in review_care.get('failed_push_prs_detail', []):
                    lines.append(f"| #{pr.get('pr_number', '?')} | {pr.get('branch', '?')} | {pr.get('push_status') or pr.get('status', '?')} |")
                lines.append("")
            if review_care.get('exhausted_prs_detail'):
                lines.extend([
                    "### Exhausted PRs",
                    "",
                    "| PR | Branch | Attempts |",
                    "|----|--------|----------|",
                ])
                for pr in review_care.get('exhausted_prs_detail', []):
                    lines.append(f"| #{pr.get('pr_number', '?')} | {pr.get('branch', '?')} | {pr.get('attempts', 0)} |")
                lines.append("")

        # Metrics
        lines.extend([
            "---",
            "",
            "## Metrics",
            "",
            f"- **Total Findings:** {repo.current_findings_count}",
            f"- **Total Fixes Applied:** {repo.total_fixes}",
            f"- **Total PRs Created:** {repo.total_prs}",
            f"- **Total Merges:** {repo.total_merges}",
            "",
        ])

        if repo.onboarded_at:
            lines.extend([
                f"- **Onboarded:** {repo.onboarded_at[:10]}",
            ])

        if repo.last_run_at:
            lines.extend([
                f"- **Last Run:** {repo.last_run_at[:10]}",
            ])

        lines.extend([
            "",
            "---",
            "",
            f"*Report generated by QA Agent v2.0.0*",
        ])

        return "\n".join(lines)
    
    def generate_pdf(self,
                     repo: Repo,
                     baseline: Optional[Baseline],
                     health: Optional[HealthScore],
                     history: List[Dict],
                     findings_by_category: Dict[str, int],
                     output_path: Optional[Path] = None,
                     review_care: Optional[Dict] = None) -> Path:
        """Generate a PDF report for a repository."""

        # Generate markdown
        markdown = self.generate_markdown_report(repo, baseline, health, history, findings_by_category, review_care)
        
        # Determine output path
        if output_path is None:
            output_path = self.workspace / 'reports' / f"{repo.config.name}-report.pdf"
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Check if pdf-report skill exists
        if not self.generate_script.exists():
            print(f"Warning: PDF report skill not found at {self.generate_script}")
            print("Saving markdown report instead...")
            md_path = output_path.with_suffix('.md')
            md_path.write_text(markdown)
            return md_path
        
        # Check for virtual environment
        venv_python = self.pdf_report_skill / '.venv' / 'bin' / 'python3'
        if venv_python.exists():
            python_cmd = str(venv_python)
        else:
            python_cmd = 'python3'
        
        # Use pdf-report skill to generate PDF
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(markdown)
            temp_md = f.name
        
        try:
            result = subprocess.run(
                [python_cmd, str(self.generate_script), temp_md, str(output_path), 
                 '--title', f'QA Report: {repo.config.name}'],
                capture_output=True,
                text=True,
                cwd=str(self.pdf_report_skill)
            )
            
            if result.returncode != 0:
                print(f"PDF generation failed: {result.stderr}")
                # Fallback to markdown
                md_path = output_path.with_suffix('.md')
                md_path.write_text(markdown)
                return md_path
            
            return output_path
            
        finally:
            Path(temp_md).unlink(missing_ok=True)
