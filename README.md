<p align="center">
  <img src="design_assets/logo.svg" width="180" alt="bluei" />
</p>

<p align="center">
  <img src="https://img.shields.io/github/v/release/waterworkshq/bluei" alt="version" />
  <img src="https://img.shields.io/badge/tests-6017%20passed-brightgreen" alt="tests" />
  <img src="https://img.shields.io/github/license/waterworkshq/bluei" alt="license" />
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey" alt="platform" />
</p>

<h3 align="center">
  <a href="https://bluei.dev">bluei.dev</a>
</h3>

> **⚠️ Pre-release software.** This is `v0.1.0-beta.1` — an early preview with significant bugs, incomplete features, and breaking changes ahead. **Do not use in production.** Use for evaluation, testing, and contributions only. Production-ready releases will follow in the coming weeks.

# bluei — Trust the silence

**Autonomous repository health. Silence from the depths.**

bluei is an autonomous QA agent for GitHub repositories. It dives into your codebase, surfaces every speck (lint errors, type issues, test gaps, documentation drift, performance smells), tracks health over time, and optionally fixes issues and opens PRs — on a schedule or on demand.

Think of it as a tireless underwater drone for your repos: it surfaces only when it finds something worth surfacing, and it never creates noise.

---

## Quick Install

```bash
curl -fsSL https://bluei.dev/install.sh | bash
```

That's it. The installer checks for Python 3.11+, installs `uv` if needed, clones the repo, bootstraps the environment, and puts `bluei` on your PATH.

```bash
bluei --version   # verify
bluei init        # onboard your first project
```

<details>
<summary><strong>Manual install</strong> (clone + symlink)</summary>

```bash
# 1. Clone
git clone https://github.com/waterworkshq/bluei ~/.bluei
cd ~/.bluei

# 2. Bootstrap
./scripts/bootstrap.sh

# 3. Symlink to PATH
mkdir -p ~/.local/bin
ln -sf "$(pwd)/bin/bluei" ~/.local/bin/bluei

# 4. Verify
bluei --version
```

Ensure `~/.local/bin` is in your `$PATH`. Add this to `~/.bashrc` if not:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

</details>

---

## Quick Start

```bash
# Interactive setup wizard (recommended for first time)
bluei init

# Or headless onboarding for scripting
bluei onboard --repo /path/to/my-project --mode observe --profile conservative

# See status
bluei status
bluei status --repo my-project

# Scan for issues (dry-run by default in observe mode)
bluei scan my-project

# Preview specks without creating anything
bluei duster my-project

# Fix issues and open PRs
bluei clean my-project

# Check vitality
bluei health my-project

# Generate an HTML report dashboard
bluei report my-project --format html

# Scan for hardcoded secrets
bluei secrets /path/to/my-project

# Generate GitHub Actions CI workflow
bluei ci my-project > .github/workflows/bluei.yml

# Install pre-commit hook
bluei install-hook my-project
```

---

## Command Reference

| Command | Description |
|---------|-------------|
| `bluei init` | Interactive first-run setup wizard |
| `bluei onboard --repo <path>` | Headless onboarding for a new repository |
| `bluei install` | Print installation/setup instructions |
| `bluei scan <name>` | Run discovery/issue-cycle scan on a repo |
| `bluei clean <name>` | Fix issues and open Pull Requests |
| `bluei duster <name>` | Dry-run scan to preview specks without changes |
| `bluei doctor [<name>]` | Run operational checks/diagnostics |
| `bluei run <name>` | Run full orchestrated QA cycle |
| `bluei patterns <subcommand>` | Inspect and manage learned fix patterns |
| `bluei campaign <subcommand>` | Plan and execute multi-finding refactor campaigns |
| `bluei emergent <subcommand>` | Propose, shadow-test, activate, and discover findings from learned rules |
| `bluei dashboard` | Generate a read-only observability dashboard from repo state |
| `bluei languages` | List installed language packs and declared detector rules |
| `bluei health <name>` | Show vitality (0–100) |
| `bluei status [--repo <name>]` | Show agent or repo status |
| `bluei report <name>` | Generate a QA report (PDF by default) |
| `bluei preflight --repo <path>` | Assess a repository before onboarding |
| `bluei heal --repo <path>` | Repair dirty worktrees from transient artifacts |
| `bluei secrets <path>` | Scan for hardcoded secrets (API keys, tokens, passwords) |
| `bluei ci` | Generate a GitHub Actions CI workflow YAML |
| `bluei install-hook --repo <path>` | Install a pre-commit hook for scanning |
| `bluei update` | Self-update bluei to the latest version |
| `bluei install-cron --repo <name>` | Install a cron schedule for a repo |
| `bluei lesson <name>` | View or manage lesson logs |
| `bluei upgrade-config <name>` | Upgrade a repo config to the latest schema |
| `bluei notify <name>` | Send a test notification or check channels |
| `bluei help [<command>]` | Show help for a specific command |

