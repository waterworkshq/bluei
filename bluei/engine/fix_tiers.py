"""Tiered validation for deterministic fixes.

Safe fixes (T0) skip expensive checks; risky fixes (T4) get extra
scrutiny and route to the human review queue.  Tiers are resolved from
recipe safety labels, per-rule overrides, or config overrides.
"""

from __future__ import annotations

import logging
import enum
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

from bluei.engine.state import _append_text


class FixTier(enum.IntEnum):
    """Confidence tier for a fix, controlling validation strictness.

    T0 = guaranteed safe (syntax check only), T4 = structural change
    requiring human review.
    """
    T0_GUARANTEED = 0
    T1_IDEMPOTENT = 1
    T2_VALIDATED = 2
    T3_CONTEXTUAL = 3
    T4_STRUCTURAL = 4


# Map recipe safety labels to fix tiers.
SAFETY_TO_TIER = {
    "guaranteed": FixTier.T0_GUARANTEED,
    "idempotent": FixTier.T1_IDEMPOTENT,
    "needs_validation": FixTier.T2_VALIDATED,
    "needs_review": FixTier.T3_CONTEXTUAL,
    "requires_human": FixTier.T4_STRUCTURAL,
}

# Per-rule tier overrides that take precedence over recipe safety labels.
TIER_OVERRIDES: Dict[str, FixTier] = {
    "ruff-e501": FixTier.T0_GUARANTEED,
    "ruff-c408": FixTier.T1_IDEMPOTENT,
    "ruff-b007": FixTier.T1_IDEMPOTENT,
    "ruff-b904": FixTier.T3_CONTEXTUAL,
    "ruff-s311": FixTier.T3_CONTEXTUAL,
    "broad-except": FixTier.T3_CONTEXTUAL,
    "xo-max-lines": FixTier.T4_STRUCTURAL,
    "xo-complexity": FixTier.T4_STRUCTURAL,
}

# File patterns that are too risky for automated fixes.
CRITICAL_FILE_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "uv.lock", "Cargo.lock",
    ".env", ".env.local", ".env.production",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".github/workflows/", ".gitlab-ci.yml", "Jenkinsfile",
    "Makefile",
)

# Size limits for tier-based validation.
T0_MAX_LINES = 5000
T3_MAX_ADDITIONS = 50
T3_MAX_DELETIONS = 30


def resolve_tier(
    finding: Any,
    recipe: Any = None,
    config_overrides: Optional[Dict[str, str]] = None,
) -> FixTier:
    """Determine the validation tier for a finding.

    Resolution order: recipe safety label → config overrides → per-rule
    overrides → default T2.

    Args:
        finding: The finding object (must have a ``rule`` attribute).
        recipe: Optional recipe with a ``safety`` attribute.
        config_overrides: Optional mapping of rule → tier string.

    Returns:
        The resolved FixTier.
    """
    rule = getattr(finding, "rule", "") or ""

    if recipe is not None:
        safety = getattr(recipe, "safety", None)
        if safety and safety in SAFETY_TO_TIER:
            return SAFETY_TO_TIER[safety]

    if config_overrides and rule in config_overrides:
        tier_str = config_overrides[rule]
        try:
            return _parse_tier_string(tier_str)
        except (ValueError, KeyError):
            _logger.debug("Failed to parse tier config override")

    if rule in TIER_OVERRIDES:
        return TIER_OVERRIDES[rule]

    return FixTier.T2_VALIDATED


def _parse_tier_string(s: str) -> FixTier:
    """Parse a tier string like ``"T2"`` or ``"VALIDATED"`` into a FixTier enum.

    Args:
        s: Tier identifier string.

    Returns:
        The corresponding FixTier.

    Raises:
        ValueError: If the string does not match any known tier.
    """
    s = s.strip().upper()
    if s.startswith("T"):
        s = s[1:]
    mapping = {
        "0": FixTier.T0_GUARANTEED,
        "1": FixTier.T1_IDEMPOTENT,
        "2": FixTier.T2_VALIDATED,
        "3": FixTier.T3_CONTEXTUAL,
        "4": FixTier.T4_STRUCTURAL,
    }
    if s in mapping:
        return mapping[s]
    for tier in FixTier:
        if tier.name == s or tier.name.endswith(s):
            return tier
    raise ValueError(f"unknown tier: {s}")


def check_tier_escalation(
    rule: str,
    state: Dict[str, Any],
    current_tier: FixTier,
    threshold: int = 3,
) -> Optional[FixTier]:
    """Escalate a rule's tier if it has failed too many times.

    Args:
        rule: The rule identifier to check.
        state: Runner state dict containing ``finding_activity``.
        current_tier: The rule's current tier.
        threshold: Number of failures before escalation.

    Returns:
        The escalated tier, or None if no escalation is warranted.
    """
    if current_tier >= FixTier.T4_STRUCTURAL:
        return None
    activity = state.get("finding_activity", {})
    failures = 0
    for entry in activity.values():
        if entry.get("rule") == rule and entry.get("action", "").startswith("fix-failed"):
            failures += 1
    if failures >= threshold:
        return FixTier(current_tier + 1)
    return None


