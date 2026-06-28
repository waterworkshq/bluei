"""Generate synthetic code variations per rule family for SPRT calibration.

For each rule family, produces N structurally-diverse variations of the
violation: different variable names, message strings, indentation, nesting,
and edge cases. The calibration harness measures how often a seeded Pattern's
structural hash matches these variations.

Note: structural hashing normalizes variable names, so renamed-only variants
hash identically. Meaningful variance comes from structurally DIFFERENT forms
of the same rule violation. The generator prioritizes structural diversity
over count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

from bluei.engine.structural_hash import compute_structural_hash  # noqa: F401

_FAMILY_GENERATORS: Dict[str, Callable[[str, str], List[str]]] = {}


@dataclass
class VariationSet:
    """A set of synthetic variations for one rule family."""

    rule_family: str
    language: str
    seed_before: str
    variations: List[str]


def generate_variations(
    rule_family: str,
    language: str,
    seed_before: str,
) -> VariationSet:
    """Generate structurally-diverse variations of a canonical violation.

    Uses family-specific generators where available, falling back to a
    generic rename/indent perturbation. Returns at least one variation
    (fewer if the seed is too short to perturb meaningfully).
    """
    generator = _FAMILY_GENERATORS.get(rule_family, _generic_variations)
    variations = generator(seed_before, language)
    return VariationSet(
        rule_family=rule_family,
        language=language,
        seed_before=seed_before,
        variations=variations,
    )


def _generic_variations(seed: str, language: str) -> List[str]:
    """Fallback: produce renamed + reindented variants.

    These hash identically to the seed (structural normalization), so they
    contribute to the 'all-match' degenerate signal. Family-specific
    generators should override for meaningful variance.
    """
    variations = [seed]
    renames = [
        ("ValueError", "CustomError"),
        ("do_something", "process"),
        ("oops", "failed"),
        ("data", "payload"),
    ]
    for old, new in renames:
        if old in seed:
            variations.append(seed.replace(old, new))
    if "\n" in seed:
        variations.append(
            "\n".join("    " + ln if ln.strip() else ln for ln in seed.splitlines())
        )
    return variations
