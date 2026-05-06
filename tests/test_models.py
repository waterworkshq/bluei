#!/usr/bin/env python3
"""Tests for qa_agent/models.py standalone functions and model round-trips."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.models import (
    generate_id,
    now_iso,
    Finding,
    HealthScore,
    Baseline,
    Repo,
    RepoConfig,
    RepoStatus,
    SafetyMode,
    SafetyProfile,
    ReviewMode,
)


class TestGenerateId:
    def test_generate_id_no_prefix(self):
        id1 = generate_id()
        id2 = generate_id()
        assert id1 and id2
        assert id1 != id2  # unique

    def test_generate_id_with_prefix(self):
        id_val = generate_id("qa")
        assert id_val.startswith("qa-")
        assert len(id_val.split("-")) >= 3  # prefix, ts, unique

    def test_generate_id_prefix_only(self):
        id_val = generate_id("test")
        parts = id_val.split("-")
        assert parts[0] == "test"


class TestNowIso:
    def test_now_iso_returns_string(self):
        result = now_iso()
        assert isinstance(result, str)
        assert result  # not empty

    def test_now_iso_contains_z(self):
        result = now_iso()
        assert "Z" in result or "+" in result  # ISO format with timezone

    def test_now_iso_reasonable_length(self):
        result = now_iso()
        assert 20 <= len(result) <= 35  # ISO with microseconds can be 32+


class TestHealthScoreBand:
    def test_band_excellent(self):
        hs = HealthScore(score=95.0, components={}, calculated_at="2026-03-22T00:00:00Z")
        assert hs.band == "excellent"
        assert hs.color == "green"

    def test_band_good(self):
        hs = HealthScore(score=80.0, components={}, calculated_at="2026-03-22T00:00:00Z")
        assert hs.band == "good"
        assert hs.color == "blue"

    def test_band_needs_work(self):
        hs = HealthScore(score=60.0, components={}, calculated_at="2026-03-22T00:00:00Z")
        assert hs.band == "needs_work"
        assert hs.color == "yellow"

    def test_band_poor(self):
        hs = HealthScore(score=40.0, components={}, calculated_at="2026-03-22T00:00:00Z")
        assert hs.band == "poor"
        assert hs.color == "orange"

    def test_band_critical(self):
        hs = HealthScore(score=15.0, components={}, calculated_at="2026-03-22T00:00:00Z")
        assert hs.band == "critical"
        assert hs.color == "red"

    def test_band_at_thresholds(self):
        for score, expected_band in [(90, "excellent"), (70, "good"), (50, "needs_work"), (30, "poor"), (0, "critical")]:
            hs = HealthScore(score=score, components={}, calculated_at="2026-03-22T00:00:00Z")
            assert hs.band == expected_band, f"score={score}"


class TestFindingRoundTrip:
    def test_finding_to_dict(self):
        f = Finding(
            finding_id="fid-123",
            repo="ky",
            path="src/main.ts",
            line=42,
            rule="complexity",
            snippet="Method too complex",
            confidence=0.8,
            quick_win=False,
            safe_to_autofix=True,
        )
        d = f.to_dict()
        assert d["finding_id"] == "fid-123"
        assert d["rule"] == "complexity"
        assert d["confidence"] == 0.8

    def test_finding_from_dict(self):
        d = {
            "finding_id": "fid-456",
            "repo": "ky",
            "path": "src/main.ts",
            "line": 10,
            "rule": "max-lines",
            "snippet": "Too many lines",
            "confidence": 0.75,
            "quick_win": True,
            "safe_to_autofix": True,
        }
        f = Finding.from_dict(d)
        assert f.finding_id == "fid-456"
        assert f.rule == "max-lines"

    def test_finding_from_dict_ignores_unknown_fields(self):
        d = {
            "finding_id": "fid-789",
            "repo": "ky",
            "path": "src/main.ts",
            "line": 11,
            "rule": "xo-no-warning-comments",
            "snippet": "TODO",
            "confidence": 0.8,
            "quick_win": True,
            "safe_to_autofix": True,
            "fix_attempts": 2,
            "last_fix_error": "boom",
            "unexpected_future_field": "ignore-me",
        }
        f = Finding.from_dict(d)
        assert f.finding_id == "fid-789"
        assert f.fix_attempts == 2
        assert f.last_fix_error == "boom"


class TestHealthScoreRoundTrip:
    def test_health_score_to_dict(self):
        hs = HealthScore(
            score=85.0,
            components={"lint": 90.0, "complexity": 80.0},
            calculated_at="2026-03-22T00:00:00Z",
        )
        d = hs.to_dict()
        assert d["score"] == 85.0
        assert d["band"] == "good"
        assert d["color"] == "blue"
        assert d["components"]["lint"] == 90.0


class TestBaselineRoundTrip:
    def test_baseline_to_dict(self):
        b = Baseline(
            id="baseline-1",
            repo_id="repo-ky",
            captured_at="2026-03-22T00:00:00Z",
            findings_total=10,
            findings_by_category={"lint": 5, "complexity": 5},
            findings_by_severity={"medium": 8, "low": 2},
            health_score=85.0,
            health_components={"lint": 90.0},
            findings_file="/tmp/findings.jsonl",
        )
        d = b.to_dict()
        assert d["findings_total"] == 10
        assert d["health_score"] == 85.0


class TestRepoStatusEnum:
    def test_status_values(self):
        assert RepoStatus.IDLE.value == "idle"
        assert RepoStatus.RUNNING.value == "running"
        assert RepoStatus.READY.value == "ready"
        assert RepoStatus.PAUSED.value == "paused"
        assert RepoStatus.ERROR.value == "error"


class TestSafetyModeEnum:
    def test_safety_mode_values(self):
        assert SafetyMode.OBSERVE.value == "observe"
        assert SafetyMode.ISSUE_ONLY.value == "issue-only"
        assert SafetyMode.PR.value == "pr"
        assert SafetyMode.MERGE.value == "merge"


class TestSafetyProfileEnum:
    def test_safety_profile_values(self):
        assert SafetyProfile.CONSERVATIVE.value == "conservative"
        assert SafetyProfile.BALANCED.value == "balanced"
        assert SafetyProfile.AGGRESSIVE.value == "aggressive"


class TestReviewModeEnum:
    def test_review_mode_values(self):
        assert ReviewMode.OBSERVATION.value == "observation"
        assert ReviewMode.AUTONOMOUS_REVIEW.value == "autonomous-review"
        assert ReviewMode.REMEDIATION.value == "remediation"

    def test_repo_config_default_review_mode(self):
        cfg = RepoConfig(
            id="repo-test",
            name="test",
            path="/tmp/test",
            language="python",
        )
        assert cfg.review_care.get("mode") == "observation"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
