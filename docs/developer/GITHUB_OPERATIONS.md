<!-- CATEGORY: developer -->

# GitHub Operations

## Overview

The GitHub operations subsystem manages the full lifecycle of PRs and issues on GitHub. It has four major components:

| Component | File | Responsibility |
|-----------|------|---------------|
| **GitHub API layer** | `bluei/engine/gh.py` (959 lines) | PR creation, issue management, merge evaluation, merge execution |
| **Rebase sweep** | `bluei/engine/rebase_sweep.py` (493 lines) | Post-merge rebase of sibling PRs |
| **Stale PR cleanup** | `bluei/engine/clean_prs.py` (191 lines) | Duplicate/stale PR detection and cleanup |
| **Regression detection** | `bluei/engine/regression.py` (347 lines) | Pre-merge regression checks |
| **Rebase telemetry** | `bluei/engine/rebase_stats.py` (121 lines) | Structured JSONL telemetry for rebase operations |

All GitHub API calls go through the `gh` CLI (not the REST API directly). Every function wraps a `gh` CLI call via `run_capture` and returns `None`/empty on failure — no exceptions are raised.

This subsystem is invoked from `cli.py` (the main executor). The merge-cycle is part of the [ARCHITECTURE.md](ARCHITECTURE.md) cycle pipeline and integrates with the [REVIEW_LIFECYCLE.md](REVIEW_LIFECYCLE.md) autonomous review gate for merge gating. For the operator-facing commands, see [OPERATOR_GUIDE.md](../operator/OPERATOR_GUIDE.md).

---

## PR Merge Lifecycle

The merge-cycle determines which open PRs are eligible for automatic merging, evaluates them against multiple gates, and merges at most one PR per run.

### Single-Merge-Per-Run Behavior

**At most one PR is merged per merge-cycle invocation.** This is enforced by a `break` statement at line 2025 of `cli.py` — once a merge succeeds, the loop exits immediately. This provides a safety buffer: a batch of merges could cascade regressions, and the single-merge behavior lets each merge settle before the next cycle evaluates the updated state.

### Deterministic Oldest-First Ordering

PRs are fetched and sorted by `fetch_open_prs_for_merge()` in `gh.py` (line 313). The sort key is:

```python
(draft_rank, created_at, number)
```

Where `draft_rank` is `0` for non-drafts and `1` for drafts. This means:

1. **Non-drafts always come before drafts** — drafts are skipped by the merge eligibility check regardless
2. **Oldest created-at timestamp first** within the same draft status
3. **PR number as tiebreaker** (lower number first)

`gh pr list` fetches up to 200 open PRs with state, draft status, creation timestamp, head ref, and base ref.

### Merge Eligibility Checks

The merge-cycle evaluates each PR (in oldest-first order) against a cascade of gates. A PR must pass **all** gates to be merged.

#### Gate 1: Auto-Merge Flag

The `--auto-merge-sandbox` CLI flag must be set. Without it, the entire merge-cycle is skipped with a `merge-cycle-skip: auto-merge flag not enabled` message.

#### Gate 2: Repository Is Sandbox

`repo_is_sandbox()` (gh.py:293) validates that the repo is a designated sandbox or self-merge repo. A repo qualifies if:

- Its slug ends with `/qa-sandbox-repo`, OR
- It is listed in the `BLUEI_SELF_MERGE_REPOS` environment variable (comma-separated)

Non-sandbox repos are blocked with `merge-cycle-block: repo not sandbox`.

#### Gate 3: Non-Draft PR

Draft PRs are skipped with `merge-skip: draft=true`.

#### Gate 4: Merge Cooldown

The PR must be at least `--merge-cooldown-minutes` old (default: 30 minutes). This buffer gives time for human review before auto-merge. The age is computed as seconds since `createdAt`. PRs younger than the cooldown are skipped with `merge-skip: cooldown age_seconds=N required=N`.

#### Gate 5: CI Check Health

`evaluate_pr_check_health()` (gh.py:351) evaluates the PR's `statusCheckRollup`:

| State | Result |
|-------|--------|
| No checks found | `eligible: true`, `reason: no-checks-detected-proceed-cautiously` |
| No rollup data | `eligible: true`, `reason: checks-unavailable-proceed-cautiously` |
| FAILURE, TIMED_OUT, CANCELLED, ACTION_REQUIRED, ERROR | `eligible: false` |
| PENDING, IN_PROGRESS, QUEUED, EXPECTED (no failures) | `eligible: true`, `reason: checks-pending-no-failures` |
| All passing/neutral | `eligible: true`, `reason: checks-pass-or-neutral` |

