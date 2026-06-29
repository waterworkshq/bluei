"""Detector oracle for the Foundry (ADR-0020).

Reuses tools/seed/validator.validate_candidate — the linter is the ground-truth
oracle (ADR-0018). Constructs a ParsedRule from the Pattern's before/after and
verifies the before triggers the rule and the after is clean.
"""

from __future__ import annotations

from typing import Tuple

from bluei.tools.seed.models import ParsedRule
from bluei.tools.seed.validator import validate_candidate


def _linter_for_rule(rule: str) -> str:
    """Infer the source linter from the rule prefix."""
    if rule.startswith("ruff"):
        return "ruff"
    if rule.startswith("eslint"):
        return "eslint"
    return "ruff"


def _run_detector_oracle(
    rule: str,
    before: str,
    after: str,
    language: str,
    *,
    ruff_executable: str = "ruff",
    eslint_executable: str = "eslint",
) -> Tuple[bool, str]:
    """Run the detector oracle on a Pattern's before/after pair.

    Returns ``(passed, reason)`` — passed is True only when the before
    triggers the rule AND the after is clean.
    """
    source_linter = _linter_for_rule(rule)
    candidate = ParsedRule(
        rule=rule,
        language=language,
        before=before,
        after=after,
        detector_before="",
        detector_after="",
        negative_examples=[],
        has_autofix=False,
        source_linter=source_linter,
        validation_command=f"{source_linter} check --select {rule}"
        if source_linter == "ruff"
        else f"eslint --rule {rule}",
        description="Foundry oracle candidate",
    )
    result = validate_candidate(
        candidate,
        ruff_executable=ruff_executable,
        eslint_executable=eslint_executable,
    )
    if result.valid:
        return True, ""
    return False, result.reason or "detector oracle failed"
