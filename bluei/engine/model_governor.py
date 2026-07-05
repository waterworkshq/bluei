"""bluei.engine.model_governor — the Model Governor (selection-function foundation).

The Model Governor recommends which Routing Ladder tier should handle a Finding
that the deterministic cascade could not resolve. See ADR-0022 (locus +
inert default) and the CONTEXT.md terms "Model Governor" and "Routing Ladder".

Per ADR-0022 the Governor:

  * emits only a tier ORDINAL (``tier-0`` / ``tier-1`` / ``tier-2`` /
    ``escalate``) — it never resolves a tier to a concrete model id.
    Tier→model resolution is the backend's job at invocation time, via model
    discovery. The module therefore contains no vendor model-id literals.
  * ships with an IDENTITY default (``identity_selection`` returns ``tier-2``,
    the current-model tier) so alpha.6 records a recommendation per Finding
    without changing any live fix behavior. beta.1 swaps
    ``identity_selection`` → ``select_tier`` as a policy flip, not a code edit.

This is Slice 0 of the v0.2.0-alpha.6 "Economics" release. The live-path
wiring (Slice 1) and the Benchmark Harness (Slice 2) build on this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from bluei.engine.jsonl import append_jsonl
from bluei.engine.models import Finding, now_iso
from bluei.engine.rule_family import derive_rule_family

if TYPE_CHECKING:  # pragma: no cover — forward refs only, avoids import cycle
    from bluei.engine.model_discovery import ModelDiscovery, ResolvedModel


class ModelTier(str, Enum):
    """The Routing Ladder rungs (CONTEXT.md "Routing Ladder").

    ``str, Enum`` so ``.value`` serializes directly to JSONL without a custom
    ``default=`` callable (mirrors ``IssueStatus`` in engine/models.py).
    Distinct from ``FixTier`` (engine/fix_tiers.py — validation-rigor tiers
    T0–T3); the two enums are unrelated despite the similar name.
    """

    TIER_0 = "tier-0"  # cheapest, lowest capability
    TIER_1 = "tier-1"  # strong, mid-cost
    TIER_2 = "tier-2"  # frontier, highest capability and cost
    ESCALATE = "escalate"  # no LLM tier should handle; route to Escalation (human)


@dataclass
class RuleFamilyCoverage:
    """Deterministic coverage for a Finding's rule family.

    The sole primary signal for tier selection (PRD AC-P1-2). Counts seeded
    assets (Patterns, Recipes, Golden Bundles) sharing the Finding's rule
    family, plus whether the cascade already matched this Finding.

    Partial availability (F2 fix): at the live pr-cycle call site only
    ``pattern_store`` is in scope; ``recipes`` and ``bundles`` are NOT loaded
    there. Under the identity default the coverage values are unused (returns
    ``tier-2`` regardless), so the live path passes a partial snapshot
    (pattern_count from the store; recipe/bundle counts default 0). The
    Benchmark passes all three (full coverage). None inputs contribute 0.
    """

    rule_family: str
    pattern_count: int = 0
    recipe_count: int = 0
    bundle_count: int = 0
    cascade_matched: bool = False

    @property
    def total_assets(self) -> int:
        return self.pattern_count + self.recipe_count + self.bundle_count

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CoveragePolicy:
    """Maps coverage → tier. Tunable thresholds.

    Defaults are absolute asset-count bands (0 / 1-2 / 3+), chosen so the 12
    known rule families distribute across tiers; tunable. The Benchmark makes
    the threshold effect visible (PRD: "documented defaults").
    """

    # A Finding the cascade already matched deterministically never reaches the
    # Governor. If one does (defensive), route to the cheapest tier.
    cascade_matched_tier: ModelTier = ModelTier.TIER_0
    # Per-family asset-count bands → tier. 0 assets = tier-2 (the gap),
    # 1-2 = tier-1, 3+ = tier-0. These are documented defaults, not empirical.
    tier_0_min_assets: int = 3
    tier_1_min_assets: int = 1
    # When no asset exists AND the cascade didn't match → escalate?
    escalate_on_zero_coverage: bool = False  # default: route to tier-2, not escalate


@dataclass
class TierRecommendation:
    """The Governor's per-Finding output. Recorded to the recommendation ledger."""

    tier: ModelTier
    rationale: str
    coverage: RuleFamilyCoverage
    policy_version: str = "beta.1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.value,
            "rationale": self.rationale,
            "coverage": self.coverage.to_dict(),
            "policy_version": self.policy_version,
        }


# The callable the LLM call site accepts (PRD AC-P1-5). Threaded through the
# cycle context (Slice 1) so beta.1 can swap identity → select_tier via config.
SelectionFn = Callable[[Finding, RuleFamilyCoverage], TierRecommendation]


