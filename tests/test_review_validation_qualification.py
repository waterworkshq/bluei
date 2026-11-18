#!/usr/bin/env python3
"""Review-side validation qualification (Stage1 F10).

Covers ``WorktreeMixin._run_validation`` — the review wrapper over
``engine.run_named_checks`` — and its gate in ``_execute_prepared_remediation``:

- No configured checks (config empty AND guess yields none) -> ``ok=False``
  with the closed reason ``unqualified: no-validation-configured``.
- Commands whose runtime is unavailable (missing executable / missing script)
  are recorded as unavailable evidence and never crash, never pass.
- Review validation is strict: every check must be available AND rc==0
  (no baseline-failure tolerance).
- An unqualified or unavailable validation never reaches the commit/push
  boundary even when the backend succeeded (rc 0) and files changed.

Helpers are intentionally self-contained (no cross-test imports) and follow
the ``ReviewCycleEngine.__new__`` fixture pattern used across review tests:
an engine built this way has no ``baseline_inferrer`` attribute, so
``_guess_validation_commands`` deterministically returns ``[]``.
"""

from pathlib import Path

import pytest

from bluei.app.models import Repo, RepoConfig
from bluei.app.state import StateManager
from bluei.engine.validation import RC_UNAVAILABLE
from bluei.review.cycle import ReviewCycleEngine

UNQUALIFIED_REASON = "unqualified: no-validation-configured"
FAILED_REASON = "validation-failed-or-unavailable"


def _make_engine(tmp_path: Path, **repo_overrides) -> ReviewCycleEngine:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    defaults = dict(
        id="repo-test",
        name="owner/test-repo",
        path=str(repo_path),
        language="typescript",
    )
    defaults.update(repo_overrides)
    repo = Repo(config=RepoConfig(**defaults))
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    return engine


def test_no_validation_commands_reports_unqualified(tmp_path):
    """No config checks and no guessable commands -> unqualified, never a pass."""
    engine = _make_engine(tmp_path)  # no baseline_checks, no baseline_inferrer
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = engine._run_validation(worktree)

    assert result["ok"] is False
    assert result["results"] == []
    assert result["reason"] == UNQUALIFIED_REASON


def test_missing_executable_and_script_unavailable_no_crash(tmp_path):
    """Missing PATH executable and missing relative script are unavailable evidence, not crashes or passes."""
    engine = _make_engine(
        tmp_path,
        baseline_checks=[
            ["bluei-no-such-tool-3f9a", "--check"],  # bare name absent from PATH
            ["./no-such-review-script.sh", "arg"],  # relative script absent in worktree
        ],
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = engine._run_validation(worktree)  # must not raise

    assert result["ok"] is False
    assert result["reason"] == FAILED_REASON
    assert len(result["results"]) == 2
    for record in result["results"]:
        assert record["available"] is False
        assert record["returncode"] == RC_UNAVAILABLE


def test_executed_check_nonzero_rc_fails_strictly(tmp_path):
    """A check that genuinely executes with rc!=0 fails review validation (no baseline-failure tolerance)."""
    engine = _make_engine(tmp_path, baseline_checks=[["false"]])
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = engine._run_validation(worktree)

    assert result["ok"] is False
    assert result["reason"] == FAILED_REASON
    record = result["results"][0]
    assert record["available"] is True  # executed — real rc kept, not unavailable
    assert record["returncode"] == 1


def test_available_zero_rc_check_passes(tmp_path):
    """Available check with rc==0 qualifies validation."""
    engine = _make_engine(tmp_path, baseline_checks=[["true"]])
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = engine._run_validation(worktree)

    assert result["ok"] is True
    assert result["reason"] == "completed"
    record = result["results"][0]
    assert record["command"] == ["true"]
    assert record["available"] is True
    assert record["returncode"] == 0


@pytest.mark.parametrize("qualification", ["no-checks", "unavailable-check"])
def test_unqualified_or_unavailable_validation_never_commits_or_pushes(
    tmp_path, qualification
):
    """With a succeeding backend (rc 0) and changed files, unqualified/unavailable validation blocks the commit/push boundary."""
    repo_overrides = (
        {} if qualification == "no-checks" else {"baseline_checks": [["./missing.sh"]]}
    )
    engine = _make_engine(tmp_path, **repo_overrides)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    engine._run_shell = lambda command, cwd: {
        "returncode": 0,
        "stdout": "backend done",
        "stderr": "",
        "timed_out": False,
    }
    # This test targets validation, not the separate HEAD-unavailable refusal.
    engine._run_git_result = lambda command, cwd: {
        "returncode": 0, "stdout": "a" * 40, "stderr": "", "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: ["src/reviewed.ts"]

    def _forbidden_boundary(*args, **kwargs):
        raise AssertionError(
            "commit/push boundary must not be reached when validation is "
            f"{qualification}"
        )

    engine._apply_commit_push_boundary = _forbidden_boundary

    # State as handed over by the managed-PR retry queue.
    plan = {
        "status": "retry_prepared",
        "worktree": {"worktree_path": str(worktree)},
        "backend_command": "backend-run",
    }
    review_record = {"attempts_used": 0}

    result = engine._execute_prepared_remediation(
        plan,
        review_record,
        dry_run=False,
        allow_review_push=True,  # even an authorized push is gated
        snapshot={"pr_number": 42, "branch": "qa/fix-42"},
    )

    assert result["executed"] is True
    assert result["status"] == "retry_failed_validation"
    assert result["push_result"] is None
    assert result["attempts_used"] == 1
    assert result["validation"]["ok"] is False
    expected_reason = (
        UNQUALIFIED_REASON if qualification == "no-checks" else FAILED_REASON
    )
    assert result["validation"]["reason"] == expected_reason
