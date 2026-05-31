"""Tests for handle_max_duplicate_escalation — the automated response handler."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bluei.engine.escalation import (
    handle_max_duplicate_escalation,
)


def _make_escalation(finding_id: str, pr_numbers: list[int], threshold: int = 3) -> dict:
    return {
        'type': 'max_duplicate_prs',
        'finding_id': finding_id,
        'open_pr_count': len(pr_numbers),
        'pr_numbers': pr_numbers,
        'threshold': threshold,
        'detail': f'Finding {finding_id} has {len(pr_numbers)} open PRs',
    }


def _make_issue(finding_id: str, status: str = 'open') -> dict:
    return {
        'issue_id': f'issue-{finding_id}',
        'finding_id': finding_id,
        'status': status,
        'rule': 'some-rule',
        'path': 'src/foo.py',
        'history': [],
    }


class TestHandleMaxDuplicateClosePRs:
    """Close excess PRs, keep newest."""

    @patch('bluei.engine.clean_prs._close_pr')
    def test_closes_oldest_keeps_newest(self, mock_close) -> None:
        mock_close.return_value = True
        esc = [_make_escalation('f1', [10, 11, 12, 13, 14])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
        )
        assert result['closed_prs'] == [10, 11, 12, 13]
        assert mock_close.call_count == 4
        last_call = mock_close.call_args
        assert 'Retained #14' in last_call[0][2]

    @patch('bluei.engine.clean_prs._close_pr')
    def test_skips_close_in_dry_run(self, mock_close) -> None:
        esc = [_make_escalation('f1', [10, 11, 12])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert result['closed_prs'] == []
        mock_close.assert_not_called()

    @patch('bluei.engine.clean_prs._close_pr')
    def test_handles_close_failure(self, mock_close) -> None:
        mock_close.side_effect = [True, False, True]
        esc = [_make_escalation('f1', [10, 11, 12, 13])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
        )
        assert result['closed_prs'] == [10, 12]
        assert result['close_failed'] == [11]

    @patch('bluei.engine.clean_prs._close_pr')
    def test_handles_close_exception(self, mock_close) -> None:
        mock_close.side_effect = [True, OSError('gh CLI crashed'), True]
        esc = [_make_escalation('f1', [10, 11, 12, 13])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
        )
        assert result['closed_prs'] == [10, 12]
        assert result['close_failed'] == [11]

    @patch('bluei.engine.clean_prs._close_pr')
    def test_single_pr_no_close(self, mock_close) -> None:
        esc = [_make_escalation('f1', [10])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
        )
        assert result['closed_prs'] == []
        mock_close.assert_not_called()

    @patch('bluei.engine.clean_prs._close_pr')
    def test_empty_pr_numbers_skips_close(self, mock_close) -> None:
        esc = [{'type': 'max_duplicate_prs', 'finding_id': 'f1', 'open_pr_count': 3, 'pr_numbers': [], 'threshold': 3}]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
        )
        assert result['closed_prs'] == []
        mock_close.assert_not_called()


class TestHandleMaxDuplicateRouteHuman:
    """Route matching issues to human review."""

    def test_routes_matching_issue(self) -> None:
        issue = _make_issue('f1')
        esc = [_make_escalation('f1', [10, 11, 12])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': [issue]},
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert issue['status'] == 'needs-human-max-duplicates-exceeded'
        assert 'f1' in result['routed_findings']
        assert any(h.get('event') == 'needs-human-max-duplicates-exceeded' for h in issue.get('history', []))

    def test_skips_route_if_no_matching_issue(self) -> None:
        esc = [_make_escalation('f1', [10, 11, 12])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': [_make_issue('f2')]},
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert result['routed_findings'] == []

    def test_no_double_route_if_already_routed(self) -> None:
        issue = _make_issue('f1', status='needs-human-max-duplicates-exceeded')
        esc = [_make_escalation('f1', [10, 11, 12])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': [issue]},
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert result['routed_findings'] == []
        routed_events = [h for h in issue.get('history', []) if h.get('event') == 'needs-human-max-duplicates-exceeded']
        assert len(routed_events) == 0


class TestHandleMaxDuplicatePause:
    """Pause findings via cooldown."""

    def test_marks_finding_activity(self) -> None:
        state: dict = {}
        esc = [_make_escalation('f1', [10, 11, 12])]
        handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state=state,
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert 'finding_activity' in state
        assert 'f1' in state['finding_activity']
        assert state['finding_activity']['f1']['last_action'] == 'max-duplicates-paused'
        assert state['finding_activity']['f1']['failure_count'] == 1

    def test_paused_findings_returned(self) -> None:
        esc = [_make_escalation('f1', [10, 11, 12])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert 'f1' in result['paused_findings']


class TestHandleMaxDuplicateMultiple:
    """Multiple findings handled independently."""

    @patch('bluei.engine.clean_prs._close_pr')
    def test_two_findings(self, mock_close) -> None:
        mock_close.return_value = True
        esc = [
            _make_escalation('f1', [10, 11, 12]),
            _make_escalation('f2', [20, 21, 22, 23]),
        ]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
        )
        assert sorted(result['closed_prs']) == [10, 11, 20, 21, 22]
        assert sorted(result['paused_findings']) == ['f1', 'f2']

    def test_empty_escalations(self) -> None:
        result = handle_max_duplicate_escalation(
            escalations=[],
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
        )
        assert result['closed_prs'] == []
        assert result['close_failed'] == []
        assert result['paused_findings'] == []
        assert result['routed_findings'] == []

    def test_escalation_without_finding_id_skipped(self) -> None:
        esc = [{'type': 'max_duplicate_prs', 'open_pr_count': 3, 'pr_numbers': [1, 2, 3], 'threshold': 3}]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
        )
        assert result['paused_findings'] == []
        assert result['routed_findings'] == []


class TestHandleMaxDuplicateStateMutation:
    """Verify state and issues_data are mutated in-place."""

    def test_state_has_activity_entries(self) -> None:
        state: dict = {}
        esc = [_make_escalation('f1', [10, 11, 12])]
        handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state=state,
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert 'finding_activity' in state
        entry = state['finding_activity']['f1']
        assert entry['last_action'] == 'max-duplicates-paused'
        assert 'last_action_at' in entry
        assert entry['failure_count'] == 1

    def test_issues_data_status_changed(self) -> None:
        issue = _make_issue('f1')
        issues_data = {'issues': [issue]}
        esc = [_make_escalation('f1', [10, 11, 12])]
        handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data=issues_data,
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert issues_data['issues'][0]['status'] == 'needs-human-max-duplicates-exceeded'

    def test_close_comment_mentions_retained_pr(self, tmp_path: Path) -> None:
        log_file = tmp_path / 'test.log'
        log_file.write_text('')
        esc = [_make_escalation('f1', [10, 11, 12])]
        with patch('bluei.engine.clean_prs._close_pr') as mock_close:
            mock_close.return_value = True
            handle_max_duplicate_escalation(
                escalations=esc,
                repo_slug='owner/repo',
                cwd=Path('/tmp'),
                state={},
                issues_data={'issues': []},
                log_file=log_file,
            )
            call_args = mock_close.call_args[0]
            comment = call_args[2]
            assert 'Retained #12' in comment


class TestCLIArgDefaults:
    """Verify CLI argument defaults for max-duplicates."""

    @staticmethod
    def _parse_args(args: list[str] | None = None) -> object:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument('--max-duplicate-prs-threshold', type=int, default=3)
        p.add_argument('--no-auto-close-duplicate-prs', action='store_true', default=False)
        return p.parse_args(args or [])

    def test_max_duplicate_prs_threshold_default(self) -> None:
        args = self._parse_args()
        assert args.max_duplicate_prs_threshold == 3

    def test_no_auto_close_default_false(self) -> None:
        args = self._parse_args()
        assert args.no_auto_close_duplicate_prs is False

    def test_max_duplicate_prs_threshold_override(self) -> None:
        args = self._parse_args(['--max-duplicate-prs-threshold', '5'])
        assert args.max_duplicate_prs_threshold == 5

    def test_no_auto_close_flag_set(self) -> None:
        args = self._parse_args(['--no-auto-close-duplicate-prs'])
        assert args.no_auto_close_duplicate_prs is True


class TestCLIWiringIntegration:
    """Test the escalation→handler wiring logic without running the full CLI."""

    @patch('bluei.engine.gh.gh_json')
    def test_handler_called_with_threshold_override(self, mock_gh_json) -> None:
        mock_gh_json.return_value = [
            {'number': 10, 'title': 'Fix', 'url': '', 'body': '- dedupe_key: f1\n'},
            {'number': 11, 'title': 'Fix', 'url': '', 'body': '- dedupe_key: f1\n'},
        ]
        from bluei.engine.escalation import run_escalation_checks
        escalation_findings = run_escalation_checks(
            run_log_file=Path('/dev/null'),
            escalation_file=Path('/dev/null'),
            issues_data={'issues': []},
            merges_failed=0,
            merges_succeeded=0,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            thresholds={'max_duplicate_prs': 2},
        )
        max_dup = [f for f in escalation_findings if f['type'] == 'max_duplicate_prs']
        assert len(max_dup) == 1
        assert max_dup[0]['threshold'] == 2

    @patch('bluei.engine.clean_prs._close_pr')
    def test_dry_run_flag_prevents_close(self, mock_close) -> None:
        mock_close.return_value = True
        esc = [_make_escalation('f1', [10, 11, 12])]
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
            dry_run=True,
        )
        assert result['closed_prs'] == []
        assert result['paused_findings'] == ['f1']
        mock_close.assert_not_called()

    @patch('bluei.engine.clean_prs._close_pr')
    def test_no_auto_close_flag_equivalent_to_dry_run(self, mock_close) -> None:
        mock_close.return_value = True
        esc = [_make_escalation('f1', [10, 11, 12])]
        no_auto_close = True
        result = handle_max_duplicate_escalation(
            escalations=esc,
            repo_slug='owner/repo',
            cwd=Path('/tmp'),
            state={},
            issues_data={'issues': []},
            log_file=Path('/dev/null'),
            dry_run=no_auto_close,
        )
        assert result['closed_prs'] == []
        assert result['paused_findings'] == ['f1']
        mock_close.assert_not_called()
