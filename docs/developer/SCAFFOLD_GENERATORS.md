<!-- CATEGORY: developer -->

# Scaffold Generators

## Overview

The Scaffold Generators subsystem auto-generates test files and documentation sections from **module introspection** — it reads source code, extracts the public API surface, and emits a complete (though stub-heavy) test file or a structured doc section. It lives inside the sandbox execution engine at `bluei/engine/scaffold/` and is invoked by the **recipe engine** when a scaffold-type recipe matches a finding.

Concrete examples of what it produces:

| Scaffold type | Input | Output |
|---|---|---|
| `test_file` | `src/user_service.py` | `tests/test_user_service.py` with stubs for every public function and class |
| `doc_section` (rollback) | `docs/playbook.md` | Injects a `## Rollback` section with project-specific verify commands |
| `doc_section` (quickstart) | `README.md` | Injects a `## Quick start` section with auto-detected install/test commands |

This is a **fix-time generator**: it runs only when the recipe engine determines a gap (e.g. "no test file exists for `src/foo.py`") and picks a scaffold recipe to close it.

## Architecture

The subpackage contains 9 files across the scaffold/ root and its inspectors/ subdirectory:

```
bluei/engine/scaffold/
├── __init__.py          # Public API re-exports (ScaffoldEngine, models)
├── engine.py            # ScaffoldEngine — orchestrator
├── doc_scaffolder.py    # Markdown section insertion helpers
├── framework_detect.py  # Test-framework auto-detection
├── models.py            # Dataclasses: ModuleExports, ScaffoldResult, etc.
├── path_resolver.py     # Source ↔ test path mapping
├── templates.py         # String template functions for test files & doc sections
└── inspectors/
    ├── __init__.py
    └── python.py        # Python module inspector (stdlib ast)
```

### Component Interaction Diagram

```
Recipe Engine (ScaffoldHandler)
    │
    ▼
┌───────────────────────────────────────────────────────────┐
│ ScaffoldEngine                                             │
│                                                            │
│  generate_test_file()          generate_doc_section()      │
│  ┌──────────────┐             ┌──────────────────┐        │
│  │ 1. path_resolver           │ 1. check file     │        │
│  │    test→source  │          │    exists          │        │
│  │ 2. inspectors    │          │ 2. detect install/ │        │
│  │    python.py     │          │    test commands   │        │
│  │ 3. framework_detect        │ 3. templates       │        │
│  │ 4. templates    │          │    render_section  │        │
│  │ 5. compile check│          │ 4. doc_scaffolder  │        │
│  │ 6. write file   │          │    insert_section  │        │
│  └──────────────┘             └──────────────────┘        │
│                                                            │
│  Returns: ScaffoldResult(success, output_path, content)    │
└───────────────────────────────────────────────────────────┘
```

Every path ends with a `ScaffoldResult` — a pass/fail dataclass carrying the generated content, the output path, and an optional error string. The caller (recipe handler) translates this into a `RecipeFixResult` for the fix pipeline.

## Scaffold Engine

`engine.py` defines `ScaffoldEngine`, the top-level orchestrator. It exposes two public methods:

### `generate_test_file(test_path, worktree, language="python")`

The primary entry point for test scaffolding. Steps:

1. **Resolve source path** — calls `TestPathResolver.test_to_source()` to find the source module that corresponds to the given test path.
2. **Fallback** — if resolution fails, searches `worktree/src/` for a matching module name (stripping `test_` prefix).
3. **Existence guard** — returns failure if the source module doesn't exist.
4. **Language dispatch** — currently only `"python"` is supported. All other languages return an immediate failure.
5. **Inspector** — calls `inspect_python_module(source_path, worktree)` to extract `ModuleExports`.
6. **Syntax guard** — catches `SyntaxError` from the AST parser. Returns failure with the parse error message.
7. **Exports guard** — if the module has zero public functions and zero public classes, returns failure ("no public exports").
8. **Framework detection** — calls `detect_test_framework(worktree, "python")` to determine whether to emit `pytest` or `unittest` patterns.
9. **Template render** — calls `render_test_file(exports, framework)` to produce the Python source.
10. **Compile check** — compiles the generated content with `compile(content, str(test_path), "exec")`. If the generated code has syntax errors, returns failure.
11. **Overwrite guard** — if the test file already exists, returns failure (never overwrites).
12. **Write** — creates parent directories, writes the UTF-8 file. Returns success with the full content.

