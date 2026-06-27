"""CLI entry point for the sandbox runner and status artifact writer.

Handles arg parsing, engine selection, and full scan lifecycle: discover →
issues → fixes → PRs → merge.  Writes ``status.json`` per run.

Exit codes: 0 success, 1 smoke-test fail, 2 abort, 4 needs-human.
"""

import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bluei.engine.models import now_iso, Finding, FixEngine
from bluei.engine.utils import (
    append_lesson,
    assert_safe_repo,
)
from bluei.engine.state import (
    load_state,
    load_issues,
    _append_text,
    reconcile_open_workload,
)
from bluei.engine.cost_tracker import CostTracker
from bluei.engine.gh import (
    get_origin_url,
    parse_github_repo,
    repo_is_sandbox,
)
from bluei.engine.review_helpers import classify_review_feedback, review_loop_allowed
from bluei.engine.startup import run_startup_self_healing
from bluei.engine.mnemo_client import is_mnemo_available
from bluei.engine.pattern_store import FixPatternStore
from bluei.engine.git_utils import refresh_docs_index
from bluei.engine.constants import (
    DEFAULT_FINDINGS,
    DEFAULT_ISSUES,
    DEFAULT_DOCS_INDEX,
    DEFAULT_LESSONS_LOG,
)

logger = logging.getLogger(__name__)

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
from bluei.engine.commands.context import RunContext  # noqa: E402


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

    # --- Construct RunContext: single source of truth for pipeline state ---
    run_issue_cycle = args.run_phase in ("issue-cycle", "orchestrated")
    run_pr_cycle = args.run_phase in ("pr-cycle", "orchestrated")
    run_merge_cycle = args.run_phase in ("merge-cycle", "orchestrated")

    cost_log_path = Path(state_file).parent / "cost_log.jsonl"
    run_id = str(uuid.uuid4())
    cost_tracker = CostTracker(
        log_path=cost_log_path,
        soft_warn=2.0,
        hard_limit=10.0,
        run_id=run_id,
    )

    issues_data = load_issues(issues_file)

    ctx = RunContext(
        args=args,
        repo_path=repo_path,
        state_file=state_file,
        log_file=log_file,
        findings_file=findings_file,
        issues_file=issues_file,
        worktree_root=worktree_root,
        status_file=status_file,
        docs_index_file=docs_index_file,
        lessons_file=lessons_file,
        review_state_file=review_state_file,
        gh_repo_slug=gh_repo_slug,
        origin_url=origin_url,
        state=state,
        issues_data=issues_data,
        open_issues=open_issues,
        open_prs=open_prs,
        reconcile_event=reconcile_event,
        previous_last_run_at=previous_last_run_at,
        cost_tracker=cost_tracker,
        cost_log_path=cost_log_path,
        run_id=run_id,
        pattern_store=pattern_store,
        PER_REPO_BASELINE_CHECKS=PER_REPO_BASELINE_CHECKS,
        run_issue_cycle=run_issue_cycle,
        run_pr_cycle=run_pr_cycle,
        run_merge_cycle=run_merge_cycle,
    )

    if args.reconcile_only:
        return run_reconcile_only(ctx)

    if (
        args.run_phase in ("verify-only",) or ctx.run_issue_cycle
    ) and not ctx.docs_index_file.exists():
        refresh_docs_index(ctx.repo_path, ctx.docs_index_file, ctx.log_file)

    cap_ok = True
    if args.run_phase in ("verify-only",) or ctx.run_issue_cycle:
        (
            ctx.findings,
            ctx.written_findings,
            ctx.eligible_findings,
            ctx.suppressed_findings,
            ctx.refactor_routed_items,
            _disc_blocked,
            cap_ok,
        ) = run_discover_phase(ctx)
        ctx.blocked_reasons.extend(_disc_blocked)

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

    if ctx.run_issue_cycle:
        _created, _open, _blocked = run_issue_creation_phase(ctx)
        ctx.created_issues.extend(_created)
        ctx.open_issues = _open
        ctx.blocked_reasons.extend(_blocked)

    if args.run_phase == "verify-only":
        return run_verify_only(ctx)

    if ctx.run_pr_cycle:
        pr_result = run_pr_cycle_phase(ctx)
        ctx.created_prs = pr_result["created_prs"]
        ctx.open_prs = pr_result["open_prs"]
        ctx.fix_attempts = pr_result["fix_attempts"]
        ctx.fixes_verified = pr_result["fixes_verified"]
        ctx.fixes_failed_verification = pr_result["fixes_failed_verification"]
        ctx.issues_escalated_max_retries = pr_result["issues_escalated_max_retries"]
        ctx.claude_invocations = pr_result["claude_invocations"]
        ctx.deterministic_invocations = pr_result["deterministic_invocations"]
        ctx.blocked_reasons = pr_result["blocked_reasons"]
        ctx.state = pr_result["state"]
        ctx.issues_data = pr_result["issues_data"]
        ctx.eligible_findings = pr_result["eligible_findings"]
    if ctx.run_merge_cycle:
        (
            ctx.merges_failed,
            ctx.merges_succeeded,
            ctx.merge_attempts,
            ctx.merged_pr_urls,
            ctx.open_prs,
            ctx.open_issues,
            ctx.blocked_reasons,
            ctx.reconcile_event,
        ) = run_merge_cycle_phase(ctx)

    return run_finalize_phase(ctx)


if __name__ == "__main__":
    sys.exit(main())
