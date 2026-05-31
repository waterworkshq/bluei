#!/usr/bin/env python3
"""Publish-state reconciliation and summary — extracted from review.cycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from bluei.review.models import (
    PublishStatus,
)
from bluei.app.models import (
    now_iso,
)
from bluei.review.types import PublishFilterResult

COMMENT_MAX_LINES = 60  # Guard against runaway output


def _build_pass_filter_result() -> PublishFilterResult:
    """Return a passing filter result for early/bypassed paths."""
    return PublishFilterResult(passed=True, decision="bypassed", failed_reason="")


@dataclass
class ReconciliationResult:
    """
    Result of reconciling current candidate findings against prior publish state.

    Attributes:
        new_findings: Finding IDs that have no prior publish record;
                      must be published (or skipped) in this run.
        already_published: Finding IDs that were published in a prior run
                           and are still present with the same fingerprint;
                           no action needed.
        absent_findings: Finding IDs that were published in a prior run
                         but are absent from the current candidate set;
                         treated as resolved.
        superseded_findings: Finding IDs whose fingerprint was previously
                             published but whose current candidate differs
                             (re-occurrence at same location); requires
                             re-evaluation before publishing.
        pending_findings: Finding IDs that exist in prior state but are
                         not yet published (still pending); no new action.
        all_prior_findings: Set of all finding IDs in prior publish state.
    """

    new_findings: List[str] = field(default_factory=list)
    already_published: List[str] = field(default_factory=list)
    absent_findings: List[str] = field(default_factory=list)
    superseded_findings: List[str] = field(default_factory=list)
    pending_findings: List[str] = field(default_factory=list)
    all_prior_findings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def reconcile_publish_state(
    current_candidates: List[Dict[str, Any]],
    prior_publish_state: Dict[str, Any],
) -> ReconciliationResult:
    """
    Compare current candidate findings against prior publish state and
    classify each finding by its publication status.

    Classification rules (evaluated in order):
    1. If a finding's ID is not in prior state at all          → new_findings
    2. If prior status is 'published' and fingerprint matches → already_published
    3. If prior status is 'published' and fingerprint differs  → superseded_findings
    4. If prior status is 'pending'/'failed'/'skipped'          → pending_findings
    5. Any prior ID absent from current_candidates              → absent_findings

    Args:
        current_candidates: List of finding dicts for the current run. Each
                            must contain at minimum ``finding_id`` and
                            ``finding_fingerprint``.
        prior_publish_state: The loaded ``review_publish_state.json`` dict
                             for the repo (findings + runs sub-dicts).

    Returns:
        ReconciliationResult with classified finding IDs.
    """
    prior_findings: Dict[str, Dict[str, Any]] = prior_publish_state.get("findings", {})

    # Build sets for efficient lookup
    prior_ids: set = set(prior_findings.keys())
    current_ids: set = {
        f.get("finding_id") for f in current_candidates if f.get("finding_id")
    }
    current_fps: Dict[str, str] = {
        f.get("finding_id"): f.get("finding_fingerprint", "")
        for f in current_candidates
        if f.get("finding_id")
    }

    new_findings: List[str] = []
    already_published: List[str] = []
    superseded_findings: List[str] = []
    pending_findings: List[str] = []

    for finding_id, current_fp in current_fps.items():
        if finding_id not in prior_ids:
            new_findings.append(finding_id)
            continue

        prior_entry = prior_findings[finding_id]
        prior_status = str(prior_entry.get("status", ""))

        # Normalize status: handle both enum values and raw strings
        # PublishStatus enum members are strings, so direct comparison works
        if prior_status == PublishStatus.PUBLISHED.value:
            prior_fp = prior_entry.get("finding_fingerprint", "")
            if current_fp == prior_fp:
                already_published.append(finding_id)
            else:
                superseded_findings.append(finding_id)
        elif prior_status in {
            PublishStatus.PENDING.value,
            PublishStatus.FAILED.value,
            PublishStatus.SKIPPED.value,
            PublishStatus.SUPERSEDED.value,
            "",
        }:
            pending_findings.append(finding_id)
        else:
            # Unknown status — treat as pending
            pending_findings.append(finding_id)

    # Absent: was in prior state but not in current candidates
    absent_findings = sorted(prior_ids - current_ids)

    return ReconciliationResult(
        new_findings=sorted(new_findings),
        already_published=sorted(already_published),
        absent_findings=absent_findings,
        superseded_findings=sorted(superseded_findings),
        pending_findings=sorted(pending_findings),
        all_prior_findings=sorted(prior_ids),
    )


def build_publish_entry(
    finding_id: str,
    status: PublishStatus,
    run_id: Optional[str] = None,
    error: Optional[str] = None,
    finding_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a per-finding publish-state entry dict.

    Args:
        finding_id: Stable finding ID (e.g. ``rf-abc123-000``).
        status: PublishStatus enum value.
        run_id: Run ID that last updated this entry (optional).
        error: Error message if status is ``failed`` (optional).
        finding_fingerprint: The finding's fingerprint (optional, stored
                              for cross-run fingerprint comparison).

    Returns:
        A dict suitable for storage in the ``findings`` sub-dict of
        ``review_publish_state.json``.
    """
    entry: Dict[str, Any] = {
        "status": status.value,
        "updated_at": now_iso(),
    }
    if run_id is not None:
        entry["run_id"] = run_id
    if error is not None:
        entry["error"] = error
    if finding_fingerprint is not None:
        entry["finding_fingerprint"] = finding_fingerprint
    return entry


