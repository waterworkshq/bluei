"""Tests for fix_tiers.py — tiered validation for deterministic fixes."""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bluei.engine.fix_tiers import (
    CRITICAL_FILE_PATTERNS,
    SAFETY_TO_TIER,
    TIER_OVERRIDES,
    FixTier,
    TieredValidationResult,
    TieredValidator,
    _auto_diff_review,
    _parse_tier_string,
    check_tier_escalation,
    resolve_tier,
)


class FakeFinding:
    def __init__(self, rule="unknown-rule", path="test.py"):
        self.rule = rule
        self.path = path
        self.finding_id = "test-finding-001"


class FakeRecipe:
    def __init__(self, safety="needs_validation"):
        self.safety = safety


class TestFixTier:
    def test_tier_ordering(self):
        assert FixTier.T0_GUARANTEED < FixTier.T1_IDEMPOTENT
        assert FixTier.T1_IDEMPOTENT < FixTier.T2_VALIDATED
        assert FixTier.T2_VALIDATED < FixTier.T3_CONTEXTUAL
        assert FixTier.T3_CONTEXTUAL < FixTier.T4_STRUCTURAL

    def test_tier_values(self):
        assert FixTier.T0_GUARANTEED == 0
        assert FixTier.T4_STRUCTURAL == 4

    def test_all_tiers_present(self):
        assert len(FixTier) == 5

    def test_safety_mapping_complete(self):
        expected = {
            "guaranteed",
            "idempotent",
            "needs_validation",
            "needs_review",
            "requires_human",
        }
        assert set(SAFETY_TO_TIER.keys()) == expected


class TestParseTierString:
    @pytest.mark.parametrize(
        "input_str,expected",
        [
            ("T0", FixTier.T0_GUARANTEED),
            ("T1", FixTier.T1_IDEMPOTENT),
            ("T2", FixTier.T2_VALIDATED),
            ("T3", FixTier.T3_CONTEXTUAL),
            ("T4", FixTier.T4_STRUCTURAL),
            ("0", FixTier.T0_GUARANTEED),
            ("4", FixTier.T4_STRUCTURAL),
            ("GUARANTEED", FixTier.T0_GUARANTEED),
            ("VALIDATED", FixTier.T2_VALIDATED),
            ("STRUCTURAL", FixTier.T4_STRUCTURAL),
        ],
    )
    def test_valid_inputs(self, input_str, expected):
        assert _parse_tier_string(input_str) == expected

    def test_invalid_input(self):
        with pytest.raises(ValueError):
            _parse_tier_string("T99")

    def test_whitespace_stripped(self):
        assert _parse_tier_string("  T0  ") == FixTier.T0_GUARANTEED


class TestResolveTier:
    def test_default_is_t2(self):
        f = FakeFinding(rule="totally-unknown-rule")
        assert resolve_tier(f) == FixTier.T2_VALIDATED

    def test_recipe_safety_overrides(self):
        f = FakeFinding(rule="ruff-e501")
        recipe = FakeRecipe(safety="guaranteed")
        assert resolve_tier(f, recipe=recipe) == FixTier.T0_GUARANTEED

    def test_recipe_safety_needs_validation(self):
        f = FakeFinding(rule="unknown")
        recipe = FakeRecipe(safety="needs_validation")
        assert resolve_tier(f, recipe=recipe) == FixTier.T2_VALIDATED

    def test_recipe_safety_requires_human(self):
        f = FakeFinding(rule="unknown")
        recipe = FakeRecipe(safety="requires_human")
        assert resolve_tier(f, recipe=recipe) == FixTier.T4_STRUCTURAL

    def test_config_overrides_no_recipe(self):
        f = FakeFinding(rule="some-rule")
        assert (
            resolve_tier(f, config_overrides={"some-rule": "T0"})
            == FixTier.T0_GUARANTEED
        )

    def test_recipe_takes_priority_over_config(self):
        f = FakeFinding(rule="some-rule")
        recipe = FakeRecipe(safety="guaranteed")
        assert (
            resolve_tier(f, recipe=recipe, config_overrides={"some-rule": "T4"})
            == FixTier.T0_GUARANTEED
        )

    def test_tier_overrides_ruff_e501(self):
        f = FakeFinding(rule="ruff-e501")
        assert resolve_tier(f) == FixTier.T0_GUARANTEED

    def test_tier_overrides_ruff_b904(self):
        f = FakeFinding(rule="ruff-b904")
        assert resolve_tier(f) == FixTier.T3_CONTEXTUAL

    def test_tier_overrides_xo_max_lines(self):
        f = FakeFinding(rule="xo-max-lines")
        assert resolve_tier(f) == FixTier.T4_STRUCTURAL

    def test_config_overrides_invalid_tier_falls_through(self):
        f = FakeFinding(rule="ruff-e501")
        assert (
            resolve_tier(f, config_overrides={"ruff-e501": "INVALID"})
            == FixTier.T0_GUARANTEED
        )

    def test_empty_config_overrides(self):
        f = FakeFinding(rule="ruff-e501")
        assert resolve_tier(f, config_overrides={}) == FixTier.T0_GUARANTEED


