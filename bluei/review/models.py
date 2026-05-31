"""review.models — Review-domain models previously in app/models.py.

Moved here 2026-05-31 to decouple review/ from app/ layer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from bluei.engine.models import Finding


class CompressionMode(str, Enum):
    """
    Compression/chunking mode used for an autonomous review run.

    full_diff    - Entire diff/file set is processed in a single pass
                    (no chunking; all content fed to LLM together).
    compressed   - Content is compressed or summarized before being passed
                    to the LLM (e.g. via a separate summarization step).
    multi_pass   - Content is processed across multiple targeted passes,
                    each focusing on a specific subset or aspect.
    """

    FULL_DIFF = "full_diff"
    COMPRESSED = "compressed"
    MULTI_PASS = "multi_pass"


@dataclass
class MonitoredSafetyState:
    """
    Phase G7: Monitored-rollout safety state for circuit-breaker behavior.

    Tracks consecutive failures in guarded-live publication attempts to
    implement a circuit-breaker / open-cooldown pattern that prevents
    repeated live publication attempts after failures.

    Attributes:
        circuit_open: True when the circuit breaker has opened due to
                      repeated failures. Live publication is blocked while
                      circuit is open.
        failure_count: Number of consecutive guarded-live publish failures
                       since the last successful live publication.
        cooldown_until: ISO timestamp when the cooldown expires and the
                        circuit may close again. None if circuit is closed.
        last_failure_at: ISO timestamp of the most recent failure.
        last_failure_reason: Human-readable reason for the most recent failure.
        auto_rollback_active: True when monitored feedback has tripped a
                        fail-closed rollback for guarded live publication.
        auto_rollback_reason: Human-readable rollback trigger summary.
        auto_rollback_triggered_at: ISO timestamp when rollback was activated.
    """

    circuit_open: bool = False
    failure_count: int = 0
    cooldown_until: Optional[str] = None  # ISO timestamp
    last_failure_at: Optional[str] = None
    last_failure_reason: str = ""
    auto_rollback_active: bool = False
    auto_rollback_reason: str = ""
    auto_rollback_triggered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MonitoredSafetyState":
        return cls(
            circuit_open=data.get("circuit_open", False),
            failure_count=data.get("failure_count", 0),
            cooldown_until=data.get("cooldown_until"),
            last_failure_at=data.get("last_failure_at"),
            last_failure_reason=data.get("last_failure_reason", ""),
            auto_rollback_active=data.get("auto_rollback_active", False),
            auto_rollback_reason=data.get("auto_rollback_reason", ""),
            auto_rollback_triggered_at=data.get("auto_rollback_triggered_at"),
        )

    def record_failure(self, reason: str, cooldown_seconds: int) -> None:
        """
        Record a publish failure and open the circuit if threshold exceeded.

        Args:
            reason: Human-readable failure reason.
            cooldown_seconds: How long the cooldown lasts in seconds.
        """

        self.failure_count += 1
        self.last_failure_at = datetime.now(timezone.utc).isoformat()
        self.last_failure_reason = reason
        # Circuit opens when failure_count reaches the threshold (set by caller)
        # The caller checks threshold and sets circuit_open = True

    def record_success(self) -> None:
        """Reset failure tracking after a successful live publication."""
        self.circuit_open = False
        self.failure_count = 0
        self.cooldown_until = None
        self.last_failure_at = None
        self.last_failure_reason = ""

    def check_cooldown_ready(self) -> bool:
        """
        Check if cooldown period has elapsed and circuit can close.

        Returns:
            True if cooldown has expired (or was never set), False if still in cooldown.
        """
        if not self.circuit_open or not self.cooldown_until:
            return True

        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(self.cooldown_until)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now >= expires


class FindingSource(str, Enum):
    """Where a review finding originated."""

    LINTER = "linter"
    LLM = "llm"
    BASELINE = "baseline"
    REVIEW_FEEDBACK = "review-feedback"
    MANUAL = "manual"


class FindingActionability(str, Enum):
    """How directly actionable a finding is."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"


