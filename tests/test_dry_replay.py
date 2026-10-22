"""Tests for Dry Replay (ADR-0011) — non-mutating evidence collection.

Covers T6.7 of the alpha.2 build: HIT / FAILURE / MISS outcomes, cap
enforcement, winner exclusion, governance exclusion, worktree-unchanged
guarantee, and the safe-mode skip guard inside _process_one_issue.
"""

from __future__ import annotations

import json
from pathlib import Path

from bluei.engine.dry_replay import (
    collect_dry_replay_candidates,
    run_dry_replay,
)
from bluei.engine.jsonl import append_jsonl
from bluei.engine.pattern_store import FixPattern, FixPatternStore


def _store_from_jsonl(tmp_path: Path, *patterns: FixPattern) -> FixPatternStore:
    """Build a store by writing the patterns directly to JSONL.

    Bypasses :meth:`FixPatternStore.append`, which deduplicates by rule +
    normalized ``before_snippet`` and regenerates ``pattern_id`` — we want
    the IDs and snippets we supplied preserved verbatim.
    """
    store_path = tmp_path / "patterns.jsonl"
    for p in patterns:
        append_jsonl(store_path, p.to_dict())
    return FixPatternStore(store_path)


def _make_pattern(
    pid: str,
    rule: str = "trailing-whitespace",
    language: str = "python",
    before: str = "x = 1   ",
    after: str = "x = 1",
) -> FixPattern:
    return FixPattern(
        pattern_id=pid,
        rule=rule,
        language=language,
        file_path="src/app.py",
        before_snippet=before,
        after_snippet=after,
        diff_patch="",
    )


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# collect_dry_replay_candidates
# ---------------------------------------------------------------------------


def test_collect_returns_matched_pattern_excluding_winner(tmp_path, make_finding):
    # Two patterns with identical normalized before-snippet → second one is
    # what we want to dry-replay when the first is the winner.
    winner = _make_pattern("fp-winner")
    alt = _make_pattern("fp-alt")
    store = _store_from_jsonl(tmp_path, winner, alt)

    finding = make_finding(rule="trailing-whitespace", snippet="x = 1   ")

    cands = collect_dry_replay_candidates(
        finding, store, {}, winner_pattern_id="fp-winner"
    )
    pids = {c.pattern_id for c in cands}
    assert "fp-winner" not in pids
    assert "fp-alt" in pids


def test_collect_includes_paused_for_evidence_recovery(tmp_path, make_finding):
    """ADR-0011: Dry Replay does NOT exclude PAUSED patterns — evidence
    collection is the recovery mechanism for auto-re-promote (ADR-0012)."""
    p = _make_pattern("fp-paused")
    store = _store_from_jsonl(tmp_path, p)
    finding = make_finding(rule="trailing-whitespace", snippet="x = 1   ")

    governance = {"pattern:fp-paused": "paused"}
    cands = collect_dry_replay_candidates(finding, store, governance)
    assert len(cands) == 1  # PAUSED pattern IS included for evidence recovery
    assert cands[0].pattern_id == "fp-paused"


def test_collect_dedupes_same_pattern_across_lookups(tmp_path, make_finding):
    p = _make_pattern("fp-dup")
    store = _store_from_jsonl(tmp_path, p)
    finding = make_finding(rule="trailing-whitespace", snippet="x = 1   ")

    cands = collect_dry_replay_candidates(finding, store, {})
    assert len(cands) == 1
    assert cands[0].pattern_id == "fp-dup"


# ---------------------------------------------------------------------------
# run_dry_replay outcomes
# ---------------------------------------------------------------------------


def _setup_worktree(tmp_path: Path, content: str) -> Path:
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "app.py").write_text(content)
    return worktree


