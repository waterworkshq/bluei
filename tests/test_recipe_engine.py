"""test_recipe_engine.py — Tests for the declarative fix recipe system."""

import textwrap
from pathlib import Path

import pytest

from bluei.engine.recipe_schema import (
    Recipe,
    RecipeMatch,
    RecipeReplacement,
    RecipeValidation,
    load_recipe,
)
from bluei.engine.recipe_handlers import (
    CommandHandler,
    RecipeFixResult,
    RegexSubstituteHandler,
    TextHandler,
)
from bluei.engine.recipe_engine import RecipeEngine, builtin_recipe_dir


def _write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ── Schema Tests ──────────────────────────────────────────────────────────


class TestLoadRecipe:
    def test_load_valid_recipe(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            id: test-rule
            rule: test-rule
            language: python
            safety: needs_validation
            description: "Test recipe"
            match:
              type: rule_exact
            replacement:
              type: text
              value: "old"
              replacement: "new"
            validation:
              run_baseline: true
              run_target: false
        """)
        p = tmp_path / "test-rule.yaml"
        p.write_text(yaml_content)
        r = load_recipe(p)
        assert r.id == "test-rule"
        assert r.rule == "test-rule"
        assert r.language == "python"
        assert r.safety == "needs_validation"
        assert r.replacement.type == "text"
        assert r.replacement.value == "old"
        assert r.replacement.replacement == "new"

    def test_load_minimal_recipe(self, tmp_path):
        yaml_content = "id: minimal\nrule: minimal\n"
        p = tmp_path / "minimal.yaml"
        p.write_text(yaml_content)
        r = load_recipe(p)
        assert r.id == "minimal"
        assert r.language == "*"
        assert r.safety == "needs_validation"

    def test_load_missing_id_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("rule: foo\n")
        with pytest.raises(ValueError, match="missing required field: id"):
            load_recipe(p)

    def test_load_missing_rule_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("id: foo\n")
        with pytest.raises(ValueError, match="missing required field: rule"):
            load_recipe(p)

    def test_load_non_mapping_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("[]\n")
        with pytest.raises(ValueError, match="does not contain a YAML mapping"):
            load_recipe(p)


# ── Handler Tests ─────────────────────────────────────────────────────────


class TestTextHandler:
    def test_simple_replace(self, tmp_path):
        fp = _write_file(tmp_path / "src" / "example.py", "return amount + discount\n")
        recipe = Recipe(
            id="test",
            rule="test",
            replacement=RecipeReplacement(
                type="text",
                value="return amount + discount",
                replacement="return amount - discount",
            ),
        )
        result = TextHandler().apply(recipe, fp, tmp_path)
        assert result.success
        assert "return amount - discount" in fp.read_text()

    def test_no_match_returns_failure(self, tmp_path):
        fp = _write_file(tmp_path / "src" / "example.py", "no match here\n")
        recipe = Recipe(
            id="test",
            rule="test",
            replacement=RecipeReplacement(
                type="text", value="missing", replacement="new"
            ),
        )
        result = TextHandler().apply(recipe, fp, tmp_path)
        assert not result.success
        assert "no changes" in result.error

    def test_missing_file_returns_failure(self, tmp_path):
        fp = tmp_path / "nonexistent.py"
        recipe = Recipe(
            id="test", rule="test", replacement=RecipeReplacement(type="text")
        )
        result = TextHandler().apply(recipe, fp, tmp_path)
        assert not result.success
        assert "not found" in result.error

    def test_guard_condition_prevents_replace(self, tmp_path):
        fp = _write_file(
            tmp_path / "doc.md", "## Incident handling\n## Rollback\nstuff\n"
        )
        recipe = Recipe(
            id="test",
            rule="test",
            replacement=RecipeReplacement(
                type="text",
                value="## Incident handling",
                replacement="## Incident handling\nNEW SECTION\n",
                condition="## Rollback",
            ),
        )
        result = TextHandler().apply(recipe, fp, tmp_path)
        assert not result.success


class TestRegexSubstituteHandler:
    def test_regex_replacement(self, tmp_path):
        fp = _write_file(tmp_path / "src" / "types.ts", "let x: any = 1;\n")
        recipe = Recipe(
            id="test",
            rule="test",
            replacement=RecipeReplacement(
                type="regex_substitute",
                pattern=r":\s*any\b",
                replacement=": unknown",
            ),
        )
        result = RegexSubstituteHandler().apply(recipe, fp, tmp_path)
        assert result.success
        assert "let x: unknown = 1;" in fp.read_text()

    def test_no_pattern_returns_failure(self, tmp_path):
        fp = _write_file(tmp_path / "f.py", "text\n")
        recipe = Recipe(
            id="test",
            rule="test",
            replacement=RecipeReplacement(type="regex_substitute"),
        )
        result = RegexSubstituteHandler().apply(recipe, fp, tmp_path)
        assert not result.success
        assert "no regex pattern" in result.error

    def test_dotall_flag(self, tmp_path):
        fp = _write_file(tmp_path / "doc.md", "## Quick start\n```bash\nold\n```\n")
        recipe = Recipe(
            id="test",
            rule="test",
            replacement=RecipeReplacement(
                type="regex_substitute",
                pattern="## Quick start\\n```bash\\n.*?```",
                replacement="## Quick start\n```bash\nnew\n```",
            ),
            metadata={"regex_dotall": True},
        )
        result = RegexSubstituteHandler().apply(recipe, fp, tmp_path)
        assert result.success
        assert "new" in fp.read_text()


class TestCommandHandler:
    def test_command_modifies_file(self, tmp_path):
        fp = _write_file(tmp_path / "f.py", "x = 1  \n")
        recipe = Recipe(
            id="test",
            rule="test",
            replacement=RecipeReplacement(
                type="command",
                command=["sed", "-i", "s/  $//", "{file}"],
            ),
        )
        result = CommandHandler().apply(recipe, fp, tmp_path)
        assert result.success

    def test_missing_command_returns_failure(self, tmp_path):
        fp = _write_file(tmp_path / "f.py", "text\n")
        recipe = Recipe(
            id="test", rule="test", replacement=RecipeReplacement(type="command")
        )
        result = CommandHandler().apply(recipe, fp, tmp_path)
        assert not result.success
        assert "no command" in result.error

    def test_nonexistent_command_returns_failure(self, tmp_path):
        fp = _write_file(tmp_path / "f.py", "text\n")
        recipe = Recipe(
            id="test",
            rule="test",
            replacement=RecipeReplacement(
                type="command",
                command=["nonexistent_tool_xyz", "{file}"],
            ),
        )
        result = CommandHandler().apply(recipe, fp, tmp_path)
        assert not result.success
        assert "not found" in result.error

    def test_rule_code_substitution(self, tmp_path, make_finding):
        fp = _write_file(tmp_path / "f.py", "x=1\n")
        finding = make_finding(
            finding_id="f1",
            repo="test",
            rule="ruff-C408",
            path="src/example.py",
            snippet="",
            confidence=0.9,
        )
        recipe = Recipe(
            id="test",
            rule="ruff-*",
            replacement=RecipeReplacement(
                type="command",
                command=["echo", "{rule_code}", "{file}"],
            ),
        )
        result = CommandHandler().apply(recipe, fp, tmp_path, finding=finding)
        assert not result.success
        assert "no file changes" in result.error or result.success is False


# ── Engine Tests ──────────────────────────────────────────────────────────


class TestRecipeEngine:
    def test_builtin_recipes_load(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        assert engine.recipe_count >= 12

    def test_exact_match(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        r = engine.match("discount-math-sign", "python")
        assert r is not None
        assert r.id == "discount-math-sign"

    def test_wildcard_match(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        r = engine.match("ruff-C408", "python")
        assert r is not None
        assert r.id == "ruff-autofix"

    def test_no_match(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        r = engine.match("nonexistent-rule", "python")
        assert r is None

    def test_language_filter_rejects(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        r = engine.match("discount-math-sign", "typescript")
        assert r is None

    def test_wildcard_language_matches_any(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        r = engine.match("trailing-whitespace", "python")
        assert r is not None
        r2 = engine.match("trailing-whitespace", "typescript")
        assert r2 is not None

    def test_has_recipe(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        assert engine.has_recipe("discount-math-sign")
        assert engine.has_recipe("ruff-C408")
        assert not engine.has_recipe("nonexistent")

    def test_eslint_autofix_wildcard_loaded(self):
        """AC-P2-7: eslint-autofix.yaml covers autofixable eslint rules."""
        engine = RecipeEngine([builtin_recipe_dir()])
        assert engine.has_recipe("eslint-no-unused-vars", language="typescript")
        assert engine.has_recipe("eslint-no-unused-vars", language="javascript")
        assert engine.has_recipe("eslint-no-unused-vars", language="python")
        recipe = engine.match("eslint-no-unused-vars", "typescript")
        assert recipe is not None
        assert recipe.id == "eslint-autofix"
        assert recipe.language == "*"

    def test_empty_dirs(self):
        engine = RecipeEngine([])
        assert engine.recipe_count == 0
        assert engine.match("anything", "python") is None

    def test_nonexistent_dir(self):
        engine = RecipeEngine([Path("/nonexistent")])
        assert engine.recipe_count == 0

    def test_apply_text_recipe(self, tmp_path):
        engine = RecipeEngine([builtin_recipe_dir()])
        fp = _write_file(tmp_path / "src" / "example.py", "return amount + discount\n")
        recipe = engine.match("discount-math-sign", "python")
        assert recipe is not None
        result = engine.apply(recipe, fp, tmp_path)
        assert result.success
        assert "return amount - discount" in fp.read_text()

    def test_apply_regex_recipe(self, tmp_path):
        engine = RecipeEngine([builtin_recipe_dir()])
        fp = _write_file(tmp_path / "types.ts", "let x: any = 1;\n")
        recipe = engine.match("type-explicit-any", "typescript")
        assert recipe is not None
        result = engine.apply(recipe, fp, tmp_path)
        assert result.success
        assert ": unknown" in fp.read_text()


class TestRecipeEngineCustomDir:
    def test_load_from_custom_dir(self, tmp_path):
        recipe_yaml = textwrap.dedent("""\
            id: custom-fix
            rule: custom-rule
            language: python
            replacement:
              type: text
              value: "bad"
              replacement: "good"
        """)
        (tmp_path / "custom-fix.yaml").write_text(recipe_yaml)
        engine = RecipeEngine([tmp_path])
        assert engine.recipe_count == 1
        r = engine.match("custom-rule", "python")
        assert r is not None
        assert r.id == "custom-fix"

    def test_custom_dir_priority_over_builtin(self, tmp_path):
        recipe_yaml = textwrap.dedent("""\
            id: custom-trailing
            rule: trailing-whitespace
            language: "*"
            priority: 3
            replacement:
              type: text
              value: "  "
              replacement: ""
        """)
        (tmp_path / "custom-trailing.yaml").write_text(recipe_yaml)
        engine = RecipeEngine([tmp_path, builtin_recipe_dir()])
        r = engine.match("trailing-whitespace", "python")
        assert r is not None
        assert r.id == "custom-trailing"


# ── Integration: lifecycle.py with recipe engine ──────────────────────────


class TestRecipeIntegration:
    def test_discount_math_via_recipe(self, tmp_path, make_finding):
        from bluei.engine.lifecycle import apply_autofix

        repo = tmp_path / "repo"
        repo.mkdir()
        fp = repo / "src" / "example.py"
        fp.parent.mkdir(parents=True)
        fp.write_text(
            "def price(amount, discount):\n    return amount + discount\n",
            encoding="utf-8",
        )

        import subprocess

        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True
        )

        log = tmp_path / "run.log"
        finding = make_finding(
            finding_id="f1",
            repo="test",
            rule="discount-math-sign",
            path="src/example.py",
            snippet="",
            confidence=0.9,
        )
        result = apply_autofix(repo, finding, log)
        assert result is True
        assert "return amount - discount" in fp.read_text()
        assert "recipe=" in log.read_text()

    def test_trailing_whitespace_via_recipe(self, tmp_path, make_finding):
        from bluei.engine.lifecycle import apply_autofix

        repo = tmp_path / "repo"
        repo.mkdir()
        fp = repo / "src" / "example.py"
        fp.parent.mkdir(parents=True)
        fp.write_text("x = 1  \ny = 2\t\n", encoding="utf-8")

        import subprocess

        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True
        )

        log = tmp_path / "run.log"
        finding = make_finding(
            finding_id="f1",
            repo="test",
            rule="trailing-whitespace",
            path="src/example.py",
            snippet="",
            confidence=0.9,
        )
        result = apply_autofix(repo, finding, log)
        assert result is True
        content = fp.read_text()
        assert "x = 1  " not in content
        assert "y = 2\t" not in content

    def test_no_recipe_falls_through_to_legacy(self, tmp_path, make_finding):
        from bluei.engine.lifecycle import apply_autofix

        repo = tmp_path / "repo"
        repo.mkdir()
        fp = repo / "src" / "queue.py"
        fp.parent.mkdir(parents=True)
        fp.write_text(
            "from os import path\nitems = [1,2,3]\nfor i in range(10):\n    x = items.pop(0)\n",
            encoding="utf-8",
        )

        import subprocess

        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True
        )

        log = tmp_path / "run.log"
        finding = make_finding(
            finding_id="f1",
            repo="test",
            rule="perf-pop-front-loop",
            path="src/queue.py",
            snippet="",
            confidence=0.9,
        )
        result = apply_autofix(repo, finding, log)
        assert result is True
        assert "popleft" in fp.read_text()

    def test_unknown_rule_no_op(self, tmp_path, make_finding):
        from bluei.engine.lifecycle import apply_autofix

        repo = tmp_path / "repo"
        repo.mkdir()
        fp = repo / "src" / "example.py"
        fp.parent.mkdir(parents=True)
        fp.write_text("x = 1\n", encoding="utf-8")

        import subprocess

        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True
        )

        log = tmp_path / "run.log"
        finding = make_finding(
            finding_id="f1",
            repo="test",
            rule="unknown-rule-xyz",
            path="src/example.py",
            snippet="",
            confidence=0.9,
        )
        result = apply_autofix(repo, finding, log)
        assert result is False


# ── Built-in recipe validation ────────────────────────────────────────────


class TestBuiltinRecipes:
    @pytest.mark.parametrize("yaml_file", sorted(builtin_recipe_dir().glob("*.yaml")))
    def test_recipe_loads(self, yaml_file):
        recipe = load_recipe(yaml_file)
        assert recipe.id
        assert recipe.rule
        assert recipe.replacement.type in (
            "text",
            "regex_substitute",
            "command",
            "template",
            "scaffold",
        )

    def test_recipe_count(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        assert engine.recipe_count >= 10

    def test_all_text_recipes_have_replacement(self):
        engine = RecipeEngine([builtin_recipe_dir()])
        for rule, recipes in engine.recipes.items():
            for r in recipes:
                if r.replacement.type == "text":
                    assert (
                        r.replacement.value is not None
                        or r.replacement.pattern is not None
                    ), f"Text recipe {r.id} missing value/pattern"


# ── Merged from test_recipe_engine_remaining.py ──

import logging
from unittest.mock import MagicMock as _MagicMock


def _write_recipe(path: Path, yaml_content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_content, encoding="utf-8")
    return path


class TestLoadAllExceptionHandling:
    def test_bad_yaml_file_logs_warning_and_continues(self, tmp_path, caplog):
        good_yaml = textwrap.dedent("""\
            id: good-recipe
            rule: good-rule
            language: python
            replacement:
              type: text
              value: "old"
              replacement: "new"
        """)
        _write_recipe(tmp_path / "good.yaml", good_yaml)
        (tmp_path / "bad.yaml").write_text("not: valid: yaml: [\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="bluei.engine.recipe_engine"):
            engine = RecipeEngine([tmp_path])

        assert engine.recipe_count == 1
        assert engine.match("good-rule", "python") is not None
        assert any("failed to load" in r.message for r in caplog.records)

    def test_missing_required_field_logs_warning(self, tmp_path, caplog):
        (tmp_path / "missing_id.yaml").write_text("rule: something\n", encoding="utf-8")
        _write_recipe(
            tmp_path / "valid.yaml",
            "id: valid\nrule: valid\nlanguage: python\n",
        )

        with caplog.at_level(logging.WARNING, logger="bluei.engine.recipe_engine"):
            engine = RecipeEngine([tmp_path])

        assert engine.recipe_count == 1

    def test_multiple_bad_files_all_logged(self, tmp_path, caplog):
        (tmp_path / "bad1.yaml").write_text("[]\n", encoding="utf-8")
        (tmp_path / "bad2.yaml").write_text("id: only-id\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="bluei.engine.recipe_engine"):
            engine = RecipeEngine([tmp_path])

        assert engine.recipe_count == 0
        warning_msgs = [r.message for r in caplog.records]
        assert len(warning_msgs) == 2


class TestScoreRecipeFallbackBranch:
    def test_non_matching_rule_returns_zero(self, tmp_path):
        engine = RecipeEngine([tmp_path])
        recipe = Recipe(
            id="test",
            rule="completely-different-rule",
            language="*",
            match=RecipeMatch(type="rule_exact"),
            replacement=RecipeReplacement(type="text"),
        )
        score = engine._score_recipe(recipe, "target-rule", "python", {})
        assert score == 0.0

    def test_non_wildcard_mismatch_returns_zero(self, tmp_path):
        engine = RecipeEngine([tmp_path])
        recipe = Recipe(
            id="test",
            rule="alpha",
            language="*",
            match=RecipeMatch(type="rule_exact"),
            replacement=RecipeReplacement(type="text"),
        )
        score = engine._score_recipe(recipe, "beta", "python", {})
        assert score == 0.0

    def test_wildcard_prefix_mismatch_returns_zero(self, tmp_path):
        engine = RecipeEngine([tmp_path])
        recipe = Recipe(
            id="test",
            rule="alpha-*",
            language="*",
            match=RecipeMatch(type="rule_exact"),
            replacement=RecipeReplacement(type="text"),
        )
        score = engine._score_recipe(recipe, "beta-something", "python", {})
        assert score == 0.0


class TestContextGuardExcludesPattern:
    def test_excludes_pattern_blocks_match(self, tmp_path):
        recipe = Recipe(
            id="guarded",
            rule="my-rule",
            language="*",
            match=RecipeMatch(
                type="rule_exact",
                context_guard={"excludes_pattern": r"SKIP_THIS_FILE"},
            ),
            replacement=RecipeReplacement(type="text"),
        )
        engine = RecipeEngine([tmp_path])
        engine.recipes["my-rule"] = [recipe]

        result = engine.match(
            "my-rule", "python", file_text="some SKIP_THIS_FILE content"
        )
        assert result is None

    def test_excludes_pattern_passes_when_no_match(self, tmp_path):
        recipe = Recipe(
            id="guarded",
            rule="my-rule",
            language="*",
            priority=1,
            match=RecipeMatch(
                type="rule_exact",
                context_guard={"excludes_pattern": r"SKIP_THIS_FILE"},
            ),
            replacement=RecipeReplacement(type="text"),
        )
        engine = RecipeEngine([tmp_path])
        engine.recipes["my-rule"] = [recipe]

        result = engine.match("my-rule", "python", file_text="clean content here")
        assert result is not None
        assert result.id == "guarded"

    def test_excludes_pattern_with_empty_file_text_passes(self, tmp_path):
        recipe = Recipe(
            id="guarded",
            rule="my-rule",
            language="*",
            priority=1,
            match=RecipeMatch(
                type="rule_exact",
                context_guard={"excludes_pattern": r"bad"},
            ),
            replacement=RecipeReplacement(type="text"),
        )
        engine = RecipeEngine([tmp_path])
        engine.recipes["my-rule"] = [recipe]

        result = engine.match("my-rule", "python", file_text="")
        assert result is not None

    def test_excludes_pattern_with_no_file_text_passes(self, tmp_path):
        recipe = Recipe(
            id="guarded",
            rule="my-rule",
            language="*",
            priority=1,
            match=RecipeMatch(
                type="rule_exact",
                context_guard={"excludes_pattern": r"bad"},
            ),
            replacement=RecipeReplacement(type="text"),
        )
        engine = RecipeEngine([tmp_path])
        engine.recipes["my-rule"] = [recipe]

        result = engine.match("my-rule", "python")
        assert result is not None

    def test_context_guard_without_excludes_pattern_passes(self, tmp_path):
        recipe = Recipe(
            id="guarded",
            rule="my-rule",
            language="*",
            priority=1,
            match=RecipeMatch(
                type="rule_exact",
                context_guard={},
            ),
            replacement=RecipeReplacement(type="text"),
        )
        engine = RecipeEngine([tmp_path])
        engine.recipes["my-rule"] = [recipe]

        result = engine.match("my-rule", "python", file_text="anything")
        assert result is not None

    def test_context_guard_none_excludes_passes(self, tmp_path):
        recipe = Recipe(
            id="guarded",
            rule="my-rule",
            language="*",
            priority=1,
            match=RecipeMatch(
                type="rule_exact",
                context_guard={"excludes_pattern": None},
            ),
            replacement=RecipeReplacement(type="text"),
        )
        engine = RecipeEngine([tmp_path])
        engine.recipes["my-rule"] = [recipe]

        result = engine.match("my-rule", "python", file_text="anything")
        assert result is not None


class TestApplyUnknownHandlerType:
    def test_unknown_handler_type_returns_failure(self, tmp_path):
        recipe = Recipe(
            id="bad-handler",
            rule="test",
            replacement=RecipeReplacement(type="nonexistent_type"),
        )
        engine = RecipeEngine([tmp_path])
        fp = tmp_path / "dummy.py"
        fp.write_text("pass\n", encoding="utf-8")

        result = engine.apply(recipe, fp, tmp_path)
        assert result.success is False
        assert "unknown handler type: nonexistent_type" in result.error
        assert result.recipe_id == "bad-handler"

    def test_unknown_handler_preserves_recipe_id(self, tmp_path):
        recipe = Recipe(
            id="my-custom-id",
            rule="test",
            replacement=RecipeReplacement(type="magic"),
        )
        engine = RecipeEngine([tmp_path])

        result = engine.apply(recipe, tmp_path / "f.py", tmp_path)
        assert result.recipe_id == "my-custom-id"
        assert result.method == "magic"


class TestReload:
    def test_reload_clears_and_reindexes(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            id: reload-test
            rule: reload-rule
            language: python
            replacement:
              type: text
              value: "a"
              replacement: "b"
        """)
        _write_recipe(tmp_path / "reload-test.yaml", yaml_content)

        engine = RecipeEngine([tmp_path])
        assert engine.recipe_count == 1
        assert engine.match("reload-rule", "python") is not None

        engine.recipes.clear()
        engine._wildcard_recipes.clear()
        assert engine.recipe_count == 0

        engine.reload()
        assert engine.recipe_count == 1
        assert engine.match("reload-rule", "python") is not None

    def test_reload_with_multiple_dirs(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        _write_recipe(
            dir_a / "r1.yaml",
            "id: r1\nrule: rule-a\nlanguage: python\n",
        )
        _write_recipe(
            dir_b / "r2.yaml",
            "id: r2\nrule: rule-b\nlanguage: python\n",
        )

        engine = RecipeEngine([dir_a, dir_b])
        assert engine.recipe_count == 2

        engine.reload()
        assert engine.recipe_count == 2
        assert engine.match("rule-a", "python") is not None
        assert engine.match("rule-b", "python") is not None

    def test_reload_with_no_dirs(self, tmp_path):
        engine = RecipeEngine([])
        engine.reload()
        assert engine.recipe_count == 0

    def test_reload_picks_up_new_files(self, tmp_path):
        engine = RecipeEngine([tmp_path])
        assert engine.recipe_count == 0

        _write_recipe(
            tmp_path / "new.yaml",
            "id: new-recipe\nrule: new-rule\nlanguage: python\n",
        )
        engine.reload()
        assert engine.recipe_count == 1
        assert engine.match("new-rule", "python") is not None

    def test_reload_clears_wildcards(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            id: wc-reload
            rule: wc-*
            language: "*"
            replacement:
              type: text
              value: "x"
              replacement: "y"
        """)
        _write_recipe(tmp_path / "wc.yaml", yaml_content)

        engine = RecipeEngine([tmp_path])
        assert len(engine._wildcard_recipes) == 1

        engine._wildcard_recipes.clear()
        engine.reload()
        assert len(engine._wildcard_recipes) == 1
