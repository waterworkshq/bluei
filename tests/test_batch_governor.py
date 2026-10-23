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

    def test_timeout_records_invocation_cost_and_returns_failure(
        self, tmp_path, make_finding
    ):
        # A timed-out Claude subprocess DID launch — its invocation cost must
        # reach the ledger even though the call raised, and the existing
        # FAILED claude-exception handling is preserved (no uncaught
        # TimeoutExpired — it is not an OSError subclass).
        import subprocess as _sp

        from bluei.engine.cost_tracker import CostTracker

        finding = make_finding(rule="ruff-b904", safe_to_autofix=False)
        cost_log = tmp_path / "cost_log.jsonl"
        tracker = CostTracker(log_path=cost_log)

        with patch(
            "bluei.engine.lifecycle.apply_claude_fix",
            side_effect=_sp.TimeoutExpired(cmd="claude", timeout=120),
        ):
            result = _apply_single_fix(
                finding=finding,
                worktree_path=tmp_path,
                repo_path=tmp_path,
                args=_make_args(),
                log_file=tmp_path / "log.txt",
                cost_tracker=tracker,
            )

        assert result.status == "failed"
        assert result.error.startswith("claude-exception:")
        assert result.fix_method == "claude"
        rows = _read_ledger(cost_log)
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 3000
        assert rows[0]["output_tokens"] == 300
