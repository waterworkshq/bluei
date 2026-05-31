import json
from pathlib import Path

import pytest

from bluei.engine.shared_pattern_library import (
    PROMOTION_MIN_APPLICATIONS,
    PROMOTION_MIN_REPOS,
    PROMOTION_MIN_SUCCESS_RATE,
    CrossRepoPattern,
    SharedPatternLibrary,
    check_promotion_eligibility,
    promote_pattern_to_recipe,
    _now_iso,
)


def _make_cross_repo_pattern(
    rule="ruff-b904",
    source_repos=None,
    success_count=25,
    failure_count=0,
    confidence=0.96,
    **kw,
):
    return CrossRepoPattern(
        pattern_id=kw.get("pattern_id", f"xp-{rule}-abc12345"),
        rule=rule,
        language=kw.get("language", "python"),
        structural_hash=kw.get("structural_hash", "abc123def4567890"),
        before_snippet=kw.get("before_snippet", "raise ValueError()"),
        after_snippet=kw.get("after_snippet", "raise ValueError() from cause"),
        confidence=confidence,
        source_repos=source_repos or {"repo-a", "repo-b", "repo-c", "repo-d", "repo-e"},
        success_count=success_count,
        failure_count=failure_count,
    )


class TestCheckPromotionEligibility:
    def test_eligible_pattern(self):
        pat = _make_cross_repo_pattern()
        assert check_promotion_eligibility(pat) is True

    def test_too_few_repos(self):
        pat = _make_cross_repo_pattern(source_repos={"repo-a", "repo-b"})
        assert check_promotion_eligibility(pat) is False

    def test_too_low_success_rate(self):
        pat = _make_cross_repo_pattern(success_count=20, failure_count=5)
        total = 25
        rate = 20 / total
        assert rate < PROMOTION_MIN_SUCCESS_RATE
        assert check_promotion_eligibility(pat) is False

    def test_too_few_applications(self):
        pat = _make_cross_repo_pattern(success_count=10, failure_count=0)
        assert check_promotion_eligibility(pat) is False

    def test_has_failures(self):
        pat = _make_cross_repo_pattern(failure_count=1)
        assert check_promotion_eligibility(pat) is False

    def test_zero_total(self):
        pat = _make_cross_repo_pattern(success_count=0, failure_count=0)
        assert check_promotion_eligibility(pat) is False


class TestPromotePatternToRecipe:
    def test_generates_valid_yaml(self, tmp_path):
        pat = _make_cross_repo_pattern()
        out_dir = tmp_path / "staged"
        result = promote_pattern_to_recipe(pat, "python", out_dir)
        assert result is not None
        assert result.exists()
        content = result.read_text()
        assert "auto-ruff-b904-" in content
        assert "rule_exact" in content
        assert "regex_substitute" in content
        assert "bluei-auto-promotion" in content

    def test_idempotent_safety_for_high_confidence(self, tmp_path):
        pat = _make_cross_repo_pattern(confidence=0.97)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        content = result.read_text()
        assert "safety: idempotent" in content

    def test_needs_validation_safety_for_lower_confidence(self, tmp_path):
        pat = _make_cross_repo_pattern(confidence=0.94)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        content = result.read_text()
        assert "safety: needs_validation" in content

    def test_staged_dir_created(self, tmp_path):
        out_dir = tmp_path / "nested" / "staged"
        pat = _make_cross_repo_pattern()
        result = promote_pattern_to_recipe(pat, "python", out_dir)
        assert result is not None
        assert out_dir.is_dir()

    def test_ineligible_pattern_returns_none(self, tmp_path):
        pat = _make_cross_repo_pattern(source_repos={"repo-a"})
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is None

    def test_recipe_metadata(self, tmp_path):
        pat = _make_cross_repo_pattern()
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        content = result.read_text()
        assert f"source_pattern_id: {pat.pattern_id}" in content
        assert "promoted_at:" in content

    def test_source_repos_in_metadata(self, tmp_path):
        pat = _make_cross_repo_pattern()
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        content = result.read_text()
        assert "source_repos:" in content


class TestRecipeEngineLoadsStaged:
    def test_staged_recipe_dir_exists(self):
        from bluei.engine.recipe_engine import staged_recipe_dir
        d = staged_recipe_dir()
        assert d.name == "staged"
        assert d.is_dir()

    def test_engine_loads_staged_dir(self, tmp_path):
        from bluei.engine.recipe_engine import RecipeEngine, staged_recipe_dir
        from bluei.engine.recipe_schema import load_recipe
        staged = tmp_path / "staged"
        staged.mkdir()
        recipe_yaml = staged / "test-auto.yaml"
        recipe_yaml.write_text(
            "id: test-auto\n"
            "rule: test-auto-rule\n"
            "language: python\n"
            "safety: idempotent\n"
            "description: test\n"
            "match:\n"
            "  type: rule_exact\n"
            "  rule: test-auto-rule\n"
            "replacement:\n"
            "  type: text\n"
            "  value: old\n"
            "  replacement: new\n"
        )
        engine = RecipeEngine([staged])
        assert engine.has_recipe("test-auto-rule")


class TestPromotionEndToEnd:
    def test_publish_eligible_promoted(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "shared")
        for repo in ["repo-a", "repo-b", "repo-c", "repo-d", "repo-e"]:
            lib.publish(
                rule="test-promo",
                language="python",
                before_snippet="raise Error",
                after_snippet="raise Error from cause",
                confidence=0.96,
                repo_id=repo,
                success_count=5,
                failure_count=0,
            )
        cross_pat = lib._get_cached("python").get("test-promo::")
        if cross_pat is None:
            for v in lib._get_cached("python").values():
                if v.rule == "test-promo":
                    cross_pat = v
                    break
        assert cross_pat is not None
        result = promote_pattern_to_recipe(
            cross_pat, "python", tmp_path / "staged",
        )
        assert result is not None
        assert result.exists()

    def test_publish_ineligible_not_promoted(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "shared")
        lib.publish(
            rule="no-promo",
            language="python",
            before_snippet="x = 1",
            after_snippet="x = 2",
            confidence=0.85,
            repo_id="single-repo",
        )
        cross_pat = None
        for v in lib._get_cached("python").values():
            if v.rule == "no-promo":
                cross_pat = v
                break
        if cross_pat:
            result = promote_pattern_to_recipe(
                cross_pat, "python", tmp_path / "staged",
            )
            assert result is None
