"""Tests for the Benchmark Harness (Slice 2 of alpha.6 "Economics").

Covers AC-P2-1 through AC-P2-6:
  * AC-P2-1: corpus manifest non-empty + deterministic
  * AC-P2-2: per-finding result row
  * AC-P2-3: gap analysis (bundle-only family → partial; tier-2-only → full gap)
  * AC-P2-4: FlywheelScore exact $ via the explicit two-rate formula (F10)
  * AC-P2-5: reproducibility — two runs byte-identical JSON
  * AC-P2-6: no subprocess invocation during a benchmark run
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.engine.corpus_manifest import CorpusEntry, load_corpus_manifest
from bluei.engine.model_governor import (
    CoveragePolicy,
    ModelTier,
    RuleFamilyCoverage,
)
from bluei.engine.models import Finding
from bluei.tools.benchmark import (
    BenchmarkResult,
    FamilyCoverageGap,
    FindingBenchmark,
    FlywheelScore,
    MockModelDiscovery,
    ResolvedModel,
    default_mock_discovery,
    run_benchmark,
)
from bluei.tools.benchmark.report import render_benchmark_markdown
from bluei.tools.benchmark.runner import _aggregate_gaps, _compute_flywheel_score


# ─── Fixtures ──────────────────────────────────────────────────────────


def _make_finding(rule: str, asset_id: str) -> Finding:
    return Finding(
        finding_id=f"test-{asset_id}",
        repo="test",
        path=f"<synthetic>/{asset_id}",
        line=1,
        rule=rule,
        snippet="x",
        confidence=1.0,
        quick_win=False,
        safe_to_autofix=False,
    )


def _make_entry(
    rule: str, asset_class: str, asset_id: str | None = None
) -> CorpusEntry:
    from bluei.engine.rule_family import derive_rule_family

    if asset_id is None:
        asset_id = f"{asset_class}-{rule}"

    return CorpusEntry(
        finding=_make_finding(rule, asset_id),
        rule_family=derive_rule_family(rule),
        asset_class=asset_class,
        asset_id=asset_id,
        source_path=f"<test>/{asset_id}",
    )


def _mock_discovery_3tier() -> MockModelDiscovery:
    """Deterministic 3-tier mock with distinct rates for formula verification."""
    return MockModelDiscovery(
        {
            ModelTier.TIER_0: ResolvedModel(
                ModelTier.TIER_0, "deterministic", "mock-t0", 0.00025, 0.001
            ),
            ModelTier.TIER_1: ResolvedModel(
                ModelTier.TIER_1, "claude", "mock-t1", 0.003, 0.012
            ),
            ModelTier.TIER_2: ResolvedModel(
                ModelTier.TIER_2, "claude", "mock-t2", 0.015, 0.075
            ),
        }
    )


# ─── AC-P2-1: corpus manifest non-empty + deterministic ────────────────


class TestCorpusManifest:
    def test_manifest_non_empty(self):
        """AC-P2-1: the manifest covers Bundles + Patterns + Recipes."""
        manifest = load_corpus_manifest()
        assert len(manifest) > 0, "corpus manifest must be non-empty"

        classes = {e.asset_class for e in manifest}
        assert "bundle" in classes, "manifest must include Golden Bundles"
        assert "pattern" in classes, "manifest must include seeded Patterns"
        assert "recipe" in classes, "manifest must include Recipes"

    def test_manifest_deterministic(self):
        """AC-P2-1: two loads produce byte-identical manifests."""
        first = load_corpus_manifest()
        second = load_corpus_manifest()
        assert len(first) == len(second)
        for a, b in zip(first, second):
            assert a.asset_id == b.asset_id
            assert a.rule_family == b.rule_family
            assert a.asset_class == b.asset_class
            assert a.finding.finding_id == b.finding.finding_id
            assert a.finding.rule == b.finding.rule

    def test_manifest_sorted_by_asset_id(self):
        """AC-P2-1: manifest is sorted by asset_id for determinism."""
        manifest = load_corpus_manifest()
        ids = [e.asset_id for e in manifest]
        assert ids == sorted(ids)

    def test_corpus_entry_rule_property(self):
        """CorpusEntry.rule delegates to finding.rule (recipe protocol)."""
        entry = _make_entry("ruff-b904", "test-1")
        assert entry.rule == "ruff-b904"
        assert entry.rule_family == "ruff-b"


# ─── AC-P2-2: per-finding result row ──────────────────────────────────


class TestBenchmarkResult:
    def test_run_benchmark_produces_result(self):
        """AC-P2-2: run_benchmark produces a BenchmarkResult with family gaps."""
        result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        assert isinstance(result, BenchmarkResult)
        assert result.corpus_size > 0
        assert len(result.family_gaps) > 0
        assert result.tier_distribution  # non-empty
        assert isinstance(result.flywheel_score, FlywheelScore)

    def test_family_gaps_have_rows(self):
        """AC-P2-2: each family gap row is well-formed."""
        result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        for gap in result.family_gaps:
            assert isinstance(gap, FamilyCoverageGap)
            assert gap.findings_in_corpus > 0
            assert (
                gap.deterministic_resolved + gap.governor_reached
                == gap.findings_in_corpus
            )
            # tier_distribution sums to findings_in_corpus
            total_tier = sum(gap.tier_distribution.values())
            assert total_tier == gap.findings_in_corpus

    def test_tier_distribution_sums_to_corpus(self):
        """AC-P2-2: whole-corpus tier distribution sums to corpus_size."""
        result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        assert sum(result.tier_distribution.values()) == result.corpus_size

    def test_policy_version(self):
        result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        assert result.policy_version == "beta.1"


# ─── AC-P2-3: gap analysis ────────────────────────────────────────────


class TestGapAnalysis:
    def test_bundle_only_family_is_partial_gap(self):
        """AC-P2-3: a family with only bundles has det=0 (partial gap).

        Bundles don't count as cascade coverage (the F3 proxy excludes
        asset_class == "bundle"). So a bundle-only family has zero
        deterministic resolution. The Governor still routes them based on
        bundle_count, so is_gap may be False — the gap is "partial" (no
        deterministic coverage, but Governor coverage exists).
        """
        # Synthetic manifest: 2 bundles for "ruff-b", no patterns/recipes.
        manifest = [
            _make_entry("ruff-b904", "bundle", "bundle-1"),
            _make_entry("ruff-b007", "bundle", "bundle-2"),
        ]
        # Monkeypatch the manifest loader.
        with patch(
            "bluei.tools.benchmark.runner.load_corpus_manifest",
            return_value=manifest,
        ):
            result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())

        gap = next(g for g in result.family_gaps if g.rule_family == "ruff-b")
        assert gap.deterministic_resolved == 0
        assert gap.findings_in_corpus == 2

    def test_full_gap_via_aggregate(self):
        """AC-P2-3: a family where all findings route to tier-2 is a full gap.

        Tested via _aggregate_gaps directly: synthetic FindingBenchmark list
        where a family has zero cascade matches and 100% tier-2 routing.
        """
        fbs = [
            FindingBenchmark(
                asset_id="gap-1",
                asset_class="pattern",
                rule_family="full-gap-family",
                cascade_matched=False,
                recommended_tier=ModelTier.TIER_2.value,
                coverage=RuleFamilyCoverage(rule_family="full-gap-family"),
                rationale="test",
            ),
            FindingBenchmark(
                asset_id="gap-2",
                asset_class="pattern",
                rule_family="full-gap-family",
                cascade_matched=False,
                recommended_tier=ModelTier.TIER_2.value,
                coverage=RuleFamilyCoverage(rule_family="full-gap-family"),
                rationale="test",
            ),
        ]
        gaps = _aggregate_gaps(fbs)
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.rule_family == "full-gap-family"
        assert gap.deterministic_resolved == 0
        assert gap.tier_distribution[ModelTier.TIER_2.value] == 2
        assert gap.is_gap is True

    def test_no_gap_when_cascade_resolved(self):
        """AC-P2-3: a family with cascade matches is NOT a gap."""
        fbs = [
            FindingBenchmark(
                asset_id="ok-1",
                asset_class="pattern",
                rule_family="covered-family",
                cascade_matched=True,
                recommended_tier=ModelTier.TIER_0.value,
                coverage=RuleFamilyCoverage(
                    rule_family="covered-family", cascade_matched=True
                ),
                rationale="test",
            ),
        ]
        gaps = _aggregate_gaps(fbs)
        assert gaps[0].is_gap is False
        assert gaps[0].deterministic_resolved == 1

    def test_f3_proxy_excludes_self_for_patterns(self):
        """The F3 proxy excludes a pattern's own entry from cascade coverage.

        A family with a SINGLE pattern: cascade_matched=False (no other
        pattern/recipe covers it). A family with TWO patterns: both have
        cascade_matched=True (they cover each other).
        """
        # Single pattern — no "elsewhere" coverage.
        manifest = [_make_entry("ruff-b904", "solo-pattern")]
        with patch(
            "bluei.tools.benchmark.runner.load_corpus_manifest",
            return_value=manifest,
        ):
            result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        gap = result.family_gaps[0]
        assert gap.deterministic_resolved == 0  # self excluded

    def test_f3_proxy_two_patterns_mutual_coverage(self):
        """CR-3: a family with TWO patterns → both cascade-match each other.

        The F3 self-exclusion (``pr_counts[family] - 1``) is the load-bearing
        correctness claim: each pattern excludes ITSELF but counts the peer.
        With two patterns in the same family, both have
        ``cascade_matched=True`` → ``deterministic_resolved == 2``. A regression
        that dropped the ``- 1`` would still produce 2 here, but a regression
        that double-excluded (``- 2``) or skipped the mutual case entirely
        would produce 0.
        """
        manifest = [
            _make_entry("ruff-b904", "pattern", "pat-a"),
            _make_entry("ruff-b007", "pattern", "pat-b"),
        ]
        with patch(
            "bluei.tools.benchmark.runner.load_corpus_manifest",
            return_value=manifest,
        ):
            result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        gap = result.family_gaps[0]
        assert gap.rule_family == "ruff-b"
        assert gap.deterministic_resolved == 2  # both match each other

    def test_f3_proxy_bundle_uses_pattern_coverage(self):
        """A bundle with a pattern in the same family: cascade_matched=True."""
        manifest = [
            CorpusEntry(
                finding=_make_finding("ruff-b904", "gb-1"),
                rule_family="ruff-b",
                asset_class="bundle",
                asset_id="gb-1",
                source_path="<test>",
            ),
            CorpusEntry(
                finding=_make_finding("ruff-b904", "pat-1"),
                rule_family="ruff-b",
                asset_class="pattern",
                asset_id="pat-1",
                source_path="<test>",
            ),
        ]
        with patch(
            "bluei.tools.benchmark.runner.load_corpus_manifest",
            return_value=manifest,
        ):
            result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        # The bundle entry should have cascade_matched=True (pattern exists).
        gap = result.family_gaps[0]
        assert gap.rule_family == "ruff-b"
        # 2 findings: 1 cascade-matched (the bundle), 1 not (the pattern, self-excluded)
        assert gap.deterministic_resolved == 1
        assert gap.findings_in_corpus == 2


# ─── AC-P2-4: FlywheelScore exact $ ───────────────────────────────────


class TestFlywheelScore:
    def test_exact_dollars_two_finding_fixture(self):
        """AC-P2-4: FlywheelScore matches the explicit two-rate formula.

        Fixture: 2 findings.
          * Finding A: cascade_matched=True → deterministic savings = full
            tier-2 cost.
          * Finding B: cascade_matched=False, routed tier-2 → tier-1 →
            routing savings = (tier2.in - tier1.in)*3000/1000 +
            (tier2.out - tier1.out)*300/1000.
        """
        discovery = _mock_discovery_3tier()
        tier2 = discovery.discover("benchmark")
        t2 = next(m for m in tier2 if m.tier == ModelTier.TIER_2)
        t1 = next(m for m in tier2 if m.tier == ModelTier.TIER_1)

        # Expected values via the explicit formula.
        in_tok, out_tok = 3000, 300
        expected_det = (
            t2.input_per_1k * in_tok / 1000 + t2.output_per_1k * out_tok / 1000
        )
        expected_routing = (t2.input_per_1k - t1.input_per_1k) * in_tok / 1000 + (
            t2.output_per_1k - t1.output_per_1k
        ) * out_tok / 1000
        expected_total = expected_det + expected_routing
        expected_per = expected_total / 2

        fbs = [
            FindingBenchmark(
                asset_id="a",
                asset_class="pattern",
                rule_family="fam-a",
                cascade_matched=True,
                recommended_tier=ModelTier.TIER_0.value,
                coverage=RuleFamilyCoverage(rule_family="fam-a", cascade_matched=True),
                rationale="cascade",
            ),
            FindingBenchmark(
                asset_id="b",
                asset_class="pattern",
                rule_family="fam-b",
                cascade_matched=False,
                recommended_tier=ModelTier.TIER_1.value,
                coverage=RuleFamilyCoverage(rule_family="fam-b"),
                rationale="governor downgrade",
            ),
        ]
        new_dist = {t.value: 0 for t in ModelTier}
        new_dist[ModelTier.TIER_0.value] = 1
        new_dist[ModelTier.TIER_1.value] = 1

        fs = _compute_flywheel_score(fbs, discovery, new_dist, corpus_size=2)

        assert fs.deterministic_savings_usd == pytest.approx(expected_det)
        assert fs.routing_savings_usd == pytest.approx(expected_routing)
        assert fs.total_usd == pytest.approx(expected_total)
        assert fs.per_finding_usd == pytest.approx(expected_per)

    def test_no_savings_when_all_tier2(self):
        """All findings routed to tier-2 (identity): zero savings."""
        discovery = _mock_discovery_3tier()
        fbs = [
            FindingBenchmark(
                asset_id="x",
                asset_class="pattern",
                rule_family="f",
                cascade_matched=False,
                recommended_tier=ModelTier.TIER_2.value,
                coverage=RuleFamilyCoverage(rule_family="f"),
                rationale="no coverage",
            ),
        ]
        new_dist = {t.value: 0 for t in ModelTier}
        new_dist[ModelTier.TIER_2.value] = 1
        fs = _compute_flywheel_score(fbs, discovery, new_dist, corpus_size=1)
        assert fs.total_usd == 0.0
        assert fs.routing_savings_usd == 0.0
        assert fs.deterministic_savings_usd == 0.0

    def test_old_distribution_always_tier2(self):
        """AC-P2-4: old_tier_distribution is always 100% tier-2 (baseline)."""
        result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        old = result.flywheel_score.old_tier_distribution
        assert old[ModelTier.TIER_2.value] == result.corpus_size
        assert sum(old.values()) == result.corpus_size

    def test_mocked_rates_recorded(self):
        """AC-P2-4: mocked rates are recorded for reproducibility audit."""
        discovery = _mock_discovery_3tier()
        fbs = []
        new_dist = {t.value: 0 for t in ModelTier}
        fs = _compute_flywheel_score(fbs, discovery, new_dist, corpus_size=0)
        assert ModelTier.TIER_2.value in fs.mocked_rates
        assert "input_per_1k" in fs.mocked_rates[ModelTier.TIER_2.value]
        assert "output_per_1k" in fs.mocked_rates[ModelTier.TIER_2.value]

    def test_total_non_negative(self):
        """FlywheelScore total is always >= 0."""
        result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        assert result.flywheel_score.total_usd >= 0


# ─── AC-P2-5: reproducibility ─────────────────────────────────────────


class TestReproducibility:
    def test_two_runs_byte_identical_json(self):
        """AC-P2-5: two benchmark runs produce byte-identical JSON.

        Mocks now_iso so generated_at is stable across runs.
        """
        fixed_ts = "2026-07-04T12:00:00+00:00"
        discovery = _mock_discovery_3tier()

        with patch("bluei.tools.benchmark.runner.now_iso", return_value=fixed_ts):
            r1 = run_benchmark(CoveragePolicy(), discovery)
            r2 = run_benchmark(CoveragePolicy(), discovery)

        j1 = json.dumps(r1.to_dict(), sort_keys=True, indent=2)
        j2 = json.dumps(r2.to_dict(), sort_keys=True, indent=2)
        assert j1 == j2

    def test_mock_discovery_deterministic(self):
        """MockModelDiscovery returns identical results across calls."""
        d = _mock_discovery_3tier()
        first = d.discover("benchmark")
        second = d.discover("benchmark")
        assert len(first) == len(second)
        for a, b in zip(first, second):
            assert a.tier == b.tier
            assert a.input_per_1k == b.input_per_1k
            assert a.output_per_1k == b.output_per_1k


# ─── AC-P2-6: no subprocess ───────────────────────────────────────────


class TestNoSubprocess:
    def test_no_subprocess_called(self):
        """AC-P2-6: benchmark never invokes subprocess."""
        with (
            patch("subprocess.run") as mock_run,
            patch("subprocess.Popen") as mock_popen,
            patch("subprocess.call") as mock_call,
        ):
            run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
            mock_call.assert_not_called()


# ─── Report renderer ──────────────────────────────────────────────────


class TestReport:
    def test_report_renders_markdown(self):
        """The markdown report contains expected sections."""
        with patch(
            "bluei.tools.benchmark.runner.now_iso",
            return_value="2026-07-04T12:00:00+00:00",
        ):
            result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        md = render_benchmark_markdown(result)
        assert "# Benchmark Report" in md
        assert "Tier Distribution" in md
        assert "Per-Family Coverage Gaps" in md
        assert "Flywheel Score" in md
        assert "Mocked rates" in md

    def test_report_handles_empty_corpus(self):
        """The report renders even with an empty corpus."""
        with patch(
            "bluei.tools.benchmark.runner.load_corpus_manifest",
            return_value=[],
        ):
            result = run_benchmark(CoveragePolicy(), _mock_discovery_3tier())
        md = render_benchmark_markdown(result)
        assert "# Benchmark Report" in md
        assert result.corpus_size == 0


# ─── CLI ──────────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_help(self, capsys):
        from bin.cmd_benchmark import _cmd_benchmark

        rc = _cmd_benchmark(["--help"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "benchmark" in captured.out.lower()

    def test_cli_runs(self, capsys, tmp_path):
        from bin.cmd_benchmark import _cmd_benchmark

        output = tmp_path / "result.json"
        rc = _cmd_benchmark(["--output", str(output)])
        assert rc == 0
        assert output.exists()
        data = json.loads(output.read_text())
        assert "corpus_size" in data
        assert data["corpus_size"] > 0

    def test_cli_default_policy(self, capsys):
        """CLI with no args runs the default CoveragePolicy."""
        from bin.cmd_benchmark import _cmd_benchmark

        rc = _cmd_benchmark([])
        assert rc == 0
