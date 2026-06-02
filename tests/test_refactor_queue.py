#!/usr/bin/env python3
"""test_refactor_queue.py — tests for refactor_queue module."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Allow running as script from core/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from bluei.engine.models import Finding
from bluei.engine.reforge import RefactorWork, RefactorPhase


class FakeFinding:
    """Minimal Finding-like object for testing queue operations."""

    def __init__(
        self,
        finding_id: str = "test-001",
        rule: str = "xo-max-lines",
        path: str = "src/foo.ts",
        line: int = 10,
        snippet: str = "long file",
        confidence: float = 0.9,
        quick_win: bool = False,
        safe_to_autofix: bool = False,
        repo: str = "test-repo",
    ):
        self.finding_id = finding_id
        self.rule = rule
        self.path = path
        self.line = line
        self.snippet = snippet
        self.confidence = confidence
        self.quick_win = quick_win
        self.safe_to_autofix = safe_to_autofix
        self.repo = repo
        self.refactor_class: str | None = None
        self.refactor_phase: str | None = None

    def as_dict(self):
        return {
            "finding_id": self.finding_id,
            "repo": self.repo,
            "path": self.path,
            "line": self.line,
            "rule": self.rule,
            "snippet": self.snippet,
            "confidence": self.confidence,
            "quick_win": self.quick_win,
            "safe_to_autofix": self.safe_to_autofix,
            "refactor_class": self.refactor_class,
            "refactor_phase": self.refactor_phase,
        }


def test_queue_status_enum_values():
    """QueueStatus enum has the expected values."""
    from bluei.engine.refactor_queue import QueueStatus

    assert QueueStatus.PENDING_REVIEW.value == "pending_review"
    assert QueueStatus.APPROVED.value == "approved"
    assert QueueStatus.EXECUTING.value == "executing"
    assert QueueStatus.COMPLETED.value == "completed"
    assert QueueStatus.ABORTED.value == "aborted"
    print("✅ QueueStatus enum values correct")


def test_queue_entry_serialization():
    """QueueEntry serializes and deserializes correctly."""
    from bluei.engine.refactor_queue import QueueEntry, QueueStatus

    rw = RefactorWork(finding_id="test-001")
    rw.mark_splitting(["part1.ts", "part2.ts"], original_line_count=3000)

    entry = QueueEntry(
        work_id="rw-abc123",
        status=QueueStatus.PENDING_REVIEW,
        finding_id="test-001",
        rule="xo-max-lines",
        file_path="src/foo.ts",
        repo="test-repo",
        refactor_work=rw,
        finding_dict={"finding_id": "test-001", "rule": "xo-max-lines"},
    )

    d = entry.as_dict()
    assert d["work_id"] == "rw-abc123"
    assert d["status"] == "pending_review"
    assert d["refactor_work"]["phase"] == "splitting"
    assert d["refactor_work"]["planned_targets"] == ["part1.ts", "part2.ts"]
    assert d["refactor_work"]["original_line_count"] == 3000

    # Round-trip
    entry2 = QueueEntry.from_dict(d)
    assert entry2.work_id == entry.work_id
    assert entry2.status == entry.status
    assert entry2.refactor_work.phase == entry.refactor_work.phase
    assert entry2.refactor_work.planned_targets == entry.refactor_work.planned_targets

    print("✅ QueueEntry serialization round-trip correct")


def test_refactor_queue_enqueue_and_get():
    """RefactorQueue enqueues a finding and retrieves it."""
    from bluei.engine.refactor_queue import RefactorQueue, QueueStatus

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir) / "queue"
        queue = RefactorQueue(queue_dir=queue_dir)

        finding = FakeFinding(
            finding_id="rq-001", rule="xo-max-lines", path="src/big.ts"
        )
        rw = RefactorWork(finding_id="rq-001")
        rw.mark_aborted("safety gate triggered")

        entry = queue.enqueue(finding, rw)

        assert entry.work_id.startswith("rw-")
        assert entry.status == QueueStatus.PENDING_REVIEW
        assert entry.finding_id == "rq-001"
        assert entry.rule == "xo-max-lines"

        # Retrieve by work_id
        entry2 = queue.get(entry.work_id)
        assert entry2 is not None
        assert entry2.work_id == entry.work_id
        assert entry2.refactor_work.phase == RefactorPhase.ABORTED

        print("✅ RefactorQueue enqueue and get correct")


def test_queue_approve_transitions():
    """approve() transitions pending_review → approved."""
    from bluei.engine.refactor_queue import RefactorQueue, QueueStatus

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir) / "queue"
        queue = RefactorQueue(queue_dir=queue_dir)

        finding = FakeFinding(finding_id="rq-002", rule="xo-complexity")
        rw = RefactorWork(finding_id="rq-002")
        entry = queue.enqueue(finding, rw)

        assert entry.status == QueueStatus.PENDING_REVIEW
        assert entry.approved_at is None

        ok = queue.approve(entry.work_id, "human")
        assert ok is True

        entry2 = queue.get(entry.work_id)
        assert entry2 is not None
        assert entry2.status == QueueStatus.APPROVED
        assert entry2.approved_at is not None
        assert entry2.approved_by == "human"

        print("✅ queue approve() transitions pending_review → approved")


def test_queue_complete_transitions():
    """complete() transitions executing → completed."""
    from bluei.engine.refactor_queue import RefactorQueue, QueueStatus

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir) / "queue"
        queue = RefactorQueue(queue_dir=queue_dir)

        finding = FakeFinding(finding_id="rq-003")
        rw = RefactorWork(finding_id="rq-003")
        entry = queue.enqueue(finding, rw, status=QueueStatus.APPROVED)
        queue.start_execution(entry.work_id)

        entry2 = queue.get(entry.work_id)
        assert entry2.status == QueueStatus.EXECUTING

        ok = queue.complete(entry.work_id)
        assert ok is True

        entry3 = queue.get(entry.work_id)
        assert entry3.status == QueueStatus.COMPLETED
        assert entry3.completed_at is not None

        print("✅ queue complete() transitions executing → completed")


def test_queue_fail_transitions():
    """fail() transitions executing → aborted with error message."""
    from bluei.engine.refactor_queue import RefactorQueue, QueueStatus

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir) / "queue"
        queue = RefactorQueue(queue_dir=queue_dir)

        finding = FakeFinding(finding_id="rq-004")
        rw = RefactorWork(finding_id="rq-004")
        entry = queue.enqueue(finding, rw, status=QueueStatus.APPROVED)
        queue.start_execution(entry.work_id)

        ok = queue.fail(
            entry.work_id, "validation failed: target check returned non-zero"
        )
        assert ok is True

        entry2 = queue.get(entry.work_id)
        assert entry2.status == QueueStatus.ABORTED
        assert (
            entry2.error_message == "validation failed: target check returned non-zero"
        )

        print("✅ queue fail() transitions executing → aborted with error message")


def test_queue_list_items_filtered():
    """list_items() returns only entries matching the status filter."""
    from bluei.engine.refactor_queue import RefactorQueue, QueueStatus

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir) / "queue"
        queue = RefactorQueue(queue_dir=queue_dir)

        # Create 3 entries in different statuses
        f1 = FakeFinding(finding_id="rq-005a")
        rw1 = RefactorWork(finding_id="rq-005a")
        e1 = queue.enqueue(f1, rw1)  # pending_review

        f2 = FakeFinding(finding_id="rq-005b")
        rw2 = RefactorWork(finding_id="rq-005b")
        e2 = queue.enqueue(f2, rw2, status=QueueStatus.APPROVED)

        f3 = FakeFinding(finding_id="rq-005c")
        rw3 = RefactorWork(finding_id="rq-005c")
        e3 = queue.enqueue(f3, rw3, status=QueueStatus.EXECUTING)
        queue.start_execution(e3.work_id)

        pending = queue.list_items(status=QueueStatus.PENDING_REVIEW.value)
        pending_ids = [e.finding_id for e in pending]
        assert "rq-005a" in pending_ids
        assert "rq-005b" not in pending_ids
        assert "rq-005c" not in pending_ids

        executing = queue.list_items(status=QueueStatus.EXECUTING.value)
        exec_ids = [e.finding_id for e in executing]
        assert "rq-005c" in exec_ids

        print("✅ list_items() status filtering correct")


def test_queue_count_by_status():
    """count_by_status() returns accurate counts per category."""
    from bluei.engine.refactor_queue import RefactorQueue, QueueStatus

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir) / "queue"
        queue = RefactorQueue(queue_dir=queue_dir)

        # 2 pending, 1 approved, 1 completed
        for i in range(2):
            f = FakeFinding(finding_id=f"rq-count-{i}a")
            rw = RefactorWork(finding_id=f"rq-count-{i}a")
            queue.enqueue(f, rw)
        for i in range(1):
            f = FakeFinding(finding_id=f"rq-count-{i}b")
            rw = RefactorWork(finding_id=f"rq-count-{i}b")
            queue.enqueue(f, rw, status=QueueStatus.APPROVED)
        for i in range(1):
            f = FakeFinding(finding_id=f"rq-count-{i}c")
            rw = RefactorWork(finding_id=f"rq-count-{i}c")
            e = queue.enqueue(f, rw, status=QueueStatus.APPROVED)
            queue.start_execution(e.work_id)  # APPROVED → EXECUTING
            queue.complete(e.work_id)  # EXECUTING → COMPLETED

        counts = queue.count_by_status()
        assert counts["pending_review"] == 2, (
            f"expected 2 pending, got {counts['pending_review']}"
        )
        assert counts["approved"] == 1, f"expected 1 approved, got {counts['approved']}"
        assert counts["completed"] == 1, (
            f"expected 1 completed, got {counts['completed']}"
        )

        print("✅ count_by_status() accurate")


def test_enqueue_refactor_work_convenience():
    """enqueue_refactor_work() creates a pending_review entry in the default queue."""
    from bluei.engine.refactor_queue import enqueue_refactor_work, RefactorQueue

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir) / "queue"

        finding = FakeFinding(finding_id="rq-ease-001", rule="xo-complexity")
        rw = RefactorWork(finding_id="rq-ease-001")

        # Monkey-patch DEFAULT_REFACTOR_QUEUE_DIR for this test
        import bluei.engine.refactor_queue as rq_mod

        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir

        try:
            entry = enqueue_refactor_work(finding, rw)
            assert entry.work_id.startswith("rw-")
            assert entry.finding_id == "rq-ease-001"

            # Verify it exists in the queue
            queue = RefactorQueue(queue_dir=queue_dir)
            entry2 = queue.get(entry.work_id)
            assert entry2 is not None
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original

    print("✅ enqueue_refactor_work() convenience function correct")


def test_list_pending_review():
    """list_pending_review() returns all pending_review entries."""
    from bluei.engine.refactor_queue import (
        list_pending_review,
        RefactorQueue,
        enqueue_refactor_work,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir) / "queue"

        import bluei.engine.refactor_queue as rq_mod

        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir

        try:
            f1 = FakeFinding(finding_id="rq-pend-001", rule="xo-max-lines")
            rw1 = RefactorWork(finding_id="rq-pend-001")
            enqueue_refactor_work(f1, rw1)

            f2 = FakeFinding(finding_id="rq-pend-002", rule="xo-complexity")
            rw2 = RefactorWork(finding_id="rq-pend-002")
            enqueue_refactor_work(f2, rw2)

            pending = list_pending_review()
            pending_ids = [e.finding_id for e in pending]
            assert "rq-pend-001" in pending_ids
            assert "rq-pend-002" in pending_ids
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original

    print("✅ list_pending_review() returns all pending entries")


def test_create_refactor_plan():
    """create_refactor_plan() returns a structured plan dict."""
    from bluei.engine.refactor_queue import create_refactor_plan

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a 3000-line file
        big_file = Path(tmpdir) / "src" / "big.ts"
        big_file.parent.mkdir(parents=True, exist_ok=True)
        big_file.write_text(
            "\n".join([f"// line {i}" for i in range(3000)]), encoding="utf-8"
        )

        finding = FakeFinding(
            finding_id="rq-plan-001", rule="xo-max-lines", path=str(big_file)
        )
        plan = create_refactor_plan(finding)

        assert plan["strategy"] == "split_by_line_count"
        assert plan["estimated_parts"] == 2  # 3000 / 1500 = 2
        assert len(plan["planned_files"]) == 2
        assert "description" in plan

        # For a non-xo-max-lines rule
        finding2 = FakeFinding(
            finding_id="rq-plan-002", rule="xo-complexity", path="src/foo.ts"
        )
        plan2 = create_refactor_plan(finding2)
        assert plan2["strategy"] == "general_refactor"
        assert plan2["rule"] == "xo-complexity"

    print("✅ create_refactor_plan() returns structured plan dict")


def test_update_status_artifact_includes_refactor_queue_counts():
    """status.json surfaces refactor queue counts in current_counts and latest_run_metrics."""
    from bluei.engine.commands.helpers import update_status_artifact
    from bluei.engine.refactor_queue import RefactorQueue, QueueStatus

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        status_file = tmp / "status.json"
        issues_file = tmp / "issues.json"
        findings_file = tmp / "findings.jsonl"
        worktree_root = tmp / "worktrees"
        repo_path = tmp / "repo"
        repo_path.mkdir()
        worktree_root.mkdir()
        issues_file.write_text('{"issues": []}\n', encoding="utf-8")
        findings_file.write_text(
            '{"finding_id":"f1"}\n{"finding_id":"f2"}\n', encoding="utf-8"
        )

        import bluei.engine.refactor_queue as rq_mod

        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            queue = RefactorQueue(queue_dir=queue_dir)
            pending = queue.enqueue(
                FakeFinding(finding_id="rq-status-pending"),
                RefactorWork(finding_id="rq-status-pending"),
            )
            approved = queue.enqueue(
                FakeFinding(finding_id="rq-status-approved"),
                RefactorWork(finding_id="rq-status-approved"),
                status=QueueStatus.APPROVED,
            )
            executing = queue.enqueue(
                FakeFinding(finding_id="rq-status-executing"),
                RefactorWork(finding_id="rq-status-executing"),
                status=QueueStatus.APPROVED,
            )
            queue.start_execution(executing.work_id)

            args = argparse.Namespace(
                fix_engine="claude",
                claude_cmd_template='claude --print "fix"',
                staleness_threshold_seconds=600,
                repo_path=repo_path,
                state_file=tmp / "state.json",
                log_file=tmp / "run.log",
                findings_file=findings_file,
                issues_file=issues_file,
                worktree_root=worktree_root,
                open_issues_cap=10,
                open_prs_cap=5,
                issue_confidence_threshold=0.8,
                max_files_changed=20,
                max_loc_diff=500,
                max_prs_per_run=2,
                max_issues_per_run=3,
                finding_cooldown_seconds=3600,
                merge_cooldown_minutes=60,
                max_fix_attempts_per_issue=3,
                docs_index_file=tmp / "docs-index.json",
                refresh_docs_index=False,
                live_github_actions=False,
                auto_merge_sandbox=False,
                run_phase="refactor-cycle",
                max_queue_items=7,
                auto_approve=False,
            )
            update_status_artifact(
                status_file=status_file,
                state={"open_issues": 1, "open_prs": 0, "created": []},
                issues_file=issues_file,
                findings_file=findings_file,
                args=args,
                run_mode="refactor-cycle",
                reconcile_event={"kind": "noop"},
            )

            status = json.loads(status_file.read_text(encoding="utf-8"))
            assert status["current_counts"]["refactor_queue_total"] == 3
            assert status["refactor_queue"] == {
                "pending_review": 1,
                "approved": 1,
                "executing": 1,
                "completed": 0,
                "aborted": 0,
                "total": 3,
            }
            assert status["latest_run_metrics"]["refactor_queue_total"] == 3
            assert status["latest_run_metrics"]["refactor_queue_pending_review"] == 1
            assert status["latest_run_metrics"]["refactor_queue_approved"] == 1
            assert status["latest_run_metrics"]["refactor_queue_executing"] == 1
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original

    print("✅ update_status_artifact includes refactor queue counts")


def main():
    tests = [
        test_queue_status_enum_values,
        test_queue_entry_serialization,
        test_refactor_queue_enqueue_and_get,
        test_queue_approve_transitions,
        test_queue_complete_transitions,
        test_queue_fail_transitions,
        test_queue_list_items_filtered,
        test_queue_count_by_status,
        test_enqueue_refactor_work_convenience,
        test_list_pending_review,
        test_create_refactor_plan,
        test_update_status_artifact_includes_refactor_queue_counts,
    ]

    failed = []
    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"❌ {test.__name__}: {e}")
            failed.append(test.__name__)

    print()
    if failed:
        print(f"❌ {len(failed)} test(s) failed: {failed}")
        sys.exit(1)
    else:
        print(f"✅ All {len(tests)} tests passed")


if __name__ == "__main__":
    main()


# ── Tests extracted from test_phase5_modules_remaining.py ──

from unittest.mock import patch

from bluei.engine.refactor_queue import (
    QueueEntry,
    QueueStatus,
    RefactorQueue,
    create_refactor_plan,
    enqueue_refactor_work,
)


class TestRefactorQueueGetEdgeCases:
    """get() handles missing files and corrupt JSON."""

    def test_get_returns_none_for_missing_file(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        assert queue.get("nonexistent-id") is None

    def test_get_returns_none_for_corrupt_json(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        work_dir = tmp_path / "queue" / "finding_work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "rw-bad.json").write_text("{bad json", encoding="utf-8")
        assert queue.get("rw-bad") is None


class TestRefactorQueueListAllCategories:
    """list_items() filtering behaviour."""

    def test_list_items_no_status_scans_all(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        rw = RefactorWork(finding_id="f-1")
        queue.enqueue(FakeFinding("f-1"), rw)
        items = queue.list_items()
        assert len(items) == 1

    def test_list_items_filters_by_worktree_path(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        rw = RefactorWork(finding_id="f-1")
        queue.enqueue(FakeFinding("f-1", path="/worktrees/repo-1/src/a.ts"), rw)
        queue.enqueue(FakeFinding("f-2", path="/worktrees/repo-2/src/b.ts"), rw)
        filtered = queue.list_items(worktree_path="/worktrees/repo-1")
        assert len(filtered) == 1
        assert filtered[0].finding_id == "f-1"


class TestRefactorQueueTransitionGuard:
    """Transition guards reject wrong-status and missing entries."""

    def test_approve_returns_false_for_wrong_status(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        rw = RefactorWork(finding_id="f-1")
        entry = queue.enqueue(FakeFinding("f-1"), rw, status=QueueStatus.APPROVED)
        assert queue.approve(entry.work_id) is False

    def test_approve_returns_false_for_missing(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        assert queue.approve("nonexistent") is False

    def test_complete_returns_false_for_wrong_status(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        rw = RefactorWork(finding_id="f-1")
        entry = queue.enqueue(FakeFinding("f-1"), rw)
        assert queue.complete(entry.work_id) is False

    def test_fail_returns_false_for_wrong_status(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        rw = RefactorWork(finding_id="f-1")
        entry = queue.enqueue(FakeFinding("f-1"), rw)
        assert queue.fail(entry.work_id, "error") is False


class TestRefactorQueueAbortPending:
    """abort_pending() transitions and edge cases."""

    def test_abort_pending_transitions_to_aborted(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        rw = RefactorWork(finding_id="f-1")
        entry = queue.enqueue(FakeFinding("f-1"), rw)
        assert entry.status == QueueStatus.PENDING_REVIEW
        assert queue.abort_pending(entry.work_id, "not needed") is True
        reloaded = queue.get(entry.work_id)
        assert reloaded.status == QueueStatus.ABORTED
        assert reloaded.error_message == "not needed"
        assert reloaded.completed_at is not None

    def test_abort_pending_wrong_status(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        rw = RefactorWork(finding_id="f-1")
        entry = queue.enqueue(FakeFinding("f-1"), rw, status=QueueStatus.APPROVED)
        assert queue.abort_pending(entry.work_id, "nope") is False

    def test_abort_pending_missing(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        assert queue.abort_pending("nonexistent", "nope") is False


class TestEnqueueRefactorWorkWithWorktree:
    """enqueue_refactor_work resolves paths relative to worktree."""

    def test_enqueue_with_worktree_path(self, tmp_path):
        queue_dir = tmp_path / "queue"
        import bluei.engine.refactor_queue as rq_mod

        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            rw = RefactorWork(finding_id="f-1")
            entry = enqueue_refactor_work(
                FakeFinding("f-1", path="src/big.ts"),
                rw,
                worktree_path=tmp_path / "repo",
            )
            assert str(tmp_path / "repo" / "src" / "big.ts") in entry.file_path
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original


class TestCreateRefactorPlanFileReadError:
    """create_refactor_plan handles unreadable files gracefully."""

    def test_create_refactor_plan_handles_read_error(self, tmp_path):
        big_file = tmp_path / "big.ts"
        big_file.write_text("\n".join(["line"] * 100), encoding="utf-8")
        finding = FakeFinding("f-1", rule="xo-max-lines", path=str(big_file))
        with patch.object(Path, "read_text", side_effect=PermissionError("nope")):
            plan = create_refactor_plan(finding)
        assert plan["strategy"] == "split_by_line_count"


class TestRefactorQueueListItemsNoneEntry:
    """list_items() skips phantom link files."""

    def test_list_items_skips_missing_entries(self, tmp_path):
        queue = RefactorQueue(queue_dir=tmp_path / "queue")
        rw = RefactorWork(finding_id="f-1")
        queue.enqueue(FakeFinding("f-1"), rw)
        link_dir = tmp_path / "queue" / "pending_review"
        (link_dir / "rw-phantom.link").write_text("phantom", encoding="utf-8")
        items = queue.list_items(status=QueueStatus.PENDING_REVIEW.value)
        ids = [e.work_id for e in items]
        assert len([i for i in ids if i.startswith("rw-")]) >= 1
