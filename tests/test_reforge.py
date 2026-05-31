#!/usr/bin/env python3
"""test_reforge.py — tests for the reforge module (refactor-class finding routing & state)."""

import sys
from pathlib import Path

# Allow running as script from core/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))
PACKAGE = Path(__file__).parent

from typing import Optional


class FakeFinding:
    """Minimal Finding-like object for testing classify_finding without the full model."""

    def __init__(
        self,
        finding_id: str = "test-001",
        rule: str = "ruff-b904",
        path: str = "src/foo.py",
        line: int = 10,
        snippet: str = "pass",
        confidence: float = 0.9,
        quick_win: bool = True,
        safe_to_autofix: bool = True,
    ):
        self.finding_id = finding_id
        self.rule = rule
        self.path = path
        self.line = line
        self.snippet = snippet
        self.confidence = confidence
        self.quick_win = quick_win
        self.safe_to_autofix = safe_to_autofix
        # Fields set by classify_finding
        self.refactor_class: Optional[str] = None
        self.refactor_phase: Optional[str] = None


def test_refactor_class_enum_values():
    """RefactorClass enum has the expected values."""
    from bluei.engine.reforge import RefactorClass

    assert RefactorClass.SIMPLE_FIX.value == "simple_fix"
    assert RefactorClass.REFACTOR_CLASS.value == "refactor_class"
    assert RefactorClass.CLAUDE_FIX.value == "claude_fix"
    print("✅ RefactorClass enum values correct")


def test_refactor_phase_enum_values():
    """RefactorPhase enum has the expected values."""
    from bluei.engine.reforge import RefactorPhase

    assert RefactorPhase.PLANNING.value == "planning"
    assert RefactorPhase.SPLITTING.value == "splitting"
    assert RefactorPhase.VALIDATING.value == "validating"
    assert RefactorPhase.DONE.value == "done"
    assert RefactorPhase.ABORTED.value == "aborted"
    print("✅ RefactorPhase enum values correct")


def test_classify_xo_max_lines():
    """xo-max-lines is classified as REFACTOR_CLASS."""
    from bluei.engine.reforge import RefactorClass, classify_finding

    f = FakeFinding(rule="xo-max-lines")
    rc = classify_finding(f)
    assert rc == RefactorClass.REFACTOR_CLASS, f"Expected REFACTOR_CLASS, got {rc}"
    print("✅ xo-max-lines → REFACTOR_CLASS")


def test_classify_xo_complexity():
    """xo-complexity is classified as REFACTOR_CLASS."""
    from bluei.engine.reforge import RefactorClass, classify_finding

    f = FakeFinding(rule="xo-complexity")
    rc = classify_finding(f)
    assert rc == RefactorClass.REFACTOR_CLASS, f"Expected REFACTOR_CLASS, got {rc}"
    print("✅ xo-complexity → REFACTOR_CLASS")


def test_classify_ruff_safe():
    """ruff-c408 with safe_to_autofix=True and deterministic default → SIMPLE_FIX."""
    from bluei.engine.reforge import RefactorClass, classify_finding

    # ruff-c408 has context rules with default_strategy="deterministic"
    # and path "src/foo.ts" doesn't match any migration/test pattern
    f = FakeFinding(rule="ruff-c408", safe_to_autofix=True, path="src/foo.ts")
    rc = classify_finding(f)
    assert rc == RefactorClass.SIMPLE_FIX, f"Expected SIMPLE_FIX, got {rc}"
    print("✅ ruff-c408 (safe, no context match) → SIMPLE_FIX")


def test_classify_ruff_unsafe():
    """ruff-* rule with safe_to_autofix=False but a loaded recipe routes to SIMPLE_FIX."""
    from bluei.engine.reforge import RefactorClass, classify_finding

    # ruff-f401 has a loaded recipe, so recipe-aware routing sends it to SIMPLE_FIX
    # even though safe_to_autofix=False (recipe engine takes precedence)
    f = FakeFinding(rule="ruff-f401", safe_to_autofix=False)
    rc = classify_finding(f)
    assert rc == RefactorClass.SIMPLE_FIX, (
        f"Expected SIMPLE_FIX (recipe match), got {rc}"
    )


def test_classify_type_explicit_any():
    """type-explicit-any is CONTEXTUAL_FIX (context rule default=llm_with_context)."""
    from bluei.engine.reforge import RefactorClass, classify_finding

    f = FakeFinding(rule="type-explicit-any")
    rc = classify_finding(f)
    assert rc == RefactorClass.CONTEXTUAL_FIX, f"Expected CONTEXTUAL_FIX, got {rc}"
    print("✅ type-explicit-any → CONTEXTUAL_FIX")


