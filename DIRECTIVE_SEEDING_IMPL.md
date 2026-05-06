# Implementation Architecture — Directive Seeding

## Overview

This document describes **HOW** to implement the three directive-seeding improvements defined in `DIRECTIVE_SEEDING_DESIGN.md`. The implementation is divided into five ordered phases. Phases 1–3 are self-contained data-layer changes. Phase 4 connects the data layer to the LLM prompt. Phase 5 is a smoke test.

Reference documents:
- **Design**: `/home/vikas/.openclaw/workspace/qa-agent/DIRECTIVE_SEEDING_DESIGN.md`
- **Source of truth for Finding fields**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/models.py` (9 existing fields)

## Key Codebase Facts Confirmed by Reading Sources

| Fact | Location |
|---|---|
| `Finding` has 9 fields (no `fix_attempts` etc. yet) | `models.py` |
| `append_lesson()` has no `finding_id` param | `utils.py` |
| `render_claude_fix_prompt()` has no history params | `prompts.py` |
| `apply_claude_fix()` takes 9 params, no findings/lessons files | `lifecycle.py ~432` |
| `apply_autofix()` does NOT write findings.jsonl | `lifecycle.py ~64` |
| `run_validation_gate()` returns `(bool, str)` | `lifecycle.py ~491` |
| `cli.py` calls `apply_claude_fix` at `~674` | `cli.py` |
| `lessons_file` is a `Path` in `cli.py` main() | `cli.py` |
| `filter_findings_by_cooldown()` uses flat cooldown | `state.py` |
| `mark_finding_activity()` stores `{last_action, last_action_at}` | `state.py` |
| `DEFAULT_FINDING_COOLDOWN_SECONDS = 4*60*60` | `constants.py` |

---

## Phase 1: Findings Failure Memory (Self-Contained, No Cross-Cutting)

This phase extends the `Finding` dataclass and adds `state.py` persistence functions. It has **no dependencies on prompts or lifecycle**.

### Step 1.1: Extend `Finding` dataclass (`models.py`)

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/models.py`

