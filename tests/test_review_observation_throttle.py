#!/usr/bin/env python3
"""
Tests for the inter-PR delay throttle in _run_observation_cycle.

Verifies:
- When review_care.inter_pr_delay_seconds is set, time.sleep is called BETWEEN
  PRs (N-1 times for N PRs), but NOT before the first PR.
- When inter_pr_delay_seconds is 0 or unset (default), time.sleep is never
  called from the inter-PR throttle path.
- Dry-run mode skips the delay even when configured.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from bluei.app.models import Repo, RepoConfig
from bluei.review.cycle import ReviewCycleEngine
from bluei.review.provider import GitHubReviewProvider
from bluei.app.state import StateManager


def _make_repo(tmp_path: Path, review_care_extra: dict) -> Repo:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    review_care = {
        "enabled": True,
        "max_attempts": 3,
        "max_loops": 2,
        "max_prs_per_run": 5,
    }
    review_care.update(review_care_extra)
    return Repo(
        config=RepoConfig(
            id="repo-test",
            name="test-repo",
            path=str(repo_path),
            language="typescript",
            review_care=review_care,
        )
    )


def _make_engine(repo: Repo, state: StateManager) -> ReviewCycleEngine:
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    provider = MagicMock(spec=GitHubReviewProvider)
    provider.repo = repo
    provider.state = state
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/test-repo"
    provider.current_login = "sound"
    engine.provider = provider
    return engine


def _three_prs():
    return [
        {
            "number": n,
            "url": f"https://github.com/owner/test-repo/pull/{n}",
            "headRefName": f"qa/branch-{n}",
            "author": {"login": "sound"},
            "title": f"PR {n}",
            "isDraft": False,
            "state": "OPEN",
        }
        for n in (101, 102, 103)
    ]


def _snapshot_for(pr_number: int) -> dict:
    return {
        "pr_number": pr_number,
        "pr_url": f"https://github.com/owner/test-repo/pull/{pr_number}",
        "branch": f"qa/branch-{pr_number}",
        "author": "sound",
        "fetched_at": "2026-06-14T12:00:00Z",
        "review_decision": "APPROVED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": [],
        "actionable_comments": [],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": f"fp-{pr_number}",
    }


def test_inter_pr_delay_applied_between_prs(tmp_path):
    """When configured, _time.sleep is called N-1 times for N PRs."""
    repo = _make_repo(tmp_path, {"inter_pr_delay_seconds": 1.5})
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = _make_engine(repo, state)
    engine.provider.list_managed_prs.return_value = _three_prs()
    engine.provider.fetch_review_snapshot.side_effect = lambda n: _snapshot_for(n)

    with patch("bluei.review.observation._time.sleep") as mock_sleep:
        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

    # 3 PRs => 2 sleeps (between PR 1-2 and PR 2-3). Not before the first PR.
    assert mock_sleep.call_count == 2, (
        f"Expected 2 inter-PR sleeps for 3 PRs, got {mock_sleep.call_count}"
    )
    mock_sleep.assert_called_with(1.5)


def test_inter_pr_delay_not_applied_when_unset(tmp_path):
    """Default (0 / unset) must NOT introduce any sleep — preserves behavior."""
    repo = _make_repo(tmp_path, {})
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = _make_engine(repo, state)
    engine.provider.list_managed_prs.return_value = _three_prs()
    engine.provider.fetch_review_snapshot.side_effect = lambda n: _snapshot_for(n)

    with patch("bluei.review.observation._time.sleep") as mock_sleep:
        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

    assert mock_sleep.call_count == 0, (
        f"Default (0) delay must not sleep; got {mock_sleep.call_count} calls"
    )


def test_inter_pr_delay_not_applied_when_zero(tmp_path):
    """Explicit 0 must behave identically to unset."""
    repo = _make_repo(tmp_path, {"inter_pr_delay_seconds": 0})
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = _make_engine(repo, state)
    engine.provider.list_managed_prs.return_value = _three_prs()
    engine.provider.fetch_review_snapshot.side_effect = lambda n: _snapshot_for(n)

    with patch("bluei.review.observation._time.sleep") as mock_sleep:
        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

    assert mock_sleep.call_count == 0


def test_inter_pr_delay_skipped_in_dry_run(tmp_path):
    """Dry-run mode makes no real API calls, so the throttle should be skipped."""
    repo = _make_repo(tmp_path, {"inter_pr_delay_seconds": 5.0})
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = _make_engine(repo, state)
    engine.provider.list_managed_prs.return_value = _three_prs()
    engine.provider.fetch_review_snapshot.side_effect = lambda n: _snapshot_for(n)

    with patch("bluei.review.observation._time.sleep") as mock_sleep:
        engine._run_observation_cycle(dry_run=True, allow_review_push=False)

    assert mock_sleep.call_count == 0, (
        f"Dry-run must skip inter-PR delay; got {mock_sleep.call_count} calls"
    )


def test_inter_pr_delay_single_pr_no_sleep(tmp_path):
    """One PR means no inter-PR gap, so zero sleeps even when configured."""
    repo = _make_repo(tmp_path, {"inter_pr_delay_seconds": 2.0})
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = _make_engine(repo, state)
    engine.provider.list_managed_prs.return_value = [_three_prs()[0]]
    engine.provider.fetch_review_snapshot.side_effect = lambda n: _snapshot_for(n)

    with patch("bluei.review.observation._time.sleep") as mock_sleep:
        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

    assert mock_sleep.call_count == 0
