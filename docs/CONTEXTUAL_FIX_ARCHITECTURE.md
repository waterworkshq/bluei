# Contextual Fix Engine — Architecture

**Date:** 2026-04-21
**Status:** Approved for implementation
**Scope:** Phase 1-4 of contextual fix capability

---

## 1. Problem

The QA agent's fix pipeline has a binary world: deterministic autofix (ruff --fix) or LLM fix (Claude). Both fail when the fix is **context-dependent** — technically possible but unsafe in certain codebase locations.

**Live evidence from zulip:**
- 1,412 `ruff-c408` findings blocked (dict→{} is unsafe in Django migrations but safe everywhere else)
- 42 `ruff-b007` findings blocked (no ruff fix available, but rename `i→_i` is trivially safe)
- 295 `ruff-b904` findings blocked (raise-from semantics vary by framework context)
- **Total: ~1,750 findings per discovery run stuck with no fix path**

These end up as `needs-human-max-retries-exceeded` or `needs-human-not-fixable`, requiring Sound's intervention.

---

## 2. Architecture Overview

### 2.1 Current Fix Pipeline

```
Finding (safe_to_autofix from catalog)
    ↓
if safe_to_autofix:
    apply_autofix()           # ruff --fix
    → success or fix_failed_verification
else:
    if rule in LLM_FIXABLE:
        apply_claude_fix()    # Claude without context
    else:
        mark needs-human-not-fixable
```

**Gap:** No concept of "this fix is safe in context X but unsafe in context Y."

### 2.2 Target Fix Pipeline

```
Finding discovered
    ↓
classify_finding(finding)  ← enhanced with context rules
    ↓
┌─────────────────┬──────────────────┬──────────────────┬──────────────────┐
│   SIMPLE_FIX    │ CONTEXTUAL_FIX   │ REFACTOR_CLASS   │  CLAUDE_FIX      │
│                 │     (NEW)        │                  │                  │
│ ruff --fix      │ Context engine:  │ Multi-phase      │ Claude single-   │
│ safe applicab.  │ 1. detect ctx    │ split/merge      │ pass (type/      │
│                 │ 2. pick strategy │ (Claude)         │  coverage)       │
│                 │ 3. apply fix     │                  │                  │
│                 │ 4. verify        │                  │                  │
└─────────────────┴──────────────────┴──────────────────┴──────────────────┘
     ↓                    ↓                    ↓                    ↓
  success/fail      success/fail/learn     success/fail        success/fail
```

### 2.2 Core Components

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CONTEXTUAL FIX ENGINE                             │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────────────┐    ┌──────────────────┐    ┌───────────────┐  │
│  │ Context Rule     │    │ Context Matcher  │    │ Fix Strategy  │  │
│  │ Registry         │    │                  │    │ Selector      │  │
│  │                  │    │ - file patterns  │    │               │  │
│  │ rule → contexts  │───→│ - framework detect│───→│ deterministic │  │
│  │ (YAML/Python)    │    │ - code patterns  │    │ llm_with_ctx  │  │
│  │                  │    │ - AST inspection │    │ skip          │  │
│  └──────────────────┘    └──────────────────┘    └───────┬───────┘  │
│                                                           │          │
│  ┌──────────────────┐    ┌──────────────────┐    ┌───────▼───────┐  │
│  │ Context Failure  │←───│ Fix Executor     │←───│ Context-Aware │  │
│  │ Tracker (Learn)  │    │                  │    │ Prompt Builder│  │
│  │                  │    │ - apply fix      │    │               │  │
│  │ - record failures│    │ - verify result  │    │ Inject:       │  │
│  │ - auto-update    │    │ - record outcome │    │  framework    │  │
│  │   rules on repeat│    │                  │    │  safety hints │  │
│  └──────────────────┘    └──────────────────┘    └───────────────┘  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.3 Data Flow

```
Issue discovered (linters.py)
    ↓
classify_finding() checks:
    1. REFACTOR_CLASS_RULES (existing)
    2. CLAUDE_FIX_RULES (existing)
    3. CONTEXT_RULES (NEW) ← matches rule + file path + framework
    4. Fallback to catalog safe_to_autofix (existing)
    ↓
Issue created with:
    - finding_id, rule, path, confidence (existing)
    - context_rule_matched (NEW)
    - context_fix_strategy (NEW)
    - safe_to_autofix (updated based on context, not just catalog)
    ↓
pr-cycle evaluates issues:
    - SIMPLE_FIX → apply_autofix (ruff --fix)
    - CONTEXTUAL_FIX → apply_contextual_fix (NEW)
        → if strategy=deterministic_safe: ruff --fix
        → if strategy=llm_with_context: Claude with context prompt
        → if strategy=skip: mark contextually-blocked
    - REFACTOR_CLASS → refactor-cycle (existing)
    - CLAUDE_FIX → apply_claude_fix (existing)
    ↓
Fix verification (existing):
    - Re-run linter to check finding is closed
    - If closed → resolved_verified, create PR
    - If open → ContextFailure recorded
```

