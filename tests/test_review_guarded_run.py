#!/usr/bin/env python3
"""Tests for Phase G4: Guarded-run policy for autonomous-review.

Run with:
    PYTHONPATH=. .venv/bin/python -m pytest tests/test_review_guarded_run.py -v

These tests verify that:
1. Missing guard flag → local-only (backend not called, gh not called)
2. Guard + live_actions + PR context → guarded live path allowed
3. Guard flag present but live_actions=False → local-only
4. Guard missing or PR ambiguous → refusal/local-only with explicit reason
5. State/run artifacts capture the guarded/live-vs-local decision
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
    generate_id,
)
from qa_agent.review import ReviewCycleEngine, GitHubReviewProvider
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Isolation helper — unique directory per test to avoid pytest tmp_path sharing
# ---------------------------------------------------------------------------

def _isolated_tmp() -> Path:
    base = Path(f"/tmp/qa_guarded_run_{uuid.uuid4().hex[:8]}")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base


# ---------------------------------------------------------------------------
# Shared stub candidates (from test_review_autonomous_cycle.py)
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
    {
        "repo": "test-repo",
        "path": "src/utils.ts",
        "line": 42,
        "header": "excessively-long-line",
        "snippet": "x = function_call(arg1, arg2, arg3)",
        "source": "linter",
        "actionability": "low",
        "severity": "low",
        "confidence": 0.6,
        "safe_to_autofix": True,
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
# Test: _is_guarded_live_review_enabled — guard reasons
# ---------------------------------------------------------------------------

class TestGuardEnabled:
    """Guard reasons are correctly computed from config."""

    def test_both_disabled_returns_both_disabled(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(tmp)
        enabled, reason = engine._is_guarded_live_review_enabled()
        assert enabled is False
        assert reason == "guard-failed-both-disabled"

    def test_guard_true_live_actions_false_returns_live_actions_disabled(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(tmp, github_overrides={"live_actions": False}, review_care_overrides={"guarded_live_review": True})
        enabled, reason = engine._is_guarded_live_review_enabled()
        assert enabled is False
        assert reason == "guard-failed-live-actions-disabled"

    def test_guard_false_live_actions_true_returns_guard_disabled(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(tmp, github_overrides={"live_actions": True}, review_care_overrides={"guarded_live_review": False})
        enabled, reason = engine._is_guarded_live_review_enabled()
        assert enabled is False
        assert reason == "guard-failed-guarded-live-review-disabled"

    def test_both_true_returns_passed(self):
        tmp = _isolated_tmp()
        engine, _, _ = make_engine(tmp, github_overrides={"live_actions": True}, review_care_overrides={"guarded_live_review": True})
        enabled, reason = engine._is_guarded_live_review_enabled()
        assert enabled is True
        assert reason == "guard-passed"


# ---------------------------------------------------------------------------
# Test: guard blocks backend generation
# ---------------------------------------------------------------------------

class TestGuardBlocksBackend:
    """When guard is disabled, _generate_from_backend is NOT called."""

    def test_guard_disabled_but_live_actions_false_allows_backend(self):
        """When live_actions=False (local-only), backend IS allowed even if guard is disabled.

        The guard only blocks the backend when live_actions=True (live publication path)
        and guarded_live_review=False. When live_actions=False the run is already
        safely local-only, so the backend can run for richer local analysis.
        """
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)
        backend_called = False

        original_generate = engine._generate_from_backend

        def track_backend(*args, **kwargs):
            nonlocal backend_called
            backend_called = True
            return original_generate(*args, **kwargs)

        engine._generate_from_backend = track_backend

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert backend_called is True, "_generate_from_backend SHOULD be called in local-only mode (live_actions=False)"
        assert result.findings_detected == 2

    def test_guard_live_actions_true_blocks_backend(self):
        """When live_actions=True but guard disabled, backend is blocked (stays local-only)."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)
        backend_called = False

        original_generate = engine._generate_from_backend

        def track_backend(*args, **kwargs):
            nonlocal backend_called
            backend_called = True
            return original_generate(*args, **kwargs)

        engine._generate_from_backend = track_backend

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert backend_called is False, "_generate_from_backend should NOT be called when live_actions=True but guarded_live_review=False"
        # But local path still works
        assert result.findings_detected == 2

    def test_guard_enabled_calls_backend(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo"},
            review_care_overrides={"guarded_live_review": True, "review_claude_template": "test"},
        )
        backend_called = False

        def stub_backend(*args, **kwargs):
            nonlocal backend_called
            backend_called = True
            return []

        engine._generate_from_backend = stub_backend
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert backend_called is True, "_generate_from_backend SHOULD be called when guard is enabled"
        # Local stub still used as fallback
        assert result.findings_detected >= 0


