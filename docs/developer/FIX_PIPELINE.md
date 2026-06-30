<!-- CATEGORY: developer -->

# Fix Pipeline

## Overview

The fix pipeline is the core value engine of bluei — it takes a discovery finding (lint violation, bug pattern, coverage gap) and carries it through classification, routing, tiered fix application, validation, and PR creation. It lives primarily in `bluei/engine/` and is invoked by the `pr-cycle` execution path in `cli.py`.

The pipeline is designed as a **cost-ordered cascade**: deterministic fixes (linter auto-fixes, YAML recipes, pattern replay, AST transforms) are tried before falling through to LLM-based fixes. Every fix is gated by tiered validation that grows stricter with risk — syntax-only checks for trivial fixes, full regression detection for structural changes.

The pipeline integrates with the larger bluei lifecycle: findings flow from [discovery](ARCHITECTURE.md) → **[this pipeline]** → [PR review](REVIEW_LIFECYCLE.md). Rule definitions, context overrides, and recipe YAML are covered in [RULES_REFERENCE.md](../reference/RULES_REFERENCE.md). Configuration knobs are documented in [CONFIG_REFERENCE.md](../reference/CONFIG_REFERENCE.md).

## Pipeline Walkthrough

```
┌────────────────────────────────────────────────────────────────────┐
│                        Fix Pipeline Flow                           │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  Finding ──► classify ──► route ──► tier ──► validate ──► verify   │
│              (reforge)    (cli)    (tiers)   (tiered)    (lifecycle)│
│                  │           │                  │            │      │
│                  ▼           ▼                  ▼            ▼      │
│              RefactorClass  ┌─────────────┐  FixTier     verify_   │
│              enum           │ Deterministic│  T0-T4      fix_closed│
│                             │ Cascade      │                       │
│                             │ Recipe Engine│                       │
│                             │ Contextual   │                       │
│                             │ Fix Engine   │                       │
│                             │ LLM (Claude) │                       │
│                             └─────────────┘                        │
│                                                                    │
│  verify ──► pattern ──► commit ──► push ──► PR                     │
│             extract    (git)     (gh)     (GitHub)                 │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

| Step | Module | What Happens |
|------|--------|--------------|
| **classify** | `reforge.py` | `classify_finding()` assigns a `RefactorClass` based on rule sets, context rules, and recipe availability |
| **route** | `cli.py` pr-cycle loop | Routes to deterministic cascade (CASCADE_FIX), direct LLM (CLAUDE_FIX/CLAIM_FIX), human review (REFACTOR_CLASS), or contextual engine (CONTEXTUAL_FIX) |
| **tier** | `fix_tiers.py` | `resolve_tier()` determines per-finding validation strictness (T0-T4) from recipe safety labels, per-rule overrides, or config |
| **apply** | `cascade.py`, `recipe_engine.py`, `context_fix.py`, `lifecycle.py` | The selected fix engine attempts the fix in an isolated git worktree |
| **validate** | `fix_tiers.py`, `lifecycle.py` | `TieredValidator.validate()` runs tier-appropriate checks; auto-rollback on failure |
| **verify** | `lifecycle.py` | `verify_fix_closed()` re-scans the worktree to confirm the finding is resolved |
| **extract** | `pattern_extractor.py` | Successful fixes are mined for reusable patterns stored in the pattern store |
| **commit** | `lifecycle.py` | `git_commit_all()` stages and commits with the finding-aware message |
| **push + PR** | `cli.py` pr-cycle loop | Branch is pushed; GitHub PR is created or updated via `gh.py` |

## Classification & Routing

### RefactorClass Enum

Defined in `reforge.py`. Five fix classes determine the execution path:

| Class | Value | Meaning | Engine Used |
|-------|-------|---------|-------------|
| `SIMPLE_FIX` | `simple_fix` | Deterministic single-file edit; no LLM needed | Recipe engine or hardcoded autofix |
| `CONTEXTUAL_FIX` | `contextual_fix` | Context-dependent fix requiring codebase awareness | Contextual fix engine (`deterministic_safe` or `llm_with_context`) |
| `REFACTOR_CLASS` | `refactor_class` | Structural change (split/merge, complexity reduction) | Human review queue; bypasses automatic fix |
| `CLAUDE_FIX` | `claude_fix` | Rule requiring LLM judgment (test coverage, type safety) | Claude fix engine, single-pass |
| `CASCADE_FIX` | `cascade_fix` | Multi-engine cascade: deterministic first, LLM fallback | 9-stage deterministic cascade → Claude |

### classify_finding() Resolution Order

`classify_finding()` in `reforge.py` uses this precedence (first match wins):

```
1. REFACTOR_CLASS_RULES   ──► REFACTOR_CLASS  (human review)
2. CONTEXT_RULES match     ──► SIMPLE_FIX / CONTEXTUAL_FIX / REFACTOR_CLASS (per context)
3. CONTEXT_RULES default   ──► SIMPLE_FIX / CONTEXTUAL_FIX / CLAUDE_FIX  (per default_strategy)
4. CLAUDE_FIX_RULES        ──► CLAUDE_FIX       (always LLM)
5. Recipe exists            ──► SIMPLE_FIX       (deterministic YAML)
6. ruff-* + safe_to_autofix ──► SIMPLE_FIX       (linter auto-fix)
7. Default                  ──► CASCADE_FIX      (try 9 stages, fall to LLM)
```

### Context Rules Override Routing

`CONTEXT_RULES` in `constants.py` is a list of per-rule configurations that override default routing based on **file path patterns** and **detected frameworks**. Each rule has a `default_strategy` and a list of `contexts` with file-specific overrides.

**Context rule structure:**

```python
{
    "rule": "ruff-b904",
    "default_strategy": "llm_with_context",
    "contexts": [
        {
            "file_patterns": ["**/middleware*.py"],
            "framework": "django",
            "fix_strategy": "llm_with_context",
            "prompt_hint": "..."
        }
    ]
}
```

**Strategy behaviors:**

| Strategy | Maps To | Effect |
|----------|---------|--------|
| `deterministic` / `deterministic_safe` | `SIMPLE_FIX` | Apply ruff autofix or recipe directly |
| `llm_with_context` | `CONTEXTUAL_FIX` | Run deterministic cascade first, fall to Claude with injected prompt hints |
| `skip` (matched context) | `REFACTOR_CLASS` | Route to human review; no automated fix |
| `skip` (default, no match) | `CLAUDE_FIX` | No context matched — fall to LLM |

**Key context rules and their overrides:**

| Rule | Default | Notable Context Overrides |
|------|---------|--------------------------|
| `ruff-b904` | `llm_with_context` | Django middleware: keep `llm_with_context` with exception-chain guidance |
| `ruff-c408` | `deterministic` | Django migrations: `skip` (dict() required for runtime resolution) |
| `ruff-e501` | `deterministic_safe` | Django migrations: `skip`; URL/routing files: `skip` |
| `ruff-s311` | `skip` | Test files: `deterministic_safe` (pseudo-random OK in tests) |
| `broad-except` | `llm_with_context` | Task queue workers: `skip`; Django middleware: `skip`; Test files: `skip` |
| `type-explicit-any` | `llm_with_context` | `.d.ts`/types: `skip`; Test files: `deterministic_safe` |
| `hardcoded-tmp-path` | `deterministic_safe` | Test files: `skip`; Scripts: `llm_with_context` |

The full context rule registry contains 28 rules with file-pattern + framework combinations. See [RULES_REFERENCE.md](../reference/RULES_REFERENCE.md) for the complete table.

## Fix Tiers (T0-T4)

Defined in `fix_tiers.py`. Tiers control validation strictness — higher tiers mean more rigorous checks and greater resistance to unsafe changes.

### Tier Enumeration

| Tier | Name | Validation Scope |
|------|------|-----------------|
| `T0_GUARANTEED` | Guaranteed safe | Syntax check only. Verify file exists and compiles/parses. |
| `T1_IDEMPOTENT` | Idempotent | Run baseline checks; all must pass with exit code 0. |
| `T2_VALIDATED` | Validated | Full validation gate: baseline → post-fix comparison, regression detection, target check verification. |
| `T3_CONTEXTUAL` | Contextual | T2 + automated diff review: single-file enforcement, critical-file blacklist, line-addition/deletion limits. |
| `T4_STRUCTURAL` | Structural | T3 + mandatory human review flag. Fix is applied but not auto-committed. |

### Safety-to-Tier Mapping

Recipe `safety` labels map to tiers via `SAFETY_TO_TIER`:

| Recipe Safety | FixTier |
|---------------|---------|
| `guaranteed` | `T0_GUARANTEED` |
| `idempotent` | `T1_IDEMPOTENT` |
| `needs_validation` | `T2_VALIDATED` |
| `needs_review` | `T3_CONTEXTUAL` |
| `requires_human` | `T4_STRUCTURAL` |

### Per-Rule Tier Overrides

`TIER_OVERRIDES` hardcodes tiers for specific rules, taking precedence over recipe safety labels:

| Rule | Override Tier | Rationale |
|------|:------------:|-----------|
| `ruff-e501` | T0 | Line-length formatting is always syntactically safe |
| `ruff-c408` | T1 | Dict comprehension rewrites are idempotent |
| `ruff-b007` | T1 | Unused loop variable removal is idempotent |
| `ruff-b904` | T3 | Bare `raise` fix requires exception-chain context |
| `ruff-s311` | T3 | Security-sensitive; needs diff review |
| `broad-except` | T3 | Exception narrowing requires context awareness |
| `xo-max-lines` | T4 | File splitting is structural; needs human review |
| `xo-complexity` | T4 | Complexity reduction is structural |

### Tier Resolution Order

`resolve_tier()` checks in this sequence (first match wins):

1. Recipe `safety` label (if a recipe was matched)
2. Config overrides (`config.yaml` → rule → tier string)
3. `TIER_OVERRIDES` per-rule mapping
4. Default: `T2_VALIDATED`

### Auto-Escalation

`check_tier_escalation()` bumps a rule's tier when it accumulates **3 or more** failed fix attempts in the `finding_activity` state. This prevents infinite retry loops on hopeless rules:

```
T0 ──3 failures──► T1 ──3 failures──► T2 ──3 failures──► T3 ──3 failures──► T4
```

At T4, escalation stops — further failures trigger human review routing.

### Safety Limits

| Limit | Value | Applied At |
|-------|-------|-----------|
| `T0_MAX_LINES` | 5,000 | T0: files larger than this skip syntax check |
| `T3_MAX_ADDITIONS` | 50 | T3: diff review rejects >50 added lines |
| `T3_MAX_DELETIONS` | 30 | T3: diff review rejects >30 deleted lines |
| `CRITICAL_FILE_PATTERNS` | lock files, Dockerfile, CI configs, .env, Makefile | T3: diff review rejects any modification to critical files |
| `LARGE_FILE_SAFETY_LIMIT` | 5,000 | `can_auto_refactor()`: files over this line count → human review |

## Deterministic Cascade

The cascade (`cascade.py`) is a **9-stage pipeline** that tries progressively more sophisticated deterministic fix engines before falling through to the LLM. It is managed by `DeterministicCascade` and invoked via `apply_cascade_fix()` in `lifecycle.py`.

### Cascade Stages (in execution order)

| # | Stage | Name | Engine | Applies To |
|---|-------|------|--------|-----------|
| 1 | `ruff-fix` | `ruff check --fix --unsafe-fixes` | Linter | Python files, `ruff-*` rules |
| 2 | `ruff-format` | `ruff format` | Linter | Python files, `ruff-e501` only |
| 3 | `pyupgrade` | `pyupgrade --py311-plus` | Linter | Python files, `ruff-UP*` rules |
| 4 | `autoflake` | `autoflake --in-place --remove-all-unused-imports` | Linter | Python files, `ruff-F401` only |
| 5 | `isort` | `isort --profile black` | Linter | Python files, `ruff-I001` only |
| 6 | `recipe-engine` | Recipe match + handler dispatch | Recipe | Any language, rule-matched recipes |
| 7 | `pattern-replay` | Previously-learned fix replay | Pattern Store | Any language, ≥85% confidence patterns |
| 8 | `composite-pattern` | Multi-file composite fix | Pattern Store | Any language, ≥70% confidence patterns |
| 9 | `ast-transform` | AST-based code transform | AST Engine | Python only, registered transforms |

### First-Success-Wins Semantics

`DeterministicCascade.execute()` iterates stages in order. For each stage:

1. **`can_handle()`** — checks language match, rule applicability, binary availability, and pattern existence
2. **`attempt()`** — applies the fix
3. If `result.success == True`:
   - If `result.validation_passed == False` → **rollback** (`git checkout` the file), mark stage as failed
   - If `result.validation_passed == True` → **return immediately** with the successful result
4. If `result.success == False` → mark stage as failed, continue to next

If all stages fail, the cascade returns `"cascade_exhausted"`. The caller (`apply_cascade_fix`) then falls through to `apply_claude_fix()` if `allow_llm=True`.

### Cascade Telemetry

Each cascade execution writes structured JSON telemetry (`CascadeTelemetry`) to the log file, recording: stages tried, skipped, failed, total latency, and final outcome. This feeds into the [health score](ARCHITECTURE.md) and [auto-tuning](../reference/CONFIG_REFERENCE.md).

### CascadeContext

The `CascadeContext` dataclass bundles configuration shared across all stages:

| Field | Purpose |
|-------|---------|
| `worktree` | Git worktree path where edits are applied |
| `repo_path` | Original repository root |
| `log_file` | Append-only log for structured output |
| `language` | Detected language (`python`, `typescript`, `go`, `rust`) |
| `baseline_results` | Pre-computed baseline check results |
| `pattern_store` | `FixPatternStore` for pattern replay stages |
| `recipe_engine` | `RecipeEngine` singleton for recipe lookups |
| `max_stages` | Cap on stages attempted (default: 10) |
| `allow_llm` | Whether to fall through to Claude after exhaustion |
| `shared_library` | Cross-repo `SharedPatternLibrary` for pattern promotion |
| `replay_threshold_overrides` | Per-rule confidence thresholds from auto-tuning |

## Recipe Engine

The recipe engine (`recipe_engine.py`) provides declarative, YAML-defined fix recipes. It is the most cost-efficient deterministic fix path.

### Recipe Structure

Recipes are YAML files loaded from `recipes/built-in/` (shipped with bluei) and `recipes/staged/` (user-defined). The `Recipe` dataclass (`recipe_schema.py`) has these sections:

| Section | Key Fields | Purpose |
|---------|-----------|---------|
| **Identity** | `id`, `rule`, `language`, `description`, `priority` | What this recipe fixes and its selection priority |
| **Match** | `type`, `pattern`, `scope`, `context_guard` | How to locate the issue in source files |
| **Replacement** | `type`, `value`/`pattern`/`command`/`template_file` | What fix to apply |
| **Validation** | `safety` | Maps to `FixTier` via `SAFETY_TO_TIER` |

### Handler Types

The replacement `type` field dispatches to one of four handlers in `recipe_handlers.py`:

| Type | Handler | What It Does |
|------|---------|-------------|
| `text` | `TextHandler` | Literal string find-and-replace |
| `regex_substitute` | `RegexSubstituteHandler` | Regex pattern substitution with backreferences |
| `command` | `CommandHandler` | Shell command execution (e.g., `sed`, formatters) |
| `scaffold` | `ScaffoldHandler` | Template-file-based code generation |

### Recipe Matching

`RecipeEngine.match()` scores candidate recipes:

- **+10 base** for language compatibility
- **+50** for exact rule match
- **+30** for wildcard prefix match (e.g., `ruff-*` matches `ruff-F401`)
- **+100 per priority level** from recipe metadata
- **Reject** if `context_guard.excludes_pattern` matches the file content

The highest-scoring recipe wins.

### Recipe Loading

```
builtin_recipe_dir()  →  bluei/engine/recipes/built-in/*.yaml
staged_recipe_dir()   →  bluei/engine/recipes/staged/*.yaml
```

The `RecipeEngine` singleton (`_recipe_engine` in `lifecycle.py`) loads from both directories. Patterns promoted from the shared pattern library are written into `staged/` and trigger a `reload()`.

See [RULES_REFERENCE.md](../reference/RULES_REFERENCE.md#recipes) for the complete list of 16 built-in recipes.

## Contextual Fix Engine

The contextual fix engine (`context_fix.py`) handles findings classified as `CONTEXTUAL_FIX` — rules where the right fix strategy depends on the specific file and framework context.

### Strategy Selection

`apply_contextual_fix()` looks up the context rule for the finding, then tries to match the file path against context patterns:

| Match Result | Strategy | Behavior |
|-------------|----------|----------|
| Context matched → `deterministic_safe` | Ruff autofix | `apply_autofix()` directly |
| Context matched → `llm_with_context` | Claude + context | `apply_claude_fix()` with `build_contextual_prompt()` injecting framework hints |
| Context matched → `skip` | No fix | Finding marked unresolved; may escalate to human review |
| No context match → default `deterministic` | Ruff autofix | `apply_autofix()` |
| No context match → default `llm_with_context` | Claude + context | `apply_claude_fix()` with context rule's `prompt_hint` |
| No context match → default `skip` | No fix | Skip |
| No context rule at all | Fallback autofix | `apply_autofix()` |

### Failure Tracking & Auto-Skip

The engine tracks fix failures per **rule + file_path + framework** triple in `state/context_failures.jsonl`. After **3 failures** (`_CONTEXT_FAILURE_SKIP_THRESHOLD`) for the same combination:

1. `should_skip_due_to_failures()` returns True — subsequent attempts are skipped
2. `update_context_rule_on_repeated_failure()` mutates the in-memory context rule's strategy to `"skip"` for the rest of the process lifetime

This is an **in-memory mutation only** — restarting the process resets to the defaults in `CONTEXT_RULES`.

### Contextual Prompt Injection

`build_contextual_prompt()` constructs a Claude prompt that includes:

- Finding metadata (rule, path, line, snippet)
- **Codebase context** (framework name, matched file patterns)
- **Safety guidance** (prompt_hint from the matched context override)
- Standard instructions: read surrounding context, apply minimal fix, preserve framework conventions

## LLM Fix (Claude/OpenCode)

The LLM fix engine (`apply_claude_fix()` in `lifecycle.py`) is the most powerful but most expensive fix path. It is invoked when all deterministic stages are exhausted or when a rule is explicitly marked as LLM-required.

### Directive Seeding

Before invoking the LLM, `apply_claude_fix()` loads context from multiple sources to improve fix quality:

| Source | Loaded By | Injected As |
|--------|----------|-------------|
| `LESSONS_LOG.md` | `load_lessons_for_finding()` | **Prior context** — recent fix history for this specific finding |
| `LESSONS_LOG.md` | `load_lessons_for_rule(limit=5)` | **Similar fixes** — cross-finding pattern of successes/failures for this rule |
| `LESSONS_LOG.md` | `load_failure_clusters_for_rule()` | **Failure patterns** — clustered failure modes for this rule |
| `findings.jsonl` | `load_finding_record()` | **Fix history** — attempts count, last error |
| LLM-fixable rules YAML | `_get_llm_fixable_rules()` | **Rule-specific guidance** — `prompt_hint` from `llm_fixable_rules.yaml` |
| Pattern store | `format_pattern_hint()` | **Learned fix patterns** — mid-confidence pattern replay hints |
| Mnemo (Phase 2) | `_should_use_mnemo()` | **Prior context from memory** — long-term codebase memory (reserved) |

### Prompt Structure

`render_claude_fix_prompt()` in `prompts.py` builds prompts with 10 ordered sections:

```
Section 1: Finding metadata    (rule, file, line, confidence)
Section 2: Snippet             (offending code)
Section 3: Constraints         (max_files_changed, max_loc_diff, no unrelated edits)
Section 4: Validation context  (baseline checks + rule-target checks)
Section 5: Prior context from memory (Mnemo directives, if available)
Section 6: Prior context       (LESSONS_LOG fix_history, rule_history, learned patterns, failure clusters)
Section 6e: Authoritative guidelines (ADR-0017 — PEP 8, OWASP, TS conventions matched by rule family)
Section 6f: Repo taste         (alpha.5 — framework conventions matched by RepoConfig.framework)
Section 7: Fix history         (attempts, last error — only if retrying)
```

> **Note:** Sections 6e and 6f are prompt-context channels (NOT governed assets).
> The four specialized renderers below (xo-max-lines, xo-complexity, test-coverage-*,
> type-missing-*) do NOT receive any Section 6 context channels — threading taste/guidelines
> into them is a tracked patch-level deferral.

### Specialized Prompts

For certain rules, `render_claude_fix_prompt()` delegates to specialized renderers:

| Rule(s) | Renderer | Special Content |
|---------|----------|----------------|
| `xo-max-lines` | `render_maxlines_refactor_prompt()` | TDD split process: run tests → analyze → split → re-test |
| `xo-complexity` | `render_complexity_refactor_prompt()` | 5 refactoring strategies (extract method, early returns, etc.) |
| `test-coverage-*` | `render_test_coverage_prompt()` | Test writing guidelines, existing pattern adherence |
| `type-missing-*` | `render_type_safety_prompt()` | Type annotation guidelines, generic usage rules |

### Execution

The LLM is invoked via a configurable shell command template (default: `claude --print "Read {prompt_file}..."`). The prompt is written to `.qa-fix-prompt.md` in the worktree, the command is executed via `subprocess.run()` with a 300-second timeout, and the prompt file is cleaned up afterward.

### Post-Fix Recording

After the LLM invocation:

- **Success**: `update_finding_record()` marks `fix_success: True`; `append_lesson()` records the success in `LESSONS_LOG.md`; `_extract_fix_pattern()` mines the diff for reusable patterns
- **Failure**: `increment_fix_attempt()` bumps the attempt counter; `append_lesson()` records the error in `LESSONS_LOG.md`

### Cooldown & Cost Controls

- **Cooldown**: `DEFAULT_FINDING_COOLDOWN_SECONDS` (4 hours) prevents re-fixing the same finding in rapid succession
- **Cost limit**: `cost_tracker.exceeded_limit()` gates Claude invocations — once the hard dollar limit is reached for a cycle, LLM fixes are skipped
- **Max attempts**: `max_fix_attempts_per_issue` (configurable) escalates findings to `needs-human-max-retries-exceeded` status
- **Consecutive failure gate**: `check_finding_escalation_before_fix()` detects repeated failures and escalates before even attempting

## Validation Gates

Validation is the safety backbone of the pipeline. Every fix, regardless of engine, passes through tiered validation before being accepted.

### run_validation_gate()

`run_validation_gate()` in `lifecycle.py` is the central gate. It runs three check phases:

1. **Baseline** — run named checks on the original repo (or reuse pre-computed results)
2. **Post-fix** — run the same checks on the worktree with the fix applied
3. **Target** — run rule-specific checks that must pass for this particular fix

**Regression detection**: a check that passed at baseline (rc=0) but fails post-fix (rc≠0) is a regression → gate fails.

**Target failure**: any target check with rc≠0 → gate fails.

`run_named_checks()` executes an arbitrary dict of `{name: [command]}` mappings, capturing exit codes, stdout, and SHA-256 fingerprints for deterministic comparison.

### TieredValidator

`TieredValidator.validate()` dispatches to tier-specific validation methods:

| Tier | Validation Method | What It Checks |
|------|------------------|----------------|
| T0 | `_validate_t0()` | File existence + syntax/compile check |
| T1 | `_validate_t1()` | All baseline checks must return rc=0 |
| T2 | `_validate_t2()` | Full `run_validation_gate()` with regression + target checks |
| T3 | `_validate_t3()` | T2 + `_auto_diff_review()` (file count, critical files, line limits) |
| T4 | `_validate_t4()` | T3 + sets `requires_human_review=True` for mandatory queue routing |

### Auto-Rollback

`_tier_validate()` in `lifecycle.py` wraps `TieredValidator.validate()` with automatic rollback:

1. Run validator
2. If `passed == False` → `git checkout -- <file>` to revert changes → return False
3. If `requires_human_review == True` → route to human review queue via `route_to_human_review()` → return False
4. Otherwise → return True

### verify_fix_closed()

After validation passes, `verify_fix_closed()` re-runs the discovery engine (`discover_findings()`) on the worktree and confirms the finding no longer fires. This is the final acceptance gate — a fix is only considered "closed" if the linter/analyzer no longer reports the finding.

### Diff Review (T3+)

`_auto_diff_review()` inspects the `git diff --stat` and `git diff --numstat` output; it rejects fixes that:

- Modify more than 1 file
- Touch any file matching `CRITICAL_FILE_PATTERNS` (lock files, Dockerfile, CI configs, .env, Makefile)
- Add more than `T3_MAX_ADDITIONS` (50) lines
- Delete more than `T3_MAX_DELETIONS` (30) lines

## End-to-End Example

### Concrete Walkthrough: `ruff-b904` Finding

A `ruff-b904` (bare `raise` without `from` clause) finding is discovered in a Django middleware file.

```
1. DISCOVERY
   ruff check → finding_id: abc12345, rule: ruff-b904, path: src/middleware.py, line: 42
   Snippet: "except ValueError: raise AppError('bad value')"

2. CLASSIFY (reforge.py)
   rule "ruff-b904" is not in REFACTOR_CLASS_RULES or CLAUDE_FIX_RULES
   CONTEXT_RULES has entry for "ruff-b904":
     default_strategy: "llm_with_context"
     contexts: [{file_patterns: ["**/middleware*.py"], framework: "django", fix_strategy: "llm_with_context"}]
   File "src/middleware.py" matches "**/middleware*.py" → strategy: "llm_with_context"
   → RefactorClass.CONTEXTUAL_FIX

