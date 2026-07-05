"""Tests for ``resolve_governed_model`` — the shared Governor resolution helper.

Phase 1 of beta.1 (ADR-0022 amendment 1). The helper is called ONCE at the
pr-cycle call site (and will be called by batch in Phase 2). It:

  * records a recommendation for every Finding reaching the call point
    (guard permitting);
  * resolves the recommended tier to a concrete model via discovery;
  * injects ``--model <id>`` into the command template;
  * returns the model_name for the cost ledger.

Covers the four resolution paths:
  1. guard not met (ledger_path or selection_fn None)
  2. discovery None (empty operator config / C2 identity)
  3. tier mapped → model resolved + flag injected
  4. tier unmapped → resolved_model None → identity template
"""

from __future__ import annotations

import json
from typing import List

from bluei.engine.model_discovery import ResolvedModel
from bluei.engine.model_governor import (
    DEFAULT_ESTIMATE_LABEL,
    ModelTier,
    resolve_governed_model,
    select_tier,
)
from bluei.engine.models import Finding


# --- fixtures ---------------------------------------------------------------


def _finding(**overrides):
    defaults = dict(
        finding_id="f-001",
        repo="test-repo",
        path="src/main.py",
        line=10,
        rule="ruff-b904",
        snippet="pass",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
        fix_attempts=0,
    )
    defaults.update(overrides)
    return Finding(**defaults)


class _FakeStore:
    """Duck-typed pattern store with enough assets to push select_tier → tier-0."""

    class _Pattern:
        def __init__(self, rf):
            self.rule_family = rf

    def __init__(self, count, family="ruff-b"):
        self._patterns = [self._Pattern(family) for _ in range(count)]

    def load_active(self):
        return list(self._patterns)


class _FakeDiscovery:
    """Minimal ModelDiscovery Protocol implementation."""

    def __init__(self, models: List[ResolvedModel]):
        self._models = models

    def discover(self, backend: str) -> List[ResolvedModel]:
        return list(self._models)


def _read_ledger(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


BASE_TEMPLATE = "claude --dangerously-skip-permissions --print"


# --- path 1: guard not met --------------------------------------------------


class TestGuardNotMet:
    def test_ledger_none_returns_identity(self, tmp_path):
        finding = _finding()
        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=_FakeStore(4),
            discovery=_FakeDiscovery([]),
            base_template=BASE_TEMPLATE,
            ledger_path=None,
            run_id="run-x",
        )
        assert rec is None
        assert resolved is None
        assert tmpl == BASE_TEMPLATE
        assert name == DEFAULT_ESTIMATE_LABEL

    def test_selection_fn_none_returns_identity(self, tmp_path):
        finding = _finding()
        ledger = tmp_path / "gov.jsonl"
        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=None,
            pattern_store=_FakeStore(4),
            discovery=_FakeDiscovery([]),
            base_template=BASE_TEMPLATE,
            ledger_path=ledger,
            run_id="run-x",
        )
        assert rec is None
        assert resolved is None
        assert tmpl == BASE_TEMPLATE
        assert name == DEFAULT_ESTIMATE_LABEL
        # No ledger written when guard not met
        assert not ledger.exists()


# --- path 2: discovery None (C2 identity) -----------------------------------


class TestDiscoveryNone:
    def test_returns_base_template_unchanged(self, tmp_path):
        finding = _finding()
        ledger = tmp_path / "gov.jsonl"
        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=_FakeStore(4),
            discovery=None,
            base_template=BASE_TEMPLATE,
            ledger_path=ledger,
            run_id="run-c2",
        )
        assert tmpl == BASE_TEMPLATE
        assert name == DEFAULT_ESTIMATE_LABEL
        assert resolved is None
        assert rec is not None  # recommendation still recorded

    def test_ledger_row_written_with_beta1_policy_version(self, tmp_path):
        finding = _finding()
        ledger = tmp_path / "gov.jsonl"
        resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=_FakeStore(4),
            discovery=None,
            base_template=BASE_TEMPLATE,
            ledger_path=ledger,
            run_id="run-pv",
        )
        rows = _read_ledger(ledger)
        assert len(rows) == 1
        assert rows[0]["policy_version"] == "beta.1"
        assert rows[0]["finding_id"] == "f-001"


# --- path 3: tier mapped → flag injected ------------------------------------


class TestTierMapped:
    def test_tier0_model_injected_into_template(self, tmp_path):
        finding = _finding()
        ledger = tmp_path / "gov.jsonl"
        model = ResolvedModel(
            tier=ModelTier.TIER_0,
            backend="claude",
            model_id="claude-3-5-haiku",
            input_per_1k=0.25,
            output_per_1k=1.25,
        )
        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=_FakeStore(4),  # 4 assets → tier-0
            discovery=_FakeDiscovery([model]),
            base_template=BASE_TEMPLATE,
            ledger_path=ledger,
            run_id="run-t0",
        )
        assert rec is not None
        assert rec.tier is ModelTier.TIER_0
        assert resolved is not None
        assert resolved.model_id == "claude-3-5-haiku"
        assert "--model claude-3-5-haiku" in tmpl
        assert name == "claude-3-5-haiku"

    def test_tier1_model_injected_into_template(self, tmp_path):
        finding = _finding()
        ledger = tmp_path / "gov.jsonl"
        model = ResolvedModel(
            tier=ModelTier.TIER_1,
            backend="claude",
            model_id="claude-3-5-sonnet",
            input_per_1k=3.0,
            output_per_1k=15.0,
        )
        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=_FakeStore(2),  # 2 assets → tier-1
            discovery=_FakeDiscovery([model]),
            base_template=BASE_TEMPLATE,
            ledger_path=ledger,
            run_id="run-t1",
        )
        assert rec is not None
        assert rec.tier is ModelTier.TIER_1
        assert "--model claude-3-5-sonnet" in tmpl
        assert name == "claude-3-5-sonnet"


# --- path 4: tier unmapped → identity template ------------------------------


class TestTierUnmapped:
    def test_unmapped_tier_returns_identity_template(self, tmp_path):
        finding = _finding()
        ledger = tmp_path / "gov.jsonl"
        # Discovery only has a tier-0 model; select_tier with 4 assets → tier-0
        # which IS mapped. Use 2 assets → tier-1 which is NOT in discovery.
        model = ResolvedModel(
            tier=ModelTier.TIER_0,
            backend="claude",
            model_id="claude-3-5-haiku",
            input_per_1k=0.25,
            output_per_1k=1.25,
        )
        rec, resolved, tmpl, name = resolve_governed_model(
            finding=finding,
            selection_fn=select_tier,
            pattern_store=_FakeStore(2),  # tier-1 requested
            discovery=_FakeDiscovery([model]),  # only tier-0 available
            base_template=BASE_TEMPLATE,
            ledger_path=ledger,
            run_id="run-unmapped",
        )
        assert rec is not None
        assert rec.tier is ModelTier.TIER_1
        assert resolved is None  # tier-1 not in discovery
        assert tmpl == BASE_TEMPLATE  # unchanged
        assert name == DEFAULT_ESTIMATE_LABEL
