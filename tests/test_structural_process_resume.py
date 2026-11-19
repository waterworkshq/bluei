"""An actual killed public structural process resumes the same approved item."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.test_structural_continuation import (
    AFTER_A, BEFORE_A, CHECK_A, SYNTAX_CHECK, _backend_calls, _entries,
    _env_with_tools, _make_backend, _read_finding, _register_struct, _run_refactor,
)
from tests.test_ordinary_repair_loop import BLUEI_CLI, ROOT, _bluei, _make_corpus, _state


def test_killed_public_process_resumes_same_approved_item(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    corpus = _make_corpus(tmp_path, "resume", {
        "pyproject.toml": "", "price.py": "", "task_a.py": BEFORE_A,
        "check_behavior.py": CHECK_A,
    })
    backend = _make_backend(tmp_path, AFTER_A, "task_a.py")
    sentinel = tmp_path / "backend-entered"
    release = tmp_path / "resume-allowed"
    # First transport blocks before editing; killing its process group leaves
    # a durable executing item but no accepted patch. The next explicit run
    # uses the same inert transport with the gate released.
    original = backend.read_text()
    backend.write_text(original.replace(
        "import json, sys\n", "import json, sys, time\n"
    ).replace(
        f"Path('task_a.py').write_text({AFTER_A!r})",
        f"Path({str(sentinel)!r}).write_text('entered')\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.02)\n"
        f"Path('task_a.py').write_text({AFTER_A!r})",
    ))
    _register_struct(ws, "resume", corpus, [
        [sys.executable, "-c", SYNTAX_CHECK, "task_a.py"],
        [sys.executable, "check_behavior.py"],
    ], backend)
    env = _env_with_tools(ws, tmp_path)
    scan = _bluei(env, ws, "scan", "scan", "resume")
    assert scan.returncode == 0, scan.stdout + scan.stderr
    finding = _read_finding(ws, "resume", "task_a.py")
    pending = [entry for entry in _entries(ws, "resume", "pending_review")
               if entry.finding_id == finding["finding_id"]]
    assert len(pending) == 1
    identity = pending[0].work_id
    command = [sys.executable, str(BLUEI_CLI), "run", "resume", "--phase", "refactor-cycle",
               "--fix-engine", "claude", "--no-dry-run", "--no-learning",
               "--approve-refactor-item", identity]
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while not sentinel.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sentinel.exists(), "inert transport did not reach the declared interruption point"
        executing = _entries(ws, "resume", "executing")
        assert len(executing) == 1 and executing[0].work_id == identity
        assert executing[0].approved_by == "human" and executing[0].attempts == 1
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        out, err = process.communicate(timeout=10)
        (ws / "proof-runs" / "killed.out").write_text(out)
        (ws / "proof-runs" / "killed.err").write_text(err)
    assert process.returncode == -signal.SIGKILL
    state = _state(ws, "resume")
    assert not list(state.glob("*.patch"))
    interrupted = json.loads((state / "status.json").read_text())
    assert interrupted["last_run_completed"] is False
    assert (corpus / "task_a.py").read_text() == BEFORE_A
    release.write_text("explicit resume")
    resumed = _run_refactor(env, ws, "resume", "resumed")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    completed = [entry for entry in _entries(ws, "resume", "completed") if entry.work_id == identity]
    assert len(completed) == 1
    assert completed[0].approved_by == "human" and completed[0].attempts == 2
    assert Path(completed[0].retained_patch).is_file()
    assert len(_backend_calls(tmp_path)) == 2
    assert len(list(state.glob("*.patch"))) == 1
    metrics = json.loads((state / "status.json").read_text())["latest_run_metrics"]
    assert metrics["run_completed"] is True
    assert metrics["fix_attempts"] == metrics["claude_invocations"] == metrics["fixes_verified"] == 1
    assert (corpus / "task_a.py").read_text() == BEFORE_A
