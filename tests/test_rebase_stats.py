"""Tests for rebase telemetry — log, load, and summarise."""

from __future__ import annotations

from pathlib import Path

import pytest

from bluei.engine.rebase_stats import (
    log_rebase_stats,
    load_rebase_stats,
    summary_from_stats,
)


class TestLogAndLoad:
    """Write/read roundtrip for rebase telemetry."""

    def test_log_and_load_roundtrip(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "rebase_stats.jsonl"
        stats = {
            "rebases_attempted": 5,
            "rebases_succeeded": 4,
            "rebases_conflicted": 1,
            "rebases_skipped": 0,
            "duration_seconds": 12.5,
        }
        log_rebase_stats(stats_path, stats)

        entries = load_rebase_stats(stats_path)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["rebases_attempted"] == 5
        assert entry["rebases_succeeded"] == 4
        assert entry["rebases_conflicted"] == 1
        assert entry["rebases_skipped"] == 0
        assert entry["duration_seconds"] == 12.5
        assert "timestamp" in entry

    def test_multiple_entries(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "rebase_stats.jsonl"
        for i in range(3):
            log_rebase_stats(stats_path, {"rebases_attempted": i + 1})

        entries = load_rebase_stats(stats_path, limit=10)
        assert len(entries) == 3

    def test_limit_returns_newest_first(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "rebase_stats.jsonl"
        for i in range(5):
            log_rebase_stats(stats_path, {"rebases_attempted": i + 1})

        entries = load_rebase_stats(stats_path, limit=2)
        assert len(entries) == 2
        # Newest first
        assert entries[0]["rebases_attempted"] == 5
        assert entries[1]["rebases_attempted"] == 4

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "nonexistent.jsonl"
        entries = load_rebase_stats(stats_path)
        assert entries == []

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        entries = load_rebase_stats(tmp_path / "missing.jsonl")
        assert entries == []

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "sub" / "dir" / "rebase_stats.jsonl"
        log_rebase_stats(stats_path, {"rebases_attempted": 1})
        assert stats_path.exists()

    def test_handles_bad_json_lines_gracefully(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "rebase_stats.jsonl"
        stats_path.write_text('{"valid": 1}\ncorrupted_line\n{"valid": 2}\n')
        entries = load_rebase_stats(stats_path)
        assert len(entries) == 2  # Only valid lines
        assert entries[0]["valid"] == 2
        assert entries[1]["valid"] == 1

    def test_oserror_on_read_returns_empty(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "noperm" / "rebase_stats.jsonl"
        stats_path.parent.mkdir()
        stats_path.write_text('{"test": 1}\n')
        stats_path.chmod(0o000)
        try:
            entries = load_rebase_stats(stats_path)
            assert entries == []
        finally:
            stats_path.chmod(0o644)


class TestSummaryFromStats:
    """Aggregation from telemetry entries."""

    def test_empty_entries(self) -> None:
        summary = summary_from_stats([])
        assert summary["total_sweeps"] == 0
        assert summary["total_rebased"] == 0
        assert summary["total_conflicted"] == 0
        assert summary["total_skipped"] == 0
        assert summary["success_rate_pct"] == 0.0

    def test_aggregates_single_entry(self) -> None:
        entries = [
            {
                "rebases_attempted": 10,
                "rebases_succeeded": 8,
                "rebases_conflicted": 2,
                "rebases_skipped": 0,
                "duration_seconds": 30.0,
                "timestamp": "2025-05-13T00:00:00",
            }
        ]
        summary = summary_from_stats(entries)
        assert summary["total_sweeps"] == 1
        assert summary["total_rebased"] == 8
        assert summary["total_conflicted"] == 2
        assert summary["total_skipped"] == 0
        assert summary["total_attempted"] == 10
        assert summary["avg_duration_seconds"] == 30.0
        assert summary["success_rate_pct"] == 80.0
        assert summary["latest_timestamp"] == "2025-05-13T00:00:00"

    def test_aggregates_multiple_entries(self) -> None:
        entries = [
            {
                "rebases_attempted": 10,
                "rebases_succeeded": 8,
                "rebases_conflicted": 2,
                "rebases_skipped": 0,
                "duration_seconds": 20.0,
                "timestamp": "2025-05-13T01:00:00",
            },
            {
                "rebases_attempted": 5,
                "rebases_succeeded": 5,
                "rebases_conflicted": 0,
                "rebases_skipped": 0,
                "duration_seconds": 10.0,
                "timestamp": "2025-05-13T02:00:00",
            },
        ]
        summary = summary_from_stats(entries)
        assert summary["total_sweeps"] == 2
        assert summary["total_rebased"] == 13
        assert summary["total_conflicted"] == 2
        assert summary["total_attempted"] == 15
        assert summary["avg_duration_seconds"] == 15.0
        assert summary["success_rate_pct"] == pytest.approx(86.7, rel=1e-2)

    def test_zero_attempted_does_not_divide(self) -> None:
        entries = [
            {
                "rebases_attempted": 0,
                "rebases_succeeded": 0,
                "rebases_conflicted": 0,
                "rebases_skipped": 5,
                "duration_seconds": 1.0,
                "timestamp": "t1",
            },
        ]
        summary = summary_from_stats(entries)
        assert summary["success_rate_pct"] == 0.0

    def test_handles_missing_keys_gracefully(self) -> None:
        entries = [
            {"timestamp": "t1"},  # No numeric fields at all
        ]
        summary = summary_from_stats(entries)
        assert summary["total_sweeps"] == 1
        assert summary["total_rebased"] == 0
        assert summary["total_attempted"] == 0
        assert summary["success_rate_pct"] == 0.0

    def test_latest_timestamp_from_newest(self) -> None:
        entries = [
            {
                "rebases_attempted": 1,
                "rebases_succeeded": 1,
                "rebases_conflicted": 0,
                "rebases_skipped": 0,
                "duration_seconds": 5.0,
                "timestamp": "2025-05-13T10:00:00",
            },
            {
                "rebases_attempted": 1,
                "rebases_succeeded": 1,
                "rebases_conflicted": 0,
                "rebases_skipped": 0,
                "duration_seconds": 5.0,
                "timestamp": "2025-05-13T11:00:00",
            },
        ]
        summary = summary_from_stats(entries)
        assert (
            summary["latest_timestamp"] == "2025-05-13T10:00:00"
        )  # First entry (newest first)
