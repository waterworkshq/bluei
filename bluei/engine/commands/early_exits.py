"""Early-exit command handlers for the bluei engine CLI.

Each handler returns an exit code (int) or None if the command doesn't match.
These are the commands that exit immediately without entering the multi-phase pipeline.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from bluei.engine.state import _append_text

logger = logging.getLogger(__name__)


def run_docs_index_command(
    *,
    repo_path: Path,
    docs_index_file: Path,
    log_file: Path,
    args: Any,
) -> Optional[int]:
    """Handle --run-phase=docs-index. Returns exit code or None to skip."""
    if not (
        getattr(args, "refresh_docs_index", False) or args.run_phase == "docs-index"
    ):
        if args.run_phase != "docs-index":
            return None

    from bluei.engine.git_utils import refresh_docs_index

    docs_entries = refresh_docs_index(repo_path, docs_index_file, log_file)
    if args.run_phase == "docs-index":
        print(f"[DONE] DOCS-INDEX entries={len(docs_entries)} file={docs_index_file}")
        return 0
    return None


def run_refactor_cycle_command(
    *,
    repo_path: Path,
    worktree_root: Path,
    log_file: Path,
    args: Any,
) -> Optional[int]:
    """Handle --run-phase=refactor-cycle. Returns exit code or None to skip."""
    if args.run_phase != "refactor-cycle":
        return None

    from bluei.engine.lifecycle import process_refactor_queue

    _append_text(
        log_file,
        f"refactor-cycle: start worktree={worktree_root} dry_run={args.dry_run}",
    )
    result = process_refactor_queue(
        worktree_path=worktree_root,
        repo_path=repo_path,
        dry_run=args.dry_run,
        max_items=args.max_queue_items,
        auto_approve=args.auto_approve,
    )
    processed = result.get("processed", [])
    approved = result.get("approved", [])
    pending = result.get("pending", [])
    failed = result.get("failed", [])
    print(
        f"[DONE] refactor-cycle processed={len(processed)} "
        f"auto_approved={len(approved)} pending={len(pending)} failed={len(failed)}"
    )
    _append_text(
        log_file,
        f"refactor-cycle: done processed={processed} approved={approved} "
        f"pending={pending} failed={failed}",
    )
    return 0


def run_smoke_test_command(
    *,
    repo_path: Path,
    log_file: Path,
    args: Any,
) -> Optional[int]:
    """Handle --run-phase=smoke-test or --smoke-test. Returns exit code or None to skip."""
    if args.run_phase != "smoke-test" and not getattr(args, "smoke_test", False):
        return None

    from bluei.engine.validation import run_smoke_test

    _append_text(log_file, f"smoke-test: starting repo_path={repo_path}")
    result = run_smoke_test(repo_path=repo_path, log_file=log_file)
    checks = result["checks"]
    duration_ms = result["duration_ms"]
    errors = result["errors"]

    status = "PASS" if result["passed"] else "FAIL"
    print(f"[SMOKE-TEST] {status} ({duration_ms}ms)")
    for name, passed in checks.items():
        icon = "\u2713" if passed else "\u2717"
        print(f"  {icon} {name}")
    if errors:
        for err in errors:
            print(f"      {err}")
    print(f"  duration: {duration_ms}ms")

    _append_text(
        log_file,
        f"smoke-test: {status} duration_ms={duration_ms} "
        f"git={checks['git']} worktree={checks['worktree']} linter={checks['linter']}",
    )
    if errors:
        for err in errors:
            _append_text(log_file, f"smoke-test: error {err}")

    return 0 if result["passed"] else 1


def run_clean_prs_command(
    *,
    repo_path: Path,
    log_file: Path,
    args: Any,
) -> Optional[int]:
    """Handle --run-phase=clean-prs. Returns exit code or None to skip."""
    if args.run_phase != "clean-prs":
        return None

    from bluei.engine.clean_prs import clean_stale_prs
    from bluei.engine.gh import get_origin_url, parse_github_repo

    _slug_owner, _slug_name = parse_github_repo(get_origin_url(repo_path))
    _repo_slug = f"{_slug_owner}/{_slug_name}" if _slug_owner and _slug_name else ""
    _append_text(log_file, "clean-prs: starting")
    result = clean_stale_prs(
        repo_slug=_repo_slug,
        cwd=repo_path,
        log_file=log_file,
        dry_run=args.dry_run,
        stale_hours=getattr(args, "stale_pr_hours", 48),
        dedup_window=getattr(args, "stale_dedup_window", 24),
    )
    print(
        f"[DONE] clean-prs closed={result['closed']} "
        f"duplicates={result['duplicates']} stale={result['stale']}"
    )
    return 0


def validate_safety(
    *,
    args: Any,
    log_file: Path,
) -> Optional[int]:
    """Validate safety-related CLI args. Returns exit code on violation, None if OK."""
    if getattr(args, "force_push", False):
        print("[ABORT] force push is disabled in safety mode")
        _append_text(log_file, "abort: force push disabled")
        return 2

    if args.max_prs_per_run < 1 or args.max_prs_per_run > 2:
        print("[ABORT] max-prs-per-run is hard-locked to 1-2 for sandbox safety")
        _append_text(
            log_file,
            f"abort: max-prs-per-run must be 1 or 2, got {args.max_prs_per_run}",
        )
        return 2

    if args.open_issues_cap < 10 or args.open_issues_cap > 50:
        print("[ABORT] open-issues-cap must stay within 10-50 for sandbox safety")
        _append_text(
            log_file,
            f"abort: invalid open-issues-cap={args.open_issues_cap} (allowed 10-50)",
        )
        return 2

    if args.open_prs_cap < 1 or args.open_prs_cap > 10:
        print("[ABORT] open-prs-cap must stay within 1-10 for sandbox safety")
        _append_text(
            log_file, f"abort: invalid open-prs-cap={args.open_prs_cap} (allowed 1-10)"
        )
        return 2

    if args.merge_cooldown_minutes < 0:
        print("[ABORT] merge-cooldown-minutes must be >= 0")
        _append_text(log_file, "abort: invalid merge-cooldown-minutes")
        return 2

    if args.finding_cooldown_seconds < 0:
        print("[ABORT] finding-cooldown-seconds must be >= 0")
        _append_text(log_file, "abort: invalid finding-cooldown-seconds")
        return 2

    if args.staleness_threshold_seconds < 1:
        print("[ABORT] staleness-threshold-seconds must be positive")
        _append_text(log_file, "abort: invalid staleness-threshold-seconds")
        return 2

    if args.max_fix_attempts_per_issue < 1:
        print("[ABORT] max-fix-attempts-per-issue must be >= 1")
        _append_text(log_file, "abort: invalid max-fix-attempts-per-issue")
        return 2

    return None
