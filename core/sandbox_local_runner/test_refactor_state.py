#!/usr/bin/env python3
"""Tests for RefactorWork persistence helpers in state.py."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sandbox_local_runner.reforge import RefactorWork


def seed_findings_file(path: Path, finding_id: str) -> None:
    payload = {
        "finding_id": finding_id,
        "repo": "test-repo",
        "path": "src/foo.ts",
        "line": 10,
        "rule": "xo-max-lines",
        "snippet": "example snippet",
        "confidence": 0.95,
        "quick_win": False,
        "safe_to_autofix": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def test_save_and_load_refactor_work_round_trip():
    from sandbox_local_runner.state import load_refactor_work, save_refactor_work

    with tempfile.TemporaryDirectory() as tmpdir:
        findings_file = Path(tmpdir) / "findings.jsonl"
        seed_findings_file(findings_file, "state-1")

        rw = RefactorWork(finding_id="state-1")
        rw.mark_splitting(["part1.ts", "part2.ts"], original_line_count=3200)
        rw.written_files = {"part1.ts"}

        assert save_refactor_work("state-1", findings_file, rw) is True

        loaded = load_refactor_work("state-1", findings_file)
        assert loaded is not None
        assert loaded.phase.value == "splitting"
        assert loaded.planned_targets == ["part1.ts", "part2.ts"]
        assert loaded.original_line_count == 3200
        assert loaded.written_files == {"part1.ts"}


def test_get_pending_refactor_work_filters_done_and_aborted():
    from sandbox_local_runner.state import get_pending_refactor_work, save_refactor_work

    with tempfile.TemporaryDirectory() as tmpdir:
        findings_file = Path(tmpdir) / "findings.jsonl"
        findings_file.parent.mkdir(parents=True, exist_ok=True)

        base_records = []
        for idx in range(3):
            base_records.append({
                "finding_id": f"pending-{idx}",
                "repo": "test-repo",
                "path": f"src/{idx}.ts",
                "line": 10,
                "rule": "xo-max-lines",
                "snippet": "example snippet",
                "confidence": 0.95,
                "quick_win": False,
                "safe_to_autofix": False,
            })
        with findings_file.open("w", encoding="utf-8") as f:
            for record in base_records:
                f.write(json.dumps(record, sort_keys=True) + "\n")

        planning = RefactorWork(finding_id="pending-0")
        validating = RefactorWork(finding_id="pending-1")
        validating.mark_validating("abc123")
        done = RefactorWork(finding_id="pending-2")
        done.mark_done()

        assert save_refactor_work("pending-0", findings_file, planning) is True
        assert save_refactor_work("pending-1", findings_file, validating) is True
        assert save_refactor_work("pending-2", findings_file, done) is True

        pending = get_pending_refactor_work(findings_file)
        pending_ids = {item["finding_id"] for item in pending}
        assert pending_ids == {"pending-0", "pending-1"}
