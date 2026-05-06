#!/usr/bin/env python3
"""Tests for Phase G5: Rollout mode for autonomous-review.

Run with:
    PYTHONPATH=. .venv/bin/python -m pytest tests/test_review_rollout_mode.py -v

These tests verify the live_rollout_mode progression:
1. local_only (default) → no backend when live_actions=True, no live publish
2. shadow → backend generation + targeting, but NO actual GitHub publish; record intent
3. limited → full guarded path (requires guarded_live_review + live_actions)
4. Bad/ambiguous values fall back to local_only with explicit reason
5. Unsafe combinations (shadow/limited without live_actions) fall back safely
6. observation mode behavior is unchanged
7. Run artifacts and events capture rollout mode clearly
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

from qa_agent.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    LiveRolloutMode,
    generate_id,
)
from qa_agent.review import ReviewCycleEngine, GitHubReviewProvider
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Isolation helper — unique directory per test
# ---------------------------------------------------------------------------

def _isolated_tmp() -> Path:
    base = Path(f"/tmp/qa_rollout_{uuid.uuid4().hex[:8]}")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base


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
    return engine, repo, state


# ---------------------------------------------------------------------------
# Test: _get_live_rollout_mode — mode resolution
# ---------------------------------------------------------------------------

class TestRolloutModeResolution:
    """_get_live_rollout_mode returns correct mode and reason."""

    def test_default_local_only(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(tmp)
        mode, reason = engine._get_live_rollout_mode()
        assert mode == LiveRolloutMode.LOCAL_ONLY
        assert reason == "local-only-default"

    def test_explicit_local_only(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(
            tmp,
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value},
        )
        mode, reason = engine._get_live_rollout_mode()
        assert mode == LiveRolloutMode.LOCAL_ONLY

    def test_shadow_mode(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.SHADOW.value},
        )
        mode, reason = engine._get_live_rollout_mode()
        assert mode == LiveRolloutMode.SHADOW
        assert reason == "shadow-mode-active"

    def test_limited_mode_guard_passed(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "guarded_live_review": True,
            },
        )
        mode, reason = engine._get_live_rollout_mode()
        assert mode == LiveRolloutMode.LIMITED
        assert reason == "limited-mode-guard-passed"

    def test_limited_mode_guard_failed_fallback(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "guarded_live_review": False,
            },
        )
        mode, reason = engine._get_live_rollout_mode()
        assert mode == LiveRolloutMode.LOCAL_ONLY
        assert "limited-mode-guard-failed" in reason
        assert "fallback-to-local-only" in reason

    def test_bad_value_falls_back_to_local_only(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(
            tmp,
            review_care_overrides={"live_rollout_mode": "invalid-typo-value"},
        )
        mode, reason = engine._get_live_rollout_mode()
        assert mode == LiveRolloutMode.LOCAL_ONLY
        assert "unknown-live-rollout-mode" in reason
        assert "fallback-to-local-only" in reason

    def test_shadow_without_live_actions_falls_back(self):
        """shadow mode without live_actions is unsafe → falls back to local_only."""
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(
            tmp,
            github_overrides={"live_actions": False},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.SHADOW.value},
        )
        mode, reason = engine._get_live_rollout_mode()
        assert mode == LiveRolloutMode.LOCAL_ONLY
        assert "requires-live-actions" in reason
        assert "falling-back-to-local-only" in reason

    def test_limited_without_live_actions_falls_back(self):
        """limited mode without live_actions is unsafe → falls back to local_only."""
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(
            tmp,
            github_overrides={"live_actions": False},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.LIMITED.value},
        )
        mode, reason = engine._get_live_rollout_mode()
        assert mode == LiveRolloutMode.LOCAL_ONLY
        assert "requires-live-actions" in reason
        assert "falling-back-to-local-only" in reason


# ---------------------------------------------------------------------------
# Test: local_only behavior (default)
# ---------------------------------------------------------------------------

class TestLocalOnlyBehavior:
    """local_only mode: no backend when live_actions=True, no live publish."""

    def test_local_only_blocks_backend_when_live_actions_true(self):
        """When live_actions=True and local_only mode, backend is blocked."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value},
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)
        backend_called = False

        def track_backend(*args, **kwargs):
            nonlocal backend_called
            backend_called = True
            return []

        engine._generate_from_backend = track_backend

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert backend_called is False, "Backend should NOT be called in local_only mode with live_actions=True"
        assert result.findings_detected == 1

    def test_local_only_allows_backend_when_live_actions_false(self):
        """When live_actions=False, local_only allows backend for local analysis."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": False},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value},
        )
        backend_called = False

        def track_backend(*args, **kwargs):
            nonlocal backend_called
            backend_called = True
            return []

        engine._generate_from_backend = track_backend
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert backend_called is True, "Backend should be called in local_only mode when live_actions=False"

    def test_local_only_no_gh_api_call(self):
        """local_only never calls gh API (publish is skipped).

        Note: when live_actions=True but guard disabled, _post_summary_to_github is
        called (live_actions gate) but refuses to publish. This means gh pr list
        IS called for target resolution, but gh pr comment (the actual mutation)
        is NOT called. The test tracks gh pr comment specifically.
        """
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value},
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        comment_called = False
        original_run = subprocess.run

        def track_run(args, **kwargs):
            nonlocal comment_called
            # Only track the actual mutation call (gh pr comment)
            if "pr" in args and "comment" in args:
                comment_called = True
            return original_run(args, **kwargs)

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_run):
            result = engine._run_autonomous_review_cycle(dry_run=False)

        assert comment_called is False, "gh pr comment should NOT be called in local_only mode"

    def test_local_only_lifecycle_phase_is_guard_disabled(self):
        """local_only lifecycle_phase is guard-disabled."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.LOCAL_ONLY.value},
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1
        assert runs[0]["lifecycle_phase"] == "guard-disabled"
        assert runs[0]["live_rollout_mode"] == LiveRolloutMode.LOCAL_ONLY.value
        assert runs[0]["rollout_reason"] == "local-only-default"


