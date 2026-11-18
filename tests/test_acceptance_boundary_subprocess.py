"""Acceptance-boundary public regressions (ACCEPTANCE-BOUNDARY-3ks3txvh, A02/A03).

Runs the canonical public CLI (``bin/bluei``) against an inert local backend
executable. No provider CLI, credentials, or network: the "model" is a local
script that records its argv. Harness shape follows
``tests/test_selected_repair_subprocess.py``; nothing is imported from it
because that module has no shared helpers (a single parametrized test).

Specified boundary (Stage 1, S1-03/S1-06 — A02):

- Baseline checks that are not execution-valid (required executable missing,
  required Python script missing) and an explicitly empty check list
  (``config=[]``, no implicit default/fixture checks) must fail qualification
  BEFORE any backend dispatch: no model invocation, no prompt handed out, no
  retained patch, registered source unchanged.
- The surfaced failure reason is the safe category
  (``needs-human-validation-failed``); raw private commands, secret argv, and
  benign tool stderr (e.g. ``can't open file``) must not surface in status.
- The failed attempt is a preparation attempt: ``fix_attempts == 1`` with
  zero backend invocations.
- Control: a genuine pre-existing check that DOES execute and exits 1 stays
  permitted — the backend is dispatched exactly once and the fix is retained
  (unchanged-failure policy).

Containment (Stage 1, S1-05 — A03): the ``refactor-cycle`` public route
refuses an unqualified run with ``[UNQUALIFIED] refactor-cycle`` and a
non-zero exit; no queue mutation and no backend call happen on that path.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "def answer():\n    return missing_name\n"
FIXED_SOURCE = "def answer():\n    return 42\n"
SECRET = "inline-credential-must-not-surface"
ABSENT_TOOL = "bluei-acceptance-absent-tool"
ABSENT_SCRIPT = "acceptance_absent_check.py"
SAFE_CATEGORY = "needs-human-validation-failed"
# Tokens that must never surface in the selected-lane failure status: the
# safe category hides check names, private command argv, and benign stderr.
UNSURFACEABLE = [
    SECRET,
    ABSENT_TOOL,
    ABSENT_SCRIPT,
    "No such file or directory",
    "can't open file",
    "lint_check.py",
    "test_price.py",
]


def _prepare_repo(tmp_path, backend_exit=0):
    """Workspace + registered target repo + inert backend (harness shape)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "bluei").symlink_to(ROOT / "bluei", target_is_directory=True)
    (workspace / "plugins").symlink_to(ROOT / "plugins", target_is_directory=True)
    target = tmp_path / "target"
    target.mkdir()
    (target / "requirements.txt").write_text("")
    (target / "app.py").write_text(SOURCE)
    for args in [["init", "-q"], ["add", "."],
                 ["-c", "user.name=Test", "-c", "user.email=test@localhost",
                  "commit", "-qm", "fixture"]]:
        subprocess.run(["git", "-C", str(target), *args], check=True,
                       capture_output=True)
    manager = ConfigManager(workspace)
    registry = RepoRegistry(manager)
    repo = registry.create_from_path(target, name="acceptance", language="python")
    repo.config.fix_engine = "deterministic"
    repo.config.github["live_actions"] = True
    repo.config.cooldowns["finding_seconds"] = 0
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "backend-calls.jsonl"
    captured_prompt = tmp_path / "captured-prompt.md"
    backend = tools / "claude"
    backend.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"Path({str(captured_prompt)!r}).write_text(Path(sys.argv[1]).read_text())\n"
        f"if {backend_exit} == 0:\n    Path('app.py').write_text({FIXED_SOURCE!r})\n"
        f"raise SystemExit({backend_exit})\n"
    )
    backend.chmod(0o755)
    repo.config.claude_template = f"{backend} {{prompt_file}} {{finding_id}}"
    return SimpleNamespace(workspace=workspace, target=target, manager=manager,
                           registry=registry, repo=repo, tools=tools,
                           calls=calls, captured_prompt=captured_prompt,
                           state=manager.repos_dir / "acceptance" / "state")


