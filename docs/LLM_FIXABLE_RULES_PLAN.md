# LLM-Fixable Rules — Implementation Plan

**Date:** 2026-04-01  
**Status:** Pre-implementation  
**Related:** `CHANGELOG.md` v2.6.0, `constants.py`, `cli.py` pr-cycle

---

## Problem

41 "open" issues in zulip are stuck with `safe_to_autofix: False`. The pr-cycle skips them (line 574 of `cli.py`). They accumulate, count against the actionable cap, and block new issue creation.

All 41 are `ruff-b904` (raise without cause) — mundane fixes that an LLM agent can handle with the right prompt.

## Goal

1. **Keep creating issues for ALL findings** — no change to issue-cycle
2. **Route non-autofixable but LLM-fixable issues through the fix engine** — instead of skipping them
3. **Mark truly unfixable issues as `needs-human`** — so they don't block the pipeline
4. **Track LLM-fixable vs deterministic-fixable** in issue metadata

## Architecture

### New Concept: Fix Strategy

Currently, findings have `safe_to_autofix` (boolean). This is too binary.

New model:

| `safe_to_autofix` | In `LLM_FIXABLE_RULES`? | Action |
|---|---|---|
| True | — | Deterministic autofix (current path) |
| False | Yes | Route to LLM fix engine with rule-specific prompt |
| False | No | Mark as `needs-human-not-fixable`, skip |

### New File: `llm_fixable_rules.yaml`

```yaml
# Maps rules that ruff can't autofix but LLM agents can handle
# Each entry provides a prompt hint for the fix engine

rules:
  ruff-b904:
    description: "raise without cause — add 'from' clause to exception re-raises"
    prompt_hint: |
      Fix the bare `raise` statement by adding an appropriate `from` clause:
      - If re-raising the caught exception: use `raise ... from e` (or the caught variable)
      - If intentionally suppressing context: use `raise ... from None`
      - Preserve the original exception message
    complexity: low
    languages: [python]

  ruff-s311:
    description: "stdlib random used in security-sensitive context — replace with secrets"
    prompt_hint: |
      Replace `random` module usage with `secrets` module where security matters.
      Use `secrets.choice()` instead of `random.choice()`, `secrets.randbelow()` instead of `random.randint()`.
      Only replace in security-sensitive contexts (tokens, passwords, keys, IDs).
    complexity: low
    languages: [python]

  # Future additions:
  # ruff-b905:
  #   description: "zip without strict"
  #   prompt_hint: "Add strict=True to zip() calls where iterables should be same length"
  #   complexity: low
```

### Code Changes

#### 1. `constants.py` — Add `LLM_FIXABLE_RULES` loader

```python
import yaml

def load_llm_fixable_rules() -> Dict[str, Dict[str, Any]]:
    """Load LLM-fixable rule definitions from YAML config."""
    rules_file = Path(__file__).parent / 'llm_fixable_rules.yaml'
    if rules_file.exists():
        with open(rules_file) as f:
            data = yaml.safe_load(f)
        return data.get('rules', {})
    return {}
```

#### 2. `cli.py` pr-cycle — Replace `safe_to_autofix` skip with LLM routing

**Current code (line 574-575):**
```python
if not finding.safe_to_autofix:
    continue
```

**New code:**
```python
if not finding.safe_to_autofix:
    if finding.rule in LLM_FIXABLE_RULES:
        # Route to LLM fix engine — don't skip
        pass
    else:
        # Truly not fixable — mark for human if not already
        if issue.get('status') not in NON_ACTIONABLE_ISSUE_STATUSES:
            set_issue_status(issue, 'needs-human-not-fixable',
                f'rule {finding.rule} is not autofixable and not LLM-fixable')
        continue
```

#### 3. `cli.py` pr-cycle — Add prompt hint injection

When an LLM-fixable issue reaches the fix engine, inject the rule's `prompt_hint` into the fix command. This happens around line 689 where `use_claude_engine` is determined:

```python
# Determine fix strategy
if finding.safe_to_autofix:
    use_claude_engine = (args.fix_engine == 'claude' or finding.rule in CLAUDE_REQUIRED_RULES)
    extra_prompt = None
elif finding.rule in LLM_FIXABLE_RULES:
    use_claude_engine = True  # LLM-fixable rules always use LLM engine
    extra_prompt = LLM_FIXABLE_RULES[finding.rule].get('prompt_hint')
else:
    continue  # Already handled above, but defensive
```

Then pass `extra_prompt` to `apply_claude_fix()`.

#### 4. `state.py` — Add `needs-human-not-fixable` to non-actionable statuses

```python
NON_ACTIONABLE_ISSUE_STATUSES = frozenset({
    # ... existing statuses ...
    'needs-human-not-fixable',  # Rule not autofixable and not LLM-fixable
})
```