# ---------------------------------------------------------------------------
# Test: shadow mode behavior
# ---------------------------------------------------------------------------

class TestShadowModeBehavior:
    """shadow mode: backend generates, targeting resolves, but NO actual publish."""

    def test_shadow_allows_backend(self):
        """shadow mode allows backend generation."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
                "guarded_live_review": False,
            },
        )
        backend_called = False

        def track_backend(*args, **kwargs):
            nonlocal backend_called
            backend_called = True
            return []

        engine._generate_from_backend = track_backend
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert backend_called is True, "Backend SHOULD be called in shadow mode"

    def test_shadow_does_not_call_gh_api(self):
        """shadow mode does NOT actually post to GitHub."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
                "guarded_live_review": False,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        gh_called = False
        original_run = subprocess.run

        def track_run(args, **kwargs):
            nonlocal gh_called
            if "pr" in args and "comment" in args:
                gh_called = True
            return original_run(args, **kwargs)

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_run):
            engine._run_autonomous_review_cycle(dry_run=False)

        assert gh_called is False, "gh pr comment should NOT be called in shadow mode"

    def test_shadow_creates_shadow_entry_in_publish_state(self):
        """shadow mode records shadow=True in the run publish entry."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
                "guarded_live_review": False,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        pstate = state.load_review_publish_state(repo.config.name)
        assert len(pstate["runs"]) == 1
        run_id = list(pstate["runs"].keys())[0]
        run_entry = pstate["runs"][run_id]
        assert run_entry.get("shadow") is True
        assert run_entry.get("shadow_summary_text") is not None
        assert run_entry.get("rollout_mode") == LiveRolloutMode.SHADOW.value
        assert "QA-Agent" in run_entry.get("shadow_summary_text", "")

    def test_shadow_lifecycle_phase_is_shadow_published(self):
        """shadow mode lifecycle_phase is shadow-published."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
                "guarded_live_review": False,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1
        assert runs[0]["lifecycle_phase"] == "shadow-published"
        assert runs[0]["live_rollout_mode"] == LiveRolloutMode.SHADOW.value

    def test_shadow_emits_shadow_event(self):
        """shadow mode emits autonomous-review-shadow-published event."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
                "guarded_live_review": False,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        events_file = state.get_review_events_file(repo.config.name)
        assert events_file.exists()
        events = [json.loads(line) for line in events_file.read_text().strip().splitlines()]
        shadow_events = [e for e in events if "shadow" in e.get("event", "")]
        assert len(shadow_events) >= 1, "Should have at least one shadow event"
        latest_shadow = shadow_events[-1]
        assert latest_shadow["event"] == "autonomous-review-shadow-published"
        assert latest_shadow["details"]["rollout_mode"] == LiveRolloutMode.SHADOW.value
        assert latest_shadow["details"]["action"] == "shadow-would-have-published"

    def test_shadow_run_completion_reason_records_shadow(self):
        """run_completion_reason for shadow mode mentions shadow publication."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
                "guarded_live_review": False,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert "shadow" in runs[0]["run_completion_reason"].lower()
        assert "would have been posted" in runs[0]["run_completion_reason"].lower()


