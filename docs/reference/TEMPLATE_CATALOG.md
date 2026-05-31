<!-- CATEGORY: reference -->

# Template & Rule Pack Catalog

## Overview

bluei uses a two-tier template system to bootstrap repository configurations during onboarding:

1. **Repo templates** (`templates/repos/*.yaml`) — preset baseline config for a project archetype: limits, discovery settings, baseline checks, safety policy, and a pointer to a rule pack.

2. **Rule packs** (`templates/rules/*.yaml`) — sets of analysis rules tailored to a specific ecosystem and project role (API vs. library vs. frontend).

During onboarding, bluei auto-detects the project's language, framework, package manager, and monorepo layout, then selects the best-fit repo template and corresponding rule pack. Users can override any auto-detected value with CLI flags.

The onboarding engine lives in `bluei/app/onboarding.py`. Configuration merging uses deep merge: template values serve as the base, auto-detected overrides are layered on top, and CLI flag overrides win. Any key not supplied by the template is filled in from the global `templates/config.yaml.template` defaults.

---

## Repo Templates (11)

### django-app

| Field | Value |
|---|---|
| **Language** | python |
| **Frameworks** | django |
| **Plugin** | `python` |
| **Rule Pack** | `python-api-safe` |
| **Template Type** | `web-framework` |

**What it configures:**
- **Discovery:** ecosystem=python, max 60 findings
- **Baseline checks:** `python3 manage.py test`
- **Limits:** max 4 files changed, 200 lines diff, 3 fix attempts, 15 open issues cap, 3 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=python, template_type=web-framework

**When selected:** When `manage.py` or `**/settings.py` is detected, or the framework is explicitly `django`.

---

### fastapi-app

| Field | Value |
|---|---|
| **Language** | python |
| **Frameworks** | fastapi |
| **Plugin** | `python` |
| **Rule Pack** | `python-api-safe` |
| **Template Type** | `api` |

**What it configures:**
- **Discovery:** ecosystem=python, max 50 findings
- **Baseline checks:** none predefined (inferred at onboarding)
- **Limits:** max 3 files changed, 150 lines diff, 2 fix attempts, 10 open issues cap, 2 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=python, template_type=api

**When selected:** When `from fastapi` or `import fastapi` is found in source files, or the framework is explicitly `fastapi`.

---

### go-service

| Field | Value |
|---|---|
| **Language** | go |
| **Frameworks** | none |
| **Plugin** | `go` |
| **Rule Pack** | `go-safe` |
| **Template Type** | `service` |

**What it configures:**
- **Discovery:** ecosystem=go, max 40 findings
- **Baseline checks:** `go test ./...`, `go vet ./...`
- **Limits:** max 3 files changed, 120 lines diff, 2 fix attempts, 8 open issues cap, 2 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=go, template_type=service

**When selected:** When language is detected as Go (via `go.mod` or `go.sum`).

---

### next-app

| Field | Value |
|---|---|
| **Language** | typescript |
| **Frameworks** | nextjs |
| **Plugin** | `typescript` |
| **Rule Pack** | `node-frontend-safe` |
| **Template Type** | `fullstack` |

**What it configures:**
- **Discovery:** ecosystem=node, max 60 findings
- **Baseline checks:** none predefined (inferred from package.json scripts)
- **Limits:** max 4 files changed, 200 lines diff, 3 fix attempts, 15 open issues cap, 3 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=node, template_type=fullstack

**When selected:** When `next` is in package.json dependencies or the framework is detected as `nextjs`.

---

### node-api

| Field | Value |
|---|---|
| **Language** | typescript |
| **Frameworks** | express |
| **Plugin** | `typescript` |
| **Rule Pack** | `node-backend-safe` |
| **Template Type** | `api` |

**What it configures:**
- **Discovery:** ecosystem=node, max 50 findings, **Docker hinted** (use_docker=true + docker_hint)
- **Baseline checks:** none predefined (inferred from package.json scripts)
- **Limits:** max 3 files changed, 100 lines diff, 2 fix attempts, 10 open issues cap, 2 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=node, template_type=api

**When selected:** When `express` or `@nestjs/core` is in package.json dependencies.

---

