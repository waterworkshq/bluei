# Batch PR Engine — Design Spec

**Date:** 2026-04-21
**Author:** Red
**Status:** Design v1 — awaiting review

---

## 1. Problem Statement

The QA agent creates one PR per finding. For micro issues (individual linter findings), this causes:

- **PR churn**: 50 findings → 50 PRs, each needing CI, review, merge
- **Wasted compute**: 50 worktrees, 50 CI runs, 50 branch operations
- **Review fatigue**: Sound has to glance at 50 PRs for trivial single-line fixes
- **Cap starvation**: `open_prs_cap` fills up fast, blocking other work

**Concrete evidence:** zulip has 51 open issues, mostly `ruff-c408` (27) and `ruff-b904` (23). If these were all fixable, they'd create 50 PRs for single-line `dict() → {}` changes.

---

## 2. Design Goals

1. **Reduce PR count** by grouping related micro findings into single PRs
2. **Maintain safety**: each finding is still independently verified
3. **Partial success tolerance**: if some fixes in a batch fail, ship the successes and retry failures
4. **Backward compatible**: solo-finding path unchanged; opt-in via config
5. **Observable**: PR body lists all findings; batch state tracked in findings JSONL
6. **Extensible**: grouping rules are declarative; new strategies easy to add

---

## 3. Grouping Engine

### 3.1 Rule Matching

```python
def rule_matches(finding_rule: str, rule_pattern: str) -> bool:
    """Match a finding's rule against a batch rule pattern.
    
    Supports:
    - Exact match: "ruff-c408" == "ruff-c408"
    - Prefix match: "ruff-b904" matches "ruff-*"
    - Regex (future): "ruff-(c408|b904)"
    """
    if "*" in rule_pattern:
        import fnmatch
        return fnmatch.fnmatch(finding_rule, rule_pattern)
    return finding_rule == rule_pattern
```

### 3.2 Isolation Rules

Some findings MUST NOT be batched, even if they match a batch rule:

```python
def is_isolated(finding, isolation_config) -> bool:
    """Check if a finding should be excluded from batching.
    
    Reasons for isolation:
    - File is in a sensitive path (migrations, config)
    - File is a test file (different review standards)
    - Finding has prior fix failures
    - Finding is in a complex file (>500 lines)
    """
    for pattern in isolation_config.file_patterns:
        if fnmatch.fnmatch(finding.path, pattern):
            return True
    return False
```

### 3.3 Chunking Algorithm

```python
def chunk_findings(findings: list, rule_config) -> list[list]:
    """Split findings into chunks respecting size limits.
    
    Respects:
    - max_batch_size: max findings per chunk
    - max_files_per_batch: max unique files per chunk
    - max_loc_per_batch: estimated max lines changed
    
    Strategy: greedy fill — add findings to current chunk
    until a limit is hit, then start a new chunk.
    """
    chunks = []
    current = []
    current_files = set()
    current_est_loc = 0
    
    for finding in findings:
        file_in_chunk = finding.path not in current_files
        
        if (len(current) >= rule_config.max_batch_size or
            (file_in_chunk and len(current_files) >= rule_config.max_files_per_batch)):
            chunks.append(current)
            current = []
            current_files = set()
            current_est_loc = 0
        
        current.append(finding)
        current_files.add(finding.path)
        current_est_loc += 1  # estimate: 1 line per micro fix
    
    if current:
        chunks.append(current)
    
    return chunks
```

### 3.4 Grouping Entry Point