Use `bluei help <command>` or `bluei <command> --help` for detailed per-command help.

---

## Detectors

bluei finds across **8 language categories** using 7 built-in tools plus 3 optional LLM backends:

| Language | Tool | What It Finds |
|----------|------|---------------|
| Python | ruff (B, E, W, F, S, C4) | Lint errors, style issues, security bugs, comprehension misuse |
| TypeScript / JavaScript | ESLint, xo | Code quality, style violations, complexity |
| TypeScript | tsc --noEmit --strict | Type safety: missing types, explicit `any`, untyped imports |
| Go | staticcheck | Bugs, style issues, dead code, simplification opportunities |
| Shell / Bash | shellcheck | Quoting issues, deprecated syntax, injection vectors |
| Dockerfile | hadolint | Security: unversioned images, apt cleanup, layer consolidation |
| Markdown | markdownlint | Style: header spacing, bare URLs, hard tabs, line length |
| All | gitleaks | Secrets: AWS keys, GitHub tokens, API keys, private keys, high-entropy strings |

All detectors run on every `bluei scan`, `bluei duster`, or `bluei run`. Tools that aren't installed are skipped gracefully. Plugins can extend discovery to additional languages via `PluginLoader`.

---

## Language Packs

bluei language packs live under `plugins/<language>/` and declare their rules, supported languages, health mapping, and optional validation checks. Pack metadata is used to populate detector catalogs and health scoring without editing central constants for every new language.

```bash
# List installed language packs and their rules
bluei languages
```

The built-in pack set covers Python, TypeScript/JavaScript, Go, Rust, Shell, Dockerfile, and Markdown. Go and Rust include expanded static-analysis rule catalogs; Shell, Dockerfile, and Markdown add lightweight built-in discovery for common issues while remaining compatible with external tools such as shellcheck, hadolint, and markdownlint.

---

## Learned Fix Patterns

bluei records successful fix patterns and can reuse them before spending another LLM call on similar findings. The deterministic cascade tries 8 stages in order before falling back to LLM: 5 linter stages (ruff-fix, ruff-format, pyupgrade, autoflake, isort), recipe engine, pattern replay (with structural hashing + fuzzy matching), composite patterns, and AST transforms.

```bash
# List active learned patterns for a repo
bluei patterns list --repo my-project

# Inspect the stored before/after pattern and source evidence
bluei patterns show fp-a1b2c3d4e5f6 --repo my-project

# Disable or restore a pattern without deleting its history
bluei patterns deactivate fp-a1b2c3d4e5f6 --repo my-project
bluei patterns reactivate fp-a1b2c3d4e5f6 --repo my-project
```

Patterns are confidence-scored from successful reuse and feedback. Low-confidence or deactivated patterns are retained for audit history but excluded from active replay. High-confidence patterns are auto-promoted to staged YAML recipes for faster replay on subsequent runs. Patterns are also shared across repos via structural hashing — privacy-normalized before publishing.

---

## Campaign Orchestration

Campaigns group related findings into ordered phases so larger cleanup work can be planned, reviewed, paused, resumed, or aborted without losing state.

