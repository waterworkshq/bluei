<!-- CATEGORY: developer -->

# Review Lifecycle

## Overview

The review lifecycle engine (`bluei/review/cycle.py`, ~5,900 lines) manages GitHub PR reviews autonomously within bluei's architecture. It is the largest single module in the codebase and is invoked by the runner when the phase is `review-cycle`.

The engine provides two main operating paths:

- **Observation cycle**: Read-only analysis of managed PRs — classifies review feedback, plans remediation, and posts status comments
- **Autonomous review cycle**: Full pipeline that generates candidate findings, classifies them, determines remediation eligibility, reconciles against prior state, and optionally publishes results to GitHub via a guarded live-path

The engine integrates deeply with [ARCHITECTURE.md](ARCHITECTURE.md)'s core engine layer, the [FIX_PIPELINE.md](FIX_PIPELINE.md) fix cascades (through backend templates), and [GITHUB_OPERATIONS.md](GITHUB_OPERATIONS.md) (through `gh` CLI subprocess calls).

```
  runner.py
       ↓
  ReviewCycleEngine.run()
```

## Review Modes

Three review modes are defined by the `ReviewMode` enum (`bluei/app/models.py`):

| Mode | Enum Value | Description |
|------|-----------|-------------|
| **Observation** | `observation` | Read-only. Fetches PR snapshots, classifies review feedback, plans remediation. Never pushes changes. |
| **Autonomous Review** | `autonomous-review` | Generates candidate findings from backend (claude/opencode) or local stub, normalizes, deduplicates, reconciles, and optionally publishes to GitHub via guarded live-publish path. |
| **Remediation** | `remediation` | Standalone remediation mode. Currently a stub — logs an event and returns neutral. |

Mode is configured in `repo.config.review_care.mode` and selected at runtime by `ReviewCycleEngine.run()`:

```
run() dispatches based on mode:
  observation       → _run_observation_cycle()
  autonomous-review → _run_autonomous_review_cycle()
  remediation       → _run_remediation_cycle()  [stub]
```

### Mode Configuration

```yaml
# repo config (review_care block)
review_care:
  mode: observation               # or autonomous-review, remediation
  enabled: true
  provider_order: ['github']
  max_attempts: 3                 # max remediation attempts per PR
  max_loops: 2                    # loop guard threshold
  max_prs_per_run: 1
  retry_delay_minutes: 15
  guarded_live_review: false      # Phase G4 gate
  live_rollout_mode: local_only   # Phase G5 rollout control
```

### Live Rollout Modes

Controlled by `LiveRolloutMode` enum (`bluei/app/models.py`):

| Mode | Enum Value | Behavior |
|------|-----------|----------|
| **Local Only** | `local_only` | No backend when `live_actions=True`. Safe local analysis only. Default. |
| **Shadow** | `shadow` | Backend generation + target resolution + summary build. Records intent as SHADOW entry. Never actually posts to GitHub. |
| **Limited** | `limited` | Full guarded path. Backend generation + live publication only when `guarded_live_review=True` AND `live_actions=True`. |

Mode resolution is handled by `_get_live_rollout_mode()` which validates the config value, falls back to `local_only` for unsafe combinations, and applies the guarded-live-review sub-gate for limited mode.

## PR Snapshot Fetching

### GraphQL Query

The `GRAPHQL_QUERY` (review.py:44) fetches PR data from the GitHub GraphQL API using `gh api graphql`. It queries:

```graphql
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number, url, title, isDraft, state, reviewDecision
      createdAt, updatedAt
      author { login }
      headRefName, headRepositoryOwner { login }
      mergeStateStatus
      reviews(last: 100) { nodes { author { login }, state, submittedAt } }
      reviewThreads(first: 100) { nodes { isResolved, isOutdated,           comments(last: 20) {
            nodes { author { login } body createdAt }
          } } }
      comments(last: 50) { nodes { author { login }, body, createdAt } }
    }
  }
}
```

