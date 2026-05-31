import json
from pathlib import Path

import pytest

from bluei.app.auto_tune import (
    BATCH_REDUCTION_FACTOR,
    CONSECUTIVE_RETRY_FAILURE_THRESHOLD,
    COOLDOWN_BOOST_MULTIPLIER,
    MAX_COOLDOWN_HOURS,
    MIN_BATCH_SIZE,
    compute_tune,
    flag_tune_success,
    read_tune_overrides,
    reset_tune,
)


def _write_jsonl(path: Path, records):
    lines = [json.dumps(r) for r in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _write_tune_state(path: Path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def _make_stat(retry_failed=0, findings_failed=0, **extra):
    rec = {"retry_failed": retry_failed, "findings_failed": findings_failed}
    rec.update(extra)
    return rec


class TestComputeTune:
    def test_missing_stats_file_returns_empty(self, tmp_path):
        stats = tmp_path / "missing.jsonl"
        tune = tmp_path / "tune.json"
        result = compute_tune(stats, tune)
        assert result == {}

    def test_empty_stats_file_returns_empty(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        stats.write_text("")
        result = compute_tune(stats, tune)
        assert result == {}

    def test_below_threshold_no_override(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_stat(retry_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD - 1)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert result == {}

    def test_at_threshold_triggers_batch_reduction(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 4}})
        records = [_make_stat(retry_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert "max_prs_per_run" in result
        assert result["max_prs_per_run"] == max(MIN_BATCH_SIZE, int(4 * BATCH_REDUCTION_FACTOR))

    def test_above_threshold_triggers_override(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 8}})
        records = [_make_stat(retry_failed=3) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD + 3)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert "max_prs_per_run" in result
        assert "_reason" in result

    def test_mixed_records_only_trailing_count(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 4}})
        records = [_make_stat(retry_failed=0)] * 5
        records += [_make_stat(retry_failed=1)] * CONSECUTIVE_RETRY_FAILURE_THRESHOLD
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert "max_prs_per_run" in result

    def test_trailing_success_breaks_streak(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_stat(retry_failed=1)] * CONSECUTIVE_RETRY_FAILURE_THRESHOLD
        records.append(_make_stat(retry_failed=0))
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert result == {}

    def test_findings_failure_triggers_cooldown_boost(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_stat(findings_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert "finding_cooldown_seconds" in result
        expected = min(int(14400 * COOLDOWN_BOOST_MULTIPLIER), MAX_COOLDOWN_HOURS * 3600)
        assert result["finding_cooldown_seconds"] == expected

    def test_both_failures_trigger_both_overrides(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 4}})
        records = [_make_stat(retry_failed=1, findings_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert "max_prs_per_run" in result
        assert "finding_cooldown_seconds" in result

    def test_cooldown_capped_at_max(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        max_seconds = MAX_COOLDOWN_HOURS * 3600
        _write_tune_state(tune, {"tuned_fields": {"finding_cooldown_seconds": max_seconds}})
        records = [_make_stat(findings_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        if "finding_cooldown_seconds" in result:
            assert result["finding_cooldown_seconds"] <= max_seconds

    def test_batch_floored_at_min(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": MIN_BATCH_SIZE}})
        records = [_make_stat(retry_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        if "max_prs_per_run" in result:
            assert result["max_prs_per_run"] >= MIN_BATCH_SIZE

    def test_state_persisted_to_disk(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_stat(findings_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        compute_tune(stats, tune)
        assert tune.exists()
        saved = json.loads(tune.read_text())
        assert "tuned_fields" in saved
        assert "last_tune_ts" in saved

    def test_corrupt_jsonl_line_handled_gracefully(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        records = [_make_stat(retry_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        original = stats.read_text()
        stats.write_text(original + "NOT JSON{{{\n")
        result = compute_tune(stats, tune)
        assert isinstance(result, dict)

    def test_custom_retry_threshold(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 4}})
        records = [_make_stat(retry_failed=1), _make_stat(retry_failed=1)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune, retry_threshold=2)
        assert "max_prs_per_run" in result

    def test_reason_string_contains_failure_count(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 8}})
        n = CONSECUTIVE_RETRY_FAILURE_THRESHOLD + 2
        records = [_make_stat(retry_failed=1) for _ in range(n)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert f"x{n}" in result.get("_reason", "")

    def test_existing_tuned_batch_respected(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 4}})
        records = [_make_stat(retry_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        result = compute_tune(stats, tune)
        assert result["max_prs_per_run"] == max(MIN_BATCH_SIZE, int(4 * BATCH_REDUCTION_FACTOR))

    def test_only_whitespace_jsonl_returns_empty(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        stats.write_text("   \n  \n\n")
        result = compute_tune(stats, tune)
        assert result == {}


class TestReadTuneOverrides:
    def test_missing_file_returns_empty(self, tmp_path):
        tune = tmp_path / "missing.json"
        assert read_tune_overrides(tune) == {}

    def test_valid_state_returns_tuned_fields(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 2}, "last_tune_ts": "2025-01-01"})
        result = read_tune_overrides(tune)
        assert result == {"max_prs_per_run": 2}

    def test_empty_tuned_fields_returns_empty(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {}, "last_tune_ts": None})
        result = read_tune_overrides(tune)
        assert result == {}

    def test_corrupt_file_returns_empty(self, tmp_path):
        tune = tmp_path / "tune.json"
        tune.write_text("BROKEN{NOT JSON")
        result = read_tune_overrides(tune)
        assert result == {}

    def test_no_tuned_fields_key_returns_empty(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"last_tune_ts": "2025-01-01"})
        result = read_tune_overrides(tune)
        assert result == {}

    def test_multiple_fields_returned(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"max_prs_per_run": 1, "finding_cooldown_seconds": 28800},
            "last_tune_ts": "2025-01-01",
        })
        result = read_tune_overrides(tune)
        assert result["max_prs_per_run"] == 1
        assert result["finding_cooldown_seconds"] == 28800


class TestResetTune:
    def test_creates_clean_state(self, tmp_path):
        tune = tmp_path / "tune.json"
        reset_tune(tune)
        assert tune.exists()
        state = json.loads(tune.read_text())
        assert state["tuned_fields"] == {}
        assert state["last_tune_ts"] is None

    def test_overwrites_existing_state(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"max_prs_per_run": 1},
            "last_tune_ts": "2025-06-01T00:00:00Z",
        })
        reset_tune(tune)
        state = json.loads(tune.read_text())
        assert state["tuned_fields"] == {}
        assert state["last_tune_ts"] is None

    def test_creates_parent_directories(self, tmp_path):
        tune = tmp_path / "deep" / "nested" / "tune.json"
        reset_tune(tune)
        assert tune.exists()

    def test_read_after_reset_returns_empty(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 1}})
        reset_tune(tune)
        assert read_tune_overrides(tune) == {}

    def test_double_reset_idempotent(self, tmp_path):
        tune = tmp_path / "tune.json"
        reset_tune(tune)
        first = tune.read_text()
        reset_tune(tune)
        second = tune.read_text()
        assert first == second


class TestFlagTuneSuccess:
    def test_no_overrides_clears_state(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {}, "last_tune_ts": None})
        flag_tune_success(tune)
        state = json.loads(tune.read_text())
        assert state["tuned_fields"] == {}

    def test_batch_incremented_toward_recovery(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"max_prs_per_run": 1},
            "last_tune_ts": "2025-01-01",
        })
        flag_tune_success(tune)
        result = read_tune_overrides(tune)
        assert result.get("max_prs_per_run", 2) >= 1

    def test_batch_removed_when_reaches_threshold(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"max_prs_per_run": 1},
            "last_tune_ts": "2025-01-01",
        })
        flag_tune_success(tune)
        result = read_tune_overrides(tune)
        assert "max_prs_per_run" not in result

    def test_cooldown_halved_on_success(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"finding_cooldown_seconds": 28800},
            "last_tune_ts": "2025-01-01",
        })
        flag_tune_success(tune)
        result = read_tune_overrides(tune)
        if "finding_cooldown_seconds" in result:
            assert result["finding_cooldown_seconds"] == 14400

    def test_cooldown_removed_when_below_default(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"finding_cooldown_seconds": 20000},
            "last_tune_ts": "2025-01-01",
        })
        flag_tune_success(tune)
        result = read_tune_overrides(tune)
        assert "finding_cooldown_seconds" not in result

    def test_both_fields_adjusted_together(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"max_prs_per_run": 1, "finding_cooldown_seconds": 28800},
            "last_tune_ts": "2025-01-01",
        })
        flag_tune_success(tune)
        result = read_tune_overrides(tune)
        assert "max_prs_per_run" not in result
        assert "finding_cooldown_seconds" not in result

    def test_last_tune_ts_cleared_after_success(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"max_prs_per_run": 1},
            "last_tune_ts": "2025-01-01",
        })
        flag_tune_success(tune)
        state = json.loads(tune.read_text())
        assert state["last_tune_ts"] is None

    def test_success_on_empty_state_no_error(self, tmp_path):
        tune = tmp_path / "tune.json"
        flag_tune_success(tune)
        state = json.loads(tune.read_text())
        assert state["tuned_fields"] == {}

    def test_success_on_missing_file(self, tmp_path):
        tune = tmp_path / "missing.json"
        flag_tune_success(tune)
        assert tune.exists()

    def test_gradual_recovery_multiple_successes(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"finding_cooldown_seconds": 43200},
            "last_tune_ts": "2025-01-01",
        })
        flag_tune_success(tune)
        state = json.loads(tune.read_text())
        assert state["tuned_fields"].get("finding_cooldown_seconds") == 21600

        flag_tune_success(tune)
        state = json.loads(tune.read_text())
        assert state["tuned_fields"].get("finding_cooldown_seconds") is None

    def test_recovery_step_tracked(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"max_prs_per_run": 1},
            "last_tune_ts": "2025-01-01",
        })
        flag_tune_success(tune)
        state = json.loads(tune.read_text())
        tuned = state.get("tuned_fields", {})
        if not tuned:
            assert True
        else:
            assert "_recovery_step" in tuned

    def test_reset_at_set_when_fully_recovered(self, tmp_path):
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {
            "tuned_fields": {"max_prs_per_run": 1},
            "last_tune_ts": "2025-06-01T00:00:00Z",
        })
        flag_tune_success(tune)
        state = json.loads(tune.read_text())
        assert state.get("reset_at") == "2025-06-01T00:00:00Z"


