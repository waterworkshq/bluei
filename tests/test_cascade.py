"""test_cascade.py — Tests for the multi-engine deterministic cascade."""

import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

import pytest

from bluei.engine.cascade import (
    CascadeContext,
    CascadeResult,
    CascadeStage,
    CascadeTelemetry,
    DeterministicCascade,
    LinterFixStage,
    RecipeCascadeStage,
    PatternReplayCascadeStage,
    ASTTransformCascadeStage,
    CompositePatternCascadeStage,
    default_linter_stages,
    default_cascade_stages,
)
from bluei.engine.models import Finding


def _finding(rule: str = "ruff-e501", path: str = "src/app.py") -> Finding:
    return Finding(
        finding_id="f-1",
        repo="demo",
        path=path,
        line=10,
        rule=rule,
        snippet="line too long",
        confidence=0.85,
        quick_win=False,
        safe_to_autofix=False,
    )


class _AlwaysSucceed(CascadeStage):
    name = "always-succeed"
    tier = 1

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=True,
            latency_ms=10,
        )


class _AlwaysFail(CascadeStage):
    name = "always-fail"
    tier = 2

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        return CascadeResult(
            success=False,
            stage_name=self.name,
            error="intentional failure",
            latency_ms=5,
        )


class _ConditionalHandle(CascadeStage):
    name = "conditional"
    tier = 1

    def __init__(self, handled_rules):
        self.handled_rules = handled_rules

    def can_handle(self, finding, context):
        return finding.rule in self.handled_rules

    def attempt(self, finding, worktree, context):
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=True,
            latency_ms=1,
        )


class _SucceedButFailValidation(CascadeStage):
    name = "bad-validation"
    tier = 1
    rolled_back = False

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=False,
            latency_ms=5,
        )

    def rollback(self, finding, worktree):
        self.rolled_back = True


class _HighTierStage(CascadeStage):
    name = "high-tier"
    tier = 10

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=True,
            latency_ms=100,
        )