```python
def group_findings_for_batch(
    queue_candidates: list[tuple[dict, Finding]],
    batch_rules: list[BatchRule],
) -> list[BatchGroup]:
    """Main grouping function.
    
    Input: list of (issue, finding) tuples from pr-cycle queue
    Output: list of BatchGroup objects (some solo, some multi-finding)
    
    Algorithm:
    1. Sort batch rules by priority
    2. For each rule, find matching findings
    3. Separate isolated findings (always solo)
    4. Chunk remaining findings into batches
    5. Any ungrouped findings become solo batches
    """
    batches = []
    batched_ids = set()
    
    for rule_config in sorted(batch_rules, key=lambda r: r.priority):
        matching = [
            (issue, f) for issue, f in queue_candidates
            if rule_matches(f.rule, rule_config.rule_pattern)
            and f.finding_id not in batched_ids
        ]
        
        if not matching:
            continue
        
        # Separate isolated from batchable
        isolated = []
        batchable = []
        for issue, f in matching:
            if is_isolated(f, rule_config.isolation):
                isolated.append((issue, f))
            else:
                batchable.append((issue, f))
        
        # Create solo batches for isolated findings
        for issue, f in isolated:
            batches.append(BatchGroup.from_solo(issue, f))
            batched_ids.add(f.finding_id)
        
        # Chunk and create multi-finding batches
        findings_only = [f for _, f in batchable]
        for chunk in chunk_findings(findings_only, rule_config):
            issues_map = {f.finding_id: issue for issue, f in batchable}
            group = BatchGroup.from_findings(chunk, issues_map, rule_config)
            batches.append(group)
            batched_ids.update(f.finding_id for f in chunk)
    
    # Any remaining findings become solo
    for issue, f in queue_candidates:
        if f.finding_id not in batched_ids:
            batches.append(BatchGroup.from_solo(issue, f))
    
    return batches
```

---

## 4. Data Structures

### 4.1 BatchGroup

```python
@dataclass
class BatchGroup:
    """A group of findings to be fixed in a single PR."""
    
    batch_id: str                     # e.g. "batch-20260421-c408-001"
    rule_pattern: str                 # e.g. "ruff-c408"
    group_by: str                     # "rule" | "file" | "directory" | "solo"
    findings: list[Finding]           # findings in this batch
    issues: list[dict]                # corresponding issue records
    
    # Limits (from batch rule config)
    max_files: int = 15
    max_loc: int = 500
    
    # Execution state
    status: BatchStatus = BatchStatus.OPEN
    worktree_path: Optional[Path] = None
    branch: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    
    # Fix results (per finding)
    fix_results: dict[str, FixResult] = field(default_factory=dict)
    
    # Retry/split tracking
    retry_count: int = 0
    split_history: list[dict] = field(default_factory=list)
    
    @classmethod
    def from_solo(cls, issue: dict, finding: Finding) -> "BatchGroup":
        """Create a solo batch (single finding)."""
        return cls(
            batch_id=f"solo-{finding.finding_id[:8]}",
            rule_pattern=finding.rule,
            group_by="solo",
            findings=[finding],
            issues=[issue],
        )
    
    @classmethod
    def from_findings(
        cls,
        findings: list[Finding],
        issues_map: dict[str, dict],
        rule_config,
    ) -> "BatchGroup":
        """Create a multi-finding batch."""
        ts = datetime.now().strftime('%Y%m%d')
        rule_short = findings[0].rule.replace("ruff-", "")[:8]
        batch_id = f"batch-{ts}-{rule_short}-{uuid4().hex[:4]}"
        
        return cls(
            batch_id=batch_id,
            rule_pattern=findings[0].rule,
            group_by=rule_config.group_by,
            findings=findings,
            issues=[issues_map[f.finding_id] for f in findings],
            max_files=rule_config.max_files_per_batch,
            max_loc=rule_config.max_loc_per_batch,
        )
    
    @property
    def is_solo(self) -> bool:
        return len(self.findings) == 1
    
    @property
    def file_count(self) -> int:
        return len(set(f.path for f in self.findings))
    
    def pr_title(self) -> str:
        if self.is_solo:
            f = self.findings[0]
            return f"fix: resolve {f.rule} in {f.path}"
        return f"fix: resolve {len(self.findings)} {self.rule_pattern} findings"
    
    def pr_body(self) -> str:
        if self.is_solo:
            return self._solo_body()
        return self._batch_body()
    
    def _solo_body(self) -> str:
        f = self.findings[0]
        return f"""## Fix: {f.rule}

- **File:** `{f.path}`
- **Line:** {f.line}
- **Rule:** `{f.rule}`
- **Snippet:** {f.snippet}

---
*Generated by qa-agent*
"""
    
    def _batch_body(self) -> str:
        files = set(f.path for f in self.findings)
        rows = []
        for i, f in enumerate(self.findings, 1):
            issue_num = None
            for issue in self.issues:
                if issue.get('finding_id') == f.finding_id:
                    issue_num = issue.get('github', {}).get('issue_number')
                    break
            issue_link = f"[#{issue_num}](...)" if issue_num else "unlinked"
            rows.append(f"| {i} | `{f.path}` | {f.line} | {issue_link} |")
        
        return f"""## Batch Fix: {len(self.findings)} {self.rule_pattern} findings

This PR resolves {len(self.findings)} findings of type `{self.rule_pattern}` across {len(files)} files.

### Findings

| # | File | Line | Issue |
|---|------|------|-------|
{chr(10).join(rows)}

### Scope
- Files changed: {len(files)}
- Fix method: autofix

### Verification
- [ ] All target detectors no longer fire
- [ ] No baseline regressions

---
*Generated by qa-agent batch PR engine*
"""
```

