#!/usr/bin/env python3
"""Tests for Batch PR split/recovery engine (Phase 3).

Covers: handle_batch_failure, split_batch, commit_partial_batch,
        split_on_conflicts, should_split_batch, recover_interrupted_batch,
        and the split wiring in process_batch.
"""

from __future__ import annotations

import json
import os
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, call

import pytest

from core.sandbox_local_runner.models import (
    BatchGroup, BatchRule, BatchStatus, Finding, FixResult,
)
from core.sandbox_local_runner.batch_pr import (
    commit_partial_batch,
    handle_batch_failure,
    process_batch,
    recover_interrupted_batch,
    should_split_batch,
    split_batch,
    split_on_conflicts,
)


# ── Helpers ──────────────────────────────────────────────────────

def make_finding(finding_id: str = 'f001', rule: str = 'ruff-c408',
                 path: str = 'zerver/lib/message.py',
                 line: int = 42, **overrides) -> Finding:
    defaults = {
        'finding_id': finding_id,
        'repo': 'test-repo',
        'path': path,
        'line': line,
        'rule': rule,
        'snippet': 'dict()',
        'confidence': 0.72,
        'quick_win': True,
        'safe_to_autofix': True,
    }
    defaults.update(overrides)
    return Finding(**defaults)


def make_batch(findings: List[Finding],
               fix_results: Optional[Dict[str, Any]] = None,
               status: str = 'open',
               retry_count: int = 0,
               split_history: Optional[List] = None,
               issues: Optional[List[dict]] = None,
               branch: Optional[str] = None,
               worktree_path: Optional[Path] = None,
               rule_pattern: str = 'ruff-c408',
               group_by: str = 'rule',
               ) -> BatchGroup:
    if issues is None:
        issues = [{'finding_id': f.finding_id, 'id': f'ISS-{i}'}
                  for i, f in enumerate(findings)]
    if fix_results is None:
        fix_results = {f.finding_id: FixResult(
            finding_id=f.finding_id, status='success', diff_lines=1)
            for f in findings}
    batch = BatchGroup(
        batch_id='batch-test-001',
        rule_pattern=rule_pattern,
        group_by=group_by,
        findings=findings,
        issues=issues,
        status=status,
        worktree_path=worktree_path,
        branch=branch,
        fix_results=fix_results,
        retry_count=retry_count,
        split_history=split_history or [],
    )
    return batch


# ── should_split_batch tests ─────────────────────────────────────

class TestShouldSplitBatch:
    def test_no_failures_returns_false(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(5)]
        fix_results = {f.finding_id: FixResult(finding_id=f.finding_id,
                                                 status='success')
                      for f in findings}
        batch = make_batch(findings, fix_results)
        assert should_split_batch(batch) is False

    def test_49_percent_failure_returns_false(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(100)]
        fix_results = {}
        for i, f in enumerate(findings):
            fix_results[f.finding_id] = FixResult(
                finding_id=f.finding_id,
                status='failed' if i < 49 else 'success',
            )
        batch = make_batch(findings, fix_results)
        assert should_split_batch(batch) is False

    def test_51_percent_failure_returns_true(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(100)]
        fix_results = {}
        for i, f in enumerate(findings):
            fix_results[f.finding_id] = FixResult(
                finding_id=f.finding_id,
                status='failed' if i < 51 else 'success',
            )
        batch = make_batch(findings, fix_results)
        assert should_split_batch(batch) is True

    def test_60_percent_failure_returns_true(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(10)]
        fix_results = {}
        for i, f in enumerate(findings):
            fix_results[f.finding_id] = FixResult(
                finding_id=f.finding_id,
                status='failed' if i < 6 else 'success',
            )
        batch = make_batch(findings, fix_results)
        assert should_split_batch(batch) is True

    def test_max_depth_respected(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(10)]
        fix_results = {f.finding_id: FixResult(finding_id=f.finding_id,
                                                status='failed')
                      for f in findings}
        batch = make_batch(findings, fix_results, retry_count=3)
        assert should_split_batch(batch, max_depth=3) is False

    def test_at_max_depth_minus_one_returns_true(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(10)]
        fix_results = {f.finding_id: FixResult(finding_id=f.finding_id,
                                                status='failed')
                      for f in findings}
        batch = make_batch(findings, fix_results, retry_count=2)
        assert should_split_batch(batch, max_depth=3) is True

    def test_skipped_not_counted_as_failure(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(10)]
        fix_results = {}
        for i, f in enumerate(findings):
            status = 'skipped' if i < 5 else 'success'
            fix_results[f.finding_id] = FixResult(finding_id=f.finding_id,
                                                   status=status)
        batch = make_batch(findings, fix_results)
        assert should_split_batch(batch) is False

    def test_dict_fix_results_supported(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(10)]
        fix_results = {}
        for i, f in enumerate(findings):
            fix_results[f.finding_id] = {
                'status': 'failed' if i < 6 else 'success',
                'finding_id': f.finding_id,
            }
        batch = make_batch(findings, fix_results)
        assert should_split_batch(batch) is True


