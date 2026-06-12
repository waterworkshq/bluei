"""Tests for bluei.engine.worktree — worktree lifecycle management."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from bluei.engine.worktree import (
    WorktreeResult,
    create_worktree,
    remove_worktree,
    prune_worktrees,
    hydrate_worktree,
    get_worktree_branch,
)


@pytest.fixture
def mock_run():
    """Mock subprocess.run for git commands."""
    with patch("bluei.engine.utils.subprocess.run") as m:
        m.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yield m


@pytest.fixture
def repo_path(tmp_path):
    """Create a mock repo directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture
def worktree_path(tmp_path):
    """Create a mock worktree directory."""
    wt = tmp_path / "worktree"
    return wt


@pytest.fixture
def log_file(tmp_path):
    """Create a mock log file."""
    return tmp_path / "test.log"


class TestCreateWorktree:
    """Tests for create_worktree()."""

    def test_create_worktree_success(
        self, repo_path, worktree_path, log_file, mock_run
    ):
        """Successful worktree creation returns WorktreeResult with success=True."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        result = create_worktree(
            repo_path=repo_path,
            branch="test-branch",
            worktree_path=worktree_path,
            log_file=log_file,
        )

        assert result.success is True
        assert result.worktree_path == worktree_path
        assert result.branch == "test-branch"
        assert result.error is None

        # Verify git commands were called
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("worktree" in cmd and "prune" in cmd for cmd in calls)
        assert any("worktree" in cmd and "add" in cmd for cmd in calls)

    def test_create_worktree_with_start_point(
        self, repo_path, worktree_path, log_file, mock_run
    ):
        """Worktree creation with start_point appends it to the git command."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        result = create_worktree(
            repo_path=repo_path,
            branch="test-branch",
            worktree_path=worktree_path,
            start_point="origin/main",
            log_file=log_file,
        )

        assert result.success is True
        # Verify start_point was included in the add command
        add_calls = [c for c in mock_run.call_args_list if "add" in str(c)]
        assert len(add_calls) > 0
        add_cmd = add_calls[-1][0][0]
        assert "origin/main" in add_cmd

    def test_create_worktree_failure(
        self, repo_path, worktree_path, log_file, mock_run
    ):
        """Failed worktree creation returns WorktreeResult with success=False and error."""
        mock_run.return_value = MagicMock(returncode=1, stdout="fatal: error")

        result = create_worktree(
            repo_path=repo_path,
            branch="test-branch",
            worktree_path=worktree_path,
            log_file=log_file,
        )

        assert result.success is False
        assert result.error is not None
        assert "failed to create worktree" in result.error

    def test_create_worktree_removes_existing(
        self, repo_path, tmp_path, log_file, mock_run
    ):
        """If worktree_path exists, it's removed before creation."""
        existing = tmp_path / "existing"
        existing.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        result = create_worktree(
            repo_path=repo_path,
            branch="test-branch",
            worktree_path=existing,
            log_file=log_file,
        )

        assert result.success is True
        # Verify rm -rf was called
        rm_calls = [c for c in mock_run.call_args_list if "rm" in str(c)]
        assert len(rm_calls) > 0

    def test_create_worktree_logs_to_file(
        self, repo_path, worktree_path, log_file, mock_run
    ):
        """Operations are logged to the log file."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        create_worktree(
            repo_path=repo_path,
            branch="test-branch",
            worktree_path=worktree_path,
            log_file=log_file,
        )

        log_content = log_file.read_text()
        assert "worktree:" in log_content
        assert "created" in log_content or "prune" in log_content.lower()


class TestRemoveWorktree:
    """Tests for remove_worktree()."""

    def test_remove_worktree_basic(self, repo_path, worktree_path, log_file, mock_run):
        """Basic worktree removal calls git worktree remove and prune."""
        worktree_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        remove_worktree(
            worktree_path=worktree_path,
            repo_path=repo_path,
            log_file=log_file,
        )

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("worktree" in cmd and "remove" in cmd for cmd in calls)
        assert any("worktree" in cmd and "prune" in cmd for cmd in calls)

    def test_remove_worktree_with_branch_deletion(
        self, repo_path, worktree_path, log_file, mock_run
    ):
        """When delete_branch=True, the branch is deleted after worktree removal."""
        worktree_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        remove_worktree(
            worktree_path=worktree_path,
            repo_path=repo_path,
            branch="test-branch",
            delete_branch=True,
            log_file=log_file,
        )

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("branch" in cmd and "-D" in cmd for cmd in calls)

    def test_remove_worktree_without_branch_deletion(
        self, repo_path, worktree_path, log_file, mock_run
    ):
        """When delete_branch=False, the branch is NOT deleted."""
        worktree_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        remove_worktree(
            worktree_path=worktree_path,
            repo_path=repo_path,
            branch="test-branch",
            delete_branch=False,
            log_file=log_file,
        )

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert not any("branch" in cmd and "-D" in cmd for cmd in calls)

    def test_remove_worktree_nonexistent_path(
        self, repo_path, worktree_path, log_file, mock_run
    ):
        """If worktree_path doesn't exist, skip the remove command."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        remove_worktree(
            worktree_path=worktree_path,
            repo_path=repo_path,
            log_file=log_file,
        )

        # prune should still be called
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("worktree" in cmd and "prune" in cmd for cmd in calls)


