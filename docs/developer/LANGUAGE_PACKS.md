<!-- CATEGORY: developer -->

# bluei — Language Packs

Language packs are plugins that detect issues in specific programming languages
or file types. Each pack has a `plugin.py`, `plugin.yaml`, and optional
`health_mapping.yaml`.

## Built-in Packs

### Python (`plugins/python/`)

| Tool | What it finds |
|------|---------------|
| ruff (B, E, W, F, S, C4) | Lint errors, style issues, security bugs, comprehension misuse |
| pylint | Code quality, naming, refactoring opportunities |
| mypy | Type safety |

**Key rules:** `broad-except`, `trailing-whitespace`, `perf-pop-front-loop`,
`perf-list-membership-loop`, `hardcoded-tmp-path`, `test-gap-missing-file`

### TypeScript / JavaScript (`plugins/typescript/`)

| Tool | What it finds |
|------|---------------|
| xo | Code quality, style violations, complexity |
| eslint | Lint errors, best practices |
| tsc --noEmit --strict | Type safety: missing types, explicit `any`, untyped imports |

**Key rules:** `type-explicit-any`, `type-missing-return`, `xo-max-lines`,
`xo-complexity`, `xo-no-warning-comments`, `test-coverage-branch`

### Go (`plugins/go/`)

| Tool | What it finds |
|------|---------------|
| staticcheck | Bugs, style issues, dead code, simplification opportunities |

### Rust (`plugins/rust/`)

| Tool | What it finds |
|------|---------------|
| clippy | Lint errors, style issues, performance optimizations |

### Shell (`plugins/shell/`)

| Tool | What it finds |
|------|---------------|
| shellcheck | Quoting issues, deprecated syntax, injection vectors |

### Dockerfile (`plugins/dockerfile/`)

| Tool | What it finds |
|------|---------------|
| hadolint | Unversioned images, apt cleanup, layer consolidation, best practices |

### Markdown (`plugins/markdown/`)

| Tool | What it finds |
|------|---------------|
| markdownlint | Header spacing, bare URLs, hard tabs, line length, style |

### Test (`plugins/test/`)

A minimal plugin used for unit-testing the plugin loader system. Not for
production use.

## Plugin Structure

Each plugin directory contains:

```
plugins/<language>/
  plugin.py              # Plugin class extending DiscoveryPlugin (required)
  plugin.yaml            # manifest: id, languages, discovery rules (required)
  health_mapping.yaml    # maps linter rules to health scoring (optional)
```

### `plugin.yaml`

```yaml
id: python
description: Python code quality via ruff, pylint, and mypy
languages:
  - python
version: "1.0"

discovery:
  rules:
    - id: broad-except
      category: lint
      description: 'Bare except: catches too much, hides bugs'
      confidence: 0.85
      autofix: false
      quick_win: true
```

### `plugin.py`

Must define a `Plugin` class extending `DiscoveryPlugin` (from `bluei.app.plugins`):

```python
from bluei.app.plugins import DiscoveryPlugin

class Plugin(DiscoveryPlugin):
    def detect(self, repo_path: Path) -> bool:
        """Return True if this plugin can handle the given repo."""
        return (repo_path / "setup.py").exists()

    def discover(self, repo_path: Path, config: dict) -> list:
        """Run detection and return a list of Finding objects."""
        ...
```

### `health_mapping.yaml`

Maps rule IDs to health score components:

```yaml
rule_to_component:
  broad-except: lint_quality
  hardcoded-tmp-path: security_quality

category_inference:
  todo: technical_debt
```

Loaded by `PluginLoader` to integrate plugin findings into the health score
engine.

## Creating a Custom Plugin

1. Create `plugins/<your-language>/plugin.yaml` with an `id`, `languages`,
   and `discovery.rules` array.
2. Create `plugins/<your-language>/plugin.py` with a `Plugin` class extending
   `DiscoveryPlugin`. Implement `detect()` (boolean probe) and `discover()`
   (finding production).
3. Optionally, create `health_mapping.yaml` with `rule_to_component` mappings
   for health score integration.
4. Run `bluei languages` to verify your plugin is discovered.
5. Test with `bluei duster my-project` on a repo with the target language.

## Rule Categories

Categories used across built-in plugins:

| Category | Description |
|----------|-------------|
| `bug` | Logic errors, incorrect behavior |
| `lint` | Style and linting issues |
| `type-safety` | Missing types, type errors |
| `security` | Potential security issues |
| `perf` / `perf-smell` | Performance anti-patterns |
| `docs` | Missing or stale documentation |
| `test-coverage` | Missing tests or coverage gaps |
| `technical-debt` | Code that needs refactoring |
| `refactor` | Code that needs restructuring |

