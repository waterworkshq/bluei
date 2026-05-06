#!/usr/bin/env python3
"""Tests for route_findings_with_intent orchestrator routing."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sandbox_local_runner.models import Finding


def make_finding(
    finding_id: str,
    rule: str,
    path: str = "src/foo.ts",
    confidence: float = 0.95,
    safe_to_autofix: bool = False,
) -> Finding:
    return Finding(
        finding_id=finding_id,
        repo="test-repo",
        path=path,
        line=10,
        rule=rule,
        snippet="example snippet",
        confidence=confidence,
        quick_win=False,
        safe_to_autofix=safe_to_autofix,
    )


def seed_findings_file(path: Path, findings: list[Finding]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for finding in findings:
            f.write(json.dumps(finding.as_dict(), sort_keys=True) + "\n")


def test_route_findings_with_intent_autofix_safe_bucket():
    from sandbox_local_runner.orchestrator import route_findings_with_intent

    finding = make_finding("safe-1", "ruff-b904", safe_to_autofix=True)
    routed = route_findings_with_intent([finding], confidence_threshold=0.8)

    assert [f.finding_id for f in routed["autofix_safe"]] == ["safe-1"]
    assert routed["refactor_queue"] == []
    assert routed["human_review"] == []
    assert routed["skipped"] == []


def test_route_findings_with_intent_skips_low_confidence():
    from sandbox_local_runner.orchestrator import route_findings_with_intent

    finding = make_finding("skip-1", "ruff-b904", confidence=0.4, safe_to_autofix=True)
    routed = route_findings_with_intent([finding], confidence_threshold=0.8)

    assert [f.finding_id for f in routed["skipped"]] == ["skip-1"]


def test_route_findings_with_intent_persists_refactor_work_for_small_refactor():
    from sandbox_local_runner.orchestrator import route_findings_with_intent
    from sandbox_local_runner.state import load_refactor_work

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        findings_file = tmp / "findings.jsonl"
        worktree = tmp / "repo"
        worktree.mkdir()
        target = worktree / "src" / "big.ts"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")

        finding = make_finding("refactor-1", "xo-max-lines", path="src/big.ts")
        seed_findings_file(findings_file, [finding])

        routed = route_findings_with_intent(
            [finding],
            confidence_threshold=0.8,
            findings_file=findings_file,
            worktree_path=worktree,
        )

        assert len(routed["refactor_queue"]) == 1
        queue_item = routed["refactor_queue"][0]
        assert queue_item["queued_work_id"] is None
        assert queue_item["refactor_work"].phase.value == "planning"

        persisted = load_refactor_work("refactor-1", findings_file)
        assert persisted is not None
        assert persisted.phase.value == "planning"


def test_route_findings_with_intent_enqueues_large_refactor_for_human_review():
    from sandbox_local_runner.orchestrator import route_findings_with_intent
    from sandbox_local_runner.state import load_refactor_work

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        findings_file = tmp / "findings.jsonl"
        worktree = tmp / "repo"
        worktree.mkdir()
        target = worktree / "src" / "huge.ts"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(f"line {i}" for i in range(5001)), encoding="utf-8")

        finding = make_finding("refactor-2", "xo-max-lines", path="src/huge.ts")
        seed_findings_file(findings_file, [finding])

        routed = route_findings_with_intent(
            [finding],
            confidence_threshold=0.8,
            findings_file=findings_file,
            worktree_path=worktree,
        )

        assert len(routed["refactor_queue"]) == 1
        queue_item = routed["refactor_queue"][0]
        assert queue_item["queued_work_id"]
        assert queue_item["refactor_work"].phase.value == "aborted"
        assert queue_item["refactor_work"].needs_human_review is True

        persisted = load_refactor_work("refactor-2", findings_file)
        assert persisted is not None
        assert persisted.phase.value == "aborted"
        assert persisted.needs_human_review is True


def test_route_findings_with_intent_routes_claude_fix_to_human_review_bucket():
    from sandbox_local_runner.orchestrator import route_findings_with_intent

    finding = make_finding("human-1", "type-explicit-any")
    routed = route_findings_with_intent([finding], confidence_threshold=0.8)

    assert [f.finding_id for f in routed["human_review"]] == ["human-1"]
