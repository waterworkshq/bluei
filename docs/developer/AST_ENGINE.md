<!-- CATEGORY: developer -->

# AST Engine

## Overview

The AST Engine is a 14-file subpackage at `bluei/engine/ast_engine/` that provides cross-language AST-aware issue detection and deterministic code transformation. It fits into the fix pipeline as both a **discovery component** (finds issues during cycle scanning) and a **repair component** (applies transformations as a tier-2 cascade stage).

Unlike regex-based recipes or LLM-driven fixes, the AST Engine operates on structured syntax trees — it knows the difference between a function call, a string constant, and an operator — enabling high-confidence matches with very low false-positive rates.

**Where it fits in the pipeline:**

1. **Discovery phase** — `orchestrator.py` calls language-specific scan functions (`_ast_scan_python_files`, `_ast_scan_ts_js_files`, `_ast_scan_go_files`, `_ast_scan_rust_files`) that walk source files, parse them, and run pattern matchers. Matched patterns become `Finding` objects.
2. **Fix phase** — `cascade.py` runs `ASTTransformCascadeStage` (tier 2, after linters/recipes/pattern-replay but before LLM fallback). For each Python finding that has a registered transform, it parses the file, applies the tree mutation, unparses, and validates.

## Architecture

```
bluei/engine/ast_engine/
├── __init__.py              # Factory functions (get_python_matcher, get_ts_matcher, etc.)
├── models.py                # Data classes: ASTPattern, ASTMatch, ASTTransform, ASTTransformResult
├── matcher.py               # ASTPatternMatcher — walks Python AST, checks constraint dicts
├── context.py               # Python AST context queries (parent map, scope, loops)
├── transformer.py           # ASTTransformer — mutates nodes, unparses, validates output
├── ts_parser.py             # TreeSitterAdapter + TSNode — uniform tree-sitter wrapper
├── ts_matcher.py            # TSPatternMatcher — tree-sitter matcher for TS/Go/Rust
├── patterns/
│   ├── __init__.py
│   ├── python_patterns.py   # 9 Python AST patterns
│   ├── ts_patterns.py      # 6 TypeScript/JavaScript patterns
│   ├── go_patterns.py      # 3 Go patterns
│   └── rust_patterns.py    # 3 Rust patterns
└── transforms/
    ├── __init__.py
    └── python_transforms.py # 8 Python AST transforms
```

### Component Interaction

```
                    ┌──────────────────────────┐
                    │        models.py          │
                    │  ASTPattern, ASTMatch,    │
                    │  ASTTransform,            │
                    │  ASTTransformResult        │
                    └──────┬─────────┬──────────┘
                           │         │
              ┌────────────┘         └──────────────┐
              ▼                                     ▼
   ┌─────────────────────┐              ┌─────────────────────┐
   │    matcher.py        │              │   transformer.py     │
   │  ASTPatternMatcher   │              │   ASTTransformer     │
   │                      │              │                      │
   │  find_matches()      │──────────────│  apply(match, xform) │
   │  _matches_pattern()  │   produces   │  _transform_*()      │
   │  _check_constraint() │   ASTMatch   │  compile() verify    │
   └──────────┬───────────┘              └──────────────────────┘
              │
      ┌───────┴────────┐
      ▼                ▼
┌───────────┐   ┌────────────────┐
│ context.py│   │  ts_matcher.py  │
│           │   │ TSPatternMatcher│
│ parent    │   │                 │
│ maps,     │   │ _matches()      │
│ scope     │   │ _check_         │
│ lookups   │   │ constraint()    │
└───────────┘   └────────┬───────┘
                         │
                  ┌──────┴──────┐
                  ▼             ▼
           ┌───────────┐  ┌──────────────┐
           │ts_parser.py│  │ patterns/     │
           │TreeSitter  │  │ (python, ts,  │
           │Adapter     │  │  go, rust)    │
           │TSNode      │  └──────────────┘
           └───────────┘
```

**Flow during discovery:**

