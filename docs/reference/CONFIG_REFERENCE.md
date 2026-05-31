<!-- CATEGORY: reference -->

# bluei — Configuration Reference

## Registry (`registry.yaml`)

The registry tracks onboarded repositories. Populated by `bluei init` or `bluei onboard`.

```yaml
repos:
  - name: my-project          # display name
    id: repo-20260101-xxx     # auto-generated UUID
    path: /path/to/repo       # absolute path to git repo
    language: python           # auto-detected or overridden
    enabled: true              # enable/disable scans
    mode: watch-only           # care level (watch-only, note-only, offer-fixes, full-care)
    profile: conservative      # care style (conservative, balanced, aggressive)
    fix_engine: deterministic  # deterministic, auto, claude, opencode
version: '1.0'
```

## Care Levels

| Level | Create Issues | Create PRs | Auto-merge | Description |
|-------|:---:|:---:|:---:|-------------|
| `watch-only` | No | No | No | Dry-run only. Preview without changes. |
| `note-only` | Yes | No | No | Create issues, no PRs or merges. |
| `offer-fixes` | Yes | Yes | No | Issues and PRs, no auto-merge. |
| `full-care` | Yes | Yes | Yes | Full autonomy. |

## Care Profiles

| Profile | Description |
|---------|-------------|
| `conservative` | Minimal changes. Small file sets, low diff limits. Prefer safe fixes. |
| `balanced` | Moderate changes. Default limits. Mix of safe and proactive fixes. |
| `aggressive` | Maximum coverage. Higher limits, more file changes. |

## Per-Repo Config (`config.yaml`)

Generated per onboarded repo. Available keys:

```yaml
plugin_id: python            # language plugin to use
enabled: true                # enable/disable scans for this repo
fix_engine: deterministic    # engine choice
framework: fastapi            # auto-detected (django, fastapi, flask, express, nextjs, etc.)
rule_pack: python-api-safe   # which rule template to apply

# Discovery
discovery:
  ecosystem: python          # python, node, go, rust, shell, docker, markdown
  max_findings: 50           # max findings per scan

# Limits
limits:
  max_files_changed: 3       # max files modified per fix
  max_loc_diff: 150          # max lines-of-code changed per fix
  max_fix_attempts: 2        # max retry attempts per finding
  open_issues_cap: 10        # max open issues
  open_prs_cap: 2            # max open PRs

# Safety
safety:
  mode: observe              # observe, review, fix
  profile: conservative       # conservative, balanced, aggressive

# GitHub integration
github:
  live_actions: false        # enable live PR/issue creation
  auto_merge: false           # enable auto-merge
```

## CLI Options

### Scan / Duster

```
bluei scan <repo>                    # run discovery, create issues
bluei duster <repo>                  # dry-run — preview without changes
bluei scan <repo> --fix-engine auto  # override fix engine
```

### Clean

```
bluei clean <repo>                   # fix findings, open PRs
bluei clean <repo> --fix-engine deterministic
```

### Cron Schedules

```
bluei install-cron <repo>
bluei install-cron <repo> --issue-schedule "0 */4 * * *"
bluei install-cron <repo> --pr-schedule "0 */6 * * *"
bluei install-cron <repo> --merge-schedule "0 6,18 * * *"
```

### Campaign

```
bluei campaign plan --repo <repo> --rules <rule1,rule2> --paths "src/**"
bluei campaign run <campaign_id> --repo <repo> --dry-run
bluei campaign run <campaign_id> --repo <repo> --allow-mutate --worktree /path
bluei campaign pause <campaign_id> --repo <repo> --reason "..."
bluei campaign resume <campaign_id> --repo <repo>
bluei campaign abort <campaign_id> --repo <repo>
```

### Patterns

```
bluei patterns list --repo <repo>
bluei patterns show <pattern_id> --repo <repo>
bluei patterns deactivate <pattern_id> --repo <repo>
bluei patterns reactivate <pattern_id> --repo <repo>
```

### Emergent Rules

