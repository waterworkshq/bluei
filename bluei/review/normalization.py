#!/usr/bin/env python3
"""Finding normalization — extracted from review.cycle for single-responsibility."""

from __future__ import annotations

from typing import Any, Dict, List

from bluei.review.models import (
    FindingActionability,
    FindingSeverity,
    FindingSource,
    make_finding_fingerprint,
    make_review_finding_id,
)
from bluei.app.models import (
    now_iso,
)
from bluei.review.types import CandidateValidationError

# Fields required on a raw candidate finding dict
_REQUIRED_CANDIDATE_FIELDS = frozenset(
    {
        "repo",
        "path",
        "line",
        "header",
        "source",
    }
)


def normalize_candidate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse and normalize a raw candidate finding dict into a clean dict
    suitable for identity assignment and deduplication.

    Args:
        raw: A dict that may contain finding-like fields from any source
             (LLM output, linter, manual entry, etc.)

    Returns:
        A normalized dict with:
        - All required fields coerced to correct types
        - Enums resolved to model enums where possible
        - Defaults filled for optional fields

    Raises:
        CandidateValidationError: If the candidate is missing required fields
            or has values that cannot be coerced.

    The returned dict has these keys (all others from raw are dropped):
        repo, path, line, header, snippet, source, actionability, severity,
        confidence, safe_to_autofix, discovered_at
    """
    errors: List[str] = []

    # Check required fields
    for field_name in _REQUIRED_CANDIDATE_FIELDS:
        if field_name not in raw or raw[field_name] is None:
            errors.append(f"Missing required field: {field_name}")

    if errors:
        raise CandidateValidationError(
            f"Candidate validation failed: {errors[0]}",
            errors=errors,
        )

    # --- Coerce required fields ---
    repo = str(raw["repo"]).strip()
    if not repo:
        errors.append("repo cannot be empty")

    path = str(raw["path"]).strip()
    if not path:
        errors.append("path cannot be empty")

    try:
        line = int(raw["line"])
        if line < 0:
            errors.append(f"line must be non-negative, got {line}")
    except (TypeError, ValueError):
        errors.append(f"line must be an integer, got {raw['line']!r}")

    header = str(raw["header"]).strip()
    if not header:
        errors.append("header cannot be empty")

    if errors:
        raise CandidateValidationError(
            f"Candidate validation failed: {errors[0]}",
            errors=errors,
        )

    # --- Source (required enum) ---
    raw_source = raw.get("source", FindingSource.MANUAL.value)
    try:
        if isinstance(raw_source, FindingSource):
            source = raw_source
        else:
            source = FindingSource(str(raw_source).strip().lower())
    except Exception:
        raise CandidateValidationError(
            f"Invalid source value: {raw_source!r}",
            errors=[f"source must be one of: {[s.value for s in FindingSource]}"],
        )

    # --- Optional enum fields ---
    def _coerce_enum(value: Any, enum_cls, default: Any) -> Any:
        if value is None:
            return default
        try:
            if isinstance(value, enum_cls):
                return value
            return enum_cls(str(value).strip().lower())
        except Exception:
            return default

    actionability = _coerce_enum(
        raw.get("actionability"),
        FindingActionability,
        FindingActionability.MEDIUM,
    )
    severity = _coerce_enum(
        raw.get("severity"),
        FindingSeverity,
        FindingSeverity.MEDIUM,
    )

    # --- Optional scalar fields ---
    snippet = str(raw.get("snippet", ""))

    try:
        confidence = float(raw.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5

    safe_to_autofix = bool(raw.get("safe_to_autofix", False))
    discovered_at = raw.get("discovered_at") or now_iso()

    return {
        "repo": repo,
        "path": path,
        "line": line,
        "header": header,
        "snippet": snippet,
        "source": source,
        "actionability": actionability,
        "severity": severity,
        "confidence": confidence,
        "safe_to_autofix": safe_to_autofix,
        "discovered_at": discovered_at,
    }


def assign_finding_identity(
    normalized: Dict[str, Any],
    attempt: int = 0,
) -> Dict[str, Any]:
    """
    Assign a deterministic finding_id and finding_fingerprint to a
    normalized candidate finding.

    Uses the QA-owned helpers ``make_finding_fingerprint`` and
    ``make_review_finding_id`` so that identity is stable across
    re-runs for the same logical finding.

    Args:
        normalized: A dict from ``normalize_candidate``.
        attempt: Non-negative integer for disambiguating repeated findings
                 at the same location (default 0 = first occurrence).

    Returns:
        The normalized dict augmented with:
        - finding_fingerprint: 64-char SHA-256 hex
        - finding_id: ``rf-{short_fp}-{attempt:03d}``

    Raises:
        ValueError: If attempt is negative.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")

    fp = make_finding_fingerprint(
        repo=normalized["repo"],
        path=normalized["path"],
        line=normalized["line"],
        header=normalized["header"],
        snippet=normalized.get("snippet", ""),
    )
    fid = make_review_finding_id(fp, attempt)

    return {
        **normalized,
        "finding_fingerprint": fp,
        "finding_id": fid,
    }


def dedupe_findings(
    findings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Deduplicate exact structural duplicates from a list of findings.

    Duplicates are identified by identical ``finding_fingerprint`` values.
    When multiple findings share the same fingerprint, the first one
    (in input order) is kept.

    Args:
        findings: List of finding dicts (must have ``finding_fingerprint`` key).

    Returns:
        A new list with duplicates removed, preserving original order.
    """
    seen: set[str] = set()
    result: List[Dict[str, Any]] = []
    for f in findings:
        fp = f.get("finding_fingerprint")
        if not fp:
            # Fall back to repr-based dedup for pre-identity findings
            key = repr(f)
        else:
            key = fp
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result
