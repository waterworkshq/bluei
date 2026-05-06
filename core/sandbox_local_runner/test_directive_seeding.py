#!/usr/bin/env python3
"""test_directive_seeding.py — Tests for directive-seeding phases 1–3."""

import json
import tempfile
import sys
from pathlib import Path
from datetime import datetime, timezone

# Ensure the workspace root (parent of core/) is on sys.path
import os
_workdir = os.environ.get('WORKSPACE_ROOT')
if _workdir:
    sys.path.insert(0, _workdir)
else:
    # Fallback: go up three levels from this file to reach workspace root
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core.sandbox_local_runner.models import Finding
from core.sandbox_local_runner.state import (
    load_finding_record,
    update_finding_record,
    increment_fix_attempt,
)
from core.sandbox_local_runner.utils import (
    load_lessons_for_finding,
    load_recent_lessons,
    append_lesson,
)
from core.sandbox_local_runner.lifecycle import _should_use_mnemo


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
    f = _make_finding(fix_attempts=1, fix_success=True, last_fix_error='boom', last_fix_at=datetime.now(timezone.utc).isoformat())
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


def test_should_use_mnemo_bypasses_trivial_first_pass_quick_win():
    finding = Finding(
        finding_id='mnemo-trivial',
        repo='repo',
        path='src/test.py',
        line=1,
        rule='ruff-f401',
        snippet='import os',
        confidence=0.99,
        quick_win=True,
        safe_to_autofix=True,
    )
    ok, reason = _should_use_mnemo(finding, None, [])
    assert ok is False
    assert reason == 'trivial-first-pass-quick-win'


def test_should_use_mnemo_for_retry_attempts():
    finding = Finding(
        finding_id='mnemo-retry',
        repo='repo',
        path='src/test.py',
        line=1,
        rule='ruff-f401',
        snippet='import os',
        confidence=0.99,
        quick_win=True,
        safe_to_autofix=True,
    )
    ok, reason = _should_use_mnemo(finding, {'fix_attempts': 2}, [])
    assert ok is True
    assert reason == 'retry-attempts=2'


def test_should_use_mnemo_for_non_quick_win():
    finding = Finding(
        finding_id='mnemo-hard',
        repo='repo',
        path='src/test.py',
        line=1,
        rule='custom-complex',
        snippet='very complex multi-branch logic here',
        confidence=0.75,
        quick_win=False,
        safe_to_autofix=True,
    )
    ok, reason = _should_use_mnemo(finding, None, [])
    assert ok is True
    assert reason == 'not-quick-win'


# ─── Phase 2: utils.py lesson-load function tests ─────────────────────────

def _write_lessons(content: str) -> Path:
    """Helper: write content to a temp LESSONS_LOG.md and return the Path."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='LESSONS_LOG.md', delete=False
    )
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


# --- append_lesson with finding_id ---

def test_append_lesson_with_finding_id():
    with tempfile.TemporaryDirectory() as td:
        lessons_file = Path(td) / 'LESSONS_LOG.md'
        append_lesson(
            lessons_file,
            'fix-cycle',
            finding_id='abc123',
            what_broke='something broke',
            what_worked='something worked',
        )
        text = lessons_file.read_text()
        assert 'finding_id: abc123' in text
        assert 'fix-cycle' in text
        assert 'something broke' in text
        assert 'something worked' in text
        # Empty finding_id should NOT write the tag line
        append_lesson(
            lessons_file,
            'pr-cycle',
            finding_id='',
            what_changed='did a thing',
        )
        text2 = lessons_file.read_text()
        assert 'finding_id:' not in text2.split('pr-cycle')[1]


# --- load_lessons_for_finding ---

def test_load_lessons_for_finding_empty():
    """Empty/non-existent file returns empty list."""
    result = load_lessons_for_finding('any', Path('/tmp/no-such-file-12345.md'))
    assert result == []


def test_load_lessons_for_finding_no_match():
    """File with entries but none matching finding_id returns empty list."""
    content = """\n## 2026-03-25 | pr-cycle\n- **Broke:** something\n## 2026-03-24 | fix-cycle\n- **Worked:** something\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding('xyz789', p)
        assert result == []
    finally:
        p.unlink()