class TestTierEscalation:
    def test_no_escalation_below_threshold(self):
        state = {
            "finding_activity": {
                "a": {"rule": "test", "action": "fix-failed"},
            }
        }
        assert (
            check_tier_escalation("test", state, FixTier.T2_VALIDATED, threshold=3)
            is None
        )

    def test_escalation_at_threshold(self):
        state = {
            "finding_activity": {
                "a": {"rule": "test", "action": "fix-failed-1"},
                "b": {"rule": "test", "action": "fix-failed-2"},
                "c": {"rule": "test", "action": "fix-failed-3"},
            }
        }
        result = check_tier_escalation("test", state, FixTier.T2_VALIDATED, threshold=3)
        assert result == FixTier.T3_CONTEXTUAL

    def test_no_escalation_at_t4(self):
        state = {
            "finding_activity": {
                "a": {"rule": "test", "action": "fix-failed"},
                "b": {"rule": "test", "action": "fix-failed"},
                "c": {"rule": "test", "action": "fix-failed"},
            }
        }
        assert (
            check_tier_escalation("test", state, FixTier.T4_STRUCTURAL, threshold=3)
            is None
        )

    def test_no_escalation_wrong_rule(self):
        state = {
            "finding_activity": {
                "a": {"rule": "other-rule", "action": "fix-failed"},
                "b": {"rule": "other-rule", "action": "fix-failed"},
                "c": {"rule": "other-rule", "action": "fix-failed"},
            }
        }
        assert (
            check_tier_escalation("test", state, FixTier.T2_VALIDATED, threshold=3)
            is None
        )

    def test_no_escalation_non_failure_actions(self):
        state = {
            "finding_activity": {
                "a": {"rule": "test", "action": "resolved"},
                "b": {"rule": "test", "action": "resolved"},
                "c": {"rule": "test", "action": "resolved"},
            }
        }
        assert (
            check_tier_escalation("test", state, FixTier.T2_VALIDATED, threshold=3)
            is None
        )

    def test_escalation_from_t0(self):
        state = {
            "finding_activity": {
                "a": {"rule": "t", "action": "fix-failed"},
                "b": {"rule": "t", "action": "fix-failed"},
                "c": {"rule": "t", "action": "fix-failed"},
            }
        }
        result = check_tier_escalation("t", state, FixTier.T0_GUARANTEED, threshold=3)
        assert result == FixTier.T1_IDEMPOTENT


class TestTieredValidatorT0:
    def test_valid_python_passes(self, tmp_path):
        (tmp_path / "test.py").write_text("x = 1\n")
        f = FakeFinding(path="test.py")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.passed
        assert result.tier == FixTier.T0_GUARANTEED

    def test_syntax_error_fails(self, tmp_path):
        (tmp_path / "test.py").write_text("def foo(\n")
        f = FakeFinding(path="test.py")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert not result.passed
        assert "syntax error" in result.message.lower()

    def test_missing_file_fails(self, tmp_path):
        f = FakeFinding(path="nonexistent.py")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert not result.passed
        assert "not found" in result.message.lower()

    def test_non_python_passes(self, tmp_path):
        (tmp_path / "app.ts").write_text("const x: any = 1;")
        f = FakeFinding(path="app.ts")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.passed

    def test_latency_recorded(self, tmp_path):
        (tmp_path / "test.py").write_text("x = 1\n")
        f = FakeFinding(path="test.py")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.latency_ms >= 0


