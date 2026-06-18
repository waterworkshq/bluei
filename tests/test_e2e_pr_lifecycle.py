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

        with (
            patch("bluei.engine.gh.time.sleep"),
            patch("bluei.engine.gh.run_capture") as mock_rc,
        ):
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


# ===========================================================================
# Lifecycle coverage gap — stale cleanup, review observation state, batch dedup
#
# Added to close the gaps identified in docs/plans/e2e/E2E_PR_LIFECYCLE.md.
# Strategy mirrors the rest of this file: mock the gh subprocess boundary
# (bluei.engine.clean_prs.run_capture / bluei.engine.gh.gh_json) and let real
# state files do the persistence legwork where applicable.
# ===========================================================================


class TestStalePRCleanup:
    """clean_stale_prs identifies stale PRs and closes them via gh pr close."""

    def test_stale_pr_detection_and_cleanup(self, repo_with_remote, tmp_path):
        from datetime import timedelta

        from bluei.engine.clean_prs import clean_stale_prs

        # --- Arrange: one fresh, one 12h-old, one 72h-stale ---
        # Titles use distinct rule patterns so _find_duplicate_prs doesn't
        # group them — we want to isolate the stale-detection path here.
        now = datetime.now(timezone.utc)
        log_file = tmp_path / "clean.log"
        log_file.write_text("")

        fresh_pr = {
            "number": 101,
            "title": "fix: resolve ruff-c408 in catalog.py",
            "headRefName": "fix/fresh-101",
            "createdAt": now.isoformat(),
            "updatedAt": now.isoformat(),
            "body": "",
            "labels": [],
        }
        stale_pr = {
            "number": 102,
            "title": "fix: resolve ruff-e501 in orders.py",
            "headRefName": "fix/stale-102",
            "createdAt": (now - timedelta(hours=72)).isoformat(),
            "updatedAt": (now - timedelta(hours=72)).isoformat(),
            "body": "",
            "labels": [],
        }
        slightly_old_pr = {
            "number": 103,
            "title": "fix: resolve ruff-f401 in users.py",
            "headRefName": "fix/old-103",
            "createdAt": (now - timedelta(hours=12)).isoformat(),
            "updatedAt": (now - timedelta(hours=12)).isoformat(),
            "body": "",
            "labels": [],
        }
        served_prs = [fresh_pr, stale_pr, slightly_old_pr]
        closed_calls = []

        def mock_run_capture(cmd, *, cwd=None, timeout=None):
            # Intercept gh pr list (return canned JSON) and gh pr close (record).
            if not cmd or cmd[0] != "gh":
                return (0, "")
            if len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "list":
                return (0, json.dumps(served_prs))
            if len(cmd) >= 4 and cmd[1] == "pr" and cmd[2] == "close":
                closed_calls.append({"pr_number": int(cmd[3]), "cmd": list(cmd)})
                return (0, "")
            return (0, "")

        # --- Act ---
        with patch("bluei.engine.clean_prs.run_capture", side_effect=mock_run_capture):
            result = clean_stale_prs(
                repo_slug="test/repo",
                cwd=repo_with_remote,
                log_file=log_file,
                dry_run=False,
                stale_hours=48,
            )

        # --- Assert: summary counts ---
        assert result["stale"] == 1, f"Expected exactly 1 stale PR, got {result}"
        assert result["closed"] == 1, f"Expected exactly 1 closed PR, got {result}"
        assert result["duplicates"] == 0, "No duplicates in this fixture"

        # --- Assert: gh pr close invoked once, on #102 only ---
        closed_numbers = {c["pr_number"] for c in closed_calls}
        assert closed_numbers == {102}, (
            f"Expected only stale #102 closed, got {closed_numbers}"
        )
        assert 101 not in closed_numbers, "Fresh PR #101 must not be closed"
        assert 103 not in closed_numbers, "12h-old PR #103 must not be closed"

        # --- Assert: close command shape matches gh pr close contract ---
        close_cmd = next(c for c in closed_calls if c["pr_number"] == 102)["cmd"]
        assert close_cmd[:3] == ["gh", "pr", "close"]
        assert close_cmd[3] == "102"
        assert "--repo" in close_cmd
        assert "test/repo" in close_cmd
        # Comment carries the staleness reason
        comment_idx = close_cmd.index("--comment")
        assert "stale" in close_cmd[comment_idx + 1].lower()

        # --- Assert: log file persisted (real state side-effect) ---
        log_text = log_file.read_text()
        assert "clean-stale-prs" in log_text
        assert "#102" in log_text


