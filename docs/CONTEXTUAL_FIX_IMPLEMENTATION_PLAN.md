# Contextual Fix Engine — Implementation Plan

**Date:** 2026-04-21
**Author:** Red
**Status:** Ready for delegation

---

## Summary

Four incremental batches, each shippable independently. Each batch adds capability without breaking prior behavior.

| Batch | Phase | What | Effort | Risk |
|-------|-------|------|--------|------|
| 1 | Foundation | Context rule registry + classification | Medium | Low |
| 2 | Execution | Contextual fix pipeline + prompt builder | High | Medium |
| 3 | Learning | Context failure tracker + auto-rule-updates | Medium | Low |
| 4 | Migration | Reconcile existing stuck issues | Low | Low |

---

## Batch 1: Context Rule Registry + Classification

**Goal:** Define context rules and route findings through context-aware classification.

**Acceptance:**
- `RefactorClass.CONTEXTUAL_FIX` exists in enum
- Context rule registry is loadable
- `classify_finding()` returns `CONTEXTUAL_FIX` for context-matched findings
- 50+ unit tests pass
- No regression to existing SIMPLE_FIX/REFACTOR_CLASS/CLAUSE_FIX classification

### 1.1 Add `CONTEXTUAL_FIX` to `RefactorClass` enum

**File:** `core/sandbox_local_runner/reforge.py`

```python
class RefactorClass(str, enum.Enum):
    SIMPLE_FIX = "simple_fix"
    CONTEXTUAL_FIX = "contextual_fix"    # NEW
    REFACTOR_CLASS = "refactor_class"
    CLAUDE_FIX = "claude_fix"
```

### 1.2 Add ContextRule and ContextOverride dataclasses

**File:** `core/sandbox_local_runner/reforge.py` (or new `context_rules.py`)

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class ContextOverride:
    """A specific context where the default fix strategy changes."""
    file_patterns: list[str]         # glob patterns, e.g. ["**/migrations/*.py"]
    framework: str                   # e.g. "django", "any"
    fix_strategy: str                # "deterministic_safe" | "llm_with_context" | "skip"
    prompt_hint: Optional[str] = None

@dataclass
class ContextRule:
    """A rule with context-specific fix behavior."""
    rule: str                        # e.g. "ruff-c408"
    default_strategy: str            # "deterministic" | "llm_with_context" | "skip"
    contexts: list[ContextOverride] = field(default_factory=list)
```

### 1.3 Define initial context rule registry

**File:** `core/sandbox_local_runner/constants.py` (append to existing catalog section)

```python
CONTEXT_RULES: list[dict] = [
    {
        "rule": "ruff-c408",
        "default_strategy": "deterministic",
        "contexts": [
            {
                "file_patterns": ["**/migrations/*.py"],
                "framework": "django",
                "fix_strategy": "skip",
                "prompt_hint": "Django migration files require dict() for runtime model resolution. Do not rewrite."
            },
            {
                "file_patterns": ["**/test_*.py", "**/*_test.py", "tests/**"],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "Using dict() literal in test files is safe."
            }
        ]
    },
    {
        "rule": "ruff-b904",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/middleware*.py"],
                "framework": "django",
                "fix_strategy": "llm_with_context",
                "prompt_hint": "In Django middleware, preserve exception chain semantics. Use 'raise ... from e' but don't break existing error handling contracts."
            }
        ]
    },
    {
        "rule": "ruff-b007",
        "default_strategy": "llm_with_context",
        "contexts": [
            {
                "file_patterns": ["**/fixtures*.py"],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "Rename unused loop variable `i` to `_i`"
            }
        ]
    },
    {
        "rule": "ruff-s311",
        "default_strategy": "skip",
        "contexts": [
            {
                "file_patterns": ["**/test_*.py", "**/*_test.py", "tests/**", "**/fixtures*.py"],
                "framework": "any",
                "fix_strategy": "deterministic_safe",
                "prompt_hint": "Using pseudo-random generators in test files is acceptable for test data generation."
            }
        ]
    }
]
```

### 1.4 Implement context matching and classification

**File:** `core/sandbox_local_runner/reforge.py`

```python
import fnmatch

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
```

### 1.5 Enhance `classify_finding()` with context awareness

**File:** `core/sandbox_local_runner/reforge.py` — update existing `classify_finding`

```python
def classify_finding(finding, repo_path=None) -> RefactorClass:
    rule = getattr(finding, "rule", "") or ""

    # 1. Check existing classification rules
    if rule in REFACTOR_CLASS_RULES:
        return RefactorClass.REFACTOR_CLASS
    if rule in CLAUDE_FIX_RULES:
        return RefactorClass.CLAUSE_FIX

    # 2. Check context rule registry (NEW)
    context_rule = get_context_rule(rule)
    if context_rule:
        matched = match_context(finding.path, context_rule)
        if matched:
            if matched.fix_strategy == "skip":
                return RefactorClass.REFACTOR_CLASS  # Route to human/structural
            if matched.fix_strategy == "llm_with_context":
                return RefactorClass.CONTEXTUAL_FIX
            if matched.fix_strategy == "deterministic_safe":
                return RefactorClass.SIMPLE_FIX  # Override catalog
        # No context match → use default
        if context_rule.default_strategy == "deterministic":
            return RefactorClass.SIMPLE_FIX
        elif context_rule.default_strategy == "llm_with_context":
            return RefactorClass.CONTEXTUAL_FIX
        elif context_rule.default_strategy == "skip":
            return RefactorClass.CLAUSE_FIX

    # 3. Fall back to catalog-based classification
    if finding.safe_to_autofix:
        return RefactorClass.SIMPLE_FIX
    return RefactorClass.CLAUSE_FIX