### Fields Extracted

| Field | Source | Purpose |
|-------|--------|---------|
| `reviewDecision` | PR root | GitHub review decision (APPROVED, CHANGES_REQUESTED, etc.) |
| `mergeStateStatus` | PR root | Mergeability (CLEAN, BLOCKED, UNSTABLE, UNKNOWN) |
| `reviews` | Reviews connection | Latest review state per reviewer (deduped by reviewer, latest wins) |
| `reviewThreads` | Review threads | Unresolved, non-outdated threads with comment bodies |
| `comments` | PR comments | General PR comments (separate from review thread comments) |

### Snapshot Normalization

`_normalize_snapshot()` (review.py:242) converts the raw GraphQL response into a structured dict:

1. **Author filtering**: PR author's comments are excluded (self-review noise)
2. **Bot filtering**: Comments from known bots (github-actions, dependabot, renovate, codecov, greptile-apps, codeant-ai) are excluded
3. **Review dedup**: Per-reviewer, only the latest review state is kept
4. **Change requester extraction**: All reviewers whose latest state is `CHANGES_REQUESTED` are collected as `active_change_requesters`
5. **Comment classification**: Each comment body is classified as actionable or informational (see classification below)
6. **Fingerprint**: A SHA-256 hash of `(review_decision, merge_state_status, active_change_requesters, actionable_comments)` for change detection

The normalized snapshot contains:

```
{
  provider, fetched_at, pr_number, pr_url, branch, author,
  review_decision, merge_state_status,
  latest_review_states, active_change_requesters,
  unresolved_threads, actionable_comments, informational_comments,
  score_optional, checks_summary_optional, fingerprint
}
```

## Review Comment Classification

### Classification Logic

`_classify_comment()` (review.py:365) uses keyword-based markers to classify comments. The default is **informational** — comments are only actionable when they contain explicit blocking markers.

### Actionable Markers

```
"not safe to merge", "unsafe to merge", "must fix", "needs fix",
"should fix", "still fails", "will still fail", "won't work",
"will not work", "blocking", "changes requested", "request changes",
"regression", "broken", "incorrect", "bug",
"linting violation will still be reported"
```

### Informational Markers

```
"nit:", "optional", "consider", "could", "might", "suggestion"
```

### Bot Chatter Filtering

`_should_ignore_comment()` (review.py:341) filters out automation noise:

- `<!-- bluei-review-cycle:` — bluei's own review cycle comments
- `automated verification passed for finding` — verification bot messages
- `is reviewing your pr`, `finished reviewing your pr`, `is running incremental review` — review bot status messages
- `thanks for using codeant` — product marketing
- `**tip:**` — bot tips

`_is_bot()` (review.py:354) identifies bot accounts by `[bot]` suffix or known login names (github-actions, dependabot, renovate, codecov, greptile-apps, codeant-ai).

### Classification Taxonomy

Comments are partitioned into three categories in the snapshot:

| Category | Counted In | Meaning |
|----------|-----------|---------|
| `actionable_comments` | Triggers remediation | Contains blocking marker → must be addressed |
| `informational_comments` | Tracked, not blocking | Contains suggestion marker or no markers |
| Ignored | Not stored | Bot-generated or bluei's own comments |

## Remediation Workflow

### Observation-Cycle Path (7 Steps)

When actionable review feedback is detected on a managed PR:

```
1. PR snapshot fetched       → GraphQL API pull
2. Comments classified       → actionable vs informational vs ignored
3. Fingerprint compared      → change detection (new feedback vs stale)
4. Max-attempts check         → retry_exhausted if attempts >= max_attempts
5. Loop-guard check           → loop_guard_paused if fingerprint repeats > max_loops
6. Remediation planned        → prompt file + backend command + worktree prep
7. Remediation executed       → backend shell → collect changed files → validate → commit/push
```

### Step Details

**1. PR Snapshot** — `GitHubReviewProvider.fetch_review_snapshot(pr_number)` calls the GraphQL API and normalizes.

