"""Tests for Model Governor live-path wiring (beta.1 act-on-recommendation).

Covers:
  * AC-P1-1 — ``RunContext.selection_fn`` default is now ``select_tier``
    (beta.1 flip; was ``identity_selection`` in alpha.6).
  * AC-P1-2 — with a discovery returning a tier-0 model, the resolved template
    carries ``--model <id>``.
  * AC-P1-4 — ``record_invocation`` receives the discovered model id (or
    ``DEFAULT_ESTIMATE_LABEL`` under empty config), never the old hardcoded
    literal in a way that breaks.
  * AC-P1-5 — under ``ctx.discovery is None`` (empty operator config / C2),
    the call-site path produces a byte-identical template + model_name to
    alpha.6 (the regression guard).
  * AC-P1-6 — when ``select_tier`` returns ESCALATE (escalate_on_zero_coverage
    policy, zero seeded assets), the recorded recommendation carries
    tier="escalate".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import List

from bluei.engine.commands.context import RunContext
from bluei.engine.model_discovery import ResolvedModel
from bluei.engine.model_governor import (
    DEFAULT_ESTIMATE_LABEL,
    CoveragePolicy,
    ModelTier,
    compute_coverage,
    identity_selection,
    record_recommendation,
    resolve_governed_model,
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
    def test_default_selection_fn_is_select_tier(self):
        # AC-P1-1 (B2): beta.1 flips the default to select_tier (real
        # recommendations). identity_selection remains the explicit opt-out.
        ctx = RunContext(args=SimpleNamespace())
        assert ctx.selection_fn is select_tier

    def test_governor_ledger_path_defaults_to_none(self):
        ctx = RunContext(args=SimpleNamespace())
        assert ctx.governor_ledger_path is None

    def test_discovery_defaults_to_none(self):
        # AC-P1-5: discovery None = identity behavior (empty operator config).
        ctx = RunContext(args=SimpleNamespace())
        assert ctx.discovery is None


class TestAcP15IdentityRecordsTier2:
    def test_identity_records_tier_2_with_high_coverage(self, tmp_path, make_finding):
        finding = make_finding(rule="ruff-b904")
        ledger = tmp_path / "governor_recommendations.jsonl"

        ctx = RunContext(args=SimpleNamespace())
        ctx.run_id = "run-identity"
        # Explicit opt-out: identity_selection returns tier-2 regardless of
        # coverage. (beta.1 default is select_tier; identity is now opt-in.)
        ctx.selection_fn = identity_selection
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

        # Identity (explicit opt-out) → tier-2
        ctx_id = RunContext(args=SimpleNamespace())
        ctx_id.run_id = "id"
        ctx_id.selection_fn = identity_selection
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


# --------------------------------------------------------------------------
# beta.1: resolve_governed_model wiring through the call-site path
# --------------------------------------------------------------------------


class _FakeDiscovery:
    """Minimal ModelDiscovery Protocol implementation for wiring tests."""

    def __init__(self, models):
        self._models = models

    def discover(self, backend):
        return list(self._models)


def _tier0_claude_model():
    return ResolvedModel(
        tier=ModelTier.TIER_0,
        backend="claude",
        model_id="claude-3-5-haiku",
        input_per_1k=0.25,
        output_per_1k=1.25,
    )


class TestResolveGovernedModelWiring:
    """AC-P1-2 / AC-P1-4 / AC-P1-5 — the call-site resolution path."""

    def test_empty_config_template_is_byte_identical_to_alpha(
        self, tmp_path, make_finding
    ):
        # AC-P1-5: ctx.discovery=None → resolved_template == base_template
        # and model_name == DEFAULT_ESTIMATE_LABEL (byte-identical to alpha.6).
        finding = make_finding(rule="ruff-b904")
        ledger = tmp_path / "governor_recommendations.jsonl"
        base_template = "claude --dangerously-skip-permissions --print"

        ctx = RunContext(args=SimpleNamespace())
        ctx.run_id = "run-empty"
        ctx.governor_ledger_path = ledger

        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=ctx.selection_fn,
            pattern_store=ctx.pattern_store,
            discovery=ctx.discovery,  # None
            base_template=base_template,
            ledger_path=ctx.governor_ledger_path,
            run_id=ctx.run_id,
        )
        assert tmpl == base_template  # unchanged
        assert name == DEFAULT_ESTIMATE_LABEL
        assert resolved is None
        assert rec is not None  # recommendation still recorded

    def test_tier0_discovery_injects_model_flag(self, tmp_path, make_finding):
        # AC-P1-2: a discovery returning a tier-0 model → resolved template
        # carries --model <id>.
        finding = make_finding(rule="ruff-b904")
        family = derive_rule_family(finding.rule)
        ledger = tmp_path / "governor_recommendations.jsonl"
        base_template = "claude --dangerously-skip-permissions --print"

        store = _FakeStore([_FakePattern(family) for _ in range(4)])
        discovery = _FakeDiscovery([_tier0_claude_model()])

        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=store,
            discovery=discovery,
            base_template=base_template,
            ledger_path=ledger,
            run_id="run-tier0",
        )
        assert rec is not None
        assert rec.tier is ModelTier.TIER_0
        assert resolved is not None
        assert "--model claude-3-5-haiku" in tmpl
        assert name == "claude-3-5-haiku"

    def test_record_invocation_receives_discovered_id(self, tmp_path, make_finding):
        # AC-P1-4: model_name (fed to record_invocation) is the discovered id,
        # not the old hardcoded literal — unless discovery is None.
        finding = make_finding(rule="ruff-b904")
        family = derive_rule_family(finding.rule)
        ledger = tmp_path / "governor_recommendations.jsonl"
        base_template = "claude --dangerously-skip-permissions --print"

        store = _FakeStore([_FakePattern(family) for _ in range(4)])

        # Discovered → discovered id
        _, _, _, name = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=store,
            discovery=_FakeDiscovery([_tier0_claude_model()]),
            base_template=base_template,
            ledger_path=ledger,
            run_id="run-disc",
        )
        assert name == "claude-3-5-haiku"

        # Empty config → DEFAULT_ESTIMATE_LABEL (the alpha.6 rate anchor)
        _, _, _, name_empty = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=store,
            discovery=None,
            base_template=base_template,
            ledger_path=ledger,
            run_id="run-empty",
        )
        assert name_empty == DEFAULT_ESTIMATE_LABEL

    def test_guard_not_met_returns_identity(self, tmp_path, make_finding):
        # ledger_path None → guard not met → no recording, identity template.
        finding = make_finding(rule="ruff-b904")
        base_template = "claude --print"

        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=None,
            discovery=_FakeDiscovery([_tier0_claude_model()]),
            base_template=base_template,
            ledger_path=None,  # guard not met
            run_id="run-guard",
        )
        assert rec is None
        assert resolved is None
        assert tmpl == base_template
        assert name == DEFAULT_ESTIMATE_LABEL

    def test_none_ledger_path_via_ctx_is_noop(self, tmp_path, make_finding):
        # CR-4: the pr-cycle call site passes ``ctx.governor_ledger_path`` to
        # ``resolve_governed_model``. When the ctx default (None) flows through,
        # the guard fires: no ledger file, no exception, identity return
        # ``(None, None, base_template, DEFAULT_ESTIMATE_LABEL)``. This is the
        # pr-cycle inert-posture contract (the batch equivalent landed in
        # test_batch_governor.py).
        finding = make_finding(rule="ruff-b904")
        base_template = "claude --dangerously-skip-permissions --print"
        ledger = tmp_path / "governor_recommendations.jsonl"

        ctx = RunContext(args=SimpleNamespace())
        ctx.governor_ledger_path = None  # the default; pr-cycle passes this

        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=ctx.selection_fn,
            pattern_store=ctx.pattern_store,
            discovery=ctx.discovery,
            base_template=base_template,
            ledger_path=ctx.governor_ledger_path,  # None flows through
            run_id=ctx.run_id,
        )
        assert rec is None
        assert resolved is None
        assert tmpl == base_template
        assert name == DEFAULT_ESTIMATE_LABEL
        assert not ledger.exists()  # no ledger file written