class TestReviewObservationState:
    """Observation cycle persists active_prs.json + review_state.json on disk."""

    def test_review_observation_persists_state(self, tmp_path):
        from bluei.app.config import ConfigManager
        from bluei.app.registry import RepoRegistry
        from bluei.app.state import StateManager
        from bluei.engine.models import now_iso

        # --- Arrange: real workspace, real registry, real state manager ---
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config_mgr = ConfigManager(workspace=workspace)
        registry = RepoRegistry(config_mgr)
        state_mgr = StateManager(config_mgr.repos_dir)

        repo_name = "test-repo"
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()

        config = config_mgr.render_config_from_template(
            name=repo_name,
            path=str(repo_path),
            language="python",
        )
        registry.create(config)

        ts = now_iso()
        active_prs_data = {
            "prs": {
                "10": {"status": "open", "observations": 3, "last_seen": ts},
                "11": {"status": "open", "observations": 1, "last_seen": ts},
                "12": {"status": "merged", "observations": 5, "last_seen": ts},
            }
        }
        review_state_data = {
            "prs": {
                "10": {"last_review": ts, "findings_count": 2},
            }
        }

        # --- Act: write both state files (real disk persistence, no mocks) ---
        state_mgr.save_active_prs(repo_name, active_prs_data)
        state_mgr.save_review_state(repo_name, review_state_data)

        # --- Assert: files exist at expected locations ---
        active_file = state_mgr.get_active_prs_file(repo_name)
        review_file = state_mgr.get_review_state_file(repo_name)
        assert active_file.exists(), f"active_prs.json not written at {active_file}"
        assert review_file.exists(), f"review_state.json not written at {review_file}"

        # --- Assert: raw JSON on disk has the expected shape ---
        with open(active_file) as f:
            active_on_disk = json.load(f)
        assert "prs" in active_on_disk
        assert active_on_disk["prs"]["10"]["status"] == "open"
        assert active_on_disk["prs"]["11"]["observations"] == 1
        assert active_on_disk["prs"]["12"]["status"] == "merged"

        with open(review_file) as f:
            review_on_disk = json.load(f)
        assert "prs" in review_on_disk
        assert review_on_disk["prs"]["10"]["findings_count"] == 2
        assert review_on_disk["prs"]["10"]["last_review"] == ts

        # --- Assert: round-trip through StateManager's load API ---
        loaded_active = state_mgr.load_active_prs(repo_name)
        assert loaded_active["prs"]["10"]["observations"] == 3
        assert loaded_active["prs"]["11"]["status"] == "open"
        assert loaded_active["prs"]["12"]["status"] == "merged"

        loaded_review = state_mgr.load_review_state(repo_name)
        assert "10" in loaded_review["prs"]
        assert loaded_review["prs"]["10"]["last_review"] == ts
        assert loaded_review["prs"]["10"]["findings_count"] == 2

        # --- Assert: a subsequent observation overwrites prior state cleanly ---
        updated_active = {
            "prs": {
                "10": {"status": "merged", "observations": 4, "last_seen": ts},
            }
        }
        state_mgr.save_active_prs(repo_name, updated_active)
        reloaded = state_mgr.load_active_prs(repo_name)
        assert reloaded["prs"]["10"]["status"] == "merged"
        assert reloaded["prs"]["10"]["observations"] == 4
        # Old PRs 11/12 are gone because the second write replaced the dict
        assert "11" not in reloaded.get("prs", {})
        assert "12" not in reloaded.get("prs", {})


class TestBatchPRDedup:
    """find_batch_pr_by_rule returns existing batch PR so dupes aren't created."""

    def test_batch_pr_dedup_skips_existing(self, repo_with_remote):
        from datetime import timedelta

        from bluei.engine.gh import find_batch_pr_by_rule

        # --- Arrange: 4 PRs — fresh batch, stale batch, wrong branch, wrong rule ---
        now = datetime.now(timezone.utc)
        fresh_batch_pr = {
            "number": 99,
            "title": "fix: resolve 9 ruff-c408 findings",
            "headRefName": "qa/batch-c408-abc12345",
            "createdAt": (now - timedelta(hours=1)).isoformat(),
            "url": "https://github.com/test/repo/pull/99",
        }
        stale_batch_pr = {
            "number": 88,
            "title": "fix: resolve 3 ruff-c408 findings",
            "headRefName": "qa/batch-c408-oldoldold",
            "createdAt": (now - timedelta(hours=72)).isoformat(),
            "url": "https://github.com/test/repo/pull/88",
        }
        unrelated_branch_pr = {
            "number": 77,
            "title": "fix: resolve ruff-e501 in main.py",
            "headRefName": "fix/long-lines",  # not a batch branch
            "createdAt": (now - timedelta(hours=1)).isoformat(),
            "url": "https://github.com/test/repo/pull/77",
        }
        different_rule_batch_pr = {
            "number": 66,
            "title": "fix: resolve 2 ruff-e501 findings",
            "headRefName": "qa/batch-e501-deadbeef",  # batch branch, different rule
            "createdAt": (now - timedelta(hours=1)).isoformat(),
            "url": "https://github.com/test/repo/pull/66",
        }
        served_payload = [
            fresh_batch_pr,
            stale_batch_pr,
            unrelated_branch_pr,
            different_rule_batch_pr,
        ]

        # --- Act: happy path — find the fresh ruff-c408 batch PR ---
        with patch("bluei.engine.gh.gh_json", return_value=served_payload):
            result = find_batch_pr_by_rule(
                "test/repo", "ruff-c408", cwd=repo_with_remote, max_age_hours=24
            )

        # --- Assert: returned the fresh matching PR ---
        assert result is not None, "Expected to find existing batch PR for ruff-c408"
        assert result["number"] == 99
        assert result["headRefName"].startswith("qa/batch-c408-")
        # Stale PR (#88) matched the prefix but exceeded the age window
        assert result["number"] != 88
        # Wrong-rule batch PR (#66) was excluded by the prefix
        assert result["number"] != 66

        # --- Act: negative path — unknown rule returns None ---
        with patch("bluei.engine.gh.gh_json", return_value=served_payload):
            no_match = find_batch_pr_by_rule(
                "test/repo", "ruff-f401", cwd=repo_with_remote, max_age_hours=24
            )
        assert no_match is None, (
            "Should return None when no batch PR matches the rule prefix"
        )

        # --- Act: boundary path — zero-hour window excludes even the fresh PR ---
        with patch("bluei.engine.gh.gh_json", return_value=served_payload):
            too_strict = find_batch_pr_by_rule(
                "test/repo", "ruff-c408", cwd=repo_with_remote, max_age_hours=0
            )
        assert too_strict is None, "0h age window must exclude the 1h-old fresh PR"
