import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bluei.engine.governance import PromotionResult
from bluei.engine.shared_pattern_library import (
    CrossRepoPattern,
    SharedPatternLibrary,
    check_promotion_eligibility,
    promote_pattern_to_recipe,
    MIN_CONFIDENCE_TO_SHARE,
    PROMOTION_MIN_REPOS,
)
from bluei.engine.pattern_store import FixPattern, FixPatternStore
from bluei.engine.pattern_replay import _resolve_pattern


def _mf(make_finding, **overrides):
    """Wrap conftest make_finding with shared_pattern_library-specific defaults."""
    defaults = {
        "finding_id": "f-001",
        "rule": "test-rule",
        "snippet": "result = amount + discount",
        "path": "test.py",
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return make_finding(**defaults)


def _make_xrepo(**overrides):
    defaults = dict(
        pattern_id="xp-test-rule-abcd1234",
        rule="test-rule",
        language="python",
        structural_hash="abcd1234abcd1234",
        before_snippet="<v0> = <v1> + <v2>",
        after_snippet="<v0> = <v1> - <v2>",
        confidence=0.9,
        source_repos={"repo-a"},
        success_count=5,
        failure_count=0,
    )
    defaults.update(overrides)
    return CrossRepoPattern(**defaults)


class TestCrossRepoPattern:
    def test_to_dict(self):
        p = _make_xrepo()
        d = p.to_dict()
        assert d["pattern_id"] == "xp-test-rule-abcd1234"
        assert d["rule"] == "test-rule"
        assert isinstance(d["source_repos"], list)
        assert "repo-a" in d["source_repos"]

    def test_from_dict(self):
        d = {
            "pattern_id": "xp-test",
            "rule": "r1",
            "language": "python",
            "structural_hash": "abc123",
            "before_snippet": "before",
            "after_snippet": "after",
            "confidence": 0.85,
            "source_repos": ["repo-a", "repo-b"],
            "success_count": 10,
            "failure_count": 1,
        }
        p = CrossRepoPattern.from_dict(d)
        assert p.source_repos == {"repo-a", "repo-b"}
        assert p.success_count == 10

    def test_from_dict_string_repos(self):
        d = {
            "pattern_id": "xp-test",
            "rule": "r1",
            "language": "python",
            "structural_hash": "abc123",
            "before_snippet": "before",
            "after_snippet": "after",
            "source_repos": "repo-a",
        }
        p = CrossRepoPattern.from_dict(d)
        assert p.source_repos == {"repo-a"}


class TestSharedPatternLibraryPublish:
    def test_publish_creates_language_file(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        pid = lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            confidence=0.9,
            repo_id="repo-a",
        )
        assert pid.startswith("xp-")
        assert (tmp_path / "lib" / "python.jsonl").exists()

    def test_publish_below_threshold_returns_empty(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        pid = lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="x = 1",
            after_snippet="x = 2",
            confidence=0.5,
            repo_id="repo-a",
        )
        assert pid == ""

    def test_publish_merges_same_structural_hash(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            confidence=0.9,
            repo_id="repo-a",
            success_count=5,
        )
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="total = price + rebate",
            after_snippet="total = price - rebate",
            confidence=0.95,
            repo_id="repo-b",
            success_count=3,
        )
        result = lib.lookup("test-rule", "sum = a + b", "python")
        assert result is not None
        assert "repo-a" in result.source_repos
        assert "repo-b" in result.source_repos
        assert result.success_count >= 8

    def test_publish_different_rules_are_separate(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="rule-a",
            language="python",
            before_snippet="x = 1",
            after_snippet="x = 2",
            confidence=0.9,
            repo_id="repo-a",
        )
        lib.publish(
            rule="rule-b",
            language="python",
            before_snippet="x = 1",
            after_snippet="x = 3",
            confidence=0.9,
            repo_id="repo-a",
        )
        assert (tmp_path / "lib" / "python.jsonl").exists()
        records = []
        for line in (tmp_path / "lib" / "python.jsonl").read_text().splitlines():
            records.append(json.loads(line))
        assert len(records) == 2

    def test_publish_persists_to_disk(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="x = 1 + 2",
            after_snippet="x = 1 - 2",
            confidence=0.9,
            repo_id="repo-a",
        )
        lib2 = SharedPatternLibrary(tmp_path / "lib")
        result = lib2.lookup("test-rule", "x = 1 + 2", "python")
        assert result is not None


