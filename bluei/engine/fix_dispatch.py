"""Unified fix dispatch — try-replay → cascade → claude → autofix → contextual.

Provides a common dispatch pattern for applying fixes across the codebase.
Each caller can use dispatch_fix() directly or wrap it with its own orchestration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from bluei.engine.models import Finding

_logger = logging.getLogger(__name__)


class FixStrategy(Enum):
    """Fix dispatch strategy — determines the order of fix methods."""

    REPLAY_FIRST = "replay_first"
    CASCADE_FIRST = "cascade_first"
    AUTOFIX_FIRST = "autofix_first"
    CONTEXT_AWARE = "context_aware"
    MINIMAL = "minimal"


@dataclass
class FixDispatchResult:
    """Result of a fix dispatch attempt.

    Attributes:
        success: Whether the fix was applied successfully.
        method_used: Which method was used ("replay", "cascade", "claude",
            "autofix", "contextual", or None if all failed).
        error: Error message if all methods failed.
        replay_pattern_id: Pattern ID if replay was used.
        cost: Claude token cost if Claude was used.
    """

    success: bool
    method_used: Optional[str] = None
    error: Optional[str] = None
    replay_pattern_id: Optional[str] = None
    cost: float = 0.0


def _try_autofix(
    finding: Finding,
    worktree_path: Path,
    log_file: Path,
    *,
    pattern_store_path: Optional[Path] = None,
    detected_frameworks: Optional[list] = None,
) -> bool:
    """Try deterministic autofix. Returns True if applied."""
    from bluei.engine.lifecycle import apply_autofix

    try:
        return apply_autofix(
            worktree_path,
            finding,
            log_file,
            pattern_store_path=pattern_store_path,
            detected_frameworks=detected_frameworks,
        )
    except Exception as exc:
        _logger.debug("autofix failed for %s: %s", finding.finding_id, exc)
        return False


def _try_contextual_fix(
    finding: Finding,
    worktree_path: Path,
    repo_path: Path,
    log_file: Path,
    *,
    baseline_checks: Optional[dict] = None,
    target_checks: Optional[dict] = None,
    claude_cmd_template: Optional[str] = None,
    max_files_changed: int = 5,
    max_loc_diff: int = 200,
    pattern_store_path: Optional[Path] = None,
    detected_frameworks: Optional[list] = None,
) -> bool:
    """Try context-aware fix. Returns True if applied."""
    from bluei.engine.context_fix import apply_contextual_fix

    try:
        return apply_contextual_fix(
            repo_path=repo_path,
            finding=finding,
            log_file=log_file,
            worktree_path=worktree_path,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            claude_cmd_template=claude_cmd_template,
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
            pattern_store_path=pattern_store_path,
            detected_frameworks=detected_frameworks,
        )
    except Exception as exc:
        _logger.debug("contextual fix failed for %s: %s", finding.finding_id, exc)
        return False


def _try_claude_fix(
    finding: Finding,
    worktree_path: Path,
    log_file: Path,
    *,
    baseline_checks: dict,
    target_checks: dict,
    claude_cmd_template: str,
    max_files_changed: int = 5,
    max_loc_diff: int = 200,
    pattern_store_path: Optional[Path] = None,
    lessons_file: Optional[Path] = None,
    findings_file: Optional[Path] = None,
    extra_prompt: Optional[str] = None,
    learned_patterns: Optional[str] = None,
    authoritative_guidelines: Optional[str] = None,
) -> tuple[int, str, Optional[str]]:
    """Try Claude fix. Returns (rc, output, prompt_file)."""
    from bluei.engine.lifecycle import ClaudeFixRequest, apply_claude_fix

    rc, output, prompt_file = apply_claude_fix(
        ClaudeFixRequest(
            worktree_path=worktree_path,
            finding=finding,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            claude_cmd_template=claude_cmd_template,
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
            log_file=log_file,
            pattern_store_path=pattern_store_path,
            lessons_file=lessons_file,
            findings_file=findings_file,
            extra_prompt=extra_prompt,
            learned_patterns=learned_patterns,
            authoritative_guidelines=authoritative_guidelines,
        ),
    )
    return rc, output, prompt_file


def _try_cascade_fix(
    finding: Finding,
    worktree_path: Path,
    log_file: Path,
    *,
    pattern_store_path: Optional[Path] = None,
    deterministic_only: bool = False,
) -> bool:
    """Try cascade fix. Returns True if applied."""
    from bluei.engine.lifecycle import apply_cascade_fix

    try:
        return apply_cascade_fix(
            worktree_path,
            finding,
            log_file,
            pattern_store_path=pattern_store_path,
            deterministic_only=deterministic_only,
        )
    except Exception as exc:
        _logger.debug("cascade fix failed for %s: %s", finding.finding_id, exc)
        return False


def _try_pattern_replay(
    finding: Finding,
    worktree_path: Path,
    log_file: Path,
    *,
    pattern_store: Any,
    baseline_checks: dict,
) -> tuple[bool, Optional[str]]:
    """Try pattern replay. Returns (replayed, pattern_id)."""
    from bluei.engine.pattern_replay import try_replay

    try:
        replayed, replay_pid = try_replay(
            worktree_path=worktree_path,
            finding=finding,
            store=pattern_store,
            baseline_checks=baseline_checks,
            log_file=log_file,
        )
        return replayed, replay_pid
    except Exception as exc:
        _logger.debug("pattern replay failed for %s: %s", finding.finding_id, exc)
        return False, None


def dispatch_fix(
    finding: Finding,
    worktree_path: Path,
    strategy: FixStrategy,
    *,
    repo_path: Path,
    log_file: Path,
    pattern_store: Any = None,
    pattern_store_path: Optional[Path] = None,
    baseline_checks: Optional[dict] = None,
    target_checks: Optional[dict] = None,
    claude_cmd_template: Optional[str] = None,
    max_files_changed: int = 5,
    max_loc_diff: int = 200,
    detected_frameworks: Optional[list] = None,
    deterministic_only: bool = False,
    lessons_file: Optional[Path] = None,
    findings_file: Optional[Path] = None,
    extra_prompt: Optional[str] = None,
    learned_patterns: Optional[str] = None,
    authoritative_guidelines: Optional[str] = None,
) -> FixDispatchResult:
    """Dispatch a fix attempt using the specified strategy.

    Args:
        finding: The Finding to fix.
        worktree_path: Path to the worktree.
        strategy: Which dispatch strategy to use.
        repo_path: Original repo root.
        log_file: Append-only log path.
        pattern_store: Optional FixPatternStore for replay.
        pattern_store_path: Optional path to pattern store.
        baseline_checks: Pre-computed baseline check commands.
        target_checks: Pre-computed target check commands.
        claude_cmd_template: Template for Claude CLI invocation.
        max_files_changed: Maximum allowed files changed by LLM fix.
        max_loc_diff: Maximum allowed LOC diff for LLM fix.
        detected_frameworks: List of detected framework identifiers.
        deterministic_only: If True, skip LLM fixes in cascade.
        lessons_file: Optional path to lessons file.
        findings_file: Optional path to findings file.
        extra_prompt: Optional extra prompt for Claude.
        learned_patterns: Optional learned pattern hints for Claude.
        authoritative_guidelines: Optional authoritative guidelines (ADR-0017)
            injected as prompt-context into the Claude fix prompt.

    Returns:
        FixDispatchResult with success status and method used.
    """
    if baseline_checks is None:
        baseline_checks = {}
    if target_checks is None:
        target_checks = {}

    # Strategy: MINIMAL — replay → autofix only
    if strategy == FixStrategy.MINIMAL:
        if pattern_store is not None:
            replayed, pid = _try_pattern_replay(
                finding,
                worktree_path,
                log_file,
                pattern_store=pattern_store,
                baseline_checks=baseline_checks,
            )
            if replayed:
                return FixDispatchResult(
                    success=True, method_used="replay", replay_pattern_id=pid
                )

        if _try_autofix(
            finding, worktree_path, log_file, pattern_store_path=pattern_store_path
        ):
            return FixDispatchResult(success=True, method_used="autofix")

        return FixDispatchResult(success=False, error="autofix-failed")

    # Strategy: AUTOFIX_FIRST — autofix → contextual → claude
    if strategy == FixStrategy.AUTOFIX_FIRST:
        if _try_autofix(
            finding, worktree_path, log_file, pattern_store_path=pattern_store_path
        ):
            return FixDispatchResult(success=True, method_used="autofix")

        if _try_contextual_fix(
            finding,
            worktree_path,
            repo_path,
            log_file,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            claude_cmd_template=claude_cmd_template or "",
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
            pattern_store_path=pattern_store_path,
            detected_frameworks=detected_frameworks,
        ):
            return FixDispatchResult(success=True, method_used="contextual")

        if claude_cmd_template:
            rc, output, _ = _try_claude_fix(
                finding,
                worktree_path,
                log_file,
                baseline_checks=baseline_checks,
                target_checks=target_checks,
                claude_cmd_template=claude_cmd_template,
                max_files_changed=max_files_changed,
                max_loc_diff=max_loc_diff,
                pattern_store_path=pattern_store_path,
                lessons_file=lessons_file,
                findings_file=findings_file,
                extra_prompt=extra_prompt,
                learned_patterns=learned_patterns,
                authoritative_guidelines=authoritative_guidelines,
            )
            if rc == 0:
                return FixDispatchResult(success=True, method_used="claude")
            return FixDispatchResult(success=False, error=f"claude-rc={rc}")

        return FixDispatchResult(success=False, error="all-methods-failed")

    # Strategy: CASCADE_FIRST — cascade → claude fallback
    if strategy == FixStrategy.CASCADE_FIRST:
        if _try_cascade_fix(
            finding,
            worktree_path,
            log_file,
            pattern_store_path=pattern_store_path,
            deterministic_only=deterministic_only,
        ):
            return FixDispatchResult(success=True, method_used="cascade")

        if claude_cmd_template and not deterministic_only:
            rc, output, _ = _try_claude_fix(
                finding,
                worktree_path,
                log_file,
                baseline_checks=baseline_checks,
                target_checks=target_checks,
                claude_cmd_template=claude_cmd_template,
                max_files_changed=max_files_changed,
                max_loc_diff=max_loc_diff,
                pattern_store_path=pattern_store_path,
                lessons_file=lessons_file,
                findings_file=findings_file,
                extra_prompt=extra_prompt,
                learned_patterns=learned_patterns,
                authoritative_guidelines=authoritative_guidelines,
            )
            if rc == 0:
                return FixDispatchResult(success=True, method_used="claude")

        return FixDispatchResult(success=False, error="cascade-exhausted")

    # Strategy: CONTEXT_AWARE — context rule → autofix or claude
    if strategy == FixStrategy.CONTEXT_AWARE:
        if _try_contextual_fix(
            finding,
            worktree_path,
            repo_path,
            log_file,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            claude_cmd_template=claude_cmd_template or "",
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
            pattern_store_path=pattern_store_path,
            detected_frameworks=detected_frameworks,
        ):
            return FixDispatchResult(success=True, method_used="contextual")

        return FixDispatchResult(success=False, error="contextual-failed")

    # Strategy: REPLAY_FIRST — replay → cascade → claude/autofix → contextual
    if strategy == FixStrategy.REPLAY_FIRST:
        # 1. Try pattern replay
        if pattern_store is not None:
            replayed, pid = _try_pattern_replay(
                finding,
                worktree_path,
                log_file,
                pattern_store=pattern_store,
                baseline_checks=baseline_checks,
            )
            if replayed:
                return FixDispatchResult(
                    success=True, method_used="replay", replay_pattern_id=pid
                )

        # 2. Try cascade (for cascade_fix refactor class)
        if getattr(finding, "refactor_class", "") == "cascade_fix":
            if _try_cascade_fix(
                finding,
                worktree_path,
                log_file,
                pattern_store_path=pattern_store_path,
                deterministic_only=deterministic_only,
            ):
                return FixDispatchResult(success=True, method_used="cascade")
            return FixDispatchResult(success=False, error="cascade-exhausted")

        # 3. Try Claude or autofix
        if claude_cmd_template:
            rc, output, _ = _try_claude_fix(
                finding,
                worktree_path,
                log_file,
                baseline_checks=baseline_checks,
                target_checks=target_checks,
                claude_cmd_template=claude_cmd_template,
                max_files_changed=max_files_changed,
                max_loc_diff=max_loc_diff,
                pattern_store_path=pattern_store_path,
                lessons_file=lessons_file,
                findings_file=findings_file,
                extra_prompt=extra_prompt,
                learned_patterns=learned_patterns,
                authoritative_guidelines=authoritative_guidelines,
            )
            if rc == 0:
                return FixDispatchResult(success=True, method_used="claude")
            return FixDispatchResult(success=False, error=f"claude-rc={rc}")

        # 4. Try autofix
        if _try_autofix(
            finding, worktree_path, log_file, pattern_store_path=pattern_store_path
        ):
            return FixDispatchResult(success=True, method_used="autofix")

        # 5. Try contextual fallback
        if _try_contextual_fix(
            finding,
            worktree_path,
            repo_path,
            log_file,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            claude_cmd_template=claude_cmd_template or "",
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
            pattern_store_path=pattern_store_path,
            detected_frameworks=detected_frameworks,
        ):
            return FixDispatchResult(success=True, method_used="contextual")

        return FixDispatchResult(success=False, error="all-methods-failed")

    return FixDispatchResult(success=False, error=f"unknown-strategy: {strategy}")
