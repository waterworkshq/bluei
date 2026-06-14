#!/usr/bin/env python3
"""
Integration tests for the full review-cycle lifecycle.

These tests exercise the complete flow:
- Review feedback detected
- Remediation planned/prepared
- Execution simulated
- Unattended push blocked (retry_pending_push)
- Status artifacts reflect correct state

UNATTENDED PUSH POLICY:
- Tests verify the conservative policy where unattended runs NEVER push.
- Push requires explicit --allow-review-push flag.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from bluei.app.models import Repo, RepoConfig
from bluei.review.cycle import ReviewCycleEngine
from bluei.review.provider import GitHubReviewProvider
from bluei.review.types import ReviewCycleResult
from bluei.app.state import StateManager


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
                "max_attempts": 3,
                "max_loops": 2,
                "max_prs_per_run": 1,
            },
        )
    )


def make_engine(repo: Repo, state: StateManager) -> ReviewCycleEngine:
    """Create a ReviewCycleEngine with mocked provider."""
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


def test_full_lifecycle_unattended_push_blocked(tmp_path):
    """
    END-TO-END LIFECYCLE TEST:

    Slices through the entire review-cycle flow:
    1. Provider observes a PR with review feedback (blocked state)
    2. Review state is persisted correctly
    3. Remediation is planned and prepared
    4. Backend command executes (simulated) with changed files
    5. Unattended push policy blocks the push -> retry_pending_push
    6. Status artifacts show "awaiting_operator_push" state
    7. status.json reflects correct metrics

    This validates the core integration is complete and the unattended
    push policy is enforced end-to-end.
    """
    repo = make_repo(tmp_path)
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = make_engine(repo, state)
    mock_provider = engine.provider

    mock_provider.list_managed_prs.return_value = [
        {
            "number": 42,
            "url": "https://github.com/owner/test-repo/pull/42",
            "headRefName": "qa/fix-issue-42",
            "author": {"login": "sound"},
            "title": "Fix issue 42",
            "isDraft": False,
            "state": "OPEN",
        }
    ]

    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 42,
        "pr_url": "https://github.com/owner/test-repo/pull/42",
        "branch": "qa/fix-issue-42",
        "author": "sound",
        "fetched_at": "2026-03-22T12:00:00Z",
        "review_decision": "CHANGES_REQUESTED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": ["reviewer1"],
        "actionable_comments": [
            {"author": "reviewer1", "body": "please add tests for this function"}
        ],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "abc123def456",
    }

    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    engine._prepare_worktree = lambda snapshot, dry_run: {
        "worktree_path": str(worktree_path),
        "local_branch": "qa-review-pr-42",
        "prepared": not dry_run,
        "dry_run": dry_run,
    }
    engine._run_shell = lambda command, cwd, timeout_seconds=900: {
        "returncode": 0,
        "stdout": "Simulated backend execution",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: ["src/fix.ts", "tests/fix.test.ts"]
    engine._run_validation = lambda worktree_path: {
        "ok": True,
        "results": [],
        "reason": "completed",
    }

    result = engine.run(dry_run=False, allow_review_push=False)

    assert result.active_prs == 1
    assert result.blocked_prs == 1
    assert result.retry_eligible_prs == 0
    assert result.retry_planned_prs == 1

    active_prs = state.load_active_prs(repo.config.name)
    assert "42" in active_prs["prs"]
    pr_record = active_prs["prs"]["42"]

    assert pr_record["status"] == "retry_pending_push", (
        f"Expected 'retry_pending_push', got '{pr_record['status']}'"
    )
    assert pr_record["merge_readiness"]["state"] == "awaiting_operator_push"
    assert "Validated remediation" in pr_record["merge_readiness"]["reason"]

    assert "execution_result" in pr_record
    exec_result = pr_record["execution_result"]
    assert exec_result["status"] == "retry_pending_push"
    assert exec_result["executed"] is True
    assert exec_result["push_result"]["status"] == "pending_operator_confirmation"
    assert exec_result["push_result"]["allow_review_push"] is False
    assert len(exec_result["changed_files"]) == 2

    review_state = state.load_review_state(repo.config.name)
    assert "42" in review_state["prs"]
    review_record = review_state["prs"]["42"]
    assert review_record["attempts_used"] == 1
    assert review_record["retry_eligible"] is False
    assert review_record["last_action"] == "retry_pending_push"

    status_file = state._get_state_dir(repo.config.name) / "status.json"
    assert status_file.exists()
    with open(status_file) as f:
        status_data = json.load(f)

    assert status_data["review_care"]["active_managed_prs"] == 1
    assert status_data["review_care"]["review_blocked_prs"] == 1
    assert status_data["review_care"]["retry_eligible_prs"] == 0

    events_file = state._get_state_dir(repo.config.name) / "review_events.jsonl"
    assert events_file.exists()
    events = events_file.read_text().strip().split("\n")
    assert len(events) >= 1

    first_event = json.loads(events[0])
    assert first_event["pr_number"] == 42
    assert first_event["event"] in ("review_feedback_detected", "retry_prepared")
    last_event = json.loads(events[-1])
    assert last_event["event"] == "retry_pending_push"


def test_full_lifecycle_explicit_push_allowed(tmp_path):
    """
    END-TO-END LIFECYCLE TEST (PUSH ALLOWED):

    Same as above but WITH explicit --allow-review-push flag.
    Verifies the push path works when explicitly enabled.
    """
    repo = make_repo(tmp_path)
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = make_engine(repo, state)
    mock_provider = engine.provider

    mock_provider.list_managed_prs.return_value = [
        {
            "number": 99,
            "url": "https://github.com/owner/test-repo/pull/99",
            "headRefName": "qa/fix-issue-99",
            "author": {"login": "sound"},
            "title": "Fix issue 99",
            "isDraft": False,
            "state": "OPEN",
        }
    ]

    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 99,
        "pr_url": "https://github.com/owner/test-repo/pull/99",
        "branch": "qa/fix-issue-99",
        "author": "sound",
        "fetched_at": "2026-03-22T12:00:00Z",
        "review_decision": "CHANGES_REQUESTED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": ["reviewer2"],
        "actionable_comments": [
            {"author": "reviewer2", "body": "fix the bug in this function"}
        ],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "xyz789",
    }

    worktree_path = tmp_path / "worktree-push"
    worktree_path.mkdir()

    engine._prepare_worktree = lambda snapshot, dry_run: {
        "worktree_path": str(worktree_path),
        "local_branch": "qa-review-pr-99",
        "prepared": not dry_run,
        "dry_run": dry_run,
    }
    engine._run_shell = lambda command, cwd, timeout_seconds=900: {
        "returncode": 0,
        "stdout": "Simulated backend execution",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: ["src/fix.ts"]
    engine._run_validation = lambda worktree_path: {
        "ok": True,
        "results": [],
        "reason": "completed",
    }
    engine._apply_commit_push_boundary = (
        lambda worktree_path, snapshot, changed_files, allow_review_push: {
            "status": "pushed",
            "allow_review_push": allow_review_push,
            "target_branch": snapshot["branch"],
            "changed_files": changed_files,
            "cleanup": {"removed": True},
        }
    )

    result = engine.run(dry_run=False, allow_review_push=True)

    assert result.active_prs == 1
    assert result.blocked_prs == 1

    active_prs = state.load_active_prs(repo.config.name)
    pr_record = active_prs["prs"]["99"]

    assert pr_record["status"] == "retry_pushed", (
        f"Expected 'retry_pushed', got '{pr_record['status']}'"
    )
    assert pr_record["merge_readiness"]["state"] == "awaiting_re_review"

    exec_result = pr_record["execution_result"]
    assert exec_result["status"] == "retry_pushed"
    assert exec_result["push_result"]["status"] == "pushed"
    assert exec_result["push_result"]["allow_review_push"] is True


def test_lifecycle_exhaustion_without_push(tmp_path):
    """
    Test that exhausted PRs do not attempt remediation at all.

    When attempts_used >= max_attempts, the flow should:
    1. Detect the PR
    2. Mark as exhausted
    3. NOT plan or execute remediation
    """
    repo = make_repo(tmp_path)
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    pre_existing_review_state = {
        "prs": {
            "55": {
                "attempts_used": 3,
                "last_snapshot_fingerprint": "existing-fp",
                "last_action": "retry_failed",
            }
        }
    }
    state.save_review_state(repo.config.name, pre_existing_review_state)

    engine = make_engine(repo, state)
    mock_provider = engine.provider

    mock_provider.list_managed_prs.return_value = [
        {
            "number": 55,
            "url": "https://github.com/owner/test-repo/pull/55",
            "headRefName": "qa/fix-issue-55",
            "author": {"login": "sound"},
            "title": "Fix issue 55",
            "isDraft": False,
            "state": "OPEN",
        }
    ]

    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 55,
        "pr_url": "https://github.com/owner/test-repo/pull/55",
        "branch": "qa/fix-issue-55",
        "author": "sound",
        "fetched_at": "2026-03-22T12:00:00Z",
        "review_decision": "CHANGES_REQUESTED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": ["reviewer1"],
        "actionable_comments": [{"author": "reviewer1", "body": "fix this"}],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "new-fingerprint",
    }

    remediation_called = []
    engine._plan_remediation = lambda snapshot, review_record, dry_run: (
        remediation_called.append(snapshot["pr_number"]) or None
    )

    result = engine.run(dry_run=False, allow_review_push=False)

    assert result.active_prs == 1
    assert result.blocked_prs == 1
    assert result.retry_eligible_prs == 0
    assert result.retry_exhausted_prs == 1
    assert result.retry_planned_prs == 0

    assert len(remediation_called) == 0, (
        "Should NOT have planned remediation for exhausted PR"
    )

    active_prs = state.load_active_prs(repo.config.name)
    pr_record = active_prs["prs"]["55"]

    assert pr_record["status"] == "retry_exhausted"
    assert (
        "exhausted" in pr_record["merge_readiness"]["reason"].lower()
        or "3" in pr_record["merge_readiness"]["reason"]
    )

    review_state = state.load_review_state(repo.config.name)
    assert review_state["prs"]["55"]["retry_eligible"] is False


def test_lifecycle_pending_push_preserved_across_runs(tmp_path):
    """
    Test that pending_push state is preserved when already executed and waiting.

    When a PR is in retry_pending_push with execution_result already saved,
    subsequent runs should:
    1. Preserve the pending state correctly
    2. Track the PR as blocked
    3. Keep awaiting_operator_push state

    Note: The current implementation may re-execute the remediation command,
    but the key invariant is that the state transitions correctly to
    retry_pending_push when push is not allowed.
    """
    repo = make_repo(tmp_path)
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    worktree_path = tmp_path / "existing_worktree"
    worktree_path.mkdir()

    pre_existing_active = {
        "prs": {
            "77": {
                "pr_number": 77,
                "url": "https://github.com/owner/test-repo/pull/77",
                "branch": "qa/fix-issue-77",
                "author": "sound",
                "source": "qa-agent-heuristic",
                "status": "retry_pending_push",
                "merge_readiness": {
                    "state": "awaiting_operator_push",
                    "reason": "Validated remediation is waiting for explicit commit/push approval",
                    "evaluated_at": "2026-03-22T10:00:00Z",
                },
            }
        }
    }
    pre_existing_review = {
        "prs": {
            "77": {
                "attempts_used": 1,
                "last_snapshot_fingerprint": "stable-fp-123",
                "last_action": "retry_pending_push",
                "planned_remediation": {
                    "status": "retry_prepared",
                    "prompt_file": "/tmp/prompt.md",
                    "worktree": {"worktree_path": str(worktree_path), "prepared": True},
                    "backend_command": "echo 'remediation-command'",
                },
                "execution_result": {
                    "status": "retry_pending_push",
                    "executed": True,
                    "changed_files": ["src/a.ts"],
                    "push_result": {
                        "status": "pending_operator_confirmation",
                        "target_branch": "qa/fix-issue-77",
                        "changed_files": ["src/a.ts"],
                    },
                    "attempts_used": 1,
                },
            }
        }
    }
    state.save_active_prs(repo.config.name, pre_existing_active)
    state.save_review_state(repo.config.name, pre_existing_review)

    engine = make_engine(repo, state)
    mock_provider = engine.provider

    mock_provider.list_managed_prs.return_value = [
        {
            "number": 77,
            "url": "https://github.com/owner/test-repo/pull/77",
            "headRefName": "qa/fix-issue-77",
            "author": {"login": "sound"},
            "title": "Fix issue 77",
            "isDraft": False,
            "state": "OPEN",
        }
    ]

    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 77,
        "pr_url": "https://github.com/owner/test-repo/pull/77",
        "branch": "qa/fix-issue-77",
        "author": "sound",
        "fetched_at": "2026-03-22T12:00:00Z",
        "review_decision": "CHANGES_REQUESTED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": ["reviewer1"],
        "actionable_comments": [{"author": "reviewer1", "body": "still needs work"}],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "stable-fp-123",
    }

    engine._run_shell = lambda command, cwd, timeout_seconds=900: {
        "returncode": 0,
        "stdout": "Simulated re-execution",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: ["src/a.ts"]
    engine._run_validation = lambda worktree_path: {
        "ok": True,
        "results": [],
        "reason": "completed",
    }

    result = engine.run(dry_run=False, allow_review_push=False)

    assert result.active_prs == 1
    assert result.blocked_prs == 1

    active_prs = state.load_active_prs(repo.config.name)
    pr_record = active_prs["prs"]["77"]

    assert pr_record["status"] == "retry_pending_push", (
        f"Expected 'retry_pending_push', got '{pr_record['status']}'"
    )
    assert pr_record["merge_readiness"]["state"] == "awaiting_operator_push"

    exec_result = pr_record["execution_result"]
    assert exec_result["status"] == "retry_pending_push"
    assert exec_result["push_result"]["status"] == "pending_operator_confirmation"
    assert exec_result["push_result"]["allow_review_push"] is False


def test_lifecycle_clean_pr_starts_pending_review_before_review_artifact(tmp_path):
    """
    A clean PR must not default straight to merge_ready.

    Until qa-agent has published a review artifact for the current snapshot,
    the PR should remain pending_review.
    """
    repo = make_repo(tmp_path)
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = make_engine(repo, state)
    mock_provider = engine.provider

    mock_provider.list_managed_prs.return_value = [
        {
            "number": 100,
            "url": "https://github.com/owner/test-repo/pull/100",
            "headRefName": "qa/fix-clean-pr",
            "author": {"login": "sound"},
            "title": "Fix clean PR",
            "isDraft": False,
            "state": "OPEN",
        }
    ]

    # CLEAN merge state, NO actionable comments, NO change requesters
    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 100,
        "pr_url": "https://github.com/owner/test-repo/pull/100",
        "branch": "qa/fix-clean-pr",
        "author": "sound",
        "fetched_at": "2026-04-01T06:00:00Z",
        "review_decision": "APPROVED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": [],
        "actionable_comments": [],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "clean-fingerprint",
    }

    result = engine.run(dry_run=False, allow_review_push=False)

    assert result.active_prs == 1
    assert result.merge_ready_prs == 0
    assert result.blocked_prs == 0
    assert result.retry_eligible_prs == 0

    active_prs = state.load_active_prs(repo.config.name)
    pr_record = active_prs["prs"]["100"]

    assert pr_record["status"] == "pending_review", (
        f"Expected 'pending_review', got '{pr_record['status']}'"
    )
    assert pr_record["merge_readiness"]["state"] == "awaiting_review_artifact"
    assert "review" in pr_record["merge_readiness"]["reason"].lower()

    review_state = state.load_review_state(repo.config.name)
    review_record = review_state["prs"]["100"]
    assert review_record["last_action"] == "pending_review"
    assert review_record["last_snapshot"]["merge_state_status"] == "CLEAN"
    assert review_record["last_snapshot"]["actionable_comment_count"] == 0


def test_review_cycle_posts_pr_comment_then_records_pending_review_first(tmp_path):
    repo = make_repo(tmp_path)
    repo.config.github["live_actions"] = True
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    engine = make_engine(repo, state)
    mock_provider = engine.provider

    mock_provider.list_managed_prs.return_value = [
        {
            "number": 77,
            "url": "https://github.com/owner/test-repo/pull/77",
            "headRefName": "qa/review-77",
            "author": {"login": "sound"},
            "title": "Review me",
            "isDraft": False,
            "state": "OPEN",
        }
    ]
    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 77,
        "pr_url": "https://github.com/owner/test-repo/pull/77",
        "branch": "qa/review-77",
        "author": "sound",
        "fetched_at": "2026-03-22T12:00:00Z",
        "review_decision": "REVIEW_REQUIRED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": [],
        "actionable_comments": [],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "merge-ready-77",
    }

    published = {}

    def _fake_publish(pr_number, summary_text, publication_key, existing_review):
        published["pr_number"] = pr_number
        published["summary_text"] = summary_text
        published["publication_key"] = publication_key
        return f"https://github.com/owner/test-repo/pull/{pr_number}#issuecomment-1"

    engine._publish_review_cycle_comment = _fake_publish

    result = engine.run(dry_run=False, allow_review_push=False)

    assert result.merge_ready_prs == 0
    assert published["pr_number"] == 77
    assert published["publication_key"] == "merge-ready-77:pending_review"
    assert "bluei Review, PR #77" in published["summary_text"]
    assert "`pending_review`" in published["summary_text"]

    review_state = state.load_review_state(repo.config.name)
    review_record = review_state["prs"]["77"]
    assert review_record["last_review_comment_key"] == "merge-ready-77:pending_review"
    assert review_record["last_review_comment_url"].endswith("#issuecomment-1")

    active_prs = state.load_active_prs(repo.config.name)
    assert active_prs["prs"]["77"]["status"] == "pending_review"
    assert active_prs["prs"]["77"]["review_comment"]["url"].endswith("#issuecomment-1")


def test_review_cycle_marks_unstable_snapshot_merge_ready_when_artifact_exists(
    tmp_path,
):
    repo = make_repo(tmp_path)
    repo.config.github["live_actions"] = True
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    state.save_review_state(
        repo.config.name,
        {
            "version": 1,
            "updated_at": "2026-03-22T11:59:00Z",
            "prs": {
                "77": {
                    "last_provider": "github",
                    "last_polled_at": "2026-03-22T11:59:00Z",
                    "last_snapshot_fingerprint": "merge-ready-77",
                    "last_snapshot": {
                        "review_decision": "REVIEW_REQUIRED",
                        "merge_state_status": "UNSTABLE",
                        "active_change_requesters": [],
                        "actionable_comment_count": 0,
                        "informational_comment_count": 0,
                    },
                    "attempts_used": 0,
                    "loop_count": 0,
                    "retry_eligible": False,
                    "last_action": "pending_review",
                    "last_action_at": "2026-03-22T11:59:00Z",
                    "last_action_reason": "No actionable review blockers and merge state is unstable",
                    "planned_remediation": None,
                    "execution_result": None,
                    "last_review_comment_key": "merge-ready-77:pending_review",
                    "last_review_comment_url": "https://github.com/owner/test-repo/pull/77#issuecomment-1",
                    "last_review_comment_at": "2026-03-22T11:59:00Z",
                    "escalation": None,
                }
            },
        },
    )

    engine = make_engine(repo, state)
    mock_provider = engine.provider
    mock_provider.list_managed_prs.return_value = [
        {
            "number": 77,
            "url": "https://github.com/owner/test-repo/pull/77",
            "headRefName": "qa/review-77",
            "author": {"login": "sound"},
            "title": "Review me",
            "isDraft": False,
            "state": "OPEN",
        }
    ]
    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 77,
        "pr_url": "https://github.com/owner/test-repo/pull/77",
        "branch": "qa/review-77",
        "author": "sound",
        "fetched_at": "2026-03-22T12:05:00Z",
        "review_decision": "REVIEW_REQUIRED",
        "merge_state_status": "UNSTABLE",
        "active_change_requesters": [],
        "actionable_comments": [],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "merge-ready-77",
    }

    result = engine.run(dry_run=False, allow_review_push=False)

    assert result.merge_ready_prs == 1

    active_prs = state.load_active_prs(repo.config.name)
    assert active_prs["prs"]["77"]["status"] == "merge_ready"
    assert active_prs["prs"]["77"]["merge_readiness"]["state"] == "ready_for_merge"
    assert (
        "pending fresh merge triage"
        in active_prs["prs"]["77"]["merge_readiness"]["reason"]
    )

    review_state = state.load_review_state(repo.config.name)
    assert review_state["prs"]["77"]["last_action"] == "merge_ready"


def test_clean_pr_becomes_merge_ready_after_pending_review_artifact_exists(tmp_path):
    repo = make_repo(tmp_path)
    repo.config.github["live_actions"] = True
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    state.save_review_state(
        repo.config.name,
        {
            "version": 1,
            "updated_at": "2026-03-22T11:59:00Z",
            "prs": {
                "77": {
                    "last_provider": "github",
                    "last_polled_at": "2026-03-22T11:59:00Z",
                    "last_snapshot_fingerprint": "merge-ready-77",
                    "last_snapshot": {
                        "review_decision": "REVIEW_REQUIRED",
                        "merge_state_status": "CLEAN",
                        "active_change_requesters": [],
                        "actionable_comment_count": 0,
                        "informational_comment_count": 0,
                    },
                    "attempts_used": 0,
                    "loop_count": 0,
                    "retry_eligible": False,
                    "last_action": "observed",
                    "last_action_at": "2026-03-22T11:59:00Z",
                    "last_action_reason": "Clean PR awaiting review artifact",
                    "planned_remediation": None,
                    "execution_result": None,
                    "last_review_comment_key": "merge-ready-77:pending_review",
                    "last_review_comment_url": "https://github.com/owner/test-repo/pull/77#issuecomment-1",
                    "last_review_comment_at": "2026-03-22T11:59:00Z",
                    "escalation": None,
                }
            },
        },
    )

    engine = make_engine(repo, state)
    mock_provider = engine.provider
    mock_provider.list_managed_prs.return_value = [
        {
            "number": 77,
            "url": "https://github.com/owner/test-repo/pull/77",
            "headRefName": "qa/review-77",
            "author": {"login": "sound"},
            "title": "Review me",
            "isDraft": False,
            "state": "OPEN",
        }
    ]
    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 77,
        "pr_url": "https://github.com/owner/test-repo/pull/77",
        "branch": "qa/review-77",
        "author": "sound",
        "fetched_at": "2026-03-22T12:05:00Z",
        "review_decision": "REVIEW_REQUIRED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": [],
        "actionable_comments": [],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "merge-ready-77",
    }

    published = {}

    def _fake_publish(pr_number, summary_text, publication_key, existing_review):
        published["pr_number"] = pr_number
        published["summary_text"] = summary_text
        published["publication_key"] = publication_key
        return existing_review["last_review_comment_url"]

    engine._publish_review_cycle_comment = _fake_publish

    result = engine.run(dry_run=False, allow_review_push=False)

    assert result.merge_ready_prs == 1
    assert published["publication_key"] == "merge-ready-77:merge_ready"

    active_prs = state.load_active_prs(repo.config.name)
    assert active_prs["prs"]["77"]["status"] == "merge_ready"
    assert active_prs["prs"]["77"]["merge_readiness"]["state"] == "ready_for_merge"

    review_state = state.load_review_state(repo.config.name)
    assert review_state["prs"]["77"]["last_action"] == "merge_ready"


def test_review_cycle_republishes_when_feedback_state_changes(tmp_path):
    repo = make_repo(tmp_path)
    repo.config.github["live_actions"] = True
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)

    state.save_review_state(
        repo.config.name,
        {
            "version": 1,
            "updated_at": "2026-03-22T11:59:00Z",
            "prs": {
                "77": {
                    "last_provider": "github",
                    "last_polled_at": "2026-03-22T11:59:00Z",
                    "last_snapshot_fingerprint": "merge-ready-77",
                    "last_snapshot": {
                        "review_decision": "REVIEW_REQUIRED",
                        "merge_state_status": "CLEAN",
                        "active_change_requesters": [],
                        "actionable_comment_count": 0,
                        "informational_comment_count": 0,
                    },
                    "attempts_used": 0,
                    "loop_count": 0,
                    "retry_eligible": False,
                    "last_action": "observed",
                    "last_action_at": "2026-03-22T11:59:00Z",
                    "last_action_reason": "No actionable review blockers and merge state is clean",
                    "planned_remediation": None,
                    "execution_result": None,
                    "last_review_comment_key": "merge-ready-77:merge_ready",
                    "last_review_comment_url": "https://github.com/owner/test-repo/pull/77#issuecomment-1",
                    "last_review_comment_at": "2026-03-22T11:59:00Z",
                    "escalation": None,
                }
            },
        },
    )

    engine = make_engine(repo, state)
    mock_provider = engine.provider
    mock_provider.list_managed_prs.return_value = [
        {
            "number": 77,
            "url": "https://github.com/owner/test-repo/pull/77",
            "headRefName": "qa/review-77",
            "author": {"login": "sound"},
            "title": "Review me",
            "isDraft": False,
            "state": "OPEN",
        }
    ]
    mock_provider.fetch_review_snapshot.return_value = {
        "pr_number": 77,
        "pr_url": "https://github.com/owner/test-repo/pull/77",
        "branch": "qa/review-77",
        "author": "sound",
        "fetched_at": "2026-03-22T12:05:00Z",
        "review_decision": "CHANGES_REQUESTED",
        "merge_state_status": "CLEAN",
        "active_change_requesters": ["reviewer1"],
        "actionable_comments": [
            {"author": "reviewer1", "body": "please add a regression test"}
        ],
        "informational_comments": [],
        "unresolved_threads": [],
        "score_optional": None,
        "checks_summary_optional": None,
        "fingerprint": "feedback-77",
    }

    engine._plan_remediation = lambda snapshot, existing_review, dry_run: None

    published = {}

    def _fake_publish(pr_number, summary_text, publication_key, existing_review):
        published["pr_number"] = pr_number
        published["summary_text"] = summary_text
        published["publication_key"] = publication_key
        return f"https://github.com/owner/test-repo/pull/{pr_number}#issuecomment-2"

    engine._publish_review_cycle_comment = _fake_publish

    result = engine.run(dry_run=False, allow_review_push=False)

    assert result.blocked_prs == 1
    assert result.retry_eligible_prs == 1
    assert published["publication_key"] == "feedback-77:review_feedback_detected"
    assert "please add a regression test" in published["summary_text"]
    assert "`review_feedback_detected`" in published["summary_text"]

    review_state = state.load_review_state(repo.config.name)
    review_record = review_state["prs"]["77"]
    assert (
        review_record["last_review_comment_key"]
        == "feedback-77:review_feedback_detected"
    )
    assert review_record["last_review_comment_url"].endswith("#issuecomment-2")


# ---------------------------------------------------------------------------
# pending_push_cycles — escalation guard for the retry_pending_push branch
# (M1 fix). PRs stuck in retry_pending_push with unchanged fingerprint must
# escalate to retry_exhausted after _MAX_PENDING_PUSH_CYCLES (5) cycles, and
# the counter must reset whenever the PR leaves retry_pending_push.
# ---------------------------------------------------------------------------


def _classify_engine(tmp_path: Path) -> ReviewCycleEngine:
    """Build a minimal engine for direct _classify_pr_status unit tests."""
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    return engine


def _feedback_snapshot(fingerprint: str = "stable-fp") -> dict:
    """Snapshot with actionable feedback — triggers retry_eligible path."""
    return {
        "pr_number": 42,
        "pr_url": "https://example.test/pr/42",
        "branch": "qa/fix-42",
        "author": "sound",
        "fetched_at": "2026-06-14T12:00:00Z",
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


def test_pending_push_cycles_increment_and_escalate(tmp_path):
    """
    M1 fix: PR stays in retry_pending_push for N-1 cycles (counter 1..4),
    then escalates to retry_exhausted on the 5th cycle.
    """
    from bluei.review.observation import _MAX_PENDING_PUSH_CYCLES
    from bluei.review.types import ReviewCycleResult

    assert _MAX_PENDING_PUSH_CYCLES == 5, "Test is calibrated to the default threshold"

    engine = _classify_engine(tmp_path)
    snapshot = _feedback_snapshot("stable-fp")

    # Simulate the record carried across cycles.
    existing_review = {
        "last_snapshot_fingerprint": "stable-fp",
        "last_action": "retry_pending_push",
        "attempts_used": 1,
        "loop_count": 0,
        "pending_push_cycles": 0,
        "planned_remediation": {"status": "retry_prepared"},
        "execution_result": {"status": "retry_pending_push", "attempts_used": 1},
    }

    escalations_seen = 0
    for cycle in range(1, _MAX_PENDING_PUSH_CYCLES + 1):
        result = ReviewCycleResult()
        classification = engine._classify_pr_status(
            snapshot=snapshot,
            existing_review=existing_review,
            dry_run=False,
            lock_handle=object(),
            result=result,
        )

        if cycle < _MAX_PENDING_PUSH_CYCLES:
            # Cycles 1..4 stay in retry_pending_push with monotonic counter.
            assert classification["status"] == "retry_pending_push", (
                f"Cycle {cycle}: expected retry_pending_push, "
                f"got {classification['status']}"
            )
            assert classification["pending_push_cycles"] == cycle, (
                f"Cycle {cycle}: expected counter {cycle}, "
                f"got {classification['pending_push_cycles']}"
            )
            assert result.retry_exhausted_prs == 0
        else:
            # Cycle 5 escalates to retry_exhausted and resets the counter.
            assert classification["status"] == "retry_exhausted", (
                f"Cycle {cycle}: expected retry_exhausted, "
                f"got {classification['status']}"
            )
            assert classification["pending_push_cycles"] == 0, (
                "Counter must reset to 0 after escalation so a future re-entry "
                "starts fresh"
            )
            assert result.retry_exhausted_prs == 1
            assert "pending-push timeout" in classification["merge_reason"].lower()
            escalations_seen += 1

        # Carry the persisted counter forward to simulate the next cycle.
        existing_review = {
            **existing_review,
            "pending_push_cycles": classification["pending_push_cycles"],
        }

    assert escalations_seen == 1, "Exactly the threshold cycle should escalate"


def test_pending_push_cycles_reset_on_fingerprint_change(tmp_path):
    """
    M1 fix: pending_push_cycles must reset to 0 when the fingerprint changes,
    because the PR has transitioned out of the (same-fingerprint) pending-push
    branch into the normal retry-eligibility flow.
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)

    # Pre-existing state: PR was stuck in retry_pending_push for 3 cycles.
    existing_review = {
        "last_snapshot_fingerprint": "old-fp",
        "last_action": "retry_pending_push",
        "attempts_used": 1,
        "loop_count": 0,
        "pending_push_cycles": 3,
        "planned_remediation": {"status": "retry_prepared"},
        "execution_result": {"status": "retry_pending_push", "attempts_used": 1},
    }

    # New snapshot has a DIFFERENT fingerprint — reviewer pushed or PR changed.
    snapshot = _feedback_snapshot("new-fp-after-operator-push")
    result = ReviewCycleResult()

    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=True,
        lock_handle=None,
        result=result,
    )

    # Not in the pending-push branch anymore — counter must reset.
    assert classification["status"] != "retry_pending_push", (
        "Fingerprint change should exit the pending-push branch"
    )
    assert classification["pending_push_cycles"] == 0, (
        "Counter must reset when fingerprint changes; got "
        f"{classification['pending_push_cycles']}"
    )


