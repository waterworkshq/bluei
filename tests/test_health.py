#!/usr/bin/env python3
"""Pytest-native tests for Health Engine."""

from qa_agent.models import Finding, HealthScore
from qa_agent.health import HealthEngine, HealthWeights


def make_finding(**overrides):
    data = {
        'finding_id': 'f1',
        'repo': '/tmp/test',
        'path': 'test.py',
        'line': 10,
        'rule': 'bug-rule',
        'snippet': 'bug',
        'confidence': 0.85,
        'quick_win': False,
        'safe_to_autofix': False,
    }
    data.update(overrides)
    return Finding(**data)


def test_empty_findings_perfect_score():
    engine = HealthEngine()
    score = engine.calculate([])
    assert score.score == 100.0


def test_critical_finding_reduces_score():
    engine = HealthEngine()
    score = engine.calculate([make_finding(confidence=0.95)])
    assert score.score < 100.0


def test_component_scores_calculated():
    engine = HealthEngine()
    findings = [make_finding(rule='type-any', snippet='any', confidence=0.85, safe_to_autofix=True)]
    score = engine.calculate(findings)
    assert 'code_quality' in score.components
    assert 'test_coverage' in score.components
    assert 'documentation' in score.components
    assert 'type_safety' in score.components
    assert 'performance' in score.components


def test_health_score_bands():
    assert HealthScore(score=95, components={}, calculated_at='2026-01-01').band == 'excellent'
    assert HealthScore(score=75, components={}, calculated_at='2026-01-01').band == 'good'
    assert HealthScore(score=55, components={}, calculated_at='2026-01-01').band == 'needs_work'
    assert HealthScore(score=35, components={}, calculated_at='2026-01-01').band == 'poor'
    assert HealthScore(score=15, components={}, calculated_at='2026-01-01').band == 'critical'


def test_findings_by_category():
    engine = HealthEngine()
    findings = [
        make_finding(rule='type-any', snippet='any', safe_to_autofix=True),
        make_finding(finding_id='f2', line=20, rule='test-missing', snippet='no test', confidence=0.80, safe_to_autofix=True),
    ]
    by_cat = engine.get_findings_by_category(findings)
    assert 'type_safety' in by_cat
    assert 'test_coverage' in by_cat


def test_findings_by_severity():
    engine = HealthEngine()
    findings = [
        make_finding(confidence=0.95),
        make_finding(finding_id='f2', line=20, rule='warn', snippet='warn', confidence=0.70, quick_win=True, safe_to_autofix=True),
    ]
    by_sev = engine.get_findings_by_severity(findings)
    assert by_sev['critical'] == 1
    assert by_sev['low'] == 1


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


def test_baseline_creation():
    engine = HealthEngine()
    findings = [make_finding(safe_to_autofix=True)]
    score = engine.calculate(findings)
    baseline = engine.create_baseline(
        repo_id='test-repo',
        findings=findings,
        health=score,
        findings_file='/tmp/findings.jsonl',
    )
    assert baseline.repo_id == 'test-repo'
    assert baseline.findings_total == 1
    assert baseline.health_score == score.score
