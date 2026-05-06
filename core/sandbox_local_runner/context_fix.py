"""context_fix.py — Contextual fix engine for CONTEXTUAL_FIX findings.

Applies context-aware fixes using the appropriate strategy based on matched
context rules.  Routes to deterministic autofix or LLM-based fix depending
on the per-rule context configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .reforge import get_context_rule, match_context
from .state import _append_text

if TYPE_CHECKING:
    from .models import Finding
    from .reforge import ContextOverride


def apply_contextual_fix(
    repo_path: Path,
    finding: "Finding",
    log_file: Path,
    worktree_path: Path,
) -> bool:
    """Apply a context-aware fix for a CONTEXTUAL_FIX finding.

    Strategy selection:
    - deterministic_safe: run ruff --fix via apply_autofix
    - llm_with_context: run Claude fix engine with injected context prompt
    - skip: log and return False (no fix attempted)
    - no context match: fall back to apply_autofix

    Returns True if the fix was applied successfully.
    """
    from .lifecycle import apply_autofix, apply_claude_fix

    context_rule = get_context_rule(finding.rule)
    if not context_rule:
        _append_text(log_file, f'contextual-fix: no context rule for {finding.rule}')
        return apply_autofix(worktree_path, finding, log_file)

    matched = match_context(finding.path, context_rule)
    if not matched:
        # No context match → use default strategy from the rule
        strategy = context_rule.default_strategy
        _append_text(log_file, f'contextual-fix: no context match for {finding.path}, using default strategy={strategy}')
        if strategy in ('deterministic', 'deterministic_safe'):
            return apply_autofix(worktree_path, finding, log_file)
        if strategy == 'llm_with_context':
            prompt = build_contextual_prompt(finding, ContextOverride(
                file_patterns=[],
                framework=context_rule.rule,
                fix_strategy='llm_with_context',
                prompt_hint=context_rule.contexts[0].prompt_hint if context_rule.contexts else None,
            ))
            return apply_claude_fix(
                repo_path=repo_path,
                finding=finding,
                prompt=prompt,
                worktree_path=worktree_path,
                log_file=log_file,
            )
        # skip or unknown
        _append_text(log_file, f'contextual-fix: default strategy={strategy} — skipping')
        return False

    _append_text(log_file, f'contextual-fix: rule={finding.rule} context={matched.framework} strategy={matched.fix_strategy}')

    if matched.fix_strategy in ("deterministic", "deterministic_safe"):
        return apply_autofix(worktree_path, finding, log_file)

    if matched.fix_strategy == "llm_with_context":
        prompt = build_contextual_prompt(finding, matched)
        return apply_claude_fix(
            repo_path=repo_path,
            finding=finding,
            prompt=prompt,
            worktree_path=worktree_path,
            log_file=log_file,
        )

    # skip or unknown
    _append_text(log_file, f'contextual-fix: strategy={matched.fix_strategy} — skipping fix')
    return False


def build_contextual_prompt(finding: "Finding", context: "ContextOverride") -> str:
    """Build a Claude prompt with injected context hints.

    Includes finding details, codebase context, and safety guidance
    from the matched context rule.
    """
    return (
        f"Fix this {finding.rule} issue in {finding.path}.\n"
        f"\n"
        f"## Finding\n"
        f"{finding.snippet}\n"
        f"Line: {finding.line}\n"
        f"Rule: {finding.rule}\n"
        f"\n"
        f"## Codebase Context\n"
        f"Framework: {context.framework}\n"
        f"File patterns matched: {context.file_patterns}\n"
        f"\n"
        f"## Safety Guidance\n"
        f"{context.prompt_hint or 'Apply the fix while respecting codebase conventions.'}\n"
        f"\n"
        f"## Instructions\n"
        f"1. Read the file to understand the surrounding context\n"
        f"2. Apply the minimal fix that resolves the finding\n"
        f"3. Do NOT change unrelated code\n"
        f"4. Preserve framework-specific patterns and conventions\n"
    )
