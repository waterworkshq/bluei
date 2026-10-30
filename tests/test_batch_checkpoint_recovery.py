"""Checkpoint failures must preserve accepted batch work for manual recovery."""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bluei.engine import batch_pr
from bluei.engine.models import BatchGroup
from bluei.engine.worktree_checkpoint import CheckpointError, CheckpointRestoreError


@pytest.mark.parametrize("failure", [CheckpointError, CheckpointRestoreError])
def test_batch_checkpoint_failure_preserves_real_worktree(git_repo, tmp_path, make_finding, failure):
    target = git_repo / "app.py"
    target.write_text("baseline\n")
    subprocess.run(["git", "add", "app.py"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=git_repo, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()
    batch = BatchGroup("recover", "ruff-f401", findings=[
        make_finding(path="app.py"), make_finding(path="other.py")])
    args = SimpleNamespace(dry_run=True, live_github_actions=False,
                           worktree_root=str(tmp_path / "worktrees"))

    def interrupted_batch(**kwargs):
        (kwargs["worktree_path"] / "app.py").write_text("accepted batch fix\n")
        raise failure("checkpoint cannot be cleaned or restored")

    with patch.object(batch_pr, "apply_batch_fixes", side_effect=interrupted_batch), \
            patch("bluei.engine.git_ops.git_commit_all") as commit, \
            patch("bluei.engine.git_ops.git_push_branch") as push:
        with pytest.raises(failure) as excinfo:
            batch_pr.process_batch(batch, git_repo, args, tmp_path / "batch.log")
    commit.assert_not_called()
    push.assert_not_called()
    assert batch.worktree_path.is_dir()
    assert (batch.worktree_path / "app.py").read_text() == "accepted batch fix\n"
    assert target.read_text() == "baseline\n"
    assert subprocess.check_output(["git", "rev-parse", batch.branch], cwd=git_repo, text=True).strip() == base
    assert str(batch.worktree_path) in " ".join(excinfo.value.__notes__)
    subprocess.run(["git", "worktree", "remove", "--force", str(batch.worktree_path)], cwd=git_repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "branch", "-D", batch.branch], cwd=git_repo, check=True, capture_output=True)
