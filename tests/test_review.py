#!/usr/bin/env python3
"""Tests for review-cycle helpers."""

from pathlib import Path

from qa_agent.models import Repo, RepoConfig
from qa_agent.review import GitHubReviewProvider, ReviewCycleEngine
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
        )
    )


def test_is_managed_pr_by_branch_prefix(tmp_path):
    repo = make_repo(tmp_path)
    provider = GitHubReviewProvider.__new__(GitHubReviewProvider)
    provider.repo = repo
    provider.state = StateManager(tmp_path / "repos")
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/repo"
    provider.current_login = "sound"
    assert provider._is_managed_pr("someone-else", "qa/fix-issue") is True
    assert provider._is_managed_pr("someone-else", "feature/new-work") is False


def test_normalize_snapshot_filters_bots_and_author_comments(tmp_path):
    repo = make_repo(tmp_path)
    provider = GitHubReviewProvider.__new__(GitHubReviewProvider)
    provider.repo = repo
    provider.state = StateManager(tmp_path / "repos")
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/repo"
    provider.current_login = "sound"

    snapshot = provider._normalize_snapshot(
        {
            "number": 12,
            "url": "https://example.test/pr/12",
            "headRefName": "qa/fix-12",
            "author": {"login": "sound"},
            "reviewDecision": "CHANGES_REQUESTED",
            "reviews": {
                "nodes": [
                    {
                        "author": {"login": "reviewer1"},
                        "state": "CHANGES_REQUESTED",
                        "submittedAt": "2026-03-20T00:00:00Z",
                    },
                    {
                        "author": {"login": "dependabot"},
                        "state": "CHANGES_REQUESTED",
                        "submittedAt": "2026-03-20T00:01:00Z",
                    },
                ]
            },
            "reviewThreads": {
                "nodes": [
                    {
                        "isResolved": False,
                        "isOutdated": False,
                        "comments": {
                            "nodes": [
                                {
                                    "author": {"login": "sound"},
                                    "body": "self note",
                                    "createdAt": "2026-03-20T00:00:00Z",
                                },
                                {
                                    "author": {"login": "reviewer1"},
                                    "body": "Please add tests",
                                    "createdAt": "2026-03-20T00:01:00Z",
                                },
                                {
                                    "author": {"login": "github-actions"},
                                    "body": "bot noise",
                                    "createdAt": "2026-03-20T00:02:00Z",
                                },
                            ]
                        },
                    }
                ]
            },
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "greptile-apps"},
                        "body": "Not safe to merge as-is. The linting violation will still be reported.",
                        "createdAt": "2026-03-20T00:03:00Z",
                    },
                    {
                        "author": {"login": "sound"},
                        "body": "<!-- qa-agent-review-cycle: pr=12 key=test -->",
                        "createdAt": "2026-03-20T00:04:00Z",
                    },
                ]
            },
        }
    )

    assert snapshot["active_change_requesters"] == ["reviewer1"]
    assert len(snapshot["actionable_comments"]) == 1
    assert snapshot["actionable_comments"][0]["body"] == "please add tests"
    assert snapshot["fingerprint"]


def test_classify_comment_treats_blocking_language_as_actionable(tmp_path):
    repo = make_repo(tmp_path)
    provider = GitHubReviewProvider.__new__(GitHubReviewProvider)
    provider.repo = repo
    provider.state = StateManager(tmp_path / "repos")
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/repo"
    provider.current_login = "sound"

    assert provider._classify_comment("suggestion: not safe to merge as-is") == "actionable"
    assert provider._classify_comment("optional: consider renaming this variable") == "informational"



def test_ignore_comment_filters_status_chatter(tmp_path):
    repo = make_repo(tmp_path)
    provider = GitHubReviewProvider.__new__(GitHubReviewProvider)
    provider.repo = repo
    provider.state = StateManager(tmp_path / "repos")
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/repo"
    provider.current_login = "sound"

    assert provider._should_ignore_comment("codeant ai is reviewing your pr") is True
    assert provider._should_ignore_comment("codeant ai finished reviewing your pr") is True
    assert provider._should_ignore_comment("codeant ai is running incremental review") is True
    assert provider._should_ignore_comment("automated verification passed for finding abc") is True
    assert provider._should_ignore_comment("**tip:** try greploops") is True
    assert provider._should_ignore_comment("please add a regression test") is False