**2. Classification** — Happens during `_normalize_snapshot()`. Each comment passes through `_classify_comment()`.

**3. Fingerprint Check** — If `existing_fingerprint == snapshot.fingerprint` AND actionable comments exist, the loop_count increments. If fingerprints differ, loop_count resets to 0.

**4. Max-Attempts** — `attempts_used >= max_attempts` → status `retry_exhausted`. Locked out of further remediation.

**5. Loop-Guard** — `loop_count > max_loops` → status `loop_guard_paused`. Prevents infinite remediation on feedback that doesn't change despite fix attempts.

**6. Remediation Plan** (`_plan_remediation()`):

- Render markdown prompt (`_render_remediation_prompt()`) with actionable comments, constraints, Mnemo context
- Write prompt to `state/review_prompts/<repo>/pr-<N>.md`
- Resolve backend command (`_render_backend_command()`) from claude/opencode/deterministic template
- Create or reuse git worktree (`_prepare_worktree()`) checked out at PR branch

**7. Execution** (`_execute_prepared_remediation()`):

- Run backend shell command in worktree (900s timeout)
- Collect changed files (`git status --porcelain`)
- Run validation commands (test/lint/build from config or guessed)
- Apply commit/push boundary (`_apply_commit_push_boundary()`)

### Commit/Push Boundary

`_apply_commit_push_boundary()` (review.py:1082) controls whether changes are pushed:

| `allow_review_push` | Changed Files | Status |
|---------------------|--------------|--------|
| `False` (default) | Any | `pending_operator_confirmation` |
| `True` | None | `no_changes` |
| `True` | Present | `git add` → `git commit` → `git push origin HEAD:<branch>` → `pushed` |

The push boundary includes optional worktree cleanup (`cleanup_worktrees_after_push` config).

## Circuit-Breaker Safety

### MonitoredSafetyState

`MonitoredSafetyState` (`bluei/app/models.py:126`) implements a circuit-breaker / open-cooldown pattern:

```
                    ┌─────────────┐
                    │  CLOSED     │  (normal operation)
                    └──────┬──────┘
                           │ successive failures >= threshold
                           ▼
                    ┌─────────────┐
                    │  OPEN       │  (live publication blocked)
                    └──────┬──────┘
                           │ cooldown_seconds elapsed
                           ▼
                    ┌─────────────┐
                    │  CLOSED     │  (resume normal)
                    └─────────────┘
```

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `circuit_open` | `bool` | True when live publication is blocked |
| `failure_count` | `int` | Consecutive failures since last success |
| `cooldown_until` | `str` | ISO timestamp when cooldown expires |
| `last_failure_at` | `str` | ISO timestamp of most recent failure |
| `last_failure_reason` | `str` | Human-readable failure reason |
| `auto_rollback_active` | `bool` | Fail-closed rollback triggered |
| `auto_rollback_reason` | `str` | Rollback trigger summary |
| `auto_rollback_triggered_at` | `Optional[str]` | ISO timestamp when rollback was activated |

### Auto-Rollback

`_evaluate_feedback_auto_rollback()` triggers when recent negative feedback exceeds threshold:

- `monitored_auto_rollback_enabled` must be `True`
- Sliding window of `monitored_feedback_window` events (default 20)
- Minimum `monitored_feedback_min_events` (default 3)
- If `negative_ratio >= monitored_negative_feedback_threshold` (default 0.3) → rollback
- On rollback, suggests disabling `guarded_live_review` and falling back to `shadow` mode

### Publish Filters (Phase G6)

`_check_limited_publish_filters()` applies in LIMITED mode when the guard is active:

| Filter | Config Key | Default | Behavior |
|--------|-----------|---------|----------|
| Max findings count | `limited_max_findings_count` | 10 | Blocks if total findings exceeds limit |
| PR context required | `limited_require_pr_context` | True | Blocks if no PR context available |
| Allowed severities | `limited_allowed_severities` | None (all pass) | Blocks findings with non-allowlisted severities |
| Allowed headers | `limited_allowed_headers` | None (all pass) | Blocks findings with non-allowlisted headers |

