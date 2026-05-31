import yaml
from pathlib import Path

import pytest

from bluei.engine.shared_pattern_library import (
    CrossRepoPattern,
    promote_pattern_to_recipe,
    check_promotion_eligibility,
)


def _eligible_pattern(**overrides):
    defaults = dict(
        pattern_id="xp-ruff-b904-abc12345",
        rule="ruff-b904",
        language="python",
        structural_hash="deadbeefcafe1234",
        before_snippet="raise ValueError(msg)",
        after_snippet="raise ValueError(msg) from cause",
        confidence=0.96,
        source_repos={"repo-a", "repo-b", "repo-c", "repo-d", "repo-e"},
        success_count=25,
        failure_count=0,
    )
    defaults.update(overrides)
    return CrossRepoPattern(**defaults)


class TestBasicRoundTrip:
    def test_promote_and_parse_all_fields_match(self, tmp_path):
        pat = _eligible_pattern(
            rule="ruff-b904",
            before_snippet="raise ValueError(msg)",
            after_snippet="raise ValueError(msg) from cause",
            structural_hash="deadbeefcafe1234",
            confidence=0.97,
            source_repos={"repo-a", "repo-b", "repo-c", "repo-d", "repo-e"},
            success_count=30,
            failure_count=0,
        )
        out_dir = tmp_path / "staged"
        result = promote_pattern_to_recipe(pat, "python", out_dir)
        assert result is not None
        assert result.exists()
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["rule"] == "ruff-b904"
        assert loaded["language"] == "python"
        assert loaded["match"]["rule"] == "ruff-b904"
        assert loaded["match"]["type"] == "rule_exact"
        assert loaded["replacement"]["type"] == "regex_substitute"
        assert loaded["replacement"]["pattern"] == "raise ValueError(msg)"
        assert loaded["replacement"]["replacement"] == "raise ValueError(msg) from cause"
        assert loaded["safety"] == "idempotent"
        assert loaded["metadata"]["source_pattern_id"] == pat.pattern_id
        assert loaded["metadata"]["author"] == "bluei-auto-promotion"
        assert loaded["metadata"]["version"] == 1
        assert sorted(loaded["metadata"]["source_repos"]) == sorted(pat.source_repos)
        assert "promoted_at" in loaded["metadata"]
        assert loaded["validation"]["run_baseline"] is True
        assert loaded["id"].startswith("auto-")

    def test_round_trip_preserves_rule_text_verbatim(self, tmp_path):
        rule_text = "my-custom-linter-rule"
        pat = _eligible_pattern(rule=rule_text)
        result = promote_pattern_to_recipe(pat, "go", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["rule"] == rule_text
        assert loaded["match"]["rule"] == rule_text


class TestYAMLInjectionSafety:
    def test_colons_in_snippets(self, tmp_path):
        pat = _eligible_pattern(
            before_snippet="url: http://example.com:8080",
            after_snippet="url: https://secure.example.com:443",
        )
        result = promote_pattern_to_recipe(pat, "yaml", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["replacement"]["pattern"] == "url: http://example.com:8080"
        assert loaded["replacement"]["replacement"] == "url: https://secure.example.com:443"

    def test_newlines_in_snippets(self, tmp_path):
        before = "line1\nline2\nline3"
        after = "line1\nline2-fixed\nline3"
        pat = _eligible_pattern(before_snippet=before, after_snippet=after)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["replacement"]["pattern"] == before
        assert loaded["replacement"]["replacement"] == after

    def test_quotes_in_snippets(self, tmp_path):
        before = 'msg = "hello \'world\'"'
        after = "msg = 'hello \"world\"'"
        pat = _eligible_pattern(before_snippet=before, after_snippet=after)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["replacement"]["pattern"] == before
        assert loaded["replacement"]["replacement"] == after

    def test_yaml_document_separator_in_snippet(self, tmp_path):
        before = "---\ntitle: test\n---"
        after = "---\ntitle: fixed\n---"
        pat = _eligible_pattern(before_snippet=before, after_snippet=after)
        result = promote_pattern_to_recipe(pat, "yaml", tmp_path)
        raw = result.read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw)
        assert loaded is not None
        assert loaded["replacement"]["pattern"] == before
        assert loaded["replacement"]["replacement"] == after

    def test_mixed_metacharacters_in_rule(self, tmp_path):
        pat = _eligible_pattern(rule="rule:with:colons & [brackets]")
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["rule"] == "rule:with:colons & [brackets]"


class TestIdempotentSafetyTag:
    def test_confidence_095_gets_idempotent(self, tmp_path):
        pat = _eligible_pattern(confidence=0.95)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["safety"] == "idempotent"

    def test_confidence_099_gets_idempotent(self, tmp_path):
        pat = _eligible_pattern(confidence=0.99)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["safety"] == "idempotent"

    def test_confidence_10_gets_idempotent(self, tmp_path):
        pat = _eligible_pattern(confidence=1.0)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["safety"] == "idempotent"


class TestNeedsValidationSafetyTag:
    def test_confidence_094_gets_needs_validation(self, tmp_path):
        pat = _eligible_pattern(confidence=0.94)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["safety"] == "needs_validation"

    def test_confidence_093_gets_needs_validation(self, tmp_path):
        pat = _eligible_pattern(confidence=0.93)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["safety"] == "needs_validation"

    def test_confidence_boundary_below_095(self, tmp_path):
        pat = _eligible_pattern(confidence=0.9499)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["safety"] == "needs_validation"


class TestIneligiblePattern:
    def test_too_few_repos_returns_none(self, tmp_path):
        pat = _eligible_pattern(source_repos={"repo-a", "repo-b"})
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is None

    def test_too_few_repos_creates_no_file(self, tmp_path):
        pat = _eligible_pattern(source_repos={"repo-a"})
        promote_pattern_to_recipe(pat, "python", tmp_path)
        yaml_files = list(tmp_path.glob("*.yaml"))
        assert yaml_files == []

    def test_has_failures_returns_none(self, tmp_path):
        pat = _eligible_pattern(failure_count=1)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is None

    def test_too_few_applications_returns_none(self, tmp_path):
        pat = _eligible_pattern(success_count=10, failure_count=0)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is None

    def test_check_eligibility_matches_promote(self, tmp_path):
        pat = _eligible_pattern(source_repos={"repo-a"})
        assert check_promotion_eligibility(pat) is False
        assert promote_pattern_to_recipe(pat, "python", tmp_path) is None

    def test_eligible_check_matches_promote(self, tmp_path):
        pat = _eligible_pattern()
        assert check_promotion_eligibility(pat) is True
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is not None


class TestSpecialCharsInRuleFilename:
    def test_colon_sanitized_in_filename(self, tmp_path):
        pat = _eligible_pattern(rule="my:rule:with:colons")
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is not None
        assert ":" not in result.name
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["rule"] == "my:rule:with:colons"

    def test_slash_sanitized_in_filename(self, tmp_path):
        pat = _eligible_pattern(rule="path/to/rule")
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is not None
        assert "/" not in result.name
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["rule"] == "path/to/rule"

    def test_spaces_sanitized_in_filename(self, tmp_path):
        pat = _eligible_pattern(rule="rule with spaces")
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is not None
        assert " " not in result.name
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["rule"] == "rule with spaces"

    def test_hash_sanitized_in_filename(self, tmp_path):
        pat = _eligible_pattern(rule="rule#with#hashes")
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is not None
        assert "#" not in result.name
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["rule"] == "rule#with#hashes"

    def test_structural_hash_in_filename(self, tmp_path):
        pat = _eligible_pattern(structural_hash="abcdef0123456789")
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        assert result is not None
        assert "abcdef01" in result.name

    def test_recipe_id_format(self, tmp_path):
        pat = _eligible_pattern(rule="ruff-b904", structural_hash="deadbeef12345678")
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        assert loaded["id"].startswith("auto-ruff-b904-")
        assert "deadbeef" in loaded["id"]

    def test_long_rule_truncated_in_id(self, tmp_path):
        long_rule = "a" * 100
        pat = _eligible_pattern(rule=long_rule)
        result = promote_pattern_to_recipe(pat, "python", tmp_path)
        loaded = yaml.safe_load(result.read_text(encoding="utf-8"))
        rule_part = loaded["id"].split("-")[1] if "auto-" in loaded["id"] else ""
        assert len(rule_part) <= 32
        assert loaded["rule"] == long_rule