```
bluei emergent propose --repo <repo>
bluei emergent validate --repo <repo>
bluei emergent scan --repo <repo> --worktree /path
bluei emergent approve <rule_id> --repo <repo>
bluei emergent reject <rule_id> --repo <repo> --reason "..."
bluei emergent promote <rule_id> --repo <repo>
bluei emergent list --repo <repo>
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `QA_AGENT_WORKSPACE` | repo root | Override workspace directory |
| `BLUEI_MODE` | (prompt) | Pre-set care level on init |
| `MNEMO_CLI_PATH` | `mnemo-cli` | Path to Mnemo CLI for memory integration |
| `BLUEI_SELF_MERGE_REPOS` | empty | Comma-separated repo slugs with self-merge privileges |

## Cost Tracking

The cost tracker (`bluei/engine/cost_tracker.py`) tracks model API
invocation costs. Default limits are hardcoded in the constructor:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `soft_warn` | $2.00 | Log warning if cost exceeds this per run |
| `hard_limit` | $10.00 | Skip LLM fixes if cost exceeds this per run |

### Model Rates

Known per-token rates (USD per 1,000 tokens) tracked by `MODEL_RATES` in
`cost_tracker.py`:

| Model | Input ($/1K) | Output ($/1K) |
|-------|:-----------:|:------------:|
| `claude-sonnet-4` | 0.003 | 0.015 |
| `claude-sonnet-4-20250514` | 0.003 | 0.015 |
| `claude-3-5-sonnet-20241022` | 0.003 | 0.015 |
| `claude-opus-4` | 0.015 | 0.075 |
| `claude-3-opus-20240229` | 0.015 | 0.075 |
| `claude-3-haiku-20240307` | 0.00025 | 0.00125 |
| `gpt-4o` | 0.005 | 0.015 |
| `gpt-4o-mini` | 0.00015 | 0.0006 |
| `default` | 0.003 | 0.015 |

Each fix invocation logs the model used, estimated token counts, and calculated
cost to `cost_log.jsonl`. The `CostTracker` compares cumulative cost against
`soft_warn` and `hard_limit` before each LLM call. Pattern replay savings
(subtracting deterministic fix cost from what an equivalent LLM call would have
cost) are tracked separately.

## Cooldown

| Key | Default | Description |
|-----|---------|-------------|
| `DEFAULT_FINDING_COOLDOWN_SECONDS` | 14400 (4h) | Base cooldown before retrying a finding |
| `MAX_COOLDOWN_SECONDS` | 604800 (7d) | Max cooldown after repeated failures (exponential backoff) |

## Escalation

| Key | Default | Description |
|-----|---------|-------------|
| `consecutive_failure_threshold` | 3 | Escalate after N consecutive failures |
| `silence_after_resolved_minutes` | 60 | Suppress repeat alerts within this window |

## Recipe YAML Format

Recipes drive the deterministic fix engine. Each recipe is a single YAML file
(one per fix) loaded from `recipes/built-in/` or `recipes/staged/` directories.

For the complete list of all 16 built-in recipes, their handlers, and what they fix,
see [RULES_REFERENCE.md](RULES_REFERENCE.md#recipes).

### Top-Level Fields

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `id` | string | **Yes** | — | Unique recipe identifier (e.g. `trailing-whitespace`) |
| `rule` | string | **Yes** | — | Rule name to match. Supports wildcard suffix (e.g. `ruff-*`) |
| `language` | string | No | `*` | Target language (`python`, `go`, `javascript`, etc.) or `*` for all |
| `safety` | string | No | `needs_validation` | `guaranteed` = safe without tests, `needs_validation` = run validation |
| `description` | string | No | `""` | Human-readable summary of what the recipe does |
| `match` | mapping | No | `{type: rule_exact}` | How findings are matched (see below) |
| `replacement` | mapping | No | `{type: text}` | How the fix is applied (see below) |
| `validation` | mapping | No | `{run_baseline: true, run_target: true}` | Test validation controls |
| `metadata` | mapping | No | `{}` | Arbitrary metadata (version, author, handler-specific keys) |
| `priority` | int | No | `1` | Higher values win during recipe matching (scored as `priority * 100`) |

### `match` Sub-Fields

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `type` | string | No | `rule_exact` | Match strategy: `rule_exact`, `rule_prefix` |
| `pattern` | string | No | — | Regex pattern for custom matching |
| `rule` | string | No | — | Explicit rule name to match against |
| `prefix` | string | No | — | Rule prefix for `rule_prefix` match type |
| `scope` | string | No | `line` | Match granularity: `line` |
| `context_guard` | mapping | No | — | Guard conditions; supports `excludes_pattern` to skip files |

### `replacement` Sub-Fields

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `type` | string | No | `text` | Handler: `text`, `regex_substitute`, `command`, `scaffold` |
| `value` | string | No | — | Search string for `text` handler |
| `replacement` | string | No | — | Replacement text (literal or regex backreference) |
| `pattern` | string | No | — | Regex pattern for `regex_substitute` handler |
| `command` | list\[string\] | No | — | Shell command for `command` handler. `{file}` and `{rule_code}` are interpolated |
| `template_file` | string | No | — | Path to a template file (reserved) |
| `prepend_pattern` | string | No | — | Regex pattern to locate insertion point for prepend |
| `prepend_template` | string | No | — | Text to prepend at the matched location |
| `condition` | string | No | — | Guard string. If present in file text → skip (for `text`); if absent → skip (for `regex_substitute`) |
| `count` | int | No | `1` | Max replacements per file. `0` = unlimited |

### `validation` Sub-Fields

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `run_baseline` | bool | No | `true` | Run test suite before applying fix |
| `run_target` | bool | No | `true` | Run test suite after applying fix |

### `metadata` Common Keys

| Key | Type | Description |
|-----|------|-------------|
| `version` | int | Recipe revision number |
| `author` | string | Recipe author identifier |
| `wildcard` | bool | Marks wildcard/prefix-matching recipes |
| `regex_dotall` | bool | Enable `re.DOTALL` flag for regex handler |
| `regex_multiline` | bool | Enable `re.MULTILINE` flag for regex handler |
| `scaffold_type` | string | For `scaffold` handler: `test_file` or `doc_section` |
| `caveat` | string | Human-readable limitation note |

### Complete Example — Regex Substitute

```yaml
id: debug-console-log
rule: debug-console-log
language: javascript
safety: needs_validation
description: "Remove leftover console.log() calls from production code"

