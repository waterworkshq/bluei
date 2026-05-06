# QA Agent

**Standalone autonomous repository QA**

Version: 2.5.0

## Quick Start

```bash
# Clone and bootstrap (any machine with Python 3.11+)
git clone <your-repo-url> qa-agent
cd qa-agent
./qa-agent init          # interactive setup wizard

# Or with npm (once published)
npm install -g qa-agent
qa-agent init
```

## What it is

`qa-agent` is the host-side control plane for autonomous repository QA.

It is designed to:
- discover issues
- score repository health
- queue or apply fixes
- create/update PRs
- track history and regressions
- run independently of OpenClaw’s isolated-session cron path

For `ky`, the intended ownership is now:
- **execution:** `qa-agent` + `sandbox_local_runner.py`
- **scheduling:** host `crontab`
- **fix backends:** local CLI tools (`claude`, `opencode`) with deterministic fallback
- **state:** `qa-agent/repos/<repo>/...`
- **records / reporting mirror:** deterministic scripts writing to `~/Obsidian/Logs/...`

---

## Current architecture

```text
host cron
   ↓
qa-agent CLI
   ↓
qa_agent.runner.RunEngine
   ↓
core/sandbox_local_runner.py
   ↓
repo worktrees / GitHub / validation / health snapshots
```

### Key design points

- **Standalone-first:** execution does not depend on OpenClaw model auth.
- **Backend fallback:** `auto -> claude -> opencode -> deterministic`
- **Per-phase locking:** prevents overlapping issue/pr/merge runs.
- **Per-repo state:** each onboarded repo keeps config, runs, baselines, findings, and health history.
- **WhatsApp-safe operator output:** compact text modes for status/health/report/doctor.
- **Safety gates:** explicit mode/profile, dirty-tree protection, and live-action control.

---

## Directory layout

```text
qa-agent/
├── qa-agent                    # CLI entry point
├── core/
│   └── sandbox_local_runner.py # real execution engine (symlink/wrapper target)
├── docs/
├── qa_agent/
│   ├── config.py
│   ├── health.py
│   ├── healer.py              # transient artifact auto-heal (dirty-tree prevention)
│   ├── models.py
│   ├── onboard.py
│   ├── plugins.py
│   ├── registry.py
│   ├── report.py
│   ├── review.py
│   ├── runner.py
│   └── state.py
├── repos/
│   └── <repo>/
│       ├── config.yaml
│       ├── baselines/
│       ├── runs/
│       └── state/
├── logs/
├── plugins/
├── scripts/
├── templates/
└── tests/
```

---

## Prerequisites

Required:
- Python 3.11+
- Git
- GitHub CLI (`gh`)
- `uv`

Optional but recommended:
- Docker
- Claude Code CLI (`claude`)
- OpenCode CLI (`opencode`)

See also: `docs/INSTALL.md`

---

## Bootstrap

```bash
cd /path/to/qa-agent
./scripts/bootstrap.sh
```

Then:

```bash
./qa-agent doctor --format whatsapp
./qa-agent preflight --repo /path/to/repo
```

---

## Recommended first-run flow

```bash
# assess the target repo
./qa-agent preflight --repo /path/to/repo

# onboard conservatively
./qa-agent onboard \
  --repo /path/to/repo \
  --mode observe \
  --profile conservative

# inspect the generated repo config
./qa-agent repos show my-repo

# dry-run before live actions
./qa-agent run --repo my-repo --phase issue-cycle --dry-run

# install host cron once ready
./qa-agent install-cron --repo my-repo
```

---

## Commands

## `qa-agent init`
Interactive first-run setup wizard. Guides the user through prerequisites check, GitHub auth, repo selection, safety mode, and cron schedule — then runs onboard and optionally installs crons.

```bash
./qa-agent init
```

## `qa-agent update`
Self-update qa-agent to the latest version. Detects whether it was installed via npm or git and updates accordingly.

```bash
./qa-agent update   # git pull or npm update
qa-agent --version  # verify
```

## `qa-agent status`
Show overall or per-repo status.

Examples:
```bash
./qa-agent status
./qa-agent status --repo ky
./qa-agent status --repo ky --format whatsapp
./qa-agent status --repo ky --format json
```

Formats:
- `text` (default)
- `whatsapp`
- `json`

---

## `qa-agent preflight`
Assess a repository before onboarding.

