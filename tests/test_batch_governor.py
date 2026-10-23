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
            )

        # AC-P3-2a: ledger row recorded for this finding
        rows = _read_ledger(ledger)
        assert len(rows) == 1
        assert rows[0]["finding_id"] == finding.finding_id
        assert rows[0]["run_id"] == "run-batch-test"
        assert rows[0]["tier"] == "tier-0"

        # AC-P3-2b: resolved template carries --model from discovery
        assert "--model claude-3-5-haiku" in captured["tmpl"]

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
                )

        rows = _read_ledger(cost_log)
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 3000
        assert rows[0]["output_tokens"] == 300
        assert "aborting batch" in (tmp_path / "log.txt").read_text()


class TestBatchTimeoutAbortsBatchNotCycle:
    def test_process_batch_timeout_returns_batch_failure(self, tmp_path, make_finding):
        # A timed-out fix escapes the per-finding loop and is caught at the
        # BATCH boundary: the batch is abandoned (failed status, no commit,
        # no PR) and process_batch returns a failure result — the exception
        # must not escape to kill the whole cycle's remaining batches.
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
        assert detail == "claude-timeout-batch-aborted"
        assert "batch-aborted" in (tmp_path / "log.txt").read_text()


class TestSplitResultPropagation:
    def test_all_failed_sub_batches_report_failure(self, tmp_path, make_finding):
        # The parent batch's "split-and-retried" is success only if at least
        # one sub-batch produced a PR. When every sub-batch fails (here: each
        # times out), the parent must report failure — otherwise the cycle
        # counts a nonexistent PR and can consume the PR cap.
        import subprocess as _sp

        from bluei.engine.batch_pr import process_batch
        from bluei.engine.models import BatchGroup

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        parent = BatchGroup(
            batch_id="parent", rule_pattern="ruff-b904", findings=[finding] * 6
        )
        sub_a = BatchGroup(
            batch_id="sub-a", rule_pattern="ruff-b904", findings=[finding] * 3
        )
        sub_b = BatchGroup(
            batch_id="sub-b", rule_pattern="ruff-b904", findings=[finding] * 3
        )
        args = _make_args()
        args.dry_run = True

        def _apply(batch, **kwargs):
            if batch.batch_id == "parent":
                return 1, 5  # one success, many failures -> split warranted
            raise _sp.TimeoutExpired(cmd="claude", timeout=120)

        with patch("bluei.engine.batch_pr._create_worktree", return_value=True), patch(
            "bluei.engine.batch_pr.apply_batch_fixes", side_effect=_apply
        ), patch(
            "bluei.engine.batch_pr.should_split_batch", return_value=True
        ), patch(
            "bluei.engine.batch_pr.handle_batch_failure",
            return_value=[sub_a, sub_b],
        ):
            ok, detail = process_batch(
                batch=parent,
                repo_path=tmp_path,
                args=args,
                log_file=tmp_path / "log.txt",
            )

        assert ok is False
        assert detail == "split-all-sub-batches-failed"
        assert parent.status == "failed"

    def test_one_successful_sub_batch_still_reports_success(
        self, tmp_path, make_finding
    ):
        import subprocess as _sp

        from bluei.engine.batch_pr import process_batch
        from bluei.engine.models import BatchGroup

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        parent = BatchGroup(
            batch_id="parent2", rule_pattern="ruff-b904", findings=[finding] * 6
        )
        sub_a = BatchGroup(
            batch_id="sub-a2", rule_pattern="ruff-b904", findings=[finding] * 3
        )
        sub_b = BatchGroup(
            batch_id="sub-b2", rule_pattern="ruff-b904", findings=[finding] * 3
        )
        args = _make_args()
        args.dry_run = True

        def _apply(batch, **kwargs):
            if batch.batch_id == "parent2":
                return 1, 5
            if batch.batch_id == "sub-a2":
                # Sub-batch that reaches the commit/PR stage: stop before
                # real git/gh by reporting a created PR via the solo path is
                # not available — instead let it "succeed" through the
                # no-split route by returning all successes and no failures.
                return 3, 0
            raise _sp.TimeoutExpired(cmd="claude", timeout=120)

        with patch("bluei.engine.batch_pr._create_worktree", return_value=True), patch(
            "bluei.engine.batch_pr.apply_batch_fixes", side_effect=_apply
        ), patch(
            "bluei.engine.batch_pr.should_split_batch",
            side_effect=lambda batch, max_depth=3: batch.batch_id == "parent2",
        ), patch(
            "bluei.engine.batch_pr.handle_batch_failure",
            return_value=[sub_a, sub_b],
        ), patch(
            "bluei.engine.git_ops.git_commit_all", return_value="ok"
        ), patch(
            "bluei.engine.git_ops.git_push_branch", return_value=True
        ):
            ok, detail = process_batch(
                batch=parent,
                repo_path=tmp_path,
                args=args,
                log_file=tmp_path / "log.txt",
            )

        assert ok is True
        assert detail == "split-and-retried"


class TestSplitSoloSubBatch:
    def test_split_generated_solo_sub_batch_runs_batch_flow(self, tmp_path, make_finding):
        # A split can produce a single-finding sub-batch. At the TOP level a
        # solo batch delegates to the single-finding path, but inside the
        # split recursion nobody re-routes it — it must run the normal batch
        # flow (allow_solo) instead of returning "solo-delegated" and going
        # unprocessed.
        from bluei.engine.batch_pr import process_batch
        from bluei.engine.models import BatchGroup

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        parent = BatchGroup(
            batch_id="parent3", rule_pattern="ruff-b904", findings=[finding] * 6
        )
        solo_sub = BatchGroup(
            batch_id="solo-sub", rule_pattern="ruff-b904", findings=[finding]
        )
        multi_sub = BatchGroup(
            batch_id="multi-sub", rule_pattern="ruff-b904", findings=[finding] * 5
        )
        args = _make_args()
        args.dry_run = True

        def _apply(batch, **kwargs):
            if batch.batch_id == "parent3":
                return 1, 5
            return (1, 0) if batch.is_solo else (0, 1)

        with patch("bluei.engine.batch_pr._create_worktree", return_value=True), patch(
            "bluei.engine.batch_pr.apply_batch_fixes", side_effect=_apply
        ), patch(
            "bluei.engine.batch_pr.should_split_batch",
            side_effect=lambda batch, max_depth=3: batch.batch_id == "parent3",
        ), patch(
            "bluei.engine.batch_pr.handle_batch_failure",
            return_value=[solo_sub, multi_sub],
        ), patch(
            "bluei.engine.git_ops.git_commit_all", return_value="ok"
        ), patch(
            "bluei.engine.git_ops.git_push_branch", return_value=True
        ):
            ok, detail = process_batch(
                batch=parent,
                repo_path=tmp_path,
                args=args,
                log_file=tmp_path / "log.txt",
            )

        # The solo sub-batch ran the batch flow and produced the dry-run PR;
        # the multi sub-batch failed — the parent succeeds on the solo's PR.
        assert ok is True
        assert detail == "split-and-retried"
        assert "batch-dry-run: would create PR for solo-sub" in (
            tmp_path / "log.txt"
        ).read_text()

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
