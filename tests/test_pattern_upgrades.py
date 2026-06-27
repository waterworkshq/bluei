"""P4-7: Integration tests for Phase 4 Pattern Replay Upgrades.

Tests cross-cutting scenarios:
1. Cross-repo: publish from repo A → lookup from repo B → apply
2. Fuzzy match: different variable names → match → correct fix
3. Composite: 3-step pattern → all steps applied → rollback on failure
4. Promotion: high-confidence pattern → staged recipe generated
5. Threshold tuning: telemetry feed → threshold adjusted
"""

import json
import subprocess
from pathlib import Path

import pytest

from bluei.engine.composite_pattern import (
    CompositePattern,
    CompositePatternApplier,
    CompositePatternStep,
    CompositePatternStore,
)
from bluei.engine.cascade import (
    CascadeContext,
    CascadeResult,
    CompositePatternCascadeStage,
    DeterministicCascade,
    default_cascade_stages,
)
from bluei.engine.models import Finding
from bluei.engine.governance import PromotionResult
from bluei.engine.pattern_store import FixPattern, FixPatternStore, normalize_snippet
from bluei.engine.shared_pattern_library import (
    CrossRepoPattern,
    SharedPatternLibrary,
    check_promotion_eligibility,
    promote_pattern_to_recipe,
)
from bluei.engine.structural_hash import (
    compute_structural_hash,
    fuzzy_structural_match,
    normalize_for_sharing,
)
from bluei.app.auto_tune import adjust_replay_thresholds


class TestCrossRepoSharing:
    def test_publish_from_repo_a_lookup_from_repo_b(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "shared")
        lib.publish(
            rule="broad-except",
            language="python",
            before_snippet="except:\n    pass",
            after_snippet="except Exception:\n    pass",
            confidence=0.92,
            repo_id="repo-alpha",
            success_count=5,
            failure_count=0,
        )
        lib.publish(
            rule="broad-except",
            language="python",
            before_snippet="except:\n    pass",
            after_snippet="except Exception:\n    pass",
            confidence=0.88,
            repo_id="repo-beta",
            success_count=3,
            failure_count=0,
        )
        result = lib.lookup("broad-except", "except:\n    pass", "python")
        assert result is not None
        assert "repo-alpha" in result.source_repos
        assert "repo-beta" in result.source_repos

    def test_privacy_normalization_strips_secrets(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "shared")
        lib.publish(
            rule="hardcoded-secret",
            language="python",
            before_snippet='API_KEY = "sk-abc123def456"',
            after_snippet='API_KEY = os.environ["API_KEY"]',
            confidence=0.9,
            repo_id="repo-x",
        )
        shared = list(lib._get_cached("python").values())
        assert len(shared) == 1
        assert "sk-abc123def456" not in shared[0].before_snippet

    def test_shared_library_stats(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "shared")
        lib.publish(
            rule="r1",
            language="python",
            before_snippet="a",
            after_snippet="b",
            confidence=0.9,
            repo_id="r1",
        )
        lib.publish(
            rule="r2",
            language="typescript",
            before_snippet="x",
            after_snippet="y",
            confidence=0.9,
            repo_id="r1",
        )
        stats = lib.load_stats()
        assert stats["total_patterns"] == 2
        assert "python" in stats["languages"]
        assert "typescript" in stats["languages"]


class TestFuzzyMatching:
    def test_different_var_names_match(self):
        code_a = "result = compute(total + offset)"
        code_b = "value = calculate(sum + delta)"
        score = fuzzy_structural_match(code_a, code_b, "python")
        assert score >= 0.5

    def test_different_operators_no_match(self):
        code_a = "x = a + b"
        code_b = "x = a - b"
        score = fuzzy_structural_match(code_a, code_b, "python")
        assert score < 0.7

    def test_structural_hash_equivalence(self):
        h1 = compute_structural_hash("result = foo(a, b)", "python")
        h2 = compute_structural_hash("total = bar(x, y)", "python")
        assert h1 == h2

    def test_structural_hash_operator_difference(self):
        h1 = compute_structural_hash("x = a + b", "python")
        h2 = compute_structural_hash("x = a - b", "python")
        assert h1 != h2

    def test_normalize_for_sharing_strips_imports(self):
        shared = normalize_for_sharing("from mycorp.secrets import KEY", "python")
        assert "mycorp" not in shared
        assert "KEY" not in shared


