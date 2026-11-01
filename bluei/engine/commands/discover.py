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
    canonical_markdown_rule,
    _load_markdown_identities,
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
    ctx = None
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
    detector_outcomes: Dict[str, Dict[str, Any]] = {}

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
            repo_path,
            log_file=log_file,
            docs_index_file=docs_index_file,
            outcomes=detector_outcomes,
        )
    elif not cap_ok:
        detector_outcomes["discovery"] = {
            "status": "skipped",
            "count": 0,
            "reason": f"open-issue cap reached; discovery not run ({blocked_reasons[-1] if blocked_reasons else 'cap'})",
        }

    if ctx is not None:
        ctx.detector_outcomes = detector_outcomes
    for detector, record in detector_outcomes.items():
        _append_text(
            log_file,
            f"detector-outcome: {detector} status={record.get('status')} "
            f"count={record.get('count', 0)}"
            + (f" reason={record['reason']}" if record.get("reason") else ""),
        )

    written_findings = append_findings(findings_file, findings)
    if ctx is not None and ctx.run_id:
        from bluei.engine.state_io import atomic_json_write
        atomic_json_write(ctx.status_file.parent / "discovery-current.json", {
            "run_id": ctx.run_id, "findings_detected": len(findings),
            "findings_written": written_findings, "detector_outcomes": detector_outcomes,
        })
    _append_text(
        log_file,
        f"discovery: findings_detected={len(findings)} findings_written={written_findings}",
    )
    if any(record.get("status") in ("failed", "unavailable") for record in detector_outcomes.values()):
        # Findings are persisted for the record but not routed anywhere; the
        # real cap result still passes through untouched.
        blocked_reasons.append("discovery incomplete; findings retained but not routed")
        return (findings, written_findings, [], [], [], blocked_reasons, cap_ok)

    aliases: Dict[str, set[str]] = {}
    if any(canonical_markdown_rule(finding.rule) for finding in findings):
        _load_markdown_identities(findings_file, aliases=aliases)
    if args.run_phase == "verify-only":
        return (findings, written_findings, [], [], [], blocked_reasons, cap_ok)
    issues_by_id: Dict[str, List[Dict[str, Any]]] = {}
    all_issues_by_id: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues_data.get("issues", []) if aliases else []:
        fid = issue.get("finding_id")
        all_issues_by_id.setdefault(fid, []).append(issue)
        if issue.get("status") not in {"resolved_verified", "resolved_merged", "closed"}:
            issues_by_id.setdefault(fid, []).append(issue)
    pr_ids = set()
    for record in state.get("created", []) if aliases else []:
        if record.get("github_pr_number") is not None or record.get("github_pr_url"):
            for fid in record.get("linked_finding_ids", []):
                history = all_issues_by_id.get(fid, [])
                if not history or fid in issues_by_id:
                    pr_ids.add(fid)
    routed_findings = []
    for finding in findings:
        ids = aliases.get(finding.finding_id, {finding.finding_id})
        if len(ids) < 2:
            routed_findings.append(finding)
            continue
        matches = [issue for fid in ids for issue in issues_by_id.get(fid, [])]
        linked_ids = {issue["finding_id"] for issue in matches} | ids.intersection(pr_ids)
        if len(ids) > 1 and (len(matches) > 1 or len(linked_ids) > 1):
            reason = f"Markdown alias conflict for {finding.finding_id}; manual resolution required"
            blocked_reasons.append(reason)
            _append_text(log_file, reason)
            detector_outcomes["markdown-identity"] = {
                "status": "failed", "count": 0, "reason": reason,
            }
            suppressed_findings.append(finding)
            continue
        if len(ids) > 1 and linked_ids:
            finding.finding_id = next(iter(linked_ids))
        routed_findings.append(finding)
    if detector_outcomes.get("markdown-identity", {}).get("status") == "failed":
        return (findings, written_findings, [], findings, [], blocked_reasons, cap_ok)
    eligible_findings, suppressed_findings = filter_findings_by_cooldown(
        findings=routed_findings,
        state=state,
        cooldown_seconds=args.finding_cooldown_seconds,
        log_file=log_file,
        **({"aliases": aliases} if aliases else {}),
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
