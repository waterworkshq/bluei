#!/usr/bin/env python3
"""Tests for Batch PR grouping engine (Phase 1).

Covers: rule matching, isolation, chunking, conflict detection,
        full grouping pipeline, solo fallback, and state persistence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.sandbox_local_runner.models import (
    BatchGroup, BatchRule, BatchStatus, Finding, FixResult,
)
from core.sandbox_local_runner.batch_pr import (
    load_batch_rules, rule_matches, is_isolated, chunk_findings,
    group_findings_for_batch, check_batch_conflicts,
)
from core.sandbox_local_runner.state import (
    load_batches, save_batch_record, update_batch_record,
)


# ── Helpers ──────────────────────────────────────────────────────

def make_finding(**overrides) -> Finding:
    """Create a Finding with sensible defaults."""
    defaults = {
        'finding_id': 'f001',
        'repo': 'test-repo',
        'path': 'zerver/lib/message.py',
        'line': 42,
        'rule': 'ruff-c408',
        'snippet': 'dict(a=1)',
        'confidence': 0.72,
        'quick_win': True,
        'safe_to_autofix': True,
    }
    defaults.update(overrides)
    return Finding(**defaults)


def make_issue(finding_id: str = 'f001', **overrides) -> Dict[str, Any]:
    """Create a minimal issue dict."""
    defaults = {
        'finding_id': finding_id,
        'issue_id': f'ISS-{finding_id[:4]}',
        'status': 'open',
        'github': {'issue_number': 1},
    }
    defaults.update(overrides)
    return defaults


def make_candidate(fid='f001', rule='ruff-c408', path='zerver/lib/message.py', line=42):
    """Create a (issue, finding) candidate tuple."""
    finding = make_finding(finding_id=fid, rule=rule, path=path, line=line)
    issue = make_issue(finding_id=fid)
    return (issue, finding)


# ── Rule Matching ────────────────────────────────────────────────

class TestRuleMatches:
    def test_exact_match(self):
        assert rule_matches('ruff-c408', 'ruff-c408') is True

    def test_exact_no_match(self):
        assert rule_matches('ruff-c408', 'ruff-b904') is False

    def test_wildcard_match(self):
        assert rule_matches('ruff-c408', 'ruff-*') is True

    def test_wildcard_no_match(self):
        assert rule_matches('pylint-c408', 'ruff-*') is False

    def test_empty_pattern(self):
        assert rule_matches('ruff-c408', '') is False


# ── Isolation ────────────────────────────────────────────────────

class TestIsIsolated:
    def test_migrations_excluded(self):
        f = make_finding(path='zerver/migrations/0001_initial.py')
        config = {'file_patterns': ['**/migrations/*.py']}
        assert is_isolated(f, config) is True

    def test_normal_file_not_isolated(self):
        f = make_finding(path='zerver/lib/message.py')
        config = {'file_patterns': ['**/migrations/*.py']}
        assert is_isolated(f, config) is False

    def test_middleware_excluded(self):
        f = make_finding(path='zerver/middleware/logging.py')
        config = {'file_patterns': ['**/middleware*.py']}
        assert is_isolated(f, config) is True

    def test_empty_isolation_config(self):
        f = make_finding()
        assert is_isolated(f, {}) is False

    def test_no_file_patterns(self):
        f = make_finding()
        assert is_isolated(f, {'other_key': 'value'}) is False


# ── Chunking ─────────────────────────────────────────────────────

class TestChunkFindings:
    def test_single_chunk_under_limit(self):
        findings = [make_finding(finding_id=f'f{i:03d}') for i in range(5)]
        rule = BatchRule(rule_pattern='ruff-c408', max_batch_size=10)
        chunks = chunk_findings(findings, rule)
        assert len(chunks) == 1
        assert len(chunks[0]) == 5

    def test_splits_at_max_batch_size(self):
        findings = [make_finding(finding_id=f'f{i:03d}') for i in range(25)]
        rule = BatchRule(rule_pattern='ruff-c408', max_batch_size=10)
        chunks = chunk_findings(findings, rule)
        assert len(chunks) == 3  # 10 + 10 + 5
        assert len(chunks[0]) == 10
        assert len(chunks[1]) == 10
        assert len(chunks[2]) == 5

    def test_splits_at_max_files(self):
        findings = [
            make_finding(finding_id=f'f{i:03d}', path=f'zerver/file_{i}.py')
            for i in range(8)
        ]
        rule = BatchRule(rule_pattern='ruff-c408', max_batch_size=20, max_files_per_batch=5)
        chunks = chunk_findings(findings, rule)
        assert len(chunks) == 2  # 5 files + 3 files
        assert len(chunks[0]) == 5
        assert len(chunks[1]) == 3

    def test_empty_findings(self):
        rule = BatchRule(rule_pattern='ruff-c408')
        assert chunk_findings([], rule) == []

    def test_single_finding(self):
        findings = [make_finding()]
        rule = BatchRule(rule_pattern='ruff-c408')
        chunks = chunk_findings(findings, rule)
        assert len(chunks) == 1
        assert len(chunks[0]) == 1

    # ── Dynamic batch sizing ─────────────────────────────────────

    def test_high_density_many_findings_few_files_one_batch(self):
        """20 findings in 5 files → high density → large batch, all in one chunk."""
        paths = [f'zerver/file_{i % 5}.py' for i in range(20)]
        findings = [
            make_finding(finding_id=f'f{i:03d}', path=paths[i])
            for i in range(20)
        ]
        rule = BatchRule(rule_pattern='ruff-c408', max_batch_size=20, max_files_per_batch=15)
        # auto_batch: 5/20=0.25 → int(15/0.25)=60 → clamped 30 → effective=min(30,20)=20
        # All 20 findings fit in one chunk (20 ≤ 20)
        chunks = chunk_findings(findings, rule)
        assert len(chunks) == 1
        assert len(chunks[0]) == 20

    def test_low_density_many_findings_many_files_smaller_batches(self):
        """20 findings in 20 unique files → low density → smaller batches."""
        findings = [
            make_finding(finding_id=f'f{i:03d}', path=f'zerver/file_{i}.py')
            for i in range(20)
        ]
        rule = BatchRule(rule_pattern='ruff-c408', max_batch_size=20, max_files_per_batch=15)
        # auto_batch: 20/20=1.0 → int(15/1.0)=15 → effective=min(15,20)=15
        # 20 findings with batch_size=15 → splits into [15, 5]
        chunks = chunk_findings(findings, rule)
        assert len(chunks) == 2
        assert len(chunks[0]) == 15
        assert len(chunks[1]) == 5

    def test_all_findings_one_file_high_auto_capped(self):
        """10 findings in 1 file → auto_batch very high, capped by max_batch_size."""
        findings = [
            make_finding(finding_id=f'f{i:03d}', path='zerver/same.py')
            for i in range(10)
        ]
        rule = BatchRule(rule_pattern='ruff-c408', max_batch_size=20, max_files_per_batch=15)
        # auto_batch: 1/10=0.1 → int(15/0.1)=150 → clamped 30 → effective=min(30,20)=20
        # 10 findings ≤ 20, all fit in one chunk
        chunks = chunk_findings(findings, rule)
        assert len(chunks) == 1
        assert len(chunks[0]) == 10

    def test_each_finding_unique_file_auto_equals_max_files(self):
        """30 findings in 30 unique files → auto_batch = max_files_per_batch."""
        findings = [
            make_finding(finding_id=f'f{i:03d}', path=f'zerver/unique_{i}.py')
            for i in range(30)
        ]
        rule = BatchRule(rule_pattern='ruff-c408', max_batch_size=30, max_files_per_batch=15)
        # auto_batch: 30/30=1.0 → int(15/1.0)=15 → effective=min(15,30)=15
        # 30 findings with batch_size=15 → splits into [15, 15]
        chunks = chunk_findings(findings, rule)
        assert len(chunks) == 2
        assert all(len(c) == 15 for c in chunks)


# ── Conflict Detection ───────────────────────────────────────────

class TestCheckBatchConflicts:
    def test_no_conflicts_different_files(self):
        f1 = make_finding(finding_id='f1', path='a.py', line=10)
        f2 = make_finding(finding_id='f2', path='b.py', line=12)
        conflicts = check_batch_conflicts([f1, f2])
        assert len(conflicts) == 0

    def test_conflict_same_file_nearby_lines(self):
        f1 = make_finding(finding_id='f1', path='a.py', line=10)
        f2 = make_finding(finding_id='f2', path='a.py', line=13)
        conflicts = check_batch_conflicts([f1, f2])
        assert len(conflicts) == 1
        assert conflicts[0][0].finding_id == 'f1'
        assert conflicts[0][1].finding_id == 'f2'

    def test_no_conflict_same_file_far_lines(self):
        f1 = make_finding(finding_id='f1', path='a.py', line=10)
        f2 = make_finding(finding_id='f2', path='a.py', line=20)
        conflicts = check_batch_conflicts([f1, f2])
        assert len(conflicts) == 0

    def test_empty_list(self):
        assert check_batch_conflicts([]) == []

    def test_multiple_conflicts(self):
        f1 = make_finding(finding_id='f1', path='a.py', line=10)
        f2 = make_finding(finding_id='f2', path='a.py', line=12)
        f3 = make_finding(finding_id='f3', path='a.py', line=14)
        conflicts = check_batch_conflicts([f1, f2, f3])
        assert len(conflicts) == 2  # (f1,f2) and (f2,f3)


# ── Full Grouping Pipeline ──────────────────────────────────────

class TestGroupFindingsForBatch:
    def test_groups_same_rule_into_one_batch(self):
        """10 ruff-c408 findings → 1 batch."""
        candidates = [
            make_candidate(fid=f'f{i:03d}', rule='ruff-c408', path=f'zerver/file_{i}.py')
            for i in range(10)
        ]
        rules = [BatchRule(rule_pattern='ruff-c408', enabled=True, group_by='rule',
                           max_batch_size=20, priority=1)]
        batches = group_findings_for_batch(candidates, rules)
        assert len(batches) == 1
        assert len(batches[0].findings) == 10
        assert batches[0].group_by == 'rule'
        assert batches[0].rule_pattern == 'ruff-c408'
        assert not batches[0].is_solo

    def test_isolation_excludes_migrations(self):
        """Migration file findings become solo batches."""
        candidates = [
            make_candidate(fid='f001', rule='ruff-c408', path='zerver/migrations/0001_initial.py'),
            make_candidate(fid='f002', rule='ruff-c408', path='zerver/lib/message.py'),
            make_candidate(fid='f003', rule='ruff-c408', path='zerver/views/home.py'),
        ]
        rules = [BatchRule(
            rule_pattern='ruff-c408', enabled=True, group_by='rule',
            max_batch_size=20, priority=1,
            isolation={'file_patterns': ['**/migrations/*.py']},
        )]
        batches = group_findings_for_batch(candidates, rules)
        # f002 and f003 batched together; f001 solo
        assert len(batches) == 2
        solo = [b for b in batches if b.is_solo]
        multi = [b for b in batches if not b.is_solo]
        assert len(solo) == 1
        assert len(multi) == 1
        assert solo[0].findings[0].path == 'zerver/migrations/0001_initial.py'
        assert len(multi[0].findings) == 2

    def test_chunking_splits_oversized_batches(self):
        """25 findings with max 10 → 3 batches (10+10+5)."""
        candidates = [
            make_candidate(fid=f'f{i:03d}', rule='ruff-c408', path=f'zerver/file_{i}.py')
            for i in range(25)
        ]
        rules = [BatchRule(rule_pattern='ruff-c408', enabled=True, group_by='rule',
                           max_batch_size=10, priority=1)]
        batches = group_findings_for_batch(candidates, rules)
        assert len(batches) == 3
        assert len(batches[0].findings) == 10
        assert len(batches[1].findings) == 10
        assert len(batches[2].findings) == 5

    def test_solo_fallback_for_non_batchable(self):
        """Findings with no matching rule become solo batches."""
        candidates = [
            make_candidate(fid='f001', rule='some-unknown-rule', path='a.py'),
        ]
        rules = [BatchRule(rule_pattern='ruff-c408', enabled=True, priority=1)]
        batches = group_findings_for_batch(candidates, rules)
        assert len(batches) == 1
        assert batches[0].is_solo
        assert batches[0].group_by == 'solo'

    def test_disabled_rules_skipped(self):
        """Disabled rules don't match anything."""
        candidates = [
            make_candidate(fid='f001', rule='ruff-c408', path='a.py'),
        ]
        rules = [BatchRule(rule_pattern='ruff-c408', enabled=False, priority=1)]
        batches = group_findings_for_batch(candidates, rules)
        assert len(batches) == 1
        assert batches[0].is_solo  # Falls through to solo

    def test_multiple_rules(self):
        """Different rules get separate batches."""
        candidates = [
            make_candidate(fid='f001', rule='ruff-c408', path='a.py'),
            make_candidate(fid='f002', rule='ruff-c408', path='b.py'),
            make_candidate(fid='f003', rule='ruff-b904', path='c.py'),
            make_candidate(fid='f004', rule='ruff-b904', path='d.py'),
        ]
        rules = [
            BatchRule(rule_pattern='ruff-c408', enabled=True, group_by='rule',
                      max_batch_size=20, priority=1),
            BatchRule(rule_pattern='ruff-b904', enabled=True, group_by='rule',
                      max_batch_size=20, priority=2),
        ]
        batches = group_findings_for_batch(candidates, rules)
        assert len(batches) == 2
        c408_batch = [b for b in batches if b.rule_pattern == 'ruff-c408'][0]
        b904_batch = [b for b in batches if b.rule_pattern == 'ruff-b904'][0]
        assert len(c408_batch.findings) == 2
        assert len(b904_batch.findings) == 2

    def test_wildcard_rule_catches_remaining(self):
        """Catch-all ruff-* rule matches findings not matched by specific rules."""
        candidates = [
            make_candidate(fid='f001', rule='ruff-xyz999', path='a.py'),
            make_candidate(fid='f002', rule='ruff-abc123', path='b.py'),
        ]
        rules = [
            BatchRule(rule_pattern='ruff-c408', enabled=True, group_by='rule', priority=1),
            BatchRule(rule_pattern='ruff-*', enabled=True, group_by='rule',
                      max_batch_size=20, priority=99),
        ]
        batches = group_findings_for_batch(candidates, rules)
        # Both caught by ruff-* wildcard, batched together
        assert len(batches) == 1
        assert len(batches[0].findings) == 2

    def test_empty_candidates(self):
        batches = group_findings_for_batch([], [BatchRule(rule_pattern='ruff-c408')])
        assert batches == []


