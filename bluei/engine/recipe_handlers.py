"""Handler implementations for recipe transformation types.

Each handler corresponds to a replacement.type value in a recipe YAML
(e.g. "text", "regex_substitute", "command", "scaffold") and applies the
appropriate fix to the target file within a sandbox worktree.
"""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from bluei.engine.recipe_schema import Recipe


@dataclass
class RecipeFixResult:
    """Outcome of applying a recipe fix to a file.

    Attributes:
        success: Whether the fix was applied without error.
        files_changed: List of file paths (relative to worktree) that were modified.
        validation_passed: Whether post-fix validation succeeded (default True).
        error: Error message if success is False; None otherwise.
        recipe_id: ID of the recipe that produced this result.
        method: Handler type that was used (e.g. "text", "command").
    """
    success: bool
    files_changed: List[str] = field(default_factory=list)
    validation_passed: bool = True
    error: Optional[str] = None
    recipe_id: str = ""
    method: str = ""


class RecipeHandler(ABC):
    """Abstract base for all recipe fix handlers."""

    @abstractmethod
    def apply(self, recipe: Recipe, file_path: Path, worktree: Path, finding: object = None) -> RecipeFixResult: ...


class TextHandler(RecipeHandler):
    """Applies literal text substitution or regex-based single-line replacement."""

    def apply(self, recipe: Recipe, file_path: Path, worktree: Path, finding: object = None) -> RecipeFixResult:
        """Replace a literal string or regex match in the target file.

        If recipe.replacement.condition is set and found in the file text, the
        fix is skipped (guard condition met).

        Args:
            recipe: Recipe with replacement config (value/pattern + replacement).
            file_path: Path to the source file to modify.
            worktree: Root of the sandbox worktree.
            finding: Unused; kept for handler interface consistency.

        Returns:
            RecipeFixResult with success=True if text was changed.
        """
        if not file_path.exists():
            return RecipeFixResult(success=False, error=f"file not found: {file_path}", recipe_id=recipe.id, method="text")

        text = file_path.read_text(encoding="utf-8")
        original = text
        search = recipe.replacement.value or ""
        replace_with = recipe.replacement.replacement or ""

        if recipe.replacement.pattern:
            guard = recipe.replacement.condition or ""
            if guard and guard in text:
                return RecipeFixResult(success=False, error=f"guard condition met, skipping: {guard}", recipe_id=recipe.id, method="text")
            text = re.sub(recipe.replacement.pattern, replace_with, text, count=1)
        else:
            guard = recipe.replacement.condition or ""
            if guard and guard in text:
                return RecipeFixResult(success=False, error=f"guard condition met, skipping: {guard}", recipe_id=recipe.id, method="text")
            if search and search in text:
                text = text.replace(search, replace_with, 1)

        if text == original:
            return RecipeFixResult(success=False, error="no changes made", recipe_id=recipe.id, method="text")

        file_path.write_text(text, encoding="utf-8")
        return RecipeFixResult(success=True, files_changed=[str(file_path.relative_to(worktree))], recipe_id=recipe.id, method="text")


class RegexSubstituteHandler(RecipeHandler):
    """Applies regex substitution with optional prepend and flag support."""

    def apply(self, recipe: Recipe, file_path: Path, worktree: Path, finding: object = None) -> RecipeFixResult:
        """Perform regex substitution on the target file content.

        Supports DOTALL/MULTILINE flags via recipe metadata keys
        "regex_dotall" and "regex_multiline". A condition string acts as a
        positive guard — the fix only runs if the condition text is present.

        Args:
            recipe: Recipe with replacement.pattern, .replacement, and optional .prepend_*.
            file_path: Path to the source file to modify.
            worktree: Root of the sandbox worktree.
            finding: Unused; kept for handler interface consistency.

        Returns:
            RecipeFixResult with success=True if text was changed.
        """
        if not file_path.exists():
            return RecipeFixResult(success=False, error=f"file not found: {file_path}", recipe_id=recipe.id, method="regex_substitute")

        text = file_path.read_text(encoding="utf-8")
        original = text

        pattern = recipe.replacement.pattern
        replacement = recipe.replacement.replacement or ""
        if not pattern:
            return RecipeFixResult(success=False, error="no regex pattern specified", recipe_id=recipe.id, method="regex_substitute")

        guard = recipe.replacement.condition or ""
        if guard and guard not in text:
            return RecipeFixResult(success=False, error=f"guard condition not met: {guard}", recipe_id=recipe.id, method="regex_substitute")

        flags = 0
        if recipe.metadata.get("regex_dotall"):
            flags |= re.DOTALL
        if recipe.metadata.get("regex_multiline"):
            flags |= re.MULTILINE

        text = re.sub(pattern, replacement, text, count=recipe.replacement.count, flags=flags)

        if recipe.replacement.prepend_pattern and recipe.replacement.prepend_template:
            text = re.sub(
                recipe.replacement.prepend_pattern,
                recipe.replacement.prepend_template,
                text,
                count=1,
            )

        if text == original:
            return RecipeFixResult(success=False, error="no changes made", recipe_id=recipe.id, method="regex_substitute")

        file_path.write_text(text, encoding="utf-8")
        return RecipeFixResult(success=True, files_changed=[str(file_path.relative_to(worktree))], recipe_id=recipe.id, method="regex_substitute")