class FindingSeverity(str, Enum):
    """Normalized severity for review findings."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class PublishStatus(str, Enum):
    """
    Publication status for a review finding or run.

    absent     - Finding was present in prior state but is absent from the
                 current candidate set.  For published findings this means
                 the issue appears resolved.
    pending    - New finding not yet published.
    published  - Successfully published to GitHub.
    failed     - Publishing was attempted but failed; error is captured.
    skipped    - Intentionally skipped (e.g. below confidence threshold).
    superseded - A finding with the same fingerprint was already published
                 in an earlier run; the current occurrence is a re-run.
    """

    ABSENT = "absent"
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"


class LearnedRuleStatus(str, Enum):
    """
    Lifecycle status for a learned rule.

    tentative   - Newly proposed; not yet active.  Must pass safety gates
                  and accumulate sufficient evidence before activating.
    active      - Activated and applying to findings.  Can be suppressed
                  by operator-authored rules or conflict resolution.
    rejected    - Rejected at proposal time due to safety gates or conflicts.
                  Never activates.
    superseded  - Was active but was later overridden by an operator-authored
                  rule or a newer learned rule with higher precedence.
    """

    TENTATIVE = "tentative"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


def normalize_finding_path(path: str) -> str:
    """
    Normalize a file path for consistent fingerprinting.

    - Strips leading/trailing whitespace
    - Converts backslashes to forward slashes
    - Collapses multiple slashes to single slash
    - Removes trailing slash

    Does NOT strip a repo prefix — that would require knowing the repo
    name and could accidentally strip legitimate path segments (e.g.
    ``src/`` from ``src/main.ts``). Callers should pre-strip the repo
    prefix before calling this function if needed.
    """
    if not path:
        return ""
    path = path.strip().replace("\\", "/")
    # Collapse multiple slashes
    while "//" in path:
        path = path.replace("//", "/")
    # Remove trailing slash
    path = path.rstrip("/")
    return path


def normalize_finding_header(header: str) -> str:
    """
    Normalize a header/rule identifier for fingerprinting.

    - Lowercases
    - Strips whitespace
    - Collapses internal whitespace to single space
    - Strips leading/trailing punctuation
    """
    if not header:
        return ""
    normalized = header.strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip(".-_/: ")
    return normalized


def make_finding_fingerprint(
    repo: str,
    path: str,
    line: int,
    header: str,
    snippet: str,
) -> str:
    """
    Generate a deterministic SHA-256 fingerprint for a finding.

    This fingerprint identifies the *logical* finding regardless of
    generated finding_id. It is stable across re-runs for the same
    code location + rule combination.

    Args:
        repo: repository identifier
        path: file path (will be normalized)
        line: line number
        header: rule/header identifier (will be normalized)
        snippet: code snippet (will be trimmed before hashing)

    Returns:
        64-character hex SHA-256 fingerprint
    """
    norm_path = normalize_finding_path(path)
    norm_header = normalize_finding_header(header)
    # Trim snippet to first 200 chars for stability
    snippet = (snippet or "")[:200].strip()
    payload = "|".join(str(x) for x in [repo, norm_path, line, norm_header, snippet])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_review_finding_id(fingerprint: str, attempt: int = 0) -> str:
    """
    Generate a QA-agent-owned finding_id from a fingerprint + attempt.

    The finding_id is deterministic for a given fingerprint + attempt
    combination, making it stable across review runs for the same finding.

    Format: ``rf-{fingerprint[:12]}-{attempt:03d}``

    Args:
        fingerprint: 64-char SHA-256 hex from make_finding_fingerprint
        attempt: non-negative integer (0 = first occurrence)

    Returns:
        Stable finding_id string
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    short_fp = fingerprint[:12]
    return f"rf-{short_fp}-{attempt:03d}"


@dataclass
class ReviewFinding:
    """
    A finding produced during an autonomous review run.

    This is distinct from the older ``Finding`` dataclass which is
    linter-output oriented.  ``ReviewFinding`` captures a finding that
    may come from an LLM review, a linter, a baseline diff, or
    human review feedback.

    Identity is owned by the QA-agent: the ``finding_id`` is
    deterministically generated from a content fingerprint and is
    stable across re-runs for the same logical finding.
    """

    # Identity (QA-owned, deterministic)
    finding_id: str
    finding_fingerprint: str  # SHA-256 hex from make_finding_fingerprint

    # Core location
    repo: str
    path: str
    line: int
    header: str  # e.g. rule name, LLM-generated label

    # Classification
    source: FindingSource
    actionability: FindingActionability
    severity: FindingSeverity

    # Loop-ready lineage (inert until loop logic is wired)
    run_id: Optional[str] = None
    parent_finding_id: Optional[str] = None

    # Defaults
    snippet: str = ""
    confidence: float = 0.5
    discovered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["source"] = self.source.value
        out["actionability"] = self.actionability.value
        out["severity"] = self.severity.value
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewFinding":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if isinstance(filtered.get("source"), str):
            filtered["source"] = FindingSource(filtered["source"])
        if isinstance(filtered.get("actionability"), str):
            filtered["actionability"] = FindingActionability(filtered["actionability"])
        if isinstance(filtered.get("severity"), str):
            filtered["severity"] = FindingSeverity(filtered["severity"])
        filtered.setdefault("run_id", None)
        filtered.setdefault("parent_finding_id", None)
        filtered.setdefault("snippet", "")
        filtered.setdefault("confidence", 0.5)
        filtered.setdefault("discovered_at", None)
        return cls(**filtered)


