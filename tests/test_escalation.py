"""Tests for the escalation threshold module."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.sandbox_local_runner.escalation import (
    check_merge_failure_pattern,
    check_reappearing_findings,
    check_dedup_saturation,
    check_rebase_conflict_trend,
    run_escalation_checks,
)


class TestCheckMergeFailurePattern:
    """Consecutive merge failure detection."""

    def test_escalates_when_threshold_reached(self) -> None:
        result = check_merge_failure_pattern(3, 0)
        assert result is not None
        assert result['type'] == 'consecutive_merge_failures'
        assert result['count'] == 3

    def test_does_not_escalate_with_successes(self) -> None:
        result = check_merge_failure_pattern(3, 1)
        assert result is None

    def test_does_not_escalate_below_threshold(self) -> None:
        result = check_merge_failure_pattern(2, 0)
        assert result is None

    def test_custom_threshold(self) -> None:
        result = check_merge_failure_pattern(5, 0, {'consecutive_merge': 5})
        assert result is not None
        assert result['count'] == 5

    def test_zero_failures_no_escalation(self) -> None:
        result = check_merge_failure_pattern(0, 3)
        assert result is None


class TestCheckReappearingFindings:
    """Reappearing finding detection."""

    def test_flags_reappearing_finding(self) -> None:
        issues = {
            'issues': [
                {
                    'finding_id': 'finding-42',
                    'rule': 'ruff-c408',
                    'path': 'src/foo.py',
                    'history': [
                        {'event': 'open'},
                        {'event': 'fix_failed_verification'},
                        {'event': 'fix_failed_verification'},
                        {'event': 'fix_failed_verification'},
                    ],
                }
            ]
        }
        result = check_reappearing_findings(issues)
        assert len(result) == 1
        assert result[0]['type'] == 'reappearing_finding'
        assert result[0]['finding_id'] == 'finding-42'
        assert result[0]['failed_attempts'] == 3

    def test_clean_finding_no_escalation(self) -> None:
        issues = {
            'issues': [
                {
                    'finding_id': 'finding-1',
                    'history': [
                        {'event': 'open'},
                        {'event': 'fix_success'},
                        {'event': 'resolved_merged'},
                    ],
                }
            ]
        }
        result = check_reappearing_findings(issues)
        assert result == []

    def test_empty_issues(self) -> None:
        result = check_reappearing_findings({'issues': []})
        assert result == []

    def test_issue_without_finding_id_skipped(self) -> None:
        issues = {'issues': [{'rule': 'ruff-x', 'history': [{'event': 'open'}] * 5}]}
        result = check_reappearing_findings(issues)
        assert result == []


class TestCheckDedupSaturation:
    """Dedup guard saturation detection."""

    def test_flags_saturation(self) -> None:
        log_lines = [
            'some line',
            'batch-skip-duplicate: batch-1 existing PR #10',
            'batch-skip-duplicate: batch-2 existing PR #11',
            'batch-skip-duplicate: batch-3 existing PR #12',
        ]
        result = check_dedup_saturation(log_lines)
        assert len(result) == 1
        assert result[0]['type'] == 'dedup_saturation'
        assert result[0]['count'] == 3

    def test_clean_below_threshold(self) -> None:
        log_lines = ['batch-skip-duplicate: batch-1 existing PR #10']
        result = check_dedup_saturation(log_lines)
        assert result == []

    def test_empty_log(self) -> None:
        result = check_dedup_saturation([])
        assert result == []


class TestCheckRebaseConflictTrend:
    """Rebase conflict trend detection."""

    def test_flags_consistent_conflicts(self, tmp_path: Path) -> None:
        stats_file = tmp_path / 'rebase_stats.jsonl'
        stats_file.write_text(
            '{"conflicted": [{"pr": 1}], "rebased": []}\n'
            '{"conflicted": [{"pr": 2}], "rebased": []}\n'
            '{"conflicted": [{"pr": 3}], "rebased": []}\n'
        )
        result = check_rebase_conflict_trend(stats_file)
        assert len(result) == 1
        assert result[0]['type'] == 'rebase_conflict_trend'

    def test_clean_mixed_results(self, tmp_path: Path) -> None:
        stats_file = tmp_path / 'rebase_stats.jsonl'
        stats_file.write_text(
            '{"conflicted": [], "rebased": [{"pr": 1}]}\n'
            '{"conflicted": [{"pr": 2}], "rebased": []}\n'
            '{"conflicted": [{"pr": 3}], "rebased": []}\n'
        )
        result = check_rebase_conflict_trend(stats_file)
        assert result == []

    def test_not_enough_records(self, tmp_path: Path) -> None:
        stats_file = tmp_path / 'rebase_stats.jsonl'
        stats_file.write_text('{"conflicted": [{"pr": 1}], "rebased": []}\n')
        result = check_rebase_conflict_trend(stats_file)
        assert result == []

    def test_missing_file(self, tmp_path: Path) -> None:
        result = check_rebase_conflict_trend(tmp_path / 'nonexistent.jsonl')
        assert result == []


class TestRunEscalationChecks:
    """Integration: run all checks."""

    def test_all_clean(self, tmp_path: Path) -> None:
        log_file = tmp_path / 'run.log'
        log_file.write_text('')
        escalation_file = tmp_path / 'escalation_log.jsonl'
        result = run_escalation_checks(
            run_log_file=log_file,
            escalation_file=escalation_file,
            issues_data={'issues': []},
            merges_failed=0,
            merges_succeeded=2,
        )
        assert result == []
        # Escalation log should NOT be written (no findings)
        assert not escalation_file.exists()

    def test_merge_failure_escalation(self, tmp_path: Path) -> None:
        log_file = tmp_path / 'run.log'
        log_file.write_text('')
        escalation_file = tmp_path / 'escalation_log.jsonl'
        result = run_escalation_checks(
            run_log_file=log_file,
            escalation_file=escalation_file,
            issues_data={'issues': []},
            merges_failed=4,
            merges_succeeded=0,
        )
        assert len(result) == 1
        assert result[0]['type'] == 'consecutive_merge_failures'
        # Escalation log should be written
        assert escalation_file.exists()
        content = escalation_file.read_text()
        assert 'consecutive_merge_failures' in content
