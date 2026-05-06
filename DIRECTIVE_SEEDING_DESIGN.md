# Directive Seeding Design — QA Agent

## Summary

The QA agent currently has no feedback loop. Findings are stored in `state/qa_findings.jsonl`, cycle-outcome lessons are appended to `LESSONS_LOG.md`, but **none of this data is ever read back** to inform future fix attempts. The agent keeps failing at the same findings without knowing it has tried them before.

This design adds three incremental improvements that close the loop:

1. **Read LESSONS_LOG.md** at prompt time — inject prior-context from past cycles into the fix prompt.
2. **Per-finding failure memory in findings.jsonl** — track fix attempts, last error, and success/failure persistently.
3. **Exponential backoff cooldown** — instead of a flat 4-hour cooldown, failed findings get progressively longer cooldowns, and successful findings reset quickly.

All three are backward-compatible and can be implemented in order.

---

## Improvement 1: Make LESSONS_LOG Readable (Feedback Loop)

### Current State

`utils.py` has `append_lesson()` that writes markdown entries to `LESSONS_LOG.md`:

```markdown
## 2026-03-24 | pr-cycle
- **Broke:** 1 fixes failed verification; 1 blocked events
- **Changed:** 1 fixes verified; 1 PRs created
```

**Critical gap**: `append_lesson()` has **no `finding_id` field**. Entries cannot be attributed to specific findings. The design below fixes this by adding `finding_id` to the signature and the log format.

Nothing reads LESSONS_LOG back. The write-only log accumulates indefinitely.

### Design

#### `utils.py` — modify `append_lesson()` + add two read functions

**Modify `append_lesson()` — add `finding_id` parameter:**

```python
def append_lesson(
    lessons_file: Path,
    cycle_type: str,
    finding_id: str = "",          # NEW: attribute entry to a finding
    what_broke: str = "",
    what_changed: str = "",
    what_worked: str = "",
) -> None:
```

The log format becomes:
```markdown
## 2026-03-24 | fix-cycle
finding_id: abc123def...
- **Worked:** fix succeeded rule=ruff-c408
```

