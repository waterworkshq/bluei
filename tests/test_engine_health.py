"""Tests for bluei/engine/health.py — cost enrichment and health summaries."""

import json
from pathlib import Path

import pytest

from bluei.engine.health import build_cost_health_summary, enrich_health_with_cost


def _write_jsonl(path: Path, records: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TestEnrichHealthWithCost:
    def test_returns_unchanged_when_no_cost_log(self):
        summary = {"score": 85.0}
        result = enrich_health_with_cost(summary)
        assert result is summary
        assert "cost" not in result

    def test_returns_unchanged_when_cost_log_missing(self, tmp_path):
        summary = {"score": 85.0}
        result = enrich_health_with_cost(
            summary, cost_log_path=tmp_path / "nonexistent.jsonl"
        )
        assert "cost" not in result

    def test_returns_unchanged_when_none_path(self):
        summary = {"score": 85.0}
        result = enrich_health_with_cost(summary, cost_log_path=None)
        assert "cost" not in result

    def test_enriches_with_single_entry(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(log_path, [{"cost": 0.05, "model": "gpt-4"}])

        summary = {"score": 85.0}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert "cost" in result
        assert result["cost"]["total_cost"] == 0.05
        assert result["cost"]["total_invocations"] == 1
        assert result["cost"]["avg_cost_per_run"] == 0.05

    def test_enriches_with_multiple_entries(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {"cost": 0.10, "model": "gpt-4"},
                {"cost": 0.05, "model": "gpt-3.5"},
                {"cost": 0.08, "model": "gpt-4"},
            ],
        )

        summary = {"score": 80.0}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert result["cost"]["total_cost"] == 0.23
        assert result["cost"]["total_invocations"] == 3
        assert result["cost"]["per_model"]["gpt-4"]["count"] == 2
        assert result["cost"]["per_model"]["gpt-3.5"]["count"] == 1

    def test_uses_total_runs_for_avg(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(log_path, [{"cost": 0.30, "model": "gpt-4"}])

        summary = {"score": 80.0}
        result = enrich_health_with_cost(summary, cost_log_path=log_path, total_runs=10)
        assert result["cost"]["avg_cost_per_run"] == round(0.30 / 10, 6)

    def test_returns_unchanged_on_corrupt_jsonl(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        log_path.write_text("not json\n")

        summary = {"score": 80.0}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert "cost" not in result

    def test_returns_unchanged_on_empty_entries(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        log_path.write_text("")

        summary = {"score": 80.0}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert "cost" not in result

    def test_handles_missing_cost_field(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(log_path, [{"model": "gpt-4"}])

        summary = {"score": 80.0}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert result["cost"]["total_cost"] == 0.0

    def test_per_model_breakdown(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {"cost": 0.10, "model": "gpt-4"},
                {"cost": 0.05, "model": "claude"},
                {"cost": 0.15, "model": "gpt-4"},
            ],
        )

        summary = {}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert result["cost"]["per_model"]["gpt-4"]["count"] == 2
        assert result["cost"]["per_model"]["gpt-4"]["cost"] == round(0.25, 6)
        assert result["cost"]["per_model"]["claude"]["count"] == 1
        assert result["cost"]["per_model"]["claude"]["cost"] == 0.05

    def test_zero_total_runs_uses_invocations(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {"cost": 0.10, "model": "gpt-4"},
                {"cost": 0.20, "model": "gpt-4"},
            ],
        )

        summary = {}
        result = enrich_health_with_cost(summary, cost_log_path=log_path, total_runs=0)
        assert result["cost"]["avg_cost_per_run"] == round(0.30 / 2, 6)

    def test_handles_os_error(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        log_path.mkdir()

        summary = {}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert "cost" not in result


class TestBuildCostHealthSummary:
    def test_returns_empty_without_log(self):
        result = build_cost_health_summary()
        assert result == {}

    def test_returns_cost_with_log(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(log_path, [{"cost": 0.15, "model": "gpt-4"}])

        result = build_cost_health_summary(cost_log_path=log_path)
        assert "cost" in result
        assert result["cost"]["total_cost"] == 0.15
