#!/usr/bin/env python3
"""Integration tests for the refactor-cycle run-phase and queue processing."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from bluei.engine.models import Finding
from bluei.engine.refactor_queue import (
    RefactorQueue,
    QueueStatus,
    enqueue_refactor_work,
)
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


def _snapshot_queue_files(queue_dir: Path) -> dict:
    """Map of relative path to bytes for every file under the queue dir."""
    return {
        str(p.relative_to(queue_dir)): p.read_bytes()
        for p in sorted(queue_dir.rglob("*"))
        if p.is_file()
    }


def test_refactor_queue_refusal_leaves_queue_byte_identical():
    """Stage1 refusal: no processing, no auto-approval, no backend, no mutation.

    Consolidates the obsolete dry-run/approved/max_items/pending-select tests:
    every flag combination now yields the same refusal, so one call with all
    flags set covers them.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        worktree = tmp / "worktree"
        worktree.mkdir()

        import bluei.engine.refactor_queue as rq_mod

        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            from bluei.engine.lifecycle import process_refactor_queue

            queue = RefactorQueue(queue_dir=queue_dir)

            # One approved and one still pending entry on disk.
            f1 = FakeFinding(finding_id="rq-exec-001", path="src/a.ts")
            rw1 = RefactorWork(finding_id="rq-exec-001")
            e1 = enqueue_refactor_work(f1, rw1, worktree)
            queue.approve(e1.work_id, "human")
            f2 = FakeFinding(finding_id="rq-exec-002", path="src/b.ts")
            rw2 = RefactorWork(finding_id="rq-exec-002")
            e2 = enqueue_refactor_work(f2, rw2, worktree)

            before = _snapshot_queue_files(queue_dir)

            with patch(
                "bluei.engine.lifecycle.apply_claude_fix",
                return_value=(1, "mocked: claude not available in tests", ""),
            ) as mock_apply:
                result = process_refactor_queue(
                    worktree_path=worktree,
                    repo_path=worktree.parent / "repo",
                    dry_run=False,
                    max_items=1,
                    auto_approve=True,
                )
        finally:
            rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = original

        assert result == {
            "processed": [],
            "approved": [],
            "pending": [],
            "failed": [],
            "unqualified": (
                "structural repair unqualified: missing configured scope, "
                "validation, closure and durable-artifact acceptance"
            ),
        }, f"expected Stage1 refusal result, got {result}"
        mock_apply.assert_not_called()
        # No mutation: queue storage byte-identical, statuses unchanged on disk.
        assert _snapshot_queue_files(queue_dir) == before
        assert queue.get(e1.work_id).status == QueueStatus.APPROVED
        assert queue.get(e2.work_id).status == QueueStatus.PENDING_REVIEW
        print("✅ Stage1 refusal leaves queue byte-identical and backend idle")


def test_queue_entry_from_dict_roundtrips():
    """QueueEntry serialization survives through Finding.from_dict round-trip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        queue_dir = tmp / "queue"
        worktree = tmp / "worktree"
        worktree.mkdir()

        import bluei.engine.refactor_queue as rq_mod

        original = rq_mod.DEFAULT_REFACTOR_QUEUE_DIR
        rq_mod.DEFAULT_REFACTOR_QUEUE_DIR = queue_dir
        try:
            finding = FakeFinding(
                finding_id="rq-serial-001", rule="xo-max-lines", path="src/big.ts"
            )
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

        import bluei.engine.refactor_queue as rq_mod

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
        test_refactor_queue_refusal_leaves_queue_byte_identical,
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
