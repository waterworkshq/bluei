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

import subprocess
import sys
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

# Stage1 qualification boundary: an explicit PER_REPO check that really
# executes (available=True evidence) keeps both validation gates passed AND
# qualified; an empty check set never qualifies autonomous repair.
BASELINE_CHECKS = {"baseline": [sys.executable, "-c", "pass"]}


def _git_worktree(tmp_path, name="wt", content="except:\n    pass\n", modified=False):
    """Real git repo standing in for the worktree.

    Stage1 runs a real ``git rev-parse HEAD`` before dispatch and after the
    fix and a real ``git diff --numstat HEAD``, so worktree fixtures must be
    actual repositories. With ``modified=True`` the tracked file carries an
    uncommitted change so the flow passes the no-op check and reaches real
    patch retention — the acceptance boundary that owns positive HIT,
    savings and ledger recording."""
    wt = tmp_path / name
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "main.py").write_text(content)
    for git_args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@bluei.dev"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "initial"],
    ):
        subprocess.run(git_args, cwd=str(wt), check=True, capture_output=True)
    if modified:
        (wt / "src" / "main.py").write_text("except Exception:\n    pass\n")
    return wt


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
        PER_REPO_BASELINE_CHECKS=BASELINE_CHECKS,
        **ctx_overrides,
    )
    return ctx


@contextmanager
def _neutralize_post_fix(worktree_path: Path):
    """Neutralize GitHub/worktree/post-fix phases around a REAL git worktree.

    Stage1 needs a real repository here (head guard + real diff_stats +
    real patch retention); the baseline gate runs the real PER_REPO check in
    it. Dry-replay candidate collection is neutralized — counterfactual
    evidence is not under test; the standalone/cascade ledger wiring is."""
    with ExitStack() as stack:
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle._resolve_github", return_value=False)
        )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle._setup_worktree",
                return_value=_WorktreeSetup(
                    worktree_path=worktree_path, worktree_branch="fix/f001"
                ),
            )
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
        stack.enter_context(
            patch(
                "bluei.engine.dry_replay.collect_dry_replay_candidates",
                return_value=[],
            )
        )
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

    with _neutralize_post_fix(_git_worktree(tmp_path, "wt-hit", modified=True)):
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay",
                return_value=(True, pid),
            ) as mock_replay,
            patch("bluei.engine.lifecycle.apply_cascade_fix") as mock_cascade,
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    # Mainline call-site contract: negative outcomes stay recorded per
    # record_outcome=True, but the positive HIT is deferred (record_success
    # False) — the acceptance boundary owns it after outer validation,
    # closure and the retained patch artifact.
    assert mock_replay.call_args.kwargs["record_outcome"] is True
    assert mock_replay.call_args.kwargs["record_success"] is False

    # exactly one ledger record, standalone-replay shape
    assert len(ctx.ledger_records) == 1
    rec = ctx.ledger_records[0]
    assert rec["via"] == "standalone-replay"
    assert rec["outcome"] == "resolved_deterministic"
    assert rec["final_stage"] == "pattern-replay"
    assert rec["pattern_id"] == pid
    assert rec["finding_id"] == "f001"
    assert rec["cycle"] == "pr-cycle"

    # The retained artifact backs the positive record: a real local-fix
    # patch exists for this issue before the HIT was counted.
    assert Path(issue["verified_fix_patch"]).exists()
    assert ctx.cost_tracker.record_pattern_replay_savings.call_count == 1

    # HIT recorded exactly once on the pattern store (success_count +1,
    # verified stamp) — only the acceptance block records it.
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

    with _neutralize_post_fix(_git_worktree(tmp_path, "wt-short", modified=True)):
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay", return_value=(True, pid)
            ),
            patch("bluei.engine.lifecycle.apply_cascade_fix") as mock_cascade,
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    # cascade never runs — the `if not replay_succeeded` short-circuit
    mock_cascade.assert_not_called()
    # exactly one record for this Finding (accepted standalone HIT)
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

    def fake_replay(**kwargs):
        # record_outcome=True: a real miss records its own MISS internally
        # (ADR-0004 three-way recording) — mirror that so the exactly-once
        # store update below is actually asserted, not assumed.
        if kwargs.get("record_outcome", True):
            store.record_replay_outcome(pid, ReplayOutcome.MISS)
        return (False, None)

    with _neutralize_post_fix(_git_worktree(tmp_path, "wt-miss")):
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay",
                side_effect=fake_replay,
            ) as mock_replay,
            patch(
                "bluei.engine.lifecycle.apply_cascade_fix",
                side_effect=fake_cascade,
            ) as mock_cascade,
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    # standalone used internal three-way recording with record_outcome=True
    # (MISS recording is not gated by record_success — only positives are)
    assert mock_replay.call_args.kwargs["record_outcome"] is True
    assert mock_replay.call_args.kwargs["record_success"] is False

    # cascade received the shared accumulator + ledger_path + cycle
    call_kwargs = mock_cascade.call_args.kwargs
    assert call_kwargs["ledger_records"] is ctx.ledger_records
    assert call_kwargs["cycle"] == "pr-cycle"
    assert call_kwargs["ledger_path"] is not None

    # exactly ONE record total — the cascade's; standalone left none
    assert len(ctx.ledger_records) == 1
    assert ctx.ledger_records[0]["via"] == "cascade"

    # standalone's MISS recorded exactly once on the pattern store
    # (skip_count +1, used stamp; success/failure untouched); the cascade
    # mock records nothing, so any second update would show up here.
    pattern = store.get_pattern(pid)
    assert pattern is not None
    assert pattern.success_count == before_succ
    assert pattern.failure_count == before_fail
    assert pattern.skip_count == before_skip + 1
    assert pattern.last_used_at is not None


