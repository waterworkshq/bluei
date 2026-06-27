<!-- CATEGORY: operator -->

# bluei — Operator Guide

## Installation

```bash
curl -fsSL https://bluei.dev/install.sh | bash
```

Requires: Python 3.11+, git. The installer auto-installs `uv` if missing.

## Getting Started

### 1. Onboard a project

```bash
# Interactive wizard
bluei init

# Or headless
bluei init --path ~/my-project
bluei onboard --repo ~/my-project --mode watch-only --profile conservative
```

### 2. Preview before doing anything

```bash
bluei duster my-project    # dry-run scan — no changes
```

### 3. Start with conservative care

```bash
bluei scan my-project      # discover issues, create GitHub issues
```

### 4. Graduate to fixing

```bash
bluei clean my-project     # fix issues, open PRs
```

### 5. Monitor health

```bash
bluei health my-project
bluei report my-project --format html
```

### 6. Set it and forget it

```bash
bluei install-cron my-project
```

## Day-to-Day Commands

### Status and monitoring

```bash
bluei status                        # agent-wide status
bluei status --repo my-project      # per-repo status
bluei health my-project             # health score (0-100)
bluei health my-project --days 7    # last 7 days
bluei health my-project --format whatsapp  # WhatsApp-friendly format
```

### Reports

```bash
bluei report my-project                    # PDF report (default)
bluei report my-project --format html      # HTML dashboard
bluei report my-project --format json      # JSON data
bluei report my-project --format text      # plain text
bluei report my-project -o report.html     # custom output path
```

### Dashboard

```bash
bluei dashboard                     # fleet-wide observability dashboard
bluei dashboard --repo my-project   # single-repo dashboard
bluei dashboard --format json       # JSON export
```

### Diagnostics

```bash
bluei doctor                    # check all repos
bluei doctor my-project         # check specific repo
```

### Pre-onboarding assessment

```bash
bluei preflight --repo /path/to/new-project
```

### Worktree healing

```bash
bluei heal --repo my-project              # dry-run — preview what would be cleaned
bluei heal --repo my-project --no-dry-run # actually clean
```

## Advanced Operations

### Campaigns — batched fixes

Plan and execute groups of related fixes together:

```bash
# Plan a campaign for specific rules in a path
bluei campaign plan --repo my-project --rules type-explicit-any,ruff-b904 --paths "src/api/**"

# Save the plan
bluei campaign plan --repo my-project --rules type-explicit-any --save

# List saved campaigns
bluei campaign list --repo my-project

# Dry-run execution
bluei campaign run camp-20260518-abc123 --repo my-project --dry-run

# Live execution (requires worktree)
bluei campaign run camp-20260518-abc123 --repo my-project --allow-mutate --worktree /path

# Pause, resume, abort
bluei campaign pause camp-20260518-abc123 --repo my-project --reason "reviewing manually"
bluei campaign resume camp-20260518-abc123 --repo my-project
bluei campaign abort camp-20260518-abc123 --repo my-project
```

### Dashboard

Generate a read-only observability dashboard from repo state:

```bash
# Fleet-wide dashboard (all registered repos)
bluei dashboard

# Single-repo dashboard
bluei dashboard --repo my-project --output dashboard.html

# JSON export for programmatic consumption
bluei dashboard --format json --output dashboard.json
```

The dashboard displays: vitality history, finding trends, learned fix patterns,
emergent rule lifecycle counts, campaign progress, review publication metrics,
auto-tune state, cycle suppressions, rebase success rate, recent escalations,
and per-repo state file sizes. It reads from existing `state/` files only —
it never collects new data or runs fixes.

See also: [HEALTH_METHODOLOGY.md](../developer/HEALTH_METHODOLOGY.md) for the scoring methodology.

### Merge Lifecycle

The merge cycle follows strict safety rules:

```bash
# Merge eligible PRs (one per run)
bluei run my-project --phase merge-cycle --no-dry-run
```

**Merge rules:**
- **One PR per run** — prevents chain-failure cascades
- **Oldest-first ordering** — non-draft PRs first, then by creation date, then by PR number
- **Regression check** — tests for deleted tests, export changes, new lint violations (blocks at score > 0.5)
- **DIRTY/BEHIND triage** — conflicted or behind-base PRs are returned to `pr-cycle` for branch repair
- **Autonomous merge gating** — `merge_ready` review artifacts from the review cycle can satisfy the merge gate without human `APPROVED` review
- **Worktree safety** — each merge runs in an isolated worktree

```bash
# Inspect merge candidates
bluei status --repo my-project | grep -A5 "merge candidates"
```

See also: [GITHUB_OPERATIONS.md](../developer/GITHUB_OPERATIONS.md) for the full merge lifecycle.

### Patterns — learned fixes

Inspect and manage bluei's learned fix patterns:

```bash
# List active patterns
bluei patterns list --repo my-project

# Inspect a specific pattern
bluei patterns show fp-a1b2c3d4e5f6 --repo my-project

# Disable or restore a pattern
bluei patterns deactivate fp-a1b2c3d4e5f6 --repo my-project
bluei patterns reactivate fp-a1b2c3d4e5f6 --repo my-project
```

