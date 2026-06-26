"""Slice 4 (T1.4/T1.5): pr_cycle standalone deferral + ledger_records accumulator.

Verifies exactly-once recording per Finding (ADR-0004) and that standalone-replay
HITs + cascade resolutions both feed the shared ``ctx.ledger_records`` accumulator
that finalize reads (ADR-0005).

The standalone-replay block lives deep in ``_process_one_issue``. Rather than
mock the entire post-fix verification/PR machinery, we neutralize the post-fix
phases via ``diff_stats``→(0,0) + ``verify_fix_closed``→closed, which hits the
early "verified without changes" return (pr_cycle.py ~1042). That exercises the
real replay/cascade wiring and asserts on ``ctx.ledger_records`` + the pattern
store — exactly the surfaces this slice changed.
"""

from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.context import RunContext
from bluei.engine.commands.pr_cycle import _process_one_issue, _WorktreeSetup
from bluei.engine.models import Finding
from bluei.engine.pattern_store import FixPattern, FixPatternStore, ReplayOutcome
from bluei.engine.verify import VerificationResult


# ── helpers ──────────────────────────────────────────────────────────────────


def _pattern(**overrides):
    data = {
        "pattern_id": "",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/example.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "--- a\n+++ a\n-except:\n+except Exception:\n",
        "confidence": 0.95,
        "success_count": 0,
        "failure_count": 0,
        "skip_count": 0,
        "source": "claude",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_used_at": None,
        "last_verified_at": None,
        "source_finding_ids": ["f001"],
    }
    data.update(overrides)
    return FixPattern(**data)


def _args(**overrides):
    defaults = dict(
        repo_path="/tmp/repo",
        dry_run=True,
        live_github_actions=False,
        allow_main_commit=False,
        max_prs_per_run=2,
        open_prs_cap=5,
        issue_confidence_threshold=0.5,
        max_fix_attempts_per_issue=3,
        batch_pr_enabled=False,
        fix_engine="deterministic",
        deterministic_only=False,
        max_files_changed=5,
        max_loc_diff=200,
        allow_unchanged_baseline_failures=True,
        claude_cmd_template="",
        pattern_store_path=None,
        batch_state_file="/tmp/batches.jsonl",
        max_duplicate_prs_threshold=3,
        no_auto_close_duplicate_prs=False,
        workspace=None,
        run_phase="pr-cycle",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _finding(**overrides):
    defaults = dict(
        finding_id="f001",
        repo="test-repo",
        path="src/main.py",
        line=42,
        rule="broad-except",
        snippet="except:\n    pass",
        confidence=0.85,
        quick_win=True,
        safe_to_autofix=True,
        fix_attempts=0,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def _cost_tracker():
    return MagicMock(
        exceeded_limit=MagicMock(return_value=False),
        cycle_total=MagicMock(return_value=0.0),
        estimate_invocation_cost=MagicMock(return_value=Decimal("0.05")),
        record_pattern_replay_savings=MagicMock(),
        record_invocation=MagicMock(),
    )


def _build_ctx(tmp_path, store, **ctx_overrides):
    ctx = RunContext(
        args=_args(),
        repo_path=tmp_path / "repo",
        state_file=tmp_path / "state" / "status.json",
        log_file=tmp_path / "run.log",
        findings_file=tmp_path / "findings.jsonl",
        worktree_root=tmp_path / "worktrees",
        cost_tracker=_cost_tracker(),
        pattern_store=store,
        PER_REPO_BASELINE_CHECKS={},
        **ctx_overrides,
    )
    return ctx


@contextmanager
def _neutralize_post_fix():
    """Neutralize GitHub/worktree/post-fix phases so the replay block runs and
    the function returns via the 'verified without changes' path (diff_stats→(0,0)
    + verify_fix_closed→closed)."""
    with ExitStack() as stack:
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle._resolve_github", return_value=False)
        )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle._setup_worktree",
                return_value=_WorktreeSetup(
                    worktree_path=Path("/tmp/wt"), worktree_branch="fix/f001"
                ),
            )
        )
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle.diff_stats", return_value=(0, 0))
        )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle.verify_fix_closed",
                return_value=VerificationResult(
                    passed=True, is_closed=True, message=""
                ),
            )
        )
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
        )
        stack.enter_context(patch("bluei.engine.commands.pr_cycle.set_issue_status"))
        stack.enter_context(patch("bluei.engine.commands.pr_cycle._append_text"))
        stack.enter_context(patch("bluei.engine.commands.pr_cycle.remove_worktree"))
        yield


# ── (d) RunContext default ───────────────────────────────────────────────────


