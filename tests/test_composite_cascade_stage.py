import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.cascade import (
    CascadeContext,
    CascadeResult,
    CompositePatternCascadeStage,
    DeterministicCascade,
)
from bluei.engine.composite_pattern import (
    CompositePattern,
    CompositePatternStep,
    CompositePatternStore,
)
from bluei.engine.models import Finding


def _make_step(
    order: int = 0,
    scope: str = "finding_location",
    match: str = "old",
    replacement: str = "new",
    file_pattern: str | None = None,
    description: str = "test step",
) -> CompositePatternStep:
    return CompositePatternStep(
        order=order,
        description=description,
        match=match,
        replacement=replacement,
        scope=scope,
        file_pattern=file_pattern,
    )


def _make_pattern(
    rule: str = "ruff-b904",
    steps: list | None = None,
    confidence: float = 0.8,
    pattern_id: str = "comp-test123",
    language: str = "python",
) -> CompositePattern:
    return CompositePattern(
        pattern_id=pattern_id,
        rule=rule,
        language=language,
        steps=steps or [_make_step()],
        confidence=confidence,
        source="test",
    )


def _setup_store_with_pattern(
    tmp_path: Path,
    pattern: CompositePattern,
) -> CompositePatternStore:
    store_path = tmp_path / "composite_patterns.jsonl"
    store = CompositePatternStore(store_path)
    store.append(pattern)
    return store


def _mock_pattern_store(store_path: Path) -> MagicMock:
    mock = MagicMock()
    mock.store_path = store_path
    return mock


class TestCompositePatternCascadeStageCanHandle:
    def test_returns_false_when_pattern_store_is_none(self, make_finding):
        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=None)
        finding = make_finding()
        assert stage.can_handle(finding, ctx) is False

    def test_returns_false_when_pattern_store_has_no_store_path(self, make_finding):
        stage = CompositePatternCascadeStage()
        mock_store = MagicMock()
        del mock_store.store_path
        ctx = CascadeContext(pattern_store=mock_store)
        finding = make_finding()
        assert stage.can_handle(finding, ctx) is False

    def test_returns_false_when_no_composite_pattern_exists_for_rule(self, make_finding, tmp_path):
        store_path = tmp_path / "composite_patterns.jsonl"
        CompositePatternStore(store_path)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps)
        finding = make_finding(rule="nonexistent-rule")
        assert stage.can_handle(finding, ctx) is False

    def test_returns_false_when_pattern_below_confidence_threshold(self, make_finding, tmp_path):
        pattern = _make_pattern(rule="ruff-b904", confidence=0.3)
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        stage = CompositePatternCascadeStage(confidence_threshold=0.7)
        ctx = CascadeContext(pattern_store=mock_ps)
        finding = make_finding(rule="ruff-b904")
        assert stage.can_handle(finding, ctx) is False

    def test_returns_true_when_pattern_exists_and_above_threshold(self, make_finding, tmp_path):
        pattern = _make_pattern(rule="ruff-b904", confidence=0.85)
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        stage = CompositePatternCascadeStage(confidence_threshold=0.7)
        ctx = CascadeContext(pattern_store=mock_ps)
        finding = make_finding(rule="ruff-b904")
        assert stage.can_handle(finding, ctx) is True

    def test_returns_true_at_exact_confidence_threshold(self, make_finding, tmp_path):
        pattern = _make_pattern(rule="ruff-b904", confidence=0.7)
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        stage = CompositePatternCascadeStage(confidence_threshold=0.7)
        ctx = CascadeContext(pattern_store=mock_ps)
        finding = make_finding(rule="ruff-b904")
        assert stage.can_handle(finding, ctx) is True


