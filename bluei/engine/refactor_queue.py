"""Durable on-disk queue for REFACTOR_CLASS findings.

Two intake lanes, one durable lifecycle:

- ``pending_review`` — safety-gated structural work awaiting explicit human
  approval (the intentional human-review queue; never implicitly promoted).
- ``ready`` — safety-allowed structural work that is execution-eligible
  without approval (distinct lane, not an approval).

Queue directories: finding_work/ → pending_review/ | ready/ → approved/
→ executing/ → completed/ or aborted/. Each entry is a JSON RefactorWork
record plus the originating Finding.
"""

from __future__ import annotations

import logging
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_logger = logging.getLogger(__name__)

from bluei.engine.constants import DEFAULT_REFACTOR_QUEUE_DIR
from bluei.engine.models import Finding, now_iso
from bluei.engine.reforge import RefactorPhase, RefactorWork

#: Trust boundary for work ids: only the canonical ``rw-<hex>`` shape is
#: accepted before any path construction. ``../`` and separators never match.
_WORK_ID_RE = re.compile(r"^rw-[A-Za-z0-9_-]{1,64}$")


def _valid_work_id(work_id: Any) -> bool:
    return isinstance(work_id, str) and bool(_WORK_ID_RE.match(work_id))


class QueueStatus(str, Enum):
    """Status labels for queue entries."""

    PENDING_REVIEW = "pending_review"  # Awaiting human approval
    READY = "ready"  # Safety-allowed; execution-eligible WITHOUT approval
    APPROVED = "approved"  # Approved, ready to execute
    EXECUTING = "executing"  # Currently being processed
    COMPLETED = "completed"  # Successfully completed
    ABORTED = "aborted"  # Failed or rejected


#: Statuses in which an entry is a live duplicate target for dedupe.
LIVE_STATUSES = (
    QueueStatus.PENDING_REVIEW,
    QueueStatus.READY,
    QueueStatus.APPROVED,
    QueueStatus.EXECUTING,
)

#: Directory category names, in lifecycle order.
QUEUE_CATEGORIES = (
    "pending_review",
    "ready",
    "approved",
    "executing",
    "completed",
    "aborted",
)


