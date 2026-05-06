#!/usr/bin/env python3
"""Obsidian log sync for QA Agent.

Reads current qa-agent state/run artifacts and writes markdown logs to
~/Obsidian/Logs/{issue-cycle,pr-cycle,merge-cycle,qa-monitor}/YYYY-MM-DD.md
in the established Obsidian format.

Each phase directory gets a date-stamped file whose content has a repo
header (e.g. "## ky") so multiple repos' data can coexist in the same file.
If a file already exists, each repo gets its own section (## <repo>) that
completely replaces any prior section for that repo.

Usage:
    python3 obsidian_sync.py --repo ky --phase issue-cycle
    python3 obsidian_sync.py --repo ky --phase review-cycle
    python3 obsidian_sync.py --repo zulip --phase qa-monitor
    python3 obsidian_sync.py --all                # sync all repos, all phases
    python3 obsidian_sync.py --repo ky --dry-run  # preview to stdout
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

QA_AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOS_DIR = QA_AGENT_ROOT / "repos"
OBSIDIAN_ROOT = Path.home() / "Obsidian" / "Logs"

CYCLE_SUBDIRS = {
    "issue-cycle": "issue-cycle",
    "pr-cycle": "pr-cycle",
    "merge-cycle": "merge-cycle",
    "review-cycle": "review-cycle",
    "qa-monitor": "qa-monitor",
}

REPO_HEADERS = {
    "issue-cycle": "## {repo} (vikasagarwal101/{repo})",
    "pr-cycle": "## {repo} (vikasagarwal101/{repo})",
    "merge-cycle": "## {repo} (vikasagarwal101/{repo})",
    "review-cycle": "## {repo} (vikasagarwal101/{repo})",
    "qa-monitor": "## {repo} (vikasagarwal101/{repo})",
}


def to_ist(ts_str: str) -> str:
    """Format ISO timestamp as Asia/Kolkata local time."""
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


def _health_icon(score: float) -> str:
    if score >= 90:
        return "✅"
    if score >= 70:
        return "🟡"
    return "🔴"


def _find_first_repo_section(content: str) -> Optional[int]:
    """Find the byte offset of the first ## <repo> (owner/repo) section."""
    m = re.search(r'^## [\w-]+ \(\S+/\S+\)', content, re.MULTILINE)
    return m.start() if m else None


# Matches a full section: heading line + all following content lines (that don't start with ##)
_OLD_SECTION_RE = re.compile(
    r'^## [^\n]+\n(?:(?:(?!^## )[^\n]*\n)*)',
    re.MULTILINE,
)


def _replace_repo_section(content: str, repo_name: str, new_section: str) -> str:
    """Replace a repo section in existing file content.

    Handles three cases:
    1. Old format (no ## <repo> headers): replaces the entire first ## section
    2. Mixed old+new: strips old content (before first ## <repo>), then replaces/inserts
    3. New format with existing repo section: replaces that section in place

    If no matching section found, appends at end.
    """
    # Check if this file has any ## <repo> (...) sections
    first_repo_pos = _find_first_repo_section(content)

    if first_repo_pos is None:
        # Pure old format — consume the full first ## section
        m = _OLD_SECTION_RE.search(content)
        if m:
            return _OLD_SECTION_RE.sub(new_section, content, count=1)
        # No ## sections at all — replace everything
        return new_section

    # Mixed old+new format: old content exists before the first ## <repo> section.
    # Only strip old content if the first repo section is NOT a new-format section
    # (i.e., it's old stale data we want to replace wholesale).
    if first_repo_pos > 0:
        # Check if the first section is new-format (has owner/repo in parens)
        first_section_start = content[first_repo_pos:]
        has_new_format = bool(
            re.match(r'## [\w-]+ \(\S+/\S+\)', first_section_start)
        )
        if has_new_format:
            # New-format sections present; don't strip. Just try to replace/add
            # repo section via the patterns below (don't strip old content).
            pass
        else:
            # Old-format first section — strip it
            content = content[first_repo_pos:]

    # Now content starts with ## <repo>. Try to replace that section.
    # Use [\w-]+ for repo/owner parts to handle hyphenated names.
    patterns = [
        re.compile(
            r'(## ' + re.escape(repo_name) + r' \([\w-]+/[\w-]+\).+?)(?=\n## |\Z)',
            re.DOTALL,
        ),
        re.compile(
            r'(## ' + re.escape(repo_name) + r'(?:$|\s(?!\()[^\n]*\n.*?)(?=\n## |\Z))',
            re.DOTALL,
        ),
    ]
    for pattern in patterns:
        if pattern.search(content):
            return pattern.sub(new_section, content)
    # Not found — append
    return content.rstrip() + "\n\n" + new_section + "\n"


