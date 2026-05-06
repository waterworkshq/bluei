#!/usr/bin/env python3
"""Daily summary generator for QA Agent.

Reads current qa-agent state/run artifacts and produces a daily summary
markdown report.

Usage:
    python3 daily_summary.py --repo ky
    python3 daily_summary.py --all
    python3 daily_summary.py --repo ky --format markdown
    python3 daily_summary.py --repo ky --format pdf
"""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

QA_AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOS_DIR = QA_AGENT_ROOT / "repos"
OBSIDIAN_ROOT = Path.home() / "Obsidian" / "Logs"


def _health_icon(score: float) -> str:
    if score >= 90:
        return "✅"
    if score >= 70:
        return "🟡"
    return "🔴"


def to_ist(ts_str: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        ist_dt = dt.astimezone(ZoneInfo("Asia/Kolkata"))
        return ist_dt.strftime("%Y-%m-%d %H:%M IST")
    except Exception:
        return ts_str


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def load_runs(repo_name: str, limit: int = 48) -> List[Dict]:
    runs_dir = REPOS_DIR / repo_name / "runs"
    if not runs_dir.exists():
        return []
    runs = []
    for f in sorted(runs_dir.glob("run-*.json"), reverse=True)[:limit]:
        try:
            runs.append(json.loads(f.read_text()))
        except Exception:
            pass
    return runs


def get_today_runs(runs: List[Dict], date_str: str) -> List[Dict]:
    return [r for r in runs if r.get("started_at", "").startswith(date_str)]


def load_latest_health(repo_name: str) -> Optional[Dict]:
    health_file = REPOS_DIR / repo_name / "state" / "health_history.jsonl"
    if not health_file.exists():
        return None
    try:
        lines = health_file.read_text().strip().split("\n")
        for line in reversed(lines):
            if line.strip():
                return json.loads(line)
    except Exception:
        pass
    return None


def build_summary_markdown(repo_name: str, date_str: str) -> str:
    runs = load_runs(repo_name)
    today_runs = get_today_runs(runs, date_str)

    status = read_json(REPOS_DIR / repo_name / "state" / "status.json")
    issues_data = read_json(REPOS_DIR / repo_name / "state" / "issues.json")
    active_prs = read_json(REPOS_DIR / repo_name / "state" / "active_prs.json")

    # Derive counts from source-of-truth files, not stale status.json counters
    issues = issues_data.get("issues", [])
    open_issue_list = [i for i in issues if i.get("status") == "open"]
    closed_issues = [i for i in issues if i.get("status") in ("closed", "completed")]

    prs = active_prs.get("prs", {})

    open_issues = len(open_issue_list)
    open_prs = len(prs)
    findings_entries = len(issues)  # all tracked findings entries

    # Warn if status.json current_counts diverges from source-of-truth computation
    stale_counts = status.get("current_counts", {})
    stale_open_issues = stale_counts.get("open_issues")
    stale_open_prs = stale_counts.get("open_prs")
    stale_findings = stale_counts.get("findings_entries")
    if stale_open_issues is not None and stale_open_issues != open_issues:
        print(f"[WARN] {repo_name}: status.json open_issues={stale_open_issues} "
              f"differs from source-of-truth issues.json count={open_issues}", file=sys.stderr)
    if stale_open_prs is not None and stale_open_prs != open_prs:
        print(f"[WARN] {repo_name}: status.json open_prs={stale_open_prs} "
              f"differs from source-of-truth active_prs.json count={open_prs}", file=sys.stderr)
    if stale_findings is not None and abs(stale_findings - findings_entries) > 1:
        print(f"[WARN] {repo_name}: status.json findings_entries={stale_findings} "
              f"differs from source-of-truth issues.json count={findings_entries}", file=sys.stderr)

    health = load_latest_health(repo_name)
    score = health.get("score", 0) if health else 0

    # Phase breakdown
    phase_counts = {}
    for r in today_runs:
        p = r.get("phase", "unknown")
        phase_counts[p] = phase_counts.get(p, 0) + 1

    # Only count newly created PRs from issue-cycle and pr-cycle.
    # review-cycle's prs_created field reflects PRs under review management,
    # not newly opened PRs — summing it inflates the new-PR count.
    creation_phases = {"issue-cycle", "pr-cycle"}
    total_prs_created = sum(
        r.get("prs_created", 0) for r in today_runs if r.get("phase") in creation_phases
    )
    total_merges = sum(r.get("merges_completed", 0) for r in today_runs)
    total_findings = sum(r.get("findings_detected", 0) for r in today_runs)
    total_issues_created = sum(r.get("issues_created", 0) for r in today_runs)

    lines = [
        f"# QA Daily Summary — {date_str}",
        f"**Repo:** {repo_name}",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M IST')}",
        "",
        "---",
        "",
        "## Health & Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Health Score | {_health_icon(score)} {score:.1f}/100 |",
        f"| Open Issues | {open_issues} |",
        f"| Open PRs | {open_prs} |",
        f"| Findings Tracked | {findings_entries} |",
        "",
        "## Today's Activity",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Total Runs | {len(today_runs)} |",
        f"| Issues Created | {total_issues_created} |",
        f"| PRs Created | {total_prs_created} |",
        f"| Merges | {total_merges} |",
        f"| Findings Detected | {total_findings} |",
        "",
    ]

    if phase_counts:
        lines.extend(["### Cycles Executed", ""])
        for phase, count in sorted(phase_counts.items()):
            lines.append(f"- **{phase}:** {count} runs")
        lines.append("")

    # Failures
    failures = [r for r in today_runs if r.get("status") not in ("completed", "ok")]
    if failures:
        lines.extend(["### Failures", ""])
        for f in failures[:5]:
            started = to_ist(f.get("started_at", "?"))
            phase = f.get("phase", "?")
            error = f.get("error", "unknown")
            lines.append(f"- **{phase}** @ {started}: {error[:80]}")
        lines.append("")

    # Open issues
    if open_issue_list:
        lines.extend(["## Open Issues", ""])
        lines.extend(["| # | Title | Rule |", "|----|-------|-----|"])
        for i in open_issue_list[:10]:
            num = i.get("github", {}).get("issue_number") or i.get("issue_id", "?")
            title = (i.get("title") or i.get("snippet") or "")[:50]
            rule = i.get("rule", "?")
            lines.append(f"| {num} | {title} | {rule} |")
        lines.append("")

    # Active PRs
    if prs:
        lines.extend(["## Active PRs", ""])
        lines.extend(["| # | Branch | Status |", "|----|--------|--------|"])
        for pr_num, pr_data in sorted(prs.items(), key=lambda x: int(x[0]), reverse=True)[:10]:
            branch = (pr_data.get("branch") or "?")[:30]
            status = pr_data.get("status", "?")
            lines.append(f"| {pr_num} | {branch} | {status} |")
        lines.append("")

    # Most recent runs
    if today_runs:
        lines.extend(["## Recent Runs", ""])
        lines.extend(["| Time | Phase | Status | PRs | Merges |", "|------|-------|--------|-----|--------|"])
        for r in today_runs[:8]:
            started = to_ist(r.get("started_at", "?"))
            phase = r.get("phase", "?")[:15]
            status = r.get("status", "?")
            prs_c = r.get("prs_created", 0)
            merges = r.get("merges_completed", 0)
            lines.append(f"| {started} | {phase} | {status} | {prs_c} | {merges} |")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            f"*Generated by QA Agent v2.0.0 — source: qa-agent/repos/{repo_name}/state/*",
        ]
    )

    return "\n".join(lines)


