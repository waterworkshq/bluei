"""AC-P2-6: every detection-only seeded Pattern rule has a matching Recipe."""

from __future__ import annotations

from pathlib import Path

from bluei.engine.jsonl import read_jsonl
from bluei.engine.recipe_engine import (
    RecipeEngine,
    builtin_recipe_dir,
    staged_recipe_dir,
)


def test_every_seeded_pattern_has_a_recipe() -> None:
    engine = RecipeEngine([builtin_recipe_dir(), staged_recipe_dir()])
    seeded_dir = (
        Path(__file__).resolve().parent.parent / "bluei" / "engine" / "seeded_patterns"
    )
    rules: set[str] = set()
    for jsonl_path in seeded_dir.glob("*.jsonl"):
        for record in read_jsonl(jsonl_path):
            if isinstance(record, dict) and "rule" in record:
                rules.add(record["rule"])
    assert rules, "no seeded patterns found"
    missing = [r for r in sorted(rules) if not engine.has_recipe(r)]
    assert missing == [], f"Rules without Recipes: {missing}"