match:
  type: rule_exact
  rule: debug-console-log

replacement:
  type: regex_substitute
  pattern: "[ \\t]*console\\.log\\([^)]*\\);?[ \\t]*\\n"
  replacement: ""
  count: 1

validation:
  run_baseline: true
  run_target: true

metadata:
  version: 1
  author: bluei-core
```

### Complete Example — Command Handler (Wildcard Rule)

```yaml
id: ruff-autofix
rule: "ruff-*"
language: python
safety: needs_validation
description: "Apply ruff auto-fix for supported rules"

match:
  type: rule_prefix
  prefix: "ruff-"

replacement:
  type: command
  command: ["ruff", "check", "--fix", "--unsafe-fixes", "--preview", "--select={rule_code}", "{file}"]

validation:
  run_baseline: true
  run_target: false

metadata:
  version: 1
  author: bluei-core
  wildcard: true
```

### Complete Example — Scaffold Handler

```yaml
id: test-gap-missing-file
rule: test-gap-missing-file
language: python
safety: needs_validation
description: "Generate test file from source module introspection"

match:
  type: rule_exact
  rule: test-gap-missing-file

replacement:
  type: scaffold

validation:
  run_baseline: false
  run_target: false

metadata:
  version: 1
  author: bluei-core
  scaffold_type: test_file
  language: python
```

## Batch Rules YAML Format

Batch rules control how related findings are grouped into a single PR.
Defined in `batch_rules.yaml` (default path: `bluei/engine/batch_rules.yaml`).

### Top-Level Structure

```yaml
rules:
  - rule_pattern: "..."
    # ... rule fields
```

### Rule Fields

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `rule_pattern` | string | **Yes** | — | Rule name or glob pattern to match (e.g. `ruff-c408`, `ruff-*`) |
| `enabled` | bool | No | `true` | Enable or disable this batch rule |
| `group_by` | string | No | `rule` | Grouping strategy (see below) |
| `max_batch_size` | int | No | `20` | Maximum findings per batch |
| `max_files_per_batch` | int | No | `15` | Maximum distinct files per batch |
| `max_loc_per_batch` | int | No | `500` | Maximum lines of code changed per batch |
| `severity` | string | No | `normal` | Batch severity: `critical`, `high`, `normal`, `low` |
| `isolation` | mapping | No | `{}` | Isolation rules to exclude specific files from batching |
| `priority` | int | No | `99` | Lower numbers match first |

### Grouping Strategies (`group_by`)

| Strategy | Description |
|----------|-------------|
| `rule` | Group all findings for the same rule into one PR |
| `file` | Group all findings touching the same file |
| `directory` | Group findings by directory |
| `cross-rule` | Combine multiple rules into a single housekeeping PR |

### `isolation` Sub-Fields

| Field | Type | Description |
|-------|------|-------------|
| `file_patterns` | list\[string\] | Glob patterns for files that should **not** be batched (handled as solo PRs) |

### Complete Example

```yaml
rules:
  # Cross-rule housekeeping batch — groups all ruff style fixes
  - rule_pattern: "ruff-*"
    enabled: false             # opt-in, disabled by default
    group_by: "cross-rule"
    max_batch_size: 30
    max_files_per_batch: 20
    max_loc_per_batch: 1000
    severity: "low"
    isolation:
      file_patterns:
        - "**/migrations/*.py"
        - "**/middleware*.py"
    priority: 10

  # Per-rule batching for a specific ruff rule
  - rule_pattern: "ruff-c408"
    enabled: true
    group_by: "rule"
    max_batch_size: 20
    max_files_per_batch: 15
    max_loc_per_batch: 500
    severity: "normal"
    isolation:
      file_patterns:
        - "**/migrations/*.py"
    priority: 1

  # Security rule — keep batches small
  - rule_pattern: "ruff-s311"
    enabled: true
    group_by: "file"
    max_batch_size: 10
    max_files_per_batch: 5
    severity: "high"
    priority: 4
