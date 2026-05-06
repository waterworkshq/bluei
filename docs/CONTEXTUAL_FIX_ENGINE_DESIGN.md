# Contextual Fix Engine — Design Spec

**Date:** 2026-04-21
**Author:** Red
**Status:** Design v1 — awaiting review

---

## 1. Problem Statement

The QA agent currently fails to autonomously fix a large class of valid findings because they're **context-dependent**, not because they're inherently unfixable.

### Evidence from zulip

| Rule | Count | Old `safe_to_autofix` | ruff's actual `applicability` | Outcome |
|------|-------|----------------------|------------------------------|---------|
| `ruff-c408` | 1,412 | ✅ True (catalog) | `"unsafe"` (Django migrations) | `needs-human-max-retries-exceeded` |
| `ruff-b007` | 42 | ✅ True (catalog) | `null` (no fix offered) | `needs-human-max-retries-exceeded` |
| `ruff-b904` | 295 | ❌ False (catalog) | N/A | `needs-human-not-fixable` |
| `ruff-s311` | 1 | ❌ False (catalog) | N/A | `needs-human-not-fixable` |

**Total: ~1,750 findings per discovery run that could be fixed with context but are currently stuck.**

The fix shipped 2026-04-21 (JSON output + per-instance applicability) prevents *false positives* — it stops the system from wrongly attempting unsafe autofixes. But it doesn't *solve* the underlying issues. It's a guardrail, not a fix engine.

### Root Cause

The current fix pipeline has a binary world:

```
deterministic autofix (ruff --fix)  →  applies or doesn't
        ↓ if it fails
LLM fix (Claude)                    →  applies or doesn't
        ↓ if it fails
human escalation
```

What's missing is the **context-aware middle layer**: understanding WHY a fix fails for specific code patterns in specific contexts, and adapting the fix strategy accordingly.

---

## 2. Design Goals

1. **Autonomous resolution** of all fixable findings without user intervention
2. **Codebase-aware**: the engine must understand framework-specific contexts (Django, Flask, TypeScript, etc.)
3. **Progressive sophistication**: start with simple context rules, evolve toward learned patterns
4. **Backward compatible**: no breaking changes to existing SIMPLE_FIX or REFACTOR_CLASS lanes
5. **Observable**: every fix attempt must carry context metadata so we can learn from failures

---

## 3. Architecture: Three-Tier Fix Engine

```
Finding discovered
    ↓
classify_finding(finding)
    ↓
┌─────────────────────────────────────────────────────────┐
│                  FIX CLASSIFICATION                      │
├──────────────────┬──────────────────┬────────────────────┤
│   SIMPLE_FIX     │  CONTEXTUAL_FIX  │   REFACTOR_CLASS   │
│   (existing)     │     (NEW)        │   (existing)       │
│                  │                  │                    │
│ ruff --fix       │ Context-aware    │ Multi-phase split/ │
│ safe applicab.   │ fix pipeline     │ merge (Claude)     │
│                  │                  │                    │
│ No context check │                  │                    │
│ needed           │                  │                    │
└──────────────────┴──────────────────┴────────────────────┘
```

### New Class: `CONTEXTUAL_FIX`

```python
class RefactorClass(str, enum.Enum):
    SIMPLE_FIX = "simple_fix"
    CONTEXTUAL_FIX = "contextual_fix"    # NEW
    REFACTOR_CLASS = "refactor_class"
    CLAUDE_FIX = "claude_fix"
```

**Definition:** A finding where the fix is *technically possible* but requires codebase-specific context to apply safely. The fix may be:
- A deterministic transformation that's unsafe in certain contexts
- A pattern rewrite that needs framework knowledge
- A style fix that's safe in most places but risky in specific file types

**Examples:**
- `ruff-c408` (`dict()` → `{}`): Safe in application code, unsafe in Django migration files
- `ruff-b904` (raise from err): Safe in application code, needs understanding of exception chains in async handlers
- `ruff-s311` (pseudo-random): Safe in test files, unsafe in security-sensitive code

---

## 4. Context Rule Registry

A structured registry that maps rules to contexts where deterministic autofixes are unsafe, and defines the fix strategy for each context.

```python
@dataclass
class ContextRule:
    """A rule with context-specific fix behavior."""
    
    rule: str                        # e.g. "ruff-c408"
    
    # Default: what to do if no context rule matches
    default_strategy: str            # "deterministic" | "llm" | "skip"
    
    # Context-specific overrides
    contexts: list["ContextOverride"]
    
    # Confidence boost/penalty per context
    confidence_adjustment: float = 0.0

@dataclass
class ContextOverride:
    """A specific context where the default fix strategy changes."""
    
    # How to detect this context
    file_patterns: list[str]         # e.g. ["**/migrations/*.py"]
    code_patterns: list[str]         # e.g. ["from django.db import migrations"]
    framework: str                   # e.g. "django"
    
    # What to do instead of default
    fix_strategy: str                # "llm_with_context" | "deterministic_safe" | "skip"
    
    # Prompt hints for LLM-based fixes
    prompt_hint: str | None = None
    # e.g. "Do not rewrite dict() as {} in Django migration files; the dict() call is required for runtime model resolution"
```