def compute_coverage(
    finding: Finding,
    rule_family: str,
    pattern_store: Optional[Any] = None,
    recipes: Optional[List[Any]] = None,
    bundles: Optional[List[Any]] = None,
) -> RuleFamilyCoverage:
    """Count seeded assets for ``rule_family``. All inputs optional (F2 fix).

    Callers pass what is in scope. Live path: ``pattern_store`` only (partial).
    Benchmark: all three. ``None`` inputs contribute 0 to their count.

    ``cascade_matched`` is NOT computed here — it is the cascade's per-Finding
    result, not an asset count. This function returns it as ``False``; callers
    that know the cascade outcome set ``coverage.cascade_matched = True`` after
    construction.

    Args:
        finding: the Finding being routed (carried for identity/ledger use; not
            itself counted).
        rule_family: pre-derived family key (``derive_rule_family(finding.rule)``).
        pattern_store: a ``FixPatternStore`` (uses ``load_active()``) OR an
            iterable of objects with a ``rule_family`` attribute. ``None`` → 0.
        recipes: iterable of objects with a ``.rule`` attribute (Recipes lack a
            stored ``rule_family``, so it is derived via ``derive_rule_family``).
            ``None`` → 0.
        bundles: iterable of objects with a ``rule_family`` attribute or dicts
            with a ``"rule_family"`` key. ``None`` → 0.
    """
    pattern_count = 0
    if pattern_store is not None:
        if hasattr(pattern_store, "load_active"):
            patterns = pattern_store.load_active()
        else:
            patterns = pattern_store
        pattern_count = sum(
            1 for _p in patterns if getattr(_p, "rule_family", "") == rule_family
        )

    recipe_count = 0
    if recipes is not None:
        recipe_count = sum(
            1
            for _r in recipes
            if derive_rule_family(getattr(_r, "rule", "")) == rule_family
        )

    bundle_count = 0
    if bundles is not None:
        for _b in bundles:
            rf = getattr(_b, "rule_family", None)
            if rf is None and isinstance(_b, dict):
                rf = _b.get("rule_family")
            if rf == rule_family:
                bundle_count += 1

    return RuleFamilyCoverage(
        rule_family=rule_family,
        pattern_count=pattern_count,
        recipe_count=recipe_count,
        bundle_count=bundle_count,
        cascade_matched=False,
    )


def _coverage_rationale(coverage: RuleFamilyCoverage) -> str:
    return (
        f"rule family '{coverage.rule_family}' has {coverage.total_assets} seeded assets "
        f"(patterns={coverage.pattern_count}, recipes={coverage.recipe_count}, "
        f"bundles={coverage.bundle_count})"
    )


def select_tier(
    finding: Finding,
    coverage: RuleFamilyCoverage,
    policy: CoveragePolicy = CoveragePolicy(),
) -> TierRecommendation:
    """The Governor's coverage-based selection function (PRD AC-P1-1, AC-P1-2).

    Coverage-only primary signal:

      * ``cascade_matched`` → ``policy.cascade_matched_tier`` (tier-0 by
        default; defensive — a matched Finding shouldn't reach the Governor).
      * ``total_assets >= tier_0_min_assets`` → tier-0.
      * ``total_assets >= tier_1_min_assets`` → tier-1.
      * ``total_assets == 0``:
          - ``escalate_on_zero_coverage`` → ESCALATE.
          - else → tier-2 (the gap; flagged by the Benchmark).

    NOT the alpha.6 default — the live call site uses ``identity_selection``.
    The Benchmark calls this directly on the corpus. The ``policy`` argument is
    read-only here, so the shared default instance is safe.
    """
    if coverage.cascade_matched:
        tier = policy.cascade_matched_tier
        rationale = (
            f"rule family '{coverage.rule_family}' cascade-matched deterministically"
        )
    elif coverage.total_assets >= policy.tier_0_min_assets:
        tier = ModelTier.TIER_0
        rationale = _coverage_rationale(coverage)
    elif coverage.total_assets >= policy.tier_1_min_assets:
        tier = ModelTier.TIER_1
        rationale = _coverage_rationale(coverage)
    elif coverage.total_assets == 0 and policy.escalate_on_zero_coverage:
        tier = ModelTier.ESCALATE
        rationale = (
            f"rule family '{coverage.rule_family}' has no seeded assets "
            f"and policy escalates on zero coverage"
        )
    else:
        # Below the tier-1 threshold: either zero assets when not escalating,
        # or a nonzero count in the gap (0, tier_1_min) under a custom policy.
        # Route to tier-2 (the coverage gap). Total over all non-negative
        # integers for any tier_0_min >= tier_1_min >= 1.
        tier = ModelTier.TIER_2
        rationale = _coverage_rationale(coverage)

    return TierRecommendation(tier=tier, rationale=rationale, coverage=coverage)


