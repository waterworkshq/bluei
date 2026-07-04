<!-- CATEGORY: reference -->

# CLI Reference

## Overview

`bluei` is the command-line interface for the autonomous QA agent. The entry point is
`bin/bluei` (a Python shebang wrapper), which delegates to `bin/bluei.py` — the CLI
dispatcher. The dispatcher handles venv bootstrapping, command parsing, help text
rendering, and dispatch to the canonical `bluei` app and engine modules.

**Invocation chain:**

```
bin/bluei (bash shebang)
  → bin/bluei.py (CLI dispatcher — 592 lines, ~27 commands)
    → bluei/app/ (runner, config, health, registry, etc.)
      → bluei/engine/ (execution engine)
        → repo worktrees / GitHub / linters / LLM fixers
```

The dispatcher auto-bootstraps the `.venv/` on first run and validates argument
shapes before handing off to the engine via `subprocess`. Commands follow one of
four invocation patterns:

| Pattern | Example commands | How they pass arguments |
|---------|-----------------|------------------------|
| Zero-arg | `init`, `install`, `update`, `dashboard`, `languages` | Handled inline or dispatched to engine |
| Named-project | `scan`, `clean`, `duster`, `run`, `health`, `report`, `ci`, `install-hook`, `install-cron` | Positional `<name>` + flags forwarded to engine |
| Named-argument | `onboard`, `preflight`, `heal`, `patterns`, `campaign`, `emergent`, `lesson`, `upgrade-config` | Uses `--repo`, `--mode`, etc. |
| Path | `secrets` | Positional `<path>` argument |

---

## Command Index

All 29 commands sorted alphabetically:

| # | Command | Description |
|---|---------|-------------|
| 1 | `benchmark` | Run the dev Benchmark Harness over the Seed Library corpus (coverage gap analysis + Flywheel Score) |
| 2 | `campaign` | Plan and execute multi-finding refactor campaigns |
| 3 | `ci` | Generate GitHub Actions CI workflow |
| 4 | `clean` | Fix specks and open pull requests |
| 5 | `dashboard` | Generate a read-only observability dashboard |
| 6 | `doctor` | Run operational checks/diagnostics |
| 7 | `duster` | Dry-run scan — preview specks, no changes |
| 8 | `emergent` | Inspect and manage proposed emergent rules |
| 9 | `heal` | Heal dirty worktrees from transient artifacts |
| 10 | `health` | Show vitality score (0–100) |
| 11 | `help` | Show help for a command or overview |
| 12 | `init` | Register a project (interactive or `--path`) |
| 13 | `install` | Print installation/setup instructions |
| 14 | `install-cron` | Install host cron schedule for project cycles |
| 15 | `install-hook` | Install pre-commit hook |
| 16 | `languages` | List installed language packs and rules |
| 17 | `learn` | Inspect governance state, pending approvals, and create Golden Validation Bundles |
| 18 | `lesson` | Inspect and manage per-repo lesson entries |
| 19 | `onboard` | Headless onboarding for a new project |
| 20 | `patterns` | Inspect and manage learned fix patterns |
| 21 | `preflight` | Assess a project before registering |
| 22 | `report` | Generate a vitality report (PDF by default) |
| 23 | `run` | Run full orchestrated sweep (scan → clean → verify) |
| 24 | `scan` | Run discovery/issue-cycle scan on a project |
| 25 | `secrets` | Scan for hardcoded secrets |
| 26 | `status` | Show agent or project status |
| 27 | `update` | Self-update to the latest version |
| 28 | `upgrade-config` | Upgrade repo configuration to latest schema |
| 29 | `version` | Print version info (`--version` or `-v` flag) |

> **Alias:** `setup` is an alias for `install`.

---

## Per-Command Reference

### `benchmark`

Run the dev Benchmark Harness over the committed Seed Library corpus. Replays
Golden Bundles + seeded Patterns + Recipes through the cascade-simulation
proxy and the Model Governor, producing a per-rule-family coverage gap
analysis + Flywheel Score. **Internal dev tool** — not user-facing; drives the
Deterministic Flywheel improvement loop (gaps identify where to add Patterns,
Recipes, or Bundles). Static analysis only — no model invocation.

**Syntax:**

```
bluei benchmark [options]
```

**Flags:**

