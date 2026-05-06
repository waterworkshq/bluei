#!/usr/bin/env python3
"""QA Agent watchdog — inspects latest run state and logs for both repos."""
import json
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
REPOS = ["ky", "zulip"]
RUN_MARKER = "🏃 Running "


def load_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


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
    for repo in REPOS:
        rd = ROOT / "repos" / repo
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
    """Run a quick dry cycle per repo to confirm the agent starts and completes."""
    lines: list[str] = []
    all_passed = True
    for repo in REPOS:
        start = time.time()
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "sandbox_local_runner",
                    "--repo-path", str(ROOT / "repos" / repo),
                    "status",
                    "--dry-run",
                    "--findings-limit", "1",
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            elapsed = time.time() - start
            if result.returncode == 0:
                lines.append(f"  ✅ {repo} smoke-test passed ({elapsed:.1f}s)")
            else:
                lines.append(f"  ❌ {repo} smoke-test FAILED rc={result.returncode} ({elapsed:.1f}s)")
                lines.append(f"     stderr: {result.stderr.strip()[-200:]}")
                all_passed = False
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            lines.append(f"  ❌ {repo} smoke-test TIMEOUT (>30s)")
            all_passed = False
        except Exception as exc:
            lines.append(f"  ❌ {repo} smoke-test ERROR: {exc}")
            all_passed = False

    prefix = "✅ HEALTH OK" if all_passed else "❌ HEALTH FAILURE"
    return f"{prefix}\n" + "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--smoke-test" in args:
        print(smoke_test())
    else:
        print(check())