# ── split_batch tests ────────────────────────────────────────────

class TestSplitBatch:
    def test_split_single_failure_to_solo(self):
        findings = [make_finding(finding_id='f001')]
        fix_results = {'f001': FixResult(finding_id='f001', status='failed')}
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        sub_batches = split_batch(batch, Path('/tmp/repo'), mock_args, log_file)

        assert len(sub_batches) == 1
        assert sub_batches[0].is_solo is True
        assert sub_batches[0].findings[0].finding_id == 'f001'

    def test_split_two_failures_to_two_solos(self):
        findings = [make_finding(finding_id='f001'), make_finding(finding_id='f002')]
        fix_results = {
            'f001': FixResult(finding_id='f001', status='failed'),
            'f002': FixResult(finding_id='f002', status='failed'),
        }
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        sub_batches = split_batch(batch, Path('/tmp/repo'), mock_args, log_file)

        assert len(sub_batches) == 2
        assert all(sb.is_solo for sb in sub_batches)

    def test_split_six_failures_to_halves(self):
        findings = [make_finding(finding_id=f'f{i:03d}') for i in range(6)]
        fix_results = {f.finding_id: FixResult(finding_id=f.finding_id,
                                               status='failed')
                      for f in findings}
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        sub_batches = split_batch(batch, Path('/tmp/repo'), mock_args, log_file)

        # 6 failures → halves of ~3 each
        assert len(sub_batches) == 2
        total_findings = sum(len(sb.findings) for sb in sub_batches)
        assert total_findings == 6

    def test_split_respects_max_depth(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(10)]
        fix_results = {f.finding_id: FixResult(finding_id=f.finding_id,
                                               status='failed')
                      for f in findings}
        batch = make_batch(findings, fix_results, retry_count=3)
        batch.batch_id = 'batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        sub_batches = split_batch(batch, Path('/tmp/repo'), mock_args, log_file)

        assert sub_batches == []

    def test_split_increments_retry_count(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(5)]
        fix_results = {f.finding_id: FixResult(finding_id=f.finding_id,
                                               status='failed')
                      for f in findings}
        batch = make_batch(findings, fix_results, retry_count=1)
        batch.batch_id = 'batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        sub_batches = split_batch(batch, Path('/tmp/repo'), mock_args, log_file)

        for sb in sub_batches:
            assert sb.retry_count == 2

    def test_split_batch_id_inherits_suffix(self):
        findings = [make_finding(finding_id='f001')]
        fix_results = {'f001': FixResult(finding_id='f001', status='failed')}
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-abc123'
        batch.split_history = []
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        sub_batches = split_batch(batch, Path('/tmp/repo'), mock_args, log_file)

        assert len(sub_batches) == 1
        assert 'batch-abc123' in sub_batches[0].batch_id

    def test_split_all_success_returns_empty(self):
        findings = [make_finding(finding_id=f'f{i}') for i in range(5)]
        fix_results = {f.finding_id: FixResult(finding_id=f.finding_id,
                                               status='success')
                      for f in findings}
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        sub_batches = split_batch(batch, Path('/tmp/repo'), mock_args, log_file)

        assert sub_batches == []


# ── split_on_conflicts tests ─────────────────────────────────────

