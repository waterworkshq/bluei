"""reforge.py — Refactor-class finding detection, routing, and state scaffolding.

This module provides the first autonomous-large-refactor scaffolding:

1. Finding classification — categorizes a Finding into:
     SIMPLE_FIX   : deterministic single-file, single-edit
     REFACTOR_CLASS: multi-file or structural (xo-max-lines, xo-complexity)
     CLAUDE_FIX   : rule needs LLM judgment (type safety, test coverage)

2. RefactorPhase state machine — tracks progress of a REFACTOR_CLASS work item:
     PLANNING → SPLITTING → VALIDATING → DONE (or ABORTED)

3. Safety gates — hard limits on what autonomous refactors can do without
   human review.

Specimen: ky test/hooks.ts (4271 lines) → split into part1-4.ts
"""

from __future__ import annotations

import enum
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set

from .constants import MAX_LINES_REFACTOR_LIMIT, MAX_LINES_REFACTOR_TARGET


class RefactorClass(str, enum.Enum):
    """Taxonomy of fix classes, ordered by increasing autonomy risk."""

    #: Deterministic single-file single-edit; no LLM needed.
    SIMPLE_FIX = "simple_fix"

    #: Context-dependent fix — technically possible but requires codebase context.
    #: Uses context-aware pipeline: deterministic_safe or llm_with_context.
    CONTEXTUAL_FIX = "contextual_fix"

    #: Structural change — multi-file split/merge, complexity reduction.
    #: Requires Claude fix engine and multi-phase validation.
    REFACTOR_CLASS = "refactor_class"

    #: Rule that needs LLM judgment (type safety, test coverage gaps).
    #: Uses Claude fix engine but without multi-phase tracking.
    CLAUDE_FIX = "claude_fix"


class RefactorPhase(str, enum.Enum):
    """State machine for REFACTOR_CLASS work items."""

    #: Analyzing file structure, planning split/merge strategy.
    PLANNING = "planning"

    #: Actively rewriting files (splitting, moving code).
    SPLITTING = "splitting"

    #: Running baseline + target validation checks.
    VALIDATING = "validating"

    #: All checks passed; refactor complete.
    DONE = "done"

    #: Validation failed or safety gate triggered; requires human review.
    ABORTED = "aborted"


@dataclass
class ContextOverride:
    """A specific context where the default fix strategy changes."""
    file_patterns: list[str]
    framework: str
    fix_strategy: str
    prompt_hint: Optional[str] = None


@dataclass
class ContextRule:
    """A rule with context-specific fix behavior."""
    rule: str
    default_strategy: str
    contexts: list[ContextOverride] = field(default_factory=list)


# Rules that require structural refactoring (multi-file, TDD approach).
REFACTOR_CLASS_RULES: Set[str] = {
    "xo-max-lines",   # file-too-long → split into partN.ts
    "xo-complexity",  # function-complexity → extract/subtitle
}

# Rules that need LLM judgment but aren't structural.
CLAUDE_FIX_RULES: Set[str] = {
    "type-explicit-any",
    "test-coverage-branch",
    "test-coverage-function",
    "test-gap-missing-case",
    "test-gap-missing-file",
}

# Safety gate: files above this line count MUST go through human review
# even if they are otherwise REFACTOR_CLASS.  The Claude engine is
# invoked but the result is flagged rather than auto-committed.
LARGE_FILE_SAFETY_LIMIT = 5_000


@dataclass
class RefactorWork:
    """Lightweight state record for a REFACTOR_CLASS finding.

    One instance per active refactor work item.
    Stored in the findings JSONL record under the ``refactor_work`` key.
    """

    finding_id: str

    #: Current phase of the refactor state machine.
    phase: RefactorPhase = RefactorPhase.PLANNING

    #: For xo-max-lines: the planned split targets (e.g. ["part1.ts", "part2.ts"]).
    planned_targets: list[str] = field(default_factory=list)

    #: For xo-max-lines: number of lines in the original (pre-refactor) file.
    original_line_count: int = 0

    #: max lines per target file that the refactor aims for.
    target_lines_per_file: int = MAX_LINES_REFACTOR_TARGET

    #: Set of files that have been written so far in the SPLITTING phase.
    written_files: set[str] = field(default_factory=set)

    #: Validation fingerprint from the baseline run (before any changes).
    baseline_fingerprint: str = ""

    #: Human review required flag — set when safety gate triggers.
    needs_human_review: bool = False

    #: Human review outcome (if reviewed).
    review_outcome: Optional[str] = None

    def mark_splitting(self, targets: list[str], original_line_count: int) -> None:
        self.phase = RefactorPhase.SPLITTING
        self.planned_targets = list(targets)
        self.original_line_count = original_line_count
        self.written_files = set()

    def mark_validating(self, baseline_fingerprint: str) -> None:
        self.phase = RefactorPhase.VALIDATING
        self.baseline_fingerprint = baseline_fingerprint

    def mark_done(self) -> None:
        self.phase = RefactorPhase.DONE

    def mark_aborted(self, reason: str) -> None:
        self.phase = RefactorPhase.ABORTED
        self.needs_human_review = True
        self.review_outcome = reason