```bash
# Create a dry-run campaign plan from current findings
bluei campaign plan --repo my-project --rules type-explicit-any --paths "src/api/**"

# Save the plan and inspect progress/events later
bluei campaign plan --repo my-project --rules type-explicit-any --paths "src/api/**" --save
bluei campaign list --repo my-project
bluei campaign status camp-20260518-abc123 --repo my-project
bluei campaign events camp-20260518-abc123 --repo my-project

# Execute safely
bluei campaign run camp-20260518-abc123 --repo my-project --dry-run
bluei campaign run camp-20260518-abc123 --repo my-project --allow-mutate --worktree /path/to/worktree

# Operator lifecycle controls
bluei campaign pause camp-20260518-abc123 --repo my-project --reason "waiting on review"
bluei campaign resume camp-20260518-abc123 --repo my-project
bluei campaign abort camp-20260518-abc123 --repo my-project --reason "scope changed"
```

Real campaign execution requires an explicit worktree and clean-worktree preflight. Guarded runs record events and preserve terminal states so completed or aborted campaigns are not accidentally reopened.

---

## Emergent Rules

Emergent rules let bluei turn repeated findings and successful fix patterns into operator-controlled detector proposals.

```bash
# Propose rules from repeated current findings
bluei emergent propose --repo my-project --min-observations 5

# Propose from successful learned fix patterns
bluei emergent propose --repo my-project --from-patterns --min-success-count 5

# Move safe proposals into candidate state, rejecting rules already covered by built-ins
bluei emergent validate --repo my-project

# Run candidate/tentative/active rules in shadow mode against a worktree
bluei emergent scan --repo my-project --worktree /path/to/worktree

# Record shadow validation evidence; safe rules become active after enough clean runs
bluei emergent shadow er-abc123 --repo my-project --matches 12 --false-positives 0

# Explicitly write findings from active emergent rules into repo state
bluei emergent discover --repo my-project --worktree /path/to/worktree
```

Only `active` emergent rules can create normal findings, and only through the explicit `discover` command. `candidate` and `tentative` rules remain shadow-only so new detector ideas can collect evidence before they affect issue creation, campaigns, or health reporting.

---

## Observability Dashboard

The dashboard is a static, self-contained view over existing bluei state. It does not collect new data or run fixes.

```bash
# Generate a fleet dashboard
bluei dashboard

# Generate a dashboard for one repo
bluei dashboard --repo my-project --output dashboard.html

# Export the same aggregate data as JSON
bluei dashboard --format json --output dashboard.json
```

It summarizes vitality history, learned fix patterns, emergent rule lifecycle counts, campaign progress, review publication metrics, auto-tune state, cycle suppressions, rebase success rate, recent escalations, and raw JSON/JSONL state file sizes from each repo's `state/` directory.

---

## Care Levels

bluei uses a graduated care model to give you control:

| Mode | Behavior |
|------|----------|
| **watch only** | Dry-run only. Scans and reports specks — never touches GitHub or your working tree. |
| **note only** | Creates GitHub issues for specks. Never opens PRs or merges. |
| **offer fixes** | Creates issues and opens PRs with fixes. Never merges automatically. |
| **full care** | Full autonomy: finds issues, fixes them, opens PRs, and merges approved PRs. |

Each repo has its own care level, set during onboarding. Change it later in `repos/<name>/config.yaml`.

Additional care gates:

- **Dirty-tree protection**: blocks live execution when uncommitted changes exist (auto-heals transient artifacts like `__pycache__/`)
- **Profile presets**: `conservative` (cautious thresholds), `balanced` (default), `aggressive` (maximum fixes)

---

## Requirements

