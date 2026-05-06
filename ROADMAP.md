# ROADMAP — QA Agent

> A system that serves as both red blood cells (routine maintenance) and white blood cells (immune response) for repository health.

---

## Phase 1 — Clean the Noise (This Week)

### 1.1 Stale/Duplicate PR Auto-Cleanup
**Status:** ⬜ Not started  
**Files:** `core/sandbox_local_runner/batch_pr.py`, `core/sandbox_local_runner/cli.py`  
**Config:** *(new)* `github.pr_cleanup.stale_hours`, `github.pr_cleanup.max_duplicates`  
**Description:** Before opening a new PR, check if an equivalent PR already exists for the same finding. Close older duplicates automatically.

- [ ] Add duplicate detection before `batch_pr.create_pr()` — compare by finding ID, file path, and lint rule
- [ ] Close older duplicates with a comment referencing the newer PR
- [ ] Configurable stale threshold (default: 48h)
- [ ] Log cleanup actions to cycle log

**Why:** Zulip has 5 open PRs for the same ruff-c408 fix. New PRs should never be created while an equivalent one is open.

### 1.2 Self-Healing — Stale Locks & Worktrees
**Status:** ⬜ Not started  
**Files:** `core/sandbox_local_runner/lifecycle.py`, `core/sandbox_local_runner/git_utils.py`  
**Config:** *(auto)* Dead lock detection by age  
**Description:** On startup, detect and clear stale lock files (older than 4h) and orphaned worktrees (git worktree prune).

- [ ] Lock file age check — delete locks `> 4h` old
- [ ] `git worktree prune` on startup
- [ ] Log cleaned items per cycle
- [ ] No config needed — runs automatically

**Why:** Every machine reboot orphans worktrees and leaves stale locks. The agent should survive a crash without manual cleanup.

### 1.3 Dedup Guard in Batch Creation
**Status:** ⬜ Not started  
**Files:** `core/sandbox_local_runner/batch_pr.py`, `qa_agent/healer.py`  
**Config:** *(new)* `github.pr_cleanup.duplicate_window_hours`  
**Description:** Before creating a batch PR, check if a PR with the same `title` + `base_branch` + `finding_id` was created in the last N hours. Skip if so.

- [ ] Query `gh pr list` with title/subject match
- [ ] Compare opened-at timestamp vs duplicate window
- [ ] Log `skipped-duplicate` with ref to existing PR
- [ ] Integrate with the detection from 1.1

**Why:** Batch cycle should never create PR #122-126 ever again.

---

## Phase 2 — Make It Observable (This Month)

### 2.1 Auto-Rebase Telemetry
**Status:** ⬜ Not started  
**Files:** `core/sandbox_local_runner/rebase_sweep.py`  
**Config:** *(none needed)*  
**Description:** Log structured telemetry for each rebase sweep: how many siblings found, how many rebased clean, how many conflicted, how many skipped. Expose as a heartbeat file.

- [ ] Emit structured JSON log entry per sweep
- [ ] Update `scripts/qa_watchdog.py` to parse and surface rebase stats
- [ ] Track rolling count: `rebases_completed`, `rebases_conflicted`, `rebases_skipped`
- [ ] Write to `state/rebase_stats.jsonl`

**Why:** Auto-rebase was shipped blind. We need to know if it's working, silent-merging conflicts, or never firing.

### 2.2 Agent Health Checks
**Status:** ⬜ Not started  
**Files:** `scripts/qa_watchdog.py`  
**Config:** *(none needed)*  
**Description:** Periodic smoke test — run `qa-agent --repo ky status --dry-run` and confirm it completes within 30s. Alert if it fails.

- [ ] Add `--smoke-test` mode that runs one cycle per repo in dry-run
- [ ] Watchdog reports pass/fail with duration
- [ ] Alert Sound if 3 consecutive failures

**Why:** If the agent crashes after a reboot or pip update, nobody knows until Sound checks manually.

### 2.3 Reboot-Resilient State
**Status:** ⬜ Not started  
**Files:** `core/sandbox_local_runner/lifecycle.py`, `qa_agent/runner.py`  
**Config:** *(none needed)*  
**Description:** On agent startup, detect stale git worktrees (from `git worktree list`), clean orphaned references, and repair state.json if corrupted.

