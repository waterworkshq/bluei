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

from qa_agent.models import Repo, RepoConfig
from qa_agent.review import GitHubReviewProvider, ReviewCycleEngine, ReviewCycleResult
from qa_agent.state import StateManager


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
    assert "QA Agent Review, PR #77" in published["summary_text"]
    assert "`pending_review`" in published["summary_text"]

    review_state = state.load_review_state(repo.config.name)
    review_record = review_state["prs"]["77"]
    assert review_record["last_review_comment_key"] == "merge-ready-77:pending_review"
    assert review_record["last_review_comment_url"].endswith("#issuecomment-1")

    active_prs = state.load_active_prs(repo.config.name)
    assert active_prs["prs"]["77"]["status"] == "pending_review"
    assert active_prs["prs"]["77"]["review_comment"]["url"].endswith(
        "#issuecomment-1"
    )



def test_review_cycle_marks_unstable_snapshot_merge_ready_when_artifact_exists(tmp_path):
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
    assert "pending fresh merge triage" in active_prs["prs"]["77"]["merge_readiness"]["reason"]

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
    assert review_record["last_review_comment_key"] == "feedback-77:review_feedback_detected"
    assert review_record["last_review_comment_url"].endswith("#issuecomment-2")
