"""Git operations for the fix lifecycle — commit, push, diff stats."""

import logging
from pathlib import Path
from typing import Tuple

from bluei.engine.state import _append_text
from bluei.engine.utils import run_capture

_logger = logging.getLogger(__name__)


def git_commit_all(repo_path: Path, message: str, log_file: Path, dry_run: bool) -> str:
    """Stage all changes and commit with the given message.

    Args:
        repo_path: Path to the git repository.
        message: Commit message.
        log_file: Path to the run log.
        dry_run: If True, log the would-be commit without executing it.

    Returns:
        'committed', 'no_changes', or 'error'.
    """
    rc, _ = run_capture(["git", "add", "-A"], cwd=repo_path)
    if rc != 0:
        _append_text(log_file, "error: git add failed")
        return "error"

    rc, _ = run_capture(["git", "diff", "--cached", "--quiet"], cwd=repo_path)
    if rc == 0:
        _append_text(log_file, "autofix: no staged changes to commit")
        return "no_changes"
    if rc not in (0, 1):
        _append_text(log_file, f"error: git diff --cached --quiet failed rc={rc}")
        return "error"

    if dry_run:
        _append_text(log_file, f'dry-run-live: would git commit message="{message}"')
        return "committed"

    rc, out = run_capture(["git", "commit", "-m", message], cwd=repo_path)
    if rc != 0:
        _append_text(log_file, f"error: git commit failed output={out[:300]}")
        return "error"
    return "committed"


def git_push_branch(
    repo_path: Path, branch: str, log_file: Path, dry_run: bool
) -> bool:
    """Push the given branch to origin.

    Args:
        repo_path: Path to the git repository.
        branch: Branch name to push.
        log_file: Path to the run log.
        dry_run: If True, log the would-be push without executing it.

    Returns:
        True if push succeeded (or dry-run), False on failure.
    """
    if dry_run:
        _append_text(log_file, f"dry-run-live: would git push -u origin {branch}")
        return True
    rc, out = run_capture(["git", "push", "-u", "origin", branch], cwd=repo_path)
    if rc != 0:
        _append_text(
            log_file, f"error: git push failed branch={branch} output={out[:300]}"
        )
        return False
    return True


def diff_stats(repo_path: Path) -> Tuple[int, int]:
    """Return (files_changed, total_loc_diff) from the working tree diff.

    Args:
        repo_path: Path to the git repository.

    Returns:
        Tuple of (files_changed, lines_added_plus_deleted). (0, 0) on error.
    """
    rc, out = run_capture(["git", "diff", "--numstat"], cwd=repo_path)
    if rc != 0:
        return 0, 0
    files_changed = 0
    loc_diff = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s = parts[0], parts[1]
        if add_s != "-" and del_s != "-":
            try:
                loc_diff += int(add_s) + int(del_s)
            except ValueError:
                _logger.debug("Failed to parse diff stat line")
        files_changed += 1
    return files_changed, loc_diff
