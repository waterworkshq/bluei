"""Recipe dispatch engine — loads YAML recipes, matches them to findings, and applies fixes.

RecipeEngine indexes recipes by rule name, resolves the best match using a
scoring system, and delegates the actual fix to the appropriate handler.

See docs/RULES_REFERENCE.md#recipes for the complete list of built-in recipes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from bluei.engine.recipe_handlers import HANDLER_REGISTRY, RecipeFixResult
from bluei.engine.recipe_schema import Recipe, load_recipe

logger = logging.getLogger(__name__)


class RecipeEngine:
    """Indexes fix recipes, matches them to findings, and applies the selected handler."""

    def __init__(self, recipe_dirs: Optional[List[Path]] = None):
        """
        Args:
            recipe_dirs: List of directories containing YAML recipe files to load.
        """
        self._recipe_dirs = recipe_dirs or []
        self.recipes: Dict[str, List[Recipe]] = {}
        self._wildcard_recipes: List[Recipe] = []
        self._load_all(self._recipe_dirs)

    def _load_all(self, recipe_dirs: List[Path]) -> None:
        """Scan directories for *.yaml recipes and index each one."""
        for dir_path in recipe_dirs:
            if not dir_path.is_dir():
                continue
            for yaml_file in sorted(dir_path.glob("*.yaml")):
                try:
                    recipe = load_recipe(yaml_file)
                    self._index_recipe(recipe)
                except Exception as exc:
                    logger.warning("recipe_engine: failed to load %s: %s", yaml_file, exc)

    def _index_recipe(self, recipe: Recipe) -> None:
        """Register a recipe under its rule key or wildcard prefix."""
        rule = recipe.rule
        if rule.endswith("*"):
            self._wildcard_recipes.append(recipe)
            prefix = rule.rstrip("*")
            bucket = self.recipes.setdefault(prefix, [])
            bucket.append(recipe)
        else:
            bucket = self.recipes.setdefault(rule, [])
            bucket.append(recipe)

    def match(self, rule: str, language: str = "python", **context) -> Optional[Recipe]:
        """Find the best-scoring recipe for a given rule and language.

        Args:
            rule: Linter rule name to match against.
            language: Source file language for language-aware filtering.
            **context: Additional context (e.g. file_text) for context_guard checks.

        Returns:
            The highest-scoring Recipe, or None if nothing matches.
        """
        candidates = []

        for recipe in self.recipes.get(rule, []):
            score = self._score_recipe(recipe, rule, language, context)
            if score > 0:
                candidates.append((score, recipe))

        for recipe in self._wildcard_recipes:
            prefix = recipe.rule.rstrip("*")
            if rule.startswith(prefix):
                score = self._score_recipe(recipe, rule, language, context)
                if score > 0:
                    candidates.append((score, recipe))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _score_recipe(self, recipe: Recipe, rule: str, language: str, context: dict) -> float:
        """Score a recipe's relevance to the current finding.

        Returns 0.0 if the recipe is incompatible (wrong language, failed guard, etc.).
        Scoring: base +50 for exact rule match, +30 for wildcard prefix match,
        +100 per priority level.
        """
        score = 0.0

        if recipe.language != "*" and recipe.language != language:
            return 0.0

        score += 10.0

        if recipe.rule == rule:
            score += 50.0
        elif recipe.rule.endswith("*") and rule.startswith(recipe.rule.rstrip("*")):
            score += 30.0
        else:
            return 0.0

        if recipe.match.context_guard:
            excludes = recipe.match.context_guard.get("excludes_pattern")
            if excludes:
                import re
                file_text = context.get("file_text", "")
                if file_text and re.search(excludes, file_text):
                    return 0.0

        score += recipe.priority * 100

        return score

    def apply(self, recipe: Recipe, file_path: Path, worktree: Path, finding: object = None) -> RecipeFixResult:
        """Dispatch a recipe to its registered handler and return the result.

        Args:
            recipe: The matched Recipe to apply.
            file_path: Path to the target source file.
            worktree: Root of the working tree (for relative path computation).
            finding: Optional finding object providing extra context (e.g. rule code).

        Returns:
            RecipeFixResult indicating success/failure and files changed.
        """
        handler_cls = HANDLER_REGISTRY.get(recipe.replacement.type)
        if not handler_cls:
            return RecipeFixResult(
                success=False,
                error=f"unknown handler type: {recipe.replacement.type}",
                recipe_id=recipe.id,
                method=recipe.replacement.type,
            )
        handler = handler_cls()
        return handler.apply(recipe, file_path, worktree, finding=finding)

    def has_recipe(self, rule: str, language: str = "python") -> bool:
        """Check whether any loaded recipe can handle the given rule and language.

        Args:
            rule: Linter rule name to look up.
            language: Source file language.

        Returns:
            True if at least one matching recipe exists.
        """
        if rule in self.recipes and self.recipes[rule]:
            for r in self.recipes[rule]:
                if r.language == "*" or r.language == language:
                    return True
        for r in self._wildcard_recipes:
            prefix = r.rule.rstrip("*")
            if rule.startswith(prefix) and (r.language == "*" or r.language == language):
                return True
        return False

    @property
    def recipe_count(self) -> int:
        """Total number of loaded recipes across all rule buckets."""
        total = sum(len(v) for v in self.recipes.values())
        return total

    def reload(self) -> None:
        """Clear all indexed recipes and re-scan the configured directories."""
        self.recipes = {}
        self._wildcard_recipes = []
        self._load_all(self._recipe_dirs)


def builtin_recipe_dir() -> Path:
    """Return the path to the built-in recipe directory shipped with this package."""
    return Path(__file__).parent / "recipes" / "built-in"


def staged_recipe_dir() -> Path:
    """Return the path to the staged (user-defined) recipe directory."""
    return Path(__file__).parent / "recipes" / "staged"