| Flag | Description |
|------|-------------|
| `--output <path>` | Write the report to a file (markdown) instead of stdout |
| `--policy <yaml>` | Override the default `CoveragePolicy` thresholds |

**Output:** a per-rule-family table (deterministic-resolved / Governor-reached /
tier distribution / gap flag) + a Flywheel Score breakdown ($ avoided per
Finding under new-vs-old routing via mocked rates). Reproducible across runs.

> See ADR-0022 (Model Governor) + CONTEXT.md "Benchmark Harness" / "Flywheel Score".

---

### 1. `campaign`

Plan and execute multi-finding refactor campaigns. Campaigns group related
findings into ordered phases so larger cleanup work can be planned, reviewed,
paused, resumed, or aborted.

**Syntax:**

```
bluei campaign <subcommand> --repo <name> [options]
```

**Subcommands:**

| Subcommand | Description |
|------------|-------------|
| `plan` | Read current findings and print an ordered campaign plan |
| `list` | List saved campaign plans |
| `status <id>` | Show a saved campaign plan with progress |
| `events <id>` | Show recent campaign event history |
| `run <id>` | Walk or guardedly execute a saved plan |
| `pause <id>` | Pause a saved campaign plan |
| `resume <id>` | Resume a paused campaign plan |
| `abort <id>` | Abort a saved campaign plan |

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--repo <name>` | string | *(required)* | Repository name |
| `--rules <a,b,c>` | string | — | Comma-separated rule IDs to include (plan) |
| `--paths <glob[,...]>` | string | — | Comma-separated path globs to include (plan) |
| `--strategy <name>` | string | `rule_based` | Planning strategy (plan) |
| `--save` | flag | — | Persist a generated plan to state/campaigns |
| `--dry-run` | flag | — | Required for campaign run (no mutations) |
| `--allow-mutate` | flag | — | Allow real campaign fix execution |
| `--worktree <dir>` | string | — | Worktree path required with `--allow-mutate` |
| `--reason <text>` | string | — | Operator reason for pause/abort |
| `--limit <n>` | int | `20` | Number of recent events to show |
| `--state-root <dir>` | string | — | Override repos state root for tests/tools |

**Examples:**

```bash
# Plan a campaign for specific rules
bluei campaign plan --repo my-app --rules type-explicit-any --paths "src/api/**"

# Save and later dry-run
bluei campaign plan --repo my-app --rules type-explicit-any --save
bluei campaign run camp-20260518-abc123 --repo my-app --dry-run

# Live execution with worktree
bluei campaign run camp-20260518-abc123 --repo my-app --allow-mutate --worktree /tmp/wt

