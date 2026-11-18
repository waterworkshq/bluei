"""Canonical public batch commands with real discovery and exact retained bytes."""

import json
import subprocess
import sys

from tests.test_ordinary_repair_loop import (
    ROOT, _bluei, _env, _git, _issues, _make_corpus, _register, _state,
)


SOURCE = '''def parse_first(raw):
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError("first invalid")


def parse_second(raw):
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError("second invalid")
'''

CHECK = '''import sys
sys.dont_write_bytecode = True
from quantities import parse_first, parse_second
for function, message in [(parse_first, "first invalid"), (parse_second, "second invalid")]:
    assert function("42") == 42
    try:
        function("invalid")
    except RuntimeError as exc:
        assert str(exc) == message
        assert isinstance(exc.__cause__, ValueError)
    else:
        raise AssertionError("expected error")
'''


def _fixture(tmp_path, checks=True):
    repo = _make_corpus(tmp_path, "quantities", {"quantities.py": SOURCE, "check_cause.py": CHECK})
    ws = tmp_path / "workspace"
    ws.mkdir()
    baseline = [[sys.executable, "-c", "import ast; ast.parse(open('quantities.py').read())"]] if checks else []
    _register(ws, "quantities", repo, baseline)
    return repo, ws, _env(ws)


def _batch_run(ws, env, label):
    return _bluei(env, ws, label, "run", "quantities", "--fix-engine", "deterministic",
                  "--no-dry-run", "--no-learning", "--batch-pr-enabled")


def test_public_local_batch_retains_all_members_without_publication(tmp_path):
    repo, ws, env = _fixture(tmp_path)
    before = subprocess.run([sys.executable, "check_cause.py"], cwd=repo, capture_output=True)
    assert before.returncode == 1
    result = _batch_run(ws, env, "batch")
    assert result.returncode == 0, result.stdout + result.stderr
    status = json.loads((_state(ws, "quantities") / "status.json").read_text())
    metrics = status["latest_run_metrics"]
    assert metrics["fix_attempts"] == 2
    assert metrics["deterministic_invocations"] == 2
    assert metrics["claude_invocations"] == 0
    assert metrics["fixes_verified"] == 2
    assert metrics["prs_created"] == 0
    issues = [item for item in _issues(ws, "quantities") if item["rule"] == "ruff-b904"]
    assert len(issues) == 2
    artifacts = {item["verified_fix_patch"] for item in issues}
    assert len(artifacts) == 1
    from pathlib import Path

    patch = Path(artifacts.pop())
    assert patch.is_file() and patch.stat().st_size > 0
    assert all(item["status"] == "open" for item in issues)
    assert (repo / "quantities.py").read_text() == SOURCE
    assert not _git(repo, "status", "--porcelain").stdout
    independent = tmp_path / "independent"
    subprocess.run(["git", "clone", "--no-local", str(repo), str(independent)], check=True, capture_output=True)
    _git(independent, "remote", "remove", "origin")
    assert not _git(independent, "remote", "-v").stdout
    _git(independent, "apply", "--check", str(patch))
    _git(independent, "apply", str(patch))
    after = subprocess.run([sys.executable, "check_cause.py"], cwd=independent, capture_output=True)
    assert after.returncode == 0, after.stderr
    assert _git(independent, "diff", "--name-only").stdout.splitlines() == ["quantities.py"]
    repeat = _batch_run(ws, env, "repeat")
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    repeated = json.loads((_state(ws, "quantities") / "status.json").read_text())["latest_run_metrics"]
    assert repeated["fix_attempts"] == 0
    assert repeated["fixes_verified"] == 0
    assert {item["verified_fix_patch"] for item in _issues(ws, "quantities") if item["rule"] == "ruff-b904"} == {str(patch)}


def test_public_batch_without_checks_refuses_before_repair(tmp_path):
    repo, ws, env = _fixture(tmp_path, checks=False)
    result = _batch_run(ws, env, "unqualified")
    assert "unqualified" in result.stdout.lower() + result.stderr.lower()
    status = json.loads((_state(ws, "quantities") / "status.json").read_text())
    metrics = status["latest_run_metrics"]
    assert metrics["fixes_verified"] == 0
    assert metrics["claude_invocations"] == 0
    assert metrics["prs_created"] == 0
    assert (repo / "quantities.py").read_text() == SOURCE
    assert not list(_state(ws, "quantities").rglob("*.patch"))
