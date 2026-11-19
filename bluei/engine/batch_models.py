"""Batch lane result/metadata models (Stage 3 acceptance).

process_batch returns a BatchResult — a 2-tuple ``(success, detail)`` so
every existing ``ok, detail = process_batch(...)`` call site keeps working —
carrying structured BatchMetrics on ``.metrics`` for the cycle's counter
reconciliation.

Counter semantics are ACTUAL-CALL based, not success-derived:
``deterministic_fixes``/``model_fixes`` count real backend invocations
(including calls that failed), ``attempts`` counts members actually
dispatched (members after an aborting exception are never counted),
``verified_fixes`` counts members accepted by the aggregate gate with a
retained artifact, ``prs_created`` counts real PRs only. Failed members are
derived by the caller as max(0, attempts - verified_fixes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class BatchMetrics:
    """Reconciliation metrics for one process_batch outcome."""

    attempts: int = 0
    deterministic_fixes: int = 0
    model_fixes: int = 0
    verified_fixes: int = 0
    prs_created: int = 0
    issue_patches: Dict[str, str] = field(default_factory=dict)
    mode: str = "failed"

    def merge(self, other: "BatchMetrics") -> "BatchMetrics":
        """Fold a split child's outcome into this one."""
        self.attempts += other.attempts
        self.deterministic_fixes += other.deterministic_fixes
        self.model_fixes += other.model_fixes
        self.verified_fixes += other.verified_fixes
        self.prs_created += other.prs_created
        self.issue_patches.update(other.issue_patches)
        return self


class BatchResult(tuple):
    """(success, detail) tuple with structured .metrics attached."""

    def __new__(cls, success: bool, detail: Optional[str], metrics: Optional[BatchMetrics] = None):
        obj = super().__new__(cls, (success, detail))
        obj.metrics = metrics if metrics is not None else BatchMetrics()
        return obj

    @property
    def success(self) -> bool:
        return self[0]

    @property
    def detail(self) -> Optional[str]:
        return self[1]