### 4.2 BatchStatus

```python
class BatchStatus(str, enum.Enum):
    OPEN = "open"
    FIXING = "fixing"
    FIXING_PARTIAL = "fixing_partial"
    PR_CREATED = "pr_created"
    PR_MERGED = "pr_merged"
    FAILED = "failed"
    SPLIT = "split"
    ABORTED = "aborted"
```

### 4.3 FixResult

```python
@dataclass
class FixResult:
    """Result of fixing a single finding within a batch."""
    finding_id: str
    status: str           # "success" | "failed" | "skipped"
    diff_lines: int = 0
    error: Optional[str] = None
    fix_method: str = "autofix"  # "autofix" | "contextual" | "claude"
```

### 4.4 BatchRule

```python
@dataclass
class BatchRule:
    """Configuration for batching a specific rule or rule pattern."""
    rule_pattern: str              # "ruff-c408" or "ruff-*"
    enabled: bool = True
    group_by: str = "rule"         # "rule" | "file" | "directory"
    max_batch_size: int = 20
    max_files_per_batch: int = 15
    max_loc_per_batch: int = 500
    isolation: dict = field(default_factory=dict)
    priority: int = 99
```

---

## 5. Batch Fix Execution

### 5.1 Main Execution Flow

```python
def process_batch(batch: BatchGroup, repo_path: Path, args, log_file: Path) -> bool:
    """Process a batch: create worktree, apply fixes, create PR.
    
    Returns True if PR was created (even if partial success).
    """
    if batch.is_solo:
        return process_solo_finding(batch.findings[0], batch.issues[0], repo_path, args, log_file)
    
    # Multi-finding batch
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    branch = f"qa/batch-{batch.rule_pattern.replace('ruff-', '')}-{ts}"
    worktree_path = args.worktree_root / f"qa-batch-{batch.batch_id}"
    
    # Create shared worktree
    _create_worktree(repo_path, worktree_path, branch)
    batch.worktree_path = worktree_path
    batch.branch = branch
    batch.status = BatchStatus.FIXING
    
    # Apply all fixes
    for finding in batch.findings:
        issue = _find_issue_for_finding(batch.issues, finding.finding_id)
        result = apply_single_fix_in_batch(
            finding=finding,
            issue=issue,
            worktree_path=worktree_path,
            repo_path=repo_path,
            args=args,
            log_file=log_file,
        )
        batch.fix_results[finding.finding_id] = result
    
    # Tally results
    batch.total_fixes_attempted = len(batch.fix_results)
    batch.total_fixes_succeeded = sum(
        1 for r in batch.fix_results.values() if r.status == "success"
    )
    batch.total_fixes_failed = sum(
        1 for r in batch.fix_results.values() if r.status == "failed"
    )
    
    # Check if too many failures
    failure_rate = batch.total_fixes_failed / max(batch.total_fixes_attempted, 1)
    if failure_rate > 0.5 and batch.retry_count < 2:
        batch.retry_count += 1
        return handle_batch_failure(batch, repo_path, args, log_file)
    
    # Commit successful changes
    if batch.total_fixes_succeeded > 0:
        git_commit_all(worktree_path, batch.pr_title(), log_file)
        git_push_branch(worktree_path, branch, log_file)
        
        # Create PR
        pr = create_batch_pr(batch, args.repo_slug, log_file)
        batch.pr_number = pr['number']
        batch.pr_url = pr['url']
        batch.status = BatchStatus.PR_CREATED
        
        return True
    
    return False
```