def finding_fingerprint(
    repo: str,
    rule: str,
    path: str,
    finding_id: str,
    line: int,
    snippet: str,
) -> str:
    """Stable FULL-EVIDENCE identity hash for dedupe and approval drift checks.

    The old coarse ``repo|rule|path`` identity is insufficient: two findings
    at the same location with different evidence (moved line, different
    snippet, different finding_id) must never merge into one queue entry,
    and an approval given for one piece of evidence must not silently
    authorize a changed finding. ``finding_id`` itself is the stable hash of
    the full evidence (repo|path|line|rule|snippet); line/snippet are
    included explicitly so identity also holds for non-canonical producers.
    """
    import hashlib

    source_path = Path(path)
    if Path(repo).is_absolute() and not source_path.is_absolute():
        source_path = Path(repo) / source_path
    material = (
        f"{repo}|{rule}|{source_path}|{finding_id}|{line}|{(snippet or '').strip()}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _evidence_of(finding: Any) -> tuple:
    """Full-evidence fingerprint inputs for a Finding-like record."""
    return (
        finding.repo,
        finding.rule,
        finding.path,
        finding.finding_id,
        int(getattr(finding, "line", 0) or 0),
        str(getattr(finding, "snippet", "") or ""),
    )


@dataclass
class QueueEntry:
    """One entry in the refactor queue.

    Attributes:
        work_id: Unique identifier for this work item.
        status: Current queue status.
        finding_id: ID of the originating Finding.
        rule: Rule name (e.g. "xo-max-lines").
        file_path: Path to the file being refactored.
        repo: Repository identifier.
        refactor_work: The associated RefactorWork record.
        finding_dict: Full Finding dict at time of queueing.
        queued_at: ISO timestamp when entered the queue.
        updated_at: ISO timestamp of last status change.
        approved_at: ISO timestamp of approval (if approved).
        approved_by: Who/what approved it (e.g. "human", "auto_approved").
        executed_at: ISO timestamp when execution started.
        completed_at: ISO timestamp when execution finished.
        error_message: Error detail if aborted.
        attempts: Number of dispatch attempts (history; distinct from accepted).
        retained_patch: Artifact path of the accepted patch once completed.
        finding_fingerprint: sha256 FULL-EVIDENCE identity
            (repo|rule|path|finding_id|line|snippet) for dedupe and approval
            drift checks. Coarse repo|rule|path identities are never enough
            to merge findings or carry an approval across evidence changes.
    """

    work_id: str
    status: QueueStatus
    finding_id: str
    rule: str
    file_path: str
    repo: str
    refactor_work: RefactorWork
    finding_dict: Dict[str, Any]
    queued_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None
    executed_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    attempts: int = 0
    retained_patch: Optional[str] = None
    finding_fingerprint: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "work_id": self.work_id,
            "status": self.status.value,
            "finding_id": self.finding_id,
            "rule": self.rule,
            "file_path": self.file_path,
            "repo": self.repo,
            "refactor_work": {
                "finding_id": self.refactor_work.finding_id,
                "phase": self.refactor_work.phase.value,
                "planned_targets": self.refactor_work.planned_targets,
                "original_line_count": self.refactor_work.original_line_count,
                "target_lines_per_file": self.refactor_work.target_lines_per_file,
                "written_files": list(self.refactor_work.written_files),
                "baseline_fingerprint": self.refactor_work.baseline_fingerprint,
                "needs_human_review": self.refactor_work.needs_human_review,
                "review_outcome": self.refactor_work.review_outcome,
            },
            "finding_dict": self.finding_dict,
            "queued_at": self.queued_at,
            "updated_at": self.updated_at,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "executed_at": self.executed_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "attempts": self.attempts,
            "retained_patch": self.retained_patch,
            "finding_fingerprint": self.finding_fingerprint,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> QueueEntry:
        rw_dict = d.get("refactor_work", {})
        rw = RefactorWork(
            finding_id=rw_dict.get("finding_id", ""),
            phase=RefactorPhase(rw_dict.get("phase", "planning")),
            planned_targets=rw_dict.get("planned_targets", []),
            original_line_count=rw_dict.get("original_line_count", 0),
            written_files=set(rw_dict.get("written_files", [])),
            baseline_fingerprint=rw_dict.get("baseline_fingerprint", ""),
            needs_human_review=rw_dict.get("needs_human_review", False),
            review_outcome=rw_dict.get("review_outcome"),
        )
        return cls(
            work_id=d["work_id"],
            status=QueueStatus(d["status"]),
            finding_id=d["finding_id"],
            rule=d["rule"],
            file_path=d["file_path"],
            repo=d["repo"],
            refactor_work=rw,
            finding_dict=d.get("finding_dict", {}),
            queued_at=d.get("queued_at", ""),
            updated_at=d.get("updated_at", ""),
            approved_at=d.get("approved_at"),
            approved_by=d.get("approved_by"),
            executed_at=d.get("executed_at"),
            completed_at=d.get("completed_at"),
            error_message=d.get("error_message"),
            attempts=int(d.get("attempts", 0) or 0),
            retained_patch=d.get("retained_patch"),
            finding_fingerprint=d.get("finding_fingerprint", ""),
        )


# ----------------------------------------------------------------------
# Queue directory structure helpers
# ----------------------------------------------------------------------


def _queue_dir(base: Path, category: str) -> Path:
    d = base / category
    d.mkdir(parents=True, exist_ok=True)
    return d


def _work_file(base: Path, work_id: str) -> Path:
    return base / "finding_work" / f"{work_id}.json"


def _symlink_path(base: Path, category: str, work_id: str) -> Path:
    return _queue_dir(base, category) / f"{work_id}.link"


# ----------------------------------------------------------------------
# RefactorQueue — main queue class
# ----------------------------------------------------------------------


class RefactorQueue:
    """On-disk refactor queue with human-review gating.

    Queue entries are stored as JSON files under ``queue_dir/finding_work/``.
    Symlinks in ``pending_review/``, ``approved/``, ``executing/``,
    ``completed/``, and ``aborted/`` provide fast category listing.

    Args:
        queue_dir: Root directory for queue files. Per-source runs pass
                   ``state_dir / "refactor_queue"`` so each repository has
                   its own durable queue; ``DEFAULT_REFACTOR_QUEUE_DIR``
                   remains the fallback for direct legacy callers.
    """

    def __init__(self, queue_dir: Optional[Path] = None) -> None:
        if queue_dir is None:
            queue_dir = DEFAULT_REFACTOR_QUEUE_DIR
        self.queue_dir = Path(queue_dir)
        self.work_dir = self.queue_dir / "finding_work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        for cat in QUEUE_CATEGORIES:
            _queue_dir(self.queue_dir, cat)

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def enqueue(
        self,
        finding: Finding,
        refactor_work: RefactorWork,
        status: QueueStatus = QueueStatus.PENDING_REVIEW,
    ) -> QueueEntry:
        """Create a new queue entry for a REFACTOR_CLASS finding.

        Returns the newly created QueueEntry, or the existing live entry
        when an entry with the same FULL-EVIDENCE identity (same finding_id
        plus repo/rule/path/line/snippet) is already live (no duplicates).
        A finding whose evidence changed (e.g. moved line, edited TODO text)
        is a different item: it enqueues as a fresh entry rather than
        silently inheriting the old entry's approval or lifecycle state.
        """
        existing = self.find_live_duplicate(finding)
        if existing is not None:
            return existing
        work_id = f"rw-{uuid.uuid4().hex[:12]}"
        entry = QueueEntry(
            work_id=work_id,
            status=status,
            finding_id=finding.finding_id,
            rule=finding.rule,
            file_path=finding.path,
            repo=finding.repo,
            refactor_work=refactor_work,
            finding_dict=finding.as_dict(),
            finding_fingerprint=finding_fingerprint(*_evidence_of(finding)),
        )
        self._write_entry(entry)
        self._relink(entry)
        return entry

    def get(self, work_id: str) -> Optional[QueueEntry]:
        """Load a queue entry by work_id, or None if not found.

        Trust boundary: the work_id is rejected BEFORE any path
        construction unless it matches the canonical ``rw-...`` shape, so a
        caller-supplied id (e.g. ``--approve-refactor-item``) can never
        traverse outside the queue directory (``../``, separators, escapes
        all return None here and every transition returns False).
        """
        if not _valid_work_id(work_id):
            return None
        path = _work_file(self.queue_dir, work_id)
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return QueueEntry.from_dict(d)
        except Exception:
            return None

    def find_live_duplicate(self, finding: Finding) -> Optional[QueueEntry]:
        """Return the live entry with the same identity, if any.

        Live = pending_review, ready, approved or executing. Terminal
        entries (completed/aborted) never block a fresh enqueue, so an
        interrupted or failed target can be retried with fresh evidence
        without regenerating a completed entry's record.

        Identity is the FULL finding evidence, never coarse
        repo|rule|path. Legacy entries persisted without a fingerprint fall
        back to exact finding_id equality (itself a stable hash of the full
        evidence) — not to a coarse location match, which would merge
        different findings and carry approvals across drift.
        """
        fp = finding_fingerprint(*_evidence_of(finding))
        for status in LIVE_STATUSES:
            for entry in self.list_items(status=status.value):
                if entry.finding_fingerprint:
                    if entry.finding_fingerprint == fp:
                        return entry
                elif entry.finding_id == finding.finding_id:
                    return entry
        return None

    def list_items(
        self,
        status: Optional[str] = None,
        worktree_path: Optional[str] = None,
    ) -> List[QueueEntry]:
        """List queue entries, optionally filtered by status.

        Args:
            status: QueueStatus value to filter by (e.g. "pending_review").
            worktree_path: If set, only return entries where file_path
                           is under this worktree path.
        """
        if status:
            cat_dir = _queue_dir(self.queue_dir, status)
            work_ids = [p.stem for p in cat_dir.iterdir() if p.suffix == ".link"]
        else:
            # Scan all categories
            work_ids: Set[str] = set()
            for cat in QUEUE_CATEGORIES:
                cat_dir = _queue_dir(self.queue_dir, cat)
                for p in cat_dir.iterdir():
                    if p.suffix == ".link":
                        work_ids.add(p.stem)

        entries: List[QueueEntry] = []
        for wid in work_ids:
            entry = self.get(wid)
            if entry is None:
                continue
            if worktree_path and not entry.file_path.startswith(worktree_path):
                continue
            entries.append(entry)
        return entries

    def approve(self, work_id: str, approved_by: str = "human") -> bool:
        """Approve a pending_review entry for execution.

        Transitions: pending_review → approved
        Returns True if the entry was found and transitioned.
        """
        entry = self.get(work_id)
        if entry is None or entry.status != QueueStatus.PENDING_REVIEW:
            return False
        entry.status = QueueStatus.APPROVED
        entry.approved_at = now_iso()
        entry.approved_by = approved_by
        entry.updated_at = now_iso()
        self._write_entry(entry)
        self._relink(entry)
        return True

    def start_execution(self, work_id: str) -> bool:
        """Mark an execution-eligible entry as now executing.

        Eligible intake: APPROVED (explicit human approval) or READY
        (safety-allowed lane — eligibility, not approval). The executing
        state is persisted BEFORE dispatch so an interrupted run leaves a
        truthful durable record.
        Transitions: approved|ready → executing
        """
        entry = self.get(work_id)
        if entry is None or entry.status not in (
            QueueStatus.APPROVED,
            QueueStatus.READY,
        ):
            return False
        entry.status = QueueStatus.EXECUTING
        entry.attempts += 1
        entry.executed_at = now_iso()
        entry.updated_at = now_iso()
        self._write_entry(entry)
        self._relink(entry)
        return True

    def resume_execution(self, work_id: str) -> bool:
        """Durable exact-item continuation after an interrupted run.

        The entry was already execution-eligible when it started
        (APPROVED or READY); a crash left it EXECUTING. The consumer calls
        this ONLY after fresh discovery produced the exact same finding
        identity, so the SAME work item keeps its id and any approval —
        no new id is minted and approval is never lost on retry. Attempts
        are incremented per actual dispatch; the attempt limit itself is
        enforced by the executor on the issue's recorded attempts.
        Transitions: executing → executing (attempt recorded).
        """
        entry = self.get(work_id)
        if entry is None or entry.status != QueueStatus.EXECUTING:
            return False
        entry.attempts += 1
        entry.executed_at = now_iso()
        entry.updated_at = now_iso()
        self._write_entry(entry)
        self._relink(entry)
        return True

    def complete(self, work_id: str, artifact_path: Optional[str] = None) -> bool:
        """Mark an executing entry as successfully completed.

        Transitions: executing → completed. ``artifact_path`` records the
        retained patch artifact so completion is truthful and durable.
        """
        entry = self.get(work_id)
        if entry is None or entry.status != QueueStatus.EXECUTING:
            return False
        entry.status = QueueStatus.COMPLETED
        entry.completed_at = now_iso()
        entry.updated_at = now_iso()
        if artifact_path:
            entry.retained_patch = artifact_path
        self._write_entry(entry)
        self._relink(entry)
        return True

    def fail(self, work_id: str, error_message: str) -> bool:
        """Mark an executing entry as aborted with an error.

        Transitions: executing → aborted
        """
        entry = self.get(work_id)
        if entry is None or entry.status != QueueStatus.EXECUTING:
            return False
        entry.status = QueueStatus.ABORTED
        entry.error_message = error_message
        entry.completed_at = now_iso()
        entry.updated_at = now_iso()
        self._write_entry(entry)
        self._relink(entry)
        return True

    def abort_pending(self, work_id: str, reason: str) -> bool:
        """Abort a pending_review entry without execution.

        Transitions: pending_review → aborted
        """
        entry = self.get(work_id)
        if entry is None or entry.status != QueueStatus.PENDING_REVIEW:
            return False
        entry.status = QueueStatus.ABORTED
        entry.error_message = reason
        entry.completed_at = now_iso()
        entry.updated_at = now_iso()
        self._write_entry(entry)
        self._relink(entry)
        return True

    def count_by_status(self) -> Dict[str, int]:
        """Return a dict mapping status label → count of entries."""
        counts: Dict[str, int] = {}
        for cat in QUEUE_CATEGORIES:
            cat_dir = _queue_dir(self.queue_dir, cat)
            counts[cat] = sum(1 for p in cat_dir.iterdir() if p.suffix == ".link")
        return counts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_entry(self, entry: QueueEntry) -> None:
        path = _work_file(self.queue_dir, entry.work_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entry.as_dict(), sort_keys=True) + "\n", encoding="utf-8"
        )

    def _relink(self, entry: QueueEntry) -> None:
        """Place/remove symlinks to reflect current status."""
        for cat in QUEUE_CATEGORIES:
            link_path = _symlink_path(self.queue_dir, cat, entry.work_id)
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
        # Place symlink in the current status category
        link_path = _symlink_path(self.queue_dir, entry.status.value, entry.work_id)
        target = _work_file(self.queue_dir, entry.work_id).resolve()
        try:
            link_path.symlink_to(target)
        except OSError:
            # May fail if already exists (handle gracefully)
            _logger.debug("Failed to create queue symlink")