```

### 1.6 Tests for Batch 1

**File:** `tests/test_context_rules.py`

Test cases:
1. `test_context_rule_c408_django_migration → skip`
2. `test_context_rule_c408_test_file → deterministic_safe`
3. `test_context_rule_c408_app_code → deterministic (default)`
4. `test_context_rule_b904_middleware → llm_with_context`
5. `test_context_rule_b007_fixtures → deterministic_safe`
6. `test_context_rule_s311_test → deterministic_safe`
7. `test_context_rule_s311_app_code → skip (default)`
8. `test_classify_finding_contextual_fix_returned`
9. `test_classify_finding_no_context_rule_fallback_to_catalog`
10. `test_match_context_no_match_returns_none`

### 1.7 Delegation Instructions for Batch 1

```
Task: Implement context rule registry and classification for qa-agent contextual fix engine.

Reference docs:
- Architecture: qa-agent/docs/CONTEXTUAL_FIX_ARCHITECTURE.md
- Design: qa-agent/docs/CONTEXTUAL_FIX_ENGINE_DESIGN.md
- Implementation plan: qa-agent/docs/CONTEXTUAL_FIX_IMPLEMENTATION_PLAN.md (this file, Batch 1 section)

Scope:
1. Add CONTEXTUAL_FIX to RefactorClass enum in reforge.py
2. Add ContextRule and ContextOverride dataclasses in reforge.py
3. Add CONTEXT_RULES catalog to constants.py (4 rules: c408, b904, b007, s311)
4. Implement get_context_rule(), match_context(), _load_context_rules() in reforge.py
5. Enhance classify_finding() to check context rules before falling back to catalog
6. Write 10+ unit tests in tests/test_context_rules.py

Constraints:
- Do NOT modify existing SIMPLE_FIX, REFACTOR_CLASS, or CLAUDE_FIX behavior
- Do NOT modify linters.py, cli.py, or lifecycle.py in this batch
- All existing tests must still pass
- Use the exact dataclass structures and function signatures from the implementation plan