### 5.2 Single Fix Within Batch

```python
def apply_single_fix_in_batch(
    finding: Finding,
    issue: dict,
    worktree_path: Path,
    repo_path: Path,
    args,
    log_file: Path,
) -> FixResult:
    """Apply one fix within a shared worktree.
    
    The worktree is shared, so each fix accumulates on top of previous ones.
    This means order matters — we process findings in path order to minimize conflicts.
    """
    # Determine fix method
    llm_rules = _get_llm_fixable_rules()
    
    if finding.safe_to_autofix:
        applied = apply_autofix(worktree_path, finding, log_file)
        if applied:
            # Verify this specific finding is closed
            closed = verify_finding_closed(worktree_path, finding, log_file)
            if closed:
                return FixResult(finding_id=finding.finding_id, status="success", diff_lines=1, fix_method="autofix")
            return FixResult(finding_id=finding.finding_id, status="failed", error="verification-failed", fix_method="autofix")
        else:
            # Try contextual fix
            from .context_fix import apply_contextual_fix
            applied = apply_contextual_fix(repo_path, finding, log_file, worktree_path)
            if applied:
                return FixResult(finding_id=finding.finding_id, status="success", diff_lines=1, fix_method="contextual")
            return FixResult(finding_id=finding.finding_id, status="failed", error="no-fix-applied", fix_method="autofix")
    
    elif finding.rule in llm_rules:
        rc, output, _ = apply_claude_fix(
            worktree_path=worktree_path,
            finding=finding,
            baseline_checks=BASELINE_VALIDATION_CHECKS,
            target_checks=build_target_checks(finding),
            claude_cmd_template=args.claude_cmd_template,
            log_file=log_file,
            repo_path=repo_path,
        )
        if rc == 0:
            return FixResult(finding_id=finding.finding_id, status="success", fix_method="claude")
        return FixResult(finding_id=finding.finding_id, status="failed", error=f"claude rc={rc}", fix_method="claude")
    
    return FixResult(finding_id=finding.finding_id, status="skipped", error="no-fix-method")
```

### 5.3 Verification Within Batch

```python
def verify_finding_closed(worktree_path: Path, finding: Finding, log_file: Path) -> bool:
    """Re-run the specific detector for one finding and check it's resolved.
    
    Uses the per-instance applicability check from the linter JSON output.
    """
    # Re-run the linter for this specific rule+file
    findings = run_linter_for_finding(worktree_path, finding.rule, finding.path)
    
    # Check if our specific finding (same line+snippet) is still present
    for f in findings:
        if (f.path == finding.path and
            f.line == finding.line and
            f.rule == finding.rule):
            return False
    
    return True
```

---

## 6. PR Creation

### 6.1 Batch PR Creation

```python
def create_batch_pr(batch: BatchGroup, repo_slug: str, log_file: Path) -> dict:
    """Create a GitHub PR for the batch.
    
    Uses the standard gh CLI flow but with a batch-aware title and body.
    """
    title = batch.pr_title()
    body = batch.pr_body()
    branch = batch.branch
    
    _append_text(log_file, f'batch-pr: creating PR for {batch.batch_id} title={title}')
    
    rc, output = run_capture(
        ['gh', 'pr', 'create',
         '--repo', repo_slug,
         '--title', title,
         '--body', body,
         '--head', branch,
         '--base', 'main'],
        cwd=batch.worktree_path,
    )
    
    if rc != 0:
        _append_text(log_file, f'batch-pr: gh pr create failed rc={rc} output={output[:300]}')
        raise RuntimeError(f"Failed to create batch PR: {output}")
    
    # Parse PR number and URL from output
    pr_url = output.strip()
    pr_number = int(pr_url.split('/')[-1])
    
    return {'number': pr_number, 'url': pr_url}
```

