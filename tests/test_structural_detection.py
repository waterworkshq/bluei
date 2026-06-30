"""Slice 1 (Pillar 4) — AST STRUCTURAL detection for emergent rules.

Covers AC-P4-1..6:
- extract_detection_ast (AC-P4-1): rename-invariant blob; non-Python/unparseable → None.
- scan_shadow_rules STRUCTURAL branch (AC-P4-2): exact hash + fuzzy fallback.
- proposer AST-first wiring (AC-P4-3): STRUCTURAL + ast_pattern for Python.
- Python-only (AC-P4-4): non-Python STRUCTURAL → 0 matches, no crash.
- measure_false_positives STRUCTURAL (AC-P4-5).
- Regex regression (AC-P4-6): existing alpha.4 regex detection unchanged.
"""

import json
from pathlib import Path

from bluei.app.emergent_rules import (
    DetectionPattern,
    DetectionType,
    EmergentRule,
    EmergentRuleStatus,
    extract_detection_ast,
    extract_detection_regex,
    measure_false_positives,
    propose_rules_from_findings,
    scan_shadow_rules,
)
from bluei.engine.models import Finding


def _structural_rule(
    snippet: str,
    *,
    language: str = "python",
    rule_id: str = "er-struct",
    file_glob: str = "**/*.py",
    negative_examples=None,
    status: EmergentRuleStatus = EmergentRuleStatus.TENTATIVE,
    category: str = "lint",
) -> EmergentRule:
    blob = extract_detection_ast(snippet, language)
    assert blob is not None, f"fixture snippet must produce an ast blob: {snippet!r}"
    return EmergentRule(
        rule_id=rule_id,
        header="structural fixture",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.STRUCTURAL,
            search_pattern="",
            file_glob=file_glob,
            ast_pattern=blob,
        ),
        language=language,
        category=category,
        status=status,
        negative_examples=negative_examples or [],
    )


# ---------------------------------------------------------------------------
# AC-P4-1 — extract_detection_ast
# ---------------------------------------------------------------------------


def test_extract_detection_ast_returns_blob_with_expected_keys() -> None:
    blob = extract_detection_ast('raise ValueError("x")', "python")
    assert blob is not None
    payload = json.loads(blob)
    assert set(payload.keys()) == {
        "hash",
        "nodes",
        "operators",
        "canonical_snippet",
        "language",
    }
    assert payload["language"] == "python"
    assert payload["canonical_snippet"] == 'raise ValueError("x")'
    assert isinstance(payload["hash"], str) and len(payload["hash"]) >= 8
    assert isinstance(payload["nodes"], list)
    assert isinstance(payload["operators"], list)


def test_extract_detection_ast_is_rename_invariant() -> None:
    """Identifiers and literals normalize independently: renaming an identifier
    or changing a literal's value preserves the hash (same node-type shape)."""
    a = extract_detection_ast('raise ValueError("x")', "python")
    b = extract_detection_ast('raise CustomError("msg")', "python")
    assert a and b
    assert json.loads(a)["hash"] == json.loads(b)["hash"]


def test_extract_detection_ast_distinguishes_node_types() -> None:
    raise_blob = extract_detection_ast('raise ValueError("x")', "python")
    return_blob = extract_detection_ast('return ValueError("x")', "python")
    assert raise_blob and return_blob
    assert json.loads(raise_blob)["hash"] != json.loads(return_blob)["hash"]


def test_extract_detection_ast_none_for_non_python() -> None:
    assert extract_detection_ast('console.log("x")', "typescript") is None
    assert extract_detection_ast('console.log("x")', "go") is None


def test_extract_detection_ast_none_for_unparseable_or_empty() -> None:
    assert extract_detection_ast("def (", "python") is None
    assert extract_detection_ast("", "python") is None
    assert extract_detection_ast("   \n  ", "python") is None


# ---------------------------------------------------------------------------
# AC-P4-2 + AC-P4-4 — scan_shadow_rules STRUCTURAL branch
# ---------------------------------------------------------------------------


