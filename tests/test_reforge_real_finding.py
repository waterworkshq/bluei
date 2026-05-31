import pytest

from bluei.engine.models import Finding
from bluei.engine.reforge import (
    RefactorClass,
    RefactorPhase,
    classify_finding,
    describe_class,
    get_context_rule,
)


def _finding(
    rule="ruff-e501",
    path="src/foo.py",
    repo="demo",
    safe_to_autofix=True,
    confidence=0.9,
    quick_win=True,
) -> Finding:
    return Finding(
        finding_id="test-finding-001",
        repo=repo,
        path=path,
        line=10,
        rule=rule,
        snippet="pass",
        confidence=confidence,
        quick_win=quick_win,
        safe_to_autofix=safe_to_autofix,
    )


def test_classify_xo_max_lines_returns_refactor_class():
    f = _finding(rule="xo-max-lines")
    rc = classify_finding(f)
    assert rc == RefactorClass.REFACTOR_CLASS


def test_classify_xo_complexity_returns_refactor_class():
    f = _finding(rule="xo-complexity")
    rc = classify_finding(f)
    assert rc == RefactorClass.REFACTOR_CLASS


def test_classify_ruff_safe_autofix_returns_simple_fix():
    f = _finding(rule="ruff-C408", safe_to_autofix=True)
    rc = classify_finding(f)
    assert rc == RefactorClass.SIMPLE_FIX


def test_classify_ruff_unsafe_autofix_returns_simple_fix_via_recipe():
    f = _finding(rule="ruff-XYZ999", safe_to_autofix=False)
    rc = classify_finding(f)
    assert rc == RefactorClass.SIMPLE_FIX


def test_classify_test_coverage_branch_returns_claude_fix():
    f = _finding(rule="test-coverage-branch")
    rc = classify_finding(f)
    assert rc == RefactorClass.CLAUDE_FIX


def test_classify_test_coverage_function_returns_claude_fix():
    f = _finding(rule="test-coverage-function")
    rc = classify_finding(f)
    assert rc == RefactorClass.CLAUDE_FIX


def test_classify_type_explicit_any_returns_contextual_fix():
    f = _finding(rule="type-explicit-any")
    rc = classify_finding(f)
    assert rc == RefactorClass.CONTEXTUAL_FIX


def test_classify_unknown_rule_returns_cascade_fix():
    f = _finding(rule="totally-unknown-rule-abc")
    rc = classify_finding(f)
    assert rc == RefactorClass.CASCADE_FIX


def test_safe_to_autofix_true_ruff_rule():
    f = _finding(rule="ruff-F401", safe_to_autofix=True)
    rc = classify_finding(f)
    assert rc == RefactorClass.SIMPLE_FIX


def test_safe_to_autofix_false_ruff_rule():
    f = _finding(rule="ruff-F401", safe_to_autofix=False)
    rc = classify_finding(f)
    assert rc == RefactorClass.SIMPLE_FIX


def test_classify_annotates_refactor_class_on_finding():
    f = _finding(rule="xo-max-lines")
    assert f.refactor_class is None
    classify_finding(f)
    assert f.refactor_class == RefactorClass.REFACTOR_CLASS.value


def test_classify_annotates_simple_fix():
    f = _finding(rule="ruff-C408", safe_to_autofix=True)
    classify_finding(f)
    assert f.refactor_class == RefactorClass.SIMPLE_FIX.value


def test_classify_annotates_claude_fix():
    f = _finding(rule="test-coverage-branch")
    classify_finding(f)
    assert f.refactor_class == RefactorClass.CLAUDE_FIX.value


def test_classify_annotates_cascade_fix():
    f = _finding(rule="unknown-rule-xyz")
    classify_finding(f)
    assert f.refactor_class == RefactorClass.CASCADE_FIX.value


def test_classify_annotates_contextual_fix():
    f = _finding(rule="type-explicit-any")
    classify_finding(f)
    assert f.refactor_class == RefactorClass.CONTEXTUAL_FIX.value


def test_context_rule_override_deterministic_safe():
    rule = "ruff-C408"
    ctx = get_context_rule(rule)
    if ctx is None:
        pytest.skip("no context rule for ruff-C408")
    f = _finding(
        rule=rule, path="src/does-not-match-any-pattern.ts", safe_to_autofix=True
    )
    rc = classify_finding(f)
    assert rc == RefactorClass.SIMPLE_FIX


def test_context_rule_missing_defaults_cascade():
    f = _finding(rule="no-context-rule-zzz", safe_to_autofix=False)
    rc = classify_finding(f)
    assert rc == RefactorClass.CASCADE_FIX


def test_finding_as_dict_includes_refactor_class_after_classify():
    f = _finding(rule="xo-complexity")
    classify_finding(f)
    d = f.as_dict()
    assert d["refactor_class"] == RefactorClass.REFACTOR_CLASS.value


def test_finding_from_dict_round_trip():
    f = _finding(rule="ruff-E501")
    classify_finding(f)
    d = f.as_dict()
    f2 = Finding.from_dict(d)
    assert f2.refactor_class == f.refactor_class
    assert f2.rule == f.rule
    assert f2.finding_id == f.finding_id


def test_refactor_class_enum_all_values():
    values = {e.value for e in RefactorClass}
    assert "simple_fix" in values
    assert "refactor_class" in values
    assert "claude_fix" in values
    assert "cascade_fix" in values
    assert "contextual_fix" in values


def test_describe_class_returns_nonempty_strings():
    for rc in RefactorClass:
        desc = describe_class(rc)
        assert isinstance(desc, str)
        assert len(desc) > 10


def test_classify_preserves_other_finding_fields():
    f = _finding(rule="xo-max-lines")
    original_id = f.finding_id
    original_repo = f.repo
    original_path = f.path
    original_line = f.line
    classify_finding(f)
    assert f.finding_id == original_id
    assert f.repo == original_repo
    assert f.path == original_path
    assert f.line == original_line


def test_classify_multiple_rules_produce_expected_classes():
    cases = [
        ("xo-max-lines", RefactorClass.REFACTOR_CLASS),
        ("xo-complexity", RefactorClass.REFACTOR_CLASS),
        ("test-coverage-branch", RefactorClass.CLAUDE_FIX),
        ("test-coverage-function", RefactorClass.CLAUDE_FIX),
        ("test-gap-missing-case", RefactorClass.CLAUDE_FIX),
        ("type-explicit-any", RefactorClass.CONTEXTUAL_FIX),
    ]
    for rule, expected in cases:
        f = _finding(rule=rule)
        rc = classify_finding(f)
        assert rc == expected, f"{rule} → {rc}, expected {expected}"


def test_real_finding_has_required_dataclass_fields():
    f = _finding()
    assert hasattr(f, "finding_id")
    assert hasattr(f, "repo")
    assert hasattr(f, "path")
    assert hasattr(f, "line")
    assert hasattr(f, "rule")
    assert hasattr(f, "snippet")
    assert hasattr(f, "confidence")
    assert hasattr(f, "quick_win")
    assert hasattr(f, "safe_to_autofix")
    assert hasattr(f, "fix_attempts")
    assert hasattr(f, "last_fix_error")
    assert hasattr(f, "refactor_class")
    assert hasattr(f, "refactor_phase")