def test_run_dry_replay_hit_outcome_and_winner_restored(tmp_path, make_finding):
    original = "x = 1   \n"
    winner = "x = 1\n"
    worktree = _setup_worktree(tmp_path, original)

    pattern = _make_pattern("fp-hit", before="x = 1   ", after="x = 1")
    store = _store_from_jsonl(tmp_path, pattern)
    finding = make_finding(
        rule="trailing-whitespace", path="src/app.py", snippet="x = 1   "
    )

    cands = collect_dry_replay_candidates(finding, store, {})
    dr_path = tmp_path / "dry_replay.jsonl"

    count = run_dry_replay(
        finding,
        cands,
        worktree,
        original_content=original,
        winner_content=winner,
        baseline_checks={},
        log_file=tmp_path / "log.txt",
        run_id="run-1",
        dry_replay_path=dr_path,
    )

    assert count == 1
    recs = _read_records(dr_path)
    assert len(recs) == 1
    assert recs[0]["would_have_outcome"] == "HIT"
    assert recs[0]["pattern_id"] == "fp-hit"
    assert recs[0]["run_id"] == "run-1"
    assert recs[0]["finding_id"] == finding.finding_id
    assert recs[0]["rule"] == finding.rule
    assert recs[0]["validation_commands_passed"] == []
    # Worktree restored to winner content
    assert (worktree / "src" / "app.py").read_text() == winner


def test_run_dry_replay_miss_when_snippet_absent(tmp_path, make_finding):
    original = "completely different text\n"
    winner = original  # no actual fix applied
    worktree = _setup_worktree(tmp_path, original)

    pattern = _make_pattern("fp-miss", before="x = 1   ", after="x = 1")
    store = _store_from_jsonl(tmp_path, pattern)
    finding = make_finding(
        rule="trailing-whitespace", path="src/app.py", snippet="x = 1   "
    )

    cands = collect_dry_replay_candidates(finding, store, {})
    dr_path = tmp_path / "dry_replay.jsonl"

    run_dry_replay(
        finding,
        cands,
        worktree,
        original_content=original,
        winner_content=winner,
        baseline_checks={},
        log_file=tmp_path / "log.txt",
        run_id="run-2",
        dry_replay_path=dr_path,
    )

    recs = _read_records(dr_path)
    assert recs[0]["would_have_outcome"] == "MISS"
    assert (worktree / "src" / "app.py").read_text() == winner


def test_run_dry_replay_failure_when_validation_fails(tmp_path, make_finding):
    original = "x = 1   \n"
    winner = "x = 1\n"
    worktree = _setup_worktree(tmp_path, original)

    pattern = _make_pattern("fp-fail", before="x = 1   ", after="x = 1")
    store = _store_from_jsonl(tmp_path, pattern)
    finding = make_finding(
        rule="trailing-whitespace", path="src/app.py", snippet="x = 1   "
    )

    cands = collect_dry_replay_candidates(finding, store, {})
    dr_path = tmp_path / "dry_replay.jsonl"

    # baseline check that always fails
    failing_checks = {"lint": "false"}

    run_dry_replay(
        finding,
        cands,
        worktree,
        original_content=original,
        winner_content=winner,
        baseline_checks=failing_checks,
        log_file=tmp_path / "log.txt",
        run_id="run-3",
        dry_replay_path=dr_path,
    )

    recs = _read_records(dr_path)
    assert recs[0]["would_have_outcome"] == "FAILURE"
    assert (worktree / "src" / "app.py").read_text() == winner


def test_run_dry_replay_cap_enforcement(tmp_path, make_finding):
    """Cap is applied by run_dry_replay over the candidate list — it does
    not depend on lookup semantics (which dedupe by rule + before-snippet).
    We feed 25 hand-built candidates directly and assert only 20 are run.
    """
    original = "x = 1   \n"
    winner = "x = 1\n"
    worktree = _setup_worktree(tmp_path, original)

    cands = [
        _make_pattern(
            f"fp-cap-{i}",
            before="x = 1   ",
            after=f"x = {i}",
        )
        for i in range(25)
    ]

    finding = make_finding(
        rule="trailing-whitespace", path="src/app.py", snippet="x = 1   "
    )
    dr_path = tmp_path / "dry_replay.jsonl"

    count = run_dry_replay(
        finding,
        cands,
        worktree,
        original_content=original,
        winner_content=winner,
        baseline_checks={},
        log_file=tmp_path / "log.txt",
        run_id="run-cap",
        dry_replay_path=dr_path,
        cap=20,
    )
    assert count == 20
    recs = _read_records(dr_path)
    assert len(recs) == 20
    assert (worktree / "src" / "app.py").read_text() == winner


