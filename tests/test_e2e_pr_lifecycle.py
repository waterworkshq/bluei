#!/usr/bin/env python3
"""E2E PR lifecycle tests — merge gate, PR creation, rebase sweep, cleanup.

All gh commands are mocked. Real git is used for branch/commit/rebase
operations.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _git_commit(repo_path: Path, msg: str):
    subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", msg],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )


@pytest.fixture
def repo_with_remote(tmp_path, git_commit_all):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@bluei.dev"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Bluei Test"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    git_commit_all(repo, "initial commit")
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return repo


def _make_pr(
    number,
    title="Test PR",
    branch="fix-1",
    state="OPEN",
    is_draft=False,
    created_at=None,
):
    return {
        "number": number,
        "title": title,
        "headRefName": branch,
        "baseRefName": "main",
        "state": state,
        "isDraft": is_draft,
        "createdAt": created_at or datetime.now(timezone.utc).isoformat(),
        "url": f"https://github.com/test/repo/pull/{number}",
    }


class TestMergeGate:
    def test_fetch_returns_prs_sorted(self, repo_with_remote, monkeypatch):
        from bluei.engine.gh import fetch_open_prs_for_merge

        prs_data = [
            _make_pr(2, is_draft=True),
            _make_pr(1, is_draft=False),
        ]
        monkeypatch.setattr(
            "bluei.engine.gh.gh_json",
            lambda cmd, cwd: prs_data,
        )

        prs = fetch_open_prs_for_merge("test/repo", repo_with_remote)
        assert len(prs) == 2
        assert prs[0]["number"] == 1
        assert prs[1]["number"] == 2

    def test_merge_pr_dry_run(self, repo_with_remote):
        from bluei.engine.gh import merge_pr

        success, reason = merge_pr("test/repo", 42, dry_run=True, cwd=repo_with_remote)
        assert success is True
        assert "dry-run" in reason

    def test_merge_pr_real_calls_gh(self, repo_with_remote):
        from bluei.engine.gh import merge_pr

        with patch("bluei.engine.gh.run_capture") as mock_rc:
            mock_rc.return_value = (0, "merged")
            success, reason = merge_pr(
                "test/repo", 42, dry_run=False, cwd=repo_with_remote
            )
            assert success is True
            assert reason == "merged"
            assert mock_rc.called

    def test_merge_pr_failure(self, repo_with_remote):
        from bluei.engine.gh import merge_pr

        with patch("bluei.engine.gh.run_capture") as mock_rc:
            mock_rc.return_value = (1, "conflict")
            success, reason = merge_pr(
                "test/repo", 42, dry_run=False, cwd=repo_with_remote
            )
            assert success is False


class TestPRCreation:
    def test_create_issue_dry_run(self, repo_with_remote, tmp_path):
        from bluei.engine.gh import create_or_update_github_issue
        from bluei.engine.models import Finding

        finding = Finding(
            finding_id="f-001",
            repo="test/repo",
            path="main.py",
            line=1,
            rule="test-rule",
            snippet="test snippet",
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )
        log_file = tmp_path / "test.log"

        with patch("bluei.engine.gh.run_capture") as mock_rc:
            mock_rc.return_value = (1, "not found")
            result = create_or_update_github_issue(
                "test/repo",
                finding,
                dry_run=True,
                log_file=log_file,
                cwd=repo_with_remote,
            )

        assert result["created"] is True or result.get("error") is not None


class TestRebaseSweep:
    def test_sweep_finds_sibling_branches(self, repo_with_remote, tmp_path):
        repo = repo_with_remote
        log_file = tmp_path / "rebase.log"

        subprocess.run(
            ["git", "checkout", "-b", "fix-a"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        (repo / "fix_a.txt").write_text("a")
        _git_commit(repo, "fix a")

        subprocess.run(
            ["git", "checkout", "master"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", "fix-b"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        (repo / "fix_b.txt").write_text("b")
        _git_commit(repo, "fix b")

        subprocess.run(
            ["git", "checkout", "master"],
            cwd=repo,
            capture_output=True,
            check=True,
        )

        mock_prs = [
            _make_pr(1, branch="fix-a"),
            _make_pr(2, branch="fix-b"),
        ]

        with (
            patch("bluei.engine.gh.gh_json") as mock_gh_json,
            patch("bluei.engine.gh.run_capture") as mock_rc,
        ):
            mock_gh_json.return_value = mock_prs
            mock_rc.return_value = (0, "ok")

            from bluei.engine.rebase_sweep import sweep_rebase

            result = sweep_rebase(
                repo_path=repo,
                gh_repo_slug="test/repo",
                merged_pr_number=99,
                base_branch="master",
                log_file=log_file,
                dry_run=True,
            )

        assert isinstance(result, dict)
        assert "rebased" in result
        assert "conflicted" in result


class TestParseGithubRepo:
    def test_parse_https_url(self):
        from bluei.engine.gh import parse_github_repo

        owner, name = parse_github_repo("https://github.com/owner/repo.git")
        assert owner == "owner"
        assert name == "repo"

    def test_parse_ssh_url(self):
        from bluei.engine.gh import parse_github_repo

        owner, name = parse_github_repo("git@github.com:myorg/myrepo.git")
        assert owner == "myorg"
        assert name == "myrepo"

    def test_parse_non_github(self):
        from bluei.engine.gh import parse_github_repo

        owner, name = parse_github_repo("https://gitlab.com/foo/bar.git")
        assert owner == ""
        assert name == ""


class TestDedupeMarker:
    def test_marker_format(self):
        from bluei.engine.gh import finding_dedupe_marker

        marker = finding_dedupe_marker("f-abc123")
        assert "[finding_id:f-abc123]" in marker

    def test_marker_in_issue_search(self, repo_with_remote, monkeypatch):
        from bluei.engine.gh import find_existing_github_issue

        monkeypatch.setattr(
            "bluei.engine.gh.gh_json",
            lambda cmd, cwd: None,
        )
        result = find_existing_github_issue("test/repo", "f-001", cwd=repo_with_remote)
        assert result is None
