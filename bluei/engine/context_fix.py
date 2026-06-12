"""Context-aware fix engine for CONTEXTUAL_FIX findings.

Routes fixes to deterministic autofix or LLM-based strategies based on
per-rule context configuration and detected framework.  Tracks repeated
failures to auto-skip hopeless rule+framework combinations.
"""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, TYPE_CHECKING

_logger = logging.getLogger(__name__)

from bluei.engine.reforge import ContextOverride, get_context_rule, match_context
from bluei.engine.state import _append_text

if TYPE_CHECKING:
    from bluei.engine.models import Finding

_CONTEXT_FAILURE_SKIP_THRESHOLD = 3


def _load_lifecycle_functions():
    modules = []
    try:
        from bluei.engine import lifecycle as core_lifecycle

        modules.append(core_lifecycle)
    except (ImportError, AttributeError):
        _logger.debug("Failed to import lifecycle functions")

    for module in modules:
        apply_autofix = getattr(module, "apply_autofix", None)
        apply_claude_fix = getattr(module, "apply_claude_fix", None)
        if hasattr(apply_autofix, "mock_calls") or hasattr(
            apply_claude_fix, "mock_calls"
        ):
            return (
                apply_autofix,
                apply_claude_fix,
                getattr(module, "ClaudeFixRequest", None),
            )

    if not modules:
        raise ModuleNotFoundError("bluei engine lifecycle module not found")
    module = modules[0]
    return module.apply_autofix, module.apply_claude_fix, module.ClaudeFixRequest


@dataclass
class ContextFailure:
    """Tracks repeated fix failures for a rule+file+framework combination."""

    rule: str
    file_path: str
    framework: str
    strategy: str
    count: int = 0


def record_context_failure(
    failures_path: Path, rule: str, file_path: str, framework: str, strategy: str
) -> None:
    """Append or increment a failure record for a rule+file+framework triple.

    Args:
        failures_path: Path to the JSONL failures file.
        rule: Rule identifier.
        file_path: Relative file path of the finding.
        framework: Detected framework name.
        strategy: Fix strategy that was attempted.
    """
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    records = load_context_failures(failures_path)
    key = f"{rule}::{file_path}::{framework}"
    if key in records:
        records[key].count += 1
    else:
        records[key] = ContextFailure(
            rule=rule,
            file_path=file_path,
            framework=framework,
            strategy=strategy,
            count=1,
        )
    lines = []
    for cf in records.values():
        lines.append(
            json.dumps(
                {
                    "rule": cf.rule,
                    "file_path": cf.file_path,
                    "framework": cf.framework,
                    "strategy": cf.strategy,
                    "count": cf.count,
                }
            )
        )
    failures_path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def load_context_failures(failures_path: Path) -> Dict[str, ContextFailure]:
    """Load all failure records from the JSONL file.

    Args:
        failures_path: Path to the failures file.

    Returns:
        Mapping of ``"rule::file::framework"`` → ContextFailure.
    """
    if not failures_path.exists():
        return {}
    records = {}
    for line in failures_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            key = f"{d['rule']}::{d['file_path']}::{d['framework']}"
            records[key] = ContextFailure(
                rule=d["rule"],
                file_path=d["file_path"],
                framework=d.get("framework", ""),
                strategy=d.get("strategy", ""),
                count=d.get("count", 1),
            )
        except (json.JSONDecodeError, KeyError):
            continue
    return records


def should_skip_due_to_failures(
    failures_path: Path, rule: str, file_path: str, framework: str
) -> bool:
    """Check whether a rule+file+framework has failed enough times to skip.

    Args:
        failures_path: Path to the failures file.
        rule: Rule identifier.
        file_path: Relative file path.
        framework: Detected framework name.

    Returns:
        True if the failure count meets or exceeds the skip threshold.
    """
    records = load_context_failures(failures_path)
    key = f"{rule}::{file_path}::{framework}"
    entry = records.get(key)
    return entry is not None and entry.count >= _CONTEXT_FAILURE_SKIP_THRESHOLD


def update_context_rule_on_repeated_failure(
    rule: str,
    context_framework: str,
    failures_path: Path,
) -> bool:
    """Auto-skip a rule+context after repeated failures by setting its strategy to skip.

    Mutates the in-memory context rule cache (persists for the process lifetime
    but is NOT written back to constants.py — restart resets to defaults).

    Args:
        rule: Rule identifier.
        context_framework: Framework that triggered the failures.
        failures_path: Path to the JSONL failures file.

    Returns:
        True if the rule was mutated to skip.
    """
    records = load_context_failures(failures_path)
    failure_count = 0
    for cf in records.values():
        if cf.rule == rule and cf.framework == context_framework:
            failure_count += cf.count
    if failure_count < _CONTEXT_FAILURE_SKIP_THRESHOLD:
        return False

    context_rule = get_context_rule(rule)
    if not context_rule:
        return False

    for ctx in context_rule.contexts:
        if ctx.framework == context_framework and ctx.fix_strategy != "skip":
            ctx.fix_strategy = "skip"
            return True
        if ctx.framework == "any" and ctx.fix_strategy != "skip":
            ctx.fix_strategy = "skip"
            return True

    if context_rule.default_strategy != "skip":
        context_rule.default_strategy = "skip"
        return True

    return False


