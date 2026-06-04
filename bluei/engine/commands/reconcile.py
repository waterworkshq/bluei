"""Reconcile-only early exit phase handler."""

from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.state import _append_text, save_state
from bluei.engine.commands.helpers import update_status_artifact


def run_reconcile_only(*args, **kwargs) -> int:
    """Handle --reconcile-only early exit."""
    if args:
        ctx = args[0]
        state_file = ctx.state_file
        state = ctx.state
        status_file = ctx.status_file
        issues_file = ctx.issues_file
        findings_file = ctx.findings_file
        log_file = ctx.log_file
        args = ctx.args
        reconcile_event = ctx.reconcile_event
        previous_last_run_at = ctx.previous_last_run_at
        open_issues = ctx.open_issues
        open_prs = ctx.open_prs
    else:
        state_file = kwargs["state_file"]
        state = kwargs["state"]
        status_file = kwargs["status_file"]
        issues_file = kwargs["issues_file"]
        findings_file = kwargs["findings_file"]
        log_file = kwargs["log_file"]
        args = kwargs["args"]
        reconcile_event = kwargs["reconcile_event"]
        previous_last_run_at = kwargs["previous_last_run_at"]
        open_issues = kwargs["open_issues"]
        open_prs = kwargs["open_prs"]

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
