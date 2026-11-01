"""Patch artifacts must be bounded and retain source issue truth."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry
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


def test_selected_public_lane_rejects_oversize_diff_candidate(tmp_path):
    """The production 1 MiB diff bound trips on a real git diff, not a scope gate.

    A fake backend closes the finding and swaps one tracked binary file for
    2 MiB of stdlib randomness: scope stays inside the scoped-run baseline
    (1 file changed, 0 loc — numstat reports "-" for binaries against the
    5-file/200-loc defaults), so only the kernel-enforced diff-output ceiling
    in the real selected patch path can reject it.
    """
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "bluei").symlink_to(root / "bluei", target_is_directory=True)
    (workspace / "plugins").symlink_to(root / "plugins", target_is_directory=True)
    target = tmp_path / "target"
    target.mkdir()
    (target / "requirements.txt").write_text("")
    (target / "app.py").write_text("def answer():\n    return missing_name\n")
    payload = b"tracked-binary-payload-v1"
    (target / "payload.bin").write_bytes(payload)
    for args in [["init", "-q"], ["add", "."],
                 ["-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture"]]:
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)
    manager = ConfigManager(workspace)
    registry = RepoRegistry(manager)
    repo = registry.create_from_path(target, name="selected", language="python")
    repo.config.fix_engine = "claude"
    repo.config.baseline_checks = [[sys.executable, "-c", "import ast;ast.parse(open('app.py').read())"]]
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "backend-calls.jsonl"
    backend = tools / "claude"
    backend.write_text(
        f"#!{sys.executable}\nimport os\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write('call\\n')\n"
        "Path('app.py').write_text('def answer():\\n    return 42\\n')\n"
        "Path('payload.bin').write_bytes(os.urandom(2 << 20))\n"
    )
    backend.chmod(0o755)
    repo.config.claude_template = f"{backend} {{prompt_file}} {{finding_id}}"
    manager.save_repo_config(repo.config)
    env = dict(os.environ, QA_AGENT_WORKSPACE=str(workspace), PYTHONPATH=str(root),
               PATH=str(tools) + os.pathsep + os.environ.get("PATH", ""))
    command = [sys.executable, str(root / "bin/bluei")]
    scan = subprocess.run([*command, "duster", "selected", "--discovery-scope", "python"],
                          env=env, cwd=root, capture_output=True, text=True, timeout=30)
    assert scan.returncode == 0, scan.stdout + scan.stderr
    state = manager.repos_dir / "selected/state"
    findings = [json.loads(line) for line in (state / "findings.jsonl").read_text().splitlines()]
    finding = next(item for item in findings if item["rule"] == "ruff-F821" and item["path"] == "app.py")
    before = (target / "app.py").read_bytes()
    result = subprocess.run([*command, "run", "selected", "--discovery-scope", "python",
                             "--finding-id", finding["finding_id"], "--fix-engine", "claude",
                             "--no-dry-run"],
                            env=env, cwd=root, capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    # The selected lane's own incomplete branch, not a crash or a scope abort.
    assert result.returncode == 1, output
    assert "failed its single model attempt" in output
    # Exactly one fake model call; the candidate itself was fine until the diff.
    assert len(calls.read_text().splitlines()) == 1, output
    metrics = json.loads((state / "status.json").read_text())["latest_run_metrics"]
    assert metrics["fix_attempts"] == metrics["claude_invocations"] == 1
    assert metrics["fixes_verified"] == 0
    assert metrics["fixes_failed_verification"] == 1
    assert metrics["run_completed"] is False
    # The production 1 MiB diff bound is the blocker — scope limits are not.
    assert "selected patch could not be retained" in metrics["blocked_reasons"]
    assert not any("scope" in reason for reason in metrics["blocked_reasons"])
    assert not list(state.rglob("*.patch"))
    issues = json.loads((state / "issues.json").read_text())["issues"]
    selected = next(issue for issue in issues if issue.get("finding_id") == finding["finding_id"])
    assert selected["status"] == "fix_failed_verification"
    # The real `git diff --binary HEAD` died on the kernel file-size limit
    # production applies via RLIMIT_FSIZE — the 1 MiB bound itself tripped.
    assert "patch retention failed" in selected["status_detail"]
    assert "git', 'diff', '--binary', 'HEAD'" in selected["status_detail"]
    assert "SIGXFSZ" in selected["status_detail"]
    assert "selected_fix_patch" not in selected
    # The oversized content never reached the main checkout or a retained artifact.
    assert (target / "app.py").read_bytes() == before
    assert (target / "payload.bin").read_bytes() == payload
    assert not subprocess.check_output(["git", "-C", str(target), "status", "--porcelain"])