### `generate_doc_section(doc_path, section_type, worktree, insert_before, insert_after)`

Generates and inserts a section into an existing markdown document:

1. **Existence guard** — returns failure if the doc file doesn't exist.
2. **Duplicate guard** — returns failure if the heading (derived from `section_type`) already appears in the document.
3. **Section dispatch** — two supported types:
   - `"rollback"` → uses `render_rollback_section()` with auto-detected verify commands
   - `"quickstart"` → uses `render_quickstart_section()` with auto-detected install + test commands
4. **Insertion positioning** — `_insert_section()` supports anchoring before a landmark string (`insert_before`) or after one (`insert_after`). Falls back to appending at end of file.
5. **Write** — overwrites the doc file with the updated content. Returns success.

Both methods are idempotent by design: they will not produce output if the target already exists (test file) or the section heading is already present (doc).

## Module Inspection

The `inspectors/` subpackage contains **language-specific** code inspectors. The only inspector currently implemented is for Python.

### Python Inspector (`inspectors/python.py`)

Uses Python's **stdlib `ast`** module — zero external dependencies. The public API is a single function:

```
inspect_python_module(file_path: Path, worktree: Path) → ModuleExports
```

**Extraction rules:**

| AST node | Maps to | Public filter |
|---|---|---|
| `ast.FunctionDef`, `ast.AsyncFunctionDef` | `FunctionExport` | Name must not start with `_` |
| `ast.ClassDef` | `ClassExport` | Name must not start with `_` |
| `ast.Assign` (top-level) | `constants` list (str) | Target name must not start with `_` |
| `ast.AnnAssign` (top-level) | `constants` list (str) | Target name must not start with `_` |
| `ast.Import`, `ast.ImportFrom` | `imports` list (str) | All imports |

**`FunctionExport` captures:**

- `name` — function name
- `params` — list of `ParamInfo(name, type_annotation, default)`; `self`/`cls` are filtered out
- `return_type` — return annotation as a string (via `ast.unparse`)
- `docstring` — extracted via `ast.get_docstring()`
- `decorators` — each decorator unparsed as a string (e.g. `"@dataclass"`, `"@app.route('/')"`)
- `is_async` — True for `AsyncFunctionDef`

`ParamInfo` also captures `*args` and `**kwargs` as params with names like `*items` and `**options`. Default values are extracted (unparsed) for positional args.

**`ClassExport` captures:**

- `name` — class name
- `bases` — unparsed base class expressions (e.g. `["BaseModel"]`, `["ABC"]`)
- `methods` — list of `FunctionExport` for non-private methods; `__init__` is included, other `_`-prefixed methods are excluded
- `is_abstract` — True if decorators contain `abc.ABC`/`ABCMeta`, or any base contains `ABC`

**Module name inference** (`_infer_module_name`): strips `src/` prefix if present, drops `__init__.py`, strips `.py` extensions, joins remaining path segments with dots. E.g. `src/my_pkg/services/user_service.py` → `"my_pkg.services.user_service"`.

### TypeScript / Other Languages

