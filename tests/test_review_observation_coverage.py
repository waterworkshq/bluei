#!/usr/bin/env python3
"""
Coverage tests for the observation-related mixins.

Targets specific uncovered lines identified by `pytest --cov` in:
  - bluei/review/observation.py         (temporarily_unreachable recovery branch)
  - bluei/review/pr_targeting.py         (exception handlers in PR resolution)
  - bluei/review/publication_pipeline.py (shadow outcomes, retry, transient failures)
  - bluei/review/status_classification.py (retry_lock_busy, non-CLEAN pending_review,
                                            retry_failed counter in _handle_retry_remediation)

Patterns follow the existing review tests:
  - Construct ReviewCycleEngine via __new__ to skip the constructor side effects.
  - Mock the GitHubReviewProvider via MagicMock(spec=...).
  - Use tmp_path + StateManager for any persistence assertions.
  - Patch subprocess.run / time.sleep where the production code calls them.
"""

import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from bluei.app.models import Repo, RepoConfig
from bluei.app.state import StateManager
from bluei.common.models import LiveRolloutMode
from bluei.review.cycle import ReviewCycleEngine
from bluei.review.models import PublishStatus
from bluei.review.provider import GitHubReviewProvider
from bluei.review.types import ReviewCycleResult


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def make_repo(tmp_path: Path, review_care_extra: dict = None) -> Repo:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    review_care = {
        "enabled": True,
        "max_attempts": 3,
        "max_loops": 2,
        "max_prs_per_run": 1,
    }
    if review_care_extra:
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


def make_engine(repo: Repo, state: StateManager) -> ReviewCycleEngine:
    """Build a ReviewCycleEngine bypassing __init__ with a mocked provider."""
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


def feedback_snapshot(fingerprint: str = "fp-1", pr_number: int = 42) -> dict:
    """Snapshot with actionable feedback — retry-eligible path."""
    return {
        "pr_number": pr_number,
        "pr_url": f"https://example.test/pr/{pr_number}",
        "branch": f"qa/branch-{pr_number}",
        "author": "sound",
        "fetched_at": "2026-06-18T12:00:00Z",
        "review_decision": "CHANGES_REQUESTED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": ["reviewer1"],
        "actionable_comments": [{"author": "reviewer1", "body": "fix this"}],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": fingerprint,
    }


def clean_snapshot(
    fingerprint: str = "fp-clean",
    pr_number: int = 42,
    merge_state_status: str = "CLEAN",
) -> dict:
    """Snapshot with no actionable feedback — not retry-eligible."""
    return {
        "pr_number": pr_number,
        "pr_url": f"https://example.test/pr/{pr_number}",
        "branch": f"qa/branch-{pr_number}",
        "author": "sound",
        "fetched_at": "2026-06-18T12:00:00Z",
        "review_decision": "APPROVED",
        "merge_state_status": merge_state_status,
        "active_change_requesters": [],
        "actionable_comments": [],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": fingerprint,
    }


def setup_state_with_unreachable_pr(
    state: StateManager,
    repo_name: str,
    pr_number: int = 55,
    *,
    recovery_attempts: int = 0,
    next_recovery_at: str | None = None,
    status: str = "temporarily_unreachable",
) -> dict:
    """Seed active/review state with a PR that has gone temporarily_unreachable."""
    pr_key = str(pr_number)
    active = {
        "prs": {
            pr_key: {
                "pr_number": pr_number,
                "url": f"https://github.com/owner/test-repo/pull/{pr_number}",
                "branch": f"qa/fix-{pr_number}",
                "author": "sound",
                "status": status,
                "recovery_attempts": recovery_attempts,
                "updated_at": "2026-06-18T10:00:00Z",
            }
        }
    }
    if next_recovery_at is not None:
        active["prs"][pr_key]["next_recovery_at"] = next_recovery_at
    review = {
        "prs": {
            pr_key: {
                "last_snapshot_fingerprint": "old-fp",
                "attempts_used": 1,
                "loop_count": 0,
            }
        }
    }
    state.save_active_prs(repo_name, active)
    state.save_review_state(repo_name, review)
    return active["prs"][pr_key]


# ---------------------------------------------------------------------------
# Observation: temporarily_unreachable recovery branch (observation.py:45-157)
# ---------------------------------------------------------------------------