def test_load_lessons_for_finding_match():
    """Single matching entry is returned correctly."""
    content = """\n## 2026-03-25 | fix-cycle\nfinding_id: match-id-42\n- **Broke:** it crashed\n- **Changed:** updated dep\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding('match-id-42', p)
        assert len(result) == 1
        assert result[0]['date'] == '2026-03-25'
        assert result[0]['cycle_type'] == 'fix-cycle'
        assert result[0]['finding_id'] == 'match-id-42'
        assert result[0]['broke'] == 'it crashed'
        assert result[0]['changed'] == 'updated dep'
        assert result[0]['worked'] == ''
    finally:
        p.unlink()


def test_load_lessons_for_finding_multiple_entries_newest_first():
    """Multiple matching entries returned newest-first."""
    content = """\n## 2026-03-20 | pr-cycle\nfinding_id: fid-abc\n- **Worked:** old approach\n\n## 2026-03-22 | fix-cycle\nfinding_id: fid-other\n- **Worked:** other\n\n## 2026-03-25 | fix-cycle\nfinding_id: fid-abc\n- **Broke:** new failure\n- **Changed:** newer approach\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding('fid-abc', p)
        assert len(result) == 2
        # Newest first
        assert result[0]['date'] == '2026-03-25'
        assert result[0]['broke'] == 'new failure'
        assert result[1]['date'] == '2026-03-20'
        assert result[1]['worked'] == 'old approach'
    finally:
        p.unlink()


def test_load_lessons_for_finding_malformed():
    """Malformed lines are skipped without raising."""
    content = """\n## 2026-03-25 | fix-cycle\nfinding_id: fid-mal\nnot a valid line\n- **Broke:** valid break\n- **Changed:** !! invalid syntax here !!\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding('fid-mal', p)
        assert len(result) == 1
        assert result[0]['broke'] == 'valid break'
    finally:
        p.unlink()


def test_load_lessons_for_finding_no_finding_id_omitted():
    """Entry without finding_id tag is NOT returned (cannot be attributed)."""
    content = """\n## 2026-03-25 | fix-cycle\n- **Worked:** untagged entry\n\n## 2026-03-24 | fix-cycle\nfinding_id: fid-123\n- **Worked:** tagged entry\n"""
    p = _write_lessons(content)
    try:
        result = load_lessons_for_finding('fid-123', p)
        assert len(result) == 1
        assert result[0]['worked'] == 'tagged entry'
    finally:
        p.unlink()


# --- load_recent_lessons ---

def test_load_recent_lessons():
    """Returns most recent N entries newest-first, all finding_ids."""
    content = """\n## 2026-03-20 | pr-cycle\n- **Worked:** entry-1\n\n## 2026-03-22 | fix-cycle\nfinding_id: fid-2\n- **Broke:** entry-2\n\n## 2026-03-25 | fix-cycle\nfinding_id: fid-3\n- **Changed:** entry-3\n"""
    p = _write_lessons(content)
    try:
        result = load_recent_lessons(p, limit=3)
        assert len(result) == 3
        assert result[0]['date'] == '2026-03-25'
        assert result[0]['changed'] == 'entry-3'
        assert result[1]['date'] == '2026-03-22'
        assert result[1]['broke'] == 'entry-2'
        assert result[2]['date'] == '2026-03-20'
        assert result[2]['finding_id'] == ''
    finally:
        p.unlink()


def test_load_recent_lessons_fewer_available():
    """Returns all entries if fewer exist than limit."""
    content = """\n## 2026-03-25 | fix-cycle\n- **Worked:** only one\n"""
    p = _write_lessons(content)
    try:
        result = load_recent_lessons(p, limit=20)
        assert len(result) == 1
    finally:
        p.unlink()


def test_load_recent_lessons_limit_zero():
    """limit=0 returns empty list (no entries)."""
    content = """\n## 2026-03-25 | fix-cycle\n- **Worked:** something\n"""
    p = _write_lessons(content)
    try:
        result = load_recent_lessons(p, limit=0)
        assert result == []
    finally:
        p.unlink()


# ─── Phase 3: Exponential Backoff Cooldown ─────────────────────────────────

import tempfile as _tempfile
from pathlib import Path as _Path

from core.sandbox_local_runner.state import (
    get_effective_cooldown,
    mark_finding_activity,
    filter_findings_by_cooldown,
    MAX_COOLDOWN_SECONDS,
)
from core.sandbox_local_runner.models import Finding


def test_effective_cooldown_no_activity():
    """No activity entry → returns base cooldown (no errors)."""
    state = {'finding_activity': {}}
    result = get_effective_cooldown('fid-x', state, base_cooldown_seconds=3600)
    assert result == 3600


def test_effective_cooldown_zero_failures():
    """failure_count=0 → returns base cooldown."""
    state = {'finding_activity': {'fid-1': {'failure_count': 0, 'last_action': 'fix-attempt'}}}
    result = get_effective_cooldown('fid-1', state, base_cooldown_seconds=3600)
    assert result == 3600


def test_effective_cooldown_one_failure():
    """failure_count=1 → base * 2^1 = base * 2."""
    state = {'finding_activity': {'fid-1': {'failure_count': 1}}}
    result = get_effective_cooldown('fid-1', state, base_cooldown_seconds=3600)
    assert result == 7200