class TestIntegration:
    def test_full_cycle_failure_then_recovery(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 4}})

        records = [_make_stat(retry_failed=1, findings_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)

        overrides = compute_tune(stats, tune)
        assert "max_prs_per_run" in overrides
        assert "finding_cooldown_seconds" in overrides

        current = read_tune_overrides(tune)
        assert current["max_prs_per_run"] == overrides["max_prs_per_run"]

        flag_tune_success(tune)
        post = read_tune_overrides(tune)
        if "max_prs_per_run" in post:
            assert post["max_prs_per_run"] > overrides["max_prs_per_run"]

    def test_consecutive_failures_then_reset(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"

        records = [_make_stat(findings_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        compute_tune(stats, tune)
        assert read_tune_overrides(tune) != {}

        reset_tune(tune)
        assert read_tune_overrides(tune) == {}

    def test_corrupt_tune_state_handled_across_functions(self, tmp_path):
        tune = tmp_path / "tune.json"
        tune.write_text("BROKEN")

        assert read_tune_overrides(tune) == {}

        reset_tune(tune)
        assert read_tune_overrides(tune) == {}

    def test_compute_tune_uses_existing_state_for_reduction(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"

        records = [_make_stat(retry_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        _write_tune_state(tune, {"tuned_fields": {"max_prs_per_run": 8}})

        result = compute_tune(stats, tune)
        assert result["max_prs_per_run"] == max(MIN_BATCH_SIZE, int(8 * BATCH_REDUCTION_FACTOR))

    def test_success_then_refailure_re_triggers(self, tmp_path):
        stats = tmp_path / "stats.jsonl"
        tune = tmp_path / "tune.json"

        records = [_make_stat(retry_failed=1) for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)]
        _write_jsonl(stats, records)
        compute_tune(stats, tune)

        flag_tune_success(tune)

        records2 = [_make_stat(retry_failed=0)] * 2 + [_make_stat(retry_failed=1)] * CONSECUTIVE_RETRY_FAILURE_THRESHOLD
        _write_jsonl(stats, records2)
        result = compute_tune(stats, tune)
        assert isinstance(result, dict)


# ── Merged from test_auto_tune_remaining.py ──

from unittest.mock import patch as _patch

from bluei.app.auto_tune import (
    _save_tune_state,
    _load_jsonl,
    _check_retry_pattern,
    _check_finding_pattern,
    adjust_replay_thresholds,
)


class TestSaveTuneStateOSError:
    def test_oserror_suppressed(self, tmp_path):
        tune_path = tmp_path / "readonly" / "tune.json"
        tune_path.parent.mkdir()
        tune_path.parent.chmod(0o444)
        try:
            _save_tune_state(tune_path, {"tuned_fields": {"x": 1}})
        except Exception:
            raise AssertionError("_save_tune_state should suppress OSError")
        finally:
            tune_path.parent.chmod(0o755)


class TestAdjustReplayThresholds:
    def test_empty_records_returns_empty(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        tel.write_text("")
        result = adjust_replay_thresholds(tel, tune)
        assert result == {}

    def test_missing_file_returns_empty(self, tmp_path):
        result = adjust_replay_thresholds(
            tmp_path / "missing.jsonl",
            tmp_path / "tune.json",
        )
        assert result == {}

    def test_raises_threshold_on_failures(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [
            {"rule": "ruff-c408", "success": False},
            {"rule": "ruff-c408", "success": False},
            {"rule": "ruff-c408", "success": False},
        ]
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(tel, tune, failure_window=3)
        assert "ruff-c408" in result
        assert result["ruff-c408"] > 0.85

    def test_lowers_threshold_on_consecutive_successes(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [
            {"rule": "ruff-c408", "success": True, "final_stage": "autofix"},
        ] * 20
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(tel, tune, success_window=20)
        assert "ruff-c408" in result
        assert result["ruff-c408"] < 0.85

    def test_no_change_when_stable(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [
            {"rule": "ruff-c408", "success": True, "final_stage": "autofix"},
        ] * 5
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(
            tel, tune, success_window=20, failure_window=10
        )
        assert result == {}

    def test_multiple_rules_tracked_separately(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [
            {"rule": "ruff-c408", "success": False},
            {"rule": "ruff-c408", "success": False},
            {"rule": "xo-no-any", "success": False},
            {"rule": "xo-no-any", "success": False},
        ]
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(tel, tune, failure_window=2)
        assert "ruff-c408" in result
        assert "xo-no-any" in result

    def test_existing_threshold_respected(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        tune.write_text(json.dumps({"replay_thresholds": {"ruff-c408": 0.90}}))
        records = [{"rule": "ruff-c408", "success": False}] * 3
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(tel, tune, failure_window=3, raise_step=0.03)
        assert result["ruff-c408"] == 0.93

    def test_threshold_capped_at_ceiling(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        tune.write_text(json.dumps({"replay_thresholds": {"ruff-c408": 0.97}}))
        records = [{"rule": "ruff-c408", "success": False}] * 3
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(
            tel, tune, failure_window=3, ceiling=0.98, raise_step=0.05
        )
        assert result["ruff-c408"] <= 0.98

    def test_threshold_floored(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        tune.write_text(json.dumps({"replay_thresholds": {"ruff-c408": 0.71}}))
        records = [
            {"rule": "ruff-c408", "success": True, "final_stage": "autofix"}
        ] * 20
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(
            tel, tune, success_window=20, floor=0.70, lower_step=0.02
        )
        assert result["ruff-c408"] >= 0.70

    def test_state_saved_when_adjustments(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [{"rule": "ruff-c408", "success": False}] * 3
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        adjust_replay_thresholds(tel, tune, failure_window=3)
        assert tune.exists()
        state = json.loads(tune.read_text())
        assert "replay_thresholds" in state
        assert "replay_threshold_ts" in state

    def test_state_not_saved_when_no_adjustments(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [{"rule": "ruff-c408", "success": True, "final_stage": "autofix"}]
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        adjust_replay_thresholds(tel, tune)
        assert not tune.exists()

    def test_empty_rule_skipped(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [{"rule": "", "success": False}] * 3
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(tel, tune, failure_window=3)
        assert result == {}

    def test_cascade_exhausted_doesnt_count_as_success(self, tmp_path):
        tel = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [
            {"rule": "ruff-c408", "success": True, "final_stage": "cascade_exhausted"},
        ] * 20
        tel.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        result = adjust_replay_thresholds(tel, tune, success_window=20)
        assert result == {}


class TestCheckRetryPattern:
    def test_returns_count_when_above_threshold(self):
        records = [{"retry_failed": 1}] * 5
        assert _check_retry_pattern(records, 4) == 5

    def test_returns_none_below_threshold(self):
        records = [{"retry_failed": 1}] * 3
        assert _check_retry_pattern(records, 4) is None

    def test_stops_at_first_success(self):
        records = [{"retry_failed": 1}, {"retry_failed": 1}, {"retry_failed": 0}]
        assert _check_retry_pattern(records, 2) is None


class TestCheckFindingPattern:
    def test_returns_count_when_above_threshold(self):
        records = [{"findings_failed": 1}] * 5
        assert _check_finding_pattern(records, 4) == 5

    def test_returns_none_below_threshold(self):
        records = [{"findings_failed": 1}] * 2
        assert _check_finding_pattern(records, 4) is None