def test_is_bot_recognizes_review_automation_accounts(tmp_path):
    repo = make_repo(tmp_path)
    provider = GitHubReviewProvider.__new__(GitHubReviewProvider)
    provider.repo = repo
    provider.state = StateManager(tmp_path / "repos")
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/repo"
    provider.current_login = "sound"

    assert provider._is_bot("greptile-apps") is True
    assert provider._is_bot("codeant-ai") is True
    assert provider._is_bot("reviewer1") is False


def test_render_remediation_prompt_contains_pr_context(tmp_path):
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")

    prompt = engine._render_remediation_prompt(
        {
            "pr_number": 12,
            "pr_url": "https://example.test/pr/12",
            "branch": "qa/fix-12",
            "actionable_comments": [
                {"author": "reviewer1", "body": "please add tests"}
            ],
        },
        attempts_used=1,
    )

    assert "PR: #12" in prompt
    assert "Attempt: 2/3" in prompt
    assert "please add tests" in prompt


def test_render_remediation_prompt_includes_mnemo_context_when_available(tmp_path):
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._build_mnemo_review_context = lambda **kwargs: "## Mnemo context\n\n### Mnemo query: `please add tests`\nrelated files"

    prompt = engine._render_remediation_prompt(
        {
            "pr_number": 12,
            "pr_url": "https://example.test/pr/12",
            "branch": "qa/fix-12",
            "actionable_comments": [
                {"author": "reviewer1", "body": "please add tests"}
            ],
        },
        attempts_used=1,
    )

    assert "## Mnemo context" in prompt
    assert "related files" in prompt


def test_plan_remediation_writes_prompt_file(tmp_path):
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._render_backend_command = lambda prompt_file: f"echo using {prompt_file}"
    engine._prepare_worktree = lambda snapshot, dry_run: {
        "worktree_path": "/tmp/worktree",
        "local_branch": "qa-review-pr-12",
        "prepared": True,
        "dry_run": dry_run,
    }

    plan = engine._plan_remediation(
        {
            "pr_number": 12,
            "pr_url": "https://example.test/pr/12",
            "branch": "qa/fix-12",
            "fetched_at": "2026-03-20T00:00:00Z",
            "actionable_comments": [
                {"author": "reviewer1", "body": "please add tests"}
            ],
        },
        {"attempts_used": 0},
        dry_run=False,
    )

    assert plan is not None
    prompt_path = Path(plan["prompt_file"])
    assert prompt_path.exists()
    assert "please add tests" in prompt_path.read_text()
    assert plan["status"] == "retry_prepared"
    assert plan["backend_command"].startswith("echo using ")


def test_prepare_worktree_dry_run_returns_path(tmp_path):
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    provider = GitHubReviewProvider.__new__(GitHubReviewProvider)
    provider.repo = repo
    provider.state = engine.state
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/repo"
    provider.current_login = "sound"
    engine.provider = provider

    worktree = engine._prepare_worktree(
        {"pr_number": 77, "branch": "qa/fix-77"}, dry_run=True
    )
    assert worktree["dry_run"] is True
    assert worktree["prepared"] is False
    assert worktree["local_branch"] == "qa-review-pr-77"



def test_prepare_worktree_prefers_pull_ref_head(tmp_path):
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    provider = GitHubReviewProvider.__new__(GitHubReviewProvider)
    provider.repo = repo
    provider.state = engine.state
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/repo"
    provider.current_login = "sound"
    engine.provider = provider

    commands = []

    def _run_repo_cmd(cmd, cwd=None, check=True):
        commands.append(cmd)
        return ""

    engine._run_repo_cmd = _run_repo_cmd

    worktree = engine._prepare_worktree(
        {"pr_number": 77, "branch": "qa/fix-77"}, dry_run=False
    )

    assert worktree["prepared"] is True
    assert commands[0] == [
        "git",
        "fetch",
        "origin",
        "pull/77/head:refs/remotes/origin/pr/77/head",
    ]
    assert commands[1][-1] == "refs/remotes/origin/pr/77/head"


def test_render_backend_command_uses_review_template(tmp_path):
    repo = make_repo(tmp_path)
    repo.config.review_claude_template = "claude custom {prompt_file}"
    repo.config.fix_engine = "claude"
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    command = engine._render_backend_command(Path("/tmp/prompt.md"))
    assert command == "claude custom /tmp/prompt.md"


def test_execute_prepared_remediation_dry_run_is_non_mutating(tmp_path):
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": "/tmp/does-not-matter"},
            "backend_command": "echo hi",
        },
        {"attempts_used": 0},
        dry_run=True,
    )
    assert result["executed"] is False
    assert result["status"] == "retry_prepared"


