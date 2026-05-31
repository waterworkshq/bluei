import json
from pathlib import Path

import pytest

from bluei.app.auto_tune import (
    REPLAY_THRESHOLD_CEILING,
    REPLAY_THRESHOLD_FLOOR,
    adjust_replay_thresholds,
    _load_tune_state,
)


def _write_telemetry(path: Path, records):
    lines = [json.dumps(r) for r in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _make_telemetry(rule="test-rule", success=True, stage="pattern-replay"):
    return {"rule": rule, "success": success, "final_stage": stage if success else "cascade_exhausted"}


class TestAdjustReplayThresholds:
    def test_all_successes_lowers_threshold(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_telemetry(success=True) for _ in range(25)]
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert "test-rule" in result
        assert result["test-rule"] < 0.85

    def test_floor_enforced(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        tune.write_text(json.dumps({"replay_thresholds": {"test-rule": 0.71}}))
        records = [_make_telemetry(success=True) for _ in range(25)]
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert result.get("test-rule", 0.71) >= REPLAY_THRESHOLD_FLOOR

    def test_one_failure_raises_threshold(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_telemetry(success=True) for _ in range(9)]
        records.append(_make_telemetry(success=False))
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert "test-rule" in result
        assert result["test-rule"] > 0.85

    def test_ceiling_enforced(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        tune.write_text(json.dumps({"replay_thresholds": {"test-rule": 0.97}}))
        records = [_make_telemetry(success=False) for _ in range(5)]
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert result.get("test-rule", 0.97) <= REPLAY_THRESHOLD_CEILING

    def test_success_then_failure_raises(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_telemetry(success=True) for _ in range(20)]
        records.append(_make_telemetry(success=False))
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert result.get("test-rule", 0.85) > 0.85

    def test_insufficient_data_no_change(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_telemetry(success=True) for _ in range(5)]
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert "test-rule" not in result

    def test_multiple_rules_adjusted_independently(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_telemetry(rule="rule-a", success=True) for _ in range(25)]
        records += [_make_telemetry(rule="rule-b", success=False) for _ in range(5)]
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert "rule-a" in result
        assert "rule-b" in result
        assert result["rule-a"] < 0.85
        assert result["rule-b"] > 0.85

    def test_persist_and_reload(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_telemetry(success=True) for _ in range(25)]
        _write_telemetry(telem, records)
        adjust_replay_thresholds(telem, tune)
        state = _load_tune_state(tune)
        assert "replay_thresholds" in state
        assert "test-rule" in state["replay_thresholds"]

    def test_empty_telemetry_returns_empty(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        result = adjust_replay_thresholds(telem, tune)
        assert result == {}

    def test_all_failures_raises_threshold(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_telemetry(success=False) for _ in range(15)]
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert "test-rule" in result
        assert result["test-rule"] > 0.85

    def test_existing_thresholds_preserved_for_rules_without_telemetry(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        tune.write_text(json.dumps({
            "replay_thresholds": {"other-rule": 0.9, "test-rule": 0.85}
        }))
        records = [_make_telemetry(success=True) for _ in range(25)]
        _write_telemetry(telem, records)
        adjust_replay_thresholds(telem, tune)
        state = _load_tune_state(tune)
        assert state["replay_thresholds"]["other-rule"] == 0.9
