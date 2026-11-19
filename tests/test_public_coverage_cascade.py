"""Public contextual coverage repair; supplied reports are not coverage-meter proof."""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.test_ordinary_repair_loop import _bluei, _env, _git, _issues, _make_corpus, _register, _state


def test_public_coverage_candidate_uses_configured_cascade(tmp_path):
    report = {"answer.py": {"statementMap": {"0": {"start": {"line": 2}}}, "s": {"0": 0}}}
    source = "def answer():\n    return 42\n"
    repo = _make_corpus(tmp_path, "coverage-lib", {
        "answer.py": source, "exercise.py": "# No behavior check executed yet.\n",
        "coverage/coverage-final.json": json.dumps(report),
    })
    ws = tmp_path / "workspace"
    ws.mkdir()
    config_dir = _register(ws, "coverage-lib", repo, [[sys.executable, "-c", "import ast; ast.parse(open('answer.py').read())"]])
    tools = tmp_path / "tools"
    tools.mkdir()
    backend = tools / "opencode"
    calls = tmp_path / "calls.jsonl"
    backend.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path.cwd()))\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write('inert\\n')\n"
        "Path('exercise.py').write_text('from answer import answer\\nassert answer() == 42\\n')\n"
        "exec(compile(Path('exercise.py').read_text(), 'exercise.py', 'exec'))\n"
        "path = Path('coverage/coverage-final.json')\n"
        "report = json.loads(path.read_text())\nreport['answer.py']['s']['0'] = 1\n"
        "path.write_text(json.dumps(report))\n"
    )
    backend.chmod(0o755)
    path = config_dir / "config.yaml"
    path.write_text(path.read_text().replace("fix_engine: deterministic", "fix_engine: opencode")
                    + "opencode_template: " + json.dumps(f"{backend} {{prompt_file}} {{finding_id}}") + "\n")
    env = _env(ws)
    env["PATH"] = str(tools) + os.pathsep + env["PATH"]
    run = _bluei(env, ws, "coverage", "run", "coverage-lib", "--fix-engine", "opencode",
                 "--no-dry-run", "--no-learning")
    assert run.returncode == 0, run.stdout + run.stderr
    log = (ws / "logs/coverage-lib/run.log").read_text()
    # Existing context rules route this diagnostic to contextual model repair,
    # not cascade. The real routing result is part of this public control.
    assert "claude-fix:" in log and str(backend) in log
    metrics = json.loads((_state(ws, "coverage-lib") / "status.json").read_text())["latest_run_metrics"]
    assert metrics["fix_attempts"] == metrics["fixes_verified"] == 1
    assert metrics["model_invocations_by_backend"] == {"opencode": 1}
    assert metrics["prs_created"] == 0
    assert len(calls.read_text().splitlines()) == 1
    matching = [item for item in _issues(ws, "coverage-lib") if item["rule"] == "test-coverage-line"]
    assert len(matching) == 1
    patch = Path(matching[0]["verified_fix_patch"])
    assert patch.is_file()
    independent = tmp_path / "independent"
    subprocess.run(["git", "clone", "--no-local", str(repo), str(independent)], check=True, capture_output=True)
    _git(independent, "remote", "remove", "origin")
    _git(independent, "apply", "--check", str(patch))
    _git(independent, "apply", str(patch))
    check = subprocess.run([sys.executable, "exercise.py"], cwd=independent, capture_output=True)
    assert check.returncode == 0, check.stderr
    assert "assert answer() == 42" in (independent / "exercise.py").read_text()
    assert (repo / "answer.py").read_text() == source
    assert "No behavior check" in (repo / "exercise.py").read_text()
    # Coverage is a supplied synthetic report, not an independently metered
    # test runner. This proves public routing/context/artifact composition,
    # not genuine coverage improvement or autonomous model efficacy.