### 6.2 Issue Linking

After PR creation, all linked issues need to be updated:

```python
def link_issues_to_batch_pr(batch: BatchGroup, pr_number: int, pr_url: str, log_file: Path):
    """Update all issues in the batch to point to the shared PR."""
    for issue in batch.issues:
        issue_github = issue.setdefault('github', {})
        issue_github['pr_number'] = pr_number
        issue_github['pr_url'] = pr_url
        issue_github['batch_id'] = batch.batch_id
        
        # Add comment to each issue
        if issue_github.get('issue_number'):
            gh_issue_comment(
                repo_slug,
                issue_github['issue_number'],
                f"This finding has been batched into PR #{pr_number}: {pr_url}",
                cwd=repo_path,
            )
        
        set_issue_status(issue, 'pr_opened', f'batched in PR #{pr_number}')
```

---

## 7. Split/Recovery

### 7.1 Failure Detection

```python
def should_split_batch(batch: BatchGroup) -> bool:
    """Determine if a batch should be split due to failures.
    
    Split when:
    - More than 50% of fixes failed
    - AND we haven't already split this batch 3+ times
    """
    if batch.total_fixes_failed <= batch.total_fixes_attempted * 0.5:
        return False
    if len(batch.split_history) >= 3:
        return False
    return True
```

### 7.2 Split Implementation

```python
def split_batch(batch: BatchGroup, repo_path: Path, args, log_file: Path) -> list[BatchGroup]:
    """Split a failed batch into smaller sub-batches.
    
    Strategy:
    1. Separate successful and failed findings
    2. Successful findings → create PR immediately
    3. Failed findings → split into smaller batches (halve the size)
    """
    successful = [f for f in batch.findings if batch.fix_results.get(f.finding_id, {}).status == "success"]
    failed = [f for f in batch.findings if batch.fix_results.get(f.finding_id, {}).status == "failed"]
    
    sub_batches = []
    
    # Handle successful findings — commit and PR
    if successful:
        _commit_partial_batch(successful, batch, log_file)
    
    # Handle failed findings — split
    if failed:
        if len(failed) <= 2:
            # Just 1-2 failures → convert to solo
            for f in failed:
                issue = _find_issue_for_finding(batch.issues, f.finding_id)
                sub_batches.append(BatchGroup.from_solo(issue, f))
                _append_text(log_file, f'batch-split: finding {f.finding_id[:8]} → solo')
        else:
            # More failures → halve the batch size
            half = max(len(failed) // 2, 1)
            for i in range(0, len(failed), half):
                chunk = failed[i:i+half]
                issues_map = {f.finding_id: _find_issue_for_finding(batch.issues, f.finding_id) for f in chunk}
                sub_batch = BatchGroup.from_findings(chunk, issues_map, BatchRule(
                    rule_pattern=batch.rule_pattern,
                    max_batch_size=half,
                ))
                sub_batches.append(sub_batch)
                _append_text(log_file, f'batch-split: {len(chunk)} findings → new batch {sub_batch.batch_id}')
    
    # Record split
    batch.split_history.append({
        "split_at": now_iso(),
        "successful_count": len(successful),
        "failed_count": len(failed),
        "sub_batches_created": len(sub_batches),
        "reason": "too_many_fix_failures",
    })
    batch.status = BatchStatus.SPLIT
    
    return sub_batches
```

---

## 8. Configuration

### 8.1 Per-Repo Config (config.yaml)

```yaml
batch_pr:
  enabled: true
  min_batch_size: 2
  max_batch_size: 20
  max_files_per_batch: 15
  max_loc_per_batch: 500
  split_on_failure: true
  max_split_depth: 3
  rules:
    ruff-c408:
      enabled: true
      group_by: "rule"
      max_batch_size: 20
      max_files_per_batch: 15
      isolation:
        file_patterns:
          - "**/migrations/*.py"
    ruff-b904:
      enabled: true
      group_by: "rule"
      max_batch_size: 15
      max_files_per_batch: 10
      isolation:
        file_patterns:
          - "**/middleware*.py"
    ruff-b007:
      enabled: true
      group_by: "rule"
      max_batch_size: 10
    ruff-s311:
      enabled: true
      group_by: "file"
      max_batch_size: 10
      max_files_per_batch: 5
```

