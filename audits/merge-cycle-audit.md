# Merge-Cycle Audit Report

**Date:** 2025-05-04  
**Auditor:** Red (QA Agent Code Review)  
**Scope:** Merge-cycle logic, state handling, review gate, GitHub API interaction, edge cases  
**Files reviewed:**
- `qa_agent/review.py` — ReviewCycleEngine, observation/autonomous cycles
- `qa_agent/runner.py` — RunEngine, lock management
- `qa_agent/state.py` — StateManager, file persistence
- `core/sandbox_local_runner/gh.py` — `evaluate_pr_reviews`, `merge_pr`, `evaluate_pr_mergeability`
- `core/sandbox_local_runner/cli.py` — merge-cycle phase, autonomous review gate

---

## Summary

The merge-cycle has solid fundamentals: atomic writes, file locks, unattended-push protection, and a COMMENTED-review patch in `evaluate_pr_reviews`. However, the audit found **21 issues** across failure handling, race conditions, gate logic gaps, and hardcoded assumptions. Three are critical.

---

## Critical Issues

### C1. DISMISSED reviews bypass the merge gate entirely
**Severity:** 🔴 Critical  
**Location:** `core/sandbox_local_runner/gh.py:297-310` (`evaluate_pr_reviews`)

**Description:** The `evaluate_pr_reviews` function filters out `COMMENTED` reviews via `substantive_states = states - {'COMMENTED'}`, then checks for `CHANGES_REQUESTED` and `APPROVED`. However, GitHub's `DISMISSED` review state is **never checked**. A reviewer can request changes, the PR author can dismiss the review, and the gate will see an empty `substantive_states` set — falling through to "no reviews, no branch protection → proceed".

**Current behavior:** A PR with a dismissed CHANGES_REQUESTED review (and no other reviews) on an unprotected branch will be treated as eligible for merge.

**Expected behavior:** DISMISSED reviews should be filtered out (they carry no weight), but the gate should log or track that a dismissal occurred. More importantly, when `substantive_states` is empty after filtering, the function falls through to checking branch protection — which may not require reviews on many repos.

**Suggested fix:**
```python
# In evaluate_pr_reviews, after filtering COMMENTED:
substantive_states = states - {'COMMENTED', 'DISMISSED'}
```
Additionally, consider adding a config option to require at least one APPROVED review even when no branch protection exists, to prevent the "no reviews = eligible" path from being a default merge path.

---

### C2. Observation cycle state overwrite loses PRs not in current GitHub query
**Severity:** 🔴 Critical  
**Location:** `qa_agent/review.py:~1275-1285` (`_run_observation_cycle`)

**Description:** At the start of each observation cycle, the code creates fresh `active_records` and `review_records` dicts, then only populates them from `managed_prs` (the current GitHub query result). Previous PR records are loaded for reference but the new records dict replaces the old one entirely via `_persist_review_state`. If a PR temporarily disappears from the GitHub API (network blip, transient listing issue, pagination limit), all its state — including `attempts_used`, `loop_count`, `last_review_comment_key` — is silently dropped.

**Current behavior:** A transient GitHub API gap during `list_managed_prs` causes permanent loss of review remediation state for any PR not returned. The PR will restart with `attempts_used=0` on the next cycle, potentially re-attempting fixes that already failed or exhausting retry budgets.

**Expected behavior:** PRs that were active in the previous state but absent from the current GitHub listing should be preserved (possibly in a "stale" or "temporarily_unreachable" status) rather than silently dropped.

**Suggested fix:** Before overwriting, merge stale PRs from `previous_active_records` that are not in the current `managed_prs` list:
```python
# Preserve PRs not in current managed list
current_pr_numbers = {str(p["number"]) for p in managed_prs}
for pr_key, prev_record in previous_active_records.items():
    if pr_key not in current_pr_numbers:
        active_records[pr_key] = {
            **prev_record,
            "status": "temporarily_unreachable",
            "updated_at": now_iso(),
        }
        review_records[pr_key] = previous_review_records.get(pr_key, {})
```

---

### C3. Review-state file race between sandbox_local_runner and qa_agent review cycle
**Severity:** 🔴 Critical  
**Location:** `qa_agent/state.py` (atomic writes) vs `core/sandbox_local_runner/cli.py` (direct JSON reads)

