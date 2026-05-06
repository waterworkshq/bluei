#Retired Migration Scripts

These scripts were one-time migration utilities used to transition from `pr-automation` to qa-agent ownership.

## Status: RETIRED (2026-03-21)

These scripts have been **quarantined** and should not be used. They are kept for historical reference only.

## Why Retired

Per the QA-agent review integration migration plan:
- qa-agent is now the sole control plane for repo healthcare
- Legacy `pr-automation` paths are being phased out
- These one-time migration scripts have already served their purpose
- Repos (`ky`, `zulip`, `qa-sandbox`) have been migrated to qa-agent-native state management

## Files

- `migrate_ky.py` - Migrated ky repo from pr-automation to qa-agent
- `migrate_sandbox.py` - Migrated qa-sandbox repo from pr-automation to qa-agent

## What Changed (Phase4 Cleanup)

On2026-03-21:
- Moved migration scripts from active `scripts/` to retired location
- Added regression test to prevent production code from referencing pr-automation paths
- Documented retirement in progress-log.md

## DoNot Run These

These scripts reference legacy paths that should not be used:
- `/home/vikas/.openclaw/workspace/pr-automation/state`
- Old state file naming conventions

If you need to migrate a new repo, use the canonical qa-agent onboarding flow instead:
```bash
qa-agent onboard --repo <path-to-repo> --name <repo-name>
```