### 8.2 CLI Args

```python
parser.add_argument('--batch-pr-enabled', action='store_true', default=True,
                    help='Enable batch PR creation for micro findings')
parser.add_argument('--batch-pr-max-size', type=int, default=20,
                    help='Maximum findings per batch PR')
parser.add_argument('--batch-pr-max-files', type=int, default=15,
                    help='Maximum files per batch PR')
parser.add_argument('--batch-pr-split-on-failure', action='store_true', default=True,
                    help='Split batch on fix failures')
```

---

## 9. State Persistence

### 9.1 Batch Records File

```
state/
├── state.json              # existing (open_issues, open_prs, etc.)
├── issues.json             # existing
├── findings.jsonl          # existing
└── batches.jsonl           # NEW — batch records
```

Each line in `batches.jsonl`:

```json
{"batch_id": "batch-20260421-c408-001", "rule_pattern": "ruff-c408", "group_by": "rule", "status": "pr_created", "created_at": "2026-04-21T18:00:00Z", "findings": [{"finding_id": "abc123", "path": "zerver/lib/message.py", "line": 42, "rule": "ruff-c408", "issue_id": "ISS-001", "fix_status": "success"}], "pr_number": 42, "pr_url": "https://github.com/.../pull/42", "total_fixes_attempted": 5, "total_fixes_succeeded": 5, "total_fixes_failed": 0, "retry_count": 0, "split_history": []}
```

### 9.2 State Functions

```python
def load_batches(path: Path) -> list[dict]:
    if not path.exists():
        return []
    batches = []
    with path.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                batches.append(json.loads(raw))
    return batches

def save_batch_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, sort_keys=True) + '\n')

def update_batch_record(path: Path, batch_id: str, updates: dict) -> None:
    """Rewrite the batches file with updated record."""
    records = load_batches(path)
    for i, record in enumerate(records):
        if record.get('batch_id') == batch_id:
            records[i].update(updates)
            break
    path.write_text('\n'.join(json.dumps(r, sort_keys=True) for r in records) + '\n')
```

---

## 10. Integration with Existing Cycles

### 10.1 Pr-Cycle Changes

```python
# cli.py — run_pr_cycle section

# OLD (simplified):
for issue, finding in queue_candidates:
    process_single_finding(issue, finding, ...)

# NEW:
batch_groups = group_findings_for_batch(
    queue_candidates,
    load_batch_rules(args),
)

for batch in batch_groups:
    if batch.is_solo:
        # Existing single-finding path — no changes needed
        process_single_finding(batch.issues[0], batch.findings[0], ...)
    else:
        # New batch path
        process_batch(batch, repo_path, args, log_file)
```

### 10.2 Issue-Cycle (Unchanged)

The issue cycle remains unchanged. Issues are still created 1:1 per finding.
The batching happens at PR creation time, not issue creation time.

This means:
- Each finding still has its own GitHub issue (for tracking)
- Multiple issues can be linked to a single PR (for fixing)
- Issue status is updated when the batch PR is created/merged

### 10.3 Merge-Cycle (Minimal Changes)

Merge cycle needs to know about batches:

```python
# When merging a batch PR:
# 1. Check if all linked issues should be marked resolved
# 2. Update all issue statuses to 'resolved_merged'
# 3. Record merge in batch state

def on_batch_pr_merged(batch: BatchGroup, log_file: Path):
    for issue in batch.issues:
        set_issue_status(issue, 'resolved_merged', f'merged in batch PR #{batch.pr_number}')
    batch.status = BatchStatus.PR_MERGED
    save_batch_record(batch)
```

---

## 11. Edge Cases

### 11.1 Fix Conflicts Within Batch

Two findings in the same batch might touch overlapping code:

```python
def check_batch_conflicts(findings: list[Finding]) -> list[tuple]:
    """Check for potential conflicts within a batch.
    
    Two findings conflict if:
    - They're in the same file AND
    - Their line numbers are within 5 lines of each other
    """
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
```