**Description:** The `qa_agent` review cycle (`ReviewCycleEngine`) writes `review_state.json` and `active_prs.json` via `StateManager.save_review_state()` which uses atomic `_atomic_json_write`. However, the sandbox runner's merge-cycle reads `review_state.json` via `_load_review_state` using a raw `json.loads(review_state_file.read_text())`. While atomic rename prevents torn reads, both systems can **logically race**: the review cycle can update the state while the merge-cycle is mid-evaluation, causing the merge decision to be based on stale or inconsistent state. The sandbox runner has **no lock** on the review state file, and the review cycle's PR-level locks (`_acquire_pr_lock`) don't gate the merge-cycle's reads.

**Current behavior:** Two concurrent runs (review-cycle + merge-cycle orchestrated mode) can read/write `review_state.json` simultaneously. The merge-cycle may read a half-updated state where some PRs reflect the current cycle but others don't.

**Expected behavior:** The merge-cycle should either acquire a read lock on the review state or the two cycles should be serialized via the existing repo-level phase lock (`_acquire_lock` in `RunEngine`).

**Suggested fix:** Ensure orchestrated runs never run review-cycle and merge-cycle concurrently for the same repo. The `RunEngine._acquire_lock` already locks per `(repo_name, phase)`, so the same repo with different phases can race. Either:
1. Use a single lock key for both phases (e.g., `"review-cycle"` and `"merge-cycle"` share a lock), or
2. Have the merge-cycle acquire an advisory read lock on `review_state.json`.

---

## High Issues

### H1. No retry/backoff on `gh pr list` or GraphQL calls during observation
**Severity:** 🟠 High  
**Location:** `qa_agent/review.py` — `list_managed_prs`, `fetch_review_snapshot`

**Description:** The observation cycle calls `gh pr list` and `gh api graphql` with `check=False` and no retry. A transient network error or rate limit returns an empty/error result that propagates as an exception in `_run()` → RuntimeError → caught at the PR-iteration level in the `finally` block. If this happens for the first PR, the entire cycle's state may be partially persisted.

**Current behavior:** Transient API errors abort the PR observation; partial state may be written (due to `_persist_review_state` calls inside the PR loop).

**Expected behavior:** Transient API errors should be retried with backoff; the cycle should be resilient to per-PR failures without losing state for already-processed PRs.

**Suggested fix:** Add retry logic (2-3 attempts with exponential backoff) to `_run()` in `GitHubReviewProvider`, similar to the retry pattern already used in `_post_summary_to_github`.

---

### H2. `_persist_review_state` called multiple times per PR in observation cycle
**Severity:** 🟠 High  
**Location:** `qa_agent/review.py:~1490-1550`

**Description:** Within the observation cycle's PR loop, `_persist_review_state` is called 3+ times per PR: once after the initial assessment, once after remediation execution, and once after the review comment. Each call writes the full state file atomically. If any intermediate write fails (disk full, permissions), later writes may succeed with inconsistent state. Additionally, this creates excessive I/O.

**Current behavior:** Multiple redundant atomic writes per PR per cycle. If the process crashes between writes, state may be in an inconsistent intermediate state.

**Expected behavior:** State should be written once at the end of each PR's processing, or use a transactional batch approach.

**Suggested fix:** Move the per-PR state persistence to a single write at the end of each PR's processing block (before the `finally` clause).

---

### H3. `merge_pr` doesn't handle the case where GitHub auto-merges via merge queue
**Severity:** 🟠 High  
**Location:** `core/sandbox_local_runner/gh.py:417-430`

**Description:** `merge_pr` calls `gh pr merge --merge --delete-branch`. If the repo uses a GitHub merge queue, the PR may already be enqueued or auto-merged by the time this runs. The command will fail with a non-zero exit code, and the failure is counted as `merges_failed`. The issue tracker won't mark the issue as resolved.

**Current behavior:** Merge-queued PRs are treated as merge failures.

**Expected behavior:** Detect the "already merged" / "in merge queue" response and treat it as success (or at least not count it as a failure).

**Suggested fix:** Parse the error output for markers like "already merged", "merge queue", or "Auto-merge is already enabled", and return `(True, 'already-merged-or-queued')`.

---

### H4. `evaluate_pr_mergeability` doesn't handle `HAS_HOOKS` merge state
**Severity:** 🟠 High  
**Location:** `core/sandbox_local_runner/gh.py:329-403`

**Description:** GitHub's `mergeStateStatus` can return `HAS_HOOKS` (mergeable with passing pre-receive hooks). The function's fallthrough at line 401 returns `eligible: True` with `reason: 'mergeable-state-pass'`, which is correct. However, this state isn't explicitly documented in the code, making it fragile for future maintainers who may add a new condition that accidentally catches `HAS_HOOKS` in a non-eligible path.

