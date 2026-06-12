"""Worktree lifecycle management — create, remove, prune, hydrate.

Reusable across pr_cycle, batch_pr, review, and startup. All functions are
best-effort and log errors rather than raising exceptions.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bluei.engine.utils import run_capture, run_no_capture

_logger = logging.getLogger(__name__)


@dataclass
class WorktreeResult:
    """Result of a worktree operation."""

    success: bool
    worktree_path: Path
    branch: str
    error: Optional[str] = None


def create_worktree(
    repo_path: Path,
    branch: str,
    worktree_path: Path,
    *,
    start_point: Optional[str] = None,
    log_file: Optional[Path] = None,
) -> WorktreeResult:
    """Create a git worktree at worktree_path with the given branch.

    Steps:
    1. Prune stale worktree metadata
    2. Remove existing worktree_path if present
    3. git worktree add -B <branch> <path> [<start_point>]

    Args:
        repo_path: The main repository path (cwd for git commands)
        branch: Branch name for the worktree
        worktree_path: Path where the worktree will be created
        start_point: Optional git start point (commit, branch, or ref)
        log_file: Optional log file for operation messages

    Returns:
        WorktreeResult with success status and paths
    """
    from bluei.engine.state import _append_text

    def _log(msg: str) -> None:
        if log_file is not None:
            _append_text(log_file, msg)
        else:
            _logger.debug(msg)

    prune_worktrees(repo_path, log_file=log_file)

    if worktree_path.exists():
        run_no_capture(["rm", "-rf", str(worktree_path)], cwd=repo_path)

    cmd = ["git", "worktree", "add", "-B", branch, str(worktree_path)]
    if start_point:
        cmd.append(start_point)

    rc, out = run_capture(cmd, cwd=repo_path)
    if rc != 0:
        error_msg = f"failed to create worktree output={(out or '<empty>')[:300]}"
        _log(f"worktree: {error_msg}")
        return WorktreeResult(
            success=False,
            worktree_path=worktree_path,
            branch=branch,
            error=error_msg,
        )

    _log(f"worktree: created worktree={worktree_path} branch={branch}")
    return WorktreeResult(
        success=True,
        worktree_path=worktree_path,
        branch=branch,
    )


def remove_worktree(
    worktree_path: Path,
    repo_path: Path,
    *,
    branch: Optional[str] = None,
    delete_branch: bool = False,
    log_file: Optional[Path] = None,
) -> None:
    """Remove a git worktree and optionally delete its branch.

    Steps:
    1. git worktree remove --force <path>
    2. git worktree prune
    3. Optionally: git branch -D <branch>

    Args:
        worktree_path: Path to the worktree to remove
        repo_path: The main repository path (cwd for git commands)
        branch: Optional branch name to delete
        delete_branch: If True, delete the branch after removing worktree
        log_file: Optional log file for operation messages
    """
    from bluei.engine.state import _append_text

    def _log(msg: str) -> None:
        if log_file is not None:
            _append_text(log_file, msg)
        else:
            _logger.debug(msg)

    if worktree_path.exists():
        run_no_capture(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
        )

    prune_worktrees(repo_path, log_file=log_file)

    if delete_branch and branch:
        run_no_capture(["git", "branch", "-D", branch], cwd=repo_path)

    _log(f"worktree: removed worktree={worktree_path}")


def prune_worktrees(
    repo_path: Path,
    *,
    log_file: Optional[Path] = None,
) -> None:
    """Prune stale worktree metadata.

    Best-effort — errors are logged but not raised.

    Args:
        repo_path: The main repository path (cwd for git commands)
        log_file: Optional log file for operation messages
    """
    run_no_capture(["git", "worktree", "prune"], cwd=repo_path)


def hydrate_worktree(
    repo_path: Path,
    worktree_path: Path,
    *,
    log_file: Optional[Path] = None,
) -> None:
    """Best-effort link shared dependency folders into a fresh git worktree.

    Currently symlinks node_modules from the main repo into the worktree
    to avoid redundant npm installs.

    Args:
        repo_path: The main repository path (source of dependencies)
        worktree_path: The worktree path (target for symlinks)
        log_file: Optional log file for operation messages
    """
    from bluei.engine.state import _append_text

    def _log(msg: str) -> None:
        if log_file is not None:
            _append_text(log_file, msg)
        else:
            _logger.debug(msg)

    for dirname in ("node_modules",):
        source = repo_path / dirname
        target = worktree_path / dirname
        if not source.exists() or target.exists():
            continue
        try:
            os.symlink(source, target, target_is_directory=True)
            _log(f"worktree-deps: linked {dirname} from repo into worktree")
        except Exception as exc:
            _log(f"worktree-deps: failed to link {dirname}: {exc}")


def get_worktree_branch(worktree_path: Path) -> str:
    """Get the current branch name in a worktree.

    Args:
        worktree_path: Path to the worktree

    Returns:
        Branch name, or empty string if not in a worktree or on error
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(worktree_path),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""