#### Gate 6: Review Status (with Autonomous Fallback)

`evaluate_pr_reviews()` (gh.py:418) checks the PR's `latestReviews`:

- `CHANGES_REQUESTED` → blocked (`changes-requested`)
- `PENDING` → blocked (`review-pending`)
- `APPROVED` → passes (`review-check-pass`)
- No reviews, no branch protection requiring them → passes (`no-reviews-no-protection-pass`)
- No reviews, but branch protection requires them → blocked (`no-reviews-but-protection-requires-them`)

Comments-only reviews (`COMMENTED`) are treated as absent — they carry no substantive verdict.

**Autonomous review-artifact merge gating (Gate 6b):** If the GitHub review API says a PR is not eligible, the system falls back to the local autonomous review gate. This gate reads `review_state.json` (written by the review lifecycle engine — see [REVIEW_LIFECYCLE.md](REVIEW_LIFECYCLE.md)) and checks:

1. `last_action` must be `merge_ready`
2. `merge_state_status` must be `CLEAN`, `UNKNOWN`, or `UNSTABLE`
3. `actionable_comment_count` must be `0`
4. `active_change_requesters` must be empty
5. `last_review_comment_key` must be present (review artifact exists)

If all conditions pass, the PR is eligible via the autonomous gate. This is logged as `merge-autonomous-gate-pass`.

#### Gate 7: Merge State Status

`evaluate_pr_mergeability()` (gh.py:476) checks GitHub's `mergeStateStatus`:

| Status | Result |
|--------|--------|
| `CLEAN` | ✅ Mergeable |
| `UNSTABLE` | ✅ Mergeable if check health is eligible |
| `UNKNOWN` | ✅ Mergeable (proceed cautiously) |
| `BEHIND` | ❌ Blocked — triage back to pr-cycle |
| `DIRTY` | ❌ Blocked — triage back to pr-cycle |
| `BLOCKED` | ❌ Blocked |
| Unavailable | ✅ Mergeable (proceed cautiously) |

##### DIRTY/BEHIND Triage Back to pr-cycle

When `mergeStateStatus` is `DIRTY` or `BEHIND` and `requires_pr_fix` is `True`, the PR is triaged back to the fix cycle via `_triage_pr_back_to_fix_cycle()` (cli.py:192). This function sets the issue status to `pr_merge_conflict` and records the reason. On the next pr-cycle, the issue re-enters the queue for a new fix attempt on a rebased branch.

#### Gate 8: Regression Detection (Optional)