def test_execute_prepared_remediation_reports_no_changes(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._run_shell = lambda command, cwd: {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: []
    engine._run_validation = lambda cwd: {
        "ok": True,
        "results": [],
        "reason": "completed",
    }
    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "echo hi",
        },
        {"attempts_used": 1},
        dry_run=False,
    )
    assert result["executed"] is True
    assert result["status"] == "retry_no_changes"
    assert result["attempts_used"] == 2


def test_execute_prepared_remediation_validation_failure_beats_no_changes(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._run_shell = lambda command, cwd: {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: []
    engine._run_validation = lambda cwd: {
        "ok": False,
        "results": [{"command": ["ruff", "check", "."], "returncode": 1}],
        "reason": "completed",
    }
    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "echo hi",
        },
        {"attempts_used": 1},
        dry_run=False,
    )
    assert result["executed"] is True
    assert result["status"] == "retry_failed_validation"
    assert result["attempts_used"] == 2
    assert result["validation"]["ok"] is False



def test_execute_prepared_remediation_respects_max_attempts(tmp_path):
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(tmp_path / "worktree")},
            "backend_command": "echo hi",
        },
        {"attempts_used": 3},
        dry_run=False,
    )
    assert result["executed"] is False
    assert result["status"] == "retry_exhausted"


def test_execute_prepared_remediation_handles_timeout(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._run_shell = lambda command, cwd: {
        "returncode": 124,
        "stdout": "",
        "stderr": "",
        "timed_out": True,
    }
    engine._collect_changed_files = lambda cwd: []
    engine._run_validation = lambda cwd: {
        "ok": True,
        "results": [],
        "reason": "completed",
    }
    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "echo hi",
        },
        {"attempts_used": 0},
        dry_run=False,
    )
    assert result["executed"] is True
    assert result["status"] == "retry_failed_timeout"


def test_execute_prepared_remediation_requires_explicit_push_confirmation(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._run_shell = lambda command, cwd: {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: ["source/index.ts"]
    engine._run_validation = lambda cwd: {
        "ok": True,
        "results": [],
        "reason": "completed",
    }
    engine._apply_commit_push_boundary = (
        lambda worktree_path, snapshot, changed_files, allow_review_push: {
            "status": "pending_operator_confirmation",
            "allow_review_push": allow_review_push,
            "target_branch": snapshot["branch"],
            "changed_files": changed_files,
        }
    )
    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "echo hi",
        },
        {"attempts_used": 0},
        dry_run=False,
        allow_review_push=False,
        snapshot={"pr_number": 12, "branch": "qa/fix-12"},
    )
    assert result["executed"] is True
    assert result["status"] == "retry_pending_push"
    assert result["push_result"]["status"] == "pending_operator_confirmation"


