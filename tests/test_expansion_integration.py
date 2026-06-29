#!/usr/bin/env python3
"""Integration tests for P4.0 expansion.

Verifies:
1. Recipe + context rule interaction for Tier 1 rules
2. classify_finding() precedence holds with new entries
3. RecipeCascadeStage picks up new recipes
4. Existing rules are unaffected (regression)
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from bluei.engine.constants import CONTEXT_RULES
from bluei.engine.reforge import (
    CLAUDE_FIX_RULES,
    REFACTOR_CLASS_RULES,
    RefactorClass,
    classify_finding,
    get_context_rule,
    match_context,
)
from bluei.engine.recipe_engine import RecipeEngine, builtin_recipe_dir


class TestRecipeContextInteraction:
    """Tier 1 rules: recipes work, context rules protect wrong contexts."""

    def test_orders_tax_truncation_recipe_applies_in_source(self):
        engine = RecipeEngine(recipe_dirs=[builtin_recipe_dir()])
        recipe = engine.match("orders-tax-truncation", language="python")
        assert recipe is not None, "Recipe should exist for orders-tax-truncation"

    def test_orders_tax_truncation_source_routes_simple_fix(self, make_finding):
        f = make_finding(
            path="src/orders.py",
            rule="orders-tax-truncation",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.SIMPLE_FIX

    def test_orders_tax_truncation_test_routes_refactor_class(self, make_finding):
        f = make_finding(
            path="tests/test_orders.py",
            rule="orders-tax-truncation",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_debug_console_log_recipe_applies_in_source(self):
        engine = RecipeEngine(recipe_dirs=[builtin_recipe_dir()])
        recipe = engine.match("debug-console-log", language="javascript")
        assert recipe is not None

    def test_debug_console_log_source_routes_simple_fix(self, make_finding):
        f = make_finding(
            path="src/app.ts",
            rule="debug-console-log",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.SIMPLE_FIX

    def test_debug_console_log_test_routes_refactor_class(self, make_finding):
        f = make_finding(
            path="src/foo.test.ts",
            rule="debug-console-log",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_go_empty_interface_recipe_applies_in_source(self):
        engine = RecipeEngine(recipe_dirs=[builtin_recipe_dir()])
        recipe = engine.match("go-empty-interface", language="go")
        assert recipe is not None

    def test_go_empty_interface_source_routes_simple_fix(self, make_finding):
        f = make_finding(
            path="src/types.go",
            rule="go-empty-interface",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.SIMPLE_FIX

    def test_go_empty_interface_vendor_routes_refactor_class(self, make_finding):
        f = make_finding(
            path="vendor/lib/types.go",
            rule="go-empty-interface",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.REFACTOR_CLASS


class TestClassifyFindingPrecedence:
    """Verify classify_finding() precedence with new entries."""

    def test_refactor_class_rules_take_priority_over_context(self, make_finding):
        f = make_finding(
            path="src/main.py",
            rule="xo-max-lines",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_context_rules_take_priority_over_claude_fix(self, make_finding):
        f = make_finding(
            path="src/types/global.d.ts",
            rule="ts-unsafe-any-cast",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.REFACTOR_CLASS

    def test_claude_fix_rules_for_uncovered_files(self, make_finding):
        f = make_finding(
            path="src/app.ts",
            rule="ts-unsafe-any-cast",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.CONTEXTUAL_FIX

    def test_recipe_match_after_context_rules(self, make_finding):
        f = make_finding(
            path="src/main.py",
            rule="trailing-whitespace",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.SIMPLE_FIX

    def test_cascade_fix_for_rules_with_no_routing(self, make_finding):
        f = make_finding(
            path="src/app.py",
            rule="doc-gap-uncovered-module",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.CASCADE_FIX

    def test_doc_drift_stale_reference_cascade(self, make_finding):
        f = make_finding(
            path="src/app.py",
            rule="doc-drift-stale-reference",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.CASCADE_FIX

    def test_claude_fix_rules_for_test_coverage(self, make_finding):
        f = make_finding(
            path="src/app.py",
            rule="test-coverage-branch",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.CLAUDE_FIX

    def test_claude_fix_rules_for_test_coverage_function(self, make_finding):
        f = make_finding(
            path="src/app.py",
            rule="test-coverage-function",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.CLAUDE_FIX

    def test_claude_fix_rules_for_test_gap_missing_case(self, make_finding):
        f = make_finding(
            path="src/app.py",
            rule="test-gap-missing-case",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(f)
        assert rc == RefactorClass.CLAUDE_FIX


class TestRecipeEngineIntegration:
    """Verify RecipeEngine picks up new recipes."""

    def test_total_recipe_count(self):
        engine = RecipeEngine(recipe_dirs=[builtin_recipe_dir()])
        assert engine.recipe_count == 17

    def test_new_recipes_have_matching_context_rules(self):
        engine = RecipeEngine(recipe_dirs=[builtin_recipe_dir()])
        for rule, lang in [
            ("orders-tax-truncation", "python"),
            ("debug-console-log", "javascript"),
            ("go-empty-interface", "go"),
        ]:
            assert engine.has_recipe(rule, language=lang), f"Recipe missing: {rule}"
            ctx = get_context_rule(rule)
            assert ctx is not None, f"Context rule missing for recipe: {rule}"

    def test_all_new_recipes_load_without_error(self):
        engine = RecipeEngine(recipe_dirs=[builtin_recipe_dir()])
        for rule, lang in [
            ("orders-tax-truncation", "python"),
            ("debug-console-log", "javascript"),
            ("go-empty-interface", "go"),
        ]:
            recipe = engine.match(rule, language=lang)
            assert recipe is not None, f"Recipe not matched: {rule}"
            assert recipe.replacement.type in ("text", "regex_substitute")


class TestRegressionExistingRules:
    """Existing 17 context rules and 13 original recipes still work."""

    @pytest.mark.parametrize(
        "rule",
        [
            "ruff-c408",
            "ruff-b904",
            "ruff-b007",
            "ruff-s311",
            "ruff-e501",
            "broad-except",
            "perf-pop-front-loop",
            "perf-list-membership-loop",
            "trailing-whitespace",
            "hardcoded-tmp-path",
            "type-explicit-any",
            "type-missing-return",
            "xo-max-lines",
            "xo-complexity",
            "xo-no-warning-comments",
            "test-gap-missing-file",
            "test-coverage-line",
        ],
    )
    def test_existing_context_rules_present(self, rule):
        ctx = get_context_rule(rule)
        assert ctx is not None, f"Existing context rule missing: {rule}"

    @pytest.mark.parametrize(
        "rule,lang",
        [
            ("catalog-query-not-normalized", "python"),
            ("discount-math-sign", "python"),
            ("docs-legacy-reference", "python"),
            ("docs-missing-rollback", "python"),
            ("docs-quickstart-gap", "python"),
            ("hardcoded-tmp-path", "python"),
            ("inventory-invalid-quantity", "python"),
            ("notifications-email-no-trim", "python"),
            ("notifications-type-guard-missing", "python"),
            ("test-gap-missing-file", "python"),
            ("trailing-whitespace", "python"),
            ("type-explicit-any", "typescript"),
        ],
    )
    def test_existing_recipes_still_load(self, rule, lang):
        engine = RecipeEngine(recipe_dirs=[builtin_recipe_dir()])
        assert engine.has_recipe(rule, language=lang), (
            f"Existing recipe missing: {rule}"
        )

    def test_context_rules_no_duplicates(self):
        names = [r["rule"] for r in CONTEXT_RULES]
        assert len(names) == len(set(names))

    def test_refactor_class_rules_unchanged(self):
        assert "xo-max-lines" in REFACTOR_CLASS_RULES
        assert "xo-complexity" in REFACTOR_CLASS_RULES

    def test_claude_fix_rules_unchanged(self):
        assert "test-coverage-branch" in CLAUDE_FIX_RULES
        assert "test-coverage-function" in CLAUDE_FIX_RULES
        assert "test-gap-missing-case" in CLAUDE_FIX_RULES


class TestRoutingCoverageSummary:
    """Final coverage counts matching the plan."""

    def test_context_rules_count(self):
        assert len(CONTEXT_RULES) == 28

    def test_refactor_class_count(self):
        assert len(REFACTOR_CLASS_RULES) == 4

    def test_claude_fix_count(self):
        assert len(CLAUDE_FIX_RULES) == 3

    def test_recipe_count(self):
        engine = RecipeEngine(recipe_dirs=[builtin_recipe_dir()])
        assert engine.recipe_count == 17