# Lifecycle management
bluei campaign pause camp-20260518-abc123 --repo my-app --reason "manual review"
bluei campaign resume camp-20260518-abc123 --repo my-app
bluei campaign abort camp-20260518-abc123 --repo my-app --reason "scope changed"
```

**Exit codes:** `0` (success), `1` (error/invalid args), `2` (campaign aborted — no matching findings)

---

### 2. `ci`

Generate a GitHub Actions CI workflow (`.github/workflows/bluei.yml`).

**Syntax:**

```
bluei ci <project-name> [--force]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--force` | flag | — | Overwrite existing workflow file |

> Any additional flags are passed through to the engine.

**Example:**

```bash
bluei ci my-project > .github/workflows/bluei.yml
bluei ci my-project --force
```

**Exit codes:** `0` (success), `1` (error)

---

### 3. `clean`

Fix specks and open pull requests. Creates issues AND opens PRs with automated
fixes. (Contrast with `scan` which only creates issues.)

**Syntax:**

```
bluei clean <project-name> [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--fix-engine <engine>` | string | `auto` | Fix engine: `auto`, `deterministic`, `claude`, `opencode` |

> Additional flags are passed through to the engine.

**Engine mapping:** `clean` dispatches to the engine as `run --repo <name> --phase pr-cycle --no-dry-run`.

**Examples:**

```bash
bluei clean my-app
bluei clean my-app --fix-engine claude
```

**Exit codes:** `0` (success), `1` (error or missing project name)

---

### 4. `dashboard`

Generate a read-only, self-contained observability dashboard from repo state.
Does not collect new data or run fixes.

**Syntax:**

```
bluei dashboard [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--repo <name>` | string | — | Limit dashboard data to one repo |
| `--format <fmt>` | string | `html` | Output format: `html` or `json` |
| `--output, -o <path>` | string | `bluei-dashboard.{ext}` | Output file path |
| `--state-root <dir>` | string | — | Override repos state root for tests/tools |
| `--open` | flag | — | Open the dashboard in the default browser after writing |
| `--watch` | flag | — | Regenerate on interval (with `--interval=<n>`, default 30s) |

**Examples:**

```bash
bluei dashboard
bluei dashboard --repo my-app -o dashboard.html
bluei dashboard --format json -o data.json
bluei dashboard --open --watch --interval=60
```

**Exit codes:** `0` (success), `1` (error)

---

### 5. `doctor`

Run operational diagnostics on a project or the whole agent. Checks
configuration, dependencies, tool availability, and more.

**Syntax:**

```
bluei doctor [<project-name>] [options]
```

**Examples:**

```bash
bluei doctor              # Check all projects
bluei doctor my-app       # Check a specific project
```

**Exit codes:** `0` (success), `1` (error)

---

### 6. `duster`

A duster is a gentle, non-destructive way to remove dust. Scans the project for
specks and reports what it finds without creating any issues, PRs, or making any
changes. Equivalent to `scan` with `--dry-run`.

**Syntax:**

```
bluei duster <project-name>
```

**Engine mapping:** `run --repo <name> --phase issue-cycle --dry-run`.

**Example:**

```bash
bluei duster my-app
```

**Exit codes:** `0` (success), `1` (error or missing project name)

---

### 7. `emergent`

Inspect and manage emergent rules — detection rules that bluei has proposed from
observing repeated findings and fix pattern successes. Rules progress through a
lifecycle: `proposed → validated → candidate → tentative → active`.

**Syntax:**

```
bluei emergent <subcommand> --repo <name> [options]
```

**Subcommands:**

| Subcommand | Description |
|------------|-------------|
| `propose` | Create/update proposed rules from current findings or fix patterns |
| `validate` | Promote safe proposals to candidates, rejecting those covered by built-ins |
| `scan` | Run candidate/tentative/active rules in shadow mode against a worktree |
| `discover` | Write findings from active emergent rules into repo state |
| `shadow <id>` | Record manual shadow-mode evidence for a candidate |
| `approve <id>` | Manually approve a rule to active status |
| `reject <id>` | Reject a rule proposal or candidate |
| `retire <id>` | Retire a rule without deleting its evidence |
| `promote <id>` | Retire an emergent rule and write it as a hand-authored plugin |
| `gc` | Retire stale rules (old active or proposed) |
| `list` | List proposed emergent rules |
| `show <id>` | Show full rule details |

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--repo <name>` | string | *(required)* | Repository name |
| `--min-observations <n>` | int | `5` | Minimum repeated findings before proposal |
| `--from-patterns` | flag | — | Propose from successful fix patterns instead of findings |
| `--min-success-count <n>` | int | `5` | Minimum fix pattern successes before proposal |
| `--run-id <id>` | string | `manual` | Evidence run identifier |
| `--worktree <path>` | string | — | Worktree path for shadow scanning/discovery |
| `--reason <text>` | string | — | Lifecycle transition reason |
| `--matches <n>` | int | `0` | Shadow matches observed |
| `--false-positives <n>` | int | `0` | Shadow matches judged false-positive |
| `--min-shadow-runs <n>` | int | `3` | Shadow runs before active/rejected decision |
| `--plugin-dir <dir>` | string | — | Plugin directory for promote (writes plugin.yaml) |
| `--state-root <dir>` | string | — | Override repos state root for tests/tools |

**Examples:**

```bash
# Propose rules from repeated findings
bluei emergent propose --repo my-app --min-observations 5

# Propose from successful fix patterns
bluei emergent propose --repo my-app --from-patterns --min-success-count 5

# Validate proposals and shadow-scan
bluei emergent validate --repo my-app
bluei emergent scan --repo my-app --worktree /tmp/wt

# Manual shadow evidence
bluei emergent shadow er-abc123 --repo my-app --matches 12 --false-positives 0

# Approve and promote to plugin
bluei emergent approve er-abc123 --repo my-app
bluei emergent promote er-abc123 --repo my-app --plugin-dir plugins/python/

# Garbage-collect stale rules
bluei emergent gc --repo my-app
```

**Exit codes:** `0` (success), `1` (error/invalid args)

---

### 8. `heal`

Heal a dirty worktree caused by transient artifacts (pycache, lock files, stale
worktrees). By default runs as a dry-run — add `--no-dry-run` to actually heal.

**Syntax:**

```
bluei heal --repo <path> [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--no-dry-run` | flag | — | Actually heal (default: dry-run only) |
| `--remove-artifacts` | flag | — | Also remove transient artifact directory/files |

**Examples:**

```bash
bluei heal --repo my-app                  # Dry-run preview
bluei heal --repo my-app --no-dry-run      # Execute healing
bluei heal --repo my-app --no-dry-run --remove-artifacts
```

**Exit codes:** `0` (success), `1` (error)

---

### 9. `health`

Show the vitality score (0–100) for a project.

**Syntax:**

```
bluei health <project-name> [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--days <n>` | int | `30` | Days of history |
| `--format <fmt>` | string | `text` | Output format: `text` or `whatsapp` |

> Additional flags are passed through to the engine.

**Examples:**

```bash
bluei health my-app
bluei health my-app --days 7
bluei health my-app --format whatsapp
```

**Exit codes:** `0` (success), `1` (error or missing project name)

---

### 10. `help`

Show help text.

**Syntax:**

```
bluei help [<command>]
bluei <command> --help
bluei <command> -h
```

**Example:**

```bash
bluei help
bluei help scan
bluei campaign --help
```

**Exit codes:** `0` (always)

---

### 11. `init`

Register a project with bluei. When run without `--path`, launches an
interactive first-run wizard. With `--path`, performs quick auto-registration:
auto-detects git remote, language, prompts for care level, and registers in the
local registry.

**Syntax:**

```
bluei init [--path <dir>] [--name <name>]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--path, -p <dir>` | string | cwd | Path to a git project |
| `--name, -n <name>` | string | dir name | Custom project name |

**Care levels (interactive prompt):**

| Choice | Mode | Behavior |
|--------|------|----------|
| 1 | Watch only | Dry-run only, no GitHub interaction |
| 2 | Note only | Creates issues, no PRs |
| 3 | Offer fixes | Issues + PRs, no auto-merge |
| 4 | Full care | Full autonomy: issues, PRs, merges |

**Examples:**

```bash
bluei init                          # Interactive wizard
bluei init --path ~/my-project      # Quick auto-registration
bluei init -p . -n my-app           # Register cwd with custom name
```

**Exit codes:** `0` (success), `1` (error — path not found, registration failure)

---

### 12. `install`

Print installation/setup instructions.

**Syntax:**

```
bluei install
bluei setup     # alias
```

**Exit codes:** `0` (always)

---

### 13. `install-cron`

Install host cron schedule for project cycle automation. Sets up crontab entries
for issue-cycle, PR-cycle, review-cycle, and merge-cycle.

**Syntax:**

```
bluei install-cron <project-name> [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--issue-schedule <cron>` | string | `0 */4 * * *` | Issue cycle schedule |
| `--pr-schedule <cron>` | string | `0 */6 * * *` | PR cycle schedule |
| `--review-schedule <cron>` | string | `30 * * * *` | Review cycle schedule |
| `--merge-schedule <cron>` | string | `0 6,18 * * *` | Merge cycle schedule |

> Additional flags are passed through to the engine.

**Example:**

```bash
bluei install-cron my-app
bluei install-cron my-app --issue-schedule "0 */2 * * *"
```

**Exit codes:** `0` (success), `1` (error or missing project name)

---

### 14. `install-hook`

Install a pre-commit hook that runs `bluei duster` on every commit.

**Syntax:**

```
bluei install-hook <project-name> [--force]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--force` | flag | — | Overwrite existing hook |

> Additional flags are passed through to the engine.

**Example:**

```bash
bluei install-hook my-app
bluei install-hook my-app --force
```

**Exit codes:** `0` (success), `1` (error or missing project name)

---

### 15. `languages`

List installed language packs and their declared detector rules. Each pack shows
its supported languages, rule count, and per-rule metadata (category, confidence,
autofix capability).

**Syntax:**

```
bluei languages
```

**Example output:**

```
go [go] 15 rule(s)
  S1000 category=simplify confidence=0.90 autofix=False
  SA4006 category=bug confidence=0.85 autofix=False
python [python] 23 rule(s)
  B004 category=bug confidence=0.75 autofix=False
  ...
```

**Exit codes:** `0` (success)

---

### 16. `learn`

Inspect governance state, pending approvals, decision history, and create Golden Validation Bundles from known-good fixes. The unified read/inbox surface for the Operator Control Plane. Write-verbs (pause, resume, retire) stay in their native namespaces (`patterns`, `emergent`).

**Syntax:**

```
bluei learn <subcommand> [options]
```

**Subcommands:**

| Subcommand | Description |
|------------|-------------|
| `inbox [--repo <name>]` | List pending governance approvals (assets awaiting operator decision) |
| `status <asset_ref> [--repo <name>]` | Show governance state, native state, and recent SPRT evidence for a Governed Asset |
| `audit <asset_ref> [--repo <name>]` | Show full decision history (all ApprovalRecords) for a Governed Asset |
| `bundle <pattern_id> --worktree <path> --from-finding <finding_id> [--repo <name>]` | Create a Golden Validation Bundle from a known-good fix in a worktree |

**Asset Reference format:** `<class>:<id>` — e.g., `pattern:fp-a1b2c3d4`, `recipe:auto-ruff-b904-...`, `transform:python-ruff-F401`, `emergent_rule:er-abc123`.

**Examples:**

```bash
# Check for pending promotions
bluei learn inbox --repo my-project

# Inspect a Pattern's governance state + SPRT evidence
bluei learn status pattern:fp-a1b2c3d4 --repo my-project

# Full audit trail for a Recipe
bluei learn audit recipe:auto-ruff-b904-a1b2c3d4 --repo my-project

# Create a bundle from a successful fix
bluei learn bundle fp-a1b2c3d4 --worktree /path/to/worktree --from-finding f-001 --repo my-project
```

**Exit codes:** 0 success, 1 not found / error, 2 missing required arguments.

---

### 17. `lesson`

Inspect and manage per-repo lesson entries that feed the fix feedback loop.
Lessons record what broke, what changed, and what worked for cross-finding hints.

**Syntax:**

```
bluei lesson <subcommand> --repo <name> [options]
```

**Subcommands:**

| Subcommand | Description |
|------------|-------------|
| `add` | Write a manual lesson entry (cross-finding hint) |
| `list` | List recent lessons, optionally filtered by rule |
| `show <id>` | Show lesson entries for a specific finding_id |

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--repo <name>` | string | *(required)* | Repository name |
| `--broke <text>` | string | — | What broke or failed (add) |
| `--changed <text>` | string | — | What was changed successfully (add) |
| `--worked <text>` | string | — | What worked or was accomplished (add) |
| `--finding-id <id>` | string | — | Tag entry with a specific finding_id (add) |
| `--rule <rule>` | string | — | Filter by rule name (list) |
| `--limit <n>` | int | `20` | Number of entries to show (list) |

**Examples:**

```bash
bluei lesson add --repo my-app --broke "ruff-b904 bare raises need from AppError(...)"
bluei lesson add --repo my-app --changed "disable xo-complexity for legacy files"
bluei lesson list --repo my-app --rule ruff-b904
bluei lesson show abc123def --repo my-app
```

**Exit codes:** `0` (success), `1` (error/invalid args)

---

### 18. `onboard`

Headless onboarding for an existing local project. Registers the repo with
custom care level and profile without interactive prompts.

**Syntax:**

```
bluei onboard --repo <path> [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--name <name>` | string | dir name | Custom project name |
| `--language <lang>` | string | auto-detect | Force language detection |
| `--mode <mode>` | string | — | Care level: `watch-only`, `note-only`, `offer-fixes`, `full-care` |
| `--profile <profile>` | string | — | Care style: `conservative`, `balanced`, `aggressive` |
| `--skip-baseline` | flag | — | Skip starting point capture |
| `--allow-dirty-worktree` | flag | — | Allow onboarding with dirty working tree |

> Additional flags are passed through to the engine.

**Example:**

```bash
bluei onboard --repo ~/my-project --mode watch-only --profile conservative
```

**Exit codes:** `0` (success), `1` (error)

---

### 19. `patterns`

Inspect and manage learned fix patterns. bluei records successful fix patterns
and can reuse them before spending another LLM call on similar findings.

**Syntax:**

```
bluei patterns <subcommand> --repo <name> [options]
```

**Subcommands:**

| Subcommand | Description |
|------------|-------------|
| `list` | List all active patterns with confidence and stats |
| `show <id>` | Show full pattern details including before/after/diff |
| `deactivate <id>` | Manually deactivate a pattern (set confidence to 0) |
| `reactivate <id>` | Reactivate a deactivated pattern (set confidence to 0.5) |

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--repo <name>` | string | *(required)* | Project name |

**Examples:**

```bash
bluei patterns list --repo my-app
bluei patterns show fp-a1b2c3d4e5f6 --repo my-app
bluei patterns deactivate fp-a1b2c3d4e5f6 --repo my-app
bluei patterns reactivate fp-a1b2c3d4e5f6 --repo my-app
```

**Exit codes:** `0` (success), `1` (error/invalid args)

---

### 20. `preflight`

Assess whether a project is ready for bluei registration. Runs checks before
onboarding to surface potential issues.

**Syntax:**

```
bluei preflight --repo <path>
```

**Example:**

```bash
bluei preflight --repo /path/to/new-project
```

**Exit codes:** `0` (ready), `1` (error)

---

### 21. `report`

Generate a comprehensive vitality report for a project. PDF by default. Also
supports text, JSON, HTML, and WhatsApp formats.

**Syntax:**

```
bluei report <project-name> [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--format <fmt>` | string | `pdf` | Output format: `pdf`, `text`, `json`, `html`, `whatsapp` |
| `--output, -o <path>` | string | — | Output file path |
| `--days <n>` | int | `30` | Days of history |

> Additional flags are passed through to the engine.

**Examples:**

```bash
bluei report my-app                            # PDF (default)
bluei report my-app --format html              # HTML dashboard
bluei report my-app --format json -o data.json # JSON export
bluei report my-app --format text --days 7     # Last 7 days
```

**Exit codes:** `0` (success), `1` (error or missing project name)

---

### 22. `run`

Run a full orchestrated sweep (discover, fix, review, merge). Equivalent to
running `scan → clean → verify` in sequence.

**Syntax:**

```
bluei run <project-name> [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--dry-run` | flag | — | Preview without making changes |
| `--fix-engine <engine>` | string | `auto` | Fix engine: `auto`, `deterministic`, `claude`, `opencode` |

**Engine mapping:** `run --repo <name> --phase orchestrated --no-dry-run`.

> Additional flags are passed through to the engine.

**Examples:**

```bash
bluei run my-app                     # Full sweep
bluei run my-app --dry-run           # Preview only
bluei run my-app --fix-engine claude # Use Claude for fixes
```

**Exit codes:** `0` (success), `1` (error or missing project name)

---

### 23. `scan`

Run a discovery/issue-cycle scan on a project. Discovers specks and creates
GitHub issues for them. (Contrast with `clean` which also opens PRs.)

**Syntax:**

```
bluei scan <project-name> [options]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--fix-engine <engine>` | string | `auto` | Fix engine: `auto`, `deterministic`, `claude`, `opencode` |

**Engine mapping:** `run --repo <name> --phase issue-cycle --no-dry-run`.

> Additional flags are passed through to the engine.

**Examples:**

```bash
bluei scan my-app
bluei scan my-app --fix-engine deterministic
```

**Exit codes:** `0` (success), `1` (error or missing project name)

---

### 24. `secrets`

Scan a directory for hardcoded secrets (API keys, tokens, passwords, private
keys, high-entropy strings). Delegates to `bin/bluei_secrets.py`.

**Syntax:**

```
bluei secrets <path>
```

**Example:**

```bash
bluei secrets ./src
bluei secrets ~/my-project
```

**Exit codes:** `0` (success), `1` (error or missing path)

---

### 25. `status`

Show agent-wide status or per-project status.

**Syntax:**

```
bluei status [--repo <name>]
```

**Flags:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--repo <name>` | string | — | Show status for a specific project |

**Examples:**

```bash
bluei status                        # Agent-wide status
bluei status --repo my-app          # Per-project status
```

**Exit codes:** `0` (success), `1` (error)

---

### 26. `update`

Self-update bluei to the latest version. Delegates to the engine's update
mechanism.

**Syntax:**

```
bluei update
```

**Exit codes:** `0` (success), `1` (error)

---

### 27. `upgrade-config`

Upgrade a repository's configuration to the latest schema. Uses the
`OnboardEngine.upgrade_config()` path.

**Syntax:**

```
bluei upgrade-config --repo <name>
```

**Example:**

```bash
bluei upgrade-config --repo my-app
```

**Exit codes:** `0` (success), `1` (error or missing `--repo`)

---

### 28. `version`

Print version information for the bluei CLI and the engine backend.

**Syntax:**

```
bluei --version
bluei -v
```

**Example output:**

```
bluei version 2.6.0
```

**Exit codes:** `0` (always)

---

## Exit Codes

The bluei CLI dispatcher returns the following exit codes:

| Code | Meaning | When |
|------|---------|------|
| `0` | Success | Command completed successfully |
| `1` | General failure | Invalid arguments, missing required parameters, unknown command, engine failure, path not found |
| `2` | Campaign aborted | Campaign plan returned with `aborted` status (no matching findings) |

> **Note:** The engine (`bluei/engine/`) may return
> additional exit codes not tracked by the dispatcher. Safety gates within the
> fix pipeline (e.g., tiered validation in `fix_tiers.py`) use internal result
> objects rather than process exit codes.

---

## Common Command Sequences

### Onboard and assess a new project

```bash
# 1. Check if the project is ready
bluei preflight --repo ~/my-project

# 2. Onboard with conservative settings
bluei onboard --repo ~/my-project --mode watch-only --profile conservative

# 3. Preview specks (dry-run)
bluei duster my-project

# 4. Check health
bluei health my-project
```

### Graduated adoption: watch → note → fix → full care

```bash
# Start safe — scan only, preview everything
bluei scan my-project
bluei duster my-project

# Once comfortable: create issues
bluei onboard --repo ~/my-project --mode note-only

# Graduate to fixes
bluei onboard --repo ~/my-project --mode offer-fixes
bluei clean my-project

# Full autonomy (requires trust)
bluei onboard --repo ~/my-project --mode full-care
bluei run my-project
```

### Monitoring and reporting

```bash
# Daily health check
bluei health my-project --days 1

# Weekly report
bluei report my-project --format html --days 7 -o weekly.html

# Agent-wide overview
bluei status
bluei dashboard --open

# Check what detectors are available
bluei languages
```

### Automating with cron

```bash
# Set up scheduled scanning
bluei install-cron my-project

# Override schedules for high-traffic repos
bluei install-cron my-project \
  --issue-schedule "0 */2 * * *" \
  --pr-schedule "0 */4 * * *"

# Verify + status
crontab -l
bluei status --repo my-project
```

### Worktree hygiene

```bash
# Check for dirty worktrees (dry-run)
bluei heal --repo my-project

# Actually clean up
bluei heal --repo my-project --no-dry-run --remove-artifacts
```

### Pattern and rule management

```bash
# Inspect what bluei has learned
bluei patterns list --repo my-app

# Drill into a pattern
bluei patterns show fp-a1b2c3d4e5f6 --repo my-app

# Manage emergent rules
bluei emergent list --repo my-app
bluei emergent propose --repo my-app
bluei emergent validate --repo my-app
bluei emergent approve er-abc123 --repo my-app

# Record lessons manually
bluei lesson add --repo my-app --worked "ruff-b904 fix applied cleanly"
bluei lesson list --repo my-app
```

### CI/CD integration

```bash
# Generate GitHub Actions workflow
bluei ci my-project > .github/workflows/bluei.yml

# Install pre-commit hook for local scanning
bluei install-hook my-project
```

### Campaign-driven refactoring

```bash
# Plan a focused campaign
bluei campaign plan --repo my-app \
  --rules type-explicit-any,ruff-b904 \
  --paths "src/api/**" --save

# Review the plan
bluei campaign status camp-20260518-abc123 --repo my-app

# Dry-run for safety
bluei campaign run camp-20260518-abc123 --repo my-app --dry-run

# Execute with explicit worktree
bluei campaign run camp-20260518-abc123 --repo my-app \
  --allow-mutate --worktree /tmp/wt

# Monitor progress
bluei campaign events camp-20260518-abc123 --repo my-app
```

### Secret scanning

```bash
# Scan for leaked secrets
bluei secrets ./src

# Scan entire project
bluei secrets ~/my-project
```