class TestSharedPatternLibraryLookup:
    def test_exact_structural_match(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            confidence=0.9,
            repo_id="repo-a",
        )
        result = lib.lookup("test-rule", "total = price + rebate", "python")
        assert result is not None
        assert result.confidence >= 0.9

    def test_no_match(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            confidence=0.9,
            repo_id="repo-a",
        )
        result = lib.lookup("test-rule", "def foo():\n    pass", "python")
        assert result is None

    def test_fuzzy_fallback(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="result = compute(price) + fee",
            after_snippet="result = compute(price) - fee",
            confidence=0.9,
            repo_id="repo-a",
        )
        result = lib.lookup(
            "test-rule",
            "total = calculate(base) + tax",
            "python",
        )
        assert result is not None

    def test_wrong_rule_no_match(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="rule-a",
            language="python",
            before_snippet="x = 1 + 2",
            after_snippet="x = 1 - 2",
            confidence=0.9,
            repo_id="repo-a",
        )
        result = lib.lookup("rule-b", "x = 1 + 2", "python")
        assert result is None

    def test_wrong_language_no_match(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="x = 1 + 2",
            after_snippet="x = 1 - 2",
            confidence=0.9,
            repo_id="repo-a",
        )
        lang_file = tmp_path / "lib" / "python.jsonl"
        assert lang_file.exists()
        assert not (tmp_path / "lib" / "go.jsonl").exists()

    def test_with_catalog(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="ruff-b904",
            language="python",
            before_snippet="raise ValueError(msg)",
            after_snippet="raise ValueError(msg) from e",
            confidence=0.9,
            repo_id="repo-a",
        )
        from bluei.engine.constants import DETECTOR_CATALOG

        result = lib.lookup(
            "ruff-b904",
            "raise TypeError(err)",
            "python",
            catalog=DETECTOR_CATALOG,
        )
        assert result is not None


class TestSharedPatternLibraryStats:
    def test_empty_stats(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        stats = lib.load_stats()
        assert stats["total_patterns"] == 0

    def test_stats_after_publish(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="x = 1",
            after_snippet="x = 2",
            confidence=0.9,
            repo_id="repo-a",
        )
        stats = lib.load_stats()
        assert stats["total_patterns"] == 1
        assert "python" in stats["languages"]


class TestResolvePatternWithSharedLibrary:
    def test_per_repo_store_hit_first(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "repo_patterns.jsonl")
        pattern = FixPattern(
            pattern_id="",
            rule="test-rule",
            language="python",
            file_path="test.py",
            before_snippet="x = 1 + 2",
            after_snippet="x = 1 - 2",
            diff_patch="fake",
            confidence=0.9,
        )
        store.append(pattern)
        shared_lib = SharedPatternLibrary(tmp_path / "shared")
        finding = _mf(make_finding, snippet="x = 1 + 2")
        result = _resolve_pattern(finding, store, tmp_path / "log.txt", shared_lib)
        assert result is not None

    def test_shared_library_hit_when_store_misses(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "repo_patterns.jsonl")
        shared_lib = SharedPatternLibrary(tmp_path / "shared")
        shared_lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            confidence=0.9,
            repo_id="other-repo",
        )
        finding = _mf(make_finding, snippet="total = price + rebate")
        result = _resolve_pattern(finding, store, tmp_path / "log.txt", shared_lib)
        assert result is not None
        assert result.pattern_id.startswith("xp-")

    def test_no_match_anywhere(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "repo_patterns.jsonl")
        shared_lib = SharedPatternLibrary(tmp_path / "shared")
        finding = _mf(make_finding, snippet="def foo():\n    pass")
        result = _resolve_pattern(finding, store, tmp_path / "log.txt", shared_lib)
        assert result is None

    def test_none_shared_library_ok(self, tmp_path, make_finding):
        store = FixPatternStore(tmp_path / "repo_patterns.jsonl")
        finding = _mf(make_finding, snippet="x = 1 + 2")
        result = _resolve_pattern(finding, store, tmp_path / "log.txt", None)
        assert result is None