**Current behavior:** Works correctly by fallthrough but is undocumented.

**Expected behavior:** Explicit handling with a clear comment.

**Suggested fix:** Add explicit handling for `HAS_HOOKS`:
```python
if merge_state == 'HAS_HOOKS':
    return {
        'eligible': True, 'requires_pr_fix': False,
        'merge_state_status': merge_state,
        'reason': 'mergeable-with-hooks',
    }
```

---

### H5. Worktree cleanup failure is silent and leaves stale worktrees
**Severity:** 🟠 High  
**Location:** `qa_agent/review.py` — `_prepare_worktree`, observation cycle cleanup

**Description:** When a worktree already exists for a PR, `_prepare_worktree` skips creation (line `if not path.exists()`). But if a previous run crashed without cleaning up, the worktree may be in a dirty or detached state. The code will try to use it as-is, potentially running the backend command against stale content. There's no validation of the worktree's state (branch correctness, cleanliness).

**Current behavior:** Stale/dirty worktrees are reused without validation.

**Expected behavior:** Verify the worktree is on the correct branch and is clean, or force-recreate it.

**Suggested fix:** Add worktree validation:
```python
if path.exists():
    # Verify it's on the correct branch
    current_branch = self._run_repo_cmd(["git", "branch", "--show-current"], cwd=path, check=False)
    if current_branch.strip() != local_branch:
        self._cleanup_worktree(path)
        # Recreate below
```

---

### H6. Loop count incremented on remediated fingerprint match but never reset on actual reviewer re-engagement
**Severity:** 🟠 High  
**Location:** `qa_agent/review.py:~1262-1274`

**Description:** The `loop_count` is incremented when the fingerprint hasn't changed and `previous_attempted_remediation` is true. It's reset to 0 when the fingerprint changes. However, if a reviewer posts new comments (changing the fingerprint) but the actionable content is essentially the same concern rephrased, `loop_count` resets and the PR can loop indefinitely. The loop guard only triggers when the fingerprint is identical across remediation attempts.

**Current behavior:** A reviewer who keeps rephrasing the same concern generates new fingerprints each time, resetting the loop counter.

**Expected behavior:** Consider a semantic-level loop guard (e.g., tracking the reviewer + approximate topic) in addition to the fingerprint-level guard.

**Suggested fix:** Add a secondary loop guard based on the combination of `active_change_requesters` count and the count of actionable comments remaining stable across multiple remediation attempts, regardless of fingerprint.

---

## Medium Issues

### M1. `retry_pending_push` status persists across cycles without escalation
**Severity:** 🟡 Medium  
**Location:** `qa_agent/review.py:~1295-1303`

**Description:** When `allow_review_push=False`, the PR enters `retry_pending_push` state. On the next cycle, if the fingerprint hasn't changed, it stays in this state indefinitely. There's no escalation or timeout for `retry_pending_push` — it will never auto-resolve and never alert the operator beyond the review comment.

**Current behavior:** PRs in `retry_pending_push` stay there forever if the operator doesn't intervene.

**Expected behavior:** After N cycles in `retry_pending_push`, escalate (e.g., send notification, increase severity, or eventually mark as `retry_exhausted`).

**Suggested fix:** Add a `pending_push_cycles` counter; after 3 cycles, transition to `retry_exhausted` with a clear reason.

---

### M2. `_classify_comment` defaults to "actionable" for ambiguous comments
**Severity:** 🟡 Medium  
**Location:** `qa_agent/review.py:~250-270`

**Description:** The comment classifier checks for blocking markers, then informational markers, and **falls through to "actionable"** for anything that doesn't match either set. This means comments like "thanks for the update" or "I'll review this later" — which contain neither blocking nor informational markers — are classified as actionable, potentially blocking merge.

**Current behavior:** Non-blocking, non-informational comments (e.g., acknowledgments) are treated as actionable blockers.

**Expected behavior:** Default classification should be "informational" (non-blocking) unless the comment contains clear blocking signals.

**Suggested fix:** Change the default return from `"actionable"` to `"informational"`:
```python
return "informational"  # Default: non-blocking
```

---

### M3. `list_managed_prs` hardcodes `--limit 50` — large repos may miss PRs
**Severity:** 🟡 Medium  
**Location:** `qa_agent/review.py:~180-190`