3. TIER (fix_tiers.py)
   resolve_tier(): no recipe safety label, no config override
   TIER_OVERRIDES["ruff-b904"] → FixTier.T3_CONTEXTUAL

4. ROUTE (cli.py pr-cycle)
   classify_finding() returns CONTEXTUAL_FIX → routes to contextual fix engine
   (If it had returned CASCADE_FIX, it would go through the 9 cascade stages)

5. FIX (context_fix.py)
   apply_contextual_fix():
     matched context: framework="django", strategy="llm_with_context"
     build_contextual_prompt() injects prompt_hint:
       "In Django middleware, preserve exception chain semantics.
        Use 'raise ... from e' but don't break existing error handling contracts."
     apply_claude_fix() invoked with the contextual prompt
     Claude reads middleware.py, adds "from e" clause:
       "except ValueError as e: raise AppError('bad value') from e"

6. VALIDATE (fix_tiers.py)
   TieredValidator.validate(tier=T3):
     T2: run_validation_gate() → baseline checks pass, no regressions, target checks pass
     T3: _auto_diff_review() → 1 file changed, not critical, +1 line added, 0 deleted → PASS

7. VERIFY (lifecycle.py)
   verify_fix_closed(): re-scan worktree → ruff-b904 no longer fires → FIX IS CLOSED