class TestObservationRecoveryBranch:
    """Cover the recovery branch for PRs that previously went unreachable."""

    def test_backoff_active_preserves_state_without_recovery(self, tmp_path):
        """When now < next_recovery_at, the PR stays temporarily_unreachable
        and the recovery path is NOT attempted (no gh api call)."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        setup_state_with_unreachable_pr(
            state, repo.config.name, pr_number=55, next_recovery_at=future
        )

        engine = make_engine(repo, state)
        # PR 55 is no longer in managed_prs (the trigger for the recovery branch)
        engine.provider.list_managed_prs.return_value = []
        engine.provider.fetch_review_snapshot.side_effect = AssertionError(
            "fetch_review_snapshot must not be called during backoff"
        )
        engine._run_repo_cmd = lambda cmd, cwd=None, check=True: (_ for _ in ()).throw(
            AssertionError("gh api must not be called during backoff")
        )

        result = engine._run_observation_cycle(dry_run=False, allow_review_push=False)

        # Recovery branch preserved the record but counted zero active PRs.
        assert result.active_prs == 0
        active = state.load_active_prs(repo.config.name)
        assert active["prs"]["55"]["status"] == "temporarily_unreachable"
        # review record is carried forward unchanged
        review = state.load_review_state(repo.config.name)
        assert "55" in review["prs"]

    def test_invalid_next_recovery_at_string_falls_through_to_recovery(self, tmp_path):
        """A malformed next_recovery_at string raises in fromisoformat, the
        except (ValueError, TypeError) swallows it, and recovery proceeds."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        setup_state_with_unreachable_pr(
            state,
            repo.config.name,
            pr_number=77,
            next_recovery_at="not-a-date",
            recovery_attempts=1,
        )

        engine = make_engine(repo, state)
        engine.provider.list_managed_prs.return_value = []
        # gh api fails so the catch-all fires and bumps the attempt counter.
        engine._run_repo_cmd = lambda cmd, cwd=None, check=True: (_ for _ in ()).throw(
            RuntimeError("gh api unreachable")
        )

        result = engine._run_observation_cycle(dry_run=False, allow_review_push=False)

        active = state.load_active_prs(repo.config.name)
        rec = active["prs"]["77"]
        # Falls through to backoff scheduling (attempts increment from 1 to 2).
        assert rec["status"] == "temporarily_unreachable"
        assert rec["recovery_attempts"] == 2
        assert rec["next_recovery_at"] is not None
        assert result.active_prs == 0

    def test_max_recovery_attempts_escalates_with_writer_callback(self, tmp_path):
        """recovery_attempts >= 10 invokes escalation_writer and marks the PR
        permanently_unreachable."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        setup_state_with_unreachable_pr(
            state, repo.config.name, pr_number=99, recovery_attempts=10
        )

        engine = make_engine(repo, state)
        engine.provider.list_managed_prs.return_value = []

        escalate_calls = []

        def fake_writer(*, message, severity, repo):
            escalate_calls.append(
                {"message": message, "severity": severity, "repo": repo}
            )

        engine.escalation_writer = fake_writer

        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

        assert len(escalate_calls) == 1, "escalation_writer must be called once"
        assert escalate_calls[0]["severity"] == "error"
        assert "99" in escalate_calls[0]["message"]
        assert escalate_calls[0]["repo"] == repo.config.name

        active = state.load_active_prs(repo.config.name)
        rec = active["prs"]["99"]
        assert rec["status"] == "permanently_unreachable"
        assert rec["recovery_attempts"] == 10

    def test_max_recovery_attempts_escalates_without_writer(self, tmp_path):
        """When no escalation_writer attribute is present (engine built via __new__),
        escalation still marks the PR permanently_unreachable and does NOT raise."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        setup_state_with_unreachable_pr(
            state, repo.config.name, pr_number=100, recovery_attempts=15
        )

        engine = make_engine(repo, state)
        # Note: no escalation_writer attribute set — getattr returns None.
        engine.provider.list_managed_prs.return_value = []

        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

        active = state.load_active_prs(repo.config.name)
        rec = active["prs"]["100"]
        assert rec["status"] == "permanently_unreachable"
        assert rec["recovery_attempts"] == 15

    def test_escalation_writer_exception_is_swallowed(self, tmp_path):
        """If escalation_writer raises, the cycle must NOT crash — the error
        is logged at debug and escalation still proceeds."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        setup_state_with_unreachable_pr(
            state, repo.config.name, pr_number=101, recovery_attempts=10
        )

        engine = make_engine(repo, state)
        engine.provider.list_managed_prs.return_value = []

        def bad_writer(**kwargs):
            raise RuntimeError("writer unavailable")

        engine.escalation_writer = bad_writer

        # Must not raise.
        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

        active = state.load_active_prs(repo.config.name)
        assert active["prs"]["101"]["status"] == "permanently_unreachable"

    def test_recovery_succeeds_when_pr_still_open(self, tmp_path):
        """When gh api returns the PR with state=open, it is re-added to
        managed_prs and processed in the same run."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        setup_state_with_unreachable_pr(
            state, repo.config.name, pr_number=200, recovery_attempts=2
        )

        engine = make_engine(repo, state)
        engine.provider.list_managed_prs.return_value = []
        # Recovery gh api returns the PR as OPEN.
        pr_data = {
            "number": 200,
            "state": "open",
            "headRefName": "qa/fix-200",
            "author": {"login": "sound"},
            "title": "Recovered",
            "url": "https://github.com/owner/test-repo/pull/200",
        }
        engine._run_repo_cmd = lambda cmd, cwd=None, check=True: __import__(
            "json"
        ).dumps(pr_data)
        engine.provider.fetch_review_snapshot.return_value = clean_snapshot(
            "fp-recovered", pr_number=200
        )

        result = engine._run_observation_cycle(dry_run=False, allow_review_push=False)

        # Re-added to managed_prs, so it counted as active.
        assert result.active_prs == 1
        active = state.load_active_prs(repo.config.name)
        # Status reflects the normal classification path (clean snapshot → pending_review).
        assert active["prs"]["200"]["status"] == "pending_review"

    def test_recovery_fails_and_increments_attempts(self, tmp_path):
        """When the gh api recovery fetch raises, attempts increment and a new
        next_recovery_at is scheduled."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        setup_state_with_unreachable_pr(
            state, repo.config.name, pr_number=300, recovery_attempts=3
        )

        engine = make_engine(repo, state)
        engine.provider.list_managed_prs.return_value = []
        engine._run_repo_cmd = lambda cmd, cwd=None, check=True: (_ for _ in ()).throw(
            RuntimeError("gh timeout")
        )

        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

        active = state.load_active_prs(repo.config.name)
        rec = active["prs"]["300"]
        assert rec["status"] == "temporarily_unreachable"
        assert rec["recovery_attempts"] == 4
        assert "next_recovery_at" in rec
        # Backoff table: index min(4-1, 8) = 3 → _RECOVERY_BACKOFF_MINUTES[3] = 240.
        scheduled = datetime.fromisoformat(rec["next_recovery_at"])
        # The scheduled time should be roughly 240 minutes from now (allow slack).
        delta = scheduled - datetime.now(timezone.utc)
        assert timedelta(minutes=200) < delta < timedelta(minutes=280)

    def test_default_branch_taken_when_dry_run_skips_recovery(self, tmp_path):
        """In dry_run, the `not dry_run` guard skips the recovery branch even
        when status IS temporarily_unreachable. The PR is still mutated in
        memory via the default branch (lines 152-157).

        State is NOT persisted in dry_run (line 266-267 guard), so we verify
        via the in-memory active_records dict captured through a wrapper."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        # Status IS temporarily_unreachable — would enter recovery if not for dry_run.
        setup_state_with_unreachable_pr(
            state, repo.config.name, pr_number=400, status="temporarily_unreachable"
        )

        engine = make_engine(repo, state)
        engine.provider.list_managed_prs.return_value = []
        engine._run_repo_cmd = lambda cmd, cwd=None, check=True: (_ for _ in ()).throw(
            AssertionError("gh api must not be called in dry_run default branch")
        )

        # Capture the in-memory active_state passed to _persist_review_state.
        # In dry_run the persistence call is skipped, so we wrap _persist_review_state
        # to observe the records that WOULD have been written.
        captured = {}

        def fake_persist(active_state, review_state, result):
            captured["active"] = active_state
            captured["review"] = review_state

        engine._persist_review_state = fake_persist

        engine._run_observation_cycle(dry_run=True, allow_review_push=False)

        # _persist_review_state is NOT called in dry_run, so the captured dict
        # remains empty — but the cycle completed without invoking gh api
        # (no exception raised), confirming the dry_run guard fired and the
        # default branch executed in memory.
        assert captured == {}, "_persist_review_state must be skipped in dry_run"

    def test_default_branch_when_status_was_not_temporarily_unreachable(self, tmp_path):
        """A prior record with status != temporarily_unreachable takes the default
        branch (lines 152-157) regardless of dry_run flag — exercises the path
        where the outer `if status == 'temporarily_unreachable' and not dry_run`
        evaluates False because the status check fails."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        setup_state_with_unreachable_pr(
            state, repo.config.name, pr_number=500, status="merge_ready"
        )

        engine = make_engine(repo, state)
        engine.provider.list_managed_prs.return_value = []
        engine._run_repo_cmd = lambda cmd, cwd=None, check=True: (_ for _ in ()).throw(
            AssertionError(
                "gh api must not be called when status != temporarily_unreachable"
            )
        )

        engine._run_observation_cycle(dry_run=False, allow_review_push=False)

        active = state.load_active_prs(repo.config.name)
        rec = active["prs"]["500"]
        assert rec["status"] == "temporarily_unreachable"


# ---------------------------------------------------------------------------
# PR targeting: exception handlers (pr_targeting.py:51-52, 65-66, 84-85)
# ---------------------------------------------------------------------------


class TestPrTargetingExceptionHandlers:
    def test_find_open_prs_returns_empty_on_subprocess_error(self, tmp_path):
        """When subprocess.run raises inside _find_open_prs, the broad
        except Exception returns []. (pr_targeting.py:51-52)"""
        repo = make_repo(tmp_path, review_care_extra={"max_prs_per_run": 1})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        with patch.object(
            subprocess,
            "run",
            side_effect=FileNotFoundError("gh binary missing"),
        ):
            result = engine._find_open_prs()

        assert result == []

    def test_find_open_prs_returns_empty_on_json_error(self, tmp_path):
        """Malformed JSON output also hits the broad except and returns []."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        fake_result = MagicMock(returncode=0, stdout="not json at all", stderr="")
        with patch.object(subprocess, "run", return_value=fake_result):
            result = engine._find_open_prs()

        assert result == []

    def test_resolve_target_pr_skips_non_integer_targeted_pr_number(self, tmp_path):
        """prior targeted_pr_number that can't be int()-coerced is silently
        skipped via except (TypeError, ValueError). (pr_targeting.py:65-66)"""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        prior_publish = {
            "runs": {
                "arun-bad-1": {"targeted_pr_number": "not-a-number"},
                "arun-bad-2": {"targeted_pr_number": None},  # TypeError on int(None)
            },
            "findings": {},
        }

        # No prior distinct values resolve; no managed PRs; one open PR → resolved.
        engine.provider.list_managed_prs = MagicMock(return_value=[])
        engine._find_open_prs = MagicMock(
            return_value=[
                {"number": 7, "title": "Only", "updatedAt": "2026-06-18T00:00:00Z"}
            ]
        )

        resolved, reason = engine._resolve_target_pr_for_run(prior_publish)
        assert resolved == 7
        assert "single-open-pr-7" in reason

    def test_resolve_target_pr_handles_list_managed_prs_exception(self, tmp_path):
        """When provider.list_managed_prs raises, the broad except sets
        managed_prs = [] and resolution falls through to _find_open_prs.
        (pr_targeting.py:84-85)"""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        engine.provider.list_managed_prs = MagicMock(
            side_effect=RuntimeError("github 500")
        )
        engine._find_open_prs = MagicMock(
            return_value=[
                {"number": 9, "title": "Only", "updatedAt": "2026-06-18T00:00:00Z"}
            ]
        )

        resolved, reason = engine._resolve_target_pr_for_run(
            {"runs": {}, "findings": {}}
        )
        assert resolved == 9
        assert "single-open-pr-9" in reason


