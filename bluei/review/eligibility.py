#!/usr/bin/env python3
"""Remediation eligibility — extracted from review.cycle for single-responsibility."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from bluei.review.models import (
    FindingActionability,
    FindingSeverity,
    FindingSource,
    normalize_finding_header,
    normalize_finding_path,
)
from bluei.app.models import (
    RepoConfig,
)

# Default confidence threshold for remediation eligibility
_DEFAULT_MIN_CONFIDENCE = 0.6

# Minimum actionability for autonomous remediation
_DEFAULT_MIN_ACTIONABILITY = FindingActionability.MEDIUM

# Rank mappings for enum-based ordinal comparisons.
# These give the semantic ordering that string-value enums can't provide
# via direct comparison operators.
_ACTIONABILITY_RANK: Dict[FindingActionability, int] = {
    FindingActionability.INFORMATIONAL: 0,
    FindingActionability.LOW: 1,
    FindingActionability.MEDIUM: 2,
    FindingActionability.HIGH: 3,
}

_SEVERITY_RANK: Dict[FindingSeverity, int] = {
    FindingSeverity.NONE: 0,
    FindingSeverity.LOW: 1,
    FindingSeverity.MEDIUM: 2,
    FindingSeverity.HIGH: 3,
    FindingSeverity.CRITICAL: 4,
}


@dataclass
class RemediationEligibility:
    """
    Result of computing remediation eligibility for a finding.

    Attributes:
        eligible: True if the finding passes all spec gates.
        reason: Human-readable summary of the decision.
        rejected_gates: List of gate names that caused rejection (empty if eligible).
        safe_to_autofix: Whether the finding is marked safe_to_autofix.
        severity_ok: Whether severity is non-critical.
        actionability_ok: Whether actionability meets the minimum threshold.
        confidence_ok: Whether confidence meets the minimum threshold.
    """

    eligible: bool
    reason: str
    rejected_gates: List[str] = field(default_factory=list)
    safe_to_autofix: bool = False
    severity_ok: bool = True
    actionability_ok: bool = True
    confidence_ok: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def is_remediation_eligible(
    finding: Dict[str, Any],
    repo_config: Optional[RepoConfig] = None,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    min_actionability: FindingActionability = _DEFAULT_MIN_ACTIONABILITY,
) -> RemediationEligibility:
    """
    Compute whether a finding is eligible for autonomous remediation.

    Eligibility gates (ALL must pass):
    1. confidence >= min_confidence
    2. actionability >= min_actionability
    3. safe_to_autofix is True
    4. severity is non-critical (not FindingSeverity.CRITICAL)
    5. source is a validated source (not MANUAL unless overridden)

    Args:
        finding: A finding dict (normalized or ReviewFinding-like).
        repo_config: Optional RepoConfig for repo-specific allowlist checks.
        min_confidence: Minimum confidence threshold (default 0.6).
        min_actionability: Minimum actionability level (default MEDIUM).

    Returns:
        RemediationEligibility dataclass with eligible flag and details.
    """
    rejected: List[str] = []
    severity = finding.get("severity")
    actionability = finding.get("actionability")
    confidence = float(finding.get("confidence", 0.0))
    safe_to_autofix = bool(finding.get("safe_to_autofix", False))
    source = finding.get("source")

    # Gate 1: confidence
    confidence_ok = confidence >= min_confidence
    if not confidence_ok:
        rejected.append("confidence")

    # Gate 2: actionability
    actionability_ok = bool(
        actionability is not None
        and (
            _ACTIONABILITY_RANK.get(actionability, 0)
            >= _ACTIONABILITY_RANK.get(min_actionability, 0)
        )
    )
    if not actionability_ok:
        rejected.append("actionability")

    # Gate 3: safe_to_autofix
    safe_to_autofix_ok = safe_to_autofix
    if not safe_to_autofix_ok:
        rejected.append("safe_to_autofix")

    # Gate 4: non-critical severity
    CRITICAL = FindingSeverity.CRITICAL
    if isinstance(severity, FindingSeverity):
        severity_ok = severity != CRITICAL
    elif isinstance(severity, str):
        severity_ok = severity.lower() != CRITICAL.value
    else:
        severity_ok = True  # Unknown severity treated as eligible
    if not severity_ok:
        rejected.append("severity")

    # Gate 5: validated source (not raw MANUAL)
    if isinstance(source, FindingSource):
        source_ok = source != FindingSource.MANUAL
    elif isinstance(source, str):
        source_ok = source.strip().lower() != FindingSource.MANUAL.value
    else:
        source_ok = False
    if not source_ok:
        rejected.append("source")

    # Gate 6: allowlist check (repo-config-defined paths)
    allowlist_ok = True
    if repo_config is not None:
        rules_disabled = set(repo_config.rules_disabled or [])
        header = str(finding.get("header", "")).strip()
        norm_header = normalize_finding_header(header)
        if norm_header in rules_disabled:
            allowlist_ok = False
            rejected.append("allowlist")
        # Also check path-based allowlist (future extension point)
        path = str(finding.get("path", "")).strip()
        norm_path = normalize_finding_path(path)
        allowlisted_paths = repo_config.rules_enabled or []
        # If rules_enabled is non-empty, only those rules are allowed;
        # anything not in the list is rejected
        if allowlisted_paths and norm_header not in allowlisted_paths:
            allowlist_ok = False
            rejected.append("allowlist")

    eligible = (
        confidence_ok
        and actionability_ok
        and safe_to_autofix_ok
        and severity_ok
        and source_ok
        and allowlist_ok
    )

    if eligible:
        reason = (
            f"Eligible: confidence={confidence:.2f}, "
            f"actionability={actionability}, severity={severity}, "
            f"safe_to_autofix={safe_to_autofix}, source={source}"
        )
    else:
        reason = f"Not eligible: rejected gates={rejected}"

    return RemediationEligibility(
        eligible=eligible,
        reason=reason,
        rejected_gates=list(rejected),
        safe_to_autofix=safe_to_autofix_ok,
        severity_ok=severity_ok,
        actionability_ok=actionability_ok,
        confidence_ok=confidence_ok,
    )