class TestTieredValidatorT1:
    def test_passes_with_no_checks(self, tmp_path):
        f = FakeFinding()
        v = TieredValidator()
        result = v.validate(FixTier.T1_IDEMPOTENT, tmp_path, f, checks=None)
        assert result.passed

    def test_passes_with_empty_checks(self, tmp_path):
        f = FakeFinding()
        v = TieredValidator()
        result = v.validate(FixTier.T1_IDEMPOTENT, tmp_path, f, checks={})
        assert result.passed

    def test_passes_with_good_baseline(self, tmp_path):
        f = FakeFinding()
        baseline = {"check_a": {"rc": 0}}
        v = TieredValidator()
        result = v.validate(
            FixTier.T1_IDEMPOTENT,
            tmp_path,
            f,
            baseline_results=baseline,
            checks={"check_a": ["true"]},
        )
        assert result.passed

    def test_fails_with_bad_baseline(self, tmp_path):
        f = FakeFinding()
        baseline = {"check_a": {"rc": 1}}
        v = TieredValidator()
        result = v.validate(
            FixTier.T1_IDEMPOTENT,
            tmp_path,
            f,
            baseline_results=baseline,
            checks={"check_a": ["false"]},
        )
        assert not result.passed


class TestTieredValidatorT4:
    def test_sets_human_review_flag(self, tmp_path):
        f = FakeFinding(path="big_file.py")
        (tmp_path / "big_file.py").write_text("x = 1\n")
        v = TieredValidator()
        with patch.object(
            v,
            "_validate_t3",
            return_value=TieredValidationResult(passed=True, message="ok"),
        ):
            result = v.validate(FixTier.T4_STRUCTURAL, tmp_path, f)
        assert result.requires_human_review


class TestAutoDiffReview:
    def test_clean_worktree_passes(self, tmp_path):
        f = FakeFinding(path="test.py")
        with patch("bluei.engine.utils.run_capture", return_value=(0, "")):
            ok, msg = _auto_diff_review(tmp_path, f)
        assert ok

    def test_critical_file_blocked(self, tmp_path):
        f = FakeFinding(path="test.py")
        with patch("bluei.engine.utils.run_capture") as mock_rc:
            mock_rc.side_effect = [
                (0, "package-lock.json |  5 +++--\n1 file changed"),
                (0, "5\t3\tpackage-lock.json"),
            ]
            ok, msg = _auto_diff_review(tmp_path, f)
        assert not ok
        assert "critical" in msg.lower()

    def test_multi_file_blocked(self, tmp_path):
        f = FakeFinding(path="test.py")
        with patch("bluei.engine.utils.run_capture") as mock_rc:
            mock_rc.return_value = (0, "a.py | 1 +\nb.py | 1 -\n2 files changed")
            ok, msg = _auto_diff_review(tmp_path, f)
        assert not ok
        assert "too many" in msg.lower()


class TestTierOverrides:
    def test_all_overrides_are_valid_tiers(self):
        for rule, tier in TIER_OVERRIDES.items():
            assert isinstance(tier, FixTier)

    def test_no_t0_overrides_for_bug_rules(self):
        bug_rules = {
            "discount-math-sign",
            "catalog-query-not-normalized",
            "broad-except",
        }
        for rule in bug_rules:
            if rule in TIER_OVERRIDES:
                assert TIER_OVERRIDES[rule] >= FixTier.T2_VALIDATED


# ── Tests extracted from test_campaigns_scaffold_tiers_remaining.py ─────────