@dataclass
class ReviewSummary:
    """
    A summary of a completed (or aborted) review run.

    Provides a stable artifact for downstream consumers that want
    aggregate data without loading every individual ReviewFinding.
    """

    id: str  # QA-owned stable id
    run_id: str
    repo: str

    # Counts
    finding_count: int = 0
    actionable_count: int = 0
    critical_count: int = 0

    # Delta from baseline (inert until baseline tracking is wired)
    baseline_summary_id: Optional[str] = None
    delta_findings: int = 0
    delta_actionable: int = 0

    # Defaults
    generated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewSummary":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        filtered.setdefault("finding_count", 0)
        filtered.setdefault("actionable_count", 0)
        filtered.setdefault("critical_count", 0)
        filtered.setdefault("baseline_summary_id", None)
        filtered.setdefault("delta_findings", 0)
        filtered.setdefault("delta_actionable", 0)
        filtered.setdefault("generated_at", None)
        return cls(**filtered)


@dataclass
class LearnedRule:
    """
    A learned rule derived from repeated feedback/findings during autonomous review.

    Learned rules are a conservative mechanism: they can suppress low-risk
    style/format/import-order patterns when the same finding occurs
    repeatedly across runs, but they NEVER override operator-authored rules
    and NEVER activate based on reaction-only signals.

    Lifecycle:
      tentative → active  (when evidence threshold met AND safety gates pass)
      tentative → rejected (when safety gates or conflicts fail)
      active → superseded (when operator-authored rule takes precedence)

    Attributes:
        rule_id:         QA-owned stable identifier (e.g. ``lr-{fp[:12]}-{n:03d}``).
        header:          Normalized rule/finding header this rule suppresses.
        pattern:         Normalized path pattern or glob this rule applies to.
        status:          Current LearnedRuleStatus.
        risk_level:      ``low`` for style/format/import-order only.
        precedence:      Lower number = higher priority.  Operator rules are
                         always precedence 0; learned rules start at 10.
        evidence_count:  Number of times this pattern was observed.
        proposal_run_id: Run ID that proposed this rule.
        activated_at:    ISO timestamp when status changed to ACTIVE (if ever).
        superseded_by:   rule_id of the rule that superseded this one (if any).
        created_at:      ISO timestamp of rule creation.
        updated_at:      ISO timestamp of last update.
        source_finding_ids: List of finding_ids that contributed to this rule.
        notes:           Human-readable context (how rule was derived).
    """

    rule_id: str
    header: str
    pattern: str
    status: LearnedRuleStatus

    # Classification
    risk_level: str = "low"  # "low" | "high" — high-risk never auto-activates
    precedence: int = 10  # 0 = operator-authored; 10+ = learned

    # Evidence
    evidence_count: int = 0
    source_finding_ids: List[str] = field(default_factory=list)

    # Lineage
    proposal_run_id: Optional[str] = None
    activated_at: Optional[str] = None
    superseded_by: Optional[str] = None

    # Timestamps
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # Notes
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["status"] = self.status.value
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LearnedRule":
        allowed = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if isinstance(filtered.get("status"), str):
            filtered["status"] = LearnedRuleStatus(filtered["status"])
        filtered.setdefault("risk_level", "low")
        filtered.setdefault("precedence", 10)
        filtered.setdefault("evidence_count", 0)
        filtered.setdefault("source_finding_ids", [])
        filtered.setdefault("proposal_run_id", None)
        filtered.setdefault("activated_at", None)
        filtered.setdefault("superseded_by", None)
        filtered.setdefault("created_at", None)
        filtered.setdefault("updated_at", None)
        filtered.setdefault("notes", "")
        return cls(**filtered)
