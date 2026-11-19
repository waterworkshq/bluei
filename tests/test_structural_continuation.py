"""Structural continuation regressions for the Stage 3 refactor-cycle lane.

Drives the canonical public CLI only — ``bin/bluei scan <name>`` for real
tool-produced structural findings and ``bin/bluei run <name> --phase
refactor-cycle --fix-engine claude --no-dry-run --no-learning`` for the
durable-queue consumer — against fresh, remote-free, ordinary public Python
corpora. The model transport is an INERT local script (a ``claude``
executable on PATH plus ``claude_template`` pointing at it): no real model,
no network. Approvals are SIMULATED HUMAN ACTIONS, labeled as such: the
test author plays the human and passes the explicit
``--approve-refactor-item <work_id>`` action; nothing in the product
approves automatically.

Corpus shape per workspace: an empty ``price.py`` (a configured
string-detector input, so the detector family records ``ran`` — no markdown
files, whose linter is deliberately unavailable and would truthfully fail
the producer guard) plus small Python modules carrying real
``debt-todo-marker`` diagnostics (``# TODO:`` lines, plugin-produced,
confidence 0.72). Each marked function is a behavioral stub returning
incomplete data; a per-workspace behavior check FAILS on the stub and
PASSES on the completed function — the explicit before-fail/after-pass
evidence — alongside an always-passing syntax control check (verification
requires a passing control in both baseline and post). The marker is the
diagnostic signal; the bug claim is the incomplete behavior, not the text.

Contracts asserted:
1. pending_review persists without approval (no dispatch, no patch, idle
   transport); bulk ``--auto-approve`` is visibly rejected; an unknown or
   path-traversal ``--approve-refactor-item`` id is refused (work-id trust
   boundary).
2. One explicitly approved item executes exactly once through the qualified
   seam: queue entry COMPLETED with the retained artifact, issue open with
   ``verified_fix_patch``, source pristine, counters exact (no double
   count), transport called once.
3. The retained patch completes the stubbed behavior: the check CALLS the
   function and asserts exact simple values — fails before the patch (the
   call returns the incomplete stub), passes after (before-fail/after-pass
   on a substantive claim, values preserved).
4. A repeat run does not re-execute completed work: no new attempt, no new
   patch; the still-present finding re-enqueues as a fresh pending_review
   entry (terminal entries never block).
5. Interruption: a stale executing entry with no fresh evidence aborts with
   a distinct ``interrupted:`` record; a stale executing entry whose exact
   finding is still fresh CONTINUES the same durable work id (attempts
   recorded, approval retained — Stage 3 is incomplete if approval is
   lost), then completes.
6. Reconcile: a run killed after retention saved the issue but before queue
   completion is reconciled on the next explicit run — entry COMPLETED from
   the retained artifact, no re-execution, no duplicate accepted counters.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_ordinary_repair_loop import (
    BLUEI_CLI,
    ROOT,
    RUN_TIMEOUT,
    _bluei,
    _env,
    _git,
    _make_corpus,
    _state,
    _yaml,
)

SYNTAX_CHECK = "import ast, sys; ast.parse(open(sys.argv[1]).read())"

# The debt marker is the DIAGNOSTIC; the defect it marks is behavioral: the
# function is a stub returning incomplete data where completed behavior
# returns the real result. Each behavior check fails on the stub and passes
# on the completed function — an explicit before-fail/after-pass claim, not
# marker-text matching. Distinct snippets keep finding identities distinct
# (full-evidence identity, never coarse path identity).
BEFORE_A = (
    "def backlog_items():\n"
    "    # TODO: reconcile warehouse backlog\n"
    "    return []\n"
)
AFTER_A = "def backlog_items():\n    return ['shelf-3', 'shelf-7']\n"
BEFORE_B = (
    "def parity_positions():\n"
    "    # TODO: verify parity offset table\n"
    "    return []\n"
)
AFTER_B = "def parity_positions():\n    return [0, 2, 4]\n"
BEFORE_D = (
    "def margin_ledger():\n"
    "    # TODO: fold margin adjustments into ledger\n"
    "    return {}\n"
)
COMPLETED_D = "def margin_ledger():\n    return {'adjusted': True}\n"
BEFORE_C = (
    "def cycle_counter():\n"
    "    # TODO: reset cycle counter nightly\n"
    "    return -1\n"
)

CHECK_A = '''\
import sys
sys.dont_write_bytecode = True
from task_a import backlog_items
items = backlog_items()
assert items == ['shelf-3', 'shelf-7'], f"backlog stub incomplete: {items!r}"
'''
CHECK_B = '''\
import sys
sys.dont_write_bytecode = True
from task_b import parity_positions
positions = parity_positions()
assert positions == [0, 2, 4], f"parity stub incomplete: {positions!r}"
'''
CHECK_C = '''\
import sys
sys.dont_write_bytecode = True
from task_c import cycle_counter
count = cycle_counter()
assert count == 0, f"cycle counter never completed: {count!r}"
'''


def _queue_dir(ws: Path, name: str) -> Path:
    return _state(ws, name) / "refactor_queue"


def _read_finding(ws: Path, name: str, target: str,
                  rule: str = "debt-todo-marker") -> dict:
    path = _state(ws, name) / "findings.jsonl"
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matches = [r for r in records if r.get("rule") == rule
               and r.get("path") == target]
    assert len(matches) == 1, f"expected one {rule} finding in {target}, got {matches}"
    return matches[0]


def _entries(ws: Path, name: str, status: str):
    from bluei.engine.refactor_queue import RefactorQueue

    return RefactorQueue(_queue_dir(ws, name)).list_items(status=status)


def _entry_for(ws: Path, name: str, status: str, finding_id: str):
    matches = [e for e in _entries(ws, name, status) if e.finding_id == finding_id]
    assert len(matches) == 1, f"expected one {status} entry, got {matches}"
    return matches[0]


def _backend_calls(tmp: Path) -> list:
    calls = tmp / "backend-calls.jsonl"
    if not calls.exists():
        return []
    return [
        json.loads(line)
        for line in calls.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _make_backend(tmp: Path, fixed_source: str, target: str) -> Path:
    """Inert local model transport: logs its invocation, applies the known
    fix to the target module in cwd (the repair worktree), exits 0."""
    tools = tmp / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    calls = tmp / "backend-calls.jsonl"
    backend = tools / "claude"
    backend.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as s:\n"
        "    s.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"Path({target!r}).write_text({fixed_source!r})\n",
        encoding="utf-8",
    )
    backend.chmod(0o755)
    return backend


def _register_struct(ws: Path, name: str, repo: Path, baseline_checks, backend):
    """Author the public workspace artifacts (no product ConfigManager)."""
    (ws / "bluei").symlink_to(ROOT / "bluei", target_is_directory=True)
    (ws / "plugins").symlink_to(ROOT / "plugins", target_is_directory=True)
    lines = [
        "id: " + _yaml("repo-" + name),
        "name: " + _yaml(name),
        "path: " + _yaml(str(repo)),
        "language: python",
        "enabled: true",
        "fix_engine: claude",
        "claude_template: " + _yaml(f"{backend} {{prompt_file}} {{finding_id}}"),
        "baseline_checks:",
    ]
    lines += ["  - " + _yaml(list(cmd)) for cmd in baseline_checks]
    lines += [
        "limits:",
        "  open_issues_cap: 20",
        "  open_prs_cap: 5",
        "  max_prs_per_run: 2",
        "  max_issues_per_run: 10",
        "  max_files_changed: 5",
        "  max_loc_diff: 200",
        "  max_fix_attempts: 3",
        "cooldowns:",
        "  finding_seconds: 0",
        "  merge_minutes: 0",
        "  staleness_seconds: 7200",
        "github:",
        "  live_actions: false",
        "  auto_merge: false",
    ]
    repo_dir = ws / "repos" / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return repo_dir


def _env_with_tools(ws: Path, tmp: Path) -> dict:
    env = _env(ws)
    env["PATH"] = str(tmp / "tools") + os.pathsep + env.get("PATH", "")
    return env


def _run_refactor(env, ws: Path, name: str, label: str, *extra: str):
    return _bluei(
        env, ws, label, "run", name,
        "--phase", "refactor-cycle", "--fix-engine", "claude",
        "--no-dry-run", "--no-learning", *extra,
        timeout=RUN_TIMEOUT,
    )


def _seed_entry(ws: Path, name: str, repo: Path, finding: dict, *,
                approve=False, execute=False):
    """Seed the per-source durable queue exactly as discover would: real
    tool-produced finding record enqueued worktree-rooted (same identity
    basis the consumer matches against), then explicit (simulated human)
    approval and/or start of execution."""
    from bluei.engine.models import Finding
    from bluei.engine.refactor_queue import RefactorQueue, enqueue_refactor_work
    from bluei.engine.reforge import RefactorWork

    finding_obj = Finding.from_dict(finding)
    entry = enqueue_refactor_work(
        finding_obj,
        RefactorWork(finding_id=finding_obj.finding_id),
        worktree_path=repo,
        queue_dir=_queue_dir(ws, name),
    )
    queue = RefactorQueue(_queue_dir(ws, name))
    if approve:
        assert queue.approve(entry.work_id, "human")
    if execute:
        assert queue.start_execution(entry.work_id)
    return queue.get(entry.work_id)


# ── Workspace 1: approval-gated execution, refusal controls, repeat ─────────

@pytest.fixture(scope="module")
def ws1(tmp_path_factory):
    base = tmp_path_factory.mktemp("structural-ws1")
    ws = base / "workspace"
    ws.mkdir()
    corpus = _make_corpus(base, "structural-a", {
        "price.py": "",
        "pyproject.toml": "",
        "task_a.py": BEFORE_A,
        "check_behavior.py": CHECK_A,
    })
    backend = _make_backend(base, AFTER_A, "task_a.py")
    _register_struct(ws, "structural-a", corpus, [
        [sys.executable, "-c", SYNTAX_CHECK, "task_a.py"],
        [sys.executable, "check_behavior.py"],
    ], backend)
    env = _env_with_tools(ws, base)
    scan = _bluei(env, ws, "ws1-scan", "scan", "structural-a")
    assert scan.returncode == 0, scan.stdout + scan.stderr
    finding = _read_finding(ws, "structural-a", "task_a.py")
    entry = _seed_entry(ws, "structural-a", corpus, finding)
    return base, ws, corpus, env, finding, entry


def test_pending_review_persists_without_approval(ws1):
    _base, ws, corpus, env, finding, entry = ws1
    run = _run_refactor(env, ws, "structural-a", "ws1-pending")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "dispatched=0" in run.stdout
    live = _entry_for(ws, "structural-a", "pending_review", finding["finding_id"])
    assert live.work_id == entry.work_id
    assert live.approved_by is None
    assert _backend_calls(_base) == []
    assert list(_state(ws, "structural-a").glob("*.patch")) == []
    assert (corpus / "task_a.py").read_text(encoding="utf-8") == BEFORE_A


def test_bulk_auto_approve_is_visibly_rejected(ws1):
    _base, ws, _corpus, env, _finding, _entry = ws1
    run = _run_refactor(env, ws, "structural-a", "ws1-auto-approve",
                        "--auto-approve")
    assert run.returncode != 0
    assert "auto-approval is not supported" in (run.stdout + run.stderr)


@pytest.mark.parametrize("bad_id", ["rw-nonexistent", "../finding_work/x"])
def test_unknown_or_traversing_approval_id_refused(ws1, bad_id):
    _base, ws, _corpus, env, _finding, _entry = ws1
    label = "ws1-refuse-traversal" if "/" in bad_id else "ws1-refuse-unknown"
    run = _run_refactor(env, ws, "structural-a", label,
                        "--approve-refactor-item", bad_id)
    assert run.returncode != 0
    assert "approval refused" in (run.stdout + run.stderr)
    # The trust boundary rejected the id before touching any path.
    assert (_state(ws, "structural-a") / "refactor_queue" / "completed"
            / f"{bad_id}.link").exists() is False


def test_simulated_human_approval_executes_once_and_completes(ws1):
    """SIMULATED HUMAN APPROVAL (labeled): the test author explicitly passes
    --approve-refactor-item for the one pending item; the product performs no
    automatic approval anywhere."""
    base, ws, corpus, env, finding, entry = ws1
    run = _run_refactor(env, ws, "structural-a", "ws1-approved",
                        "--approve-refactor-item", entry.work_id)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "dispatched=1" in run.stdout and "verified=1" in run.stdout

    completed = _entry_for(ws, "structural-a", "completed", finding["finding_id"])
    assert completed.work_id == entry.work_id
    assert completed.approved_by == "human"
    assert completed.attempts == 1
    patch = Path(completed.retained_patch)
    assert patch.exists()

    issues = json.loads(
        (_state(ws, "structural-a") / "issues.json").read_text(encoding="utf-8")
    )["issues"]
    structural = [i for i in issues if i.get("finding_id") == finding["finding_id"]]
    assert len(structural) == 1
    issue = structural[0]
    assert issue["status"] == "open"
    assert Path(issue["verified_fix_patch"]) == patch
    attempts = [e for e in issue.get("history", [])
                if e.get("event") == "fix_attempted"]
    assert len(attempts) == 1

    # Exactly one transport invocation; counters exact (no double count).
    assert len(_backend_calls(base)) == 1
    metrics = json.loads(
        (_state(ws, "structural-a") / "status.json").read_text(encoding="utf-8")
    )["latest_run_metrics"]
    assert metrics["fix_attempts"] == 1
    assert metrics["fixes_verified"] == 1
    assert metrics["claude_invocations"] == 1

    # Registered source stays pristine; the fix lives in the retained patch.
    assert (corpus / "task_a.py").read_text(encoding="utf-8") == BEFORE_A


@pytest.mark.parametrize("stage,expected_rc", [("pristine", 1), ("patched", 0)])
def test_retained_patch_completes_stub_behavior_preserving_values(
    ws1, tmp_path, stage, expected_rc,
):
    """Substantive claim: the patch completes the stubbed behavior. The check
    CALLS the function first, then asserts the exact simple values — before
    the patch the call returns the incomplete stub and the assertion fails;
    after it the same call returns ['shelf-3', 'shelf-7']."""
    _base, ws, corpus, _env, finding, _entry = ws1
    completed = _entry_for(ws, "structural-a", "completed", finding["finding_id"])
    patch = Path(completed.retained_patch)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(corpus), str(clone)],
                   check=True, capture_output=True, text=True, timeout=60)
    _git(clone, "remote", "remove", "origin")
    if stage == "patched":
        _git(clone, "apply", str(patch))
        touched = [line.split(" b/", 1)[1] for line in
                   patch.read_text(encoding="utf-8").splitlines()
                   if line.startswith("+++ b/")]
        assert touched == ["task_a.py"]
    behavior = subprocess.run(
        [sys.executable, "check_behavior.py"],
        cwd=str(clone), capture_output=True, text=True, timeout=60,
    )
    assert behavior.returncode == expected_rc, behavior.stdout + behavior.stderr
    if stage == "pristine":
        # The failing claim is behavioral: the call ran and returned the
        # incomplete stub, not marker text.
        assert "backlog stub incomplete: []" in behavior.stderr


def test_repeat_run_requeues_fresh_but_never_reexecutes(ws1):
    base, ws, corpus, env, finding, entry = ws1
    run = _run_refactor(env, ws, "structural-a", "ws1-repeat")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "dispatched=0" in run.stdout

    # No re-execution: same single patch, same single transport call.
    assert len(_backend_calls(base)) == 1
    assert len(list(_state(ws, "structural-a").glob("*.patch"))) == 1
    completed = _entry_for(ws, "structural-a", "completed", finding["finding_id"])
    assert completed.attempts == 1

    # The still-present finding enqueues as a FRESH pending item (the
    # completed entry is terminal and never blocks a new one).
    fresh_pending = _entries(ws, "structural-a", "pending_review")
    assert [e.finding_id for e in fresh_pending] == [finding["finding_id"]]
    assert fresh_pending[0].work_id != entry.work_id
    assert (corpus / "task_a.py").read_text(encoding="utf-8") == BEFORE_A


# ── Workspace 2: interruption abort vs exact-item continuation ──────────────

@pytest.fixture(scope="module")
def ws2(tmp_path_factory):
    base = tmp_path_factory.mktemp("structural-ws2")
    ws = base / "workspace"
    ws.mkdir()
    corpus = _make_corpus(base, "structural-b", {
        "price.py": "",
        "pyproject.toml": "",
        "task_b.py": BEFORE_B,
        "task_d.py": BEFORE_D,
        "check_behavior.py": CHECK_B,
    })
    backend = _make_backend(base, AFTER_B, "task_b.py")
    _register_struct(ws, "structural-b", corpus, [
        [sys.executable, "-c", SYNTAX_CHECK, "task_b.py"],
        [sys.executable, "check_behavior.py"],
    ], backend)
    env = _env_with_tools(ws, base)
    scan = _bluei(env, ws, "ws2-scan", "scan", "structural-b")
    assert scan.returncode == 0, scan.stdout + scan.stderr
    b_finding = _read_finding(ws, "structural-b", "task_b.py")
    d_finding = _read_finding(ws, "structural-b", "task_d.py")
    # SIMULATED HUMAN APPROVAL (labeled) for the continuation item, then a
    # crash mid-flight leaves it durably EXECUTING (persisted before
    # dispatch). The stale item is seeded the same way, and its evidence is
    # then removed from the corpus before the resume run.
    b_entry = _seed_entry(ws, "structural-b", corpus, b_finding,
                          approve=True, execute=True)
    assert b_entry.approved_by == "human"
    assert b_entry.attempts == 1
    _seed_entry(ws, "structural-b", corpus, d_finding,
                approve=True, execute=True)
    (corpus / "task_d.py").write_text(COMPLETED_D, encoding="utf-8")
    _git(corpus, "add", "-A")
    _git(corpus, "commit", "-qm", "remove stale structural evidence")
    return base, ws, corpus, env, b_finding, b_entry


def test_interrupted_without_evidence_aborts_and_exact_item_continues(ws2):
    base, ws, corpus, env, finding, b_entry = ws2
    run = _run_refactor(env, ws, "structural-b", "ws2-resume")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "dispatched=1" in run.stdout and "verified=1" in run.stdout

    # Exact-item continuation: SAME durable work id (approval not lost),
    # second attempt recorded, completed with retained artifact.
    completed = _entry_for(ws, "structural-b", "completed", finding["finding_id"])
    assert completed.work_id == b_entry.work_id
    assert completed.approved_by == "human"
    assert completed.attempts == 2
    assert Path(completed.retained_patch).exists()
    assert (corpus / "task_b.py").read_text(encoding="utf-8") == BEFORE_B
    assert len(_backend_calls(base)) == 1  # only the continued item ran

    # The stale item whose fresh evidence disappeared aborted truthfully
    # with the distinct interrupted record; it was never dispatched.
    aborted = _entries(ws, "structural-b", "aborted")
    assert len(aborted) == 1
    assert aborted[0].error_message.startswith("interrupted:")
    assert aborted[0].finding_id != finding["finding_id"]
    calls = _backend_calls(base)
    assert all(finding["finding_id"] in json.dumps(c) for c in calls)


# ── Workspace 3: reconcile after kill-between-retention-and-completion ──────

@pytest.fixture(scope="module")
def ws3(tmp_path_factory):
    base = tmp_path_factory.mktemp("structural-ws3")
    ws = base / "workspace"
    ws.mkdir()
    corpus = _make_corpus(base, "structural-c", {
        "price.py": "",
        "pyproject.toml": "",
        "task_c.py": BEFORE_C,
        "check_behavior.py": CHECK_C,
    })
    backend = _make_backend(base, BEFORE_C, "task_c.py")  # must never run
    _register_struct(ws, "structural-c", corpus, [
        [sys.executable, "-c", SYNTAX_CHECK, "task_c.py"],
        [sys.executable, "check_behavior.py"],
    ], backend)
    env = _env_with_tools(ws, base)
    scan = _bluei(env, ws, "ws3-scan", "scan", "structural-c")
    assert scan.returncode == 0, scan.stdout + scan.stderr
    finding = _read_finding(ws, "structural-c", "task_c.py")
    c_entry = _seed_entry(ws, "structural-c", corpus, finding,
                          approve=True, execute=True)

    # Kill-after-retention state: the issue already carries the verified
    # patch (retention persisted BEFORE positive effects), but the queue
    # entry is still EXECUTING — completion never happened.
    state = _state(ws, "structural-c")
    patch = state / f"local-fix-{finding['finding_id'][:16]}.patch"
    patch.write_text("diff --git a/task_c.py b/task_c.py\n--- a/task_c.py\n+++ b/task_c.py\n@@ -1,3 +1,2 @@\n def cycle_counter():\n-    # TODO: reset cycle counter nightly\n-    return -1\n+    return 0\n", encoding="utf-8")
    (state / "issues.json").write_text(
        json.dumps({"issues": [dict(
            finding,
            issue_id=f"refactor-{c_entry.work_id}",
            status="open",
            github={},
            history=[{"event": "fix_attempted", "at": "prior-run"}],
            verified_fix_patch=str(patch),
        )]}),
        encoding="utf-8",
    )
    return base, ws, corpus, env, finding, c_entry, patch


def test_reconcile_completes_from_retained_artifact_without_reexecution(ws3):
    base, ws, corpus, env, finding, c_entry, patch = ws3
    run = _run_refactor(env, ws, "structural-c", "ws3-reconcile")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "reconciled=1" in run.stdout and "dispatched=0" in run.stdout

    completed = _entry_for(ws, "structural-c", "completed", finding["finding_id"])
    assert completed.work_id == c_entry.work_id
    assert Path(completed.retained_patch) == patch
    # No re-invocation, no new attempt, no duplicate accepted counters.
    assert _backend_calls(base) == []
    assert completed.attempts == 1
    issues = json.loads(
        (_state(ws, "structural-c") / "issues.json").read_text(encoding="utf-8")
    )["issues"]
    issue = next(i for i in issues if i.get("finding_id") == finding["finding_id"])
    attempts = [e for e in issue.get("history", [])
                if e.get("event") == "fix_attempted"]
    assert len(attempts) == 1
    metrics = json.loads(
        (_state(ws, "structural-c") / "status.json").read_text(encoding="utf-8")
    )["latest_run_metrics"]
    assert metrics.get("fix_attempts", 0) == 0
    assert metrics.get("fixes_verified", 0) == 0
    assert (corpus / "task_c.py").read_text(encoding="utf-8") == BEFORE_C