# ── exactly-once: standalone-fail→cascade = one record, no standalone update ─
# (covered by test_standalone_miss_defers_to_cascade_exactly_once above; this is
# the explicit end-to-end exactly-once assertion the prompt's DoD calls out.)


def test_standalone_fail_then_cascade_records_exactly_once_on_store(tmp_path):
    """When standalone replay fails (validation failure), the standalone path's
    outcome is recorded internally (record_outcome=True) and the cascade
    records the one authoritative outcome via its own
    try_replay(record_outcome=True). Here the standalone try_replay is mocked
    to a no-record miss; the cascade's record is represented by the fake
    cascade appending one row; the store must reflect exactly one update
    attributable to the cascade only."""
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

    with _neutralize_post_fix(_git_worktree(tmp_path, "wt-failthen")):
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

    with _neutralize_post_fix(_git_worktree(tmp_path, "wt-shape", modified=True)):
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


def test_standalone_hit_record_carries_measured_latency(tmp_path):
    """latency_ms is the measured try_replay duration, not a hardcoded sentinel."""
    from bluei.engine import pattern_replay as _pr

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

    # Warm the resolver cache so the governance gate's _resolve_pattern (run
    # before the timed replay) resolves from cache. Only pr_cycle.time is
    # faked (the replay t0/latency pair); pattern_replay keeps the real
    # clock, so no other in-flow call can consume the side effects.
    _pr._clear_pattern_cache()
    _pr._resolve_pattern(finding, store, tmp_path / "warmup.log")

    with _neutralize_post_fix(_git_worktree(tmp_path, "wt-latency", modified=True)):
        with (
            patch(
                "bluei.engine.commands.pr_cycle.try_replay", return_value=(True, pid)
            ),
            patch("bluei.engine.lifecycle.apply_cascade_fix"),
            patch(
                "bluei.engine.commands.pr_cycle.time",
                new=SimpleNamespace(
                    monotonic=MagicMock(side_effect=[100.0, 100.25])
                ),
            ),
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    rec = ctx.ledger_records[0]
    assert rec["latency_ms"] == 250  # (100.25 - 100.0) * 1000


# ── fail-closed: a pattern-resolution failure must not bypass governance ─────


def test_resolve_failure_fails_closed_skips_standalone_replay(tmp_path):
    """A transient _resolve_pattern failure must not land in the replay
    branch (that branch would run the standalone replay WITHOUT the
    governance check). The replay is skipped entirely and visibly, and the
    cascade/LLM path proceeds (its pattern stage consults governance
    independently)."""
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

    with _neutralize_post_fix(_git_worktree(tmp_path, "wt-resolvefail")):
        with (
            # pr_cycle imports _resolve_pattern function-locally from
            # pattern_replay — patch the source-module attribute.
            patch(
                "bluei.engine.pattern_replay._resolve_pattern",
                side_effect=RuntimeError("transient resolution failure"),
            ),
            patch("bluei.engine.commands.pr_cycle.try_replay") as mock_replay,
            patch("bluei.engine.commands.pr_cycle._append_text") as mock_log,
            patch(
                "bluei.engine.lifecycle.apply_cascade_fix", return_value=True
            ) as mock_cascade,
        ):
            _process_one_issue(ctx, 0, issue, finding, {})  # must not raise

    # fail closed: the standalone replay never ran
    mock_replay.assert_not_called()
    # the skip is visible in the log with the fail-closed reason
    # (_append_text's first arg is the log path; the message is second)
    assert any(
        "pattern-replay-skip" in str(c.args[1])
        and "reason=resolve_failed_fail_closed" in str(c.args[1])
        for c in mock_log.call_args_list
        if len(c.args) > 1
    )
    # the never-attempted candidate must not enter the cascade's attempted set
    assert mock_cascade.call_args.kwargs["attempted_pattern_ids"] is None


def test_governance_skip_excludes_unattempted_candidate_from_cascade_set(tmp_path):
    """attempted_pattern_ids semantics: when the governance gate SKIPS the
    standalone replay, the candidate pattern was never attempted — its id
    must NOT be passed to the cascade (the cascade's pattern stage is free
    to attempt it under its own governance consult)."""
    store = FixPatternStore(tmp_path / "patterns.jsonl")
    pid = store.append(_pattern())
    store._rebuild_from_disk()

    ctx = _build_ctx(
        tmp_path, store, governance_state={f"pattern:{pid}": "paused"}
    )
    finding = _finding(rule="broad-except", refactor_class="cascade_fix")
    issue = {
        "issue_id": "i1",
        "finding_id": "f001",
        "status": "open",
        "rule": "broad-except",
    }

    with _neutralize_post_fix(_git_worktree(tmp_path, "wt-govskip")):
        with (
            patch("bluei.engine.commands.pr_cycle.try_replay") as mock_replay,
            patch(
                "bluei.engine.lifecycle.apply_cascade_fix", return_value=True
            ) as mock_cascade,
        ):
            _process_one_issue(ctx, 0, issue, finding, {})

    # governance-inactive pattern → standalone replay skipped
    mock_replay.assert_not_called()
    # skipped ⇒ never attempted ⇒ the candidate id is NOT in the set
    assert mock_cascade.call_args.kwargs["attempted_pattern_ids"] is None
