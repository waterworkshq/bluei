"""CLI entry point for the sandbox runner and status artifact writer.

Handles arg parsing, engine selection, and full scan lifecycle: discover →
issues → fixes → PRs → merge.  Writes ``status.json`` per run.

Exit codes: 0 success, 1 smoke-test fail, 2 abort, 4 needs-human.
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bluei.engine.models import now_iso, age_seconds, Finding, parse_iso, FixEngine
from bluei.engine.utils import (
    sanitize_command_template,
    run_capture,
    run_no_capture,
    branch_suffix,
    append_lesson,
    assert_safe_repo,
    is_path_tracked,
)
from bluei.engine.state import (
    load_state,
    save_state,
    load_issues,
    save_issues,
    _append_text,
    guard_open_issues,
    guard_open_prs,
    record_reconciliation_event,
    reconcile_open_workload,
    mark_finding_activity,
    filter_findings_by_cooldown,
    append_findings,
    load_finding_record,
    update_finding_record,
    increment_fix_attempt,
    count_actionable_issues,
    NON_ACTIONABLE_ISSUE_STATUSES,
)
from bluei.engine.cost_tracker import CostTracker
from bluei.engine.gh import (
    get_origin_url,
    parse_github_repo,
    repo_is_sandbox,
    find_existing_github_issue,
    find_existing_github_pr,
    gh_issue_comment,
    gh_issue_close,
    gh_pr_comment,
    finding_from_issue_record,
    fetch_open_prs_for_merge,
    evaluate_pr_check_health,
    evaluate_pr_reviews,
    evaluate_pr_mergeability,
    merge_failure_requires_pr_fix,
    merge_pr,
    create_or_update_github_issue,
    create_or_update_github_pr,
    fetch_github_live_counts,
)
from bluei.engine.rebase_sweep import sweep_rebase
from bluei.engine.orchestrator import (
    build_active_cycle_command,
    build_issue_cycle_command,
    build_pr_cycle_command,
    build_merge_cycle_command,
    build_orchestrated_cycle_command,
    build_refactor_cycle_command,
    build_reconcile_only_command,
    build_docs_index_refresh_command,
    build_verification_only_command,
    discover_findings,
    create_issues_for_findings,
    choose_safe_autofix_items,
    route_findings_with_intent,
    find_issue_for_finding,
    ensure_issue_for_finding,
    append_issue_history,
    set_issue_status,
    count_failed_fix_attempts,
    check_consecutive_fix_failures,
    check_finding_escalation_before_fix,
)
from bluei.engine.git_ops import git_commit_all, git_push_branch, diff_stats
from bluei.engine.lifecycle import (
    apply_autofix,
    apply_claude_fix,
    process_refactor_queue,
)
from bluei.engine.review_helpers import classify_review_feedback, review_loop_allowed
from bluei.engine.startup import run_startup_self_healing
from bluei.engine.validation import (
    verify_fix_closed,
    run_named_checks,
    build_target_checks,
    run_validation_gate,
    choose_validation_baseline,
    run_smoke_test,
)
from bluei.engine.prompts import (
    render_test_coverage_prompt,
    render_type_safety_prompt,
    render_complexity_refactor_prompt,
    render_maxlines_refactor_prompt,
    render_claude_fix_prompt,
)
from bluei.engine.mnemo_client import is_mnemo_available
from bluei.engine.pattern_replay import try_replay
from bluei.engine.pattern_store import FixPatternStore
from bluei.engine.reforge import RefactorClass, classify_finding
from bluei.engine.git_utils import get_branch, refresh_docs_index, load_docs_index
from bluei.engine.constants import (
    DEFAULT_STATE,
    DEFAULT_LOG,
    DEFAULT_FINDINGS,
    DEFAULT_STATUS,
    DEFAULT_REPO,
    DETECTOR_CATALOG,
    WORKSPACE,
    AGENT_ROOT,
    DEFAULT_ISSUES,
    DEFAULT_WORKTREE_ROOT,
    DEFAULT_DOCS_INDEX,
    DEFAULT_LESSONS_LOG,
    BASELINE_VALIDATION_CHECKS,
    RULE_TARGET_CHECKS,
    CLAUDE_REQUIRED_RULES,
    DEFAULT_FIX_ENGINE,
    DEFAULT_CLAUDE_CMD_TEMPLATE,
    QA_FIX_PROMPT_FILENAME,
    DEFAULT_FINDING_COOLDOWN_SECONDS,
    DEFAULT_STALENESS_THRESHOLD_SECONDS,
    DEFAULT_BATCH_RULES_PATH,
    DEFAULT_BATCH_STATE,
    load_llm_fixable_rules,
)

logger = logging.getLogger(__name__)

from bluei.engine.commands.helpers import (  # noqa: E402
    _build_refactor_queue_snapshot,
    _compute_health_score,
    _autonomous_review_gate_passes,
    _get_llm_fixable_rules,
    _hydrate_worktree_dependencies,
    _load_batch_rules_for_args,
    _load_review_state,
    _reconcile_issue_pr_link,
    _triage_pr_back_to_fix_cycle,
    update_status_artifact,
)
from bluei.engine.commands.parse_args import (  # noqa: E402
    normalize_run_phase,
    parse_args,
    resolve_baseline_checks,
)
from bluei.engine.commands.early_exits import (  # noqa: E402
    run_clean_prs_command,
    run_docs_index_command,
    run_refactor_cycle_command,
    run_smoke_test_command,
    validate_safety,
)
from bluei.engine.commands.pipeline import (  # noqa: E402
    run_discover_phase,
    run_finalize_phase,
    run_issue_creation_phase,
    run_merge_cycle_phase,
    run_pr_cycle_phase,
    run_reconcile_only,
    run_verify_only,
)


def main() -> int:
    """Parse CLI arguments and run the configured sandbox lifecycle phase.

    Returns:
        Exit code: 0 success, 1 smoke-test fail, 2 abort, 4 needs-human.
    """
    args = parse_args()
    normalize_run_phase(args)
    PER_REPO_BASELINE_CHECKS = resolve_baseline_checks(args)

    repo_path = Path(args.repo_path)

    # Phase 4: log mnemo availability
    mnemo_ok = is_mnemo_available(repo_path)
    if mnemo_ok:
        logger.info("mnemo available — recall and seeding enabled for this repo")
    else:
        logger.info("mnemo unavailable — falling back to local reranker")

    state_file = Path(args.state_file)
    log_file = Path(args.log_file)
    findings_file = Path(args.findings_file)
    issues_file = Path(args.issues_file)
    worktree_root = Path(args.worktree_root)
    status_file = Path(args.status_file)
    docs_index_file = Path(args.docs_index_file)
    lessons_file = Path(args.lessons_file)
    if args.lessons_file == str(DEFAULT_LESSONS_LOG):
        from bluei.engine.constants import repo_lessons_path

        lessons_file = repo_lessons_path(repo_path)
    if args.findings_file == str(DEFAULT_FINDINGS):
        from bluei.engine.constants import repo_findings_path

        findings_file = repo_findings_path(repo_path)
    if args.issues_file == str(DEFAULT_ISSUES):
        from bluei.engine.constants import repo_issues_path

        issues_file = repo_issues_path(repo_path)
    if args.docs_index_file == str(DEFAULT_DOCS_INDEX):
        from bluei.engine.constants import repo_docs_index_path

        docs_index_file = repo_docs_index_path(repo_path)
    review_state_file = issues_file.with_name("review_state.json")

    pattern_store: Optional[FixPatternStore] = None
    if args.pattern_store_path is not None:
        try:
            pattern_store = FixPatternStore(Path(args.pattern_store_path))
        except Exception as exc:
            _append_text(log_file, f"pattern-store: init error {exc}")

    # Handle manual lesson entry
    if args.log_lesson:
        append_lesson(
            lessons_file=lessons_file,
            cycle_type="manual",
            what_broke=args.log_lesson if "broke" in args.log_lesson.lower() else "",
            what_changed=args.log_lesson
            if "changed" in args.log_lesson.lower()
            or "change" in args.log_lesson.lower()
            else "",
            what_worked=args.log_lesson if "worked" in args.log_lesson.lower() else "",
        )
        # If only logging a lesson, exit
        if args.reconcile_only or args.run_phase == "docs-index":
            print(f"[DONE] Logged manual lesson to {lessons_file}")
            return 0

    try:
        assert_safe_repo(repo_path)
    except Exception as e:
        print(f"[ABORT] {e}")
        _append_text(log_file, f"abort: {e}")
        return 2

    # --- Wave 1, Item 3: Self-healing on startup ---
    try:
        locks_dir = (
            Path(args.worktree_root).parent / "locks"
            if hasattr(args, "worktree_root")
            else None
        )
        heal_result = run_startup_self_healing(
            repo_path=repo_path,
            log_file=log_file,
            locks_dir=locks_dir,
            dry_run=getattr(args, "dry_run", True),
        )
        if heal_result.get("stale_locks_removed", 0) > 0:
            _append_text(
                log_file,
                f"self-heal: removed {heal_result['stale_locks_removed']} stale lock(s)",
            )
        if heal_result.get("errors"):
            for err in heal_result["errors"]:
                _append_text(log_file, f"self-heal: error {err}")
    except Exception as e:
        _append_text(log_file, f"self-heal: init error {e}")

    # --- Batch recovery: resolve interrupted batches from prior crashes ---
    try:
        from bluei.engine.batch_pr import recover_interrupted_batch
        from bluei.engine.state import load_batches

        batches_file = Path(
            getattr(args, "batch_state_file", str(state_file.parent / "batches.jsonl"))
        )
        if batches_file.exists():
            batches = load_batches(batches_file)
            for record in batches:
                bid = record.get("batch_id", "")
                status = record.get("status", "")
                if status in ("fixing", "fixing-partial"):
                    recovered = recover_interrupted_batch(
                        bid, batches_file, worktree_root
                    )
                    if recovered is not None:
                        _append_text(
                            log_file,
                            f"batch-recovery: {bid} resolved → {recovered.status}",
                        )
    except Exception as e:
        _append_text(log_file, f"batch-recovery: init error {e}")

    if args.fix_engine == FixEngine.CLAUDE.value:
        if sys.executable is None or True:
            pass  # Keep going; the actual check happens at fix time

    rc = run_docs_index_command(
        repo_path=repo_path,
        docs_index_file=docs_index_file,
        log_file=log_file,
        args=args,
    )
    if rc is not None:
        return rc

    rc = run_refactor_cycle_command(
        repo_path=repo_path,
        worktree_root=worktree_root,
        log_file=log_file,
        args=args,
    )
    if rc is not None:
        return rc

    rc = run_smoke_test_command(
        repo_path=repo_path,
        log_file=log_file,
        args=args,
    )
    if rc is not None:
        return rc

    rc = run_clean_prs_command(
        repo_path=repo_path,
        log_file=log_file,
        args=args,
    )
    if rc is not None:
        return rc

    rc = validate_safety(args=args, log_file=log_file)
    if rc is not None:
        return rc

    origin_url = get_origin_url(repo_path)
    gh_owner, gh_name = parse_github_repo(origin_url)
    gh_repo_slug = f"{gh_owner}/{gh_name}" if gh_owner and gh_name else ""
    if args.live_github_actions and not gh_repo_slug:
        print("[ABORT] live mode requires a GitHub origin remote")
        _append_text(log_file, "abort: live mode requested on non-GitHub repo")
        return 2

    if args.auto_merge_sandbox and not repo_is_sandbox(gh_repo_slug):
        print("[ABORT] --auto-merge-sandbox is restricted to qa-sandbox-repo only")
        _append_text(
            log_file,
            f"abort: auto-merge not allowed for repo={gh_repo_slug or 'unknown'}",
        )
        return 2

    if not args.reconcile_only and args.run_phase != "merge-cycle":
        # Review loop policy scaffolding
        pr_tags = [x.strip() for x in args.pr_tags.split(",") if x.strip()]
        review_ok, review_reason = review_loop_allowed(
            args.pr_author, pr_tags, args.bot_author, args.explicit_tag
        )
        _append_text(log_file, review_reason)
        if not review_ok:
            print(f"[NEEDS-HUMAN] {review_reason}")
            return 4

        feedback_class = classify_review_feedback(args.review_feedback)
        if feedback_class == "needs-human":
            _append_text(
                log_file, "review-feedback classified as conceptual -> needs-human"
            )
            print(
                "[NEEDS-HUMAN] conceptual review feedback requires human intervention"
            )
            return 4

    state = load_state(state_file)
    previous_last_run_at = state.get("last_run_at")
    open_issues, open_prs, reconcile_event = reconcile_open_workload(
        repo_path=repo_path,
        state=state,
        log_file=log_file,
        simulate_open_issues=args.simulate_open_issues,
        simulate_open_prs=args.simulate_open_prs,
    )
    state["last_run_at"] = now_iso()

    if args.reconcile_only:
        return run_reconcile_only(
            state_file=state_file,
            state=state,
            status_file=status_file,
            issues_file=issues_file,
            findings_file=findings_file,
            log_file=log_file,
            args=args,
            reconcile_event=reconcile_event,
            previous_last_run_at=previous_last_run_at,
            open_issues=open_issues,
            open_prs=open_prs,
        )

    blocked_reasons: List[str] = []
    fix_attempts = 0
    fixes_verified = 0
    fixes_failed_verification = 0
    issues_escalated_max_retries = 0
    created_prs = 0
    claude_invocations = 0
    opencode_invocations = 0
    deterministic_invocations = 0
    merge_attempts = 0
    merges_succeeded = 0
    merges_failed = 0
    merged_pr_urls: List[str] = []

    # Cost tracker — persisted per-cycle so cumulative cost history is available
    cost_log_path = Path(state_file).parent / "cost_log.jsonl"
    cost_tracker = CostTracker(
        log_path=cost_log_path,
        soft_warn=2.0,
        hard_limit=10.0,
    )

    findings: List[Finding] = []
    written_findings = 0
    eligible_findings: List[Finding] = []
    suppressed_findings: List[Finding] = []
    refactor_routed_items: List[Dict[str, Any]] = []

    issues_data = load_issues(issues_file)
    created_issues: List[Dict[str, Any]] = []

    run_issue_cycle = args.run_phase in ("issue-cycle", "orchestrated")
    run_pr_cycle = args.run_phase in ("pr-cycle", "orchestrated")
    run_merge_cycle = args.run_phase in ("merge-cycle", "orchestrated")

    if (
        args.run_phase in ("verify-only",) or run_issue_cycle
    ) and not docs_index_file.exists():
        refresh_docs_index(repo_path, docs_index_file, log_file)

    cap_ok = True
    if args.run_phase in ("verify-only",) or run_issue_cycle:
        (
            findings,
            written_findings,
            eligible_findings,
            suppressed_findings,
            refactor_routed_items,
            _disc_blocked,
            cap_ok,
        ) = run_discover_phase(
            repo_path=repo_path,
            docs_index_file=docs_index_file,
            findings_file=findings_file,
            log_file=log_file,
            state=state,
            issues_data=issues_data,
            args=args,
            run_issue_cycle=run_issue_cycle,
            run_pr_cycle=run_pr_cycle,
        )
        blocked_reasons.extend(_disc_blocked)

    # --- Contextual Fix Migration ---
    if args.migrate_context:
        from bluei.engine.migrate_context import reclassify_findings, dry_run_report

        findings_path = Path(args.findings_file)
        if args.dry_run:
            report = dry_run_report(findings_path)
            _append_text(log_file, "migrate-context (dry run):\n" + report)
            print(report)
        else:
            changes = reclassify_findings(findings_path)
            _append_text(
                log_file, f"migrate-context: {len(changes)} findings reclassified"
            )
            for fid, info in changes.items():
                _append_text(
                    log_file,
                    f"  {fid}: {info['old_class']} -> {info['new_class']} (rule={info['rule']})",
                )
            print(f"Reclassified {len(changes)} findings.")

    if run_issue_cycle:
        _created, _open, _blocked = run_issue_creation_phase(
            issues_data=issues_data,
            eligible_findings=eligible_findings,
            refactor_routed_items=refactor_routed_items,
            log_file=log_file,
            args=args,
            gh_repo_slug=gh_repo_slug,
            open_issues=open_issues,
        )
        created_issues.extend(_created)
        open_issues = _open
        blocked_reasons.extend(_blocked)

    if args.run_phase == "verify-only":
        return run_verify_only(
            findings=findings,
            issues_data=issues_data,
            issues_file=issues_file,
            state_file=state_file,
            state=state,
            status_file=status_file,
            findings_file=findings_file,
            log_file=log_file,
            args=args,
            reconcile_event=reconcile_event,
            previous_last_run_at=previous_last_run_at,
            open_issues=open_issues,
            open_prs=open_prs,
            written_findings=written_findings,
            suppressed_findings=suppressed_findings,
            blocked_reasons=blocked_reasons,
        )

    if run_pr_cycle:
        pr_result = run_pr_cycle_phase(
            repo_path=repo_path,
            findings_file=findings_file,
            log_file=log_file,
            worktree_root=worktree_root,
            gh_repo_slug=gh_repo_slug,
            review_state_file=review_state_file,
            docs_index_file=docs_index_file,
            lessons_file=lessons_file,
            args=args,
            state=state,
            issues_data=issues_data,
            eligible_findings=eligible_findings,
            findings=findings,
            PER_REPO_BASELINE_CHECKS=PER_REPO_BASELINE_CHECKS,
            cost_tracker=cost_tracker,
            pattern_store=pattern_store,
            created_prs=created_prs,
            open_prs=open_prs,
            fix_attempts=fix_attempts,
            fixes_verified=fixes_verified,
            fixes_failed_verification=fixes_failed_verification,
            issues_escalated_max_retries=issues_escalated_max_retries,
            claude_invocations=claude_invocations,
            deterministic_invocations=deterministic_invocations,
            blocked_reasons=blocked_reasons,
        )
        created_prs = pr_result["created_prs"]
        open_prs = pr_result["open_prs"]
        fix_attempts = pr_result["fix_attempts"]
        fixes_verified = pr_result["fixes_verified"]
        fixes_failed_verification = pr_result["fixes_failed_verification"]
        issues_escalated_max_retries = pr_result["issues_escalated_max_retries"]
        claude_invocations = pr_result["claude_invocations"]
        deterministic_invocations = pr_result["deterministic_invocations"]
        blocked_reasons = pr_result["blocked_reasons"]
        state = pr_result["state"]
        issues_data = pr_result["issues_data"]
        eligible_findings = pr_result["eligible_findings"]
    if run_merge_cycle:
        (
            merges_failed,
            merges_succeeded,
            merge_attempts,
            merged_pr_urls,
            open_prs,
            open_issues,
            blocked_reasons,
            reconcile_event,
        ) = run_merge_cycle_phase(
            repo_path=repo_path,
            log_file=log_file,
            review_state_file=review_state_file,
            state=state,
            issues_data=issues_data,
            args=args,
            gh_repo_slug=gh_repo_slug,
            merges_failed=merges_failed,
            merges_succeeded=merges_succeeded,
            merge_attempts=merge_attempts,
            merged_pr_urls=merged_pr_urls,
            open_prs=open_prs,
            open_issues=open_issues,
            blocked_reasons=blocked_reasons,
            reconcile_event=reconcile_event,
        )

    return run_finalize_phase(
        state_file=state_file,
        issues_file=issues_file,
        status_file=status_file,
        findings_file=findings_file,
        log_file=log_file,
        lessons_file=lessons_file,
        repo_path=repo_path,
        args=args,
        state=state,
        issues_data=issues_data,
        reconcile_event=reconcile_event,
        previous_last_run_at=previous_last_run_at,
        open_issues=open_issues,
        open_prs=open_prs,
        findings=findings,
        written_findings=written_findings,
        created_issues=created_issues,
        suppressed_findings=suppressed_findings,
        blocked_reasons=blocked_reasons,
        fix_attempts=fix_attempts,
        fixes_verified=fixes_verified,
        fixes_failed_verification=fixes_failed_verification,
        created_prs=created_prs,
        issues_escalated_max_retries=issues_escalated_max_retries,
        merge_attempts=merge_attempts,
        merges_succeeded=merges_succeeded,
        merges_failed=merges_failed,
        merged_pr_urls=merged_pr_urls,
        claude_invocations=claude_invocations,
        opencode_invocations=opencode_invocations,
        deterministic_invocations=deterministic_invocations,
        cost_tracker=cost_tracker,
        cost_log_path=cost_log_path,
        gh_repo_slug=gh_repo_slug,
    )


if __name__ == "__main__":
    sys.exit(main())