class TestTieredValidatorT2:
    def test_t2_delegates_to_validation_gate(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        v = TieredValidator()
        gate_result = {
            "passed": True,
            "message": "all good",
            "regressions": [],
            "target_failures": [],
        }
        mock_validation = MagicMock()
        mock_validation.run_validation_gate.return_value = gate_result
        with patch.dict("sys.modules", {"bluei.engine.validation": mock_validation}):
            result = v.validate(
                FixTier.T2_VALIDATED,
                tmp_path,
                f,
                checks={"test": ["pytest"]},
                log_file=tmp_path / "log.txt",
            )
        assert result.passed
        assert result.regressions == []
        assert result.target_failures == []


class TestTieredValidatorT3:
    def test_t3_fails_on_diff_review(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        v = TieredValidator()
        with patch.object(
            v,
            "_validate_t2",
            return_value=TieredValidationResult(passed=True, message="ok"),
        ):
            with patch.dict(
                "sys.modules",
                {
                    "bluei.engine.utils": MagicMock(
                        run_capture=MagicMock(
                            return_value=(0, "a.py | 1 +\nb.py | 1 -\n2 files changed")
                        )
                    )
                },
            ):
                result = v.validate(
                    FixTier.T3_CONTEXTUAL,
                    tmp_path,
                    f,
                    log_file=tmp_path / "log.txt",
                )
        assert not result.passed
        assert "diff review failed" in result.message

    def test_t3_passes_when_all_checks_ok(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        v = TieredValidator()
        with patch.object(
            v,
            "_validate_t2",
            return_value=TieredValidationResult(passed=True, message="ok"),
        ):
            with patch.dict(
                "sys.modules",
                {
                    "bluei.engine.utils": MagicMock(
                        run_capture=MagicMock(return_value=(0, ""))
                    )
                },
            ):
                result = v.validate(
                    FixTier.T3_CONTEXTUAL,
                    tmp_path,
                    f,
                    log_file=tmp_path / "log.txt",
                )
        assert result.passed


class TestAutoDiffReviewEdgeCases:
    def test_git_diff_stat_fails(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        with patch("bluei.engine.utils.run_capture", return_value=(1, "")):
            ok, msg = _auto_diff_review(tmp_path, f)
        assert not ok
        assert "git diff" in msg

    def test_single_file_passes(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        with patch("bluei.engine.utils.run_capture") as mock_rc:
            mock_rc.side_effect = [
                (0, "app.py | 1 +\n1 file changed"),
                (0, "1\t0\tapp.py"),
            ]
            ok, msg = _auto_diff_review(tmp_path, f)
        assert ok

    def test_numstat_additions_exceeded(self, tmp_path):
        f = MagicMock()
        with patch("bluei.engine.utils.run_capture") as mock_rc:
            mock_rc.side_effect = [
                (0, "app.py | 100 +\n1 file changed"),
                (0, "100\t0\tapp.py"),
            ]
            ok, msg = _auto_diff_review(tmp_path, f)
        assert not ok
        assert "too many additions" in msg

    def test_numstat_deletions_exceeded(self, tmp_path):
        f = MagicMock()
        with patch("bluei.engine.utils.run_capture") as mock_rc:
            mock_rc.side_effect = [
                (0, "app.py | 50 -\n1 file changed"),
                (0, "0\t100\tapp.py"),
            ]
            ok, msg = _auto_diff_review(tmp_path, f)
        assert not ok
        assert "too many deletions" in msg

    def test_numstat_binary_file_dashes(self, tmp_path):
        f = MagicMock()
        with patch("bluei.engine.utils.run_capture") as mock_rc:
            mock_rc.side_effect = [
                (0, "image.png | Bin\n1 file changed"),
                (0, "-\t-\timage.png"),
            ]
            ok, msg = _auto_diff_review(tmp_path, f)
        assert ok

    def test_diff_stat_skips_non_pipe_lines(self, tmp_path):
        f = MagicMock()
        with patch("bluei.engine.utils.run_capture") as mock_rc:
            mock_rc.side_effect = [
                (0, "fatal: something\napp.py | 1 +\n1 file changed"),
                (0, "1\t0\tapp.py"),
            ]
            ok, msg = _auto_diff_review(tmp_path, f)
        assert ok

    def test_numstat_with_value_error_handled(self, tmp_path):
        f = MagicMock()
        with patch("bluei.engine.utils.run_capture") as mock_rc:
            mock_rc.side_effect = [
                (0, "app.py | 1 +\n1 file changed"),
                (0, "not_a_number\tnot_a_number\tapp.py"),
            ]
            ok, msg = _auto_diff_review(tmp_path, f)
        assert ok


class TestResolveTierEdgeCases:
    def test_finding_without_rule_attr(self):
        f = MagicMock(spec=[])
        result = resolve_tier(f)
        assert result == FixTier.T2_VALIDATED

    def test_recipe_with_none_safety(self):
        f = MagicMock()
        f.rule = "test"
        recipe = MagicMock()
        recipe.safety = None
        result = resolve_tier(f, recipe=recipe)
        assert result == FixTier.T2_VALIDATED

    def test_recipe_with_unknown_safety(self):
        f = MagicMock()
        f.rule = "test"
        recipe = MagicMock()
        recipe.safety = "totally_unknown"
        result = resolve_tier(f, recipe=recipe)
        assert result == FixTier.T2_VALIDATED


class TestTieredValidatorT1EdgeCases:
    def test_t1_uses_run_named_checks_when_no_baseline(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        v = TieredValidator()
        mock_lifecycle = MagicMock()
        mock_lifecycle.run_named_checks.return_value = {"check_a": {"rc": 0}}
        with patch.dict("sys.modules", {"bluei.engine.lifecycle": mock_lifecycle}):
            result = v.validate(
                FixTier.T1_IDEMPOTENT,
                tmp_path,
                f,
                checks={"check_a": ["true"]},
                log_file=tmp_path / "log.txt",
            )
        assert result.passed

    def test_t1_run_named_checks_fails(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        v = TieredValidator()
        mock_lifecycle = MagicMock()
        mock_lifecycle.run_named_checks.return_value = {"check_a": {"rc": 1}}
        with patch.dict("sys.modules", {"bluei.engine.lifecycle": mock_lifecycle}):
            result = v.validate(
                FixTier.T1_IDEMPOTENT,
                tmp_path,
                f,
                checks={"check_a": ["false"]},
                log_file=tmp_path / "log.txt",
            )
        assert not result.passed


class TestTieredValidatorT0EdgeCases:
    def test_t0_compile_check_generic_exception(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        (tmp_path / "test.py").write_text("x = 1\n", encoding="utf-8")
        v = TieredValidator()
        with patch("builtins.compile", side_effect=OSError("disk error")):
            result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert not result.passed
        assert "compile check failed" in result.message

    def test_t0_go_file_passes(self, tmp_path):
        f = MagicMock()
        f.path = "main.go"
        (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.passed

    def test_t0_rs_file_passes(self, tmp_path):
        f = MagicMock()
        f.path = "main.rs"
        (tmp_path / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.passed

    def test_t0_tsx_file_passes(self, tmp_path):
        f = MagicMock()
        f.path = "app.tsx"
        (tmp_path / "app.tsx").write_text(
            "export default function() {}\n", encoding="utf-8"
        )
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.passed

    def test_t0_jsx_file_passes(self, tmp_path):
        f = MagicMock()
        f.path = "app.jsx"
        (tmp_path / "app.jsx").write_text(
            "export default function() {}\n", encoding="utf-8"
        )
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.passed

    def test_t0_js_file_passes(self, tmp_path):
        f = MagicMock()
        f.path = "app.js"
        (tmp_path / "app.js").write_text("module.exports = {};\n", encoding="utf-8")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.passed

    def test_t0_no_log_file_uses_dev_null(self, tmp_path):
        f = MagicMock()
        f.path = "test.py"
        (tmp_path / "test.py").write_text("x = 1\n", encoding="utf-8")
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f, log_file=None)
        assert result.passed

    def test_t0_empty_path(self, tmp_path):
        f = MagicMock()
        f.path = ""
        v = TieredValidator()
        result = v.validate(FixTier.T0_GUARANTEED, tmp_path, f)
        assert result.passed
