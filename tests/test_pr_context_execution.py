#!/usr/bin/env python3
"""Tests for Phase G3: PR-context autonomous-review execution.

Run with:
    PYTHONPATH=. .venv/bin/python -m pytest tests/test_pr_context_execution.py -v

These tests verify that when a PR number is known, it is carried through:
- Run creation (ReviewRun.pr_number)
- Prompt artifact metadata (PR context section)
- Publish state (targeted_pr_number in run entry)
- Summary generation (PR number in comment header)
- Events (resolved_target_pr)
- Backend generation bridge (pr_context dict)
- Consistent re-targeting across reruns
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import MagicMock, patch

from qa_agent.models import (
    Repo,
    RepoConfig,
    ReviewMode,
    PublishStatus,
    generate_id,
)
from qa_agent.review import (
    ReviewCycleEngine,
    GitHubReviewProvider,
    build_review_summary_comment,
    build_run_publish_entry,
    build_publish_entry,
    ReconciliationResult,
)
from qa_agent.state import StateManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_repo(tmp_path: Path, github_overrides: dict = None) -> Repo:
    github = {
        "live_actions": False,
        "auto_merge": False,
    }
    if github_overrides:
        github.update(github_overrides)
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
        github=github,
    )
    return Repo(config=config)


def make_engine(repo: Repo, state: StateManager) -> ReviewCycleEngine:
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = state
    engine.provider = MagicMock(spec=GitHubReviewProvider)
    return engine


def make_reconciliation() -> ReconciliationResult:
    return ReconciliationResult(
        new_findings=["rf-abc001-000"],
        already_published=["rf-abc002-000"],
        absent_findings=[],
        superseded_findings=[],
        pending_findings=[],
        all_prior_findings=["rf-abc002-000"],
    )


# ---------------------------------------------------------------------------
# Test: _resolve_pr_context_for_autonomous_run — explicit config wins
# ---------------------------------------------------------------------------

class TestResolvePrContextExplicit:
    """Explicit pr_number in github config is used without consulting live_actions."""

    def test_explicit_pr_number_returns_it(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": 42, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        prior = state.load_review_publish_state(repo.config.name)
        pr_num, reason = engine._resolve_pr_context_for_autonomous_run(prior)

        assert pr_num == 42
        assert reason == "explicit-config-pr-42"

    def test_explicit_pr_non_integer_coerced(self, tmp_path):
        """A string pr_number like '42' is coerced to int."""
        repo = make_repo(tmp_path, {"pr_number": "99", "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        prior = state.load_review_publish_state(repo.config.name)
        pr_num, reason = engine._resolve_pr_context_for_autonomous_run(prior)

        assert pr_num == 99
        assert reason == "explicit-config-pr-99"

    def test_explicit_pr_invalid_string_falls_through(self, tmp_path):
        """A non-numeric pr_number falls through to live_actions check."""
        repo = make_repo(tmp_path, {"pr_number": "not-a-number", "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        prior = state.load_review_publish_state(repo.config.name)
        pr_num, reason = engine._resolve_pr_context_for_autonomous_run(prior)

        # live_actions is False, so no further resolution
        assert pr_num is None
        assert reason == "live-actions-disabled"

    def test_explicit_pr_live_actions_disabled_still_wins(self, tmp_path):
        """Even when live_actions is False, explicit pr_number is used."""
        repo = make_repo(tmp_path, {"pr_number": 7, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        prior = state.load_review_publish_state(repo.config.name)
        pr_num, reason = engine._resolve_pr_context_for_autonomous_run(prior)

        assert pr_num == 7
        # Does NOT fall through to _resolve_target_pr_for_run since explicit wins


# ---------------------------------------------------------------------------
# Test: _resolve_pr_context_for_autonomous_run — live_actions fallback
# ---------------------------------------------------------------------------

class TestResolvePrContextLiveActions:
    """When no explicit pr_number, live_actions=True falls back to _resolve_target_pr_for_run."""

    def test_no_pr_number_live_actions_false_returns_none(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": None, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        prior = state.load_review_publish_state(repo.config.name)
        pr_num, reason = engine._resolve_pr_context_for_autonomous_run(prior)

        assert pr_num is None
        assert reason == "live-actions-disabled"

    def test_no_pr_number_live_actions_true_calls_target_pr_resolver(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": None, "live_actions": True})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        # Simulate a prior publish state with a targeted PR
        prior = state.load_review_publish_state(repo.config.name)
        prior["runs"] = {
            "prior-run": {
                "targeted_pr_number": 55,
                "status": "published",
            }
        }

        with patch.object(
            engine, "_resolve_target_pr_for_run", return_value=(55, "prior-targeted-55")
        ) as mock_resolve:
            pr_num, reason = engine._resolve_pr_context_for_autonomous_run(prior)

        mock_resolve.assert_called_once_with(prior)
        assert pr_num == 55
        assert reason == "resolved-prior-targeted-55"


# ---------------------------------------------------------------------------
# Test: pr_number persisted in ReviewRun artifact
# ---------------------------------------------------------------------------

class TestPrNumberInReviewRun:
    """When PR context is known, it is stored in the ReviewRun JSON file."""

    def test_review_run_file_records_pr_number(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": 31})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        # Stub candidates to keep the run fast
        with patch.object(
            engine, "_generate_from_backend", return_value=[]
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            engine.run(dry_run=False)

        run_files = list(state.get_review_runs_dir(repo.config.name).glob("*.json"))
        assert len(run_files) == 1

        data = json.loads(run_files[0].read_text())
        assert data["pr_number"] == 31
        assert data["run_id"] == data["id"]

    def test_review_run_file_pr_number_none_when_no_context(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": None, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        with patch.object(
            engine, "_generate_from_backend", return_value=[]
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            engine.run(dry_run=False)

        run_files = list(state.get_review_runs_dir(repo.config.name).glob("*.json"))
        assert len(run_files) == 1

        data = json.loads(run_files[0].read_text())
        assert data["pr_number"] is None


# ---------------------------------------------------------------------------
# Test: targeted_pr_number in publish-state run entry
# ---------------------------------------------------------------------------

class TestPrNumberInPublishState:
    """The run publish entry records targeted_pr_number when a PR is known."""

    def test_run_publish_entry_has_targeted_pr_number(self):
        entry = build_run_publish_entry(
            status=PublishStatus.PUBLISHED,
            run_id="run-42",
            findings_total=5,
            findings_published=3,
            findings_failed=0,
            targeted_pr_number=17,
        )
        assert entry["targeted_pr_number"] == 17
        assert entry["status"] == "published"

    def test_run_publish_entry_omits_targeted_pr_when_none(self):
        entry = build_run_publish_entry(
            status=PublishStatus.PUBLISHED,
            run_id="run-42",
            findings_total=5,
            findings_published=3,
            findings_failed=0,
        )
        assert "targeted_pr_number" not in entry

    def test_run_publish_entry_has_targeted_pr_url_when_provided(self):
        entry = build_run_publish_entry(
            status=PublishStatus.PUBLISHED,
            run_id="run-42",
            findings_total=5,
            findings_published=3,
            findings_failed=0,
            targeted_pr_number=17,
            targeted_pr_url="https://github.com/owner/repo/pull/17",
        )
        assert entry["targeted_pr_number"] == 17
        assert entry["targeted_pr_url"] == "https://github.com/owner/repo/pull/17"

    def test_publish_state_records_targeted_pr_after_run(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": 22, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        with patch.object(
            engine, "_generate_from_backend", return_value=[]
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            engine.run(dry_run=False)

        pstate = state.load_review_publish_state(repo.config.name)
        assert len(pstate["runs"]) == 1
        run_id = list(pstate["runs"].keys())[0]
        assert pstate["runs"][run_id]["targeted_pr_number"] == 22

    def test_publish_state_targeted_pr_none_when_no_context(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": None, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        with patch.object(
            engine, "_generate_from_backend", return_value=[]
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            engine.run(dry_run=False)

        pstate = state.load_review_publish_state(repo.config.name)
        run_id = list(pstate["runs"].keys())[0]
        # When no PR context, targeted_pr_number key is absent (not stored as null)
        assert "targeted_pr_number" not in pstate["runs"][run_id]


# ---------------------------------------------------------------------------
# Test: PR context in prompt artifact
# ---------------------------------------------------------------------------

class TestPrContextInPromptArtifact:
    """When pr_context is provided, _build_candidate_prompt_artifact includes it."""

    def test_prompt_artifact_includes_pr_context(self, tmp_path):
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        pr_context = {"pr_number": 99, "pr_url": None, "resolution": "explicit-config-pr-99"}
        artifact = engine._build_candidate_prompt_artifact(pr_context=pr_context)

        assert "#99" in artifact or "PR Number: #99" in artifact
        assert "99" in artifact
        # PR context section must mention the PR number
        assert "PR Context" in artifact

    def test_prompt_artifact_no_pr_context_section_when_none(self, tmp_path):
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        artifact = engine._build_candidate_prompt_artifact(pr_context=None)

        assert "PR Context" not in artifact
        # Still contains repo info
        assert "Repository" in artifact

    def test_prompt_artifact_backward_compatible_no_arg(self, tmp_path):
        """Calling without pr_context arg still works (backward compat)."""
        repo = make_repo(tmp_path)
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        artifact = engine._build_candidate_prompt_artifact()

        assert "Repository" in artifact
        assert "PR Context" not in artifact


# ---------------------------------------------------------------------------
# Test: backend generation bridge receives pr_context
# ---------------------------------------------------------------------------

class TestPrContextInBackendBridge:
    """_generate_from_backend accepts and uses pr_context when calling _build_candidate_prompt_artifact."""

    def test_backend_bridge_passes_pr_context(self, tmp_path):
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
            github={"pr_number": 55, "live_actions": False},
            review_claude_template="test-template",  # direct field, not inside github
        )
        repo = Repo(config=config)
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        pr_context = {"pr_number": 55, "pr_url": None, "resolution": "explicit-config-pr-55"}

        captured_args = {}

        def capture_prompt_artifact(*args, **kwargs):
            captured_args["pr_context"] = kwargs.get("pr_context")
            return "# dummy artifact\n[]"

        with patch.object(
            type(engine), "_resolve_backend", return_value="claude"
        ), patch.object(
            type(engine), "_build_candidate_prompt_artifact", side_effect=capture_prompt_artifact
        ), patch.object(
            type(engine), "_run_backend_candidate_command", return_value="[]"
        ):
            result = engine._generate_from_backend(
                run_id="test-run",
                pr_context=pr_context,
            )

        assert captured_args["pr_context"] == pr_context

    def test_backend_bridge_no_pr_context_still_works(self, tmp_path):
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
            github={"live_actions": False},
            review_claude_template="test-template",
        )
        repo = Repo(config=config)
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        captured_args = {}

        def capture_prompt_artifact(*args, **kwargs):
            captured_args["pr_context"] = kwargs.get("pr_context")
            return "# dummy artifact\n[]"

        with patch.object(
            type(engine), "_resolve_backend", return_value="claude"
        ), patch.object(
            type(engine), "_build_candidate_prompt_artifact", side_effect=capture_prompt_artifact
        ), patch.object(
            type(engine), "_run_backend_candidate_command", return_value="[]"
        ):
            result = engine._generate_from_backend(run_id="test-run")

        assert captured_args["pr_context"] is None


# ---------------------------------------------------------------------------
# Test: PR number in summary comment
# ---------------------------------------------------------------------------

class TestPrNumberInSummaryComment:
    """build_review_summary_comment includes PR number in header when provided."""

    def test_comment_header_includes_pr_number(self):
        recon = make_reconciliation()
        comment = build_review_summary_comment(
            repo="owner/repo",
            run_id="run-abc",
            reconciliation=recon,
            run_status="completed",
            pr_number=12,
        )
        # PR number should appear in the header section
        assert "#12" in comment or "PR:" in comment
        # Repo name must still be present
        assert "owner/repo" in comment

    def test_comment_no_pr_section_when_pr_number_none(self):
        recon = make_reconciliation()
        comment = build_review_summary_comment(
            repo="owner/repo",
            run_id="run-abc",
            reconciliation=recon,
            run_status="completed",
        )
        assert "## PR Context" not in comment
        # Repo and run_id still present
        assert "owner/repo" in comment
        assert "run-abc" in comment


# ---------------------------------------------------------------------------
# Test: consistent PR targeting across reruns
# ---------------------------------------------------------------------------

class TestConsistentPrTargetingReruns:
    """When a PR is explicitly configured, reruns always target the same PR."""

    def test_rerun_targets_same_pr_from_config(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": 77, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        captured_pr_numbers = []

        # Match _generate_from_backend signature
        def capture_pr_context(run_id, pr_context=None):
            if pr_context:
                captured_pr_numbers.append(pr_context.get("pr_number"))
            return []

        with patch.object(
            engine, "_generate_from_backend", side_effect=capture_pr_context
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            # First run
            engine.run(dry_run=False)
            # Second run
            engine.run(dry_run=False)

        assert captured_pr_numbers == [77, 77]

    def test_publish_state_records_same_pr_on_both_runs(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": 88, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        with patch.object(
            engine, "_generate_from_backend", return_value=[]
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            engine.run(dry_run=False)
            engine.run(dry_run=False)

        pstate = state.load_review_publish_state(repo.config.name)
        pr_numbers = [
            entry["targeted_pr_number"]
            for entry in pstate["runs"].values()
        ]
        assert pr_numbers == [88, 88]


# ---------------------------------------------------------------------------
# Test: local-only fallback when no PR context
# ---------------------------------------------------------------------------

class TestLocalOnlyFallback:
    """When no PR context is available, the run proceeds in local-only mode safely."""

    def test_no_pr_context_run_completes_without_error(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": None, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        with patch.object(
            engine, "_generate_from_backend", return_value=[]
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            result = engine.run(dry_run=False)

        # Run completed without error
        assert result.findings_detected == 0
        # ReviewRun artifact created
        run_files = list(state.get_review_runs_dir(repo.config.name).glob("*.json"))
        assert len(run_files) == 1

    def test_no_pr_context_publish_state_has_null_target(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": None, "live_actions": False})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        with patch.object(
            engine, "_generate_from_backend", return_value=[]
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            engine.run(dry_run=False)

        pstate = state.load_review_publish_state(repo.config.name)
        run_entry = list(pstate["runs"].values())[0]
        # When no PR context, targeted_pr_number key is absent (not stored as null)
        assert "targeted_pr_number" not in run_entry

    def test_live_actions_true_but_no_pr_available_stays_local(self, tmp_path):
        """When live_actions=True but no PR can be resolved, run stays local."""
        repo = make_repo(tmp_path, {"pr_number": None, "live_actions": True})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        # _resolve_target_pr_for_run returns (None, reason) when no PR available
        with patch.object(
            engine, "_resolve_target_pr_for_run", return_value=(None, "no-open-prs")
        ), patch.object(
            engine, "_generate_from_backend", return_value=[]
        ), patch.object(
            engine, "_generate_local_candidates", return_value=[]
        ):
            result = engine.run(dry_run=False)

        # Still ran successfully in local-only mode
        assert result.findings_detected == 0
        pstate = state.load_review_publish_state(repo.config.name)
        run_entry = list(pstate["runs"].values())[0]
        # targeted_pr_number is None when no PR was resolved (explicit null)
        assert run_entry.get("targeted_pr_number") is None
        # Error recorded explaining why PR was refused
        assert "no-open-prs" in run_entry.get("error", "")


# ---------------------------------------------------------------------------
# Test: dry_run does not call PR resolution (avoids network/IO)
# ---------------------------------------------------------------------------

class TestDryRunNoPrResolution:
    """dry_run=True returns immediately without resolving PR context."""

    def test_dry_run_skips_pr_resolution(self, tmp_path):
        repo = make_repo(tmp_path, {"pr_number": 5, "live_actions": True})
        state = StateManager(tmp_path / "repos")
        engine = make_engine(repo, state)

        with patch.object(
            engine, "_resolve_pr_context_for_autonomous_run", return_value=(5, "explicit")
        ) as mock_resolve:
            result = engine.run(dry_run=True)

        # PR resolution was NOT called
        mock_resolve.assert_not_called()
        # No run artifact created
        assert list(state.get_review_runs_dir(repo.config.name).glob("*.json")) == []
