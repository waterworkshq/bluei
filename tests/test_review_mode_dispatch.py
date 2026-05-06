#!/usr/bin/env python3
"""Tests for Phase D1 mode dispatch in ReviewCycleEngine.

Run with: python -m pytest tests/test_review_mode_dispatch.py -v
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import Repo, RepoConfig, ReviewMode
from qa_agent.review import ReviewCycleEngine, GitHubReviewProvider
from qa_agent.state import StateManager


def make_repo(tmp_path: Path, mode: str = None) -> Repo:
    """Factory: creates a Repo with a RepoConfig at tmp_path."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = RepoConfig(
        id="repo-test",
        name="test-repo",
        path=str(repo_path),
        language="typescript",
    )
    if mode is not None:
        config.review_care["mode"] = mode
    return Repo(config=config)


def make_engine(tmp_path: Path, mode: str = None) -> ReviewCycleEngine:
    """Factory: creates a ReviewCycleEngine backed by a mock provider."""
    repo = make_repo(tmp_path, mode=mode)
    state = StateManager(tmp_path / "repos")
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    # Mock the provider so list_managed_prs returns empty (we only test dispatch)
    engine.provider = MagicMock(spec=GitHubReviewProvider)
    engine.provider.list_managed_prs.return_value = []
    return engine


# ---------------------------------------------------------------------------
# _get_review_mode
# ---------------------------------------------------------------------------

class TestGetReviewMode:
    def test_explicit_observation(self, tmp_path):
        engine = make_engine(tmp_path, mode="observation")
        assert engine._get_review_mode() == "observation"

    def test_explicit_autonomous_review(self, tmp_path):
        engine = make_engine(tmp_path, mode="autonomous-review")
        assert engine._get_review_mode() == "autonomous-review"

    def test_explicit_remediation(self, tmp_path):
        engine = make_engine(tmp_path, mode="remediation")
        assert engine._get_review_mode() == "remediation"

    def test_missing_mode_falls_back_to_observation(self, tmp_path):
        """Backward-compat: missing mode key → observation behavior."""
        repo = make_repo(tmp_path, mode=None)
        # Simulate a legacy config where 'mode' key is absent
        del repo.config.review_care["mode"]
        engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
        engine.repo = repo
        engine.state = StateManager(tmp_path / "repos")
        engine.provider = MagicMock(spec=GitHubReviewProvider)
        engine.provider.list_managed_prs.return_value = []
        assert engine._get_review_mode() == "observation"

    def test_unknown_mode_falls_back_to_observation(self, tmp_path):
        """Unknown/invalid mode values are normalized to observation for safety."""
        engine = make_engine(tmp_path, mode="future-unknown-mode")
        assert engine._get_review_mode() == "observation"


# ---------------------------------------------------------------------------
# run() dispatch — observation mode
# ---------------------------------------------------------------------------

class TestRunDispatch:
    def test_observation_mode_calls_observation_cycle(self, tmp_path):
        engine = make_engine(tmp_path, mode="observation")
        with patch.object(
            engine, "_run_observation_cycle", return_value=MagicMock()
        ) as mock_obs:
            result = engine.run(dry_run=True)
            mock_obs.assert_called_once_with(True, False)
            assert result is mock_obs.return_value

    def test_explicit_observation_mode_calls_observation_cycle(self, tmp_path):
        """Explicit 'observation' in config goes to observation cycle."""
        engine = make_engine(tmp_path, mode=ReviewMode.OBSERVATION.value)
        with patch.object(
            engine, "_run_observation_cycle", return_value=MagicMock()
        ) as mock_obs:
            result = engine.run(dry_run=True)
            mock_obs.assert_called_once_with(True, False)

    def test_autonomous_review_mode_calls_autonomous_cycle(self, tmp_path):
        """autonomous-review mode must NOT silently run observation logic."""
        engine = make_engine(tmp_path, mode="autonomous-review")
        with patch.object(
            engine, "_run_autonomous_review_cycle", return_value=MagicMock()
        ) as mock_ar:
            result = engine.run(dry_run=True)
            mock_ar.assert_called_once_with(True, False)
            # Observation cycle must NOT be called
            engine.provider.list_managed_prs.assert_not_called()

    def test_remediation_mode_calls_remediation_cycle(self, tmp_path):
        """remediation mode must NOT silently run observation logic."""
        engine = make_engine(tmp_path, mode="remediation")
        with patch.object(
            engine, "_run_remediation_cycle", return_value=MagicMock()
        ) as mock_rem:
            result = engine.run(dry_run=True)
            mock_rem.assert_called_once_with(True, False)
            # Observation cycle must NOT be called
            engine.provider.list_managed_prs.assert_not_called()

    def test_missing_mode_behaves_like_observation(self, tmp_path):
        """Backward-compat: missing mode routes to observation cycle."""
        repo = make_repo(tmp_path, mode=None)
        del repo.config.review_care["mode"]
        engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
        engine.repo = repo
        engine.state = StateManager(tmp_path / "repos")
        engine.provider = MagicMock(spec=GitHubReviewProvider)
        engine.provider.list_managed_prs.return_value = []
        with patch.object(
            engine, "_run_observation_cycle", return_value=MagicMock()
        ) as mock_obs:
            result = engine.run(dry_run=True)
            mock_obs.assert_called_once_with(True, False)

    def test_unknown_mode_behaves_like_observation(self, tmp_path):
        """Unknown mode falls back to observation cycle."""
        engine = make_engine(tmp_path, mode="not-a-real-mode")
        with patch.object(
            engine, "_run_observation_cycle", return_value=MagicMock()
        ) as mock_obs:
            result = engine.run(dry_run=True)
            mock_obs.assert_called_once_with(True, False)


