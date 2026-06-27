"""test_cascade_governance.py — ADR-0008 cascade-stage consultation wiring.

Verifies that the four cascade-stage chokepoints consult the projected
Governance State overlay (Phase 3 wiring) and skip PAUSED/RETIRED assets.

* PatternReplayCascadeStage — per-candidate fallthrough (exact/structural/fuzzy)
* RecipeCascadeStage         — single recipe.id check
* ASTTransformCascadeStage   — single transform check
* discover_active_rule_findings — emergent-rule filter

No-op with empty governance trail: every asset defaults to ACTIVE.
"""

from pathlib import Path
from unittest.mock import MagicMock

from bluei.app.emergent_rules import (
    DetectionPattern,
    EmergentRule,
    EmergentRuleStatus,
    discover_active_rule_findings,
)
from bluei.engine.cascade import (
    ASTTransformCascadeStage,
    CascadeContext,
    PatternReplayCascadeStage,
    RecipeCascadeStage,
)
from bluei.engine.models import Finding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding(rule: str = "ruff-F401", path: str = "src/app.py") -> Finding:
    return Finding(
        finding_id="f-1",
        repo="demo",
        path=path,
        line=10,
        rule=rule,
        snippet="import os",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )


def _ctx(**overrides) -> CascadeContext:
    return CascadeContext(
        worktree=Path("/tmp/demo"),
        repo_path=Path("/tmp/demo"),
        log_file=Path("/tmp/demo/log"),
        language="python",
        **overrides,
    )


def _mock_pattern(pattern_id: str, confidence: float) -> MagicMock:
    p = MagicMock()
    p.pattern_id = pattern_id
    p.confidence = confidence
    return p


# ---------------------------------------------------------------------------
# PatternReplayCascadeStage
# ---------------------------------------------------------------------------


def test_pattern_replay_active_by_default_empty_governance() -> None:
    stage = PatternReplayCascadeStage(confidence_threshold=0.8)
    store = MagicMock()
    store.lookup.return_value = _mock_pattern("fp-1", 0.9)
    ctx = _ctx(pattern_store=store, governance_state={})
    assert stage.can_handle(_finding(), ctx) is True


def test_pattern_replay_paused_blocks_exact_match() -> None:
    stage = PatternReplayCascadeStage(confidence_threshold=0.8)
    store = MagicMock()
    store.lookup.return_value = _mock_pattern("fp-1", 0.9)
    # structural + fuzzy return None so nothing falls through
    store.lookup_structural.return_value = None
    store.lookup_fuzzy.return_value = None
    ctx = _ctx(
        pattern_store=store,
        governance_state={"pattern:fp-1": "paused"},
    )
    assert stage.can_handle(_finding(), ctx) is False


def test_pattern_replay_paused_exact_falls_through_to_structural() -> None:
    """Per-candidate fallthrough: a PAUSED exact match must NOT short-circuit
    the structural lookup, which may return an ACTIVE pattern."""
    stage = PatternReplayCascadeStage(confidence_threshold=0.8)
    store = MagicMock()
    store.lookup.return_value = _mock_pattern("fp-paused", 0.95)
    store.lookup_structural.return_value = _mock_pattern("fp-active", 0.9)
    store.lookup_fuzzy.return_value = None
    ctx = _ctx(
        pattern_store=store,
        governance_state={
            "pattern:fp-paused": "paused",
            "pattern:fp-active": "active",
        },
    )
    assert stage.can_handle(_finding(), ctx) is True


def test_pattern_replay_all_paused_returns_false() -> None:
    """Only return False when every lookup finds nothing or only PAUSED/RETIRED."""
    stage = PatternReplayCascadeStage(confidence_threshold=0.8)
    store = MagicMock()
    store.lookup.return_value = _mock_pattern("fp-a", 0.95)
    store.lookup_structural.return_value = _mock_pattern("fp-b", 0.9)
    store.lookup_fuzzy.return_value = _mock_pattern("fp-c", 0.92)
    ctx = _ctx(
        pattern_store=store,
        governance_state={
            "pattern:fp-a": "paused",
            "pattern:fp-b": "retired",
            "pattern:fp-c": "paused",
        },
    )
    assert stage.can_handle(_finding(), ctx) is False


def test_pattern_replay_paused_then_active_fuzzy_returns_true() -> None:
    stage = PatternReplayCascadeStage(confidence_threshold=0.8)
    store = MagicMock()
    store.lookup.return_value = _mock_pattern("fp-paused", 0.95)
    store.lookup_structural.return_value = None
    store.lookup_fuzzy.return_value = _mock_pattern("fp-fuzzy", 0.88)
    ctx = _ctx(
        pattern_store=store,
        governance_state={
            "pattern:fp-paused": "paused",
            "pattern:fp-fuzzy": "active",
        },
    )
    assert stage.can_handle(_finding(), ctx) is True


