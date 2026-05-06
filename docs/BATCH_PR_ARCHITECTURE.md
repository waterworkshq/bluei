# Batch PR Engine — Architecture

**Date:** 2026-04-21
**Status:** Design v1 — awaiting review
**Scope:** Multi-finding PR creation to reduce PR churn

---

## 1. Problem

The QA agent creates **one PR per finding**, regardless of how trivial or related the fixes are. For micro issues like individual linter warnings (e.g. `ruff-c408`, `ruff-b007`, `ruff-s311`), this creates massive PR churn:

- 27 `ruff-c408` findings → 27 separate PRs
- 23 `ruff-b904` findings → 23 separate PRs
- Each PR carries overhead: worktree creation, branch, CI run, review cycle, merge

**Target:** Group related micro findings into a single PR. One PR fixes 10 `ruff-c408` issues across 10 files instead of 10 separate PRs.

---

## 2. Architecture Overview

### 2.1 Current Flow (1:1:1)

```
Finding → Issue → Worktree → Fix → PR → Merge
  (1)      (1)     (1)      (1)    (1)   (1)
```

Every finding gets its own isolated pipeline. No sharing.

### 2.2 Target Flow (N:1:1)

```
Finding ─┐
Finding ─┼→ Batch Group → Issue(s) → Worktree → Fix All → PR → Merge
Finding ─┘   (N:1)         (N:1)      (1)       (1)      (1)   (1)

Finding → Issue → Worktree → Fix → PR → Merge
 (1)      (1)     (1)      (1)    (1)   (1)
```

Some findings are batched; others remain solo. The grouping engine decides.

### 2.3 Core Components

```
┌─────────────────────────────────────────────────────────────┐
│                    BATCH PR ENGINE                            │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────────┐    ┌──────────────────┐                │
│  │ Batch Grouping   │    │ Batch State      │                │
│  │ Engine           │    │ Tracker          │                │
│  │                  │    │                  │                │
│  │ Input: eligible  │───→│ batch_id         │                │
│  │ findings         │    │ member_findings  │                │
│  │                  │    │ status           │                │
│  └──────────────────┘    │ worktree_path    │                │
│                           │ pr_number        │                │
│  ┌──────────────────┐    │ fix_results      │                │
│  │ Grouping Rules   │    └────────┬─────────┘                │
│  │ Registry         │             │                          │
│  │                  │             │                          │
│  │ rule → group_by  │             │                          │
│  │ (file, dir,      │    ┌────────▼─────────┐                │
│  │  framework,      │    │ Batch Fix        │                │
│  │  max_batch)      │───→│ Executor         │                │
│  │                  │    │                  │                │
│  └──────────────────┘    │ - create shared  │                │
│                           │   worktree       │                │
│  ┌──────────────────┐    │ - apply all fixes│                │
│  │ Batch PR         │    │ - verify all     │                │
│  │ Creator          │    │ - record results │                │
│  │                  │    │ - partial ok     │                │
│  │ - PR title:      │    └──────────────────┘                │
│  │   "Fix 12       │                                         │
│  │   ruff-c408     │    ┌──────────────────┐                │
│  │   findings"     │    │ Split/Recover    │                │
│  │ - Body: table   │    │ Handler          │                │
│  │   of findings   │    │                  │                │
│  │ - Link issues   │    │ - if batch fails │                │
│  └──────────────────┘    │   → split into   │                │
│                           │   sub-batches    │                │
│                           │ - if individual  │                │
│                           │   fails → solo   │                │
│                           └──────────────────┘                │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Grouping Rules

### 3.1 Rule Registry

A declarative registry defines which rules are eligible for batching and how to group them:

```yaml
batch_rules:
  - rule_pattern: "ruff-c408"        # exact match or prefix
    enabled: true
    group_by: "rule"                  # group all same-rule together
    max_batch_size: 20                # cap per PR
    max_files_per_batch: 15           # cap files touched
    max_loc_per_batch: 500            # cap lines of code changed
    isolation:                        # findings that MUST NOT be batched
      file_patterns:
        - "**/migrations/*.py"        # Django migrations — always solo
        - "**/test_*.py"              # test files — separate batch
    priority: 1                       # lower = higher priority for grouping

  - rule_pattern: "ruff-b904"
    enabled: true
    group_by: "rule"
    max_batch_size: 15
    max_files_per_batch: 10
    max_loc_per_batch: 300
    isolation:
      file_patterns:
        - "**/middleware*.py"
    priority: 2

  - rule_pattern: "ruff-s311"
    enabled: true
    group_by: "file"                  # group by file (multiple rules per file)
    max_batch_size: 10
    max_files_per_batch: 5
    max_loc_per_batch: 200
    priority: 3

  - rule_pattern: "ruff-*"            # catch-all for other ruff rules
    enabled: false                    # disabled by default
    group_by: "rule"
    max_batch_size: 10
    priority: 99