def test_scan_shadow_rules_structural_exact_hash_match(tmp_path) -> None:
    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    target = worktree / "src" / "api.py"
    target.write_text(
        'def good():\n    return 1\n\ndef bad():\n    raise ValueError("oops")\n',
        encoding="utf-8",
    )
    rule = _structural_rule('raise ValueError("oops")', file_glob="src/**/*.py")

    matches = scan_shadow_rules([rule], worktree)

    assert len(matches) == 1
    m = matches[0]
    assert m.rule_id == "er-struct"
    assert m.path == "src/api.py"
    assert m.line == 5
    assert "raise ValueError" in m.snippet
    assert m.status == EmergentRuleStatus.TENTATIVE


def test_scan_shadow_rules_structural_rename_variant_matches(tmp_path) -> None:
    """AC-P4-2 hash match: a renamed-identifier/literal variant still matches
    (same node-type shape — Constant stays Constant, Name stays Name)."""
    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "api.py").write_text(
        'def bad():\n    raise CustomError("nope")\n', encoding="utf-8"
    )
    rule = _structural_rule('raise ValueError("x")', file_glob="src/**/*.py")

    matches = scan_shadow_rules([rule], worktree)
    assert len(matches) == 1
    assert "raise CustomError" in matches[0].snippet


def test_scan_shadow_rules_structural_no_match_when_different(tmp_path) -> None:
    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "api.py").write_text(
        "def good():\n    return 'all clear'\n", encoding="utf-8"
    )
    rule = _structural_rule('raise ValueError("x")', file_glob="src/**/*.py")

    assert scan_shadow_rules([rule], worktree) == []


def test_scan_shadow_rules_structural_python_only_no_crash(tmp_path) -> None:
    """AC-P4-4: a non-Python STRUCTURAL rule scans no files and never raises."""
    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "app.ts").write_text('console.log("x");\n', encoding="utf-8")
    # Build the rule manually — _structural_rule asserts Python; use raw shape.
    rule = EmergentRule(
        rule_id="er-ts-struct",
        header="TS structural",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.STRUCTURAL,
            search_pattern="",
            file_glob="src/**/*.ts",
            ast_pattern=json.dumps(
                {
                    "hash": "deadbeefdeadbeef",
                    "nodes": ["expr"],
                    "operators": [],
                    "canonical_snippet": 'console.log("x")',
                    "language": "typescript",
                }
            ),
        ),
        language="typescript",
        category="lint",
        status=EmergentRuleStatus.TENTATIVE,
    )

    assert scan_shadow_rules([rule], worktree) == []


def test_scan_shadow_rules_structural_malformed_blob_skipped(tmp_path) -> None:
    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "api.py").write_text(
        'raise ValueError("x")\n', encoding="utf-8"
    )
    rule = EmergentRule(
        rule_id="er-malformed",
        header="malformed",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.STRUCTURAL,
            search_pattern="",
            file_glob="src/**/*.py",
            ast_pattern="not-json{",
        ),
        language="python",
        category="lint",
        status=EmergentRuleStatus.TENTATIVE,
    )

    assert scan_shadow_rules([rule], worktree) == []


# ---------------------------------------------------------------------------
# AC-P4-5 — measure_false_positives STRUCTURAL
# ---------------------------------------------------------------------------


def test_measure_false_positives_structural_counts_hash_matches() -> None:
    rule = _structural_rule(
        'raise ValueError("x")',
        negative_examples=[
            'raise SomeError("msg")',  # same shape (Name+Constant) → hash match
            'return "ok"',  # different → not matched
            'raise ValueError("y")',  # exact shape → hash match
        ],
    )
    assert measure_false_positives(rule) == 2


def test_measure_false_positives_structural_handles_malformed() -> None:
    rule = EmergentRule(
        rule_id="er-bad",
        header="bad",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.STRUCTURAL,
            ast_pattern="{nope",
        ),
        language="python",
        category="lint",
        negative_examples=["raise ValueError('x')"],
    )
    assert measure_false_positives(rule) == 0


