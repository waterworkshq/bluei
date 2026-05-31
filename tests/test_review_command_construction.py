#!/usr/bin/env python3
"""
Integration tests verifying actual command construction in the review lifecycle.

These tests mock at the subprocess.run level (NOT the provider level) so that the
real command-building code path executes. This catches flag drift, missing args,
and ensures the two independent gh-interaction layers (provider vs engine) stay
consistent.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from bluei.app.models import Repo, RepoConfig
from bluei.app.state import StateManager
from bluei.review.cycle import GitHubReviewProvider, ReviewCycleEngine, GRAPHQL_QUERY


def _make_repo(tmp_path: Path, **overrides) -> Repo:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    defaults = dict(
        id="repo-test",
        name="owner/test-repo",
        path=str(repo_path),
        language="typescript",
        github={"owner": "owner", "repo": "test-repo"},
        review_care={
            "enabled": True,
            "max_attempts": 3,
            "max_loops": 2,
            "max_prs_per_run": 1,
            "cleanup_worktrees_after_push": False,
        },
    )
    defaults.update(overrides)
    return Repo(config=RepoConfig(**defaults))


def _make_engine_with_mock_provider(
    tmp_path: Path, **repo_overrides
) -> ReviewCycleEngine:
    repo = _make_repo(tmp_path, **repo_overrides)
    state = StateManager(tmp_path / "repos")
    state._get_state_dir(repo.config.name).mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# 1. GitHubReviewProvider.list_managed_prs()
# ---------------------------------------------------------------------------


def test_list_managed_prs_gh_command_flags(tmp_path):
    """Verify the gh pr list command has correct repo, state, limit, json flags."""
    repo = _make_repo(tmp_path)
    state = StateManager(tmp_path / "repos")

    pr_list_json = json.dumps(
        [
            {
                "number": 1,
                "url": "https://x/pull/1",
                "title": "T",
                "headRefName": "qa/fix-1",
                "author": {"login": "bot"},
                "isDraft": False,
                "state": "OPEN",
            },
        ]
    )
    api_user_json = json.dumps({"login": "bot"})

    call_count = 0
    captured_cmds = []

    def _fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        captured_cmds.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        if "remote" in cmd:
            result.stdout = "https://github.com/owner/test-repo.git"
        elif "api" in cmd and "user" in cmd:
            result.stdout = api_user_json
        elif "pr" in cmd and "list" in cmd:
            result.stdout = pr_list_json
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    with patch.object(subprocess, "run", side_effect=_fake_run):
        provider = GitHubReviewProvider(repo, state)
        provider.list_managed_prs()

    gh_cmd = captured_cmds[2]  # 0=remote, 1=api user, 2=pr list
    assert gh_cmd[0:3] == ["gh", "pr", "list"]
    assert "--repo" in gh_cmd
    repo_idx = gh_cmd.index("--repo")
    assert gh_cmd[repo_idx + 1] == "owner/test-repo"
    assert "--state" in gh_cmd
    state_idx = gh_cmd.index("--state")
    assert gh_cmd[state_idx + 1] == "open"
    assert "--limit" in gh_cmd
    limit_idx = gh_cmd.index("--limit")
    assert gh_cmd[limit_idx + 1] == "50"
    assert "--json" in gh_cmd
    json_idx = gh_cmd.index("--json")
    json_fields = gh_cmd[json_idx + 1].split(",")
    for field in (
        "number",
        "url",
        "title",
        "headRefName",
        "author",
        "isDraft",
        "state",
    ):
        assert field in json_fields, f"Missing json field: {field}"


# ---------------------------------------------------------------------------
# 2. GitHubReviewProvider.fetch_review_snapshot()
# ---------------------------------------------------------------------------


def test_fetch_review_snapshot_graphql_command(tmp_path):
    """Verify the gh api graphql command has correct query and variables."""
    repo = _make_repo(tmp_path)
    state = StateManager(tmp_path / "repos")

    graphql_response = json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "url": "https://x/pull/42",
                        "title": "T",
                        "isDraft": False,
                        "state": "OPEN",
                        "reviewDecision": "APPROVED",
                        "createdAt": "",
                        "updatedAt": "",
                        "author": {"login": "dev"},
                        "headRefName": "main",
                        "headRepositoryOwner": {"login": "owner"},
                        "mergeStateStatus": "CLEAN",
                        "reviews": {"nodes": []},
                        "reviewThreads": {"nodes": []},
                        "comments": {"nodes": []},
                    }
                }
            }
        }
    )
    api_user_json = json.dumps({"login": "bot"})

    captured_cmds = []

    def _fake_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        if "remote" in cmd:
            result.stdout = "https://github.com/owner/test-repo.git"
        elif "api" in cmd and "user" in cmd:
            result.stdout = api_user_json
        elif "graphql" in cmd:
            result.stdout = graphql_response
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    with patch.object(subprocess, "run", side_effect=_fake_run):
        provider = GitHubReviewProvider(repo, state)
        provider.fetch_review_snapshot(42)

    gql_cmd = captured_cmds[-1]
    assert gql_cmd[0:3] == ["gh", "api", "graphql"]

    def _get_flag_value(cmd, flag):
        idx = cmd.index(flag)
        return cmd[idx + 1]

    assert _get_flag_value(gql_cmd, "-f").startswith("owner=")
    assert _get_flag_value(gql_cmd, "-f").split("=", 1)[1] == "owner"

    has_owner = any(v == "owner=owner" for v in gql_cmd)
    has_name = any(v == "name=test-repo" for v in gql_cmd)
    has_number = any(v == "number=42" for v in gql_cmd)
    assert has_owner, "Missing owner=owner variable"
    assert has_name, "Missing name=test-repo variable"
    assert has_number, "Missing number=42 variable"

    query_vals = [v for v in gql_cmd if v.startswith("query=")]
    assert len(query_vals) == 1
    assert query_vals[0] == f"query={GRAPHQL_QUERY}"


# ---------------------------------------------------------------------------
# 3. ReviewCycleEngine._publish_review_cycle_comment()
# ---------------------------------------------------------------------------


def test_publish_review_cycle_comment_gh_pr_comment_structure(tmp_path):
    """Verify gh pr comment command has correct pr number, repo, and body."""
    engine = _make_engine_with_mock_provider(
        tmp_path, github={"live_actions": True, "owner": "owner", "repo": "test-repo"}
    )
    engine.repo.config.github = {"live_actions": True}

    captured_cmds = []

    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/test-repo/pull/42#issuecomment-1",
            stderr="",
        )
        engine._publish_review_cycle_comment(
            pr_number=42,
            summary_text="## bluei Review\nbody text",
            publication_key="fp123:merge_ready",
            existing_review={},
        )
        captured_cmds = [list(c[0][0]) for c in mock_run.call_args_list]

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert cmd[0:3] == ["gh", "pr", "comment"]
    assert str(42) in cmd
    assert "--repo" in cmd
    repo_idx = cmd.index("--repo")
    assert cmd[repo_idx + 1] == "owner/test-repo"
    assert "--body" in cmd
    body_idx = cmd.index("--body")
    assert cmd[body_idx + 1] == "## bluei Review\nbody text"


def test_publish_review_cycle_comment_skips_when_live_actions_disabled(tmp_path):
    """Verify no subprocess call when live_actions is False."""
    engine = _make_engine_with_mock_provider(tmp_path)

    with patch.object(subprocess, "run") as mock_run:
        result = engine._publish_review_cycle_comment(
            pr_number=42,
            summary_text="text",
            publication_key="key",
            existing_review={},
        )

    assert result is None
    mock_run.assert_not_called()


def test_publish_review_cycle_comment_skips_unchanged(tmp_path):
    """Verify comment is skipped when publication_key matches existing."""
    engine = _make_engine_with_mock_provider(
        tmp_path, github={"live_actions": True, "owner": "owner", "repo": "test-repo"}
    )
    engine.repo.config.github = {"live_actions": True}

    with patch.object(subprocess, "run") as mock_run:
        result = engine._publish_review_cycle_comment(
            pr_number=42,
            summary_text="text",
            publication_key="fp:status",
            existing_review={
                "last_review_comment_key": "fp:status",
                "last_review_comment_url": "https://existing",
            },
        )

    assert result == "https://existing"
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 4. ReviewCycleEngine._find_open_prs()
# ---------------------------------------------------------------------------


def test_find_open_prs_gh_pr_list_command(tmp_path):
    """Verify gh pr list command for _find_open_prs has correct flags."""
    engine = _make_engine_with_mock_provider(tmp_path)

    pr_json = json.dumps(
        [
            {"number": 7, "title": "Fix", "updatedAt": "2026-01-01T00:00:00Z"},
        ]
    )

    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=pr_json, stderr="")
        engine._find_open_prs()

    cmd = mock_run.call_args[0][0]
    assert cmd[0:3] == ["gh", "pr", "list"]
    assert "--json" in cmd
    json_idx = cmd.index("--json")
    json_fields = cmd[json_idx + 1].split(",")
    assert set(json_fields) == {"number", "title", "updatedAt"}
    assert "--repo" in cmd
    repo_idx = cmd.index("--repo")
    assert cmd[repo_idx + 1] == "owner/test-repo"
    assert "--state" in cmd
    state_idx = cmd.index("--state")
    assert cmd[state_idx + 1] == "open"
    assert "--limit" in cmd
    limit_idx = cmd.index("--limit")
    assert cmd[limit_idx + 1] == "10"


# ---------------------------------------------------------------------------
# 5. ReviewCycleEngine._prepare_worktree()
# ---------------------------------------------------------------------------


def test_prepare_worktree_git_fetch_and_worktree_add(tmp_path):
    """Verify git fetch + worktree add command sequence."""
    engine = _make_engine_with_mock_provider(tmp_path)
    snapshot = {"pr_number": 42, "branch": "qa/fix-42"}
    worktree_expected = engine._review_worktree_path(42)

    captured_cmds = []

    def _fake_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with patch.object(subprocess, "run", side_effect=_fake_run):
        result = engine._prepare_worktree(snapshot, dry_run=False)

    assert result["prepared"] is True

    fetch_cmds = [c for c in captured_cmds if c[0] == "git" and "fetch" in c]
    assert len(fetch_cmds) >= 1
    fetch_cmd = fetch_cmds[0]
    assert fetch_cmd[0:3] == ["git", "fetch", "origin"]
    assert any("pull/42/head" in arg for arg in fetch_cmd)

    worktree_cmds = [c for c in captured_cmds if c[0] == "git" and "worktree" in c]
    assert len(worktree_cmds) >= 1
    wt_cmd = worktree_cmds[0]
    assert wt_cmd[0:4] == ["git", "worktree", "add", "-B"]
    assert wt_cmd[4] == "qa-review-pr-42"
    assert str(worktree_expected) in wt_cmd


def test_prepare_worktree_dry_run_no_subprocess(tmp_path):
    """Verify dry_run skips all subprocess calls."""
    engine = _make_engine_with_mock_provider(tmp_path)
    snapshot = {"pr_number": 42, "branch": "qa/fix-42"}

    with patch.object(subprocess, "run") as mock_run:
        result = engine._prepare_worktree(snapshot, dry_run=True)

    assert result["dry_run"] is True
    assert result["prepared"] is False
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 6. ReviewCycleEngine._apply_commit_push_boundary()
# ---------------------------------------------------------------------------


def test_apply_commit_push_git_add_commit_push_sequence(tmp_path):
    """Verify git add + commit + push command sequence."""
    engine = _make_engine_with_mock_provider(tmp_path)
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    snapshot = {"pr_number": 42, "branch": "qa/fix-42"}
    changed_files = ["src/a.ts", "tests/a.test.ts"]

    captured_cmds = []

    def _fake_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with patch.object(subprocess, "run", side_effect=_fake_run):
        result = engine._apply_commit_push_boundary(
            worktree_path=worktree_path,
            snapshot=snapshot,
            changed_files=changed_files,
            allow_review_push=True,
        )

    assert result["status"] == "pushed"

    add_cmds = [c for c in captured_cmds if c[0] == "git" and "add" in c]
    assert len(add_cmds) == 1
    assert add_cmds[0] == ["git", "add", "--"] + changed_files

    commit_cmds = [c for c in captured_cmds if c[0] == "git" and "commit" in c]
    assert len(commit_cmds) == 1
    assert commit_cmds[0] == [
        "git",
        "commit",
        "-m",
        "bluei: address review feedback for PR #42",
    ]

    push_cmds = [c for c in captured_cmds if c[0] == "git" and "push" in c]
    assert len(push_cmds) == 1
    assert push_cmds[0] == ["git", "push", "origin", "HEAD:qa/fix-42"]


def test_apply_commit_push_blocks_without_flag(tmp_path):
    """Verify push is blocked when allow_review_push=False."""
    engine = _make_engine_with_mock_provider(tmp_path)
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    snapshot = {"pr_number": 42, "branch": "qa/fix-42"}

    with patch.object(subprocess, "run") as mock_run:
        result = engine._apply_commit_push_boundary(
            worktree_path=worktree_path,
            snapshot=snapshot,
            changed_files=["src/a.ts"],
            allow_review_push=False,
        )

    assert result["status"] == "pending_operator_confirmation"
    mock_run.assert_not_called()


def test_apply_commit_push_no_changes(tmp_path):
    """Verify no git commands when changed_files is empty."""
    engine = _make_engine_with_mock_provider(tmp_path)
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    snapshot = {"pr_number": 42, "branch": "qa/fix-42"}

    with patch.object(subprocess, "run") as mock_run:
        result = engine._apply_commit_push_boundary(
            worktree_path=worktree_path,
            snapshot=snapshot,
            changed_files=[],
            allow_review_push=True,
        )

    assert result["status"] == "no_changes"
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Cross-layer consistency: provider vs engine use same repo slug
# ---------------------------------------------------------------------------


def test_provider_and_engine_use_consistent_repo_slug(tmp_path):
    """Verify both layers construct the same --repo value for the same repo."""
    pr_list_json = json.dumps([])
    api_user_json = json.dumps({"login": "bot"})

    provider_cmds = []

    def _provider_run(cmd, **kwargs):
        provider_cmds.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        if "remote" in cmd:
            result.stdout = "https://github.com/acme/project-x.git"
        elif "api" in cmd and "user" in cmd:
            result.stdout = api_user_json
        else:
            result.stdout = pr_list_json
        result.stderr = ""
        return result

    repo = _make_repo(tmp_path)
    state = StateManager(tmp_path / "repos")

    with patch.object(subprocess, "run", side_effect=_provider_run):
        provider = GitHubReviewProvider(repo, state)
        provider.list_managed_prs()

    provider_repo_vals = set()
    for cmd in provider_cmds:
        if "--repo" in cmd:
            idx = cmd.index("--repo")
            provider_repo_vals.add(cmd[idx + 1])

    engine = _make_engine_with_mock_provider(
        tmp_path,
        name="acme/project-x",
        github={"owner": "acme", "repo": "project-x", "live_actions": False},
    )

    engine_cmds = []
    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        engine._find_open_prs()
        for c in mock_run.call_args_list:
            engine_cmds.append(list(c[0][0]))

    engine_repo_vals = set()
    for cmd in engine_cmds:
        if "--repo" in cmd:
            idx = cmd.index("--repo")
            engine_repo_vals.add(cmd[idx + 1])

    assert provider_repo_vals == engine_repo_vals, (
        f"Provider slugs {provider_repo_vals} != Engine slugs {engine_repo_vals}"
    )