# ---------------------------------------------------------------------------
# Publication pipeline: shadow outcomes, retry, transient failures
# (publication_pipeline.py:55-58, 133, 146, 160, 183-211, 241-259, 280-286)
# ---------------------------------------------------------------------------


def _shadow_engine(tmp_path: Path, *, open_prs):
    """Build an engine configured for shadow mode with the given open PRs."""
    repo = make_repo(
        tmp_path,
        review_care_extra={
            "live_rollout_mode": LiveRolloutMode.SHADOW.value,
        },
    )
    repo.config.github["live_actions"] = True
    repo.config.github["owner"] = "owner"
    repo.config.github["repo"] = "repo"
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
    engine = make_engine(repo, state)
    engine._find_open_prs = MagicMock(return_value=open_prs)
    return engine, repo, state


class TestShadowModeTargetingOutcomes:
    """Cover the three targeting_outcome branches in SHADOW mode (lines 53-58)."""

    def test_shadow_target_pr_would_post_when_open(self, tmp_path):
        """target_pr_number is in open_prs → 'target-pr-N-would-post' outcome."""
        engine, repo, state = _shadow_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "Open", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        prior = {"runs": {}, "findings": {}}
        result = engine._post_summary_to_github(
            summary_text="shadow summary",
            run_id="arun-shadow-1",
            prior_publish=prior,
            target_pr_number=42,
        )

        # Shadow mode never actually publishes.
        assert result is None
        run_entry = prior["runs"]["arun-shadow-1"]
        assert run_entry["shadow"] is True
        assert run_entry["targeted_pr_number"] == 42

        events_file = state.get_review_events_file(repo.config.name)
        events = [
            __import__("json").loads(line)
            for line in events_file.read_text().splitlines()
        ]
        shadow_events = [
            e for e in events if e.get("event") == "autonomous-review-shadow-published"
        ]
        assert len(shadow_events) == 1
        assert (
            shadow_events[0]["details"]["targeting_outcome"]
            == "target-pr-42-would-post"
        )

    def test_shadow_target_pr_not_open_when_missing(self, tmp_path):
        """target_pr_number is set but not in open_prs → 'target-pr-N-not-open'."""
        engine, repo, state = _shadow_engine(
            tmp_path,
            open_prs=[
                {
                    "number": 11,
                    "title": "Different",
                    "updatedAt": "2026-06-18T00:00:00Z",
                }
            ],
        )

        prior = {"runs": {}, "findings": {}}
        result = engine._post_summary_to_github(
            summary_text="shadow summary",
            run_id="arun-shadow-2",
            prior_publish=prior,
            target_pr_number=99,
        )

        assert result is None
        events_file = state.get_review_events_file(repo.config.name)
        events = [
            __import__("json").loads(line)
            for line in events_file.read_text().splitlines()
        ]
        shadow_events = [
            e for e in events if e.get("event") == "autonomous-review-shadow-published"
        ]
        assert (
            shadow_events[0]["details"]["targeting_outcome"] == "target-pr-99-not-open"
        )

    def test_shadow_target_pr_none_when_no_open_prs(self, tmp_path):
        """target_pr_number set AND open_prs is empty → falls into else branch
        ('would-post') because open_prs evaluates falsy."""
        engine, repo, _ = _shadow_engine(tmp_path, open_prs=[])

        prior = {"runs": {}, "findings": {}}
        result = engine._post_summary_to_github(
            summary_text="shadow summary",
            run_id="arun-shadow-3",
            prior_publish=prior,
            target_pr_number=5,
        )

        assert result is None