# ─── Phase builders ───────────────────────────────────────────────────────────

def _load_findings_count(repo_name: str) -> int:
    """Derive findings count directly from findings.jsonl (source of truth)."""
    findings_file = REPOS_DIR / repo_name / "state" / "findings.jsonl"
    if not findings_file.exists():
        return 0
    try:
        return sum(1 for line in findings_file.read_text().splitlines() if line.strip())
    except Exception:
        return 0


def _build_issue_cycle(repo_name: str, date_str: str) -> str:
    status_data = read_json(REPOS_DIR / repo_name / "state" / "status.json")
    issues_data = read_json(REPOS_DIR / repo_name / "state" / "issues.json")
    runs = load_runs(repo_name, limit=10)

    today_runs = [r for r in runs if r.get("started_at", "").startswith(date_str)]
    issue_runs = [r for r in today_runs if r.get("phase") == "issue-cycle"]

    issues = issues_data.get("issues", [])
    open_issues = [i for i in issues if i.get("status") == "open"]
    closed_today = [i for i in issues if i.get("status") in ("closed", "completed") and i.get("updated_at", "").startswith(date_str)]
    created_today = [i for i in issues if i.get("created_at", "").startswith(date_str)]

    # Derive counts from source-of-truth files, not stale status.json current_counts
    findings_count = _load_findings_count(repo_name)
    open_issues_count = len(open_issues)

    # Warn if status.json current_counts diverges from source-of-truth computation
    stale_counts = status_data.get("current_counts", {})
    stale_findings = stale_counts.get("findings_entries")
    stale_open_issues = stale_counts.get("open_issues")
    if stale_findings is not None and abs(stale_findings - findings_count) > 1:
        print(f"[WARN] {repo_name}: status.json findings_entries={stale_findings} "
              f"differs from source-of-truth findings.jsonl count={findings_count}", file=sys.stderr)
    if stale_open_issues is not None and stale_open_issues != open_issues_count:
        print(f"[WARN] {repo_name}: status.json open_issues={stale_open_issues} "
              f"differs from source-of-truth issues.json count={open_issues_count}", file=sys.stderr)

    latest_run = runs[0] if runs else None

    lines = [
        f"## {repo_name} (vikasagarwal101/{repo_name})",
        "",
        "### Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Issues Created | {len(created_today)} |",
        f"| Issues Closed | {len(closed_today)} |",
        f"| Open Issues | {open_issues_count} |",
        f"| Findings Tracked | {findings_count} |",
    ]

    if latest_run:
        hb = latest_run.get("health_before", 0)
        ha = latest_run.get("health_after", 0)
        hd = ha - hb
        if hb:
            lines.append(f"| Health Score | {_health_icon(ha)} {ha:.1f} (Δ{hd:+.1f}) |")

    lines.extend(["", "### Current Open Issues", ""])

    if open_issues:
        lines.extend(["| Issue | Rule | Status |", "|-------|------|--------|"])
        for i in open_issues[-20:]:
            num = i.get("github", {}).get("issue_number") or i.get("issue_id") or i.get("id", "?")
            snippet = (i.get("snippet") or "")[:55]
            rule = i.get("rule", "?")
            status = i.get("status", "open")
            lines.append(f"| #{num} | {snippet} | {rule} | {status} |")
    else:
        lines.append("No open issues.")

    lines.extend(["", "### Activity Today", ""])
    if issue_runs:
        for run in issue_runs[:5]:
            started = to_ist(run.get("started_at", ""))
            status_r = run.get("status", "?")
            detected = run.get("findings_detected", 0)
            created = run.get("issues_created", 0)
            errors = run.get("error")
            lines.extend([
                f"**{run.get('phase', '?')}** @ {started}: {status_r} | Detected: {detected} | Created: {created}",
            ])
            if errors:
                lines.append(f"  - Error: `{errors[:100]}`")
    else:
        lines.append("No issue-cycle runs today.")

    lines.extend(["", f"*Source: qa-agent host-side state — {date_str}*"])
    return "\n".join(lines)


