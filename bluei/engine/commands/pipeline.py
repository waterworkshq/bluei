"""Pipeline phase handlers for the bluei engine CLI.

Each function handles one phase of the sequential pipeline, receiving shared
state via explicit parameters.  Called from cli.py main() in order.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bluei.engine.constants import AGENT_ROOT
from bluei.engine.gh import (
    evaluate_pr_check_health,
    evaluate_pr_mergeability,
    evaluate_pr_reviews,
    fetch_open_prs_for_merge,
    merge_failure_requires_pr_fix,
    merge_pr,
    repo_is_sandbox,
)
from bluei.engine.models import Finding, FixEngine, now_iso, parse_iso
from bluei.engine.rebase_sweep import sweep_rebase
from bluei.engine.state import (
    _append_text,
    append_findings,
    count_actionable_issues,
    filter_findings_by_cooldown,
    guard_open_issues,
    reconcile_open_workload,
    save_state,
    save_issues,
)
from bluei.engine.commands.helpers import (
    _autonomous_review_gate_passes,
    _load_review_state,
    _triage_pr_back_to_fix_cycle,
    update_status_artifact,
)

logger = logging.getLogger(__name__)


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


DiscoverResult = Tuple[
    List[Finding],
    int,
    List[Finding],
    List[Finding],
    List[Dict[str, Any]],
    List[str],
    bool,
]


def run_discover_phase(
    *,
    repo_path: Path,
    docs_index_file: Path,
    findings_file: Path,
    log_file: Path,
    state: Dict[str, Any],
    issues_data: Dict[str, Any],
    args: Any,
    run_issue_cycle: bool,
    run_pr_cycle: bool,
) -> DiscoverResult:
    """Run the discovery phase: find findings, apply cooldown, route refactors.

    Returns:
        (findings, written_findings, eligible_findings, suppressed_findings,
         refactor_routed_items, blocked_reasons, cap_ok)
    """
    from bluei.engine.orchestrator import discover_findings, route_findings_with_intent
    from bluei.engine.reforge import RefactorClass, classify_finding
    from bluei.engine.utils import is_path_tracked

    findings: List[Finding] = []
    eligible_findings: List[Finding] = []
    suppressed_findings: List[Finding] = []
    refactor_routed_items: List[Dict[str, Any]] = []
    blocked_reasons: List[str] = []
    cap_ok = True

    if run_issue_cycle:
        pre_discovery_actionable = count_actionable_issues(issues_data)
        cap_ok, cap_reason = guard_open_issues(
            pre_discovery_actionable, args.open_issues_cap
        )
        _append_text(log_file, f"pre-discovery-cap-check: {cap_reason}")
        if not cap_ok:
            blocked_reasons.append(cap_reason)

    if args.run_phase in ("verify-only",) or (run_issue_cycle and cap_ok):
        findings = discover_findings(
            repo_path, log_file=log_file, docs_index_file=docs_index_file
        )

    written_findings = append_findings(findings_file, findings)
    _append_text(
        log_file,
        f"discovery: findings_detected={len(findings)} findings_written={written_findings}",
    )
    eligible_findings, suppressed_findings = filter_findings_by_cooldown(
        findings=findings,
        state=state,
        cooldown_seconds=args.finding_cooldown_seconds,
        log_file=log_file,
    )
    if suppressed_findings:
        _append_text(
            log_file,
            f"cooldown-summary: suppressed_findings={len(suppressed_findings)}",
        )

    if args.live_github_actions and run_pr_cycle:
        tracked_findings: List[Finding] = []
        untracked_count = 0
        for finding in eligible_findings:
            if is_path_tracked(repo_path, finding.path):
                tracked_findings.append(finding)
            else:
                untracked_count += 1
                _append_text(
                    log_file,
                    f"discovery-skip: untracked path for live queue path={finding.path} rule={finding.rule}",
                )
        eligible_findings = tracked_findings
        if untracked_count:
            _append_text(
                log_file,
                f"discovery-skip-summary: untracked_findings={untracked_count}",
            )

    refactor_findings: List[Finding] = []
    remaining_findings: List[Finding] = []
    for finding in eligible_findings:
        if classify_finding(finding) == RefactorClass.REFACTOR_CLASS:
            refactor_findings.append(finding)
        else:
            remaining_findings.append(finding)
    if refactor_findings:
        routed = route_findings_with_intent(
            refactor_findings,
            confidence_threshold=args.issue_confidence_threshold,
            findings_file=findings_file,
            worktree_path=repo_path,
            log_file=log_file,
        )
        refactor_routed_items = list(routed.get("refactor_queue", []))
        _append_text(
            log_file,
            f"refactor-routing-summary: routed={len(refactor_routed_items)} skipped={len(routed.get('skipped', []))}",
        )
    eligible_findings = remaining_findings

    return (
        findings,
        written_findings,
        eligible_findings,
        suppressed_findings,
        refactor_routed_items,
        blocked_reasons,
        cap_ok,
    )


IssueCreationResult = Tuple[
    List[Dict[str, Any]],
    int,
    List[str],
]


def run_issue_creation_phase(
    *,
    issues_data: Dict[str, Any],
    eligible_findings: List[Any],
    refactor_routed_items: List[Dict[str, Any]],
    log_file: Path,
    args: Any,
    gh_repo_slug: str,
    open_issues: int,
) -> IssueCreationResult:
    """Create issues for discovered findings.

    Returns:
        (created_issues, open_issues, blocked_reasons)
    """
    from bluei.engine.orchestrator import (
        create_issues_for_findings,
        ensure_issue_for_finding,
        find_issue_for_finding,
        set_issue_status,
    )
    from bluei.engine.state import (
        count_actionable_issues,
        guard_open_issues,
        _append_text,
    )
    from bluei.engine.gh import create_or_update_github_issue, finding_from_issue_record
    from bluei.engine.constants import AGENT_ROOT

    created_issues: List[Dict[str, Any]] = []
    blocked_reasons: List[str] = []

    actionable_issue_count = count_actionable_issues(issues_data)
    _append_text(
        log_file,
        f"actionable-issues: raw_open={open_issues} actionable={actionable_issue_count}",
    )

    for routed_item in refactor_routed_items:
        finding = routed_item["finding"]
        existing_issue = find_issue_for_finding(issues_data, finding.finding_id)
        issue = ensure_issue_for_finding(
            issues_data=issues_data,
            finding=finding,
            confidence_threshold=args.issue_confidence_threshold,
        )
        if issue is None:
            continue
        if existing_issue is None:
            created_issues.append(issue)
            open_issues += 1
        refactor_meta = issue.setdefault("refactor", {})
        refactor_meta["phase"] = routed_item["refactor_work"].phase.value
        if routed_item.get("queued_work_id"):
            refactor_meta["queue_work_id"] = routed_item["queued_work_id"]
        refactor_meta["review_reason"] = routed_item.get("reason", "planning")
        set_issue_status(
            issue,
            "needs-human-refactor-review",
            routed_item.get("reason", "planning"),
        )

    actionable_issue_count = count_actionable_issues(issues_data)

    for _ in range(args.max_issues_per_run):
        ok, reason = guard_open_issues(actionable_issue_count, args.open_issues_cap)
        _append_text(log_file, reason)
        if not ok:
            blocked_reasons.append(reason)
            break

        batch = create_issues_for_findings(
            issues_data=issues_data,
            findings=eligible_findings,
            confidence_threshold=args.issue_confidence_threshold,
            max_issues_per_run=1,
            cycle_signals_path=AGENT_ROOT / "state" / "cycle_signals.json",
        )
        if not batch:
            break

        for issue in batch:
            if issue.get("issue_id") == "SUPPRESSED":
                suppressed_reason = issue.get("reason", "suppressed")
                _append_text(
                    log_file,
                    f"suppressed-cross-cycle: finding={issue['finding_id']} rule={issue.get('rule', '?')} reason={suppressed_reason}",
                )
                continue
            if args.live_github_actions:
                issue_finding = finding_from_issue_record(issue)
                if issue_finding is not None:
                    gh_issue = create_or_update_github_issue(
                        repo_slug=gh_repo_slug,
                        finding=issue_finding,
                        dry_run=args.dry_run,
                        log_file=log_file,
                        cwd=Path(args.repo_path),
                    )
                    issue_github = issue.setdefault("github", {})
                    if gh_issue.get("number") is not None:
                        issue_github["issue_number"] = gh_issue.get("number")
                    if gh_issue.get("url"):
                        issue_github["issue_url"] = gh_issue.get("url")

            created_issues.append(issue)
            open_issues += 1

    return created_issues, open_issues, blocked_reasons


def run_finalize_phase(
    *,
    state_file: Path,
    issues_file: Path,
    status_file: Path,
    findings_file: Path,
    log_file: Path,
    lessons_file: Path,
    repo_path: Path,
    args: Any,
    state: Dict[str, Any],
    issues_data: Dict[str, Any],
    reconcile_event: Dict[str, Any],
    previous_last_run_at: Optional[str],
    open_issues: int,
    open_prs: int,
    findings: List[Any],
    written_findings: int,
    created_issues: List[Any],
    suppressed_findings: List[Any],
    blocked_reasons: List[str],
    fix_attempts: int,
    fixes_verified: int,
    fixes_failed_verification: int,
    created_prs: int,
    issues_escalated_max_retries: int,
    merge_attempts: int,
    merges_succeeded: int,
    merges_failed: int,
    merged_pr_urls: List[str],
    claude_invocations: int,
    opencode_invocations: int,
    deterministic_invocations: int,
    cost_tracker: Any,
    cost_log_path: Path,
    gh_repo_slug: str,
) -> int:
    """Save state, write status artifact, log lessons, run escalation checks."""
    import json

    from bluei.engine.constants import AGENT_ROOT
    from bluei.engine.models import now_iso
    from bluei.engine.state import _append_text, save_issues, save_state
    from bluei.engine.utils import append_lesson
    from bluei.engine.commands.helpers import update_status_artifact

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

    if args.live_github_actions:
        mode = "DRY-RUN-LIVE" if args.dry_run else "ACTIVE-LIVE"
    else:
        mode = "DRY-RUN" if args.dry_run else "ACTIVE-SIM"
    run_mode = f"{mode}-{args.run_phase.upper()}"
    update_status_artifact(
        status_file=status_file,
        state=state,
        issues_file=issues_file,
        findings_file=findings_file,
        args=args,
        run_mode=run_mode,
        reconcile_event=reconcile_event,
        previous_last_run_at=previous_last_run_at,
        run_metrics={
            "findings_detected": len(findings),
            "findings_written": written_findings,
            "issues_created": len(created_issues),
            "fix_attempts": fix_attempts,
            "prs_created": created_prs,
            "fixes_verified": fixes_verified,
            "fixes_failed_verification": fixes_failed_verification,
            "unresolved_open": unresolved_open,
            "findings_suppressed_by_cooldown": len(suppressed_findings),
            "issues_escalated_max_retries": issues_escalated_max_retries,
            "merge_attempts": merge_attempts,
            "merges_succeeded": merges_succeeded,
            "merges_failed": merges_failed,
            "merged_pr_urls": merged_pr_urls,
            "blocked_events": len(blocked_reasons),
            "blocked_reasons": blocked_reasons,
            "claude_invocations": claude_invocations,
            "deterministic_invocations": deterministic_invocations,
            "cost_total_usd": round(cost_tracker.cycle_total(), 6),
            "cost_warned": cost_tracker.warned(),
            "cost_limit_reached": cost_tracker.exceeded_limit(),
        },
    )

    try:
        from bluei.engine.health import enrich_health_with_cost

        if status_file.exists():
            status_data = json.loads(status_file.read_text())
            enrich_health_with_cost(
                status_data,
                cost_log_path=cost_log_path,
                total_runs=max(claude_invocations, 1),
            )
            status_file.write_text(
                json.dumps(status_data, indent=2, sort_keys=True) + "\n"
            )
    except (ImportError, OSError, json.JSONDecodeError):
        logger.debug("Failed to save pipeline status")

    print(
        f"[DONE] {run_mode} findings={len(findings)} issues_created={len(created_issues)} "
        f"fix_attempts={fix_attempts} fixes_verified={fixes_verified} "
        f"fixes_failed_verification={fixes_failed_verification} prs_created={created_prs} "
        f"issues_escalated_max_retries={issues_escalated_max_retries} "
        f"merges={merges_succeeded}/{merge_attempts} "
        f"cost: claude={claude_invocations} deterministic={deterministic_invocations} "
        f"total=${cost_tracker.cycle_total():.4f}"
    )
    _append_text(
        log_file,
        f"done: mode={run_mode} findings={len(findings)} issues={len(created_issues)} "
        f"fix_attempts={fix_attempts} fixes_verified={fixes_verified} "
        f"fixes_failed_verification={fixes_failed_verification} prs={created_prs} "
        f"issues_escalated_max_retries={issues_escalated_max_retries} "
        f"merges={merges_succeeded}/{merge_attempts} "
        f"cost: claude={claude_invocations} deterministic={deterministic_invocations} "
        f"total=${cost_tracker.cycle_total():.4f}",
    )

    if (
        args.run_phase in ("issue-cycle", "pr-cycle", "merge-cycle", "orchestrated")
        and not args.reconcile_only
    ):
        broke_parts: List[str] = []
        changed_parts: List[str] = []
        worked_parts: List[str] = []

        if fixes_failed_verification > 0:
            broke_parts.append(f"{fixes_failed_verification} fixes failed verification")

        if fixes_verified > 0:
            changed_parts.append(f"{fixes_verified} fixes verified")
        if created_prs > 0:
            changed_parts.append(f"{created_prs} PRs created")

        if len(created_issues) > 0:
            worked_parts.append(f"{len(created_issues)} issues flagged")
        if merges_succeeded > 0:
            worked_parts.append(f"{merges_succeeded} merges succeeded")

        has_content = bool(broke_parts or changed_parts or worked_parts)
        if has_content:
            append_lesson(
                lessons_file=lessons_file,
                cycle_type=args.run_phase,
                what_broke="; ".join(broke_parts) if broke_parts else "",
                what_changed="; ".join(changed_parts) if changed_parts else "",
                what_worked="; ".join(worked_parts) if worked_parts else "",
            )

    if args.run_phase in ("merge-cycle", "pr-cycle"):
        from bluei.engine.escalation import (
            EscalationConfig,
            check_cycle_escalation,
            log_escalation_event,
            run_escalation_checks,
        )

        escalation_file = AGENT_ROOT / "state" / "escalation_log.jsonl"
        rebase_stats_file = AGENT_ROOT / "state" / "rebase_stats.jsonl"
        escalation_findings = run_escalation_checks(
            run_log_file=log_file,
            escalation_file=escalation_file,
            issues_data=issues_data,
            merges_failed=merges_failed,
            merges_succeeded=merges_succeeded,
            rebase_stats_file=rebase_stats_file,
            repo_slug=gh_repo_slug,
            cwd=repo_path,
            thresholds={"max_duplicate_prs": args.max_duplicate_prs_threshold},
        )
        if escalation_findings:
            _append_text(
                log_file, f"escalation: {len(escalation_findings)} pattern(s) detected"
            )
            for ef in escalation_findings:
                _append_text(log_file, f"  escalation-{ef['type']}: {ef['detail']}")

            max_dup_escalations = [
                f for f in escalation_findings if f["type"] == "max_duplicate_prs"
            ]
            if max_dup_escalations:
                from bluei.engine.escalation import handle_max_duplicate_escalation

                handle_result = handle_max_duplicate_escalation(
                    escalations=max_dup_escalations,
                    repo_slug=gh_repo_slug,
                    cwd=repo_path,
                    state=state,
                    issues_data=issues_data,
                    log_file=log_file,
                    dry_run=args.dry_run or args.no_auto_close_duplicate_prs,
                )
                _append_text(
                    log_file,
                    f"max-duplicates: closed={len(handle_result['closed_prs'])} "
                    f"close_failed={len(handle_result['close_failed'])} "
                    f"routed={len(handle_result['routed_findings'])} "
                    f"paused={len(handle_result['paused_findings'])}",
                )

        if escalation_findings and not args.dry_run:
            from bluei.engine.notify import deliver_escalations

            notify_results = deliver_escalations(
                escalation_findings=escalation_findings,
                repo_name=gh_repo_slug or repo_path.name,
                workspace=AGENT_ROOT,
            )
            if notify_results:
                _append_text(
                    log_file,
                    f"notifications: {sum(1 for r in notify_results if r.success)} delivered, {sum(1 for r in notify_results if not r.success)} failed",
                )

        _esc_config = EscalationConfig(
            consecutive_failure_threshold=args.max_fix_attempts_per_issue,
            escalation_log_path=escalation_file,
        )
        _cycle_log: List[Dict[str, Any]] = []
        for issue in issues_data.get("issues", []):
            hist = issue.get("history", [])
            if not hist:
                continue
            last_event = hist[-1]
            is_failure = str(last_event.get("event", "")).lower() in (
                "fix_failed_verification",
            ) or str(last_event.get("event", "")).lower().startswith("needs-human")
            is_success = str(last_event.get("event", "")).lower() in (
                "resolved_verified",
                "resolved_merged",
                "pr_opened",
            )
            if is_failure or is_success:
                _cycle_log.append(
                    {
                        "finding_id": issue.get("finding_id", "unknown"),
                        "status": "failure" if is_failure else "success",
                        "cycle_type": args.run_phase,
                        "timestamp": last_event.get("at", now_iso()),
                    }
                )
        _cycle_escalation = check_cycle_escalation(_esc_config, _cycle_log)
        if _cycle_escalation:
            log_escalation_event(_esc_config, _cycle_escalation)
            _append_text(
                log_file,
                f"escalation-cycle: {_cycle_escalation['detail']}",
            )

    return 0


MergeCycleResult = Tuple[
    int,  # merges_failed
    int,  # merges_succeeded
    int,  # merge_attempts
    List[str],  # merged_pr_urls
    int,  # open_prs
    int,  # open_issues
    List[str],  # blocked_reasons
    Dict[str, Any],  # reconcile_event
]


def run_merge_cycle_phase(
    *,
    repo_path: Path,
    log_file: Path,
    review_state_file: Path,
    state: Dict[str, Any],
    issues_data: Dict[str, Any],
    args: Any,
    gh_repo_slug: str,
    merges_failed: int,
    merges_succeeded: int,
    merge_attempts: int,
    merged_pr_urls: List[str],
    open_prs: int,
    open_issues: int,
    blocked_reasons: List[str],
    reconcile_event: Dict[str, Any],
) -> MergeCycleResult:
    """Run the merge cycle: fetch open PRs, evaluate health/reviews/mergeability, merge eligible."""
    from bluei.engine.orchestrator import set_issue_status

    if not args.auto_merge_sandbox:
        reason = "merge-cycle-skip: auto-merge flag not enabled"
        _append_text(log_file, reason)
        blocked_reasons.append(reason)
        return (
            merges_failed,
            merges_succeeded,
            merge_attempts,
            merged_pr_urls,
            open_prs,
            open_issues,
            blocked_reasons,
            reconcile_event,
        )
    if not gh_repo_slug or not repo_is_sandbox(gh_repo_slug):
        reason = f"merge-cycle-block: repo not sandbox ({gh_repo_slug or 'unknown'})"
        _append_text(log_file, reason)
        blocked_reasons.append(reason)
        return (
            merges_failed,
            merges_succeeded,
            merge_attempts,
            merged_pr_urls,
            open_prs,
            open_issues,
            blocked_reasons,
            reconcile_event,
        )

    open_pr_list = fetch_open_prs_for_merge(gh_repo_slug, cwd=repo_path)
    now = datetime.now(timezone.utc)
    cooldown_seconds = args.merge_cooldown_minutes * 60
    for pr in open_pr_list:
        pr_number = int(pr.get("number"))
        pr_url = str(pr.get("url") or "")
        created_at = parse_iso(pr.get("createdAt"))
        age = int((now - created_at).total_seconds()) if created_at else 0

        if bool(pr.get("isDraft")):
            _append_text(log_file, f"merge-skip: pr=#{pr_number} draft=true")
            continue

        if age < cooldown_seconds:
            _append_text(
                log_file,
                f"merge-skip: pr=#{pr_number} cooldown age_seconds={age} required={cooldown_seconds}",
            )
            continue

        check_health = evaluate_pr_check_health(gh_repo_slug, pr_number, cwd=repo_path)
        if not check_health.get("eligible", False):
            merges_failed += 1
            reason = str(check_health.get("reason") or "checks-not-eligible")
            detail = f"merge-block: pr=#{pr_number} reason={reason}"
            blocked_reasons.append(detail)
            _append_text(log_file, detail)
            continue

        review_status = evaluate_pr_reviews(gh_repo_slug, pr_number, cwd=repo_path)
        if not review_status.get("eligible", False):
            review_state = _load_review_state(review_state_file)
            autonomous_ok, autonomous_reason = _autonomous_review_gate_passes(
                review_state, pr_number
            )
            if autonomous_ok:
                _append_text(
                    log_file,
                    f"merge-autonomous-gate-pass: pr=#{pr_number} reason={autonomous_reason}",
                )
            else:
                merges_failed += 1
                reason = str(review_status.get("reason") or "review-not-approved")
                detail = f"merge-block: pr=#{pr_number} reason={reason} autonomous_gate={autonomous_reason}"
                blocked_reasons.append(detail)
                _append_text(log_file, detail)
                continue

        mergeability = evaluate_pr_mergeability(gh_repo_slug, pr_number, cwd=repo_path)
        if not mergeability.get("eligible", False):
            reason = str(mergeability.get("reason") or "merge-state-not-eligible")
            merge_state_status = str(
                mergeability.get("merge_state_status") or ""
            ).upper()
            if merge_state_status == "UNKNOWN" or reason == "merge-state-unknown":
                mergeability = {
                    **mergeability,
                    "eligible": True,
                    "requires_pr_fix": False,
                    "merge_state_status": "UNKNOWN",
                    "reason": "merge-state-unknown-proceed-cautiously",
                }
                _append_text(
                    log_file,
                    f"merge-caution: pr=#{pr_number} normalized legacy unknown merge-state to cautious pass",
                )
            else:
                branch = str(pr.get("headRefName") or "")
                if mergeability.get("requires_pr_fix", False):
                    for issue in issues_data.get("issues", []):
                        issue_github = (
                            issue.get("github", {})
                            if isinstance(issue.get("github"), dict)
                            else {}
                        )
                        if int(issue_github.get("pr_number") or 0) == pr_number:
                            _triage_pr_back_to_fix_cycle(
                                issue=issue,
                                pr_number=pr_number,
                                pr_url=pr_url,
                                branch=branch,
                                reason=reason,
                                log_file=log_file,
                            )
                    _append_text(
                        log_file,
                        f"merge-triaged: pr=#{pr_number} reason={reason}",
                    )
                    continue

                merges_failed += 1
                detail = f"merge-block: pr=#{pr_number} reason={reason}"
                blocked_reasons.append(detail)
                _append_text(log_file, detail)
                continue

        merge_attempts += 1
        if not check_health.get("has_checks", False):
            _append_text(
                log_file,
                f"merge-caution: pr=#{pr_number} no checks found; proceeding",
            )

        if getattr(args, "regression_check", False):
            from bluei.engine.regression import check_regressions
            from bluei.engine.utils import run_no_capture

            head_ref = str(pr.get("headRefName", ""))
            base_ref = str(pr.get("baseRefName", "main"))
            if head_ref:
                _append_text(
                    log_file,
                    f"regression: checking PR #{pr_number} ({head_ref} vs {base_ref})",
                )
                run_no_capture(["git", "fetch", "origin", head_ref], cwd=repo_path)
                regression_findings = check_regressions(
                    repo_path,
                    f"origin/{base_ref}",
                    f"origin/{head_ref}",
                    log_file,
                )
                if regression_findings:
                    detail = f"regression: {len(regression_findings)} finding(s) detected — blocking merge"
                    merges_failed += 1
                    blocked_reasons.append(detail)
                    _append_text(log_file, detail)
                    continue

        merged, merge_reason = merge_pr(
            gh_repo_slug, pr_number, dry_run=args.dry_run, cwd=repo_path
        )
        if merged:
            merges_succeeded += 1
            if pr_url:
                merged_pr_urls.append(pr_url)
            open_prs = max(0, open_prs - 1)
            _append_text(
                log_file,
                f"merge-success: pr=#{pr_number} reason={merge_reason}",
            )

            for issue in issues_data.get("issues", []):
                issue_github = (
                    issue.get("github", {})
                    if isinstance(issue.get("github"), dict)
                    else {}
                )
                if int(issue_github.get("pr_number") or 0) == pr_number:
                    set_issue_status(
                        issue,
                        "resolved_merged",
                        f"PR merged: {pr_url or pr_number}",
                    )

            if args.auto_rebase_enabled:
                _append_text(log_file, "auto-rebase: starting sibling sweep")
                try:
                    base_branch = str(pr.get("baseRefName", "main"))
                    rebase_stats_file = getattr(args, "rebase_stats_file", None) or (
                        AGENT_ROOT / "state" / "rebase_stats.jsonl"
                    )
                    rebase_result = sweep_rebase(
                        repo_path=repo_path,
                        gh_repo_slug=gh_repo_slug,
                        merged_pr_number=pr_number,
                        base_branch=base_branch,
                        log_file=log_file,
                        dry_run=args.dry_run,
                        max_prs=args.rebase_max_prs,
                        rebase_stats_file=rebase_stats_file,
                    )
                    for r in rebase_result.get("rebased", []):
                        _append_text(log_file, f"rebase-ok: pr=#{r['pr_number']}")
                    for c in rebase_result.get("conflicted", []):
                        _append_text(
                            log_file,
                            f"rebase-conflict: pr=#{c['pr_number']} files={c['files']}",
                        )
                        blocked_reasons.append(
                            f"rebase-conflict: pr=#{c['pr_number']} files={', '.join(c['files'])}"
                        )
                    for s in rebase_result.get("skipped", []):
                        _append_text(
                            log_file,
                            f"rebase-skip: pr=#{s['pr_number']} reason={s['reason']}",
                        )
                except Exception as exc:
                    _append_text(log_file, f"auto-rebase-error: {exc}")
            break
        else:
            branch = str(pr.get("headRefName") or "")
            if merge_failure_requires_pr_fix(merge_reason):
                for issue in issues_data.get("issues", []):
                    issue_github = (
                        issue.get("github", {})
                        if isinstance(issue.get("github"), dict)
                        else {}
                    )
                    if int(issue_github.get("pr_number") or 0) == pr_number:
                        _triage_pr_back_to_fix_cycle(
                            issue=issue,
                            pr_number=pr_number,
                            pr_url=pr_url,
                            branch=branch,
                            reason=merge_reason,
                            log_file=log_file,
                        )
                _append_text(
                    log_file,
                    f"merge-triaged: pr=#{pr_number} reason={merge_reason}",
                )
                continue

            merges_failed += 1
            detail = f"merge-failed: pr=#{pr_number} reason={merge_reason}"
            blocked_reasons.append(detail)
            _append_text(log_file, detail)

    if args.live_github_actions and not args.dry_run:
        open_issues, open_prs, reconcile_event = reconcile_open_workload(
            repo_path=repo_path,
            state=state,
            log_file=log_file,
            simulate_open_issues=args.simulate_open_issues,
            simulate_open_prs=args.simulate_open_prs,
        )

    return (
        merges_failed,
        merges_succeeded,
        merge_attempts,
        merged_pr_urls,
        open_prs,
        open_issues,
        blocked_reasons,
        reconcile_event,
    )


def run_pr_cycle_phase(
    *,
    repo_path: Path,
    findings_file: Path,
    log_file: Path,
    worktree_root: Path,
    gh_repo_slug: str,
    review_state_file: Path,
    docs_index_file: Path,
    lessons_file: Path,
    args: Any,
    state: Dict[str, Any],
    issues_data: Dict[str, Any],
    eligible_findings: List[Any],
    findings: List[Any],
    PER_REPO_BASELINE_CHECKS: Dict[str, List[str]],
    cost_tracker: Any,
    pattern_store: Any,
    # Counters (in/out)
    created_prs: int,
    open_prs: int,
    fix_attempts: int,
    fixes_verified: int,
    fixes_failed_verification: int,
    issues_escalated_max_retries: int,
    claude_invocations: int,
    deterministic_invocations: int,
    blocked_reasons: List[str],
) -> Dict[str, Any]:
    """Run the PR cycle: queue candidates, apply fixes, create PRs."""

    from bluei.engine.state import (
        NON_ACTIONABLE_ISSUE_STATUSES,
        _append_text,
        guard_open_prs,
        increment_fix_attempt,
        mark_finding_activity,
        save_batch_record,
    )
    from bluei.engine.gh import (
        create_or_update_github_issue,
        create_or_update_github_pr,
        find_existing_github_pr,
        finding_from_issue_record,
        gh_issue_close,
        gh_issue_comment,
        gh_pr_comment,
    )
    from bluei.engine.orchestrator import (
        check_finding_escalation_before_fix,
        classify_finding,
        count_failed_fix_attempts,
        route_findings_with_intent,
        set_issue_status,
    )
    from bluei.engine.git_ops import diff_stats, git_commit_all, git_push_branch
    from bluei.engine.lifecycle import (
        apply_autofix,
        apply_claude_fix,
    )
    from bluei.engine.validation import (
        build_target_checks,
        choose_validation_baseline,
        run_named_checks,
        run_validation_gate,
        verify_fix_closed,
    )
    from bluei.engine.utils import (
        branch_suffix,
        is_path_tracked,
        run_capture,
        run_no_capture,
    )
    from bluei.engine.reforge import RefactorClass
    from bluei.engine.models import Finding, now_iso
    from bluei.engine.git_utils import get_branch
    from bluei.engine.pattern_replay import try_replay
    from bluei.engine.constants import (
        AGENT_ROOT,
        BASELINE_VALIDATION_CHECKS,
        CLAUDE_REQUIRED_RULES,
        DEFAULT_BATCH_STATE,
    )
    from bluei.engine.commands.helpers import (
        _get_llm_fixable_rules,
        _hydrate_worktree_dependencies,
        _load_batch_rules_for_args,
        _reconcile_issue_pr_link,
    )

    queue_candidates: List[Tuple[Dict[str, Any], Finding]] = []
    for issue in issues_data.get("issues", []):
        if issue.get("status") in ("resolved_merged",):
            continue
        issue_github = (
            issue.get("github", {}) if isinstance(issue.get("github"), dict) else {}
        )
        if issue_github.get("pr_number") or issue_github.get("pr_url"):
            if not args.live_github_actions:
                continue
            if _reconcile_issue_pr_link(
                issue=issue,
                repo_slug=gh_repo_slug,
                repo_path=repo_path,
                log_file=log_file,
            ):
                continue
        finding = finding_from_issue_record(issue)
        if finding is None:
            continue
        if issue.get("status") in NON_ACTIONABLE_ISSUE_STATUSES:
            continue

        if args.live_github_actions and not is_path_tracked(repo_path, finding.path):
            set_issue_status(
                issue,
                "blocked_untracked_path",
                f"path not tracked in git HEAD: {finding.path}",
            )
            continue
        finding_class = classify_finding(finding)
        if finding_class == RefactorClass.REFACTOR_CLASS:
            routed = route_findings_with_intent(
                [finding],
                confidence_threshold=args.issue_confidence_threshold,
                findings_file=findings_file,
                worktree_path=repo_path,
                log_file=log_file,
            )
            refactor_item = (routed.get("refactor_queue") or [{}])[0]
            refactor_meta = issue.setdefault("refactor", {})
            if refactor_item.get("queued_work_id"):
                refactor_meta["queue_work_id"] = refactor_item["queued_work_id"]
            if refactor_item.get("refactor_work") is not None:
                refactor_meta["phase"] = refactor_item["refactor_work"].phase.value
            refactor_meta["review_reason"] = refactor_item.get("reason", "planning")
            if issue.get("status") != "needs-human-refactor-review":
                set_issue_status(
                    issue,
                    "needs-human-refactor-review",
                    refactor_item.get("reason", "planning"),
                )
            _append_text(
                log_file,
                f"pr-cycle: routed structural refactor issue={issue.get('issue_id')} finding_id={finding.finding_id} to refactor review lane",
            )
            continue

        if not finding.safe_to_autofix:
            llm_rules = _get_llm_fixable_rules()
            if finding.rule in llm_rules:
                # Rule is LLM-fixable — route to fix engine, don't skip
                pass
            elif classify_finding(finding) == RefactorClass.CONTEXTUAL_FIX:
                # Contextual fix engine can handle this — route to fix engine
                pass
            else:
                # Truly not fixable — mark for human triage
                if issue.get("status") != "needs-human-not-fixable":
                    set_issue_status(
                        issue,
                        "needs-human-not-fixable",
                        f"rule {finding.rule} is not autofixable and not LLM-fixable",
                    )
                    _append_text(
                        log_file,
                        f"skip: issue={issue.get('issue_id')} rule={finding.rule} "
                        f"not autofixable and not in LLM_FIXABLE_RULES",
                    )
                continue
        if finding.confidence < args.issue_confidence_threshold:
            continue

        # Wave 3.8: Check for consecutive fix failures before attempting
        _consec_escalated = check_finding_escalation_before_fix(
            issue=issue,
            issues_data=issues_data,
            consecutive_threshold=args.max_fix_attempts_per_issue,
            escalation_config=None,  # logged via existing escalation pipeline below
            log_file=log_file,
        )
        if _consec_escalated:
            if issue.get("status") != "needs-human-max-retries-exceeded":
                _failed = count_failed_fix_attempts(issue)
                set_issue_status(
                    issue,
                    "needs-human-max-retries-exceeded",
                    f"escalated: {_failed} consecutive fix failures (threshold: {args.max_fix_attempts_per_issue})",
                )
                issues_escalated_max_retries += 1
            continue

        # P0 Fix #1: Check if issue has exceeded max fix attempts
        failed_attempts = count_failed_fix_attempts(issue)
        if failed_attempts >= args.max_fix_attempts_per_issue:
            if issue.get("status") != "needs-human-max-retries-exceeded":
                set_issue_status(
                    issue,
                    "needs-human-max-retries-exceeded",
                    f"exceeded max fix attempts ({failed_attempts}/{args.max_fix_attempts_per_issue})",
                )
                issues_escalated_max_retries += 1
                _append_text(
                    log_file,
                    f"escalation: issue={issue.get('issue_id')} finding_id={finding.finding_id} "
                    f"exceeded max_fix_attempts_per_issue ({failed_attempts}/{args.max_fix_attempts_per_issue}) "
                    f"-> marking as needs-human-max-retries-exceeded",
                )
            continue

        queue_candidates.append((issue, finding))

    if not queue_candidates:
        _append_text(log_file, "pr-cycle: no eligible issue-queue items for autofix")

    baseline_results: Dict[str, Dict[str, Any]] = {}
    if queue_candidates:
        baseline_results = run_named_checks(
            repo_path=repo_path,
            checks=PER_REPO_BASELINE_CHECKS,
            log_file=log_file,
            phase="baseline-main",
        )
        baseline_failures = [
            name
            for name, result in baseline_results.items()
            if int(result.get("rc", 1)) != 0
        ]
        if baseline_failures:
            _append_text(
                log_file,
                f"baseline-main: failing_checks={','.join(baseline_failures)}",
            )
        else:
            _append_text(log_file, "baseline-main: all checks passing")

    current_branch = get_branch(repo_path)
    if current_branch == "main" and not args.allow_main_commit:
        _append_text(
            log_file,
            "safety: main branch direct commit blocked; using isolated worktree branch",
        )

    # Build the iteration list: batch mode groups findings, non-batch iterates directly
    if getattr(args, "batch_pr_enabled", False) and queue_candidates:
        from bluei.engine.batch_pr import (
            group_findings_for_batch,
            process_batch as _process_batch,
        )
        from bluei.engine.state import save_batch_record as _save_batch_record

        _batch_rules = _load_batch_rules_for_args(args)
        _batch_groups = group_findings_for_batch(queue_candidates, _batch_rules)
        _append_text(
            log_file,
            f"batch-cycle: {len(_batch_groups)} batch groups from {len(queue_candidates)} candidates",
        )

        # Pre-process multi-finding batches, collect solo items for single-finding path
        _solo_items = []  # list of (issue, finding)
        for _bg in _batch_groups:
            if _bg.is_solo:
                _solo_items.append((_bg.issues[0], _bg.findings[0]))
            else:
                # Process multi-finding batch inline
                if created_prs >= args.max_prs_per_run:
                    break
                ok, reason = guard_open_prs(open_prs, args.open_prs_cap)
                _append_text(log_file, reason)
                if not ok:
                    blocked_reasons.append(reason)
                    break
                _success, _detail = _process_batch(
                    batch=_bg,
                    repo_path=repo_path,
                    args=args,
                    log_file=log_file,
                )
                if _success:
                    created_prs += 1
                    open_prs += 1
                    _bsf = Path(
                        getattr(args, "batch_state_file", str(DEFAULT_BATCH_STATE))
                    )
                    _save_batch_record(_bsf, _bg.to_record())
                    _append_text(log_file, f"batch-cycle: {_bg.batch_id} -> {_detail}")
                else:
                    _append_text(
                        log_file, f"batch-cycle: {_bg.batch_id} failed: {_detail}"
                    )

        # Solo items use the same single-finding path as non-batch mode
        _iteration_items = _solo_items
    else:
        _iteration_items = queue_candidates

    # ── Single-finding loop (shared by batch-solo and non-batch paths) ──
    for idx, (issue, finding) in enumerate(_iteration_items, start=1):
        if created_prs >= args.max_prs_per_run:
            break

        ok, reason = guard_open_prs(open_prs, args.open_prs_cap)
        _append_text(log_file, reason)
        if not ok:
            blocked_reasons.append(reason)
            break

        issue_github = issue.setdefault("github", {})
        issue_number: Optional[int] = issue_github.get("issue_number")
        issue_url: str = str(issue_github.get("issue_url") or "")

        if args.live_github_actions and issue_number is None:
            gh_issue = create_or_update_github_issue(
                repo_slug=gh_repo_slug,
                finding=finding,
                dry_run=args.dry_run,
                log_file=log_file,
                cwd=repo_path,
            )
            issue_number = (
                gh_issue.get("number")
                if gh_issue.get("number") is not None
                else issue_number
            )
            issue_url = str(gh_issue.get("url") or issue_url)
            if issue_number is not None:
                issue_github["issue_number"] = issue_number
            if issue_url:
                issue_github["issue_url"] = issue_url

        existing_pr_for_repair: Optional[Dict[str, Any]] = None
        if args.live_github_actions:
            existing_pr = find_existing_github_pr(
                gh_repo_slug, finding.finding_id, cwd=repo_path
            )
            if existing_pr and str(existing_pr.get("state") or "").upper() == "OPEN":
                pr_number = int(existing_pr["number"])
                pr_url = str(existing_pr.get("url") or "")
                issue_github["pr_number"] = pr_number
                issue_github["pr_url"] = pr_url
                issue_github["branch"] = str(
                    existing_pr.get("headRefName") or issue_github.get("branch") or ""
                )
                if issue.get("status") == "pr_merge_conflict":
                    existing_pr_for_repair = existing_pr
                    _append_text(
                        log_file,
                        f"pr-cycle: resuming existing PR #{pr_number} for merge-conflict repair",
                    )
                else:
                    if issue_number is not None and not args.dry_run:
                        gh_issue_comment(
                            gh_repo_slug,
                            issue_number,
                            f"Existing PR already open for this finding: {pr_url}",
                            cwd=repo_path,
                        )
                    set_issue_status(
                        issue,
                        "pr_opened",
                        "existing live PR already present for finding",
                    )
                    mark_finding_activity(
                        state=state,
                        finding_ids=[finding.finding_id],
                        action="pr-open-existing",
                    )
                    continue
            elif existing_pr:
                _append_text(
                    log_file,
                    f"pr-cycle: ignoring closed linked PR #{existing_pr.get('number')} for finding={finding.finding_id}",
                )

        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        finding_suffix = finding.finding_id[:8]
        if args.live_github_actions:
            worktree_branch = str(
                (existing_pr_for_repair or {}).get("headRefName")
                or issue_github.get("branch")
                or f"qa/live-{branch_suffix(finding.rule)}-{finding_suffix}"
            )
        else:
            worktree_branch = f"bluei/{ts}-{idx}"
        # Use an absolute, finding-specific path so stale interrupted runs are less likely to collide.
        worktree_path = worktree_root.resolve() / f"bluei-{ts}-{idx}-{finding_suffix}"

        # Best-effort cleanup of stale worktree metadata/path before creating the next isolated worktree.
        run_no_capture(["git", "worktree", "prune"], cwd=repo_path)
        if worktree_path.exists():
            run_no_capture(["rm", "-rf", str(worktree_path)], cwd=repo_path)

        add_rc, add_out = run_capture(
            ["git", "worktree", "add", "-B", worktree_branch, str(worktree_path)],
            cwd=repo_path,
        )
        if add_rc != 0:
            blocked_reasons.append("failed-to-create-worktree")
            _append_text(
                log_file,
                f"error: failed to create worktree output={(add_out or '<empty>')[:300]}",
            )
            break

        _hydrate_worktree_dependencies(
            repo_path=repo_path, worktree_path=worktree_path, log_file=log_file
        )

        run_status = "unknown"
        try:
            fix_attempts += 1
            set_issue_status(issue, "fix_attempted", "starting sandbox autofix attempt")

            worktree_baseline_results = run_named_checks(
                repo_path=worktree_path,
                checks=PER_REPO_BASELINE_CHECKS,
                log_file=log_file,
                phase="worktree-baseline",
            )

            target_checks = build_target_checks(finding)

            # Try pattern replay before standard fix path
            replay_succeeded = False
            replay_pattern_hint: Optional[str] = None

            if pattern_store is not None:
                replayed, replay_pid = try_replay(
                    worktree_path=worktree_path,
                    finding=finding,
                    store=pattern_store,
                    baseline_checks=PER_REPO_BASELINE_CHECKS,
                    log_file=log_file,
                )
                if replayed:
                    replay_succeeded = True
                    _append_text(
                        log_file,
                        f"pattern-replay-savings: pattern_id={replay_pid} rule={finding.rule}",
                    )
                    saved = cost_tracker.estimate_invocation_cost(
                        "claude-sonnet-4",
                        input_tokens=4000,
                        output_tokens=2000,
                    )
                    cost_tracker.record_pattern_replay_savings(
                        model="claude-sonnet-4",
                        saved_cost=saved,
                        pattern_id=replay_pid or "",
                        rule=finding.rule,
                    )
                elif replay_pid is not None:
                    from bluei.engine.pattern_replay import format_pattern_hint

                    pattern = pattern_store.get_pattern(replay_pid)
                    if pattern is not None:
                        replay_pattern_hint = format_pattern_hint(pattern)

            if not replay_succeeded:
                # Route CASCADE_FIX through the multi-engine cascade
                if getattr(finding, "refactor_class", "") == "cascade_fix":
                    from bluei.engine.lifecycle import apply_cascade_fix

                    applied = apply_cascade_fix(
                        worktree_path,
                        finding,
                        log_file,
                        pattern_store_path=Path(args.pattern_store_path)
                        if args.pattern_store_path
                        else None,
                        deterministic_only=args.deterministic_only,
                    )
                    if applied:
                        run_status = "fix-applied:cascade"
                        deterministic_invocations += 1
                        _append_text(
                            log_file,
                            f"cascade-fix: succeeded finding_id={finding.finding_id} rule={finding.rule}",
                        )
                    else:
                        run_status = "fix-noop:cascade-exhausted"
                        fixes_failed_verification += 1
                        _append_text(
                            log_file,
                            f"cascade-fix: exhausted finding_id={finding.finding_id} rule={finding.rule}",
                        )
                        if finding.finding_id:
                            increment_fix_attempt(
                                finding.finding_id,
                                findings_file,
                                f"cascade exhausted rule={finding.rule}",
                            )
                    if applied:
                        pass  # fall through to diff stats
                    else:
                        continue
                else:
                    # --- Fix engine (backend) resolution ---
                    # Fallback chain:
                    #   1. CLAUDE_REQUIRED_RULES → always use Claude regardless of --fix-engine
                    #   2. LLM-fixable rules (not safe_to_autofix but in LLM_FIXABLE_RULES) → Claude
                    #   3. --fix-engine=claude → Claude
                    #   4. Everything else → deterministic autofix (with contextual-fix fallback)
                    llm_rules = _get_llm_fixable_rules()
                    is_llm_fixable = (
                        not finding.safe_to_autofix and finding.rule in llm_rules
                    )
                    use_claude_engine = (
                        args.fix_engine == FixEngine.CLAUDE.value
                        or finding.rule in CLAUDE_REQUIRED_RULES
                        or is_llm_fixable
                    )
                # Track model invocation for cost tracking
                if use_claude_engine:
                    claude_invocations += 1
                else:
                    deterministic_invocations += 1
                # Store prompt hint for LLM-fixable rules
                extra_prompt = (
                    llm_rules.get(finding.rule, {}).get("prompt_hint")
                    if is_llm_fixable
                    else None
                )
                # Keep replay pattern hint separate from rule prompt hint
                # (they are semantically different: rule hint is operator-authored,
                #  replay hint comes from format_pattern_hint())
                learned_patterns = replay_pattern_hint

                # Cost-limit gate: skip Claude invocation if hard limit exceeded
                if use_claude_engine and cost_tracker.exceeded_limit():
                    _append_text(
                        log_file,
                        f"cost-limit: skipping claude fix for {finding.finding_id} "
                        f"({finding.rule}) — hard limit (${cost_tracker.cycle_total():.2f}) reached",
                    )
                    run_status = "fix-skipped:cost-limit-reached"
                    set_issue_status(issue, "fix_skipped", run_status)
                    fixes_failed_verification += 1
                    continue

                if use_claude_engine:
                    rc, claude_output, prompt_file = apply_claude_fix(
                        worktree_path=worktree_path,
                        finding=finding,
                        baseline_checks=BASELINE_VALIDATION_CHECKS,
                        target_checks=target_checks,
                        claude_cmd_template=args.claude_cmd_template,
                        max_files_changed=args.max_files_changed,
                        max_loc_diff=args.max_loc_diff,
                        log_file=log_file,
                        findings_file=findings_file,
                        lessons_file=lessons_file,
                        repo_path=repo_path,
                        extra_prompt=extra_prompt,
                        pattern_store_path=Path(args.pattern_store_path)
                        if args.pattern_store_path
                        else None,
                        learned_patterns=learned_patterns,
                    )
                    # Record cost — use default estimate if actual token counts unavailable
                    model_name = "claude-sonnet-4"
                    cost_tracker.record_invocation(
                        model=model_name,
                        input_tokens=3000,
                        output_tokens=300,
                    )
                    if rc != 0:
                        run_status = "fix-failed-verification:claude-command-failed"
                        set_issue_status(issue, "fix_failed_verification", run_status)
                        fixes_failed_verification += 1
                        if (
                            args.live_github_actions
                            and issue_number is not None
                            and not args.dry_run
                        ):
                            gh_issue_comment(
                                gh_repo_slug,
                                issue_number,
                                (
                                    "Claude autofix command failed "
                                    f"(rc={rc}) for {finding.rule} in {finding.path}:{finding.line}. "
                                    f"Prompt: {prompt_file}. Output: {(claude_output or '<empty>')[:300]}"
                                ),
                                cwd=repo_path,
                            )
                        current_entry = state.get("finding_activity", {}).get(
                            finding.finding_id, {}
                        )
                        current_failures = current_entry.get("failure_count", 0)
                        mark_finding_activity(
                            state=state,
                            finding_ids=[finding.finding_id],
                            action="fix-failed-verification",
                            failure_count=current_failures + 1,
                            last_error=f"claude rc={rc}",
                        )
                        continue
                else:
                    applied = apply_autofix(
                        worktree_path,
                        finding,
                        log_file,
                        pattern_store_path=Path(args.pattern_store_path)
                        if args.pattern_store_path
                        else None,
                    )
                    if not applied:
                        # Try contextual fix engine before giving up
                        from bluei.engine.context_fix import (
                            apply_contextual_fix,
                            record_context_failure,
                        )

                        _append_text(
                            log_file,
                            f"contextual-fix: attempting for rule={finding.rule} path={finding.path}",
                        )
                        applied = apply_contextual_fix(
                            repo_path=repo_path,
                            finding=finding,
                            log_file=log_file,
                            worktree_path=worktree_path,
                        )
                        if not applied:
                            from bluei.engine.context_fix import (
                                update_context_rule_on_repeated_failure,
                            )
                            from bluei.engine.reforge import (
                                get_context_rule,
                                match_context as _match_context,
                            )

                            failures_path = (
                                Path(args.workspace or ".")
                                / "state"
                                / "context_failures.jsonl"
                            )
                            ctx_rule = get_context_rule(finding.rule)
                            matched_ctx = (
                                _match_context(finding.path, ctx_rule)
                                if ctx_rule
                                else None
                            )
                            fw = matched_ctx.framework if matched_ctx else ""
                            strategy = (
                                matched_ctx.fix_strategy
                                if matched_ctx
                                else "contextual"
                            )
                            record_context_failure(
                                failures_path,
                                finding.rule,
                                finding.path,
                                fw,
                                strategy,
                            )
                            if ctx_rule and matched_ctx:
                                mutated = update_context_rule_on_repeated_failure(
                                    finding.rule, fw, failures_path
                                )
                                if mutated:
                                    _append_text(
                                        log_file,
                                        f"context-learning: rule={finding.rule} context={fw} auto-updated to skip",
                                    )
                    if not applied:
                        run_status = "fix-noop"
                        set_issue_status(
                            issue,
                            "fix_failed_verification",
                            "autofix could not modify target pattern",
                        )
                        fixes_failed_verification += 1
                        if finding.finding_id:
                            increment_fix_attempt(
                                finding.finding_id,
                                findings_file,
                                f"autofix no-op for rule={finding.rule}",
                            )
                        if (
                            args.live_github_actions
                            and issue_number is not None
                            and not args.dry_run
                        ):
                            gh_issue_comment(
                                gh_repo_slug,
                                issue_number,
                                f"Autofix could not update pattern for {finding.rule} in {finding.path}:{finding.line}.",
                                cwd=repo_path,
                            )
                        current_entry = state.get("finding_activity", {}).get(
                            finding.finding_id, {}
                        )
                        current_failures = current_entry.get("failure_count", 0)
                        mark_finding_activity(
                            state=state,
                            finding_ids=[finding.finding_id],
                            action="fix-failed-verification",
                            failure_count=current_failures + 1,
                            last_error=f"autofix no-op rule={finding.rule}",
                        )
                        continue

            files_changed, loc_diff = diff_stats(worktree_path)
            _append_text(
                log_file,
                f"fix-scope-stats: files_changed={files_changed} loc_diff={loc_diff}",
            )

            if files_changed == 0 and loc_diff == 0:
                verified_without_changes = verify_fix_closed(
                    worktree_path,
                    finding,
                    log_file,
                    docs_index_file=docs_index_file,
                )
                if verified_without_changes:
                    set_issue_status(
                        issue,
                        "resolved_verified",
                        "finding already closed on branch; no code change needed",
                    )
                    fixes_verified += 1
                    if (
                        args.live_github_actions
                        and issue_number is not None
                        and not args.dry_run
                    ):
                        gh_issue_comment(
                            gh_repo_slug,
                            issue_number,
                            "Finding no longer reproduces on the current branch. Closing without a new PR.",
                            cwd=repo_path,
                        )
                        gh_issue_close(
                            gh_repo_slug,
                            issue_number,
                            "Resolved by existing branch state; no additional change required.",
                            cwd=repo_path,
                        )
                    mark_finding_activity(
                        state=state,
                        finding_ids=[finding.finding_id],
                        action="resolved-noop-verified",
                    )
                    continue

            if files_changed > args.max_files_changed or loc_diff > args.max_loc_diff:
                run_status = "needs-human-scope-limit-exceeded"
                blocked_reasons.append(run_status)
                set_issue_status(issue, "fix_failed_verification", run_status)
                fixes_failed_verification += 1
                if (
                    args.live_github_actions
                    and issue_number is not None
                    and not args.dry_run
                ):
                    gh_issue_comment(
                        gh_repo_slug,
                        issue_number,
                        f"Fix exceeded scope limits (files={files_changed}, loc={loc_diff}); needs human follow-up.",
                        cwd=repo_path,
                    )
                break

            post_fix_results = run_named_checks(
                repo_path=worktree_path,
                checks=PER_REPO_BASELINE_CHECKS,
                log_file=log_file,
                phase="post-fix",
            )
            target_results = (
                run_named_checks(
                    repo_path=worktree_path,
                    checks=target_checks,
                    log_file=log_file,
                    phase="target-check",
                )
                if target_checks
                else {}
            )

            validation_result = run_validation_gate(
                repo_path=worktree_path,
                worktree_path=worktree_path,
                checks={},
                baseline_results=choose_validation_baseline(
                    repo_baseline_results=baseline_results,
                    worktree_baseline_results=worktree_baseline_results,
                    log_file=log_file,
                ),
                post_fix_results=post_fix_results,
                target_results=target_results,
                allow_unchanged_baseline_failures=args.allow_unchanged_baseline_failures,
                log_file=log_file,
            )
            checks_ok = validation_result.get("passed", False)
            validation_reason = validation_result.get("message", "")
            if not checks_ok:
                run_status = f"needs-human-validation-failed:{validation_reason}"
                blocked_reasons.append(run_status)
                set_issue_status(issue, "fix_failed_verification", run_status)
                fixes_failed_verification += 1
                if (
                    args.live_github_actions
                    and issue_number is not None
                    and not args.dry_run
                ):
                    gh_issue_comment(
                        gh_repo_slug,
                        issue_number,
                        f"Validation gate failed after autofix ({validation_reason}); keeping issue open for manual intervention.",
                        cwd=repo_path,
                    )
                break

            verified = verify_fix_closed(
                worktree_path, finding, log_file, docs_index_file=docs_index_file
            )
            if not verified:
                run_status = "fix-failed-verification"
                set_issue_status(
                    issue,
                    "fix_failed_verification",
                    "detector still firing after fix + validation",
                )
                fixes_failed_verification += 1
                if (
                    args.live_github_actions
                    and issue_number is not None
                    and not args.dry_run
                ):
                    gh_issue_comment(
                        gh_repo_slug,
                        issue_number,
                        "Post-fix verification failed: detector still firing.",
                        cwd=repo_path,
                    )
                current_entry = state.get("finding_activity", {}).get(
                    finding.finding_id, {}
                )
                current_failures = current_entry.get("failure_count", 0)
                mark_finding_activity(
                    state=state,
                    finding_ids=[finding.finding_id],
                    action="fix-failed-verification",
                    failure_count=current_failures + 1,
                    last_error="detector still firing after fix",
                )
                continue

            set_issue_status(
                issue,
                "resolved_verified",
                "detector no longer firing after fix + validation",
            )
            fixes_verified += 1

            pr_number: Optional[int] = None
            pr_url = ""
            if args.live_github_actions:
                commit_message = (
                    f"fix(bluei): {finding.rule} [{finding.finding_id[:8]}]"
                )
                commit_result = git_commit_all(
                    worktree_path,
                    commit_message,
                    log_file=log_file,
                    dry_run=args.dry_run,
                )
                if commit_result == "no_changes":
                    run_status = "resolved-verified-noop"
                    set_issue_status(
                        issue,
                        "resolved_verified",
                        "detector no longer firing and no repo diff remained to commit",
                    )
                    mark_finding_activity(
                        state=state,
                        finding_ids=[finding.finding_id],
                        action="resolved-verified-noop",
                        failure_count=0,
                        last_error=None,
                    )
                    if (
                        args.live_github_actions
                        and issue_number is not None
                        and not args.dry_run
                    ):
                        gh_issue_comment(
                            gh_repo_slug,
                            issue_number,
                            "Post-fix verification passed and the effective fix was already present, so no new commit/PR was needed.",
                            cwd=repo_path,
                        )
                    continue
                if commit_result != "committed":
                    run_status = "needs-human-commit-failed"
                    blocked_reasons.append(run_status)
                    set_issue_status(issue, "fix_failed_verification", run_status)
                    fixes_failed_verification += 1
                    break

                pushed = git_push_branch(
                    worktree_path,
                    worktree_branch,
                    log_file=log_file,
                    dry_run=args.dry_run,
                )
                if not pushed:
                    run_status = "needs-human-push-failed"
                    blocked_reasons.append(run_status)
                    set_issue_status(issue, "fix_failed_verification", run_status)
                    fixes_failed_verification += 1
                    break

                pr_result = create_or_update_github_pr(
                    repo_slug=gh_repo_slug,
                    finding=finding,
                    branch=worktree_branch,
                    issue_number=issue_number,
                    dry_run=args.dry_run,
                    log_file=log_file,
                    cwd=worktree_path,
                )
                pr_number = (
                    pr_result.get("number")
                    if pr_result.get("number") is not None
                    else None
                )
                pr_url = str(pr_result.get("url") or "")
                if pr_number is not None:
                    issue_github["pr_number"] = pr_number
                if pr_url:
                    issue_github["pr_url"] = pr_url
                issue_github["branch"] = worktree_branch

                if issue_number is not None and not args.dry_run:
                    gh_issue_comment(
                        gh_repo_slug,
                        issue_number,
                        f"Post-fix verification passed. PR: {pr_url or '(pending URL)'}",
                        cwd=repo_path,
                    )
                if pr_number is not None and not args.dry_run:
                    gh_pr_comment(
                        gh_repo_slug,
                        pr_number,
                        f"Automated verification passed for finding {finding.finding_id}.",
                        cwd=repo_path,
                    )
            else:
                pr_url = ""

            if pr_number is not None or not args.live_github_actions:
                set_issue_status(
                    issue, "pr_opened", "autofix PR created from issue queue"
                )

            entry = {
                "type": "pr",
                "repo": str(repo_path),
                "branch": worktree_branch,
                "dry_run": args.dry_run,
                "live_github_actions": bool(args.live_github_actions),
                "created_at": now_iso(),
                "linked_issue_ids": [issue.get("id") or issue["issue_id"]],
                "linked_finding_ids": [finding.finding_id],
                "github_issue_url": issue_url,
                "github_issue_number": issue_number,
                "github_pr_url": pr_url,
                "github_pr_number": pr_number,
                "note": "live GitHub PR workflow complete after fix+verify"
                if args.live_github_actions
                else "simulated local PR creation after e2e fix+verification gate",
            }
            state.setdefault("created", []).append(entry)
            mark_finding_activity(
                state=state,
                finding_ids=[finding.finding_id],
                action="pr-opened",
                failure_count=0,
                last_error=None,
            )
            open_prs += 1
            created_prs += 1
            run_status = (
                "pr-live-created"
                if args.live_github_actions
                else "pr-simulated-resolved-verified"
            )

        finally:
            run_no_capture(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=repo_path,
            )
            run_no_capture(["git", "worktree", "prune"], cwd=repo_path)
            if not args.live_github_actions:
                run_no_capture(["git", "branch", "-D", worktree_branch], cwd=repo_path)
            _append_text(
                log_file, f"cleanup: branch={worktree_branch} status={run_status}"
            )

    return {
        "created_prs": created_prs,
        "open_prs": open_prs,
        "fix_attempts": fix_attempts,
        "fixes_verified": fixes_verified,
        "fixes_failed_verification": fixes_failed_verification,
        "issues_escalated_max_retries": issues_escalated_max_retries,
        "claude_invocations": claude_invocations,
        "deterministic_invocations": deterministic_invocations,
        "blocked_reasons": blocked_reasons,
        "state": state,
        "issues_data": issues_data,
        "eligible_findings": eligible_findings,
    }
