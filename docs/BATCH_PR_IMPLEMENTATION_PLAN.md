# Batch PR Engine — Implementation Plan

**Date:** 2026-04-21
**Status:** Draft — pending Sound review
**Phases:** 4 (Grouping → Execution → Split/Recovery → Cross-Rule)
**Estimated effort:** 3-4 weeks

---

## Overview

This plan implements a batch PR engine for the QA agent that groups related micro findings (like individual linter warnings) into single PRs, reducing PR churn from 50+ PRs to 3-5 PRs per cycle.

**Key principle:** Issues remain 1:1 per finding (for tracking). Batching happens at PR creation time (multiple issues → one PR).

---

## Phase 1: Grouping Engine

**Goal:** Take a list of eligible findings and group them into batches based on declarative rules.

### Artifacts

| File | Description |
|------|-------------|
| `core/sandbox_local_runner/models.py` | Add `BatchGroup`, `BatchStatus`, `FixResult`, `BatchRule` dataclasses |
| `core/sandbox_local_runner/batch_rules.yaml` | Declarative batching rules |
| `core/sandbox_local_runner/batch_pr.py` | Grouping engine + BatchGroup operations |
| `core/sandbox_local_runner/constants.py` | Add `BATCH_RULES_DEFAULT`, `DEFAULT_BATCH_RULES_PATH` |
| `core/sandbox_local_runner/state.py` | Add `load_batches()`, `save_batch_record()`, `update_batch_record()` |
| `core/sandbox_local_runner/cli.py` | Add `load_batch_rules()` helper |
| `core/sandbox_local_runner/tests/test_batch_grouping.py` | Unit tests for grouping |

### Step 1.1: Data Models (models.py)

Add these to `models.py` alongside the existing `Finding` dataclass:

```python
# BatchStatus enum
class BatchStatus(str, enum.Enum):
    OPEN = "open"
    FIXING = "fixing"
    FIXING_PARTIAL = "fixing_partial"
    PR_CREATED = "pr_created"
    PR_MERGED = "pr_merged"
    FAILED = "failed"
    SPLIT = "split"
    ABORTED = "aborted"

# FixResult dataclass
@dataclass
class FixResult:
    finding_id: str
    status: str
    diff_lines: int = 0
    error: Optional[str] = None
    fix_method: str = "autofix"

# BatchRule dataclass
@dataclass
class BatchRule:
    rule_pattern: str
    enabled: bool = True
    group_by: str = "rule"
    max_batch_size: int = 20
    max_files_per_batch: int = 15
    max_loc_per_batch: int = 500
    isolation: dict = field(default_factory=dict)
    priority: int = 99

# BatchGroup dataclass (see design doc for full implementation)
@dataclass
class BatchGroup:
    batch_id: str
    rule_pattern: str
    group_by: str
    findings: list
    issues: list
    max_files: int = 15
    max_loc: int = 500
    status: str = "open"
    worktree_path: Optional[Path] = None
    branch: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    fix_results: dict = field(default_factory=dict)
    retry_count: int = 0
    split_history: list = field(default_factory=list)
    
    @property
    def is_solo(self): ...
    @property
    def file_count(self): ...
    def pr_title(self): ...
    def pr_body(self): ...
    @classmethod
    def from_solo(cls, ...): ...
    @classmethod
    def from_findings(cls, ...): ...
```

### Step 1.2: Batch Rules (batch_rules.yaml)

```yaml
# batch_rules.yaml
rules:
  - rule_pattern: "ruff-c408"
    enabled: true
    group_by: "rule"
    max_batch_size: 20
    max_files_per_batch: 15
    max_loc_per_batch: 500
    isolation:
      file_patterns:
        - "**/migrations/*.py"
    priority: 1

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

  - rule_pattern: "ruff-b007"
    enabled: true
    group_by: "rule"
    max_batch_size: 10
    max_files_per_batch: 10
    priority: 3

  - rule_pattern: "ruff-s311"
    enabled: true
    group_by: "file"
    max_batch_size: 10
    max_files_per_batch: 5
    priority: 4
```

### Step 1.3: Grouping Engine (batch_pr.py)

Core functions:

