"""test_e2e_pipeline.py — End-to-end pipeline tests: classify → cascade → fix → validate → extract → store."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.cascade import (
    CascadeContext,
    CascadeResult,
    CascadeStage,
    DeterministicCascade,
    PatternReplayCascadeStage,
    RecipeCascadeStage,
    default_cascade_stages,
)
from bluei.engine.lifecycle import (
    _extract_fix_pattern,
    apply_cascade_fix,
)
from bluei.engine.models import Finding
from bluei.engine.pattern_store import FixPattern, FixPatternStore
from bluei.engine.pattern_replay import try_replay, _clear_pattern_cache
from bluei.engine.reforge import RefactorClass, classify_finding


def _seed_pattern(
    store: FixPatternStore,
    rule: str,
    before: str,
    after: str,
    target_confidence: float = 0.95,
    **kwargs,
) -> str:
    pattern = FixPattern(
        pattern_id="",
        rule=rule,
        language=kwargs.get("language", "python"),
        file_path=kwargs.get("file_path", "src/main.py"),
        before_snippet=before,
        after_snippet=after,
        diff_patch="",
        confidence=target_confidence,
        success_count=kwargs.get("success_count", 5),
        source=kwargs.get("source", "autofix"),
        source_finding_ids=kwargs.get("source_finding_ids", []),
    )
    pid = store.append(pattern)
    # Confidence is now preserved by _prepare_new_pattern (no longer reset
    # to INITIAL_CONFIDENCE), so no update_confidence adjustment is needed.
    return pid


def _setup_repo_with_unused_import(repo: Path, git_commit_all_fn) -> tuple:

    src = repo / "src"
    src.mkdir()
    py_file = src / "main.py"
    py_file.write_text(
        "import os\nimport sys\n\ndef hello():\n    print(sys.version)\n",
        encoding="utf-8",
    )
    git_commit_all_fn(repo)
    return repo, py_file


def _setup_repo_with_broad_except(repo: Path, git_commit_all_fn) -> tuple:

    src = repo / "src"
    src.mkdir()
    py_file = src / "main.py"
    py_file.write_text("try:\n    do_stuff()\nexcept:\n    pass\n", encoding="utf-8")
    git_commit_all_fn(repo)
    return repo, py_file


class TestClassifyRouting:
    def test_unknown_rule_routes_to_cascade_fix(self, make_finding):
        finding = make_finding(rule="totally-unknown-xyz")
        result = classify_finding(finding)
        assert result == RefactorClass.CASCADE_FIX
        assert finding.refactor_class == "cascade_fix"

    def test_xo_max_lines_routes_to_refactor_class(self, make_finding):
        finding = make_finding(rule="xo-max-lines")
        result = classify_finding(finding)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_test_coverage_routes_to_claude_fix(self, make_finding):
        finding = make_finding(rule="test-coverage-branch")
        result = classify_finding(finding)
        assert result == RefactorClass.CLAUDE_FIX

    def test_ruff_safe_autofix_routes_to_simple_fix(self, make_finding):
        finding = make_finding(rule="ruff-F401", safe_to_autofix=True)
        result = classify_finding(finding)
        assert result == RefactorClass.SIMPLE_FIX

    def test_ruff_unsafe_routes_to_simple_fix_via_recipe(self, make_finding):
        finding = make_finding(rule="ruff-F401", safe_to_autofix=False)
        result = classify_finding(finding)
        assert result == RefactorClass.SIMPLE_FIX

    def test_classify_sets_refactor_class_on_finding(self, make_finding):
        finding = make_finding(rule="unknown-rule")
        assert finding.refactor_class is None
        classify_finding(finding)
        assert finding.refactor_class == "cascade_fix"


class TestFullPipelinePatternReplay:
    def test_pattern_replay_removes_unused_import(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        _clear_pattern_cache()
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        pattern_store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(pattern_store_path)
        _seed_pattern(
            store,
            "ruff-F401",
            "import os\nimport sys",
            "import sys",
            target_confidence=0.95,
        )

        finding = make_finding(
            rule="ruff-F401",
            path="src/main.py",
            snippet="import os\nimport sys",
            line=1,
        )

        replayed, pattern_id = try_replay(repo, finding, store, {}, log_file)
        assert replayed is True
        assert pattern_id is not None

        fixed_content = py_file.read_text(encoding="utf-8")
        assert "import os" not in fixed_content
        assert "import sys" in fixed_content

        log_text = log_file.read_text(encoding="utf-8")
        assert "pattern-replay-success" in log_text

    def test_pattern_replay_records_success_in_store(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        _clear_pattern_cache()
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        pattern_store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(pattern_store_path)
        pid = _seed_pattern(
            store,
            "ruff-F401",
            "import os\nimport sys",
            "import sys",
            target_confidence=0.95,
            success_count=3,
        )

        finding = make_finding(
            rule="ruff-F401",
            path="src/main.py",
            snippet="import os\nimport sys",
            line=1,
        )
        replayed, returned_pid = try_replay(repo, finding, store, {}, log_file)

        assert replayed is True
        store2 = FixPatternStore(pattern_store_path)
        updated = store2.get_pattern(pid)
        assert updated is not None
        assert updated.success_count == 4


class TestPatternExtractAfterFix:
    def test_extract_pattern_from_git_diff(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_broad_except(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        pattern_store_path = tmp_path / "state" / "patterns.jsonl"

        py_file.write_text(
            "try:\n    do_stuff()\nexcept Exception:\n    pass\n", encoding="utf-8"
        )

        finding = make_finding(
            rule="broad-except",
            path="src/main.py",
            snippet="except:\n    pass",
            line=3,
        )

        _extract_fix_pattern(repo, finding, "autofix", pattern_store_path, log_file)

        store = FixPatternStore(pattern_store_path)
        patterns = store.load_active(rule="broad-except")
        assert len(patterns) >= 1

        pat = patterns[0]
        assert pat.rule == "broad-except"
        assert pat.language == "python"
        assert "except" in pat.before_snippet

        log_text = log_file.read_text(encoding="utf-8")
        assert "pattern-extract:" in log_text

    def test_extract_no_pattern_when_no_diff(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_broad_except(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")
        pattern_store_path = tmp_path / "state" / "patterns.jsonl"

        finding = make_finding(
            rule="broad-except", path="src/main.py", snippet="x", line=1
        )
        _extract_fix_pattern(repo, finding, "autofix", pattern_store_path, log_file)

        store = FixPatternStore(pattern_store_path)
        assert len(store.load_active()) == 0

        log_text = log_file.read_text(encoding="utf-8")
        assert "pattern-extract-skip:" in log_text or "no-pattern" in log_text

    def test_extract_returns_none_when_no_store_path(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_broad_except(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        finding = make_finding(
            rule="broad-except", path="src/main.py", snippet="x", line=1
        )
        _extract_fix_pattern(repo, finding, "autofix", None, log_file)


class TestCascadeFixFullPipeline:
    def test_cascade_with_custom_succeeding_stage(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        class _RemoveUnusedImport(CascadeStage):
            name = "test-fix-unused-import"
            tier = 1

            def can_handle(self, finding, context):
                return finding.rule == "ruff-F401"

            def attempt(self, finding, worktree, context):
                fp = worktree / finding.path
                if not fp.exists():
                    return CascadeResult(
                        success=False, stage_name=self.name, error="file not found"
                    )
                lines = fp.read_text(encoding="utf-8").splitlines(True)
                new_lines = [l for l in lines if l.strip() != "import os"]
                fp.write_text("".join(new_lines), encoding="utf-8")
                return CascadeResult(
                    success=True,
                    stage_name=self.name,
                    changes_made=[finding.path],
                    validation_passed=True,
                    latency_ms=5,
                )

        cascade = DeterministicCascade([_RemoveUnusedImport()])
        ctx = CascadeContext(
            worktree=repo,
            repo_path=repo,
            log_file=log_file,
            language="python",
            allow_llm=False,
        )
        finding = make_finding(
            rule="ruff-F401", path="src/main.py", snippet="import os", line=1
        )
        result = cascade.execute(finding, repo, ctx)

        assert result.success is True
        assert result.stage_name == "test-fix-unused-import"
        fixed = py_file.read_text(encoding="utf-8")
        assert "import os" not in fixed
        assert "import sys" in fixed


class TestCascadeExhaustedPipeline:
    def test_exhausted_no_matching_stages(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        finding = make_finding(
            rule="no-matching-rule-xyz", path="src/main.py", snippet="blah", line=1
        )

        class _OnlyHandlesSpecific(CascadeStage):
            name = "specific-handler"
            tier = 1

            def can_handle(self, finding, context):
                return finding.rule == "ruff-F401"

            def attempt(self, finding, worktree, context):
                return CascadeResult(
                    success=True, stage_name=self.name, validation_passed=True
                )

        cascade = DeterministicCascade([_OnlyHandlesSpecific()])
        ctx = CascadeContext(
            worktree=repo,
            repo_path=repo,
            log_file=log_file,
            language="python",
            allow_llm=False,
        )
        result = cascade.execute(finding, repo, ctx)

        assert result.success is False
        assert result.stage_name == "cascade_exhausted"
        assert "exhausted" in (result.error or "")

        original_content = py_file.read_text(encoding="utf-8")
        assert "import os" in original_content

    def test_exhausted_all_stages_fail(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        class _FailA(CascadeStage):
            name = "fail-a"
            tier = 1

            def can_handle(self, f, ctx):
                return True

            def attempt(self, f, w, ctx):
                return CascadeResult(success=False, stage_name=self.name, error="nope")

        class _FailB(CascadeStage):
            name = "fail-b"
            tier = 2

            def can_handle(self, f, ctx):
                return True

            def attempt(self, f, w, ctx):
                return CascadeResult(
                    success=False, stage_name=self.name, error="also nope"
                )

        cascade = DeterministicCascade([_FailA(), _FailB()])
        ctx = CascadeContext(
            worktree=repo,
            repo_path=repo,
            log_file=log_file,
            language="python",
            allow_llm=False,
        )
        finding = make_finding(
            rule="ruff-F401", path="src/main.py", snippet="import os", line=1
        )
        result = cascade.execute(finding, repo, ctx)

        assert result.success is False
        assert result.stage_name == "cascade_exhausted"

        log_text = log_file.read_text(encoding="utf-8")
        assert "cascade: all stages exhausted" in log_text


class TestRecipeStagePipeline:
    def test_recipe_stage_matches_and_applies(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        mock_recipe = MagicMock(id="recipe-remove-unused-import")
        mock_result = MagicMock(
            success=True,
            files_changed=["src/main.py"],
            validation_passed=True,
            error=None,
        )

        mock_engine = MagicMock()
        mock_engine.match.return_value = mock_recipe
        mock_engine.apply.return_value = mock_result

        finding = make_finding(
            rule="ruff-F401", path="src/main.py", snippet="import os", line=1
        )
        ctx = CascadeContext(
            worktree=repo,
            repo_path=repo,
            log_file=log_file,
            language="python",
            recipe_engine=mock_engine,
            allow_llm=False,
        )

        stage = RecipeCascadeStage()
        assert stage.can_handle(finding, ctx)

        result = stage.attempt(finding, repo, ctx)
        assert result.success is True
        assert "recipe:" in result.stage_name
        mock_engine.apply.assert_called_once()

    def test_recipe_stage_skipped_when_no_engine(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        ctx = CascadeContext(
            worktree=repo,
            repo_path=repo,
            log_file=log_file,
            language="python",
            recipe_engine=None,
            allow_llm=False,
        )
        stage = RecipeCascadeStage()
        finding = make_finding(
            rule="ruff-F401", path="src/main.py", snippet="import os", line=1
        )
        assert stage.can_handle(finding, ctx) is False


class TestDeterministicOnlyMode:
    def test_deterministic_only_no_llm_fallback(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        pattern_store_path = tmp_path / "state" / "patterns.jsonl"

        finding = make_finding(
            rule="no-such-rule-ever", path="src/main.py", snippet="import os", line=1
        )
        classify_finding(finding)

        result = apply_cascade_fix(
            worktree_path=repo,
            finding=finding,
            log_file=log_file,
            pattern_store_path=pattern_store_path,
            deterministic_only=True,
        )

        assert result is False

        log_text = log_file.read_text(encoding="utf-8")
        assert "cascade: all stages exhausted" in log_text
        assert "falling to LLM" not in log_text

    def test_deterministic_only_with_succeeding_stage_via_cascade(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        from bluei.engine.lifecycle import _reset_lifecycle_cache

        _reset_lifecycle_cache()

        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        pattern_store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(pattern_store_path)
        _seed_pattern(
            store,
            "test-direct-fix",
            "import os\nimport sys",
            "import sys",
            target_confidence=0.95,
        )

        finding = make_finding(
            rule="test-direct-fix",
            path="src/main.py",
            snippet="import os\nimport sys",
            line=1,
        )
        classify_finding(finding)

        _clear_pattern_cache()

        result = apply_cascade_fix(
            worktree_path=repo,
            finding=finding,
            log_file=log_file,
            pattern_store_path=pattern_store_path,
            deterministic_only=True,
        )

        assert result is True
        fixed = py_file.read_text(encoding="utf-8")
        assert "import os" not in fixed


class TestPatternStorePersistence:
    def test_store_persist_and_reload(self, make_finding, tmp_path):
        store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(store_path)
        pid = _seed_pattern(store, "ruff-F401", "import os", "", target_confidence=0.9)

        store2 = FixPatternStore(store_path)
        loaded = store2.get_pattern(pid)
        assert loaded is not None
        assert loaded.rule == "ruff-F401"
        assert abs(loaded.confidence - 0.9) < 0.01

    def test_store_lookup_matches_normalized_snippet(self, make_finding, tmp_path):
        store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(store_path)
        _seed_pattern(store, "ruff-F401", "  import os  ", "", target_confidence=0.95)

        found = store.lookup("ruff-F401", "import os")
        assert found is not None
        assert found.confidence >= 0.95

    def test_store_deactivated_pattern_not_returned(self, make_finding, tmp_path):
        store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(store_path)

        pattern = FixPattern(
            pattern_id="",
            rule="ruff-F401",
            language="python",
            file_path="src/main.py",
            before_snippet="import os",
            after_snippet="",
            diff_patch="",
            confidence=0.5,
            source="autofix",
        )
        pid = store.append(pattern)

        store.update_confidence(pid, -0.3)

        store2 = FixPatternStore(store_path)
        found = store2.lookup("ruff-F401", "import os")
        assert found is None


class TestEndToEndFullLoop:
    def test_classify_cascade_pattern_replay_validate_extract(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        _clear_pattern_cache()

        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        pattern_store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(pattern_store_path)
        _seed_pattern(
            store,
            "e2e-unused-import",
            "import os\nimport sys",
            "import sys",
            target_confidence=0.95,
        )

        finding = make_finding(
            rule="e2e-unused-import",
            path="src/main.py",
            snippet="import os\nimport sys",
            line=1,
        )

        rc = classify_finding(finding)
        assert rc == RefactorClass.CASCADE_FIX

        class _PatternReplayStage(CascadeStage):
            name = "pattern-replay-e2e"
            tier = 2

            def can_handle(self, f, ctx):
                return ctx.pattern_store is not None

            def attempt(self, f, w, ctx):
                replayed, pid = try_replay(
                    w, f, ctx.pattern_store, ctx.baseline_results, ctx.log_file
                )
                return CascadeResult(
                    success=replayed,
                    stage_name=self.name,
                    changes_made=[f.path] if replayed else [],
                    validation_passed=replayed,
                    pattern_id=pid,
                )

        cascade = DeterministicCascade([_PatternReplayStage()])
        ctx = CascadeContext(
            worktree=repo,
            repo_path=repo,
            log_file=log_file,
            language="python",
            pattern_store=store,
            allow_llm=False,
        )
        result = cascade.execute(finding, repo, ctx)

        assert result.success is True
        fixed = py_file.read_text(encoding="utf-8")
        assert "import os" not in fixed
        assert "import sys" in fixed

        _extract_fix_pattern(
            repo, finding, "pattern-replay", pattern_store_path, log_file
        )

        store2 = FixPatternStore(pattern_store_path)
        all_patterns = store2.load_active(rule="e2e-unused-import")
        assert len(all_patterns) >= 1

        log_text = log_file.read_text(encoding="utf-8")
        assert "pattern-replay-success" in log_text

    def test_classify_cascade_recipe_match_and_apply(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        finding = make_finding(
            rule="ruff-F401", path="src/main.py", snippet="import os", line=1
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.SIMPLE_FIX or rc == RefactorClass.CASCADE_FIX

        mock_recipe = MagicMock(id="recipe-f401")
        mock_result = MagicMock(
            success=True,
            files_changed=["src/main.py"],
            validation_passed=True,
            error=None,
        )
        mock_engine = MagicMock()
        mock_engine.match.return_value = mock_recipe
        mock_engine.apply.return_value = mock_result

        ctx = CascadeContext(
            worktree=repo,
            repo_path=repo,
            log_file=log_file,
            language="python",
            recipe_engine=mock_engine,
            allow_llm=False,
        )
        stage = RecipeCascadeStage()
        result = stage.attempt(finding, repo, ctx)

        assert result.success is True
        assert "recipe:" in result.stage_name

    def test_classify_cascade_exhausted_deterministic_only(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")
        pattern_store_path = tmp_path / "state" / "patterns.jsonl"

        finding = make_finding(
            rule="phantom-rule-999", path="src/main.py", snippet="import os", line=1
        )
        classify_finding(finding)

        result = apply_cascade_fix(
            worktree_path=repo,
            finding=finding,
            log_file=log_file,
            pattern_store_path=pattern_store_path,
            deterministic_only=True,
        )

        assert result is False
        original = py_file.read_text(encoding="utf-8")
        assert "import os" in original

        log_text = log_file.read_text(encoding="utf-8")
        assert "cascade_exhausted" in log_text or "all stages exhausted" in log_text


class TestEdgeCases:
    def test_fix_for_missing_file_returns_false(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo = git_repo
        dummy = repo / "README.md"
        dummy.write_text("# test\n", encoding="utf-8")
        git_commit_all(repo)

        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")
        pattern_store_path = tmp_path / "state" / "patterns.jsonl"

        finding = make_finding(
            rule="ruff-F401", path="nonexistent.py", snippet="import os", line=1
        )
        classify_finding(finding)

        result = apply_cascade_fix(
            worktree_path=repo,
            finding=finding,
            log_file=log_file,
            pattern_store_path=pattern_store_path,
            deterministic_only=True,
        )
        assert result is False

    def test_pattern_replay_no_match_returns_false(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        _clear_pattern_cache()
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        pattern_store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(pattern_store_path)

        finding = make_finding(
            rule="ruff-F401", path="src/main.py", snippet="import os", line=1
        )
        replayed, pid = try_replay(repo, finding, store, {}, log_file)

        assert replayed is False
        assert pid is None

    def test_pattern_replay_low_confidence_does_not_apply(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        _clear_pattern_cache()
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        pattern_store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(pattern_store_path)

        pattern = FixPattern(
            pattern_id="",
            rule="ruff-F401",
            language="python",
            file_path="src/main.py",
            before_snippet="import os\nimport sys",
            after_snippet="import sys",
            diff_patch="",
            confidence=0.5,
            source="autofix",
        )
        store.append(pattern)

        finding = make_finding(
            rule="ruff-F401",
            path="src/main.py",
            snippet="import os\nimport sys",
            line=1,
        )
        replayed, pid = try_replay(repo, finding, store, {}, log_file)

        assert replayed is False
        original = py_file.read_text(encoding="utf-8")
        assert "import os" in original

    def test_multiple_findings_different_rules(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo = git_repo

        src = repo / "src"
        src.mkdir()
        py_file = src / "main.py"
        py_file.write_text(
            "import os\nimport sys\n\ntry:\n    do_stuff()\nexcept:\n    pass\n\nprint(sys.version)\n",
            encoding="utf-8",
        )
        git_commit_all(repo)

        f1 = make_finding(
            rule="ruff-F401",
            path="src/main.py",
            snippet="import os",
            line=1,
            finding_id="f1",
        )
        f2 = make_finding(
            rule="broad-except",
            path="src/main.py",
            snippet="except:\n    pass",
            line=5,
            finding_id="f2",
        )
        f3 = make_finding(
            rule="xo-max-lines",
            path="src/main.py",
            snippet="long file",
            line=1,
            finding_id="f3",
        )
        f4 = make_finding(
            rule="totally-unknown",
            path="src/main.py",
            snippet="x",
            line=1,
            finding_id="f4",
        )

        assert classify_finding(f1) in (
            RefactorClass.SIMPLE_FIX,
            RefactorClass.CASCADE_FIX,
        )
        assert classify_finding(f2) == RefactorClass.CONTEXTUAL_FIX
        assert classify_finding(f3) == RefactorClass.REFACTOR_CLASS
        assert classify_finding(f4) == RefactorClass.CASCADE_FIX

    def test_cascade_telemetry_logged_on_exhaustion(
        self, make_finding, git_repo, git_commit_all, tmp_path
    ):
        repo, py_file = _setup_repo_with_unused_import(git_repo, git_commit_all)
        log_file = tmp_path / "run.log"
        log_file.write_text("", encoding="utf-8")

        class _AlwaysNo(CascadeStage):
            name = "always-no"
            tier = 1

            def can_handle(self, f, ctx):
                return True

            def attempt(self, f, w, ctx):
                return CascadeResult(success=False, stage_name=self.name, error="nope")

        cascade = DeterministicCascade([_AlwaysNo()])
        ctx = CascadeContext(
            worktree=repo,
            repo_path=repo,
            log_file=log_file,
            language="python",
            allow_llm=False,
        )
        finding = make_finding(
            rule="ruff-F401", path="src/main.py", snippet="import os", line=1
        )
        cascade.execute(finding, repo, ctx)

        log_text = log_file.read_text(encoding="utf-8")
        assert "cascade-telemetry:" in log_text
        for line in log_text.splitlines():
            if "cascade-telemetry:" in line:
                data = json.loads(line.split("cascade-telemetry: ", 1)[1])
                assert data["success"] is False
                assert "always-no" in data["stages_tried"]
                break

    def test_store_update_confidence(self, make_finding, tmp_path):
        store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(store_path)

        pattern = FixPattern(
            pattern_id="",
            rule="ruff-F401",
            language="python",
            file_path="src/main.py",
            before_snippet="import os",
            after_snippet="",
            diff_patch="",
            confidence=0.5,
            source="autofix",
        )
        pid = store.append(pattern)

        store.update_confidence(pid, 0.3)
        store2 = FixPatternStore(store_path)
        updated = store2.get_pattern(pid)
        assert abs(updated.confidence - 0.8) < 0.01

    def test_store_record_replay(self, make_finding, tmp_path):
        store_path = tmp_path / "state" / "patterns.jsonl"
        store = FixPatternStore(store_path)

        pid = _seed_pattern(
            store, "ruff-F401", "import os", "", target_confidence=0.9, success_count=1
        )

        store.record_replay(pid, success=True)
        store2 = FixPatternStore(store_path)
        updated = store2.get_pattern(pid)
        assert updated.success_count >= 2
        assert updated.last_verified_at is not None