# ----------------------------------------------------------------------
# Convenience helpers
# ----------------------------------------------------------------------


def enqueue_refactor_work(
    finding: Finding,
    refactor_work: RefactorWork,
    worktree_path: Optional[Path] = None,
    status: QueueStatus = QueueStatus.PENDING_REVIEW,
    queue_dir: Optional[Path] = None,
) -> QueueEntry:
    """Enqueue a REFACTOR_CLASS finding to the durable queue.

    ``queue_dir`` selects the per-source durable queue (discover passes
    ``state_dir / 'refactor_queue'``). ``status`` selects the intake lane:
    PENDING_REVIEW (safety-gated, awaits explicit human approval) or READY
    (safety-allowed execution lane — eligibility, not approval).

    Args:
        finding: The Finding being queued.
        refactor_work: The associated RefactorWork state record.
        worktree_path: Root path of the worktree. If provided, the entry's
            file_path is stored as worktree_path / finding.path so that
            list_items(worktree_path=...) filtering works correctly.
        status: Intake lane (PENDING_REVIEW or READY).
        queue_dir: Per-source queue directory. Defaults to the legacy
            module default when not provided.
    """
    queue = RefactorQueue(queue_dir)
    # Store worktree-rooted path so list_items filtering by worktree_path works
    if worktree_path is not None:
        # Clone finding with worktree-rooted path for storage
        finding_copy = Finding.from_dict(finding.as_dict())
        finding_copy.path = str(Path(worktree_path) / finding.path)
        return queue.enqueue(finding_copy, refactor_work, status)
    return queue.enqueue(finding, refactor_work, status)