def _public_env(fixture, with_tools_on_path=True):
    env = dict(os.environ, QA_AGENT_WORKSPACE=str(fixture.workspace),
               PYTHONPATH=str(ROOT))
    if with_tools_on_path:
        env["PATH"] = str(fixture.tools) + os.pathsep + env.get("PATH", "")
    return env


def _selected_finding_id(fixture, env):
    command = [sys.executable, str(ROOT / "bin/bluei")]
    scan = subprocess.run([*command, "duster", "acceptance",
                           "--discovery-scope", "python"],
                          env=env, cwd=ROOT, capture_output=True, text=True,
                          timeout=30)
    assert scan.returncode == 0, scan.stdout + scan.stderr
    findings = [json.loads(line) for line in
                (fixture.state / "findings.jsonl").read_text().splitlines()]
    matching = [item for item in findings
                if item["rule"] == "ruff-f821" and item["path"] == "app.py"]
    assert len(matching) == 1
    return matching[0]["finding_id"]


def _run_selected(fixture, env, finding_id):
    return subprocess.run(
        [sys.executable, str(ROOT / "bin/bluei"), "run", "acceptance",
         "--discovery-scope", "python", "--finding-id", finding_id,
         "--fix-engine", "claude", "--no-dry-run", "--no-learning"],
        env=env, cwd=ROOT, capture_output=True, text=True, timeout=45)


def _boundary_checks(tmp_path, mode):
    if mode == "missing-executable":
        return [[ABSENT_TOOL, "--password", SECRET]]
    if mode == "missing-python-script":
        return [[sys.executable, str(tmp_path / ABSENT_SCRIPT),
                 "--password", SECRET]]
    return []  # "no-checks": explicit empty — no implicit default checks


@pytest.mark.parametrize("mode", [
    "missing-executable",
    "missing-python-script",
    "no-checks",
])
def test_unqualified_baseline_fails_before_backend_dispatch(tmp_path, mode):
    """A02: not-execution-valid or empty checks refuse the run pre-dispatch."""
    fixture = _prepare_repo(tmp_path)
    fixture.repo.config.baseline_checks = _boundary_checks(tmp_path, mode)
    fixture.manager.save_repo_config(fixture.repo.config)
    env = _public_env(fixture)
    finding_id = _selected_finding_id(fixture, env)
    before = (fixture.target / "app.py").read_bytes()

    result = _run_selected(fixture, env, finding_id)
    output = result.stdout + result.stderr

    # Parent decision: qualification fails the run before backend dispatch.
    assert result.returncode != 0, output
    assert not fixture.calls.exists(), output
    assert not fixture.captured_prompt.exists()
    assert SECRET not in output
    # No retained patch, learning store untouched, source unchanged.
    assert not list(fixture.state.rglob("*.patch"))
    assert not (fixture.state / "fix_patterns.jsonl").exists()
    assert (fixture.target / "app.py").read_bytes() == before
    assert not subprocess.check_output(
        ["git", "-C", str(fixture.target), "status", "--porcelain"])

    status = json.loads((fixture.state / "status.json").read_text())
    failed = status["latest_run_metrics"]
    error = status["run_interruption"]["error"]
    # Surfaced reason is the safe category; details stay out of status.
    assert error == failed["failure_reason"]
    assert SAFE_CATEGORY in error
    for token in UNSURFACEABLE:
        assert token not in error, token
    # Preparation attempt only: one fix attempt, zero model calls.
    assert failed["fix_attempts"] == 1
    assert failed["claude_invocations"] == 0
    assert not any(failed.get("model_invocations_by_backend", {}).values())
    assert failed["fixes_failed_verification"] == 1
    assert status["current_counts"]["by_status"]["fix_failed_verification"] == 1
    issues = json.loads((fixture.state / "issues.json").read_text())["issues"]
    selected = next(issue for issue in issues
                    if issue.get("finding_id") == finding_id)
    assert selected["status"] == "fix_failed_verification"


