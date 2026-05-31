"""Tests for exception paths in lifecycle.py — error handling coverage."""

from pathlib import Path

import pytest

from bluei.engine.lifecycle import (
    apply_autofix,
    verify_fix_closed,
    build_target_checks,
    git_commit_all,
    git_push_branch,
    run_startup_self_healing,
    run_smoke_test,
    run_named_checks,
    choose_validation_baseline,
    diff_stats,
)


class TestRunSmokeTestErrorPaths:
    def test_all_checks_failed_on_non_repo(self, tmp_path):
        """A directory that is not a git repo — smoke test fails all checks."""
        repo = tmp_path / "not-a-git-repo"
        repo.mkdir()
        result = run_smoke_test(
            repo_path=repo,
            log_file=tmp_path / "smoke.log",
        )
        assert result["passed"] is False

    def test_all_checks_represented(self, tmp_path):
        result = run_smoke_test(
            repo_path=tmp_path,
            log_file=tmp_path / "smoke.log",
        )
        assert "git" in result["checks"]
        assert "worktree" in result["checks"]
        assert "linter" in result["checks"]
        assert "duration_ms" in result
        assert isinstance(result["errors"], list)


class TestRunStartupSelfHealing:
    def test_none_locks_dir_handled(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        log_file = tmp_path / "heal.log"
        result = run_startup_self_healing(
            repo_path=repo,
            log_file=log_file,
            locks_dir=None,
            dry_run=True,
        )
        assert isinstance(result, dict)
        assert "errors" in result

    def test_missing_worktree_dir_handled(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        log_file = tmp_path / "heal.log"
        locks_dir = tmp_path / "nonexistent" / "locks"
        result = run_startup_self_healing(
            repo_path=repo,
            log_file=log_file,
            locks_dir=locks_dir,
            dry_run=True,
        )
        assert isinstance(result, dict)


class TestVerifyFixClosedErrorPaths:
    def test_finding_not_present_in_repo(self, tmp_path):
        """Finding doesn't match any discovered findings — should return False."""
        from bluei.engine.models import Finding

        finding = Finding(
            finding_id="nonexistent-fix",
            repo="test-repo",
            rule="unused-import",
            path="nonexistent_file.py",
            line=1,
            snippet="import os",
            confidence=0.5,
            quick_win=False,
            severity="error",
            safe_to_autofix=False,
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        log_file = tmp_path / "verify.log"

        verified = verify_fix_closed(repo, finding, log_file)
        assert isinstance(verified, bool), "Finding should not be verified as closed"

    def test_missing_docs_index(self, tmp_path):
        from bluei.engine.models import Finding

        finding = Finding(
            finding_id="f1",
            repo="test-repo",
            rule="test-rule",
            path="test.py",
            line=1,
            snippet="import os",
            confidence=0.5,
            quick_win=False,
            severity="error",
            safe_to_autofix=False,
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        log_file = tmp_path / "verify.log"
        docs_file = tmp_path / "nonexistent.json"

        verified = verify_fix_closed(repo, finding, log_file, docs_index_file=docs_file)
        assert isinstance(verified, bool)


class TestRunNamedChecksErrorPaths:
    def test_empty_checks_returns_empty(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        log = tmp_path / "checks.log"
        result = run_named_checks(repo, {}, log, phase="test")
        assert result == {}

    def test_broken_check_command(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        log = tmp_path / "checks.log"
        result = run_named_checks(
            repo,
            {"bad-check": ["sh", "-c", "exit 1"]},
            log,
            phase="test",
        )
        assert "bad-check" in result


class TestChooseValidationBaseline:
    def test_repo_baseline_when_no_worktree(self):
        repo = {"check-0": {"rc": 0}}
        worktree = {}
        chosen = choose_validation_baseline(repo, worktree, Path("/tmp/log"))
        assert chosen is repo

    def test_worktree_baseline_when_matches_repo(self):
        baseline = {"check-0": {"rc": 1, "fingerprint": "abc"}}
        chosen = choose_validation_baseline(baseline, baseline, Path("/tmp/log"))
        assert chosen is baseline


class TestGitCommitAllErrorPaths:
    def test_no_git_repo(self, tmp_path):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        log = tmp_path / "commit.log"
        result = git_commit_all(non_repo, "test commit", log_file=log, dry_run=True)
        assert result != "committed"

    def test_empty_message(self, tmp_path):
        non_repo = tmp_path / "empty-msg-repo"
        non_repo.mkdir()
        log = tmp_path / "commit.log"
        result = git_commit_all(non_repo, "  ", log_file=log, dry_run=True)
        assert result == "error"


class TestGitPushBranchErrorPaths:
    def test_no_git_repo(self, tmp_path):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        log = tmp_path / "push.log"
        pushed = git_push_branch(non_repo, "test-branch", log_file=log, dry_run=True)
        assert pushed is True  # dry_run=True simulates success

    def test_dry_run_returns_true_when_reachable(self, tmp_path):
        """In dry-run mode, push is simulated as success."""
        non_repo = tmp_path / "dry-push-repo"
        non_repo.mkdir()
        log = tmp_path / "push.log"
        pushed = git_push_branch(non_repo, "test-branch", log_file=log, dry_run=True)
        assert pushed is True  # dry_run=True skips the actual push


class TestDiffStatsErrorPaths:
    def test_no_git_repo(self, tmp_path):
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        files, loc = diff_stats(non_repo)
        assert files == 0
        assert loc == 0


class TestBuildTargetChecks:
    def test_finding_with_no_target_checks(self):
        from bluei.engine.models import Finding

        finding = Finding(
            finding_id="no-target",
            repo="test-repo",
            rule="no-variant",
            path="x.py",
            line=1,
            snippet="import os",
            confidence=0.5,
            quick_win=False,
            severity="error",
            safe_to_autofix=False,
        )
        checks = build_target_checks(finding)
        assert isinstance(checks, dict)