1. `orchestrator.py` calls a scan function (e.g., `_ast_scan_python_files`)
2. Scan function imports `get_python_matcher()` from `__init__.py`
3. `__init__.py` creates an `ASTPatternMatcher` with patterns from `patterns/python_patterns.py`
4. For each `.py` file, `matcher.find_matches(source, path, "python")` is called
5. `ASTPatternMatcher` parses source with `ast.parse()`, walks all nodes, checks each pattern's constraints
6. Constraints that need scope context delegate to `context.py` functions (e.g., `find_enclosing_function`, `is_in_return_statement`)
7. Post-filters (hardcoded in `_post_filter`) apply additional checks that are impractical as generic constraints
8. Results are returned as `ASTMatch` objects and converted to `Finding` objects by the orchestrator

**Flow during fix:**

1. `ASTTransformCascadeStage.can_handle()` checks if the finding's rule has a registered transform
2. `attempt()` creates a synthetic `ASTMatch` from the finding, calls `ASTTransformer.apply()`
3. `ASTTransformer` re-parses the source, locates the target node by line/col, dispatches to a `_transform_*` handler
4. Handler mutates the AST tree in place (e.g., wrapping a comparison in `set()`, inserting an `isinstance` guard)
5. Modified tree is unparsed via `ast.unparse()` and compile-checked with `compile()`
6. If valid, the new source is written to the worktree; `verify_fix_closed()` confirms the fix resolved the finding

### Key Design Decisions

- **Two matcher classes, not one.** Python uses `ASTPatternMatcher` with stdlib `ast`; all other languages use `TSPatternMatcher` with tree-sitter. The constraint keys are completely different because they reflect different AST node models.
- **Lazy parser initialization.** `TreeSitterAdapter` caches parsers per language at class level and gracefully returns `None` if tree-sitter is not installed. Orchestrators check `is_available()` before attempting scans.
- **Synthetic matches during fix.** The cascade stage creates an `ASTMatch` with `node=None` because the original match node object doesn't survive serialization across finding objects. The transformer re-locates the node by line/column and type matching.
- **Output validation.** Every transform re-compiles the output with `compile()` before writing to disk. Invalid output is rejected and the finding falls through to LLM.

## Supported Languages

| Language | File Extensions | Parser | Matcher Class | Detection | Transformation |
|----------|----------------|--------|---------------|-----------|----------------|
| Python | `.py` | stdlib `ast` | `ASTPatternMatcher` | ✅ 9 patterns | ✅ 8 transforms |
| TypeScript | `.ts`, `.tsx` | tree-sitter (`tree_sitter_typescript`) | `TSPatternMatcher` | ✅ 6 patterns | ❌ |
| JavaScript | `.js`, `.jsx` | tree-sitter (`tree_sitter_javascript`) | `TSPatternMatcher` | ✅ (shared with TS) | ❌ |
| Go | `.go` | tree-sitter (`tree_sitter_go`) | `TSPatternMatcher` | ✅ 3 patterns | ❌ |
| Rust | `.rs` | tree-sitter (`tree_sitter_rust`) | `TSPatternMatcher` | ✅ 3 patterns | ❌ |

**Parser selection logic** (from `ts_parser.py`):

- Python always uses stdlib `ast` — no tree-sitter dependency
- TS/JS tries `tree_sitter_typescript` first, falls back to `tree_sitter_javascript`
- Go and Rust each have their own optional tree-sitter package
- If a tree-sitter package is not installed, the adapter returns `None` and scans for that language are silently skipped

## Pattern Matchers

### How Patterns Are Defined

Patterns are `ASTPattern` dataclass instances defined in language-specific files under `patterns/`. Each pattern has:

```python
@dataclass
class ASTPattern:
    id: str                        # Unique identifier (e.g., "perf-pop-front-loop")
    language: str                  # "python", "typescript", "go", "rust"
    node_type: str                 # AST node type to match (e.g., "Call", "BinOp")
    constraints: Dict[str, Any]    # Positive constraints (all must match)
    negation_constraints: Optional[Dict[str, Any]]  # Negative constraints (none must match)
    confidence: float              # 0.0–1.0 confidence score
    category: str                  # "bug", "perf-smell", "lint", "type-safety"
```