def list_pending_review(worktree_path: Optional[str] = None) -> List[QueueEntry]:
    """Return all entries awaiting human review."""
    queue = RefactorQueue()
    return queue.list_items(
        status=QueueStatus.PENDING_REVIEW.value, worktree_path=worktree_path
    )


def list_refactor_work(
    status: Optional[str] = None,
    queue_dir: Optional[Path] = None,
) -> List[QueueEntry]:
    """List durable queue entries for the public CLI (``refactor list``)."""
    return RefactorQueue(queue_dir).list_items(status=status)


def approve_refactor_item(
    work_id: str,
    approved_by: str = "human",
    queue_dir: Optional[Path] = None,
) -> Optional[QueueEntry]:
    """Explicit public approval action (``--approve-refactor-item``).

    PENDING_REVIEW → APPROVED only. Returns the refreshed entry, or None
    when the work_id is unknown or not awaiting review (e.g. READY is
    safety-eligibility, not review, and is never "approved"). This is the
    only approval producer; there is no automatic path.
    """
    queue = RefactorQueue(queue_dir)
    if not queue.approve(work_id, approved_by=approved_by):
        return None
    return queue.get(work_id)


def create_refactor_plan(finding: Finding) -> Dict[str, Any]:
    """Create a structured refactor plan dict for a finding.

    This is a simple first-cut planner used when a queue entry needs
    a plan but one wasn't pre-computed. Returns the plan dict.
    The plan describes the file split strategy (for xo-max-lines)
    or general approach (for other rules).
    """
    rule = getattr(finding, "rule", "") or ""

    if rule == "xo-max-lines":
        from bluei.engine.constants import MAX_LINES_REFACTOR_TARGET

        path = getattr(finding, "path", "") or ""
        line_count = getattr(finding, "line", 0) or 0

        # Rough estimation: count lines in file at path
        estimated_lines = line_count
        if path:
            full_path = Path(path)
            if full_path.is_file():
                try:
                    estimated_lines = len(
                        full_path.read_text(encoding="utf-8").splitlines()
                    )
                except Exception:
                    _logger.debug("Failed to estimate file line count")

        estimated_parts = max(
            1,
            (estimated_lines + MAX_LINES_REFACTOR_TARGET - 1)
            // MAX_LINES_REFACTOR_TARGET,
        )
        parts = [f"part{i + 1}{Path(path).suffix}" for i in range(estimated_parts)]

        return {
            "strategy": "split_by_line_count",
            "target_lines_per_file": MAX_LINES_REFACTOR_TARGET,
            "estimated_parts": estimated_parts,
            "planned_files": parts,
            "description": f"Split {path} into ~{estimated_parts} files of ~{MAX_LINES_REFACTOR_TARGET} lines each",
        }

    return {
        "strategy": "general_refactor",
        "rule": rule,
        "description": f"Refactor {rule} for file {getattr(finding, 'path', '')}",
    }
