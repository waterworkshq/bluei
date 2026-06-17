"""batch_pr.py — Batch PR grouping and execution engine.

Phase 1: Pure grouping logic (rules, isolation, chunking, conflict detection).
Phase 2: Batch fix execution (shared worktrees, sequential fixes, batch PRs).
Phase 3: Split/recovery logic and conflict detection.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import yaml

from bluei.engine.models import (
    BatchGroup,
    BatchRule,
    BatchStatus,
    Finding,
    FixEngine,
    FixResult,
    FixStatus,
    now_iso,
)
from bluei.engine.worktree import (
    create_worktree,
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

# ────────────────────────────────────────────────────────────────
# Phase 2: Batch Fix Execution
# ────────────────────────────────────────────────────────────────


def _create_worktree(
    repo_path: Path, worktree_path: Path, branch: str, log_file: Path
) -> bool:
    """Create a git worktree for batch fixes.

    Returns True if the worktree was created successfully.
    Delegates to bluei.engine.worktree.create_worktree.
    """
    result = create_worktree(
        repo_path=repo_path,
        branch=branch,
        worktree_path=worktree_path,
        log_file=log_file,
    )
    return result.success


def verify_finding_closed(
    worktree_path: Path, finding: Finding, log_file: Path
) -> bool:
    """Re-run the specific linter rule for one finding and check it's resolved.

    Uses the verify module to check if the finding is closed.
    """
    from bluei.engine.verify import verify_fix_closed

    try:
        result = verify_fix_closed(worktree_path, finding, log_file)
        return result.is_closed
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.debug(
            "verify_finding_closed: lifecycle verify failed for %s: %s %s",
            finding.finding_id,
            type(exc).__name__,
            exc,
        )
        return False


def apply_batch_fixes(
    batch: BatchGroup,
    worktree_path: Path,
    repo_path: Path,
    args,
    log_file: Path,
) -> Tuple[int, int]:
    """Apply all fixes within a shared worktree sequentially.

    For each finding in the batch:
    - If safe_to_autofix: try apply_autofix(), then apply_contextual_fix() fallback
    - If LLM-fixable: try apply_claude_fix()
    - Otherwise: skip

    Returns (successes, failures) tally.
    """
    from bluei.engine.lifecycle import apply_autofix
    from bluei.engine.state import _append_text

    successes = 0
    failures = 0

    for finding in batch.findings:
        result = _apply_single_fix(
            finding=finding,
            worktree_path=worktree_path,
            repo_path=repo_path,
            args=args,
            log_file=log_file,
        )
        batch.fix_results[finding.finding_id] = result

        if result.status == FixStatus.SUCCESS.value:
            successes += 1
            _append_text(
                log_file,
                f"batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} "
                f"path={finding.path}:{finding.line} status=success method={result.fix_method}",
            )
        elif result.status == FixStatus.SKIPPED.value:
            _append_text(
                log_file,
                f"batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} status=skipped "
                f"reason={result.error}",
            )
        else:
            failures += 1
            _append_text(
                log_file,
                f"batch-fix: finding={finding.finding_id[:8]} rule={finding.rule} "
                f"path={finding.path}:{finding.line} status=failed error={result.error}",
            )

    return successes, failures


def _apply_single_fix(
    finding: Finding,
    worktree_path: Path,
    repo_path: Path,
    args,
    log_file: Path,
) -> FixResult:
    """Apply one fix within a shared batch worktree.

    Follows the same fix strategy as the existing pr-cycle:
    1. If safe_to_autofix → apply_autofix, verify, fallback to contextual
    2. If LLM-fixable → apply_claude_fix
    3. Otherwise → skip
    """
    from bluei.engine.lifecycle import (
        ClaudeFixRequest,
        apply_autofix,
        apply_claude_fix,
    )
    from bluei.engine.validation import build_target_checks
    from bluei.engine.constants import (
        BASELINE_VALIDATION_CHECKS,
        CLAUDE_REQUIRED_RULES,
        load_llm_fixable_rules,
    )
    from bluei.engine.state import _append_text
    import subprocess

    llm_rules = load_llm_fixable_rules()
    is_llm_fixable = not finding.safe_to_autofix and finding.rule in llm_rules
    use_claude = (
        getattr(args, "fix_engine", "deterministic") == FixEngine.CLAUDE.value
        or finding.rule in CLAUDE_REQUIRED_RULES
        or is_llm_fixable
    )

    # FIX: Even in 'claude' mode, always try apply_autofix first for safe_to_autofix
    # findings. apply_autofix runs ruff --fix in seconds; Claude takes ~60s/finding
    # and cannot apply edits in non-interactive --print mode.
    if finding.safe_to_autofix:
        applied = apply_autofix(worktree_path, finding, log_file)
        if applied:
            closed = verify_finding_closed(worktree_path, finding, log_file)
            if closed:
                return FixResult(
                    finding_id=finding.finding_id,
                    status=FixStatus.SUCCESS.value,
                    diff_lines=1,
                    fix_method="autofix",
                )
            return FixResult(
                finding_id=finding.finding_id,
                status=FixStatus.FAILED.value,
                error="verification-failed",
                fix_method="autofix",
            )
        # Autofix failed or couldn't apply; try contextual fallback before Claude
        try:
            from bluei.engine.context_fix import apply_contextual_fix

            _append_text(
                log_file,
                f"batch-fix: contextual fallback for rule={finding.rule} path={finding.path}",
            )
            applied = apply_contextual_fix(
                repo_path=repo_path,
                finding=finding,
                log_file=log_file,
                worktree_path=worktree_path,
            )
            if applied:
                closed = verify_finding_closed(worktree_path, finding, log_file)
                if closed:
                    return FixResult(
                        finding_id=finding.finding_id,
                        status=FixStatus.SUCCESS.value,
                        diff_lines=1,
                        fix_method="contextual",
                    )
                return FixResult(
                    finding_id=finding.finding_id,
                    status=FixStatus.FAILED.value,
                    error="contextual-verification-failed",
                    fix_method="contextual",
                )
        except (subprocess.CalledProcessError, OSError) as exc:
            _append_text(
                log_file,
                f"batch-fix: contextual fallback exception for {finding.finding_id[:8]}: {exc}",
            )
        # safe_to_autofix finding: both autofix and contextual couldn't apply.
        # If Claude is available, try it as final fallback.
        # Otherwise return 'failed' (not 'skipped') — autofix was available but didn't work.
        if not use_claude:
            return FixResult(
                finding_id=finding.finding_id,
                status=FixStatus.FAILED.value,
                error="autofix-unavailable",
                fix_method="autofix",
            )
        # fall through to Claude path

    elif not use_claude:
        # Not safe_to_autofix and not going to Claude → nothing we can try
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.SKIPPED.value,
            error="not-llm-fixable",
            fix_method="autofix",
        )

    # Claude fix path
    target_checks = build_target_checks(finding)
    # Capture worktree state before Claude so we can detect if anything changed
    try:
        before_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(worktree_path),
            text=True,
            capture_output=True,
        )
        before_commit = before_result.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        before_commit = None

    try:
        rc, output, prompt_file = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=worktree_path,
                finding=finding,
                baseline_checks=BASELINE_VALIDATION_CHECKS,
                target_checks=target_checks,
                claude_cmd_template=args.claude_cmd_template,
                max_files_changed=args.max_files_changed,
                max_loc_diff=args.max_loc_diff,
                log_file=log_file,
            ),
        )
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        _append_text(
            log_file, f"batch-fix: claude exception for {finding.finding_id[:8]}: {exc}"
        )
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.FAILED.value,
            error=f"claude-exception: {exc}",
            fix_method="claude",
        )

    # FIX: rc=0 does not mean Claude applied a fix. In --print non-interactive mode,
    # Claude returns 0 after analyzing but cannot use Edit/Bash tools.
    # Detect this by checking (a) Claude output mentions blocked tools, or
    # (b) the worktree HEAD commit is unchanged.
    tools_blocked = output and (
        "Edit" in output
        and ("blocked" in output or "denied" in output or "cannot" in output.lower())
        or "cannot apply" in output.lower()
        or "all file-modifying tools are blocked" in output
    )

    worktree_changed = False
    if not tools_blocked and before_commit:
        try:
            after_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(worktree_path),
                text=True,
                capture_output=True,
            )
            after_commit = after_result.stdout.strip()
            worktree_changed = after_commit != before_commit
        except (subprocess.CalledProcessError, OSError):
            logger.debug("Failed to check worktree commit status")

    # Fallback: OpenCode (and some other engines) edit files in-place without
    # auto-committing. If git HEAD is unchanged, check for unstaged file diffs.
    if not tools_blocked and not worktree_changed:
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=str(worktree_path),
                text=True,
                capture_output=True,
            )
            if diff_result.stdout and diff_result.stdout.strip():
                worktree_changed = True
        except (subprocess.CalledProcessError, OSError):
            logger.debug("Failed to check worktree diff status")

    if rc == 0 and not tools_blocked and worktree_changed:
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.SUCCESS.value,
            fix_method="claude",
        )
    elif tools_blocked:
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.FAILED.value,
            error="claude-tools-blocked",
            fix_method="claude",
        )
    else:
        return FixResult(
            finding_id=finding.finding_id,
            status=FixStatus.FAILED.value,
            error=f"claude rc={rc} no-change",
            fix_method="claude",
        )


def create_batch_pr(
    batch: BatchGroup,
    repo_slug: str,
    log_file: Path,
    safety_config: Optional[dict] = None,
    repo_config: Optional[dict] = None,
) -> Dict[str, Any]:
    """Create a GitHub PR for the batch.

    Uses the standard `gh pr create` flow with batch-aware title and body.

    Returns dict with 'number' and 'url'.
    Raises RuntimeError on failure.
    """
    from bluei.engine.safety_gates import (
        check_pr_creation_allowed,
        resolve_base_branch,
    )
    from bluei.engine.utils import run_capture
    from bluei.engine.state import _append_text

    title = batch.pr_title()
    body = batch.pr_body()
    branch = batch.branch
    base_branch = resolve_base_branch(safety_config, repo_config)

    _append_text(log_file, f"batch-pr: creating PR for {batch.batch_id} title={title}")

    if safety_config:
        allowed, reason = check_pr_creation_allowed(base_branch, safety_config)
        if not allowed:
            _append_text(
                log_file,
                f"safety-block: batch PR creation blocked batch={batch.batch_id} reason={reason}",
            )
            raise RuntimeError(f"blocked-by-safety-mode: {reason}")

    rc, output = run_capture(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug,
            "--title",
            title,
            "--body",
            body,
            "--head",
            branch,
            "--base",
            base_branch,
        ],
        cwd=batch.worktree_path,
    )

    if rc != 0:
        _append_text(
            log_file,
            f"batch-pr: gh pr create failed rc={rc} output={(output or '<empty>')[:300]}",
        )
        raise RuntimeError(f"Failed to create batch PR: {output}")

    # Find the line containing the PR URL (gh may output warnings before it)
    pr_url = ""
    for line in output.strip().splitlines():
        if "/pull/" in line:
            pr_url = line.strip()
            break
    pr_number = None
    if pr_url:
        try:
            pr_number = int(pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            logger.debug("Failed to parse PR number from URL")

    _append_text(log_file, f"batch-pr: created PR #{pr_number} url={pr_url}")
    return {"number": pr_number, "url": pr_url}


def link_issues_to_batch_pr(
    batch: BatchGroup,
    pr_number: int,
    pr_url: str,
    repo_slug: str,
    repo_path: Path,
    log_file: Path,
) -> None:
    """Update all issues in the batch to point to the shared PR.

    For each issue:
    - Set issue.github['pr_number'], ['pr_url'], ['batch_id']
    - Call set_issue_status(issue, 'pr_opened', ...)
    - Comment on the GitHub issue linking to the batch PR
    """
    from bluei.engine.orchestrator import set_issue_status
    from bluei.engine.gh import gh_issue_comment
    from bluei.engine.state import _append_text

    for issue in batch.issues:
        issue_github = issue.setdefault("github", {})
        issue_github["pr_number"] = pr_number
        issue_github["pr_url"] = pr_url
        issue_github["batch_id"] = batch.batch_id

        set_issue_status(issue, "pr_opened", f"batched in PR #{pr_number}")

        issue_number = issue_github.get("issue_number")
        if issue_number is not None:
            try:
                gh_issue_comment(
                    repo_slug,
                    issue_number,
                    f"This finding has been batched into PR #{pr_number}: {pr_url}",
                    cwd=repo_path,
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                _append_text(
                    log_file,
                    f"batch-link: failed to comment on issue #{issue_number}: {exc}",
                )

        _append_text(
            log_file,
            f"batch-link: issue={issue.get('issue_id') or issue.get('id')} "
            f"linked to PR #{pr_number} batch={batch.batch_id}",
        )


# Phase 3 symbols (should_split_batch, commit_partial_batch, split_batch,
# split_on_conflicts, handle_batch_failure, recover_interrupted_batch,
# _batch_from_record) are imported from bluei.engine.batch_recovery at top.


def process_batch(
    batch: BatchGroup,
    repo_path: Path,
    args,
    log_file: Path,
    safety_config: Optional[dict] = None,
    repo_config: Optional[dict] = None,
) -> Tuple[bool, Optional[str]]:
    """Process a multi-finding batch: worktree → fixes → PR.

    Returns (success: bool, detail: str).
    success=True if a PR was created (even with partial fixes).

    For solo batches, returns (False, 'solo-delegated') so the caller
    can route to the existing single-finding path.
    """
    from bluei.engine.git_ops import git_commit_all, git_push_branch
    from bluei.engine.state import _append_text, save_batch_record
    from bluei.engine.constants import DEFAULT_BATCH_STATE, DEFAULT_WORKTREE_ROOT

    # Solo batches should use the existing single-finding path
    if batch.is_solo:
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

        # Dedup check: skip if an equivalent batch PR is already open
        from bluei.engine.gh import find_batch_pr_by_rule

        dup_pr = find_batch_pr_by_rule(
            repo_slug,
            batch.rule_pattern,
            cwd=repo_path,
            max_age_hours=getattr(args, "batch_dedup_hours", 24),
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

    # Create shared worktree
    if not _create_worktree(repo_path, worktree_path, branch, log_file):
        batch.status = BatchStatus.FAILED.value
        return False, "worktree-creation-failed"

    try:
        # Hydrate dependencies (e.g. node_modules symlink)
        hydrate_worktree(repo_path, worktree_path, log_file=log_file)

        # Apply all fixes sequentially
        successes, failures = apply_batch_fixes(
            batch=batch,
            worktree_path=worktree_path,
            repo_path=repo_path,
            args=args,
            log_file=log_file,
        )

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
            sub_batches = handle_batch_failure(batch, repo_path, args, log_file)
            # Process sub-batches recursively
            for sub_batch in sub_batches:
                process_batch(sub_batch, repo_path, args, log_file)
            return True, "split-and-retried"
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

    finally:
        # Cleanup worktree
        remove_worktree(
            worktree_path=worktree_path,
            repo_path=repo_path,
            branch=branch,
            delete_branch=not getattr(args, "live_github_actions", False),
            log_file=log_file,
        )
        _append_text(log_file, f"batch-cleanup: branch={branch}")
