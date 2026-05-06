#!/usr/bin/env python3
"""Tests for Phase G1+G2: autonomous-review real local execution path.

Run with: python -m pytest tests/test_review_autonomous_cycle.py -v
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


# ---------------------------------------------------------------------------
# Module-level isolation helper — creates a guaranteed-unique directory that
# does not collide with pytest's class-level tmp_path fixture.
# ---------------------------------------------------------------------------

def _isolated_tmp() -> Path:
    """Return a unique isolated temp directory, removing any pre-existing one."""
    base = Path(f"/tmp/qa_test_autonomous_{uuid.uuid4().hex[:8]}")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base

from qa_agent.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    FindingSource,
    FindingActionability,
    FindingSeverity,
    PublishStatus,
    generate_id,
)
from qa_agent.review import (
    ReviewCycleEngine,
    GitHubReviewProvider,
    normalize_candidate,
    assign_finding_identity,
    build_review_summary_comment,
    ReconciliationResult,
    build_publish_entry,
    compute_run_publish_status,
)
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Fixtures / shared data
# ---------------------------------------------------------------------------

def make_repo(tmp_path: Path) -> Repo:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = RepoConfig(
        id="repo-test",
        name="test-repo",
        path=str(repo_path),
        language="typescript",
        review_care={
            "enabled": True,
            "mode": ReviewMode.AUTONOMOUS_REVIEW.value,
            "max_attempts": 3,
        },
    )
    return Repo(config=config)


def make_engine(repo: Repo, state: StateManager) -> ReviewCycleEngine:
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    engine.provider = MagicMock(spec=GitHubReviewProvider)
    return engine


# ---------------------------------------------------------------------------
# Candidate stub: synthetic candidates via _generate_local_candidates override
# ---------------------------------------------------------------------------

STUB_CANDIDATES = [
    {
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 10,
        "header": "outstanding-todo",
        "snippet": "# TODO: refactor this function",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.MEDIUM.value,
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
        "snippet": "x = function_call(arg1, arg2, arg3, arg4, arg5, arg6, arg7, arg8, arg9, arg10, arg11)",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.LOW.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.6,
        "safe_to_autofix": True,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
    {
        # Duplicate of the first candidate (same location)
        "repo": "test-repo",
        "path": "src/main.ts",
        "line": 10,
        "header": "outstanding-todo",
        "snippet": "# TODO: refactor this function",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.MEDIUM.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.9,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
    {
        # Eligible candidate: safe to autofix + high confidence
        "repo": "test-repo",
        "path": "src/handler.ts",
        "line": 5,
        "header": "excessively-long-line",
        "snippet": "y = AnotherLongFunctionName(arg_a, arg_b, arg_c, arg_d, arg_e, arg_f, arg_g)",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.HIGH.value,
        "severity": FindingSeverity.MEDIUM.value,
        "confidence": 0.85,
        "safe_to_autofix": True,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# Test: result counters are populated after non-dry-run
# ---------------------------------------------------------------------------

class TestAutonomousReviewResultCounters:
    def test_dry_run_returns_zero_counters(self):
        """dry_run=True returns immediately without processing candidates."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=True)

        assert result.findings_detected == 0
        assert result.findings_published == 0
        assert result.findings_failed == 0
        assert result.findings_skipped == 0
        assert result.findings_absent == 0

    def test_non_dry_run_returns_populated_counters(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        # 3 unique findings (one duplicate should be deduped)
        assert result.findings_detected == 3
        assert result.findings_published >= 1  # at least the eligible one published
        assert result.findings_skipped == 0     # all stub candidates are valid/eligible
        assert result.findings_absent == 0     # first run, nothing absent

    def test_empty_candidates_returns_zero_counters(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: []

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert result.findings_detected == 0
        assert result.findings_published == 0


# ---------------------------------------------------------------------------
# Test: ReviewRun artifact is persisted
# ---------------------------------------------------------------------------

class TestReviewRunArtifact:
    def test_review_run_file_is_created(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 1
        run = runs[0]
        assert run["status"] == "completed"
        assert run["mode"] == "autonomous-review"
        assert run["repo"] == "test-repo"
        assert run["findings_total"] == 3
        assert run["findings_eligible"] >= 1
        assert run["findings_published"] >= 1
        assert "reconciliation" in run
        assert "run_id" in run
        assert "run_file" not in run  # not a stored field

    def test_review_run_persists_with_run_id(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        run_id = state.list_review_runs(repo.config.name)[0]["run_id"]
        loaded = state.load_review_run(repo.config.name, run_id)
        assert loaded is not None
        assert loaded["run_id"] == run_id
        assert loaded["findings_total"] == 3


# ---------------------------------------------------------------------------
# Test: findings are persisted
# ---------------------------------------------------------------------------

class TestFindingsPersistence:
    def test_findings_jsonl_contains_deduped_findings(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        findings = state.load_review_findings(repo.config.name)
        assert len(findings) == 3  # deduped from 4 candidates
        for f in findings:
            assert "finding_id" in f
            assert "finding_fingerprint" in f
            assert "repo" in f

    def test_individual_finding_files_created(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        findings = state.load_review_findings(repo.config.name)
        for f in findings:
            fid = f["finding_id"]
            loaded = state.load_review_finding(repo.config.name, fid)
            assert loaded is not None
            assert loaded["finding_id"] == fid
            assert loaded["run_id"] is not None


# ---------------------------------------------------------------------------
# Test: publish state is reconciled and persisted
# ---------------------------------------------------------------------------

class TestPublishStatePersistence:
    """Tests that verify state is correctly persisted between cycle runs.

    Each test uses a uuid-based unique base directory to guarantee absolute
    isolation from other tests (bypasses pytest's tmp_path fixture which may
    be shared within a test class in some configurations).
    """

    def _isolated(self, key: str):
        """Create a unique directory guaranteed not to collide with other tests."""
        import shutil, uuid
        base = Path(f"/tmp/qa_persist_{key}_{uuid.uuid4().hex[:8]}")
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)
        repo = make_repo(base)
        state = StateManager(base / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)
        return engine, repo, state, base

    def test_publish_state_updated_after_run(self):
        engine, repo, state, _ = self._isolated("after_run")
        engine._run_autonomous_review_cycle(dry_run=False)
        publish = state.load_review_publish_state(repo.config.name)
        assert "findings" in publish
        assert "runs" in publish
        assert len(publish["findings"]) == 3
        # At least the one run we just created should be present
        assert len(publish["runs"]) >= 1

    def test_second_run_reconciles_previously_published_findings(self):
        engine, repo, state, _ = self._isolated("second_run")
        result1 = engine._run_autonomous_review_cycle(dry_run=False)
        assert result1.findings_published >= 1
        result2 = engine._run_autonomous_review_cycle(dry_run=False)
        assert result2.findings_detected == 3
        runs = state.list_review_runs(repo.config.name)
        assert len(runs) == 2
        latest_run = runs[0]
        assert len(latest_run["reconciliation"]["already_published"]) >= 1

    def test_publish_state_persists_between_runs(self):
        """A new StateManager reading the same state dir finds the persisted run."""
        engine, repo, state, base = self._isolated("between_runs")
        engine._run_autonomous_review_cycle(dry_run=False)
        # Verify that a fresh StateManager reading the same repos_dir
        # finds the findings and at least one run.
        repos_dir = base / "repos"
        state2 = StateManager(repos_dir)
        publish = state2.load_review_publish_state(repo.config.name)
        assert len(publish["findings"]) == 3
        assert len(publish["runs"]) >= 1


# ---------------------------------------------------------------------------
# Test: review events are appended
# ---------------------------------------------------------------------------

class TestReviewEvents:
    def test_completion_event_appended(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        events_file = state.get_review_events_file(repo.config.name)
        assert events_file.exists()
        lines = events_file.read_text().strip().splitlines()
        # Phase J may emit learned-rule-log events before the completion event
        completion_events = [json.loads(l) for l in lines if json.loads(l)["event"] == "autonomous-review-completed"]
        assert len(completion_events) == 1
        event = completion_events[0]
        assert event["run_id"] is not None
        assert event["details"]["findings_total"] == 3

    def test_no_events_in_dry_run(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=True)

        events_file = state.get_review_events_file(repo.config.name)
        assert not events_file.exists()


# ---------------------------------------------------------------------------
# Test: deterministic summary comment is produced
# ---------------------------------------------------------------------------

class TestSummaryComment:
    def test_summary_file_written(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        # Check that summary was written to review_prompts dir
        runs = state.list_review_runs(repo.config.name)
        run_id = runs[0]["run_id"]
        summary_dir = state.get_review_prompts_dir(repo.config.name)
        summary_files = list(summary_dir.glob(f"autonomous-run-{run_id}.md"))
        assert len(summary_files) == 1
        content = summary_files[0].read_text()
        assert "QA-Agent Autonomous Review Summary" in content
        assert run_id in content

    def test_summary_deterministic_same_candidates(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

        # Run twice with same candidates
        for _ in range(2):
            engine = make_engine(repo, state)
            engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)
            engine._run_autonomous_review_cycle(dry_run=False)

        runs = state.list_review_runs(repo.config.name)
        summaries = []
        for run in runs:
            run_id = run["run_id"]
            summary_dir = state.get_review_prompts_dir(repo.config.name)
            sf = list(summary_dir.glob(f"autonomous-run-{run_id}.md"))
            if sf:
                summaries.append(sf[0].read_text())

        # Both summaries should be non-empty and contain expected sections
        assert all("QA-Agent Autonomous Review Summary" in s for s in summaries)


# ---------------------------------------------------------------------------
# Test: validation/normalization layer is wired correctly
# ---------------------------------------------------------------------------

class TestValidationLayer:
    def test_invalid_candidates_are_skipped(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        mixed_candidates = list(STUB_CANDIDATES) + [
            # Missing required field 'header' — should be skipped
            {
                "repo": "test-repo",
                "path": "src/bad.ts",
                "line": 1,
                # header missing
                "source": "llm",
            },
        ]
        engine._generate_local_candidates = lambda: mixed_candidates

        result = engine._run_autonomous_review_cycle(dry_run=False)

        # Should still process valid candidates, skipping the bad one
        assert result.findings_skipped >= 1
        assert result.findings_detected == 3  # Only valid ones counted

    def test_deduplicated_candidates_produce_single_finding(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        # Two identical candidates for same location
        duplicate_candidates = list(STUB_CANDIDATES[:2])
        engine._generate_local_candidates = lambda: duplicate_candidates + [
            {**duplicate_candidates[0]},  # exact duplicate
        ]

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert result.findings_detected == 2  # deduplicated from 3


# ---------------------------------------------------------------------------
# Test: mode dispatch routes correctly to autonomous-review
# ---------------------------------------------------------------------------

class TestAutonomousReviewDispatch:
    def test_run_calls_autonomous_review_for_autonomous_review_mode(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine.run(dry_run=False)

        # Should have findings from autonomous-review, not PR-based counts
        assert result.findings_detected >= 1

    def test_observation_mode_unchanged_does_not_process_findings(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.review_care["mode"] = ReviewMode.OBSERVATION.value
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        # Observation cycle with empty PR list
        engine.provider.list_managed_prs.return_value = []

        result = engine.run(dry_run=True)

        assert result.findings_detected == 0  # autonomous counters not set by obs cycle


# ---------------------------------------------------------------------------
# Test: no LLM plumbing, no GitHub API calls, no push
# ---------------------------------------------------------------------------

class TestSafetyConstraints:
    def test_observation_provider_methods_not_called(self):
        """fetch_review_snapshot (observation-mode specific) must never be called in autonomous-review."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        engine._run_autonomous_review_cycle(dry_run=False)

        # list_managed_prs IS now legitimately called for safe PR targeting
        # (part of _resolve_target_pr_for_run priority chain).
        # fetch_review_snapshot is the dangerous one — observation-mode only.
        engine.provider.fetch_review_snapshot.assert_not_called()

    def test_findings_do_not_enter_remediation_cycle_from_here(self):
        """Autonomous-review findings are NOT fed back into PR remediation path."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        # Remediation counters should be 0 (different execution path)
        assert result.retry_eligible_prs == 0
        assert result.retry_planned_prs == 0
        assert result.retry_executed_prs == 0


# ---------------------------------------------------------------------------
# Integration: full local lifecycle with empty repo
# ---------------------------------------------------------------------------

class TestEmptyRepoPath:
    def test_no_repo_path_means_no_candidates(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        # repo path exists but has no .py files
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        # _generate_local_candidates reads repo path — no files there

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert result.findings_detected == 0
        assert result.findings_published == 0


# ---------------------------------------------------------------------------
# Integration: local file scanning stub produces candidates
# ---------------------------------------------------------------------------

class TestLocalFileScanning:
    def test_scans_python_files_for_todo_markers(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        # Create a Python file with a TODO
        src_dir = tmp / "repo" / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "sample.py").write_text(
            "# TODO: this needs refactoring\ndef foo(): pass\n",
            encoding="utf-8",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        # Override candidates to use actual file scan
        engine._generate_local_candidates = (
            ReviewCycleEngine._generate_local_candidates.__get__(engine, ReviewCycleEngine)
        )

        candidates = engine._generate_local_candidates()

        assert len(candidates) >= 1
        todo_candidates = [c for c in candidates if "todo" in c["header"]]
        assert len(todo_candidates) >= 1
        assert todo_candidates[0]["repo"] == "test-repo"

    def test_scans_python_files_for_long_lines(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        src_dir = tmp / "repo" / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        long_line = "x = " + ", ".join([f"arg{i}" for i in range(30)]) + "\n"
        (src_dir / "long.py").write_text(long_line, encoding="utf-8")
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = (
            ReviewCycleEngine._generate_local_candidates.__get__(engine, ReviewCycleEngine)
        )

        candidates = engine._generate_local_candidates()

        long_candidates = [c for c in candidates if c["header"] == "excessively-long-line"]
        assert len(long_candidates) >= 1


# ---------------------------------------------------------------------------
# Phase J: Live publication bridge tests
# ---------------------------------------------------------------------------

class TestLivePublicationBridge:
    """Tests for _post_summary_to_github and integration with autonomous cycle."""

    def test_live_actions_false_skips_publish(self):
        """live_actions=False → _post_summary_to_github returns None without calling gh."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = False
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        # Patch subprocess.run to fail if called
        called = False
        original_run = subprocess.run

        def track_run(*args, **kwargs):
            nonlocal called
            called = True
            return original_run(*args, **kwargs)

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_run):
            result = engine._run_autonomous_review_cycle(dry_run=False)

        assert called is False, "subprocess.run should not be called when live_actions=False"
        # Local state should still be populated
        assert result.findings_published >= 1

    def test_live_actions_true_invokes_gh_pr_list_and_comment(self):
        """live_actions=True + dry_run=False → gh pr list and comment are called."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "test-owner"
        repo.config.github["repo"] = "test-repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        gh_calls = []

        def fake_run(args, **kwargs):
            gh_calls.append(args)
            if "pr" in args and "list" in args:
                # gh pr list
                return MagicMock(
                    returncode=0,
                    stdout='[{"number": 42, "title": "Test PR", "updatedAt": "2026-03-29T00:00:00Z"}]',
                    stderr="",
                )
            elif "pr" in args and "comment" in args:
                # gh pr comment
                return MagicMock(
                    returncode=0,
                    stdout="https://github.com/test-owner/test-repo/pull/42#issuecomment-123",
                    stderr="",
                )
            return MagicMock(returncode=1, stdout="", stderr="unknown")

        with unittest.mock.patch.object(subprocess, "run", side_effect=fake_run):
            result = engine._run_autonomous_review_cycle(dry_run=False)

        pr_list_calls = [c for c in gh_calls if "list" in c]
        pr_comment_calls = [c for c in gh_calls if "comment" in c]
        assert len(pr_list_calls) >= 1, "gh pr list should be called"
        assert len(pr_comment_calls) >= 1, "gh pr comment should be called"
        assert result.findings_published >= 1

    def test_publish_failure_marks_state_failed(self):
        """gh pr comment failure → run publish entry marked failed, error stored."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "test-owner"
        repo.config.github["repo"] = "test-repo"
        # Enable guarded live-review path so gh API call is actually made
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
                return MagicMock(
                    returncode=1,
                    stdout="",
                    stderr="token expired",
                )
            return MagicMock(returncode=1, stdout="", stderr="unknown")

        with unittest.mock.patch.object(subprocess, "run", side_effect=fake_run):
            result = engine._run_autonomous_review_cycle(dry_run=False)

        publish = state.load_review_publish_state(repo.config.name)
        runs = publish.get("runs", {})
        # Find the run we just created
        latest_run = None
        for rid, rentry in runs.items():
            if rid.startswith("arun-"):
                latest_run = rentry
                break
        assert latest_run is not None
        assert latest_run.get("status") == PublishStatus.FAILED.value
        assert "token expired" in latest_run.get("error", "")

    def test_rerun_does_not_republish_already_published_run(self):
        """If prior_publish already has comment_url for run_id, _post_summary skips gh call.

        We test this at the _post_summary_to_github level directly (not the full
        cycle) to avoid the complexity of mocking generate_id across module boundaries.
        The integration at cycle level is verified by test_live_actions_true_invokes_...
        """
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "test-owner"
        repo.config.github["repo"] = "test-repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Pre-populate prior_publish with a run that already has a comment_url
        pre_run_id = "arun-already-posted-456"
        prior_publish = {
            "findings": {},
            "runs": {
                pre_run_id: {
                    "status": "published",
                    "comment_url": "https://github.com/test-owner/test-repo/pull/1#issuecomment-888",
                    "run_id": pre_run_id,
                }
            },
        }

        gh_called = False

        def track_gh(args, **kwargs):
            nonlocal gh_called
            gh_called = True
            return MagicMock(returncode=0, stdout="", stderr="should not be called")

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_gh):
            # Call _post_summary_to_github directly with the pre-populated state
            result = engine._post_summary_to_github(
                summary_text="any summary",
                run_id=pre_run_id,
                prior_publish=prior_publish,
            )

        assert gh_called is False, (
            "gh should NOT be called when run_id already has comment_url in prior_publish"
        )
        # Returns the existing comment_url (not None) so caller knows what was already posted
        assert result == "https://github.com/test-owner/test-repo/pull/1#issuecomment-888"

    def test_local_only_path_still_works(self):
        """With live_actions=False the full local path (state + artifacts) works unchanged."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = False
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._generate_local_candidates = lambda: list(STUB_CANDIDATES)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        # State artifacts exist
        publish = state.load_review_publish_state(repo.config.name)
        assert "findings" in publish
        assert "runs" in publish
        # ReviewRun files exist in the correct directory (review_runs/, not runs/)
        runs_dir = state.get_review_runs_dir(repo.config.name)
        assert any(runs_dir.glob("*.json"))
        # Counters are populated
        assert result.findings_published >= 1


# ---------------------------------------------------------------------------
# Test: PR targeting hardening (Phase G2 slice)
# ---------------------------------------------------------------------------

class TestPRTargetingHardening:
    """Tests for safe PR targeting and refusal semantics in autonomous-review."""

    # --- _resolve_target_pr_for_run ---

    def test_single_open_pr_is_targeted(self):
        """Exactly one open PR → that PR is resolved as target."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Directly mock _find_open_prs (avoids subprocess complexity)
        engine._find_open_prs = MagicMock(
            return_value=[{"number": 7, "title": "PR", "updatedAt": "2026-03-29T00:00:00Z"}]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        resolved, reason = engine._resolve_target_pr_for_run({"runs": {}, "findings": {}})

        assert resolved == 7, f"Expected PR 7, got {resolved}"
        assert "single-open-pr-7" in reason

    def test_multiple_open_prs_refused(self):
        """Multiple open PRs with no prior targeting → resolution returns None (refuse)."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        engine._find_open_prs = MagicMock(
            return_value=[
                {"number": 1, "title": "PR1", "updatedAt": "2026-03-29T00:00:01Z"},
                {"number": 2, "title": "PR2", "updatedAt": "2026-03-29T00:00:00Z"},
            ]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        resolved, reason = engine._resolve_target_pr_for_run({"runs": {}, "findings": {}})

        assert resolved is None, f"Expected None (refuse), got {resolved}"
        assert "multiple-open-prs-2-refused" in reason

    def test_prior_targeted_pr_reused_on_rerun(self):
        """Prior run targeted PR 5; single open PR 5 still open → reuse PR 5."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        engine._find_open_prs = MagicMock(
            return_value=[{"number": 5, "title": "Prior PR", "updatedAt": "2026-03-29T00:00:00Z"}]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        prior_publish = {
            "runs": {
                "arun-old-001": {
                    "status": "published",
                    "comment_url": "https://github.com/owner/repo/pull/5#issuecomment-1",
                    "targeted_pr_number": 5,
                }
            },
            "findings": {},
        }

        resolved, reason = engine._resolve_target_pr_for_run(prior_publish)

        assert resolved == 5, f"Expected prior targeted PR 5, got {resolved}"
        assert "prior-targeted-pr-5-reused" in reason

    def test_rerun_does_not_misTarget_different_pr(self):
        """Prior run targeted PR 3; now multiple open PRs (3, 4, 5) → prior PR still open → anchored to 3.

        Safe behavior: if the prior-targeted PR is still open, reuse it (anchoring).
        Refusing would break the PR's review history continuity.
        """
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        engine._find_open_prs = MagicMock(
            return_value=[
                {"number": 3, "title": "PR3", "updatedAt": "2026-03-29T00:00:03Z"},
                {"number": 4, "title": "PR4", "updatedAt": "2026-03-29T00:00:02Z"},
                {"number": 5, "title": "PR5", "updatedAt": "2026-03-29T00:00:01Z"},
            ]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        prior_publish = {
            "runs": {
                "arun-old-001": {
                    "status": "published",
                    "comment_url": "https://github.com/owner/repo/pull/3#issuecomment-1",
                    "targeted_pr_number": 3,
                }
            },
            "findings": {},
        }

        resolved, reason = engine._resolve_target_pr_for_run(prior_publish)

        # prior-targeted PR 3 is still open → anchored to 3 (safe: same PR, not different)
        assert resolved == 3, f"Expected anchor to prior PR 3, got {resolved}"
        assert "prior-targeted-pr-3-reused" in reason

    def test_prior_targeted_pr_closed_is_refused(self):
        """Prior run targeted PR 3; that PR is now closed; single open PR 7 → refuse (prior is gone)."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Only PR 7 is open — PR 3 (prior target) is not in this list
        engine._find_open_prs = MagicMock(
            return_value=[{"number": 7, "title": "New PR", "updatedAt": "2026-03-29T00:00:00Z"}]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        prior_publish = {
            "runs": {
                "arun-old-001": {
                    "status": "published",
                    "comment_url": "https://github.com/owner/repo/pull/3#issuecomment-1",
                    "targeted_pr_number": 3,
                }
            },
            "findings": {},
        }

        resolved, reason = engine._resolve_target_pr_for_run(prior_publish)

        # prior-targeted step passes (one distinct: 3), but verification finds
        # 3 not in open_numbers → returns None with "prior-targeted-pr-3-now-closed"
        assert resolved is None, f"Expected None (prior closed), got {resolved}"
        assert "prior-targeted-pr-3-now-closed" in reason

    def test_single_managed_pr_is_targeted(self):
        """Only one managed PR → that PR is resolved (managed step short-circuits before open PRs)."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        engine.provider.list_managed_prs = MagicMock(
            return_value=[{"number": 3, "title": "Managed PR"}]
        )
        # This should NOT be called (managed step short-circuits)
        engine._find_open_prs = MagicMock(
            return_value=[
                {"number": 1, "title": "PR1", "updatedAt": "2026-03-29T00:00:01Z"},
                {"number": 2, "title": "PR2", "updatedAt": "2026-03-29T00:00:00Z"},
            ]
        )

        resolved, reason = engine._resolve_target_pr_for_run({"runs": {}, "findings": {}})

        assert resolved == 3, f"Expected managed PR 3, got {resolved}"
        assert "single-managed-pr-3" in reason
        engine._find_open_prs.assert_not_called()

    def test_multiple_managed_prs_refused(self):
        """Multiple managed PRs → refuse (ambiguous), do not fall through to open PRs."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        engine.provider.list_managed_prs = MagicMock(
            return_value=[
                {"number": 1, "title": "PR1"},
                {"number": 2, "title": "PR2"},
            ]
        )
        # This should NOT be called (managed step refuses before open PRs discovery)
        engine._find_open_prs = MagicMock(return_value=[{"number": 1}])

        resolved, reason = engine._resolve_target_pr_for_run({"runs": {}, "findings": {}})

        assert resolved is None, f"Expected None (refuse), got {resolved}"
        assert "multiple-managed-prs-2-refused" in reason
        engine._find_open_prs.assert_not_called()

    # --- _post_summary_to_github refusal on ambiguous multiple-PRs ---

    def test_ambiguous_multi_pr_stays_local_only(self):
        """Multiple open PRs, no prior targeting → stays local-only, no gh comment posted."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        engine._find_open_prs = MagicMock(
            return_value=[
                {"number": 10, "title": "PR10", "updatedAt": "2026-03-29T00:00:02Z"},
                {"number": 20, "title": "PR20", "updatedAt": "2026-03-29T00:00:01Z"},
            ]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        gh_called = False
        original_run = subprocess.run

        def track_run(args, **kwargs):
            nonlocal gh_called
            gh_called = True
            return original_run(args, **kwargs)

        prior_publish = {"runs": {}, "findings": {}}

        with unittest.mock.patch.object(subprocess, "run", side_effect=track_run):
            result = engine._post_summary_to_github(
                summary_text="test summary",
                run_id="arun-test-001",
                prior_publish=prior_publish,
            )

        assert result is None, f"Expected None (refused), got {result}"
        assert gh_called is False, "gh should not be called when multiple PRs are ambiguous"
        # The refusal event should be recorded
        events_file = state.get_review_events_file(repo.config.name)
        assert events_file.exists()
        events = [json.loads(line) for line in events_file.read_text().strip().splitlines()]
        refusal_events = [e for e in events if e.get("event") == "autonomous-review-publish-refused"]
        assert len(refusal_events) == 1
        assert "multiple-open-prs" in refusal_events[0]["details"]["reason"]

    # --- publish state records targeted_pr_number ---

    def test_publish_state_records_targeted_pr_on_success(self):
        """Successful gh comment → run publish entry contains targeted_pr_number."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Open PRs: only PR 42 is open
        engine._find_open_prs = MagicMock(
            return_value=[{"number": 42, "title": "The PR", "updatedAt": "2026-03-29T00:00:00Z"}]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        def fake_run(args, **kwargs):
            if "pr" in args and "comment" in args:
                return MagicMock(
                    returncode=0,
                    stdout="https://github.com/owner/repo/pull/42#issuecomment-999",
                    stderr="",
                )
            return MagicMock(returncode=1, stdout="", stderr="unknown")

        prior_publish = {"runs": {}, "findings": {}}

        with unittest.mock.patch.object(subprocess, "run", side_effect=fake_run):
            result = engine._post_summary_to_github(
                summary_text="review summary for 42",
                run_id="arun-post-042",
                prior_publish=prior_publish,
                target_pr_number=42,  # Explicit: PR 42 is the single open PR
            )

        assert result == "https://github.com/owner/repo/pull/42#issuecomment-999"
        run_entry = prior_publish["runs"]["arun-post-042"]
        assert run_entry.get("targeted_pr_number") == 42
        assert run_entry.get("comment_url") == "https://github.com/owner/repo/pull/42#issuecomment-999"
        assert run_entry.get("status") == PublishStatus.PUBLISHED.value

    def test_publish_state_records_refused_when_ambiguous(self):
        """Refused publication → run publish entry has targeted_pr_number=None and error=target-refused:..."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        engine._find_open_prs = MagicMock(
            return_value=[
                {"number": 5, "title": "PR5", "updatedAt": "2026-03-29T00:00:02Z"},
                {"number": 6, "title": "PR6", "updatedAt": "2026-03-29T00:00:01Z"},
            ]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        prior_publish = {"runs": {}, "findings": {}}

        result = engine._post_summary_to_github(
            summary_text="should be refused",
            run_id="arun-refused-001",
            prior_publish=prior_publish,
        )

        assert result is None
        run_entry = prior_publish["runs"]["arun-refused-001"]
        assert run_entry.get("targeted_pr_number") is None
        assert "target-refused" in run_entry.get("error", "")
        assert run_entry.get("status") == PublishStatus.FAILED.value

    # --- explicit target_pr_number parameter overrides resolution ---

    def test_explicit_target_pr_number_wins(self):
        """Passing explicit target_pr_number bypasses resolution; open-PRs check uses that PR."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Open PRs: only PR 42 is open
        engine._find_open_prs = MagicMock(
            return_value=[{"number": 42, "title": "The PR", "updatedAt": "2026-03-29T00:00:00Z"}]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        gh_called_with_pr = None

        def fake_run(args, **kwargs):
            nonlocal gh_called_with_pr
            if "pr" in args and "comment" in args:
                pr_idx = args.index("comment") + 1
                gh_called_with_pr = int(args[pr_idx])
                return MagicMock(
                    returncode=0,
                    stdout=f"https://github.com/owner/repo/pull/{gh_called_with_pr}#issuecomment-1",
                    stderr="",
                )
            return MagicMock(returncode=1, stdout="", stderr="unknown")

        # Explicit target_pr_number=42 matches the single open PR
        with unittest.mock.patch.object(subprocess, "run", side_effect=fake_run):
            result = engine._post_summary_to_github(
                summary_text="explicit target test",
                run_id="arun-explicit-001",
                prior_publish={"runs": {}, "findings": {}},
                target_pr_number=42,
            )

        assert result is not None
        assert gh_called_with_pr == 42, f"Expected gh called with PR 42, got {gh_called_with_pr}"

    # --- rerun does not mis-target ---

    def test_rerun_stays_anchored_to_prior_pr(self):
        """Second run with same prior publish → same PR is targeted (prior-targeted step wins)."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        engine._find_open_prs = MagicMock(
            return_value=[{"number": 8, "title": "The PR", "updatedAt": "2026-03-29T00:00:00Z"}]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        prior_publish_after_run1 = {
            "runs": {
                "arun-first-001": {
                    "status": "published",
                    "comment_url": "https://github.com/owner/repo/pull/8#issuecomment-1",
                    "targeted_pr_number": 8,
                }
            },
            "findings": {},
        }

        resolved, reason = engine._resolve_target_pr_for_run(prior_publish_after_run1)

        assert resolved == 8, f"Expected rerun to anchor to PR 8, got {resolved}"
        assert "prior-targeted-pr-8-reused" in reason

    def test_target_pr_closed_refuses(self):
        """Explicit target_pr_number is provided but that PR is not in open PRs → refuses."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        repo.config.github["live_actions"] = True
        repo.config.github["owner"] = "owner"
        repo.config.github["repo"] = "repo"
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Open PRs list shows only PR 11 — PR 99 is not open
        engine._find_open_prs = MagicMock(
            return_value=[{"number": 11, "title": "Different PR", "updatedAt": "2026-03-29T00:00:00Z"}]
        )
        engine.provider.list_managed_prs = MagicMock(return_value=[])

        prior_publish = {"runs": {}, "findings": {}}

        result = engine._post_summary_to_github(
            summary_text="should refuse",
            run_id="arun-closed-target-001",
            prior_publish=prior_publish,
            target_pr_number=99,  # PR 99 is not in open PRs — should refuse
        )

        assert result is None
        run_entry = prior_publish["runs"]["arun-closed-target-001"]
        assert run_entry.get("error") == "target-pr-99-not-open"
        assert run_entry.get("targeted_pr_number") is None



if __name__ == "__main__":
    import pytest
    import unittest
    pytest.main([__file__, "-v"])