```python
def load_batch_rules(rules_path: Path) -> list[BatchRule]:
    """Load batch rules from YAML file."""

def rule_matches(finding_rule: str, rule_pattern: str) -> bool:
    """Match finding rule against pattern (exact or prefix)."""

def is_isolated(finding, isolation_config: dict) -> bool:
    """Check if finding should be excluded from batching."""

def chunk_findings(findings: list, rule_config: BatchRule) -> list[list]:
    """Split findings into chunks respecting size limits."""

def group_findings_for_batch(
    queue_candidates: list[tuple],
    batch_rules: list[BatchRule],
) -> list[BatchGroup]:
    """Main grouping entry point."""

def check_batch_conflicts(findings: list) -> list[tuple]:
    """Detect potential conflicts within a batch (same file, nearby lines)."""
```

### Step 1.4: State Persistence (state.py)

```python
def load_batches(path: Path) -> list[dict]:
    """Load batch records from JSONL file."""

def save_batch_record(path: Path, record: dict) -> None:
    """Append a batch record to the JSONL file."""

def update_batch_record(path: Path, batch_id: str, updates: dict) -> None:
    """Rewrite the batches file with an updated record."""
```

### Step 1.5: Constants (constants.py)

```python
DEFAULT_BATCH_RULES_PATH = AGENT_ROOT / "batch_rules.yaml"
DEFAULT_BATCH_STATE = Path("state/batches.jsonl")
BATCH_RULES_DEFAULT = [...]  # fallback if YAML not found
```

### Step 1.6: CLI Integration (cli.py)

Add argument parsing:
```python
parser.add_argument('--batch-pr-enabled', action='store_true', default=True)
parser.add_argument('--batch-pr-rules', type=Path, default=None)
```

Add helper:
```python
def load_batch_rules(args) -> list[BatchRule]:
    rules_path = args.batch_pr_rules or DEFAULT_BATCH_RULES_PATH
    if rules_path.exists():
        return batch_pr.load_batch_rules(rules_path)
    return BATCH_RULES_DEFAULT
```

### Step 1.7: Tests

```python
# test_batch_grouping.py
def test_group_same_rule():
    """All ruff-c408 findings group into one batch."""

def test_isolation_excludes():
    """Migration files are never batched."""

def test_chunking_respects_limits():
    """20 findings split into 2 batches of 10."""

def test_solo_fallback():
    """Non-batchable findings become solo batches."""

def test_conflict_detection():
    """Two findings in same file within 5 lines are flagged."""
```

### Phase 1 Acceptance Criteria

- [ ] `group_findings_for_batch()` correctly groups 10 `ruff-c408` findings into 1 batch
- [ ] Isolation rules exclude migration files from batches
- [ ] Chunking splits oversized batches
- [ ] Solo findings use existing single-finding path
- [ ] Batch state persists to `batches.jsonl`
- [ ] All tests pass

---

## Phase 2: Batch Fix Execution

**Goal:** Process batches in shared worktrees, apply all fixes, create batch PRs.

### Artifacts

| File | Description |
|------|-------------|
| `core/sandbox_local_runner/batch_pr.py` | Add `process_batch()`, `apply_single_fix_in_batch()`, `create_batch_pr()` |
| `core/sandbox_local_runner/cli.py` | Wire batch engine into pr-cycle |
| `core/sandbox_local_runner/gh.py` | Add `create_batch_pr()` helper (if needed) |
| `core/sandbox_local_runner/tests/test_batch_execution.py` | Integration tests |

### Step 2.1: Batch Processing (batch_pr.py)

