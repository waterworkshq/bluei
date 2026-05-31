"""Tests for bluei.engine.recipe_schema — dataclasses and YAML loader."""

import yaml
import tempfile
from pathlib import Path

import pytest

from bluei.engine.recipe_schema import (
    Recipe,
    RecipeMatch,
    RecipeReplacement,
    RecipeValidation,
    load_recipe,
)


class TestRecipeMatch:
    def test_default_scope_is_line(self):
        m = RecipeMatch(type="rule_exact")
        assert m.scope == "line"
        assert m.type == "rule_exact"

    def test_all_fields(self):
        m = RecipeMatch(
            type="regex",
            pattern=r"import\s+os",
            rule="F401",
            prefix="src/",
            scope="file",
            context_guard={"excludes_pattern": r"test_.*\.py"},
        )
        assert m.type == "regex"
        assert m.pattern == r"import\s+os"
        assert m.rule == "F401"
        assert m.prefix == "src/"
        assert m.scope == "file"
        assert m.context_guard == {"excludes_pattern": r"test_.*\.py"}

    def test_optional_fields_are_none_by_default(self):
        m = RecipeMatch(type="prefix")
        assert m.pattern is None
        assert m.rule is None
        assert m.prefix is None
        assert m.context_guard is None


class TestRecipeReplacement:
    def test_default_count_is_one(self):
        r = RecipeReplacement(type="text")
        assert r.count == 1

    def test_all_fields(self):
        r = RecipeReplacement(
            type="regex_substitute",
            value="old_import",
            command=["sed", "-i"],
            template_file="fix.j2",
            pattern=r"import\s+(\w+)",
            replacement=r"from \1 import",
            prepend_pattern=r"^#",
            prepend_template="# coding: utf-8",
            condition="file_exists",
            count=3,
        )
        assert r.type == "regex_substitute"
        assert r.value == "old_import"
        assert r.command == ["sed", "-i"]
        assert r.template_file == "fix.j2"
        assert r.pattern == r"import\s+(\w+)"
        assert r.replacement == r"from \1 import"
        assert r.prepend_pattern == r"^#"
        assert r.prepend_template == "# coding: utf-8"
        assert r.condition == "file_exists"
        assert r.count == 3

    def test_nested_objects_are_independent(self):
        r1 = RecipeReplacement(type="text", value="a")
        r2 = RecipeReplacement(type="text", value="b")
        assert r1.value != r2.value


class TestRecipeValidation:
    def test_defaults(self):
        v = RecipeValidation()
        assert v.run_baseline is True
        assert v.run_target is True

    def test_custom(self):
        v = RecipeValidation(run_baseline=False, run_target=False)
        assert v.run_baseline is False
        assert v.run_target is False


class TestRecipe:
    def test_minimal(self):
        r = Recipe(id="test-1", rule="F401")
        assert r.id == "test-1"
        assert r.rule == "F401"
        assert r.language == "*"
        assert r.safety == "needs_validation"
        assert r.description == ""
        assert isinstance(r.match, RecipeMatch)
        assert isinstance(r.replacement, RecipeReplacement)
        assert isinstance(r.validation, RecipeValidation)
        assert isinstance(r.metadata, dict)
        assert r.priority == 1

    def test_full(self):
        r = Recipe(
            id="fix-001",
            rule="E501*",
            language="python",
            safety="safe",
            description="Fix long lines",
            match=RecipeMatch(type="regex", pattern=r".{100,}"),
            replacement=RecipeReplacement(type="text", value="break"),
            validation=RecipeValidation(run_baseline=True, run_target=False),
            metadata={"author": "qa-bot"},
            priority=10,
        )
        assert r.language == "python"
        assert r.safety == "safe"
        assert r.match.type == "regex"
        assert r.replacement.type == "text"
        assert r.validation.run_target is False
        assert r.metadata["author"] == "qa-bot"
        assert r.priority == 10


class TestLoadRecipe:
    def test_loads_minimal_recipe(self):
        yaml_content = "id: minimal\nrule: F401\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            path = Path(f.name)

        try:
            recipe = load_recipe(path)
            assert recipe.id == "minimal"
            assert recipe.rule == "F401"
        finally:
            path.unlink(missing_ok=True)

    def test_loads_full_recipe(self):
        yaml_content = """\
id: full-recipe
rule: E501
language: python
safety: safe
description: Fix long lines
match:
  type: regex
  pattern: ".{120,}"
replacement:
  type: text
  value: "break"
validation:
  run_baseline: true
  run_target: false
metadata:
  author: qa-bot
priority: 5
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            path = Path(f.name)

        try:
            recipe = load_recipe(path)
            assert recipe.id == "full-recipe"
            assert recipe.language == "python"
            assert recipe.match.type == "regex"
            assert recipe.replacement.type == "text"
            assert recipe.validation.run_target is False
            assert recipe.metadata["author"] == "qa-bot"
            assert recipe.priority == 5
        finally:
            path.unlink(missing_ok=True)

    def test_missing_id_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("rule: F401\n")
            f.flush()
            path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="missing required field"):
                load_recipe(path)
        finally:
            path.unlink(missing_ok=True)

    def test_missing_rule_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("id: no-rule\n")
            f.flush()
            path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="missing required field"):
                load_recipe(path)
        finally:
            path.unlink(missing_ok=True)

    def test_not_a_mapping_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("- list_item\n")
            f.flush()
            path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="does not contain a YAML mapping"):
                load_recipe(path)
        finally:
            path.unlink(missing_ok=True)

    def test_empty_file_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("\n")
            f.flush()
            path = Path(f.name)

        try:
            with pytest.raises(ValueError):
                load_recipe(path)
        finally:
            path.unlink(missing_ok=True)