def _build_pr_cycle(repo_name: str, date_str: str) -> str:
    runs = load_runs(repo_name, limit=20)
    issues_data = read_json(REPOS_DIR / repo_name / "state" / "issues.json")
    active_prs_data = read_json(REPOS_DIR / repo_name / "state" / "active_prs.json")

    today_runs = [r for r in runs if r.get("started_at", "").startswith(date_str)]
    pr_runs = [r for r in today_runs if r.get("phase") == "pr-cycle"]

    prs_created = sum(r.get("prs_created", 0) for r in pr_runs)
    fixes_verified = sum(r.get("fixes_verified", 0) for r in pr_runs)
    fixes_failed = sum(r.get("fixes_failed", 0) for r in pr_runs)
    issues = issues_data.get("issues", [])
    open_issues = [i for i in issues if i.get("status") == "open"]
    prs = active_prs_data.get("prs", {})

    lines = [
        f"## {repo_name} (vikasagarwal101/{repo_name})",
        "",
        "### Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| PRs Created | {prs_created} |",
        f"| Fixes Verified | {fixes_verified} |",
        f"| Fixes Failed | {fixes_failed} |",
        f"| Open Issues | {len(open_issues)} |",
        "",
    ]

    if prs:
        lines.extend(["### Active PRs", "", "| # | Branch | Author | Status |", "|----|--------|--------|--------|"])
        for pr_num, pr_data in sorted(prs.items(), key=lambda x: int(x[0]), reverse=True)[:20]:
            branch = (pr_data.get("branch") or "?")[:30]
            author = pr_data.get("author", "?")
            status = pr_data.get("status", "?")
            lines.append(f"| {pr_num} | {branch} | {author} | {status} |")
        lines.append("")
    else:
        lines.extend(["### Active PRs", "", "No active PRs.", ""])

    if pr_runs:
        lines.extend(["### Cycles Executed", ""])
        for run in pr_runs[:5]:
            started = to_ist(run.get("started_at", ""))
            phase = run.get("phase", "?")
            errors = run.get("error")
            lines.extend([
                f"**{phase}** @ {started}: **{run.get('status', '?')}** | "
                f"PRs: {run.get('prs_created', 0)} | "
                f"Verified: {run.get('fixes_verified', 0)}",
            ])
            if errors:
                lines.append(f"  - Error: `{errors[:100]}`")
        lines.append("")

    if open_issues[:5]:
        lines.extend(["### Open Issues", ""])
        for i in open_issues[:5]:
            num = i.get("github", {}).get("issue_number") or i.get("issue_id") or i.get("id", "?")
            snippet = (i.get("snippet") or "")[:55]
            rule = i.get("rule", "?")
            lines.append(f"- #{num}: {snippet} ({rule})")

    lines.extend(["", f"*Source: qa-agent host-side state — {date_str}*"])
    return "\n".join(lines)