All filters must pass for live publication. Failure → `filter-blocked` lifecycle phase.

## Chunk Ordering

Phase H provides deterministic file ordering for future multi-pass LLM review. Currently all modes use single-chunk (full_diff), but the infrastructure is ready.

### Language Priority

Files are ordered by language priority (descending), then file size (descending), then path (ascending):

```
typescript   > javascript  > python   > go
rust         > java        > cpp      > c
ruby         > php
```

The repo's declared primary language gets +100 priority boost. `order_files_for_chunking()` (review.py:4764) produces the deterministic ordering.

### ChunkManifest

`ChunkManifest` dataclass (review.py:4811) describes the chunk layout:

| Field | Description |
|-------|-------------|
| `mode` | CompressionMode (full_diff, compressed, multi_pass) |
| `token_budget` | Max tokens per pass (placeholder) |
| `total_files` | Total files in manifest |
| `total_chunks` | Number of chunks |
| `chunks` | List of file path lists per chunk |
| `ordering` | Deterministic ordering used |

## Review Artifact Validation

### What Constitutes a Review Artifact

A review artifact is a published bluei review-cycle comment on a PR. Artifacts are identified by the `publication_key`:

```
{fingerprint}:{status}         → general review comment
{fingerprint}:merge_ready      → merge-ready declaration
```

### Merge-Ready Gating

During observation cycle processing (review.py:1518–1599), merge readiness is determined by:

1. **No actionable blockers**: `snapshot.actionable_comments` is empty AND `active_change_requesters` is empty
2. **Artifact exists for snapshot**: `prior_comment_key` starts with `current_snapshot_prefix`
3. **Merge-ready artifact exists**: `prior_comment_key == current_merge_ready_key`

| Condition | Status | Merge State | Merge Reason |
|-----------|--------|-------------|-------------|
| Actionable comments present | `review_feedback_detected` | `blocked_by_review` | "N actionable review comments" |
| No blockers + merge-ready artifact | `merge_ready` | `ready_for_merge` | "QA review artifact exists for this snapshot" |
| No blockers + general artifact | `merge_ready` | `ready_for_merge` | "QA review artifact exists... pending fresh merge triage" |
| No blockers + no artifact | `pending_review` | `awaiting_review_artifact` | "bluei review... has not been published yet" |
| No blockers + non-CLEAN merge state | `awaiting_mergeability` | `awaiting_mergeability` | "merge state is {state}" |

### Autonomous Review Artifacts and Merge Gates

Autonomous review runs publish summary comments via `_post_summary_to_github()`. When these comments are posted to a PR that also has managed-PR state, the comment key pattern satisfies the merge-ready artifact detection in the observation cycle.

## State Machine

### Observation-Cycle States

```
    ┌──────────────┐
    │  pending_     │  PR exists in managed list, no snapshot fetched yet
    │  review       │
    └──────┬───────┘
           │ snapshot fetched
           ▼
    ┌──────────────┐     actionable comments present
    │ review_      │────────────────────────────┐
    │ feedback_    │                            │
    │ detected     │                            │
    └──────┬───────┘                            │
           │ no actionable blockers             │
           ▼                                    ▼
    ┌──────────────┐                   ┌──────────────────┐
    │ merge_ready  │                   │ remediation      │
    │              │                   │ planned/executed  │
    └──────────────┘                   └────────┬─────────┘
                                               │
                      ┌────────────────────────┼────────────────────────┐
                      │                        │                         │
                      ▼                        ▼                         ▼
              ┌──────────────┐        ┌──────────────┐          ┌──────────────┐
              │ retry_       │        │ retry_       │          │ retry_       │
              │ planned      │        │ prepared     │          │ lock_busy    │
              └──────┬───────┘        └──────┬───────┘          └──────────────┘
                     │                       │
                     │ remediation executed   │
                     ▼                       ▼
              ┌──────────────────────────────────────────┐
              │        execution outcomes:               │
              │  retry_executed     (backend succeeded)   │
              │  retry_pending_push (changes held)        │
              │  retry_pushed       (pushed to remote)    │
              │  retry_failed       (backend non-zero)    │
              │  retry_failed_      │
              │    validation       (tests failed)        │
              │  retry_failed_timeout(backend timed out)  │
              │  retry_failed_push  (commit/push failed)  │
              │  retry_no_changes    (no files modified)  │
              │  retry_exhausted     (max attempts)       │
              │  loop_guard_paused   (fingerprint loop)   │
              └──────────────────────────────────────────┘
```