def test_pending_push_cycles_reset_on_status_change(tmp_path):
    """
    M1 fix: pending_push_cycles must reset to 0 when the PR transitions to
    a status other than retry_pending_push (e.g., retry_exhausted via
    max_attempts, loop_guard_paused, or merge_ready). Stale counters must
    not carry over.
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)

    # Pre-existing state: stuck in pending-push for 4 cycles, but attempts_used
    # has now reached max_attempts via a separate path. The classification
    # chain should NOT enter the pending-push branch (fingerprint matches)
    # because attempts_used >= max_attempts takes the exhausted branch...
    # Wait — the pending-push branch is FIRST in the chain, so a fingerprint
    # match always enters it. To test status-change reset, we need a case
    # where the pending-push branch does NOT fire: e.g., fingerprint matches
    # but previous_action is NOT retry_pending_push anymore (e.g., it transitioned
    # to retry_pushed via --allow-review-push, then back to feedback).
    existing_review = {
        "last_snapshot_fingerprint": "stable-fp",
        "last_action": "retry_pushed",  # Not retry_pending_push
        "attempts_used": 2,
        "loop_count": 0,
        "pending_push_cycles": 4,  # Stale from a prior pending-push phase
        "planned_remediation": None,
        "execution_result": None,
    }

    snapshot = _feedback_snapshot("stable-fp")
    result = ReviewCycleResult()

    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=True,
        lock_handle=None,
        result=result,
    )

    # Status is NOT retry_pending_push (previous_action was retry_pushed).
    assert classification["status"] != "retry_pending_push", (
        "previous_action != retry_pending_push should not re-enter that branch"
    )
    assert classification["pending_push_cycles"] == 0, (
        "Stale counter must reset when status is anything other than "
        f"retry_pending_push; got {classification['pending_push_cycles']}"
    )


def test_pending_push_cycles_zero_on_first_pending_push(tmp_path):
    """
    M1 fix: on the FIRST cycle entering retry_pending_push classification
    (previous_action already retry_pending_push from execution, counter was 0),
    the counter becomes 1 and status stays retry_pending_push.

    This guards the test_lifecycle_pending_push_preserved_across_runs invariant.
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)

    existing_review = {
        "last_snapshot_fingerprint": "stable-fp-123",
        "last_action": "retry_pending_push",
        "attempts_used": 1,
        "loop_count": 0,
        # No pending_push_cycles key — defaults to 0
        "planned_remediation": {"status": "retry_prepared"},
        "execution_result": {"status": "retry_pending_push", "attempts_used": 1},
    }
    snapshot = _feedback_snapshot("stable-fp-123")
    result = ReviewCycleResult()

    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=False,
        lock_handle=object(),
        result=result,
    )

    assert classification["status"] == "retry_pending_push"
    assert classification["pending_push_cycles"] == 1
    assert result.retry_exhausted_prs == 0