# ---------------------------------------------------------------------------
# RecipeCascadeStage
# ---------------------------------------------------------------------------


def test_recipe_active_by_default() -> None:
    stage = RecipeCascadeStage()
    recipe = MagicMock()
    recipe.id = "test-recipe"
    engine = MagicMock()
    engine.match.return_value = recipe
    ctx = _ctx(recipe_engine=engine, governance_state={})
    assert stage.can_handle(_finding(), ctx) is True


def test_recipe_paused_blocks() -> None:
    stage = RecipeCascadeStage()
    recipe = MagicMock()
    recipe.id = "test-recipe"
    engine = MagicMock()
    engine.match.return_value = recipe
    ctx = _ctx(
        recipe_engine=engine,
        governance_state={"recipe:test-recipe": "paused"},
    )
    assert stage.can_handle(_finding(), ctx) is False


def test_recipe_retired_blocks() -> None:
    stage = RecipeCascadeStage()
    recipe = MagicMock()
    recipe.id = "test-recipe"
    engine = MagicMock()
    engine.match.return_value = recipe
    ctx = _ctx(
        recipe_engine=engine,
        governance_state={"recipe:test-recipe": "retired"},
    )
    assert stage.can_handle(_finding(), ctx) is False


# ---------------------------------------------------------------------------
# ASTTransformCascadeStage
# ---------------------------------------------------------------------------


def _ast_stage_with_transform(transform: MagicMock) -> ASTTransformCascadeStage:
    stage = ASTTransformCascadeStage()
    stage._transformer = MagicMock()
    stage._transformer.get_transform.return_value = transform
    stage._initialized = True
    return stage


def test_ast_active_by_default() -> None:
    stage = _ast_stage_with_transform(MagicMock())
    ctx = _ctx(governance_state={})
    assert stage.can_handle(_finding(rule="ruff-F401"), ctx) is True


def test_ast_paused_blocks() -> None:
    stage = _ast_stage_with_transform(MagicMock())
    ctx = _ctx(
        governance_state={"transform:python-ruff-F401": "paused"},
    )
    assert stage.can_handle(_finding(rule="ruff-F401"), ctx) is False


def test_ast_no_transform_returns_false() -> None:
    stage = ASTTransformCascadeStage()
    stage._transformer = MagicMock()
    stage._transformer.get_transform.return_value = None
    stage._initialized = True
    ctx = _ctx(governance_state={})
    assert stage.can_handle(_finding(rule="ruff-F401"), ctx) is False


# ---------------------------------------------------------------------------
# discover_active_rule_findings
# ---------------------------------------------------------------------------


def _emergent_rule(
    rule_id: str, status: EmergentRuleStatus = EmergentRuleStatus.ACTIVE
) -> EmergentRule:
    return EmergentRule(
        rule_id=rule_id,
        header=rule_id,
        detection_pattern=DetectionPattern(
            search_pattern="llm-typed-dict",
            file_glob="src/api/**",
        ),
        language="python",
        category="type",
        status=status,
        confidence=0.82,
        severity_default="low",
    )


def _emergent_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "repo"
    target = worktree / "src" / "api" / "users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def handle():\n    llm-typed-dict\n", encoding="utf-8")
    return worktree


def test_discover_filters_paused_rule(tmp_path) -> None:
    worktree = _emergent_worktree(tmp_path)
    active = _emergent_rule("er-active")
    paused = _emergent_rule("er-paused")

    findings = discover_active_rule_findings(
        [active, paused],
        worktree,
        repo_name="demo",
        governance_state={"emergent_rule:er-paused": "paused"},
    )
    rule_ids = {f.rule for f in findings}
    assert "emergent:er-active" in rule_ids
    assert "emergent:er-paused" not in rule_ids


def test_discover_no_governance_keeps_all_active(tmp_path) -> None:
    worktree = _emergent_worktree(tmp_path)
    a = _emergent_rule("er-a")
    b = _emergent_rule("er-b")
    findings = discover_active_rule_findings([a, b], worktree, repo_name="demo")
    assert len(findings) == 2


def test_discover_retired_filtered(tmp_path) -> None:
    worktree = _emergent_worktree(tmp_path)
    retired = _emergent_rule("er-retired")
    findings = discover_active_rule_findings(
        [retired],
        worktree,
        repo_name="demo",
        governance_state={"emergent_rule:er-retired": "retired"},
    )
    assert findings == []
