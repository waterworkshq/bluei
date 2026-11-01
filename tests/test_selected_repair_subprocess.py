"""Real public/engine/model subprocesses with an inert local backend executable."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry


@pytest.mark.parametrize("backend_name,backend_exit,missing_id,new_module", [
    ("claude", 0, False, False), ("claude", 9, False, False),
    ("claude", 0, True, False), ("opencode", 0, False, False), ("claude", 0, False, True),
    ("opencode-e2e", 0, False, False),
])
def test_selected_public_repair_is_bounded(tmp_path, backend_name, backend_exit, missing_id, new_module):
    e2e = backend_name.endswith("-e2e")
    backend_name = backend_name.removesuffix("-e2e")
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "bluei").symlink_to(root / "bluei", target_is_directory=True)
    (workspace / "plugins").symlink_to(root / "plugins", target_is_directory=True)
    target = tmp_path / "target"
    target.mkdir()
    (target / "requirements.txt").write_text("")
    (target / "app.py").write_text("def answer():\n    return missing_name\n")
    (target / "unrelated.tsx").write_text("const = ;\n")
    for args in [["init", "-q"], ["add", "."],
                 ["-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture"]]:
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)
    manager = ConfigManager(workspace)
    registry = RepoRegistry(manager)
    repo = registry.create_from_path(target, name="selected", language="python")
    repo.config.fix_engine = "claude"
    repo.config.github["live_actions"] = True  # selected lane must suppress this
    repo.config.baseline_checks = [[sys.executable, "-c", "import ast;ast.parse(open('app.py').read())"]]
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "backend-calls.jsonl"
    backend = tools / backend_name
    backend.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"if {backend_exit} == 0:\n    Path('app.py').write_text('def answer():\\n    return 42\\n')\n"
        f"if {new_module!r}:\n    Path('helper.py').write_text('answer = 42\\n')\n    Path('app.py').write_text('from helper import answer\\n')\n"
        f"raise SystemExit({backend_exit})\n"
    )
    backend.chmod(0o755)
    setattr(repo.config, backend_name + "_template", f"{backend} {{prompt_file}} {{finding_id}}")
    manager.save_repo_config(repo.config)
    registry.update("selected", {"current_health_score": 73.0, "current_findings_count": 8})
    env = dict(os.environ, QA_AGENT_WORKSPACE=str(workspace), PYTHONPATH=str(root),
               PATH=str(tools) + os.pathsep + os.environ.get("PATH", ""))
    command = [sys.executable, str(root / "bin/bluei")]
    scan = subprocess.run([*command, "duster", "selected", "--discovery-scope", "python"],
                          env=env, cwd=root, capture_output=True, text=True, timeout=30)
    assert scan.returncode == 0, scan.stdout + scan.stderr
    state = manager.repos_dir / "selected/state"
    findings = [json.loads(line) for line in (state / "findings.jsonl").read_text().splitlines()]
    finding = next(item for item in findings if item["rule"] == "ruff-F821" and item["path"] == "app.py")
    finding_id = "missing-id" if missing_id else finding["finding_id"]
    # An existing unrelated issue must not be processed or changed.
    unrelated = {"issue_id": "unrelated", "finding_id": "not-selected", "status": "open",
                 "rule": "ruff-F821", "path": "other.py", "line": 1, "confidence": 0.9,
                 "safe_to_autofix": False, "snippet": "another_missing_name"}
    (state / "issues.json").write_text(json.dumps({"issues": [unrelated]}))
    before = (target / "app.py").read_bytes()
    result = subprocess.run([*command, "run", "selected", "--discovery-scope", "python",
                             "--finding-id", finding_id, "--fix-engine", backend_name, "--no-dry-run",
                             *(["--phase", "e2e"] if e2e else [])],
                            env=env, cwd=root, capture_output=True, text=True, timeout=45)
    output = result.stdout + result.stderr
    if missing_id:
        assert result.returncode != 0, output
        assert not calls.exists()
    else:
        assert len(calls.read_text().splitlines()) == 1, output
        assert (result.returncode == 0) == (backend_exit == 0 and not new_module), output
        if backend_exit != 0 or new_module:
            failed = json.loads((state / "status.json").read_text())["latest_run_metrics"]
            assert failed["run_completed"] is False
            assert failed["fix_attempts"] == failed["claude_invocations"] == 1
            assert failed["fixes_failed_verification"] == 1
            assert failed["model_invocations_by_backend"] == {backend_name: 1}
            assert failed["cost_total_usd"] > 0 and failed["learning_mode"] == "paused"
            assert failed["blocked_reasons"] and not failed["cost_limit_reached"]
            assert not list(state.glob("*.patch"))
    issues = json.loads((state / "issues.json").read_text())["issues"]
    assert next(issue for issue in issues if issue["issue_id"] == "unrelated") == unrelated
    assert (target / "app.py").read_bytes() == before
    assert not subprocess.check_output(["git", "-C", str(target), "status", "--porcelain"])
    stored = registry.find_by_name("selected")
    assert stored.current_health_score == 73 and stored.current_findings_count == 8
    assert not (state / "health_history.jsonl").exists()
    assert not (state / "fix_patterns.jsonl").exists()
    if backend_exit == 0 and not missing_id and not new_module:
        assert "Python scope complete" in output
        patches = list(state.rglob("*.patch"))
        assert len(patches) == 1
        assert "return 42" in patches[0].read_text()
        latest = json.loads((state / "status.json").read_text())["latest_run_metrics"]
        assert latest["claude_invocations"] == 1 and latest["prs_created"] == 0
        assert latest["model_invocations_by_backend"] == {backend_name: 1}
        run_history = [json.loads(path.read_text()) for path in (state.parent / "runs").glob("*.json")]
        assert any(run["model_invocations_by_backend"] == {backend_name: 1} for run in run_history)
        selected_issue = next(issue for issue in issues if issue.get("finding_id") == finding_id)
        assert selected_issue["status"] == "open"
        assert selected_issue["selected_fix_patch"] == str(patches[0])
