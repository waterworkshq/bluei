"""Structural execution consumer: durable queue → qualified executor.

Loads execution-eligible refactor queue entries — READY (safety-allowed
lane; eligibility, never approval) and APPROVED (explicit human approval
via ``--approve-refactor-item``) — and delegates each to the shared
qualified per-issue executor seam in ``pr_cycle.execute_structural_entry``
(single executor: baseline/scope/postchecks/closure and patch retention
live there, not here). This module owns queue lifecycle truthfulness only.

Scope ceiling (Stage 3): bounded structural reduction of EXISTING files
with a tracked patch. New-file splits (e.g. xo-max-lines partN.ts) remain
unqualified — the executor seam fail-closes them with a truthful
run_status; they are never silently dropped or fake-completed.
# ponytail: new-file split support requires a tracked-multiple-file patch
# contract in the seam; add when that lands, not before.

Durability contract:
- ``start_execution`` persists executing + attempts BEFORE dispatch, so an
  interrupted run leaves a truthful record.
- On an explicit next refactor-cycle run with FRESH discovery evidence:
  * an entry whose issue already carries a retained patch (or a created PR)
    is reconciled to COMPLETED without re-invoking the executor and without
    duplicating accepted counters — this covers a kill after retention but
    before queue completion;
  * a stale executing entry with a fresh EXACT-evidence match resumes the
    same durable work item (attempts recorded; approval retained);
  * a stale executing entry with no fresh evidence is aborted as
    ``interrupted: ...`` (distinct from execution failures).
- Completed entries are never regenerated; a recurring finding after
  completion enqueues as a fresh entry.
- Identity for dedupe/freshness/approval drift is the FULL finding evidence
  (finding_fingerprint over repo|rule|path|finding_id|line|snippet), never
  coarse repo|rule|path.

Return contract (parent CLI: ``refactor_rc or finalize_rc``):
0 = truthful completion or no-op (zero-or-more verified items), 1 =
dispatched work but nothing verified, 2 = blocked precondition (visible
--auto-approve rejection, approval refused, producer incomplete, baseline
or integration unqualified, invalid item limit).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from bluei.engine.commands.context import RunContext
from bluei.engine.models import Finding
from bluei.engine.refactor_queue import (
    QueueEntry,
    QueueStatus,
    RefactorQueue,
    approve_refactor_item,
    finding_fingerprint,
)
from bluei.engine.state import _append_text
from bluei.engine.validation import responsible_detector_ran

#: Executor seam location (parent-owned). Resolved lazily so this module
#: stays importable and testable independent of the seam.
_SEAM_MODULE = "bluei.engine.commands.pr_cycle"
_SEAM_NAME = "execute_structural_entry"

Executor = Callable[
    [RunContext, QueueEntry, Finding, Dict[str, Dict[str, Any]], int],
    Tuple[Any, Optional[Dict[str, Any]]],
]


def _resolve_seam(executor: Optional[Executor]) -> Optional[Executor]:
    if executor is not None:
        return executor
    import importlib

    try:
        mod = importlib.import_module(_SEAM_MODULE)
        return getattr(mod, _SEAM_NAME)  # type: ignore[no-any-return]
    except (ImportError, AttributeError):
        return None


def _queue_dir_for(ctx: RunContext) -> Optional[Path]:
    """Per-source durable queue: ``state_dir / 'refactor_queue'``.

    Matches the directory ``run_discover_phase`` passes to
    ``route_findings_with_intent(queue_dir=...)``. Returns None only for
    direct callers without a state file (legacy module default).
    """
    state_file = getattr(ctx, "state_file", None)
    if not state_file or not str(state_file):
        return None
    return Path(state_file).parent / "refactor_queue"


def _detector_outcomes_complete(outcomes: Dict[str, Dict[str, Any]]) -> bool:
    return not any(
        record.get("status") in {"failed", "unavailable"}
        for record in (outcomes or {}).values()
    )


def _fresh_match(
    entry: QueueEntry,
    fresh_findings: List[Finding],
    outcomes: Dict[str, Dict[str, Any]],
    repo_path: Optional[Path] = None,
) -> Optional[Finding]:
    """Exactly one fresh finding matching the entry's FULL evidence identity,
    produced by a detector that actually ran. None otherwise (absent,
    ambiguous, stale, drifted or producer-incomplete).

    Identity basis: discovery normally persists repository-relative paths;
    direct queue clients may supply repo-rooted paths. Fingerprint generation
    normalizes both forms against the same absolute repository identity. ``finding_id`` (itself the stable hash
    of repo|path|line|rule|snippet) plus line/snippet make the match exact —
    never coarse repo|rule|path.
    """
    if not entry.finding_fingerprint:
        return None

    def _basis_path(f: Finding) -> str:
        if repo_path is None or Path(f.path).is_absolute():
            return f.path
        return str(Path(repo_path) / f.path)

    matches = []
    for f in fresh_findings:
        fp = finding_fingerprint(
            f.repo, f.rule, _basis_path(f), f.finding_id, int(f.line or 0), f.snippet
        )
        if fp == entry.finding_fingerprint:
            matches.append(f)
    if len(matches) != 1:
        return None
    if not responsible_detector_ran(matches[0].rule, outcomes):
        return None
    return matches[0]


def _accepted_artifact(issue: Dict[str, Any]) -> Optional[str]:
    """Retained accepted artifact path on an issue record, if any."""
    patch = issue.get("verified_fix_patch") or issue.get("selected_fix_patch")
    if not isinstance(patch, str):
        return None
    try:
        path = Path(patch)
        return patch if path.is_file() and path.stat().st_size > 0 else None
    except (OSError, ValueError):
        return None


def _issue_for_entry(
    ctx: RunContext, entry: QueueEntry
) -> Optional[Dict[str, Any]]:
    matches = [
        issue
        for issue in (ctx.issues_data.get("issues", []) or [])
        if issue.get("finding_id") == entry.finding_id
    ]
    return matches[0] if len(matches) == 1 else None


def run_refactor_cycle_phase(
    ctx: RunContext,
    executor: Optional[Executor] = None,
) -> int:
    """Public structural execution consumer for ``--run-phase=refactor-cycle``.

    Requires ``ctx`` post-discovery (``ctx.findings`` and
    ``ctx.detector_outcomes`` populated by ``run_discover_phase``); this
    phase never rediscovers or executes on stale memory. Per-item outcomes
    are durably recorded either way; the seam owns ctx counter projection
    (CounterDeltas are cumulative snapshots assigned by the seam — the
    consumer never re-adds them) and the normal finalize phase reports them.
    """
    args = ctx.args
    log_file = ctx.log_file
    queue = RefactorQueue(_queue_dir_for(ctx))

    def _blocked(reason: str) -> int:
        print(f"[BLOCKED] refactor-cycle: {reason}")
        _append_text(log_file, f"refactor-cycle: blocked reason={reason}")
        ctx.blocked_reasons.append(reason)
        return 2

    # Explicit human approval only. A bulk auto-approve request is a
    # visible rejection, never an implicit promotion of pending_review.
    if getattr(args, "auto_approve", False):
        return _blocked(
            "--auto-approve rejected: review approval is explicit "
            "(use --approve-refactor-item <work_id>)"
        )

    # Fresh discovery evidence is mandatory for any state change. Producer
    # must have completed for every detector; an empty finding set is
    # complete evidence (nothing to match), not a block by itself.
    fresh_findings: List[Finding] = list(getattr(ctx, "findings", None) or [])
    outcomes: Dict[str, Dict[str, Any]] = getattr(ctx, "detector_outcomes", None) or {}
    if not _detector_outcomes_complete(outcomes):
        return _blocked("producer incomplete: detector failed or unavailable")

    if getattr(args, "dry_run", False):
        counts = queue.count_by_status()
        print(
            "[DONE] refactor-cycle (dry-run) "
            + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        _append_text(log_file, f"refactor-cycle: dry-run counts={counts}")
        return 0

    # Explicit single-item approval. Approval is granted only when the item
    # exists in this per-source queue, is pending_review, and its FULL
    # evidence identity matches exactly one fresh finding from a detector
    # that ran — never before freshness, and never across evidence drift.
    approve_id = getattr(args, "approve_refactor_item", None)
    if approve_id:
        approve_entry = queue.get(approve_id)
        if approve_entry is None or approve_entry.status != QueueStatus.PENDING_REVIEW:
            return _blocked(
                f"approval refused: {approve_id} unknown or not pending_review"
            )
        if _fresh_match(approve_entry, fresh_findings, outcomes, ctx.repo_path) is None:
            return _blocked(
                f"approval refused: {approve_id} has no fresh unique matching "
                "finding (absent, ambiguous or drifted evidence)"
            )
        approved = approve_refactor_item(approve_id, queue_dir=queue.queue_dir)
        if approved is None:
            return _blocked(f"approval refused: {approve_id} transition failed")
        _append_text(
            log_file,
            f"refactor-cycle: approved work_id={approve_id} by={approved.approved_by}",
        )

    # Reconcile accepted artifacts BEFORE any dispatch: a prior run may have
    # been killed after retention saved the issue (verified patch + open
    # status) but before the queue entry completed. The existing issue record
    # is the artifact evidence; completing the queue entry from it needs no
    # re-invocation and adds no accepted counters.
    reconciled: List[str] = []
    candidates: List[QueueEntry] = []
    candidates += queue.list_items(status=QueueStatus.READY.value)
    candidates += queue.list_items(status=QueueStatus.APPROVED.value)
    candidates += queue.list_items(status=QueueStatus.EXECUTING.value)
    for entry in candidates:
        issue = _issue_for_entry(ctx, entry)
        if issue is None:
            continue
        patch = _accepted_artifact(issue)
        pr_number = (issue.get("github") or {}).get("pr_number")
        if patch or pr_number:
            if queue.complete(entry.work_id, patch):
                reconciled.append(entry.work_id)
                _append_text(
                    log_file,
                    f"refactor-cycle: reconciled work_id={entry.work_id} "
                    "from retained issue artifact (no re-execution)",
                )

    # Stale executing entries: exact-evidence continuation preserves the
    # durable item id and any approval; no fresh evidence means a truthful
    # interrupted abort (terminal; retry only via a later explicit run whose
    # fresh evidence re-enqueues the finding).
    continued: List[QueueEntry] = []
    interrupted: List[str] = []
    for stale in queue.list_items(status=QueueStatus.EXECUTING.value):
        if _fresh_match(stale, fresh_findings, outcomes, ctx.repo_path) is not None:
            continued.append(stale)
        else:
            queue.fail(
                stale.work_id,
                "interrupted: no fresh evidence on this explicit refactor-cycle run",
            )
            interrupted.append(stale.work_id)

    eligible: List[QueueEntry] = []
    eligible += queue.list_items(status=QueueStatus.READY.value)
    eligible += queue.list_items(status=QueueStatus.APPROVED.value)
    eligible += continued

    seam = _resolve_seam(executor)
    if eligible and seam is None:
        return _blocked("structural executor integration not present")

    # Plan dispatches against fresh evidence before running any check, so an
    # all-stale queue performs no work at all.
    planned: List[Tuple[QueueEntry, Finding]] = []
    skipped_stale: List[str] = []
    for entry in eligible:
        fresh = _fresh_match(entry, fresh_findings, outcomes, ctx.repo_path)
        if entry.attempts >= args.max_fix_attempts_per_issue:
            skipped_stale.append(entry.work_id)
            ctx.blocked_reasons.append(f"structural attempt ceiling reached: {entry.work_id}")
            _append_text(log_file, f"refactor-cycle: attempt ceiling work_id={entry.work_id} attempts={entry.attempts}")
            continue
        issue = _issue_for_entry(ctx, entry)
        if issue and (issue.get("verified_fix_patch") or issue.get("selected_fix_patch")) and not _accepted_artifact(issue):
            skipped_stale.append(entry.work_id)
            ctx.blocked_reasons.append(f"structural retained artifact unavailable: {entry.work_id}")
            _append_text(log_file, f"refactor-cycle: retained artifact unavailable work_id={entry.work_id}")
            continue
        if fresh is None:
            skipped_stale.append(entry.work_id)
            _append_text(
                log_file,
                f"refactor-cycle: skip stale work_id={entry.work_id} "
                f"status={entry.status.value} "
                "finding absent, ambiguous, drifted or producer-incomplete",
            )
            continue
        planned.append((entry, fresh))

    max_items = getattr(args, "max_queue_items", None)
    if max_items is not None and max_items <= 0:
        return _blocked("max-queue-items must be positive")

    baseline_results: Dict[str, Dict[str, Any]] = {}
    if planned:
        # Configured baseline via the ordinary helper — same contract as the
        # ordinary lane; no second executor, no hardcoded defaults, no
        # additional discovery scan.
        from bluei.engine.validation import run_named_checks

        baseline_results = run_named_checks(
            repo_path=ctx.repo_path,
            checks=ctx.PER_REPO_BASELINE_CHECKS,
            log_file=log_file,
            phase="refactor-cycle-baseline",
            environment_repo=ctx.repo_path,
        )

    dispatched = 0
    verified = 0
    failed: List[str] = []
    for entry, fresh in planned:
        if max_items is not None and dispatched >= max_items:
            break

        # Persist in-progress BEFORE dispatch (durable interrupted state).
        # Continuation resumes the same executing entry (attempt recorded,
        # id and approval retained); fresh entries start from approved/ready.
        started = (
            queue.resume_execution(entry.work_id)
            if entry.status == QueueStatus.EXECUTING
            else queue.start_execution(entry.work_id)
        )
        if not started:
            _append_text(
                log_file,
                f"refactor-cycle: start refused work_id={entry.work_id} "
                f"status={entry.status.value}",
            )
            continue
        dispatched += 1

        issue: Optional[Dict[str, Any]] = None
        try:
            # The seam signature is (ctx, item, finding, baseline_results,
            # idx) with item.work_id: the durable QueueEntry IS the item.
            # CounterDeltas returned are cumulative snapshots already
            # assigned to ctx by the seam — never re-add them here.
            _deltas, issue = seam(ctx, entry, fresh, baseline_results, dispatched - 1)
        except Exception as exc:
            queue.fail(entry.work_id, f"{type(exc).__name__}: {exc}")
            failed.append(entry.work_id)
            _append_text(
                log_file,
                f"refactor-cycle: attempt failed work_id={entry.work_id} "
                f"error={type(exc).__name__}: {exc}",
            )
            continue

        patch = _accepted_artifact(issue or {})
        pr_number = ((issue or {}).get("github") or {}).get("pr_number")
        if issue is not None and (patch or pr_number):
            queue.complete(entry.work_id, patch)
            verified += 1
        else:
            queue.fail(
                entry.work_id,
                str(
                    (issue or {}).get("status_detail")
                    or (issue or {}).get("run_status")
                    or "executor produced no accepted artifact"
                ),
            )
            failed.append(entry.work_id)

    pending = len(queue.list_items(status=QueueStatus.PENDING_REVIEW.value))
    print(
        f"[DONE] refactor-cycle dispatched={dispatched} verified={verified} "
        f"failed={len(failed)} reconciled={len(reconciled)} "
        f"interrupted={len(interrupted)} skipped_stale={len(skipped_stale)} "
        f"pending_review={pending}"
    )
    _append_text(
        log_file,
        f"refactor-cycle: done dispatched={dispatched} verified={verified} "
        f"failed={failed} reconciled={reconciled} interrupted={interrupted} "
        f"skipped_stale={skipped_stale} pending_review={pending}",
    )
    if (dispatched and not verified) or (skipped_stale and not verified and not reconciled):
        return 1
    return 0