def test_classify_test_coverage_rules():
    """test-coverage-branch and test-coverage-function are CLAUDE_FIX."""
    from bluei.engine.reforge import RefactorClass, classify_finding

    for rule in ("test-coverage-branch", "test-coverage-function"):
        f = FakeFinding(rule=rule)
        rc = classify_finding(f)
        assert rc == RefactorClass.CLAUDE_FIX, (
            f"Expected CLAUDE_FIX for {rule}, got {rc}"
        )
    print("✅ test-coverage-* → CLAUDE_FIX")


def test_classify_unknown_rule():
    """Unknown rules default to CASCADE_FIX."""
    from bluei.engine.reforge import RefactorClass, classify_finding

    f = FakeFinding(rule="some-unknown-rule")
    rc = classify_finding(f)
    assert rc == RefactorClass.CASCADE_FIX, f"Expected CASCADE_FIX, got {rc}"


def test_refactor_work_state_machine():
    """RefactorWork state machine transitions correctly."""
    from bluei.engine.reforge import RefactorWork, RefactorPhase

    w = RefactorWork(finding_id="test-001")
    assert w.phase == RefactorPhase.PLANNING
    assert not w.needs_human_review

    w.mark_splitting(targets=["part1.ts", "part2.ts"], original_line_count=3000)
    assert w.phase == RefactorPhase.SPLITTING
    assert w.planned_targets == ["part1.ts", "part2.ts"]
    assert w.original_line_count == 3000
    assert "part1.ts" in w.written_files or len(w.written_files) == 0

    w.mark_validating(baseline_fingerprint="abc123")
    assert w.phase == RefactorPhase.VALIDATING
    assert w.baseline_fingerprint == "abc123"

    w.mark_done()
    assert w.phase == RefactorPhase.DONE

    w2 = RefactorWork(finding_id="test-002")
    w2.mark_aborted("file too large")
    assert w2.phase == RefactorPhase.ABORTED
    assert w2.needs_human_review is True
    assert w2.review_outcome == "file too large"

    print("✅ RefactorWork state machine transitions correct")


def test_can_auto_refactor_within_limits():
    """can_auto_refactor returns (True, '') for files within limits."""
    from bluei.engine.reforge import can_auto_refactor, RefactorClass, classify_finding

    f = FakeFinding(rule="xo-max-lines", path="test/small.ts")

    # No worktree path → can't check line count → allowed (conservative)
    allowed, reason = can_auto_refactor(f, worktree_path=None)
    assert allowed is True, f"Expected True with no worktree, got {allowed}"

    print("✅ can_auto_refactor within limits passes")


def test_can_auto_refactor_large_file_trigger():
    """can_auto_refactor returns (False, reason) when LARGE_FILE_SAFETY_LIMIT exceeded."""
    from bluei.engine.reforge import (
        can_auto_refactor,
        LARGE_FILE_SAFETY_LIMIT,
    )

    # Create a temp file that's over LARGE_FILE_SAFETY_LIMIT lines
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        big_file = Path(tmpdir) / "big.ts"
        # Write a file with LARGE_FILE_SAFETY_LIMIT + 100 lines
        big_file.write_text(
            "\n".join([f"// line {i}" for i in range(LARGE_FILE_SAFETY_LIMIT + 100)]),
            encoding="utf-8",
        )

        class BigFinding:
            finding_id = "big-001"
            rule = "xo-max-lines"
            path = str(big_file.relative_to(tmpdir))
            line = 1
            snippet = ""
            confidence = 0.9
            quick_win = True
            safe_to_autofix = True

        allowed, reason = can_auto_refactor(BigFinding(), worktree_path=Path(tmpdir))
        assert allowed is False, f"Expected False for large file, got {allowed}"
        assert "LARGE_FILE_SAFETY_LIMIT" in reason
        print("✅ can_auto_refactor triggers safety gate for large files")


def test_describe_class():
    """describe_class returns a non-empty string for each class."""
    from bluei.engine.reforge import RefactorClass, describe_class

    for rc in RefactorClass:
        desc = describe_class(rc)
        assert isinstance(desc, str), f"describe_class({rc}) should return str"
        assert len(desc) > 10, f"describe_class({rc}) too short: {desc!r}"
    print("✅ describe_class returns meaningful strings")


def test_is_large_refactor_split_ratio():
    """is_large_refactor returns True when split ratio > 6 parts."""
    from bluei.engine.reforge import is_large_refactor, LARGE_FILE_SAFETY_LIMIT

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a file that would need ~8 parts at MAX_LINES_REFACTOR_TARGET
        from bluei.engine.constants import MAX_LINES_REFACTOR_TARGET

        huge_file = Path(tmpdir) / "huge.ts"
        needed_lines = (MAX_LINES_REFACTOR_TARGET + 1) * 8
        huge_file.write_text(
            "\n".join([f"// line {i}" for i in range(needed_lines)]), encoding="utf-8"
        )

        class HugeFinding:
            finding_id = "huge-001"
            rule = "xo-max-lines"
            path = str(huge_file.relative_to(tmpdir))
            line = 1
            snippet = ""
            confidence = 0.9
            quick_win = True
            safe_to_autofix = True

        result = is_large_refactor(HugeFinding(), worktree_path=Path(tmpdir))
        # split ratio 8 > 6 → should be True
        assert result is True, f"Expected True for 8-part split, got {result}"
        print(f"✅ is_large_refactor True when split ratio > 6 parts")