**Before (existing)**:
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

    def as_dict(self) -> Dict[str, Any]:
        return {
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
```

**After (replace the entire `Finding` class and add `from_dict`)**:

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
    # --- NEW FIELDS (all Optional with defaults for backward compat) ---
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
        # Only serialize new fields when non-default — preserves old records on disk
        if self.fix_attempts > 0:
            d['fix_attempts'] = self.fix_attempts
        if self.last_fix_error is not None:
            d['last_fix_error'] = self.last_fix_error
        if self.last_fix_at is not None:
            d['last_fix_at'] = self.last_fix_at
        if self.fix_success:
            d['fix_success'] = self.fix_success
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Finding:
        """Deserialize from a dict (e.g. from JSONL). Handles both old records
        (missing new fields → defaults) and new records (fields preserved)."""
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

**Verify**:
```bash
cd /home/vikas/.openclaw/workspace/qa-agent
python3 -c "
from core.sandbox_local_runner.models import Finding
# Test new fields default
f = Finding('a','r','p',1,'rule','s',0.9,True,True)
assert f.fix_attempts == 0
assert f.last_fix_error is None
assert f.fix_success == False
# Test as_dict omits defaults
d = f.as_dict()
assert 'fix_attempts' not in d
assert 'last_fix_error' not in d
assert 'fix_success' not in d
# Test as_dict includes non-defaults
f2 = Finding('a','r','p',1,'rule','s',0.9,True,True, fix_attempts=3, fix_success=True)
d2 = f2.as_dict()
assert d2['fix_attempts'] == 3
assert d2['fix_success'] == True
# Test from_dict roundtrip (new record)
d3 = f2.as_dict()
f3 = Finding.from_dict(d3)
assert f3.fix_attempts == 3
assert f3.fix_success == True
# Test from_dict with old record (no new fields)
old = {'finding_id':'x','repo':'r','path':'p','line':1,'rule':'r','snippet':'s','confidence':0.9,'quick_win':True,'safe_to_autofix':True}
f4 = Finding.from_dict(old)
assert f4.fix_attempts == 0
assert f4.last_fix_error is None
assert f4.fix_success == False
print('OK')
"
```

### Step 1.2: Add `state.py` functions

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/state.py`

Add these three functions **after** the existing `filter_findings_by_cooldown()` function and **before** the module's other functions (or group them near `append_findings` for logical coherence). Place them after `append_findings` — they operate on the same file.

```python
def load_finding_record(finding_id: str, findings_file: Path) -> Optional[Dict[str, Any]]:
    """Load a single finding record by finding_id from a JSONL file.

    Returns the dict representation of the finding, or None if not found.
    Does NOT reconstruct a Finding object — returns raw dict for efficiency.
    Handles malformed lines gracefully.
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
    Malformed lines are preserved as raw strings on rewrite.
    """
    if not findings_file.exists():
        return False

    records: List[Any] = []   # List[Dict] or str (raw malformed lines)
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
                # Malformed line — preserve as raw string
                records.append(raw)

    if not found:
        return False

    findings_file.parent.mkdir(parents=True, exist_ok=True)
    with findings_file.open('w', encoding='utf-8') as f:
        for item in records:
            if isinstance(item, str):
                f.write(item + '\n')
            else:
                f.write(json.dumps(item, sort_keys=True) + '\n')
    return True


def increment_fix_attempt(
    finding_id: str,
    findings_file: Path,
    error: Optional[str],
) -> None:
    """Increment fix_attempts, set last_fix_at, last_fix_error on a finding record.

    Called after every fix attempt (both success and failure) in apply_claude_fix.
    On success, caller also calls update_finding_record with fix_success=True.
    Handles missing files, missing records, and missing fields gracefully.
    """
    record = load_finding_record(finding_id, findings_file)
    current_attempts = record.get('fix_attempts', 0) if record else 0

    updates: Dict[str, Any] = {
        'fix_attempts': current_attempts + 1,
        'last_fix_at': now_iso(),
    }
    if error is not None:
        # Truncate long errors to prevent unbounded field growth
        updates['last_fix_error'] = error[:500] if len(error) > 500 else error

    update_finding_record(finding_id, findings_file, updates)
```

**Note**: These functions require `json` import — confirm `from __future__ import annotations` and `import json` are already at the top of `state.py`. They are.

### Step 1.3: Update `__init__.py` re-exports

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/__init__.py`

Add `from_dict` to the existing `models` re-exports block (currently imports `Finding, now_iso, parse_iso, age_seconds, stable_finding_id`):

```python
from .models import Finding, now_iso, parse_iso, age_seconds, stable_finding_id
# Finding.from_dict is a classmethod — accessible via Finding.from_dict directly;
# it does NOT need to be separately re-exported.
```

Also add the three new `state.py` functions to the existing state re-exports block (currently imports `load_state, save_state, load_findings_seen, append_findings, ...`):

```python
from .state import (
    load_state, save_state,
    load_findings_seen, append_findings,
    load_finding_record,          # NEW
    update_finding_record,         # NEW
    increment_fix_attempt,         # NEW
    load_issues, save_issues,
    guard_open_issues, guard_open_prs,
    record_reconciliation_event, reconcile_open_workload,
    mark_finding_activity, filter_findings_by_cooldown,
)
```

### Step 1.4: Write unit tests

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/test_directive_seeding.py`

Create this new test file. It can run independently alongside `test_refactor.py`.

```python
#!/usr/bin/env python3
"""test_directive_seeding.py — Tests for directive-seeding phases 1–3."""

import json
import tempfile
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.sandbox_local_runner.models import Finding
from core.sandbox_local_runner.state import (
    load_finding_record,
    update_finding_record,
    increment_fix_attempt,
)


# ─── Finding dataclass tests ───────────────────────────────────────────────

def _make_finding(fix_attempts=0, last_fix_error=None, last_fix_at=None, fix_success=False):
    return Finding(
        finding_id='test-id-001',
        repo='test-repo',
        path='src/test.py',
        line=10,
        rule='test-rule',
        snippet='x = 1',
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
        fix_attempts=fix_attempts,
        last_fix_error=last_fix_error,
        last_fix_at=last_fix_at,
        fix_success=fix_success,
    )


def test_finding_from_dict_roundtrip_new_record():
    """Roundtrip a Finding with all new fields set."""
    original = _make_finding(fix_attempts=3, last_fix_error='ruff rc=1', last_fix_at='2026-03-25T00:00:00Z', fix_success=False)
    d = original.as_dict()
    restored = Finding.from_dict(d)
    assert restored.finding_id == original.finding_id
    assert restored.fix_attempts == 3
    assert restored.last_fix_error == 'ruff rc=1'
    assert restored.last_fix_at == '2026-03-25T00:00:00Z'
    assert restored.fix_success == False


def test_finding_from_dict_roundtrip_old_record():
    """Roundtrip a Finding with no new fields (old JSONL format)."""
    old_dict = {
        'finding_id': 'old-id',
        'repo': 'old-repo',
        'path': 'old.py',
        'line': 5,
        'rule': 'old-rule',
        'snippet': 'y = 2',
        'confidence': 0.8,
        'quick_win': False,
        'safe_to_autofix': True,
    }
    f = Finding.from_dict(old_dict)
    assert f.fix_attempts == 0
    assert f.last_fix_error is None
    assert f.last_fix_at is None
    assert f.fix_success == False
    # as_dict of a freshly deserialized old record omits new fields
    d = f.as_dict()
    assert 'fix_attempts' not in d
    assert 'last_fix_error' not in d
    assert 'fix_success' not in d


def test_finding_from_dict_partial_record():
    """Partial record: only some new fields present."""
    partial = {
        'finding_id': 'x',
        'repo': 'r',
        'path': 'p',
        'line': 1,
        'rule': 'rule',
        'snippet': 's',
        'confidence': 0.9,
        'quick_win': True,
        'safe_to_autofix': True,
        'fix_attempts': 2,
    }
    f = Finding.from_dict(partial)
    assert f.fix_attempts == 2
    assert f.last_fix_error is None
    assert f.last_fix_at is None
    assert f.fix_success == False


def test_finding_as_dict_omits_defaults():
    """as_dict must not write fields that have default values."""
    f = _make_finding()
    d = f.as_dict()
    assert 'fix_attempts' not in d
    assert 'last_fix_error' not in d
    assert 'last_fix_at' not in d
    assert 'fix_success' not in d


def test_finding_as_dict_includes_non_defaults():
    """as_dict must write fields that have non-default values."""
    f = _make_finding(fix_attempts=1, fix_success=True, last_fix_error='boom')
    d = f.as_dict()
    assert d['fix_attempts'] == 1
    assert d['fix_success'] == True
    assert d['last_fix_error'] == 'boom'
    assert 'last_fix_at' in d


# ─── state.py function tests ────────────────────────────────────────────────

def test_load_finding_record_found():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp:
        tmp.write(json.dumps({'finding_id': 'fid-1', 'repo': 'r', 'path': 'p', 'line': 1, 'rule': 'rule', 'snippet': 's', 'confidence': 0.9, 'quick_win': True, 'safe_to_autofix': True}) + '\n')
        tmp.write(json.dumps({'finding_id': 'fid-2', 'repo': 'r', 'path': 'p', 'line': 2, 'rule': 'rule2', 'snippet': 's', 'confidence': 0.8, 'quick_win': False, 'safe_to_autofix': False}) + '\n')
        path = Path(tmp.name)
    try:
        result = load_finding_record('fid-2', path)
        assert result is not None
        assert result['finding_id'] == 'fid-2'
        assert result['line'] == 2
    finally:
        path.unlink()


def test_load_finding_record_not_found():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp:
        tmp.write(json.dumps({'finding_id': 'fid-1', 'repo': 'r', 'path': 'p', 'line': 1, 'rule': 'rule', 'snippet': 's', 'confidence': 0.9, 'quick_win': True, 'safe_to_autofix': True}) + '\n')
        path = Path(tmp.name)
    try:
        result = load_finding_record('nonexistent', path)
        assert result is None
    finally:
        path.unlink()


def test_load_finding_record_missing_file():
    result = load_finding_record('x', Path('/tmp/does-not-exist-12345.jsonl'))
    assert result is None


def test_load_finding_record_skips_malformed():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp:
        tmp.write('not valid json\n')
        tmp.write(json.dumps({'finding_id': 'fid-good', 'repo': 'r', 'path': 'p', 'line': 1, 'rule': 'rule', 'snippet': 's', 'confidence': 0.9, 'quick_win': True, 'safe_to_autofix': True}) + '\n')
        tmp.write('also not json\n')
        path = Path(tmp.name)
    try:
        result = load_finding_record('fid-good', path)
        assert result is not None
        assert result['finding_id'] == 'fid-good'
    finally:
        path.unlink()


def test_update_finding_record():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp:
        tmp.write(json.dumps({'finding_id': 'fid-1', 'repo': 'r', 'path': 'p', 'line': 1, 'rule': 'rule', 'snippet': 's', 'confidence': 0.9, 'quick_win': True, 'safe_to_autofix': True, 'fix_attempts': 1}) + '\n')
        tmp.write(json.dumps({'finding_id': 'fid-2', 'repo': 'r', 'path': 'p', 'line': 2, 'rule': 'rule2', 'snippet': 's', 'confidence': 0.8, 'quick_win': False, 'safe_to_autofix': False}) + '\n')
        path = Path(tmp.name)
    try:
        ok = update_finding_record('fid-1', path, {'fix_success': True, 'fix_attempts': 2})
        assert ok is True
        lines = path.read_text().splitlines()
        rec1 = json.loads([l for l in lines if 'fid-1' in l][0])
        assert rec1['fix_success'] is True
        assert rec1['fix_attempts'] == 2
        # fid-2 unchanged
        rec2 = json.loads([l for l in lines if 'fid-2' in l][0])
        assert rec2['finding_id'] == 'fid-2'
    finally:
        path.unlink()


def test_update_finding_record_not_found():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp:
        tmp.write(json.dumps({'finding_id': 'fid-1', 'repo': 'r', 'path': 'p', 'line': 1, 'rule': 'rule', 'snippet': 's', 'confidence': 0.9, 'quick_win': True, 'safe_to_autofix': True}) + '\n')
        path = Path(tmp.name)
    try:
        ok = update_finding_record('nonexistent', path, {'fix_success': True})
        assert ok is False
        # File unchanged
        lines = path.read_text().splitlines()
        assert len(lines) == 1
    finally:
        path.unlink()


def test_update_finding_record_preserves_malformed():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp:
        tmp.write('malformed line\n')
        tmp.write(json.dumps({'finding_id': 'fid-1', 'repo': 'r', 'path': 'p', 'line': 1, 'rule': 'rule', 'snippet': 's', 'confidence': 0.9, 'quick_win': True, 'safe_to_autofix': True}) + '\n')
        path = Path(tmp.name)
    try:
        ok = update_finding_record('fid-1', path, {'fix_success': True})
        assert ok is True
        lines = path.read_text().splitlines()
        assert 'malformed line' in lines[0]
    finally:
        path.unlink()


def test_increment_fix_attempt():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp:
        tmp.write(json.dumps({'finding_id': 'inc-1', 'repo': 'r', 'path': 'p', 'line': 1, 'rule': 'rule', 'snippet': 's', 'confidence': 0.9, 'quick_win': True, 'safe_to_autofix': True}) + '\n')
        path = Path(tmp.name)
    try:
        # First attempt
        increment_fix_attempt('inc-1', path, 'error A')
        rec = load_finding_record('inc-1', path)
        assert rec['fix_attempts'] == 1
        assert rec['last_fix_error'] == 'error A'
        assert rec['last_fix_at'] is not None

        # Second attempt
        increment_fix_attempt('inc-1', path, 'error B')
        rec = load_finding_record('inc-1', path)
        assert rec['fix_attempts'] == 2
        assert rec['last_fix_error'] == 'error B'

        # No-op if not found
        increment_fix_attempt('nonexistent', path, 'error C')
    finally:
        path.unlink()


def test_increment_fix_attempt_truncates_long_error():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as tmp:
        tmp.write(json.dumps({'finding_id': 'inc-2', 'repo': 'r', 'path': 'p', 'line': 1, 'rule': 'rule', 'snippet': 's', 'confidence': 0.9, 'quick_win': True, 'safe_to_autofix': True}) + '\n')
        path = Path(tmp.name)
    try:
        long_error = 'X' * 1000
        increment_fix_attempt('inc-2', path, long_error)
        rec = load_finding_record('inc-2', path)
        assert len(rec['last_fix_error']) == 500
    finally:
        path.unlink()


def run_tests():
    tests = [
        test_finding_from_dict_roundtrip_new_record,
        test_finding_from_dict_roundtrip_old_record,
        test_finding_from_dict_partial_record,
        test_finding_as_dict_omits_defaults,
        test_finding_as_dict_includes_non_defaults,
        test_load_finding_record_found,
        test_load_finding_record_not_found,
        test_load_finding_record_missing_file,
        test_load_finding_record_skips_malformed,
        test_update_finding_record,
        test_update_finding_record_not_found,
        test_update_finding_record_preserves_malformed,
        test_increment_fix_attempt,
        test_increment_fix_attempt_truncates_long_error,
    ]
    failed = []
    for t in tests:
        try:
            t()
            print(f'  ✅ {t.__name__}')
        except Exception as e:
            print(f'  ❌ {t.__name__}: {e}')
            failed.append(t.__name__)
    print()
    if failed:
        print(f'FAILED: {len(failed)}/{len(tests)}')
        sys.exit(1)
    else:
        print(f'PASSED: all {len(tests)} tests')


if __name__ == '__main__':
    run_tests()
```

**Run the Phase 1 tests**:
```bash
cd /home/vikas/.openclaw/workspace/qa-agent
python3 core/sandbox_local_runner/test_directive_seeding.py
```

---

## Phase 2: LESSONS_LOG Readable (`utils.py`)

This phase adds read functions to `utils.py`. It builds on **no other phases** — it is independently useful.

### Step 2.1: Modify `append_lesson()` signature

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/utils.py`

**Before**:
```python
def append_lesson(
    lessons_file: Path,
    cycle_type: str,
    what_broke: str = '',
    what_changed: str = '',
    what_worked: str = '',
) -> None:
    """Append a short lesson entry to the lessons log."""
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    lines: List[str] = [f"\n## {date_str} | {cycle_type}"]
    ...
```

**After** — replace the entire function body:

```python
def append_lesson(
    lessons_file: Path,
    cycle_type: str,
    finding_id: str = '',        # NEW: attribute entry to a specific finding
    what_broke: str = '',
    what_changed: str = '',
    what_worked: str = '',
) -> None:
    """Append a short lesson entry to the lessons log.

    Each entry is 1-4 lines capturing what broke, changed, or worked.
    Entries can optionally be tagged with a finding_id for targeted retrieval.

    Log format (finding_id omitted when empty):
        ## 2026-03-25 | pr-cycle
        finding_id: abc123def...
        - **Broke:** ...
        - **Changed:** ...
        - **Worked:** ...
    """
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    lines: List[str] = [f"\n## {date_str} | {cycle_type}"]

    # Tag with finding_id if provided (NEW)
    if finding_id:
        lines.append(f"finding_id: {finding_id}")

    if what_broke:
        lines.append(f"- **Broke:** {what_broke}")
    if what_changed:
        lines.append(f"- **Changed:** {what_changed}")
    if what_worked:
        lines.append(f"- **Worked:** {what_worked}")

    if len(lines) == 1:
        # No content, don't write
        return

    lessons_file.parent.mkdir(parents=True, exist_ok=True)
    with lessons_file.open('a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
```

**Backward compat**: All existing call sites pass `cycle_type` and keyword args — the new `finding_id=''` default means they all continue to work.

### Step 2.2: Add `load_lessons_for_finding()`

Add **after** `append_lesson()` in `utils.py`:

```python
def load_lessons_for_finding(finding_id: str, lessons_file: Path) -> List[Dict[str, Any]]:
    """Parse lessons_file for entries tagged with this finding_id.

    Returns a list of lesson-entry dicts, newest entries first.
    Entries without a finding_id tag are skipped (cannot be attributed).
    Malformed lines are silently skipped.

    Returns:
        List of dicts: {
            'date': '2026-03-25',
            'cycle_type': 'fix-cycle',
            'finding_id': 'abc...',
            'broke': str,
            'changed': str,
            'worked': str,
        }
    """
    if not lessons_file.exists():
        return []

    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    with lessons_file.open('r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.rstrip()

            # New entry header
            if line.startswith('## '):
                # Flush previous entry
                if current and current.get('finding_id') == finding_id:
                    entries.append(current)
                # Parse new header: "## 2026-03-25 | pr-cycle"
                parts = line.lstrip('#').strip().split('|')
                date_part = parts[0].strip() if parts else ''
                cycle_type = parts[1].strip() if len(parts) > 1 else ''
                current = {
                    'date': date_part,
                    'cycle_type': cycle_type,
                    'finding_id': '',   # will be set below
                    'broke': '',
                    'changed': '',
                    'worked': '',
                }
                continue

            if not current:
                continue

            # Finding ID tag
            if line.startswith('finding_id:'):
                current['finding_id'] = line.split(':', 1)[1].strip()
                continue

            # Bullet fields
            if line.startswith('- **Broke:**'):
                current['broke'] = line.split('**Broke:**', 1)[1].strip()
            elif line.startswith('- **Changed:**'):
                current['changed'] = line.split('**Changed:**', 1)[1].strip()
            elif line.startswith('- **Worked:**'):
                current['worked'] = line.split('**Worked:**', 1)[1].strip()

    # Flush last entry
    if current and current.get('finding_id') == finding_id:
        entries.append(current)

    # Newest-first
    entries.reverse()
    return entries
```

### Step 2.3: Add `load_recent_lessons()`

Add **after** `load_lessons_for_finding()` in `utils.py`:

```python
def load_recent_lessons(lessons_file: Path, limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent `limit` lesson entries, newest-first.

    Unlike load_lessons_for_finding, this parses ALL entries regardless of
    finding_id. Entries without a finding_id tag have finding_id=''.

    Returns:
        List of dicts (same shape as load_lessons_for_finding).
    """
    if not lessons_file.exists():
        return []
    if limit <= 0:
        return []

    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    with lessons_file.open('r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.rstrip()

            if line.startswith('## '):
                # Flush previous entry
                if current:
                    entries.append(current)
                    if len(entries) >= limit:
                        break  # Safe: current was the OLD entry, new entry is not yet set
                # Set current to the NEW entry from this ## header BEFORE checking break next time
                parts = line.lstrip('#').strip().split('|')
                date_part = parts[0].strip() if parts else ''
                cycle_type = parts[1].strip() if len(parts) > 1 else ''
                current = {
                    'date': date_part,
                    'cycle_type': cycle_type,
                    'finding_id': '',
                    'broke': '',
                    'changed': '',
                    'worked': '',
                }
                continue
                parts = line.lstrip('#').strip().split('|')
                date_part = parts[0].strip() if parts else ''
                cycle_type = parts[1].strip() if len(parts) > 1 else ''
                current = {
                    'date': date_part,
                    'cycle_type': cycle_type,
                    'finding_id': '',
                    'broke': '',
                    'changed': '',
                    'worked': '',
                }
                continue

            if not current:
                continue

            if line.startswith('finding_id:'):
                current['finding_id'] = line.split(':', 1)[1].strip()
            elif line.startswith('- **Broke:**'):
                current['broke'] = line.split('**Broke:**', 1)[1].strip()
            elif line.startswith('- **Changed:**'):
                current['changed'] = line.split('**Changed:**', 1)[1].strip()
            elif line.startswith('- **Worked:**'):
                current['worked'] = line.split('**Worked:**', 1)[1].strip()

    if current and len(entries) < limit:
        entries.append(current)

    entries.reverse()
    return entries
```

### Step 2.4: Update `__init__.py` re-exports

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/__init__.py`

Add the two new utils functions to the existing utils re-exports:

```python
from .utils import (
    run_capture, run_no_capture, is_path_tracked,
    sanitize_command_template, command_list_to_shell, append_lesson,
    load_lessons_for_finding,     # NEW
    load_recent_lessons,          # NEW
    assert_safe_repo, branch_suffix,
)
```

### Step 2.5: Write Phase 2 unit tests

Add these to `test_directive_seeding.py` (after Phase 1 tests). Requires importing `load_lessons_for_finding` and `load_recent_lessons` from `core.sandbox_local_runner.state` or `core.sandbox_local_runner.utils` (they're exported from `__init__.py`).

Add to the import at the top of `test_directive_seeding.py`:
```python
from core.sandbox_local_runner.utils import load_lessons_for_finding, load_recent_lessons
```

Add these test functions:

```python
# ─── utils.py lesson-load function tests ──────────────────────────────────

def _write_lessons(content: str) -> Path:
    """Helper: write content to a temp file and return the Path."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as tmp:
        tmp.write(content)
        return Path(tmp.name)


def test_load_lessons_for_finding_empty():
    p = _write_lessons('')
    try:
        assert load_lessons_for_finding('x', p) == []
    finally:
        p.unlink()


def test_load_lessons_for_finding_no_match():
    content = """\
## 2026-03-24 | pr-cycle
finding_id: other-id
- **Worked:** something
"""
    p = _write_lessons(content)
    try:
        assert load_lessons_for_finding('x', p) == []
    finally:
        p.unlink()


def test_load_lessons_for_finding_match():
    content = """\
## 2026-03-24 | pr-cycle
finding_id: fid-123
- **Worked:** test passed

## 2026-03-23 | fix-cycle
finding_id: fid-456
- **Broke:** ruff failed
"""
    p = _write_lessons(content)
    try:
        results = load_lessons_for_finding('fid-123', p)
        assert len(results) == 1
        assert results[0]['date'] == '2026-03-24'
        assert results[0]['cycle_type'] == 'pr-cycle'
        assert results[0]['finding_id'] == 'fid-123'
        assert results[0]['worked'] == 'test passed'
        assert results[0]['broke'] == ''
    finally:
        p.unlink()


def test_load_lessons_for_finding_multiple_entries_newest_first():
    content = """\
## 2026-03-20 | fix-cycle
finding_id: multi-1
- **Changed:** first

## 2026-03-22 | fix-cycle
finding_id: multi-1
- **Changed:** second

## 2026-03-24 | fix-cycle
finding_id: multi-1
- **Changed:** third
"""
    p = _write_lessons(content)
    try:
        results = load_lessons_for_finding('multi-1', p)
        assert len(results) == 3
        # Newest first
        assert results[0]['changed'] == 'third'
        assert results[2]['changed'] == 'first'
    finally:
        p.unlink()


def test_load_lessons_for_finding_malformed():
    content = """\
not a real line
## 2026-03-24 | fix-cycle
finding_id: bad-id
malformed bullet
- **Worked:** valid
## broken header
finding_id: bad-id
"""
    p = _write_lessons(content)
    try:
        results = load_lessons_for_finding('bad-id', p)
        # Should skip malformed lines and return the valid entry
        assert len(results) == 1
        assert results[0]['worked'] == 'valid'
    finally:
        p.unlink()


def test_load_lessons_for_finding_no_finding_id_omitted():
    """Entries without finding_id: tag should not match."""
    content = """\
## 2026-03-24 | pr-cycle
- **Worked:** old entry without tag
"""
    p = _write_lessons(content)
    try:
        assert load_lessons_for_finding('anything', p) == []
    finally:
        p.unlink()


def test_load_recent_lessons():
    content = """\
## 2026-03-20 | a-cycle
- **Changed:** entry 1

## 2026-03-21 | b-cycle
- **Changed:** entry 2

## 2026-03-22 | c-cycle
- **Changed:** entry 3

## 2026-03-23 | d-cycle
- **Changed:** entry 4

## 2026-03-24 | e-cycle
- **Changed:** entry 5
"""
    p = _write_lessons(content)
    try:
        # limit=3 should return newest 3
        results = load_recent_lessons(p, limit=3)
        assert len(results) == 3
        assert results[0]['cycle_type'] == 'e-cycle'
        assert results[2]['cycle_type'] == 'c-cycle'
    finally:
        p.unlink()


def test_load_recent_lessons_fewer_available():
    content = """\
## 2026-03-24 | only-one
- **Changed:** just one
"""
    p = _write_lessons(content)
    try:
        results = load_recent_lessons(p, limit=20)
        assert len(results) == 1
    finally:
        p.unlink()


def test_load_recent_lessons_limit_zero():
    p = _write_lessons('## 2026-03-24 | x\n- **Changed:** y\n')
    try:
        assert load_recent_lessons(p, limit=0) == []
    finally:
        p.unlink()
```

**Run Phase 1+2 tests together**:
```bash
cd /home/vikas/.openclaw/workspace/qa-agent
python3 core/sandbox_local_runner/test_directive_seeding.py
```

---

## Phase 3: Exponential Backoff Cooldown (`state.py`)

This phase modifies `state.py` functions. It builds on **Phase 1** (failure tracking fields exist in `mark_finding_activity` entries) but has **no dependency on utils or prompts**.

### Step 3.1: Add `get_effective_cooldown()`

Add at the **top** of `state.py` (near the other constants block) or right before `mark_finding_activity`:

```python
MAX_COOLDOWN_SECONDS = 7 * 24 * 60 * 60  # 7 days — cap for exponential backoff
```

Then add the function **after** the `MAX_RECONCILIATION_EVENTS` import line or near `filter_findings_by_cooldown`:

```python
def get_effective_cooldown(
    finding_id: str,
    state: Dict[str, Any],
    base_cooldown_seconds: int,
) -> int:
    """Returns effective cooldown for a finding, accounting for failure history.

    Formula:
      - failure_count == 0 (or absent): base_cooldown_seconds
      - failure_count >= 1: base_cooldown_seconds * (2 ** failure_count)
      - Capped at MAX_COOLDOWN_SECONDS (7 days)

    State is read but NOT mutated by this function.

    Args:
        finding_id: The finding to look up.
        state: The full state dict (from load_state / in-memory).
        base_cooldown_seconds: The flat cooldown configured by the user.

    Returns:
        Effective cooldown in seconds.
    """
    activity = state.get('finding_activity', {})
    entry = activity.get(finding_id, {})
    failure_count = entry.get('failure_count', 0)

    if failure_count == 0:
        return base_cooldown_seconds

    effective = base_cooldown_seconds * (2 ** failure_count)
    return min(effective, MAX_COOLDOWN_SECONDS)
```

### Step 3.2: Extend `mark_finding_activity()`

**Replace** the existing `mark_finding_activity` function in `state.py`:

**Before**:
```python
def mark_finding_activity(state: Dict[str, Any], finding_ids: List[str], action: str) -> None:
    if not finding_ids:
        return
    activity = state.setdefault('finding_activity', {})
    ts = now_iso()
    for finding_id in finding_ids:
        activity[finding_id] = {
            'last_action': action,
            'last_action_at': ts,
        }
```

**After**:
```python
def mark_finding_activity(
    state: Dict[str, Any],
    finding_ids: List[str],
    action: str,
    failure_count: Optional[int] = None,   # NEW
    last_error: Optional[str] = None,        # NEW
) -> None:
    """Record an activity event for one or more findings.

    Args:
        state: The full state dict (mutated in-place).
        finding_ids: Finding IDs to record activity for.
        action: Human-readable action label (e.g. 'fix-attempt', 'fix-succeeded').
        failure_count: Optional. If provided, stores the failure count on the
            finding's activity entry, enabling exponential backoff cooldown.
        last_error: Optional. If provided, stores the last error string.
    """
    if not finding_ids:
        return
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

**Backward compat**: All existing call sites (in `cli.py`) pass only 3 positional args — the two new kwargs default to `None`, so existing code is unaffected.

### Step 3.3: Modify `filter_findings_by_cooldown()`

**Replace** the existing `filter_findings_by_cooldown` function in `state.py`:

**Before**:
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
        elapsed = age_seconds(last_action_at, reference=now)
        if elapsed is not None and elapsed < cooldown_seconds:
            remaining = cooldown_seconds - elapsed
            _append_text(
                log_file,
                'cooldown-suppress: '
                f'finding_id={finding.finding_id} '
                f'last_action={entry.get("last_action", "unknown")} '
                f'last_action_at={last_action_at} '
                f'remaining_seconds={remaining}',
            )
            suppressed.append(finding)
            continue
        allowed.append(finding)

    return allowed, suppressed
```

**After**:
```python
def filter_findings_by_cooldown(
    findings: List[Finding],
    state: Dict[str, Any],
    cooldown_seconds: int,
    log_file: Path,
) -> tuple[List[Finding], List[Finding]]:
    """Filter findings by per-finding cooldown.

    Uses get_effective_cooldown to determine the per-finding cooldown,
    which applies exponential backoff based on failure_count.
    """
    allowed: List[Finding] = []
    suppressed: List[Finding] = []
    now = datetime.now(timezone.utc)

    for finding in findings:
        effective_cooldown = get_effective_cooldown(
            finding.finding_id, state, cooldown_seconds
        )
        entry = state.get('finding_activity', {}).get(finding.finding_id, {})
        last_action_at = entry.get('last_action_at')
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

**Key change**: Uses `get_effective_cooldown()` instead of the flat `cooldown_seconds`. The log message format gains two new fields: `effective_cooldown=N` and `failure_count=M`.

### Step 3.4: Write Phase 3 unit tests

Add to `test_directive_seeding.py`:

```python
from core.sandbox_local_runner.state import (
    get_effective_cooldown,
    mark_finding_activity,
    filter_findings_by_cooldown,
)


# ─── Exponential backoff tests ─────────────────────────────────────────────

def test_effective_cooldown_no_activity():
    """Finding with no entry in state should get base cooldown."""
    state = {'finding_activity': {}}
    result = get_effective_cooldown('x', state, base_cooldown_seconds=14400)
    assert result == 14400


def test_effective_cooldown_zero_failures():
    """Finding with failure_count=0 should get base cooldown."""
    state = {
        'finding_activity': {
            'x': {'last_action': 'pr-opened', 'last_action_at': '2026-03-25T00:00:00Z', 'failure_count': 0}
        }
    }
    result = get_effective_cooldown('x', state, base_cooldown_seconds=14400)
    assert result == 14400


def test_effective_cooldown_one_failure():
    """failure_count=1 → 2x base."""
    state = {
        'finding_activity': {
            'x': {'last_action': 'fix-attempt', 'failure_count': 1}
        }
    }
    result = get_effective_cooldown('x', state, base_cooldown_seconds=14400)
    assert result == 28800  # 4h * 2


def test_effective_cooldown_exponential():
    """failure_count=3 → 8x base."""
    state = {
        'finding_activity': {
            'x': {'last_action': 'fix-attempt', 'failure_count': 3}
        }
    }
    result = get_effective_cooldown('x', state, base_cooldown_seconds=14400)
    assert result == 115200  # 4h * 8


def test_effective_cooldown_capped_at_7_days():
    """failure_count=10 → capped at 7 days, not 4h * 2^10."""
    state = {
        'finding_activity': {
            'x': {'last_action': 'fix-attempt', 'failure_count': 10}
        }
    }
    result = get_effective_cooldown('x', state, base_cooldown_seconds=14400)
    seven_days = 7 * 24 * 60 * 60
    assert result == seven_days  # capped, not 14400 * 1024
    assert result < 14400 * 1024  # definitely not uncapped


def test_mark_finding_activity_with_failure_count():
    """mark_finding_activity stores failure_count and last_error."""
    state = {'finding_activity': {}}
    mark_finding_activity(state, ['f1'], action='fix-attempt', failure_count=2, last_error='ruff rc=1')
    entry = state['finding_activity']['f1']
    assert entry['failure_count'] == 2
    assert entry['last_error'] == 'ruff rc=1'
    assert entry['last_action'] == 'fix-attempt'


def test_mark_finding_activity_resets_failure_count():
    """failure_count=0 can be passed to reset."""
    state = {
        'finding_activity': {
            'f1': {'last_action': 'fix-attempt', 'failure_count': 5, 'last_error': 'old error'}
        }
    }
    mark_finding_activity(state, ['f1'], action='fix-succeeded', failure_count=0, last_error=None)
    entry = state['finding_activity']['f1']
    assert entry['failure_count'] == 0
    assert entry.get('last_error') is None


def test_filter_finds_suppressed_with_extended_cooldown():
    """A finding with 2 failures whose elapsed time is less than 16h
    should be suppressed (16h = 4h * 2^2)."""
    from datetime import datetime, timezone
    from core.sandbox_local_runner.models import Finding

    # Set last_action_at to 10 hours ago
    ten_hours_ago = (datetime.now(timezone.utc)).isoformat()

    # Manually create state with failure_count=2
    state = {
        'finding_activity': {
            'f-new': {
                'last_action': 'fix-attempt',
                'last_action_at': ten_hours_ago,
                'failure_count': 2,
            }
        }
    }
    # Need to set the timestamp correctly
    state['finding_activity']['f-new']['last_action_at'] = (
        datetime.now(timezone.utc)
    ).isoformat()

    f = Finding(
        finding_id='f-new',
        repo='r',
        path='p',
        line=1,
        rule='rule',
        snippet='s',
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )

    import io
    from pathlib import Path
    log_buffer = io.StringIO()
    # Create a dummy log file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as lf:
        log_path = Path(lf.name)

    try:
        allowed, suppressed = filter_findings_by_cooldown(
            findings=[f],
            state=state,
            cooldown_seconds=14400,  # 4h base
            log_file=log_path,
        )
        # 2 failures → effective cooldown = 4h * 4 = 16h
        # elapsed ~0s < 16h → suppressed
        assert len(suppressed) == 1
        assert suppressed[0].finding_id == 'f-new'
        assert len(allowed) == 0
    finally:
        log_path.unlink()
```

**Run all Phase 1–3 tests**:
```bash
cd /home/vikas/.openclaw/workspace/qa-agent
python3 core/sandbox_local_runner/test_directive_seeding.py
```

---

## Phase 4: Wire into Lifecycle (`prompts.py` + `lifecycle.py` + `cli.py`)

This phase connects the data layer (Phases 1–3) to the LLM prompt so the agent actually receives the context. It must be done last.

### Step 4.1: Extend `render_claude_fix_prompt()`

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/prompts.py`

**Strategy**: All existing callers pass only the 5 original args. The new params (`fix_history`, `finding_record`) will default to `None`. When both are `None`/empty, the function behaves identically to before.

**Add** `from __future__ import annotations` (already present) and add these two new parameters to the function signature. Add them as the **last two optional parameters** to avoid breaking any existing call sites:

**Before**:
```python
def render_claude_fix_prompt(
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    max_files_changed: int,
    max_loc_diff: int,
) -> str:
```

**After** — replace the entire function body (the specialized prompts at the top remain unchanged; only the base prompt at the end gains the new section):

```python
def render_claude_fix_prompt(
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    max_files_changed: int,
    max_loc_diff: int,
    fix_history: Optional[List[Dict[str, Any]]] = None,      # NEW
    finding_record: Optional[Dict[str, Any]] = None,          # NEW
) -> str:
    # [specialized prompt dispatch — unchanged]
    if finding.rule == 'xo-max-lines':
        ...
    if finding.rule in (...):
        ...

    # [build baseline_lines and target_lines — unchanged]

    # NEW: Inject fix context sections
    extra_sections: List[str] = []

    # Section A: Fix history from findings.jsonl (Phase 2 finding_record)
    if finding_record is not None and finding_record.get('fix_attempts', 0) > 0:
        attempts = finding_record['fix_attempts']
        last_error = finding_record.get('last_fix_error') or '(none)'
        extra_sections.append(
            f'## Fix history\n'
            f'- Attempts: {attempts}\n'
            f'- Last error: {last_error}\n'
            f'- This is a known-difficult finding. Consider a more conservative approach.\n'
        )

    # Section B: Prior context from LESSONS_LOG.md (Phase 1 fix_history)
    if fix_history:
        extra_sections.append('## Prior context')
        for entry in fix_history[:3]:  # Show up to 3 most recent
            date = entry.get('date', 'unknown')
            cycle = entry.get('cycle_type', 'unknown')
            status = entry.get('changed') or entry.get('broke') or '(no detail)'
            extra_sections.append(
                f'- {date} ({cycle}): {status}'
            )
        extra_sections.append('')

    extra_text = '\n'.join(extra_sections) if extra_sections else ''

    snippet = finding.snippet or '(snippet unavailable)'
    return '\n'.join(
        [
            '# QA Autofix Task',
            '',
            '## Finding metadata',
            f'- finding_id: `{finding.finding_id}`',
            f'- rule: `{finding.rule}`',
            f'- file: `{finding.path}`',
            f'- line: `{finding.line}`',
            f'- confidence: `{finding.confidence}`',
            '',
            extra_text,   # ← injected here (empty string if no context)
            '## Snippet',
            '```',
            snippet,
            '```',
            '',
            '## Constraints (must follow)',
            '- Make the minimal change required to fix this finding.',
            '- Respect scope caps (do not exceed these limits):',
            f'  - max_files_changed: `{max_files_changed}`',
            f'  - max_loc_diff: `{max_loc_diff}`',
            '- No unrelated edits or refactors.',
            '- Preserve existing behavior outside this finding.',
            '',
            '## Validation command context',
            'Run relevant checks from repo root and fail (non-zero exit) if they do not pass.',
            '',
            '### Baseline checks (always relevant)',
            baseline_lines,
            '',
            '### Rule-target checks',
            target_lines,
        ]
    ) + '\n'
```

### Step 4.2: Update `apply_claude_fix()`

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/lifecycle.py`

**Add import** at the top of the file (group with other `.utils` imports):
```python
from .utils import (
    run_capture, run_no_capture, sanitize_command_template,
    append_lesson,             # ALREADY IMPORTED — no change needed
    load_lessons_for_finding,   # ADD
)
```

**Replace the existing `apply_claude_fix()` function** (starts at line ~432):

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
    findings_file: Path,           # NEW
    lessons_file: Path,             # NEW
) -> Tuple[int, str, str]:
    """Run a Claude-assisted fix for a single finding.

    Args:
        findings_file: Path to the JSONL findings file (for tracking fix attempts).
        lessons_file: Path to LESSONS_LOG.md (for per-finding lesson logging).
    """
    # Load fix history for this finding (Phase 1 + Phase 2)
    fix_history = load_lessons_for_finding(finding.finding_id, lessons_file)

    # Load finding record from JSONL for fix_attempts count
    finding_record = None
    if findings_file.exists():
        try:
            # Import here to avoid circular — load_finding_record is in state.py
            from .state import load_finding_record as _load_finding_record
            finding_record = _load_finding_record(finding.finding_id, findings_file)
        except Exception:
            finding_record = None

    prompt_path = worktree_path / QA_FIX_PROMPT_FILENAME
    prompt_text = render_claude_fix_prompt(
        finding=finding,
        baseline_checks=baseline_checks,
        target_checks=target_checks,
        max_files_changed=max_files_changed,
        max_loc_diff=max_loc_diff,
        fix_history=fix_history,
        finding_record=finding_record,
    )
    prompt_path.write_text(prompt_text, encoding='utf-8')

    try:
        try:
            command = claude_cmd_template.format(
                prompt_file=shlex.quote(str(prompt_path)),
                finding_id=shlex.quote(finding.finding_id),
                rule=shlex.quote(finding.rule),
                path=shlex.quote(finding.path),
            )
        except KeyError as exc:
            error = f'invalid claude command template placeholder: {exc}'
            _append_text(log_file, f'claude-autofix: {error}')
            return 2, error, str(prompt_path)

        _append_text(
            log_file,
            'claude-autofix: '
            f'finding_id={finding.finding_id} prompt_file={prompt_path} '
            f'cmd={sanitize_command_template(command)}',
        )
        res = subprocess.run(
            ['bash', '-l', '-c', command],
            cwd=str(worktree_path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = (res.stdout or '').strip()

        # Record the fix attempt result (Phase 2 findings failure memory)
        error_msg: Optional[str] = None
        if res.returncode != 0:
            error_msg = f"claude rc={res.returncode} output={output[:300]}"

        from .state import increment_fix_attempt, update_finding_record
        increment_fix_attempt(finding.finding_id, findings_file, error_msg)

        if res.returncode == 0:
            update_finding_record(finding.finding_id, findings_file, {'fix_success': True})
            append_lesson(
                lessons_file=lessons_file,
                cycle_type='fix-cycle',
                finding_id=finding.finding_id,
                what_changed=f"fix succeeded rule={finding.rule}",
            )
        else:
            append_lesson(
                lessons_file=lessons_file,
                cycle_type='fix-cycle',
                finding_id=finding.finding_id,
                what_broke=f"fix failed rc={res.returncode}",
            )

        _append_text(
            log_file,
            'claude-autofix-result: '
            f'finding_id={finding.finding_id} rc={res.returncode} output={(output or "<empty>")[:1000]}',
        )
        return res.returncode, output, str(prompt_path)
    finally:
        try:
            prompt_path.unlink(missing_ok=True)
        except Exception:
            pass
```

### Step 4.3: `apply_autofix()` — No changes needed

`apply_autofix()` (`lifecycle.py` ~line 64) writes only to the log file and does not interact with `findings.jsonl`. Per the design, deterministic fixes don't get failure tracking. **Do not modify `apply_autofix()`**.

### Step 4.4: Update orchestrator call sites in `cli.py`

**File**: `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/cli.py`

**Change 1**: Add the two new imports from `state` and `lifecycle` (add to existing import blocks):

In the `from .state import (...)` block, add:
```python
from .state import (
    ...,
    load_finding_record,          # ADD
    update_finding_record,        # ADD
    increment_fix_attempt,        # ADD
)
```

In the `from .lifecycle import (...)` block, no changes needed — `apply_claude_fix` is already imported (but its signature has changed).

**Change 2**: Update the `apply_claude_fix()` call in `cli.py` (around **line 674**).

Find this code:
```python
rc, claude_output, prompt_file = apply_claude_fix(
    worktree_path=worktree_path,
    finding=finding,
    baseline_checks=BASELINE_VALIDATION_CHECKS,
    target_checks=target_checks,
    claude_cmd_template=args.claude_cmd_template,
    max_files_changed=args.max_files_changed,
    max_loc_diff=args.max_loc_diff,
    log_file=log_file,
)
```

Replace with:
```python
rc, claude_output, prompt_file = apply_claude_fix(
    worktree_path=worktree_path,
    finding=finding,
    baseline_checks=BASELINE_VALIDATION_CHECKS,
    target_checks=target_checks,
    claude_cmd_template=args.claude_cmd_template,
    max_files_changed=args.max_files_changed,
    max_loc_diff=args.max_loc_diff,
    log_file=log_file,
    findings_file=findings_file,    # NEW
    lessons_file=lessons_file,      # NEW
)
```

Both `findings_file` and `lessons_file` are `Path` objects already defined in `main()`:
- `findings_file = Path(args.findings_file)` (defined around line 180)
- `lessons_file = Path(args.lessons_file)` (defined around line 193)

**Change 3**: After a failed fix in the `apply_autofix` branch, also record failure tracking (the `apply_claude_fix` branch now handles this internally; the deterministic branch does not):

In the `apply_autofix` branch, after `fixes_failed_verification += 1`, add:

```python
# Record failure tracking for deterministic fixes too (Phase 2)
if finding.finding_id:
    increment_fix_attempt(
        finding.finding_id,
        findings_file,
        f'autofix no-op for rule={finding.rule}',
    )
```

**Change 4**: Update `mark_finding_activity` calls in `cli.py` to pass failure context:

In the failure branch of the Claude fix (after `run_status = 'fix-failed-verification:claude-command-failed'`), find the existing `mark_finding_activity` call and extend it:

**Before**:
```python
mark_finding_activity(state=state, finding_ids=[finding.finding_id], action='fix-failed-verification')
```

**After** (the exact location in cli.py — search for `mark_finding_activity(state=state, finding_ids=[finding.finding_id], action='fix-failed-verification')`):
```python
# Get current failure count from state, then increment
current_entry = state.get('finding_activity', {}).get(finding.finding_id, {})
current_failures = current_entry.get('failure_count', 0)
mark_finding_activity(
    state=state,
    finding_ids=[finding.finding_id],
    action='fix-failed-verification',
    failure_count=current_failures + 1,
    last_error=f'claude rc={rc}',
)
```

Similarly update the other failure call sites for `apply_autofix` failures:
```python
mark_finding_activity(
    state=state,
    finding_ids=[finding.finding_id],
    action='fix-failed-verification',
    failure_count=state.get('finding_activity', {}).get(finding.finding_id, {}).get('failure_count', 0) + 1,
    last_error=f'autofix no-op rule={finding.rule}',
)
```

And for the success path (`action='pr-opened'`):
```python
mark_finding_activity(
    state=state,
    finding_ids=[finding.finding_id],
    action='pr-opened',
    failure_count=0,   # Reset on success
    last_error=None,
)
```

### Step 4.5: Write Phase 4 unit tests

Add to `test_directive_seeding.py`:

```python
from core.sandbox_local_runner.prompts import render_claude_fix_prompt


def _make_minimal_finding():
    from core.sandbox_local_runner.models import Finding
    return Finding(
        finding_id='test-001',
        repo='test-repo',
        path='src/test.py',
        line=10,
        rule='ruff-b007',
        snippet='x = 1',
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )


def test_prompt_has_prior_context_when_history_exists():
    """When fix_history is non-empty, prompt must contain ## Prior context section."""
    f = _make_minimal_finding()
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        fix_history=[
            {'date': '2026-03-24', 'cycle_type': 'fix-cycle', 'finding_id': 'test-001', 'broke': 'ruff rc=1', 'changed': '', 'worked': ''},
        ],
    )
    assert '## Prior context' in prompt
    assert '2026-03-24' in prompt
    assert 'ruff rc=1' in prompt


def test_prompt_omits_prior_context_when_empty():
    """When fix_history is [], prompt must NOT contain ## Prior context."""
    f = _make_minimal_finding()
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        fix_history=[],
    )
    assert '## Prior context' not in prompt


def test_prompt_omits_prior_context_when_none():
    """When fix_history is None (default), prompt must NOT contain ## Prior context."""
    f = _make_minimal_finding()
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        # fix_history not passed → defaults to None
    )
    assert '## Prior context' not in prompt


def test_prompt_has_fix_history_when_attempts_gt_zero():
    """When finding_record has fix_attempts > 0, prompt must contain ## Fix history."""
    f = _make_minimal_finding()
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        finding_record={'fix_attempts': 3, 'last_fix_error': 'ruff rc=1', 'last_fix_at': '2026-03-25T00:00:00Z'},
    )
    assert '## Fix history' in prompt
    assert 'Attempts: 3' in prompt
    assert 'ruff rc=1' in prompt
    assert 'known-difficult' in prompt


def test_prompt_omits_fix_history_when_attempts_zero():
    """When finding_record has fix_attempts=0, ## Fix history must be absent."""
    f = _make_minimal_finding()
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        finding_record={'fix_attempts': 0, 'last_fix_error': None},
    )
    assert '## Fix history' not in prompt