```

### 3.2 Grouping Strategies

| Strategy | Description | Example |
|----------|-------------|---------|
| `rule` | All findings with the same rule go into one batch | All `ruff-c408` findings → 1 batch |
| `file` | All findings in the same file go into one batch | All findings in `utils.py` → 1 batch |
| `directory` | Findings in the same directory subtree | All findings in `zerver/` → 1 batch |
| `framework` | Findings sharing the same detected framework context | All Django middleware findings → 1 batch |

### 3.3 Grouping Algorithm

```python
def group_findings_for_batch(eligible_findings, batch_rules):
    """Group findings into batches based on rules registry.
    
    Returns: List[BatchGroup]
    """
    batches = []
    ungrouped = []
    
    for rule_config in sorted(batch_rules, key=lambda r: r.priority):
        # Find matching findings
        matching = [
            f for f in eligible_findings
            if rule_matches(f.rule, rule_config.rule_pattern)
            and f.finding_id not in already_batched
        ]
        
        if not matching:
            continue
        
        # Apply isolation rules (exclude certain paths)
        isolatable = []
        batchable = []
        for f in matching:
            if is_isolated(f, rule_config.isolation):
                isolatable.append(f)
            else:
                batchable.append(f)
        
        ungrouped.extend(isolatable)
        
        # Split into batches respecting size limits
        for chunk in chunk_findings(batchable, rule_config):
            batches.append(BatchGroup(
                batch_id=generate_batch_id(),
                rule=rule_config.rule_pattern,
                group_by=rule_config.group_by,
                findings=chunk,
                max_files=rule_config.max_files_per_batch,
                max_loc=rule_config.max_loc_per_batch,
            ))
    
    # Remaining findings stay solo
    for f in ungrouped:
        if f.finding_id not in already_batched:
            batches.append(BatchGroup(
                batch_id=generate_batch_id(),
                rule=f.rule,
                group_by="solo",
                findings=[f],
            ))
    
    return batches
```

---

## 4. Batch State Model

### 4.1 Batch Record

```json
{
  "batch_id": "batch-20260421-c408-001",
  "rule_pattern": "ruff-c408",
  "group_by": "rule",
  "status": "open",
  "created_at": "2026-04-21T18:00:00Z",
  "findings": [
    {
      "finding_id": "abc123...",
      "path": "zerver/lib/message.py",
      "line": 42,
      "rule": "ruff-c408",
      "issue_id": "ISS-001",
      "fix_status": "pending"
    },
    {
      "finding_id": "def456...",
      "path": "zerver/views/home.py",
      "line": 15,
      "rule": "ruff-c408",
      "issue_id": "ISS-002",
      "fix_status": "pending"
    }
  ],
  "worktree_path": "/path/to/qa-sandbox-batch-001",
  "branch": "qa/batch-ruff-c408-20260421",
  "pr_number": null,
  "pr_url": null,
  "fix_results": {
    "abc123...": { "status": "success", "diff_lines": 1 },
    "def456...": { "status": "success", "diff_lines": 1 }
  },
  "total_fixes_attempted": 0,
  "total_fixes_succeeded": 0,
  "total_fixes_failed": 0,
  "retry_count": 0,
  "split_history": []
}
```

### 4.2 Batch Statuses

| Status | Meaning |
|--------|---------|
| `open` | Batch created, not yet being processed |
| `fixing` | Fixes are being applied in worktree |
| `fixing_partial` | Some fixes applied, others pending retry |
| `pr_created` | PR is open on GitHub |
| `pr_merged` | PR was merged |
| `failed` | Batch failed all retry attempts |
| `split` | Batch was split into smaller batches |
| `aborted` | Batch was abandoned (safety gate) |

---

## 5. Batch Fix Execution

### 5.1 Shared Worktree

Instead of one worktree per finding, batches share one worktree:

```python
def apply_batch_fixes(batch: BatchGroup, repo_path: Path, worktree_path: Path, log_file: Path) -> bool:
    """Apply all fixes in a batch within a shared worktree.
    
    Strategy:
    1. Create worktree (shared by all batch members)
    2. For each finding:
       a. Apply fix (autofix, contextual, or Claude)
       b. Record result (success/fail)
       c. If fail, mark for retry or split
    3. Verify all fixes together
    4. Commit all changes as one commit
    5. Push branch
    """
    results = {}
    
    for finding in batch.findings:
        result = apply_single_fix_in_batch(finding, worktree_path, log_file)
        results[finding.finding_id] = result
    
    batch.fix_results = results
    batch.total_fixes_attempted = len(results)
    batch.total_fixes_succeeded = sum(1 for r in results.values() if r.status == "success")
    batch.total_fixes_failed = sum(1 for r in results.values() if r.status == "failed")
    
    # If too many failed, consider splitting
    if batch.total_fixes_failed > batch.total_fixes_attempted * 0.5:
        return handle_batch_failure(batch, repo_path, log_file)
    
    # Commit all successful changes together
    if batch.total_fixes_succeeded > 0:
        git_commit_all(
            worktree_path,
            message=batch.pr_title(),  # "fix: resolve 12 ruff-c408 findings"
            log_file=log_file,
        )
    
    return batch.total_fixes_succeeded > 0
