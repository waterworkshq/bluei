"""escalation.py — Pattern detection and escalation thresholds.

Detects systemic patterns that indicate problems beyond individual PRs/issues:
- Consecutive merge failures
- Repeatedly reappearing findings
- Dedup guard saturation
- Rebase conflict trends

Logs structured records to state/escalation_log.jsonl for downstream
monitoring and Sound notification.
"""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

from bluei.engine.jsonl import append_jsonl, read_jsonl
from bluei.engine.models import IssueStatus
from bluei.engine.state import _append_text


# ── Config (can be promoted to CLI args / config.yaml later) ──

CONSECUTIVE_MERGE_THRESHOLD = 3
REAPPEARING_FINDING_THRESHOLD = 3
DEDUP_SATURATION_THRESHOLD = 3
REBASE_CONFLICT_TREND_THRESHOLD = 3
MAX_DUPLICATE_PRS_THRESHOLD = 3
MAX_DUPLICATE_PAUSE_SECONDS = 6 * 60 * 60


@dataclass
class EscalationConfig:
    """Configuration for cycle-level escalation thresholds.

    Attributes:
        consecutive_failure_threshold: Number of consecutive cycle failures
            before an escalation event is triggered (default: 3).
        silence_after_resolved_minutes: After an escalation event is logged,
            suppress further escalation for this finding for N minutes
            (default: 60).
        escalation_log_path: Path to the JSONL escalation log file.
    """

    consecutive_failure_threshold: int = 3
    silence_after_resolved_minutes: int = 60
    escalation_log_path: Optional[Path] = None

    @property
    def silence_seconds(self) -> int:
        return self.silence_after_resolved_minutes * 60


DEFAULT_ESCALATION_CONFIG = EscalationConfig(
    escalation_log_path=Path("state/escalation_log.jsonl"),
)


def _append_escalation(escalation_file: Path, record: Dict[str, Any]) -> None:
    """Append one escalation record to the log."""
    try:
        append_jsonl(escalation_file, record, default=str)
    except OSError:
        _logger.debug("Failed to append escalation record")