If `finding_id` is `""` (empty, default), the `finding_id:` line is omitted. This preserves backward compat with existing entries (they just won't be findable by finding_id — acceptable).

**Add `load_lessons_for_finding()`:**

```python
def load_lessons_for_finding(finding_id: str, lessons_file: Path) -> List[Dict[str, Any]]:
    """Parse lessons_file for entries tagged with this finding_id. Returns newest-first."""
    # Parse: when line starts with "finding_id: {fid}", open a new entry
    # When line starts with "## ", flush previous entry
    # Include entry if its finding_id matches
    # Return list of dicts: {date, cycle_type, finding_id, broke, changed, worked}
```

**Add `load_recent_lessons()`:**

```python
def load_recent_lessons(lessons_file: Path, limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent `limit` lesson entries, newest-first."""
    # Parse bottom-up from the file, collect until limit reached
```

#### `prompts.py` — modify `render_claude_fix_prompt()`

Add an optional `fix_history: List[Dict[str, Any]] = None` parameter. When non-empty, inject a `## Prior context` section:

```
## Prior context
- This finding was attempted {n} time(s) previously.
- Last attempt ({date}): fix {status} — {summary}
- Recommendation: {recommendation}
```

If `fix_history` is empty or None, the section is omitted entirely.

#### `lifecycle.py` — wire the call

In `apply_claude_fix()`, load history before calling `render_claude_fix_prompt()`:

```python
from .utils import load_lessons_for_finding, DEFAULT_LESSONS_LOG

def apply_claude_fix(
    ...
    findings_file: Path,       # NEW
    lessons_file: Path,        # NEW — passed in from orchestrator
):
    ...
    fix_history = load_lessons_for_finding(finding.finding_id, lessons_file)
    prompt_text = render_claude_fix_prompt(
        ...
        fix_history=fix_history,
    )
    ...

    # After fix attempt, log per-finding lesson
    if rc == 0:
        append_lesson(
            lessons_file=lessons_file,
            cycle_type='fix',
            finding_id=finding.finding_id,
            what_changed=f"fix succeeded rule={finding.rule}",
        )
    else:
        append_lesson(
            lessons_file=lessons_file,
            cycle_type='fix',
            finding_id=finding.finding_id,
            what_broke=f"fix failed rc={rc}",
        )
```

**Note**: `cli.py` already calls `append_lesson()` after active cycles (orchestrator-level logging). The lifecycle-level per-finding logging above supplements — not replaces — that. Orchestrator logs a cycle summary, lifecycle logs per-finding outcomes.

### Backward Compatibility

- Existing `LESSONS_LOG.md` entries without `finding_id:` lines are silently skipped by `load_lessons_for_finding` — no false positives, no crashes.
- `append_lesson()` gains an optional param with default `""`; all existing call sites continue to work.
- `render_claude_fix_prompt()` gains an optional param with default `None`; all existing call sites continue to work.

### Risks & Edge Cases

- If `LESSONS_LOG.md` grows large (years of entries), parsing it on every `apply_claude_fix` call could be slow. Mitigation: `load_lessons_for_finding` reads the file and scans; for very large files a tail-based approach (last N KB) could be used, but this is premature optimization.
- Entries without `finding_id` embedded cannot be attributed to a specific finding. `load_lessons_for_finding` returns `[]` rather than guessing. This is correct behavior.

---

## Improvement 2: Per-Finding Failure Memory in findings.jsonl

### Current State

`append_findings()` in `state.py` writes `Finding` objects to `state/<repo>/findings.jsonl` (one per repo) as one JSON object per line. The `Finding` dataclass has 8 fields. There is no mechanism to record what happened after a fix was attempted.

### Design

#### Extend `Finding` dataclass in `models.py`

```python
@dataclass
class Finding:
    finding_id: str
    repo: str
    path: str
    line: int
    rule: str
    snippet: str
    confidence: float
    quick_win: bool
    safe_to_autofix: bool
    # New fields — all Optional with defaults for backward compat
    fix_attempts: int = 0
    last_fix_error: Optional[str] = None
    last_fix_at: Optional[str] = None
    fix_success: bool = False

    def as_dict(self) -> Dict[str, Any]:
        d = {
            'finding_id': self.finding_id,
            'repo': self.repo,
            'path': self.path,
            'line': self.line,
            'rule': self.rule,
            'snippet': self.snippet,
            'confidence': self.confidence,
            'quick_win': self.quick_win,
            'safe_to_autofix': self.safe_to_autofix,
        }
        # Only serialize new fields if non-default (saves space in old records)
        if self.fix_attempts > 0:
            d['fix_attempts'] = self.fix_attempts
        if self.last_fix_error is not None:
            d['last_fix_error'] = self.last_fix_error
        if self.last_fix_at is not None:
            d['last_fix_at'] = self.last_fix_at
        if self.fix_success:
            d['fix_success'] = self.fix_success
        return d
```

**Backward compatibility note**: `as_dict()` only writes the new fields when they have non-default values. Old JSONL records without these fields will deserialize correctly because the dataclass defaults handle missing keys.

**Deserialization** via `from_dict()` classmethod — handles both old records (missing fields get defaults) and new records (fields preserved):

```python
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Finding:
        return cls(
            finding_id=d['finding_id'],
            repo=d['repo'],
            path=d['path'],
            line=d['line'],
            rule=d['rule'],
            snippet=d['snippet'],
            confidence=d['confidence'],
            quick_win=d['quick_win'],
            safe_to_autofix=d['safe_to_autofix'],
            fix_attempts=d.get('fix_attempts', 0),
            last_fix_error=d.get('last_fix_error'),
            last_fix_at=d.get('last_fix_at'),
            fix_success=d.get('fix_success', False),
        )
```

**Key**: `increment_fix_attempt` must handle the case where a finding was just written by `append_findings()` but `as_dict()` didn't include `fix_attempts: 0` (because it's the default). `record.get('fix_attempts', 0)` returns 0 for old records and for new records, then increments to 1 — correct behavior.