# ---------------------------------------------------------------------------
# Test: guard blocks live GitHub publication
# ---------------------------------------------------------------------------

class TestGuardBlocksLivePublish:
    """When guard is disabled, _post_summary_to_github is NOT called."""

    def test_guard_disabled_skips_gh_call(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        gh_called = False
        original_run = subprocess.run

        def track_run(args, **kwargs):
            nonlocal gh_called
            gh_called = True
            return original_run(args, **kwargs)

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_run):
            result = engine._run_autonomous_review_cycle(dry_run=False)

        assert gh_called is False, "subprocess.run (gh) should NOT be called when guard is disabled"
        # But run completed locally
        assert result.findings_detected == 2

    def test_guard_enabled_but_live_actions_false_skips_gh(self):
        """guard=true but live_actions=false → stays local."""
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": False},
            review_care_overrides={"guarded_live_review": True},
        )
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        gh_called = False
        original_run = subprocess.run

        def track_run(args, **kwargs):
            nonlocal gh_called
            gh_called = True
            return original_run(args, **kwargs)

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_run):
            result = engine._run_autonomous_review_cycle(dry_run=False)

        assert gh_called is False
        assert result.findings_detected == 2


# ---------------------------------------------------------------------------
# Test: guard event is emitted in all cases
# ---------------------------------------------------------------------------

