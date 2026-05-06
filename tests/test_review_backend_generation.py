#!/usr/bin/env python3
"""Tests for Phase G1-G2: autonomous-review backend generation bridge.

Run with: python -m pytest tests/test_review_backend_generation.py -v
"""

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Module-level isolation helper
# ---------------------------------------------------------------------------

def _isolated_tmp() -> Path:
    """Return a unique isolated temp directory, removing any pre-existing one."""
    base = Path(f"/tmp/qa_test_backend_gen_{uuid.uuid4().hex[:8]}")
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
)
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Fixtures / shared data
# ---------------------------------------------------------------------------

def make_repo(tmp_path: Path, review_care_mode=None, **overrides) -> Repo:
    """Create a Repo with the given overrides. Use review_care_mode to set
    the review mode without conflicting with **overrides."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    if review_care_mode is None:
        review_care_mode = ReviewMode.AUTONOMOUS_REVIEW.value
    config = RepoConfig(
        id="repo-test",
        name="test-repo",
        path=str(repo_path),
        language="typescript",
        review_care={
            "enabled": True,
            "mode": review_care_mode,
            "max_attempts": 3,
        },
        **overrides,
    )
    return Repo(config=config)


def make_engine(repo: Repo, state: StateManager) -> ReviewCycleEngine:
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    engine.provider = MagicMock(spec=GitHubReviewProvider)
    engine.provider.repo_path = Path(repo.config.path)
    # Provide required methods that observation cycle needs
    engine.provider.list_managed_prs.return_value = []
    engine._update_status_artifact = MagicMock()
    return engine


def load_review_events(state: StateManager, repo_name: str):
    """Load review events from the JSONL file."""
    events_file = state.get_review_events_file(repo_name)
    if not events_file.exists():
        return []
    events = []
    with open(events_file) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# Synthetic backend candidates for mocking
# ---------------------------------------------------------------------------

BACKEND_CANDIDATES_VALID = [
    {
        "repo": "test-repo",
        "path": "src/backend.ts",
        "line": 5,
        "header": "outstanding-todo",
        "snippet": "# TODO: implement authentication",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.MEDIUM.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.8,
        "safe_to_autofix": False,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
    {
        "repo": "test-repo",
        "path": "src/db.ts",
        "line": 20,
        "header": "excessively-long-line",
        "snippet": "x = function_call(arg1, arg2, arg3, arg4, arg5, arg6, arg7, arg8)",
        "source": FindingSource.LINTER.value,
        "actionability": FindingActionability.LOW.value,
        "severity": FindingSeverity.LOW.value,
        "confidence": 0.6,
        "safe_to_autofix": True,
        "discovered_at": "2026-03-29T00:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# Test: _generate_from_backend with no backend configured
# ---------------------------------------------------------------------------

class TestBackendGenerationNoConfig:
    """When no review backend template is configured, returns empty and
    records a 'skipped' event so caller falls back to local stub."""

    def test_no_template_returns_empty_and_logs_event(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)  # no review_claude_template / review_opencode_template
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")
        candidates = engine._generate_from_backend(run_id)

        assert candidates == []
        events = load_review_events(state, repo.config.name)
        skip_events = [e for e in events if e.get("event") == "backend-generation skipped"]
        assert len(skip_events) == 1
        assert skip_events[0].get("reason") == "no_review_backend_configured"

    def test_cycle_with_no_backend_uses_local_stub(self):
        """Full cycle: no backend configured → local stub candidates are used."""
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Override local stub to return known candidates
        stub_candidates = [
            {
                "repo": "test-repo",
                "path": "src/stub.ts",
                "line": 1,
                "header": "outstanding-todo",
                "snippet": "# TODO: stub",
                "source": FindingSource.LINTER.value,
                "actionability": FindingActionability.LOW.value,
                "severity": FindingSeverity.LOW.value,
                "confidence": 0.5,
                "safe_to_autofix": False,
                "discovered_at": "2026-03-29T00:00:00Z",
            }
        ]
        engine._generate_local_candidates = lambda: list(stub_candidates)

        result = engine._run_autonomous_review_cycle(dry_run=False)

        assert result.findings_detected == 1
        events = load_review_events(state, repo.config.name)
        skip_events = [e for e in events if e.get("event") == "backend-generation skipped"]
        assert len(skip_events) == 1


# ---------------------------------------------------------------------------
# Test: _generate_from_backend with backend configured, valid output
# ---------------------------------------------------------------------------

class TestBackendGenerationValidOutput:
    """When backend is configured and returns valid JSON, candidates are
    returned and a 'succeeded' event is recorded."""

    def test_valid_json_array_returned(self):
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="claude",
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")

        # Mock shutil.which and subprocess.run in the review module
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("qa_agent.review.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps(BACKEND_CANDIDATES_VALID),
                    stderr="",
                )
                candidates = engine._generate_from_backend(run_id)

        assert len(candidates) == 2
        # Candidates should be normalized
        for c in candidates:
            assert "header" in c
            assert "path" in c

        events = load_review_events(state, repo.config.name)
        success_events = [e for e in events if e.get("event") == "backend-generation succeeded"]
        assert len(success_events) == 1
        assert success_events[0].get("candidate_count") == 2


class TestCandidatePromptArtifactMnemo:
    def test_candidate_prompt_artifact_includes_mnemo_context_when_available(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._build_mnemo_review_context = lambda **kwargs: (
            "## Mnemo context\n\n### Mnemo query: `src/backend.ts`\nrelated code paths\n"
        )

        artifact = engine._build_candidate_prompt_artifact(
            pr_context={
                "pr_number": 33,
                "branch": "qa/fix-33",
                "changed_files": ["src/backend.ts"],
            }
        )

        assert "## PR Context" in artifact
        assert "## Mnemo context" in artifact
        assert "related code paths" in artifact


class TestMnemoCandidateSignals:
    def test_apply_mnemo_candidate_signals_boosts_supported_findings(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._mnemo_available = lambda: True

        def fake_query(query, limit=3):
            if "src/backend.ts" in query or "outstanding-todo" in query:
                return "support"
            return ""

        engine._mnemo_query = fake_query
        findings = [
            {
                "repo": "test-repo",
                "path": "src/backend.ts",
                "line": 5,
                "header": "outstanding-todo",
                "snippet": "# TODO: implement authentication",
                "source": FindingSource.LINTER,
                "actionability": FindingActionability.MEDIUM,
                "severity": FindingSeverity.LOW,
                "confidence": 0.5,
                "safe_to_autofix": False,
                "discovered_at": "2026-03-29T00:00:00Z",
                "finding_id": "rf-a-000",
                "finding_fingerprint": "a" * 64,
            },
            {
                "repo": "test-repo",
                "path": "src/other.ts",
                "line": 8,
                "header": "weak-signal",
                "snippet": "misc",
                "source": FindingSource.LINTER,
                "actionability": FindingActionability.MEDIUM,
                "severity": FindingSeverity.LOW,
                "confidence": 0.55,
                "safe_to_autofix": False,
                "discovered_at": "2026-03-29T00:00:00Z",
                "finding_id": "rf-b-000",
                "finding_fingerprint": "b" * 64,
            },
        ]

        boosted = engine._apply_mnemo_candidate_signals(findings)
        assert boosted[0]["path"] == "src/backend.ts"
        assert boosted[0]["mnemo_support_hits"] >= 1
        assert boosted[0]["confidence"] > 0.5

    def test_apply_mnemo_candidate_signals_noop_when_unavailable(self):
        tmp = _isolated_tmp()
        repo = make_repo(tmp)
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)
        engine._mnemo_available = lambda: False

        findings = [{"finding_id": "rf-a-000", "confidence": 0.5, "path": "a", "header": "h", "snippet": ""}]
        result = engine._apply_mnemo_candidate_signals(findings)
        assert result == findings

    def test_valid_json_dict_with_findings_key(self):
        """Backend returns {"findings": [...]} wrapper — should unwrap correctly."""
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="opencode",
            review_opencode_template="opencode run 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")

        with patch("shutil.which", return_value="/usr/bin/opencode"):
            with patch("qa_agent.review.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps({"findings": BACKEND_CANDIDATES_VALID}),
                    stderr="",
                )
                candidates = engine._generate_from_backend(run_id)

        assert len(candidates) == 2
        events = load_review_events(state, repo.config.name)
        success_events = [e for e in events if e.get("event") == "backend-generation succeeded"]
        assert len(success_events) == 1


# ---------------------------------------------------------------------------
# Test: _generate_from_backend with backend failure
# ---------------------------------------------------------------------------

class TestBackendGenerationFailure:
    """When backend command fails (non-zero exit), returns empty and
    records a 'failed' event so caller falls back to local stub."""

    def test_nonzero_exit_returns_empty_and_logs_failure_event(self):
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="claude",
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("qa_agent.review.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1,
                    stdout="",
                    stderr="claude error: something went wrong",
                )
                candidates = engine._generate_from_backend(run_id)

        assert candidates == []
        events = load_review_events(state, repo.config.name)
        failure_events = [e for e in events if e.get("event") == "backend-generation failed"]
        assert len(failure_events) == 1
        assert "claude error" in failure_events[0].get("error", "")
        assert failure_events[0].get("details", {}).get("fallback") == "local_stub_engaged"

    def test_cycle_on_backend_failure_falls_back_to_local_stub(self):
        """Full cycle: backend fails → fallback to local stub + failure event."""
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="claude",
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        stub_candidates = [
            {
                "repo": "test-repo",
                "path": "src/stub.ts",
                "line": 1,
                "header": "outstanding-todo",
                "snippet": "# TODO: from stub",
                "source": FindingSource.LINTER.value,
                "actionability": FindingActionability.LOW.value,
                "severity": FindingSeverity.LOW.value,
                "confidence": 0.5,
                "safe_to_autofix": False,
                "discovered_at": "2026-03-29T00:00:00Z",
            }
        ]
        engine._generate_local_candidates = lambda: list(stub_candidates)

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("qa_agent.review.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1,
                    stdout="",
                    stderr="claude error",
                )
                result = engine._run_autonomous_review_cycle(dry_run=False)

        assert result.findings_detected == 1
        events = load_review_events(state, repo.config.name)
        failure_events = [e for e in events if e.get("event") == "backend-generation failed"]
        assert len(failure_events) == 1
        completed_events = [e for e in events if e.get("event") == "autonomous-review-completed"]
        assert len(completed_events) == 1


# ---------------------------------------------------------------------------
# Test: _generate_from_backend with invalid output
# ---------------------------------------------------------------------------

class TestBackendGenerationInvalidOutput:
    """When backend returns invalid JSON or unexpected type, returns empty
    and records an appropriate event so caller falls back to local stub."""

    def test_non_json_output_returns_empty_and_logs_event(self):
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="claude",
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("qa_agent.review.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout="This is not JSON output",
                    stderr="",
                )
                candidates = engine._generate_from_backend(run_id)

        assert candidates == []
        events = load_review_events(state, repo.config.name)
        invalid_events = [e for e in events if e.get("event") == "backend-generation invalid-json"]
        assert len(invalid_events) == 1
        assert invalid_events[0].get("details", {}).get("fallback") == "local_stub_engaged"

    def test_empty_output_returns_empty_and_logs_event(self):
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="claude",
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("qa_agent.review.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout="   ",  # whitespace only
                    stderr="",
                )
                candidates = engine._generate_from_backend(run_id)

        assert candidates == []
        events = load_review_events(state, repo.config.name)
        empty_events = [e for e in events if e.get("event") == "backend-generation empty"]
        assert len(empty_events) == 1

    def test_unexpected_json_type_returns_empty_and_logs_event(self):
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="claude",
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("qa_agent.review.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps("just a string"),  # string, not dict/list
                    stderr="",
                )
                candidates = engine._generate_from_backend(run_id)

        assert candidates == []
        events = load_review_events(state, repo.config.name)
        type_events = [e for e in events if e.get("event") == "backend-generation unexpected-type"]
        assert len(type_events) == 1

    def test_all_invalid_candidates_returns_empty_and_logs_event(self):
        """Backend returns JSON but all candidates fail normalize_candidate."""
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="claude",
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")
        invalid_candidates = [
            {"repo": "test-repo"},  # missing required fields
            {"repo": "test-repo", "path": "x", "line": 1},  # missing header, source
        ]

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("qa_agent.review.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps(invalid_candidates),
                    stderr="",
                )
                candidates = engine._generate_from_backend(run_id)

        assert candidates == []
        events = load_review_events(state, repo.config.name)
        no_valid_events = [e for e in events if e.get("event") == "backend-generation no-valid-candidates"]
        assert len(no_valid_events) == 1


# ---------------------------------------------------------------------------
# Test: observation mode unchanged
# ---------------------------------------------------------------------------

class TestObservationModeUnchanged:
    """Observation mode does not use backend generation — only autonomous
    review mode triggers the bridge."""

    def test_observation_mode_runs_without_backend_generation(self):
        """Ensure observation mode does not call _generate_from_backend."""
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            review_care_mode=ReviewMode.OBSERVATION.value,
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Spy on _generate_from_backend
        called = False
        original = engine._generate_from_backend
        def spy(*args, **kwargs):
            nonlocal called
            called = True
            return original(*args, **kwargs)
        engine._generate_from_backend = spy

        result = engine.run(dry_run=False)

        # Observation mode should not trigger autonomous review cycle
        assert not called
        # Result counters should be zero (observation mode doesn't process)
        assert result.findings_detected == 0


# ---------------------------------------------------------------------------
# Test: dry_run guard still works
# ---------------------------------------------------------------------------

class TestDryRunGuard:
    """dry_run=True returns immediately without any candidate generation."""

    def test_dry_run_skips_all_generation(self):
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        # Spy on _generate_from_backend
        called = False
        original = engine._generate_from_backend
        def spy(*args, **kwargs):
            nonlocal called
            called = True
            return original(*args, **kwargs)
        engine._generate_from_backend = spy

        result = engine._run_autonomous_review_cycle(dry_run=True)

        assert not called
        assert result.findings_detected == 0
        assert result.findings_published == 0


# ---------------------------------------------------------------------------
# Test: backend available but deterministic fallback when not claude/opencode
# ---------------------------------------------------------------------------

class TestBackendResolution:
    """When fix_engine is 'deterministic', backend generation is skipped."""

    def test_deterministic_engine_skips_backend(self):
        tmp = _isolated_tmp()
        repo = make_repo(
            tmp,
            fix_engine="deterministic",
            review_claude_template="claude --print 'Read {prompt_file}'",
        )
        state = StateManager(tmp / "repos")
        state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
        engine = make_engine(repo, state)

        run_id = generate_id("arun")
        candidates = engine._generate_from_backend(run_id)

        assert candidates == []
        events = load_review_events(state, repo.config.name)
        skip_events = [e for e in events if e.get("event") == "backend-generation skipped"]
        assert len(skip_events) == 1
        assert "deterministic" in skip_events[0].get("reason", "")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