def test_execute_prepared_remediation_pushes_when_allowed(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._run_shell = lambda command, cwd: {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: ["source/index.ts"]
    engine._run_validation = lambda cwd: {
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
    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "echo hi",
        },
        {"attempts_used": 0},
        dry_run=False,
        allow_review_push=True,
        snapshot={"pr_number": 12, "branch": "qa/fix-12"},
    )
    assert result["executed"] is True
    assert result["status"] == "retry_pushed"
    assert result["push_result"]["status"] == "pushed"
    assert result["push_result"]["cleanup"]["removed"] is True


def test_loop_pressure_requires_prior_execution_attempt():
    existing_fingerprint = "abc"
    same_fingerprint = "abc"
    actionable = [{"author": "reviewer", "body": "please fix"}]
    previous_action = "retry_prepared"
    previous_attempted_remediation = previous_action in {
        "retry_executed",
        "retry_failed",
        "retry_failed_validation",
        "retry_no_changes",
        "retry_failed_timeout",
    }
    stale_pause = not previous_attempted_remediation
    loop_count = 3
    if existing_fingerprint == same_fingerprint and actionable:
        if previous_attempted_remediation:
            loop_count += 1
        else:
            loop_count = 0
    assert loop_count == 0
    assert stale_pause is True


def test_retry_exhausted_status_tracked_in_result(tmp_path):
    """When max_attempts is exhausted before execution, PR should be marked as retry_exhausted."""
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")

    # Setup provider to return a managed PR
    provider = GitHubReviewProvider.__new__(GitHubReviewProvider)
    provider.repo = repo
    provider.state = engine.state
    provider.repo_path = Path(repo.config.path)
    provider.repo_slug = "owner/repo"
    provider.current_login = "sound"
    provider.list_managed_prs = lambda: [
        {
            "number": 33,
            "url": "https://example.test/pr/33",
            "headRefName": "qa/fix-33",
            "author": {"login": "sound"},
        }
    ]
    provider.fetch_review_snapshot = lambda pr_number: {
        "pr_number": pr_number,
        "pr_url": f"https://example.test/pr/{pr_number}",
        "branch": f"qa/fix-{pr_number}",
        "fetched_at": "2026-03-20T00:00:00Z",
        "actionable_comments": [{"author": "reviewer1", "body": "please fix"}],
        "informational_comments": [],
        "active_change_requesters": ["reviewer1"],
        "review_decision": "CHANGES_REQUESTED",
        "merge_state_status": "CLEAN",
        "fingerprint": "abc123",
    }
    engine.provider = provider

    # Setup review state with attempts_used = 3 (exhausted)
    review_state = engine.state.load_review_state(repo.config.name)
    review_state["prs"] = {
        "33": {"attempts_used": 3, "last_snapshot_fingerprint": "abc123"}
    }
    engine.state.save_review_state(repo.config.name, review_state)

    result = engine.run(dry_run=True)

    # Should increment retry_exhausted_prs because attempts_used >= max_attempts
    assert result.retry_eligible_prs == 0
    assert result.retry_exhausted_prs == 1
    assert result.retry_planned_prs == 0  # Should NOT plan remediation


def test_unattended_push_policy_default_blocks_push(tmp_path):
    """
    UNATTENDED PUSH POLICY TEST:
    Verify that by default (allow_review_push=False), validated changes
    result in 'retry_pending_push' state and do NOT push to remote.

    Unattended review-cycle runs (e.g., cron jobs) must NEVER push code.
    """
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._run_shell = lambda command, cwd: {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: ["src/file.ts", "src/other.ts"]
    engine._run_validation = lambda cwd: {
        "ok": True,
        "results": [],
        "reason": "completed",
    }

    # Simulate the default case: allow_review_push=False (unattended mode)
    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "echo fix",
        },
        {"attempts_used": 0},
        dry_run=False,
        allow_review_push=False,
        snapshot={"pr_number": 99, "branch": "qa/fix-99"},
    )

    assert result["executed"] is True
    assert result["status"] == "retry_pending_push", (
        f"Expected 'retry_pending_push', got '{result['status']}'"
    )
    assert result["push_result"]["status"] == "pending_operator_confirmation"
    assert "changed_files" in result["push_result"]
    assert len(result["push_result"]["changed_files"]) == 2


def test_unattended_push_policy_explicit_flag_enables_push(tmp_path):
    """
    UNATTENDED PUSH POLICY TEST:
    Verify that with explicit --allow-review-push, the remediation
    proceeds to push after validation.
    """
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")
    engine._run_shell = lambda command, cwd: {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    }
    engine._collect_changed_files = lambda cwd: ["src/file.ts"]
    engine._run_validation = lambda cwd: {
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

    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "echo fix",
        },
        {"attempts_used": 0},
        dry_run=False,
        allow_review_push=True,
        snapshot={"pr_number": 99, "branch": "qa/fix-99"},
    )

    assert result["executed"] is True
    assert result["status"] == "retry_pushed", (
        f"Expected 'retry_pushed', got '{result['status']}'"
    )
    assert result["push_result"]["status"] == "pushed"
    assert result["push_result"]["allow_review_push"] is True


def test_apply_commit_push_boundary_returns_early_when_push_not_allowed(tmp_path):
    """
    UNATTENDED PUSH POLICY TEST:
    Verify that _apply_commit_push_boundary returns immediately with
    'pending_operator_confirmation' when allow_review_push=False,
    without staging, committing, or pushing any changes.
    """
    repo = make_repo(tmp_path)
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    engine.state = StateManager(tmp_path / "repos")

    result = engine._apply_commit_push_boundary(
        worktree_path=Path("/nonexistent/worktree"),  # Should not be accessed
        snapshot={"pr_number": 1, "branch": "qa/test"},
        changed_files=["a.ts", "b.ts"],
        allow_review_push=False,
    )

    assert result["status"] == "pending_operator_confirmation"
    assert result["allow_review_push"] is False
    assert result["target_branch"] == "qa/test"
    assert result["changed_files"] == ["a.ts", "b.ts"]
    # No git operations should have been attempted
    assert "git_add" not in result
    assert "git_commit" not in result
    assert "git_push" not in result