#### 5. `apply_claude_fix()` — Accept optional prompt augmentation

Add an optional `extra_prompt` parameter to inject rule-specific hints into the Claude/OpenCode command.

### Files to Modify

| File | Change | Risk |
|---|---|---|
| `constants.py` | Add `LLM_FIXABLE_RULES` loader | Low |
| `llm_fixable_rules.yaml` | **NEW** — rule definitions | New file, no risk |
| `cli.py` | Replace `safe_to_autofix` skip with LLM routing | **Medium** — core pr-cycle logic |
| `state.py` | Add `needs-human-not-fixable` to non-actionable | Low |
| `lifecycle.py` | Add `extra_prompt` param to fix functions | Low |
| `CHANGELOG.md` | Document v2.6.0 changes | Documentation |
| `tests/test_llm_fixable_rules.py` | **NEW** — test suite | New file |

### Test Plan

1. **Unit: Rule loading** — Verify `load_llm_fixable_rules()` loads YAML correctly
2. **Unit: Filter logic** — Mock issues with various `safe_to_autofix` + rule combos, verify correct routing
3. **Unit: Status assignment** — Verify `needs-human-not-fixable` is set for truly unfixable issues
4. **Unit: Non-actionable count** — Verify `needs-human-not-fixable` is excluded from actionable cap
5. **Integration: pr-cycle dry run** — Run pr-cycle with --dry-run on zulip, verify LLM-fixable issues are queued
6. **Regression: existing tests** — All 250 existing tests must still pass

---

## Implementation Status: COMPLETE ✅

**Completed:** 2026-04-01

### What was done
- [x] Created `llm_fixable_rules.yaml` with ruff-b904 and ruff-s311
- [x] Added `load_llm_fixable_rules()` to constants.py with caching + fallback
- [x] Added `needs-human-not-fixable` to NON_ACTIONABLE_ISSUE_STATUSES
- [x] Modified pr-cycle filter to route LLM-fixable rules to fix engine
- [x] Added `extra_prompt` parameter to apply_claude_fix()
- [x] Added `count_actionable_issues()` for actionable cap counting
- [x] 11 new tests — all passing
- [x] Updated CHANGELOG.md for v2.6.0

### Files modified
- `core/sandbox_local_runner/llm_fixable_rules.yaml` (NEW)
- `core/sandbox_local_runner/constants.py`
- `core/sandbox_local_runner/state.py`
- `core/sandbox_local_runner/cli.py`
- `core/sandbox_local_runner/lifecycle.py`
- `core/sandbox_local_runner/__init__.py`
- `tests/test_llm_fixable_rules.py` (NEW)
- `CHANGELOG.md`
- `docs/LLM_FIXABLE_RULES_PLAN.md` (this file)

### Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Claude fix engine fails on LLM-fixable rules | Existing max-fix-attempts escalation handles this — will escalate to needs-human after 3 failures |
| Prompt hints are too generic | Start with specific rules (b904, s311), iterate based on results |
| Existing tests break | Run full test suite before merging, fix any that depend on `safe_to_autofix=False` being skipped |
| yaml import not available | Add `pyyaml` to dependencies, or fall back to Python dict constant |
| `apply_claude_fix` signature change | Make `extra_prompt` optional with default `None` |

### Implementation Order

1. Create `llm_fixable_rules.yaml` with initial rules
2. Add `load_llm_fixable_rules()` to `constants.py`
3. Add `needs-human-not-fixable` to `NON_ACTIONABLE_ISSUE_STATUSES` in `state.py`
4. Modify pr-cycle filter in `cli.py` (the surgical change)
5. Add `extra_prompt` parameter to fix functions
6. Write tests (`tests/test_llm_fixable_rules.py`)
7. Run full test suite + fix any regressions
8. Update `CHANGELOG.md` for v2.6.0
9. Update this doc with actual results

### Pivot Triggers

If during implementation:
- **apply_claude_fix can't accept extra_prompt easily** → Use environment variable or temp file to pass hint
- **yaml not available** → Use Python dict constant in constants.py instead
- **Existing tests fail badly** → Add `--feature-flag llm-fixable` to gate the new behavior
- **Claude fix engine times out on b904** → Lower complexity estimate, add timeout handling

---

## Success Criteria

- [ ] 41 stuck "open" issues get processed (routed to LLM engine)
- [ ] New findings with `safe_to_autofix=False` but LLM-fixable rules get queued for fixing
- [ ] Truly unfixable rules get `needs-human-not-fixable` status and don't block cap
- [ ] All 250+ existing tests pass
- [ ] New tests cover the LLM-fixable routing logic
- [ ] CHANGELOG updated