**Description:** The managed PR listing is capped at 50. On repos with many open PRs, managed PRs beyond position 50 are never observed or acted upon.

**Current behavior:** Only the first 50 open PRs are checked.

**Expected behavior:** Paginate through all open PRs, or at least make the limit configurable.

**Suggested fix:** Add pagination support or increase the limit to a configurable value (default 200).

---

### M4. `review_events.jsonl` grows unbounded with no rotation
**Severity:** 🟡 Medium  
**Location:** `qa_agent/state.py:append_review_event`

**Description:** Review events are appended to a JSONL file with no size or age-based rotation. In active repos, this file can grow to tens of MBs over weeks, slowing down any consumer that reads it (e.g., `_evaluate_feedback_auto_rollback` loads the entire file).

**Current behavior:** Unbounded growth; full-file reads for feedback evaluation.

**Expected behavior:** Rotate or compact the events file periodically.

**Suggested fix:** Add a `rotate_review_events` method that keeps only the last N events or events from the last N days, triggered at the start of each cycle.

---

### M5. `feedback_events.jsonl` also grows unbounded
**Severity:** 🟡 Medium  
**Location:** `qa_agent/state.py:append_feedback_event`, `load_feedback_events`

**Description:** Same as M4 but for feedback events. `_evaluate_feedback_auto_rollback` loads all events and slices `[-window:]`, which still requires reading the entire file.

**Suggested fix:** Same as M4 — rotation/compaction.

---

### M6. Autonomous review's `_generate_local_candidates` only scans Python files for long lines
**Severity:** 🟡 Medium  
**Location:** `qa_agent/review.py:~3575-3610`

**Description:** The long-line scanner has two loops over `repo_path.rglob("*.py")` — the same glob pattern is used for both the TODO/FIXME scan and the long-line scan. TypeScript, Go, Rust, Java files are never scanned for long lines, even though the repo may be configured for those languages.

**Current behavior:** Only Python files are scanned for long lines and TODO markers.

**Expected behavior:** Scan files matching the repo's configured language.

**Suggested fix:** Use language-aware glob patterns based on `self.repo.config.language`.

---

### M7. No rate-limit awareness in the observation cycle's sequential API calls
**Severity:** 🟡 Medium  
**Location:** `qa_agent/review.py:~1275` (observation cycle loop)

**Description:** The observation cycle makes one GraphQL API call per managed PR, plus one `gh pr comment` per PR. With 20 managed PRs, that's 40+ API calls in rapid succession. GitHub's secondary rate limit can kick in, causing failures that are not retried.

**Current behavior:** Rapid sequential API calls with no rate-limit awareness.

**Expected behavior:** Add small delays between PRs or batch the API calls.

**Suggested fix:** Add a configurable delay (e.g., 2 seconds) between PR processing iterations.

---

### M8. `_atomic_json_write` doesn't fsync before rename
**Severity:** 🟡 Medium  
**Location:** `qa_agent/state.py:_atomic_json_write`

**Description:** The atomic write writes to a `.tmp` file and renames, but doesn't `fsync` before rename. On a crash, the temp file may not be fully flushed to disk, and `os.replace` will move a partially-written file. This is a well-known POSIX pitfall.

**Current behavior:** Crash during write may produce a corrupt file.

**Expected behavior:** `fsync` the temp file before rename.

**Suggested fix:**
```python
def _atomic_json_write(path: Path, data: Any) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
```

---

### M9. `MonitoredSafetyState.cooldown_until` comparison uses string comparison
**Severity:** 🟡 Medium  
**Location:** `qa_agent/models.py` (referenced by `MonitoredSafetyState.check_cooldown_ready`)

**Description:** The cooldown check compares ISO timestamp strings directly. While this works for ISO 8601 timestamps in the same timezone format, it can break if one timestamp has a `Z` suffix and another has `+00:00` or includes microseconds.

**Current behavior:** May incorrectly evaluate cooldown status if timestamp formats differ.

**Expected behavior:** Parse both timestamps to `datetime` objects for comparison.

**Suggested fix:** Use `datetime.fromisoformat()` for both sides of the comparison, normalizing timezone handling.

---

## Low Issues

### L1. `_run_review_cycle` doesn't propagate exceptions to run status
**Severity:** 🟢 Low  
**Location:** `qa_agent/runner.py:_run_review_cycle`

