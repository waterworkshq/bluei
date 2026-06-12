"""Verification module for fix validation.

This module provides a unified interface for verifying fixes across the codebase.
It wraps the lower-level validation functions with a consistent result type.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.models import Finding
from bluei.engine.validation import (
    build_target_checks,
    run_named_checks,
    run_validation_gate,
    verify_fix_closed as _verify_fix_closed,
)


@dataclass
class VerificationResult:
    """Result of a verification operation.

    Attributes:
        passed: Whether the verification passed.
        message: Human-readable message (empty on success).
        is_closed: Whether the finding is closed (for verify_fix_closed).
        regressions: List of check names that regressed.
        target_failures: List of target check names that failed.
        baseline_results: Pre-computed baseline results.
        post_results: Pre-computed post-fix results.
    """

    passed: bool
    message: str = ""
    is_closed: bool = False
    regressions: List[str] = field(default_factory=list)
    target_failures: List[str] = field(default_factory=list)
    baseline_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    post_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def verify_fix_closed(
    worktree_path: Path,
    finding: Finding,
    log_file: Path,
    docs_index_file: Optional[Path] = None,
) -> VerificationResult:
    """Verify that a finding is closed after a fix.

    This wraps the lower-level verify_fix_closed function and returns a
    VerificationResult instead of a bool.

    Args:
        worktree_path: Path to the worktree.
        finding: The finding to verify.
        log_file: Path to the log file.
        docs_index_file: Optional path to docs index file. If None, uses default.

    Returns:
        VerificationResult with is_closed set to the verification result.
    """
    kwargs = {}
    if docs_index_file is not None:
        kwargs["docs_index_file"] = docs_index_file

    is_closed = _verify_fix_closed(
        worktree_path=worktree_path,
        finding=finding,
        log_file=log_file,
        **kwargs,
    )
    return VerificationResult(
        passed=is_closed,
        is_closed=is_closed,
        message="" if is_closed else "Finding still open after fix",
    )


def run_validation(
    repo_path: Path,
    worktree_path: Path,
    checks: Optional[Dict[str, List[str]]],
    baseline_results: Optional[Dict[str, Dict[str, Any]]] = None,
    post_fix_results: Optional[Dict[str, Dict[str, Any]]] = None,
    target_results: Optional[Dict[str, Dict[str, Any]]] = None,
    allow_unchanged_baseline_failures: bool = True,
    log_file: Optional[Path] = None,
) -> VerificationResult:
    """Run validation gate and return a VerificationResult.

    This wraps the lower-level run_validation_gate function and returns a
    VerificationResult instead of a Dict.

    Args:
        repo_path: Repository root path.
        worktree_path: Worktree path.
        checks: Dict of check-name → command list.
        baseline_results: Optional pre-computed baseline results.
        post_fix_results: Optional pre-computed post-fix results.
        target_results: Optional pre-computed target results.
        allow_unchanged_baseline_failures: If True, baseline check failures
            that are unchanged post-fix are not treated as regressions.
        log_file: Optional log file path.

    Returns:
        VerificationResult with validation results.
    """
    result = run_validation_gate(
        repo_path=repo_path,
        worktree_path=worktree_path,
        checks=checks,
        baseline_results=baseline_results,
        post_fix_results=post_fix_results,
        target_results=target_results,
        allow_unchanged_baseline_failures=allow_unchanged_baseline_failures,
        log_file=log_file,
    )
    return VerificationResult(
        passed=result.get("passed", False),
        message=result.get("message", ""),
        regressions=result.get("regressions", []),
        target_failures=result.get("target_failures", []),
        baseline_results=result.get("baseline_results", {}),
        post_results=result.get("post_results", {}),
    )


def build_target_checks_for_finding(finding: Finding) -> Dict[str, List[str]]:
    """Build target checks for a finding.

    This is a convenience wrapper around build_target_checks.

    Args:
        finding: The finding to build checks for.

    Returns:
        Dict of check-name → command list.
    """
    return build_target_checks(finding)