def test_effective_cooldown_exponential():
    """failure_count=3 → base * 2^3."""
    state = {'finding_activity': {'fid-1': {'failure_count': 3}}}
    result = get_effective_cooldown('fid-1', state, base_cooldown_seconds=3600)
    assert result == 3600 * 8  # 28800


def test_effective_cooldown_capped_at_7_days():
    """failure_count high enough to exceed 7-day cap → capped."""
    state = {'finding_activity': {'fid-1': {'failure_count': 10}}}
    result = get_effective_cooldown('fid-1', state, base_cooldown_seconds=3600)
    assert result == MAX_COOLDOWN_SECONDS
    assert result == 7 * 24 * 60 * 60


def test_mark_finding_activity_with_failure_count():
    """mark_finding_activity stores failure_count and last_error."""
    state = {'finding_activity': {}}
    mark_finding_activity(
        state, ['fid-1'], 'fix-attempt',
        failure_count=2, last_error='ruff failed',
    )
    entry = state['finding_activity']['fid-1']
    assert entry['failure_count'] == 2
    assert entry['last_error'] == 'ruff failed'
    assert entry['last_action'] == 'fix-attempt'
    assert 'last_action_at' in entry


def test_mark_finding_activity_resets_failure_count():
    """Existing entry fields are preserved when failure_count is updated."""
    state = {'finding_activity': {
        'fid-old': {
            'last_action': 'fix-attempt',
            'last_action_at': '2026-01-01T00:00:00Z',
            'failure_count': 5,
            'last_error': 'old error',
        }
    }}
    mark_finding_activity(
        state, ['fid-old'], 'fix-attempt',
        failure_count=0,
    )
    entry = state['finding_activity']['fid-old']
    assert entry['failure_count'] == 0
    # Previous fields preserved:
    assert entry['last_error'] == 'old error'


def test_filter_finds_suppressed_with_extended_cooldown():
    """Findings with failure_count get extended cooldown via get_effective_cooldown."""
    with _tempfile.TemporaryDirectory() as td:
        log_file = _Path(td) / 'runner.log'

        # State: finding has failure_count=1, making effective cooldown 2x
        state = {
            'finding_activity': {
                'fid-extended': {
                    'last_action': 'fix-attempt',
                    'last_action_at': '2026-03-25T17:00:00Z',
                    'failure_count': 1,
                }
            }
        }

        findings = [
            Finding(
                finding_id='fid-extended',
                repo='r',
                path='p.py',
                line=1,
                rule='R001',
                snippet='x=1',
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ]

        # Base cooldown = 3600s; effective = 7200s (2h) due to failure_count=1.
        # Current time is ~18:09 on 2026-03-25 (see test context).
        # Elapsed from 17:00 to 18:09 = ~69 min = ~4140s.
        # Since 4140 < 7200, it MUST be suppressed.
        allowed, suppressed = filter_findings_by_cooldown(
            findings, state, cooldown_seconds=3600, log_file=log_file
        )
        assert allowed == []
        assert len(suppressed) == 1
        assert suppressed[0].finding_id == 'fid-extended'


# ─── Phase 4: Prompt injection tests ───────────────────────────────────────

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
        # Phase 2: append_lesson + load_lessons_for_finding + load_recent_lessons
        test_append_lesson_with_finding_id,
        test_load_lessons_for_finding_empty,
        test_load_lessons_for_finding_no_match,
        test_load_lessons_for_finding_match,
        test_load_lessons_for_finding_multiple_entries_newest_first,
        test_load_lessons_for_finding_malformed,
        test_load_lessons_for_finding_no_finding_id_omitted,
        test_load_recent_lessons,
        test_load_recent_lessons_fewer_available,
        test_load_recent_lessons_limit_zero,
        # Phase 3: Exponential Backoff Cooldown
        test_effective_cooldown_no_activity,
        test_effective_cooldown_zero_failures,
        test_effective_cooldown_one_failure,
        test_effective_cooldown_exponential,
        test_effective_cooldown_capped_at_7_days,
        test_mark_finding_activity_with_failure_count,
        test_mark_finding_activity_resets_failure_count,
        test_filter_finds_suppressed_with_extended_cooldown,
        # Phase 4: Prompt injection
        test_prompt_has_prior_context_when_history_exists,
        test_prompt_omits_prior_context_when_empty,
        test_prompt_omits_prior_context_when_none,
        test_prompt_has_fix_history_when_attempts_gt_zero,
        test_prompt_omits_fix_history_when_attempts_zero,
        test_prompt_omits_fix_history_when_record_none,
        test_prompt_has_both_sections_when_both_provided,
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
