"""Verify-only early exit phase handler."""

from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.models import now_iso
from bluei.engine.state import _append_text, save_issues, save_state
from bluei.engine.commands.helpers import update_status_artifact


def run_verify_only(*args, **kwargs) -> int:
    """Handle --run-phase=verify-only exit."""
    if args:
        ctx = args[0]
        findings = ctx.findings
        issues_data = ctx.issues_data
        issues_file = ctx.issues_file
        state_file = ctx.state_file
        state = ctx.state
        status_file = ctx.status_file
        findings_file = ctx.findings_file
        log_file = ctx.log_file
        args = ctx.args
        reconcile_event = ctx.reconcile_event
        previous_last_run_at = ctx.previous_last_run_at
        open_issues = ctx.open_issues
        open_prs = ctx.open_prs
        written_findings = ctx.written_findings
        suppressed_findings = ctx.suppressed_findings
        blocked_reasons = ctx.blocked_reasons
    else:
        findings = kwargs["findings"]
        issues_data = kwargs["issues_data"]
        issues_file = kwargs["issues_file"]
        state_file = kwargs["state_file"]
        state = kwargs["state"]
        status_file = kwargs["status_file"]
        findings_file = kwargs["findings_file"]
        log_file = kwargs["log_file"]
        args = kwargs["args"]
        reconcile_event = kwargs["reconcile_event"]
        previous_last_run_at = kwargs["previous_last_run_at"]
        open_issues = kwargs["open_issues"]
        open_prs = kwargs["open_prs"]
        written_findings = kwargs["written_findings"]
        suppressed_findings = kwargs["suppressed_findings"]
        blocked_reasons = kwargs["blocked_reasons"]

    from bluei.engine.orchestrator import set_issue_status

    fixes_verified = 0
    fixes_failed_verification = 0

    active_keys = {(f.rule, f.path) for f in findings}
    for issue in issues_data.get("issues", []):
        key = (str(issue.get("rule", "")), str(issue.get("path", "")))
        if key in active_keys:
            set_issue_status(
                issue,
                "fix_failed_verification",
                "detector still firing in verification-only cycle",
            )
            fixes_failed_verification += 1
        else:
            set_issue_status(
                issue,
                "resolved_verified",
                "detector no longer firing in verification-only cycle",
            )
            fixes_verified += 1
    save_issues(issues_file, issues_data)

    unresolved_open = len(
        [
            x
            for x in issues_data.get("issues", [])
            if x.get("status") not in ("resolved_verified", "resolved_merged")
        ]
    )
    state["open_issues"] = open_issues
    state["open_prs"] = open_prs
    state["last_run_at"] = now_iso()
    save_state(state_file, state)
    update_status_artifact(
        status_file=status_file,
        state=state,
        issues_file=issues_file,
        findings_file=findings_file,
        args=args,
        run_mode="VERIFY-ONLY",
        reconcile_event=reconcile_event,
        previous_last_run_at=previous_last_run_at,
        run_metrics={
            "findings_detected": len(findings),
            "findings_written": written_findings,
            "issues_created": 0,
            "fix_attempts": 0,
            "prs_created": 0,
            "fixes_verified": fixes_verified,
            "fixes_failed_verification": fixes_failed_verification,
            "unresolved_open": unresolved_open,
            "findings_suppressed_by_cooldown": len(suppressed_findings),
            "issues_escalated_max_retries": 0,
            "merge_attempts": 0,
            "merges_succeeded": 0,
            "merges_failed": 0,
            "merged_pr_urls": [],
            "blocked_events": len(blocked_reasons),
            "blocked_reasons": blocked_reasons,
        },
    )
    print(
        f"[DONE] VERIFY-ONLY findings={len(findings)} fixes_verified={fixes_verified} "
        f"fixes_failed_verification={fixes_failed_verification}"
    )
    return 0
