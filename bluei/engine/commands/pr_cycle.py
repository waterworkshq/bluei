"""PR cycle phase: queue candidates, apply fixes, create PRs."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import time

from bluei.engine.models import Finding, FixEngine, IssueStatus, now_iso
from bluei.engine.state import (
    NON_ACTIONABLE_ISSUE_STATUSES,
    _append_text,
    guard_open_prs,
    increment_fix_attempt,
    mark_finding_activity,
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
    ClaudeFixRequest,
    apply_autofix,
    apply_claude_fix,
)
from bluei.engine.validation import (
    build_target_checks,
    choose_validation_baseline,
    run_named_checks,
    run_validation_gate,
)
from bluei.engine.verify import verify_fix_closed
from bluei.engine.utils import (
    branch_suffix,
    is_path_tracked,
)
from bluei.engine.reforge import RefactorClass
from bluei.engine.git_utils import get_branch
from bluei.engine.pattern_replay import try_replay
from bluei.engine.pattern_store import ReplayOutcome
from bluei.engine.guideline_loader import load_authoritative_guidelines
from bluei.engine.repo_taste_loader import load_repo_taste
from bluei.engine.rule_family import derive_rule_family
from bluei.engine.report import infer_language_from_path
from bluei.engine.constants import (
    BASELINE_VALIDATION_CHECKS,
    CLAUDE_REQUIRED_RULES,
    DEFAULT_BATCH_STATE,
)
from bluei.engine.commands.context import RunContext
from bluei.engine.commands.helpers import (
    _get_llm_fixable_rules,
    _load_batch_rules_for_args,
    _reconcile_issue_pr_link,
)
from bluei.engine.worktree import (
    create_worktree,
    hydrate_worktree,
    remove_worktree,
)


def _select_candidates(
    *,
    repo_path: Path,
    findings_file: Path,
    log_file: Path,
    gh_repo_slug: str,
    args: Any,
    issues_data: Dict[str, Any],
) -> Tuple[List[Tuple[Dict[str, Any], Finding]], int]:
    """Phase A: walk the issue queue, filter and classify each issue.

    Returns (candidates, escalated_max_retries_incremented). Mutates
    ``issues_data`` in place (refactor metadata, blocked statuses).
    """
    candidates: List[Tuple[Dict[str, Any], Finding]] = []
    escalated = 0
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
            if issue.get("status") != IssueStatus.NEEDS_HUMAN_REFACTOR_REVIEW.value:
                set_issue_status(
                    issue,
                    IssueStatus.NEEDS_HUMAN_REFACTOR_REVIEW.value,
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
                pass
            elif classify_finding(finding) == RefactorClass.CONTEXTUAL_FIX:
                pass
            else:
                if issue.get("status") != IssueStatus.NEEDS_HUMAN_NOT_FIXABLE.value:
                    set_issue_status(
                        issue,
                        IssueStatus.NEEDS_HUMAN_NOT_FIXABLE.value,
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

        _consec_escalated = check_finding_escalation_before_fix(
            issue=issue,
            issues_data=issues_data,
            consecutive_threshold=args.max_fix_attempts_per_issue,
            escalation_config=None,
            log_file=log_file,
        )
        if _consec_escalated:
            if (
                issue.get("status")
                != IssueStatus.NEEDS_HUMAN_MAX_RETRIES_EXCEEDED.value
            ):
                _failed = count_failed_fix_attempts(issue)
                set_issue_status(
                    issue,
                    IssueStatus.NEEDS_HUMAN_MAX_RETRIES_EXCEEDED.value,
                    f"escalated: {_failed} consecutive fix failures (threshold: {args.max_fix_attempts_per_issue})",
                )
                escalated += 1
            continue

        failed_attempts = count_failed_fix_attempts(issue)
        if failed_attempts >= args.max_fix_attempts_per_issue:
            if (
                issue.get("status")
                != IssueStatus.NEEDS_HUMAN_MAX_RETRIES_EXCEEDED.value
            ):
                set_issue_status(
                    issue,
                    IssueStatus.NEEDS_HUMAN_MAX_RETRIES_EXCEEDED.value,
                    f"exceeded max fix attempts ({failed_attempts}/{args.max_fix_attempts_per_issue})",
                )
                escalated += 1
                _append_text(
                    log_file,
                    f"escalation: issue={issue.get('issue_id')} finding_id={finding.finding_id} "
                    f"exceeded max_fix_attempts_per_issue ({failed_attempts}/{args.max_fix_attempts_per_issue}) "
                    f"-> marking as {IssueStatus.NEEDS_HUMAN_MAX_RETRIES_EXCEEDED.value}",
                )
            continue

        candidates.append((issue, finding))

    return candidates, escalated


class _BreakLoop(Exception):
    """Internal signal: ``_process_one_issue`` uses this to break the orchestrator's
    per-issue loop, replicating the original ``break`` statements.
    """


@dataclass
class FinalizeResult:
    """Result of PR finalization.

    Attributes:
        success: Whether the PR was created successfully (or no_changes).
        pr_number: PR number if created.
        pr_url: PR URL if created.
        run_status: Human-readable status string.
        should_break: If True, caller should raise _BreakLoop.
        fixes_verified_delta: Change to fixes_verified counter.
        fixes_failed_verification_delta: Change to fixes_failed_verification counter.
        created_prs_delta: Change to created_prs counter.
        open_prs_delta: Change to open_prs counter.
        blocked_reasons_additions: New entries for blocked_reasons list.
    """

    success: bool
    pr_number: Optional[int] = None
    pr_url: str = ""
    run_status: str = ""
    should_break: bool = False
    fixes_verified_delta: int = 0
    fixes_failed_verification_delta: int = 0
    created_prs_delta: int = 0
    open_prs_delta: int = 0
    blocked_reasons_additions: List[str] = field(default_factory=list)


@dataclass
class CounterDeltas:
    """Snapshot of mutable counters returned by _process_one_issue.

    Successor to the untyped Dict[str, int] previously returned. Field names
    match RunContext counter fields for straightforward accumulation:
    ctx.created_prs += deltas.created_prs, etc.
    """

    created_prs: int = 0
    open_prs: int = 0
    fix_attempts: int = 0
    fixes_verified: int = 0
    fixes_failed_verification: int = 0
    claude_invocations: int = 0
    deterministic_invocations: int = 0
    blocked_reasons: List[str] = field(default_factory=list)


@dataclass
class _IssueCounters:
    """Mutable counter state for _process_one_issue.

    Initialized from RunContext, mutated in place by the function body,
    converted to CounterDeltas via to_deltas() for the return value.
    Replaces 8 individual local variables + the _counter_snapshot helper.
    """

    created_prs: int = 0
    open_prs: int = 0
    fix_attempts: int = 0
    fixes_verified: int = 0
    fixes_failed_verification: int = 0
    claude_invocations: int = 0
    deterministic_invocations: int = 0
    blocked_reasons: List[str] = field(default_factory=list)

    @classmethod
    def from_ctx(cls, ctx: "RunContext") -> "_IssueCounters":
        return cls(
            created_prs=ctx.created_prs,
            open_prs=ctx.open_prs,
            fix_attempts=ctx.fix_attempts,
            fixes_verified=ctx.fixes_verified,
            fixes_failed_verification=ctx.fixes_failed_verification,
            claude_invocations=ctx.claude_invocations,
            deterministic_invocations=ctx.deterministic_invocations,
            blocked_reasons=list(ctx.blocked_reasons),
        )

    def to_deltas(self) -> CounterDeltas:
        return CounterDeltas(
            created_prs=self.created_prs,
            open_prs=self.open_prs,
            fix_attempts=self.fix_attempts,
            fixes_verified=self.fixes_verified,
            fixes_failed_verification=self.fixes_failed_verification,
            claude_invocations=self.claude_invocations,
            deterministic_invocations=self.deterministic_invocations,
            blocked_reasons=list(self.blocked_reasons),
        )


def _finalize_pr_for_issue(
    *,
    issue: Dict[str, Any],
    issue_github: Dict[str, Any],
    issue_number: Optional[int],
    issue_url: str,
    finding: Finding,
    worktree_path: Path,
    worktree_branch: str,
    repo_path: Path,
    gh_repo_slug: str,
    log_file: Path,
    args: Any,
    state: Dict[str, Any],
    safety_config: Optional[Dict[str, Any]] = None,
    repo_config: Optional[Dict[str, Any]] = None,
) -> FinalizeResult:
    """Commit, push, create PR, and link to issue after a successful fix verification.

    Returns a FinalizeResult with counter deltas. The caller updates its own
    counters from the result and checks should_break before raising _BreakLoop.
    """
    set_issue_status(
        issue,
        "resolved_verified",
        "detector no longer firing after fix + validation",
    )

    pr_number: Optional[int] = None
    pr_url = ""

    if args.live_github_actions:
        commit_message = f"fix(bluei): {finding.rule} [{finding.finding_id[:8]}]"
        commit_result = git_commit_all(
            worktree_path,
            commit_message,
            log_file=log_file,
            dry_run=args.dry_run,
        )
        if commit_result == "no_changes":
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
            return FinalizeResult(
                success=True,
                run_status="resolved-verified-noop",
                fixes_verified_delta=1,
            )
        if commit_result != "committed":
            run_status = IssueStatus.NEEDS_HUMAN_COMMIT_FAILED.value
            set_issue_status(issue, "fix_failed_verification", run_status)
            return FinalizeResult(
                success=False,
                run_status=run_status,
                should_break=True,
                fixes_failed_verification_delta=1,
                blocked_reasons_additions=[run_status],
            )

        pushed = git_push_branch(
            worktree_path,
            worktree_branch,
            log_file=log_file,
            dry_run=args.dry_run,
            safety_config=safety_config,
        )
        if not pushed:
            run_status = IssueStatus.NEEDS_HUMAN_PUSH_FAILED.value
            set_issue_status(issue, "fix_failed_verification", run_status)
            return FinalizeResult(
                success=False,
                run_status=run_status,
                should_break=True,
                fixes_failed_verification_delta=1,
                blocked_reasons_additions=[run_status],
            )

        pr_result = create_or_update_github_pr(
            repo_slug=gh_repo_slug,
            finding=finding,
            branch=worktree_branch,
            issue_number=issue_number,
            dry_run=args.dry_run,
            log_file=log_file,
            cwd=worktree_path,
            safety_config=safety_config,
            repo_config=repo_config,
        )
        pr_number = (
            pr_result.get("number") if pr_result.get("number") is not None else None
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
                cwd=worktree_path,
            )

    if pr_number is not None or not args.live_github_actions:
        set_issue_status(issue, "pr_opened", "autofix PR created from issue queue")

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

    run_status = (
        "pr-live-created"
        if args.live_github_actions
        else "pr-simulated-resolved-verified"
    )
    return FinalizeResult(
        success=True,
        pr_number=pr_number,
        pr_url=pr_url,
        run_status=run_status,
        fixes_verified_delta=1,
        created_prs_delta=1,
        open_prs_delta=1,
    )


@dataclass
class _WorktreeSetup:
    """Result of Phase 2: worktree path/branch handed to subsequent phases."""

    worktree_path: Path
    worktree_branch: str
    existing_pr_for_repair: Optional[Dict[str, Any]] = None


def _resolve_github(
    ctx: RunContext,
    counters: _IssueCounters,
    issue: Dict[str, Any],
    finding: Finding,
) -> bool:
    """Phase 1: Resolve GitHub issue + check for existing PR.

    Creates/updates the GitHub issue when live actions are on and no issue
    number is known, then looks for an existing PR for the finding. When an
    open PR exists that is not under repair, short-circuits the whole pipeline.

    Returns True if the orchestrator should short-circuit (existing open PR
    found, no repair needed); False to continue to worktree setup. Mutates the
    ``issue`` dict in place (issue_number/issue_url/PR metadata + a transient
    ``_existing_pr_for_repair`` entry consumed by Phase 2).
    """
    args = ctx.args
    gh_repo_slug = ctx.gh_repo_slug
    repo_path = ctx.repo_path
    log_file = ctx.log_file
    state = ctx.state

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
                return True
        elif existing_pr:
            _append_text(
                log_file,
                f"pr-cycle: ignoring closed linked PR #{existing_pr.get('number')} for finding={finding.finding_id}",
            )

    issue["_existing_pr_for_repair"] = existing_pr_for_repair
    return False


def _setup_worktree(
    ctx: RunContext,
    counters: _IssueCounters,
    idx: int,
    issue: Dict[str, Any],
    finding: Finding,
    existing_pr_for_repair: Optional[Dict[str, Any]],
) -> Optional[_WorktreeSetup]:
    """Phase 2: Compute branch, create + hydrate the worktree.

    Returns None when worktree creation failed (short-circuit with a blocked
    reason recorded on ``counters``); otherwise returns a ``_WorktreeSetup``
    describing the path/branch for the subsequent phases.
    """
    args = ctx.args
    worktree_root = ctx.worktree_root
    repo_path = ctx.repo_path
    log_file = ctx.log_file
    issue_github = issue.setdefault("github", {})

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
    worktree_path = worktree_root.resolve() / f"bluei-{ts}-{idx}-{finding_suffix}"

    wt_result = create_worktree(
        repo_path=repo_path,
        branch=worktree_branch,
        worktree_path=worktree_path,
        log_file=log_file,
    )
    if not wt_result.success:
        counters.blocked_reasons.append("failed-to-create-worktree")
        return None

    hydrate_worktree(
        repo_path=repo_path, worktree_path=worktree_path, log_file=log_file
    )

    return _WorktreeSetup(
        worktree_path=worktree_path,
        worktree_branch=worktree_branch,
        existing_pr_for_repair=existing_pr_for_repair,
    )


def _process_one_issue(
    ctx: RunContext,
    idx: int,
    issue: Dict[str, Any],
    finding: Finding,
    baseline_results: Dict[str, Dict[str, Any]],
) -> CounterDeltas:
    """Phase D+E: process a single (issue, finding) through worktree setup,
    fix dispatch, verification, and PR publication.

    Counter semantics are preserved exactly: this function reads initial
    counter values from ``ctx``, mutates them via local rebinding, and returns
    the new values as a ``CounterDeltas`` snapshot. The orchestrator's locals
    are then updated from the return value.

    Raises ``_BreakLoop`` to signal that the orchestrator should stop iterating
    (replaces the original ``break`` statements).
    """
    repo_path = ctx.repo_path
    findings_file = ctx.findings_file
    log_file = ctx.log_file
    worktree_root = ctx.worktree_root
    gh_repo_slug = ctx.gh_repo_slug
    docs_index_file = ctx.docs_index_file
    lessons_file = ctx.lessons_file
    args = ctx.args
    state = ctx.state
    pattern_store = ctx.pattern_store
    cost_tracker = ctx.cost_tracker
    PER_REPO_BASELINE_CHECKS = ctx.PER_REPO_BASELINE_CHECKS
    counters = _IssueCounters.from_ctx(ctx)
    safety_config = getattr(ctx, "safety_config", None)
    repo_config = getattr(ctx, "repo_config", None)

    # Phase 1: GitHub resolution (create/update issue + existing PR check).
    # Short-circuits when an existing open PR is found and not under repair.
    if _resolve_github(ctx, counters, issue, finding):
        return counters.to_deltas()

    # Re-derive issue metadata after Phase 1, which may have created/updated
    # the GitHub issue and mutated the issue dict in place. The transient
    # ``_existing_pr_for_repair`` entry is popped so it never persists.
    issue_github = issue.setdefault("github", {})
    issue_number: Optional[int] = issue_github.get("issue_number")
    issue_url: str = str(issue_github.get("issue_url") or "")
    existing_pr_for_repair: Optional[Dict[str, Any]] = issue.pop(
        "_existing_pr_for_repair", None
    )

    # Phase 2: Worktree setup (branch computation + create + hydrate).
    # Short-circuits when worktree creation fails (records blocked reason).
    wt = _setup_worktree(ctx, counters, idx, issue, finding, existing_pr_for_repair)
    if wt is None:
        return counters.to_deltas()
    worktree_path = wt.worktree_path
    worktree_branch = wt.worktree_branch

    run_status = "unknown"
    try:
        counters.fix_attempts += 1
        set_issue_status(issue, "fix_attempted", "starting sandbox autofix attempt")

        worktree_baseline_results = run_named_checks(
            repo_path=worktree_path,
            checks=PER_REPO_BASELINE_CHECKS,
            log_file=log_file,
            phase="worktree-baseline",
        )

        target_checks = build_target_checks(finding)

        # File-level checkpoint for Dry Replay (ADR-0011). try_replay below
        # mutates the worktree file; we need the pre-fix state to replay
        # alternate Patterns non-destructively after the winner is chosen.
        _dr_target_file = worktree_path / finding.path if finding.path else None
        _dr_original_content = None
        if _dr_target_file and _dr_target_file.exists():
            try:
                _dr_original_content = _dr_target_file.read_text(encoding="utf-8")
            except OSError as _dr_cp_exc:
                _append_text(
                    log_file,
                    f"dry-replay-checkpoint-failed: path={finding.path} error={type(_dr_cp_exc).__name__}",
                )
                _dr_original_content = None

        replay_succeeded = False
        replay_pid: Optional[str] = None
        replay_pattern_hint: Optional[str] = None

        if pattern_store is not None:
            _replay_t0 = time.monotonic()
            replayed, replay_pid = try_replay(
                worktree_path=worktree_path,
                finding=finding,
                store=pattern_store,
                baseline_checks=PER_REPO_BASELINE_CHECKS,
                log_file=log_file,
                record_outcome=False,
            )
            _replay_latency_ms = int((time.monotonic() - _replay_t0) * 1000)
            if replayed:
                replay_succeeded = True
                # record_outcome=False deferred all internal recording; record HIT explicitly
                if replay_pid is not None:
                    pattern_store.record_replay_outcome(replay_pid, ReplayOutcome.HIT)
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
                ctx.ledger_records.append(
                    {
                        "cycle": args.run_phase,
                        "finding_id": finding.finding_id,
                        "rule": finding.rule,
                        "stages_tried": [],
                        "final_stage": "pattern-replay",
                        "outcome": "resolved_deterministic",
                        "via": "standalone-replay",
                        "pattern_id": replay_pid,
                        "latency_ms": _replay_latency_ms,
                        "timestamp": now_iso(),
                    }
                )
            elif replay_pid is not None:
                from bluei.engine.pattern_replay import format_pattern_hint

                pattern = pattern_store.get_pattern(replay_pid)
                if pattern is not None:
                    replay_pattern_hint = format_pattern_hint(pattern)

        if not replay_succeeded:
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
                    ledger_path=ctx.state_file.parent / "cascade_resolutions.jsonl",
                    cycle=args.run_phase,
                    ledger_records=ctx.ledger_records,
                    run_id=ctx.run_id,
                    cost_tracker=ctx.cost_tracker,
                    governance_state=ctx.governance_state,
                )
                if applied:
                    run_status = "fix-applied:cascade"
                    counters.deterministic_invocations += 1
                    _append_text(
                        log_file,
                        f"cascade-fix: succeeded finding_id={finding.finding_id} rule={finding.rule}",
                    )
                else:
                    run_status = "fix-noop:cascade-exhausted"
                    counters.fixes_failed_verification += 1
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
                    return counters.to_deltas()
            else:
                llm_rules = _get_llm_fixable_rules()
                is_llm_fixable = (
                    not finding.safe_to_autofix and finding.rule in llm_rules
                )
                use_claude_engine = (
                    args.fix_engine == FixEngine.CLAUDE.value
                    or finding.rule in CLAUDE_REQUIRED_RULES
                    or is_llm_fixable
                )
                if use_claude_engine:
                    counters.claude_invocations += 1
                else:
                    counters.deterministic_invocations += 1
                extra_prompt = (
                    llm_rules.get(finding.rule, {}).get("prompt_hint")
                    if is_llm_fixable
                    else None
                )
                learned_patterns = replay_pattern_hint
                guideline_lang = getattr(finding, "language", None) or (
                    infer_language_from_path(finding.path, fallback="all")
                )
                authoritative_guidelines = load_authoritative_guidelines(
                    finding.rule, guideline_lang
                )
                framework = repo_config.framework if repo_config else None
                repo_taste = load_repo_taste(
                    framework,
                    guideline_lang,
                    rule_family=derive_rule_family(finding.rule),
                )

                if use_claude_engine and cost_tracker.exceeded_limit():
                    _append_text(
                        log_file,
                        f"cost-limit: skipping claude fix for {finding.finding_id} "
                        f"({finding.rule}) — hard limit (${cost_tracker.cycle_total():.2f}) reached",
                    )
                    run_status = "fix-skipped:cost-limit-reached"
                    set_issue_status(issue, "fix_skipped", run_status)
                    counters.fixes_failed_verification += 1
                    return counters.to_deltas()

                if use_claude_engine:
                    rc, claude_output, prompt_file = apply_claude_fix(
                        ClaudeFixRequest(
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
                            extra_prompt=extra_prompt,
                            pattern_store_path=Path(args.pattern_store_path)
                            if args.pattern_store_path
                            else None,
                            learned_patterns=learned_patterns,
                            authoritative_guidelines=authoritative_guidelines,
                            repo_taste=repo_taste,
                        ),
                    )
                    model_name = "claude-sonnet-4"
                    cost_tracker.record_invocation(
                        model=model_name,
                        input_tokens=3000,
                        output_tokens=300,
                    )
                    if rc != 0:
                        run_status = "fix-failed-verification:claude-command-failed"
                        set_issue_status(issue, "fix_failed_verification", run_status)
                        counters.fixes_failed_verification += 1
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
                        return counters.to_deltas()
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
                        counters.fixes_failed_verification += 1
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
                        return counters.to_deltas()

        # Dry Replay phase (ADR-0011) — counterfactual evidence collection for
        # matched-but-not-selected Patterns. Restores original → applies each
        # candidate → validates → records outcome → restores winner. Worktree
        # state is unchanged after this phase. Guarded so it stays inert when
        # learning is paused, no pattern store, or no checkpointed content.
        if (
            ctx.learning_mode != "paused"
            and _dr_original_content is not None
            and pattern_store is not None
            and finding.path
        ):
            _dr_target = worktree_path / finding.path
            _dr_winner_content = (
                _dr_target.read_text(encoding="utf-8") if _dr_target.exists() else None
            )

            if _dr_winner_content is not None:
                from bluei.engine.dry_replay import (
                    collect_dry_replay_candidates,
                    run_dry_replay,
                )

                _dr_winner_pid = replay_pid if replay_succeeded else None
                _dr_candidates = collect_dry_replay_candidates(
                    finding,
                    pattern_store,
                    ctx.governance_state,
                    winner_pattern_id=_dr_winner_pid,
                )

                _dr_cap = 20  # default; TODO: read from config
                _dr_path = ctx.state_file.parent / "dry_replay.jsonl"

                _dr_count = run_dry_replay(
                    finding,
                    _dr_candidates,
                    worktree_path,
                    _dr_original_content,
                    _dr_winner_content,
                    PER_REPO_BASELINE_CHECKS,
                    log_file,
                    ctx.run_id,
                    _dr_path,
                    cap=_dr_cap,
                )

                # cap-hit tracking surfaced to finalize
                ctx._dry_replay_performed = (
                    getattr(ctx, "_dry_replay_performed", 0) + _dr_count
                )
                ctx._dry_replay_capped = len(_dr_candidates) > _dr_cap

        files_changed, loc_diff = diff_stats(worktree_path)
        _append_text(
            log_file,
            f"fix-scope-stats: files_changed={files_changed} loc_diff={loc_diff}",
        )

        if files_changed == 0 and loc_diff == 0:
            vfc_result = verify_fix_closed(
                worktree_path,
                finding,
                log_file,
                docs_index_file=docs_index_file,
            )
            verified_without_changes = vfc_result.is_closed
            if verified_without_changes:
                set_issue_status(
                    issue,
                    "resolved_verified",
                    "finding already closed on branch; no code change needed",
                )
                counters.fixes_verified += 1
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
                return counters.to_deltas()

        if files_changed > args.max_files_changed or loc_diff > args.max_loc_diff:
            run_status = IssueStatus.NEEDS_HUMAN_SCOPE_LIMIT_EXCEEDED.value
            counters.blocked_reasons.append(run_status)
            set_issue_status(issue, "fix_failed_verification", run_status)
            counters.fixes_failed_verification += 1
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
            # Signal orchestrator to break out of the loop (preserves original break).
            raise _BreakLoop()

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
            run_status = (
                f"{IssueStatus.NEEDS_HUMAN_VALIDATION_FAILED.value}:{validation_reason}"
            )
            counters.blocked_reasons.append(run_status)
            set_issue_status(issue, "fix_failed_verification", run_status)
            counters.fixes_failed_verification += 1
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
            raise _BreakLoop()

        vfc_result = verify_fix_closed(
            worktree_path, finding, log_file, docs_index_file=docs_index_file
        )
        verified = vfc_result.is_closed
        if not verified:
            run_status = "fix-failed-verification"
            set_issue_status(
                issue,
                "fix_failed_verification",
                "detector still firing after fix + validation",
            )
            counters.fixes_failed_verification += 1
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
            return counters.to_deltas()

        finalize_result = _finalize_pr_for_issue(
            issue=issue,
            issue_github=issue_github,
            issue_number=issue_number,
            issue_url=issue_url,
            finding=finding,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            repo_path=repo_path,
            gh_repo_slug=gh_repo_slug,
            log_file=log_file,
            args=args,
            state=state,
            safety_config=safety_config,
            repo_config=repo_config,
        )
        counters.fixes_verified += finalize_result.fixes_verified_delta
        counters.fixes_failed_verification += (
            finalize_result.fixes_failed_verification_delta
        )
        counters.created_prs += finalize_result.created_prs_delta
        counters.open_prs += finalize_result.open_prs_delta
        counters.blocked_reasons.extend(finalize_result.blocked_reasons_additions)
        run_status = finalize_result.run_status
        if finalize_result.should_break:
            raise _BreakLoop()

    finally:
        remove_worktree(
            worktree_path=worktree_path,
            repo_path=repo_path,
            branch=worktree_branch,
            delete_branch=not args.live_github_actions,
            log_file=log_file,
        )
        _append_text(log_file, f"cleanup: branch={worktree_branch} status={run_status}")

    return counters.to_deltas()


def run_pr_cycle_phase(*args, **kwargs) -> Dict[str, Any]:
    """Run the PR cycle: queue candidates, apply fixes, create PRs."""
    if args:
        ctx = args[0]
    else:
        ctx = RunContext(
            args=kwargs["args"],
            repo_path=kwargs["repo_path"],
            findings_file=kwargs["findings_file"],
            log_file=kwargs["log_file"],
            worktree_root=kwargs["worktree_root"],
            gh_repo_slug=kwargs["gh_repo_slug"],
            review_state_file=kwargs["review_state_file"],
            docs_index_file=kwargs["docs_index_file"],
            lessons_file=kwargs["lessons_file"],
            state=kwargs["state"],
            issues_data=kwargs["issues_data"],
            eligible_findings=kwargs["eligible_findings"],
            findings=kwargs["findings"],
            PER_REPO_BASELINE_CHECKS=kwargs["PER_REPO_BASELINE_CHECKS"],
            cost_tracker=kwargs["cost_tracker"],
            pattern_store=kwargs["pattern_store"],
            created_prs=kwargs["created_prs"],
            open_prs=kwargs["open_prs"],
            fix_attempts=kwargs["fix_attempts"],
            fixes_verified=kwargs["fixes_verified"],
            fixes_failed_verification=kwargs["fixes_failed_verification"],
            issues_escalated_max_retries=kwargs["issues_escalated_max_retries"],
            claude_invocations=kwargs["claude_invocations"],
            deterministic_invocations=kwargs["deterministic_invocations"],
            blocked_reasons=kwargs["blocked_reasons"],
        )
    repo_path = ctx.repo_path
    findings_file = ctx.findings_file
    log_file = ctx.log_file
    gh_repo_slug = ctx.gh_repo_slug
    review_state_file = ctx.review_state_file
    args = ctx.args
    state = ctx.state
    issues_data = ctx.issues_data
    eligible_findings = ctx.eligible_findings
    findings = ctx.findings
    PER_REPO_BASELINE_CHECKS = ctx.PER_REPO_BASELINE_CHECKS
    created_prs = ctx.created_prs
    open_prs = ctx.open_prs
    fix_attempts = ctx.fix_attempts
    fixes_verified = ctx.fixes_verified
    fixes_failed_verification = ctx.fixes_failed_verification
    issues_escalated_max_retries = ctx.issues_escalated_max_retries
    claude_invocations = ctx.claude_invocations
    deterministic_invocations = ctx.deterministic_invocations
    blocked_reasons = ctx.blocked_reasons

    # Governance State projection (ADR-0008): read-time view of the
    # approval_records.jsonl trail. Empty until the first record is written
    # (substrate no-op — is_governance_active returns True for all refs).
    from bluei.engine.governance import project_governance_state, resolve_learning_mode
    from bluei.engine.jsonl import read_jsonl

    _approval_records = read_jsonl(ctx.state_file.parent / "approval_records.jsonl")
    ctx.governance_state = project_governance_state(_approval_records)

    # Repo Taste Profile (ADR-0017 principle): resolve the repo's
    # RepoConfig.framework once at cycle start so the taste channel can read
    # it per-finding without re-loading the YAML. Function-local import
    # mirrors the load_global_config import below (no enforce_architecture
    # violation; pr_cycle is already an app.config consumer).
    try:
        from bluei.app.config import ConfigManager

        ctx.repo_config = ConfigManager().load_repo_config(ctx.repo_path.name)
    except Exception:
        ctx.repo_config = None

    # Learning mode (ADR-0013): resolve the global tri-state ceiling once at
    # cycle start. Defaults to ``active`` when no config.yaml is present.
    from bluei.app.config import load_global_config

    ctx.learning_mode = resolve_learning_mode(load_global_config())

    queue_candidates, _escalated = _select_candidates(
        repo_path=repo_path,
        findings_file=findings_file,
        log_file=log_file,
        gh_repo_slug=gh_repo_slug,
        args=args,
        issues_data=issues_data,
    )
    issues_escalated_max_retries += _escalated

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

        _solo_items = []
        for _bg in _batch_groups:
            if _bg.is_solo:
                _solo_items.append((_bg.issues[0], _bg.findings[0]))
            else:
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

        _iteration_items = _solo_items
    else:
        _iteration_items = queue_candidates

    # Per-issue processing: delegate to _process_one_issue (extracted helper).
    for idx, (issue, finding) in enumerate(_iteration_items, start=1):
        if created_prs >= args.max_prs_per_run:
            break
        ok, reason = guard_open_prs(open_prs, args.open_prs_cap)
        _append_text(log_file, reason)
        if not ok:
            blocked_reasons.append(reason)
            break
        try:
            _delta = _process_one_issue(
                ctx,
                idx,
                issue,
                finding,
                baseline_results,
            )
            created_prs = _delta.created_prs
            open_prs = _delta.open_prs
            fix_attempts = _delta.fix_attempts
            fixes_verified = _delta.fixes_verified
            fixes_failed_verification = _delta.fixes_failed_verification
            claude_invocations = _delta.claude_invocations
            deterministic_invocations = _delta.deterministic_invocations
            blocked_reasons = _delta.blocked_reasons
        except _BreakLoop:
            break
    # end for

    # SPRT check (ADR-0012): batch-evaluate LLR for every Pattern that had
    # Dry Replays this cycle. Runs in both ``active`` and ``audit_only`` modes;
    # in ``audit_only`` the decisions are computed but NOT written (no
    # ApprovalRecords appended). ``paused`` skips everything.
    if ctx.learning_mode != "paused":
        from bluei.engine.sprt import run_sprt_check
        from bluei.engine.jsonl import append_jsonl as _sprt_append_jsonl

        _dr_path = ctx.state_file.parent / "dry_replay.jsonl"
        _ar_path = ctx.state_file.parent / "approval_records.jsonl"

        if _dr_path.exists():
            _dr_records = read_jsonl(_dr_path, skip_errors=True)
            _pattern_ids = list(
                set(r.get("pattern_id") for r in _dr_records if r.get("pattern_id"))
            )

            _new_records = run_sprt_check(
                _pattern_ids, _dr_path, _ar_path, load_global_config()
            )

            # Only write decisions in active mode; audit_only computes but
            # does not fire (ADR-0013).
            if ctx.learning_mode == "active":
                for _record in _new_records:
                    _sprt_append_jsonl(_ar_path, _record)

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