def _build_merge_cycle(repo_name: str, date_str: str) -> str:
    runs = load_runs(repo_name, limit=20)
    today_runs = [r for r in runs if r.get("started_at", "").startswith(date_str)]
    merge_runs = [r for r in today_runs if r.get("phase") == "merge-cycle"]
    active_prs_data = read_json(REPOS_DIR / repo_name / "state" / "active_prs.json")
    status_data = read_json(REPOS_DIR / repo_name / "state" / "status.json")

    merged = sum(r.get("merges_completed", 0) for r in merge_runs)
    prs_created = sum(r.get("prs_created", 0) for r in merge_runs)
    prs = active_prs_data.get("prs", {})
    latest_run_metrics = status_data.get("latest_run_metrics", {}) if isinstance(status_data, dict) else {}
    blocked_reasons = latest_run_metrics.get("blocked_reasons", []) if isinstance(latest_run_metrics, dict) else []

    lines = [
        f"## {repo_name} (vikasagarwal101/{repo_name})",
        "",
        "### Summary",
        "",
        "| Status | Count |",
        "|--------|-------|",
        f"| Merged | {merged} |",
        f"| PRs Opened | {prs_created} |",
        f"| Active Tracked | {len(prs)} |",
        "",
    ]

    if merge_runs:
        lines.append("### Cycles Executed")
        for run in merge_runs[:3]:
            started = to_ist(run.get("started_at", ""))
            lines.extend([
                f"**{run.get('phase', '?')}** @ {started}: **{run.get('status', '?')}** | "
                f"Merges: {run.get('merges_completed', 0)} | "
                f"PRs: {run.get('prs_created', 0)} | "
                f"Health Δ: {run.get('health_delta', 0):+.1f}",
            ])
        lines.append("")

    if blocked_reasons:
        lines.extend(["### Current Merge Blockers", ""])
        for reason in blocked_reasons[:10]:
            lines.append(f"- `{reason}`")
        lines.append("")

    if prs:
        lines.extend([
            "### Active PRs",
            "",
            "| # | Branch | Author | Status | Merge Readiness | Reason |",
            "|----|--------|--------|--------|-----------------|--------|",
        ])
        for pr_num, pr_data in sorted(prs.items(), key=lambda x: int(x[0]), reverse=True)[:10]:
            branch = (pr_data.get("branch") or "?")[:30]
            author = pr_data.get("author", "?")
            status = pr_data.get("status", "?")
            merge_readiness = pr_data.get("merge_readiness") or {}
            merge_state = (merge_readiness.get("state") or "?")[:17]
            merge_reason = (merge_readiness.get("reason") or "").replace("|", "/")[:48]
            lines.append(f"| {pr_num} | {branch} | {author} | {status} | {merge_state} | {merge_reason} |")
        lines.append("")

    lines.extend(["", f"*Source: qa-agent host-side state — {date_str}*"])
    return "\n".join(lines)


