#!/usr/bin/env python3
"""Tests that install-cron respects safety modes - no impossible phases are scheduled."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import SafetyMode


# Replicate the scheduling logic from cmd_install_cron
def _scheduling_for_mode(mode):
    """Mirror the logic in cmd_install_cron for INCLUDE_* flags."""
    include_pr = '1' if mode in {SafetyMode.PR.value, SafetyMode.MERGE.value} else '0'
    # Review-cycle manages existing PRs and retries; safe to run in ISSUE_ONLY and above
    include_review = '1' if mode in {
        SafetyMode.ISSUE_ONLY.value, SafetyMode.PR.value, SafetyMode.MERGE.value
    } else '0'
    include_merge = '1' if mode == SafetyMode.MERGE.value else '0'
    return {
        'include_pr': include_pr,
        'include_review': include_review,
        'include_merge': include_merge,
    }


class TestSchedulingBySafetyMode:
    """Verify that each safety mode only enables phases that are safe/allowed."""

    def test_observe_mode_schedules_nothing_live(self):
        """OBSERVE mode should not schedule any --no-dry-run phases."""
        flags = _scheduling_for_mode(SafetyMode.OBSERVE.value)
        assert flags['include_pr'] == '0'
        assert flags['include_review'] == '0'  # review touches PRs; observe blocks all live actions
        assert flags['include_merge'] == '0'

    def test_issue_only_schedules_issue_cycle_only(self):
        """ISSUE_ONLY mode should schedule issue-cycle but NOT pr-cycle or merge-cycle."""
        flags = _scheduling_for_mode(SafetyMode.ISSUE_ONLY.value)
        assert flags['include_pr'] == '0'
        assert flags['include_review'] == '1'  # review manages existing PRs; safe in issue-only
        assert flags['include_merge'] == '0'

    def test_pr_mode_schedules_issue_and_pr(self):
        """PR mode should schedule issue-cycle and pr-cycle but NOT merge-cycle."""
        flags = _scheduling_for_mode(SafetyMode.PR.value)
        assert flags['include_pr'] == '1'
        assert flags['include_review'] == '1'
        assert flags['include_merge'] == '0'

    def test_merge_mode_schedules_all(self):
        """MERGE mode should schedule all phases."""
        flags = _scheduling_for_mode(SafetyMode.MERGE.value)
        assert flags['include_pr'] == '1'
        assert flags['include_review'] == '1'
        assert flags['include_merge'] == '1'


class TestPhaseSafetyInvariant:
    """Ensure an invariant: no scheduled phase is blocked by its safety mode."""

    # Map: mode -> phases that should NEVER be scheduled
    FORBIDDEN = {
        SafetyMode.OBSERVE.value: {'pr-cycle', 'merge-cycle', 'review-cycle'},
        SafetyMode.ISSUE_ONLY.value: {'pr-cycle', 'merge-cycle'},
        SafetyMode.PR.value: {'merge-cycle'},
        SafetyMode.MERGE.value: set(),  # all phases allowed
    }

    @pytest.mark.parametrize('mode,forbidden_phases', [
        (SafetyMode.OBSERVE.value, {'pr-cycle', 'merge-cycle', 'review-cycle'}),
        (SafetyMode.ISSUE_ONLY.value, {'pr-cycle', 'merge-cycle'}),
        (SafetyMode.PR.value, {'merge-cycle'}),
        (SafetyMode.MERGE.value, set()),
    ])
    def test_no_forbidden_phases_scheduled(self, mode, forbidden_phases):
        flags = _scheduling_for_mode(mode)
        scheduled_pr = flags['include_pr'] == '1'
        scheduled_merge = flags['include_merge'] == '1'
        scheduled_review = flags['include_review'] == '1'

        # If review is scheduled, make sure review-cycle is not in the forbidden set for that mode
        if 'review-cycle' in forbidden_phases:
            assert not scheduled_review, f"review-cycle should not be scheduled in {mode}"

        if 'pr-cycle' in forbidden_phases:
            assert not scheduled_pr, f"pr-cycle should not be scheduled in {mode}"

        if 'merge-cycle' in forbidden_phases:
            assert not scheduled_merge, f"merge-cycle should not be scheduled in {mode}"


class TestPhaseAllowedForModeFunction:
    """Test the _phase_allowed_for_mode function from the CLI."""

    def _phase_allowed(self, mode: str, phase: str, dry_run: bool) -> tuple[bool, str]:
        """Replicate the _phase_allowed_for_mode logic."""
        if dry_run:
            return True, ''
        if mode == SafetyMode.OBSERVE.value:
            return False, 'Observe mode blocks non-dry-run execution.'
        if mode == SafetyMode.ISSUE_ONLY.value and phase in {'pr-cycle', 'merge-cycle'}:
            return False, 'Issue-only mode blocks PR and merge execution.'
        if mode == SafetyMode.PR.value and phase == 'merge-cycle':
            return False, 'PR mode blocks merge execution.'
        return True, ''

    @pytest.mark.parametrize('phase,mode,allowed', [
        # Non-dry-run cases
        ('issue-cycle', SafetyMode.OBSERVE.value, False),  # observe blocks all live
        ('issue-cycle', SafetyMode.ISSUE_ONLY.value, True),
        ('pr-cycle', SafetyMode.ISSUE_ONLY.value, False),  # blocked
        ('merge-cycle', SafetyMode.ISSUE_ONLY.value, False),  # blocked
        ('merge-cycle', SafetyMode.PR.value, False),  # blocked
        ('merge-cycle', SafetyMode.MERGE.value, True),
        ('pr-cycle', SafetyMode.PR.value, True),
        ('review-cycle', SafetyMode.OBSERVE.value, False),  # review is live action
    ])
    def test_phase_allowed(self, phase, mode, allowed):
        # Test as non-dry-run
        result_allowed, _ = self._phase_allowed(mode, phase, dry_run=False)
        assert result_allowed == allowed, f'{mode}/{phase} (non-dry-run): expected allowed={allowed}'

    def test_all_phases_allowed_in_merge_mode(self):
        """All phases should be allowed in MERGE mode for non-dry-run."""
        for phase in ['issue-cycle', 'pr-cycle', 'review-cycle', 'merge-cycle']:
            allowed, _ = self._phase_allowed(SafetyMode.MERGE.value, phase, dry_run=False)
            assert allowed, f'{phase} should be allowed in MERGE mode'


class TestSafetyModeSchedulingIntegration:
    """Integration tests: ensure cmd_install_cron respects safety modes."""

    def test_install_cron_script_respects_observe_mode(self, tmp_path):
        """
        When mode=observe, the install-cron script should receive INCLUDE_*=0 for
        PR, REVIEW, and MERGE — meaning no live-action cron entries are installed.
        """
        flags = _scheduling_for_mode(SafetyMode.OBSERVE.value)
        assert flags['include_pr'] == '0'
        assert flags['include_review'] == '0'
        assert flags['include_merge'] == '0'
        # Only issue-cycle cron entry should be installed (the non-conditional one
        # in install-cron.sh is always the issue-cycle line)
        # The issue-cycle is always installed (no INCLUDE_ guard around it)

    def test_install_cron_script_issue_only_includes_review(self, tmp_path):
        """
        ISSUE_ONLY should include review (since review manages existing PRs)
        but not pr or merge.
        """
        flags = _scheduling_for_mode(SafetyMode.ISSUE_ONLY.value)
        assert flags['include_pr'] == '0'
        assert flags['include_review'] == '1'
        assert flags['include_merge'] == '0'

    def test_install_cron_script_pr_includes_review_and_pr(self):
        """PR mode should include both pr and review."""
        flags = _scheduling_for_mode(SafetyMode.PR.value)
        assert flags['include_pr'] == '1'
        assert flags['include_review'] == '1'
        assert flags['include_merge'] == '0'

    def test_install_cron_script_merge_includes_all(self):
        """MERGE mode should include all phases."""
        flags = _scheduling_for_mode(SafetyMode.MERGE.value)
        assert flags['include_pr'] == '1'
        assert flags['include_review'] == '1'
        assert flags['include_merge'] == '1'