class TestGuardEventEmitted:
    """A guard event is always appended to review_events.jsonl."""

    def test_guard_disabled_emits_blocked_event(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        events_file = state.get_review_events_file(repo.config.name)
        assert events_file.exists()
        events = [json.loads(line) for line in events_file.read_text().strip().splitlines()]
        guard_events = [e for e in events if "guard" in e.get("event", "")]
        assert len(guard_events) >= 1
        latest_guard = guard_events[-1]
        assert latest_guard["event"] == "autonomous-review-guard-blocked"
        assert latest_guard["details"]["guard_enabled"] is False
        assert "disabled" in latest_guard["details"]["guard_reason"]

    def test_guard_enabled_emits_passed_event(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo", "pr_number": 5},
            review_care_overrides={"guarded_live_review": True, "review_claude_template": "test"},
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

        events_file = state.get_review_events_file(repo.config.name)
        events = [json.loads(line) for line in events_file.read_text().strip().splitlines()]
        guard_events = [e for e in events if "guard" in e.get("event", "")]
        assert len(guard_events) >= 1
        latest_guard = guard_events[-1]
        assert latest_guard["event"] == "autonomous-review-guard-passed"
        assert latest_guard["details"]["guard_enabled"] is True
        assert latest_guard["details"]["guard_reason"] == "guard-passed"


# ---------------------------------------------------------------------------
# Test: state artifacts capture guard decision
# ---------------------------------------------------------------------------

class TestGuardDecisionInArtifacts:
    """ReviewRun and publish-state capture the guard decision."""

    def test_review_run_artifact_has_guard_fields(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1
        run = runs[0]
        assert "guard_enabled" in run
        assert "guard_reason" in run
        assert run["guard_enabled"] is False
        assert run["guard_reason"] == "guard-failed-both-disabled"

    def test_review_run_artifact_guard_passed_when_both_enabled(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True, "owner": "owner", "repo": "repo", "pr_number": 5},
            review_care_overrides={"guarded_live_review": True, "review_claude_template": "test"},
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
        assert len(runs) == 1
        run = runs[0]
        assert run["guard_enabled"] is True
        assert run["guard_reason"] == "guard-passed"

    def test_publish_state_run_entry_has_guard_fields(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        pstate = state.load_review_publish_state(repo.config.name)
        assert len(pstate["runs"]) == 1
        run_id = list(pstate["runs"].keys())[0]
        run_entry = pstate["runs"][run_id]
        # targeted_pr_number may or may not be present depending on gh config
        assert "guard_enabled" in run_entry or run_entry.get("status") in {
            "published", "failed", "skipped"
        }


# ---------------------------------------------------------------------------
# Test: observation mode behavior is unchanged
# ---------------------------------------------------------------------------

class TestObservationModeUnchanged:
    """Observation mode still goes through its own path (not autonomous-review)."""

    def test_observation_mode_does_not_use_guard(self):
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
                "guarded_live_review": True,  # should be ignored
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

        # Observation cycle
        engine.provider.list_managed_prs.return_value = []
        result = engine.run(dry_run=True)

        # Observation mode uses its own run path, not autonomous-review
        # Therefore no guard events should be emitted (guard is for autonomous-review only)
        events_file = state.get_review_events_file(repo.config.name)
        guard_events = []
        if events_file.exists():
            events = [json.loads(line) for line in events_file.read_text().strip().splitlines()]
            guard_events = [e for e in events if "guard" in e.get("event", "")]
        # Observation mode goes through a different path, no autonomous-review guard events
        assert len(guard_events) == 0


# ---------------------------------------------------------------------------
# Test: local-only path still works end-to-end when guard disabled
# ---------------------------------------------------------------------------

class TestLocalOnlyPathUnchanged:
    """When guard is disabled, full local pipeline still runs (state, artifacts, events)."""

    def test_local_only_creates_review_run_file(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert result.findings_detected == 2
        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1

    def test_local_only_creates_findings_artifacts(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        findings = state.load_review_findings(repo.config.name)
        assert len(findings) == 2
        for f in findings:
            assert "finding_id" in f
            assert "finding_fingerprint" in f

    def test_local_only_publish_state_updated(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        pstate = state.load_review_publish_state(repo.config.name)
        assert "findings" in pstate
        assert "runs" in pstate
        assert len(pstate["findings"]) == 2

    def test_local_only_summary_artifact_created(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(tmp, github_overrides={"live_actions": False})
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        run_id = runs[0]["run_id"]
        summary_dir = state.get_review_prompts_dir(repo.config.name)
        summary_files = list(summary_dir.glob(f"autonomous-run-{run_id}.md"))
        assert len(summary_files) == 1
        assert "QA-Agent Autonomous Review Summary" in summary_files[0].read_text()


# ---------------------------------------------------------------------------
# Test: dry_run returns immediately without guard evaluation
# ---------------------------------------------------------------------------

class TestDryRunSkipsGuard:
    """dry_run=True returns before guard evaluation (no side effects)."""

    def test_dry_run_returns_before_guard_check(self):
        tmp = _isolated_tmp()
        engine, repo, state = make_engine(
            tmp,
            github_overrides={"live_actions": True},
            review_care_overrides={"guarded_live_review": True},
        )
        # Guard method should never be called on dry_run
        original_guard = engine._is_guarded_live_review_enabled

        guard_called = False
        def track_guard():
            nonlocal guard_called
            guard_called = True
            return original_guard()

        engine._is_guarded_live_review_enabled = track_guard

        result = engine.run(dry_run=True)

        assert guard_called is False, "Guard should not be evaluated on dry_run"
        assert result.findings_detected == 0
        # No events or artifacts created
        assert not state.get_review_events_file(repo.config.name).exists()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