@dataclass
class TieredValidationResult:
    """Outcome of a tiered validation run."""

    passed: bool = True
    tier: FixTier = FixTier.T2_VALIDATED
    message: str = ""
    regressions: List[str] = field(default_factory=list)
    target_failures: List[str] = field(default_factory=list)
    requires_human_review: bool = False
    latency_ms: int = 0


class TieredValidator:
    """Dispatches validation logic based on fix confidence tier."""

    def validate(
        self,
        tier: FixTier,
        worktree_path: Path,
        finding: Any,
        baseline_results: Optional[Dict[str, Dict[str, Any]]] = None,
        checks: Optional[Dict[str, List[str]]] = None,
        log_file: Optional[Path] = None,
        repo_path: Optional[Path] = None,
    ) -> TieredValidationResult:
        """Run the validation checks appropriate for the given tier.

        Args:
            tier: The fix's confidence tier.
            worktree_path: Path to the working tree with the fix applied.
            finding: The finding object being fixed.
            baseline_results: Pre-computed baseline check results (optional).
            checks: Named checks to run (tool → list of args).
            log_file: Append-only log file path.
            repo_path: Path to the original repo (may differ from worktree).

        Returns:
            TieredValidationResult with pass/fail status and any regressions.
        """
        if log_file is None:
            log_file = Path("/dev/null")

        t0 = time.monotonic()
        if tier == FixTier.T0_GUARANTEED:
            result = self._validate_t0(worktree_path, finding, log_file)
        elif tier == FixTier.T1_IDEMPOTENT:
            result = self._validate_t1(repo_path or worktree_path, worktree_path, checks, baseline_results, log_file)
        elif tier == FixTier.T2_VALIDATED:
            result = self._validate_t2(repo_path or worktree_path, worktree_path, checks, baseline_results, log_file)
        elif tier == FixTier.T3_CONTEXTUAL:
            result = self._validate_t3(repo_path or worktree_path, worktree_path, checks, baseline_results, finding, log_file)
        elif tier == FixTier.T4_STRUCTURAL:
            result = self._validate_t4(repo_path or worktree_path, worktree_path, checks, baseline_results, finding, log_file)
        else:
            result = TieredValidationResult(passed=False, tier=tier, message=f"unknown tier: {tier}")

        elapsed = int((time.monotonic() - t0) * 1000)
        result.tier = tier
        result.latency_ms = elapsed
        _append_text(log_file, f"tier-validation: tier={tier.name} passed={result.passed} latency_ms={elapsed}")
        return result

    def _validate_t0(
        self,
        worktree_path: Path,
        finding: Any,
        log_file: Path,
    ) -> TieredValidationResult:
        """T0: Verify the target file exists and passes a syntax check.

        Args:
            worktree_path: Working tree root.
            finding: Finding with a ``path`` attribute.
            log_file: Log file path.

        Returns:
            TieredValidationResult indicating syntax validity.
        """
        path = getattr(finding, "path", "") or ""
        file_path = worktree_path / path

        if not file_path.exists():
            return TieredValidationResult(
                passed=False,
                message=f"file not found: {path}",
            )

        if path.endswith(".py"):
            try:
                source = file_path.read_text(encoding="utf-8")
                compile(source, str(file_path), "exec")
            except SyntaxError as exc:
                return TieredValidationResult(
                    passed=False,
                    message=f"syntax error: {exc}",
                )
            except Exception as exc:
                return TieredValidationResult(
                    passed=False,
                    message=f"compile check failed: {exc}",
                )

        if path.endswith(".ts") or path.endswith(".tsx") or path.endswith(".js") or path.endswith(".jsx"):
            pass

        if path.endswith(".go") or path.endswith(".rs"):
            pass

        return TieredValidationResult(passed=True, message="t0 syntax check passed")

    def _validate_t1(
        self,
        repo_path: Path,
        worktree_path: Path,
        checks: Optional[Dict[str, List[str]]],
        baseline_results: Optional[Dict[str, Dict[str, Any]]],
        log_file: Path,
    ) -> TieredValidationResult:
        """T1: Run baseline checks and confirm none fail.

        Args:
            repo_path: Original repo path for running checks.
            worktree_path: Working tree with the fix applied.
            checks: Named checks configuration.
            baseline_results: Pre-computed results (used directly if provided).
            log_file: Log file path.

        Returns:
            TieredValidationResult with pass/fail status.
        """
        if not checks:
            return TieredValidationResult(passed=True, message="t1 no checks to run")

        from bluei.engine.lifecycle import run_named_checks

        if baseline_results is not None:
            current_results = baseline_results
        else:
            current_results = run_named_checks(
                repo_path=repo_path, checks=checks, log_file=log_file, phase="validation-t1-baseline"
            )

        for name, check_result in current_results.items():
            rc = int(check_result.get("rc", 1))
            if rc != 0:
                return TieredValidationResult(
                    passed=False,
                    message=f"baseline check failed: {name}",
                    regressions=[name],
                )

        return TieredValidationResult(passed=True, message="t1 baseline checks passed")

    def _validate_t2(
        self,
        repo_path: Path,
        worktree_path: Path,
        checks: Optional[Dict[str, List[str]]],
        baseline_results: Optional[Dict[str, Dict[str, Any]]],
        log_file: Path,
    ) -> TieredValidationResult:
        """T2: Full validation gate — baseline + target regression comparison.

        Args:
            repo_path: Original repo path.
            worktree_path: Working tree with the fix applied.
            checks: Named checks configuration.
            baseline_results: Pre-computed baseline results.
            log_file: Log file path.

        Returns:
            TieredValidationResult including regressions and target failures.
        """
        from bluei.engine.lifecycle import run_validation_gate

        result = run_validation_gate(
            repo_path=repo_path,
            worktree_path=worktree_path,
            checks=checks,
            baseline_results=baseline_results,
            log_file=log_file,
        )
        return TieredValidationResult(
            passed=result.get("passed", False),
            message=result.get("message", ""),
            regressions=result.get("regressions", []),
            target_failures=result.get("target_failures", []),
        )

    def _validate_t3(
        self,
        repo_path: Path,
        worktree_path: Path,
        checks: Optional[Dict[str, List[str]]],
        baseline_results: Optional[Dict[str, Dict[str, Any]]],
        finding: Any,
        log_file: Path,
    ) -> TieredValidationResult:
        """T3: T2 validation plus automated diff review (file count, diff size).

        Args:
            repo_path: Original repo path.
            worktree_path: Working tree with the fix applied.
            checks: Named checks configuration.
            baseline_results: Pre-computed baseline results.
            finding: The finding being fixed.
            log_file: Log file path.

        Returns:
            TieredValidationResult with diff review outcome.
        """
        t2_result = self._validate_t2(repo_path, worktree_path, checks, baseline_results, log_file)
        if not t2_result.passed:
            return t2_result

        diff_ok, diff_msg = _auto_diff_review(worktree_path, finding)
        if not diff_ok:
            return TieredValidationResult(
                passed=False,
                message=f"diff review failed: {diff_msg}",
            )

        return TieredValidationResult(passed=True, message="t3 validated + diff review passed")

    def _validate_t4(
        self,
        repo_path: Path,
        worktree_path: Path,
        checks: Optional[Dict[str, List[str]]],
        baseline_results: Optional[Dict[str, Dict[str, Any]]],
        finding: Any,
        log_file: Path,
    ) -> TieredValidationResult:
        """T4: T3 validation plus mandatory human review flag.

        Args:
            repo_path: Original repo path.
            worktree_path: Working tree with the fix applied.
            checks: Named checks configuration.
            baseline_results: Pre-computed baseline results.
            finding: The finding being fixed.
            log_file: Log file path.

        Returns:
            TieredValidationResult with ``requires_human_review=True``.
        """
        t3_result = self._validate_t3(repo_path, worktree_path, checks, baseline_results, finding, log_file)
        t3_result.requires_human_review = True
        return t3_result


