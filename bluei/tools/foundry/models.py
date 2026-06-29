"""Dataclass for the Foundry's intermediate proposal (ADR-0020)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bluei.engine.recipe_schema import Recipe


@dataclass
class RecipeProposal:
    """The Foundry's intermediate after LLM emission + before packaging.

    Attributes:
        recipe_yaml: Validated YAML text from the LLM.
        parsed: ``parse_recipe`` result (strict Recipe schema).
        source_pattern_id: Seeded Pattern the proposal was derived from.
        source_bundle_id: Corresponding GoldenBundle id if linked, else None.
        detector_passed: Oracle verdict — before triggers, after clean.
        rejection_reason: Set when ``detector_passed`` is False.
    """

    recipe_yaml: str
    parsed: Recipe
    source_pattern_id: str
    source_bundle_id: Optional[str]
    detector_passed: bool
    rejection_reason: Optional[str] = None
