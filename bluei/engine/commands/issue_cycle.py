"""Issue creation phase: create GitHub issues for discovered findings."""

from pathlib import Path
from typing import Any, Dict, List, Tuple

IssueCreationResult = Tuple[
    List[Dict[str, Any]],
    int,
    List[str],
]


def run_issue_creation_phase(*args, **kwargs) -> IssueCreationResult:
    """Create issues for discovered findings.

    Returns:
        (created_issues, open_issues, blocked_reasons)
    """
    if args:
        ctx = args[0]
        issues_data = ctx.issues_data
        eligible_findings = ctx.eligible_findings
        refactor_routed_items = ctx.refactor_routed_items
        log_file = ctx.log_file
        args = ctx.args
        gh_repo_slug = ctx.gh_repo_slug
        open_issues = ctx.open_issues
    else:
        issues_data = kwargs["issues_data"]
        eligible_findings = kwargs["eligible_findings"]
        refactor_routed_items = kwargs["refactor_routed_items"]
        log_file = kwargs["log_file"]
        args = kwargs["args"]
        gh_repo_slug = kwargs["gh_repo_slug"]
        open_issues = kwargs["open_issues"]

    from bluei.engine.orchestrator import (
        create_issues_for_findings,
        ensure_issue_for_finding,
        find_issue_for_finding,
        set_issue_status,
    )
    from bluei.engine.models import IssueStatus
    from bluei.engine.state import (
        count_actionable_issues,
        guard_open_issues,
        _append_text,
    )
    from bluei.engine.gh import create_or_update_github_issue, finding_from_issue_record
    from bluei.engine.constants import AGENT_ROOT

    created_issues: List[Dict[str, Any]] = []
    blocked_reasons: List[str] = []

    actionable_issue_count = count_actionable_issues(issues_data)
    _append_text(
        log_file,
        f"actionable-issues: raw_open={open_issues} actionable={actionable_issue_count}",
    )

    for routed_item in refactor_routed_items:
        finding = routed_item["finding"]
        existing_issue = find_issue_for_finding(issues_data, finding.finding_id)
        issue = ensure_issue_for_finding(
            issues_data=issues_data,
            finding=finding,
            confidence_threshold=args.issue_confidence_threshold,
        )
        if issue is None:
            continue
        if existing_issue is None:
            created_issues.append(issue)
            open_issues += 1
        refactor_meta = issue.setdefault("refactor", {})
        refactor_meta["phase"] = routed_item["refactor_work"].phase.value
        if routed_item.get("queued_work_id"):
            refactor_meta["queue_work_id"] = routed_item["queued_work_id"]
        refactor_meta["review_reason"] = routed_item.get("reason", "planning")
        set_issue_status(
            issue,
            IssueStatus.NEEDS_HUMAN_REFACTOR_REVIEW.value,
            routed_item.get("reason", "planning"),
        )

    from bluei.engine.state import canonical_markdown_rule
    reopened = 0
    if not args.dry_run and args.max_issues_per_run > 0:
        by_id = {issue.get("finding_id"): issue for issue in issues_data.get("issues", [])}
        for finding in eligible_findings:
            if reopened >= args.max_issues_per_run:
                break
            existing = by_id.get(finding.finding_id)
            if not canonical_markdown_rule(finding.rule) or not existing:
                continue
            github = existing.get("github") or {}
            if existing.get("status") not in {"resolved_verified", "resolved_merged", "closed"} or (
                (github.get("pr_number") or github.get("pr_url"))
                and existing.get("status") != "resolved_merged"
            ):
                continue
            ok, reason = guard_open_issues(count_actionable_issues(issues_data), args.open_issues_cap)
            if not ok:
                blocked_reasons.append(reason)
                break
            permitted = create_issues_for_findings(
                issues_data={"issues": []}, findings=[finding],
                confidence_threshold=args.issue_confidence_threshold, max_issues_per_run=1,
                cycle_signals_path=AGENT_ROOT / "state" / "cycle_signals.json",
            )
            if not permitted or permitted[0].get("issue_id") == "SUPPRESSED":
                continue
            if args.live_github_actions and github.get("issue_number") is not None:
                from bluei.engine.utils import run_capture
                rc, _ = run_capture([
                    "gh", "issue", "reopen", str(github["issue_number"]), "--repo", gh_repo_slug,
                ], cwd=Path(args.repo_path))
                if rc != 0:
                    blocked_reasons.append("recurrent Markdown GitHub issue could not be reopened")
                    continue
            if existing.get("status") == "resolved_merged":
                previous_pr = {key: github.pop(key) for key in ("pr_number", "pr_url", "branch") if key in github}
                if previous_pr:
                    existing.setdefault("history", []).append({"event": "previous_merged_pr", "github": previous_pr})
            set_issue_status(existing, "open", "Markdown finding recurred after verification")
            reopened += 1
            open_issues += 1

    actionable_issue_count = count_actionable_issues(issues_data)

    for _ in range(args.max_issues_per_run - reopened):
        ok, reason = guard_open_issues(actionable_issue_count, args.open_issues_cap)
        _append_text(log_file, reason)
        if not ok:
            blocked_reasons.append(reason)
            break

        batch = create_issues_for_findings(
            issues_data=issues_data,
            findings=eligible_findings,
            confidence_threshold=args.issue_confidence_threshold,
            max_issues_per_run=1,
            cycle_signals_path=AGENT_ROOT / "state" / "cycle_signals.json",
        )
        if not batch:
            break

        for issue in batch:
            if issue.get("issue_id") == "SUPPRESSED":
                suppressed_reason = issue.get("reason", "suppressed")
                _append_text(
                    log_file,
                    f"suppressed-cross-cycle: finding={issue['finding_id']} rule={issue.get('rule', '?')} reason={suppressed_reason}",
                )
                continue
            if args.live_github_actions:
                issue_finding = finding_from_issue_record(issue)
                if issue_finding is not None:
                    gh_issue = create_or_update_github_issue(
                        repo_slug=gh_repo_slug,
                        finding=issue_finding,
                        dry_run=args.dry_run,
                        log_file=log_file,
                        cwd=Path(args.repo_path),
                    )
                    issue_github = issue.setdefault("github", {})
                    if gh_issue.get("number") is not None:
                        issue_github["issue_number"] = gh_issue.get("number")
                    if gh_issue.get("url"):
                        issue_github["issue_url"] = gh_issue.get("url")

            created_issues.append(issue)
            open_issues += 1

    return created_issues, open_issues, blocked_reasons