| Dependency | Required? | Notes |
|-----------|-----------|-------|
| Python 3.11+ | ✅ Required | Core runtime |
| Git | ✅ Required | Repository operations |
| GitHub CLI (`gh`) | ✅ Required | GitHub API auth & operations |
| `uv` | ✅ Required | Python package management |
| Docker | ⬜ Optional | Containerized linting (xo linter) |
| Claude Code CLI | ⬜ Optional | LLM-backed fix engine |
| OpenCode CLI | ⬜ Optional | LLM-backed fix engine |
| ESLint + TypeScript | ⬜ Optional | JS/TS linting (installed via `npm install -g eslint`) |
| Go staticcheck | ⬜ Optional | Go code analysis (`go install honnef.co/go/tools/cmd/staticcheck@latest`) |
| ShellCheck | ⬜ Optional | Shell script analysis (`brew install shellcheck` or download binary) |
| hadolint | ⬜ Optional | Dockerfile linting (download from GitHub releases) |
| markdownlint-cli | ⬜ Optional | Markdown style checking (`npm install -g markdownlint-cli`) |
| actionlint | ⬜ Optional | GitHub Actions workflow linting (download from GitHub releases) |
| gitleaks | ⬜ Optional | Secret scanning (`brew install gitleaks` or download binary) |

---

## Architecture

```
host cron / CLI
   ↓
bluei (bin/bluei) — bash wrapper
   ↓
bluei.app.runner.RunEngine
   ↓
bluei.engine — execution engine
   ↓
repo worktrees / GitHub / validation / vitality snapshots
```

Key design principles:

- **Standalone-first**: no dependency on OpenClaw, Docker, or any platform
- **Backend fallback**: `auto → claude → opencode → deterministic` — the deterministic cascade tries 8 stages before LLM, saving ~65% of LLM calls
- **Per-phase locking**: prevents overlapping issue/pr/merge runs
- **Per-repo state**: each onboarded repo keeps config, runs, baselines, specks, and health history
- **Detector ecosystem**: 8 language categories covered by 7 tools (ruff, ESLint, staticcheck, shellcheck, hadolint, markdownlint, gitleaks) plus TypeScript compiler and xo — all optional, all gracefully skipped if not installed
- **Plugin system**: external plugins can extend discovery to additional languages via `PluginLoader`
- **Secret scanning**: gitleaks integration catches hardcoded secrets on every scan
- **HTML reporting**: `bluei report --format html` generates a standalone dark-themed dashboard with vitality charts
- **CI/CD generation**: `bluei ci` outputs a GitHub Actions workflow; `bluei install-hook` adds pre-commit scanning

---

## What's Next

| Area | Status | Notes |
|------|--------|-------|
| v0.2.0-alpha.1 "Evidence" | Delivered | Flywheel Ledger + Pattern Court Lite: deterministic-vs-LLM metrics, cascade-stage counts, honest three-way replay outcomes, savings, and Pattern Court Lite explainability |
| v0.2.0-alpha.2 "Safe Learning" | Delivered | Governance substrate (Operator Control Plane, Golden Validation Bundles, Dry Replay, SPRT demotion, bluei learn CLI). Ships inert — evidence population in alpha.3. |
| v0.2.0-alpha.3 "Evidence Foundation" | Planned | Self-seed the substrate from vetted open-source data + complete evidence enrichment capture infra. Replaces "grow from production fixes" (no production deployment before stable). |
| v0.2.0-alpha.4 "Deterministic Assets" | Planned | Recipe Foundry, Rule Hatchery, and Python AST structural replay — built on a populated substrate |
| v0.2.0-alpha.5 "Repo Native" | Planned | Repo Taste Profile and Campaign Lab for evidence-gathering Campaigns |
| v0.2.0-alpha.6 "Economics" | Planned | Model Governor and proof harness for model downgrade and cost/reliability evidence |

See [Roadmap](docs/meta/ROADMAP.md) for release boundaries and the planning seed map.

---

## Scheduling

bluei is designed for **host cron** scheduling:

```bash
bluei install-cron --repo my-project
```

Each repo maintains its own issue-cycle, pr-cycle, review-cycle, and merge-cycle schedules. Lock files under `locks/` prevent concurrent runs of the same repo/phase.

---

## Documentation

