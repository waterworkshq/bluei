"""Tests for bluei.engine.startup — self-healing: stale locks, orphan worktrees, state repair."""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.startup import (
    STALE_LOCK_HOURS,
    _parse_worktree_list_porcelain,
    run_startup_self_healing,
)


class TestParseWorktreeListPorcelain:
    def test_parses_valid_output(self):
        output = (
            "worktree /repo\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo/bluei-wt\n"
            "HEAD def456\n"
            "branch refs/heads/fix-1\n"
            "\n"
        )
        with patch("bluei.engine.startup.run_capture", return_value=(0, output)):
            result = _parse_worktree_list_porcelain(Path("/repo"))

        assert len(result) == 2
        assert result[0]["worktree"] == "/repo"
        assert result[0]["branch"] == "refs/heads/main"
        assert result[1]["worktree"] == "/repo/bluei-wt"
        assert result[1]["branch"] == "refs/heads/fix-1"

    def test_parses_detached_head(self):
        output = "worktree /repo\nHEAD abc\ndetached\n\n"
        with patch("bluei.engine.startup.run_capture", return_value=(0, output)):
            result = _parse_worktree_list_porcelain(Path("/repo"))
        assert result[0]["branch"] == "detached"

    def test_returns_empty_on_failure(self):
        with patch("bluei.engine.startup.run_capture", return_value=(1, "")):
            assert _parse_worktree_list_porcelain(Path("/repo")) == []

    def test_returns_empty_on_exception(self):
        with patch("bluei.engine.startup.run_capture", side_effect=OSError("nope")):
            assert _parse_worktree_list_porcelain(Path("/repo")) == []


class TestStaleLockCleanup:
    def test_removes_stale_locks(self, tmp_path):
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        stale = locks_dir / "old.lock"
        stale.write_text("{}")
        old_time = time.time() - (STALE_LOCK_HOURS + 1) * 3600
        os.utime(stale, (old_time, old_time))

        fresh = locks_dir / "fresh.lock"
        fresh.write_text("{}")

        result = run_startup_self_healing(
            repo_path=tmp_path,
            locks_dir=locks_dir,
        )
        assert result["stale_locks_removed"] == 1
        assert not stale.exists()
        assert fresh.exists()

    def test_dry_run_does_not_remove(self, tmp_path):
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        stale = locks_dir / "old.lock"
        stale.write_text("{}")
        old_time = time.time() - (STALE_LOCK_HOURS + 1) * 3600
        os.utime(stale, (old_time, old_time))

        result = run_startup_self_healing(
            repo_path=tmp_path,
            locks_dir=locks_dir,
            dry_run=True,
        )
        assert result["stale_locks_removed"] == 1
        assert stale.exists()

    def test_no_locks_dir_is_safe(self, tmp_path):
        with patch("bluei.engine.startup.run_capture", return_value=(0, "")):
            result = run_startup_self_healing(
                repo_path=tmp_path,
                locks_dir=tmp_path / "nonexistent",
            )
        assert result["stale_locks_removed"] == 0
        assert result["errors"] == []


class TestWorktreePrune:
    @patch("bluei.engine.startup.run_capture")
    def test_prune_success(self, mock_rc):
        mock_rc.return_value = (0, "Pruning worktrees")
        result = run_startup_self_healing(repo_path=Path("/tmp/r"))
        assert result["worktrees_pruned"] is True

    @patch("bluei.engine.startup.run_capture")
    def test_prune_failure_recorded(self, mock_rc):
        mock_rc.return_value = (1, "error")
        result = run_startup_self_healing(repo_path=Path("/tmp/r"))
        assert result["worktrees_pruned"] is False
        assert any("worktree-prune" in e for e in result["errors"])


