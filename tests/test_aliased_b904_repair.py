"""Public repair of a B904 finding whose handler already binds the cause."""
import json
import subprocess
import sys

import pytest

from tests.test_ordinary_repair_loop import (
    AST_SYNTAX_CHECK,
    _git,
    _make_corpus,
    _patches,
    _register,
    _repair_run,
)


def test_repair_reuses_existing_exception_alias(tmp_path):
    source = '''\
def parse_port(raw):
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid port: {exc}")  # preserve this comment
'''
    check = '''\
import sys

sys.dont_write_bytecode = True
from parser_util import parse_port

assert parse_port("443") == 443
try:
    parse_port("bad")
except RuntimeError as exc:
    assert str(exc).startswith("invalid port: ")
    assert isinstance(exc.__cause__, ValueError)
else:
    raise AssertionError("expected RuntimeError")
'''
    repo = _make_corpus(tmp_path, "ports", {"parser_util.py": source, "check.py": check})
    ws = tmp_path / "workspace"
    ws.mkdir()
    _register(ws, "ports", repo, [
        ["python3", "-c", AST_SYNTAX_CHECK, "parser_util.py"],
        ["python3", "check.py"],
    ])
    run = _repair_run(ws, "ports", "aliased")
    assert run.returncode == 0, run.stdout + run.stderr
    patches = _patches(ws, "ports")
    assert len(patches) == 1, run.stdout + run.stderr
    assert (repo / "parser_util.py").read_text() == source

    independent = _make_corpus(tmp_path, "independent", {"parser_util.py": source, "check.py": check})
    _git(independent, "apply", str(patches[0]))
    assert _git(independent, "diff", "--name-only").stdout.splitlines() == ["parser_util.py"]
    repaired = (independent / "parser_util.py").read_text()
    assert repaired == source.replace('exc}")  #', 'exc}") from exc  #')
    subprocess.run([sys.executable, "-B", "check.py"], cwd=independent, check=True, timeout=30)
    detector = subprocess.run(
        ["ruff", "check", "--isolated", "--no-cache", "--select", "B904", "--output-format=json", "parser_util.py"],
        cwd=independent, capture_output=True, text=True, check=True, timeout=30,
    )
    assert json.loads(detector.stdout) == []


@pytest.mark.parametrize("body, reject_edit", [
    ('        exc = None\n        raise RuntimeError("invalid port")\n', False),
    ('        if raw:\n            raise RuntimeError("invalid port")\n', False),
    ('        raise RuntimeError(\n            "invalid port",\n        )\n', False),
    ('        raise RuntimeError(str(exc := None))\n', False),
    ('        raise RuntimeError("invalid port")\n', True),
], ids=["rebound", "nested", "multiline", "assignment-expression", "validation-veto"])
def test_aliased_b904_retains_no_patch_when_ineligible(tmp_path, body, reject_edit):
    source = (
        'def parse_port(raw):\n'
        '    try:\n        return int(raw)\n'
        '    except ValueError as exc:\n' + body
    )
    repo = _make_corpus(tmp_path, "ports", {"parser_util.py": source})
    ws = tmp_path / "workspace"
    ws.mkdir()
    checks = [["python3", "-c", AST_SYNTAX_CHECK, "parser_util.py"]]
    if reject_edit:
        checks.append(["python3", "-c", "assert 'from exc' not in open('parser_util.py').read()"])
    _register(ws, "ports", repo, checks)

    run = _repair_run(ws, "ports", "refused")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "fix_attempts=1 fixes_verified=0 fixes_failed_verification=1" in run.stdout
    assert not _patches(ws, "ports")
    assert (repo / "parser_util.py").read_text() == source
