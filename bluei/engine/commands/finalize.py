"""Finalize phase: save state, write status artifact, log lessons, escalation checks."""

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.constants import AGENT_ROOT
from bluei.engine.models import is_needs_human_status, now_iso
from bluei.engine.state import _append_text, save_issues, save_state
from bluei.engine.utils import append_lesson
from bluei.engine.commands.helpers import update_status_artifact

logger = logging.getLogger(__name__)


def _build_flywheel_ledger(
    ledger_records: List[Dict[str, Any]],
    cost_tracker: Any,
    pattern_store: Any,
) -> Dict[str, Any]:
    """Build the flywheel_ledger block from in-memory records + cost tracker + pattern store.

    ``ledger_records`` holds exactly one record per *attempted* Finding across all
    resolution paths (standalone-replay HIT, cascade stage wins, LLM fallback,
    exhaustion). Per-stage deterministic wins are read off each record's
    ``final_stage``. Note: pattern-replay *misses* and *failures* produce no
    resolution record (they don't resolve the Finding), so the per-cycle block
    reports ``pattern_replay_resolutions`` (resolving hits only), not full
    HIT/MISS/FAILURE — those live on the Pattern store cumulatively (AC-C4 health).
    """
    findings_attempted = len(ledger_records)

    resolved_deterministic_by_stage: Counter = Counter()
    resolved_llm = 0
    exhausted = 0
    pattern_replay_resolutions = 0

    for rec in ledger_records:
        outcome = rec.get("outcome")
        if outcome == "resolved_deterministic":
            final_stage = rec.get("final_stage", "unknown")
            resolved_deterministic_by_stage[final_stage] += 1
            if final_stage == "pattern-replay":
                pattern_replay_resolutions += 1
        elif outcome == "resolved_llm":
            resolved_llm += 1
        elif outcome == "exhausted":
            exhausted += 1

    resolved_deterministic_total = sum(resolved_deterministic_by_stage.values())

    savings_usd = round(cost_tracker.cycle_savings(), 6)
    cost_total_usd = round(cost_tracker.cycle_total(), 6)

    # Rates per ADR-0002: num/denom (%)
    if findings_attempted > 0:
        det_rate = f"{resolved_deterministic_total}/{findings_attempted} ({resolved_deterministic_total * 100 / findings_attempted:.1f}%)"
        llm_rate = f"{resolved_llm}/{findings_attempted} ({resolved_llm * 100 / findings_attempted:.1f}%)"
        exhaust_rate = f"{exhausted}/{findings_attempted} ({exhausted * 100 / findings_attempted:.1f}%)"
    else:
        det_rate = "0/0 (n/a)"
        llm_rate = "0/0 (n/a)"
        exhaust_rate = "0/0 (n/a)"

    rates = {
        "deterministic_resolution": det_rate,
        "llm_fallback": llm_rate,
        "exhausted": exhaust_rate,
    }

    # Pattern store snapshot (cumulative)
    active_pattern_count = 0
    top_failing_patterns: List[Dict[str, Any]] = []
    if pattern_store:
        try:
            active_patterns = pattern_store.load_active()
            active_pattern_count = len(active_patterns)
            sorted_by_failure = sorted(
                active_patterns, key=lambda p: p.failure_count, reverse=True
            )
            top_failing_patterns = [
                {
                    "pattern_id": p.pattern_id,
                    "rule": p.rule,
                    "failure_count": p.failure_count,
                }
                for p in sorted_by_failure[:3]
                if p.failure_count > 0
            ]
        except Exception:
            logger.debug("finalize: failed to read pattern store for ledger snapshot")

    block: Dict[str, Any] = {
        "findings_attempted": findings_attempted,
        "resolved_deterministic_by_stage": dict(resolved_deterministic_by_stage),
        "resolved_deterministic_total": resolved_deterministic_total,
        "resolved_llm": resolved_llm,
        "exhausted": exhausted,
        "pattern_replay_resolutions": pattern_replay_resolutions,
        "savings_usd": savings_usd,
        "cost_total_usd": cost_total_usd,
        # alpha.6: Governor's routing contribution to "$ avoided". Always 0.0
        # under the identity default (no live downgrades); active in beta.1.
        # Default 0.0 (not None) so old status.json blocks load identically.
        "routing_savings_usd": 0.0,
        "rates": rates,
        "active_pattern_count": active_pattern_count,
        "top_failing_patterns": top_failing_patterns,
    }
    return block