class TestPrivacyNormalization:
    def test_published_snippet_has_normalized_vars(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="result = my_secret_value + discount",
            after_snippet="result = my_secret_value - discount",
            confidence=0.9,
            repo_id="repo-a",
        )
        lang_file = tmp_path / "lib" / "python.jsonl"
        content = lang_file.read_text()
        assert "my_secret_value" not in content

    def test_published_snippet_has_no_string_literals(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet='password = "supersecret123"',
            after_snippet='password = get_env("PASSWORD")',
            confidence=0.9,
            repo_id="repo-a",
        )
        lang_file = tmp_path / "lib" / "python.jsonl"
        content = lang_file.read_text()
        assert "supersecret123" not in content


class TestCascadeContextSharedLibrary:
    def test_cascade_context_has_shared_library_field(self):
        from bluei.engine.cascade import CascadeContext

        ctx = CascadeContext(shared_library="test")
        assert ctx.shared_library == "test"

    def test_cascade_context_default_none(self):
        from bluei.engine.cascade import CascadeContext

        ctx = CascadeContext()
        assert ctx.shared_library is None


class TestEndToEndCrossRepo:
    def test_learn_from_repo_a_apply_to_repo_b(self, tmp_path, make_finding):
        lib = SharedPatternLibrary(tmp_path / "shared")
        lib.publish(
            rule="discount-math-sign",
            language="python",
            before_snippet="total = amount + discount",
            after_snippet="total = amount - discount",
            confidence=0.92,
            repo_id="ecommerce-app",
            success_count=10,
            failure_count=0,
        )

        repo_b_store = FixPatternStore(tmp_path / "repo_b" / "patterns.jsonl")
        finding = _mf(
            make_finding,
            rule="discount-math-sign",
            snippet="price = base_price + markup",
            path="pricing.py",
        )
        result = _resolve_pattern(finding, repo_b_store, tmp_path / "log.txt", lib)
        assert result is not None
        # Cross-repo confidence is capped below PROMPT_HINT_THRESHOLD because
        # privacy-normalized snippets can't be literally replayed in real files.
        assert result.confidence == pytest.approx(0.49)

    def test_multi_repo_confidence_increases(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "shared")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="x = a + b",
            after_snippet="x = a - b",
            confidence=0.9,
            repo_id="repo-a",
            success_count=5,
        )
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="y = c + d",
            after_snippet="y = c - d",
            confidence=0.95,
            repo_id="repo-b",
            success_count=8,
        )
        result = lib.lookup("test-rule", "z = e + f", "python")
        assert result is not None
        assert len(result.source_repos) == 2
        assert result.success_count >= 13


# ── Extracted from test_medium_modules_remaining.py ──
# Additional lookup, composite, promotion, and edge case tests


