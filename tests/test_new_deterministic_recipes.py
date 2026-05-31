#!/usr/bin/env python3
"""Tests for the 3 new deterministic recipes: orders-tax-truncation, debug-console-log, go-empty-interface."""

import textwrap
from pathlib import Path

import pytest

from bluei.engine.recipe_engine import RecipeEngine, builtin_recipe_dir
from bluei.engine.recipe_handlers import (
    RecipeFixResult,
    RegexSubstituteHandler,
    TextHandler,
)
from bluei.engine.recipe_schema import (
    Recipe,
    RecipeReplacement,
    load_recipe,
)


def _write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _load_new_recipe(recipe_id: str) -> Recipe:
    path = builtin_recipe_dir() / f"{recipe_id}.yaml"
    assert path.exists(), f"Recipe file not found: {path}"
    return load_recipe(path)


# ── Recipe Loading ─────────────────────────────────────────────────────────


class TestRecipeLoading:
    def test_orders_tax_truncation_loads(self):
        r = _load_new_recipe("orders-tax-truncation")
        assert r.id == "orders-tax-truncation"
        assert r.rule == "orders-tax-truncation"
        assert r.language == "python"
        assert r.safety == "needs_validation"
        assert r.replacement.type == "regex_substitute"

    def test_debug_console_log_loads(self):
        r = _load_new_recipe("debug-console-log")
        assert r.id == "debug-console-log"
        assert r.rule == "debug-console-log"
        assert r.language == "javascript"
        assert r.safety == "needs_validation"
        assert r.replacement.type == "regex_substitute"

    def test_go_empty_interface_loads(self):
        r = _load_new_recipe("go-empty-interface")
        assert r.id == "go-empty-interface"
        assert r.rule == "go-empty-interface"
        assert r.language == "go"
        assert r.safety == "needs_validation"
        assert r.replacement.type == "regex_substitute"


# ── orders-tax-truncation ──────────────────────────────────────────────────


