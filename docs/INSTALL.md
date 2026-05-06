# QA Agent Standalone Install

## Goal

Set up `qa-agent` on a machine without relying on OpenClaw runtime features.

## Related Documentation

- [Architecture](../../docs/qa-agent-architecture.md) — System architecture and components
- [Operator Guide](../../docs/qa-agent-operator-guide-autonomous-review.md) — Operating autonomous review
- [Config Reference](../../docs/qa-agent-config-reference.md) — Configuration options
- [Implementation Status](../../docs/qa-agent-autonomous-review-status.md) — Current feature status

## Option A — npm (recommended, once published)

```bash
npm install -g qa-agent   # installs the JS wrapper + Python bootstrap
qa-agent init             # interactive first-run setup
```

The npm package includes the Node.js wrapper which auto-bootstraps the Python environment on first run.

> **Note:** Rename `package.json` `name` field before publishing to npm. Use `npm publish --access public` for a public package.

## Option B — Git clone

```bash
git clone <repo-url> qa-agent
cd qa-agent
./qa-agent init    # or: bash scripts/bootstrap.sh
```

## Requirements

Required:
- `python3`
- `git`
- `gh` (GitHub CLI)
- `uv`

Recommended:
- `claude`
- `opencode`
- `docker`

## Bootstrap

```bash
cd /path/to/qa-agent
./scripts/bootstrap.sh
```

This will:
- verify core tools
- create `.venv`
- install local test dependencies
- create standard workspace directories
- print the next steps

## First checks

```bash
./qa-agent doctor --format whatsapp
./qa-agent preflight --repo /path/to/target-repo
```

## Onboard a repo safely

Start conservative:

```bash
./qa-agent onboard \
  --repo /path/to/target-repo \
  --mode observe \
  --profile conservative
```

Then inspect:

```bash
./qa-agent repos show target-repo
./qa-agent status --repo target-repo
```

## Dry-run before live execution

```bash
./qa-agent run --repo target-repo --phase issue-cycle --dry-run
```

## Install host cron schedule

```bash
./qa-agent install-cron --repo target-repo
```

You can customize schedules:

```bash
./qa-agent install-cron \
  --repo target-repo \
  --issue-schedule '0 */4 * * *' \
  --pr-schedule '0 */6 * * *' \
  --merge-schedule '0 6,18 * * *'
```

Installed entries run through `scripts/run_and_sync.sh` rather than calling `qa-agent run` directly. That wrapper keeps Obsidian in sync programmatically by updating:
- `~/Obsidian/Logs/issue-cycle/`
- `~/Obsidian/Logs/pr-cycle/`
- `~/Obsidian/Logs/merge-cycle/`
- `~/Obsidian/Logs/qa-monitor/`
- `~/Obsidian/Logs/qa-daily/`

The source of truth remains `qa-agent/repos/<repo>/state/` and `qa-agent/repos/<repo>/runs/`.

## Safety notes

- `observe` blocks non-dry-run execution.
- `issue-only` blocks PR/merge execution.
- `pr` blocks merge execution.
- dirty working trees block live execution unless explicitly allowed by policy.
- onboarding never defaults `auto_merge` to true.

## Upgrade notes

Existing repos without explicit `safety` config are migrated heuristically:
- if `github.live_actions` was enabled, a live-capable safety mode is inferred
- otherwise they default to `observe`