# ---------------------------------------------------------------------------
# Stub methods return neutral results and log events
# ---------------------------------------------------------------------------

class TestAutonomousReviewStub:
    def test_returns_neutral_result(self, tmp_path):
        engine = make_engine(tmp_path, mode="autonomous-review")
        result = engine._run_autonomous_review_cycle(dry_run=True)
        assert result.active_prs == 0
        assert result.blocked_prs == 0
        assert result.merge_ready_prs == 0

    def test_logs_autonomous_review_completed_event_when_not_dry_run(self, tmp_path):
        """When not dry-run, the cycle completes and logs a completion event."""
        repo = make_repo(tmp_path, mode="autonomous-review")
        state = StateManager(tmp_path / "repos")
        engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
        engine.repo = repo
        engine.state = state
        engine.provider = MagicMock(spec=GitHubReviewProvider)

        events_file = state.get_review_events_file("test-repo")
        events_file.parent.mkdir(parents=True, exist_ok=True)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        # Result counters are non-zero when candidates are processed
        assert result.active_prs == 0
        assert events_file.exists()
        lines = events_file.read_text().strip().splitlines()
        # Phase J may emit learned-rule-log events before the completion event
        completion_events = [json.loads(l) for l in lines if json.loads(l)["event"] == "autonomous-review-completed"]
        assert len(completion_events) == 1
        event = completion_events[0]
        assert event["details"]["mode"] == "autonomous-review"
        assert "findings_total" in event["details"]

    def test_does_not_call_provider(self, tmp_path):
        """Cycle must not invoke any GitHub provider methods."""
        engine = make_engine(tmp_path, mode="autonomous-review")
        engine._run_autonomous_review_cycle(dry_run=True)
        engine.provider.list_managed_prs.assert_not_called()
        engine.provider.fetch_review_snapshot.assert_not_called()

    def test_dry_run_does_not_persist_artifacts(self):
        """Dry-run does not write any state artifacts.

        Uses uuid-based unique path to avoid class-level pytest tmp_path
        fixture pollution in TestAutonomousReviewStub.
        """
        import shutil, uuid
        # Use uuid to guarantee a unique path that won't collide with pytest's tmp_path
        base = Path(f"/tmp/qa_dispatch_dryrun_{uuid.uuid4().hex[:8]}")
        base.mkdir(parents=True)
        try:
            repo = make_repo(base, mode="autonomous-review")
            state = StateManager(base / "repos")
            state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
            engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
            engine.repo = repo
            engine.state = state
            engine.provider = MagicMock(spec=GitHubReviewProvider)

            result = engine._run_autonomous_review_cycle(dry_run=True)

            # No events written in dry-run
            events_file = state.get_review_events_file(repo.config.name)
            assert not events_file.exists()

            # No review runs persisted
            runs = state.list_review_runs(repo.config.name)
            assert len(runs) == 0

            # No publish state persisted
            publish = state.load_review_publish_state(repo.config.name)
            assert publish.get("findings", {}) == {}
            assert publish.get("runs", {}) == {}
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestRemediationStub:
    def test_returns_neutral_result(self, tmp_path):
        engine = make_engine(tmp_path, mode="remediation")
        result = engine._run_remediation_cycle(dry_run=True)
        assert result.active_prs == 0
        assert result.blocked_prs == 0
        assert result.merge_ready_prs == 0

    def test_logs_not_implemented_event_when_not_dry_run(self, tmp_path):
        repo = make_repo(tmp_path, mode="remediation")
        state = StateManager(tmp_path / "repos")
        engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
        engine.repo = repo
        engine.state = state
        engine.provider = MagicMock(spec=GitHubReviewProvider)

        events_file = state.get_review_events_file("test-repo")
        events_file.parent.mkdir(parents=True, exist_ok=True)

        result = engine._run_remediation_cycle(dry_run=False)

        assert result.active_prs == 0
        assert events_file.exists()
        lines = events_file.read_text().strip().splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["event"] == "remediation-not-implemented"
        assert event["details"]["mode"] == "remediation"
        assert "not yet implemented" in event["details"]["message"]

    def test_does_not_call_provider(self, tmp_path):
        """Stub must not invoke any provider methods."""
        engine = make_engine(tmp_path, mode="remediation")
        engine._run_remediation_cycle(dry_run=True)
        engine.provider.list_managed_prs.assert_not_called()
        engine.provider.fetch_review_snapshot.assert_not_called()


# ---------------------------------------------------------------------------
# Observation cycle preserves existing behavior (smoke test with mocks)
# ---------------------------------------------------------------------------

class TestObservationCycleSmoke:
    def test_observation_cycle_does_not_fall_through_to_stub(self, tmp_path):
        """run() with observation mode must call _run_observation_cycle."""
        engine = make_engine(tmp_path, mode="observation")
        # The observation cycle accesses list_managed_prs; verify it's NOT a stub
        engine.provider.list_managed_prs.return_value = []
        # If dispatch is broken and routes to stub, this assertion would fail
        # because stub never calls provider
        result = engine.run(dry_run=True)
        # Provider was called (observation cycle ran)
        engine.provider.list_managed_prs.assert_called()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