class TestOrdersTaxTruncation:
    def test_replaces_int_with_round(self, tmp_path):
        code = "def calc_tax(order):\n    return int(order.subtotal * order.tax_rate)\n"
        fp = _write_file(tmp_path / "src" / "orders.py", code)
        recipe = _load_new_recipe("orders-tax-truncation")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert result.success
        new_code = fp.read_text()
        assert "round(" in new_code
        assert "int(" not in new_code
        assert "2" in new_code

    def test_guard_skips_if_already_round(self, tmp_path):
        code = "def calc_tax(order):\n    return round(order.subtotal * order.tax_rate, 2)\n"
        fp = _write_file(tmp_path / "src" / "orders.py", code)
        recipe = _load_new_recipe("orders-tax-truncation")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert not result.success

    def test_no_match_returns_failure(self, tmp_path):
        code = "def foo():\n    return 42\n"
        fp = _write_file(tmp_path / "src" / "foo.py", code)
        recipe = _load_new_recipe("orders-tax-truncation")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert not result.success

    def test_does_not_match_int_without_multiply(self, tmp_path):
        code = "x = int(3.7)\n"
        fp = _write_file(tmp_path / "src" / "util.py", code)
        recipe = _load_new_recipe("orders-tax-truncation")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert not result.success

    def test_engine_matches_rule(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        recipe = engine.match("orders-tax-truncation", "python")
        assert recipe is not None
        assert recipe.id == "orders-tax-truncation"

    def test_engine_no_match_wrong_language(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        recipe = engine.match("orders-tax-truncation", "go")
        assert recipe is None


# ── debug-console-log ──────────────────────────────────────────────────────


class TestDebugConsoleLog:
    def test_removes_console_log_line(self, tmp_path):
        code = 'function foo() {\n  console.log("debug");\n  return 1;\n}\n'
        fp = _write_file(tmp_path / "src" / "utils.ts", code)
        recipe = _load_new_recipe("debug-console-log")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert result.success
        new_code = fp.read_text()
        assert "console.log" not in new_code
        assert "return 1" in new_code

    def test_removes_without_semicolon(self, tmp_path):
        code = 'function foo() {\n  console.log("debug")\n  return 1;\n}\n'
        fp = _write_file(tmp_path / "src" / "utils.ts", code)
        recipe = _load_new_recipe("debug-console-log")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert result.success
        assert "console.log" not in fp.read_text()

    def test_no_console_log_returns_failure(self, tmp_path):
        code = "function foo() {\n  return 1;\n}\n"
        fp = _write_file(tmp_path / "src" / "utils.ts", code)
        recipe = _load_new_recipe("debug-console-log")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert not result.success

    def test_preserves_other_code(self, tmp_path):
        code = 'const x = 1;\nconsole.log("debug");\nconst y = 2;\n'
        fp = _write_file(tmp_path / "src" / "app.ts", code)
        recipe = _load_new_recipe("debug-console-log")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert result.success
        new_code = fp.read_text()
        assert "const x = 1" in new_code
        assert "const y = 2" in new_code
        assert "console.log" not in new_code

    def test_engine_matches_rule(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        recipe = engine.match("debug-console-log", "javascript")
        assert recipe is not None
        assert recipe.id == "debug-console-log"

    def test_engine_no_match_wrong_language(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        recipe = engine.match("debug-console-log", "python")
        assert recipe is None


# ── go-empty-interface ─────────────────────────────────────────────────────


class TestGoEmptyInterface:
    def test_replaces_interface_with_any(self, tmp_path):
        code = "package main\n\nfunc Process(v interface{}) {\n}\n"
        fp = _write_file(tmp_path / "pkg" / "handler.go", code)
        recipe = _load_new_recipe("go-empty-interface")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert result.success
        new_code = fp.read_text()
        assert "any" in new_code
        assert "interface{}" not in new_code

    def test_no_interface_returns_failure(self, tmp_path):
        code = "package main\n\nfunc Process(v any) {\n}\n"
        fp = _write_file(tmp_path / "pkg" / "handler.go", code)
        recipe = _load_new_recipe("go-empty-interface")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert not result.success

    def test_replaces_one_at_a_time(self, tmp_path):
        code = "package main\n\nvar a interface{}\nvar b interface{}\n"
        fp = _write_file(tmp_path / "pkg" / "types.go", code)
        recipe = _load_new_recipe("go-empty-interface")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert result.success
        new_code = fp.read_text()
        assert new_code.count("any") == 1
        assert new_code.count("interface{}") == 1

    def test_preserves_string_literal(self, tmp_path):
        code = 'package main\n\nimport "fmt"\nfunc main() {\n\tfmt.Println("interface{}")\n}\n'
        fp = _write_file(tmp_path / "pkg" / "main.go", code)
        recipe = _load_new_recipe("go-empty-interface")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert not result.success
        assert '"interface{}"' in fp.read_text()

    def test_preserves_backtick_string(self, tmp_path):
        code = "package main\n\nvar s = `interface{}`\n"
        fp = _write_file(tmp_path / "pkg" / "main.go", code)
        recipe = _load_new_recipe("go-empty-interface")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert not result.success
        assert "`interface{}`" in fp.read_text()

    def test_replaces_map_type(self, tmp_path):
        code = "package main\n\nvar m = map[string]interface{}{}\n"
        fp = _write_file(tmp_path / "pkg" / "types.go", code)
        recipe = _load_new_recipe("go-empty-interface")
        handler = RegexSubstituteHandler()
        result = handler.apply(recipe, fp, tmp_path)
        assert result.success
        assert "map[string]any" in fp.read_text()

    def test_engine_matches_rule(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        recipe = engine.match("go-empty-interface", "go")
        assert recipe is not None
        assert recipe.id == "go-empty-interface"

    def test_engine_no_match_wrong_language(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        recipe = engine.match("go-empty-interface", "python")
        assert recipe is None


# ── Engine Integration ─────────────────────────────────────────────────────


class TestEngineIntegration:
    def test_all_three_recipes_load_in_engine(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        assert engine.match("orders-tax-truncation", "python") is not None
        assert engine.match("debug-console-log", "javascript") is not None
        assert engine.match("go-empty-interface", "go") is not None

    def test_existing_recipes_still_load(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        assert engine.match("discount-math-sign", "python") is not None
        assert engine.match("trailing-whitespace", "python") is not None
        assert engine.match("hardcoded-tmp-path", "python") is not None

    def test_recipe_count_increased(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        all_recipe_ids = set()
        for recipe_list in engine.recipes.values():
            for r in recipe_list:
                all_recipe_ids.add(r.id)
        for r in engine._wildcard_recipes:
            all_recipe_ids.add(r.id)
        assert len(all_recipe_ids) >= 14, (
            f"Expected >= 14 recipes, got {len(all_recipe_ids)}"
        )