# ── BatchGroup Data Model ───────────────────────────────────────

class TestBatchGroupModel:
    def test_from_solo(self):
        f = make_finding()
        issue = make_issue()
        bg = BatchGroup.from_solo(issue, f)
        assert bg.is_solo
        assert bg.group_by == 'solo'
        assert bg.batch_id.startswith('solo-')
        assert len(bg.findings) == 1

    def test_from_findings(self):
        findings = [make_finding(finding_id=f'f{i:03d}') for i in range(3)]
        issues_map = {f.finding_id: make_issue(finding_id=f.finding_id) for f in findings}
        rule = BatchRule(rule_pattern='ruff-c408', group_by='rule',
                         max_files_per_batch=15, max_loc_per_batch=500)
        bg = BatchGroup.from_findings(findings, issues_map, rule)
        assert not bg.is_solo
        assert bg.group_by == 'rule'
        assert bg.batch_id.startswith('batch-')
        assert len(bg.findings) == 3
        assert len(bg.issues) == 3

    def test_file_count(self):
        findings = [
            make_finding(finding_id='f1', path='a.py'),
            make_finding(finding_id='f2', path='b.py'),
            make_finding(finding_id='f3', path='a.py'),  # duplicate
        ]
        issues_map = {f.finding_id: make_issue(f.finding_id) for f in findings}
        rule = BatchRule(rule_pattern='ruff-c408')
        bg = BatchGroup.from_findings(findings, issues_map, rule)
        assert bg.file_count == 2

    def test_pr_title_solo(self):
        f = make_finding()
        issue = make_issue()
        bg = BatchGroup.from_solo(issue, f)
        title = bg.pr_title()
        assert 'ruff-c408' in title
        assert 'message.py' in title

    def test_pr_title_batch(self):
        findings = [make_finding(finding_id=f'f{i:03d}') for i in range(5)]
        issues_map = {f.finding_id: make_issue(f.finding_id) for f in findings}
        rule = BatchRule(rule_pattern='ruff-c408')
        bg = BatchGroup.from_findings(findings, issues_map, rule)
        title = bg.pr_title()
        assert '5' in title
        assert 'ruff-c408' in title

    def test_pr_body_solo(self):
        f = make_finding()
        issue = make_issue()
        bg = BatchGroup.from_solo(issue, f)
        body = bg.pr_body()
        assert 'message.py' in body
        assert '42' in body

    def test_pr_body_batch(self):
        findings = [make_finding(finding_id=f'f{i:03d}', path=f'file_{i}.py') for i in range(3)]
        issues_map = {f.finding_id: make_issue(f.finding_id) for f in findings}
        rule = BatchRule(rule_pattern='ruff-c408')
        bg = BatchGroup.from_findings(findings, issues_map, rule)
        body = bg.pr_body()
        assert '3' in body
        assert 'file_0.py' in body

    def test_to_record(self):
        f = make_finding()
        issue = make_issue()
        bg = BatchGroup.from_solo(issue, f)
        record = bg.to_record()
        assert record['batch_id'] == bg.batch_id
        assert record['group_by'] == 'solo'
        assert len(record['findings']) == 1