class TestSplitOnConflicts:
    def test_no_conflicts_returns_original_batch(self):
        findings = [
            make_finding(finding_id='f001', path='a.py', line=10),
            make_finding(finding_id='f002', path='b.py', line=20),
        ]
        batch = make_batch(findings)
        groups = split_on_conflicts(batch)
        assert len(groups) == 1
        assert groups[0] is batch

    def test_conflict_detection_and_split(self):
        # Two findings within 5 lines in same file → conflict
        findings = [
            make_finding(finding_id='f001', path='a.py', line=10),
            make_finding(finding_id='f002', path='a.py', line=12),
            make_finding(finding_id='f003', path='b.py', line=50),
        ]
        batch = make_batch(findings)
        groups = split_on_conflicts(batch)
        # f001 and f002 conflict → f003 is non-conflicting
        assert len(groups) >= 2
        all_findings = [f for g in groups for f in g.findings]
        assert len(all_findings) == 3

    def test_nearby_findings_split_correctly(self):
        # Three findings in same file at lines 10, 11, 12 → all conflict
        findings = [
            make_finding(finding_id='f001', path='a.py', line=10),
            make_finding(finding_id='f002', path='a.py', line=11),
            make_finding(finding_id='f003', path='a.py', line=12),
        ]
        batch = make_batch(findings)
        groups = split_on_conflicts(batch)
        # All 3 are conflicting → 3 solo batches
        all_solos = [g for g in groups if g.is_solo]
        assert len(all_solos) == 3

    def test_non_conflicting_findings_grouped_together(self):
        # Two findings in same file far apart → no conflicts
        # They should remain in a single batch (not split)
        findings = [
            make_finding(finding_id='f001', path='a.py', line=10),
            make_finding(finding_id='f002', path='a.py', line=200),
        ]
        batch = make_batch(findings)
        groups = split_on_conflicts(batch)
        # No conflicts → original batch returned unchanged (len=1)
        assert len(groups) == 1
        assert groups[0] is batch
        assert not groups[0].is_solo  # 2 findings → not solo


# ── handle_batch_failure tests ─────────────────────────────────────

class TestHandleBatchFailure:
    def test_separates_successes_from_failures(self):
        findings = [
            make_finding(finding_id='f001'),
            make_finding(finding_id='f002'),
            make_finding(finding_id='f003'),
            make_finding(finding_id='f004'),
        ]
        fix_results = {
            'f001': FixResult(finding_id='f001', status='success'),
            'f002': FixResult(finding_id='f002', status='success'),
            'f003': FixResult(finding_id='f003', status='failed'),
            'f004': FixResult(finding_id='f004', status='failed'),
        }
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-test'
        batch.worktree_path = Path('/tmp/worktree')
        batch.branch = 'qa/batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.batch_pr.commit_partial_batch',
                   return_value=True) as mock_commit:
            sub_batches = handle_batch_failure(batch, Path('/tmp/repo'),
                                                mock_args, log_file)

        mock_commit.assert_called_once()
        # 2 successes → split 2 failures into solos
        assert len(sub_batches) == 2
        assert all(sb.is_solo for sb in sub_batches)

    def test_split_history_recorded(self):
        findings = [
            make_finding(finding_id='f001'),
            make_finding(finding_id='f002'),
        ]
        fix_results = {
            'f001': FixResult(finding_id='f001', status='success'),
            'f002': FixResult(finding_id='f002', status='failed'),
        }
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-test'
        batch.worktree_path = Path('/tmp/worktree')
        batch.branch = 'qa/batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.batch_pr.commit_partial_batch',
                   return_value=True):
            sub_batches = handle_batch_failure(batch, Path('/tmp/repo'),
                                                mock_args, log_file)

        assert len(batch.split_history) == 1
        entry = batch.split_history[0]
        assert entry['successful_count'] == 1
        assert entry['failed_count'] == 1
        assert entry['sub_batches_created'] == 1
        assert entry['reason'] == 'too_many_fix_failures'

    def test_no_failures_returns_empty_sub_batches(self):
        findings = [
            make_finding(finding_id='f001'),
            make_finding(finding_id='f002'),
        ]
        fix_results = {
            'f001': FixResult(finding_id='f001', status='success'),
            'f002': FixResult(finding_id='f002', status='success'),
        }
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-test'
        batch.worktree_path = Path('/tmp/worktree')
        batch.branch = 'qa/batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.batch_pr.commit_partial_batch',
                   return_value=True):
            sub_batches = handle_batch_failure(batch, Path('/tmp/repo'),
                                                mock_args, log_file)

        # No failures → no sub-batches
        assert sub_batches == []

    def test_all_failures_no_commit(self):
        findings = [
            make_finding(finding_id='f001'),
            make_finding(finding_id='f002'),
        ]
        fix_results = {
            'f001': FixResult(finding_id='f001', status='failed'),
            'f002': FixResult(finding_id='f002', status='failed'),
        }
        batch = make_batch(findings, fix_results)
        batch.batch_id = 'batch-test'
        batch.worktree_path = Path('/tmp/worktree')
        batch.branch = 'qa/batch-test'
        mock_args = MagicMock()
        mock_args.max_split_depth = 3
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.batch_pr.commit_partial_batch',
                   return_value=False) as mock_commit:
            sub_batches = handle_batch_failure(batch, Path('/tmp/repo'),
                                                mock_args, log_file)

        # No successful findings → commit_partial_batch not called
        mock_commit.assert_not_called()
        # 2 failures → 2 solo sub-batches
        assert len(sub_batches) == 2