No TypeScript (or JavaScript/Go/Rust) inspector exists yet. The `engine.py` dispatcher explicitly returns an error for non-Python languages in `generate_test_file()`. The doc section generator is language-agnostic (it only manipulates markdown). See [G-04](ARCHITECTURE.md#gaps) for the planned expansion.

## Framework Detection

`framework_detect.py` auto-detects the test framework in use by inspecting project configuration files. This information flavors the generated output — e.g. `pytest`-style functions vs `unittest.TestCase` subclasses.

### `detect_test_framework(worktree, language)`

| Language | Detection logic | Possible returns |
|---|---|---|
| `python` | Checks `pytest.ini`, `pyproject.toml` (for `[tool.pytest` or `pytest`), `setup.cfg` (for `[tool:pytest]`), `conftest.py`, `unittest.cfg` | `"pytest"` (default), `"unittest"` |
| `javascript` / `typescript` | Parses `package.json` deps/devDeps for vitest/jest/mocha; falls back to config file presence | `"vitest"`, `"jest"`, `"mocha"` (default: `"jest"`) |
| `go` | Static | `"go_test"` |
| `rust` | Static | `"cargo_test"` |

**Python priority order**: `pytest.ini` → `pyproject.toml` with pytest content → `setup.cfg` with `[tool:pytest]` → `conftest.py` → `unittest.cfg` → default `"pytest"`.

**JS priority order**: package.json deps (vitest → jest → mocha) → config file presence → default `"jest"`.

### `detect_install_commands(worktree)`

Returns a newline-separated string of install commands. Detection order:

1. `pyproject.toml` with `pytest` → `"pip install pytest"`
2. `requirements-dev.txt` → `"pip install -r requirements-dev.txt"`
3. `setup.py` → `"pip install -e ."`
4. Fallback → `"pip install pytest"`

### `detect_test_commands(worktree)`

Returns a newline-separated string of test commands:

1. If `pytest.ini` or `conftest.py` exists → `"pytest -q"`
2. If `pyproject.toml` exists → `"pytest -q"`
3. Fallback → `"pytest -q"`
4. Additionally, if `Makefile` contains `test` → appends `"make test"`

## Path Resolution

`path_resolver.py` defines `TestPathResolver`, which maps between **source file paths** and **test file paths** using a simple convention: test files live in a `tests/` directory with a `test_` prefix.

### `source_to_test(source_path, worktree) → Path`

| Source path pattern | Result |
|---|---|
| `src/pkg/sub/file.py` | `tests/test_file.py` |
| `src/file.py` | `tests/test_file.py` |
| `anything/else.py` | `tests/test_else.py` |

The `test_` prefix is only added if the filename doesn't already start with `test_`. Intermediate `src/` subdirectories are **flattened** — `src/pkg/sub/mod.py` maps to `tests/test_mod.py`, not `tests/pkg/sub/test_mod.py`.

### `test_to_source(test_path, worktree) → Optional[Path]`

Reverse mapping. Strips the `test_` prefix and `.py` suffix, then searches:

1. `worktree/src/<source_name>`
2. `worktree/<source_name>`
3. `worktree/src/<subdir>/<source_name>` for each non-`_`-prefixed subdirectory in `src/`

Returns the first match found, or `None`.

### `infer_module_name(source_path, worktree) → str`

Converts a file path to a Python module name. Strips `src/`, drops `__init__.py`, strips `.py`, joins with dots.

## Template System

`templates.py` contains **Python string-composing functions**. Despite the `RecipeReplacement.template_file` field in the schema mentioning "Jinja-style templates", the current implementation does **not** use Jinja2 — all templates are plain Python functions returning formatted strings.

### `render_test_file(exports: ModuleExports, framework: str) → str`

Produces a complete Python test file:

```
"""Tests for my_pkg.services.user_service."""
import pytest
from my_pkg.services.user_service import get_user, create_user, UserModel


def test_get_user_basic():
    # TODO: implement test for get_user
    pass


def test_create_user_basic():
    # TODO: implement test for create_user
    pass


class TestUserModel:
    def test_save_basic(self):
        # TODO: implement test for UserModel.save
        pass
```

**Rendering rules:**

- Imports `pytest` on line 2
- Imports all public names from the source module
- For each public function: emits a `test_<name>_basic()` stub with a `# TODO` comment
- For each public class: emits a `Test<ClassName>` class with one `test_<method>_basic(self)` per non-`__init__` method
- If the module has no public exports, emits a docstring-only stub

Note: the `framework` parameter is accepted but not currently used to vary output. All generated tests use bare `pytest` style regardless of framework detection result. The parameter exists as an extension point.

### `render_rollback_section(verify_commands: str) → str`

Produces a `## Rollback` markdown section with a 4-step rollback procedure. The verify command (`pytest -q`, `make test`, etc.) is injected into step 3.

### `render_quickstart_section(install_commands, test_commands) → str`

Produces a `## Quick start` markdown section with a single code block containing the auto-detected install and test commands.

### `doc_scaffolder.py`

Provides a lower-level `insert_section()` function with more flexible anchor positioning (supports `after_heading` using a regex to find the next heading boundary). Used independently by some fix flows, but the `ScaffoldEngine.generate_doc_section()` has its own inline `_insert_section()` — these are **separate implementations** with subtle differences in insertion logic.

## Supported Scaffold Types

### `test_file` — Test file generation

| Property | Detail |
|---|---|
| Trigger | Recipe with `metadata.scaffold_type: test_file` |
| Input | A test path (the desired output location) and a worktree |
| Output | A `test_*.py` file with stubs for every public export |
| Language support | Python only (via `ast`) |
| Idempotent | Skips if the test file already exists |
| Example recipe | `built-in/test-gap-missing-file.yaml` |

The generated test file uses **bare pytest** test functions (no `unittest.TestCase` subclass), with one `test_<name>_basic()` per public function and one `Test<Class>` class per public class. Each stub contains a `# TODO` comment and `pass`.

### `doc_section` — Documentation section generation

| Property | Detail |
|---|---|
| Trigger | Recipe with `metadata.scaffold_type: doc_section` |
| Input | A markdown doc path, a `section_type`, and a worktree |
| Output | The same file with a new `## <Section>` injected |
| Section types | `rollback`, `quickstart` |
| Language support | Language-agnostic (markdown manipulation only) |
| Idempotent | Skips if the heading already exists |

Doc sections auto-detect project-specific commands from the worktree — install commands from `pyproject.toml`/`setup.py`, test commands from `pytest.ini`/`conftest.py`/`Makefile`.

## Integration Points

### Recipe Engine Wiring

The scaffold system is triggered from `recipe_handlers.py` via the `ScaffoldHandler` class. When a recipe's `replacement.type` is `"scaffold"`, the handler dispatches:

```
ScaffoldHandler.apply(recipe, file_path, worktree, finding)
  → ScaffoldEngine.generate_test_file()       // scaffold_type == "test_file"
  → ScaffoldEngine.generate_doc_section()     // scaffold_type == "doc_section"
  → RecipeFixResult(success, files_changed, recipe_id, method="scaffold")
```

The `file_path` parameter serves different roles depending on scaffold type:

- For `test_file`: it is the **target test file path** (e.g. `tests/test_foo.py`) — the engine resolves backward to find the source
- For `doc_section`: it is the **document path** to insert into (e.g. `docs/playbook.md`)

### Recipe YAML Structure

A scaffold recipe in YAML looks like:

```yaml
id: test-gap-missing-file
rule: test-gap-missing-file
language: python
safety: needs_validation

match:
  type: rule_exact
  rule: test-gap-missing-file

replacement:
  type: scaffold           # ← triggers ScaffoldHandler

metadata:
  scaffold_type: test_file # "test_file" | "doc_section"
  language: python         # for test_file
  section_type: rollback   # for doc_section
```

The `match.type: rule_exact` means this recipe is triggered by a **rule engine match** rather than pattern matching — a gap rule (e.g. "missing test file") generates a finding with a specific rule ID, and the recipe with the matching `rule` field is selected.

### Relationship to the Fix Pipeline

Scaffold generation is one step in the broader fix pipeline documented in [FIX_PIPELINE.md](FIX_PIPELINE.md). The pipeline flow is:

```
Finding detected → Recipe matched → Handler applied (scaffold) → Validation → Commit
```

The scaffold step is a **generative fix** — unlike text replacements or command runs, it creates new artifacts from module introspection rather than modifying existing ones. The `safety: needs_validation` flag on scaffold recipes means the generated file requires manual review before the fix is considered complete.

### Configuration

No separate configuration file is needed. The scaffold engine reads from:

- `Recipe.metadata` — `scaffold_type`, `language`, `section_type`
- Worktree filesystem — `pytest.ini`, `pyproject.toml`, `package.json`, `Makefile` for framework/command detection
- `RecipeReplacement.template_file` — schema field present but **not currently consumed** by the scaffold handler

## Cross-References

- [ARCHITECTURE.md](ARCHITECTURE.md) — overall bluei architecture and where scaffold fits in the sandbox runner
- [FIX_PIPELINE.md](FIX_PIPELINE.md) — how scaffold recipes participate in the detect→fix→validate→commit pipeline
- [CONFIG_REFERENCE.md](../reference/CONFIG_REFERENCE.md) — recipe YAML schema reference including `replacement.type: scaffold`