### node-library

| Field | Value |
|---|---|
| **Language** | typescript |
| **Frameworks** | none |
| **Plugin** | `typescript` |
| **Rule Pack** | `node-library-safe` |
| **Template Type** | `library` |

**What it configures:**
- **Discovery:** ecosystem=node, max 50 findings
- **Baseline checks:** none predefined (inferred from package.json scripts)
- **Limits:** max 3 files changed, 120 lines diff, 2 fix attempts, 10 open issues cap, 2 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=node, template_type=library

**When selected:** Default for Node repos that have `build` and `test` scripts but no server/frontend framework markers. Also the fallback when `package.json` exists.

---

### node-workspace-root

| Field | Value |
|---|---|
| **Language** | typescript |
| **Frameworks** | none |
| **Plugin** | `typescript` |
| **Rule Pack** | `node-backend-safe` |
| **Template Type** | `monorepo` |

**What it configures:**
- **Discovery:** ecosystem=node, max 80 findings, **monorepo=true**
- **Baseline checks:** none predefined (inferred from package.json scripts)
- **Limits:** max 5 files changed, 250 lines diff, 3 fix attempts, 20 open issues cap, 4 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=node, template_type=monorepo

**When selected:** When a monorepo/workspace pattern is detected: `workspaces` field in package.json or `pnpm-workspace.yaml` exists.

---

### python-api

| Field | Value |
|---|---|
| **Language** | python |
| **Frameworks** | flask |
| **Plugin** | `python` |
| **Rule Pack** | `python-api-safe` |
| **Template Type** | `api` |

**What it configures:**
- **Discovery:** ecosystem=python, max 50 findings, **Docker hinted** (use_docker=true + docker_hint)
- **Baseline checks:** none predefined (inferred at onboarding)
- **Limits:** max 3 files changed, 150 lines diff, 2 fix attempts, 10 open issues cap, 2 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=python, template_type=api

