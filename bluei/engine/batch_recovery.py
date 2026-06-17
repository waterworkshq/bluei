"""batch_recovery.py — Batch split, failure handling, and recovery.

Imported by bluei.engine.batch_pr (which re-exports for backward compat).
Uses deferred imports for _find_issue_for_finding and check_batch_conflicts
to avoid circular dependency with batch_pr.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.models import (
    BatchGroup,
    BatchRule,
    BatchStatus,
    Finding,
    FixResult,
    FixStatus,
    now_iso,
)
from bluei.engine.worktree import remove_worktree

logger = logging.getLogger(__name__)


def should_split_batch(batch: BatchGroup, max_depth: int = 3) -> bool:
    """Decide whether a batch should be split due to excessive failures.

    Returns True when:
    - Failure rate > 50%
    - AND batch hasn't exceeded max_split_depth

    Returns False otherwise.
    """
    # Count only attempted (non-skipped) results
    attempted = 0
    failed = 0
    for fid, result in batch.fix_results.items():
        status = (
            result.status if isinstance(result, FixResult) else result.get("status")
        )
        if status == FixStatus.SKIPPED.value:
            continue
        attempted += 1
        if status == FixStatus.FAILED.value:
            failed += 1

    if attempted == 0:
        return False

    failure_rate = failed / attempted
    if failure_rate <= 0.5:
        return False

    if batch.retry_count >= max_depth:
        return False

    return True


def commit_partial_batch(
    successful_findings: List[Finding],
    batch: BatchGroup,
    log_file: Path,
    safety_config: Optional[dict] = None,
) -> bool:
    """Commit partial results for successful findings.

    The worktree already has the successful fixes applied.
    Commits only those changes, pushes the branch, and creates a PR.

    Returns True if partial PR was created successfully.
    """
    from bluei.engine.git_ops import git_commit_all, git_push_branch
    from bluei.engine.state import _append_text

    if not successful_findings:
        return False

    if batch.worktree_path is None or batch.branch is None:
        _append_text(
            log_file, f"batch-partial: no worktree/branch for {batch.batch_id}"
        )
        return False

    # Stage only the files touched by successful findings
    from bluei.engine.utils import run_capture, run_no_capture

    successful_files = sorted({f.path for f in successful_findings})
    for filepath in successful_files:
        run_no_capture(["git", "add", filepath], cwd=batch.worktree_path)

    commit_message = (
        f"fix: resolve {len(successful_findings)} {batch.rule_pattern} findings "
        f"(partial — {len(batch.findings) - len(successful_findings)} deferred)"
    )
    commit_result = git_commit_all(
        batch.worktree_path,
        commit_message,
        log_file=log_file,
        dry_run=False,
    )
    if commit_result == "no_changes":
        _append_text(
            log_file, f"batch-partial: no changes to commit for {batch.batch_id}"
        )
        return False

    pushed = git_push_branch(
        batch.worktree_path,
        batch.branch,
        log_file=log_file,
        dry_run=False,
        safety_config=safety_config,
    )
    if not pushed:
        _append_text(log_file, f"batch-partial: push failed for {batch.batch_id}")
        return False

    _append_text(
        log_file,
        f"batch-partial: committed {len(successful_findings)} successful fixes for {batch.batch_id}",
    )
    return True


def split_batch(
    batch: BatchGroup,
    repo_path: Path,
    args: Any,
    log_file: Path,
) -> List[BatchGroup]:
    """Split failed findings into sub-batches for retry.

    Strategy:
    - 1-2 failures → convert to solo batches
    - 3+ failures → halve into smaller sub-batches
    - Respects max_split_depth (from args, default 3)
    - Each sub-batch inherits batch_id prefix with split suffix
    """
    from bluei.engine.batch_grouping import (
        _find_issue_for_finding,
        check_batch_conflicts,
    )
    from bluei.engine.state import _append_text

    failed_findings = []
    for f in batch.findings:
        result = batch.fix_results.get(f.finding_id)
        if isinstance(result, FixResult):
            is_failed = result.status == FixStatus.FAILED.value
        elif isinstance(result, dict):
            is_failed = result.get("status") == FixStatus.FAILED.value
        else:
            is_failed = True  # No result → treat as failed
        if is_failed:
            failed_findings.append(f)

    if not failed_findings:
        return []

    sub_batches: List[BatchGroup] = []
    max_depth = getattr(args, "max_split_depth", 3)

    if batch.retry_count >= max_depth:
        _append_text(
            log_file,
            f"batch-split: {batch.batch_id} at max depth {batch.retry_count}, not splitting further",
        )
        return []

    split_suffix = f"-s{len(batch.split_history) + 1}"

    if len(failed_findings) <= 2:
        # Convert each to a solo batch
        for f in failed_findings:
            issue = _find_issue_for_finding(batch.issues, f.finding_id)
            solo = BatchGroup.from_solo(issue, f)
            solo.batch_id = f"{batch.batch_id}{split_suffix}-{f.finding_id[:8]}"
            solo.retry_count = batch.retry_count + 1
            sub_batches.append(solo)
            _append_text(
                log_file,
                f"batch-split: finding {f.finding_id[:8]} → solo batch {solo.batch_id}",
            )
    else:
        # Try conflict-aware splitting when findings have inter-file conflicts
        conflicts = check_batch_conflicts(failed_findings)
        if conflicts:
            conflict_groups = split_on_conflicts(batch)
            has_clean_group = any(not g.is_solo for g in conflict_groups)
            if has_clean_group:
                _append_text(
                    log_file,
                    f"batch-split: {batch.batch_id} {len(conflicts)} conflict pair(s), "
                    f"using conflict-aware splitting",
                )
                for i, sb in enumerate(conflict_groups):
                    sb.batch_id = f"{batch.batch_id}{split_suffix}-c{i + 1}"
                    sb.retry_count = batch.retry_count + 1
                return conflict_groups

        # No useful conflicts or all findings conflict — halve into smaller sub-batches
        half = max(len(failed_findings) // 2, 1)
        for chunk_idx, i in enumerate(range(0, len(failed_findings), half)):
            chunk = failed_findings[i : i + half]
            issues_map = {
                f.finding_id: _find_issue_for_finding(batch.issues, f.finding_id)
                for f in chunk
            }
            rule_config = BatchRule(
                rule_pattern=batch.rule_pattern,
                max_batch_size=half,
            )
            sub_batch = BatchGroup.from_findings(chunk, issues_map, rule_config)
            sub_batch.batch_id = f"{batch.batch_id}{split_suffix}-h{chunk_idx + 1}"
            sub_batch.retry_count = batch.retry_count + 1
            sub_batches.append(sub_batch)
            _append_text(
                log_file,
                f"batch-split: {len(chunk)} findings → sub-batch {sub_batch.batch_id}",
            )

    return sub_batches


def split_on_conflicts(batch: BatchGroup) -> List[BatchGroup]:
    """Enhanced conflict handling: split batch at conflict boundaries.

    Builds a conflict graph from check_batch_conflicts() results.
    Separates non-conflicting findings into a clean batch.
    Isolates conflicting findings as solo batches.
    Returns list of conflict-free sub-batches.
    """
    from bluei.engine.batch_grouping import (
        _find_issue_for_finding,
        check_batch_conflicts,
    )

    conflicts = check_batch_conflicts(batch.findings)
    if not conflicts:
        return [batch]

    # Collect IDs of all findings involved in conflicts
    conflict_ids: set = set()
    for a, b in conflicts:
        conflict_ids.add(a.finding_id)
        conflict_ids.add(b.finding_id)

    non_conflicting = [f for f in batch.findings if f.finding_id not in conflict_ids]
    conflicting = [f for f in batch.findings if f.finding_id in conflict_ids]

    groups: List[BatchGroup] = []

    if non_conflicting:
        issues_map = {
            f.finding_id: _find_issue_for_finding(batch.issues, f.finding_id)
            for f in non_conflicting
        }
        rule_config = BatchRule(rule_pattern=batch.rule_pattern)
        clean_batch = BatchGroup.from_findings(non_conflicting, issues_map, rule_config)
        clean_batch.retry_count = batch.retry_count
        clean_batch.split_history = list(batch.split_history)
        groups.append(clean_batch)

    # Each conflicting finding gets its own solo batch
    for f in conflicting:
        issue = _find_issue_for_finding(batch.issues, f.finding_id)
        solo = BatchGroup.from_solo(issue, f)
        solo.retry_count = batch.retry_count
        solo.split_history = list(batch.split_history)
        groups.append(solo)

    return groups


def handle_batch_failure(
    batch: BatchGroup,
    repo_path: Path,
    args: Any,
    log_file: Path,
) -> List[BatchGroup]:
    """Main failure handler for a batch with excessive fix failures.

    1. Separates successful and failed findings from batch.fix_results
    2. If successful findings exist → commit and create PR for those
    3. If failed findings exist → split into sub-batches or solo
    4. Records split in batch.split_history
    5. Returns list of sub-BatchGroup objects for retry
    """
    from bluei.engine.state import _append_text

    successful_findings: List[Finding] = []
    failed_findings: List[Finding] = []

    for f in batch.findings:
        result = batch.fix_results.get(f.finding_id)
        if isinstance(result, FixResult):
            status = result.status
        elif isinstance(result, dict):
            status = result.get("status", FixStatus.FAILED.value)
        else:
            status = FixStatus.FAILED.value

        if status == FixStatus.SUCCESS.value:
            successful_findings.append(f)
        else:
            failed_findings.append(f)

    # Commit successful fixes if any
    if successful_findings:
        commit_partial_batch(successful_findings, batch, log_file)

    # Split failed findings into sub-batches
    sub_batches = split_batch(batch, repo_path, args, log_file)

    # Record split in history
    batch.split_history.append(
        {
            "split_at": now_iso(),
            "successful_count": len(successful_findings),
            "failed_count": len(failed_findings),
            "sub_batches_created": len(sub_batches),
            "reason": "too_many_fix_failures",
        }
    )
    batch.status = BatchStatus.SPLIT.value

    _append_text(
        log_file,
        f"batch-failure-handler: {batch.batch_id} split: "
        f"{len(successful_findings)} succeeded, {len(failed_findings)} failed, "
        f"{len(sub_batches)} sub-batches created",
    )

    return sub_batches


def recover_interrupted_batch(
    batch_id: str,
    batches_file: Path,
    worktree_root: Path,
) -> Optional[BatchGroup]:
    """Recover from an interrupted batch.

    Loads batch by ID from batches file and attempts recovery:
    - If status is FIXING or FIXING_PARTIAL:
      - If worktree exists and branch pushed → mark PR_CREATED
      - If worktree exists but not pushed → mark ABORTED, clean up worktree
      - If no worktree → mark ABORTED
    - Saves updated status to batches file.
    - Returns recovered BatchGroup or None.
    """
    from bluei.engine.state import load_batches, update_batch_record, _append_text
    from bluei.engine.utils import run_no_capture, run_capture

    batches = load_batches(batches_file)
    record = None
    for b in batches:
        if b.get("batch_id") == batch_id:
            record = b
            break

    if record is None:
        return None

    status = record.get("status", "")
    if status not in (BatchStatus.FIXING.value, BatchStatus.FIXING_PARTIAL.value):
        return None

    # Derive batch object from record
    batch = _batch_from_record(record)
    if batch is None:
        return None

    worktree_path_str = record.get("worktree_path")
    branch = record.get("branch")
    worktree_path = Path(worktree_path_str) if worktree_path_str else None

    new_status: Optional[str] = None

    if worktree_path and worktree_path.exists():
        # Worktree exists — check if branch was pushed to remote
        pushed = False
        if branch:
            rc, _ = run_capture(
                ["git", "branch", "-r", "--list", f"origin/{branch}"],
                cwd=worktree_path,
            )
            # Alternative: check if branch exists on remote
            rc2, remote_branches = run_capture(
                ["git", "ls-remote", "--heads", "origin", branch],
                cwd=worktree_path,
            )
            pushed = rc2 == 0 and remote_branches and remote_branches.strip() != ""

        if pushed:
            new_status = BatchStatus.PR_CREATED.value
        else:
            # Worktree exists but not pushed → abort and clean up
            new_status = BatchStatus.ABORTED.value
            # Clean up worktree
            try:
                remove_worktree(
                    worktree_path=worktree_path,
                    repo_path=worktree_root,
                )
            except (subprocess.CalledProcessError, OSError):
                logging.debug(
                    "worktree force-remove failed during batch recovery for %s",
                    worktree_path,
                )
    else:
        # No worktree → abort
        new_status = BatchStatus.ABORTED.value

    if new_status:
        batch.status = new_status
        update_batch_record(batches_file, batch_id, {"status": new_status})

    return batch


def _batch_from_record(record: Dict[str, Any]) -> Optional[BatchGroup]:
    """Reconstruct a BatchGroup from a persisted record dict.

    Note: Finding objects are reconstructed with minimal fields needed
    for split/recovery operations.
    """
    findings_data = record.get("findings", [])
    findings: List[Finding] = []
    issues: list = []

    for fd in findings_data:
        finding = Finding(
            finding_id=fd.get("finding_id", ""),
            repo="",
            path=fd.get("path", ""),
            line=fd.get("line", 0),
            rule=fd.get("rule", ""),
            snippet="",
            confidence=0.0,
            quick_win=False,
            safe_to_autofix=False,
        )
        findings.append(finding)
        issues.append(
            {
                "finding_id": fd.get("finding_id", ""),
                "id": fd.get("issue_id"),
            }
        )

    worktree_path_str = record.get("worktree_path")

    return BatchGroup(
        batch_id=record.get("batch_id", ""),
        rule_pattern=record.get("rule_pattern", ""),
        group_by=record.get("group_by", ""),
        findings=findings,
        issues=issues,
        status=record.get("status", "open"),
        worktree_path=Path(worktree_path_str) if worktree_path_str else None,
        branch=record.get("branch"),
        pr_number=record.get("pr_number"),
        pr_url=record.get("pr_url"),
        fix_results=record.get("fix_results", {}),
        retry_count=record.get("retry_count", 0),
        split_history=record.get("split_history", []),
    )