---

## Plugin Contract Reference

A **language pack** is a directory under `plugins/` that follows a specific file structure, class hierarchy, and lifecycle. This section documents every element of the contract so you can build or debug a plugin without reading the loader source.

### Required Files

Every plugin directory **must** contain:

| File | Required | Purpose |
|------|----------|---------|
| `plugin.yaml` | **Yes** | Manifest declaring plugin identity, languages, detection patterns, and discovery rules |
| `plugin.py` | **Yes** | Python module exporting a `Plugin` class that extends `DiscoveryPlugin` or `ToolWrapperPlugin` |
| `health_mapping.yaml` | No | Maps rule IDs and categories to health-score components |

A minimal directory tree:

```
plugins/my-language/
  plugin.yaml           # required
  plugin.py             # required
  health_mapping.yaml   # optional
```

### plugin.yaml Schema

The manifest is a YAML file with these top-level fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `string` | Yes | Unique plugin identifier (e.g. `plugin-python`) |
| `name` | `string` | Yes | Human-readable name (e.g. `Python Plugin`) |
| `version` | `string` | Yes | Semantic version (e.g. `1.0.0`) |
| `author` | `string` | No | Author or team name |
| `languages` | `list[string]` | Yes | Languages this plugin handles (e.g. `[python]`, `[typescript, javascript]`) |
| `requires_container` | `boolean` | No | Whether a container is needed for tool execution (default `false`) |
| `container_name` | `string \| null` | No | Container image name when `requires_container` is true |

#### `discovery` section

Describes the external tool invocation and the rules the plugin can detect.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `discovery.tool` | `string` | No | CLI tool to invoke (e.g. `ruff`, `npx`). Omit for regex-only plugins |
| `discovery.tool_args` | `list[string]` | No | Static arguments prepended before runtime paths |
| `discovery.rules` | `list[Rule]` | No | Rules this plugin declares (used by `detector_catalog()`) |

Each **rule** entry:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `string` | Yes | Internal rule identifier (e.g. `broad-except`) |
| `category` | `string` | No | Rule category (e.g. `bug`, `lint`, `perf-smell`). Default `lint` |
| `confidence` | `float` | No | Confidence score `0.0`–`1.0`. Default `0.7` |
| `autofix` | `boolean` | No | Whether the rule can be auto-fixed. Default `false` |
| `claude_required` | `boolean` | No | Whether Claude-level reasoning is needed to fix. Default `false` |

#### `validation` section

Declares baseline validation commands to run after fixes.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `validation.baseline_checks` | `list[list[string]]` | No | List of command arrays (e.g. `[["python3", "-m", "pytest", "-q"]]`) |

#### `detection` section

Tells the loader how to decide whether a repo matches this plugin. Used by the default `ToolWrapperPlugin.detect()` implementation.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `detection.files` | `list[string]` | No | Glob patterns checked against the repo root (e.g. `["setup.py", "pyproject.toml"]`). First match wins |

#### Minimal example

```yaml
id: plugin-test
name: Test Plugin
version: 1.0.0
author: bluei

languages:
  - test

requires_container: false

discovery:
  rules:
    - id: test-rule
      category: bug
      confidence: 0.85
      autofix: true

validation:
  baseline_checks: []

detection:
  files:
    - test.txt
```

### Required Class Structure

All plugins inherit from `DiscoveryPlugin` (defined in `bluei/app/plugins.py`). There are two paths:

#### Path 1: Direct `DiscoveryPlugin` subclass

For plugins that do **not** wrap an external CLI tool (e.g. pure regex scanners, test fixtures).

```python
from bluei.app.plugins import DiscoveryPlugin

class Plugin(DiscoveryPlugin):
    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def languages(self) -> list[str]: ...
    @property
    def rules(self) -> list[str]: ...

    def detect(self, repo_path: Path) -> bool: ...
    def discover(self, repo_path: Path, config: dict) -> list[Finding]: ...
```

#### Path 2: `ToolWrapperPlugin` subclass

For plugins that invoke an external CLI tool (e.g. `ruff`, `eslint`). Inherits `detect()`, `discover()`, and `_run_tool()` from `ToolWrapperPlugin`.

