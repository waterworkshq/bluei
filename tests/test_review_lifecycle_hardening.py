#!/usr/bin/env python3
"""Tests for Phase G4 lifecycle hardening: run_completion_reason, lifecycle_phase, candidate_source.

These tests verify that autonomous-review runs produce explicit lifecycle markers in
run artifacts and events:
- run_completion_reason: human-readable explanation of why the run ended the way it did
- lifecycle_phase: explicit state label (guard-disabled, guarded-live-published, etc.)
- candidate_source: where the finding candidates came from (backend vs local-stub)

Run with:
    .venv/bin/python -m pytest tests/test_review_lifecycle_hardening.py -v
"""

import json
import shutil
import subprocess
import sys
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    PublishStatus,
)
from qa_agent.review import ReviewCycleEngine, GitHubReviewProvider
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Shared stub finding set
# ---------------------------------------------------------------------------
STUB_CANDIDATES = [
    {
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 10,
        "header": "unused-variable",
        "snippet": "x = 1",
        "source": "linter",
        "actionability": "medium",
        "severity": "low",
        "confidence": 0.7,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
        "finding_id": "f1",
    },
]


def _isolated_tmp():
    import tempfile
    return Path(tempfile.mkdtemp(prefix="qa_lifecycle_"))


def make_repo(tmp_path: Path) -> Repo:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    return Repo(
        config=RepoConfig(
            id="repo-test",
            name="test-repo",
            path=str(repo_path),
            language="typescript",
            review_care={
                "enabled": True,
                "mode": ReviewMode.AUTONOMOUS_REVIEW.value,
                "max_attempts": 3,
            },
        ),
    )


