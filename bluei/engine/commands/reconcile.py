"""Reconcile-only early exit phase handler."""

from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.state import _append_text, save_state
from bluei.engine.commands.helpers import update_status_artifact


def run_reconcile_only(
    *,
    state_file: Path,
    state: Dict[str, Any],
    status_file: Path,
    issues_file: Path,
    findings_file: Path,
    log_file: Path,
    args: Any,
    reconcile_event: Dict[str, Any],
    previous_last_run_at: Optional[str],
    open_issues: int,
    open_prs: int,
) -> int:
    """Handle --reconcile-only early exit."""
    save_state(state_file, state)
    update_status_artifact(
        status_file=status_file,
        state=state,
        issues_file=issues_file,
        findings_file=findings_file,
        args=args,
        run_mode="RECONCILE-ONLY",
        reconcile_event=reconcile_event,
        previous_last_run_at=previous_last_run_at,
        run_metrics={
            "findings_detected": 0,
            "findings_written": 0,
            "issues_created": 0,
            "fix_attempts": 0,
            "prs_created": 0,
            "fixes_verified": 0,
            "fixes_failed_verification": 0,
            "unresolved_open": int(state.get("open_issues", 0)),
            "findings_suppressed_by_cooldown": 0,
            "issues_escalated_max_retries": 0,
            "merge_attempts": 0,
            "merges_succeeded": 0,
            "merges_failed": 0,
            "merged_pr_urls": [],
            "blocked_events": 0,
            "blocked_reasons": [],
        },
    )
    print(
        f"[DONE] RECONCILE-ONLY source={reconcile_event['reason']} "
        f"open_issues={open_issues} open_prs={open_prs}"
    )
    _append_text(
        log_file,
        f"done: mode=RECONCILE-ONLY open_issues={open_issues} open_prs={open_prs}",
    )
    return 0