def run_finalize_phase(*args, **kwargs) -> int:
    """Save state, write status artifact, log lessons, run escalation checks."""
    ctx = None
    if args:
        ctx = args[0]
        state_file = ctx.state_file
        issues_file = ctx.issues_file
        status_file = ctx.status_file
        findings_file = ctx.findings_file
        log_file = ctx.log_file
        lessons_file = ctx.lessons_file
        repo_path = ctx.repo_path
        args = ctx.args
        state = ctx.state
        issues_data = ctx.issues_data
        reconcile_event = ctx.reconcile_event
        previous_last_run_at = ctx.previous_last_run_at
        open_issues = ctx.open_issues
        open_prs = ctx.open_prs
        findings = ctx.findings
        written_findings = ctx.written_findings
        created_issues = ctx.created_issues
        suppressed_findings = ctx.suppressed_findings
        blocked_reasons = ctx.blocked_reasons
        fix_attempts = ctx.fix_attempts
        fixes_verified = ctx.fixes_verified
        fixes_failed_verification = ctx.fixes_failed_verification
        created_prs = ctx.created_prs
        issues_escalated_max_retries = ctx.issues_escalated_max_retries
        merge_attempts = ctx.merge_attempts
        merges_succeeded = ctx.merges_succeeded
        merges_failed = ctx.merges_failed
        merged_pr_urls = ctx.merged_pr_urls
        claude_invocations = ctx.claude_invocations
        opencode_invocations = ctx.opencode_invocations
        deterministic_invocations = ctx.deterministic_invocations
        cost_tracker = ctx.cost_tracker
        cost_log_path = ctx.cost_log_path
        gh_repo_slug = ctx.gh_repo_slug
        ledger_records = ctx.ledger_records
        pattern_store = ctx.pattern_store
        learning_mode = ctx.learning_mode
    else:
        state_file = kwargs["state_file"]
        issues_file = kwargs["issues_file"]
        status_file = kwargs["status_file"]
        findings_file = kwargs["findings_file"]
        log_file = kwargs["log_file"]
        lessons_file = kwargs["lessons_file"]
        repo_path = kwargs["repo_path"]
        args = kwargs["args"]
        state = kwargs["state"]
        issues_data = kwargs["issues_data"]
        reconcile_event = kwargs["reconcile_event"]
        previous_last_run_at = kwargs["previous_last_run_at"]
        open_issues = kwargs["open_issues"]
        open_prs = kwargs["open_prs"]
        findings = kwargs["findings"]
        written_findings = kwargs["written_findings"]
        created_issues = kwargs["created_issues"]
        suppressed_findings = kwargs["suppressed_findings"]
        blocked_reasons = kwargs["blocked_reasons"]
        fix_attempts = kwargs["fix_attempts"]
        fixes_verified = kwargs["fixes_verified"]
        fixes_failed_verification = kwargs["fixes_failed_verification"]
        created_prs = kwargs["created_prs"]
        issues_escalated_max_retries = kwargs["issues_escalated_max_retries"]
        merge_attempts = kwargs["merge_attempts"]
        merges_succeeded = kwargs["merges_succeeded"]
        merges_failed = kwargs["merges_failed"]
        merged_pr_urls = kwargs["merged_pr_urls"]
        claude_invocations = kwargs["claude_invocations"]
        opencode_invocations = kwargs["opencode_invocations"]
        deterministic_invocations = kwargs["deterministic_invocations"]
        cost_tracker = kwargs["cost_tracker"]
        cost_log_path = kwargs["cost_log_path"]
        gh_repo_slug = kwargs["gh_repo_slug"]
        ledger_records = kwargs.get("ledger_records", [])
        pattern_store = kwargs.get("pattern_store")
        learning_mode = kwargs.get("learning_mode", "active")

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
            "learning_mode": learning_mode,
        },
    )

    flywheel_ledger = _build_flywheel_ledger(
        ledger_records, cost_tracker, pattern_store
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
            status_data["flywheel_ledger"] = flywheel_ledger
            status_file.write_text(
                json.dumps(status_data, indent=2, sort_keys=True) + "\n"
            )
    except (ImportError, OSError, json.JSONDecodeError):
        logger.debug("Failed to save pipeline status")

    det_total = flywheel_ledger["resolved_deterministic_total"]
    attempted = flywheel_ledger["findings_attempted"]
    llm = flywheel_ledger["resolved_llm"]
    replay_hits = flywheel_ledger["pattern_replay_resolutions"]
    saved = flywheel_ledger["savings_usd"]
    ledger_suffix = (
        f" det={det_total}/{attempted} llm={llm}"
        f" replay_hits={replay_hits} saved=${saved:.4f}"
    )

    # Dry Replay summary (ADR-0011) — only if any Dry Replays were performed
    # this cycle. ``_dry_replay_performed`` / ``_dry_replay_capped`` are set
    # on RunContext by the Dry Replay phase in _process_one_issue. Only the
    # RunContext-style call path surfaces them.
    _dr_performed = getattr(ctx, "_dry_replay_performed", 0) if ctx else 0
    _dr_capped = bool(getattr(ctx, "_dry_replay_capped", False)) if ctx else False
    dry_replay_suffix = ""
    if _dr_performed:
        dry_replay_suffix = (
            f" dry-replay: {_dr_performed} performed{', cap-hit' if _dr_capped else ''}"
        )
    print(
        f"[DONE] {run_mode} learning={learning_mode} findings={len(findings)} issues_created={len(created_issues)} "
        f"fix_attempts={fix_attempts} fixes_verified={fixes_verified} "
        f"fixes_failed_verification={fixes_failed_verification} prs_created={created_prs} "
        f"issues_escalated_max_retries={issues_escalated_max_retries} "
        f"merges={merges_succeeded}/{merge_attempts} "
        f"cost: claude={claude_invocations} deterministic={deterministic_invocations} "
        f"total=${cost_tracker.cycle_total():.4f}{ledger_suffix}{dry_replay_suffix}"
    )
    _append_text(
        log_file,
        f"done: mode={run_mode} learning={learning_mode} findings={len(findings)} issues={len(created_issues)} "
        f"fix_attempts={fix_attempts} fixes_verified={fixes_verified} "
        f"fixes_failed_verification={fixes_failed_verification} prs={created_prs} "
        f"issues_escalated_max_retries={issues_escalated_max_retries} "
        f"merges={merges_succeeded}/{merge_attempts} "
        f"cost: claude={claude_invocations} deterministic={deterministic_invocations} "
        f"total=${cost_tracker.cycle_total():.4f}{ledger_suffix}{dry_replay_suffix}",
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
            ) or is_needs_human_status(str(last_event.get("event", "")))
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
