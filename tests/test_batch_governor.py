"""Tests for batch-path Model Governor wiring (Phase 2, T2.3).

Covers:
  * AC-P3-2 — with the 4 Governor kwargs threaded into ``_apply_single_fix``,
    a recommendation row is recorded to the ledger AND the resolved template
    (carrying ``--model`` from discovery) reaches ``apply_claude_fix`` via
    ``ClaudeFixRequest.claude_cmd_template``.
  * Default-guard — without the kwargs (the existing batch callers), no ledger
    file is created and no exception is raised (existing batch tests unchanged).
"""

from __future__ import annotations

import json
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bluei.engine.batch_execution import _apply_single_fix
from bluei.engine.model_discovery import ResolvedModel
from bluei.engine.model_governor import ModelTier, TierRecommendation, select_tier


# --- fakes (mirror test_governor_wiring.py) ---


class _FakeDiscovery:
    """Minimal ModelDiscovery Protocol implementation for wiring tests."""

    def __init__(self, models):
        self._models = models

    def discover(self):
        return list(self._models)


def _tier0_claude_model():
    return ResolvedModel(
        tier=ModelTier.TIER_0,
        backend="claude",
        model_id="claude-3-5-haiku",
        input_per_1k=0.25,
        output_per_1k=1.25,
    )


def _fake_select_tier_0(finding, coverage):
    return TierRecommendation(
        tier=ModelTier.TIER_0, rationale="test-tier-0", coverage=coverage
    )


