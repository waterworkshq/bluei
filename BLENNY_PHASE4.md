# Phase 4: Universal Repo Support

> **Goal:** `blenny init` works on any git repo with a remote. Watchdog discovers repos dynamically. Cron is per-repo, not hardcoded.

## Current State

| Component | Problem |
|-----------|---------|
| `scripts/qa_watchdog.py` | `REPOS = ["ky", "zulip"]` hardcoded at line 11 |
| `scripts/install-cron.sh` | Takes explicit `$REPO` arg, creates static cron lines |
| `blenny init` / `onboard` | Requires registry pre-config — no auto-detect from git remote |
| `qa-agent` compat shim (line 2) | Docstring still says "Ceph CLI" |
| Cron jobs on host | May still reference `ky`/`zulip` — no registry-driven way to re-create |

## Design

### Part A: Dynamic Watchdog

**Change:** `REPOS` list reads from `registry.yaml` instead of hardcoded list.

```python
# Before
REPOS = ["ky", "zulip"]

# After
import yaml
from pathlib import Path

def _discover_repos():
    registry_path = Path(__file__).resolve().parents[1] / "registry.yaml"
    if not registry_path.exists():
        return []
    data = yaml.safe_load(registry_path.read_text())
    return [r["name"] for r in data.get("repos", []) if r.get("enabled", True)]

REPOS = _discover_repos()
```

Also fix the path resolution — watchdog currently looks in `ROOT / "repos" / repo / ...` which expects repos under the qa-agent workspace. The `repos/` directory under qa-agent may not exist anymore after Phase 1 cleanup. Verify the actual state file paths.

### Part B: blenny init That Works Anywhere

**Change:** `blenny init` (and `blenny onboard`) auto-detect the git remote when given a path.

The flow:

```
blenny init --repo /path/to/project
  ↓
Detect git repo at path
  ↓
Extract remote URL (git remote get-url origin)
  ↓
Auto-detect: GitHub, GitLab, or no remote
  ↓
Detect language (from LANGUAGE_MARKERS in onboard.py)
  ↓
Prompt for safety mode (observe/issue-only/pr/merge)
  ↓
Register in registry.yaml
  ↓
Done — blenny scan works immediately
```

**No registry pre-requirement.** If no registry.yaml exists, create one on first init.

### Part C: Registry-Driven Cron

**Change:** `install-cron.sh` becomes `blenny install-cron <name>` that reads schedule from the repo config.

The `qa-agent` entry point already has `install-cron` with `--issue-schedule`, `--pr-schedule`, etc. flags. The missing link is that `blenny` (the new Python entry) doesn't expose this — add it to the command dispatch.

The cron entry uses `blenny` not `qa-agent`:

```cron
0 */4 * * * /home/vikas/.local/bin/blenny scan repo-name >> ~/.blenny/logs/repo-name.log 2>&1
```

### Part D: Compat Shim Cleanup

**Change:** Fix the `qa-agent` script's header docstring at line 2:

```python
# Before
"""Ceph CLI — Autonomous repo health. Silence from the depths."""

# After
"""QA Agent — Autonomous repository quality assurance (blenny backend)."""
```

## Files to Touch

| File | Change |
|------|--------|
| `scripts/qa_watchdog.py` | Part A — dynamic REPOS from registry.yaml + verify path resolution |
| `qa_agent/onboard.py` | Part B — add `_detect_git_remote()` + `onboard_from_path()` that doesn't need registry |
| `qa_agent/registry.py` | Part B — `create()` should accept path + auto-detect, fallback to interactive |
| `bin/blenny` | Part C — add `install-cron` to dispatch |
| `qa-agent` (root) | Part D — fix "Ceph CLI" docstring |
| `scripts/install-cron.sh` | Part C — verify still works or retire in favor of `blenny install-cron` |

## Non-Goals

- Rewriting `core/sandbox_local_runner/cli.py` — it's the backend engine, stays as-is
- GitLab/self-hosted remote detection for cron scheduling — Phase 5 if needed
- Removing the `qa-agent` compat shim — cron/CI callers use it, remove in Phase 5