**When selected:** When `flask` is detected as a framework, or `app.py`/`main.py` is at the repo root (and it's not a Django/FastAPI app).

---

### python-library

| Field | Value |
|---|---|
| **Language** | python |
| **Frameworks** | none |
| **Plugin** | `python` |
| **Rule Pack** | `python-library-safe` |
| **Template Type** | `library` |

**What it configures:**
- **Discovery:** ecosystem=python, max 50 findings
- **Baseline checks:** none predefined (inferred at onboarding)
- **Limits:** max 3 files changed, 120 lines diff, 2 fix attempts, 10 open issues cap, 2 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=python, template_type=library

**When selected:** Default for Python repos when `setup.py` or `pyproject.toml` is present and no API framework is detected. Also the ultimate Python fallback.

---

### react-app

| Field | Value |
|---|---|
| **Language** | typescript |
| **Frameworks** | react |
| **Plugin** | `typescript` |
| **Rule Pack** | `node-frontend-safe` |
| **Template Type** | `frontend` |

**What it configures:**
- **Discovery:** ecosystem=node, max 50 findings
- **Baseline checks:** none predefined (inferred from package.json scripts)
- **Limits:** max 3 files changed, 150 lines diff, 2 fix attempts, 10 open issues cap, 2 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=node, template_type=frontend

**When selected:** When `react` is in package.json dependencies but `next` is not (Next.js takes precedence via the `next-app` template).

---

### rust-crate

| Field | Value |
|---|---|
| **Language** | rust |
| **Frameworks** | none |
| **Plugin** | `rust` |
| **Rule Pack** | `rust-safe` |
| **Template Type** | `crate` |

**What it configures:**
- **Discovery:** ecosystem=rust, max 40 findings
- **Baseline checks:** `cargo test`, `cargo check`
- **Limits:** max 3 files changed, 120 lines diff, 2 fix attempts, 8 open issues cap, 2 open PRs cap
- **Safety:** observe mode, conservative profile
- **Meta:** template_version=2, ecosystem=rust, template_type=crate

**When selected:** When language is detected as Rust (via `Cargo.toml` or `Cargo.lock`).

---

## Rule Packs (7)

Each rule pack is a **YAML file** (`templates/rules/<name>.yaml`) with the following schema:

- `name` — pack identifier
- `description` — human-readable summary
- `ecosystem` — `python`, `node`, `go`, or `rust`
- `rules_enabled` — list of rule IDs active for this pack
- `rules_disabled` — list of rule IDs explicitly suppressed
- `severity_overrides` — map of rule ID to severity (`low`, `medium`, `high`)
- `quick_win_bias` — float 0.0–1.0; higher value means the engine prioritizes low-effort findings

### go-safe

| Field | Value |
|---|---|
| **Ecosystem** | go |
| **Quick Win Bias** | 0.3 |
| **Rules enabled** | `debt-todo-marker` |
| **Rules disabled** | *(none)* |
| **Severity overrides** | *(none)* |

Profile: minimal, best-effort pack for Go services. Only detects TODO/FIXME markers as technical debt.

---

### node-backend-safe

| Field | Value |
|---|---|
| **Ecosystem** | node |
| **Quick Win Bias** | 0.4 |
| **Rules enabled** | `trailing-whitespace`, `type-explicit-any`, `type-missing-return`, `xo-no-warning-comments`, `test-coverage-branch`, `test-coverage-line` |
| **Rules disabled** | *(none)* |
| **Severity overrides** | *(none)* |

Profile: full-spectrum pack for Node.js backends and APIs. Type safety (`explicit-any`, `missing-return`), code hygiene (`trailing-whitespace`, `warning-comments`), and test coverage both at branch and line level.

---

### node-frontend-safe

| Field | Value |
|---|---|
| **Ecosystem** | node |
| **Quick Win Bias** | 0.6 |
| **Rules enabled** | `trailing-whitespace`, `type-explicit-any`, `type-missing-return`, `xo-max-lines`, `xo-complexity`, `xo-no-warning-comments` |
| **Rules disabled** | `xo-complexity` |
| **Severity overrides** | `type-explicit-any` → `medium` |

Profile: tuned for React/Next.js frontends. Complexity rules are enabled in the catalog but **disabled by default** (frontend components are often intentionally complex). Type `any`-usage is demoted to medium severity. Highest quick-win bias (0.6) since frontends have many low-hanging whitespace/linter fixes.

---

### node-library-safe

| Field | Value |
|---|---|
| **Ecosystem** | node |
| **Quick Win Bias** | 0.5 |
| **Rules enabled** | `trailing-whitespace`, `xo-no-warning-comments`, `type-explicit-any`, `xo-max-lines`, `xo-complexity` |
| **Rules disabled** | `test-coverage-branch` |
| **Severity overrides** | *(none)* |

Profile: conservative library profile. Test coverage line rules are omitted and branch coverage is explicitly disabled — libraries are expected to have full test suites but coverage tooling varies. Focuses on type safety, file length, and code hygiene.

---

### python-api-safe

| Field | Value |
|---|---|
| **Ecosystem** | python |
| **Quick Win Bias** | 0.5 |
| **Rules enabled** | `broad-except`, `trailing-whitespace`, `hardcoded-tmp-path`, `perf-pop-front-loop`, `test-gap-missing-file`, `test-coverage-branch` |
| **Rules disabled** | `debt-todo-marker` |
| **Severity overrides** | `broad-except` → `high` |

Profile: covers Django, Flask, and FastAPI apps. Bare `except:` clauses are elevated to **high** severity (API crash risk). Performance rules catch O(n) `list.pop(0)` in loops. `debt-todo-marker` is disabled (TODO comments in evolving APIs are expected).

---

### python-library-safe

| Field | Value |
|---|---|
| **Ecosystem** | python |
| **Quick Win Bias** | 0.6 |
| **Rules enabled** | `broad-except`, `trailing-whitespace`, `hardcoded-tmp-path`, `perf-pop-front-loop`, `perf-list-membership-loop`, `test-gap-missing-file` |
| **Rules disabled** | *(none)* |
| **Severity overrides** | *(none)* |

Profile: broadest Python ruleset. Adds `perf-list-membership-loop` (catching `list` used for O(n) membership tests where `set` would be O(1)). Highest quick-win bias (0.6) — libraries benefit from fast hygiene improvement. No test-coverage-branch rule since library test coverage patterns vary widely.

---

### rust-safe

| Field | Value |
|---|---|
| **Ecosystem** | rust |
| **Quick Win Bias** | 0.3 |
| **Rules enabled** | `debt-todo-marker` |
| **Rules disabled** | *(none)* |
| **Severity overrides** | *(none)* |

Profile: minimal, identical to `go-safe`. Detects TODO/FIXME markers. Rust's compiler already catches most issues at compile time, so the rule set is intentionally small.

---

## Template Selection Heuristics

### How Language Detection Works

The `OnboardEngine.detect_language()` method (in `bluei/app/onboarding.py`) uses a layered detection strategy:

1. **Marker files** — checks for language fingerprint files:
   - Python: `setup.py`, `pyproject.toml`, `requirements.txt`, `Pipfile`, `setup.cfg`
   - TypeScript: `tsconfig.json`
   - JavaScript: `package.json`, `.eslintrc`, `.prettierrc`
   - Go: `go.mod`, `go.sum`
   - Rust: `Cargo.toml`, `Cargo.lock`
   - Java: `pom.xml`, `build.gradle`, `build.gradle.kts`
   - Ruby: `Gemfile`, `Rakefile`
   - PHP: `composer.json`

2. **Dependency enrichment** — if `package.json` exists, it scans `dependencies` and `devDependencies` for framework markers and `@types/` packages to boost TypeScript score.

3. **Requirements enrichment** — if `requirements.txt` exists, scans for Python framework markers.

4. **File extension fallback** — if zero markers matched, scans repository root files: `.py`, `.ts`/`.tsx`, `.js`/`.jsx`.

5. **Minimum score filter** — languages must score ≥ 1 to appear in the ranked results. The highest-scoring language is the primary.

The standalone `detect_language()` function (also in `onboard.py`) is a faster version used for quick CLI auto-detection (e.g., `bluei init --path`).

For mixed-language repos (e.g., Python backend + TypeScript frontend), secondary languages are also recorded on the `LanguageInfo` object so baseline checks can be inferred for all detected languages.

### How Framework Detection Works

The `detect_frameworks()` function scans for specific artifacts:

**Python frameworks:**
- `django` — `manage.py` or any `**/settings.py` file
- `fastapi` — `from fastapi` or `import fastapi` in up to 20 `.py` files
- `flask` — `from flask` or `import flask` in up to 20 `.py` files
- `celery` — `import celery` / `from celery` in `.py` files or `celery` in `requirements.txt`

**TypeScript/JavaScript frameworks** (from `package.json`):
- `nextjs` — `next` in dependencies
- `express` — `express` in dependencies
- `react` — `react` in dependencies
- `nestjs` — `@nestjs/core` in dependencies
- `vitest` — `vitest` in dependencies

### How Template Is Selected

The `OnboardEngine.select_template()` method follows this decision tree:

```
Language = typescript/javascript?
├─ Monorepo detected?                   → node-workspace-root
├─ nextjs framework / next in deps?     → next-app
├─ react framework / react in deps?     → react-app
├─ express/nestjs / deps contain them?  → node-api
├─ build+test scripts exist?            → node-library
└─ package.json exists?                 → node-library (fallback)

Language = python?
├─ django framework?                    → django-app
├─ fastapi framework?                   → fastapi-app
├─ flask framework?                     → python-api
├─ manage.py exists?                    → django-app
├─ app.py or main.py exists?            → python-api
├─ setup.py/pyproject.toml exists?      → python-library
└─ else                                 → python-library (fallback)

Language = go?                          → go-service
Language = rust?                        → rust-crate
Otherwise                               → None (no template)
```

### How Rule Packs Are Matched

The `OnboardEngine.select_rule_pack()` method is a static mapping:

```python
node-library       → node-library-safe
node-api           → node-backend-safe
next-app           → node-frontend-safe
react-app          → node-frontend-safe
node-workspace-root → node-backend-safe
python-library     → python-library-safe
python-api         → python-api-safe
django-app         → python-api-safe
fastapi-app        → python-api-safe
go-service         → go-safe
rust-crate         → rust-safe
```

All three Python web frameworks (Django, Flask, FastAPI) share the same `python-api-safe` rule pack. The API-template variants only differ in baseline checks and limits.

---

## Onboarding Process

### The 13-Step Onboarding Wizard

The `OnboardEngine.onboard()` method runs a 13-step pipeline:

| Step | Action | Details |
|------|--------|---------|
| 1 | **Determine name** | Uses `--name` flag if provided, otherwise the directory name of `repo_path`. |
| 2 | **Duplicate check** | Rejects onboarding if the repo is already in the registry (`registry.find_by_path`). |
| 3 | **Detect language** | Runs `detect_language()` to build a `LanguageInfo` object. The `--language` flag can override the auto-detected primary language. |
| 4 | **Detect framework** | Runs `detect_framework()`. The `--framework` flag can override. |
| 5 | **Select plugin** | Picks the best plugin ID for the language (`select_plugin`). Falls back to the `--plugin-id` flag. |
| 6 | **Select template** | Runs `select_template()` to choose a repo template. Can be overridden with `--template`. |
| 7 | **Generate config** | Aggregates auto-detected values (baseline checks, discovery, fix strategy, safety) and deep-merges with the selected template. Produces a `RepoConfig`. |
| 8 | **Safety check** | If `require_clean_worktree` is true and `live_actions` is enabled, it rejects onboarding on a dirty worktree (unless `--allow-dirty-worktree` is passed). |
| 9 | **Create repo** | Registers the repo in `registry.yaml` and writes `config.yaml` to the per-repo state directory. |
| 10 | **Run discovery** | Executes the plugin's `discover()` to scan for initial findings. |
| 11 | **Calculate health** | Runs `HealthEngine.calculate()` on the initial findings. |
| 12 | **Capture baseline** | Saves the initial findings snapshot as a baseline for future trend comparison (unless `--no-baseline`). |
| 13 | **Return result** | Returns an `OnboardResult` with the repo object, baseline, health score, language info, findings count, suggested baseline checks, and review items. |

### Headless vs Interactive Onboarding

- **Headless** (`bluei init /path/to/repo`): Accepts all defaults. Suitable for CI or bulk onboarding. Uses auto-detected language, framework, template, and safety profile.
- **Interactive**: Prompts for name, language, framework, plugin, template, safety mode, and safety profile when not provided via flags. The CLI walks you through each choice step by step.

### What Each Step Does (in detail)

**Steps 3-4: Detection** — The language and framework detection functions are standalone and can also be called independently (e.g., `detect_language()` for quick gating in scripts). Package manager detection (`detect_package_manager`) resolves `npm`, `yarn`, `pnpm`, `bun`, `pip`, `poetry`, `uv`, or `pipenv`. Build tool detection (`detect_build_tool`) resolves `package-scripts`, `pyproject`, `setuptools`, `go`, or `cargo`.

**Step 6: Template selection** — Uses the decision tree detailed in the previous section. Monorepo detection (`detect_monorepo`) checks for npm `workspaces` and `pnpm-workspace.yaml`.

**Step 7: Config generation** — `generate_config()` delegates to `ConfigManager.render_config_from_template()`, which:
1. Loads the selected template YAML.
2. Deep-merges the template with auto-detected overrides (baseline checks, discovery hints, fix strategy).
3. Adds metadata (`onboarding_version`, `template` name, `inferred_by`, `secondary_languages`, `detected_frameworks`, `framework_detection_date`).
4. Returns a `RepoConfig` object, assigning a `repo-<name>` ID.

**Step 8: Safety policy** — `infer_safety_policy()` resolves:
- Mode: one of `observe`, `issue-only`, `pr`, `merge`
- Profile: one of `conservative`, `balanced`, `aggressive`
- Protected branches: defaults to `main` and `master`, plus the remote HEAD if detected
- Notes: human-readable descriptions of what each mode/profile allows

**Steps 10-12: Baseline** — The baseline captures what the repo looked like when first onboarded. Future scans are compared against this baseline to calculate trend metrics (improving/regressing). Baselines are stored as JSON in the per-repo state directory.

### Config Generation Process

The pipeline for config generation:

```
CLI flags (highest priority)
    │
    ▼
Detection overrides (baseline_checks, discovery, fix_strategy)
    │
    ▼
Selected repo template (deep-merged as base)
    │
    ▼
templates/config.yaml.template (global defaults)
    │
    ▼
RepoConfig validated and written to repos/<name>/config.yaml
```

---

## Config Schema

### What `config.yaml.template` Defines

The file `templates/config.yaml.template` is a Jinja2-style template (using `{{ }}` placeholders) that defines the canonical shape of every repo's `config.yaml`. It provides the global defaults used when a repo template doesn't specify a particular key.

Full schema:

```yaml
repo:
  id: {{ id }}                    # auto-generated unique ID (e.g., "repo-20260101-xxx")
  name: {{ name }}                # display name for CLI and registry
  path: {{ path }}                # absolute filesystem path
  language: {{ language }}        # primary language (python, typescript, javascript, go, rust)
  framework: {{ framework }}      # auto-detected or overridden (django, fastapi, flask, etc.)
  enabled: true                   # toggle to enable/disable this repo's scans
  plugin_id: {{ plugin_id }}      # plugin identifier (python, typescript, go, rust)

discovery:
  internal: true                  # use internal discovery engine
  external_script: null           # path to optional external discovery script
  skip_internal: false            # skip internal discovery entirely

rules:
  enabled: null                   # list of rule IDs to enable (null = use rule pack defaults)
  disabled: []                    # list of rule IDs to disable

fix:
  engine: deterministic           # fix engine: deterministic, claude, opencode, auto
  claude_template: ''             # command template for Claude-based fixes

validation:
  baseline_checks: []             # list of shell commands run as validation
  allow_unchanged_baseline_failures: true  # tolerate known pre-existing failures

limits:
  open_issues_cap: 20             # max open issues allowed
  open_prs_cap: 5                 # max open PRs allowed
  max_prs_per_run: 2              # max PRs created per scan
  max_issues_per_run: 10          # max issues created per scan
  max_files_changed: 5            # max files modified per fix
  max_loc_diff: 200               # max lines-of-code changed per fix
  max_fix_attempts: 3             # max retry attempts per finding

cooldowns:
  finding_seconds: 14400          # cooldown between finding re-emissions (4 hours)
  merge_minutes: 30               # cooldown after merge before next action
  staleness_seconds: 7200         # staleness window (2 hours)

github:
  live_actions: false             # enable real GitHub issue/PR creation
  auto_merge: false               # enable auto-merge of bluei PRs
```

### Key Sections

#### `discovery`
Controls how findings are discovered. `internal: true` uses bluei's built-in scanning plugins. `external_script` can point to a custom discovery script. `skip_internal` disables the built-in scanner when you only want external scripts.

#### `limits`
Fine-grained caps that constrain how much bluei can change in a single run. These are the "blast radius controls"—no single fix can exceed the per-fix limits, and no single run can exceed the aggregate caps. The repo templates set tighter limits than the global defaults (e.g., `fastapi-app` caps at 10 issues and 150 LOC, vs. the 20/200 global defaults).

#### `safety`
Not present in `config.yaml.template` but injected during onboarding by `infer_safety_policy()`. Contains:
- `mode`: execution mode (observe, issue-only, pr, merge)
- `profile`: safety profile (conservative, balanced, aggressive)
- `require_clean_worktree`: prevents live runs on dirty worktrees
- `protected_branches`: branches that get extra safety gating

#### `github`
Controls real GitHub actions. `live_actions` gates all write operations—when false, everything is a dry run. `auto_merge` is always defaulted to `false` during onboarding regardless of safety mode.

### Rule Pack Integration

When a repo template is selected, its `rule_pack` field links to a rule pack. The `rules` section in the generated config can override the pack:

- `rules.enabled: null` — use the rule pack's defaults as-is
- `rules.enabled: [custom-rule]` — append additional rules
- `rules.disabled: [rule-to-skip]` — suppress specific rules from the pack
- `severity_overrides` — adjust rule severities (applied during `OnboardEngine.build_review_items()`)

This allows repos to start with a rule pack baseline and then customize—e.g., a Flask API might start with `python-api-safe` but disable `test-coverage-branch` if it doesn't use coverage tooling.

---

## CI Workflow Template

The file `templates/ci.yml.j2` is a Jinja2 template that generates a GitHub Actions workflow. Invoke it with:

```bash
bluei ci <repo_name>
```

### Template Variables

| Variable | Description |
|----------|-------------|
| `{{ repo_name }}` | Name of the repository (used in the workflow comment) |
| `__BLUEI_INSTALL_PATH__` | Replaced with the actual bluei install path at generation time |
| `${{ github.event.inputs.phase }}` | Manually triggered phase selection (GitHub Actions variable) |

### What the Generated Workflow Does

1. **Triggers on:** push to `main`/`master`, pull requests to `main`/`master`, and manual `workflow_dispatch`.
2. **Manual dispatch options:** `issue-cycle`, `pr-cycle`, `review-cycle`, `merge-cycle`, `orchestrated`
3. **Environment:** Python 3.11 on Ubuntu latest, 30-minute timeout
4. **Permissions:** `contents: read`, `issues: write`, `pull-requests: write`, `checks: write`

**Steps:**

| Step | Action |
|------|--------|
| Checkout | `actions/checkout@v4` with full depth (needed for git-based discovery) |
| Setup Python | `actions/setup-python@v5` with Python 3.11 |
| Install bluei | `pip install -e` from the install path |
| System deps | Installs `shellcheck`, optionally `markdownlint-cli` and `actionlint` |
| Run scan | `bluei scan . --phase <selected> --format json`, continues on error |
| Upload artifact | Saves `bluei-output.json`, `.bluei/state/*.json`, `.bluei/logs/*.log` with 14-day retention |
| Post summary | Writes a truncated JSON summary to `$GITHUB_STEP_SUMMARY` |

The scan's `continue-on-error: true` means a scan that finds issues still allows the workflow to succeed—bluei failures should not block CI.

---

## Customization

### How to Create Custom Templates

To add a new repo template (e.g., for a Java Spring Boot app):

1. Create `templates/repos/spring-boot.yaml`:

```yaml
plugin_id: java
enabled: true
fix_engine: deterministic
framework: spring-boot
rule_pack: java-api-safe
discovery:
  ecosystem: java
  max_findings: 50
baseline_checks:
  - ["./gradlew", "test"]
limits:
  max_files_changed: 3
  max_loc_diff: 150
  max_fix_attempts: 2
  open_issues_cap: 10
  open_prs_cap: 2
safety:
  mode: observe
  profile: conservative
meta:
  template_version: 2
  ecosystem: java
  template_type: api
```

2. Create a corresponding rule pack `templates/rules/java-api-safe.yaml` if needed.

3. Add a selection rule in `OnboardEngine.select_template()` to match the new language/framework.

4. Add a rule pack mapping entry in `OnboardEngine.select_rule_pack()`.

### How to Create Custom Rule Packs

1. Create `templates/rules/<name>.yaml`:

```yaml
name: my-custom-pack
description: Custom rule pack for my project
ecosystem: python
rules_enabled:
  - trailing-whitespace
  - broad-except
rules_disabled:
  - debt-todo-marker
severity_overrides:
  broad-except: medium
quick_win_bias: 0.5
```

2. Reference it in a repo template or override via the per-repo `config.yaml`:

```yaml
rules:
  enabled: null          # use pack defaults
  disabled: []           # additional disabled rules
```

### Template Deep Merge Behavior

The `_deep_merge()` function in `bluei/app/config.py` implements recursive dictionary merging:

```python
def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)  # recurse into nested dicts
        else:
            result[key] = val  # overwrite scalar values
    return result
```

**Merge order during onboarding:**

1. Template YAML (base)
2. Detection overrides (baseline_checks, discovery, fix_strategy)
3. CLI flag overrides (top priority)
4. Metadata injected last

This means:
- Lists (like `baseline_checks`) are **replaced**, not appended — the onboarding engine explicitly deduplicates and merges them before passing to the template renderer.
- Nested dicts (like `discovery`, `limits`) are deeply merged — individual keys like `max_findings` or `use_docker` can be overridden without losing other keys in the same section.
- Scalar values (like `fix_engine`, `framework`) are simply overwritten by the higher-priority source.