# ---------------------------------------------------------------------------
# Test: limited mode behavior
# ---------------------------------------------------------------------------

class TestLimitedModeBehavior:
    """limited mode: full guarded path when guard conditions are met."""

    def test_limited_guard_enabled_allows_backend(self):
        """limited + guarded_live_review=True + live_actions=True → backend allowed."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "guarded_live_review": True,
            },
        )
        backend_called = False

        def track_backend(*args, **kwargs):
            nonlocal backend_called
            backend_called = True
            return []

        engine._generate_from_backend = track_backend
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert backend_called is True, "Backend SHOULD be called in limited mode when guard passes"

    def test_limited_guard_disabled_blocks_backend(self):
        """limited + guarded_live_review=False + live_actions=True → backend blocked."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "guarded_live_review": False,
            },
        )
        backend_called = False

        def track_backend(*args, **kwargs):
            nonlocal backend_called
            backend_called = True
            return []

        engine._generate_from_backend = track_backend
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert backend_called is False, "Backend should NOT be called in limited mode when guard fails"

    def test_limited_publishes_when_guard_passes(self):
        """limited + guard passed → actually publishes to GitHub."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "guarded_live_review": True,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        def fake_run(args, **kwargs):
            if "pr" in args and "list" in args:
                return MagicMock(returncode=0, stdout='[{"number": 5}]', stderr="")
            if "pr" in args and "comment" in args:
                return MagicMock(returncode=0, stdout="https://gh.io/pr5", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="")

        with unittest.mock.patch.object(subprocess, "run", side_effect=fake_run):
            engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert runs[0]["lifecycle_phase"] == "guarded-live-published"
        assert runs[0]["comment_url"] == "https://gh.io/pr5"


# ---------------------------------------------------------------------------
# Test: bad/ambiguous combinations fall back safely
# ---------------------------------------------------------------------------

class TestFallbackBehavior:
    """Unsafe or ambiguous config falls back to local_only with explicit reason."""

    def test_unknown_mode_value_falls_back_to_local_only(self):
        """Unknown live_rollout_mode value → local_only with explicit reason."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={
                "live_rollout_mode": "probably-shadowed",  # typo
                "guarded_live_review": True,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1
        assert runs[0]["live_rollout_mode"] == LiveRolloutMode.LOCAL_ONLY.value
        assert "unknown-live-rollout-mode" in runs[0]["rollout_reason"]
        assert "fallback-to-local-only" in runs[0]["rollout_reason"]
        # Should NOT have called backend when in fallback local_only
        # (but backend might have been called via the fallback path since live_actions=True + guard=False)

    def test_shadow_without_live_actions_stays_local_only(self):
        """shadow mode with live_actions=False → local_only fallback (no GitHub context)."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": False},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert runs[0]["live_rollout_mode"] == LiveRolloutMode.LOCAL_ONLY.value
        assert "requires-live-actions" in runs[0]["rollout_reason"]

    def test_limited_guard_failed_falls_back_with_explicit_reason(self):
        """limited mode when guard fails → local_only with explicit guard failure reason."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.LIMITED.value,
                "guarded_live_review": False,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert runs[0]["live_rollout_mode"] == LiveRolloutMode.LOCAL_ONLY.value
        assert "limited-mode-guard-failed" in runs[0]["rollout_reason"]
        assert "guard-failed-guarded-live-review-disabled" in runs[0]["rollout_reason"]