def _load_context_rules() -> dict[str, ContextRule]:
    """Load context rules from constants.CONTEXT_RULES."""
    from .constants import CONTEXT_RULES
    rules = {}
    for entry in CONTEXT_RULES:
        contexts = [
            ContextOverride(
                file_patterns=c["file_patterns"],
                framework=c["framework"],
                fix_strategy=c["fix_strategy"],
                prompt_hint=c.get("prompt_hint"),
            )
            for c in entry.get("contexts", [])
        ]
        rules[entry["rule"]] = ContextRule(
            rule=entry["rule"],
            default_strategy=entry["default_strategy"],
            contexts=contexts,
        )
    return rules


_CONTEXT_RULES_CACHE: Optional[dict[str, ContextRule]] = None


def get_context_rule(rule: str) -> Optional[ContextRule]:
    """Get context rule for a rule name, or None if not found."""
    global _CONTEXT_RULES_CACHE
    if _CONTEXT_RULES_CACHE is None:
        _CONTEXT_RULES_CACHE = _load_context_rules()
    return _CONTEXT_RULES_CACHE.get(rule)


def match_context(file_path: str, context_rule: ContextRule) -> Optional[ContextOverride]:
    """Check if a file path matches any context override for this rule."""
    for ctx in context_rule.contexts:
        for pattern in ctx.file_patterns:
            if fnmatch.fnmatch(file_path, pattern):
                return ctx
    return None


def classify_finding(finding) -> RefactorClass:
    """Classify a Finding into one of three fix classes.

    This is the primary routing function — called before any fix engine
    is invoked.  It determines the execution path and safety-gate behaviour.

    Args:
        finding: A ``Finding`` dataclass instance.

    Returns:
        ``RefactorClass`` enum value.
    """
    rule = getattr(finding, "rule", "") or ""

    # Explicit REFACTOR_CLASS rules always take this path.
    if rule in REFACTOR_CLASS_RULES:
        finding.refactor_class = RefactorClass.REFACTOR_CLASS.value
        return RefactorClass.REFACTOR_CLASS

    # Explicit CLAUDE_FIX rules (LLM judgment needed).
    if rule in CLAUDE_FIX_RULES:
        finding.refactor_class = RefactorClass.CLAUDE_FIX.value
        return RefactorClass.CLAUDE_FIX

    # Check context rule registry (NEW)
    context_rule = get_context_rule(rule)
    if context_rule:
        matched = match_context(getattr(finding, "path", "") or "", context_rule)
        if matched:
            if matched.fix_strategy == "skip":
                finding.refactor_class = RefactorClass.REFACTOR_CLASS.value
                return RefactorClass.REFACTOR_CLASS
            if matched.fix_strategy == "llm_with_context":
                finding.refactor_class = RefactorClass.CONTEXTUAL_FIX.value
                return RefactorClass.CONTEXTUAL_FIX
            if matched.fix_strategy == "deterministic_safe":
                finding.refactor_class = RefactorClass.SIMPLE_FIX.value
                return RefactorClass.SIMPLE_FIX
        # No context match → use default
        if context_rule.default_strategy == "deterministic":
            finding.refactor_class = RefactorClass.SIMPLE_FIX.value
            return RefactorClass.SIMPLE_FIX
        elif context_rule.default_strategy == "llm_with_context":
            finding.refactor_class = RefactorClass.CONTEXTUAL_FIX.value
            return RefactorClass.CONTEXTUAL_FIX
        elif context_rule.default_strategy == "skip":
            finding.refactor_class = RefactorClass.CLAUDE_FIX.value
            return RefactorClass.CLAUDE_FIX

    # Ruff rules with safe_to_autofix are SIMPLE_FIX.
    if rule.startswith("ruff-") and getattr(finding, "safe_to_autofix", False):
        finding.refactor_class = RefactorClass.SIMPLE_FIX.value
        return RefactorClass.SIMPLE_FIX

    # Default: delegate to Claude for the rest.
    # Most rules land here and go through apply_claude_fix().
    finding.refactor_class = RefactorClass.CLAUDE_FIX.value
    return RefactorClass.CLAUDE_FIX