def test_cascade_first_success_wins(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    cascade = DeterministicCascade([_AlwaysSucceed(), _AlwaysFail()])
    result = cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    assert result.success
    assert result.stage_name == "always-succeed"


def test_cascade_exhausted_when_all_fail(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    cascade = DeterministicCascade([_AlwaysFail()])
    result = cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    assert not result.success
    assert result.stage_name == "cascade_exhausted"


def test_cascade_skips_unhandleable_stages(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    stage = _ConditionalHandle({"ruff-e501"})
    cascade = DeterministicCascade([stage])
    result = cascade.execute(
        _finding(rule="other-rule"), tmp_path, CascadeContext(log_file=log)
    )
    assert not result.success
    assert result.stage_name == "cascade_exhausted"


def test_cascade_respects_max_stages(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    stages = [_AlwaysFail() for _ in range(20)]
    cascade = DeterministicCascade(stages)
    ctx = CascadeContext(log_file=log, max_stages=3)
    result = cascade.execute(_finding(), tmp_path, ctx)
    assert not result.success
    log_text = log.read_text()
    assert "max_stages=3 reached" in log_text


def test_cascade_rollback_on_validation_failure(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    bad = _SucceedButFailValidation()
    good = _AlwaysSucceed()
    good.tier = 2
    cascade = DeterministicCascade([bad, good])
    result = cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    assert result.success
    assert result.stage_name == "always-succeed"
    assert bad.rolled_back


def test_cascade_orders_by_tier(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    high = _HighTierStage()
    low = _AlwaysSucceed()
    low.tier = 1
    low.name = "low-tier"
    cascade = DeterministicCascade([high, low])
    result = cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    assert result.success
    assert result.stage_name == "low-tier"


def test_cascade_register_adds_stage(tmp_path):
    cascade = DeterministicCascade()
    cascade.register(_AlwaysSucceed())
    assert len(cascade.stages) == 1
    assert cascade.stages[0].name == "always-succeed"


def test_cascade_register_sorts_by_tier(tmp_path):
    cascade = DeterministicCascade()
    cascade.register(_HighTierStage())
    cascade.register(_AlwaysSucceed())
    assert cascade.stages[0].tier <= cascade.stages[1].tier


def test_cascade_empty_stages_returns_exhausted(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    cascade = DeterministicCascade()
    result = cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    assert not result.success
    assert result.stage_name == "cascade_exhausted"


def test_cascade_telemetry_written_on_success(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    cascade = DeterministicCascade([_AlwaysSucceed()])
    cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    log_text = log.read_text()
    assert "cascade-telemetry:" in log_text
    assert '"success": true' in log_text


def test_cascade_telemetry_written_on_exhaustion(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    cascade = DeterministicCascade([_AlwaysFail()])
    cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    log_text = log.read_text()
    assert "cascade-telemetry:" in log_text
    assert '"success": false' in log_text


def test_cascade_result_latency_accumulated(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    stages = [_AlwaysFail(), _AlwaysFail()]
    for i, s in enumerate(stages):
        s.name = f"fail-{i}"
    cascade = DeterministicCascade(stages)
    result = cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    assert not result.success
    assert result.latency_ms >= 0


def test_cascade_telemetry_to_dict():
    t = CascadeTelemetry(
        finding_id="f-1",
        rule="ruff-e501",
        stages_tried=["ruff-fix"],
        stages_skipped=["recipe-engine"],
        stages_failed=[{"stage": "ruff-fix", "reason": "no match"}],
        final_stage="pattern-replay",
        total_latency_ms=1200,
        success=True,
    )
    d = t.to_dict()
    assert d["finding_id"] == "f-1"
    assert d["stages_tried"] == ["ruff-fix"]
    assert d["success"] is True


def test_cascade_context_defaults():
    ctx = CascadeContext()
    assert ctx.max_stages == 10
    assert ctx.allow_llm is True
    assert ctx.language == ""


# --- LinterFixStage tests ---


def test_linter_stage_can_handle_checks_language(tmp_path):
    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "check", "--fix", "{file}"],
        languages=["python"],
    )
    ctx_py = CascadeContext(language="python", log_file=tmp_path / "log.txt")
    ctx_ts = CascadeContext(language="typescript", log_file=tmp_path / "log.txt")
    with patch("shutil.which", return_value="/usr/bin/ruff"):
        assert stage.can_handle(_finding(), ctx_py)
        assert not stage.can_handle(_finding(), ctx_ts)


def test_linter_stage_can_handle_checks_tool_installed(tmp_path):
    stage = LinterFixStage(
        name="test-linter",
        command=["nonexistent-tool", "{file}"],
        languages=["python"],
    )
    ctx = CascadeContext(language="python", log_file=tmp_path / "log.txt")
    with patch("shutil.which", return_value=None):
        assert not stage.can_handle(_finding(), ctx)


def test_linter_stage_can_handle_checks_rule_match(tmp_path):
    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "{file}"],
        languages=["python"],
        applicable_rules={"ruff-*"},
    )
    ctx = CascadeContext(language="python", log_file=tmp_path / "log.txt")
    with patch("shutil.which", return_value="/usr/bin/ruff"):
        assert stage.can_handle(_finding(rule="ruff-e501"), ctx)
        assert not stage.can_handle(_finding(rule="xo-max-lines"), ctx)


def test_linter_stage_attempt_returns_success_when_resolved(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")

    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "check", "--fix", "{file}"],
        languages=["python"],
    )

    with (
        patch("shutil.which", return_value="/usr/bin/ruff"),
        patch("bluei.engine.cascade.run_capture", return_value=(0, "")),
        patch("bluei.engine.validation.verify_fix_closed", return_value=True),
    ):
        ctx = CascadeContext(language="python", log_file=log)
        result = stage.attempt(_finding(), tmp_path, ctx)
        assert result.success
        assert result.validation_passed
        assert result.stage_name == "test-linter"


def test_linter_stage_attempt_returns_failure_when_not_resolved(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")

    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "check", "--fix", "{file}"],
        languages=["python"],
    )

    with (
        patch("shutil.which", return_value="/usr/bin/ruff"),
        patch("bluei.engine.cascade.run_capture", return_value=(1, "error output")),
        patch("bluei.engine.validation.verify_fix_closed", return_value=False),
    ):
        ctx = CascadeContext(language="python", log_file=log)
        result = stage.attempt(_finding(), tmp_path, ctx)
        assert not result.success
        assert result.error == "error output"


def test_linter_stage_rollback_runs_git_checkout(tmp_path):
    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "{file}"],
        languages=["python"],
    )
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    with patch("bluei.engine.cascade.run_capture") as mock_rc:
        stage.rollback(_finding(path="src/app.py"), tmp_path)
        mock_rc.assert_called_once()
        assert "git" in mock_rc.call_args[0][0]
        assert "checkout" in mock_rc.call_args[0][0]


def test_linter_stage_attempt_file_not_found(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "{file}"],
        languages=["python"],
    )
    ctx = CascadeContext(language="python", log_file=log)
    result = stage.attempt(_finding(path="nonexistent.py"), tmp_path, ctx)
    assert not result.success
    assert result.error == "file not found"


def test_default_linter_stages_returns_five():
    stages = default_linter_stages()
    assert len(stages) == 5
    names = [s.name for s in stages]
    assert "ruff-fix" in names
    assert "ruff-format" in names
    assert "pyupgrade" in names
    assert "autoflake" in names
    assert "isort" in names


def test_default_linter_stages_all_tier_1():
    for stage in default_linter_stages():
        assert stage.tier == 1


def test_linter_stage_with_glob_rule_pattern(tmp_path):
    stage = LinterFixStage(
        name="test-linter",
        command=["ruff", "{file}"],
        languages=["python"],
        applicable_rules={"ruff-*"},
    )
    ctx = CascadeContext(language="python", log_file=tmp_path / "log.txt")
    with patch("shutil.which", return_value="/usr/bin/ruff"):
        assert stage.can_handle(_finding(rule="ruff-UP001"), ctx)
        assert stage.can_handle(_finding(rule="ruff-F401"), ctx)
        assert not stage.can_handle(_finding(rule="broad-except"), ctx)


# --- RecipeCascadeStage tests ---


def test_recipe_stage_can_handle_no_engine(tmp_path):
    stage = RecipeCascadeStage()
    ctx = CascadeContext(recipe_engine=None, log_file=tmp_path / "log.txt")
    assert not stage.can_handle(_finding(), ctx)


def test_recipe_stage_can_handle_with_matching_recipe(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    engine = MagicMock()
    engine.match.return_value = MagicMock(id="test-recipe")
    stage = RecipeCascadeStage()
    ctx = CascadeContext(
        recipe_engine=engine,
        language="python",
        worktree=tmp_path,
        log_file=tmp_path / "log.txt",
    )
    assert stage.can_handle(_finding(), ctx)


def test_recipe_stage_can_handle_no_match(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    engine = MagicMock()
    engine.match.return_value = None
    stage = RecipeCascadeStage()
    ctx = CascadeContext(
        recipe_engine=engine,
        language="python",
        worktree=tmp_path,
        log_file=tmp_path / "log.txt",
    )
    assert not stage.can_handle(_finding(), ctx)


def test_recipe_stage_attempt_success(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    recipe = MagicMock(id="test-recipe")
    engine = MagicMock()
    engine.match.return_value = recipe
    engine.apply.return_value = MagicMock(
        success=True, files_changed=["src/app.py"], validation_passed=True, error=None
    )
    stage = RecipeCascadeStage()
    ctx = CascadeContext(
        recipe_engine=engine, language="python", worktree=tmp_path, log_file=log
    )
    result = stage.attempt(_finding(), tmp_path, ctx)
    assert result.success
    assert "recipe:test-recipe" in result.stage_name


def test_recipe_stage_attempt_failure(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    recipe = MagicMock(id="test-recipe")
    engine = MagicMock()
    engine.match.return_value = recipe
    engine.apply.return_value = MagicMock(
        success=False, files_changed=[], validation_passed=False, error="apply failed"
    )
    stage = RecipeCascadeStage()
    ctx = CascadeContext(
        recipe_engine=engine, language="python", worktree=tmp_path, log_file=log
    )
    result = stage.attempt(_finding(), tmp_path, ctx)
    assert not result.success
    assert result.error == "apply failed"


def test_recipe_stage_rollback(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    stage = RecipeCascadeStage()
    with patch("bluei.engine.cascade.run_capture") as mock_rc:
        stage.rollback(_finding(path="src/app.py"), tmp_path)
        mock_rc.assert_called_once()


# --- PatternReplayCascadeStage tests ---


def test_pattern_replay_stage_can_handle_no_store(tmp_path):
    stage = PatternReplayCascadeStage()
    ctx = CascadeContext(pattern_store=None, log_file=tmp_path / "log.txt")
    assert not stage.can_handle(_finding(), ctx)


def test_pattern_replay_stage_can_handle_with_pattern(tmp_path):
    store = MagicMock()
    pattern = MagicMock(confidence=0.90)
    store.lookup.return_value = pattern
    stage = PatternReplayCascadeStage()
    ctx = CascadeContext(pattern_store=store, log_file=tmp_path / "log.txt")
    with patch("bluei.engine.pattern_replay.normalize_snippet", return_value="snip"):
        assert stage.can_handle(_finding(), ctx)


def test_pattern_replay_stage_can_handle_low_confidence(tmp_path):
    store = MagicMock()
    pattern = MagicMock(confidence=0.50)
    store.lookup.return_value = pattern
    stage = PatternReplayCascadeStage(confidence_threshold=0.85)
    ctx = CascadeContext(pattern_store=store, log_file=tmp_path / "log.txt")
    with patch("bluei.engine.pattern_replay.normalize_snippet", return_value="snip"):
        assert not stage.can_handle(_finding(), ctx)


def test_pattern_replay_stage_attempt_success(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    store = MagicMock()
    stage = PatternReplayCascadeStage()
    ctx = CascadeContext(pattern_store=store, baseline_results={}, log_file=log)
    with patch(
        "bluei.engine.pattern_replay.try_replay", return_value=(True, "pid-123")
    ):
        result = stage.attempt(_finding(), tmp_path, ctx)
    assert result.success
    assert result.pattern_id == "pid-123"


def test_pattern_replay_stage_attempt_failure(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    store = MagicMock()
    stage = PatternReplayCascadeStage()
    ctx = CascadeContext(pattern_store=store, baseline_results={}, log_file=log)
    with patch("bluei.engine.pattern_replay.try_replay", return_value=(False, None)):
        result = stage.attempt(_finding(), tmp_path, ctx)
    assert not result.success


def test_pattern_replay_stage_rollback(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    stage = PatternReplayCascadeStage()
    with patch("bluei.engine.cascade.run_capture") as mock_rc:
        stage.rollback(_finding(path="src/app.py"), tmp_path)
        mock_rc.assert_called_once()


# --- ASTTransformCascadeStage tests ---


def test_ast_stage_can_handle_non_python(tmp_path):
    stage = ASTTransformCascadeStage()
    ctx = CascadeContext(language="typescript", log_file=tmp_path / "log.txt")
    assert not stage.can_handle(_finding(), ctx)


def test_ast_stage_can_handle_python_with_transform(tmp_path):
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    mock_transformer = MagicMock()
    mock_transformer.get_transform.return_value = MagicMock()
    stage._transformer = mock_transformer
    ctx = CascadeContext(language="python", log_file=tmp_path / "log.txt")
    assert stage.can_handle(_finding(rule="broad-except"), ctx)


def test_ast_stage_can_handle_python_no_transform(tmp_path):
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    mock_transformer = MagicMock()
    mock_transformer.get_transform.return_value = None
    stage._transformer = mock_transformer
    ctx = CascadeContext(language="python", log_file=tmp_path / "log.txt")
    assert not stage.can_handle(_finding(rule="unknown-rule"), ctx)


def test_ast_stage_attempt_success(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("except:\n    pass\n")
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    mock_transformer = MagicMock()
    mock_transformer.get_transform.return_value = MagicMock(id="fix-broad-except")
    mock_transformer.apply.return_value = MagicMock(
        success=True, source="except Exception:\n    pass\n"
    )
    stage._transformer = mock_transformer
    ctx = CascadeContext(language="python", log_file=log)
    with patch("bluei.engine.validation.verify_fix_closed", return_value=True):
        result = stage.attempt(
            _finding(rule="broad-except", path="src/app.py"), tmp_path, ctx
        )
    assert result.success
    assert result.pattern_id == "fix-broad-except"


def test_ast_stage_attempt_transform_fails(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("except:\n    pass\n")
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    from unittest.mock import PropertyMock

    mock_transformer = MagicMock()
    mock_result = MagicMock()
    mock_result.success = False
    mock_result.error = "node not found"
    mock_transformer.get_transform.return_value = MagicMock(id="fix-broad-except")
    mock_transformer.apply.return_value = mock_result
    stage._transformer = mock_transformer
    ctx = CascadeContext(language="python", log_file=log)
    result = stage.attempt(
        _finding(rule="broad-except", path="src/app.py"), tmp_path, ctx
    )
    assert not result.success


def test_ast_stage_rollback(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    stage = ASTTransformCascadeStage()
    with patch("bluei.engine.cascade.run_capture") as mock_rc:
        stage.rollback(_finding(path="src/app.py"), tmp_path)
        mock_rc.assert_called_once()


# --- default_cascade_stages tests ---


def test_default_cascade_stages_includes_all():
    stages = default_cascade_stages()
    names = [s.name for s in stages]
    assert "ruff-fix" in names
    assert "ruff-format" in names
    assert "recipe-engine" in names
    assert "pattern-replay" in names
    assert "ast-transform" in names


def test_default_cascade_stages_linters_are_tier_1():
    for stage in default_cascade_stages():
        if "ruff" in stage.name or stage.name in ("pyupgrade", "autoflake", "isort"):
            assert stage.tier == 1


def test_default_cascade_stages_engines_are_tier_2():
    for stage in default_cascade_stages():
        if stage.name in ("recipe-engine", "pattern-replay", "ast-transform"):
            assert stage.tier == 2


# --- E2E Integration Tests ---


def test_e2e_classify_unknown_rule_returns_cascade_fix():
    from bluei.engine.reforge import RefactorClass, classify_finding

    f = _finding(rule="totally-unknown-rule-xyz")
    rc = classify_finding(f)
    assert rc == RefactorClass.CASCADE_FIX


def test_e2e_classify_refactor_class_still_direct():
    from bluei.engine.reforge import (
        RefactorClass,
        classify_finding,
        REFACTOR_CLASS_RULES,
    )

    if not REFACTOR_CLASS_RULES:
        pytest.skip("no REFACTOR_CLASS_RULES defined")
    rule = list(REFACTOR_CLASS_RULES)[0]
    f = _finding(rule=rule)
    rc = classify_finding(f)
    assert rc == RefactorClass.REFACTOR_CLASS


def test_e2e_cascade_deterministic_only_never_calls_llm(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")

    class DeterministicWin(CascadeStage):
        name = "det-win"
        tier = 1

        def can_handle(self, finding, context):
            return True

        def attempt(self, finding, worktree, context):
            return CascadeResult(
                success=True,
                stage_name=self.name,
                changes_made=[finding.path],
                validation_passed=True,
                latency_ms=5,
            )

    cascade = DeterministicCascade([DeterministicWin()])
    ctx = CascadeContext(log_file=log, allow_llm=False)
    result = cascade.execute(_finding(), tmp_path, ctx)
    assert result.success
    assert result.stage_name == "det-win"


def test_e2e_cascade_deterministic_only_exhausted_no_llm(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")

    cascade = DeterministicCascade([_AlwaysFail()])
    ctx = CascadeContext(log_file=log, allow_llm=False)
    result = cascade.execute(_finding(), tmp_path, ctx)
    assert not result.success
    assert result.stage_name == "cascade_exhausted"


def test_e2e_cascade_rollback_restores_file(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    original = "x = 1\n"
    target.write_text(original)

    class MutateThenFail(CascadeStage):
        name = "mutate-fail"
        tier = 1

        def can_handle(self, finding, context):
            return True

        def attempt(self, finding, worktree, context):
            fp = worktree / finding.path
            fp.write_text("mutated!\n", encoding="utf-8")
            return CascadeResult(
                success=True,
                stage_name=self.name,
                changes_made=[finding.path],
                validation_passed=False,
                latency_ms=1,
            )

        def rollback(self, finding, worktree):
            fp = worktree / finding.path
            if fp.exists():
                from bluei.engine.utils import run_capture

                run_capture(
                    ["git", "checkout", "--", str(fp)], cwd=worktree, timeout=10
                )

    cascade = DeterministicCascade([MutateThenFail()])
    ctx = CascadeContext(log_file=log)
    result = cascade.execute(_finding(path="src/app.py"), tmp_path, ctx)
    assert not result.success


def test_e2e_cascade_telemetry_has_all_fields(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    cascade = DeterministicCascade([_AlwaysFail(), _AlwaysSucceed()])
    cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    log_text = log.read_text()
    assert "cascade-telemetry:" in log_text
    import json

    for line in log_text.splitlines():
        if "cascade-telemetry:" in line:
            data = json.loads(line.split("cascade-telemetry: ", 1)[1])
            assert "finding_id" in data
            assert "stages_tried" in data
            assert "stages_failed" in data
            assert "total_latency_ms" in data
            assert "success" in data
            break


def test_e2e_refactor_class_enum_has_all_values():
    from bluei.engine.reforge import RefactorClass

    values = [e.value for e in RefactorClass]
    assert "cascade_fix" in values
    assert "simple_fix" in values
    assert "contextual_fix" in values
    assert "claude_fix" in values
    assert "refactor_class" in values


def test_e2e_describe_class_all_have_descriptions():
    from bluei.engine.reforge import RefactorClass, describe_class

    for rc in RefactorClass:
        desc = describe_class(rc)
        assert isinstance(desc, str)
        assert len(desc) > 10, f"{rc} description too short: {desc!r}"


def test_e2e_cascade_context_allow_llm_default():
    ctx = CascadeContext()
    assert ctx.allow_llm is True
    ctx_no = CascadeContext(allow_llm=False)
    assert ctx_no.allow_llm is False


def test_e2e_linter_stage_substitutes_file_arg(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    stage = LinterFixStage(
        name="test-sub",
        command=["echo", "{file}"],
        languages=["python"],
    )
    with (
        patch(
            "bluei.engine.cascade.run_capture", return_value=(0, str(target))
        ) as mock_rc,
        patch("bluei.engine.validation.verify_fix_closed", return_value=True),
    ):
        ctx = CascadeContext(language="python", log_file=log)
        stage.attempt(_finding(path="src/app.py"), tmp_path, ctx)
        cmd = mock_rc.call_args[0][0]
        assert str(target) in cmd


def test_e2e_multiple_stages_first_wins(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")

    class Stage1(CascadeStage):
        name = "stage-1"
        tier = 1

        def can_handle(self, f, ctx):
            return True

        def attempt(self, f, w, ctx):
            return CascadeResult(
                success=True,
                stage_name=self.name,
                changes_made=[f.path],
                validation_passed=True,
                latency_ms=1,
            )

    class Stage2(CascadeStage):
        name = "stage-2"
        tier = 2
        attempted = False

        def can_handle(self, f, ctx):
            return True

        def attempt(self, f, w, ctx):
            Stage2.attempted = True
            return CascadeResult(
                success=True,
                stage_name=self.name,
                changes_made=[f.path],
                validation_passed=True,
                latency_ms=1,
            )

    cascade = DeterministicCascade([Stage2(), Stage1()])
    result = cascade.execute(_finding(), tmp_path, CascadeContext(log_file=log))
    assert result.success
    assert result.stage_name == "stage-1"
    assert not Stage2.attempted


# ── Extracted from test_cascade_and_utils_remaining.py ──
# Additional cascade error paths and edge cases


class _NoopStage(CascadeStage):
    name = "noop"
    tier = 1

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        return CascadeResult(success=False, stage_name=self.name, error="noop")


def test_cascade_stage_default_rollback_is_noop(tmp_path):
    stage = _NoopStage()
    result = stage.rollback(_finding(), tmp_path)
    assert result is None


def test_write_telemetry_exception_silent(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")

    def mock_append(log_file, text):
        if "cascade-telemetry:" in text:
            raise RuntimeError("telemetry write failed")

    with patch("bluei.engine.cascade._append_text", side_effect=mock_append):
        cascade = DeterministicCascade([_AlwaysSucceed()])
        ctx = CascadeContext(log_file=log)
        result = cascade.execute(_finding(), tmp_path, ctx)

    assert result.success


def test_recipe_can_handle_exception_returns_false(tmp_path):
    engine = MagicMock()
    engine.match.side_effect = RuntimeError("boom")
    stage = RecipeCascadeStage()
    ctx = CascadeContext(
        recipe_engine=engine,
        language="python",
        worktree=tmp_path,
        log_file=tmp_path / "log.txt",
    )
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    assert stage.can_handle(_finding(), ctx) is False


def test_recipe_attempt_no_matching_recipe(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    engine = MagicMock()
    engine.match.return_value = None
    stage = RecipeCascadeStage()
    ctx = CascadeContext(
        recipe_engine=engine,
        language="python",
        worktree=tmp_path,
        log_file=log,
    )
    result = stage.attempt(_finding(), tmp_path, ctx)
    assert not result.success
    assert result.error == "no matching recipe"


def test_recipe_attempt_exception(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    recipe = MagicMock(id="test-recipe")
    engine = MagicMock()
    engine.match.return_value = recipe
    engine.apply.side_effect = RuntimeError("apply exploded")
    stage = RecipeCascadeStage()
    ctx = CascadeContext(
        recipe_engine=engine,
        language="python",
        worktree=tmp_path,
        log_file=log,
    )
    result = stage.attempt(_finding(), tmp_path, ctx)
    assert not result.success
    assert "apply exploded" in result.error


def test_pattern_replay_structural_lookup_succeeds(tmp_path):
    store = MagicMock()
    low_conf = MagicMock(confidence=0.50)
    high_conf = MagicMock(confidence=0.90)
    store.lookup.return_value = low_conf
    store.lookup_structural.return_value = high_conf
    stage = PatternReplayCascadeStage()
    ctx = CascadeContext(pattern_store=store, log_file=tmp_path / "log.txt")
    with patch("bluei.engine.pattern_store.normalize_snippet", return_value="snip"):
        assert stage.can_handle(_finding(), ctx) is True


def test_pattern_replay_fuzzy_lookup_succeeds(tmp_path):
    store = MagicMock()
    low_conf = MagicMock(confidence=0.50)
    store.lookup.return_value = low_conf
    store.lookup_structural.return_value = low_conf
    store.lookup_fuzzy.return_value = MagicMock(confidence=0.90)
    stage = PatternReplayCascadeStage()
    ctx = CascadeContext(pattern_store=store, log_file=tmp_path / "log.txt")
    with patch("bluei.engine.pattern_store.normalize_snippet", return_value="snip"):
        assert stage.can_handle(_finding(), ctx) is True


def test_pattern_replay_fuzzy_lookup_low_confidence(tmp_path):
    store = MagicMock()
    low_conf = MagicMock(confidence=0.50)
    store.lookup.return_value = low_conf
    store.lookup_structural.return_value = low_conf
    store.lookup_fuzzy.return_value = low_conf
    stage = PatternReplayCascadeStage(confidence_threshold=0.85)
    ctx = CascadeContext(pattern_store=store, log_file=tmp_path / "log.txt")
    with patch("bluei.engine.pattern_store.normalize_snippet", return_value="snip"):
        assert stage.can_handle(_finding(), ctx) is False


def test_pattern_replay_attempt_exception(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    store = MagicMock()
    stage = PatternReplayCascadeStage()
    ctx = CascadeContext(pattern_store=store, baseline_results={}, log_file=log)
    with patch(
        "bluei.engine.pattern_replay.try_replay",
        side_effect=RuntimeError("replay died"),
    ):
        result = stage.attempt(_finding(), tmp_path, ctx)
    assert not result.success
    assert "replay died" in result.error


def test_ast_ensure_init_import_failure(tmp_path):
    stage = ASTTransformCascadeStage()
    assert stage._initialized is False
    with patch.dict("sys.modules", {"bluei.engine.ast_engine.transformer": None}):
        ctx = CascadeContext(language="python", log_file=tmp_path / "log.txt")
        result = stage.can_handle(_finding(), ctx)
    assert result is False
    assert stage._initialized is True
    assert stage._transformer is None


def test_ast_can_handle_transformer_none(tmp_path):
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    stage._transformer = None
    ctx = CascadeContext(language="python", log_file=tmp_path / "log.txt")
    assert stage.can_handle(_finding(), ctx) is False


def test_ast_attempt_transformer_none(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    stage._transformer = None
    ctx = CascadeContext(language="python", log_file=log)
    result = stage.attempt(_finding(), tmp_path, ctx)
    assert not result.success
    assert result.error == "transformer not available"


def test_ast_attempt_no_transform_for_rule(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    mock_transformer = MagicMock()
    mock_transformer.get_transform.return_value = None
    stage._transformer = mock_transformer
    ctx = CascadeContext(language="python", log_file=log)
    result = stage.attempt(_finding(rule="unknown-rule"), tmp_path, ctx)
    assert not result.success
    assert result.error == "no transform for rule"


def test_ast_attempt_file_not_found(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    mock_transformer = MagicMock()
    mock_transformer.get_transform.return_value = MagicMock(id="fix-thing")
    stage._transformer = mock_transformer
    ctx = CascadeContext(language="python", log_file=log)
    result = stage.attempt(_finding(path="nonexistent.py"), tmp_path, ctx)
    assert not result.success
    assert result.error == "file not found"


def test_ast_attempt_exception_during_transform(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("")
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    stage = ASTTransformCascadeStage()
    stage._initialized = True
    mock_transformer = MagicMock()
    mock_transformer.get_transform.return_value = MagicMock(id="fix-thing")
    mock_transformer.apply.side_effect = RuntimeError("AST explosion")
    stage._transformer = mock_transformer
    ctx = CascadeContext(language="python", log_file=log)
    result = stage.attempt(
        _finding(rule="broad-except", path="src/app.py"), tmp_path, ctx
    )
    assert not result.success
    assert "AST explosion" in result.error


def test_composite_can_handle_exception_returns_false(tmp_path):
    mock_ps = MagicMock()
    mock_ps.store_path = tmp_path / "patterns.jsonl"
    stage = CompositePatternCascadeStage()
    ctx = CascadeContext(pattern_store=mock_ps)
    with patch(
        "bluei.engine.composite_pattern.CompositePatternStore",
        side_effect=RuntimeError("store init failed"),
    ):
        result = stage.can_handle(_finding(), ctx)
    assert result is False


def test_composite_attempt_exception_returns_failure(tmp_path):
    mock_ps = MagicMock()
    mock_ps.store_path = tmp_path / "patterns.jsonl"
    log = tmp_path / "log.txt"
    log.write_text("")
    stage = CompositePatternCascadeStage()
    ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
    with patch(
        "bluei.engine.composite_pattern.CompositePatternStore",
        side_effect=RuntimeError("store boom"),
    ):
        result = stage.attempt(_finding(), tmp_path, ctx)
    assert not result.success
    assert "store boom" in result.error
