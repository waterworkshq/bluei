"""Exercise the actual command recipe boundary with real subprocess output."""

import json
import shutil
import sys
from types import SimpleNamespace

import pytest

from bluei.engine.recipe_engine import RecipeEngine, builtin_recipe_dir
from bluei.engine.recipe_handlers import CommandHandler
from bluei.engine.recipe_schema import parse_recipe


def test_failed_command_is_not_success_even_after_writing_target(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("original = True\n")
    recipe = parse_recipe(json.dumps({
        "id": "failing-command", "rule": "example", "language": "python",
        "safety": "needs_validation", "match": {"type": "rule_exact", "rule": "example"},
        "replacement": {"type": "command", "command": [sys.executable, "-c",
            "from pathlib import Path; Path('target.py').write_text('changed = True\\n'); raise SystemExit(7)"]},
    }))
    result = CommandHandler().apply(recipe, target, tmp_path)
    assert result.success is False
    assert "rc=7" in result.error


@pytest.mark.parametrize("rule,safe", [("ruff-F401,F821", True), ("ruff-", True), ("ruff-F821", False)])
def test_invalid_or_nonfixable_rule_never_invokes_command(tmp_path, rule, safe):
    target = tmp_path / "target.py"
    target.write_text("import os\n")
    marker = tmp_path / "launched"
    recipe = parse_recipe(json.dumps({
        "id": "guarded-command", "rule": "ruff-*", "language": "python",
        "safety": "needs_validation", "match": {"type": "rule_prefix", "prefix": "ruff-"},
        "replacement": {"type": "command", "command": [sys.executable, "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()", "{rule_code}"]},
    }))
    result = CommandHandler().apply(recipe, target, tmp_path,
                                    finding=SimpleNamespace(rule=rule, safe_to_autofix=safe))
    assert result.success is False
    assert not marker.exists()
    assert target.read_text() == "import os\n"


def test_shipped_ruff_recipe_only_fixes_selected_safe_rule(tmp_path):
    if shutil.which("ruff") is None:
        pytest.skip("ruff is not installed")
    target = tmp_path / "target.py"
    target.write_text("import os\n\nprint(missing_name)\n")
    engine = RecipeEngine([builtin_recipe_dir()])
    finding = SimpleNamespace(rule="ruff-F401", safe_to_autofix=True)
    recipe = engine.match(finding.rule, "python")
    assert recipe is not None
    result = engine.apply(recipe, target, tmp_path, finding=finding)
    assert result.success is True
    assert "import os" not in target.read_text()
    assert "print(missing_name)" in target.read_text()
    assert "--unsafe-fixes" not in recipe.replacement.command
    assert "--no-unsafe-fixes" in recipe.replacement.command