### Initial Registry (derived from live evidence)

```yaml
context_rules:
  - rule: ruff-c408
    default_strategy: deterministic    # ruff --fix works in most places
    contexts:
      - file_patterns: ["**/migrations/*.py"]
        framework: django
        fix_strategy: skip             # NEVER apply in migrations
        prompt_hint: "Django migration files require dict() for runtime model resolution. Do not rewrite."
      - file_patterns: ["**/test_*.py", "**/*_test.py", "tests/**"]
        framework: any
        fix_strategy: deterministic    # Safe in test files
      
  - rule: ruff-b904
    default_strategy: llm_with_context
    contexts:
      - file_patterns: ["**/middleware*.py"]
        framework: django
        fix_strategy: llm_with_context
        prompt_hint: "In Django middleware, preserve exception chain semantics. Use 'raise ... from e' but don't break existing error handling contracts."
      
  - rule: ruff-b007
    default_strategy: llm_with_context
    contexts:
      - file_patterns: ["**/fixtures*.py"]
        framework: any
        fix_strategy: deterministic_safe  # rename i → _i is always safe
        prompt_hint: "Rename unused loop variable to _i"
      
  - rule: ruff-s311
    default_strategy: skip             # Security-sensitive, always LLM
    contexts:
      - file_patterns: ["**/test_*.py", "**/fixtures*.py"]
        framework: any
        fix_strategy: deterministic    # Safe in test/fixtures
        prompt_hint: "Using random in test files is acceptable for test data generation."
```

---

## 5. Fix Pipeline Changes

### 5.1 Classification: `classify_finding` Enhancement

```python
def classify_finding(finding, repo_path=None) -> RefactorClass:
    rule = finding.rule
    
    # 1. Check existing classification rules
    if rule in REFACTOR_CLASS_RULES:
        return RefactorClass.REFACTOR_CLASS
    if rule in CLAUDE_FIX_RULES:
        return RefactorClass.CLAUDE_FIX
    
    # 2. Check context rule registry
    context_rule = get_context_rule(rule)
    if context_rule:
        matched_context = match_context(finding.path, repo_path, context_rule)
        if matched_context:
            if matched_context.fix_strategy == "skip":
                return RefactorClass.REFACTOR_CLASS  # Route to human/structural
            if matched_context.fix_strategy == "llm_with_context":
                return RefactorClass.CONTEXTUAL_FIX
            if matched_context.fix_strategy == "deterministic_safe":
                # Override catalog: this specific context IS safe
                return RefactorClass.SIMPLE_FIX
        
        # No context match → use default
        if context_rule.default_strategy == "deterministic":
            return RefactorClass.SIMPLE_FIX
        elif context_rule.default_strategy == "llm_with_context":
            return RefactorClass.CONTEXTUAL_FIX
        elif context_rule.default_strategy == "skip":
            return RefactorClass.CLADE_FIX
    
    # 3. Fall back to catalog-based classification
    if finding.safe_to_autofix:
        return RefactorClass.SIMPLE_FIX
    return RefactorClass.CLAUSE_FIX
```

### 5.2 Execution: Contextual Fix Engine

```python
def apply_contextual_fix(
    repo_path: Path,
    finding: Finding,
    log_file: Path,
    worktree_path: Path,
) -> bool:
    """Apply a context-aware fix for a CONTEXTUAL_FIX finding.
    
    Strategy:
    1. Check context rule registry for this rule + file
    2. If deterministic_safe context → run ruff --fix
    3. If llm_with_context context → run Claude with injected context prompt
    4. Verify the fix closed the finding
    5. Record outcome for learning
    """
    context_rule = get_context_rule(finding.rule)
    if not context_rule:
        return False
    
    matched = match_context(finding.path, repo_path, context_rule)
    if not matched:
        # No context match → fall back to deterministic
        return apply_autofix(worktree_path, finding, log_file)
    
    if matched.fix_strategy == "deterministic_safe":
        # This context IS safe — run the fix
        return apply_autofix(worktree_path, finding, log_file)
    
    if matched.fix_strategy == "llm_with_context":
        # Use Claude with context-aware prompt
        prompt = build_contextual_prompt(finding, matched)
        return apply_claude_fix(
            repo_path=repo_path,
            finding=finding,
            prompt=prompt,
            worktree_path=worktree_path,
            log_file=log_file,
        )
    
    # skip or unknown
    return False
```

### 5.3 Prompt Construction

```python
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

---

## 6. Learning Layer: Context Failure Tracker

When a contextual fix fails, we record the failure with context metadata so the system learns:

```python
@dataclass
class ContextFailure:
    rule: str
    file_pattern: str
    context_detected: str           # e.g. "django-migration"
    fix_strategy_used: str          # e.g. "llm_with_context"
    failure_reason: str             # e.g. "verification-still-fails"
    count: int
    last_attempt: str               # ISO timestamp

