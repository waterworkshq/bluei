"""Tests for CycleSignalStore — cross-cycle suppression bridge."""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Import through qa_agent since that's where CycleSignalStore lives
from qa_agent.cycle_signals import CycleSignalStore, SUPPRESSION_DURATION_CYCLES


class TestCycleSignalStore:
    """Core read/write/prune behavior."""

    def test_load_empty_when_missing(self, tmp_path: Path):
        store = CycleSignalStore(tmp_path / "cycle_signals.json")
        data = store.load()
        assert data == {"suppressed_rules": {}}

    def test_load_empty_when_corrupt(self, tmp_path: Path):
        f = tmp_path / "cycle_signals.json"
        f.write_text("{invalid json")
        store = CycleSignalStore(f)
        data = store.load()
        assert data == {"suppressed_rules": {}}

    def test_suppress_rule_writes_entry(self, tmp_path: Path):
        f = tmp_path / "cycle_signals.json"
        store = CycleSignalStore(f)
        store.suppress_rule("ruff-b904", "retry_failed x5 consecutive cycles", duration_cycles=24)

        data = json.loads(f.read_text())
        entry = data["suppressed_rules"]["ruff-b904"]
        assert entry["reason"] == "retry_failed x5 consecutive cycles"
        assert entry["duration_cycles"] == 24
        assert "expires_at" in entry

    def test_is_rule_suppressed_returns_reason(self, tmp_path: Path):
        store = CycleSignalStore(tmp_path / "cycle_signals.json")
        store.suppress_rule("ruff-c408", "test suppression", duration_cycles=48)

        reason = store.is_rule_suppressed("ruff-c408")
        assert reason == "test suppression"

    def test_is_rule_suppressed_returns_none_for_unknown(self, tmp_path: Path):
        store = CycleSignalStore(tmp_path / "cycle_signals.json")
        assert store.is_rule_suppressed("nonexistent-rule") is None

    def test_lift_suppression_removes_entry(self, tmp_path: Path):
        store = CycleSignalStore(tmp_path / "cycle_signals.json")
        store.suppress_rule("ruff-b007", "test", duration_cycles=24)
        assert store.is_rule_suppressed("ruff-b007") is not None

        store.lift_suppression("ruff-b007")
        assert store.is_rule_suppressed("ruff-b007") is None

    def test_prune_expired_on_read(self, tmp_path: Path):
        """Expired entries are removed when is_rule_suppressed is called."""
        f = tmp_path / "cycle_signals.json"
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        f.write_text(json.dumps({
            "suppressed_rules": {
                "ruff-expired": {
                    "expires_at": expired,
                    "reason": "too old",
                    "duration_cycles": 1,
                },
                "ruff-active": {
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                    "reason": "still valid",
                    "duration_cycles": 48,
                },
            },
        }))

        store = CycleSignalStore(f)
        assert store.is_rule_suppressed("ruff-expired") is None  # pruned
        assert store.is_rule_suppressed("ruff-active") == "still valid"  # kept

        # Verify file was cleaned up
        data = json.loads(f.read_text())
        assert "ruff-expired" not in data["suppressed_rules"]
        assert "ruff-active" in data["suppressed_rules"]

    def test_get_all_suppressed_filters_expired(self, tmp_path: Path):
        f = tmp_path / "cycle_signals.json"
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        f.write_text(json.dumps({
            "suppressed_rules": {
                "ruff-gone": {"expires_at": expired, "reason": "gone", "duration_cycles": 1},
                "ruff-here": {
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                    "reason": "here", "duration_cycles": 48,
                },
            },
        }))

        store = CycleSignalStore(f)
        all_suppressed = store.get_all_suppressed()
        assert "ruff-gone" not in all_suppressed
        assert "ruff-here" in all_suppressed

    def test_suppress_rule_does_not_clear_other_rules(self, tmp_path: Path):
        f = tmp_path / "cycle_signals.json"
        f.write_text(json.dumps({
            "suppressed_rules": {
                "ruff-existing": {
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                    "reason": "existing",
                    "duration_cycles": 48,
                },
            },
        }))

        store = CycleSignalStore(f)
        store.suppress_rule("ruff-new", "new entry", duration_cycles=24)

        data = json.loads(f.read_text())
        assert "ruff-existing" in data["suppressed_rules"]
        assert "ruff-new" in data["suppressed_rules"]

    def test_multiple_suppressions_independent(self, tmp_path: Path):
        store = CycleSignalStore(tmp_path / "cycle_signals.json")
        store.suppress_rule("ruff-b904", "reason a", duration_cycles=24)
        store.suppress_rule("ruff-c408", "reason b", duration_cycles=48)

        assert store.is_rule_suppressed("ruff-b904") == "reason a"
        assert store.is_rule_suppressed("ruff-c408") == "reason b"

    def test_lift_suppression_nonexistent_does_not_error(self, tmp_path: Path):
        store = CycleSignalStore(tmp_path / "cycle_signals.json")
        store.lift_suppression("does-not-exist")  # should not raise


class TestRecordRetryFailurePattern:
    """record_retry_failure_pattern logic."""

    def test_retry_failure_suppresses_rule(self, tmp_path: Path):
        from qa_agent.cycle_signals import record_retry_failure_pattern, RETRY_FAILURE_SUPPRESSION_THRESHOLD
        f = tmp_path / "cycle_signals.json"
        stats_f = tmp_path / "review_stats.jsonl"
        # Write enough consecutive failure records
        stats_f.write_text("\n".join(
            json.dumps({"retry_failed": 1}) for _ in range(RETRY_FAILURE_SUPPRESSION_THRESHOLD)
        ))

        store = CycleSignalStore(f)
        result = record_retry_failure_pattern(store, stats_f, "__global__")
        assert result is True
        assert store.is_rule_suppressed("__global__") is not None

    def test_clean_record_does_not_suppress(self, tmp_path: Path):
        from qa_agent.cycle_signals import record_retry_failure_pattern
        f = tmp_path / "cycle_signals.json"
        stats_f = tmp_path / "review_stats.jsonl"
        stats_f.write_text(json.dumps({"retry_failed": 0}))

        store = CycleSignalStore(f)
        result = record_retry_failure_pattern(store, stats_f, "__global__")
        assert result is False