Checks include:
- repo path
- git / gh / gh auth
- runner presence
- plugin detection
- language / framework detection
- Docker relevance
- clean working tree safety gate
- suggested validation commands

Examples:
```bash
./qa-agent preflight --repo /path/to/repo
./qa-agent preflight --repo /path/to/repo --format whatsapp
./qa-agent preflight --repo /path/to/repo --format json
```

---

## `qa-agent onboard --repo /path/to/repo [options]`
Onboard a new repository.

Important options:
- `--name`
- `--language`
- `--skip-baseline`
- `--mode observe|issue-only|pr|merge`
- `--profile conservative|balanced|aggressive`
- `--allow-dirty-worktree`

Example:
```bash
./qa-agent onboard --repo /path/to/repo --mode observe --profile conservative
```

Onboarding now infers:
- package manager
- build tool
- baseline checks
- Docker discovery hints
- backend defaults
- review items before live runs

---

## `qa-agent repos list`
List onboarded repositories.

```bash
./qa-agent repos list
```

## `qa-agent repos show <name>`
Show detailed repo configuration and runtime metrics.

```bash
./qa-agent repos show ky
```

---

## `qa-agent run --repo <name> [options]`
Run a QA phase.

Options:
- `--phase`
  - `issue-cycle`
  - `pr-cycle`
  - `merge-cycle`
  - `orchestrated`
  - `verify-only`
- `--dry-run`
- `--no-dry-run`
- `--fix-engine`
  - `auto`
  - `claude`
  - `opencode`
  - `deterministic`

Examples:
```bash
./qa-agent run --repo ky --phase issue-cycle --dry-run
./qa-agent run --repo ky --phase pr-cycle --fix-engine auto --no-dry-run
./qa-agent run --repo ky --phase merge-cycle --no-dry-run
```

### Backend resolution

When `--fix-engine auto` is used, the runner resolves backends in this order:
1. `claude`
2. `opencode`
3. `deterministic`

This is controlled per repo in `repos/<repo>/config.yaml`.

### Safety enforcement

- `observe` blocks non-dry-run execution
- `issue-only` blocks PR and merge execution
- `pr` blocks merge execution
- dirty working trees block live execution when clean-tree safety is enabled
- the healer auto-ignores transient artifacts (coverage, cache, node_modules, __pycache__, etc.) to prevent spurious dirty-tree blocks

---

## `qa-agent health --repo <name>`
Show health score and trend.

Examples:
```bash
./qa-agent health --repo ky
./qa-agent health --repo ky --days 30
./qa-agent health --repo ky --format whatsapp
```

Formats:
- `text`
- `whatsapp`

---

## `qa-agent report --repo <name>`
Generate either a PDF report or a compact text summary.

Examples:
```bash
./qa-agent report --repo ky                 # PDF
./qa-agent report --repo ky --output /tmp/ky.pdf
./qa-agent report --repo ky --format text --days 7
./qa-agent report --repo ky --format whatsapp --days 7
```

Formats:
- `pdf` (default)
- `text`
- `whatsapp`

---

## `qa-agent doctor`
Run operational checks for the standalone agent.

Checks include:
- python3
- git
- gh
- gh auth
- claude
- opencode
- docker
- runner path
- repo summary / latest run status

Examples:
```bash
./qa-agent doctor
./qa-agent doctor --repo ky
./qa-agent doctor --repo ky --format whatsapp
./qa-agent doctor --format json
```

---

## `qa-agent heal`
Heal a dirty worktree caused by common transient generated artifacts.

The healer detects safe transient artifacts (coverage/, `__pycache__/`, `.ruff_cache/`, `node_modules/`, etc.) and can:
- add missing patterns to `.gitignore` so they stop dirtying the tree
- remove the artifacts directly

This runs automatically as part of each cron cycle via `run_and_sync.sh`, and can also be invoked manually.

Examples:
```bash
./qa-agent heal --repo /path/to/repo            # dry-run + safety check
./qa-agent heal --repo /path/to/repo --no-dry-run          # apply
./qa-agent heal --repo /path/to/repo --remove-artifacts   # also delete artifacts
./qa-agent heal --repo /path/to/repo --force              # override safety check
```

---

## `qa-agent install-cron`
Install host cron entries for a repo.

Examples:
```bash
./qa-agent install-cron --repo ky
./qa-agent install-cron --repo ky --issue-schedule '0 */4 * * *'
```

This wraps `scripts/install-cron.sh` and writes issue/pr/merge schedules into the user crontab.