8. EXTRACT (pattern_extractor.py)
   _extract_fix_pattern(): diffs are mined → pattern learned for future ruff-b904 fixes

9. COMMIT + PR (lifecycle.py + gh.py)
   git add -A, git commit -m "fix: add exception cause in middleware (ruff-b904)"
   git push, GitHub PR created with link to issue
```

### Same Finding, Different Context

If the same `ruff-b904` finding were in `src/utils.py` (not a middleware file):

- Context match fails → uses `default_strategy: "llm_with_context"`
- Still gets Claude fix, but without the middleware-specific `prompt_hint`
- Still resolves at T3

If the finding were in a file with no CONTEXT_RULES entry at all:

- Falls through to CLAUDE_FIX_RULES check → not in that set
- Recipe check → no recipe for `ruff-b904`
- `ruff-*` prefix, but `safe_to_autofix=False` → not SIMPLE_FIX
- Default → `CASCADE_FIX` → 9 cascade stages → exhausted → falls to `apply_claude_fix()`

## Module Index

| Module | File | Responsibility |
|--------|------|----------------|
| **reforge** | `bluei/engine/reforge.py` | Classification: `classify_finding()`, `RefactorClass` enum, `RefactorPhase` state machine, safety gates (`can_auto_refactor`, `is_large_refactor`) |
| **fix_tiers** | `bluei/engine/fix_tiers.py` | Tiered validation: `FixTier` enum, `resolve_tier()`, `TieredValidator`, tier escalation, `_auto_diff_review()` |
| **cascade** | `bluei/engine/cascade.py` | Multi-engine cascade: `DeterministicCascade`, 9 `CascadeStage` subclasses (linter, recipe, pattern replay, composite, AST), `CascadeContext` |
| **lifecycle** | `bluei/engine/lifecycle.py` | Fix lifecycle orchestration: `apply_autofix()`, `apply_cascade_fix()`, `apply_claude_fix()`, `run_validation_gate()`, `verify_fix_closed()`, `run_named_checks()`, `_tier_validate()` |
| **context_fix** | `bluei/engine/context_fix.py` | Context-aware fixes: `apply_contextual_fix()`, `build_contextual_prompt()`, failure tracking, auto-skip |
| **prompts** | `bluei/engine/prompts.py` | LLM prompt rendering: `render_claude_fix_prompt()`, specialized renderers for test coverage, type safety, complexity, max-lines |
| **recipe_engine** | `bluei/engine/recipe_engine.py` | Recipe system: `RecipeEngine.match()`, `RecipeEngine.apply()`, `has_recipe()`, built-in/staged directory loading |
| **recipe_schema** | `bluei/engine/recipe_schema.py` | Recipe data model: `Recipe`, `RecipeMatch`, `RecipeReplacement`, `RecipeValidation` dataclasses, YAML loader |
| **recipe_handlers** | `bluei/engine/recipe_handlers.py` | Recipe execution: `TextHandler`, `RegexSubstituteHandler`, `CommandHandler`, `ScaffoldHandler` |
| **constants** | `bluei/engine/constants.py` | All constants: `DETECTOR_CATALOG`, `CONTEXT_RULES`, `CLAUDE_REQUIRED_RULES`, `BASELINE_VALIDATION_CHECKS`, `RULE_TARGET_CHECKS`, tier limits, cooldown configs |
| **cli** | `bluei/engine/cli.py` | pr-cycle execution path: finding iteration, worktree creation, engine dispatch, PR creation, cost tracking, batch grouping |
| **models** | `bluei/engine/models.py` | `Finding` dataclass: rule, path, line, snippet, confidence, `safe_to_autofix`, `refactor_class` |
| **state** | `bluei/engine/state.py` | Persistent state: `load_finding_record()`, `update_finding_record()`, `increment_fix_attempt()`, activity tracking |
| **pattern_store** | `bluei/engine/pattern_store.py` | Fix pattern persistence: `FixPatternStore`, `lookup()`, `lookup_structural()`, `lookup_fuzzy()` |
| **pattern_extractor** | `bluei/engine/pattern_extractor.py` | Pattern mining: `extract()` learns reusable patterns from successful fixes |
| **pattern_replay** | `bluei/engine/pattern_replay.py` | Pattern replay: `try_replay()` applies previously-learned fixes |
| **composite_pattern** | `bluei/engine/composite_pattern.py` | Multi-file patterns: `CompositePatternStore`, `CompositePatternApplier` |
| **shared_pattern_library** | `bluei/engine/shared_pattern_library.py` | Cross-repo pattern sharing: `publish()`, `promote_pattern_to_recipe()` |
| **ast_engine** | `bluei/engine/ast_engine/` | AST transforms: `ASTTransformer`, language-specific transform registries |
| **utils** | `bluei/engine/utils.py` | Utility functions: `run_capture()`, `append_lesson()`, `load_lessons_for_finding()`, `load_failure_clusters_for_rule()` |

---

## Composite & Shared Patterns

### Composite Patterns

`composite_pattern.py` handles multi-step fixes that modify multiple files
atomically. Unlike single-step recipe/pattern fixes, composite patterns apply
changes across several files in a single transaction. If any step fails, the
entire composite fix is rolled back.

- **CompositePatternStore** — durable storage for learned multi-file patterns
- **CompositePatternApplier** — applies all steps in order, verifies each
- **Safety:** Composite fixes count as a single fix attempt. All member files
  must pass validation together.

### Shared Pattern Library

`shared_pattern_library.py` provides a thread-safe cross-repo pattern cache
using structural hashing for pattern deduplication. Patterns learned from one
repo can be replayed on others with similar code structures.

- **Privacy normalization** — file paths and identifiers are normalized before
  hashing to prevent leaking project-specific details
- **Confidence merging** — when the same structural hash matches multiple stored
  patterns, confidence scores are merged using a weighted average
- **Auto-promotion** — patterns with confidence above `AUTO_REPLAY_THRESHOLD`
  (0.9) are automatically promoted to staged YAML recipes for faster replay
| **refactor_queue** | `bluei/engine/refactor_queue.py` | Human review queue: `RefactorQueue`, `enqueue_refactor_work()`, approve/complete/fail lifecycle |
| **batch_pr** | `bluei/engine/batch_pr.py` | Batch PR system: `group_findings_for_batch()`, `process_batch()` |
| **orchestrator** | `bluei/engine/orchestrator.py` | Finding orchestration: `discover_findings()`, cycle command builder |
| **llm_fixable_rules.yaml** | `bluei/engine/llm_fixable_rules.yaml` | LLM-fixable rule definitions with `prompt_hint`, complexity, language filters |