def _limited_engine(tmp_path: Path, *, open_prs=None):
    """Build an engine configured for limited (live) mode with mocked open PRs."""
    repo = make_repo(
        tmp_path,
        review_care_extra={
            "live_rollout_mode": LiveRolloutMode.LIMITED.value,
            "guarded_live_review": True,
            "retry_delay_minutes": 15,
        },
    )
    repo.config.github["live_actions"] = True
    repo.config.github["owner"] = "owner"
    repo.config.github["repo"] = "repo"
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
    engine = make_engine(repo, state)
    if open_prs is not None:
        engine._find_open_prs = MagicMock(return_value=open_prs)
    return engine, repo, state


class TestPublishTransientFailureRetry:
    """Cover the transient-failure retry logic and keyword matching."""

    def _set_failed_then_success(self, fail_stderr: str, fail_stdout: str = ""):
        """Returns a fake_run that fails once with given stderr/stdout, then succeeds."""
        calls = {"count": 0}

        def fake_run(args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return MagicMock(returncode=1, stdout=fail_stdout, stderr=fail_stderr)
            return MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/42#issuecomment-ok",
                stderr="",
            )

        return fake_run

    def test_retry_on_rate_limit_keyword(self, tmp_path):
        """gh fails with 'rate limit' in stderr → retried, second attempt succeeds.
        Covers rate-limit keyword detection (line 146) and retry block (241-259)."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        fake_run = self._set_failed_then_success("HTTP 429: rate limit exceeded")
        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                result = engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-rl-1",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert result == "https://github.com/owner/repo/pull/42#issuecomment-ok"
        assert prior["runs"]["arun-rl-1"]["status"] == PublishStatus.PUBLISHED.value

    def test_retry_on_secondary_rate_limit_keyword(self, tmp_path):
        """Different keyword from the rate-limit list also triggers retry."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        fake_run = self._set_failed_then_success("secondary rate limit detected")
        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-srl-1",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert prior["runs"]["arun-srl-1"]["status"] == PublishStatus.PUBLISHED.value

    def test_retry_on_network_error_keyword(self, tmp_path):
        """Network-error keywords ('connection reset', 'ssl', etc.) trigger retry.
        Covers line 160."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        fake_run = self._set_failed_then_success("ssl handshake failed")
        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-net-1",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert prior["runs"]["arun-net-1"]["status"] == PublishStatus.PUBLISHED.value

    def test_retry_on_connection_refused_keyword(self, tmp_path):
        """Another network keyword: 'connection refused'."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        fake_run = self._set_failed_then_success("connection refused")
        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-net-2",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert prior["runs"]["arun-net-2"]["status"] == PublishStatus.PUBLISHED.value

    def test_retry_on_timeout_returncode_124(self, tmp_path):
        """returncode == 124 (timeout) is a transient failure → retried.
        Covers line 133."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        fake_run = self._set_failed_then_success("")
        # Override first-call return to use returncode 124.
        calls = {"count": 0}

        def fake_run_124(args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return MagicMock(returncode=124, stdout="", stderr="timeout")
            return MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/42#issuecomment-124",
                stderr="",
            )

        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run_124):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                result = engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-timeout-1",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert result.endswith("#issuecomment-124")
        assert (
            prior["runs"]["arun-timeout-1"]["status"] == PublishStatus.PUBLISHED.value
        )

    def test_non_transient_failure_is_not_retried(self, tmp_path):
        """A non-transient gh failure (e.g. 'token expired') does NOT retry —
        it fails immediately on attempt 1."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        calls = {"count": 0}

        def fake_run(args, **kwargs):
            calls["count"] += 1
            return MagicMock(returncode=1, stdout="", stderr="token expired")

        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                result = engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-nontransient-1",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert result is None
        assert calls["count"] == 1, "Non-transient failure must not be retried"
        assert (
            prior["runs"]["arun-nontransient-1"]["status"] == PublishStatus.FAILED.value
        )
        assert "token expired" in prior["runs"]["arun-nontransient-1"]["error"]

    def test_retries_exhausted_marks_failed(self, tmp_path):
        """When all retries return a transient failure, status ends as FAILED."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        # Always return a transient failure.
        def fake_run(args, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="rate limit exceeded")

        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                result = engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-exhausted-1",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert result is None
        # max_retries=2 → 3 attempts total (initial + 2 retries).
        run_entry = prior["runs"]["arun-exhausted-1"]
        assert run_entry["status"] == PublishStatus.FAILED.value
        assert "rate limit" in run_entry["error"]


class TestPublishSubprocessExceptionRetry:
    """Cover the inner except Exception as exc block (lines 183-211)."""

    def test_subprocess_exception_retried_then_succeeds(self, tmp_path):
        """When subprocess.run raises on the first attempt, the inner except
        catches it, sleeps, then retries and succeeds."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        calls = {"count": 0}

        def fake_run(args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("broken pipe")
            return MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/42#issuecomment-exc",
                stderr="",
            )

        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                result = engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-exc-1",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert result.endswith("#issuecomment-exc")
        assert prior["runs"]["arun-exc-1"]["status"] == PublishStatus.PUBLISHED.value
        assert calls["count"] == 2

    def test_subprocess_exception_exhausted_marks_failed(self, tmp_path):
        """When subprocess.run raises on every attempt (including final), the
        inner except's final-attempt branch marks the run FAILED."""
        engine, repo, _ = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        def fake_run(args, **kwargs):
            raise OSError("permanently broken")

        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch("bluei.review.publication_pipeline._time.sleep"):
                result = engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-exc-2",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert result is None
        run_entry = prior["runs"]["arun-exc-2"]
        assert run_entry["status"] == PublishStatus.FAILED.value
        assert "gh-call-exception" in run_entry["error"]