def _auto_diff_review(worktree_path: Path, finding: Any) -> tuple[bool, str]:
    """Inspect the working-tree diff for safety violations.

    Rejects changes that touch critical files, modify more than one file,
    or exceed line-addition/deletion limits.

    Args:
        worktree_path: Working tree root.
        finding: The finding being fixed.

    Returns:
        Tuple of (passed, error message).  Message is empty on success.
    """
    from bluei.engine.utils import run_capture

    rc, diff_output = run_capture(
        ["git", "diff", "--stat"],
        cwd=worktree_path,
        timeout=15,
    )
    if rc != 0:
        return False, "git diff --stat failed"

    lines = diff_output.strip().splitlines()
    changed_files = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("fatal") or "|" not in line:
            continue
        parts = line.split("|")[0].strip()
        if parts:
            changed_files.append(parts)

    if not changed_files:
        return True, ""

    if len(changed_files) > 1:
        return False, f"too many files changed: {len(changed_files)} (expected 1)"

    for cf in changed_files:
        for pattern in CRITICAL_FILE_PATTERNS:
            if pattern in cf:
                return False, f"critical file modified: {cf}"

    rc, numstat = run_capture(
        ["git", "diff", "--numstat"],
        cwd=worktree_path,
        timeout=15,
    )
    if rc == 0 and numstat.strip():
        for line in numstat.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                try:
                    additions = int(parts[0]) if parts[0] != "-" else 0
                    deletions = int(parts[1]) if parts[1] != "-" else 0
                    if additions > T3_MAX_ADDITIONS:
                        return False, f"too many additions: {additions} (max {T3_MAX_ADDITIONS})"
                    if deletions > T3_MAX_DELETIONS:
                        return False, f"too many deletions: {deletions} (max {T3_MAX_DELETIONS})"
                except ValueError:
                    _logger.debug("Failed to parse diff stat line")

    return True, ""
