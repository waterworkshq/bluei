"""Helper functions extracted from cli.py for the command decomposition.

These functions are pure utilities or standalone helpers with no dependency
on the main() pipeline state.  They are re-exported from ``bluei.engine.cli``
for backward compatibility.
"""

import argparse
import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bluei.engine.constants import (
    DEFAULT_BATCH_RULES_PATH,
    DETECTOR_CATALOG,
    load_llm_fixable_rules,
)
from bluei.engine.gh import find_existing_github_pr
from bluei.engine.models import age_seconds, now_iso
from bluei.engine.orchestrator import (
    append_issue_history,
    build_active_cycle_command,
    build_docs_index_refresh_command,
    build_issue_cycle_command,
    build_merge_cycle_command,
    build_orchestrated_cycle_command,
    build_pr_cycle_command,
    build_reconcile_only_command,
    build_refactor_cycle_command,
    build_verification_only_command,
    set_issue_status,
)
from bluei.engine.state import (
    NON_ACTIONABLE_ISSUE_STATUSES,
    _append_text,
    load_issues,
)
from bluei.engine.utils import run_capture, sanitize_command_template

logger = logging.getLogger(__name__)

_LLM_FIXABLE_RULES: Optional[Dict[str, Dict[str, Any]]] = None


def _compute_health_score(
    *,
    raw_open_issues: int,
    live_open_prs: int,
    issues_list: List[Dict[str, Any]],
) -> int:
    """Compute the legacy status health score used by older wiring tests."""
    terminal_issues = sum(
        1
        for issue in issues_list
        if issue.get("status", "open") in NON_ACTIONABLE_ISSUE_STATUSES
    )
    score = 100
    score -= max(0, int(raw_open_issues)) * 5
    score -= max(0, int(live_open_prs)) * 10
    score -= terminal_issues * 3
    return max(0, min(100, score))


def _build_refactor_queue_snapshot() -> Dict[str, Any]:
    """Return lightweight refactor queue counts for status/reporting."""
    try:
        import bluei.engine.refactor_queue as rq_mod

        queue = rq_mod.RefactorQueue()
        counts = queue.count_by_status()
    except Exception:
        counts = {}

    snapshot = {
        "pending_review": int(counts.get("pending_review", 0)),
        "approved": int(counts.get("approved", 0)),
        "executing": int(counts.get("executing", 0)),
        "completed": int(counts.get("completed", 0)),
        "aborted": int(counts.get("aborted", 0)),
    }
    snapshot["total"] = sum(snapshot.values())
    return snapshot


def _triage_pr_back_to_fix_cycle(
    *,
    issue: Dict[str, Any],
    pr_number: int,
    pr_url: str,
    branch: str,
    reason: str,
    log_file: Path,
) -> None:
    """Reset an issue to ``pr_merge_conflict`` so it re-enters the fix cycle.

    Args:
        issue: Issue record dict (mutated in-place).
        pr_number: GitHub PR number.
        pr_url: GitHub PR URL.
        branch: Head branch name.
        reason: Triage reason string stored in issue history.
        log_file: Run log file path.
    """
    issue_github = issue.setdefault("github", {})
    issue_github["pr_number"] = pr_number
    if pr_url:
        issue_github["pr_url"] = pr_url
    if branch:
        issue_github["branch"] = branch
    set_issue_status(issue, "pr_merge_conflict", reason)
    _append_text(
        log_file,
        f"triage: pr=#{pr_number} returned to pr-cycle reason={reason}",
    )