```python
def process_batch(batch: BatchGroup, repo_path: Path, args, log_file: Path) -> bool:
    """Process a batch: worktree → fixes → PR.
    
    Returns True if PR was created.
    """
    if batch.is_solo:
        # Delegate to existing single-finding path
        return process_solo_finding(batch.findings[0], batch.issues[0], repo_path, args, log_file)
    
    # Create shared worktree
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    branch = f"qa/batch-{batch.rule_pattern.replace('ruff-', '')}-{ts}"
    worktree_path = args.worktree_root / f"qa-batch-{batch.batch_id}"
    
    _create_worktree(repo_path, worktree_path, branch)
    batch.worktree_path = worktree_path
    batch.branch = branch
    batch.status = BatchStatus.FIXING.value
    
    # Apply all fixes sequentially
    for finding in batch.findings:
        issue = _find_issue_for_finding(batch.issues, finding.finding_id)
        result = apply_single_fix_in_batch(finding, issue, worktree_path, repo_path, args, log_file)
        batch.fix_results[finding.finding_id] = result
    
    # Tally
    batch.total_fixes_attempted = len(batch.fix_results)
    batch.total_fixes_succeeded = sum(1 for r in batch.fix_results.values() if r.status == "success")
    batch.total_fixes_failed = sum(1 for r in batch.fix_results.values() if r.status == "failed")
    
    # Check failure rate
    failure_rate = batch.total_fixes_failed / max(batch.total_fixes_attempted, 1)
    if failure_rate > 0.5 and args.batch_pr_split_on_failure and batch.retry_count < 2:
        batch.retry_count += 1
        # Defer split to Phase 3 — for now, just log
        _append_text(log_file, f'batch: {batch.batch_id} would split (Phase 3)')
        return False
    
    # Commit successful changes
    if batch.total_fixes_succeeded > 0:
        git_commit_all(worktree_path, batch.pr_title(), log_file)
        git_push_branch(worktree_path, batch.branch, log_file)
        
        # Create PR
        pr = create_batch_pr(batch, args.repo_slug, log_file)
        batch.pr_number = pr['number']
        batch.pr_url = pr['url']
        batch.status = BatchStatus.PR_CREATED.value
        
        # Link all issues
        link_issues_to_batch_pr(batch, pr['number'], pr['url'], log_file)
        
        return True
    
    return False
```

### Step 2.2: Single Fix Within Batch

```python
def apply_single_fix_in_batch(finding, issue, worktree_path, repo_path, args, log_file):
    """Apply one fix within a shared worktree."""
    if finding.safe_to_autofix:
        applied = apply_autofix(worktree_path, finding, log_file)
        if applied:
            closed = verify_finding_closed(worktree_path, finding, log_file)
            if closed:
                return FixResult(finding_id=finding.finding_id, status="success", diff_lines=1)
            return FixResult(finding_id=finding.finding_id, status="failed", error="verification-failed")
        else:
            # Try contextual fix
            from .context_fix import apply_contextual_fix
            applied = apply_contextual_fix(repo_path, finding, log_file, worktree_path)
            if applied:
                return FixResult(finding_id=finding.finding_id, status="success", fix_method="contextual")
            return FixResult(finding_id=finding.finding_id, status="failed", error="no-fix-applied")
    
    # LLM fix path
    llm_rules = _get_llm_fixable_rules()
    if finding.rule in llm_rules:
        rc, _, _ = apply_claude_fix(worktree_path, finding, ..., log_file=log_file, repo_path=repo_path)
        if rc == 0:
            return FixResult(finding_id=finding.finding_id, status="success", fix_method="claude")
        return FixResult(finding_id=finding.finding_id, status="failed", error=f"claude rc={rc}")
    
    return FixResult(finding_id=finding.finding_id, status="skipped", error="no-fix-method")
```

### Step 2.3: PR Creation

```python
def create_batch_pr(batch: BatchGroup, repo_slug: str, log_file: Path) -> dict:
    """Create GitHub PR for batch."""
    title = batch.pr_title()
    body = batch.pr_body()
    branch = batch.branch
    
    rc, output = run_capture(
        ['gh', 'pr', 'create', '--repo', repo_slug, '--title', title,
         '--body', body, '--head', branch, '--base', 'main'],
        cwd=batch.worktree_path,
    )
    
    if rc != 0:
        raise RuntimeError(f"Failed to create batch PR: {output}")
    
    pr_url = output.strip()
    pr_number = int(pr_url.split('/')[-1])
    return {'number': pr_number, 'url': pr_url}

def link_issues_to_batch_pr(batch, pr_number, pr_url, log_file):
    """Update all issues to point to the shared PR."""
    for issue in batch.issues:
        issue_github = issue.setdefault('github', {})
        issue_github['pr_number'] = pr_number
        issue_github['pr_url'] = pr_url
        issue_github['batch_id'] = batch.batch_id
        set_issue_status(issue, 'pr_opened', f'batched in PR #{pr_number}')
```