# When a fix fails N times in the same context:
# → Update context rule: change fix_strategy to "skip"
# → Log for human review
# → Add to "known-bad-contexts" registry
```

### Learning Rules

| Condition | Action |
|-----------|--------|
| Fix fails 2+ times in same context + strategy | Change strategy to `skip`, log for review |
| Fix succeeds after context rule override | Boost confidence in that rule/context pair |
| New context discovered (unmatched pattern) | Log as "unknown-context" for registry expansion |
| Same rule fails across N different contexts | Flag rule as "likely-not-autofixable" |

---

## 7. Integration Points

### 7.1 Where This Touches Existing Code

| File | Change |
|------|--------|
| `core/sandbox_local_runner/reforge.py` | Add `CONTEXTUAL_FIX` to enum, add `ContextRule`/`ContextOverride` dataclasses, add `CONTEXTUAL_FIX_RULES` set |
| `core/sandbox_local_runner/constants.py` | Add `CONTEXT_RULES` catalog entry per rule |
| `core/sandbox_local_runner/linters.py` | Already updated (JSON output + per-instance applicability). Add context rule check during finding creation. |
| `core/sandbox_local_runner/cli.py` | Add `apply_contextual_fix` call in pr-cycle after `apply_autofix` returns False |
| `core/sandbox_local_runner/lifecycle.py` | Add `build_contextual_prompt()`, context failure tracking |
| `core/sandbox_local_runner/state.py` | Add `context_failure` tracking to finding records |

### 7.2 Backward Compatibility

- `SIMPLE_FIX` behavior unchanged (only triggers when ruff `applicability: "safe"`)
- `REFACTOR_CLASS` behavior unchanged
- `CLAUSE_FIX` behavior unchanged
- New `CONTEXTUAL_FIX` is a new lane, not a modification of existing lanes
- Existing findings with `safe_to_autofix=True` but unsafe applicability will be re-classified on next discovery run

### 7.3 Existing Issues (Migration Path)

The 21 `needs-human-max-retries-exceeded` issues in zulip are already escalated. To fix them:

1. **Option A (automatic):** Next discovery run re-evaluates the same findings with context rules. If a context rule says "skip", the issue status is updated from `needs-human-max-retries-exceeded` to `contextually-blocked` with a reason.

2. **Option B (manual):** Run a one-shot context re-evaluation script that:
   - Reads existing issues.json
   - Re-classifies each issue against context rules
   - Updates issue status accordingly
   - Resets `needs-human-max-retries-exceeded` → `contextually-blocked` or back to `open` (if now fixable)

---

## 8. Rollout Plan

### Phase 1: Context Rule Registry + Classification
- [ ] Add `CONTEXTUAL_FIX` enum value
- [ ] Create context rule registry (YAML or Python)
- [ ] Add initial rules for ruff-c408, ruff-b904, ruff-b007, ruff-s311
- [ ] Update `classify_finding()` to check context rules
- [ ] Tests: classification with context rules

### Phase 2: Contextual Fix Execution
- [ ] Add `apply_contextual_fix()` function
- [ ] Add `build_contextual_prompt()` function
- [ ] Wire into pr-cycle after deterministic autofix fails
- [ ] Tests: contextual fix execution on zulip samples

### Phase 3: Learning Layer
- [ ] Add `ContextFailure` tracking
- [ ] Auto-update context rules on repeated failures
- [ ] Tests: learning loop behavior

### Phase 4: Auto-Migration of Existing Issues
- [ ] Context re-evaluation script for existing issues
- [ ] Status migration: `needs-human-max-retries-exceeded` → appropriate status
- [ ] Tests: migration correctness

---

## 9. Success Metrics

| Metric | Before | Target |
|--------|--------|--------|
| Findings stuck in `needs-human-max-retries-exceeded` | 21 | 0 |
| Findings stuck in `needs-human-not-fixable` | 0 (but many marked unsafe) | Resolved via context routing |
| PRs created per cycle (zulip) | 0 | >0 (fixable findings get PRs) |
| Human escalations per cycle | 21+ | <5 (only truly unfixable) |
| Context rule accuracy | N/A | >90% (measured by fix success rate) |

---

## 10. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Context rules are incomplete | Learning layer auto-discovers new contexts |
| LLM fixes are too slow/expensive | Only LLM for `llm_with_context` strategy; deterministic where possible |
| Context matching false positives | Start conservative — prefer `skip` over wrong `fix` |
| Registry becomes stale | Automated context discovery from fix failures |
| Breaking existing SIMPLE_FIX lane | New lane, not modification of existing lane |

---

## 11. Open Questions

1. **Should context rules be file-based or code-content-based?**
   - File patterns are cheap but imprecise
   - Code content (AST analysis) is precise but expensive
   - Start with file patterns, evolve to content matching

2. **Should context rules be per-repo or global?**
   - Global rules cover common patterns (Django, Flask)
   - Per-repo rules for custom conventions
   - Start global, add per-repo overrides as needed

3. **How do we bootstrap the initial context registry?**
   - Use live evidence from zulip (the 21 failed issues)
   - Manual review of top failing rules
   - Automated analysis of ruff's fix applicability across repos

4. **What about non-ruff rules?**
   - The context engine is rule-agnostic
   - Any rule with context-dependent fixability can use it
   - Start with ruff (proven need), expand as needed