Acceptance:
- 50+ unit tests pass (including existing + new)
- classify_finding() returns CONTEXTUAL_FIX for context-matched findings
- Context rules load correctly from constants.CONTEXT_RULES
- No regression to existing classification
```

---

## Batch 2: Contextual Fix Execution + Prompt Builder

**Goal:** Execute context-aware fixes through the pr-cycle pipeline.

**Acceptance:**
- `apply_contextual_fix()` function works end-to-end
- Context-aware prompts are built correctly for each strategy
- pr-cycle routes CONTEXTUAL_FIX findings to the contextual fix engine
- Integration tests pass on zulip sample findings

### 2.1 Create `context_fix.py` module

**File:** `core/sandbox_local_runner/context_fix.py` (NEW)

```python
"""Contextual fix engine — applies context-aware fixes for CONTEXTUAL_FIX findings."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .reforge import ContextRule, ContextOverride, get_context_rule, match_context
from .models import Finding
from .lifecycle import apply_autofix, apply_claude_fix, run_named_checks
from .constants import PER_REPO_BASELINE_CHECKS
from .state import _append_text


def apply_contextual_fix(
    repo_path: Path,
    finding: Finding,
    log_file: Path,
    worktree_path: Path,
) -> bool:
    """Apply a context-aware fix for a CONTEXTUAL_FIX finding.
    
    Returns True if the fix was applied and verified.
    """
    context_rule = get_context_rule(finding.rule)
    if not context_rule:
        _append_text(log_file, f'contextual-fix: no context rule for {finding.rule}')
        return False
    
    matched = match_context(finding.path, context_rule)
    if not matched:
        # No context match → fall back to deterministic autofix
        _append_text(log_file, f'contextual-fix: no context match for {finding.path}, falling back to deterministic')
        return apply_autofix(worktree_path, finding, log_file)
    
    _append_text(log_file, f'contextual-fix: rule={finding.rule} context={matched.framework} strategy={matched.fix_strategy}')
    
    if matched.fix_strategy == "deterministic_safe":
        return apply_autofix(worktree_path, finding, log_file)
    
    if matched.fix_strategy == "llm_with_context":
        prompt = build_contextual_prompt(finding, matched)
        # Use Claude fix engine with context-injected prompt
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


def build_contextual_prompt(finding: Finding, context: ContextOverride) -> str:
    """Build a Claude prompt with injected context hints."""
    return f"""Fix this {finding.rule} issue in {finding.path}.

## Finding
{finding.snippet}
Line: {finding.line}
Rule: {finding.rule}

## Codebase Context
Framework: {context.framework}
File patterns matched: {context.file_patterns}

## Safety Guidance
{context.prompt_hint or "Apply the fix while respecting codebase conventions."}

