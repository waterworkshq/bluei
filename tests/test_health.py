#!/usr/bin/env python3
"""Pytest-native tests for Health Engine."""

from unittest.mock import MagicMock

import pytest

from bluei.app.models import HealthScore
from bluei.app.health import HealthEngine, HealthWeights


def test_empty_findings_perfect_score():
    engine = HealthEngine()
    score = engine.calculate([])
    assert score.score == 100.0


def test_critical_finding_reduces_score(make_app_finding):
    engine = HealthEngine()
    score = engine.calculate([make_app_finding(confidence=0.95, safe_to_autofix=False)])
    assert score.score < 100.0


def test_component_scores_calculated(make_app_finding):
    engine = HealthEngine()
    findings = [
        make_app_finding(
            rule="type-any", snippet="any", confidence=0.85, safe_to_autofix=True
        )
    ]
    score = engine.calculate(findings)
    assert "code_quality" in score.components
    assert "test_coverage" in score.components
    assert "documentation" in score.components
    assert "type_safety" in score.components
    assert "performance" in score.components


def test_health_score_bands():
    assert (
        HealthScore(score=95, components={}, calculated_at="2026-01-01").band
        == "excellent"
    )
    assert (
        HealthScore(score=75, components={}, calculated_at="2026-01-01").band == "good"
    )
    assert (
        HealthScore(score=55, components={}, calculated_at="2026-01-01").band
        == "needs_work"
    )
    assert (
        HealthScore(score=35, components={}, calculated_at="2026-01-01").band == "poor"
    )
    assert (
        HealthScore(score=15, components={}, calculated_at="2026-01-01").band
        == "critical"
    )


def test_findings_by_category(make_app_finding):
    engine = HealthEngine()
    findings = [
        make_app_finding(rule="type-any", snippet="any", safe_to_autofix=True),
        make_app_finding(
            finding_id="f2",
            line=20,
            rule="test-missing",
            snippet="no test",
            confidence=0.80,
            safe_to_autofix=True,
        ),
    ]
    by_cat = engine.get_findings_by_category(findings)
    assert "type_safety" in by_cat
    assert "test_coverage" in by_cat


def test_findings_by_severity(make_app_finding):
    engine = HealthEngine()
    findings = [
        make_app_finding(confidence=0.95, safe_to_autofix=False),
        make_app_finding(
            finding_id="f2",
            line=20,
            rule="warn",
            snippet="warn",
            confidence=0.70,
            quick_win=True,
            safe_to_autofix=True,
        ),
    ]
    by_sev = engine.get_findings_by_severity(findings)
    assert by_sev["critical"] == 1
    assert by_sev["low"] == 1


def test_custom_weights():
    weights = HealthWeights(
        code_quality=0.40,
        test_coverage=0.30,
        documentation=0.15,
        type_safety=0.10,
        performance=0.05,
    )
    engine = HealthEngine(weights=weights)
    assert engine.weights.code_quality == 0.40
    assert engine.weights.test_coverage == 0.30


def test_baseline_creation(make_app_finding):
    engine = HealthEngine()
    findings = [make_app_finding(safe_to_autofix=True)]
    score = engine.calculate(findings)
    baseline = engine.create_baseline(
        repo_id="test-repo",
        findings=findings,
        health=score,
        findings_file="/tmp/findings.jsonl",
    )
    assert baseline.repo_id == "test-repo"
    assert baseline.findings_total == 1
    assert baseline.health_score == score.score


# ── Additional health tests (from phase 8 extraction) ────────────────


class TestHealthWeightsUnknownField:
    def test_unknown_field_raises_type_error(self):
        with pytest.raises(TypeError, match="Unknown HealthWeights fields"):
            HealthWeights(typo_field=0.5)


class TestHealthWeightsLegacyCompat:
    def test_code_quality_distributes(self):
        w = HealthWeights(code_quality=0.40)
        assert w.code_quality == pytest.approx(0.40, abs=0.01)
        assert w.bug_quality > 0
        assert w.lint_quality > 0


class TestHealthEngineSeverityEdgeCases:
    def test_get_severity_medium_boundary(self, make_app_finding):
        engine = HealthEngine()
        f = make_app_finding(confidence=0.76, safe_to_autofix=False)
        assert engine._get_severity(f) == "medium"

    def test_get_severity_high_boundary(self, make_app_finding):
        engine = HealthEngine()
        f = make_app_finding(confidence=0.85, safe_to_autofix=False)
        assert engine._get_severity(f) == "high"

    def test_get_severity_low(self, make_app_finding):
        engine = HealthEngine()
        f = make_app_finding(confidence=0.50, safe_to_autofix=False)
        assert engine._get_severity(f) == "low"


class TestHealthEngineGetComponent:
    def test_emergent_rule_with_component_override(self, make_app_finding):
        er = MagicMock()
        er.rule_id = "custom-001"
        er.component_override = "test_gaps"
        er.weight_multiplier = 0.5
        engine = HealthEngine(emergent_rules=[er])
        f = make_app_finding(rule="emergent:custom-001", safe_to_autofix=False)
        assert engine._get_component(f) == "test_gaps"

    def test_emergent_rule_without_override(self, make_app_finding):
        er = MagicMock()
        er.rule_id = "custom-002"
        er.component_override = None
        engine = HealthEngine(emergent_rules=[er])
        f = make_app_finding(rule="emergent:custom-002", safe_to_autofix=False)
        assert engine._get_component(f) == "bug_quality"

    def test_category_inference_by_keyword_in_rule(self, make_app_finding):
        engine = HealthEngine()
        f = make_app_finding(rule="some-perf-issue", safe_to_autofix=False)
        assert engine._get_component(f) == "performance"

    def test_default_fallback(self, make_app_finding):
        engine = HealthEngine()
        f = make_app_finding(rule="totally-unknown-xyz123", safe_to_autofix=False)
        assert engine._get_component(f) == "bug_quality"


