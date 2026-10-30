"""Native failed-child acceptance for public reporting, without mocked run results."""

import json
from pathlib import Path

import pytest

from bin import bluei as cli
from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry


@pytest.mark.parametrize("child_exit", [0, 7])
def test_public_scan_rejects_missing_current_completion(tmp_path, monkeypatch, capsys, child_exit):
    workspace = tmp_path / "workspace"
    child = workspace / "bluei/engine/cli.py"
    child.parent.mkdir(parents=True)
    child.write_text(f'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
def option(name): return Path(args[args.index(name)+1])
status = option('--status-file')
status.parent.mkdir(parents=True, exist_ok=True)
(status.parent / 'discovery-current.json').write_text(json.dumps({{
    'run_id': os.environ['BLUEI_RUN_ID'], 'findings_detected': 3,
    'detector_outcomes': {{'python-linter': {{'status': 'ran', 'count': 3}}}}
}}))
# No new history lines: repeated scans may be entirely deduplicated.
print('partial child output, no completion marker')
raise SystemExit({child_exit})
''')
    manager = ConfigManager(workspace)
    registry = RepoRegistry(manager)
    target = tmp_path / "target"
    target.mkdir()
    repo = registry.create_from_path(target, name="acceptance", language="python")
    repo.config.fix_engine = "deterministic"
    repo.config.github["live_actions"] = False
    manager.save_repo_config(repo.config)
    registry.update("acceptance", {"current_health_score": 73.0})
    state = manager.repos_dir / "acceptance/state"
    (state / "status.json").write_text(json.dumps({
        "last_run_completed": True,
        "latest_run_metrics": {"run_id": "stale-run", "run_completed": True,
                               "findings_detected": 0},
        "health_score": 73.0,
    }))
    from bluei.app import config as config_module
    from bluei.app.runner import RunEngine
    original_init = RunEngine.__init__
    def interrupted_engine(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.runner_path = child
    monkeypatch.setattr(RunEngine, "__init__", interrupted_engine)
    monkeypatch.setattr(config_module, "WORKSPACE", workspace)
    code = cli._cmd_duster("acceptance")
    output = capsys.readouterr()
    assert code == 1, (output.out, output.err, cli.__file__)
    assert "incomplete" in (output.out + output.err).lower()
    assert "3" in output.out
    assert "Findings: 0" not in output.out
    stored = registry.find_by_name("acceptance")
    assert stored.current_health_score == 73.0
    status = json.loads((state / "status.json").read_text())
    assert status["last_run_completed"] is False
    assert status["latest_run_metrics"]["run_completed"] is False
    records = list((manager.repos_dir / "acceptance/runs").glob("*.json"))
    assert len(records) == 1
    run = json.loads(records[0].read_text())
    assert run["scan_complete"] is False
    assert run["findings_detected"] == 3


@pytest.mark.parametrize("phase,expected_success", [("issue-cycle", False), ("pr-cycle", True), ("merge-cycle", True)])
def test_skipped_scan_is_not_new_health_evidence(tmp_path, phase, expected_success):
    from bluei.app.health import HealthEngine
    from bluei.app.runner import RunEngine, RunOptions
    from bluei.app.state import StateManager
    manager = ConfigManager(tmp_path / "workspace")
    registry = RepoRegistry(manager)
    target = tmp_path / "target"
    target.mkdir()
    repo = registry.create_from_path(target, name="skipped", language="python")
    repo.config.fix_engine = "deterministic"
    manager.save_repo_config(repo.config)
    registry.update("skipped", {"current_health_score": 73.0})
    repo = registry.find_by_name("skipped")
    state = StateManager(manager.repos_dir)
    child = tmp_path / "skipped_engine.py"
    child.write_text('''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
path = Path(args[args.index('--status-file')+1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({'latest_run_metrics': {
    'run_id': os.environ['BLUEI_RUN_ID'], 'run_completed': True,
    'findings_detected': 0,
    'detector_outcomes': {'discovery': {'status': 'skipped', 'count': 0, 'reason': 'cap or phase'}}
}}))
print('[DONE] findings=0')
''')
    engine = RunEngine(registry, state, HealthEngine(), manager)
    engine.runner_path = child
    result = engine.run(repo, RunOptions(phase=phase, dry_run=True, fix_engine="deterministic"))
    assert result.success is expected_success
    assert result.run.scan_complete is False
    assert registry.find_by_name("skipped").current_health_score == 73.0
    assert not (state._get_state_dir("skipped") / "health_history.jsonl").exists()


def test_verify_only_does_not_resolve_issues_when_every_detector_skipped(tmp_path):
    from types import SimpleNamespace
    from bluei.engine.commands.verify_only import run_verify_only
    from bluei.engine.commands.parse_args import parse_args
    import sys
    previous = sys.argv
    try:
        sys.argv = ["bluei", "--repo-path", str(tmp_path), "--run-phase", "verify-only"]
        args = parse_args()
    finally:
        sys.argv = previous
    issues = {"issues": [{"issue_id": "i1", "rule": "ruff-F821", "path": "app.py", "status": "open"}]}
    path = tmp_path / "issues.json"
    path.write_text(json.dumps(issues))
    result = run_verify_only(
        findings=[], issues_data=issues, issues_file=path, state_file=tmp_path / "state.json",
        state={}, status_file=tmp_path / "status.json", findings_file=tmp_path / "findings.jsonl",
        log_file=tmp_path / "run.log", args=args, reconcile_event={}, previous_last_run_at=None,
        open_issues=1, open_prs=0, written_findings=0, suppressed_findings=[], blocked_reasons=[],
        detector_outcomes={"internal": {"status": "skipped", "count": 0}},
    )
    assert result == 1
    assert json.loads(path.read_text()) == issues
    assert issues["issues"][0]["status"] == "open"


def test_native_engine_partial_scan_clears_stale_success(tmp_path):
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / "empty-repo"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"last_run_completed": True, "health_score": 73,
        "latest_run_metrics": {"run_id": "stale", "run_completed": True}}))
    env = dict(os.environ, PYTHONPATH=str(root), BLUEI_RUN_ID="native-partial",
               QA_AGENT_WORKSPACE=str(tmp_path / "workspace"))
    env["--skip-internal-discovery"] = "1"
    args = [sys.executable, "-m", "bluei.engine.cli", "--repo-path", str(target),
            "--run-phase", "verify-only", "--status-file", str(status),
            "--simulate-open-issues", "0", "--simulate-open-prs", "0"]
    for flag, name in [("--state-file", "state.json"), ("--log-file", "run.log"),
                       ("--findings-file", "findings.jsonl"), ("--issues-file", "issues.json"),
                       ("--docs-index-file", "docs.json"), ("--lessons-file", "lessons.md"),
                       ("--worktree-root", "worktrees")]:
        args.extend([flag, str(tmp_path / name)])
    result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1, result.stdout + result.stderr
    stored = json.loads(status.read_text())
    assert stored["health_score"] == 73
    assert stored["last_run_completed"] is False
    assert stored["latest_run_metrics"]["run_id"] == "native-partial"
    assert stored["latest_run_metrics"]["run_completed"] is False