# ── commit_partial_batch tests ────────────────────────────────────

class TestCommitPartialBatch:
    def test_commits_successful_files(self):
        findings = [
            make_finding(finding_id='f001', path='a.py'),
            make_finding(finding_id='f002', path='a.py'),
        ]
        batch = make_batch(findings)
        batch.worktree_path = Path('/tmp/worktree')
        batch.branch = 'qa/batch'
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.utils.run_no_capture') as mock_add, \
             patch('core.sandbox_local_runner.lifecycle.git_commit_all',
                   return_value='committed') as mock_commit, \
             patch('core.sandbox_local_runner.lifecycle.git_push_branch',
                   return_value=True) as mock_push:
            ok = commit_partial_batch(findings, batch, log_file)

        assert ok is True
        # Both findings are in the same file a.py, so only 1 unique path staged
        assert mock_add.call_count >= 1

    def test_no_worktree_returns_false(self):
        findings = [make_finding(finding_id='f001')]
        batch = make_batch(findings)
        batch.worktree_path = None
        batch.branch = None
        log_file = Path('/tmp/test.log')

        ok = commit_partial_batch(findings, batch, log_file)
        assert ok is False


# ── recover_interrupted_batch tests ──────────────────────────────

class TestRecoverInterruptedBatch:
    def test_recover_interrupted_batch_pushed(self, tmp_path):
        # Simulate: worktree exists + branch pushed to remote
        worktree_root = tmp_path / 'repo'
        worktree_root.mkdir(parents=True, exist_ok=True)
        worktree_path = worktree_root / 'worktree'
        worktree_path.mkdir(parents=True, exist_ok=True)

        batches_file = tmp_path / 'batches.jsonl'
        record = {
            'batch_id': 'batch-001',
            'rule_pattern': 'ruff-c408',
            'group_by': 'rule',
            'status': BatchStatus.FIXING.value,
            'worktree_path': str(worktree_path),
            'branch': 'qa/batch-001',
            'findings': [
                {'finding_id': 'f001', 'path': 'a.py', 'line': 10, 'rule': 'ruff-c408'},
            ],
            'fix_results': {},
            'retry_count': 0,
            'split_history': [],
        }
        batches_file.write_text(json.dumps(record) + '\n')

        with patch('core.sandbox_local_runner.utils.run_capture',
                   return_value=(0, 'origin/qa/batch-001\n')):
            batch = recover_interrupted_batch(
                'batch-001', batches_file, worktree_root)

        assert batch is not None
        assert batch.status == BatchStatus.PR_CREATED.value

    def test_recover_interrupted_batch_not_pushed(self, tmp_path):
        # Simulate: worktree exists but branch not pushed
        worktree_root = tmp_path / 'repo'
        worktree_root.mkdir(parents=True, exist_ok=True)
        worktree_path = worktree_root / 'worktree'
        worktree_path.mkdir(parents=True, exist_ok=True)


        batches_file = tmp_path / 'batches.jsonl'
        record = {
            'batch_id': 'batch-002',
            'rule_pattern': 'ruff-c408',
            'group_by': 'rule',
            'status': BatchStatus.FIXING.value,
            'worktree_path': str(worktree_path),
            'branch': 'qa/batch-002',
            'findings': [
                {'finding_id': 'f001', 'path': 'a.py', 'line': 10, 'rule': 'ruff-c408'},
            ],
            'fix_results': {},
            'retry_count': 0,
            'split_history': [],
        }
        batches_file.write_text(json.dumps(record) + '\n')

        with patch('core.sandbox_local_runner.utils.run_capture',
                   return_value=(1, '')), \
             patch('core.sandbox_local_runner.utils.run_no_capture'):
            batch = recover_interrupted_batch(
                'batch-002', batches_file, worktree_root)

        assert batch is not None
        assert batch.status == BatchStatus.ABORTED.value

    def test_recover_interrupted_batch_no_worktree(self, tmp_path):
        # Simulate: no worktree → ABORTED
        worktree_root = tmp_path / 'repo'
        worktree_root.mkdir(parents=True, exist_ok=True)

        batches_file = tmp_path / 'batches.jsonl'
        record = {
            'batch_id': 'batch-003',
            'rule_pattern': 'ruff-c408',
            'group_by': 'rule',
            'status': BatchStatus.FIXING.value,
            'worktree_path': str(worktree_root / 'nonexistent'),
            'branch': 'qa/batch-003',
            'findings': [
                {'finding_id': 'f001', 'path': 'a.py', 'line': 10, 'rule': 'ruff-c408'},
            ],
            'fix_results': {},
            'retry_count': 0,
            'split_history': [],
        }
        batches_file.write_text(json.dumps(record) + '\n')

        batch = recover_interrupted_batch('batch-003', batches_file, worktree_root)

        assert batch is not None
        assert batch.status == BatchStatus.ABORTED.value

    def test_unknown_batch_id_returns_none(self, tmp_path):
        batches_file = tmp_path / 'batches.jsonl'
        batches_file.write_text('')
        batch = recover_interrupted_batch('batch-unknown', batches_file, tmp_path)
        assert batch is None

    def test_wrong_status_returns_none(self, tmp_path):
        worktree_root = tmp_path / 'repo'
        worktree_root.mkdir(parents=True)
        batches_file = tmp_path / 'batches.jsonl'
        record = {
            'batch_id': 'batch-004',
            'status': BatchStatus.PR_CREATED.value,
            'worktree_path': str(worktree_root / 'wt'),
            'branch': 'qa/batch',
            'findings': [],
            'fix_results': {},
            'retry_count': 0,
            'split_history': [],
            'rule_pattern': 'ruff-c408',
            'group_by': 'rule',
        }
        batches_file.write_text(json.dumps(record) + '\n')

        batch = recover_interrupted_batch('batch-004', batches_file, worktree_root)
        assert batch is None