```

### Routing Logic

Findings are routed based on matching (first match wins, ordered by `priority`):

1. Exact patterns (`ruff-c408`) are checked before wildcards (`ruff-*`)
2. Files matching an `isolation.file_patterns` entry are excluded from the batch and fixed as solo PRs
3. Wildcard patterns use fnmatch glob semantics

## LLM Fixable Rules YAML Format

Maps rules that linters **cannot** autofix to LLM fix instructions.
Defined in `llm_fixable_rules.yaml` (default path: `bluei/engine/llm_fixable_rules.yaml`).

### Routing Logic

```
safe_to_autofix=True                          → deterministic autofix
safe_to_autofix=False + rule in this file     → LLM fix engine with prompt_hint
safe_to_autofix=False + rule NOT in this file → needs-human-not-fixable
```

### Top-Level Structure

```yaml
rules:
  ruff-b904:
    description: "..."
    prompt_hint: |
      ...
    complexity: low
    languages: [python]
```

Keys under `rules` are rule names (exact match, no glob patterns).

### Rule Fields

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `description` | string | No | — | Short human-readable summary of the issue |
| `prompt_hint` | string | No | — | Multi-line instruction injected into the LLM fix prompt |
| `complexity` | string | No | — | Estimated fix complexity: `low`, `medium`, `high` |
| `languages` | list\[string\] | No | — | Target languages (e.g. `[python]`, `[typescript, javascript]`) |

### Complete Example

```yaml
rules:
  ruff-b904:
    description: "raise without cause — add 'from' clause to exception re-raises"
    prompt_hint: |
      Fix the bare `raise` statement by adding an appropriate `from` clause:
      - If re-raising the caught exception: use `raise ... from e` (or the caught variable name)
      - If intentionally suppressing context: use `raise ... from None`
      - Preserve the original exception type and message
      - Do NOT change the exception type or add new exceptions
    complexity: low
    languages: [python]

  ruff-s311:
    description: "stdlib random used in security-sensitive context — replace with secrets"
    prompt_hint: |
      Replace `random` module usage with `secrets` module where security matters.
      Use `secrets.choice()` instead of `random.choice()`, `secrets.randbelow()` instead of `random.randint()`.
      Only replace in security-sensitive contexts (tokens, passwords, keys, IDs).
      If the usage is clearly non-security (e.g., shuffling display order), leave it.
    complexity: low
    languages: [python]

  type-untyped-import:
    description: "untyped import or missing declaration file in TypeScript"
    prompt_hint: |
      Fix the untyped import with the smallest safe change.
      Prefer, in order:
      - using an existing typed import path if one already exists in the repo,
      - adding a local `.d.ts` declaration stub for the imported module,
      - adding a narrow module declaration instead of broad `any` pollution.
      Keep the declaration minimal and specific to the imported surface actually used.
      Do not weaken unrelated type safety just to silence the error.
    complexity: medium
    languages: [typescript, javascript]

  test-coverage-line:
    description: "specific uncovered line or statement in coverage data"
    prompt_hint: |
      Add the smallest focused test that executes the uncovered line or statement.
      Reuse existing test helpers and patterns in the repo.
      Prefer augmenting an existing nearby test file over creating a brand new large test module.
      Do not add broad snapshot tests or weak assertions just to flip coverage.
    complexity: medium
    languages: [typescript, javascript, python]
```
