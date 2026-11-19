"""Public configured backend control; Ruff wildcard routing is not cascade proof."""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.test_ordinary_repair_loop import (
    _bluei, _env, _git, _issues, _make_corpus, _register, _state,
)


SOURCE = "def answer():\n    return missing_value\n"
CHECK = "from answer import answer\nassert answer() == 42\n"


def test_ordinary_public_cascade_uses_configured_opencode_candidate(tmp_path):
    repo = _make_corpus(tmp_path, "cascade", {
        "answer.py": SOURCE, "check_answer.py": CHECK, "requirements.txt": "",
    })
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_dir = _register(ws, "cascade", repo, [[
        sys.executable, "-c", "import ast; ast.parse(open('answer.py').read())",
    ]])
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "inert-calls.jsonl"
    backend = tools / "opencode"
    backend.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "Path('answer.py').write_text('def answer():\\n    return 42\\n')\n"
    )
    backend.chmod(0o755)
    config = config_dir / "config.yaml"
    text = config.read_text().replace("fix_engine: deterministic", "fix_engine: opencode")
    text += "opencode_template: " + json.dumps(f"{backend} {{prompt_file}} {{finding_id}}") + "\n"
    config.write_text(text)
    env = _env(ws)
    env["PATH"] = str(tools) + os.pathsep + env["PATH"]
    before = subprocess.run([sys.executable, "check_answer.py"], cwd=repo, capture_output=True)
    assert before.returncode == 1 and b"NameError" in before.stderr
    run = _bluei(env, ws, "cascade", "run", "cascade", "--fix-engine", "opencode",
                 "--no-dry-run", "--no-learning")
    assert run.returncode == 0, run.stdout + run.stderr
    state = _state(ws, "cascade")
    metrics = json.loads((state / "status.json").read_text())["latest_run_metrics"]
    assert metrics["fix_attempts"] == metrics["fixes_verified"] == 1
    assert metrics["claude_invocations"] == 1  # Legacy total-model counter, not a Claude provider claim.
    assert metrics["model_invocations_by_backend"] == {"opencode": 1}
    assert metrics["prs_created"] == 0
    assert len(calls.read_text().splitlines()) == 1
    log = (ws / "logs/cascade/run.log").read_text()
    # The generic Ruff recipe intentionally classifies this as SIMPLE_FIX;
    # this public control qualifies ordinary backend identity, not cascade.
    assert "claude-fix:" in log
    assert str(backend) in log
    matching = [issue for issue in _issues(ws, "cascade") if issue["rule"] == "ruff-f821"]
    assert len(matching) == 1 and matching[0]["status"] == "open"
    patch = Path(matching[0]["verified_fix_patch"])
    assert patch.is_file()
    independent = tmp_path / "independent"
    subprocess.run(["git", "clone", "--no-local", str(repo), str(independent)], check=True, capture_output=True)
    _git(independent, "remote", "remove", "origin")
    assert not _git(independent, "remote", "-v").stdout
    _git(independent, "apply", "--check", str(patch))
    _git(independent, "apply", str(patch))
    after = subprocess.run([sys.executable, "check_answer.py"], cwd=independent, capture_output=True)
    assert after.returncode == 0, after.stderr
    assert (repo / "answer.py").read_text() == SOURCE
