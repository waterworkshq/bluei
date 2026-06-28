"""Centralized runtime asset registration with provenance stamping.

Thin wrapper around FixPatternStore.append that centralizes the insertion
path for runtime producers. The pattern extractor calls this instead of
store.append() directly so alpha.4's Recipe Foundry and Rule Hatchery can
route through the same interface.

Scope note (refined from DESIGN during PROMPT writing): register_asset does
NOT write ApprovalRecords on initial mined-pattern insertion, because the
default Governance State for unlisted assets is already ACTIVE (ADR-0008).
Governance routing for promotions (pattern->recipe) stays in
promote_pattern_to_recipe. register_asset's job is: source stamp + insert.
"""

from pathlib import Path

from bluei.engine.pattern_store import FixPattern, FixPatternStore


def register_asset(
    candidate: FixPattern,
    source: str,
    store: FixPatternStore,
) -> str:
    """Register a candidate Pattern with provenance stamping.

    Stamps ``source`` on the candidate, then delegates to ``store.append()``
    which handles structural-hash dedup and merge (incrementing success_count
    for a duplicate rule+before_snippet).

    Returns the persisted ``pattern_id`` (the id assigned by ``append`` via
    ``_prepare_new_pattern``, not the candidate's incoming id).
    """
    candidate.source = source
    return store.append(candidate)
