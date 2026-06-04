"""Discovery phase: find findings, apply cooldown, route refactors."""

from pathlib import Path
from typing import Any, Dict, List, Tuple

from bluei.engine.models import Finding
from bluei.engine.state import (
    _append_text,
    append_findings,
    count_actionable_issues,
    filter_findings_by_cooldown,
    guard_open_issues,
)

DiscoverResult = Tuple[
    List[Finding],
    int,
    List[Finding],
    List[Finding],
    List[Dict[str, Any]],
    List[str],
    bool,
]


def run_discover_phase(*args, **kwargs) -> DiscoverResult:
    """Run the discovery phase: find findings, apply cooldown, route refactors.

    Returns:
        (findings, written_findings, eligible_findings, suppressed_findings,
         refactor_routed_items, blocked_reasons, cap_ok)
    """
    if args:
        ctx = args[0]
        repo_path = ctx.repo_path
        docs_index_file = ctx.docs_index_file
        findings_file = ctx.findings_file
        log_file = ctx.log_file
        state = ctx.state
        issues_data = ctx.issues_data
        args = ctx.args
        run_issue_cycle = ctx.run_issue_cycle
        run_pr_cycle = ctx.run_pr_cycle
    else:
        repo_path = kwargs["repo_path"]
        docs_index_file = kwargs["docs_index_file"]
        findings_file = kwargs["findings_file"]
        log_file = kwargs["log_file"]
        state = kwargs["state"]
        issues_data = kwargs["issues_data"]
        args = kwargs["args"]
        run_issue_cycle = kwargs["run_issue_cycle"]
        run_pr_cycle = kwargs["run_pr_cycle"]

    from bluei.engine.orchestrator import discover_findings, route_findings_with_intent
    from bluei.engine.reforge import RefactorClass, classify_finding
    from bluei.engine.utils import is_path_tracked

    findings: List[Finding] = []
    eligible_findings: List[Finding] = []
    suppressed_findings: List[Finding] = []
    refactor_routed_items: List[Dict[str, Any]] = []
    blocked_reasons: List[str] = []
    cap_ok = True

    if run_issue_cycle:
        pre_discovery_actionable = count_actionable_issues(issues_data)
        cap_ok, cap_reason = guard_open_issues(
            pre_discovery_actionable, args.open_issues_cap
        )
        _append_text(log_file, f"pre-discovery-cap-check: {cap_reason}")
        if not cap_ok:
            blocked_reasons.append(cap_reason)

    if args.run_phase in ("verify-only",) or (run_issue_cycle and cap_ok):
        findings = discover_findings(
            repo_path, log_file=log_file, docs_index_file=docs_index_file
        )

    written_findings = append_findings(findings_file, findings)
    _append_text(
        log_file,
        f"discovery: findings_detected={len(findings)} findings_written={written_findings}",
    )
    eligible_findings, suppressed_findings = filter_findings_by_cooldown(
        findings=findings,
        state=state,
        cooldown_seconds=args.finding_cooldown_seconds,
        log_file=log_file,
    )
    if suppressed_findings:
        _append_text(
            log_file,
            f"cooldown-summary: suppressed_findings={len(suppressed_findings)}",
        )

    if args.live_github_actions and run_pr_cycle:
        tracked_findings: List[Finding] = []
        untracked_count = 0
        for finding in eligible_findings:
            if is_path_tracked(repo_path, finding.path):
                tracked_findings.append(finding)
            else:
                untracked_count += 1
                _append_text(
                    log_file,
                    f"discovery-skip: untracked path for live queue path={finding.path} rule={finding.rule}",
                )
        eligible_findings = tracked_findings
        if untracked_count:
            _append_text(
                log_file,
                f"discovery-skip-summary: untracked_findings={untracked_count}",
            )

    refactor_findings: List[Finding] = []
    remaining_findings: List[Finding] = []
    for finding in eligible_findings:
        if classify_finding(finding) == RefactorClass.REFACTOR_CLASS:
            refactor_findings.append(finding)
        else:
            remaining_findings.append(finding)
    if refactor_findings:
        routed = route_findings_with_intent(
            refactor_findings,
            confidence_threshold=args.issue_confidence_threshold,
            findings_file=findings_file,
            worktree_path=repo_path,
            log_file=log_file,
        )
        refactor_routed_items = list(routed.get("refactor_queue", []))
        _append_text(
            log_file,
            f"refactor-routing-summary: routed={len(refactor_routed_items)} skipped={len(routed.get('skipped', []))}",
        )
    eligible_findings = remaining_findings

    return (
        findings,
        written_findings,
        eligible_findings,
        suppressed_findings,
        refactor_routed_items,
        blocked_reasons,
        cap_ok,
    )