When `--regression-check` is set, regression checks run before merge (see [Regression Detection](#regression-detection) below). If any regressions are found, the merge is blocked.

### How merge-cycle Works End-to-End

The merge-cycle in `cli.py` (starting at line 1851) follows this flow:

```
cli.py: merge-cycle block (L1851)
  │
  ├─ Gate 1: Check --auto-merge-sandbox flag
  │    └─ Skip if not set
  ├─ Gate 2: Validate repo is sandbox
  │    └─ Block if not sandbox
  │
  ├─ fetch_open_prs_for_merge() → oldest-first sorted list
  │
  └─ for each PR (oldest first):
       ├─ Gate 3: Skip drafts
       ├─ Gate 4: Enforce merge cooldown (age >= cooldown_minutes)
       ├─ Gate 5: evaluate_pr_check_health()
       ├─ Gate 6: evaluate_pr_reviews()
       │    └─ Gate 6b: _autonomous_review_gate_passes() (fallback)
       ├─ Gate 7: evaluate_pr_mergeability()
       │    └─ DIRTY/BEHIND → _triage_pr_back_to_fix_cycle()
       ├─ Gate 8: --regression-check? → check_regressions()
       │
       └─ merge_pr() via `gh pr merge --merge --delete-branch`
            ├─ Success → mark issue as resolved_merged, trigger rebase sweep
            └─ Failure:
                 ├─ merge_failure_requires_pr_fix()? → triage back to pr-cycle
                 └─ Otherwise → merges_failed counter

Post-merge:
  ├─ If --auto-rebase-enabled: sweep_rebase() on sibling PRs
  └─ reconcile_open_workload() to refresh live open issue/PR counts
```

The command is assembled by `build_merge_cycle_command()` in `orchestrator.py` (line 410):

```
python -m bluei.engine.cli --run-phase merge-cycle --no-dry-run --auto-merge-sandbox
```

**Merge failure requiring PR fix:** `merge_failure_requires_pr_fix()` (gh.py:562) checks the error string from a failed merge for markers like `not mergeable`, `cannot be cleanly created`, `merge conflict`, `is behind the base branch`, or `head branch is out of date`. If any match, the issue is triaged back to `pr_merge_conflict` status for repair in the next pr-cycle.

---

## Rebase Sweep

After a successful merge, the rebase sweep (`rebase_sweep.py`) rebases sibling open PRs onto the updated base branch. This keeps the PR pool fresh and reduces merge conflicts.

### Post-Merge Rebase of Sibling PRs

The `sweep_rebase()` function (line 305 of `rebase_sweep.py`) is called from `cli.py` after each successful merge (only when `--auto-rebase-enabled` is set):

```python
rebase_result = sweep_rebase(
    repo_path=repo_path,
    gh_repo_slug=gh_repo_slug,
    merged_pr_number=pr_number,
    base_branch=base_branch,
    log_file=log_file,
    dry_run=args.dry_run,
    max_prs=args.rebase_max_prs,       # default: 5
    rebase_stats_file=rebase_stats_file,
)
```

### How Sibling PRs Are Fetched and Ordered

`_fetch_sibling_prs()` (line 25) uses the `gh` CLI to list open PRs targeting the same base branch:

```
gh pr list --repo owner/repo --base main --state open --json number,headRefName,headRepository,createdAt,body --limit 50
```

The merged PR is excluded by `pr_number`. Results are sorted **oldest first** by `createdAt` to maximize rebase success rate — older PRs are closer to the fork point and less likely to have accumulated complex conflicts.

Results are capped at `max_prs` (default: 5).

### Conflict Detection and Markers

PRs already containing the `CONFLICT_MARKER_HEADER` in their body are skipped. The marker format is:

```
🤖 Auto-rebase conflict detected
```

The full marker block appended to the PR body on conflict (via `_update_pr_body_with_conflict()`, line 205):

```
---
🤖 Auto-rebase conflict detected
Files: path/to/file1.py, path/to/file2.py
Skipped until resolved.
```

The `_has_conflict_marker()` helper (line 198) checks for the presence of this header in the PR body.

### Clean Rebase → Force-Push

Each sibling goes through `_rebase_sibling()` (line 100), which:

1. **Fetches** the head branch from origin
2. **Computes the fork point** via `git merge-base origin/{base} origin/{head}`
3. **Creates a local tracking branch** `rebase-sweep-{pr_number}` from `origin/{head_branch}`
4. **Rebases** via `git rebase --onto origin/{base_branch} {fork_point}`
5. **On success**: captures the new HEAD SHA, restores to `origin/{base_branch}` (detached state)
6. **On conflict**: captures conflicting files via `git diff --name-only --diff-filter=U`, aborts the rebase, and restores state

If the rebase succeeds, `_force_push_rebased()` (line 254) pushes using `--force-with-lease` (not bare `--force`) to avoid overwriting unknown remote changes:

```
git push --force-with-lease origin {local_branch}:{remote_branch}
```

After a successful push, the local tracking branch is cleaned up. The result is logged with the new HEAD SHA and a note indicating whether the PR had a pre-existing dirty state ("pre-existing-dirty" or "clean").

### Conflicted Rebase → Flag and Pull from Merge Rotation

If the rebase conflicts:

1. The conflicting file list is extracted from `git diff --name-only --diff-filter=U`
2. `_update_pr_body_with_conflict()` appends the `CONFLICT_MARKER_HEADER` block to the PR body
3. The PR is added to the `conflicted` list in the result
4. The conflict is logged with file paths

The conflict marker in the PR body is checked by merge-cycle — `_has_conflict_marker()` in `rebase_sweep.py` is checked during `sweep_rebase()` itself to skip already-conflicted PRs on subsequent sweeps.

### Rebase Telemetry (rebase_stats.jsonl)

`rebase_stats.py` provides structured JSONL telemetry via `log_rebase_stats()` and `load_rebase_stats()`:

Each sweep writes one JSONL entry to `state/rebase_stats.jsonl` containing:

```json
{
  "timestamp": "2026-05-23T12:00:00Z",
  "repo": "owner/repo",
  "merged_pr": 42,
  "base": "main",
  "dry_run": false,
  "rebases_attempted": 5,
  "rebases_succeeded": 3,
  "rebases_conflicted": 1,
  "rebases_skipped": 1,
  "duration_seconds": 12.345
}
```

`load_rebase_stats()` returns the most recent N entries (newest first), and `summary_from_stats()` computes aggregate counters and success rate across all entries. This telemetry is consumed by the escalation checker and health reporting surfaces.

### Staggering

Between each force-push, there is a 2-second sleep (`time.sleep(2)`) to allow verify windows between pushes.

---

## Stale PR Cleanup

The stale PR cleanup (`clean_prs.py`) identifies and closes duplicate and stale PRs. It runs as a standalone phase (`--run-phase clean-prs`) or can be triggered in the pipeline.

### Duplicate PR Detection

`_find_duplicate_prs()` (line 56) groups PRs by title pattern. It uses regex matching on the title:

- `fix: resolve N RULE findings` — extracts the rule name portion
- `fix: resolve RULE in ...` — extracts the rule name portion
- Falls back to the entire title if neither pattern matches

Within each group, PRs are sorted oldest-first. All but the newest (last) are marked as duplicates. Each duplicate is recorded with:

```python
{
    'type': 'duplicate',
    'pr_number': pr['number'],
    'title': pr['title'],
    'pattern': pattern,
    'age_hours': <float>,
    'reason': 'Superseded by #<newest_pr_number>',
}
```

### Staleness Threshold (48h Default)

`_find_stale_prs()` (line 110) checks `updatedAt` — if a PR hasn't been updated in `max_age_hours` (default: 48), it's considered stale. Each stale PR is recorded with:

```python
{
    'type': 'stale',
    'pr_number': pr['number'],
    'title': pr['title'],
    'age_hours': <float>,
    'reason': 'No activity in Nh (threshold: 48h)',
}
```

### Dedup Window (24h Default)

The `dedup_window` parameter (default: 24 hours) is passed to `clean_stale_prs()` but is currently unused in the duplicate detection logic — the `_find_duplicate_prs()` function groups by title pattern across all open PRs regardless of creation time. The parameter exists for future windowed dedup logic.

### How Cleanup Runs in the Pipeline

The cleanup runs as a standalone phase:

```bash
python -m bluei.engine.cli --run-phase clean-prs --repo-path /path/to/repo
```

In `cli.py` (line 768), the `clean-prs` phase:

1. Parses the GitHub repo slug from the origin URL
2. Calls `clean_stale_prs()` with configurable `stale_hours` (48 default) and `dedup_window` (24 default)
3. Returns a summary: `{closed, duplicates, stale, items}`

Duplicates are closed first, then stale PRs. PRs already in the duplicate list are removed from the stale list to avoid double-closing.

### Key Functions

| Function | Line | Purpose |
|----------|------|---------|
| `clean_stale_prs()` | 141 | Main entry point: scans, finds duplicates/stale, closes them |
| `_fetch_open_prs()` | 21 | Fetches all open PRs with details via `gh pr list` (limit 100) |
| `_find_duplicate_prs()` | 56 | Groups by title pattern, keeps newest, marks rest for closure |
| `_find_stale_prs()` | 110 | Finds PRs with no activity beyond staleness threshold |
| `_close_pr()` | 41 | Closes a PR with a comment explaining the reason |
| `_pr_age_hours()` | 99 | Computes age in hours from `createdAt` |

---

## Regression Detection

`regression.py` detects regressions in a PR before merging. It compares the PR branch against the base branch to find unwanted changes.

### What Regressions Are Checked Before Merge

Three categories of regressions are checked by `check_regressions()` (line 170):

```
check_regressions(repo_path, base_branch, head_branch, log_file) → List[Finding]
```

1. **Test deletions** — test files deleted or modified
2. **Export/API surface changes** — public API modules or init files deleted
3. **New lint violations** — new ruff violations introduced by the PR

### Test Deletions Detection

`_find_test_deletions()` (line 43) examines the `git diff --name-status` output for deleted (`D` status) files matching test prefixes:

- `tests/`
- `test_`
- `spec/`
- `__tests__/`

For each deleted test file, it estimates the number of test functions lost by checking the base-branch version with `git show {base}:{path}` and counting lines starting with:

- `def test_` / `async def test_` (Python)
- `it(` / `describe(` / `test(` (JavaScript/TypeScript)

**Note on renames**: A rename (D + A pair) triggers a false positive on the D side. The A side (new path) is not flagged. The docstring notes this as a known limitation. A future enhancement could pair D+A entries by content hash.

### Export/API Surface Changes Detection

`_find_export_changes()` (line 82) checks for deletions of:

- `__init__.py` files (strong signal of module re-export removal)
- Files in `/exports/` directories
- `index.ts` / `index.js` barrel files

Each finding is classified as:

- `module_init_deleted` — an `__init__.py` was deleted
- `export_module_deleted` — an export barrel module was deleted

### New Lint Violations Detection

`_find_lint_regressions()` (line 109) runs **ruff** on changed Python files only. It:

1. Checks if the project uses ruff (looks for `ruff.toml` or `[tool.ruff]` in `pyproject.toml`)
2. Identifies changed `.py` files from `git diff {base}...{head} -- *.py`
3. Runs `ruff check --select ALL --output-format concise --quiet` on only the changed files
4. Parses the output into structured findings with file, line number, rule code, and message

This is scoped to changed files only to avoid flagging pre-existing lint issues.

### Weighted Scoring

`compute_regression_score()` (line 302) produces a score from 0.0 (clean) to 1.0 (severe regression) using weighted factors:

| Factor | Max Weight | Per-Item Weight |
|--------|------------|----------------|
| Deleted test files | 0.3 | 0.15 per file |
| Deleted export modules | 0.4 | 0.4 per module |
| Deleted `__init__.py` files | 0.2 | 0.2 per file |
| New lint violations | 0.3 | 0.1 per violation |

The total is the sum of all factors, capped at 1.0.

### Blocks Merge at >0.5

In the merge-cycle (cli.py, starting at line 1966), when `--regression-check` is enabled:

1. The PR's head and base branches are determined
2. The head branch is fetched locally (`git fetch origin {head_ref}`)
3. `check_regressions()` runs comparing `origin/{base_ref}` vs `origin/{head_ref}`
4. If any regression findings are returned, the merge is **blocked** with a log entry: `regression: N finding(s) detected — blocking merge`

The PR-level `evaluate_pr_regression()` in `gh.py` (line 818) provides a higher-level wrapper that:

- Fetches PR metadata and diff
- Runs the same three regression checks
- Computes the score via `compute_regression_score()`
- Returns an action: `safe-to-merge` (score ≤ 0.3), `review-required` (0.3 < score ≤ 0.5), or `block-merge` (score > 0.5)

### Key Functions

| Function | Line | Purpose |
|----------|------|---------|
| `check_regressions()` | 170 | Main entry point: runs all three checks and returns findings |
| `compute_regression_score()` | 302 | Weighted scoring across all regression categories |
| `_find_test_deletions()` | 43 | Detects deleted test files and estimates function loss |
| `_find_export_changes()` | 82 | Detects deleted `__init__.py` and export barrel files |
| `_find_lint_regressions()` | 109 | Compares ruff violations between base and head branches |
| `_git_diff_names()` | 20 | Runs `git diff --name-status {base}...{head}` |
| `detect_removed_tests()` | 242 | Standalone function for detecting test deletions |
| `detect_export_changes()` | 271 | Standalone function for detecting export changes |

---

## Module Index

### `gh.py` — GitHub API Abstraction

| Function | Line | Purpose |
|----------|------|---------|
| `get_origin_url()` | 21 | Returns the git remote origin URL |
| `parse_github_repo()` | 29 | Extracts `(owner, repo)` from a remote URL |
| `finding_dedupe_marker()` | 56 | Generates a `[finding_id:...]` deduplication marker |
| `gh_json()` | 60 | Runs a `gh` CLI command and parses JSON output |
| `parse_issue_number_from_url()` | 79 | Extracts issue number from a GitHub issue URL |
| `parse_pr_number_from_url()` | 86 | Extracts PR number from a GitHub PR URL |
| `find_existing_github_issue()` | 93 | Searches for an existing issue by dedupe marker |
| `find_existing_github_pr()` | 130 | Searches for an existing PR by dedupe marker |
| `find_batch_pr_by_rule()` | 167 | Finds an existing batch PR for the same rule pattern |
| `gh_issue_comment()` | 223 | Posts a comment on a GitHub issue |
| `gh_issue_close()` | 231 | Closes a GitHub issue with a comment |
| `gh_pr_comment()` | 239 | Posts a comment on a GitHub PR |
| `finding_from_issue_record()` | 247 | Reconstructs a `Finding` from a persisted issue dict |
| `repo_is_sandbox()` | 293 | Checks if a repo is a sandbox or self-merge repo |
| `fetch_open_prs_for_merge()` | 313 | Fetches open PRs sorted: non-drafts first, oldest first, PR number tiebreaker |
| `evaluate_pr_check_health()` | 351 | Evaluates CI check status for a PR |
| `evaluate_pr_reviews()` | 418 | Evaluates PR review status and branch-protection requirements |
| `evaluate_pr_mergeability()` | 476 | Evaluates merge readiness from `mergeStateStatus` |
| `merge_failure_requires_pr_fix()` | 562 | Checks if a merge failure is fixable (conflict/behind) |
| `merge_pr()` | 583 | Merges a PR via `gh pr merge --merge --delete-branch` |
| `create_or_update_github_issue()` | 615 | Creates or updates an issue for a finding |
| `create_or_update_github_pr()` | 693 | Creates or updates a PR for a finding |
| `fetch_github_live_counts()` | 762 | Fetches live open issue/PR counts via GraphQL |
| `evaluate_pr_regression()` | 818 | Full regression evaluation of a PR (diff + checks) |

### `rebase_sweep.py` — Post-Merge Rebase

| Function | Line | Purpose |
|----------|------|---------|
| `sweep_rebase()` | 305 | Main entry point: rebases sibling PRs after a merge |
| `_fetch_sibling_prs()` | 25 | Fetches open PRs targeting the same base branch (oldest first) |
| `_compute_fork_point()` | 62 | Returns fork-point SHA and old base SHA for rebase |
| `_rebase_sibling()` | 100 | Attempts to rebase one sibling PR (fetch → rebase → capture/abort) |
| `_has_conflict_marker()` | 198 | Checks if a PR body contains the conflict marker |
| `_update_pr_body_with_conflict()` | 205 | Appends the conflict marker to a PR body |
| `_force_push_rebased()` | 254 | Force-pushes a rebased branch using `--force-with-lease` |
| `_append_text()` | 296 | Appends a timestamped line to the log file |

### `clean_prs.py` — Stale PR Cleanup

| Function | Line | Purpose |
|----------|------|---------|
| `clean_stale_prs()` | 141 | Main entry point: finds and closes stale/duplicate PRs |
| `_fetch_open_prs()` | 21 | Fetches all open PRs with details (limit: 100) |
| `_find_duplicate_prs()` | 56 | Groups PRs by title pattern, keeps newest, marks rest |
| `_find_stale_prs()` | 110 | Finds PRs with no activity beyond staleness threshold |
| `_close_pr()` | 41 | Closes a PR with a comment |
| `_pr_age_hours()` | 99 | Computes PR age in hours from `createdAt` |

### `regression.py` — Regression Detection

| Function | Line | Purpose |
|----------|------|---------|
| `check_regressions()` | 170 | Main entry point: runs all regression checks |
| `compute_regression_score()` | 302 | Weighted scoring (0.0–1.0) across all categories |
| `_git_diff_names()` | 20 | Runs `git diff --name-status {base}...{head}` |
| `_find_test_deletions()` | 43 | Finds deleted test files with function count estimates |
| `_find_export_changes()` | 82 | Finds deleted `__init__.py` and export barrel modules |
| `_find_lint_regressions()` | 109 | Compares ruff lint violations between branches |
| `detect_removed_tests()` | 242 | Standalone: returns list of deleted test file paths |
| `detect_export_changes()` | 271 | Standalone: returns list of export change descriptions |

### `rebase_stats.py` — Rebase Telemetry

| Function | Line | Purpose |
|----------|------|---------|
| `log_rebase_stats()` | 20 | Writes one telemetry entry to JSONL |
| `load_rebase_stats()` | 43 | Returns most recent N entries (newest first) |
| `summary_from_stats()` | 78 | Computes aggregate counters and success rate |

---

## Cross-References

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — Overall system architecture. Merge flow diagram at line 317. Module index entries for gh.py, regression.py, rebase_sweep.py, clean_prs.py.
- **[REVIEW_LIFECYCLE.md](REVIEW_LIFECYCLE.md)** — Autonomous review gate that feeds into merge-cycle's Gate 6b. `review_state.json` is the artifact consumed by `_autonomous_review_gate_passes()`.
- **[OPERATOR_GUIDE.md](../operator/OPERATOR_GUIDE.md)** — End-user commands. `bluei run <repo> --phase merge-cycle` triggers the subsystem.
- **[FIX_PIPELINE.md](FIX_PIPELINE.md)** — Regression detection is integrated into the T2 validation gate in the fix cascade.