def build_summary_pdf(repo_name: str, date_str: str, output_path: Path) -> bool:
    """Generate PDF from markdown summary using pdf-report skill."""
    import shutil
    pdf_skill = QA_AGENT_ROOT.parent / "skills" / "pdf-report"
    generate_script = pdf_skill / "scripts" / "generate_pdf.py"

    if not generate_script.exists():
        print(f"PDF skill not found at {generate_script}", file=sys.stderr)
        return False

    venv_python = pdf_skill / ".venv" / "bin" / "python3"
    python_cmd = str(venv_python) if venv_python.exists() else "python3"

    markdown = build_summary_markdown(repo_name, date_str)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(markdown)
        temp_md = Path(f.name)

    try:
        result = subprocess.run(
            [python_cmd, str(generate_script), str(temp_md), str(output_path),
             "--title", f"QA Daily Summary — {repo_name} — {date_str}"],
            capture_output=True,
            text=True,
            cwd=str(pdf_skill),
        )
        if result.returncode != 0:
            print(f"PDF generation failed: {result.stderr}", file=sys.stderr)
            return False
        return True
    finally:
        temp_md.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate QA Agent daily summary")
    parser.add_argument("--repo", help="Repository name (e.g. ky, zulip)")
    parser.add_argument("--all", action="store_true", help="Generate for all known repos")
    parser.add_argument("--date", help="Date string YYYY-MM-DD (default: today)")
    parser.add_argument("--format", choices=["markdown", "pdf", "both"], default="markdown")
    parser.add_argument("--output-dir", type=Path, help="Output directory (default: ~/Obsidian/Logs/qa-daily/)")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    repos = []
    if args.all:
        if REPOS_DIR.exists():
            repos = [d.name for d in REPOS_DIR.iterdir() if d.is_dir()]
    elif args.repo:
        repos = [args.repo]
    else:
        parser.print_help()
        return

    output_dir = args.output_dir or (OBSIDIAN_ROOT.parent / "Logs" / "qa-daily")
    output_dir.mkdir(parents=True, exist_ok=True)

    for repo in repos:
        md_content = build_summary_markdown(repo, date_str)

        if args.format in ("markdown", "both"):
            md_path = output_dir / f"{repo}-{date_str}.md"
            md_path.write_text(md_content)
            print(f"Markdown: {md_path}")

        if args.format in ("pdf", "both"):
            pdf_path = output_dir / f"{repo}-{date_str}.pdf"
            if build_summary_pdf(repo, date_str, pdf_path):
                print(f"PDF: {pdf_path}")
            else:
                # Fallback to markdown
                md_path = output_dir / f"{repo}-{date_str}.md"
                md_path.write_text(md_content)
                print(f"PDF failed, markdown saved: {md_path}")


if __name__ == "__main__":
    import sys
    main()
