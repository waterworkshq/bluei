"""Checkpoint failure containment under the unqualified batch-repair refusal.

Current contract (ACCEPTANCE-BOUNDARY-3ks3txvh): process_batch refuses an
unqualified batch BEFORE creating a worktree, so the checkpoint-failure
recovery path (worktree retention for manual recovery) is never entered.
These tests prove the refusal fires first with no filesystem, git, or batch
side effects on a real repository.
"""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bluei.engine import batch_pr
from bluei.engine.models import BatchGroup
from bluei.engine.worktree_checkpoint import CheckpointError, CheckpointRestoreError

UNQUALIFIED_DETAIL = (
    "batch repair unqualified: missing configured scope/validation/closure acceptance"
)


@pytest.mark.parametrize("failure", [CheckpointError, CheckpointRestoreError, KeyboardInterrupt, SystemExit])
def test_batch_checkpoint_failure_preserves_real_worktree(git_repo, tmp_path, make_finding, failure):
    # current contract: batch repair is refused before any worktree exists, so
    # a checkpoint failure can never surface — if the patched apply_batch_fixes
    # were reached it would raise `failure` and fail this test loudly.
    target = git_repo / "app.py"
    target.write_text("baseline\n")
    subprocess.run(["git", "add", "app.py"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=git_repo, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()
    worktree_root = tmp_path / "worktrees"
    batch = BatchGroup("recover", "ruff-f401", findings=[
        make_finding(path="app.py"), make_finding(path="other.py")])
    args = SimpleNamespace(dry_run=True, live_github_actions=False,
                           worktree_root=str(worktree_root))
    log_file = tmp_path / "batch.log"

    def interrupted_batch(**kwargs):
        raise failure("checkpoint cannot be cleaned or restored")

    with patch.object(batch_pr, "apply_batch_fixes", side_effect=interrupted_batch):
        ok, detail = batch_pr.process_batch(batch, git_repo, args, log_file)

    assert ok is False
    assert detail == UNQUALIFIED_DETAIL
    # Batch state untouched: no worktree/branch recorded, status never left open.
    assert batch.worktree_path is None
    assert batch.branch is None
    assert batch.status == "open"
    # No filesystem side effects: no worktree root, nothing staged in the repo.
    assert not worktree_root.exists()
    worktrees = subprocess.check_output(
        ["git", "worktree", "list", "--porcelain"], cwd=git_repo, text=True)
    assert "qa-batch-" not in worktrees
    branches = subprocess.check_output(
        ["git", "branch", "--list", "qa/batch-*"], cwd=git_repo, text=True).strip()
    assert branches == ""
    # Source unchanged: HEAD still at the baseline commit, file content intact.
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip() == base
    assert target.read_text() == "baseline\n"
    # The refusal itself is logged (no checkpoint-failure log line exists).
    log = log_file.read_text()
    assert "batch-refused" in log
    assert "batch-checkpoint-failure" not in log
