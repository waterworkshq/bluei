"""Tests for the linter validator (ADR-0018 oracle)."""

from unittest.mock import patch, MagicMock
from pathlib import Path

from bluei.tools.seed.models import ParsedRule
from bluei.tools.seed.validator import (
    ValidationResult,
    validate_candidate,
    validate_with_ruff,
    _run_ruff,
    _ruff_code_from_rule,
)


def _make_candidate(rule="ruff-b904", before="", after="", source_linter="ruff"):
    return ParsedRule(
        rule=rule,
        language="python",
        before=before,
        after=after,
        detector_before="",
        detector_after="",
        negative_examples=[],
        has_autofix=False,
        source_linter=source_linter,
        validation_command="ruff check --select B904",
        description="test",
    )


def test_validate_accepts_correct_candidate():
    """Before triggers, after is clean → valid."""
    candidate = _make_candidate(before="x = 1\n", after="x = 1\n")
    with patch("bluei.tools.seed.validator._run_ruff") as mock_ruff:
        mock_ruff.side_effect = [
            [{"code": "B904", "message": "violation", "fix": {}}],  # before triggers
            [],  # after clean
        ]
        result = validate_with_ruff(candidate, "B904")
    assert result.valid
    assert result.before_triggered
    assert result.after_clean
    assert candidate.detector_before == "B904 violation"
    assert candidate.has_autofix is True


def test_validate_rejects_no_trigger():
    """Before doesn't trigger → invalid."""
    candidate = _make_candidate(before="x = 1\n", after="x = 1\n")
    with patch("bluei.tools.seed.validator._run_ruff") as mock_ruff:
        mock_ruff.side_effect = [
            [],  # before doesn't trigger
            [],
        ]
        result = validate_with_ruff(candidate, "B904")
    assert not result.valid
    assert "did not trigger" in result.reason


def test_validate_rejects_dirty_after():
    """Before triggers but after still has diagnostics → invalid."""
    candidate = _make_candidate(before="x = 1\n", after="x = 1\n")
    with patch("bluei.tools.seed.validator._run_ruff") as mock_ruff:
        mock_ruff.side_effect = [
            [{"code": "B904", "message": "violation"}],  # before triggers
            [{"code": "B904", "message": "still broken"}],  # after NOT clean
        ]
        result = validate_with_ruff(candidate, "B904")
    assert not result.valid
    assert "after still has" in result.reason


def test_validate_dispatches_by_linter():
    """validate_candidate dispatches to the right linter."""
    candidate = _make_candidate(source_linter="unknown")
    result = validate_candidate(candidate)
    assert not result.valid
    assert "unknown linter" in result.reason


def test_ruff_code_extraction():
    assert _ruff_code_from_rule("ruff-b904") == "B904"
    assert _ruff_code_from_rule("ruff-sim101") == "SIM101"
    assert _ruff_code_from_rule("B904") == "B904"