# ── process_batch split wiring tests ───────────────────────────────

class TestProcessBatchSplitWiring:
    @patch('core.sandbox_local_runner.batch_pr.handle_batch_failure')
    @patch('core.sandbox_local_runner.batch_pr.apply_batch_fixes')
    @patch('core.sandbox_local_runner.batch_pr._hydrate_batch_worktree_deps')
    @patch('core.sandbox_local_runner.batch_pr._create_worktree')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_process_batch_calls_split_on_high_failure_rate(
        self, mock_append, mock_create_wt, mock_hydrate, mock_fixes, mock_handle,
    ):
        """process_batch calls handle_batch_failure when failure_rate > 50%."""
        mock_create_wt.return_value = True
        mock_fixes.return_value = (4, 6)  # 4 successes, 6 failures = 60% failure rate
        mock_handle.return_value = []
        batch = make_batch(findings=[make_finding(finding_id=f'f{i}') for i in range(10)])
        batch.batch_id = 'batch-split-test'

        class FakeArgs:
            dry_run = True
            batch_pr_split_on_failure = True
            max_split_depth = 3
            worktree_root = Path('/tmp/wt')
            claude_cmd_template = None
            max_files_changed = 10
            max_loc_diff = 200
            live_github_actions = False

        with patch('core.sandbox_local_runner.utils.run_no_capture'):
            ok, reason = process_batch(batch, Path('/tmp/repo'), FakeArgs(), Path('/tmp/batch.log'))

        mock_handle.assert_called_once()
        assert ok is True
        assert reason == 'split-and-retried'

    @patch('core.sandbox_local_runner.batch_pr.apply_batch_fixes')
    @patch('core.sandbox_local_runner.batch_pr._hydrate_batch_worktree_deps')
    @patch('core.sandbox_local_runner.batch_pr._create_worktree')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_process_batch_max_depth_aborts(
        self, mock_append, mock_create_wt, mock_hydrate, mock_fixes,
    ):
        """process_batch aborts when max split depth is reached."""
        mock_create_wt.return_value = True
        mock_fixes.return_value = (4, 6)  # 4 successes, 6 failures = 60% failure rate
        batch = make_batch(
            findings=[make_finding(finding_id=f'f{i}') for i in range(10)],
            retry_count=3,
        )
        batch.batch_id = 'batch-split-test'

        class FakeArgs:
            dry_run = True
            batch_pr_split_on_failure = True
            max_split_depth = 3
            worktree_root = Path('/tmp/wt')
            claude_cmd_template = None
            max_files_changed = 10
            max_loc_diff = 200
            live_github_actions = False

        with patch('core.sandbox_local_runner.utils.run_no_capture'):
            ok, reason = process_batch(batch, Path('/tmp/repo'), FakeArgs(), Path('/tmp/batch.log'))

        assert ok is False
        assert reason == 'max-split-depth-exceeded'
        assert batch.status == BatchStatus.ABORTED.value