### Autonomous-Review Lifecycle Phases

```
    ┌──────────────┐
    │ guard-       │  guarded_live_review disabled → local-only run
    │ disabled     │
    └──────────────┘

    ┌──────────────┐
    │ shadow-      │  live_rollout_mode=shadow → record intent, no GitHub API
    │ published    │
    └──────────────┘

    ┌──────────────┐
    │ filter-      │  limited publish filters failed → publication blocked
    │ blocked      │
    └──────────────┘

    ┌──────────────┐
    │ safety-      │  circuit breaker open → publication blocked
    │ blocked      │
    └──────────────┘

    ┌──────────────┐
    │ guarded-     │  target ambiguity, no open PR → publish refused
    │ live-refused │
    └──────────────┘

    ┌──────────────┐
    │ guarded-     │  gh pr comment failed (non-transient error)
    │ live-failed  │
    └──────────────┘

    ┌──────────────┐
    │ guarded-     │  summary comment successfully posted to GitHub
    │ live-        │
    │ published    │
    └──────────────┘
```

### State Transitions (Observation Cycle)

| From | To | Trigger |
|------|----|---------|
| (none) | `pending_review` | Managed PR exists, snapshot not yet processed |
| `pending_review` | `review_feedback_detected` | Actionable comments or change requesters found |
| `pending_review` | `merge_ready` | No blockers, review artifact exists for this snapshot |
| `pending_review` | `awaiting_mergeability` | No blockers but merge state is BEHIND/DIRTY/BLOCKED |
| `review_feedback_detected` | `retry_planned` | Remediation plan created (no worktree yet) |
| `review_feedback_detected` | `retry_prepared` | Worktree ready, prompt written |
| `review_feedback_detected` | `retry_exhausted` | `attempts_used >= max_attempts` |
| `review_feedback_detected` | `loop_guard_paused` | `loop_count > max_loops` |
| `review_feedback_detected` | `retry_lock_busy` | Lock held by another process |
| `retry_prepared` | `retry_executed` | Backend succeeded, no changes to push |
| `retry_prepared` | `retry_pending_push` | Changes validated, awaiting operator approval |
| `retry_prepared` | `retry_pushed` | Changes committed and pushed |
| `retry_prepared` | `retry_failed` | Backend non-zero exit |
| `retry_prepared` | `retry_failed_validation` | Tests/lint failed after changes |
| `retry_prepared` | `retry_failed_timeout` | Backend timed out |
| `retry_prepared` | `retry_failed_push` | Commit or push failed |
| `retry_prepared` | `retry_no_changes` | Backend made no file modifications |

## Key Functions & API

