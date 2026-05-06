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

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .state import _append_text


# ── Config (can be promoted to CLI args / config.yaml later) ──

CONSECUTIVE_MERGE_THRESHOLD = 3
REAPPEARING_FINDING_THRESHOLD = 3
DEDUP_SATURATION_THRESHOLD = 3
REBASE_CONFLICT_TREND_THRESHOLD = 3


def _load_escalation_log(log_file: Path) -> List[Dict[str, Any]]:
    """Load existing escalation log entries."""
    if not log_file.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        for line in log_file.read_text().strip().splitlines():
            if line.strip():
                records.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        pass
    return records


def _append_escalation(escalation_file: Path, record: Dict[str, Any]) -> None:
    """Append one escalation record to the log."""
    try:
        escalation_file.parent.mkdir(parents=True, exist_ok=True)
        with open(escalation_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, default=str) + '\n')
    except OSError:
        pass


def _load_rebase_stats(stats_file: Path) -> List[Dict[str, Any]]:
    """Load rebase telemetry for trend detection."""
    if not stats_file.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        for line in stats_file.read_text().strip().splitlines():
            if line.strip():
                records.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        pass
    return records


def check_merge_failure_pattern(
    merges_failed: int,
    merges_succeeded: int,
    thresholds: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """Detect consecutive merge failures.

    If merges_failed >= threshold and merges_succeeded == 0,
    returns an escalation record. Otherwise returns None.
    """
    threshold = (thresholds or {}).get('consecutive_merge', CONSECUTIVE_MERGE_THRESHOLD)
    if merges_failed >= threshold and merges_succeeded == 0:
        return {
            'type': 'consecutive_merge_failures',
            'count': merges_failed,
            'threshold': threshold,
            'detail': f'{merges_failed} consecutive merge failures with no successful merges',
        }
    return None


def check_reappearing_findings(
    issues_data: Dict[str, Any],
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Detect findings that keep reappearing after being fixed.

    Checks issue history for findings with >= threshold
    fix_failed_verification or needs-human events.
    """
    threshold = (thresholds or {}).get('reappearing_finding', REAPPEARING_FINDING_THRESHOLD)
    escalated: List[Dict[str, Any]] = []

    from .orchestrator import count_failed_fix_attempts

    for issue in issues_data.get('issues', []):
        finding_id = issue.get('finding_id', '')
        if not finding_id:
            continue
        attempts = count_failed_fix_attempts(issue)
        if attempts >= threshold:
            escalated.append({
                'type': 'reappearing_finding',
                'finding_id': finding_id,
                'rule': issue.get('rule', ''),
                'path': issue.get('path', ''),
                'failed_attempts': attempts,
                'threshold': threshold,
                'detail': f'Finding {finding_id} failed {attempts} times (threshold: {threshold})',
            })

    return escalated


def check_dedup_saturation(
    cycle_log_lines: List[str],
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Detect dedup guard saturation from cycle log lines.

    If batch-skip-duplicate appears >= threshold times in the
    current cycle, the batch config may need tuning.
    """
    threshold = (thresholds or {}).get('dedup_saturation', DEDUP_SATURATION_THRESHOLD)
    dedup_count = sum(1 for l in cycle_log_lines if 'batch-skip-duplicate' in l)

    if dedup_count >= threshold:
        return [{
            'type': 'dedup_saturation',
            'count': dedup_count,
            'threshold': threshold,
            'detail': f'Dedup guard fired {dedup_count} times in this cycle (threshold: {threshold})',
        }]
    return []


def check_rebase_conflict_trend(
    stats_file: Path,
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Detect rebase conflict trends from telemetry.

    If the last 3 rebase sweeps all had conflicts, flag it.
    """
    threshold = (thresholds or {}).get('rebase_conflict', REBASE_CONFLICT_TREND_THRESHOLD)
    records = _load_rebase_stats(stats_file)
    if len(records) < threshold:
        return []

    # Check last N sweeps for conflicts
    recent = records[-threshold:]
    all_had_conflicts = all(len(r.get('conflicted', [])) > 0 for r in recent)
    if all_had_conflicts:
        return [{
            'type': 'rebase_conflict_trend',
            'sweeps_checked': threshold,
            'total_conflicts': sum(len(r.get('conflicted', [])) for r in recent),
            'detail': f'Last {threshold} rebase sweeps all had conflicts',
        }]
    return []


def run_escalation_checks(
    run_log_file: Path,
    escalation_file: Path,
    issues_data: Dict[str, Any],
    merges_failed: int,
    merges_succeeded: int,
    thresholds: Optional[Dict[str, int]] = None,
    rebase_stats_file: Optional[Path] = None,
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
            pass

    # 1. Consecutive merge failures
    merge_issue = check_merge_failure_pattern(merges_failed, merges_succeeded, thresholds)
    if merge_issue:
        findings.append(merge_issue)

    # 2. Reappearing findings
    findings.extend(check_reappearing_findings(issues_data, thresholds))

    # 3. Dedup saturation
    findings.extend(check_dedup_saturation(log_lines, thresholds))

    # 4. Rebase conflict trend
    if rebase_stats_file is not None:
        findings.extend(check_rebase_conflict_trend(rebase_stats_file, thresholds))

    # Log escalations
    if findings:
        record = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'findings': findings,
            'count': len(findings),
        }
        _append_escalation(escalation_file, record)

    return findings
