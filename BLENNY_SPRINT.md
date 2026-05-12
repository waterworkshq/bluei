# Blenny Sprint Brief — Fish Needs a Real Tank

**Owner:** Red (code sprint lead)
**Perimeter:** Green (monitoring, edge cases, what falls through cracks)
**Mascot:** Blenny — little fish, big eyes, cleaning stations 🐟
**Tagline:** Keep your waters clean. Ink and dust.

---

## Current State (Audited 2026-05-12)

| Metric | Value |
|--------|-------|
| Codebase root | `~/.openclaw/workspace/qa-agent/` |
| Total footprint | **1.5 GB** (zulip.bak = ~1.3 GB of that) |
| Core engine | 40+ Python files in `core/sandbox_local_runner/` (refactored, ~2K lines in cli.py alone) |
| CLI entry points | `bin/ceph` (656-line bash) + `bin/qa-agent.js` (Node bootstrapper) |
| Stale locks | 3 locks in `locks/` |
| Test files | 40+ tests in `tests/` |
| Active repos | diet-tracking-app, workout-planner-app, mem-db (via CLI state) |
| Orphaned repos (tied to old cron) | ky, zulip (from pre-blenny era) |
| zulip.bak worktree | Dead weight, not referenced by any active state |

---

## Phase 1 — Clean the Tank (Housekeeping)

### 1.1 Drop the dead weight
- `rm -rf repos/zulip.bak/` — **1.3 GB gone**
- `rm -rf repos/ky.bak/` if exists and unused
- Clean stale locks in `locks/`
- Remove retired migration scripts in `scripts/retired/` (migrate_ky.py, migrate_sandbox.py)

### 1.2 Organize the core package
- `core/sandbox_local_runner/` has accumulated extras: test files, migrate_context.py, test_*.py mixed with production code. Move tests back to `tests/`
- Strip debug/test stubs from package (enforce_architecture.py, check_completeness.py — these were Phase 0 artifacts from the refactor, belong in CI, not shipped)
- Clean `__pycache__` from all directories

### 1.3 Purge stale state
- Review `state/refactor_queue/finding_work/` — ~27 stale finding work files — can likely be cleared
- Review `state/escalation_log.jsonl` — truncate if overwhelming

---

## Phase 2 — Rebrand: Ceph → Blenny

### 2.1 Rename CLI entry
- `bin/ceph` → `bin/blenny` (rename + update internal references)
- Update `CURRENT_FILE` resolution, `CEPH_DIR` → `BLENNY_DIR`, all strings
- Add symlink `~/.local/bin/blenny` → `bin/blenny`

### 2.2 Update README
- Replace all Ceph/🐙 references with Blenny/🐟
- Update tagline (already "Keep your waters clean" — keep that)
- Update version string

### 2.3 Rename internal constants
- Package references: `ceph` → `blenny` in outputs, help text, status strings
- Environment variables if any
- GitHub repo references (if any point to blenny-dev org)

---

## Phase 3 — Tighten the CLI

### 3.1 Rewrite bash wrapper as pure Python
- Current: 656-line bash wrapper + 2008-line cli.py + 100-line Node.js bootstrapper
- Target: **Single Python entry point** at `bin/blenny` (shebang Python)
- qa-agent.js becomes unnecessary if we handle bootstrapping in Python
- Bash wrapper and cli.py merge into one clean module

### 3.2 Lexicon enforcement
Ensure CLI surface matches the Blenny lexicon consistently:

| Blenny Command | Maps To | Status |
|---------------|---------|--------|
| `blenny scan <name>` | issue-cycle (discover only) | ✅ exists |
| `blenny ink <name>` | issue-cycle + fix + PR | ✅ exists as `ink` |
| `blenny duster <name>` | dry-run scan, no changes | ✅ exists as `duster` |
| `blenny doctor <name>` | operational diagnostics | ✅ exists as `doctor` |
| `blenny run <name>` | full orchestrated cycle | ✅ exists |
| `blenny init` | interactive setup wizard | ✅ exists |
| `blenny health <name>` | health score | ✅ exists |
| `blenny report <name>` | generate PDF report | ✅ exists |

All present. No new commands needed for Phase 3 — just clean the implementation.

### 3.3 Argument normalization
- All commands should accept `[name]` at the end, not require `--repo` flag
- Consistent `--help` output
- Color output without TTY check hacks (use `rich` or simple ANSI)

---

## Phase 4 — Universal Repo Support (The "Tank" Concept)

### 4.1 Make repo-add trivial
- `blenny init` should detect GitHub remote, auto-generate config
- `blenny init --all` — discover all local clones registered on this machine
- Remove dependency on `repos/` directory hierarchy for ky/zulip — those were for a sandbox runner that's now refactored

### 4.2 State isolation per repo
- Already exists in principle — each repo has its own config/state/findings
- Clean the abstraction so any GitHub repo works without manual `repos/<name>/config.yaml` creation

### 4.3 Remove ky/zulip from cron config
- The 4-hour watchdog cron still references ky/zulip monitoring
- Phase them out or make them inactive repos (silent skip)

---

## Execution Order (Blocks)

```
Week 1: Phase 1 (Clean)  →  Phase 2 (Rebrand)
Week 2: Phase 3 (CLI)    →  Phase 4 (Universal)
```

Let me know your capacity and I'll start carving up tasks.

---

*Ink and dust.*