### Step 2.4: Pr-Cycle Wiring (cli.py)

Replace the current pr-cycle loop:

```python
# In cli.py, the run_pr_cycle section:

# After building queue_candidates...

if args.batch_pr_enabled:
    batch_rules = load_batch_rules(args)
    batch_groups = group_findings_for_batch(queue_candidates, batch_rules)
    
    for batch in batch_groups:
        if created_prs >= args.max_prs_per_run:
            break
        
        ok, _ = process_batch(batch, repo_path, args, log_file)
        if ok:
            created_prs += 1
            open_prs += 1
else:
    # Existing single-finding path
    for issue, finding in queue_candidates:
        if created_prs >= args.max_prs_per_run:
            break
        process_single_finding(issue, finding, repo_path, args, log_file)
        created_prs += 1
        open_prs += 1
```

### Step 2.5: Tests

```python
# test_batch_execution.py
def test_batch_fixes_all_findings():
    """Apply fixes for all findings in a batch."""

def test_batch_partial_success():
    """Some fixes succeed, some fail — PR created for successes."""

def test_batch_pr_title_and_body():
    """PR title mentions count and rule; body has finding table."""

def test_batch_issue_linking():
    """All issues in batch are linked to the PR."""

def test_batch_solo_delegation():
    """Solo batches use existing single-finding path."""
```

### Phase 2 Acceptance Criteria

- [ ] Batch creates shared worktree for all findings
- [ ] All fixes are applied sequentially within the worktree
- [ ] PR is created with correct title and finding table
- [ ] All issues are linked to the batch PR
- [ ] Partial success creates PR for successes
- [ ] Solo batches delegate to existing path unchanged
- [ ] `batch_pr.enabled: false` falls back to existing behavior

---

## Phase 3: Split/Recovery

**Goal:** Handle batch failures by splitting into smaller batches and retrying failed findings.

### Artifacts

| File | Description |
|------|-------------|
| `core/sandbox_local_runner/batch_pr.py` | Add `handle_batch_failure()`, `split_batch()`, `commit_partial_batch()` |
| `core/sandbox_local_runner/batch_pr.py` | Add `check_batch_conflicts()`, conflict-driven splitting |
| `core/sandbox_local_runner/batch_pr.py` | Add `recover_interrupted_batch()` |
| `core/sandbox_local_runner/tests/test_batch_split.py` | Split/recovery tests |

### Step 3.1: Split Logic

```python
def handle_batch_failure(batch: BatchGroup, repo_path: Path, args, log_file: Path) -> list[BatchGroup]:
    """Handle a failed batch: commit successes, split failures."""
    successful = [f for f in batch.findings if batch.fix_results.get(f.finding_id, {}).status == "success"]
    failed = [f for f in batch.findings if batch.fix_results.get(f.finding_id, {}).status == "failed"]
    
    sub_batches = []
    
    # Commit successful fixes
    if successful:
        _commit_partial_batch(successful, batch, log_file)
    
    # Split failed findings
    if failed:
        if len(failed) <= 2:
            # Convert to solo
            for f in failed:
                issue = _find_issue_for_finding(batch.issues, f.finding_id)
                sub_batches.append(BatchGroup.from_solo(issue, f))
        else:
            # Split into halves
            half = max(len(failed) // 2, 1)
            for i in range(0, len(failed), half):
                chunk = failed[i:i+half]
                issues_map = {f.finding_id: _find_issue_for_finding(batch.issues, f.finding_id) for f in chunk}
                sub_batch = BatchGroup.from_findings(chunk, issues_map, BatchRule(
                    rule_pattern=batch.rule_pattern,
                    max_batch_size=half,
                ))
                sub_batches.append(sub_batch)
    
    # Record split
    batch.split_history.append({
        "split_at": now_iso(),
        "successful_count": len(successful),
        "failed_count": len(failed),
        "sub_batches_created": len(sub_batches),
    })
    batch.status = BatchStatus.SPLIT.value
    
    return sub_batches
```