# ---------------------------------------------------------------------------
# Test: observation mode behavior is unchanged
# ---------------------------------------------------------------------------

class TestObservationModeUnchanged:
    """observation mode still uses its own path (not autonomous-review)."""

    def test_observation_mode_does_not_use_rollout_mode(self):
        tmp = _isolated_tmp()
        repo_path = tmp / "repo"
        repo_path.mkdir()
        config = RepoConfig(
            id="repo-test",
            name="test-repo",
            path=str(repo_path),
            language="typescript",
            review_care={
                "enabled": True,
                "mode": ReviewMode.OBSERVATION.value,
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
            },
            github={"live_actions": True},
        )
        repo = Repo(config=config)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
        engine.repo = repo
        engine.state = state
        engine.provider = MagicMock(spec=GitHubReviewProvider)
        engine.provider.list_managed_prs.return_value = []

        result = engine.run(dry_run=True)

        # Observation mode does not go through autonomous-review cycle
        events_file = state.get_review_events_file(repo.config.name)
        rollout_events = []
        if events_file.exists():
            events = [json.loads(line) for line in events_file.read_text().strip().splitlines()]
            rollout_events = [e for e in events if "rollout" in str(e.get("details", {}))]
        assert len(rollout_events) == 0, "Observation mode should not emit rollout events"


# ---------------------------------------------------------------------------
# Test: run artifacts capture rollout mode and shadow state
# ---------------------------------------------------------------------------

class TestArtifactsCaptureRolloutMode:
    """ReviewRun and publish-state capture live_rollout_mode and shadow state."""

    def test_review_run_has_live_rollout_mode(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.SHADOW.value},
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1
        assert "live_rollout_mode" in runs[0]
        assert "rollout_reason" in runs[0]
        assert "guard_enabled" in runs[0]
        assert "guard_reason" in runs[0]
        assert runs[0]["live_rollout_mode"] == LiveRolloutMode.SHADOW.value

    def test_review_event_has_rollout_mode(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.SHADOW.value},
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        events_file = state.get_review_events_file(repo.config.name)
        events = [json.loads(line) for line in events_file.read_text().strip().splitlines()]
        completed = [e for e in events if e.get("event") == "autonomous-review-completed"]
        assert len(completed) >= 1
        assert completed[-1]["details"]["live_rollout_mode"] == LiveRolloutMode.SHADOW.value
        assert completed[-1]["details"]["rollout_reason"] == "shadow-mode-active"

    def test_shadow_entry_has_target_pr_and_summary(self):
        """shadow publish state entry has targeted_pr_number and shadow_summary_text."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={
                "live_rollout_mode": LiveRolloutMode.SHADOW.value,
                "guarded_live_review": False,
            },
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        pstate = state.load_review_publish_state(repo.config.name)
        run_id = list(pstate["runs"].keys())[0]
        entry = pstate["runs"][run_id]
        assert entry.get("shadow") is True
        assert entry.get("targeted_pr_number") is not None or entry.get("status") == "pending"
        assert len(entry.get("shadow_summary_text", "")) > 0


# ---------------------------------------------------------------------------
# Test: dry_run skips rollout mode evaluation
# ---------------------------------------------------------------------------

class TestDryRunSkipsRollout:
    """dry_run=True returns before rollout mode evaluation."""

    def test_dry_run_does_not_evaluate_rollout_mode(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={"live_rollout_mode": LiveRolloutMode.SHADOW.value},
        )

        original_get_mode = engine._get_live_rollout_mode

        mode_called = False
        def track_get_mode():
            nonlocal mode_called
            mode_called = True
            return original_get_mode()

        engine._get_live_rollout_mode = track_get_mode

        result = engine.run(dry_run=True)

        assert mode_called is False, "_get_live_rollout_mode should not be called on dry_run"
        assert result.findings_detected == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