def test_large_file_safety_limit_value():
    """LARGE_FILE_SAFETY_LIMIT is a reasonable value (5000)."""
    from bluei.engine.reforge import LARGE_FILE_SAFETY_LIMIT

    assert LARGE_FILE_SAFETY_LIMIT == 5000, (
        f"LARGE_FILE_SAFETY_LIMIT = {LARGE_FILE_SAFETY_LIMIT}, expected 5000"
    )
    print(f"✅ LARGE_FILE_SAFETY_LIMIT = {LARGE_FILE_SAFETY_LIMIT}")


def test_refactor_work_serialization():
    """RefactorWork fields are of the expected types (set is intentional for written_files)."""
    from bluei.engine.reforge import RefactorWork, RefactorPhase
    import json

    w = RefactorWork(
        finding_id="test-001",
        phase=RefactorPhase.SPLITTING,
        planned_targets=["part1.ts", "part2.ts"],
        original_line_count=3000,
        written_files={"part1.ts"},
        baseline_fingerprint="",
        needs_human_review=False,
        review_outcome=None,
    )

    # Check that the non-set fields serialize cleanly
    d = {
        "finding_id": w.finding_id,
        "phase": w.phase.value,
        "planned_targets": w.planned_targets,
        "original_line_count": w.original_line_count,
        "baseline_fingerprint": w.baseline_fingerprint,
        "needs_human_review": w.needs_human_review,
        "review_outcome": w.review_outcome,
    }
    json.dumps(d)  # must not raise
    # written_files is a set (correct) — that's fine internally
    assert isinstance(w.written_files, set)
    print("✅ RefactorWork core fields are JSON-serializable")


def test_classify_finding_annotates_finding():
    """classify_finding returns the RefactorClass and sets finding.refactor_class."""
    from bluei.engine.reforge import RefactorClass, classify_finding

    f = FakeFinding(rule="xo-complexity")
    rc = classify_finding(f)
    assert f.refactor_class == rc.value, (
        f"finding.refactor_class not set; got {f.refactor_class}"
    )
    print("✅ classify_finding annotates finding.refactor_class")


def main():
    tests = [
        test_refactor_class_enum_values,
        test_refactor_phase_enum_values,
        test_classify_xo_max_lines,
        test_classify_xo_complexity,
        test_classify_ruff_safe,
        test_classify_ruff_unsafe,
        test_classify_type_explicit_any,
        test_classify_test_coverage_rules,
        test_classify_unknown_rule,
        test_refactor_work_state_machine,
        test_can_auto_refactor_within_limits,
        test_can_auto_refactor_large_file_trigger,
        test_describe_class,
        test_is_large_refactor_split_ratio,
        test_large_file_safety_limit_value,
        test_refactor_work_serialization,
        test_classify_finding_annotates_finding,
    ]

    failed = []
    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"❌ {test.__name__}: {e}")
            failed.append(test.__name__)

    print()
    if failed:
        print(f"❌ {len(failed)} test(s) failed: {failed}")
        sys.exit(1)
    else:
        print(f"✅ All {len(tests)} tests passed")


if __name__ == "__main__":
    main()


# ── Merged from test_reforge_remaining.py ──

import pytest as _pytest
from unittest.mock import patch as _patch, MagicMock as _MagicMock

from bluei.engine.models import Finding as _Finding
from bluei.engine.reforge import (
    classify_finding as _classify_finding,
    is_large_refactor as _is_large_refactor,
    can_auto_refactor as _can_auto_refactor,
    RefactorClass as _RefactorClass,
    LARGE_FILE_SAFETY_LIMIT,
)
from bluei.engine.constants import (
    MAX_LINES_REFACTOR_LIMIT,
    MAX_LINES_REFACTOR_TARGET,
)


def _engine_finding(
    rule="some-rule",
    path="src/foo.py",
    line=10,
    snippet="pass",
    confidence=0.9,
    quick_win=True,
    safe_to_autofix=True,
):
    return _Finding(
        finding_id="test-001",
        repo="test-repo",
        path=path,
        line=line,
        rule=rule,
        snippet=snippet,
        confidence=confidence,
        quick_win=quick_win,
        safe_to_autofix=safe_to_autofix,
    )