def test_pending_push_cycles_persisted_to_review_record(tmp_path):
    """
    M1 fix: pending_push_cycles flows from classification → review record so
    it survives across cycles. _record_pr_in_review_state must write the
    value into the persisted record.
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)
    snapshot = _feedback_snapshot("stable-fp-XYZ")

    existing_review = {
        "last_snapshot_fingerprint": "stable-fp-XYZ",
        "last_action": "retry_pending_push",
        "attempts_used": 1,
        "loop_count": 0,
        "pending_push_cycles": 2,
        "planned_remediation": {"status": "retry_prepared"},
        "execution_result": {"status": "retry_pending_push", "attempts_used": 1},
    }

    result = ReviewCycleResult()
    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=False,
        lock_handle=object(),
        result=result,
    )
    assert classification["pending_push_cycles"] == 3

    # Verify the record-builder writes the counter into the persisted record.
    record = engine._record_pr_in_review_state(
        snapshot=snapshot,
        existing_review=existing_review,
        status=classification["status"],
        merge_reason=classification["merge_reason"],
        remediation_plan=classification["remediation_plan"],
        execution_result=classification["execution_result"],
        loop_count=classification["loop_count"],
        paused=classification["paused"],
        stale_pause=classification["stale_pause"],
        retry_eligible=False,
        pending_push_cycles=classification["pending_push_cycles"],
    )

    assert record["pending_push_cycles"] == 3
    assert record["last_action"] == "retry_pending_push"


# ---------------------------------------------------------------------------
# H6: semantic-level loop guard. When a reviewer rephrases the same concern,
# the fingerprint changes (it hashes comment bodies) but the stable signal
# (counts of change-requesters and actionable comments) does not. Without
# this guard, loop_count resets to 0 on every rephrase and the bot can loop
# indefinitely. The fix preserves loop_count when counts are stable AND the
# prior cycle attempted remediation.
# ---------------------------------------------------------------------------


def test_h6_loop_count_preserved_on_reviewer_rephrase(tmp_path):
    """
    H6 fix: reviewer rephrases the same concern (different fingerprint, same
    counts) after a remediation was attempted → loop_count is PRESERVED, not
    reset to 0. Without this guard, the bot loops indefinitely.
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)

    # Prior cycle: remediation was attempted, loop_count was 2.
    existing_review = {
        "last_snapshot_fingerprint": "old-fp",
        "last_action": "retry_executed",
        "attempts_used": 1,
        "loop_count": 2,
        # Prior stable-signal counts (will match the new snapshot).
        "change_requester_count": 1,
        "actionable_comment_count": 1,
        "planned_remediation": None,
        "execution_result": None,
    }

    # New snapshot: DIFFERENT fingerprint (reviewer rephrased the comment)
    # but the SAME stable signal — 1 change-requester, 1 actionable comment.
    snapshot = _feedback_snapshot("new-fp-rephrased")
    result = ReviewCycleResult()

    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=True,
        lock_handle=None,
        result=result,
    )

    assert classification["loop_count"] == 2, (
        "Rephrase with stable counts and prior remediation must PRESERVE "
        f"loop_count; got {classification['loop_count']}"
    )
    # Counts flow out in the classification so they can be persisted.
    assert classification["change_requester_count"] == 1
    assert classification["actionable_comment_count"] == 1