def test_prompt_omits_fix_history_when_record_none():
    """When finding_record is None (default), ## Fix history must be absent."""
    f = _make_minimal_finding()
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
    )
    assert '## Fix history' not in prompt


def test_prompt_has_both_sections_when_both_provided():
    """When both fix_history and finding_record are non-empty, both sections appear."""
    f = _make_minimal_finding()
    prompt = render_claude_fix_prompt(
        finding=f,
        baseline_checks={},
        target_checks={},
        max_files_changed=5,
        max_loc_diff=200,
        fix_history=[{'date': '2026-03-24', 'cycle_type': 'fix-cycle', 'finding_id': 'test-001', 'broke': 'old error', 'changed': '', 'worked': ''}],
        finding_record={'fix_attempts': 2, 'last_fix_error': 'recent error'},
    )
    assert '## Prior context' in prompt
    assert '## Fix history' in prompt
    assert 'Attempts: 2' in prompt
    assert 'old error' in prompt
    assert 'recent error' in prompt
```

---

## Phase 5: Integration Test

### Step 5.1: End-to-end smoke test

This is a **conceptual** integration test — it describes the full cycle. It cannot run in isolation but serves as a checklist.

Create `/home/vikas/.openclaw/workspace/qa-agent/core/sandbox_local_runner/test_directive_seeding_e2e.py`:

```python
#!/usr/bin/env python3
"""test_directive_seeding_e2e.py — End-to-end smoke test for directive seeding.

This test simulates the full flow:
1. A finding is discovered and appended to findings.jsonl
2. First fix attempt fails → failure_count increments, lesson logged
3. Second filter cycle sees extended cooldown
4. Prompt gets context injected

Run manually with a live sandbox repo (not in CI by default).
"""
import json
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.sandbox_local_runner.models import Finding
from core.sandbox_local_runner.state import (
    load_finding_record, update_finding_record, increment_fix_attempt,
    mark_finding_activity, filter_findings_by_cooldown, get_effective_cooldown,
    load_state, save_state,
)
from core.sandbox_local_runner.utils import append_lesson, load_lessons_for_finding


