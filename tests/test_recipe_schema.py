"""Tests for bluei.engine.recipe_schema — dataclasses and YAML loader."""

import json
import yaml
import tempfile
from pathlib import Path

import pytest

from bluei.engine.recipe_schema import (
    MAX_RECIPE_ID_LENGTH,
    Recipe,
    RecipeMatch,
    RecipeReplacement,
    RecipeValidation,
    is_safe_recipe_id,
    load_recipe,
    parse_recipe,
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


class TestRecipeIdValidation:
    """D3-03: the recipe id becomes a filename + asset_ref and may be
    LLM-authored, so parse_recipe rejects path-like/over-long ids at the
    schema gate."""

    def test_accepts_slug_ids(self):
        recipe = parse_recipe("id: eslint-no-unused-vars\nrule: eslint-no-unused-vars\n")
        assert recipe.id == "eslint-no-unused-vars"

    def test_accepts_max_length_and_mixed_charset(self):
        long_id = "a" + "B9._-" * 19 + "xxxx"  # exactly 100 chars, all allowed
        assert len(long_id) == MAX_RECIPE_ID_LENGTH
        recipe = parse_recipe(f"id: {long_id}\nrule: F401\n")
        assert recipe.id == long_id

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../../escape",  # traversal (D3-03)
            "/abs/path",  # absolute path (D3-03)
            "a/b",  # nested path
            "..",  # parent ref
            "",  # empty
            ".",  # bare dot
            "-leading-dash",  # shell-flag-looking lead
            "sp ace",  # whitespace
            "tab\tid",  # control char
            "uñîcode",  # non-ASCII
            "semi;colon",  # shell metachar
            "x" * (MAX_RECIPE_ID_LENGTH + 1),  # over-long
        ],
    )
    def test_parse_recipe_rejects_unsafe_ids(self, bad_id):
        text = f"id: {json.dumps(bad_id)}\nrule: F401\n"
        with pytest.raises(ValueError, match="Recipe id must match"):
            parse_recipe(text)

    def test_parse_recipe_rejects_non_string_id(self):
        with pytest.raises(ValueError, match="Recipe id must match"):
            parse_recipe("id: 123\nrule: F401\n")

    def test_is_safe_recipe_id_direct(self):
        assert is_safe_recipe_id("eslint-no-unused-vars") is True
        assert is_safe_recipe_id("../../escape") is False
        assert is_safe_recipe_id("") is False
        assert is_safe_recipe_id(None) is False
        assert is_safe_recipe_id(123) is False