class TestStateRepair:
    @patch("bluei.engine.constants.DEFAULT_STATE")
    @patch("bluei.engine.startup.repair_state")
    def test_repairs_corrupted_state(self, mock_repair, mock_ds, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{corrupt")
        mock_ds.exists.return_value = True
        mock_ds.__fspath__ = lambda s: str(state_file)
        mock_repair.return_value = True

        with patch("bluei.engine.startup.run_capture", return_value=(0, "")):
            result = run_startup_self_healing(repo_path=tmp_path)
        mock_repair.assert_called()


class TestOrphanWorktreeDetection:
    @patch("bluei.engine.startup.run_no_capture")
    @patch("bluei.engine.startup.run_capture")
    @patch("bluei.engine.constants.DEFAULT_WORKTREE_ROOT")
    def test_removes_orphan_dir_with_dotgit(
        self, mock_wt_root, mock_rc, mock_no_cap, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()

        wt_root = tmp_path / "worktrees"
        wt_root.mkdir()
        orphan = wt_root / "fix-abc"
        orphan.mkdir()
        (orphan / ".git").write_text("gitdir: /something")
        mock_wt_root.exists.return_value = True
        mock_wt_root.iterdir.return_value = [orphan]
        mock_wt_root.__fspath__ = lambda s: str(wt_root)

        listed_output = f"worktree {repo}\nHEAD aaa\nbranch refs/heads/main\n\n"
        mock_rc.side_effect = [
            (0, "Pruned"),
            (0, listed_output),
        ]

        result = run_startup_self_healing(repo_path=repo, log_file=None)
        assert result["worktrees_pruned"] is True
        rm_calls = [c for c in mock_no_cap.call_args_list if c[0][0][0] == "rm"]
        assert len(rm_calls) == 1

    @patch("bluei.engine.startup.run_no_capture")
    @patch("bluei.engine.startup.run_capture")
    @patch("bluei.engine.constants.DEFAULT_WORKTREE_ROOT")
    def test_skips_non_orphan_dirs(self, mock_wt_root, mock_rc, mock_no_cap, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        wt_root = tmp_path / "worktrees"
        wt_root.mkdir()
        listed_dir = wt_root / "fix-abc"
        listed_dir.mkdir()
        mock_wt_root.exists.return_value = True
        mock_wt_root.iterdir.return_value = [listed_dir]
        mock_wt_root.__fspath__ = lambda s: str(wt_root)

        listed_output = (
            f"worktree {repo}\nHEAD aaa\nbranch refs/heads/main\n\n"
            f"worktree {listed_dir}\nHEAD bbb\nbranch refs/heads/fix-abc\n\n"
        )
        mock_rc.side_effect = [
            (0, "Pruned"),
            (0, listed_output),
        ]

        result = run_startup_self_healing(repo_path=repo)
        mock_no_cap.assert_not_called()

    @patch("bluei.engine.startup.run_no_capture")
    @patch("bluei.engine.startup.run_capture")
    def test_cleans_missing_worktree_branch(self, mock_rc, mock_no_cap, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        missing_wt = tmp_path / "deleted-wt"

        listed_output = (
            f"worktree {repo}\nHEAD aaa\nbranch refs/heads/main\n\n"
            f"worktree {missing_wt}\nHEAD bbb\nbranch refs/heads/fix-old\n\n"
        )
        mock_rc.side_effect = [
            (0, "Pruned"),
            (0, listed_output),
        ]

        result = run_startup_self_healing(repo_path=repo)
        branch_delete_calls = [
            c for c in mock_no_cap.call_args_list if c[0][0][1] == "branch"
        ]
        assert len(branch_delete_calls) == 1
        assert branch_delete_calls[0][0][0] == ["git", "branch", "-D", "fix-old"]

    @patch("bluei.engine.startup.run_no_capture")
    @patch("bluei.engine.startup.run_capture")
    def test_dry_run_does_not_remove_orphan(self, mock_rc, mock_no_cap, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        missing_wt = tmp_path / "deleted-wt"

        listed_output = (
            f"worktree {repo}\nHEAD aaa\nbranch refs/heads/main\n\n"
            f"worktree {missing_wt}\nHEAD bbb\nbranch refs/heads/fix-old\n\n"
        )
        mock_rc.side_effect = [
            (0, "Pruned"),
            (0, listed_output),
        ]

        result = run_startup_self_healing(repo_path=repo, dry_run=True)
        mock_no_cap.assert_not_called()

    @patch(
        "bluei.engine.startup._parse_worktree_list_porcelain",
        side_effect=OSError("boom"),
    )
    @patch("bluei.engine.startup.run_capture")
    def test_exception_in_worktree_list_recorded(self, mock_rc, mock_parse, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        mock_rc.return_value = (0, "Pruned")

        result = run_startup_self_healing(repo_path=repo)
        assert any("worktree-list-porcelain" in e for e in result["errors"])


class TestStaleBatchCleanup:
    @patch("bluei.engine.startup.load_batches")
    @patch("bluei.engine.startup.run_capture")
    def test_removes_batch_with_deleted_branch(self, mock_rc, mock_load, tmp_path):
        batches_file = tmp_path / "batches.jsonl"
        batches_file.write_text("")

        mock_load.return_value = [
            {"batch_id": "b1", "branch": "fix-xyz", "worktree_path": "/tmp/wt"},
            {"batch_id": "b2", "branch": "fix-live", "worktree_path": "/tmp/wt2"},
        ]
        mock_rc.side_effect = [
            (0, "Pruned"),
            (0, ""),  # _parse_worktree_list_porcelain
            (1, ""),  # branch fix-xyz doesn't exist
            (0, ""),  # branch fix-live exists
        ]

        with patch("bluei.engine.constants.DEFAULT_BATCH_STATE", batches_file):
            result = run_startup_self_healing(repo_path=tmp_path)
        assert result["worktrees_pruned"] is True

    @patch("bluei.engine.startup.load_batches")
    @patch("bluei.engine.startup.run_capture")
    def test_removes_batch_with_missing_worktree(self, mock_rc, mock_load, tmp_path):
        batches_file = tmp_path / "batches.jsonl"
        batches_file.write_text("")

        mock_load.return_value = [
            {
                "batch_id": "b3",
                "branch": "fix-ok",
                "worktree_path": str(tmp_path / "nonexistent"),
            },
        ]
        mock_rc.side_effect = [
            (0, "Pruned"),
            (0, ""),  # _parse_worktree_list_porcelain
            (0, ""),  # branch exists
        ]

        with patch("bluei.engine.constants.DEFAULT_BATCH_STATE", batches_file):
            result = run_startup_self_healing(repo_path=tmp_path)

    @patch("bluei.engine.startup.load_batches")
    @patch("bluei.engine.startup.run_capture")
    def test_rewrites_batch_file_after_cleanup(self, mock_rc, mock_load, tmp_path):
        batches_file = tmp_path / "batches.jsonl"
        batches_file.write_text('{"batch_id":"old"}\n')

        active_batch = {"batch_id": "b-live", "branch": "fix-live", "worktree_path": ""}
        stale_batch = {"batch_id": "b-stale", "branch": "fix-gone", "worktree_path": ""}
        mock_load.return_value = [active_batch, stale_batch]
        mock_rc.side_effect = [
            (0, "Pruned"),
            (0, ""),  # _parse_worktree_list_porcelain call
            (1, ""),  # b-live branch missing -> stale
            (1, ""),  # b-stale branch missing -> stale
        ]

        with patch("bluei.engine.constants.DEFAULT_BATCH_STATE", batches_file):
            run_startup_self_healing(repo_path=tmp_path)
        remaining = batches_file.read_text().strip()
        assert remaining == ""

    @patch("bluei.engine.startup.load_batches")
    @patch("bluei.engine.startup.run_capture")
    def test_dry_run_does_not_rewrite_batch(self, mock_rc, mock_load, tmp_path):
        original_content = '{"batch_id":"old"}\n'
        batches_file = tmp_path / "batches.jsonl"
        batches_file.write_text(original_content)

        stale = {"batch_id": "b-stale", "branch": "fix-gone", "worktree_path": ""}
        mock_load.return_value = [stale]
        mock_rc.side_effect = [
            (0, "Pruned"),
            (0, ""),  # _parse_worktree_list_porcelain
            (1, ""),  # branch gone
        ]

        with patch("bluei.engine.constants.DEFAULT_BATCH_STATE", batches_file):
            run_startup_self_healing(repo_path=tmp_path, dry_run=True)
        assert batches_file.read_text() == original_content
