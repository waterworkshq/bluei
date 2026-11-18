"""Refuse dangling artifacts and repeated interrupted dispatch beyond the cap."""

from types import SimpleNamespace

import pytest

from bluei.engine.commands.context import RunContext
from bluei.engine.commands.refactor_cycle import run_refactor_cycle_phase
from bluei.engine.models import Finding
from bluei.engine.refactor_queue import RefactorQueue
from bluei.engine.reforge import RefactorWork


def _context(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    finding = Finding(finding_id="current", repo=str(source), path="task.py", line=1,
                      rule="debt-todo-marker", snippet="# TODO: work", confidence=0.72,
                      quick_win=False, safe_to_autofix=False)
    args = SimpleNamespace(dry_run=False, auto_approve=False, approve_refactor_item=None,
                           max_queue_items=None, max_fix_attempts_per_issue=2)
    ctx = RunContext(args=args, repo_path=source, state_file=tmp_path / "state/run.json",
                     log_file=tmp_path / "run.log", findings=[finding],
                     detector_outcomes={"sandbox-string-detectors": {"status": "ran", "count": 1}},
                     issues_data={"issues": []})
    queue = RefactorQueue(tmp_path / "state/refactor_queue")
    entry = queue.enqueue(finding, RefactorWork(finding_id=finding.finding_id))
    assert queue.approve(entry.work_id)
    assert queue.start_execution(entry.work_id)
    return ctx, queue, entry, finding


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_dangling_retained_metadata_cannot_complete_queue(tmp_path, kind):
    ctx, queue, entry, finding = _context(tmp_path)
    path = tmp_path / "state/candidate.patch"
    if kind == "empty":
        path.write_text("")
    ctx.issues_data["issues"] = [{"finding_id": finding.finding_id, "verified_fix_patch": str(path), "github": {}}]
    calls = []
    rc = run_refactor_cycle_phase(ctx, executor=lambda *args: calls.append(args))
    assert rc == 1
    assert calls == []
    assert queue.get(entry.work_id).status.value == "executing"
    assert queue.get(entry.work_id).retained_patch is None
    assert "retained artifact unavailable" in ctx.blocked_reasons[0]


def test_interrupted_attempt_ceiling_preserves_item_without_dispatch(tmp_path):
    ctx, queue, entry, _finding = _context(tmp_path)
    assert queue.resume_execution(entry.work_id)
    calls = []
    rc = run_refactor_cycle_phase(ctx, executor=lambda *args: calls.append(args))
    assert rc == 1
    assert calls == []
    saved = queue.get(entry.work_id)
    assert saved.attempts == 2 and saved.approved_by == "human"
    assert saved.status.value == "executing"
    assert "attempt ceiling" in ctx.blocked_reasons[0]