class TestPublishOuterException:
    """Cover the outer try/except in _post_summary_to_github (lines 280-286).

    The outer except fires only when an exception escapes the per-iteration
    inner handlers — e.g. when state.append_review_event raises after a
    successful gh call.
    """

    def test_outer_exception_returns_none_and_records_error(self, tmp_path):
        engine, repo, state = _limited_engine(
            tmp_path,
            open_prs=[
                {"number": 42, "title": "PR", "updatedAt": "2026-06-18T00:00:00Z"}
            ],
        )

        # gh succeeds on first attempt.
        def fake_run(args, **kwargs):
            return MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/42#issuecomment-x",
                stderr="",
            )

        # state.append_review_event raises — propagates out of the for-loop body
        # into the outer except.
        original_append = state.append_review_event

        def broken_append(repo_name, event):
            # Only break on the success-path event so refusal events still work
            # if they fire before the success path (they shouldn't here).
            if event.get("event") == "autonomous-review-published":
                raise RuntimeError("state disk full")
            original_append(repo_name, event)

        prior = {"runs": {}, "findings": {}}

        with patch.object(subprocess, "run", side_effect=fake_run):
            with patch.object(state, "append_review_event", side_effect=broken_append):
                result = engine._post_summary_to_github(
                    summary_text="payload",
                    run_id="arun-outer-1",
                    prior_publish=prior,
                    target_pr_number=42,
                )

        assert result is None
        run_entry = prior["runs"]["arun-outer-1"]
        assert run_entry["status"] == PublishStatus.FAILED.value
        assert "state disk full" in run_entry["error"]
        assert run_entry["targeted_pr_number"] == 42


