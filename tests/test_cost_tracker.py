"""Tests for CostTracker — cost tracking per cycle."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bluei.engine.cost_tracker import CostTracker


@pytest.fixture
def tracker(tmp_path: Path) -> CostTracker:
    log_path = tmp_path / "cost_log.jsonl"
    return CostTracker(log_path=log_path, soft_warn=2.0, hard_limit=10.0)


class TestRecordInvocation:
    """Cost calculation per invocation."""

    def test_records_and_returns_cost(self, tracker: CostTracker) -> None:
        cost = tracker.record_invocation("claude-sonnet-4", input_tokens=1000, output_tokens=500)
        # input: 1000 * 0.003 / 1000 = 0.003
        # output: 500 * 0.015 / 1000 = 0.0075
        # total: 0.0105
        assert cost == pytest.approx(0.0105, rel=1e-3)

    def test_default_model_fallback(self, tracker: CostTracker) -> None:
        cost = tracker.record_invocation("unknown-model", input_tokens=1000, output_tokens=0)
        assert cost == pytest.approx(0.003, rel=1e-3)

    def test_zero_tokens(self, tracker: CostTracker) -> None:
        cost = tracker.record_invocation("claude-sonnet-4", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_gpt4o_mini_rate(self, tracker: CostTracker) -> None:
        cost = tracker.record_invocation("gpt-4o-mini", input_tokens=1000, output_tokens=1000)
        # input: 1000 * 0.00015 / 1000 = 0.00015
        # output: 1000 * 0.0006 / 1000 = 0.0006
        # total: 0.00075
        assert cost == pytest.approx(0.00075, rel=1e-3)

    def test_large_invocation(self, tracker: CostTracker) -> None:
        cost = tracker.record_invocation("claude-opus-4", input_tokens=100_000, output_tokens=10_000)
        # input: 100_000 * 0.015 / 1000 = 1.5
        # output: 10_000 * 0.075 / 1000 = 0.75
        # total: 2.25
        assert cost == pytest.approx(2.25, rel=1e-3)

    def test_persists_to_log_file(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t = CostTracker(log_path=log_path)
        t.record_invocation("claude-sonnet-4", input_tokens=500, output_tokens=200)
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        import json
        entry = json.loads(lines[0])
        assert entry["model"] == "claude-sonnet-4"
        assert entry["input_tokens"] == 500
        assert entry["output_tokens"] == 200

    def test_increments_invocation_count(self, tracker: CostTracker) -> None:
        tracker.record_invocation("claude-sonnet-4", 100, 50)
        tracker.record_invocation("claude-sonnet-4", 200, 100)
        # Verify cycle_total increased
        assert tracker.cycle_total() > 0.0


class TestCycleTotal:
    """Cumulative cost."""

    def test_starts_at_zero(self, tracker: CostTracker) -> None:
        assert tracker.cycle_total() == 0.0

    def test_accumulates_across_invocations(self, tracker: CostTracker) -> None:
        c1 = tracker.record_invocation("claude-sonnet-4", input_tokens=1000, output_tokens=500)
        c2 = tracker.record_invocation("claude-sonnet-4", input_tokens=2000, output_tokens=1000)
        expected = c1 + c2
        assert tracker.cycle_total() == pytest.approx(expected, rel=1e-3)

    def test_accumulates_large_numbers(self, tmp_path: Path) -> None:
        t = CostTracker(log_path=tmp_path / "cost.jsonl", soft_warn=1000, hard_limit=5000)
        for _ in range(10):
            t.record_invocation("claude-sonnet-4", input_tokens=100_000, output_tokens=50_000)
        # Per call: 100k*0.003/1k + 50k*0.015/1k = 0.3 + 0.75 = 1.05
        # 10 calls: 10.5
        assert t.cycle_total() == pytest.approx(10.5, rel=1e-3)


class TestWarned:
    """Soft warning threshold."""

    def test_initial_state(self, tracker: CostTracker) -> None:
        assert not tracker.warned()

    def test_triggers_after_threshold(self, tmp_path: Path) -> None:
        t = CostTracker(log_path=tmp_path / "cost.jsonl", soft_warn=1.0, hard_limit=10.0)
        t.record_invocation("claude-sonnet-4", input_tokens=200_000, output_tokens=100_000)
        # 200k*0.003/1k + 100k*0.015/1k = 0.6 + 1.5 = 2.1 >= 1.0
        assert t.warned()

    def test_not_triggered_below_threshold(self, tmp_path: Path) -> None:
        t = CostTracker(log_path=tmp_path / "cost.jsonl", soft_warn=5.0, hard_limit=10.0)
        t.record_invocation("claude-3-haiku-20240307", input_tokens=1000, output_tokens=500)
        # 1000*0.00025/1k + 500*0.00125/1k = 0.00025 + 0.000625 = 0.000875 < 5.0
        assert not t.warned()


class TestExceededLimit:
    """Hard limit threshold."""

    def test_initial_state(self, tracker: CostTracker) -> None:
        assert not tracker.exceeded_limit()

    def test_triggers_after_hard_limit(self, tmp_path: Path) -> None:
        t = CostTracker(log_path=tmp_path / "cost.jsonl", soft_warn=1.0, hard_limit=5.0)
        for _ in range(10):
            t.record_invocation("claude-sonnet-4", input_tokens=500_000, output_tokens=200_000)
        total = t.cycle_total()
        # Each: 500k*0.003/1k + 200k*0.015/1k = 1.5 + 3.0 = 4.5
        # 10: 45.0 >= 5.0
        assert total > 5.0
        assert t.exceeded_limit()

    def test_not_triggered_below_hard_limit(self, tmp_path: Path) -> None:
        t = CostTracker(log_path=tmp_path / "cost.jsonl", hard_limit=100.0)
        t.record_invocation("claude-3-haiku-20240307", input_tokens=1000, output_tokens=500)
        assert not t.exceeded_limit()

    def test_warned_before_exceeded(self, tmp_path: Path) -> None:
        t = CostTracker(log_path=tmp_path / "cost.jsonl", soft_warn=1.0, hard_limit=5.0)
        # First invocation below warn
        t.record_invocation("claude-sonnet-4", input_tokens=200_000, output_tokens=100_000)
        # 0.6 + 1.5 = 2.1 >= 1.0 but < 5.0
        assert t.warned()
        assert not t.exceeded_limit()
        # Push over hard limit
        t.record_invocation("claude-sonnet-4", input_tokens=300_000, output_tokens=200_000)
        assert t.exceeded_limit()


class TestRecordPatternReplaySavings:
    """Pattern replay savings recording."""

    def test_appends_correct_json_record(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t = CostTracker(log_path=log_path)
        t.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=0.012,
            pattern_id="fp-abc123",
            rule="broad-except",
        )
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "pattern_replay_savings"
        assert entry["model"] == "claude-sonnet-4"
        assert entry["saved_cost"] == 0.012
        assert entry["pattern_id"] == "fp-abc123"
        assert entry["rule"] == "broad-except"
        assert "timestamp" in entry

    def test_savings_do_not_affect_cycle_total(self, tracker: CostTracker) -> None:
        tracker.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=999.0,
            pattern_id="fp-x",
            rule="test",
        )
        assert tracker.cycle_total() == 0.0

    def test_savings_do_not_trigger_soft_warn(self, tmp_path: Path) -> None:
        t = CostTracker(log_path=tmp_path / "c.jsonl", soft_warn=0.01, hard_limit=10.0)
        t.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=5.0,
            pattern_id="fp-x",
            rule="test",
        )
        assert not t.warned()

    def test_savings_do_not_trigger_hard_limit(self, tmp_path: Path) -> None:
        t = CostTracker(log_path=tmp_path / "c.jsonl", soft_warn=0.01, hard_limit=0.01)
        t.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=999.0,
            pattern_id="fp-x",
            rule="test",
        )
        assert not t.exceeded_limit()

    def test_multiple_savings_accumulate_in_log(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t = CostTracker(log_path=log_path)
        for i in range(5):
            t.record_pattern_replay_savings(
                model="claude-sonnet-4",
                saved_cost=0.01 * (i + 1),
                pattern_id=f"fp-{i}",
                rule=f"rule-{i}",
            )
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 5
        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert entry["pattern_id"] == f"fp-{i}"

    def test_savings_and_invocations_coexist_in_log(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t = CostTracker(log_path=log_path)
        t.record_invocation("claude-sonnet-4", input_tokens=1000, output_tokens=500)
        t.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=0.0105,
            pattern_id="fp-abc",
            rule="broad-except",
        )
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 2
        inv = json.loads(lines[0])
        assert "type" not in inv
        assert inv["cost"] == pytest.approx(0.0105, rel=1e-3)
        sav = json.loads(lines[1])
        assert sav["type"] == "pattern_replay_savings"

    def test_uses_model_rates_for_saved_cost(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t = CostTracker(log_path=log_path)
        saved = t.estimate_invocation_cost("claude-sonnet-4", input_tokens=1000, output_tokens=500)
        assert saved == pytest.approx(0.0105, rel=1e-3)
        t.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=saved,
            pattern_id="fp-test",
            rule="broad-except",
        )
        entry = json.loads(log_path.read_text().strip().splitlines()[0])
        assert entry["saved_cost"] == pytest.approx(0.0105, rel=1e-3)


class TestEstimateInvocationCost:
    """Cost estimation without recording."""

    def test_matches_record_invocation_cost(self, tracker: CostTracker) -> None:
        estimated = tracker.estimate_invocation_cost("claude-sonnet-4", input_tokens=1000, output_tokens=500)
        actual = tracker.record_invocation("claude-sonnet-4", input_tokens=1000, output_tokens=500)
        assert estimated == pytest.approx(actual, rel=1e-6)

    def test_unknown_model_uses_default(self, tracker: CostTracker) -> None:
        cost = tracker.estimate_invocation_cost("nonexistent-model", input_tokens=1000, output_tokens=0)
        assert cost == pytest.approx(0.003, rel=1e-3)


class TestLoadHistory:
    """load_history returns savings alongside invocations."""

    def test_returns_savings_records(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t = CostTracker(log_path=log_path)
        t.record_invocation("claude-sonnet-4", input_tokens=100, output_tokens=50)
        t.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=0.005,
            pattern_id="fp-1",
            rule="rule-a",
        )
        entries = CostTracker.load_history(log_path)
        assert len(entries) == 2
        assert entries[1]["type"] == "pattern_replay_savings"

    def test_empty_log_returns_empty(self, tmp_path: Path) -> None:
        entries = CostTracker.load_history(tmp_path / "nonexistent.jsonl")
        assert entries == []


class TestSummaryWithSavings:
    """summary() includes pattern replay savings."""

    def test_summary_includes_savings(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t = CostTracker(log_path=log_path)
        t.record_invocation("claude-sonnet-4", input_tokens=1000, output_tokens=500)
        t.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=0.0105,
            pattern_id="fp-1",
            rule="broad-except",
        )
        s = CostTracker.summary(log_path)
        assert s["total_invocations"] == 1
        assert s["pattern_replay_savings"]["count"] == 1
        assert s["pattern_replay_savings"]["total_saved"] == pytest.approx(0.0105, rel=1e-3)

    def test_summary_no_savings_omits_key(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t = CostTracker(log_path=log_path)
        t.record_invocation("claude-sonnet-4", input_tokens=100, output_tokens=50)
        s = CostTracker.summary(log_path)
        assert "pattern_replay_savings" not in s

    def test_summary_empty_log(self, tmp_path: Path) -> None:
        s = CostTracker.summary(tmp_path / "nonexistent.jsonl")
        assert s["total_invocations"] == 0
        assert "pattern_replay_savings" not in s


# ── Merged from test_cost_tracker_remaining.py ──

from unittest.mock import patch as _patch


class TestOSErrorOnWrite:
    def test_invocation_continues_on_oserror(self, tmp_path):
        log_path = tmp_path / "noperm" / "cost.jsonl"
        log_path.parent.mkdir()
        log_path.parent.chmod(0o444)
        try:
            t = CostTracker(log_path=log_path)
            cost = t.record_invocation(
                "claude-sonnet-4", input_tokens=1000, output_tokens=500
            )
            assert cost > 0
            assert t.cycle_total() > 0
        finally:
            log_path.parent.chmod(0o755)

    def test_savings_continues_on_oserror(self, tmp_path):
        log_path = tmp_path / "noperm" / "cost.jsonl"
        log_path.parent.mkdir()
        log_path.parent.chmod(0o444)
        try:
            t = CostTracker(log_path=log_path)
            t.record_pattern_replay_savings(
                model="claude-sonnet-4",
                saved_cost=0.01,
                pattern_id="fp-1",
                rule="test",
            )
            assert t.cycle_total() == 0.0
        finally:
            log_path.parent.chmod(0o755)


class TestHardLimitExceeded:
    def test_hard_limit_logs_warning(self, tmp_path):
        log_path = tmp_path / "cost.jsonl"
        t = CostTracker(log_path=log_path, soft_warn=0.001, hard_limit=0.01)
        with _patch("bluei.engine.cost_tracker.logger") as mock_logger:
            t.record_invocation(
                "claude-sonnet-4", input_tokens=10000, output_tokens=5000
            )
            assert t.exceeded_limit()
            mock_logger.warning.assert_called()
            call_args = mock_logger.warning.call_args_list
            hard_msgs = [c for c in call_args if "HARD LIMIT" in str(c)]
            assert len(hard_msgs) >= 1

    def test_hard_limit_fires_once(self, tmp_path):
        log_path = tmp_path / "cost.jsonl"
        t = CostTracker(log_path=log_path, soft_warn=0.001, hard_limit=0.01)
        t.record_invocation("claude-sonnet-4", input_tokens=10000, output_tokens=5000)
        assert t.exceeded_limit()
        with _patch("bluei.engine.cost_tracker.logger") as mock_logger:
            t.record_invocation(
                "claude-sonnet-4", input_tokens=10000, output_tokens=5000
            )
            hard_msgs = [
                c for c in mock_logger.warning.call_args_list if "HARD LIMIT" in str(c)
            ]
            assert len(hard_msgs) == 0


class TestLoadHistoryCorruptLine:
    def test_skips_corrupt_lines(self, tmp_path):
        log_path = tmp_path / "cost.jsonl"
        log_path.write_text('{"model":"x","cost":1}\nBROKEN\n{"model":"y","cost":2}\n')
        entries = CostTracker.load_history(log_path)
        assert len(entries) == 2
