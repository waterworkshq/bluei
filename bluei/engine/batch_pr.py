"""batch_pr.py — Batch PR orchestrator and re-export facade.

Phase 1: Pure grouping logic → bluei.engine.batch_grouping
Phase 2: Batch fix execution → bluei.engine.batch_execution
Phase 2: Batch PR creation/linking → bluei.engine.batch_pr_creation
Phase 3: Split/recovery logic → bluei.engine.batch_recovery
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple, TYPE_CHECKING
from uuid import uuid4

from bluei.engine.models import (
    BatchGroup,
    BatchStatus,
)
from bluei.engine.worktree import (
    hydrate_worktree,
    remove_worktree,
)
from bluei.engine.batch_recovery import (
    should_split_batch,
    commit_partial_batch,
    split_batch,
    split_on_conflicts,
    handle_batch_failure,
    recover_interrupted_batch,
    _batch_from_record,
)

if TYPE_CHECKING:
    from bluei.engine.model_discovery import ModelDiscovery
    from bluei.engine.model_governor import SelectionFn

logger = logging.getLogger(__name__)


# Phase 1 extraction (refactor/god-batch-pr-decomp): pure grouping functions moved to
# bluei.engine.batch_grouping. Re-exported here for backward compat — preserves all
# existing imports and test patch targets like `bluei.engine.batch_pr.group_findings_for_batch`.
from bluei.engine.batch_grouping import (  # noqa: F401
    SEVERITY_ORDER,
    _find_issue_for_finding,
    _severity_batch_cap,
    check_batch_conflicts,
    chunk_findings,
    group_findings_for_batch,
    is_isolated,
    load_batch_rules,
    rule_matches,
)

# Phase 2 extraction: execution functions moved to bluei.engine.batch_execution
# (apply_batch_fixes, _apply_single_fix, verify_finding_closed, _create_worktree).
# Re-exported here for backward compat with imports and test patch targets.
#
# Note: patches on `bluei.engine.batch_pr.apply_batch_fixes` and `_create_worktree`
# still affect calls from process_batch (below) because process_batch resolves
# these names through this module's namespace. Patches on `_apply_single_fix`
# and `verify_finding_closed` only affect calls from inside batch_execution if
# retargeted to `bluei.engine.batch_execution.<name>` — see commit message and
# docs/plans/god-module-decomp/batch_pr.md §4.2 for the facade-patch gotcha.
from bluei.engine.batch_execution import (  # noqa: F401
    _apply_single_fix,
    _create_worktree,
    apply_batch_fixes,
    verify_finding_closed,
)

# Phase 2 extraction: PR creation functions moved to bluei.engine.batch_pr_creation.
# Re-exported here for backward compat. Patches on these names still affect calls
# from process_batch (below) because process_batch resolves them via this module.
from bluei.engine.batch_pr_creation import (  # noqa: F401
    create_batch_pr,
    link_issues_to_batch_pr,
)


def parse_pr_count(detail: Optional[str]) -> int:
    """Number of PRs a successful process_batch detail represents.

    "split-and-retried:N" carries the count of PRs its sub-batches
    created; every other success detail is a single PR.
    """
    if detail and detail.startswith("split-and-retried:"):
        try:
            return max(1, int(detail.split(":", 1)[1]))
        except ValueError:
            return 1
    return 1


def process_batch(
    batch: BatchGroup,
    repo_path: Path,
    args,
    log_file: Path,
    safety_config: Optional[dict] = None,
    # D7-C-1/H-1: production passes the RepoConfig DATACLASS stored on
    # RunContext.repo_config; resolve_base_branch accepts both shapes.
    repo_config: Any = None,
    *,
    selection_fn: Optional[SelectionFn] = None,
    governor_ledger_path: Optional[Path] = None,
    run_id: str = "",
    discovery: Optional[ModelDiscovery] = None,
    cost_tracker: Optional["object"] = None,
    is_split_child: bool = False,
    pr_budget: Optional[list] = None,
) -> Tuple[bool, Optional[str]]:
    """Process a multi-finding batch: worktree → fixes → PR.

    Returns (success: bool, detail: str).
    success=True if a PR was created (even with partial fixes).

    For solo batches, returns (False, 'solo-delegated') so the caller
    can route to the existing single-finding path. ``is_split_child=True``
    (split-generated sub-batches) adjusts two behaviors for the recursion
    context: a solo sub-batch runs the normal batch flow (no dispatcher
    exists to re-route it), and the rule-wide duplicate-PR check is
    skipped — sibling sub-batches of one split intentionally produce
    multiple PRs for the same rule; that check guards against a LATER
    cycle re-opening a recent PR, not against same-run siblings.

    ``pr_budget`` is an optional single-element list holding the remaining
    PR slots for the run; when present, the split loop stops launching
    sub-batches once it is exhausted, so one split cannot overshoot the
    cycle's PR caps. Children share the same holder, so nested splits
    draw from the same budget.
    """
    from bluei.engine.git_ops import git_commit_all, git_push_branch
    from bluei.engine.state import _append_text, save_batch_record
    from bluei.engine.constants import DEFAULT_BATCH_STATE, DEFAULT_WORKTREE_ROOT

    # Solo batches should use the existing single-finding path — unless the
    # solo batch was generated by a split, where no dispatcher will re-route
    # it (see docstring).
    if batch.is_solo and not is_split_child:
        return False, "solo-delegated"

    # ── Multi-finding batch ──
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uniq = uuid4().hex[:6]
    rule_short = batch.rule_pattern.replace("ruff-", "")[:8]
    branch = f"qa/batch-{rule_short}-{ts}-{uniq}"
    worktree_root = Path(getattr(args, "worktree_root", str(DEFAULT_WORKTREE_ROOT)))
    worktree_path = worktree_root.resolve() / f"qa-batch-{batch.batch_id}"

    batch.branch = branch
    batch.worktree_path = worktree_path
    batch.status = BatchStatus.FIXING.value

    _append_text(
        log_file,
        f"batch: processing {batch.batch_id} findings={len(batch.findings)} "
        f"rule={batch.rule_pattern} branch={branch}",
    )

    # Derive repo slug — needed for PR creation (not needed in dry-run)
    if not getattr(args, "dry_run", True):
        from bluei.engine.gh import get_origin_url, parse_github_repo

        origin_url = get_origin_url(repo_path)
        gh_owner, gh_name = parse_github_repo(origin_url)
        repo_slug = f"{gh_owner}/{gh_name}" if gh_owner and gh_name else ""

        if not repo_slug:
            batch.status = BatchStatus.FAILED.value
            _append_text(
                log_file,
                f"batch-abort: {batch.batch_id} no repo slug could be derived from {origin_url}",
            )
            return False, "no-repo-slug"

        # Dedup check: skip if an equivalent batch PR is already open.
        # Split children are exempt — siblings of one split intentionally
        # open multiple PRs for the same rule (the check guards against a
        # later cycle re-opening a recent PR).
        from bluei.engine.gh import find_batch_pr_by_rule

        dup_pr = (
            None
            if is_split_child
            else find_batch_pr_by_rule(
                repo_slug,
                batch.rule_pattern,
                cwd=repo_path,
                max_age_hours=getattr(args, "batch_dedup_hours", 24),
            )
        )
        if dup_pr is not None:
            _append_text(
                log_file,
                f"batch-skip-duplicate: {batch.batch_id} existing PR #{dup_pr['number']} "
                f"title={dup_pr.get('title', '')} url={dup_pr.get('url', '')}",
            )
            batch.status = BatchStatus.SKIPPED.value
            _append_text(
                log_file,
                f"batch-abort: {batch.batch_id} duplicate PR #{dup_pr['number']}",
            )
            return False, f"duplicate-existing-pr-#{dup_pr['number']}"

    from bluei.engine.worktree_checkpoint import CheckpointError, CheckpointRestoreError

    # Create shared worktree
    if not _create_worktree(repo_path, worktree_path, branch, log_file):
        batch.status = BatchStatus.FAILED.value
        return False, "worktree-creation-failed"

    preserve_worktree = False
    try:
        # Hydrate dependencies (e.g. node_modules symlink)
        hydrate_worktree(repo_path, worktree_path, log_file=log_file)

        # Apply all fixes sequentially
        try:
            successes, failures = apply_batch_fixes(
                batch=batch,
                worktree_path=worktree_path,
                repo_path=repo_path,
                args=args,
                log_file=log_file,
                selection_fn=selection_fn,
                governor_ledger_path=governor_ledger_path,
                run_id=run_id,
                discovery=discovery,
                cost_tracker=cost_tracker,
            )
        except subprocess.TimeoutExpired:
            # A timed-out Claude fix re-raises from the per-finding loop: its
            # partial edits may sit in this batch's worktree, so the BATCH is
            # abandoned (no commit, no PR — the worktree is discarded) while
            # the cycle continues with the remaining batches and findings.
            batch.status = BatchStatus.FAILED.value
            _append_text(
                log_file,
                f"batch-aborted: {batch.batch_id} reason=claude-timeout "
                "(worktree discarded; no commit, no PR)",
            )
            return False, "claude-timeout-batch-aborted"

        _append_text(
            log_file,
            f"batch-fixes: {batch.batch_id} successes={successes} failures={failures}",
        )

        # No successful fixes → abort
        if successes == 0:
            batch.status = BatchStatus.FAILED.value
            _append_text(log_file, f"batch-abort: {batch.batch_id} no successful fixes")
            return False, "no-successful-fixes"

        # Check if too many failures — split if needed
        max_depth = getattr(args, "max_split_depth", 3)
        split_warranted = should_split_batch(batch, max_depth=max_depth)
        if split_warranted and getattr(args, "batch_pr_split_on_failure", True):
            batch.retry_count += 1
            sub_batches = handle_batch_failure(
                batch, repo_path, args, log_file, safety_config=safety_config
            )
            # Process sub-batches recursively; propagate their results —
            # "split-and-retried" is success only if at least one sub-batch
            # produced a PR. Reporting success when every sub-batch failed
            # (e.g. each timed out) makes the cycle count a PR that was
            # never created and can consume the PR cap for nothing.
            sub_results = []
            for sub_batch in sub_batches:
                if pr_budget is not None and pr_budget[0] <= 0:
                    _append_text(
                        log_file,
                        f"batch-skip: {sub_batch.batch_id} reason=pr-budget-exhausted",
                    )
                    continue
                _ok, _detail = process_batch(
                    sub_batch,
                    repo_path,
                    args,
                    log_file,
                    safety_config=safety_config,
                    repo_config=repo_config,
                    selection_fn=selection_fn,
                    governor_ledger_path=governor_ledger_path,
                    run_id=run_id,
                    discovery=discovery,
                    cost_tracker=cost_tracker,
                    is_split_child=True,
                    pr_budget=pr_budget,
                )
                sub_results.append((_ok, _detail))
                if (
                    pr_budget is not None
                    and _ok
                    and not _detail.startswith("split-and-retried:")
                ):
                    # A child that itself split already drew its own PRs
                    # from this shared budget inside its recursion — only
                    # draw here for children that consumed nothing
                    # themselves, or nested splits would decrement twice.
                    pr_budget[0] -= parse_pr_count(_detail)
            if sub_results and any(ok for ok, _detail in sub_results):
                # Carry the number of PRs the sub-batches actually created
                # ("split-and-retried:N", counting nested splits) so the
                # cycle's PR accounting counts them all, not one.
                n_prs = sum(
                    parse_pr_count(_detail)
                    for ok, _detail in sub_results
                    if ok
                )
                return True, f"split-and-retried:{n_prs}"
            batch.status = BatchStatus.FAILED.value
            _append_text(
                log_file,
                f"batch-abort: {batch.batch_id} split produced no successful "
                "sub-batches (no PR created)",
            )
            return False, "split-all-sub-batches-failed"
        elif (
            not split_warranted
            and batch.retry_count >= max_depth
            and failures > 0
            and failures > successes
        ):
            _append_text(
                log_file,
                f"batch: {batch.batch_id} max split depth reached, aborting",
            )
            batch.status = BatchStatus.ABORTED.value
            return False, "max-split-depth-exceeded"

        # Commit all successful changes
        commit_message = batch.pr_title()
        commit_result = git_commit_all(
            worktree_path,
            commit_message,
            log_file=log_file,
            dry_run=getattr(args, "dry_run", True),
        )
        if commit_result == "no_changes":
            batch.status = BatchStatus.FAILED.value
            _append_text(log_file, f"batch-abort: {batch.batch_id} commit=no_changes")
            return False, "commit-no-changes"

        # Push branch
        pushed = git_push_branch(
            worktree_path,
            branch,
            log_file=log_file,
            dry_run=getattr(args, "dry_run", True),
            safety_config=safety_config,
        )
        if not pushed:
            batch.status = BatchStatus.FAILED.value
            return False, "push-failed"

        # Create PR (skip in dry-run)
        if getattr(args, "dry_run", True):
            _append_text(
                log_file,
                f"batch-dry-run: would create PR for {batch.batch_id} "
                f"branch={branch} title={commit_message}",
            )
            batch.status = BatchStatus.DRY_RUN.value
            return True, "dry-run-pr-simulated"

        try:
            pr = create_batch_pr(
                batch,
                repo_slug,
                log_file,
                safety_config=safety_config,
                repo_config=repo_config,
            )
        except RuntimeError:
            batch.status = BatchStatus.FAILED.value
            _append_text(
                log_file,
                f"batch-pr-failed: {batch.batch_id} create_batch_pr raised RuntimeError",
            )
            return False, "pr-creation-failed"

        pr_number = pr.get("number")
        pr_url = pr.get("url", "")
        batch.pr_number = pr_number
        batch.pr_url = pr_url
        batch.status = BatchStatus.PR_CREATED.value

        # Link all issues to the batch PR
        if pr_number is not None:
            link_issues_to_batch_pr(
                batch=batch,
                pr_number=pr_number,
                pr_url=pr_url,
                repo_slug=repo_slug,
                repo_path=repo_path,
                log_file=log_file,
            )

        # Save batch state
        batch_state_file = getattr(args, "batch_state_file", None)
        if batch_state_file:
            save_batch_record(Path(batch_state_file), batch.to_record())

        _append_text(
            log_file,
            f"batch-success: {batch.batch_id} PR #{pr_number} "
            f"successes={successes} failures={failures}",
        )
        return True, f"pr-created-#{pr_number}"

    except (CheckpointError, CheckpointRestoreError, KeyboardInterrupt, SystemExit) as exc:
        preserve_worktree = True
        batch.status = BatchStatus.FAILED.value
        exc.add_note(f"batch worktree retained for recovery: {worktree_path}; branch={branch}")
        try:
            _append_text(log_file, f"batch-checkpoint-failure: retained worktree={worktree_path} branch={branch} error={exc}")
        except BaseException:
            pass
        raise
    finally:
        if not preserve_worktree:
            remove_worktree(
                worktree_path=worktree_path,
                repo_path=repo_path,
                branch=branch,
                delete_branch=not getattr(args, "live_github_actions", False),
                log_file=log_file,
            )
            _append_text(log_file, f"batch-cleanup: branch={branch}")