### How Matching Works

**Python (`ASTPatternMatcher`):**

1. `ast.parse(source)` → tree
2. `build_parent_map(tree)` → parent lookup for scope queries
3. Filter patterns by `pattern.language == language`
4. For each applicable pattern, walk all nodes of matching `node_type`
5. Check all `constraints` via `_check_constraint()`, which dispatches by key name to type-specific checkers
6. Check all `negation_constraints` — if any matches, the node is excluded
7. Apply `_post_filter()` for pattern-specific logic that doesn't fit the constraint model (e.g., "only flag `pop(0)` not `pop(1)`", "only flag in a loop body")
8. Build context dict (enclosing function name, in-loop flag) for the match

**Non-Python (`TSPatternMatcher`):**

1. `TreeSitterAdapter(language).parse(source)` → `TSNode` root
2. `root.find_all(pattern.node_type)` → all nodes of the target type
3. For each node, check constraints via `_check_constraint()`, which dispatches on constraint key prefix (`ts_*`, `go_*`, `rust_*`, `in_test_file`)
4. Check negation constraints — if any matches, skip

### Python Constraint Keys

These keys are supported in the `constraints` dict of a Python `ASTPattern`:

| Key | Meaning | Example |
|-----|---------|---------|
| `op` | Operator type name | `"op": "Add"` |
| `left` | Left operand spec | `"left": "Name"` |
| `right` | Right operand spec | `"right": {"type": "Constant", "value": 0}` |
| `parent` | Parent node type | `"parent": {"type": "Expr", "depth": 1}` |
| `func` | Function being called | `"func": {"type": "Attribute", "attr": "pop"}` |
| `ops` | Comparison operators | `"ops": [{"type": "In"}]` |
| `test` | Condition/if-test spec | `"test": "Compare"` |
| `type` | Specific type field | For `ExceptHandler`: `"type": "Exception"` |
| `args` | Call argument specs | `"args": [{"type": "Constant", "value": 0}]` |
| `body_contains` | Body must contain node type | `"body_contains": {"type": "Call"}` |
| `context` | Delegates to context sub-keys (see below) | `"context": {"in_function_return": True}` |

### Python Context Sub-Keys

Used via the `constraints.context` dict. These require walking the parent chain or the enclosing function body:

| Context Key | What It Checks |
|-------------|---------------|
| `function_name_pattern` | Enclosing function name matches regex |
| `variables_include` | Node variable names include given substrings |
| `enclosing_function_calls_exclude` | Enclosing function does NOT call these methods |
| `in_function_return` | Node is inside a `return` statement |
| `caller_chain_excludes` | Method call chain does NOT include these methods |
| `in_loop_body` | Node is inside a `for`/`while` loop |
| `in_except_handler` | Node is inside an `except` block |
| `function_has_isinstance_guard` | Enclosing function already has `isinstance()` check (negated) |
| `function_uses_str_methods` | Enclosing function uses string methods like `.lower()`, `.strip()` |

### Tree-Sitter Constraint Keys

Shared across TS/JS/Go/Rust patterns in `TSPatternMatcher`. Each language uses its own prefix:

| Key | Languages | Purpose |
|-----|-----------|---------|
| `ts_node_child_type` | TS/JS | Child node has given type (e.g., `"predefined_type"`) |
| `ts_node_child_text` | TS/JS | Child node text equals given value (e.g., `"any"`) |
| `ts_call_name` | TS/JS | Function call name (e.g., `"console.log"`) |
| `ts_right_type` | TS/JS | Right side of `as` expression type |
| `ts_missing_return_type` | TS/JS | Function has no return type annotation |
| `ts_is_exported` | TS/JS | Node is inside an `export` statement |
| `ts_comment_pattern` | TS/JS | Comment text matches regex |
| `ts_call_returns_promise` | TS/JS | Call expression involves Promise |
| `ts_has_catch` | TS/JS | Node is inside `try` or has `.catch()` |
| `ts_is_awaited` | TS/JS | Node is inside `await` expression |
| `in_test_file` | All | File path contains `test`/`spec`/`__test__` |
| `go_rhs_is_call` | Go | Short var decl right-hand side is a call |
| `go_lhs_has_error_var` | Go | Short var decl left-hand side contains `err` |
| `go_has_defer_recover` | Go | Enclosing function has `defer` + `recover` |
| `go_interface_empty` | Go | Interface has no method declarations |
| `rust_method_name` | Rust | Called method name (e.g., `"unwrap"`, `"expect"`) |
| `rust_arg_empty_or_vague` | Rust | Call argument is empty or vague |

## AST Transforms

### How Transforms Work

Transforms are `ASTTransform` dataclass instances that map a pattern ID to a transformation type with parameters:

```python
@dataclass
class ASTTransform:
    id: str                  # Transform ID (e.g., "fix-perf-pop-front-loop")
    source_pattern_id: str   # Which pattern this fixes (e.g., "perf-pop-front-loop")
    transform_type: str      # Handler method name (e.g., "prepend_method")
    params: Dict[str, Any]   # Parameters passed to the handler
```

When `ASTTransformer.apply(source, match, transform)` is called:

1. **Parse** — `ast.parse(source)`
2. **Locate** — `_find_node_in_tree()` finds the target node in the fresh parse by line/column + node type, with fallbacks
3. **Dispatch** — `_transform_{transform_type}(tree, node, params)` is called dynamically
4. **Unparse** — `ast.unparse(tree)` produces new source code
5. **Validate** — `compile(new_source, "<ast-transform>", "exec")` ensures the output is syntactically valid
6. **Return** — `ASTTransformResult(success=True, source=new_source, ...)`

The cascade stage then writes `result.source` to the worktree file and runs `verify_fix_closed()` to confirm the finding was resolved.

### Transform Types

| Transform Type | Handler Method | What It Does |
|---------------|----------------|--------------|
| `replace_op` | `_transform_replace_op` | Swaps an operator (e.g., `Add` → `Sub`) in a `BinOp` node |
| `prepend_method` | `_transform_prepend_method` | Inserts a method call between an object and its attribute call (e.g., `x.lower()` → `x.strip().lower()`) |
| `insert_guard` | `_transform_insert_guard` | Inserts an `if not isinstance(...): raise TypeError(...)` guard in a function body |
| `wrap_set` | `_transform_wrap_set` | Wraps comparison operands in `set()` calls |
| `replace_path` | `_transform_replace_path` | Replaces path prefix in call arguments (e.g., `/tmp/` → `/var/lib/`) |
| `wrap_normalize` | `_transform_wrap_normalize` | Wraps comparison operands in normalization methods (e.g., `.strip().lower()`) |

### Safety Guarantees

- **No regex find-and-replace.** All changes are structural AST mutations — they cannot accidentally modify strings, comments, or wrong parts of the code.
- **Compile verification.** Every transformed output is compiled with Python's `compile()` function before being written to disk.
- **Closed-loop verification.** After writing, `verify_fix_closed()` re-runs the detector to confirm the finding was resolved.
- **Git rollback.** If verification fails, the cascade stage's `rollback()` method runs `git checkout` on the affected file.

## TypeScript Support

### Parser (`ts_parser.py`)

The `TreeSitterAdapter` class wraps tree-sitter parsers for TypeScript, JavaScript, Go, and Rust:

- **`TSNode`** — a lightweight wrapper around tree-sitter's node objects, providing `.type`, `.text`, `.start_row`/`.start_col`, `.children`, `.parent`, and navigation methods (`child_by_field_name`, `children_by_type`, `find_all`).
- **`TreeSitterAdapter`** — manages parser lifecycle with class-level caching (`_parsers` dict). The `_get_parser()` method attempts to import language-specific packages (`tree_sitter_typescript`, `tree_sitter_javascript`, `tree_sitter_go`, `tree_sitter_rust`) and builds `Language` + `Parser` instances.
- **`is_available()`** — static check used by orchestrators to skip tree-sitter scans if the package isn't installed.

### Matcher (`ts_matcher.py`)

The `TSPatternMatcher` class uses tree-sitter nodes for matching:

- `find_matches(source, file_path, language)` parses the source and runs all applicable patterns
- `_matches(node, pattern, file_path)` checks constraints and negation constraints
- `_check_constraint(node, key, value, file_path)` dispatches to language-specific checkers based on the constraint key prefix
- Unlike the Python matcher, there is no post-filter — all logic lives in constraint keys

## Adding New Patterns

### For Python (stdlib ast)

1. **Define the pattern** in `patterns/python_patterns.py`:

```python
ASTPattern(
    id="my-new-pattern",            # Unique ID, kebab-case
    language="python",
    node_type="Call",               # ast node type name (e.g., Call, BinOp, Compare, Constant)
    constraints={
        "func": {"type": "Attribute", "attr": "bad_method"},
        "context": {
            "in_function_return": True,
        },
    },
    confidence=0.85,                # Adjust based on false-positive risk
    category="bug",                 # "bug", "perf-smell", "lint"
)
```

1. **Add post-filter logic** (if needed) in `matcher.py` `_post_filter()` method. Only needed when constraints can't express the check (e.g., checking a specific argument value like `pop(0)`).

2. **Register in `DETECTOR_CATALOG`** in `bluei/engine/constants.py`:

```python
"my-new-pattern": {
    "name": "my-new-pattern",
    "category": "bug",
    "language": "python",
    "confidence": 0.85,
    "autofix": True,
    "source": "ast",
},
```

1. **Create a transform** (optional) in `transforms/python_transforms.py` if you want the fix cascade to auto-fix this pattern:

```python
ASTTransform(
    id="fix-my-new-pattern",
    source_pattern_id="my-new-pattern",
    transform_type="replace_op",    # Or another transform type
    params={"old_op": "Add", "new_op": "Sub"},
)
```

1. **Add tests** in `tests/` — see existing `test_cascade.py` AST transform tests for patterns.

### For TypeScript/JavaScript (tree-sitter)

1. **Define the pattern** in `patterns/ts_patterns.py` using `ts_*` constraint keys.
2. **Add constraint logic** in `ts_matcher.py` `_check_constraint()` if new constraint keys are needed.
3. **Register in `DETECTOR_CATALOG`** in `constants.py`.

### For Go

Same as TypeScript — define in `patterns/go_patterns.py`, add any new `go_*` constraint keys to `ts_matcher.py`.

### For Rust

Same as TypeScript — define in `patterns/rust_patterns.py`, add any new `rust_*` constraint keys to `ts_matcher.py`.

### Adding New Transform Types

To add a new transform handler:

1. Add a new entry in `transforms/python_transforms.py` with a new `transform_type` value
2. Implement `_transform_{type}(self, tree, node, params)` in `transformer.py`
3. The handler method receives the full `ast.AST` tree, the target `ast.AST` node, and `params` dict
4. Use `ast.fix_missing_locations()` on any newly constructed nodes
5. Mutate the tree in place — do not return anything

## Pattern Catalog

### Python Patterns

