#!/usr/bin/env python3
"""Tests for Phase G6: Limited publish filters and monitored-rollout signals.

Run with:
    PYTHONPATH=. .venv/bin/python -m pytest tests/test_review_publish_filters.py -v

These tests verify that:
1. limited mode + passing filters → publishes (rollout_eligible=True)
2. limited mode + failing max_findings_count → falls back safely
3. limited mode + failing require_pr_context → falls back safely
4. limited mode + failing severity subset → falls back safely
5. limited mode + failing header subset → falls back safely
6. shadow mode → runs filter check for observability but never goes live
7. local_only mode → bypasses filters (rollout_eligible=False)
8. State/events capture filter outcome and rollout monitoring fields
9. observation mode unchanged
"""

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _isolated_tmp() -> Path:
    """Return a unique isolated temp directory per test."""
    base = Path(f"/tmp/qa_publish_filter_{uuid.uuid4().hex[:8]}")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base


from qa_agent.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    LiveRolloutMode,
    FindingSource,
    FindingActionability,
    FindingSeverity,
    PublishStatus,
    generate_id,
)
from qa_agent.review import (
    ReviewCycleEngine,
    GitHubReviewProvider,
    PublishFilterResult,
    _build_pass_filter_result,
    normalize_candidate,
    assign_finding_identity,
)
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Stub candidates used across tests
# ---------------------------------------------------------------------------

