"""Issue lifecycle: creation, status, history, failure tracking, persistence.

Concentrates all issue-related operations that were previously scattered
across orchestrator.py, state.py, and escalation.py."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.models import (
    Finding,
    IssueStatus,
    NEEDS_HUMAN_STATUSES,
    is_needs_human_status,
    now_iso,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence (moved from engine/state.py)
# ---------------------------------------------------------------------------


def load_issues(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"issues": []}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        return {"issues": []}
    if "issues" not in data or not isinstance(data["issues"], list):
        data["issues"] = []
    return data


def save_issues(path: Path, data: Dict[str, Any]) -> None:
    from bluei.engine.state_io import atomic_json_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, data)


# Statuses that indicate an issue is NOT actionable (blocked/escalated/resolved)
# Derived from IssueStatus enum so the canonical contract stays in one place.
NON_ACTIONABLE_ISSUE_STATUSES = frozenset(member.value for member in IssueStatus)


def count_actionable_issues(issues_data: Dict[str, Any]) -> int:
    """Count only actionable issues (excluding blocked/escalated/resolved)."""
    actionable = 0
    for issue in issues_data.get("issues", []):
        status = issue.get("status", "open")
        if status not in NON_ACTIONABLE_ISSUE_STATUSES:
            actionable += 1
    return actionable


def guard_open_issues(open_issues: int, cap: int) -> tuple[bool, str]:
    """Check if open issues (count) is below the cap.

    IMPORTANT: Callers should use count_actionable_issues() to get open_issues
    rather than raw counts, so blocked/escalated issues don't stall the pipeline.
    """
    if open_issues >= cap:
        return (
            False,
            f"guard-block: open issues={open_issues} meets/exceeds issue cap={cap}",
        )
    return True, f"guard-pass: open issues={open_issues} below issue cap={cap}"


# ---------------------------------------------------------------------------
# Query (moved from engine/orchestrator.py)
# ---------------------------------------------------------------------------


def find_issue_for_finding(
    issues_data: Dict[str, Any], finding_id: str
) -> Optional[Dict[str, Any]]:
    for issue in issues_data.get("issues", []):
        if str(issue.get("finding_id")) == str(finding_id):
            return issue
    return None


def count_failed_fix_attempts(issue: Dict[str, Any]) -> int:
    """Count the number of failed fix verification attempts from issue history."""
    count = 0
    history = issue.get("history", [])
    failed_events = {
        "fix_failed_verification",
        IssueStatus.NEEDS_HUMAN_VALIDATION_FAILED.value,
        IssueStatus.NEEDS_HUMAN_SCOPE_LIMIT_EXCEEDED.value,
        IssueStatus.NEEDS_HUMAN_COMMIT_FAILED.value,
        IssueStatus.NEEDS_HUMAN_PUSH_FAILED.value,
        IssueStatus.NEEDS_HUMAN_MAX_RETRIES_EXCEEDED.value,
    }
    last_open_index = 0
    for idx, entry in enumerate(history):
        if str(entry.get("event", "")).lower() == "open":
            last_open_index = idx

    for entry in history[last_open_index + 1 :]:
        event = str(entry.get("event", "")).lower()
        if event in failed_events or is_needs_human_status(event):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Mutation (moved from engine/orchestrator.py)
# ---------------------------------------------------------------------------


def append_issue_history(
    issue: Dict[str, Any], event: str, detail: Optional[str] = None
) -> None:
    history = issue.setdefault("history", [])
    payload: Dict[str, Any] = {"at": now_iso(), "event": event}
    if detail:
        payload["detail"] = detail
    history.append(payload)


def set_issue_status(
    issue: Dict[str, Any], status: str, detail: Optional[str] = None
) -> None:
    issue["status"] = status
    issue["updated_at"] = now_iso()
    if detail:
        issue["status_detail"] = detail
    append_issue_history(issue, status, detail)


# ---------------------------------------------------------------------------
# Creation (moved from engine/orchestrator.py)
# ---------------------------------------------------------------------------


def create_issues_for_findings(
    issues_data: Dict[str, Any],
    findings: List[Finding],
    confidence_threshold: float,
    max_issues_per_run: int,
    cycle_signals_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Convert qualifying findings into tracked issues, respecting caps and suppressions.

    Args:
        issues_data: Mutable issues dict (``{issues: [...]}``). New issues
            are appended in-place.
        findings: Raw Finding objects from discovery.
        confidence_threshold: Minimum confidence to create an issue.
        max_issues_per_run: Cap on new issues created in one call.
        cycle_signals_path: Optional path to the cycle-signals YAML file
            containing ``suppressed_rules``.

    Returns:
        List of newly created issue dicts (including SUPPRESSED markers).
    """
    existing = {
        str(x.get("finding_id"))
        for x in issues_data.get("issues", [])
        if x.get("finding_id")
    }
    created: List[Dict[str, Any]] = []

    # Load cross-cycle signals to check for suppressed rules
    _cycle_signal_checker = None
    if cycle_signals_path is not None:
        try:
            from pathlib import Path as _Path

            _signal_file = (
                _Path(cycle_signals_path)
                if isinstance(cycle_signals_path, (str, _Path))
                else cycle_signals_path
            )
            if _signal_file.exists():
                _signal_data = json.loads(_signal_file.read_text())
                _suppressed = _signal_data.get("suppressed_rules", {})
                _now = datetime.now(timezone.utc).isoformat()
                _active_suppressions = {
                    r: info
                    for r, info in _suppressed.items()
                    if info.get("expires_at", "") > _now
                }
                _cycle_signal_checker = _active_suppressions
        except (OSError, json.JSONDecodeError):
            _logger.debug("Failed to read cycle signal suppressions")

    for finding in findings:
        if len(created) >= max_issues_per_run:
            break
        if finding.confidence < confidence_threshold:
            continue
        if finding.finding_id in existing:
            continue

        # Cross-cycle suppression check
        if _cycle_signal_checker:
            _global_reason = _cycle_signal_checker.get("__global__")
            if _global_reason:
                created.append(
                    {
                        "issue_id": "SUPPRESSED",
                        "finding_id": finding.finding_id,
                        "rule": finding.rule,
                        "status": "suppressed_cross_cycle",
                        "reason": _global_reason.get("reason", "suppressed"),
                        "created_at": now_iso(),
                    }
                )
                continue
            _rule_reason = _cycle_signal_checker.get(finding.rule)
            if _rule_reason:
                created.append(
                    {
                        "issue_id": "SUPPRESSED",
                        "finding_id": finding.finding_id,
                        "rule": finding.rule,
                        "status": "suppressed_cross_cycle",
                        "reason": _rule_reason.get("reason", "suppressed"),
                        "created_at": now_iso(),
                    }
                )
                continue

        issue_id = f"QA-{len(issues_data['issues']) + len(created) + 1:04d}"
        issue = {
            "issue_id": issue_id,
            "finding_id": finding.finding_id,
            "repo": finding.repo,
            "path": finding.path,
            "line": finding.line,
            "rule": finding.rule,
            "snippet": finding.snippet,
            "confidence": finding.confidence,
            "quick_win": finding.quick_win,
            "safe_to_autofix": finding.safe_to_autofix,
            "status": "open",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "source": "bluei_engine_v2",
            "history": [{"at": now_iso(), "event": "open"}],
        }
        created.append(issue)

    issues_data["issues"].extend(created)
    return created