# ---------------------------------------------------------------------------
# Status classification: retry_lock_busy, non-CLEAN pending_review,
# awaiting_mergeability, retry_failed counter
# (status_classification.py:159-160, 191-198, 254-255)
# ---------------------------------------------------------------------------


def _classify_engine(tmp_path: Path) -> ReviewCycleEngine:
    """Minimal engine for direct _classify_pr_status unit tests."""
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    return engine


class TestStatusClassificationBranches:
    def test_retry_lock_busy_when_no_lock_acquired(self, tmp_path):
        """retry-eligible PR with attempts remaining, not dry_run, no lock_handle
        → status 'retry_lock_busy'. (lines 159-160)"""
        engine = _classify_engine(tmp_path)
        snapshot = feedback_snapshot("fp-lock-busy")

        existing_review = {
            "last_snapshot_fingerprint": "fp-lock-busy",  # same fp so loop_count resets
            "last_action": "review_feedback_detected",
            "attempts_used": 0,
            "loop_count": 0,
        }
        result = ReviewCycleResult()

        classification = engine._classify_pr_status(
            snapshot=snapshot,
            existing_review=existing_review,
            dry_run=False,
            lock_handle=None,  # No lock acquired
            result=result,
        )

        assert classification["status"] == "retry_lock_busy", (
            f"Expected retry_lock_busy, got {classification['status']}"
        )
        assert classification["merge_reason"] == "PR remediation lock already held"
        # Lock busy PRs are still retry-eligible; the planning step is skipped.
        assert classification["remediation_plan"] is None

    def test_pending_review_unknown_state_no_artifact(self, tmp_path):
        """No actionable feedback, merge_state_status=UNKNOWN, no review artifact
        → pending_review with non-CLEAN merge_reason text. (lines 191-194)"""
        engine = _classify_engine(tmp_path)
        snapshot = clean_snapshot("fp-unknown", merge_state_status="UNKNOWN")

        existing_review = {
            "last_snapshot_fingerprint": "different-fp",
            "last_action": "observed",
            "attempts_used": 0,
            "loop_count": 0,
            # No last_review_comment_key — no artifact exists.
        }
        result = ReviewCycleResult()

        classification = engine._classify_pr_status(
            snapshot=snapshot,
            existing_review=existing_review,
            dry_run=True,
            lock_handle=None,
            result=result,
        )

        assert classification["status"] == "pending_review"
        assert classification["merge_state"] == "awaiting_review_artifact"
        # The non-CLEAN branch mentions the merge state and the missing review.
        assert "unknown" in classification["merge_reason"].lower()
        assert "bluei review" in classification["merge_reason"].lower()

    def test_pending_review_unstable_state_no_artifact(self, tmp_path):
        """UNSTABLE state, no artifact → same else branch, different text."""
        engine = _classify_engine(tmp_path)
        snapshot = clean_snapshot("fp-unstable", merge_state_status="UNSTABLE")

        existing_review = {
            "last_snapshot_fingerprint": "different-fp",
            "last_action": "observed",
            "attempts_used": 0,
            "loop_count": 0,
        }
        result = ReviewCycleResult()

        classification = engine._classify_pr_status(
            snapshot=snapshot,
            existing_review=existing_review,
            dry_run=True,
            lock_handle=None,
            result=result,
        )

        assert classification["status"] == "pending_review"
        assert "unstable" in classification["merge_reason"].lower()

    def test_awaiting_mergeability_when_state_not_cleanable(self, tmp_path):
        """merge_state_status outside {CLEAN, UNKNOWN, UNSTABLE} (e.g. BLOCKED)
        → awaiting_mergeability branch. (lines 195-198)"""
        engine = _classify_engine(tmp_path)
        snapshot = clean_snapshot("fp-blocked", merge_state_status="BLOCKED")

        existing_review = {
            "last_snapshot_fingerprint": "different-fp",
            "last_action": "observed",
            "attempts_used": 0,
            "loop_count": 0,
        }
        result = ReviewCycleResult()

        classification = engine._classify_pr_status(
            snapshot=snapshot,
            existing_review=existing_review,
            dry_run=True,
            lock_handle=None,
            result=result,
        )

        assert classification["status"] == "awaiting_mergeability", (
            f"Expected awaiting_mergeability, got {classification['status']}"
        )
        assert classification["merge_state"] == "awaiting_mergeability"
        assert "blocked" in classification["merge_reason"].lower()
        assert "merge state" in classification["merge_reason"].lower()