| Function/Method | Location | Description |
|----------------|----------|-------------|
| `GitHubReviewProvider.list_managed_prs()` | review.py:171 | Returns open, non-draft PRs matching managed-PR heuristics |
| `GitHubReviewProvider.fetch_review_snapshot(pr_number)` | review.py:210 | Fetches and normalizes a PR's review state via GraphQL |
| `GitHubReviewProvider._normalize_snapshot(pr)` | review.py:242 | Converts raw GraphQL to structured snapshot with fingerprint |
| `GitHubReviewProvider._classify_comment(body)` | review.py:365 | Classifies comment as actionable or informational |
| `GitHubReviewProvider._should_ignore_comment(body)` | review.py:341 | Filters bot-generated and automated-review comments |
| `GitHubReviewProvider._is_bot(login)` | review.py:354 | Identifies bot accounts |
| `ReviewCycleEngine.run(dry_run, allow_review_push)` | review.py:1316 | Mode dispatcher — routes to observation/autonomous/remediation cycle |
| `ReviewCycleEngine._run_observation_cycle(dry_run, allow_review_push)` | review.py:1343 | Full observation cycle: snapshot → classification → remediation |
| `ReviewCycleEngine._run_autonomous_review_cycle(dry_run, allow_review_push)` | review.py:2728 | Full autonomous review: candidate gen → normalization → publish |
| `ReviewCycleEngine._plan_remediation(snapshot, review_record, dry_run)` | review.py:961 | Creates remediation plan: prompt file, backend cmd, worktree |
| `ReviewCycleEngine._execute_prepared_remediation(plan, record, dry_run, ...)` | review.py:1173 | Executes remediation: backend shell → changes → validation → push |
| `ReviewCycleEngine._prepare_worktree(snapshot, dry_run)` | review.py:817 | Creates/reuses git worktree at PR branch |
| `ReviewCycleEngine._apply_commit_push_boundary(worktree_path, ...)` | review.py:1082 | Commits and optionally pushes changed files |
| `ReviewCycleEngine._publish_review_cycle_comment(pr_number, ...)` | review.py:531 | Posts review-cycle status comment to GitHub (with dedup) |
| `ReviewCycleEngine._check_monitored_safety()` | review.py:2510 | Checks circuit-breaker state for live publication |
| `ReviewCycleEngine._get_live_rollout_mode()` | review.py:2312 | Reads and validates live_rollout_mode from config |
| `ReviewCycleEngine._check_limited_publish_filters(...)` | review.py:2399 | Applies publish filters for limited mode |
| `normalize_candidate(raw)` | review.py:3893 | Validates and normalizes a raw candidate finding dict |
| `assign_finding_identity(normalized)` | review.py:4014 | Assigns deterministic finding_id and fingerprint |
| `dedupe_findings(findings)` | review.py:4057 | Removes exact structural duplicates from findings list |
| `is_remediation_eligible(finding, ...)` | review.py:4088 | Computes whether a finding can be auto-remediated |
| `reconcile_publish_state(current_candidates, prior_publish_state)` | review.py:4258 | Compares current findings against prior publish state |
| `order_files_for_chunking(files, language)` | review.py:4764 | Orders files deterministically by language priority + size |
| `build_chunk_manifest(files, mode, ...)` | review.py:4847 | Builds ChunkManifest from file list |

## Module Index

| Module | Role |
|--------|------|
| `bluei/review/cycle.py` | Main review lifecycle engine (~5,900 lines): snapshot fetching, comment classification, observation cycle, autonomous review cycle, remediation workflow, circuit-breaker, publish filters, chunk ordering, candidate validation, reconciliation |
| `bluei/app/models.py` | Data models and enums: `ReviewMode`, `LiveRolloutMode`, `MonitoredSafetyState`, `PublishStatus`, `FindingActionability`, `FindingSeverity`, `FindingSource`, `FeedbackSentiment`, `FeedbackSource`, `ReviewRunStatus`, `CompressionMode`, `ReviewFinding`, `ReviewSummary`, `ReviewRun`, `FeedbackEvent`, `LearnedRule`, `LearnedRuleStatus`, fingerprinting helpers |
| `bluei/app/state.py` | Persistent state: `StateManager` for review PR state, publish state, safety state, review events, review findings, review runs, learned rules |
| `bluei/app/runner.py` | Cycle orchestration: `_run_review_cycle()` creates `ReviewCycleEngine` and dispatches |
| `bluei/app/escalation.py` | Structured JSONL log for unrecoverable states (permanently unreachable PRs) |