def test_h6_loop_count_resets_on_genuine_new_review_content(tmp_path):
    """
    H6 fix: fingerprint changed AND the number of actionable comments also
    changed (reviewer added a second concern) → genuine new review content,
    loop_count resets to 0 (current behavior preserved).
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)

    existing_review = {
        "last_snapshot_fingerprint": "old-fp",
        "last_action": "retry_executed",
        "attempts_used": 1,
        "loop_count": 2,
        "change_requester_count": 1,
        "actionable_comment_count": 1,
        "planned_remediation": None,
        "execution_result": None,
    }

    # New snapshot: different fingerprint AND a different actionable-comment
    # count (2 instead of 1) — reviewer left genuinely new content.
    snapshot = _feedback_snapshot("new-fp-genuine")
    snapshot["actionable_comments"] = [
        {"author": "reviewer1", "body": "fix this"},
        {"author": "reviewer1", "body": "also fix that"},
    ]
    result = ReviewCycleResult()

    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=True,
        lock_handle=None,
        result=result,
    )

    assert classification["loop_count"] == 0, (
        "Genuine new review content (counts changed) must reset loop_count; "
        f"got {classification['loop_count']}"
    )
    assert classification["actionable_comment_count"] == 2


def test_h6_loop_count_resets_on_first_cycle_without_prior_counts(tmp_path):
    """
    H6 fix: backward compatibility. On the first cycle (or for an older
    review record that predates the H6 fields), prev counts are missing —
    the guard must fall through to reset (current behavior).
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)

    existing_review = {
        "last_snapshot_fingerprint": "old-fp",
        "last_action": "retry_executed",
        "attempts_used": 1,
        "loop_count": 2,
        # NOTE: no change_requester_count / actionable_comment_count keys —
        # simulates a record written before the H6 fix shipped.
        "planned_remediation": None,
        "execution_result": None,
    }

    snapshot = _feedback_snapshot("new-fp-first-cycle")
    result = ReviewCycleResult()

    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=True,
        lock_handle=None,
        result=result,
    )

    assert classification["loop_count"] == 0, (
        "Missing prior counts (first cycle / old record) must reset "
        f"loop_count for backward compatibility; got {classification['loop_count']}"
    )