def test_full_directive_seeding_flow():
    """Simulate the complete directive-seeding feedback loop."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        findings_file = tmp / 'findings.jsonl'
        lessons_file = tmp / 'LESSONS_LOG.md'
        state_file = tmp / 'state.json'

        # 1. Discovery: append a finding
        f = Finding(
            finding_id='e2e-test-001',
            repo='test-repo',
            path='src/test.py',
            line=10,
            rule='ruff-b007',
            snippet='x = 1',
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )
        from core.sandbox_local_runner.state import append_findings
        written = append_findings(findings_file, [f])
        assert written == 1, f'Expected 1 written, got {written}'

        # Verify it was written (no new fields)
        rec = load_finding_record('e2e-test-001', findings_file)
        assert rec is not None
        assert rec['fix_attempts'] == 0  # default

        # 2. First fix attempt FAILS
        increment_fix_attempt('e2e-test-001', findings_file, 'claude rc=1')
        rec = load_finding_record('e2e-test-001', findings_file)
        assert rec['fix_attempts'] == 1
        assert rec['last_fix_error'] == 'claude rc=1'

        # Load state and record failure
        state = load_state(state_file)
        mark_finding_activity(
            state, ['e2e-test-001'],
            action='fix-attempt',
            failure_count=1,
            last_error='claude rc=1',
        )
        save_state(state_file, state)

        # Log a per-finding lesson
        append_lesson(
            lessons_file=lessons_file,
            cycle_type='fix-cycle',
            finding_id='e2e-test-001',
            what_broke='fix failed rc=1',
        )

        # 3. Second fix attempt FAILS
        increment_fix_attempt('e2e-test-001', findings_file, 'claude rc=1 again')
        state = load_state(state_file)
        mark_finding_activity(
            state, ['e2e-test-001'],
            action='fix-attempt',
            failure_count=2,
            last_error='claude rc=1 again',
        )
        save_state(state_file, state)

        # 4. Effective cooldown check
        effective = get_effective_cooldown('e2e-test-001', state, base_cooldown_seconds=14400)
        assert effective == 57600, f'Expected 57600 (4h * 4), got {effective}'  # 4h * 4

        # 5. LESSONS_LOG: can retrieve the entry
        lessons = load_lessons_for_finding('e2e-test-001', lessons_file)
        assert len(lessons) == 1
        assert lessons[0]['broke'] == 'fix failed rc=1'
        assert lessons[0]['finding_id'] == 'e2e-test-001'

        # 6. Prompt context: check render_claude_fix_prompt includes context
        from core.sandbox_local_runner.prompts import render_claude_fix_prompt
        fix_history = load_lessons_for_finding('e2e-test-001', lessons_file)
        rec = load_finding_record('e2e-test-001', findings_file)
        prompt = render_claude_fix_prompt(
            finding=f,
            baseline_checks={},
            target_checks={},
            max_files_changed=5,
            max_loc_diff=200,
            fix_history=fix_history,
            finding_record=rec,
        )
        assert '## Prior context' in prompt
        assert '## Fix history' in prompt
        assert 'Attempts: 2' in prompt
        assert 'fix failed rc=1' in prompt

        # 7. Fix SUCCEEDS — failure_count resets
        update_finding_record('e2e-test-001', findings_file, {'fix_success': True})
        state = load_state(state_file)
        mark_finding_activity(
            state, ['e2e-test-001'],
            action='fix-succeeded',
            failure_count=0,  # Reset on success
            last_error=None,
        )
        save_state(state_file, state)

        # Cooldown resets to base
        effective_after = get_effective_cooldown('e2e-test-001', state, base_cooldown_seconds=14400)
        assert effective_after == 14400, f'Expected 14400 after reset, got {effective_after}'

        rec_after = load_finding_record('e2e-test-001', findings_file)
        assert rec_after['fix_success'] == True

        print('✅ Full directive-seeding E2E flow passed')


if __name__ == '__main__':
    test_full_directive_seeding_flow()
```

---

## Rollout Order

Implement phases in this exact order. Each phase is independently testable.

| Phase | File(s) Changed | Why This Order |
|---|---|---|
| **1** | `models.py`, `state.py` (new funcs), `__init__.py` | Self-contained data layer. No downstream consumers yet. |
| **2** | `utils.py`, `__init__.py` | Utilities that read LESSONS_LOG. No dependencies on Phase 1. |
| **3** | `state.py` (modify funcs) | Depends on Phase 1 field layout (`failure_count` in `mark_finding_activity` entry). |
| **4** | `prompts.py`, `lifecycle.py`, `cli.py` | Connects all data. Depends on Phases 1, 2, 3 existing. |
| **5** | New `test_directive_seeding_e2e.py` | Integration test. Depends on all phases complete. |

**Verification command after each phase**:
```bash
# Phase 1
python3 -c "from core.sandbox_local_runner.models import Finding; print('models OK')"
python3 core/sandbox_local_runner/test_directive_seeding.py

# Phase 2 — after Phase 1
python3 -c "from core.sandbox_local_runner.utils import load_lessons_for_finding, load_recent_lessons; print('utils OK')"

# Phase 3 — after Phase 2
python3 -c "from core.sandbox_local_runner.state import get_effective_cooldown, filter_findings_by_cooldown; print('state OK')"

# Phase 4 — after Phase 3
python3 -c "from core.sandbox_local_runner.prompts import render_claude_fix_prompt; print('prompts OK')"
python3 -c "from core.sandbox_local_runner.lifecycle import apply_claude_fix; print('lifecycle OK')"

# Phase 5
python3 core/sandbox_local_runner/test_directive_seeding_e2e.py
```

---

## Backward Compatibility Checklist

| Change | Backward Compatible? | Details |
|---|---|---|
| `Finding` dataclass gains 4 new fields with defaults | ✅ YES | `fix_attempts=0`, `last_fix_error=None`, `last_fix_at=None`, `fix_success=False` — all have defaults. Old records without these fields deserialize correctly via `from_dict`. |
| `Finding.as_dict()` omits default fields | ✅ YES | Old code reading `as_dict()` results sees the same output. |
| `Finding.from_dict()` added | ✅ YES | Pure addition. |
| `load_finding_record()` — new function | ✅ YES | Returns `None` for missing files/records. All callers can handle `None`. |
| `update_finding_record()` — new function | ✅ YES | Returns `False` for missing. All callers can handle `False`. |
| `increment_fix_attempt()` — new function | ✅ YES | Called only by new code in Phase 4. |
| `append_lesson()` gains `finding_id=''` default | ✅ YES | All existing call sites omit this param. |
| `load_lessons_for_finding()` — new function | ✅ YES | Called only by new code in Phase 4. |
| `load_recent_lessons()` — new function | ✅ YES | Called only by new code in Phase 4. |
| `get_effective_cooldown()` — new function | ✅ YES | Called only by Phase 3's `filter_findings_by_cooldown`. |
| `mark_finding_activity()` gains optional kwargs | ✅ YES | All existing call sites pass 3 positional args only. |
| `filter_findings_by_cooldown()` uses new formula | ✅ YES | State entries without `failure_count` get treated as `0` → same flat cooldown. |
| `render_claude_fix_prompt()` gains optional params | ✅ YES | `fix_history=None`, `finding_record=None` — defaults to old behavior. |
| `apply_claude_fix()` signature changes | ⚠️ CALL SITE CHANGE | Only `cli.py` calls this. Must pass `findings_file` and `lessons_file`. Existing callers (if any) in tests must be updated. |
| `cli.py` — `mark_finding_activity` calls updated | ⚠️ CALL SITE CHANGE | Existing calls in `cli.py` use only positional args — still works due to defaults. New failure-tracking calls added. |

**Breaking change note**: `apply_claude_fix()` now requires `findings_file` and `lessons_file`. If any test or external caller invokes `apply_claude_fix` directly without these params, it will raise `TypeError`. Update those call sites to pass the file paths.

---

## Risk Mitigation

### Phase 1 Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `Finding.from_dict()` introduced after `append_findings` writes records — records written during implementation have `discovered_at` but no new fields | Low | `from_dict()` uses `.get('fix_attempts', 0)` — handles missing fields gracefully. |
| `as_dict()` omits `fix_attempts: 0` for newly discovered findings — `load_finding_record` returns records without the field | Low | `increment_fix_attempt` uses `record.get('fix_attempts', 0)` — correct for both old (missing) and new (0) records. |
| JSONL file grows unbounded with per-attempt writes | Low | Each fix attempt rewrites the file (O(n)). For thousands of findings this is acceptable. Monitor `findings.jsonl` size; shard if needed. |

**Detection**: Run `test_directive_seeding.py` after Phase 1. Also run existing `test_refactor.py` to confirm no regressions.

### Phase 2 Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `load_lessons_for_finding()` scans entire file on every `apply_claude_fix` call — slow for large logs | Medium | File is appended-only and grows slowly. First implementation scans full file. If profiling shows it matters, implement a tail-scan (last 10KB). |
| Existing `LESSONS_LOG.md` entries without `finding_id:` break the parser | Low | Parser skips lines that don't match known patterns. Malformed bullets are ignored. Header without finding_id is treated as untagged. |
| `append_lesson` now writes an extra blank line before `##` | Low | The format `"\n" + "## ..."` means existing entries end with `\n`. New entries start with `\n## ...`. This produces a double-newline between entries — acceptable in markdown. |

**Detection**: `test_load_lessons_for_finding_*` tests cover malformed/unexpected input.

### Phase 3 Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `filter_findings_by_cooldown` now uses exponential formula — existing findings with `failure_count` not present get treated as 0 | Low | This is the desired behavior — no change in behavior for old entries. |
| 7-day cap reached quickly with repeated failures | Low | Intentional design. After 7 failures the finding is capped. Agent will still see it after 7 days. |
| `mark_finding_activity` with `failure_count=0` overwrites existing higher count on success — if called before `increment_fix_attempt`, wrong order could lose count | Low | The order in `cli.py` is: `increment_fix_attempt` (writes to findings.jsonl) then `mark_finding_activity` (resets state). Correct. |

**Detection**: `test_effective_cooldown_*` tests cover all formula branches. `test_filter_finds_suppressed_with_extended_cooldown` covers the suppression case.

### Phase 4 Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `apply_claude_fix` now has 2 new required params — if any test calls it without them, it crashes | Medium | Add the two file-path params to all test call sites. The existing `test_refactor.py` doesn't call `apply_claude_fix` directly. |
| Circular import: `lifecycle.py` imports from `utils.py` (already imports), imports `load_finding_record` from `state.py` — no cycle | N/A | Verified: `state.py` → `models.py`, `utils.py` → `constants.py`. `lifecycle.py` → `state.py`, `utils.py`. No cycle. |
| `increment_fix_attempt` called inside the `finally` block of `apply_claude_fix` might not run if `render_claude_fix_prompt` raises | Low | `increment_fix_attempt` is called after `subprocess.run`, which is outside the try block for prompt rendering. It runs regardless of prompt write success. |
| `findings_file` or `lessons_file` passed as `Path('/dev/null')` or empty path — `update_finding_record` handles missing files but caller may not expect `False` | Low | Both functions return `False` for missing files and no-op gracefully. Log already has entries for the fix result. |

**Detection**: After Phase 4, run full `test_refactor.py` and `test_directive_seeding.py`. Also run a dry-run of the CLI to confirm the orchestrator doesn't crash on startup.

### Phase 5 Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| E2E test uses `tempfile.TemporaryDirectory` — not runnable against a real repo | Intentional | This is a smoke test, not a full integration test against a live sandbox. Run manually with `--repo-path` pointing to a real sandbox repo. |

---

*Document version: 1.0 — aligned with `DIRECTIVE_SEEDING_DESIGN.md` dated 2026-03-25.*