### Emergent rules — new detection rules

Discover and manage rules that bluei has learned from observing code:

```bash
# Propose rules from repeated findings
bluei emergent propose --repo my-project

# Validate proposals
bluei emergent validate --repo my-project

# Shadow-scan in a worktree
bluei emergent scan --repo my-project --worktree /path/to/worktree

# List proposed rules
bluei emergent list --repo my-project

# Approve or reject
bluei emergent approve em-abc123 --repo my-project
bluei emergent reject em-abc123 --repo my-project --reason "too many false positives"

# Promote to a plugin
bluei emergent promote em-abc123 --repo my-project --plugin-dir plugins/python/

# Garbage-collect stale rules
bluei emergent gc --repo my-project
```

### Languages

```bash
bluei languages    # list installed language packs and their rules
```

### Learning governance — safe-mode and approvals

bluei's learning subsystem (Pattern promotion, Dry Replay, SPRT demotion) can be controlled via the `learning.mode` config key and the `bluei learn` CLI.

**Safe-mode levels** (in `repos/<name>/config.yaml` or workspace `config.yaml`):

```yaml
learning:
  mode: active  # active | audit_only | paused
```

| Mode | Behavior |
|------|----------|
| `active` | Normal operation: Dry Replay runs, SPRT fires, promotions proceed per policy |
| `audit_only` | Shadow evaluation: Dry Replay runs, SPRT computes but doesn't fire, all transitions forced gate-closed |
| `paused` | Full freeze: no Dry Replay, no SPRT, no promotions |

**Recommended onboarding**: start with `audit_only` for the first few cycles, inspect what *would* have happened, then switch to `active`.

**Inspecting governance state:**

```bash
# List pending approvals (Pattern→Recipe promotions awaiting your decision)
bluei learn inbox --repo my-project

# Inspect a Pattern's governance state + recent SPRT evidence
bluei learn status pattern:fp-a1b2c3d4 --repo my-project

# Full audit trail for any governed asset
bluei learn audit recipe:auto-ruff-b904-a1b2c3d4 --repo my-project

# Create a Golden Validation Bundle from a known-good fix
bluei learn bundle fp-a1b2c3d4 --worktree /path/to/worktree --from-finding f-001 --repo my-project
```

**Note:** write-verbs (`pause`, `resume`, `retire`) stay in their native namespaces (`patterns pause`, `emergent approve`). The `learn` namespace is read-only + bundle creation.

## CI/CD Integration

Generate a GitHub Actions workflow for CI-based scanning:

```bash
bluei ci my-project > .github/workflows/bluei.yml
```

Install a pre-commit hook for scanning on every commit:

```bash
bluei install-hook /path/to/my-project
```

## Secrets Scanning

```bash
bluei secrets /path/to/my-project    # scan for hardcoded secrets
```

## Self-Update

```bash
bluei update    # update to latest version
```

## Scripts Reference

Key scripts in `scripts/` for operational use:

| Script | Purpose |
|--------|---------|
| `bootstrap.sh` | Create `.venv`, install dependencies, prepare workspace |
| `install.sh` | One-liner curl-based installer |
| `install-cron.sh` | Install crontab entries for scheduled runs |
| `run_and_sync.sh` | Run a QA phase then sync Obsidian records |
| `daily_summary.py` | Generate per-repo daily markdown summaries |
| `health_digest.py` | Generate health score digest from state files |
| `qa_status_digest.py` | Generate aggregated status across all repos |
| `qa_watchdog.py` | Watchdog for monitoring QA agent health |
| `update_crontab.sh` | Update crontab entries from repo config |
| `test-all.sh` | Run the full test suite |

**Tools** (`scripts/tools/`):

| Tool | Purpose |
|------|---------|
| `check_completeness.py` | Verify all findings have fix attempts |
| `enforce_architecture.py` | Check module boundary compliance |
| `migrate_context.py` | Migrate context rules between formats |

## Troubleshooting

### bluei command not found

Ensure `~/.local/bin` is in your PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### No findings discovered

- Check the repo has supported file types (`.py`, `.ts`, `.go`, etc.)
- Run `bluei doctor my-project` to check linter availability
- Check `bluei languages` to see which packs are installed

### Fixes not working

- Ensure the repo is clean (no uncommitted changes)
- Check the care level: `watch-only` blocks all changes
- Inspect `findings.jsonl` in the repo's state directory for fix attempt history
- Run `bluei health my-project` to see detailed metrics

### Worktree cleanup fails

```bash
bluei heal --repo my-project --no-dry-run
```

This cleans stale locks, prunes worktrees, repairs corrupted state, and removes
orphaned worktree directories.

## State Directory Structure

All operational data lives under `repos/<repo>/state/`:

```
repos/my-project/
  state/
    findings.jsonl          # all discovered findings
    issues.json             # GitHub issues state
    active_prs.json         # open PRs with status
    fix_patterns.jsonl      # learned fix patterns
    health_history.jsonl    # health scores over time
    escalation_log.jsonl    # escalation events
    cost_log.jsonl          # API costs
    review_state.json       # PR review state
  runs/                     # run output logs
  logs/                     # cycle logs
```