def compute_run_publish_status(
    finding_statuses: List[PublishStatus],
) -> PublishStatus:
    """
    Compute the rollup publish status for a run from its findings' statuses.

    Rollup rule (worst-case wins):
        failed  > pending/skipped/superseded > published > absent
    (i.e. if any finding failed, the run is failed;
          else if any is pending/skipped/superseded, the run is pending;
          else if all are published, the run is published;
          else absent.)

    Args:
        finding_statuses: List of PublishStatus values for the run's findings.

    Returns:
        The rollup PublishStatus for the run.
    """
    if not finding_statuses:
        return PublishStatus.PENDING

    # Check for failure first
    if PublishStatus.FAILED in finding_statuses:
        return PublishStatus.FAILED
    # Check for non-terminal pending-like statuses
    pending_like = {
        PublishStatus.PENDING,
        PublishStatus.SKIPPED,
        PublishStatus.SUPERSEDED,
    }
    if any(s in pending_like for s in finding_statuses):
        return PublishStatus.PENDING
    # If any finding is published, the run published something
    if any(s == PublishStatus.PUBLISHED for s in finding_statuses):
        return PublishStatus.PUBLISHED
    # All absent
    return PublishStatus.ABSENT


def build_run_publish_entry(
    status: PublishStatus,
    run_id: Optional[str] = None,
    findings_total: int = 0,
    findings_published: int = 0,
    findings_failed: int = 0,
    error: Optional[str] = None,
    targeted_pr_number: Optional[int] = None,
    targeted_pr_url: Optional[str] = None,
    lifecycle_phase: Optional[str] = None,
    comment_url: Optional[str] = None,
    # Phase G6: publish filter signals
    publish_filter_decision: Optional[str] = None,
    publish_filter_reason: Optional[str] = None,
    rollout_eligible: Optional[bool] = None,
    attention_recommended: Optional[bool] = None,
    # Phase G7: monitored safety signals
    safety_circuit_open: Optional[bool] = None,
    safety_failure_count: Optional[int] = None,
    safety_cooldown_until: Optional[str] = None,
    auto_rollback_active: Optional[bool] = None,
    auto_rollback_reason: Optional[str] = None,
    auto_rollback_triggered_at: Optional[str] = None,
    operator_action_required: Optional[bool] = None,
    operator_action_summary: Optional[str] = None,
    suggested_review_care_patch: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a per-run publish-state entry dict.

    Args:
        status: Rollup PublishStatus for the run.
        run_id: The run's ID (optional).
        findings_total: Total number of findings in the run.
        findings_published: How many were successfully published.
        findings_failed: How many failed to publish.
        error: Error message if status is ``failed`` (optional).
        targeted_pr_number: The PR number explicitly targeted for publication (optional).
        targeted_pr_url: The PR URL explicitly targeted (optional).
        lifecycle_phase: Explicit lifecycle phase label (optional).  Values:
            ``guard-disabled``, ``local-only``, ``guarded-live-published``,
            ``guarded-live-refused``, ``guarded-live-failed``, ``filter-blocked``,
            ``safety-blocked``.
        comment_url: The GitHub comment URL if publication succeeded (optional).
        publish_filter_decision: Phase G6 filter decision (optional). Values:
            ``pass``, ``fail``, ``skipped``, ``bypassed``.
        publish_filter_reason: Human-readable reason for filter decision (optional).
        rollout_eligible: Whether the run is eligible for live publish (optional).
        attention_recommended: Whether alerts/attention are recommended (optional).
        safety_circuit_open: Phase G7 circuit-breaker open state (optional).
        safety_failure_count: Phase G7 consecutive failure count (optional).
        safety_cooldown_until: Phase G7 cooldown expiry ISO timestamp (optional).
        auto_rollback_active: Phase G7 rollback-active flag (optional).
        auto_rollback_reason: Phase G7 rollback reason (optional).
        auto_rollback_triggered_at: Phase G7 rollback activation timestamp (optional).
        operator_action_required: Whether operator intervention is recommended now.
        operator_action_summary: Short action summary for operators.
        suggested_review_care_patch: Suggested review_care config mutation payload.

    Returns:
        A dict suitable for storage in the ``runs`` sub-dict of
        ``review_publish_state.json``.
    """
    entry: Dict[str, Any] = {
        "status": status.value,
        "updated_at": now_iso(),
        "findings_total": findings_total,
        "findings_published": findings_published,
        "findings_failed": findings_failed,
    }
    if run_id is not None:
        entry["run_id"] = run_id
    if error is not None:
        entry["error"] = error
    if targeted_pr_number is not None:
        entry["targeted_pr_number"] = targeted_pr_number
    if targeted_pr_url is not None:
        entry["targeted_pr_url"] = targeted_pr_url
    if lifecycle_phase is not None:
        entry["lifecycle_phase"] = lifecycle_phase
    if comment_url is not None:
        entry["comment_url"] = comment_url
    # Phase G6: publish filter monitoring signals
    if publish_filter_decision is not None:
        entry["publish_filter_decision"] = publish_filter_decision
    if publish_filter_reason is not None:
        entry["publish_filter_reason"] = publish_filter_reason
    if rollout_eligible is not None:
        entry["rollout_eligible"] = rollout_eligible
    if attention_recommended is not None:
        entry["attention_recommended"] = attention_recommended
    # Phase G7: monitored safety signals
    if safety_circuit_open is not None:
        entry["safety_circuit_open"] = safety_circuit_open
    if safety_failure_count is not None:
        entry["safety_failure_count"] = safety_failure_count
    if safety_cooldown_until is not None:
        entry["safety_cooldown_until"] = safety_cooldown_until
    if auto_rollback_active is not None:
        entry["auto_rollback_active"] = auto_rollback_active
    if auto_rollback_reason is not None:
        entry["auto_rollback_reason"] = auto_rollback_reason
    if auto_rollback_triggered_at is not None:
        entry["auto_rollback_triggered_at"] = auto_rollback_triggered_at
    if operator_action_required is not None:
        entry["operator_action_required"] = operator_action_required
    if operator_action_summary is not None:
        entry["operator_action_summary"] = operator_action_summary
    if suggested_review_care_patch is not None:
        entry["suggested_review_care_patch"] = suggested_review_care_patch
    return entry


def build_review_summary_comment(
    repo: str,
    run_id: str,
    reconciliation: ReconciliationResult,
    run_status: str = "completed",
    run_error: Optional[str] = None,
    *,
    pr_number: Optional[int] = None,
    include_absent: bool = True,
    include_superseded: bool = True,
    max_finding_lines: int = 5,
) -> str:
    """Build a deterministic review-summary comment body for an autonomous run.

    Output is stable for the same inputs (sorted keys, deterministic ordering).

    Args:
        repo: Repository name.
        run_id: Run identifier.
        reconciliation: ReconciliationResult from publish-state comparison.
        run_status: Run status string (default "completed").
        run_error: Optional error string for the header.
        pr_number: Optional PR number for the header.
        include_absent: Whether to list absent/resolved findings.
        include_superseded: Whether to list superseded findings.
        max_finding_lines: Max finding rows per table section.

    Returns:
        The full comment body string (~60 lines max).
    """
    lines: List[str] = []
    WIDTH = 80

    def rule(char: str = "─", length: int = WIDTH) -> str:
        return char * length

    def header(text: str, level: int = 2) -> str:
        return f"{'#' * level} {text}"

    def bold(text: str) -> str:
        return f"**{text}**"

    def inline_code(text: str) -> str:
        return f"`{text}`"

    def fmt_table(rows: List[Tuple[str, ...]], cols: List[str]) -> List[str]:
        """Build a simple ASCII table. cols is list of header names."""
        if not rows:
            return []
        # Compute column widths
        widths = [len(c) for c in cols]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(str(cell)))
        # Header
        header_row = " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
        sep = "-|-".join("-" * w for w in widths)
        out = [header_row, sep]
        for row in rows:
            data_row = " | ".join(
                str(row[i] if i < len(row) else "").ljust(widths[i])
                for i in range(len(cols))
            )
            out.append(data_row)
        return out

    # ---- Header ----
    lines.append(header("bluei Review Summary", 2))
    lines.append("")
    meta_parts = [
        f"**Repo:** {inline_code(repo)}",
        f"**Run:** {inline_code(run_id)}",
    ]
    if pr_number is not None:
        meta_parts.append(f"**PR:** {inline_code(f'#{pr_number}')}")
    meta_parts.append(f"**Status:** {inline_code(run_status)}")
    if run_error:
        meta_parts.append(f"**Error:** {inline_code(str(run_error)[:60])}")
    lines.append("  ·  ".join(meta_parts))
    lines.append("")

    total = (
        len(reconciliation.new_findings)
        + len(reconciliation.already_published)
        + len(reconciliation.absent_findings)
        + len(reconciliation.superseded_findings)
        + len(reconciliation.pending_findings)
    )
    p_count = len(reconciliation.already_published)
    f_count = len(reconciliation.superseded_findings)
    a_count = len(reconciliation.absent_findings)

    stats_line = (
        f"Specks: {total} total"
        f" · {bold(f'+{len(reconciliation.new_findings)}')} new"
        f" · {bold(f'✓{p_count}')} published"
        f" · {bold(f'~{f_count}')} superseded"
        f" · {bold(f'○{a_count}')} absent"
    )
    if reconciliation.pending_findings:
        stats_line += f" · {bold(f'?{len(reconciliation.pending_findings)}')} pending"
    lines.append(stats_line)
    lines.append("")

    # ---- New findings table ----
    if reconciliation.new_findings:
        lines.append(header("New Specks", 3))
        new_rows = [(fid,) for fid in reconciliation.new_findings[:max_finding_lines]]
        if len(reconciliation.new_findings) > max_finding_lines:
            new_rows.append(
                ("…", f"+{len(reconciliation.new_findings) - max_finding_lines} more")
            )
        for row in fmt_table(new_rows, ["Speck ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Already published ----
    if reconciliation.already_published:
        lines.append(header("Already Published (No Action Needed)", 3))
        pub_rows = [
            (fid,) for fid in reconciliation.already_published[:max_finding_lines]
        ]
        if len(reconciliation.already_published) > max_finding_lines:
            pub_rows.append(
                (
                    "…",
                    f"+{len(reconciliation.already_published) - max_finding_lines} more",
                )
            )
        for row in fmt_table(pub_rows, ["Speck ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Superseded ----
    if include_superseded and reconciliation.superseded_findings:
        lines.append(header("Superseded (Re-occurrence — Review Before Publishing)", 3))
        sup_rows = [
            (fid,) for fid in reconciliation.superseded_findings[:max_finding_lines]
        ]
        if len(reconciliation.superseded_findings) > max_finding_lines:
            sup_rows.append(
                (
                    "…",
                    f"+{len(reconciliation.superseded_findings) - max_finding_lines} more",
                )
            )
        for row in fmt_table(sup_rows, ["Speck ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Absent (resolved) ----
    if include_absent and reconciliation.absent_findings:
        lines.append(header("Absent — Resolved Since Last Run", 3))
        abs_rows = [
            (fid,) for fid in reconciliation.absent_findings[:max_finding_lines]
        ]
        if len(reconciliation.absent_findings) > max_finding_lines:
            abs_rows.append(
                (
                    "…",
                    f"+{len(reconciliation.absent_findings) - max_finding_lines} more",
                )
            )
        for row in fmt_table(abs_rows, ["Speck ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Pending (from prior state, not yet published) ----
    if reconciliation.pending_findings:
        lines.append(header("Pending from Prior Run (Not Yet Published)", 3))
        pend_rows = [
            (fid,) for fid in reconciliation.pending_findings[:max_finding_lines]
        ]
        if len(reconciliation.pending_findings) > max_finding_lines:
            pend_rows.append(
                (
                    "…",
                    f"+{len(reconciliation.pending_findings) - max_finding_lines} more",
                )
            )
        for row in fmt_table(pend_rows, ["Speck ID"]):
            lines.append(f"  {row}")
        lines.append("")

    # ---- Footer ----
    lines.append(rule("─"))
    lines.append(f" _Generated by bluei · run {inline_code(run_id)}_")

    # Guard against runaway output
    if len(lines) > COMMENT_MAX_LINES:
        lines = lines[:COMMENT_MAX_LINES]
        lines.append(f" _(output truncated at {COMMENT_MAX_LINES} lines)_")

    return "\n".join(lines) + "\n"
