"""Integration tests for the CostTracker gate semantics.

These tests verify the conditions under which the CLI would skip Claude
invocations (the gate at bluei/engine/cli.py:1954).  They exercise the
real CostTracker with realistic thresholds ($2 soft, $10 hard) — the same
values used in production — and prove the gate behaves correctly without
running the full CLI pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bluei.engine.cost_tracker import MODEL_RATES, CostTracker

PRODUCTION_SOFT_WARN = 2.0
PRODUCTION_HARD_LIMIT = 10.0


@pytest.fixture
def production_tracker(tmp_path: Path) -> CostTracker:
    return CostTracker(
        log_path=tmp_path / "cost_log.jsonl",
        soft_warn=PRODUCTION_SOFT_WARN,
        hard_limit=PRODUCTION_HARD_LIMIT,
    )


def _cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = MODEL_RATES.get(model, MODEL_RATES["default"])
    return (
        input_tokens * rates["input_per_1k"] / 1000
        + output_tokens * rates["output_per_1k"] / 1000
    )


def _invocation_to_reach(
    cost_target: float, model: str = "claude-sonnet-4"
) -> tuple[int, int, int]:
    per_call = _cost_of(model, 500_000, 200_000)
    n = int(cost_target / per_call) + 1
    return n, 500_000, 200_000


class TestHardLimitPreventsGatePassage:
    """Gate check: exceeded_limit() returns True → Claude would be skipped."""

    def test_exceeded_after_accumulating_past_10_dollars(
        self, production_tracker: CostTracker
    ) -> None:
        assert not production_tracker.exceeded_limit()
        n, inp, out = _invocation_to_reach(PRODUCTION_HARD_LIMIT)
        for _ in range(n):
            production_tracker.record_invocation(
                "claude-sonnet-4", input_tokens=inp, output_tokens=out
            )
        assert production_tracker.cycle_total() >= PRODUCTION_HARD_LIMIT
        assert production_tracker.exceeded_limit()

    def test_subsequent_recording_still_works_after_limit(
        self, production_tracker: CostTracker
    ) -> None:
        n, inp, out = _invocation_to_reach(PRODUCTION_HARD_LIMIT)
        for _ in range(n):
            production_tracker.record_invocation(
                "claude-sonnet-4", input_tokens=inp, output_tokens=out
            )
        assert production_tracker.exceeded_limit()
        cost_before = production_tracker.cycle_total()
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=1000, output_tokens=500
        )
        assert production_tracker.cycle_total() > cost_before

    def test_gate_would_skip_claude_exactly_at_threshold(
        self, production_tracker: CostTracker
    ) -> None:
        per_call = _cost_of("claude-sonnet-4", 500_000, 200_000)
        target_calls = int(PRODUCTION_HARD_LIMIT / per_call)
        for i in range(target_calls):
            production_tracker.record_invocation(
                "claude-sonnet-4", input_tokens=500_000, output_tokens=200_000
            )
        assert not production_tracker.exceeded_limit(), (
            "should not exceed before reaching exactly $10"
        )
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=500_000, output_tokens=200_000
        )
        assert production_tracker.exceeded_limit(), "should exceed once total >= $10"


class TestSoftLimitSetsWarnedFlag:
    """Gate check: warned() flips at $2, but exceeded_limit() stays False."""

    def test_warned_at_exactly_2_dollars(self, production_tracker: CostTracker) -> None:
        per_call = _cost_of("claude-sonnet-4", 200_000, 100_000)
        n = int(PRODUCTION_SOFT_WARN / per_call) + 1
        for _ in range(n):
            production_tracker.record_invocation(
                "claude-sonnet-4", input_tokens=200_000, output_tokens=100_000
            )
        assert production_tracker.warned()
        assert not production_tracker.exceeded_limit()

    def test_warned_stays_false_below_2_dollars(
        self, production_tracker: CostTracker
    ) -> None:
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=100_000, output_tokens=50_000
        )
        assert not production_tracker.warned()

    def test_warned_flag_is_sticky(self, production_tracker: CostTracker) -> None:
        per_call = _cost_of("claude-sonnet-4", 200_000, 100_000)
        n = int(PRODUCTION_SOFT_WARN / per_call) + 1
        for _ in range(n):
            production_tracker.record_invocation(
                "claude-sonnet-4", input_tokens=200_000, output_tokens=100_000
            )
        assert production_tracker.warned()
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=0, output_tokens=0
        )
        assert production_tracker.warned()


class TestThresholdOrdering:
    """Proves warned becomes True BEFORE exceeded becomes True."""

    def test_warned_precedes_exceeded(self, tmp_path: Path) -> None:
        t = CostTracker(
            log_path=tmp_path / "cost.jsonl",
            soft_warn=1.0,
            hard_limit=5.0,
        )
        per_call = _cost_of("claude-sonnet-4", 200_000, 100_000)
        warned_at = None
        exceeded_at = None
        for i in range(20):
            t.record_invocation(
                "claude-sonnet-4", input_tokens=200_000, output_tokens=100_000
            )
            if warned_at is None and t.warned():
                warned_at = i + 1
            if exceeded_at is None and t.exceeded_limit():
                exceeded_at = i + 1
                break
        assert warned_at is not None
        assert exceeded_at is not None
        assert warned_at < exceeded_at

    def test_with_production_thresholds_warned_first(
        self, production_tracker: CostTracker
    ) -> None:
        warned_seen = False
        per_call = _cost_of("claude-sonnet-4", 500_000, 200_000)
        for _ in range(100):
            production_tracker.record_invocation(
                "claude-sonnet-4", input_tokens=500_000, output_tokens=200_000
            )
            if production_tracker.warned() and not production_tracker.exceeded_limit():
                warned_seen = True
            if production_tracker.exceeded_limit():
                break
        assert warned_seen, (
            "There must exist a window where warned=True but exceeded=False"
        )

    def test_no_exceeded_before_warned(self, production_tracker: CostTracker) -> None:
        per_call = _cost_of("claude-sonnet-4", 500_000, 200_000)
        for _ in range(100):
            production_tracker.record_invocation(
                "claude-sonnet-4", input_tokens=500_000, output_tokens=200_000
            )
            if production_tracker.exceeded_limit():
                assert production_tracker.warned(), (
                    "exceeded must never be True before warned"
                )
                break


class TestCostTrackerRoundTripWithLogFile:
    """Full round-trip: record invocations → JSONL on disk → verify structure."""

    def test_jsonl_entries_have_required_keys(
        self, production_tracker: CostTracker, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=1000, output_tokens=500
        )
        production_tracker.record_invocation(
            "gpt-4o", input_tokens=2000, output_tokens=1000
        )
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            entry = json.loads(line)
            for key in (
                "timestamp",
                "model",
                "input_tokens",
                "output_tokens",
                "cost",
                "cycle_total_so_far",
            ):
                assert key in entry

    def test_cycle_total_so_far_is_monotonic(
        self, production_tracker: CostTracker, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        for _ in range(5):
            production_tracker.record_invocation(
                "claude-sonnet-4", input_tokens=100_000, output_tokens=50_000
            )
        entries = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        for i in range(1, len(entries)):
            assert (
                entries[i]["cycle_total_so_far"] > entries[i - 1]["cycle_total_so_far"]
            )

    def test_totals_match_tracker_state(
        self, production_tracker: CostTracker, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=10_000, output_tokens=5_000
        )
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=20_000, output_tokens=10_000
        )
        production_tracker.record_invocation(
            "gpt-4o", input_tokens=5_000, output_tokens=2_500
        )
        entries = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        logged_total = entries[-1]["cycle_total_so_far"]
        assert logged_total == pytest.approx(production_tracker.cycle_total(), rel=1e-6)
        logged_sum = sum(e["cost"] for e in entries)
        assert logged_sum == pytest.approx(production_tracker.cycle_total(), rel=1e-6)


class TestPatternReplaySavingsTrackedCorrectly:
    """Savings are logged separately and never affect cost thresholds."""

    def test_savings_logged_with_correct_type(
        self, production_tracker: CostTracker, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=1000, output_tokens=500
        )
        production_tracker.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=0.0105,
            pattern_id="fp-test",
            rule="broad-except",
        )
        entries = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        assert len(entries) == 2
        assert "type" not in entries[0]
        assert entries[1]["type"] == "pattern_replay_savings"
        assert entries[1]["saved_cost"] == pytest.approx(0.0105, rel=1e-3)

    def test_large_savings_do_not_flip_gate(
        self, production_tracker: CostTracker
    ) -> None:
        production_tracker.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=999.0,
            pattern_id="fp-huge",
            rule="test",
        )
        assert not production_tracker.warned()
        assert not production_tracker.exceeded_limit()
        assert production_tracker.cycle_total() == 0.0

    def test_summary_separates_savings_from_costs(
        self, production_tracker: CostTracker, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        production_tracker.record_invocation(
            "claude-sonnet-4", input_tokens=1000, output_tokens=500
        )
        production_tracker.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=0.5,
            pattern_id="fp-1",
            rule="broad-except",
        )
        production_tracker.record_pattern_replay_savings(
            model="claude-sonnet-4",
            saved_cost=0.3,
            pattern_id="fp-2",
            rule="unused-import",
        )
        s = CostTracker.summary(log_path)
        assert s["total_invocations"] == 1
        assert s["total_cost"] == pytest.approx(
            production_tracker.cycle_total(), rel=1e-6
        )
        assert s["pattern_replay_savings"]["count"] == 2
        assert s["pattern_replay_savings"]["total_saved"] == pytest.approx(
            0.8, rel=1e-3
        )


class TestLoadHistoryFromLogFile:
    """CostTracker.load_history reads pre-existing JSONL correctly."""

    def test_loads_pre_written_log(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        entries_to_write = [
            {
                "timestamp": "2025-01-01T00:00:00Z",
                "model": "claude-sonnet-4",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost": 0.0105,
            },
            {
                "timestamp": "2025-01-01T00:01:00Z",
                "model": "gpt-4o",
                "input_tokens": 2000,
                "output_tokens": 1000,
                "cost": 0.025,
            },
            {
                "timestamp": "2025-01-01T00:02:00Z",
                "type": "pattern_replay_savings",
                "model": "claude-sonnet-4",
                "saved_cost": 0.01,
                "pattern_id": "fp-1",
                "rule": "test",
            },
        ]
        with log_path.open("w") as f:
            for e in entries_to_write:
                f.write(json.dumps(e) + "\n")
        loaded = CostTracker.load_history(log_path)
        assert len(loaded) == 3
        assert loaded[0]["model"] == "claude-sonnet-4"
        assert loaded[1]["model"] == "gpt-4o"
        assert loaded[2]["type"] == "pattern_replay_savings"

    def test_new_tracker_sees_existing_log(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        t1 = CostTracker(
            log_path=log_path,
            soft_warn=PRODUCTION_SOFT_WARN,
            hard_limit=PRODUCTION_HARD_LIMIT,
        )
        t1.record_invocation(
            "claude-sonnet-4", input_tokens=500_000, output_tokens=200_000
        )
        t1_cost = t1.cycle_total()
        t2 = CostTracker(
            log_path=log_path,
            soft_warn=PRODUCTION_SOFT_WARN,
            hard_limit=PRODUCTION_HARD_LIMIT,
        )
        assert t2.cycle_total() == 0.0
        history = CostTracker.load_history(log_path)
        assert len(history) == 1
        assert history[0]["cost"] == pytest.approx(t1_cost, rel=1e-3)

    def test_load_history_ignores_malformed_lines(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        with log_path.open("w") as f:
            f.write('{"model":"claude-sonnet-4","cost":0.01}\n')
            f.write("NOT JSON\n")
            f.write('{"model":"gpt-4o","cost":0.02}\n')
            f.write("\n")
        loaded = CostTracker.load_history(log_path)
        assert len(loaded) == 2
        assert loaded[0]["model"] == "claude-sonnet-4"
        assert loaded[1]["model"] == "gpt-4o"

    def test_summary_from_pre_written_log(self, tmp_path: Path) -> None:
        log_path = tmp_path / "cost_log.jsonl"
        with log_path.open("w") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": "2025-01-01T00:00:00Z",
                        "model": "claude-sonnet-4",
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cost": 0.0105,
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "timestamp": "2025-01-01T00:01:00Z",
                        "type": "pattern_replay_savings",
                        "model": "claude-sonnet-4",
                        "saved_cost": 0.5,
                        "pattern_id": "fp-1",
                        "rule": "test",
                    }
                )
                + "\n"
            )
        s = CostTracker.summary(log_path)
        assert s["total_cost"] == pytest.approx(0.0105, rel=1e-3)
        assert s["total_invocations"] == 1
        assert s["pattern_replay_savings"]["total_saved"] == pytest.approx(
            0.5, rel=1e-3
        )