### Step 3.2: Conflict Detection

```python
def check_batch_conflicts(findings: list) -> list[tuple]:
    """Find pairs of findings that might conflict."""
    by_file = {}
    for f in findings:
        by_file.setdefault(f.path, []).append(f)
    
    conflicts = []
    for path, file_findings in by_file.items():
        sorted_findings = sorted(file_findings, key=lambda f: f.line)
        for i in range(len(sorted_findings) - 1):
            if sorted_findings[i+1].line - sorted_findings[i].line < 5:
                conflicts.append((sorted_findings[i], sorted_findings[i+1]))
    
    return conflicts

def split_on_conflicts(batch: BatchGroup) -> list[BatchGroup]:
    """Split a batch at conflict boundaries."""
    conflicts = check_batch_conflicts(batch.findings)
    if not conflicts:
        return [batch]
    
    # Build conflict graph and split into non-conflicting groups
    conflict_pairs = set()
    for a, b in conflicts:
        conflict_pairs.add(a.finding_id)
        conflict_pairs.add(b.finding_id)
    
    non_conflicting = [f for f in batch.findings if f.finding_id not in conflict_pairs]
    conflicting = [f for f in batch.findings if f.finding_id in conflict_pairs]
    
    groups = []
    if non_conflicting:
        issues_map = {f.finding_id: _find_issue_for_finding(batch.issues, f.finding_id) for f in non_conflicting}
        groups.append(BatchGroup.from_findings(non_conflicting, issues_map, BatchRule(rule_pattern=batch.rule_pattern)))
    
    for f in conflicting:
        issue = _find_issue_for_finding(batch.issues, f.finding_id)
        groups.append(BatchGroup.from_solo(issue, f))
    
    return groups
```

### Step 3.3: Interrupted Batch Recovery

```python
def recover_interrupted_batch(batch_id: str, batches_file: Path) -> Optional[BatchGroup]:
    """Recover from an interrupted batch."""
    batch = load_batch_by_id(batch_id, batches_file)
    if batch is None:
        return None
    
    if batch.status in (BatchStatus.FIXING.value, BatchStatus.FIXING_PARTIAL.value):
        if batch.worktree_path and batch.worktree_path.exists():
            if batch.branch and branch_exists_on_remote(batch.branch):
                batch.status = BatchStatus.PR_CREATED.value
            else:
                batch.status = BatchStatus.ABORTED.value
        else:
            batch.status = BatchStatus.ABORTED.value
    
    return batch
```

### Step 3.4: Tests

```python
# test_batch_split.py
def test_split_on_high_failure_rate():
    """Batch with 60% failure rate splits into sub-batches."""

def test_split_single_failure_to_solo():
    """1 failed finding becomes solo."""

def test_split_multiple_failures_to_halves():
    """6 failed findings split into 2 batches of 3."""

def test_conflict_detection_and_split():
    """Nearby findings in same file are detected and split."""

def test_interrupted_batch_recovery():
    """Partially pushed batch is recovered correctly."""
```

### Phase 3 Acceptance Criteria

- [ ] Failed batch commits successes and splits failures
- [ ] Single failure → solo batch
- [ ] Multiple failures → smaller sub-batches
- [ ] Conflict detection splits problematic findings
- [ ] Interrupted batches are recovered or aborted cleanly
- [ ] Max split depth is respected (no infinite recursion)

---

## Phase 4: Cross-Rule + Optimization (Future)

**Goal:** Batch findings across different rules and optimize grouping.

### Planned Features

| Feature | Description |
|---------|-------------|
| Cross-rule batching | Group all `ruff-*` style fixes into one "housekeeping" PR |
| Time-window batching | Collect findings over a 1-hour window before batching |
| Priority-aware batching | Security findings → solo; style findings → batched |
| Rollup PRs | "Weekly linter cleanup" PR |
| Dependency-aware grouping | Group findings that touch related code paths |

### Implementation Notes

These features build on the Phase 1-3 foundation. The `group_by` field in `BatchRule` would support new strategies:
- `"cross-rule"`: Group by rule prefix (e.g., all `ruff-*`)
- `"time-window"`: Group by collection time window
- `"priority"`: Group by finding priority/severity

