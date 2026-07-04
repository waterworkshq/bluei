"""bluei.tools.benchmark.runner — the Benchmark Harness core.

Replays the synthetic Seed Library corpus through the cascade-simulation
proxy (F3) and the Model Governor, producing a per-rule-family coverage gap
analysis + Flywheel Score ($ avoided per Finding via mocked rates).

Static analysis only — NO model invocation (AC-P2-6), NO subprocess. The
Benchmark is a build-time dev tool that drives the Deterministic Flywheel
improvement loop: gaps identify where to add Patterns/Recipes/Bundles.

Slice 2 of alpha.6 "Economics". See DESIGN § "Benchmark result schema" +
§ "FlywheelScore" + ARCHITECTURE § "Benchmark Harness (dev tool)".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from bluei.engine.corpus_manifest import CorpusEntry, load_corpus_manifest
from bluei.engine.model_governor import (
    CoveragePolicy,
    ModelTier,
    RuleFamilyCoverage,
    compute_coverage,
    select_tier,
)
from bluei.engine.models import now_iso
from bluei.engine.rule_family import derive_rule_family

# Fixed token estimates (mirrors pr_cycle.py:900-901). Pinned constants for
# the FlywheelScore formula (F10); not model ids — consistent with ADR-0022.
_IN_TOK = 3000
_OUT_TOK = 300


# ─── Resolver interface (tier → model, mockable) ────────────────────────


@dataclass
class ResolvedModel:
    """A tier resolved to a concrete invocation.

    Populated by discovery (mocked in alpha.6). beta.1: real CLI discovery.
    """

    tier: ModelTier
    backend: str  # "claude" | "opencode" | "deterministic"
    model_id: str  # from discovery, NOT hardcoded in the Governor
    input_per_1k: float
    output_per_1k: float
    cli_template: Optional[str] = None


class MockModelDiscovery:
    """Deterministic discovery for tests/benchmark.

    Returns a fixed tier→model mapping. PRD AC-P1-7: pinned for
    reproducibility. The ``model_id`` strings here are test fixture data
    (ADR-0022's no-hardcoding rule applies to the Governor module, not
    benchmark fixtures).
    """

    def __init__(self, mapping: Dict[ModelTier, ResolvedModel]) -> None:
        self._mapping = dict(mapping)

    def discover(self, backend: str) -> List[ResolvedModel]:
        return list(self._mapping.values())


def default_mock_discovery() -> MockModelDiscovery:
    """Pinned deterministic fixture for the ``bluei benchmark`` CLI.

    Three tiers at distinct cost bands (cheap / mid / frontier). Rates are
    per-1k-token USD; reproducibility audit recorded in ``FlywheelScore``.
    """
    return MockModelDiscovery(
        {
            ModelTier.TIER_0: ResolvedModel(
                ModelTier.TIER_0,
                "deterministic",
                "mock-tier-0",
                0.00025,
                0.001,
            ),
            ModelTier.TIER_1: ResolvedModel(
                ModelTier.TIER_1,
                "claude",
                "mock-tier-1",
                0.003,
                0.012,
            ),
            ModelTier.TIER_2: ResolvedModel(
                ModelTier.TIER_2,
                "claude",
                "mock-tier-2",
                0.015,
                0.075,
            ),
        }
    )


def resolve_model(
    discovery: MockModelDiscovery, tier: ModelTier
) -> Optional[ResolvedModel]:
    """Pick the discovered model matching the requested tier."""
    for model in discovery.discover("benchmark"):
        if model.tier == tier:
            return model
    return None


# ─── Result schema ─────────────────────────────────────────────────────


@dataclass
class FamilyCoverageGap:
    """Per-family coverage gap row (DESIGN § "FamilyCoverageGap")."""

    rule_family: str
    findings_in_corpus: int
    deterministic_resolved: int
    governor_reached: int
    tier_distribution: Dict[str, int]
    is_gap: bool


@dataclass
class FlywheelScore:
    """PRD AC-P2-4. $ avoided per Finding under new-vs-old routing.

    Uses the explicit two-rate formula (F10 fix):
    ``routing_savings = (tier2.in - tierN.in) * 3000/1000 +
    (tier2.out - tierN.out) * 300/1000``.
    """

    per_finding_usd: float
    total_usd: float
    deterministic_savings_usd: float
    routing_savings_usd: float
    old_tier_distribution: Dict[str, int]
    new_tier_distribution: Dict[str, int]
    mocked_rates: Dict[str, Dict[str, float]]


@dataclass
class BenchmarkResult:
    """Top-level Benchmark output (DESIGN § "BenchmarkResult")."""

    corpus_size: int
    family_gaps: List[FamilyCoverageGap]
    tier_distribution: Dict[str, int]
    flywheel_score: FlywheelScore
    policy_version: str
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── Per-finding result row (dev/debug; not in the DESIGN top-level schema) ──


@dataclass
class FindingBenchmark:
    """One corpus entry's benchmark outcome (intermediate; not serialized
    in the top-level ``BenchmarkResult``)."""

    asset_id: str
    asset_class: str
    rule_family: str
    cascade_matched: bool
    recommended_tier: str
    coverage: RuleFamilyCoverage
    rationale: str


# ─── Core computation ──────────────────────────────────────────────────


def _empty_tier_distribution() -> Dict[str, int]:
    return {tier.value: 0 for tier in ModelTier}


def _cascade_families(
    manifest: List[CorpusEntry],
) -> Dict[str, int]:
    """Pre-compute the count of Pattern+Recipe assets per family.

    Used by the F3 cascade-simulation proxy: ``cascade_matched`` iff a
    Pattern OR Recipe (``asset_class != "bundle"``) with the same family
    exists **elsewhere** in the manifest (excluding the entry itself when
    the entry is a Pattern/Recipe — avoids the vacuous self-match).
    """
    counts: Dict[str, int] = {}
    for entry in manifest:
        if entry.asset_class != "bundle":
            counts[entry.rule_family] = counts.get(entry.rule_family, 0) + 1
    return counts


def _compute_flywheel_score(
    findings: List[FindingBenchmark],
    discovery: MockModelDiscovery,
    new_tier_distribution: Dict[str, int],
    corpus_size: int,
) -> FlywheelScore:
    """Compute the FlywheelScore via the explicit two-rate formula (F10).

    * ``deterministic_savings``: cascade-resolved findings contribute the
      FULL tier-2 invocation cost avoided (the finding never reaches an LLM).
    * ``routing_savings``: Governor-downgraded findings (cascade miss,
      routed below tier-2) contribute the (tier-2 − recommended) delta.
    """
    tier2 = resolve_model(discovery, ModelTier.TIER_2)
    if tier2 is None:
        raise ValueError("MockModelDiscovery must include tier-2 rates")

    full_tier2_cost = (
        tier2.input_per_1k * _IN_TOK / 1000 + tier2.output_per_1k * _OUT_TOK / 1000
    )

    deterministic_savings = 0.0
    routing_savings = 0.0
    for fb in findings:
        if fb.cascade_matched:
            # The cascade resolved this finding — full frontier cost avoided.
            deterministic_savings += full_tier2_cost
            continue
        # Governor routing: only non-cascade findings reach the Governor.
        if fb.recommended_tier == ModelTier.TIER_2.value:
            continue  # no downgrade, no savings
        if fb.recommended_tier == ModelTier.ESCALATE.value:
            continue  # escalated — no LLM cost, but no "savings" vs tier-2
        tier_n = resolve_model(discovery, ModelTier(fb.recommended_tier))
        if tier_n is None:
            continue
        routing_savings += (
            tier2.input_per_1k - tier_n.input_per_1k
        ) * _IN_TOK / 1000 + (
            tier2.output_per_1k - tier_n.output_per_1k
        ) * _OUT_TOK / 1000

    total = deterministic_savings + routing_savings
    per_finding = total / corpus_size if corpus_size else 0.0
    old_dist = {tier.value: 0 for tier in ModelTier}
    old_dist[ModelTier.TIER_2.value] = corpus_size
    mocked_rates = {
        m.tier.value: {
            "input_per_1k": m.input_per_1k,
            "output_per_1k": m.output_per_1k,
        }
        for m in discovery.discover("benchmark")
    }
    return FlywheelScore(
        per_finding_usd=per_finding,
        total_usd=total,
        deterministic_savings_usd=deterministic_savings,
        routing_savings_usd=routing_savings,
        old_tier_distribution=old_dist,
        new_tier_distribution=dict(new_tier_distribution),
        mocked_rates=mocked_rates,
    )


def _aggregate_gaps(
    finding_results: List[FindingBenchmark],
) -> List[FamilyCoverageGap]:
    """Aggregate per-finding results into per-family gap rows.

    Exposed for direct unit testing of the gap logic (AC-P2-3): callers can
    construct synthetic ``FindingBenchmark`` lists to verify ``is_gap`` under
    controlled tier distributions.
    """
    by_family: Dict[str, List[FindingBenchmark]] = {}
    for fb in finding_results:
        by_family.setdefault(fb.rule_family, []).append(fb)

    family_gaps: List[FamilyCoverageGap] = []
    for family, fbs in by_family.items():
        total = len(fbs)
        det = sum(1 for fb in fbs if fb.cascade_matched)
        gov = total - det
        fam_tier_dist = _empty_tier_distribution()
        for fb in fbs:
            fam_tier_dist[fb.recommended_tier] += 1
        # Gap: no deterministic resolution AND everything routed to tier-2.
        is_gap = det == 0 and fam_tier_dist[ModelTier.TIER_2.value] == total
        family_gaps.append(
            FamilyCoverageGap(
                rule_family=family,
                findings_in_corpus=total,
                deterministic_resolved=det,
                governor_reached=gov,
                tier_distribution=fam_tier_dist,
                is_gap=is_gap,
            )
        )
    # Sort by gap severity (tier-2 count desc), then family for determinism.
    family_gaps.sort(
        key=lambda g: (
            -g.tier_distribution[ModelTier.TIER_2.value],
            g.rule_family,
        )
    )
    return family_gaps


def run_benchmark(
    policy: CoveragePolicy,
    discovery: MockModelDiscovery,
) -> BenchmarkResult:
    """Run the Benchmark Harness over the full committed corpus.

    Static analysis only — no subprocess (AC-P2-6), no model invocation.
    Deterministic output (AC-P2-5): two runs over the same corpus + same
    ``policy`` + same ``discovery`` produce byte-identical JSON (modulo
    ``generated_at``, which the reproducibility test mocks via ``now_iso``).

    Args:
        policy: the ``CoveragePolicy`` thresholds mapping coverage → tier.
        discovery: pinned ``MockModelDiscovery`` providing tier rates.

    Returns:
        ``BenchmarkResult`` with family gaps + tier distribution +
        FlywheelScore.
    """
    manifest = load_corpus_manifest()
    pr_counts = _cascade_families(manifest)

    # Full asset lists for compute_coverage. CorpusEntry exposes both
    # .rule_family (patterns/bundles) and .rule (recipes) so it satisfies
    # the protocols compute_coverage expects without adapter objects.
    patterns = [e for e in manifest if e.asset_class == "pattern"]
    recipes = [e for e in manifest if e.asset_class == "recipe"]
    bundles = [e for e in manifest if e.asset_class == "bundle"]

    finding_results: List[FindingBenchmark] = []

    for entry in manifest:
        coverage = compute_coverage(
            entry.finding,
            entry.rule_family,
            pattern_store=patterns,
            recipes=recipes,
            bundles=bundles,
        )
        # F3 cascade-simulation proxy (Phase 6 spec review):
        # cascade_matched iff a Pattern OR Recipe (asset_class != "bundle")
        # with the same family exists ELSEWHERE in the manifest. For a
        # Pattern/Recipe entry, exclude self (the vacuous self-match); for
        # a Bundle entry, all patterns/recipes are "elsewhere".
        total_pr = pr_counts.get(entry.rule_family, 0)
        if entry.asset_class != "bundle":
            coverage.cascade_matched = (total_pr - 1) > 0
        else:
            coverage.cascade_matched = total_pr > 0

        rec = select_tier(entry.finding, coverage, policy)
        fb = FindingBenchmark(
            asset_id=entry.asset_id,
            asset_class=entry.asset_class,
            rule_family=entry.rule_family,
            cascade_matched=coverage.cascade_matched,
            recommended_tier=rec.tier.value,
            coverage=coverage,
            rationale=rec.rationale,
        )
        finding_results.append(fb)

    # Aggregate: per-family gap rows + whole-corpus tier distribution.
    tier_dist = _empty_tier_distribution()
    for fb in finding_results:
        tier_dist[fb.recommended_tier] += 1

    family_gaps = _aggregate_gaps(finding_results)

    flywheel = _compute_flywheel_score(
        finding_results, discovery, tier_dist, len(manifest)
    )

    return BenchmarkResult(
        corpus_size=len(manifest),
        family_gaps=family_gaps,
        tier_distribution=tier_dist,
        flywheel_score=flywheel,
        policy_version="alpha.6",
        generated_at=now_iso(),
    )
