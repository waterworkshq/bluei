#!/usr/bin/env python3
"""Integration tests for the refactor-cycle run-phase and queue processing."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'core'))

from sandbox_local_runner.models import Finding
from sandbox_local_runner.refactor_queue import (
    RefactorQueue,
    QueueStatus,
    enqueue_refactor_work,
)
from sandbox_local_runner.reforge import RefactorWork, RefactorPhase


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


def test_refactor_queue_process_dry_run_approved():
    """process_refactor_queue in dry-run mode returns approved items as processed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        worktree = tmp / "worktree"
        worktree.mkdir()

        import sandbox_local_runner.refactor_queue as rq_mod
        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            from sandbox_local_runner.lifecycle import process_refactor_queue

            finding = FakeFinding(finding_id="rq-dry-001", rule="xo-complexity", path="src/foo.ts")
            rw = RefactorWork(finding_id="rq-dry-001")
            entry = enqueue_refactor_work(finding, rw, worktree)
            RefactorQueue(queue_dir=queue_dir).approve(entry.work_id, "human")

            # process_refactor_queue reads patched DEFAULT_REFACTOR_QUEUE_DIR at call time
            result = process_refactor_queue(
                worktree_path=worktree,
                repo_path=worktree.parent / "repo",
                dry_run=True,
                max_items=None,
                auto_approve=False,
            )
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original

        assert entry.work_id in result["processed"], (
            f"expected {entry.work_id} in processed, got {result}"
        )
        assert result["failed"] == []
        print("✅ dry_run approved item listed as processed")


def test_refactor_queue_auto_approve_moves_pending():
    """auto_approve=True transitions pending_review → approved before processing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        worktree = tmp / "worktree"
        worktree.mkdir()

        import sandbox_local_runner.refactor_queue as rq_mod
        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            from sandbox_local_runner.lifecycle import process_refactor_queue

            finding = FakeFinding(finding_id="rq-auto-001", rule="xo-max-lines", path="src/big.ts")
            rw = RefactorWork(finding_id="rq-auto-001")
            entry = enqueue_refactor_work(finding, rw, worktree)
            assert entry.status == QueueStatus.PENDING_REVIEW

            # Use dry_run=False to actually perform the approval
            result = process_refactor_queue(
                worktree_path=worktree,
                repo_path=worktree.parent / "repo",
                dry_run=False,
                max_items=None,
                auto_approve=True,
            )
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original

        assert entry.work_id in result["approved"], (
            f"expected {entry.work_id} in approved, got {result}"
        )
        # dry_run=False means execution is attempted; Claude fix engine will fail
        # since there's no real Claude binary, so the item goes to failed
        assert entry.work_id in result["failed"], (
            f"expected {entry.work_id} in failed (Claude not available), got {result}"
        )
        # The item should NOT still be in pending after auto_approve
        assert entry.work_id not in result["pending"], (
            f"item should not be pending after auto-approve, got {result}"
        )
        print("✅ auto_approve transitions pending → approved")


def test_refactor_queue_max_items_limits_processing():
    """max_items=N caps the number of approved items processed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        worktree = tmp / "worktree"
        worktree.mkdir()

        import sandbox_local_runner.refactor_queue as rq_mod
        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            from sandbox_local_runner.lifecycle import process_refactor_queue

            queue = RefactorQueue(queue_dir=queue_dir)
            work_ids = []
            for i in range(3):
                finding = FakeFinding(finding_id=f"rq-max-{i}", rule="xo-complexity", path=f"src/f{i}.ts")
                rw = RefactorWork(finding_id=f"rq-max-{i}")
                e = enqueue_refactor_work(finding, rw, worktree)
                queue.approve(e.work_id, "human")
                work_ids.append(e.work_id)

            result = process_refactor_queue(
                worktree_path=worktree,
                repo_path=worktree.parent / "repo",
                dry_run=True,
                max_items=2,
                auto_approve=False,
            )
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original

        assert len(result["processed"]) == 2, (
            f"expected 2 processed, got {len(result['processed'])}: {result}"
        )
        print("✅ max_items=2 limits processing to 2 items")