---

## Config Integration

### Repo Config (config.yaml)

Add to each repo's config:

```yaml
batch_pr:
  enabled: true
  min_batch_size: 2
  max_batch_size: 20
  max_files_per_batch: 15
  max_loc_per_batch: 500
  split_on_failure: true
  max_split_depth: 3
```

### CLI Defaults

```python
DEFAULT_BATCH_PR_ENABLED = True
DEFAULT_BATCH_PR_MAX_SIZE = 20
DEFAULT_BATCH_PR_MAX_FILES = 15
DEFAULT_BATCH_PR_MAX_LOC = 500
DEFAULT_BATCH_PR_SPLIT_ON_FAILURE = True
DEFAULT_BATCH_PR_MAX_SPLIT_DEPTH = 3
```

---

## File Summary

### New Files
| File | Phase |
|------|-------|
| `core/sandbox_local_runner/batch_pr.py` | 1 |
| `core/sandbox_local_runner/batch_rules.yaml` | 1 |
| `core/sandbox_local_runner/tests/test_batch_grouping.py` | 1 |
| `core/sandbox_local_runner/tests/test_batch_execution.py` | 2 |
| `core/sandbox_local_runner/tests/test_batch_split.py` | 3 |

### Modified Files
| File | Phase | Changes |
|------|-------|---------|
| `core/sandbox_local_runner/models.py` | 1 | Add BatchGroup, BatchStatus, FixResult, BatchRule |
| `core/sandbox_local_runner/state.py` | 1 | Add batch persistence functions |
| `core/sandbox_local_runner/constants.py` | 1 | Add batch rule defaults |
| `core/sandbox_local_runner/cli.py` | 1, 2 | Add batch args, wire into pr-cycle |

---

## Testing Matrix

| Test | Phase | Type | Scenario | Expected |
|------|-------|------|----------|----------|
| T1 | 1 | Unit | 10 ruff-c408 findings → 1 batch | 1 batch with 10 findings |
| T2 | 1 | Unit | Migration file finding → solo | 1 solo batch |
| T3 | 1 | Unit | 25 findings, max 10 → 3 batches | 3 batches (10, 10, 5) |
| T4 | 1 | Unit | Non-batchable rule → solo | 1 solo batch |
| T5 | 2 | Integration | Batch fix all findings → PR created | PR with all fixes |
| T6 | 2 | Integration | 8 succeed, 2 fail → PR + retry | PR with 8, 2 for retry |
| T7 | 2 | Integration | Solo batch → existing path | Identical to current behavior |
| T8 | 2 | Integration | batch_pr.enabled=false | Existing behavior unchanged |
| T9 | 3 | Unit | 60% failure → split | Sub-batches created |
| T10 | 3 | Unit | 1 failure → solo | Solo batch created |
| T11 | 3 | Unit | Conflict detected → split at boundary | Conflicting findings isolated |
| T12 | 3 | Unit | Interrupted batch → recovery | Correct status restored |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Fix conflicts within batch | Failed PR | Conflict detection + split-on-conflict |
| Batch too large for review | Sound rejects | Hard caps on files/LRs; PR body shows scope |
| Partial success creates confusion | Tracking gap | Each issue updated with batch PR link |
| Existing pr-cycle regression | Broken CI | Feature flag; solo path unchanged |
| Worktree collision | Corrupted state | Unique worktree paths per batch |

---

## Dependencies

- **Phase 1:** None — pure grouping logic
- **Phase 2:** Phase 1 + existing `apply_autofix`, `apply_claude_fix`, `apply_contextual_fix`
- **Phase 3:** Phase 2 + existing `verify_fix_closed`
- **Phase 4:** Phase 3 + future grouping strategies

---

## Success Criteria

| Metric | Before | After (Target) |
|--------|--------|----------------|
| PRs per cycle (zulip) | 50+ | 3-5 |
| Worktrees per cycle | 50+ | 3-5 |
| CI runs per cycle | 50+ | 3-5 |
| Time per cycle | ~30 min | ~10 min |
| Human review overhead | 50 PRs | 3-5 PRs |
