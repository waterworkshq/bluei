from bluei.app.health import HealthEngine
from bluei.app.emergent_rules import EmergentRule, EmergentRuleStatus, DetectionPattern
from bluei.engine.models import Finding


def _finding(rule: str, confidence: float = 0.85, **kw) -> Finding:
    defaults = {
        "finding_id": "f-1",
        "repo": "demo",
        "path": "src/app.py",
        "line": 10,
        "snippet": "test",
        "quick_win": False,
        "safe_to_autofix": False,
    }
    defaults.update(kw)
    defaults["rule"] = rule
    defaults["confidence"] = confidence
    return Finding(**defaults)


def _active_rule(
    rule_id: str,
    *,
    weight_multiplier: float = 0.5,
    component_override: str | None = None,
) -> EmergentRule:
    return EmergentRule(
        rule_id=rule_id,
        header=f"Test rule {rule_id}",
        detection_pattern=DetectionPattern(search_pattern=rule_id),
        language="python",
        category="lint",
        status=EmergentRuleStatus.ACTIVE,
        weight_multiplier=weight_multiplier,
        component_override=component_override,
    )


def test_emergent_finding_reduces_penalty(tmp_path):
    emergent = _active_rule("er-test", weight_multiplier=0.5)
    engine = HealthEngine(emergent_rules=[emergent])
    f = _finding(rule="emergent:er-test", confidence=0.90)
    score_with = engine.calc_component_score("bug_quality", [f])
    engine_no_em = HealthEngine(emergent_rules=[])
    score_without = engine_no_em.calc_component_score("bug_quality", [f])
    assert score_with > score_without


def test_non_emergent_finding_unchanged():
    engine = HealthEngine(emergent_rules=[_active_rule("er-test")])
    f = _finding(rule="broad-except", confidence=0.90)
    score = engine.calc_component_score("lint_quality", [f])
    engine_no = HealthEngine(emergent_rules=[])
    score_no = engine_no.calc_component_score("lint_quality", [f])
    assert score == score_no


def test_emergent_component_override():
    emergent = _active_rule("er-override", component_override="type_safety")
    engine = HealthEngine(emergent_rules=[emergent])
    f = _finding(rule="emergent:er-override", confidence=0.90)
    component = engine._get_component(f)
    assert component == "type_safety"


def test_emergent_default_component_is_bug_quality():
    emergent = _active_rule("er-no-override")
    engine = HealthEngine(emergent_rules=[emergent])
    f = _finding(rule="emergent:er-no-override", confidence=0.90)
    component = engine._get_component(f)
    assert component == "bug_quality"


def test_weight_multiplier_zero_removes_penalty():
    emergent = _active_rule("er-zero", weight_multiplier=0.0)
    engine = HealthEngine(emergent_rules=[emergent])
    f = _finding(rule="emergent:er-zero", confidence=0.90)
    score = engine.calc_component_score("bug_quality", [f])
    assert score == 100.0


def test_weight_multiplier_one_keeps_penalty():
    emergent = _active_rule("er-full", weight_multiplier=1.0)
    engine = HealthEngine(emergent_rules=[emergent])
    f = _finding(rule="emergent:er-full", confidence=0.90)
    score = engine.calc_component_score("bug_quality", [f])
    engine_no = HealthEngine(emergent_rules=[])
    score_no = engine_no.calc_component_score("bug_quality", [f])
    assert score == score_no