def test_h6_loop_count_resets_when_no_prior_remediation(tmp_path):
    """
    H6 fix: even if counts are unchanged, if no remediation was attempted
    last cycle, the rephrase interpretation does not apply — reset to 0.
    This prevents false-positive loop detection on the second-ever cycle.
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)

    existing_review = {
        "last_snapshot_fingerprint": "old-fp",
        "last_action": "review_feedback_detected",  # not a remediation action
        "attempts_used": 0,
        "loop_count": 1,
        "change_requester_count": 1,
        "actionable_comment_count": 1,
        "planned_remediation": None,
        "execution_result": None,
    }

    snapshot = _feedback_snapshot("new-fp-no-remediation")
    result = ReviewCycleResult()

    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=True,
        lock_handle=None,
        result=result,
    )

    assert classification["loop_count"] == 0, (
        "Stable counts but no prior remediation must reset loop_count "
        f"(avoid false-positive loop detection); got {classification['loop_count']}"
    )


def test_h6_counts_persisted_to_review_record(tmp_path):
    """
    H6 fix: _record_pr_in_review_state must persist change_requester_count
    and actionable_comment_count as top-level fields so the next cycle can
    read them in _classify_pr_status.
    """
    engine = _classify_engine(tmp_path)
    snapshot = _feedback_snapshot("any-fp")

    existing_review = {
        "last_snapshot_fingerprint": "old-fp",
        "last_action": "retry_executed",
        "attempts_used": 1,
        "loop_count": 0,
    }

    record = engine._record_pr_in_review_state(
        snapshot=snapshot,
        existing_review=existing_review,
        status="review_feedback_detected",
        merge_reason="test",
        remediation_plan=None,
        execution_result=None,
        loop_count=0,
        paused=False,
        stale_pause=False,
        retry_eligible=True,
        pending_push_cycles=0,
    )

    assert record["change_requester_count"] == 1, (
        "change_requester_count must be persisted from snapshot"
    )
    assert record["actionable_comment_count"] == 1, (
        "actionable_comment_count must be persisted from snapshot"
    )


def test_h6_loop_count_increments_across_multiple_rephrase_cycles(tmp_path):
    """
    H6 integration: a PR that loops through remediation → reviewer rephrase
    (same counts) → remediation again must eventually trip the loop guard.
    Before H6 the counter kept resetting to 0 and the guard never fired.
    """
    from bluei.review.types import ReviewCycleResult

    engine = _classify_engine(tmp_path)
    # max_loops is 2 in make_repo — the guard fires at loop_count > 2.

    existing_review = {
        "last_snapshot_fingerprint": "fp-0",
        "last_action": "retry_executed",
        "attempts_used": 1,
        "loop_count": 3,
        "change_requester_count": 1,
        "actionable_comment_count": 1,
        "planned_remediation": None,
        "execution_result": None,
    }

    # Reviewer rephrases — new fingerprint, same counts.
    snapshot = _feedback_snapshot("fp-1-rephrased")
    result = ReviewCycleResult()

    classification = engine._classify_pr_status(
        snapshot=snapshot,
        existing_review=existing_review,
        dry_run=True,
        lock_handle=None,
        result=result,
    )

    # loop_count preserved at 3 → strictly greater than max_loops (2) → guard fires.
    assert classification["loop_count"] == 3
    assert classification["status"] == "loop_guard_paused", (
        "Preserved loop_count must trip the loop guard when it exceeds "
        f"max_loops; got status {classification['status']}"
    )
    assert classification["paused"] is True
    assert result.paused_prs == 1
