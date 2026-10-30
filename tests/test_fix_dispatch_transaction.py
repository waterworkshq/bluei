"""Checkpoint errors must escape fix dispatch; no fallback after unsafe state.

Also covers KeyboardInterrupt restoration through the apply_autofix
transactional wrapper, including staged and unstaged pre-existing state.
"""

import subprocess
from unittest.mock import patch

import pytest

from bluei.engine import fix_dispatch, lifecycle
from bluei.engine.fix_dispatch import FixDispatchResult, FixStrategy, dispatch_fix
from bluei.engine.worktree_checkpoint import CheckpointError, CheckpointRestoreError


def _finding(make_finding, **overrides):
    defaults = {"finding_id": "f-1", "rule": "ruff-e501", "path": "src/app.py"}
    defaults.update(overrides)
    return make_finding(**defaults)


def test_cascade_restore_error_propagates_and_blocks_claude_fallback(
    tmp_path, make_finding
):
    called = []

    def unsafe(*args, **kwargs):
        called.append("cascade")
        raise CheckpointRestoreError("restore failed; backup retained")

    with patch.object(lifecycle, "apply_cascade_fix", side_effect=unsafe), patch.object(
        fix_dispatch, "_try_claude_fix", side_effect=lambda *a, **k: called.append("claude")
    ):
        with pytest.raises(CheckpointRestoreError, match="backup retained"):
            dispatch_fix(
                _finding(make_finding),
                tmp_path,
                FixStrategy.CASCADE_FIRST,
                repo_path=tmp_path,
                log_file=tmp_path / "log",
                claude_cmd_template="claude",
            )
    assert called == ["cascade"], "no fallback may run after unsafe state"


def test_autofix_checkpoint_error_propagates_and_blocks_fallbacks(tmp_path, make_finding):
    called = []

    def unsafe(*args, **kwargs):
        called.append("autofix")
        raise CheckpointError("worktree over checkpoint ceiling")

    with patch.object(lifecycle, "apply_autofix", side_effect=unsafe), patch.object(
        fix_dispatch, "_try_contextual_fix", side_effect=lambda *a, **k: called.append("contextual")
    ), patch.object(
        fix_dispatch, "_try_claude_fix", side_effect=lambda *a, **k: called.append("claude")
    ):
        with pytest.raises(CheckpointError, match="checkpoint ceiling"):
            dispatch_fix(
                _finding(make_finding),
                tmp_path,
                FixStrategy.AUTOFIX_FIRST,
                repo_path=tmp_path,
                log_file=tmp_path / "log",
                claude_cmd_template="claude",
            )
    assert called == ["autofix"], "no fallback may run after unsafe state"


def test_dispatch_generic_failure_still_falls_back(tmp_path, make_finding):
    """Ordinary failures keep the intended fallback feature (regression guard)."""
    from types import SimpleNamespace

    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("original")

    class OkRecipe:
        def match(self, *a, **k):
            return SimpleNamespace(id="r")

        def apply(self, recipe, file_path, worktree, finding=None):
            file_path.write_text("fixed")
            return SimpleNamespace(success=True)

    with patch.object(lifecycle, "_get_recipe_engine", lambda: OkRecipe()), patch.object(
        lifecycle, "_tier_validate", lambda *a: True
    ):
        result = dispatch_fix(
            _finding(make_finding),
            tmp_path,
            FixStrategy.MINIMAL,
            repo_path=tmp_path,
            log_file=tmp_path / "log",
        )
    assert result.success and result.method_used == "autofix"
    assert target.read_text() == "fixed"


def _commit(repo, rel, content):
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    subprocess.run(["git", "add", rel], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)


def test_keyboard_interrupt_after_write_restores_staged_and_unstaged(
    git_repo, make_finding
):
    from types import SimpleNamespace

    _commit(git_repo, "src/app.py", "committed\n")
    target = git_repo / "src/app.py"
    target.write_text("staged\n")
    subprocess.run(["git", "add", "src/app.py"], cwd=git_repo, capture_output=True, check=True)
    target.write_text("unstaged\n")
    index_before = (git_repo / ".git" / "index").read_bytes()

    class InterruptingRecipe:
        def match(self, *a, **k):
            return SimpleNamespace(id="r")

        def apply(self, recipe, file_path, worktree, finding=None):
            file_path.write_text("INTERRUPTED MID-EDIT\n")
            raise KeyboardInterrupt

    with patch.object(lifecycle, "_get_recipe_engine", lambda: InterruptingRecipe()):
        with pytest.raises(KeyboardInterrupt):
            lifecycle.apply_autofix(
                git_repo, _finding(make_finding), git_repo / "log"
            )

    assert target.read_text() == "unstaged\n"
    assert (git_repo / ".git" / "index").read_bytes() == index_before
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True
    ).stdout.strip()
    # The log file lives inside the worktree but is checkpoint-excluded, so it
    # legitimately remains as untracked.
    assert status.splitlines()[0] == "MM src/app.py", status
