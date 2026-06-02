"""Verify-only early exit phase handler."""

from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.models import now_iso
from bluei.engine.state import _append_text, save_issues, save_state
from bluei.engine.commands.helpers import update_status_artifact


def run_verify_only(
    *,
    findings: List[Any],
    issues_data: Dict[str, Any],
    issues_file: Path,
    state_file: Path,
    state: Dict[str, Any],
    status_file: Path,
    findings_file: Path,
    log_file: Path,
    args: Any,
    reconcile_event: Dict[str, Any],
    previous_last_run_at: Optional[str],
    open_issues: int,
    open_prs: int,
    written_findings: int,
    suppressed_findings: List[Any],
    blocked_reasons: List[str],
) -> int:
    """Handle --run-phase=verify-only exit."""
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
