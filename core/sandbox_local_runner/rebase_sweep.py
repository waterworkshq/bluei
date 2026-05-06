"""rebase_sweep.py — Post-merge rebase sweep across sibling PRs.

After a merge-cycle successfully lands a PR, this module rebases every other
open PR targeting the same base branch onto the updated base. Clean rebases
are force-pushed (via force-with-lease) and the PR stays in the merge queue.
Conflicted rebases are flagged with a machine-readable marker in the PR body
and pulled from the merge rotation.

Design:
  - One merge at a time.  No parallel merges, no lock table, no file tracking.
  - Siblings are identified by matching base branch (baseRefName).
  - Rebase order: oldest first (higher success rate).
  - Conflicts are surfaced with file paths in a predictable marker format
    that the existing PR evaluator can grep for during evaluation.
  - Staggered force-pushes give the next merge-cycle a clean verify window
    per sibling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import run_capture
from .models import now_iso


# Machine-readable conflict marker format.  merge-cycle's evaluator should
# skip PRs whose body contains this marker.
CONFLICT_MARKER_HEADER = "🤖 Auto-rebase conflict detected"


def _fetch_sibling_prs(
    gh_repo_slug: str,
    base_branch: str,
    exclude_pr_number: int,
    cwd: Path,
) -> List[Dict[str, Any]]:
    """Fetch open PRs targeting the same base branch, excluding the merged PR."""
    rc, out = run_capture(
        [
            "gh", "pr", "list",
            "--repo", gh_repo_slug,
            "--base", base_branch,
            "--state", "open",
            "--json", "number,headRefName,headRepository,createdAt,body",
            "--limit", "50",
        ],
        cwd=cwd,
    )
    if rc != 0 or not out.strip():
        return []

    try:
        all_prs = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []

    # Filter out the PR that was just merged
    siblings = [pr for pr in all_prs if int(pr.get("number", 0)) != exclude_pr_number]

    # Sort by age ascending (oldest first)
    def _age_key(pr: Dict[str, Any]) -> str:
        return pr.get("createdAt", "")

    siblings.sort(key=_age_key)
    return siblings


def _compute_fork_point(
    repo_path: Path,
    base_branch: str,
    head_branch: str,
) -> Tuple[str, str]:
    """Return (fork_point_sha, old_base_sha) for a feature branch.

    - fork_point_sha: the merge-base between base_branch and head_branch
      (the commit they diverged from).
    - old_base_sha: the current HEAD of base_branch / origin/base_branch
      (this is what exists *before* the merge that triggered the sweep).

    The rebase command becomes:
        git rebase --onto origin/{base_branch} {fork_point_sha} {head_branch}
    """
    # Fetch current origin base
    rc, _ = run_capture(
        ["git", "fetch", "origin", base_branch],
        cwd=repo_path,
    )
    if rc != 0:
        return ("", "")

    rc1, fork_pt = run_capture(
        ["git", "merge-base", f"origin/{base_branch}", f"origin/{head_branch}"],
        cwd=repo_path,
    )
    fork_pt = fork_pt.strip() if rc1 == 0 else ""

    rc2, old_base = run_capture(
        ["git", "rev-parse", f"origin/{base_branch}"],
        cwd=repo_path,
    )
    old_base = old_base.strip() if rc2 == 0 else ""

    return (fork_pt, old_base)


def _rebase_sibling(
    repo_path: Path,
    head_branch: str,
    base_branch: str,
    local_branch: str,
) -> Tuple[bool, str, List[str]]:
    """Attempt to rebase one sibling PR onto the updated base branch.

    Uses a named local tracking branch to avoid detached HEAD, which simplifies
    force-pushing the result back to the remote.

    Args:
        repo_path: Path to the local repo.
        head_branch: Remote PR branch name (e.g. "fix-bug-123").
        base_branch: Target base branch (e.g. "main").
        local_branch: Temporary local tracking branch name.

    Flow:
      1. Fetch head branch from origin
      2. Create local branch {local_branch} tracking origin/{head_branch}
      3. git rebase --onto origin/{base_branch} {fork_point}
      4. If clean → capture SHA, restore base branch
      5. If conflict → capture file list, abort rebase, clean up local branch

    Returns:
        (success, new_head_sha or error_message, conflict_files)
    """
    # Fetch the head branch from origin
    rc_fetch, _ = run_capture(
        ["git", "fetch", "origin", head_branch],
        cwd=repo_path,
        timeout=30,
    )
    if rc_fetch != 0:
        return (False, "fetch-failed", [])

    # Compute fork point
    fork_pt, old_base = _compute_fork_point(repo_path, base_branch, head_branch)
    if not fork_pt or not old_base:
        # Fetch base branch if merge-base failed (may not have origin/main yet)
        run_capture(["git", "fetch", "origin", base_branch], cwd=repo_path, timeout=30)
        fork_pt, old_base = _compute_fork_point(repo_path, base_branch, head_branch)
        if not fork_pt or not old_base:
            return (False, "merge-base-failed", [])

    # Clean up any stale local branch from a previous sweep
    run_capture(["git", "branch", "-D", local_branch], cwd=repo_path, timeout=10)

    # Create a local tracking branch from the remote head ref
    rc_create, _ = run_capture(
        ["git", "checkout", "-b", local_branch, f"origin/{head_branch}"],
        cwd=repo_path,
        timeout=30,
    )
    if rc_create != 0:
        return (False, "local-branch-create-failed", [])

    # Attempt the rebase: replay local branch commits on top of current base
    rc, out = run_capture(
        [
            "git", "rebase",
            "--onto", f"origin/{base_branch}",
            fork_pt,
        ],
        cwd=repo_path,
        timeout=120,
    )

    if rc == 0:
        # Rebase was clean — get the new HEAD
        rc_head, new_head = run_capture(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
        )
        new_sha = new_head.strip() if rc_head == 0 else ""
        # Switch back to detached base-branch state (the run engine manages its own worktree)
        run_capture(["git", "checkout", "-f", f"origin/{base_branch}"], cwd=repo_path)
        return (True, new_sha, [])

    # Rebase failed — capture conflicting files
    rc_diff, diff_out = run_capture(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_path,
    )
    conflict_files = [f.strip() for f in diff_out.splitlines() if f.strip()] if rc_diff == 0 else []

    # Abort the rebase to restore clean state
    run_capture(["git", "rebase", "--abort"], cwd=repo_path)
    run_capture(["git", "checkout", "-f", f"origin/{base_branch}"], cwd=repo_path)

    # Clean up the local branch
    run_capture(["git", "branch", "-D", local_branch], cwd=repo_path, timeout=10)

    # Log error detail
    error_msg = out.strip()[:500] if out.strip() else "rebase-conflict"
    return (False, error_msg, conflict_files)


def _has_conflict_marker(body: Optional[str]) -> bool:
    """Check if a PR body already contains the auto-rebase conflict marker."""
    if not body:
        return False
    return CONFLICT_MARKER_HEADER in body


def _update_pr_body_with_conflict(
    gh_repo_slug: str,
    pr_number: int,
    conflict_files: List[str],
    cwd: Path,
    dry_run: bool,
) -> None:
    """Append a machine-readable conflict marker to the PR body.

    The marker format is predictable so merge-cycle's evaluate_pr_mergeability()
    or a similar gate can grep for it and skip the PR.
    """
    if dry_run:
        return

    conflict_list = ", ".join(conflict_files)
    marker_block = (
        f"\n\n---\n"
        f"{CONFLICT_MARKER_HEADER}\n"
        f"Files: {conflict_list}\n"
        f"Skipped until resolved.\n"
    )

    # Get current PR body
    rc, body = run_capture(
        ["gh", "pr", "view", str(pr_number), "--repo", gh_repo_slug, "--json", "body"],
        cwd=cwd,
    )
    if rc != 0:
        return

    try:
        pr_data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return

    current_body = pr_data.get("body", "") or ""

    # Skip if already marked
    if _has_conflict_marker(current_body):
        return

    new_body = current_body + marker_block
    run_capture(
        ["gh", "pr", "update", str(pr_number), "--repo", gh_repo_slug, "--body", new_body],
        cwd=cwd,
    )


def _force_push_rebased(
    local_branch: str,
    remote_branch: str,
    new_head_sha: str,
    repo_path: Path,
    log_file: Path,
    dry_run: bool,
    pre_existing_dirty: bool = False,
) -> bool:
    """Force-push a successfully rebased local branch to the remote PR branch.

    Uses --force-with-lease to avoid overwriting unknown remote changes.
    Pushes local_branch -> remote_branch via explicit refspec.
    Cleans up local tracking branch after successful push.
    Returns True on success.
    """
    if dry_run:
        return True

    rc, out = run_capture(
        ["git", "push", "--force-with-lease", "origin", f"{local_branch}:{remote_branch}"],
        cwd=repo_path,
        timeout=60,
    )
    if rc != 0:
        _append_text(
            log_file,
            f"rebase-push-fail: {local_branch}->{remote_branch} error={(out or 'unknown')[:200]}",
        )
        return False

    # Clean up local tracking branch
    run_capture(["git", "branch", "-D", local_branch], cwd=repo_path, timeout=10)

    note = "pre-existing-dirty" if pre_existing_dirty else "clean"
    _append_text(
        log_file,
        f"rebase-pushed: {remote_branch} sha={new_head_sha[:12]} note={note}",
    )
    return True


def _append_text(log_file: Path, text: str) -> None:
    """Append a line to the log file."""
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {text}\n")
    except OSError:
        pass


def sweep_rebase(
    repo_path: Path,
    gh_repo_slug: str,
    merged_pr_number: int,
    base_branch: str,
    log_file: Path,
    dry_run: bool = False,
    max_prs: int = 5,
    rebase_stats_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Perform a rebase sweep after a merge.

    Args:
        repo_path: Path to the local repo (sandbox worktree).
        gh_repo_slug: GitHub slug (e.g. "owner/repo").
        merged_pr_number: The PR that was just merged.
        base_branch: The target branch (e.g. "main").
        log_file: Path to the run log.
        dry_run: If True, log intentions without executing.
        max_prs: Maximum siblings to rebase in one sweep.

    Returns:
        Dict with keys:
            rebased: List[Dict] — successfully rebased PRs
            conflicted: List[Dict] — PRs with conflicts
            skipped: List[Dict] — PRs already conflict-marked or errored
    """
    _append_text(log_file, f"rebase-sweep-start: merged_pr=#{merged_pr_number} base={base_branch}")

    result: Dict[str, Any] = {
        "rebased": [],
        "conflicted": [],
        "skipped": [],
    }

    siblings = _fetch_sibling_prs(gh_repo_slug, base_branch, merged_pr_number, cwd=repo_path)
    if not siblings:
        _append_text(log_file, "rebase-sweep: no siblings found")
        return result

    # Sort oldest first and cap
    siblings = siblings[:max_prs]
    _append_text(log_file, f"rebase-sweep: found {len(siblings)} sibling(s)")

    for pr in siblings:
        pr_number = int(pr.get("number", 0))
        head_branch = pr.get("headRefName", "")
        pr_body = pr.get("body") or ""

        if not head_branch:
            result["skipped"].append({"pr_number": pr_number, "reason": "no-head-branch"})
            continue

        # Skip PRs already marked with a conflict marker
        if _has_conflict_marker(pr_body):
            _append_text(log_file, f"rebase-skip: pr=#{pr_number} already conflict-marked")
            result["skipped"].append({"pr_number": pr_number, "reason": "already-conflict-marked"})
            continue

        # Detect pre-existing dirty state (stuck temporarily_unreachable PRs)
        merge_state = str(pr.get("mergeStateStatus") or "").upper()
        pre_existing_dirty = merge_state in ("DIRTY", "UNKNOWN", "BEHIND")

        local_branch = f"rebase-sweep-{pr_number}"

        _append_text(log_file, f"rebase-attempt: pr=#{pr_number} branch={head_branch}")

        success, detail, conflict_files = _rebase_sibling(
            repo_path=repo_path,
            head_branch=head_branch,
            base_branch=base_branch,
            local_branch=local_branch,
        )

        if success:
            # Guard: skip if new_head_sha is empty (rev-parse failed after clean rebase)
            if not detail:
                _append_text(log_file, f"rebase-skip: pr=#{pr_number} empty-HEAD-after-rebase")
                result["skipped"].append({"pr_number": pr_number, "reason": "empty-HEAD-after-rebase"})
                continue

            pushed = _force_push_rebased(
                local_branch=local_branch,
                remote_branch=head_branch,
                new_head_sha=detail,
                repo_path=repo_path,
                log_file=log_file,
                dry_run=dry_run,
                pre_existing_dirty=pre_existing_dirty,
            )
            if pushed:
                result["rebased"].append({
                    "pr_number": pr_number,
                    "branch": head_branch,
                    "new_head": detail[:12],
                    "pre_existing_dirty": pre_existing_dirty,
                })
            else:
                result["skipped"].append({
                    "pr_number": pr_number,
                    "reason": "force-push-failed",
                })
        else:
            if conflict_files:
                _update_pr_body_with_conflict(
                    gh_repo_slug=gh_repo_slug,
                    pr_number=pr_number,
                    conflict_files=conflict_files,
                    cwd=repo_path,
                    dry_run=dry_run,
                )
                result["conflicted"].append({
                    "pr_number": pr_number,
                    "branch": head_branch,
                    "files": conflict_files,
                    "error": detail[:200],
                })
                _append_text(
                    log_file,
                    f"rebase-conflict: pr=#{pr_number} files={conflict_files}",
                )
            else:
                result["skipped"].append({
                    "pr_number": pr_number,
                    "reason": detail[:200],
                })
                _append_text(
                    log_file,
                    f"rebase-fail: pr=#{pr_number} error={detail[:200]}",
                )

        # Stagger: small gap between force-pushes for verify windows
        time.sleep(2)

    _append_text(
        log_file,
        f"rebase-sweep-end: "
        f"rebased={len(result['rebased'])} "
        f"conflicted={len(result['conflicted'])} "
        f"skipped={len(result['skipped'])}",
    )

    # Persist sweep telemetry
    if rebase_stats_file is not None:
        try:
            record = {
                "timestamp": now_iso(),
                "repo": gh_repo_slug,
                "merged_pr": merged_pr_number,
                "base": base_branch,
                "dry_run": dry_run,
                "rebased": [{"pr": r["pr_number"], "branch": r.get("branch","")} for r in result["rebased"]],
                "conflicted": [{"pr": c["pr_number"], "files": c.get("files",[])} for c in result["conflicted"]],
                "skipped": [{"pr": s["pr_number"], "reason": s.get("reason","")} for s in result["skipped"]],
            }
            with open(rebase_stats_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass

    return result