class TestCompositePatternCascadeStageAttemptSuccess:
    def test_success_path_applies_pattern_and_resolves(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("raise Error\n", encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="ruff-b904",
            steps=[
                _make_step(
                    order=0, match="raise Error", replacement="raise Error from cause"
                )
            ],
            confidence=0.85,
            pattern_id="comp-applied-ok",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        finding = make_finding(
            rule="ruff-b904", path="src/main.py", snippet="raise Error"
        )

        with patch("bluei.engine.validation.verify_fix_closed", return_value=True):
            result = stage.attempt(finding, worktree, ctx)

        assert result.success is True
        assert result.stage_name == "composite-pattern"
        assert result.pattern_id == "comp-applied-ok"
        assert result.validation_passed is True
        assert "src/main.py" in result.changes_made
        assert result.error is None
        assert f.read_text(encoding="utf-8") == "raise Error from cause\n"

    def test_success_records_positive_result_in_store(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("raise Error\n", encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="ruff-b904",
            steps=[
                _make_step(
                    order=0, match="raise Error", replacement="raise Error from cause"
                )
            ],
            confidence=0.85,
            pattern_id="comp-rec-ok",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        finding = make_finding(
            rule="ruff-b904", path="src/main.py", snippet="raise Error"
        )

        with patch("bluei.engine.validation.verify_fix_closed", return_value=True):
            stage.attempt(finding, worktree, ctx)

        comp_store = CompositePatternStore(tmp_path / "composite_patterns.jsonl")
        stored = comp_store.get("comp-rec-ok")
        assert stored is not None
        assert stored.success_count == 1

    def test_multi_step_pattern_applies_all_steps(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f1 = worktree / "src" / "a.py"
        f2 = worktree / "src" / "b.py"
        f1.parent.mkdir(parents=True, exist_ok=True)
        f2.parent.mkdir(parents=True, exist_ok=True)
        f1.write_text("import old_mod\n", encoding="utf-8")
        f2.write_text("from old_mod import func\n", encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="import-rename",
            steps=[
                _make_step(
                    order=0,
                    scope="finding_location",
                    match="old_mod",
                    replacement="new_mod",
                ),
                _make_step(
                    order=1,
                    scope="related_file",
                    match="old_mod",
                    replacement="new_mod",
                    file_pattern="src/b.py",
                ),
            ],
            confidence=0.9,
            pattern_id="comp-multi",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        finding = make_finding(
            rule="import-rename", path="src/a.py", snippet="import old_mod"
        )

        with patch("bluei.engine.validation.verify_fix_closed", return_value=True):
            result = stage.attempt(finding, worktree, ctx)

        assert result.success is True
        assert len(result.changes_made) == 2
        assert "new_mod" in f1.read_text(encoding="utf-8")
        assert "new_mod" in f2.read_text(encoding="utf-8")


class TestCompositePatternCascadeStageAttemptFailure:
    def test_attempt_returns_failure_when_no_store_path(self, make_finding):
        stage = CompositePatternCascadeStage()
        mock_store = MagicMock()
        del mock_store.store_path
        ctx = CascadeContext(pattern_store=mock_store)
        finding = make_finding()
        result = stage.attempt(finding, Path("/tmp"), ctx)
        assert result.success is False
        assert result.stage_name == "composite-pattern"
        assert "no pattern store path" in (result.error or "")

    def test_attempt_returns_failure_when_pattern_not_found_in_store(self, make_finding, tmp_path):
        store_path = tmp_path / "composite_patterns.jsonl"
        CompositePatternStore(store_path)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")

        stage = CompositePatternCascadeStage()
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        finding = make_finding(rule="missing-rule")
        result = stage.attempt(finding, Path("/tmp"), ctx)
        assert result.success is False
        assert "no composite pattern found" in (result.error or "")

    def test_attempt_returns_failure_when_pattern_does_not_match_file(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("completely unrelated content\n", encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="ruff-b904",
            steps=[
                _make_step(
                    order=0, match="raise Error", replacement="raise Error from cause"
                )
            ],
            confidence=0.85,
            pattern_id="comp-nomatch",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        finding = make_finding(
            rule="ruff-b904", path="src/main.py", snippet="raise Error"
        )

        with patch("bluei.engine.validation.verify_fix_closed", return_value=False):
            result = stage.attempt(finding, worktree, ctx)

        assert result.success is False
        assert "fix did not resolve finding" in (result.error or "")
        assert result.stage_name == "composite-pattern"

    def test_attempt_returns_failure_when_apply_regex_is_invalid(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("some code\n", encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="bad-regex-rule",
            steps=[_make_step(order=0, match="[invalid", replacement="new")],
            confidence=0.85,
            pattern_id="comp-badregex",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        finding = make_finding(
            rule="bad-regex-rule", path="src/main.py", snippet="some code"
        )

        result = stage.attempt(finding, worktree, ctx)

        assert result.success is False
        assert "regex error" in (result.error or "")

    def test_attempt_records_failure_result_in_store(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("unrelated\n", encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="ruff-b904",
            steps=[
                _make_step(
                    order=0, match="raise Error", replacement="raise Error from cause"
                )
            ],
            confidence=0.85,
            pattern_id="comp-failrec",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        finding = make_finding(
            rule="ruff-b904", path="src/main.py", snippet="raise Error"
        )

        with patch("bluei.engine.validation.verify_fix_closed", return_value=False):
            result = stage.attempt(finding, worktree, ctx)

        assert result.success is False
        comp_store = CompositePatternStore(tmp_path / "composite_patterns.jsonl")
        stored = comp_store.get("comp-failrec")
        assert stored is not None
        assert stored.failure_count >= 1

    def test_attempt_returns_failure_when_fix_does_not_resolve(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("raise Error\n", encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="ruff-b904",
            steps=[
                _make_step(
                    order=0, match="raise Error", replacement="raise Error from cause"
                )
            ],
            confidence=0.85,
            pattern_id="comp-unresolved",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        finding = make_finding(
            rule="ruff-b904", path="src/main.py", snippet="raise Error"
        )

        with patch("bluei.engine.validation.verify_fix_closed", return_value=False):
            result = stage.attempt(finding, worktree, ctx)

        assert result.success is False
        assert "fix did not resolve finding" in (result.error or "")


class TestCompositePatternCascadeStageRollback:
    def test_rollback_restores_file_via_git_checkout(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        original = "raise Error\n"
        f.write_text(original, encoding="utf-8")
        git_commit_all(worktree)

        f.write_text("raise Error from cause\n", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        finding = make_finding(rule="ruff-b904", path="src/main.py")
        stage.rollback(finding, worktree)

        assert f.read_text(encoding="utf-8") == original

    def test_rollback_no_error_when_file_does_not_exist(self, make_finding, tmp_path):
        stage = CompositePatternCascadeStage()
        finding = make_finding(path="nonexistent.py")
        stage.rollback(finding, tmp_path)

    def test_rollback_uses_git_checkout_command(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("original\n", encoding="utf-8")
        git_commit_all(worktree)
        f.write_text("modified\n", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        finding = make_finding(path="src/main.py")

        with patch("bluei.engine.cascade.run_capture") as mock_rc:
            stage.rollback(finding, worktree)
            mock_rc.assert_called_once()
            cmd = mock_rc.call_args[0][0]
            assert "git" in cmd
            assert "checkout" in cmd
            assert "--" in cmd


class TestCompositePatternCascadeStageDefaults:
    def test_stage_name(self):
        stage = CompositePatternCascadeStage()
        assert stage.name == "composite-pattern"

    def test_stage_tier(self):
        stage = CompositePatternCascadeStage()
        assert stage.tier == 2

    def test_stage_cost_is_zero(self):
        stage = CompositePatternCascadeStage()
        assert stage.estimated_cost == 0.0

    def test_default_confidence_threshold(self):
        stage = CompositePatternCascadeStage()
        assert stage.confidence_threshold == 0.7

    def test_custom_confidence_threshold(self):
        stage = CompositePatternCascadeStage(confidence_threshold=0.9)
        assert stage.confidence_threshold == 0.9


class TestCompositePatternCascadeStageInCascade:
    def test_cascade_uses_composite_stage_on_success(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("raise Error\n", encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="ruff-b904",
            steps=[
                _make_step(
                    order=0, match="raise Error", replacement="raise Error from cause"
                )
            ],
            confidence=0.85,
            pattern_id="comp-cascade-ok",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        cascade = DeterministicCascade([stage])

        finding = make_finding(
            rule="ruff-b904", path="src/main.py", snippet="raise Error"
        )

        with patch("bluei.engine.validation.verify_fix_closed", return_value=True):
            result = cascade.execute(finding, worktree, ctx)

        assert result.success is True
        assert result.stage_name == "composite-pattern"
        assert result.pattern_id == "comp-cascade-ok"

    def test_cascade_skips_composite_stage_when_no_patterns(self, make_finding, tmp_path):
        store_path = tmp_path / "composite_patterns.jsonl"
        CompositePatternStore(store_path)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        cascade = DeterministicCascade([stage])

        finding = make_finding(rule="no-patterns-for-this")

        result = cascade.execute(finding, tmp_path, ctx)

        assert result.success is False
        assert result.stage_name == "cascade_exhausted"

    def test_cascade_rolls_back_composite_on_validation_failure(self, make_finding, git_repo, git_commit_all, tmp_path):
        worktree = git_repo
        f = worktree / "src" / "main.py"
        f.parent.mkdir(parents=True)
        original = "raise Error\n"
        f.write_text(original, encoding="utf-8")
        git_commit_all(worktree)

        pattern = _make_pattern(
            rule="ruff-b904",
            steps=[
                _make_step(
                    order=0, match="raise Error", replacement="raise Error from cause"
                )
            ],
            confidence=0.85,
            pattern_id="comp-valfail",
        )
        _setup_store_with_pattern(tmp_path, pattern)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        cascade = DeterministicCascade([stage])

        finding = make_finding(
            rule="ruff-b904", path="src/main.py", snippet="raise Error"
        )

        with patch("bluei.engine.validation.verify_fix_closed", return_value=False):
            result = cascade.execute(finding, worktree, ctx)

        assert result.success is False
        stage.rollback(finding, worktree)
        assert f.read_text(encoding="utf-8") == original

    def test_cascade_telemetry_includes_composite_stage(self, make_finding, tmp_path):
        store_path = tmp_path / "composite_patterns.jsonl"
        CompositePatternStore(store_path)
        mock_ps = _mock_pattern_store(tmp_path / "patterns.jsonl")
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")

        stage = CompositePatternCascadeStage()
        ctx = CascadeContext(pattern_store=mock_ps, log_file=log)
        cascade = DeterministicCascade([stage])

        finding = make_finding(rule="nonexistent")

        cascade.execute(finding, tmp_path, ctx)

        log_text = log.read_text()
        assert "composite-pattern" in log_text