STUB_FINDINGS__LOW_ONLY = [
    {
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 10,
        "header": "outstanding-todo",
        "snippet": "# TODO: refactor",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.LOW.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.7,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
    {
        "repo": "test-repo",
        "path": "src/utils.ts",
        "line": 42,
        "header": "excessively-long-line",
        "snippet": "x = 1",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.LOW.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.6,
        "safe_to_autofix": True,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
]

STUB_FINDINGS__MIXED_SEVERITY = [
    {
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 10,
        "header": "outstanding-todo",
        "snippet": "# TODO: refactor",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.MEDIUM.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.7,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
    {
        "repo": "test-repo",
        "path": "src/security.kt",
        "line": 20,
        "header": "hardcoded-secret",
        "snippet": "password = 'secret'",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.HIGH.value,
        "severity": FindingSeverity.CRITICAL.value,
        "confidence": 0.9,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
]

STUB_FINDINGS__MIXED_HEADERS = [
    {
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 10,
        "header": "outstanding-todo",
        "snippet": "# TODO: refactor",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.MEDIUM.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.7,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
    {
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 20,
        "header": "security-vulnerability",
        "snippet": "eval(userInput)",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.HIGH.value,
        "severity": FindingSeverity.HIGH.value,
        "confidence": 0.85,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
]


def _make_engine(
    tmp: Path,
    live_actions: bool = False,
    guarded_live_review: bool = False,
    live_rollout_mode: str = LiveRolloutMode.LIMITED.value,
    limited_max_findings_count: int = 10,
    limited_require_pr_context: bool = True,
    limited_allowed_severities: list = None,
    limited_allowed_headers: list = None,
    github_pr_number: int = 42,
):
    """Create a configured engine for publish-filter testing."""
    repo_path = tmp / "repo"
    repo_path.mkdir()
    github = {
        "live_actions": live_actions,
        "auto_merge": False,
        "pr_number": github_pr_number,
        "repo": "test-repo",  # required for _post_summary_to_github to not crash
    }
    review_care = {
        "enabled": True,
        "mode": ReviewMode.AUTONOMOUS_REVIEW.value,
        "max_attempts": 3,
        "guarded_live_review": guarded_live_review,
        "live_rollout_mode": live_rollout_mode,
        "limited_max_findings_count": limited_max_findings_count,
        "limited_require_pr_context": limited_require_pr_context,
    }
    if limited_allowed_severities is not None:
        review_care["limited_allowed_severities"] = limited_allowed_severities
    if limited_allowed_headers is not None:
        review_care["limited_allowed_headers"] = limited_allowed_headers

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


def _stub_candidates(engine, findings_list):
    """Override _generate_local_candidates to return synthetic findings."""
    engine._generate_local_candidates = lambda: findings_list


# ---------------------------------------------------------------------------
# PublishFilterResult dataclass tests
# ---------------------------------------------------------------------------

class TestPublishFilterResultDataclass:
    def test_pass_result(self):
        r = PublishFilterResult(passed=True, decision="pass", failed_reason="")
        assert r.passed is True
        assert r.decision == "pass"
        assert r.failed_reason == ""

    def test_fail_result(self):
        r = PublishFilterResult(
            passed=False,
            decision="fail",
            failed_reason="findings_count=15 exceeds limit=10",
        )
        assert r.passed is False
        assert r.decision == "fail"
        assert r.failed_reason == "findings_count=15 exceeds limit=10"

    def test_bypassed_result(self):
        r = _build_pass_filter_result()
        assert r.passed is True
        assert r.decision == "bypassed"
        assert r.failed_reason == ""


# ---------------------------------------------------------------------------
# Filter method tests
# ---------------------------------------------------------------------------

class TestCheckLimitedPublishFilters:

    def test_passes_when_all_filters_pass(self):
        tmp = _isolated_tmp()
        engine, _, _ = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            limited_max_findings_count=10,
            limited_require_pr_context=True,
            limited_allowed_severities=["low", "medium"],
            limited_allowed_headers=["outstanding-todo", "excessively-long-line"],
            github_pr_number=42,
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        # Normalize the stub findings
        from qa_agent.review import normalize_candidate, assign_finding_identity
        normalized = [assign_finding_identity(normalize_candidate(f)) for f in STUB_FINDINGS__LOW_ONLY]

        pr_context = {"pr_number": 42, "pr_url": None, "resolution": "explicit"}
        result = engine._check_limited_publish_filters(
            findings_total=len(normalized),
            findings_list=normalized,
            pr_context=pr_context,
        )
        assert result.passed is True
        assert result.decision == "pass"
        assert result.failed_reason == ""

    def test_fails_max_findings_count(self):
        tmp = _isolated_tmp()
        engine, _, _ = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            limited_max_findings_count=2,
            github_pr_number=42,
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        from qa_agent.review import normalize_candidate, assign_finding_identity

        # STUB_FINDINGS__LOW_ONLY has 2 findings. Make 5 findings to exceed limit of 2.
        many_findings = []
        for i in range(5):
            f = dict(STUB_FINDINGS__LOW_ONLY[0])
            f["line"] = i
            f["header"] = f"finding-{i}"
            many_findings.append(f)
        normalized_many = [assign_finding_identity(normalize_candidate(f)) for f in many_findings]
        assert len(normalized_many) == 5

        pr_context = {"pr_number": 42, "pr_url": None, "resolution": "explicit"}
        result = engine._check_limited_publish_filters(
            findings_total=len(normalized_many),
            findings_list=normalized_many,
            pr_context=pr_context,
        )
        assert result.passed is False
        assert result.decision == "fail"
        assert "findings_count=5 exceeds" in result.failed_reason
        assert "limited_max_findings_count=2" in result.failed_reason

    def test_fails_require_pr_context_missing(self):
        tmp = _isolated_tmp()
        engine, _, _ = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            limited_require_pr_context=True,
            github_pr_number=None,  # no PR configured
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        from qa_agent.review import normalize_candidate, assign_finding_identity
        normalized = [assign_finding_identity(normalize_candidate(f)) for f in STUB_FINDINGS__LOW_ONLY]

        # pr_context is None
        result = engine._check_limited_publish_filters(
            findings_total=len(normalized),
            findings_list=normalized,
            pr_context=None,
        )
        assert result.passed is False
        assert result.decision == "fail"
        assert "pr_context required but not available" in result.failed_reason

    def test_fails_require_pr_context_null_number(self):
        tmp = _isolated_tmp()
        engine, _, _ = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            limited_require_pr_context=True,
            github_pr_number=None,
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        from qa_agent.review import normalize_candidate, assign_finding_identity
        normalized = [assign_finding_identity(normalize_candidate(f)) for f in STUB_FINDINGS__LOW_ONLY]

        # pr_context with pr_number=None
        pr_context = {"pr_number": None, "pr_url": None, "resolution": "some-reason"}
        result = engine._check_limited_publish_filters(
            findings_total=len(normalized),
            findings_list=normalized,
            pr_context=pr_context,
        )
        assert result.passed is False
        assert "pr_number is None" in result.failed_reason

    def test_fails_severity_subset(self):
        tmp = _isolated_tmp()
        engine, _, _ = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            limited_allowed_severities=["low"],  # critical not allowed
            github_pr_number=42,
        )
        _stub_candidates(engine, STUB_FINDINGS__MIXED_SEVERITY)

        from qa_agent.review import normalize_candidate, assign_finding_identity
        normalized = [assign_finding_identity(normalize_candidate(f)) for f in STUB_FINDINGS__MIXED_SEVERITY]

        pr_context = {"pr_number": 42, "pr_url": None, "resolution": "explicit"}
        result = engine._check_limited_publish_filters(
            findings_total=len(normalized),
            findings_list=normalized,
            pr_context=pr_context,
        )
        assert result.passed is False
        assert result.decision == "fail"
        assert "severity subset check failed" in result.failed_reason
        assert "critical" in result.failed_reason

    def test_fails_header_subset(self):
        tmp = _isolated_tmp()
        engine, _, _ = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            limited_allowed_headers=["outstanding-todo"],  # security-vulnerability not allowed
            github_pr_number=42,
        )
        _stub_candidates(engine, STUB_FINDINGS__MIXED_HEADERS)

        from qa_agent.review import normalize_candidate, assign_finding_identity
        normalized = [assign_finding_identity(normalize_candidate(f)) for f in STUB_FINDINGS__MIXED_HEADERS]

        pr_context = {"pr_number": 42, "pr_url": None, "resolution": "explicit"}
        result = engine._check_limited_publish_filters(
            findings_total=len(normalized),
            findings_list=normalized,
            pr_context=pr_context,
        )
        assert result.passed is False
        assert result.decision == "fail"
        assert "header subset check failed" in result.failed_reason
        assert "security-vulnerability" in result.failed_reason

    def test_passes_when_require_pr_context_is_disabled(self):
        tmp = _isolated_tmp()
        engine, _, _ = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            limited_require_pr_context=False,
            github_pr_number=None,
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        from qa_agent.review import normalize_candidate, assign_finding_identity
        normalized = [assign_finding_identity(normalize_candidate(f)) for f in STUB_FINDINGS__LOW_ONLY]

        result = engine._check_limited_publish_filters(
            findings_total=len(normalized),
            findings_list=normalized,
            pr_context=None,
        )
        assert result.passed is True
        assert result.decision == "pass"


# ---------------------------------------------------------------------------
# Integration: limited mode + passing filters → publishes
# ---------------------------------------------------------------------------

class TestLimitedModePublishFilterIntegration:

    def test_limited_pass_filter_rollout_eligible(self):
        """Passing filters in limited+guarded mode → rollout_eligible=True."""
        tmp = _isolated_tmp()
        engine, repo, state = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            live_rollout_mode=LiveRolloutMode.LIMITED.value,
            limited_max_findings_count=10,
            limited_require_pr_context=True,
            limited_allowed_severities=["low", "medium"],
            limited_allowed_headers=["outstanding-todo", "excessively-long-line"],
            github_pr_number=42,
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        # Mock _post_summary_to_github to avoid real GitHub API calls.
        # We need it to still perform the in-memory state mutations that the
        # real method does so the cycle can read them back.
        def mock_post(summary_text, run_id, prior_publish, target_pr_number=None):
            prior_publish.setdefault("runs", {})
            prior_publish["runs"][run_id] = {
                "status": "published",
                "comment_url": "https://github.com/test/repo/pull/42#issuecomment-123",
            }
            return "https://github.com/test/repo/pull/42#issuecomment-123"
        engine._post_summary_to_github = MagicMock(side_effect=mock_post)

        prior_publish = state.load_review_publish_state(repo.config.name)
        prior_publish.setdefault("runs", {})

        result = engine._run_autonomous_review_cycle(dry_run=False, allow_review_push=False)

        # _run_autonomous_review_cycle uses its own prior_publish via
        # state.load_review_publish_state() — reload from disk to see it.
        reloaded = state.load_review_publish_state(repo.config.name)
        run_ids = list(reloaded["runs"].keys())
        assert len(run_ids) == 1, f"expected 1 run, got {run_ids}"
        run_id = run_ids[0]
        run_entry = reloaded["runs"][run_id]
        assert run_entry["rollout_eligible"] is True
        assert run_entry["publish_filter_decision"] == "pass"
        assert run_entry["publish_filter_reason"] == ""
        assert run_entry["attention_recommended"] is False

    def test_limited_fail_filter_blocks_publish(self):
        """Failing filters in limited+guarded mode → filter-blocked lifecycle."""
        tmp = _isolated_tmp()
        # Use many findings (5 > limit=2) to trigger filter failure
        many_findings = []
        for i in range(5):
            f = dict(STUB_FINDINGS__LOW_ONLY[0])
            f["line"] = i
            f["header"] = f"finding-{i}"
            many_findings.append(f)

        engine, repo, state = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            live_rollout_mode=LiveRolloutMode.LIMITED.value,
            limited_max_findings_count=2,  # 5 findings will exceed this
            limited_require_pr_context=True,
            github_pr_number=42,
        )
        _stub_candidates(engine, many_findings)

        # Mock: mimics what _post_summary_to_github does when target_pr=None
        # (filter-blocked → refused, records FAILED status)
        def mock_post(summary_text, run_id, prior_publish, target_pr_number=None):
            prior_publish.setdefault("runs", {})
            prior_publish["runs"][run_id] = {
                "status": "failed",
                "error": "target-refused:filter-blocked",
                "targeted_pr_number": None,
            }
            return None
        engine._post_summary_to_github = MagicMock(side_effect=mock_post)

        prior_publish = state.load_review_publish_state(repo.config.name)
        prior_publish.setdefault("runs", {})

        result = engine._run_autonomous_review_cycle(dry_run=False, allow_review_push=False)

        # Cycle uses its own prior_publish via state.load_review_publish_state;
        # reload from disk to read it back.
        reloaded = state.load_review_publish_state(repo.config.name)
        run_ids = list(reloaded["runs"].keys())
        assert len(run_ids) == 1, f"expected 1 run, got {run_ids}"
        run_id = run_ids[0]
        run_entry = reloaded["runs"][run_id]

        # Filter should have failed and blocked live publication
        assert run_entry["publish_filter_decision"] == "fail"
        assert "findings_count=5 exceeds" in run_entry["publish_filter_reason"]
        assert run_entry["rollout_eligible"] is False
        assert run_entry["attention_recommended"] is True
        assert run_entry["lifecycle_phase"] == "filter-blocked"

    def test_limited_fail_pr_context_blocks_publish(self):
        """Missing PR context in limited+guarded mode → filter-blocked lifecycle."""
        tmp = _isolated_tmp()
        engine, repo, state = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            live_rollout_mode=LiveRolloutMode.LIMITED.value,
            limited_require_pr_context=True,
            github_pr_number=None,  # no PR context
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        def mock_post(summary_text, run_id, prior_publish, target_pr_number=None):
            prior_publish.setdefault("runs", {})
            prior_publish["runs"][run_id] = {
                "status": "failed",
                "error": "target-refused:pr-context-required",
                "targeted_pr_number": None,
            }
            return None
        engine._post_summary_to_github = MagicMock(side_effect=mock_post)

        result = engine._run_autonomous_review_cycle(dry_run=False, allow_review_push=False)

        reloaded = state.load_review_publish_state(repo.config.name)
        run_ids = list(reloaded["runs"].keys())
        assert len(run_ids) == 1
        run_id = run_ids[0]
        run_entry = reloaded["runs"][run_id]

        assert run_entry["publish_filter_decision"] == "fail"
        assert "pr_context required" in run_entry["publish_filter_reason"]
        assert run_entry["rollout_eligible"] is False
        assert run_entry["lifecycle_phase"] == "filter-blocked"

    def test_shadow_mode_runs_filter_check_but_not_live(self):
        """Shadow mode runs filter check for observability but never goes live."""
        tmp = _isolated_tmp()
        engine, repo, state = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            live_rollout_mode=LiveRolloutMode.SHADOW.value,
            limited_max_findings_count=10,
            limited_require_pr_context=True,
            github_pr_number=42,
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        # Shadow mode: _post_summary_to_github sets shadow=True and status=pending
        def mock_post(summary_text, run_id, prior_publish, target_pr_number=None):
            prior_publish.setdefault("runs", {})
            prior_publish["runs"][run_id] = {
                "status": "pending",
                "shadow": True,
                "shadow_summary_text": summary_text,
                "targeted_pr_number": target_pr_number,
                "rollout_mode": LiveRolloutMode.SHADOW.value,
            }
            return None
        engine._post_summary_to_github = MagicMock(side_effect=mock_post)

        result = engine._run_autonomous_review_cycle(dry_run=False, allow_review_push=False)

        reloaded = state.load_review_publish_state(repo.config.name)
        run_ids = list(reloaded["runs"].keys())
        assert len(run_ids) == 1
        run_id = run_ids[0]
        run_entry = reloaded["runs"][run_id]

        # Shadow mode: filter check ran (observability) but not eligible for live
        assert run_entry["publish_filter_decision"] == "pass"
        assert run_entry["rollout_eligible"] is False  # shadow never goes live
        assert run_entry["attention_recommended"] is False
        assert run_entry["lifecycle_phase"] == "shadow-published"
        assert run_entry.get("shadow") is True

    def test_local_only_mode_bypasses_filters(self):
        """local_only mode bypasses filter check (not applicable)."""
        tmp = _isolated_tmp()
        engine, repo, state = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=False,
            live_rollout_mode=LiveRolloutMode.LOCAL_ONLY.value,
            limited_max_findings_count=10,
            github_pr_number=42,
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        # local_only: guard disabled, _post_summary_to_github refuses but records entry.
        def mock_post(summary_text, run_id, prior_publish, target_pr_number=None):
            prior_publish.setdefault("runs", {})
            prior_publish["runs"][run_id] = {
                "status": "failed",
                "error": "target-refused:guard-disabled",
                "targeted_pr_number": None,
            }
            return None
        engine._post_summary_to_github = MagicMock(side_effect=mock_post)

        result = engine._run_autonomous_review_cycle(dry_run=False, allow_review_push=False)

        reloaded = state.load_review_publish_state(repo.config.name)
        run_ids = list(reloaded["runs"].keys())
        assert len(run_ids) == 1
        run_id = run_ids[0]
        run_entry = reloaded["runs"][run_id]

        # local_only: bypassed filters, rollout not eligible, guard disabled
        assert run_entry["publish_filter_decision"] == "bypassed"
        assert run_entry["rollout_eligible"] is False
        assert run_entry["lifecycle_phase"] == "guard-disabled"

    def test_review_run_data_contains_filter_signals(self):
        """review_run artifact captures all filter monitoring fields."""
        tmp = _isolated_tmp()
        engine, repo, state = _make_engine(
            tmp,
            live_actions=True,
            guarded_live_review=True,
            live_rollout_mode=LiveRolloutMode.LIMITED.value,
            limited_max_findings_count=10,
            limited_require_pr_context=True,
            limited_allowed_severities=["low", "medium"],
            limited_allowed_headers=["outstanding-todo", "excessively-long-line"],
            github_pr_number=42,
        )
        _stub_candidates(engine, STUB_FINDINGS__LOW_ONLY)

        def mock_post(summary_text, run_id, prior_publish, target_pr_number=None):
            prior_publish.setdefault("runs", {})
            prior_publish["runs"][run_id] = {
                "status": "published",
                "comment_url": "https://github.com/test/repo/pull/42#issuecomment-123",
            }
            return "https://github.com/test/repo/pull/42#issuecomment-123"
        engine._post_summary_to_github = MagicMock(side_effect=mock_post)

        result = engine._run_autonomous_review_cycle(dry_run=False, allow_review_push=False)

        reloaded = state.load_review_publish_state(repo.config.name)
        run_ids = list(reloaded["runs"].keys())
        assert len(run_ids) == 1
        run_id = run_ids[0]

        # The review_run artifact is persisted to disk by the cycle.
        run_file = state._get_state_dir(repo.config.name) / "review_runs" / f"{run_id}.json"
        assert run_file.exists(), f"review_run file not found: {run_file}"
        run_data = json.loads(run_file.read_text())

        assert "publish_filter_decision" in run_data
        assert "publish_filter_reason" in run_data
        assert "rollout_eligible" in run_data
        assert "attention_recommended" in run_data
        assert run_data["publish_filter_decision"] == "pass"
        assert run_data["rollout_eligible"] is True
        assert run_data["attention_recommended"] is False

    def test_observation_mode_unchanged(self):
        """observation mode still works without filter-related errors."""
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
            },
        )
        repo = Repo(config=config)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

        engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
        engine.repo = repo
        engine.state = state
        engine.provider = MagicMock(spec=GitHubReviewProvider)

        # Observation mode should not raise, should return empty result
        result = engine._run_autonomous_review_cycle(dry_run=False, allow_review_push=False)
        assert result is not None


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