def test_runcontext_defaults_ledger_records_to_empty_list():
    ctx = RunContext(args=SimpleNamespace())
    assert ctx.ledger_records == []
    # independent default per instance
    other = RunContext(args=SimpleNamespace())
    other.ledger_records.append({"x": 1})
    assert ctx.ledger_records == []


# ── (a) standalone HIT → exactly one standalone-replay record + HIT on store ─


def test_standalone_hit_emits_one_ledger_record_and_records_hit(tmp_path):
    store = FixPatternStore(tmp_path / "patterns.jsonl")
    pid = store.append(_pattern())
    store._rebuild_from_disk()

    ctx = _build_ctx(tmp_path, store)
    finding = _finding(rule="broad-except")
    issue = {
        "issue_id": "i1",
        "finding_id": "f001",
        "status": "open",
        "rule": "broad-except",
    }

    before = store.get_pattern(pid)
    assert before is not None
    succ_before = before.success_count

    with _neutralize_post_fix():
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay", return_value=(True, pid)
            ) as mock_replay,
            patch("bluei.engine.lifecycle.apply_cascade_fix") as mock_cascade,
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    # record_outcome=False passed to standalone try_replay (defers internal recording)
    assert mock_replay.call_args.kwargs["record_outcome"] is False

    # exactly one ledger record, standalone-replay shape
    assert len(ctx.ledger_records) == 1
    rec = ctx.ledger_records[0]
    assert rec["via"] == "standalone-replay"
    assert rec["outcome"] == "resolved_deterministic"
    assert rec["final_stage"] == "pattern-replay"
    assert rec["pattern_id"] == pid
    assert rec["finding_id"] == "f001"
    assert rec["cycle"] == "pr-cycle"

    # HIT recorded exactly once on the pattern store (success_count +1, verified stamp)
    pattern = store.get_pattern(pid)
    assert pattern is not None
    assert pattern.success_count == succ_before + 1
    assert pattern.last_verified_at is not None


# ── (c) standalone HIT short-circuits cascade → exactly one record total ────


def test_standalone_hit_short_circuits_cascade_one_record_total(tmp_path):
    store = FixPatternStore(tmp_path / "patterns.jsonl")
    pid = store.append(_pattern())
    store._rebuild_from_disk()

    ctx = _build_ctx(tmp_path, store)
    finding = _finding(rule="broad-except")
    issue = {
        "issue_id": "i1",
        "finding_id": "f001",
        "status": "open",
        "rule": "broad-except",
    }

    with _neutralize_post_fix():
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay", return_value=(True, pid)
            ),
            patch("bluei.engine.lifecycle.apply_cascade_fix") as mock_cascade,
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    # cascade never runs — the `if not replay_succeeded` short-circuit
    mock_cascade.assert_not_called()
    # exactly one record for this Finding
    assert len(ctx.ledger_records) == 1
    assert ctx.ledger_records[0]["via"] == "standalone-replay"


# ── (b) standalone miss → no standalone record, cascade owns the one record ──


def test_standalone_miss_defers_to_cascade_exactly_once(tmp_path):
    store = FixPatternStore(tmp_path / "patterns.jsonl")
    pid = store.append(_pattern())
    store._rebuild_from_disk()

    ctx = _build_ctx(tmp_path, store)
    # cascade branch requires refactor_class == "cascade_fix"
    finding = _finding(rule="broad-except", refactor_class="cascade_fix")
    issue = {
        "issue_id": "i1",
        "finding_id": "f001",
        "status": "open",
        "rule": "broad-except",
    }

    before = store.get_pattern(pid)
    assert before is not None
    before_succ, before_skip, before_fail = (
        before.success_count,
        before.skip_count,
        before.failure_count,
    )

    def fake_cascade(worktree, fnd, log, **kwargs):
        # mimic the real cascade emission contract (ADR-0005): append one
        # resolution record into the shared accumulator it was handed.
        kwargs["ledger_records"].append(
            {
                "cycle": kwargs.get("cycle"),
                "finding_id": fnd.finding_id,
                "rule": fnd.rule,
                "stages_tried": ["pattern-replay"],
                "final_stage": "pattern-replay",
                "outcome": "resolved_deterministic",
                "via": "cascade",
                "pattern_id": pid,
                "latency_ms": 12,
                "timestamp": "2026-06-26T00:00:00+00:00",
            }
        )
        return True

    with _neutralize_post_fix():
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay", return_value=(False, None)
            ) as mock_replay,
            patch(
                "bluei.engine.lifecycle.apply_cascade_fix",
                side_effect=fake_cascade,
            ) as mock_cascade,
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    # standalone deferred recording
    assert mock_replay.call_args.kwargs["record_outcome"] is False

    # cascade received the shared accumulator + ledger_path + cycle
    call_kwargs = mock_cascade.call_args.kwargs
    assert call_kwargs["ledger_records"] is ctx.ledger_records
    assert call_kwargs["cycle"] == "pr-cycle"
    assert call_kwargs["ledger_path"] is not None

    # exactly ONE record total — the cascade's; standalone left none
    assert len(ctx.ledger_records) == 1
    assert ctx.ledger_records[0]["via"] == "cascade"

    # standalone recorded nothing on the pattern store (miss/fail → no update)
    pattern = store.get_pattern(pid)
    assert pattern is not None
    assert pattern.success_count == before_succ
    assert pattern.skip_count == before_skip
    assert pattern.failure_count == before_fail