def is_large_refactor(finding, worktree_path: Optional[Path] = None) -> bool:
    """Safety gate: return True when a refactor exceeds safe autonomous limits.

    Checks two dimensions:
    1. Absolute file size — files over LARGE_FILE_SAFETY_LIMIT lines always
       need human review, even if they are valid REFACTOR_CLASS findings.
    2. Ratio check — for xo-max-lines, if (original_lines / MAX_LINES_REFACTOR_TARGET)
       would require more than 6 split files, flag as large.

    Args:
        finding: A ``Finding`` dataclass instance.
        worktree_path: Optional path to the repo worktree (used for line count).

    Returns:
        True if the refactor is too large/complex for fully-autonomous handling.
    """
    rule = getattr(finding, "rule", "") or ""
    path = getattr(finding, "path", "") or ""

    # Check 1: absolute size limit
    if worktree_path and path:
        file_path = worktree_path / path
        if file_path.is_file():
            try:
                line_count = len(file_path.read_text(encoding="utf-8").splitlines())
                if line_count > LARGE_FILE_SAFETY_LIMIT:
                    return True
            except Exception:
                pass

    # Check 2: split-ratio check for xo-max-lines
    if rule == "xo-max-lines" and worktree_path and path:
        file_path = worktree_path / path
        if file_path.is_file():
            try:
                line_count = len(file_path.read_text(encoding="utf-8").splitlines())
                target = MAX_LINES_REFACTOR_TARGET
                estimated_parts = (line_count + target - 1) // target
                # Flag if we'd need more than 6 parts — likely too structural
                if estimated_parts > 6:
                    return True
            except Exception:
                pass

    return False


def can_auto_refactor(finding, worktree_path: Optional[Path] = None) -> tuple[bool, str]:
    """Combined safety gate: returns (allowed, reason).

    Use this before committing to the autonomous refactor path.
    If allowed=False, the finding should be routed to human review.

    Args:
        finding: A ``Finding`` dataclass instance.
        worktree_path: Optional path to the repo worktree.

    Returns:
        (True, "")  — autonomous refactor path is clear.
        (False, msg) — safety gate triggered; include msg in review request.
    """
    rule = getattr(finding, "rule", "") or ""
    path = getattr(finding, "path", "") or ""

    if is_large_refactor(finding, worktree_path):
        return False, (
            f"safety_gate: file '{path}' exceeds LARGE_FILE_SAFETY_LIMIT "
            f"({LARGE_FILE_SAFETY_LIMIT} lines) or split ratio > 6×; "
            "human review required before committing."
        )

    # For xo-max-lines, verify target is within the auto-refactor range
    if rule == "xo-max-lines" and worktree_path and path:
        file_path = worktree_path / path
        if file_path.is_file():
            try:
                line_count = len(file_path.read_text(encoding="utf-8").splitlines())
                if line_count > MAX_LINES_REFACTOR_LIMIT:
                    return False, (
                        f"safety_gate: xo-max-lines file '{path}' has {line_count} lines, "
                        f"exceeds MAX_LINES_REFACTOR_LIMIT ({MAX_LINES_REFACTOR_LIMIT}); "
                        "human review required."
                    )
            except Exception:
                pass

    return True, ""


def describe_class(rc: RefactorClass) -> str:
    """Human-readable description of a RefactorClass."""
    return {
        RefactorClass.SIMPLE_FIX: "Simple deterministic fix — single file, single edit, no LLM needed.",
        RefactorClass.CONTEXTUAL_FIX: (
            "Context-aware fix — technically possible but requires codebase context. "
            "Uses context-aware pipeline with deterministic_safe or llm_with_context strategy."
        ),
        RefactorClass.REFACTOR_CLASS: (
            "Structural refactor — file split, complexity reduction, or multi-file rewrite. "
            "Uses Claude fix engine with multi-phase validation."
        ),
        RefactorClass.CLAUDE_FIX: (
            "LLM-judgment fix — type safety, test coverage, or similar rule that needs "
            "Claude fix engine (single-pass, no multi-phase tracking)."
        ),
    }.get(rc, "Unknown")