If conflicts are found, split the batch at the conflict boundary.

### 11.2 Batch Interrupted Mid-Execution

If the process dies mid-batch:

```python
def recover_interrupted_batch(batch_id: str, batches_file: Path) -> Optional[BatchGroup]:
    """Check if a batch was interrupted and needs recovery.
    
    Recovery:
    - If worktree exists and branch was pushed → check PR status
    - If worktree exists but nothing pushed → abort and retry
    - If no worktree → treat as new
    """
    batch = load_batch_by_id(batch_id, batches_file)
    if batch is None:
        return None
    
    if batch.status == BatchStatus.FIXING:
        # Was mid-fix when interrupted
        if batch.worktree_path and batch.worktree_path.exists():
            # Worktree exists — check if branch was pushed
            if batch.branch and branch_exists_on_remote(batch.branch):
                batch.status = BatchStatus.PR_CREATED
            else:
                batch.status = BatchStatus.ABORTED
        else:
            batch.status = BatchStatus.ABORTED
        
        save_batch_record(batch)
    
    return batch
```

### 11.3 Batch Exceeds Scope Limits

If the cumulative diff of a batch exceeds `max_files_changed` or `max_loc_diff`:

```python
def check_batch_scope(batch: BatchGroup) -> bool:
    """Check if the batch's estimated scope exceeds safety limits.
    
    If it does, split the batch into smaller chunks.
    """
    if batch.file_count > batch.max_files:
        return False
    if len(batch.findings) > batch.max_batch_size:
        return False
    return True
```

---

## 12. Testing Strategy

### 12.1 Unit Tests

| Test File | Coverage |
|-----------|----------|
| `test_batch_grouping.py` | Grouping rules, isolation, chunking |
| `test_batch_execution.py` | Shared worktree, fix application, verification |
| `test_batch_pr.py` | PR title/body generation, issue linking |
| `test_batch_split.py` | Split logic, partial success, recursive split |
| `test_batch_conflicts.py` | Conflict detection, split-on-conflict |

### 12.2 Integration Tests

| Test | Scenario |
|------|----------|
| `test_batch_e2e_zulip` | Batch 10 `ruff-c408` findings in zulip repo |
| `test_batch_partial_success` | 8 succeed, 2 fail → split and retry |
| `test_batch_isolation` | Migrations findings stay solo |
| `test_batch_solo_fallback` | Single finding uses existing path |

### 12.3 Validation Criteria

- [ ] Grouping produces correct batch assignments for known findings
- [ ] Batch PR title and body are correct
- [ ] All issues are linked to the batch PR
- [ ] Partial success creates PR for successes, retries failures
- [ ] Isolated findings are never batched
- [ ] Solo fallback works identically to existing single-finding path
- [ ] No regression in existing pr-cycle behavior when `batch_pr.enabled: false`

---

## 13. Rollout Plan

### Phase 1: Grouping Engine (Week 1)
- [ ] Add `BatchGroup`, `BatchStatus`, `FixResult` to `models.py`
- [ ] Create `batch_rules.yaml` with rules for zulip's top 4 ruff rules
- [ ] Implement `group_findings_for_batch()` in new `batch_pr.py`
- [ ] Add batch state persistence to `state.py`
- [ ] Tests: grouping correctness

### Phase 2: Batch Execution (Week 2)
- [ ] Implement `process_batch()` — shared worktree, sequential fixes
- [ ] Implement `create_batch_pr()` — PR with finding table
- [ ] Wire into `cli.py` pr-cycle
- [ ] Config flag: `batch_pr.enabled`
- [ ] Tests: batch execution on zulip

### Phase 3: Split/Recovery (Week 3)
- [ ] Implement `handle_batch_failure()` and `split_batch()`
- [ ] Implement conflict detection
- [ ] Implement interrupted batch recovery
- [ ] Tests: split scenarios, edge cases

### Phase 4: Cross-Rule + Optimization (Future)
- [ ] Cross-rule batching ("all ruff style fixes")
- [ ] Time-window collection
- [ ] Rollup PR templates
- [ ] Metrics dashboard
