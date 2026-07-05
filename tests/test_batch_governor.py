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
from unittest.mock import patch

from bluei.engine.batch_execution import _apply_single_fix
from bluei.engine.model_discovery import ResolvedModel
from bluei.engine.model_governor import ModelTier, TierRecommendation


# --- fakes (mirror test_governor_wiring.py) ---


class _FakeDiscovery:
    """Minimal ModelDiscovery Protocol implementation for wiring tests."""

    def __init__(self, models):
        self._models = models

    def discover(self, backend):
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