def _load_review_state(review_state_file: Path) -> Dict[str, Any]:
    """Load the autonomous review state JSON, returning {} on missing/corrupt files.

    Args:
        review_state_file: Path to ``review_state.json``.

    Returns:
        Parsed dict, or empty dict on any error.
    """
    if not review_state_file.exists():
        return {}
    try:
        payload = json.loads(review_state_file.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _autonomous_review_gate_passes(
    review_state: Dict[str, Any], pr_number: int
) -> Tuple[bool, str]:
    """Check whether a PR has passed the autonomous review gate for merge.

    Args:
        review_state: Full review state dict (``review_state.json`` contents).
        pr_number: GitHub PR number to look up.

    Returns:
        (passed, reason) tuple — ``passed`` is True when all gates pass.
    """
    prs = review_state.get("prs") if isinstance(review_state.get("prs"), dict) else {}
    pr_state = prs.get(str(pr_number)) if isinstance(prs, dict) else None
    if not isinstance(pr_state, dict):
        return False, "review-state-missing"

    if str(pr_state.get("last_action") or "") != "merge_ready":
        return False, "review-state-not-merge-ready"

    snapshot = (
        pr_state.get("last_snapshot")
        if isinstance(pr_state.get("last_snapshot"), dict)
        else {}
    )
    if str(snapshot.get("merge_state_status") or "").upper() not in {
        "CLEAN",
        "UNKNOWN",
        "UNSTABLE",
    }:
        return False, "review-state-not-cautiously-mergeable"
    if int(snapshot.get("actionable_comment_count") or 0) != 0:
        return False, "review-state-has-actionable-comments"
    if list(snapshot.get("active_change_requesters") or []):
        return False, "review-state-has-change-requesters"
    if not pr_state.get("last_review_comment_key"):
        return False, "review-artifact-missing"

    return True, "review-artifact-merge-ready"


def _get_llm_fixable_rules() -> Dict[str, Dict[str, Any]]:
    global _LLM_FIXABLE_RULES
    if _LLM_FIXABLE_RULES is None:
        _LLM_FIXABLE_RULES = load_llm_fixable_rules()
    return _LLM_FIXABLE_RULES


def _load_batch_rules_for_args(args: argparse.Namespace) -> List[Any]:
    """Load batch rules from args or fall back to built-in defaults."""
    from bluei.engine.batch_pr import load_batch_rules as _load_yaml_rules
    from bluei.engine.models import BatchRule

    if not getattr(args, "batch_pr_enabled", True):
        return []

    rules_path = getattr(args, "batch_pr_rules", None)
    if rules_path is not None and rules_path.exists():
        return _load_yaml_rules(rules_path)

    if DEFAULT_BATCH_RULES_PATH.exists():
        return _load_yaml_rules(DEFAULT_BATCH_RULES_PATH)

    return [
        BatchRule(
            rule_pattern="ruff-c408",
            enabled=True,
            group_by="rule",
            max_batch_size=20,
            max_files_per_batch=15,
            max_loc_per_batch=500,
            isolation={"file_patterns": ["**/migrations/*.py"]},
            priority=1,
        ),
        BatchRule(
            rule_pattern="ruff-b904",
            enabled=True,
            group_by="rule",
            max_batch_size=15,
            max_files_per_batch=10,
            max_loc_per_batch=300,
            isolation={"file_patterns": ["**/middleware*.py"]},
            priority=2,
        ),
        BatchRule(
            rule_pattern="ruff-b007",
            enabled=True,
            group_by="rule",
            max_batch_size=10,
            priority=3,
        ),
        BatchRule(
            rule_pattern="ruff-s311",
            enabled=True,
            group_by="file",
            max_batch_size=10,
            max_files_per_batch=5,
            priority=4,
        ),
    ]


def _hydrate_worktree_dependencies(
    repo_path: Path, worktree_path: Path, log_file: Path
) -> None:
    """Best-effort link shared dependency folders into a fresh git worktree."""
    for dirname in ("node_modules",):
        source = repo_path / dirname
        target = worktree_path / dirname
        if not source.exists() or target.exists():
            continue
        try:
            os.symlink(source, target, target_is_directory=True)
            _append_text(
                log_file, f"worktree-deps: linked {dirname} from repo into worktree"
            )
        except Exception as exc:
            _append_text(log_file, f"worktree-deps: failed to link {dirname}: {exc}")


def _reconcile_issue_pr_link(
    *,
    issue: Dict[str, Any],
    repo_slug: str,
    repo_path: Path,
    log_file: Path,
) -> bool:
    """Return True when the issue is still backed by an open live PR and should skip queueing."""
    issue_github = (
        issue.get("github", {}) if isinstance(issue.get("github"), dict) else {}
    )
    if not (issue_github.get("pr_number") or issue_github.get("pr_url")):
        return False

    finding_id = str(issue.get("finding_id") or "")
    if not finding_id:
        return True

    existing_pr = find_existing_github_pr(repo_slug, finding_id, cwd=repo_path)
    if existing_pr and str(existing_pr.get("state") or "").upper() == "OPEN":
        issue_github["pr_number"] = int(existing_pr["number"])
        issue_github["pr_url"] = str(
            existing_pr.get("url") or issue_github.get("pr_url") or ""
        )
        issue_github["branch"] = str(
            existing_pr.get("headRefName") or issue_github.get("branch") or ""
        )
        issue["github"] = issue_github
        return issue.get("status") != "pr_merge_conflict"

    stale_pr_number = issue_github.pop("pr_number", None)
    stale_pr_url = issue_github.pop("pr_url", None)
    issue_github.pop("branch", None)
    issue["github"] = issue_github

    stale_ref = stale_pr_url or stale_pr_number or "unknown"
    if issue.get("status") in {"pr_opened", "pr_merge_conflict"}:
        set_issue_status(
            issue, "open", f"linked PR no longer open; returned to queue ({stale_ref})"
        )
    else:
        append_issue_history(
            issue, "pr_link_cleared", f"linked PR no longer open ({stale_ref})"
        )
    _append_text(
        log_file,
        f"pr-link-reset: issue={issue.get('issue_id') or issue.get('id')} stale_pr={stale_ref}",
    )
    return False


def update_status_artifact(
    status_file: Path,
    state: Dict[str, Any],
    issues_file: Path,
    findings_file: Path,
    args: argparse.Namespace,
    run_mode: str,
    reconcile_event: Dict[str, Any],
    previous_last_run_at: Optional[str] = None,
    run_metrics: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the ``status.json`` artifact consumed by external dashboards / CI.

    Args:
        status_file: Path to ``status.json`` (created if missing).
        state: Persisted run state dict.
        issues_file: Path to issues JSON file.
        findings_file: Path to findings JSONL file.
        args: Parsed CLI arguments.
        run_mode: Labelled run mode string.
        reconcile_event: Reconciliation payload from ``reconcile_open_workload``.
        previous_last_run_at: Timestamp from the prior run (for staleness).
        run_metrics: Per-cycle metric counters (defaults used when None).
    """
    if status_file.exists():
        try:
            status = json.loads(status_file.read_text())
            if not isinstance(status, dict):
                status = {}
        except Exception:
            status = {}
    else:
        status = {}

    generated_at = now_iso()
    last_run_before_update = status.get("last_run_at") or previous_last_run_at
    previous_age_seconds = age_seconds(last_run_before_update)
    threshold_seconds = int(args.staleness_threshold_seconds)

    findings_entries = 0
    if findings_file.exists():
        with findings_file.open("r", encoding="utf-8") as f:
            findings_entries = sum(1 for line in f if line.strip())

    issues_data = load_issues(issues_file)
    issues_list = issues_data.get("issues", [])
    status_counter = Counter(i.get("status", "unknown") for i in issues_list)
    actionable_issues = sum(
        1
        for i in issues_list
        if i.get("status", "open") not in NON_ACTIONABLE_ISSUE_STATUSES
    )
    raw_open_issues = sum(1 for i in issues_list if i.get("status", "open") == "open")
    refactor_queue = _build_refactor_queue_snapshot()
    status["generated_at"] = generated_at
    status["last_run_at"] = generated_at
    status["run_mode"] = run_mode
    status["fix_configuration"] = {
        "fix_engine": args.fix_engine,
        "claude_cmd_template": sanitize_command_template(args.claude_cmd_template),
    }
    active_prs_file = status_file.parent / "active_prs.json"
    live_open_prs = 0
    if active_prs_file.exists():
        try:
            active_prs_data = json.loads(active_prs_file.read_text())
            live_open_prs = len(active_prs_data.get("prs", {}))
        except Exception:
            live_open_prs = int(state.get("open_prs", 0))
    else:
        live_open_prs = int(state.get("open_prs", 0))

    status["current_counts"] = {
        "open_issues": raw_open_issues,
        "actionable_issues": actionable_issues,
        "open_prs": live_open_prs,
        "created_records_total": len(state.get("created", [])),
        "issue_records_total": len(issues_list),
        "findings_entries": findings_entries,
        "refactor_queue_total": refactor_queue["total"],
        "by_status": dict(status_counter),
    }
    status["refactor_queue"] = refactor_queue
    status["last_reconciliation"] = reconcile_event
    status["staleness"] = {
        "threshold_seconds": threshold_seconds,
        "age_seconds": 0,
        "is_stale": False,
        "stale_after": (
            datetime.now(timezone.utc) + timedelta(seconds=threshold_seconds)
        ).isoformat(),
        "previous_last_run_at": last_run_before_update,
        "previous_age_seconds": previous_age_seconds,
        "was_stale_before_run": previous_age_seconds is not None
        and previous_age_seconds > threshold_seconds,
    }
    status["manual_one_cycle_command"] = build_active_cycle_command(args)
    status["issue_cycle_command"] = build_issue_cycle_command(args)
    status["pr_cycle_command"] = build_pr_cycle_command(args)
    status["merge_cycle_command"] = build_merge_cycle_command(args)
    status["orchestrated_cycle_command"] = build_orchestrated_cycle_command(args)
    status["refactor_cycle_command"] = build_refactor_cycle_command(args)
    status["reconcile_only_command"] = build_reconcile_only_command(args)
    status["verification_only_command"] = build_verification_only_command(args)
    status["docs_index_refresh_command"] = build_docs_index_refresh_command(args)
    status["detector_catalog"] = DETECTOR_CATALOG
    latest_run_metrics = dict(
        run_metrics
        or {
            "findings_detected": 0,
            "findings_written": 0,
            "issues_created": 0,
            "fix_attempts": 0,
            "prs_created": 0,
            "fixes_verified": 0,
            "fixes_failed_verification": 0,
            "unresolved_open": 0,
            "findings_suppressed_by_cooldown": 0,
            "issues_escalated_max_retries": 0,
            "merge_attempts": 0,
            "merges_succeeded": 0,
            "merges_failed": 0,
            "merged_pr_urls": [],
            "blocked_events": 0,
            "blocked_reasons": [],
        }
    )
    latest_run_metrics["refactor_queue_total"] = refactor_queue["total"]
    latest_run_metrics["refactor_queue_pending_review"] = refactor_queue[
        "pending_review"
    ]
    latest_run_metrics["refactor_queue_approved"] = refactor_queue["approved"]
    latest_run_metrics["refactor_queue_executing"] = refactor_queue["executing"]
    latest_run_metrics["refactor_queue_completed"] = refactor_queue["completed"]
    latest_run_metrics["refactor_queue_aborted"] = refactor_queue["aborted"]
    status["latest_run_metrics"] = latest_run_metrics

    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