def check_cycle_escalation(
    config: EscalationConfig,
    cycle_log: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Check if the last N consecutive cycles were failures.

    Inspects the cycle log (list of dicts with 'status' and 'finding_id'
    keys) for the most recent entries.  If the last N (where N =
    config.consecutive_failure_threshold) consecutive entries for ANY
    finding are failures, returns an escalation event dict for that
    finding.

    Args:
        config: EscalationConfig with threshold settings.
        cycle_log: List of cycle result dicts.  Each dict should have:
            - 'finding_id': str
            - 'status': str ('success' or 'failure')
            - 'cycle_type': str (e.g. 'pr-cycle', 'merge-cycle')
            - 'timestamp': str (ISO-8601)

    Returns:
        An escalation event dict if threshold breached, else None.
        Event dict structure:
            {
                'type': 'cycle_escalation',
                'finding_id': str,
                'consecutive_failures': int,
                'threshold': int,
                'cycle_type': str,
                'detail': str,
            }
    """
    if not cycle_log:
        return None

    threshold = config.consecutive_failure_threshold
    if threshold < 1:
        return None

    # Group cycle_log entries by finding_id, preserving order
    finding_logs: Dict[str, List[Dict[str, Any]]] = {}
    for entry in cycle_log:
        fid = entry.get("finding_id", "")
        if not fid:
            continue
        finding_logs.setdefault(fid, []).append(entry)

    for finding_id, entries in finding_logs.items():
        if len(entries) < threshold:
            continue

        # Check the last N consecutive entries for this finding
        recent = entries[-threshold:]
        all_failures = all(
            str(e.get("status", "")).lower() == "failure" for e in recent
        )
        if all_failures:
            return {
                "type": "cycle_escalation",
                "finding_id": finding_id,
                "consecutive_failures": threshold,
                "threshold": threshold,
                "cycle_type": recent[-1].get("cycle_type", "unknown"),
                "detail": (
                    f"Finding {finding_id} failed {threshold} consecutive cycles "
                    f"(last cycle: {recent[-1].get('cycle_type', 'unknown')})"
                ),
            }

    return None


def log_escalation_event(
    config: EscalationConfig,
    event: Dict[str, Any],
) -> None:
    """Write an escalation event to the escalation log.

    Wraps the event in a standard envelope with timestamp and writes
    as one JSONL line to config.escalation_log_path.

    Args:
        config: EscalationConfig specifying the log path.
        event: The escalation event dict to log.
    """
    log_path = config.escalation_log_path
    if log_path is None:
        return

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "findings": [event],
        "count": 1,
    }
    try:
        append_jsonl(log_path, record, default=str)
    except OSError:
        _logger.debug("Failed to append escalation stats")


def _load_rebase_stats(stats_file: Path) -> List[Dict[str, Any]]:
    """Load rebase telemetry for trend detection."""
    return read_jsonl(stats_file)


def check_merge_failure_pattern(
    merges_failed: int,
    merges_succeeded: int,
    thresholds: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """Detect consecutive merge failures.

    If merges_failed >= threshold and merges_succeeded == 0,
    returns an escalation record. Otherwise returns None.
    """
    threshold = (thresholds or {}).get("consecutive_merge", CONSECUTIVE_MERGE_THRESHOLD)
    if merges_failed >= threshold and merges_succeeded == 0:
        return {
            "type": "consecutive_merge_failures",
            "count": merges_failed,
            "threshold": threshold,
            "detail": f"{merges_failed} consecutive merge failures with no successful merges",
        }
    return None


def check_reappearing_findings(
    issues_data: Dict[str, Any],
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Detect findings that keep reappearing after being fixed.

    Re-exported from issue_lifecycle — kept for backward compat.
    """
    from bluei.engine.issue_lifecycle import check_reappearing_findings as _impl

    return _impl(issues_data, thresholds)


def check_dedup_saturation(
    cycle_log_lines: List[str],
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Detect dedup guard saturation from cycle log lines.

    If batch-skip-duplicate appears >= threshold times in the
    current cycle, the batch config may need tuning.
    """
    threshold = (thresholds or {}).get("dedup_saturation", DEDUP_SATURATION_THRESHOLD)
    dedup_count = sum(1 for l in cycle_log_lines if "batch-skip-duplicate" in l)

    if dedup_count >= threshold:
        return [
            {
                "type": "dedup_saturation",
                "count": dedup_count,
                "threshold": threshold,
                "detail": f"Dedup guard fired {dedup_count} times in this cycle (threshold: {threshold})",
            }
        ]
    return []


def check_rebase_conflict_trend(
    stats_file: Path,
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Detect rebase conflict trends from telemetry.

    If the last 3 rebase sweeps all had conflicts, flag it.
    """
    threshold = (thresholds or {}).get(
        "rebase_conflict", REBASE_CONFLICT_TREND_THRESHOLD
    )
    records = _load_rebase_stats(stats_file)
    if len(records) < threshold:
        return []

    # Check last N sweeps for conflicts
    recent = records[-threshold:]
    all_had_conflicts = all(len(r.get("conflicted", [])) > 0 for r in recent)
    if all_had_conflicts:
        return [
            {
                "type": "rebase_conflict_trend",
                "sweeps_checked": threshold,
                "total_conflicts": sum(len(r.get("conflicted", [])) for r in recent),
                "detail": f"Last {threshold} rebase sweeps all had conflicts",
            }
        ]
    return []


def load_escalation_log(
    config: EscalationConfig,
) -> List[Dict[str, Any]]:
    """Load all escalation records from the log.

    Args:
        config: EscalationConfig specifying the log path.

    Returns:
        List of escalation record dicts, newest first.
    """
    log_path = config.escalation_log_path
    if log_path is None:
        return []
    # Preserve original "any bad line → empty" semantics: a single
    # malformed line aborts the whole read. Use skip_errors=False and
    # catch the propagated exception to return [].
    try:
        records = read_jsonl(log_path, skip_errors=False)
    except (json.JSONDecodeError, OSError):
        _logger.debug("Failed to read escalation log")
        return []
    # read_jsonl returns oldest-first; reverse for newest-first.
    records.reverse()
    return records


def check_max_duplicate_prs(
    repo_slug: str,
    cwd: Path,
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Detect findings with too many open PRs.

    Queries open PRs, groups by dedupe_key in the PR body, and
    escalates when any single finding has accumulated >= threshold
    open PRs simultaneously. This usually indicates the finding
    cannot be fixed automatically and needs human review.

    Args:
        repo_slug: GitHub repo slug (owner/name).
        cwd: Working directory for gh CLI.
        thresholds: Optional threshold overrides.

    Returns:
        List of escalation records for findings exceeding the threshold.
    """
    threshold = (thresholds or {}).get("max_duplicate_prs", MAX_DUPLICATE_PRS_THRESHOLD)

    from bluei.engine.gh import gh_json

    payload = gh_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo_slug,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            "number,title,url,body",
        ],
        cwd=cwd,
    )
    if not isinstance(payload, list):
        return []

    dedupe_counts: Dict[str, List[Dict[str, Any]]] = {}
    for pr in payload:
        body = str(pr.get("body") or "")
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("- dedupe_key:"):
                finding_id = line.split(":", 1)[1].strip()
                if finding_id:
                    dedupe_counts.setdefault(finding_id, []).append(pr)
                break

    findings: List[Dict[str, Any]] = []
    for finding_id, prs in dedupe_counts.items():
        if len(prs) >= threshold:
            findings.append(
                {
                    "type": "max_duplicate_prs",
                    "finding_id": finding_id,
                    "open_pr_count": len(prs),
                    "pr_numbers": [
                        pr["number"] for pr in prs if isinstance(pr.get("number"), int)
                    ],
                    "threshold": threshold,
                    "detail": (
                        f"Finding {finding_id} has {len(prs)} open PRs "
                        f"(threshold: {threshold}): "
                        f"{', '.join(str(pr.get('number')) for pr in prs)}"
                    ),
                }
            )
    return findings


def handle_max_duplicate_escalation(
    escalations: List[Dict[str, Any]],
    repo_slug: str,
    cwd: Path,
    state: Dict[str, Any],
    issues_data: Dict[str, Any],
    log_file: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Respond to max_duplicate_prs escalations.

    Three operations, all in one call:
    1. Close excess PRs (keep newest), gated by dry_run.
    2. Route matching findings to human review via issue status (permanent
       until human clears — the status goes into NON_ACTIONABLE_ISSUE_STATUSES).
    3. Record activity in state for audit and cooldown tracking.

    Mutates state and issues_data in-place. Caller persists them.

    Args:
        escalations: Output of check_max_duplicate_prs().
        repo_slug: GitHub repo slug.
        cwd: Working directory for gh CLI.
        state: State dict (mutated in-place).
        issues_data: Issues dict (mutated in-place).
        log_file: Log file for action records.
        dry_run: If True, skip PR close (route+activity still execute).

    Returns:
        Summary dict with closed_prs, close_failed, paused_findings, routed_findings.
    """
    from bluei.engine.clean_prs import _close_pr
    from bluei.engine.orchestrator import set_issue_status
    from bluei.engine.state import mark_finding_activity

    closed_prs: List[int] = []
    close_failed: List[int] = []
    paused_findings: List[str] = []
    routed_findings: List[str] = []

    for esc in escalations:
        finding_id = esc.get("finding_id", "")
        pr_numbers = esc.get("pr_numbers", [])
        open_count = esc.get("open_pr_count", 0)

        if not finding_id:
            continue

        # 1. Close excess PRs (keep newest = highest number)
        if pr_numbers and len(pr_numbers) > 1 and not dry_run:
            sorted_prs = sorted(pr_numbers)
            kept = sorted_prs[-1]
            to_close = sorted_prs[:-1]
            for pr_num in to_close:
                reason = (
                    f"Closing duplicate — finding {finding_id} has {open_count} open PRs. "
                    f"Retained #{kept}. Routed to human review."
                )
                try:
                    if _close_pr(repo_slug, pr_num, reason, False, cwd):
                        closed_prs.append(pr_num)
                        _append_text(
                            log_file,
                            f"  max-dup-close: #{pr_num} (finding {finding_id}, kept #{kept})",
                        )
                    else:
                        close_failed.append(pr_num)
                        _append_text(
                            log_file,
                            f"  max-dup-close-failed: #{pr_num} (finding {finding_id})",
                        )
                except Exception:
                    close_failed.append(pr_num)
                    _append_text(
                        log_file,
                        f"  max-dup-close-error: #{pr_num} (finding {finding_id})",
                    )
        elif dry_run and pr_numbers and len(pr_numbers) > 1:
            _append_text(
                log_file,
                f"  max-dup-dry-run: would close {len(pr_numbers) - 1} PRs for finding {finding_id}",
            )

        # 2. Route matching issues to human review
        for issue in issues_data.get("issues", []):
            if issue.get("finding_id") == finding_id:
                if (
                    issue.get("status")
                    != IssueStatus.NEEDS_HUMAN_MAX_DUPLICATES_EXCEEDED.value
                ):
                    set_issue_status(
                        issue,
                        IssueStatus.NEEDS_HUMAN_MAX_DUPLICATES_EXCEEDED.value,
                        f"{open_count} open PRs for finding {finding_id} (threshold: {esc.get('threshold', 3)})",
                    )
                    routed_findings.append(finding_id)
                    _append_text(
                        log_file,
                        f"  max-dup-route: finding {finding_id} routed to human review ({open_count} PRs)",
                    )
                break

        # 3. Pause finding via cooldown
        mark_finding_activity(
            state, [finding_id], "max-duplicates-paused", failure_count=1
        )
        paused_findings.append(finding_id)
        _append_text(
            log_file,
            f"  max-dup-pause: finding {finding_id} paused (routed to human, will not be re-attempted)",
        )

    return {
        "closed_prs": closed_prs,
        "close_failed": close_failed,
        "paused_findings": paused_findings,
        "routed_findings": routed_findings,
    }


def run_escalation_checks(
    run_log_file: Path,
    escalation_file: Path,
    issues_data: Dict[str, Any],
    merges_failed: int,
    merges_succeeded: int,
    thresholds: Optional[Dict[str, int]] = None,
    rebase_stats_file: Optional[Path] = None,
    repo_slug: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Run all escalation checks and log any findings.

    Returns the list of escalation records (empty = all clean).
    """
    findings: List[Dict[str, Any]] = []

    # Read current cycle log lines
    log_lines: List[str] = []
    if run_log_file.exists():
        try:
            log_lines = run_log_file.read_text().splitlines()
        except OSError:
            _logger.debug("Failed to read run log file")

    # 1. Consecutive merge failures
    merge_issue = check_merge_failure_pattern(
        merges_failed, merges_succeeded, thresholds
    )
    if merge_issue:
        findings.append(merge_issue)

    # 2. Reappearing findings
    findings.extend(check_reappearing_findings(issues_data, thresholds))

    # 3. Dedup saturation
    findings.extend(check_dedup_saturation(log_lines, thresholds))

    # 4. Rebase conflict trend
    if rebase_stats_file is not None:
        findings.extend(check_rebase_conflict_trend(rebase_stats_file, thresholds))

    # 5. Max duplicate PRs
    if repo_slug and cwd is not None:
        findings.extend(check_max_duplicate_prs(repo_slug, cwd, thresholds))

    # Log escalations
    if findings:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "findings": findings,
            "count": len(findings),
        }
        _append_escalation(escalation_file, record)

    return findings


# ---------------------------------------------------------------------------
# Escalation log writer (merged from app/escalation.py)
# ---------------------------------------------------------------------------


DEFAULT_ESCALATION_FILE = Path("state/escalation_log.jsonl")


def write_escalation(
    message: str,
    severity: str = "error",
    repo: Optional[str] = None,
    escalation_file: Optional[Path] = None,
) -> None:
    """Write an escalation record to escalation_log.jsonl.

    Args:
        message: Human-readable escalation message.
        severity: Severity level (info, warning, error).
        repo: Optional repo name for context.
        escalation_file: Path to escalation log. Defaults to
                        state/escalation_log.jsonl relative to CWD.
    """
    file_path = escalation_file or DEFAULT_ESCALATION_FILE
    record: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "findings": [
            {
                "type": severity,
                "detail": f"[{repo}] {message}" if repo else message,
            }
        ],
        "count": 1,
    }

    try:
        abs_path = (
            file_path.resolve() if file_path else Path.cwd() / DEFAULT_ESCALATION_FILE
        )
        append_jsonl(abs_path, record, default=str)
    except OSError:
        _logger.debug("Failed to write escalation record")


def check_escalation_status(repo_name: str) -> Dict[str, Any]:
    """Read the escalation log and return current escalation status for a repo.

    Args:
        repo_name: Repository name or slug to filter by.

    Returns:
        Dict with keys: total_escalations, active_escalations, latest, types, repo.
    """
    escalation_file = DEFAULT_ESCALATION_FILE

    records: list[Dict[str, Any]] = read_jsonl(escalation_file)

    repo_records: list[Dict[str, Any]] = []
    for r in records:
        findings = r.get("findings", [])
        if not findings:
            continue
        repo_match = False
        for f in findings:
            detail = str(f.get("detail", "") or "")
            if repo_name in detail:
                repo_match = True
                break
        if repo_match:
            repo_records.append(r)

    now = datetime.now(timezone.utc)
    active_cutoff = now.timestamp() - 3600

    active_count = 0
    for r in repo_records:
        ts_str = r.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str).timestamp()
                if ts >= active_cutoff:
                    active_count += 1
            except (ValueError, TypeError):
                _logger.debug("Failed to parse escalation timestamp")

    type_counts: Dict[str, int] = {}
    for r in repo_records:
        for f in r.get("findings", []):
            ftype = str(f.get("type", "unknown"))
            type_counts[ftype] = type_counts.get(ftype, 0) + 1

    latest = repo_records[-1] if repo_records else None

    return {
        "total_escalations": len(repo_records),
        "active_escalations": active_count,
        "latest": latest,
        "types": type_counts,
        "repo": repo_name,
    }