def test_genuine_preexisting_rc1_check_stays_permitted(tmp_path):
    """A02 control: an executed pre-existing rc!=0 check does not block."""
    fixture = _prepare_repo(tmp_path, backend_exit=0)
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "genuinely_failing.py").write_text("raise SystemExit(1)\n")
    fixture.repo.config.baseline_checks = [
        [sys.executable, str(checks / "genuinely_failing.py")],
        [sys.executable, "-c", "import ast; from pathlib import Path; ast.parse(Path('app.py').read_text())"],
    ]
    fixture.manager.save_repo_config(fixture.repo.config)
    env = _public_env(fixture)
    finding_id = _selected_finding_id(fixture, env)
    before = (fixture.target / "app.py").read_bytes()

    result = _run_selected(fixture, env, finding_id)
    output = result.stdout + result.stderr

    # Policy allows genuine unchanged failures: backend dispatched exactly once.
    assert result.returncode == 0, output
    assert fixture.calls.exists(), output
    assert len(fixture.calls.read_text().splitlines()) == 1
    patches = list(fixture.state.rglob("*.patch"))
    assert len(patches) == 1
    assert "return 42" in patches[0].read_text()
    assert (fixture.target / "app.py").read_bytes() == before
    issues = json.loads((fixture.state / "issues.json").read_text())["issues"]
    selected = next(issue for issue in issues
                    if issue.get("finding_id") == finding_id)
    assert selected["status"] == "open"
    assert selected["selected_fix_patch"] == str(patches[0])


def test_refactor_cycle_public_route_unqualified_no_backend(tmp_path, make_finding):
    """An unauthorized live refactor run stops before backend/queue effects."""
    from bluei.engine.reforge import RefactorWork
    from bluei.engine.refactor_queue import QueueStatus, RefactorQueue

    fixture = _prepare_repo(tmp_path)
    fixture.repo.config.baseline_checks = [[ABSENT_TOOL, "--check", SECRET]]
    fixture.manager.save_repo_config(fixture.repo.config)
    env = _public_env(fixture, with_tools_on_path=True)

    # A pending human-review work item that must not be mutated or executed.
    finding = make_finding(finding_id="rw-acceptance-boundary",
                           repo="acceptance", path="app.py", rule="xo-max-lines",
                           safe_to_autofix=False)
    queue = RefactorQueue()
    entry = queue.enqueue(finding, RefactorWork(finding_id=finding.finding_id))
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin/bluei"), "run", "acceptance",
             "--phase", "refactor-cycle", "--fix-engine", "claude",
             "--no-dry-run", "--no-learning"],
            env=env, cwd=ROOT, capture_output=True, text=True, timeout=45)
        output = result.stdout + result.stderr
        # Engine output is also mirrored into the run logs; accept either.
        logs = "".join(path.read_text(errors="replace")
                       for path in sorted((fixture.workspace / "logs"
                                           / "acceptance").glob("*.log")))

        assert result.returncode != 0, output
        assert "[ABORT] live mode requires a GitHub origin remote" in output + logs
        assert "[DONE] refactor-cycle" not in output + logs
        assert SECRET not in output
        # No backend call (inert claude is on PATH), no queue mutation.
        assert not fixture.calls.exists(), output
        assert queue.get(entry.work_id).status == QueueStatus.PENDING_REVIEW
        for category in ("approved", "executing", "completed"):
            links = list((queue.queue_dir / category).glob("*.link"))
            assert entry.work_id not in {link.stem for link in links}
        assert not subprocess.check_output(
            ["git", "-C", str(fixture.target), "status", "--porcelain"])
    finally:
        for category in ("finding_work", "pending_review", "approved",
                         "executing", "completed", "aborted"):
            path = queue.queue_dir / category / (
                f"{entry.work_id}.json" if category == "finding_work"
                else f"{entry.work_id}.link")
            if path.exists():
                path.unlink()