def test_classify_finding_recipe_routes_to_simple_fix():
    mock_engine = _MagicMock()
    mock_engine.has_recipe.return_value = True
    with _patch("bluei.engine.lifecycle._recipe_engine", mock_engine):
        f = _engine_finding(rule="custom-recipe-match-rule")
        rc = _classify_finding(f)
        assert rc is _RefactorClass.SIMPLE_FIX
        assert f.refactor_class == _RefactorClass.SIMPLE_FIX.value
        mock_engine.has_recipe.assert_called_once_with("custom-recipe-match-rule")


def test_classify_finding_recipe_check_no_match():
    mock_engine = _MagicMock()
    mock_engine.has_recipe.return_value = False
    with _patch("bluei.engine.lifecycle._recipe_engine", mock_engine):
        f = _engine_finding(rule="custom-no-recipe-rule")
        rc = _classify_finding(f)
        assert rc is _RefactorClass.CASCADE_FIX
        assert f.refactor_class == _RefactorClass.CASCADE_FIX.value


def test_is_large_refactor_read_exception_during_size_check(tmp_path):
    bad_file = tmp_path / "unreadable.py"
    bad_file.write_bytes(b"\x80\xff\xfe" * 100)
    f = _engine_finding(rule="xo-complexity", path="unreadable.py")
    result = _is_large_refactor(f, worktree_path=tmp_path)
    assert result is False


def test_is_large_refactor_read_exception_during_split_ratio(tmp_path):
    bad_file = tmp_path / "bad_split.ts"
    bad_file.write_bytes(b"\x80\xff\xfe" * 100)
    f = _engine_finding(rule="xo-max-lines", path="bad_split.ts")
    result = _is_large_refactor(f, worktree_path=tmp_path)
    assert result is False


def test_is_large_refactor_normal_file_below_all_limits(tmp_path):
    small_file = tmp_path / "small.ts"
    small_file.write_text("\n".join(["// line"] * 100), encoding="utf-8")
    f = _engine_finding(rule="xo-max-lines", path="small.ts")
    result = _is_large_refactor(f, worktree_path=tmp_path)
    assert result is False


def test_is_large_refactor_no_worktree():
    f = _engine_finding(rule="xo-max-lines", path="src/big.ts")
    result = _is_large_refactor(f, worktree_path=None)
    assert result is False


def test_is_large_refactor_file_does_not_exist(tmp_path):
    f = _engine_finding(rule="xo-max-lines", path="nonexistent.ts")
    result = _is_large_refactor(f, worktree_path=tmp_path)
    assert result is False


def test_can_auto_refactor_xo_max_lines_exceeds_limit(tmp_path):
    line_count = MAX_LINES_REFACTOR_LIMIT + 500
    target_file = tmp_path / "oversize.ts"
    target_file.write_text(
        "\n".join([f"// line {i}" for i in range(line_count)]), encoding="utf-8"
    )
    f = _engine_finding(rule="xo-max-lines", path="oversize.ts")
    allowed, reason = _can_auto_refactor(f, worktree_path=tmp_path)
    assert allowed is False
    assert "MAX_LINES_REFACTOR_LIMIT" in reason
    assert str(line_count) in reason
    assert "oversize.ts" in reason


def test_can_auto_refactor_xo_max_lines_within_limit(tmp_path):
    line_count = MAX_LINES_REFACTOR_LIMIT - 100
    target_file = tmp_path / "within.ts"
    target_file.write_text(
        "\n".join([f"// line {i}" for i in range(line_count)]), encoding="utf-8"
    )
    f = _engine_finding(rule="xo-max-lines", path="within.ts")
    allowed, reason = _can_auto_refactor(f, worktree_path=tmp_path)
    assert allowed is True
    assert reason == ""


def test_can_auto_refactor_read_exception_during_limit_check(tmp_path):
    bad_file = tmp_path / "corrupt.ts"
    bad_file.write_bytes(b"\x80\xff\xfe" * 100)
    f = _engine_finding(rule="xo-max-lines", path="corrupt.ts")
    allowed, reason = _can_auto_refactor(f, worktree_path=tmp_path)
    assert allowed is True
    assert reason == ""


def test_can_auto_refactor_non_xo_rule(tmp_path):
    small_file = tmp_path / "regular.py"
    small_file.write_text("x = 1\n", encoding="utf-8")
    f = _engine_finding(rule="ruff-b904", path="regular.py")
    allowed, reason = _can_auto_refactor(f, worktree_path=tmp_path)
    assert allowed is True
    assert reason == ""


def test_can_auto_refactor_no_worktree():
    f = _engine_finding(rule="xo-max-lines", path="src/any.ts")
    allowed, reason = _can_auto_refactor(f, worktree_path=None)
    assert allowed is True
    assert reason == ""
