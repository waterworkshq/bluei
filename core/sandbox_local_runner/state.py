"""sandbox_local_runner.state — State, issues, findings persistence and workload reconciliation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    DEFAULT_FINDING_COOLDOWN_SECONDS,
    MAX_RECONCILIATION_EVENTS,
)
from .reforge import RefactorPhase, RefactorWork
from .models import Finding, age_seconds, now_iso
from .utils import run_capture
from .gh import fetch_github_live_counts, get_origin_url

MAX_COOLDOWN_SECONDS = 7 * 24 * 60 * 60  # 7 days — cap for exponential backoff


def get_effective_cooldown(
    finding_id: str,
    state: Dict[str, Any],
    base_cooldown_seconds: int,
) -> int:
    """Returns effective cooldown for a finding, accounting for failure history.

    Formula:
      - failure_count == 0 (or absent): base_cooldown_seconds
      - failure_count >= 1: base_cooldown_seconds * (2 ** failure_count)
      - Capped at MAX_COOLDOWN_SECONDS (7 days)

    State is read but NOT mutated by this function.

    Args:
        finding_id: The finding to look up.
        state: The full state dict (from load_state / in-memory).
        base_cooldown_seconds: The flat cooldown configured by the user.

    Returns:
        Effective cooldown in seconds.
    """
    activity = state.get('finding_activity', {})
    entry = activity.get(finding_id, {})
    failure_count = entry.get('failure_count', 0)

    if failure_count == 0:
        return base_cooldown_seconds

    effective = base_cooldown_seconds * (2 ** failure_count)
    return min(effective, MAX_COOLDOWN_SECONDS)


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            'open_issues': 0,
            'open_prs': 0,
            'created': [],
            'finding_activity': {},
            'reconciliation_events': [],
        }
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        data = {}
    data.setdefault('open_issues', 0)
    data.setdefault('open_prs', 0)
    data.setdefault('created', [])
    data.setdefault('finding_activity', {})
    data.setdefault('reconciliation_events', [])
    return data


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')


def _append_text(path: Path, msg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(f"[{now_iso()}] {msg}\n")


def load_findings_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    seen: set[str] = set()
    with path.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                fid = obj.get('finding_id')
                if fid:
                    seen.add(str(fid))
            except Exception:
                continue
    return seen


def append_findings(path: Path, findings: List[Finding]) -> int:
    seen = load_findings_seen(path)
    written = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        for finding in findings:
            if finding.finding_id in seen:
                continue
            payload = finding.as_dict()
            payload['discovered_at'] = now_iso()
            f.write(json.dumps(payload, sort_keys=True) + '\n')
            written += 1
    return written


def load_issues(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {'issues': []}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        return {'issues': []}
    if 'issues' not in data or not isinstance(data['issues'], list):
        data['issues'] = []
    return data


def save_issues(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


# Statuses that indicate an issue is NOT actionable (blocked/escalated/resolved)
NON_ACTIONABLE_ISSUE_STATUSES = frozenset({
    'needs-human-max-retries-exceeded',
    'needs-human-validation-failed',
    'needs-human-scope-limit-exceeded',
    'needs-human-commit-failed',
    'needs-human-push-failed',
    'blocked_untracked_path',
    'resolved_merged',
    'resolved_verified',
    'needs-human-not-fixable',  # Rule not autofixable and not LLM-fixable
    'needs-human-refactor-review',  # Structural refactor routed to dedicated queue/review lane
})


def count_actionable_issues(issues_data: Dict[str, Any]) -> int:
    """Count only actionable issues (excluding blocked/escalated/resolved)."""
    actionable = 0
    for issue in issues_data.get('issues', []):
        status = issue.get('status', 'open')
        if status not in NON_ACTIONABLE_ISSUE_STATUSES:
            actionable += 1
    return actionable


def guard_open_issues(open_issues: int, cap: int) -> tuple[bool, str]:
    """Check if open issues (count) is below the cap.

    IMPORTANT: Callers should use count_actionable_issues() to get open_issues
    rather than raw counts, so blocked/escalated issues don't stall the pipeline.
    """
    if open_issues >= cap:
        return False, f'guard-block: open issues={open_issues} meets/exceeds issue cap={cap}'
    return True, f'guard-pass: open issues={open_issues} below issue cap={cap}'


def guard_open_prs(open_prs: int, cap: int) -> tuple[bool, str]:
    if open_prs >= cap:
        return False, f'guard-block: open prs={open_prs} meets/exceeds PR cap={cap}'
    return True, f'guard-pass: open prs={open_prs} below PR cap={cap}'


def record_reconciliation_event(
    state: Dict[str, Any],
    log_file: Path,
    before_issues: int,
    before_prs: int,
    after_issues: int,
    after_prs: int,
    reason: str,
) -> Dict[str, Any]:
    event = {
        'timestamp': now_iso(),
        'reason': reason,
        'before': {
            'open_issues': before_issues,
            'open_prs': before_prs,
        },
        'after': {
            'open_issues': after_issues,
            'open_prs': after_prs,
        },
    }
    events = state.setdefault('reconciliation_events', [])
    events.append(event)
    if len(events) > MAX_RECONCILIATION_EVENTS:
        del events[:-MAX_RECONCILIATION_EVENTS]
    _append_text(
        log_file,
        'reconcile: '
        f"reason={reason} "
        f"before_open_issues={before_issues} before_open_prs={before_prs} "
        f"after_open_issues={after_issues} after_open_prs={after_prs}",
    )
    return event


def reconcile_open_workload(
    repo_path: Path,
    state: Dict[str, Any],
    log_file: Path,
    simulate_open_issues: Optional[int],
    simulate_open_prs: Optional[int],
) -> tuple[int, int, Dict[str, Any]]:
    before_issues = int(state.get('open_issues', 0))
    before_prs = int(state.get('open_prs', 0))
    after_issues = before_issues
    after_prs = before_prs
    reason = 'state-counters'

    origin_url = get_origin_url(repo_path)
    live_counts, live_reason = fetch_github_live_counts(repo_path)
    if live_counts is not None:
        after_issues = int(live_counts['open_issues'])
        after_prs = int(live_counts['open_prs'])
        reason = live_reason
    elif 'github.com' in origin_url:
        reason = live_reason
    else:
        if simulate_open_issues is not None:
            after_issues = int(simulate_open_issues)
            reason = 'cli-simulated-open-workload'
        if simulate_open_prs is not None:
            after_prs = int(simulate_open_prs)
            reason = 'cli-simulated-open-workload'

    state['open_issues'] = after_issues
    state['open_prs'] = after_prs
    event = record_reconciliation_event(
        state=state,
        log_file=log_file,
        before_issues=before_issues,
        before_prs=before_prs,
        after_issues=after_issues,
        after_prs=after_prs,
        reason=reason,
    )
    return after_issues, after_prs, event


def mark_finding_activity(
    state: Dict[str, Any],
    finding_ids: List[str],
    action: str,
    failure_count: Optional[int] = None,
    last_error: Optional[str] = None,
) -> None:
    """Record an activity event for one or more findings.

    Args:
        state: The full state dict (mutated in-place).
        finding_ids: Finding IDs to record activity for.
        action: Human-readable action label (e.g. 'fix-attempt', 'fix-succeeded').
        failure_count: Optional. If provided, stores the failure count on the
            finding's activity entry, enabling exponential backoff cooldown.
        last_error: Optional. If provided, stores the last error string.
    """
    if not finding_ids:
        return
    activity = state.setdefault('finding_activity', {})
    ts = now_iso()
    for finding_id in finding_ids:
        entry = activity.setdefault(finding_id, {
            'last_action': action,
            'last_action_at': ts,
        })
        entry['last_action'] = action
        entry['last_action_at'] = ts
        if failure_count is not None:
            entry['failure_count'] = failure_count
        if last_error is not None:
            entry['last_error'] = last_error


def filter_findings_by_cooldown(
    findings: List[Finding],
    state: Dict[str, Any],
    cooldown_seconds: int,
    log_file: Path,
) -> tuple[List[Finding], List[Finding]]:
    allowed: List[Finding] = []
    suppressed: List[Finding] = []
    activity = state.setdefault('finding_activity', {})
    now = datetime.now(timezone.utc)

    for finding in findings:
        entry = activity.get(finding.finding_id, {})
        last_action_at = entry.get('last_action_at')
        effective_cooldown = get_effective_cooldown(
            finding.finding_id, state, cooldown_seconds
        )
        elapsed = age_seconds(last_action_at, reference=now)
        if elapsed is not None and elapsed < effective_cooldown:
            remaining = effective_cooldown - elapsed
            failure_count = entry.get('failure_count', 0)
            _append_text(
                log_file,
                'cooldown-suppress: '
                f'finding_id={finding.finding_id} '
                f'last_action={entry.get("last_action", "unknown")} '
                f'last_action_at={last_action_at} '
                f'effective_cooldown={effective_cooldown} '
                f'remaining_seconds={remaining} '
                f'failure_count={failure_count}',
            )
            suppressed.append(finding)
            continue
        allowed.append(finding)

    return allowed, suppressed


def load_finding_record(finding_id: str, findings_file: Path) -> Optional[Dict[str, Any]]:
    """Load a single finding record by finding_id from a JSONL file.

    Returns the dict representation of the finding, or None if not found.
    Does NOT reconstruct a Finding object — returns raw dict for efficiency.
    Handles malformed lines gracefully.
    """
    if not findings_file.exists():
        return None
    with findings_file.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if obj.get('finding_id') == finding_id:
                    return obj
            except Exception:
                continue
    return None


def update_finding_record(finding_id: str, findings_file: Path, updates: Dict[str, Any]) -> bool:
    """Patch a finding record's extra fields in-place in a JSONL file.

    Reads all records, replaces the matching one, rewrites the file.
    Returns True if found and updated, False if not found.
    Malformed lines are preserved as raw strings on rewrite.
    """
    if not findings_file.exists():
        return False

    records: List[Any] = []   # List[Dict] or str (raw malformed lines)
    found = False
    with findings_file.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if obj.get('finding_id') == finding_id:
                    obj.update(updates)
                    found = True
                records.append(obj)
            except Exception:
                # Malformed line — preserve as raw string
                records.append(raw)

    if not found:
        return False

    findings_file.parent.mkdir(parents=True, exist_ok=True)
    with findings_file.open('w', encoding='utf-8') as f:
        for item in records:
            if isinstance(item, str):
                f.write(item + '\n')
            else:
                f.write(json.dumps(item, sort_keys=True) + '\n')
    return True


def increment_fix_attempt(
    finding_id: str,
    findings_file: Path,
    error: Optional[str],
) -> None:
    """Increment fix_attempts, set last_fix_at, last_fix_error on a finding record.

    Called after every fix attempt (both success and failure) in apply_claude_fix.
    On success, caller also calls update_finding_record with fix_success=True.
    Handles missing files, missing records, and missing fields gracefully.
    """
    record = load_finding_record(finding_id, findings_file)
    current_attempts = record.get('fix_attempts', 0) if record else 0

    updates: Dict[str, Any] = {
        'fix_attempts': current_attempts + 1,
        'last_fix_at': now_iso(),
    }
    if error is not None:
        # Truncate long errors to prevent unbounded field growth
        updates['last_fix_error'] = error[:500] if len(error) > 500 else error

    update_finding_record(finding_id, findings_file, updates)


def load_refactor_work(finding_id: str, findings_file: Path) -> Optional[RefactorWork]:
    """Load persisted RefactorWork for a finding from the findings JSONL file."""
    record = load_finding_record(finding_id, findings_file)
    if not record:
        return None
    raw = record.get('refactor_work')
    if not isinstance(raw, dict):
        return None
    try:
        return RefactorWork(
            finding_id=raw.get('finding_id', finding_id),
            phase=RefactorPhase(raw.get('phase', RefactorPhase.PLANNING.value)),
            planned_targets=list(raw.get('planned_targets', [])),
            original_line_count=int(raw.get('original_line_count', 0) or 0),
            target_lines_per_file=int(raw.get('target_lines_per_file', 0) or 0),
            written_files=set(raw.get('written_files', [])),
            baseline_fingerprint=str(raw.get('baseline_fingerprint', '')),
            needs_human_review=bool(raw.get('needs_human_review', False)),
            review_outcome=raw.get('review_outcome'),
        )
    except Exception:
        return None


def save_refactor_work(finding_id: str, findings_file: Path, refactor_work: RefactorWork) -> bool:
    """Persist RefactorWork under the finding record in the findings JSONL file."""
    payload = {
        'refactor_work': {
            'finding_id': refactor_work.finding_id,
            'phase': refactor_work.phase.value,
            'planned_targets': list(refactor_work.planned_targets),
            'original_line_count': refactor_work.original_line_count,
            'target_lines_per_file': refactor_work.target_lines_per_file,
            'written_files': sorted(refactor_work.written_files),
            'baseline_fingerprint': refactor_work.baseline_fingerprint,
            'needs_human_review': refactor_work.needs_human_review,
            'review_outcome': refactor_work.review_outcome,
        },
        'refactor_phase': refactor_work.phase.value,
    }
    return update_finding_record(finding_id, findings_file, payload)


def get_pending_refactor_work(findings_file: Path) -> List[Dict[str, Any]]:
    """Return finding records with refactor work still awaiting completion."""
    if not findings_file.exists():
        return []

    pending: List[Dict[str, Any]] = []
    with findings_file.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            refactor_work = obj.get('refactor_work')
            if not isinstance(refactor_work, dict):
                continue
            phase = str(refactor_work.get('phase', ''))
            if phase in (RefactorPhase.DONE.value, RefactorPhase.ABORTED.value):
                continue
            pending.append(obj)
    return pending


# ────────────────────────────────────────────────────────────────
# Batch PR state persistence (Phase 1)
# ────────────────────────────────────────────────────────────────


def load_batches(path: Path) -> List[Dict[str, Any]]:
    """Load batch records from a JSONL file.

    Returns an empty list if the file does not exist or is empty.
    Malformed lines are silently skipped.
    """
    if not path.exists():
        return []
    batches: List[Dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                batches.append(json.loads(raw))
            except Exception:
                continue
    return batches


def save_batch_record(path: Path, record: Dict[str, Any]) -> None:
    """Append a single batch record to the JSONL file.

    Creates parent directories if they don't exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, sort_keys=True) + '\n')


def update_batch_record(path: Path, batch_id: str, updates: Dict[str, Any]) -> bool:
    """Rewrite the batches file with an updated record.

    Reads all records, updates the matching one, rewrites the file.
    Returns True if a record was found and updated, False otherwise.
    Malformed lines are silently skipped (not preserved).
    """
    if not path.exists():
        return False

    records: List[Dict[str, Any]] = []
    found = False
    with path.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if obj.get('batch_id') == batch_id:
                    obj.update(updates)
                    found = True
                records.append(obj)
            except Exception:
                continue

    if not found:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + '\n')
    return True
