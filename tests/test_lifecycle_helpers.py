"""Tests for helper functions in bluei.engine.lifecycle — decision logic, validation gate."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.models import Finding
from bluei.engine.lifecycle import _should_use_mnemo, _tier_validate
from bluei.engine.constants import CLAUDE_REQUIRED_RULES


def _f(**overrides):
    defaults = dict(
        finding_id="f001",
        repo="r",
        path="p.py",
        line=1,
        rule="ruff-c408",
        snippet="x = 1",
        confidence=0.8,
        quick_win=True,
        safe_to_autofix=True,
    )
    defaults.update(overrides)
    return Finding(**defaults)


class TestShouldUseMnemo:
    def test_claude_required_rule(self):
        f = _f(
            rule=list(CLAUDE_REQUIRED_RULES)[0]
            if CLAUDE_REQUIRED_RULES
            else "ruff-zero"
        )
        ok, reason = _should_use_mnemo(f, None, [])
        assert ok is True
        assert "claude-required-rule" in reason

    def test_retry_when_attempts_present(self):
        f = _f(fix_attempts=3)
        ok, reason = _should_use_mnemo(f, None, [])
        assert ok is True
        assert "retry-attempts=3" == reason

    def test_lesson_history_triggers_use(self):
        f = _f()
        ok, reason = _should_use_mnemo(f, None, [{"rule": "old"}])
        assert ok is True
        assert "lesson-history=1" == reason

    def test_not_quick_win(self):
        f = _f(quick_win=False)
        ok, reason = _should_use_mnemo(f, None, [])
        assert ok is True
        assert reason == "not-quick-win"

    def test_unsafe_to_autofix(self):
        f = _f(safe_to_autofix=False)
        ok, reason = _should_use_mnemo(f, None, [])
        assert ok is True
        assert reason == "unsafe-to-autofix"

    def test_long_snippet(self):
        f = _f(snippet="x" * 120)
        ok, reason = _should_use_mnemo(f, None, [])
        assert ok is True
        assert reason == "long-snippet"

    def test_trivial_case_skips(self):
        f = _f()  # quick_win=True, safe_to_autofix=True, short snippet, no history
        ok, reason = _should_use_mnemo(f, None, [])
        assert ok is False
        assert "trivial-first-pass-quick-win" in reason

    def test_finding_record_attempts_count(self):
        f = _f(fix_attempts=0)
        record = {"fix_attempts": 2}
        ok, reason = _should_use_mnemo(f, record, [])
        assert ok is True
        assert reason == "retry-attempts=2"


class TestTierValidate:
    @patch("bluei.engine.lifecycle._append_text")
    @patch("bluei.engine.lifecycle._get_tiered_validator")
    def test_validation_passes_cleanly(self, mock_validator, mock_log):
        from bluei.engine.fix_tiers import FixTier

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.requires_human_review = False
        mock_validator.return_value.validate.return_value = mock_result

        result = _tier_validate(
            FixTier.T1_IDEMPOTENT, Path("/wt"), _f(), Path("/log"), state=None
        )
        assert result is True

    @patch("bluei.engine.lifecycle._append_text")
    @patch("bluei.engine.lifecycle._get_tiered_validator")
    @patch("bluei.engine.lifecycle.run_capture")
    def test_validation_fails_triggers_rollback(
        self, mock_run, mock_validator, mock_log
    ):
        from bluei.engine.fix_tiers import FixTier

        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.requires_human_review = False
        mock_result.message = "lint regression"
        mock_validator.return_value.validate.return_value = mock_result
        mock_run.return_value = (0, "")

        result = _tier_validate(
            FixTier.T1_IDEMPOTENT, Path("/wt"), _f(), Path("/log"), state=None
        )
        assert result is False
        mock_run.assert_called_once()
        checkout_args = mock_run.call_args[0][0]
        assert checkout_args[0] == "git" and checkout_args[1] == "checkout"

    @patch("bluei.engine.lifecycle._append_text")
    @patch("bluei.engine.lifecycle._get_tiered_validator")
    @patch("bluei.engine.lifecycle.run_capture")
    def test_rollback_failure_logged(self, mock_run, mock_validator, mock_log):
        from bluei.engine.fix_tiers import FixTier

        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.requires_human_review = False
        mock_result.message = "error"
        mock_validator.return_value.validate.return_value = mock_result
        mock_run.return_value = (1, "git error")

        result = _tier_validate(
            FixTier.T1_IDEMPOTENT, Path("/wt"), _f(), Path("/log"), state=None
        )
        assert result is False
        rollback_failed = any(
            "ROLLBACK_FAILED" in str(c) for c in mock_log.call_args_list
        )
        assert rollback_failed

    @patch("bluei.engine.lifecycle.route_to_human_review")
    @patch("bluei.engine.lifecycle._append_text")
    @patch("bluei.engine.lifecycle._get_tiered_validator")
    def test_requires_human_review_routes(self, mock_validator, mock_log, mock_route):
        from bluei.engine.fix_tiers import FixTier

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.requires_human_review = True
        mock_validator.return_value.validate.return_value = mock_result

        result = _tier_validate(
            FixTier.T1_IDEMPOTENT, Path("/wt"), _f(), Path("/log"), state=None
        )
        assert result is False
        mock_route.assert_called_once()
        human_review = any("HUMAN_REVIEW" in str(c) for c in mock_log.call_args_list)
        assert human_review

    @patch("bluei.engine.lifecycle.check_tier_escalation")
    @patch("bluei.engine.lifecycle._append_text")
    @patch("bluei.engine.lifecycle._get_tiered_validator")
    @patch("bluei.engine.lifecycle.run_capture")
    def test_tier_escalation_checked_on_failure(
        self, mock_run, mock_validator, mock_log, mock_escalate
    ):
        from bluei.engine.fix_tiers import FixTier

        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.requires_human_review = False
        mock_validator.return_value.validate.return_value = mock_result
        mock_run.return_value = (0, "")
        mock_escalate.return_value = FixTier.T3_CONTEXTUAL

        result = _tier_validate(
            FixTier.T1_IDEMPOTENT, Path("/wt"), _f(), Path("/log"), state={"key": "val"}
        )
        assert result is False
        mock_escalate.assert_called_once()
        escalation = any("tier-escalation" in str(c) for c in mock_log.call_args_list)
        assert escalation