def test_run_dry_replay_worktree_unchanged_after_multiple_candidates(
    tmp_path, make_finding
):
    original = "x = 1   \nx = 2   \n"
    winner = "x = 1\nx = 2\n"
    worktree = _setup_worktree(tmp_path, original)

    cands = [
        _make_pattern("fp-a", before="x = 1   ", after="x = 1"),
        _make_pattern("fp-b", before="x = 2   ", after="x = 2"),
    ]

    finding = make_finding(
        rule="trailing-whitespace", path="src/app.py", snippet="x = 1   "
    )
    dr_path = tmp_path / "dry_replay.jsonl"

    run_dry_replay(
        finding,
        cands,
        worktree,
        original_content=original,
        winner_content=winner,
        baseline_checks={},
        log_file=tmp_path / "log.txt",
        run_id="run-multi",
        dry_replay_path=dr_path,
    )

    # Each candidate is checkpointed/restored; final state is the winner
    assert (worktree / "src" / "app.py").read_text() == winner


# ---------------------------------------------------------------------------
# Safe-mode skip — verify the guard condition inside _process_one_issue
# ---------------------------------------------------------------------------


def test_dry_replay_safe_mode_guard_pinned():
    """Pins the production Dry Replay guard in ``_process_one_issue``.

    The Dry Replay phase in pr_cycle._process_one_issue is gated by
    ``ctx.learning_mode != "paused"``. When paused, the whole block is
    skipped — no candidate lookup, no file writes. This locks the contract
    that Phase 7 (SPRT) and the safe-mode ADR rely on.

    Source-pinning is used (the ``test_integration_guard_active_only``
    idiom from test_sprt.py) because driving ``_process_one_issue``
    end-to-end requires a full RunContext, worktree, pattern store, and
    GitHub transport — orchestrator-level integration too heavy here. The
    behavioral half below still evaluates the *actual* guard expression
    lifted verbatim from the source, so deleting or weakening the guard
    (e.g. dropping the learning_mode check) fails this test.
    """
    import inspect
    import re
    from types import SimpleNamespace

    from bluei.engine.commands import pr_cycle

    src = inspect.getsource(pr_cycle._process_one_issue)

    # (a) Source pin: the dry-replay block is gated on learning_mode and
    # the guard precedes the dry-replay machinery it protects.
    assert 'ctx.learning_mode != "paused"' in src
    assert "run_dry_replay" in src
    guard_idx = src.index('ctx.learning_mode != "paused"')
    dr_idx = src.index("run_dry_replay")
    assert guard_idx < dr_idx, "guard must precede the dry-replay call it protects"

    # (b) Behavioral half: lift the guard expression verbatim from the
    # production source and evaluate it against constructed inputs —
    # paused means skipped.
    match = re.search(
        r"if \(\s*ctx\.learning_mode != \"paused\".*?\):", src, re.DOTALL
    )
    assert match is not None, "dry-replay guard expression not found"
    guard_expr = match.group(0)[len("if "):].rstrip(":")

    def _guard(learning_mode, original_content, store, path):
        namespace = {
            "ctx": SimpleNamespace(learning_mode=learning_mode),
            "_dr_original_content": original_content,
            "pattern_store": store,
            "finding": SimpleNamespace(path=path),
        }
        return eval(guard_expr, {"__builtins__": {}}, namespace)

    assert not _guard("paused", "orig", object(), "src/x.py")
    assert not _guard("active", None, object(), "src/x.py")
    assert not _guard("active", "orig", None, "src/x.py")
    assert not _guard("active", "orig", object(), "")
    assert _guard("active", "orig", object(), "src/x.py")