def ensure_issue_for_finding(
    issues_data: Dict[str, Any],
    finding: Finding,
    confidence_threshold: float,
) -> Optional[Dict[str, Any]]:
    """Return the existing issue for a finding, or create one if it qualifies.

    Args:
        issues_data: Mutable issues dict (``{issues: [...]}``).
        finding: A single Finding to look up or create.
        confidence_threshold: Minimum confidence to create a new issue.

    Returns:
        The matched or newly created issue dict, or None if below threshold.
    """
    existing = find_issue_for_finding(issues_data, finding.finding_id)
    if existing:
        return existing
    if finding.confidence < confidence_threshold:
        return None

    issue_id = f"QA-{len(issues_data['issues']) + 1:04d}"
    issue = {
        "issue_id": issue_id,
        "finding_id": finding.finding_id,
        "repo": finding.repo,
        "path": finding.path,
        "line": finding.line,
        "rule": finding.rule,
        "snippet": finding.snippet,
        "confidence": finding.confidence,
        "quick_win": finding.quick_win,
        "safe_to_autofix": finding.safe_to_autofix,
        "status": "open",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "source": "bluei_engine_v2",
        "history": [{"at": now_iso(), "event": "open"}],
    }
    issues_data["issues"].append(issue)
    return issue


# ---------------------------------------------------------------------------
# Checks (moved from engine/orchestrator.py and engine/escalation.py)
# ---------------------------------------------------------------------------