- [ ] Replace manual `ls locks/` check with `git worktree list --porcelain` parsing
- [ ] Auto-prune unreachable worktrees
- [ ] Graceful recovery when `state/batches.jsonl` is missing/empty

**Why:** Every reboot currently breaks worktree references. The agent should survive a restart like any daemon.

### 2.4 Cost Tracking Per Cycle
**Status:** ⬜ Not started  
**Files:** `core/sandbox_local_runner/llm_utils.py` *(planned — model call wrapper)*  
**Config:** *(new)* `cost.max_cost_per_run`  
**Description:** Track total model API cost per cycle. Log tokens used per provider. Warn if a cycle exceeds budget.

- [ ] Count input + output tokens per model call
- [ ] Estimate cost using provider rate cards
- [ ] Write running total to `state/cost_log.jsonl`
- [ ] Soft limit: log warning if `> $2/run`
- [ ] Hard limit: skip LLM-dependent tasks if `> $10/run`

**Why:** If a cycle burns $15 in model tokens and finds zero issues, that's a tuning signal, not a success.

---

## Phase 3 — Make It Smart (Next)

### 3.1 Regression Detection
**Status:** ⬜ Not started  
**Files:** *(new)* `core/sandbox_local_runner/regression.py`  
**Config:** *(new)* `regression.*`  
**Description:** Before merging a PR, run a comparison between the PR branch and main. Detect removed tests, deleted exports, type breakage. Flag for human review.

- [ ] Diff `test/` directory — fail if existing tests are deleted without replacement
- [ ] Check public API surface — detect removed exports
- [ ] Block merge if regression score exceeds threshold

### 3.2 Multi-Repo Registry
**Status:** ⬜ Not started  
**Files:** `qa_agent/registry.py`, `registry.yaml`  
**Config:** `registry.yaml` (multi-repo)  
**Description:** Replace the hardcoded ky+zulip setup with a YAML registry. Add a repo via `qa-agent register <owner/repo>`, and the agent auto-discovers language + issues + PRs.

- [ ] Registry entry format: `{repo, url, language, auto_discover: bool}`
- [ ] Auto-discover new issues without config changes
- [ ] `qa-agent add-repo` CLI command
- [ ] Onboard defaults from existing repo patterns

### 3.3 Escalation Thresholds
**Status:** ⬜ Not started  
**Files:** `core/sandbox_local_runner/orchestrator.py`  
**Config:** *(new)* `escalation.*`  
**Description:** Detect patterns that indicate systemic problems. If 3 merge cycles fail in a row, don't retry — notify Sound. If the same finding reappears after being fixed, reopen with higher priority.

- [ ] Consecutive failure counter per cycle type
- [ ] Reopened-finding detection (issue lifecycle tracking)
- [ ] Sound notification via email on threshold breach

### 3.4 Test Suite for the Agent Itself
**Status:** ⬜ Not started  
**Files:** `tests/` (new test modules)  
**Config:** *(none)*  
**Description:** Unit tests for the agent's own code — `test_rebase_sweep.py`, `test_gh.py`, `test_batch_pr.py`. Run with `pytest`.

- [ ] Mock GitHub API for `gh.py` test isolation
- [ ] `test_rebase_sweep.py` — test stagger, dirty detection, empty-SHA guard
- [ ] `test_batch_pr.py` — test dedup logic
- [ ] CI integration: run on `git push` to standalone repo

---

## Appendix — Config Reference

| Phase | Key | Default | Description |
|-------|-----|---------|-------------|
| 1 | `github.pr_cleanup.stale_hours` | `48` | Close PRs older than this threshold |
| 1 | `github.pr_cleanup.duplicate_window_hours` | `24` | Skip PRs duplicating one created in this window |
| 1 | `github.pr_cleanup.max_duplicates` | `3` | Max open PRs for the same finding before alerting |
| 2 | `cost.max_cost_per_run` | `10.0` | Hard token cost limit per cycle (USD) |
| 2 | `cost.soft_warn_at` | `2.0` | Log warning if cost exceeds this (USD) |
| 3 | `regression.test_delete_threshold` | `3` | Max tests that can be removed before blocking |
| 3 | `escalation.consecutive_failures` | `3` | Notify Sound after N consecutive failures |
| 3 | `escalation.auto_reopen` | `true` | Reopen issue if same finding resurfaces |

---

*Last updated: 2026-05-06*
*Drafted by: @red_mnemo_bot, refined with @green_mnemo_bot*
