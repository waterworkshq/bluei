"""Tests for F2 (confirmation prompts) and F3 (safety summary)."""

from unittest.mock import patch, MagicMock
from pathlib import Path


class TestConfirmHighRiskMode:
    """F2: Confirmation prompts for high-risk safety modes."""

    def test_merge_mode_requires_confirmation(self):
        from bin.bluei import _confirm_high_risk_mode

        with patch("builtins.input", return_value="n"):
            assert _confirm_high_risk_mode("full-care") is False

    def test_merge_mode_confirmed_with_y(self):
        from bin.bluei import _confirm_high_risk_mode

        with patch("builtins.input", return_value="y"):
            assert _confirm_high_risk_mode("full-care") is True

    def test_merge_mode_confirmed_with_yes(self):
        from bin.bluei import _confirm_high_risk_mode

        with patch("builtins.input", return_value="yes"):
            assert _confirm_high_risk_mode("merge") is True

    def test_auto_yes_skips_confirmation(self):
        from bin.bluei import _confirm_high_risk_mode

        # Should not call input at all
        with patch(
            "builtins.input", side_effect=AssertionError("input should not be called")
        ):
            assert _confirm_high_risk_mode("full-care", auto_yes=True) is True

    def test_observe_mode_no_confirmation_needed(self):
        from bin.bluei import _confirm_high_risk_mode

        with patch(
            "builtins.input", side_effect=AssertionError("input should not be called")
        ):
            assert _confirm_high_risk_mode("watch-only") is True

    def test_pr_mode_no_confirmation_needed(self):
        from bin.bluei import _confirm_high_risk_mode

        with patch(
            "builtins.input", side_effect=AssertionError("input should not be called")
        ):
            assert _confirm_high_risk_mode("offer-fixes") is True

    def test_issue_only_mode_no_confirmation_needed(self):
        from bin.bluei import _confirm_high_risk_mode

        with patch(
            "builtins.input", side_effect=AssertionError("input should not be called")
        ):
            assert _confirm_high_risk_mode("note-only") is True

    def test_default_response_is_no(self):
        """Empty input (just Enter) should be treated as 'no'."""
        from bin.bluei import _confirm_high_risk_mode

        with patch("builtins.input", return_value=""):
            assert _confirm_high_risk_mode("full-care") is False


class TestPrintSafetySummary:
    """F3: Safety policy summary as dedicated output."""

    def test_safety_summary_prints_mode(self, capsys):
        from bin.bluei import _print_safety_summary

        config = MagicMock()
        config.safety = {
            "mode": "observe",
            "profile": "conservative",
            "protected_branches": ["main", "master"],
            "require_clean_worktree": True,
        }
        config.fix_engine = "auto"
        config.github = {"live_actions": False}

        _print_safety_summary(config)
        captured = capsys.readouterr()
        assert "Watch only" in captured.out
        assert "conservative" in captured.out
        assert "main" in captured.out and "master" in captured.out
        assert "auto" in captured.out
        assert "disabled" in captured.out

    def test_safety_summary_shows_merge_mode(self, capsys):
        from bin.bluei import _print_safety_summary

        config = MagicMock()
        config.safety = {
            "mode": "merge",
            "profile": "aggressive",
            "protected_branches": ["main"],
        }
        config.fix_engine = "claude"
        config.github = {"live_actions": True}

        _print_safety_summary(config)
        captured = capsys.readouterr()
        assert "Full care" in captured.out
        assert "auto-merge" in captured.out
        assert "enabled" in captured.out

    def test_safety_summary_shows_pr_mode(self, capsys):
        from bin.bluei import _print_safety_summary

        config = MagicMock()
        config.safety = {"mode": "pr", "profile": "balanced", "protected_branches": []}
        config.fix_engine = "deterministic"
        config.github = {"live_actions": False}

        _print_safety_summary(config)
        captured = capsys.readouterr()
        assert "Offer fixes" in captured.out

    def test_safety_summary_has_distinct_section(self, capsys):
        """F3: The summary should be visually distinct with a section header."""
        from bin.bluei import _print_safety_summary

        config = MagicMock()
        config.safety = {
            "mode": "observe",
            "profile": "conservative",
            "protected_branches": ["main"],
        }
        config.fix_engine = "auto"
        config.github = {"live_actions": False}

        _print_safety_summary(config)
        captured = capsys.readouterr()
        assert "Safety Policy" in captured.out
        assert "──" in captured.out  # Section delimiter