# ── BatchStatus & FixResult ─────────────────────────────────────

class TestBatchStatusEnum:
    def test_all_statuses(self):
        expected = {'open', 'fixing', 'fixing_partial', 'pr_created',
                    'pr_merged', 'failed', 'split', 'aborted', 'dry_run'}
        assert set(s.value for s in BatchStatus) == expected


class TestFixResult:
    def test_defaults(self):
        fr = FixResult(finding_id='f1', status='success')
        assert fr.diff_lines == 0
        assert fr.error is None
        assert fr.fix_method == 'autofix'

    def test_with_error(self):
        fr = FixResult(finding_id='f1', status='failed', error='verification-failed')
        assert fr.error == 'verification-failed'


# ── State Persistence ───────────────────────────────────────────

class TestBatchStatePersistence:
    def test_load_empty(self, tmp_path):
        path = tmp_path / 'batches.jsonl'
        batches = load_batches(path)
        assert batches == []

    def test_save_and_load(self, tmp_path):
        path = tmp_path / 'batches.jsonl'
        record = {
            'batch_id': 'batch-001',
            'rule_pattern': 'ruff-c408',
            'status': 'open',
            'findings': [{'finding_id': 'f1', 'path': 'a.py', 'line': 10}],
        }
        save_batch_record(path, record)
        batches = load_batches(path)
        assert len(batches) == 1
        assert batches[0]['batch_id'] == 'batch-001'

    def test_save_multiple(self, tmp_path):
        path = tmp_path / 'batches.jsonl'
        for i in range(3):
            save_batch_record(path, {'batch_id': f'batch-{i}', 'status': 'open'})
        batches = load_batches(path)
        assert len(batches) == 3

    def test_update_record(self, tmp_path):
        path = tmp_path / 'batches.jsonl'
        save_batch_record(path, {'batch_id': 'batch-001', 'status': 'open'})
        save_batch_record(path, {'batch_id': 'batch-002', 'status': 'open'})

        updated = update_batch_record(path, 'batch-001', {'status': 'pr_created', 'pr_number': 42})
        assert updated is True

        batches = load_batches(path)
        b001 = [b for b in batches if b['batch_id'] == 'batch-001'][0]
        assert b001['status'] == 'pr_created'
        assert b001['pr_number'] == 42

        b002 = [b for b in batches if b['batch_id'] == 'batch-002'][0]
        assert b002['status'] == 'open'

    def test_update_nonexistent(self, tmp_path):
        path = tmp_path / 'batches.jsonl'
        save_batch_record(path, {'batch_id': 'batch-001', 'status': 'open'})
        result = update_batch_record(path, 'batch-999', {'status': 'failed'})
        assert result is False

    def test_update_missing_file(self, tmp_path):
        path = tmp_path / 'nonexistent.jsonl'
        result = update_batch_record(path, 'batch-001', {'status': 'failed'})
        assert result is False

    def test_malformed_lines_skipped(self, tmp_path):
        path = tmp_path / 'batches.jsonl'
        path.write_text("not json\n{bad\n")
        batches = load_batches(path)
        assert batches == []


# ── Load Batch Rules from YAML ──────────────────────────────────

class TestLoadBatchRules:
    def test_load_builtin_rules(self):
        rules_path = Path(__file__).resolve().parents[1] / 'core' / 'sandbox_local_runner' / 'batch_rules.yaml'
        if not rules_path.exists():
            pytest.skip('batch_rules.yaml not found')
        rules = load_batch_rules(rules_path)
        assert len(rules) >= 4
        # First rule is the cross-rule wildcard (disabled by default)
        assert rules[0].rule_pattern == 'ruff-*'
        assert rules[0].group_by == 'cross-rule'
        assert rules[0].enabled is False
        # Per-rule entries follow
        assert rules[1].rule_pattern == 'ruff-c408'
        assert rules[1].max_batch_size == 20
        assert rules[1].isolation == {'file_patterns': ['**/migrations/*.py']}

    def test_load_empty_yaml(self, tmp_path):
        path = tmp_path / 'rules.yaml'
        path.write_text('rules: []\n')
        rules = load_batch_rules(path)
        assert rules == []

    def test_load_no_rules_key(self, tmp_path):
        path = tmp_path / 'rules.yaml'
        path.write_text('other: stuff\n')
        rules = load_batch_rules(path)
        assert rules == []