class TestPruneWorktrees:
    """Tests for prune_worktrees()."""

    def test_prune_worktrees_calls_git(self, repo_path, mock_run):
        """prune_worktrees calls git worktree prune."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        prune_worktrees(repo_path=repo_path)

        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("worktree" in cmd and "prune" in cmd for cmd in calls)


class TestHydrateWorktree:
    """Tests for hydrate_worktree()."""

    def test_hydrate_creates_symlink(self, repo_path, worktree_path, log_file):
        """hydrate_worktree creates a symlink for node_modules."""
        worktree_path.mkdir()
        node_modules = repo_path / "node_modules"
        node_modules.mkdir()

        hydrate_worktree(
            repo_path=repo_path,
            worktree_path=worktree_path,
            log_file=log_file,
        )

        target = worktree_path / "node_modules"
        assert target.is_symlink()
        assert os.readlink(target) == str(node_modules)

    def test_hydrate_skips_if_source_missing(self, repo_path, worktree_path, log_file):
        """hydrate_worktree skips if source doesn't exist."""
        worktree_path.mkdir()
        # No node_modules in repo_path

        hydrate_worktree(
            repo_path=repo_path,
            worktree_path=worktree_path,
            log_file=log_file,
        )

        target = worktree_path / "node_modules"
        assert not target.exists()

    def test_hydrate_skips_if_target_exists(self, repo_path, worktree_path, log_file):
        """hydrate_worktree skips if target already exists."""
        worktree_path.mkdir()
        node_modules = repo_path / "node_modules"
        node_modules.mkdir()
        target = worktree_path / "node_modules"
        target.mkdir()  # Already exists

        hydrate_worktree(
            repo_path=repo_path,
            worktree_path=worktree_path,
            log_file=log_file,
        )

        # Should not be a symlink (still a directory)
        assert not target.is_symlink()

    def test_hydrate_logs_operations(self, repo_path, worktree_path, log_file):
        """hydrate_worktree logs symlink operations."""
        worktree_path.mkdir()
        node_modules = repo_path / "node_modules"
        node_modules.mkdir()

        hydrate_worktree(
            repo_path=repo_path,
            worktree_path=worktree_path,
            log_file=log_file,
        )

        log_content = log_file.read_text()
        assert "worktree-deps:" in log_content
        assert "linked" in log_content


class TestGetWorktreeBranch:
    """Tests for get_worktree_branch()."""

    def test_get_worktree_branch_success(self, worktree_path):
        """get_worktree_branch returns the branch name on success."""
        worktree_path.mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="test-branch\n",
            )

            branch = get_worktree_branch(worktree_path)

            assert branch == "test-branch"

    def test_get_worktree_branch_failure(self, worktree_path):
        """get_worktree_branch returns empty string on failure."""
        worktree_path.mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
            )

            branch = get_worktree_branch(worktree_path)

            assert branch == ""

    def test_get_worktree_branch_exception(self, worktree_path):
        """get_worktree_branch returns empty string on exception."""
        worktree_path.mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("git not found")

            branch = get_worktree_branch(worktree_path)

            assert branch == ""


class TestWorktreeResult:
    """Tests for WorktreeResult dataclass."""

    def test_worktree_result_defaults(self):
        """WorktreeResult has sensible defaults."""
        result = WorktreeResult(
            success=True,
            worktree_path=Path("/tmp/wt"),
            branch="test",
        )
        assert result.error is None

    def test_worktree_result_with_error(self):
        """WorktreeResult can hold an error message."""
        result = WorktreeResult(
            success=False,
            worktree_path=Path("/tmp/wt"),
            branch="test",
            error="something went wrong",
        )
        assert result.error == "something went wrong"
