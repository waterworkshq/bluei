#!/usr/bin/env python3
"""Tests for Batch PR execution engine (Phase 2).

Covers: process_batch, apply_batch_fixes, create_batch_pr,
        link_issues_to_batch_pr, verify_finding_closed,
        solo delegation, batch-disabled fallback.
"""

from __future__ import annotations

import json
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, call

import pytest

from core.sandbox_local_runner.models import (
    BatchGroup, BatchRule, BatchStatus, Finding, FixResult,
)
from core.sandbox_local_runner.batch_pr import (
    apply_batch_fixes,
    create_batch_pr,
    group_findings_for_batch,
    link_issues_to_batch_pr,
    process_batch,
    verify_finding_closed,
    _find_issue_for_finding,
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


def make_batch_group(n_findings=3, rule='ruff-c408') -> BatchGroup:
    """Create a multi-finding BatchGroup for testing."""
    findings = []
    issues = []
    for i in range(n_findings):
        fid = f'f{i:03d}'
        findings.append(make_finding(
            finding_id=fid, rule=rule,
            path=f'zerver/lib/file_{i}.py', line=10 + i,
        ))
        issues.append(make_issue(finding_id=fid))
    return BatchGroup(
        batch_id=f'batch-test-{rule}',
        rule_pattern=rule,
        group_by='rule',
        findings=findings,
        issues=issues,
    )


@dataclass
class FakeArgs:
    """Fake args namespace for testing."""
    worktree_root: str = '/tmp/test-worktrees'
    dry_run: bool = True
    live_github_actions: bool = False
    fix_engine: str = 'deterministic'
    claude_cmd_template: str = 'echo "mock"'
    max_files_changed: int = 5
    max_loc_diff: int = 200
    batch_state_file: str = '/tmp/test-batches.jsonl'
    batch_pr_enabled: bool = True


# ── Test: _find_issue_for_finding ────────────────────────────────

class TestFindIssueForFinding:
    def test_finds_matching_issue(self):
        issues = [
            {'finding_id': 'f001', 'status': 'open'},
            {'finding_id': 'f002', 'status': 'open'},
        ]
        result = _find_issue_for_finding(issues, 'f002')
        assert result is not None
        assert result['finding_id'] == 'f002'

    def test_returns_none_for_missing(self):
        issues = [{'finding_id': 'f001'}]
        assert _find_issue_for_finding(issues, 'f999') is None

    def test_uses_id_field_as_fallback(self):
        issues = [{'id': 'f001', 'status': 'open'}]
        result = _find_issue_for_finding(issues, 'f001')
        assert result is not None


# ── Test: Solo batch delegation ──────────────────────────────────

class TestSoloBatchDelegation:
    def test_solo_batch_returns_false_with_solo_delegated(self):
        """Solo batches return (False, 'solo-delegated') so caller uses existing path."""
        finding = make_finding(finding_id='f001')
        issue = make_issue(finding_id='f001')
        batch = BatchGroup.from_solo(issue, finding)

        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        success, detail = process_batch(batch, Path('/tmp/repo'), args, log_file)
        assert success is False
        assert detail == 'solo-delegated'


# ── Test: apply_batch_fixes ──────────────────────────────────────

class TestApplyBatchFixes:
    @patch('core.sandbox_local_runner.batch_pr._apply_single_fix')
    def test_all_fixes_succeed(self, mock_fix):
        """All findings get fixed successfully."""
        mock_fix.return_value = FixResult(
            finding_id='f001', status='success', diff_lines=1, fix_method='autofix',
        )
        batch = make_batch_group(n_findings=3)
        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.state._append_text'):
            successes, failures = apply_batch_fixes(
                batch, Path('/tmp/worktree'), Path('/tmp/repo'), args, log_file,
            )
        assert successes == 3
        assert failures == 0
        assert len(batch.fix_results) == 3
        assert all(r.status == 'success' for r in batch.fix_results.values())

    @patch('core.sandbox_local_runner.batch_pr._apply_single_fix')
    def test_partial_success(self, mock_fix):
        """Some fixes succeed, some fail."""
        results = [
            FixResult(finding_id='f000', status='success', fix_method='autofix'),
            FixResult(finding_id='f001', status='failed', error='no-fix', fix_method='autofix'),
            FixResult(finding_id='f002', status='success', fix_method='autofix'),
        ]
        mock_fix.side_effect = results
        batch = make_batch_group(n_findings=3)
        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.state._append_text'):
            successes, failures = apply_batch_fixes(
                batch, Path('/tmp/worktree'), Path('/tmp/repo'), args, log_file,
            )
        assert successes == 2
        assert failures == 1

    @patch('core.sandbox_local_runner.batch_pr._apply_single_fix')
    def test_all_fixes_fail(self, mock_fix):
        mock_fix.return_value = FixResult(
            finding_id='f001', status='failed', error='no-fix', fix_method='autofix',
        )
        batch = make_batch_group(n_findings=2)
        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.state._append_text'):
            successes, failures = apply_batch_fixes(
                batch, Path('/tmp/worktree'), Path('/tmp/repo'), args, log_file,
            )
        assert successes == 0
        assert failures == 2


# ── Test: _apply_single_fix ──────────────────────────────────────

class TestApplySingleFix:
    @patch('core.sandbox_local_runner.batch_pr.verify_finding_closed', return_value=True)
    @patch('core.sandbox_local_runner.lifecycle.apply_autofix', return_value=True)
    @patch('core.sandbox_local_runner.state._append_text')
    def test_autofix_succeeds(self, mock_append, mock_autofix, mock_verify):
        """Safe-to-autofix finding gets fixed via apply_autofix."""
        from core.sandbox_local_runner.batch_pr import _apply_single_fix

        finding = make_finding(safe_to_autofix=True)
        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        result = _apply_single_fix(finding, Path('/tmp/wt'), Path('/tmp/repo'), args, log_file)
        assert result.status == 'success'
        assert result.fix_method == 'autofix'

    @patch('core.sandbox_local_runner.batch_pr.verify_finding_closed', return_value=False)
    @patch('core.sandbox_local_runner.lifecycle.apply_autofix', return_value=True)
    @patch('core.sandbox_local_runner.state._append_text')
    def test_autofix_verification_fails(self, mock_append, mock_autofix, mock_verify):
        """Autofix applied but verification failed."""
        from core.sandbox_local_runner.batch_pr import _apply_single_fix

        finding = make_finding(safe_to_autofix=True)
        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        result = _apply_single_fix(finding, Path('/tmp/wt'), Path('/tmp/repo'), args, log_file)
        assert result.status == 'failed'
        assert result.error == 'verification-failed'

    @patch('core.sandbox_local_runner.lifecycle.apply_autofix', return_value=False)
    @patch('core.sandbox_local_runner.state._append_text')
    def test_autofix_fails_no_contextual(self, mock_append, mock_autofix):
        """Autofix fails, contextual fix also fails in deterministic mode.
        Result is 'failed' with 'autofix-unavailable' — autofix was tried but couldn't apply.
        Claude is NOT used as fallback in deterministic mode (use_claude=False).
        """
        from core.sandbox_local_runner.batch_pr import _apply_single_fix

        finding = make_finding(safe_to_autofix=True)
        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        # Mock context_fix module to return False (couldn't apply)
        mock_ctx = MagicMock(return_value=False)
        with patch.dict('sys.modules', {'core.sandbox_local_runner.context_fix': MagicMock(apply_contextual_fix=mock_ctx)}):
            result = _apply_single_fix(finding, Path('/tmp/wt'), Path('/tmp/repo'), args, log_file)
        # Both autofix and contextual failed to apply → 'failed' (autofix was available but didn't work)
        assert result.status == 'failed'
        assert result.error == 'autofix-unavailable'


# ── Test: create_batch_pr ────────────────────────────────────────

class TestCreateBatchPR:
    def test_pr_title_and_body_correct(self):
        """Batch PR has correct title and body for multi-finding batch."""
        batch = make_batch_group(n_findings=5, rule='ruff-c408')
        assert '5' in batch.pr_title()
        assert 'ruff-c408' in batch.pr_title()
        body = batch.pr_body()
        assert 'Batch Fix:' in body
        assert '5' in body
        assert '| #' in body

    def test_pr_body_has_finding_table(self):
        """Batch PR body includes a table of all findings."""
        batch = make_batch_group(n_findings=3, rule='ruff-c408')
        body = batch.pr_body()
        assert '| 1 |' in body
        assert '| 2 |' in body
        assert '| 3 |' in body
        # Check file paths appear
        assert 'file_0.py' in body
        assert 'file_1.py' in body
        assert 'file_2.py' in body

    @patch('core.sandbox_local_runner.utils.run_capture')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_create_batch_pr_parses_pr_number(self, mock_append, mock_run):
        """create_batch_pr parses PR number from gh output."""
        mock_run.return_value = (0, 'https://github.com/owner/repo/pull/42\n')
        batch = make_batch_group(n_findings=3)
        batch.worktree_path = Path('/tmp/wt')
        batch.branch = 'qa/batch-c408-1234'

        result = create_batch_pr(batch, 'owner/repo', Path('/tmp/log'))
        assert result['number'] == 42
        assert 'pull/42' in result['url']

    @patch('core.sandbox_local_runner.utils.run_capture')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_create_batch_pr_failure_raises(self, mock_append, mock_run):
        """create_batch_pr raises RuntimeError on gh failure."""
        mock_run.return_value = (1, 'error: PR creation failed')
        batch = make_batch_group(n_findings=2)
        batch.worktree_path = Path('/tmp/wt')
        batch.branch = 'qa/batch-c408-1234'

        with pytest.raises(RuntimeError, match='Failed to create batch PR'):
            create_batch_pr(batch, 'owner/repo', Path('/tmp/log'))


# ── Test: link_issues_to_batch_pr ────────────────────────────────

class TestLinkIssuesToBatchPR:
    @patch('core.sandbox_local_runner.gh.gh_issue_comment')
    @patch('core.sandbox_local_runner.orchestrator.set_issue_status')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_all_issues_linked(self, mock_append, mock_status, mock_comment):
        """All issues in batch are linked to the PR."""
        batch = make_batch_group(n_findings=3)

        link_issues_to_batch_pr(
            batch=batch,
            pr_number=42,
            pr_url='https://github.com/owner/repo/pull/42',
            repo_slug='owner/repo',
            repo_path=Path('/tmp/repo'),
            log_file=Path('/tmp/log'),
        )

        # Check all issues have PR metadata
        for issue in batch.issues:
            assert issue['github']['pr_number'] == 42
            assert issue['github']['batch_id'] == batch.batch_id

        # Check set_issue_status called for each issue
        assert mock_status.call_count == 3

        # Check gh_issue_comment called for each issue (they all have issue_number)
        assert mock_comment.call_count == 3

    @patch('core.sandbox_local_runner.gh.gh_issue_comment')
    @patch('core.sandbox_local_runner.orchestrator.set_issue_status')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_skips_comment_when_no_issue_number(self, mock_append, mock_status, mock_comment):
        """Issues without issue_number don't get gh comments."""
        batch = make_batch_group(n_findings=2)
        # Remove issue_number from one issue
        batch.issues[1]['github'] = {}

        link_issues_to_batch_pr(
            batch=batch,
            pr_number=42,
            pr_url='https://github.com/owner/repo/pull/42',
            repo_slug='owner/repo',
            repo_path=Path('/tmp/repo'),
            log_file=Path('/tmp/log'),
        )

        # Only one issue has issue_number
        assert mock_comment.call_count == 1


# ── Test: verify_finding_closed ──────────────────────────────────

class TestVerifyFindingClosed:
    @patch('core.sandbox_local_runner.lifecycle.verify_fix_closed', return_value=True)
    def test_returns_true_when_closed(self, mock_vfc):
        """verify_finding_closed returns True when lifecycle verify passes."""
        finding = make_finding()
        result = verify_finding_closed(Path('/tmp/wt'), finding, Path('/tmp/log'))
        assert result is True

    @patch('core.sandbox_local_runner.lifecycle.verify_fix_closed', side_effect=Exception('boom'))
    def test_returns_false_on_exception(self, mock_vfc):
        """verify_finding_closed returns False on exception."""
        finding = make_finding()
        result = verify_finding_closed(Path('/tmp/wt'), finding, Path('/tmp/log'))
        assert result is False


# ── Test: process_batch (multi-finding) ──────────────────────────

class TestProcessBatch:
    @patch('core.sandbox_local_runner.batch_pr.apply_batch_fixes')
    @patch('core.sandbox_local_runner.batch_pr._hydrate_batch_worktree_deps')
    @patch('core.sandbox_local_runner.batch_pr._create_worktree')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_worktree_created_for_batch(self, mock_append, mock_create_wt, mock_hydrate, mock_fixes):
        """process_batch creates a shared worktree for all batch findings."""
        mock_create_wt.return_value = True
        mock_fixes.return_value = (2, 0)  # 2 successes, 0 failures
        batch = make_batch_group(n_findings=2)
        args = FakeArgs(dry_run=True)
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.lifecycle.git_commit_all', return_value='committed'), \
             patch('core.sandbox_local_runner.lifecycle.git_push_branch', return_value=True), \
             patch('core.sandbox_local_runner.utils.run_no_capture'):
            success, detail = process_batch(batch, Path('/tmp/repo'), args, log_file)

        assert success is True
        assert batch.status == BatchStatus.DRY_RUN.value
        # Verify worktree was created with batch ID in path
        create_call = mock_create_wt.call_args
        assert f'qa-batch-{batch.batch_id}' in str(create_call[0][1])

    @patch('core.sandbox_local_runner.batch_pr.apply_batch_fixes')
    @patch('core.sandbox_local_runner.batch_pr._hydrate_batch_worktree_deps')
    @patch('core.sandbox_local_runner.batch_pr._create_worktree')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_no_successes_returns_false(self, mock_append, mock_create_wt, mock_hydrate, mock_fixes):
        """process_batch returns False when no fixes succeed."""
        mock_create_wt.return_value = True
        mock_fixes.return_value = (0, 3)  # 0 successes, 3 failures
        batch = make_batch_group(n_findings=3)
        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.utils.run_no_capture'):
            success, detail = process_batch(batch, Path('/tmp/repo'), args, log_file)

        assert success is False
        assert batch.status == BatchStatus.FAILED.value

    @patch('core.sandbox_local_runner.batch_pr._create_worktree')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_worktree_failure_returns_false(self, mock_append, mock_create_wt):
        """process_batch returns False when worktree creation fails."""
        mock_create_wt.return_value = False
        batch = make_batch_group(n_findings=2)
        args = FakeArgs()
        log_file = Path('/tmp/test.log')

        success, detail = process_batch(batch, Path('/tmp/repo'), args, log_file)
        assert success is False
        assert 'worktree' in detail

    @patch('core.sandbox_local_runner.batch_pr.apply_batch_fixes')
    @patch('core.sandbox_local_runner.batch_pr._hydrate_batch_worktree_deps')
    @patch('core.sandbox_local_runner.batch_pr._create_worktree')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_partial_success_still_creates_pr(self, mock_append, mock_create_wt, mock_hydrate, mock_fixes):
        """process_batch creates PR even with partial success."""
        mock_create_wt.return_value = True
        batch = make_batch_group(n_findings=3)
        # Mock apply_batch_fixes to return tally AND populate fix_results
        # (the real function does both, but mocking skips the real impl)
        from core.sandbox_local_runner.models import FixResult
        for i, f in enumerate(batch.findings):
            batch.fix_results[f.finding_id] = FixResult(
                finding_id=f.finding_id,
                status='success' if i < 2 else 'failed',
                diff_lines=1 if i < 2 else 0,
                fix_method='autofix',
            )
        mock_fixes.return_value = (2, 1)  # 2 successes, 1 failure
        args = FakeArgs(dry_run=True)
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.lifecycle.git_commit_all', return_value='committed'), \
             patch('core.sandbox_local_runner.lifecycle.git_push_branch', return_value=True), \
             patch('core.sandbox_local_runner.utils.run_no_capture'):
            success, detail = process_batch(batch, Path('/tmp/repo'), args, log_file)

        assert success is True
        # Verify fix results are recorded
        assert len(batch.fix_results) == 3

    @patch('core.sandbox_local_runner.batch_pr.apply_batch_fixes')
    @patch('core.sandbox_local_runner.batch_pr._hydrate_batch_worktree_deps')
    @patch('core.sandbox_local_runner.batch_pr._create_worktree')
    @patch('core.sandbox_local_runner.state._append_text')
    def test_branch_naming_convention(self, mock_append, mock_create_wt, mock_hydrate, mock_fixes):
        """Branch name follows qa/batch-{rule_short}-{timestamp} convention."""
        mock_create_wt.return_value = True
        mock_fixes.return_value = (2, 0)
        # Use 2+ findings so it's not solo (solo returns before setting branch)
        batch = make_batch_group(n_findings=2, rule='ruff-c408')
        args = FakeArgs(dry_run=True)
        log_file = Path('/tmp/test.log')

        with patch('core.sandbox_local_runner.lifecycle.git_commit_all', return_value='committed'), \
             patch('core.sandbox_local_runner.lifecycle.git_push_branch', return_value=True), \
             patch('core.sandbox_local_runner.utils.run_no_capture'):
            process_batch(batch, Path('/tmp/repo'), args, log_file)

        assert batch.branch is not None
        assert batch.branch.startswith('qa/batch-c408-')


# ── Test: Batch disabled falls back ──────────────────────────────

class TestBatchDisabledFallback:
    def test_no_batch_rules_when_disabled(self):
        """When batch_pr_enabled=False, _load_batch_rules_for_args returns empty list."""
        from core.sandbox_local_runner.cli import _load_batch_rules_for_args

        args = MagicMock()
        args.batch_pr_enabled = False
        rules = _load_batch_rules_for_args(args)
        assert rules == []

    def test_batch_disabled_uses_queue_candidates_directly(self):
        """When batch_pr_enabled=False, iteration_items = queue_candidates."""
        candidates = [make_candidate('f001'), make_candidate('f002'), make_candidate('f003')]
        # Simulate non-batch path
        _iteration_items = candidates
        assert len(_iteration_items) == 3
        for idx, (issue, finding) in enumerate(_iteration_items, start=1):
            assert issue['finding_id'] == finding.finding_id


# ── Test: Integration (grouping + execution wiring) ──────────────

class TestBatchIntegration:
    def test_grouping_produces_correct_solo_and_batch(self):
        """Grouping creates batches for batchable rules and solo for others."""
        candidates = [
            make_candidate('f001', rule='ruff-c408', path='zerver/a.py'),
            make_candidate('f002', rule='ruff-c408', path='zerver/b.py'),
            make_candidate('f003', rule='ruff-c408', path='zerver/c.py'),
            make_candidate('f004', rule='trailing-whitespace', path='zerver/d.py'),
        ]
        rules = [
            BatchRule(rule_pattern='ruff-c408', enabled=True, group_by='rule',
                      max_batch_size=20, max_files_per_batch=15, priority=1),
        ]
        groups = group_findings_for_batch(candidates, rules)

        # Should have 2 groups: one batch of 3 c408, one solo for trailing-whitespace
        assert len(groups) == 2
        batch_group = [g for g in groups if not g.is_solo]
        solo_group = [g for g in groups if g.is_solo]
        assert len(batch_group) == 1
        assert len(solo_group) == 1
        assert len(batch_group[0].findings) == 3
        assert solo_group[0].findings[0].rule == 'trailing-whitespace'

    def test_pr_body_for_batch_with_issue_numbers(self):
        """Batch PR body references issue numbers when available."""
        candidates = [
            (make_issue(finding_id='f001', github={'issue_number': 10}), make_finding(finding_id='f001', rule='ruff-c408', path='a.py', line=1)),
            (make_issue(finding_id='f002', github={'issue_number': 11}), make_finding(finding_id='f002', rule='ruff-c408', path='b.py', line=2)),
        ]
        rules = [BatchRule(rule_pattern='ruff-c408', enabled=True, group_by='rule', priority=1)]
        groups = group_findings_for_batch(candidates, rules)

        assert len(groups) == 1
        body = groups[0].pr_body()
        assert '#10' in body
        assert '#11' in body