## Instructions
1. Read the file to understand the surrounding context
2. Apply the minimal fix that resolves the finding
3. Do NOT change unrelated code
4. Preserve framework-specific patterns and conventions
"""
```

### 2.2 Wire into `cli.py` pr-cycle

**File:** `core/sandbox_local_runner/cli.py` — in the pr-cycle section, after `apply_autofix` returns False:

```python
# Existing code:
applied = apply_autofix(worktree_path, finding, log_file)
if not applied:
    # NEW: Try contextual fix before giving up
    if finding.rule in get_context_rule_rules():  # has a context rule
        applied = apply_contextual_fix(
            repo_path=repo_path,
            finding=finding,
            log_file=log_file,
            worktree_path=worktree_path,
        )
    if not applied:
        run_status = 'fix-noop'
        set_issue_status(issue, 'fix_failed_verification', 'autofix could not modify target pattern')
        # ... rest of existing failure handling
```

Actually, the better integration point is earlier — in the queue candidate selection. If a finding is classified as CONTEXTUAL_FIX, it should be eligible for the queue. Currently, findings with `safe_to_autofix=False` are filtered out. We need to update the eligibility check:

```python
# In cli.py pr-cycle, around the safe_to_autofix check:
if not finding.safe_to_autofix:
    llm_rules = _get_llm_fixable_rules()
    if finding.rule in llm_rules:
        pass  # route to LLM fix
    elif classify_finding(finding) == RefactorClass.CONTEXTUAL_FIX:
        pass  # NEW: route to contextual fix
    else:
        # truly not fixable
        continue
```

### 2.3 Tests for Batch 2

**File:** `tests/test_contextual_fix.py`

Test cases:
1. `test_apply_contextual_fix_deterministic_safe_context`
2. `test_apply_contextual_fix_llm_with_context_context`
3. `test_apply_contextual_fix_skip_strategy`
4. `test_apply_contextual_fix_no_context_rule`
5. `test_build_contextual_prompt_includes_framework`
6. `test_build_contextual_prompt_includes_safety_guidance`
7. `test_pr_cycle_routes_contextual_fix_to_engine`
8. Integration: run on zulip sample finding (c408 in non-migration file)

### 2.4 Delegation Instructions for Batch 2

```
Task: Implement contextual fix execution pipeline for qa-agent.

Prerequisites: Batch 1 must be complete (context rule registry + classification).

Reference docs:
- Architecture: qa-agent/docs/CONTEXTUAL_FIX_ARCHITECTURE.md
- Design: qa-agent/docs/CONTEXTUAL_FIX_ENGINE_DESIGN.md
- Implementation plan: qa-agent/docs/CONTEXTUAL_FIX_IMPLEMENTATION_PLAN.md (this file, Batch 2 section)

Scope:
1. Create context_fix.py with apply_contextual_fix() and build_contextual_prompt()
2. Wire apply_contextual_fix() into cli.py pr-cycle after apply_autofix returns False
3. Update pr-cycle queue eligibility to include CONTEXTUAL_FIX findings
4. Write 8+ unit tests in tests/test_contextual_fix.py

Constraints:
- Reuse existing apply_autofix() and apply_claude_fix() from lifecycle.py
- Do NOT modify the verification or PR creation logic
- All existing tests must still pass
- Use exact function signatures from implementation plan

Acceptance:
- apply_contextual_fix() correctly routes to deterministic or LLM based on context
- Context-aware prompts include framework and safety guidance
- pr-cycle queues CONTEXTUAL_FIX findings for fix attempts
- Integration test passes on zulip sample findings
```

---

## Batch 3: Context Failure Tracking + Learning Layer

**Goal:** Track contextual fix failures and auto-update context rules.

**Acceptance:**
- Context failures are recorded with full metadata
- Repeated failures in same context trigger rule updates
- Rule updates are bounded (only more restrictive)
- Tests verify learning loop behavior

### 3.1 Context Failure Tracking

**File:** `core/sandbox_local_runner/context_fix.py` (append)

```python
from dataclasses import dataclass, field, asdict
import json
from datetime import datetime, timezone

@dataclass
class ContextFailure:
    rule: str
    file_pattern: str
    context_detected: str
    fix_strategy_used: str
    failure_reason: str
    count: int = 1
    last_attempt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ContextFailure":
        return cls(**data)


def record_context_failure(
    rule: str,
    file_path: str,
    context: str,
    strategy: str,
    reason: str,
    findings_file: Path,
) -> None:
    """Record a contextual fix failure to the findings file."""
    failure = ContextFailure(
        rule=rule,
        file_pattern=file_path,
        context_detected=context,
        fix_strategy_used=strategy,
        failure_reason=reason,
        count=1,
        last_attempt=datetime.now(timezone.utc).isoformat(),
    )
    # Append to findings JSONL
    with open(findings_file, "a") as f:
        f.write(json.dumps({"type": "context_failure", **failure.to_dict()}) + "\n")


def load_context_failures(findings_file: Path) -> list[ContextFailure]:
    """Load context failures from findings file."""
    failures = []
    if not findings_file.exists():
        return failures
    with open(findings_file) as f:
        for line in f:
            try:
                record = json.loads(line.strip())
                if record.get("type") == "context_failure":
                    failures.append(ContextFailure.from_dict(record))
            except (json.JSONDecodeError, TypeError):
                continue
    return failures


def update_context_rule_on_repeated_failure(
    rule: str,
    context: str,
    findings_file: Path,
) -> bool:
    """If a fix failed 3+ times in same context, update the rule to skip.
    
    Returns True if rule was updated.
    """
    failures = load_context_failures(findings_file)
    matching = [f for f in failures if f.rule == rule and f.context_detected == context]
    total = sum(f.count for f in matching)
    
    if total >= 3:
        # Update the context rule in-memory (persisted on next restart)
        context_rule = get_context_rule(rule)
        if context_rule:
            for ctx in context_rule.contexts:
                if ctx.framework == context.split("-")[0]:  # e.g. "django" from "django-migration"
                    if ctx.fix_strategy != "skip":
                        ctx.fix_strategy = "skip"
                        return True
    return False
```

### 3.2 Wire into pr-cycle fix failure path

**File:** `core/sandbox_local_runner/cli.py` — after contextual fix fails:

```python
# After apply_contextual_fix returns False:
if not applied:
    # Record context failure
    context_rule = get_context_rule(finding.rule)
    if context_rule:
        matched = match_context(finding.path, context_rule)
        if matched:
            record_context_failure(
                rule=finding.rule,
                file_path=finding.path,
                context=f"{matched.framework}-{matched.file_patterns[0].split('/')[-1]}",
                strategy=matched.fix_strategy,
                reason="contextual-fix-failed-verification",
                findings_file=findings_file,
            )
            # Check if rule should be updated
            update_context_rule_on_repeated_failure(
                rule=finding.rule,
                context=f"{matched.framework}",
                findings_file=findings_file,
            )
```

### 3.3 Tests for Batch 3

**File:** `tests/test_context_learning.py`

Test cases:
1. `test_record_context_failure_appends_to_findings`
2. `test_load_context_failures_parses_records`
3. `test_update_rule_on_repeated_failure_changes_to_skip`
4. `test_update_rule_no_change_if_already_skip`
5. `test_update_rule_no_change_below_threshold`
6. `test_learning_loop_end_to_end`

### 3.4 Delegation Instructions for Batch 3

```
Task: Implement context failure tracking and learning layer for qa-agent.

Prerequisites: Batches 1 and 2 must be complete.

Reference docs:
- Architecture: qa-agent/docs/CONTEXTUAL_FIX_ARCHITECTURE.md
- Design: qa-agent/docs/CONTEXTUAL_FIX_ENGINE_DESIGN.md
- Implementation plan: qa-agent/docs/CONTEXTUAL_FIX_IMPLEMENTATION_PLAN.md (this file, Batch 3 section)

Scope:
1. Add ContextFailure dataclass to context_fix.py
2. Implement record_context_failure(), load_context_failures(), update_context_rule_on_repeated_failure()
3. Wire context failure recording into cli.py pr-cycle after contextual fix fails
4. Write 6+ unit tests in tests/test_context_learning.py

Constraints:
- Failures are appended to existing findings JSONL (new record type)
- Rule updates are bounded: only more restrictive (skip), never less
- All existing tests must still pass

Acceptance:
- Context failures are recorded with full metadata
- Repeated failures (3+) in same context trigger rule update to skip
- Rule updates don't affect already-skip rules
- End-to-end learning loop test passes
```

---

## Batch 4: Migrate Existing Stuck Issues

**Goal:** Re-evaluate the 21 `needs-human-max-retries-exceeded` issues in zulip and reset them to appropriate statuses based on context rules.

**Acceptance:**
- Existing stuck issues are re-classified with context rules
- Issues that are now fixable via context rules are reset to `open`
- Issues that remain unfixable get appropriate `contextually-blocked` status
- A reconciliation script is available for future use

### 4.1 Context Reconciliation Script

**File:** `scripts/context_reconcile.py` (NEW)

```python
"""Reconcile existing stuck issues against context rules.

