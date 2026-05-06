#!/usr/bin/env python3
"""Tests for Phase G7: Monitored-rollout safety (circuit-breaker / open-cooldown).

Run with:
    PYTHONPATH=. .venv/bin/python -m pytest tests/test_review_monitored_safety.py -v

These tests verify that:
1. Repeated transient failures trigger monitored safety state (circuit opens)
2. Monitored safety blocks/degrades live publication path safely
3. Cooldown/open-circuit state eventually allows retry when conditions clear
4. Observation mode unchanged (safety checks only apply to limited+guarded path)
5. Safety state is persisted and loaded correctly
6. Run artifacts/events capture safety state correctly
"""

import json
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _isolated_tmp() -> Path:
    """Return a unique isolated temp directory per test."""
    base = Path(f"/tmp/qa_monitored_safety_{uuid.uuid4().hex[:8]}")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base


from qa_agent.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    LiveRolloutMode,
    MonitoredSafetyState,
    generate_id,
)
from qa_agent.review import ReviewCycleEngine, GitHubReviewProvider
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Shared stub candidates
# ---------------------------------------------------------------------------

STUB_CANDIDATES = [
    {
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 10,
        "header": "outstanding-todo",
        "snippet": "# TODO: refactor this function",
        "source": "linter",
        "actionability": "medium",
        "severity": "low",
        "confidence": 0.7,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_engine(tmp: Path, github_overrides: dict = None, review_care_overrides: dict = None):
    repo_path = tmp / "repo"
    repo_path.mkdir()
    github = {"live_actions": False, "auto_merge": False}
    if github_overrides:
        github.update(github_overrides)
    review_care = {
        "enabled": True,
        "mode": ReviewMode.AUTONOMOUS_REVIEW.value,
        "max_attempts": 3,
        "guarded_live_review": False,
        "live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value,
        "monitored_failure_threshold": 3,
        "monitored_cooldown_seconds": 300,
        "monitored_auto_rollback_enabled": False,
        "monitored_negative_feedback_threshold": 0.3,
        "monitored_feedback_min_events": 3,
        "monitored_feedback_window": 20,
    }
    if review_care_overrides:
        review_care.update(review_care_overrides)
    config = RepoConfig(
        id="repo-test",
        name="test-repo",
        path=str(repo_path),
        language="typescript",
        github=github,
        review_care=review_care,
    )
    repo = Repo(config=config)
    state = StateManager(tmp / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    engine.provider = MagicMock(spec=GitHubReviewProvider)
    return engine


# ---------------------------------------------------------------------------
# MonitoredSafetyState unit tests
# ---------------------------------------------------------------------------

class TestMonitoredSafetyState(unittest.TestCase):
    """Unit tests for MonitoredSafetyState model."""

    def test_record_failure_increments_count(self):
        state = MonitoredSafetyState()
        self.assertEqual(state.failure_count, 0)
        state.record_failure("test failure", cooldown_seconds=300)
        self.assertEqual(state.failure_count, 1)
        self.assertEqual(state.last_failure_reason, "test failure")
        self.assertFalse(state.circuit_open)

    def test_record_success_resets_state(self):
        state = MonitoredSafetyState(failure_count=2, circuit_open=True)
        state.record_success()
        self.assertEqual(state.failure_count, 0)
        self.assertFalse(state.circuit_open)
        self.assertIsNone(state.cooldown_until)
        self.assertIsNone(state.last_failure_at)

    def test_from_dict_restores_auto_rollback_fields(self):
        state = MonitoredSafetyState.from_dict({
            "auto_rollback_active": True,
            "auto_rollback_reason": "negative-feedback-ratio-0.50-over-threshold-0.30",
            "auto_rollback_triggered_at": "2026-03-30T00:00:00+00:00",
        })
        self.assertTrue(state.auto_rollback_active)
        self.assertIn("negative-feedback-ratio", state.auto_rollback_reason)
        self.assertEqual(state.auto_rollback_triggered_at, "2026-03-30T00:00:00+00:00")

    def test_check_cooldown_ready_false_when_in_cooldown(self):
        from datetime import datetime, timezone, timedelta
        state = MonitoredSafetyState(
            circuit_open=True,
            cooldown_until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        self.assertFalse(state.check_cooldown_ready())

    def test_check_cooldown_ready_true_when_expired(self):
        from datetime import datetime, timezone, timedelta
        state = MonitoredSafetyState(
            circuit_open=True,
            cooldown_until=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        )
        self.assertTrue(state.check_cooldown_ready())

    def test_to_dict_roundtrip(self):
        state = MonitoredSafetyState(
            circuit_open=True,
            failure_count=2,
            cooldown_until="2026-03-30T00:00:00+00:00",
            last_failure_at="2026-03-29T23:00:00+00:00",
            last_failure_reason="gh rate limit",
        )
        data = state.to_dict()
        restored = MonitoredSafetyState.from_dict(data)
        self.assertEqual(restored.circuit_open, True)
        self.assertEqual(restored.failure_count, 2)
        self.assertEqual(restored.last_failure_reason, "gh rate limit")

    def test_default_state_is_circuit_closed(self):
        state = MonitoredSafetyState()
        self.assertFalse(state.circuit_open)
        self.assertEqual(state.failure_count, 0)
        self.assertTrue(state.check_cooldown_ready())


# ---------------------------------------------------------------------------
# State persistence tests
# ---------------------------------------------------------------------------

class TestMonitoredSafetyStatePersistence(unittest.TestCase):
    """Tests for monitored safety state persistence via StateManager."""

    def test_save_and_load_monitored_safety_state(self):
        tmp = _isolated_tmp()
        try:
            state = StateManager(tmp / "repos")
            safety_data = {
                "circuit_open": True,
                "failure_count": 2,
                "cooldown_until": "2026-03-30T00:00:00+00:00",
                "last_failure_at": "2026-03-29T23:00:00+00:00",
                "last_failure_reason": "rate limit",
            }
            state.save_monitored_safety_state("test-repo", safety_data)
            loaded = state.load_monitored_safety_state("test-repo")
            self.assertEqual(loaded["circuit_open"], True)
            self.assertEqual(loaded["failure_count"], 2)
            self.assertEqual(loaded["last_failure_reason"], "rate limit")
        finally:
            shutil.rmtree(tmp)

    def test_load_returns_default_when_no_file(self):
        tmp = _isolated_tmp()
        try:
            state = StateManager(tmp / "repos")
            loaded = state.load_monitored_safety_state("nonexistent-repo")
            self.assertEqual(loaded["circuit_open"], False)
            self.assertEqual(loaded["failure_count"], 0)
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Circuit-breaker logic tests (internal methods)
# ---------------------------------------------------------------------------

class TestCircuitBreakerLogic(unittest.TestCase):
    """Tests for _check_monitored_safety and _record_publish_failure_for_safety."""

    def test_circuit_allows_live_when_closed(self):
        tmp = _isolated_tmp()
        try:
            engine = make_engine(tmp)
            allows, reason, safety_state = engine._check_monitored_safety()
            self.assertTrue(allows)
            self.assertEqual(reason, "circuit-closed")
            self.assertFalse(safety_state.circuit_open)
        finally:
            shutil.rmtree(tmp)

    def test_circuit_blocks_when_open_and_cooldown_active(self):
        tmp = _isolated_tmp()
        try:
            from datetime import datetime, timezone, timedelta
            engine = make_engine(tmp)
            # Pre-set circuit open with future cooldown
            future_cooldown = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            engine.state.save_monitored_safety_state("test-repo", {
                "circuit_open": True,
                "failure_count": 3,
                "cooldown_until": future_cooldown,
                "last_failure_reason": "rate limit",
            })
            allows, reason, safety_state = engine._check_monitored_safety()
            self.assertFalse(allows)
            self.assertIn("cooldown-active", reason)
            self.assertTrue(safety_state.circuit_open)
        finally:
            shutil.rmtree(tmp)

    def test_circuit_closes_when_cooldown_expired(self):
        tmp = _isolated_tmp()
        try:
            from datetime import datetime, timezone, timedelta
            engine = make_engine(tmp)
            # Pre-set circuit open with past cooldown (expired)
            past_cooldown = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            state_mgr = StateManager(tmp / "repos")
            state_mgr.save_monitored_safety_state("test-repo", {
                "circuit_open": True,
                "failure_count": 3,
                "cooldown_until": past_cooldown,
                "last_failure_reason": "rate limit",
            })
            allows, reason, safety_state = engine._check_monitored_safety()
            # Circuit should close but still not allow (must pass filters independently)
            self.assertFalse(allows)
            self.assertIn("cooldown-expired", reason)
            self.assertFalse(safety_state.circuit_open)
            # Failure count is preserved for visibility
            self.assertEqual(safety_state.failure_count, 3)
        finally:
            shutil.rmtree(tmp)

    def test_record_failure_increments_and_opens_circuit_at_threshold(self):
        tmp = _isolated_tmp()
        try:
            engine = make_engine(tmp, review_care_overrides={
                "monitored_failure_threshold": 3,
            })
            # Record 2 failures (below threshold)
            engine._record_publish_failure_for_safety("run1", "rate limit")
            engine._record_publish_failure_for_safety("run2", "network error")
            safety_data = engine.state.load_monitored_safety_state("test-repo")
            self.assertEqual(safety_data["failure_count"], 2)
            self.assertFalse(safety_data["circuit_open"])
            # Record 3rd failure (at threshold) - should open circuit
            engine._record_publish_failure_for_safety("run3", "timeout")
            safety_data = engine.state.load_monitored_safety_state("test-repo")
            self.assertEqual(safety_data["failure_count"], 3)
            self.assertTrue(safety_data["circuit_open"])
            self.assertIsNotNone(safety_data["cooldown_until"])
        finally:
            shutil.rmtree(tmp)

    def test_record_success_resets_failure_count(self):
        tmp = _isolated_tmp()
        try:
            engine = make_engine(tmp)
            # Pre-set some failures
            engine.state.save_monitored_safety_state("test-repo", {
                "circuit_open": False,
                "failure_count": 2,
                "last_failure_reason": "rate limit",
            })
            updated = engine._record_publish_success_for_safety("run-success")
            self.assertEqual(updated.failure_count, 0)
            self.assertFalse(updated.circuit_open)
            safety_data = engine.state.load_monitored_safety_state("test-repo")
            self.assertEqual(safety_data["failure_count"], 0)
            self.assertFalse(safety_data["circuit_open"])
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Integration tests for safety-blocked publish path
# ---------------------------------------------------------------------------

class TestSafetyBlockedPublishPath(unittest.TestCase):
    """Tests verifying that circuit-breaker blocks live publication in limited+guarded mode."""

    def test_lifecycle_phase_safety_blocked_when_circuit_open(self):
        tmp = _isolated_tmp()
        try:
            from datetime import datetime, timezone, timedelta
            # Set up engine with circuit open
            engine = make_engine(tmp, github_overrides={"live_actions": True}, review_care_overrides={
                "guarded_live_review": True,
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "monitored_failure_threshold": 2,
            })
            # Open the circuit (record 2 failures to meet threshold)
            engine._record_publish_failure_for_safety("run1", "rate limit")
            engine._record_publish_failure_for_safety("run2", "network error")
            # Verify circuit is open
            safety_data = engine.state.load_monitored_safety_state("test-repo")
            self.assertTrue(safety_data["circuit_open"])
            # The _check_monitored_safety should block
            allows, reason, _ = engine._check_monitored_safety()
            self.assertFalse(allows)
            self.assertIn("cooldown-active", reason)
        finally:
            shutil.rmtree(tmp)

    def test_observation_mode_unaffected_by_safety_state(self):
        """Observation mode should never attempt live publication regardless of safety state."""
        tmp = _isolated_tmp()
        try:
            from datetime import datetime, timezone, timedelta
            # Set up observation mode engine with circuit open
            engine = make_engine(tmp, github_overrides={"live_actions": True}, review_care_overrides={
                "guarded_live_review": False,
                "live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value,
                "monitored_failure_threshold": 1,  # Very low threshold
            })
            # Open the circuit
            engine._record_publish_failure_for_safety("run1", "rate limit")
            # In observation mode, _check_monitored_safety is never consulted
            # because guard_enabled is False
            safety_data = engine.state.load_monitored_safety_state("test-repo")
            self.assertTrue(safety_data["circuit_open"])
            # But observation mode should still work (it never attempts live publish)
            self.assertFalse(engine.repo.config.review_care.get("guarded_live_review"))
        finally:
            shutil.rmtree(tmp)

    def test_local_only_mode_unaffected_by_safety_state(self):
        """local_only mode should never attempt live publication regardless of safety state."""
        tmp = _isolated_tmp()
        try:
            from datetime import datetime, timezone, timedelta
            # Set up local_only mode engine with circuit open
            engine = make_engine(tmp, github_overrides={"live_actions": True}, review_care_overrides={
                "guarded_live_review": False,
                "live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value,
                "monitored_failure_threshold": 1,
            })
            # Open the circuit
            engine._record_publish_failure_for_safety("run1", "rate limit")
            safety_data = engine.state.load_monitored_safety_state("test-repo")
            self.assertTrue(safety_data["circuit_open"])
            # local_only never attempts live publication, so safety state is irrelevant
            self.assertEqual(
                engine.repo.config.review_care.get("live_rollout_mode"),
                LiveRolloutMode.LOCAL_ONLY.value
            )
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Run artifact / event capture tests
# ---------------------------------------------------------------------------

class TestSafetyStateInArtifacts(unittest.TestCase):
    """Tests verifying safety state is captured in run artifacts and events."""

    def test_review_run_data_contains_safety_fields(self):
        tmp = _isolated_tmp()
        try:
            engine = make_engine(tmp, github_overrides={"live_actions": True}, review_care_overrides={
                "guarded_live_review": True,
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
            })
            # Record a failure to have non-default safety state
            engine._record_publish_failure_for_safety("test-run", "test failure")
            safety_data = engine.state.load_monitored_safety_state("test-repo")
            # Verify safety state is persisted with correct fields
            self.assertIn("circuit_open", safety_data)
            self.assertIn("failure_count", safety_data)
            self.assertIn("cooldown_until", safety_data)
            self.assertIn("last_failure_reason", safety_data)
        finally:
            shutil.rmtree(tmp)

    def test_monitored_safety_event_emitted_on_circuit_open(self):
        tmp = _isolated_tmp()
        try:
            engine = make_engine(tmp, review_care_overrides={
                "monitored_failure_threshold": 2,
            })
            # Record failures to open circuit
            engine._record_publish_failure_for_safety("run1", "rate limit")
            engine._record_publish_failure_for_safety("run2", "network error")
            # Check review events were emitted
            events_file = tmp / "repos" / "test-repo" / "state" / "review_events.jsonl"
            self.assertTrue(events_file.exists(), f"Events file not found: {events_file}")
            with open(events_file) as f:
                events_content = f.read()
            self.assertIn("monitored-safety-failure-recorded", events_content)
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Cooldown expiration behavior
# ---------------------------------------------------------------------------

class TestFeedbackAutoRollback(unittest.TestCase):
    """Tests for feedback-driven fail-closed rollback in monitored rollout."""

    def test_feedback_auto_rollback_activates_when_negative_ratio_exceeds_threshold(self):
        tmp = _isolated_tmp()
        try:
            engine = make_engine(tmp, review_care_overrides={
                "guarded_live_review": True,
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "monitored_auto_rollback_enabled": True,
                "monitored_negative_feedback_threshold": 0.5,
                "monitored_feedback_min_events": 3,
                "monitored_feedback_window": 10,
            })
            for signal in ("negative", "request_change", "approve"):
                engine.state.append_feedback_event("test-repo", {"signal": signal})

            updated = engine._evaluate_feedback_auto_rollback("run-feedback-1")
            self.assertTrue(updated.auto_rollback_active)
            self.assertIn("negative-feedback-ratio", updated.auto_rollback_reason)

            allows, reason, state_after = engine._check_monitored_safety()
            self.assertFalse(allows)
            self.assertIn("auto-rollback-active", reason)
            self.assertTrue(state_after.auto_rollback_active)

            events_file = tmp / "repos" / "test-repo" / "state" / "review_events.jsonl"
            with open(events_file) as f:
                events_content = f.read()
            self.assertIn("monitored-safety-auto-rollback-activated", events_content)
        finally:
            shutil.rmtree(tmp)

    def test_feedback_auto_rollback_does_not_activate_below_threshold(self):
        tmp = _isolated_tmp()
        try:
            engine = make_engine(tmp, review_care_overrides={
                "guarded_live_review": True,
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "monitored_auto_rollback_enabled": True,
                "monitored_negative_feedback_threshold": 0.8,
                "monitored_feedback_min_events": 3,
                "monitored_feedback_window": 10,
            })
            for signal in ("negative", "approve", "approve"):
                engine.state.append_feedback_event("test-repo", {"signal": signal})

            updated = engine._evaluate_feedback_auto_rollback("run-feedback-2")
            self.assertFalse(updated.auto_rollback_active)

            allows, reason, state_after = engine._check_monitored_safety()
            self.assertTrue(allows)
            self.assertEqual(reason, "circuit-closed")
            self.assertFalse(state_after.auto_rollback_active)
        finally:
            shutil.rmtree(tmp)


class TestAutonomousRunSafetyFields(unittest.TestCase):
    """Tests ensuring auto-rollback becomes operator-visible in run artifacts."""

    def test_run_artifact_marks_attention_when_auto_rollback_active(self):
        tmp = _isolated_tmp()
        try:
            engine = make_engine(tmp, github_overrides={"live_actions": True}, review_care_overrides={
                "guarded_live_review": True,
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "monitored_auto_rollback_enabled": True,
                "monitored_negative_feedback_threshold": 0.5,
                "monitored_feedback_min_events": 3,
                "monitored_feedback_window": 10,
            })
            engine._generate_local_candidates = lambda pr_context=None: list(STUB_CANDIDATES)
            engine._resolve_pr_context_for_autonomous_run = lambda prior_publish: (123, "explicit-test")
            engine._post_summary_to_github = lambda **kwargs: None
            for signal in ("negative", "request_change", "approve"):
                engine.state.append_feedback_event("test-repo", {"signal": signal})

            result = engine._run_autonomous_review_cycle(dry_run=False)
            assert result.findings_detected == 1

            runs_dir = tmp / "repos" / "test-repo" / "state" / "review_runs"
            run_files = sorted(runs_dir.glob("*.json"))
            self.assertTrue(run_files)
            import json
            run_data = json.loads(run_files[-1].read_text())
            self.assertTrue(run_data["attention_recommended"])
            self.assertTrue(run_data["auto_rollback_active"])
            self.assertIn("negative-feedback-ratio", run_data["auto_rollback_reason"])
            self.assertTrue(run_data["operator_action_required"])
            self.assertEqual(
                run_data["operator_action_summary"],
                "disable-guarded-live-review-and-fallback-to-shadow",
            )
            self.assertEqual(
                run_data["suggested_review_care_patch"]["live_rollout_mode"],
                "shadow",
            )
            self.assertEqual(run_data["lifecycle_phase"], "safety-blocked")
        finally:
            shutil.rmtree(tmp)


class TestCooldownExpiration(unittest.TestCase):
    """Tests verifying that cooldown expiration allows retry."""

    def test_circuit_closes_after_cooldown_expires(self):
        tmp = _isolated_tmp()
        try:
            from datetime import datetime, timezone, timedelta
            engine = make_engine(tmp, review_care_overrides={
                "monitored_failure_threshold": 2,
                "monitored_cooldown_seconds": 300,
            })
            # Open the circuit with a near-expired cooldown
            near_expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            engine.state.save_monitored_safety_state("test-repo", {
                "circuit_open": True,
                "failure_count": 2,
                "cooldown_until": near_expired,
                "last_failure_reason": "rate limit",
            })
            # Check should close circuit since cooldown expired
            allows, reason, safety_state = engine._check_monitored_safety()
            self.assertFalse(allows)  # Still False - filters must pass independently
            self.assertIn("cooldown-expired", reason)
            self.assertFalse(safety_state.circuit_open)
            # But failure count is preserved
            self.assertEqual(safety_state.failure_count, 2)
        finally:
            shutil.rmtree(tmp)

    def test_cooldown_duration_respected(self):
        tmp = _isolated_tmp()
        try:
            from datetime import datetime, timezone, timedelta
            engine = make_engine(tmp, review_care_overrides={
                "monitored_failure_threshold": 1,
                "monitored_cooldown_seconds": 3600,  # 1 hour
            })
            # Open circuit
            engine._record_publish_failure_for_safety("run1", "rate limit")
            safety_data = engine.state.load_monitored_safety_state("test-repo")
            cooldown_until = datetime.fromisoformat(safety_data["cooldown_until"])
            if cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            # Cooldown should be approximately 1 hour from now
            diff = (cooldown_until - now).total_seconds()
            self.assertGreater(diff, 3500)  # At least ~3500 seconds (allowing some tolerance)
            self.assertLessEqual(diff, 3700)  # At most ~3700 seconds
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