class CommandHandler(RecipeHandler):
    """Runs an external command against the target file and verifies it changed."""

    def apply(self, recipe: Recipe, file_path: Path, worktree: Path, finding: object = None) -> RecipeFixResult:
        """Execute the command from recipe.replacement.command in the worktree.

        Template placeholders ``{file}`` and ``{rule_code}`` are expanded in
        each argument.  The handler compares file content before/after to
        determine success.

        Args:
            recipe: Recipe with replacement.command argv list.
            file_path: Path to the source file (passed as {file} placeholder).
            worktree: Working directory for the subprocess.
            finding: Optional finding object; used to extract ruff rule codes.

        Returns:
            RecipeFixResult with success=True if the file content changed.
        """
        if not recipe.replacement.command:
            return RecipeFixResult(success=False, error="no command specified", recipe_id=recipe.id, method="command")

        content_before = file_path.read_text(encoding="utf-8") if file_path.exists() else ""

        rule_code = ""
        if finding and hasattr(finding, "rule") and finding.rule.startswith("ruff-"):
            rule_code = finding.rule.replace("ruff-", "", 1)

        cmd = [
            arg.replace("{file}", str(file_path)).replace("{rule_code}", rule_code)
            for arg in recipe.replacement.command
        ]

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(worktree),
            )
        except FileNotFoundError:
            return RecipeFixResult(success=False, error=f"command not found: {cmd[0]}", recipe_id=recipe.id, method="command")
        except subprocess.TimeoutExpired:
            return RecipeFixResult(success=False, error=f"command timed out: {cmd[0]}", recipe_id=recipe.id, method="command")

        content_after = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
        file_changed = content_before != content_after

        if not file_changed:
            return RecipeFixResult(success=False, error="command ran but no file changes", recipe_id=recipe.id, method="command")

        return RecipeFixResult(
            success=True,
            files_changed=[str(file_path.relative_to(worktree))],
            recipe_id=recipe.id,
            method="command",
        )


HANDLER_REGISTRY = {
    "text": TextHandler,
    "regex_substitute": RegexSubstituteHandler,
    "command": CommandHandler,
    "scaffold": lambda: ScaffoldHandler(),
}
"""Maps replacement.type strings to handler classes for dispatch."""


class ScaffoldHandler(RecipeHandler):
    """Generates new files (tests, docs) via the scaffold engine."""

    def apply(self, recipe: Recipe, file_path: Path, worktree: Path, finding: object = None) -> RecipeFixResult:
        """Delegate to ScaffoldEngine based on recipe.metadata["scaffold_type"].

        Supported scaffold types: "test_file" and "doc_section".

        Args:
            recipe: Recipe with metadata.scaffold_type and language/section_type config.
            file_path: Path to the source file that triggered the finding.
            worktree: Root of the sandbox worktree.
            finding: Unused; kept for handler interface consistency.

        Returns:
            RecipeFixResult with success=True and the generated file path.
        """
        from bluei.engine.scaffold.engine import ScaffoldEngine

        engine = ScaffoldEngine()
        scaffold_type = (recipe.metadata or {}).get("scaffold_type", "test_file")

        if scaffold_type == "test_file":
            language = (recipe.metadata or {}).get("language", "python")
            result = engine.generate_test_file(file_path, worktree, language=language)
        elif scaffold_type == "doc_section":
            section_type = (recipe.metadata or {}).get("section_type", "rollback")
            result = engine.generate_doc_section(file_path, section_type, worktree)
        else:
            return RecipeFixResult(
                success=False,
                error=f"unknown scaffold type: {scaffold_type}",
                recipe_id=recipe.id,
                method="scaffold",
            )

        if not result.success:
            return RecipeFixResult(
                success=False,
                error=result.error or "scaffold generation failed",
                recipe_id=recipe.id,
                method="scaffold",
            )

        files_changed = [str(result.output_path.relative_to(worktree))] if result.output_path else []
        return RecipeFixResult(
            success=True,
            files_changed=files_changed,
            recipe_id=recipe.id,
            method="scaffold",
        )
