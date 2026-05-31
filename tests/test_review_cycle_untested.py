"""Tests for previously untested methods in bluei.review.cycle.ReviewCycleEngine."""

from pathlib import Path

import pytest

from bluei.app.models import Repo, RepoConfig, RepoStatus
from bluei.app.state import StateManager
from bluei.review.cycle import ReviewCycleEngine


def _make_test_repo(repo_path):
    config = RepoConfig(
        id="test-repo", name="test-repo", path=str(repo_path), language="python"
    )
    return Repo(
        config=config,
        status=RepoStatus.READY,
        onboarded_at="2026-01-01T00:00:00Z",
        last_run_at=None,
        baseline=None,
        current_findings_count=0,
        current_health_score=100,
        total_fixes=0,
        total_prs=0,
        total_merges=0,
    )


@pytest.fixture
def engine(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()

    monkeypatch.setattr(
        "bluei.review.provider.GitHubReviewProvider._run",
        lambda self, cmd: "",
    )
    monkeypatch.setattr(
        "bluei.review.provider.GitHubReviewProvider._get_repo_slug",
        lambda self: "test/test-repo",
    )

    return ReviewCycleEngine(
        repo=_make_test_repo(repo_path),
        state=StateManager(repos_dir=repos_dir),
    )


class TestReviewCycleEngine:
    def test_instantiation(self, engine):
        assert engine is not None
        assert engine.__dict__.get("_mnemo_cache", {}) == {}

    def test_get_review_mode(self, engine):
        mode = engine._get_review_mode()
        assert isinstance(mode, str)

    def test_mnemo_available(self, engine):
        result = engine._mnemo_available()
        assert isinstance(result, bool)

    def test_guess_validation_commands(self, engine):
        commands = engine._guess_validation_commands()
        assert isinstance(commands, list)

    def test_render_backend_command_default(self, engine, tmp_path):
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test")
        cmd = engine._render_backend_command(prompt_file)
        assert isinstance(cmd, str)

    def test_review_worktree_path(self, engine):
        p = engine._review_worktree_path(79)
        assert "79" in str(p)

    def test_review_lock_path(self, engine):
        p = engine._review_lock_path(79)
        assert ".lock" in str(p)