| Pattern ID | Category | Node Type | What It Detects | Confidence |
|-----------|----------|-----------|-----------------|------------|
| `perf-pop-front-loop` | perf-smell | `Call` | `list.pop(0)` inside a loop — O(n) per iteration | 0.83 |
| `perf-list-membership-loop` | perf-smell | `Compare` | `x in list` inside a loop — O(n) membership per iteration | 0.82 |
| `notifications-email-no-trim` | bug | `Call` | `.lower()` in a return without `.strip()` first — whitespace in email normalization | 0.89 |
| `notifications-type-guard-missing` | bug | `FunctionDef` | Function uses string methods without `isinstance(str)` guard — type safety gap | 0.87 |
| `hardcoded-tmp-path` | lint | `Call` | `Path("/tmp/...")` — hardcoded temp directory path | 0.81 |
| `hardcoded-tmp-path-string` | lint | `Constant` | String literal containing `/tmp/` — hardcoded temp path | 0.81 |
| `catalog-query-not-normalized` | bug | `Compare` | `==` comparison without `.strip()` or `.lower()` — case/whitespace-sensitive matching | 0.93 |
| `discount-math-sign` | bug | `BinOp` | `Add` operator inside discount/price/calc functions — likely wrong sign | 0.95 |
| `broad-except` | lint | `ExceptHandler` | Bare `except:` or `except Exception:` — catches too broadly | 0.88 |

### TypeScript/JavaScript Patterns

| Pattern ID | Category | Node Type | What It Detects | Confidence |
|-----------|----------|-----------|-----------------|------------|
| `type-explicit-any` | type-safety | `type_annotation` | `: any` type annotation | 0.85 |
| `debug-console-log` | lint | `call_expression` | `console.log(...)` outside test files | 0.70 |
| `ts-unsafe-any-cast` | type-safety | `as_expression` | `as any` type cast | 0.82 |
| `type-missing-return` | type-safety | `function_declaration` | Exported function without return type annotation | 0.80 |
| `xo-no-warning-comments` | lint | `comment` | TODO / FIXME / HACK / XXX comments outside test files | 0.80 |
| `ts-unhandled-promise` | bug | `call_expression` | Promise-returning call without `.catch()` or `await` | 0.78 |

### Go Patterns

| Pattern ID | Category | Node Type | What It Detects | Confidence |
|-----------|----------|-----------|-----------------|------------|
| `go-unchecked-error` | bug | `short_var_declaration` | Function call result not assigned to `err` variable | 0.75 |
| `go-empty-interface` | lint | `interface_type` | `interface{}` (empty interface) — prefer typed interfaces | 0.70 |
| `go-goroutine-unsafe` | bug | `go_statement` | Goroutine without `defer` + `recover` — potential panic loss | 0.65 |

### Rust Patterns

| Pattern ID | Category | Node Type | What It Detects | Confidence |
|-----------|----------|-----------|-----------------|------------|
| `rust-unwrap-panic` | bug | `call_expression` | `.unwrap()` outside test files — potential panic | 0.72 |
| `rust-clone-unnecessary` | perf-smell | `call_expression` | `.clone()` call — possible unnecessary allocation | 0.60 |
| `rust-expect-vague` | lint | `call_expression` | `.expect("")` with empty or vague message | 0.70 |

## Transform Catalog

All transforms are Python-only. Each maps a pattern ID to a fix.

| Transform ID | Pattern (source) | Transform Type | What It Changes | Params |
|-------------|-----------------|----------------|-----------------|--------|
| `fix-perf-pop-front-loop` | `perf-pop-front-loop` | `prepend_method` | Prepends `.copy()` or similar (no-op — params empty) | `{}` |
| `fix-perf-list-membership-loop` | `perf-list-membership-loop` | `wrap_set` | Wraps list operand in `set()` for O(1) membership | `{}` |
| `fix-notifications-email-no-trim` | `notifications-email-no-trim` | `prepend_method` | Inserts `.strip()` before `.lower()` | `{"method": "strip"}` |
| `fix-notifications-type-guard-missing` | `notifications-type-guard-missing` | `insert_guard` | Inserts `if not isinstance(value, str): raise TypeError(...)` | `{"variable": "value", "check_type": "str", "message": "Expected str"}` |
| `fix-hardcoded-tmp-path` | `hardcoded-tmp-path` | `replace_path` | Replaces `/tmp/` → `/var/lib/` in path arguments | `{"old_prefix": "/tmp/", "new_prefix": "/var/lib/"}` |
| `fix-catalog-query-not-normalized` | `catalog-query-not-normalized` | `wrap_normalize` | Wraps comparison operands in `.strip().lower()` | `{"methods": ["strip", "lower"]}` |
| `fix-discount-math-sign` | `discount-math-sign` | `replace_op` | Changes `Add` to `Sub` in discount calculations | `{"old_op": "Add", "new_op": "Sub"}` |
| `fix-broad-except` | `broad-except` | `replace_op` | (No-op — params empty, no actual tree mutation) | `{}` |