def apply_contextual_fix(
    repo_path: Path,
    finding: "Finding",
    log_file: Path,
    worktree_path: Path,
    pattern_store_path: Optional[Path] = None,
    detected_frameworks: Optional[list] = None,
    baseline_checks: Optional[dict] = None,
    target_checks: Optional[dict] = None,
    claude_cmd_template: Optional[str] = None,
    max_files_changed: int = 5,
    max_loc_diff: int = 200,
) -> bool:
    """Apply a context-aware fix for a CONTEXTUAL_FIX finding.

    Strategy selection: deterministic_safe → ruff autofix, llm_with_context →
    Claude fix with injected prompt, skip → no fix.  Falls back to autofix
    when no context rule matches.

    Args:
        repo_path: Original repo root.
        finding: The Finding object to fix.
        log_file: Append-only log path.
        worktree_path: Working tree with the fix applied.
        pattern_store_path: Optional path to the pattern JSONL store.
        detected_frameworks: List of detected framework identifiers.
        baseline_checks: Pre-computed baseline check results.
        target_checks: Pre-computed target check results.
        claude_cmd_template: Template for the Claude CLI invocation.
        max_files_changed: Maximum allowed files changed by an LLM fix.
        max_loc_diff: Maximum allowed lines-of-code diff for an LLM fix.

    Returns:
        True if the fix was applied successfully.
    """
    apply_autofix, apply_claude_fix, ClaudeFixRequest = _load_lifecycle_functions()

    context_rule = get_context_rule(finding.rule)
    if not context_rule:
        _append_text(log_file, f"contextual-fix: no context rule for {finding.rule}")
        return apply_autofix(
            worktree_path,
            finding,
            log_file,
            pattern_store_path=pattern_store_path,
            detected_frameworks=detected_frameworks,
        )

    matched = match_context(finding.path, context_rule, detected_frameworks)
    if not matched:
        # No context match → use default strategy from the rule
        strategy = context_rule.default_strategy
        _append_text(
            log_file,
            f"contextual-fix: no context match for {finding.path}, using default strategy={strategy}",
        )
        if strategy in ("deterministic", "deterministic_safe"):
            return apply_autofix(
                worktree_path,
                finding,
                log_file,
                pattern_store_path=pattern_store_path,
                detected_frameworks=detected_frameworks,
            )
        if strategy == "llm_with_context":
            prompt = build_contextual_prompt(
                finding,
                ContextOverride(
                    file_patterns=[],
                    framework=context_rule.rule,
                    fix_strategy="llm_with_context",
                    prompt_hint=context_rule.contexts[0].prompt_hint
                    if context_rule.contexts
                    else None,
                ),
            )
            rc, _output, _prompt_file = apply_claude_fix(
                ClaudeFixRequest(
                    worktree_path=worktree_path,
                    finding=finding,
                    baseline_checks=baseline_checks or {},
                    target_checks=target_checks or {},
                    claude_cmd_template=claude_cmd_template or "",
                    max_files_changed=max_files_changed,
                    max_loc_diff=max_loc_diff,
                    log_file=log_file,
                    pattern_store_path=pattern_store_path,
                ),
            )
            return rc == 0
        # skip or unknown
        _append_text(
            log_file, f"contextual-fix: default strategy={strategy} — skipping"
        )
        return False

    _append_text(
        log_file,
        f"contextual-fix: rule={finding.rule} context={matched.framework} strategy={matched.fix_strategy}",
    )

    if matched.fix_strategy in ("deterministic", "deterministic_safe"):
        return apply_autofix(
            worktree_path,
            finding,
            log_file,
            pattern_store_path=pattern_store_path,
            detected_frameworks=detected_frameworks,
        )

    if matched.fix_strategy == "llm_with_context":
        prompt = build_contextual_prompt(finding, matched)
        rc, _output, _prompt_file = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=worktree_path,
                finding=finding,
                baseline_checks=baseline_checks or {},
                target_checks=target_checks or {},
                claude_cmd_template=claude_cmd_template or "",
                max_files_changed=max_files_changed,
                max_loc_diff=max_loc_diff,
                log_file=log_file,
                pattern_store_path=pattern_store_path,
            ),
        )
        return rc == 0

    # skip or unknown
    _append_text(
        log_file, f"contextual-fix: strategy={matched.fix_strategy} — skipping fix"
    )
    return False


def build_contextual_prompt(finding: "Finding", context: "ContextOverride") -> str:
    """Build a Claude prompt with injected context hints for an LLM fix.

    Args:
        finding: The Finding to fix.
        context: Matched context override with framework and prompt hints.

    Returns:
        Formatted prompt string for the Claude fix engine.
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