def test_measure_false_positives_structural_python_only_no_crash() -> None:
    """AC-P4-4: a non-Python STRUCTURAL rule never raises (text-hasher fallback).
    Uses a negative example that does NOT text-match the canonical so the count
    is 0 — keeps the assertion meaningful without coupling to text-hasher quirks.
    """
    rule = EmergentRule(
        rule_id="er-ts",
        header="ts",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.STRUCTURAL,
            ast_pattern=json.dumps(
                {
                    "hash": "deadbeefdeadbeef",
                    "nodes": [],
                    "operators": [],
                    "canonical_snippet": 'console.log("unique-canonical")',
                    "language": "typescript",
                }
            ),
        ),
        language="typescript",
        category="lint",
        negative_examples=["definitely_different_code()"],
    )
    # No exception; count is 0 because the example doesn't match the canonical.
    assert measure_false_positives(rule) == 0


# ---------------------------------------------------------------------------
# AC-P4-3 — proposer AST-first wiring
# ---------------------------------------------------------------------------


def test_propose_rules_from_findings_python_gets_structural() -> None:
    findings = [
        Finding(
            finding_id=f"f-{i}",
            repo="r",
            path="src/api.py",
            line=1,
            rule="py-raise-foo",
            snippet='raise ValueError("oops")',
            confidence=0.9,
            quick_win=False,
            safe_to_autofix=False,
        )
        for i in range(6)
    ]
    proposals = propose_rules_from_findings(
        findings, run_id="run-1", min_observations=5
    )
    assert len(proposals) == 1
    rule = proposals[0]
    assert rule.detection_pattern.detection_type == DetectionType.STRUCTURAL
    assert rule.detection_pattern.search_pattern == ""
    assert rule.detection_pattern.ast_pattern is not None
    blob = json.loads(rule.detection_pattern.ast_pattern)
    assert blob["language"] == "python"
    assert "hash" in blob


def test_propose_rules_from_findings_non_python_falls_through_to_regex_or_text() -> (
    None
):
    # TS snippet: no AST path; regex detection kicks in (or text fallback).
    findings = [
        Finding(
            finding_id=f"f-{i}",
            repo="r",
            path="src/app.ts",
            line=1,
            rule="ts-console",
            snippet='console.log("x")',
            confidence=0.9,
            quick_win=False,
            safe_to_autofix=False,
        )
        for i in range(6)
    ]
    proposals = propose_rules_from_findings(
        findings, run_id="run-1", min_observations=5
    )
    assert len(proposals) == 1
    rule = proposals[0]
    assert rule.detection_pattern.detection_type in (
        DetectionType.REGEX_PATTERN,
        DetectionType.TEXT_PATTERN,
    )
    assert rule.detection_pattern.ast_pattern is None


# ---------------------------------------------------------------------------
# AC-P4-6 — regex regression (alpha.4 behavior unchanged)
# ---------------------------------------------------------------------------


def test_regex_detection_still_works_alongside_structural(tmp_path) -> None:
    """A REGEX rule still matches via compiled.search; STRUCTURAL doesn't interfere."""
    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "api.py").write_text(
        "foo = 1\nbar = 2\nbaz = 3\n", encoding="utf-8"
    )
    regex_rule = EmergentRule(
        rule_id="er-regex",
        header="regex",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.REGEX_PATTERN,
            search_pattern=r"(\w+)\s*=\s*\d+",
            file_glob="src/**/*.py",
        ),
        language="python",
        category="lint",
        status=EmergentRuleStatus.TENTATIVE,
    )
    matches = scan_shadow_rules([regex_rule], worktree)
    assert len(matches) == 3
    assert all(m.rule_id == "er-regex" for m in matches)


def test_extract_detection_regex_unaffected_by_structural_addition() -> None:
    # AC-P4-6: the alpha.4 regex helper signature/behavior is unchanged.
    pattern = extract_detection_regex('raise ValueError("x")', "python")
    assert pattern  # non-empty
    assert r"(\w+)" in pattern
