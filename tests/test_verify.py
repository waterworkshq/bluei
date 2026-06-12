"""Tests for the verify module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.models import Finding
from bluei.engine.verify import (
    VerificationResult,
    build_target_checks_for_finding,
    run_validation,
    verify_fix_closed,
)


@pytest.fixture
def sample_finding():
    """Create a sample finding for testing."""
    return Finding(
        finding_id="test-finding-1",
        repo="test-repo",
        path="test.py",
        line=10,
        rule="test-rule",
        snippet="def test(): pass",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
        severity="high",
    )


@pytest.fixture
def mock_log_file(tmp_path):
    """Create a mock log file."""
    return tmp_path / "test.log"


class TestVerificationResult:
    """Tests for VerificationResult dataclass."""

    def test_default_values(self):
        """Test default values are set correctly."""
        result = VerificationResult(passed=True)
        assert result.passed is True
        assert result.message == ""
        assert result.is_closed is False
        assert result.regressions == []
        assert result.target_failures == []
        assert result.baseline_results == {}
        assert result.post_results == {}

    def test_custom_values(self):
        """Test custom values are set correctly."""
        result = VerificationResult(
            passed=False,
            message="Test failed",
            is_closed=True,
            regressions=["check1"],
            target_failures=["target1"],
            baseline_results={"check1": {"rc": 0}},
            post_results={"check1": {"rc": 1}},
        )
        assert result.passed is False
        assert result.message == "Test failed"
        assert result.is_closed is True
        assert result.regressions == ["check1"]
        assert result.target_failures == ["target1"]
        assert result.baseline_results == {"check1": {"rc": 0}}
        assert result.post_results == {"check1": {"rc": 1}}


class TestVerifyFixClosed:
    """Tests for verify_fix_closed function."""

    @patch("bluei.engine.verify._verify_fix_closed")
    def test_finding_closed(self, mock_verify, sample_finding, mock_log_file, tmp_path):
        """Test when finding is closed."""
        mock_verify.return_value = True
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        result = verify_fix_closed(worktree_path, sample_finding, mock_log_file)

        assert result.passed is True
        assert result.is_closed is True
        assert result.message == ""
        mock_verify.assert_called_once()

    @patch("bluei.engine.verify._verify_fix_closed")
    def test_finding_still_open(
        self, mock_verify, sample_finding, mock_log_file, tmp_path
    ):
        """Test when finding is still open."""
        mock_verify.return_value = False
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        result = verify_fix_closed(worktree_path, sample_finding, mock_log_file)

        assert result.passed is False
        assert result.is_closed is False
        assert result.message == "Finding still open after fix"
        mock_verify.assert_called_once()

    @patch("bluei.engine.verify._verify_fix_closed")
    def test_with_docs_index_file(
        self, mock_verify, sample_finding, mock_log_file, tmp_path
    ):
        """Test with custom docs_index_file."""
        mock_verify.return_value = True
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        docs_index = tmp_path / "docs_index.json"

        result = verify_fix_closed(
            worktree_path, sample_finding, mock_log_file, docs_index
        )

        assert result.passed is True
        mock_verify.assert_called_once()
        call_kwargs = mock_verify.call_args[1]
        assert call_kwargs["docs_index_file"] == docs_index


class TestRunValidation:
    """Tests for run_validation function."""

    @patch("bluei.engine.verify.run_validation_gate")
    def test_validation_passed(self, mock_gate, tmp_path, mock_log_file):
        """Test when validation passes."""
        mock_gate.return_value = {
            "passed": True,
            "message": "",
            "regressions": [],
            "target_failures": [],
            "baseline_results": {"check1": {"rc": 0}},
            "post_results": {"check1": {"rc": 0}},
        }
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        result = run_validation(
            repo_path=repo_path,
            worktree_path=worktree_path,
            checks={"check1": ["pytest"]},
            log_file=mock_log_file,
        )

        assert result.passed is True
        assert result.message == ""
        assert result.regressions == []
        assert result.target_failures == []
        mock_gate.assert_called_once()

    @patch("bluei.engine.verify.run_validation_gate")
    def test_validation_failed_with_regressions(
        self, mock_gate, tmp_path, mock_log_file
    ):
        """Test when validation fails with regressions."""
        mock_gate.return_value = {
            "passed": False,
            "message": "Regressions detected",
            "regressions": ["check1", "check2"],
            "target_failures": [],
            "baseline_results": {"check1": {"rc": 0}, "check2": {"rc": 0}},
            "post_results": {"check1": {"rc": 1}, "check2": {"rc": 1}},
        }
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        result = run_validation(
            repo_path=repo_path,
            worktree_path=worktree_path,
            checks={"check1": ["pytest"], "check2": ["ruff"]},
            log_file=mock_log_file,
        )

        assert result.passed is False
        assert result.message == "Regressions detected"
        assert result.regressions == ["check1", "check2"]
        assert result.target_failures == []

    @patch("bluei.engine.verify.run_validation_gate")
    def test_validation_failed_with_target_failures(
        self, mock_gate, tmp_path, mock_log_file
    ):
        """Test when validation fails with target failures."""
        mock_gate.return_value = {
            "passed": False,
            "message": "Target checks failed",
            "regressions": [],
            "target_failures": ["target1"],
            "baseline_results": {},
            "post_results": {},
        }
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        result = run_validation(
            repo_path=repo_path,
            worktree_path=worktree_path,
            checks={"target1": ["pytest", "test_target.py"]},
            log_file=mock_log_file,
        )

        assert result.passed is False
        assert result.message == "Target checks failed"
        assert result.regressions == []
        assert result.target_failures == ["target1"]

    @patch("bluei.engine.verify.run_validation_gate")
    def test_with_precomputed_results(self, mock_gate, tmp_path, mock_log_file):
        """Test with precomputed baseline and post-fix results."""
        mock_gate.return_value = {
            "passed": True,
            "message": "",
            "regressions": [],
            "target_failures": [],
            "baseline_results": {"check1": {"rc": 0}},
            "post_results": {"check1": {"rc": 0}},
        }
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        baseline = {"check1": {"rc": 0, "fingerprint": "abc"}}
        post_fix = {"check1": {"rc": 0, "fingerprint": "abc"}}

        result = run_validation(
            repo_path=repo_path,
            worktree_path=worktree_path,
            checks={"check1": ["pytest"]},
            baseline_results=baseline,
            post_fix_results=post_fix,
            log_file=mock_log_file,
        )

        assert result.passed is True
        mock_gate.assert_called_once()
        call_kwargs = mock_gate.call_args[1]
        assert call_kwargs["baseline_results"] == baseline
        assert call_kwargs["post_fix_results"] == post_fix


class TestBuildTargetChecksForFinding:
    """Tests for build_target_checks_for_finding function."""

    @patch("bluei.engine.verify.build_target_checks")
    def test_builds_target_checks(self, mock_build, sample_finding):
        """Test that target checks are built correctly."""
        expected_checks = {"test-rule": ["pytest", "test_specific.py"]}
        mock_build.return_value = expected_checks

        result = build_target_checks_for_finding(sample_finding)

        assert result == expected_checks
        mock_build.assert_called_once_with(sample_finding)

    @patch("bluei.engine.verify.build_target_checks")
    def test_empty_checks_for_unknown_rule(self, mock_build, sample_finding):
        """Test that empty checks are returned for unknown rules."""
        mock_build.return_value = {}

        result = build_target_checks_for_finding(sample_finding)

        assert result == {}
        mock_build.assert_called_once_with(sample_finding)