| Category | Doc | Description |
|----------|-----|-------------|
| **User** | [INSTALL.md](docs/user/INSTALL.md) | Installation instructions and platform setup |
| **Operator** | [OPERATOR_GUIDE.md](docs/operator/OPERATOR_GUIDE.md) | Day-to-day operations: campaigns, patterns, emergent rules, CI/CD, merge lifecycle |
| **Reference** | [CONFIG_REFERENCE.md](docs/reference/CONFIG_REFERENCE.md) | Configuration keys, YAML schemas, care levels, model rates |
| **Reference** | [RULES_REFERENCE.md](docs/reference/RULES_REFERENCE.md) | All 43 detection rules, routing, context rules, recipes, cascade stages |
| **Reference** | [CLI_REFERENCE.md](docs/reference/CLI_REFERENCE.md) | All 27 commands with flags, subcommands, exit codes |
| **Reference** | [TEMPLATE_CATALOG.md](docs/reference/TEMPLATE_CATALOG.md) | 11 repo templates + 7 rule packs with selection heuristics |
| **Reference** | [config-migration.md](docs/reference/config-migration.md) | Config v1→v2 migration guide |
| **Developer** | [ARCHITECTURE.md](docs/developer/ARCHITECTURE.md) | Full system architecture, data flow, all module responsibilities |
| **Developer** | [FIX_PIPELINE.md](docs/developer/FIX_PIPELINE.md) | Fix pipeline: classification, routing, tiering, cascade, validation |
| **Developer** | [REVIEW_LIFECYCLE.md](docs/developer/REVIEW_LIFECYCLE.md) | Review lifecycle: modes, remediation, circuit breaker, state machine |
| **Developer** | [AST_ENGINE.md](docs/developer/AST_ENGINE.md) | Cross-language AST detection patterns and transforms |
| **Developer** | [SCAFFOLD_GENERATORS.md](docs/developer/SCAFFOLD_GENERATORS.md) | Test and doc scaffold generation from module inspection |
| **Developer** | [HEALTH_METHODOLOGY.md](docs/developer/HEALTH_METHODOLOGY.md) | Health scoring, dashboard, reports, cost tracking |
| **Developer** | [GITHUB_OPERATIONS.md](docs/developer/GITHUB_OPERATIONS.md) | Merge lifecycle, rebase sweep, cleanup, regression detection |
| **Developer** | [LANGUAGE_PACKS.md](docs/developer/LANGUAGE_PACKS.md) | Plugin development guide and contract reference |
| **Developer** | [TESTING.md](docs/developer/TESTING.md) | Testing philosophy, writing tests, fixture patterns |
| **Meta** | [docs/README.md](docs/README.md) | Full documentation index with categorization |
| **Meta** | [CONTRIBUTING.md](CONTRIBUTING.md) | Contributing guidelines and PR process |
| **Meta** | [SECURITY.md](SECURITY.md) | Security policy and vulnerability reporting |
| **Meta** | [OPEN_SOURCE_POLICY.md](OPEN_SOURCE_POLICY.md) | Open-source core, commercial offering, and contribution-back policy |
| **Meta** | [TRADEMARK.md](TRADEMARK.md) | Brand and trademark use policy |
| **Meta** | [CHANGELOG.md](CHANGELOG.md) | Release history |

---

## Open Source And Commercial Use

bluei's public core is licensed under [AGPL-3.0-or-later](./LICENSE) and is intended to remain open source. Self-hosting, personal use, modification, and upstream contributions are welcome.

If you modify and run the AGPL-covered core as a network service for others, you must make the corresponding modified source available under AGPL terms.

The official bluei name, logo, domain, Waterworks HQ branding, and future official hosted/commercial offerings are controlled separately from the code license. See [OPEN_SOURCE_POLICY.md](./OPEN_SOURCE_POLICY.md), [TRADEMARK.md](./TRADEMARK.md), and [CLA.md](./CLA.md).

---

## License

[AGPL-3.0-or-later](./LICENSE) — free to self-host and use. Network service providers must disclose source for modified versions.

---

*Built by Waterworks HQ.*