## Integration Points

### Discovery Phase

The AST Engine integrates with the discovery phase through four scan functions in `orchestrator.py`:

| Function | Language | Matcher Factory | Parser |
|----------|----------|----------------|--------|
| `_ast_scan_python_files()` | Python | `get_python_matcher()` | stdlib `ast` |
| `_ast_scan_ts_js_files()` | TypeScript / JavaScript | `get_ts_matcher()` | tree-sitter |
| `_ast_scan_go_files()` | Go | `get_go_matcher()` | tree-sitter |
| `_ast_scan_rust_files()` | Rust | `get_rust_matcher()` | tree-sitter |

Each function:

1. Checks parser availability (tree-sitter functions bail early if not installed)
2. Walks appropriate file extensions in the repo, skipping excluded dirs (`.git`, `node_modules`, `dist`, `build`, `.next`, `coverage`, `vendor`)
3. Skips empty files and files that can't be read (OSError, UnicodeDecodeError)
4. Calls `matcher.find_matches(source, rel_path, language)`
5. Converts `ASTMatch` → `Finding`, looking up the rule in `DETECTOR_CATALOG` for confidence overrides

### Fix Phase (`ASTTransformCascadeStage`)

The `ASTTransformCascadeStage` in `cascade.py` is a tier-2 stage in the fix cascade pipeline. Its position in the cascade is:

```
Linter stages (tier 1) → RecipeCascadeStage (tier 2) → PatternReplayCascadeStage (tier 2)
    → CompositePatternCascadeStage (tier 2) → ASTTransformCascadeStage (tier 2)
    → [LLM fallback]
```

Key characteristics:

- **`name`**: `"ast-transform"`
- **`tier`**: `2` (deterministic, zero-cost)
- **`estimated_latency_ms`**: `1000`
- **`estimated_cost`**: `0.0`
- **Language scoping**: Python only (`can_handle()` returns `False` for non-Python findings)
- **Lazy initialization**: The transformer is loaded on first `can_handle()` call, not at stage construction
- **Rule routing**: `can_handle()` checks if `self._transformer.get_transform(finding.rule)` returns a registered transform
- **Synthetic matching**: Creates an `ASTMatch` from the finding (line number + snippet) since the original match node is not serialized
- **Closed-loop validation**: After writing the transformed source, calls `verify_fix_closed()` to re-detect the finding

### Adding Non-Python Transform Support

The roadmap (see [ROADMAP.md](../meta/ROADMAP.md)) notes that `ASTTransformCascadeStage` is Python-only. Adding TypeScript/Go/Rust transforms would require:

1. A tree-sitter-based `TSTransformer` class (analogous to `ASTTransformer` but operating on tree-sitter nodes)
2. A `tree_sitter` `Language` object to convert mutated trees back to source
3. New `ASTTransformCascadeStage` subclasses or language-aware dispatching in the existing stage

## Cross-References

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — Overall system architecture, component descriptions, and execution engine overview
- **[FIX_PIPELINE.md](FIX_PIPELINE.md)** — End-to-end fix pipeline: classification, routing, tiering, and cascade stage ordering
- **[RULES_REFERENCE.md](../reference/RULES_REFERENCE.md)** — Full detection rule catalog, cascade stage reference, and routing decision tree
- **[ROADMAP.md](../meta/ROADMAP.md)** — Upcoming plans including non-Python AST transform support
