"""Data models and YAML loader for deterministic fix recipes.

Defines the schema (dataclasses) for recipes and a loader that parses
YAML recipe files into Recipe instances used by the engine and handlers.
"""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RecipeMatch:
    """Defines how a recipe locates its target in source files.

    Attributes:
        type: Match strategy (e.g. "rule_exact", "regex", "prefix").
        pattern: Regex or literal pattern to search for.
        rule: Linter rule to match exactly.
        prefix: File-path prefix filter for narrowing scope.
        scope: Granularity of the match ("line" or "file").
        context_guard: Dict with "excludes_pattern" to skip files matching a regex.
    """
    type: str
    pattern: Optional[str] = None
    rule: Optional[str] = None
    prefix: Optional[str] = None
    scope: str = "line"
    context_guard: Optional[Dict[str, Any]] = None


@dataclass
class RecipeReplacement:
    """Defines the fix action a recipe applies once matched.

    Attributes:
        type: Handler type — "text", "regex_substitute", "command", or "scaffold".
        value: Literal search string for text-based replacements.
        command: Shell command argv list for command-type fixes.
        template_file: Path to a Jinja-style template (scaffold handler).
        pattern: Regex pattern for substitution handlers.
        replacement: Replacement string (literal or regex backref).
        prepend_pattern: Regex whose match triggers a prepend before the main substitution.
        prepend_template: Text to prepend when prepend_pattern matches.
        condition: Guard string — presence/absence controls whether the fix runs.
        count: Max number of substitutions to perform.
    """
    type: str
    value: Optional[str] = None
    command: Optional[List[str]] = None
    template_file: Optional[str] = None
    pattern: Optional[str] = None
    replacement: Optional[str] = None
    prepend_pattern: Optional[str] = None
    prepend_template: Optional[str] = None
    condition: Optional[str] = None
    count: int = 1


@dataclass
class RecipeValidation:
    """Controls post-fix validation behaviour.

    Attributes:
        run_baseline: Run the baseline check suite after applying the fix.
        run_target: Run the target check suite after applying the fix.
    """
    run_baseline: bool = True
    run_target: bool = True


@dataclass
class Recipe:
    """Top-level recipe definition tying a linter rule to a fix strategy.

    Attributes:
        id: Unique recipe identifier (e.g. "unused-import-rm").
        rule: Linter rule name or wildcard prefix (e.g. "F401*").
        language: Target language filter ("python", "go", etc.) or "*" for all.
        safety: Safety classification — "needs_validation", "safe", or "dangerous".
        description: Human-readable summary of what the recipe does.
        match: Match configuration describing how to locate the issue.
        replacement: Replacement configuration describing the fix action.
        validation: Post-fix validation toggles.
        metadata: Arbitrary key-value metadata consumed by specific handlers.
        priority: Numeric priority; higher values win during recipe selection.
    """
    id: str
    rule: str
    language: str = "*"
    safety: str = "needs_validation"
    description: str = ""
    match: RecipeMatch = field(default_factory=lambda: RecipeMatch(type="rule_exact"))
    replacement: RecipeReplacement = field(default_factory=lambda: RecipeReplacement(type="text"))
    validation: RecipeValidation = field(default_factory=RecipeValidation)
    metadata: Dict[str, Any] = field(default_factory=dict)
    priority: int = 1


def load_recipe(path: Path) -> Recipe:
    """Parse a YAML recipe file into a validated Recipe instance.

    Args:
        path: Path to the .yaml recipe file.

    Returns:
        Fully-populated Recipe with nested Match, Replacement, and Validation.

    Raises:
        ValueError: If the file is not a YAML mapping or lacks required fields.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Recipe file {path} does not contain a YAML mapping")

    required = ("id", "rule")
    for key in required:
        if key not in raw:
            raise ValueError(f"Recipe file {path} missing required field: {key}")

    match_raw = raw.get("match", {})
    match = RecipeMatch(
        type=match_raw.get("type", "rule_exact"),
        pattern=match_raw.get("pattern"),
        rule=match_raw.get("rule"),
        prefix=match_raw.get("prefix"),
        scope=match_raw.get("scope", "line"),
        context_guard=match_raw.get("context_guard"),
    )

    repl_raw = raw.get("replacement", {})
    replacement = RecipeReplacement(
        type=repl_raw.get("type", "text"),
        value=repl_raw.get("value"),
        command=repl_raw.get("command"),
        template_file=repl_raw.get("template_file"),
        pattern=repl_raw.get("pattern"),
        replacement=repl_raw.get("replacement"),
        prepend_pattern=repl_raw.get("prepend_pattern"),
        prepend_template=repl_raw.get("prepend_template"),
        condition=repl_raw.get("condition"),
        count=repl_raw.get("count", 1),
    )

    val_raw = raw.get("validation", {})
    validation = RecipeValidation(
        run_baseline=val_raw.get("run_baseline", True),
        run_target=val_raw.get("run_target", True),
    )

    return Recipe(
        id=raw["id"],
        rule=raw["rule"],
        language=raw.get("language", "*"),
        safety=raw.get("safety", "needs_validation"),
        description=raw.get("description", ""),
        match=match,
        replacement=replacement,
        validation=validation,
        metadata=raw.get("metadata", {}),
        priority=raw.get("priority", 1),
    )
