"""Public Python-scope contracts, including an actual discovery subprocess."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from bin import bluei as cli
from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry
from bluei.app.runner import RunEngine, RunOptions
from bluei.app.state import StateManager
from bluei.app.health import HealthEngine


@pytest.mark.parametrize("extra", [
    ["--discovery-scope", "other"],
    ["--finding-id", "f-1"],
    ["--discovery-scope", "python", "--finding-id", "f-1", "--fix-engine", "deterministic"],
    ["--discovery-scope", "python", "--finding-id", "f-1", "--fix-engine", "claude", "--live-github-actions"],
    ["--discovery-scope", "python", "--run-phase", "verify-only"],
])
def test_engine_rejects_invalid_scope_selection(monkeypatch, extra):
    from bluei.engine.commands.parse_args import parse_args
    monkeypatch.setattr(sys, "argv", ["bluei", *extra])
    with pytest.raises(SystemExit) as exc:
        parse_args()
    assert exc.value.code == 2


def test_selected_engine_arguments_force_one_attempt_no_learning(monkeypatch):
    from bluei.engine.commands.parse_args import parse_args
    monkeypatch.setattr(sys, "argv", ["bluei", "--discovery-scope", "python", "--finding-id", "f-1",
                                      "--fix-engine", "claude", "--pattern-store-path", "/ignored/patterns"])
    args = parse_args()
    assert args.max_prs_per_run == args.max_issues_per_run == args.max_fix_attempts_per_issue == 1
    assert not args.batch_pr_enabled and args.no_learning and args.pattern_store_path is None


def test_runner_selected_arguments_suppress_live_and_learning(tmp_path):
    manager = ConfigManager(tmp_path / "workspace")
    registry = RepoRegistry(manager)
    target = tmp_path / "target"
    target.mkdir()
    repo = registry.create_from_path(target, name="selected", language="python")
    repo.config.github.update(live_actions=True, auto_merge=True, auto_rebase={"enabled": True})
    engine = RunEngine(registry, StateManager(manager.repos_dir), HealthEngine(), manager)
    options = RunOptions(discovery_scope="python", finding_id="f-1", fix_engine="claude")
    args = engine._build_cli_args(repo, options, {"logical_backend": "claude", "runner_engine": "claude", "template": "fake {prompt_file}"})
    assert args[args.index("--discovery-scope") + 1] == "python"
    assert args[args.index("--finding-id") + 1] == "f-1"
    assert "--no-learning" in args
    assert not {"--live-github-actions", "--auto-merge-sandbox", "--auto-rebase-enabled", "--pattern-store-path"} & set(args)


def test_generated_selected_command_preserves_scope_and_no_merge(monkeypatch):
    from bluei.engine.commands.parse_args import parse_args
    from bluei.engine.command_builder import build_orchestrated_cycle_command
    monkeypatch.setattr(sys, "argv", ["bluei", "--discovery-scope", "python", "--finding-id", "f-1",
                                      "--fix-engine", "claude"])
    command = build_orchestrated_cycle_command(parse_args())
    assert "--discovery-scope python" in command and "--finding-id f-1" in command
    assert "--no-learning" in command and "--auto-merge-sandbox" not in command


def test_actual_public_python_discovery_preserves_repository_health(tmp_path, monkeypatch, capsys):
    from bluei.app import config as config_module
    from bluei.app.runner import RunEngine

    workspace = tmp_path / "workspace"
    root = Path(__file__).resolve().parents[1]
    workspace.mkdir()
    # Real engine source, not a fabricated completion child.
    (workspace / "bluei").symlink_to(root / "bluei", target_is_directory=True)
    (workspace / "plugins").symlink_to(root / "plugins", target_is_directory=True)
    (workspace / "tests").symlink_to(root / "tests", target_is_directory=True)
    target = tmp_path / "target"
    target.mkdir()
    (target / "app.py").write_text("def run():\n    return unknown_name\n")
    (target / "requirements.txt").write_text("")
    (target / "broken.tsx").write_text("const = ;\n")
    (target / "README.md").write_text("unformatted text \n")
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "add", "."], check=True)
    subprocess.run(["git", "-C", str(target), "-c", "user.name=Test", "-c", "user.email=test@localhost",
                    "commit", "-qm", "fixture"], check=True)
    manager = ConfigManager(workspace)
    registry = RepoRegistry(manager)
    repo = registry.create_from_path(target, name="python-only", language="python")
    repo.config.fix_engine = "deterministic"
    repo.config.github["live_actions"] = False
    manager.save_repo_config(repo.config)
    registry.update("python-only", {"current_health_score": 73.0, "current_findings_count": 5})
    state = manager.repos_dir / "python-only/state"
    history = state / "health_history.jsonl"
    history.write_text('{"score": 73}\n')
    monkeypatch.setattr(config_module, "WORKSPACE", workspace)
    original_init = RunEngine.__init__

    def real_engine(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.runner_path = root / "bluei/engine/cli.py"

    monkeypatch.setattr(RunEngine, "__init__", real_engine)
    code = cli._cmd_run("python-only", ["--phase", "issue-cycle", "--dry-run", "--discovery-scope", "python"])
    output = capsys.readouterr()
    assert code == 0, (output.out, output.err)
    assert "Python scope complete; repository-wide health unchanged." in output.out
    run = json.loads(next((manager.repos_dir / "python-only/runs").glob("*.json")).read_text())
    assert run["discovery_scope"] == "python" and run["scan_complete"] is True
    assert run["health_before"] == run["health_after"] == 73.0 and run["health_delta"] == 0
    stored = registry.find_by_name("python-only")
    assert stored.current_health_score == 73 and stored.current_findings_count == 5
    assert history.read_text() == '{"score": 73}\n'
    assert not (workspace / "state/health_trend.jsonl").exists()
    current = json.loads((state / "discovery-current.json").read_text())
    assert current["discovery_scope"] == "python"
    assert current["detector_outcomes"]["ast-python"]["status"] == "ran"
    assert current["detector_outcomes"]["ast-typescript"]["status"] == "skipped"
    findings = [json.loads(line) for line in (state / "findings.jsonl").read_text().splitlines()]
    assert any(item["rule"] == "ruff-F821" and item["path"] == "app.py" for item in findings)
    assert all(item["path"].endswith(".py") for item in findings)