def _build_review_cycle(repo_name: str, date_str: str) -> str:
    runs = load_runs(repo_name, limit=20)
    today_runs = [r for r in runs if r.get("started_at", "").startswith(date_str)]
    review_runs = [r for r in today_runs if r.get("phase") == "review-cycle"]
    active_prs_data = read_json(REPOS_DIR / repo_name / "state" / "active_prs.json")
    prs = active_prs_data.get("prs", {})
    review_care_data = active_prs_data.get("review_care", {})
    managed_prs = review_care_data.get("managed_prs", {})
    active = sum(1 for pr in managed_prs.values() if pr.get("status") in ("awaiting_review", "in_progress"))
    pending_push = sum(1 for pr in managed_prs.values() if pr.get("status") == "pending_push_approval")
    ready = sum(1 for pr in managed_prs.values() if pr.get("status") == "merge_ready")
    failed = sum(1 for pr in managed_prs.values() if pr.get("status") in ("failed_push", "retry_failed"))

    lines = [
        f"## {repo_name} (vikasagarwal101/{repo_name})",
        "",
        "### Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Active PRs | {len(prs)} |",
        f"| Managed by review care | {len(managed_prs)} |",
        f"| Awaiting review | {active} |",
        f"| Pending push approval | {pending_push} |",
        f"| Merge ready | {ready} |",
        f"| Failed | {failed} |",
        "",
    ]

    if review_runs:
        lines.append("### Cycles Executed")
        for run in review_runs[:5]:
            started = to_ist(run.get("started_at", ""))
            lines.extend([
                f"**review-cycle** @ {started}: **{run.get('status', '?')}** | "
                f"Findings: {run.get('findings_detected', 0)} | "
                f"Fixes verified: {run.get('fixes_verified', 0)}",
            ])
            if run.get("error"):
                lines.append(f"  Error: `{run.get('error')[:100]}`")
        lines.append("")

    if prs:
        lines.extend([
            "### Managed PRs",
            "",
            "| # | Branch | Status | Merge Readiness | Review Notes |",
            "|----|--------|--------|-----------------|--------------|",
        ])
        for pr_num, pr_data in sorted(prs.items(), key=lambda x: int(x[0]), reverse=True)[:15]:
            branch = (pr_data.get("branch") or "?")[:28]
            status = pr_data.get("status", "?")
            merge_readiness = pr_data.get("merge_readiness") or {}
            merge_state = (merge_readiness.get("state") or "?")[:17]
            review_notes = (merge_readiness.get("reason") or "").replace("|", "/")[:60]
            lines.append(f"| {pr_num} | {branch} | {status} | {merge_state} | {review_notes} |")
        lines.append("")
    else:
        lines.extend(["### Managed PRs", "", "No PRs under review care.", ""])

    lines.extend(["", f"*Source: qa-agent host-side state — {date_str}*"])
    return "\n".join(lines)


def _build_qa_monitor(repo_name: str, date_str: str) -> str:
    issues_data = read_json(REPOS_DIR / repo_name / "state" / "issues.json")
    active_prs_data = read_json(REPOS_DIR / repo_name / "state" / "active_prs.json")
    runs = load_runs(repo_name, limit=10)
    today_runs = [r for r in runs if r.get("started_at", "").startswith(date_str)]

    issues = issues_data.get("issues", [])
    open_issues = [i for i in issues if i.get("status") == "open"]
    prs = active_prs_data.get("prs", {})
    open_issues_count = len(open_issues)
    open_prs_count = len(prs)

    # Latest health
    latest_health = None
    health_file = REPOS_DIR / repo_name / "state" / "health_history.jsonl"
    if health_file.exists():
        try:
            for line in reversed(health_file.read_text().strip().split("\n")):
                if line.strip():
                    latest_health = json.loads(line)
                    break
        except Exception:
            pass

    score = latest_health.get("score", 0) if latest_health else 0

    lines = [
        f"## {repo_name} (vikasagarwal101/{repo_name})",
        "",
        "### Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Open Issues | {open_issues_count} |",
        f"| Open PRs | {open_prs_count} |",
        f"| Health Score | {_health_icon(score)} {score:.1f} |",
        "",
    ]

    if today_runs:
        phase_counts = {}
        for r in today_runs:
            p = r.get("phase", "?")
            phase_counts[p] = phase_counts.get(p, 0) + 1
        lines.append("### Activity Today")
        for p, c in sorted(phase_counts.items()):
            lines.append(f"- **{p}:** {c} runs")
        lines.append("")
    else:
        lines.extend(["### Activity Today", "", "No runs today.", ""])

    if open_issues:
        lines.extend(["### Open Issues", ""])
        lines.extend(["| # | Snippet | Rule |", "|-----|---------|-----|"])
        for i in open_issues[:10]:
            num = i.get("github", {}).get("issue_number") or i.get("issue_id") or i.get("id", "?")
            snippet = (i.get("snippet") or "")[:50]
            rule = i.get("rule", "?")
            lines.append(f"| #{num} | {snippet} | {rule} |")
        lines.append("")
    else:
        lines.extend(["### Open Issues", "", "No open issues.", ""])

    if prs:
        lines.extend(["### Open PRs", "", "| # | Branch | Status |", "|----|--------|--------|"])
        for pr_num, pr_data in sorted(prs.items(), key=lambda x: int(x[0]), reverse=True)[:10]:
            branch = (pr_data.get("branch") or "?")[:30]
            status = pr_data.get("status", "?")
            lines.append(f"| {pr_num} | {branch} | {status} |")
        lines.append("")
    else:
        lines.extend(["### Open PRs", "", "No open PRs.", ""])

    lines.extend(["", f"*Source: qa-agent host-side state — {date_str}*"])
    return "\n".join(lines)