```

### 5.2 PR Creation

```python
def create_batch_pr(batch: BatchGroup, repo_slug: str, log_file: Path) -> dict:
    """Create a single PR for the entire batch.
    
    PR title: "fix: resolve {N} {rule} findings"
    PR body: Table of all findings with links to issues
    """
    title = batch.pr_title()
    body = batch.pr_body()  # markdown table of findings
    
    pr = create_or_update_github_pr(
        repo_slug=repo_slug,
        title=title,
        body=body,
        branch=batch.branch,
        batch_id=batch.batch_id,
        log_file=log_file,
    )
    
    # Link all issues to this PR
    for finding in batch.findings:
        issue = find_issue_by_id(finding.issue_id)
        if issue:
            issue['github']['pr_number'] = pr['number']
            issue['github']['pr_url'] = pr['url']
            issue['github']['batch_id'] = batch.batch_id
            set_issue_status(issue, 'pr_opened', f'batched in PR #{pr["number"]}')
    
    return pr
```

### 5.3 PR Body Template

```markdown
## Batch Fix: {N} {rule} findings

This PR resolves {N} findings of type `{rule}` across {M} files.

### Findings

| # | File | Line | Issue |
|---|------|------|-------|
| 1 | `zerver/lib/message.py` | 42 | [#ISS-001](...) |
| 2 | `zerver/views/home.py` | 15 | [#ISS-002](...) |
| ... |

### Scope
- Files changed: {N}
- Lines changed: {M}
- Fix method: {autofix|contextual|claude}

### Verification
- [ ] All target detectors no longer fire
- [ ] No baseline regressions

---
*Generated by qa-agent batch PR engine*
```

---

## 6. Split/Recovery Handler

When a batch fails (e.g., one fix breaks something), the system can split:

```python
def handle_batch_failure(batch: BatchGroup, repo_path: Path, log_file: Path):
    """Handle a batch that had too many failures.
    
    Strategy:
    1. Identify which individual fixes failed
    2. Split into sub-batches:
       - Successful fixes → create PR immediately
       - Failed fixes → create smaller batches or solo
    3. Record split history
    """
    successful = [f for f in batch.findings if batch.fix_results[f.finding_id].status == "success"]
    failed = [f for f in batch.findings if batch.fix_results[f.finding_id].status == "failed"]
    
    if successful:
        # Commit and PR the successful ones
        commit_successful_batch(successful, batch, log_file)
    
    if failed:
        if len(failed) == 1:
            # Just one failure → make it solo
            convert_to_solo(failed[0], log_file)
        else:
            # Multiple failures → split into smaller batches
            split_into_sub_batches(failed, batch, log_file)
    
    batch.status = "split"
    batch.split_history.append({
        "split_at": now_iso(),
        "successful_count": len(successful),
        "failed_count": len(failed),
        "reason": "too_many_fix_failures",
    })
```

---

## 7. Integration Points

### 7.1 Files Modified

| File | Change |
|------|--------|
| `core/sandbox_local_runner/batch_pr.py` | **NEW** — Batch grouping engine, execution, PR creation |
| `core/sandbox_local_runner/batch_rules.yaml` | **NEW** — Declarative grouping rules |
| `core/sandbox_local_runner/models.py` | Add `BatchGroup` dataclass, `BatchStatus` enum |
| `core/sandbox_local_runner/state.py` | Add batch state persistence (load/save batches) |
| `core/sandbox_local_runner/cli.py` | Wire batch engine into pr-cycle; fallback to solo |
| `core/sandbox_local_runner/constants.py` | Add `BATCH_RULES` catalog, defaults |
| `core/sandbox_local_runner/gh.py` | Add `create_batch_pr()` helper |

### 7.2 Pr-Cycle Integration

```python
# In cli.py run_pr_cycle:

# OLD: iterate issues one by one
for issue, finding in queue_candidates:
    ... # single worktree, single fix, single PR

# NEW: group into batches first
batch_groups = group_findings_for_batch(queue_candidates, batch_rules)

for batch in batch_groups:
    if len(batch.findings) == 1:
        # Solo finding — use existing single-finding path
        process_solo_finding(batch.findings[0], ...)
    else:
        # Batch — use new batch path
        process_batch(batch, ...)
```

### 7.3 Config Flags

```yaml
# In repo config.yaml
batch_pr:
  enabled: true
  min_batch_size: 2              # minimum findings to trigger batching
  max_batch_size: 20             # hard cap
  max_files_per_batch: 15
  max_loc_per_batch: 500
  split_on_failure: true         # split batch if fixes fail
  allow_cross_file_batch: true   # allow batching across different files
  rules:
    - "ruff-c408"
    - "ruff-b904"
    - "ruff-b007"
    - "ruff-s311"
```

---

## 8. Safety Properties

1. **No regression:** Solo-finding path unchanged when batching is disabled
2. **Batch isolation:** Each batch gets its own worktree — no cross-batch pollution
3. **Partial success ok:** If 8/10 fixes succeed, the PR ships with 8; 2 retry as smaller batches
4. **Size caps enforced:** `max_files`, `max_loc`, `max_batch_size` are hard limits
5. **No auto-merge:** Sound still merges manually — unchanged
6. **Review transparency:** PR body lists every finding so reviewers see the full scope

---

## 9. Future Capabilities Enabled

This batching infrastructure opens up:

1. **Cross-rule batching:** Group different but related rules (e.g., all `ruff-*` style fixes) into one "housekeeping" PR
2. **Priority batching:** Critical security fixes get solo PRs; style fixes get batched
3. **Time-window batching:** Collect findings over a window (e.g., 1 hour) and batch them together
4. **Dependency-aware batching:** Group findings that touch related code paths together
5. **Rollup PRs:** "Weekly linter cleanup" PR that batches all eligible findings from the week

---

## 10. Success Metrics

| Metric | Before | Target |
|--------|--------|--------|
| PRs per cycle (zulip, 50+ findings) | 50+ | 3-5 |
| Worktrees created per cycle | 50+ | 3-5 |
| CI runs per cycle | 50+ | 3-5 |
| Time per cycle | ~30 min | ~10 min |
| Human review overhead | 50 PRs to glance at | 3-5 PRs to review |

---

## 11. Rollout Plan

### Phase 1: Grouping Engine + Solo Fallback
- [ ] Add `BatchGroup` dataclass and `BatchStatus` enum to `models.py`
- [ ] Create `batch_rules.yaml` with initial rules for ruff-c408, ruff-b904, ruff-b007, ruff-s311
- [ ] Implement `group_findings_for_batch()` in new `batch_pr.py`
- [ ] Add batch state persistence to `state.py`
- [ ] Tests: grouping correctness, isolation rules

### Phase 2: Batch Fix Execution
- [ ] Implement `apply_batch_fixes()` — shared worktree, sequential fixes
- [ ] Implement `create_batch_pr()` — PR with finding table
- [ ] Wire into `cli.py` pr-cycle
- [ ] Config flag: `batch_pr.enabled`
- [ ] Tests: batch fix execution on zulip samples

### Phase 3: Split/Recovery
- [ ] Implement `handle_batch_failure()` — split logic
- [ ] Implement `convert_to_solo()` — failed finding → solo path
- [ ] Implement `split_into_sub_batches()` — recursive split
- [ ] Tests: split scenarios, partial success

### Phase 4: Cross-Rule + Rollup Batching
- [ ] Add `group_by: "cross-rule"` strategy
- [ ] Implement time-window collection
- [ ] Rollup PR templates
- [ ] Tests: cross-rule grouping
