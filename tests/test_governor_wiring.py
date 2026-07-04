"""Tests for Model Governor live-path wiring (Slice 1 of alpha.6 "Economics").

Covers:
  * AC-P1-5 — the pr-cycle call site accepts a ``selection_fn`` parameter and
    swapping it changes the recorded tier (identity default → tier-2; a
    select_tier-with-policy swap → tier-0 for high coverage).
  * AC-P1-6 — when ``select_tier`` returns ESCALATE (escalate_on_zero_coverage
    policy, zero seeded assets), the recorded recommendation carries
    tier="escalate".

Scope note: full escalation-routing-to-``escalation_log.jsonl`` wiring is
deferred beyond alpha.6; recording the ESCALATE recommendation to the Governor
ledger is sufficient proof here (the existing Escalation path consumes it
later). No new escalation infrastructure is created (AC-P1-6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import List

from bluei.engine.commands.context import RunContext
from bluei.engine.model_governor import (
    CoveragePolicy,
    ModelTier,
    compute_coverage,
    identity_selection,
    record_recommendation,
    select_tier,
)
from bluei.engine.rule_family import derive_rule_family


# --- lightweight duck-typed fixtures (mirror test_model_governor.py) ---


@dataclass
class _FakePattern:
    rule_family: str = ""


class _FakeStore:
    def __init__(self, patterns: List[_FakePattern]) -> None:
        self._patterns = patterns

    def load_active(self) -> List[_FakePattern]:
        return list(self._patterns)


def _read_ledger(ledger_path):
    return [
        json.loads(line)
        for line in ledger_path.read_text().splitlines()
        if line.strip()
    ]


def _record_via_ctx(ctx, finding, ledger_path, store=None):
    """Mirror the _process_one_issue recording flow via ctx.selection_fn."""
    coverage = compute_coverage(
        finding, derive_rule_family(finding.rule), pattern_store=store
    )
    rec = ctx.selection_fn(finding, coverage)
    record_recommendation(finding, rec, ledger_path, ctx.run_id)
    return rec


# --------------------------------------------------------------------------
# AC-P1-5 — selection_fn parameter is real + swappable
# --------------------------------------------------------------------------


class TestSelectionFnDefault:
    def test_default_selection_fn_is_identity(self):
        ctx = RunContext(args=SimpleNamespace())
        assert ctx.selection_fn is identity_selection

    def test_governor_ledger_path_defaults_to_none(self):
        ctx = RunContext(args=SimpleNamespace())
        assert ctx.governor_ledger_path is None


class TestAcP15IdentityRecordsTier2:
    def test_identity_records_tier_2_with_high_coverage(self, tmp_path, make_finding):
        finding = make_finding(rule="ruff-b904")
        ledger = tmp_path / "governor_recommendations.jsonl"

        ctx = RunContext(args=SimpleNamespace())
        ctx.run_id = "run-identity"
        # identity default; high-coverage store should still produce tier-2
        store = _FakeStore([_FakePattern("ruff-b") for _ in range(4)])

        rec = _record_via_ctx(ctx, finding, ledger, store=store)
        assert rec.tier is ModelTier.TIER_2

        rows = _read_ledger(ledger)
        assert len(rows) == 1
        assert rows[0]["tier"] == "tier-2"
        assert rows[0]["finding_id"] == finding.finding_id


class TestAcP15SwapChangesTier:
    def test_select_tier_records_tier_0_for_high_coverage(self, tmp_path, make_finding):
        finding = make_finding(rule="ruff-b904")
        family = derive_rule_family(finding.rule)
        ledger = tmp_path / "governor_recommendations.jsonl"

        ctx = RunContext(args=SimpleNamespace())
        ctx.run_id = "run-select"
        # beta.1 swap: bind a policy via partial (the config-flip pattern).
        ctx.selection_fn = partial(select_tier, policy=CoveragePolicy())
        store = _FakeStore([_FakePattern(family) for _ in range(4)])

        rec = _record_via_ctx(ctx, finding, ledger, store=store)
        assert rec.tier is ModelTier.TIER_0

        rows = _read_ledger(ledger)
        assert rows[0]["tier"] == "tier-0"

    def test_identity_vs_select_tier_produce_different_tiers(
        self, tmp_path, make_finding
    ):
        finding = make_finding(rule="ruff-b904")
        family = derive_rule_family(finding.rule)
        store = _FakeStore([_FakePattern(family) for _ in range(3)])

        # Identity default → tier-2
        ctx_id = RunContext(args=SimpleNamespace())
        ctx_id.run_id = "id"
        ledger_id = tmp_path / "identity.jsonl"
        rec_id = _record_via_ctx(ctx_id, finding, ledger_id, store=store)

        # select_tier swap → tier-0 (3+ assets)
        ctx_sel = RunContext(args=SimpleNamespace())
        ctx_sel.run_id = "sel"
        ctx_sel.selection_fn = partial(select_tier, policy=CoveragePolicy())
        ledger_sel = tmp_path / "select.jsonl"
        rec_sel = _record_via_ctx(ctx_sel, finding, ledger_sel, store=store)

        assert rec_id.tier is ModelTier.TIER_2
        assert rec_sel.tier is ModelTier.TIER_0

        assert _read_ledger(ledger_id)[0]["tier"] == "tier-2"
        assert _read_ledger(ledger_sel)[0]["tier"] == "tier-0"


# --------------------------------------------------------------------------
# AC-P1-6 — escalate recommendation recording
# --------------------------------------------------------------------------


class TestAcP16Escalation:
    """When select_tier returns ESCALATE, the recommendation is recorded as such.

    Scope note (PROMPT T1.3): full escalation-routing-to-escalation_log.jsonl
    wiring is beyond Slice 1. Recording the ESCALATE recommendation to the
    Governor ledger is the alpha.6 proof point; the existing Escalation path
    consumes it later. No new escalation infrastructure is created.
    """

    def test_escalate_recorded_on_zero_coverage_policy(self, tmp_path, make_finding):
        finding = make_finding(rule="ruff-b904")
        ledger = tmp_path / "governor_recommendations.jsonl"

        ctx = RunContext(args=SimpleNamespace())
        ctx.run_id = "run-escalate"
        # No store → zero coverage; escalate_on_zero_coverage=True
        ctx.selection_fn = partial(
            select_tier, policy=CoveragePolicy(escalate_on_zero_coverage=True)
        )

        rec = _record_via_ctx(ctx, finding, ledger, store=None)
        assert rec.tier is ModelTier.ESCALATE

        rows = _read_ledger(ledger)
        assert len(rows) == 1
        assert rows[0]["tier"] == "escalate"
        assert "escalates on zero coverage" in rows[0]["rationale"]

    def test_identity_default_does_not_escalate_on_zero_coverage(
        self, tmp_path, make_finding
    ):
        finding = make_finding(rule="ruff-b904")
        ledger = tmp_path / "governor_recommendations.jsonl"

        ctx = RunContext(args=SimpleNamespace())
        ctx.run_id = "run-id-zero"

        rec = _record_via_ctx(ctx, finding, ledger, store=None)
        assert rec.tier is ModelTier.TIER_2

        rows = _read_ledger(ledger)
        assert rows[0]["tier"] == "tier-2"