def make_engine(repo: Repo, state: StateManager) -> ReviewCycleEngine:
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    engine.provider = MagicMock(spec=GitHubReviewProvider)
    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLifecyclePhaseGuardDisabled:
    """guard_enabled=False → lifecycle_phase='guard-disabled', run_completion_reason set."""

    def test_guard_disabled_lifecycle_phase_is_guard_disabled(self):
        """When guarded_live_review is False, lifecycle_phase is 'guard-disabled'."""
        tmp = _isolated_tmp()
        try:
            repo = make_repo(tmp)
            repo.config.github["live_actions"] = False  # irrelevant without guard
            state = StateManager(tmp / "repos")
            state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
            engine = make_engine(repo, state)
            engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

            result = engine._run_autonomous_review_cycle(dry_run=False)

            publish = state.load_review_publish_state(repo.config.name)
            runs = publish.get("runs", {})
            latest = next((r for rid, r in runs.items() if rid.startswith("arun-")), None)
            assert latest is not None, "No run found in publish state"
            assert latest.get("lifecycle_phase") == "guard-disabled", (
                f"Expected lifecycle_phase='guard-disabled', got {latest.get('lifecycle_phase')}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_guard_disabled_run_completion_reason_contains_guard_disabled(self):
        """run_completion_reason explains guard-disabled state."""
        tmp = _isolated_tmp()
        try:
            repo = make_repo(tmp)
            state = StateManager(tmp / "repos")
            state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
            engine = make_engine(repo, state)
            engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

            engine._run_autonomous_review_cycle(dry_run=False)

            review_run_files = list(
                (state._get_state_dir(repo.config.name) / "review_runs").glob("arun-*.json")
            )
            assert review_run_files, "No review run file created"
            import json
            run_data = json.loads(review_run_files[0].read_text())
            assert "run_completion_reason" in run_data, "run_completion_reason missing from artifact"
            assert "guard-disabled" in run_data["run_completion_reason"].lower(), (
                f"run_completion_reason should mention 'guard-disabled': "
                f"{run_data.get('run_completion_reason')}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestLifecyclePhaseGuardedLivePublished:
    """guard_enabled=True + gh call succeeds → lifecycle_phase='guarded-live-published'."""

    def test_successful_gh_publish_lifecycle_phase_guarded_live_published(self):
        """gh comment succeeds → lifecycle_phase is 'guarded-live-published'."""
        tmp = _isolated_tmp()
        try:
            repo = make_repo(tmp)
            repo.config.github["live_actions"] = True
            repo.config.github["owner"] = "test-owner"
            repo.config.github["repo"] = "test-repo"
            repo.config.review_care["guarded_live_review"] = True
            state = StateManager(tmp / "repos")
            state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
            engine = make_engine(repo, state)
            engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

            def fake_run(args, **kwargs):
                if "pr" in args and "list" in args:
                    return MagicMock(
                        returncode=0,
                        stdout='[{"number": 42, "title": "PR", "updatedAt": "2026-03-29T00:00:00Z"}]',
                        stderr="",
                    )
                elif "pr" in args and "comment" in args:
                    return MagicMock(
                        returncode=0,
                        stdout="https://github.com/test-owner/test-repo/pull/42#issuecomment-123",
                        stderr="",
                    )
                return MagicMock(returncode=1, stdout="", stderr="unknown")

            with unittest.mock.patch.object(subprocess, "run", side_effect=fake_run):
                engine._run_autonomous_review_cycle(dry_run=False)

            publish = state.load_review_publish_state(repo.config.name)
            runs = publish.get("runs", {})
            latest = next((r for rid, r in runs.items() if rid.startswith("arun-")), None)
            assert latest is not None
            assert latest.get("lifecycle_phase") == "guarded-live-published", (
                f"Expected lifecycle_phase='guarded-live-published', got {latest.get('lifecycle_phase')}"
            )
            assert latest.get("comment_url") is not None, "comment_url should be set"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestLifecyclePhaseGuardedLiveFailed:
    """guard_enabled=True + gh call fails → lifecycle_phase='guarded-live-failed'."""

    def test_failed_gh_publish_lifecycle_phase_guarded_live_failed(self):
        """gh comment fails (non-transient) → lifecycle_phase is 'guarded-live-failed'."""
        tmp = _isolated_tmp()
        try:
            repo = make_repo(tmp)
            repo.config.github["live_actions"] = True
            repo.config.github["owner"] = "test-owner"
            repo.config.github["repo"] = "test-repo"
            repo.config.review_care["guarded_live_review"] = True
            state = StateManager(tmp / "repos")
            state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
            engine = make_engine(repo, state)
            engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

            def fake_run(args, **kwargs):
                if "pr" in args and "list" in args:
                    return MagicMock(
                        returncode=0,
                        stdout='[{"number": 99, "title": "PR", "updatedAt": "2026-03-29T00:00:00Z"}]',
                        stderr="",
                    )
                elif "pr" in args and "comment" in args:
                    return MagicMock(returncode=1, stdout="", stderr="auth error")
                return MagicMock(returncode=1, stdout="", stderr="unknown")

            with unittest.mock.patch.object(subprocess, "run", side_effect=fake_run):
                engine._run_autonomous_review_cycle(dry_run=False)

            publish = state.load_review_publish_state(repo.config.name)
            runs = publish.get("runs", {})
            latest = next((r for rid, r in runs.items() if rid.startswith("arun-")), None)
            assert latest is not None
            assert latest.get("lifecycle_phase") == "guarded-live-failed", (
                f"Expected lifecycle_phase='guarded-live-failed', got {latest.get('lifecycle_phase')}"
            )
            assert latest.get("status") == PublishStatus.FAILED.value, (
                f"Expected status='failed', got {latest.get('status')}"
            )
            assert "auth error" in latest.get("error", ""), (
                f"Expected 'auth error' in error, got {latest.get('error')}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCandidateSourceField:
    """candidate_source field is set in run artifacts."""

    def test_candidate_source_is_set_in_review_run_artifact(self):
        """The review run artifact contains a candidate_source field."""
        tmp = _isolated_tmp()
        try:
            repo = make_repo(tmp)
            state = StateManager(tmp / "repos")
            state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
            engine = make_engine(repo, state)
            engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

            engine._run_autonomous_review_cycle(dry_run=False)

            review_run_files = list(
                (state._get_state_dir(repo.config.name) / "review_runs").glob("arun-*.json")
            )
            assert review_run_files, "No review run file created"
            import json
            run_data = json.loads(review_run_files[0].read_text())
            assert "candidate_source" in run_data, (
                f"candidate_source missing from run artifact. Keys: {list(run_data.keys())}"
            )
            assert run_data["candidate_source"] in (
                "local-stub", "backend", "local-stub-fallback"
            ), f"Unexpected candidate_source: {run_data.get('candidate_source')}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestReviewRunArtifactFields:
    """ReviewRun artifact contains all new lifecycle fields."""

    def test_review_run_artifact_has_lifecycle_fields(self):
        """The run artifact contains run_completion_reason, lifecycle_phase, candidate_source."""
        tmp = _isolated_tmp()
        try:
            repo = make_repo(tmp)
            state = StateManager(tmp / "repos")
            state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
            engine = make_engine(repo, state)
            engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

            engine._run_autonomous_review_cycle(dry_run=False)

            review_run_files = list(
                (state._get_state_dir(repo.config.name) / "review_runs").glob("arun-*.json")
            )
            assert review_run_files, "No review run file created"
            import json
            run_data = json.loads(review_run_files[0].read_text())

            for field in ("run_completion_reason", "lifecycle_phase", "candidate_source", "comment_url"):
                assert field in run_data, f"Field '{field}' missing from run artifact"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGuardedLiveReviewDefault:
    """guarded_live_review defaults to False (safe by default)."""

    def test_guarded_live_review_default_is_false(self):
        """RepoConfig.review_care['guarded_live_review'] defaults to False."""
        tmp = _isolated_tmp()
        try:
            repo_path = tmp / "repo"
            repo_path.mkdir()
            repo = Repo(
                config=RepoConfig(
                    id="repo-test",
                    name="test-repo",
                    path=str(repo_path),
                    language="typescript",
                    # No explicit guarded_live_review
                ),
            )
            assert repo.config.review_care.get("guarded_live_review") is False, (
                f"guarded_live_review should default to False, got "
                f"{repo.config.review_care.get('guarded_live_review')!r}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEventLifecycleFields:
    """autonomous-review-completed event contains lifecycle_phase and candidate_source."""

    def test_event_contains_lifecycle_phase_and_candidate_source(self):
        """The review event emitted after the run contains lifecycle_phase and candidate_source."""
        tmp = _isolated_tmp()
        try:
            repo = make_repo(tmp)
            state = StateManager(tmp / "repos")
            state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
            engine = make_engine(repo, state)
            engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

            engine._run_autonomous_review_cycle(dry_run=False)

            events_file = state.get_review_events_file(repo.config.name)
            events = []
            if events_file.exists():
                for line in events_file.read_text().strip().split("\n"):
                    if line.strip():
                        events.append(json.loads(line))
            completed_events = [e for e in events if e.get("event") == "autonomous-review-completed"]
            assert completed_events, f"No autonomous-review-completed events found. Events: {events}"
            evt = completed_events[0]["details"]
            assert "lifecycle_phase" in evt, f"lifecycle_phase missing from event. Keys: {list(evt.keys())}"
            assert "candidate_source" in evt, f"candidate_source missing from event. Keys: {list(evt.keys())}"
            assert evt["lifecycle_phase"] == "guard-disabled", (
                f"Expected lifecycle_phase='guard-disabled', got {evt.get('lifecycle_phase')}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import shutil, tempfile
    # Run tests manually
    tmp = Path(tempfile.mkdtemp())
    try:
        import pytest
        sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