### 2.4 Component Dependencies

```
reforge.py
    ├── RefactorClass enum (add CONTEXTUAL_FIX)
    ├── ContextRule dataclass
    ├── ContextOverride dataclass
    ├── CONTEXT_RULES registry
    ├── classify_finding() (enhanced)
    └── match_context() (NEW)

context_fix.py (NEW module)
    ├── ContextFailure dataclass
    ├── load_context_failures()
    ├── record_context_failure()
    ├── apply_contextual_fix()
    └── build_contextual_prompt()

linters.py
    └── discover_python_linter_findings()
        └── Already updated: JSON output + per-instance applicability
        └── (Phase 4: add context rule check during finding creation)

cli.py
    └── pr-cycle execution path
        └── add apply_contextual_fix() call after apply_autofix returns False

lifecycle.py
    └── build_contextual_prompt()
    └── context failure tracking helpers

state.py
    └── context_failure persistence (append to findings JSONL)
```

### 2.5 Persistence Model

Context rules live in `constants.py` (Python dict) — same pattern as `DETECTOR_CATALOG`.

Context failures are appended to the findings JSONL file as new record type:

```json
{
  "type": "context_failure",
  "rule": "ruff-c408",
  "file_pattern": "**/migrations/*.py",
  "context_detected": "django-migration",
  "fix_strategy_used": "deterministic",
  "failure_reason": "verification-still-fails",
  "count": 3,
  "last_attempt": "2026-04-21T14:47:44+00:00"
}
```

Issue records get two new optional fields:
```json
{
  "context_rule_matched": "ruff-c408.django-migration",
  "context_fix_strategy": "skip"
}
```

### 2.6 Safety Properties

1. **No regression:** SIMPLE_FIX, REFACTOR_CLASS, and CLAUDE_FIX behavior unchanged
2. **Conservative defaults:** Unmatched contexts fall back to existing behavior (catalog-based)
3. **No auto-escalation:** Contextual fix failures record context metadata before escalating
4. **Learning is bounded:** Context rules can only become more restrictive (skip → never less restrictive without human review)

### 2.7 Scope Boundaries

**In scope:**
- Ruff rules on Python repos (initially zulip)
- File-pattern-based context matching
- Deterministic and LLM-based fix strategies
- Context failure tracking and auto-rule-updates

**Out of scope (future phases):**
- AST-based context matching (phase 5+)
- Non-ruff rules (expandable, but not initial focus)
- Per-repo context rule overrides (phase 3+)
- Cross-repo pattern learning (phase 4+)

---

## 3. File Structure

```
qa-agent/core/sandbox_local_runner/
├── reforge.py                  # Add CONTEXTUAL_FIX, ContextRule, ContextOverride
├── context_fix.py              # NEW: Contextual fix engine
├── context_rules.py            # NEW: Context rule registry (or embed in constants.py)
├── linters.py                  # Already updated (JSON output)
├── cli.py                      # Wire apply_contextual_fix into pr-cycle
├── lifecycle.py                # build_contextual_prompt, failure tracking
├── state.py                    # Context failure persistence
├── constants.py                # Add CONTEXT_RULES catalog
└── tests/
    ├── test_contextual_fix.py  # NEW: Contextual fix execution tests
    ├── test_context_rules.py   # NEW: Context rule matching tests
    └── test_context_learning.py # NEW: Learning layer tests
```

---

## 4. Key Interfaces

### 4.1 Public API

```python
# reforge.py
def classify_finding(finding, repo_path=None) -> RefactorClass
def match_context(file_path: str, repo_path: Path, rule: ContextRule) -> ContextOverride | None

# context_fix.py
def apply_contextual_fix(
    repo_path: Path,
    finding: Finding,
    log_file: Path,
    worktree_path: Path,
) -> bool

def build_contextual_prompt(finding: Finding, context: ContextOverride) -> str

def record_context_failure(
    rule: str,
    file_path: str,
    context: str,
    strategy: str,
    reason: str,
    findings_file: Path,
) -> None
```

### 4.2 Entry Points

```bash
# No new CLI commands — contextual fixes run as part of existing pr-cycle
./qa-agent run --repo zulip --phase pr-cycle --no-dry-run

# Optional: one-shot context re-evaluation for existing issues (Phase 4)
./qa-agent run --repo zulip --phase context-reconcile --no-dry-run
```
