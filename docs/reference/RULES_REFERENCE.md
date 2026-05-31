<!-- CATEGORY: reference -->

# Rules Reference — bluei

Complete reference for all detection rules, their routing behavior, and fix strategies.

**How to use this doc:**
- Looking for what a specific rule does? → [Detection Rules](#detection-rules)
- Want to know how a rule gets fixed? → [Routing](#routing-overview)
- Adding a new rule? → [Contributing](#contributing-new-rules)
- Looking for recipe YAML details? → [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md)

> **Fix Tiers (T0–T4):** Each rule is assigned a validation tier that controls how
> much scrutiny a fix receives before being accepted. T0 = guaranteed safe (syntax
> check only), T4 = structural change requiring human review. See
> [FIX_PIPELINE.md](../developer/FIX_PIPELINE.md) for the full tier system and per-rule overrides.

---

## Routing Overview

Every finding passes through `classify_finding()` (in `reforge.py`) which determines the fix strategy. The precedence is:

```
1. REFACTOR_CLASS_RULES  → Human review (cannot be auto-fixed)
2. CONTEXT_RULES         → Context-aware routing (skip/LLM/deterministic based on file path)
3. CLAUDE_FIX_RULES      → Always LLM (no deterministic path)
4. Recipe match          → Deterministic YAML fix
5. Ruff autofix          → Deterministic lint fix
6. CASCADE_FIX           → Try 9 stages, then fall to LLM
```

### Strategy → Behavior Mapping

| Strategy | RefactorClass | What happens |
|----------|---------------|--------------|
| `REFACTOR_CLASS_RULES` match | REFACTOR_CLASS | Finding queued for human review. No automated fix. |
| Context `skip` | REFACTOR_CLASS | Human review. |
| Context `deterministic_safe` | SIMPLE_FIX | Apply deterministic fix directly (recipe, text, regex). |
| Context `llm_with_context` | CONTEXTUAL_FIX | Run cascade first, fall to LLM with prompt hints. |
| `CLAUDE_FIX_RULES` match | CLAUDE_FIX | Always LLM. |
| Recipe match | SIMPLE_FIX | Apply recipe (text/regex/command/scaffold handler). |
| Default (no match) | CASCADE_FIX | 9-stage cascade: linter → recipe → pattern replay → composite → AST → LLM. |

### Coverage Summary

| Routing | Count | Description |
|---------|-------|-------------|
| REFACTOR_CLASS_RULES | 4 | Structural refactors + dangerous rules → human review |
| CONTEXT_RULES | 28 | Context-aware routing with file pattern matching |
| CLAUDE_FIX_RULES | 3 | Test coverage rules → always LLM |
| Recipe match | 16 | Deterministic YAML fixes (see [Recipes](#recipes)) |
| Blind CASCADE_FIX | 2 | No guidance — cascade tries everything |

---

## Detection Rules

All 43 rules in `DETECTOR_CATALOG` (defined in `constants.py`).

### Bug Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `discount-math-sign` | python | 0.95 | yes | Incorrect addition in discount calculation (should be subtraction) |
| `catalog-query-not-normalized` | python | 0.93 | yes | String comparison missing normalization |
| `orders-tax-truncation` | python | 0.92 | no | `int()` truncates instead of rounding in currency calculations |
| `notifications-email-no-trim` | python | 0.89 | yes | Email normalization missing `.strip()` before `.lower()` |
| `notifications-type-guard-missing` | python | 0.87 | yes | Missing `isinstance` type guard in normalize function |
| `inventory-invalid-quantity` | python | 0.84 | yes | Missing quantity validation guard in inventory check |
| `ruff-b904` | python | 0.80 | no | Ruff: raise without from inside except (B904) |
| `go-unchecked-error` | go | 0.75 | no | Missing error check on function return |
| `go-goroutine-unsafe` | go | 0.65 | no | Goroutine without panic recovery or error handling |
| `ts-unhandled-promise` | javascript | 0.78 | no | Promise created but never handled (no `.catch()`, `await`, or `return`) |
| `rust-unwrap-panic` | rust | 0.72 | no | `.unwrap()` call that can panic at runtime |

### Lint / Style Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `broad-except` | python | 0.88 | no | Catching overly broad exception (bare `except:` or `Exception`) |
| `hardcoded-tmp-path` | python | 0.81 | yes | Hardcoded `/tmp` path instead of proper state directory |
| `trailing-whitespace` | python | 0.75 | yes | Trailing whitespace on code lines |
| `xo-no-warning-comments` | typescript | 0.80 | yes | TODO/FIXME/HACK warning comments in production code |
| `debug-console-log` | javascript | 0.70 | no | Leftover `console.log()` calls in production code |
| `ruff-b007` | python | 0.75 | yes | Ruff: unused loop variable (B007) |
| `ruff-e501` | python | 0.65 | yes | Ruff: line too long (E501) |
| `ruff-c408` | python | 0.72 | no | Ruff: unnecessary dict call — use literal (C408) |
| `go-empty-interface` | go | 0.70 | yes | `interface{}` instead of `any` (Go 1.18+) |
| `rust-expect-vague` | rust | 0.70 | no | `.expect()` with a vague or uninformative message |

### Security Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `ruff-s311` | python | 0.78 | no | Ruff: use of non-cryptographic random (S311) |

### Type Safety Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `type-explicit-any` | javascript | 0.85 | yes | Explicit `any` type annotation (use `unknown` instead) |
| `type-missing-return` | javascript | 0.80 | yes | Function missing return type annotation |
| `ts-unsafe-any-cast` | javascript | 0.82 | yes | `as any` type cast (use `as unknown` instead) |
| `type-missing-param` | javascript | 0.78 | yes | Function parameter missing type annotation |
| `type-untyped-import` | javascript | 0.75 | no | Import from untyped module without type declaration |

### Performance Smell Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `perf-pop-front-loop` | python | 0.83 | yes | `list.pop(0)` in a loop (O(n) per pop — use `collections.deque`) |
| `perf-list-membership-loop` | python | 0.82 | yes | `in` on a list inside a loop (O(n) — use a set) |
| `rust-clone-unnecessary` | rust | 0.60 | no | Unnecessary `.clone()` — borrow checker may allow reference |

### Refactor Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `xo-max-lines` | typescript | 0.85 | yes | File exceeds maximum line count — needs splitting |
| `xo-complexity` | typescript | 0.82 | yes | Function complexity too high — needs extraction |

### Documentation Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `docs-legacy-reference` | python | 0.91 | yes | Documentation references deprecated module name |
| `docs-missing-rollback` | python | 0.86 | yes | Operations doc missing rollback runbook section |
| `docs-quickstart-gap` | python | 0.74 | yes | Quick start section missing setup/install commands |
| `doc-gap-uncovered-module` | python | 0.78 | no | Module has no documentation (from docs index) |
| `doc-drift-stale-reference` | python | 0.80 | no | Documentation reference is stale (file changed since indexed) |

### Debt / TODO Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `debt-todo-marker` | python | 0.72 | no | TODO/FIXME/HACK comment (intentional debt marker) |

### Test Gap / Coverage Rules

| Rule | Language | Confidence | Autofix | Description |
|------|----------|:----------:|:-------:|-------------|
| `test-gap-missing-file` | python | 0.79 | yes | Source module has no corresponding test file |
| `test-gap-missing-case` | python | 0.77 | yes | Test file missing edge case coverage |
| `test-coverage-branch` | python | 0.82 | yes | Branch coverage gap — uncovered conditional path |
| `test-coverage-function` | python | 0.80 | yes | Function coverage gap — uncovered function |
| `test-coverage-line` | python | 0.78 | no | Line coverage gap — uncovered code line |

---

## REFACTOR_CLASS_RULES

These rules always route to **human review**. No automated fix attempted.

Defined in `reforge.py:78-82`.

| Rule | Why human review |
|------|-----------------|
| `xo-max-lines` | File too long — requires structural split into multiple files. |
| `xo-complexity` | Function too complex — requires extraction/subdivision. |
| `debt-todo-marker` | TODO/FIXME are intentional debt markers. LLM "fixing" them produces noise, not value. |
| `rust-clone-unnecessary` | 0.60 confidence (lowest in catalog). Removing `.clone()` without borrow checker reasoning can cause use-after-move bugs. |

---

## CLAUDE_FIX_RULES

These rules always use **LLM** for fixing. No deterministic path.

Defined in `reforge.py:84-88`.

| Rule | Why LLM |
|------|---------|
| `test-coverage-branch` | Branch coverage requires understanding test logic and generating new assertions. |
| `test-coverage-function` | Function coverage requires writing new test functions. |
| `test-gap-missing-case` | Missing test cases require understanding edge cases and writing assertions. |

---

## Context Rules

All 28 context rules in `CONTEXT_RULES` (defined in `constants.py`).

Each rule has a `default_strategy` (applied when no file pattern matches) and
zero or more `contexts` with `file_patterns` that override the strategy for specific paths.

### Strategy Reference

| `default_strategy` | Routes to | When used |
|---------------------|-----------|-----------|
| `deterministic` | SIMPLE_FIX | Pure deterministic fix, no validation needed |
| `deterministic_safe` | SIMPLE_FIX | Deterministic with validation (baseline + target tests) |
| `llm_with_context` | CONTEXTUAL_FIX | Cascade first, then LLM with context prompt hints |
| `skip` | CLAUDE_FIX | Note: this routes to LLM, NOT human review. See [Asymmetry Note](#skip-strategy-asymmetry). |

| Context `fix_strategy` | Routes to | When used |
|-------------------------|-----------|-----------|
| `skip` | REFACTOR_CLASS | Human review — do not attempt fix |
| `deterministic_safe` | SIMPLE_FIX | Safe deterministic fix |
| `llm_with_context` | CONTEXTUAL_FIX | Cascade + LLM with hints |

### Skip Strategy Asymmetry

**Important:** `default_strategy: "skip"` and context `fix_strategy: "skip"` route differently:

- `fix_strategy: "skip"` (matched context) → REFACTOR_CLASS (human review)
- `default_strategy: "skip"` (no context matched) → CLAUDE_FIX (LLM)

This is a known asymmetry in `classify_finding()` (reforge.py:224-242). Rules that
genuinely need human review for ALL files should use `REFACTOR_CLASS_RULES`, not
context rules with `default_strategy: "skip"`.

---

### Python Rules

#### `ruff-c408`
**Default:** `deterministic` (use dict literal)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/migrations/*.py` | django | skip | Django migrations require `dict()` for runtime model resolution |
| `**/test_*.py`, `**/*_test.py`, `tests/**` | any | deterministic_safe | Using dict literal in tests is safe |

#### `ruff-b904`
**Default:** `llm_with_context` (raise without from inside except)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/middleware*.py` | django | llm_with_context | Preserve exception chain semantics in middleware |

#### `ruff-b007`
**Default:** `llm_with_context` (unused loop variable)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/fixtures*.py` | any | deterministic_safe | Rename unused loop variable `i` to `_i` |

#### `ruff-s311`
**Default:** `skip` (non-cryptographic random)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/test_*.py`, `**/*_test.py`, `tests/**`, `**/fixtures*.py` | any | deterministic_safe | Pseudo-random in tests is acceptable for test data |

#### `ruff-e501`
**Default:** `deterministic_safe` (line too long)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/migrations/*.py` | django | skip | Auto-generated migrations; wrapping makes diffs harder |
| `**/urls.py`, `**/routing.py` | any | skip | URL patterns are more readable on one line |

#### `broad-except`
**Default:** `llm_with_context` (overly broad exception catch)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/taskqueue*.py`, `**/celery*.py`, `**/tasks/*.py`, `**/workers/*.py` | any | skip | Task queue workers catch broadly for retry/logging |
| `**/middleware*.py` | django | skip | Django middleware catches broadly by design |
| `**/test_*.py`, `**/*_test.py` | any | skip | Tests often assert on exception behavior |

#### `perf-pop-front-loop`
**Default:** `deterministic_safe` (pop(0) in loop)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/migrations/*.py` | django | skip | Migration data ops may use pop(0) intentionally |

#### `perf-list-membership-loop`
**Default:** `deterministic_safe` (in list in loop)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/test_*.py`, `**/*_test.py` | any | skip | Perf optimization in tests not worth the overhead |

#### `trailing-whitespace`
**Default:** `deterministic_safe` (strip trailing whitespace)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/*.md`, `*.md`, `**/*.rst`, `*.rst`, `**/*.txt`, `*.txt` | any | skip | Markdown/RST use trailing spaces for line continuation |

#### `hardcoded-tmp-path`
**Default:** `deterministic_safe` (replace /tmp with state dir)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/test_*.py`, `**/*_test.py`, `**/conftest.py` | any | skip | Tests use /tmp for tempdir fixtures |
| `**/scripts/*.py`, `scripts/*.py`, `**/tools/*.py`, `tools/*.py` | any | llm_with_context | Scripts may use /tmp intentionally — verify first |

#### `test-gap-missing-file`
**Default:** `deterministic_safe` (generate test file)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/migrations/**`, `**/alembic/**` | any | skip | DB migrations don't need test files |
| `**/__pycache__/**`, `**/node_modules/**`, `**/.venv/**` | any | skip | Generated/vendor dirs don't need tests |

#### `test-coverage-line`
**Default:** `llm_with_context` (line coverage gap)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/migrations/**`, `**/alembic/**`, `**/generated/**` | any | skip | Generated/migration code doesn't need coverage |
| `**/conftest.py`, `**/fixtures/**` | any | skip | Test infrastructure doesn't need its own coverage |

#### `orders-tax-truncation`
**Default:** `deterministic_safe` (int(X*Y) → round(X*Y, 2))

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/test_*.py`, `**/*_test.py`, `tests/**` | any | skip | Tests may intentionally verify truncation behavior |

### TypeScript / JavaScript Rules

#### `type-explicit-any`
**Default:** `llm_with_context` (explicit `any` → `unknown`)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/*.d.ts`, `**/types/**`, `**/typings/**` | any | skip | Declaration files use `any` at type boundaries |
| `**/test/**`, `**/__tests__/**`, `**/*.test.ts`, `**/*.spec.ts` | any | deterministic_safe | In tests, replace `any` with `unknown` |
| `**/migrations/**`, `**/scripts/**` | any | skip | Dynamic data shapes need `any` |
| `**/*.config.*` (next, webpack, vite, rollup) | any | skip | Build configs have complex dynamic types |

#### `type-missing-return`
**Default:** `deterministic_safe` (add return type annotation)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/pages/api/**`, `**/app/api/**` | nextjs | llm_with_context | Next.js API routes have specific return type expectations |
| `**/controllers/**`, `**/handlers/**` | express | llm_with_context | Express handlers return void — don't add types blindly |

#### `ts-unsafe-any-cast`
**Default:** `llm_with_context` (`as any` → `as unknown`)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/*.d.ts`, `**/types/**`, `**/typings/**` | any | skip | Declaration files use `as any` at interop boundaries |
| `**/*.test.ts`, `**/*.spec.ts` | any | deterministic_safe | In tests, replace `as any` with `as unknown` |
| `**/*.config.*`, `*.config.*` | any | skip | Build configs use `as any` for complex dynamic types |

#### `ts-unhandled-promise`
**Default:** `llm_with_context` (unhandled promise)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/*.test.ts`, `**/*.spec.ts`, `**/__tests__/**` | any | skip | Tests use `expect(promise).rejects` — "unhandled" is intentional |
| `**/*.config.*`, `*.config.*` | any | skip | Build configs use fire-and-forget async patterns |

#### `xo-max-lines`
**Default:** `skip` (file too long)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/generated/**`, `**/auto-generated/**`, `**/*.generated.ts` | any | skip | Generated code is not a refactoring target |
| `**/migrations/**`, `**/seeds/**` | any | skip | DB migrations/seeds are large by nature |

#### `xo-complexity`
**Default:** `skip` (function too complex)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/generated/**`, `**/auto-generated/**` | any | skip | Generated code is not a refactoring target |
| `**/parsers/**`, `**/serializers/**`, `**/deserializers/**` | any | llm_with_context | Parser code is inherently complex — extract by case |

#### `xo-no-warning-comments`
**Default:** `deterministic_safe` (remove TODO/FIXME)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/test/**`, `**/__tests__/**` | any | skip | TODO/FIXME in tests are acceptable tracking markers |

#### `debug-console-log`
**Default:** `deterministic_safe` (remove console.log)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/*.test.ts`, `**/*.spec.ts`, `**/__tests__/**`, `**/test/**` | any | skip | Tests use console.log for debug output |
| `**/scripts/**`, `**/tools/**`, `**/debug/**` | any | skip | Script/debug files use console.log intentionally |

### Go Rules

#### `go-empty-interface`
**Default:** `deterministic_safe` (`interface{}` → `any`)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/vendor/**`, `**/third_party/**`, `**/*.generated.go` | any | skip | Vendor/generated Go files should not be modified |

#### `go-unchecked-error`
**Default:** `llm_with_context` (missing error check)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/*_test.go`, `**/test/**` | any | skip | Go tests skip error checking for brevity |
| `**/vendor/**`, `**/*.generated.go` | any | skip | Vendor/generated code should not be modified |

#### `go-goroutine-unsafe`
**Default:** `llm_with_context` (unsafe goroutine)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/*_test.go`, `**/test/**` | any | skip | Test goroutines don't need production panic recovery |
| `**/vendor/**`, `**/*.generated.go` | any | skip | Vendor/generated code should not be modified |

### Rust Rules

#### `rust-unwrap-panic`
**Default:** `llm_with_context` (.unwrap() that can panic)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/test/**`, `**/*_test.rs`, `**/tests/**` | any | skip | Rust tests commonly use `.unwrap()` for simplicity |

#### `rust-expect-vague`
**Default:** `llm_with_context` (vague .expect() message)

| File Pattern | Framework | Strategy | Why |
|-------------|-----------|----------|-----|
| `**/generated/**`, `**/auto-generated/**` | any | skip | Generated code should not be modified |
| `**/*_test.rs`, `**/tests/**` | any | skip | Tests use short `.expect("test")` intentionally |

---

## Recipes

All 16 built-in recipes. Files are in `bluei/engine/recipes/built-in/`.

Recipe YAML format is documented in [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md#recipe-yaml-format).

| Recipe File | Rule | Language | Handler | Safety | What it fixes |
|-------------|------|----------|---------|--------|---------------|
| `discount-math-sign.yaml` | `discount-math-sign` | python | text | needs_validation | `amount + discount` → `amount - discount` |
| `catalog-query-not-normalized.yaml` | `catalog-query-not-normalized` | python | text | needs_validation | `item == query` → `item.strip().lower() == query.strip().lower()` |
| `orders-tax-truncation.yaml` | `orders-tax-truncation` | python | regex_substitute | needs_validation | `int(X*Y)` → `round(X*Y, 2)` |
| `notifications-email-no-trim.yaml` | `notifications-email-no-trim` | python | text | needs_validation | `value.lower()` → `value.strip().lower()` |
| `notifications-type-guard-missing.yaml` | `notifications-type-guard-missing` | python | regex_substitute | needs_validation | Add `isinstance` type guard to normalize function |
| `inventory-invalid-quantity.yaml` | `inventory-invalid-quantity` | python | regex_substitute | needs_validation | Add `if quantity <= 0: return False` guard |
| `hardcoded-tmp-path.yaml` | `hardcoded-tmp-path` | python | text | needs_validation | `/tmp/qa-sandbox-state.json` → `/var/lib/qa-sandbox/state.json` |
| `trailing-whitespace.yaml` | `trailing-whitespace` | * | regex_substitute | guaranteed | Strip trailing whitespace from all lines |
| `docs-legacy-reference.yaml` | `docs-legacy-reference` | * | text | guaranteed | `legacy_pricer.py` → `price.py` |
| `docs-missing-rollback.yaml` | `docs-missing-rollback` | * | text | needs_validation | Insert rollback runbook section |
| `docs-quickstart-gap.yaml` | `docs-quickstart-gap` | * | regex_substitute | needs_validation | Replace incomplete quick start with full commands |
| `test-gap-missing-file.yaml` | `test-gap-missing-file` | python | scaffold | needs_validation | Generate test file from module introspection |
| `ruff-autofix.yaml` | `ruff-*` (wildcard) | python | command | needs_validation | Run `ruff check --fix` for matching rules |
| `type-explicit-any.yaml` | `type-explicit-any` | typescript | regex_substitute | needs_validation | `: any` → `: unknown` |
| `debug-console-log.yaml` | `debug-console-log` | javascript | regex_substitute | needs_validation | Remove single-line `console.log()` calls |
| `go-empty-interface.yaml` | `go-empty-interface` | go | regex_substitute | needs_validation | `interface{}` → `any` (skips string literals) |

---

## Uncovered Rules (CASCADE_FIX)

Two rules have no routing guidance and fall to the cascade:

| Rule | Why no routing | What happens |
|------|---------------|--------------|
| `doc-gap-uncovered-module` | Generated from docs index which doesn't list vendor files | Cascade tries all stages → LLM writes documentation |
| `doc-drift-stale-reference` | Same reason | Cascade tries all stages → LLM updates stale reference |

These are low-risk: the docs index tracks the project's own code files, so vendor
findings are practically impossible.

---

## Cascade Stages

When a finding reaches CASCADE_FIX, these 9 stages are tried in order (first success wins):

| # | Instance Name | Class | Tier | Language | What it does |
|---|--------------|-------|:----:|----------|--------------|
| 1 | `ruff-fix` | LinterFixStage | 1 | python | `ruff check --fix` |
| 2 | `ruff-format` | LinterFixStage | 1 | python | `ruff format` |
| 3 | `pyupgrade` | LinterFixStage | 1 | python | `pyupgrade` |
| 4 | `autoflake` | LinterFixStage | 1 | python | `autoflake --remove-all-unused-imports` |
| 5 | `isort` | LinterFixStage | 1 | python | `isort` |
| 6 | `recipe-engine` | RecipeCascadeStage | 2 | any | Apply matching YAML recipe |
| 7 | `pattern-replay` | PatternReplayCascadeStage | 2 | any | Apply learned fix pattern |
| 8 | `composite-pattern` | CompositePatternCascadeStage | 2 | any | Apply multi-step composite pattern |
| 9 | `ast-transform` | ASTTransformCascadeStage | 2 | python | AST-level code transformation |

Note: The first 5 stages are all `LinterFixStage` instances with different names and commands.
They are Python-only and require the respective tool on `$PATH` (`shutil.which()` check in `can_handle()`).

---

## Contributing New Rules

### Adding a new detection rule

1. Add entry to `DETECTOR_CATALOG` in `constants.py` with rule name, category, confidence, language
2. Add detection logic in the appropriate plugin (`plugins/<language>/plugin.py`)
3. Decide routing — see decision tree below
4. Add tests

### Routing decision tree

```
Does the rule need human judgment for ALL files?
  → YES: Add to REFACTOR_CLASS_RULES in reforge.py
  → NO: Can it be fixed with a text/regex replacement?
    → YES: Create a recipe YAML + context rule (skip wrong contexts)
    → NO: Can context (file path, framework) determine skip/fix?
      → YES: Add context rule to CONTEXT_RULES in constants.py
      → NO: Does it need LLM but could benefit from prompt hints?
        → YES: Add to CLAUDE_FIX_RULES (or context rule with llm_with_context)
        → NO: Leave as CASCADE_FIX (cascade + LLM fallback)
```

### Adding a new recipe

1. Create `bluei/engine/recipes/built-in/<rule-name>.yaml`
2. Follow the schema in [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md#recipe-yaml-format)
3. Add a context rule to protect wrong contexts (test files, vendor, etc.)
4. Update this doc's [Recipes](#recipes) table

### Adding a new context rule

1. Add entry to `CONTEXT_RULES` list in `constants.py`
2. Include `default_strategy` and `contexts` with `file_patterns`
3. **Always include both `X/**` AND `**/X/**`** patterns — Python's `fnmatch` doesn't
   treat `**` as recursive. `vendor/foo.go` matches `vendor/**` but not `**/vendor/**`.
4. Update this doc's context rule section
5. Add parametrized tests in `tests/test_context_rules_expansion.py`

### Source files

| What | File |
|------|------|
| Detection rules catalog | `bluei/engine/constants.py` — `DETECTOR_CATALOG` |
| Context rules | `bluei/engine/constants.py` — `CONTEXT_RULES` |
| REFACTOR_CLASS / CLAUDE_FIX sets | `bluei/engine/reforge.py` — `REFACTOR_CLASS_RULES`, `CLAUDE_FIX_RULES` |
| Routing logic | `bluei/engine/reforge.py` — `classify_finding()` |
| Context matching | `bluei/engine/reforge.py` — `get_context_rule()`, `match_context()` |
| Recipe engine | `bluei/engine/recipe_engine.py` |
| Recipe YAML schema | `bluei/engine/recipe_schema.py` |
| Recipe handlers | `bluei/engine/recipe_handlers.py` |
| Cascade stages | `bluei/engine/cascade.py` |
| Plugin detection | `plugins/<language>/plugin.py` |
| Recipe YAML files | `bluei/engine/recipes/built-in/*.yaml` |