Installed cron entries now run through `scripts/run_and_sync.sh`, which:
- executes the qa-agent phase
- syncs deterministic Obsidian logs for that phase
- refreshes `qa-monitor`
- refreshes a daily markdown summary in `~/Obsidian/Logs/qa-daily/`

This keeps `qa-agent` as the source of truth while preserving an Obsidian record.

---

## Repo configuration

Each repo lives at:

```text
repos/<repo>/config.yaml
```

Important fields:
- `enabled`
- `fix_engine`
- `fallback_engines`
- `claude_template`
- `opencode_template`
- `baseline_checks`
- `limits.*`
- `cooldowns.*`
- `github.live_actions`
- `github.auto_merge`
- `discovery.*`
- `safety.mode`
- `safety.profile`
- `safety.require_clean_worktree`
- `safety.protected_branches`

### `ky` notes

`ky` currently uses:
- `fix_engine: auto`
- `fallback_engines: [claude, opencode, deterministic]`
- Docker-backed discovery
- explicit safety policy (`mode: merge`, `profile: balanced`)
- live GitHub actions enabled
- auto-merge disabled

---

## Scheduling

The intended production path is **host cron**, not OpenClaw cron.

Example current host schedule for `ky`:
- issue-cycle: every 4 hours
- pr-cycle: every 6 hours
- merge-cycle: 06:00 and 18:00 IST

The runner also uses lock files under:

```text
qa-agent/locks/
```

to prevent concurrent runs of the same repo/phase.

---

## Health system

The health engine persists snapshots to:

```text
repos/<repo>/state/health_history.jsonl
```

It tracks:
- overall score
- component scores
- findings count
- historical snapshots over time

The newer engine uses granular components such as:
- `bug_quality`
- `lint_quality`
- `technical_debt`
- `documentation`
- `performance`
- `test_gaps`
- `test_coverage`
- `type_safety`
- `maintainability`

Backward-compatible aliases like `code_quality` are preserved for older callers.

---

## Development / validation

### One command

```bash
cd /path/to/qa-agent
./scripts/test-all.sh
```

### Syntax check

```bash
python3 -m py_compile qa-agent qa_agent/*.py
```

---

## Operational intent

This system should behave as a **dedicated QA agent**, not an OpenClaw-owned service.

That means:
- local execution authority
- local backend auth/tooling
- host-side scheduling
- resilient fallback behavior
- explicit safety modes
- small, inspectable state and logs

---

## Documentation

Full documentation is available in `docs/`:

### Core Documentation
- **[Architecture](../docs/qa-agent-architecture.md)** — System architecture, components, and data flow
- **[Operator Guide](../docs/qa-agent-operator-guide-autonomous-review.md)** — Step-by-step guide for enabling autonomous PR review
- **[Config Reference](../docs/qa-agent-config-reference.md)** — Complete configuration reference

### Implementation Documentation
- **[Implementation Spec](../docs/qa-agent-autonomous-review-implementation-spec.md)** — Detailed build specification
- **[Implementation Status](../docs/qa-agent-autonomous-review-status.md)** — Current implementation status
- **[Task Board](../docs/qa-agent-autonomous-review-task-board.md)** — Build tasks and progress
- **[Pre-Ship Checklist](../docs/qa-agent-autonomous-review-failure-mode-checklist.md)** — Safety verification checklist

### Quick Navigation
- `docs/INSTALL.md` — Installation instructions
- `docs/PLUG_AND_PLAY_ROADMAP.md` — Language/ecosystem support roadmap

---

## License

`qa-agent` is licensed under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**.

Why this choice:
- people can self-host and use it freely,
- the project remains open-source,
- and hosted/network use of modified versions must also disclose source.

This fits the intended model of:
- open self-hosting now,
- and a future hosted/commercial offering by the owner.

See:
- [`LICENSE`](./LICENSE)
- [`CONTRIBUTING.md`](./CONTRIBUTING.md)
- [`CLA.md`](./CLA.md)
- [`OWNERSHIP.md`](./OWNERSHIP.md)

## Contributions

External contributions are welcome, but by opening a pull request you agree to the contributor terms in [`CLA.md`](./CLA.md).

This is intentional so the project can remain flexible for future:
- hosted offerings,
- commercial licensing,
- enterprise agreements,
- and potential transfer to a future company entity.

---

*Maintained in OpenClaw workspace by Red.*