#### New functions in `state.py`

```python
def load_finding_record(finding_id: str, findings_file: Path) -> Optional[Dict[str, Any]]:
    """Load a single finding record by finding_id from a JSONL file.
    
    Returns the dict representation of the finding, or None if not found.
    Does NOT reconstruct a Finding object — returns raw dict for efficiency.
    """
    if not findings_file.exists():
        return None
    with findings_file.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if obj.get('finding_id') == finding_id:
                    return obj
            except Exception:
                continue
    return None


def update_finding_record(finding_id: str, findings_file: Path, updates: Dict[str, Any]) -> bool:
    """Patch a finding record's extra fields in-place in a JSONL file.
    
    Reads all records, replaces the matching one, rewrites the file.
    Returns True if found and updated, False if not found.
    """
    if not findings_file.exists():
        return False
    records: List[Dict[str, Any]] = []
    found = False
    with findings_file.open('r', encoding='utf-8') as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if obj.get('finding_id') == finding_id:
                    obj.update(updates)
                    found = True
                records.append(obj)
            except Exception:
                records.append({'raw': raw})  # preserve malformed lines
    
    if not found:
        return False
    
    findings_file.parent.mkdir(parents=True, exist_ok=True)
    with findings_file.open('w', encoding='utf-8') as f:
        for obj in records:
            if 'raw' in obj:
                f.write(obj['raw'] + '\n')
            else:
                f.write(json.dumps(obj, sort_keys=True) + '\n')
    return True


def increment_fix_attempt(finding_id: str, findings_file: Path, error: Optional[str]) -> None:
    """Increment fix_attempts, set last_fix_at, last_fix_error on a finding record.
    
    Called after every fix attempt (both success and failure) in apply_claude_fix.
    On success, caller also sets fix_success=True via a separate update.
    """
    updates: Dict[str, Any] = {
        'fix_attempts': 0,  # will be incremented below
        'last_fix_at': now_iso(),
    }
    if error:
        updates['last_fix_error'] = error[:500] if len(error) > 500 else error
    
    # First load current count
    record = load_finding_record(finding_id, findings_file)
    current_attempts = record.get('fix_attempts', 0) if record else 0
    updates['fix_attempts'] = current_attempts + 1
    
    update_finding_record(finding_id, findings_file, updates)
```

#### Wire into `lifecycle.py` — `apply_claude_fix()`

```python
def apply_claude_fix(
    worktree_path: Path,
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    claude_cmd_template: str,
    max_files_changed: int,
    max_loc_diff: int,
    log_file: Path,
    findings_file: Path,          # NEW
    lessons_file: Path,            # NEW
) -> Tuple[int, str, str]:
    ...
    rc, output, prompt_path_str = run_claude_subprocess(...)

    # Record the attempt BEFORE returning
    error_msg: Optional[str] = None
    if rc != 0:
        error_msg = f"claude rc={rc} output={output[:300]}"
    
    increment_fix_attempt(finding.finding_id, findings_file, error_msg)
    
    # If fix succeeded (rc == 0), also update fix_success
    if rc == 0:
        update_finding_record(finding.finding_id, findings_file, {'fix_success': True})
        # Also record a positive lesson
        append_lesson(
            lessons_file=lessons_file,
            cycle_type='fix-cycle',
            what_changed=f"fix succeeded for {finding.finding_id} rule={finding.rule}",
        )
    ...
```

#### Wire into `prompts.py` — `render_claude_fix_prompt()`

Add an optional parameter `finding_record: Optional[Dict[str, Any]] = None`.

If provided and `fix_attempts > 0`, inject a `## Fix history` section just before `## Snippet`:

```
## Fix history
- Attempts: {fix_attempts}
- Last error: {last_fix_error}
- This is a known-difficult finding. Consider a more conservative approach.
```

If `fix_attempts == 0` or the record is None, omit the section.

### Backward Compatibility

- Existing `qa_findings.jsonl` records without the new fields deserialize correctly via `Finding.from_dict()`.
- `as_dict()` only writes new fields when non-default, so old records on disk remain valid.
- All new functions handle missing files gracefully (`load_finding_record` returns `None`, `update_finding_record` returns `False`).

### Risks & Edge Cases

- `update_finding_record` rewrites the entire JSONL file on every fix attempt. For files with thousands of findings this is acceptable (O(n) write). If performance becomes an issue, a sharded file approach could be considered later.
- `increment_fix_attempt` does a read-then-write (two file ops). This is acceptable given fix attempts are infrequent.
- The `error_msg` truncation at 500 chars prevents unbounded field growth.

---

## Improvement 3: Exponential Backoff Cooldown

### Current State

`filter_findings_by_cooldown()` in `state.py` uses a single static `cooldown_seconds` threshold:

```python
elapsed = age_seconds(last_action_at, reference=now)
if elapsed is not None and elapsed < cooldown_seconds:
    # suppressed
```

The `mark_finding_activity()` entry stores `{'last_action': action, 'last_action_at': ts}` — no failure context.

### Design

#### Extend `mark_finding_activity()` — add failure tracking

```python
def mark_finding_activity(
    state: Dict[str, Any],
    finding_ids: List[str],
    action: str,
    failure_count: Optional[int] = None,
    last_error: Optional[str] = None,
) -> None:
    activity = state.setdefault('finding_activity', {})
    ts = now_iso()
    for finding_id in finding_ids:
        entry = activity.setdefault(finding_id, {
            'last_action': action,
            'last_action_at': ts,
        })
        entry['last_action'] = action
        entry['last_action_at'] = ts
        if failure_count is not None:
            entry['failure_count'] = failure_count
        if last_error is not None:
            entry['last_error'] = last_error
```

#### New function in `state.py`

```python
MAX_COOLDOWN_SECONDS = 7 * 24 * 60 * 60  # 7 days

def get_effective_cooldown(
    finding_id: str,
    state: Dict[str, Any],
    base_cooldown_seconds: int,
) -> int:
    """
    Returns effective cooldown for a finding.
    
    - On success (failure_count is 0 or not present): base_cooldown_seconds
    - On failure: exponential backoff = base_cooldown_seconds * (2 ** failure_count)
    - Capped at MAX_COOLDOWN_SECONDS (7 days)
    
    State is read but NOT mutated by this function.
    """
    activity = state.get('finding_activity', {})
    entry = activity.get(finding_id, {})
    failure_count = entry.get('failure_count', 0)
    
    if failure_count == 0:
        return base_cooldown_seconds
    
    effective = base_cooldown_seconds * (2 ** failure_count)
    return min(effective, MAX_COOLDOWN_SECONDS)
```

#### Modify `filter_findings_by_cooldown()` in `state.py`

```python
def filter_findings_by_cooldown(
    findings: List[Finding],
    state: Dict[str, Any],
    cooldown_seconds: int,
    log_file: Path,
) -> tuple[List[Finding], List[Finding]]:
    allowed: List[Finding] = []
    suppressed: List[Finding] = []
    activity = state.setdefault('finding_activity', {})
    now = datetime.now(timezone.utc)

    for finding in findings:
        entry = activity.get(finding.finding_id, {})
        last_action_at = entry.get('last_action_at')
        effective_cooldown = get_effective_cooldown(finding.finding_id, state, cooldown_seconds)
        elapsed = age_seconds(last_action_at, reference=now)
        
        if elapsed is not None and elapsed < effective_cooldown:
            remaining = effective_cooldown - elapsed
            failure_count = entry.get('failure_count', 0)
            _append_text(
                log_file,
                'cooldown-suppress: '
                f'finding_id={finding.finding_id} '
                f'last_action={entry.get("last_action", "unknown")} '
                f'last_action_at={last_action_at} '
                f'effective_cooldown={effective_cooldown} '
                f'failure_count={failure_count} '
                f'remaining_seconds={remaining}',
            )
            suppressed.append(finding)
            continue
        allowed.append(finding)

    return allowed, suppressed
```

