"""Merge cycle phase: fetch open PRs, evaluate, merge eligible ones."""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from bluei.engine.constants import AGENT_ROOT
from bluei.engine.gh import (
    evaluate_pr_check_health,
    evaluate_pr_mergeability,
    evaluate_pr_reviews,
    fetch_open_prs_for_merge,
    merge_failure_requires_pr_fix,
    merge_pr,
    repo_is_sandbox,
)
from bluei.engine.models import parse_iso
from bluei.engine.rebase_sweep import sweep_rebase
from bluei.engine.state import (
    _append_text,
    reconcile_open_workload,
)
from bluei.engine.commands.helpers import (
    _autonomous_review_gate_passes,
    _load_review_state,
    _triage_pr_back_to_fix_cycle,
)

logger = logging.getLogger(__name__)

MergeCycleResult = Tuple[
    int,
    int,
    int,
    List[str],
    int,
    int,
    List[str],
    Dict[str, Any],
]


def run_merge_cycle_phase(*args, **kwargs) -> MergeCycleResult:
    """Run the merge cycle: fetch open PRs, evaluate health/reviews/mergeability, merge eligible."""
    if args:
        ctx = args[0]
        repo_path = ctx.repo_path
        log_file = ctx.log_file
        review_state_file = ctx.review_state_file
        state = ctx.state
        issues_data = ctx.issues_data
        args = ctx.args
        gh_repo_slug = ctx.gh_repo_slug
        merges_failed = ctx.merges_failed
        merges_succeeded = ctx.merges_succeeded
        merge_attempts = ctx.merge_attempts
        merged_pr_urls = ctx.merged_pr_urls
        open_prs = ctx.open_prs
        open_issues = ctx.open_issues
        blocked_reasons = ctx.blocked_reasons
        reconcile_event = ctx.reconcile_event
    else:
        repo_path = kwargs["repo_path"]
        log_file = kwargs["log_file"]
        review_state_file = kwargs["review_state_file"]
        state = kwargs["state"]
        issues_data = kwargs["issues_data"]
        args = kwargs["args"]
        gh_repo_slug = kwargs["gh_repo_slug"]
        merges_failed = kwargs["merges_failed"]
        merges_succeeded = kwargs["merges_succeeded"]
        merge_attempts = kwargs["merge_attempts"]
        merged_pr_urls = kwargs["merged_pr_urls"]
        open_prs = kwargs["open_prs"]
        open_issues = kwargs["open_issues"]
        blocked_reasons = kwargs["blocked_reasons"]
        reconcile_event = kwargs["reconcile_event"]

    from bluei.engine.orchestrator import set_issue_status

    if not args.auto_merge_sandbox:
        reason = "merge-cycle-skip: auto-merge flag not enabled"
        _append_text(log_file, reason)
        blocked_reasons.append(reason)
        return (
            merges_failed,
            merges_succeeded,
            merge_attempts,
            merged_pr_urls,
            open_prs,
            open_issues,
            blocked_reasons,
            reconcile_event,
        )
    if not gh_repo_slug or not repo_is_sandbox(gh_repo_slug):
        reason = f"merge-cycle-block: repo not sandbox ({gh_repo_slug or 'unknown'})"
        _append_text(log_file, reason)
        blocked_reasons.append(reason)
        return (
            merges_failed,
            merges_succeeded,
            merge_attempts,
            merged_pr_urls,
            open_prs,
            open_issues,
            blocked_reasons,
            reconcile_event,
        )

    open_pr_list = fetch_open_prs_for_merge(gh_repo_slug, cwd=repo_path)
    now = datetime.now(timezone.utc)
    cooldown_seconds = args.merge_cooldown_minutes * 60
    for pr in open_pr_list:
        pr_number = int(pr.get("number"))
        pr_url = str(pr.get("url") or "")
        created_at = parse_iso(pr.get("createdAt"))
        age = int((now - created_at).total_seconds()) if created_at else 0

        if bool(pr.get("isDraft")):
            _append_text(log_file, f"merge-skip: pr=#{pr_number} draft=true")
            continue

        if age < cooldown_seconds:
            _append_text(
                log_file,
                f"merge-skip: pr=#{pr_number} cooldown age_seconds={age} required={cooldown_seconds}",
            )
            continue

        check_health = evaluate_pr_check_health(gh_repo_slug, pr_number, cwd=repo_path)
        if not check_health.get("eligible", False):
            merges_failed += 1
            reason = str(check_health.get("reason") or "checks-not-eligible")
            detail = f"merge-block: pr=#{pr_number} reason={reason}"
            blocked_reasons.append(detail)
            _append_text(log_file, detail)
            continue

        review_status = evaluate_pr_reviews(gh_repo_slug, pr_number, cwd=repo_path)
        if not review_status.get("eligible", False):
            review_state = _load_review_state(review_state_file)
            autonomous_ok, autonomous_reason = _autonomous_review_gate_passes(
                review_state, pr_number
            )
            if autonomous_ok:
                _append_text(
                    log_file,
                    f"merge-autonomous-gate-pass: pr=#{pr_number} reason={autonomous_reason}",
                )
            else:
                merges_failed += 1
                reason = str(review_status.get("reason") or "review-not-approved")
                detail = f"merge-block: pr=#{pr_number} reason={reason} autonomous_gate={autonomous_reason}"
                blocked_reasons.append(detail)
                _append_text(log_file, detail)
                continue

        mergeability = evaluate_pr_mergeability(gh_repo_slug, pr_number, cwd=repo_path)
        if not mergeability.get("eligible", False):
            reason = str(mergeability.get("reason") or "merge-state-not-eligible")
            merge_state_status = str(
                mergeability.get("merge_state_status") or ""
            ).upper()
            if merge_state_status == "UNKNOWN" or reason == "merge-state-unknown":
                mergeability = {
                    **mergeability,
                    "eligible": True,
                    "requires_pr_fix": False,
                    "merge_state_status": "UNKNOWN",
                    "reason": "merge-state-unknown-proceed-cautiously",
                }
                _append_text(
                    log_file,
                    f"merge-caution: pr=#{pr_number} normalized legacy unknown merge-state to cautious pass",
                )
            else:
                branch = str(pr.get("headRefName") or "")
                if mergeability.get("requires_pr_fix", False):
                    for issue in issues_data.get("issues", []):
                        issue_github = (
                            issue.get("github", {})
                            if isinstance(issue.get("github"), dict)
                            else {}
                        )
                        if int(issue_github.get("pr_number") or 0) == pr_number:
                            _triage_pr_back_to_fix_cycle(
                                issue=issue,
                                pr_number=pr_number,
                                pr_url=pr_url,
                                branch=branch,
                                reason=reason,
                                log_file=log_file,
                            )
                    _append_text(
                        log_file,
                        f"merge-triaged: pr=#{pr_number} reason={reason}",
                    )
                    continue

                merges_failed += 1
                detail = f"merge-block: pr=#{pr_number} reason={reason}"
                blocked_reasons.append(detail)
                _append_text(log_file, detail)
                continue

        merge_attempts += 1
        if not check_health.get("has_checks", False):
            _append_text(
                log_file,
                f"merge-caution: pr=#{pr_number} no checks found; proceeding",
            )

        if getattr(args, "regression_check", False):
            from bluei.engine.regression import check_regressions
            from bluei.engine.utils import run_no_capture

            head_ref = str(pr.get("headRefName", ""))
            base_ref = str(pr.get("baseRefName", "main"))
            if head_ref:
                _append_text(
                    log_file,
                    f"regression: checking PR #{pr_number} ({head_ref} vs {base_ref})",
                )
                run_no_capture(["git", "fetch", "origin", head_ref], cwd=repo_path)
                regression_findings = check_regressions(
                    repo_path,
                    f"origin/{base_ref}",
                    f"origin/{head_ref}",
                    log_file,
                )
                if regression_findings:
                    detail = f"regression: {len(regression_findings)} finding(s) detected — blocking merge"
                    merges_failed += 1
                    blocked_reasons.append(detail)
                    _append_text(log_file, detail)
                    continue

        merged, merge_reason = merge_pr(
            gh_repo_slug, pr_number, dry_run=args.dry_run, cwd=repo_path
        )
        if merged:
            merges_succeeded += 1
            if pr_url:
                merged_pr_urls.append(pr_url)
            open_prs = max(0, open_prs - 1)
            _append_text(
                log_file,
                f"merge-success: pr=#{pr_number} reason={merge_reason}",
            )

            for issue in issues_data.get("issues", []):
                issue_github = (
                    issue.get("github", {})
                    if isinstance(issue.get("github"), dict)
                    else {}
                )
                if int(issue_github.get("pr_number") or 0) == pr_number:
                    set_issue_status(
                        issue,
                        "resolved_merged",
                        f"PR merged: {pr_url or pr_number}",
                    )

            if args.auto_rebase_enabled:
                _append_text(log_file, "auto-rebase: starting sibling sweep")
                try:
                    base_branch = str(pr.get("baseRefName", "main"))
                    rebase_stats_file = getattr(args, "rebase_stats_file", None) or (
                        AGENT_ROOT / "state" / "rebase_stats.jsonl"
                    )
                    rebase_result = sweep_rebase(
                        repo_path=repo_path,
                        gh_repo_slug=gh_repo_slug,
                        merged_pr_number=pr_number,
                        base_branch=base_branch,
                        log_file=log_file,
                        dry_run=args.dry_run,
                        max_prs=args.rebase_max_prs,
                        rebase_stats_file=rebase_stats_file,
                    )
                    for r in rebase_result.get("rebased", []):
                        _append_text(log_file, f"rebase-ok: pr=#{r['pr_number']}")
                    for c in rebase_result.get("conflicted", []):
                        _append_text(
                            log_file,
                            f"rebase-conflict: pr=#{c['pr_number']} files={c['files']}",
                        )
                        blocked_reasons.append(
                            f"rebase-conflict: pr=#{c['pr_number']} files={', '.join(c['files'])}"
                        )
                    for s in rebase_result.get("skipped", []):
                        _append_text(
                            log_file,
                            f"rebase-skip: pr=#{s['pr_number']} reason={s['reason']}",
                        )
                except Exception as exc:
                    _append_text(log_file, f"auto-rebase-error: {exc}")
            break
        else:
            branch = str(pr.get("headRefName") or "")
            if merge_failure_requires_pr_fix(merge_reason):
                for issue in issues_data.get("issues", []):
                    issue_github = (
                        issue.get("github", {})
                        if isinstance(issue.get("github"), dict)
                        else {}
                    )
                    if int(issue_github.get("pr_number") or 0) == pr_number:
                        _triage_pr_back_to_fix_cycle(
                            issue=issue,
                            pr_number=pr_number,
                            pr_url=pr_url,
                            branch=branch,
                            reason=merge_reason,
                            log_file=log_file,
                        )
                _append_text(
                    log_file,
                    f"merge-triaged: pr=#{pr_number} reason={merge_reason}",
                )
                continue

            merges_failed += 1
            detail = f"merge-failed: pr=#{pr_number} reason={merge_reason}"
            blocked_reasons.append(detail)
            _append_text(log_file, detail)

    if args.live_github_actions and not args.dry_run:
        open_issues, open_prs, reconcile_event = reconcile_open_workload(
            repo_path=repo_path,
            state=state,
            log_file=log_file,
            simulate_open_issues=args.simulate_open_issues,
            simulate_open_prs=args.simulate_open_prs,
        )

    return (
        merges_failed,
        merges_succeeded,
        merge_attempts,
        merged_pr_urls,
        open_prs,
        open_issues,
        blocked_reasons,
        reconcile_event,
    )
