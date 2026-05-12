# 🐙 Blenny — Keep your waters clean. Ink and dust.

**Autonomous repository health. Silence from the depths.**

[![Version](https://img.shields.io/badge/version-2.5.0-blue)](./package.json)
[![License](https://img.shields.io/badge/license-AGPL--3.0--or--later-green)](./LICENSE)

Blenny is an autonomous QA agent for GitHub repositories. It dives into your codebase, surfaces every finding (lint errors, type issues, test gaps, documentation drift, performance smells), tracks health over time, and optionally fixes issues and opens PRs — on a schedule or on demand.

Think of it as a tireless underwater drone for your repos: it surfaces only when it finds something worth surfacing, and it never creates noise.

---

## Quick Install

```bash
# 1. Clone
git clone <your-repo-url> ~/.blenny
cd ~/.blenny

# 2. Bootstrap
./scripts/bootstrap.sh

# 3. Symlink to PATH
mkdir -p ~/.local/bin
ln -sf "$(pwd)/bin/blenny" ~/.local/bin/blenny

# 4. Verify
blenny --version
```

Ensure `~/.local/bin` is in your `$PATH`. Add this to `~/.bashrc` if not:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

---

## Quick Start

```bash
# Interactive setup wizard (recommended for first time)
blenny init

# Or headless onboarding for scripting
blenny onboard --repo /path/to/my-project --mode observe --profile conservative

# See status
blenny status
blenny status --repo my-project

# Scan for issues (dry-run by default in observe mode)
blenny scan my-project

# Preview findings without creating anything
blenny duster my-project

# Fix issues and open PRs
blenny ink my-project

# Check health score
blenny health my-project

# Generate an HTML report dashboard
blenny report my-project --format html

# Scan for hardcoded secrets
blenny secrets /path/to/my-project

# Generate GitHub Actions CI workflow
blenny ci > .github/workflows/blenny.yml

# Install pre-commit hook
blenny install-hook --repo /path/to/my-project
```

---

## Command Reference

| Command | Description |
|---------|-------------|
| `blenny init` | Interactive first-run setup wizard |
| `blenny onboard --repo <path>` | Headless onboarding for a new repository |
| `blenny install` | Print installation/setup instructions |
| `blenny scan <name>` | Run discovery/issue-cycle scan on a repo |
| `blenny ink <name>` | Fix issues and open Pull Requests |
| `blenny duster <name>` | Dry-run scan to preview findings without changes |
| `blenny doctor [<name>]` | Run operational checks/diagnostics |
| `blenny run <name>` | Run full orchestrated QA cycle |
| `blenny health <name>` | Show health score (0–100) |
| `blenny status [--repo <name>]` | Show agent or repo status |
| `blenny report <name>` | Generate a QA report (PDF by default) |
| `blenny preflight --repo <path>` | Assess a repository before onboarding |
| `blenny heal --repo <path>` | Repair dirty worktrees from transient artifacts |
| `blenny secrets <path>` | Scan for hardcoded secrets (API keys, tokens, passwords) |
| `blenny ci` | Generate a GitHub Actions CI workflow YAML |
| `blenny install-hook --repo <path>` | Install a pre-commit hook for scanning |
| `blenny update` | Self-update qa-agent to the latest version |
| `blenny help [<command>]` | Show help for a specific command |

Use `blenny help <command>` or `blenny <command> --help` for detailed per-command help.

---

## Detectors

Blenny finds across **8 language categories** using 7 built-in tools plus 3 optional LLM backends:

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

All detectors run on every `blenny scan`, `blenny duster`, or `blenny run`. Tools that aren't installed are skipped gracefully. Plugins can extend discovery to additional languages via `PluginLoader`.

---

## Safety Profiles

Blenny uses a graduated safety model to give you control:

| Mode | Behavior |
|------|----------|
| **observe** | Dry-run only. Scans and reports findings — never touches GitHub or your working tree. |
| **issue-only** | Creates GitHub issues for findings. Never opens PRs or merges. |
| **pr** | Creates issues and opens PRs with fixes. Never merges automatically. |
| **merge** | Full autonomy: finds issues, fixes them, opens PRs, and merges approved PRs. |

Each repo has its own safety mode, set during onboarding. Change it later in `repos/<name>/config.yaml`.

Additional safety gates:
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
blenny (bin/blenny) — bash wrapper
   ↓
qa-agent — Python CLI entry point
   ↓
qa_agent.runner.RunEngine
   ↓
core/sandbox_local_runner.py — execution engine
   ↓
repo worktrees / GitHub / validation / health snapshots
```

Key design principles:
- **Standalone-first**: no dependency on OpenClaw, Docker, or any platform
- **Backend fallback**: `auto → claude → opencode → deterministic`
- **Per-phase locking**: prevents overlapping issue/pr/merge runs
- **Per-repo state**: each onboarded repo keeps config, runs, baselines, findings, and health history
- **Detector ecosystem**: 8 language categories covered by 7 tools (ruff, ESLint, staticcheck, shellcheck, hadolint, markdownlint, gitleaks) plus TypeScript compiler and xo — all optional, all gracefully skipped if not installed
- **Plugin system**: external plugins can extend discovery to additional languages via `PluginLoader`
- **Secret scanning**: gitleaks integration catches hardcoded secrets on every scan
- **HTML reporting**: `blenny report --format html` generates a standalone dark-themed dashboard with health charts
- **CI/CD generation**: `blenny ci` outputs a GitHub Actions workflow; `blenny install-hook` adds pre-commit scanning

---

## Scheduling

Blenny is designed for **host cron** scheduling:

```bash
blenny install-cron --repo my-project
```

Each repo maintains its own issue-cycle, pr-cycle, review-cycle, and merge-cycle schedules. Lock files under `locks/` prevent concurrent runs of the same repo/phase.

---

## License

[AGPL-3.0-or-later](./LICENSE) — free to self-host and use. Network service providers must disclose source for modified versions.

---

*Built by Waterworks HQ. Maintained in OpenClaw workspace by Red.*