class TestCompositeIntegration:
    def test_three_step_pattern_two_files(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f1 = worktree / "src" / "main.py"
        f2 = worktree / "src" / "utils.py"
        f1.parent.mkdir(parents=True, exist_ok=True)
        f2.parent.mkdir(parents=True, exist_ok=True)
        f1.write_text("# old header\nimport os\nresult = old_func()")
        f2.write_text("def old_func():\n    return 42\n")

        pat = CompositePattern(
            pattern_id="comp-integ1",
            rule="multi-fix",
            language="python",
            steps=[
                CompositePatternStep(
                    order=0,
                    description="fix header",
                    match="old header",
                    replacement="new header",
                    scope="file_header",
                ),
                CompositePatternStep(
                    order=1,
                    description="fix import",
                    match="old_func",
                    replacement="new_func",
                    scope="finding_location",
                ),
                CompositePatternStep(
                    order=2,
                    description="fix utils",
                    match="old_func",
                    replacement="new_func",
                    scope="related_file",
                    file_pattern="src/utils.py",
                ),
            ],
            confidence=0.85,
        )
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is True, f"err={err}"
        assert "new header" in f1.read_text()
        assert "new_func" in f1.read_text()
        assert "new_func" in f2.read_text()
        assert len(changes) == 2

    def test_rollback_on_middle_step_failure(self, tmp_path):
        worktree = tmp_path / "repo"
        worktree.mkdir()
        f1 = worktree / "src" / "main.py"
        f1.parent.mkdir(parents=True, exist_ok=True)
        original = "original content"
        f1.write_text(original)

        pat = CompositePattern(
            pattern_id="comp-rollback",
            rule="rollback-test",
            language="python",
            steps=[
                CompositePatternStep(
                    order=0,
                    description="step1 ok",
                    match="original",
                    replacement="modified",
                    scope="finding_location",
                ),
                CompositePatternStep(
                    order=1,
                    description="step2 fail",
                    match="x",
                    replacement="y",
                    scope="related_file",
                    file_pattern="nonexistent/*.py",
                ),
            ],
            confidence=0.7,
        )
        applier = CompositePatternApplier()
        ok, changes, err = applier.apply(pat, "src/main.py", worktree)
        assert ok is False
        assert f1.read_text() == original

    def test_composite_store_persistence(self, tmp_path):
        path = tmp_path / "comp.jsonl"
        store1 = CompositePatternStore(path)
        pat = CompositePattern(
            pattern_id="",
            rule="persist-test",
            language="python",
            steps=[
                CompositePatternStep(
                    order=0,
                    description="t",
                    match="a",
                    replacement="b",
                    scope="finding_location",
                )
            ],
        )
        pid = store1.append(pat)
        store2 = CompositePatternStore(path)
        found = store2.lookup("persist-test")
        assert found is not None
        assert found.pattern_id == pid


class TestPromotionIntegration:
    def test_high_confidence_promoted_to_recipe(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "shared")
        for repo in ["a", "b", "c", "d", "e"]:
            lib.publish(
                rule="promo-test",
                language="python",
                before_snippet="raise Error()",
                after_snippet="raise Error() from cause",
                confidence=0.96,
                repo_id=repo,
                success_count=5,
                failure_count=0,
            )
        cross_pat = None
        for v in lib._get_cached("python").values():
            if v.rule == "promo-test":
                cross_pat = v
                break
        assert cross_pat is not None
        assert check_promotion_eligibility(cross_pat)
        result = promote_pattern_to_recipe(cross_pat, "python", tmp_path / "staged")
        assert result == PromotionResult.PROMOTED
        staged_dir = tmp_path / "staged"
        content = sorted(staged_dir.glob("*.yaml"))[0].read_text()
        assert "rule: promo-test" in content
        assert "regex_substitute" in content

    def test_low_confidence_not_promoted(self, tmp_path):
        pat = CrossRepoPattern(
            pattern_id="xp-low-1234",
            rule="low-conf",
            language="python",
            structural_hash="hash1",
            before_snippet="a",
            after_snippet="b",
            confidence=0.5,
            source_repos={"r1", "r2"},
        )
        assert not check_promotion_eligibility(pat)


class TestThresholdTuning:
    def test_success_lowers_failure_raises(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"

        records = [_make_telemetry(rule="tune-rule", success=True) for _ in range(25)]
        _write_telemetry(telem, records)
        result = adjust_replay_thresholds(telem, tune)
        assert "tune-rule" in result
        lowered = result["tune-rule"]
        assert lowered < 0.85

        records2 = [_make_telemetry(rule="tune-rule", success=False)]
        _write_telemetry(telem, records2)
        result2 = adjust_replay_thresholds(telem, tune)
        assert "tune-rule" in result2
        assert result2["tune-rule"] > lowered

    def test_persistence_across_calls(self, tmp_path):
        telem = tmp_path / "telemetry.jsonl"
        tune = tmp_path / "tune.json"
        records = [
            _make_telemetry(rule="persist-rule", success=True) for _ in range(25)
        ]
        _write_telemetry(telem, records)
        adjust_replay_thresholds(telem, tune)

        from bluei.app.auto_tune import _load_tune_state

        state = _load_tune_state(tune)
        assert "persist-rule" in state.get("replay_thresholds", {})


class TestCascadeWithComposite:
    def test_default_stages_include_composite(self):
        stages = default_cascade_stages()
        names = [s.name for s in stages]
        assert "composite-pattern" in names

    def test_composite_stage_tier(self):
        stage = CompositePatternCascadeStage()
        assert stage.tier == 2


def _make_telemetry(rule="test-rule", success=True):
    return {
        "rule": rule,
        "success": success,
        "final_stage": "pattern-replay" if success else "cascade_exhausted",
    }


def _write_telemetry(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n")