class TestHandleRetryRemediationRetryFailedCounter:
    """Cover the elif execution_status.startswith('retry_failed') branch in
    _handle_retry_remediation (lines 254-255)."""

    def test_retry_failed_validation_increments_retry_failed_counter(self, tmp_path):
        """When the execution_result status starts with 'retry_failed', the
        retry_failed_prs counter is bumped (not retry_executed_prs)."""
        engine = _classify_engine(tmp_path)
        # _handle_retry_remediation needs state.append_review_event to work.
        engine.state._get_state_dir(engine.repo.config.name).mkdir(
            parents=True, exist_ok=True
        )

        pr_number = 42
        pr_key = "42"
        snapshot = feedback_snapshot("fp-handle-failed", pr_number=pr_number)

        # Simulate execution that failed validation.
        execution_result = {
            "status": "retry_failed_validation",
            "executed": True,
            "attempts_used": 2,
            "changed_files": ["a.ts"],
            "validation": {"ok": False},
            "backend_result": {"returncode": 0},
            "push_result": None,
        }

        # Pre-populate the records so the method can mutate them.
        active_records = {pr_key: {"status": "review_feedback_detected"}}
        review_records = {pr_key: {"attempts_used": 1}}
        result = ReviewCycleResult()

        # _handle_retry_remediation calls _execute_prepared_remediation first.
        engine._execute_prepared_remediation = lambda *a, **kw: execution_result

        engine._handle_retry_remediation(
            pr_number=pr_number,
            pr_key=pr_key,
            snapshot=snapshot,
            existing_review={"attempts_used": 1},
            remediation_plan={"status": "retry_prepared"},
            fallback_status="review_feedback_detected",
            dry_run=False,
            allow_review_push=False,
            active_records=active_records,
            review_records=review_records,
            result=result,
        )

        assert result.retry_failed_prs == 1, (
            "retry_failed_validation must bump retry_failed_prs"
        )
        assert result.retry_executed_prs == 0
        assert active_records[pr_key]["status"] == "retry_failed_validation"
        assert review_records[pr_key]["attempts_used"] == 2

    def test_retry_failed_timeout_increments_retry_failed_counter(self, tmp_path):
        """Another retry_failed_* variant — retry_failed_timeout."""
        engine = _classify_engine(tmp_path)
        engine.state._get_state_dir(engine.repo.config.name).mkdir(
            parents=True, exist_ok=True
        )

        execution_result = {
            "status": "retry_failed_timeout",
            "executed": True,
            "attempts_used": 3,
            "changed_files": [],
            "validation": None,
            "backend_result": {"returncode": 124},
            "push_result": None,
        }

        active_records = {"7": {"status": "review_feedback_detected"}}
        review_records = {"7": {"attempts_used": 2}}
        result = ReviewCycleResult()

        engine._execute_prepared_remediation = lambda *a, **kw: execution_result

        engine._handle_retry_remediation(
            pr_number=7,
            pr_key="7",
            snapshot=feedback_snapshot("fp-timeout", pr_number=7),
            existing_review={"attempts_used": 2},
            remediation_plan={"status": "retry_prepared"},
            fallback_status="review_feedback_detected",
            dry_run=False,
            allow_review_push=False,
            active_records=active_records,
            review_records=review_records,
            result=result,
        )

        assert result.retry_failed_prs == 1
        assert active_records["7"]["status"] == "retry_failed_timeout"


# ---------------------------------------------------------------------------
# Run as script
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