class TestHealthEngineCalculateWithCoverageData:
    def test_coverage_data_overrides(self):
        engine = HealthEngine()
        score = engine.calculate([], coverage_data={"percentage": 75.0})
        assert score.components["test_coverage"] == 75.0


class TestHealthEngineImprovementBonus:
    def test_positive_improvement_gives_bonus(self):
        engine = HealthEngine()
        score = engine.calculate([], baseline_score=80.0)
        assert score.score > 100.0

    def test_negative_improvement_no_bonus(self):
        engine = HealthEngine()
        score = engine.calculate([], baseline_score=200.0)
        assert score.score == 100.0


class TestHealthEnginePrioritizeIssues:
    def test_prioritize_returns_sorted(self, make_app_finding):
        engine = HealthEngine()
        findings = [
            make_app_finding(finding_id="f1", confidence=0.50, safe_to_autofix=False),
            make_app_finding(finding_id="f2", confidence=0.95, safe_to_autofix=True),
            make_app_finding(finding_id="f3", confidence=0.80, safe_to_autofix=False),
        ]
        issues = engine.prioritize_issues(findings, max_issues=2)
        assert len(issues) == 2
        assert issues[0].priority_score >= issues[1].priority_score

    def test_prioritize_urgency_critical(self, make_app_finding):
        engine = HealthEngine()
        f = make_app_finding(confidence=0.95, safe_to_autofix=True)
        issues = engine.prioritize_issues([f])
        assert issues[0].urgency == "critical"

    def test_prioritize_urgency_low(self, make_app_finding):
        engine = HealthEngine()
        f = make_app_finding(confidence=0.50, safe_to_autofix=False)
        issues = engine.prioritize_issues([f])
        assert issues[0].urgency == "low"

    def test_prioritize_reasons(self, make_app_finding):
        engine = HealthEngine()
        f = make_app_finding(
            confidence=0.95, rule="discount-math-sign", safe_to_autofix=True
        )
        issues = engine.prioritize_issues([f])
        assert "quick win" in issues[0].reason
        assert "critical severity" in issues[0].reason
        assert "potential bug" in issues[0].reason

    def test_prioritize_empty(self):
        engine = HealthEngine()
        assert engine.prioritize_issues([]) == []


class TestHealthEngineSaveSnapshot:
    def test_save_and_read(self, tmp_path):
        engine = HealthEngine()
        score = HealthScore(
            score=85.0, components={"bug_quality": 90.0}, calculated_at="2026-01-01"
        )
        engine.save_health_snapshot("repo", score, 5, tmp_path)
        history = engine.get_health_history("repo", tmp_path)
        assert len(history) == 1
        assert history[0]["score"] == 85.0

    def test_get_history_missing_file(self, tmp_path):
        engine = HealthEngine()
        assert engine.get_health_history("repo", tmp_path) == []

    def test_get_history_truncation(self, tmp_path):
        engine = HealthEngine()
        score = HealthScore(score=80.0, components={}, calculated_at="2026-01-01")
        for i in range(5):
            engine.save_health_snapshot("repo", score, i, tmp_path)
        history = engine.get_health_history("repo", tmp_path, days=2)
        assert len(history) == 2


class TestHealthEngineImprovementSummaryExt:
    def test_improvement(self):
        engine = HealthEngine()
        summary = engine.get_improvement_summary(
            current_score=85.0,
            baseline_score=70.0,
            current_findings={"bug_quality": 2},
            baseline_findings={"bug_quality": 5},
        )
        assert summary["improved"] is True
        assert summary["score_improvement"] == 15.0
        assert summary["component_changes"]["bug_quality"]["improved"] is True
        assert summary["component_changes"]["bug_quality"]["change"] == 3

    def test_regression(self):
        engine = HealthEngine()
        summary = engine.get_improvement_summary(
            current_score=60.0,
            baseline_score=80.0,
            current_findings={"bug_quality": 10},
            baseline_findings={"bug_quality": 2},
        )
        assert summary["improved"] is False
        assert summary["score_improvement"] == -20.0

    def test_new_component_in_current(self):
        engine = HealthEngine()
        summary = engine.get_improvement_summary(
            current_score=80.0,
            baseline_score=80.0,
            current_findings={"new_component": 3},
            baseline_findings={},
        )
        assert "new_component" in summary["component_changes"]


class TestRegisterLanguagePackMappings:
    def test_merges_rule_to_component(self):
        HealthEngine.register_language_pack_mappings(
            rule_to_component={"custom-go-rule": "bug_quality"},
        )
        assert HealthEngine.RULE_TO_COMPONENT.get("custom-go-rule") == "bug_quality"
        del HealthEngine.RULE_TO_COMPONENT["custom-go-rule"]

    def test_merges_category_inference(self):
        HealthEngine.register_language_pack_mappings(
            category_inference={"rustcheck": "lint_quality"},
        )
        assert HealthEngine.CATEGORY_INFERENCE.get("rustcheck") == "lint_quality"
        del HealthEngine.CATEGORY_INFERENCE["rustcheck"]