#### Update `mark_finding_activity` call sites in `lifecycle.py`

After a failed fix attempt (validation fails), increment the failure count:

```python
# In the lifecycle, after a fix failure:
current_activity = state['finding_activity'].get(finding.finding_id, {})
current_failure_count = current_activity.get('failure_count', 0)
mark_finding_activity(
    state,
    [finding.finding_id],
    action='fix-attempt',
    failure_count=current_failure_count + 1,
    last_error=error_summary,
)
```

After a successful fix:

```python
mark_finding_activity(
    state,
    [finding.finding_id],
    action='fix-succeeded',
    failure_count=0,  # reset on success
    last_error=None,
)
```

### Backward Compatibility

- Existing `finding_activity` entries without `failure_count` are treated as `0` — same as current behavior (flat cooldown).
- `mark_finding_activity` now accepts optional kwargs; all existing call sites continue to work.
- `filter_findings_by_cooldown` is a pure read of `state` — no migration needed.

### Risks & Edge Cases

- **Saturation at 7 days**: After ~7 failures, the effective cooldown caps at 7 days. This is intentional to prevent indefinite suppression.
- **State persistence**: `mark_finding_activity` mutates the in-memory `state` dict. The caller is responsible for eventually calling `save_state()` to persist. Currently the orchestrator calls `save_state` after `mark_finding_activity` — this contract must be maintained.
- **Race condition**: If two fix cycles run concurrently for the same finding, the failure_count could be inconsistent. This is acceptable for the QA agent's current single-threaded design.

---

## Combined: How All Three Work Together

The full flow, from finding discovery through to the next cycle's prompt:

```
Finding discovered
  → append_findings() writes to qa_findings.jsonl
  → mark_finding_activity(state, [finding_id], 'found')

Fix cycle begins
  → filter_findings_by_cooldown(findings, state, cooldown_seconds)
      → get_effective_cooldown(finding_id, state, base_cooldown)
          • First time: returns base_cooldown (4 hours)
          • After N failures: returns min(4h * 2^N, 7 days)
      → Finding suppressed if elapsed < effective_cooldown
      → Log includes effective_cooldown and failure_count

Finding allowed → apply_claude_fix() or apply_autofix() called

Fix succeeds (rc == 0, validation passes)
  → increment_fix_attempt(finding_id, findings_file, error=None)
      → fix_attempts += 1, last_fix_at = now, last_fix_error = None
  → update_finding_record(finding_id, findings_file, {'fix_success': True})
      → fix_success = True persisted to qa_findings.jsonl
  → mark_finding_activity(state, [finding_id], 'fix-succeeded', failure_count=0)
      → failure_count resets to 0 → effective_cooldown back to base
  → append_lesson(lessons_file, 'fix-cycle', what_changed=f"fix succeeded for {finding_id}")

Fix fails (rc != 0 or validation fails)
  → increment_fix_attempt(finding_id, findings_file, error=error_msg)
      → fix_attempts += 1, last_fix_at = now, last_fix_error = error_msg
  → mark_finding_activity(state, [finding_id], 'fix-attempt',
        failure_count=prev_failure_count + 1, last_error=error_summary)
      → next effective_cooldown = base * 2^(failure_count+1)
  → append_lesson(lessons_file, 'fix-cycle', what_broke=f"fix failed: {error_summary}")

Next cycle:
  → render_claude_fix_prompt() called with:
      fix_history = load_lessons_for_finding(finding.finding_id, lessons_file)
      finding_record = load_finding_record(finding.finding_id, findings_file)
  → Prompt gets injected sections if data exists:
      ## Prior context (from LESSONS_LOG.md)
          - This finding was attempted 2 times previously.
          - Last attempt (2026-03-24): fix failed — ruff check returned non-zero.
          - Recommendation: this file may need manual review before auto-fixing.
      ## Fix history (from qa_findings.jsonl)
          - Attempts: 3
          - Last error: ruff check failed with exit code 1
          - This is a known-difficult finding. Consider a more conservative approach.
  → Agent sees prior failures → can choose different strategy or escalate
```

