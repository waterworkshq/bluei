"""Recipe proposer — drives LLM emission + validation for the Foundry.

Mirrors tools/seed/synthesizer.synthesize_rule: injectable llm_callback. Steps:
build prompt -> call llm_callback -> extract fenced YAML -> strict parse via
parse_recipe -> strip unknown top-level keys -> run detector oracle -> return
RecipeProposal or None.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

import yaml

from bluei.engine.bundle_loader import GoldenBundle
from bluei.engine.pattern_store import FixPattern
from bluei.engine.recipe_schema import (
    Recipe,
    RecipeMatch,
    RecipeReplacement,
    RecipeValidation,
    parse_recipe,
)
from bluei.tools.foundry.models import RecipeProposal
from bluei.tools.foundry.oracle import _run_detector_oracle
from bluei.tools.foundry.prompt import _build_prompt


_KNOWN_REPLACEMENT_TYPES = {"text", "regex_substitute", "command", "scaffold"}
_KNOWN_MATCH_TYPES = {"rule_exact", "regex", "prefix", "rule_prefix"}


def _extract_fenced_yaml(response: str) -> Optional[str]:
    """Extract the first fenced ```yaml``` block, else the whole response."""
    match = re.search(r"```(?:yaml|yml)?\s*\n(.*?)\n```", response, re.DOTALL)
    if match:
        return match.group(1)
    stripped = response.strip()
    if stripped.startswith("id:") or "id:" in stripped.split("\n", 1)[0]:
        return stripped
    return None


def _strip_unknown_keys(yaml_text: str) -> str:
    """Drop unknown top-level keys; re-emit clean YAML.

    Keeps only the Recipe dataclass top-level fields; ``metadata`` survives as
    a Dict. Returns the original text if the input is not parseable as a
    mapping (the caller's parse step surfaces the error).
    """
    allowed = {
        "id",
        "rule",
        "language",
        "safety",
        "description",
        "match",
        "replacement",
        "validation",
        "metadata",
        "priority",
    }
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return yaml_text
    if not isinstance(raw, dict):
        return yaml_text
    cleaned = {k: v for k, v in raw.items() if k in allowed}
    return yaml.safe_dump(cleaned, sort_keys=False, default_flow_style=False)


def propose_recipe(
    pattern: FixPattern,
    bundle: Optional[GoldenBundle],
    llm_callback: Callable[[str], str],
    *,
    ruff_executable: str = "ruff",
    eslint_executable: str = "eslint",
) -> Optional[RecipeProposal]:
    """Propose a Recipe from a seeded Pattern via an LLM.

    Returns ``None`` if the LLM emits malformed YAML or the parse rejects.
    Returns a ``RecipeProposal`` with ``detector_passed=False`` (and a reason)
    when the parse succeeds but the detector oracle fails.
    """
    prompt = _build_prompt(pattern, [bundle] if bundle else [])
    response = llm_callback(prompt)

    raw_yaml = _extract_fenced_yaml(response)
    if raw_yaml is None:
        return None

    try:
        cleaned_yaml = _strip_unknown_keys(raw_yaml)
        parsed: Recipe = parse_recipe(cleaned_yaml)
    except (ValueError, yaml.YAMLError):
        return None

    if parsed.replacement.type not in _KNOWN_REPLACEMENT_TYPES:
        return None
    if parsed.match.type not in _KNOWN_MATCH_TYPES:
        return None

    passed, reason = _run_detector_oracle(
        pattern.rule,
        pattern.before_snippet,
        pattern.after_snippet,
        pattern.language,
        ruff_executable=ruff_executable,
        eslint_executable=eslint_executable,
    )

    return RecipeProposal(
        recipe_yaml=cleaned_yaml,
        parsed=parsed,
        source_pattern_id=pattern.pattern_id,
        source_bundle_id=bundle.id if bundle else None,
        detector_passed=passed,
        rejection_reason=None if passed else reason,
    )
