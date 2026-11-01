"""Real public/engine/model subprocesses with an inert local backend executable."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry


@pytest.mark.parametrize("backend_name,backend_exit,missing_id,new_module,baseline_exit,rule,selected", [
    ("claude", 0, False, False, 0, "ruff-F821", True),
    ("claude", 9, False, False, 0, "ruff-F821", True),
    ("claude", 0, True, False, 0, "ruff-F821", True),
    ("opencode", 0, False, False, 0, "ruff-F821", True),
    ("claude", 0, False, True, 0, "ruff-F821", True),
    ("opencode-e2e", 0, False, False, 0, "ruff-F821", True),
    ("claude", 0, False, False, 1, "ruff-F821", True),
    ("claude", 0, False, False, 0, "lock-self-reentry", True),
    ("opencode", 0, False, False, 0, "lock-self-reentry", True),
    ("claude", 9, False, False, 0, "lock-self-reentry", True),
    ("claude", 0, True, False, 0, "lock-self-reentry", True),
    ("claude", 0, False, False, 0, "lock-self-reentry", False),
])
def test_selected_public_repair_is_bounded(tmp_path, backend_name, backend_exit, missing_id, new_module, baseline_exit, rule, selected):
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
    source = "def answer():\n    return missing_name\n"
    fixed_source = "def answer():\n    return 42\n"
    if rule == "lock-self-reentry":
        source = (
            "import threading\n\nclass Monitor:\n"
            "    def __init__(self):\n        self.guard = threading.Lock()\n"
            "    def dashboard(self):\n        with self.guard:\n            return self.snapshot()\n"
            "    def snapshot(self):\n        with self.guard:\n            return 42\n"
        )
        fixed_source = source.replace("threading.Lock()", "threading.RLock()")
    (target / "app.py").write_text(source)
    if selected:
        (target / "unrelated.tsx").write_text("const = ;\n")
    for args in [["init", "-q"], ["add", "."],
                 ["-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture"]]:
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)
    manager = ConfigManager(workspace)
    registry = RepoRegistry(manager)
    repo = registry.create_from_path(target, name="selected", language="python")
    repo.config.fix_engine = "claude"
    repo.config.github["live_actions"] = selected  # selected lane must suppress this
    check_calls = tmp_path / "check-calls.jsonl"
    check_code = (
        "import ast; from pathlib import Path; "
        "ast.parse(Path('app.py').read_text()); "
        f"with_log = Path({str(check_calls)!r}); "
        "stream = with_log.open('a'); stream.write('checked\\n'); stream.close(); "
        f"raise SystemExit({baseline_exit})"
    )
    secret = "inline-credential-must-not-reach-model"
    repo.config.baseline_checks = [[sys.executable, "-c", check_code, "--password", secret]]
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "backend-calls.jsonl"
    captured_prompt = tmp_path / "captured-prompt.md"
    backend = tools / backend_name
    backend.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"Path({str(captured_prompt)!r}).write_text(Path(sys.argv[1]).read_text())\n"
        f"if {backend_exit} == 0:\n    Path('app.py').write_text({fixed_source!r})\n"
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
    matching = [item for item in findings if item["rule"] == rule and item["path"] == "app.py"]
    assert len(matching) == 1
    finding = matching[0]
    if rule == "lock-self-reentry":
        assert not finding["safe_to_autofix"]
    finding_id = "missing-id" if missing_id else finding["finding_id"]
    # An existing unrelated issue must not be processed or changed.
    unrelated = {"issue_id": "unrelated", "finding_id": "not-selected", "status": "open",
                 "rule": "ruff-F821", "path": "other.py", "line": 1, "confidence": 0.9,
                 "safe_to_autofix": False, "snippet": "another_missing_name"}
    (state / "issues.json").write_text(json.dumps({"issues": [unrelated] if selected else []}))
    before = (target / "app.py").read_bytes()
    result = subprocess.run([*command, "run", "selected", "--discovery-scope", "python" if selected else "repository",
                             *(["--finding-id", finding_id] if selected else []),
                             "--fix-engine", backend_name, "--no-dry-run", "--no-learning",
                             *(["--phase", "e2e"] if e2e else [])],
                            env=env, cwd=root, capture_output=True, text=True, timeout=45)
    output = result.stdout + result.stderr
    if missing_id or not selected:
        if missing_id:
            assert result.returncode != 0, output
        else:
            assert result.returncode == 0, output
        assert not calls.exists()
    else:
        assert calls.exists(), output
        assert len(calls.read_text().splitlines()) == 1, output
        from bluei.engine.utils import command_list_to_shell
        prompt = captured_prompt.read_text()
        assert command_list_to_shell(repo.config.baseline_checks[0]) not in prompt
        assert secret not in prompt and str(check_calls) not in prompt
        assert "lint_check.py" not in prompt and "test_price.py" not in prompt
        assert "Baseline check 1" in prompt
        assert ("pre-fix: failed" if baseline_exit else "pre-fix: passed") in prompt
        assert "engine reruns the private configured commands" in prompt
        assert "unchanged failures" in prompt
        assert "fail (non-zero exit) if they do not pass" not in prompt
        assert len(check_calls.read_text().splitlines()) >= 2
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
    if selected:
        assert next(issue for issue in issues if issue["issue_id"] == "unrelated") == unrelated
    else:
        lock_issue = next(issue for issue in issues if issue.get("finding_id") == finding_id)
        assert lock_issue["status"] == "needs-human-not-fixable"
        latest = json.loads((state / "status.json").read_text())["latest_run_metrics"]
        assert latest["fix_attempts"] == latest["claude_invocations"] == latest["prs_created"] == 0
    assert (target / "app.py").read_bytes() == before
    assert not subprocess.check_output(["git", "-C", str(target), "status", "--porcelain"])
    stored = registry.find_by_name("selected")
    if selected:
        assert stored.current_health_score == 73 and stored.current_findings_count == 8
        assert not (state / "health_history.jsonl").exists()
    assert not (state / "fix_patterns.jsonl").exists()
    if selected and backend_exit == 0 and not missing_id and not new_module:
        assert "Python scope complete" in output
        patches = list(state.rglob("*.patch"))
        assert len(patches) == 1
        assert ("threading.RLock()" if rule == "lock-self-reentry" else "return 42") in patches[0].read_text()
        latest = json.loads((state / "status.json").read_text())["latest_run_metrics"]
        assert latest["claude_invocations"] == 1 and latest["prs_created"] == 0
        assert latest["model_invocations_by_backend"] == {backend_name: 1}
        run_history = [json.loads(path.read_text()) for path in (state.parent / "runs").glob("*.json")]
        assert any(run["model_invocations_by_backend"] == {backend_name: 1} for run in run_history)
        selected_issue = next(issue for issue in issues if issue.get("finding_id") == finding_id)
        assert selected_issue["status"] == "open"
        assert selected_issue["selected_fix_patch"] == str(patches[0])
