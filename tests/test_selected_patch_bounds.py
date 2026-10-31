"""Patch artifacts must be bounded and retain source issue truth."""
from pathlib import Path
import subprocess
from unittest.mock import patch

from tests.test_selected_finding_repair import _ctx, _fresh_finding, _issue, _selected_args
from bluei.engine.commands.pr_cycle import run_pr_cycle_phase


def test_dirty_selected_source_is_rejected_before_model(tmp_path):
    ctx = _ctx(tmp_path, _selected_args(tmp_path), [_issue()], eligible=[_fresh_finding()])
    (ctx.repo_path / "dirty.py").write_text("x = unknown\n")
    with patch("bluei.engine.commands.pr_cycle.apply_claude_fix") as model, patch(
        "bluei.engine.commands.pr_cycle.create_worktree"
    ) as worktree:
        result = run_pr_cycle_phase(ctx)
    assert result["selected_error"] and result["fix_attempts"] == 0
    assert ctx.issues_data["issues"][0]["status"] == "open"
    model.assert_not_called()
    worktree.assert_not_called()


def test_patch_output_file_limit_rejects_oversized_child_output(tmp_path):
    import resource
    import sys
    import tempfile
    # Same kernel-enforced output ceiling used for git diff; no unbounded PIPE.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            [sys.executable, "-c", "import os;os.write(1,b'x'*(2<<20))"], stdout=output,
            stderr=subprocess.DEVNULL,
            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, ((1 << 20) + 1,) * 2),
        )
        output.seek(0)
        assert len(output.read((1 << 20) + 2)) == (1 << 20) + 1