**Description:** `_run_review_cycle` catches no exceptions — any exception propagates to the outer `try` in `run()`, which sets `run.status = 'error'`. However, the outer `finally` calls `_release_lock` and `self.state.save_run`, which may themselves fail if the error is filesystem-related.

**Current behavior:** Cascading failure possible.

**Suggested fix:** Wrap `_run_review_cycle` in its own try/except with explicit error handling.

---

### L2. `generate_id` is not cryptographically unique
**Severity:** 🟢 Low  
**Location:** `qa_agent/models.py:generate_id`

**Description:** Run IDs are generated with `uuid.uuid4().hex[:12]` (or similar). While collisions are extremely unlikely, using a longer hex or full UUID would eliminate any concern.

**Suggested fix:** Use full UUID or at least 16+ hex chars.

---

### L3. `_should_ignore_comment` markers are hardcoded and not configurable
**Severity:** 🟢 Low  
**Location:** `qa_agent/review.py:_should_ignore_comment`

**Description:** The list of ignored comment markers (e.g., `"is reviewing your pr"`, `"**tip:**"`) is hardcoded. New bot integrations would require code changes.

**Suggested fix:** Make the ignore list configurable via `review_care.ignored_comment_markers`.

---

### L4. GraphQL query fetches `last: 100` reviews — insufficient for heavily-reviewed PRs
**Severity:** 🟢 Low  
**Location:** `qa_agent/review.py:GRAPHQL_QUERY`

**Description:** The GraphQL query uses `reviews(last: 100)` and `reviewThreads(first: 100)`. PRs with more than 100 reviews or threads will have incomplete data.

**Suggested fix:** Add pagination support or increase the limit.

---

## Rebase Recovery Design Considerations (Not Yet Implemented)

Since rebase recovery is not yet implemented, here are the failure modes the design should account for:

1. **Rebase conflicts during auto-rebase:** The rebase may conflict with the base branch. The system needs to detect conflicts and either auto-resolve trivial ones or escalate.

2. **Force-push invalidates worktrees:** After a rebase, the local branch history diverges from the remote. Any existing worktrees become invalid. The design must clean up worktrees before rebase and recreate them after.

3. **Review state invalidation:** After a rebase, the PR's review snapshot fingerprint will change (new commit SHA). The `loop_count` and `retry_eligible` state must be preserved across rebase, not reset.

4. **Branch protection `BEHIND` state:** The merge-cycle currently blocks on `BEHIND` merge state with no remediation. Rebase recovery is the intended fix — the design must handle the race between the merge-cycle detecting `BEHIND` and the rebase cycle completing.

5. **Concurrent rebase and merge:** Two concurrent cycles may attempt to rebase and merge the same PR. The design needs either a lock per PR or a state machine that prevents conflicting operations.

6. **Rebase during active review:** If a reviewer is mid-review when a rebase happens, their in-progress review may be invalidated. The design should check for pending reviews before rebasing.

---

## GitHub API Interaction Edge Cases

### Token Expiry
- **Current behavior:** No handling. All `gh` calls fail with authentication errors; the cycle treats these as generic failures.
- **Recommendation:** Detect `401`/`403` authentication errors specifically and log a distinct "gh-auth-expired" event.

### Rate Limiting
- **Current behavior:** `_post_summary_to_github` has retry with backoff (good). The observation cycle's GraphQL calls do not.
- **Recommendation:** Add rate-limit detection to `_run()` in `GitHubReviewProvider` (check stderr for rate limit markers).

### Network Failures Mid-Operation
- **Current behavior:** A network failure during `merge_pr` leaves the PR in an unknown state — the merge may have succeeded on GitHub's side but the response was lost.
- **Recommendation:** After a merge failure, re-check the PR's state before counting it as a failure.

---

## Hardcoded Assumptions

| Assumption | Location | Risk |
|---|---|---|
| Python-only file scanning | `_generate_local_candidates` | Non-Python repos get no local candidates |
| `--limit 50` for PR listing | `list_managed_prs` | Large repos miss PRs |
| `.py` extension for long-line scan | Long-line scanner | TypeScript/Go repos unaffected |
| 1-hour timeout for sandbox runner | `runner.py:timeout=3600` | Complex repos may need more time |
| 15-second timeout for `gh pr comment` | `_publish_review_cycle_comment` | May be too short for large comment bodies |
| `max_prs_per_run * 20` PR processing cap | Observation cycle | Formula is unclear and may exceed API limits |
| Review comment body truncated at 400 chars | `_normalize_text` | Important context may be lost |

---

*End of audit report.*