Usage:
    python3 scripts/context_reconcile.py --repo zulip --issues-file repos/zulip/state/issues.json
"""

import argparse
import json
from pathlib import Path

from core.sandbox_local_runner.reforge import (
    classify_finding,
    get_context_rule,
    match_context,
    RefactorClass,
)
from core.sandbox_local_runner.models import Finding


def reconcile_issues(issues_file: Path, dry_run: bool = True) -> dict:
    """Re-evaluate stuck issues against context rules."""
    with open(issues_file) as f:
        issues_data = json.load(f)
    
    results = {"reset_to_open": 0, "marked_blocked": 0, "unchanged": 0, "details": []}
    
    for issue in issues_data.get("issues", []):
        if issue.get("status") not in (
            "needs-human-max-retries-exceeded",
            "needs-human-not-fixable",
            "fix_failed_verification",
        ):
            continue
        
        # Reconstruct finding from issue
        finding = Finding(
            finding_id=issue.get("finding_id", ""),
            repo=issue.get("repo", ""),
            path=issue.get("path", ""),
            line=issue.get("line", 0),
            rule=issue.get("rule", ""),
            snippet=issue.get("snippet", ""),
            confidence=issue.get("confidence", 0.0),
            safe_to_autofix=issue.get("safe_to_autofix", False),
        )
        
        # Re-classify with context rules
        new_class = classify_finding(finding)
        old_status = issue.get("status")
        
        if new_class in (RefactorClass.SIMPLE_FIX, RefactorClass.CONTEXTUAL_FIX):
            # Now fixable!
            if not dry_run:
                issue["status"] = "open"
                issue["context_rule_matched"] = get_context_rule(finding.rule).rule if get_context_rule(finding.rule) else None
            results["reset_to_open"] += 1
            results["details"].append({
                "issue_id": issue.get("issue_id"),
                "rule": finding.rule,
                "old_status": old_status,
                "new_class": new_class.value,
                "action": "reset_to_open",
            })
        else:
            # Still not fixable — mark as contextually blocked
            if not dry_run:
                issue["status"] = "contextually-blocked"
                context_rule = get_context_rule(finding.rule)
                if context_rule:
                    matched = match_context(finding.path, context_rule)
                    if matched:
                        issue["context_fix_strategy"] = matched.fix_strategy
            results["marked_blocked"] += 1
            results["details"].append({
                "issue_id": issue.get("issue_id"),
                "rule": finding.rule,
                "old_status": old_status,
                "new_class": new_class.value,
                "action": "marked_blocked",
            })
    
    if not dry_run:
        with open(issues_file, "w") as f:
            json.dump(issues_data, f, indent=2, default=str)
    
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issues-file", required=True, type=Path)
    parser.add_argument("--no-dry-run", action="store_true")
    args = parser.parse_args()
    
    results = reconcile_issues(args.issues_file, dry_run=not args.no_dry_run)
    print(json.dumps(results, indent=2))
```

### 4.2 Delegation Instructions for Batch 4

```
Task: Implement context reconciliation for existing stuck issues in qa-agent.

Prerequisites: Batches 1-3 must be complete.

Reference docs:
- Architecture: qa-agent/docs/CONTEXTUAL_FIX_ARCHITECTURE.md
- Design: qa-agent/docs/CONTEXTUAL_FIX_ENGINE_DESIGN.md
- Implementation plan: qa-agent/docs/CONTEXTUAL_FIX_IMPLEMENTATION_PLAN.md (this file, Batch 4 section)

Scope:
1. Create scripts/context_reconcile.py for one-shot reconciliation
2. Run dry-run on zulip issues.json to verify correct re-classification
3. Run live (no-dry-run) after Sound confirms the dry-run output

Constraints:
- Script is non-destructive by default (dry_run=True)
- Issues that become fixable are reset to "open"
- Issues that remain unfixable get "contextually-blocked" status
- Script output shows exact changes before applying them

Acceptance:
- Dry-run shows expected re-classification of 21 stuck issues
- Live run updates issues.json correctly
- Script can be re-run idempotently
```

---

## Rollout Order & Dependencies

```
Batch 1 (Registry + Classification)
    ↓
Batch 2 (Execution + Prompts)  — depends on Batch 1
    ↓
Batch 3 (Learning Layer)        — depends on Batch 2
    ↓
Batch 4 (Migration)             — depends on Batch 1 (classification), runs after all batches
```

Batches can be delegated in parallel if the delegation instructions are self-contained and reference the shared architecture/design docs. However, Batch 2 code depends on Batch 1 types being available, so they should be sequential.

**Recommended order:**
1. Delegate Batch 1 → wait for completion + tests
2. Delegate Batch 2 → wait for completion + tests
3. Delegate Batch 3 → wait for completion + tests
4. Run Batch 4 dry-run → Sound reviews → run live