def test_refactor_queue_approved_items_require_execution_phase():
    """Only APPROVED items are picked up for execution; PENDING_REVIEW are not."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        worktree = tmp / "worktree"
        worktree.mkdir()

        import sandbox_local_runner.refactor_queue as rq_mod
        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            from sandbox_local_runner.lifecycle import process_refactor_queue

            queue = RefactorQueue(queue_dir=queue_dir)

            # One approved via enqueue_refactor_work + approve
            f1 = FakeFinding(finding_id="rq-exec-001", path="src/a.ts")
            rw1 = RefactorWork(finding_id="rq-exec-001")
            e1 = enqueue_refactor_work(f1, rw1, worktree)
            queue.approve(e1.work_id, "human")

            # One pending (enqueued but not approved)
            f2 = FakeFinding(finding_id="rq-exec-002", path="src/b.ts")
            rw2 = RefactorWork(finding_id="rq-exec-002")
            e2 = enqueue_refactor_work(f2, rw2, worktree)
            assert e2.status == QueueStatus.PENDING_REVIEW

            result = process_refactor_queue(
                worktree_path=worktree,
                repo_path=worktree.parent / "repo",
                dry_run=True,
                max_items=None,
                auto_approve=False,
            )
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original

        assert e1.work_id in result["processed"], (
            f"e1 should be processed, got {result}"
        )
        assert e2.work_id in result["pending"], (
            f"e2 should be pending, got {result}"
        )
        print("✅ only APPROVED items are processed; PENDING_REVIEW go to pending list")


def test_queue_entry_from_dict_roundtrips():
    """QueueEntry serialization survives through Finding.from_dict round-trip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        worktree = tmp / "worktree"
        worktree.mkdir()

        import sandbox_local_runner.refactor_queue as rq_mod
        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            finding = FakeFinding(finding_id="rq-serial-001", rule="xo-max-lines", path="src/big.ts")
            rw = RefactorWork(finding_id="rq-serial-001")
            rw.mark_splitting(["part1.ts", "part2.ts"], original_line_count=3000)

            entry = enqueue_refactor_work(finding, rw, worktree_path=worktree)

            queue = RefactorQueue(queue_dir=queue_dir)
            loaded = queue.get(entry.work_id)
            assert loaded is not None
            assert loaded.work_id == entry.work_id
            assert loaded.status == entry.status
            assert loaded.refactor_work.phase == RefactorPhase.SPLITTING
            assert loaded.refactor_work.planned_targets == ["part1.ts", "part2.ts"]
            assert loaded.refactor_work.original_line_count == 3000

            # Verify finding_dict round-trips
            finding_loaded = Finding.from_dict(loaded.finding_dict)
            assert finding_loaded.finding_id == finding.finding_id
            assert finding_loaded.rule == finding.rule
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original
        print("✅ QueueEntry finding_dict round-trips through Finding.from_dict")


def test_enqueue_refactor_work_stores_worktree_rooted_path():
    """enqueue_refactor_work stores worktree-rooted file_path when worktree_path is given."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        worktree = tmp / "worktree"
        worktree.mkdir()

        import sandbox_local_runner.refactor_queue as rq_mod
        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            finding = FakeFinding(finding_id="rq-path-001", path="src/huge.ts")
            rw = RefactorWork(finding_id="rq-path-001")
            entry = enqueue_refactor_work(finding, rw, worktree_path=worktree)

            # file_path should be worktree-rooted
            assert entry.file_path.startswith(str(worktree)), (
                f"expected file_path to start with {worktree}, got {entry.file_path}"
            )
            assert entry.file_path.endswith("src/huge.ts"), (
                f"expected file_path to end with src/huge.ts, got {entry.file_path}"
            )
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original
        print("✅ enqueue_refactor_work stores worktree-rooted file_path")


def main():
    tests = [
        test_refactor_queue_process_dry_run_approved,
        test_refactor_queue_auto_approve_moves_pending,
        test_refactor_queue_max_items_limits_processing,
        test_refactor_queue_approved_items_require_execution_phase,
        test_queue_entry_from_dict_roundtrips,
        test_enqueue_refactor_work_stores_worktree_rooted_path,
    ]

    failed = []
    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"❌ {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed.append(test.__name__)

    print()
    if failed:
        print(f"❌ {len(failed)} test(s) failed: {failed}")
        sys.exit(1)
    else:
        print(f"✅ All {len(tests)} tests passed")


if __name__ == "__main__":
    main()