# ── exactly-once: standalone-fail→cascade = one record, no standalone update ─
# (covered by test_standalone_miss_defers_to_cascade_exactly_once above; this is
# the explicit end-to-end exactly-once assertion the prompt's DoD calls out.)


def test_standalone_fail_then_cascade_records_exactly_once_on_store(tmp_path):
    """When standalone replay fails (validation failure), the standalone path
    records nothing (record_outcome=False) and the cascade records the one
    authoritative outcome via its own try_replay(record_outcome=True). Here the
    cascade's record is represented by the fake cascade appending one row; the
    store must reflect exactly one update attributable to the cascade only."""
    store = FixPatternStore(tmp_path / "patterns.jsonl")
    pid = store.append(_pattern())
    store._rebuild_from_disk()

    ctx = _build_ctx(tmp_path, store)
    finding = _finding(rule="broad-except", refactor_class="cascade_fix")
    issue = {
        "issue_id": "i1",
        "finding_id": "f001",
        "status": "open",
        "rule": "broad-except",
    }

    before = store.get_pattern(pid)
    assert before is not None
    succ_before = before.success_count

    def fake_cascade(worktree, fnd, log, **kwargs):
        # the real cascade calls try_replay(record_outcome=True) → one store update
        store.record_replay_outcome(pid, ReplayOutcome.HIT)
        kwargs["ledger_records"].append(
            {
                "cycle": kwargs.get("cycle"),
                "finding_id": fnd.finding_id,
                "rule": fnd.rule,
                "stages_tried": ["pattern-replay"],
                "final_stage": "pattern-replay",
                "outcome": "resolved_deterministic",
                "via": "cascade",
                "pattern_id": pid,
                "latency_ms": 12,
                "timestamp": "2026-06-26T00:00:00+00:00",
            }
        )
        return True

    with _neutralize_post_fix():
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay", return_value=(False, None)
            ),
            patch(
                "bluei.engine.lifecycle.apply_cascade_fix",
                side_effect=fake_cascade,
            ),
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    # exactly one ledger record (cascade), exactly one store HIT (cascade only)
    assert len(ctx.ledger_records) == 1
    assert ctx.ledger_records[0]["via"] == "cascade"
    pattern = store.get_pattern(pid)
    assert pattern is not None
    assert pattern.success_count == succ_before + 1


# ── record-shape parity: standalone-HIT and cascade records are field-identical ─
# (Phase-2 watch item: a single aggregator must handle both. Assert key sets match.)


def test_standalone_and_cascade_record_shapes_are_field_identical(tmp_path):
    store = FixPatternStore(tmp_path / "patterns.jsonl")
    pid = store.append(_pattern())
    store._rebuild_from_disk()

    ctx = _build_ctx(tmp_path, store)
    finding = _finding(rule="broad-except")
    issue = {
        "issue_id": "i1",
        "finding_id": "f001",
        "status": "open",
        "rule": "broad-except",
    }

    cascade_shape = {
        "cycle": "pr-cycle",
        "finding_id": "f001",
        "rule": "broad-except",
        "stages_tried": ["pattern-replay"],
        "final_stage": "pattern-replay",
        "outcome": "resolved_deterministic",
        "via": "cascade",
        "pattern_id": pid,
        "latency_ms": 12,
        "timestamp": "2026-06-26T00:00:00+00:00",
    }

    with _neutralize_post_fix():
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay", return_value=(True, pid)
            ),
            patch("bluei.engine.lifecycle.apply_cascade_fix"),
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    assert len(ctx.ledger_records) == 1
    standalone_keys = set(ctx.ledger_records[0].keys())
    cascade_keys = set(cascade_shape.keys())
    assert standalone_keys == cascade_keys, (
        f"shape drift — standalone={standalone_keys} cascade={cascade_keys}"
    )
