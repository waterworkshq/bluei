"""Startup self-healing — stale lock cleanup, orphan worktree pruning."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.state import _append_text, load_batches, repair_state
from bluei.engine.utils import run_capture, run_no_capture

_logger = logging.getLogger(__name__)

STALE_LOCK_HOURS = 4
STALE_WORKTREE_DAYS = 7


def _parse_worktree_list_porcelain(repo_path: Path) -> List[Dict[str, str]]:
    """Parse `git worktree list --porcelain` and return a list of dicts.

    Each dict has keys:
        - worktree: str (absolute path)
        - head: str (commit sha or empty)
        - branch: str (ref name, or 'detached' if detached HEAD)
        - bare: bool (True if this is the bare repo)

    Returns an empty list on error.
    """
    try:
        rc, output = run_capture(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_path,
        )
        if rc != 0 or not output:
            return []
    except Exception:
        return []

    worktrees: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            if current and "worktree" in current:
                worktrees.append(current)
            current = {}
            continue
        parts = line.split(" ", 1)
        key = parts[0]
        value = parts[1] if len(parts) > 1 else ""
        if key == "worktree":
            current["worktree"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key == "detached":
            current["branch"] = "detached"
        elif key == "bare":
            current["bare"] = "true"
    if current and "worktree" in current:
        worktrees.append(current)
    return worktrees


def run_startup_self_healing(
    repo_path: Path,
    log_file: Optional[Path] = None,
    locks_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run self-healing on startup: clean stale locks, prune orphaned worktrees.

    Args:
        repo_path: Path to the git repository.
        log_file: Optional log file path.
        locks_dir: Optional path to lock files directory.
        dry_run: If True, only report what would be cleaned.

    Returns:
        Dict with keys:
            - stale_locks_removed: int
            - worktrees_pruned: bool
            - errors: List[str]
    """
    result: Dict[str, Any] = {
        "stale_locks_removed": 0,
        "worktrees_pruned": False,
        "errors": [],
    }

    # 1. Clean stale lock files
    if locks_dir is not None and locks_dir.exists():
        try:
            now = datetime.now(timezone.utc)
            for lock_file in locks_dir.iterdir():
                if not lock_file.is_file():
                    continue
                try:
                    mtime = datetime.fromtimestamp(
                        lock_file.stat().st_mtime, tz=timezone.utc
                    )
                    age_hours = (now - mtime).total_seconds() / 3600
                    if age_hours >= STALE_LOCK_HOURS:
                        if not dry_run:
                            lock_file.unlink()
                        result["stale_locks_removed"] += 1
                        if log_file:
                            _append_text(
                                log_file,
                                f"self-heal: removed stale lock {lock_file.name} "
                                f"age={age_hours:.1f}h",
                            )
                except (OSError, ValueError) as e:
                    result["errors"].append(f"lock-check:{lock_file.name}:{e}")
        except Exception as e:
            result["errors"].append(f"locks-dir:{e}")

    # 2. Prune orphaned git worktrees
    try:
        rc, output = run_capture(
            ["git", "worktree", "prune", "--verbose"],
            cwd=repo_path,
        )
        if rc == 0:
            result["worktrees_pruned"] = True
            if log_file:
                _append_text(log_file, "self-heal: git worktree prune completed")
        else:
            result["errors"].append(f"worktree-prune:rc={rc}")
    except Exception as e:
        result["errors"].append(f"worktree-prune:{e}")

    # 3. Orphaned worktree detection via git worktree list --porcelain
    orphaned_wt_count = 0
    try:
        listed_worktrees = _parse_worktree_list_porcelain(repo_path)
        listed_paths = {
            Path(w["worktree"]).resolve() for w in listed_worktrees if "worktree" in w
        }

        # Check directories under known worktree root for orphaned worktrees
        from bluei.engine.constants import DEFAULT_WORKTREE_ROOT

        if DEFAULT_WORKTREE_ROOT.exists():
            for wt_dir in DEFAULT_WORKTREE_ROOT.iterdir():
                if not wt_dir.is_dir():
                    continue
                resolved = wt_dir.resolve()
                if resolved in listed_paths:
                    continue
                if resolved == repo_path.resolve():
                    continue

                dotgit = resolved / ".git"
                if dotgit.exists():
                    if not dry_run:
                        run_no_capture(["rm", "-rf", str(resolved)], cwd=repo_path)
                    orphaned_wt_count += 1
                    if log_file:
                        _append_text(
                            log_file,
                            f"self-heal: removed orphaned worktree dir {wt_dir.name}",
                        )

        # Listed worktrees whose directories no longer exist on disk
        for wt in listed_worktrees:
            wt_path = Path(wt.get("worktree", "")).resolve()
            if wt_path == repo_path.resolve():
                continue
            if not wt_path.exists():
                branch_ref = wt.get("branch", "")
                if branch_ref and branch_ref != "detached":
                    branch_name = branch_ref.replace("refs/heads/", "", 1)
                    if not dry_run:
                        run_no_capture(
                            ["git", "branch", "-D", branch_name], cwd=repo_path
                        )
                if log_file:
                    _append_text(
                        log_file,
                        f"self-heal: cleaned missing worktree reference {wt_path}",
                    )
    except Exception as e:
        result["errors"].append(f"worktree-list-porcelain:{e}")

    if orphaned_wt_count > 0:
        if log_file:
            _append_text(
                log_file,
                f"self-heal: removed {orphaned_wt_count} orphaned worktree dir(s)",
            )

    # 4. Repair corrupted/missing state.json
    try:
        from bluei.engine.constants import DEFAULT_STATE

        state_path = (
            repo_path / "state.json" if not DEFAULT_STATE.exists() else DEFAULT_STATE
        )
        if state_path.exists():
            was_repaired = repair_state(state_path)
            if was_repaired:
                if log_file:
                    _append_text(
                        log_file,
                        f"self-heal: repaired corrupted state.json at {state_path}",
                    )
    except Exception as e:
        result["errors"].append(f"state-repair:{e}")
        if log_file:
            _append_text(log_file, f"self-heal: state repair failed ({e})")

    # 5. Clean stale batch state (batches.jsonl records with no corresponding worktree)
    stale_batch_count = 0
    try:
        from bluei.engine.constants import DEFAULT_BATCH_STATE

        if DEFAULT_BATCH_STATE.exists():
            batches = load_batches(DEFAULT_BATCH_STATE)
            active_batches: list[dict[str, Any]] = []
            for batch in batches:
                batch_branch = batch.get("branch", "")
                batch_wt_path = batch.get("worktree_path", "")
                if batch_branch:
                    # Check if the branch still exists
                    rc_check, _ = run_capture(
                        ["git", "rev-parse", "--verify", f"refs/heads/{batch_branch}"],
                        cwd=repo_path,
                    )
                    if rc_check != 0:
                        stale_batch_count += 1
                        if log_file:
                            _append_text(
                                log_file,
                                f"self-heal: stale batch record batch_id={batch.get('batch_id', '?')} branch={batch_branch}",
                            )
                        continue
                if batch_wt_path:
                    wt_p = Path(batch_wt_path)
                    if not wt_p.exists():
                        stale_batch_count += 1
                        if log_file:
                            _append_text(
                                log_file,
                                f"self-heal: stale batch record batch_id={batch.get('batch_id', '?')} worktree={batch_wt_path}",
                            )
                        continue
                active_batches.append(batch)

            if stale_batch_count > 0:
                if not dry_run:
                    with open(DEFAULT_BATCH_STATE, "w", encoding="utf-8") as f:
                        for batch in active_batches:
                            f.write(json.dumps(batch, sort_keys=True) + "\n")
                if log_file:
                    _append_text(
                        log_file,
                        f"self-heal: removed {stale_batch_count} stale batch record(s) from {DEFAULT_BATCH_STATE.name}",
                    )
    except Exception as e:
        result["errors"].append(f"batch-state-clean:{e}")

    return result
