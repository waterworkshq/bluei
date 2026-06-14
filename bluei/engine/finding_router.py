"""Finding router: bucket findings into intentional execution lanes.

Moved from orchestrator.py to reduce its surface area. Classifies each
finding into autofix-safe, refactor-queue, human-review, or skipped,
and (when a worktree is available) records queue metadata for the
refactor pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.models import Finding
from bluei.engine.reforge import (
    RefactorClass,
    RefactorWork,
    can_auto_refactor,
    classify_finding,
)
from bluei.engine.refactor_queue import enqueue_refactor_work
from bluei.engine.state import _append_text, save_refactor_work


def choose_safe_autofix_items(
    findings: List[Finding], confidence_threshold: float
) -> List[Finding]:
    return [
        f
        for f in findings
        if f.safe_to_autofix and f.confidence >= confidence_threshold
    ]


def route_findings_with_intent(
    findings: List[Finding],
    confidence_threshold: float,
    findings_file: Optional[Path] = None,
    worktree_path: Optional[Path] = None,
    log_file: Optional[Path] = None,
    detected_frameworks: Optional[List[str]] = None,
) -> Dict[str, List[Any]]:
    """Route findings into intentional execution lanes.

    Buckets:
    - autofix_safe: deterministic low-risk fixes
    - refactor_queue: structural refactor findings, with queue metadata when queued
    - human_review: non-autofix findings needing manual or later LLM handling
    - skipped: below confidence threshold
    """
    routed: Dict[str, List[Any]] = {
        "autofix_safe": [],
        "refactor_queue": [],
        "human_review": [],
        "skipped": [],
    }

    for finding in findings:
        if finding.confidence < confidence_threshold:
            routed["skipped"].append(finding)
            continue

        rc = classify_finding(finding, detected_frameworks)
        if rc == RefactorClass.SIMPLE_FIX and finding.safe_to_autofix:
            routed["autofix_safe"].append(finding)
            continue

        if rc == RefactorClass.REFACTOR_CLASS:
            refactor_work = RefactorWork(finding_id=finding.finding_id)
            finding.refactor_phase = refactor_work.phase.value
            queued_work_id: Optional[str] = None
            route_reason = "planning"

            if worktree_path is not None:
                allowed, reason = can_auto_refactor(finding, worktree_path)
                if not allowed:
                    refactor_work.mark_aborted(reason)
                    finding.refactor_phase = refactor_work.phase.value
                    route_reason = reason
                    entry = enqueue_refactor_work(finding, refactor_work)
                    queued_work_id = entry.work_id

            if findings_file is not None:
                save_refactor_work(finding.finding_id, findings_file, refactor_work)

            if log_file is not None:
                _append_text(
                    log_file,
                    "route-findings: "
                    f"finding_id={finding.finding_id} rule={finding.rule} "
                    f"class={rc.value} phase={refactor_work.phase.value} "
                    f"queued_work_id={queued_work_id or ''} reason={route_reason}",
                )

            routed["refactor_queue"].append(
                {
                    "finding": finding,
                    "refactor_work": refactor_work,
                    "queued_work_id": queued_work_id,
                    "reason": route_reason,
                }
            )
            continue

        routed["human_review"].append(finding)

    return routed
