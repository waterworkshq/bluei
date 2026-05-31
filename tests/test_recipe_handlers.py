"""Tests for bluei/engine/recipe_handlers.py — text, regex, command, scaffold handlers.

Uses real file operations with tmp_path. No mocks needed for text/regex handlers.
CommandHandler is tested with real subprocess calls (using `echo` which is universal).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bluei.engine.recipe_handlers import (
    HANDLER_REGISTRY,
    CommandHandler,
    RecipeFixResult,
    RegexSubstituteHandler,
    TextHandler,
)
from bluei.engine.recipe_schema import (
    Recipe,
    RecipeMatch,
    RecipeReplacement,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_recipe(**overrides) -> Recipe:
    """Build a minimal Recipe with sensible defaults for handler tests."""
    replacement_kw = overrides.pop("replacement", {})
    replacement = RecipeReplacement(
        type=replacement_kw.get("type", "text"),
        value=replacement_kw.get("value"),
        pattern=replacement_kw.get("pattern"),
        replacement=replacement_kw.get("replacement"),
        command=replacement_kw.get("command"),
        condition=replacement_kw.get("condition"),
        prepend_pattern=replacement_kw.get("prepend_pattern"),
        prepend_template=replacement_kw.get("prepend_template"),
        count=replacement_kw.get("count", 1),
    )
    return Recipe(
        id=overrides.get("id", "test-recipe"),
        rule=overrides.get("rule", "TEST001"),
        replacement=replacement,
        metadata=overrides.get("metadata", {}),
    )


def _write_file(worktree: Path, name: str, content: str) -> Path:
    p = worktree / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# RecipeFixResult
# ---------------------------------------------------------------------------


class TestRecipeFixResult:
    def test_defaults(self):
        r = RecipeFixResult(success=True)
        assert r.success is True
        assert r.files_changed == []
        assert r.validation_passed is True
        assert r.error is None
        assert r.recipe_id == ""
        assert r.method == ""

    def test_all_fields(self):
        r = RecipeFixResult(
            success=False,
            files_changed=["a.py"],
            validation_passed=False,
            error="boom",
            recipe_id="r1",
            method="text",
        )
        assert r.error == "boom"
        assert r.method == "text"


# ---------------------------------------------------------------------------
# HANDLER_REGISTRY
# ---------------------------------------------------------------------------


class TestHandlerRegistry:
    def test_all_handler_types_registered(self):
        assert "text" in HANDLER_REGISTRY
        assert "regex_substitute" in HANDLER_REGISTRY
        assert "command" in HANDLER_REGISTRY
        assert "scaffold" in HANDLER_REGISTRY

    def test_text_handler_instantiable(self):
        handler = HANDLER_REGISTRY["text"]()
        assert isinstance(handler, TextHandler)


# ---------------------------------------------------------------------------
# TextHandler
# ---------------------------------------------------------------------------


class TestTextHandler:
    @pytest.fixture
    def handler(self):
        return TextHandler()

    def test_literal_replacement(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "import os\nimport sys\n")
        recipe = _make_recipe(
            replacement={"value": "import os", "replacement": "# removed"}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        assert "removed" in f.read_text()
        assert "import os" not in f.read_text()

    def test_regex_replacement(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "x = 42\n")
        recipe = _make_recipe(
            replacement={"pattern": r"x = \d+", "replacement": "x = 99"}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        assert "x = 99" in f.read_text()

    def test_file_not_found(self, handler, tmp_path):
        missing = tmp_path / "nope.py"
        recipe = _make_recipe(replacement={"value": "old", "replacement": "new"})
        result = handler.apply(recipe, missing, tmp_path)
        assert result.success is False
        assert "file not found" in result.error

    def test_no_match_no_change(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "hello world\n")
        recipe = _make_recipe(
            replacement={"value": "not_here", "replacement": "changed"}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "no changes" in result.error

    def test_guard_condition_met_skips_fix(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "already_fixed = True\nimport os\n")
        recipe = _make_recipe(
            replacement={
                "value": "import os",
                "replacement": "# removed",
                "condition": "already_fixed",
            }
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "guard" in result.error

    def test_regex_guard_condition_met_skips_fix(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "SKIP=True\nx = 42\n")
        recipe = _make_recipe(
            replacement={
                "pattern": r"x = \d+",
                "replacement": "x = 0",
                "condition": "SKIP=True",
            }
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False

    def test_only_first_occurrence_replaced(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "foo foo foo\n")
        recipe = _make_recipe(replacement={"value": "foo", "replacement": "bar"})
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        text = f.read_text()
        assert text.count("bar") == 1
        assert text.count("foo") == 2

    def test_relative_path_in_files_changed(self, handler, tmp_path):
        f = _write_file(tmp_path, "src/app.py", "old code")
        recipe = _make_recipe(replacement={"value": "old", "replacement": "new"})
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        assert str(result.files_changed[0]) == "src/app.py"

    def test_method_and_recipe_id_set(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "old")
        recipe = _make_recipe(
            id="my-recipe-42", replacement={"value": "old", "replacement": "new"}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.method == "text"
        assert result.recipe_id == "my-recipe-42"


# ---------------------------------------------------------------------------
# RegexSubstituteHandler
# ---------------------------------------------------------------------------


class TestRegexSubstituteHandler:
    @pytest.fixture
    def handler(self):
        return RegexSubstituteHandler()

    def test_basic_regex_substitution(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "def foo(): pass\n")
        recipe = _make_recipe(
            replacement={"pattern": r"def (\w+)", "replacement": r"async def \1"}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        assert "async def foo" in f.read_text()

    def test_no_pattern_returns_error(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "text\n")
        recipe = _make_recipe(replacement={"pattern": None})
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "no regex pattern" in result.error

    def test_file_not_found(self, handler, tmp_path):
        missing = tmp_path / "nope.py"
        recipe = _make_recipe(replacement={"pattern": r"\d+"})
        result = handler.apply(recipe, missing, tmp_path)
        assert result.success is False
        assert "file not found" in result.error

    def test_no_match_no_change(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "hello\n")
        recipe = _make_recipe(
            replacement={"pattern": r"xyz\d+", "replacement": "replaced"}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "no changes" in result.error

    @pytest.mark.parametrize(
        "flag,pattern,replacement,input_text,expected",
        [
            ("regex_dotall", r"line1.line2", "matched", "line1\nline2\n", True),
            ("regex_multiline", r"^line2$", "matched", "line1\nline2\n", True),
        ],
    )
    def test_regex_flags_from_metadata(
        self, handler, tmp_path, flag, pattern, replacement, input_text, expected
    ):
        f = _write_file(tmp_path, "app.py", input_text)
        recipe = _make_recipe(
            replacement={"pattern": pattern, "replacement": replacement},
            metadata={flag: True},
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is expected

    def test_guard_condition_not_met_skips(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "x = 42\n")
        recipe = _make_recipe(
            replacement={
                "pattern": r"x = \d+",
                "replacement": "x = 0",
                "condition": "MUST_EXIST_FIRST",
            }
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "guard condition not met" in result.error

    def test_guard_condition_present_allows_fix(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "PREREQ=True\nx = 42\n")
        recipe = _make_recipe(
            replacement={
                "pattern": r"x = \d+",
                "replacement": "x = 0",
                "condition": "PREREQ=True",
            }
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True

    def test_prepend_pattern_and_template(self, handler, tmp_path):
        content = "import os\nimport sys\n"
        f = _write_file(tmp_path, "app.py", content)
        recipe = _make_recipe(
            replacement={
                "pattern": r"import sys",
                "replacement": "import sys",
                "prepend_pattern": r"(import os)",
                "prepend_template": r"# header\n\1",
            }
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        text = f.read_text()
        assert "# header" in text

    def test_count_limits_substitutions(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "aaa bbb aaa bbb\n")
        recipe = _make_recipe(
            replacement={"pattern": "aaa", "replacement": "xxx", "count": 1}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        text = f.read_text()
        assert text.count("xxx") == 1
        assert text.count("aaa") == 1

    def test_method_and_recipe_id_set(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "x = 42")
        recipe = _make_recipe(
            id="regex-r1",
            replacement={"pattern": r"\d+", "replacement": "0"},
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.method == "regex_substitute"
        assert result.recipe_id == "regex-r1"


# ---------------------------------------------------------------------------
# CommandHandler
# ---------------------------------------------------------------------------


class TestCommandHandler:
    @pytest.fixture
    def handler(self):
        return CommandHandler()

    def test_no_command_returns_error(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "hello\n")
        recipe = _make_recipe(replacement={"command": None})
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "no command" in result.error

    def test_successful_command_changes_file(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "old content\n")
        # `sed -i` works on Linux; replaces "old" with "new" in-place
        recipe = _make_recipe(
            replacement={"command": ["sed", "-i", "s/old/new/", "{file}"]}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        assert "new content" in f.read_text()

    def test_command_not_found(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "content\n")
        recipe = _make_recipe(
            replacement={"command": ["nonexistent_tool_xyz", "{file}"]}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "command not found" in result.error

    def test_command_no_file_change(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "unchanged\n")
        # `true` succeeds but doesn't modify the file
        recipe = _make_recipe(replacement={"command": ["true"]})
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "no file changes" in result.error

    def test_file_placeholder_expanded(self, handler, tmp_path):
        f = _write_file(tmp_path, "sub/app.py", "old content\n")
        recipe = _make_recipe(
            replacement={"command": ["sed", "-i", "s/old/new/", "{file}"]}
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.success is True
        assert str(result.files_changed[0]) == "sub/app.py"

    def test_rule_code_placeholder_from_finding(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "x = 1\n")
        finding = MagicMock()
        finding.rule = "ruff-E501"
        # Use `true` — file won't change, but verify no crash from placeholder expansion
        recipe = _make_recipe(replacement={"command": ["true", "{rule_code}"]})
        result = handler.apply(recipe, f, tmp_path, finding=finding)
        # Command runs (returns 0) but no file change → expected failure
        assert result.success is False

    def test_command_timeout(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "x\n")
        recipe = _make_recipe(replacement={"command": ["sleep", "120"]})
        with patch("bluei.engine.recipe_handlers.subprocess.run") as mock_run:
            import subprocess

            mock_run.side_effect = subprocess.TimeoutExpired(cmd="sleep", timeout=60)
            result = handler.apply(recipe, f, tmp_path)
        assert result.success is False
        assert "timed out" in result.error

    def test_method_and_recipe_id_set(self, handler, tmp_path):
        f = _write_file(tmp_path, "app.py", "old\n")
        recipe = _make_recipe(
            id="cmd-r1",
            replacement={"command": ["sed", "-i", "s/old/new/", "{file}"]},
        )
        result = handler.apply(recipe, f, tmp_path)
        assert result.method == "command"
        assert result.recipe_id == "cmd-r1"
