"""Tests for bluei.engine.model_governor (Slice 0 of alpha.6 "Economics").

Covers T0.2–T0.7: data models, compute_coverage, select_tier, identity_selection,
record_recommendation, and the AC-P1-3 no-vendor-model-id assertion.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List

import pytest

from bluei.engine.model_governor import (
    CoveragePolicy,
    ModelTier,
    RuleFamilyCoverage,
    SelectionFn,
    TierRecommendation,
    compute_coverage,
    identity_selection,
    record_recommendation,
    select_tier,
)
from bluei.engine.rule_family import derive_rule_family


# --- lightweight duck-typed fixtures for compute_coverage inputs ---


@dataclass
class _FakePattern:
    rule_family: str = ""


@dataclass
class _FakeRecipe:
    rule: str = ""


@dataclass
class _FakeBundle:
    rule_family: str = ""


class _FakeStore:
    """Mimics FixPatternStore.load_active() without touching the filesystem."""

    def __init__(self, patterns: List[_FakePattern]) -> None:
        self._patterns = patterns

    def load_active(self) -> List[_FakePattern]:
        return list(self._patterns)


# Twelve representative rule families (mirrors derive_rule_family output space).
KNOWN_FAMILIES = [
    "ruff-b",
    "ruff-c",
    "ruff-f",
    "ruff-e",
    "eslint-no-unused",
    "eslint-no-undef",
    "pylint-c",
    "pylint-w",
    "mypy-attr",
    "bandit-b",
    "gosec-g",
    "clippy-needless",
]


# --------------------------------------------------------------------------
# T0.1 — ModelTier serialization
# --------------------------------------------------------------------------


class TestModelTier:
    def test_tier_0_value(self):
        assert ModelTier.TIER_0.value == "tier-0"

    def test_all_values(self):
        assert [m.value for m in ModelTier] == [
            "tier-0",
            "tier-1",
            "tier-2",
            "escalate",
        ]

    def test_str_enum_json_serializes(self):
        # str, Enum → .value is a plain str; json.dumps needs no default=
        assert json.dumps(ModelTier.TIER_0.value) == '"tier-0"'
        assert json.dumps(ModelTier.ESCALATE.value) == '"escalate"'

    def test_value_lookup(self):
        assert ModelTier("tier-1") is ModelTier.TIER_1
        assert ModelTier("tier-2") is ModelTier.TIER_2


# --------------------------------------------------------------------------
# T0.2 — data models
# --------------------------------------------------------------------------


class TestRuleFamilyCoverage:
    def test_total_assets_sums(self):
        c = RuleFamilyCoverage(
            rule_family="ruff-b", pattern_count=2, recipe_count=1, bundle_count=3
        )
        assert c.total_assets == 6

    def test_defaults(self):
        c = RuleFamilyCoverage(rule_family="x")
        assert c.total_assets == 0
        assert c.cascade_matched is False
        assert c.pattern_count == 0
        assert c.recipe_count == 0
        assert c.bundle_count == 0

    def test_to_dict_roundtrip(self):
        c = RuleFamilyCoverage(
            rule_family="ruff-b",
            pattern_count=2,
            recipe_count=1,
            bundle_count=0,
            cascade_matched=True,
        )
        assert c.to_dict() == {
            "rule_family": "ruff-b",
            "pattern_count": 2,
            "recipe_count": 1,
            "bundle_count": 0,
            "cascade_matched": True,
        }


class TestCoveragePolicy:
    def test_defaults(self):
        p = CoveragePolicy()
        assert p.cascade_matched_tier is ModelTier.TIER_0
        assert p.tier_0_min_assets == 3
        assert p.tier_1_min_assets == 1
        assert p.escalate_on_zero_coverage is False


class TestTierRecommendation:
    def test_to_dict(self):
        coverage = RuleFamilyCoverage(rule_family="ruff-b", pattern_count=1)
        rec = TierRecommendation(
            tier=ModelTier.TIER_1, rationale="r", coverage=coverage
        )
        d = rec.to_dict()
        assert d["tier"] == "tier-1"
        assert d["rationale"] == "r"
        assert d["coverage"]["rule_family"] == "ruff-b"
        assert d["policy_version"] == "alpha.6"

    def test_policy_version_default(self):
        rec = TierRecommendation(
            tier=ModelTier.TIER_2,
            rationale="x",
            coverage=RuleFamilyCoverage(rule_family="f"),
        )
        assert rec.policy_version == "alpha.6"


# --------------------------------------------------------------------------
# T0.4 — select_tier
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patterns,recipes,bundles,cascade,expected",
    [
        (0, 0, 0, False, ModelTier.TIER_2),  # zero coverage → the gap
        (1, 0, 0, False, ModelTier.TIER_1),
        (0, 1, 0, False, ModelTier.TIER_1),
        (0, 0, 1, False, ModelTier.TIER_1),
        (2, 0, 0, False, ModelTier.TIER_1),  # 1-2 band
        (3, 0, 0, False, ModelTier.TIER_0),  # 3+ band
        (0, 0, 3, False, ModelTier.TIER_0),
        (1, 1, 1, False, ModelTier.TIER_0),  # total 3
        (5, 0, 0, False, ModelTier.TIER_0),
        (0, 0, 0, True, ModelTier.TIER_0),  # cascade matched wins
        (5, 0, 0, True, ModelTier.TIER_0),  # cascade wins even with assets
    ],
)
def test_select_tier_coverage_mapping(
    make_finding, patterns, recipes, bundles, cascade, expected
):
    finding = make_finding()
    coverage = RuleFamilyCoverage(
        rule_family="ruff-b",
        pattern_count=patterns,
        recipe_count=recipes,
        bundle_count=bundles,
        cascade_matched=cascade,
    )
    rec = select_tier(finding, coverage)
    assert rec.tier == expected
    assert rec.rationale  # non-empty
    assert "ruff-b" in rec.rationale


def test_select_tier_default_policy_is_inert_gap(make_finding):
    finding = make_finding()
    coverage = RuleFamilyCoverage(rule_family="ruff-b")  # 0 assets, no cascade
    rec = select_tier(finding, coverage)
    assert rec.tier is ModelTier.TIER_2


def test_select_tier_escalate_on_zero_coverage(make_finding):
    finding = make_finding()
    coverage = RuleFamilyCoverage(rule_family="ruff-b")
    policy = CoveragePolicy(escalate_on_zero_coverage=True)
    rec = select_tier(finding, coverage, policy)
    assert rec.tier is ModelTier.ESCALATE
    assert "escalates on zero coverage" in rec.rationale


def test_select_tier_custom_thresholds(make_finding):
    finding = make_finding()
    # Raise tier-0 bar to 10 assets: 3 assets should now be tier-1.
    coverage = RuleFamilyCoverage(rule_family="ruff-b", pattern_count=3)
    rec = select_tier(finding, coverage, CoveragePolicy(tier_0_min_assets=10))
    assert rec.tier is ModelTier.TIER_1


@pytest.mark.parametrize("family", KNOWN_FAMILIES)
def test_select_tier_works_for_all_known_families(make_finding, family):
    finding = make_finding(rule=family + "-x900")
    coverage = RuleFamilyCoverage(rule_family=family, pattern_count=2)
    rec = select_tier(finding, coverage)
    assert rec.tier is ModelTier.TIER_1
    assert family in rec.rationale


# --------------------------------------------------------------------------
# T0.5 — identity_selection
# --------------------------------------------------------------------------


def test_identity_selection_returns_tier_2(make_finding):
    finding = make_finding()
    coverage = RuleFamilyCoverage(rule_family="ruff-b", pattern_count=5)
    rec = identity_selection(finding, coverage)
    assert rec.tier is ModelTier.TIER_2
    assert rec.rationale == "identity-default (alpha.6 inert; beta.1 swaps)"
    assert rec.coverage is coverage


def test_identity_selection_ignores_coverage_level(make_finding):
    finding = make_finding()
    # Even with zero assets, identity stays tier-2 (not escalate, not tier-0).
    coverage = RuleFamilyCoverage(rule_family="ruff-b")
    rec = identity_selection(finding, coverage)
    assert rec.tier is ModelTier.TIER_2


# --------------------------------------------------------------------------
# T0.3 — compute_coverage
# --------------------------------------------------------------------------


def test_compute_coverage_pattern_store_only():
    from bluei.engine.models import Finding as _F

    f = _F(
        finding_id="x",
        repo="r",
        path="p",
        line=1,
        rule="ruff-b904",
        snippet="s",
        confidence=0.5,
        quick_win=False,
        safe_to_autofix=False,
    )
    store = _FakeStore(
        [_FakePattern("ruff-b"), _FakePattern("ruff-b"), _FakePattern("ruff-f")]
    )
    coverage = compute_coverage(f, "ruff-b", pattern_store=store)
    assert coverage.pattern_count == 2
    assert coverage.recipe_count == 0
    assert coverage.bundle_count == 0
    assert coverage.cascade_matched is False
    assert coverage.rule_family == "ruff-b"
    assert coverage.total_assets == 2


def test_compute_coverage_plain_list_store(make_finding):
    finding = make_finding()
    patterns = [_FakePattern("ruff-b"), _FakePattern(""), _FakePattern("ruff-f")]
    coverage = compute_coverage(finding, "ruff-b", pattern_store=patterns)
    assert coverage.pattern_count == 1  # unseeded "" never matches


def test_compute_coverage_with_recipes(make_finding):
    finding = make_finding()
    recipes = [
        _FakeRecipe("ruff-b904"),  # derive_rule_family → "ruff-b"
        _FakeRecipe("ruff-f401"),  # → "ruff-f"
        _FakeRecipe("ruff-b007"),  # → "ruff-b"
    ]
    coverage = compute_coverage(finding, "ruff-b", recipes=recipes)
    assert coverage.recipe_count == 2


def test_compute_coverage_recipe_uses_derive_rule_family(make_finding):
    finding = make_finding()
    # Confirm the recipe path actually runs through derive_rule_family.
    recipes = [_FakeRecipe("eslint-no-unused-vars")]  # → "eslint-no-unused"
    coverage = compute_coverage(finding, "eslint-no-unused", recipes=recipes)
    assert coverage.recipe_count == 1
    assert derive_rule_family("eslint-no-unused-vars") == "eslint-no-unused"


def test_compute_coverage_bundles_objects_and_dicts(make_finding):
    finding = make_finding()
    bundles = [
        _FakeBundle("ruff-b"),
        {"rule_family": "ruff-b"},
        {"rule_family": "ruff-f"},
        {"other": "x"},  # no rule_family key → skipped
    ]
    coverage = compute_coverage(finding, "ruff-b", bundles=bundles)
    assert coverage.bundle_count == 2


def test_compute_coverage_all_none(make_finding):
    finding = make_finding()
    coverage = compute_coverage(finding, "ruff-b")
    assert coverage.total_assets == 0
    assert coverage.pattern_count == 0
    assert coverage.recipe_count == 0
    assert coverage.bundle_count == 0


def test_compute_coverage_full(make_finding):
    finding = make_finding()
    store = _FakeStore(
        [_FakePattern("ruff-b"), _FakePattern("ruff-b"), _FakePattern("ruff-f")]
    )
    recipes = [_FakeRecipe("ruff-b904")]
    bundles = [{"rule_family": "ruff-b"}]
    coverage = compute_coverage(finding, "ruff-b", store, recipes, bundles)
    assert (coverage.pattern_count, coverage.recipe_count, coverage.bundle_count) == (
        2,
        1,
        1,
    )
    assert coverage.total_assets == 4


def test_compute_coverage_unseeded_pattern_does_not_match(make_finding):
    finding = make_finding()
    # Patterns with default rule_family="" must never count for a real family.
    store = _FakeStore([_FakePattern(""), _FakePattern("")])
    coverage = compute_coverage(finding, "ruff-b", pattern_store=store)
    assert coverage.pattern_count == 0


# --------------------------------------------------------------------------
# T0.6 — record_recommendation
# --------------------------------------------------------------------------


def test_record_recommendation_appends_row(tmp_path, make_finding):
    finding = make_finding(finding_id="f-abc", rule="ruff-b904")
    coverage = RuleFamilyCoverage(
        rule_family="ruff-b", pattern_count=3, recipe_count=0, bundle_count=0
    )
    rec = TierRecommendation(tier=ModelTier.TIER_0, rationale="boom", coverage=coverage)
    ledger = tmp_path / "governor_recommendations.jsonl"

    record_recommendation(finding, rec, ledger, "run-001")

    assert ledger.exists()
    rows = [
        json.loads(line) for line in ledger.read_text().splitlines() if line.strip()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "run-001"
    assert row["finding_id"] == "f-abc"
    assert row["rule"] == "ruff-b904"
    assert row["rule_family"] == "ruff-b"
    assert row["tier"] == "tier-0"
    assert row["rationale"] == "boom"
    assert row["coverage"] == {
        "rule_family": "ruff-b",
        "pattern_count": 3,
        "recipe_count": 0,
        "bundle_count": 0,
        "cascade_matched": False,
    }
    assert row["policy_version"] == "alpha.6"
    assert "timestamp" in row and row["timestamp"]


def test_record_recommendation_creates_parent_dirs(tmp_path, make_finding):
    finding = make_finding()
    rec = identity_selection(finding, RuleFamilyCoverage(rule_family="ruff-b"))
    ledger = tmp_path / "nested" / "deep" / "governor_recommendations.jsonl"
    record_recommendation(finding, rec, ledger, "run-1")
    assert ledger.exists()


def test_record_recommendation_sort_keys_determinism(tmp_path, make_finding):
    finding = make_finding(finding_id="f1", rule="ruff-b904")
    rec = identity_selection(finding, RuleFamilyCoverage(rule_family="ruff-b"))
    ledger = tmp_path / "governor_recommendations.jsonl"

    record_recommendation(finding, rec, ledger, "run-1")
    record_recommendation(finding, rec, ledger, "run-2")

    lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    keys1 = list(json.loads(lines[0]).keys())
    keys2 = list(json.loads(lines[1]).keys())
    # Same key order across rows (sort_keys=True) and ascending.
    assert keys1 == keys2
    assert keys1 == sorted(keys1)
    # run_ids differ; the key order does not.
    assert json.loads(lines[0])["run_id"] == "run-1"
    assert json.loads(lines[1])["run_id"] == "run-2"


# --------------------------------------------------------------------------
# T0.7 — SelectionFn alias + AC-P1-3 no-vendor-model-id assertion
# --------------------------------------------------------------------------


def test_selection_fn_alias_is_callable_type():
    # SelectionFn must accept identity_selection structurally.
    fn: SelectionFn = identity_selection
    assert callable(fn)
    from bluei.engine.models import Finding as _F

    f = _F(
        finding_id="x",
        repo="r",
        path="p",
        line=1,
        rule="r",
        snippet="s",
        confidence=0.5,
        quick_win=False,
        safe_to_autofix=False,
    )
    out = fn(f, RuleFamilyCoverage(rule_family="r"))
    assert isinstance(out, TierRecommendation)


def test_no_vendor_model_id_literals():
    """AC-P1-3: the Governor module must contain no vendor model-id strings.

    Per ADR-0022 the module reasons over tier ordinals only; model ids are
    discovered at invocation, never hardcoded. The only strings here should be
    tier ordinals + rationale text + import paths.
    """
    import bluei.engine.model_governor as mg

    source = inspect.getsource(mg).lower()
    for forbidden in ("claude", "gpt", "sonnet", "opus", "haiku"):
        assert forbidden not in source, (
            f"model_governor source contains forbidden model-id literal: {forbidden!r}"
        )