# ─── File-level operations ─────────────────────────────────────────────────────

def _get_file_header(phase: str, date_str: str) -> str:
    headers = {
        "issue-cycle": f"# Issue-Cycle Log - {date_str}",
        "pr-cycle": f"# PR-Cycle Log - {date_str}",
        "merge-cycle": f"# Merge-Cycle Log - {date_str}",
        "qa-monitor": f"# QA Monitor Log - {date_str}",
    }
    return headers.get(phase, f"# {phase} Log - {date_str}")


def _phase_content(phase: str, repo_name: str, date_str: str) -> str:
    builders = {
        "issue-cycle": _build_issue_cycle,
        "pr-cycle": _build_pr_cycle,
        "merge-cycle": _build_merge_cycle,
        "review-cycle": _build_review_cycle,
        "qa-monitor": _build_qa_monitor,
    }
    return builders[phase](repo_name, date_str)


def sync_phase(repo_name: str, phase: str, date_str: Optional[str] = None, dry_run: bool = False) -> bool:
    """Sync a specific phase for a repo to Obsidian.

    Writes a repo-specific section (## <repo>) into the date-stamped file,
    preserving other repos' sections already in the file.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    subdir = CYCLE_SUBDIRS.get(phase)
    if not subdir:
        print(f"Unknown phase: {phase}", file=sys.stderr)
        return False

    obsidian_dir = OBSIDIAN_ROOT / subdir
    output_path = obsidian_dir / f"{date_str}.md"

    new_section = _phase_content(phase, repo_name, date_str)

    if dry_run:
        print(f"# === Would update {output_path} with section ## {repo_name} ===")
        print(new_section)
        print(f"# === END ===\n")
        return True

    try:
        obsidian_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"Cannot create {obsidian_dir}: {e}", file=sys.stderr)
        return False

    # Read existing content or create from scratch
    if output_path.exists():
        existing = output_path.read_text()
    else:
        existing = _get_file_header(phase, date_str) + "\n"

    updated = _replace_repo_section(existing, repo_name, new_section)
    output_path.write_text(updated)
    print(f"Synced {repo_name}/{phase} -> {output_path}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync QA Agent state to Obsidian logs")
    parser.add_argument("--repo", help="Repository name (e.g. ky, zulip)")
    parser.add_argument("--all", action="store_true", help="Sync all known repos")
    parser.add_argument("--phase", choices=list(CYCLE_SUBDIRS.keys()), help="Specific phase to sync")
    parser.add_argument("--date", help="Date string YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout instead of writing files")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    repos = []
    if args.all:
        repos = ["ky", "zulip"]
    elif args.repo:
        repos = [args.repo]
    else:
        parser.print_help()
        return

    phases = [args.phase] if args.phase else list(CYCLE_SUBDIRS.keys())

    for repo in repos:
        for phase in phases:
            sync_phase(repo, phase, date_str, args.dry_run)


if __name__ == "__main__":
    main()