---

## Testing Strategy

### Improvement 1 Tests

**Unit tests for `load_lessons_for_finding`**:
1. Empty file → returns `[]`
2. File with no matching finding_id → returns `[]`
3. File with one matching entry → returns one entry with correct fields
4. File with multiple matching entries → returns newest-first
5. File with malformed lines → skips malformed, returns valid entries only

**Unit tests for `load_recent_lessons`**:
1. File with fewer than `limit` entries → returns all
2. File with more than `limit` entries → returns exactly `limit` newest
3. `limit=0` → returns `[]`

**Integration: prompt contains Prior context**:
1. `fix_history=[]` → prompt has no `## Prior context` section
2. `fix_history=[{...}]` → prompt contains the section with correct values
3. `fix_history=None` → prompt has no section (default behavior)

### Improvement 2 Tests

**Unit tests for `Finding.from_dict` / `as_dict` roundtrip**:
1. Old record (no new fields) → `from_dict` produces defaults → `as_dict` omits new fields → matches original
2. New record with all fields → roundtrip preserves all values
3. Partial record (only `fix_attempts`) → other fields get defaults

**Unit tests for `load_finding_record`**:
1. Finding exists → returns correct dict
2. Finding does not exist → returns `None`
3. Malformed JSONL line → skipped, continues searching

**Unit tests for `update_finding_record`**:
1. Finding exists → updates fields, returns `True`, file reflects change
2. Finding does not exist → returns `False`, file unchanged
3. Malformed line in file → preserved as raw on rewrite

**Integration: `increment_fix_attempt` called correctly**:
1. First attempt → `fix_attempts=1, last_fix_at=now, last_fix_error=None`
2. Second attempt with error → `fix_attempts=2, last_fix_error=error`
3. Call `update_finding_record` with `fix_success=True` → persists correctly

### Improvement 3 Tests

**Unit tests for `get_effective_cooldown`**:
1. No activity entry → returns `base_cooldown_seconds`
2. `failure_count=0` → returns `base_cooldown_seconds`
3. `failure_count=1` → returns `base_cooldown_seconds * 2`
4. `failure_count=10` → returns `min(base * 2^10, MAX_COOLDOWN_SECONDS)` = 7 days cap
5. `failure_count=3` → returns `base * 8`

**Unit tests for `filter_findings_by_cooldown` with exponential backoff**:
1. Finding with 0 failures, elapsed=3h, base=4h → allowed (3h < 4h)
2. Finding with 1 failure, elapsed=5h, base=4h → suppressed (5h < 8h)
3. Finding with 2 failures, elapsed=10h, base=4h → suppressed (10h < 16h)
4. Finding with 3 failures, elapsed=35h, base=4h → allowed (35h > 32h)

**Log message includes new fields**:
1. On suppression, log message contains `effective_cooldown=N` and `failure_count=M`

**`mark_finding_activity` failure_count update**:
1. Call with `failure_count=2` → entry has `failure_count=2`
2. Call with `failure_count=0` → entry has `failure_count=0` (reset)

### Cross-cutting Integration Tests

1. Full cycle: fix fails → failure_count increments → next cycle sees extended cooldown → log confirms
2. Full cycle: fix succeeds → failure_count resets to 0 → next cycle uses base cooldown
3. Findings file survives multiple restart cycles (state reloaded from disk)