class TestLookupFuzzyScan:
    def test_fuzzy_scan_skips_insufficient_repos(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        lang_file.parent.mkdir(parents=True, exist_ok=True)
        pat = CrossRepoPattern(
            pattern_id="xp-test",
            rule="test-rule",
            language="python",
            structural_hash="manualhash123456",
            before_snippet="x = foo(a, b)",
            after_snippet="x = bar(a, b)",
            confidence=0.9,
            source_repos=set(),
            success_count=5,
            failure_count=0,
        )
        lang_file.write_text(json.dumps(pat.to_dict()) + "\n")
        result = lib.lookup("test-rule", "x = baz(c, d, e)", "python")
        assert result is None

    def test_fuzzy_scan_finds_match(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lib.publish(
            rule="test-rule",
            language="python",
            before_snippet="x = foo(a, b)",
            after_snippet="x = bar(a, b)",
            confidence=0.9,
            repo_id="repo-a",
        )
        result = lib.lookup("test-rule", "x = baz(c, d, e)", "python")
        assert result is not None


class TestPublishComposite:
    def test_below_threshold(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        pattern = MagicMock()
        pattern.confidence = 0.5
        pattern.rule = "test-rule"
        pattern.pattern_id = "cp-001"
        result = lib.publish_composite(pattern, "repo-a", "python")
        assert result == ""

    def test_new_entry(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        pattern = MagicMock()
        pattern.confidence = 0.9
        pattern.rule = "test-rule"
        pattern.pattern_id = "cp-001"
        pattern.to_dict.return_value = {
            "pattern_id": "cp-001",
            "rule": "test-rule",
            "confidence": 0.9,
        }
        result = lib.publish_composite(pattern, "repo-a", "python")
        assert "test-rule" in result

    def test_existing_entry_update_repo(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        pattern1 = MagicMock()
        pattern1.confidence = 0.9
        pattern1.rule = "test-rule"
        pattern1.pattern_id = "cp-001"
        pattern1.to_dict.return_value = {
            "pattern_id": "cp-001",
            "rule": "test-rule",
            "confidence": 0.9,
        }
        lib.publish_composite(pattern1, "repo-a", "python")
        pattern2 = MagicMock()
        pattern2.confidence = 0.95
        pattern2.rule = "test-rule"
        pattern2.pattern_id = "cp-001"
        pattern2.to_dict.return_value = {
            "pattern_id": "cp-001",
            "rule": "test-rule",
            "confidence": 0.95,
        }
        lib.publish_composite(pattern2, "repo-b", "python")
        result = lib.lookup_composite("test-rule", "python")
        assert result is not None
        assert len(result) == 1
        assert "repo-a" in result[0]["source_repos"]
        assert "repo-b" in result[0]["source_repos"]

    def test_existing_entry_no_source_repos_key(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        composite_file = tmp_path / "lib" / "python_composite.jsonl"
        composite_file.parent.mkdir(parents=True, exist_ok=True)
        composite_file.write_text(
            json.dumps({"pattern_id": "cp-001", "rule": "test-rule", "confidence": 0.9})
            + "\n"
        )
        pattern = MagicMock()
        pattern.confidence = 0.95
        pattern.rule = "test-rule"
        pattern.pattern_id = "cp-001"
        pattern.to_dict.return_value = {
            "pattern_id": "cp-001",
            "rule": "test-rule",
            "confidence": 0.95,
        }
        lib.publish_composite(pattern, "repo-a", "python")
        result = lib.lookup_composite("test-rule", "python")
        assert result is not None
        assert "source_repos" in result[0]
        assert "repo-a" in result[0]["source_repos"]


class TestLoadCompositeEdgeCases:
    def test_empty_lines_skipped(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        composite_file = tmp_path / "lib" / "python_composite.jsonl"
        composite_file.parent.mkdir(parents=True, exist_ok=True)
        composite_file.write_text(
            "\n\n"
            + json.dumps(
                {"pattern_id": "cp-001", "rule": "test-rule", "confidence": 0.9}
            )
            + "\n"
        )
        result = lib.lookup_composite("test-rule", "python")
        assert result is not None

    def test_bad_json_skipped(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        composite_file = tmp_path / "lib" / "python_composite.jsonl"
        composite_file.parent.mkdir(parents=True, exist_ok=True)
        composite_file.write_text(
            "bad json line\n"
            + json.dumps(
                {"pattern_id": "cp-001", "rule": "test-rule", "confidence": 0.9}
            )
            + "\n"
        )
        result = lib.lookup_composite("test-rule", "python")
        assert result is not None

    def test_oserror_returns_empty(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        composite_file = tmp_path / "lib" / "python_composite.jsonl"
        composite_file.parent.mkdir(parents=True, exist_ok=True)
        composite_file.write_text("data\n")
        with patch.object(Path, "read_text", side_effect=OSError("fail")):
            result = lib._load_composite_language(composite_file)
        assert result == {}


class TestMergeConfidenceRaw:
    def test_basic_merge(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        result = lib._merge_confidence_raw(0.8, 0.9, 5, 1)
        assert 0.0 <= result <= 1.0

    def test_zero_stats(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        result = lib._merge_confidence_raw(0.5, 0.9, 0, 0)
        assert 0.0 <= result <= 1.0


class TestLoadStatsEmptyLines:
    def test_empty_lines_in_stats_file(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        lang_file.parent.mkdir(parents=True, exist_ok=True)
        lang_file.write_text(
            "\n\n"
            + json.dumps(
                {
                    "pattern_id": "xp-test",
                    "rule": "r1",
                    "language": "python",
                    "structural_hash": "abc",
                    "before_snippet": "b",
                    "after_snippet": "a",
                    "source_repos": ["repo-a"],
                }
            )
            + "\n"
        )
        stats = lib.load_stats()
        assert stats["total_patterns"] == 1


class TestLoadLanguageEdgeCases:
    def test_empty_lines_skipped(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        lang_file.parent.mkdir(parents=True, exist_ok=True)
        lang_file.write_text(
            "\n\n"
            + json.dumps(
                {
                    "pattern_id": "xp-test",
                    "rule": "r1",
                    "language": "python",
                    "structural_hash": "abc",
                    "before_snippet": "b",
                    "after_snippet": "a",
                }
            )
            + "\n"
        )
        records = lib._load_language("python")
        assert len(records) == 1

    def test_oserror_returns_empty(self, tmp_path):
        lib = SharedPatternLibrary(tmp_path / "lib")
        lang_file = tmp_path / "lib" / "python.jsonl"
        lang_file.parent.mkdir(parents=True, exist_ok=True)
        lang_file.write_text("{}\n")
        with patch.object(Path, "read_text", side_effect=OSError("fail")):
            records = lib._load_language("python")
        assert records == {}


class TestCheckPromotionEligibility:
    def test_zero_total_returns_false(self):
        p = _make_xrepo(
            success_count=0,
            failure_count=0,
            source_repos={"r1", "r2", "r3", "r4", "r5"},
        )
        assert check_promotion_eligibility(p) is False

    def test_too_few_repos(self):
        p = _make_xrepo(success_count=20, failure_count=0, source_repos={"r1"})
        assert check_promotion_eligibility(p) is False

    def test_too_few_applications(self):
        p = _make_xrepo(
            success_count=5,
            failure_count=0,
            source_repos={"r1", "r2", "r3", "r4", "r5"},
        )
        assert check_promotion_eligibility(p) is False

    def test_low_success_rate(self):
        p = _make_xrepo(
            success_count=15,
            failure_count=5,
            source_repos={"r1", "r2", "r3", "r4", "r5"},
        )
        assert check_promotion_eligibility(p) is False

    def test_has_failures(self):
        p = _make_xrepo(
            success_count=20,
            failure_count=1,
            source_repos={"r1", "r2", "r3", "r4", "r5"},
        )
        assert check_promotion_eligibility(p) is False

    def test_eligible(self):
        p = _make_xrepo(
            success_count=20,
            failure_count=0,
            source_repos={"r1", "r2", "r3", "r4", "r5"},
        )
        assert check_promotion_eligibility(p) is True


class TestPromotePatternToRecipe:
    def test_not_eligible(self, tmp_path):
        p = _make_xrepo(success_count=0, failure_count=0)
        result = promote_pattern_to_recipe(p, "python", tmp_path / "recipes")
        assert result == PromotionResult.NOT_ELIGIBLE

    def test_eligible_idempotent(self, tmp_path):
        p = _make_xrepo(
            success_count=20,
            failure_count=0,
            confidence=0.96,
            source_repos={"r1", "r2", "r3", "r4", "r5"},
        )
        recipes_dir = tmp_path / "recipes"
        result = promote_pattern_to_recipe(p, "python", recipes_dir)
        assert result == PromotionResult.PROMOTED
        content = sorted(recipes_dir.glob("*.yaml"))[0].read_text()
        assert "idempotent" in content

    def test_eligible_needs_validation(self, tmp_path):
        p = _make_xrepo(
            success_count=20,
            failure_count=0,
            confidence=0.93,
            source_repos={"r1", "r2", "r3", "r4", "r5"},
        )
        recipes_dir = tmp_path / "recipes"
        result = promote_pattern_to_recipe(p, "python", recipes_dir)
        assert result == PromotionResult.PROMOTED
        content = sorted(recipes_dir.glob("*.yaml"))[0].read_text()
        assert "needs_validation" in content