def _read_ledger(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _make_args():
    return SimpleNamespace(
        fix_engine="claude",
        claude_cmd_template="claude --dangerously-skip-permissions --print",
        max_files_changed=10,
        max_loc_diff=500,
    )


# Stage 3: the model path requires the chosen validation-baseline RECORDS
# (rc outcomes) — the configured checks dict never reaches the request
# (PR14 leak hardening): ClaudeFixRequest gets {} checks + private statuses.
_BASELINE_RESULTS = {"lint": {"rc": 0, "ok": True}}


class TestBatchGovernorWiring:
    """AC-P3-2: the batch path records + resolves via the Governor helper."""

    def test_ledger_recorded_and_template_carries_model(self, tmp_path, make_finding):
        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        ledger = tmp_path / "governor_recommendations.jsonl"
        log_file = tmp_path / "log.txt"
        args = _make_args()
        discovery = _FakeDiscovery([_tier0_claude_model()])

        captured = {}

        def _capture(req):
            captured["tmpl"] = req.claude_cmd_template
            captured["checks"] = req.baseline_checks
            captured["statuses"] = req.baseline_statuses
            return (0, "claude output", None)

        with patch("bluei.engine.lifecycle.apply_claude_fix", side_effect=_capture):
            _apply_single_fix(
                finding=finding,
                worktree_path=tmp_path,
                repo_path=tmp_path,
                args=args,
                log_file=log_file,
                selection_fn=_fake_select_tier_0,
                governor_ledger_path=ledger,
                run_id="run-batch-test",
                discovery=discovery,
                baseline_results=_BASELINE_RESULTS,
            )

        # AC-P3-2a: ledger row recorded for this finding
        rows = _read_ledger(ledger)
        assert len(rows) == 1
        assert rows[0]["finding_id"] == finding.finding_id
        assert rows[0]["run_id"] == "run-batch-test"
        assert rows[0]["tier"] == "tier-0"

        # AC-P3-2b: resolved template carries --model from discovery
        assert "--model claude-3-5-haiku" in captured["tmpl"]
        # Stage 3 leak contract: no check argv/names in the request — empty
        # checks + private rc statuses only.
        assert captured["checks"] == {}
        assert captured["statuses"] == [0]

    def test_no_kwargs_no_ledger_no_exception(self, tmp_path, make_finding):
        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        ledger = tmp_path / "governor_recommendations.jsonl"
        log_file = tmp_path / "log.txt"
        args = _make_args()

        with patch(
            "bluei.engine.lifecycle.apply_claude_fix",
            return_value=(0, "claude output", None),
        ):
            # No Governor kwargs → defaults; must not raise, must not record.
            result = _apply_single_fix(
                finding=finding,
                worktree_path=tmp_path,
                repo_path=tmp_path,
                args=args,
                log_file=log_file,
                baseline_results=_BASELINE_RESULTS,
            )

        assert not ledger.exists()
        assert result is not None

    def test_guard_delegation_none_ledger_with_selection_fn(self, tmp_path, make_finding):
        # The deleted local ``if governor_ledger_path is not None:`` wrapper
        # used to intercept exactly this shape (selection_fn present, ledger
        # None). Now _apply_single_fix calls resolve_governed_model
        # unconditionally and the helper's internal guard owns the no-op:
        # no exception, no ledger file, and apply_claude_fix receives the
        # base template unchanged. (test_no_kwargs_no_ledger_no_exception
        # covers both-None; this covers ledger-None-with-selection-fn.)
        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        ledger = tmp_path / "governor_recommendations.jsonl"
        log_file = tmp_path / "log.txt"
        args = _make_args()

        captured = {}

        def _capture(req):
            captured["tmpl"] = req.claude_cmd_template
            return (0, "claude output", None)

        with patch("bluei.engine.lifecycle.apply_claude_fix", side_effect=_capture):
            result = _apply_single_fix(
                finding=finding,
                worktree_path=tmp_path,
                repo_path=tmp_path,
                args=args,
                log_file=log_file,
                selection_fn=select_tier,
                governor_ledger_path=None,
                run_id="run-batch-guard",
                discovery=None,
                baseline_results=_BASELINE_RESULTS,
            )

        assert not ledger.exists()
        assert result is not None
        assert captured["tmpl"] == args.claude_cmd_template

    def test_none_ledger_calls_helper_directly(self, tmp_path, make_finding):
        # Delegation proof, not just outcome: the deleted local
        # ``if governor_ledger_path is not None:`` wrapper would have kept
        # resolve_governed_model UNCALLED under ledger=None. Production now
        # calls the helper unconditionally (the helper's own guard owns the
        # no-op), so the mock must be hit exactly once even with
        # governor_ledger_path=None. _apply_single_fix imports the function
        # inside its body at call time, so patching the source module
        # attribute intercepts it.
        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        log_file = tmp_path / "log.txt"
        args = _make_args()

        gov_mock = MagicMock(
            return_value=(None, None, args.claude_cmd_template, "claude-sonnet-4")
        )

        with patch("bluei.engine.lifecycle.apply_claude_fix", return_value=(0, "out", None)):
            with patch(
                "bluei.engine.model_governor.resolve_governed_model", gov_mock
            ):
                result = _apply_single_fix(
                    finding=finding,
                    worktree_path=tmp_path,
                    repo_path=tmp_path,
                    args=args,
                    log_file=log_file,
                    selection_fn=select_tier,
                    governor_ledger_path=None,
                    run_id="run-batch-delegation",
                    discovery=None,
                    baseline_results=_BASELINE_RESULTS,
                )

        gov_mock.assert_called_once()
        assert result is not None


class TestBatchCostLedger:
    """D5-F4: batch Claude invocations reach the cost ledger via a threaded
    CostTracker (same threading pattern as the Governor kwargs). Without a
    tracker the batch path is unchanged — no crash, no rows."""

    def test_threaded_tracker_records_invocation_with_discovered_rates(
        self, tmp_path, make_finding
    ):
        from bluei.engine.batch_execution import apply_batch_fixes
        from bluei.engine.cost_tracker import CostTracker
        from bluei.engine.models import BatchGroup

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        ledger = tmp_path / "governor_recommendations.jsonl"
        cost_log = tmp_path / "cost_log.jsonl"
        log_file = tmp_path / "log.txt"
        args = _make_args()
        discovery = _FakeDiscovery([_tier0_claude_model()])
        tracker = CostTracker(log_path=cost_log)
        batch = BatchGroup(
            batch_id="batch-test-b904",
            rule_pattern="ruff-b904",
            group_by="rule",
            findings=[finding],
            issues=[],
        )

        # Driven through apply_batch_fixes (not _apply_single_fix directly)
        # so the cost_tracker PROPAGATION through the batch chain is what
        # this test pins.
        with patch(
            "bluei.engine.lifecycle.apply_claude_fix",
            return_value=(0, "claude output", None),
        ):
            apply_batch_fixes(
                batch,
                worktree_path=tmp_path,
                repo_path=tmp_path,
                args=args,
                log_file=log_file,
                selection_fn=_fake_select_tier_0,
                governor_ledger_path=ledger,
                run_id="run-batch-cost",
                discovery=discovery,
                cost_tracker=tracker,
                baseline_results=_BASELINE_RESULTS,
            )

        # The batch ran the finding through the chain (mocked no-change
        # claude output tallies as a failure; the cost row below is the
        # propagation this test pins, not the fix outcome).
        assert finding.finding_id in batch.fix_results
        rows = _read_ledger(cost_log)
        assert len(rows) == 1
        # The Governor-resolved model id (not a guess) with discovered rates.
        assert rows[0]["model"] == "claude-3-5-haiku"
        assert rows[0]["rates_source"] == "discovered"
        assert rows[0]["input_tokens"] == 3000
        assert rows[0]["output_tokens"] == 300

    def test_no_tracker_no_crash_no_rows(self, tmp_path, make_finding):
        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        ledger = tmp_path / "governor_recommendations.jsonl"
        cost_log = tmp_path / "cost_log.jsonl"
        log_file = tmp_path / "log.txt"
        args = _make_args()

        with patch(
            "bluei.engine.lifecycle.apply_claude_fix",
            return_value=(0, "claude output", None),
        ):
            result = _apply_single_fix(
                finding=finding,
                worktree_path=tmp_path,
                repo_path=tmp_path,
                args=args,
                log_file=log_file,
                selection_fn=_fake_select_tier_0,
                governor_ledger_path=ledger,
                run_id="run-batch-no-cost",
                discovery=_FakeDiscovery([_tier0_claude_model()]),
                baseline_results=_BASELINE_RESULTS,
                # cost_tracker omitted → default None → no crash, no rows
            )

        assert result is not None
        assert not cost_log.exists()

    def test_exceeded_limit_skips_claude_invocation(self, tmp_path, make_finding):
        # Pre-invocation guard (mirrors the pr-cycle call site): once the
        # hard limit is exceeded, apply_claude_fix must not launch at all.
        # The skip returns the same FAILED/claude FixResult shape a claude
        # failure produces, and records no cost row (nothing ran).
        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        tracker = MagicMock()
        tracker.exceeded_limit.return_value = True
        tracker.cycle_total.return_value = 12.34

        with patch("bluei.engine.lifecycle.apply_claude_fix") as mock_fix:
            result = _apply_single_fix(
                finding=finding,
                worktree_path=tmp_path,
                repo_path=tmp_path,
                args=_make_args(),
                log_file=tmp_path / "log.txt",
                cost_tracker=tracker,
                baseline_results=_BASELINE_RESULTS,
            )

        mock_fix.assert_not_called()
        tracker.record_invocation.assert_not_called()
        assert result.status == "failed"
        assert result.error == "cost-limit"
        assert result.fix_method == "claude"

    def test_timeout_records_invocation_cost_and_aborts_batch(
        self, tmp_path, make_finding
    ):
        # A timed-out Claude subprocess DID launch — its invocation cost must
        # reach the ledger even though the call raised. The timeout then
        # RE-RAISES: a killed fix can leave partial edits in the shared
        # worktree, so the batch must abort rather than let a later finding
        # commit them (TimeoutExpired is not an OSError subclass, so it is
        # matched explicitly).
        import subprocess as _sp

        from bluei.engine.cost_tracker import CostTracker

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        cost_log = tmp_path / "cost_log.jsonl"
        tracker = CostTracker(log_path=cost_log)

        with patch(
            "bluei.engine.lifecycle.apply_claude_fix",
            side_effect=_sp.TimeoutExpired(cmd="claude", timeout=120),
        ):
            with pytest.raises(_sp.TimeoutExpired):
                _apply_single_fix(
                    finding=finding,
                    worktree_path=tmp_path,
                    repo_path=tmp_path,
                    args=_make_args(),
                    log_file=tmp_path / "log.txt",
                    cost_tracker=tracker,
                    baseline_results=_BASELINE_RESULTS,
                )

        rows = _read_ledger(cost_log)
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 3000
        assert rows[0]["output_tokens"] == 300
        assert "aborting batch" in (tmp_path / "log.txt").read_text()


class TestBatchTimeoutAbortsBatchNotCycle:
    def test_process_batch_timeout_returns_batch_failure(self, tmp_path, make_finding):
        # current contract: batch repair is refused before any fix runs, so a
        # timed-out fix can neither escape to kill the cycle nor produce the
        # old "claude-timeout-batch-aborted" result — the batch is refused
        # upfront and the cycle's remaining batches are unaffected.
        import subprocess as _sp

        from bluei.engine.batch_pr import process_batch
        from bluei.engine.models import BatchGroup

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        batch = BatchGroup(
            batch_id="b-timeout",
            rule_pattern="ruff-b904",
            findings=[finding, finding],
        )
        args = _make_args()
        args.dry_run = True

        with patch("bluei.engine.batch_pr._create_worktree", return_value=True):
            with patch(
                "bluei.engine.batch_execution._apply_single_fix",
                side_effect=_sp.TimeoutExpired(cmd="claude", timeout=120),
            ):
                ok, detail = process_batch(
                    batch=batch,
                    repo_path=tmp_path,
                    args=args,
                    log_file=tmp_path / "log.txt",
                )

        assert ok is False
        assert detail == (
            "batch repair unqualified: missing configured scope/validation/closure acceptance"
        )
        assert batch.status == "open"
        log = (tmp_path / "log.txt").read_text()
        assert "batch-refused" in log
        assert "batch-aborted" not in log


class TestSplitResultPropagation:
    def test_all_failed_sub_batches_report_failure(self, tmp_path, make_finding):
        # current contract: batch repair is refused before the split machinery,
        # so a high-failure parent reports the refusal (no sub-batch results to
        # propagate) and the split orchestration is never entered: no retry
        # bump, no split history, no recursive sub-batch calls.
        from bluei.engine.batch_pr import process_batch
        from bluei.engine.models import BatchGroup

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        parent = BatchGroup(
            batch_id="parent", rule_pattern="ruff-b904", findings=[finding] * 6
        )
        args = _make_args()
        args.dry_run = True

        def _forbidden(*a, **k):
            raise AssertionError("split orchestration entered by refused batch")

        with patch("bluei.engine.batch_pr.should_split_batch", side_effect=_forbidden), patch(
            "bluei.engine.batch_pr.handle_batch_failure", side_effect=_forbidden
        ):
            ok, detail = process_batch(
                batch=parent,
                repo_path=tmp_path,
                args=args,
                log_file=tmp_path / "log.txt",
            )

        assert ok is False
        assert detail == (
            "batch repair unqualified: missing configured scope/validation/closure acceptance"
        )
        assert parent.status == "open"
        assert parent.retry_count == 0
        assert parent.split_history == []
        assert "split-and-retried" not in (tmp_path / "log.txt").read_text()


class TestSplitSoloSubBatch:
    # test_split_generated_solo_sub_batch_runs_batch_flow removed: split
    # recursion is refused before entry; the split-child-solo refusal is
    # pinned by TestProcessBatchUnqualifiedContainment::
    # test_split_child_solo_sub_batch_also_refused in
    # tests/test_unqualified_repair_lanes.py.

    def test_top_level_solo_still_delegates(self, tmp_path, make_finding):
        from bluei.engine.batch_pr import process_batch
        from bluei.engine.models import BatchGroup

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        solo = BatchGroup(
            batch_id="top-solo", rule_pattern="ruff-b904", findings=[finding]
        )
        ok, detail = process_batch(
            batch=solo,
            repo_path=tmp_path,
            args=_make_args(),
            log_file=tmp_path / "log.txt",
        )
        assert ok is False
        assert detail == "solo-delegated"


class TestSplitPrAccounting:
    def test_two_successful_subs_report_two_prs(self):
        # current contract: split orchestration is refused before running, but
        # parse_pr_count stays the live accounting hook pr_cycle.py applies to
        # every process_batch detail — its split-count parsing semantics
        # remain pinned here.
        from bluei.engine.batch_pr import parse_pr_count

        assert parse_pr_count("split-and-retried:2") == 2
        assert parse_pr_count("split-and-retried:1") == 1
        assert parse_pr_count("dry-run-pr-simulated") == 1
        assert parse_pr_count("pr-created-#9") == 1
        assert parse_pr_count("split-and-retried:bogus") == 1
        assert parse_pr_count(None) == 1

    # test_recursive_call_forwards_safety_and_repo_config removed: the split
    # recursion it drove is refused before entry; kwarg acceptance is pinned
    # by every other process_batch call here that passes safety_config/
    # repo_config without error.


# TestSplitChildDedupExemption removed: the rule-wide duplicate-PR check it
# exempted split siblings from is unreachable — refusal precedes any gh
# consultation (pinned by TestLiveGHDedupDetection in
# tests/test_batch_live_gh.py and the unqualified-repair-lanes guards).


class TestNestedSplitBudget:
    # test_nested_split_pr_counts_propagate and
    # test_nested_child_does_not_double_draw_budget removed: the nested-split
    # orchestration they drove is refused before entry; the pure count
    # semantics they relied on stay pinned by TestSplitPrAccounting above.

    def test_budget_stops_later_sub_batches(self, tmp_path, make_finding):
        # current contract: a refused batch must not draw from the cycle's
        # shared PR budget — the slots stay available for lanes that still
        # qualify, and no sub-batch skip bookkeeping is emitted.
        from bluei.engine.batch_pr import process_batch
        from bluei.engine.models import BatchGroup

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        parent = BatchGroup(
            batch_id="parent8", rule_pattern="ruff-b904", findings=[finding] * 6
        )
        args = _make_args()
        args.dry_run = True
        pr_budget = [1]

        with patch("bluei.engine.batch_pr.handle_batch_failure") as mock_handle:
            ok, detail = process_batch(
                batch=parent,
                repo_path=tmp_path,
                args=args,
                log_file=tmp_path / "log.txt",
                pr_budget=pr_budget,
            )

        assert ok is False
        assert detail == (
            "batch repair unqualified: missing configured scope/validation/closure acceptance"
        )
        assert pr_budget == [1]
        mock_handle.assert_not_called()
        log = (tmp_path / "log.txt").read_text()
        assert "pr-budget-exhausted" not in log
        assert "batch-refused" in log