```python
from bluei.app.plugins import ToolWrapperPlugin

class Plugin(ToolWrapperPlugin):
    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def languages(self) -> list[str]: ...
    @property
    def rules(self) -> list[str]: ...

    def __init__(self):                    # Load manifest for tool config
        super().__init__(manifest)

    def parse_output(self, output: str, repo_path: Path) -> list[Finding]: ...
    # detect() and discover() are inherited
```

#### Abstract properties and methods

| Member | Type | Class | Must implement? |
|--------|------|-------|-----------------|
| `id` | `@property → str` | `DiscoveryPlugin` | Yes |
| `name` | `@property → str` | `DiscoveryPlugin` | Yes |
| `languages` | `@property → list[str]` | `DiscoveryPlugin` | Yes |
| `rules` | `@property → list[str]` | `DiscoveryPlugin` | Yes |
| `detect(repo_path)` | `→ bool` | `DiscoveryPlugin` | Yes (unless inherited from `ToolWrapperPlugin`) |
| `discover(repo_path, config)` | `→ list[Finding]` | `DiscoveryPlugin` | Yes (unless inherited from `ToolWrapperPlugin`) |
| `parse_output(output, repo_path)` | `→ list[Finding]` | `ToolWrapperPlugin` | Yes (if subclassing `ToolWrapperPlugin`) |

### Method Call Order

The plugin lifecycle follows a fixed sequence:

```
PluginLoader.discover()
  │  Scans plugin dirs, reads plugin.yaml files
  │
  ├─► PluginLoader.load(plugin_id)
  │     Imports plugin.py, instantiates Plugin class
  │
  └─► PluginLoader.load_all()
        Loads every discovered plugin

For each loaded plugin on a target repo:

  1. detect(repo_path) → bool
     Quick check: "Should this plugin run on this repo?"
     Returns False → skip entirely.

  2. discover(repo_path, config) → list[Finding]
     Full analysis. Returns zero or more Finding objects.
     For ToolWrapperPlugin, this calls:
       _run_tool(repo_path) → raw output (or None)
       parse_output(output, repo_path) → list[Finding]
```

#### Flow diagram

```
detect()───False──► skip plugin
    │
   True
    │
    ▼
discover(repo_path, config)
    │
    ├── ToolWrapperPlugin path:
    │     _run_tool(repo_path)
    │       ├── tool not found / timeout → return []
    │       └── stdout captured
    │     parse_output(stdout, repo_path) → list[Finding]
    │
    └── Direct DiscoveryPlugin path:
          custom logic → list[Finding]
```

### ToolWrapperPlugin Lifecycle

`ToolWrapperPlugin` (defined in `bluei/app/plugins.py`) handles the common pattern of running an external CLI tool and parsing its output. Here is how it fits together:

1. **Construction** — The plugin's `__init__` loads its own `plugin.yaml` manifest and passes it to `ToolWrapperPlugin.__init__(manifest)`. The manifest is stored as `self._manifest`.

2. **Tool configuration** — Read from the manifest at runtime:
   - `self.tool_name` → `manifest["discovery"]["tool"]`
   - `self.tool_args` → `manifest["discovery"]["tool_args"]`

3. **Detection** — `detect()` checks the `detection.files` patterns from the manifest against the repo root. Override this method for custom detection logic (e.g. the TypeScript plugin also inspects `package.json`).

4. **Discovery** — `discover()` calls `_run_tool()`, which:
   - Checks `shutil.which(tool)` — returns `None` if the tool is missing
   - Builds the command: `[tool] + tool_args + extra_args`
   - Runs via `subprocess.run()` with a 120-second timeout
   - Returns `stdout` on success, `None` on failure

5. **Parsing** — If `_run_tool()` returns output, `discover()` delegates to `parse_output()` (which you must implement). If the tool is unavailable, `discover()` returns `[]`.

6. **Fallback** — Plugins commonly supplement tool output with regex-based `_text_scan()` and merge/dedup the two lists (by `path`, `line`, `rule`).

```python
# Simplified discover() from ToolWrapperPlugin:
def discover(self, repo_path, config):
    output = self._run_tool(repo_path)
    if output is not None:
        return self.parse_output(output, repo_path)
    return []
```

### health_mapping.yaml Format

The health mapping file connects plugin rule IDs and category keywords to the health scoring engine's component names. It is optional — when absent, the loader substitutes empty mappings.

| Section | Type | Description |
|---------|------|-------------|
| `rule_to_component` | `map<string, string>` | Direct mapping from a rule ID to a health component |
| `category_inference` | `map<string, string>` | Fuzzy keyword → component mapping for rules not in `rule_to_component` |

Example from the Python plugin:

```yaml
rule_to_component:
  discount-math-sign: bug_quality
  broad-except: lint_quality
  perf-pop-front-loop: performance
  test-gap-missing-file: test_gaps

category_inference:
  discount: bug_quality
  trailing: lint_quality
  perf: performance
```

The loader (`PluginLoader._load_health_mapping`) reads this file and registers the mappings with `HealthEngine.register_language_pack_mappings()`. This happens during `PluginLoader.discover()`, before any plugin is loaded or executed.

### PluginLoader Discovery

`PluginLoader` (in `bluei/app/plugins.py`) is responsible for finding, loading, and caching plugins.

#### Discovery flow

```
PluginLoader(plugins_dir)
  │
  discover()
  │  Iterates subdirectories of plugins_dir
  │  Reads plugin.yaml from each subdirectory
  │  Loads health_mapping.yaml (or empty defaults)
  │  Registers health mappings with HealthEngine
  │  Stores manifests in self._manifests
  │
  load(plugin_id)
  │  Resolves manifest → plugin directory
  │  Imports plugin.py via importlib
  │  Expects module-level class named "Plugin"
  │  Instantiates Plugin() with no args
  │  Caches in self._plugins
  │
  load_all()
  │  Calls discover(), then load() for each manifest
  │
  get_for_language(language)
     Searches loaded plugins for a matching language (case-insensitive)
```

#### Key lookup methods

| Method | Returns | Description |
|--------|---------|-------------|
| `discover()` | `list[dict]` | Scan `plugins_dir`, parse all manifests. Returns raw manifest dicts |
| `load(plugin_id)` | `DiscoveryPlugin \| None` | Import and instantiate a single plugin by ID |
| `load_all()` | `dict[str, DiscoveryPlugin]` | Load every discovered plugin |
| `get(plugin_id)` | `DiscoveryPlugin \| None` | Return a previously loaded plugin |
| `get_for_language(lang)` | `DiscoveryPlugin \| None` | Find loaded plugin supporting a language |
| `get_manifest(plugin_id)` | `dict \| None` | Return raw manifest for a plugin |
| `list_available()` | `list[str]` | Plugin IDs from discovered manifests (not necessarily loaded) |
| `list_loaded()` | `list[str]` | Plugin IDs that have been instantiated |
| `detector_catalog()` | `list[dict]` | Flat list of all rule metadata across all manifests |

#### Important details

- **Class name must be `Plugin`** — The loader looks for `module.Plugin` by name. Any other class name will be silently ignored.
- **No-argument constructor** — `load()` calls `Plugin()` with no arguments. If your `__init__` requires parameters, loading will fail. `ToolWrapperPlugin` subclasses typically load their own manifest from `Path(__file__).parent / "plugin.yaml"`.
- **Lazy loading** — Manifests are read on `discover()`, but the Python module is only imported on `load()`. This means invalid `plugin.py` files won't cause errors until an attempt is made to use them.
- **Caching** — Once loaded, plugins are cached in `_plugins`. Subsequent `load()` calls for the same ID return the cached instance.
- **Language matching is case-insensitive** — `get_for_language("Python")` and `get_for_language("python")` are equivalent.

### Finding Model

All `discover()` methods return a `list[Finding]`. The `Finding` dataclass (from `bluei.app.models`) has these fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `finding_id` | `str` | — | Unique identifier (use a hash of repo + path + line + rule) |
| `repo` | `str` | — | Repository path or URL |
| `path` | `str` | — | File path relative to repo root |
| `line` | `int` | — | Line number (1-indexed) |
| `rule` | `str` | — | Rule ID matching a `plugin.yaml` rule |
| `snippet` | `str` | — | Short excerpt of the problematic code (max ~200 chars) |
| `confidence` | `float` | — | Detection confidence `0.0`–`1.0` |
| `quick_win` | `bool` | — | Whether this is easy to fix |
| `safe_to_autofix` | `bool` | — | Whether auto-fixing is safe |
| `severity` | `str` | `"medium"` | Severity level: `low`, `medium`, `high` |
| `category` | `str` | `""` | Category tag (e.g. `lint`, `bug`, `type-safety`) |
| `discovered_at` | `str \| None` | `None` | ISO 8601 timestamp |
| `fix_attempts` | `int` | `0` | Number of auto-fix attempts |
| `last_fix_error` | `str \| None` | `None` | Error from last fix attempt |
| `last_fix_at` | `str \| None` | `None` | Timestamp of last fix attempt |
| `fix_success` | `bool` | `False` | Whether the last fix succeeded |