def identity_selection(
    finding: Finding, coverage: RuleFamilyCoverage
) -> TierRecommendation:
    """The explicit opt-out fallback (ADR-0022 inert default).

    Returns ``tier-2`` (the current-model / frontier tier) with an
    'identity-default' rationale. The call site invokes the current model
    unchanged — no behavior change. beta.1 flipped the cycle default to
    ``select_tier`` (real recommendations); this function remains for
    operators who explicitly want the inert posture (set ``ctx.selection_fn
    = identity_selection``) and for tests that exercise it directly.
    """
    return TierRecommendation(
        tier=ModelTier.TIER_2,
        rationale="identity-default (alpha.6 inert; beta.1 swaps)",
        coverage=coverage,
    )


def record_recommendation(
    finding: Finding,
    rec: TierRecommendation,
    ledger_path: Path,
    run_id: str,
) -> None:
    """Append a recommendation row to the Governor ledger (JSONL).

    Mirrors ``dry_replay``'s row shape (``run_id``, ``finding_id``, ``rule``,
    outcome, ``timestamp``). ``sort_keys=True`` for deterministic output.

    Deviation from the DESIGN row *function signature*: the row itself is
    unchanged, but this function takes the ``Finding`` (alongside the
    recommendation) because ``TierRecommendation`` does not carry
    ``finding_id`` / ``rule``. The Finding supplies identity; the
    recommendation supplies the decision. (T0.6 note — option (b) chosen.)
    """
    row = {
        "run_id": run_id,
        "finding_id": finding.finding_id,
        "rule": finding.rule,
        "rule_family": rec.coverage.rule_family,
        "tier": rec.tier.value,
        "rationale": rec.rationale,
        "coverage": rec.coverage.to_dict(),
        "policy_version": rec.policy_version,
        "timestamp": now_iso(),
    }
    append_jsonl(Path(ledger_path), row, sort_keys=True)


# M4: cost-ledger rate anchor (estimate-only, never an invocation id). Kept
# verbatim from alpha.6's hardcoded ``model_name = "claude-sonnet-4"`` so
# ``cost_tracker.record_invocation`` → ``MODEL_RATES.get(model)`` resolves to
# the SAME rate alpha.6 used under empty operator config (C2 identity). This is
# NOT a vendor invocation id — it never enters the command template; it feeds
# the legacy cost estimator only. See DESIGN §5/M4, ADR-0022 amendment 1.
DEFAULT_ESTIMATE_LABEL = "claude-sonnet-4"


def resolve_governed_model(
    finding: Finding,
    selection_fn: SelectionFn,
    pattern_store: Optional[Any],
    discovery: Optional["ModelDiscovery"],
    base_template: str,
    ledger_path: Optional[Path],
    run_id: str,
) -> Tuple[Optional[TierRecommendation], Optional["ResolvedModel"], str, str]:
    """Shared Governor resolution (pr-cycle + batch both call this).

    Returns ``(recommendation, resolved_model, resolved_template,
    model_name_for_ledger)``:

    * ``ledger_path`` None or ``selection_fn`` None → guard not met →
      ``(None, None, base_template, DEFAULT_ESTIMATE_LABEL)`` (mirrors the
      pr_cycle.py:880-883 guard).
    * ``discovery`` None or tier unmapped → ``resolved_model`` None → template
      unchanged (identity), ``model_name == DEFAULT_ESTIMATE_LABEL``.
    * Otherwise the discovered model id is spliced into the template via
      ``inject_model_flag`` and returned as ``model_name``.

    The Governor still records a recommendation for EVERY Finding reaching this
    point (not only those routed to the LLM fix backend) — the recommendation
    ledger is independent of the fix backend. Resolution to a concrete model is
    a separate concern.
    """
    if ledger_path is None or selection_fn is None:
        return None, None, base_template, DEFAULT_ESTIMATE_LABEL
    coverage = compute_coverage(
        finding, derive_rule_family(finding.rule), pattern_store=pattern_store
    )
    rec = selection_fn(finding, coverage)
    record_recommendation(finding, rec, ledger_path, run_id)
    resolved_model = None
    if discovery is not None:
        from bluei.engine.model_discovery import (
            resolve_model,
        )  # lazy; avoid top-level cycle

        resolved_model = resolve_model(discovery, rec.tier)
    from bluei.engine.model_discovery import inject_model_flag

    resolved_template = inject_model_flag(base_template, resolved_model)
    model_name = resolved_model.model_id if resolved_model else DEFAULT_ESTIMATE_LABEL
    return rec, resolved_model, resolved_template, model_name
