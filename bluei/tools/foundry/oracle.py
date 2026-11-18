"""Detector oracle for the Foundry (ADR-0020).

Reuses tools/seed/validator.validate_candidate — the linter is the ground-truth
oracle (ADR-0018). Constructs a ParsedRule from the Pattern's before/after and
verifies the before triggers the rule and the after is clean.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Tuple

from bluei.engine.recipe_engine import RecipeEngine
from bluei.engine.recipe_schema import Recipe
from bluei.engine.validation import run_named_checks

# Independent clean fixtures, not rejected repair attempts or the expected output.
_B904_NEGATIVES = (
    'def parse(raw):\n    try:\n        return int(raw)\n    except ValueError as exc:\n        raise RuntimeError("invalid") from exc\n',
    'def parse(raw):\n    try:\n        return int(raw)\n    except ValueError:\n        raise\n',
)


def qualify_retained_recipe(
    recipe: Recipe, yaml_text: str, evidence: dict, repo_path: Path,
    checks: list, *, ruff_executable: str = "ruff",
) -> bool:
    """Apply the authored literal repair, then qualify its actual bytes."""
    if (
        recipe.rule != "ruff-b904" or recipe.language != "python"
        or recipe.match.type != "rule_exact" or recipe.match.rule != recipe.rule
        or recipe.match.context_guard or recipe.safety not in {"needs_validation", "idempotent"}
        or recipe.replacement.type != "text" or not recipe.replacement.value
        or not recipe.replacement.replacement or recipe.replacement.pattern
        or recipe.replacement.condition or not recipe.validation.run_baseline
        or not recipe.validation.run_target or not checks
    ):
        return False
    before, after = evidence["before"], evidence["after"]
    if before in _B904_NEGATIVES or after in _B904_NEGATIVES:
        return False

    def detector(path, expected):
        result = subprocess.run(
            [ruff_executable, "check", "--isolated", "--no-cache", "--select", "B904",
             "--output-format", "json", str(path)],
            cwd=path.parent, capture_output=True, check=False, timeout=30,
        )
        payload = json.loads(result.stdout)
        if result.stderr or not isinstance(payload, list):
            return False
        if not expected:
            return result.returncode == 0 and payload == []
        return result.returncode == 1 and len(payload) == 1 and payload[0].get("code") == "B904"

    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Use the pinned repository tree so configured checks see their normal fixtures.
            checkout = root / "checkout"
            subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", "--local", str(repo_path), str(checkout)],
                capture_output=True, check=True, timeout=30,
            )
            subprocess.run(
                ["git", "-c", "advice.detachedHead=false", "checkout", "--detach", evidence["base"]],
                cwd=checkout, capture_output=True, check=True, timeout=30,
            )
            recipes = root / "recipes"
            recipes.mkdir()
            (recipes / "proposal.yaml").write_text(yaml_text, encoding="utf-8")
            engine = RecipeEngine([recipes])
            target = checkout / evidence["path"]
            if not target.resolve().is_relative_to(checkout) or target.read_bytes() != before.encode():
                return False
            selected = engine.match(recipe.rule, recipe.language, file_text=before)
            if selected != recipe or not detector(target, True):
                return False
            if not engine.apply(selected, target, checkout).success:
                return False
            if target.read_bytes() != after.encode() or not detector(target, False):
                return False
            results = run_named_checks(checkout, {str(i): c for i, c in enumerate(checks)},
                                       root / "validation.log", "foundry")
            if not all(r["available"] and r["rc"] == 0 for r in results.values()):
                return False
            if target.read_bytes() != after.encode():
                return False
            for text in (after, *_B904_NEGATIVES):
                fixture = root / "clean.py"
                fixture.write_text(text, encoding="utf-8")
                if not detector(fixture, False) or engine.apply(selected, fixture, root).success:
                    return False
                if fixture.read_bytes() != text.encode():
                    return False
            return True
    except (OSError, ValueError, AttributeError, TypeError, subprocess.SubprocessError):
        return False

from bluei.tools.seed.models import ParsedRule
from bluei.tools.seed.validator import validate_candidate


def _linter_for_rule(rule: str) -> str:
    """Infer the source linter from the rule prefix."""
    if rule.startswith("ruff"):
        return "ruff"
    if rule.startswith("eslint"):
        return "eslint"
    return "ruff"


def _run_detector_oracle(
    rule: str,
    before: str,
    after: str,
    language: str,
    *,
    ruff_executable: str = "ruff",
    eslint_executable: str = "eslint",
) -> Tuple[bool, str]:
    """Run the detector oracle on a Pattern's before/after pair.

    Returns ``(passed, reason)`` — passed is True only when the before
    triggers the rule AND the after is clean.
    """
    source_linter = _linter_for_rule(rule)
    candidate = ParsedRule(
        rule=rule,
        language=language,
        before=before,
        after=after,
        detector_before="",
        detector_after="",
        negative_examples=[],
        has_autofix=False,
        source_linter=source_linter,
        validation_command=f"{source_linter} check --select {rule}"
        if source_linter == "ruff"
        else f"eslint --rule {rule}",
        description="Foundry oracle candidate",
    )
    result = validate_candidate(
        candidate,
        ruff_executable=ruff_executable,
        eslint_executable=eslint_executable,
    )
    if result.valid:
        return True, ""
    return False, result.reason or "detector oracle failed"