def check_consecutive_fix_failures(
    issue: Dict[str, Any],
    consecutive_threshold: int = 3,
) -> bool:
    """Check if the last N consecutive fix attempts for this issue all failed.

    Args:
        issue: The issue dict with 'history' list.
        consecutive_threshold: Number of consecutive failures to trigger (default 3).

    Returns:
        True if the last N consecutive attempts were all failures.
    """
    history = issue.get("history", [])
    if not history:
        return False

    consecutive_failures = 0
    for entry in reversed(history):
        event = str(entry.get("event", "")).lower()

        if event in ("resolved_verified", "resolved_merged", "pr_opened"):
            break
        if event == "open":
            break

        if event == "fix_failed_verification" or is_needs_human_status(event):
            consecutive_failures += 1
            if consecutive_failures >= consecutive_threshold:
                return True

    return False


def check_finding_escalation_before_fix(
    issue: Dict[str, Any],
    issues_data: Dict[str, Any],
    consecutive_threshold: int = 3,
    escalation_config: Optional[Any] = None,
    log_file: Optional[Path] = None,
) -> bool:
    """Check if a finding should be escalated (skipped) before a fix attempt.

    Args:
        issue: The issue dict to check.
        issues_data: Full issues data dict (used for context).
        consecutive_threshold: Number of consecutive failures before escalating.
        escalation_config: Optional EscalationConfig for logging.
        log_file: Optional path to the run log.

    Returns:
        True if the finding should be escalated/skipped.
    """
    if check_consecutive_fix_failures(issue, consecutive_threshold):
        finding_id = issue.get("finding_id", "unknown")
        issue_id = issue.get("issue_id", "unknown")
        rule = issue.get("rule", "unknown")
        detail = f"Consecutive fix failures for issue={issue_id} finding={finding_id} rule={rule}"

        if escalation_config is not None:
            try:
                from bluei.engine.escalation import log_escalation_event

                event = {
                    "type": "cycle_escalation",
                    "finding_id": finding_id,
                    "issue_id": issue_id,
                    "rule": rule,
                    "consecutive_failures": consecutive_threshold,
                    "threshold": consecutive_threshold,
                    "cycle_type": "pr-cycle",
                    "detail": detail,
                }
                log_escalation_event(escalation_config, event)
            except Exception:
                logging.debug("escalation event logging failed")
                pass
        if log_file is not None:
            from bluei.engine.state import _append_text

            _append_text(log_file, f"escalation: {detail}")

        return True

    return False


def check_reappearing_findings(
    issues_data: Dict[str, Any],
    thresholds: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Detect findings that keep reappearing after being fixed.

    Checks issue history for findings with >= threshold
    fix_failed_verification or needs-human events.
    """
    from bluei.engine.escalation import REAPPEARING_FINDING_THRESHOLD

    threshold = (thresholds or {}).get(
        "reappearing_finding", REAPPEARING_FINDING_THRESHOLD
    )
    escalated: List[Dict[str, Any]] = []

    for issue in issues_data.get("issues", []):
        finding_id = issue.get("finding_id", "")
        if not finding_id:
            continue
        attempts = count_failed_fix_attempts(issue)
        if attempts >= threshold:
            escalated.append(
                {
                    "type": "reappearing_finding",
                    "finding_id": finding_id,
                    "rule": issue.get("rule", ""),
                    "path": issue.get("path", ""),
                    "failed_attempts": attempts,
                    "threshold": threshold,
                    "detail": f"Finding {finding_id} failed {attempts} times (threshold: {threshold})",
                }
            )

    return escalated
