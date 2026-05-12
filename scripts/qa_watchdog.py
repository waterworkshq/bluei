#!/usr/bin/env python3
"""Blenny watchdog — inspects latest run state and logs for all registered repos."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
RUN_MARKER = "🏃 Running "


def load_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _discover_repos():
    """Read enabled repos from registry.yaml."""
    registry_path = ROOT / "registry.yaml"
    if not registry_path.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(registry_path.read_text())
        return [
            r["name"] for r in data.get("repos", [])
            if r.get("enabled", True)
        ]
    except Exception:
        return []


REPOS = _discover_repos()


def latest_run(repo):
    runs_dir = ROOT / "repos" / repo / "runs"
    if not runs_dir.exists():
        return None
    runs = sorted(runs_dir.glob("run-*.json"), reverse=True)
    if not runs:
        return None
    for r in runs:
        try:
            return json.loads(r.read_text())
        except Exception:
            pass
    return None


def latest_log_block(lines):
    """Return only the latest run block from a qa-agent repo log.

    The repo logs are append-only and can contain old tracebacks that no longer
    reflect the current state. We only want diagnostics from the most recent
    run block, which begins at the last `🏃 Running ...` marker.
    """
    if not lines:
        return []
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if RUN_MARKER in lines[i]:
            start = i
            break
    return lines[start:]


def recent_error_lines(lines):
    block = latest_log_block(lines)
    return [
        l for l in block
        if "error" in l.lower() or "failed" in l.lower() or "exception" in l.lower()
    ]


def check():
    alerts = []

    if not REPOS:
        return "No repos configured. Run 'blenny init' or 'blenny onboard --repo <path>' to add one."

    for repo in REPOS:
        rd = ROOT / "repos" / repo
        if not rd.exists():
            alerts.append(f"--- {repo} ---")
            alerts.append(f"  ⚠ State directory not found at {rd}")
            alerts.append(f"  Repo is registered but never initialized. Run 'blenny scan {repo}' to create state.")
            continue

        status = load_json(rd / "state" / "status.json")
        issues_data = load_json(rd / "state" / "issues.json")
        prs_data = load_json(rd / "state" / "active_prs.json")
        latest = latest_run(repo)

        counts = status.get("current_counts", {})
        open_issues = [i for i in issues_data.get("issues", []) if i.get("status") == "open"]
        prs = prs_data.get("prs", {})

        # Check latest run
        run_status = "unknown"
        run_phase = "?"
        if latest:
            run_status = latest.get("status", "?")
            run_phase = latest.get("phase", "?")
            started = latest.get("started_at", "?")
            run_error = latest.get("error")
        else:
            started = "never"

        alerts.append(f"--- {repo} ---")
        alerts.append(f"  health:     {counts.get('health_score', '?')}")
        alerts.append(f"  findings:   {counts.get('findings_entries', 0)}")
        alerts.append(f"  open issues: {len(open_issues)}")
        alerts.append(f"  open PRs:   {len(prs)}")
        alerts.append(f"  last run:   {started[:19] if started != '?' else started} | {run_phase} | {run_status}")
        if latest and latest.get("error"):
            alerts.append(f"  ERROR:      {latest['error'][:120]}")

        # Check log for errors from the latest run block only.
        log_path = ROOT / "logs" / f"qa-agent-{repo}.log"
        if log_path.exists():
            lines = log_path.read_text().splitlines()
            error_lines = recent_error_lines(lines)
            if error_lines[-3:]:
                alerts.append(f"  log errors (latest run, last 3):")
                for l in error_lines[-3:]:
                    alerts.append(f"    {l[:120]}")

    # Cross-repo: check review cycle telemetry for summary
    review_stats = ROOT / 'state' / 'review_stats.jsonl'
    if review_stats.exists():
        try:
            lines = review_stats.read_text().strip().splitlines()
            if lines:
                latest = json.loads(lines[-1])
                alerts.append('--- review cycle ---')
                alerts.append(f'  active: {latest.get("active_prs",0)} | blocked: {latest.get("blocked_prs",0)} | merge-ready: {latest.get("merge_ready",0)}')
                if latest.get("retry_failed",0) > 0 or latest.get("retry_exhausted",0) > 0:
                    alerts.append(f'  ⚠ retry failures: {latest.get("retry_failed",0)} exhausted: {latest.get("retry_exhausted",0)}')
                if latest.get("findings_failed",0) > 0:
                    alerts.append(f'  ⚠ findings failed: {latest.get("findings_failed",0)}')
        except (json.JSONDecodeError, OSError):
            pass

    # Cross-repo: check escalation log for active patterns
    escalation_file = ROOT / 'state' / 'escalation_log.jsonl'
    if escalation_file.exists():
        try:
            unread = []
            for line in escalation_file.read_text().strip().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                for f in rec.get('findings', []):
                    unread.append(f'  ⚠ {f["type"]}: {f["detail"]}')
            if unread:
                alerts.append('--- escalations ---')
                alerts.extend(unread[-5:])  # last 5 only
        except (json.JSONDecodeError, OSError):
            pass

    return "\n".join(alerts)


def smoke_test() -> str:
    """Quick sanity — validates module import and repo state dirs."""
    lines: list[str] = []
    all_passed = True

    if not REPOS:
        return "No repos configured — nothing to smoke test."

    # Validate the core module loads
    try:
        import importlib.util
        core_mod = "sandbox_local_runner"
        spec = importlib.util.find_spec(core_mod)
        if spec is None:
            # Try with PYTHONPATH
            sys.path.insert(0, str(ROOT / "core"))
            spec = importlib.util.find_spec(core_mod)
        if spec:
            lines.append(f"  ✅ module '{core_mod}' importable")
        else:
            lines.append(f"  ❌ module '{core_mod}' NOT FOUND")
            all_passed = False
    except Exception as exc:
        lines.append(f"  ❌ module import error: {exc}")
        all_passed = False

    # Check each repo state dir
    for repo in REPOS:
        rd = ROOT / "repos" / repo
        if not rd.exists():
            lines.append(f"  ⚠ {repo} state dir missing — no runs yet")
            continue
        # Quick check: can we list run files?
        runs_dir = rd / "runs"
        if runs_dir.exists():
            run_files = list(runs_dir.glob("run-*.json"))
            run_count = len(run_files)
        else:
            run_count = 0
        lines.append(f"  ✅ {repo}: {run_count} run files, state dir present")

    prefix = "✅ HEALTH OK" if all_passed else "❌ HEALTH FAILURE"
    return f"{prefix}\n" + "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--smoke-test" in args:
        print(smoke_test())
    else:
        print(check())
